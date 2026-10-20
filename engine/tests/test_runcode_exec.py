"""run_code 执行层接线：ToolCall -> AgentRuntime._run_code。

重点验证参数解析（timeout= / memory=）—— 这里踩过 split_path_args 的坑。

用法：
    cd engine && ./.venv/Scripts/python.exe tests/test_runcode_exec.py
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


class _FakeExecutor:
    def __init__(self, wd: Path):
        self._wd = str(wd)

    def snapshot(self):
        return {"work_dir": self._wd, "shell": "pwsh.exe"}


async def run_checks(tmp: Path) -> None:
    work = tmp / "work"
    work.mkdir()
    rt = AgentRuntime.__new__(AgentRuntime)
    rt.executor = _FakeExecutor(work)

    # ---------- 1. 基本 ----------
    print("\n[1] 基本")
    call = ToolCall(kind=ToolKind.RUN_CODE, target="", payload="print('hi from code')")
    ex, text, _attach = await rt._execute(call)  # type: ignore[misc]
    check("exit_code=0", ex.exit_code == 0, ex.output[:120])
    check("kind 正确", ex.kind == "run_code", ex.kind)
    check("输出正确", "hi from code" in ex.output, ex.output[:160])
    check("回传含沙箱说明", "沙箱" in text, text[:200])

    # ---------- 2. 参数解析（踩坑点）----------
    print("\n[2] 参数解析")
    # timeout=2，脚本 sleep 30 → 应超时
    call = ToolCall(kind=ToolKind.RUN_CODE, target="timeout=2", payload="import time; time.sleep(30)")
    ex, text, _attach = await rt._execute(call)  # type: ignore[misc]
    check("timeout 生效（超时）", ex.exit_code == 124, f"code={ex.exit_code}")
    check("文本含超时", "超时" in text, text[:160])

    # ---------- 3. 沙箱限制在 run_code 里生效 ----------
    print("\n[3] 沙箱限制")
    evil = tmp / "evil.txt"
    call = ToolCall(
        kind=ToolKind.RUN_CODE, target="",
        payload=f"open(r'{evil}', 'w').write('x')",
    )
    ex, _text, _attach = await rt._execute(call)  # type: ignore[misc]
    check("范围外写被拒", ex.exit_code != 0, f"code={ex.exit_code}")
    check("evil 未创建", not evil.exists())

    # ---------- 4. 异常传播 ----------
    print("\n[4] 异常")
    call = ToolCall(kind=ToolKind.RUN_CODE, target="", payload="raise ValueError('boom')")
    ex, text, _attach = await rt._execute(call)  # type: ignore[misc]
    check("异常非零退出", ex.exit_code != 0, str(ex.exit_code))
    check("输出含异常", "boom" in ex.output, ex.output[:160])


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="gad-runcode-"))
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
