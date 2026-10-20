"""执行通道重置（restart / cancel）的正确性自测。

守护的核心事故
--------------
`LocalExecutor` 只有**一个** `_done` Event，被三种语义共用：
「命令跑完了」「命令超时了」「执行器被 restart 了」。

`restart()` = `stop()` + `start()`，而 `start()` 里的 `_bootstrap()` 会：
  1. `self._done.clear()`
  2. 写 init 脚本，`Write-Output 'GAD_EXECUTOR_READY'`
  3. `self._done.wait(15)` —— 等 init 跑完
  4. `self._lines.clear()`

于是正在 `_done.wait()` 上阻塞的那条命令会被**别人的**信号唤醒，
`finished=True`、`_collect()` 抢在 `_lines.clear()` 之前拿到
`GAD_EXECUTOR_READY`，最后返回「exit_code=0，命令成功」。
用户看到的是「已取消」，输出却是启动横幅 —— 假成功，而且看起来一切正常。

修法：给执行通道加**代数**（`_epoch`）。`stop()` 时 `_bump_epoch()` 并
`_done.set()`，等待者发现 `self._epoch != my_epoch` 就知道「我的通道没了」，
返回 `reason="channel_reset"` 而不是伪装成成功或超时。

顺带守的另一条：`_read_loop` 必须绑死**自己那个** Popen 对象。
之前它读 `self._proc`，restart 之后旧读线程会看到新进程，把
`_proc_died` 置位污染下一条命令。

用法：
    cd engine && ./.venv/Scripts/python.exe tests/test_executor_restart.py
无 PowerShell 的环境自动跳过（返回 0）。
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gad_engine.config import ExecutorConfig
from gad_engine.executor import LocalExecutor, resolve_shell

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    mark = "通过" if cond else "失败"
    extra = f" —— {detail}" if detail and not cond else ""
    print(f"  [{mark}] {name}{extra}")


def _skip(msg: str) -> int:
    print(f"  [跳过] {msg}")
    return 0


def main() -> int:
    print("=" * 70)
    print("执行通道重置（restart / cancel）自测")
    print("=" * 70)

    try:
        resolve_shell("")
    except FileNotFoundError as exc:
        return _skip(f"本机没有 PowerShell：{exc}")

    cfg = ExecutorConfig(work_dir=str(Path.home()))

    # ---------- 1. 基本可用性（后面几项的前提） ----------
    print("\n[1] 基线：能跑通一条普通命令")
    ex = LocalExecutor(cfg)
    try:
        r = ex.run("Write-Output 'BASELINE-OK'")
        check("基线命令成功", r.ok and "BASELINE-OK" in r.output, f"{r.exit_code} {r.output[:120]!r}")
        check("基线没有误报 proc_died", r.reason != "proc_died", r.reason)
    finally:
        ex.stop()

    # ---------- 2. restart 中止正在跑的命令：不得伪装成成功 ----------
    #
    # 这是本轮修的 bug。判据三条，缺一不可：
    #   a) 不能报 exit_code=0 —— 那是假成功；
    #   b) 输出里**不得**出现 GAD_EXECUTOR_READY —— 那是启动横幅，不是命令输出；
    #   c) reason 必须是 channel_reset，让人一眼看出不是超时。
    print("\n[2] restart 中止在跑的命令（cancel 走的就是这条路）")
    ex = LocalExecutor(cfg)
    result_box: dict[str, object] = {}

    def _runner() -> None:
        result_box["r"] = ex.run("Start-Sleep -Seconds 30; Write-Output 'SHOULD-NOT-APPEAR'")

    th = threading.Thread(target=_runner, name="test-run", daemon=True)
    try:
        th.start()
        time.sleep(0.6)          # 让命令真正进到 Start-Sleep
        t0 = time.monotonic()
        ex.restart()             # 等价于 job:cancel 触发的 on_cancel
        th.join(timeout=8.0)
        elapsed = time.monotonic() - t0

        check("被中止的命令线程能返回（没有卡到 30 秒）",
              not th.is_alive() and elapsed < 8.0, f"存活={th.is_alive()} 耗时={elapsed:.2f}s")

        r = result_box.get("r")
        check("拿到结果对象", r is not None, str(result_box))
        if r is not None:
            check("不得报成成功（exit_code != 0）", r.exit_code != 0, f"exit_code={r.exit_code}")
            check("不得把启动横幅当输出",
                  "GAD_EXECUTOR_READY" not in r.output,
                  f"输出={r.output[:200]!r}")
            check("reason 标为 channel_reset（不是 timeout）",
                  r.reason == "channel_reset", f"reason={r.reason!r}")
            check("不是 timeout 标志", r.timed_out is False, str(r.timed_out))
    finally:
        ex.stop()

    # ---------- 3. restart 之后执行器仍可用 ----------
    print("\n[3] restart 之后执行器仍可用")
    ex = LocalExecutor(cfg)
    try:
        ex.run("Write-Output 'BEFORE'")
        ex.restart()
        r = ex.run("Write-Output 'AFTER-RESTART'")
        check("restart 后新命令成功", r.ok and "AFTER-RESTART" in r.output,
              f"{r.exit_code} {r.output[:120]!r}")
        check("新命令输出不含旧命令内容", "BEFORE" not in r.output, r.output[:120])
    finally:
        ex.stop()

    # ---------- 4. 超时分支同样不得拿到启动横幅 ----------
    #
    # 超时路径原来是 `self.restart()` 之后才 `self._collect()` ——
    # 而 restart 里的 bootstrap 已经把 `_lines` 清空并写了 READY。
    print("\n[4] 命令超时：部分输出必须是命令自己的，不是启动横幅")
    ex = LocalExecutor(cfg)
    try:
        r = ex.run(
            "Write-Output 'PARTIAL-BEFORE-TIMEOUT'; Start-Sleep -Seconds 30",
            timeout_ms=1200,
        )
        check("超时被识别", r.timed_out is True and r.reason == "timeout",
              f"timed_out={r.timed_out} reason={r.reason!r}")
        check("超时前已产生的输出被保留",
              "PARTIAL-BEFORE-TIMEOUT" in r.output, f"输出={r.output[:200]!r}")
        check("超时输出不含启动横幅",
              "GAD_EXECUTOR_READY" not in r.output, f"输出={r.output[:200]!r}")
    finally:
        ex.stop()

    # ---------- 5. 连续 restart 不泄漏 / 不串味 ----------
    print("\n[5] 连续 restart（模拟反复 cancel）")
    ex = LocalExecutor(cfg)
    try:
        ok = True
        detail = ""
        for i in range(3):
            r = ex.run(f"Write-Output 'ROUND-{i}'")
            if not (r.ok and f"ROUND-{i}" in r.output):
                ok = False
                detail = f"第 {i} 轮：{r.exit_code} {r.output[:80]!r}"
                break
            ex.restart()
        check("三轮 restart 后仍能正确执行", ok, detail)
        check("epoch 随 restart 递增（3 次 restart → 至少 3）",
              ex._epoch >= 3, f"_epoch={ex._epoch}")
    finally:
        ex.stop()

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
