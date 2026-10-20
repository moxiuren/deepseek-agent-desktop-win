"""轮次计数（`session.turns`）的归属自测。

为什么单独钉这一条
------------------
`turns` 是 `max_turns_per_session` 的**唯一输入**，而它一度被**两个地方各加一次**：
传输层 `deepseek_rpc._stream_completion` 与编排层 `SessionManager.update_from_response`
都作用在**同一个 `SessionMeta` 对象**上。

2026-10-15 实测：发 1 轮，`turns` 变成 **2**。后果是配置写 25 轮、实际约 **12 轮**
就触发轮换 —— 而轮换会新建服务端会话、把上下文悄悄换掉（界面看不出任何变化）。
这类「数字对不上、又没人会去核对」的偏差正是本项目最想避免的。

约定：**轮次计数的唯一归属方是编排层**。它才知道「一个用户轮次包含几次往返」；
传输层只负责把 `rid` / `rcid` / `last_used_at` 这几个**幂等**的锚点补回去。

用法：
    cd engine && ./.venv/Scripts/python.exe tests/test_session_turns.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gad_engine.agent import AgentRuntime
from gad_engine.config import EngineConfig
from gad_engine.store import ConversationStore
from gad_engine.transport.base import (
    RemoteStatus,
    Transport,
    TurnOutcome,
)

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    mark = "通过" if cond else "失败"
    extra = f" —— {detail}" if detail and not cond else ""
    print(f"  [{mark}] {name}{extra}")


class FaithfulTransport(Transport):
    """照**约定**来的假传输层：只补 `rid` / `rcid`，**不碰 `turns`**。

    这正是修复后 `deepseek_rpc.py` 的行为 —— 用它来验证「一轮就是一轮」。
    """

    name = "faithful"
    supports_streaming = True

    def __init__(self) -> None:
        self.n = 0

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def health(self) -> RemoteStatus:
        return RemoteStatus(reachable=True)

    async def send(self, prompt, session, **kwargs) -> TurnOutcome:
        self.n += 1
        # 只补锚点（幂等），计数交给编排层。
        session.rid = str(self.n)
        session.rcid = str(self.n)
        return TurnOutcome(
            text=f"回复{self.n}", cid="cid-1", rid=str(self.n), rcid=str(self.n)
        )


def make_cfg(tmp: Path, max_turns: int = 25) -> EngineConfig:
    cfg = EngineConfig()
    cfg.identity.account_label = "测试账号"
    cfg.pacing.base_gap_ms = 0.0
    cfg.pacing.floor_gap_ms = 0.0
    cfg.pacing.active_start_hour = 0
    cfg.pacing.active_end_hour = 24
    # 单小时硬顶（默认 20）一并让开：默认值下多轮场景会变成「等窗口滑出 ≈1 小时」。
    cfg.pacing.hourly_max = 0
    cfg.pacing.max_turns_per_session = max_turns
    # 本文件测的是**自动轮换的阈值**，所以必须显式把它打开 ——
    # 默认已改成 False（「什么时候换会话」交给用户，见 `PacingConfig.auto_rotate`）。
    # 不打开的话 `rotate_if_needed()` 恒为 None，下面第 2 段会整段失效。
    cfg.pacing.auto_rotate = True
    cfg.prompt.template_file = str(tmp / "system-prompt.md")
    cfg.memory.file = str(tmp / "memory.md")
    return cfg


async def scenario(tmp: Path) -> None:
    # ---------- 1. 一轮就是一轮 ----------
    print("\n[1] 计数归属：一轮只加 1")
    rt = AgentRuntime(
        make_cfg(tmp / "a"),
        transport=FaithfulTransport(),
        store=ConversationStore(tmp / "a" / "conversations"),
    )
    await rt.run("第一句")
    check("发 1 轮 → turns=1", rt.session.meta.turns == 1, str(rt.session.meta.turns))

    for i in range(4):
        await rt.run(f"第{i + 2}句")
    check("发 5 轮 → turns=5", rt.session.meta.turns == 5, str(rt.session.meta.turns))

    # ---------- 2. 轮换恰好在配置的上限触发 ----------
    print("\n[2] 轮换阈值：到 3 轮才轮换，不是到一半就轮换")
    rt2 = AgentRuntime(
        make_cfg(tmp / "b", max_turns=3),
        transport=FaithfulTransport(),
        store=ConversationStore(tmp / "b" / "conversations"),
    )
    await rt2.run("a")
    await rt2.run("b")
    check("2 轮时 turns=2", rt2.session.meta.turns == 2, str(rt2.session.meta.turns))
    check("未到上限不轮换", rt2.gates.rotate_if_needed() is None)
    await rt2.run("c")
    check("3 轮时 turns=3", rt2.session.meta.turns == 3, str(rt2.session.meta.turns))
    rotated = rt2.gates.rotate_if_needed()
    check("到上限才轮换", rotated is not None, str(rotated))

    # ---------- 3. 源码守卫：传输层不得再碰 turns ----------
    #
    # 行为测试挡不住「有人把自增又加回传输层」—— 因为假传输层是测试代码，
    # 真实传输层的回归它看不见。所以这里直接扫源码。
    print("\n[3] 源码守卫：传输层不再对 turns 计数")
    src = (ROOT / "gad_engine" / "transport" / "deepseek_rpc.py").read_text(
        encoding="utf-8"
    )
    check(
        "deepseek_rpc 里没有对 turns 的自增",
        "session.turns += 1" not in src and "session.turns+=1" not in src,
        "传输层又在对 turns 计数了 —— 会与编排层重复累加",
    )


def main() -> int:
    print("=" * 70)
    print("会话层 · 轮次计数归属自测")
    print("=" * 70)
    with tempfile.TemporaryDirectory(prefix="gad-turns-") as tmp:
        asyncio.run(scenario(Path(tmp)))

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
