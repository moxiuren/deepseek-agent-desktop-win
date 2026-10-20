"""L2 节奏层 —— 目标不是限流，是「像不像人」。

原版 `DynamicThrottle` 的参数（10s 窗口 ≤2 次整轮提交、BASE_GAP 1500ms、
FLOOR 200ms、突发后硬停 12s）是对 DeepSeek **响应式限流**做的适配 ——
换句话说，**它是「撞了才减速」**。

本层要解决的是更前面的一件事：行为特征。固定间隔本身就是机器特征，
1500ms + 均匀抖动仍然是个窄分布。所以改用 log-normal 分布，
并叠加昼夜节律与阅读停顿。

**DeepSeek 侧这条特别要紧**（2026-10-15 实测）：17 分钟内 9 次 `completion`
就撞上服务端 68 秒「生成超时」；原版历史记录里的按分钟限流
（`2026-09-12 Messages too frequent`，工具环 2~5 秒连发）也是同一个机制。
⇒ **本档位的请求密度上限比 Gemini 低得多**，节奏层不是装饰，是必需品。

分工（别和 L4 重叠）
--------------------
  * **硬停 / 退避** → `circuit.py`（L4）。它有独立退避与长冷却。
  * **恢复后放慢**   → 本层。`note_server_busy()` 只加罚，不做硬停 ——
    否则「一恢复就立刻满速再撞一次」。
"""

from __future__ import annotations

import math
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from .config import PacingConfig


@dataclass
class PacingDecision:
    allowed: bool
    wait_ms: int
    reason: str
    target_gap_ms: float = 0.0
    burst_count: int = 0

    @property
    def wait_sec(self) -> float:
        return round(self.wait_ms / 1000.0, 1)


@dataclass
class PacingStats:
    sends: int = 0
    holds: int = 0
    bursts: int = 0
    rate_limits: int = 0
    busy_hits: int = 0
    #: 被「单小时硬顶」拦下的次数（见 `PacingConfig.hourly_max`）。
    hour_capped: int = 0
    last_gap_ms: float = 0.0
    recent_gaps_ms: list[float] = field(default_factory=list)


