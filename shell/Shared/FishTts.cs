using System.IO;
using System.Net.Http;
using System.Text;
using System.Text.Json;

// **`Path` 在这个文件里必须完全限定成 `System.IO.Path`。**
// WPF 项目里 `System.Windows.Shapes.Path` 会通过隐式 using 进来，
// 于是 `System.IO.Path.Combine` 会被解析成图形类 —— 报的是
// 「'string' does not contain a definition for 'Combine'」，
// 离真正的原因（命名冲突）隔得很远。这是 WPF 项目的经典坑。

namespace DeepSeekAgent.Desktop;

/// <summary>
/// 朗读后端的配置。
///
/// **为什么要有 `Engine` 这个开关**：本项目原本只有 DeepSeek 网页版的服务端
/// 流式朗读（引擎签票 + 前端直连 WS）。Fish Audio 是另一条完全独立的路 ——
/// HTTP 一次性返回整段音频，没有 WS、没有 ack 流控。两者的音频管线不兼容，
/// 不能互相顶替，只能二选一。
///
/// 默认走 `fish`：它用独立的免费额度，**不消耗 DeepSeek 账号的朗读配额**，
/// 也就不会因为朗读触发风控。DeepSeek 那条路原样保留，随时可切回。
/// </summary>
internal sealed class TtsConfig
{
    /// <summary>`"fish"` 或 `"deepseek"`。认不出来的值一律回落到 `"fish"`。</summary>
    public string Engine { get; set; } = "fish";

    /// <summary>
    /// Fish 的 API 根。**注意它和 TTS 路径不在同一层**：
    /// TTS 是 `{Base}/v1/tts`，音色列表是 `{Base}/model`（**不带 `/v1`**）。
    /// 这是实测出来的 —— 给音色列表拼上 `/v1` 会 404。
    /// </summary>
    public string Base { get; set; } = "https://api.fish.audio";

    /// <summary>
    /// Fish API key。**明文落盘**，与项目里 `engine/.token` 的处理一致。
    ///
    /// 没上 DPAPI 是权衡后的决定：DPAPI 在 .NET 8 需要额外的
    /// `System.Security.Cryptography.ProtectedData` 包，而这里的威胁模型
    /// 与 `.token` 相同（本机用户目录、默认 ACL 只当前用户可读）。
    /// 若哪天要分发给他人使用，这里必须改成 DPAPI 或系统凭据管理器。
    ///
    /// **绝不写进日志、绝不经 Bridge 传给前端** —— 前端只需要音频字节。
    /// </summary>
    public string ApiKey { get; set; } = "";

    /// <summary>
    /// Fish 的模型名。**必须放 HTTP header**，放 body 会被忽略并按默认
    /// （付费）模型计费，直接返回 402。`s2.1-pro-free` 是免费引擎。
    /// </summary>
    public string Model { get; set; } = "s2.1-pro-free";

    /// <summary>
    /// 默认音色 id（Fish 的 `reference_id`，32 位十六进制）。空串 = 用 Fish
    /// 自己的默认音色。
    /// </summary>
    public string Voice { get; set; } = "";

    /// <summary>输出格式。mp3 体积小、WebAudio 原生可解，不需要额外解码器。</summary>
    public string Format { get; set; } = "mp3";
}

