# AGENTS.md — deepseek-agent-desktop

> 给所有 agent 的进门须知。**动手前先读完这一页，尤其是「外壳归属」那节。**

## 外壳归属（最容易搞错的一条）

| 目录 | 状态 | 说明 |
|---|---|---|
| `shell/DeepSeekAgent.Web/` | **唯一主线** | WebView2 + 本地 HTML/JS。改 UI 只改这里 |
| `shell/Shared/` | **活的代码** | 宿主层 6 个 .cs + `Assets/app.ico`，被 Web 壳与 selftest 链接引用 |
| `shell/selftest/` | **活的代码** | 共享层离线自测，可独立构建运行 |
| `shell/_archive/wpf-shell/` | **已归档 2026-10-20** | 不参与构建，不维护。**不要在这里改任何东西** |
| `winui/` | **冻结** | WinUI 3 骨架，编译未通过，不是主线 |

**判定依据**：当前运行的是 `shell/DeepSeekAgent.Web/bin/Release/net8.0-windows/DeepSeekAgent.Web.exe`。

### 陷阱：`namespace DeepSeekAgent.Desktop` 不代表 WPF 是主线

`shell/Shared/` 里的文件写的是 `namespace DeepSeekAgent.Desktop;`，Web 壳的 `RootNamespace` 也是它。
**这是历史命名，不是「WPF 是主线」的证据。** 判断主线只看目录与进程，不看 namespace。

## 关键约束

1. **不要移动 `shell/Shared/` 里的文件**。移动要同步改 4 个 csproj 的相对路径
   （Web 壳 7 处 + 3 个 selftest 各 1 处），漏一处就断构建。
2. **不要从 `shell/_archive/wpf-shell/` 复活代码**。真要复活，先读该目录的 `README.md`。
3. **改 `shell/Shared/` 后必须跑 3 个自测**：
   ```bash
   cd shell/selftest/FishTtsTest           && dotnet build -c Release && ./bin/Release/net8.0/FishTtsTest.exe
   cd shell/selftest/LogRetentionTest      && dotnet build -c Release && ./bin/Release/net8.0/LogRetentionTest.exe
   cd shell/selftest/StatusLogThrottleTest && dotnet build -c Release && ./bin/Release/net8.0/StatusLogThrottleTest.exe
   ```
4. **构建主壳时如果 exe 被占用**（进程在跑），用独立输出目录验证编译：
   ```bash
   dotnet build -c Release -p:BaseOutputPath=/tmp/dsbuild/ -p:BaseIntermediateOutputPath=/tmp/dsbuild/obj/
   ```
   注意：换 `BaseIntermediateOutputPath` 前必须先 `rm -rf obj`，否则旧 `.g.cs` 会被二次编译，报一堆 `CS0102` 假错误。

## 架构要点

- 传输层可插拔：`engine/gad_engine/transport/`，档位 `deepseek_rpc` / `chrome` / `gemini_rpc`，
  工厂函数 `create_transport(cfg)`，壳内下拉框切换。
- C# 壳 ⇄ Python 引擎走 JSON-RPC（`engine/gad_engine/__main__.py`），
  方法名如 `config.set` / `config.transports` / `identity.*` / `conversation.*`。
- 凭据：DeepSeek 在 `engine/.token`（明文，**禁止入库**）；
  Gemini 在 `%LOCALAPPDATA%\Gemini-Agent\cookies-<label>.json`。
- 会话与记忆**不按档位/账号隔离**：`conversations/` 与 `MEMORY.md` 是全局单份。

## 项目规则

工作流、Vault 铁律、记账要求见用户全局规则
`C:\Users\Admin\Documents\ObsidianVault\02-System-Rules\Agent-Rules.md`。
