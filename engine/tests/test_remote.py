"""服务端历史映射层自测。

样本取自 2026-10-15 的真实响应形态（`tools/probe_history_shape.py` 实测），
**内容脱敏**，只保留结构与类型特征 —— 这样断言不会随账号数据漂移。

覆盖四件事：
1. `split_system_and_task` —— 系统提示词剥离
2. `extract_text` —— 正文白名单（THINK / SEARCH 必须被丢）
3. `normalize_history` —— 结构归一化、锚点、类型统一
4. `merge_with_local` —— 服务端为文本权威、本地为元数据权威

用法：
    cd engine && ./.venv/Scripts/python.exe tests/test_remote.py
"""

from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gad_engine.remote import (
    TASK_SEP,
    extract_text,
    merge_with_local,
    normalize_history,
    split_system_and_task,
)
from gad_engine.store import Message

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    mark = "通过" if cond else "失败"
    extra = f" —— {detail}" if detail and not cond else ""
    print(f"  [{mark}] {name}{extra}")


def biz(messages: list[dict], **session_over: object) -> dict:
    """拼一个和真实响应同构的 `biz_data`。"""
    session = {
        "id": "sid-0001",
        "title": "测试会话",
        "current_message_id": messages[-1]["message_id"] if messages else 0,
        "is_empty": not messages,
        **session_over,
    }
    return {"chat_session": session, "chat_messages": messages}


def msg(mid: int, role: str, frags: list[tuple[str, str]], parent: int | None = None) -> dict:
    return {
        "message_id": mid,
        "parent_id": parent,
        "role": role,
        "status": "FINISHED",
        "accumulated_token_usage": 100 * mid,
        "inserted_at": 1792068300.0 + mid,
        "fragments": [{"id": i, "type": t, "content": c} for i, (t, c) in enumerate(frags, 1)],
    }


