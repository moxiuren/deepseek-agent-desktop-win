using System.Globalization;
using System.IO;
using System.Text.RegularExpressions;

namespace DeepSeekAgent.Desktop;

/// <summary>
/// 引擎日志的保留策略 —— 决定「哪些旧文件可以删」。
/// </summary>
///
/// <remarks>
/// **为什么需要它**（2026-10-19）：日志目录实测 197 个文件 / 181 MB，
/// 且 `engine-*.log` 每次启动新建一个、**从不清理**。这不是「磁盘快满了」
/// 的问题，而是「真出事时找不到现场」的问题 —— 189 个文件里，
/// 封禁那天的那个要靠时间戳手工翻。
///
/// **拆成 <see cref="Plan"/> + <see cref="Apply"/> 两段**：<see cref="Plan"/>
/// 是纯函数（只看元数据，不碰文件内容、不删任何东西），所以能在测试里
/// 用临时目录精确断言「该删谁、该留谁」；<see cref="Apply"/> 才真删。
/// 混在一起写的话，测一次就得先造一堆真文件。
///
/// **删的顺序永远是「最老的先删」**，且有三类硬保护（见 <see cref="Plan"/>）。
/// </remarks>
internal static class LogRetention
{
    /// <summary>引擎日志按天数保留的下限。</summary>
    public const int DefaultKeepDays = 7;

    /// <summary>引擎日志的总量上限（超过后从最老的开始删）。</summary>
    public const long DefaultMaxTotalBytes = 200L * 1024 * 1024;

    /// <summary>审计流水（`sends-*.jsonl`）保留天数 —— 比引擎日志长得多。</summary>
    ///
    /// <remarks>
    /// **审计是「事后分析」的唯一依据，不能和调试日志同等对待。**
    /// 引擎日志 7 天够用（查最近的异常）；但「三个月前那次限流是什么模式」
    /// 只有审计流水答得上来，而它按天分文件、单日体量很小（几十条记录），
    /// 留 90 天也就几百 KB。
    /// </remarks>
    public const int DefaultKeepAuditDays = 90;

    /// <summary>最近这么久以内的日志一律不删，无论总量是否超限。</summary>
    ///
    /// <remarks>
    /// **这是防「崩溃循环抹掉现场」的安全阀。** 设想引擎启动即崩、壳不停重试：
    /// 一小时内能产生上百个小日志，按总量删会从最老的开始删 ——
    /// 而那时「最老的」正是几十分钟前刚开始出问题的那一批。
    ///
    /// 代价是极端情况下总量可能短暂超过上限。这是**有意的失败方向**：
    /// 宁可多占几十 MB，也不能删掉正在发生的故障现场。
    /// </remarks>
    public static readonly TimeSpan MinKeepWindow = TimeSpan.FromHours(24);

    /// <summary>`engine-YYYYMMDD-HHMMSS.log` —— 只认自己写的文件名。</summary>
    private static readonly Regex EngineLogPattern =
        new(@"^engine-\d{8}-\d{6}\.log$", RegexOptions.IgnoreCase | RegexOptions.Compiled);

    /// <summary>`sends-YYYYMMDD.jsonl` —— 审计流水。</summary>
    private static readonly Regex AuditPattern =
        new(@"^sends-\d{8}\.jsonl$", RegexOptions.IgnoreCase | RegexOptions.Compiled);

    /// <summary>事故快照目录前缀 —— **永不清理**，见 <see cref="Plan"/> 的说明。</summary>
    public const string IncidentPrefix = "incident-";

