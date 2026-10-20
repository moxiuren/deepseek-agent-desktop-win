using System.Text.Json;
using DeepSeekAgent.Desktop;

namespace DeepSeekAgent.SelfTest;

/// <summary>
/// `StatusLogThrottle` 离线自测 —— 不发请求、不启引擎。
/// </summary>
///
/// <remarks>
/// 钉死的核心命题：**倒计时变化不该让日志落盘，语义状态变化必须落盘**。
/// 这是 165 MB 日志的真正成因 —— 快照里 `next_ok_ms` / `age_sec` 每 5 秒都变，
/// 若用「整段 JSON 相等」判重，一行都折叠不掉。
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

    /// <summary>造一个 status 响应行。可变字段与不变字段分开传，便于测「只改倒计时」。</summary>
    private static string StatusLine(
        string level = "closed",
        string reason = "正常",
        int busyStreak = 0,
        int turns = 3,
        string cid = "cid-1",
        long nextOkMs = 0,
        double ageSec = 1.0,
        long remainingMs = 0,
        int hourSends = 0)
    {
        var payload = new
        {
            id = 7,
            result = new
            {
                started = true,
                model = "deepseek-chat",
                circuit = new
                {
                    level,
                    reason,
                    remaining_ms = remainingMs,
                    degrade = "none",
                    last_signal = "",
                    last_signal_at = "",
                    counters = new
                    {
                        auth_failures = 0,
                        busy_streak = busyStreak,
                        rpc_errors = 0,
                        biz_errors = 0,
                        muted_hits = 0,
                    },
                },
                decision = new { allowed = true, layer = "—", reason = "放行", wait_ms = 0 },
                pacing = new
                {
                    burst_count = 1,
                    max_burst = 3,
                    hour_sends = hourSends,
                    hourly_max = 10,
                    hour_window_remaining_ms = 3_000_000,
                    next_ok_ms = nextOkMs,
                    in_active_window = true,
                },
                session = new { cid, turns, age_sec = ageSec, temporary = false, max_turns = 40 },
                transport = new
                {
                    name = "deepseek-rpc",
                    ready = true,
                    background = new { note = "", account_suspended = false },
                },
            },
        };
        return JsonSerializer.Serialize(payload);
    }

    private static int Main()
    {
        Console.OutputEncoding = System.Text.Encoding.UTF8;
        var t0 = new DateTime(2026, 10, 19, 12, 0, 0, DateTimeKind.Utc);

        // ---------------- [1] 非 status 行一律放过 ----------------
        Console.WriteLine("[1] 非 status 行一律原样落盘");
        var th = new StatusLogThrottle();
        Check("普通事件行 → null（交给调用方原样落）",
            th.Offer("{\"type\":\"event\",\"event\":\"delta\",\"data\":{}}", t0) is null);
        Check("非 JSON 行 → null",
            th.Offer("Traceback (most recent call last):", t0) is null);
        Check("空行 → null", th.Offer("", t0) is null);
        Check("有 result 但不是 status（缺 circuit）→ null",
            th.Offer("{\"id\":1,\"result\":{\"text\":\"hi\"}}", t0) is null);
        Check("有 circuit 但缺 pacing → null",
            th.Offer("{\"id\":1,\"result\":{\"circuit\":{}}}", t0) is null);

        // ---------------- [2] 首次必落盘 ----------------
        Console.WriteLine("\n[2] 首次 status 必落盘");
        th = new StatusLogThrottle();
        var first = StatusLine();
        var r1 = th.Offer(first, t0);
        Check("首行落盘（返回原文）", r1 == first);

        // ---------------- [3] 只有倒计时变化 → 折叠 ----------------
        Console.WriteLine("\n[3] 只有倒计时变化 → 折叠（这是 165MB 的成因）");
        var r2 = th.Offer(StatusLine(nextOkMs: 41_234, ageSec: 6.0, remainingMs: 999), t0.AddSeconds(5));
        Check("倒计时变化但语义不变 → 折叠（返回空串）", r2 == string.Empty);
        var r3 = th.Offer(StatusLine(nextOkMs: 88_000, ageSec: 11.0), t0.AddSeconds(10));
        Check("连续第二次仍折叠", r3 == string.Empty);
        Check("折叠计数为 2", th.SkippedTotal == 2, $"实际 {th.SkippedTotal}");

        // ---------------- [4] 语义变化 → 立刻落盘 ----------------
        Console.WriteLine("\n[4] 语义变化 → 立刻落盘");
        th = new StatusLogThrottle();
        th.Offer(StatusLine(), t0);
        var openLine = StatusLine(level: "open", reason: "服务端限流（429）");
        var r4 = th.Offer(openLine, t0.AddSeconds(5));
        Check("熔断档位变化 → 落盘原文", r4 == openLine);
        Check("落盘后折叠计数清零", th.SkippedTotal == 0);

        th = new StatusLogThrottle();
        th.Offer(StatusLine(), t0);
        var busyLine = StatusLine(busyStreak: 2);
        Check("busy_streak 变化 → 落盘", th.Offer(busyLine, t0.AddSeconds(5)) == busyLine);

        th = new StatusLogThrottle();
        th.Offer(StatusLine(), t0);
        var turnLine = StatusLine(turns: 4);
        Check("会话轮次变化 → 落盘", th.Offer(turnLine, t0.AddSeconds(5)) == turnLine);

        th = new StatusLogThrottle();
        th.Offer(StatusLine(), t0);
        var cidLine = StatusLine(cid: "cid-2");
        Check("会话 cid 变化 → 落盘", th.Offer(cidLine, t0.AddSeconds(5)) == cidLine);

        th = new StatusLogThrottle();
        th.Offer(StatusLine(), t0);
        var hourLine = StatusLine(hourSends: 1);
        Check("小时预算消耗 → 落盘", th.Offer(hourLine, t0.AddSeconds(5)) == hourLine);

        // ---------------- [5] 心跳 ----------------
        Console.WriteLine("\n[5] 长时间无变化 → 心跳行（证明轮询还活着）");
        th = new StatusLogThrottle();
        th.Offer(StatusLine(), t0);
        th.Offer(StatusLine(), t0.AddSeconds(5));
        th.Offer(StatusLine(), t0.AddSeconds(10));
        var hb = th.Offer(StatusLine(), t0.AddSeconds(305));   // 超过 5 分钟心跳
        Check("超过心跳间隔 → 落一行", !string.IsNullOrEmpty(hb) && hb != string.Empty);
        Check("心跳行标注「未变化」", hb!.Contains("status 未变化", StringComparison.Ordinal));
        // 落盘那行之后的两条被折叠，心跳行报的是「自上次落盘以来」的条数。
        Check("心跳行报的是自上次落盘以来的折叠数",
            hb.Contains("\"skipped\":2", StringComparison.Ordinal), hb);
        Check("心跳行不含完整快照（很小）", hb.Length < 200, $"{hb.Length} 字节");
        // SkippedTotal 是**累计**，心跳不会把它清零 —— 它统计整个实例生命周期。
        Check("SkippedTotal 是累计值（心跳不清零）", th.SkippedTotal == 2, $"{th.SkippedTotal}");

        // 心跳行本身不改变指纹 —— 下一 tick 若仍无变化，应继续折叠
        var afterHb = th.Offer(StatusLine(), t0.AddSeconds(310));
        Check("心跳后下一 tick 继续折叠", afterHb == string.Empty);
        Check("心跳后累计值继续增长", th.SkippedTotal == 3, $"{th.SkippedTotal}");

        // ---------------- [6] 折叠率实测 ----------------
        Console.WriteLine("\n[6] 折叠率（模拟 1 小时 · 5 秒一 tick · 每 10 分钟一次状态变化）");
        th = new StatusLogThrottle();
        var baseTime = t0;
        int total = 0, logged = 0;
        for (var i = 0; i < 720; i++)   // 3600 / 5 = 720 tick
        {
            var now = baseTime.AddSeconds(i * 5);
            // 每 120 tick（10 分钟）变一次轮次，其余只有倒计时在变
            var line = StatusLine(
                turns: 3 + i / 120,
                nextOkMs: (i * 137) % 60_000,
                ageSec: i * 5.0);
            var res = th.Offer(line, now);
            total++;
            if (res is not null && res.Length > 0) logged++;
        }
        var ratio = 100.0 * (total - logged) / total;
        Console.WriteLine($"    720 tick 中实际落盘 {logged} 行（折叠 {ratio:F1}%）");
        Check("折叠率 > 95%", ratio > 95.0, $"{ratio:F1}%");
        Check("仍保留变化行与心跳（未全静默）", logged >= 6, $"{logged} 行");

        // ---------------- [7] 指纹本身 ----------------
        Console.WriteLine("\n[7] 指纹不包含倒计时");
        // `JsonElement` 在 .NET 8 没有字符串索引器，统一用 TryGetProperty。
        static JsonElement Result(string line)
        {
            using var doc = JsonDocument.Parse(line);
            return doc.RootElement.GetProperty("result").Clone();
        }

        var f1 = StatusLogThrottle.Fingerprint(Result(StatusLine(nextOkMs: 0, ageSec: 0, remainingMs: 0)));
        var f2 = StatusLogThrottle.Fingerprint(Result(StatusLine(nextOkMs: 99_999, ageSec: 999, remainingMs: 88_888)));
        var f3 = StatusLogThrottle.Fingerprint(Result(StatusLine(level: "halted")));
        Check("倒计时不同 → 指纹相同", f1 == f2);
        Check("档位不同 → 指纹不同", f1 != f3);

        // ---------------- [8] 脏数据不炸 ----------------
        Console.WriteLine("\n[8] 脏数据不炸");
        th = new StatusLogThrottle();
        Check("截断 JSON → null（当普通行处理）",
            th.Offer("{\"id\":1,\"result\":{\"circuit\":{\"pacing\":", t0) is null);
        Check("result 是数组 → null",
            th.Offer("{\"id\":1,\"result\":[{\"circuit\":1,\"pacing\":2}]}", t0) is null);
        Check("circuit 不是对象 → 仍能判（字段全空）",
            th.Offer("{\"id\":1,\"result\":{\"circuit\":5,\"pacing\":{}}}", t0) is not null);

        Console.WriteLine(new string('-', 60));
        Console.WriteLine($"通过 {_pass} · 失败 {Fail.Count}");
        if (Fail.Count > 0)
        {
            foreach (var f in Fail) Console.WriteLine($"  失败项：{f}");
            return 1;
        }
        Console.WriteLine("  [状态日志折叠自测通过]");
        return 0;
    }
}
