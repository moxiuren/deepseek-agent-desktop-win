"""并行工具执行自测。

重点验证**分组正确性**（连续只读段），而不是「真的并发跑」——
后者靠 asyncio.gather 保证，前者才是我们自己写的逻辑，也最容易错。

用法：
    cd engine && ./.venv/Scripts/python.exe tests/test_parallel.py
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gad_engine.agent import AgentRuntime, ToolExecution
from gad_engine.protocol import ToolCall, ToolKind

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
    else:
        FAIL.append(name + (f" — {detail}" if detail else ""))


def C(kind: ToolKind) -> ToolCall:
    return ToolCall(kind=kind, target="", payload="")


class _FakeCfg:
    class executor:
        parallel_reads = True


class _FakeReport:
    def __init__(self):
        self.executions = []


def test_groups() -> None:
    rt = AgentRuntime.__new__(AgentRuntime)

    # 全是只读 → 一组
    calls = [C(ToolKind.READ_FILE), C(ToolKind.GREP), C(ToolKind.GLOB)]
    groups = rt._group_for_parallel(calls)
    check("全只读 → 1 组", len(groups) == 1, str(len(groups)))
    check("组内含 3 个", len(groups[0]) == 3, str(len(groups[0])))

    # 读-写-读 → 3 组（写单独一组）
    calls = [C(ToolKind.READ_FILE), C(ToolKind.WRITE_FILE), C(ToolKind.READ_FILE)]
    groups = rt._group_for_parallel(calls)
    check("读-写-读 → 3 组", len(groups) == 3, str([len(g) for g in groups]))
    check("中间那组是写", groups[1][0][1].kind == ToolKind.WRITE_FILE)

    # 读读-写-读读 → 3 组（[2,1,2]）
    calls = [
        C(ToolKind.READ_FILE), C(ToolKind.GREP),
        C(ToolKind.WRITE_FILE),
        C(ToolKind.READ_FILE), C(ToolKind.GLOB),
    ]
    groups = rt._group_for_parallel(calls)
    check("读读-写-读读 → 3 组", len(groups) == 3, str([len(g) for g in groups]))
    check("组大小 [2,1,2]", [len(g) for g in groups] == [2, 1, 2],
          str([len(g) for g in groups]))

    # 全是写 → 每个单独一组（全串行）
    calls = [C(ToolKind.WRITE_FILE), C(ToolKind.LOCAL_CMD)]
    groups = rt._group_for_parallel(calls)
    check("全写 → 2 组", len(groups) == 2, str(len(groups)))

    # 空
    check("空输入 → 0 组", len(rt._group_for_parallel([])) == 0)

    # 保持原下标 —— 这是关键：并行后还要按原顺序回结果
    calls = [C(ToolKind.GREP), C(ToolKind.READ_FILE), C(ToolKind.WRITE_FILE)]
    groups = rt._group_for_parallel(calls)
    idxs = [i for g in groups for (i, _c) in g]
    check("下标完整且有序", idxs == [0, 1, 2], str(idxs))


def test_parallel_timing() -> None:
    """真跑一次：三个「睡 0.2 秒」的只读调用并发，总耗时应远小于 0.6 秒。"""

    class FakeRt(AgentRuntime):
        async def _execute(self, call):  # type: ignore[override]
            await asyncio.sleep(0.2)
            return (
                ToolExecution(kind=call.kind.value, target="", exit_code=0, output="ok"),
                "ok",
                None,
            )

    rt = FakeRt.__new__(FakeRt)
    rt.cfg = _FakeCfg()
    rt._tool_heartbeat = lambda *a, **k: asyncio.sleep(99)  # type: ignore
    rt._emit = lambda *a, **k: None  # type: ignore

    calls = [C(ToolKind.READ_FILE), C(ToolKind.GREP), C(ToolKind.GLOB)]
    report = _FakeReport()
    start = time.monotonic()
    results, _att = asyncio.run(rt._dispatch(calls, report, turn=1))  # type: ignore
    elapsed = time.monotonic() - start

    check("并发：3×0.2s < 0.5s", elapsed < 0.5, f"{elapsed:.3f}s")
    check("结果数正确", len(results) == 3, str(len(results)))
    check("executions 记录 3 条", len(report.executions) == 3, str(len(report.executions)))


def test_serial_when_disabled() -> None:
    """关掉并行 → 串行，耗时约 0.6 秒。"""

    class FakeCfg:
        class executor:
            parallel_reads = False

    class FakeRt(AgentRuntime):
        async def _execute(self, call):  # type: ignore[override]
            await asyncio.sleep(0.2)
            return (
                ToolExecution(kind=call.kind.value, target="", exit_code=0, output="ok"),
                "ok",
                None,
            )

    rt = FakeRt.__new__(FakeRt)
    rt.cfg = FakeCfg()
    rt._tool_heartbeat = lambda *a, **k: asyncio.sleep(99)  # type: ignore
    rt._emit = lambda *a, **k: None  # type: ignore

    calls = [C(ToolKind.READ_FILE), C(ToolKind.GREP), C(ToolKind.GLOB)]
    start = time.monotonic()
    asyncio.run(rt._dispatch(calls, _FakeReport(), turn=1))  # type: ignore
    elapsed = time.monotonic() - start
    check("串行：3×0.2s ≥ 0.55s", elapsed >= 0.55, f"{elapsed:.3f}s")


def test_order_preserved() -> None:
    """并发后，结果顺序必须与调用顺序一致（否则模型会对错结果）。"""

    class FakeRt(AgentRuntime):
        async def _execute(self, call):  # type: ignore[override]
            # 第一个故意慢 —— 若按完成顺序收集，结果会乱
            if call.target == "slow":
                await asyncio.sleep(0.15)
            else:
                await asyncio.sleep(0.01)
            return (
                ToolExecution(kind=call.kind.value, target=call.target,
                              exit_code=0, output=call.target),
                call.target,
                None,
            )

    rt = FakeRt.__new__(FakeRt)
    rt.cfg = _FakeCfg()
    rt._tool_heartbeat = lambda *a, **k: asyncio.sleep(99)  # type: ignore
    rt._emit = lambda *a, **k: None  # type: ignore

    calls = [
        ToolCall(kind=ToolKind.READ_FILE, target="slow", payload=""),
        ToolCall(kind=ToolKind.GREP, target="fast", payload=""),
    ]
    results, _ = asyncio.run(rt._dispatch(calls, _FakeReport(), turn=1))  # type: ignore
    check("顺序保持（slow 在前）", results == ["slow", "fast"], str(results))


def test_single_exception_swallowed() -> None:
    """单个工具抛异常时，不该终止整轮 —— 吞成结果文本。"""

    class FakeRt(AgentRuntime):
        async def _execute(self, call):  # type: ignore[override]
            if call.kind is ToolKind.WRITE_FILE:
                raise RuntimeError("写炸了")
            return (
                ToolExecution(kind=call.kind.value, target="", exit_code=0, output="ok"),
                "ok",
                None,
            )

    rt = FakeRt.__new__(FakeRt)
    rt.cfg = _FakeCfg()
    rt._tool_heartbeat = lambda *a, **k: asyncio.sleep(99)  # type: ignore
    rt._emit = lambda *a, **k: None  # type: ignore

    # 单跑一个写（会抛）—— 不该冒泡
    calls = [C(ToolKind.WRITE_FILE)]
    try:
        results, _ = asyncio.run(rt._dispatch(calls, _FakeReport(), turn=1))  # type: ignore
        check("单跑异常不冒泡", True)
        check("异常变成结果文本", results and "异常" in results[0], str(results))
    except RuntimeError as exc:
        check("单跑异常不冒泡", False, f"冒泡了：{exc}")


def test_cancel_propagates() -> None:
    """取消信号必须穿透 —— 不能被 gather 吞成「工具异常」。"""

    class FakeRt(AgentRuntime):
        async def _execute(self, call):  # type: ignore[override]
            raise asyncio.CancelledError()

    rt = FakeRt.__new__(FakeRt)
    rt.cfg = _FakeCfg()
    rt._tool_heartbeat = lambda *a, **k: asyncio.sleep(99)  # type: ignore
    rt._emit = lambda *a, **k: None  # type: ignore

    # 并行的两个只读调用都抛 CancelledError
    calls = [C(ToolKind.READ_FILE), C(ToolKind.GREP)]
    try:
        asyncio.run(rt._dispatch(calls, _FakeReport(), turn=1))  # type: ignore
        check("取消信号穿透", False, "被吞掉了（run.cancel 会失效）")
    except asyncio.CancelledError:
        check("取消信号穿透", True)
    except Exception as exc:  # noqa: BLE001
        check("取消信号穿透", False, f"抛了别的：{type(exc).__name__}")


def main() -> int:
    print("\n[1] 分组逻辑")
    test_groups()

    print("\n[2] 真并发计时")
    test_parallel_timing()

    print("\n[3] 关掉并行 → 串行")
    test_serial_when_disabled()

    print("\n[4] 结果顺序保持")
    test_order_preserved()

    print("\n[5] 单跑异常不终止整轮")
    test_single_exception_swallowed()

    print("\n[6] 取消信号穿透")
    test_cancel_propagates()

    print("\n" + "-" * 70)
    print(f"  通过 {len(PASS)} · 失败 {len(FAIL)}")
    if FAIL:
        print("  失败项：")
        for name in FAIL:
            print(f"    - {name}")
    print("-" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
