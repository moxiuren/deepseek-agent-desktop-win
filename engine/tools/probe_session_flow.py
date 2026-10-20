"""阶段 0.2 完整流程探针 —— 走通 create_session → challenge → completion。

背景（2026-10-14 23:48 实测查明）
--------------------------------
第一次尝试直连发消息，服务端回：

    {"code":0,"data":{"biz_code":1,"biz_msg":"invalid chat session id"}}

⇒ **`chat_session_id` 不能客户端自己编**，必须调 `/api/v0/chat_session/create` 拿。

同时全量抓包发现原版直连还缺三个头：

    x-device-id              持久设备 UUID
    x-client-timezone-offset 时区偏移（本机 28800 = +8h）
    x-hif-leim               只在 completion 里出现，来源未知

本脚本负责：
  1. 调 `chat_session/create` 拿 session_id，**并打印响应头**（找 x-hif-leim 来源）
  2. 调 `create_pow_challenge` 拿挑战，同样打印响应头
  3. 解 PoW
  4. 调 `completion`，看能否收到正常 SSE

用法
----
    cd engine
    ./.venv/Scripts/python.exe tools/probe_session_flow.py --dry-run   # 只到第 2 步，不发消息
    ./.venv/Scripts/python.exe tools/probe_session_flow.py             # 完整（会真的发消息）
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

import argparse
import asyncio
import base64
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gad_engine.pow import Challenge, solve

BASE = "https://chat.deepseek.com"
CDP_PORT = 9333
IMPERSONATE = "chrome150"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)
OUT_DIR = Path(__file__).resolve().parent.parent.parent / "docs" / "evidence"

# 设备标识：真实浏览器生成一次后复用。这里持久化到本地，避免每次换新 id。
DEVICE_ID_FILE = Path(__file__).resolve().parent.parent / ".device-id"
# 关注的头：可能是 x-hif-leim 的来源
WATCH_RESP_HEADERS = ("x-hif", "x-ds-", "set-cookie", "x-device", "x-request")


def log(msg: str) -> None:
    print(msg, flush=True)


def device_id() -> str:
    if DEVICE_ID_FILE.exists():
        v = DEVICE_ID_FILE.read_text(encoding="utf-8").strip()
        if v:
            return v
    v = str(uuid.uuid4())
    DEVICE_ID_FILE.write_text(v, encoding="utf-8")
    return v


# ---------- token ----------


async def _read_token(port: int) -> str:
    import websockets

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2.0) as r:
            targets = json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise RuntimeError(f"连不上 CDP 端口 {port}：{exc}") from exc
    pages = [t for t in targets if t.get("type") == "page"]
    target = next((t for t in pages if "deepseek.com" in (t.get("url") or "")), None) or (
        pages[0] if pages else None
    )
    if not target:
        raise RuntimeError("找不到可用的 page target")

    async with websockets.connect(target["webSocketDebuggerUrl"], max_size=8 * 1024 * 1024) as ws:
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
            {"expression": "window.localStorage.getItem('userToken')", "returnByValue": True},
        )
        raw = (res.get("result") or {}).get("value")
        if not raw:
            raise RuntimeError("localStorage 无 userToken —— 浏览器未登录")
        try:
            parsed = json.loads(raw)
            token = parsed.get("value") if isinstance(parsed, dict) else raw
        except (ValueError, TypeError):
            token = raw
        if not token or len(token) < 20:
            raise RuntimeError("token 形态异常")
        return token


# ---------- 头 ----------


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


def dump_response(resp, label: str) -> dict:
    """打印并返回响应头里值得关注的部分。"""
    found: dict[str, str] = {}
    for k, v in resp.headers.items():
        lk = k.lower()
        if any(w in lk for w in WATCH_RESP_HEADERS):
            found[k] = v
    log(f"      [{label}] 响应头关注项：{found if found else '（无命中）'}")
    return found


# ---------- 三步 ----------


def step_create_session(session, token: str, dev: str) -> tuple[str, dict]:
    url = f"{BASE}/api/v0/chat_session/create"
    resp = session.post(url, headers=headers_for(token, dev), json={}, timeout=30)
    log(f"  HTTP {resp.status_code}")
    if resp.status_code != 200:
        raise RuntimeError(f"create_session 失败：{resp.status_code} {resp.text[:200]}")
    heads = dump_response(resp, "chat_session/create")
    body = resp.json()
    if body.get("code") != 0:
        raise RuntimeError(f"create_session 业务错误：{body}")
    biz = body.get("data", {}).get("biz_data") or {}
    # 实测结构（2026-10-14 23:51）：data.biz_data.chat_session.id
    session_obj = biz.get("chat_session") or {}
    sid = session_obj.get("id") or biz.get("id") or biz.get("chat_session_id") or ""
    if not sid:
        raise RuntimeError(f"响应里没有 session id：{json.dumps(body)[:300]}")
    return sid, heads


def step_challenge(session, token: str, dev: str, referer: str) -> tuple[Challenge, dict]:
    url = f"{BASE}/api/v0/chat/create_pow_challenge"
    resp = session.post(
        url,
        headers=headers_for(token, dev, referer),
        json={"target_path": "/api/v0/chat/completion"},
        timeout=30,
    )
    log(f"  HTTP {resp.status_code}")
    if resp.status_code != 200:
        raise RuntimeError(f"challenge 失败：{resp.status_code} {resp.text[:200]}")
    heads = dump_response(resp, "create_pow_challenge")
    body = resp.json()
    if body.get("code") != 0:
        raise RuntimeError(f"challenge 业务错误：{body}")
    return Challenge.from_api(body["data"]["biz_data"]["challenge"]), heads


def step_completion(
    session, token: str, dev: str, referer: str, pow_header: str,
    sid: str, prompt: str, extra: dict,
) -> dict:
    url = f"{BASE}/api/v0/chat/completion"
    h = headers_for(token, dev, referer)
    h["accept"] = "text/event-stream"
    h["x-ds-pow-response"] = pow_header
    h.update(extra)  # 例如 x-hif-leim

    payload = {
        "chat_session_id": sid,
        "parent_message_id": None,
        "model_type": "default",
        "prompt": prompt,
        "ref_file_ids": [],
        "thinking_enabled": False,
        "search_enabled": False,
        "action": None,
        "preempt": False,
    }

    raw_lines: list[str] = []
    chunks: list[str] = []
    started = time.monotonic()
    with session.stream("POST", url, headers=h, json=payload, timeout=120) as resp:
        log(f"  HTTP {resp.status_code}")
        if resp.status_code != 200:
            raise RuntimeError(f"completion 失败：{resp.status_code} {resp.read()[:200]!r}")
        for line in resp.iter_lines():
            if isinstance(line, bytes):
                line = line.decode("utf-8", "replace")
            if not line:
                continue
            raw_lines.append(line)
            if not line.startswith("data: "):
                continue
            data = line[6:].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except ValueError:
                continue
            _collect(obj, chunks)

    return {
        "elapsed_s": round(time.monotonic() - started, 1),
        "chars": sum(len(c) for c in chunks),
        "text": "".join(chunks)[:300],
        "raw_count": len(raw_lines),
        "raw_head": raw_lines[:6],
    }


def _collect(node, chunks: list[str]) -> None:
    if isinstance(node, list):
        for i in node:
            _collect(i, chunks)
        return
    if not isinstance(node, dict):
        return
    for k in ("content", "text", "response", "answer"):
        v = node.get(k)
        if isinstance(v, str) and v:
            chunks.append(v)
    for k in ("choices", "v", "data"):
        if k in node:
            _collect(node[k], chunks)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", default="")
    ap.add_argument("--prompt", default="ping")
    ap.add_argument("--dry-run", action="store_true", help="只走到第 2 步，不发消息")
    ap.add_argument("--port", type=int, default=CDP_PORT)
    args = ap.parse_args()

    from curl_cffi import requests as creq

    log("=" * 70)
    log("阶段 0.2 完整流程探针 —— create_session → challenge → completion")
    log("=" * 70)

    token = args.token.strip() or asyncio.run(_read_token(args.port))
    dev = device_id()
    log(f"[准备] token len={len(token)} · x-device-id={dev}")

    session = creq.Session(impersonate=IMPERSONATE)
    log(f"[准备] curl_cffi impersonate={IMPERSONATE}")

    log("")
    log("[步骤 1] chat_session/create")
    try:
        sid, _ = step_create_session(session, token, dev)
    except Exception as exc:  # noqa: BLE001
        log(f"[失败] {exc}")
        return 2
    referer = f"{BASE}/a/chat/s/{sid}"
    log(f"      OK —— session_id={sid}")

    log("")
    log("[步骤 2] create_pow_challenge")
    try:
        ch, _ = step_challenge(session, token, dev, referer)
    except Exception as exc:  # noqa: BLE001
        log(f"[失败] {exc}")
        return 3
    log(f"      OK —— difficulty={ch.difficulty}")

    if args.dry_run:
        log("")
        log("[dry-run] 到此为止，未发送任何消息。")
        return 0

    log("")
    log("[步骤 3] 解 PoW")
    t0 = time.monotonic()
    solved = solve(ch, "/api/v0/chat/completion")
    log(f"      OK —— answer={solved['answer']}（{time.monotonic()-t0:.1f}s）")
    pow_header = base64.b64encode(
        json.dumps(
            {k: v for k, v in solved.items() if not k.startswith("_")},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).decode("ascii")

    log("")
    log("[步骤 4] completion（**真的发一条消息**）")
    try:
        result = step_completion(
            session, token, dev, referer, pow_header, sid, args.prompt, extra={}
        )
    except Exception as exc:  # noqa: BLE001
        log(f"[失败] {exc}")
        return 4

    log(f"      {result['elapsed_s']}s · {result['chars']} 字 · 原始行 {result['raw_count']}")
    for i, ln in enumerate(result["raw_head"]):
        log(f"        [{i}] {ln[:170]}")
    if result["text"]:
        log(f"      正文：{result['text'][:200]}")
    else:
        log("      >>> 仍为 0 字 —— 若响应是 biz_code 错误，说明还缺某个头（见步骤 1/2 的响应头关注项）")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = OUT_DIR / f"probe-session-flow-{stamp}.json"
    out.write_text(
        json.dumps(
            {
                "impersonate": IMPERSONATE,
                "device_id": dev,
                "session_id": sid,
                "difficulty": ch.difficulty,
                "answer": solved["answer"],
                "result": result,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    log(f"      报告：{out}")

    ok = result["chars"] > 0
    log("")
    log("  [0.2 通过] 完整链路走通，收到真实回复。" if ok else "  [0.2 未通过] 仍未收到正文，见上方原始行。")
    return 0 if ok else 5


if __name__ == "__main__":
    raise SystemExit(main())
