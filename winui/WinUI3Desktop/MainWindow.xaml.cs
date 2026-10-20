using Microsoft.UI.Xaml;

namespace DeepSeekAgent.WinUI3;

/// <summary>
/// WinUI 3 主窗口（骨架）。
/// 引擎接线点（对照 WPF 版 AgentHost + EngineClient，逐项搬运）：
/// TODO-1 引擎子进程拉起（python -m gad_engine，stdio JSON-RPC）；
/// TODO-2 initialize / conversation.* / memory.* / chat.set / prompt.*；
/// TODO-3 事件流：delta / thinking / pow_stats / tool_start-progress-done；
/// TODO-4 身份对话框（单 token）/ 设置页（提示词模板）/ 侧栏对话列表；
/// TODO-5 门禁：selfcheck / layout-audit / perfcheck 的 WinUI3 对等实现
/// （WinUI3 跑“不显示窗口的自检”比 WPF 受限，方案待定）。
/// </summary>
public sealed partial class MainWindow : Window
{
    public MainWindow()
    {
        InitializeComponent();
    }
}
