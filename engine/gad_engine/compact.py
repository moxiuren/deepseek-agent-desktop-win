"""上下文压缩 —— 换会话时的「接力摘要」。

它解决什么
----------

用户主动要求「换一条新会话继续」时，新会话在服务端是**一张白纸**
（`chat_session_id` 隔离，见 `memory.py` 的说明）。什么都不做的话，换过去就是
彻底失忆 —— 而这恰恰是用户点那个按钮时最不想要的结果。

所以本层只做一件事：把当前对话压成一份**可注入的摘要**，让新会话带着
「之前发生过什么」开工。

与「自动轮换」的关系
--------------------

`session.py` 的自动轮换（25 轮 / 20 分钟）是**静默**的：界面不变、模型失忆、
用户完全看不出发生了什么。本项目现在把「什么时候换会话」交给用户决定
（`PacingConfig.auto_rotate = False`），换的那一刻由本层负责**接上上下文**。
两者互补，不是一回事。

为什么默认用模型摘要（与 `memory.py` 的规矩不同）
-------------------------------------------------

`memory.py` 写着「刻意不做自动摘要、零额外请求」，那是**每轮都要跑**的东西。
而这里**用户按一次按钮才跑一次** —— 请求频率差三个数量级，防封约束的意义
完全不同。

而且这一轮请求对上游最友好：**发进当前会话**（服务端已持有完整上下文），
只发一句指令。不需要把整段历史重新拼进去，token 成本极低，也不会污染
新会话的会话树（旧会话随即弃用）。

失败一律降级到 `summarize_local()`（纯规则、零请求）。摘要拿不到不是致命
错误，但「换过去却什么都不记得」是 —— 所以本地兜底是必需的，不是可选项。
"""

from __future__ import annotations

import re

#: 摘要注入的默认上限（字符）。
#:
#: **必须有上限**：摘要是要注入到新会话**每一轮**提示词里的（与长期记忆同一
#: 处理方式）。没有上限的话，多代接力之后它会悄悄把提示词挤爆，症状是
#: 「模型开始变笨」，而没人会想到是摘要太长。
DEFAULT_MAX_CHARS = 1500

#: 发进当前会话的摘要指令。
#:
#: 写法上有三个刻意的选择：
#:   1. **明确说「只输出摘要」** —— 否则模型很可能先来一句「好的，我来总结一下」，
#:      那段寒暄会被一起注入到新会话，纯属噪声。
#:   2. **给固定结构** —— 自由文本的摘要会漂移（这一代有【目标】、下一代没了），
#:      而结构化条目对「接着往下做」这个用途最有用。
#:   3. **点明用途是交接** —— 让模型知道这份摘要会被拿到另一个会话里读，
#:      它就会保留「未决问题」这类只有交接才需要的字段。
SUMMARY_PROMPT = """\
请把我们到目前为止的这段对话压缩成一份**交接摘要**，我会拿着它在一个全新的
对话里继续。要求：

- **只输出摘要本身**：不要寒暄、不要复述我的原话、不要向我提问、不要输出工具调用。
- 严格按下面的结构，条目尽量短：

【目标】
（我们最终要达成什么，一两句话）

【已确认的事实与结论】
（已经查实、可以直接当作前提的东西）

【已经做过的操作】
（跑过什么命令、改过哪些文件、得到什么结果）

【还没解决的问题】
（悬而未决、下一步要接着做的）

- 总量控制在 600 字以内。如果某个部分确实没有内容，写「（无）」。
"""

#: 从模型回复里提取摘要时，剥掉它可能套上的代码围栏。
_FENCE_BLOCK_RE = re.compile(r"^```[a-zA-Z0-9_-]*\s*\n(.*?)\n?```\s*$", re.DOTALL)

#: 模型爱加的开场白（「好的，我来总结…」）。只剥**开头这一句**，不贪心 ——
#: 摘要正文里出现这些词是正常的。
_PREAMBLE_RE = re.compile(
    r"^(?:好的|好[，,]|明白|收到|没问题)[^\n]{0,40}(?:摘要|总结|汇总)[^\n]{0,20}[：:，,]?\s*\n+",
)

