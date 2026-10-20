#!/usr/bin/env python3
"""极简 CDP 客户端 —— 直连 page 级 WebSocket。

## 为什么不直接用 playwright 的 connect_over_cdp

WebView2 的 CDP 端点 `/json/list` 正常，但 `connect_over_cdp` 时常拿到
0 个 context（`browser.contexts[0]` 抛 IndexError）。原因是 WebView2
不实现 Browser 域的 Target 自动附着，playwright 依赖它枚举 page。

绕过办法：`/json/list` 里每个 page 自带 `webSocketDebuggerUrl`，
直接连上去发 `Runtime.evaluate`。这条路不经过 Target 域，稳定得多。

## 只做一件事

`evaluate(expr)` → 在页面里跑一段表达式，返回 JSON 值。
支持 async（`awaitPromise: true`），因为页面里大量探针是 async 函数。
"""
from __future__ import annotations

import itertools
import json

from websocket import create_connection


class CdpError(RuntimeError):
    pass


class CdpPage:
    """一个 page 级 CDP 会话。用 with 保证连接关闭。"""

    def __init__(self, ws_url: str, timeout: float = 60.0):
        # Chromium 93+ 的 CDP 会校验 WebSocket 的 Origin 头，非白名单一律 403。
        # suppress_origin 让它干脆不发 Origin —— 这是本地回环调试的标准做法，
        # 比去改浏览器启动参数 `--remote-allow-origins=*` 干净（那个会放宽到所有来源）。
        self._ws = create_connection(ws_url, timeout=timeout, suppress_origin=True)
        self._ids = itertools.count(1)
        self._timeout = timeout

    def __enter__(self) -> "CdpPage":
        return self

    def __exit__(self, *exc) -> None:
        try:
            self._ws.close()
        except Exception:
            pass

    def _call(self, method: str, params: dict | None = None) -> dict:
        mid = next(self._ids)
        self._ws.send(json.dumps({"id": mid, "method": method,
                                  "params": params or {}}))
        while True:
            raw = self._ws.recv()
            msg = json.loads(raw)
            # 事件（没有 id）跳过 —— 本客户端不订阅事件，只等自己的回包。
            if msg.get("id") != mid:
                continue
            if "error" in msg:
                raise CdpError(f"{method}: {msg['error'].get('message')}")
            return msg.get("result", {})

    def evaluate(self, expr: str, await_promise: bool = True):
        """跑一段 JS 表达式，返回其值。

        `expr` 可以是裸表达式，也可以是 `() => {...}` 形式的函数 ——
        后者会被包成 IIFE 调用，方便写多语句探针。
        """
        wrapped = expr
        res = self._call("Runtime.evaluate", {
            "expression": wrapped,
            "returnByValue": True,
            "awaitPromise": await_promise,
            "userGesture": True,
        })
        if res.get("exceptionDetails"):
            det = res["exceptionDetails"]
            text = (det.get("exception") or {}).get("description") or det.get("text")
            raise CdpError(f"页面异常：{text}")
        return (res.get("result") or {}).get("value")
