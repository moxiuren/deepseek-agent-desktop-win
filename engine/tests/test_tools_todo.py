"""任务清单（`tools_todo.py`）自测。

用法：
    cd engine && ./.venv/Scripts/python.exe tests/test_tools_todo.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gad_engine import tools_todo as TD

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
    else:
        FAIL.append(name + (f" — {detail}" if detail else ""))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="gad-todo-"))
    try:
        # ---------- 1. 路径生成 ----------
        print("\n[1] 路径生成")
        p1 = TD.todo_path_for(tmp, "conv-abc123")
        p2 = TD.todo_path_for(tmp, "conv-xyz")
        check("不同对话不同文件", p1 != p2, f"{p1} {p2}")
        check("在 todos 目录下", p1.parent.name == "todos", str(p1))
        # 路径穿越应被清洗
        p3 = TD.todo_path_for(tmp, "../../evil")
        # 正确判据：resolve 之后必须仍在 todos 目录内。
        # （清洗后的**文件名** `.._.._evil.md` 里含 `..` 是正常的，不构成穿越。）
        todos_dir = TD.todo_dir(tmp).resolve()
        try:
            p3.resolve().relative_to(todos_dir)
            inside = True
        except ValueError:
            inside = False
        check("路径穿越被清洗", inside, str(p3))

        # ---------- 2. write ----------
        print("\n[2] write")
        path = TD.todo_path_for(tmp, "c1")
        body = "- [ ] 读代码\n- [ ] 改 bug\n- [x] 建环境"
        ok, out = TD.todo_write(path, body)
        check("写入成功", ok and "成功" in out, out[:120])
        check("进度 1/3", "1/3" in out, out[:120])
        check("文件已落盘", path.exists())

        # ---------- 3. read ----------
        print("\n[3] read")
        ok, out = TD.todo_read(path)
        check("读取成功", ok and "读代码" in out, out[:200])
        check("进度显示", "1/3" in out, out[:120])

        # ---------- 4. 覆盖写 ----------
        print("\n[4] 覆盖写")
        ok, _ = TD.todo_write(path, "- [x] 全部做完")
        ok, out = TD.todo_read(path)
        check("覆盖生效", "全部做完" in out and "读代码" not in out, out[:200])
        check("进度 1/1", "1/1" in out, out[:120])

        # ---------- 5. 空清单 ----------
        print("\n[5] 空清单")
        p_new = TD.todo_path_for(tmp, "c2")
        ok, out = TD.todo_read(p_new)
        check("不存在的清单不报错", ok and "还没有" in out, out[:120])

        # ---------- 6. 错误 ----------
        print("\n[6] 错误")
        ok, out = TD.todo_write(path, "")
        check("空内容被拒", not ok and "为空" in out, out[:120])

        ok, out = TD.todo_write(path, "x" * 9000)
        check("超长被拒", not ok and "过长" in out, out[:120])

        # ---------- 7. 进度统计边界 ----------
        print("\n[7] 进度统计")
        check("无清单项", TD.summarize_progress("just text") == (0, 0))
        check("混合状态", TD.summarize_progress("- [ ] a\n- [x] b\n- [X] c") == (2, 3))
        check("不同符号", TD.summarize_progress("* [ ] a\n+ [x] b") == (1, 2))
        check("忽略非清单行", TD.summarize_progress("标题\n- [ ] a\n正文") == (0, 1))

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
