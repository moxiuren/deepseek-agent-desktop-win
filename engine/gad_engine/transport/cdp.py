"""极薄的 Chrome DevTools Protocol 客户端 —— 只做产品真正用到的那几件事。

## 为什么不用 playwright / selenium

`docs/evidence/20261018-指纹比对/` 那轮的 GitHub 侦察结论是「零新依赖即可」：
本机已装 `websockets`，CDP 本身就是「websocket 上跑 JSON-RPC」。引入 playwright
的代价是 30 MB 浏览器下载 + 一条与我们目标冲突的路径（它默认改 UA / 加
`--enable-automation`），而这些正是我们要避免的。所以自己写这一层，
只保留五个动作：连接、调用、求值、等条件、导航。

## ★ 核心难点：命令响应与事件共用一条 websocket

CDP 的所有消息（命令响应 + 事件推送）都走同一条连接。天真写法
`await ws.send(...)` 然后 `await ws.recv()` 会在**并发调用**时互相抢帧：
A 命令的响应被 B 的 `recv` 读走，两边都挂死。

证据目录里的探测脚本为此写了「不跑事件循环」的规避（见
`probe_deepseek_frontend.py` 第 122 行注释）。产品代码绕不过去 —— 流式回答
完全依赖 `Runtime.bindingCalled` 事件。

正确骨架（puppeteer / playwright 的共同做法）：

    一个常驻读循环
      ├─ 有 id  → 是命令响应 → 交给等它的 future
      └─ 有 method → 是事件 → 交给按名字注册的队列

本文件就是这条骨架的最小实现。
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import socket
import sys
from typing import Any

__all__ = ["Cdp", "CdpError", "_KillOnCloseJob", "cleanup_stale_profiles", "free_port"]


class CdpError(RuntimeError):
    """CDP 调用失败（协议层错误或页面内 JS 抛异常）。"""


def free_port() -> int:
    """让系统分配一个空闲端口。

    **不能写死端口**：残留的 Chrome 调试实例会让 `/json/list` 返回
    **别人的**页面，脚本于是拿到一份看似正常、实际无关的结果。
    这个坑在指纹比对那轮踩过（`probe_chrome_cdp.py` 的注释里有记录）。
    """
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Cdp:
    """一条到某个 page target 的 CDP 连接。

    生命周期：`connect()` → 若干 `call()` / `eval()` / `subscribe()` → `close()`。
    """

    def __init__(self, ws) -> None:
        self.ws = ws
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._subs: dict[str, asyncio.Queue] = {}
        self._reader: asyncio.Task | None = None
        self._closed = False

    # ---------- 连接 ----------

    @classmethod
    async def connect(cls, ws_url: str) -> Cdp:
        import websockets

        # `max_size=None` 必需：`Runtime.evaluate` 的返回值里可能带整页 HTML
        # （默认 1 MiB 上限会直接断开连接，且报错信息只说是「消息过大」）。
        ws = await websockets.connect(ws_url, max_size=None)
        self = cls(ws)
        self._reader = asyncio.create_task(self._read_loop())
        return self

    async def close(self) -> None:
        self._closed = True
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress((asyncio.CancelledError, Exception)):
                await self._reader
        with contextlib.suppress(Exception):
            await self.ws.close()
        # 还挂着的调用一律唤醒成错误，别让调用方永远等下去
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(CdpError("CDP 连接已关闭"))
        self._pending.clear()

    # ---------- 读循环 ----------

    async def _read_loop(self) -> None:
        try:
            while not self._closed:
                raw = await self.ws.recv()
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                mid = msg.get("id")
                if mid is not None:
                    fut = self._pending.pop(mid, None)
                    if fut is not None and not fut.done():
                        if "error" in msg:
                            fut.set_exception(CdpError(str(msg["error"])))
                        else:
                            fut.set_result(msg.get("result") or {})
                    continue
                method = msg.get("method")
                if method:
                    q = self._subs.get(method)
                    if q is not None:
                        q.put_nowait(msg.get("params") or {})
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001  连接断了
            if not self._closed:
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(CdpError("CDP 连接中断"))
                self._pending.clear()

    # ---------- 命令 ----------

    async def call(self, method: str, params: dict | None = None,
                   timeout: float = 60.0) -> dict:
        """发一条 CDP 命令并等它的响应。"""
        if self._closed:
            raise CdpError("CDP 连接已关闭")
        mid = next(self._ids)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        await self.ws.send(json.dumps({"id": mid, "method": method,
                                       "params": params or {}}))
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(mid, None)

    # ---------- 求值 ----------

    async def eval(self, expr: str, *, await_promise: bool = False,
                   timeout: float = 60.0) -> Any:
        """在页面主世界求值一个表达式，返回其值（JSON 可序列化）。

        ★ 用 `returnByValue=True` 时，**返回 JS 对象会被自动反序列化** ——
          调用方若自己 `json.loads(...)` 会拿到 dict 而非 str 而报错。
          需要结构化数据时，**在 JS 侧包一层 `JSON.stringify`**，让返回值
          恒为字符串。这个坑在探测脚本里踩过（见 `probe_chrome_cdp.py`）。

        `await_promise=False` 是刻意的默认值：流式回答必须让 async IIFE
        **立刻返回**，否则 `Runtime.evaluate` 会一直挂到流结束，
        `on_delta` 就彻底失去意义（见 `chrome_cdp.py` 的 send）。
        """
        r = await self.call(
            "Runtime.evaluate",
            {"expression": expr, "returnByValue": True,
             "awaitPromise": await_promise},
            timeout=timeout,
        )
        if r.get("exceptionDetails"):
            d = r["exceptionDetails"]
            text = d.get("text") or ""
            exc = (d.get("exception") or {}).get("description") or ""
            raise CdpError(f"页面内 JS 异常：{text} {exc}".strip())
        return (r.get("result") or {}).get("value")

    # ---------- 事件订阅 ----------

    def subscribe(self, method: str) -> asyncio.Queue:
        """订阅一个 CDP 事件名，返回收它的队列。"""
        q: asyncio.Queue = asyncio.Queue()
        self._subs[method] = q
        return q

    def unsubscribe(self, method: str) -> None:
        self._subs.pop(method, None)

    # ---------- 组合动作 ----------

    async def wait_js(self, expr: str, timeout: float = 30.0) -> bool:
        """轮询一个 JS 表达式直到它为真。超时返回 False（不抛）。"""
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            try:
                if await self.eval(expr):
                    return True
            except CdpError:
                # 导航过程中 executionContext 会被销毁，这里报错是正常的
                pass
            await asyncio.sleep(0.2)
        return False

    async def navigate(self, url: str, timeout: float = 60.0) -> None:
        await self.call("Page.navigate", {"url": url})
        if not await self.wait_js("document.readyState==='complete'", timeout):
            raise CdpError(f"页面加载超时：{url}")

    async def add_binding(self, name: str) -> None:
        """注册一个页面可调用的函数，调用时以事件形式推回。

        这是**流式回答的唯一通道**：页面里 `fetch` 的 ReadableStream
        逐块读出来，每块调一次这个函数，Python 侧从
        `Runtime.bindingCalled` 事件收到 payload。

        ★ 每次导航后 binding 会随 executionContext 一起消失 ——
          调用方必须在导航之后、使用之前重新注册。
        """
        await self.call("Runtime.addBinding", {"name": name})

    async def wait_event(self, queue: asyncio.Queue, timeout: float = 60.0) -> dict:
        try:
            return await asyncio.wait_for(queue.get(), timeout=timeout)
        except TimeoutError as exc:
            raise CdpError("等待 CDP 事件超时") from exc


def chrome_paths() -> list[str]:
    """本机 Chrome 的候选路径（Windows）。"""
    return [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]


def find_chrome() -> str | None:
    from pathlib import Path

    for p in chrome_paths():
        if Path(p).exists():
            return p
    return None


async def wait_page_target(port: int, timeout: float = 40.0) -> dict | None:
    """轮询 `/json/list` 直到出现 page 目标。

    **不能只连一次**：调试端口在进程启动后才监听，标签页创建还要更晚。
    一次连不上就判失败是最常见的误报来源。
    """
    import urllib.request

    deadline = asyncio.get_running_loop().time() + timeout
    url = f"http://127.0.0.1:{port}/json/list"
    while asyncio.get_running_loop().time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                targets = json.loads(r.read().decode("utf-8"))
            pages = [t for t in targets if t.get("type") == "page"]
            if pages:
                return pages[0]
        except Exception:  # noqa: BLE001  还没起来
            pass
        await asyncio.sleep(0.3)
    return None


def kill_tree(proc) -> None:
    """杀掉整个进程树。

    Chrome 是**多进程**架构：`Popen` 拿到的只是浏览器主进程，渲染 / GPU /
    网络服务进程都是它的子进程。`terminate()` 只杀主进程，子进程会残留并
    继续占着 profile 目录 —— 下次启动就会撞上 `SingletonLock`。
    Windows 上 `taskkill /T` 才是彻底做法。
    """
    import subprocess

    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                   capture_output=True)
    with contextlib.suppress(Exception):
        proc.wait(timeout=8)


# --------------------------------------------------------------------------
# 父进程死亡时的兜底回收（Windows Job Object）
# --------------------------------------------------------------------------

#: `JOBOBJECT_EXTENDED_LIMIT_INFORMATION` 的 InformationClass 编号。
_JOB_EXTENDED_LIMIT = 9
#: 只设这一个标志：job 句柄一关（进程退出时内核自动关），job 里所有进程被杀。
#: **不设内存 / 进程数上限** —— Chrome 天然要几十个进程、几百 MB，设了会把它掐死。
_JOB_KILL_ON_CLOSE = 0x00002000


class _KillOnCloseJob:
    """把子进程绑进一个「父死子亡」的 job。

    ## 为什么 `taskkill /T` 不够

    `kill_tree()` 只在**正常 `stop()` 路径**上被调用。实测出现过这种孤儿
    （2026-10-18 13:25 起、父进程 21652 已不存在、`gad-chrome-s3seisif`
    目录也留着）：

      * 引擎被 `taskkill /F`（测试超时、用户强杀）
      * 引擎进程崩溃
      * 进程在 `stop()` 执行到一半时被杀

    这三种情况里，`taskkill /T` **一行都没机会执行**。而 Chrome 是独立
    会话里的常驻进程，父进程死了它照样活着。

    Job Object 把回收交给内核：句柄随父进程退出而关闭，
    `KILL_ON_JOB_CLOSE` 立刻终止 job 内全部进程。这是唯一能覆盖
    「父进程被强杀」的机制。

    ## 为什么不复用 `sandbox._JobObject`

    那个类把内存上限与进程数上限一起设了，且是给**用户代码**用的。
    Chrome 的进程数与内存都远超那些阈值 —— 复用会直接把它掐死。
    这里只需要一个 flag。

    ## 非 Windows

    直接 no-op：POSIX 上 `Popen(start_new_session=True)` + `killpg`
    才是对应做法，但本项目只在 Windows 上跑浏览器档位。
    """

    def __init__(self) -> None:
        self._handle = None

    def create(self) -> bool:
        if sys.platform != "win32":
            return False
        try:
            import ctypes

            class _IO_COUNTERS(ctypes.Structure):
                _fields_ = [(f"F{i}", ctypes.c_ulonglong) for i in range(6)]

            class _BASIC_LIMIT(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", ctypes.c_uint32),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", ctypes.c_uint32),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", ctypes.c_uint32),
                    ("SchedulingClass", ctypes.c_uint32),
                ]

            class _EXTENDED_LIMIT(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", _BASIC_LIMIT),
                    ("IoInfo", _IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = k32.CreateJobObjectW(None, None)
            if not handle:
                return False
            info = _EXTENDED_LIMIT()
            info.BasicLimitInformation.LimitFlags = _JOB_KILL_ON_CLOSE
            ok = k32.SetInformationJobObject(
                handle,
                _JOB_EXTENDED_LIMIT,
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
            if not ok:
                k32.CloseHandle(handle)
                return False
            self._handle = handle
            # 句柄留在实例上，**不要显式关闭** —— 关闭就等于立刻杀掉子进程。
            # 它的生命周期跟着本对象（进而跟着父进程）走。
            self._k32 = k32
            return True
        except Exception:  # noqa: BLE001 - 兜底机制本身失败不该阻断启动
            return False

    def assign(self, proc) -> bool:
        if self._handle is None:
            return False
        handle = getattr(proc, "_handle", None)
        if handle is None:
            return False
        try:
            return bool(self._k32.AssignProcessToJobObject(self._handle, int(handle)))
        except Exception:  # noqa: BLE001
            return False


# --------------------------------------------------------------------------
# 孤儿 profile 目录清理
# --------------------------------------------------------------------------


def _process_command_lines() -> dict[int, str]:
    """pid -> 命令行。查不到就返回空表（调用方按「不敢删」处理）。"""
    import subprocess

    ps = (
        "Get-CimInstance Win32_Process | "
        "Select-Object ProcessId,CommandLine | "
        "ConvertTo-Json -Compress"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=40,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    try:
        data = json.loads(r.stdout or "[]")
    except ValueError:
        return {}
    if isinstance(data, dict):
        data = [data]
    out: dict[int, str] = {}
    for item in data:
        try:
            pid = int(item.get("ProcessId"))
        except (TypeError, ValueError):
            continue
        out[pid] = str(item.get("CommandLine") or "")
    return out


def cleanup_stale_profiles(prefix: str = "gad-chrome-", *, min_age_sec: float = 0.0) -> int:
    """删掉**确认没人在用**的遗留 profile 目录，返回删掉的数量。

    ## 为什么需要它

    Job Object 保证了「父进程一死，Chrome 跟着死」，但**目录不会自己消失**：
    `shutil.rmtree` 写在 `stop()` 里，而强杀路径下 `stop()` 一行都跑不到。
    实测每次强杀都在 `%TEMP%` 留一个 `gad-chrome-xxxxxxxx`，里面是几十 MB
    的 profile（Cache / Code Cache / GPUCache）。跑几十次测试就是几个 GB。

    ## 判据：**命令行**，不是目录存在性

    「目录存在」不等于「有人在用」—— 这正是要删的那些。
    真正的判据是：有没有活着的进程，命令行里带 `--user-data-dir=<这个目录>`。

    **拿不到进程列表时一律不删**。这个函数删的是磁盘上的真实目录，
    宁可留垃圾也不能误删正在跑的实例的 profile —— 那会让 Chrome 当场崩。

    ## 为什么还要 min_age_sec

    调用方通常在启动时跑它。此时可能有**另一个引擎实例**刚建好目录、
    还没来得及 spawn Chrome，进程表里看不到对应命令行。默认 0 是因为
    正常流程下目录由 `stop()` 删，能在启动时看到的都是旧孤儿；但测试脚本
    会传一个正数，避开「刚建好还没起进程」的窗口。
    """
    import re
    import shutil
    import tempfile
    import time
    from pathlib import Path

    root = Path(tempfile.gettempdir())
    if not root.is_dir():
        return 0
    # **必须精确匹配 profile 目录的形状**：`mkdtemp(prefix="gad-chrome-")`
    # 产出的是 `gad-chrome-` + 8 位 `[a-z0-9_]`。若用 `gad-chrome-*` 通配，
    # 会连测试脚本给引擎的 `gad-chrome-engine-xxxx` / `gad-chrome-orphan-xxxx`
    # 一起匹配上 —— 那是引擎的 LOCALAPPDATA，删了会当场把正在跑的测试搞坏。
    pattern = re.compile(rf"^{re.escape(prefix)}[a-z0-9_]{{8}}$")
    cands = [p for p in root.iterdir() if p.is_dir() and pattern.match(p.name)]
    if not cands:
        return 0

    now = time.time()
    live: dict[int, str] = _process_command_lines()
    if not live:
        # 查不到进程表 -> 无法证明没人用 -> 一个都不删。
        return 0

    # 用规范化后的小写路径比对：Windows 路径大小写不敏感，且
    # `%TEMP%` 可能是 8.3 短名（`ADMIN~1`），子串匹配比全等更稳。
    in_use: list[str] = []
    for cmd in live.values():
        if not cmd:
            continue
        low = cmd.lower()
        marker = "--user-data-dir="
        idx = low.find(marker)
        if idx < 0:
            continue
        in_use.append(low[idx + len(marker):].split('"')[0].strip())

    removed = 0
    for p in cands:
        try:
            if min_age_sec and now - p.stat().st_mtime < min_age_sec:
                continue
            target = str(p).lower()
            if any(target in u or u in target for u in in_use if u):
                continue
            shutil.rmtree(p, ignore_errors=True)
            if not p.exists():
                removed += 1
        except OSError:
            continue
    return removed
