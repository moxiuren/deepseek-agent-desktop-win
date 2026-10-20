"""完整链路诊断 —— 走 AgentRuntime 编排层，逐事件打时间线。

与 diag_link.py 的区别：那个测**裸 transport**，这个测**闸门 + 编排 + 工具循环**。
上一轮「发一条消息 184s 无返回」发生在这一层，所以要在这里复现。

用法
----
    cd engine
    ./.venv/Scripts/python.exe tools/diag_full.py --thinking off
    ./.venv/Scripts/python.exe tools/diag_full.py --thinking on --timeout 240
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gad_engine.agent import AgentRuntime  # noqa: E402
from gad_engine.config import EngineConfig  # noqa: E402


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--goal", default="只回复两个字：收到")
    ap.add_argument("--timeout", type=float, default=240.0)
    ap.add_argument("--thinking", choices=["on", "off"], default="")
    ap.add_argument("--search", action="store_true", help="开联网搜索")
    ap.add_argument("--max-turns", type=int, default=0)
    args = ap.parse_args()

    cfg = EngineConfig.load()
    if args.thinking:
        cfg.chat.extended_thinking = args.thinking == "on"
    if args.search:
        cfg.chat.search_enabled = True
    if args.max_turns:
        cfg.max_tool_turns = args.max_turns
    print(
        f"[{ts()}] 配置：transport={cfg.transport} · thinking={cfg.chat.extended_thinking} · "
        f"search={cfg.chat.search_enabled} · "
        f"max_tool_turns={cfg.max_tool_turns} · label={cfg.identity.account_label!r}",
        flush=True,
    )

    t0 = time.monotonic()

    def on_event(kind: str, data: dict) -> None:
        el = time.monotonic() - t0
        body = json.dumps(data, ensure_ascii=False)
        if len(body) > 500:
            body = body[:500] + " …"
        print(f"[{ts()}] +{el:6.1f}s  {kind:<14} {body}", flush=True)

    async def go() -> None:
        rt = AgentRuntime(cfg, on_event=on_event)
        await rt.start()
        st = rt.status()
        print(
            f"[{ts()}] started · transport_ready={st.get('transport', {}).get('ready')} · "
            f"decision={json.dumps(st.get('decision'), ensure_ascii=False)}",
            flush=True,
        )
        try:
            rep = await asyncio.wait_for(rt.run(args.goal), timeout=args.timeout)
            print(f"[{ts()}] === RUN DONE +{time.monotonic() - t0:.1f}s ===", flush=True)
            print(json.dumps(rep.to_dict(), ensure_ascii=False, indent=2)[:4000], flush=True)
        except TimeoutError:
            print(f"[{ts()}] === RUN TIMEOUT {args.timeout:.0f}s ===", flush=True)
        finally:
            await rt.stop()

    asyncio.run(go())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
