using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Runtime.CompilerServices;
using System.Text.Json;
using System.Windows.Threading;

namespace DeepSeekAgent.Desktop;

/// <summary>会话流水条目的语义类型。经典版与优化版各自决定怎么画。</summary>
public enum TranscriptKind
{
    Goal,
    Assistant,
    TurnDivider,
    ToolCall,
    Gate,
    Breaker,
    Notice,
    Error,
    Summary,
    Info,
    /// <summary>「思考中」占位条 —— 只在 `chat.show_thinking=false` 时存在，
    /// 正文一开始就撤掉。它不是为了展示思考内容，是为了让用户知道**确实在跑**。</summary>
    Thinking,
    /// <summary>可折叠的「思考过程」块 —— 上游 `THINK` 片段的**原文**。
    ///
    /// 与 <see cref="Thinking"/> 的区别：那条只是一行心跳（「思考中…（已 N 字）」），
    /// 这条装着思考全文，思考时展开、正文一开始就自动收起。
    /// 由 `chat.show_thinking` 决定走哪条 —— 两者**互斥**，不会同时出现。
    /// </summary>
    Thought,
}

/// <summary>
/// 一条「思考过程」的可见状态（只在 <see cref="TranscriptKind.Thought"/> 上有意义）。
///
/// 思考原文本身放在 <see cref="TranscriptItem.Text"/> 里（那是会触发
/// `PropertyChanged` 的流式字段），这里只放头部文案要用的元数据。
/// </summary>
public sealed class ThoughtState
{
    public int Turn { get; init; }

    /// <summary>上游已吐出的思考字数（引擎按 0.5 秒节流上报）。</summary>
    public int Chars { get; set; }

    /// <summary>「还没开口」时上游已经等了多久（秒）。只有还没吐思考增量时才有意义。</summary>
    public int Waited { get; set; }

    /// <summary>是否还在思考中。正文一开始流就置 false，视图据此把块收起来。</summary>
    public bool Streaming { get; set; } = true;
}

public enum LogLevel
{
    Faint,
    Soft,
    Ok,
    Warn,
    Bad,
}

/// <summary>
/// 会话流水的单条记录。
/// 实现 INotifyPropertyChanged 是为了流式增量：`Text` 一变，
/// 用模板绑定的界面（优化版）自动刷新，不必自己去找对应的控件。
/// </summary>
public sealed class TranscriptItem : INotifyPropertyChanged
{
    private string _text = "";

    public int Seq { get; init; }
    public TranscriptKind Kind { get; init; }
    public string Detail { get; init; } = "";
    public DateTimeOffset At { get; init; } = DateTimeOffset.Now;

    /// <summary>
    /// 这条气泡对应的**服务端助手消息 id**（`response_message_id`）。
    ///
    /// 只有「有正文的助手气泡」才有值，而且**一轮只落一条**（见
    /// `HandleEvent` 的 `turn_end`）：朗读票据是按**单条服务端消息**签发的，
    /// 不是按会话、更不是按气泡 —— 一次任务里工具调用会把正文切成好几块，
    /// 但它们对应的是同一条服务端消息，给每块都挂同一个 id 只会让前端
    /// 画出一排功能完全相同的按钮。
    ///
    /// 空串 = 这条读不了（思考块 / 工具卡片 / 用户消息 / 本轮没正文）。
    /// </summary>
    public string MessageId { get; set; } = "";

    public string Text
    {
        get => _text;
        set
        {
            if (_text == value) return;
            _text = value;
            PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(Text)));
        }
    }

    /// <summary>
    /// 工具调用的实时状态（只在 <see cref="TranscriptKind.ToolCall"/> 上有意义）。
    /// 一张卡片会经历「运行中 → 完成」，靠它就地更新，而不是每来一条事件就
    /// 往对话流里再插一行 —— 那会把流刷爆。
    /// </summary>
    public ToolRunState? Tool { get; set; }

    /// <summary>
    /// 「思考过程」块的实时状态（只在 <see cref="TranscriptKind.Thought"/> 上有意义）。
    /// 思考原文放在 <see cref="Text"/> 里 —— 那条走 `PropertyChanged`，视图绑定即可。
    /// </summary>
    public ThoughtState? Thought { get; set; }

    public event PropertyChangedEventHandler? PropertyChanged;
}

/// <summary>
/// 一次工具调用的可见状态。字段来自引擎的三条事件：
/// `tool_start` / `tool_progress` / `tool_done`，另有 `stream` 喂进来的实时输出。
/// </summary>
public sealed class ToolRunState
{
    public int Seq { get; init; }
    public int Turn { get; init; }
    public string Kind { get; init; } = "";
    public string Target { get; init; } = "";
    public string Payload { get; init; } = "";

    public bool Running { get; set; } = true;
    public long ElapsedMs { get; set; }
    public int ExitCode { get; set; }
    public string Output { get; set; } = "";
    public bool Rejected { get; set; }

    /// <summary>工具类型的中文短名。</summary>
    public string Label => Kind switch
    {
        "local_cmd" => "执行命令",
        "local_cmd:async" => "后台执行",
        "write_file" => "写入文件",
        "agent-screenshot" => "截屏",
        "agent-attach" => "附加文件",
        _ => Kind,
    };

    /// <summary>卡片上要显示的那一行主体：命令显示命令本身，写文件显示路径。
    /// （写文件的 `payload` 是**文件内容**，不能往卡片上贴。）</summary>
    public string Subject => Kind switch
    {
        "local_cmd" or "local_cmd:async" => Payload,
        "write_file" => Target,
        "agent-attach" => Payload,
        _ => Target,
    };

    /// <summary>补充信息（写入文件的体量之类）。</summary>
    public string Note => Kind == "write_file" && Payload.Length > 0
        ? $"内容 {System.Text.Encoding.UTF8.GetByteCount(Payload)} 字节"
        : "";

    /// <summary>是否失败（非零退出码，或被安全策略拒绝）。</summary>
    public bool Failed => Rejected || ExitCode != 0;
}

/// <summary>
/// 初始提示词的一份快照。开源之后提示词是给用户自己改的，所以界面需要拿到
/// 模板原文、文件位置、以及**和真实运行同一套渲染**出来的预览。
/// </summary>
public sealed class PromptDoc
{
    public string Path { get; init; } = "";
    public string Template { get; init; } = "";
    public string DefaultTemplate { get; init; } = "";
    public bool IsDefault { get; init; }
    public List<string> Placeholders { get; init; } = new();

    /// <summary>模板里写了、但程序不认识的占位符（例如把 {{soul}} 打成 {{Soul}}）。
    /// 渲染时会**原样保留**，这里报出来让用户自己发现。</summary>
    public List<string> UnknownPlaceholders { get; init; } = new();

    /// <summary>拼好的最终提示词（占位符已替换）。</summary>
    public string Preview { get; init; } = "";
}

public sealed class LogItem
{
    public int Seq { get; init; }
    public string Text { get; init; } = "";
    public LogLevel Level { get; init; }
    public DateTimeOffset At { get; init; } = DateTimeOffset.Now;
}

/// <summary>闸门状态的强类型快照。原始 JSON 只在这里解析一次。</summary>
public sealed class HostStatus
{
    public bool Started { get; set; }
    public string Mode { get; set; } = "";
    public string Transport { get; set; } = "—";
    public bool TransportReady { get; set; } = true;

    /// <summary>
    /// 账号是否被服务端处置（封禁 / 停用）。
    ///
    /// **必须与「未登录 / token 过期」分开** —— DeepSeek 把两者都报成
    /// HTTP 200 + 业务码 40003，只看状态码分不出来。处置是账号级的，
    /// 有明确到期时间，界面提示「重新登录」永远修不好它。
    /// 证据来源：传输层在响应原文里识别到 suspended 标记后置位
    /// （见 `deepseek_rpc.looks_suspended`）。
    /// </summary>
    public bool AccountSuspended { get; set; }

    /// <summary>处置的响应原文（含截止时间），直接展示给用户。</summary>
    public string SuspendedDetail { get; set; } = "";

    public string CircuitLevel { get; set; } = "?";
    public string CircuitReason { get; set; } = "";

    /// <summary>熔断层的**最近一次信号**（`auth` / `rate_limit` / `server_busy` /
    /// `biz_error` / `rpc_error`）。语义映射在引擎侧（`circuit.Signal`），界面不自己写一份。</summary>
    public string LastSignal { get; set; } = "";

    /// <summary>最近一次信号发生的时刻（HH:mm:ss）。</summary>
    public string LastSignalAt { get; set; } = "";

    /// <summary>连续「服务端忙」次数。到阈值会升成长冷却 ——
    /// 这是本档位唯一一个「出事之前」能拿到的信号（见 `circuit.note_server_busy`）。</summary>
    public int BusyStreak { get; set; }

    public int AuthFailures { get; set; }
    public int RpcErrors { get; set; }
    public int BizErrors { get; set; }

    /// <summary>降级目标（`none` / `slower` / `webview`）。</summary>
    public string Degrade { get; set; } = "none";

    /// <summary>「刷新凭据」是否正在跑。用来禁用按钮 + 显示进度文案。</summary>
    public bool IdentityRefreshing { get; set; }

    /// <summary>是否已持有可用的 token（来自 `identity` 快照）。</summary>
    public bool HasToken { get; set; }

    /// <summary>账号标识（best-effort，可能为空 —— DeepSeek 侧认不出时由用户手填）。</summary>
    public string IdentityLabel { get; set; } = "";

    /// <summary>当前指纹伪装档位（`chrome150` 等），给身份对话框显示。</summary>
    public string Impersonate { get; set; } = "";

    /// <summary>凭据文件落点，给身份对话框显示（只显示路径，不显示内容）。</summary>
    public string TokenFile { get; set; } = "";

    /// <summary>L1 身份层的阻塞性违规项。空 = 可以提交。</summary>
    public List<string> IdentityViolations { get; } = new();

    /// <summary>非阻塞性提示（如「DeepSeek 无访客模式」）。</summary>
    public List<string> IdentityNotices { get; } = new();

    /// <summary>本会话使用的模型名。空字符串 = 账号默认。</summary>
    public string Model { get; set; } = "";

    public bool GateAllowed { get; set; }
    public string GateReason { get; set; } = "";
    public double GateWaitMs { get; set; }

    public int BurstCount { get; set; }
    public int MaxBurst { get; set; }
    public double LastGapMs { get; set; }
    public bool InActiveWindow { get; set; }

    public string SessionCid { get; set; } = "";
    public int SessionTurns { get; set; }
    public int SessionMaxTurns { get; set; }

    /// <summary>
    /// 会话是否走「临时对话」。
    /// 临时对话**不会**出现在 DeepSeek 网页版的历史记录里 —— 这是刻意的
    /// （不留痕），但用户看到「历史里找不到刚才的对话」时极易误判成
    /// 「根本没用到我的账号」，所以要在界面上显式说出来。
    /// 默认值必须和引擎侧 `ChatConfig.temporary` 一致（= false = 一般对话），
    /// 否则 `chat` 块缺失时界面会谎报成「临时对话」。
    /// </summary>
    public bool SessionTemporary { get; set; } = false;

    /// <summary>
    /// 是否开启思考模式（上游 `extended_thinking`）。
    /// 它写在每一轮的请求体与模型选择头里，属于 per-turn 参数 ——
    /// 和模型一样，切换不重启引擎，下一轮发送就生效。
    ///
    /// 布尔是真源；界面上的下拉读它来决定选中项。
    /// </summary>
    public bool ExtendedThinking { get; set; } = true;

    /// <summary>思考模式的中文短标签，给监视器那行用。真源是 <see cref="ExtendedThinking"/>。</summary>
    public string ThinkingMode => ExtendedThinking ? "思考模式" : "标准模式";

    /// <summary>
    /// 是否开启联网搜索（上游请求体的 `search_enabled`，即网页版顶栏那个搜索按钮）。
    /// 与思考模式同属 per-turn 参数 —— 写在每一轮的请求体里，切换不重启引擎，下一轮生效。
    ///
    /// **默认 false，与引擎侧 `ChatConfig.search_enabled` 一致。**
    /// 联网是 opt-in：跟随网页版「用户点了才搜」的行为，不替用户恒开。
    /// </summary>
    public bool SearchEnabled { get; set; } = false;

    /// <summary>
    /// 是否把思考原文显示出来（引擎侧 `chat.show_thinking`）。
    ///
    /// **与 <see cref="ExtendedThinking"/> 是两件事**，别混：
    ///   · `ExtendedThinking`  = 上游**要不要思考**（写在每轮请求体里）
    ///   · `ShowThinking`      = 思考出来了**给不给你看**（纯本地显示 + 入库开关）
    /// 关掉它不影响上游照常思考，只是界面回到「一行心跳」的老样子。
    ///
    /// **默认 true，与引擎侧 `ChatConfig.show_thinking` 一致。**
    /// </summary>
    public bool ShowThinking { get; set; } = true;

    // ---------- 当前对话与长期记忆 ----------
    //
    // 都是 `status` 快照里的**轻量**字段。列表与全文各有
    // `conversation.list` / `conversation.current` 负责 —— 状态轮询每 5 秒
    // 一次，不该把整条历史带上。

    /// <summary>当前对话的本地 id（引擎侧生成，形如 `20261015-195230-a3f9`）。</summary>
    public string ConversationId { get; set; } = "";

    /// <summary>当前对话标题（由首条用户消息自动生成，可改）。</summary>
    public string ConversationTitle { get; set; } = "";

    /// <summary>当前对话的用户轮次。</summary>
    public int ConversationTurns { get; set; }

    /// <summary>长期记忆的字数。0 = 还没有任何记忆。</summary>
    public int MemoryChars { get; set; }

    /// <summary>记忆是否启用（引擎侧 `memory.enabled`）。</summary>
    public bool MemoryEnabled { get; set; } = true;

    /// <summary>记忆文件路径，给设置页显示。</summary>
    public string MemoryPath { get; set; } = "";

    /// <summary>后台保活状态的最近一次说明（只在变化时更新，用来避免重复刷日志）。</summary>
    public string BackgroundNote { get; set; } = "";

    // ---------- 工具权限 ----------
    //
    // 引擎侧 `config.permissions` 的只读回显。真源在引擎，界面只显示与下发，
    // **不缓存一份自己的判断** —— 否则「界面显示可写、引擎按只读拒了」这种
    // 分叉一旦出现，用户看到的和实际生效的就对不上，而这种错最难自查。

    /// <summary>当前权限档位：`read_only` / `write` / `exec`。</summary>
    public string PermissionLevel { get; set; } = "exec";

    /// <summary>dry-run：打开后写类与执行类工具只回预览、不执行。</summary>
    public bool DryRun { get; set; }

    /// <summary>
    /// 引擎给的可选档位清单（含中文标签）。
    ///
    /// **界面不要硬编码三档名字** —— 引擎以后加档位只改一处，这里跟着变。
    /// 空清单时界面自己兜底（见 `SettingsDialog.ReloadAsync`），
    /// 这样引擎版本旧一点也不至于把下拉框做成空白。
    /// </summary>
    public List<PermissionLevelOption> PermissionLevels { get; } = new();
}

/// <summary>权限档位的一项（`id` 是引擎认的值，`label` 是给用户看的中文）。</summary>
public sealed class PermissionLevelOption
{
    public string Id { get; init; } = "";
    public string Label { get; init; } = "";

    /// <summary>下拉框直接绑这个 —— `ComboBox` 默认调 `ToString()`。</summary>
    public override string ToString() => Label.Length > 0 ? Label : Id;
}

/// <summary>对话列表里的一条。</summary>
public sealed class ConversationSummary
{
    public string Id { get; init; } = "";
    public string Title { get; init; } = "";
    public bool Active { get; init; }
    public int TurnCount { get; init; }
    public string Preview { get; init; } = "";
    public DateTimeOffset UpdatedAt { get; init; }

    /// <summary>列表里显示的一行副标题：轮次 + 时间。</summary>
    public string Subtitle
    {
        get
        {
            var when = UpdatedAt == default
                ? ""
                : UpdatedAt.LocalDateTime.ToString("MM-dd HH:mm");
            var turns = TurnCount > 0 ? $"{TurnCount} 轮" : "空对话";
            return when.Length > 0 ? $"{turns} · {when}" : turns;
        }
    }

    public string PreviewLine =>
        string.IsNullOrWhiteSpace(Preview) ? "（还没有消息）" : Preview;
}

/// <summary>
/// 侧栏「网页版」列表里的一条 —— 一条**服务端**会话。
///
/// 它和 <see cref="ConversationSummary"/> 的区别是「来源」：后者是本地存着的
/// 对话，前者是账号下的服务端会话（可能是用户在 DeepSeek 网页版里聊出来的，
/// 本地此前完全不知道）。`Bound` 表示这条远端会话已经被接管成了本地对话。
/// </summary>
public sealed class RemoteSession
{
    /// <summary>服务端 `chat_session_id`。接管时用它。</summary>
    public string RemoteId { get; init; } = "";
    public string Title { get; init; } = "";

    /// <summary>已接管时对应的本地对话 id（未接管为空）。</summary>
    public string LocalId { get; init; } = "";
    public bool Bound { get; init; }
    public bool Active { get; init; }
    public DateTimeOffset UpdatedAt { get; init; }

    /// <summary>列表里的一行副标题：接管状态 + 最后活动时间。</summary>
    public string Subtitle
    {
        get
        {
            var when = UpdatedAt == default
                ? ""
                : UpdatedAt.LocalDateTime.ToString("MM-dd HH:mm");
            var state = Bound ? "已接管" : "未接管";
            return when.Length > 0 ? $"{state} · {when}" : state;
        }
    }

    public string PreviewLine => Bound
        ? "已在本地 · 点一下切过去继续"
        : "点一下接管这条会话，继续聊";
}

/// <summary>一个可选模型。`Name` 是发给上游的名字，空串表示「账号默认」。</summary>
public sealed class ModelOption
{
    public string Name { get; init; } = "";
    public string DisplayName { get; init; } = "";
    public string Description { get; init; } = "";
    public bool IsAvailable { get; init; } = true;

    public override string ToString() => DisplayName;
}

/// <summary>
/// 模型列表查询结果。
/// `Ready` 表示引擎确实有运行时、这个列表是权威的；为 false 时界面应当
/// 稍后再问一次，而不是把「暂时没有」当成「本档位不支持选模型」。
/// </summary>
public sealed record ModelCatalog(
    IReadOnlyList<ModelOption> Models, bool Ready, string Current);

/// <summary>
/// 一个可选的传输档位。
///
/// `Available` 与 `Ready` 是**两个不同的问句**，别合并：
///   · Available —— 这台机器上跑得起来吗？（依赖装没装）
///   · Ready     —— 按**当前配置**能不能连上？（cookie 在不在 / 有没有 token）
///
/// 只有 Available=false 才该置灰。Ready=false 是**正常状态**：用户选它，
/// 然后去补凭据 —— 那正是切档位的意义。把两者合成一个布尔会让界面在
/// 「装了但还没配」时把选项锁死，用户反而没法进入配置流程。
/// </summary>
public sealed class TransportOption
{
    public string Name { get; init; } = "";
    public string Label { get; init; } = "";
    public bool Available { get; init; } = true;
    public bool Ready { get; init; } = true;
    public bool Guest { get; init; }
    public bool HasCookie { get; init; }
    public string Note { get; init; } = "";

    public override string ToString() => Label;
}

/// <summary>档位清单 + 当前档位。`Ready=false` 表示引擎没连上，清单不可信。</summary>
public sealed record TransportCatalog(
    IReadOnlyList<TransportOption> Items, bool Ready, string Current);

/// <summary>
/// 共享会话层：引擎生命周期、事件分发、状态轮询、任务提交、账号切换，
/// 全部收敛在这里，界面只是它的一个视图。
///
/// 为什么要有这一层：经典版和优化版是两套完全不同的视觉，但必须共用
/// 同一个引擎会话 —— 否则切一次界面就丢一次上下文、重启一次引擎。
/// 现在两套界面都从 `Transcript` / `Monitor` / `Status` 读数据，
/// 新开一个窗口只要先渲染一遍历史、再订阅增量事件，就能无缝接上。
///
/// 线程约定：所有对外事件都在 UI 线程上触发；引擎的原始事件来自后台
/// 读线程，由本类统一 `BeginInvoke` 归拢。
/// </summary>
public sealed class AgentHost : IAsyncDisposable
{
    public const int MaxTranscript = 600;
    public const int MaxMonitor = 400;

