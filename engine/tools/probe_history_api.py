"""探测服务端的「会话列表 / 历史消息」接口是否存在。

背景
----
需求要求「进入某个会话时读取该会话的历史（历史来自 DeepSeek 服务端）」，
并据此实现真正的上下文隔离。但本项目的抓包只覆盖了
`create_pow_challenge` + `completion` 两个接口（见
`docs/evidence/20261014-真实请求画像分析.md`），**会话列表与历史消息接口
从未被探测过**。

在拿到实测结论之前，任何「切会话就拉服务端历史」的方案都是猜的。
本脚本负责把这个事实补上。

方法
----
只发**只读**请求（列会话 / 取历史），不新建会话、不发消息 ——
避免污染账号数据，也避免不必要的风控暴露。

先试会话列表类端点，命中后从返回里取一个真实 session_id，
再用它去试历史消息类端点（这样不需要额外 create 一个空会话）。

用法
----
    cd engine
    ./.venv/Scripts/python.exe -X utf8 tools/probe_history_api.py
    ./.venv/Scripts/python.exe -X utf8 tools/probe_history_api.py --sid <已知会话id>
"""

from __future__ import annotations

import argparse
import json
import os as _os
import sys
import uuid
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gad_engine.identity import DEFAULT_TOKEN_FILE, load_token  # noqa: E402

# ★ 直连鉴权探测的闸门 —— **默认拒绝**（理由见 probe_curl_cffi.py 同款注释）。
#
# 与其它直连脚本的区别：本模块**同时是共享常量模块**（`BASE` / `IMPERSONATE` /
# `device_id` / `headers_for` 被 `probe_history_shape.py` 与 `verify_session_isolation.py`
# import）。所以闸门**只在直接执行时**生效 —— 放在模块顶层会把 import 方一起弄死
# （实测：`verify_session_isolation.py` 走的是**已上闸门的传输层**，却被这里拖死）。
# **import 方自己负责上闸门。**
if __name__ == "__main__" and _os.environ.get("GAD_ALLOW_LIVE_AUTH") != "1":
    raise SystemExit(
        "已阻止直连鉴权探测（GAD_ALLOW_LIVE_AUTH 未设为 1）。\n"
        "本脚本绕过传输层闸门，故自带闸门 —— 默认拒绝；确需真打请显式设 =1。"
    )

BASE = "https://chat.deepseek.com"
IMPERSONATE = "chrome150"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)
DEVICE_ID_FILE = ROOT / ".device-id"

# 候选端点：(方法, 路径, body)。命名依据是网页版同类接口的习惯叫法。
#
# 第一轮实测结论（2026-10-15 21:5x）：POST `chat_session/fetch_page` 与
# `users/current` 回 **405 Method Not Allowed**（API 层 JSON），而
# `list` / `page` / `fetch` 回的是 SPA 首页 HTML —— 405 说明**路径存在、
# 方法不对**，未匹配路径才会被 SPA 兜底吞成 200 HTML。故本轮改用 GET。
LIST_CANDIDATES: list[tuple[str, str, dict | None]] = [
    ("GET", "/api/v0/chat_session/fetch_page", None),
    ("GET", "/api/v0/chat_session/fetch_page?count=20", None),
    ("GET", "/api/v0/chat_session/fetch_page?count=50&updated_at=0", None),
]

HISTORY_CANDIDATES: list[tuple[str, str, dict | None]] = [
    ("GET", "/api/v0/chat/history_messages", None),
    ("GET", "/api/v0/chat_session/history_messages", None),
    ("POST", "/api/v0/chat/history_messages", {"chat_session_id": None}),
    ("POST", "/api/v0/chat_session/messages", {"chat_session_id": None}),
]

MISC_CANDIDATES: list[tuple[str, str, dict | None]] = [
    ("GET", "/api/v0/users/current", None),
]


def log(msg: str = "") -> None:
    print(msg, flush=True)


def device_id() -> str:
    if DEVICE_ID_FILE.exists():
        v = DEVICE_ID_FILE.read_text(encoding="utf-8").strip()
        if v:
            return v
    v = str(uuid.uuid4())
    DEVICE_ID_FILE.write_text(v, encoding="utf-8")
    return v


def headers_for(token: str, dev: str) -> dict:
    return {
        "accept": "*/*",
        "authorization": f"Bearer {token}",
        "content-type": "application/json",
        "origin": BASE,
        "referer": BASE + "/",
        "user-agent": UA,
        "x-client-bundle-id": "com.deepseek.chat",
        "x-client-locale": "en_US",
        "x-client-platform": "web",
        "x-client-version": "2.5.0",
        "x-client-timezone-offset": "28800",
        "x-device-id": dev,
        "x-device-model": "",
    }


