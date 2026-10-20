# shell/Shared —— 宿主层共享代码

**这 6 个文件是活的代码，不是归档。** 它们被主线外壳
`shell/DeepSeekAgent.Web/` 与 `shell/selftest/` 通过 `Compile Include` 链接引用，
**原文件零改动复用**。

| 文件 | 职责 |
|---|---|
| `AgentHost.cs` | 进程生命周期、会话状态机、工具调度、消息流水（最大的一块，约 3800 行） |
| `EngineClient.cs` | 与 Python 引擎的 JSON-RPC 编解码 |
| `DeltaPacer.cs` | 流式增量节流 |
| `StatusLogThrottle.cs` | 状态日志折叠 |
| `LogRetention.cs` | 日志保留策略 |
| `FishTts.cs` | Fish 语音合成的 HTTP 调用与配置存储 |
| `Assets/app.ico` | 应用图标，Web 壳的 `ApplicationIcon` 指向它 |

## 为什么在这里

2026-10-20 归档 WPF 壳时，这 6 个文件被从 `shell/DeepSeekAgent.Desktop/` 抽出。
它们与 UI 技术无关（只依赖 `Dispatcher`，属 `WindowsBase`，不是 WPF UI 类型；
`FishTts.cs` 只依赖 `System.Net.Http`），两个壳都能用，所以独立成层。

归档说明见 `shell/_archive/wpf-shell/README.md`。

## 约束

- **改这里的代码会同时影响主线壳与自测**，改完务必跑：
  ```bash
  cd shell/selftest/FishTtsTest            && dotnet build -c Release && ./bin/Release/net8.0/FishTtsTest.exe
  cd shell/selftest/LogRetentionTest       && dotnet build -c Release && ./bin/Release/net8.0/LogRetentionTest.exe
  cd shell/selftest/StatusLogThrottleTest  && dotnet build -c Release && ./bin/Release/net8.0/StatusLogThrottleTest.exe
  ```
- **不要移动这些文件**。移动就要同步改 4 个 csproj 的相对路径
  （Web 壳 7 处 + 3 个 selftest 各 1 处），漏一处就断构建。
- `namespace DeepSeekAgent.Desktop` 是历史命名，保留原样。**namespace 不代表主线归属**。
