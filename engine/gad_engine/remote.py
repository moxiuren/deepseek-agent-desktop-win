"""服务端历史 ↔ 本地对话的映射层。

为什么需要单独一层
------------------
`store.py` 里的对话是**本地视角**的：「用户输入了什么、助手回了什么」。
而服务端 `chat/history_messages` 返回的是**请求视角**的 —— 每条消息带
`message_id` / `parent_id` / `fragments`，且**第 1 条 USER 消息的 content
是完整的系统提示词**（`agent.py::_bootstrap_prompt()` 的产物）。

直接把服务端消息塞进界面，会得到一串「你是一个运行在桌面客户端里的本地代理…」
的气泡 —— 那不是用户说过的话。所以这一层负责三件事：

1. **提取正文** —— 只取 `fragments` 里 `type ∈ {REQUEST, RESPONSE}` 的 content。
   `THINK` / `SEARCH` / `TOOL_*` 一律丢弃。正文里混进「Found 12 web pages」
   是本项目实测踩过的坑（见 `transport/base.py` 里 `TurnOutcome` 的注释）。
2. **剥离系统提示词** —— 按 `\n\n# 任务\n` 切开，取后半段当用户输入。
   这个分隔符由 `agent.py` 的 `f"{prompt}\\n\\n# 任务\\n{goal}"` 保证稳定。
3. **合并本地元数据** —— 服务端只存文本；本地的 `tool_calls` / `thoughts` /
   `stop_reason` 不能因为「同步了一次」就丢掉。按 `rid` 匹配继承。

类型约定（这条踩过坑，写死在这里）
----------------------------------
服务端的 `message_id` 是 **int**，而 `SessionMeta.rid` 是 **str**。
本模块**统一输出 str** —— 因为 `deepseek_rpc.py` 构造请求时用的是
`int(x) if str(x).isdigit() else None`：一旦拿到非数字的 rid，它会**静默**
把 `parent_message_id` 置成 `None`，服务端因此新建一个 root，上下文被悄悄
断掉且不报错。统一成 str 之后，「字符串 ↔ int」的转换点在整个项目里只剩一处。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .store import Message

#: 系统提示词与用户输入之间的分隔符。
#: **必须与 `agent.py::_bootstrap_prompt()` 的拼接格式完全一致** ——
#: 改那边就要改这里，否则服务端历史里的用户输入会带着一大段系统提示词。
TASK_SEP = "\n\n# 任务\n"

#: 计入正文的片段类型。白名单而非黑名单：实测类型有
#: `REQUEST` / `RESPONSE` / `THINK` / `SEARCH` / `TOOL_OPEN` / `TOOL_SEARCH`，
#: 且服务端用的是 `THINK` 而不是 `THINKING` —— 按「非 THINKING 即正文」的
#: 黑名单写法会把后四种全吞进正文。
BODY_FRAG_TYPES = ("RESPONSE", "REQUEST")

#: 服务端角色 → 本地角色。
_ROLE_MAP = {"USER": "user", "ASSISTANT": "assistant"}


@dataclass
class RemoteHistory:
    """一次服务端历史读取的归一化结果。"""

    messages: list[Message] = field(default_factory=list)
    #: 续接锚点 —— `chat_session.current_message_id`（末条 message_id）。
    #: 切回旧会话时，下一轮的 `parent_message_id` 就该用它。
    anchor: str = ""
    #: 从第 1 条 USER 消息里剥离出来的系统提示词（没有就为空）。
    system_prompt: str = ""
    #: 服务端原始消息条数（诊断用：和 len(messages) 对不上就说明有消息被丢了）。
    raw_count: int = 0
    #: 会话标题（服务端侧的）。
    title: str = ""
    #: 跳过的片段类型计数（诊断用）。
    skipped_frag_types: dict[str, int] = field(default_factory=dict)


def split_system_and_task(content: str) -> tuple[str, str]:
    """把「系统提示词 + 任务」切成两半。

    没有分隔符时退化为 `("", content)` —— 那是**用户直接发的**消息
    （第 2 轮之后的工具结果、或在网页版里手打的），本就没有系统提示词。
    """
    if TASK_SEP in content:
        head, _, tail = content.partition(TASK_SEP)
        return head, tail
    return "", content


def extract_text(msg: dict, skipped: dict[str, int] | None = None) -> str:
    """从一条服务端消息里取出正文。"""
    parts: list[str] = []
    for frag in msg.get("fragments") or []:
        if not isinstance(frag, dict):
            continue
        ftype = str(frag.get("type") or "")
        if ftype in BODY_FRAG_TYPES:
            content = frag.get("content")
            if isinstance(content, str) and content:
                parts.append(content)
        elif skipped is not None and ftype:
            skipped[ftype] = skipped.get(ftype, 0) + 1
    return "".join(parts)


def normalize_history(biz_data: dict) -> RemoteHistory:
    """把 `chat/history_messages` 的 `biz_data` 归一化成本地形态。

    **不修改入参**，纯函数 —— 这样它能被单测用录制样本直接喂。
    """
    out = RemoteHistory()
    session = biz_data.get("chat_session") or {}
    out.title = str(session.get("title") or "")
    anchor = session.get("current_message_id")
    out.anchor = str(anchor) if anchor is not None else ""

    raw = biz_data.get("chat_messages") or []
    out.raw_count = len(raw)

    for i, msg in enumerate(raw):
        if not isinstance(msg, dict):
            continue
        role = _ROLE_MAP.get(str(msg.get("role") or "").upper(), "")
        if not role:
            continue

        text = extract_text(msg, out.skipped_frag_types)
        if not text.strip():
            continue

        # 只有**第 1 条 USER 消息**可能是「系统提示词 + 任务」的拼接体。
        # 后续 USER 消息是工具结果，不含系统提示词 —— 对它们做 partition
        # 是白费，而且万一工具输出里恰好含这个串就会被切错。
        if i == 0 and role == "user":
            head, tail = split_system_and_task(text)
            if head:
                out.system_prompt = head
                text = tail
                if not text.strip():
                    # 只发了系统提示词、还没有任务 —— 这条不进历史，
                    # 但锚点仍要保留（下一轮就接在它后面）。
                    continue

        ts = msg.get("inserted_at")
        out.messages.append(
            Message(
                role=role,
                text=text,
                ts=float(ts) if isinstance(ts, (int, float)) else 0.0,
                cid=str(session.get("id") or ""),
                # 统一成 str —— 见模块头「类型约定」。
                rid=str(msg.get("message_id")) if msg.get("message_id") is not None else "",
            )
        )

    return out


def merge_with_local(remote: list[Message], local: list[Message]) -> list[Message]:
    """把本地元数据合并进服务端历史。

    规则（服务端是**文本**权威源，本地是**元数据**权威源）：

    * 以服务端消息序列为骨架 —— 它含网页版里聊过的内容，本地没有
    * 每条 assistant 消息若本地存在 `rid` 相同的记录，**继承**其
      `tool_calls` / `thoughts` / `stop_reason` / `error`
    * 本地有、服务端没有的消息（例如某轮发送失败、从未落服务端）→
      **保留在末尾**。删掉就是伪造历史：那些消息用户确实在界面上看到过。

    返回**新列表**，不修改入参。
    """
    by_rid = {m.rid: m for m in local if m.rid}
    remote_rids = {rm.rid for rm in remote if rm.rid}
    merged: list[Message] = []

    for rm in remote:
        src = by_rid.get(rm.rid) if rm.rid else None
        if src is None:
            merged.append(rm)
            continue
        merged.append(
            Message(
                role=rm.role,
                text=rm.text,          # 文本以服务端为准
                ts=rm.ts,
                cid=rm.cid or src.cid,
                rid=rm.rid,
                error=src.error,
                tool_calls=src.tool_calls,
                turns=src.turns,
                stop_reason=src.stop_reason,
                thoughts=src.thoughts,
            )
        )

    # 服务端没有、本地有的 —— 追加在末尾，保持原相对顺序。
    # 判据是「这条的 rid 在不在服务端」，不用另外维护一个 used 集合。
    for lm in local:
        if lm.rid and lm.rid in remote_rids:
            continue
        merged.append(lm)

    return merged
