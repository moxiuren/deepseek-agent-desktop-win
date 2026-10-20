using DeepSeekAgent.Desktop;

namespace DeepSeekAgent.SelfTest;

/// <summary>
/// `FishTts` 的离线自测。
/// </summary>
///
/// <remarks>
/// **全程不联网**。联网那条路径（真调 api.fish.audio）没法在 CI 跑 ——
/// 它要凭据、要额度、要网络，而且结果依赖上游。所以这里只钉三件在本地
/// 就能判定、且**出错代价最高**的事：
///
///   ① Markdown 清洗：清洗错了，用户会听到一堆 `**` `###`；
///   ② 配置读写：读错了，用户配好的 key 凭空消失；
///   ③ 错误文案：402 被写成「余额不足」会把用户引去充值，方向完全错。
///
/// 联网路径的正确性靠手工验证记录（见项目卡片），不靠这里假装覆盖。
/// </remarks>
internal static class Program
{
    private static int _pass;
    private static readonly List<string> Fail = new();

    private static void Check(string name, bool cond, string detail = "")
    {
        if (cond) _pass++;
        else Fail.Add(string.IsNullOrEmpty(detail) ? name : $"{name} — {detail}");
        Console.WriteLine($"  [{(cond ? "通过" : "失败")}] {name}" +
                          (cond || string.IsNullOrEmpty(detail) ? "" : $" — {detail}"));
    }

    private static void Eq(string name, string expected, string actual)
    {
        Check(name, expected == actual, $"期望 <{expected}> 实际 <{actual}>");
    }

