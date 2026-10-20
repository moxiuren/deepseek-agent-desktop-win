"""清理账号里的测试会话 —— 支持 dry-run、按标题模式筛选、限速。

背景
----
2026-10-18 清点：账号下 74 条服务端会话全部产生于当天，其中
13:13–13:17 五分钟内建了 26 条、13:16 一分钟内 10 条 —— 这是明显的
机器特征。`chat_session/delete` 已实测确认可用（`tools/verify_delete_session.py`，
净零实验通过：建一条 → 删同一条 → history 查不到）。

安全设计
--------
1. **默认 dry-run** —— 不加 `--apply` 只打印将删哪些，一条都不删。
2. **必须给筛选条件** —— 不给 `--match` 且不给 `--ids` 时拒绝执行，
   杜绝「手滑删光全部会话」。
3. **限速** —— 每条之间 `--gap` 秒（默认 1.5），避免一分钟删 70 条的
   爆发式请求，那本身就是极强的机器特征。
4. **删除前二次确认** —— `--apply` 时会先打印完整清单并要求输入 `DELETE`。
5. **可回溯** —— 每条删除结果都追加到 `docs/evidence/session-cleanup-<时间>.jsonl`，
   含 id、标题、时间、响应码。删错了能查出删了什么。

用法
----
    cd engine

    # 1. 先看会删哪些（默认就是 dry-run）
    ./.venv/Scripts/python.exe -X utf8 tools/cleanup_test_sessions.py --match 数数1到20

    # 2. 按时间窗筛选（13:12–13:18 那批密集测试）
    ./.venv/Scripts/python.exe -X utf8 tools/cleanup_test_sessions.py \
        --since "2026-10-18 13:12" --until "2026-10-18 13:18"

    # 3. 真正删除（需双闸门 + 手工输入 DELETE）
    GAD_ALLOW_LIVE_AUTH=1 ./.venv/Scripts/python.exe -X utf8 \
        tools/cleanup_test_sessions.py --since "2026-10-18 13:12" \
        --until "2026-10-18 13:18" --apply
"""

from __future__ import annotations

import argparse
import json
import os as _os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ★ 闸门与 probe_history_api.py 同款：绕过传输层，故自带闸门，默认拒绝。
#   本脚本会**真的删数据**，所以这条闸门比探测脚本更重要。
if __name__ == "__main__" and _os.environ.get("GAD_ALLOW_LIVE_AUTH") != "1":
    raise SystemExit(
        "已阻止（GAD_ALLOW_LIVE_AUTH 未设为 1）。\n"
        "本脚本会真的删除服务端会话，故自带闸门 —— 默认拒绝。"
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
DELETE_PATH = "/api/v0/chat_session/delete"
EVIDENCE_DIR = ROOT.parent / "docs" / "evidence"


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


def to_seconds(ts: float) -> float:
    """服务端 `updated_at` 是**秒级浮点**（实测 1792306981.417）。"""
    if not ts:
        return 0.0
    return ts / 1000.0 if ts > 1e11 else float(ts)


def parse_when(s: str) -> float:
    """解析 `YYYY-MM-DD HH:MM` 或 `YYYY-MM-DD`。"""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).timestamp()
        except ValueError:
            continue
    raise SystemExit(f"时间格式无法解析：{s!r}（用 'YYYY-MM-DD HH:MM'）")


def fetch_all(session, token: str, dev: str, page: int = 100) -> list[dict]:
    """拉取会话列表。

    ⚠️ `count` 有上限：实测 `count=300` 回 `biz_code=1 ILLEGAL_COUNT`。
    100 可用。若账号会话超过 100 条，本函数只能拿到前 100 —— 会打印警告。
    """
    r = session.get(
        BASE + LIST_PATH,
        headers=headers_for(token, dev),
        params={"count": page},
        timeout=25,
    )
    body = r.json()
    data = body.get("data") or {}
    if data.get("biz_code") not in (0, None):
        raise SystemExit(
            f"读会话列表失败：biz_code={data.get('biz_code')} {data.get('biz_msg')}"
        )
    biz = data.get("biz_data") or {}
    return biz.get("chat_sessions") or []


