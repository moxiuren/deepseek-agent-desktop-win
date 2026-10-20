"""链路卡点诊断 —— 单轮真发，逐事件打时间戳，超时可控。

目的：回答「完整链路为什么卡住」。与 smoke_transport 的区别：
  * 从 `engine/.token` 读凭据（不把 token 放命令行）
  * 每一步（start / health / send / 每个 delta / 超时）都打**相对时间**
  * 内置硬超时（默认 150s），超时也能拿到「卡在第几阶段」
  * 自动把原始 SSE 落到 `docs/evidence/sse/`

用法
----
    cd engine
    ./.venv/Scripts/python.exe tools/diag_link.py
    ./.venv/Scripts/python.exe tools/diag_link.py --prompt "只回复两个字：收到" --timeout 90
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gad_engine.session import SessionMeta  # noqa: E402
from gad_engine.transport import DeepSeekRpcTransport, TransportError  # noqa: E402

SSE_DIR = ROOT.parent / "docs" / "evidence" / "sse"


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def log(msg: str) -> None:
    print(f"[{ts()}] {msg}", flush=True)


async def run(prompt: str, timeout: float, temporary: bool, search: bool) -> int:
    token = (ROOT / ".token").read_text(encoding="utf-8").strip()
    if not token:
        log("[错误] .token 为空 —— 先跑身份获取")
        return 1

    SSE_DIR.mkdir(parents=True, exist_ok=True)
    os.environ["DS_SSE_DUMP"] = str(SSE_DIR)
    log(f"准备：token len={len(token)} · SSE 转储 → {SSE_DIR}")

    tp = DeepSeekRpcTransport(token=token)
    t0 = time.monotonic()

    log("[1] start() …")
    await tp.start()
    log(f"    OK +{time.monotonic() - t0:.2f}s · device={tp.background_state().get('device_id')}")

    log("[2] health() …")
    st = await tp.health()
    log(f"    reachable={st.reachable} · {st.account_status_short or '-'} · {st.note}")

    session = SessionMeta(temporary=temporary)
    deltas: list[str] = []

    def on_delta(chunk: str) -> None:
        deltas.append(chunk)
        log(f"    delta#{len(deltas):<3} +{time.monotonic() - t1:.2f}s  {chunk!r}")

    log(f"[3] send() 开始 · temporary={temporary} · search={search} · 硬超时 {timeout:.0f}s")
    t1 = time.monotonic()
    try:
        out = await asyncio.wait_for(
            tp.send(
                prompt,
                session,
                temporary=temporary,
                search_enabled=search,
                on_delta=on_delta,
            ),
            timeout=timeout,
        )
    except TimeoutError:
        log(f"[卡点] TIMEOUT +{time.monotonic() - t1:.1f}s · 收到 delta={len(deltas)} 段")
        log("       含义：流建立后长时间无终止帧，或某阶段（PoW/create）根本没回来")
        await tp.stop()
        return 4
    except TransportError as exc:
        log(f"[失败] TransportError +{time.monotonic() - t1:.1f}s "
            f"auth={exc.auth} fatal={exc.fatal} config={exc.config}")
        log(f"       {exc}")
        await tp.stop()
        return 2

    el = time.monotonic() - t1
    log(f"[4] send() 返回 +{el:.1f}s")
    log(f"    ok={out.ok} · 正文 {len(out.text)} 字 · 增量 {len(deltas)} 段")
    log(f"    text={out.text!r}")
    if out.error:
        log(f"    error={out.error}")
    log(f"    cid={out.cid} rid={out.rid} rcid={out.rcid}")

    await tp.stop()
    log(f"完成 · 总耗时 +{time.monotonic() - t0:.1f}s")
    return 0 if (out.text and not out.error) else 3


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", default="只回复两个字：收到")
    ap.add_argument("--timeout", type=float, default=150.0)
    ap.add_argument("--temporary", action="store_true", help="开临时会话（默认关）")
    ap.add_argument("--search", action="store_true", help="开联网搜索（对应网页版的搜索按钮）")
    args = ap.parse_args()
    try:
        return asyncio.run(run(args.prompt, args.timeout, args.temporary, args.search))
    except KeyboardInterrupt:
        log("[中断]")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
