"""job 工具的执行层接线：ToolCall -> AgentRuntime._run_job。

用假的 executor 造实例（只提供 jobs 需要的 run/restart），避免拉起真实进程。

第 [7] 段是**真进程**测试：`job:cancel` 走真实 `LocalExecutor.restart`。
这条链路曾经有一个隐蔽的假成功 —— cancel 之后 `job.output` 是
`GAD_EXECUTOR_READY`（执行器启动横幅），状态却是 cancelled。见
`test_executor_restart.py` 的模块头。无 PowerShell 的环境自动跳过这一段。

用法：
    cd engine && ./.venv/Scripts/python.exe tests/test_job_exec.py
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gad_engine.agent import AgentRuntime
from gad_engine.config import ExecutorConfig
from gad_engine.executor import LocalExecutor, resolve_shell
from gad_engine.jobs import JobManager
from gad_engine.protocol import ToolCall, ToolKind

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
    else:
        FAIL.append(name + (f" — {detail}" if detail else ""))


class _FakeResult:
    def __init__(self, code=0, out=""):
        self.exit_code = code
        self.output = out


class _FakeExecutor:
    """只实现 jobs / _run_async 需要的方法。"""

    def __init__(self):
        self.restart_calls = 0

    def run(self, command: str):
        time.sleep(0.05)
        return _FakeResult(0, f"ran: {command}")

    def restart(self):
        self.restart_calls += 1


def _wait_done(rt, jid, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        job = rt.jobs.get(jid)
        if job and job.finished:
            return job
        time.sleep(0.01)
    return rt.jobs.get(jid)


async def run_checks() -> None:
    rt = AgentRuntime.__new__(AgentRuntime)
    rt.executor = _FakeExecutor()
    rt.jobs = JobManager()

    # ---------- 1. _run_async 返回作业号 ----------
    print("\n[1] _run_async 返回作业号")
    ex = await rt._run_async("echo hello")
    check("kind 正确", ex.kind == "local_cmd:async", ex.kind)
    check("输出含 job-", "job-" in ex.output, ex.output[:120])
    jid = ex.output.split("作业号 ")[1].split("。")[0]
    check("能提取 id", jid.startswith("job-"), jid)

    job = _wait_done(rt, jid)
    check("作业完成", job and job.finished, job.status if job else "None")
    check("捕获输出", job and "ran: echo hello" in job.output, job.output[:120] if job else "")

    # ---------- 2. job:list ----------
    print("\n[2] job:list")
    call = ToolCall(kind=ToolKind.JOB, target="list", payload="")
    ex, _text, _attach = await rt._execute(call)  # type: ignore[misc]
    check("exit_code=0", ex.exit_code == 0, ex.output[:120])
    check("列出作业", jid in ex.output, ex.output[:200])
    check("含状态词", "done" in ex.output, ex.output[:200])

    # ---------- 3. job:status ----------
    print("\n[3] job:status")
    call = ToolCall(kind=ToolKind.JOB, target=f"status {jid}", payload="")
    ex, _text, _attach = await rt._execute(call)  # type: ignore[misc]
    check("exit_code=0", ex.exit_code == 0, ex.output[:120])
    check("含作业号", jid in ex.output, ex.output[:200])
    check("含输出内容", "ran: echo hello" in ex.output, ex.output[:300])

    # 默认子命令 = list
    call = ToolCall(kind=ToolKind.JOB, target="", payload="")
    ex, _text, _attach = await rt._execute(call)  # type: ignore[misc]
    check("空子命令默认 list", "共" in ex.output and "作业" in ex.output, ex.output[:120])

    # ---------- 4. job:status 不存在 ----------
    print("\n[4] job:status 不存在")
    call = ToolCall(kind=ToolKind.JOB, target="status job-nope", payload="")
    ex, _text, _attach = await rt._execute(call)  # type: ignore[misc]
    check("exit_code=1", ex.exit_code == 1, str(ex.exit_code))
    check("提示没有", "没有这个作业" in ex.output, ex.output[:120])

    # status 缺参数
    call = ToolCall(kind=ToolKind.JOB, target="status", payload="")
    ex, _text, _attach = await rt._execute(call)  # type: ignore[misc]
    check("缺参数报错", ex.exit_code == 1 and "缺少" in ex.output, ex.output[:120])

    # ---------- 5. job:cancel ----------
    print("\n[5] job:cancel")
    # 起一个慢作业
    ex_slow = await rt._run_async("slow-command")
    jid_slow = ex_slow.output.split("作业号 ")[1].split("。")[0]
    call = ToolCall(kind=ToolKind.JOB, target=f"cancel {jid_slow}", payload="")
    ex, _text, _attach = await rt._execute(call)  # type: ignore[misc]
    check("cancel exit_code=0", ex.exit_code == 0, ex.output[:160])
    check("调用了 restart", rt.executor.restart_calls >= 1, str(rt.executor.restart_calls))

    # 取消不存在的
    call = ToolCall(kind=ToolKind.JOB, target="cancel job-nope", payload="")
    ex, _text, _attach = await rt._execute(call)  # type: ignore[misc]
    check("取消不存在 exit_code=1", ex.exit_code == 1, ex.output[:120])

    # ---------- 6. 未知子命令 ----------
    print("\n[6] 未知子命令")
    call = ToolCall(kind=ToolKind.JOB, target="frobnicate", payload="")
    ex, _text, _attach = await rt._execute(call)  # type: ignore[misc]
    check("未知子命令报错", ex.exit_code == 1 and "未知子命令" in ex.output, ex.output[:160])


async def run_live_cancel_check() -> None:
    """[7] 真进程：cancel 之后 job.output 不得是执行器启动横幅。

    这一段之所以必须用真 executor，是因为 bug 只在**真 restart** 时出现：
    假 executor 的 restart 只是把计数器 +1，根本不会重新 bootstrap。
    """
    print("\n[7] 真 executor · cancel 不得污染 job.output")
    try:
        resolve_shell("")
    except FileNotFoundError as exc:
        print(f"  [跳过] 本机没有 PowerShell：{exc}")
        return

    rt = AgentRuntime.__new__(AgentRuntime)
    rt.executor = LocalExecutor(ExecutorConfig(work_dir=str(Path.home())))
    rt.jobs = JobManager()
    rt._screen_command = lambda cmd: None  # type: ignore[method-assign]

    try:
        ex = await rt._run_async("Start-Sleep -Seconds 30; Write-Output 'SHOULD-NOT-APPEAR'")
        jid = ex.output.split("作业号 ")[1].split("。")[0]
        time.sleep(0.6)   # 让命令真正进到 Start-Sleep

        call = ToolCall(kind=ToolKind.JOB, target=f"cancel {jid}", payload="")
        ex_c, _t, _a = await rt._execute(call)  # type: ignore[misc]
        check("cancel 受理", ex_c.exit_code == 0, ex_c.output[:160])

        job = _wait_done(rt, jid, timeout=10.0)
        check("作业终态是 cancelled", job is not None and job.status == "cancelled",
              job.status if job else "None")
        if job is not None:
            check("输出不含执行器启动横幅",
                  "GAD_EXECUTOR_READY" not in job.output,
                  f"输出={job.output[:200]!r}")
            check("输出明确说明被中止",
                  "中止" in job.output or "重置" in job.output,
                  f"输出={job.output[:200]!r}")
            check("没有跑到 30 秒（真被中断了）", job.duration_ms < 15000,
                  f"{job.duration_ms}ms")

        # cancel 之后执行器还能用 —— 这是 restart 的核心契约。
        ex2 = await rt._run_async("Write-Output 'AFTER-CANCEL-OK'")
        jid2 = ex2.output.split("作业号 ")[1].split("。")[0]
        job2 = _wait_done(rt, jid2, timeout=15.0)
        check("cancel 之后 executor 仍可用",
              job2 is not None and job2.status == "done" and "AFTER-CANCEL-OK" in job2.output,
              f"{job2.status if job2 else 'None'} {job2.output[:120] if job2 else ''}")
    finally:
        rt.executor.stop()


def main() -> int:
    async def _all() -> None:
        await run_checks()
        await run_live_cancel_check()

    asyncio.run(_all())
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