    /// <summary>
    /// 重放分页的**字数**预算（每批）。这是唯一真正决定排版成本的量。
    ///
    /// 2026-10-18 的教训，两次都被真实数据打回来：
    ///
    /// **第一次**用「50 块」当单位。依据是 `--perfcheck` 探针 ⑤b 里
    /// 每块 358 字的合成消息 —— 50 块约 150 ms，看起来是个漂亮的交点。
    /// 但真实对话里前 50 条合计 **246630 字**，单条最长 **40887 字**，
    /// 同样 50 块实测 **1575.85 ms 排版**。单位选错，差 10 倍。
    ///
    /// **第二次**才是现在这版：按字数切。排版成本正比于字数（换行、
    /// 字形度量、文本布局），与「几个控件」几乎无关。
    ///
    /// 取值依据是**三档实测**（2026-10-18，样本 724 条 / 1624 KB / 82 万字，
    /// 真窗口 1220×780，`--replaycheck` 同数据对照）：
    ///
    /// | 预算 | 批次数 | 首屏 | 总耗时 |
    /// |---|---|---|---|
    /// | 8000  | 135 |  50 ms | 7416 ms |
    /// | 20000 |  62 | 100 ms | 7075 ms |
    /// | 40000 |  29 |  97 ms | 6875 ms |
    /// | 一次性 |  — | 3525 ms | 3525 ms |
    ///
    /// 两条读出来的结论：
    ///
    /// **一、批次越少总耗时越低，但降幅远小于批次降幅。** 批次 135→29
    /// 少了 78%，总耗时只降 7%。说明每批的固定开销**不是常数**，而是
    /// 随「当前已铺多少」增长 —— 每批都要重新布局整棵树，成本约
    /// `已铺量 × 批次数`。分页的代价是 O(n²/批大小)，不是 O(n)。
    /// 所以「切更细」换不来总时间，只能换来更低的首次延迟。
    ///
    /// **二、首屏在 40000 这档仍在 100 ms 内**，比 8000 档的 50 ms 只多
    /// 一倍，而批次少 78%。100 ms 是人眼「瞬时」的边界，40000 是
    /// 首屏预算与批次开销的最优交点。
    ///
    /// **它救不了单条超长消息**：样本里有一条 40887 字，它自己一批
    /// （见 `ReplayBatchEnd` 的「至少收一条」）。这是本方案的下限，
    /// 不是 bug —— 再往下切要把单条消息的文本渲染拆段，属另一个量级的工程。
    /// </summary>
    public const int ReplayCharBudget = 40000;

    /// <summary>
    /// 每批的**块数**上限。防的是另一种极端：几百条「嗯」「好」这种极短消息
    /// 合起来字数不多，但每块都有固定开销（模板实例化、边距、样式解析）。
    /// 只按字数切会让这一批塞进几百个控件。
    /// </summary>
    public const int ReplayBatchSize = 50;

    /// <summary>重放分页的代号。每次 <see cref="ReplayConversation"/> 自增，旧任务据此自我作废。</summary>
    private int _replayGen;

    /// <summary>尚未铺完的重放行（拍平后的快照）；全部铺完即置空。</summary>
    private List<(TranscriptKind Kind, string Text, string Thoughts)>? _replayRows;

    /// <summary>
    /// 与 <see cref="_replayRows"/> 等长的字数表（正文 + 思考）。
    /// 单独存一份而不是每批现算 `row.Text.Length`：切批函数要反复读它，
    /// 而 `string.Length` 是 O(1) 但会让切批逻辑与「行长什么样」耦合 ——
    /// 抽成纯数字表之后，`ReplayBatchEnd` 能脱离 `TranscriptKind` 单测。
    /// </summary>
    private int[]? _replayChars;

    /// <summary>
    /// 本次重放使用的字数预算。存字段而不是靠参数往下传：
    /// `PumpReplay` 是排队执行的（`BeginInvoke`），参数只能在排队那一刻定死，
    /// 而后续每一批都要读同一个值。
    /// </summary>
    private int _replayCharBudget = ReplayCharBudget;

    /// <summary>本次重放的块数上限。理由同 <see cref="_replayCharBudget"/>。</summary>
    private int _replayBatchSize = ReplayBatchSize;

    /// <summary>下一次补块从 <see cref="_replayRows"/> 的哪个下标开始。</summary>
    private int _replayNext;

    private readonly Dispatcher _ui;
    private readonly ObservableCollection<TranscriptItem> _transcript = new();
    private readonly ObservableCollection<LogItem> _monitor = new();
    private readonly ObservableCollection<ConversationSummary> _conversations = new();
    private readonly ObservableCollection<RemoteSession> _remoteSessions = new();
    private readonly DispatcherTimer _statusTimer;

    private TranscriptItem? _streaming;

    // 增量匀速队列：平时（队列空）直吐零延迟，只有 burst 才进队列摊平。
    // 定时器只在有积压时才转，吐空即停 —— 不存在常驻 tick。
    private readonly DeltaPacer _pacer = new();
    private DispatcherTimer? _paceTimer;
    private TranscriptItem? _thinking;
    /// <summary>当前这轮的「思考过程」块（`chat.show_thinking=true` 时才有）。</summary>
    private TranscriptItem? _thought;

    /// <summary>
    /// 上一条「本轮结束」摘要的**文本**，用来按内容去重。
    ///
    /// 为什么需要它：一次任务里引擎会发**两条**结束事件，而且带的是同一份
    /// report —— `run_end`（`agent.run()` 返回前，引擎自己发）与
    /// `run.completed`（RPC 层在 `run.async` 的后台任务返回后补发）。
    /// 两条都画摘要的话，界面上「本轮结束：… N 次工具调用」会连着出现两次。
    /// 详见下面那个 case 里的注释。
    /// </summary>
    private string _lastRunSummary = "";

    private int _seq;
    private EngineClient? _engine;

    /// <summary>
    /// 朗读后端配置。**懒加载**：只有用户真的点朗读时才读盘。
    ///
    /// 不在构造时读的理由：绝大多数会话根本不朗读，而构造是开窗路径上
    /// 最敏感的环节（晚 10ms 用户就能感觉到）。配置里有 API key，
    /// 读盘失败也只是「朗读不可用」，不该影响主流程。
    /// </summary>
    private TtsConfig? _ttsCfg;

    /// <summary>「接力新会话」是否正在跑。它要联网（可能十几秒），
    /// 用它挡住重复点击 —— 连点两次会真的压两次摘要、换两次会话。</summary>
    private bool _handoffBusy;

    // ---------- 工具调用 ----------
    //
    // 引擎按 `seq` 发三条事件（start / progress / done），界面靠它把三条
    // 对到**同一张卡片**上，而不是每来一条就往对话流里插一行。
    private readonly Dictionary<int, TranscriptItem> _toolBySeq = new();

    /// <summary>当前正在执行的工具调用序号（0 = 没有）。
    /// 用来把执行器的实时输出行归到这张卡片上。</summary>
    private int _activeToolSeq;

    /// <summary>正在执行的那次调用的实时输出缓冲（有上限，避免长命令吃内存）。</summary>
    private readonly System.Text.StringBuilder _liveOutput = new();
    // 实时输出到上限后只追加一次截断说明，不再静默冻结（以前超 4000 字就停更，
    // 命令还在跑、卡片却不动，看着像卡死）。最终全文以 tool_done 为准。
    private bool _liveCapped;
    private const int MaxLiveOutputChars = 20000;

    public AgentHost(Dispatcher ui)
    {
        _ui = ui;
        _statusTimer = new DispatcherTimer(DispatcherPriority.Background, ui)
        {
            Interval = TimeSpan.FromSeconds(5),
        };
        _statusTimer.Tick += async (_, _) => await RefreshStatusAsync();
    }

    // ---------- 只读视图 ----------

    public ObservableCollection<TranscriptItem> Transcript => _transcript;
    public ObservableCollection<LogItem> Monitor => _monitor;
    public HostStatus Status { get; } = new();
    public bool Running { get; private set; }
    public bool EngineStarted => _engine is not null;
    public string PythonPath => _engine?.PythonPath ?? "";
    public string StartupError { get; private set; } = "";

    /// <summary>
    /// 当前正在流式追加的那条助手消息。
    /// 视图靠它区分「这一条会继续长」与「这一条已经定稿」——
    /// 窗口在半路接上来时，重放里也能认出它，从而不会重复画一块。
    /// </summary>
    public TranscriptItem? Streaming => _streaming;

    // ---------- 事件（均在 UI 线程） ----------

    public event Action<TranscriptItem>? ItemAdded;
    public event Action<TranscriptItem>? ItemChanged;

    /// <summary>对话流超过上限、最旧的一条被裁掉。视图据此删掉对应控件。
    ///
    /// **为什么必须单独开一个事件**（与 <see cref="TranscriptCleared"/> 同源）：
    /// `TrimTranscript()` 只动 `_transcript`，不产生任何 `ItemAdded`。
    /// WPF 壳的视图自己维护了控件列表，能靠数量对齐；**Web 壳不能** ——
    /// 前端 `onItem` 是纯 Append，从不删节点。结果是超长对话里数据层
    /// 稳在 600 条、DOM 却涨到 740 个（实测），两边永久对不上。
    ///
    /// 裁掉的是**数据**，界面却还留着，用户往上翻能看到已经不存在的历史。
    /// 所以裁掉时必须把「裁了哪一条」明确广播出去，不能让视图自己猜。</summary>
    public event Action<TranscriptItem>? ItemRemoved;

    /// <summary>对话流被整体清空（切对话 / 重放前）。视图据此把 UI 里的旧气泡删掉。
    ///
    /// **为什么必须单独开一个事件**：`_transcript.Clear()` 只动数据模型，而视图
    /// 是「收到 `ItemAdded` 就往面板里追加控件」的 —— 清空**不产生任何
    /// `ItemAdded`**，视图因此完全收不到通知。结果是旧对话的气泡原地留着，
    /// 新对话的消息接着往后排。
    ///
    /// 实测症状：切换会话时记录越堆越多，所有会话混在同一个主窗口里。
    /// 数据层看着是对的（`_transcript` 确实清了），所以只看模型永远查不出来。</summary>
    public event Action? TranscriptCleared;
    public event Action<LogItem>? Logged;
    public event Action? StatusChanged;
    public event Action<bool>? RunningChanged;

    /// <summary>「刷新账号」静默刷新失败、且已确认必须人工登录。
    /// 界面据此把登录入口直接摆到用户面前，省掉他自己去找第二步。</summary>
    public event Action? IdentityNeedsLogin;

    /// <summary>本地对话列表变了（刷新 / 新建 / 改名 / 删除 / 切换后）。
    ///
    /// **为什么 WPF 壳没有它也能用**：WPF 侧是
    /// `LstConversations.ItemsSource = Host.Conversations`，
    /// `_conversations` 是 <see cref="ObservableCollection{T}"/>，增删会自己
    /// 通过绑定刷到界面上。WebView2 壳没有数据绑定这条通道，只有
    /// 「宿主主动推」这一种方式 —— 少了这个事件，列表就永远停在
    /// 页面加载那一刻的快照上（实测：启动时列表为空，之后一直空着，
    /// 而顶栏的会话标题却有值，看起来像是"列表坏了"）。
    /// 数据层完全正确，问题只在"没人告诉视图"。</summary>
    public event Action? ConversationsChanged;

    /// <summary>网页版会话列表变了。理由同 <see cref="ConversationsChanged"/>。</summary>
    public event Action? RemoteSessionsChanged;

    // ---------- 启动 / 停止 ----------

    public async Task StartAsync()
    {
        if (_engine is not null) return;
        try
        {
            var engine = EngineClient.Start(EngineClient.ResolveEngineRoot());
            engine.EngineEvent += OnEngineEvent;
            engine.Fault += OnEngineFault;
            _engine = engine;

            Log($"引擎已启动 · {engine.PythonPath}", LogLevel.Faint);

            var info = await engine.InitializeAsync();
            if (info.TryGetProperty("transport", out var transport))
                Status.Transport = transport.GetString() ?? "—";

            await engine.StartAsync();
            Add(TranscriptKind.Info, "引擎就绪。输入任务目标后按 Enter 发送，Shift+Enter 换行。");

            _statusTimer.Start();
            await RefreshStatusAsync(force: true);

            // 启动自检：**后台跑，不阻塞界面**。会发一次
            // `create_pow_challenge` —— 这是唯一能读到「账号被处置」的途径
            // （DeepSeek 没有账号状态接口）。不 await：网络慢的时候
            // 启动不该卡在这里；结果回来再刷一次状态。
            _ = ProbeIdentityOnStartAsync();
        }
        catch (Exception ex)
        {
            StartupError = ex.Message;
            Add(TranscriptKind.Error, $"启动失败：{ex.Message}");
            Log(ex.ToString(), LogLevel.Bad);
            StatusChanged?.Invoke();
        }
    }

    /// <summary>
    /// 启动自检：验证一次缓存凭据，把结果直接摆到界面上。
    ///
    /// **它能证明什么、不能证明什么**（2026-10-17 实测，别写错）：
    ///   · 通过 ⇒ token 有效、链路可达
    ///   · **不等于账号没被封** —— `create_pow_challenge` 不校验账号处置，
    ///     封禁只在 `completion` 响应里报 40003 + `suspended`。
    ///   所以这里成功时不宣称「账号正常」，只把状态刷成可用；真正的处置
    ///   状态由发送路径捕获后置位。
    ///
    /// **只发一次，失败不重试**。封禁期内反复探测会被判定为重试，有延长
    /// 处置的风险；这条路径的存在是为了「启动时看一眼凭据」，不是为了轮询。
    /// </summary>
    private async Task ProbeIdentityOnStartAsync()
    {
        var engine = _engine;
        if (engine is null || Status.IdentityRefreshing) return;

        Status.IdentityRefreshing = true;
        StatusChanged?.Invoke();
        try
        {
            var r = await engine.ProbeIdentityAsync();
            var ok = r.TryGetProperty("ok", out var o) && o.ValueKind == JsonValueKind.True;
            var detail = r.TryGetProperty("detail", out var d) ? d.GetString() : "";
            var suspended = r.TryGetProperty("suspended", out var sp) &&
                            sp.ValueKind == JsonValueKind.True;

            if (suspended)
            {
                Add(TranscriptKind.Error,
                    $"账号被服务端处置：{detail}\n" +
                    "封禁期内重新登录不会解除限制 —— 请等处置到期，不要反复重试。");
                Log($"[身份] 启动自检：账号被处置 · {detail}", LogLevel.Bad);
            }
            else if (ok)
            {
                Log("[身份] 启动自检通过", LogLevel.Ok);
            }
            else if (!string.IsNullOrWhiteSpace(detail) && !detail.Contains("没有缓存 token"))
            {
                // 没有 token 是常态（还没登录），不该当成故障刷一条提示。
                Add(TranscriptKind.Notice,
                    $"启动自检：凭据不可用（{detail}）。可以用「打开浏览器登录」重新取一次。");
                Log($"[身份] 启动自检失败：{detail}", LogLevel.Warn);
            }

            await RefreshStatusAsync(force: true);
        }
        catch (Exception ex)
        {
            // 自检失败不该挡住使用 —— 用户仍然可以点「刷新并验证」手动重试。
            Log($"[身份] 启动自检异常：{ex.Message}", LogLevel.Warn);
        }
        finally
        {
            Status.IdentityRefreshing = false;
            StatusChanged?.Invoke();
        }
    }

    public async ValueTask DisposeAsync()
    {
        _statusTimer.Stop();
        var engine = _engine;
        _engine = null;
        if (engine is not null) await engine.DisposeAsync();
    }

    // ---------- 状态 ----------

    /// <summary>
    /// 刷新状态快照。
    ///
    /// `force` 只在「状态真的可能变了」时才给 true —— 启动完成、切身份、
    /// 切模型、浏览器登录成功、任务结束。定时器那一跳**必须**是 false：
    /// 否则等于每 5 秒强制一次，配置里的 `status_poll_ms` 就成了死配置
    /// （间隔判断在引擎侧，外壳负责不要绕过它）。
    /// </summary>
    public async Task RefreshStatusAsync(bool force = false)
    {
        var engine = _engine;
        if (engine is null) return;
        try
        {
            ApplyStatus(await engine.StatusAsync(poll: force));
        }
        catch (Exception ex)
        {
            Log($"状态轮询失败：{ex.Message}", LogLevel.Warn);
        }
    }