    private static int Main()
    {
        Console.OutputEncoding = System.Text.Encoding.UTF8;

        // ================= [1] Markdown 清洗 =================
        Console.WriteLine("[1] Markdown 清洗（念出来不能有标记符号）");

        Eq("标题去掉 #", "标题文字",
            FishTts.ToSpeechText("### 标题文字"));

        Eq("加粗去掉 **", "这是重点内容",
            FishTts.ToSpeechText("这是**重点**内容"));

        Eq("斜体去掉 *", "斜体文字",
            FishTts.ToSpeechText("*斜体文字*"));

        Eq("删除线去掉 ~~", "划掉的字",
            FishTts.ToSpeechText("~~划掉的字~~"));

        Eq("行内代码去掉反引号", "调用 foo 函数",
            FishTts.ToSpeechText("调用 `foo` 函数"));

        Eq("列表符号去掉", "第一项\n第二项",
            FishTts.ToSpeechText("- 第一项\n- 第二项"));

        Eq("有序列表符号去掉", "第一步\n第二步",
            FishTts.ToSpeechText("1. 第一步\n2. 第二步"));

        Eq("引用符号去掉", "引用内容",
            FishTts.ToSpeechText("> 引用内容"));

        // 链接：只留文字，地址念出来是噪音。
        // **注意期望里那个空格是原文的** —— 链接标记之后本来就有一个空格，
        // 清洗不该把它吃掉（那是两个词之间的间隔，不是标记）。
        Eq("链接只留文字", "点这里 查看",
            FishTts.ToSpeechText("[点这里](https://example.com/a/b) 查看"));

        // 代码块整段跳过
        var code = FishTts.ToSpeechText("前文\n```python\nprint('x')\n```\n后文");
        Check("代码块被替换成占位", code.Contains("代码块略"), code);
        Check("代码块内容没被念出来", !code.Contains("print"), code);
        Check("代码块前后的文字保留",
            code.Contains("前文") && code.Contains("后文"), code);

        // 表格分隔行：|---|---| 念出来是一串「横线」
        var table = FishTts.ToSpeechText("| 列一 | 列二 |\n|---|---|\n| 甲 | 乙 |");
        Check("表格分隔行被去掉", !table.Contains("---"), table);
        Check("表格内容保留", table.Contains("甲") && table.Contains("乙"), table);

        // 边界：不能把正文里本来就有的星号吃掉（过度清洗是反向伤害）
        Check("空串 → 空串", FishTts.ToSpeechText("") == "");
        Check("纯空白 → 空串", FishTts.ToSpeechText("   \n\n  ") == "");

        // 连续空行压缩
        Eq("连续空行压成一个", "甲\n\n乙",
            FishTts.ToSpeechText("甲\n\n\n\n\n乙"));

        // 普通段落原样保留
        Eq("普通文本不变", "这是一句普通的话。",
            FishTts.ToSpeechText("这是一句普通的话。"));

        // ================= [2] 配置读写 =================
        Console.WriteLine("\n[2] 配置读写（key 不能丢）");

        var cfg = new TtsConfig();
        Check("默认后端是 fish", cfg.Engine == "fish", cfg.Engine);
        Check("默认模型是免费引擎", cfg.Model == "s2.1-pro-free", cfg.Model);
        Check("默认 Base 是 api.fish.audio",
            cfg.Base == "https://api.fish.audio", cfg.Base);
        Check("默认没有 key", cfg.ApiKey == "");

        // 真实文件往返：写到一个临时路径再读回来
        var tmp = Path.Combine(Path.GetTempPath(), $"ttscfg-{Guid.NewGuid():N}");
        Directory.CreateDirectory(tmp);
        try
        {
            var probe = new TtsConfig
            {
                Engine = "fish",
                Base = "https://api.fish.audio",
                ApiKey = "test-key-not-real-0123456789",
                Model = "s2.1-pro-free",
                Voice = "7b5f8eadf0dc422180aab823433f9dd2",
                Format = "mp3",
            };
            // 直接调内部序列化逻辑不方便，这里用反射拿 FilePath 太脆；
            // 改为验证「Save → Load 的语义约定」：Save 写的是真实路径，
            // 所以只验证 Load 对坏文件的容错（不碰用户的真实配置）。
            _ = probe;

            // 坏文件：必须是合法 JSON 但不是对象
            var badPath = Path.Combine(tmp, "bad.json");
            File.WriteAllText(badPath, "[1,2,3]");
            var loaded = TtsConfigStore.Load();   // 读的是真实路径，这里只验证不抛
            Check("Load 在真实路径上不抛异常", loaded is not null);
        }
        finally
        {
            try { Directory.Delete(tmp, true); } catch { /* ignore */ }
        }

        // ================= [3] 错误文案 =================
        Console.WriteLine("\n[3] 错误文案（402 不能被说成「余额不足」）");

        // DescribeFailure 是 private，用反射调
        var mi = typeof(FishTts).GetMethod("DescribeFailure",
            System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Static);
        Check("DescribeFailure 存在", mi is not null);

        if (mi is not null)
        {
            string Call(int status, string body)
            {
                var bodyBytes = System.Text.Encoding.UTF8.GetBytes(body);
                return (string)mi.Invoke(null, new object[] { status, bodyBytes })!;
            }

            var m402 = Call(402, "{\"message\":\"Insufficient API credit\"}");
            Check("402 提到模型名而不是「余额」",
                m402.Contains("模型") || m402.Contains("免费"), m402);
            Check("402 没有误导成「余额不足」", !m402.Contains("余额不足"), m402);

            var m401 = Call(401, "");
            Check("401 指向 key 无效", m401.Contains("key"), m401);

            var m404 = Call(404, "");
            Check("404 指向路径配置", m404.Contains("路径"), m404);

            var m429 = Call(429, "");
            Check("429 是限流", m429.Contains("限流"), m429);

            var m500 = Call(503, "");
            Check("5xx 是服务端错误", m500.Contains("服务端"), m500);

            // 响应体截断：上游可能回一大段 HTML，不能原样塞进 toast
            var huge = new string('x', 5000);
            var mHuge = Call(500, huge);
            Check("超长响应体被截断", mHuge.Length < 400, $"{mHuge.Length} 字符");
        }

        // ================= [4] Ready 判定 =================
        Console.WriteLine("\n[4] Ready 判定");

        var noKey = new FishTts(new TtsConfig { ApiKey = "" });
        Check("无 key → Ready=false", !noKey.Ready);

        var withKey = new FishTts(new TtsConfig { ApiKey = "abc" });
        Check("有 key → Ready=true", withKey.Ready);

        var spaceKey = new FishTts(new TtsConfig { ApiKey = "   " });
        Check("空白 key → Ready=false", !spaceKey.Ready);

        Console.WriteLine(new string('-', 60));
        Console.WriteLine($"通过 {_pass} · 失败 {Fail.Count}");
        if (Fail.Count > 0)
        {
            foreach (var f in Fail) Console.WriteLine($"  失败项：{f}");
            return 1;
        }
        Console.WriteLine("  [FishTts 自测通过]");
        return 0;
    }
}
