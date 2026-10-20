"""T4 —— 真实 Chrome 内核传输层（headful，窗口可移出屏幕）。

## 为什么需要这一档

`deepseek_rpc` 用 `curl_cffi impersonate="chrome150"` 伪造 TLS 指纹。
2026-10-18 的实测（`docs/evidence/20261018-指纹比对/`）证明这条路**有硬上限**：

    真实 Chrome 155 : JA4 t13d1517h2_8daaf6152771_cb7bf5808d99（19 个扩展）
    我们 chrome150  : JA4 t13d1516h2_8daaf6152771_806a8c22fdea（18 个扩展）

差别不是「版本落后」这么简单 —— `chrome150` 这份指纹**是在 macOS Tahoe 上
采集的**（`curl_cffi.fingerprints` 元数据可证）。我们的 UA 声称 Windows，
TLS 握手却是 macOS 的扩展排列。**跨平台拼接**比版本落后更可疑。

另外两条 curl_cffi 无法修复的：
  * `priority: u=1, i` —— libcurl 把它当保留字**静默丢弃**（设了也发不出去）
  * `sec-fetch-user: ?1` —— 指纹默认头补的，真实 XHR 没有（**多发**）

所以这一档不再伪造：直接起本机真 Chrome，让它自己发请求。
上面两条**自动变成正确值** —— 因为浏览器本来就是那样发的。

## 三个实测结论（决定了本文件的设计）

1. **`--headless=new` 不可用** —— UA 里带 `HeadlessChrome/155.0.0.0`，
   公开、易检测、且消除它就得引入新的伪造。
2. **窗口位置不影响指纹** —— `--window-position=-32000,-32000` 的 JA4 /
   扩展数与正常 headful 逐项一致。窗口位置是纯 UI 属性，不参与 TLS 构造。
   ⇒ **可以「headful 但用户看不见」**。
3. **extension 数会随连接状态变化**（19 → 20，ALPS 只在会话复用时出现）。
   真实网页版的请求序列本来就是「先导航建连、再 XHR 复用」，
   所以 `t13d1517 → t13d1518` 的转变**本身就是正常用户的形状**。

## 登录态

不需要重新登录。实测：把现有 `engine/.token` 写进页面
`localStorage['userToken']` 后刷新，页面自动登录（显示
"Where would you like to begin?"）。token 是 DeepSeek 自研格式（64 字符，
非 JWT），Bearer 头与 localStorage 两处都能用。

## 与 `deepseek_rpc` 的分工

**协议逻辑一行不重复**：challenge 结构、PoW 求解、SSE 解析全部复用
（`_Acc` / `_SseParser` / `pow.solve`）。本文件只换「怎么把字节发出去、
怎么把字节收回来」—— 从 `curl_cffi` 换成「页面内 fetch + CDP binding」。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from ..pow import Challenge, solve
from ..session import SessionMeta
from .base import (
    DeltaCallback,
    RemoteStatus,
    ThoughtCallback,
    Transport,
    TransportError,
    TurnOutcome,
)
from .cdp import (
    Cdp,
    CdpError,
    _KillOnCloseJob,
    cleanup_stale_profiles,
    find_chrome,
    free_port,
    kill_tree,
    wait_page_target,
)
from .deepseek_rpc import (
    ACCOUNT_INFO_PATH,
    ALLOW_LIVE_ENV,
    BASE,
    CHALLENGE_PATH,
    CLIENT_VERSION,
    COMPLETION_PATH,
    HISTORY_PATH,
    SESSION_CREATE_PATH,
    SESSION_DELETE_PATH,
    SESSION_LIST_MAX_COUNT,
    SESSION_LIST_MIN_COUNT,
    SESSION_LIST_PATH,
    TTS_TICKET_PATH,
    TTS_VOICE_PATH,
    TTS_VOICES_PATH,
    UPLOAD_PATH,
    _Acc,
    _i18n_pick,
    _load_device_id,
    _SseParser,
    _tz_offset_seconds,
    describe_biz_failure,
)

#: 页面内推流用的 binding 名。带前缀避免和页面自身的全局函数撞名。
BINDING = "__gadCdpPush"

#: 流式读取的**静默超时**（秒）。不是总时长 —— 长回答可以跑几分钟，
#: 判据是「多久没有新字节」。600 秒是保守值：实测最长的思考轮次 ~90 秒
#: 首字延迟，留足余量。
STREAM_IDLE_TIMEOUT = 600.0


def _js_json(obj: Any) -> str:
    """把 Python 值序列化成可安全嵌进 JS 的字面量。

    **不能直接 `json.dumps` 后拼进字符串模板** —— 反引号、`${`、
    以及 U+2028/U+2029 在 JS 字符串里是特殊或非法的。`json.dumps` 的输出
    本身是合法 JS 字面量，但若外层用反引号包住就会被破坏。
    所以调用方一律用 `JSON.parse(<这里的结果>)` 而不是反引号拼接。
    """
    return json.dumps(obj, ensure_ascii=False)


class ChromeCdpTransport(Transport):
    """真实 Chrome 内核 + CDP 驱动的传输层。"""

    name = "chrome-cdp"
    supports_streaming = True

    def __init__(
        self,
        token_provider: Callable[[], str] | None = None,
        device_id_path: Path | None = None,
        model: str = "default",
        search_enabled: bool = False,
        chrome: str | None = None,
        profile_dir: str | None = None,
        offscreen: bool = True,
        headless: bool = False,
        workers: int | None = None,
        idle_close_sec: float = 900.0,
    ) -> None:
        self._token_provider = token_provider
        self._device_path = device_id_path
        self._model = model or "default"
        self._search_enabled = search_enabled
        self._chrome = chrome
        self._profile_dir = profile_dir
        self._offscreen = offscreen
        #: **默认 False，且不建议打开** —— headless 的 UA 带 `HeadlessChrome`，
        #: 那是一个公开可检测的破绽（见模块 docstring 结论 1）。
        #: 保留这个参数只为本地调试（不想让窗口出现在屏幕上时）。
        self._headless = headless
        self._workers = workers
        self._idle_close_sec = idle_close_sec
        #: PoW 预取缓存。`None` = 未启用（首次 `start()` 后才建）。
        self._pow_cache = None
        self._idle_task: asyncio.Task | None = None
        self._idle_closed = False

        self._proc: subprocess.Popen | None = None
        #: 「父死子亡」的兜底 job。`stop()` 走的是 `taskkill /T`，但引擎被
        #: 强杀 / 崩溃时那一行根本没机会跑 —— 见 `cdp._KillOnCloseJob`。
        self._job: _KillOnCloseJob | None = None
        #: job 是否真的建起来并绑上了。**必须能观测**：这层兜底失败是
        #: 静默的（`create()` 失败就 `self._job = None`），而它唯一生效的
        #: 场景是「父进程被强杀」—— 那种场景下没人能回头读日志。
        #: 所以要在**还活着的时候**就能从 `status` 里看到它成没成。
        self._job_bound = False
        self._cdp: Cdp | None = None
        self._port = 0
        self._tmp_profile: str | None = None
        self._device_id = ""
        self._last_used = 0.0
        self._account_suspended = False
        self._suspended_detail = ""
        self._stream_seq = 0

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        token = self._read_token()
        if not token:
            raise TransportError(
                "浏览器档位需要一份可注入的 token（engine/.token）。"
                "请先在设置里登录或粘贴 token —— 本档位不负责首次登录流程。",
                auth=True,
            )
        await self._launch()
        try:
            await self._prepare_page(token)
        except Exception:
            # 起得来但用不了（例如 token 失效）—— 别留一个孤儿 Chrome 占着 profile
            await self.stop()
            raise
        self._start_pow_prefetch()
        self._last_used = time.time()
        self._idle_closed = False
        self._start_idle_watchdog()

    async def stop(self) -> None:
        if self._idle_task is not None:
            self._idle_task.cancel()
            with contextlib.suppress((asyncio.CancelledError, Exception)):
                await self._idle_task
            self._idle_task = None
        if self._pow_cache is not None:
            await self._pow_cache.stop_async()
            self._pow_cache = None
        if self._cdp is not None:
            await self._cdp.close()
            self._cdp = None
        if self._proc is not None:
            kill_tree(self._proc)
            self._proc = None
        if self._tmp_profile:
            import shutil

            shutil.rmtree(self._tmp_profile, ignore_errors=True)
            self._tmp_profile = None

    async def health(self) -> RemoteStatus:
        st = RemoteStatus(reachable=False, model=self._model)
        if self._cdp is None:
            st.note = "浏览器未启动"
            return st
        try:
            body = await self._fetch_json("GET", ACCOUNT_INFO_PATH)
        except (TransportError, CdpError) as exc:
            st.note = f"账号查询失败：{exc}"
            return st
        if body.get("code") not in (0, None):
            st.note = f"账号查询业务错误：{json.dumps(body)[:200]}"
            return st
        biz = (body.get("data") or {}).get("biz_data") or {}
        st.reachable = True
        st.account_status = 0
        st.account_status_short = "正常"
        email = str(biz.get("email") or biz.get("mobile") or "")
        st.account_status_text = f"已登录 {email}" if email else "已登录"
        if biz.get("is_muted"):
            st.account_status_short = "账号被禁言"
            st.account_status_text = f"禁言至 {biz.get('mute_until')}"
        st.note = "浏览器档位 · 真实 Chrome 内核"
        return st

    def background_state(self) -> dict:
        return {
            "transport": self.name,
            "chrome_pid": getattr(self._proc, "pid", None),
            "debug_port": self._port,
            "headless": self._headless,
            "offscreen": self._offscreen,
            # 兜底回收是否真的挂上了。false 时引擎被强杀会留孤儿 ——
            # 这是运行期就能看见的告警，不该等到出事才发现。
            "job_bound": self._job_bound,
            "idle_sec": round(time.time() - self._last_used, 1) if self._last_used else None,
            "idle_close_sec": self._idle_close_sec,
            "idle_closed": self._idle_closed,
            "pow_cache": self._pow_cache.stats() if self._pow_cache is not None else None,
        }

    async def set_model(self, name: str) -> None:
        self._model = (name or "default").strip() or "default"

    # ---------- 启动与页面准备 ----------

    async def _launch(self) -> None:
        exe = self._chrome or find_chrome()
        if not exe:
            raise TransportError(
                "没找到 Chrome。请安装 Google Chrome，或在配置里指定 chrome 路径。",
                config=True,
            )
        if self._profile_dir:
            profile = self._profile_dir
            Path(profile).mkdir(parents=True, exist_ok=True)
        else:
            # 独立 profile：**不能复用用户日常 profile** ——
            # 那会把引擎的行为混进用户自己的浏览历史，且 Chrome 不允许
            # 同一 profile 被两个实例同时打开（会直接把我们的实例踢掉）。
            # 先把上一轮被强杀留下的孤儿目录扫掉。Job Object 只保证
            # 进程死，`rmtree` 在 stop() 里 —— 强杀时那行跑不到，
            # 于是每崩一次就在 %TEMP% 留几十 MB。
            # 60 秒保护期避开「另一个实例刚建好目录、Chrome 还没 spawn」的窗口。
            with contextlib.suppress(Exception):
                cleanup_stale_profiles(min_age_sec=60.0)
            self._tmp_profile = tempfile.mkdtemp(prefix="gad-chrome-")
            profile = self._tmp_profile

        self._port = free_port()
        args = [
            exe,
            f"--remote-debugging-port={self._port}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        if self._headless:
            args.insert(1, "--headless=new")
        elif self._offscreen:
            # 实测：窗口位置是纯 UI 属性，不参与 TLS 构造，
            # 所以移到屏幕外拿到的指纹与正常 headful 逐项一致。
            args += ["--window-position=-32000,-32000", "--window-size=1280,900"]

        self._proc = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        # 绑进「父死子亡」job。**必须在 Chrome 继续 fork 之前尽快 assign** ——
        # 实测从 Popen 返回到 assign 之间约几十毫秒，此时 Chrome 通常只起了
        # 主进程；已经 fork 出来的子进程会随主进程一起被 taskkill /T 或
        # job 的 TerminateJobObject 带走（job 是进程级继承：子进程在
        # assign 之后 fork 的自动进 job）。
        #
        # 失败**不阻断**：这层是兜底，正常路径的 `kill_tree()` 仍在。
        self._job = _KillOnCloseJob()
        if self._job.create():
            self._job_bound = self._job.assign(self._proc)
        else:
            self._job = None
        # 绑不上不阻断启动，但要留一条痕 —— 否则「兜底没生效」这件事
        # 会一直藏着，直到某天父进程被强杀、Chrome 成了孤儿才发现。
        if not self._job_bound:
            import logging

            logging.getLogger(__name__).warning(
                "Chrome 未能绑进父死子亡 job（pid=%s）—— 引擎被强杀时 Chrome 可能成为孤儿",
                self._proc.pid,
            )
        target = await wait_page_target(self._port)
        if target is None:
            raise TransportError(
                f"Chrome 调试端口 {self._port} 未就绪（进程 pid={self._proc.pid}）",
                fatal=True,
            )
        self._cdp = await Cdp.connect(target["webSocketDebuggerUrl"])
        await self._cdp.call("Page.enable")
        await self._cdp.call("Runtime.enable")
        if self._offscreen and not self._headless:
            await self._hide_windows()
        # 设备 id 的落点必须与 RPC 档位一致 —— 同一个账号在两档之间来回切换时，
        # 若 device-id 变了，服务端会看到「同一个 token、两台设备」，
        # 那是比指纹差异更容易命中的异常。
        self._device_id = _load_device_id(
            self._device_path
            or (Path(__file__).resolve().parents[2] / ".device-id")
        )

    def _start_idle_watchdog(self) -> None:
        """空闲超时后关掉 Chrome。

        **为什么必须有**：Chrome 是常驻进程 —— 一个渲染进程 + GPU 进程 +
        网络服务，空转也要 150–300 MB 内存和几个百分点的 CPU。用户一天
        可能只发十几条消息，中间隔着几小时。没有这条 watchdog，引擎一开
        就是整天挂着十几个 Chrome 进程。

        关掉之后 `_proc` / `_cdp` 都是 `None`，下次 `send()` 会走
        「transport 未 start()」的 fatal 错误 —— 所以**必须能被自动重建**。
        这里不自动重启（重启要 6–7 秒，用户不知道发生了什么），而是把
        `_proc` 留着不清，让上层能看到「已空闲关闭」这个状态再决定。
        """
        if self._idle_close_sec <= 0:
            return

        async def _watch() -> None:
            try:
                while True:
                    await asyncio.sleep(min(60.0, self._idle_close_sec / 4))
                    idle = time.time() - self._last_used
                    if idle >= self._idle_close_sec:
                        # 先停预取再关窗口：预取线程还活着的话，
                        # 它会往一个已关闭的 CDP 上发请求。
                        if self._pow_cache is not None:
                            await self._pow_cache.stop_async()
                            self._pow_cache = None
                        if self._cdp is not None:
                            await self._cdp.close()
                            self._cdp = None
                        if self._proc is not None:
                            kill_tree(self._proc)
                            self._proc = None
                        self._idle_closed = True
                        return
            except asyncio.CancelledError:
                raise

        self._idle_task = asyncio.create_task(_watch())

    async def _hide_windows(self) -> None:
        """把本实例的 Chrome 窗口真正隐藏（SW_HIDE），消掉任务栏图标。

        ## 为什么 `--window-position=-32000,-32000` 还不够

        那个参数只把窗口搬到屏幕外，`IsWindowVisible()` 仍为真 ——
        **任务栏上照样有一个 Chrome 图标**，用户点一下就会看见全屏的
        DeepSeek 页面。对一个「后台跑着的引擎」来说这是明显的穿帮。

        ## 为什么 SW_HIDE 是安全的（2026-10-18 实测）

        担心的风险是：窗口隐藏后 `document.visibilityState` 变 `hidden`，
        Chrome 对隐藏页面的定时器节流会拖慢 `fetch` 流式。

        实测结论：**hidden 不影响流式**。同一实例下对比：
          * 屏幕外（hidden）: 39 帧 / 跨度 109–144 ms
          * 屏幕内（visible）: 39 帧 / 跨度 138 ms
          * SW_HIDE 后（hidden）: 39 帧 / 跨度 144 ms，总耗时 1624 ms
        三者无实质差异 —— 被节流的是定时器，不是网络 IO 与 CDP binding。

        ## 为什么只隐藏本实例的窗口

        `EnumWindows` 会枚举全系统的窗口。若不加 `GetWindowThreadProcessId`
        过滤，就会把用户自己开着的 Chrome 一起隐藏掉 —— 那是在动别人的东西。
        """
        if self._proc is None:
            return
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            pid = self._proc.pid
            hwnds: list[int] = []

            @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            def _cb(hwnd, _lparam):
                owner = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
                if owner.value == pid and user32.IsWindowVisible(hwnd):
                    hwnds.append(hwnd)
                return True

            user32.EnumWindows(_cb, 0)
            for hwnd in hwnds:
                user32.ShowWindow(hwnd, 0)  # SW_HIDE
        except Exception:  # noqa: BLE001
            # 隐藏失败不影响功能（窗口只是在屏幕外）—— 不因为「不够隐蔽」
            # 就把整个传输层判死。
            pass

    async def _prepare_page(self, token: str) -> None:
        """导航 → 注入 token → 刷新 → 确认已登录。"""
        assert self._cdp is not None
        await self._cdp.navigate(BASE + "/")
        await self._cdp.wait_js("document.body && document.body.innerText.length > 0", 30)

        # 注入登录态。`userToken` 的形态是 `{"value": "...", "__version": "0"}`，
        # 实测（2026-10-18）这样写进去后刷新即自动登录。
        await self._cdp.eval(
            "localStorage.setItem('userToken', JSON.stringify("
            f"{{value: {_js_json(token)}, __version: '0'}})); true"
        )
        await self._cdp.call("Page.reload")
        if not await self._cdp.wait_js(
            "document.readyState==='complete'", 60
        ):
            raise TransportError("页面刷新后未加载完成", fatal=True)

        # 等前端把登录态落进内存（实测约 1–3 秒；AWS WAF 脚本先跑完）
        ok = await self._cdp.wait_js(
            "!!document.querySelector('textarea, [contenteditable=\"true\"]')", 25
        )
        body = await self._fetch_json("GET", ACCOUNT_INFO_PATH)
        if body.get("code") not in (0, None):
            raise TransportError(
                f"token 注入后仍未登录：{json.dumps(body)[:200]}。"
                "请重新登录或更新 engine/.token。",
                auth=True,
            )
        biz = (body.get("data") or {}).get("biz_data") or {}
        if biz.get("is_muted"):
            self._account_suspended = True
            self._suspended_detail = f"账号被禁言（mute_until={biz.get('mute_until')}）"
        if not ok:
            # 输入框没找到不等于失败（前端改版过 DOM），只记不抛 ——
            # 真正的判据是上面的账号接口。
            self._last_used = time.time()

    # ---------- 页面内请求 ----------

    def _read_token(self) -> str:
        if self._token_provider is None:
            return ""
        r = self._token_provider()
        return (r or "").strip()

    def _client_headers(self) -> dict:
        """真实网页版会带、而浏览器不会自动生成的业务头。

        **刻意不含** `user-agent` / `sec-ch-ua` / `priority` / `sec-fetch-*` /
        `referer` / `origin` / `accept-language` —— 那些浏览器自己会生成，
        而且它们本来就是我们换内核要拿的东西。手写一份反而会覆盖成错的。
        """
        return {
            "authorization": f"Bearer {self._read_token()}",
            "x-client-bundle-id": "com.deepseek.chat",
            "x-client-locale": "en_US",
            "x-client-platform": "web",
            "x-client-version": CLIENT_VERSION,
            "x-client-timezone-offset": _tz_offset_seconds(),
            "x-device-id": self._device_id,
            "x-device-model": "",
        }

    # ---------- PoW 预取 ----------

    def _start_pow_prefetch(self) -> None:
        """起后台预取。

        **为什么浏览器档位更需要它**：RPC 档位同步解 PoW 是 5–8 秒，而
        浏览器档位实测 **7.4 秒**（`mode=multi`, 8 workers）—— 首字节延迟
        几乎全被它吃掉。预取命中时这段归零。

        **取 challenge 必须走页面内 fetch**（`_fetch_json`），不能另起一个
        curl_cffi session —— 那样取 challenge 的 TLS 指纹就和我们声称的
        「真实浏览器」不一致了，等于在第一步就露馅。
        """
        from ..pow_cache import PowCache

        # 容量 3、初始延迟 3 秒。
        #
        # 实测（2026-10-18）：`capacity=2, initial_delay=6` 时，启动后 16 秒
        # 只攒到 1 个 —— 后台是**串行**补货，一次求解 6 秒，两次就要 12 秒
        # 还要加上取 challenge 的时间。真实网页版一次并发取 3 个，所以容量
        # 对齐到 3；延迟缩到 3 秒让预热更早开始（Chrome 启动本身已花掉
        # 6–7 秒，那时用户还没开始打字）。
        cache = PowCache(capacity=3, initial_delay_sec=3.0)
        cache.start_async(self._prefetch_one)
        self._pow_cache = cache

    async def _prefetch_one(self):
        """后台预取一个 challenge 并解好（供 `PowCache` 的异步驱动调用）。"""
        body = await self._fetch_json(
            "POST", CHALLENGE_PATH, {"target_path": COMPLETION_PATH}
        )
        biz = (body.get("data") or {}).get("biz_data") or {}
        ch = Challenge.from_api(biz["challenge"])

        # 求解是纯 CPU（多进程），**必须放线程** —— 否则整个事件循环停摆，
        # CDP 读循环跟着停，连正在进行的流式回答都会卡住。
        solved = await asyncio.to_thread(
            solve, ch, COMPLETION_PATH, workers=self._workers
        )

        # `expire_at` 实测是毫秒时间戳（13 位）；留一道秒级兼容。
        expire_ts = float(ch.expire_at)
        if expire_ts > 1e11:
            expire_ts /= 1000.0
        return expire_ts, solved

    async def _fetch_json(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        *,
        params: dict | None = None,
        referer: str | None = None,
    ) -> dict:
        """在页面里发一个**短请求**并拿 JSON 结果。

        只用于 challenge / 建会话 / 账号查询 / 会话列表这类立刻返回的接口。
        completion 走流式，见 `_stream_completion`。

        `params` 拼进 query string，`referer` 覆盖默认的站点根 ——
        这两项是 2026-10-20 补会话列表时加的：真实网页版的
        `chat_session/fetch_page` 与 `history_messages` 都带 query，
        且 `history_messages` 的 referer 指向具体会话 URL。
        **为什么不直接用 URLSearchParams 手拼 path**：拼在 Python 侧
        才能让日志与报错里出现的是真实请求地址，而不是一段 JS 表达式。
        """
        assert self._cdp is not None
        headers = self._client_headers()
        if payload is not None:
            headers["content-type"] = "application/json"
        if referer:
            headers["referer"] = referer

        url = path
        if params:
            url = f"{path}?{urlencode(params)}"

        js = f"""(async () => {{
            try {{
                const r = await fetch({_js_json(url)}, {{
                    method: {_js_json(method)},
                    headers: JSON.parse({_js_json(json.dumps(headers))}),
                    credentials: 'include',
                    body: {_js_json(json.dumps(payload, ensure_ascii=False)) if payload is not None else 'null'}
                }});
                const t = await r.text();
                return JSON.stringify({{status: r.status, body: t}});
            }} catch (e) {{
                return JSON.stringify({{error: String(e)}});
            }}
        }})()"""
        raw = await self._cdp.eval(js, await_promise=True, timeout=120)
        res = json.loads(raw)
        if res.get("error"):
            raise TransportError(f"{url} 请求失败：{res['error']}")
        status = int(res.get("status") or 0)
        text = res.get("body") or ""
        if status in (401, 403):
            raise TransportError(f"{url} 未认证：HTTP {status} {text[:200]}", auth=True)
        if status != 200:
            raise TransportError(f"{url} 失败：HTTP {status} {text[:200]}")
        try:
            body = json.loads(text)
        except ValueError as exc:
            raise TransportError(f"{url} 返回非 JSON：{text[:200]}") from exc
        if body.get("code") not in (0, None):
            raise TransportError(f"{url} 业务错误：{json.dumps(body)[:300]}")
        d = body.get("data")
        if isinstance(d, dict) and d.get("biz_code"):
            suspended, detail = describe_biz_failure(d)
            if suspended:
                self._account_suspended = True
                self._suspended_detail = detail
            raise TransportError(
                f"{url} 业务失败 biz_code={d['biz_code']}：{detail}",
                auth=suspended,
                suspended=suspended,
            )
        return body

    async def _upload_file(
        self, path: str, *, thinking: bool = False, model: str = "default"
    ) -> dict:
        """上传一个附件，返回 `biz_data`（含 `id`）。

        ## 与 RPC 档位的三点差异

        1. **body 用页面内的 `FormData` 构造**，不自己拼 multipart。浏览器
           生成的 boundary 与 `content-type` 与真实网页版逐字节一致；手拼
           反而可能差一个空格或引号。RPC 档位踩过的坑（`curl_cffi` 的
           `files=` 不可用、必须删 `content-type`）在这里**不存在** ——
           因为 `fetch` 拿到 `FormData` 时会自己设头，我们连碰都不用碰。
        2. **文件内容经 base64 传入页面**。CDP 传不了二进制，只能传字符串。
           这会把内存占用放大 4/3，但附件本来就是「Text extraction only」，
           服务端只提取文本 —— 大文件没有意义，所以可以接受。
        3. **上传有自己的 PoW**（`target_path=UPLOAD_PATH`），与发消息那份
           不能互换，也**不进预取缓存**：上传频率远低于发送，为它维护
           第二个缓存池不值得。
        """
        assert self._cdp is not None
        p = Path(path)
        if not p.is_file():
            raise TransportError(f"附件不存在或不是文件：{path}", config=True)
        data = p.read_bytes()
        if not data:
            raise TransportError(f"附件是空文件，没有内容可提取：{path}", config=True)

        # ① 上传专用 PoW
        body = await self._fetch_json(
            "POST", CHALLENGE_PATH, {"target_path": UPLOAD_PATH}
        )
        biz = (body.get("data") or {}).get("biz_data") or {}
        try:
            ch = Challenge.from_api(biz["challenge"])
        except (KeyError, ValueError) as exc:
            raise TransportError(f"上传 challenge 结构异常：{exc}", fatal=True) from exc

        if self._pow_cache is not None:
            self._pow_cache.pause_async()
        try:
            solved = await asyncio.to_thread(
                solve, ch, UPLOAD_PATH, workers=self._workers
            )
        finally:
            if self._pow_cache is not None:
                self._pow_cache.resume_async()

        pow_header = base64.b64encode(
            json.dumps(
                {k: v for k, v in solved.items() if not k.startswith("_")},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).decode("ascii")

        headers = self._client_headers()
        headers["x-ds-pow-response"] = pow_header
        headers["x-thinking-enabled"] = "1" if thinking else "0"
        headers["x-model-type"] = model
        headers["x-file-size"] = str(len(data))
        # **不要设 content-type** —— FormData 会让浏览器自己写带 boundary 的那份。

        # ② 页面内构造 FormData 并 POST。
        #    base64 走 JSON.parse 传入，避免反引号 / `${` 破坏模板字符串。
        b64 = base64.b64encode(data).decode("ascii")
        js = f"""(async () => {{
            try {{
                const bin = atob({_js_json(b64)});
                const buf = new Uint8Array(bin.length);
                for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
                const fd = new FormData();
                // 字段名是 `file`（不是 files / upload），filename 必须给，
                // 否则服务端拿不到扩展名、文本提取会按未知类型处理。
                fd.append('file', new Blob([buf]), {_js_json(p.name)});
                const r = await fetch({_js_json(UPLOAD_PATH)}, {{
                    method: 'POST',
                    headers: JSON.parse({_js_json(json.dumps(headers))}),
                    credentials: 'include',
                    body: fd
                }});
                const t = await r.text();
                return JSON.stringify({{status: r.status, body: t}});
            }} catch (e) {{
                return JSON.stringify({{error: String(e)}});
            }}
        }})()"""
        raw = await self._cdp.eval(js, await_promise=True, timeout=180)
        res = json.loads(raw)
        if res.get("error"):
            raise TransportError(f"上传请求失败：{res['error']}")
        status = int(res.get("status") or 0)
        text = res.get("body") or ""
        if status in (401, 403):
            raise TransportError(f"上传未认证：HTTP {status} {text[:200]}", auth=True)
        if status != 200:
            raise TransportError(f"上传失败：HTTP {status} {text[:200]}")
        try:
            rb = json.loads(text)
        except ValueError as exc:
            raise TransportError(f"上传返回非 JSON：{text[:200]}", config=True) from exc
        if rb.get("code") not in (0, None):
            raise TransportError(f"上传业务错误：{json.dumps(rb)[:300]}")

        ub = (rb.get("data") or {}).get("biz_data") or {}
        if not ub.get("id"):
            raise TransportError(
                f"上传响应缺 id（结构可能已变）：{json.dumps(rb)[:300]}", config=True
            )
        return ub

    # ---------- 只读：服务端会话 ----------
    #
    # 2026-10-20 补。此前本档位**只实现了发送链路**，`list_sessions` /
    # `fetch_history` 落到 `base.py` 的默认实现上——那两个默认实现是
    # 「抛错而不是 pass」，于是切到 chrome 档位后侧栏**永远空着**，
    # 对话流里挂一条「传输档位 'chrome-cdp' 不支持列出会话」。
    #
    # 这正是那条设计哲学的代价面：它把静默失效变成了一声明响，
    # 但响完之后得有人去补实现。补的方式是**复用 T3 的路径常量与边界规则**
    # （`SESSION_LIST_MIN/MAX_COUNT` 的实测结论、响应结构），
    # 只把「谁发这个请求」从 curl_cffi 换成页面内 fetch。
    #
    # ⚠️ 为什么不干脆继承 T3 的同步实现：那些实现走 `_require_session()`，
    # 拿的是 curl_cffi 的 session 对象。浏览器档位下那个对象不存在，
    # 也不该存在——用 curl_cffi 发列表请求会让 TLS 指纹在第一步就退回伪造。

    async def list_sessions(self, count: int = 30) -> list[dict]:
        """列出账号下的会话（只读）。

        服务端的两条硬约束照旧生效（2026-10-18 实测 T-20）：
        `count > 100` **静默返回空列表**而不是报错，`count = 1` 也返回空。
        所以这里与 T3 一样**主动夹住边界并显式抛错**，绝不把超限请求发出去 ——
        「功能坏了」被读成「账号下没有会话」是本项目最忌讳的失败形态。
        """
        if count < SESSION_LIST_MIN_COUNT or count > SESSION_LIST_MAX_COUNT:
            raise TransportError(
                f"会话列表条数必须在 {SESSION_LIST_MIN_COUNT}–"
                f"{SESSION_LIST_MAX_COUNT} 之间（收到 {count}）。"
                "超出上限时服务端会**静默返回空列表**而不是报错，"
                "所以这里提前拦住，避免「读不到会话」被误判成「没有会话」。",
                config=True,
            )
        if self._cdp is None:
            raise TransportError("transport 未 start()", fatal=True)
        body = await self._fetch_json(
            "GET", SESSION_LIST_PATH, None, params={"count": int(count)}
        )
        biz = (body.get("data") or {}).get("biz_data") or {}
        sessions = biz.get("chat_sessions") or []
        self._last_used = time.time()
        return sessions

    async def fetch_history(self, chat_session_id: str) -> dict:
        """拉某个会话的完整历史（只读）。

        返回 `biz_data`，形如
        `{chat_session: {...}, chat_messages: [...], cache_control, cache_reset_at}`。
        `chat_session.current_message_id` 就是**续接锚点**（等于末条 message_id）。

        `referer` 必须指向该会话的 URL —— 真实网页版就是这么发的，
        缺了它服务端仍可能返回，但那是一个「我们多发少发了一个头」的信号，
        没必要在这个成本上省。
        """
        cid = (chat_session_id or "").strip()
        if not cid:
            raise TransportError("fetch_history 需要 chat_session_id", config=True)
        if self._cdp is None:
            raise TransportError("transport 未 start()", fatal=True)
        body = await self._fetch_json(
            "GET",
            HISTORY_PATH,
            None,
            params={"chat_session_id": cid},
            referer=f"{BASE}/a/chat/s/{cid}",
        )
        self._last_used = time.time()
        return (body.get("data") or {}).get("biz_data") or {}

    # ---------- 写：服务端会话生命周期 ----------

    async def create_session(self) -> str:
        """显式新建一个空会话，返回 id。

        ⚠️ 新建出来的是「空会话」：`title=null`、`title_type="WIP"`，
        **不会出现在 `list_sessions` 的返回里**（列表只列有内容的会话）。
        要确认它真的存在，得用 `fetch_history(sid)`（`biz_code=0` 即存在）。
        """
        if self._cdp is None:
            raise TransportError("transport 未 start()", fatal=True)
        body = await self._fetch_json("POST", SESSION_CREATE_PATH, {})
        biz = (body.get("data") or {}).get("biz_data") or {}
        sid = ((biz.get("chat_session") or {}).get("id")) or biz.get("id") or ""
        if not sid:
            raise TransportError(
                f"create_session 响应里没有 id：{json.dumps(body)[:300]}", fatal=True
            )
        self._last_used = time.time()
        return sid

    async def delete_session(self, chat_session_id: str) -> None:
        """永久删除一个服务端会话。**不可逆**，上层必须先二次确认。

        幂等：服务端对不存在的 id 也回 `biz_code=0`。所以本方法「不抛异常」
        只意味着「服务端接受了这次调用」，不意味着「确实删掉了一条存在的会话」。
        要验证得靠 `fetch_history` 复查。

        空 id 绝不发出去 —— 少数接口把「缺省」解释成「全部」，
        那是「一条命令清空账号」。宁可在这里炸。
        """
        cid = (chat_session_id or "").strip()
        if not cid:
            raise TransportError("delete_session 需要 chat_session_id", config=True)
        if self._cdp is None:
            raise TransportError("transport 未 start()", fatal=True)
        await self._fetch_json(
            "POST",
            SESSION_DELETE_PATH,
            {"chat_session_id": cid},
            referer=f"{BASE}/a/chat/s/{cid}",
        )
        self._last_used = time.time()

    # ---------- 朗读（TTS）控制面 ----------
    #
    # 与 T3 同一条分工：三个方法都**不碰音频流**，音频由前端直连
    # `wss://chat.deepseek.com/api/v0/chat/tts/`，引擎只签票。
    #
    # ⚠️ 这三个会**真的打上游接口**，与 `ALLOW_LIVE_ENV` 那套闸门无关 ——
    #    它们是产品功能，不是探测。调用方必须接受「用户点一次朗读就消耗一次
    #    配额」，不要拿它做轮询或预热。

    async def fetch_tts_ticket(self, chat_session_id: str, message_id: str) -> str:
        """签一张一次性朗读票据。

        两个参数**不参与这个请求**，但这里仍然要求传 —— 票据的可用范围由
        服务端按「这个 scope 能访问哪些资源」决定，调用方必须先自己确认
        「这条消息确实属于当前账号的当前会话」，否则会拿到一张能签但用不了的票
        （连流时回 `code 2 INVALID_INPUT`）。所以它们是**前置条件的显式化**，
        不是冗余。
        """
        cid = (chat_session_id or "").strip()
        mid = (message_id or "").strip()
        if not cid:
            raise TransportError("fetch_tts_ticket 需要 chat_session_id", config=True)
        if not mid:
            raise TransportError("fetch_tts_ticket 需要 message_id", config=True)
        if self._cdp is None:
            raise TransportError("transport 未 start()", fatal=True)
        body = await self._fetch_json(
            "POST",
            TTS_TICKET_PATH,
            {"scope": "tts"},
            referer=f"{BASE}/a/chat/s/{cid}",
        )
        ticket = str(((body.get("data") or {}).get("biz_data") or {}).get("ticket") or "")
        if not ticket:
            # 结构变了（不是空字符串的正常情况）—— 报出来，别让下游拿空票去连流，
            # 那样失败会发生在 WS 握手阶段，错误信息离真正的原因很远。
            raise TransportError(
                f"auth/ticket 没返回 ticket：{json.dumps(body)[:300]}", config=True
            )
        self._last_used = time.time()
        return ticket

    async def list_tts_voices(self) -> list[dict]:
        """列出可用音色。只读，**不消耗朗读配额**。

        两个容易搞错的字段：
          * `is_default` 是**出厂默认**（永远只有 mira 为 true），
            **不能**拿它当选中态 —— 那样切换明明成功了，面板却永远显示 mira。
          * 选中态只能看 `biz_data.current_voice_id`，它在 biz_data **顶层**，
            不在每个 voice 对象里（2026-10-19 实测）。
        """
        if self._cdp is None:
            raise TransportError("transport 未 start()", fatal=True)
        body = await self._fetch_json("GET", TTS_VOICES_PATH)
        biz = (body.get("data") or {}).get("biz_data") or {}
        raw = biz.get("voices") or biz.get("voice_list") or []
        current = str(biz.get("current_voice_id") or "")

        out: list[dict] = []
        for v in raw:
            if not isinstance(v, dict):
                continue
            vid = str(v.get("voice_id") or v.get("id") or "")
            if not vid:
                continue
            out.append(
                {
                    "voice_id": vid,
                    # 名称与描述都是 i18n 字典（`{"zh": "贝壳", "en": "Mira"}`）。
                    # 直接取 `v["name"]` 永远是空 —— 那样界面上显示的是 `mira`
                    # 而不是「贝壳」。
                    "name": _i18n_pick(v.get("name_i18n"), v.get("name"), vid),
                    "desc": _i18n_pick(
                        v.get("description_i18n"),
                        v.get("desc") or v.get("description"),
                        "",
                    ),
                    "gender": str(v.get("gender") or ""),
                    "languages": list(v.get("languages") or []),
                    "is_default": bool(v.get("is_default")),
                    "is_current": bool(current) and vid == current,
                    "demo_urls": dict(v.get("demo_urls") or {}),
                }
            )
        self._last_used = time.time()
        return out

    async def set_tts_voice(self, voice_id: str) -> None:
        """切换朗读音色。**写操作**：改的是账号级偏好，网页版也跟着变。"""
        vid = (voice_id or "").strip()
        if not vid:
            raise TransportError("set_tts_voice 需要 voice_id", config=True)
        if self._cdp is None:
            raise TransportError("transport 未 start()", fatal=True)
        await self._fetch_json("POST", TTS_VOICE_PATH, {"voice_id": vid})
        self._last_used = time.time()

    # ---------- 发送 ----------

    async def send(
        self,
        prompt: str,
        session: SessionMeta,
        *,
        temporary: bool = True,
        extended_thinking: bool = False,
        search_enabled: bool | None = None,
        files: list[str] | None = None,
        on_delta: DeltaCallback | None = None,
        on_thought: ThoughtCallback | None = None,
    ) -> TurnOutcome:
        if self._cdp is None:
            if self._idle_closed:
                # 空闲超时把 Chrome 关了。**自动重建**，而不是把「请重新启动」
                # 抛给用户 —— 用户没做错任何事，只是隔了几小时才发下一条。
                # 重建要 6–7 秒，但总比一条莫名其妙的 fatal 好。
                await self.start()
            else:
                raise TransportError("transport 未 start()", fatal=True)

        # ★ 与 `deepseek_rpc` 共用同一把闸门 —— 换档位不等于绕开风控纪律。
        #   产品外壳会带 `GAD_ALLOW_LIVE_AUTH=1`；测试脚本不该真发。
        if os.environ.get(ALLOW_LIVE_ENV) != "1":
            raise TransportError(
                f"已阻止真实发送（{ALLOW_LIVE_ENV} 未设为 1）。"
                "浏览器档位同样受这条闸门约束 —— 它是「不再触发风控」的底线，"
                "不是某个实现的局部开关。",
            )
        # 1) 会话：新会话必须由服务端创建
        if not session.cid:
            body = await self._fetch_json("POST", "/api/v0/chat_session/create", {})
            biz = (body.get("data") or {}).get("biz_data") or {}
            sid = ((biz.get("chat_session") or {}).get("id")) or biz.get("id") or ""
            if not sid:
                raise TransportError(
                    f"create_session 响应里没有 id：{json.dumps(body)[:300]}", fatal=True
                )
            session.cid = sid
            session.rid = ""
            session.rcid = ""

        # 2) challenge + 3) 解 PoW —— **优先用后台预取的结果**。
        #
        #    与 RPC 档位同一条策略（见 `pow_cache.py` 的 docstring）：真实
        #    网页版提前算好，发送时 0 ms。命中缓存时 `challenge_ms` 与
        #    `pow_ms` 都记 0，用 `pow_cached` 区分 —— 否则「0 秒解完」
        #    在日志里看起来像测量坏了。
        t_pow = time.monotonic()
        cached = self._pow_cache.take() if self._pow_cache is not None else None
        if cached is not None:
            solved = cached
            pow_cached = True
        else:
            # ★ 同步解之前**先暂停后台预取**：否则两个 8 进程池抢同一批核，
            #   实测会把一次 3–6 秒的求解拖到 10 秒以上。发送是用户在等的
            #   那条路，优先级高于「提前准备下一个」。
            if self._pow_cache is not None:
                self._pow_cache.pause_async()
            try:
                body = await self._fetch_json(
                    "POST", CHALLENGE_PATH, {"target_path": COMPLETION_PATH}
                )
                biz = (body.get("data") or {}).get("biz_data") or {}
                try:
                    ch = Challenge.from_api(biz["challenge"])
                except (KeyError, ValueError) as exc:
                    raise TransportError(
                        f"challenge 结构异常：{exc}", fatal=True
                    ) from exc

                # **必须放到线程里**：纯 CPU 密集（多进程，3–8 秒）。若在事件
                # 循环里同步跑，CDP 的读循环会整个停摆，binding 事件全部堆积 ——
                # 表现是「回答发出来了，但界面一个字都收不到」，极难定位。
                solved = await asyncio.to_thread(
                    solve, ch, COMPLETION_PATH, workers=self._workers
                )
            finally:
                if self._pow_cache is not None:
                    self._pow_cache.resume_async()
            pow_cached = False
        pow_ms = (time.monotonic() - t_pow) * 1000.0
        pow_header = base64.b64encode(
            json.dumps(
                {k: v for k, v in solved.items() if not k.startswith("_")},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).decode("ascii")

        # 4) 流式完成
        parent_raw = session.parent_message_id
        if parent_raw is not None and not str(parent_raw).isdigit():
            raise TransportError(
                f"会话续接锚点异常：parent_message_id={parent_raw!r} 不是数字。",
                config=True,
            )
        parent = int(parent_raw) if parent_raw else None
        # 3.5) 附件上传 —— **每个文件一次独立的 PoW + 一次 FormData POST**。
        #
        # 放在 completion 的 PoW **之后**：预取命中时那一步是 0 成本，而上传
        # 是同步解（几秒量级）。若反过来先上传，这几秒可能把预取来的
        # challenge 熬到过期，等于把「0 ms」白扔了。
        #
        # 上传失败**不降级成「不带附件继续发」** —— 用户明确挂了附件，
        # 悄悄丢掉是最坏的结果（模型会答非所问，界面上却看起来一切正常）。
        ref_file_ids: list[str] = []
        uploaded: list[dict] = []
        upload_ms = 0.0
        if files:
            t_upload = time.monotonic()
            for fp in files:
                ub = await self._upload_file(
                    fp,
                    thinking=bool(extended_thinking),
                    model=self._model or "default",
                )
                uploaded.append(ub)
                ref_file_ids.append(str(ub["id"]))
            upload_ms = (time.monotonic() - t_upload) * 1000.0

        payload = {
            "chat_session_id": session.cid,
            "parent_message_id": parent,
            "model_type": None if parent else (self._model or "default"),
            "prompt": prompt,
            "ref_file_ids": ref_file_ids,
            "thinking_enabled": bool(extended_thinking),
            "search_enabled": bool(self._search_enabled if search_enabled is None else search_enabled),
            "action": None,
            "preempt": False,
        }

        outcome = await self._stream_completion(
            payload, pow_header, session, on_delta, on_thought
        )
        outcome.pow_ms = round(pow_ms, 1)
        outcome.pow_mode = str(solved.get("_mode") or "")
        outcome.pow_workers = int(solved.get("_workers") or 0)
        outcome.pow_cached = pow_cached
        outcome.upload_ms = round(upload_ms, 1)
        outcome.uploaded_files = [
            {"path": str(fp), "id": str(u.get("id") or ""),
             "name": str(u.get("file_name") or Path(str(fp)).name),
             "status": u.get("status")}
            # `strict=False` 是刻意的：上传可能少返回一条（服务端只回成功的），
            # 这里按较短的截断，不要因为条数不齐就抛异常把整轮对话打断。
            for fp, u in zip(files or [], uploaded, strict=False)
        ]
        self._last_used = time.time()

        if outcome.rid:
            session.rid = outcome.rid
            session.rcid = outcome.rcid or session.rcid
            session.last_used_at = time.time()
        return outcome

    async def _stream_completion(
        self,
        payload: dict,
        pow_header: str,
        session: SessionMeta,
        on_delta: DeltaCallback | None,
        on_thought: ThoughtCallback | None,
    ) -> TurnOutcome:
        """页面内 fetch + ReadableStream → CDP binding → SSE 解析器。

        ★ **IIFE 必须立即返回**（`await_promise=False`）。若写成
          `await fetch(...)` 再逐块读，`Runtime.evaluate` 会一直挂到流结束
          才返回 —— 那时 `on_delta` 已经失去意义，界面全程空白。
          所以这里让 IIFE 自己跑，数据通过 binding 推回来。
        """
        assert self._cdp is not None
        self._stream_seq += 1
        tag = f"s{self._stream_seq}"

        headers = self._client_headers()
        headers["content-type"] = "application/json"
        headers["x-ds-pow-response"] = pow_header
        # `x-hif-leim` 从页面 localStorage 取（真实前端也是这么来的）。
        # 取不到就不带 —— 带一个过期的值比不带更像伪造（实测它会轮换）。
        leim = await self._cdp.eval(
            "localStorage.getItem('hif_leim_cached') || ''"
        )
        if leim:
            headers["x-hif-leim"] = str(leim)

        # binding 每次导航后会随 executionContext 消失，所以每轮重新注册。
        # 重复注册同名 binding 会报错，忽略即可（幂等语义）。
        with contextlib.suppress(CdpError):
            await self._cdp.add_binding(BINDING)

        q = self._cdp.subscribe("Runtime.bindingCalled")

        js = f"""(async () => {{
            const TAG = {_js_json(tag)};
            const push = (o) => window.{BINDING}(JSON.stringify(Object.assign({{tag: TAG}}, o)));
            try {{
                const r = await fetch({_js_json(COMPLETION_PATH)}, {{
                    method: 'POST',
                    headers: JSON.parse({_js_json(json.dumps(headers))}),
                    credentials: 'include',
                    // ★ body 必须是**字符串**，不能包 JSON.parse。
                    //   包了之后 fetch 拿到的是对象，会被 String() 成
                    //   `[object Object]`，服务端（serde）报
                    //   `[0]: expected value at line 1 column 2` —— 那个
                    //   错误文案完全看不出是「body 类型错了」。
                    body: {_js_json(json.dumps(payload, ensure_ascii=False))}
                }});
                if (!r.ok) {{
                    const t = await r.text();
                    push({{t: 'error', status: r.status, body: t.slice(0, 2000)}});
                    return;
                }}
                const reader = r.body.getReader();
                const dec = new TextDecoder('utf-8');
                while (true) {{
                    const {{done, value}} = await reader.read();
                    if (done) break;
                    // stream:true 必需 —— 否则多字节 UTF-8 被切在块边界会乱码。
                    // 中文回答里这几乎是必然发生的。
                    push({{t: 'chunk', v: dec.decode(value, {{stream: true}})}});
                }}
                push({{t: 'end'}});
            }} catch (e) {{
                push({{t: 'error', body: String(e)}});
            }}
        }})(); true"""
        await self._cdp.eval(js, await_promise=False)

        acc = _Acc()
        #: 解析器是同步的，回调是 async 的 —— 中间隔一个待发队列。
        #: **必须边收边发**：攒到流结束再一次性吐，正是本项目最痛恨的
        #: 「发了没反应」（`base.py` 里 `on_thought` 那段注释讲的就是这个）。
        pending: list[tuple[str, str]] = []

        def emit(kind: str, value: Any) -> None:
            if kind in ("delta", "thought"):
                pending.append((kind, str(value)))

        parser = _SseParser(acc, emit)

        async def flush() -> None:
            """把 emit 攒下的增量按顺序交给调用方。"""
            while pending:
                kind, value = pending.pop(0)
                cb = on_delta if kind == "delta" else on_thought
                if cb is None:
                    continue
                r = cb(value)
                if asyncio.iscoroutine(r):
                    await r

        buf = ""
        try:
            while True:
                ev = await self._cdp.wait_event(q, timeout=STREAM_IDLE_TIMEOUT)
                if ev.get("name") != BINDING:
                    continue
                try:
                    msg = json.loads(ev.get("payload") or "{}")
                except ValueError:
                    continue
                if msg.get("tag") != tag:
                    continue
                kind = msg.get("t")
                if kind == "chunk":
                    buf += msg.get("v") or ""
                    # 按行切；最后一段可能是不完整的行，留在 buf 里等下一块。
                    # **不能用 splitlines()** —— 它把 \r\n 也算成断行，
                    # 而 SSE 里 \r 属于行尾标记，语义上要单独剥掉。
                    stop = False
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        line = line.rstrip("\r")
                        if not parser.feed_line(line):
                            buf = ""
                            stop = True
                            break
                    await flush()
                    if stop:
                        break
                elif kind == "end":
                    if buf:
                        parser.feed_line(buf.rstrip("\r"))
                    await flush()
                    break
                elif kind == "error":
                    raise TransportError(
                        f"completion 请求失败：HTTP {msg.get('status')} "
                        f"{(msg.get('body') or '')[:300]}",
                        auth=(msg.get("status") in (401, 403)),
                    )
        finally:
            self._cdp.unsubscribe("Runtime.bindingCalled")

        text = acc.reply_text()
        outcome = TurnOutcome(
            text=text,
            thoughts=acc.thought_text(),
            cid=session.cid,
            rid=str(acc.response_message_id) if acc.response_message_id else "",
            rcid=str(acc.request_message_id) if acc.request_message_id else "",
            search_pages=acc.search_pages,
            search_sources=len(acc.search_results),
        )
        if not text:
            if acc.error:
                suffix = f"（finish_reason={acc.finish_reason}）" if acc.finish_reason else ""
                outcome.error = f"服务端错误：{acc.error}{suffix}"
            elif acc.status:
                outcome.error = f"未收到正文（status={acc.status}）"
            else:
                outcome.error = "未收到正文（流为空）"
        return outcome
