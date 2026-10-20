"""dump `chat/history_messages` 的响应结构（只看字段，不落盘正文）。

用途：为「切会话时拉服务端历史」这一需求确认数据形态 ——
每条消息有哪些字段、`current_message_id` 是什么语义、
能否直接映射成本地 Transcript / 能否作为续接锚点。

隐私：**不把对话正文写进项目**，只打印字段名与类型；正文只取前 40 字
用于判断字段含义，且只出现在终端。
"""

from __future__ import annotations

# ★ 直连鉴权探测的闸门 —— **默认拒绝**（理由见 probe_curl_cffi.py 同款注释）。
#   本脚本用 `curl_cffi` 直连 DeepSeek，**不走传输层** ⇒ `_probe_once` 的闸门拦不住。
import os as _os

if _os.environ.get("GAD_ALLOW_LIVE_AUTH") != "1":
    raise SystemExit(
        "已阻止直连鉴权探测（GAD_ALLOW_LIVE_AUTH 未设为 1）。\n"
        "本脚本绕过传输层闸门，故自带闸门 —— 默认拒绝；确需真打请显式设 =1。"
    )

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gad_engine.identity import DEFAULT_TOKEN_FILE, load_token  # noqa: E402
from tools.probe_history_api import BASE, IMPERSONATE, device_id, headers_for  # noqa: E402


def shape(node, depth: int = 0, max_depth: int = 4) -> str:
    """把一个 JSON 节点压成「字段名: 类型」的可读骨架。"""
    pad = "  " * depth
    if isinstance(node, dict):
        lines = []
        for k, v in node.items():
            if isinstance(v, (dict, list)):
                lines.append(f"{pad}{k}: {type(v).__name__}")
                if depth < max_depth:
                    lines.append(shape(v, depth + 1, max_depth))
            else:
                prev = str(v)
                if len(prev) > 40:
                    prev = prev[:40] + "..."
                lines.append(f"{pad}{k}: {type(v).__name__} = {prev}")
        return "\n".join(lines)
    if isinstance(node, list):
        if not node:
            return f"{pad}(空数组)"
        lines = [f"{pad}[0] 共 {len(node)} 项，首项结构："]
        lines.append(shape(node[0], depth + 1, max_depth))
        return "\n".join(lines)
    return f"{pad}{node!r}"


def main() -> int:
    from curl_cffi import requests as creq

    token = load_token(DEFAULT_TOKEN_FILE)
    dev = device_id()
    session = creq.Session(impersonate=IMPERSONATE)
    h = headers_for(token, dev)

    print("=" * 74)
    print("history_messages 响应结构 dump（正文不入项目，仅终端预览）")
    print("=" * 74)

    r = session.get(f"{BASE}/api/v0/chat_session/fetch_page", headers=h,
                    params={"count": 5}, timeout=25)
    biz = (r.json().get("data") or {}).get("biz_data") or {}
    sessions = biz.get("chat_sessions") or []
    print(f"\n[会话列表] 共 {len(sessions)} 项，单条字段：")
    if sessions:
        print(shape(sessions[0], 1))

    if not sessions:
        print("没有会话可用于 dump")
        return 1

    sid = sessions[0]["id"]
    print(f"\n[历史] 取 chat_session_id={sid}")
    r2 = session.get(f"{BASE}/api/v0/chat/history_messages", headers=h,
                     params={"chat_session_id": sid}, timeout=25)
    body = r2.json()
    d = (body.get("data") or {}).get("biz_data") or {}

    print(f"\n顶层字段：{list(body.keys())}")
    print(f"biz_data 字段：{list(d.keys())}")

    cs = d.get("chat_session") or {}
    print("\n--- chat_session 字段 ---")
    print(shape(cs, 1))

    msgs = d.get("chat_messages") or []
    print(f"\n--- chat_messages：共 {len(msgs)} 条 ---")
    for i, m in enumerate(msgs[:3]):
        print(f"\n  [消息 {i}] 字段：{list(m.keys())}")
        print(shape(m, 2, max_depth=2))

    if msgs:
        print("\n--- 关键锚点 ---")
        print(f"  chat_session.current_message_id = {cs.get('current_message_id')}")
        print(f"  末条消息 message_id            = {msgs[-1].get('message_id')}")
        ids = [m.get("message_id") for m in msgs]
        print(f"  全部 message_id                = {ids}")
        roles = [m.get("role") for m in msgs]
        print(f"  全部 role                      = {roles}")
        pids = [m.get("parent_id") for m in msgs]
        print(f"  全部 parent_id                 = {pids}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
