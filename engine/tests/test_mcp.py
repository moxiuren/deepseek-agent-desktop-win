"""MCP 客户端（`mcp.py`）自测。

用一个**假 MCP server**（Python 脚本）跑真实握手与调用 —— 这样测的是
真实的 stdio JSON-RPC 往返，不是 mock。协议细节错了会当场暴露。

用法：
    cd engine && ./.venv/Scripts/python.exe tests/test_mcp.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import textwrap
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gad_engine import mcp

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
    else:
        FAIL.append(name + (f" — {detail}" if detail else ""))


#: 一个最小可用的假 MCP server。实现 initialize / tools/list / tools/call。
FAKE_SERVER = '''
import json, sys

TOOLS = [
    {"name": "echo", "description": "回声", "inputSchema": {"type": "object"}},
    {"name": "add", "description": "加法", "inputSchema": {"type": "object"}},
]

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
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake", "version": "1.0"},
        }}
    elif method == "tools/list":
        out = {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    elif method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        if name == "echo":
            text = "echo: " + str(args.get("text", ""))
            out = {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": text}], "isError": False}}
        elif name == "add":
            total = int(args.get("a", 0)) + int(args.get("b", 0))
            out = {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": str(total)}], "isError": False}}
        else:
            out = {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": "未知工具"}], "isError": True}}
    else:
        # 通知（无 id）不回复
        if mid is None:
            continue
        out = {"jsonrpc": "2.0", "id": mid,
               "error": {"code": -32601, "message": "未知方法"}}
    if mid is not None:
        sys.stdout.write(json.dumps(out) + "\\n")
        sys.stdout.flush()
'''


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="gad-mcp-"))
    try:
        # 写假 server
        server_py = tmp / "fake_server.py"
        server_py.write_text(textwrap.dedent(FAKE_SERVER), encoding="utf-8")

        # ---------- 1. 连接 + 握手 ----------
        print("\n[1] 连接与握手")
        cfg = mcp.McpServerConfig(
            name="fake", command=sys.executable, args=[str(server_py)]
        )
        conn = mcp.McpConnection(cfg)
        conn.start()
        check("连接成功", conn.alive and not conn.error, conn.error)
        check("发现 2 个工具", len(conn.tools) == 2, str(len(conn.tools)))
        names = sorted(t.name for t in conn.tools)
        check("工具名正确", names == ["add", "echo"], str(names))
        check("全名带前缀", conn.tools[0].qualified.startswith("fake/"),
              conn.tools[0].qualified)

        # ---------- 2. 调用工具 ----------
        print("\n[2] 调用工具")
        out = conn.call_tool("echo", {"text": "hello"})
        check("echo 返回正确", out == "echo: hello", out)
        out2 = conn.call_tool("add", {"a": 3, "b": 4})
        check("add 返回正确", out2 == "7", out2)

        # ---------- 3. 错误处理 ----------
        print("\n[3] 错误处理")
        out3 = conn.call_tool("nonexistent", {})
        check("未知工具报错", "报错" in out3 or "未知" in out3, out3)

        # ---------- 4. Manager ----------
        print("\n[4] Manager")
        conn.stop()
        mgr = mcp.McpManager()
        mgr.load([cfg])
        check("manager 有 1 个连接", len(mgr.connections()) == 1)
        check("manager 聚合工具", len(mgr.all_tools()) == 2, str(len(mgr.all_tools())))
        found = mgr.find("fake/echo")
        check("find 命中", found is not None and found[1].name == "echo")
        check("find 未命中", mgr.find("fake/nope") is None)
        check("find 无斜杠", mgr.find("echo") is None)
        check("find 未知 server", mgr.find("other/echo") is None)

        snap = mgr.snapshot()
        check("snapshot 有 servers", len(snap["servers"]) == 1, str(snap))
        check("snapshot tool_count", snap["tool_count"] == 2, str(snap))

        # ---------- 5. 禁用 ----------
        print("\n[5] 禁用")
        mgr.load([mcp.McpServerConfig(name="off", command=sys.executable, enabled=False)])
        check("禁用不连接", len(mgr.connections()) == 0, str(len(mgr.connections())))

        mgr.stop_all()

        # ---------- 6. 启动失败 ----------
        print("\n[6] 启动失败")
        bad = mcp.McpConnection(
            mcp.McpServerConfig(name="bad", command="definitely-not-a-real-cmd-xyz")
        )
        bad.start()
        check("启动失败有 error", bool(bad.error), bad.error)
        check("启动失败 not alive", not bad.alive)

        # 空 command
        empty = mcp.McpConnection(mcp.McpServerConfig(name="e", command=""))
        empty.start()
        check("空 command 报错", "command" in empty.error.lower(), empty.error)

        # ---------- 7. flatten ----------
        print("\n[7] 结果拍平")
        check("纯文本", mcp._flatten_result(
            {"content": [{"type": "text", "text": "hi"}]}) == "hi")
        check("多块拼接", mcp._flatten_result(
            {"content": [{"type": "text", "text": "a"},
                         {"type": "text", "text": "b"}]}) == "a\nb")
        check("空内容", mcp._flatten_result({"content": []}) == "(无输出)")
        check("isError", "报错" in mcp._flatten_result(
            {"content": [{"type": "text", "text": "x"}], "isError": True}))

        # ---------- 8. 配置解析 ----------
        print("\n[8] 配置解析")
        c = mcp.McpServerConfig.from_dict("s1", {
            "command": "python", "args": ["-m", "x"], "env": {"K": "V"}})
        check("from_dict command", c.command == "python", c.command)
        check("from_dict args", c.args == ["-m", "x"], str(c.args))
        check("from_dict env", c.env == {"K": "V"}, str(c.env))
        check("from_dict 默认启用", c.enabled is True)

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
