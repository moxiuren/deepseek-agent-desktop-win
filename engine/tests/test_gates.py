"""闸门层自测（不依赖 pytest，直接跑）。

覆盖：
  L1 身份违规拦截 · 正常放行 · L2 间隔拦截 · L4 服务端忙退避与长冷却 ·
  成功恢复 · 认证失败升级停机 · 业务错误不误伤 · 配置错误不计入熔断 ·
  两条信号路径（抛出的 TransportError / 200 里的 outcome.error）分类一致

用法：
    cd engine && ./.venv/Scripts/python.exe tests/test_gates.py
"""

from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gad_engine.circuit import (
    BreakerLevel,
    CircuitBreaker,
    DegradeTarget,
    Signal,
    classify_error_text,
)
from gad_engine.config import EngineConfig
from gad_engine.gates import GateChain
from gad_engine.pacing import PacingGate
from gad_engine.session import SessionManager
from gad_engine.transport.base import TransportError, TurnOutcome


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, sec: float) -> None:
        self.t += sec


PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    mark = "通过" if cond else "失败"
    print(f"  [{mark}] {name}" + (f" —— {detail}" if detail and not cond else ""))


def build(clock: FakeClock, *, account_label: str = "test-account"):
    cfg = EngineConfig()
    cfg.identity.account_label = account_label
    # 关掉昼夜节律，否则测试会受真实小时数影响
    cfg.pacing.active_start_hour = 0
    cfg.pacing.active_end_hour = 24
    # 关掉阅读停顿，让节奏可预测
    cfg.pacing.read_pause_every = 0
    # 单小时硬顶也关掉 —— 本节要反复 evaluate/record_send 做场景，
    # 默认 20 会在中途把判定变成「等窗口滑出 ≈1 小时」（挂起，不报错）。
    cfg.pacing.hourly_max = 0
    pacing = PacingGate(cfg.pacing, clock=clock)
    breaker = CircuitBreaker(cfg.circuit, clock=clock)
    session = SessionManager(max_turns=cfg.pacing.max_turns_per_session)
    chain = GateChain(cfg, pacing, breaker, session, clock=clock)
    return cfg, pacing, breaker, session, chain


