"""实测：切换对话时服务端会话是否真的隔离，以及上下文能否续接。

要回答的问题
------------
用户反馈「多回合之间不会真正切换到对应会话，所有会话共用一个窗口」。
代码层面看 `load_conversation()` 确实换了 `cid`/`rid`，但**没有实测证据**。
在给方案之前必须把这条钉死 —— 否则方案可能建在一个不成立的前提上。

本脚本用**真实传输层**跑三段：
  1. 对话 A 发「记住数字 731」
  2. 新建对话 B 发「记住数字 942」→ 断言 cid_A != cid_B
  3. 切回 A 问「刚才的数字是几」→ 断言回复含 731（证明续接成功）
  4. 用 `chat_session/fetch_page` + `chat/history_messages` 回读，
     验证服务端确实存了两个独立会话、且能取回历史

代价与边界
----------
* 本地存储隔离到临时目录，不碰真实 AppData。
* **服务端会话无法隔离** —— 会在账号里留下 2 个测试会话（约 4 条消息）。
* 只跑必要的 3 次发送，不做压力测试。

用法
----
    cd engine
    ./.venv/Scripts/python.exe -X utf8 tools/verify_session_isolation.py
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
from tools.probe_history_api import BASE, IMPERSONATE, device_id, headers_for  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []
RUN_TIMEOUT = 200.0


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    mark = "通过" if cond else "失败"
    extra = f" —— {detail}" if detail and not cond else ""
    print(f"  [{mark}] {name}{extra}", flush=True)


def log(msg: str = "") -> None:
    print(msg, flush=True)


def make_cfg(tmp: Path) -> EngineConfig:
    cfg = EngineConfig()
    cfg.identity.account_label = "会话隔离验证"
    cfg.pacing.base_gap_ms = 0.0
    cfg.pacing.floor_gap_ms = 0.0
    cfg.pacing.active_start_hour = 0
    cfg.pacing.active_end_hour = 24
    cfg.pacing.max_turns_per_session = 50
    cfg.max_tool_turns = 2
    cfg.chat.search_enabled = False
    cfg.chat.extended_thinking = False
    # 隔离磁盘：默认模板/记忆都落真实 AppData，验证不该碰。
    cfg.prompt.template_file = str(tmp / "system-prompt.md")
    cfg.memory.file = str(tmp / "memory.md")
    return cfg


async def run_once(rt: AgentRuntime, goal: str, label: str) -> str:
    log(f"    发送[{label}]：{goal[:40]}")
    try:
        report = await asyncio.wait_for(rt.run(goal), timeout=RUN_TIMEOUT)
    except TimeoutError:
        log(f"    !! {RUN_TIMEOUT:.0f}s 超时")
        return ""
    text = (report.final_text or "").strip()
    log(f"    回复：{text[:90]!r}  (stop={report.stop_reason}, turns={report.turns})")
    return text


def server_sessions(limit: int = 30) -> list[dict]:
    """从服务端拉会话列表（只读）。"""
    from curl_cffi import requests as creq

    tok = load_token(DEFAULT_TOKEN_FILE)
    s = creq.Session(impersonate=IMPERSONATE)
    r = s.get(
        f"{BASE}/api/v0/chat_session/fetch_page",
        headers=headers_for(tok, device_id()),
        params={"count": limit},
        timeout=25,
    )
    biz = (r.json().get("data") or {}).get("biz_data") or {}
    return biz.get("chat_sessions") or []


def server_history(sid: str) -> dict:
    """拉某会话的服务端历史（只读）。"""
    from curl_cffi import requests as creq

    tok = load_token(DEFAULT_TOKEN_FILE)
    s = creq.Session(impersonate=IMPERSONATE)
    r = s.get(
        f"{BASE}/api/v0/chat/history_messages",
        headers=headers_for(tok, device_id()),
        params={"chat_session_id": sid},
        timeout=25,
    )
    return ((r.json().get("data") or {}).get("biz_data") or {})


async def scenario() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="gad-iso-"))
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

    log("")
    log("[1] 对话 A")
    r1 = await run_once(rt, "请记住数字 731。只回复两个字：好的", "A")
    conv_a, cid_a = rt.conversation.id, rt.conversation.session.cid
    log(f"    conv_A={conv_a[:12]}...  cid_A={cid_a}")

    log("")
    log("[2] 新建对话 B")
    rt.new_conversation()
    r2 = await run_once(rt, "请记住数字 942。只回复两个字：好的", "B")
    conv_b, cid_b = rt.conversation.id, rt.conversation.session.cid
    log(f"    conv_B={conv_b[:12]}...  cid_B={cid_b}")

    log("")
    log("[3] 隔离断言")
    check("A、B 都拿到了服务端 cid", bool(cid_a) and bool(cid_b), f"{cid_a} / {cid_b}")
    check("两个对话的 chat_session 不同", cid_a != cid_b, f"{cid_a} == {cid_b}")
    check("A 的回复有正文", bool(r1), r1[:40])
    check("B 的回复有正文", bool(r2), r2[:40])

    log("")
    log("[4] 切回 A，检查上下文是否续上")
    rt.load_conversation(conv_a)
    log(f"    切换后引擎持有 cid={rt.session.meta.cid}  rid={rt.session.meta.rid}")
    check("切换后引擎的 cid 是 A 的", rt.session.meta.cid == cid_a, rt.session.meta.cid)
    check("切换后 rid 非空（能接 parent）", bool(rt.session.meta.rid), "rid 为空")
    r3 = await run_once(rt, "我刚才让你记住的数字是几？只回答数字", "A-续问")
    check("A 的上下文续上了（回复含 731）", "731" in r3, r3[:60])
    check("A 没串到 B 的内容（回复不含 942）", "942" not in r3, r3[:60])

    log("")
    log("[5] 用服务端接口回读（验证 fetch_page / history_messages 可用）")
    try:
        sessions = server_sessions()
        ids = {s.get("id") for s in sessions}
        log(f"    服务端会话总数：{len(sessions)}")
        check("服务端列表里能找到 A", cid_a in ids, cid_a)
        check("服务端列表里能找到 B", cid_b in ids, cid_b)
        if cid_a in ids:
            hist = server_history(cid_a)
            msgs = hist.get("chat_messages") or []
            cs = hist.get("chat_session") or {}
            log(f"    A 的历史：{len(msgs)} 条消息 · current_message_id="
                f"{cs.get('current_message_id')}")
            for m in msgs:
                kinds = [f.get("type") for f in (m.get("fragments") or [])]
                log(f"      #{m.get('message_id')} parent={m.get('parent_id')} "
                    f"role={m.get('role')} fragments={kinds}")
            check("A 的服务端历史非空", len(msgs) > 0, str(len(msgs)))
            check("current_message_id 等于末条 message_id",
                  cs.get("current_message_id") == msgs[-1].get("message_id") if msgs else False,
                  f"{cs.get('current_message_id')} vs {msgs[-1].get('message_id') if msgs else None}")
    except Exception as exc:  # noqa: BLE001
        check("服务端回读", False, f"{type(exc).__name__}: {exc}")

    log("")
    log("-" * 70)
    log(f"  通过 {len(PASS)} · 失败 {len(FAIL)}")
    if FAIL:
        for f in FAIL:
            log(f"    失败项：{f}")
    log("-" * 70)
    log(f"  本地临时目录（可删）：{tmp}")


def main() -> int:
    log("=" * 70)
    log("会话隔离与上下文续接实测（真实传输层，会发 3 条消息）")
    log("=" * 70)
    try:
        asyncio.run(scenario())
    except KeyboardInterrupt:
        log("中断")
        return 130
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
