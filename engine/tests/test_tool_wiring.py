"""接线总检 —— 所有工具在 protocol + agent 两层的完整性。

这是一个「防漏」测试：新增工具时改了几处、漏了哪处，一跑就知道。
它不是功能测试（那是各专项测试的事），只回答一个问题：
**协议里认识的工具，执行层是否都有对应分支。**

用法：
    cd engine && ./.venv/Scripts/python.exe tests/test_tool_wiring.py
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gad_engine import protocol as P

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
    else:
        FAIL.append(name + (f" — {detail}" if detail else ""))


#: 期望存在的全部工具 kind
EXPECTED_KINDS = {
    "local_cmd", "local_cmd:async", "write_file",
    "replace_in_file", "read_file", "list_dir",
    "grep", "glob", "todo:write", "todo:read",
    "job", "ask_user", "run_tests", "mcp", "web_fetch", "web_search",
    "multi_edit", "apply_patch", "run_code", "git",
    "agent-screenshot", "agent-attach",
}

#: 每个 kind 的示例 info 行（用于验证 classify 真的能识别）
SAMPLES = {
    "local_cmd": "local_cmd",
    "local_cmd:async": "local_cmd:async",
    "write_file": "write_file:C:/x.txt",
    "replace_in_file": "replace_in_file:C:/x.py",
    "multi_edit": "multi_edit:C:/x.py",
    "apply_patch": "apply_patch",
    "read_file": "read_file:C:/x.py offset=1",
    "list_dir": "list_dir:C:/ depth=2",
    "grep": "grep:foo path=.",
    "glob": "glob:*.py",
    "todo:write": "todo:write",
    "todo:read": "todo:read",
    "job": "job:list",
    "ask_user": "ask_user:选哪个",
    "run_tests": "run_tests",
    "mcp": "mcp:list",
    "web_fetch": "web_fetch:https://x.com",
    "web_search": "web_search:hello",
    "run_code": "run_code",
    "git": "git:status",
    "agent-screenshot": "agent-screenshot",
    "agent-attach": "agent-attach:C:/x.png",
}


def main() -> int:
    # ---------- 1. ToolKind 枚举齐全 ----------
    print("\n[1] ToolKind 枚举")
    actual = {k.value for k in P.ToolKind}
    missing = EXPECTED_KINDS - actual
    extra = actual - EXPECTED_KINDS
    check("枚举无缺失", not missing, f"缺: {missing}")
    check("枚举无多余", not extra, f"多: {extra}")

    # ---------- 2. classify 能识别每个 kind ----------
    print("\n[2] classify 识别")
    for kind_val, sample in SAMPLES.items():
        result = P.classify(sample)
        ok = result is not None and result[0].value == kind_val
        check(f"识别 {kind_val}", ok, f"{sample!r} -> {result}")

    # ---------- 3. 协议文本提到每个工具 ----------
    print("\n[3] 协议文本")
    # agent-screenshot / agent-attach 在 CONTRACT_RULES 里以别的形式出现
    for kw in ["local_cmd", "write_file", "replace_in_file", "read_file",
               "list_dir", "grep", "glob", "todo", "job", "ask_user", "run_tests",
               "git"]:
        check(f"协议含 {kw}", kw in P.CONTRACT_RULES, "")

    # ---------- 4. 执行层分支存在 ----------
    print("\n[4] 执行层分支")
    from gad_engine.agent import AgentRuntime
    src = inspect.getsource(AgentRuntime._execute)
    for kind_val in EXPECTED_KINDS:
        # 每个 kind 应在 _execute 里被引用（或其辅助方法）
        enum_name = None
        for k in P.ToolKind:
            if k.value == kind_val:
                enum_name = k.name
                break
        if enum_name:
            check(f"_execute 引用 {kind_val}", f"ToolKind.{enum_name}" in src, "")

    # ---------- 5. 新增的辅助方法存在 ----------
    print("\n[5] 辅助方法")
    for meth in ["_screen_command", "_run_fs", "_run_job", "_run_async", "_run_command", "_todo_path"]:
        check(f"有 {meth}", hasattr(AgentRuntime, meth), "")

    # ---------- 6. 新模块可导入 ----------
    print("\n[6] 模块导入")
    for mod in ["tools_fs", "tools_search", "tools_todo", "tools_tests", "jobs", "safety",
                "tools_git"]:
        try:
            __import__(f"gad_engine.{mod}")
            check(f"导入 {mod}", True)
        except Exception as exc:  # noqa: BLE001
            check(f"导入 {mod}", False, str(exc))

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
