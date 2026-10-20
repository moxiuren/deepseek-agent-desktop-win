"""浏览器登录 —— 起一个专用浏览器，经 CDP 把 DeepSeek token 取回来。

**这是全项目唯一需要浏览器的环节。** 取完即可关掉，对话全程不再需要它。

为什么必须走 CDP
----------------

DeepSeek 的凭据是 `localStorage.userToken` —— **`localStorage` 只有页面里的 JS
能读到**，磁盘上没有可离线解析的副本（不像 cookie 还有文件可寻）。

原版（`deepseek-mac`）靠 WebView2 的 `ExecuteScriptAsync` 读它，那是它**唯一**
的 WebView2 依赖点。本模块用 CDP 顶替：连一个**独立 profile** 的 Chrome，
`Runtime.evaluate` 执行一小段 JS 把值取出来。

为什么用专用 profile
--------------------

Chrome 对「同一个 user-data-dir 已经在运行」会把新进程**转交给旧进程**并
**忽略** `--remote-debugging-port`。用独立目录既绕开这个坑，也不碰用户的日常 Chrome。

与 gemini 版的差异（不是抄漏）
-----------------------------

| | Gemini 版 | **本模块** |
|---|---|---|
| 取什么 | `Storage.getCookies` 取两个 cookie | `Runtime.evaluate` 读 `localStorage` |
| 主要竞态 | **`__Secure-1PSIDTS` 会在页面加载后轮换**，旧值随即失效 ⇒ 必须等它「稳定」 | 无已知轮换竞态，但仍读两次求稳 |
| 有效性 | 取到即可用 | **必须额外验一次**（token 可能是旧的 / 被服务端作废） |

> **有效性验证为什么必须做**：DeepSeek 的 `401` **无法区分「token 过期」与
> 「账号被处置」**（见 `circuit.py`）。如果不验就存，用户会看到「登录成功」
> 却在第一次对话时炸 —— 而且分不清是哪个原因。所以本模块提供
> `validate_token()`，用一次极轻的 `create_pow_challenge` 把这件事问清楚。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

DEEPSEEK_URL = "https://chat.deepseek.com/"
DEFAULT_PORT = 9333

# 读 token 的 JS。**必须容错**：localStorage 里存的可能是裸串，
# 也可能是 `{"value": "..."}` 形态的 JSON（实测是后者）。
_TOKEN_JS = """
(() => {
  try {
    const raw = window.localStorage.getItem('userToken');
    if (!raw) return '';
    try {
      const p = JSON.parse(raw);
      // ★ 解析成功就**只能**从 value 取。
      // 未登录时 DeepSeek 写的是 {"value":null,"__version":"0"} —— 解析成功、
      // value 为 null。原来这里写的是 `if (p && typeof p === 'object' && p.value)`，
      // 条件为假就继续往下落到 `return String(raw)`，于是**整段 JSON 被当成 token**
      // （30 字节），一路上被当成「读到了登录态」，最后把好 token 覆盖掉。
      // 空就是空，解析成功之后绝不允许退回裸串。
      if (p && typeof p === 'object') return p.value ? String(p.value) : '';
    } catch (e) { /* 不是 JSON，按裸串处理 */ }
    return String(raw);
  } catch (e) { return ''; }
})()
"""

# 认账号名（best-effort）。DeepSeek 的 localStorage 里**只有 userToken**，
# 没有邮箱/手机号，所以只能从页面文本里捞。认不出不影响主流程。
_LABEL_JS = """
(() => {
  const pat = /([A-Za-z0-9._%+\\-]+@[A-Za-z0-9.\\-]+\\.[A-Za-z]{2,})/;
  const html = document.documentElement ? document.documentElement.outerHTML : '';
  const m = html.match(pat);
  return m ? m[1] : '';
})()
"""


class BrowserLoginError(RuntimeError):
    """浏览器登录流程中的可展示错误。"""


@dataclass
class BrowserInfo:
    name: str
    path: str


def _candidates() -> list[tuple[str, str]]:
    """自动探测的候选（不含用户显式指定的路径 —— 那条由 find_browser 直接处理）。"""
    local = os.environ.get("LOCALAPPDATA", "")
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    return [
        ("Chrome", os.path.join(pf, "Google", "Chrome", "Application", "chrome.exe")),
        ("Chrome", os.path.join(pf86, "Google", "Chrome", "Application", "chrome.exe")),
        ("Chrome", os.path.join(local, "Google", "Chrome", "Application", "chrome.exe")),
        # Edge 同为 Chromium，CDP 接口一致 —— 没有 Chrome 时兜底
        ("Edge", os.path.join(pf86, "Microsoft", "Edge", "Application", "msedge.exe")),
        ("Edge", os.path.join(pf, "Microsoft", "Edge", "Application", "msedge.exe")),
    ]


def find_browser(explicit: str = "") -> BrowserInfo:
    """按 Chrome → Edge 的顺序找一个可用的 Chromium 浏览器。

    **显式指定时不做回退**：`explicit` 给了路径却不存在，直接报错，不静默改用
    自动探测到的浏览器 —— 否则用户配错 `identity.chrome_binary` 会毫无察觉，
    程序用着另一个浏览器却以为在用自己的配置（典型的「假绿」）。
    """
    if explicit.strip():
        path = explicit.strip()
        if not os.path.isfile(path):
            raise BrowserLoginError(
                f"配置里指定的浏览器不存在：{path}\n"
                "请修正 identity.chrome_binary，或清空它改用自动探测。"
            )
        return BrowserInfo(name="自定义", path=path)

    for name, path in _candidates():
        if path and os.path.isfile(path):
            return BrowserInfo(name=name, path=path)

    for exe in ("chrome", "chrome.exe", "msedge", "msedge.exe"):
        found = shutil.which(exe)
        if found:
            return BrowserInfo(name="Chrome" if "chrome" in exe else "Edge", path=found)

    raise BrowserLoginError(
        "找不到 Chrome 或 Edge。请安装其中之一，"
        "或在配置里指定 identity.chrome_binary 指向浏览器可执行文件。"
    )


def default_profile_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "DeepSeek-Agent" / "chrome-profile"


# ---------- CDP 底层 ----------


def cdp_alive(port: int, timeout: float = 1.5) -> dict | None:
    """探测调试端口。返回 `/json/version` 的内容，不可达则 None。"""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/version", timeout=timeout
        ) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


class _Cdp:
    """极简 CDP 客户端：一次只发一条命令，跳过无关事件。"""

    def __init__(self, ws) -> None:
        self._ws = ws
        self._next_id = 0

    async def call(
        self,
        method: str,
        params: dict | None = None,
        session_id: str | None = None,
        timeout: float = 20.0,
    ) -> dict:
        self._next_id += 1
        msg_id = self._next_id
        payload: dict = {"id": msg_id, "method": method}
        if params:
            payload["params"] = params
        if session_id:
            payload["sessionId"] = session_id
        await self._ws.send(json.dumps(payload))

        deadline = time.monotonic() + timeout
        while True:
            remain = deadline - time.monotonic()
            if remain <= 0:
                raise BrowserLoginError(f"CDP {method} 等待响应超时")
            raw = await asyncio.wait_for(self._ws.recv(), timeout=remain)
            data = json.loads(raw)
            if data.get("id") != msg_id:
                continue  # 事件或其他命令的响应，忽略
            if "error" in data:
                raise BrowserLoginError(f"CDP {method} 返回错误：{data['error']}")
            return data.get("result") or {}


def normalize_token(raw: object) -> str:
    """把各种形态的 token 输入归一成裸串。

    接受三种输入（用户手粘时都会遇到）：
      * 裸 token 串
      * `{"value": "...", ...}` 形态的 JSON（**这是 localStorage 里的真实形态**）
      * 带引号的 JSON 字符串
    """
    if raw is None:
        return ""
    if isinstance(raw, dict):
        v = raw.get("value")
        return str(v).strip() if v else ""
    text = str(raw).strip()
    if not text:
        return ""
    if text.startswith("{"):
        try:
            obj = json.loads(text)
        except ValueError:
            pass
        else:
            if isinstance(obj, dict):
                # ★ 解析成功就**只能**从 value 取（与 _TOKEN_JS 同一条道理）。
                # 原来这里写的是 `if ... and obj.get("value")`，条件为假就继续
                # 往下落到末尾的 `return text`，把 `{"value":null,"__version":"0"}`
                # 整段当成 token 返回 —— 这正是把好凭据覆盖掉的那 30 字节。
                v = obj.get("value")
                return str(v).strip() if v else ""
    # 去掉可能被一起粘进来的引号与 `Bearer ` 前缀
    text = text.strip().strip('"').strip("'")
    if text.lower().startswith("bearer "):
        text = text[7:].strip()
    return text


def _pick_page(targets: list[dict]) -> dict | None:
    pages = [t for t in targets if t.get("type") == "page"]
    return next(
        (t for t in pages if "deepseek.com" in (t.get("url") or "")), None
    ) or (pages[0] if pages else None)


async def _read_token_once(port: int) -> tuple[str, str]:
    """读一次 (token, 账号名 best-effort)。"""
    import websockets

    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/list", timeout=3.0
        ) as r:
            targets = json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise BrowserLoginError(f"连不上 CDP 端口 {port}：{exc}") from exc

    target = _pick_page(targets)
    if not target:
        raise BrowserLoginError("找不到可用的 page target")

    async with websockets.connect(
        target["webSocketDebuggerUrl"], max_size=8 * 1024 * 1024
    ) as ws:
        cdp = _Cdp(ws)

        async def evaluate(expr: str) -> str:
            res = await cdp.call(
                "Runtime.evaluate", {"expression": expr, "returnByValue": True}
            )
            val = (res.get("result") or {}).get("value")
            return val if isinstance(val, str) else ""

        await cdp.call("Runtime.enable", {})
        token = normalize_token(await evaluate(_TOKEN_JS))
        label = ""
        try:
            label = (await evaluate(_LABEL_JS)).strip()
        except BrowserLoginError:
            label = ""

        # ★ 顺带抄走浏览器里的 device-id。
        #
        # 2026-10-17 23:18 全量抓包确认：请求头的 `x-device-id` 就是
        # `localStorage["deepseek-device-id:chat"]`。此前引擎自己 `uuid4()` 造了一个，
        # 导致同一账号出现**两个设备身份**（聚合层 L4 的可疑信号）。
        #
        # 读 token 的这一刻正是「浏览器已登录」的时机，该键必然存在 —— 顺手拿走，
        # 后续请求就用同一个身份。失败**不影响登录**（它只是指纹对齐项，不是凭据）。
        #
        # 延迟 import：`deepseek_rpc` 依赖较重（curl_cffi），且模块级 import 有
        # 循环依赖风险（`identity` 两侧都用）。
        try:
            from .transport.deepseek_rpc import DEVICE_ID_LS_KEY, save_device_id

            did = await evaluate(
                f"window.localStorage.getItem({json.dumps(DEVICE_ID_LS_KEY)})"
            )
            if did:
                save_device_id(did)
        except Exception:  # noqa: BLE001
            pass
        return token, label


async def read_token_from_cdp_async(port: int = DEFAULT_PORT) -> str:
    """`read_token_from_cdp` 的协程形态。

    **在 `Engine.handle()` 里必须用这个。** handle 是 `async def`，在里面调同步
    版会触发 `asyncio.run()` 的「cannot be called from a running event loop」，
    而异常从 `identity.refresh` 里冒出去之后，前端只看到一条 RPC 错误 ——
    看着像凭据坏了，其实是代码坏了（真实发生过）。
    """
    token, _ = await _read_token_once(port)
    if not token:
        raise BrowserLoginError("localStorage 无 userToken —— 浏览器未登录 DeepSeek")
    if len(token) < 20:
        raise BrowserLoginError(f"token 形态异常（len={len(token)}）")
    return token


async def read_token_and_label_async(port: int = DEFAULT_PORT) -> tuple[str, str]:
    """`read_token_and_label` 的协程形态（同上，给事件循环里的调用方）。"""
    return await _read_token_once(port)


def read_token_from_cdp(port: int = DEFAULT_PORT) -> str:
    """同步读一次 token（读不到即抛错）。

    保留这个同步入口，是因为探针与一次性脚本用起来最顺手。
    **不要在事件循环里调它** —— 用 `read_token_from_cdp_async`。
    """
    return asyncio.run(read_token_from_cdp_async(port))


def read_token_and_label(port: int = DEFAULT_PORT) -> tuple[str, str]:
    """同步版（同上）。**不要在事件循环里调它**。"""
    return asyncio.run(read_token_and_label_async(port))


# ---------- 登录会话 ----------


@dataclass
class BrowserLoginSession:
    """一次「打开浏览器 → 等登录 → 取 token」的会话。

    生命周期由调用方掌握：`start()` → 反复 `poll()` → `close_browser()`。
    """

    profile_dir: Path = field(default_factory=default_profile_dir)
    port: int = DEFAULT_PORT
    browser_path: str = ""
    url: str = DEEPSEEK_URL
    headless: bool = False

    #: 连续多少次 `poll()` 读到**同一个** token 才认「登录态稳定」。
    #:
    #: DeepSeek 侧没有观测到 Gemini 那种「页面加载即轮换凭据」的竞态，
    #: 所以默认只需 2 次（求稳，成本极低）—— 两次读到同一个值就落盘。
    settle_reads: int = 2

    _proc: subprocess.Popen | None = field(default=None, init=False, repr=False)
    _info: BrowserInfo | None = field(default=None, init=False, repr=False)
    _label: str = field(default="", init=False, repr=False)
    _settle_count: int = field(default=0, init=False, repr=False)
    _settle_last: str = field(default="", init=False, repr=False)

    # ---------- 启动 ----------

    def start(self) -> dict:
        """启动（或复用）浏览器。返回给界面展示的信息。"""
        existing = cdp_alive(self.port)
        if existing:
            return {
                "browser": self._info.name if self._info else "已有实例",
                "path": self._info.path if self._info else "",
                "profile": str(self.profile_dir),
                "port": self.port,
                "url": self.url,
                "reused": True,
                "already_running": True,
            }

        info = find_browser(self.browser_path)
        self._info = info
        self.profile_dir.mkdir(parents=True, exist_ok=True)

        args = [
            info.path,
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self.profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=Translate",
        ]
        if self.headless:
            args.append("--headless=new")
        args.append(self.url)

        creationflags = 0
        if os.name == "nt":
            # 让浏览器不随引擎进程一起被回收
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        self._proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )

        # 等调试端点起来。起不来通常是「该 profile 已被一个没开调试端口的
        # 浏览器实例占着」—— Chrome 会把新进程转交给旧进程并忽略端口参数。
        for _ in range(60):
            if cdp_alive(self.port):
                return {
                    "browser": info.name,
                    "path": info.path,
                    "profile": str(self.profile_dir),
                    "port": self.port,
                    "url": self.url,
                    "reused": False,
                    "already_running": False,
                }
            if self._proc.poll() is not None:
                raise BrowserLoginError(
                    f"{info.name} 启动后立即退出（退出码 {self._proc.returncode}）。"
                    "请确认浏览器可正常手动打开。"
                )
            time.sleep(0.5)

        raise BrowserLoginError(
            f"{info.name} 已启动，但调试端口 {self.port} 一直没起来。"
            f"通常是这个专用配置目录已被另一个浏览器实例占用 —— "
            f"请先关掉所有用「{self.profile_dir}」打开的窗口再重试。"
        )

    # ---------- 轮询 ----------

    async def poll(self) -> dict:
        """读一次 token。

        state:
          waiting     —— 还没登录（读不到 token）
          settling    —— 读到了，但还没连续读到同一个值
          ready       —— 拿到稳定 token
          no_browser  —— 浏览器已关闭
        """
        if not cdp_alive(self.port):
            return {"state": "no_browser"}

        try:
            token, label = await _read_token_once(self.port)
        except BrowserLoginError as exc:
            return {"state": "error", "error": str(exc)}

        if not token:
            self._settle_count = 0
            self._settle_last = ""
            return {"state": "waiting", "missing": ["localStorage.userToken"]}

        if token == self._settle_last:
            self._settle_count += 1
        else:
            self._settle_count = 1
        self._settle_last = token

        if self._settle_count < max(1, self.settle_reads):
            return {
                "state": "settling",
                "settle": self._settle_count,
                "need": self.settle_reads,
            }

        if label and not self._label:
            self._label = label
        return {
            "state": "ready",
            "token": token,
            "account_label": self._label,
            "browser": self._info.name if self._info else "",
            "profile": str(self.profile_dir),
        }

    async def wait_for_login(self, timeout_sec: float = 300.0, interval: float = 1.0) -> dict:
        """一直轮询到 ready / 超时。给「无人值守也要能取到」的场景用。"""
        deadline = time.monotonic() + timeout_sec
        last: dict = {"state": "waiting"}
        while time.monotonic() < deadline:
            last = await self.poll()
            if last["state"] in ("ready", "no_browser", "error"):
                return last
            await asyncio.sleep(interval)
        last = dict(last)
        last["state"] = "timeout"
        return last

    # ---------- 收尾 ----------

    async def close_browser(self) -> bool:
        """优雅关掉浏览器（比杀进程干净，能落盘 profile）。"""
        version = cdp_alive(self.port)
        if not version:
            self._proc = None
            return False
        try:
            import websockets

            ws_url = version.get("webSocketDebuggerUrl")
            if not ws_url:
                return False
            async with websockets.connect(ws_url, max_size=8 * 1024 * 1024) as ws:
                await _Cdp(ws).call("Browser.close", timeout=8.0)
            self._proc = None
            return True
        except Exception:  # noqa: BLE001 - 关不掉就交给下面的兜底
            return self._kill()

    def _kill(self) -> bool:
        proc = self._proc
        self._proc = None
        if proc is None or proc.poll() is not None:
            return False
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            return True
        except Exception:  # noqa: BLE001
            return False

    @property
    def launched_by_us(self) -> bool:
        return self._proc is not None


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def extract_email(text: str) -> str:
    """从一段文本里捞邮箱（账号名的兜底来源）。"""
    m = _EMAIL_RE.search(text or "")
    return m.group(0) if m else ""
