"""run_tests 执行层接线：ToolCall -> AgentRuntime._execute。

用假 executor（提供 snapshot / run），不真跑测试进程。

用法：
    cd engine && ./.venv/Scripts/python.exe tests/test_tests_exec.py
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
from gad_engine.protocol import ToolCall, ToolKind

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
    else:
        FAIL.append(name + (f" — {detail}" if detail else ""))


class _FakeResult:
    def __init__(self, code, out):
        self.exit_code = code
        self.output = out


class _FakeExecutor:
    def __init__(self, work_dir: Path, code=0, out="3 passed"):
        self._work_dir = str(work_dir)
        self._code = code
        self._out = out
        self.commands: list[str] = []

    def snapshot(self):
        return {"work_dir": self._work_dir, "shell": "pwsh.exe"}

    def run(self, command: str):
        self.commands.append(command)
        return _FakeResult(self._code, self._out)


async def run_checks(tmp: Path) -> None:
    # 造一个 pytest 项目
    proj = tmp / "proj"
    (proj / "tests").mkdir(parents=True)
    (proj / "tests" / "test_x.py").write_text("def test_a():\n    assert 1\n", encoding="utf-8")

    rt = AgentRuntime.__new__(AgentRuntime)

    # ---------- 1. 识别 + 执行 ----------
    print("\n[1] 识别并执行")
    rt.executor = _FakeExecutor(proj, 0, "3 passed")
    call = ToolCall(kind=ToolKind.RUN_TESTS, target="", payload="")
    ex, _text, _attach = await rt._execute(call)  # type: ignore[misc]
    check("exit_code=0", ex.exit_code == 0, ex.output[:120])
    check("kind 正确", ex.kind == "run_tests", ex.kind)
    check("识别为 pytest", "pytest" in ex.output, ex.output[:160])
    check("执行了命令", rt.executor.commands and "pytest" in rt.executor.commands[0],
          str(rt.executor.commands))
    check("输出含通过", "3 passed" in ex.output, ex.output[:200])

    # ---------- 2. 测试失败 ----------
    print("\n[2] 测试失败")
    rt.executor = _FakeExecutor(proj, 1, "2 failed")
    call = ToolCall(kind=ToolKind.RUN_TESTS, target="", payload="")
    ex, _text, _attach = await rt._execute(call)  # type: ignore[misc]
    check("失败时 exit_code=1", ex.exit_code == 1, str(ex.exit_code))
    check("文本含失败", "失败" in ex.output, ex.output[:120])

    # ---------- 3. cmd= 显式指定 ----------
    print("\n[3] cmd= 显式指定")
    rt.executor = _FakeExecutor(proj, 0, "ok")
    call = ToolCall(kind=ToolKind.RUN_TESTS, target="cmd=npm run check", payload="")
    ex, _text, _attach = await rt._execute(call)  # type: ignore[misc]
    check("用了指定命令", rt.executor.commands == ["npm run check"], str(rt.executor.commands))
    check("类型 explicit", "explicit" in ex.output, ex.output[:160])

    # ---------- 4. 无法识别 ----------
    print("\n[4] 无法识别")
    empty = tmp / "empty"
    empty.mkdir()
    rt.executor = _FakeExecutor(empty, 0, "")
    call = ToolCall(kind=ToolKind.RUN_TESTS, target="", payload="")
    ex, _text, _attach = await rt._execute(call)  # type: ignore[misc]
    check("无法识别 exit_code=1", ex.exit_code == 1, str(ex.exit_code))
    check("提示无法识别", "无法识别" in ex.output, ex.output[:160])
    check("未执行命令", rt.executor.commands == [], str(rt.executor.commands))

    # ---------- 5. path= 指定 ----------
    print("\n[5] path= 指定目录")
    other = tmp / "other"
    (other / "tests").mkdir(parents=True)
    # 空 tests 目录在新逻辑下不判 pytest —— 放个真用例
    (other / "tests" / "test_b.py").write_text(
        "import pytest\n\ndef test_y():\n    assert True\n", encoding="utf-8"
    )
    rt.executor = _FakeExecutor(tmp / "unrelated", 0, "1 passed")
    call = ToolCall(kind=ToolKind.RUN_TESTS, target=f"path={other}", payload="")
    ex, _text, _attach = await rt._execute(call)  # type: ignore[misc]
    check("用 path 指定的项目", ex.exit_code == 0 and "pytest" in ex.output, ex.output[:160])


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="gad-testsexec-"))
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
