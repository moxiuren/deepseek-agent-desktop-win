"""端到端验证「接力换会话」—— **会真发消息**（3 次请求）。

要回答的问题
------------

单测能证明「摘要被拼进了提示词」，但证明不了**模型真的读懂了它**。
而后者才是这个功能的全部意义：

    用户按了「接力新会话」→ 换了服务端会话（新会话树、服务端上下文是空的）
    → 模型应该**依然记得**之前聊过什么。

所以这个工具用一个**暗号**来做判据：

    1. 在第一段对话里告诉模型一个随机暗号
    2. 接力（走 `model` 模式：在旧会话里问一次摘要）
    3. 在新会话里问它「暗号是什么」

如果摘要没有真的接上，第 3 步不可能答对 —— 新会话在服务端就是一张白纸。
这比「断言提示词里有某段字符串」强得多，因为那段字符串完全可能是**没被
模型采纳**的噪声。

代价（**与只读校验工具不同，这个有痕迹**）
------------------------------------------

会发 **3 次 completion 请求**，并在账号里留下 **2 条会话**（旧 + 新）。
所以它**不放进 `--enginecheck`**，只能手动跑。
本地存储仍然隔离到临时目录（不碰真实对话库）。

用法
----

    cd engine
    ./.venv/Scripts/python.exe -X utf8 tools/verify_handoff.py
"""

from __future__ import annotations

import asyncio
import secrets
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
    cfg.identity.account_label = "接力验证"
    # 节奏：**只放开会拖时间的两处**（突发保持 12 秒、每 6 轮阅读停顿 4~9 秒），
    # 保留正常的间隔采样 —— 这个工具要验证的是功能，不是节奏，但也没必要
    # 为了快而把请求打成机器形态。
    cfg.pacing.max_burst = 100
    cfg.pacing.hold_after_burst_ms = 0
    cfg.pacing.read_pause_every = 0
    cfg.pacing.active_start_hour = 0
    cfg.pacing.active_end_hour = 24
    cfg.prompt.template_file = str(tmp / "system-prompt.md")
    cfg.memory.file = str(tmp / "memory.md")
    return cfg


async def scenario() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="gad-handoff-live-"))
    log(f"[准备] 本地存储隔离到 {tmp}")
    log("[注意] 本工具会发 3 次请求，并在账号里留下 2 条会话。")

    secret = f"紫水晶-{secrets.randbelow(9000) + 1000}"
    log(f"[暗号] {secret}（随机生成，模型不可能预先知道）")

    cfg = make_cfg(tmp)
    store = ConversationStore(tmp / "conversations")
    tp = DeepSeekRpcTransport(
        token_provider=lambda: load_token(DEFAULT_TOKEN_FILE),
        device_id_path=ROOT / ".device-id",
        model="default",
        search_enabled=False,
    )
    rt = AgentRuntime(cfg, transport=tp, store=store)

    try:
        # ---------- 1. 第一段对话：埋暗号 ----------
        log("\n[1] 第一段对话 · 埋一个暗号（第 1 次请求）")
        r1 = await rt.run(f"请记住一个暗号：{secret}。现在只需要回答「已记住」两个字。")
        log(f"     回答：{r1.final_text[:80]!r}")
        check("第一段对话有回答", bool(r1.final_text.strip()), r1.stop_reason)

        old_conv = rt.conversation
        old_id, old_cid = old_conv.id, old_conv.session.cid
        old_msgs = len(old_conv.messages)
        log(f"     对话 {old_id} · cid {old_cid} · 消息 {old_msgs} 条")
        check("第一段对话有服务端会话", bool(old_cid), old_cid)

        # ---------- 2. 接力 ----------
        log("\n[2] 接力换会话 · model 模式（第 2 次请求：在旧会话里问摘要）")
        res = await rt.handoff_conversation(mode="model")
        check("接力成功", res.get("ok") is True, str(res))
        if not res.get("ok"):
            return

        check("摘要由模型生成", res.get("mode") == "model", str(res.get("mode")))
        check("摘要非空", res.get("chars", 0) > 0, str(res.get("chars")))
        log(f"     摘要 {res.get('chars')} 字：")
        for line in str(res.get("summary", "")).splitlines():
            log(f"       | {line}")

        new_conv = rt.conversation
        check("换到了新对话", new_conv.id != old_id, new_conv.id)
        check("新对话是全新的会话树", new_conv.session.cid == "", new_conv.session.cid)
        check("新对话带上了摘要", bool(new_conv.handoff_summary.strip()))
        check("摘要溯源到旧对话", new_conv.handoff_from == old_id, new_conv.handoff_from)

        olds = [c for c in rt.list_conversations() if c["id"] == old_id]
        check("旧对话仍在列表里", len(olds) == 1)
        check("旧对话消息没被删",
              bool(olds) and olds[0]["message_count"] == old_msgs,
              f"{olds[0]['message_count'] if olds else '?'} vs {old_msgs}")

        # ---------- 3. 新会话里问暗号（决定性的一步）----------
        log("\n[3] 新会话里问暗号 · 这是整个功能的判据（第 3 次请求）")
        log("    若摘要没有真的接上，服务端这边是一张白纸，它不可能答对。")
        r3 = await rt.run("我刚才让你记住的暗号是什么？只回答暗号本身，不要解释。")
        answer = (r3.final_text or "").strip()
        log(f"     回答：{answer[:120]!r}")

        check("新会话答出了暗号", secret in answer,
              f"回答里找不到 {secret}")
        new_cid = rt.session.meta.cid
        log(f"     新会话 cid {new_cid}")
        check("新会话确实是另一条服务端会话", bool(new_cid) and new_cid != old_cid,
              f"{new_cid} vs {old_cid}")
        check("新会话的锚点已前进", bool(rt.session.meta.rid), rt.session.meta.rid)

        # ---------- 4. 摘要只在新会话里生效 ----------
        log("\n[4] 切回旧对话 · 不应再带那段摘要")
        rt.load_conversation(old_id)
        p_old = rt._bootstrap_prompt("随便问一句")
        check("旧对话的提示词不含接力摘要", "上一段对话的摘要" not in p_old)
        rt.load_conversation(new_conv.id)
        p_new = rt._bootstrap_prompt("随便问一句")
        check("新对话的提示词含接力摘要", "上一段对话的摘要" in p_new)
    finally:
        await rt.stop()


def main() -> int:
    log("=" * 70)
    log("端到端 · 接力换会话（会发 3 次请求，账号留 2 条会话）")
    log("=" * 70)
    try:
        asyncio.run(scenario())
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        check("整体执行", False, f"{type(exc).__name__}: {exc}")

    log("\n" + "-" * 70)
    log(f"  通过 {len(PASS)} · 失败 {len(FAIL)}")
    if FAIL:
        log("  失败项：")
        for name in FAIL:
            log(f"    - {name}")
    log("-" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
