"""T1 降级通道：WebView DOM 注入 transport（骨架）。

本文件是 `Transport` ABC 的 T1 实现占位。真正的 DOM 注入逻辑应从
`deepseek-mac/agent_bridge.js`（155 KB，已实战跑通）移植过来；那部分工作
需要 WebView2/CDP 运行环境 + 真实账号才能端到端验证，因此**不在离线窗口内**。

离线窗口内只做三件事，全部可验证、不发任何真实请求：
  1. 提供一个可导入、可实例化的 `WebViewTransport`，让 `DegradeTarget.WEBVIEW`
     从"标记"变成"有落点"；
  2. 所有网络相关方法**默认抛 `TransportError(config=True)`**，而不是静默
     no-op —— 与 base.py 的约定一致，避免"切档位后功能静默失效"；
  3. 通过 `test_webview.py` 钉死上述契约。

移植完成前，`start()` 会抛 `TransportError(fatal=True)`，明确告知调用方
"T1 尚未实现"，避免误用。
"""

from __future__ import annotations

from .base import RemoteStatus, Transport, TransportError, TurnOutcome


class WebViewTransport(Transport):
    """T1 降级 transport：WebView DOM 注入。

    状态：**骨架，未实现**。所有方法默认抛错，防止被误用为可用档位。
    """

    name = "webview"
    supports_streaming = False

    def __init__(self, *args, **kwargs) -> None:
        self._args = args
        self._kwargs = kwargs
        self._started = False

    async def start(self) -> None:
        raise TransportError(
            "WebViewTransport (T1) 尚未实现：DOM 注入逻辑待从 "
            "deepseek-mac/agent_bridge.js 移植",
            fatal=True,
        )

    async def stop(self) -> None:
        self._started = False

    async def health(self) -> RemoteStatus:
        return RemoteStatus(reachable=False, note="T1 未实现")

    async def send(self, prompt, session, **kwargs) -> TurnOutcome:  # type: ignore[override]
        raise TransportError(
            "WebViewTransport (T1) 尚未实现", fatal=True
        )

    async def list_sessions(self, count: int = 30) -> list:  # type: ignore[override]
        raise TransportError("WebViewTransport (T1) 不支持列出会话", config=True)

    async def fetch_history(self, chat_session_id: str) -> dict:  # type: ignore[override]
        raise TransportError("WebViewTransport (T1) 不支持读取会话历史", config=True)
