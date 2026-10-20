"""验证 `chat_session/delete` 是否真的能删掉会话 —— **净零**实验。

设计
----
1. 记录当前会话总数 N
2. 调 `chat_session/create` 建一个空会话（N+1）
3. 从列表确认它真的出现了
4. 调 `chat_session/delete` 删它
5. 再从列表确认它消失了（回到 N）

**净效果为零**：新建的那条就是被删的那条，账号数据不多不少。
跑完后账号状态与跑之前完全一致（除了列表里可能残留的排序时间戳）。

为什么必须做这个实验
--------------------
`probe_delete_endpoint.py` 实测发现：对**不存在的** id，`chat_session/delete`
也返回 `biz_code=0`（幂等语义）。这既可能是「真的删了，找不到也无所谓」，
也可能是「压根没做任何事，一律回成功」。**两种语义只能靠实证区分**，
不能靠返回值猜。

用法
----
    cd engine
    ./.venv/Scripts/python.exe -X utf8 tools/verify_delete_session.py
    ./.venv/Scripts/python.exe -X utf8 tools/verify_delete_session.py --dry-run
"""

from __future__ import annotations

import argparse
import os as _os
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ★ 闸门与 probe_history_api.py 同款：绕过传输层，故自带闸门，默认拒绝。
if __name__ == "__main__" and _os.environ.get("GAD_ALLOW_LIVE_AUTH") != "1":
    raise SystemExit(
        "已阻止直连鉴权探测（GAD_ALLOW_LIVE_AUTH 未设为 1）。\n"
        "本脚本绕过传输层闸门，故自带闸门 —— 默认拒绝；确需真打请显式设 =1。"
    )

from gad_engine.identity import DEFAULT_TOKEN_FILE, load_token  # noqa: E402

BASE = "https://chat.deepseek.com"
IMPERSONATE = "chrome150"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)
DEVICE_ID_FILE = ROOT / ".device-id"

LIST_PATH = "/api/v0/chat_session/fetch_page"
CREATE_PATH = "/api/v0/chat_session/create"
DELETE_PATH = "/api/v0/chat_session/delete"
HISTORY_PATH = "/api/v0/chat/history_messages"


def log(msg: str = "") -> None:
    print(msg, flush=True)


def device_id() -> str:
    if DEVICE_ID_FILE.exists():
        v = DEVICE_ID_FILE.read_text(encoding="utf-8").strip()
        if v:
            return v
    import uuid

    v = str(uuid.uuid4())
    DEVICE_ID_FILE.write_text(v, encoding="utf-8")
    return v


def headers_for(token: str, dev: str, referer: str = BASE + "/") -> dict:
    return {
        "accept": "*/*",
        "authorization": f"Bearer {token}",
        "content-type": "application/json",
        "origin": BASE,
        "referer": referer,
        "user-agent": UA,
        "x-client-bundle-id": "com.deepseek.chat",
        "x-client-locale": "en_US",
        "x-client-platform": "web",
        "x-client-version": "2.5.0",
        "x-client-timezone-offset": "28800",
        "x-device-id": dev,
        "x-device-model": "",
    }


