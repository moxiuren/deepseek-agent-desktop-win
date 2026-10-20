"""后台作业管理（`jobs.py`）自测。

用法：
    cd engine && ./.venv/Scripts/python.exe tests/test_jobs.py
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gad_engine import jobs as J

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
    else:
        FAIL.append(name + (f" — {detail}" if detail else ""))


class _Result:
    def __init__(self, exit_code=0, output=""):
        self.exit_code = exit_code
        self.output = output


def _wait_until(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


def main() -> int:
    # ---------- 1. 提交与完成 ----------
    print("\n[1] 提交与完成")
    m = J.JobManager()
    jid = m.submit("echo hi", lambda c: _Result(0, "hi"))
    check("返回 id", jid.startswith("job-"), jid)
    _wait_until(lambda: (m.get(jid) or J.Job("", "")).finished)
    job = m.get(jid)
    check("状态 done", job and job.status == J.DONE, job.status if job else "None")
    check("输出捕获", job and job.output == "hi", job.output if job else "")
    check("退出码", job and job.exit_code == 0)
    check("有耗时", job and job.duration_ms >= 0, str(job.duration_ms if job else None))

    # ---------- 2. 失败退出码 ----------
    print("\n[2] 失败退出码")
    jid2 = m.submit("fail", lambda c: _Result(1, "boom"))
    _wait_until(lambda: (m.get(jid2) or J.Job("", "")).finished)
    check("状态 failed", m.get(jid2).status == J.FAILED, m.get(jid2).status)

    # ---------- 3. runner 抛异常 ----------
    print("\n[3] runner 抛异常")
    def _boom(cmd):
        raise RuntimeError("炸了")
    jid3 = m.submit("boom", _boom)
    _wait_until(lambda: (m.get(jid3) or J.Job("", "")).finished)
    j3 = m.get(jid3)
    check("状态 failed", j3.status == J.FAILED, j3.status)
    check("错误被捕获", "炸了" in j3.error, j3.error)

    # ---------- 4. list ----------
    print("\n[4] list")
    items = m.list_jobs()
    check("列出 3 个", len(items) == 3, str(len(items)))
    check("按时间倒序", items[0].id == jid3, items[0].id)

    # ---------- 5. 输出截断 ----------
    print("\n[5] 输出截断")
    m5 = J.JobManager()
    big = "x" * (J.MAX_JOB_OUTPUT + 5000)
    jid5 = m5.submit("big", lambda c: _Result(0, big))
    _wait_until(lambda: (m5.get(jid5) or J.Job("", "")).finished)
    check("输出被截断", len(m5.get(jid5).output) == J.MAX_JOB_OUTPUT, str(len(m5.get(jid5).output)))

    # ---------- 6. 取消（排队中）----------
    print("\n[6] 取消")
    m6 = J.JobManager()
    gate = threading.Event()
    def _slow(cmd):
        gate.wait(2.0)
        return _Result(0, "done")
    jid6 = m6.submit("slow", _slow)
    ok, msg = m6.cancel(jid6)
    check("取消受理", ok, msg)
    check("说明提到状态确认", "job:status" in msg or "最终状态" in msg, msg)
    gate.set()
    _wait_until(lambda: (m6.get(jid6) or J.Job("", "")).finished)
    check("最终为 cancelled", m6.get(jid6).status == J.CANCELLED, m6.get(jid6).status)

    # ---------- 7. 取消已完成的 ----------
    print("\n[7] 取消已完成")
    ok, msg = m6.cancel(jid6)
    check("已完成拒绝取消", not ok and "已结束" in msg, msg)

    # ---------- 8. 取消不存在的 ----------
    print("\n[8] 取消不存在")
    ok, msg = m6.cancel("job-nope")
    check("不存在报错", not ok and "没有这个作业" in msg, msg)

    # ---------- 9. on_cancel 回调 ----------
    print("\n[9] on_cancel 回调")
    m9 = J.JobManager()
    called = []
    gate9 = threading.Event()
    jid9 = m9.submit("s", lambda c: (gate9.wait(2.0), _Result(0, ""))[1])
    ok, msg = m9.cancel(jid9, on_cancel=lambda: called.append(1))
    check("on_cancel 被调用", called == [1], str(called))
    check("说明提到中断通道", "中断执行通道" in msg, msg)
    gate9.set()

    # ---------- 10. 驱逐 ----------
    print("\n[10] 驱逐")
    m10 = J.JobManager(max_jobs=3)
    ids = []
    for i in range(6):
        jid = m10.submit(f"c{i}", lambda c: _Result(0, "x"))
        ids.append(jid)
        _wait_until(lambda: (m10.get(jid) or J.Job("", "")).finished)
    check("数量不超上限", len(m10.list_jobs()) <= 3, str(len(m10.list_jobs())))
    check("最新保留", m10.get(ids[-1]) is not None)

    # ---------- 11. snapshot ----------
    print("\n[11] snapshot")
    m.list_jobs() and J.JobManager()
    snap2 = m.snapshot()
    check("snapshot 有 total", "total" in snap2, str(snap2))
    check("snapshot 有 running", "running" in snap2, str(snap2))

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
