"""思考过程（`THINK` 片段）输出链路自测。

为什么单独钉这一条
------------------

2026-10-15 用户报的缺口是「目前 deepseek 思考的文字没有输出出来」。查下来
**不是丢了，是收到了但被刻意丢掉**：

    SSE `THINK` 片段 → `_Acc.thought_text()` → `TurnOutcome.thoughts`
      → `agent.py on_thought`（只报「已 N 字 + 最近 120 字」）
      → 外壳 `ShowThinking`（只画一行心跳）

也就是说 `outcome.thoughts` 一直带着思考原文回来，只是没有任何一层把它送到
界面上，`Message.thoughts` 字段也一直是空的。这个文件钉的就是**从引擎出去
的那一段**：`thought` 事件、`turn_end.thoughts`、以及入库。

外壳怎么画（可折叠块）不在本文件范围内 —— 这里只保证「引擎把原文交出去了」。

用法：
    cd engine && ./.venv/Scripts/python.exe tests/test_thinking.py
"""

from __future__ import annotations

import asyncio
import inspect
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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


class FakeTransport(Transport):
    """按剧本回话：每轮给定「思考片段列表 + 正文」。

    `thoughts` 里每一段都会喂给 `on_thought` —— 模拟上游「先流思考再流正文」，
    这是思考模式最真实的事件序列。
    """

    name = "fake"
    supports_streaming = True

    def __init__(self, script: list[dict]) -> None:
        self.script = list(script)
        self.calls = 0

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def health(self) -> RemoteStatus:
        return RemoteStatus(reachable=True, account_status=0, account_status_short="正常")

    async def send(
        self,
        prompt,
        session,
        *,
        temporary: bool = True,
        extended_thinking: bool = False,
        search_enabled: bool = False,
        files=None,
        on_delta=None,
        on_thought=None,
    ) -> TurnOutcome:
        self.calls += 1
        item = self.script.pop(0) if self.script else {"text": "（无剧本）", "thoughts": []}
        delay = float(item.get("delay") or 0.0)
        if delay:
            await asyncio.sleep(delay)
        pieces = list(item.get("thoughts") or [])
        # `silent=True` 模拟「整段思考只在快照帧里出现 / 走 SET 整体替换」——
        # 这两种帧**不会调用 `on_thought`**（见 `_SseParser._snapshot` / `_delta`），
        # 但 `TurnOutcome.thoughts` 照样带着全文。
        if not item.get("silent"):
            for piece in pieces:
                if on_thought is not None:
                    on_thought(piece)
        text = item.get("text", "")
        if text and on_delta is not None:
            result = on_delta(text)
            if inspect.isawaitable(result):
                await result
        return TurnOutcome(
            text=text,
            thoughts="".join(pieces),
            cid=f"cid-{self.calls}",
            rid=f"rid-{self.calls}",
            rcid=f"rcid-{self.calls}",
        )


class StubExecutor:
    """只记账、不真跑命令 —— 多轮场景（工具调用）用它避免副作用。"""

    def __init__(self) -> None:
        self.commands: list[str] = []

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def run(self, command: str) -> SimpleNamespace:
        self.commands.append(command)
        return SimpleNamespace(exit_code=0, output="ok", rejected=False)

    def snapshot(self) -> dict:
        return {"running": False}


def make_cfg(tmp: Path) -> EngineConfig:
    # 用 `sandbox()` 而不是裸 `EngineConfig()`：`scenario_toggle` 会调
    # `rt.set_chat()` 验证「开关能中途改」，而 `set_chat` 内部有一句
    # `self.cfg.save()` —— 裸构造的 cfg 没有来源路径，那一句就会落到用户的
    # **真实 config.json**。2026-10-16 实测踩到：把 `<临时目录>/toggle/`
    # 写进了用户配置，而临时目录随即被删，用户侧表现是「提示词与记忆都读不到」。
    cfg = EngineConfig.sandbox(tmp)
    # L1 闸门要求声明专用账号标识，否则一切提交被拦（刻意的设计）。
    cfg.identity.account_label = "测试账号"
    # 关掉节奏等待，让测试秒级跑完 —— 间隔闸门本身由 test_gates 覆盖。
    cfg.pacing.base_gap_ms = 0.0
    cfg.pacing.floor_gap_ms = 0.0
    cfg.pacing.active_start_hour = 0
    cfg.pacing.active_end_hour = 24
    # 单小时硬顶（默认 20）一并让开：默认值下多轮场景会变成「等窗口滑出 ≈1 小时」。
    cfg.pacing.hourly_max = 0
    cfg.pacing.hold_after_burst_ms = 0
    cfg.pacing.read_pause_every = 0
    cfg.circuit.busy_backoff_ms = 0
    cfg.circuit.busy_cooldown_ms = 0
    # 隔离磁盘路径：默认模板与记忆都落在真实 AppData 里，测试绝不能碰。
    cfg.prompt.template_file = str(tmp / "system-prompt.md")
    cfg.memory.file = str(tmp / "memory.md")
    return cfg


