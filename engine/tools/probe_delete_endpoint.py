"""探测「删除 / 归档会话」接口是否存在 —— **零副作用**。

背景
----
2026-10-18 清点发现账号下有 74 条服务端会话，全部产生于当天，
其中 13:13–13:17 五分钟内建了 26 条、13:16 一分钟内 10 条。
这是**明显的机器特征**，用户问「要不要处理」。

要回答「能不能处理」，先得知道**有没有删除接口**。项目文档
（`docs/evidence/20261015-接力换会话.md`）一直记着「`chat_session/delete`
仍未探测」。

方法 —— GET + 一个**绝对不存在的** UUID
----------------------------------------
**绝不盲发 DELETE/POST。** 如果某个路径真的是删除接口，带真实
`chat_session_id` 打过去就真的删了；连 id 都不带更危险 —— 少数接口把
「缺省」解释成「全部」。

**OPTIONS 行不通**（2026-10-18 实测）：CloudFront 在 CDN 层直接回
`403 + HTML`，连对照组 `fetch_page` 都被拒 —— 说明 OPTIONS 根本没到
API 层，这条判据在 CDN 就被截断了。

所以改用 **GET**，并给每个候选路径都挂上
`chat_session_id=00000000-0000-0000-0000-000000000000`：

  * 就算某条路径真是删除接口，删的也是一个**不存在的** id ⇒ 服务端只会
    回「找不到」，账号数据零变化。
  * 判据沿用 `probe_history_api.py` 验证过的那条：
      - 路径**存在** → API 层 JSON（业务错误如「参数非法 / 未找到」）
      - 路径**不存在** → SPA 首页兜底 `200 + HTML`

两者可区分，所以足以判断「路径在不在」。

拟人节奏
--------
候选只有 6 个，间隔 2 秒，跑完即止，不做重试。
总暴露面远小于一次正常对话。

用法
----
    cd engine
    ./.venv/Scripts/python.exe -X utf8 tools/probe_delete_endpoint.py
    ./.venv/Scripts/python.exe -X utf8 tools/probe_delete_endpoint.py --dry-run
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

#: 候选删除 / 归档路径。命名依据：网页版接口习惯 + 同类产品的常见叫法。
#: **只用于 OPTIONS 存在性探测**，不带任何 id、不执行删除。
CANDIDATES: list[str] = [
    "/api/v0/chat_session/delete",
    "/api/v0/chat_session/remove",
    "/api/v0/chat_session/del",
    "/api/v0/chat_session/archive",
    "/api/v0/chat_session/update",
    "/api/v0/chat/delete_session",
]

#: 对照组：一个**已知存在**的路径。它必须判为「存在」，
#: 否则说明判据本身失效（例如 SPA 兜底行为变了），整轮结论不可信。
CONTROL_PATH = "/api/v0/chat_session/fetch_page"

GAP_SEC = 2.0

#: 全零 UUID —— 确定不存在的会话 id。所有探测请求都挂它，
#: 保证「即便命中真删除接口，也删不到任何真实数据」。
NULL_SID = "00000000-0000-0000-0000-000000000000"


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


def probe(session, token: str, dev: str, path: str) -> dict:
    """发一次 GET，判定路径是否存在。

    `chat_session_id` 用的是**全零 UUID** —— 一个确定不存在的会话。
    即便某条路径真是删除接口，删的也是这个不存在的 id，账号数据零变化。
    """
    try:
        resp = session.get(
            BASE + path,
            headers=headers_for(token, dev),
            params={"chat_session_id": NULL_SID},
            timeout=20,
        )
    except Exception as exc:  # noqa: BLE001 - 探测脚本，异常也是结果
        return {"path": path, "status": -1, "err": f"{type(exc).__name__}: {exc}"}

    out: dict = {
        "path": path,
        "status": resp.status_code,
        "allow": resp.headers.get("allow") or resp.headers.get("Allow") or "",
        "ctype": (resp.headers.get("content-type") or "").split(";")[0],
    }
    text = resp.text or ""
    try:
        parsed = resp.json()
        out["kind"] = "json"
        biz = (parsed.get("data") or {}) if isinstance(parsed, dict) else {}
        out["biz_code"] = biz.get("biz_code") if isinstance(biz, dict) else None
        out["biz_msg"] = biz.get("biz_msg") if isinstance(biz, dict) else None
        out["head"] = str(parsed)[:200]
    except ValueError:
        out["kind"] = "html" if "<html" in text[:400].lower() else "text"
        out["head"] = text[:120].replace("\n", " ")
    return out


def judge(r: dict) -> str:
    """把响应翻译成「路径存在 / 不存在 / 未知」。

    判据优先级（2026-10-18 实测补充）：

      1. **405 + `Allow` 头 ⇒ 路径存在、方法不对**。
         这是最强的一条信号 —— 只有 API 路由匹配上了才会回 405，
         未注册路径会被 SPA 兜底成 200 HTML。
         `probe_history_api.py` 当初正是靠这条发现了 `fetch_page` 是 GET。
      2. JSON ⇒ 存在（进了 API 层）。
      3. HTML ⇒ 不存在（SPA 兜底）。
    """
    if r.get("status") == -1:
        return "网络错误"
    if r.get("status") == 405 and r.get("allow"):
        return "存在（方法不对）"
    if r.get("kind") == "html":
        return "不存在"
    if r.get("kind") == "json":
        return "存在"
    if r.get("status") in (404,):
        return "不存在"
    return "未知"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不发请求")
    ap.add_argument("--token-file", default=str(DEFAULT_TOKEN_FILE))
    args = ap.parse_args()

    log("=" * 74)
    log("删除 / 归档接口探测（GET + 全零 UUID，零副作用）")
    log("=" * 74)

    if args.dry_run:
        log(f"  将依次探测（间隔 {GAP_SEC:.0f}s）：")
        log(f"    对照组 {CONTROL_PATH}")
        for p in CANDIDATES:
            log(f"    {p}")
        log("  [dry-run] 未发出任何请求")
        return 0

    try:
        token = load_token(Path(args.token_file))
    except Exception as exc:  # noqa: BLE001
        log(f"[失败] 读取 token：{exc}")
        return 2

    from curl_cffi import requests as creq

    dev = device_id()
    log(f"[准备] token len={len(token)} · device={dev[:8]}... · impersonate={IMPERSONATE}")
    session = creq.Session(impersonate=IMPERSONATE)

    # 先打对照组：确认判据在这个时间点仍然成立。
    log("\n[对照] 已知存在的路径（必须判为「存在」）")
    rc = probe(session, token, dev, CONTROL_PATH)
    log(f"  {CONTROL_PATH:44} status={rc['status']:<4} kind={rc.get('kind')} "
        f"-> {judge(rc)}")
    if judge(rc) != "存在":
        log("  [中止] 对照组判据失效（SPA 兜底行为可能变了），本轮结论不可信")
        return 3

    log("\n[候选] 删除 / 归档路径")
    results = []
    for p in CANDIDATES:
        time.sleep(GAP_SEC)
        r = probe(session, token, dev, p)
        verdict = judge(r)
        results.append((p, verdict, r))
        allow = f" allow={r['allow']}" if r.get("allow") else ""
        biz = f" biz_code={r.get('biz_code')}" if r.get("biz_code") is not None else ""
        log(f"  {p:44} status={r['status']:<4} kind={r.get('kind'):<5}{allow}{biz} -> {verdict}")

    log("\n" + "=" * 74)
    exists = [(p, r) for p, v, r in results if v.startswith("存在")]
    if exists:
        log("  [结果] 以下路径存在：")
        for p, r in exists:
            method = f"  方法={r['allow']}" if r.get("allow") else ""
            log(f"           {p}{method}")
        log("")
        log("         存在 ≠ 是删除接口，也 ≠ 我们有权调用。")
        log("         下一步（**需用户确认后才做**）：用全零 UUID 发一次真实 POST，")
        log("         看它回的是「会话不存在」还是「无权限」—— 前者说明是删除接口，")
        log("         且这次调用不会删掉任何真实数据。")
    else:
        log("  [结果] 所有候选路径都不存在（全部被 SPA 兜底吞成 HTML）。")
        log("         ⇒ 至少这些命名下没有删除接口。")
        log("         ⇒ 若网页版确有删除功能，它用的是别的路径（需从 JS bundle 里挖）。")
    log("=" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
