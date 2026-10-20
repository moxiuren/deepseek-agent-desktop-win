using Microsoft.UI.Xaml;

namespace DeepSeekAgent.WinUI3;

/// <summary>WinUI 3 新壳入口。引擎沿用同一个（engine/gad_engine，stdio JSON-RPC）。</summary>
public partial class App : Application
{
    private Window? _window;

    public App()
    {
        InitializeComponent();
    }

    protected override void OnLaunched(LaunchActivatedEventArgs args)
    {
        _window = new MainWindow();
        _window.Activate();
    }
}