    private void ApplyStatus(JsonElement status)
    {
        Status.Started = !status.TryGetProperty("started", out var st) ||
                         st.ValueKind != JsonValueKind.False;

        if (status.TryGetProperty("mode", out var modeEl))
            Status.Mode = modeEl.GetString() ?? "";

        // 身份快照（引擎侧 `identity.status` 的形状，**不含 token 本体**）。
        //
        // 必须有这一段：顶栏的「已登录 / 未登录」、身份对话框的当前身份、以及
        // 「要不要提示用户去登录」全靠它。缺了它这三个地方会一律显示「未登录」——
        // 明明已经能用了，界面却说没登录，是最误导人的一种坏法。
        if (status.TryGetProperty("identity", out var ident) &&
            ident.ValueKind == JsonValueKind.Object)
        {
            Status.IdentityLabel = GetString(ident, "account_label");
            Status.Impersonate = GetString(ident, "impersonate");
            Status.TokenFile = GetString(ident, "token_file");

            var tok = ident.TryGetProperty("token_len", out var tl) &&
                      tl.ValueKind == JsonValueKind.Number;
            Status.HasToken = ident.TryGetProperty("has_token", out var ht)
                ? ht.ValueKind == JsonValueKind.True
                : tok && tl.GetInt32() > 0;

            Status.IdentityViolations.Clear();
            if (ident.TryGetProperty("violations", out var vio) &&
                vio.ValueKind == JsonValueKind.Array)
                foreach (var v in vio.EnumerateArray())
                {
                    var s = v.GetString();
                    if (!string.IsNullOrWhiteSpace(s)) Status.IdentityViolations.Add(s);
                }

            Status.IdentityNotices.Clear();
            if (ident.TryGetProperty("notices", out var nts) &&
                nts.ValueKind == JsonValueKind.Array)
                foreach (var n in nts.EnumerateArray())
                {
                    var s = n.GetString();
                    if (!string.IsNullOrWhiteSpace(s)) Status.IdentityNotices.Add(s);
                }
        }

        if (status.TryGetProperty("transport", out var transport))
        {
            if (transport.ValueKind == JsonValueKind.String)
            {
                Status.Transport = transport.GetString() ?? "—";
            }
            else if (transport.TryGetProperty("ready", out var ready))
            {
                Status.TransportReady = ready.ValueKind != JsonValueKind.False;
                if (!Status.TransportReady) Status.Transport = "不可用";
            }

            // 后台保活状态（心跳 / 轮换）。**只在变化时记一行** ——
            // 机器特征最大的问题就是不可见：用户看不到程序空闲时还在按固定
            // 节拍联网，也就无从判断「省没省」。
            if (transport.ValueKind == JsonValueKind.Object &&
                transport.TryGetProperty("background", out var bg) &&
                bg.ValueKind == JsonValueKind.Object)
            {
                // 账号处置标记：每次快照都读，不依赖 note 是否变化 ——
                // 它是「能不能用」的硬状态，不能因为日志去重被跳过。
                Status.AccountSuspended =
                    bg.TryGetProperty("account_suspended", out var sus) &&
                    sus.ValueKind == JsonValueKind.True;
                Status.SuspendedDetail = GetString(bg, "suspended_detail");

                var note = GetString(bg, "note");
                if (note.Length > 0 && note != Status.BackgroundNote)
                {
                    Status.BackgroundNote = note;
                    var on = bg.TryGetProperty("heartbeat", out var hb) &&
                             hb.ValueKind == JsonValueKind.True;
                    Log($"[后台] 活跃心跳 {note}", on ? LogLevel.Soft : LogLevel.Ok);
                }
            }
        }

        if (status.TryGetProperty("circuit", out var circuit))
        {
            Status.CircuitLevel = GetString(circuit, "level", "?");
            Status.CircuitReason = GetString(circuit, "reason");

            // **DeepSeek 侧没有账号状态码 / abuse / quota 这三样遥测。**
            // gemini 那边靠它们做「事前刹车」，这边一个都没有（已知接口只有 3 个，
            // 全是业务接口）。能显示的只有**响应侧**的两项计数：
            //   · last_signal  —— 最近一次信号（auth / rate_limit / server_busy / …）
            //   · busy_streak  —— 连续「服务端忙」次数，到阈值会升成长冷却
            // 这是本档位唯一一个「出事之前」能拿到的信号。
            Status.LastSignal = GetString(circuit, "last_signal");
            Status.LastSignalAt = GetString(circuit, "last_signal_at");
            Status.Degrade = GetString(circuit, "degrade", "none");

            if (circuit.TryGetProperty("counters", out var counters) &&
                counters.ValueKind == JsonValueKind.Object)
            {
                Status.BusyStreak = GetInt(counters, "busy_streak");
                Status.AuthFailures = GetInt(counters, "auth_failures");
                Status.RpcErrors = GetInt(counters, "rpc_errors");
                Status.BizErrors = GetInt(counters, "biz_errors");
            }

            if (Status.CircuitLevel is "open" or "halted")
                Log($"[熔断 {Status.CircuitLevel}] {Status.CircuitReason}",
                    Status.CircuitLevel == "halted" ? LogLevel.Bad : LogLevel.Warn);
        }

        if (status.TryGetProperty("decision", out var decision))
        {
            Status.GateAllowed = decision.TryGetProperty("allowed", out var a) &&
                                 a.ValueKind == JsonValueKind.True;
            Status.GateReason = GetString(decision, "reason");
            Status.GateWaitMs = decision.TryGetProperty("wait_ms", out var w) &&
                                w.ValueKind == JsonValueKind.Number
                ? w.GetDouble()
                : 0;
        }

        if (status.TryGetProperty("pacing", out var pacing))
        {
            Status.BurstCount = GetInt(pacing, "burst_count");
            Status.MaxBurst = GetInt(pacing, "max_burst");
            Status.LastGapMs = pacing.TryGetProperty("last_gap_ms", out var g) &&
                               g.ValueKind == JsonValueKind.Number
                ? g.GetDouble()
                : 0;
            Status.InActiveWindow = pacing.TryGetProperty("in_active_window", out var aw) &&
                                    aw.ValueKind == JsonValueKind.True;

            Log($"[节奏] 突发 {Status.BurstCount}/{Status.MaxBurst} · " +
                $"上次间隔 {Math.Round(Status.LastGapMs)}ms · " +
                $"活跃时段 {(Status.InActiveWindow ? "是" : "否")}", LogLevel.Faint);
        }

        if (status.TryGetProperty("session", out var session))
        {
            Status.SessionCid = GetString(session, "cid");
            Status.SessionTurns = GetInt(session, "turns");
            Status.SessionMaxTurns = GetInt(session, "max_turns");
            // 默认与引擎一致：缺失字段按「一般对话」处理（false），
            // 避免旧快照让界面谎报成临时对话。下面 `chat` 块若有会再覆盖一次。
            Status.SessionTemporary = session.TryGetProperty("temporary", out var tmp) &&
                                      tmp.ValueKind == JsonValueKind.True;
            if (!string.IsNullOrEmpty(Status.SessionCid))
                Log($"[会话] {Short(Status.SessionCid)}… 轮次 " +
                    $"{Status.SessionTurns}/{Status.SessionMaxTurns}", LogLevel.Faint);
        }

        // 当前模型在顶层而不是 identity 里 —— 它是会话级设置，改它不重启引擎。
        if (status.TryGetProperty("model", out var modelEl) &&
            modelEl.ValueKind == JsonValueKind.String)
            Status.Model = modelEl.GetString() ?? "";

        // 会话形态也在顶层，理由同模型：一般/临时对话与思考模式都是 per-turn
        // 参数，改它们不重启引擎。这里以 `chat` 块为准（它比 `session.temporary`
        // 更直接地反映「下一轮会怎么发」）。
        if (status.TryGetProperty("chat", out var chat))
        {
            Status.SessionTemporary = chat.TryGetProperty("temporary", out var tempEl) &&
                                      tempEl.ValueKind == JsonValueKind.True;
            Status.ExtendedThinking = !chat.TryGetProperty("extended_thinking", out var thinkEl) ||
                                      thinkEl.ValueKind != JsonValueKind.False;
            // 联网搜索是 opt-in：只有明确 true 才算开（缺字段 = 关，与引擎默认一致）
            Status.SearchEnabled = chat.TryGetProperty("search_enabled", out var searchEl) &&
                                   searchEl.ValueKind == JsonValueKind.True;
            // 显示思考过程是 opt-out（缺字段 = 开，与引擎默认一致）。
            // 与 extended_thinking 同样是「缺字段算开」，因为两者的默认值都是 true。
            Status.ShowThinking = !chat.TryGetProperty("show_thinking", out var showEl) ||
                                  showEl.ValueKind != JsonValueKind.False;
        }

        // 当前对话（轻量摘要）—— 顶栏与侧栏的「当前对话」读它。
        if (status.TryGetProperty("conversation", out var conv) &&
            conv.ValueKind == JsonValueKind.Object)
        {
            Status.ConversationId = GetString(conv, "id");
            Status.ConversationTitle = GetString(conv, "title");
            Status.ConversationTurns =
                conv.TryGetProperty("turn_count", out var tc) &&
                tc.ValueKind == JsonValueKind.Number ? tc.GetInt32() : 0;
        }

        // 长期记忆状态（路径 + 字数 + 开关）。
        if (status.TryGetProperty("memory", out var mem) &&
            mem.ValueKind == JsonValueKind.Object)
        {
            Status.MemoryPath = GetString(mem, "path");
            Status.MemoryEnabled = !mem.TryGetProperty("enabled", out var en) ||
                                   en.ValueKind != JsonValueKind.False;
            Status.MemoryChars =
                mem.TryGetProperty("chars", out var ch) &&
                ch.ValueKind == JsonValueKind.Number ? ch.GetInt32() : 0;
        }

        // 工具权限（三档 + dry-run）。
        //
        // 缺字段时**保持旧值不动**，不回落成默认 —— 与 identity / circuit 那些
        // 块的写法一致。理由：旧版引擎没有这一段，若在这里兜成 `exec`，
        // 用户明明配了只读，一次状态轮询就会把界面刷回「可执行」，
        // 而引擎实际仍在按只读拒绝 —— 界面与生效值分叉，最难自查的一种错。
        if (status.TryGetProperty("permissions", out var perms) &&
            perms.ValueKind == JsonValueKind.Object)
        {
            var lv = GetString(perms, "level");
            if (lv.Length > 0) Status.PermissionLevel = lv;
            Status.DryRun = IsTrue(perms, "dry_run");

            // 档位清单每次重建：引擎是唯一真源，界面不保留自己的一份。
            if (perms.TryGetProperty("levels", out var lvs) &&
                lvs.ValueKind == JsonValueKind.Array)
            {
                Status.PermissionLevels.Clear();
                foreach (var it in lvs.EnumerateArray())
                {
                    var id = GetString(it, "id");
                    if (id.Length == 0) continue;
                    Status.PermissionLevels.Add(new PermissionLevelOption
                    {
                        Id = id,
                        Label = GetString(it, "label"),
                    });
                }
            }
        }

        StatusChanged?.Invoke();
    }

    // ---------- 对话与历史 ----------
    //
    // 全部是引擎侧的**本地文件操作** —— 不联网、不受熔断影响，
    // 所以失败原因只有「文件读写」，不会有网络类的模糊报错。
    // 界面需要的动作就五个：刷新列表 / 切过去 / 新建 / 改名 / 删除。

    /// <summary>对话列表（最近更新在前）。</summary>
    public ObservableCollection<ConversationSummary> Conversations => _conversations;

    /// <summary>账号下的服务端会话列表（最近更新在前）。只在用户点「网页版」时拉取。</summary>
    public ObservableCollection<RemoteSession> RemoteSessions => _remoteSessions;

    /// <summary>拉取对话列表。失败不抛 —— 侧栏不该因为一次查询失败就空掉。</summary>
    public async Task RefreshConversationsAsync()
    {
        var engine = _engine;
        if (engine is null) return;
        try
        {
            var res = await engine.ConversationListAsync();
            if (!res.TryGetProperty("items", out var items) ||
                items.ValueKind != JsonValueKind.Array) return;

            _conversations.Clear();
            foreach (var it in items.EnumerateArray())
            {
                _conversations.Add(new ConversationSummary
                {
                    Id = GetString(it, "id"),
                    Title = GetString(it, "title", "新对话"),
                    Active = it.TryGetProperty("active", out var ac) &&
                             ac.ValueKind == JsonValueKind.True,
                    TurnCount = it.TryGetProperty("turn_count", out var tc) &&
                                tc.ValueKind == JsonValueKind.Number ? tc.GetInt32() : 0,
                    Preview = GetString(it, "preview"),
                    UpdatedAt = ParseUnixSeconds(it, "updated_at"),
                });
            }

            ConversationsChanged?.Invoke();
        }
        catch (Exception ex)
        {
            Log($"[对话] 列表读取失败：{ex.Message}", LogLevel.Warn);
        }
    }

    /// <summary>新建对话并切过去。</summary>
    public async Task<bool> NewConversationAsync()
    {
        var engine = _engine;
        if (engine is null) return false;
        if (Running)
        {
            Add(TranscriptKind.Notice, "上一轮还在跑，等它出结果再新建对话");
            return false;
        }
        try
        {
            await engine.ConversationNewAsync();
            ClearTranscript();
            Add(TranscriptKind.Info,
                "已新建对话 —— 这里是一张白纸。上一条仍在左侧列表里，随时可以切回去。");
            await RefreshConversationsAsync();
            await RefreshStatusAsync(force: true);
            Log("[对话] 新建", LogLevel.Faint);
            return true;
        }
        catch (Exception ex)
        {
            Add(TranscriptKind.Error, $"新建对话失败：{ex.Message}");
            return false;
        }
    }

    /// <summary>切到某条对话：加载它、重放历史、刷新列表。</summary>
    public async Task<bool> SwitchConversationAsync(string id)
    {
        var engine = _engine;
        if (engine is null || string.IsNullOrWhiteSpace(id)) return false;
        if (Running)
        {
            Add(TranscriptKind.Notice, "上一轮还在跑，等它出结果再切换对话");
            return false;
        }
        if (id == Status.ConversationId)
        {
            // 已经在这条上。仍然重放一次 —— 视图可能是刚打开的新窗口。
            await ReplayCurrentAsync();
            return true;
        }
        try
        {
            var sw = System.Diagnostics.Stopwatch.StartNew();
            var conv = await engine.ConversationLoadAsync(id);
            var loadMs = sw.ElapsedMilliseconds;
            ReplayConversation(conv);
            var replayMs = sw.ElapsedMilliseconds - loadMs;
            await RefreshConversationsAsync();
            await RefreshStatusAsync(force: true);
            sw.Stop();
            Log($"[对话] 切换到 {GetString(conv, "title", id)}" +
                $"（读盘 {loadMs}ms · 重放 {replayMs}ms · 列表状态 {sw.ElapsedMilliseconds - loadMs - replayMs}ms）",
                LogLevel.Faint);
            // 先本地切换（瞬时），再**后台**从服务端拉真实历史。
            // 刻意不 await：切对话本身不该被一次网络往返挡住。
            _ = SyncInBackgroundAsync(id);
            return true;
        }
        catch (Exception ex)
        {
            Add(TranscriptKind.Error, $"切换对话失败：{ex.Message}");
            return false;
        }
    }

    /// <summary>
    /// 后台从服务端同步一条对话的历史。
    ///
    /// **为什么是后台**：`conversation.load` 是纯本地文件操作（零延迟），
    /// 而 `conversation.sync` 要一次网络往返。切对话本来就该是瞬时的，
    /// 把它和网络绑在一起会让「点一下列表」出现可感知的卡顿。
    ///
    /// **失败不打扰用户** —— 本地历史仍然可用，同步只是让它更完整
    /// （补上在网页版里聊过的内容、校正过期的续接锚点）。
    /// </summary>
    private async Task SyncInBackgroundAsync(string id)
    {
        var engine = _engine;
        if (engine is null) return;
        try
        {
            var result = await engine.ConversationSyncAsync(id);
            if (!IsTrue(result, "synced"))
            {
                Log($"[对话] 未同步：{GetString(result, "reason", "未知原因")}", LogLevel.Faint);
                return;
            }

            // 同步期间用户可能已经切走了 —— 那就只刷新列表，**绝不能**
            // 拿这条对话的数据去覆盖当前视图（那会显示成另一条对话的内容）。
            if (id != Status.ConversationId)
            {
                await RefreshConversationsAsync();
                return;
            }

            if (result.TryGetProperty("conversation", out var conv) &&
                conv.ValueKind == JsonValueKind.Object)
            {
                var before = GetInt(result, "local_before");
                var after = GetInt(result, "local_after");
                // 服务端没带来新东西就不再铺一遍：重放一次就是一次全量布局，
                // 无变化还铺等于白冻一次。锚点校正不影响显示（只影响下次发送），
                // 跳过重绘是安全的 —— 但必须留一行日志，否则“同步了却什么都没发生”
                // 看起来像同步没跑。
                if (!ShouldReplayAfterSync(before, after))
                {
                    await RefreshConversationsAsync();
                    Log($"[对话] 已从服务端同步 · 本地 {before} → {after} 条 · 无新增，跳过重绘",
                        LogLevel.Faint);
                    return;
                }
                var sw = System.Diagnostics.Stopwatch.StartNew();
                ReplayConversation(conv);
                sw.Stop();
                await RefreshConversationsAsync();
                Log($"[对话] 已从服务端同步 · 远端 {GetInt(result, "remote_count")} 条 · " +
                    $"本地 {before} → {after} 条 · 重放 {sw.ElapsedMilliseconds}ms",
                    LogLevel.Faint);
            }
            else
            {
                await RefreshConversationsAsync();
                Log($"[对话] 已从服务端同步 · 远端 {GetInt(result, "remote_count")} 条 · " +
                    $"本地 {GetInt(result, "local_before")} → {GetInt(result, "local_after")} 条",
                    LogLevel.Faint);
            }
        }
        catch (Exception ex)
        {
            // 同步不是用户主动发起的操作，失败不该弹错。
            Log($"[对话] 服务端同步失败：{ex.Message}", LogLevel.Warn);
        }
    }

    /// <summary>手动触发一次服务端同步（给界面的「同步」入口用）。</summary>
    public async Task<bool> SyncConversationAsync(string id)
    {
        var engine = _engine;
        if (engine is null || string.IsNullOrWhiteSpace(id)) return false;
        if (Running)
        {
            Add(TranscriptKind.Notice, "上一轮还在跑，等它出结果再同步");
            return false;
        }
        await SyncInBackgroundAsync(id);
        return true;
    }

    /// <summary>
    /// 接力换会话：把当前对话的上下文压成摘要，开一条新会话继续。
    ///
    /// 这是**用户主动**的换会话动作 —— 自动轮换默认已关，所以它是现在唯一的
    /// 换法。三件事缺一不可：
    ///   ① 挡住重复点击（要联网，可能十几秒；连点会压两次摘要、换两次会话）
    ///   ② 跑完把**新对话**重放到视图 —— 否则界面还停在旧对话上，而用户下一条
    ///      消息其实已经进了新会话，界面完全看不出来
    ///   ③ 把「原对话还在列表里」说清楚 —— 否则用户会以为刚才那段被删了
    /// </summary>
    public async Task<bool> HandoffConversationAsync(string mode = "")
    {
        var engine = _engine;
        if (engine is null) return false;
        if (Running)
        {
            Add(TranscriptKind.Notice, "上一轮还在跑，等它出结果再接力");
            return false;
        }
        if (_handoffBusy) return false;
        _handoffBusy = true;
        Add(TranscriptKind.Info, "正在压缩当前上下文，准备换到新会话…");
        try
        {
            var res = await engine.ConversationHandoffAsync(mode);
            if (!IsTrue(res, "ok"))
            {
                var reason = GetString(res, "reason", "未知原因");
                Add(TranscriptKind.Error, $"接力失败：{reason}");
                Log($"[接力] 失败：{reason}", LogLevel.Warn);
                return false;
            }

            if (res.TryGetProperty("conversation", out var conv) &&
                conv.ValueKind == JsonValueKind.Object)
            {
                ReplayConversation(conv);
            }
            await RefreshConversationsAsync();
            await RefreshStatusAsync(force: true);

            var used = GetString(res, "mode", "local");
            var chars = GetInt(res, "chars");
            var by = used == "model" ? "模型生成" : "本地规则生成";
            Add(TranscriptKind.Notice,
                $"已接力到新会话 · 摘要 {chars} 字（{by}）。" +
                "之前聊过的内容已经带过来了，直接接着说要做什么就行；" +
                "原对话仍在左侧列表里，随时能切回去。");
            var note = GetString(res, "note");
            if (note.Length > 0) Add(TranscriptKind.Notice, note);
            Log($"[接力] 已换到新会话 · 摘要 {chars} 字 · {used}", LogLevel.Ok);
            return true;
        }
        catch (Exception ex)
        {
            Add(TranscriptKind.Error, $"接力失败：{ex.Message}");
            Log($"[接力] 失败：{ex.Message}", LogLevel.Warn);
            return false;
        }
        finally
        {
            _handoffBusy = false;
        }
    }

    // ---------- 网页版会话（服务端历史）----------
    //
    // 本地列表只看得见本客户端建的对话。账号下可能还有一堆用户在 DeepSeek
    // 网页版里聊出来的会话 —— 这一组把它们拉出来，并允许「接管」其中一条
    // （拉全量历史 + 校正续接锚点），此后发消息就进那条会话。

    /// <summary>
    /// 读取账号下的服务端会话列表（**联网**）。
    ///
    /// 失败不抛：读不到会话列表不是致命错误，把原因写进对话流 + 监视器即可。
    /// 返回是否成功，供界面决定提示文案。
    /// </summary>
    public async Task<bool> RefreshRemoteSessionsAsync(int count = 50)
    {
        var engine = _engine;
        if (engine is null) return false;
        try
        {
            var res = await engine.ConversationRemoteListAsync(count);
            var err = GetString(res, "error");
            if (err.Length > 0)
            {
                Add(TranscriptKind.Error, $"读取网页版会话失败：{err}");
                Log($"[会话] 远端列表读取失败：{err}", LogLevel.Warn);
                return false;
            }

            _remoteSessions.Clear();
            if (res.TryGetProperty("items", out var items) &&
                items.ValueKind == JsonValueKind.Array)
            {
                foreach (var it in items.EnumerateArray())
                {
                    var remoteId = GetString(it, "remote_id");
                    if (remoteId.Length == 0) continue;
                    _remoteSessions.Add(new RemoteSession
                    {
                        RemoteId = remoteId,
                        Title = GetString(it, "title", "（无标题）"),
                        LocalId = GetString(it, "local_id"),
                        Bound = it.TryGetProperty("bound", out var b) &&
                                b.ValueKind == JsonValueKind.True,
                        Active = it.TryGetProperty("active", out var a) &&
                                 a.ValueKind == JsonValueKind.True,
                        UpdatedAt = ParseUnixSeconds(it, "updated_at"),
                    });
                }
            }
            Log($"[会话] 读取到 {_remoteSessions.Count} 条网页版会话", LogLevel.Ok);
            RemoteSessionsChanged?.Invoke();
            return true;
        }
        catch (Exception ex)
        {
            Add(TranscriptKind.Error, $"读取网页版会话失败：{ex.Message}");
            Log($"[会话] 远端列表读取失败：{ex.Message}", LogLevel.Warn);
            return false;
        }
    }

    /// <summary>
    /// 删除一条**服务端**会话（网页版历史里的那条）。**不可逆，会联网**。
    ///
    /// 调用方（界面）必须先弹二次确认 —— 这里不做交互，只做执行。
    ///
    /// 删成功后引擎会把本地绑定一并解开。若被删的正是当前对话绑定的那条，
    /// 本地对话会退回「独立会话」：**界面必须跟着刷新**，否则顶栏还挂着
    /// 一个已经失效的 cid，用户下一句发出去就会撞 `invalid chat session id`。
    /// </summary>
    public async Task<bool> DeleteRemoteSessionAsync(string remoteId)
    {
        var engine = _engine;
        if (engine is null || string.IsNullOrWhiteSpace(remoteId)) return false;
        if (Running)
        {
            Add(TranscriptKind.Notice, "上一轮还在跑，等它出结果再删除会话");
            return false;
        }
        try
        {
            var res = await engine.ConversationRemoteDeleteAsync(remoteId);
            if (!IsTrue(res, "deleted"))
            {
                var reason = GetString(res, "reason", "未知原因");
                Add(TranscriptKind.Error, $"删除网页版会话失败：{reason}");
                Log($"[会话] 远端删除失败：{reason}", LogLevel.Warn);
                return false;
            }

            var unbound = IsTrue(res, "unbound");
            var localId = GetString(res, "local_id");
            await RefreshRemoteSessionsAsync();
            await RefreshConversationsAsync();
            await RefreshStatusAsync(force: true);

            // 解绑的若是当前对话，引擎那边的对象已经清了 cid ——
            // 界面也得重放一次，否则顶栏/状态条还停在旧值上。
            if (unbound && localId.Length > 0 && localId == Status.ConversationId)
                await ReplayCurrentAsync();

            Add(TranscriptKind.Info, unbound
                ? "已从账号里删除该会话；本地对话已解绑，之后发的消息会新建一条会话。"
                : "已从账号里删除该会话。");
            Log($"[会话] 已删除远端会话 · {remoteId}" + (unbound ? " · 本地已解绑" : ""),
                LogLevel.Ok);
            return true;
        }
        catch (Exception ex)
        {
            Add(TranscriptKind.Error, $"删除网页版会话失败：{ex.Message}");
            Log($"[会话] 远端删除失败：{ex.Message}", LogLevel.Warn);
            return false;
        }
    }

