using System.Diagnostics;
using System.IO;
using System.Text;
using System.Text.Json;

namespace DeepSeekAgent.Desktop;

/// <summary>引擎对某条请求返回了错误（带 id 的 error 响应）。</summary>
public sealed class EngineRequestException : Exception
{
    public EngineRequestException(string message) : base(message) { }
}

/// <summary>
/// 引擎客户端：以子进程方式托管 Python 引擎，走 stdio JSON-RPC。
/// 与 MCP 同构，因此引擎可以脱离 UI 单独测试。
/// </summary>
public sealed class EngineClient : IAsyncDisposable
{
    private readonly Process _proc;
    private readonly SemaphoreSlim _gate = new(1, 1);
    private readonly Dictionary<int, TaskCompletionSource<JsonElement>> _pending = new();
    private readonly object _pendingLock = new();
    private readonly StreamWriter? _log;
    private readonly object _logLock = new();
    private int _nextId;

    // ---------- 日志降噪（2026-10-19） ----------
    //
    // 折叠逻辑抽在 `StatusLogThrottle`（独立类，可单测）。这里只持有一个实例。
    private readonly StatusLogThrottle _statusThrottle = new();

    /// <summary>本次运行的引擎日志文件路径（诊断用；可能为 null）。</summary>
    public string? LogPath { get; }

    public event Action<string, JsonElement>? EngineEvent;
    public event Action<string>? Fault;

    public string PythonPath { get; }
    public string EngineRoot { get; }

    private EngineClient(Process proc, string python, string root)
    {
        _proc = proc;
        PythonPath = python;
        EngineRoot = root;
        _log = OpenLog(out var path);
        LogPath = path;
    }

