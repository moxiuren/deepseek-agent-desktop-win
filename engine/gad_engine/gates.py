"""四层闸门编排（DeepSeek 版）。

顺序：**L1 身份 → L4 熔断 → L2 节奏 → L3 会话轮换**。

L1 放最前，因为身份配置违规属于「根本不该开工」；L4 在 L2 之前，因为停机
优先级高于节流 —— 已经要停机了就不必再算间隔。

本文件相对 gemini 版的实质增量：**统一的信号入口**。
DeepSeek 的错误会从**两条路**出来，而且它们的形状完全不同：

  ① 传输层**抛出** `TransportError` —— HTTP 非 200、网络异常
  ② 传输层**正常返回**但 `TurnOutcome.error` 非空 —— HTTP 200 里的服务端错误
     （`Server busy` / `generation_timeout` / `biz_code != 0`）

如果两条路各写一套判断，同一件事就会被算成两类，熔断计数就失真了。
所以这里把两者都收敛到 `circuit.classify_error_text` 一处分类。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .circuit import (
    BreakerEvent,
    BreakerLevel,
    CircuitBreaker,
    Signal,
    classify_error_text,
)
from .config import EngineConfig
from .pacing import PacingGate
from .session import SessionManager
from .transport.base import TransportError, TurnOutcome


@dataclass
class GateDecision:
    allowed: bool
    wait_ms: int
    reason: str
    layer: str
    speed_factor: float = 1.0
    fatal: bool = False
    violations: list[str] = field(default_factory=list)

    @property
    def wait_sec(self) -> float:
        return round(self.wait_ms / 1000.0, 1)


class GateChain:
    def __init__(
        self,
        cfg: EngineConfig,
        pacing: PacingGate,
        breaker: CircuitBreaker,
        session: SessionManager,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = cfg
        self.pacing = pacing
        self.breaker = breaker
        self.session = session
        self._clock = clock

    # ---------- L1 ----------

    def identity_violations(self) -> list[str]:
        return self.cfg.identity.violations()

    # ---------- 综合判定 ----------

    def evaluate(self) -> GateDecision:
        violations = self.identity_violations()
        if violations:
            return GateDecision(
                allowed=False,
                wait_ms=0,
                reason="；".join(violations),
                layer="L1",
                fatal=True,
                violations=violations,
            )

        bstate = self.breaker.evaluate()
        if bstate.level is BreakerLevel.HALTED:
            return GateDecision(
                allowed=False,
                wait_ms=int(bstate.remaining_ms),
                reason=f"[L4] {bstate.reason}",
                layer="L4",
                fatal=True,
            )
        if bstate.level is BreakerLevel.OPEN:
            return GateDecision(
                allowed=False,
                wait_ms=int(bstate.remaining_ms),
                reason=f"[L4] {bstate.reason}",
                layer="L4",
            )

        decision = self.pacing.evaluate()
        if not decision.allowed:
            return GateDecision(
                allowed=False,
                wait_ms=decision.wait_ms,
                reason=f"[L2] {decision.reason}",
                layer="L2",
                speed_factor=bstate.speed_factor,
            )

        if bstate.speed_factor > 1.0:
            return GateDecision(
                allowed=True,
                wait_ms=0,
                reason=f"[L4] 降速 x{bstate.speed_factor:g}",
                layer="L4",
                speed_factor=bstate.speed_factor,
            )

        return GateDecision(allowed=True, wait_ms=0, reason="放行", layer="—")

    # ---------- 信号入口（本文件的核心增量） ----------

    def note_transport_error(self, exc: TransportError) -> BreakerEvent | None:
        """路径 ①：传输层抛出的异常。

        `exc.config=True` 表示**配置/用法问题**（例如参数格式不对、会话锚点损坏）。
        它既不是 RPC 抖动也不是认证失败 —— **必须直接返回，不能计入熔断**，
        否则用户手改错一个参数，重试三次就会把传输层降级掉，
        而真正的原因反而被埋在「RPC 连续失败」后面。

        注：这条 docstring 原先举的例子是「传了未实现的附件上传」。
        上传通道已于 2026-10-18 实现，那个例子不再存在，换成仍然成立的例子。
        `config=True` 的语义本身没变。
        """
        # ★ 处置优先于一切 —— 包括 `config` 与 `fatal`。账号已经被停掉了，
        #   「这是配置问题」或「传输层不可恢复」都已经不重要，重要的是立刻停手。
        #   （`config` 排在它后面是刻意的：一个配置错误不会被服务端判成禁言，
        #   但反过来，禁言的原文里确实可能夹着别的标志位。）
        if getattr(exc, "suspended", False) or classify_error_text(str(exc)) is Signal.MUTED:
            return self.breaker.note_muted(str(exc)[:160])
        if exc.config:
            return None
        if exc.fatal:
            return self.breaker.note_fatal(str(exc))

        sig = classify_error_text(str(exc))
        if exc.auth or sig is Signal.AUTH:
            return self.breaker.note_auth_failure(str(exc)[:80])
        if sig is Signal.RATE_LIMIT:
            return self.breaker.note_rate_limited()
        if sig is Signal.SERVER_BUSY:
            return self._busy()
        if sig is Signal.BIZ_ERROR:
            return self.breaker.note_biz_error(str(exc))
        return self.breaker.note_rpc_error(str(exc))

    def note_outcome(self, outcome: TurnOutcome) -> BreakerEvent | None:
        """路径 ②：HTTP 200 但服务端在流里回了错误。

        **这条路最容易被漏掉** —— 状态码是 200，只有读 `outcome.error` 才知道出事。
        今天实测的 `Server busy` + `generation_timeout` 就走这条。
        """
        if outcome.ok:
            return None
        sig = classify_error_text(outcome.error)
        if sig is Signal.MUTED:
            return self.breaker.note_muted(outcome.error[:160])
        if sig is Signal.SERVER_BUSY:
            return self._busy()
        if sig is Signal.RATE_LIMIT:
            return self.breaker.note_rate_limited()
        if sig is Signal.AUTH:
            return self.breaker.note_auth_failure(outcome.error[:80])
        if sig is Signal.BIZ_ERROR:
            return self.breaker.note_biz_error(outcome.error)
        # 空回复等其它情况：不算 RPC 异常（可能是服务端截断），只记录
        self.breaker.note_biz_error(outcome.error or "空回复")
        return None

    def note_busy(self) -> BreakerEvent:
        """显式上报一次「服务端忙」（供探针/上层直接调用）。"""
        return self._busy()

    def _busy(self) -> BreakerEvent:
        """服务端忙 —— **两层同时响应，但各管一段**：

          * L4 负责**硬停/退避**（30 分钟起，连续 3 次升到 6 小时）
          * L2 负责**恢复后放慢**（加罚，避免一恢复就满速再撞）

        分开是刻意的：如果两层都做硬停，退避会被叠成两倍；
        如果两层都不做罚，恢复瞬间的密度会和出事前一模一样。
        """
        self.pacing.note_server_busy()
        return self.breaker.note_server_busy()

    def note_success(self) -> None:
        """一次完整成功轮次。"""
        self.breaker.note_success()

    # ---------- 提交记账 ----------

    def commit(self, external: bool = False, now: float | None = None) -> None:
        """只在**真实提交成功**后调用；重试不计。

        `now` 是**提交发出那一刻**的时间戳（不是「这一轮跑完」的时刻）。
        调用方必须传它 —— 间隔闸门要量的是两次请求之间的间隔，用轮末时刻
        会把上一轮的回答耗时从间隔里抹掉，凭空多出一次等待。
        """
        if external:
            self.pacing.note_external_send()
        else:
            self.pacing.record_send(now)

    def after_turn(self) -> None:
        self.breaker.note_success()

    def rotate_if_needed(self):
        return self.session.maybe_rotate()

    # ---------- 状态快照 ----------

    def snapshot(self) -> dict:
        # 这里不要再调一次 `self.breaker.evaluate()`：上面 `evaluate()` 内部
        # 已经调过，而 `breaker.snapshot()` 自己也会读状态。重复调用除了多花
        # 一次时钟读取之外没有意义（`evaluate()` 有「OPEN 超时转 CLOSED」的
        # 副作用，但同一时刻调两次的结果完全一样）。
        decision = self.evaluate()
        return {
            "identity": {
                "account_label": self.cfg.identity.account_label,
                "impersonate": self.cfg.identity.browser_impersonate,
                "proxy": self.cfg.identity.proxy or "（直连）",
                "violations": self.identity_violations(),
                "notices": self.cfg.identity.notices(),
            },
            "circuit": self.breaker.snapshot(),
            "pacing": self.pacing.snapshot(),
            "session": self.session.snapshot(),
            "decision": {
                "allowed": decision.allowed,
                "reason": decision.reason,
                "layer": decision.layer,
                "wait_ms": decision.wait_ms,
            },
        }
