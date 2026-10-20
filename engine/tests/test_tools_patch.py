"""多编辑工具（`tools_patch.py`）自测。

用法：
    cd engine && ./.venv/Scripts/python.exe tests/test_tools_patch.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gad_engine import tools_fs as TF
from gad_engine import tools_patch as TP

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
    else:
        FAIL.append(name + (f" — {detail}" if detail else ""))


def block(old: str, new: str) -> str:
    return f"{TF.MARK_OLD}\n{old}\n{TF.MARK_SEP}\n{new}\n{TF.MARK_NEW}"


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="gad-patch-"))
    try:
        # ---------- 1. parse_multi_edit_body ----------
        print("\n[1] 解析 multi_edit body")
        body1 = block("a", "b")
        check("单块解析", TP.parse_multi_edit_body(body1) == [("a", "b")])

        body2 = block("a", "b") + "\n" + TP.EDIT_SEP + "\n" + block("c", "d")
        check("多块解析", TP.parse_multi_edit_body(body2) == [("a", "b"), ("c", "d")])

        check("空 body 返回 None", TP.parse_multi_edit_body("") is None)
        check("坏格式返回 None", TP.parse_multi_edit_body("garbage") is None)

        # ---------- 2. multi_edit 基本 ----------
        print("\n[2] multi_edit 基本")
        f = tmp / "a.txt"
        f.write_text("line1\nline2\nline3\n", encoding="utf-8")
        body = block("line1", "L1") + "\n" + TP.EDIT_SEP + "\n" + block("line3", "L3")
        ok, out = TP.multi_edit(str(f), body)
        check("成功", ok and "2 处" in out, out[:120])
        check("内容正确", f.read_text(encoding="utf-8") == "L1\nline2\nL3\n",
              repr(f.read_text(encoding="utf-8")))

        # ---------- 3. 原子性：一处失败则整体不动 ----------
        print("\n[3] 原子性")
        f2 = tmp / "b.txt"
        f2.write_text("aaa\nbbb\nccc\n", encoding="utf-8")
        before = f2.read_text(encoding="utf-8")
        # 第一个块能匹配，第二个不能
        body = block("aaa", "AAA") + "\n" + TP.EDIT_SEP + "\n" + block("nope", "X")
        ok, out = TP.multi_edit(str(f2), body)
        check("失败", not ok, out[:120])
        check("文件未改动", f2.read_text(encoding="utf-8") == before,
              repr(f2.read_text(encoding="utf-8")))
        check("说明整个未落盘", "整个编辑未落盘" in out, out[:160])

        # ---------- 4. 重复原文拒绝 ----------
        print("\n[4] 重复原文")
        f3 = tmp / "c.txt"
        f3.write_text("dup\ndup\ndup\n", encoding="utf-8")
        ok, out = TP.multi_edit(str(f3), block("dup", "x"))
        check("重复原文被拒", not ok and "出现 3 次" in out, out[:160])
        check("文件未动", f3.read_text(encoding="utf-8") == "dup\ndup\ndup\n")

        # ---------- 5. CRLF 保持 ----------
        print("\n[5] CRLF 保持")
        f4 = tmp / "crlf.txt"
        f4.write_bytes(b"a\r\nb\r\nc\r\n")
        body = block("a", "A") + "\n" + TP.EDIT_SEP + "\n" + block("c", "C")
        ok, _ = TP.multi_edit(str(f4), body)
        check("CRLF 保持", ok and f4.read_bytes() == b"A\r\nb\r\nC\r\n",
              repr(f4.read_bytes()))

        # ---------- 6. parse_unified_diff ----------
        print("\n[6] 解析 unified diff")
        diff = (
            "--- a/x.py\n"
            "+++ b/x.py\n"
            "@@ -1,3 +1,3 @@\n"
            " ctx\n"
            "-old\n"
            "+new\n"
            " more\n"
        )
        patches = TP.parse_unified_diff(diff)
        check("解析出 1 个文件", patches is not None and len(patches) == 1, str(patches))
        if patches:
            check("路径正确", patches[0].path == "x.py", patches[0].path)
            check("1 个 hunk", len(patches[0].hunks) == 1, str(len(patches[0].hunks)))

        # 多文件
        diff2 = diff + "--- a/y.py\n+++ b/y.py\n@@ -1,1 +1,1 @@\n-a\n+b\n"
        patches2 = TP.parse_unified_diff(diff2)
        check("解析出 2 个文件", patches2 is not None and len(patches2) == 2, str(len(patches2 or [])))

        check("空 diff 返回 None", TP.parse_unified_diff("") is None)
        check("垃圾返回 None", TP.parse_unified_diff("hello world") is None)

        # ---------- 7. apply_patch 基本 ----------
        print("\n[7] apply_patch")
        f5 = tmp / "patch1.txt"
        f5.write_text("line1\nline2\nline3\n", encoding="utf-8")
        diff3 = (
            "--- a/patch1.txt\n"
            "+++ b/patch1.txt\n"
            "@@ -1,3 +1,3 @@\n"
            " line1\n"
            "-line2\n"
            "+LINE2\n"
            " line3\n"
        )
        ok, out = TP.apply_patch(f"root={tmp}", diff3)
        check("应用成功", ok and "1 个文件" in out, out[:120])
        check("内容正确", f5.read_text(encoding="utf-8") == "line1\nLINE2\nline3\n",
              repr(f5.read_text(encoding="utf-8")))

        # ---------- 8. apply_patch 多文件 ----------
        print("\n[8] apply_patch 多文件")
        f6 = tmp / "p6a.txt"
        f7 = tmp / "p6b.txt"
        f6.write_text("aaa\n", encoding="utf-8")
        f7.write_text("bbb\n", encoding="utf-8")
        diff4 = (
            "--- a/p6a.txt\n+++ b/p6a.txt\n@@ -1 +1 @@\n-aaa\n+AAA\n"
            "--- a/p6b.txt\n+++ b/p6b.txt\n@@ -1 +1 @@\n-bbb\n+BBB\n"
        )
        ok, out = TP.apply_patch(f"root={tmp}", diff4)
        check("多文件成功", ok and "2 个文件" in out, out[:120])
        check("文件a已改", f6.read_text(encoding="utf-8") == "AAA\n")
        check("文件b已改", f7.read_text(encoding="utf-8") == "BBB\n")

        # ---------- 9. apply_patch 原子性 ----------
        print("\n[9] apply_patch 原子性")
        f8 = tmp / "p8a.txt"
        f9 = tmp / "p8b.txt"
        f8.write_text("ok\n", encoding="utf-8")
        f9.write_text("real\n", encoding="utf-8")
        # 第一个文件能应用，第二个文件的 hunk 匹配不上
        diff5 = (
            "--- a/p8a.txt\n+++ b/p8a.txt\n@@ -1 +1 @@\n-ok\n+OK\n"
            "--- a/p8b.txt\n+++ b/p8b.txt\n@@ -1 +1 @@\n-WRONG\n+X\n"
        )
        ok, out = TP.apply_patch(f"root={tmp}", diff5)
        check("失败", not ok, out[:120])
        check("文件a未改（原子性）", f8.read_text(encoding="utf-8") == "ok\n",
              repr(f8.read_text(encoding="utf-8")))

        # ---------- 10. apply_patch 错误 ----------
        print("\n[10] apply_patch 错误")
        ok, out = TP.apply_patch(f"root={tmp}", "not a diff")
        check("非法 diff 报错", not ok and "没解析出" in out, out[:120])

        # 文件不存在
        diff_missing = "--- a/nope.txt\n+++ b/nope.txt\n@@ -1 +1 @@\n-x\n+y\n"
        ok, out = TP.apply_patch(f"root={tmp}", diff_missing)
        check("文件不存在报错", not ok and "不存在" in out, out[:120])

        # 路径逃逸
        diff_escape = "--- a/../../evil.txt\n+++ b/../../evil.txt\n@@ -1 +1 @@\n-x\n+y\n"
        ok, out = TP.apply_patch(f"root={tmp}", diff_escape)
        check("路径逃逸被拦", not ok, out[:120])

        # ---------- 11. 无 root=（默认当前目录）----------
        print("\n[11] 无 root 参数")
        import os as _os
        old_cwd = _os.getcwd()
        try:
            _os.chdir(tmp)
            f10 = tmp / "noroot.txt"
            f10.write_text("x\n", encoding="utf-8")
            diff6 = "--- a/noroot.txt\n+++ b/noroot.txt\n@@ -1 +1 @@\n-x\n+y\n"
            ok, out = TP.apply_patch("", diff6)
            check("无 root 也能应用", ok and f10.read_text(encoding="utf-8") == "y\n",
                  out[:120])
        finally:
            _os.chdir(old_cwd)

        # ---------- 12. preview 不写盘 ----------
        print("\n[12] preview")
        fp = tmp / "prev.txt"
        fp.write_text("aa\nbb\n", encoding="utf-8")
        body_p = block("aa", "AA") + "\n" + TP.EDIT_SEP + "\n" + block("bb", "BB")
        ok, out = TP.multi_edit(f"{fp} preview=1", body_p)
        check("multi preview 成功", ok and "未写入" in out, out[:120])
        check("multi preview 未写盘", fp.read_text(encoding="utf-8") == "aa\nbb\n",
              repr(fp.read_text(encoding="utf-8")))

        fp2 = tmp / "prev2.txt"
        fp2.write_text("x\n", encoding="utf-8")
        diff_p = "--- a/prev2.txt\n+++ b/prev2.txt\n@@ -1 +1 @@\n-x\n+y\n"
        ok, out = TP.apply_patch(f"root={tmp} preview=1", diff_p)
        check("patch preview 成功", ok and "未写入" in out, out[:120])
        check("patch preview 含 diff", "+y" in out, out[:200])
        check("patch preview 未写盘", fp2.read_text(encoding="utf-8") == "x\n")

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
