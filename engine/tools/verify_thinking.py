"""端到端验证「思考过程」真的被输出出来 —— **会真发消息**（1 次请求）。

要回答的问题
------------

单测（`tests/test_thinking.py`）能证明「引擎把 `outcome.thoughts` 交出去了」，
但它喂的是假传输层 —— 证明不了**上游真的会吐思考**、也证明不了
`THINK` 片段那条 SSE 路径认得对。

而用户报的正是这条：「目前 deepseek 思考的文字没有输出出来」。
所以这个工具把整条链路跑一遍：

    上游 SSE `THINK` → `_Acc.thought_text()` → `TurnOutcome.thoughts`
      → agent 的 `thought` 事件 → `turn_end.thoughts` → 入库

判据（三条，缺一不可）
----------------------

1. **收到了 `thought` 事件**，而且**没有** `thinking` 心跳（两者互斥）
2. **思考原文非空**，且与 `turn_end` / `report` / 入库三处的全文一致
3. **思考没有漏进正文** —— 这是当初「只报字数不报原文」的理由之一，
   换成输出原文之后这条更要盯住

代价（**有痕迹**）
------------------

会发 **1 次 completion 请求**，并在账号里留下 **1 条会话**。
所以它**不放进 `--enginecheck`**，只能手动跑。
本地存储仍然隔离到临时目录（不碰真实对话库）。

用法
----

    cd engine
    ./.venv/Scripts/python.exe -X utf8 tools/verify_thinking.py
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

from gad_engine.agent import AgentRuntime  # noqa: E402
from gad_engine.config import EngineConfig  # noqa: E402
from gad_engine.identity import DEFAULT_TOKEN_FILE, load_token  # noqa: E402
from gad_engine.store import ConversationStore  # noqa: E402
from gad_engine.transport import DeepSeekRpcTransport  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    mark = "通过" if cond else "失败"
    extra = f" —— {detail}" if detail and not cond else ""
    print(f"  [{mark}] {name}{extra}", flush=True)


def log(msg: str = "") -> None:
    print(msg, flush=True)


def make_cfg(tmp: Path) -> EngineConfig:
    cfg = EngineConfig()
    cfg.identity.account_label = "思考过程验证"
    # 这一条是本次验证的前提：不开思考，上游根本不会吐 `THINK` 片段。
    cfg.chat.extended_thinking = True
    cfg.chat.show_thinking = True
    # 节奏：只放开会拖时间的那几处（突发保持、阅读停顿）——
    # 这个工具要验证的是功能，不是节奏，但也没必要为了快把请求打成机器形态。
    cfg.pacing.max_burst = 100
    cfg.pacing.hold_after_burst_ms = 0
    cfg.pacing.read_pause_every = 0
    cfg.pacing.active_start_hour = 0
    cfg.pacing.active_end_hour = 24
    cfg.prompt.template_file = str(tmp / "system-prompt.md")
    cfg.memory.file = str(tmp / "memory.md")
    return cfg


#: 刻意挑一道**必须真的推几步**的题。问「你好」这种，模型可能不思考直接答 ——
#: 那样「思考为空」就成了题目的问题，而不是链路的 bug，判据会失去意义。
#:
#: 数字是配平的：设十位 t，百位 t+2，个位 t-1，和 3t+1=13 → t=4 → **643**。
#: （第一版写成和=12，3t+1=12 无整数解 —— 模型认真推完答了「无解」，
#:  虽然是有效验证，但一道自相矛盾的题会让后来人以为工具坏了。）
PROMPT = (
    "一个三位数：各位数字之和是 13，百位比十位大 2，十位比个位大 1。"
    "请先在心里推一遍，最后只回答这个三位数本身，不要解释。"
)


async def scenario() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="gad-thinking-live-"))
    log(f"[准备] 本地存储隔离到 {tmp}")
    log("[注意] 本工具会发 1 次请求，并在账号里留下 1 条会话。")

    cfg = make_cfg(tmp)
    store = ConversationStore(tmp / "conversations")
    events: list[tuple[str, dict]] = []
    tp = DeepSeekRpcTransport(
        token_provider=lambda: load_token(DEFAULT_TOKEN_FILE),
        device_id_path=ROOT / ".device-id",
        model="default",
        search_enabled=False,
    )
    rt = AgentRuntime(
        cfg,
        transport=tp,
        on_event=lambda kind, payload: events.append((kind, dict(payload))),
        store=store,
    )

    def payloads(name: str) -> list[dict]:
        return [p for k, p in events if k == name]

    try:
        log("\n[1] 思考模式下发一次请求（唯一一次）")
        log(f"     题面：{PROMPT[:48]}…")
        report = await rt.run(PROMPT)
        reply = (report.final_text or "").strip()
        log(f"     回答：{reply[:80]!r}")
        log(f"     stop_reason={report.stop_reason} turns={report.turns}")

        # ---------- 1. 通道选对了 ----------
        log("\n[2] 走的是哪条通道")
        thoughts = payloads("thought")
        beats = payloads("thinking")
        check("收到了 thought 事件（思考原文通道）", bool(thoughts),
              f"thought={len(thoughts)}")
        check("没有 thinking 心跳（两者互斥）", not beats, f"thinking={len(beats)}")
        if not thoughts:
            log("     ⚠ 一个 thought 事件都没有 —— 上游没吐思考，或 SSE 路径没认出来。")
            log("       下一步该查 transport 的 `_THINK_FRAG_TYPES` 与真实 fragment 类型。")
            return

        streamed = "".join(p.get("text", "") for p in thoughts)
        log(f"     增量共 {len(streamed)} 字（{len(thoughts)} 帧）")

        # ---------- 2. 原文真的拿到了 ----------
        log("\n[3] 思考原文的完整性与一致性")
        check("增量拼出来的思考非空", len(streamed.strip()) > 0, repr(streamed[:80]))

        ends = payloads("turn_end")
        final_thoughts = ends[-1].get("thoughts", "") if ends else ""
        check("turn_end 带上了思考全文", bool(final_thoughts.strip()),
              repr(final_thoughts[:80]))
        check("增量是全文的前缀（增量按节流推，尾部可能没推完）",
              final_thoughts.startswith(streamed),
              f"增量 {len(streamed)} 字 / 全文 {len(final_thoughts)} 字")
        check("report.thoughts 与 turn_end 一致",
              report.thoughts == final_thoughts,
              f"{len(report.thoughts)} vs {len(final_thoughts)}")

        log(f"     思考全文 {len(final_thoughts)} 字，开头：")
        for line in final_thoughts[:300].splitlines():
            log(f"       | {line}")

        conv = rt.conversation
        check("助手消息里落了 thoughts", conv.messages[-1].thoughts == final_thoughts,
              repr(conv.messages[-1].thoughts[:60]))

        # 切回对话 / 重启后要靠 conversation.current 重放出来。
        current = rt.current_conversation()
        check("conversation.current 里带着 thoughts（切回来能重放）",
              current["messages"][-1].get("thoughts") == final_thoughts)
        reloaded = rt.load_conversation(conv.id)
        check("从磁盘重读后 thoughts 还在",
              reloaded is not None and
              reloaded["messages"][-1]["thoughts"] == final_thoughts)

        # ---------- 3. 思考没有漏进正文 ----------
        # 这是当初「只报字数、不报原文」的理由之一。改成输出原文之后，
        # 更要盯住「原文只能从 thought 通道出去，不能混进回答」。
        log("\n[4] 思考没有漏进正文")
        check("正文非空", bool(reply), report.stop_reason)
        probe = final_thoughts.strip()[:40]
        check("正文里没有夹思考原文", probe not in reply, repr(probe))
        check("正文里没有夹思考草稿的常见开头",
              "嗯，" not in reply[:8] and "Let me" not in reply[:20], repr(reply[:60]))

        # ---------- 4. 收尾 ----------
        log("\n[5] 存储开销")
        check("思考入库确实占空间（这是有意的取舍）",
              len(final_thoughts) > 0, f"{len(final_thoughts)} 字")
        log(f"     ⚠ 该对话的本地 JSON 会比关掉开关时大约 {len(final_thoughts)} 字。")
        log("       觉得涨得快就把设置页的「显示思考过程」关掉（那是同一开关）。")
    finally:
        try:
            await rt.stop()
        except Exception as exc:  # noqa: BLE001
            log(f"[收尾] 停止运行时出错（不影响结论）：{exc}")


def main() -> int:
    print("=" * 70)
    print("  思考过程输出 · 端到端验证（真实请求）")
    print("=" * 70)
    asyncio.run(scenario())

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
