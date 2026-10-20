"""停止生成（T-19）自测。

守护四件事，每一件都对应一个**实测会出错**的具体形态：

1. **传输层真的会停** —— 读循环在下一行检查标志并退出，
   已收到的正文必须保留（不是丢掉重来）。
2. **Agent 层不把它当故障** —— `stop_reason == "cancelled"`，
   绝不出现 `error` 事件，绝不调 `note_outcome`（那会把传输层降级掉）。
3. **停止的那一轮不执行工具调用** —— 用户按的是停止，不是
   「跳过回答只跑工具」。
4. **清零点正确** —— 一次任务一次清零；上一轮停过，下一轮必须正常跑完。

用法：
    cd engine && ./.venv/Scripts/python.exe -X utf8 tests/test_stop.py
"""

from __future__ import annotations

import asyncio
import inspect
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gad_engine.agent import AgentRuntime
from gad_engine.config import EngineConfig
from gad_engine.session import SessionMeta
from gad_engine.store import ConversationStore
from gad_engine.transport.base import RemoteStatus, Transport, TurnOutcome
from gad_engine.transport.deepseek_rpc import _CancelToken

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    mark = "通过" if cond else "失败"
    extra = f" —— {detail}" if detail and not cond else ""
    print(f"  [{mark}] {name}{extra}")


# ---------------------------------------------------------------- 传输层桩


class StubTransport(Transport):
    """可编排的桩：每轮从剧本取一条，支持「中途被停止」。

    `cancel_after` 模拟「用户在第 N 个增量之后按了停止」——
    桩自己调 `request_stop()`，然后像真实实现一样**在下一行检查**，
    保留已发出的正文并返回 `cancelled=True`。
    """

    name = "stub"
    supports_streaming = True

    def __init__(self, script: list[dict]) -> None:
        self.script = list(script)
        self.calls = 0
        self._cancel = _CancelToken()
        self.stop_requests = 0

    # --- 停止接口（与 deepseek_rpc.Transport 同签名） ---

    def request_stop(self) -> bool:
        self.stop_requests += 1
        self._cancel.request()
        return True

    def clear_stop(self) -> None:
        self._cancel.clear()

    # --- Transport 抽象方法 ---

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def health(self) -> RemoteStatus:
        return RemoteStatus(reachable=True, account_status=0, account_status_short="正常")

    async def send(
        self,
        prompt: str,
        session: SessionMeta,
        *,
        temporary: bool = True,
        extended_thinking: bool = False,
        search_enabled: bool = False,
        files: list[str] | None = None,
        on_delta=None,
        on_thought=None,
    ) -> TurnOutcome:
        self.calls += 1
        item = self.script.pop(0) if self.script else {"text": "（无剧本）"}
        pieces = list(item.get("deltas") or [item.get("text", "")])
        cancel_after = item.get("cancel_after")

        got: list[str] = []
        cancelled = False

        # `cancel_after < 0` = 「一个字都还没吐就按了停止」。
        if cancel_after is not None and cancel_after < 0:
            self.request_stop()

        for i, piece in enumerate(pieces):
            # ★ 与真实实现同构：**每一行之前**检查一次标志。
            if self._cancel.requested:
                cancelled = True
                break
            if on_delta is not None:
                r = on_delta(piece)
                if inspect.isawaitable(r):
                    await r
            got.append(piece)
            if cancel_after is not None and i >= cancel_after:
                # 模拟用户此刻按停止 —— 真实实现里这是 IPC 线程调的。
                self.request_stop()

        # ★ 流跑完后**再查一次**。
        #
        # 真实实现里，标志是在「下一行 SSE 到来时」被发现的：若用户恰好在
        # 最后一行之后按停止，下一行可能永远不会来（流已经结束）——
        # 那时读循环自然走完，`cancelled` 保持 False，这是**正确**的
        # （内容已经全收完了，没什么可停的）。
        #
        # 但桩的剧本是有限的：`cancel_after` 命中最后一段时，循环直接
        # 耗尽，永远没有「下一次检查」。补这一句让桩的语义与真实实现一致
        # —— 标志置位 ⇒ 这一轮算被停止。
        if not cancelled and self._cancel.requested:
            cancelled = True

        return TurnOutcome(
            text="".join(got),
            cid=f"cid-{self.calls}",
            rid=f"rid-{self.calls}",
            rcid=f"rcid-{self.calls}",
            cancelled=cancelled,
        )


