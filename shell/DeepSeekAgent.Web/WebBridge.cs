using System.Text.Encodings.Web;
using System.Text.Json;
using System.Windows.Threading;

namespace DeepSeekAgent.Desktop;

/// <summary>
/// 外壳（C#）与前端（JS）之间的桥。
///
/// 协议只有两个方向，都是 JSON：
///   C# → JS：<c>PostWebMessageAsJson</c> 推事件流（条目 / 日志 / 状态）
///   JS → C#：<c>WebMessageReceived</c> 收命令（发送 / 取消 / 切会话 / 换档位）
///
/// 为什么条目序列化是**手工挑字段**、而不是直接序列化 <see cref="TranscriptItem"/>：
/// 那会把 <c>PropertyChanged</c> 事件、以及 <c>Tool</c>/<c>Thought</c> 的整棵对象图
/// 一起带上；更要紧的是字段名会跟着 C# 属性名走 —— 将来重构改个属性名，
/// 协议就悄悄变了，而前端那边只会静默拿到 undefined。手工映射让契约显式。
/// </summary>
internal sealed class WebBridge
{
    private readonly AgentHost _host;
    private readonly Dispatcher _ui;
    private readonly Action<string> _post;

    public WebBridge(AgentHost host, Dispatcher ui, Action<string> post)
    {
        _host = host;
        _ui = ui;
        _post = post;
    }

    /// <summary>
    /// 窗口操作（最小化 / 最大化 / 关闭 / 起拖）。
    ///
    /// 为什么要有这条通道：本窗口是 <c>WindowStyle="None"</c> 自绘标题栏，
    /// 前端那排窗口按钮和标题栏拖拽都在 WebView2 里 —— 那里收不到 WPF 的
    /// 命中测试，必须绕一圈回到 C# 才能真正操作窗口。
    /// 由 <c>MainWindow</c> 接线；未接线时静默忽略（比如 mock 环境）。
    /// </summary>
    public Action<string>? WindowOp { get; set; }

    /// <summary>
    /// 弹出文件选择器，返回绝对路径列表；用户取消时返回空列表。
    ///
    /// **为什么必须由 C# 弹窗，而不是 HTML 的 <c>&lt;input type="file"&gt;</c>**：
    /// WebView2 里选到的 <c>File</c> 对象**没有本地路径** —— 浏览器安全模型
    /// 只给内容，不给路径。而引擎的上传接口要的是磁盘上的文件（它自己读字节、
    /// 算 x-file-size）。唯一能把路径递过去的办法是把整个文件 base64 穿过
    /// WebMessage 桥 —— 100MB 上限的文件会变成 133MB 的字符串，桥撑不住。
    /// 所以只能在宿主侧弹窗，把路径交给前端，再由前端原样发回来。
    ///
    /// 与 <see cref="WindowOp"/> 同一个模式：由 <c>MainWindow</c> 接线，
    /// 未接线时返回空表（mock 环境 / 单测）。
    /// </summary>
    public Func<IReadOnlyList<string>>? PickFiles { get; set; }

    // UnsafeRelaxedJsonEscaping：中文不转成 \uXXXX。
    // 安全性说明：产物只走 postMessage（JS 侧 JSON.parse），从不拼进 HTML，
    // 所以不转义 < > & 没有 XSS 面。前端渲染文本一律用 textContent。
    private static readonly JsonSerializerOptions Opts = new()
    {
        Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
    };

    private void Send(object payload)
    {
        var json = JsonSerializer.Serialize(payload, Opts);
        if (_ui.CheckAccess()) _post(json);
        else _ui.BeginInvoke(() => _post(json));
    }

    // ==================== C# → JS ====================

    /// <summary>握手：前端脚本就绪，可以开始灌数据了。</summary>
    public void SendHello() => Send(new { t = "ready" });

    public void SendHistory()
    {
        Send(new
        {
            t = "hist",
            items = _host.Transcript.Select(Item).ToArray(),
            logs = _host.Monitor.Select(Log).ToArray(),
            running = _host.Running,
            s = Status(_host.Status),
        });
    }

    public void SendItem(TranscriptItem item) => Send(new { t = "item", item = Item(item) });

    public void SendItemChanged(TranscriptItem item) => Send(new { t = "chg", item = Item(item) });

    /// <summary>最旧的一条被裁掉（`TrimTranscript` 超过 `MaxTranscript`）。
    ///
    /// 前端 `onItem` 是纯 Append，从不删节点。不推这条消息的话，
    /// 数据层裁到 600 条、DOM 会一直涨（实测 740 个），
    /// 用户往上翻还能看到已经不存在的历史。</summary>
    public void SendItemRemoved(TranscriptItem item) => Send(new { t = "rm", seq = item.Seq });

    public void SendCleared() => Send(new { t = "clear" });

    public void SendLog(LogItem log) => Send(new { t = "log", log = Log(log) });

    public void SendStatus() => Send(new
    {
        t = "status",
        running = _host.Running,
        s = Status(_host.Status),
    });

