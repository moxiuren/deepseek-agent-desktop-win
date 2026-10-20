using DeepSeekAgent.Desktop;

namespace DeepSeekAgent.SelfTest;

/// <summary>
/// `LogRetention` 离线自测 —— 全程在临时目录里造文件，绝不碰真实日志目录。
/// </summary>
///
/// <remarks>
/// 钉死的核心命题：**清理工具只能删自己的东西，且三类保护必须生效**。
/// 日志清理出错是不可逆的 —— 删掉封禁当天的现场，就再也没有第二次机会。
/// 所以这里每一条保护都要单独验证，而不是「跑一遍看着对」。
/// </remarks>
internal static class Program
{
    private static int _pass;
    private static readonly List<string> Fail = new();
    private static readonly DateTime Now = new(2026, 10, 19, 12, 0, 0, DateTimeKind.Utc);

    private static void Check(string name, bool cond, string detail = "")
    {
        if (cond) _pass++;
        else Fail.Add(string.IsNullOrEmpty(detail) ? name : $"{name} — {detail}");
        Console.WriteLine($"  [{(cond ? "通过" : "失败")}] {name}" +
                          (cond || string.IsNullOrEmpty(detail) ? "" : $" — {detail}"));
    }

    /// <summary>造一个文件并把 mtime 设成指定时刻。</summary>
    private static FileInfo Make(DirectoryInfo dir, string name, DateTime utc, int bytes = 100)
    {
        var p = Path.Combine(dir.FullName, name);
        File.WriteAllBytes(p, new byte[bytes]);
        File.SetLastWriteTimeUtc(p, utc);
        return new FileInfo(p);
    }

    private static DirectoryInfo FreshDir(string tag)
    {
        var d = new DirectoryInfo(Path.Combine(Path.GetTempPath(), $"logret-{tag}-{Guid.NewGuid():N}"));
        d.Create();
        return d;
    }