def build(tmp: Path, script: list[dict], *, show_thinking: bool = True):
    cfg = make_cfg(tmp)
    cfg.chat.show_thinking = show_thinking
    events: list[tuple[str, dict]] = []
    rt = AgentRuntime(
        cfg,
        transport=FakeTransport(script),
        executor=StubExecutor(),
        on_event=lambda kind, payload: events.append((kind, dict(payload))),
        store=ConversationStore(tmp / "conversations"),
    )
    return cfg, rt, events


def kinds(events: list[tuple[str, dict]], name: str) -> list[dict]:
    return [payload for kind, payload in events if kind == name]


def scenario_on(tmp: Path) -> None:
    """开关打开（默认）：思考原文必须原样交出去，并且入库。"""
    print("\n[1] show_thinking=True · 思考原文要出去、要入库")
    pieces = ["先看题目。", "这道题分两步：", "第一步算边长。"]
    full = "".join(pieces)
    _cfg, rt, events = build(tmp / "on", [{"text": "答案是 42。", "thoughts": pieces}])

    asyncio.run(rt.run("算一下"))

    check("发出的是 thought 事件而不是 thinking",
          bool(kinds(events, "thought")) and not kinds(events, "thinking"),
          f"thought={len(kinds(events, 'thought'))} thinking={len(kinds(events, 'thinking'))}")

    first = kinds(events, "thought")[0]
    check("第一条 thought 是「已发出、还没开口」的空心跳",
          first.get("text") == "" and first.get("waited") == 0, str(first))

    streamed = "".join(p.get("text", "") for p in kinds(events, "thought"))
    check("推出去的思考是全文的前缀（增量只推节流窗口内的）",
          full.startswith(streamed), f"推={streamed!r}")
    check("确实推出去了内容（不是全被节流吞掉）", len(streamed) > 0, repr(streamed))

    ends = kinds(events, "turn_end")
    check("turn_end 带上本轮思考全文",
          bool(ends) and ends[-1].get("thoughts") == full,
          repr(ends[-1].get("thoughts")) if ends else "没有 turn_end")

    check("run_end 带上了思考全文",
          kinds(events, "run_end")[-1].get("thoughts") == full,
          repr(kinds(events, "run_end")[-1].get("thoughts")))

    conv = rt.conversation
    check("助手消息的 thoughts 落盘", conv.messages[1].thoughts == full,
          repr(conv.messages[1].thoughts))
    check("正文没有被思考污染", conv.messages[1].text == "答案是 42。", conv.messages[1].text)

    # 切回对话 / 重启后要靠 conversation.current 把思考重放出来。
    current = rt.current_conversation()
    check("conversation.current 里带着 thoughts",
          current["messages"][1].get("thoughts") == full,
          repr(current["messages"][1].get("thoughts")))

    reloaded = rt.load_conversation(conv.id)
    check("从磁盘重读后 thoughts 还在",
          reloaded is not None and reloaded["messages"][1]["thoughts"] == full)

    check("status().chat.show_thinking 回显为真",
          rt.status()["chat"]["show_thinking"] is True)


def scenario_off(tmp: Path) -> None:
    """开关关掉：回到老行为 —— 只有心跳，一个字都不出去、也不入库。"""
    print("\n[2] show_thinking=False · 只留心跳")
    pieces = ["不该被看到的思考。"]
    _cfg, rt, events = build(tmp / "off", [{"text": "答案。", "thoughts": pieces}],
                            show_thinking=False)

    report = asyncio.run(rt.run("问一句"))

    check("走的是 thinking 心跳", bool(kinds(events, "thinking")))
    check("没有 thought 事件", not kinds(events, "thought"),
          str(kinds(events, "thought")))
    check("心跳里只有字数与尾巴，没有全文",
          all("text" not in p for p in kinds(events, "thinking")))
    check("turn_end 的 thoughts 为空",
          kinds(events, "turn_end")[-1].get("thoughts") == "",
          repr(kinds(events, "turn_end")[-1].get("thoughts")))
    check("不入库（Message.thoughts 为空）",
          rt.conversation.messages[1].thoughts == "",
          repr(rt.conversation.messages[1].thoughts))
    check("run_end 的 thoughts 也为空", report.thoughts == "", repr(report.thoughts))
    check("status().chat.show_thinking 回显为假",
          rt.status()["chat"]["show_thinking"] is False)