    /// <summary>
    /// 账号下的会话列表（`chat_session/fetch_page`）。点一条即接管（attachSession）。
    ///
    /// 这是**唯一**推给界面的会话列表。本地 `conversations/*.json` 不记录归属
    /// 账号 —— 换号后上个账号的对话会继续列在这里，点进去发消息时引擎会拿新
    /// token 去请求一个不属于它的 `chat_session_id`，服务端必然拒绝。所以本地
    /// 文件降级成接管后的缓存，不再作为列表来源（详见 `hello` 里的注释）。
    /// </summary>
    public void SendRemoteSessions() => Send(new
    {
        t = "convs",
        scope = "remote",
        items = _host.RemoteSessions.Select(c => new
        {
            // `id` 是**服务端** id —— attachSession / delRemote 用它。
            id = c.RemoteId,
            // `localId` 是接管后落地的**本地对话** id，未接管时为空。
            // 必须单独推：切换（switchConv）、重命名（renameConv）、
            // 同步（syncConv）三个动作在宿主侧都是按本地 id 工作的。
            // 早先只推了 id，前端无从得知本地 id —— 这三个动作因此根本
            // 接不上线（见 app.js 里会话菜单的注释）。
            localId = c.LocalId,
            kind = "remote",
            title = c.Title,
            active = c.Active,
            bound = c.Bound,
            turns = 0,
            preview = "",
            subtitle = c.Subtitle,
        }).ToArray(),
    });

    /// <summary>
    /// 让界面重新拉一次账号下的会话列表。
    /// 一轮跑完时调用 —— 新对话这时才拿到 `chat_session_id`，标题也刚由服务端生成。
    /// </summary>
    public void RefreshSessions() => _ = RefreshRemoteListAsync();

    /// <summary>
    /// 拉一次账号下的会话列表并推给界面（**要联网**）。
    ///
    /// 失败必须**明确告诉前端**，不能只推一个空列表 ——「账号下没有会话」和
    /// 「读不出来」是两件事：前者用户会以为自己没数据，后者他才会去查凭据。
    /// </summary>
    private async Task RefreshRemoteListAsync()
    {
        var ok = await _host.RefreshRemoteSessionsAsync();
        if (ok) SendRemoteSessions();
        else Send(new { t = "convs", scope = "remote", failed = true, items = Array.Empty<object>() });
    }

    public void SendModels(ModelCatalog cat) => Send(new
    {
        t = "models",
        ready = cat.Ready,
        current = cat.Current,
        items = cat.Models.Select(m => new
        {
            name = m.Name,
            display = m.DisplayName,
            desc = m.Description,
            available = m.IsAvailable,
        }).ToArray(),
    });

    public void SendError(string text) => Send(new { t = "err", text });

    // ---------- 身份 / 凭据（C# → JS）----------
    //
    // 三条消息，分工不重叠：
    //   idstate       当前身份快照（**不带关联号**）—— 面板任何时候都能显示，
    //                 也是每次改动之后的「刷新一次」
    //   idres         某条命令的结果，带 req 关联号
    //   idNeedsLogin  凭据不可用 → 让前端把面板摆到用户面前
    //
    // 为什么要关联号：面板里可能同时有两件事在跑（保存标识 + 登录轮询），
    // 没有 req 就分不清哪条结果属于哪次操作，界面会把「登录成功」显示在
    // 「保存标识」那一行上。

    /// <summary>身份快照。引擎不可用时 s 为 null、error 带原因 —— 不假装成功。</summary>
    public async Task SendIdentityStateAsync()
    {
        try
        {
            var info = await _host.IdentityStatusAsync();
            Send(new
            {
                t = "idstate",
                s = new
                {
                    hasToken = Bool(info, "has_token") == true,
                    label = Str(info, "account_label"),
                    impersonate = Str(info, "impersonate"),
                    tokenFile = Str(info, "token_file"),
                    // 引擎说「有一个登录会话正在进行中」。前端拿它纠正自己 ——
                    // 面板被关掉又打开过的话，前端并不知道浏览器还开着。
                    loginActive = Bool(info, "login_active") == true,
                },
            });
        }
        catch (Exception ex)
        {
            Send(new { t = "idstate", s = (object?)null, error = ex.Message });
        }
    }

    /// <summary>凭据彻底不可用（见 AgentHost 里 IdentityNeedsLogin 的触发点）。</summary>
    public void SendIdentityNeedsLogin() => Send(new { t = "idNeedsLogin" });

    // ==================== JS → C# ====================