#: 工具调用围栏（`protocol.py` 里那两个）。摘要是要读的，不该带这些 ——
#: 命令与路径已经**结构化地**列在【已经做过的操作】里了，正文再留一份纯属噪声。
_CMD_FENCE_RE = re.compile(r"```local_cmd(?::async)?\s*\n(.*?)```", re.DOTALL)
_WRITE_FENCE_BLOCK_RE = re.compile(r"```write_file\s*:[^\n]*\n.*?```", re.DOTALL)
_WRITE_FENCE_RE = re.compile(r"```write_file\s*:\s*([^\n`]+)", re.IGNORECASE)

#: 看起来像「路径」的串（Windows 盘符 / UNC / 正斜杠绝对路径）。
#:
#: 字符集里**排除中文标点**：路径后面紧跟「，其余用例全绿」这种中文时，
#: 若不排除就会把整句吃进路径里（实测踩到）。
_PATH_RE = re.compile(
    r"(?:[A-Za-z]:[\\/][^\s\"'`|<>*?，。；、（）【】《》]+"
    r"|\\\\[^\s\"'`|<>*?，。；、（）【】《》]+"
    r"|/(?:home|Users|tmp|var|opt)/[^\s\"'`|<>*?，。；、（）【】《》]+)"
)

#: 明显「不是摘要」的错误特征。用来挡住把上游报错当成摘要收下的情况 ——
#: 错误文本有时也挺长（`Server busy ... generation_timeout`），光看长度挡不住。
_ERROR_HINT_RE = re.compile(
    r"server\s*busy|generation_timeout|rate\s*limit|internal\s*error|too\s*many\s*requests",
    re.IGNORECASE,
)

#: 单条条目在本地摘要里的截断长度。
_ITEM_LEN = 160
_GOAL_LEN = 300
_TAIL_LEN = 500
_PRIOR_LEN = 400


def _oneline(text: str) -> str:
    """压成一行（摘要里不该出现换行，否则结构会被撑乱）。"""
    return " ".join((text or "").split())


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def strip_tool_fences(text: str) -> str:
    """去掉文本里的工具调用围栏。

    摘要的正文里不该出现 ````local_cmd ... ```` 这种东西 —— 它既不是给人读的，
    也和【已经做过的操作】那一段重复。
    """
    out = _CMD_FENCE_RE.sub(" ", text or "")
    out = _WRITE_FENCE_BLOCK_RE.sub(" ", out)
    # 兜底：模型输出被截断、围栏没闭合时，至少把标记本身去掉。
    out = _WRITE_FENCE_RE.sub(" ", out)
    return out


def _dedup(items: list[str], limit: int) -> list[str]:
    """去重（保持出现顺序）并限量。"""
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        key = item.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
        if len(out) >= limit:
            break
    return out


def extract_commands(messages) -> list[str]:
    """从对话里抽出**真正执行过**的命令（工具围栏里的内容）。

    为什么专门抽它：对工具型 agent 来说，「跑过哪些命令」是交接时最值钱的
    信息之一 —— 它既是「已经做过什么」，也是「别再重复做」的依据。而这类
    内容恰恰是自由文本摘要最容易漏掉的。
    """
    out: list[str] = []
    for msg in messages:
        for m in _CMD_FENCE_RE.finditer(msg.text or ""):
            body = _oneline(m.group(1))
            if body:
                out.append(body)
    return _dedup(out, 12)


def extract_paths(messages) -> list[str]:
    """抽出被写到 / 提到的文件路径（去重、限量）。

    只抽 `write_file:` 围栏的目标 + 正文里像绝对路径的串。刻意不做「像不像
    文件」的启发式判断 —— 宁少勿错，摘要里塞进误报比漏一条更糟。
    """
    out: list[str] = []
    for msg in messages:
        text = msg.text or ""
        for m in _WRITE_FENCE_RE.finditer(text):
            out.append(m.group(1).strip())
        for m in _PATH_RE.finditer(text):
            out.append(m.group(0).strip())
    return _dedup(out, 12)