def main() -> int:
    print("=" * 70)
    print("闸门层自测")
    print("=" * 70)

    # ---------- 1. L1 身份违规 ----------
    print("\n[1] L1 身份层")
    clock = FakeClock()
    cfg, pacing, breaker, _session, chain = build(clock, account_label="")
    d = chain.evaluate()
    check("未声明专用账号 → 拦截", not d.allowed and d.layer == "L1", d.reason)
    check("L1 拦截标 fatal", d.fatal)
    check("违规项非空", bool(d.violations))

    clock = FakeClock()
    cfg, pacing, breaker, _session, chain = build(clock)
    d = chain.evaluate()
    check("已声明账号 → 放行", d.allowed and d.layer == "—", d.reason)

    # 访客模式（Gemini 档位）下，「专用账号标识」这条前提不成立：
    # 没登录就没有账号可连累。若不短路，全新安装切到访客档会卡在
    # 「引擎起来了、状态也正常、但一个字都发不出去」，且报错指向一个
    # 访客模式根本用不到的字段。
    #
    # **所有变量都要换名，尤其是 `clock`。** 后面的 [2] 节直接拿上面的
    # `chain` + `clock` 继续 evaluate（没有重新 build）—— 第一版我既改了
    # `cfg` 又把 `clock` 重新赋值，两个都污染了 [2]：前者让它撞 L1，
    # 后者让 `chain.commit(now=clock())` 用的时间与 chain 内部时钟脱钩。
    gclock = FakeClock()
    gcfg, _gp, _gb, _gs, gchain = build(gclock, account_label="")
    gcfg.identity.guest_mode = True
    d = gchain.evaluate()
    check("访客模式 + 无标识 → 放行", d.allowed, d.reason)
    check("访客模式 → 违规项为空", not d.violations, "；".join(d.violations))

    # 但关掉访客模式后必须立刻恢复拦截 —— 否则「切回登录档」会带着
    # 一个空标识静默放行，而 DeepSeek 侧封号是账号级的。
    gcfg.identity.guest_mode = False
    d = gchain.evaluate()
    check("退出访客模式 → 恢复拦截", not d.allowed and d.layer == "L1", d.reason)

    # ---------- 2. L2 间隔闸门 ----------
    print("\n[2] L2 节奏层")
    chain.commit(now=clock())
    d = chain.evaluate()
    check("刚提交后 → 被间隔拦住", not d.allowed and d.layer == "L2", d.reason)
    clock.advance(30)
    d = chain.evaluate()
    check("等够 30s → 放行", d.allowed, d.reason)

    # ---------- 3. L4 服务端忙 ----------
    print("\n[3] L4 熔断层 · 服务端忙")
    clock = FakeClock()
    cfg, pacing, breaker, _session, chain = build(clock)
    ev = chain.note_busy()
    check("第 1 次忙 → OPEN", ev.level is BreakerLevel.OPEN, ev.reason)
    check("退避 30 分钟", abs(ev.until - clock() - 1800) < 1, f"until-clock={ev.until - clock():.0f}s")
    check("L2 同时被加罚", pacing.stats.busy_hits == 1)
    d = chain.evaluate()
    check("熔断中 → 拦截且 layer=L4", not d.allowed and d.layer == "L4", d.reason)
    clock.advance(1801)
    d = chain.evaluate()
    check("退避结束 → 放行", d.allowed, d.reason)

    # 连续 3 次 → 长冷却 6h
    clock = FakeClock()
    cfg, pacing, breaker, _session, chain = build(clock)
    chain.note_busy()
    clock.advance(1801)
    chain.note_busy()
    clock.advance(1801)
    ev = chain.note_busy()
    check(
        "连续第 3 次忙 → 长冷却 6 小时",
        abs(ev.until - clock() - 21600) < 1,
        f"until-clock={ev.until - clock():.0f}s",
    )

    # ---------- 4. 成功恢复 ----------
    print("\n[4] 成功恢复")
    clock = FakeClock()
    cfg, pacing, breaker, _session, chain = build(clock)
    chain.note_busy()
    chain.note_success()
    st = breaker.evaluate()
    check("成功后 busy_streak 归零", st.busy_streak == 0)
    check("成功后降级标记清空", st.degrade is DegradeTarget.NONE, st.degrade.value)
    chain.note_busy()
    st = breaker.evaluate()
    check("恢复后再忙 → 从第 1 次重新计", st.busy_streak == 1, f"streak={st.busy_streak}")

    # ---------- 5. 认证失败升级停机 ----------
    print("\n[5] 认证失败")
    clock = FakeClock()
    cfg, pacing, breaker, _session, chain = build(clock)
    chain.note_transport_error(TransportError("未认证：HTTP 401", auth=True))
    st = breaker.evaluate()
    check("第 1 次 401 → OPEN 非停机", st.level is BreakerLevel.OPEN, st.level.value)
    clock.advance(61)
    chain.note_transport_error(TransportError("未认证：HTTP 401", auth=True))
    clock.advance(121)
    chain.note_transport_error(TransportError("未认证：HTTP 401", auth=True))
    st = breaker.evaluate()
    check("第 3 次 401 → HALTED", st.level is BreakerLevel.HALTED, st.level.value)
    check("停机理由写明两种可能", "token 过期" in st.reason and "账号被处置" in st.reason, st.reason)

    # ---------- 6. 业务错误不误伤 ----------
    print("\n[6] 业务错误（可恢复）")
    clock = FakeClock()
    cfg, pacing, breaker, _session, chain = build(clock)
    chain.note_transport_error(TransportError("completion 业务错误 biz_code=1：invalid chat session id"))
    st = breaker.evaluate()
    check("业务错误 → 状态不变（仍 CLOSED）", st.level is BreakerLevel.CLOSED, st.level.value)
    check("业务错误计数 +1", st.biz_errors == 1, f"biz_errors={st.biz_errors}")

    # ---------- 7. 配置错误不计入熔断 ----------
    print("\n[7] 配置错误（不该计入）")
    clock = FakeClock()
    cfg, pacing, breaker, _session, chain = build(clock)
    ev = chain.note_transport_error(TransportError("附件上传尚未实现", config=True))
    st = breaker.evaluate()
    check("config 错误 → 返回 None", ev is None)
    check("config 错误 → 不影响任何计数", st.rpc_errors == 0 and st.biz_errors == 0, str(st))

    # ---------- 8. 两条信号路径分类一致 ----------
    print("\n[8] 两条信号路径")
    clock = FakeClock()
    cfg, pacing, breaker, _session, chain = build(clock)

    busy_text = "服务端错误：Server busy, please try again later.（finish_reason=generation_timeout）"
    check("文本分类 → SERVER_BUSY", classify_error_text(busy_text) is Signal.SERVER_BUSY)

    # 路径 ①：抛出
    clock = FakeClock()
    cfg, pacing, breaker, _session, chain = build(clock)
    chain.note_transport_error(TransportError(busy_text))
    a = breaker.evaluate()
    # 路径 ②：outcome.error（HTTP 200）
    clock = FakeClock()
    cfg, pacing, breaker, _session, chain = build(clock)
    chain.note_outcome(TurnOutcome(text="", error=busy_text))
    b = breaker.evaluate()
    check(
        "抛出 / 200 内错误 → 同一处理（都是 SERVER_BUSY + OPEN）",
        a.last_signal == b.last_signal == "server_busy"
        and a.level is b.level is BreakerLevel.OPEN,
        f"{a.last_signal}/{a.level.value} vs {b.last_signal}/{b.level.value}",
    )
    check("成功 outcome → 不产生事件", chain.note_outcome(TurnOutcome(text="hi")) is None)

    # ---------- 9. 快照形状 ----------
    print("\n[9] 状态快照")
    clock = FakeClock()
    cfg, pacing, breaker, _session, chain = build(clock)
    snap = chain.snapshot()
    check("快照含四层", all(k in snap for k in ("identity", "circuit", "pacing", "session", "decision")))
    check("熔断快照标注无遥测", snap["circuit"]["telemetry_available"] is False)
    check("身份快照不含已移除的访客模式提示", snap["identity"]["notices"] == [])

    # ---------- 10. L2 单小时硬顶 ----------
    #
    # 2026-10-17 防封禁复盘 P2-1：原参数（max_burst=2 + window=10s +
    # hold_after_burst=12s）理论天花板约 600 次/小时，对实测「一小时 75 次」
    # **完全无感**。新增 `hourly_max` 做聚合硬顶，语义是**排队**（等最早那次
    # 滑出 1 小时窗口），不是永久拒绝。
    print("\n[10] L2 单小时硬顶")
    clock = FakeClock()
    cfg, pacing, breaker, _session, chain = build(clock)
    cfg.pacing.hourly_max = 3
    # 把间隔/突发层让开，确保被拦时**一定是**小时顶而不是别的层
    cfg.pacing.base_gap_ms = 0.0
    cfg.pacing.floor_gap_ms = 0.0
    cfg.pacing.max_gap_ms = 0.0
    cfg.pacing.max_burst = 10_000
    cfg.pacing.hold_after_burst_ms = 0
    for _ in range(3):
        pacing.record_send()
        clock.advance(1)
    d = chain.evaluate()
    check("满 3 次后 → 拦截", not d.allowed, d.reason)
    check("理由指向「单小时上限」", "单小时上限" in d.reason, d.reason)
    check("hour_capped 计数累加", pacing.stats.hour_capped == 1, str(pacing.stats.hour_capped))
    snap = pacing.snapshot()
    check(
        "快照暴露小时预算（外壳要能解释「为什么要等」）",
        snap.get("hour_sends") == 3 and snap.get("hourly_max") == 3,
        f"hour_sends={snap.get('hour_sends')} hourly_max={snap.get('hourly_max')}",
    )
    check(
        "快照给出窗口剩余毫秒",
        (snap.get("hour_window_remaining_ms") or 0) > 0,
        str(snap.get("hour_window_remaining_ms")),
    )
    clock.t = 1000.0 + 3601.0
    d = chain.evaluate()
    check("最早那次滑出窗口 → 放行（排队而非永久拒绝）", d.allowed, d.reason)

    # 关闭开关时该层不生效（默认值由调用方决定）
    clock = FakeClock()
    cfg, pacing, breaker, _session, chain = build(clock)
    cfg.pacing.hourly_max = 0
    cfg.pacing.max_burst = 10_000
    cfg.pacing.hold_after_burst_ms = 0
    cfg.pacing.base_gap_ms = 0.0
    cfg.pacing.floor_gap_ms = 0.0
    cfg.pacing.max_gap_ms = 0.0
    for _ in range(50):
        pacing.record_send()
        clock.advance(1)
    d = chain.evaluate()
    check("hourly_max=0 → 该层关闭", d.allowed, d.reason)

    # ---------- 汇总 ----------
    print("\n" + "-" * 70)
    print(f"通过 {len(PASS)} · 失败 {len(FAIL)}")
    if FAIL:
        for f in FAIL:
            print(f"  失败：{f}")
    print("  [闸门层自测通过]" if not FAIL else "  [闸门层自测未通过]")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