    /// <summary>前端来一条命令。异常必须兜住 —— 它跑在 UI 线程的消息回调上，
    /// 漏出去会直接冒到 DispatcherUnhandledException。</summary>
    /// <param name="json">消息本体（<c>WebMessageAsJson</c>）。</param>
    /// <param name="dropped">
    /// 随消息一起来的**拖拽 / 粘贴文件**的绝对路径（来自
    /// <c>CoreWebView2WebMessageReceivedEventArgs.AdditionalObjects</c>）。
    /// 普通消息传空表即可。详见 <c>MainWindow.OnWebMessage</c>。
    /// </param>
    public async void Handle(string json, IReadOnlyList<string>? dropped = null)
    {
        try
        {
            using var doc = JsonDocument.Parse(json);
            var root = doc.RootElement;
            var t = Str(root, "t");

            switch (t)
            {
                case "hello":
                    // hello = 「界面已就绪，给我初始数据」。
                    //
                    // 对话列表**只有一个来源：账号下的服务端会话**。本地
                    // `conversations/*.json` 不记录归属账号 —— 换号后上一个
                    // 账号的对话会混进来，点进去发消息时引擎会拿新 token 去
                    // 请求一个不属于它的 `chat_session_id`（`deepseek_rpc.py`
                    // 里 `if not session.cid` 才新建，否则直接复用本地 cid），
                    // 服务端必然拒绝。所以本地文件降级成**接管后的缓存**，
                    // 列表只推远端。
                    //
                    // `RefreshConversationsAsync` 仍要调：引擎内部靠它载入当前
                    // 对话（`SendHistory` 的标题/轮次来自它），只是不再推给界面。
                    SendHistory();
                    await _host.RefreshConversationsAsync();
                    // 必须 await：拉完才知道当前对话属不属于这个账号。
                    await RefreshRemoteListAsync();
                    // 本地对话不记录归属账号 —— 换号后引擎仍会恢复上次打开的那条。
                    // 若它不在账号的会话列表里（属于别的账号 / 已被删除），
                    // 在这里就换掉，别等用户发消息拿到 400。
                    await _host.EnsureCurrentSessionBelongsToAccountAsync();
                    _ = RefreshModelsAsync();
                    // 档位清单同理：不阻塞握手。它只影响配置页那个下拉，
                    // 而用户不会在首屏那一瞬间去切档位。
                    _ = PushTransportsAsync();
                    break;

                case "submit":
                    await _host.SubmitAsync(Str(root, "goal"), StrList(root, "files"));
                    break;

                // 选附件。**必须由 C# 弹窗** —— WebView2 里拿不到 File 的本地路径，
                // 详见 `PickFiles` 的注释。选完把绝对路径原样回给前端，前端存着，
                // 发送时再随 `submit` 一起发回来（中间不碰文件内容）。
                case "pickFiles":
                {
                    var picked = PickFiles?.Invoke() ?? Array.Empty<string>();
                    // 用 `{ files: [] }` 而不是只发一个空数组：前端要靠这个键名
                    // 判断「这是选择器的回执」，别和别的事件混了。
                    Send(new { t = "files", files = picked });
                    break;
                }

                // 拖拽 / 粘贴进来的文件。路径**不在 JSON 里** —— 前端用
                // `postMessageWithAdditionalObjects` 把 File 对象从旁路送过来，
                // 由 `MainWindow.OnWebMessage` 从 `AdditionalObjects` 解出路径，
                // 再作为 `dropped` 参数传到这里。详见那边的注释。
                //
                // 回执复用 `pickFiles` 的 `{t:'files', files:[…]}` —— 前端
                // `onPickedFiles()` 对两者一视同仁（去重、过滤空串、渲染药丸）。
                // 一个回执通道比两个好：多一条分支就多一处可能对不齐的地方。
                case "droppedFiles":
                {
                    var paths = dropped ?? Array.Empty<string>();
                    // 一个都没解析出路径，说明拖的是**内存 Blob**（网页图片、
                    // 截图粘贴），不是磁盘文件。这种情况必须**明确告诉用户**，
                    // 不能静默吞掉 —— 拖了没反应最容易被当成程序坏了。
                    // 引擎的上传接口只吃磁盘文件，我们没有能力把内存 Blob 落盘
                    // 再上传（那要前端把内容也送过来，等于把 100MB 上限的文件
                    // 变成 133MB 的 base64 字符串穿桥）。
                    if (paths.Count == 0)
                    {
                        Send(new
                        {
                            t = "err",
                            text = "拖进来的内容不是磁盘文件（网页图片 / 截图属于内存数据），"
                                 + "没法作为附件。请先存成文件再拖，或点「附件」按钮选择。",
                        });
                        break;
                    }
                    Send(new { t = "files", files = paths });
                    break;
                }

                case "cancel":
                    await _host.CancelRunAsync();
                    break;

                case "reset":
                    await _host.ResetBreakerAsync();
                    break;

                case "newConv":
                    // 新建的是**本地空对话**：还没有 `chat_session_id`，
                    // 所以账号下的列表里不会多出一条 —— 发出第一条消息之后
                    // 它才会出现在服务端。这里不需要推列表。
                    await _host.NewConversationAsync();
                    break;

                case "switchConv":
                    await _host.SwitchConversationAsync(Str(root, "id"));
                    _ = RefreshRemoteListAsync();     // active 标记会跟着变
                    break;

                case "renameConv":
                    await _host.RenameConversationAsync(Str(root, "id"), Str(root, "title"));
                    break;

                case "delConv":
                    // **删的是服务端那条**，不是本地文件。
                    //
                    // 列表里全是服务端会话，id 也是 `remote_id`。早先这里调
                    // `DeleteConversationAsync`（按本地 id 删 store），拿 remote_id
                    // 去找本地对话必然找不到 —— `store.delete` 返回 false，
                    // 而宿主在 `deleted=false` 时不加任何提示，界面就变成
                    // 「点了删除，什么也没发生」。
                    //
                    // `DeleteRemoteSessionAsync` 才是对的动作：删服务端 + 把本地
                    // 绑定解掉（否则本地 cid 指向死会话，下一句发出去撞
                    // `invalid chat session id`）。不可逆，前端必须先二次确认。
                    await _host.DeleteRemoteSessionAsync(Str(root, "id"));
                    _ = RefreshRemoteListAsync();
                    break;

                case "syncConv":
                    // 按本地 id 工作：从服务端重拉历史，补齐在网页版里聊过的内容。
                    // 未接管的会话没有本地 id，前端会禁用这一项。
                    await _host.SyncConversationAsync(Str(root, "id"));
                    break;

                case "handoff":
                    // 接力出来的也是本地新对话（线上要等第一条消息）。
                    await _host.HandoffConversationAsync();
                    break;

                case "refreshConvs":
                    // 旧协议名，前端已改发 `listRemote` —— 保留兼容。
                    await RefreshRemoteListAsync();
                    break;

                case "listRemote":
                    // 读账号下的会话列表要联网，可能慢 —— 前端先显示
                    // 「正在读取账号下的会话…」，这里回来再推一批。
                    await RefreshRemoteListAsync();
                    break;

                case "attachSession":
                    // 接管一条服务端会话：引擎拉全量历史 + 校正续接锚点并切过去。
                    // `AttachSessionAsync` 内部已经重拉过远端列表，这里只需重推；
                    // 回一条 attached 让前端知道成败。
                    {
                        var ok = await _host.AttachSessionAsync(Str(root, "id"));
                        Send(new { t = "attached", ok });
                        if (ok)
                        {
                            SendRemoteSessions();
                            SendStatus();
                        }
                    }
                    break;

                case "setChat":
                    await _host.SetChatAsync(
                        temporary: Bool(root, "temporary"),
                        extendedThinking: Bool(root, "thinking"),
                        searchEnabled: Bool(root, "search"),
                        showThinking: Bool(root, "showThinking"));
                    break;

                case "transports":
                    await PushTransportsAsync();
                    break;

                case "setTransport":
                {
                    var name = Str(root, "name").Trim();
                    var ok = await _host.SetTransportAsync(name);
                    Send(new { t = "transportSet", name, ok });
                    // 无论成败都重推一次清单：失败时用户可能刚补了 cookie，
                    // 成功时 `ready` / `guest` 都可能变了。
                    await PushTransportsAsync();
                    SendStatus();
                    break;
                }

                case "models":
                    await RefreshModelsAsync();
                    break;

                case "setModel":
                    await _host.SetModelAsync(Str(root, "name"));
                    await RefreshModelsAsync();
                    break;

                case "identity":
                    // 页脚那枚小刷新：只重新验证一次，**不弹面板**。
                    // 与 WPF 壳的 BtnRefreshIdentity 同一语义 —— 健康胶囊显示
                    // 「未登录」时用户的下一个动作一定是「怎么修」，把它藏进
                    // 二级面板就变成两跳。
                    await _host.RefreshIdentityAsync();
                    SendStatus();
                    break;

                // ---------- 身份 / 凭据 ----------
                //
                // 与 WPF 壳的 IdentityDialog 一一对应（调的是同一批 AgentHost 方法），
                // 差别只在呈现：那边是模态 Window，这边是 WebView 里的遮罩面板。
                // 所以这里**不做任何业务判断** —— 校验、落盘、重建运行时全在
                // AgentHost / 引擎里，两边共用同一份。

                case "idStatus":
                    await SendIdentityStateAsync();
                    break;

                case "idLabel":
                {
                    var req = Int(root, "req");
                    var label = Str(root, "label").Trim();
                    // 空标识会被引擎当成「没设标识」，而 L1 身份闸门在标识为空时
                    // **拦住一切提交** —— 存一个空串等于把应用推进「能登录但发不出字」
                    // 的状态，所以在这里就挡住。
                    if (label.Length == 0)
                    {
                        Send(new { t = "idres", req, kind = "label", ok = false, text = "标识不能为空。" });
                        break;
                    }
                    try
                    {
                        await _host.SetIdentityLabelAsync(label);
                        Send(new { t = "idres", req, kind = "label", ok = true, label });
                        await SendIdentityStateAsync();
                    }
                    catch (Exception ex)
                    {
                        Send(new { t = "idres", req, kind = "label", ok = false, text = ex.Message });
                    }
                    break;
                }

                case "idSet":
                {
                    var req = Int(root, "req");
                    var token = Str(root, "token").Trim();
                    if (token.Length == 0)
                    {
                        Send(new { t = "idres", req, kind = "set", ok = false, text = "请先粘贴 token。" });
                        break;
                    }
                    try
                    {
                        var r = await _host.SetIdentityAsync(token);
                        Send(new
                        {
                            t = "idres",
                            req,
                            kind = "set",
                            ok = true,
                            // valid=false 时**不算 ok**：存一个用不了的 token 比不存更糟，
                            // 用户会以为登录好了，直到第一次对话才炸，而且报错指向别处。
                            valid = Bool(r, "valid") == true,
                            detail = Str(r, "detail"),
                            suspended = Bool(r, "suspended") == true,
                        });
                        await SendIdentityStateAsync();
                    }
                    catch (Exception ex)
                    {
                        Send(new { t = "idres", req, kind = "set", ok = false, text = ex.Message });
                    }
                    break;
                }

                case "idRefresh":
                {
                    var req = Int(root, "req");
                    var mark = LastTranscriptSeq();
                    try
                    {
                        await _host.RefreshIdentityAsync();
                        // RefreshIdentityAsync 返回 void —— 它的结论（「凭据已刷新
                        // （来源 X）· detail」/「凭据不可用：detail」）只写进对话流。
                        // WPF 壳里对话流就在对话框背后，用户能瞥见；Web 壳的面板是
                        // 遮罩，背后的对话流看不见，所以把新加的那条原样取出来显示在面板里。
                        var (ok, text) = NewTranscriptOutcome(mark);
                        Send(new { t = "idres", req, kind = "refresh", ok, text });
                        await SendIdentityStateAsync();
                    }
                    catch (Exception ex)
                    {
                        Send(new { t = "idres", req, kind = "refresh", ok = false, text = ex.Message });
                    }
                    break;
                }

                case "idClear":
                {
                    var req = Int(root, "req");
                    try
                    {
                        await _host.ClearIdentityAsync();
                        Send(new { t = "idres", req, kind = "clear", ok = true });
                        await SendIdentityStateAsync();
                    }
                    catch (Exception ex)
                    {
                        Send(new { t = "idres", req, kind = "clear", ok = false, text = ex.Message });
                    }
                    break;
                }

                // 换账号：清 token + 清专用浏览器 profile。
                // 与 idClear 分开是因为语义不同 —— 后者只清本地凭据，
                // 重新登录仍会回到同一个账号（profile 里的 cookie 还在）。
                case "idLogout":
                {
                    var req = Int(root, "req");
                    try
                    {
                        var result = await _host.LogoutIdentityAsync();
                        var removedProfile = result.TryGetProperty("removed_profile", out var rp) &&
                                             rp.ValueKind == JsonValueKind.True;
                        var clearedLoginState = result.TryGetProperty("cleared_login_state", out var cl) &&
                                                cl.ValueKind == JsonValueKind.True;
                        Send(new { t = "idres", req, kind = "logout", ok = true,
                                   removedProfile, clearedLoginState });
                        await SendIdentityStateAsync();
                    }
                    catch (Exception ex)
                    {
                        Send(new { t = "idres", req, kind = "logout", ok = false, text = ex.Message });
                    }
                    break;
                }

                case "idLoginStart":
                {
                    var req = Int(root, "req");
                    try
                    {
                        var r = await _host.StartBrowserLoginAsync();
                        Send(new
                        {
                            t = "idres",
                            req,
                            kind = "loginStart",
                            ok = true,
                            browser = Str(r, "browser"),
                            port = Str(r, "port"),
                            // 引擎已经有一个会话在跑时，回的是
                            // {already_running:true, port:N} —— **没有 browser 字段**
                            // （见 gad_engine/__main__.py 的 identity.login.start）。
                            // 前端据此换一句文案，免得显示成「已在 （调试端口 9333）」。
                            reused = Bool(r, "reused") == true || Bool(r, "already_running") == true,
                        });
                        await SendIdentityStateAsync();
                    }
                    catch (Exception ex)
                    {
                        Send(new { t = "idres", req, kind = "loginStart", ok = false, text = ex.Message });
                    }
                    break;
                }

                case "idLoginPoll":
                {
                    var req = Int(root, "req");
                    try
                    {
                        var r = await _host.PollBrowserLoginAsync();
                        Send(new
                        {
                            t = "idres",
                            req,
                            kind = "loginPoll",
                            ok = true,
                            state = Str(r, "state"),
                            error = Str(r, "error"),
                        });
                    }
                    catch (Exception ex)
                    {
                        // 轮询失败**不能静默**：前端拿不到结果就会一直等下去，
                        // 而用户看到的是「等待登录…」永远不动。回一个 error 态让它收口。
                        Send(new { t = "idres", req, kind = "loginPoll", ok = false, state = "error", error = ex.Message });
                    }
                    break;
                }

                case "idLoginFinish":
                {
                    var req = Int(root, "req");
                    try
                    {
                        var r = await _host.FinishBrowserLoginAsync();
                        Send(new
                        {
                            t = "idres",
                            req,
                            kind = "loginFinish",
                            ok = Bool(r, "ok") == true,
                            detail = Str(r, "detail"),
                            label = Str(r, "account_label"),
                        });
                        await SendIdentityStateAsync();
                    }
                    catch (Exception ex)
                    {
                        Send(new { t = "idres", req, kind = "loginFinish", ok = false, detail = ex.Message, label = "" });
                    }
                    break;
                }

                case "idLoginCancel":
                {
                    var req = Int(root, "req");
                    // 取消路径不抛 —— 它多半是用户关面板/点取消时触发的，
                    // 为一次取消失败弹一条错误反而更烦人。
                    try { await _host.CancelBrowserLoginAsync(); } catch { /* 忽略 */ }
                    Send(new { t = "idres", req, kind = "loginCancel", ok = true });
                    break;
                }

                case "replay":
                    await _host.ReplayCurrentAsync();
                    break;

                // ---------- 朗读（TTS）----------
                //
                // 三个命令都**只做控制面**：签票 / 列音色 / 切音色。
                // 音频流不在宿主侧 —— 前端拿到 `ticket` 后直连上游 WS，
                // 用 WebView2 自带的 WebCodecs 解 Opus 并播放。
                //
                // 每条命令都带 `req` 关联号：朗读按钮可以在多个气泡上同时被点，
                // 没有关联号就分不清回来的票据属于哪一次请求，前端会把 A 气泡的
                // ticket 挂到 B 气泡上 —— 听起来像「念错了内容」，实际是串了线。
                // 与身份面板那批（idstate / idres）用的是同一个套路。

                case "tts":
                {
                    var req = Str(root, "req");
                    var mid = Str(root, "messageId");
                    var res = await _host.PrepareTtsAsync(mid);
                    Send(new
                    {
                        t = "tts",
                        req,
                        messageId = Str(res, "message_id"),
                        sid = Str(res, "sid"),
                        // 票据是一次性的短时效凭证，**不是账号 token** ——
                        // 它只能读这一条消息，泄露也换不出账号控制权。
                        // 但它仍然是凭证：不写进日志、不进历史。
                        ticket = Str(res, "ticket"),
                        error = Str(res, "error"),
                    });
                    break;
                }

                case "ttsVoices":
                {
                    var req = Str(root, "req");
                    var res = await _host.ListTtsVoicesAsync();
                    var items = new List<object>();
                    if (res.TryGetProperty("items", out var arr) &&
                        arr.ValueKind == JsonValueKind.Array)
                    {
                        foreach (var v in arr.EnumerateArray())
                        {
                            items.Add(new
                            {
                                id = Str(v, "voice_id"),
                                name = Str(v, "name"),
                                desc = Str(v, "desc"),
                                gender = Str(v, "gender"),
                                languages = v.TryGetProperty("languages", out var lg) &&
                                            lg.ValueKind == JsonValueKind.Array
                                    ? lg.EnumerateArray().Select(x => x.ToString()).ToArray()
                                    : Array.Empty<string>(),
                                // `is_default` = 出厂默认（永远 mira），只能用来
                                // 打「默认」角标，**不能当选中态**。
                                // 选中态看 `is_current`（对应服务端的 current_voice_id）。
                                isDefault = Bool(v, "is_default") == true,
                                isCurrent = Bool(v, "is_current") == true,
                                demoUrl = v.TryGetProperty("demo_urls", out var du) &&
                                          du.ValueKind == JsonValueKind.Array &&
                                          du.GetArrayLength() > 0
                                    ? du[0].ToString()
                                    : "",
                            });
                        }
                    }
                    Send(new
                    {
                        t = "ttsVoices",
                        req,
                        items = items.ToArray(),
                        error = Str(res, "error"),
                    });
                    break;
                }

                case "ttsVoice":
                {
                    var req = Str(root, "req");
                    var res = await _host.SetTtsVoiceAsync(Str(root, "id"));
                    Send(new
                    {
                        t = "ttsVoice",
                        req,
                        ok = Bool(res, "ok") == true,
                        id = Str(res, "voice_id"),
                        error = Str(res, "error"),
                    });
                    break;
                }

                case "win":
                    // 同步执行，且不 await —— DragMove() 必须在鼠标按下的
                    // 同一条消息里进入模态循环，晚一拍就会抛「无法在非按下状态拖拽」。
                    WindowOp?.Invoke(Str(root, "op"));
                    break;

                // ---------- 朗读：Fish Audio 直连 ----------
                //
                // 与上面 `tts` / `ttsVoices` / `ttsVoice` 三条并存而不是替换：
                // 那三条走 DeepSeek 的服务端流式朗读（签票 + 前端直连 WS），
                // 这三条走 Fish 的一次性 HTTP。两条管线完全不同，靠
                // `ttsEngine` 命令让前端决定点朗读按钮时走哪条。
                //
                // **音频以 base64 回传**，原因见 AgentHost.SpeakFishAsync 的注释。
                case "ttsEngine":
                    Send(new { t = "ttsEngine", req = Str(root, "req"), engine = _host.TtsEngine });
                    break;

                case "ttsFish":
                {
                    var req = Str(root, "req");
                    var res = await _host.SpeakFishAsync(Str(root, "text"), Str(root, "voice"));
                    Send(new
                    {
                        t = "ttsFish",
                        req,
                        audio = Str(res, "audio"),
                        mime = Str(res, "mime"),
                        error = Str(res, "error"),
                    });
                    break;
                }

                case "ttsFishVoices":
                {
                    var req = Str(root, "req");
                    var res = await _host.ListFishVoicesAsync();
                    var items = new List<object>();
                    if (res.TryGetProperty("items", out var arr) &&
                        arr.ValueKind == JsonValueKind.Array)
                    {
                        foreach (var v in arr.EnumerateArray())
                        {
                            items.Add(new
                            {
                                id = Str(v, "id"),
                                name = Str(v, "name"),
                                desc = Str(v, "desc"),
                                tags = v.TryGetProperty("tags", out var tg) &&
                                       tg.ValueKind == JsonValueKind.Array
                                    ? tg.EnumerateArray().Select(x => x.ToString()).ToArray()
                                    : Array.Empty<string>(),
                                isCurrent = Bool(v, "isCurrent") == true,
                            });
                        }
                    }
                    Send(new
                    {
                        t = "ttsFishVoices",
                        req,
                        items = items.ToArray(),
                        current = Str(res, "current"),
                        error = Str(res, "error"),
                    });
                    break;
                }

                case "ttsFishVoice":
                {
                    var req = Str(root, "req");
                    var res = _host.SetFishVoice(Str(root, "id"));
                    Send(new
                    {
                        t = "ttsFishVoice",
                        req,
                        ok = Bool(res, "ok") == true,
                        id = Str(res, "id"),
                        error = Str(res, "error"),
                    });
                    break;
                }

                case "ttsSettings":
                {
                    var req = Str(root, "req");
                    var res = _host.GetTtsSettings();
                    Send(new
                    {
                        t = "ttsSettings",
                        req,
                        engine = Str(res, "engine"),
                        @base = Str(res, "base"),
                        model = Str(res, "model"),
                        voice = Str(res, "voice"),
                        format = Str(res, "format"),
                        hasKey = Bool(res, "hasKey") == true,
                        keyLength = Str(res, "keyLength"),
                        path = Str(res, "path"),
                    });
                    break;
                }

                case "ttsSettingsSet":
                {
                    var req = Str(root, "req");
                    // 空 key 传下去表示「保持原值」—— 面板不回传 key，
                    // 用户只是改模型时不该把已配的 key 清掉。
                    var res = _host.SetTtsSettings(
                        Str(root, "engine"), Str(root, "base"), Str(root, "model"),
                        Str(root, "format"), Str(root, "apiKey"));
                    Send(new
                    {
                        t = "ttsSettingsSet",
                        req,
                        engine = Str(res, "engine"),
                        @base = Str(res, "base"),
                        model = Str(res, "model"),
                        hasKey = Bool(res, "hasKey") == true,
                        error = "",
                    });
                    break;
                }

                default:
                    SendError($"未知命令：{t}");
                    break;
            }
        }
        catch (Exception ex)
        {
            SendError(ex.Message);
        }
    }