/// <summary>
/// 朗读配置的本地存储。
///
/// 与 <see cref="UiPrefsStore"/> 同一目录（`%LOCALAPPDATA%\DeepSeek-Agent`），
/// 但**分成两个文件**：UI 偏好每次拖分隔条都落盘，而这份配置里含 API key ——
/// 让它跟着高频写入的文件反复落盘没有意义，也放大了凭据意外暴露的面。
/// </summary>
internal static class TtsConfigStore
{
    private static readonly string FilePath = System.IO.Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
        "DeepSeek-Agent", "tts.json");

    public static string Path => FilePath;

    public static TtsConfig Load()
    {
        var cfg = new TtsConfig();
        try
        {
            if (!File.Exists(FilePath)) return cfg;
            using var doc = JsonDocument.Parse(File.ReadAllText(FilePath));
            var r = doc.RootElement;
            if (r.TryGetProperty("engine", out var e) && e.ValueKind == JsonValueKind.String)
            {
                // 白名单：认不出来的值回落，避免一个手改坏的文件让朗读彻底不可用。
                cfg.Engine = e.GetString() == "deepseek" ? "deepseek" : "fish";
            }
            if (r.TryGetProperty("base", out var b) && b.ValueKind == JsonValueKind.String
                && !string.IsNullOrWhiteSpace(b.GetString()))
                cfg.Base = b.GetString()!.Trim().TrimEnd('/');
            if (r.TryGetProperty("api_key", out var k) && k.ValueKind == JsonValueKind.String)
                cfg.ApiKey = k.GetString() ?? "";
            if (r.TryGetProperty("model", out var m) && m.ValueKind == JsonValueKind.String
                && !string.IsNullOrWhiteSpace(m.GetString()))
                cfg.Model = m.GetString()!.Trim();
            if (r.TryGetProperty("voice", out var v) && v.ValueKind == JsonValueKind.String)
                cfg.Voice = v.GetString() ?? "";
            if (r.TryGetProperty("format", out var f) && f.ValueKind == JsonValueKind.String
                && !string.IsNullOrWhiteSpace(f.GetString()))
                cfg.Format = f.GetString()!.Trim();
        }
        catch
        {
            // 读坏了就用默认值。朗读配置不值得为它弹窗 —— 但 key 会丢，
            // 表现为「点了没反应，提示未配置」，用户重新填一次即可。
            return new TtsConfig();
        }
        return cfg;
    }

    public static void Save(TtsConfig cfg)
    {
        try
        {
            var dir = System.IO.Path.GetDirectoryName(FilePath);
            if (!string.IsNullOrEmpty(dir)) Directory.CreateDirectory(dir);
            // 手写 JSON 而不是序列化对象：字段顺序稳定，且不会因为以后加了个
            // 新属性就把整个文件重排一遍（那种 diff 在排障时毫无信息量）。
            var json = new StringBuilder()
                .Append("{\n")
                .Append("  \"engine\": ").Append(JsonStr(cfg.Engine)).Append(",\n")
                .Append("  \"base\": ").Append(JsonStr(cfg.Base)).Append(",\n")
                .Append("  \"api_key\": ").Append(JsonStr(cfg.ApiKey)).Append(",\n")
                .Append("  \"model\": ").Append(JsonStr(cfg.Model)).Append(",\n")
                .Append("  \"voice\": ").Append(JsonStr(cfg.Voice)).Append(",\n")
                .Append("  \"format\": ").Append(JsonStr(cfg.Format)).Append("\n")
                .Append("}\n")
                .ToString();
            File.WriteAllText(FilePath, json, new UTF8Encoding(false));
        }
        catch
        {
            // 存不住就下次再说。
        }
    }

    private static string JsonStr(string s) =>
        "\"" + (s ?? "").Replace("\\", "\\\\").Replace("\"", "\\\"") + "\"";
}

/// <summary>一个 Fish 音色（`GET {Base}/model` 的条目，只取朗读要用的字段）。</summary>
internal sealed record FishVoice(
    string Id,
    string Title,
    string Description,
    string[] Tags);

/// <summary>
/// Fish Audio 的 HTTP 客户端 —— **绕过 fish-openai-bridge 直连上游**。
///
/// 为什么不经 bridge：那个服务做的是 OpenAI 兼容层翻译（`/v1/audio/speech`
/// → Fish `/v1/tts`），而本客户端本来就是自家代码，没有「假装是 OpenAI」的
/// 需求。多一个常驻进程、多一层翻译、多一个要同步的配置源，全是净成本。
///
/// 协议要点（都实测过，别凭记忆改）：
///   · `POST {Base}/v1/tts`，body 是 `{text, format, normalize, latency, reference_id}`
///   · **`model` 必须放 HTTP header**。放 body 会被忽略并按付费模型计费 → 402。
///   · 音色列表是 `GET {Base}/model?page_size=N`，**不带 `/v1`**。
/// </summary>
internal sealed class FishTts : IDisposable
{
    // 单例 HttpClient：每次 new 会耗尽本地端口（TIME_WAIT），
    // 而朗读是高频操作。60 秒超时 —— 长文本合成实测 1~3 秒，
    // 给到 60 秒是为了容忍大段文字 + 上游抖动。
    private static readonly HttpClient Http = new() { Timeout = TimeSpan.FromSeconds(60) };

    private readonly TtsConfig _cfg;

    public FishTts(TtsConfig cfg) => _cfg = cfg;

    /// <summary>配置是否可用（有 key 才谈得上朗读）。</summary>
    public bool Ready => !string.IsNullOrWhiteSpace(_cfg.ApiKey);

