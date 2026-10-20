"""编排层 —— 把四层闸门、协议解析、本地执行串成一个闭环。

一轮的形态：
    过闸门 -> 发送（流式）-> 增量解析 -> 命中工具调用 -> 立即派发本地执行
    -> 结果回传（同一会话）-> 下一轮

与原版（`deepseek-mac`，DOM 注入）的关键差别：
  * 不经过页面渲染，直接解析 SSE 文本流
  * 结果回传直接作为下一轮 prompt，不需要回填输入框
  * 上下文由服务端持有，不需要本地全量重放历史

与 gemini 版的两处结构性差别（**不是抄漏**）：
  * **无访客模式** —— `CircuitBreaker` 没有 `guest` 参数，`status()` 也不报 `guest_mode`
  * **无主动遥测** —— gemini 的 `poll_status()` 会喂三个遥测通道
    （`note_account_status` / `note_abuse` / `note_quota`）；DeepSeek **一个都没有**，
    所以这里只剩「探测成功/失败」两态，且默认连探测都不发（`health_probe_enabled=False`）
  * **新增 `note_outcome`** —— DeepSeek 的服务端错误走 **HTTP 200**，
    必须从 `outcome.error` 里读（见 `gates.py` 的说明）
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import platform
import re
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import (
    mcp,
    permissions,
    safety,
    sandbox,
    tools_fs,
    tools_git,
    tools_patch,
    tools_search,
    tools_tests,
    tools_todo,
    tools_web,
)
from .circuit import CircuitBreaker
from .compact import (
    SUMMARY_PROMPT,
    extract_summary_reply,
    looks_like_summary,
    summarize_local,
)
from .config import EngineConfig, app_data_dir
from .executor import LocalExecutor
from .gates import GateChain, GateDecision
from .jobs import JobManager
from .memory import MemoryStore
from .pacing import PacingGate
from .protocol import (
    ThinkFilter,
    ToolCall,
    ToolCallParser,
    ToolKind,
    build_contract_prompt,
    declares,
    format_ask_user,
    format_attachment_result,
    format_command_result,
    format_file_tool_result,
    format_write_result,
)
from .remote import merge_with_local, normalize_history
from .session import SessionManager, SessionMeta
from .store import Conversation, ConversationStore, Message
from .transport import (
    SESSION_LIST_MAX_COUNT,
    SESSION_LIST_MIN_COUNT,
    Transport,
    TransportError,
    create_transport,
)

EventCallback = Callable[[str, dict], None]

#: 可以并行执行的工具种类 —— **只读、无本地副作用**。
#:
#: 判据是「执行它不会改变任何东西」：不写文件、不跑命令、不改状态。
#: 有副作用的工具（写文件 / 编辑 / 命令 / MCP 调用 / job:cancel）**必须串行**
#: —— 它们的顺序有意义（先写后读 ≠ 先读后写），并发会打乱语义。
#:
#: web_fetch / web_search 虽发网络请求，但对本地无副作用，可以并发 ——
#: 它们的瓶颈是网络等待，并发收益最大。
#:
#: `job:list` / `job:status` 是只读的，但 JOB 这个 kind 也含 `job:cancel`
#: （有副作用），所以**整个 JOB 不放进并行集合** —— 宁可少并行，不可乱序。
_PARALLEL_SAFE = frozenset({
    ToolKind.READ_FILE,
    ToolKind.LIST_DIR,
    ToolKind.GREP,
    ToolKind.GLOB,
    ToolKind.TODO_READ,
    ToolKind.WEB_FETCH,
    ToolKind.WEB_SEARCH,
})


@dataclass
class ToolExecution:
    kind: str
    target: str
    exit_code: int
    output: str
    duration_ms: float = 0.0
    rejected: bool = False


@dataclass
class RunReport:
    goal: str
    turns: int = 0
    tool_calls: int = 0
    stop_reason: str = "completed"
    final_text: str = ""
    #: 这次任务里上游的全部思考原文（`THINK` 片段）。**只是留痕，不参与任何判断** ——
    #: 入库（`Message.thoughts`）靠它，界面「切回对话还能看到思考过程」也靠它。
    #: `chat.show_thinking` 关掉时恒为空串。
    thoughts: str = ""
    executions: list[ToolExecution] = field(default_factory=list)
    session: dict = field(default_factory=dict)
    gate_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "goal": self.goal,
            "turns": self.turns,
            "tool_calls": self.tool_calls,
            "stop_reason": self.stop_reason,
            "final_text": self.final_text,
            "thoughts": self.thoughts,
            "gate_reason": self.gate_reason,
            "session": self.session,
            "executions": [
                {
                    "kind": e.kind,
                    "target": e.target,
                    "exit_code": e.exit_code,
                    "duration_ms": round(e.duration_ms, 1),
                    "rejected": e.rejected,
                    "output_preview": e.output[:400],
                }
                for e in self.executions
            ],
        }


def _handoff_title(title: str) -> str:
    """接力出来的新对话的标题：在原标题后面标一个「（续）」。

    **不叠加** —— 接力多次时若每代都加一次，标题会变成
    「XXX（续）（续）（续）…」。已经带标记的直接沿用。
    """
    base = (title or "").strip() or "新对话"
    if base.endswith("（续）"):
        return base[:80]
    return base[:78] + "（续）"


def _screenshot_script(target: str) -> str:
    escaped = target.replace("'", "''")
    return (
        "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
        "$b=[System.Windows.Forms.Screen]::PrimaryScreen.Bounds;"
        "$bmp=New-Object System.Drawing.Bitmap $b.Width,$b.Height;"
        "$g=[System.Drawing.Graphics]::FromImage($bmp);"
        "$g.CopyFromScreen($b.Location,[System.Drawing.Point]::Empty,$b.Size);"
        f"$bmp.Save('{escaped}',[System.Drawing.Imaging.ImageFormat]::Png);"
        "$g.Dispose();$bmp.Dispose();Write-Output 'SCREENSHOT_SAVED'"
    )


class AgentRuntime:
    def __init__(
        self,
        cfg: EngineConfig,
        transport: Transport | None = None,
        executor: LocalExecutor | None = None,
        on_event: EventCallback | None = None,
        store: ConversationStore | None = None,
    ) -> None:
        self.cfg = cfg
        self._on_event = on_event or (lambda _k, _v: None)

        self.pacing = PacingGate(cfg.pacing)
        # **无 `guest` 参数** —— DeepSeek 没有访客模式（必须登录），
        # 所以不存在「未认证是正常态」这回事，401 一律按故障处理。
        self.breaker = CircuitBreaker(cfg.circuit)
        self.session = SessionManager(
            max_turns=cfg.pacing.max_turns_per_session,
            max_age_sec=cfg.pacing.max_session_age_sec,
            temporary=cfg.chat.temporary,
            # 默认关（`PacingConfig.auto_rotate`）—— 什么时候换会话由用户决定，
            # 换的时候用 `handoff_conversation()` 把上下文压成摘要带过去。
            auto_rotate=cfg.pacing.auto_rotate,
        )
        self.gates = GateChain(cfg, self.pacing, self.breaker, self.session)

        self.executor = executor or LocalExecutor(
            cfg.executor,
            on_stream=lambda line: self._emit("stream", {"text": line}),
            on_state=lambda k, v: self._emit(k, v),
        )
        self.transport = transport or create_transport(cfg)

        # ---------- 持久化：多对话 + 长期记忆 ----------
        # 当前对话由**本类唯一持有**。store 的 `rename()` 是「读-改-写」，
        # 界面若绕过本类直接调它，本类手里的对象就过期了，下一次落盘会把
        # 界面刚做的改动冲掉（这条在 `test_store.py` 里踩到过）。
        # 所以：所有涉及**当前对话**的写操作都必须经过这里。
        self.store = store or ConversationStore()
        self.conversation: Conversation = self.store.ensure_active()
        # 把上次的会话三元组灌回 SessionManager —— 这就是「重启后还能接着聊」
        # 与「切回旧对话上下文还在」的**唯一**实现点。少了这一行，两个功能
        # 都只是看起来有。
        self.session.restore(self.conversation.session)

        self.memory = MemoryStore(cfg.memory_path(), cfg.memory.max_chars)

        self._started = False
        self._start_error = ""
        #: 账号被服务端处置（封禁）的证据。**跨传输实例存活** ——
        #: 「刷新并验证」用的是临时 transport（`identity.validate_token_async`），
        #: 它的标记随实例销毁就没了；不在这里记一份，界面会退回「运行正常」。
        self._account_suspended = False
        self._suspended_detail = ""
        self._last_status_poll = 0.0
        self._async_tasks: set[asyncio.Task] = set()
        # 工具调用的全局序号。一次 run 里可能有多轮、每轮多个调用，
        # 界面要靠它把「开始 / 进度 / 结束」三条事件对上同一条卡片 ——
        # 用轮内下标会在第二轮重复，所以用单调递增的全局序号。
        self._tool_seq = 0
        self.jobs = JobManager()
        self.mcp = mcp.McpManager()
    # ---------- 事件 ----------

    def _emit(self, kind: str, payload: dict) -> None:
        with contextlib.suppress(Exception):
            self._on_event(kind, payload)

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        """启动。

        **传输层失败不抛异常** —— 否则 UI 会因为一个缺失配置直接打不开。
        失败原因记在 `_start_error`，由 UI 展示、由 run() 拦截。
        """
        if self._started:
            return

        violations = self.gates.identity_violations()
        if violations:
            self._emit("warning", {"layer": "L1", "violations": violations})
        for notice in self.cfg.identity.notices():
            self._emit("notice", {"text": notice})

        self._start_error = ""
        try:
            await self.transport.start()
        except TransportError as exc:
            self._start_error = str(exc)
            if getattr(exc, "suspended", False):
                self.mark_account_suspended(str(exc))
            self._emit("transport_unavailable", {"error": str(exc), "auth": exc.auth})

        self.executor.start()
        # MCP 连接放在最后：某个 server 挂掉不该影响传输层与执行器的可用性。
        # start_mcp() 内部吞异常，所以这里不会因为 MCP 问题起不来。
        self.start_mcp()
        self._started = True
        self._emit(
            "ready",
            {
                "transport": self.transport.name,
                "mode": self.cfg.identity.mode_label,
                "degraded": bool(self._start_error),
            },
        )

    async def stop(self) -> None:
        for task in list(self._async_tasks):
            task.cancel()
        self._async_tasks.clear()
        self.executor.stop()
        self.mcp.stop_all()
        await self.transport.stop()
        self._started = False
        self._emit("stopped", {})

    # ---------- 状态轮询（L4 的输入） ----------

    async def poll_status(self, force: bool = False) -> dict:
        """刷新远端状态（L4 熔断层的输入）。

        `force=True` —— 用户动作 / 身份或模型刚变，立刻走一遍。
        `force=False` —— 尊重 `circuit.status_poll_ms`，间隔没到就只回快照。

        **★ 这里与 gemini 版有本质差别：DeepSeek 没有可轮询的遥测。**

        gemini 的 `health()` 会带回账号状态码 / abuse / quota 三样，本方法把它们
        分别喂给熔断层的三个入口 —— 那是**事前刹车**的基础。

        DeepSeek 已知接口只有 3 个，**没有任何额度或账号状态接口**。所以：

          * `health_probe_enabled=False`（默认）时，`transport.health()` **不发任何请求**，
            只检查本地有没有 token。这个方法就退化成「读一次本地快照」——
            **零网络流量，不是机器特征**。
          * 打开探测时才会真打一次 `create_pow_challenge`（那是**额外**的机器特征，
            所以默认关）。

        ⇒ 本档位的熔断只能靠**响应信号**驱动（见 `circuit.py` 的说明），
        这个方法提供的只是「链路通不通」，不是「账号好不好」。
        """
        now = time.monotonic()
        interval = self.cfg.circuit.status_poll_ms / 1000.0
        if not force and now - self._last_status_poll < interval:
            return self.breaker.snapshot()
        self._last_status_poll = now

        if self._start_error:
            # 传输层没起来就别反复打远端
            return self.breaker.snapshot()

        try:
            status = await self.transport.health()
        except TransportError as exc:
            if getattr(exc, "suspended", False):
                self.mark_account_suspended(str(exc))
            if exc.auth:
                event = self.breaker.note_auth_failure(str(exc)[:80])
                self._emit(
                    "breaker",
                    {"level": event.level.value, "reason": event.reason, "fatal": event.fatal},
                )
            else:
                self.breaker.note_rpc_error(str(exc))
            self._emit("status_error", {"error": str(exc), "auth": exc.auth})
            return self.breaker.snapshot()

        if status.reachable:
            # 只清 RPC 计数 —— 探测**不生成**，没资格证明「服务端忙」已恢复。
            self.breaker.note_health_ok()

        snapshot = self.breaker.snapshot()
        snapshot["reachable"] = status.reachable
        if status.note:
            snapshot["remote_note"] = status.note
        self._emit("status", snapshot)
        return snapshot

    # ---------- 主循环 ----------

    async def run(
        self,
        goal: str,
        max_turns: int | None = None,
        files: list[str] | None = None,
    ) -> RunReport:
        """跑一轮。

        `files` 是**用户手动挂上的附件**（绝对路径），会随首轮消息一起发出。
        它与 agent 自己经 `attach` 工具产出的附件走同一个出口（`send(files=)`），
        区别只在来源：一个来自界面，一个来自工具。

        **为什么挂在首轮而不是每轮**：附件属于「用户这一次说的话」的一部分，
        第二轮起是模型自己在调工具，再重复上传同一批文件既浪费一次上传 PoW
        （5 秒量级），也没有语义上的必要。
        """
        max_turns = max_turns or self.cfg.max_tool_turns
        report = RunReport(goal=goal)

        # ★ 清停止标志 —— **一次任务一次清零**（2026-10-18，T-19）。
        #
        # 这是唯一的清零点。放在这里而不是 `transport.send()` 入口，是因为
        # 用户可能在**工具执行阶段**按停止（那时没有 `send()` 在跑）；
        # 若清零点在 `send()` 入口，标志会在下一次 `send()` 一开始被清掉，
        # 用户的停止请求被静默吞掉。
        #
        # 放在 `_record_user` 之前：用户已经说出口的话必须入库，
        # 即便这一轮立刻被停止。
        self.transport.clear_stop()

        if not self._started:
            await self.start()

        # 用户消息**先入库、再发送**：即使这一轮失败，「用户说了什么」也是已经
        # 发生的事实。界面上的气泡与磁盘上的历史必须一致 —— 否则重启之后
        # 那条对话会凭空少一句，用户会以为记录丢了。
        self._record_user(goal)

        if self._start_error:
            report.stop_reason = "transport_unavailable"
            report.gate_reason = self._start_error
            self._emit("stop", {"reason": "transport_unavailable", "detail": self._start_error})
            report.session = self.session.snapshot()
            self._emit("run_end", report.to_dict())
            return report

        prompt = self._bootstrap_prompt(goal)
        # 首轮就带上用户手动挂的附件。工具产出的附件（`attach`）在后面几轮
        # 会追加到同一个列表里 —— 两者走同一个出口，不分叉。
        attachments: list[str] = [str(f) for f in (files or [])]

        for turn in range(1, max_turns + 1):
            t_gate_start = time.monotonic()
            decision = await self._await_gate()
            gate_wait_ms = (time.monotonic() - t_gate_start) * 1000.0
            if not decision.allowed:
                report.stop_reason = "halted" if decision.fatal else "gated"
                report.gate_reason = decision.reason
                self._emit("stop", {"reason": report.stop_reason, "detail": decision.reason})
                break

            rotated = self.gates.rotate_if_needed()
            if rotated is not None:
                self._emit("session_rotate", {"reason": rotated.reason})

            self._emit("turn_start", {"turn": turn, "max_turns": max_turns})
            t_turn_start = time.monotonic()
            parser = ToolCallParser()
            think = ThinkFilter()
            text_parts: list[str] = []
            calls: list[ToolCall] = []
            # 与 calls 平行的全局序号，供下游 _dispatch 对上界面上的那张卡片
            call_seq: list[int] = []

            def on_delta(delta: str) -> None:
                visible = think.feed(delta)
                if not visible:
                    return
                text_parts.append(visible)
                self._emit("delta", {"text": visible})
                for call in parser.feed(visible):
                    calls.append(call)
                    # 工具调用从「被解析出来」那一刻起就是一条**活的**记录：
                    # 这里先发 tool_start，执行期间有 tool_progress 心跳，
                    # 结束再发 tool_done。原来是只发一条 tool_call ——
                    # 卡片在调用解析出来的瞬间就画完，之后本地命令可能跑几十秒、
                    # 下一轮上游又要十几秒，界面上就是**两块死寂**。
                    self._tool_seq += 1
                    call_seq.append(self._tool_seq)
                    self._emit(
                        "tool_start",
                        {
                            "seq": self._tool_seq,
                            "turn": turn,
                            "kind": call.kind.value,
                            "target": call.target,
                            "payload": call.payload,
                        },
                    )

            # 思考通道。两条路，由 `chat.show_thinking` 选：
            #
            #   · **开（默认）**：把思考**原文**增量推给界面（`thought` 事件），
            #     界面渲染成一个可折叠的「思考过程」块。2026-10-15 之前这里
            #     只推一条「思考中…（已 N 字）」心跳，思考原文被整个丢掉 ——
            #     而传输层其实一直把它装在 `outcome.thoughts` 里带回来了。
            #     用户报的「思考的文字没有输出出来」就是这个缺口。
            #   · **关**：回到老行为，只推节流后的 `thinking` 心跳。
            #
            # 两条路都**必须**有「还没开口」的那一态：思考模式下上游可能长时间
            # 一个字节都不流（2026-10-14 实测 >120 秒，连思考增量都没有，最后
            # `[Recovery] Polling ... timed out after 120s`）。那段空窗里界面要是
            # 什么都没有，用户分不清「在跑」还是「坏了」。
            #
            # 为什么是「增量」而不是「每次推全文」：思考常有几千字，按 0.5 秒
            # 推一次全文是 O(n²) 的流量，跑到后面每帧几十 KB。增量只推新增部分，
            # 界面自己拼 —— 和正文通道（`delta`）完全同一套路子。
            show_thinking = bool(self.cfg.chat.show_thinking)
            thought_chars = 0
            thought_parts: list[str] = []    # 本轮思考全文（入库 + turn_end 定稿）
            thought_tail: list[str] = []     # 「关」那条路的心跳尾巴（只留最近 8 段）
            thought_pending: list[str] = []  # 「开」那条路攒着待推的增量
            thought_last_emit = 0.0

            def _flush_thought(waited: int = 0) -> None:
                """把攒下的思考增量推一次。空文本也推 —— 那是「还在等上游」的心跳。"""
                if show_thinking:
                    self._emit(
                        "thought",
                        {
                            "turn": turn,
                            "chars": thought_chars,
                            "text": "".join(thought_pending),
                            "waited": waited,
                        },
                    )
                    thought_pending.clear()
                    return
                self._emit(
                    "thinking",
                    {
                        "chars": thought_chars,
                        "tail": "".join(thought_tail)[-120:],
                        "waited": waited,
                    },
                )

            def on_thought(delta: str) -> None:
                nonlocal thought_chars, thought_last_emit
                thought_chars += len(delta)
                thought_parts.append(delta)
                if show_thinking:
                    thought_pending.append(delta)
                else:
                    thought_tail.append(delta)
                    # 只留尾巴，避免长思考把内存撑起来。
                    if len(thought_tail) > 8:
                        del thought_tail[:-8]
                now = time.monotonic()
                # 「开」要流得细（0.5 秒一帧），否则思考过程看起来一跳一跳的；
                # 「关」只有一行状态字，2 秒足够 —— 那是它原本的节流值。
                if now - thought_last_emit < (0.5 if show_thinking else 2.0):
                    return
                thought_last_emit = now
                _flush_thought()

            # 请求一发出就先给界面一个反馈 —— **不要等上游开口**。
            # 2026-10-14 实测：思考模式下上游可能长时间一个字节都不流
            # （>120 秒，连思考增量都没有，最后 `[Recovery] Polling ... timed out
            # after 120s`）。而 `on_thought` 要等上游先给思考增量才会被调用 ——
            # 那段空窗里界面就是彻底的死寂，用户分不清「在跑」还是「坏了」。
            # 所以这里再挂一条**独立于上游**的心跳，每 3 秒报一次「已经等了多久」。
            _flush_thought(waited=0)
            wait_start = time.monotonic()

            async def _wait_heartbeat() -> None:
                while True:
                    await asyncio.sleep(3.0)
                    # 正文一旦开始流出，「还在等上游」这条心跳就失去意义。
                    # 不挡住的话，它会在正文之后**再推一条**事件，
                    # 外壳据此新建一条「思考中…」并一直残留 ——
                    # 用户读到的就是「回复说到一半没了」（2026-10-14 实测）。
                    if text_parts:
                        continue
                    _flush_thought(waited=int(time.monotonic() - wait_start))

            heartbeat = asyncio.create_task(_wait_heartbeat())
            # ⚠️ 这个时刻就是「提示词真正发出去」的时刻，**必须现在就取**。
            #
            # 它要喂给节奏闸门记账（下面的 `gates.commit(now=...)`）。原先记账
            # 取的是「这一轮跑完」的时刻，等于把回答耗时（常常 60 秒以上）
            # 从两次请求的间隔里**抹掉**了 —— 于是每一轮都在一段本来就很长的
            # 自然间隔之上，再叠加一次目标间隔的等待。对上游而言，它看到的
            # 请求间隔明明很长，我们却在本地白白多等。
            t_dispatch = time.monotonic()
            # 把「本地链路到底花了多久」明确报出去。
            #
            # 为什么值得单独一个事件：实测（tools/probe_presend_latency.py）
            # 本地前置是 0–1.3 秒、上游首字节是 14–47 秒，但**界面在这两段里
            # 长得一模一样** —— 都是「没动静」。用户只能猜，而最自然的猜法
            # 就是「还没发出去」，于是报成「从软件到发提示词出去间隔太久」。
            # 有了这个数字，用户可以直接跟「上游开始吐字还要多久」对比，
            # 自己就能分清是哪一段慢，不用再猜。
            self._emit(
                "sent",
                {
                    "turn": turn,
                    "gate_wait_ms": round(gate_wait_ms),
                    "pre_send_ms": round((t_dispatch - t_turn_start) * 1000.0),
                    "local_total_ms": round((t_dispatch - t_gate_start) * 1000.0),
                },
            )
            try:
                outcome = await self.transport.send(
                    prompt,
                    self.session.meta,
                    temporary=self.session.meta.temporary,
                    # 思考模式：和模型一样是 per-turn 参数，每轮从配置取，
                    # 所以界面切换后下一轮就生效，不需要重启引擎。
                    extended_thinking=self.cfg.chat.extended_thinking,
                    # 联网搜索：同样是 per-turn（网页版那个「搜索」按钮对应
                    # 请求体的 `search_enabled`，开了才允许联网）。**必须每轮现取** ——
                    # 传输层构造时那一次读取只是默认值，界面切换后靠这里生效。
                    search_enabled=self.cfg.chat.search_enabled,
                    files=attachments or None,
                    on_delta=on_delta,
                    on_thought=on_thought,
                )
            except TransportError as exc:
                # 路径 ①：传输层**抛出**。分类交给 gates 的统一入口 ——
                # `config=True` 会被它直接忽略（用户改一下就能好的事，
                # 计进 RPC 错误会让重试三次后无端降级传输层，
                # 把真正的原因埋在「RPC 连续失败」后面）。
                #
                # **处置标记必须在这里落库**：`send()` 把 TransportError 直接
                # 抛给调用方（不塞进 `TurnOutcome.error`），所以「路径 ②」那个
                # `if not outcome.ok` 分支**永远等不到**处置错误。漏了这一句，
                # 界面上就只剩一句一次性的 gate_reason，刷新一次状态就没了
                # —— 2026-10-17 实测：`gate_reason` 正确，`status.identity`
                # 却是 `account_suspended: false`。
                if getattr(exc, "suspended", False):
                    self.mark_account_suspended(str(exc))
                event = self.gates.note_transport_error(exc)
                if event:
                    self._emit(
                        "breaker",
                        {"level": event.level.value, "reason": event.reason, "fatal": event.fatal},
                    )
                report.stop_reason = "transport_error"
                report.gate_reason = str(exc)
                self._emit("error", {"error": str(exc), "auth": exc.auth})
                break
            finally:
                # 心跳只服务于「这一轮的等待」，轮次一结束就必须停，
                # 否则它会一直往界面推 thinking 事件。
                heartbeat.cancel()

            # 路径 ②：HTTP 200 但服务端在流里回了错误。
            #
            # **这条路最容易被漏掉** —— 状态码是 200，只有读 `outcome.error`
            # 才知道出事。DeepSeek 实测的 `Server busy` + `generation_timeout`
            # 走的正是这条（68 秒挂起后回 200）。漏掉它意味着：
            # 服务端已经在明示「我不想理你」，而我们一无所知地继续按原节奏打。
            if not outcome.ok:
                event = self.gates.note_outcome(outcome)
                if event:
                    self._emit(
                        "breaker",
                        {"level": event.level.value, "reason": event.reason, "fatal": event.fatal},
                    )
                report.stop_reason = "outcome_error"
                report.gate_reason = outcome.error
                report.turns = turn
                self._emit("error", {"error": outcome.error, "auth": False})
                break

            # 一次真实发送成功 = 账号确实可用。**只有这里能清处置标记** ——
            # 启动自检走的是 `create_pow_challenge`，它不校验账号处置，
            # 用它来清会让「账号被处置」在下次启动时被抹掉（2026-10-17 实测：
            # 封禁期该接口照样返回 200）。清掉后胶囊自动回绿，不需要额外事件。
            if self._account_suspended:
                self.clear_account_suspension()

            # PoW（工作量证明）的本地耗时 —— **必须单独上报，不能并进 `sent`**。
            #
            # 为什么：`sent`（上面 :486 处）在 `transport.send()` **之前**就发出去了，
            # 而取 challenge 与解 PoW 都发生在 `send()` **内部**。所以 `sent` 报的
            # `gate_wait_ms` / `pre_send_ms` / `local_total_ms` **一个都不含它** ——
            # 它在既有指标里是一段彻底的黑洞。
            #
            # 而这段是**纯本地空耗**：2026-10-16 实测单次数秒，且与「答案藏在
            # 区间哪个位置」强相关。它直接叠在「按下回车 → 请求真的发出」之间，
            # 用户感受到的就是「点了没反应」。没有这个数，任何关于 PoW 的
            # 优化都只能靠猜。
            #
            # 用 `pow_mode` 作守卫：其它档位（T1/T4）没有 PoW，字段留空、不报。
            if outcome.pow_mode:
                self._emit(
                    "pow_stats",
                    {
                        "turn": turn,
                        "challenge_ms": outcome.challenge_ms,
                        "pow_ms": outcome.pow_ms,
                        "mode": outcome.pow_mode,
                        "workers": outcome.pow_workers,
                        "trials": outcome.pow_trials,
                        "pool_startup_ms": outcome.pow_pool_startup_ms,
                        "pool_created": outcome.pow_pool_created,
                        "cached": outcome.pow_cached,
                        "solve_ms": outcome.pow_solve_ms,
                    },
                )

            # 附件的上传耗时 —— 与 PoW 分开报。
            #
            # **为什么不并进 `pow_stats`**：上传每个文件各要一次独立 PoW
            # （`target_path` 不同，不复用发送那份缓存），所以带附件的轮次
            # `upload_ms` 会是数秒量级。把它并进 `pow_ms` 会让「PoW 是不是
            # 变慢了」这个判断彻底失效 —— 两者都是「按下回车后本地空耗」，
            # 但成因和优化手段完全不同。
            if outcome.uploaded_files:
                self._emit(
                    "file_upload",
                    {
                        "turn": turn,
                        "count": len(outcome.uploaded_files),
                        "upload_ms": outcome.upload_ms,
                        "files": [
                            {
                                "file_name": f.get("file_name", ""),
                                "id": f.get("id", ""),
                                "status": f.get("status"),
                                "file_size": f.get("file_size"),
                            }
                            for f in outcome.uploaded_files
                        ],
                    },
                )

            attachments = []
            # 记账用**发出时刻**（t_dispatch），不是此刻 —— 理由见上面取 t_dispatch 处。
            self.gates.commit(now=t_dispatch)
            report.turns = turn

            if outcome.cid:
                self.session.update_from_response(outcome.cid, outcome.rid, outcome.rcid)

            # 本轮正文。流式通道可能**整个为空** —— 服务端把整段正文只放在快照帧里，
            # 一个增量帧都不发（2026-10-15 14:15 实测，短回复尤其容易命中）。
            # 那时 `text_parts` 是空的，必须以 transport 组装好的正文兜底，
            # 否则用户看到的是「消息发出去了，但一个字都没回来」。
            turn_text = "".join(text_parts)
            if not turn_text and outcome.text:
                turn_text = outcome.text

            report.tool_calls += len(calls)
            # 本轮思考原文。**以 transport 组装好的整段为准**，不以 `thought` 增量
            # 事件累计为准，两个理由：
            #   ① 增量是按 0.5 秒节流推的，最后那一小段（不足一个节流窗口）
            #      永远推不出去；
            #   ② 有些帧根本不走增量通道 —— 服务端把整段思考只放在快照帧里、
            #      或走 `SET` 整体替换时，`on_thought` 一次都不会被调用
            #      （见 `_SseParser._snapshot` / `_delta`），而 `outcome.thoughts`
            #      两路都覆盖。
            # 界面拿这个字段定稿，就不会出现「思考过程少了个尾巴」。
            # 关掉 `show_thinking` 时保持空串：用户既然不要看，就别入库占地方。
            turn_thoughts = ""
            if show_thinking:
                turn_thoughts = outcome.thoughts or "".join(thought_parts)
            if turn_thoughts:
                report.thoughts = (
                    f"{report.thoughts}\n\n{turn_thoughts}" if report.thoughts else turn_thoughts
                )
            self._emit(
                "turn_end",
                {
                    "turn": turn,
                    "text": turn_text,
                    "thoughts": turn_thoughts,
                    "tool_calls": len(calls),
                    # 联网搜索痕迹（开了搜索、且服务端真的搜了才有值）。
                    # 它是**状态**不是正文 —— 正文里混进 `Found 12 web pages` 是踩过的坑。
                    "search_pages": outcome.search_pages,
                    "search_sources": outcome.search_sources,
                    # 这一轮服务端分配的**助手消息 id**（`response_message_id`）。
                    #
                    # 朗读（TTS）的票据是**按单条消息**签的，不是按会话 ——
                    # 界面必须知道「这条气泡对应服务端的哪条消息」才能朗读它。
                    # 在此之前这个 id 只落在 `SessionMeta.rid` 里、只在本地存储中
                    # 可见，界面拿不到，于是朗读按钮根本无从发起。
                    #
                    # **为什么放在 `turn_end` 而不是 `run_end`**：一次任务可能
                    # 有多轮（工具调用会断开轮次），每轮各有自己的 rid；放在
                    # run_end 就只能拿到最后一轮的，前面的气泡全都对不上号。
                    "rid": outcome.rid,
                },
            )

            # 用户主动停止（2026-10-18，T-19）。
            #
            # **必须放在 `turn_end` 之后** —— 上面已经把本轮已生成的正文
            # emit 出去了，用户点「停止」的预期是「别再往下写了」，
            # 不是「把已写的也抹掉」。放在 `if not calls` 之前是因为
            # 停止的那一轮即便解析出了工具调用也**不能再执行** ——
            # 用户按的是停止，不是「跳过回答只跑工具」。
            #
            # ⚠️ **绝不能计入熔断**。`note_outcome` 一次都不调：停止是用户
            # 自己按的按钮，不是上游故障。计入的话连点几次停止就足以把
            # 传输层降级掉，而真正的原因（人手按的）反被埋在「RPC 连续失败」后面。
            # 同理 `gates.commit()` 已经在上文执行过 —— 这一轮确实向上游发了
            # 真实请求，节奏闸门必须记这笔账，否则下一次会以为间隔很长而更激进。
            if outcome.cancelled:
                report.final_text = turn_text
                report.stop_reason = "cancelled"
                report.turns = turn
                # 只有**真的收到过正文**才算上游健康。用户可能在「一个字都还没吐
                # 出来」时就按停止（那时上游可能正卡着）—— 那种情况不能当成
                # 一次成功，否则连点几次停止就能把熔断计数刷归零，
                # 把「上游确实在挂」这个事实抹掉。
                if turn_text:
                    self.gates.after_turn()
                self._emit(
                    "stop",
                    {
                        "reason": "cancelled",
                        "detail": "已按用户请求停止生成"
                        + (f"（保留已生成的 {len(turn_text)} 字）" if turn_text else ""),
                    },
                )
                break

            if not calls:
                report.final_text = turn_text
                report.stop_reason = "completed"
                self.gates.after_turn()
                break

            results, attachments = await self._dispatch(calls, report, call_seq, turn)
            prompt = "\n\n".join(results)
            # 附件只需发一次：`ref_file_ids` 一旦挂上，服务端就把它记在这条会话
            # 的上下文里，后续轮次不带也照样能引用。
            #
            # **代价必须说清楚**：每多发一轮就是一次独立的上传 PoW（5 秒量级），
            # 而且是纯本地空耗。`attachments` 以前会在每轮重传一遍 —— 带附件
            # 的多轮工具调用会白等好几次。这里发完就清空，由下一轮的 `attach`
            # 工具重新填充。
            attachments = []
        else:
            report.stop_reason = "max_turns"
            self._emit("stop", {"reason": "max_turns", "detail": f"已达 {max_turns} 轮上限"})

        # 服务端三元组每轮结束都同步回对话对象 —— 它是「切回来还能接上」与
        # 「重启恢复」的数据源。**不能只在有正文时才更新**：工具轮、空回复轮
        # 同样会让 cid/rid 前进，漏同步就会让切回来的对话接在一棵旧树上。
        self.conversation.session = self.session.meta
        if not self._record_assistant(report):
            self.store.save(self.conversation)
        report.session = self.session.snapshot()
        self._emit("run_end", report.to_dict())
        return report

    # ---------- 对话与记忆（持久化） ----------

    def _record_user(self, goal: str) -> None:
        """把用户这一轮说的话写进当前对话。"""
        text = (goal or "").strip()
        if not text:
            return
        self.store.append(self.conversation, Message(role="user", text=text))

    def _record_assistant(self, report: RunReport) -> bool:
        """把助手这一轮的最终正文写进当前对话。返回是否真的记了一条。

        **没有正文就不记** —— 空回复记进历史，重放时会多出一条空气泡，
        而真正的失败原因（`stop_reason` / `gate_reason`）反而看不见。
        """
        text = (report.final_text or "").strip()
        if not text:
            return False
        meta = self.session.meta
        self.store.append(
            self.conversation,
            Message(
                role="assistant",
                text=text,
                cid=meta.cid,
                rid=meta.rid,
                tool_calls=report.tool_calls,
                turns=report.turns,
                stop_reason=report.stop_reason,
                # 思考原文跟着这一条回答一起入库：切回这条对话、或重启软件后，
                # 界面还能把「思考过程」块重放出来（见 `ReplayConversation`）。
                thoughts=report.thoughts,
            ),
        )
        return True

    def _settings_snapshot(self) -> dict:
        """新对话的形态快照 —— 让它继承当前档位，而不是悄悄退回默认。"""
        return {
            "extended_thinking": self.cfg.chat.extended_thinking,
            "search_enabled": self.cfg.chat.search_enabled,
            "model": self.cfg.identity.model,
        }

    def list_conversations(self) -> list[dict]:
        """对话列表（含哪条是当前）。"""
        items = self.store.list_summaries()
        active = self.conversation.id if self.conversation else ""
        for item in items:
            item["active"] = item.get("id") == active
        return items

    def current_conversation(self) -> dict:
        """当前对话全文（含消息）。界面切回时靠它重放。"""
        conv = self.conversation
        return {
            "id": conv.id,
            "title": conv.title,
            "created_at": conv.created_at,
            "updated_at": conv.updated_at,
            "settings": dict(conv.settings),
            "session": conv.session.to_dict(),
            "messages": [asdict(m) for m in conv.messages],
        }

    def new_conversation(self) -> dict:
        """新建对话。

        **必须先轮换服务端会话**：新对话就该是一张白纸。若沿用上一条
        `chat_session_id`，两个不相干的话题会被接在同一棵会话树上 ——
        模型会把上文当上下文，用户却以为开了个新话题。
        """
        self.session.rotate("新建对话")
        conv = self.store.create(settings=self._settings_snapshot())
        conv.session = self.session.meta
        self.store.save(conv)
        self.conversation = conv
        return self.current_conversation()

    def load_conversation(self, cid: str) -> dict | None:
        """切到某条对话。恢复它的三元组，使服务端上下文能接回去。

        **只做本地切换** —— 服务端历史读取是 `sync_conversation()` 的事。
        分开的理由：本地切换是零延迟的，而拉服务端要一次网络往返；
        界面不该为了「顺便刷新」把切对话卡住。
        """
        conv = self.store.get(cid)
        if conv is None:
            return None
        self.conversation = conv
        self.session.restore(conv.session)
        self.store.set_active(cid)
        return self.current_conversation()

    async def sync_conversation(self, cid: str) -> dict:
        """从服务端拉该对话的历史，与本地合并，并**校正续接锚点**。

        为什么锚点必须校正：本地存的 `rid` 可能已经过期 —— 某一轮发送失败、
        或者用户刚在**网页版**里继续聊过。锚点一错，`parent_message_id` 就指向
        一条不存在的消息，服务端会把它当成新 root，上下文**静默断掉**：
        界面看起来一切正常，模型却什么都不记得了。

        返回诊断字典（界面用它显示「同步到了什么」）。
        """
        conv = self.store.get(cid)
        if conv is None:
            return {"synced": False, "reason": "对话不存在"}
        remote_cid = conv.session.cid
        if not remote_cid:
            return {"synced": False, "reason": "该对话还没有服务端会话（没发过消息）"}

        biz = await self.transport.fetch_history(remote_cid)
        remote = normalize_history(biz)
        local_before = len(conv.messages)
        conv.messages = merge_with_local(remote.messages, conv.messages)
        if remote.anchor:
            conv.session.rid = remote.anchor
        conv.synced_at = time.time()
        # 注意：**不动 `updated_at`** —— 那是「最后活动时间」，同步不该改它，
        # 否则列表排序会在每次刷新后跳来跳去。
        self.store.save(conv)

        # 同步的是当前对话 → 必须把新锚点灌回 SessionManager，
        # 否则下一轮发送用的还是旧 rid（这一步漏了，整个功能就白做）。
        is_current = bool(self.conversation and cid == self.conversation.id)
        if is_current:
            self.session.restore(conv.session)

        return {
            "synced": True,
            "conversation": self.current_conversation() if is_current else None,
            "remote_count": remote.raw_count,
            "local_before": local_before,
            "local_after": len(conv.messages),
            "anchor": remote.anchor,
            "system_prompt_chars": len(remote.system_prompt),
            "skipped_frags": dict(remote.skipped_frag_types),
        }

    # ---------- 接力换会话（上下文压缩）----------
    #
    # 用户主动触发的「换一条新会话继续」。与 `session.py` 的自动轮换是两件事：
    #
    # | | 自动轮换 | 接力（下面这两个方法） |
    # |---|---|---|
    # | 谁决定 | 计时器 / 轮数 | 用户按按钮 |
    # | 上下文 | 丢掉 —— 静默失忆 | 压成摘要带进新会话 |
    # | 可见性 | 界面毫无变化 | 明确提示「已接力」 |
    #
    # 自动轮换默认已关（`PacingConfig.auto_rotate`），所以这条路径现在是
    # **唯一**的换会话方式 —— 也就必须把上下文接上，否则换过去就是白纸。

    async def handoff_conversation(self, mode: str | None = None) -> dict:
        """把当前对话的上下文压缩成摘要，开一条新会话继续。

        流程：
          1. 生成摘要 —— `model` 模式在当前会话里问一次（服务端已持有完整
             上下文，只发一句指令）；失败或未启用时降级到 `local` 纯规则抽取
          2. 轮换服务端会话（`rotate()`），此后消息进一条全新的会话树
          3. 新建本地对话、写入摘要、切过去
          4. 旧对话**原样留在列表里** —— 压缩不可逆，原文必须可查

        摘要会被注入到新会话**每一轮**的系统提示词里（见 `_summary_block`），
        所以新会话一开口就记得之前发生过什么。

        返回诊断字典；失败带 `reason`，**不抛**。
        """
        conv = self.conversation
        if conv is None:
            return {"ok": False, "reason": "没有当前对话"}

        messages = list(conv.messages)
        if not any(
            m.role in ("user", "assistant") and (m.text or "").strip()
            for m in messages
        ):
            return {"ok": False, "reason": "这条对话还没有内容，不需要接力"}

        if not self.cfg.compact.enabled:
            return {
                "ok": False,
                "reason": "上下文压缩已关闭（配置 compact.enabled = false）",
            }

        chosen = (mode or self.cfg.compact.mode or "model").strip().lower()
        if chosen not in ("model", "local"):
            chosen = "model"

        summary = ""
        used = ""
        note = ""

        # ---- 1) 摘要 ----
        if chosen == "model":
            summary = await self._model_summary(conv)
            if summary:
                used = "model"
            else:
                note = "模型摘要没拿到（可能被闸门拦住或上游没回），已改用本地规则摘要"

        if not summary:
            # 本地摘要必须能独立工作 —— 它同时是 model 模式的兜底。
            summary = summarize_local(
                messages,
                max_chars=self.cfg.compact.max_chars,
                prior_summary=conv.handoff_summary,
            )
            if summary:
                used = used or "local"

        if not summary:
            return {"ok": False, "reason": "摘要生成失败（对话内容过少？）"}

        # ---- 2) 轮换 + 新对话 ----
        #
        # 顺序不能反：先 `rotate()` 拿到全新的空 SessionMeta，再把它交给新对话。
        # 反过来（先建对话再轮换）会让新对话挂着一个旧会话的三元组 ——
        # 于是「新会话」的第一条消息会接进旧会话树，等于没换。
        self.session.rotate("接力新会话")
        new_conv = self.store.create(
            title=_handoff_title(conv.title),
            settings=self._settings_snapshot(),
        )
        new_conv.session = self.session.meta
        new_conv.handoff_summary = summary
        new_conv.handoff_from = conv.id
        new_conv.handoff_at = time.time()
        self.store.save(new_conv)
        self.conversation = new_conv

        return {
            "ok": True,
            "mode": used,
            "note": note,
            "chars": len(summary),
            "summary": summary,
            "from": conv.id,
            "from_title": conv.title,
            "conversation": self.current_conversation(),
        }

    async def _model_summary(self, conv: Conversation) -> str:
        """在当前会话里问一次「请把以上对话压成摘要」。拿不到就返回空串。

        **发进当前会话，而不是新建一条**，有两个理由：
          * 服务端已经持有完整上下文 —— 我们只需发一句指令，不必把整段历史
            重新拼进去。token 成本极低，也不需要本地有全量记录。
          * 这条请求落在**即将弃用的旧会话**里。若新建一条会话发，账号里会
            平白多出一条只含「请总结」的垃圾会话。

        第二条理由原本还附了一句「而删除接口尚未探测」—— 删除接口已于
        2026-10-18 打通（`deepseek_rpc.py:1618` `delete_session`，
        `SESSION_DELETE_PATH` 处有净零实验记录），所以「垃圾会话删不掉」
        不再是约束。但两条理由本身仍然成立（token 成本 + 不新增可观测痕迹），
        设计没有变。

        **不抛异常**：摘要拿不到不是致命错误（有本地摘要兜底），但用户按了
        按钮却什么都没发生才是。
        """

        meta = self.session.meta
        # 没有服务端会话（还没发过消息，或上一次发送失败了）→ 这一轮会**新建**
        # 一条会话，把「请总结」当成首条消息写进去，留下一堆垃圾。直接放弃。
        if not meta.cid:
            return ""

        # **预算必须短。** 默认的 900 秒是给 `run()` 用的 —— 那里用户本来就在
        # 等一个回答，等闸门是合理的。而这里是用户**刚点完按钮、正盯着界面**：
        # 熔断层一次「服务端忙」退避就是 30 分钟，`_await_gate` 会老老实实
        # 等到预算耗尽，界面上就是「正在压缩…」卡十几分钟（实测踩到）。
        #
        # 20 秒足够覆盖正常的节奏间隔（base 2.5s + 阅读停顿最多 9s）；
        # 等不到就降级本地摘要 —— 「摘要质量差一点」远好过「点了没反应」。
        decision = await self._await_gate(budget_s=20.0)
        if not decision.allowed:
            return ""

        t_dispatch = time.monotonic()
        try:
            outcome = await self.transport.send(
                SUMMARY_PROMPT,
                meta,
                temporary=meta.temporary,
                # 摘要不需要思考模式：那是给难题推理用的，而这里只是复述已经
                # 发生过的事。关掉能省几十秒。
                extended_thinking=False,
                # 也不联网：上下文全在会话里，搜网页只会引入噪声。
                search_enabled=False,
            )
        except TransportError:
            return ""
        except Exception:  # noqa: BLE001 - 摘要失败绝不能打断主流程
            return ""

        if not outcome.ok:
            return ""

        # 记账（与 `run()` 同一套路子，用**发出时刻**）—— 这一轮也是一次
        # 真实请求，不记的话节奏闸门会以为间隔很长，下一轮反而更激进。
        self.gates.commit(now=t_dispatch)
        self.gates.after_turn()

        # 这一轮让服务端会话前进了一步（`rid` 变了）—— 旧对话的记录要跟上，
        # 否则用户切回旧对话时锚点会落后一轮。旧对话随即弃用，但记录得准。
        self.store.save(conv)

        text = extract_summary_reply(outcome.text)
        return text if looks_like_summary(text) else ""

    # ---------- 服务端会话（网页版历史）----------
    #
    # 这一组解决的是「本地只看得见自己建的对话」这个缺口：账号下可能还有一堆
    # 用户在**网页版**里聊出来的会话，本地一条都不知道。于是：
    #   * `list_remote_sessions` —— 把它们列出来，并标出哪些已被本地接管
    #   * `attach_session`       —— 把其中一条接管成本地对话（拉全量历史 +
    #                               校正续接锚点），此后发消息就进那条会话
    #
    # 两条都会**联网**，所以与本地那批（零延迟）分开：界面切对话是瞬时的，
    # 只有用户主动点「网页版」时才走这两条。

    async def list_remote_sessions(self, count: int = 50) -> dict:
        """列出账号下的服务端会话，并标出哪些已绑定到本地对话。

        返回 `{"items": [...], "error": ""}`。**失败不抛** —— 界面据此显示
        「读不到」而不是把整个侧栏弄崩；读不到会话列表不是致命错误。

        `items[i]` 字段：`remote_id`（服务端 id）/ `title` / `updated_at` /
        `pinned` / `local_id`（已接管时的本地对话 id，空=未接管）/ `bound` /
        `active`（是不是当前对话）。
        """
        await self.start()
        if self._start_error:
            return {"items": [], "error": self._start_error}

        # 夹到服务端硬边界（2026-10-18 实测 T-20：count>100 会**静默返回空列表**）。
        #
        # 这里选择「夹住」而不是「报错」：调用方的意图是「尽量多拿」，
        # 传 500 的人想要的是全部而不是一个错误。但**必须在返回值里说明**
        # 夹过 —— 否则用户以为拿到了 500 条，实际只有 100 条上限内的。
        requested = int(count)
        clamped = min(max(requested, SESSION_LIST_MIN_COUNT), SESSION_LIST_MAX_COUNT)

        try:
            sessions = await self.transport.list_sessions(clamped)
        except TransportError as exc:
            return {"items": [], "error": str(exc)}

        bindings = self.store.session_bindings()
        active_id = self.conversation.id if self.conversation else ""
        items: list[dict] = []
        for s in sessions:
            if not isinstance(s, dict):
                continue
            remote_id = str(s.get("id") or "")
            if not remote_id:
                continue
            local_id = bindings.get(remote_id, "")
            items.append(
                {
                    "remote_id": remote_id,
                    "title": str(s.get("title") or ""),
                    "updated_at": float(s.get("updated_at") or 0.0),
                    "pinned": bool(s.get("pinned")),
                    "local_id": local_id,
                    "bound": bool(local_id),
                    "active": bool(local_id and local_id == active_id),
                }
            )
        return {
            "items": items,
            "error": "",
            # 夹过上限就明说 —— 否则用户以为拿到了 count 条，实际被截。
            # `truncated=True` 且 `items` 满 100 条时，几乎可以肯定账号里
            # 还有更早的会话拿不到（分页游标无效，见 T-20 实测）。
            "requested": requested,
            "effective_count": clamped,
            "truncated": requested > SESSION_LIST_MAX_COUNT,
        }

    async def attach_session(self, remote_id: str) -> dict:
        """把一个服务端会话接管成本地对话。

        * 已绑定 → 直接切过去（等价于 `load_conversation`），不重复导入
        * 未绑定 → 新建本地对话，拉全量历史填充正文，并用
          `chat_session.current_message_id` **校正续接锚点**；标记
          `attached=True`（不自动轮换）

        接管之后，用户在这个对话里发的消息就会带那条 `chat_session_id` +
        校正后的 `parent_message_id` 发出去 —— 这正是「确保我现在的输入是
        进去那个会话」。**锚点校正这一步不能省**：本地存的 `rid` 对这条
        服务端会话毫无意义，不校正的话下一轮会被服务端当成新 root，
        上下文静默断掉。

        返回 `{"attached": bool, ...}`；失败时带 `reason`，不抛。
        """
        remote_id = (remote_id or "").strip()
        if not remote_id:
            return {"attached": False, "reason": "缺少会话 id"}

        # 已经接管过 → 切过去就行（幂等）。
        existing = self.store.find_by_session_cid(remote_id)
        if existing is not None:
            conv = self.load_conversation(existing.id)
            return {
                "attached": True,
                "reused": True,
                "id": existing.id,
                "conversation": conv,
            }

        await self.start()
        if self._start_error:
            return {"attached": False, "reason": self._start_error}
        try:
            biz = await self.transport.fetch_history(remote_id)
        except TransportError as exc:
            return {"attached": False, "reason": str(exc)}

        remote = normalize_history(biz)
        title = remote.title.strip() or f"网页版会话 {remote_id[:8]}"
        conv = self.store.create(title=title, settings=self._settings_snapshot())
        conv.messages = list(remote.messages)
        # `attached=True` 是这里唯一一件「不是照抄服务端」的事：它让这条会话
        # 免于自动轮换（见 `SessionMeta.attached`）。
        conv.session = SessionMeta(
            cid=remote_id,
            rid=remote.anchor,
            attached=True,
            turns=sum(1 for m in remote.messages if m.role == "user"),
        )
        conv.synced_at = time.time()
        self.store.save(conv)
        self.conversation = conv
        self.session.restore(conv.session)
        self.store.set_active(conv.id)
        return {
            "attached": True,
            "reused": False,
            "id": conv.id,
            "conversation": self.current_conversation(),
            "remote_count": remote.raw_count,
            "anchor": remote.anchor,
            "system_prompt_chars": len(remote.system_prompt),
        }

    async def delete_remote_session(self, remote_id: str) -> dict:
        """删除一条**服务端**会话（网页版历史里的那条）。**不可逆**。

        为什么不复用 `delete_conversation`：那个删的是本地文件，服务端那条
        还好端端躺在账号里 —— 下次 `remote_list` 又会把它列出来，用户会以为
        「删不掉」。反过来，只删服务端而不管本地绑定，会更糟：本地对话的
        `session.cid` 仍指向一个已消失的会话，下一轮 `send()` 会拿它去发，
        服务端回 `invalid chat session id`，用户看到的是「突然发不出消息」。

        所以这里做两件事，顺序不能反：

          1. 先删服务端（失败就原样返回，本地一点不动）；
          2. 再把本地绑定**解绑**（`cid` 清空、`attached` 复位），
             让这条本地对话退回「独立会话」，下一轮发消息会自己新建一条
             服务端会话，而不是撞在死 id 上。

        返回 `{"deleted": bool, "unbound": bool, "local_id": str, "reason": str}`。
        失败不抛 —— 与 `list_remote_sessions` / `attach_session` 一致，
        界面据此提示，而不是把侧栏弄崩。
        """
        remote_id = (remote_id or "").strip()
        if not remote_id:
            return {"deleted": False, "unbound": False, "local_id": "", "reason": "缺少会话 id"}

        await self.start()
        if self._start_error:
            return {
                "deleted": False,
                "unbound": False,
                "local_id": "",
                "reason": self._start_error,
            }

        try:
            await self.transport.delete_session(remote_id)
        except TransportError as exc:
            return {"deleted": False, "unbound": False, "local_id": "", "reason": str(exc)}

        # 服务端已经删了 —— 本地绑定必须跟着解，否则它会指向死 id。
        local_id = ""
        unbound = False
        conv = self.store.find_by_session_cid(remote_id)
        if conv is not None:
            local_id = conv.id
            conv.session = SessionMeta()  # cid="" / rid="" / attached=False
            self.store.save(conv)
            unbound = True
            # 如果删的正是当前对话，引擎手里那份对象也得同步 ——
            # 否则下一轮 `send()` 用的还是带死 cid 的旧对象。
            if self.conversation is not None and self.conversation.id == conv.id:
                self.conversation = conv
                self.session.restore(conv.session)

        return {"deleted": True, "unbound": unbound, "local_id": local_id, "reason": ""}

    # ---------- 朗读（TTS）----------
    #
    # 只做**控制面**：签票 + 音色。音频流由前端直连上游（理由见
    # `transport/deepseek_rpc.py` 的 `TTS_TICKET_PATH` 注释）——
    # 引擎在这里的角色是「确认这条消息确实可读，并给出连流所需的三个值」。
    #
    # 与 `list_remote_sessions` 一致：**失败不抛**，返回 `error` 让界面提示。
    # 朗读失败不该让整个会话面板崩掉，更不该污染对话流。

    async def prepare_tts(self, message_id: str) -> dict:
        """为某条消息签发朗读票据。

        返回 `{"sid","message_id","ticket","error"}`。前端拿这三个值直连
        `wss://chat.deepseek.com/api/v0/chat/tts/`。

        **必须先确认这条消息属于当前会话**：票据本身不绑消息，绑的是
        「这个 scope 能访问的资源」。签一张票去读别人的消息，服务端会在
        WS 握手阶段回 `code 2 INVALID_INPUT` —— 那时错误离原因已经很远，
        排查要绕一大圈。所以这里在签票之前先看一眼本地记录。
        """
        mid = str(message_id or "").strip()
        if not mid:
            return {"sid": "", "message_id": "", "ticket": "", "error": "缺少 message_id"}

        await self.start()
        if self._start_error:
            return {"sid": "", "message_id": mid, "ticket": "", "error": self._start_error}

        # 票据绑的是**服务端会话**，所以必须有 cid。没有 cid 说明这条对话
        # 还没发过消息（服务端会话是发第一条消息时才建的），也就没有可朗读的
        # 服务端消息 —— 这是本地可判定的，不该发一次注定失败的请求。
        meta = self.session.meta
        cid = str(meta.cid or "").strip()
        if not cid:
            return {
                "sid": "",
                "message_id": mid,
                "ticket": "",
                "error": "这条对话还没有服务端会话，无法朗读",
            }

        try:
            ticket = await self.transport.fetch_tts_ticket(cid, mid)
        except TransportError as exc:
            return {"sid": cid, "message_id": mid, "ticket": "", "error": str(exc)}

        return {"sid": cid, "message_id": mid, "ticket": ticket, "error": ""}

    async def list_tts_voices(self) -> dict:
        """列出朗读音色。只读，不消耗朗读配额。

        返回 `{"items": [...], "error": ""}`。
        """
        await self.start()
        if self._start_error:
            return {"items": [], "error": self._start_error}
        try:
            voices = await self.transport.list_tts_voices()
        except TransportError as exc:
            return {"items": [], "error": str(exc)}
        return {"items": voices, "error": ""}

    async def set_tts_voice(self, voice_id: str) -> dict:
        """切换朗读音色。**写操作** —— 改的是账号级偏好，网页版同步生效。"""
        vid = str(voice_id or "").strip()
        if not vid:
            return {"ok": False, "voice_id": "", "error": "缺少 voice_id"}
        await self.start()
        if self._start_error:
            return {"ok": False, "voice_id": vid, "error": self._start_error}
        try:
            await self.transport.set_tts_voice(vid)
        except TransportError as exc:
            return {"ok": False, "voice_id": vid, "error": str(exc)}
        return {"ok": True, "voice_id": vid, "error": ""}

    def rename_conversation(self, cid: str, title: str) -> dict | None:
        """重命名。当前对话走本类持有的对象，避免与 store 的读-改-写打架。"""
        if self.conversation and cid == self.conversation.id:
            self.conversation.title = (title or "").strip()[:80] or "新对话"
            self.store.save(self.conversation)
            return {"id": cid, "title": self.conversation.title}
        conv = self.store.rename(cid, title)
        return {"id": cid, "title": conv.title} if conv is not None else None

    def delete_conversation(self, cid: str) -> dict:
        """删除对话。删掉的是当前对话时，跟着切到替代的那条。"""
        was_active = bool(self.conversation and cid == self.conversation.id)
        ok = self.store.delete(cid)
        if was_active:
            # store.delete 会挑一条替代的 active（一条都不剩就新建一个），
            # 这里必须跟着换，否则引擎手里的对象指向一个已经不存在的对话。
            self.conversation = self.store.ensure_active()
            self.session.restore(self.conversation.session)
        return {
            "deleted": ok,
            "active": self.conversation.id if self.conversation else "",
        }

    # ---------- 长期记忆 ----------

    def memory_status(self) -> dict:
        st = self.memory.status()
        st["enabled"] = self.cfg.memory.enabled
        st["inject"] = self.cfg.memory.inject
        return st

    def read_memory(self) -> str:
        return self.memory.read()

    def write_memory(self, text: str) -> dict:
        self.memory.write(text)
        return self.memory_status()

    def append_memory(self, text: str, section: str = "") -> dict:
        """追加一条记忆。界面上的「记住这条」走这里。"""
        self.memory.append(text, section)
        return self.memory_status()

    def clear_memory(self) -> dict:
        self.memory.clear()
        return self.memory_status()

    # ---------- 工具派发 ----------

    async def _run_one(
        self, call: ToolCall, seq: int, turn: int
    ) -> tuple[ToolExecution, str, str | None] | None:
        """执行单个工具调用 + 心跳。**不改 report** —— 便于并行调用。

        调用方负责把结果按序收进 report / results。这样并行的多个调用
        各自产出、由派发层统一排序，不会互相踩。
        """
        started = time.monotonic()
        # 执行期间的心跳。**没有它，工具执行在界面上就是一段死寂**：
        # 本地命令可能跑几十秒到几分钟，而原来的实现从 tool_call 事件
        # 发出到下一轮 turn_start 之间一个事件都没有，用户看到的就是
        # 「画面停了」—— 分不清在跑还是卡死。
        beat = asyncio.create_task(self._tool_heartbeat(seq, turn, call, started))
        try:
            outcome = await self._execute(call)
        finally:
            beat.cancel()

        if outcome is None:
            return None
        execution, _text, _attach = outcome
        self._emit(
            "tool_done",
            {
                "seq": seq,
                "turn": turn,
                "kind": execution.kind,
                "target": execution.target,
                "exit_code": execution.exit_code,
                "duration_ms": round((time.monotonic() - started) * 1000.0),
                "output": execution.output,
                "rejected": execution.rejected,
            },
        )
        return outcome

    def _group_for_parallel(
        self, calls: list[ToolCall]
    ) -> list[list[tuple[int, ToolCall]]]:
        """把调用按「连续只读段」分组。

        **为什么是「连续的一段」而不是「所有只读调用」**：
        `[读A, 写B, 读C]` 里，读C 可能依赖写B 的结果 —— 全并行会打乱这个
        语义（读C 可能在写B 之前跑完）。只把**相邻**的只读调用并起来，
        就保住了「读-写-读」的顺序关系。

        返回 list[list[(原始下标, 调用)]]；每组内可并行，组间严格串行。
        """
        groups: list[list[tuple[int, ToolCall]]] = []
        current: list[tuple[int, ToolCall]] = []
        for i, call in enumerate(calls):
            if call.kind in _PARALLEL_SAFE:
                current.append((i, call))
            else:
                if current:
                    groups.append(current)
                    current = []
                groups.append([(i, call)])
        if current:
            groups.append(current)
        return groups

    async def _dispatch(
        self,
        calls: list[ToolCall],
        report: RunReport,
        seqs: list[int] | None = None,
        turn: int = 0,
    ) -> tuple[list[str], list[str]]:
        results: list[str] = []
        attachments: list[str] = []
        seqs = seqs or list(range(1, len(calls) + 1))

        # 只读调用可并发（瓶颈是 IO/网络等待），有副作用的必须串行
        # （顺序有语义）。按「连续只读段」分组，组间串行、组内并行。
        # 可在配置里用 `executor.parallel_reads = false` 关掉。
        parallel_on = bool(getattr(self.cfg.executor, "parallel_reads", True))
        groups = (
            self._group_for_parallel(calls)
            if parallel_on
            else [[(i, c)] for i, c in enumerate(calls)]
        )

        for group in groups:
            # **统一走 gather**（哪怕只有 1 个）—— 这样单跑与并发的异常语义
            # 一致：工具炸了吞成结果文本回给模型，而不是终止整轮。
            # 早先单跑是直接 await，异常会冒泡终止 run()；而同一次调用放在
            # 并行组里却被吞掉 —— 同一个工具、同样的错误，行为取决于它旁边
            # 有没有别的只读调用，这不可接受。
            outs = list(
                await asyncio.gather(
                    *[self._run_one(c, seqs[i], turn) for i, c in group],
                    return_exceptions=True,
                )
            )

            # 按组内原顺序收集 —— 保证回给模型的结果与调用顺序一致。
            for out in outs:
                if isinstance(out, BaseException):
                    # CancelledError 是**取消信号**，不是工具错误 —— 必须
                    # 重新抛出。否则 `gather(return_exceptions=True)` 会把它
                    # 吞成一条「工具执行异常」文本，于是 run.cancel 失效：
                    # 用户点了取消，任务却继续往下跑。
                    if isinstance(out, asyncio.CancelledError):
                        raise out
                    # 其它异常：单个工具炸了不该拖垮整组（也不该中断这一轮）。
                    results.append(
                        f"[工具执行异常] {type(out).__name__}: {out}"
                    )
                    continue
                if out is None:
                    continue
                execution, text, attach = out
                report.executions.append(execution)
                results.append(text)
                if attach:
                    attachments.append(attach)

        return results, attachments

    async def _tool_heartbeat(
        self, seq: int, turn: int, call: ToolCall, started: float
    ) -> None:
        """每 1.5 秒报一次「还在跑」。

        频率取 1.5 秒是权衡：太快会刷满对话流，太慢又回到「看起来卡住」。
        心跳只更新既有卡片的那一行，不加新条。
        """
        while True:
            await asyncio.sleep(1.5)
            self._emit(
                "tool_progress",
                {
                    "seq": seq,
                    "turn": turn,
                    "kind": call.kind.value,
                    "target": call.target,
                    "elapsed_ms": round((time.monotonic() - started) * 1000.0),
                },
            )

    def _todo_path(self) -> Path:
        """当前对话的清单文件路径。

        按对话隔离：todo 是「当前这件事」的进度，不是跨会话的长期事实
        （那个是 memory）。所以每个对话一份，切对话自然就换了清单。
        """
        cid = self.conversation.id if self.conversation else "default"
        return tools_todo.todo_path_for(app_data_dir(), cid)

    async def _run_code(
        self, spec: str, source: str
    ) -> tuple[ToolExecution, str, None]:
        """run_code:<参数> + 围栏正文（Python 源码）。

        脚本在**沙箱子进程**里跑（见 `sandbox.py`）——不在引擎进程里 exec。
        target 里可带 timeout= / memory=。
        """
        # **不能用 split_path_args** —— 那个要求 key=value 前有空白，而这里
        # target 整个就是 `timeout=10 memory=256`（无位置参数），实测会整段
        # 返回、参数全丢。同一个坑在 run_tests 的 cmd=、apply_patch 的 root=
        # 上各踩过一次 —— 这是第三次。纯 kv 的 spec 一律用正则直接抽。
        import re as _re

        def _kv(key: str, default: str) -> str:
            m = _re.search(rf"(?:^|\s){key}=(\S+)", spec or "")
            return m.group(1) if m else default

        try:
            timeout = float(_kv("timeout", str(sandbox.DEFAULT_TIMEOUT)))
        except ValueError:
            timeout = sandbox.DEFAULT_TIMEOUT
        timeout = max(1.0, min(timeout, 600.0))
        try:
            memory_mb = int(_kv("memory", str(sandbox.DEFAULT_MEMORY_MB)))
        except ValueError:
            memory_mb = sandbox.DEFAULT_MEMORY_MB
        memory_mb = max(64, min(memory_mb, 4096))

        try:
            work_dir = self.executor.snapshot().get("work_dir") or str(Path.home())
        except Exception:  # noqa: BLE001
            work_dir = str(Path.home())

        loop = asyncio.get_running_loop()
        started = time.monotonic()
        result = await loop.run_in_executor(
            None,
            lambda: sandbox.run_python(
                source or "",
                work_dir=work_dir,
                timeout=timeout,
                memory_mb=memory_mb,
            ),
        )
        text = sandbox.format_result(result, work_dir)
        execution = ToolExecution(
            kind=ToolKind.RUN_CODE.value,
            target=spec[:200],
            exit_code=result.exit_code,
            output=result.output,
            duration_ms=(time.monotonic() - started) * 1000.0,
            rejected=False,
        )
        return execution, text, None

    async def _run_git(self, spec: str) -> tuple[ToolExecution, str, None]:
        """git:<子命令> —— 只读三件套（status / diff / log）。

        写操作故意不接：commit/push/reset 改工作区状态，误调代价高；
        真要写库让模型走 local_cmd（显式、可审计）。
        spec 形如 `status` / `diff [<路径>|--stat]` / `log [n]`。
        """
        text_in = (spec or "").strip()
        sub, _, rest = text_in.partition(" ")
        sub = sub.strip().lower()
        rest = rest.strip()

        try:
            work_dir = self.executor.snapshot().get("work_dir") or str(Path.home())
        except Exception:  # noqa: BLE001
            work_dir = str(Path.home())

        if sub in ("", "status"):
            fn, args = tools_git.status, (work_dir,)
        elif sub == "diff":
            if rest == "--stat":
                fn, args = tools_git.diff, (work_dir, "", True)
            else:
                fn, args = tools_git.diff, (work_dir, rest, False)
        elif sub == "log":
            try:
                n = int(rest) if rest else tools_git.DEFAULT_LOG_N
            except ValueError:
                return await self._run_fs(
                    ToolKind.GIT.value,
                    spec,
                    lambda: (False, "[git:log] 条数必须是数字，如 `git:log 20`"),
                )
            fn, args = tools_git.log, (work_dir, n)
        else:
            return await self._run_fs(
                ToolKind.GIT.value,
                spec,
                lambda: (
                    False,
                    f"[git] 未知子命令：{sub}（只支持 status / diff / log）",
                ),
            )
        return await self._run_fs(ToolKind.GIT.value, spec, fn, *args)

    async def _run_fs(
        self, kind: str, target: str, fn, *args
    ) -> tuple[ToolExecution, str, None]:
        """在**线程**里跑文件工具（文件 IO 可能阻塞，不能占事件循环）。

        tools_fs 的函数统一返回 (ok, text)：text 已经是「状态行 + 内容」，
        这里只把它包成执行记录并补上收尾提示。
        """
        loop = asyncio.get_running_loop()
        started = time.monotonic()
        ok, text = await loop.run_in_executor(None, fn, *args)
        execution = ToolExecution(
            kind=kind,
            target=target[:200],
            exit_code=0 if ok else 1,
            output=text,
            duration_ms=(time.monotonic() - started) * 1000.0,
        )
        return execution, format_file_tool_result(ok, text), None

    def _preview_write_targets(
        self, kind: str, target: str, payload: str, work_dir: str
    ) -> list[str]:
        """dry-run 下报告「这次写会碰到哪些文件、会不会覆盖」。

        四种写工具的路径来源各不相同，不能只取 target 的第一个 token：

          * `write_file`        target 就是路径本身
          * `replace_in_file`   target 是 `路径 count=N expect=M`（见 `split_path_args`）
          * `multi_edit`        同上
          * `apply_patch`       target 是 `apply_patch [root=...]`，**真正的路径在
                                payload 的 diff 里** —— 只报 target 会得到
                                「目标：apply_patch」，等于什么都没说

        所有解析失败都静默降级（`contextlib.suppress`）：预览是辅助信息，
        不能因为它自己出错就把整个工具调用拦住。
        """
        out: list[str] = []

        def _report(rel: str) -> None:
            rel = (rel or "").strip().strip('"').strip("'")
            if not rel:
                return
            p = Path(os.path.expanduser(rel))
            try:
                if p.exists():
                    if p.is_dir():
                        out.append(f"目标：{rel} —— 是目录（write_file 会失败）")
                    else:
                        out.append(
                            f"目标：{rel} —— 已存在（{p.stat().st_size} 字节），"
                            "真跑会覆盖。"
                        )
                else:
                    out.append(f"目标：{rel} —— 不存在，真跑会新建。")
            except OSError as exc:
                out.append(f"目标：{rel} —— 无法读取状态（{exc}）")

        if kind == "apply_patch":
            with contextlib.suppress(Exception):
                base = Path(work_dir or ".").expanduser()
                m = re.search(r"root=(\S+)", target or "")
                if m:
                    base = Path(m.group(1)).expanduser()
                diffs = tools_patch.parse_unified_diff(payload)
                if not diffs:
                    out.append("diff 解析失败 —— 真跑会直接报错，不会写任何文件。")
                else:
                    out.append(f"涉及 {len(diffs)} 个文件：")
                    for fp in diffs:
                        rel = str(fp.path)
                        shown = rel if Path(rel).is_absolute() else str(base / rel)
                        _report(shown)
            return out

        with contextlib.suppress(Exception):
            if kind == "write_file":
                _report(target)
            else:
                # `路径 count=N expect=M` → 只取路径部分
                rel, _args = tools_fs.split_path_args(target)
                _report(rel)
        return out

    def _screen_permission(self, call: ToolCall) -> ToolExecution | None:
        """权限分级 + dry-run 拦截。返回 ToolExecution 表示拦下，None 表示放行。

        **为什么放在 `_execute` 分发之前**：`local_cmd` 与 `local_cmd:async`
        各自在 `_run_command` / `_run_async` 里已经调过一次 `_screen_command`
        （危险命令检测）。如果权限检查也分散到各个 `_run_*` 里，以后新增一个
        工具就得记得加一处检查 —— 漏掉一处就是一个绕过口。收敛到分发入口，
        结构上不可能漏。

        与 `_screen_command` 的分工：那个管「命令本身危不危险」（不可豁免的
        硬底线），这个管「模型现在该不该有这个能力」（用户可调）。
        """
        # `getattr` 兜底而不是直接 `self.cfg`：多个测试用
        # `AgentRuntime.__new__` 构造轻量对象（刻意跳过 `__init__`），
        # 只塞被测算子需要的属性。真实运行时 `__init__` 必然赋值。
        # 缺失时取默认 `exec` / 非 dry-run —— 与升级前行为一致。
        cfg = getattr(getattr(self, "cfg", None), "permissions", None)
        if cfg is None:
            cfg = permissions.PermissionConfig()
        kind = call.kind.value
        target = call.target or ""
        payload = call.payload or ""

        # 1) 权限档位
        denial = permissions.check(cfg, kind, target, payload)
        if denial is not None:
            return ToolExecution(
                kind=kind,
                target=(target or payload)[:200],
                exit_code=1,
                output=denial,
                rejected=True,
            )

        # 2) dry-run 预览
        if not permissions.needs_preview(cfg, kind, target, payload):
            return None

        # 补上下文：工作目录对所有写/执行类都有意义；写文件额外报告
        # 「会不会覆盖已有文件」—— 这正是用户看预览时最关心的一件事。
        parts: list[str] = []
        work_dir = ""
        with contextlib.suppress(Exception):
            work_dir = self.executor.snapshot().get("work_dir", "")
            if work_dir:
                parts.append(f"工作目录：{work_dir}")

        if kind in ("write_file", "replace_in_file", "multi_edit", "apply_patch"):
            parts.extend(self._preview_write_targets(kind, target, payload, work_dir))

        extra = "\n".join(parts)

        return ToolExecution(
            kind=kind,
            target=(target or payload)[:200],
            exit_code=0,
            output=permissions.preview(kind, target, payload, extra),
        )

    async def _execute(
        self, call: ToolCall
    ) -> tuple[ToolExecution, str, str | None] | None:
        """执行单个工具调用，返回 (执行记录, 回给上游的结果文本, 附件路径)。

        返回 None 表示这个 kind 不认识 —— 与旧行为一致：不加执行记录，
        也不往 results 里塞东西。
        """
        # 权限分级 + dry-run 必须在分发之前 —— 见 `_screen_permission`。
        screened = self._screen_permission(call)
        if screened is not None:
            return (
                screened,
                format_command_result(screened.exit_code, screened.output),
                None,
            )

        if call.kind is ToolKind.LOCAL_CMD:
            execution = await self._run_command(call.payload)
            return (
                execution,
                format_command_result(execution.exit_code, execution.output),
                None,
            )

        if call.kind is ToolKind.LOCAL_CMD_ASYNC:
            execution = await self._run_async(call.payload)
            return (
                execution,
                format_command_result(
                    execution.exit_code,
                    f"[异步泳道已启动] {call.payload}\n{execution.output}",
                ),
                None,
            )

        if call.kind is ToolKind.READ_FILE:
            return await self._run_fs(
                ToolKind.READ_FILE.value, call.target, tools_fs.read_file, call.target
            )

        if call.kind is ToolKind.LIST_DIR:
            return await self._run_fs(
                ToolKind.LIST_DIR.value, call.target, tools_fs.list_dir, call.target
            )

        if call.kind is ToolKind.REPLACE_IN_FILE:
            # target = 路径+参数；payload = 围栏正文（OLD/NEW 三段）
            return await self._run_fs(
                ToolKind.REPLACE_IN_FILE.value,
                call.target,
                tools_fs.replace_in_file,
                call.target,
                call.payload,
            )

        if call.kind is ToolKind.RUN_TESTS:
            # 不走 _run_fs：run_tests 需要注入工作目录和 executor.run
            # （它自己不做执行，只识别 + 拼命令，执行仍复用常驻进程）。
            loop = asyncio.get_running_loop()
            started = time.monotonic()
            root = Path(
                self.executor.snapshot().get("work_dir") or Path.home()
            )
            okf, text = await loop.run_in_executor(
                None,
                tools_tests.run_tests,
                call.target,
                root,
                self.executor.run,
            )
            execution = ToolExecution(
                kind=ToolKind.RUN_TESTS.value,
                target=call.target[:200],
                exit_code=0 if okf else 1,
                output=text,
                duration_ms=(time.monotonic() - started) * 1000.0,
            )
            from .protocol import format_file_tool_result
            return execution, format_file_tool_result(okf, text), None

        if call.kind is ToolKind.ASK_USER:
            # 问题可能在 target（单行）或 payload（围栏正文）里，取非空的那个。
            question = (call.target or "").strip() or (call.payload or "").strip()
            execution = ToolExecution(
                kind=ToolKind.ASK_USER.value,
                target=question[:200],
                exit_code=0,
                output=question,
            )
            return execution, format_ask_user(question), None

        if call.kind is ToolKind.RUN_CODE:
            return await self._run_code(call.target, call.payload)

        if call.kind is ToolKind.WEB_FETCH:
            return await self._run_fs(
                ToolKind.WEB_FETCH.value, call.target, tools_web.fetch, call.target
            )

        if call.kind is ToolKind.WEB_SEARCH:
            return await self._run_fs(
                ToolKind.WEB_SEARCH.value, call.target, tools_web.search, call.target
            )

        if call.kind is ToolKind.MCP:
            return await self._run_mcp(call.target)

        if call.kind is ToolKind.JOB:
            return await self._run_job(call.target)

        if call.kind is ToolKind.GREP:
            return await self._run_fs(
                ToolKind.GREP.value, call.target, tools_search.grep, call.target
            )

        if call.kind is ToolKind.GLOB:
            return await self._run_fs(
                ToolKind.GLOB.value, call.target, tools_search.glob_search, call.target
            )

        if call.kind is ToolKind.GIT:
            return await self._run_git(call.target)

        if call.kind is ToolKind.TODO_WRITE:
            path = self._todo_path()
            return await self._run_fs(
                ToolKind.TODO_WRITE.value,
                str(path),
                tools_todo.todo_write,
                path,
                call.payload,
            )

        if call.kind is ToolKind.TODO_READ:
            path = self._todo_path()
            return await self._run_fs(
                ToolKind.TODO_READ.value,
                str(path),
                tools_todo.todo_read,
                path,
            )

        if call.kind is ToolKind.MULTI_EDIT:
            return await self._run_fs(
                ToolKind.MULTI_EDIT.value,
                call.target,
                tools_patch.multi_edit,
                call.target,
                call.payload,
            )

        if call.kind is ToolKind.APPLY_PATCH:
            # apply_patch 的 root= 是相对**工作目录**的，不是进程 cwd ——
            # 引擎进程的 cwd 可能是任意位置，必须显式注入 work_dir。
            spec = call.target
            if "root=" not in spec:
                try:
                    wd = self.executor.snapshot().get("work_dir", "")
                except Exception:  # noqa: BLE001
                    wd = ""
                if wd:
                    spec = f"root={wd}"
            return await self._run_fs(
                ToolKind.APPLY_PATCH.value,
                call.target,
                tools_patch.apply_patch,
                spec,
                call.payload,
            )

        if call.kind is ToolKind.WRITE_FILE:
            # `_write_file` 里是同步磁盘写。模型写几百 KB 的文件时它会阻塞整个
            # 事件循环（SSE 解析、事件发送、状态轮询全停）。丢到线程池里去，
            # 事件循环继续转 —— 这条工具本来就是「写一个文件」，没有并发写同一
            # 路径的风险（工具执行是串行的）。
            execution, note = await asyncio.to_thread(
                self._write_file, call.target, call.payload
            )
            return (
                execution,
                format_write_result(call.target, execution.exit_code, note),
                None,
            )

        if call.kind is ToolKind.SCREENSHOT:
            execution, path = await self._screenshot()
            attach = path or None
            return (
                execution,
                format_attachment_result(path or "（截图失败）", bool(path)),
                attach,
            )

        if call.kind is ToolKind.ATTACH:
            path = str(Path(call.payload).expanduser())
            ok = Path(path).exists()
            return (
                ToolExecution(
                    kind=call.kind.value,
                    target=path,
                    exit_code=0 if ok else 1,
                    output="已加入下一轮附件" if ok else f"文件不存在：{path}",
                ),
                format_attachment_result(path, ok),
                path if ok else None,
            )

        return None

    def _screen_command(self, command: str) -> ToolExecution | None:
        """命令安全检查。返回 ToolExecution 表示拒绝，None 表示放行。

        BLOCK 档硬拒绝（连 #danger-ok 都不认）；WARN 档默认拒绝但可豁免。
        拒绝时**不执行**，把理由作为工具结果回传 —— 模型据此可以改用 ask_user。
        """
        severity, reason = safety.check_command(command)
        if severity == safety.OK:
            return None
        return ToolExecution(
            kind=ToolKind.LOCAL_CMD.value,
            target=command[:200],
            exit_code=1,
            output=safety.format_rejection(severity, reason, command),
            rejected=True,
        )

    async def _run_command(self, command: str) -> ToolExecution:
        blocked = self._screen_command(command)
        if blocked is not None:
            return blocked
        loop = asyncio.get_running_loop()
        started = time.monotonic()
        result = await loop.run_in_executor(None, self.executor.run, command)
        return ToolExecution(
            kind=ToolKind.LOCAL_CMD.value,
            target=command[:200],
            exit_code=result.exit_code,
            output=result.output,
            duration_ms=(time.monotonic() - started) * 1000.0,
            rejected=result.rejected,
        )

    async def _run_async(self, command: str) -> ToolExecution:
        """异步泳道：丢进 JobManager，返回可查询的作业号。

        早先这里是「发射后不管」——拿不到输出、查不了状态。现在每个后台命令
        都有 id，模型可以起一条长命令、干别的、回头用 `job:status` 看结果。
        """
        # 异步泳道同样要过安全闸门 —— 否则危险命令会绕开 _run_command 溜过去。
        blocked = self._screen_command(command)
        if blocked is not None:
            blocked.kind = ToolKind.LOCAL_CMD_ASYNC.value
            return blocked
        jid = self.jobs.submit(
            command,
            lambda cmd: self.executor.run(cmd),
            on_cancel=self.executor.restart,
        )
        return ToolExecution(
            kind=ToolKind.LOCAL_CMD_ASYNC.value,
            target=command[:200],
            exit_code=0,
            output=(
                f"命令已在后台运行，作业号 {jid}。"
                f"用 `job:status {jid}` 查看输出，`job:cancel {jid}` 取消。"
            ),
        )

    def _mcp_servers(self) -> list[mcp.McpServerConfig]:
        """从配置读出启用的 MCP server 列表。"""
        if not self.cfg.mcp.enabled:
            return []
        out: list[mcp.McpServerConfig] = []
        for name, data in (self.cfg.mcp.servers or {}).items():
            if isinstance(data, dict):
                out.append(mcp.McpServerConfig.from_dict(str(name), data))
        return out

    def start_mcp(self) -> None:
        """连接配置里的 MCP server。失败不抛 —— 某个 server 挂了不该阻止引擎启动。"""
        with contextlib.suppress(Exception):
            self.mcp.load(self._mcp_servers())

    async def _run_mcp(self, spec: str) -> tuple[ToolExecution, str, None]:
        """mcp:list / mcp:<server>/<tool> <JSON>。

        单一工具 + 动态子命令 —— 而不是给每个 MCP 工具建 ToolKind。
        理由：MCP 工具是运行时发现的，数量不定，无法预先枚举。
        """
        from .protocol import format_file_tool_result

        text_in = (spec or "").strip()
        sub, _, rest = text_in.partition(" ")
        sub = sub.strip()
        rest = rest.strip()

        def _wrap(text: str, code: int = 0) -> tuple[ToolExecution, str, None]:
            ex = ToolExecution(
                kind=ToolKind.MCP.value,
                target=text_in[:200],
                exit_code=code,
                output=text,
            )
            return ex, format_file_tool_result(code == 0, text), None

        if sub in ("", "list", "ls"):
            tools = self.mcp.all_tools()
            snap = self.mcp.snapshot()
            if not tools:
                return _wrap(
                    "[mcp:list] 没有可用的 MCP 工具。\n"
                    "（未启用 MCP，或配置里没有 server，或 server 连接失败）"
                )
            lines = [f"{t.qualified}  —  {t.description[:70]}" for t in tools]
            head = f"[mcp:list] 共 {len(tools)} 个工具，来自 {len(snap['servers'])} 个 server"
            return _wrap(head + "\n\n" + "\n".join(lines))

        # sub 形如 server/tool
        found = self.mcp.find(sub)
        if found is None:
            return _wrap(
                f"[mcp 失败] 找不到工具 {sub!r}。用 `mcp:list` 查看可用的。", 1
            )
        conn, tool = found

        # 参数是 JSON（可选）
        args: dict = {}
        if rest:
            import json as _json
            try:
                parsed = _json.loads(rest)
                if isinstance(parsed, dict):
                    args = parsed
                else:
                    return _wrap(
                        f"[mcp 失败] 参数必须是 JSON 对象，收到的是 {type(parsed).__name__}。", 1
                    )
            except ValueError as exc:
                return _wrap(f"[mcp 失败] 参数不是合法 JSON：{exc}", 1)

        loop = asyncio.get_running_loop()
        started = time.monotonic()
        try:
            out = await loop.run_in_executor(None, conn.call_tool, tool.name, args)
        except mcp.McpError as exc:
            ex = ToolExecution(
                kind=ToolKind.MCP.value,
                target=sub[:200],
                exit_code=1,
                output=f"调用失败：{exc}",
                duration_ms=(time.monotonic() - started) * 1000.0,
            )
            return ex, format_file_tool_result(False, ex.output), None

        ex = ToolExecution(
            kind=ToolKind.MCP.value,
            target=sub[:200],
            exit_code=0,
            output=out,
            duration_ms=(time.monotonic() - started) * 1000.0,
        )
        return ex, format_file_tool_result(True, out), None

    async def _run_job(self, spec: str) -> tuple[ToolExecution, str, None]:
        """job:list / job:status <id> / job:cancel <id>。"""
        parts = (spec or "").strip().split(None, 1)
        sub = (parts[0] if parts else "list").lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        def _wrap(text: str, code: int = 0) -> tuple[ToolExecution, str, None]:
            ex = ToolExecution(
                kind=ToolKind.JOB.value,
                target=spec[:200],
                exit_code=code,
                output=text,
            )
            from .protocol import format_file_tool_result
            return ex, format_file_tool_result(code == 0, text), None

        if sub in ("list", "ls", ""):
            items = self.jobs.list_jobs()
            if not items:
                return _wrap("[job:list] 当前没有后台作业。")
            lines = [
                f"{j.id}  {j.status:9}  {j.duration_ms:>9.0f}ms  {j.command[:60]}"
                for j in items
            ]
            return _wrap(f"[job:list] 共 {len(items)} 个作业" + "\n\n" + "\n".join(lines))

        if sub in ("status", "get"):
            if not arg:
                return _wrap("[job:status 失败] 缺少作业号。用法：job:status <id>", 1)
            job = self.jobs.get(arg)
            if job is None:
                return _wrap(f"[job:status 失败] 没有这个作业：{arg}", 1)
            head = (
                f"[job:status] {job.id}\n"
                f"状态 {job.status} · 退出码 {job.exit_code} · 耗时 {job.duration_ms:.0f}ms\n"
                f"命令：{job.command[:200]}"
            )
            if job.error:
                head += f"\n错误：{job.error}"
            body = job.output or "(暂无输出)"
            return _wrap(f"{head}\n\n{body}")

        if sub in ("cancel", "kill", "stop"):
            if not arg:
                return _wrap("[job:cancel 失败] 缺少作业号。用法：job:cancel <id>", 1)
            accepted, msg = self.jobs.cancel(arg, on_cancel=self.executor.restart)
            return _wrap(f"[job:cancel] {msg}", 0 if accepted else 1)

        return _wrap(
            f"[job 失败] 未知子命令 {sub!r}。可用：list / status <id> / cancel <id>", 1
        )

    def _write_file(self, target: str, content: str) -> tuple[ToolExecution, str]:
        raw = (target or "").strip()
        if not raw:
            return (
                ToolExecution(
                    kind=ToolKind.WRITE_FILE.value,
                    target="",
                    exit_code=1,
                    output="write_file 缺少目标路径",
                ),
                "目标路径为空。请写成 ```write_file:<绝对路径>。",
            )
        work_dir = ""
        with contextlib.suppress(Exception):
            work_dir = self.executor.snapshot().get("work_dir", "")
        escape = safety.check_path_escape(raw, work_dir)
        if escape:
            return (
                ToolExecution(
                    kind=ToolKind.WRITE_FILE.value,
                    target=raw[:200],
                    exit_code=1,
                    output=escape,
                    rejected=True,
                ),
                f"写入被拒绝：{escape}",
            )
        path = Path(os.path.normpath(raw))
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except OSError as exc:
            return (
                ToolExecution(
                    kind=ToolKind.WRITE_FILE.value,
                    target=str(path),
                    exit_code=1,
                    output=str(exc),
                ),
                f"写入失败：{exc}",
            )
        size = path.stat().st_size
        return (
            ToolExecution(
                kind=ToolKind.WRITE_FILE.value,
                target=str(path),
                exit_code=0,
                output=f"已写入 {size} 字节",
            ),
            f"已写入 {size} 字节（UTF-8）。",
        )

    async def _screenshot(self) -> tuple[ToolExecution, str]:
        if platform.system() != "Windows":
            return (
                ToolExecution(
                    kind=ToolKind.SCREENSHOT.value,
                    target="",
                    exit_code=1,
                    output="当前平台不支持截图",
                ),
                "",
            )
        out_dir = Path(tempfile.gettempdir()) / "deepseek-agent"
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / f"shot-{int(time.time())}.png"
        result = await self._run_command(_screenshot_script(str(target)))
        if result.exit_code == 0 and target.exists():
            return result, str(target)
        return result, ""

    # ---------- 辅助 ----------

    async def _await_gate(self, budget_s: float = 900.0) -> GateDecision:
        """在闸门放行之前一直等。

        为什么是轮询而不是「算出还要等多久就 sleep 多久」：闸门的状态会被
        外部改变（熔断层收到上游状态、用户改了身份、速率限制冷却结束），
        一次性长睡会错过这些变化。轮询本身是廉价的 —— `PacingGate.evaluate()`
        已经保证**幂等**（目标间隔抽一次就记住，见 pacing.py 第 4 步），
        所以多轮询几次不会把等待越推越长。

        `gate` 事件**只在理由变化时上报一次**：轮询一次就报一次的话，
        界面上会刷出好几条一模一样的「间隔未达目标」，把对话流顶下去，
        反而看不见真正发生了什么。
        """
        deadline = time.monotonic() + budget_s
        last_reason: str | None = None
        while True:
            decision = self.gates.evaluate()
            if decision.allowed or decision.fatal:
                return decision
            if decision.reason != last_reason:
                last_reason = decision.reason
                self._emit(
                    "gate",
                    {
                        "layer": decision.layer,
                        "reason": decision.reason,
                        "wait_ms": decision.wait_ms,
                    },
                )
            wait = min(decision.wait_ms / 1000.0, 30.0)
            if time.monotonic() + wait > deadline:
                return decision
            await asyncio.sleep(max(wait, 0.2))

    def _bootstrap_prompt(self, goal: str) -> str:
        snap = self.executor.snapshot()
        work_dir = snap.get("work_dir", "")
        # shell 提示取自**实际解析到的**解释器，不写死 —— 本机 `resolve_shell()`
        # 会优先 pwsh，但用户可以把 `executor.shell_binary` 配成 cmd.exe，
        # 那时提示词里还写「PowerShell」会让模型产出跑不通的语法。
        shell = Path(str(snap.get("shell", "") or "")).name or "shell"
        memory_block = self._memory_block()
        summary_block = self._summary_block()
        template = self.cfg.read_prompt_template()

        # 模板里写了 `{{memory}}` / `{{summary}}` 就按用户选的位置渲染；
        # 没写就追加到末尾。
        #
        # **不能只依赖占位符**：模板文件只在「不存在」时播种（刻意如此 ——
        # 免得覆盖用户改过的模板），所以老用户手里那份不会有这两个占位符，
        # 记忆与接力摘要就会整个失效，而且失效得毫无提示。它们是**客户端的
        # 系统能力**，不该因为模板是旧版就没了。
        inline = declares(template, "memory")
        inline_summary = declares(template, "summary")
        prompt = build_contract_prompt(
            os_name=f"{platform.system()} {platform.release()}",
            work_dir=work_dir,
            shell_hint=shell,
            soul=self.cfg.read_soul(),
            memory=memory_block if inline else "",
            summary=summary_block if inline_summary else "",
            max_turns=self.cfg.max_tool_turns,
            # 用户在设置里改过的模板。每轮现读，所以改完**下一轮就生效**，
            # 不需要重启引擎。
            template=template,
        )
        if memory_block and not inline:
            prompt = f"{prompt}\n\n{memory_block}"
        # 接力摘要排在记忆**之后**：记忆是跨会话的长期事实，摘要是本段对话的
        # 来龙去脉 —— 后者更贴近当下这一轮，放在后面离「# 任务」更近。
        if summary_block and not inline_summary:
            prompt = f"{prompt}\n\n{summary_block}"
        return f"{prompt}\n\n# 任务\n{goal}"

    def _memory_block(self) -> str:
        """长期记忆的注入段落。

        **每轮现读**，不是构造时读一次 —— 用户在界面上「记住这条」之后，
        下一轮就该带上，不该等到重启引擎。记忆文件很小，这点 IO 可忽略。
        """
        if not (self.cfg.memory.enabled and self.cfg.memory.inject):
            return ""
        return self.memory.as_prompt_block()

    def _summary_block(self) -> str:
        """接力摘要的注入段落（见 `compact.py`）。

        与 `_memory_block()` 的差别只有一处：记忆来自全局文件，摘要挂在
        **当前对话对象**上。所以这里是「每轮现读 `self.conversation`」——
        切对话之后下一轮自然就换成了另一条对话的摘要，不需要任何额外接线。

        没有摘要（绝大多数对话）时返回空串：它本来就是空态，写一段
        「暂无摘要」纯属占 token。
        """
        if not self.cfg.compact.enabled:
            return ""
        conv = self.conversation
        text = (conv.handoff_summary if conv is not None else "").strip()
        if not text:
            return ""
        if len(text) > self.cfg.compact.max_chars:
            text = text[: self.cfg.compact.max_chars].rstrip()
        return (
            "# 上一段对话的摘要\n\n"
            f"{text}\n\n"
            "以上是你与用户在**上一个会话**里的对话摘要。那个会话已经结束，"
            "你现在在一个新会话里继续。**把它当作已经发生的事实**，直接接着"
            "往下做：不要复述、不要向用户确认、也不要再问已经问过的问题。"
        )

    def _background_state(self) -> dict:
        """传输层的后台保活状态。取不到就当没有 —— 界面不该因此崩。"""
        try:
            state = dict(self.transport.background_state())
        except Exception:  # noqa: BLE001
            state = {}
        # 账号处置标记以 Agent 侧为准：它比传输实例活得久（见 __init__）。
        # 传输层自己也可能置位（发消息时拿到封禁响应），任一为真即为真。
        if self._account_suspended:
            state["account_suspended"] = True
            state["suspended_detail"] = self._suspended_detail
        state.setdefault("account_suspended", False)
        state.setdefault("suspended_detail", "")
        return state

    def mark_account_suspended(self, detail: str) -> None:
        """记下「账号被服务端处置」的证据（由 `identity.refresh` 调用）。"""
        self._account_suspended = True
        self._suspended_detail = detail

    def clear_account_suspension(self) -> None:
        """验证通过后清掉处置标记 —— 解封 / 换号后不该继续显示封禁。"""
        self._account_suspended = False
        self._suspended_detail = ""

    def status(self) -> dict:
        snapshot: dict[str, Any] = self.gates.snapshot()
        snapshot["transport"] = {
            "name": self.transport.name,
            "streaming": self.transport.supports_streaming,
            "ready": not self._start_error,
            "error": self._start_error,
            "background": self._background_state(),
        }
        snapshot["executor"] = self.executor.snapshot()
        snapshot["mode"] = self.cfg.identity.mode_label
        # **没有 `guest_mode`** —— DeepSeek 无访客模式，这个字段在语义上不存在，
        # 不是「没实现」。界面不要去找它。
        # 当前模型（空 = 账号默认）。放在顶层而不是 identity 里，
        # 因为它是「会话级」而不是「身份级」的设置 —— 改它不重启引擎。
        snapshot["model"] = self.cfg.identity.model
        # 会话形态（一般/临时对话、是否思考模式）。同样放顶层，理由同上。
        snapshot["chat"] = self.chat_snapshot()
        # 当前对话与长期记忆的**轻量**摘要。状态轮询每 60 秒一次，
        # 不该把整条历史带上 —— 列表与全文各有 `conversation.list` /
        # `conversation.current` 负责。
        snapshot["conversation"] = {
            "id": self.conversation.id,
            "title": self.conversation.title,
            "turn_count": self.conversation.turn_count,
            # 接力摘要的**轻量**标记（不带正文）。界面据此在标题旁挂一个
            # 「已接力」提示 —— 否则用户看到新对话里模型张口就知道旧事，
            # 会以为串了会话。
            "summary_chars": len((self.conversation.handoff_summary or "").strip()),
            "summary_from": self.conversation.handoff_from,
        }
        snapshot["memory"] = self.memory_status()
        # 权限档位与 dry-run。状态轮询会带它，界面据此显示当前模式 ——
        # 「只读模式下命令被拒」这类反馈必须有个地方能对上用户的设置。
        snapshot["permissions"] = self.permissions_snapshot()
        # 压缩档位（界面用它决定「接力」按钮是否可用、以及提示文案）。
        snapshot["compact"] = {
            "enabled": bool(self.cfg.compact.enabled),
            "mode": str(self.cfg.compact.mode or "model"),
            "max_chars": int(self.cfg.compact.max_chars),
        }
        return snapshot

    # ---------- 会话形态 ----------

    def chat_snapshot(self) -> dict:
        """当前会话形态。给界面回显用。

        `temporary_supported` 是**能力标记**，不是状态：DeepSeek 侧的临时对话
        接口尚未查明（见 `ChatConfig.temporary` 的注释），所以这个开关当前
        **不起作用**。界面必须据此把它置灰 —— 否则用户会以为自己开了「不留痕」，
        而对话其实照样进了账号历史。**这类「看起来生效了、其实没有」的开关
        比没有这个开关更危险。**
        """
        return {
            "temporary": self.session.meta.temporary,
            "temporary_supported": False,
            "extended_thinking": self.cfg.chat.extended_thinking,
            "search_enabled": self.cfg.chat.search_enabled,
            # 思考过程是否输出（见 `ChatConfig.show_thinking`）。界面拿它回显
            # 设置页那个勾选框 —— 它只管「看不看得见思考原文」，不影响上游
            # 是否思考（那是 `extended_thinking`）。
            "show_thinking": self.cfg.chat.show_thinking,
        }

    async def set_chat(
        self,
        *,
        temporary: bool | None = None,
        extended_thinking: bool | None = None,
        search_enabled: bool | None = None,
        show_thinking: bool | None = None,
    ) -> dict:
        """切换会话形态。只改配置、不重启运行时（和 `set_model` 同一条路子）。

        **`temporary` 当前不生效**（接口未查明，见 `ChatConfig.temporary`）。
        所以这里**不轮换会话、也不改上游行为**，只记下用户的选择并回一个
        `warning` —— 与其做一次「看起来切了临时对话、其实照样进历史」的假动作，
        不如让界面如实告诉用户「这个开关还不管用」。

        `extended_thinking` / `search_enabled` 是纯 per-turn 参数
        （每轮都写在请求体里），中途改不会造成不一致，**下一轮就生效**。
        """
        warning = ""
        if temporary is not None:
            new_temp = bool(temporary)
            if new_temp != self.cfg.chat.temporary:
                warning = (
                    "临时对话（不留痕）在 DeepSeek 侧尚未实现 —— "
                    "接口未查明，对话仍会写入账号历史。本次只记录选择，未改变行为。"
                )
            self.session.meta.temporary = new_temp
            self.cfg.chat.temporary = new_temp

        if extended_thinking is not None:
            self.cfg.chat.extended_thinking = bool(extended_thinking)

        if search_enabled is not None:
            self.cfg.chat.search_enabled = bool(search_enabled)

        # 思考过程是否显示。**纯本地显示开关**，下一轮生效，不改上游行为 ——
        # 「上游是否思考」由 `extended_thinking` 管，两者别混。
        if show_thinking is not None:
            self.cfg.chat.show_thinking = bool(show_thinking)

        self.cfg.save()
        out = self.chat_snapshot()
        out["rotated"] = ""
        if warning:
            out["warning"] = warning
        return out

    # ---------- 模型 ----------

    def available_models(self) -> list[dict]:
        """本账号可用模型。传输层没起来时是空列表。"""
        try:
            return [m.as_dict() for m in self.transport.available_models()]
        except Exception:  # noqa: BLE001 - 查询失败不该让界面崩
            return []

    async def set_model(self, name: str) -> str:
        """切换模型。返回生效后的模型名（空 = 账号默认）。

        只改配置、不重启运行时：模型是 per-chat 的，下一轮发送时自然生效。
        """
        target = (name or "").strip()
        if target:
            await self.transport.set_model(target)
        self.cfg.identity.model = target
        self.cfg.save()
        return target

    # ---------- 权限 ----------

    def permissions_snapshot(self) -> dict:
        """当前权限档位与 dry-run 状态。给界面回显用。

        `levels` 是**可选档位清单**，让界面不必硬编码三档名字 ——
        以后加档位只改这一处。
        """
        c = self.cfg.permissions.normalized()
        return {
            "level": c.level,
            "dry_run": c.dry_run,
            "levels": [
                {"id": permissions.READ_ONLY, "label": "只读"},
                {"id": permissions.WRITE, "label": "可写"},
                {"id": permissions.EXEC, "label": "可执行"},
            ],
        }

    async def set_permissions(
        self, *, level: str | None = None, dry_run: bool | None = None
    ) -> dict:
        """切换权限档位 / dry-run。只改配置，不重启运行时。

        **为什么不需要重启**：权限在 `_execute` 每次分发前实时读
        `self.cfg.permissions`，没有缓存的副本 —— 改完下一个工具调用立刻生效。
        这也是刻意不缓存的原因：缓存一份就得操心失效，而读一次 dataclass
        的开销可以忽略。

        非法档位在这里就收紧成 `read_only`（而不是留给 `normalized()`），
        并把实际生效值回给界面 —— 否则用户填了 `readonly` 看到成功回执，
        却不知道它被当成什么了。
        """
        # ⚠️ `PermissionConfig` 是 `frozen=True` 的 —— 不能改字段，只能整体换。
        # 这不是障碍：权限配置本就是「一整份快照」，换新对象比原地改更容易推理
        # （不存在「某处缓存了旧引用」的隐患）。
        old = self.cfg.permissions.normalized()
        warning = ""
        new_level = old.level
        if level is not None:
            raw = str(level).strip()
            norm = permissions.PermissionConfig(level=raw).normalized().level
            if norm != raw:
                warning = (
                    f"档位 `{raw}` 不是合法值（read_only / write / exec），"
                    f"已收紧为 `{norm}`。"
                )
            new_level = norm

        new_dry = old.dry_run if dry_run is None else bool(dry_run)
        self.cfg.permissions = permissions.PermissionConfig(
            level=new_level, dry_run=new_dry
        )

        self.cfg.save()
        out = self.permissions_snapshot()
        if warning:
            out["warning"] = warning
        return out
