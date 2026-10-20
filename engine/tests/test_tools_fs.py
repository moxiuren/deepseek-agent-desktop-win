"""文件系统工具（`tools_fs.py`）自测。

覆盖重点：**那些用 shell 做会静默出错的场景** —— 正则元字符、CRLF、
非 UTF-8、「以为只有一处其实有多处」、BOM 不被凭空添加。如果这些用例全绿，
说明「改代码」这件事已经从「靠模型别写错」变成了「靠代码保证」。

用法：
    cd engine && ./.venv/Scripts/python.exe tests/test_tools_fs.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gad_engine import tools_fs as T

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
    else:
        FAIL.append(name + (f" — {detail}" if detail else ""))


def _body(old: str, new: str) -> str:
    return f"{T.MARK_OLD}\n{old}\n{T.MARK_SEP}\n{new}\n{T.MARK_NEW}"


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="gad-fstools-"))
    try:
        # ---------- 1. split_path_args ----------
        print("\n[1] split_path_args")
        p, a = T.split_path_args("C:/x/y.py")
        check("纯路径无参", p == "C:/x/y.py" and a == {}, f"{p!r} {a!r}")

        p, a = T.split_path_args("C:/x/y.py offset=10 limit=50")
        check("拆出参数", p == "C:/x/y.py" and a == {"offset": "10", "limit": "50"}, f"{p!r} {a!r}")

        p, a = T.split_path_args("C:/My Folder/sub/x.py offset=5")
        check("含空格路径不误拆", p == "C:/My Folder/sub/x.py" and a == {"offset": "5"}, f"{p!r} {a!r}")

        p, a = T.split_path_args("C:/x/a=b.py")
        check("路径内含 = 不误拆", p == "C:/x/a=b.py" and a == {}, f"{p!r} {a!r}")

        # ---------- 2. read_file ----------
        print("\n[2] read_file")
        f = tmp / "a.py"
        f.write_text("line1\nline2\nline3\n", encoding="utf-8")
        ok, out = T.read_file(str(f))
        check("基本读取", ok and "共 3 行" in out, out[:80])
        check("带行号", "1\tline1" in out and "3\tline3" in out, out[:200])

        big = tmp / "big.txt"
        big.write_text("\n".join(f"L{i}" for i in range(1, 101)), encoding="utf-8")
        ok, out = T.read_file(f"{big} offset=10 limit=5")
        check("offset/limit 生效", ok and "10\tL10" in out and "14\tL14" in out, out[:200])
        check("尾部提示继续读", "offset=15" in out, out[-120:])

        ok, out = T.read_file(str(tmp / "nope.py"))
        check("文件不存在", (not ok) and "不存在" in out, out[:80])

        ok, out = T.read_file(str(tmp))
        check("目录报错", (not ok) and "目录" in out, out[:80])

        gbk = tmp / "gbk.txt"
        gbk.write_bytes("中文".encode("gbk"))
        ok, out = T.read_file(str(gbk))
        check("非 UTF-8 明确报错", (not ok) and "UTF-8" in out, out[:120])

        bom = tmp / "bom.txt"
        bom.write_bytes("hi\n".encode("utf-8-sig"))
        ok, out = T.read_file(str(bom))
        check("BOM 识别", ok and "utf-8-sig" in out, out[:80])

        huge = tmp / "huge.txt"
        huge.write_text("\n".join(f"L{i}" for i in range(5000)), encoding="utf-8")
        ok, out = T.read_file(f"{huge} limit=99999")
        check("limit 被上限截住", ok and "显示 1-2000 行" in out, out[:120])

        # ---------- 3. parse_edit_body ----------
        print("\n[3] parse_edit_body")
        check("解析成功", T.parse_edit_body(_body("old", "new")) == ("old", "new"))
        check("缺标记返回 None", T.parse_edit_body("just text") is None)
        ml = _body("def f():\n    return 1", "def f():\n    return 2")
        check("多行解析", T.parse_edit_body(ml) == ("def f():\n    return 1", "def f():\n    return 2"))

        # ---------- 4. replace_in_file ----------
        print("\n[4] replace_in_file")
        r = tmp / "r.py"
        r.write_text("x = 1\ny = 2\n", encoding="utf-8")
        ok, out = T.replace_in_file(str(r), _body("x = 1", "x = 99"))
        check("替换一处", ok and "替换 1 处" in out, out[:120])
        check("内容正确", r.read_text(encoding="utf-8") == "x = 99\ny = 2\n", repr(r.read_text(encoding="utf-8")))
        check("UTF-8 文件不新增 BOM", not r.read_bytes().startswith(b"\xef\xbb\xbf"), repr(r.read_bytes()[:6]))

        r2 = tmp / "r2.txt"
        r2.write_text("a\na\na\n", encoding="utf-8")
        ok, out = T.replace_in_file(str(r2), _body("a", "b"))
        check("默认只替第一处", ok and r2.read_text(encoding="utf-8") == "b\na\na\n", out[:120])
        check("提醒还有未替换", "共出现 3 次" in out, out[:160])

        ok, _ = T.replace_in_file(f"{r2} count=0", _body("a", "b"))
        check("count=0 全替换", ok and r2.read_text(encoding="utf-8") == "b\nb\nb\n")

        r3 = tmp / "r3.txt"
        r3.write_text("a\na\na\n", encoding="utf-8")
        ok, out = T.replace_in_file(f"{r3} expect=1", _body("a", "b"))
        check("expect 不符则中止", (not ok) and "expect=1 不符" in out, out[:160])
        check("中止后文件未动", r3.read_text(encoding="utf-8") == "a\na\na\n")

        r4 = tmp / "r4.txt"
        r4.write_text("hello\n", encoding="utf-8")
        ok, out = T.replace_in_file(str(r4), _body("nope", "x"))
        check("原文未找到", (not ok) and "未找到" in out, out[:160])
        check("未找到时文件未动", r4.read_text(encoding="utf-8") == "hello\n")

        ok, out = T.replace_in_file(str(r4), _body("", "x"))
        check("空原文被拒", (not ok) and "原文为空" in out, out[:120])

        crlf = tmp / "crlf.txt"
        crlf.write_bytes(b"a\r\nb\r\nc\r\n")
        ok, _ = T.replace_in_file(str(crlf), _body("b", "B"))
        check("CRLF 保持", ok and crlf.read_bytes() == b"a\r\nB\r\nc\r\n", repr(crlf.read_bytes()))

        meta = tmp / "meta.txt"
        meta.write_text("price = $9.99 (net)\n", encoding="utf-8")
        ok, _ = T.replace_in_file(str(meta), _body("$9.99 (net)", "$19.99 (gross)"))
        check("正则元字符当字面量", ok and meta.read_text(encoding="utf-8") == "price = $19.99 (gross)\n")

        g2 = tmp / "g2.txt"
        g2.write_bytes("中文\n".encode("gbk"))
        ok, out = T.replace_in_file(str(g2), _body("x", "y"))
        check("非 UTF-8 拒绝替换", (not ok) and "UTF-8" in out, out[:120])

        # ---------- 5. list_dir ----------
        print("\n[5] list_dir")
        d = tmp / "tree"
        (d / "sub").mkdir(parents=True)
        (d / "sub" / "x.py").write_text("hi", encoding="utf-8")
        (d / "a.txt").write_text("yo", encoding="utf-8")
        ok, out = T.list_dir(f"{d} depth=2")
        check("列目录成功", ok and "目录 1 个" in out and "文件 2 个" in out, out[:160])
        check("含子项", "a.txt" in out and "sub/" in out and "x.py" in out, out[:300])

        deep = tmp / "deep"
        cur = deep
        for i in range(6):
            cur = cur / f"d{i}"
            cur.mkdir(parents=True)
        (cur / "deep.txt").write_text("x", encoding="utf-8")
        ok, out = T.list_dir(f"{deep} depth=99")
        check("depth 被上限截住", ok and "深度 4" in out, out[:120])

        ok, out = T.list_dir(str(tmp / "nope"))
        check("不存在路径报错", (not ok) and "不存在" in out, out[:80])

        # ---------- 6. 边界 ----------
        print("\n[6] 边界")
        empty = tmp / "empty.txt"
        empty.write_text("", encoding="utf-8")
        ok, out = T.read_file(str(empty))
        check("空文件不炸", ok and "(空文件)" in out, out[:120])

        ok, out = T.replace_in_file(str(empty), "garbage")
        check("body 格式错报错", (not ok) and "格式不对" in out, out[:120])

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