    /// <summary>
    /// 合成一段文本，返回音频字节与 MIME。
    ///
    /// **失败不抛** —— 返回 `error` 让界面 toast 出来，与
    /// <see cref="AgentHost.PrepareTtsAsync"/> 的约定一致：朗读失败不该让
    /// 整个会话面板崩掉。
    /// </summary>
    public async Task<(byte[] Audio, string Mime, string Error)> SpeakAsync(
        string text, string? voice = null, CancellationToken ct = default)
    {
        if (!Ready) return (Array.Empty<byte>(), "", "未配置 Fish API key（见 tts.json）");

        var body = ToSpeechText(text);
        if (body.Length == 0) return (Array.Empty<byte>(), "", "这条消息没有可朗读的内容");

        // Fish 单次请求的文本上限比界面里的消息短得多。超了会被上游拒绝，
        // 而错误信息（400 加一段英文）对用户没有指导意义 —— 这里先自己截断
        // 并在末尾补一句，至少用户知道「后面没读完」。
        const int MaxChars = 2000;
        var truncated = body.Length > MaxChars;
        if (truncated) body = body[..MaxChars];

        var payload = new Dictionary<string, object>
        {
            ["text"] = body,
            ["format"] = string.IsNullOrWhiteSpace(_cfg.Format) ? "mp3" : _cfg.Format,
            ["normalize"] = true,
            ["latency"] = "normal",
        };
        var v = (voice ?? _cfg.Voice ?? "").Trim();
        if (v.Length > 0) payload["reference_id"] = v;

        using var req = new HttpRequestMessage(HttpMethod.Post, _cfg.Base.TrimEnd('/') + "/v1/tts");
        req.Content = new StringContent(
            JsonSerializer.Serialize(payload), Encoding.UTF8, "application/json");
        req.Headers.TryAddWithoutValidation("Authorization", "Bearer " + _cfg.ApiKey);
        // 这一行是整条链路里最容易写错的地方（见类注释）。
        req.Headers.TryAddWithoutValidation("model",
            string.IsNullOrWhiteSpace(_cfg.Model) ? "s2.1-pro-free" : _cfg.Model);

        try
        {
            using var resp = await Http.SendAsync(req, HttpCompletionOption.ResponseContentRead, ct);
            var bytes = await resp.Content.ReadAsByteArrayAsync(ct);
            if (!resp.IsSuccessStatusCode)
            {
                var msg = DescribeFailure((int)resp.StatusCode, bytes);
                return (Array.Empty<byte>(), "", msg);
            }
            var mime = resp.Content.Headers.ContentType?.MediaType ?? "audio/mpeg";
            return (bytes, mime, "");
        }
        catch (TaskCanceledException)
        {
            return (Array.Empty<byte>(), "", "Fish 合成超时（60 秒）");
        }
        catch (Exception ex)
        {
            return (Array.Empty<byte>(), "", "Fish 请求失败：" + ex.Message);
        }
    }

    /// <summary>
    /// 列出可用音色。
    ///
    /// **路由不带 `/v1`** —— `{Base}/model`。这个接口返回的是 Fish 的公开
    /// 模型库（上千条），按创建时间倒序。这里只取前 N 条：用户要的是
    /// 「随便挑一个能用的」，不是把整个库搬进面板。
    /// </summary>
    public async Task<(FishVoice[] Items, string Error)> VoicesAsync(
        int limit = 20, CancellationToken ct = default)
    {
        if (!Ready) return (Array.Empty<FishVoice>(), "未配置 Fish API key");

        var url = _cfg.Base.TrimEnd('/') + "/model?page_size=" + Math.Clamp(limit, 1, 100)
                + "&page_number=1";
        using var req = new HttpRequestMessage(HttpMethod.Get, url);
        req.Headers.TryAddWithoutValidation("Authorization", "Bearer " + _cfg.ApiKey);

        try
        {
            using var resp = await Http.SendAsync(req, ct);
            var raw = await resp.Content.ReadAsByteArrayAsync(ct);
            if (!resp.IsSuccessStatusCode)
                return (Array.Empty<FishVoice>(), DescribeFailure((int)resp.StatusCode, raw));

            using var doc = JsonDocument.Parse(raw);
            var items = new List<FishVoice>();
            if (doc.RootElement.TryGetProperty("items", out var arr) &&
                arr.ValueKind == JsonValueKind.Array)
            {
                foreach (var it in arr.EnumerateArray())
                {
                    if (it.ValueKind != JsonValueKind.Object) continue;
                    var id = Str(it, "_id");
                    if (id.Length == 0) continue;
                    var tags = Array.Empty<string>();
                    if (it.TryGetProperty("tags", out var tg) && tg.ValueKind == JsonValueKind.Array)
                        tags = tg.EnumerateArray()
                            .Select(x => x.ValueKind == JsonValueKind.String ? x.GetString() ?? "" : "")
                            .Where(x => x.Length > 0).ToArray();
                    items.Add(new FishVoice(id, Str(it, "title"), Str(it, "description"), tags));
                }
            }
            return (items.ToArray(), "");
        }
        catch (TaskCanceledException)
        {
            return (Array.Empty<FishVoice>(), "读取 Fish 音色超时");
        }
        catch (Exception ex)
        {
            return (Array.Empty<FishVoice>(), "读取 Fish 音色失败：" + ex.Message);
        }
    }

