"""PoW 并发争用的**回归测试**（2026-10-18 修复后）。

背景（修复前的事故）：`_parallel_disabled` 是模块级全局，`_get_executor()`
返回**单例**池。后台预取线程与主路径同时调 `solve()` 时共用同一个
`ProcessPoolExecutor(max_workers=8)`，各 submit 几十个分片，池被瓜分，
主路径等「首个分片返回」超过 `startup_timeout=10s` → 抛
`PowParallelUnavailable` → 被当成「池坏了」→ **本进程永久单进程**，
单次求解从 5 秒变成 40 秒。

修复：`solve()` 的多进程分支加 `_solve_lock` 串行化，并让异常携带
`transient` 标记 —— 偶发超时不再永久降级，只有连续 3 次才认定池坏了。

期望结果（修复后）：
  * [A] 串行两次      → 都 `multi`
  * [B] 并发两次      → 都 `multi`（一个等另一个），总墙钟约为单次 2 倍
  * [C] 事后单跑      → 仍 `multi`，`_parallel_disabled` 保持 `False`

用法：
    cd engine && ./.venv/Scripts/python.exe -X utf8 tools/diag_pow_concurrency.py
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gad_engine import pow as P  # noqa: E402

CH = P.Challenge(
    algorithm="DeepSeekHashV1",
    challenge="a8f7edfeb5c161a8cada44aaf10163272931267ebbd5ac5a085da51c084961d2",
    salt="94ca9d35e51f15b7677a",
    difficulty=144000,
    signature="68440d49b655ed5b500474e79a84f9d8a07e5cda67f8114e75433b87cb84adf8",
    expire_at=1789148689858,
)


def solve_once(tag: str, out: dict) -> None:
    t0 = time.monotonic()
    try:
        got = P.solve(CH, "/api/v0/chat/completion")
        out[tag] = {
            "mode": got.get("_mode"),
            "workers": got.get("_workers"),
            "elapsed_ms": got.get("_elapsed_ms"),
            "wall_ms": round((time.monotonic() - t0) * 1000.0, 1),
        }
    except Exception as exc:  # noqa: BLE001 - 诊断脚本
        out[tag] = {"error": f"{type(exc).__name__}: {exc}",
                    "wall_ms": round((time.monotonic() - t0) * 1000.0, 1)}


def main() -> int:
    print("=" * 72)
    print("  PoW 并发争用诊断")
    print("=" * 72)

    # ---------- A. 串行基线 ----------
    print("\n[A] 串行跑两次（基线）")
    for i in (1, 2):
        out: dict = {}
        solve_once(f"s{i}", out)
        print(f"    第{i}次: {out[f's{i}']}")
    print(f"    此时 _parallel_disabled = {P._parallel_disabled}")

    # ---------- B. 并发跑两次 ----------
    print("\n[B] 两个线程同时跑（复现预取线程 + 主路径）")
    # 上一轮已把池建好（pool_created=False），与生产里的稳定状态一致。
    out = {}
    threads = [
        threading.Thread(target=solve_once, args=(f"t{i}", out), name=f"solver-{i}")
        for i in (1, 2)
    ]
    t0 = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    wall = round((time.monotonic() - t0) * 1000.0, 1)
    for i in (1, 2):
        print(f"    线程{i}: {out.get(f't{i}', {'状态': '超时未返回'})}")
    print(f"    并发总墙钟: {wall}ms")
    print(f"    此时 _parallel_disabled = {P._parallel_disabled}")

    # ---------- C. 事后单跑 ----------
    print("\n[C] 事后单独再跑一次（看是否已被永久降级）")
    out2: dict = {}
    solve_once("after", out2)
    print(f"    {out2.get('after')}")
    print(f"    _parallel_disabled = {P._parallel_disabled}")

    print("\n" + "=" * 72)
    # 判读分两层：先看并发里有没有 degraded，再看事后是否被永久降级。
    #
    # 修复前（2026-10-18 之前）：并发必有一个 degraded，且事后永久单进程。
    # 修复后（`_solve_lock` 串行化 + `transient` 区分）：并发两个都 multi，
    # 事后仍 multi —— 只是**总墙钟翻倍**（一个等另一个），这是串行化的代价，
    # 也是预期行为。
    degraded = any(
        (out.get(f"t{i}") or {}).get("mode") == "degraded" for i in (1, 2)
    )
    after = (out2.get("after") or {})
    permanently_disabled = P._parallel_disabled

    if degraded and permanently_disabled:
        print("  [旧故障] 并发触发降级且永久置位 —— 这是修复前的表现")
        print("           `_solve_lock` 没生效，检查是否被绕过（例如直接调 _parallel_search）")
    elif degraded:
        print("  [降级但未永久] 并发里出现 degraded，但 _parallel_disabled 仍为 False")
        print("                 串行化起效了；degraded 只是这一次的超时，没污染后续")
    elif after.get("mode") == "multi" and not permanently_disabled:
        print("  [修复生效] 并发两次都走 multi，事后仍 multi，未永久降级")
        print(f"             代价：并发总墙钟 {wall}ms（两个求解串行，约为单次的 2 倍）")
    else:
        print("  [异常] 状态不符合任何一种预期，逐行看上面的输出")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