    private async Task RefreshModelsAsync()
    {
        try { SendModels(await _host.ListModelsAsync()); }
        catch (Exception ex) { SendError(ex.Message); }
    }

    /// <summary>
    /// 推档位清单。`current` 是**引擎报的裸名**（`deepseek_rpc` / `gemini_rpc`），
    /// 前端用它跟 `items[].name` 比对来选中当前项 —— 不要拿 `Status.Transport`
    /// 去比，那个值在链路不可用时会被写成中文「不可用」（见 `ApplyStatus`）。
    /// </summary>
    private async Task PushTransportsAsync()
    {
        try
        {
            var cat = await _host.ListTransportsAsync();
            Send(new
            {
                t = "transports",
                items = cat.Items.Select(x => new
                {
                    name = x.Name,
                    label = x.Label,
                    available = x.Available,
                    ready = x.Ready,
                    guest = x.Guest,
                    hasCookie = x.HasCookie,
                    note = x.Note,
                }).ToArray(),
                current = _host.Status.Transport,
                ready = cat.Ready,
            });
        }
        catch (Exception ex) { SendError(ex.Message); }
    }

    // ==================== 序列化 ====================

    private static object Item(TranscriptItem it) => new
    {
        seq = it.Seq,
        kind = it.Kind.ToString(),
        text = it.Text,
        detail = it.Detail,
        // 服务端助手消息 id —— 只有「本轮定稿的那块气泡」才有值。
        // 前端靠它决定「这条气泡上要不要画朗读按钮」以及「点了要读哪条」。
        // 空串时前端一律不画按钮，比画一个点了会报错的按钮好。
        messageId = it.MessageId,
        tool = it.Tool is { } t
            ? new
            {
                kind = t.Kind,
                label = t.Label,
                subject = t.Subject,
                note = t.Note,
                running = t.Running,
                elapsedMs = t.ElapsedMs,
                exitCode = t.ExitCode,
                output = t.Output,
                failed = t.Failed,
            }
            : null,
        thought = it.Thought is { } th
            ? new
            {
                streaming = th.Streaming,
                chars = th.Chars,
                waited = th.Waited,
                header = ThoughtHeader(it, th),
            }
            : null,
    };

