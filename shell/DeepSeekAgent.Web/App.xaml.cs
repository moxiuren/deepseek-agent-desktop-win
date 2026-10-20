using System.IO;
using System.Windows;

namespace DeepSeekAgent.Desktop;

/// <summary>
/// WebView2 外壳的应用入口。
///
/// 与 WPF 外壳（shell/DeepSeekAgent.Desktop）的关系：**共用同一个 AgentHost**，
/// 只是渲染层不同。所以这里只做三件事 —— 建 Host、启动它、开窗口。
/// 其余（CLI 门禁开关、对话框、字号缩放）都是 WPF 壳的渲染层职责，不在这里。
/// </summary>
public partial class App : Application
{
    /// <summary>全局唯一的会话层。窗口只是它的视图。</summary>
    public static AgentHost Host { get; private set; } = null!;

    private MainWindow? _main;

    public App()
    {
        // 启动阶段崩掉时用户只看到「窗口没出来」。落到文件里至少能查。
        DispatcherUnhandledException += (_, e) =>
        {
            WriteCrashLog(e.Exception);
            e.Handled = false;
        };
        AppDomain.CurrentDomain.UnhandledException += (_, e) =>
            WriteCrashLog(e.ExceptionObject as Exception);
    }

    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);

        Host = new AgentHost(Dispatcher);
        _ = Host.StartAsync();

        _main = new MainWindow();
        _main.Show();
    }

    protected override void OnExit(ExitEventArgs e)
    {
        // 引擎是子进程，不显式收会把 python.exe 留在后台。
        try { Host?.DisposeAsync().AsTask().Wait(3000); } catch { /* 退出路径不抛 */ }
        base.OnExit(e);
    }

    private static void WriteCrashLog(Exception? ex)
    {
        if (ex is null) return;
        try
        {
            var dir = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "DeepSeek-Agent", "logs");
            Directory.CreateDirectory(dir);
            var path = Path.Combine(dir, $"web-crash-{DateTime.Now:yyyyMMdd}.log");
            File.AppendAllText(path,
                $"\n===== {DateTime.Now:yyyy-MM-dd HH:mm:ss} =====\n{ex}\n");
        }
        catch { /* 记日志本身失败就放弃，不要因此再抛 */ }
    }
}
