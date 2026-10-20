"""会话列表边界自测（T-20）。

守护一条**实测会静默出错**的边界：`fetch_page` 的 `count > 100` 时服务端
返回**空列表 + code=0**，不是报错。若客户端不自己拦住，上层会把
「参数超限」读成「账号下没有会话」—— 功能坏了却看起来一切正常。

本测试**不联网**：只验证边界校验在**发出请求之前**就拦住了。

用法：
    cd engine && ./.venv/Scripts/python.exe -X utf8 tests/test_session_paging.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gad_engine.agent import AgentRuntime
from gad_engine.config import EngineConfig
from gad_engine.store import ConversationStore
from gad_engine.transport import (
    SESSION_LIST_MAX_COUNT,
    SESSION_LIST_MIN_COUNT,
    Transport,
    TransportError,
)
from gad_engine.transport.base import RemoteStatus, TurnOutcome

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    mark = "通过" if cond else "失败"
    extra = f" —— {detail}" if detail and not cond else ""
    print(f"  [{mark}] {name}{extra}")


class RecordingTransport(Transport):
    """只记录被请求的 count，不真联网。"""

    name = "recording"
    supports_streaming = True

    def __init__(self, sessions: list[dict]) -> None:
        self.sessions = sessions
        self.requested_counts: list[int] = []

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def health(self) -> RemoteStatus:
        return RemoteStatus(reachable=True, account_status=0, account_status_short="正常")

    async def send(self, prompt, session, **kwargs) -> TurnOutcome:
        return TurnOutcome(text="ok")

    async def list_sessions(self, count: int = 30) -> list[dict]:
        # 复刻真实实现的前置校验 —— 这样测试同时守护传输层与 agent 层。
        #
        # **记录放在校验之后**：这个列表的语义是「真的发出去的请求」，
        # 校验没过就不该留下痕迹。放在前面会让「越界请求一个都没发出去」
        # 这条断言永远失败 —— 而它恰恰是本测试最想守的那条。
        if count < SESSION_LIST_MIN_COUNT or count > SESSION_LIST_MAX_COUNT:
            raise TransportError(
                f"会话列表条数必须在 {SESSION_LIST_MIN_COUNT}–"
                f"{SESSION_LIST_MAX_COUNT} 之间（收到 {count}）",
                config=True,
            )
        self.requested_counts.append(count)
        return self.sessions[:count]


class StubExecutor:
    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def run(self, command: str):
        from types import SimpleNamespace

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
    cfg.prompt.template_file = str(tmp / "system-prompt.md")
    cfg.memory.file = str(tmp / "memory.md")
    return cfg


def build(tmp: Path, sessions: list[dict]):
    tmp.mkdir(parents=True, exist_ok=True)
    cfg = make_cfg(tmp)
    transport = RecordingTransport(sessions)
    rt = AgentRuntime(
        cfg,
        transport=transport,
        executor=StubExecutor(),
        store=ConversationStore(tmp / "conversations"),
    )
    return rt, transport


def fake_sessions(n: int) -> list[dict]:
    return [
        {"id": f"sid-{i:03d}", "title": f"会话 {i}", "updated_at": 1792306981.0 - i,
         "pinned": False}
        for i in range(n)
    ]


def main() -> int:
    print("=" * 70)
    print("会话列表边界（T-20）自测")
    print("=" * 70)

    with tempfile.TemporaryDirectory(prefix="gad-paging-") as td:
        base = Path(td)

        # ---------- 1. 常量本身 ----------
        print("\n[1] 边界常量")
        check("上限是 100（实测值）", SESSION_LIST_MAX_COUNT == 100,
              str(SESSION_LIST_MAX_COUNT))
        check("下限是 2（count=1 实测返回 0）", SESSION_LIST_MIN_COUNT == 2,
              str(SESSION_LIST_MIN_COUNT))

        # ---------- 2. 传输层拒绝越界 ----------
        print("\n[2] 传输层 · 越界必须在发请求之前拦住")
        _rt, tr = build(base / "reject", fake_sessions(50))

        async def probe(count: int) -> str:
            try:
                await tr.list_sessions(count)
                return ""
            except TransportError as exc:
                return str(exc)

        for bad in (0, 1, 101, 200, 500):
            msg = asyncio.run(probe(bad))
            check(f"count={bad} 抛 TransportError", bool(msg), "未抛错")

        check("越界请求一个都没真的发出去", tr.requested_counts == [],
              str(tr.requested_counts))

        # ---------- 3. 合法范围正常 ----------
        print("\n[3] 合法范围正常放行")
        for good in (2, 30, 100):
            got = asyncio.run(tr.list_sessions(good))
            check(f"count={good} 正常返回", isinstance(got, list), str(type(got)))

        # ---------- 4. Agent 层夹住而不是报错 ----------
        print("\n[4] Agent 层 · 超限时夹住并如实报告")
        rt2, tr2 = build(base / "clamp", fake_sessions(74))

        res = asyncio.run(rt2.list_remote_sessions(500))
        check("没有返回错误", res.get("error") == "", str(res.get("error")))
        check("实际请求被夹到 100", tr2.requested_counts == [100],
              str(tr2.requested_counts))
        check("requested 如实回显 500", res.get("requested") == 500,
              str(res.get("requested")))
        check("effective_count 是 100", res.get("effective_count") == 100,
              str(res.get("effective_count")))
        check("truncated 标记为真", res.get("truncated") is True,
              str(res.get("truncated")))
        check("拿到了 74 条", len(res.get("items") or []) == 74,
              str(len(res.get("items") or [])))

        # ---------- 5. 未超限时不谎报 truncated ----------
        print("\n[5] 未超限 · 不得谎报 truncated")
        rt3, _tr3 = build(base / "no-trunc", fake_sessions(30))
        res3 = asyncio.run(rt3.list_remote_sessions(30))
        check("truncated 为假", res3.get("truncated") is False,
              str(res3.get("truncated")))
        check("effective_count 等于请求值", res3.get("effective_count") == 30,
              str(res3.get("effective_count")))

        # ---------- 6. 下限也夹住 ----------
        print("\n[6] 下限 · count=1 会被夹到 2（服务端对 1 返回 0 条）")
        rt4, tr4 = build(base / "min", fake_sessions(10))
        asyncio.run(rt4.list_remote_sessions(1))
        check("count=1 被夹到 2", tr4.requested_counts == [2],
              str(tr4.requested_counts))

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