def list_sessions(session, token: str, dev: str, count: int = 100) -> list[dict]:
    r = session.get(
        BASE + LIST_PATH,
        headers=headers_for(token, dev),
        params={"count": count},
        timeout=20,
    )
    body = r.json()
    biz = (body.get("data") or {}).get("biz_data") or {}
    return biz.get("chat_sessions") or []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不发请求")
    ap.add_argument("--token-file", default=str(DEFAULT_TOKEN_FILE))
    args = ap.parse_args()

    log("=" * 74)
    log("delete 会话接口验证（净零：建一条 → 删同一条）")
    log("=" * 74)

    if args.dry_run:
        log("  计划：")
        log(f"    1. GET  {LIST_PATH}?count=100   记录基线 N")
        log(f"    2. POST {CREATE_PATH}           建一个空会话 → 拿到 sid")
        log(f"    3. GET  {LIST_PATH}?count=100   确认 sid 出现（N+1）")
        log(f"    4. POST {DELETE_PATH}           删掉 sid")
        log(f"    5. GET  {LIST_PATH}?count=100   确认 sid 消失（回到 N）")
        log("  [dry-run] 未发出任何请求")
        return 0

    try:
        token = load_token(Path(args.token_file))
    except Exception as exc:  # noqa: BLE001
        log(f"[失败] 读取 token：{exc}")
        return 2

    from curl_cffi import requests as creq

    dev = device_id()
    session = creq.Session(impersonate=IMPERSONATE)
    log(f"[准备] token len={len(token)} · device={dev[:8]}...")

    # ---------- 1. 基线 ----------
    before = list_sessions(session, token, dev)
    n_before = len(before)
    log(f"\n[1] 基线：{n_before} 条会话")

    # ---------- 2. 新建 ----------
    log(f"\n[2] POST {CREATE_PATH}")
    r = session.post(BASE + CREATE_PATH, headers=headers_for(token, dev), json={}, timeout=20)
    log(f"    status={r.status_code} body={(r.text or '')[:300]}")
    try:
        body = r.json()
    except ValueError:
        log("    [失败] 返回不是 JSON")
        return 3
    biz = (body.get("data") or {}).get("biz_data") or {}
    sid = (
        ((biz.get("chat_session") or {}).get("id"))
        or biz.get("id")
        or ""
    )
    if not sid:
        log("    [失败] 响应里没有会话 id")
        return 3
    log(f"    新会话 id = {sid}")

    # ---------- 3. 确认出现 ----------
    #
    # ★ 2026-10-18 实测：**新建的空会话不会出现在 fetch_page 列表里**。
    #   新建返回 `title=null, title_type="WIP"`，而列表只列「有内容」的会话。
    #   所以「建后计数不变」是**正常**的，不能当成失败。
    #   真正的存在性判据是第 3.5 步：用 history 接口查这个 id。
    time.sleep(1.0)
    after_create = list_sessions(session, token, dev)
    n_after_create = len(after_create)
    ids_after_create = {s.get("id") for s in after_create}
    appeared = sid in ids_after_create
    log(f"\n[3] 建后列表：{n_after_create} 条（基线 {n_before}）· 新会话在列表里 = {appeared}")
    if not appeared:
        log("    （预期内：空会话 title=null/title_type=WIP，不进列表。改用 history 接口验存在性）")

    # ---------- 3.5 用 history 接口确认它真的存在 ----------
    h = headers_for(token, dev, f"{BASE}/a/chat/s/{sid}")
    r = session.get(
        BASE + HISTORY_PATH, headers=h, params={"chat_session_id": sid}, timeout=20
    )
    try:
        hb = r.json()
    except ValueError:
        hb = {}
    hbiz = (hb.get("data") or {}).get("biz_code")
    hmsg = (hb.get("data") or {}).get("biz_msg") or ""
    alive_before = hbiz == 0
    log(f"\n[3.5] history 查新会话：biz_code={hbiz} biz_msg={hmsg!r} -> 存在={alive_before}")

    # ---------- 4. 删除 ----------
    log(f"\n[4] POST {DELETE_PATH}  body={{'chat_session_id': sid}}")
    r = session.post(
        BASE + DELETE_PATH,
        headers=headers_for(token, dev),
        json={"chat_session_id": sid},
        timeout=20,
    )
    log(f"    status={r.status_code} body={(r.text or '')[:300]}")

    # ---------- 5. 确认消失 ----------
    #
    # 判据同样用 history 接口 —— 列表里本来就没有它（空会话），
    # 靠列表判断「消失了」会得出永远为真的假结论。
    time.sleep(1.5)
    after_delete = list_sessions(session, token, dev)
    n_after_delete = len(after_delete)
    r = session.get(
        BASE + HISTORY_PATH, headers=h, params={"chat_session_id": sid}, timeout=20
    )
    try:
        hb = r.json()
    except ValueError:
        hb = {}
    hbiz2 = (hb.get("data") or {}).get("biz_code")
    hmsg2 = (hb.get("data") or {}).get("biz_msg") or ""
    gone = hbiz2 != 0
    log(f"\n[5] 删后列表：{n_after_delete} 条（基线 {n_before}）")
    log(f"    history 查已删会话：biz_code={hbiz2} biz_msg={hmsg2!r} -> 已消失={gone}")

    log("\n" + "=" * 74)
    log("  判读")
    log("=" * 74)
    log(f"  列表计数：基线 {n_before} → 建后 {n_after_create} → 删后 {n_after_delete}")
    log(f"  存在性：  建后存在={alive_before} · 删后存在={not gone}")
    if alive_before and gone:
        log("  [通过] delete 接口真的能删 —— 净效果为零，账号回到原状")
        log("         ⇒ 可以用它清理测试会话")
        log("         （列表计数不变是正常的：空会话本来就不进列表）")
    elif alive_before and not gone:
        log("  [失败] delete 返回成功但会话仍在 —— 幂等语义是假的（一律回成功）")
        log(f"         ⚠️ 账号里多了一条空会话（{sid}），需人工处理")
    elif not alive_before:
        log("  [失败] 新建的会话查不到 —— history 判据本身失效，本轮结论不可信")
    log("=" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
