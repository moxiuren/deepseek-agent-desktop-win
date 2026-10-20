"""阶段 0.2 闸门探针 —— curl_cffi 直连（会真的发请求）。

目的
----
回答一个问题：**补上 TLS 指纹与正确的头之后，DeepSeek 认不认我们？**

与阶段 0.1 的区别：0.1 是**观察**真实浏览器在发什么；0.2 是**亲自发**，
用 `curl_cffi` 伪装成 Chrome，看能否走通完整链路。

两段判据
--------
* **0.2a**（默认先跑）：调 `create_pow_challenge` —— 只需鉴权，不需要解 PoW。
  能拿到 challenge ⇒ **TLS 指纹 + 请求头 + token 全部被接受**。
* **0.2b**（`--full`）：解 PoW 后调 `completion`，看能否收到正常 SSE 回复。

风险提示
--------
**0.2b 会真的向账号发出一条消息**（消耗额度、计入风控观测）。
用 `--challenge-only` 可只跑 0.2a，不发任何消息。

用法
----
    cd engine
    ./.venv/Scripts/python.exe tools/probe_curl_cffi.py                  # 只跑 0.2a
    ./.venv/Scripts/python.exe tools/probe_curl_cffi.py --full           # 跑完整链路
    ./.venv/Scripts/python.exe tools/probe_curl_cffi.py --token <TOKEN>  # 手动给 token
"""

from __future__ import annotations

# ★ 直连鉴权探测的闸门 —— **默认拒绝**。
#   本脚本自建 curl_cffi 会话直连 DeepSeek，**不走传输层**，
#   所以 `deepseek_rpc._probe_once` 的 `GAD_ALLOW_LIVE_AUTH` 闸门拦不住它。
#   2026-10-17 账号 `shaomo17` 被 DeepSeek 封禁的直接原因，就是这类直连探测
#   （含 `'x'*64` 垃圾 token 撞鉴权）。宁可打不出去，也不能再触发风控。
import os as _os

if _os.environ.get("GAD_ALLOW_LIVE_AUTH") != "1":
    raise SystemExit(
        "已阻止直连鉴权探测（GAD_ALLOW_LIVE_AUTH 未设为 1）。\n"
        "本脚本绕过传输层闸门，故自带闸门 —— 默认拒绝；确需真打请显式设 =1。"
    )

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

from gad_engine.pow import Challenge, solve

BASE = "https://chat.deepseek.com"
CDP_PORT = 9333
# curl_cffi 0.16.3 最高支持 chrome150；真实浏览器是 155。
# 差 5 个版本完全正常（用户没升级），**关键是 UA 与 TLS 指纹自洽**。
IMPERSONATE = "chrome150"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"

OUT_DIR = Path(__file__).resolve().parent.parent.parent / "docs" / "evidence"


def log(msg: str) -> None:
    print(msg, flush=True)


# ---------- 取 token（从还开着的探针浏览器） ----------


def _cdp_target(port: int) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2.0) as r:
            targets = json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    pages = [t for t in targets if t.get("type") == "page"]
    for t in pages:
        if "deepseek.com" in (t.get("url") or ""):
            return t
    return pages[0] if pages else None


async def _read_token(port: int) -> str:
    import websockets

    target = _cdp_target(port)
    if not target:
        raise RuntimeError(
            f"连不上 CDP 端口 {port}。请先跑 probe_real_request.py 打开探针浏览器并登录，"
            "或用 --token 手动指定。"
        )
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
            {
                "expression": "window.localStorage.getItem('userToken')",
                "returnByValue": True,
            },
        )
        raw = (res.get("result") or {}).get("value")
        if not raw:
            raise RuntimeError("localStorage 里没有 userToken —— 浏览器里还没登录。")
        try:
            parsed = json.loads(raw)
            token = parsed.get("value") if isinstance(parsed, dict) else raw
        except (ValueError, TypeError):
            token = raw
        if not token or len(token) < 20:
            raise RuntimeError("取到的 token 形态异常（过短）。")
        return token


def get_token(port: int) -> str:
    return asyncio.run(_read_token(port))


# ---------- 请求头 ----------


def headers_for(token: str) -> dict:
    """浏览器标准头交给 curl_cffi 的 impersonate 自动生成，这里只补 DeepSeek 特有头。"""
    return {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "authorization": f"Bearer {token}",
        "content-type": "application/json",
        "origin": BASE,
        "referer": BASE + "/",
        "user-agent": UA,
        "x-client-bundle-id": "com.deepseek.chat",
        "x-client-locale": "en_US",
        "x-client-platform": "web",
        "x-client-version": "2.5.0",
    }


# ---------- 0.2a：取挑战 ----------


def create_challenge(session, token: str, target_path: str) -> Challenge:
    url = f"{BASE}/api/v0/chat/create_pow_challenge"
    resp = session.post(
        url,
        headers=headers_for(token),
        json={"target_path": target_path},
        timeout=30,
    )
    log(f"  HTTP {resp.status_code}")
    if resp.status_code != 200:
        raise RuntimeError(f"create_pow_challenge 失败：HTTP {resp.status_code} {resp.text[:300]}")
    body = resp.json()
    if body.get("code") != 0:
        raise RuntimeError(f"接口返回非 0：code={body.get('code')} msg={body.get('msg')}")
    biz = body["data"]["biz_data"]["challenge"]
    return Challenge.from_api(biz)


# ---------- 0.2b：发一轮 ----------