    private static object Log(LogItem log) => new
    {
        seq = log.Seq,
        text = log.Text,
        level = log.Level.ToString(),
        at = log.At.ToString("HH:mm:ss"),
    };

    private static object Status(HostStatus s) => new
    {
        started = s.Started,
        mode = s.Mode,
        transport = s.Transport,
        transportReady = s.TransportReady,
        circuitLevel = s.CircuitLevel,
        circuitReason = s.CircuitReason,
        lastSignal = s.LastSignal,
        degrade = s.Degrade,
        hasToken = s.HasToken,
        identityLabel = s.IdentityLabel,
        identityRefreshing = s.IdentityRefreshing,
        accountSuspended = s.AccountSuspended,
        suspendedDetail = s.SuspendedDetail,
        model = s.Model,
        gateAllowed = s.GateAllowed,
        gateReason = s.GateReason,
        gateWaitMs = s.GateWaitMs,
        busyStreak = s.BusyStreak,
        sessionCid = s.SessionCid,
        sessionTurns = s.SessionTurns,
        sessionMaxTurns = s.SessionMaxTurns,
        sessionTemporary = s.SessionTemporary,
        convTitle = s.ConversationTitle,
        convTurns = s.ConversationTurns,
        memoryChars = s.MemoryChars,
        memoryEnabled = s.MemoryEnabled,
        thinking = s.ExtendedThinking,
        search = s.SearchEnabled,
        showThinking = s.ShowThinking,
        thinkingMode = s.ThinkingMode,
    };

