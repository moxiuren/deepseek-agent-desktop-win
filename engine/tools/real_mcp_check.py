"""真实 MCP 端到端：连官方 server-memory（npx）。

这是活体验证 —— 不是假 server。跑通说明协议实现真的能对接官方 MCP server。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gad_engine import mcp


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "selfcheck":
        return 0

    cfg = mcp.McpServerConfig(
        name="memory",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-memory"],
    )
    print("连接 server-memory ...（首次 npx 会下载，可能慢）")
    conn = mcp.McpConnection(cfg)
    conn.start()

    if conn.error:
        print(f"[连接失败] {conn.error}")
        return 1

    print(f"[连接成功] 发现 {len(conn.tools)} 个工具：")
    for t in conn.tools:
        print(f"  - {t.name}: {t.description[:80]}")

    # 试着调用一个工具（server-memory 有 create_entities / read_graph 等）
    names = [t.name for t in conn.tools]
    if "read_graph" in names:
        out = conn.call_tool("read_graph", {})
        print(f"\n[read_graph 结果]\n{out[:300]}")
    elif names:
        print(f"\n[跳过调用] 未找到已知工具，可用：{names}")

    conn.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