    /// <summary>
    /// 把引擎的 stdout / stderr **落盘**。
    ///
    /// 为什么必须有：在此之前这两条管道只在内存里走一趟 —— 外壳读走、显示到监视器、
    /// 然后就没了。于是「用户报障 → 我们手上没有任何现场」。2026-10-14 报障
    /// 「没有一个回复是完整的，都是半途断了」，我们只能靠读源码反推，
    /// 因为**日志根本不存在**。落盘之后，任何一次测试都留下可查证的现场。
    /// </summary>
    private static StreamWriter? OpenLog(out string? path)
    {
        path = null;
        try
        {
            var dir = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "DeepSeek-Agent",
                "logs");
            Directory.CreateDirectory(dir);
            path = Path.Combine(dir, $"engine-{DateTime.Now:yyyyMMdd-HHmmss}.log");

            // 保留策略在**建新文件之前**跑一次 —— 这是唯一的自然时机：
            // 每次启动都会新建日志，所以清理频率天然跟得上产生速度；
            // 放在运行期定时清理反而要和写入抢文件句柄。
            //
            // 失败不影响启动：`Apply` 内部吞掉所有异常，
            // `Plan` 本身也不读文件内容、不删东西。
            string? cleanupNote = null;
            try
            {
                var doomed = LogRetention.Plan(
                    new DirectoryInfo(dir), DateTime.UtcNow, protectPath: path);
                var (deleted, freed) = LogRetention.Apply(doomed);
                if (deleted > 0)
                {
                    // 记进新日志的首行之一 —— 清理动作本身也要可回溯，
                    // 否则「我的日志怎么没了」无从解释。
                    cleanupNote =
                        $"# 日志清理：删除 {deleted} 个旧文件，释放 {LogRetention.Human(freed)}";
                }
            }
            catch { /* 清理失败绝不影响引擎启动 */ }

            var writer = new StreamWriter(path, append: true, Encoding.UTF8) { AutoFlush = true };
            writer.WriteLine($"# DeepSeek Agent 引擎日志 · {DateTimeOffset.Now:O}");
            if (cleanupNote is not null) writer.WriteLine(cleanupNote);
            return writer;
        }
        catch
        {
            path = null;
            return null;   // 记日志失败绝不能影响引擎启动
        }
    }

    private void Trace(string tag, string line)
    {
        var writer = _log;
        if (writer is null) return;
        lock (_logLock)
        {
            try { writer.WriteLine($"{DateTimeOffset.Now:HH:mm:ss.fff} [{tag}] {line}"); }
            catch { /* 磁盘满 / 文件被占 —— 静默 */ }
        }
    }

    public static EngineClient Start(string engineRoot)
    {
        var python = ResolvePython(engineRoot);
        var psi = new ProcessStartInfo(python)
        {
            WorkingDirectory = engineRoot,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true,
            StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8,
        };
        psi.ArgumentList.Add("-X");
        psi.ArgumentList.Add("utf8");
        psi.ArgumentList.Add("-m");
        psi.ArgumentList.Add("gad_engine");

        // 放行「真实鉴权探测」—— **只有产品外壳这一条路能带上它**。
        //
        // 引擎侧 `_probe_once` 默认拒绝打真实接口（见 deepseek_rpc.py 的
        // ALLOW_LIVE_ENV 说明）：2026-10-17 账号 shaomo17 被 DeepSeek 封禁，
        // 直接原因就是反复拿真实接口做鉴权探测（还用了垃圾 token 去撞）。
        // 用户手动点「刷新并验证 / 保存并验证 / 登录收口」时**必须**能真验，
        // 否则面板会永远说「验不了」；所以在启动引擎时把开关带上。
        //
        // 测试与临时脚本不走这条路（它们自己 subprocess 起引擎），
        // 因此默认就是打不出去的 —— 这正是我们要的失败方向。
        psi.Environment["GAD_ALLOW_LIVE_AUTH"] = "1";

        var proc = new Process { StartInfo = psi, EnableRaisingEvents = true };
        if (!proc.Start())
            throw new InvalidOperationException("引擎进程启动失败");

        var client = new EngineClient(proc, python, engineRoot);
        client.PumpStdout();
        client.PumpStderr();
        return client;
    }

    /// <summary>定位引擎根目录：环境变量优先，否则从可执行文件位置向上找。</summary>
    public static string ResolveEngineRoot()
    {
        var fromEnv = Environment.GetEnvironmentVariable("GAD_ENGINE_ROOT");
        if (!string.IsNullOrWhiteSpace(fromEnv) && Directory.Exists(fromEnv))
            return fromEnv!;

        DirectoryInfo? dir = new DirectoryInfo(AppContext.BaseDirectory);
        while (dir is not null)
        {
            var candidate = Path.Combine(dir.FullName, "engine", "gad_engine");
            if (Directory.Exists(candidate))
                return Path.Combine(dir.FullName, "engine");
            dir = dir.Parent;
        }
        throw new DirectoryNotFoundException(
            "未找到 engine/gad_engine。请设置环境变量 GAD_ENGINE_ROOT 指向 engine 目录。");
    }

    /// <summary>
    /// 定位引擎的 python 解释器：优先用 venv，找不到才退回 PATH 上的 `python`。
    ///
    /// `public` 是因为外壳的 `--powcheck` 门禁要**直接起一次**
    /// `python -m gad_engine --powcheck` —— 那道门禁纯本地、不需要 JSON-RPC
    /// 会话，所以不走 <see cref="Start"/>，但同样得知道解释器在哪。
    /// </summary>
    public static string ResolvePython(string engineRoot)
    {
        var venv = Path.Combine(engineRoot, ".venv", "Scripts", "python.exe");
        return File.Exists(venv) ? venv : "python";
    }

    private void PumpStdout()
    {
        _ = Task.Run(async () =>
        {
            try
            {
                while (await _proc.StandardOutput.ReadLineAsync() is { } line)
                {
                    if (string.IsNullOrWhiteSpace(line)) continue;
                    // `status` 响应走指纹折叠（见 StatusLogThrottle），其余原样落盘。
                    // **无论折叠与否都要 Dispatch** —— 降噪只影响日志，不影响协议。
                    var logged = _statusThrottle.Offer(line, DateTime.UtcNow);
                    if (logged is null) Trace("out", line);
                    else if (logged.Length > 0) Trace("out", logged);
                    try { Dispatch(line); }
                    catch (Exception ex) { Fault?.Invoke($"引擎消息解析失败：{ex.Message}"); }
                }
            }
            catch (Exception ex) { Fault?.Invoke($"引擎输出中断：{ex.Message}"); }
        });
    }

    private void PumpStderr()
    {
        _ = Task.Run(async () =>
        {
            try
            {
                while (await _proc.StandardError.ReadLineAsync() is { } line)
                {
                    if (string.IsNullOrWhiteSpace(line)) continue;
                    Trace("err", line);
                    Fault?.Invoke(line);
                }
            }
            catch { /* 进程退出时忽略 */ }
        });
    }

    private void Dispatch(string line)
    {
        using var doc = JsonDocument.Parse(line);
        var root = doc.RootElement;

        // 带 id 的响应：无论是 result 还是 error，都必须把对应的等待者唤醒。
        // 早先只认 result，带 id 的 error 会掉进下面的 Fault 分支，
        // 结果是「参数校验失败」这类瞬时错误要等满超时才抛出来。
        if (root.TryGetProperty("id", out var idEl) && idEl.ValueKind == JsonValueKind.Number)
        {
            var id = idEl.GetInt32();
            TaskCompletionSource<JsonElement>? tcs;
            lock (_pendingLock)
            {
                _pending.TryGetValue(id, out tcs);
                if (tcs is not null) _pending.Remove(id);
            }
            if (tcs is not null)
            {
                if (root.TryGetProperty("result", out var resEl))
                    tcs.TrySetResult(resEl.Clone());
                else if (root.TryGetProperty("error", out var reqErrEl))
                    tcs.TrySetException(new EngineRequestException(
                        reqErrEl.GetString() ?? "引擎返回未知错误"));
                else
                    tcs.TrySetException(new EngineRequestException("引擎返回了无法识别的响应"));
                return;
            }
        }

        if (root.TryGetProperty("type", out var typeEl) &&
            typeEl.GetString() == "event" &&
            root.TryGetProperty("event", out var evEl) &&
            root.TryGetProperty("data", out var dataEl))
        {
            EngineEvent?.Invoke(evEl.GetString() ?? string.Empty, dataEl.Clone());
            return;
        }

        if (root.TryGetProperty("error", out var errEl))
            Fault?.Invoke(errEl.GetString() ?? "引擎返回未知错误");
    }

    public async Task<JsonElement> RequestAsync(
        string method, object? parameters = null, int timeoutSeconds = 180)
    {
        var id = Interlocked.Increment(ref _nextId);
        var tcs = new TaskCompletionSource<JsonElement>(TaskCreationOptions.RunContinuationsAsynchronously);
        lock (_pendingLock) _pending[id] = tcs;

        var payload = new Dictionary<string, object?> { ["id"] = id, ["method"] = method };
        if (parameters is not null) payload["params"] = parameters;

        await _gate.WaitAsync().ConfigureAwait(false);
        try
        {
            await _proc.StandardInput.WriteLineAsync(JsonSerializer.Serialize(payload))
                .ConfigureAwait(false);
            await _proc.StandardInput.FlushAsync().ConfigureAwait(false);
        }
        finally
        {
            _gate.Release();
        }

        return await tcs.Task.WaitAsync(TimeSpan.FromSeconds(timeoutSeconds)).ConfigureAwait(false);
    }

    public Task<JsonElement> InitializeAsync() => RequestAsync("initialize");
    public Task<JsonElement> StartAsync() => RequestAsync("start", timeoutSeconds: 60);
    public Task<JsonElement> StatusAsync(bool poll = false) =>
        RequestAsync("status", new { poll }, timeoutSeconds: 60);
    /// <summary>
    /// 起一轮任务。`files` 是**附件绝对路径**，由引擎上传后挂进 `ref_file_ids`。
    ///
    /// 传 `null` 时 JSON 里是 <c>"files": null</c>，引擎侧 `params.get("files")`
    /// 拿到 None 并原样当成「没有附件」—— 与不传这个键等价，所以老的调用点
    /// 行为一字不变。
    /// </summary>
    public Task<JsonElement> RunAsync(string goal, int? maxTurns = null,
        IReadOnlyList<string>? files = null) =>
        RequestAsync("run.async", new { goal, max_turns = maxTurns, files });
    // 取消是纯本地动作（只是 cancel 一个 asyncio task），10s 足够；
    // 给短超时是为了让按钮立刻有反馈，而不是等出「取消不了」的假象。
    public Task<JsonElement> CancelRunAsync() =>
        RequestAsync("run.cancel", timeoutSeconds: 10);
    public Task<JsonElement> ResetBreakerAsync() => RequestAsync("breaker.reset");

    // ---------- 初始提示词 ----------
    //
    // 提示词是引擎每轮现读的，所以 `prompt.set` 之后**下一轮就生效**，
    // 不需要重启引擎 —— 「调提示词」这件事要反复试，不能每改一次重启一次。

    public Task<JsonElement> GetPromptAsync() => RequestAsync("prompt.get", timeoutSeconds: 30);

    public Task<JsonElement> SetPromptAsync(string template) =>
        RequestAsync("prompt.set", new { template }, timeoutSeconds: 30);

    public Task<JsonElement> ResetPromptAsync() => RequestAsync("prompt.reset", timeoutSeconds: 30);

    /// <summary>重新验证缓存凭据；不行时顺带用 CDP 从已开的浏览器补一次（不弹新窗口）。
    /// 会真发一次 `create_pow_challenge` 做验证，所以给 60s。</summary>
    public Task<JsonElement> RefreshIdentityAsync() =>
        RequestAsync("identity.refresh", timeoutSeconds: 60);

    /// <summary>
    /// 启动自检：**只验证缓存 token，不碰 CDP**。
    ///
    /// 与 <see cref="RefreshIdentityAsync"/> 的区别是失败后不去连调试端口 ——
    /// 启动路径上不该因为「浏览器没开」而多等一轮超时，也不该顺带换 token。
    ///
    /// **注意它能证明什么、不能证明什么**（2026-10-17 实测）：
    ///   · 能证明：token 仍被接受、TLS/头指纹被接受、链路可达
    ///   · **不能证明账号没被封** —— `create_pow_challenge` 不校验账号处置。
    ///     封禁只在 `completion` 的响应里报 40003 + `suspended`。
    ///   所以「启动自检通过」只代表凭据有效，不代表账号能用；界面不该据此
    ///   宣称「账号正常」。真正的处置状态仍由发送路径捕获。
    /// </summary>
    public Task<JsonElement> ProbeIdentityAsync() =>
        RequestAsync("identity.refresh", new { probe_only = true }, timeoutSeconds: 30);

    public Task<JsonElement> ConfigSetAsync(object sections) =>
        RequestAsync("config.set", sections, timeoutSeconds: 30);

    /// <summary>
    /// 本机可选的传输档位，连同「装了没 / 能不能连上」。
    ///
    /// **由引擎回答而不是界面写死**：`gemini-webapi` 装没装、cookie 文件在不在、
    /// 当前是不是访客模式 —— 这三项只有引擎知道。界面写死一张表的话，
    /// 任何一项变化都要改 C# 与 JS 两处，而且必然漂移。
    /// </summary>
    public Task<JsonElement> ConfigTransportsAsync() =>
        RequestAsync("config.transports", timeoutSeconds: 30);

    // 身份接口：`identity.status` / `clear` 是纯本地文件操作；
    // `identity.set` 会验证一次（一次极轻的网络往返），所以给 60s。
    //
    // **注意与 gemini 版的结构差别**：那边是 `account.set(label, psid, psidts)`
    // 的多账号模型；DeepSeek 只有一个 token，所以没有 label，也没有
    // `account.guest`（无访客模式）、`account.list` / `account.use`（无多账号）。
    public Task<JsonElement> IdentityStatusAsync() =>
        RequestAsync("identity.status", timeoutSeconds: 30);

    public Task<JsonElement> IdentitySetAsync(string token) =>
        RequestAsync("identity.set", new { token }, timeoutSeconds: 60);

    public Task<JsonElement> IdentityClearAsync() =>
        RequestAsync("identity.clear", timeoutSeconds: 30);

    /// <summary>
    /// 退出登录：清 token **并清掉专用浏览器 profile**（换账号用）。
    /// 只清 token 的话，重新登录会带着旧 cookie 回到同一个账号。
    /// 删 35 MB 的 profile 目录可能慢一点，给 60 秒。
    /// </summary>
    public Task<JsonElement> IdentityLogoutAsync() =>
        RequestAsync("identity.logout", timeoutSeconds: 60);

    // 浏览器登录：start 会真的拉起一个浏览器进程，所以给足超时；
    // poll / cancel 都是本地 CDP 往返，短超时足够；finish 会验证一次凭据。
    public Task<JsonElement> IdentityLoginStartAsync() =>
        RequestAsync("identity.login.start", timeoutSeconds: 90);
    public Task<JsonElement> IdentityLoginPollAsync() =>
        RequestAsync("identity.login.poll", timeoutSeconds: 60);
    public Task<JsonElement> IdentityLoginFinishAsync() =>
        RequestAsync("identity.login.finish", timeoutSeconds: 60);
    public Task<JsonElement> IdentityLoginCancelAsync() =>
        RequestAsync("identity.login.cancel", timeoutSeconds: 30);

    // 模型：list 只是读运行时里已经发现好的列表（`init()` 时就拉过一次），
    // 不走网络；set 会向上游校验一次名字，所以给得比 list 宽一点。
    // 两者都不重启引擎 —— 模型是 per-chat 参数，下一轮发送自然生效。
    public Task<JsonElement> ModelsListAsync() =>
        RequestAsync("models.list", timeoutSeconds: 30);
    public Task<JsonElement> ModelSetAsync(string name) =>
        RequestAsync("model.set", new { name }, timeoutSeconds: 30);

    // 会话形态（一般/临时对话、是否思考模式）。同样是纯本地配置写入 + 一次
    // 上游模型名解析之外的零网络操作，所以给 30s。
    // 只传要改的键 —— 引擎把「键不存在」当成「这一项不动」，传 null 也一样。
    public Task<JsonElement> ChatSetAsync(IDictionary<string, object?> patch) =>
        RequestAsync("chat.set", patch, timeoutSeconds: 30);

    // 工具权限（三档 + dry-run）。纯本地配置写入，一次网络都不发；
    // 引擎侧也不重启运行时 —— 权限在每次工具分发前实时读，改完立刻生效。
    // 同样只传要改的键，缺省 = 这一项不动。
    public Task<JsonElement> PermissionsSetAsync(IDictionary<string, object?> patch) =>
        RequestAsync("permissions.set", patch, timeoutSeconds: 30);

    // ---------- 对话与历史 ----------
    //
    // 全部是**本地文件操作**（引擎侧只碰 `<app_data>/conversations/`），
    // 不发任何网络请求，也不受熔断影响 —— 所以 30s 超时绰绰有余，
    // 而且引擎没启动时也会按需把 runtime 建起来（不 start 传输层）。

    public Task<JsonElement> ConversationListAsync() =>
        RequestAsync("conversation.list", timeoutSeconds: 30);

    public Task<JsonElement> ConversationCurrentAsync() =>
        RequestAsync("conversation.current", timeoutSeconds: 30);

    public Task<JsonElement> ConversationNewAsync() =>
        RequestAsync("conversation.new", timeoutSeconds: 30);

    public Task<JsonElement> ConversationLoadAsync(string id) =>
        RequestAsync("conversation.load", new { id }, timeoutSeconds: 30);

    /// <summary>从服务端拉该对话的历史并合并（**会发网络请求**）。</summary>
    ///
    /// <remarks>
    /// 与上面几个不同，这个**要联网** —— 它打的是 `chat/history_messages`。
    /// 所以超时给得比本地操作宽（本地那批 30s 是绰绰有余，这个要留出
    /// 网络抖动 + 服务端慢的空间），失败时调用方应静默退回本地历史。
    /// </remarks>
    public Task<JsonElement> ConversationSyncAsync(string id) =>
        RequestAsync("conversation.sync", new { id }, timeoutSeconds: 60);

    /// <summary>
    /// 接力换会话：把当前对话的上下文压缩成摘要，开一条新会话继续（**会联网**）。
    /// </summary>
    /// <remarks>
    /// 这是**用户主动**的换会话动作。自动轮换默认已关（`pacing.auto_rotate`），
    /// 所以这是现在唯一的换法 —— 换的那一刻由引擎负责把上下文接上，
    /// 否则新会话就是一张白纸。
    ///
    /// `mode` 留空 = 用引擎配置里的（`model` 问一次模型 / `local` 纯规则零请求）。
    /// 超时给得宽：`model` 模式要在旧会话里问一次摘要，而上游首字节常常十几秒，
    /// 再叠加节奏闸门的等待。**给宽不是为了让它慢慢来，是为了别在摘要已经
    /// 生成、只是回来得晚一点的时候误判成失败**（那时引擎侧其实已经换好会话了，
    /// 外壳却以为没换 —— 界面和实际状态会错位）。
    /// </remarks>
    public Task<JsonElement> ConversationHandoffAsync(string mode = "") =>
        RequestAsync(
            "conversation.handoff",
            string.IsNullOrWhiteSpace(mode) ? null : new { mode },
            timeoutSeconds: 150);

    /// <summary>列出账号下的服务端会话（**会发网络请求**）。</summary>
    /// <remarks>
    /// 打的是 `chat_session/fetch_page`。返回里既有本客户端建的会话，也有
    /// 用户在 DeepSeek 网页版里建的 —— 后者本地此前完全看不见。
    /// </remarks>
    public Task<JsonElement> ConversationRemoteListAsync(int count = 50) =>
        RequestAsync("conversation.remote_list", new { count }, timeoutSeconds: 60);

    /// <summary>接管一条服务端会话成本地对话（**会发网络请求**）。</summary>
    /// <remarks>
    /// 引擎会拉该会话的全量历史、用 `current_message_id` 校正续接锚点，
    /// 并把它标记为「接管」（不自动轮换）。此后在该对话里发的消息就进那条
    /// `chat_session_id`。
    /// </remarks>
    public Task<JsonElement> ConversationAttachAsync(string remoteId) =>
        RequestAsync("conversation.attach", new { remote_id = remoteId }, timeoutSeconds: 60);

    /// <summary>删除一条服务端会话（**不可逆，会发网络请求**）。</summary>
    /// <remarks>
    /// 打的是 `chat_session/delete`。**调用方必须先做二次确认** —— 引擎只执行，
    /// 不交互。删成功时引擎还会把本地绑定一并解开（否则那条本地对话的
    /// `session.cid` 会指向一个已消失的会话，下一轮发送直接报
    /// `invalid chat session id`）。
    ///
    /// 返回 `{"deleted": bool, "unbound": bool, "local_id": str, "reason": str}`；
    /// 失败不抛异常，看 `deleted` 与 `reason`。
    /// </remarks>
    public Task<JsonElement> ConversationRemoteDeleteAsync(string remoteId) =>
        RequestAsync(
            "conversation.remote_delete", new { remote_id = remoteId }, timeoutSeconds: 60);

    public Task<JsonElement> ConversationRenameAsync(string id, string title) =>
        RequestAsync("conversation.rename", new { id, title }, timeoutSeconds: 30);

    public Task<JsonElement> ConversationDeleteAsync(string id) =>
        RequestAsync("conversation.delete", new { id }, timeoutSeconds: 30);

    // ---------- 朗读（TTS）控制面 ----------
    //
    // 引擎只签票、不传音频。前端拿到 ticket 后直连上游 WS，用 WebView2 自带的
    // WebCodecs 解 Opus —— 这是整个设计的关键分工，理由见
    // `engine/gad_engine/transport/deepseek_rpc.py` 里 `TTS_TICKET_PATH` 的注释。

    /// <summary>为某条消息签发朗读票据（**会发网络请求**）。</summary>
    /// <remarks>
    /// 返回 <c>{"sid","message_id","ticket","error"}</c>。票据是**一次性、
    /// 短时效、只对单条消息有效**的凭证，不是账号 token —— 可以安全地交给
    /// 前端。失败不抛异常，看 <c>error</c>。
    /// </remarks>
    public Task<JsonElement> TtsTicketAsync(string messageId) =>
        RequestAsync("tts.ticket", new { message_id = messageId }, timeoutSeconds: 60);

    /// <summary>列出朗读音色（只读，**不消耗朗读配额**）。</summary>
    /// <remarks>返回 <c>{"items":[...], "error":""}</c>。</remarks>
    public Task<JsonElement> TtsVoicesAsync() =>
        RequestAsync("tts.voices", timeoutSeconds: 60);

    /// <summary>切换朗读音色（**写操作**，改账号级偏好，网页版同步生效）。</summary>
    public Task<JsonElement> TtsSetVoiceAsync(string voiceId) =>
        RequestAsync("tts.voice", new { voice_id = voiceId }, timeoutSeconds: 60);

    // ---------- 长期记忆 ----------

    public Task<JsonElement> MemoryGetAsync() =>
        RequestAsync("memory.get", timeoutSeconds: 30);

    public Task<JsonElement> MemorySetAsync(string text) =>
        RequestAsync("memory.set", new { text }, timeoutSeconds: 30);

    public Task<JsonElement> MemoryAppendAsync(string text, string section = "") =>
        RequestAsync("memory.append", new { text, section }, timeoutSeconds: 30);

    public Task<JsonElement> MemoryClearAsync() =>
        RequestAsync("memory.clear", timeoutSeconds: 30);

    public async ValueTask DisposeAsync()
    {
        try
        {
            await RequestAsync("shutdown", timeoutSeconds: 10).ConfigureAwait(false);
        }
        catch { /* 引擎可能已经退出 */ }

        try
        {
            if (!_proc.WaitForExit(3000)) _proc.Kill(entireProcessTree: true);
        }
        catch { /* 忽略 */ }
        _proc.Dispose();
        _gate.Dispose();
        lock (_logLock)
        {
            try { _log?.Dispose(); } catch { /* 忽略 */ }
        }
    }
}
