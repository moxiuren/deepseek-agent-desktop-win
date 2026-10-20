"""L4 熔断层（DeepSeek 版）—— **只能事后刹车，这是本档位最大的能力缺口**。

与 gemini 版的根本差异
----------------------

Gemini 侧有 `otAQ7b`（账号状态）/ `GPRiHf`（abuse）/ `qpEbW`（quota）三个接口，
可以**主动查询**、**事前刹车**。所以那边 `circuit.py` 的核心是
`note_account_status(code)` / `note_abuse(...)` / `note_quota(...)`
—— 全部是「查回来的状态」。

DeepSeek 已知接口只有 3 个（`chat/completion`、`chat/create_pow_challenge`、
`chat_session/create`），**没有任何额度或账号状态接口**（2026-10-14 全项目 grep 确认）。
所以本层只能**由响应信号驱动**：

| 信号 | 来源 | 性质 |
|---|---|---|
| `401` / `403` | HTTP 状态码 | 认证失败（token 过期 **或** 账号被处置 —— **两者不可区分**） |
| `429` | HTTP 状态码 | 限流 |
| `Server busy` + `generation_timeout` | **HTTP 200** 的 `event: hint` | **生成阶段被掐**（实测 2026-10-15） |
| `biz_code != 0` | HTTP 200 的 JSON 体 | 业务错误（多为可恢复，如 session 失效） |
| 网络 / 解析异常 | 客户端侧 | RPC 抖动 |

**必须说清的三件事**（别把它当成 Gemini 那套的等价物）：

1. **这是事后刹车，不是事前。** 原版死在「等 40003 才知道出事」，本层把信号面
   从 1 个（40003）扩到 5 个，但**没有一个是主动查询**。它比原版强，但达不到 Gemini。
2. **`401` 无法区分「token 过期」与「账号被处置」。** 这两者的处置完全不同
   （前者刷新即可，后者要停手），而 DeepSeek 不给区分线索。所以本层按**保守**
   处理：连续两次 401 才升级为停机，且提示里两种可能都写上。
3. **`Server busy` 是唯一「前置」信号。** 它出现在生成被拒但账号未动的时候 ——
   相当于服务端在**明示「我现在不想理你」**。这是本档位最有价值的一个信号，
   所以给它单独的退避通道（见 `note_server_busy`）。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from .config import CircuitConfig


class BreakerLevel(StrEnum):
    CLOSED = "closed"        # 放行
    THROTTLED = "throttled"  # 降速放行
    OPEN = "open"            # 退避中
    HALTED = "halted"        # 停机，需人工介入

    @property
    def is_blocking(self) -> bool:
        return self in (BreakerLevel.OPEN, BreakerLevel.HALTED)


class DegradeTarget(StrEnum):
    """降级目标。

    **注意与 Gemini 版的关键差异**：那边 `WEBVIEW` 是个**空壳**
    （`transport/webview.py` 的 `start()` 直接 raise「尚未实现」），
    所谓「降级」根本没有落点。

    DeepSeek 侧**反而真的有这个资产** —— 原版（`deepseek-mac`）的
    `agent_bridge.js`（155 KB DOM 注入）是**已经实战跑通**的。所以这里的
    `WEBVIEW` 是**可实现的**，只是尚未接（排在阶段 5）。
    """

    NONE = "none"
    SLOWER = "slower"    # 仅降速（服务端忙）
    WEBVIEW = "webview"  # 降级到 DOM 通道 —— **尚未实现**，见上


class Signal(StrEnum):
    """熔断信号类别 —— 只此一处定义码→语义的映射。"""

    AUTH = "auth"
    #: **账号被处置（禁言 / 封禁）** —— 2026-10-19 从 `AUTH` 里拆出来。
    #:
    #: 原来它与「token 过期」共用 `AUTH`，于是熔断层对两者做同一件事：
    #: 指数退避 + 连续 3 次后停机。但语义完全不同 ——
    #:
    #:   * token 过期 → 刷新凭据就能继续，退避重试是对的
    #:   * 账号被禁言 → **有明确到期时间**（服务端原文里带 `mute_until`），
    #:     重试一次就多一条违规记录。正确处置是立刻停手、等它到期。
    #:
    #: 2026-10-17 那次事故就是被归到 `AUTH` 的（`biz_code=5 user is muted`），
    #: 熔断按「认证失败」退了 60 秒 —— 如果当时按 `MUTED` 直接停机，
    #: 后面 22:53 / 23:41 / 23:42 那几次重试就不会发出去。
    MUTED = "muted"
    RATE_LIMIT = "rate_limit"
    SERVER_BUSY = "server_busy"
    BIZ_ERROR = "biz_error"
    RPC_ERROR = "rpc_error"
    FATAL = "fatal"


@dataclass
class BreakerEvent:
    level: BreakerLevel
    reason: str
    signal: Signal | None = None
    until: float = 0.0
    fatal: bool = False


@dataclass
class BreakerState:
    level: BreakerLevel = BreakerLevel.CLOSED
    reason: str = "正常"
    until: float = 0.0
    remaining_ms: float = 0.0
    speed_factor: float = 1.0
    degrade: DegradeTarget = DegradeTarget.NONE
    # 计数（给界面与排障用）
    auth_failures: int = 0
    busy_streak: int = 0
    rpc_errors: int = 0
    biz_errors: int = 0
    #: 账号被处置的命中次数。**正常永远是 0 或 1** —— 命中即硬停，
    #: 之后不该再有任何请求打出去。大于 1 说明停机被绕过过。
    muted_hits: int = 0
    # 最近一次信号，便于「它为什么停了」一眼可见
    last_signal: str = ""
    last_signal_at: str = ""
    history: list[str] = field(default_factory=list)


def classify_error_text(text: str) -> Signal | None:
    """把一段错误文本归到熔断信号。

    **抽成公共函数**是因为有两个入口都会产出错误文本：传输层抛的
    `TransportError`、以及 `TurnOutcome.error`（服务端在 HTTP 200 里回的错误）。
    两处必须用**同一套**分类，否则同一件事在两条路径上会被算成两类。
    """
    t = text or ""
    # ★ 处置必须排在鉴权之前 —— 处置原文里常同时出现 `auth` / `40003` 字样
    #   （两者走同一个业务码），先判 AUTH 会把「禁言」当成「token 过期」，
    #   于是退避 60 秒后重试，而正确处理是立刻停手。
    low = t.lower()
    if (
        "账号被处置" in t
        or "禁言" in t
        or "封禁" in t
        or "suspended" in low
        or "user is muted" in low
    ):
        return Signal.MUTED
    if "429" in t or "限流" in t:
        return Signal.RATE_LIMIT
    if "Server busy" in t or "generation_timeout" in t:
        return Signal.SERVER_BUSY
    if "未认证" in t or "401" in t or "403" in t:
        return Signal.AUTH
    if "业务错误" in t or "biz_code" in t:
        return Signal.BIZ_ERROR
    if t:
        return Signal.RPC_ERROR
    return None


class CircuitBreaker:
    """四档熔断状态机（由响应信号驱动）。

    骨架沿用原版与 gemini 版（指数退避、上限、长停机），触发源换成响应信号。
    """

    def __init__(
        self,
        cfg: CircuitConfig,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = cfg
        self._clock = clock

        self._level = BreakerLevel.CLOSED
        self._reason = "正常"
        self._until = 0.0
        self._speed_factor = 1.0
        self._degrade = DegradeTarget.NONE

        self._auth_failures = 0
        self._busy_streak = 0
        self._rpc_errors = 0
        self._biz_errors = 0
        self._muted_hits = 0

        self._last_signal = ""
        self._last_signal_at = ""
        self._history: list[str] = []

    # ---------- 事件输入 ----------

    def note_muted(self, detail: str = "") -> BreakerEvent:
        """账号被处置（禁言 / 封禁）—— **直接硬停，不退避重试**。

        与 `note_auth_failure` 的分工：那个是「凭据可能过期，刷新就能好」，
        这个是「服务端已经明确说你的账号被处置了」。

        **为什么必须硬停**：处置是有期限的（服务端原文里带截止时间），
        重试不会让它变短，只会往违规记录上再加一笔。2026-10-17 事故里，
        22:53 / 23:41 / 23:42 那几次重试就是退避策略放行的 —— 当时若按本条
        直接停机，那三次根本发不出去。

        `hard_stop_ms` 是 24 小时兜底：禁言到期时间由服务端给（通常 3 天），
        本地拿不到准确值时不假装知道 —— 到点由人工确认后再 `reset()`。
        """
        self._muted_hits += 1
        reason = "账号被服务端处置（禁言/封禁）" + (f"：{detail}" if detail else "")
        self._record(Signal.MUTED, reason)
        return self._halt(reason)

    def note_auth_failure(self, hint: str = "") -> BreakerEvent:
        """401 / 403。

        **无法区分「token 过期」与「账号被处置」** —— DeepSeek 不给线索。
        所以按保守处理：指数退避，连续多次才停机；提示里两种可能都写。
        """
        self._auth_failures += 1
        backoff = min(
            self.cfg.auth_backoff_ms * self._auth_failures,
            self.cfg.auth_backoff_max_ms,
        )
        tail = f"（{hint}）" if hint else ""
        reason = (
            f"认证失败（连续 {self._auth_failures} 次）{tail}"
            "—— 可能是 token 过期（刷新即可），也可能是账号被处置（需停手）"
        )
        self._record(Signal.AUTH, reason)
        if self._auth_failures >= 3:
            return self._halt(
                f"{reason} ⇒ 连续 3 次，按账号被处置处理，停机待人工确认"
            )
        return self._open(backoff, reason, Signal.AUTH)

    def note_rate_limited(self, hold_ms: int = 90_000) -> BreakerEvent:
        self._record(Signal.RATE_LIMIT, "服务端限流 429")
        return self._open(hold_ms, "服务端限流（429）", Signal.RATE_LIMIT)

    def note_server_busy(self) -> BreakerEvent:
        """`Server busy` + `generation_timeout` —— **本档位最有价值的前置信号**。

        实测（2026-10-15）：服务端过载/软限流时**不报错**，而是把连接挂 ~68 秒后
        回 HTTP 200 + `event: hint` 的错误。也就是说它**明示了「我现在不想理你」**，
        但账号本身没被处置 —— 这是唯一一个「在出事之前」能拿到的信号。

        退避分两级：
          * 单次命中 → 退避 `busy_backoff_ms`（默认 30 分钟）
          * 连续命中达 `busy_streak_cooldown`（默认 3 次）→ 长冷却 `busy_cooldown_ms`（默认 6 小时）
        """
        self._busy_streak += 1
        self._degrade = DegradeTarget.SLOWER
        if self._busy_streak >= self.cfg.busy_streak_cooldown:
            reason = (
                f"服务端连续 {self._busy_streak} 次拒绝生成（Server busy / generation_timeout）"
                f" ⇒ 长冷却 {self.cfg.busy_cooldown_ms / 3_600_000:.0f} 小时"
            )
            self._record(Signal.SERVER_BUSY, reason)
            return self._open(self.cfg.busy_cooldown_ms, reason, Signal.SERVER_BUSY)

        reason = (
            f"服务端拒绝生成（第 {self._busy_streak} 次，Server busy）"
            f" ⇒ 退避 {self.cfg.busy_backoff_ms / 60_000:.0f} 分钟"
        )
        self._record(Signal.SERVER_BUSY, reason)
        return self._open(self.cfg.busy_backoff_ms, reason, Signal.SERVER_BUSY)

    def note_biz_error(self, msg: str = "") -> BreakerEvent | None:
        """业务错误（HTTP 200 但 `biz_code != 0`）。

        **多数是可恢复的** —— 例如 `biz_code=1 invalid chat session id`，
        正确处理是「换个会话重发」，不是「停手」。所以这里**只计数、不改状态**，
        否则用户改错一个会话 id 就会把传输层降级掉。
        """
        self._biz_errors += 1
        self._record(Signal.BIZ_ERROR, f"业务错误：{msg[:120]}")
        return None

    def note_rpc_error(self, msg: str = "") -> BreakerEvent | None:
        """网络 / 解析类异常（非业务、非认证）。"""
        self._rpc_errors += 1
        self._record(Signal.RPC_ERROR, f"RPC 异常：{msg[:120]}")
        if self._rpc_errors >= self.cfg.degrade_after_rpc_errors:
            self._degrade = DegradeTarget.WEBVIEW
            reason = (
                f"连续 {self._rpc_errors} 次 RPC 异常"
                " ⇒ 标记降级到 T1 DOM 通道（**尚未实现**，当前等同停用 RPC）"
            )
            return self._open(self.cfg.auth_backoff_ms, reason, Signal.RPC_ERROR)
        return None

    def note_fatal(self, msg: str) -> BreakerEvent:
        return self._halt(msg)

    def note_success(self) -> None:
        """一次完整成功轮次 —— 恢复。"""
        self._rpc_errors = 0
        self._auth_failures = 0
        self._busy_streak = 0
        if self._degrade is DegradeTarget.SLOWER:
            self._degrade = DegradeTarget.NONE
        if self._level is BreakerLevel.THROTTLED:
            self._level = BreakerLevel.CLOSED
            self._reason = "正常"
            self._speed_factor = 1.0

    def note_health_ok(self) -> None:
        """一次健康探测成功。

        **只清 RPC 计数，不清 `busy_streak`** —— 两件事的粒度不同：
          * RPC 计数衡量的是「链路通不通」，探测成功就说明通了
          * `busy_streak` 衡量的是「服务端愿不愿意生成」，而探测**不生成**，
            所以它没有资格证明生成已经恢复。清了会导致「冷却期里每轮探测都把
            计数清零，于是永远到不了长冷却阈值」。
        """
        self._rpc_errors = 0

    # ---------- 判定 ----------

    def evaluate(self) -> BreakerState:
        now = self._clock()
        if self._level is BreakerLevel.OPEN and now >= self._until:
            self._level = BreakerLevel.CLOSED
            self._reason = "退避结束，恢复"
            self._until = 0.0
            self._speed_factor = 1.0

        remaining = max(0.0, (self._until - now) * 1000.0) if self._until else 0.0
        return BreakerState(
            level=self._level,
            reason=self._reason,
            until=self._until,
            remaining_ms=remaining,
            speed_factor=self._speed_factor,
            degrade=self._degrade,
            auth_failures=self._auth_failures,
            busy_streak=self._busy_streak,
            rpc_errors=self._rpc_errors,
            biz_errors=self._biz_errors,
            # ★ 漏了这个参数，`snapshot()` 就永远读到默认值 0 ——
            #   字段加了、读取端也加了，唯独中间的构造漏了。
            muted_hits=self._muted_hits,
            last_signal=self._last_signal,
            last_signal_at=self._last_signal_at,
            history=list(self._history[-20:]),
        )

    def reset(self) -> None:
        """人工解除停机（仅在确认账号已恢复后使用）。"""
        self._level = BreakerLevel.CLOSED
        self._reason = "人工解除"
        self._until = 0.0
        self._speed_factor = 1.0
        self._auth_failures = 0
        self._busy_streak = 0
        self._rpc_errors = 0
        self._history.append(f"[{time.strftime('%H:%M:%S')}] 人工解除熔断")

    def snapshot(self) -> dict:
        st = self.evaluate()
        return {
            "level": st.level.value,
            "reason": st.reason,
            "remaining_ms": round(st.remaining_ms, 1),
            "speed_factor": st.speed_factor,
            "degrade": st.degrade.value,
            "counters": {
                "auth_failures": st.auth_failures,
                "busy_streak": st.busy_streak,
                "rpc_errors": st.rpc_errors,
                "biz_errors": st.biz_errors,
                "muted_hits": st.muted_hits,
            },
            "last_signal": st.last_signal,
            "last_signal_at": st.last_signal_at,
            "telemetry_available": False,
            "note": "DeepSeek 无账号状态接口：本层为事后刹车，非事前",
            "history": st.history,
        }

    # ---------- 内部 ----------

    def _record(self, sig: Signal, reason: str) -> None:
        self._last_signal = sig.value
        self._last_signal_at = time.strftime("%H:%M:%S")
        self._history.append(f"[{self._last_signal_at}] {sig.value}: {reason[:100]}")

    def _open(self, backoff_ms: float, reason: str, sig: Signal | None = None) -> BreakerEvent:
        now = self._clock()
        self._until = max(self._until, now + backoff_ms / 1000.0)
        if self._level is not BreakerLevel.HALTED:
            self._level = BreakerLevel.OPEN
            self._reason = reason
        self._history.append(f"[{time.strftime('%H:%M:%S')}] OPEN {reason[:100]}")
        return BreakerEvent(BreakerLevel.OPEN, reason, sig, self._until)

    def _halt(self, reason: str) -> BreakerEvent:
        self._level = BreakerLevel.HALTED
        self._reason = reason
        self._until = self._clock() + self.cfg.hard_stop_ms / 1000.0
        self._history.append(f"[{time.strftime('%H:%M:%S')}] HALT {reason[:100]}")
        return BreakerEvent(BreakerLevel.HALTED, reason, Signal.FATAL, self._until, fatal=True)