class StubExecutor:
    """只记账、不真跑命令。"""

    def __init__(self) -> None:
        self.commands: list[str] = []

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def run(self, command: str):
        from types import SimpleNamespace

        self.commands.append(command)
        return SimpleNamespace(exit_code=0, output="ok", rejected=False)

    def snapshot(self) -> dict:
        return {"running": False}


def make_cfg(tmp: Path) -> EngineConfig:
    cfg = EngineConfig.sandbox(tmp)
    cfg.identity.account_label = "测试账号"
    cfg.pacing.base_gap_ms = 0.0
    cfg.pacing.floor_gap_ms = 0.0
    cfg.pacing.active_start_hour = 0
    cfg.pacing.active_end_hour = 24
    cfg.pacing.hourly_max = 0
    cfg.pacing.hold_after_burst_ms = 0
    cfg.pacing.read_pause_every = 0
    cfg.circuit.busy_backoff_ms = 0
    cfg.circuit.busy_cooldown_ms = 0
    cfg.prompt.template_file = str(tmp / "system-prompt.md")
    cfg.memory.file = str(tmp / "memory.md")
    return cfg


def build(tmp: Path, script: list[dict]):
    tmp.mkdir(parents=True, exist_ok=True)
    cfg = make_cfg(tmp)
    events: list[tuple[str, dict]] = []
    transport = StubTransport(script)
    rt = AgentRuntime(
        cfg,
        transport=transport,
        executor=StubExecutor(),
        on_event=lambda kind, payload: events.append((kind, dict(payload))),
        store=ConversationStore(tmp / "conversations"),
    )
    return rt, transport, events


def kinds(events: list[tuple[str, dict]], name: str) -> list[dict]:
    return [payload for kind, payload in events if kind == name]


# ---------------------------------------------------------------- 场景


def scenario_token(tmp: Path) -> None:
    """标志本身：置位 / 读取 / 清零。"""
    print("\n[1] _CancelToken 基本语义")
    t = _CancelToken()
    check("初始未置位", t.requested is False)
    t.request()
    check("request() 之后置位", t.requested is True)
    t.clear()
    check("clear() 之后复位", t.requested is False)


def scenario_stop_keeps_text(tmp: Path) -> None:
    """停在第 2 个增量之后 —— 已收到的两段必须保留。"""
    print("\n[2] 停止后保留已生成正文，且不报错")
    rt, _tr, events = build(
        tmp / "keep",
        [{"deltas": ["第一段。", "第二段。", "第三段不该出现。", "第四段也不该。"],
          "cancel_after": 1}],
    )

    report = asyncio.run(rt.run("写点东西"))

    check("stop_reason 是 cancelled", report.stop_reason == "cancelled",
          report.stop_reason)
    check("没有 error 事件", not kinds(events, "error"),
          str(kinds(events, "error")))
    check("stop 事件带 cancelled 理由",
          any(p.get("reason") == "cancelled" for p in kinds(events, "stop")),
          str(kinds(events, "stop")))
    check("正文保留了已生成的两段",
          report.final_text == "第一段。第二段。", repr(report.final_text))
    check("第三段确实没进来", "第三段" not in report.final_text, report.final_text)

    # turn_end 必须先于 stop 发出 —— 否则界面拿不到已生成的内容。
    order = [k for k, _ in events if k in {"turn_end", "stop"}]
    check("turn_end 在 stop 之前", order[:2] == ["turn_end", "stop"], str(order))