    /// <summary>
    /// 当前对话的 `chat_session_id` 是否属于**现在登录的账号**。
    ///
    /// 为什么需要这一步：本地对话文件不记录归属账号，换号后引擎仍会恢复上次
    /// 打开的那条 —— 顶栏显示着上个账号的标题，用户在里面发消息时，引擎会拿
    /// 新 token 去请求一个不属于它的会话，服务端必然拒绝（同类场景实测：
    /// HTTP 400 + 空 body，界面只显示「未收到正文」）。这类请求在风控眼里
    /// 还是「跨账号访问他人会话」的异常模式，所以启动时就把这种对话换掉，
    /// 不留给用户踩。
    ///
    /// 判据用**账号下的会话列表**：在里面 = 属于当前账号；不在 = 属于别的
    /// 账号，或已被用户在网页版删除 —— 两种都不该继续用。
    /// </summary>
    public async Task<bool> EnsureCurrentSessionBelongsToAccountAsync()
    {
        var engine = _engine;
        if (engine is null) return false;

        var cid = Status.SessionCid;
        if (string.IsNullOrEmpty(cid)) return true;      // 当前是空对话，无需处理
        foreach (var r in _remoteSessions)
        {
            if (r.RemoteId == cid) return true;
        }

        Log($"[会话] 当前 {Short(cid)}… 不在账号列表内 → 新建对话", LogLevel.Warn);
        try
        {
            await engine.ConversationNewAsync();
            ClearTranscript();
            Add(TranscriptKind.Notice,
                "上次打开的对话不属于当前账号（或已在网页版删除），已切到新对话。" +
                "旧对话仍留在本地缓存里，但不能再往里发消息 —— 发出去只会被服务端拒绝。");
            await RefreshConversationsAsync();
            await RefreshStatusAsync(force: true);
            return true;
        }
        catch (Exception ex)
        {
            Add(TranscriptKind.Error, $"切换到新对话失败：{ex.Message}");
            return false;
        }
    }

    /// <summary>
    /// 接管一条服务端会话：拉全量历史 + 校正续接锚点，并切过去。
    ///
    /// 接管之后，用户在这个对话里发的消息就会带那条 `chat_session_id` 发出去 ——
    /// 这就是「确保我现在的输入是进去那个会话」。引擎侧会把它标记为
    /// **不自动轮换**（见 `SessionMeta.attached`），否则接管的往往是一条已经
    /// 聊了很多轮的长会话，第一条消息就会被轮换进一个空白新会话。
    /// </summary>
    public async Task<bool> AttachSessionAsync(string remoteId)
    {
        var engine = _engine;
        if (engine is null || string.IsNullOrWhiteSpace(remoteId)) return false;
        if (Running)
        {
            Add(TranscriptKind.Notice, "上一轮还在跑，等它出结果再接管会话");
            return false;
        }
        try
        {
            var res = await engine.ConversationAttachAsync(remoteId);
            if (!IsTrue(res, "attached"))
            {
                Add(TranscriptKind.Error, $"接管会话失败：{GetString(res, "reason", "未知原因")}");
                Log($"[会话] 接管失败：{GetString(res, "reason")}", LogLevel.Warn);
                return false;
            }

            var reused = IsTrue(res, "reused");
            if (res.TryGetProperty("conversation", out var conv) &&
                conv.ValueKind == JsonValueKind.Object)
            {
                ReplayConversation(conv);
            }
            await RefreshConversationsAsync();
            await RefreshRemoteSessionsAsync();
            await RefreshStatusAsync(force: true);

            if (reused)
            {
                Add(TranscriptKind.Info, "这条网页版会话已经接管过 —— 已切过去。");
                Log($"[会话] 已接管（复用）· {remoteId}", LogLevel.Faint);
            }
            else
            {
                Add(TranscriptKind.Info,
                    "已接管这条网页版会话：完整历史已拉回，续接锚点已校正。" +
                    "之后你发的消息就进这条会话（不自动轮换）。");
                Log($"[会话] 已接管 · {remoteId} · " +
                    $"远端 {GetInt(res, "remote_count")} 条", LogLevel.Ok);
            }
            return true;
        }
        catch (Exception ex)
        {
            Add(TranscriptKind.Error, $"接管会话失败：{ex.Message}");
            Log($"[会话] 接管失败：{ex.Message}", LogLevel.Warn);
            return false;
        }
    }

    /// <summary>重命名某条对话。</summary>
    public async Task<bool> RenameConversationAsync(string id, string title)
    {
        var engine = _engine;
        if (engine is null) return false;
        try
        {
            await engine.ConversationRenameAsync(id, title);
            await RefreshConversationsAsync();
            await RefreshStatusAsync(force: true);
            return true;
        }
        catch (Exception ex)
        {
            Add(TranscriptKind.Error, $"重命名失败：{ex.Message}");
            return false;
        }
    }

    /// <summary>删除某条对话。</summary>
    public async Task<bool> DeleteConversationAsync(string id)
    {
        var engine = _engine;
        if (engine is null) return false;
        if (Running)
        {
            Add(TranscriptKind.Notice, "上一轮还在跑，等它出结果再删除");
            return false;
        }
        var wasActive = id == Status.ConversationId;
        try
        {
            var res = await engine.ConversationDeleteAsync(id);
            var deleted = res.TryGetProperty("deleted", out var d) &&
                          d.ValueKind == JsonValueKind.True;
            await RefreshConversationsAsync();
            await RefreshStatusAsync(force: true);
            // 删掉的若是当前对话，引擎已经切走了 —— 视图**必须**跟着换。
            // 不换的话界面会停在一条已经不存在的对话上，用户下一次发言
            // 其实写进了另一条对话，而界面上完全看不出来。
            if (deleted && wasActive)
                await ReplayCurrentAsync();
            else if (deleted)
                Add(TranscriptKind.Notice, "已删除该对话。");
            return deleted;
        }
        catch (Exception ex)
        {
            Add(TranscriptKind.Error, $"删除对话失败：{ex.Message}");
            return false;
        }
    }

    /// <summary>重新拉当前对话并重放。</summary>
    public async Task ReplayCurrentAsync()
    {
        var engine = _engine;
        if (engine is null) return;
        try
        {
            ReplayConversation(await engine.ConversationCurrentAsync());
        }
        catch (Exception ex)
        {
            Log($"[对话] 重放失败：{ex.Message}", LogLevel.Warn);
        }
    }

    /// <summary>
    /// 把一条对话的消息重放进对话流。
    ///
    /// **只画 user / assistant / error 三种**：工具调用、闸门提示、流式过程
    /// 都是「过程」，历史里只保留「说了什么、回了什么」。否则切一次对话会
    /// 重放出一大堆过期的执行细节，把真正的对话淹掉。
    ///
    /// 唯一的例外是**思考过程**：它也被当成「过程」在直播时折叠着留在流里，
    /// 那切回来时就不能只剩回答 —— 否则用户会以为「思考只在当时看得到」。
    /// 它以折叠态重放，不抢回答的位置。
    /// </summary>
    public void ReplayConversation(JsonElement conv) => ReplayConversation(conv, ReplayCharBudget);

    /// <summary>
    /// 同 <see cref="ReplayConversation(JsonElement)"/>，但可指定每批字数预算。
    ///
    /// 存在的唯一理由是**对照测量**：`--replaycheck` 要回答「分页把总耗时
    /// 推高了多少」，唯一可信的做法是拿同一份真实数据、同一个窗口、
    /// 同一次运行里跑两种预算 —— 传 <see cref="int.MaxValue"/> 即一次性铺完。
    /// 拿探针 ⑤ 的合成数据外推不行：那边 21 万字，真实样本 82 万字。
    ///
    /// 产品代码一律走无参重载，不传这个参数。
    /// </summary>
    public void ReplayConversation(JsonElement conv, int charBudget)
        => ReplayConversation(conv, charBudget, ReplayBatchSize);

    /// <summary>
    /// 完整重载：字数预算 + 块数上限都可指定。
    ///
    /// **两个上限都必须能放开**才能测「一次性铺完」。上一版只放开字数，
    /// 块数上限 50 还在 —— 对照跑出来只铺了 51 条，却自称「一次性铺完
    /// 600 条」，比不测更糟：它给出一个漂亮的小数字（1058ms），
    /// 而真正的基准根本没跑。这类「对照本身是假的」的错误，
    /// 报表上看不出任何异常，只有核 `Transcript.Count` 才能发现。
    /// </summary>
    public void ReplayConversation(JsonElement conv, int charBudget, int batchSize)
    {
        // 每次重放换一个代号。上一次的分页任务若还没跑完（用户在 1 秒内
        // 连切两次对话），它看到代号变了就自己停 —— 否则旧任务会把**上一条
        // 对话**的剩余消息继续铺进新对话里，而且因为都是 Append，界面上
        // 完全看不出串了哪一条。
        var gen = ++_replayGen;

        // **顺序不能反**：`ClearTranscript` 会把 `IsReplaying` 放开（它要让
        // 被打断的旧重放不至于把滚动永久锁死），所以必须先清、再置 true。
        // 反过来的话刚置上的 true 会被立刻抹掉，整段重放都在逐条滚动 ——
        // 正是「切对话冻几秒」的原形。
        ClearTranscript();
        IsReplaying = true;
        Add(TranscriptKind.Info, $"对话「{GetString(conv, "title", "新对话")}」");

        // 先把 JSON 拍平成轻量快照，再分页铺。
        //
        // 不直接持有 `JsonElement` 分页枚举的理由：`EnumerateArray` 的枚举器
        // 绑在 `JsonDocument` 的生命周期上，跨 Dispatcher 轮次持有它等于把
        // 一份可能几 MB 的文档钉在内存里直到重放跑完。拍平之后只剩
        // (kind, text, thoughts) 三元组，文档可以立刻回收。
        var rows = new List<(TranscriptKind Kind, string Text, string Thoughts)>();
        if (conv.TryGetProperty("messages", out var msgs) &&
            msgs.ValueKind == JsonValueKind.Array)
        {
            foreach (var m in msgs.EnumerateArray())
            {
                var text = GetString(m, "text");
                if (text.Length == 0) continue;
                switch (GetString(m, "role"))
                {
                    case "user":
                        rows.Add((TranscriptKind.Goal, text, ""));
                        break;
                    case "assistant":
                        rows.Add((TranscriptKind.Assistant, text, GetString(m, "thoughts")));
                        break;
                    case "error":
                        rows.Add((TranscriptKind.Error, text, ""));
                        break;
                    default:
                        rows.Add((TranscriptKind.Notice, text, ""));
                        break;
                }
            }
        }

        // ---- 首批同步铺，余下后台补 ----
        //
        // 实测（`--perfcheck` 探针 ⑤，2026-10-18）：600 块一次性铺
        // **1978.95 ms**，其中只构造不挂载只要 46.35 ms —— **97.7% 的成本
        // 在挂载 + 布局**，不在造控件。这个量级用户能明确感知（近 2 秒白屏）。
        //
        // 注意这里**没有减少任何总工作量**：600 块的总排版成本一分不少，
        // 只是被切成 12 段，每段之间让出 UI 线程。首屏从 1979 ms 降到
        // 约 150 ms（= 50 块的实测值），用户看到的是「界面立刻有内容，
        // 然后往下长」，而不是「点了没反应」。
        //
        // 别把它当成性能优化去别处找同类收益 —— 它是**感知延迟**的改造。
        // 与 rows 等长的字数表，切批只认它。
        var chars = new int[rows.Count];
        for (var i = 0; i < rows.Count; i++)
            chars[i] = rows[i].Text.Length + rows[i].Thoughts.Length;

        var first = ReplayBatchEnd(chars, 0, charBudget, batchSize);
        for (var i = 0; i < first; i++) AddReplayRow(rows[i]);
        if (first >= rows.Count)
        {
            FinishReplay(gen);
            return;
        }

        // 余下的行与游标存到字段上，而不是用局部闭包。
        //
        // 理由：**发送路径必须能同步催熟它**（见 FlushPendingReplay）。
        // 闭包只活在这次调用的栈上，外部无法触发，于是「补块途中用户按发送」
        // 就会让新消息排在剩余历史前面 —— 视图只会 Append，没有插入能力。
        _replayRows = rows;
        _replayChars = chars;
        _replayCharBudget = charBudget;
        _replayBatchSize = batchSize;
        _replayNext = first;
        // **排队，不直接调**。直接调等于首批之后立刻又同步铺第二批，
        // 首屏阻塞翻倍（实测 101 条 = 首批 50 + 第二批 50 一起挡住 UI）。
        // 排队之后首批就是真的 50 条，下一批等渲染帧跑完再补。
        _ui.BeginInvoke(DispatcherPriority.Background, new Action(() => PumpReplay(gen)));
    }

    /// <summary>
    /// 补一批历史。每批之间让出 UI 线程，让输入与绘制先跑。
    ///
    /// 用 `DispatcherPriority.Background`（低于 Input 与 Render）：
    /// 用户打字、点按钮都会插队到补块之前，补块只吃空闲时间。
    ///
    /// `gen` 是**排队时就记下的代号**，不是现读 `_replayGen` —— 后者会在
    /// 用户切对话时变掉，而这次排队的任务属于旧的那一代，必须自己能认出来。
    /// </summary>
    private void PumpReplay(int gen)
    {
        if (gen != _replayGen || _replayRows is not { } rows) return;

        // 用户已经开始新的一轮：剩余历史**一次性同步铺完**。
        //
        // 这是唯一能保住顺序的办法 —— 此时新消息还没进面板（发送路径会在
        // `SetRunning(true)` 之前调 FlushPendingReplay，见那里的说明），
        // 所以在这里抢完是安全的。宁可阻塞一次（通常只剩几十块），
        // 也不能让对话读起来是乱的。
        if (Running) { DrainReplay(); return; }

        // `_replayChars` 与 `_replayRows` 是同生共死的（都在 `ReplayConversation`
        // 里一起赋值）。万一不同步，宁可一次铺完也不能退化成「end 不前进」
        // 那种无限重排 —— 所以兜底给 `rows.Count`，而不是空表。
        var end = _replayChars is { } cs
            ? ReplayBatchEnd(cs, _replayNext, _replayCharBudget, _replayBatchSize)
            : rows.Count;
        for (; _replayNext < end; _replayNext++) AddReplayRow(rows[_replayNext]);
        if (_replayNext >= rows.Count) { FinishReplay(gen); return; }

        // 每批之后让视图跟到底部。
        //
        // **不做这一步，分页重放反而比一次性重放更糟**：面板是 Append-only 的，
        // 铺进去的内容加在视口**下方**。`ScrollToEnd` 只在 `FinishReplay` 时
        // 调一次，于是补块的 1.9 秒里用户盯着对话开头一动不动，新内容在屏幕
        // 外堆积 —— 看起来就像卡死了。实测每批 132–243 ms，跟随滚动后用户
        // 看到的是「内容在快速回溯」，与一次性重放最终停在的位置完全一致。
        ReplayBatchPumped?.Invoke();

        _ui.BeginInvoke(DispatcherPriority.Background, new Action(() => PumpReplay(gen)));
    }

    /// <summary>把队列里剩下的历史同步铺完并收尾。发送路径与 Running 分支都走它。</summary>
    private void DrainReplay()
    {
        if (_replayRows is not { } rows) return;
        for (; _replayNext < rows.Count; _replayNext++) AddReplayRow(rows[_replayNext]);
        FinishReplay(_replayGen);
    }

    /// <summary>
    /// 发送前把未铺完的历史催熟。
    ///
    /// **顺序保证**：视图只会在 `ConvPanel` 末尾 Append，没有中间插入能力。
    /// 若补块还在跑时用户按下发送，`Add(Goal)` 会先落地，剩余历史随后追上来
    /// 排到它**后面** —— 时间线错乱，而且事后无法修复（控件已挂上）。
    /// 所以发送路径必须在写第一条消息**之前**把队列排空。
    ///
    /// 代价：这一次发送会同步阻塞（通常几十毫秒，最多几百毫秒）。可接受 ——
    /// 对比「点发送却看到对话顺序错了」。
    /// </summary>
    private void FlushPendingReplay()
    {
        if (_replayRows is null) return;
        DrainReplay();
    }

    /// <summary>
    /// 一批补到哪里为止（**不含**）：从 `next` 往后取，直到**先**撞上
    /// 字数预算 <see cref="ReplayCharBudget"/> 或块数上限
    /// <see cref="ReplayBatchSize"/>，且不超过总数。
    ///
    /// 抽成纯函数是为了能进门禁。它有两条不显然的性质，都必须成立：
    ///
    /// **一、返回值一定 &gt; next**（只要 next 没到末尾）。否则 `PumpReplay`
    /// 的循环体一次都不跑，`_replayNext` 不前进，`BeginInvoke` 无限自我重排，
    /// UI 线程被空转任务占满。**这也是「至少收一条」那条 break 的唯一理由**：
    /// 一条 4 万字的消息一进来就超预算，若不无条件收下它，游标原地不动。
    ///
    /// **二、切在字数上而不是块数上**。2026-10-18 实测：真实对话前 50 条
    /// 合计 246630 字、单条最长 40887 字，按块数切出来的「50 块」要
    /// 1575.85 ms 排版；而合成样本里 50 块只要 150 ms。差 10 倍，
    /// 差别全在字数。
    /// </summary>
    /// <param name="chars">每一行正文的字数（含思考），与 <paramref name="total"/> 等长。</param>
    internal static int ReplayBatchEnd(
        IReadOnlyList<int> chars, int next,
        int charBudget = ReplayCharBudget, int batchSize = ReplayBatchSize)
    {
        var total = chars.Count;
        if (next >= total) return total;

        var end = next;
        var used = 0;
        while (end < total)
        {
            var take = end - next;          // 本批已经收了几条
            if (take >= batchSize) break;
            // **至少收一条**：见上面性质一。超长单条必须能推着游标往前走。
            if (take > 0 && used + chars[end] > charBudget) break;
            used += chars[end];
            end++;
        }
        return end;
    }

    /// <summary>铺一条重放行。思考块必须挂在回答**前面** —— 时间上它先发生。</summary>
    private void AddReplayRow((TranscriptKind Kind, string Text, string Thoughts) row)
    {
        if (row.Kind == TranscriptKind.Assistant && row.Thoughts.Length > 0)
            Add(TranscriptKind.Thought, row.Thoughts, "", it => it.Thought = new ThoughtState
            {
                Chars = row.Thoughts.Length,
                Streaming = false,   // 重放的都是过去的，一律折叠
            });
        Add(row.Kind, row.Text);
    }

    /// <summary>重放真正结束：放开滚动短路，并让视图滚一次到底。</summary>
    private void FinishReplay(int gen)
    {
        if (gen != _replayGen) return;
        _replayRows = null;
        _replayChars = null;
        _replayNext = 0;
        IsReplaying = false;
        // 收尾只滚一次（视图的 OnHistoryReplayed）。逐条滚的话，
        // N 条消息就是 N 次强制全量布局 —— 启动/切对话冻几秒的元凶。
        ReplayFinished?.Invoke();
    }

    /// <summary>
    /// 视图正在批量重放历史（启动恢复 / 切对话）。此时逐条滚到底是
    /// 纯浪费 —— 见 ScrollToEndIfFollowing 的注释。
    /// </summary>
    public bool IsReplaying { get; private set; }

    /// <summary>一次重放铺完（无论消息多少条，只发一次）。视图在此滚到底。</summary>
    public event Action? ReplayFinished;

    /// <summary>
    /// 分页重放补了一批。视图应在此跟到底部。
    ///
    /// 与 <see cref="ReplayFinished"/> 分开的理由：`ReplayFinished` 的语义是
    /// 「历史铺完了」，视图在它里面做的是**收尾**（重新打开跟随）。补批只是
    /// 「又多了一段」，两者混用会让收尾逻辑在每次补批时重跑一遍。
    /// </summary>
    public event Action? ReplayBatchPumped;

