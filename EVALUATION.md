# 评估指南

> 这份文件是给**代码评估者**看的。它说明这个项目是什么、从哪读起、
> 以及哪些地方需要重点审视。发布快照已做凭证脱敏，见文末「已脱敏内容」。

## 这是什么

把 **DeepSeek 网页版**当作后端驱动的桌面 Agent。核心不是"调用 API"，
而是一条**可替换的传输层** —— 上层（会话、工具、记忆、UI）完全不关心字节
是怎么发出去的，换档位只换 `engine/gad_engine/transport/` 下的一个类。

它模拟真实浏览器，在网页版上完成：多轮对话、工具调用、文件上传、TTS 朗读、
会话列表管理。桌面外壳是 WebView2 + 本地 HTML/JS。

## 从哪读起

按这个顺序读，能最快建立心智模型：

| 顺序 | 文件 | 读它为了知道 |
|---|---|---|
| 1 | `README.md` | 项目定位、目录、三个传输档位 |
| 2 | `engine/gad_engine/transport/base.py` | 传输层抽象契约。**所有档位必须实现的方法都在这里** |
| 3 | `engine/gad_engine/transport/deepseek_rpc.py` | 主档位（curl_cffi）。协议细节、PoW、请求头对齐 |
| 4 | `engine/gad_engine/transport/chrome_cdp.py` | 浏览器档位（真实 Chrome + CDP） |
| 5 | `engine/gad_engine/agent.py` | 会话主循环：轮换、续接锚点、流式收包 |
| 6 | `engine/gad_engine/session.py` | `SessionMeta` 的字段语义（`cid` / `rid` / `attached`） |
| 7 | `engine/gad_engine/pow.py` | DeepSeekHashV1 工作量证明求解 |
| 8 | `shell/Shared/AgentHost.cs` | 宿主层：C# 壳 ⇄ Python 引擎的 JSON-RPC 桥 |
| 9 | `shell/DeepSeekAgent.Web/Assets/web/app.js` | 前端。**列表的事实来源只有服务端**，本地文件只是缓存 |

## 设计取向（评估时值得注意）

这个项目有一以贯之的取向：**宁可报错，也不要静默失效**。

具体体现：

- `Transport` 的可选接口默认**抛 `TransportError`**，不是静默 `pass`。
  代价是切档位时必须逐条核对基类每个方法有没有实现（有过实际事故）。
- 服务端 `count > 100` 时**静默返回空列表**而非报错 —— 代码里提前拦住了，
  注释写明"避免「读不到会话」被误判成「没有会话」"。
- 会话列表只认服务端，本地 `conversations/*.json` 降级为缓存。
  原因是本地文件不记录归属账号，换号后会拿着新 token 去请求别人的会话。

这些取舍在代码注释里都有成段的说明，读注释比读实现更能理解意图。

## 重点审视方向

如果目的是**安全性评估**，这几处值得看：

- `engine/gad_engine/identity.py` —— 凭据归一化。处理用户手粘 token 的各种形态，
  有一条回归测试专门防"未登录时把整段 JSON 当 token 存进去"的数据损坏。
- `engine/gad_engine/safety.py` / `gates.py` / `permissions.py` —— 工具执行的闸门。
- `engine/gad_engine/sandbox.py` —— 命令执行的沙箱边界。
- `engine/gad_engine/tools_fs.py` / `tools_git.py` / `tools_patch.py` —— 文件与命令操作。
- `engine/gad_engine/transport/deepseek_rpc.py` 的 `send()` —— 双闸门
  `GAD_ALLOW_LIVE_AUTH=1` 才会真的向上游发消息。

如果目的是**功能性评估**，看 `engine/gad_engine/agent.py` 的主循环、
`tools_*.py` 的工具集、以及 `shell/DeepSeekAgent.Web/Assets/web/app.js` 的交互流程。

## 已知缺口

诚实列出，避免评估者把它们当成"没发现的问题"：

- `engine/tests/test_engine_rpc.py` 有一条断言失败（`guest_mode` 字段），
  与传输层无关，属待决事项。
- `chrome_cdp.py` 超过 500 行，未按职责拆分。
- 浏览器档位需要本机装 Chrome，CI 里跑不了，只能手工验证。
- `docs/evidence/`（原始抓包证据）**不在本快照内** —— 见下。

## 已脱敏内容

本快照在发布前做了以下处理，以保证不含可用凭据：

| 内容 | 处理 |
|---|---|
| 完整 Bearer token（71 字符） | 原始抓包文件整体排除，不进库 |
| `docs/evidence/` 全部抓包 | 整体排除（`.gitignore` 已声明） |
| 真实邮箱 | 替换为 `user@example.com` / `<gemini-account>@example.com` |
| 真实 device-id / 账号 id / 会话 id | 替换为 `<device-id>` / `<account-id>` / `<session-id>` 等占位符 |
| `.token` / `.device-id` | 从未复制进快照 |
| 运行期产物（`sess_tmp.json`、`_cleanup_plan.json`、截图） | 排除 |

占位符只替换了**值**，保留了文档的结论与表格结构 —— 协议知识完整，
但无法据此还原任何真实凭据。
