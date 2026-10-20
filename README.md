# deepseek-agent-desktop

把 DeepSeek 网页版当作后端驱动的桌面 Agent 引擎。核心是一条**可替换的传输层**：
上层（会话、工具、记忆、UI）完全不关心「字节是怎么发出去的」，换档位只换
`engine/gad_engine/transport/` 下的一个类。

## 目录

| 路径 | 作用 |
|---|---|
| `engine/gad_engine/transport/` | 传输层。`base.py` 是抽象，`deepseek_rpc.py`（T3，curl_cffi 协议档）与 `chrome_cdp.py`（T4，真实 Chrome 内核档）是两个实现 |
| `engine/gad_engine/` | 引擎本体：会话、PoW、身份、工具、记忆、闸门 |
| `engine/tests/` | 自测脚本（`unittest` 风格，可直接运行） |
| `shell/DeepSeekAgent.Web/` | **桌面外壳（唯一主线）**：WebView2 + 本地 HTML/JS 渲染 |
| `shell/Shared/` | 宿主层共享代码，被 Web 壳与 selftest 链接引用（活的代码，不是归档） |
| `shell/selftest/` | 共享层离线自测，可独立构建运行 |
| `shell/_archive/wpf-shell/` | 已归档的 WPF 外壳，**不参与构建、不再维护** |
| `winui/` | WinUI 3 骨架，**编译未通过，冻结状态**，不是主线 |
| `tools/` | 运维与验收脚本（见下） |
| `docs/` | 逆向证据、协议文档、实测记录 |

> **外壳归属（别搞错）**：主线是 `shell/DeepSeekAgent.Web/`（WebView2 + HTML/JS）。
> WPF 壳已于 2026-10-20 归档至 `shell/_archive/wpf-shell/`，详见该目录的 `README.md`。
> `shell/Shared/` 里的 `namespace DeepSeekAgent.Desktop` 是历史命名，**不代表 WPF 是主线**。

## 三个传输档位

| 档位 | 出口 | 特点 |
|---|---|---|
| `deepseek_rpc` | `curl_cffi` 模拟 Chrome TLS | 轻量、无需浏览器；HTTP 头需手工对齐 |
| `chrome` | 真实 Chrome（CDP 驱动） | 指纹天然真实；需本机装 Chrome，启动约 6–7 秒 |
| `gemini_rpc` | Gemini 网页版（`gemini_webapi`） | 凭据在 `%LOCALAPPDATA%\Gemini-Agent\cookies-<label>.json`；壳内下拉框可切换 |

浏览器档位的行为约束：

* **headful**。`--headless=new` 的 UA 带 `HeadlessChrome`，是可检测破绽。
* 窗口启动后 `SW_HIDE` 隐藏 —— 屏幕外 + 隐藏，用户看不到窗口、任务栏无图标。
* **实测隐藏不影响流式**（`visibilityState=hidden` 下仍是 39 帧 / 跨度 ~140 ms）。
* 独立临时 profile，不复用用户日常 profile。
* 空闲 15 分钟自动关闭 Chrome，下次发送时自动重建。

## 验收

分段跑，失败点即答案：

```bash
# 只读：起 Chrome + 注入登录态 + 读账号
engine/.venv/Scripts/python.exe -X utf8 tools/verify_chrome_transport.py

# 加免费接口：challenge + 本地 PoW 求解
engine/.venv/Scripts/python.exe -X utf8 tools/verify_chrome_transport.py --stage pow

# 真发一条（消耗额度，需双闸门）
GAD_ALLOW_LIVE_AUTH=1 engine/.venv/Scripts/python.exe -X utf8 \
  tools/verify_chrome_transport.py --stage send --i-know
```

回归测试：

```bash
for f in engine/tests/test_*.py; do engine/.venv/Scripts/python.exe -X utf8 "$f"; done
```

## 安全闸门

`send()` 只有在 `GAD_ALLOW_LIVE_AUTH=1` 时才真的向上游发消息。两个档位共用
这一条 —— 换实现不等于绕开风控纪律。测试脚本默认不该真发。

凭据只落在 `engine/.token`（不进版本库），代码与日志里不得出现明文。

## 环境

* Windows 10+，Python 3.13/3.14
* `pip install -r engine/requirements.txt`（`curl_cffi`、`websockets`）
* 浏览器档位另需本机安装 Google Chrome
