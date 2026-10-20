"""浏览器档位的分段验收 —— 每一步单独可跑、单独可判。

## 为什么拆成分步而不是一个大脚本

一次跑到底的脚本失败时只有一个红叉，看不出是哪一层坏了：
Chrome 没起来？token 注入没生效？challenge 拿不到？流式没通？
每一步单独跑，失败点就是答案。

## 安全边界

* `--stage start`   —— 起 Chrome + 注入 token + 读账号信息。**只读，不消耗额度。**
* `--stage pow`     —— 再加一次 `create_pow_challenge` + 本地求解。**免费接口。**
* `--stage send`    —— 真发一条对话。**会消耗额度**，需 `GAD_ALLOW_LIVE_AUTH=1`
                       与显式 `--i-know`，两个都要。

默认只跑到 `start`。

用法：
    engine/.venv/Scripts/python.exe -X utf8 tools/verify_chrome_transport.py
    engine/.venv/Scripts/python.exe -X utf8 tools/verify_chrome_transport.py --stage pow
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))

from gad_engine.identity import load_token  # noqa: E402
from gad_engine.session import SessionMeta  # noqa: E402
from gad_engine.transport.chrome_cdp import ChromeCdpTransport  # noqa: E402

TOKEN_FILE = ROOT / "engine" / ".token"


def line(msg: str = "") -> None:
    print(msg, flush=True)


def head(title: str) -> None:
    line()
    line("=" * 74)
    line(title)
    line("=" * 74)


async def stage_start(t: ChromeCdpTransport) -> bool:
    head("[1] 启动 Chrome + 注入登录态")
    t0 = time.monotonic()
    await t.start()
    line(f"  [通过] start() 用时 {(time.monotonic() - t0) * 1000:.0f} ms")
    st = t.background_state()
    line(f"  chrome pid : {st['chrome_pid']}")
    line(f"  调试端口    : {st['debug_port']}")
    line(f"  headless   : {st['headless']}（必须为 False）")
    line(f"  移出屏幕    : {st['offscreen']}")

    head("[2] 账号状态（只读 GET /api/v0/users/current）")
    st2 = await t.health()
    line(f"  reachable  : {st2.reachable}")
    line(f"  状态        : {st2.account_status_short}")
    line(f"  说明        : {st2.account_status_text}")
    if not st2.reachable:
        line("  [错误] 账号不可达 —— 后续步骤无意义")
        return False
    line("  [通过] 登录态有效")
    return True


async def stage_pow(t: ChromeCdpTransport) -> bool:
    head("[3] challenge + 本地求解（免费接口，不耗额度）")
    from gad_engine.pow import Challenge, solve
    from gad_engine.transport.deepseek_rpc import CHALLENGE_PATH, COMPLETION_PATH

    t0 = time.monotonic()
    body = await t._fetch_json("POST", CHALLENGE_PATH, {"target_path": COMPLETION_PATH})
    ms = (time.monotonic() - t0) * 1000
    biz = (body.get("data") or {}).get("biz_data") or {}
    ch = Challenge.from_api(biz["challenge"])
    line(f"  [通过] challenge 用时 {ms:.0f} ms")
    line(f"  algorithm  : {ch.algorithm}")
    line(f"  difficulty : {ch.difficulty}")
    line(f"  target     : {biz.get('challenge', {}).get('target_path')}")

    t0 = time.monotonic()
    solved = await asyncio.to_thread(solve, ch, COMPLETION_PATH, workers=None)
    ms = (time.monotonic() - t0) * 1000
    line(f"  [通过] PoW 求解 {ms:.0f} ms（mode={solved.get('_mode')} "
         f"workers={solved.get('_workers')} trials={solved.get('_trials')}）")
    return True


async def stage_send(t: ChromeCdpTransport) -> bool:
    head("[4] 真实单轮对话（会消耗额度）")
    sess = SessionMeta()
    deltas: list[str] = []
    thoughts: list[str] = []

    async def on_delta(s: str) -> None:
        deltas.append(s)
        print(s, end="", flush=True)

    async def on_thought(s: str) -> None:
        thoughts.append(s)

    line("  发送中…")
    t0 = time.monotonic()
    out = await t.send(
        "用一句话回答：1+1 等于几？",
        sess,
        extended_thinking=False,
        search_enabled=False,
        on_delta=on_delta,
        on_thought=on_thought,
    )
    ms = (time.monotonic() - t0) * 1000
    line()
    line(f"  用时        : {ms:.0f} ms（PoW {out.pow_ms:.0f} ms, {out.pow_mode}）")
    line(f"  会话 cid    : {out.cid}")
    line(f"  rid / rcid : {out.rid} / {out.rcid}")
    line(f"  正文长度    : {len(out.text)}")
    line(f"  思考长度    : {len(out.thoughts)}")
    line(f"  增量帧数    : {len(deltas)}")
    line(f"  error      : {out.error or '（无）'}")
    line(f"  正文        : {out.text[:200]}")
    if out.error:
        line("  [错误] 本轮有 error")
        return False
    if not out.text:
        line("  [错误] 正文为空")
        return False
    if not deltas:
        line("  [错误] 一个增量都没收到 —— 流式通道没通")
        return False
    line("  [通过] 流式通道正常")
    return True


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="start",
                    choices=["start", "pow", "send"])
    ap.add_argument("--i-know", action="store_true",
                    help="确认知道 send 阶段会消耗额度")
    ap.add_argument("--headless", action="store_true",
                    help="用 headless（仅调试；UA 会带 HeadlessChrome 标记）")
    ap.add_argument("--profile", default="", help="复用指定 profile 目录")
    args = ap.parse_args()

    token = load_token(TOKEN_FILE)
    if not token:
        line(f"[错误] 没有 token：{TOKEN_FILE}")
        return 2

    if args.stage == "send":
        if os.environ.get("GAD_ALLOW_LIVE_AUTH") != "1":
            line("[错误] 需要先设 GAD_ALLOW_LIVE_AUTH=1")
            return 2
        if not args.i_know:
            line("[错误] send 阶段会真的发消息并消耗额度，需加 --i-know")
            return 2

    t = ChromeCdpTransport(
        token_provider=lambda: token,
        offscreen=not args.headless,
        headless=args.headless,
        profile_dir=args.profile or None,
    )
    ok = False
    try:
        ok = await stage_start(t)
        if ok and args.stage in ("pow", "send"):
            ok = await stage_pow(t)
        if ok and args.stage == "send":
            ok = await stage_send(t)
    finally:
        await t.stop()

    line()
    line("=" * 74)
    line("[通过] 全部检查通过" if ok else "[失败] 有检查未通过")
    line("=" * 74)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
