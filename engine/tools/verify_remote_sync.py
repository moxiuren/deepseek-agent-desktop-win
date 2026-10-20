"""端到端验证 `conversation.sync` —— 从服务端拉回历史 + 校正续接锚点。

要回答的两个问题
----------------
1. **本地记录丢了，能不能从服务端恢复？**
   （换机器、本地文件损坏、或用户主要在网页版里聊 —— 这三种情况下本地
   只有残缺记录，而服务端是完整的。）
2. **本地锚点过期了，能不能被校正回来？**
   锚点（`SessionMeta.rid` → 请求里的 `parent_message_id`）一旦指向不存在的
   消息，服务端会当成新 root，上下文**静默断掉** —— 界面看起来一切正常。
   这是最难自己发现的一类故障，所以必须有测试钉住。

做法
----
新建一个对话发一条消息（**只发 1 条**，避免不必要的请求），然后：
  A. 清空本地 messages → sync → 断言消息从服务端回来了
  B. 把本地 rid 改成垃圾值 → sync → 断言锚点被校正回 `current_message_id`

代价
----
本地存储隔离到临时目录；但**服务端会留下 1 个测试会话**（无法避免）。

用法
----
    cd engine
    ./.venv/Scripts/python.exe -X utf8 tools/verify_remote_sync.py
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
    cfg.identity.account_label = "同步验证"
    cfg.pacing.base_gap_ms = 0.0
    cfg.pacing.floor_gap_ms = 0.0
    cfg.pacing.active_start_hour = 0
    cfg.pacing.active_end_hour = 24
    cfg.pacing.max_turns_per_session = 50
    cfg.max_tool_turns = 2
    cfg.chat.search_enabled = False
    cfg.chat.extended_thinking = False
    cfg.prompt.template_file = str(tmp / "system-prompt.md")
    cfg.memory.file = str(tmp / "memory.md")
    return cfg


async def scenario() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="gad-sync-"))
    log(f"[准备] 本地存储隔离到 {tmp}")

    cfg = make_cfg(tmp)
    store = ConversationStore(tmp / "conversations")
    tp = DeepSeekRpcTransport(
        token_provider=lambda: load_token(DEFAULT_TOKEN_FILE),
        device_id_path=ROOT / ".device-id",
        model="default",
        search_enabled=False,
    )
    rt = AgentRuntime(cfg, transport=tp, store=store)

    # ---------- 0. 只读能力先探一次 ----------
    #
    # **必须先 start()** —— 这两个接口要用 curl_cffi 的 Session 发请求，
    # 而 Session 是在 start() 里建的。没 start 时 `_require_session()` 会抛
    # `TransportError("transport 未 start()")`。start() 是幂等的（会先关掉
    # 旧 session 再建新的），所以这里显式调一次不影响后面 run()。
    log("")
    log("[0] 传输层的只读接口")
    try:
        await tp.start()
        sessions = await tp.list_sessions(count=5)
        check("list_sessions 可用", isinstance(sessions, list), str(type(sessions)))
        log(f"    服务端会话数（前 5）：{len(sessions)}")
        if sessions:
            sample = sessions[0]
            log(f"    首条字段：{sorted(sample.keys())[:8]}")
            check("列表项带 id", bool(sample.get("id")), str(sample.get("id")))
    except Exception as exc:  # noqa: BLE001
        check("list_sessions 可用", False, f"{type(exc).__name__}: {exc}")

    # ---------- 1. 建一个对话并发一条 ----------
    log("")
    log("[1] 新建对话并发一条消息")
    rt.new_conversation()
    try:
        report = await asyncio.wait_for(
            rt.run("请记住暗号：蓝鲸。只回复两个字：好的"), timeout=200
        )
    except TimeoutError:
        check("发消息完成", False, "超时")
        return
    text = (report.final_text or "").strip()
    log(f"    回复：{text[:60]!r}")
    conv_id = rt.conversation.id
    remote_cid = rt.conversation.session.cid
    local_rid = rt.conversation.session.rid
    log(f"    conv={conv_id}  cid={remote_cid}  rid={local_rid}")
    check("拿到服务端 cid", bool(remote_cid), remote_cid)
    check("本地有 2 条消息", len(rt.conversation.messages) == 2,
          str(len(rt.conversation.messages)))

    # ---------- 2. 本地记录丢失 → 能否从服务端恢复 ----------
    log("")
    log("[2] 清空本地消息，再 sync")
    rt.conversation.messages = []
    store.save(rt.conversation)
    check("本地已清空", len(store.get(conv_id).messages) == 0)

    try:
        result = await rt.sync_conversation(conv_id)
    except Exception as exc:  # noqa: BLE001
        check("sync 成功", False, f"{type(exc).__name__}: {exc}")
        return
    log(f"    sync 结果：{ {k: v for k, v in result.items() if k != 'conversation'} }")
    check("sync 报告成功", result.get("synced") is True, str(result))
    check("从服务端拉回了消息", result.get("remote_count", 0) >= 2,
          str(result.get("remote_count")))

    restored = store.get(conv_id).messages
    log(f"    恢复后本地消息数：{len(restored)}")
    for m in restored:
        log(f"      {m.role:9} rid={m.rid:>4}  {m.text[:44]!r}")
    check("本地消息已恢复", len(restored) >= 2, str(len(restored)))
    check("恢复出 user 消息",
          any(m.role == "user" for m in restored), str([m.role for m in restored]))
    check("恢复出 assistant 消息",
          any(m.role == "assistant" for m in restored), str([m.role for m in restored]))
    user_texts = " ".join(m.text for m in restored if m.role == "user")
    check("用户输入已剥离系统提示词", "你是" not in user_texts[:20],
          user_texts[:60])
    check("用户输入保留了原话", "蓝鲸" in user_texts, user_texts[:80])

    # ---------- 3. 锚点过期 → 能否校正 ----------
    log("")
    log("[3] 把锚点改成垃圾值，再 sync")
    conv = store.get(conv_id)
    conv.session.rid = "999999"
    store.save(conv)
    if rt.conversation.id == conv_id:
        rt.session.restore(conv.session)
    check("锚点已被改坏", rt.session.meta.rid == "999999", rt.session.meta.rid)

    try:
        await rt.sync_conversation(conv_id)
    except Exception as exc:  # noqa: BLE001
        check("第二次 sync 成功", False, f"{type(exc).__name__}: {exc}")
        return
    fixed = store.get(conv_id).session.rid
    log(f"    校正后锚点 = {fixed}（原垃圾值 999999）")
    check("锚点被校正", fixed != "999999", fixed)
    check("校正后锚点非空", bool(fixed), repr(fixed))
    check("锚点是纯数字（请求侧会 int() 它）", str(fixed).isdigit(), fixed)
    check("引擎手里的锚点也更新了", rt.session.meta.rid == fixed,
          f"{rt.session.meta.rid} vs {fixed}")
    check("同步时间戳已记录", store.get(conv_id).synced_at > 0,
          str(store.get(conv_id).synced_at))

    # ---------- 4. 没发过消息的对话 ----------
    log("")
    log("[4] 对「还没发过消息」的对话 sync")
    fresh = rt.new_conversation()
    r3 = await rt.sync_conversation(fresh["id"])
    check("未发消息的对话 sync 被明确拒绝",
          r3.get("synced") is False and "服务端会话" in str(r3.get("reason")), str(r3))

    log("")
    log("-" * 70)
    log(f"  通过 {len(PASS)} · 失败 {len(FAIL)}")
    for f in FAIL:
        log(f"    失败项：{f}")
    log("-" * 70)
    log(f"  本地临时目录（可删）：{tmp}")


def main() -> int:
    log("=" * 70)
    log("conversation.sync 端到端验证（真实传输层，会发 1 条消息）")
    log("=" * 70)
    try:
        asyncio.run(scenario())
    except KeyboardInterrupt:
        log("中断")
        return 130
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
