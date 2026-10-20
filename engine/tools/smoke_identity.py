"""阶段 3 端到端冒烟 —— 真拉起浏览器取凭据，取完关掉。

验证四件事：
  1. `find_browser` + `LoginSession.start()` 能把专用 profile 的浏览器拉起来
  2. `poll()` 能从 `localStorage` 读出 token（并要求连续两次读到同一个值）
  3. `validate_token()` 用一次 `create_pow_challenge` 确认它**真的可用**
  4. **取完浏览器被关掉**（这是「不跑网页版」的关键 —— 不能留一个常驻浏览器）

用法
----
    cd engine
    # 用已有登录态的 profile（开发期最常用，无需重新登录）
    ./.venv/Scripts/python.exe tools/smoke_identity.py \
        --profile "%LOCALAPPDATA%/DeepSeek-Probe/chrome-profile"
    # 走生产默认 profile（首次需要在弹出的窗口里登录）
    ./.venv/Scripts/python.exe tools/smoke_identity.py
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gad_engine import identity as I
from gad_engine.browser_login import (
    BrowserLoginError,
    BrowserLoginSession,
    cdp_alive,
    default_profile_dir,
)

OUT_DIR = Path(__file__).resolve().parent.parent.parent / "docs" / "evidence"


def log(msg: str) -> None:
    print(msg, flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="", help="浏览器专用 profile 目录")
    ap.add_argument("--port", type=int, default=9333)
    ap.add_argument("--timeout", type=float, default=180.0, help="等待登录的秒数")
    ap.add_argument("--keep-browser", action="store_true", help="跑完不关浏览器（排障用）")
    ap.add_argument("--skip-browser", action="store_true", help="只验缓存 token，不拉浏览器")
    args = ap.parse_args()

    profile = Path(args.profile).expanduser() if args.profile else default_profile_dir()

    log("=" * 70)
    log("阶段 3 端到端冒烟 —— 浏览器取凭据（取完即关）")
    log("=" * 70)
    log(f"[准备] profile = {profile}")
    log(f"[准备] port    = {args.port}")

    checks: dict[str, bool] = {}
    t0 = time.monotonic()

    # ---------- 路径 A：只验缓存 ----------
    if args.skip_browser:
        cached = I.load_token()
        log(f"\n[跳过浏览器] 缓存 token len={len(cached)}")
        if not cached:
            log("  [失败] 没有缓存 token")
            return 2
        ok, detail = I.validate_token(cached)
        log(f"  验证：{ok} —— {detail}")
        checks["缓存 token 可用"] = ok
        return _report(checks, 0.0, None)

    # ---------- 路径 B：完整浏览器流程 ----------
    if cdp_alive(args.port):
        log(f"\n[!] 端口 {args.port} 已被占用 —— 复用它（不会重复拉起浏览器）")

    sess = BrowserLoginSession(profile_dir=profile, port=args.port, settle_reads=2)
    try:
        info = sess.start()
        checks["浏览器已启动"] = True
        log(f"\n[1/5] start() OK —— {info['browser']} · reused={info['reused']}")
    except BrowserLoginError as exc:
        log(f"\n[1/5] [失败] {exc}")
        checks["浏览器已启动"] = False
        return _report(checks, time.monotonic() - t0, None)

    token = ""
    label = ""
    result: dict = {}
    try:
        log(f"[2/5] 轮询取 token（最多 {args.timeout:.0f}s）…")
        deadline = time.monotonic() + args.timeout
        import asyncio

        async def _wait() -> dict:
            last: dict = {"state": "waiting"}
            shown = ""
            while time.monotonic() < deadline:
                last = await sess.poll()
                tag = f"{last['state']}"
                if tag != shown:
                    log(f"      → {tag}" + (f" ({last.get('settle')}/{last.get('need')})" if tag == "settling" else ""))
                    shown = tag
                if last["state"] in ("ready", "no_browser", "error"):
                    return last
                await asyncio.sleep(1.0)
            last = dict(last)
            last["state"] = "timeout"
            return last

        result = asyncio.run(_wait())
    finally:
        if not args.keep_browser:
            log("[5/5] 关闭浏览器（取完即关）…")
            import asyncio

            closed = asyncio.run(sess.close_browser())
            time.sleep(1.5)
            still = cdp_alive(args.port)
            log(f"      close_browser()={closed} · 端口仍在={bool(still)}")
            checks["浏览器已关闭"] = not still
        else:
            log("[5/5] --keep-browser：保留浏览器窗口")
            checks["浏览器已关闭"] = True

    state = result.get("state")
    log(f"\n[3/5] 轮询结果：{state}")
    checks["取到 token"] = state == "ready"
    if state != "ready":
        log(f"      [失败] {result.get('error') or '未取到 token'}")
        return _report(checks, time.monotonic() - t0, None)

    token = I.normalize_token(result.get("token"))
    label = str(result.get("account_label") or "")
    log(f"      token len={len(token)} · 账号名={label or '（未识别）'}")
    checks["token 形态正常"] = len(token) >= 20

    log("\n[4/5] 验证 token 是否真的可用（create_pow_challenge，不建会话不发消息）…")
    ok, detail = I.validate_token(token)
    log(f"      {ok} —— {detail}")
    checks["token 验证通过"] = ok

    # ---------- 落盘 ----------
    if ok:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".token"
            I.save_token(token, p)
            checks["可落盘并读回"] = I.load_token(p) == token

    return _report(checks, time.monotonic() - t0, {"token_len": len(token), "label": label})


def _report(checks: dict, elapsed: float, extra: dict | None) -> int:
    log("")
    log("-" * 70)
    for k, v in checks.items():
        log(f"  [{'通过' if v else '失败'}] {k}")
    passed = all(checks.values()) and bool(checks)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = OUT_DIR / f"smoke-identity-{stamp}.json"
    out.write_text(
        json.dumps(
            {"elapsed_s": round(elapsed, 1), "checks": checks, "extra": extra or {}},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    log(f"  报告：{out}")
    log("")
    log("  [阶段 3 冒烟通过]" if passed else "  [阶段 3 冒烟未通过]")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
