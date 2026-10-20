"""后台作业管理 —— 让 `local_cmd:async` 从「发射后不管」变成「可查询」。

现在的问题
----------
`agent._run_async` 把命令丢进线程池就返回一句「已在后台运行」。之后：
  * 拿不到输出
  * 不知道跑完没有
  * 无法终止

对「跑个测试」「起个服务」这类命令，这是不可用的 —— 模型只能盲目等待。

本层做什么
----------
给每个后台命令一个 **id**，记录它的 状态 / 输出 / 退出码 / 起止时间，
并暴露 list / status / cancel。模型据此可以：
  1. 起一条长命令
  2. 干别的
  3. 回来 `job:status <id>` 看结果

线程模型
--------
每个 job 一个 daemon 线程跑 `runner(command)`。runner 是可注入的 ——
测试里塞假 runner，生产里塞 `executor.run`。JobManager 不碰 executor 内部，
只通过 runner 和可选的 on_cancel 回调与它交互。

**cancel 的诚实边界**：能真正中断的只有「还没轮到执行」的 job（排队中）。
已经在跑的命令，本层只能标记为 cancelled，能否真停取决于 on_cancel 回调 ——
对 executor 而言那是 restart（会丢失会话变量）。所以 cancel 的返回值里
必须说清「做了什么、没做什么」。
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

#: 单个 job 保留的输出上限（字符）。命令输出可能极大，只留头部。
MAX_JOB_OUTPUT = 8000
#: 最多同时保留多少个 job 记录（超出丢弃最旧的**已完成**项）。
MAX_JOBS = 50

#: 状态常量
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"

_TERMINAL = frozenset({DONE, FAILED, CANCELLED})


@dataclass
class Job:
    id: str
    command: str
    status: str = RUNNING
    output: str = ""
    exit_code: int | None = None
    error: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    cancelled_requested: bool = False

    @property
    def finished(self) -> bool:
        return self.status in _TERMINAL

    @property
    def duration_ms(self) -> float:
        end = self.finished_at or time.time()
        return round((end - self.started_at) * 1000.0, 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "command": self.command,
            "status": self.status,
            "exit_code": self.exit_code,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "output": self.output,
        }


class JobManager:
    """后台作业表。线程安全（内部一把锁）。"""

    def __init__(self, max_jobs: int = MAX_JOBS) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._max_jobs = max(1, max_jobs)

    # ---------- 提交 ----------

    def submit(
        self,
        command: str,
        runner: Callable[[str], Any],
        on_cancel: Callable[[], None] | None = None,
    ) -> str:
        """起一个后台作业，返回 job id。

        `runner(command)` 必须是同步阻塞的（在独立线程里跑），返回值需有
        `exit_code` / `output` 属性（即 executor 的 CommandResult 形态）。
        """
        jid = "job-" + uuid.uuid4().hex[:8]
        job = Job(id=jid, command=command)
        with self._lock:
            self._jobs[jid] = job
            self._evict()

        def _work() -> None:
            # 运行前再看一次：可能刚被 cancel
            with self._lock:
                if job.cancelled_requested:
                    job.status = CANCELLED
                    job.finished_at = time.time()
                    job.output = "(已取消，未执行)"
                    return
            try:
                result = runner(command)
                out = str(getattr(result, "output", "") or "")
                code = getattr(result, "exit_code", None)
                with self._lock:
                    if job.cancelled_requested:
                        job.status = CANCELLED
                        job.output = out[:MAX_JOB_OUTPUT]
                        job.exit_code = code
                    else:
                        job.status = DONE if (code in (0, None)) else FAILED
                        job.output = out[:MAX_JOB_OUTPUT]
                        job.exit_code = code
                    job.finished_at = time.time()
            except BaseException as exc:  # noqa: BLE001 - 后台线程漏异常会静默丢
                with self._lock:
                    job.status = FAILED
                    job.error = f"{type(exc).__name__}: {exc}"
                    job.finished_at = time.time()

        threading.Thread(target=_work, name=f"gad-{jid}", daemon=True).start()
        return jid

    def _evict(self) -> None:
        """超过上限时丢弃最旧的**已完成**作业。调用方须持锁。"""
        if len(self._jobs) <= self._max_jobs:
            return
        finished = sorted(
            (j for j in self._jobs.values() if j.finished),
            key=lambda j: j.finished_at,
        )
        for j in finished:
            if len(self._jobs) <= self._max_jobs:
                break
            self._jobs.pop(j.id, None)

    # ---------- 查询 ----------

    def get(self, jid: str) -> Job | None:
        with self._lock:
            return self._jobs.get(jid)

    def list_jobs(self) -> list[Job]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.started_at, reverse=True)

    # ---------- 取消 ----------

    def cancel(
        self, jid: str, on_cancel: Callable[[], None] | None = None
    ) -> tuple[bool, str]:
        """请求取消。

        返回 (是否受理, 说明)。说明**必须**讲清做了什么 —— 见模块头
        「cancel 的诚实边界」。
        """
        with self._lock:
            job = self._jobs.get(jid)
            if job is None:
                return False, f"没有这个作业：{jid}"
            if job.finished:
                return False, f"作业 {jid} 已结束（{job.status}），无需取消"
            job.cancelled_requested = True

        # 已经在跑的命令：能否真停取决于 on_cancel。本层不假装能停。
        if on_cancel is not None:
            try:
                on_cancel()
                return True, (
                    f"已请求取消 {jid}。命令可能仍在执行 —— 已尝试中断执行通道，"
                    "代价是常驻会话的变量会丢失。用 job:status 确认最终状态。"
                )
            except Exception as exc:  # noqa: BLE001
                return True, f"已标记取消 {jid}，但中断执行通道失败：{exc}"

        return True, (
            f"已标记取消 {jid}。该命令若已在执行，可能仍会跑完 —— "
            "job:status 会显示最终状态。"
        )

    # ---------- 状态 ----------

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            jobs = list(self._jobs.values())
        running = sum(1 for j in jobs if j.status == RUNNING)
        return {"total": len(jobs), "running": running}