    /// <summary>
    /// 同步回来后要不要重铺：条数没变就不铺（重铺一次就是一次全量布局）。
    /// 纯函数，可进门禁。注意锚点校正不看条数 —— 它不影响显示，
    /// 只影响下次发送，跳过重绘是安全的。
    /// </summary>
    internal static bool ShouldReplayAfterSync(long localBefore, long localAfter) =>
        localAfter != localBefore;

    private void ClearTranscript()
    {
        _pacer.Clear();
        StopPaceTimer();
        _streaming = null;
        _thinking = null;
        _thought = null;
        _lastRunSummary = "";
        _activeToolSeq = 0;
        _toolBySeq.Clear();
        _liveOutput.Clear();
        // 尚未铺完的重放队列一并作废：流水都清了，剩下的历史没有落脚点了。
        // **只清队列，不动 `_replayGen`** —— `ReplayConversation` 是
        // 「先自增代号、再调本方法」，在这里再自增会把它自己那一代也作废掉。
        _replayRows = null;
        _replayChars = null;
        _replayNext = 0;
        // 同理必须放开 `IsReplaying`：分页补块途中若被 `conversation.new` 之类
        // 打断，`FinishReplay` 永远不会跑，而 `ScrollToEndIfFollowing` 会
        // **永久**短路 —— 界面从此不再跟随新消息，且没有任何报错。
        // （`ReplayConversation` 的顺序已调整为「先 ClearTranscript，再置 true」。）
        IsReplaying = false;
        _transcript.Clear();
        // 数据清了，**必须同时通知视图清 UI** —— 见 TranscriptCleared 的说明。
        TranscriptCleared?.Invoke();
    }

    /// <summary>解析引擎侧的秒级时间戳（`time.time()` 的浮点秒）。</summary>
    private static DateTimeOffset ParseUnixSeconds(JsonElement obj, string name)
    {
        if (!obj.TryGetProperty(name, out var el) ||
            el.ValueKind != JsonValueKind.Number) return default;
        try
        {
            return DateTimeOffset.FromUnixTimeMilliseconds((long)(el.GetDouble() * 1000));
        }
        catch
        {
            return default;
        }
    }

    // ---------- 长期记忆 ----------

    /// <summary>读记忆全文（给设置页的编辑框）。</summary>
    public async Task<string> GetMemoryAsync()
    {
        var engine = _engine;
        if (engine is null) return "";
        try
        {
            var res = await engine.MemoryGetAsync();
            return GetString(res, "text");
        }
        catch (Exception ex)
        {
            Add(TranscriptKind.Error, $"读取记忆失败：{ex.Message}");
            return "";
        }
    }

    public async Task<bool> SetMemoryAsync(string text)
    {
        var engine = _engine;
        if (engine is null) return false;
        try
        {
            await engine.MemorySetAsync(text ?? "");
            await RefreshStatusAsync(force: true);
            return true;
        }
        catch (Exception ex)
        {
            Add(TranscriptKind.Error, $"保存记忆失败：{ex.Message}");
            return false;
        }
    }

    /// <summary>把一段文本追加进记忆（界面上的「记住这条」走这里）。</summary>
    public async Task<bool> AppendMemoryAsync(string text, string section = "")
    {
        var engine = _engine;
        if (engine is null) return false;
        try
        {
            await engine.MemoryAppendAsync(text ?? "", section ?? "");
            await RefreshStatusAsync(force: true);
            return true;
        }
        catch (Exception ex)
        {
            Add(TranscriptKind.Error, $"写入记忆失败：{ex.Message}");
            return false;
        }
    }

    public async Task<bool> ClearMemoryAsync()
    {
        var engine = _engine;
        if (engine is null) return false;
        try
        {
            await engine.MemoryClearAsync();
            await RefreshStatusAsync(force: true);
            return true;
        }
        catch (Exception ex)
        {
            Add(TranscriptKind.Error, $"清空记忆失败：{ex.Message}");
            return false;
        }
    }

    // ---------- 朗读（TTS）控制面 ----------
    //
    // 宿主只做**三件事**：签票、列音色、切音色。音频流不在宿主、也不在引擎 ——
    // 前端拿到票据后直连上游 WS，用 WebView2 自带的 WebCodecs 解 Opus。
    //
    // 这么分工不是为了省事，是**必然**：引擎侧要解 Opus 就得多带一个
    // 40MB 级的解码库（PyAV / opuslib），而 Chromium 内核本来就带着硬件
    // 加速的解码器；何况流控（ack 的 received_seq / played_seq）必须跟着
    // 播放进度走，那个进度只有前端知道。
    //
    // 三个方法都**不抛异常**，统一返回带 `error` 字段的对象 —— 与
    // `GetMemoryAsync` 那批的「失败返回空值」不同，朗读失败必须让用户看见
    // 原因（限流 / 没有正文 / 语言不支持），不能静默变成「点了没反应」。

    /// <summary>
    /// 为某条消息签一张朗读票据。**会发网络请求**，失败时返回
    /// <c>{"sid","message_id","ticket":"","error":"..."}</c>。
    /// </summary>
    public async Task<JsonElement> PrepareTtsAsync(string messageId)
    {
        var engine = _engine;
        if (engine is null)
            return EmptyTtsTicket(messageId, "引擎尚未就绪");
        try
        {
            return await engine.TtsTicketAsync(messageId ?? "");
        }
        catch (Exception ex)
        {
            // 不往对话流里塞错误行：用户点的是气泡上的小按钮，
            // 报错应当出现在气泡附近（前端 toast），而不是在流水里
            // 插一条看起来像「任务失败」的记录。
            Log($"[朗读] 签票失败：{ex.Message}", LogLevel.Warn);
            return EmptyTtsTicket(messageId, ex.Message);
        }
    }

    private static JsonElement EmptyTtsTicket(string messageId, string error)
    {
        using var doc = JsonDocument.Parse(JsonSerializer.Serialize(new
        {
            sid = "",
            message_id = messageId ?? "",
            ticket = "",
            error = error ?? "",
        }));
        return doc.RootElement.Clone();
    }

    /// <summary>列出可用音色。只读，**不消耗朗读配额**。</summary>
    public async Task<JsonElement> ListTtsVoicesAsync()
    {
        var engine = _engine;
        if (engine is null) return EmptyTtsVoices("引擎尚未就绪");
        try
        {
            return await engine.TtsVoicesAsync();
        }
        catch (Exception ex)
        {
            Log($"[朗读] 读取音色失败：{ex.Message}", LogLevel.Warn);
            return EmptyTtsVoices(ex.Message);
        }
    }

    private static JsonElement EmptyTtsVoices(string error)
    {
        using var doc = JsonDocument.Parse(JsonSerializer.Serialize(new
        {
            items = Array.Empty<object>(),
            error = error ?? "",
        }));
        return doc.RootElement.Clone();
    }

    /// <summary>切换音色（账号级偏好，网页版同步生效）。</summary>
    public async Task<JsonElement> SetTtsVoiceAsync(string voiceId)
    {
        var engine = _engine;
        if (engine is null)
            return EmptyTtsVoiceResult(voiceId, "引擎尚未就绪");
        try
        {
            return await engine.TtsSetVoiceAsync(voiceId ?? "");
        }
        catch (Exception ex)
        {
            Log($"[朗读] 切换音色失败：{ex.Message}", LogLevel.Warn);
            return EmptyTtsVoiceResult(voiceId, ex.Message);
        }
    }

    private static JsonElement EmptyTtsVoiceResult(string voiceId, string error)
    {
        using var doc = JsonDocument.Parse(JsonSerializer.Serialize(new
        {
            ok = false,
            voice_id = voiceId ?? "",
            error = error ?? "",
        }));
        return doc.RootElement.Clone();
    }

    // ---------- 朗读（Fish Audio 直连）----------
    //
    // 与上面那三个方法的区别：那三个走**引擎签票 + 前端直连 WS**，
    // 这三条走**宿主直连 api.fish.audio**，音频以 base64 回给前端。
    //
    // 为什么音频不直接交给前端去取：
    //   ① CSP 是 `default-src 'none'`，页面里发不出 HTTPS 请求；
    //   ② 就算放开 CSP，API key 也必须留在宿主侧 —— 它是**账号级凭据**，
    //      不是 DeepSeek 那种「只能读一条消息」的一次性票据。
    // 所以正确的分工是：key 与 HTTP 全在宿主，前端只负责把字节解码成声音。

    private TtsConfig TtsConfig()
    {
        _ttsCfg ??= TtsConfigStore.Load();
        return _ttsCfg;
    }

    /// <summary>当前朗读后端。前端据此决定「走签票还是走 Fish」。</summary>
    public string TtsEngine => TtsConfig().Engine;

    /// <summary>
    /// 用 Fish 合成一段文本，返回 base64 音频。
    ///
    /// **返回 base64 而不是字节数组**：Bridge 走的是 JSON（postMessage），
    /// 字节数组序列化后是一个很长的数字列表，体积约为 base64 的 3 倍。
    /// 一段 35KB 的 mp3 会变成 10 万字符的 JSON，明显拖慢消息传递。
    /// </summary>
    public async Task<JsonElement> SpeakFishAsync(string text, string? voiceId)
    {
        var cfg = TtsConfig();
        if (cfg.Engine != "fish")
            return FishResult("", "", "当前朗读后端不是 Fish");

        using var fish = new FishTts(cfg);
        var (audio, mime, error) = await fish.SpeakAsync(text ?? "", voiceId);
        if (error.Length > 0)
        {
            // 失败原因要能回溯，但**不能把 key 写进日志** —— 这里的
            // 错误串来自 Fish 的响应体或异常消息，本身不含凭据。
            Log($"[朗读] Fish 合成失败：{error}", LogLevel.Warn);
            return FishResult("", "", error);
        }
        return FishResult(Convert.ToBase64String(audio), mime, "");
    }

    private static JsonElement FishResult(string audioBase64, string mime, string error)
    {
        using var doc = JsonDocument.Parse(JsonSerializer.Serialize(new
        {
            audio = audioBase64 ?? "",
            mime = mime ?? "",
            error = error ?? "",
        }));
        return doc.RootElement.Clone();
    }

    /// <summary>Fish 音色列表。只读，不消耗合成额度。</summary>
    public async Task<JsonElement> ListFishVoicesAsync()
    {
        var cfg = TtsConfig();
        if (cfg.Engine != "fish")
            return FishVoicesResult(Array.Empty<object>(), "", "");

        using var fish = new FishTts(cfg);
        var (items, error) = await fish.VoicesAsync(20);
        if (error.Length > 0)
        {
            Log($"[朗读] Fish 音色读取失败：{error}", LogLevel.Warn);
            return FishVoicesResult(Array.Empty<object>(), "", error);
        }

        // `isCurrent` 由本地配置决定 —— Fish 没有「账号级当前音色」这个概念
        // （每次请求都显式传 reference_id），所以选中态只能靠本地记的 voice。
        var current = (cfg.Voice ?? "").Trim();
        var list = items.Select(v => (object)new
        {
            id = v.Id,
            name = string.IsNullOrWhiteSpace(v.Title) ? v.Id : v.Title,
            desc = v.Description ?? "",
            languages = Array.Empty<string>(),
            tags = v.Tags,
            isCurrent = current.Length > 0 && v.Id == current,
        }).ToArray();
        return FishVoicesResult(list, current, "");
    }

    private static JsonElement FishVoicesResult(object[] items, string current, string error)
    {
        using var doc = JsonDocument.Parse(JsonSerializer.Serialize(new
        {
            items,
            current = current ?? "",
            error = error ?? "",
        }));
        return doc.RootElement.Clone();
    }

    /// <summary>切换 Fish 音色。写的是本地 `tts.json`，不碰上游。</summary>
    public JsonElement SetFishVoice(string voiceId)
    {
        var cfg = TtsConfig();
        cfg.Voice = (voiceId ?? "").Trim();
        TtsConfigStore.Save(cfg);
        _ttsCfg = cfg;
        using var doc = JsonDocument.Parse(JsonSerializer.Serialize(new
        {
            ok = true,
            id = cfg.Voice,
            error = "",
        }));
        return doc.RootElement.Clone();
    }

    /// <summary>读取/写入朗读配置。设置面板用。</summary>
    public JsonElement GetTtsSettings()
    {
        var cfg = TtsConfig();
        using var doc = JsonDocument.Parse(JsonSerializer.Serialize(new
        {
            engine = cfg.Engine,
            @base = cfg.Base,
            model = cfg.Model,
            voice = cfg.Voice,
            format = cfg.Format,
            // **key 绝不回传**，只回「有没有配」与长度 —— 前端不需要它，
            // 回传等于把它多放一份到渲染进程的内存里。
            hasKey = !string.IsNullOrWhiteSpace(cfg.ApiKey),
            keyLength = (cfg.ApiKey ?? "").Length,
            path = TtsConfigStore.Path,
        }));
        return doc.RootElement.Clone();
    }

    /// <summary>保存朗读配置。空 key 表示「保持原值不变」—— 面板不回传 key，用户不改就不该被清空。</summary>
    public JsonElement SetTtsSettings(string engine, string baseUrl, string model,
        string format, string apiKey)
    {
        var cfg = TtsConfig();
        if (!string.IsNullOrWhiteSpace(engine))
            cfg.Engine = engine.Trim() == "deepseek" ? "deepseek" : "fish";
        if (!string.IsNullOrWhiteSpace(baseUrl))
            cfg.Base = baseUrl.Trim().TrimEnd('/');
        if (!string.IsNullOrWhiteSpace(model))
            cfg.Model = model.Trim();
        if (!string.IsNullOrWhiteSpace(format))
            cfg.Format = format.Trim();
        if (!string.IsNullOrWhiteSpace(apiKey))
            cfg.ApiKey = apiKey.Trim();
        TtsConfigStore.Save(cfg);
        _ttsCfg = cfg;
        return GetTtsSettings();
    }

    // ---------- 任务 ----------

    /// <summary>
    /// 现在提交会不会被接受。窗口用它决定「要不要清空输入框」——
    /// 不能被接受时保留用户已经打好的字，别让一次提示把他辛苦输的内容吃掉。
    ///
    /// **熔断与停机也算「不会被接受」**（2026-10-18 旅程 8 实测后补上）。
    /// 原来只判 `!Running`，于是熔断期间按发送：输入框被清空、附件被消费，
    /// 然后引擎在门禁处拒绝，一个字都没发出去 —— 用户看到的是
    /// 「我打的字没了，什么都没发生」。
    ///
    /// **只判 `open` / `halted`，不判 `GateAllowed`**。这个区分很关键：
    ///
    ///   · 闸门未放行（`GateAllowed == false`）时，引擎的 `_await_gate()`
    ///     会**等着**，等到放行就把这一轮发出去。也就是说提交最终是会被接受的，
    ///     只是慢。若据此判 `CanSubmit == false`，用户在等待期间反复按 Enter
    ///     都毫无反应，反而更像卡死。
    ///   · 熔断 `open` / 停机 `halted` 有明确的退避时长，期间点多少次都必然失败。
    ///     这才是该扣住输入的状态。
    ///
    /// 与前端 `app.js:isHardBlocked()` 是同一套判据 —— 两处必须一致，
    /// 否则会出现「Web 壳保住输入框、WPF 壳却清空」的壳间行为分叉。
    /// </summary>
    public bool CanSubmit =>
        _engine is not null && !Running
        && Status.CircuitLevel is not ("open" or "halted");

    /// <summary>
    /// 提交一轮。`files` 是**真附件**（绝对路径），走引擎的上传通道。
    ///
    /// `files` 会先经 `/api/v0/file/upload_file` 上传，再把 file id 挂进
    /// `ref_file_ids` —— 模型读到的是**服务端解析后的文件**（PDF/图片 OCR/
    /// 表格都能解），而不是一段由壳拼出来的文字。
    ///
    /// 2026-10-18 之前这里有个分叉：WPF 壳（`MainWindow.CombineGoal`）把文件
    /// 内容展开成最多 24KB 文字拼进 `goal`，服务端根本不知道有文件；Web 壳才
    /// 走真上传。同一个产品两个壳给出两种结果，界面上还看不出来。现在 WPF 壳
    /// 已改走同一条路，那条内联路径连同 `ExpandAttachments` 一起删了。
    ///
    /// 参数仍可选（`null` = 没有附件），与老调用点兼容。
    /// </summary>
    /// <summary>
    /// 发送前的本地附件校验。全部通过返回 <c>null</c>，否则返回可直接显示的问题清单。
    ///
    /// **引擎不是已经校验了吗，为什么壳里还要再来一遍** —— 三条理由，缺一条都
    /// 不足以证明这份重复是值得的：
    ///
    ///   ① **时机**。引擎的校验（`deepseek_rpc.py:1148` 起）在 `transport.send()`
    ///      内部，排在取 challenge、解 PoW、建连接**之后**。用户挂一个早就被删掉的
    ///      文件名，要盯着界面等好几秒才被告知。而 `FileInfo.Exists` 是一次
    ///      本地系统调用，按下回车的**那一毫秒**就能给答案。
    ///
    ///   ② **措辞**。引擎抛的是 `TransportError`，界面统一显示成「传输错误：…」
    ///      （`case "error"`）。「传输」两个字会把「文件被移走了」这种纯本地事实
    ///      说成网络问题，用户会去查网络、查账号、查额度 —— 而真正要改的只是
    ///      重新挑一个文件。
    ///
    ///   ③ **聚合**。引擎逐文件上传，第一个坏的就抛，用户挂 5 个坏 2 个就得来回
    ///      试两轮。这里一次把全部问题列出来。
    ///
    /// 限额与引擎保持一致（`UPLOAD_MAX_COUNT` / `UPLOAD_MAX_BYTES`）。
    /// **重复定义是有意的**：壳不该去反射引擎的内部常量。真值若比这里更小，
    /// 服务端仍会拒，那时引擎的报错照常显示 —— 这份校验只做「提前」和「好读」，
    /// 不做权威判定，所以放宽不会造成漏网。
    /// </summary>
    private static string? ValidateFiles(IReadOnlyList<string> files)
    {
        const int MaxCount = 50;
        const long MaxBytes = 100L * 1024 * 1024;

        if (files.Count > MaxCount)
            return $"一条消息最多挂 {MaxCount} 个附件（本次 {files.Count} 个）";

        var problems = new List<string>();
        foreach (var f in files)
        {
            // 全限定 `System.IO.Path` —— 理由同 SubmitAsync 里那段注释
            // （WPF 项目没有 `System.IO` 的隐式 using，且裸 `Path` 会与
            //  `System.Windows.Shapes.Path` 撞名）。
            var name = System.IO.Path.GetFileName(f) ?? f;
            try
            {
                // 文件夹要单独判：`FileInfo.Exists` 对目录返回 false，
                // 落进「不存在」那条会误导人去检查路径拼写。
                if (System.IO.Directory.Exists(f))
                {
                    problems.Add($"{name}：这是文件夹，请选择文件");
                    continue;
                }
                var fi = new System.IO.FileInfo(f);
                if (!fi.Exists)
                    problems.Add($"{name}：文件不存在（可能已被移动或删除）");
                else if (fi.Length == 0)
                    problems.Add($"{name}：空文件，没有内容可提取");
                else if (fi.Length > MaxBytes)
                    problems.Add($"{name}：{fi.Length / 1048576.0:0.0} MB，超过 100 MB 上限");
            }
            catch (Exception ex)
            {
                // 无权限、路径非法、网络盘断开 —— 全归到这里。
                // 一条烂路径不该把整轮提交炸成「提交失败：<异常栈>」。
                problems.Add($"{name}：{ex.Message}");
            }
        }
        return problems.Count == 0 ? null : string.Join("；", problems);
    }

