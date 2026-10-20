"""multi_edit / apply_patch 执行层接线：ToolCall -> AgentRuntime._execute。

重点验证 apply_patch 的 **work_dir 注入**（它相对工作目录，不是进程 cwd）。

用法：
    cd engine && ./.venv/Scripts/python.exe tests/test_patch_exec.py
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

from gad_engine import tools_fs as TF
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
    def __init__(self, work_dir: Path):
        self._wd = str(work_dir)

    def snapshot(self):
        return {"work_dir": self._wd, "shell": "pwsh.exe"}


def block(old: str, new: str) -> str:
    return f"{TF.MARK_OLD}\n{old}\n{TF.MARK_SEP}\n{new}\n{TF.MARK_NEW}"


async def run_checks(tmp: Path) -> None:
    work = tmp / "work"
    work.mkdir()
    rt = AgentRuntime.__new__(AgentRuntime)
    rt.executor = _FakeExecutor(work)

    # ---------- 1. multi_edit 分发 ----------
    print("\n[1] multi_edit 分发")
    f = work / "m.txt"
    f.write_text("a\nb\nc\n", encoding="utf-8")
    body = block("a", "A") + "\n" + "===== EDIT =====" + "\n" + block("c", "C")
    call = ToolCall(kind=ToolKind.MULTI_EDIT, target=str(f), payload=body)
    ex, _text, _attach = await rt._execute(call)  # type: ignore[misc]
    check("exit_code=0", ex.exit_code == 0, ex.output[:120])
    check("kind 正确", ex.kind == "multi_edit", ex.kind)
    check("文件已改", f.read_text(encoding="utf-8") == "A\nb\nC\n",
          repr(f.read_text(encoding="utf-8")))

    # ---------- 2. multi_edit 失败原子性 ----------
    print("\n[2] multi_edit 原子性")
    f2 = work / "m2.txt"
    f2.write_text("x\ny\n", encoding="utf-8")
    body2 = block("x", "X") + "\n" + "===== EDIT =====" + "\n" + block("nope", "N")
    call = ToolCall(kind=ToolKind.MULTI_EDIT, target=str(f2), payload=body2)
    ex, _text, _attach = await rt._execute(call)  # type: ignore[misc]
    check("失败 exit_code=1", ex.exit_code == 1, str(ex.exit_code))
    check("文件未动", f2.read_text(encoding="utf-8") == "x\ny\n")

    # ---------- 3. apply_patch 注入 work_dir ----------
    print("\n[3] apply_patch work_dir 注入")
    # 注意：target 里**不带** root=，应被自动注入 work_dir
    f3 = work / "p.txt"
    f3.write_text("old\n", encoding="utf-8")
    diff = "--- a/p.txt\n+++ b/p.txt\n@@ -1 +1 @@\n-old\n+new\n"
    call = ToolCall(kind=ToolKind.APPLY_PATCH, target="", payload=diff)
    ex, _text, _attach = await rt._execute(call)  # type: ignore[misc]
    check("exit_code=0", ex.exit_code == 0, ex.output[:160])
    check("kind 正确", ex.kind == "apply_patch", ex.kind)
    check("文件已改（work_dir 生效）", f3.read_text(encoding="utf-8") == "new\n",
          repr(f3.read_text(encoding="utf-8")))

    # ---------- 4. apply_patch 显式 root= 不被覆盖 ----------
    print("\n[4] apply_patch 显式 root")
    other = tmp / "other"
    other.mkdir()
    f4 = other / "q.txt"
    f4.write_text("a\n", encoding="utf-8")
    diff4 = "--- a/q.txt\n+++ b/q.txt\n@@ -1 +1 @@\n-a\n+b\n"
    call = ToolCall(kind=ToolKind.APPLY_PATCH, target=f"root={other}", payload=diff4)
    ex, _text, _attach = await rt._execute(call)  # type: ignore[misc]
    check("显式 root 生效", ex.exit_code == 0 and f4.read_text(encoding="utf-8") == "b\n",
          ex.output[:160])

    # ---------- 5. apply_patch 失败 ----------
    print("\n[5] apply_patch 失败")
    bad_diff = "--- a/nope.txt\n+++ b/nope.txt\n@@ -1 +1 @@\n-x\n+y\n"
    call = ToolCall(kind=ToolKind.APPLY_PATCH, target="", payload=bad_diff)
    ex, _text, _attach = await rt._execute(call)  # type: ignore[misc]
    check("文件不存在 exit_code=1", ex.exit_code == 1, str(ex.exit_code))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="gad-patchexec-"))
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
