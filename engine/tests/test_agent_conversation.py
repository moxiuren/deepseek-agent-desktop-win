"""编排层的对话与记忆接线自测。

验证「真实 agent 架构」的三条主干 —— 用假 Transport，**不发任何真实请求**：

1. `run()` 把 user / assistant 消息落进当前对话，并带上服务端三元组
2. 新建对话会**轮换**服务端会话；切回旧对话能把三元组**恢复**回去
   （这是「换对话还能接着聊」与「重启不丢上下文」的共同实现点 ——
   少了 rid，`parent_message_id` 就是 None，服务端会当成新会话的根）
3. 长期记忆注入系统提示词，且能被配置关掉

用法：
    cd engine && ./.venv/Scripts/python.exe tests/test_agent_conversation.py
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
    """只记录、不联网。会话 id 递增，模拟服务端每轮给新的 response id。"""

    name = "fake"
    supports_streaming = True

    def __init__(self, replies: list[str] | None = None) -> None:
        self.replies = list(replies or [])
        self.sent: list[str] = []
        self.calls: list[dict] = []

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
        idx = len(self.sent)
        self.sent.append(prompt)
        self.calls.append(
            {
                "cid": session.cid,
                "rid": session.rid,
                "parent": session.parent_message_id,
                "temporary": temporary,
                "thinking": extended_thinking,
                "search": search_enabled,
            }
        )
        text = self.replies.pop(0) if self.replies else f"第{idx + 1}轮回复"
        if on_delta is not None:
            result = on_delta(text)
            if inspect.isawaitable(result):
                await result
        return TurnOutcome(
            text=text,
            cid=f"cid-{idx + 1}",
            rid=f"rid-{idx + 1}",
            rcid=f"rcid-{idx + 1}",
        )


def make_cfg(
    template_file: Path | None = None,
    memory_file: Path | None = None,
) -> EngineConfig:
    cfg = EngineConfig()
    # L1 闸门要求声明专用账号标识，否则一切提交被拦（这是刻意的设计）。
    cfg.identity.account_label = "测试账号"
    # 关掉节奏等待，让测试秒级跑完 —— 间隔闸门本身由 test_gates 覆盖。
    cfg.pacing.base_gap_ms = 0.0
    cfg.pacing.floor_gap_ms = 0.0
    cfg.pacing.active_start_hour = 0
    cfg.pacing.active_end_hour = 24
    # 单小时硬顶（默认 20）一并让开：默认值下多轮场景会变成「等窗口滑出 ≈1 小时」。
    cfg.pacing.hourly_max = 0
    # 隔离磁盘路径：默认模板与记忆都落在真实 AppData 里，测试**绝不能**碰 ——
    # 那既会让断言随环境漂移，也会污染用户真实数据（本测试第一版就写脏过）。
    if template_file is not None:
        cfg.prompt.template_file = str(template_file)
    if memory_file is not None:
        cfg.memory.file = str(memory_file)
    return cfg


async def scenario(tmp: Path) -> None:
    # 模板文件**不预写** —— `seed_default` 会播种内置默认模板（含 {{memory}}），
    # 正好覆盖「模板里有占位符」这条路径。
    cfg = make_cfg(tmp / "system-prompt.md", tmp / "memory.md")
    store = ConversationStore(tmp / "conversations")
    tp = FakeTransport(["收到", "第二轮的回复"])
    rt = AgentRuntime(cfg, transport=tp, store=store)

    # ---------- 1. 消息落盘 ----------
    print("\n[1] run() 把消息落进当前对话")
    await rt.run("你好")
    conv = rt.conversation
    check("run 后对话有 2 条消息", len(conv.messages) == 2, str(len(conv.messages)))
    check("第 1 条是用户消息",
          conv.messages[0].role == "user" and conv.messages[0].text == "你好")
    check("第 2 条是助手消息", conv.messages[1].role == "assistant")
    check("助手正文正确", conv.messages[1].text == "收到", conv.messages[1].text)
    check("助手消息带 cid", conv.messages[1].cid == "cid-1", conv.messages[1].cid)
    check("助手消息带 rid", conv.messages[1].rid == "rid-1", conv.messages[1].rid)
    check("标题自动生成", conv.title == "你好", conv.title)
    check("三元组同步进对话对象", conv.session.cid == "cid-1", conv.session.cid)

    # ---------- 2. 同一对话内续接 ----------
    print("\n[2] 同一对话内续接（parent_message_id 要接上）")
    await rt.run("再问一句")
    check("第二轮后 4 条消息", len(rt.conversation.messages) == 4,
          str(len(rt.conversation.messages)))
    check("第二轮带上了上一轮的 rid",
          tp.calls[1]["parent"] == "rid-1", str(tp.calls[1]["parent"]))
    check("三元组推进到 cid-2", rt.conversation.session.cid == "cid-2",
          rt.conversation.session.cid)

    first_id = rt.conversation.id

    # ---------- 3. 新建对话 ----------
    print("\n[3] 新建对话 · 必须轮换服务端会话")
    new_conv = rt.new_conversation()
    check("新对话消息为空", len(new_conv["messages"]) == 0, str(len(new_conv["messages"])))
    check("新对话 id 与旧的不同", new_conv["id"] != first_id)
    check("新对话的服务端会话已轮换（cid 为空）",
          rt.session.meta.cid == "", rt.session.meta.cid)
    check("新对话成为当前对话", rt.conversation.id == new_conv["id"])

    await rt.run("新话题")
    check("新对话首轮 parent 是 None",
          tp.calls[2]["parent"] is None, str(tp.calls[2]["parent"]))

    # ---------- 4. 切回旧对话 ----------
    print("\n[4] 切回旧对话 · 三元组要恢复")
    loaded = rt.load_conversation(first_id)
    check("切回成功", loaded is not None and loaded["id"] == first_id)
    check("切回后消息完整", len(rt.conversation.messages) == 4,
          str(len(rt.conversation.messages)))
    check("切回后 cid 恢复", rt.session.meta.cid == "cid-2", rt.session.meta.cid)
    check("切回后 parent_message_id 能接上",
          rt.session.meta.parent_message_id == "rid-2",
          str(rt.session.meta.parent_message_id))

    await rt.run("接着聊")
    check("切回后新一轮用的是旧会话树",
          tp.calls[3]["cid"] == "cid-2", str(tp.calls[3]["cid"]))
    check("切回后新一轮的 parent 是旧 rid",
          tp.calls[3]["parent"] == "rid-2", str(tp.calls[3]["parent"]))

    # ---------- 5. 列表与重命名 ----------
    print("\n[5] 列表与重命名")
    items = rt.list_conversations()
    check("列表含 2 条对话", len(items) == 2, str(len(items)))
    check("列表标出唯一的当前对话",
          sum(1 for it in items if it.get("active")) == 1,
          str([it.get("active") for it in items]))
    check("列表项含 preview", all(it.get("preview") for it in items))

    renamed = rt.rename_conversation(first_id, "改过名的对话")
    check("重命名返回新标题",
          renamed is not None and renamed["title"] == "改过名的对话", str(renamed))
    check("重命名落盘", store.get(first_id).title == "改过名的对话")

    # ---------- 6. 删除 ----------
    print("\n[6] 删除对话")
    victim = rt.conversation.id
    res = rt.delete_conversation(victim)
    check("删除返回成功", res["deleted"] is True, str(res))
    check("删除当前对话后自动切走", rt.conversation.id != victim, rt.conversation.id)
    check("删除后列表剩 1 条", len(rt.list_conversations()) == 1,
          str(len(rt.list_conversations())))
    check("删除后引擎手里的对话仍然有效",
          store.get(rt.conversation.id) is not None)

    # ---------- 7. 长期记忆 ----------
    print("\n[7] 长期记忆")
    check("初始记忆为空", rt.read_memory() == "")
    rt.append_memory("用户叫小莫", section="关于用户")
    check("记忆写入成功", "用户叫小莫" in rt.read_memory())
    st = rt.memory_status()
    check("记忆状态含 enabled", st.get("enabled") is True, str(st))
    check("记忆状态含路径", bool(st.get("path")), str(st))

    await rt.run("测试记忆注入")
    prompt = tp.sent[-1]
    check("提示词含记忆内容", "用户叫小莫" in prompt)
    check("提示词含记忆标题", "# 长期记忆" in prompt)

    cfg.memory.inject = False
    await rt.run("关掉注入之后")
    check("关闭注入后提示词不含记忆", "用户叫小莫" not in tp.sent[-1])
    cfg.memory.inject = True

    cfg.memory.enabled = False
    await rt.run("整体关掉记忆")
    check("整体关闭后提示词不含记忆", "用户叫小莫" not in tp.sent[-1])
    cfg.memory.enabled = True

    # 情况 B：模板里没有 {{memory}} 占位符 —— 老用户手里那份就是。
    # 记忆是客户端的系统能力，不该因为模板是旧版就整个失效，所以要追加到末尾。
    (tmp / "system-prompt.md").write_text(
        "# 角色\n\n你是一个本地代理。\n\n{{protocol}}\n", encoding="utf-8"
    )
    await rt.run("模板里没有占位符")
    check("模板无占位符时记忆仍注入", "用户叫小莫" in tp.sent[-1])
    check("模板无占位符时记忆追加在末尾",
          tp.sent[-1].index("长期记忆") > tp.sent[-1].index("本地代理"),
          "记忆应在正文之后")

    # ---------- 8. 重启恢复 ----------
    print("\n[8] 重启恢复 · 换一个 runtime 实例读同一个目录")
    cfg2 = make_cfg(tmp / "system-prompt.md", tmp / "memory.md")
    tp2 = FakeTransport(["重启后的回复"])
    rt2 = AgentRuntime(cfg2, transport=tp2, store=ConversationStore(tmp / "conversations"))
    check("重启后拿回上次的对话", rt2.conversation.id == rt.conversation.id,
          f"{rt2.conversation.id} != {rt.conversation.id}")
    check("重启后消息还在", len(rt2.conversation.messages) > 0,
          str(len(rt2.conversation.messages)))
    check("重启后三元组恢复（能接着聊）",
          rt2.session.meta.cid == rt.session.meta.cid,
          f"{rt2.session.meta.cid} != {rt.session.meta.cid}")
    check("重启后记忆还在", "用户叫小莫" in rt2.read_memory())


def main() -> int:
    print("=" * 70)
    print("编排层 · 对话与记忆接线自测")
    print("=" * 70)
    with tempfile.TemporaryDirectory(prefix="gad-agent-") as tmp:
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
