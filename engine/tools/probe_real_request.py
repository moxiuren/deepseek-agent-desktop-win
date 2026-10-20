"""真实请求画像探针 —— 阶段 0 闸门（只读）。

目的
----
抓取 DeepSeek 网页版发一条消息时的**完整请求画像**，与本地直连实现
（`deepseek-mac/windows/DeepSeekApiClient.cs`）逐项对比，找出缺口。

为什么必须先做这一步
--------------------
2026-10-14 查明：原版直连「跑没多久就封号，封到一个时间点后自动解封」——
这是自动风控的临时处置，只看可量化信号。已知缺口至少有：

  * `x-hif-leim`  —— 服务端在前面接口下发，真实客户端会回传，原版没带
  * `x-hif-dliq`  —— 存在 localStorage，原版没处理
  * TLS 指纹      —— 原版是 .NET Schannel 裸奔
  * PoW 求解时序  —— 真实浏览器是 wasm+Worker 异步解，原版 C# 原生 67ms

本脚本负责把第一、二项**从传闻变成事实**：看清楚它们到底长什么样、
从哪个接口来、值是什么形态。不看清楚就动手，等于重复原版的错误。

本脚本只读：不写任何文件到被观测的系统，只在你本地输出报告。

用法
----
    cd engine
    .venv/Scripts/python.exe tools/probe_real_request.py

流程：脚本拉起一个**独立 profile** 的 Chrome（不碰你日常在用的那个），
你在窗口里正常登录 DeepSeek、随便发一条消息，脚本自动捕获并出报告。

2026-10-17 升级（防封禁复盘之后）
--------------------------------
这份探针**不发任何请求** —— 请求是**你自己在真浏览器里手动发的**，
它只挂在 CDP 上旁观。所以它**不需要** `GAD_ALLOW_LIVE_AUTH` 闸门
（那把闸门管的是「我们的引擎/脚本主动打真实接口」，不是「看真实浏览器」）。

本轮补了三件事，都是为了让「登录后一次抓包」能把存疑项**一次结清**：

1. **头抓全** —— 改从 `Network.requestWillBeSentExtraInfo` 取头。
   `requestWillBeSent.request.headers` 是**不全的**，浏览器自动附加的
   `accept-language` / `sec-fetch-site|mode|dest` / `origin` 常常不在里面 ——
   既有抓包缺 `accept-language` 就是这么来的，直接导致「origin 到底发不发」
   变成存疑项（复盘 §2.4 / §8-2）。
2. **localStorage 全量导出**（不再只取 `userToken` 白名单）。
3. **cookie 全量导出**（默认只记名字/长度，不落值）**+ UUID 候选扫描** ——
   直接回答「`x-device-id` 到底存在哪」这个至今未确认的问题（复盘 §2.3）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PORT = 9333
START_URL = "https://chat.deepseek.com/"
TARGET_PATHS = (
    "/api/v0/chat/create_pow_challenge",
    "/api/v0/chat/completion",
)
WATCH_HEADERS = (
    "authorization",
    "x-hif-leim",
    "x-hif-dliq",
    "x-ds-pow-response",
    "x-client-platform",
    "x-client-version",
    "x-client-locale",
    "x-client-bundle-id",
    "user-agent",
    "cookie",
)
#: 用来在 localStorage / cookie 的值里找 UUID —— **定位 `x-device-id` 的来源**。
#:
#: 2026-10-17 防封禁复盘 §2.3：同账号上出现了两个设备身份，正确做法是复用浏览器
#: 那个。但截至当时**来源未确认** —— 既有抓包只快照了 `userToken` 一个键
#: （早期版本的 `LS_KEYS` 白名单太窄），所以「它在 localStorage 里」这个说法
#: **没有证据**。于是本探针改成：**全量导出 localStorage + cookie**，并扫出所有
#: 「长得像 UUID」的值，列成候选表 —— 登录后跑一次，一眼就能看出 device-id
#: 到底存在哪。（`LS_KEYS` 白名单已删除，改为全量键枚举。）
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)

OUT_DIR = Path(__file__).resolve().parent.parent.parent / "docs" / "evidence"


def log(msg: str) -> None:
    print(msg, flush=True)


# ---------- 浏览器发现（与 gemini 侧同构，Windows 优先） ----------


def find_browser() -> str:
    local = os.environ.get("LOCALAPPDATA", "")
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    cands = [
        os.path.join(pf, "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(pf86, "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(local, "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(pf86, "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(pf, "Microsoft", "Edge", "Application", "msedge.exe"),
    ]
    for path in cands:
        if path and os.path.isfile(path):
            return path
    for exe in ("chrome", "chrome.exe", "msedge", "msedge.exe"):
        found = shutil.which(exe)
        if found:
            return found
    raise SystemExit("[错误] 找不到 Chrome 或 Edge。请先安装其中之一。")


def cdp_alive(port: int, timeout: float = 1.5) -> dict | None:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/version", timeout=timeout
        ) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def page_target(port: int, timeout: float = 2.0) -> dict | None:
    """找一个可用的 page target（优先 deepseek 页）。"""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/list", timeout=timeout
        ) as resp:
            targets = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    pages = [t for t in targets if t.get("type") == "page"]
    for t in pages:
        if "deepseek.com" in (t.get("url") or ""):
            return t
    return pages[0] if pages else None


class Cdp:
    """极简 CDP 客户端。事件与响应混在一条流里，按 id 分流。"""

    def __init__(self, ws) -> None:
        self._ws = ws
        self._next_id = 0
        self.events: list[dict] = []

    async def call(self, method: str, params: dict | None = None,
                   timeout: float = 20.0) -> dict:
        self._next_id += 1
        msg_id = self._next_id
        payload: dict = {"id": msg_id, "method": method}
        if params:
            payload["params"] = params
        await self._ws.send(json.dumps(payload))
        deadline = time.monotonic() + timeout
        while True:
            remain = deadline - time.monotonic()
            if remain <= 0:
                raise TimeoutError(f"CDP {method} 超时")
            raw = await asyncio.wait_for(self._ws.recv(), timeout=remain)
            data = json.loads(raw)
            if data.get("id") != msg_id:
                if data.get("method"):
                    self.events.append(data)
                continue
            if "error" in data:
                raise RuntimeError(f"CDP {method} 错误：{data['error']}")
            return data.get("result") or {}

    async def drain(self, seconds: float) -> list[dict]:
        """在给定时长内持续收事件（不阻塞在单条命令上）。"""
        got: list[dict] = []
        deadline = time.monotonic() + seconds
        while True:
            remain = deadline - time.monotonic()
            if remain <= 0:
                break
            try:
                raw = await asyncio.wait_for(self._ws.recv(), timeout=min(remain, 2.0))
            except TimeoutError:
                continue
            except Exception:  # noqa: BLE001 - 探测脚本：连接断了就停收，异常本身已由外层记录
                break
            data = json.loads(raw)
            if data.get("method"):
                got.append(data)
        return got


# ---------- 画像提取 ----------


def pick_headers(headers: dict, all_api: bool = False) -> dict:
    out = {}
    for k, v in headers.items():
        lk = k.lower()
        if all_api or lk in WATCH_HEADERS:
            if lk == "cookie":
                out[lk] = f"<{len(v.split(';'))} 项，已省略>"
            elif lk == "authorization":
                # **凭据一律不落盘**。原实现留了前 24 字符（`Bearer ` + token 前 17 位），
                # 那已经是凭据片段 —— 报告会进 `docs/evidence/`，等于把 token 写进仓库。
                # 只留长度：足够判断「发没发、格式对不对」，不足以复原。
                out[lk] = f"<已省略 len={len(v)}>"
            else:
                out[lk] = v
    return out


def classify(url: str, all_api: bool = False) -> str | None:
    if all_api:
        if "/api/v0/" in url:
            return url.split("?")[0].replace("https://chat.deepseek.com", "")
        return None
    for p in TARGET_PATHS:
        if p in url:
            return p
    return None


def _launch(exe: str, profile: Path) -> subprocess.Popen:
    return subprocess.Popen(
        [
            exe,
            f"--remote-debugging-port={PORT}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            START_URL,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


async def run(all_api: bool = False, attach: bool = False) -> int:
    exe = find_browser()
    profile = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "DeepSeek-Probe" / "chrome-profile"
    profile.mkdir(parents=True, exist_ok=True)

    log("=" * 68)
    log("DeepSeek 真实请求画像探针（只读）" + ("  [全量 API 模式]" if all_api else ""))
    log("=" * 68)

    proc: subprocess.Popen | None = None
    if attach:
        if not cdp_alive(PORT):
            log(f"[错误] --attach 指定复用，但端口 {PORT} 不通。")
            return 2
        log(f"[1/5] 复用端口 {PORT} 上已开着的浏览器（不重开窗口）")
    else:
        log(f"[1/5] 浏览器   : {exe}")
        log(f"      独立 profile: {profile}")
        if cdp_alive(PORT):
            log(f"[!] 端口 {PORT} 已被占用 —— 用 --attach 复用，或先关掉那个窗口。")
            return 2
        proc = _launch(exe, profile)

    log("[2/5] 等待调试端口就绪…")
    ver = None
    for _ in range(40):
        ver = cdp_alive(PORT)
        if ver:
            break
        await asyncio.sleep(0.5)
    if not ver:
        if proc:
            proc.terminate()
        log("[错误] 调试端口没起来。Chrome 在 profile 被占用时会忽略该参数。")
        return 3
    log(f"      OK: {ver.get('Browser')}")

    target = page_target(PORT)
    if not target:
        log("[错误] 找不到 page target。")
        return 4
    ws_url = target["webSocketDebuggerUrl"]
    log(f"[3/5] 已连上页面: {target.get('url')}")

    import websockets

    captured: dict[str, dict] = {}
    #: 每个 kind 当前已知的**原始**头（主事件 + ExtraInfo 合并结果）。
    #: 单独存是因为 ExtraInfo 常常**晚于**主事件到达，需要能回头重算。
    raw_by_kind: dict[str, dict] = {}
    rid_to_kind: dict[str, str] = {}
    extra_by_rid: dict[str, dict] = {}
    ls_snapshot: dict = {}
    cookie_snapshot: list[dict] = []
    uuid_like: dict[str, str] = {}

    def _apply(kind: str) -> None:
        """把当前已知的原始头重算进 captured（ExtraInfo 合并后调用）。"""
        captured[kind]["headers"] = pick_headers(raw_by_kind.get(kind) or {}, all_api)

    async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as ws:
        cdp = Cdp(ws)
        await cdp.call("Network.enable")
        await cdp.call("Runtime.enable")

        log("")
        log("-" * 68)
        log("请在那个浏览器窗口里操作：")
        log("  1) 登录你的 DeepSeek 账号（如果没登录）")
        log("  2) 随便发一条消息，比如：你好")
        log("  3) 等回答出来")
        log("脚本会自动捕获，最多等 300 秒。")
        log("-" * 68)
        log("")

        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            events = await cdp.drain(2.0)
            for ev in events:
                method = ev.get("method")
                params = ev.get("params") or {}

                # ★ **真实发出的头**只在这里拿得全。
                #
                # `Network.requestWillBeSent.request.headers` 是**不全的** ——
                # 浏览器自动附加的 `accept-language` / `sec-fetch-site|mode|dest` /
                # `origin` 等常常不在里面。既有抓包（20261014-234856）缺
                # `accept-language` 就是吃了这个亏，导致「origin 到底发不发」
                # 变成存疑项（复盘 §2.4 / §8-2）。
                # `requestWillBeSentExtraInfo` 给的是**真正上网的那份头**，
                # 还附带 `associatedCookies`。补上它，存疑项就能一次性结清。
                if method == "Network.requestWillBeSentExtraInfo":
                    rid = params.get("requestId")
                    hdr = params.get("headers") or {}
                    extra_by_rid[rid] = hdr
                    kind = rid_to_kind.get(rid)
                    if kind and kind in raw_by_kind:
                        raw_by_kind[kind].update(hdr)
                        _apply(kind)
                        log(f"[补全] {kind} ← ExtraInfo（+{len(hdr)} 头）")
                    continue

                if method != "Network.requestWillBeSent":
                    continue
                rid = params.get("requestId")
                req = params.get("request") or {}
                url = req.get("url", "")
                kind = classify(url, all_api)
                if not kind or kind in captured:
                    continue
                rid_to_kind[rid] = kind
                raw = dict(req.get("headers") or {})
                if rid in extra_by_rid:
                    raw.update(extra_by_rid[rid])
                raw_by_kind[kind] = raw
                captured[kind] = {
                    "url": url,
                    "method": req.get("method"),
                    "headers": pick_headers(raw, all_api),
                    "post_data": (req.get("postData") or "")[:4000],
                    "captured_at": datetime.now().isoformat(timespec="seconds"),
                }
                log(f"[捕获] {kind}")
                for k, v in captured[kind]["headers"].items():
                    log(f"        {k}: {v}")

            done = (
                "/api/v0/chat/completion" in captured
                if all_api
                else len(captured) >= len(TARGET_PATHS)
            )
            if not done:
                continue

            # 再等一会儿，让 completion 的 ExtraInfo 补进来（它常晚于主事件）。
            for ev in await cdp.drain(1.5):
                if ev.get("method") != "Network.requestWillBeSentExtraInfo":
                    continue
                p = ev.get("params") or {}
                kind = rid_to_kind.get(p.get("requestId"))
                if kind and kind in raw_by_kind:
                    raw_by_kind[kind].update(p.get("headers") or {})
                    _apply(kind)
                    log(f"[补全] {kind} ← ExtraInfo（延迟到达）")

            # ---- localStorage：**全量**键（不再只看白名单）----
            try:
                r = await cdp.call(
                    "Runtime.evaluate",
                    {
                        "expression": "Object.keys(window.localStorage)",
                        "returnByValue": True,
                    },
                )
                keys = (r.get("result") or {}).get("value") or []
                for key in keys:
                    rr = await cdp.call(
                        "Runtime.evaluate",
                        {
                            "expression": f"window.localStorage.getItem({json.dumps(key)})",
                            "returnByValue": True,
                        },
                    )
                    val = (rr.get("result") or {}).get("value")
                    if val is None:
                        continue
                    # **凭据不落盘**：`userToken` 这类键的值就是完整 token，
                    # 原实现截断 200 字符照样是明文落盘（报告进 `docs/evidence/`）。
                    # 疑似凭据键只记长度；其它键保留截断值用于定位 device-id。
                    #
                    # ★ 2026-10-18 补漏：原关键词表只有 token/secret/auth/session/password，
                    # 漏掉了 `settingsJwt`（值里含完整 JWT）、`smidV2`（会话标识）、
                    # `.thumbcache_*`（未知用途的长 token）—— 上一份证据文件因此把
                    # `settingsJwt` 的 JWT 原样落了盘。现补上这几类。
                    # `hif_leim_cached` 例外：它是研究目标，保留前 8 字符用于对照
                    # （两次抓包的前缀不同 ⇒ 证明它会在轮换），但完整值不落盘。
                    low = key.lower()
                    if "hif_leim" in low:
                        ls_snapshot[key] = f"<已省略 len={len(val)} prefix={val[:8]}>"
                    elif any(
                        s in low
                        for s in (
                            "token", "secret", "auth", "session", "password",
                            "jwt", "smid", "thumbcache", "waf",
                        )
                    ):
                        ls_snapshot[key] = f"<已省略 len={len(val)}>"
                    else:
                        ls_snapshot[key] = val[:200] + ("…" if len(val) > 200 else "")
                    m = _UUID_RE.search(val)
                    if m:
                        uuid_like[f"localStorage:{key}"] = m.group(0)
            except Exception as exc:  # noqa: BLE001
                ls_snapshot["_error"] = str(exc)

            # ---- cookie：**默认不落值**（可能含会话凭据），只记名字与长度；
            #      名字里带 device 的才留一小段前缀，用于定位 device-id 来源 ----
            try:
                c = await cdp.call("Network.getAllCookies")
                for ck in c.get("cookies") or []:
                    name = ck.get("name") or ""
                    val = ck.get("value") or ""
                    item = {
                        "name": name,
                        "domain": ck.get("domain"),
                        "httpOnly": bool(ck.get("httpOnly")),
                        "value_len": len(val),
                    }
                    if "device" in name.lower():
                        item["value_preview"] = val[:80] + ("…" if len(val) > 80 else "")
                        m = _UUID_RE.search(val)
                        if m:
                            uuid_like[f"cookie:{name}"] = m.group(0)
                    cookie_snapshot.append(item)
            except Exception as exc:  # noqa: BLE001
                cookie_snapshot.append({"_error": str(exc)})

            break

    # ---------- 出报告 ----------
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = OUT_DIR / f"probe-real-request-{stamp}.json"
    out.write_text(
        json.dumps(
            {
                "captured": captured,
                "local_storage": ls_snapshot,
                "cookies": cookie_snapshot,
                "uuid_like": uuid_like,
                "port": PORT,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    log("")
    log("=" * 68)
    log("[4/5] 缺口对照（对照 deepseek-mac/windows/DeepSeekApiClient.cs）")
    log("=" * 68)

    have_leim = "x-hif-leim" in (captured.get("/api/v0/chat/completion", {}).get("headers") or {})
    have_dliq = "x-hif-dliq" in (captured.get("/api/v0/chat/completion", {}).get("headers") or {})
    log(f"  x-hif-leim   真实网页版: {'有' if have_leim else '未捕获'}   原版直连: 无")
    log(f"  x-hif-dliq   真实网页版: {'有' if have_dliq else '未捕获'}   原版直连: 无")
    log(f"  PoW 挑战接口 : {'已捕获' if '/api/v0/chat/create_pow_challenge' in captured else '未捕获'}")
    log(f"  completion   : {'已捕获' if '/api/v0/chat/completion' in captured else '未捕获'}")

    # ---- 头集合完整度：这是本轮探针升级的重点 ----
    for kind, entry in captured.items():
        hdrs = entry.get("headers") or {}
        has_al = "accept-language" in hdrs
        has_sf = any(k.startswith("sec-fetch-") for k in hdrs)
        has_origin = "origin" in hdrs
        log(
            f"  头完整度 {kind}: {len(hdrs)} 个 · "
            f"accept-language={'有' if has_al else '无'} · "
            f"sec-fetch-*={'有' if has_sf else '无'} · "
            f"origin={'有' if has_origin else '无'}"
        )

    log(f"  localStorage : {len([k for k in ls_snapshot if not k.startswith('_')])} 个键")
    log(f"  cookie       : {len(cookie_snapshot)} 项（值未落盘）")

    # ---- device-id 来源候选：直接回答「它到底存在哪」 ----
    if uuid_like:
        log("  ★ 疑似 device-id 候选（值形如 UUID）：")
        for where, val in uuid_like.items():
            log(f"      {where} = {val}")
    else:
        log("  ★ 未在 localStorage / cookie 中找到 UUID 形态的值 ——")
        log("    device-id 可能来自 IndexedDB 或运行时生成，需另找。")

    log("")
    log(f"[5/5] 报告已写入: {out}")
    log("")
    log("下一步：把这份报告与 DeepSeekApiClient.cs 逐项比对，")
    log("补上缺失的头，再上 curl_cffi 做 TLS 伪装。")
    log("")
    if proc:
        log(f"探针浏览器仍在运行（PID {proc.pid}）。用完请手动关掉那个窗口。")
    else:
        log("（本轮复用已有浏览器，未新建进程；窗口保持原样。）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--all-api",
        action="store_true",
        help="抓取全部 /api/v0/ 请求（用于找「创建会话」等未知接口）",
    )
    ap.add_argument(
        "--attach",
        action="store_true",
        help="复用已在 9333 端口上开着的浏览器，不重开窗口",
    )
    args = ap.parse_args()
    try:
        return asyncio.run(run(all_api=args.all_api, attach=args.attach))
    except KeyboardInterrupt:
        log("\n[中断] 用户取消。")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