    /// <summary>把 HTTP 状态码翻成用户能懂的一句话。</summary>
    ///
    /// 402 尤其要说清楚：它**不是**「你没钱了」，最常见的原因是模型名
    /// 写成了付费模型。写成「余额不足」会让用户去充值，方向完全错。
    private static string DescribeFailure(int status, byte[] body)
    {
        var detail = "";
        try
        {
            var text = Encoding.UTF8.GetString(body);
            if (text.Length > 200) text = text[..200];
            detail = text.Trim();
        }
        catch { /* 响应体不是文本就算了 */ }

        var head = status switch
        {
            401 or 403 => "Fish API key 无效或已撤销",
            402 => "Fish 拒绝计费：模型名可能不是免费引擎（应为 s2.1-pro-free）",
            404 => "Fish 接口路径不存在（检查 Base 配置）",
            429 => "Fish 限流，请稍后再试",
            >= 500 => "Fish 服务端错误",
            _ => $"Fish 返回 HTTP {status}",
        };
        return detail.Length > 0 ? $"{head}（{detail}）" : head;
    }

    private static string Str(JsonElement o, string key) =>
        o.TryGetProperty(key, out var v) && v.ValueKind == JsonValueKind.String
            ? v.GetString() ?? "" : "";

    /// <summary>
    /// 把 Markdown 正文变成适合朗读的纯文本。
    ///
    /// **不做这件事会很难听**：Fish 会把 `**`、`###`、`|---|` 原样念出来。
    /// 这里只处理「念出来明显是噪音」的标记，不追求完整的 Markdown 解析 ——
    /// 过度清洗反而会吃掉正常内容（例如正文里本来就有的星号）。
    /// </summary>
    internal static string ToSpeechText(string markdown)
    {
        if (string.IsNullOrWhiteSpace(markdown)) return "";
        var s = markdown.Replace("\r\n", "\n").Replace('\r', '\n');

        // 代码块整段跳过：朗读代码没有意义，而且符号会变成噪音。
        s = System.Text.RegularExpressions.Regex.Replace(
            s, "```[\\s\\S]*?```", "\n（代码块略）\n");

        var lines = new List<string>();
        foreach (var raw in s.Split('\n'))
        {
            var line = raw.TrimEnd();
            var t = line.TrimStart();

            // 表格分隔行（|---|---|）念出来是一串「横线」。
            if (t.Length > 0 && t.All(c => c is '-' or '|' or ':' or ' ')) continue;

            // 标题、引用、列表符号：去掉前导标记，保留文字。
            t = System.Text.RegularExpressions.Regex.Replace(t, @"^#{1,6}\s*", "");
            t = System.Text.RegularExpressions.Regex.Replace(t, @"^>\s*", "");
            t = System.Text.RegularExpressions.Regex.Replace(t, @"^[-*+]\s+", "");
            t = System.Text.RegularExpressions.Regex.Replace(t, @"^\d+\.\s+", "");

            // 链接 `[文字](地址)` → 只留文字；地址念出来毫无意义。
            t = System.Text.RegularExpressions.Regex.Replace(t, @"\[([^\]]*)\]\([^)]*\)", "$1");
            // 行内代码与强调符号。
            t = t.Replace("`", "");
            t = System.Text.RegularExpressions.Regex.Replace(t, @"(\*\*|__|\*|_|~~)", "");

            // 表格竖线：`| 甲 | 乙 |` → `甲 乙`。竖线本身不发音，
            // 但会把两侧的字黏在一起（「甲|乙」念成「甲乙」），
            // 换成空格才有停顿。
            if (t.StartsWith('|') || t.Contains(" | "))
                t = System.Text.RegularExpressions.Regex.Replace(t, @"\s*\|\s*", " ");

            t = t.Trim();
            if (t.Length == 0)
            {
                // **空行必须保留** —— 它代表段落分隔。全过滤掉的话整篇会
                // 变成一整块没有停顿的文字，Fish 会一口气念到底，
                // 用户听不出哪句是新的段落。
                //
                // 连续空行先照收，最后由下面的 `\n{3,}` 统一压成一个空行。
                lines.Add("");
                continue;
            }
            lines.Add(t);
        }

        var joined = string.Join("\n", lines);
        // 连续空行压成一个，避免 Fish 在段落之间停顿过长。
        joined = System.Text.RegularExpressions.Regex.Replace(joined, @"\n{3,}", "\n\n");
        return joined.Trim();
    }

    public void Dispose() { /* HttpClient 是静态共享的，实例不持有资源 */ }
}
