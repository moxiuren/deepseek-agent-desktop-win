"""diff 生成（`tools_diff.py`）自测。

用法：
    cd engine && ./.venv/Scripts/python.exe tests/test_tools_diff.py
"""

from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gad_engine import tools_diff as D

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
    else:
        FAIL.append(name + (f" — {detail}" if detail else ""))


def main() -> int:
    # ---------- 1. 无变化 ----------
    print("\n[1] 无变化")
    check("相同文本 → 空 diff", D.unified_diff("a\nb\n", "a\nb\n") == "")
    check("相同 → stats 0,0", D.diff_stats("x\n", "x\n") == (0, 0))

    # ---------- 2. 基本 diff ----------
    print("\n[2] 基本 diff")
    diff = D.unified_diff("a\nb\nc\n", "a\nB\nc\n", "x.txt")
    check("非空", bool(diff), repr(diff))
    check("含文件头 a/", "a/x.txt" in diff, diff[:120])
    check("含文件头 b/", "b/x.txt" in diff, diff[:120])
    check("含删除行 -b", "\n-b" in diff or diff.startswith("-b"), diff)
    check("含新增行 +B", "\n+B" in diff or diff.startswith("+B"), diff)
    check("含上下文 a", " a" in diff or "a" in diff, diff[:200])

    # ---------- 3. stats ----------
    print("\n[3] stats")
    added, removed = D.diff_stats("a\nb\nc\n", "a\nB\nc\nd\n")
    check("新增 2 行", added == 2, str(added))
    check("删除 1 行", removed == 1, str(removed))

    # ---------- 4. CRLF 归一 ----------
    print("\n[4] CRLF")
    # CRLF 与 LF 视为相同内容（展示层归一）
    d = D.unified_diff("a\r\nb\r\n", "a\nb\n")
    check("CRLF vs LF 视为无变化", d == "", repr(d))

    # ---------- 5. 换行归一后仍有真实差异 ----------
    print("\n[5] 归一后真实差异")
    d = D.unified_diff("a\r\nb\r\n", "a\r\nB\r\n", "y.txt")
    check("内容差异仍被检出", "+B" in d, d)

    # ---------- 6. 多行替换 ----------
    print("\n[6] 多行替换")
    old = "line1\nline2\nline3\nline4\nline5\n"
    new = "line1\nCHANGED\nline4\nline5\n"
    d = D.unified_diff(old, new, "m.txt")
    check("含 +CHANGED", "+CHANGED" in d, d)
    check("含 -line2", "-line2" in d, d)
    check("含 -line3", "-line3" in d, d)

    # ---------- 7. 截断 ----------
    print("\n[7] 截断")
    big_old = "\n".join(f"old{i}" for i in range(500)) + "\n"
    big_new = "\n".join(f"new{i}" for i in range(500)) + "\n"
    d = D.unified_diff(big_old, big_new, "big.txt", max_lines=50)
    check("被截断", "已截断" in d, d[-120:])
    check("行数受控", len(d.split("\n")) < 100, str(len(d.split("\n"))))

    # ---------- 8. format_preview ----------
    print("\n[8] 预览格式")
    p = D.format_preview("x.py", "+new")
    check("含未写入", "未写入" in p, p[:120])
    check("含路径", "x.py" in p, p[:120])
    check("含应用提示", "preview=1" in p, p[:200])

    # ---------- 9. 空输入边界 ----------
    print("\n[9] 边界")
    check("两空串 → 无 diff", D.unified_diff("", "") == "")
    check("空→有内容", bool(D.unified_diff("", "x\n")), "")
    check("有内容→空", bool(D.unified_diff("x\n", "")), "")

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
