"""git 只读工具（`tools_git.py`）自测。

用法：
    cd engine && ./.venv/Scripts/python.exe tests/test_tools_git.py

说明：用真 git 在临时目录建仓库实测（`git init -b main` + 本地 `-c`
用户信息，不碰全局配置）。需要 PATH 里有 git。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gad_engine import tools_git as G
from gad_engine.protocol import ToolKind, classify

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
    else:
        FAIL.append(name + (f" — {detail}" if detail else ""))


def git(*args: str, cwd: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        timeout=60,
    )


def main() -> int:
    if not shutil.which("git"):
        print("SKIP：PATH 里没有 git")
        return 0

    tmp = Path(tempfile.mkdtemp(prefix="gad-git-test-"))
    try:
        # ---------- 1. 非仓库 ----------
        print("\n[1] 非仓库")
        ok, text = G.status(str(tmp))
        check("status 报不是仓库", not ok and "不是 git 仓库" in text, text[:80])

        # ---------- 2. 建仓 + 首次提交 ----------
        print("\n[2] 建仓")
        repo = str(tmp / "repo")
        Path(repo).mkdir()
        git("init", "-b", "main", cwd=repo)
        git("config", "user.email", "t@t.t", cwd=repo)
        git("config", "user.name", "t", cwd=repo)
        (Path(repo) / "a.txt").write_text("hello\n", encoding="utf-8")
        git("add", ".", cwd=repo)
        git("commit", "-m", "first", cwd=repo)

        ok, text = G.status(repo)
        check("干净仓库 ok", ok, text[:80])
        check("干净报工作区干净", "工作区干净" in text, text[:120])

        ok, text = G.log(repo)
        check("log 有 first", ok and "first" in text, text[:120])

        # ---------- 3. 脏改动 ----------
        print("\n[3] 脏改动")
        (Path(repo) / "a.txt").write_text("hello\nworld\n", encoding="utf-8")
        ok, text = G.status(repo)
        check("脏 status 提到 a.txt", ok and "a.txt" in text, text[:160])

        ok, text = G.diff(repo)
        check("diff 含 +world", ok and "+world" in text, text[:160])

        ok, text = G.diff(repo, "", True)
        check("diff --stat 有 a.txt", ok and "a.txt" in text, text[:160])

        ok, text = G.diff(repo, "a.txt")
        check("按路径 diff ok", ok and "+world" in text, text[:160])

        ok, text = G.diff(repo, "C:/Windows/not-here.txt")
        check("越界路径拒绝", not ok and "超出工作目录" in text, text[:80])

        # ---------- 4. log 条数 ----------
        print("\n[4] log 条数")
        ok, text = G.log(repo, 1)
        lines = [ln for ln in text.split("\n")[1:] if ln.strip()]
        check("n=1 只回一行", ok and len(lines) == 1, text[:120])
        ok, text = G.log(repo, 0)
        lines = [ln for ln in text.split("\n")[1:] if ln.strip()]
        check("n=0 钳到 1 行", ok and len(lines) == 1, text[:120])

        # ---------- 5. 分类 ----------
        print("\n[5] 分类")
        check("git:status", classify("git:status") == (ToolKind.GIT, "status"))
        check("git:log 5", classify("git:log 5") == (ToolKind.GIT, "log 5"))
        check("git:diff 带路径", classify("git:diff a.txt") == (ToolKind.GIT, "diff a.txt"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\nPASS {len(PASS)} / FAIL {len(FAIL)}")
    for f in FAIL:
        print("  FAIL:", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