class PacingGate:
    """提交预算闸门。

    clock 用单调时钟（测间隔），wall 用墙上时钟（判时段），二者不可混用。
    """

    def __init__(
        self,
        cfg: PacingConfig,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], datetime] = datetime.now,
        rng: random.Random | None = None,
    ) -> None:
        self.cfg = cfg
        self._clock = clock
        self._wall = wall
        self._rng = rng or random.Random()

        self._send_times: list[float] = []
        self._last_send_at: float | None = None
        self._last_gap_ms = 0.0
        self._turn_index = 0
        self._rate_limit_until: float = 0.0
        self._penalty_ms: float = 0.0
        self._read_pause_due = False
        self._long_pause_due = False
        #: 单小时窗口内的发送时刻 —— **独立于 `_send_times`**。
        #: `_send_times` 被 `_trim` 砍到 `window_ms`（10 秒），拿它算不出小时量。
        #: 2026-10-17 事故：峰值实测 75 次/小时，而 10 秒窗口的预算完全放行。
        self._hour_sends: list[float] = []
        # 本次提交的目标间隔 —— **抽一次就记住**，直到这次提交真的发生。
        # 见 evaluate() 里「为什么必须记住」那段。
        self._pending_target_ms: float | None = None
        self.stats = PacingStats()

    # ---------- 采样 ----------

    def sample_gap_ms(self) -> float:
        """log-normal 采样：中位数 = base_gap_ms，均值 ≈ base_gap_ms * e^(sigma^2/2)。"""
        mu = math.log(max(self.cfg.base_gap_ms, 1.0))
        value = self._rng.lognormvariate(mu, self.cfg.sigma)
        return min(max(value, self.cfg.floor_gap_ms), self.cfg.max_gap_ms)

    # ---------- 记录 ----------

    def record_send(self, now: float | None = None) -> None:
        """只在**真实提交成功**后调用，重试不计。

        `now` 传的是**提交发出那一刻**的时间，不是「这一轮结束」的时间。
        为什么必须这样：闸门要模拟的是「两次请求之间的间隔」，而上游看到的
        也正是这个量 —— 它天然包含上一轮的回答耗时。若拿「轮末」当锚点，
        那 60 秒的回答时间就被**从间隔里抹掉**了，等于在一段本来就很长的
        自然间隔之上，再叠加一次目标间隔的等待。详见 agent.py 的调用点。
        """
        t = self._clock() if now is None else now
        if self._last_send_at is not None:
            self._last_gap_ms = (t - self._last_send_at) * 1000.0
            self.stats.recent_gaps_ms.append(self._last_gap_ms)
            del self.stats.recent_gaps_ms[:-50]
        self._last_send_at = t
        self._send_times.append(t)
        self._hour_sends.append(t)
        self.stats.sends += 1
        self._turn_index += 1
        # 这次提交已经发生，本次的目标间隔作废 —— 下一次提交重新抽。
        self._pending_target_ms = None

        if self.cfg.read_pause_every > 0 and self._turn_index % self.cfg.read_pause_every == 0:
            self._read_pause_due = True
        if self.cfg.long_pause_every > 0 and self._turn_index % self.cfg.long_pause_every == 0:
            self._long_pause_due = True

    def note_external_send(self, now: float | None = None) -> None:
        """页面/其他通道发生的提交也要计入预算（原版用 window.__lastCompletionAt 实现）。"""
        t = self._clock() if now is None else now
        self._send_times.append(t)
        self._hour_sends.append(t)

    def note_rate_limit(self, hold_ms: int = 90_000) -> None:
        t = self._clock()
        self._rate_limit_until = max(self._rate_limit_until, t + hold_ms / 1000.0)
        self._penalty_ms = max(self._penalty_ms, float(self.cfg.burst_penalty_ms))
        self.stats.rate_limits += 1

    def note_server_busy(self, penalty_ms: int | None = None) -> None:
        """服务端拒绝生成（`Server busy` / `generation_timeout`）—— **只加罚，不硬停**。

        硬停由 L4 熔断层负责（它有独立的退避与 6 小时长冷却）。本层只做一件事：
        把**恢复之后**的目标间隔拉长，避免「冷却一结束就立刻满速，再撞一次」。
        默认罚 4 倍 `burst_penalty_ms`（即 10 秒），叠在 log-normal 抽样的目标间隔上。
        """
        self._penalty_ms = max(
            self._penalty_ms, float(penalty_ms or self.cfg.burst_penalty_ms * 4)
        )
        self.stats.busy_hits += 1

    # ---------- 判定 ----------

    def evaluate(self, now: float | None = None) -> PacingDecision:
        t = self._clock() if now is None else now
        self._trim(t)

        # 1. 限流冷却
        if t < self._rate_limit_until:
            return self._hold(
                (self._rate_limit_until - t) * 1000.0, "服务端限流冷却中", t
            )

        # 2. 昼夜节律
        hour = self._wall().hour
        if not (self.cfg.active_start_hour <= hour < self.cfg.active_end_hour):
            return self._hold(
                60_000.0,
                f"非活跃时段（{self.cfg.active_start_hour}:00–{self.cfg.active_end_hour}:00）",
                t,
                count_burst=False,
            )

        # 3. 突发预算
        if len(self._send_times) >= self.cfg.max_burst:
            last = self._send_times[-1]
            remain = (last + self.cfg.hold_after_burst_ms / 1000.0 - t) * 1000.0
            if remain > 0:
                self.stats.bursts += 1
                return self._hold(remain, "突发预算用尽，硬停中", t)

        # 3.5 ★ 单小时硬顶
        #
        # 为什么必须有这一层（2026-10-17 事故复盘）：10 秒窗口的预算
        # （`max_burst=2`）理论上限约 **600 次/小时**，对实测过的「一小时 75 次」
        # **完全无感**。而实测相邻间隔 77% 落在 10–29 秒 —— 约束来自模型响应耗时，
        # **不是来自闸门** ⇒ 节奏层从未成为约束，也就从未保护过我们。
        # 这条补的就是那个缺口。
        #
        # ⚠️ **单位是「发送（agent 轮次）」，不是「用户消息」。**
        #    `max_tool_turns` 默认 30 ⇒ 一条用户消息最多合法地用掉 30 次发送；
        #    实测平均 5.1 次/条、p90 = 24、最大 61（`.tmp/per_run.py`）。
        #    所以这个阈值**不能定小** —— 定成 20 会把正常任务掐断在中间
        #    （我一开始就犯了这个错，见 `PacingConfig.hourly_max` 的注释）。
        #    默认 150 是「兜住失控」而不是「限制用量」。
        #
        # 语义是**排队，不是拒绝**：等到最早那次发送滑出 1 小时窗口就能继续。
        if self.cfg.hourly_max > 0:
            self._trim_hour(t)
            if len(self._hour_sends) >= self.cfg.hourly_max:
                remain = (self._hour_sends[0] + 3600.0 - t) * 1000.0
                if remain > 0:
                    self.stats.hour_capped += 1
                    return self._hold(
                        remain,
                        f"已达单小时上限（{self.cfg.hourly_max} 次），等窗口滑出",
                        t,
                        count_burst=False,
                    )

        # 4. 间隔闸门
        #
        # ⚠️ 目标间隔**每次提交只抽一次**，抽完记住（`_pending_target_ms`），
        #    直到这次提交真的发生（`record_send` 把它清掉）才重抽。
        #
        # 为什么必须记住 —— 这里原先写的是 `target = self.sample_gap_ms()`，
        # 每次 `evaluate()` 都重新抽。而 `evaluate()` 的调用方是**轮询循环**
        # （`agent.py:_await_gate()` 的 `while True`），于是「等到目标间隔满足」
        # 变成了「一直重抽，直到某次抽到的目标恰好小于已经等过的时间」。
        # 等待量因此成了 K 次独立抽样的**最大值**，而不是一次抽样：
        # 实测（5000 次模拟）中位等待从 2.50s 膨胀到 3.77s、p99 到 10.9s、
        # 最长 18.3s —— 白等，且尾部很长，正是小莫报的「间隔太久」的组成部分。
        #
        # 更根本的一条：`evaluate()` 是**判定函数，必须幂等** ——
        # 同一个时刻调用两次必须给同一个答案。原先不满足，所以
        # `GateChain.snapshot()`（外壳每 5 秒一次的 status 轮询会走到）
        # 也会顺带重抽一次，让「看状态」这个动作本身扰动闸门。
        # 记住之后这两个问题一起消失。
        if self._pending_target_ms is None:
            self._pending_target_ms = self.sample_gap_ms()
        target = self._pending_target_ms + self._penalty_ms
        if self._last_send_at is not None:
            elapsed = (t - self._last_send_at) * 1000.0
            if elapsed < target:
                return self._hold(
                    target - elapsed, "间隔未达目标（拟人节奏）", t, target=target
                )

        # 5. 阅读停顿
        if self._read_pause_due:
            pause = self._rng.uniform(
                self.cfg.read_pause_min_ms, self.cfg.read_pause_max_ms
            )
            self._read_pause_due = False
            return self._hold(pause, "插入阅读停顿", t, target=target)

        # 5.5 长停顿（分钟级 —— 「人去做别的事了」，与上面 4–9 秒的「读一读」不同）
        if self._long_pause_due:
            pause = self._rng.uniform(
                self.cfg.long_pause_min_ms, self.cfg.long_pause_max_ms
            )
            self._long_pause_due = False
            return self._hold(pause, "插入长停顿（拟人）", t, target=target)

        self._penalty_ms = 0.0
        return PacingDecision(
            allowed=True,
            wait_ms=0,
            reason="放行",
            target_gap_ms=target,
            burst_count=len(self._send_times),
        )

    def snapshot(self) -> dict:
        t = self._clock()
        self._trim(t)
        self._trim_hour(t)
        next_ok = 0.0
        if self._last_send_at is not None:
            elapsed = (t - self._last_send_at) * 1000.0
            # 用**本次已经抽定的**目标，不再重新抽 —— 快照是只读动作，
            # 不该改变闸门状态（否则外壳每 5 秒看一次状态，就等于每 5 秒
            # 把目标间隔重掷一次骰子）。没抽过就先抽一次并记住。
            if self._pending_target_ms is None:
                self._pending_target_ms = self.sample_gap_ms()
            next_ok = max(0.0, self._pending_target_ms + self._penalty_ms - elapsed)
        return {
            "burst_count": len(self._send_times),
            "max_burst": self.cfg.max_burst,
            # ★ 单小时预算 —— 外壳要能看见「现在这一小时还剩多少」，否则
            #   用户只会看到「突然要等 40 分钟」而不知道原因。
            "hour_sends": len(self._hour_sends),
            "hourly_max": self.cfg.hourly_max,
            "hour_window_remaining_ms": round(
                max(0.0, (self._hour_sends[0] + 3600.0 - t) * 1000.0), 1
            )
            if self._hour_sends
            else 0.0,
            "last_gap_ms": round(self._last_gap_ms, 1),
            "next_ok_ms": round(next_ok, 1),
            "target_gap_ms": round(self._pending_target_ms or 0.0, 1),
            "active_hour": self._wall().hour,
            "in_active_window": self.cfg.active_start_hour
            <= self._wall().hour
            < self.cfg.active_end_hour,
            "rate_limit_remaining_ms": round(
                max(0.0, (self._rate_limit_until - t) * 1000.0), 1
            ),
            "stats": {
                "sends": self.stats.sends,
                "holds": self.stats.holds,
                "bursts": self.stats.bursts,
                "rate_limits": self.stats.rate_limits,
                "busy_hits": self.stats.busy_hits,
                "hour_capped": self.stats.hour_capped,
            },
        }

    # ---------- 内部 ----------

    def _trim(self, now: float) -> None:
        cutoff = now - self.cfg.window_ms / 1000.0
        self._send_times = [x for x in self._send_times if x >= cutoff]

    def _trim_hour(self, now: float) -> None:
        """单小时窗口。**与 `_trim` 分开** —— 窗口长度差 360 倍，不能共用。"""
        cutoff = now - 3600.0
        self._hour_sends = [x for x in self._hour_sends if x >= cutoff]

    def _hold(
        self,
        wait_ms: float,
        reason: str,
        now: float,
        target: float = 0.0,
        count_burst: bool = True,
    ) -> PacingDecision:
        if count_burst:
            self.stats.holds += 1
        # 向上取整，**不能截断**：截断会把「还差 0.4ms」变成 `wait_ms=0`，
        # 于是返回一个 `allowed=False, wait_ms=0` 的自相矛盾判定 ——
        # 调用方（`_await_gate`）拿着 0 毫秒没法睡，只能靠 `max(wait, 0.2)`
        # 兜底，白多空转一跳。向上取整之后，闸门恒在**一跳**内收敛。
        return PacingDecision(
            allowed=False,
            # `math.ceil` 在 Python 3 直接返回 int，不必再包一层 `int()`。
            wait_ms=math.ceil(max(0.0, wait_ms)),
            reason=reason,
            target_gap_ms=target,
            burst_count=len(self._send_times),
        )