    /// <summary>
    /// 思考块头部那行字。四态，顺序不能换 —— 与 WPF 壳的
    /// <c>MainWindow.ThoughtHeader</c> 是同一套规则，改一处要同步另一处。
    ///
    /// 「等太久」那一态比「思考中」更值钱：实测成功的回答只要 9.9–50.7 秒，
    /// 过了 60 秒基本不是「慢」而是上游把这轮丢了。
    /// </summary>
    private static string ThoughtHeader(TranscriptItem item, ThoughtState state)
    {
        if (!state.Streaming)
            return $"思考过程（{state.Chars} 字）· 点这里展开";

        if (item.Text.Length > 0)
            return $"思考中…（已 {state.Chars} 字）";

        if (state.Waited >= 60)
            return $"上游 {state.Waited} 秒没回一个字 —— 可能这轮被丢弃了。" +
                   "建议把模型换成 4 Argon（或把「思考程度」调回标准模式）再发一次。";

        return $"已发出，等上游开始输出…（{state.Waited} 秒）";
    }

    // ==================== 取值助手 ====================

    /// <summary>
    /// 取字符串字段。数字也认 —— 这一条是踩出来的：
    /// 引擎的 <c>identity.login.start</c> 回的 <c>port</c> 是**数字**（9333），
    /// 只认 String 的话它会被静默读成空串，界面上就显示「调试端口 —」，
    /// 而且不报任何错。WPF 壳那边的同名助手本来就认 Number，这里补齐。
    /// </summary>
    private static string Str(JsonElement root, string key) =>
        root.TryGetProperty(key, out var v)
            ? v.ValueKind switch
            {
                JsonValueKind.String => v.GetString() ?? "",
                JsonValueKind.Number => v.ToString(),
                _ => "",
            }
            : "";