    private static int Main()
    {
        Console.OutputEncoding = System.Text.Encoding.UTF8;

        // ---------------- [1] 目录不存在 → 空计划 ----------------
        Console.WriteLine("[1] 目录不存在 → 空计划（不抛异常）");
        var ghost = new DirectoryInfo(Path.Combine(Path.GetTempPath(), $"ghost-{Guid.NewGuid():N}"));
        var plan0 = LogRetention.Plan(ghost, Now);
        Check("不存在的目录 → 0 个待删", plan0.Count == 0);

        // ---------------- [2] 24 小时保护窗 ----------------
        Console.WriteLine("\n[2] 最近 24 小时一律不删（防崩溃循环抹掉现场）");
        var d2 = FreshDir("window");
        try
        {
            var fresh = Make(d2, "engine-20261019-110000.log", Now.AddHours(-1));
            var freshish = Make(d2, "engine-20261018-140000.log", Now.AddHours(-22));
            var old = Make(d2, "engine-20261010-100000.log", Now.AddDays(-9));
            var plan = LogRetention.Plan(d2, Now);
            Check("1 小时前 → 保留", !plan.Any(f => f.Name == fresh.Name));
            Check("22 小时前 → 保留（仍在保护窗内）", !plan.Any(f => f.Name == freshish.Name));
            Check("9 天前 → 待删", plan.Any(f => f.Name == old.Name));
        }
        finally { d2.Delete(true); }

        // ---------------- [3] 只认自己的文件名 ----------------
        Console.WriteLine("\n[3] 只删 engine-YYYYMMDD-HHMMSS.log，别人的东西不碰");
        var d3 = FreshDir("pattern");
        try
        {
            var mine = Make(d3, "engine-20261001-100000.log", Now.AddDays(-18));
            var userFile = Make(d3, "notes.log", Now.AddDays(-30));
            var legacy = Make(d3, "deepseek.log", Now.AddDays(-30));
            var legacyRot = Make(d3, "deepseek.log.1", Now.AddDays(-30));
            var badStamp = Make(d3, "engine-notadate.log", Now.AddDays(-30));
            var plan = LogRetention.Plan(d3, Now);
            Check("符合命名 → 待删", plan.Any(f => f.Name == mine.Name));
            Check("notes.log → 不碰", !plan.Any(f => f.Name == userFile.Name));
            Check("deepseek.log（旧版引擎）→ 不碰", !plan.Any(f => f.Name == legacy.Name));
            Check("deepseek.log.1 → 不碰", !plan.Any(f => f.Name == legacyRot.Name));
            Check("engine-notadate.log → 不碰（时间戳不合规）",
                !plan.Any(f => f.Name == badStamp.Name));
        }
        finally { d3.Delete(true); }

        // ---------------- [4] 保留天数 ----------------
        Console.WriteLine("\n[4] 保留天数规则");
        var d4 = FreshDir("days");
        try
        {
            Make(d4, "engine-20261001-100000.log", Now.AddDays(-18));
            Make(d4, "engine-20261016-100000.log", Now.AddDays(-3));
            var plan = LogRetention.Plan(d4, Now, keepDays: 7);
            Check("7 天规则：18 天前删、3 天前留", plan.Count == 1, $"{plan.Count} 个");
            var plan2 = LogRetention.Plan(d4, Now, keepDays: 30);
            Check("改成 30 天 → 一个都不删", plan2.Count == 0, $"{plan2.Count} 个");
        }
        finally { d4.Delete(true); }

        // ---------------- [5] 总量上限 ----------------
        Console.WriteLine("\n[5] 总量上限 → 从最老的开始删，直到不超");
        var d5 = FreshDir("size");
        try
        {
            // 5 个文件 × 1000 字节，都在 7 天内但超过 24 小时（否则被窗口保护）
            var files = new List<FileInfo>();
            for (var i = 0; i < 5; i++)
                files.Add(Make(d5, $"engine-2026101{5 + i}-100000.log",
                    Now.AddDays(-2).AddHours(i), bytes: 1000));
            // 上限 3000 字节 → 应删最老的 2 个
            var plan = LogRetention.Plan(d5, Now, keepDays: 7, maxTotalBytes: 3000);
            Check("超限时删最老的", plan.Count == 2, $"{plan.Count} 个");
            Check("删的确实是最老的两个",
                plan.All(f => f.LastWriteTimeUtc <= Now.AddDays(-2).AddHours(1)));
        }
        finally { d5.Delete(true); }

        // ---------------- [6] incident 快照永不删 ----------------
        Console.WriteLine("\n[6] incident-* 快照永不删（封禁现场只有一次）");
        var d6 = FreshDir("incident");
        try
        {
            var inc = d6.CreateSubdirectory("incident-20261017-102124-suspended");
            Make(inc, "engine-20261017-101901.log", Now.AddDays(-40));
            Make(inc, "sends-20261017.jsonl", Now.AddDays(-40));
            var topOld = Make(d6, "engine-20261001-100000.log", Now.AddDays(-18));
            var plan = LogRetention.Plan(d6, Now);
            Check("快照里的 engine 日志 → 不删",
                !plan.Any(f => f.DirectoryName == inc.FullName));
            Check("顶层超龄日志照常删", plan.Any(f => f.Name == topOld.Name));
        }
        finally { d6.Delete(true); }

        // ---------------- [7] 当前正在写的文件 ----------------
        Console.WriteLine("\n[7] 当前正在写的文件永不删");
        var d7 = FreshDir("protect");
        try
        {
            var cur = Make(d7, "engine-20261001-100000.log", Now.AddDays(-18));
            var plan = LogRetention.Plan(d7, Now, protectPath: cur.FullName);
            Check("protectPath 命中 → 不删", !plan.Any(f => f.Name == cur.Name));
            var plan2 = LogRetention.Plan(d7, Now);
            Check("不传 protectPath → 照常待删", plan2.Any(f => f.Name == cur.Name));
        }
        finally { d7.Delete(true); }

        // ---------------- [8] 审计流水单独的长保留期 ----------------
        Console.WriteLine("\n[8] 审计流水保留 90 天（比引擎日志长）");
        var d8 = FreshDir("audit");
        try
        {
            var a30 = Make(d8, "sends-20260919.jsonl", Now.AddDays(-30));
            var a100 = Make(d8, "sends-20260711.jsonl", Now.AddDays(-100));
            var e8 = Make(d8, "engine-20261001-100000.log", Now.AddDays(-18));
            var plan = LogRetention.Plan(d8, Now);
            Check("30 天前的审计 → 保留", !plan.Any(f => f.Name == a30.Name));
            Check("100 天前的审计 → 删", plan.Any(f => f.Name == a100.Name));
            Check("引擎日志仍按 7 天规则", plan.Any(f => f.Name == e8.Name));
            // 审计不参与总量：把上限压到 0，审计仍按天数走
            var plan2 = LogRetention.Plan(d8, Now, maxTotalBytes: 0);
            Check("总量上限不波及审计文件",
                !plan2.Any(f => f.Name == a30.Name));
        }
        finally { d8.Delete(true); }

        // ---------------- [9] Apply 真删并返回统计 ----------------
        Console.WriteLine("\n[9] Apply 真删、返回个数与字节");
        var d9 = FreshDir("apply");
        try
        {
            Make(d9, "engine-20261001-100000.log", Now.AddDays(-18), bytes: 500);
            Make(d9, "engine-20261002-100000.log", Now.AddDays(-17), bytes: 700);
            var keep = Make(d9, "engine-20261018-100000.log", Now.AddHours(-2), bytes: 300);
            var plan = LogRetention.Plan(d9, Now);
            var (deleted, freed) = LogRetention.Apply(plan);
            Check("删掉 2 个", deleted == 2, $"{deleted}");
            Check("释放 1200 字节", freed == 1200, $"{freed}");
            Check("受保护的文件仍在", keep.Exists);
            Check("被删的文件确实没了",
                !File.Exists(Path.Combine(d9.FullName, "engine-20261001-100000.log")));
        }
        finally { d9.Delete(true); }

        // ---------------- [10] Apply 遇到占用不炸 ----------------
        Console.WriteLine("\n[10] Apply 遇到删不掉的文件不抛异常");
        var d10 = FreshDir("locked");
        try
        {
            var locked = Make(d10, "engine-20261001-100000.log", Now.AddDays(-18));
            using (var hold = new FileStream(locked.FullName, FileMode.Open,
                       FileAccess.Read, FileShare.None))
            {
                var (deleted, freed) = LogRetention.Apply(new[] { locked });
                Check("占用中的文件删不掉但不抛异常", deleted == 0, $"{deleted}");
                Check("释放字节为 0", freed == 0, $"{freed}");
            }
        }
        finally { d10.Delete(true); }

        // ---------------- [11] 文件名模式 ----------------
        Console.WriteLine("\n[11] 文件名模式");
        Check("engine-20261019-120000.log 合法",
            LogRetention.MatchesEngineLogName("engine-20261019-120000.log"));
        Check("engine-2026-10-19.log 不合法",
            !LogRetention.MatchesEngineLogName("engine-2026-10-19.log"));
        Check("sends-20261019.jsonl 合法",
            LogRetention.MatchesAuditName("sends-20261019.jsonl"));
        Check("sends-2026-10-19.jsonl 不合法",
            !LogRetention.MatchesAuditName("sends-2026-10-19.jsonl"));
        Check("生成的日志名可被自己识别",
            LogRetention.MatchesEngineLogName(
                LogRetention.EngineLogName(new DateTime(2026, 10, 19, 12, 34, 56))));

        Console.WriteLine(new string('-', 60));
        Console.WriteLine($"通过 {_pass} · 失败 {Fail.Count}");
        if (Fail.Count > 0)
        {
            foreach (var f in Fail) Console.WriteLine($"  失败项：{f}");
            return 1;
        }
        Console.WriteLine("  [日志保留策略自测通过]");
        return 0;
    }
}
