"""MCP 客户端 —— 连上 Model Context Protocol 服务器，把它们的工具接进来。

为什么值得做
------------
项目的引擎↔外壳已经用 stdio JSON-RPC（与 MCP 同构）。接上 MCP 协议本身，
就能一次性获得 filesystem / git / sqlite / fetch / puppeteer 等几十种成熟
工具 —— 不用自己一个个重写。这是杠杆最大的一项扩展。

协议要点（MCP 2024-11-05 规范，stdio 传输）
-------------------------------------------
1. 启动子进程，通过 stdin/stdout 交换**换行分隔的 JSON-RPC 2.0** 消息
2. `initialize` 握手 → `notifications/initialized` 通知
3. `tools/list` 发现工具
4. `tools/call` 调用工具

**为什么按 server 分别连**：每个 MCP server 是一个独立子进程，各有自己的
生命周期与失败域。一个 server 挂了不该拖垮其它 server，也不该拖垮引擎。

**为什么工具名要用 `server/tool` 前缀**：不同 server 可能有同名工具
（两个 server 都提供 `read_file` 是常态）。前缀消除歧义。

进程模型
--------
每个 server 一个常驻子进程 + 一个读线程 + 一把写锁。调用是同步阻塞的
（在引擎侧用 run_in_executor 包起来），因为这层不该假设调用方是异步的 ——
MCP server 自己可能是同步的。
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Any

#: 握手 / 单次调用的默认超时（秒）。
DEFAULT_TIMEOUT = 30.0
#: initialize 的超时（server 启动可能慢，给宽一点）。
INIT_TIMEOUT = 60.0
#: 我们支持的 MCP 协议版本。
PROTOCOL_VERSION = "2024-11-05"


class McpError(RuntimeError):
    """MCP 客户端异常。"""


@dataclass
class McpTool:
    """一个 MCP 工具的描述。"""

    server: str
    name: str
    description: str = ""
    input_schema: dict = field(default_factory=dict)

    @property
    def qualified(self) -> str:
        """带 server 前缀的全名 —— 调用时用它。"""
        return f"{self.server}/{self.name}"


@dataclass
class McpServerConfig:
    """一个 MCP server 的启动配置。

    与主流 MCP 客户端（Claude Desktop 等）的 `mcpServers` 段同构，便于
    用户直接抄现成配置。
    """

    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    enabled: bool = True

    @classmethod
    def from_dict(cls, name: str, data: dict) -> McpServerConfig:
        return cls(
            name=name,
            command=str(data.get("command", "")),
            args=[str(a) for a in (data.get("args") or [])],
            env={str(k): str(v) for k, v in (data.get("env") or {}).items()},
            enabled=bool(data.get("enabled", True)),
        )


class McpConnection:
    """与一个 MCP server 的连接。"""

    def __init__(self, cfg: McpServerConfig) -> None:
        self.cfg = cfg
        self.proc: subprocess.Popen | None = None
        self.tools: list[McpTool] = []
        self.error: str = ""
        self._id = 0
        self._write_lock = threading.Lock()
        self._pending: dict[int, dict] = {}
        self._pending_lock = threading.Lock()
        self._reader: threading.Thread | None = None
        self._alive = False

    # ---------- 生命周期 ----------

    def start(self) -> None:
        """启动子进程并完成握手。失败时 self.error 有值，不抛。"""
        if not self.cfg.command:
            self.error = "未配置 command"
            return
        env = dict(os.environ)
        env.update(self.cfg.env)
        try:
            self.proc = subprocess.Popen(
                [self.cfg.command, *self.cfg.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, ValueError) as exc:
            self.error = f"启动失败：{exc}"
            return

        self._alive = True
        self._reader = threading.Thread(
            target=self._read_loop, name=f"mcp-{self.cfg.name}", daemon=True
        )
        self._reader.start()

        try:
            self._handshake()
        except (McpError, OSError, ValueError) as exc:
            self.error = str(exc)
            self.stop()

    def _handshake(self) -> None:
        result = self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "gad-engine", "version": "1.0"},
            },
            timeout=INIT_TIMEOUT,
        )
        if not isinstance(result, dict):
            raise McpError("initialize 返回格式异常")
        # 握手后要发一个 initialized 通知（无 id，不需要回复）
        self._notify("notifications/initialized", {})
        self._refresh_tools()

    def _refresh_tools(self) -> None:
        result = self._request("tools/list", {}, timeout=INIT_TIMEOUT)
        items = (result or {}).get("tools") if isinstance(result, dict) else None
        self.tools = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            if not name:
                continue
            self.tools.append(
                McpTool(
                    server=self.cfg.name,
                    name=name,
                    description=str(item.get("description") or ""),
                    input_schema=item.get("inputSchema") or {},
                )
            )

    def stop(self) -> None:
        self._alive = False
        proc = self.proc
        self.proc = None
        if proc is None:
            return
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()

    @property
    def alive(self) -> bool:
        return self._alive and self.proc is not None and self.proc.poll() is None

    # ---------- 调用 ----------

    def call_tool(self, name: str, arguments: dict, timeout: float = DEFAULT_TIMEOUT) -> str:
        """调用一个工具，返回**扁平化的文本结果**。

        MCP 的返回是 `{content: [{type, text}, ...], isError: bool}`。
        本层把它拼成一段文本 —— 上层（引擎）只想要「工具说了什么」，
        不需要知道 MCP 的内容块结构。
        """
        result = self._request(
            "tools/call",
            {"name": name, "arguments": arguments or {}},
            timeout=timeout,
        )
        return _flatten_result(result)

    # ---------- JSON-RPC 底层 ----------

    def _send(self, payload: dict) -> None:
        if self.proc is None or self.proc.stdin is None:
            raise McpError("连接已关闭")
        line = json.dumps(payload, ensure_ascii=False)
        with self._write_lock:
            try:
                self.proc.stdin.write(line + "\n")
                self.proc.stdin.flush()
            except (OSError, ValueError) as exc:
                raise McpError(f"写入失败：{exc}") from exc

    def _notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(self, method: str, params: dict, timeout: float = DEFAULT_TIMEOUT) -> Any:
        self._id += 1
        msg_id = self._id
        event = threading.Event()
        with self._pending_lock:
            self._pending[msg_id] = {"event": event, "result": None, "error": None}

        self._send(
            {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params}
        )

        if not event.wait(timeout):
            with self._pending_lock:
                self._pending.pop(msg_id, None)
            raise McpError(f"{method} 超时（{timeout:.0f} 秒）")

        with self._pending_lock:
            entry = self._pending.pop(msg_id, None) or {}
        if entry.get("error"):
            raise McpError(str(entry["error"]))
        return entry.get("result")

    def _read_loop(self) -> None:
        proc = self.proc
        if proc is None or proc.stdout is None:
            return
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                msg_id = msg.get("id")
                if msg_id is None:
                    continue  # 通知，不处理
                with self._pending_lock:
                    entry = self._pending.get(msg_id)
                if entry is None:
                    continue
                if "error" in msg:
                    entry["error"] = (msg["error"] or {}).get("message", "未知错误")
                else:
                    entry["result"] = msg.get("result")
                entry["event"].set()
        except (OSError, ValueError):
            pass
        finally:
            self._alive = False


def _flatten_result(result: Any) -> str:
    """把 MCP 的工具返回拍平成文本。"""
    if not isinstance(result, dict):
        return str(result)

    parts: list[str] = []
    for block in result.get("content") or []:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            parts.append(str(block.get("text") or ""))
        elif btype == "resource":
            res = block.get("resource") or {}
            parts.append(f"[资源 {res.get('uri', '?')}]")
        else:
            parts.append(f"[{btype or '未知'} 内容]")

    text = "\n".join(p for p in parts if p).strip()
    if result.get("isError"):
        return f"[MCP 工具报错]\n{text}" if text else "[MCP 工具报错]"
    return text or "(无输出)"


class McpManager:
    """管理所有 MCP server 连接。"""

    def __init__(self) -> None:
        self._conns: dict[str, McpConnection] = {}

    def load(self, servers: list[McpServerConfig]) -> None:
        """连接一批 server。已在运行的先停掉（配置可能变了）。"""
        self.stop_all()
        for cfg in servers:
            if not cfg.enabled:
                continue
            conn = McpConnection(cfg)
            conn.start()
            self._conns[cfg.name] = conn

    def stop_all(self) -> None:
        for conn in self._conns.values():
            conn.stop()
        self._conns.clear()

    # ---------- 查询 ----------

    def connections(self) -> list[McpConnection]:
        return list(self._conns.values())

    def all_tools(self) -> list[McpTool]:
        out: list[McpTool] = []
        for conn in self._conns.values():
            out.extend(conn.tools)
        return out

    def find(self, qualified: str) -> tuple[McpConnection, McpTool] | None:
        """按 `server/tool` 找连接与工具。"""
        if "/" not in qualified:
            return None
        server, _, tool_name = qualified.partition("/")
        conn = self._conns.get(server)
        if conn is None:
            return None
        for tool in conn.tools:
            if tool.name == tool_name:
                return conn, tool
        return None

    def snapshot(self) -> dict:
        """状态快照，给界面/诊断用。"""
        servers = []
        for conn in self._conns.values():
            servers.append(
                {
                    "name": conn.cfg.name,
                    "alive": conn.alive,
                    "error": conn.error,
                    "tool_count": len(conn.tools),
                }
            )
        return {"servers": servers, "tool_count": len(self.all_tools())}