def probe(session, token: str, dev: str, method: str, path: str, body: dict | None) -> dict:
    """发一次探测请求，返回 {status, body_head, biz_code, raw}。"""
    url = BASE + path
    h = headers_for(token, dev)
    try:
        if method == "GET":
            resp = session.get(url, headers=h, timeout=20)
        else:
            resp = session.post(url, headers=h, json=body or {}, timeout=20)
    except Exception as exc:  # noqa: BLE001
        return {"status": -1, "err": f"{type(exc).__name__}: {exc}", "raw": None}

    out: dict = {"status": resp.status_code, "raw": None, "body_head": ""}
    # 405 会带 Allow 头 —— 它直接告诉我们「该用什么方法」，比盲试省事。
    out["allow"] = resp.headers.get("allow") or resp.headers.get("Allow") or ""
    try:
        parsed = resp.json()
        out["raw"] = parsed
        out["biz_code"] = (parsed.get("data") or {}).get("biz_code")
        out["biz_msg"] = (parsed.get("data") or {}).get("biz_msg")
        out["body_head"] = json.dumps(parsed, ensure_ascii=False)[:420]
    except ValueError:
        out["body_head"] = resp.text[:420]
    return out


def find_session_ids(node, acc: list[str], depth: int = 0) -> None:
    """从响应里捞出像 session id 的字符串（递归找 id / chat_session_id 字段）。"""
    if depth > 8 or len(acc) > 12:
        return
    if isinstance(node, list):
        for i in node:
            find_session_ids(i, acc, depth + 1)
        return
    if not isinstance(node, dict):
        return
    for k, v in node.items():
        if k in ("id", "chat_session_id", "session_id") and isinstance(v, str) and len(v) >= 16:
            if v not in acc:
                acc.append(v)
        else:
            find_session_ids(v, acc, depth + 1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sid", default="", help="已知会话 id；给了就跳过列表探测直接试历史")
    ap.add_argument("--token-file", default=str(DEFAULT_TOKEN_FILE))
    args = ap.parse_args()

    from curl_cffi import requests as creq

    log("=" * 74)
    log("服务端会话列表 / 历史消息接口探测（只读，不新建会话、不发消息）")
    log("=" * 74)

    try:
        token = load_token(Path(args.token_file))
    except Exception as exc:  # noqa: BLE001
        log(f"[失败] 读取 token：{exc}")
        return 2
    dev = device_id()
    log(f"[准备] token len={len(token)} · device={dev[:8]}... · impersonate={IMPERSONATE}")

    session = creq.Session(impersonate=IMPERSONATE)
    discovered: list[str] = []
    hit_list: str = ""

    if not args.sid:
        log("")
        log("[A] 会话列表类端点")
        for method, path, body in LIST_CANDIDATES:
            r = probe(session, token, dev, method, path, body)
            if r.get("status") == -1:
                log(f"  {method:5} {path:42} -> 网络错误 {r['err'][:60]}")
                continue
            mark = "命中" if r["status"] == 200 and r.get("biz_code") == 0 else "未命中"
            allow = f" allow={r['allow']}" if r.get("allow") else ""
            log(f"  {method:5} {path:46} -> HTTP {r['status']} biz={r.get('biz_code')}{allow} [{mark}]")
            if r["raw"] is not None:
                find_session_ids(r["raw"], discovered)
            if r["status"] == 200 and r.get("biz_code") == 0 and not hit_list:
                hit_list = f"{method} {path}"
                log(f"        响应片段：{r['body_head'][:380]}")
            elif r["status"] != 404 and r["body_head"]:
                log(f"        响应片段：{r['body_head'][:200]}")

        log("")
        log("[B] 其它参考端点")
        for method, path, body in MISC_CANDIDATES:
            r = probe(session, token, dev, method, path, body)
            if r.get("status") == -1:
                log(f"  {method:5} {path:42} -> 网络错误")
                continue
            log(f"  {method:5} {path:42} -> HTTP {r['status']} biz={r.get('biz_code')}")
            if r["raw"] is not None:
                find_session_ids(r["raw"], discovered)

    log("")
    log(f"[C] 从列表响应里捞到的 session id：{len(discovered)} 个")
    for s in discovered[:6]:
        log(f"      {s}")

    sid = args.sid or (discovered[0] if discovered else "")
    if not sid:
        log("")
        log("[D] 跳过历史探测 —— 没有可用的 session id（列表端点没命中）")
        log("")
        log("  结论：本次未能证明服务端存在可用的会话列表接口。")
        return 0

    log("")
    log(f"[D] 历史消息类端点（用 session_id={sid[:20]}...）")
    hit_history = ""
    for method, path, body in HISTORY_CANDIDATES:
        payload = dict(body or {})
        if "chat_session_id" in payload:
            payload["chat_session_id"] = sid
        if method == "GET":
            sep = "&" if "?" in path else "?"
            path_full = f"{path}{sep}chat_session_id={sid}"
            payload = None
        else:
            path_full = path
        r = probe(session, token, dev, method, path_full, payload)
        if r.get("status") == -1:
            log(f"  {method:5} {path:46} -> 网络错误 {r['err'][:60]}")
            continue
        ok = r["status"] == 200 and r.get("biz_code") == 0
        allow = f" allow={r['allow']}" if r.get("allow") else ""
        log(f"  {method:5} {path:46} -> HTTP {r['status']} biz={r.get('biz_code')}{allow} "
            f"[{'命中' if ok else '未命中'}]")
        if ok and not hit_history:
            hit_history = f"{method} {path}"
            log(f"        响应片段：{r['body_head'][:600]}")

    log("")
    log("=" * 74)
    log(f"  列表接口：{hit_list or '未找到'}")
    log(f"  历史接口：{hit_history or '未找到'}")
    log("=" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
