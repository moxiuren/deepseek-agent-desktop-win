using System.IO;
using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Interop;
using System.Windows.Media;
using Microsoft.Web.WebView2.Core;

namespace DeepSeekAgent.Desktop;

/// <summary>
/// WebView2 外壳主窗口。整个客户区是一个 WebView2，界面全在 Assets/web 里。
///
/// 这个类只做接线，不含任何渲染逻辑：
///   1. 初始化 WebView2 环境与虚拟主机映射
///   2. 把 <see cref="AgentHost"/> 的事件转发给 <see cref="WebBridge"/>
///   3. 把前端来的消息转给 <see cref="WebBridge.Handle"/>
/// </summary>
public partial class MainWindow : Window
{
    private WebBridge? _bridge;

    public MainWindow()
    {
        InitializeComponent();

        // 屏幕比窗口小的时候（实测这台机器 1920x1200 @150%，工作区只有
        // 1280x760 DIP，比默认的 1360x860 小），CenterScreen 会把窗口顶到
        // 屏幕外 —— 抓到的窗口矩形 Top=-20 DIP，自绘标题栏被切掉。
        // 所以自己算：先按工作区的 94% 压尺寸（留出安全边），再手动居中。
        var wa = SystemParameters.WorkArea;
        Width = Math.Min(Width, wa.Width * 0.94);
        Height = Math.Min(Height, wa.Height * 0.94);
        WindowStartupLocation = WindowStartupLocation.Manual;
        Left = wa.Left + (wa.Width - Width) / 2;
        Top = wa.Top + (wa.Height - Height) / 2;

        Loaded += OnLoaded;
    }

