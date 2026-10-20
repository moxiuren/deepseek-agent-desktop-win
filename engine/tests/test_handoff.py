"""接力换会话（`handoff_conversation`）全流程自测。

为什么单独钉这一条
------------------

「接力新会话」是本项目**唯一**的换会话方式（自动轮换默认已关）。它一旦
悄悄坏掉，用户的感受就是「点了按钮，然后模型什么都不记得了」—— 而界面上
看不出任何异常。这类「静默失忆」正是整个项目最想避免的失败形态。

所以这里把每个关键不变量都钉住：
  * 摘要确实生成、确实落库、确实注入到新会话的提示词里
  * 旧对话**原样保留**（压缩不可逆，原文必须可查）
  * 新会话确实是**全新的会话树**（`cid` 为空），否则等于没换
  * 模型摘要失败时降级到本地摘要，而不是整件事失败
  * 摘要请求本身**不进本地消息**（它是引擎内部动作，不是用户说的话）

用法：
    cd engine && ./.venv/Scripts/python.exe tests/test_handoff.py
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
from gad_engine.compact import SUMMARY_PROMPT
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


class ScriptedTransport(Transport):
    """假传输层：能把「摘要请求」与「普通请求」分开应答。

    刻意**照约定实现**（只补锚点、不碰 turns），这样 `handoff_conversation`
    里的记账逻辑与真实路径一致。
    """

    name = "scripted"
    supports_streaming = True

    def __init__(self, summary_text: str = "", fail: bool = False) -> None:
        self.summary_text = summary_text
        self.fail = fail
        self.prompts: list[str] = []
        self.n = 0

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def health(self) -> RemoteStatus:
        return RemoteStatus(reachable=True)

    async def send(self, prompt, session, **kwargs) -> TurnOutcome:
        self.prompts.append(prompt)
        self.n += 1
        if session.cid == "":
            session.cid = "cid-new"          # 首轮：服务端建会话
        if self.fail:
            return TurnOutcome(error="Server busy", cid=session.cid)
        session.rid = str(self.n)
        session.rcid = str(self.n)
        if prompt == SUMMARY_PROMPT:
            return TurnOutcome(
                text=self.summary_text, cid=session.cid,
                rid=str(self.n), rcid=str(self.n),
            )
        return TurnOutcome(
            text=f"回答第 {self.n} 轮", cid=session.cid,
            rid=str(self.n), rcid=str(self.n),
        )


def make_cfg(tmp: Path) -> EngineConfig:
    cfg = EngineConfig()
    cfg.identity.account_label = "测试账号"
    # 节奏闸门全部放开。**不是为了快** —— 第 7 段要跑 30 轮，而真实的
    # 「突发预算（max_burst=2 → 保持 12 秒）+ 每 6 轮阅读停顿 4~9 秒」
    # 会让它跑两分钟以上（实测 2m11s）。这里测的是会话轮换语义，
    # 不是节奏，所以把闸门让开。
    cfg.pacing.base_gap_ms = 0.0
    cfg.pacing.floor_gap_ms = 0.0
    cfg.pacing.max_gap_ms = 0.0
    cfg.pacing.max_burst = 10_000
    cfg.pacing.hold_after_burst_ms = 0
    cfg.pacing.read_pause_every = 0
    cfg.pacing.active_start_hour = 0
    cfg.pacing.active_end_hour = 24
    # ★ 单小时硬顶（`hourly_max`，默认 20）**必须一起放开**。
    #
    # 2026-10-17 实测踩到：第 7 段要跑 30 + 26 轮，而 `hourly_max` 默认 20 ——
    # 第 21 轮起 `evaluate()` 会返回「等窗口滑出」≈ **1 小时**，而 `rt.run()`
    # 会老老实实等。结果是**整套测试卡在 test_handoff 上 19 分钟不动**，
    # 而且不报错（挂起比失败更难查）。
    #
    # 教训：给闸门加**新的默认约束**时，必须同步检查「有没有测试靠旧默认值
    # 才跑得通」。这里测的是接力语义，不是节奏，所以整层让开。
    cfg.pacing.hourly_max = 0
    cfg.pacing.long_pause_every = 0
    # 熔断层也放开：第 4 段刻意让上游返回 `Server busy`，那会让熔断层进入
    # **30 分钟**退避；而 `handoff_conversation` 里的闸门等待（20 秒预算）
    # 会老老实实等满，把测试拖慢 20 秒。这里测的是降级逻辑，不是退避策略。
    cfg.circuit.busy_backoff_ms = 0
    cfg.circuit.busy_cooldown_ms = 0
    cfg.prompt.template_file = str(tmp / "system-prompt.md")
    cfg.memory.file = str(tmp / "memory.md")
    return cfg


def make_rt(tmp: Path, transport: Transport | None = None) -> AgentRuntime:
    return AgentRuntime(
        make_cfg(tmp),
        transport=transport or ScriptedTransport(),
        store=ConversationStore(tmp / "conversations"),
    )


async def scenario(tmp: Path) -> None:
    # ---------- 1. 接力全流程（local 模式）----------
    print("\n[1] 接力：摘要生成 → 新会话 → 旧对话保留")
    rt = make_rt(tmp / "a")
    await rt.run("帮我写个脚本")
    await rt.run("再加个参数")
    old_id = rt.conversation.id
    old_msgs = len(rt.conversation.messages)
    old_cid = rt.conversation.session.cid
    check("旧对话有服务端会话", bool(old_cid), old_cid)

    res = await rt.handoff_conversation(mode="local")
    check("接力成功", res.get("ok") is True, str(res))
    check("模式为 local", res.get("mode") == "local", str(res.get("mode")))
    check("产出了摘要", res.get("chars", 0) > 0, str(res.get("chars")))

    new_conv = rt.conversation
    check("换了新对话", new_conv.id != old_id, new_conv.id)
    check("新对话带摘要", bool(new_conv.handoff_summary.strip()), new_conv.handoff_summary[:80])
    check("摘要溯源到旧对话", new_conv.handoff_from == old_id, new_conv.handoff_from)
    check("新对话消息是空的", len(new_conv.messages) == 0, str(len(new_conv.messages)))
    check("标题带（续）", new_conv.title.endswith("（续）"), new_conv.title)
    # **最关键的一条**：新对话必须挂一个全新的空 SessionMeta。
    # 若它继承了旧 cid，第一条消息就会接进旧会话树 —— 等于没换。
    check("新会话是全新的会话树", new_conv.session.cid == "", new_conv.session.cid)
    check("新会话锚点为空", new_conv.session.rid == "", new_conv.session.rid)

    olds = [c for c in rt.list_conversations() if c["id"] == old_id]
    check("旧对话仍在列表里", len(olds) == 1, str(len(olds)))
    check(
        "旧对话消息没被删",
        bool(olds) and olds[0]["message_count"] == old_msgs,
        f"{olds[0]['message_count'] if olds else '?'} vs {old_msgs}",
    )
    check("旧对话不被标记为带摘要", bool(olds) and not olds[0]["has_handoff"])
    check("新对话被标记为带摘要", any(c["has_handoff"] for c in rt.list_conversations()))

    # ---------- 2. 摘要确实注入到新会话的提示词 ----------
    print("\n[2] 注入：新会话每一轮都带摘要，旧对话不带")
    probe = new_conv.handoff_summary[:24]
    p1 = rt._bootstrap_prompt("继续")
    check("首轮提示词含摘要", probe in p1, p1[-260:])
    check("提示词标明「上一段对话的摘要」", "上一段对话的摘要" in p1)
    p2 = rt._bootstrap_prompt("第二轮任务")
    check("第二轮也带（每轮现算）", probe in p2, p2[-200:])

    rt.load_conversation(old_id)
    p_old = rt._bootstrap_prompt("回到旧对话")
    check("切回旧对话不再带摘要", "上一段对话的摘要" not in p_old)

    # ---------- 3. model 模式：走模型摘要 ----------
    print("\n[3] model 模式：摘要来自模型，且请求不进本地消息")
    tr = ScriptedTransport(
        summary_text="【目标】\n把脚本写好\n\n【还没解决的问题】\n（无）"
    )
    rt2 = make_rt(tmp / "b", transport=tr)
    await rt2.run("写个脚本")
    res2 = await rt2.handoff_conversation(mode="model")
    check("接力成功", res2.get("ok") is True, str(res2))
    check("模式为 model", res2.get("mode") == "model", str(res2.get("mode")))
    check("摘要内容来自模型", "把脚本写好" in str(res2.get("summary", "")), str(res2.get("summary")))
    check("确实发出了摘要请求", SUMMARY_PROMPT in tr.prompts)
    check(
        "摘要请求没进本地消息",
        all(m.text != SUMMARY_PROMPT for m in rt2.conversation.messages),
        "摘要是引擎内部动作，不该出现在用户的对话记录里",
    )
    # 摘要请求发的是**旧会话**（cid 有值），不是新建一条 —— 否则账号里会
    # 平白多出一条只含「请总结」的垃圾会话。
    check("摘要请求用的是旧会话锚点", len(tr.prompts) == 2, str(len(tr.prompts)))

    # ---------- 4. model 失败 → 降级 local ----------
    print("\n[4] 降级：模型摘要拿不到，接力仍要成功")
    rt3 = make_rt(tmp / "c", transport=ScriptedTransport(fail=True))
    await rt3.run("你好")
    res3 = await rt3.handoff_conversation(mode="model")
    check("仍然接力成功", res3.get("ok") is True, str(res3))
    check("降级到 local", res3.get("mode") == "local", str(res3.get("mode")))
    check("给出了降级说明", bool(res3.get("note")), str(res3.get("note")))

    # ---------- 5. 拒绝的两种情形 ----------
    print("\n[5] 拒绝：空对话 / 压缩关闭")
    rt4 = make_rt(tmp / "d")
    res4 = await rt4.handoff_conversation()
    check(
        "空对话拒绝接力",
        res4.get("ok") is False and "没有内容" in str(res4.get("reason")),
        str(res4),
    )

    rt5 = make_rt(tmp / "e")
    rt5.cfg.compact.enabled = False
    await rt5.run("说点什么")
    res5 = await rt5.handoff_conversation()
    check(
        "压缩关闭时拒绝",
        res5.get("ok") is False and "关闭" in str(res5.get("reason")),
        str(res5),
    )

    # ---------- 6. 多代接力不丢前文 ----------
    print("\n[6] 多代接力：第二代要记得第一代")
    rt6 = make_rt(tmp / "f")
    await rt6.run("第一代目标：重构 engine 模块")
    r1 = await rt6.handoff_conversation(mode="local")
    check("第一代接力成功", r1.get("ok") is True, str(r1))
    await rt6.run("第二代继续")
    r2 = await rt6.handoff_conversation(mode="local")
    check("第二代接力成功", r2.get("ok") is True, str(r2))
    check("第二代摘要含「更早的背景」", "更早的背景" in str(r2.get("summary")), str(r2.get("summary"))[:200])
    check("第一代的目标被带过来", "重构 engine 模块" in str(r2.get("summary")), str(r2.get("summary"))[:300])
    check("标题不叠加（续）", not rt6.conversation.title.endswith("（续）（续）"), rt6.conversation.title)

    # ---------- 7. 默认不自动轮换（「把选择交给用户」）----------
    print("\n[7] 自动轮换默认关闭：聊很久也不会被悄悄换掉")
    rt7 = make_rt(tmp / "g")
    check("配置默认 auto_rotate=False", rt7.cfg.pacing.auto_rotate is False)
    check("SessionManager 同步了这一项", rt7.session.auto_rotate is False)
    first_cid = ""
    for i in range(30):
        await rt7.run(f"第 {i} 句")
        if i == 0:
            first_cid = rt7.session.meta.cid
    check("30 轮后仍未轮换", rt7.gates.rotate_if_needed() is None)
    check("会话 id 始终没变", rt7.session.meta.cid == first_cid, rt7.session.meta.cid)
    check("轮次确实累加了", rt7.session.meta.turns == 30, str(rt7.session.meta.turns))

    # 打开开关后，自动轮换要恢复（配置项不是死的）
    rt8 = make_rt(tmp / "h")
    rt8.cfg.pacing.auto_rotate = True
    rt8.session.auto_rotate = True
    check("打开前没有轮换记录", not rt8.session.snapshot()["rotations"])
    for _ in range(26):          # 默认上限 25 轮
        await rt8.run("x")
    # 注意：轮换发生在 `run()` 的**轮首**（`rotate_if_needed`），所以跑完之后
    # `should_rotate()` 已经是 False（新会话只跑了 1 轮）—— 不能拿它断言。
    # 要看**轮换日志**。
    rotations = rt8.session.snapshot()["rotations"]
    check("打开开关后按轮数轮换", bool(rotations), str(rotations))


def main() -> int:
    print("=" * 70)
    print("接力换会话 · 全流程自测")
    print("=" * 70)
    with tempfile.TemporaryDirectory(prefix="gad-handoff-") as tmp:
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
