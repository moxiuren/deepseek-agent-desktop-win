"""web 工具执行层接线：ToolCall -> AgentRuntime._execute。

用本地假 HTTP 服务器，不发真实外部请求。

用法：
    cd engine && ./.venv/Scripts/python.exe tests/test_web_exec.py
"""

from __future__ import annotations

import asyncio
import http.server
import sys
import threading
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gad_engine.agent import AgentRuntime
from gad_engine.protocol import ToolCall, ToolKind

PASS: list[str] = []
FAIL: list[str] = []

PAGE = "<html><head><title>T</title></head><body><h1>标题</h1><p>正文内容</p></body></html>"


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
    else:
        FAIL.append(name + (f" — {detail}" if detail else ""))


async def run_checks(base: str) -> None:
    rt = AgentRuntime.__new__(AgentRuntime)

    # ---------- 1. web_fetch 分发 ----------
    print("\n[1] web_fetch 分发")
    call = ToolCall(kind=ToolKind.WEB_FETCH, target=base, payload="")
    ex, text, _attach = await rt._execute(call)  # type: ignore[misc]
    check("exit_code=0", ex.exit_code == 0, ex.output[:120])
    check("kind 正确", ex.kind == "web_fetch", ex.kind)
    check("提取标题", "T" in ex.output, ex.output[:200])
    check("提取正文", "正文内容" in ex.output, ex.output[:300])
    check("回传文本有收尾", "请输出下一个" in text, text[-60:])

    # ---------- 2. web_fetch 失败 ----------
    print("\n[2] web_fetch 失败")
    call = ToolCall(kind=ToolKind.WEB_FETCH, target="file:///etc/passwd", payload="")
    ex, _text, _attach = await rt._execute(call)  # type: ignore[misc]
    check("拒绝 file 协议", ex.exit_code == 1, ex.output[:120])

    call = ToolCall(kind=ToolKind.WEB_FETCH, target="", payload="")
    ex, _text, _attach = await rt._execute(call)  # type: ignore[misc]
    check("空 URL 报错", ex.exit_code == 1, ex.output[:120])

    # ---------- 3. web_search 分发（错误路径）----------
    print("\n[3] web_search")
    call = ToolCall(kind=ToolKind.WEB_SEARCH, target="", payload="")
    ex, text, _attach = await rt._execute(call)  # type: ignore[misc]
    check("空搜索词报错", ex.exit_code == 1, ex.output[:120])
    check("kind 正确", ex.kind == "web_search", ex.kind)


def main() -> int:
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        asyncio.run(run_checks(f"http://127.0.0.1:{port}/"))
    finally:
        server.shutdown()

    print("\n" + "-" * 70)
    print(f"  通过 {len(PASS)} · 失败 {len(FAIL)}")
    if FAIL:
        print("  失败项：")
        for name in FAIL:
            print(f"    - {name}")
    print("-" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