    private async void OnLoaded(object sender, RoutedEventArgs e)
    {
        try
        {
            // user data 目录必须可写；放 LocalAppData 而不是程序目录
            // （Program Files 下没写权限，装到那儿会起不来）。
            var userData = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "DeepSeek-Agent", "webview");
            var env = await CoreWebView2Environment.CreateAsync(userDataFolder: userData);
            await Web.EnsureCoreWebView2Async(env);

            var core = Web.CoreWebView2;

            // 前端资源走虚拟主机而不是 file://：
            // file:// 下没有 origin，localStorage / fetch / CSP 全是残缺的；
            // 映射成 https://app.local 之后就是一个正常的同源站点
            // （DSH 桌面版用自注册的 dsh-app: 协议达到同一目的：给它一个真正的 origin，
            //   然后用这个 origin 做 IPC 准入）。
            var webRoot = Path.Combine(AppContext.BaseDirectory, "Assets", "web");
            if (!Directory.Exists(webRoot))
                throw new DirectoryNotFoundException($"前端资源目录不存在：{webRoot}");
            core.SetVirtualHostNameToFolderMapping(
                AppHost, webRoot, CoreWebView2HostResourceAccessKind.Allow);

            core.Settings.IsStatusBarEnabled = false;
            // 关掉浏览器味儿的默认右键菜单（用户要复制文字直接 Ctrl+C 就行）；
            // DevTools 留着，F12 可查 —— 这个项目还在演进期。
            core.Settings.AreDefaultContextMenusEnabled = false;

            _bridge = new WebBridge(App.Host, Dispatcher, PostJson);
            _bridge.WindowOp = DoWindowOp;
            // 附件选择器。**必须在这里弹窗，不能在 WebBridge 里** ——
            // 对话框要挂在窗口上（Owner），而 WebBridge 拿不到窗口实例；
            // 它也刻意不引 Microsoft.Win32（桥只该管协议转发）。
            _bridge.PickFiles = PickFilesForUpload;
            core.WebMessageReceived += OnWebMessage;

            // 最大化时把 WebView2 的那圈缩放边距收掉 —— 最大化的窗口本来就不缩放，
            // 留着会在四周露出一条底色边。
            StateChanged += (_, _) =>
                Web.Margin = WindowState == WindowState.Maximized
                    ? new Thickness(0)
                    : new Thickness(6);

            // 握手有两条路，谁先到都行：
            //   ① 页面脚本自己发 {t:"hello"}（前端在启动时就发，见 app.js）
            //   ② 这里在页面加载完成后补发一条 {t:"ready"}，前端收到也回 hello
            // 前端对 hello 做了「只发一次」的保护，所以两条路都走也不会重复灌历史。
            //
            // 为什么保留 ②：① 依赖「页面脚本一定会执行」这个假设。真出问题时
            // （脚本被拦、资源 404、CSP 挡了），① 不会发生，而 ② 至少能给出
            // 一个明确的「页面加载失败」提示，而不是永远停在「启动中…」。
            core.NavigationCompleted += (_, args) =>
            {
                if (args.IsSuccess) _bridge?.SendHello();
                else _bridge?.SendError($"页面加载失败：{args.WebErrorStatus}");
            };

            var host = App.Host;
            host.ItemAdded += OnItemAdded;
            host.ItemChanged += OnItemChanged;
            host.ItemRemoved += OnItemRemoved;
            host.TranscriptCleared += OnTranscriptCleared;
            host.Logged += OnLogged;
            host.StatusChanged += OnStatusChanged;
            host.RunningChanged += OnRunningChanged;
            // 列表只显示**账号下的服务端会话**，本地 `Conversations` 不再推给
            // 界面（理由见 WebBridge 的 `hello` 与 app.js「会话列表」注释）。
            // 所以只订阅远端列表这一条。
            host.RemoteSessionsChanged += OnRemoteSessionsChanged;
            // 凭据彻底不可用 → 把身份面板摆到用户面前。
            // WPF 壳在 MainWindow 里订阅同一个事件弹 IdentityDialog；这里换成
            // 推一条消息让前端开面板 —— 理由一样：用户刚看到「未登录」，
            // 第一件事就是找「怎么修」，不该让他再去二级菜单里找第二次。
            host.IdentityNeedsLogin += OnIdentityNeedsLogin;

            core.Navigate($"https://{AppHost}/index.html");
        }
        catch (Exception ex)
        {
            MessageBox.Show(
                $"WebView2 初始化失败：\n\n{ex.Message}\n\n" +
                "请确认已安装 WebView2 Runtime（Win11 通常自带）。",
                "DeepSeek Agent Desktop", MessageBoxButton.OK, MessageBoxImage.Error);
            Application.Current.Shutdown(1);
        }
    }

    // ---------- 宿主 → 前端 ----------

    private void OnItemAdded(TranscriptItem item) => _bridge?.SendItem(item);

    private void OnItemChanged(TranscriptItem item) => _bridge?.SendItemChanged(item);

    private void OnItemRemoved(TranscriptItem item) => _bridge?.SendItemRemoved(item);

    private void OnTranscriptCleared() => _bridge?.SendCleared();

    private void OnLogged(LogItem log) => _bridge?.SendLog(log);

    private void OnStatusChanged() => _bridge?.SendStatus();

    private void OnRunningChanged(bool running)
    {
        _bridge?.SendStatus();
        // 一轮跑完 → 账号下的会话列表可能变了：
        //   · 新对话这时才拿到 `chat_session_id`（第一条消息发出去才有）
        //   · 服务端生成/更新了标题
        // 本地列表变化不再驱动界面，所以这一步必须在这里补，否则新对话
        // 要等到下次点齿轮才出现在左栏。
        if (!running) _bridge?.RefreshSessions();
    }

    private void OnRemoteSessionsChanged() => _bridge?.SendRemoteSessions();

    private void OnIdentityNeedsLogin() => _bridge?.SendIdentityNeedsLogin();

    // ---------- 前端 → 宿主 ----------

    /// <summary>
    /// 只有映射出来的那个源站才准驱动宿主。
    ///
    /// 做法参考 DeepSeek Harness 桌面版：它注册 <c>dsh-app:</c> 协议、把前端挂在
    /// <c>dsh-app://app/</c> 上，然后在**每一个** IPC 入口校验
    /// <c>event.senderFrame.url.startsWith('dsh-app://app/')</c>
    /// （见其 lib/main.js 的 assertDesktopSender），而不只在加载页面时查一次。
    ///
    /// 我们这边今天只有一个文档、一个入口，看起来是多余的。但少了这道闸，
    /// 「谁能 postMessage」就等于「谁能 submit 任务 / 改配置 / 关窗口」——
    /// 将来任何一次往窗口里加 iframe、加载外部页面、或开第二个文档，
    /// 都会静默地把这些命令暴露出去，而且不会有任何报错提示。
    /// </summary>
    private void OnWebMessage(object? sender, CoreWebView2WebMessageReceivedEventArgs e)
    {
        if (!IsAppSource(e.Source))
        {
            System.Diagnostics.Debug.WriteLine($"[bridge] 丢弃非 app.local 来源的消息：{e.Source}");
            return;
        }

        // 拖拽 / 粘贴进来的文件随消息一起到，**不在 JSON 里**。
        //
        // 前端用 `chrome.webview.postMessageWithAdditionalObjects(msg, [file])`
        // 发消息时，File 对象走的是 WebView2 的旁路通道，宿主这边从
        // `e.AdditionalObjects` 里取，每一项是 `CoreWebView2File`，其 `Path`
        // 就是磁盘绝对路径（官方文档：*"You can use this object to obtain the
        // path of a File dropped on WebView2."*）。
        //
        // 这条旁路正是「浏览器拿不到 File 路径、WebView2 拿得到」的分界：
        // 纯浏览器里 File 只有内容，而 WebView2 嵌在宿主进程里，有权把路径
        // 透出来。所以拖拽 / 粘贴**不需要**读字节、不需要 base64 穿桥。
        //
        // 注意 `Path` 可能为空：从浏览器拖来的图片、截图粘贴这类是内存 Blob，
        // 压根没有本地文件。这种要如实丢弃（而不是塞一个空串进去，让引擎
        // 在几秒后抛一个语焉不详的错误）。
        var dropped = new List<string>();
        try
        {
            foreach (var obj in e.AdditionalObjects)
            {
                if (obj is CoreWebView2File f && !string.IsNullOrWhiteSpace(f.Path))
                    dropped.Add(f.Path);
            }
        }
        catch (Exception ex)
        {
            // AdditionalObjects 在个别运行时版本上可能抛（例如消息本身不带
            // 附加对象时的老实现）。拿不到就当没有 —— 不能因为附件机制
            // 出问题，把普通消息也一起吞掉。
            System.Diagnostics.Debug.WriteLine($"[bridge] 读取 AdditionalObjects 失败：{ex.Message}");
        }

        // WebMessageAsJson 在前端 postMessage(对象) 时是对象 JSON；
        // 传字符串时是带引号的 JSON 字符串（Handle 里会解析失败并回一条错误）。
        _bridge?.Handle(e.WebMessageAsJson, dropped);
    }

    /// <summary>虚拟主机名。映射与校验都用它，改一处即可。</summary>
    private const string AppHost = "app.local";

    private static bool IsAppSource(string? source) =>
        !string.IsNullOrEmpty(source)
        && Uri.TryCreate(source, UriKind.Absolute, out var u)
        && string.Equals(u.Host, AppHost, StringComparison.OrdinalIgnoreCase);

    private void PostJson(string json)
    {
        try
        {
            Web.CoreWebView2?.PostWebMessageAsJson(json);
        }
        catch (ObjectDisposedException) { /* 窗口正在关，丢弃即可 */ }
        catch (InvalidOperationException) { /* 同上 */ }
    }

    // ---------- 自绘标题栏：窗口操作 ----------

    /// <summary>
    /// 前端那排窗口按钮 / 标题栏拖拽落到这里。全部在 UI 线程上（Handle 跑在
    /// WebMessageReceived 回调里，本来就是 UI 线程）。
    /// </summary>
    private void DoWindowOp(string op)
    {
        switch (op)
        {
            case "min":
                WindowState = WindowState.Minimized;
                break;

            case "max":
                ToggleMaximize();
                break;

            case "close":
                Close();
                break;

            case "drag":
                BeginDrag();
                break;
        }
    }

    // ---------- 附件：把磁盘上的真实文件交给引擎上传 ----------
    //
    // 这里只把**绝对路径**交给前端，发送时路径随 `submit` 回到引擎，
    // 由引擎走 `/api/v0/file/upload_file` 上传、把 file id 挂进
    // `ref_file_ids`。模型读到的是**服务端解析后的文件**（PDF/图片/表格都能解）。
    //
    // 2026-10-18 起 WPF 壳也改走同一条路（它原来是把内容展开成 24KB 文字
    // 拼进 goal，服务端不知道有文件）。两个壳现在能力对等。
    //
    // 为什么路径要绕一圈回前端再回来，而不是 C# 自己攒着：
    // 附件条要能显示、要能删、要能随输入框一起被「发出去才消费」——
    // 这些都是前端的状态。C# 只负责「弹窗 + 给出路径」，不持有列表，
    // 于是就不存在两处状态不同步的问题。
    private IReadOnlyList<string> PickFilesForUpload()
    {
        var dlg = new Microsoft.Win32.OpenFileDialog
        {
            Multiselect = true,
            CheckFileExists = true,
            Title = "选择要发给模型的附件（服务端会解析文件内容）",
        };
        return dlg.ShowDialog(this) == true ? dlg.FileNames : Array.Empty<string>();
    }

    private void ToggleMaximize() =>
        WindowState = WindowState == WindowState.Maximized
            ? WindowState.Normal
            : WindowState.Maximized;

    /// <summary>
    /// 起拖。最大化状态下要先还原，并且把窗口挪到指针下方 ——
    /// 不这么做的话，还原后的窗口会「跳」到屏幕左上角（原生标题栏不是这个手感）。
    /// </summary>
    private void BeginDrag()
    {
        if (WindowState == WindowState.Maximized)
        {
            var w = RestoreBounds.Width;              // 还原尺寸（此时还没还原）
            if (TryCursorDip(out var px, out var py))
            {
                // 指针落点占整屏宽的比例，映射到还原后窗口的同一比例处
                var wa = SystemParameters.WorkArea;
                var ratio = wa.Width > 0 ? (px - wa.Left) / wa.Width : 0.5;
                ratio = Math.Clamp(ratio, 0.05, 0.95);
                WindowState = WindowState.Normal;
                Left = px - ratio * w;
                Top = py - 20;
            }
            else
            {
                WindowState = WindowState.Normal;
            }
        }

        StartSystemMove();
    }

    // ---------- 标题栏拖拽：为什么不能用 Window.DragMove() ----------
    //
    // 原实现是 `DragMove()`，它**必然抛异常**：
    //     InvalidOperationException: Can only call DragMove when primary mouse button is down.
    //
    // 原因：WPF 的 Mouse.LeftButton 不是去读系统实时按键状态（GetAsyncKeyState），
    // 而是由 **WPF 窗口自己收到的 WM_LBUTTONDOWN / WM_LBUTTONUP 维护**的一个内部
    // 状态。我们的标题栏在 WebView2 里，按下事件落在 **Chromium 的子 HWND** 上，
    // WPF 顶层窗口从头到尾没收到过任何鼠标消息 —— 于是 Mouse.LeftButton 恒为
    // Released，DragMove 的前置检查永远不过。
    //
    // 也就是说：窗口按钮（min/max/close）一直好使，唯独「拖标题栏挪窗口」是死的。
    // 而且它死得没有声音 —— 异常被 catch 吞掉，用户只会觉得"拖不动"。
    //
    // 绕过办法：DragMove() 内部做的事其实就是「释放捕获 → 给窗口发
    // WM_NCLBUTTONDOWN + HTCAPTION，让系统的移动循环接管」。我们跳过它那个
    // 前置检查，自己发这条消息。lParam 要带当前光标屏幕坐标，作为移动起点。
    //
    // （Electron 里不需要这些：-webkit-app-region:drag 是浏览器进程在窗口层面
    //   实现的，压根不经过 IPC。WPF + WebView2 没有对应物，只能自己做。）
    private const int WM_NCLBUTTONDOWN = 0x00A1;
    private const int HTCAPTION = 2;

    private void StartSystemMove()
    {
        var hwnd = new WindowInteropHelper(this).Handle;
        if (hwnd == IntPtr.Zero) return;

        GetCursorPos(out var pt);
        var lParam = unchecked((IntPtr)((pt.Y << 16) | (pt.X & 0xFFFF)));

        ReleaseCapture();
        // 这条 SendMessage 会一直阻塞到用户松开鼠标（系统移动循环在里面跑），
        // 与 DragMove() 的行为一致。
        SendMessage(hwnd, WM_NCLBUTTONDOWN, (IntPtr)HTCAPTION, lParam);
    }

    // ---------- 无边框窗口最大化：不许盖住任务栏 ----------

    protected override void OnSourceInitialized(EventArgs e)
    {
        base.OnSourceInitialized(e);
        if (PresentationSource.FromVisual(this) is HwndSource src)
            src.AddHook(WndProc);
    }

    private const int WM_GETMINMAXINFO = 0x0024;
    private const int MONITOR_DEFAULTTONEAREST = 0x00000002;

    /// <summary>
    /// WindowStyle="None" 的窗口最大化时会盖住任务栏：实测窗口矩形变成
    /// (-10, -10) ~ (1930, 1210)，比工作区大一圈，任务栏（含时钟）整条被盖。
    /// 拦 WM_GETMINMAXINFO，把「最大化尺寸/位置」改成正好等于工作区。
    /// </summary>
    private static IntPtr WndProc(IntPtr hwnd, int msg, IntPtr w, IntPtr l, ref bool handled)
    {
        if (msg != WM_GETMINMAXINFO) return IntPtr.Zero;

        var mmi = Marshal.PtrToStructure<MINMAXINFO>(l);
        var mon = MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST);
        var mi = new MONITORINFO { cbSize = Marshal.SizeOf<MONITORINFO>() };
        if (GetMonitorInfo(mon, ref mi))
        {
            var wa = mi.rcWork;       // 工作区（已扣掉任务栏）
            var mo = mi.rcMonitor;    // 整块显示器
            mmi.ptMaxPosition.X = wa.Left - mo.Left;
            mmi.ptMaxPosition.Y = wa.Top - mo.Top;
            mmi.ptMaxSize.X = wa.Right - wa.Left;
            mmi.ptMaxSize.Y = wa.Bottom - wa.Top;
            mmi.ptMaxTrackSize.X = wa.Right - wa.Left;
            mmi.ptMaxTrackSize.Y = wa.Bottom - wa.Top;
            Marshal.StructureToPtr(mmi, l, true);
            handled = true;
        }
        return IntPtr.Zero;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct MINMAXINFO
    {
        public POINT ptReserved;
        public POINT ptMaxSize;
        public POINT ptMaxPosition;
        public POINT ptMinTrackSize;
        public POINT ptMaxTrackSize;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct RECT { public int Left, Top, Right, Bottom; }

    [StructLayout(LayoutKind.Sequential)]
    private struct MONITORINFO
    {
        public int cbSize;
        public RECT rcMonitor;
        public RECT rcWork;
        public int dwFlags;
    }

    [DllImport("user32.dll")]
    private static extern IntPtr MonitorFromWindow(IntPtr hwnd, int flags);

    [DllImport("user32.dll")]
    private static extern bool GetMonitorInfo(IntPtr hMonitor, ref MONITORINFO mi);

    // ---------- 标题栏拖拽（见 BeginDrag 上方那段注释） ----------

    [DllImport("user32.dll")]
    private static extern bool ReleaseCapture();

    [DllImport("user32.dll")]
    private static extern IntPtr SendMessage(IntPtr hWnd, int msg, IntPtr wParam, IntPtr lParam);

    // ---------- 屏幕坐标（给「最大化时起拖」用） ----------

    [System.Runtime.InteropServices.DllImport("user32.dll")]
    private static extern bool GetCursorPos(out POINT p);

    [System.Runtime.InteropServices.StructLayout(
        System.Runtime.InteropServices.LayoutKind.Sequential)]
    private struct POINT { public int X; public int Y; }

    /// <summary>
    /// 光标位置，转成 WPF 的 DIP。
    ///
    /// GetCursorPos 给的是物理像素；WPF 的 Left/Top 是 DIP，高 DPI 下直接拿
    /// 物理像素去设 Left 会偏。用 VisualTreeHelper.GetDpi 换算，不引
    /// System.Drawing（那是个 NuGet 包，会破零依赖）。
    /// </summary>
    private bool TryCursorDip(out double x, out double y)
    {
        x = y = 0;
        if (!GetCursorPos(out var p)) return false;

        var dpi = VisualTreeHelper.GetDpi(this);
        x = p.X / dpi.DpiScaleX;
        y = p.Y / dpi.DpiScaleY;
        return true;
    }

    protected override void OnClosed(EventArgs e)
    {
        // 必须退订：窗口关了但 AgentHost 还活着（它是 App 级的），
        // 不退订就会继续往一个已释放的 WebView2 上推消息。
        try
        {
            var host = App.Host;
            if (host is not null)
            {
                host.ItemAdded -= OnItemAdded;
                host.ItemChanged -= OnItemChanged;
                host.TranscriptCleared -= OnTranscriptCleared;
                host.Logged -= OnLogged;
                host.StatusChanged -= OnStatusChanged;
                host.RunningChanged -= OnRunningChanged;
                host.RemoteSessionsChanged -= OnRemoteSessionsChanged;
                host.IdentityNeedsLogin -= OnIdentityNeedsLogin;
            }
            if (Web.CoreWebView2 is { } core) core.WebMessageReceived -= OnWebMessage;
        }
        catch { /* 退出路径不抛 */ }

        base.OnClosed(e);
    }
}