    private static bool? Bool(JsonElement root, string key) =>
        root.TryGetProperty(key, out var v) && v.ValueKind is JsonValueKind.True or JsonValueKind.False
            ? v.GetBoolean()
            : null;

    /// <summary>命令关联号。取不到就用 -1 —— 前端据此能分辨「这条结果没有归属」。</summary>
    /// <summary>
    /// 取字符串数组。缺键 / 类型不对 / 空数组都返回 <c>null</c> —— 调用方
    /// （<c>SubmitAsync</c>）对 null 与空表的处理完全一致（都当没有附件），
    /// 所以不必区分。元素里的空串会被滤掉，免得把 <c>""</c> 当路径传给引擎。
    /// </summary>
    private static IReadOnlyList<string>? StrList(JsonElement root, string key)
    {
        if (!root.TryGetProperty(key, out var v) || v.ValueKind != JsonValueKind.Array)
            return null;
        var list = v.EnumerateArray()
            .Where(e => e.ValueKind == JsonValueKind.String)
            .Select(e => (e.GetString() ?? "").Trim())
            .Where(s => s.Length > 0)
            .ToList();
        return list.Count == 0 ? null : list;
    }

    private static int Int(JsonElement root, string key) =>
        root.TryGetProperty(key, out var v) && v.ValueKind == JsonValueKind.Number
            ? v.GetInt32()
            : -1;

