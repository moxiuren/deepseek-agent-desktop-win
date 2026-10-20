"""检索/清单工具的执行层接线：ToolCall -> AgentRuntime._execute。

test_tools_search.py / test_tools_todo.py 验证纯逻辑；本文件验证 agent._execute
真的会把 GREP / GLOB / TODO_WRITE / TODO_READ 分发下去。

todo 分支需要 self.conversation.id，所以这里给实例挂一个最小的假 conversation。

用法：
    cd engine && ./.venv/Scripts/python.exe tests/test_search_exec.py
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

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


async def run_checks(tmp: Path) -> None:
    rt = AgentRuntime.__new__(AgentRuntime)
    rt.conversation = SimpleNamespace(id="test-conv-b2")

    # 给 todo 一个隔离的 app_data：monkeypatch app_data_dir
    import gad_engine.agent as A
    orig = A.app_data_dir
    A.app_data_dir = lambda: tmp  # type: ignore[assignment]
    try:
        # 造点搜索素材
        (tmp / "code").mkdir()
        (tmp / "code" / "a.py").write_text("def foo():\n    return 42\n", encoding="utf-8")
        (tmp / "code" / "b.py").write_text("x = 1\n", encoding="utf-8")

        # ---------- grep 分发 ----------
        print("\n[1] grep 分发")
        call = ToolCall(kind=ToolKind.GREP, target=f"return 42 path={tmp / 'code'}", payload="")
        ex, _text, _attach = await rt._execute(call)  # type: ignore[misc]
        check("exit_code=0", ex.exit_code == 0, ex.output[:120])
        check("kind 正确", ex.kind == "grep", ex.kind)
        check("命中 a.py", "a.py" in ex.output, ex.output[:200])

        # ---------- glob 分发 ----------
        print("\n[2] glob 分发")
        call = ToolCall(kind=ToolKind.GLOB, target=f"*.py root={tmp / 'code'}", payload="")
        ex, _text, _attach = await rt._execute(call)  # type: ignore[misc]
        check("exit_code=0", ex.exit_code == 0, ex.output[:120])
        check("kind 正确", ex.kind == "glob", ex.kind)
        check("含两个文件", "a.py" in ex.output and "b.py" in ex.output, ex.output[:200])

        # ---------- todo:write 分发 ----------
        print("\n[3] todo:write 分发")
        body = "- [ ] 第一步\n- [x] 已完成"
        call = ToolCall(kind=ToolKind.TODO_WRITE, target="", payload=body)
        ex, _text, _attach = await rt._execute(call)  # type: ignore[misc]
        check("exit_code=0", ex.exit_code == 0, ex.output[:120])
        check("kind 正确", ex.kind == "todo:write", ex.kind)
        check("进度 1/2", "1/2" in ex.output, ex.output[:160])

        # ---------- todo:read 分发 ----------
        print("\n[4] todo:read 分发")
        call = ToolCall(kind=ToolKind.TODO_READ, target="", payload="")
        ex, _text, _attach = await rt._execute(call)  # type: ignore[misc]
        check("exit_code=0", ex.exit_code == 0, ex.output[:120])
        check("kind 正确", ex.kind == "todo:read", ex.kind)
        check("读回内容", "第一步" in ex.output, ex.output[:200])

        # ---------- todo 按对话隔离 ----------
        print("\n[5] todo 按对话隔离")
        rt.conversation = SimpleNamespace(id="another-conv")
        call = ToolCall(kind=ToolKind.TODO_READ, target="", payload="")
        ex, _text, _attach = await rt._execute(call)  # type: ignore[misc]
        check("新对话无清单", "还没有" in ex.output, ex.output[:160])

        # ---------- 失败场景 ----------
        print("\n[6] 失败场景")
        call = ToolCall(kind=ToolKind.GREP, target="([ path=/", payload="")
        ex, _text, _attach = await rt._execute(call)  # type: ignore[misc]
        check("非法正则 exit_code=1", ex.exit_code == 1, ex.output[:120])

    finally:
        A.app_data_dir = orig  # type: ignore[assignment]


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="gad-searchexec-"))
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
