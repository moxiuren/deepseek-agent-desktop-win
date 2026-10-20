"""服务端会话读取与接管的接线自测。

覆盖本轮新增的两条能力 —— 用假 Transport，**不发任何真实请求**：

1. `list_remote_sessions()` —— 把账号下的服务端会话列出来（含用户在网页版里
   建的），并标出哪些已经被本地接管
2. `attach_session()` —— 接管一条网页版会话：拉全量历史、剥离系统提示词、
   用 `current_message_id` **校正续接锚点**、标记 `attached`（不自动轮换）

核心断言是那句需求：「**确保我现在的输入是进去那个会话**」——
接管之后 `send()` 带的 `cid` 必须是那条服务端会话，`parent` 必须是校正后的锚点。

用法：
    cd engine && ./.venv/Scripts/python.exe tests/test_remote_sessions.py
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
from gad_engine.store import ConversationStore
from gad_engine.transport.base import (
    RemoteStatus,
    Transport,
    TransportError,
    TurnOutcome,
)

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    mark = "通过" if cond else "失败"
    extra = f" —— {detail}" if detail and not cond else ""
    print(f"  [{mark}] {name}{extra}")


class FakeRemoteTransport(Transport):
    """假传输层：会话列表与历史都由构造参数喂，`send` 回显收到的 cid。

    `send` 回显 cid 是关键 —— 真实传输层会把 cid 放进请求体，这里把它记下来，
    断言「这一轮到底发进了哪条会话」。
    """

    name = "fake-remote"
    supports_streaming = True

    def __init__(
        self,
        sessions: list[dict] | None = None,
        history: dict[str, dict] | None = None,
        fail_list: bool = False,
        fail_history: bool = False,
        fail_delete: bool = False,
    ) -> None:
        self._sessions = list(sessions or [])
        self._history = dict(history or {})
        self._fail_list = fail_list
        self._fail_history = fail_history
        self._fail_delete = fail_delete
        self.sent: list[str] = []
        self.calls: list[dict] = []
        self.deleted: list[str] = []

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def health(self) -> RemoteStatus:
        return RemoteStatus(reachable=True, account_status_short="正常")

    async def list_sessions(self, count: int = 30) -> list[dict]:
        if self._fail_list:
            raise TransportError("读不到会话列表", config=True)
        return list(self._sessions)

    async def fetch_history(self, chat_session_id: str) -> dict:
        if self._fail_history:
            raise TransportError("读不到历史", config=True)
        return self._history.get(chat_session_id, {})

    async def delete_session(self, chat_session_id: str) -> None:
        """记录被删的 id，并从假列表里摘掉 —— 便于断言「服务端真的少了一条」。"""
        if self._fail_delete:
            raise TransportError("删不掉", config=True)
        self.deleted.append(chat_session_id)
        self._sessions = [s for s in self._sessions if s.get("id") != chat_session_id]

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
        idx = len(self.sent)
        self.sent.append(prompt)
        self.calls.append(
            {
                "cid": session.cid,
                "parent": session.parent_message_id,
                "attached": session.attached,
            }
        )
        if on_delta is not None:
            result = on_delta("收到")
            if inspect.isawaitable(result):
                await result
        return TurnOutcome(
            text="收到",
            cid=session.cid or f"cid-{idx + 1}",
            rid=str(900 + idx),
            rcid=str(800 + idx),
        )


def make_cfg(tmp: Path) -> EngineConfig:
    cfg = EngineConfig()
    # L1 闸门要求声明专用账号标识，否则一切提交被拦（刻意的设计）。
    cfg.identity.account_label = "测试账号"
    # 关掉节奏等待，让测试秒级跑完。
    cfg.pacing.base_gap_ms = 0.0
    cfg.pacing.floor_gap_ms = 0.0
    cfg.pacing.active_start_hour = 0
    cfg.pacing.active_end_hour = 24
    # 单小时硬顶（默认 20）一并让开：默认值下多轮场景会变成「等窗口滑出 ≈1 小时」。
    cfg.pacing.hourly_max = 0
    # 隔离磁盘路径：模板与记忆默认落在真实 AppData，测试绝不能碰。
    cfg.prompt.template_file = str(tmp / "system-prompt.md")
    cfg.memory.file = str(tmp / "memory.md")
    return cfg


def history_for(cid: str, title: str, anchor: int, msgs: list[tuple[str, str]]) -> dict:
    """造一份 `chat/history_messages` 的 `biz_data`（形状照实测的来）。"""
    out = []
    for i, (role, text) in enumerate(msgs, start=1):
        out.append(
            {
                "message_id": i,
                "parent_id": (i - 1) or None,
                "role": role,
                "inserted_at": 1_700_000_000 + i,
                "fragments": [
                    {
                        "type": "REQUEST" if role == "USER" else "RESPONSE",
                        "content": text,
                    }
                ],
            }
        )
    return {
        "chat_session": {"id": cid, "title": title, "current_message_id": anchor},
        "chat_messages": out,
        "cache_control": "REPLACE",
        "cache_reset_at": 1792072078,
    }


SESSIONS = [
    {"id": "sess-a", "title": "网页版：上海天气", "updated_at": 1_700_000_100, "pinned": False},
    {"id": "sess-b", "title": "网页版：周报", "updated_at": 1_700_000_200, "pinned": True},
]

HISTORY = {
    "sess-a": history_for(
        "sess-a",
        "网页版：上海天气",
        42,
        [
            ("USER", "你是本地代理。\n\n# 任务\n上海今天天气"),
            ("ASSISTANT", "今天多云，26 度"),
        ],
    ),
    "sess-b": history_for(
        "sess-b",
        "网页版：周报",
        7,
        [("USER", "帮我写周报"), ("ASSISTANT", "好的，本周完成……")],
    ),
}


async def scenario(tmp: Path) -> None:
    store = ConversationStore(tmp / "conversations")
    tp = FakeRemoteTransport(SESSIONS, HISTORY)
    rt = AgentRuntime(make_cfg(tmp), transport=tp, store=store)

    # ---------- 1. 列出服务端会话 ----------
    print("\n[1] list_remote_sessions · 账号下的会话都能读到")
    res = await rt.list_remote_sessions()
    check("无错误", res["error"] == "", res["error"])
    check("列出 2 条", len(res["items"]) == 2, str(len(res["items"])))
    a = next(i for i in res["items"] if i["remote_id"] == "sess-a")
    check("标题来自服务端", a["title"] == "网页版：上海天气", a["title"])
    check("未接管时 bound=False", a["bound"] is False)
    check("未接管时 local_id 为空", a["local_id"] == "", a["local_id"])

    # ---------- 2. 接管 ----------
    print("\n[2] attach_session · 拉全量历史 + 校正锚点")
    r = await rt.attach_session("sess-a")
    check("接管成功", r["attached"] is True, str(r))
    check("不是复用", r["reused"] is False)
    conv = rt.conversation
    check("对话标题来自服务端", conv.title == "网页版：上海天气", conv.title)
    check("消息条数=服务端条数", len(conv.messages) == 2, str(len(conv.messages)))
    check(
        "系统提示词已剥离（只留用户输入）",
        conv.messages[0].text == "上海今天天气",
        conv.messages[0].text,
    )
    check("助手正文正确", conv.messages[1].text == "今天多云，26 度", conv.messages[1].text)
    check("cid 指向服务端会话", conv.session.cid == "sess-a", conv.session.cid)
    check("锚点校正为 current_message_id", conv.session.rid == "42", conv.session.rid)
    check("标记为接管", conv.session.attached is True)
    check("系统提示词被记录", r["system_prompt_chars"] > 0, str(r["system_prompt_chars"]))

    # ---------- 3. 接管后输入进对会话（核心需求）----------
    print("\n[3] 接管后发消息 · 必须进那条 chat_session + 正确 parent")
    await rt.run("再帮我查一下明天")
    call = tp.calls[-1]
    check("发送用的 cid 是接管的会话", call["cid"] == "sess-a", str(call["cid"]))
    check("parent 是校正后的锚点", call["parent"] == "42", str(call["parent"]))

    # ---------- 4. attached 不自动轮换 ----------
    print("\n[4] attached 会话不自动轮换（否则输入会进空白新会话）")
    rt.session.meta.turns = 999
    rt.session.meta.created_at -= 10_000
    rotated = rt.gates.rotate_if_needed()
    check("接管的会话不轮换", rotated is None, str(rotated))
    check("cid 保持不变", rt.session.meta.cid == "sess-a", rt.session.meta.cid)

    # ---------- 5. 幂等 ----------
    print("\n[5] 重复接管 · 退化成切过去，不重复导入")
    n_after_first = len(store.list_summaries())
    r2 = await rt.attach_session("sess-a")
    check("第二次是复用", r2["reused"] is True, str(r2))
    check("id 与第一次相同", r2["id"] == conv.id, f"{r2['id']} != {conv.id}")
    check(
        "本地对话数没变",
        len(store.list_summaries()) == n_after_first,
        f"{len(store.list_summaries())} != {n_after_first}",
    )

    # ---------- 6. 绑定识别 ----------
    print("\n[6] 列表里能认出已接管的会话")
    res2 = await rt.list_remote_sessions()
    a2 = next(i for i in res2["items"] if i["remote_id"] == "sess-a")
    b2 = next(i for i in res2["items"] if i["remote_id"] == "sess-b")
    check("sess-a 已接管", a2["bound"] is True)
    check("sess-a 指向本地对话", a2["local_id"] == conv.id, a2["local_id"])
    check("sess-a 是当前对话", a2["active"] is True)
    check("sess-b 仍未接管", b2["bound"] is False)

    # ---------- 7. 持久化 ----------
    print("\n[7] 重启后 attached 与锚点仍在")
    rt2 = AgentRuntime(
        make_cfg(tmp),
        transport=FakeRemoteTransport(SESSIONS, HISTORY),
        store=ConversationStore(tmp / "conversations"),
    )
    check("重启后仍是接管态", rt2.conversation.session.attached is True)
    check("重启后 cid 还在", rt2.conversation.session.cid == "sess-a",
          rt2.conversation.session.cid)
    check("重启后锚点还在", rt2.session.meta.rid != "", rt2.session.meta.rid)
    check("重启后不自动轮换", rt2.gates.rotate_if_needed() is None)

    # ---------- 8. 失败降级 ----------
    print("\n[8] 读不到时不抛，返回原因")
    rt3 = AgentRuntime(
        make_cfg(tmp),
        transport=FakeRemoteTransport(fail_list=True, fail_history=True),
        store=ConversationStore(tmp / "conv-fail"),
    )
    res3 = await rt3.list_remote_sessions()
    check("列表失败返回 error 且 items 为空",
          res3["error"] != "" and res3["items"] == [], str(res3))
    r3 = await rt3.attach_session("sess-x")
    check("接管失败返回 attached=False", r3["attached"] is False, str(r3))
    check("失败带原因", bool(r3.get("reason")), str(r3))

    # ---------- 9. 空 id ----------
    print("\n[9] 空 id 直接拒绝，不误伤")
    r4 = await rt.attach_session("")
    check("空 id 返回 attached=False", r4["attached"] is False, str(r4))

    # ---------- 10. 删除已接管的服务端会话 ----------
    #
    # 这是 2026-10-18 新增的能力（`chat_session/delete` 实测可用）。
    # 关键不是「删掉了」，而是**删完本地不能留一个指向死 id 的绑定** ——
    # 那种状态下一轮 `send()` 会带死 cid 发出去，服务端回
    # `invalid chat session id`，用户看到的是「突然发不出消息」。
    print("\n[10] 删除已接管的会话 · 服务端删掉 + 本地解绑")
    bound_conv = rt.store.find_by_session_cid("sess-a")
    check("删除前 sess-a 有本地绑定", bound_conv is not None)
    bound_id = bound_conv.id if bound_conv else ""
    d1 = await rt.delete_remote_session("sess-a")
    check("返回 deleted=True", d1["deleted"] is True, str(d1))
    check("返回 unbound=True", d1["unbound"] is True, str(d1))
    check("返回的 local_id 指向原对话", d1["local_id"] == bound_id, str(d1))
    check("传输层真的收到了删除调用", tp.deleted == ["sess-a"], str(tp.deleted))

    after = rt.store.find_by_session_cid("sess-a")
    check("本地绑定已解除（按 cid 查不到了）", after is None)
    revived = rt.store.get(bound_id)
    check("本地对话本体还在（只解绑，不删文件）", revived is not None)
    check("解绑后 cid 为空", revived is not None and revived.session.cid == "",
          revived.session.cid if revived else "(无)")
    check("解绑后 attached 复位", revived is not None and revived.session.attached is False)

    # 删掉的若是当前对话，引擎手里那份对象也必须同步 —— 否则下一轮
    # `send()` 用的还是带死 cid 的旧对象。
    if rt.conversation is not None and rt.conversation.id == bound_id:
        check("当前对话对象的 cid 也已清空", rt.conversation.session.cid == "",
              rt.conversation.session.cid)
        check("会话层 meta 同步清空", rt.session.meta.cid == "", rt.session.meta.cid)

    res_after = await rt.list_remote_sessions()
    check("删除后远端列表里没有 sess-a",
          all(i["remote_id"] != "sess-a" for i in res_after["items"]),
          str([i["remote_id"] for i in res_after["items"]]))

    # ---------- 11. 删除未接管的服务端会话 ----------
    print("\n[11] 删除未接管的会话 · 只删服务端，不动本地")
    d2 = await rt.delete_remote_session("sess-b")
    check("返回 deleted=True", d2["deleted"] is True, str(d2))
    check("返回 unbound=False（本来就没绑定）", d2["unbound"] is False, str(d2))
    check("local_id 为空", d2["local_id"] == "", str(d2))
    check("传输层收到 sess-b", tp.deleted == ["sess-a", "sess-b"], str(tp.deleted))

    # ---------- 12. 删除失败 / 空 id ----------
    print("\n[12] 删除失败不抛、空 id 直接拒")
    rt_fail = AgentRuntime(
        make_cfg(tmp),
        transport=FakeRemoteTransport(SESSIONS, HISTORY, fail_delete=True),
        store=ConversationStore(tmp / "conv-del-fail"),
    )
    d3 = await rt_fail.delete_remote_session("sess-a")
    check("失败返回 deleted=False", d3["deleted"] is False, str(d3))
    check("失败带原因", bool(d3["reason"]), str(d3))
    d4 = await rt.delete_remote_session("")
    check("空 id 返回 deleted=False", d4["deleted"] is False, str(d4))
    check("空 id 没打到传输层", tp.deleted == ["sess-a", "sess-b"], str(tp.deleted))


def main() -> int:
    print("=" * 70)
    print("编排层 · 服务端会话读取与接管自测")
    print("=" * 70)
    with tempfile.TemporaryDirectory(prefix="gad-remote-") as tmp:
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
