"""MCP 执行层接线：ToolCall -> AgentRuntime._run_mcp。

用一个假 MCP server 跑真实往返，验证 agent 侧的分发与结果包装。

用法：
    cd engine && ./.venv/Scripts/python.exe tests/test_mcp_exec.py
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
import textwrap
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gad_engine import mcp
from gad_engine.agent import AgentRuntime
from gad_engine.protocol import ToolCall, ToolKind

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
    else:
        FAIL.append(name + (f" — {detail}" if detail else ""))


FAKE_SERVER = '''
import json, sys
TOOLS = [{"name": "echo", "description": "回声", "inputSchema": {}}]
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    method = msg.get("method")
    mid = msg.get("id")
    if method == "initialize":
        out = {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "serverInfo": {"name": "fake", "version": "1"}}}
    elif method == "tools/list":
        out = {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    elif method == "tools/call":
        args = (msg.get("params") or {}).get("arguments") or {}
        out = {"jsonrpc": "2.0", "id": mid, "result": {
            "content": [{"type": "text", "text": "echo: " + str(args.get("text", ""))}],
            "isError": False}}
    else:
        if mid is None:
            continue
        out = {"jsonrpc": "2.0", "id": mid, "error": {"message": "未知"}}
    if mid is not None:
        sys.stdout.write(json.dumps(out) + "\\n")
        sys.stdout.flush()
'''


async def run_checks(tmp: Path) -> None:
    server_py = tmp / "fake_server.py"
    server_py.write_text(textwrap.dedent(FAKE_SERVER), encoding="utf-8")

    rt = AgentRuntime.__new__(AgentRuntime)
    rt.mcp = mcp.McpManager()
    rt.mcp.load([mcp.McpServerConfig(name="fake", command=sys.executable, args=[str(server_py)])])
    try:
        # ---------- 1. mcp:list ----------
        print("\n[1] mcp:list")
        call = ToolCall(kind=ToolKind.MCP, target="list", payload="")
        ex, _text, _attach = await rt._execute(call)  # type: ignore[misc]
        check("exit_code=0", ex.exit_code == 0, ex.output[:120])
        check("kind 正确", ex.kind == "mcp", ex.kind)
        check("列出 echo", "fake/echo" in ex.output, ex.output[:200])

        # 空子命令默认 list
        call = ToolCall(kind=ToolKind.MCP, target="", payload="")
        ex, _text, _attach = await rt._execute(call)  # type: ignore[misc]
        check("空子命令默认 list", "echo" in ex.output, ex.output[:200])

        # ---------- 2. 调用工具 ----------
        print("\n[2] 调用工具")
        call = ToolCall(kind=ToolKind.MCP, target='fake/echo {"text":"hi"}', payload="")
        ex, _text, _attach = await rt._execute(call)  # type: ignore[misc]
        check("exit_code=0", ex.exit_code == 0, ex.output[:120])
        check("返回内容", "echo: hi" in ex.output, ex.output[:200])

        # 无参数调用
        call = ToolCall(kind=ToolKind.MCP, target="fake/echo", payload="")
        ex, _text, _attach = await rt._execute(call)  # type: ignore[misc]
        check("无参数也可调用", ex.exit_code == 0, ex.output[:120])

        # ---------- 3. 错误 ----------
        print("\n[3] 错误")
        call = ToolCall(kind=ToolKind.MCP, target="fake/nonexistent", payload="")
        ex, _text, _attach = await rt._execute(call)  # type: ignore[misc]
        check("未知工具 exit_code=1", ex.exit_code == 1, str(ex.exit_code))
        check("提示用 list", "mcp:list" in ex.output, ex.output[:160])

        call = ToolCall(kind=ToolKind.MCP, target="fake/echo not-json", payload="")
        ex, _text, _attach = await rt._execute(call)  # type: ignore[misc]
        check("非法 JSON exit_code=1", ex.exit_code == 1, str(ex.exit_code))
        check("提示 JSON", "JSON" in ex.output, ex.output[:160])

        # 参数是数组（非对象）
        call = ToolCall(kind=ToolKind.MCP, target="fake/echo [1,2]", payload="")
        ex, _text, _attach = await rt._execute(call)  # type: ignore[misc]
        check("非对象参数报错", ex.exit_code == 1, str(ex.exit_code))

    finally:
        rt.mcp.stop_all()


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="gad-mcpexec-"))
    try:
        asyncio.run(run_checks(tmp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

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