    public async Task SubmitAsync(string goal, IReadOnlyList<string>? files = null)
    {
        var engine = _engine;
        goal = (goal ?? "").Trim();
        // 纯附件也可发（字可以一个不写，只发文件）—— 与 WPF 壳同一条语义。
        if (goal.Length == 0 && (files is null || files.Count == 0)) return;

        // ⚠️ 三种「发不出去」的情况，**必须以可见的方式告诉用户**。
        // 这里以前是静默 `return` —— 用户按下 Enter，输入框清空了，然后什么都没
        // 发生，界面上连一行说明都没有，只能得出「Enter 坏了」的结论。
        // 而实际上最常见的原因是「上一轮还没跑完」：思考模式下一轮要 ~90 秒，
        // 期间再按多少次 Enter 都是这一行话。
        if (engine is null)
        {
            Add(TranscriptKind.Error, "引擎未连接，无法发送（重启应用试试）");
            Log("[任务] 引擎未连接", LogLevel.Bad);
            RunningChanged?.Invoke(false);
            return;
        }
        if (Running)
        {
            Add(TranscriptKind.Notice, "上一轮还在跑，等它出结果再发（思考模式下一轮约 1 分钟）");
            Log("[任务] 上一轮仍在运行，已忽略本次发送", LogLevel.Warn);
            return;
        }

        // 熔断 / 停机：**明确拒绝并说明原因**，不把注定失败的 submit 交给引擎。
        //
        // 与 `CanSubmit` 同一条判据（见那边的注释：只判 open/halted，不判闸门）。
        // 这里仍然要挡一道，因为 Web 壳的前端是「照常提交、但不清空输入框」——
        // 提交照常发生，拒绝的理由必须由这一层给出，否则用户看到的就是
        // 「点了发送，输入框还在，但什么都没发生」。
        //
        // 不挡 `GateAllowed == false`：那种情况引擎会等，最终能发出去。
        if (Status.CircuitLevel is "open" or "halted")
        {
            var why = string.IsNullOrWhiteSpace(Status.CircuitReason) ? "原因未上报" : Status.CircuitReason;
            Add(TranscriptKind.Notice,
                Status.CircuitLevel == "halted"
                    ? $"已停机，需人工介入后才能继续发送（{why}）"
                    : $"熔断中，退避结束后再发（{why}）");
            Log($"[任务] 熔断 {Status.CircuitLevel} 期间拒绝发送 · {why}", LogLevel.Warn);
            return;
        }

        // 附件先本地验一遍，**验不过就不进入 running**。
        // 位置很关键：放在 `SetRunning(true)` 之前 —— 否则界面会先变成「运行中」
        // 再变回「空闲」，用户看到一次无意义的状态闪烁，而其实一个请求都没发。
        // 也放在 Add(Goal) 之前：坏附件不该在对话流里留下一条「用户说了这句话」
        // 的正式记录 —— 那句话根本没发出去。
        if (files is { Count: > 0 })
        {
            var bad = ValidateFiles(files);
            if (bad is not null)
            {
                Add(TranscriptKind.Error, $"附件有问题，未发送：{bad}");
                Log($"[附件] 本地校验未通过 · {bad}", LogLevel.Bad);
                RunningChanged?.Invoke(false);
                return;
            }
        }

        // ★ 必须在 `SetRunning(true)` 与 `Add(Goal)` **之前**把未铺完的历史排空。
        //
        // 视图只会往 `ConvPanel` 末尾 Append，没有中间插入能力。若此时还有
        // 历史在后台分批补块，`Add(Goal)` 会先落地，剩余历史随后追上来排到
        // 它**后面** —— 用户看到的是「我这句话跑到历史前面去了」，而且事后
        // 无法修复（控件已经挂上，只能整条对话重放）。
        //
        // 也必须在 `SetRunning(true)` 之前：那条路径看到 `Running` 就会自己去
        // Drain，但那时 Goal 气泡已经进面板了，来不及。
        FlushPendingReplay();

        SetRunning(true);
        // 纯附件时 goal 是空的 —— 条目里只写文件数，别留一条空白气泡。
        Add(TranscriptKind.Goal, goal.Length > 0
            ? goal
            : $"（仅附件 · {files!.Count} 个文件）");
        // 附件单独占一条 `Info`，**不是**为了好看：
        //   · 上传要 7–31 秒，而这行在**上传开始之前**就出现 —— 用户按下回车
        //     立刻能看到「有东西在发生」，不用等引擎的 file_upload 事件。
        //   · 附件不属于「用户说的话」，混进 Goal 气泡会让历史记录失真
        //     （回看时看不出哪些字是用户打的、哪些是系统补的）。
        //   · `Info` 是常驻条目类型，不会被 CollapseThought 之类的流式逻辑碰。
        // 这里**必须写全限定 `System.IO.Path`**，不能图省事写成 `Path`：
        // `UseWPF=true` 的项目里，`Microsoft.NET.Sdk.WindowsDesktop` 会把
        // `System.IO` 从隐式 using 里剔除（因为 `System.IO.Path` 与
        // `System.Windows.Shapes.Path` 同名），而本文件又没有 `using System.IO`。
        // 于是裸写 `Path` 一个候选都匹配不上，直接 CS0103。
        // 反过来补 `using System.IO;` 更糟 —— 这个文件被 WPF 壳共享，
        // 补了就给那边埋下 `Path` 歧义。全限定是唯一两个壳都安全写法。
        // （`?? f` 兜住极端情况：调用方绕过 WebBridge 直接传空串/空引用。）
        if (files is { Count: > 0 })
            Add(TranscriptKind.Info,
                $"附件 {files.Count} 个：" +
                string.Join("、", files.Select(f => System.IO.Path.GetFileName(f) ?? f)));
        Log($"[任务] {goal}", LogLevel.Faint);

        try
        {
            await engine.RunAsync(goal, files: files);
        }
        catch (Exception ex)
        {
            SetRunning(false);
            Add(TranscriptKind.Error, $"提交失败：{ex.Message}");
        }
    }

    /// <summary>
    /// 取消当前这一轮。
    ///
    /// 为什么需要它：上游在「流没结束但也没有 final chunk」时会进 recovery 轮询 120s，
    /// 再抛错触发 `@running(retry=5)` 整条重发 —— 最坏约 25 分钟
    /// （实测 2026-10-14：120s 空闲检测 + 120s recovery = 4 分钟/次 × 6 次）。
    /// 这段时间 <see cref="Running"/> 一直是 true，用户在界面上**什么都发不了**，
    /// 只能关掉应用。
    ///
    /// 没有做成「引擎自动超时」是刻意的：任何按时间猜的阈值都会误杀正常的长回答。
    /// 交给用户按，就不会有「我明明在等它出结果，它却自己掐了」这种事。
    /// </summary>
    public async Task CancelRunAsync()
    {
        var engine = _engine;
        if (engine is null) return;
        try
        {
            var result = await engine.CancelRunAsync();
            var ok = result.TryGetProperty("cancelled", out var c) && c.GetBoolean();
            if (!ok)
            {
                var why = result.TryGetProperty("reason", out var r) ? r.GetString() : null;
                Log($"[取消] 没有可取消的轮次：{why}", LogLevel.Warn);
                return;
            }
            Log("[取消] 已发出取消请求", LogLevel.Warn);
        }
        catch (Exception ex)
        {
            Log($"[取消] 取消失败：{ex.Message}", LogLevel.Bad);
        }
    }

    public async Task ResetBreakerAsync()
    {
        var engine = _engine;
        if (engine is null) return;
        try
        {
            var result = await engine.ResetBreakerAsync();
            Log($"[熔断] 已人工解除 · {result.GetRawText()}", LogLevel.Ok);
        }
        catch (Exception ex)
        {
            Log($"[熔断] 解除失败：{ex.Message}", LogLevel.Bad);
        }
    }

    /// <summary>
    /// 重新验证缓存凭据；不行时引擎会顺带用 CDP 从**已开着调试端口**的浏览器补一次
    /// （不弹新窗口）。要真的重新登录请走「打开浏览器登录」。
    ///
    /// **与 gemini 版的设计差别**：那边是「立刻返回 accepted，进度走 `account_refresh`
    /// 事件」—— 因为要起无头浏览器取 cookie、还要等轮换稳定，跑十几秒。
    /// DeepSeek 这边多数情况只是**验一次**（一次极轻的 `create_pow_challenge`），
    /// 同步等就够了，所以这里直接 await 结果，不需要事件通道。
    /// </summary>
    public async Task RefreshIdentityAsync()
    {
        var engine = _engine;
        if (engine is null)
        {
            Add(TranscriptKind.Error, "引擎未连接，无法刷新凭据（重启应用试试）");
            return;
        }
        if (Status.IdentityRefreshing) return;   // 防重入：连点两次不该打两次验证

        Status.IdentityRefreshing = true;
        StatusChanged?.Invoke();
        try
        {
            var result = await engine.RefreshIdentityAsync();
            var ok = result.TryGetProperty("ok", out var o) && o.ValueKind == JsonValueKind.True;
            var detail = result.TryGetProperty("detail", out var d) ? d.GetString() : "";
            var source = result.TryGetProperty("source", out var s) ? s.GetString() : "";
            // 封禁 ≠ 凭据过期。两者都报 40003，但处置是账号级的 ——
            // 提示「重新登录」会让用户白折腾，而且每次重登都是一次真实请求。
            var suspended = result.TryGetProperty("suspended", out var sp) &&
                            sp.ValueKind == JsonValueKind.True;

            if (ok)
            {
                Add(TranscriptKind.Notice, $"凭据已刷新（来源 {source}）· {detail}");
                Log($"[身份] 刷新成功 · {source} · {detail}", LogLevel.Ok);
            }
            else if (suspended)
            {
                Add(TranscriptKind.Error,
                    $"账号被服务端处置：{detail}\n" +
                    "封禁期内重新登录不会解除限制 —— 请等处置到期，不要反复重试。");
                Log($"[身份] 账号被处置：{detail}", LogLevel.Bad);
            }
            else
            {
                // **不假装成功**：验不过就说清楚，并给出下一步动作。
                Add(TranscriptKind.Error,
                    $"凭据不可用：{detail}\n" +
                    "可以用「打开浏览器登录」重新取一次，或手动粘贴 token。");
                Log($"[身份] 刷新失败：{detail}", LogLevel.Bad);
            }
            await RefreshStatusAsync(force: true);
        }
        catch (Exception ex)
        {
            Add(TranscriptKind.Error, $"刷新凭据失败：{ex.Message}");
            Log($"[身份] 刷新失败：{ex.Message}", LogLevel.Bad);
        }
        finally
        {
            Status.IdentityRefreshing = false;
            StatusChanged?.Invoke();
        }
    }

    // ---------- 初始提示词 ----------

    /// <summary>读提示词（模板原文 + 文件位置 + 与真实运行同一套渲染的预览）。
    /// 引擎不可用时返回 null，界面据此提示而不是假装成功。</summary>
    public async Task<PromptDoc?> LoadPromptAsync()
    {
        var engine = _engine;
        if (engine is null) return null;
        try
        {
            return ParsePromptDoc(await engine.GetPromptAsync());
        }
        catch (Exception ex)
        {
            Log($"[提示词] 读取失败：{ex.Message}", LogLevel.Bad);
            return null;
        }
    }

    public async Task<bool> SavePromptAsync(string template)
    {
        var engine = _engine;
        if (engine is null)
        {
            Add(TranscriptKind.Error, "引擎未连接，无法保存提示词（重启应用试试）");
            return false;
        }
        try
        {
            var doc = ParsePromptDoc(await engine.SetPromptAsync(template));
            if (doc.UnknownPlaceholders.Count > 0)
            {
                // 不拦下来，但必须说出来 —— 写错的占位符会**原样留在提示词里**，
                // 用户只会觉得「模型怎么不听话了」，不会想到是自己名字打错了。
                Add(TranscriptKind.Notice,
                    $"初始提示词已保存，但有 {doc.UnknownPlaceholders.Count} 个无法识别的占位符：" +
                    $"{string.Join("、", doc.UnknownPlaceholders)}（它们会原样留在提示词里）");
                Log($"[提示词] 未知占位符：{string.Join(", ", doc.UnknownPlaceholders)}", LogLevel.Warn);
            }
            else
            {
                Add(TranscriptKind.Notice, "初始提示词已保存 · 下一轮生效（不用重启引擎）");
                Log("[提示词] 已保存，下一轮生效", LogLevel.Ok);
            }
            return true;
        }
        catch (Exception ex)
        {
            Add(TranscriptKind.Error, $"保存提示词失败：{ex.Message}");
            Log($"[提示词] 保存失败：{ex.Message}", LogLevel.Bad);
            return false;
        }
    }

    public async Task<PromptDoc?> ResetPromptAsync()
    {
        var engine = _engine;
        if (engine is null) return null;
        try
        {
            var doc = ParsePromptDoc(await engine.ResetPromptAsync());
            Add(TranscriptKind.Notice, "初始提示词已恢复为内置默认");
            Log("[提示词] 已恢复默认", LogLevel.Ok);
            return doc;
        }
        catch (Exception ex)
        {
            Add(TranscriptKind.Error, $"恢复默认提示词失败：{ex.Message}");
            return null;
        }
    }

    private static List<string> ReadStringList(JsonElement d, string key)
    {
        var list = new List<string>();
        if (!d.TryGetProperty(key, out var arr) || arr.ValueKind != JsonValueKind.Array) return list;
        foreach (var x in arr.EnumerateArray())
            if (x.ValueKind == JsonValueKind.String) list.Add(x.GetString() ?? "");
        return list;
    }

    private static PromptDoc ParsePromptDoc(JsonElement d) => new()
    {
        Path = GetString(d, "path"),
        Template = GetString(d, "template"),
        DefaultTemplate = GetString(d, "default_template"),
        IsDefault = d.TryGetProperty("is_default", out var idf) && idf.ValueKind == JsonValueKind.True,
        Placeholders = ReadStringList(d, "placeholders"),
        UnknownPlaceholders = ReadStringList(d, "unknown_placeholders"),
        Preview = GetString(d, "preview"),
    };

    /// <summary>清空执行监视器。两个界面看的是同一份日志，所以清的是共享状态。</summary>
    public void ClearMonitor() => _monitor.Clear();

    // ---------- 身份 ----------

    /// <summary>身份快照（**不含 token 本体** —— 引擎只回长度与状态）。</summary>
    public Task<JsonElement> IdentityStatusAsync() =>
        RequireEngine().IdentityStatusAsync();

    /// <summary>
    /// 保存账号标识（配置里的 `identity.account_label`）。
    ///
    /// **必须有这个入口**：`IdentityConfig.violations()` 在标识为空时会拦住一切提交
    /// （`require_dedicated_account` 默认 True）。界面若只读不写，应用就会停在
    /// 「能登录、但一个字都发不出去」的状态，而用户看不出原因。
    /// </summary>
    public async Task SetIdentityLabelAsync(string label)
    {
        var engine = _engine;
        if (engine is null)
        {
            Add(TranscriptKind.Error, "引擎未连接，无法保存标识（重启应用试试）");
            return;
        }
        await engine.ConfigSetAsync(new { identity = new { account_label = label } });
        await RefreshStatusAsync(force: true);
    }

    /// <summary>
    /// 切换传输档位（DeepSeek / Chrome / Gemini）。
    ///
    /// ## 为什么不能只发一条 `config.set` 就完事
    ///
    /// `config.set` 在引擎侧会 `cfg.save()` + `_drop_runtime()`，下次 `start`
    /// 惰性重建传输层 —— 所以**不需要重启进程**。但它同时**丢掉了当前会话**：
    /// 新 runtime 的 `SessionManager` 是一张白纸。
    ///
    /// 这不是缺陷，而是必须的：`SessionMeta.cid` 是**服务端会话 id**，
    /// DeepSeek 的 id 拿到 Gemini 那边毫无意义。留着它，切档位后的第一句话
    /// 会带着一个不属于该服务端的 `chat_session_id` 发出去 —— 上游要么拒绝，
    /// 要么（更糟）把它当成新会话而界面以为在续聊。
    ///
    /// 所以这里显式**新建一条本地对话**，并在界面上说清楚：旧对话还在列表里，
    /// 但它属于上一个档位，切回去才接得上。
    ///
    /// ## 为什么**不**在这里动 `identity`
    ///
    /// 第一版我顺手写了「切 Gemini 就关掉 require_dedicated_account、切回来再打开」。
    /// 那是错的：① 用真实 Gemini 账号（非访客）时仍然需要标识，关掉等于拆掉
    /// 一道闸；② 切回去设成 `true` 也不等于用户原本的值，属于**擅自改用户配置**。
    ///
    /// 访客模式的豁免已经做在 `IdentityConfig.violations()` 里（`guest_mode`
    /// 短路），这里什么都不用做。
    /// </summary>
    public async Task<bool> SetTransportAsync(string name)
    {
        var engine = _engine;
        if (engine is null)
        {
            Add(TranscriptKind.Error, "引擎未连接，无法切换档位（重启应用试试）");
            return false;
        }
        var target = (name ?? "").Trim();
        if (target.Length == 0) return false;
        if (Running)
        {
            Add(TranscriptKind.Notice, "上一轮还在跑，等它出结果再切换档位");
            return false;
        }
        if (string.Equals(target, Status.Transport, StringComparison.OrdinalIgnoreCase))
            return true;

        try
        {
            await engine.ConfigSetAsync(new { transport = target });

            // runtime 已被丢弃 —— 必须重新 start，否则状态快照里
            // `transport.ready` 永远是 false（没有 runtime 可问）。
            await engine.StartAsync();

            // 新 runtime 的会话是空的。显式建一条本地对话，让界面与磁盘
            // 都对齐到「从这一条开始」。
            try { await engine.ConversationNewAsync(); }
            catch (Exception ex)
            {
                // 建对话失败不该让整个切换回滚 —— 档位已经切好了，
                // 用户还能发消息（引擎会自己建会话），只是列表里少一条。
                Log($"切档位后新建对话失败：{ex.Message}", LogLevel.Warn);
            }

            Status.Transport = target;
            await RefreshStatusAsync(force: true);
            await RefreshConversationsAsync();

            var label = target switch
            {
                "gemini_rpc" => "Gemini 网页版",
                "chrome" => "Chrome 真实内核",
                _ => "DeepSeek 网页版",
            };
            Add(TranscriptKind.Info,
                $"已切换到 {label}。新档位从一条空对话开始 —— 之前那些对话属于上一个档位，切回去才接得上。");
            Log($"[档位] 已切换 · {label}", LogLevel.Ok);
            return true;
        }
        catch (Exception ex)
        {
            Add(TranscriptKind.Error, $"切换档位失败：{ex.Message}");
            Log($"[档位] 切换失败：{ex.Message}", LogLevel.Warn);
            return false;
        }
    }

    /// <summary>
    /// 手动粘贴兜底：把用户从 DevTools 里复制出来的 token 交给引擎。
    ///
    /// 引擎会**归一化**（裸串 / `{"value":"..."}` / 带引号 / 带 `Bearer ` 前缀 都吃得下）
    /// 并**验一次**，所以返回里的 `valid` 是可信的。
    /// </summary>
    public async Task<JsonElement> SetIdentityAsync(string token)
    {
        var engine = RequireEngine();
        var result = await engine.IdentitySetAsync(token);
        var valid = result.TryGetProperty("valid", out var v) && v.ValueKind == JsonValueKind.True;
        var detail = GetString(result, "detail");

        if (valid)
        {
            await engine.StartAsync();   // 引擎已停掉运行时，按新凭据拉起来
            Add(TranscriptKind.Notice, $"已保存凭据并验证通过 · {detail}");
        }
        else
        {
            // **验不过就不说「已保存并生效」** —— 存一个用不了的 token 比不存更糟：
            // 用户会以为登录好了，直到第一次对话才炸，而且报错指向错误的方向。
            Add(TranscriptKind.Error, $"凭据已保存但验证不过：{detail}");
        }
        await RefreshStatusAsync(force: true);
        return result;
    }

    /// <summary>清掉本地缓存的凭据。</summary>
    public async Task<JsonElement> ClearIdentityAsync()
    {
        var engine = RequireEngine();
        var result = await engine.IdentityClearAsync();
        Add(TranscriptKind.Notice, "已清除本地凭据");
        await RefreshStatusAsync(force: true);
        return result;
    }

    /// <summary>
    /// 退出登录（换账号）：清 token + 清专用浏览器 profile。
    ///
    /// **为什么不能只调 ClearIdentityAsync**：浏览器登录用持久化 profile，
    /// 里面的 cookie 还在，重新登录会直接回到原账号 —— 用户以为换了，其实没换。
    /// </summary>
    public async Task<JsonElement> LogoutIdentityAsync()
    {
        var engine = RequireEngine();
        var result = await engine.IdentityLogoutAsync();
        Add(TranscriptKind.Notice, "已退出登录：本地凭据与浏览器登录态都已清除，可以换账号登录");
        await RefreshStatusAsync(force: true);
        return result;
    }

    // ---------- 浏览器登录 ----------

    /// <summary>起一个专用浏览器让用户登录，返回浏览器信息。</summary>
    public Task<JsonElement> StartBrowserLoginAsync() =>
        RequireEngine().IdentityLoginStartAsync();

    /// <summary>轮询登录状态。state：waiting / settling / ready / no_browser / error。</summary>
    public Task<JsonElement> PollBrowserLoginAsync() =>
        RequireEngine().IdentityLoginPollAsync();