def scenario_toggle(tmp: Path) -> None:
    """开关可以中途改（下一轮生效），且不影响「上游是否思考」。"""
    print("\n[3] set_chat 切开关 · 下一轮生效")
    cfg, rt, events = build(tmp / "toggle", [
        {"text": "第一次。", "thoughts": ["思考甲。"]},
        {"text": "第二次。", "thoughts": ["思考乙。"]},
    ])

    asyncio.run(rt.run("第一问"))
    check("开的时候有 thought", bool(kinds(events, "thought")))

    snap = asyncio.run(rt.set_chat(show_thinking=False))
    check("set_chat 立刻回显新值", snap["show_thinking"] is False)
    check("配置对象同步改了", cfg.chat.show_thinking is False)

    events.clear()
    asyncio.run(rt.run("第二问"))
    check("关掉之后只剩心跳", not kinds(events, "thought") and bool(kinds(events, "thinking")))

    # 关键区分：这个开关只管「看不看得见思考」，不管「上游思不思考」。
    check("show_thinking 不影响 extended_thinking",
          cfg.chat.extended_thinking == EngineConfig().chat.extended_thinking,
          str(cfg.chat.extended_thinking))


def scenario_multiturn(tmp: Path) -> None:
    """带工具调用的任务会有多轮 —— 每轮各有一段思考，要按顺序攒起来。"""
    print("\n[4] 多轮 · 每轮思考都攒进同一条助手消息")
    _cfg, rt, _events = build(tmp / "multi", [
        {
            "text": "先跑一下。\n```local_cmd\necho hi\n```\n",
            "thoughts": ["第一轮：先执行命令。"],
        },
        {"text": "跑完了，结果是 hi。", "thoughts": ["第二轮：读输出作答。"]},
    ])

    report = asyncio.run(rt.run("跑个命令"))

    check("确实跑了两轮", rt.transport.calls == 2, str(rt.transport.calls))
    check("两轮思考按顺序拼在一起",
          report.thoughts == "第一轮：先执行命令。\n\n第二轮：读输出作答。",
          repr(report.thoughts))
    check("入库的是合并后的全文",
          rt.conversation.messages[1].thoughts == report.thoughts,
          repr(rt.conversation.messages[1].thoughts))
    check("正文只留最终回答",
          rt.conversation.messages[1].text == "跑完了，结果是 hi。",
          rt.conversation.messages[1].text)


def scenario_heartbeat(tmp: Path) -> None:
    """上游长时间一个字都不流时，界面上必须还有东西在动。

    这是 2026-10-14 实测过的场景（>120 秒连思考增量都没有）。原来靠 `thinking`
    心跳兜住，现在改走 `thought` 之后**这条保障不能丢** —— 否则换成新通道的
    那一刻，界面上最要紧的「到底还在不在跑」反而没了。
    """
    print("\n[5] 上游迟迟不开口 · 心跳不能丢")
    _cfg, rt, events = build(tmp / "beat", [
        {"text": "终于回了。", "thoughts": ["迟到的思考。"], "delay": 3.4},
    ])

    asyncio.run(rt.run("等一等"))

    waited = [p for p in kinds(events, "thought") if p.get("waited", 0) >= 3]
    check("等待期间有带 waited 的心跳", bool(waited), str(kinds(events, "thought")))
    check("心跳里还没有正文（text 为空）",
          all(p.get("text") == "" for p in waited), str(waited))
    check("最终思考还是完整交出去了",
          kinds(events, "turn_end")[-1].get("thoughts") == "迟到的思考。",
          repr(kinds(events, "turn_end")[-1].get("thoughts")))


def scenario_silent(tmp: Path) -> None:
    """思考只在快照帧里出现（增量通道一个字都没走）时，也不能丢。

    这不是假想：正文通道就踩过同一个坑 —— 2026-10-15 实测「短回复整段只出现在
    快照帧，一个增量帧都没有」，当时的症状是「消息发出去了，但一个字都没回来」。
    思考通道走的是同一套 SSE 解析，**必然会有同样的帧形态**。如果只靠
    `on_thought` 累计，这一轮就会在界面上显示成「思考过程 0 字」。
    """
    print("\n[6] 思考只在快照帧里 · 不能因此丢掉")
    _cfg, rt, events = build(tmp / "silent", [
        {"text": "回答。", "thoughts": ["整段只在快照里的思考。"], "silent": True},
    ])

    report = asyncio.run(rt.run("问一句"))

    check("增量通道确实一个字都没走",
          all(p.get("text") == "" for p in kinds(events, "thought")),
          str(kinds(events, "thought")))
    check("turn_end 仍然拿到全文",
          kinds(events, "turn_end")[-1].get("thoughts") == "整段只在快照里的思考。",
          repr(kinds(events, "turn_end")[-1].get("thoughts")))
    check("入库也没有丢", report.thoughts == "整段只在快照里的思考。", repr(report.thoughts))


def main() -> int:
    print("=" * 70)
    print("  思考过程输出链路自测")
    print("=" * 70)
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        scenario_on(tmp)
        scenario_off(tmp)
        scenario_toggle(tmp)
        scenario_multiturn(tmp)
        scenario_heartbeat(tmp)
        scenario_silent(tmp)

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
