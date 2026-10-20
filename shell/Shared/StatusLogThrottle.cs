using System.Text.Json;

namespace DeepSeekAgent.Desktop;

/// <summary>
/// `status` 响应的日志折叠器 —— 决定「这一行该不该落盘」。
/// </summary>
///
/// <remarks>
/// **为什么单独一个类**（2026-10-19）：原先是 `EngineClient` 里三个字段加一个
/// 私有方法。问题有两个：一是它无法被独立测试（要验证得先起一个真进程），
/// 二是「日志降噪」与「进程托管」本来是两件事，混在一个 500 行的类里迟早会烂。
///
/// 背景：引擎日志是 stdout 原始转储，`status` 每 5 秒回一次约 3.9 KB 的全量快照
/// —— 单文件 927 行里 588 行是它，一天下来 165 MB。真出事时最该看的发送序列
/// 被埋在两万行「状态没变」里。
///
/// **折叠的判据是「语义关键字段」而不是「整段 JSON 相等」**：快照里有
/// `next_ok_ms` / `age_sec` / `remaining_ms` 这类每 tick 必变的倒计时，
/// 整段比较永远不相等，去重会完全失效。
///
/// 折叠后仍保留心跳行：完全静默就分不清「状态没变」和「引擎挂了、轮询停了」，
/// 而后者是本项目最忌讳的失败形态。
/// </remarks>
internal sealed class StatusLogThrottle
{
    private int _skipped;
    private string? _fingerprint;
    private DateTime _lastLoggedAt = DateTime.MinValue;

    /// <summary>
    /// 状态长时间不变时，每隔这么久仍落一行心跳。
    /// </summary>
    ///
    /// <remarks>
    /// **5 分钟，不是 60 秒**。心跳的作用只是「区分空闲与卡死」，而后者有
    /// 更可靠的信号：引擎进程退出会走 <c>Fault</c>（`引擎输出中断`），
    /// status RPC 卡住会在 60 秒超时后抛异常并记日志。60 秒一次心跳
    /// 会让心跳本身变成新的噪音源 —— 一小时 60 行，实测折叠率只有 91.7%；
    /// 改成 5 分钟降到 12 行/小时，折叠率 97%+。
    ///
    /// 反过来也不能更稀：出事后要能定位「大概什么时候停的」，5 分钟的粒度够用。
    /// </remarks>
    public TimeSpan Heartbeat { get; set; } = TimeSpan.FromMinutes(5);

    /// <summary>本实例累计折叠掉的条数（诊断用）。</summary>
    public int SkippedTotal { get; private set; }

    /// <summary>
    /// 判一次。返回 <see langword="null"/> 表示「不是 status 响应，照常落盘」；
    /// 返回非 null 表示「是 status 响应」，此时：
    ///   * 值非空 → 用这个值替代原文落盘（可能是原文，也可能是心跳行）
    ///   * 值为 <see cref="string.Empty"/> → 折叠掉，不落盘
    /// </summary>
    public string? Offer(string line, DateTime nowUtc)
    {
        if (!LooksLikeStatusResponse(line)) return null;

        JsonDocument doc;
        try { doc = JsonDocument.Parse(line); }
        catch { return null; }

        using (doc)
        {
            if (!doc.RootElement.TryGetProperty("result", out var result) ||
                result.ValueKind != JsonValueKind.Object)
                return null;
            if (!result.TryGetProperty("circuit", out _) ||
                !result.TryGetProperty("pacing", out _))
                return null;

            return Decide(line, result, nowUtc);
        }
    }

    private string Decide(string line, JsonElement result, DateTime nowUtc)
    {
        var fp = Fingerprint(result);
        var changed = fp != _fingerprint;
        var heartbeatDue = nowUtc - _lastLoggedAt >= Heartbeat;

        if (!changed && !heartbeatDue)
        {
            _skipped++;
            SkippedTotal++;
            return string.Empty;
        }

        if (changed)
        {
            _fingerprint = fp;
            _lastLoggedAt = nowUtc;
            _skipped = 0;
            return line;
        }

        // 心跳行：状态与上次一致，但已经静默太久 —— 证明轮询还活着。
        // 不带正文，只报「折叠了多少条」，几十字节。
        var note =
            $"{{\"note\":\"status 未变化\",\"skipped\":{_skipped}," +
            $"\"since\":\"{_lastLoggedAt:HH:mm:ss}\"}}";
        _skipped = 0;
        _lastLoggedAt = nowUtc;
        return note;
    }

