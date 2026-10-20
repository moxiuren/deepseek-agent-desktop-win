# WinUI 3 新壳（`winui/`，分支 `feature/winui3-shell`）

> 老 WPF 壳在 `shell/` 原样保留。本目录是新壳骨架，**尚未能编译运行**（欠缺见下）。
> 引擎是同一个（`engine/gad_engine`，stdio JSON-RPC），只换壳 —— 引擎契约见本 README 末尾。

## 架构（与 WPF 版同构，只换 UI 框架）

```
winui/WinUI3Desktop/   WinUI 3 外壳（Microsoft.UI.Xaml）
engine/gad_engine/     引擎（不动，stdio JSON-RPC 服务端）
```

## 本机编译条件（2026-10-16 实测）

| 项 | 状态 |
|---|---|
| .NET SDK | 8.0.424（够） |
| `dotnet workload list` | **空** —— WinUI 3 的 `windows` 工作负载没装 |
| `Microsoft.WindowsAppRuntime.*` | 本机有 1.5–2.x **运行时**（能跑别人装好的 WinUI3 应用，不能编译） |
| `Microsoft.WindowsAppSDK` NuGet 包 | 已拉通，`1.*` 浮动实际落到 **1.8.260803003**（`%USERPROFILE%\.nuget\packages`，restore 成功） |

第一道门：`dotnet workload install windows`（数 GB，需管理员）或装 VS 的
“Windows App SDK C# 模板”组件。**没过这道门之前，`dotnet build` 必败** ——
这不是代码问题，是环境问题。

## 编译验证（2026-10-16 14:45 实测，未通过）

```powershell
cd winui/WinUI3Desktop
dotnet build -c Release
# restore 成功（WinAppSDK 1.8.260803003），倒在 XAML 编译器：
# error WMC9999: Access to the path '...obj\Release\...\App.g.i.cs' is denied.
# error : XamlCompiler output file "...output.json" was not created.
```

已排除：obj 目录本身可写（PowerShell 建删文件正常）、无残留编译器进程。
剩下两种可能：① XAML 编译器子进程的 ACL/完整性与本机安全策略冲突
（本机拦过 COM/cscript 等，见既有踩坑）；② 同步工具（Syncthing 试点同步）
与新目录写竞争。下一步：procmon 抓拒绝方，或搬到非同步目录复现。
在查清前不要装 workload / 不要钉死版本号 —— 现在的问题不在那两样。

## 欠缺清单（用户要求逐项说明）

按“能跑起来 → 能用 → 好用”排序：

### P0 · 跑起来（缺一不可）
1. **编译环境**：装 workload（见上表）。预计下载数 GB + 较长安装时间。
2. **NuGet 版本钉死**：`Microsoft.WindowsAppSDK` 当前 `1.*` 浮动，首次 restore 成功后钉死具体号并记录。
3. **非打包启动验证**：`WindowsPackageType=None` + 自包含只是纸面配置，需真机双击验证（动态依赖、AppData 落点、`--selfcheck` 类无窗模式在 WinUI3 下是否可行，WPF 那套“构造窗口不显示”在 WinUI3 里受限更多）。

### P1 · 能用（对照 WPF 版逐项搬运，约 2–4 周）
4. **引擎客户端**：子进程拉起 + stdio JSON-RPC + 事件分发（`EngineClient` + `AgentHost` 事件机的 WinUI3 版，`DispatcherQueue` 代替 `Dispatcher`）。
5. **对话流**：ItemsRepeater 虚拟化 + 流式追加 + 定稿替换（WPF 的 SealStreamingBlock 在新框架下重写，旧坑：下标替换语义、滚动跟随、字号缩放遍历）。
6. **思考块 / 工具卡片三态**：含 1.5s 进度心跳与“展开全文”。
7. **身份对话框 + 设置页**（单 token / 提示词模板编辑器）。
8. **侧栏对话列表 + 服务端历史同步 + 记忆管理**（搬 `remote.py` 已在引擎侧，无需重做；UI 侧重写）。
9. **附件 / 匀速输出 / 热加载**：WPF 分支上刚做的三件套，机制照搬（松散 XAML 在 WinUI3 下叫法与加载 API 不同，需重写）。

### P2 · 好用（门禁与交付）
10. **门禁对等实现**：selfcheck / layout-audit / perfcheck 的 WinUI3 版（重点：600 块长列表开合耗时重测，WPF 的 0.4ms 数字作废）。
11. **打包与启动器**：单 exe（自包含）+ 桌面 `.bat`（GBK）+ 自检 `check` 分支。
12. **Emoji 彩色**：WinUI3 原生支持 COLR，WPF 那整段 saga 不会重演（预期零工作，待验证）。

## 引擎契约（新旧壳共用，改壳不动它）

- 入口：`python -m gad_engine`（stdio JSON-RPC）。
- 方法：`initialize` / `identity.*` / `conversation.*` / `memory.*` / `chat.set` /
  `prompt.get-set-reset` / `status` / `run.cancel`。
- 事件：`delta` / `thinking` / `thought` / `pow_stats` / `sent` /
  `tool_start-progress-done` / `stream` / `turn_start-turn_end` / `run_end`。
