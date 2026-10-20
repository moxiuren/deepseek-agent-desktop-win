"""PoW 预取缓存 —— 把「发送时同步解 5–8 秒」变成「命中即 0 ms」。

## 为什么要它

2026-10-18 实测（`docs/20261017-防封禁研究分析.md` §2.5）：

- 真实网页版一次**并发取 3 个** `create_pow_challenge`，把结果全部解好缓存；
  第二次发送 **0 个 challenge 请求、0 ms 求解**，`completion` 仍带
  `x-ds-pow-response`。
- 我们每次发送都同步「取一个、解一个」，同难度（`difficulty=144000`）
  要 **5.1–7.7 秒**。

也就是说，我们和真实客户端最刺眼的差距不是算法（那是 90 倍的吞吐差，
换实现也追不平），而是**有没有提前算**。提前算这一条不需要改算法，
就能把绝大多数发送的 PoW 开销降到 0。

## 设计

- 一个后台 daemon 线程，缓存未满时补一个：取 challenge → 求解 → 入队。
- `take()` 原子弹出一个**未过期**的结果；剩余寿命不足 `expire_margin_sec`
  的条目直接丢弃（宁可 miss 去同步解，也不发一个快过期的）。
- 容量默认 2 —— 与网页版「一次取 3 个」同量级，又不至于长时间占着 CPU。
- 串行求解，不并发抢 CPU：缓存满就 sleep，空闲时不占算力。

## 边界与前提

- challenge 的 `target_path` 固定为 `/api/v0/chat/completion`，**不绑定会话**，
  所以换会话、换 referer 都能用（referer 只是 HTTP 头，不参与 challenge 内容）。
- `Challenge.expire_at` 是**毫秒**时间戳（13 位，实测
  `1792254708602`）；`expire_after` 是相对毫秒（实测 `300000` = 5 分钟）。
  两者都给，取 `expire_at` 做绝对判据。
- 取 challenge 由调用方注入（`fetch_solve`），**缓存本身不碰网络**，
  这样它可以在测试里完全离线验证。
- **失败要退避**：取 challenge 失败通常是网络或鉴权问题，连续重试只会放大
  机器特征。失败后至少等 `fail_backoff_sec` 再试。
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from collections.abc import Callable
from typing import Any

__all__ = ["AsyncFetchSolve", "FetchSolve", "PowCache"]

#: `fetch_solve()` 的返回：`(expire_ts, solved)` 或 `None`。
#: `expire_ts` 是 Unix 秒（浮点）；`solved` 是 `pow.solve()` 的返回 dict。
FetchSolve = Callable[[], "tuple[float, dict] | None"]

#: 异步版本，供 `start_async()` 用。
AsyncFetchSolve = Callable[[], "Any"]


class PowCache:
    """后台预取并缓存已解好的 PoW 结果。"""

    def __init__(
        self,
        *,
        capacity: int = 2,
        expire_margin_sec: float = 30.0,
        initial_delay_sec: float = 5.0,
        fail_backoff_sec: float = 15.0,
        poll_sec: float = 1.0,
        max_idle_sec: float = 60.0,
    ) -> None:
        self._capacity = max(0, int(capacity))
        self._margin = float(expire_margin_sec)
        self._initial_delay = max(0.0, float(initial_delay_sec))
        self._fail_backoff = max(0.0, float(fail_backoff_sec))
        self._poll = max(0.05, float(poll_sec))
        self._max_idle = max(self._poll, float(max_idle_sec))

        self._lock = threading.Lock()
        self._items: list[dict] = []
        self._fetch: FetchSolve | None = None
        self._thread: threading.Thread | None = None
        self._afetch: AsyncFetchSolve | None = None
        self._task: asyncio.Task | None = None
        self._paused = threading.Event()
        self._stop = threading.Event()
        #: 连续多少次「刚产出就已过期」或「整批过期被清」。用来判断用户
        #: 是不是根本不在电脑前 —— 见 `_idle_sleep()`。
        self._idle_streak = 0
        self._stats = {
            "produced": 0,
            "taken": 0,
            "expired": 0,
            "failed": 0,
        }

    # ---------- 生命周期 ----------

    def start(self, fetch_solve: FetchSolve) -> None:
        """启动后台预取。`capacity <= 0` 时是空操作（等于关闭）。"""
        if self._capacity <= 0:
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._fetch = fetch_solve
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="pow-prefetch", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        """停线程。**不 `shutdown()` 求解进程池** —— 池归 `pow.py` 管。"""
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout)
        self._thread = None

    # ---------- 异步生命周期 ----------
    #
    # **为什么需要一套异步的**：浏览器档位取 challenge 只能走**页面内 fetch**
    # （`chrome_cdp._fetch_json`）。那是 CDP 调用，必须在事件循环里 await；
    # 而 `start()` 用的是后台线程，线程里没有事件循环。
    #
    # 不能用「线程 + `run_coroutine_threadsafe`」绕过：那要求线程持有主循环
    # 的引用，而 CDP 连接、Chrome 进程生命周期都绑在循环上 —— 循环一停
    # （`stop()` 之后）线程还在 submit，就是往死循环里投任务。
    # 直接用 asyncio.Task 最干净：生命周期与传输层完全一致。

    def start_async(self, fetch_solve: AsyncFetchSolve) -> None:
        """异步版 `start()`。`fetch_solve` 是一个协程函数，返回同上。"""
        if self._capacity <= 0:
            return
        if self._task is not None and not self._task.done():
            return
        self._afetch = fetch_solve
        self._task = asyncio.create_task(self._loop_async())

    async def stop_async(self) -> None:
        t = self._task
        self._task = None
        if t is None:
            return
        t.cancel()
        with contextlib.suppress((asyncio.CancelledError, Exception)):
            await t

    def pause_async(self) -> None:
        """暂停补货（不销毁 task）。

        **为什么需要**：`send()` 缓存未命中时会同步解一次 PoW，那是又一个
        8 进程池。若后台预取此刻也在解，两个池抢同一批 CPU 核 ——
        实测两边都变慢一倍（`verify_chrome_transport.py` 里出现过
        `pow=10075ms`，而单独跑是 3–6 秒）。发送是用户正在等的那条路，
        优先级必须高于「提前准备下一个」。
        """
        self._paused.set()

    def resume_async(self) -> None:
        self._paused.clear()

    # ---------- 取用 ----------

    def take(self) -> dict | None:
        """弹出一个未过期的已解结果；没有则 `None`（调用方去同步解）。

        过期条目在这里**顺手清掉** —— 它们已经没有价值，留着只会让
        `size()` 虚高、让后台线程误以为缓存满了不再补货。

        **调用本身说明用户回来了**，所以顺手把闲置退避归零：下一个补货
        应该立刻做，而不是继续按 5 分钟的节奏等。
        """
        now = time.time()
        with self._lock:
            self._idle_streak = 0
            while self._items:
                item = self._items.pop(0)
                if item["expire_ts"] - self._margin > now:
                    self._stats["taken"] += 1
                    return item["solved"]
                self._stats["expired"] += 1
        return None

    def size(self) -> int:
        with self._lock:
            return len(self._items)

    # ---------- 过期清理 ----------

    def _purge_expired_locked(self) -> int:
        """剔除已死条目，返回剔除数量。**调用方必须已持有 `self._lock`。**

        ## 为什么必须由后台线程自己做

        2026-10-18 实测发现：引擎闲置 20 分钟后第一次发送，PoW 缓存统计是
        `produced=3 taken=0 expired=2 cached=1` —— 预取明明产出了 3 个，
        发送时却**一个都用不上**，用户照旧干等 5–8 秒同步求解。

        根因是「满」的判据只看 `len(self._items)`，而清过期只在 `take()`
        里做。于是两个 5 分钟前就烂掉的条目一直占着容量，后台认为「满了」，
        一直 sleep，直到某次发送把它们弹出来才清理 —— 那时候已经晚了。

        修法就是这一句：**判断容量之前先剔除死条目**。清完如果腾出了位置，
        后台会立刻补一个新鲜的，下次发送才真的命中。
        """
        now = time.time()
        dead = [it for it in self._items if it["expire_ts"] - self._margin <= now]
        if dead:
            self._items = [it for it in self._items if it["expire_ts"] - self._margin > now]
            self._stats["expired"] += len(dead)
        return len(dead)

    def _idle_sleep(self) -> None:
        """按闲置程度退避等待。

        **为什么不能一直每 `poll` 秒重试一次**：challenge 的寿命是 5 分钟，
        而求解一次要 5–8 秒（8 个进程）。如果用户根本不在，每 5 分钟就白烧
        一次求解，一小时 12 次 —— 既是电费也是机器特征（每 5 分钟一次
        `create_pow_challenge`，比真人规律太多）。

        所以连续「刚产出就过期」时指数拉长间隔，封顶 `max_idle_sec`。
        `take()` 会把 streak 归零，用户一回来补货立刻恢复勤快。
        """
        self._idle_streak = min(self._idle_streak + 1, 20)
        delay = min(self._poll * (2 ** self._idle_streak), self._max_idle)
        self._stop.wait(delay)

    def _idle_streak_reset(self) -> None:
        with self._lock:
            self._idle_streak = 0

    async def _idle_sleep_async(self) -> None:
        """`_idle_sleep()` 的协程版。

        **不需要额外处理 stop**：`stop_async()` 直接 `task.cancel()`，
        `await asyncio.sleep()` 会抛 `CancelledError` 立刻返回。
        """
        self._idle_streak = min(self._idle_streak + 1, 20)
        delay = min(self._poll * (2 ** self._idle_streak), self._max_idle)
        await asyncio.sleep(delay)

    def stats(self) -> dict:
        with self._lock:
            return {
                **self._stats,
                "cached": len(self._items),
                "capacity": self._capacity,
            }

    # ---------- 后台 ----------

    def _loop(self) -> None:
        # 启动后先等一会儿再预热：引擎 `start()` 就在事件循环里，
        # 立刻开 8 个进程解 PoW 会把启动拖慢，而启动那几秒用户根本还没发消息。
        if self._initial_delay > 0 and self._stop.wait(self._initial_delay):
            return

        while not self._stop.is_set():
            with self._lock:
                purged = self._purge_expired_locked()
                full = len(self._items) >= self._capacity
            if purged:
                # 刚清掉一批死条目 = 用户很久没动了。先退避再补货，
                # 否则就是「每 5 分钟白烧一次求解」的永动机。
                self._idle_sleep()
                continue
            if full:
                self._stop.wait(self._poll)
                continue

            fetch = self._fetch
            if fetch is None:
                self._stop.wait(self._poll)
                continue

            try:
                got = fetch()
            except Exception:  # noqa: BLE001
                # 失败多半是网络/鉴权；连续重试只会放大机器特征。
                with self._lock:
                    self._stats["failed"] += 1
                self._stop.wait(self._fail_backoff)
                continue

            if not got:
                self._stop.wait(self._poll)
                continue

            expire_ts, solved = got
            # 解完才发现已经过期（求解本身可能就花掉几秒）—— 直接丢掉，
            # 不入队。否则 `take()` 会立刻把它判过期，白占一次缓存位。
            if float(expire_ts) - self._margin <= time.time():
                with self._lock:
                    self._stats["expired"] += 1
                self._idle_sleep()
                continue

            with self._lock:
                self._items.append({"expire_ts": float(expire_ts), "solved": solved})
                self._stats["produced"] += 1
                self._idle_streak = 0

    async def _loop_async(self) -> None:
        """`_loop()` 的协程版 —— 逻辑逐行对应，只是把 `wait` 换成 `sleep`。"""
        if self._initial_delay > 0:
            try:
                await asyncio.wait_for(self._stop_event_async(), self._initial_delay)
                return  # stop 在等待期间被 set
            except TimeoutError:
                pass

        while True:
            if self._paused.is_set():
                await asyncio.sleep(self._poll)
                continue

            with self._lock:
                purged = self._purge_expired_locked()
                full = len(self._items) >= self._capacity
            if purged:
                await self._idle_sleep_async()
                continue
            if full:
                await asyncio.sleep(self._poll)
                continue

            fetch = self._afetch
            if fetch is None:
                await asyncio.sleep(self._poll)
                continue

            try:
                got = await fetch()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                with self._lock:
                    self._stats["failed"] += 1
                await asyncio.sleep(self._fail_backoff)
                continue

            if not got:
                await asyncio.sleep(self._poll)
                continue

            expire_ts, solved = got
            if float(expire_ts) - self._margin <= time.time():
                with self._lock:
                    self._stats["expired"] += 1
                await self._idle_sleep_async()
                continue

            with self._lock:
                self._items.append({"expire_ts": float(expire_ts), "solved": solved})
                self._stats["produced"] += 1
                self._idle_streak = 0

    async def _stop_event_async(self) -> None:
        """把 `threading.Event` 桥成可 await 的等待（仅用于初始延迟）。

        轮询而不是用回调：初始延迟只有几秒，`sleep(0.1)` 的开销可以忽略，
        而回调版本要在 `stop()` 里跨线程唤醒 asyncio —— 那需要保存主循环引用，
        正是上面注释里说的要避开的东西。
        """
        while not self._stop.is_set():
            await asyncio.sleep(0.1)