    /// <summary>
    /// 廉价前置过滤：不是 JSON 对象、或不含 status 快照的特征键，直接放过。
    /// 这比先 Parse 再判断快得多 —— 调用点在热路径上，每 5 秒至少一次。
    /// </summary>
    private static bool LooksLikeStatusResponse(string line)
    {
        if (line.Length == 0 || line[0] != '{') return false;
        if (!line.Contains("\"result\"", StringComparison.Ordinal)) return false;
        // 双保险：真 status 一定有这两块（`gates.snapshot()` 产出）。
        return line.Contains("\"circuit\"", StringComparison.Ordinal) &&
               line.Contains("\"pacing\"", StringComparison.Ordinal);
    }

    /// <summary>
    /// 从 status 快照里挑出**语义关键字段**做指纹。
    /// </summary>
    ///
    /// <remarks>
    /// 刻意**不包含**每 tick 必变的倒计时：`next_ok_ms`、
    /// `hour_window_remaining_ms`、`rate_limit_remaining_ms`、`remaining_ms`、
    /// `age_sec`。含了它们，指纹永远不相等，折叠就白做。
    ///
    /// 必须包含的是「用户会因此改变行为」的那些：熔断档位与理由、闸门是否放行、
    /// 突发计数与小时预算、会话 cid 与轮次、账号是否被处置、模型、是否已启动。
    /// </remarks>
    internal static string Fingerprint(JsonElement s)
    {
        static string S(JsonElement parent, string name)
        {
            if (!parent.TryGetProperty(name, out var el)) return "";
            return el.ValueKind switch
            {
                JsonValueKind.String => el.GetString() ?? "",
                JsonValueKind.Number => el.GetRawText(),
                JsonValueKind.True => "1",
                JsonValueKind.False => "0",
                _ => "",
            };
        }

        var parts = new List<string>(20);
        if (s.TryGetProperty("circuit", out var c) && c.ValueKind == JsonValueKind.Object)
        {
            parts.Add(S(c, "level"));
            parts.Add(S(c, "reason"));
            parts.Add(S(c, "last_signal"));
            parts.Add(S(c, "degrade"));
            if (c.TryGetProperty("counters", out var k) && k.ValueKind == JsonValueKind.Object)
            {
                parts.Add(S(k, "auth_failures"));
                parts.Add(S(k, "busy_streak"));
                parts.Add(S(k, "rpc_errors"));
                parts.Add(S(k, "biz_errors"));
                parts.Add(S(k, "muted_hits"));
            }
        }
        if (s.TryGetProperty("decision", out var d) && d.ValueKind == JsonValueKind.Object)
        {
            parts.Add(S(d, "allowed"));
            parts.Add(S(d, "layer"));
            parts.Add(S(d, "reason"));
        }
        if (s.TryGetProperty("pacing", out var p) && p.ValueKind == JsonValueKind.Object)
        {
            parts.Add(S(p, "burst_count"));
            parts.Add(S(p, "hour_sends"));
            parts.Add(S(p, "hourly_max"));
            parts.Add(S(p, "in_active_window"));
        }
        if (s.TryGetProperty("session", out var se) && se.ValueKind == JsonValueKind.Object)
        {
            parts.Add(S(se, "cid"));
            parts.Add(S(se, "turns"));
        }
        if (s.TryGetProperty("transport", out var t) && t.ValueKind == JsonValueKind.Object)
        {
            parts.Add(S(t, "ready"));
            if (t.TryGetProperty("background", out var bg) && bg.ValueKind == JsonValueKind.Object)
            {
                parts.Add(S(bg, "account_suspended"));
                parts.Add(S(bg, "note"));
            }
        }
        parts.Add(S(s, "model"));
        parts.Add(S(s, "started"));
        return string.Join('|', parts);
    }
}