    /// <summary>
    /// 取走凭据并落盘。引擎会**验证一次**、**关掉浏览器**、并按新身份重建运行时。
    ///
    /// **DeepSeek 侧不需要 label** —— 账号名是 best-effort，认不出也不影响主流程
    /// （`identity.account_label` 本就是可由用户手填的展示字段）。
    /// </summary>
    public async Task<JsonElement> FinishBrowserLoginAsync()
    {
        var engine = RequireEngine();
        var result = await engine.IdentityLoginFinishAsync();
        var ok = result.TryGetProperty("ok", out var o) && o.ValueKind == JsonValueKind.True;
        var detail = GetString(result, "detail");

        if (ok)
        {
            var label = GetString(result, "account_label");
            Add(TranscriptKind.Notice,
                "已通过浏览器取到凭据并验证通过" + (label.Length > 0 ? $" · {label}" : ""));
            await engine.StartAsync();
        }
        else
        {
            Add(TranscriptKind.Error, $"取凭据失败：{detail}");
        }
        await RefreshStatusAsync(force: true);
        return result;
    }

    /// <summary>放弃这次登录并关掉专用浏览器。</summary>
    public Task<JsonElement> CancelBrowserLoginAsync() =>
        RequireEngine().IdentityLoginCancelAsync();

    // ---------- 模型 ----------

    /// <summary>
    /// 问一次本账号可用模型。
    /// 失败不抛：界面在引擎刚起来、还没 `init()` 完时就会问一次，
    /// 那一刻拿不到列表是正常态，调用方靠 `Ready` 决定要不要稍后重问。
    /// </summary>
    /// <summary>本机可选的传输档位。数据源是引擎的 `config.transports`。</summary>
    public async Task<TransportCatalog> ListTransportsAsync()
    {
        var engine = _engine;
        if (engine is null)
            return new TransportCatalog(Array.Empty<TransportOption>(), false, Status.Transport);

        try
        {
            var result = await engine.ConfigTransportsAsync();
            var list = new List<TransportOption>();
            if (result.TryGetProperty("items", out var arr) && arr.ValueKind == JsonValueKind.Array)
            {
                foreach (var it in arr.EnumerateArray())
                {
                    var name = GetString(it, "name");
                    if (name.Length == 0) continue;
                    list.Add(new TransportOption
                    {
                        Name = name,
                        Label = GetString(it, "label", name),
                        Available = !it.TryGetProperty("available", out var av) ||
                                    av.ValueKind != JsonValueKind.False,
                        Ready = !it.TryGetProperty("ready", out var rd) ||
                                rd.ValueKind != JsonValueKind.False,
                        Guest = it.TryGetProperty("guest", out var g) &&
                                g.ValueKind == JsonValueKind.True,
                        HasCookie = it.TryGetProperty("hasCookie", out var hc) &&
                                    hc.ValueKind == JsonValueKind.True,
                        Note = GetString(it, "note"),
                    });
                }
            }
            return new TransportCatalog(list, true, Status.Transport);
        }
        catch (Exception ex)
        {
            Log($"[档位] 列表获取失败：{ex.Message}", LogLevel.Warn);
            return new TransportCatalog(Array.Empty<TransportOption>(), false, Status.Transport);
        }
    }

    public async Task<ModelCatalog> ListModelsAsync()
    {
        var engine = _engine;
        if (engine is null) return new ModelCatalog(Array.Empty<ModelOption>(), false, Status.Model);

        try
        {
            var result = await engine.ModelsListAsync();
            var current = GetString(result, "current", Status.Model);
            var ready = result.TryGetProperty("ready", out var r) &&
                        r.ValueKind == JsonValueKind.True;

            var list = new List<ModelOption>();
            if (result.TryGetProperty("models", out var arr) && arr.ValueKind == JsonValueKind.Array)
            {
                foreach (var m in arr.EnumerateArray())
                {
                    var name = GetString(m, "name");
                    if (name.Length == 0) continue;
                    list.Add(new ModelOption
                    {
                        Name = name,
                        DisplayName = GetString(m, "display_name", name),
                        Description = GetString(m, "description"),
                        IsAvailable = !m.TryGetProperty("is_available", out var av) ||
                                      av.ValueKind != JsonValueKind.False,
                    });
                }
            }

            Status.Model = current;
            return new ModelCatalog(list, ready, current);
        }
        catch (Exception ex)
        {
            Log($"[模型] 列表获取失败：{ex.Message}", LogLevel.Warn);
            return new ModelCatalog(Array.Empty<ModelOption>(), false, Status.Model);
        }
    }

    /// <summary>
    /// 切换本会话使用的模型（空串 = 账号默认）。
    /// **不重启引擎** —— 引擎侧只写配置，模型是 per-chat 参数，下一轮发送生效。
    /// </summary>
    public async Task SetModelAsync(string name)
    {
        var engine = _engine;
        if (engine is null) return;

        try
        {
            var result = await engine.ModelSetAsync(name ?? "");
            Status.Model = GetString(result, "model");
            var validated = result.TryGetProperty("validated", out var v) &&
                            v.ValueKind == JsonValueKind.True;
            var shown = Status.Model.Length == 0 ? "账号默认" : Status.Model;

            Add(TranscriptKind.Notice, validated
                ? $"已切换模型 · {shown}"
                : $"已记录模型 · {shown}（引擎未就绪，下一轮发送时生效）");
            await RefreshStatusAsync(force: true);
        }
        catch (Exception ex)
        {
            Add(TranscriptKind.Error, $"切换模型失败：{ex.Message}");
            Log($"[模型] 切换失败：{ex.Message}", LogLevel.Bad);
        }
    }

    /// <summary>
    /// 切换会话形态：一般/临时对话、是否思考模式。
    /// **不重启引擎** —— 两者都是 per-turn 参数，下一轮发送就生效。
    /// 唯一的例外：`temporary` 一变，引擎会**轮换会话**（同一段对话不该中途
    /// 改变「留不留痕」——否则前半段进历史、后半段不进，语义混乱）。
    /// </summary>
    public async Task SetChatAsync(bool? temporary = null, bool? extendedThinking = null,
                                   bool? searchEnabled = null, bool? showThinking = null)
    {
        var engine = _engine;
        if (engine is null) return;

        var patch = new Dictionary<string, object?>();
        if (temporary is { } t) patch["temporary"] = t;
        if (extendedThinking is { } th) patch["extended_thinking"] = th;
        if (searchEnabled is { } se) patch["search_enabled"] = se;
        if (showThinking is { } st) patch["show_thinking"] = st;
        if (patch.Count == 0) return;

        try
        {
            var result = await engine.ChatSetAsync(patch);
            Status.SessionTemporary = result.TryGetProperty("temporary", out var tempEl) &&
                                      tempEl.ValueKind == JsonValueKind.True;
            Status.ExtendedThinking = !result.TryGetProperty("extended_thinking", out var thinkEl) ||
                                      thinkEl.ValueKind != JsonValueKind.False;
            Status.SearchEnabled = result.TryGetProperty("search_enabled", out var searchEl) &&
                                   searchEl.ValueKind == JsonValueKind.True;
            Status.ShowThinking = !result.TryGetProperty("show_thinking", out var showEl) ||
                                  showEl.ValueKind != JsonValueKind.False;

            // **只有「会话形态」才配说「已切换会话形态」。**
            // `show_thinking` 是纯本地显示开关（引擎侧注释写得很清楚：
            // 它只管「看不看得见思考原文」，不改上游行为）。把它并进
            // 「一般对话 · 思考模式 · 联网开」这句话里，用户会以为自己
            // 切换了对话类型 —— 实际只是隐藏了一段文字。
            var changedShape = temporary is not null
                            || extendedThinking is not null
                            || searchEnabled is not null;

            if (changedShape)
            {
                var shape = Status.SessionTemporary
                    ? "临时对话（不写入 DeepSeek 历史）"
                    : "一般对话（写入 DeepSeek 历史）";
                var think = Status.ExtendedThinking ? "思考模式" : "标准模式";
                var net = Status.SearchEnabled ? " · 联网开" : "";
                var rotated = GetString(result, "rotated");
                Add(TranscriptKind.Notice, rotated.Length > 0
                    ? $"已切换会话形态 · {shape} · {think}{net}（会话已轮换，下一条另起新对话）"
                    : $"已切换会话形态 · {shape} · {think}{net}");
            }
            else if (showThinking is not null)
            {
                // 纯显示开关：一句话说清「只影响看不看得见」，不冒充形态切换。
                Add(TranscriptKind.Notice, Status.ShowThinking
                    ? "已显示思考过程（只影响本地显示，不影响上游是否思考）"
                    : "已隐藏思考过程（只影响本地显示，不影响上游是否思考）");
            }
            await RefreshStatusAsync(force: true);
        }
        catch (Exception ex)
        {
            Add(TranscriptKind.Error, $"切换会话形态失败：{ex.Message}");
            Log($"[会话形态] 切换失败：{ex.Message}", LogLevel.Bad);
        }
    }

    /// <summary>
    /// 切换工具权限档位 / dry-run。**不重启引擎**。
    ///
    /// 权限在引擎侧 `_execute` 每次分发前实时读配置（`permissions.check`），
    /// 没有缓存副本 —— 改完下一个工具调用立刻按新档位判定。这也是刻意不缓存
    /// 的原因：缓存一份就得操心失效，而读一次 dataclass 的开销可以忽略。
    ///
    /// 返回引擎的**实际生效值**（不是我们传进去的值）：非法档位会被引擎收紧成
    /// `read_only` 并附 `warning`，界面必须显示那个真实值，否则用户填了
    /// `readonly` 看到「已切换」却不知道它被当成什么了。
    /// </summary>
    public async Task<PermissionResult> SetPermissionsAsync(string? level = null, bool? dryRun = null)
    {
        var engine = _engine;
        if (engine is null) return new PermissionResult(false, "", false, "引擎尚未启动");

        var patch = new Dictionary<string, object?>();
        if (level is not null) patch["level"] = level;
        if (dryRun is { } dr) patch["dry_run"] = dr;
        if (patch.Count == 0) return new PermissionResult(true, Status.PermissionLevel, Status.DryRun, "");

        try
        {
            var result = await engine.PermissionsSetAsync(patch);
            var lv = GetString(result, "level");
            if (lv.Length > 0) Status.PermissionLevel = lv;
            Status.DryRun = IsTrue(result, "dry_run");
            var warning = GetString(result, "warning");

            Log($"[权限] {Status.PermissionLevel}" +
                (Status.DryRun ? " · dry-run 开" : ""), LogLevel.Ok);
            return new PermissionResult(true, Status.PermissionLevel, Status.DryRun, warning);
        }
        catch (Exception ex)
        {
            Log($"[权限] 切换失败：{ex.Message}", LogLevel.Bad);
            return new PermissionResult(false, Status.PermissionLevel, Status.DryRun, ex.Message);
        }
    }

    private EngineClient RequireEngine() =>
        _engine ?? throw new InvalidOperationException("引擎尚未启动");

    // ---------- 引擎事件 ----------

    private void OnEngineEvent(string kind, JsonElement data) =>
        _ui.BeginInvoke(() => HandleEvent(kind, data));

    private void OnEngineFault(string text) =>
        _ui.BeginInvoke(() => Log(text, LogLevel.Faint));

    private void HandleEvent(string kind, JsonElement data)
    {
        switch (kind)
        {
            case "delta":
                _thinking = null;
                // 正文开始流了 —— 思考块该收起来了。它不删（思考过程是这一轮的
                // 记录，用户可能还想回头看），只折叠成一行。
                CollapseThought();
                AppendDelta(GetString(data, "text"));
                break;

            case "turn_start":
                FlushPacer();
                _streaming = null;
                _thinking = null;
                _thought = null;
                Add(TranscriptKind.TurnDivider, $"第 {GetInt(data, "turn")} 轮");
                // 每轮把「实际发出去的模型」写进监视器。
                // 这一行是专门为排障加的：模型是在下拉里选的、在配置里存的，
                // 但真正决定效果的是**这一轮发出去的那一个**。以前这条信息
                // 在界面上完全不可见，用户只能靠「回答得快不快、好不好」反推，
                // 一遇到异常就容易误判成「没调用到我选的模型」。
                Log($"[模型] 本轮使用 {(Status.Model.Length == 0 ? "账号默认" : Status.Model)}" +
                    $" · {(Status.SessionTemporary ? "临时对话（不写入 DeepSeek 历史）" : "一般对话（写入 DeepSeek 历史）")}" +
                    $" · {(Status.ThinkingMode.Length == 0 ? "思考模式" : Status.ThinkingMode)}",
                    LogLevel.Faint);
                break;

            // 本地链路的耗时，在提示词交出去的那一刻报一次。
            //
            // 为什么专门给一行：实测（engine/tools/probe_presend_latency.py）
            // 本地前置只花 0–1.3 秒，而上游第一个字节要等 14–47 秒 —— 但这两段
            // 在界面上**长得一模一样**，都是「没动静」。用户只能猜，最自然的猜法
            // 就是「还没发出去」，于是报成「从软件到发提示词出去间隔太久」。
            // 把可核对的数字摆出来，用户自己就能分清是哪一段慢。
            case "sent":
                Log($"[发出] 本地耗时 {GetInt(data, "local_total_ms")}ms" +
                    $"（其中闸门 {GetInt(data, "gate_wait_ms")}ms）· 已交给上游，等它开口",
                    LogLevel.Faint);
                break;

            // PoW（工作量证明）的本地耗时 —— **它不在上面那条 `sent` 里**。
            //
            // 引擎的 `sent` 是在调用 `transport.send()` **之前**发出的，而取
            // challenge 与解 PoW 都发生在 `send()` **内部** —— 所以 `sent` 报的
            // `local_total_ms` / `gate_wait_ms` 一个都不含它。这段是**纯本地空耗**
            // （实测单次数秒），而且正好落在「按下回车 → 请求真的发出」之间：
            // 不单独报出来，用户在那几秒里只能看到「点了没反应」。
            case "pow_stats":
                Log($"[PoW] 解算 {GetDouble(data, "pow_ms"):0}ms" +
                    $"（取挑战 {GetDouble(data, "challenge_ms"):0}ms" +
                    $" · 首片返回 {GetDouble(data, "pool_startup_ms"):0}ms）" +
                    $" · {GetString(data, "mode")}×{GetInt(data, "workers")} 进程" +
                    $" · 命中位置 {GetInt(data, "trials")}",
                    LogLevel.Faint);
                break;

            // 附件上传完成。**这条以前根本不存在** —— 引擎一直在发 `file_upload`，
            // 而这里的 switch 没有这个 case，事件被静默吞掉。
            //
            // 为什么必须有：上传走的是**另一份** PoW（`target_path` 不同，不能复用
            // completion 那份），实测 7 秒；引擎冷启动时进程池首次 spawn 叠加下来
            // 见过 31 秒。这段时间里界面上一条输出都没有 —— 用户按下回车，
            // 然后盯着完全静止的界面等半分钟。上面那条 `sent` 救不了它：
            // `sent` 在 `transport.send()` **之前**发出，上传发生在 `send()` 内部。
            //
            // **等级是 `Soft` 不是 `Faint`，这一点是刻意的**：监视器默认勾着
            // 「只看重要」（`#monHideFaint`），而 `.mon-line.mon-dim{display:none}`
            // 是**真隐藏**不是变灰。写成 Faint 的话这行在默认视图里根本不存在 ——
            // 专门为「别让用户干等」加的日志，自己先被藏起来了。
            case "file_upload":
                Log($"[附件] 已上传 {GetInt(data, "count")} 个 · 耗时 {GetDouble(data, "upload_ms"):0}ms" +
                    FormatUploadedFiles(data),
                    LogLevel.Soft);
                break;

            // 思考通道的心跳。**没有这一条，思考模式在界面上等于「假死」**：
            // 上游先把思考 token 流完才吐正文，实测「只回复两个字」这种题也要
            // ~90 秒，而这段时间正文通道一个字节都没有 —— 用户看到的就是
            // 「我按了 Enter，然后什么都没有」。所以这里只在对话流里放一条
            // 会自己更新的「思考中…」，正文一到就撤掉。
            case "thinking":
                ShowThinking(
                    GetInt(data, "chars"),
                    GetString(data, "tail"),
                    GetInt(data, "waited"));
                break;

            // 思考过程的**原文**（`chat.show_thinking=true` 时走这条）。
            //
            // 与上面那条心跳是**互斥**的两条路：引擎按配置只发其中一种。
            // `text` 是增量（和正文的 `delta` 同一套路子），界面自己拼 ——
            // 思考常有几千字，每帧推全文是 O(n²) 的流量。
            // `text` 为空时说明上游还没开口，那时 `waited` 才是主角。
            case "thought":
                ShowThought(
                    GetInt(data, "turn"),
                    GetString(data, "text"),
                    GetInt(data, "chars"),
                    GetInt(data, "waited"));
                break;

            // 一轮结束。**思考块的定稿点** —— 引擎在这里给出本轮思考全文。
            //
            // 为什么不能只靠 `thought` 增量拼：增量是按 0.5 秒节流推的，
            // 最后不足一个节流窗口的那一小段永远推不出去；而且有些帧根本
            // 不走增量通道（整段只在快照里 / `SET` 整体替换）。以引擎给的
            // 全文为准，界面上就不会出现「思考过程少了个尾巴」。
            case "turn_end":
                FlushPacer();
                FinalizeThought(GetString(data, "thoughts"));
                // 把本轮的服务端消息 id 落到**最后一块**助手气泡上。
                //
                // 为什么是「最后一块」而不是「每块」：一次任务里工具调用会把
                // 正文切成好几块气泡，但它们全都来自服务端**同一条**消息 ——
                // 朗读接口按 message_id 取的是整条消息的语音。给每块都挂上，
                // 界面上就会排出一列功能完全相同的朗读按钮。
                //
                // `FlushPacer()` 必须在前：定稿的 id 要落在**拼完的**那块上，
                // 否则被 pacer 扣着的最后几个字会另起一块，id 就挂到了半截气泡。
                //
                // 本轮没有正文（只调了工具）时 `_streaming` 为 null —— 不挂，
                // 那种气泡本来也没有可朗读的东西。
                if (_streaming is { } last && GetString(data, "rid") is { Length: > 0 } rid)
                {
                    last.MessageId = rid;
                    ItemChanged?.Invoke(last);
                }
                break;

            // 工具调用的三段式：start（解析出来的那一刻）→ progress（执行中每 1.5 秒）
            // → done（带退出码与输出）。旧实现只发一条 tool_call，卡片在调用解析
            // 出来的瞬间就画完了，之后本地命令跑几十秒、下一轮上游又要十几秒，
            // 界面上就是两块死寂 —— 「停顿感」这个报障正是这个。
            case "tool_start":
            {
                var seq = GetInt(data, "seq");
                // 与旧的 tool_call 行为一致：调用一出现就结束当前流式块，
                // 之后上游再吐的文字另起一块（否则会和调用前的文字黏在一起）。
                // 先吐干净队列：不吐的话剩字会和旧块一起被下面那行 null 丢掉。
                FlushPacer();
                _streaming = null;
                _thinking = null;
                var item = Add(
                    TranscriptKind.ToolCall,
                    GetString(data, "kind"),
                    GetString(data, "target"),
                    it => it.Tool = new ToolRunState
                    {
                        Seq = seq,
                        Turn = GetInt(data, "turn"),
                        Kind = GetString(data, "kind"),
                        Target = GetString(data, "target"),
                        Payload = GetString(data, "payload"),
                    });
                _toolBySeq[seq] = item;
                _activeToolSeq = seq;
                _liveOutput.Clear();
                _liveCapped = false;
                break;
            }

            case "tool_progress":
            {
                if (!_toolBySeq.TryGetValue(GetInt(data, "seq"), out var item) ||
                    item.Tool is null) break;
                item.Tool.ElapsedMs = GetInt(data, "elapsed_ms");
                ItemChanged?.Invoke(item);
                break;
            }

            case "tool_done":
            {
                if (!_toolBySeq.TryGetValue(GetInt(data, "seq"), out var item) ||
                    item.Tool is null) break;
                item.Tool.Running = false;
                item.Tool.ElapsedMs = GetInt(data, "duration_ms");
                item.Tool.ExitCode = GetInt(data, "exit_code");
                item.Tool.Output = GetString(data, "output");
                item.Tool.Rejected = data.TryGetProperty("rejected", out var rj) &&
                                     rj.ValueKind == JsonValueKind.True;
                if (_activeToolSeq == item.Tool.Seq) _activeToolSeq = 0;
                ItemChanged?.Invoke(item);
                break;
            }

            case "stream":
            {
                var text = GetString(data, "text");
                Log(text, LogLevel.Soft);
                // 执行器的实时输出行同时喂给「正在跑的那张工具卡片」——
                // 这是「持续有输出在工作」最直接的来源：命令边跑边出字，
                // 卡片就边涨，而不是等它跑完才一次性出现。
                // （边界：异步泳道在后台跑时也可能往这里推行，会被算到当前
                //   正在执行的那次调用上。单次派发是串行的，影响可忽略。）
                if (_activeToolSeq != 0 &&
                    _toolBySeq.TryGetValue(_activeToolSeq, out var live) &&
                    live.Tool is { Running: true })
                {
                    if (_liveOutput.Length < MaxLiveOutputChars)
                    {
                        _liveOutput.AppendLine(text);
                        if (_liveOutput.Length >= MaxLiveOutputChars && !_liveCapped)
                        {
                            _liveCapped = true;
                            _liveOutput.AppendLine(
                                "（实时输出较长，此处仅保留前约 2 万字；" +
                                "完成后点卡片“展开全文”看引擎回传的全文）");
                        }
                        live.Tool.Output = _liveOutput.ToString();
                        ItemChanged?.Invoke(live);
                    }
                }
                break;
            }

            case "gate":
                Add(TranscriptKind.Gate, GetString(data, "reason"), GetString(data, "layer"));
                break;

            case "breaker":
                Add(TranscriptKind.Breaker, GetString(data, "reason"));
                Log($"[熔断] {GetString(data, "reason")}", LogLevel.Bad);
                break;

            case "warning":
                Add(TranscriptKind.Notice, $"L1 身份层告警：{data.GetRawText()}");
                break;

            case "notice":
                Add(TranscriptKind.Notice, GetString(data, "text"));
                Log($"[提示] {GetString(data, "text")}", LogLevel.Faint);
                break;

            case "transport_unavailable":
                Add(TranscriptKind.Error, $"传输不可用：{GetString(data, "error")}");
                Log($"[传输不可用] {GetString(data, "error")}", LogLevel.Bad);
                Status.Transport = "不可用";
                Status.TransportReady = false;
                StatusChanged?.Invoke();
                // **凭据类失败就把身份对话框摆到用户面前。**
                // 他刚看到「未登录」，第一件事就是找「怎么修」—— 不该让他再去
                // 二级菜单里找第二次。非凭据类的失败（网络等）不弹，那是另一种问题。
                if (data.TryGetProperty("auth", out var authEl) &&
                    authEl.ValueKind == JsonValueKind.True)
                {
                    Log("[身份] 凭据不可用 —— 打开身份对话框", LogLevel.Warn);
                    IdentityNeedsLogin?.Invoke();
                }
                break;

            case "session_rotate":
                Add(TranscriptKind.Info, $"会话轮换：{GetString(data, "reason")}");
                break;

            // 注：gemini 版这里有一个 `account_refresh` 事件分支（无头浏览器重取 cookie
            // 要跑十几秒，进度只能走事件）。**DeepSeek 侧没有这个事件** ——
            // 刷新凭据多数情况只是验一次（一次极轻的 `create_pow_challenge`），
            // 同步 await 就够了（见 `RefreshIdentityAsync`）。所以这里整段删掉，
            // 不保留一个永远收不到的分支。

            case "error":
                Add(TranscriptKind.Error, $"传输错误：{GetString(data, "error")}");
                break;

            case "status_error":
                Log($"[状态] {GetString(data, "error")}", LogLevel.Warn);
                break;

            // 引擎在一次任务里有**两条**结束事件，而且带的是同一份 report：
            //   · `run_end`       —— `agent.run()` 返回之前，引擎自己发（`agent.py`）；
            //   · `run.completed` —— RPC 层在 `run.async` 的后台任务返回之后补发
            //                        （`__main__.py` 的 `_job()`）。
            // 原先两条共用一个 case，于是「本轮结束：… N 次工具调用」这条摘要
            // 连着出现两次 —— 用户报的就是这个。
            //
            // 修法是**按内容去重**，而不是「只认其中一个事件」：
            // 只认 `run_end` 的话，万一哪天引擎不再发它（比如换了运行入口），
            // 摘要就再也不显示了 —— 那是一种更糟的沉默失败。
            // 按内容去重，两条路谁先到都能显示，只有完全相同的那条被吃掉。
            case "run_end":
            case "run.completed":
            {
                FlushPacer();
                // 兜底收尾：正常路径上 `turn_end` 已经收过了。但错误退出
                // （`error` / `stop`）不会走 `turn_end`，思考块会一直挂着
                // 「思考中…」—— 收一下，免得看起来像还在跑。
                CollapseThought();
                SetRunning(false);
                var summary = Summarize(data);
                if (summary != _lastRunSummary)
                {
                    _lastRunSummary = summary;
                    Add(TranscriptKind.Summary, summary);
                }
                // 任务结束强制刷一次：额度是在**每次生成之后**由上游更新的
                // （`client.py` 的 `_fetch_usage_info()`），所以这是「刚花掉多少」
                // 最该显示出来的时刻。空闲时不必轮询 —— 见 RefreshStatusAsync 的注释。
                _ = RefreshStatusAsync(force: true);
                break;
            }

            case "run.failed":
                FlushPacer();
                SetRunning(false);
                Add(TranscriptKind.Error, $"运行失败：{GetString(data, "error")}");
                break;

            case "stop":
                FlushPacer();
                Add(TranscriptKind.Notice,
                    $"已停止：{GetString(data, "reason")} · {GetString(data, "detail")}");
                break;

            // 附件上传失败。**必须单独接住，不能让 run.failed 一肩挑**。
            //
            // 上传失败是**本地可判定的**失败：文件不存在 / 是目录 / 空文件 /
            // 超 100MB / 一次超过 50 个 —— 传输层在发请求之前就抛了，一个字节
            // 都没上网。用户要改的是「挑哪个文件」，不是「换个模型再试」。
            // 只给一句「运行失败：<异常>」，他会去查网络、查额度、查账号，
            // 而真正的原因就在附件条上。
            //
            // ⚠️ 这里**曾经**有一个 `case "file_upload_error"` —— 已删除。
            //    引擎从来不发这个事件（`grep file_upload engine/` 只有一处，
            //    在 `agent.py:657` 发的是成功事件 `file_upload`）。附件出问题时
            //    引擎抛 `TransportError(config=True)`，由 `case "error"` 接住。
            //    那条分支是我照着「应该有个失败事件」的想象写出来的，从未触发过。
            //
            //    真正的可读性问题改在**上游**解决：`SubmitAsync` 现在会在发送
            //    之前自己校验一遍文件（见那里的 `ValidateFiles`），本地就能判定的
            //    错误不会走到引擎，也就不会显示成「传输错误」这种误导性措辞。

            case "ready":
                Log($"传输通道就绪 · {GetString(data, "transport")}", LogLevel.Ok);
                Status.Mode = GetString(data, "mode");
                StatusChanged?.Invoke();
                break;
        }
    }

