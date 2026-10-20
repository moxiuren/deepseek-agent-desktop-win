"""T1 降级通道（WebViewTransport）骨架契约自测。

离线可跑、不发任何真实请求。钉死：
  1. 可导入、可实例化
  2. name = "webview"
  3. start() 抛 fatal TransportError（明确"未实现"）
  4. send() 抛 fatal TransportError
  5. health() 返回 not reachable
  6. list_sessions / fetch_history 抛 config TransportError
  7. available_models 默认空列表
用法：
    cd engine && ./.venv/Scripts/python.exe tests/test_webview.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gad_engine.transport.base import TransportError
from gad_engine.transport.webview import WebViewTransport

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    mark = "通过" if cond else "失败"
    extra = f" — {detail}" if detail and not cond else ""
    print(f"  [{mark}] {name}{extra}")


def main() -> int:
    print("=" * 70)
    print("T1 降级通道 · WebViewTransport 骨架契约自测")
    print("=" * 70)

    t = WebViewTransport()
    check("可实例化", t is not None)
    check("name = webview", t.name == "webview")

    async def run() -> None:
        # start 抛 fatal
        try:
            await t.start()
            check("start() 抛错", False, "未抛错")
        except TransportError as e:
            check("start() 抛 fatal TransportError", bool(e.fatal))

        # health not reachable
        h = await t.health()
        check("health() not reachable", not h.reachable)

        # send 抛 fatal
        try:
            await t.send("hi", None)  # type: ignore[arg-type]
            check("send() 抛错", False, "未抛错")
        except TransportError as e:
            check("send() 抛 fatal TransportError", bool(e.fatal))

        # list_sessions 抛 config
        try:
            await t.list_sessions()
            check("list_sessions() 抛错", False, "未抛错")
        except TransportError as e:
            check("list_sessions() 抛 config TransportError", bool(e.config))

        # fetch_history 抛 config
        try:
            await t.fetch_history("x")
            check("fetch_history() 抛错", False, "未抛错")
        except TransportError as e:
            check("fetch_history() 抛 config TransportError", bool(e.config))

        check("available_models 默认空", t.available_models() == [])

    asyncio.run(run())

    print()
    print(f"  通过 {len(PASS)} · 失败 {len(FAIL)}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
