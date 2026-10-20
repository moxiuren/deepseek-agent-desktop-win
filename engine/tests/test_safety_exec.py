"""安全护栏的执行层接线：危险命令 / 路径逃逸真的被拦下。

验证 agent._run_command 与 _write_file 里的安全闸门是否生效 ——
即「拦下的命令不会被执行」「拦下的写入不会落盘」。

用假 executor，避免真跑命令。

用法：
    cd engine && ./.venv/Scripts/python.exe tests/test_safety_exec.py
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gad_engine.agent import AgentRuntime

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
        self.rejected = False


class _FakeExecutor:
    def __init__(self, work_dir: Path):
        self._work_dir = str(work_dir)
        self.commands: list[str] = []

    def snapshot(self):
        return {"work_dir": self._work_dir, "shell": "pwsh.exe"}

    def run(self, command: str):
        self.commands.append(command)
        return _FakeResult(0, f"ran: {command}")

    def restart(self):
        pass


async def run_checks(tmp: Path) -> None:
    work = tmp / "work"
    work.mkdir()

    rt = AgentRuntime.__new__(AgentRuntime)
    from gad_engine.jobs import JobManager
    rt.jobs = JobManager()
    rt.executor = _FakeExecutor(work)

    # ---------- 1. 危险命令被拦（未执行）----------
    print("\n[1] 危险命令被拦")
    ex = await rt._run_command("format C:")
    check("exit_code=1", ex.exit_code == 1, str(ex.exit_code))
    check("rejected=True", ex.rejected is True, str(ex.rejected))
    check("命令未被真正执行", rt.executor.commands == [], str(rt.executor.commands))
    check("理由含不可执行", "不可执行" in ex.output, ex.output[:120])

    # ---------- 2. WARN 档默认拦 ----------
    print("\n[2] WARN 档默认拦")
    ex = await rt._run_command("Remove-Item C:/x -Recurse")
    check("递归删除被拦", ex.rejected is True, str(ex.rejected))
    check("未执行", rt.executor.commands == [], str(rt.executor.commands))
    check("提示豁免标记", "#danger-ok" in ex.output, ex.output[:160])

    # ---------- 3. 豁免标记放行 ----------
    print("\n[3] 豁免标记放行")
    ex = await rt._run_command("Remove-Item C:/x -Recurse #danger-ok")
    check("豁免后执行", "ran:" in ex.output, ex.output[:120])
    check("命令进了执行器", len(rt.executor.commands) == 1, str(rt.executor.commands))

    # ---------- 4. 正常命令放行 ----------
    print("\n[4] 正常命令放行")
    rt.executor.commands.clear()
    ex = await rt._run_command("Get-Date")
    check("正常命令执行", ex.exit_code == 0 and "ran:" in ex.output, ex.output[:120])
    check("进了执行器", rt.executor.commands == ["Get-Date"], str(rt.executor.commands))

    # ---------- 5. 写文件路径逃逸 ----------
    print("\n[5] 写文件路径逃逸")
    outside = tmp / "outside"
    outside.mkdir()
    target = outside / "evil.txt"
    ex, note = rt._write_file(str(target), "pwned")
    check("exit_code=1", ex.exit_code == 1, str(ex.exit_code))
    check("rejected=True", ex.rejected is True, str(ex.rejected))
    check("文件未落盘", not target.exists(), str(target))
    check("说明含拒绝", "拒绝" in note, note[:120])

    # ---------- 6. 工作目录内写入放行 ----------
    print("\n[6] 工作目录内写入")
    ok_target = work / "sub" / "good.txt"
    ex, note = rt._write_file(str(ok_target), "hello")
    check("exit_code=0", ex.exit_code == 0, ex.output[:120])
    check("文件已写入", ok_target.exists() and ok_target.read_text(encoding="utf-8") == "hello")
    check("自动建父目录", ok_target.parent.is_dir())

    # ---------- 7. 异步泳道也要过闸门 ----------
    print("\n[7] 异步泳道闸门")
    rt.executor.commands.clear()
    ex = await rt._run_async("format C:")
    check("异步危险命令被拦", ex.rejected is True, str(ex.rejected))
    check("未进 job 表", len(rt.jobs.list_jobs()) == 0, str(len(rt.jobs.list_jobs())))
    check("kind 为 async", ex.kind == "local_cmd:async", ex.kind)

    # 正常异步命令放行
    ex2 = await rt._run_async("echo hello")
    check("正常异步放行", ex2.exit_code == 0 and "job-" in ex2.output, ex2.output[:120])

    # ---------- 8. 前缀相同但非子目录 ----------
    print("\n[7] 前缀陷阱")
    evil = Path(str(work) + "-evil")
    evil.mkdir()
    ex, note = rt._write_file(str(evil / "x.txt"), "pwned")
    check("同前缀非子目录被拦", ex.rejected is True, str(ex.rejected))
    check("未落盘", not (evil / "x.txt").exists())


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="gad-safetyexec-"))
    try:
        asyncio.run(run_checks(tmp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

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
