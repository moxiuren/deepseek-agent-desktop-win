"""阶段 1 冒烟测试 —— 用正式 transport 真发一轮消息。

验证四件事：
  1. `Transport` 生命周期（start / health / stop）
  2. `send()` 返回 `TurnOutcome`，且 `text` 非空
  3. `on_delta` 回调**逐字收到增量**（不是只在最后一次性给全文）
  4. 会话三元组滚动更新（`cid` / `rid` 被填上，且第二轮能续上）

用法
----
    cd engine
    # 方式 A：从正在运行的调试浏览器取 token（profile 已登录）
    ./.venv/Scripts/python.exe tools/smoke_transport.py --cdp-port 9333
    # 方式 B：直接给 token
    ./.venv/Scripts/python.exe tools/smoke_transport.py --token <userToken>
    # 加 --second 再发第二轮，验证 parent_message_id 链
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gad_engine.session import SessionMeta
from gad_engine.transport import DeepSeekRpcTransport, TransportError

OUT_DIR = Path(__file__).resolve().parent.parent.parent / "docs" / "evidence"


def log(msg: str) -> None:
    print(msg, flush=True)


def read_token_cdp(port: int) -> str:
    """从调试端口读 localStorage.userToken（需要浏览器已登录）。"""
    import websockets

    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=3.0) as r:
        targets = json.loads(r.read().decode("utf-8"))
    pages = [t for t in targets if t.get("type") == "page"]
    target = next(
        (t for t in pages if "deepseek.com" in (t.get("url") or "")), None
    ) or (pages[0] if pages else None)
    if not target:
        raise RuntimeError("找不到可用的 page target")

    async def _get() -> str:
        async with websockets.connect(
            target["webSocketDebuggerUrl"], max_size=8 * 1024 * 1024
        ) as ws:
            async def call(method: str, params: dict) -> dict:
                await ws.send(json.dumps({"id": 1, "method": method, "params": params}))
                while True:
                    data = json.loads(await ws.recv())
                    if data.get("id") == 1:
                        if "error" in data:
                            raise RuntimeError(f"CDP {method}: {data['error']}")
                        return data.get("result") or {}

            await call("Runtime.enable", {})
            res = await call(
                "Runtime.evaluate",
                {
                    "expression": "window.localStorage.getItem('userToken')",
                    "returnByValue": True,
                },
            )
            raw = (res.get("result") or {}).get("value")
            if not raw:
                raise RuntimeError("localStorage 无 userToken —— 浏览器未登录")
            try:
                parsed = json.loads(raw)
                return parsed.get("value") if isinstance(parsed, dict) else raw
            except (ValueError, TypeError):
                return raw

    return asyncio.run(_get())


async def run(token: str, prompt: str, second: bool) -> int:
    log("=" * 70)
    log("阶段 1 冒烟测试 —— DeepSeekRpcTransport 端到端")
    log("=" * 70)

    tp = DeepSeekRpcTransport(token=token)
    log(f"[1/5] start() … impersonate={tp.background_state()['impersonate']}")
    await tp.start()
    log(f"      OK · device_id={tp.background_state()['device_id']}")

    log("[2/5] health() …")
    st = await tp.health()
    log(f"      reachable={st.reachable} · {st.account_status_short or '-'} · {st.note}")

    log("[3/5] available_models() …")
    models = tp.available_models()
    log(f"      {[m.name for m in models]}")

    session = SessionMeta(temporary=True)
    deltas: list[str] = []

    async def on_delta(chunk: str) -> None:
        deltas.append(chunk)
        print(chunk, end="", flush=True)

    log(f"[4/5] send() 第一轮：{prompt!r}")
    log("      流式输出 → ", )
    t0 = time.monotonic()
    try:
        out1 = await tp.send(prompt, session, on_delta=on_delta)
    except TransportError as exc:
        log(f"\n[失败] TransportError(auth={exc.auth}, fatal={exc.fatal}, config={exc.config}): {exc}")
        await tp.stop()
        return 2
    elapsed = time.monotonic() - t0

    log("")
    log(f"      ok={out1.ok} · {elapsed:.1f}s · 正文 {len(out1.text)} 字 · 增量 {len(deltas)} 段")
    log(f"      cid={out1.cid}")
    log(f"      rid={out1.rid} · rcid={out1.rcid}")
    if out1.error:
        log(f"      error={out1.error}")
    log(f"      会话三元组：{session.to_dict()}")

    ok = bool(out1.text) and not out1.error
    joined_deltas = "".join(deltas)
    checks = {
        "收到正文": bool(out1.text),
        # 增量流应非空，且拼起来是最终正文的后半段（前半段来自快照）
        "on_delta 收到增量": bool(deltas),
        "增量与正文自洽": bool(joined_deltas) and out1.text.endswith(joined_deltas),
        "cid 已填": bool(session.cid),
        "rid 已填": bool(session.rid),
    }

    out2 = None
    if second and ok:
        log("")
        log("[5/5] send() 第二轮（验证 parent_message_id 链）…")
        deltas2: list[str] = []

        async def on_delta2(chunk: str) -> None:
            deltas2.append(chunk)
            print(chunk, end="", flush=True)

        try:
            out2 = await tp.send("用一句话确认你收到了上文", session, on_delta=on_delta2)
            log("")
            log(f"      ok={out2.ok} · 正文 {len(out2.text)} 字 · cid 是否复用={out2.cid == out1.cid}")
            checks["第二轮成功"] = bool(out2.text) and not out2.error
            checks["同会话续接（cid 未变）"] = out2.cid == out1.cid
        except TransportError as exc:
            log(f"\n[第二轮失败] {exc}")
            checks["第二轮成功"] = False
    else:
        log("")
        log("[5/5] 跳过第二轮（加 --second 启用）")

    await tp.stop()

    log("")
    log("-" * 70)
    for k, v in checks.items():
        log(f"  [{'通过' if v else '失败'}] {k}")
    passed = all(checks.values())

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = OUT_DIR / f"smoke-transport-{stamp}.json"
    out_path.write_text(
        json.dumps(
            {
                "impersonate": tp.background_state()["impersonate"],
                "elapsed_s": round(elapsed, 1),
                "checks": checks,
                "first": {
                    "text": out1.text,
                    "chars": len(out1.text),
                    "delta_count": len(deltas),
                    "cid": out1.cid,
                    "rid": out1.rid,
                    "rcid": out1.rcid,
                    "error": out1.error,
                },
                "second": None
                if out2 is None
                else {
                    "text": out2.text,
                    "chars": len(out2.text),
                    "cid": out2.cid,
                    "rid": out2.rid,
                    "error": out2.error,
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    log(f"  报告：{out_path}")
    log("")
    log("  [阶段 1 冒烟通过]" if passed else "  [阶段 1 冒烟未通过]")
    return 0 if passed else 3


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", default="")
    ap.add_argument("--cdp-port", type=int, default=9333)
    ap.add_argument("--prompt", default="你好，请只回复两个字：收到")
    ap.add_argument("--second", action="store_true", help="再发第二轮，验证会话续接")
    args = ap.parse_args()

    token = args.token.strip()
    if not token:
        log(f"[准备] 从 CDP 端口 {args.cdp_port} 取 token …")
        try:
            token = read_token_cdp(args.cdp_port).strip()
        except (urllib.error.URLError, OSError, RuntimeError) as exc:
            log(f"[错误] 取 token 失败：{exc}")
            log("       请先用调试端口拉起已登录的浏览器，或直接给 --token。")
            return 1
    if not token:
        log("[错误] token 为空")
        return 1
    log(f"[准备] token len={len(token)}")

    try:
        return asyncio.run(run(token, args.prompt, args.second))
    except KeyboardInterrupt:
        log("\n[中断]")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