    // ---------- 对话流水位（给「返回 void 的宿主方法」捞结论用）----------

    /// <summary>
    /// 当前对话流最后一条的序号。配合 <see cref="NewTranscriptOutcome"/> 用。
    ///
    /// **为什么用 Seq 而不是 Count**：`_transcript` 有 600 条上限，超了会从头部裁掉
    /// （见 AgentHost.TrimTranscript），用索引做水位会在长会话里错位；Seq 是单调递增的。
    /// </summary>
    private int LastTranscriptSeq()
    {
        var t = _host.Transcript;
        return t.Count > 0 ? t[^1].Seq : 0;
    }

    /// <summary>
    /// 取水位之后新加的 Notice / Error，作为这次操作的结论。
    ///
    /// 只服务一件事：宿主里有些方法（目前是 <c>RefreshIdentityAsync</c>）返回 void，
    /// 结论只进对话流。身份面板是遮罩，看不见背后的对话流，所以要把它捞出来。
    /// 取**最后一条**而不是第一条 —— 一次调用可能同时落 Notice 和 Error。
    /// </summary>
    private (bool ok, string text) NewTranscriptOutcome(int afterSeq)
    {
        var ok = true;
        var text = "";
        foreach (var it in _host.Transcript)
        {
            if (it.Seq <= afterSeq) continue;
            if (it.Kind == TranscriptKind.Notice) { ok = true; text = it.Text; }
            else if (it.Kind == TranscriptKind.Error) { ok = false; text = it.Text; }
        }
        return (ok, text);
    }
}
