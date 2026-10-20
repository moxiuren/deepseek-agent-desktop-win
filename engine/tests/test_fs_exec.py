"""文件工具的执行层接线验证：ToolCall -> AgentRuntime._execute -> tools_fs。

test_fs_pipeline.py 验证的是「解析」，本文件验证的是「执行入口接线」——
即 agent._execute 真的会把 READ_FILE / LIST_DIR / REPLACE_IN_FILE 分发到
tools_fs 上，并包成 ToolExecution 返回。这是新加的接线点，必须单独覆盖。

为什么要用 __new__ 造实例：文件工具的执行分支（_run_fs）**不依赖任何实例状态**
（不碰 transport / executor / session）。走完整 __init__ 会拉起一堆用不上的东西，
而这里要测的只是「分发是否正确」。所以直接绕过构造。

用法：
    cd engine && ./.venv/Scripts/python.exe tests/test_fs_exec.py
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

from gad_engine.agent import AgentRuntime, ToolExecution
from gad_engine.protocol import ToolCall, ToolKind

PASS: list[str] = []
FAIL: list[str] = []

OLD_MARK = "<<<<<<< OLD"
SEP_MARK = "======="
NEW_MARK = ">>>>>>> NEW"


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
    else:
        FAIL.append(name + (f" — {detail}" if detail else ""))


async def run_checks() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="gad-fsexec-"))
    try:
        rt = AgentRuntime.__new__(AgentRuntime)  # 跳过 __init__

        f = tmp / "x.py"
        f.write_text("a = 1\nb = 2\n", encoding="utf-8")

        # ---------- read_file 分发 ----------
        print("\n[1] read_file 分发")
        call = ToolCall(kind=ToolKind.READ_FILE, target=str(f), payload="")
        out = await rt._execute(call)
        check("返回三元组", out is not None and len(out) == 3, str(out))
        ex, text, attach = out  # type: ignore[misc]
        check("是 ToolExecution", isinstance(ex, ToolExecution), type(ex).__name__)
        check("exit_code=0", ex.exit_code == 0, str(ex.exit_code))
        check("kind 正确", ex.kind == "read_file", ex.kind)
        check("output 含行号", "1\ta = 1" in ex.output, ex.output[:120])
        check("attach 为 None", attach is None)
        check("回传文本含收尾提示", "请输出下一个" in text, text[-60:])

        # ---------- list_dir 分发 ----------
        print("\n[2] list_dir 分发")
        call = ToolCall(kind=ToolKind.LIST_DIR, target=str(tmp), payload="")
        ex, text, attach = await rt._execute(call)  # type: ignore[misc]
        check("exit_code=0", ex.exit_code == 0, str(ex.exit_code))
        check("kind 正确", ex.kind == "list_dir", ex.kind)
        check("含文件名", "x.py" in ex.output, ex.output[:160])

        # ---------- replace_in_file 分发 ----------
        print("\n[3] replace_in_file 分发")
        body = f"{OLD_MARK}\nb = 2\n{SEP_MARK}\nb = 22\n{NEW_MARK}"
        call = ToolCall(kind=ToolKind.REPLACE_IN_FILE, target=str(f), payload=body)
        ex, text, attach = await rt._execute(call)  # type: ignore[misc]
        check("exit_code=0", ex.exit_code == 0, ex.output[:120])
        check("kind 正确", ex.kind == "replace_in_file", ex.kind)
        check("文件真被改", f.read_text(encoding="utf-8") == "a = 1\nb = 22\n",
              repr(f.read_text(encoding="utf-8")))

        # ---------- 失败场景 ----------
        print("\n[4] 失败场景")
        call = ToolCall(kind=ToolKind.READ_FILE, target=str(tmp / "nope.py"), payload="")
        ex, text, attach = await rt._execute(call)  # type: ignore[misc]
        check("失败时 exit_code=1", ex.exit_code == 1, str(ex.exit_code))
        check("失败文本含提示", "不存在" in ex.output, ex.output[:120])

        # 坏 body 的 replace
        call = ToolCall(kind=ToolKind.REPLACE_IN_FILE, target=str(f), payload="garbage")
        ex, text, attach = await rt._execute(call)  # type: ignore[misc]
        check("坏 body 时 exit_code=1", ex.exit_code == 1, str(ex.exit_code))

        # ---------- 旧分支不受影响 ----------
        print("\n[5] 旧分支仍在")
        # 只验证 ToolKind 枚举齐全，不真跑命令
        check("WRITE_FILE 分支存在", hasattr(ToolKind, "WRITE_FILE"))
        check("LOCAL_CMD 分支存在", hasattr(ToolKind, "LOCAL_CMD"))

    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    asyncio.run(run_checks())
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
