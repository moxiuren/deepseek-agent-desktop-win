"""检索工具（`tools_search.py`）自测。

用法：
    cd engine && ./.venv/Scripts/python.exe tests/test_tools_search.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gad_engine import tools_search as S

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
    else:
        FAIL.append(name + (f" — {detail}" if detail else ""))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="gad-search-"))
    try:
        # 构造一棵小树
        (tmp / "src").mkdir()
        (tmp / "src" / "a.py").write_text("import os\ndef foo():\n    return 42\n", encoding="utf-8")
        (tmp / "src" / "b.py").write_text("x = 1\n# TODO: fix\ny = 2\n", encoding="utf-8")
        (tmp / "docs").mkdir()
        (tmp / "docs" / "readme.md").write_text("Hello World\nhello again\n", encoding="utf-8")
        # node_modules 里的文件应被跳过
        (tmp / "node_modules").mkdir()
        (tmp / "node_modules" / "x.js").write_text("return 42\n", encoding="utf-8")
        # 二进制扩展名应被跳过
        (tmp / "img.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00return 42")

        # ---------- 1. grep 基本 ----------
        print("\n[1] grep 基本")
        ok, out = S.grep(f"return 42 path={tmp}")
        check("命中成功", ok and "命中" in out, out[:120])
        check("命中 a.py", "a.py" in out, out[:300])
        check("跳过 node_modules", "node_modules" not in out, out[:400])
        check("跳过二进制扩展名", "img.png" not in out, out[:400])
        check("带行号", "a.py:3" in out, out[:300])

        # ---------- 2. grep fixed ----------
        print("\n[2] grep fixed（字面量）")
        # 不含元字符时与普通模式结果一致
        ok, out = S.grep(f"return 42 path={tmp} fixed=1")
        check("fixed 命中", ok and "a.py" in out, out[:200])
        # 元字符：正则模式下 `(net)` 是分组，fixed 下是字面量
        (tmp / "p.txt").write_text("price (net) ok\n", encoding="utf-8")
        _ok_reg, _ = S.grep(f"(net) path={tmp}")
        ok_fixed, out_fixed = S.grep(f"(net) path={tmp} fixed=1")
        check("fixed 匹配字面量括号", ok_fixed and "p.txt" in out_fixed, out_fixed[:200])

        # ---------- 3. grep ignore_case ----------
        print("\n[3] grep ignore_case")
        ok, out = S.grep(f"hello path={tmp} ignore_case=1")
        check("忽略大小写命中两处", ok and out.count("readme.md") >= 1, out[:300])

        # ---------- 4. grep glob 过滤 ----------
        print("\n[4] grep glob 过滤")
        ok, out = S.grep(f"return 42 path={tmp} glob=*.py")
        check("只搜 .py", ok and "a.py" in out and ".md" not in out, out[:200])

        # ---------- 5. grep 无命中 ----------
        print("\n[5] grep 无命中")
        ok, out = S.grep(f"zzz_nonexistent_zzz path={tmp}")
        check("无命中返回成功", ok and "无命中" in out, out[:120])

        # ---------- 6. grep max 上限 ----------
        print("\n[6] grep max 上限")
        ok, out = S.grep(f"return path={tmp} max=1")
        check("max=1 截断", ok and "已达上限" in out, out[:160])

        # ---------- 7. grep 错误 ----------
        print("\n[7] grep 错误")
        ok, out = S.grep("")
        check("空模式报错", not ok and "缺少" in out, out[:80])
        ok, out = S.grep(f"x path={tmp / 'nope'}")
        check("路径不存在报错", not ok and "不存在" in out, out[:80])
        ok, out = S.grep(f"([ path={tmp}")
        check("非法正则报错", not ok and "无效" in out, out[:120])

        # ---------- 8. glob ----------
        print("\n[8] glob")
        ok, out = S.glob_search(f"*.py root={tmp}")
        check("按文件名匹配", ok and "a.py" in out and "b.py" in out, out[:200])
        check("不含 md", ".md" not in out, out[:200])

        ok, out = S.glob_search(f"**/*.md root={tmp}")
        check("** 递归匹配", ok and "readme.md" in out, out[:200])

        ok, out = S.glob_search(f"*.nomatch root={tmp}")
        check("无匹配返回成功", ok and "无匹配" in out, out[:120])

        ok, out = S.glob_search("")
        check("glob 空模式报错", not ok, out[:80])

        # ---------- 9. 编码 ----------
        print("\n[9] 编码容错")
        (tmp / "gbk.txt").write_bytes("中文 return 42\n".encode("gbk"))
        ok, out = S.grep(f"return 42 path={tmp}")
        check("非 UTF-8 文件被跳过不报错", ok, out[:120])

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