def main() -> int:
    ap = argparse.ArgumentParser(
        description="清理测试会话（默认 dry-run）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--match", default="", help="标题正则（不区分大小写）")
    ap.add_argument("--ids", default="", help="逗号分隔的会话 id 列表")
    ap.add_argument("--since", default="", help="起始时间 'YYYY-MM-DD HH:MM'")
    ap.add_argument("--until", default="", help="结束时间 'YYYY-MM-DD HH:MM'")
    ap.add_argument("--apply", action="store_true", help="真正执行删除（默认只打印）")
    ap.add_argument("--gap", type=float, default=1.5, help="每条之间停顿秒数，默认 1.5")
    ap.add_argument("--token-file", default=str(DEFAULT_TOKEN_FILE))
    args = ap.parse_args()

    log("=" * 78)
    log("  测试会话清理")
    log("=" * 78)

    # ---------- 筛选条件必须有 ----------
    if not (args.match or args.ids or args.since or args.until):
        log("  [拒绝] 必须给出至少一个筛选条件（--match / --ids / --since / --until）。")
        log("         这是防「手滑删光全部会话」的硬性要求。")
        return 2

    try:
        token = load_token(Path(args.token_file))
    except Exception as exc:  # noqa: BLE001
        log(f"[失败] 读取 token：{exc}")
        return 2

    from curl_cffi import requests as creq

    dev = device_id()
    session = creq.Session(impersonate=IMPERSONATE)
    rows = fetch_all(session, token, dev)
    log(f"  账号下共 {len(rows)} 条会话")
    if len(rows) >= 100:
        log("  [警告] 已达单次拉取上限 100，可能还有更多会话未列出")

    # ---------- 筛选 ----------
    pat = re.compile(args.match, re.IGNORECASE) if args.match else None
    id_set = {s.strip() for s in args.ids.split(",") if s.strip()}
    t_from = parse_when(args.since) if args.since else 0.0
    t_to = parse_when(args.until) if args.until else float("inf")

    picked: list[dict] = []
    for s in rows:
        sid = str(s.get("id") or "")
        if not sid:
            continue
        title = str(s.get("title") or "")
        ts = to_seconds(float(s.get("updated_at") or 0))
        if id_set and sid not in id_set:
            continue
        if pat and not pat.search(title):
            continue
        if ts < t_from or ts > t_to:
            continue
        picked.append({"id": sid, "title": title, "ts": ts})

    picked.sort(key=lambda r: r["ts"])
    log(f"\n  命中 {len(picked)} 条：")
    for r in picked:
        stamp = datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d %H:%M:%S")
        log(f"    {stamp}  {r['title'][:36]:<36}  {r['id'][:8]}")

    if not picked:
        log("\n  没有命中任何会话，退出。")
        return 0

    if not args.apply:
        log("\n  [dry-run] 未删除任何会话。")
        log("  确认无误后加 --apply 真正执行（会再要求输入 DELETE）。")
        return 0

    # ---------- 二次确认 ----------
    log(f"\n  ⚠️ 即将**永久删除**上面 {len(picked)} 条会话，不可撤销。")
    log("     输入 DELETE 继续，其它任何输入都会中止：")
    try:
        ans = input("     > ").strip()
    except EOFError:
        ans = ""
    if ans != "DELETE":
        log("  已中止，未删除任何会话。")
        return 0

    # ---------- 执行 ----------
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    logfile = EVIDENCE_DIR / f"session-cleanup-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    log(f"\n  审计日志：{logfile}")
    ok = 0
    failed: list[tuple[str, str]] = []
    for i, r in enumerate(picked, 1):
        try:
            resp = session.post(
                BASE + DELETE_PATH,
                headers=headers_for(token, dev),
                json={"chat_session_id": r["id"]},
                timeout=25,
            )
            body = resp.json()
            biz = (body.get("data") or {}).get("biz_code")
            good = resp.status_code == 200 and biz in (0, None)
            rec = {
                "at": datetime.now().isoformat(timespec="seconds"),
                "id": r["id"],
                "title": r["title"],
                "updated_at": r["ts"],
                "status": resp.status_code,
                "biz_code": biz,
                "ok": good,
            }
        except Exception as exc:  # noqa: BLE001
            good = False
            rec = {
                "at": datetime.now().isoformat(timespec="seconds"),
                "id": r["id"],
                "title": r["title"],
                "updated_at": r["ts"],
                "error": f"{type(exc).__name__}: {exc}",
                "ok": False,
            }
        with logfile.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if good:
            ok += 1
            log(f"    [{i}/{len(picked)}] 已删  {r['title'][:30]}")
        else:
            failed.append((r["id"], r["title"]))
            log(f"    [{i}/{len(picked)}] 失败  {r['title'][:30]}  {rec}")
        if i < len(picked):
            time.sleep(args.gap)

    log("\n" + "=" * 78)
    log(f"  完成：成功 {ok} / {len(picked)}")
    if failed:
        log(f"  失败 {len(failed)} 条：")
        for sid, title in failed:
            log(f"    {sid}  {title}")
    log(f"  审计日志：{logfile}")
    log("=" * 78)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