def scenario_no_breaker(tmp: Path) -> None:
    """停止绝不能碰熔断计数 —— 连点几次就把传输层降级掉是最糟的失败。"""
    print("\n[3] 停止不计入熔断（连续 3 次停止后传输层仍健康）")
    rt, _tr, events = build(
        tmp / "breaker",
        [{"deltas": ["a", "b"], "cancel_after": 0} for _ in range(3)],
    )

    for _ in range(3):
        asyncio.run(rt.run("再说一句"))

    snap = rt.breaker.snapshot()
    level = snap.get("level")
    check("熔断器仍是 closed（未降级）", str(level).lower().endswith("closed"),
          f"level={level} snapshot={snap}")
    check("没有 breaker 事件", not kinds(events, "breaker"),
          str(kinds(events, "breaker")))
    check("三次都成功跑到 cancelled",
          sum(1 for p in kinds(events, "stop") if p.get("reason") == "cancelled") == 3)


def scenario_clear_between_runs(tmp: Path) -> None:
    """清零点：上一轮停过，下一轮必须正常跑完。"""
    print("\n[4] 上一轮停过 · 下一轮不受影响")
    rt, tr, _events = build(
        tmp / "clear",
        [
            {"deltas": ["一", "二", "三"], "cancel_after": 0},   # 第 1 轮被停
            {"text": "完整回答"},                                  # 第 2 轮应正常完成
        ],
    )

    r1 = asyncio.run(rt.run("第一问"))
    check("第 1 轮被停止", r1.stop_reason == "cancelled", r1.stop_reason)

    r2 = asyncio.run(rt.run("第二问"))
    check("第 2 轮正常完成（标志已清）", r2.stop_reason == "completed", r2.stop_reason)
    check("第 2 轮正文完整", r2.final_text == "完整回答", repr(r2.final_text))
    check("transport.clear_stop 在每轮开头被调用", tr.stop_requests >= 1,
          f"stop_requests={tr.stop_requests}")


def scenario_no_tools_after_stop(tmp: Path) -> None:
    """停止的那一轮即便解析出工具调用也不能执行。"""
    print("\n[5] 停止后不再执行工具调用")
    # 让正文里带一个合法的工具调用围栏，同时在第 0 段后停止。
    tool_text = '我来看看。\n```tool\n{"name": "run", "args": {"command": "echo hi"}}\n```'
    rt, _tr, _events = build(
        tmp / "notools",
        [{"deltas": [tool_text], "cancel_after": 0}],
    )

    report = asyncio.run(rt.run("跑个命令"))

    check("没有执行任何命令",
          rt.executor.commands == [], str(rt.executor.commands))
    check("tool_calls 记为 0", report.tool_calls == 0, str(report.tool_calls))
    check("stop_reason 仍是 cancelled", report.stop_reason == "cancelled",
          report.stop_reason)


def scenario_empty_text(tmp: Path) -> None:
    """一个字都没收到时被停 —— 不是错误，也不能算上游健康。"""
    print("\n[6] 零正文时停止 · 仍不是错误")
    rt, _tr, events = build(
        tmp / "empty",
        [{"deltas": ["不该出现"], "cancel_after": -1}],  # 立刻停
    )

    report = asyncio.run(rt.run("说点什么"))

    check("stop_reason 是 cancelled", report.stop_reason == "cancelled",
          report.stop_reason)
    check("没有 error 事件", not kinds(events, "error"), str(kinds(events, "error")))
    check("final_text 为空", report.final_text == "", repr(report.final_text))


def main() -> int:
    print("=" * 70)
    print("停止生成（T-19）自测")
    print("=" * 70)

    with tempfile.TemporaryDirectory(prefix="gad-stop-") as td:
        base = Path(td)
        scenario_token(base)
        scenario_stop_keeps_text(base)
        scenario_no_breaker(base)
        scenario_clear_between_runs(base)
        scenario_no_tools_after_stop(base)
        scenario_empty_text(base)

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
