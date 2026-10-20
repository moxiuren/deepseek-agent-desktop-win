"""安全护栏（`safety.py`）自测。

用法：
    cd engine && ./.venv/Scripts/python.exe tests/test_safety.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gad_engine import safety as S

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
    else:
        FAIL.append(name + (f" — {detail}" if detail else ""))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="gad-safety-"))
    try:
        # ---------- 1. 正常命令放行 ----------
        print("\n[1] 正常命令放行")
        for cmd in ["Get-Date", "ls -la", "python -m pytest", "git status", "echo hi"]:
            sev, _ = S.check_command(cmd)
            check(f"放行 {cmd!r}", sev == S.OK, sev)

        # ---------- 2. 硬拒绝 ----------
        print("\n[2] 硬拒绝")
        block_cases = [
            ("format C:", "格式化"),
            ("Format D: /q", "格式化"),
            ("mkfs.ext4 /dev/sda1", "文件系统"),
            ("diskpart", "分区"),
            ("Clear-Disk -Number 0", "清除磁盘"),
            (":(){ :|:& };:", "fork"),
        ]
        for cmd, kw in block_cases:
            sev, reason = S.check_command(cmd)
            check(f"block {cmd!r}", sev == S.BLOCK and kw in reason, f"{sev} {reason}")

        # ---------- 3. 警告档 ----------
        print("\n[3] 警告档")
        warn_cases = [
            ("Remove-Item C:/x -Recurse", "递归删除"),
            ("rmdir /s C:/x", "递归删除"),
            ("del /f x.txt", "删除"),
            ("git push --force origin main", "强制推送"),
            ("git reset --hard HEAD~3", "硬重置"),
            ("reg delete HKLM\\Software\\x", "注册表"),
            ("Set-ExecutionPolicy Bypass", "执行策略"),
        ]
        for cmd, kw in warn_cases:
            sev, reason = S.check_command(cmd)
            check(f"warn {cmd!r}", sev == S.WARN and kw in reason, f"{sev} {reason}")

        # ---------- 4. 豁免标记 ----------
        print("\n[4] 豁免标记")
        sev, _ = S.check_command("Remove-Item C:/x -Recurse #danger-ok")
        check("warn 可被豁免", sev == S.OK, sev)

        # 豁免对 block 无效
        sev, _ = S.check_command("format C: #danger-ok")
        check("block 不可豁免", sev == S.BLOCK, sev)

        # ---------- 5. 空命令 ----------
        print("\n[5] 空命令")
        check("空串放行", S.check_command("")[0] == S.OK)
        check("空白放行", S.check_command("   ")[0] == S.OK)

        # ---------- 6. 拒绝文本 ----------
        print("\n[6] 拒绝文本")
        t1 = S.format_rejection(S.BLOCK, "格式化", "format C:")
        check("block 文本含不可执行", "不可执行" in t1, t1[:80])
        check("block 建议 ask_user", "ask_user" in t1, t1[:120])
        t2 = S.format_rejection(S.WARN, "递归删除", "rm -rf x")
        check("warn 文本含豁免标记", S.DANGER_OK in t2, t2[:120])

        # ---------- 7. 路径逃逸 ----------
        print("\n[7] 路径逃逸")
        work = tmp / "work"
        work.mkdir()
        (work / "inside.txt").write_text("x", encoding="utf-8")
        outside = tmp / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("x", encoding="utf-8")

        check("工作目录内放行", S.check_path_escape(str(work / "inside.txt"), str(work)) is None)
        check("工作目录本身放行", S.check_path_escape(str(work), str(work)) is None)
        r = S.check_path_escape(str(outside / "secret.txt"), str(work))
        check("逃逸被拦", r is not None and "之外" in r, str(r))

        # 前缀相同但不是子目录 —— 关键边界
        evil = Path(str(work) + "-evil")
        evil.mkdir()
        (evil / "x.txt").write_text("x", encoding="utf-8")
        r2 = S.check_path_escape(str(evil / "x.txt"), str(work))
        check("前缀相同但非子目录被拦", r2 is not None, str(r2))

        # allow 列表
        r3 = S.check_path_escape(str(outside / "secret.txt"), str(work), allow=(str(outside),))
        check("allow 列表放行", r3 is None, str(r3))

        # ---------- 8. 路径边界 ----------
        print("\n[8] 路径边界")
        check("空路径放行", S.check_path_escape("", str(work)) is None)
        check("空 work_dir 放行", S.check_path_escape("C:/x", "") is None)

    finally:
        import shutil
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