    /// <summary>
    /// 算出该删哪些文件。**纯函数：不删任何东西，也不读文件内容。**
    /// </summary>
    ///
    /// <param name="dir">日志目录（不存在时返回空列表）。</param>
    /// <param name="nowUtc">当前 UTC 时间（显式传入，测试才能造时间）。</param>
    /// <param name="protectPath">当前正在写的文件，永不删。</param>
    ///
    /// <remarks>
    /// 三类硬保护，任何一条命中就跳过：
    ///   1. **`incident-*` 目录里的文件** —— 那是封禁现场快照，
    ///      用户明确要求「过后被封了能查回原因」，删了就没有第二次机会。
    ///   2. **当前正在写的文件**（<paramref name="protectPath"/>）——
    ///      删了句柄悬空，后续写入静默丢失。
    ///   3. **最近 <see cref="MinKeepWindow"/> 内的文件** —— 见该字段的说明。
    ///
    /// 时间判据用 `LastWriteTimeUtc` 而不是文件名里的日期：
    /// 文件名是**创建**时间，而一个日志可能被写了很久；`LastWriteTime`
    /// 反映「最后一条记录是什么时候」，这才是判断「还有没有参考价值」的依据。
    /// </remarks>
    public static List<FileInfo> Plan(
        DirectoryInfo dir,
        DateTime nowUtc,
        int keepDays = DefaultKeepDays,
        long maxTotalBytes = DefaultMaxTotalBytes,
        int keepAuditDays = DefaultKeepAuditDays,
        string? protectPath = null)
    {
        var doomed = new List<FileInfo>();
        if (!dir.Exists) return doomed;

        var protect = string.IsNullOrWhiteSpace(protectPath)
            ? null
            : Path.GetFullPath(protectPath);

        // ---------- 引擎日志 ----------
        //
        // 只认 `engine-*.log` 这个模式。目录里还可能有用户自己放的文件、
        // 旧版 Python 引擎的 `deepseek.log*`、以及 `.vault-trash` 之类 ——
        // **一律不碰**。清理工具越权删别人的东西，是比日志膨胀更严重的事故。
        var engineLogs = dir.GetFiles("engine-*.log")
            .Where(f => EngineLogPattern.IsMatch(f.Name))
            .Where(f => !IsProtected(f, protect, nowUtc))
            .OrderBy(f => f.LastWriteTimeUtc)
            .ToList();

        // 规则 A：超过保留天数。
        var cutoff = nowUtc.AddDays(-keepDays);
        foreach (var f in engineLogs.Where(f => f.LastWriteTimeUtc < cutoff))
            doomed.Add(f);

        // 规则 B：剩下的总量仍超限 → 从最老的继续删，直到不超。
        long total = engineLogs.Except(doomed).Sum(f => f.Length);
        foreach (var f in engineLogs.Except(doomed))
        {
            if (total <= maxTotalBytes) break;
            doomed.Add(f);
            total -= f.Length;
        }

        // ---------- 审计流水 ----------
        //
        // 只按天数，不参与总量计算 —— 它体量极小，且是长期证据。
        var auditCutoff = nowUtc.AddDays(-keepAuditDays);
        foreach (var f in dir.GetFiles("sends-*.jsonl"))
        {
            if (!AuditPattern.IsMatch(f.Name)) continue;
            if (IsProtected(f, protect, nowUtc)) continue;
            if (f.LastWriteTimeUtc < auditCutoff) doomed.Add(f);
        }

        return doomed;
    }

    /// <summary>
    /// 执行清理。返回删掉的个数与释放的字节数。**任何异常都吞掉** ——
    /// 清理失败绝不能让引擎起不来。
    /// </summary>
    public static (int Deleted, long FreedBytes) Apply(IEnumerable<FileInfo> doomed)
    {
        int count = 0;
        long freed = 0;
        foreach (var f in doomed)
        {
            try
            {
                var size = f.Length;
                f.Delete();
                count++;
                freed += size;
            }
            catch
            {
                // 文件被别的进程占着 / 权限不足 —— 跳过，下次启动再试。
            }
        }
        return (count, freed);
    }

    private static bool IsProtected(FileInfo f, string? protectPath, DateTime nowUtc)
    {
        // 1. 当前正在写的文件
        if (protectPath is not null &&
            string.Equals(f.FullName, protectPath, StringComparison.OrdinalIgnoreCase))
            return true;

        // 2. 事故快照目录里的任何东西
        if (IsUnderIncidentDir(f)) return true;

        // 3. 最近 24 小时
        return nowUtc - f.LastWriteTimeUtc < MinKeepWindow;
    }

    /// <summary>文件是否位于某个 `incident-*` 目录下（只看最近两层，够用且便宜）。</summary>
    private static bool IsUnderIncidentDir(FileInfo f)
    {
        var parent = f.Directory;
        for (var depth = 0; depth < 2 && parent is not null; depth++)
        {
            if (parent.Name.StartsWith(IncidentPrefix, StringComparison.OrdinalIgnoreCase))
                return true;
            parent = parent.Parent;
        }
        return false;
    }

    /// <summary>把字节数渲染成人读的字符串（日志用）。</summary>
    public static string Human(long bytes) =>
        bytes < 1024 ? $"{bytes} B"
        : bytes < 1024 * 1024 ? $"{bytes / 1024.0:F1} KB"
        : $"{bytes / (1024.0 * 1024.0):F1} MB";

    /// <summary>仅测试用：从文件名解析时间戳（验证正则是否真的匹配）。</summary>
    internal static bool MatchesEngineLogName(string name) => EngineLogPattern.IsMatch(name);

    /// <summary>仅测试用：验证审计文件名模式。</summary>
    internal static bool MatchesAuditName(string name) => AuditPattern.IsMatch(name);

    /// <summary>仅测试用：格式化一个符合命名规则的文件名。</summary>
    internal static string EngineLogName(DateTime local) =>
        $"engine-{local.ToString("yyyyMMdd-HHmmss", CultureInfo.InvariantCulture)}.log";
}