def send_completion(session, token: str, pow_header: str, prompt: str, session_id: str) -> dict:
    url = f"{BASE}/api/v0/chat/completion"
    h = headers_for(token)
    h["x-ds-pow-response"] = pow_header
    h["accept"] = "text/event-stream"

    payload = {
        "chat_session_id": session_id,
        "parent_message_id": None,
        "model_type": "default",
        "prompt": prompt,
        "ref_file_ids": [],
        "thinking_enabled": False,
        "search_enabled": False,
        "action": None,
        "preempt": False,
    }

    chunks: list[str] = []
    msg_ids: list[int] = []
    raw_lines: list[str] = []
    started = time.monotonic()

    with session.stream("POST", url, headers=h, json=payload, timeout=120) as resp:
        log(f"  HTTP {resp.status_code}")
        if resp.status_code != 200:
            raw = resp.read()
            raise RuntimeError(f"completion 失败：HTTP {resp.status_code} {raw[:300]!r}")
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
            _collect(obj, chunks, msg_ids)

    return {
        "elapsed_s": round(time.monotonic() - started, 1),
        "chars": sum(len(c) for c in chunks),
        "preview": "".join(chunks)[:200],
        "msg_ids": msg_ids[-3:],
        "raw_line_count": len(raw_lines),
        "raw_head": raw_lines[:40],
    }


def _collect(node, chunks: list[str], msg_ids: list[int]) -> None:
    """递归找文本增量与消息 id（结构未知，多形态兜底）。"""
    if isinstance(node, list):
        for item in node:
            _collect(item, chunks, msg_ids)
        return
    if not isinstance(node, dict):
        return
    for key in ("message_id", "msg_id"):
        v = node.get(key)
        if isinstance(v, int):
            msg_ids.append(v)
    for key in ("content", "text", "response", "answer"):
        v = node.get(key)
        if isinstance(v, str) and v:
            chunks.append(v)
    for key in ("choices", "v", "data"):
        if key in node:
            _collect(node[key], chunks, msg_ids)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", default="", help="手动指定 token（默认从 CDP 读）")
    ap.add_argument("--full", action="store_true", help="跑完整链路（会真的发出一条消息）")
    ap.add_argument("--challenge-only", action="store_true", help="只跑 0.2a（默认行为）")
    ap.add_argument("--prompt", default="你好", help="--full 时发送的内容")
    ap.add_argument("--port", type=int, default=CDP_PORT)
    args = ap.parse_args()

    from curl_cffi import requests as creq

    log("=" * 68)
    log("阶段 0.2 探针 —— curl_cffi 直连")
    log("=" * 68)

    token = args.token.strip()
    if not token:
        log(f"[1/4] 从 CDP 端口 {args.port} 读 token…")
        try:
            token = get_token(args.port)
        except Exception as exc:  # noqa: BLE001
            log(f"[失败] {exc}")
            return 2
        log(f"      OK（len={len(token)}）")
    else:
        log("[1/4] 使用手动指定的 token")

    session = creq.Session(impersonate=IMPERSONATE)
    log(f"[2/4] curl_cffi 会话已建（impersonate={IMPERSONATE}）")

    log("[3/4] 0.2a —— 调 create_pow_challenge（验证 TLS 指纹 + 头 + token）")
    try:
        ch = create_challenge(session, token, "/api/v0/chat/completion")
    except Exception as exc:  # noqa: BLE001
        log(f"[失败] {exc}")
        log("")
        log(">>> 若为 401/403：说明 TLS 指纹或头仍被识别，需要继续调整。")
        log(">>> 若为超时/连接错误：检查网络或代理环境变量。")
        return 3
    log(f"      OK —— algorithm={ch.algorithm} difficulty={ch.difficulty}")
    log(f"      challenge={ch.challenge[:32]}…  expire_at={ch.expire_at}")
    log("")
    log("  [0.2a 通过] TLS 指纹 + 请求头 + token 全部被接受。")

    if not args.full or args.challenge_only:
        log("")
        log("[4/4] 跳过 0.2b（未指定 --full）。")
        log("      >>> 至此已证明「直连能被接受」；下一步加 --full 验证完整链路。")
        return 0

    log("")
    log("[4/4] 0.2b —— 解 PoW 并发送一轮对话（**会真的发出消息**）")
    log(f"      求解中（difficulty={ch.difficulty}，纯 Python 可能耗时数十秒）…")
    t0 = time.monotonic()
    try:
        solved = solve(ch, "/api/v0/chat/completion")
    except Exception as exc:  # noqa: BLE001
        log(f"[失败] PoW 求解：{exc}")
        return 4
    log(f"      OK —— answer={solved['answer']}（{time.monotonic()-t0:.1f}s）")

    import base64

    pow_header = base64.b64encode(
        json.dumps(
            {k: v for k, v in solved.items() if not k.startswith("_")},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).decode("ascii")

    import uuid

    session_id = str(uuid.uuid4())
    log(f"      发送中（chat_session_id={session_id}）…")
    try:
        result = send_completion(session, token, pow_header, args.prompt, session_id)
    except Exception as exc:  # noqa: BLE001
        log(f"[失败] {exc}")
        return 5

    log(f"      OK —— {result['elapsed_s']}s，收到 {result['chars']} 字")
    log(f"      原始行数：{result['raw_line_count']}")
    for i, ln in enumerate(result["raw_head"][:12]):
        log(f"        [{i}] {ln[:170]}")
    log(f"      回复预览：{result['preview']}")
    log(f"      message_id：{result['msg_ids']}")
    log("")
    log("  [0.2b 通过] 完整链路走通 —— 直连方案成立。")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = OUT_DIR / f"probe-curl-cffi-{stamp}.json"
    out.write_text(
        json.dumps(
            {
                "impersonate": IMPERSONATE,
                "challenge": {
                    "algorithm": ch.algorithm,
                    "difficulty": ch.difficulty,
                    "expire_at": ch.expire_at,
                },
                "solved_answer": solved["answer"],
                "solve_ms": solved["_elapsed_ms"],
                "result": result,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    log(f"      报告：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