def main() -> int:
    # ---------- 1. 系统提示词剥离 ----------
    print("\n[1] split_system_and_task")
    head, tail = split_system_and_task("你是本地代理。\n\n# 任务\n你好")
    check("有分隔符时切开", head == "你是本地代理。" and tail == "你好", f"{head!r} / {tail!r}")

    head, tail = split_system_and_task("这是用户直接说的话")
    check("无分隔符时整段算用户输入", head == "" and tail == "这是用户直接说的话", f"{head!r}/{tail!r}")

    head, tail = split_system_and_task(TASK_SEP + "只有任务")
    check("分隔符在最前时系统部分为空", head == "" and tail == "只有任务", f"{head!r}/{tail!r}")

    # 工具结果里恰好含这个串 —— 不该被切（调用方按「只处理第 1 条 USER」保证）
    head, tail = split_system_and_task("echo a\n\n# 任务\nb")
    check("分隔符出现在工具输出里也能切（由调用方限定范围）",
          head == "echo a" and tail == "b", f"{head!r}/{tail!r}")

    # ---------- 2. 正文白名单 ----------
    print("\n[2] extract_text")
    skipped: dict[str, int] = {}
    text = extract_text(
        {"fragments": [
            {"type": "THINK", "content": "用户在打招呼"},
            {"type": "RESPONSE", "content": "你好"},
            {"type": "SEARCH", "content": "Found 12 web pages"},
        ]},
        skipped,
    )
    check("只取 RESPONSE", text == "你好", text)
    check("THINK 被计入跳过", skipped.get("THINK") == 1, str(skipped))
    check("SEARCH 被计入跳过", skipped.get("SEARCH") == 1, str(skipped))
    check("搜索结果没混进正文", "Found" not in text, text)

    text = extract_text({"fragments": [
        {"type": "REQUEST", "content": "第一段"},
        {"type": "RESPONSE", "content": "第二段"},
    ]})
    check("REQUEST 与 RESPONSE 都计入并按序拼接", text == "第一段第二段", text)

    check("空 fragments 得到空串", extract_text({"fragments": []}) == "")
    check("缺 fragments 字段不炸", extract_text({}) == "")

    # ---------- 3. 归一化 ----------
    print("\n[3] normalize_history")
    sample = biz([
        msg(1, "USER", [("REQUEST", "你是一个本地代理。\n\n# 任务\n帮我看看磁盘")], None),
        msg(2, "ASSISTANT", [("THINK", "想想"), ("RESPONSE", "好的，正在看。")], 1),
    ])
    h = normalize_history(sample)
    check("消息条数正确", len(h.messages) == 2, str(len(h.messages)))
    check("角色映射 USER→user", h.messages[0].role == "user", h.messages[0].role)
    check("角色映射 ASSISTANT→assistant", h.messages[1].role == "assistant", h.messages[1].role)
    check("用户输入已剥离系统提示词", h.messages[0].text == "帮我看看磁盘", h.messages[0].text)
    check("系统提示词被单独取出", h.system_prompt == "你是一个本地代理。", h.system_prompt)
    check("助手正文不含思考", h.messages[1].text == "好的，正在看。", h.messages[1].text)
    check("锚点 = current_message_id", h.anchor == "2", h.anchor)
    check("锚点是 str 不是 int", isinstance(h.anchor, str), type(h.anchor).__name__)
    check("rid 是 str（int 已被转）", h.messages[0].rid == "1" and isinstance(h.messages[0].rid, str),
          repr(h.messages[0].rid))
    check("raw_count 记录原始条数", h.raw_count == 2, str(h.raw_count))
    check("标题带出来", h.title == "测试会话", h.title)
    check("时间戳转成 float", h.messages[0].ts > 0, str(h.messages[0].ts))

    # 第 2 条 USER 是工具结果，**不该**再被切
    sample2 = biz([
        msg(1, "USER", [("REQUEST", "系统提示词\n\n# 任务\n第一问")], None),
        msg(2, "ASSISTANT", [("RESPONSE", "答一")], 1),
        msg(3, "USER", [("REQUEST", "echo x\n\n# 任务\ny")], 2),
    ])
    h2 = normalize_history(sample2)
    check("后续 USER 消息原样保留（不误切）",
          h2.messages[2].text == "echo x\n\n# 任务\ny", repr(h2.messages[2].text))
    check("只有第一条带出系统提示词", h2.system_prompt == "系统提示词", h2.system_prompt)

    # 边界
    h3 = normalize_history(biz([]))
    check("空会话得到空列表", h3.messages == [] and h3.raw_count == 0)
    check("空会话锚点为 0 的字符串", h3.anchor == "0", h3.anchor)

    h4 = normalize_history({"chat_session": {"id": "x"}, "chat_messages": [
        msg(1, "USER", [("REQUEST", "只有用户一条")], None),
    ]})
    check("只有 USER 也能归一化", len(h4.messages) == 1 and h4.messages[0].role == "user")
    check("缺 current_message_id 时锚点为空", h4.anchor == "", repr(h4.anchor))

    h5 = normalize_history(biz([msg(1, "USER", [("REQUEST", "系统提示词\n\n# 任务\n")], None)]))
    check("只发了系统提示词、没有任务时不进历史", h5.messages == [], str(h5.messages))
    check("但系统提示词仍被取出", h5.system_prompt == "系统提示词", h5.system_prompt)

    h6 = normalize_history({"chat_session": {}, "chat_messages": [
        {"message_id": 1, "role": "SYSTEM", "fragments": [{"type": "RESPONSE", "content": "x"}]},
        {"message_id": 2, "role": "ASSISTANT", "fragments": [{"type": "RESPONSE", "content": "y"}]},
    ]})
    check("未知角色被跳过", len(h6.messages) == 1 and h6.messages[0].text == "y", str(len(h6.messages)))

    # ---------- 4. 合并 ----------
    print("\n[4] merge_with_local")
    remote = [
        Message(role="user", text="第一问", ts=1.0, cid="c1", rid="1"),
        Message(role="assistant", text="答一（服务端文本）", ts=2.0, cid="c1", rid="2"),
        Message(role="user", text="网页版里追加的问题", ts=3.0, cid="c1", rid="3"),
        Message(role="assistant", text="网页版里的回答", ts=4.0, cid="c1", rid="4"),
    ]
    local = [
        Message(role="user", text="第一问", ts=1.0, cid="c1", rid="1"),
        Message(role="assistant", text="答一（本地旧文本）", ts=2.0, cid="c1", rid="2",
                tool_calls=3, thoughts="本地思考", stop_reason="completed", turns=2),
    ]
    merged = merge_with_local(remote, local)
    check("合并后条数 = 服务端条数 + 本地独有", len(merged) == 4, str(len(merged)))
    check("文本以服务端为准",
          merged[1].text == "答一（服务端文本）", merged[1].text)
    check("本地 tool_calls 被继承", merged[1].tool_calls == 3, str(merged[1].tool_calls))
    check("本地 thoughts 被继承", merged[1].thoughts == "本地思考", merged[1].thoughts)
    check("本地 stop_reason 被继承", merged[1].stop_reason == "completed", merged[1].stop_reason)
    check("服务端独有消息保留", merged[2].text == "网页版里追加的问题", merged[2].text)

    local_only = [*local, Message(role="assistant", text="服务端没有的失败轮", ts=9.0, cid="c1", rid="")]
    merged2 = merge_with_local(remote, local_only)
    check("本地独有消息追加在末尾", merged2[-1].text == "服务端没有的失败轮", merged2[-1].text)
    check("本地独有消息没被吞", len(merged2) == 5, str(len(merged2)))

    merged3 = merge_with_local([], local)
    check("服务端为空时全部保留本地", len(merged3) == len(local), str(len(merged3)))

    merged4 = merge_with_local(remote, [])
    check("本地为空时全部用服务端", len(merged4) == 4, str(len(merged4)))

    check("不修改入参", len(remote) == 4 and len(local) == 2, f"{len(remote)}/{len(local)}")

    print("\n" + "-" * 70)
    print(f"  通过 {len(PASS)} · 失败 {len(FAIL)}")
    if FAIL:
        for f in FAIL:
            print(f"    失败项：{f}")
    print("-" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
