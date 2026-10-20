"""端到端验证「读取网页版会话 + 接管」—— **只读，不发送任何消息**。

要回答的三个问题
----------------
1. **账号下的服务端会话能不能全读出来？**
   （本地此前只看得见自己建的对话；用户在网页版里聊出来的那些，本地一条都不知道。）
2. **接管一条能不能拿到完整历史、并把续接锚点校正对？**
   锚点（`SessionMeta.rid` → 请求里的 `parent_message_id`）对不上，服务端会当成
   新 root，上下文**静默断掉** —— 界面看起来一切正常。这是最难自己发现的一类故障。
3. **接管后「我发的消息会进那条会话」这件事，在引擎侧是否成立？**
   （断言 `session.cid` == 那条服务端会话、`attached=True`、不自动轮换。）

做法
----
只用两个**只读**接口：`chat_session/fetch_page`（列表）与
`chat/history_messages`（历史）。**不建会话、不发消息、不产生新会话** ——
所以跑它对账号是零痕迹的。

代价
----
本地存储隔离到临时目录（不碰真实对话库）；服务端**零新增**。

用法
----
    cd engine
    ./.venv/Scripts/python.exe -X utf8 tools/verify_remote_sessions.py
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
    cfg.identity.account_label = "会话接管验证"
    cfg.pacing.base_gap_ms = 0.0
    cfg.pacing.floor_gap_ms = 0.0
    cfg.pacing.active_start_hour = 0
    cfg.pacing.active_end_hour = 24
    cfg.prompt.template_file = str(tmp / "system-prompt.md")
    cfg.memory.file = str(tmp / "memory.md")
    return cfg


async def scenario() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="gad-remote-"))
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

    try:
        # ---------- 1. 列出账号下的服务端会话 ----------
        log("\n[1] list_remote_sessions · 读取账号下的会话（只读）")
        res = await rt.list_remote_sessions(count=50)
        if res.get("error"):
            check("读取会话列表", False, str(res["error"]))
            return
        check("读取会话列表无错误", True)
        items = res["items"]
        log(f"     共 {len(items)} 条会话")
        for it in items[:8]:
            log(f"       · {it['remote_id'][:12]}…  "
                f"{'[已接管]' if it['bound'] else '[未接管]'}  {it['title'][:40]}")
        check("列表条目字段完整",
              all({"remote_id", "title", "bound", "local_id"} <= set(it) for it in items),
              "有条目缺字段")
        check("未接管时 local_id 为空",
              all(not it["local_id"] for it in items if not it["bound"]))

        if not items:
            log("\n（账号下没有会话，接管环节跳过）")
            return

        # ---------- 2. 接管第一条 ----------
        target = items[0]
        rid = target["remote_id"]
        log(f"\n[2] attach_session · 接管 {rid[:12]}…（只读：拉历史，不发消息）")
        r = await rt.attach_session(rid)
        check("接管成功", r.get("attached") is True, str(r))
        if not r.get("attached"):
            return

        conv = rt.conversation
        log(f"     对话标题「{conv.title}」 · 消息 {len(conv.messages)} 条 · "
            f"锚点 {conv.session.rid}")

        check("cid 指向被接管的会话", conv.session.cid == rid,
              f"{conv.session.cid} != {rid}")
        check("标记为接管（不自动轮换）", conv.session.attached is True)
        check("续接锚点非空", bool(conv.session.rid), "锚点为空")
        check("锚点与服务端 current_message_id 一致",
              str(conv.session.rid) == str(r.get("anchor")),
              f"{conv.session.rid} != {r.get('anchor')}")
        check("历史已归一化（无系统提示词混入正文）",
              all("你是" not in m.text[:20] for m in conv.messages),
              "首条消息疑似仍带系统提示词")

        # ---------- 3. 接管后：输入进对会话 ----------
        log("\n[3] 接管后的发送目标 · 不进网络，只看会话三元组")
        check("下一轮会带这条 chat_session_id",
              rt.session.meta.cid == rid, rt.session.meta.cid)
        check("下一轮 parent 是校正后的锚点",
              rt.session.meta.parent_message_id == (conv.session.rid or None),
              str(rt.session.meta.parent_message_id))
        rotated = rt.gates.rotate_if_needed()
        check("接管的会话不自动轮换", rotated is None, str(rotated))

        # ---------- 4. 幂等 ----------
        log("\n[4] 重复接管 · 应退化成「切过去」")
        r2 = await rt.attach_session(rid)
        check("第二次是复用", r2.get("reused") is True, str(r2))
        check("复用同一条本地对话", r2.get("id") == conv.id,
              f"{r2.get('id')} != {conv.id}")

        # ---------- 5. 绑定识别 ----------
        log("\n[5] 重新列一次 · 应能认出已接管")
        res2 = await rt.list_remote_sessions(count=50)
        again = next((i for i in res2["items"] if i["remote_id"] == rid), None)
        check("列表里找得到这条", again is not None)
        if again:
            check("标为已接管", again["bound"] is True)
            check("指向本地对话", again["local_id"] == conv.id, again["local_id"])
            check("标为当前对话", again["active"] is True)
    finally:
        await rt.stop()


def main() -> int:
    log("=" * 70)
    log("端到端 · 读取网页版会话与接管（只读，不发送）")
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