def summarize_local(
    messages,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    prior_summary: str = "",
) -> str:
    """纯规则的本地摘要（零请求）。

    它不试图「理解」对话，只做**结构化抽取**：谁提出了什么、跑过什么、
    最后得出什么。这是刻意的取舍 —— 规则摘要的质量上限不高，但它的下限
    很稳（不会跑偏、不会凭空编造），而且零请求零风险。

    它同时是模型摘要失败时的兜底，所以**必须能独立工作**。

    `prior_summary` 是上一个对话传下来的摘要。多代接力时如果不带上它，
    第一代的内容会在第二代被本地摘要时丢掉 —— 因为本地摘要只看得到
    `messages`，而更早的历史早已不在其中。
    """
    users = [m for m in (messages or []) if m.role == "user" and (m.text or "").strip()]
    assistants = [
        m for m in (messages or []) if m.role == "assistant" and (m.text or "").strip()
    ]

    # 一条有效消息都没有 → **返回空串**，不要产出一份「【目标】（未记录）」。
    # 空串会让调用方明确地拒绝这次接力（见 `handoff_conversation`），
    # 而一份只有标题没有内容的摘要会被当成有效结果注入新会话 ——
    # 既没用，又让人以为接力成功了。
    if not users and not assistants:
        return ""

    lines: list[str] = []

    if prior_summary.strip():
        lines.append("【更早的背景（上一段对话的摘要）】")
        lines.append(_clip(_oneline(prior_summary), _PRIOR_LEN))
        lines.append("")

    lines.append("【目标】")
    lines.append(_clip(_oneline(users[0].text), _GOAL_LEN) if users else "（未记录）")

    if len(users) > 1:
        lines.append("")
        lines.append("【用户先后提出的要求】")
        for msg in users[1:][-8:]:
            lines.append(f"- {_clip(_oneline(msg.text), _ITEM_LEN)}")

    commands = extract_commands(messages)
    paths = extract_paths(messages)
    if commands or paths:
        lines.append("")
        lines.append("【已经做过的操作】")
        for cmd in commands:
            lines.append(f"- 执行：{_clip(cmd, _ITEM_LEN)}")
        for path in paths:
            lines.append(f"- 涉及文件：{_clip(path, _ITEM_LEN)}")

    if assistants:
        lines.append("")
        lines.append("【最近一轮的结论】")
        # 结论取**最后一条** assistant 正文 —— 它通常是「这一轮做完了什么」。
        # 剥掉工具围栏：那些内容已经在上面结构化列过了，留在这里既重复又难读。
        tail = _oneline(strip_tool_fences(assistants[-1].text))
        lines.append(_clip(tail, _TAIL_LEN))

    out = "\n".join(lines).strip()
    if not out:
        return ""
    if len(out) > max_chars:
        out = out[:max_chars].rstrip() + "\n（摘要过长，已截断。）"
    return out


def extract_summary_reply(text: str) -> str:
    """把模型回复清理成摘要正文。

    只做三件事，都是「模型不听话」的常见形态：
      * 剥掉整段套的代码围栏（模型很喜欢把结构化文本包起来）
      * 剥掉开头一句寒暄（「好的，我来总结一下」）
      * 去掉尾部残留的工具围栏（万一它顺手输出了一个）

    **不做内容改写** —— 一旦开始「猜哪句是摘要」，就可能把正文吃掉。
    """
    body = (text or "").strip()
    if not body:
        return ""

    m = _FENCE_BLOCK_RE.match(body)
    if m:
        body = m.group(1).strip()

    body = _PREAMBLE_RE.sub("", body, count=1)
    body = _CMD_FENCE_RE.sub("", body)
    body = _WRITE_FENCE_RE.sub("", body)

    return body.strip()


def looks_like_summary(text: str) -> bool:
    """粗判一段文本「像不像摘要」。

    只用来挡住明显不是摘要的回复（报错文本、空回复、只有一句「抱歉」）。
    判据**宽松** —— 宁可放过一个质量差的摘要，也不要因为判据太严而白白降级
    （降级到本地摘要意味着用户那次点击的模型质量白费了）。
    """
    body = (text or "").strip()
    if len(body) < 24:
        return False
    # 上游报错有时也挺长（`Server busy ... generation_timeout`），光看长度挡不住。
    if _ERROR_HINT_RE.search(body):
        return False
    # 结构标记命中任意一个 → 它确实按我们给的格式输出了。
    if any(mark in body for mark in ("【", "##", "目标", "结论", "摘要")):
        return True
    # 没有结构标记但够长：仍然接受。判据宁宽勿严 —— 一份自由格式但内容完整的
    # 摘要，比「因为没写方括号就丢掉、退回本地规则版」有价值得多。
    return len(body) >= 40