    // ---------- 流水 ----------

    private void AppendDelta(string text)
    {
        if (string.IsNullOrEmpty(text)) return;

        // 队列空且定时器没转 = 平时：直吐，一字不压（快路径零延迟）。
        // 否则 = burst 中：进队列，由 tick 匀速放出。
        if (_pacer.Pending == 0 && _paceTimer is null)
        {
            AppendNow(text);
            return;
        }
        _pacer.Enqueue(text);
        StartPaceTimer();
    }

    /// <summary>
    /// AppendDelta 的原 body 改名：把字真正拼到流式块上并通知视图。
    /// 每个 delta 都重设全文（TextBlock 重排），所以 tick 把 burst 摊平后，
    /// 这里的调用次数也随之减少 —— 匀速的第二个好处：布局次数变少。
    /// </summary>
    private void AppendNow(string text)
    {
        if (_streaming is null)
        {
            var item = new TranscriptItem
            {
                Seq = ++_seq,
                Kind = TranscriptKind.Assistant,
                Text = text,
            };
            // 先挂上 _streaming 再广播：视图在 ItemAdded 里就要能认出
            // 「这一条是流式中的」，否则半路接上来的窗口会在下一段增量
            // 到来时再画一块重复的消息。
            _streaming = item;
            _transcript.Add(item);
            TrimTranscript();
            ItemAdded?.Invoke(item);
            return;
        }

        _streaming.Text += text;
        ItemChanged?.Invoke(_streaming);
    }

    private void StartPaceTimer()
    {
        if (_paceTimer is not null) return;
        // 调用方（HandleEvent）已在 UI 线程，这里建的 timer 天然跑在 UI 线程。
        _paceTimer = new DispatcherTimer(
            TimeSpan.FromMilliseconds(40),
            DispatcherPriority.Background,
            (_, _) =>
            {
                var chunk = _pacer.TakeTick();
                if (chunk.Length == 0) { StopPaceTimer(); return; }
                AppendNow(chunk);
            },
            _ui);
        _paceTimer.Start();
    }

    private void StopPaceTimer()
    {
        _paceTimer?.Stop();
        _paceTimer = null;
    }

    /// <summary>
    /// 把队列里剩的字全吐到当前流式块上并停表。轮末/工具开始/失败/停止的
    /// 必经出口 —— 不在这里吐的字会和旧块一起被 `_streaming = null` 丢掉。
    /// </summary>
    private void FlushPacer()
    {
        var rest = _pacer.Flush();
        StopPaceTimer();
        if (rest.Length > 0) AppendNow(rest);
    }

    /// <summary>
    /// **仅供 `--selfcheck`**：强制另起一条流式正文，等价于 `AppendDelta` 里
    /// 「`_streaming is null` → 建新块」那条分支。
    ///
    /// 为什么不直接调 `AppendDelta`：它只在 `_streaming` 为空时才建新块，
    /// 否则是把字拼到旧块上、只发 `ItemChanged`。自检里 `App.Host` 是共享的，
    /// 上一个窗口可能留下非空的 `_streaming`，那样这一步会**静默地什么都没测**。
    /// 先置空再走，结果才是确定的。
    /// </summary>
    internal void SelfCheckBeginStreaming(string text)
    {
        FlushPacer();
        _streaming = null;
        AppendDelta(text);
    }

    /// <summary>
    /// 在对话流里放/更新一条「思考过程」块。思考原文在 <see cref="TranscriptItem.Text"/>，
    /// 视图那边是一个可折叠的 <c>Expander</c>：思考时展开着滚，正文一到就收起。
    ///
    /// 为什么不是「每来一条事件就加一行」：思考按 0.5 秒一帧推，一轮下来几百条 ——
    /// 那样会把对话流刷爆。这里始终更新**同一条**，和工具卡片是同一个思路。
    ///
    /// 为什么要有「text 为空」的那一态：请求刚发出去时上游可能长时间一个字节
    /// 都不流（2026-10-14 实测 >120 秒连思考增量都没有）。那段时间块里只有
    /// 一行「已发出，等上游开始输出…（N 秒）」—— 没有它，界面就是彻底死寂，
    /// 用户分不清「在跑」还是「坏了」。
    /// </summary>
    private void ShowThought(int turn, string text, int chars, int waited)
    {
        if (_thought is null)
        {
            // 正文已经开始了，这条只是「还在等上游」心跳的迟到残留 —— 不建块。
            // （和 `ShowThinking` 里那条同样的守卫，理由见那里的注释。）
            if (_streaming is not null) return;
            _thought = Add(TranscriptKind.Thought, "", "", it => it.Thought = new ThoughtState
            {
                Turn = turn,
            });
        }

        var state = _thought.Thought!;
        if (text.Length > 0)
        {
            // 增量拼接。**不能赋值覆盖** —— 那是把前面几秒的思考丢掉。
            _thought.Text += text;
            state.Chars = chars > 0 ? chars : _thought.Text.Length;
            state.Streaming = true;
            state.Waited = 0;
        }
        else
        {
            // 还没开口：只更新「等了多久」。字数以引擎报的为准（此时通常为 0）。
            state.Waited = waited;
            if (chars > 0) state.Chars = chars;
        }
        ItemChanged?.Invoke(_thought);
    }

    /// <summary>正文开始流了 —— 把思考块收起来（折叠，不删除）。</summary>
    private void CollapseThought()
    {
        if (_thought?.Thought is not { Streaming: true } state) return;
        state.Streaming = false;
        ItemChanged?.Invoke(_thought);
    }

    /// <summary>
    /// 一轮的定稿：以引擎给的全文为准覆盖掉拼出来的增量，并收起。
    ///
    /// **必须以引擎的为准**：增量是按 0.5 秒节流推的，最后不足一个窗口的那
    /// 一小段推不出去；而且「整段只在快照帧里 / `SET` 整体替换」这两条路径
    /// 根本不调 `on_thought`。只靠增量拼，界面就会少一截尾巴。
    /// </summary>
    private void FinalizeThought(string full)
    {
        if (_thought is null) return;
        var state = _thought.Thought!;
        if (full.Length > 0)
        {
            _thought.Text = full;
            state.Chars = full.Length;
        }
        state.Streaming = false;
        ItemChanged?.Invoke(_thought);
    }

    /// <summary>
    /// 在对话流里放/更新一条等待提示。只更新文本，不追加新条 ——
    /// 否则 90 秒里会刷出几十条。正文或工具调用一到，`_thinking` 就被清掉。
    ///
    /// 三态：
    ///   · 上游还没吐任何思考增量（chars == 0）→「已发送，等待上游响应…（N 秒）」
    ///   · 上游开始流思考了                  →「思考中…（已 N 字）」
    ///   · 等太久（waited >= 60）            → 追加一句**可操作**的提示
    /// 第一态是必需的：2026-10-14 实测上游在思考模式下可能**一个字节都不流**
    /// （连思考增量都没有），120 秒后 recovery 超时。那段时间界面要是没有这一条，
    /// 用户看到的就是彻底死寂 —— 分不清「在跑」还是「坏了」。
    ///
    /// 第三态也是必需的，而且比第二态更值钱：同一批实测里，成功的回答只要
    /// 9.9–50.7 秒，所以过了 60 秒基本就不是「慢」而是「上游把这轮丢了」。
    /// 那种情况下干等最多会拖到十几分钟（上游 `@running(retry=5)` 会重发 5 次），
    /// 所以必须给一条**用户当下就能动手**的出路，而不是让他盯着秒数涨。
    /// </summary>
    private void ShowThinking(int chars, string tail, int waited)
    {
        // 正文一旦开始流，「还在等上游」这条提示就失去意义。而心跳是每 3 秒
        // 发一次、直到轮末才取消的 —— 于是**几乎每轮末尾**都会在正文之后
        // 再推来一条 thinking；`delta` 分支已把 `_thinking` 置空，这里就会
        // 新建一条「思考中…」并**一直留在对话流里**，用户读到的就是
        // 「回复说到一半没了」（2026-10-14 实测复现，见 probe_agent_path.py
        // 的事件序列：`… delta, delta, thinking, turn_end`）。
        // 所以正文一开始流，本提示直接退出。
        if (_streaming is not null) return;

        string text;
        if (chars > 0)
            text = $"思考中…（已 {chars} 字）";
        else if (waited >= 60)
            // 说「可能」而不是断言原因：这是给用户的逃生口，不是诊断结论。
            text = $"上游 {waited} 秒没回一个字 —— 可能这轮被丢弃了。" +
                   "建议把模型换成 4 Argon（或把「思考程度」调回标准模式）再发一次。";
        else
            // 「已发出」三个字是刻意的：实测本地只花 0–1.3 秒，剩下的
            // 14–47 秒全在上游那一侧。原先写「已发送，等待上游响应」，
            // 读起来仍像「还没发出去」—— 用户报的就是这个。
            text = $"已发出，等上游开始输出…（{waited} 秒）";
        if (_thinking is null)
        {
            _thinking = Add(TranscriptKind.Thinking, text);
            return;
        }
        if (_thinking.Text == text) return;   // 文本没变就别刷事件
        _thinking.Text = text;
        ItemChanged?.Invoke(_thinking);
    }

    private TranscriptItem Add(
        TranscriptKind kind, string text, string detail = "",
        Action<TranscriptItem>? init = null)
    {
        var item = new TranscriptItem
        {
            Seq = ++_seq,
            Kind = kind,
            Text = text ?? "",
            Detail = detail ?? "",
        };
        // init 必须在广播之前跑：视图收到 ItemAdded 时就要看到完整状态，
        // 否则工具卡片会先按「没有状态」画一遍，再靠一次额外广播纠正 —— 会闪。
        init?.Invoke(item);
        _transcript.Add(item);
        // 先广播删除、再广播新增 —— 顺序反了会让视图短暂出现
        // 「上限 + 1」个节点（先加后删）。单条无感，但连续补批时
        // 每一批都会多闪一次。
        TrimTranscript();
        ItemAdded?.Invoke(item);
        return item;
    }

    private void TrimTranscript()
    {
        while (_transcript.Count > MaxTranscript)
        {
            var oldest = _transcript[0];
            if (ReferenceEquals(oldest, _streaming)) _streaming = null;
            if (ReferenceEquals(oldest, _thinking)) _thinking = null;
            if (ReferenceEquals(oldest, _thought)) _thought = null;
            // 序号表的条目要跟着一起清，否则它会随着长会话无限增长。
            if (oldest.Tool is { } tool) _toolBySeq.Remove(tool.Seq);
            _transcript.RemoveAt(0);
            ItemRemoved?.Invoke(oldest);
        }
    }

    private void Log(string text, LogLevel level)
    {
        if (string.IsNullOrWhiteSpace(text)) return;
        var item = new LogItem { Seq = ++_seq, Text = text, Level = level };
        _monitor.Add(item);
        while (_monitor.Count > MaxMonitor) _monitor.RemoveAt(0);
        Logged?.Invoke(item);
    }

    private void SetRunning(bool value)
    {
        if (Running == value) return;
        // 新一轮开始 —— 允许再次显示「本轮结束」摘要。
        // 不清的话，两次统计**完全相同**的任务（比如连着问两个都是
        // 1 轮 0 调用的小问题）第二条摘要会被去重误吞。
        if (value) _lastRunSummary = "";
        Running = value;
        RunningChanged?.Invoke(value);
    }

    // ---------- 小工具 ----------

    private static string Summarize(JsonElement data)
    {
        var stop = GetString(data, "stop_reason");
        var text = $"本轮结束：{stop} · {GetInt(data, "turns")} 轮 · " +
                   $"{GetInt(data, "tool_calls")} 次工具调用";
        var gate = GetString(data, "gate_reason");
        return string.IsNullOrWhiteSpace(gate) ? text : $"{text} · {gate}";
    }

    private static string Short(string cid) =>
        cid[..Math.Min(8, cid.Length)];

    /// <summary>
    /// 把 `file_upload` 事件里的文件名拼成「 · a.txt, b.pdf」。
    /// 没有文件名（服务端没回显）时返回空串，不硬凑。
    /// </summary>
    private static string FormatUploadedFiles(JsonElement data)
    {
        if (!data.TryGetProperty("files", out var arr) || arr.ValueKind != JsonValueKind.Array)
            return "";
        var names = arr.EnumerateArray()
            .Select(f => GetString(f, "file_name"))
            .Where(n => n.Length > 0)
            .ToArray();
        return names.Length == 0 ? "" : " · " + string.Join("、", names);
    }

    private static string GetString(JsonElement data, string name, string fallback = "") =>
        data.TryGetProperty(name, out var el) && el.ValueKind == JsonValueKind.String
            ? el.GetString() ?? fallback
            : fallback;

    private static int GetInt(JsonElement data, string name) =>
        data.TryGetProperty(name, out var el) && el.ValueKind == JsonValueKind.Number
            ? el.GetInt32()
            : 0;

    //: 浮点取值。**不能用 `GetInt` 代替** —— 它走 `GetInt32()`，遇到 `123.4`
    //: 这种带小数的 JSON 数字会抛 `FormatException`（而 `pow_ms` 之类正是小数）。
    private static double GetDouble(JsonElement data, string name) =>
        data.TryGetProperty(name, out var el) && el.ValueKind == JsonValueKind.Number
            ? el.GetDouble()
            : 0;

    private static bool IsTrue(JsonElement data, string name) =>
        data.TryGetProperty(name, out var el) && el.ValueKind == JsonValueKind.True;
}

/// <summary>
/// <see cref="AgentHost.SetPermissionsAsync"/> 的返回值。
///
/// 刻意带上 <see cref="Warning"/>：引擎对非法档位是**收紧**（不报错）的 ——
/// 用户填 `readonly` 会得到 `read_only`，但界面上「已切换」的回执若不带一句
/// 说明，用户就不知道自己的输入被改写过。
/// </summary>
public sealed record PermissionResult(
    bool Ok, string Level, bool DryRun, string Warning);
