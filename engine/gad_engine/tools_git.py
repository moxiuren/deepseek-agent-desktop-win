"""git 只读工具 —— status / diff / log。

为什么只有读没有写
------------------
写操作（commit / push / reset / checkout / clean）改工作区状态，
模型误调一次就可能丢工作、污染分支。真要写库走 `local_cmd`
（显式命令、可审计、有去有回）。本工具只回答「仓库现在什么样」。

共同约定（与 tools_fs 同构）
---------------------------
* 所有函数返回 `(ok, text)`：text 首行永远是状态行，后续才是内容
* 命令一律 argv 数组 + `shell=False`，不在 shell 里拼字符串
* `work_dir` 不存在 / 不是目录 / git 二进制缺失 / 超时 → `(False, 原因)`
* 非仓库目录 → `(False, ...)`，不抛异常
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

#: 单个 git 子进程超时（秒）。status/diff/log 都是本地操作，不应久等。
GIT_TIMEOUT = 30
#: diff 输出上限（字符）。超了截断并注明 —— 大 diff 灌进上下文会挤掉对话。
MAX_DIFF_CHARS = 12000
#: status 显示文件数上限。超了只显示前 N 个并注明总数。
MAX_STATUS_FILES = 200
#: log 默认条数与上限。
DEFAULT_LOG_N = 10
MAX_LOG_N = 50


def _run_git(args: list[str], work_dir: str) -> tuple[bool, str]:
    """跑一条 git 命令。返回 (成功, stdout/错误信息)。"""
    wd = Path(work_dir or "")
    if not str(wd) or not wd.is_dir():
        return False, f"[git] 工作目录不存在：{work_dir}"
    git = shutil.which("git")
    if not git:
        return False, "[git] 找不到 git 可执行文件（PATH 里没有）"
    try:
        proc = subprocess.run(
            [git, *args],
            cwd=str(wd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"[git] 超时（>{GIT_TIMEOUT}s）：git {' '.join(args)}"
    except OSError as exc:
        return False, f"[git] 启动失败：{exc}"
    out = (proc.stdout or "").replace("\r\n", "\n")
    if proc.returncode != 0:
        err = (proc.stderr or "").replace("\r\n", "\n").strip()
        if "not a git repository" in err:
            return False, f"[git] 不是 git 仓库：{work_dir}"
        detail = err or out.strip() or f"退出码 {proc.returncode}"
        return False, f"[git] 命令失败：git {' '.join(args)}\n{detail}"
    return True, out


def _inside_repo(path: str, work_dir: str) -> str | None:
    """绝对路径必须落在 work_dir 内，否则返回错误信息（防 `--` 路径逃逸）。

    相对路径先按 work_dir 解析 —— 调用方传的 `a.txt` 是相对仓库的，
    不能按进程当前目录 resolve（那会误判逃逸）。
    """
    p = Path(path)
    if not p.is_absolute():
        p = Path(work_dir) / p
    try:
        full = str(p.resolve())
        base = str(Path(work_dir).resolve())
    except OSError:
        return f"[git] 路径非法：{path}"
    if full != base and not full.startswith(base + os.sep):
        return f"[git] 路径超出工作目录，不执行：{path}"
    return None


def status(work_dir: str) -> tuple[bool, str]:
    """`git status --short --branch`：分支 + 改动列表。"""
    ok, out = _run_git(["status", "--short", "--branch"], work_dir)
    if not ok:
        return False, out
    lines = [ln for ln in out.split("\n") if ln.strip()]
    branch = ""
    files = []
    for ln in lines:
        if ln.startswith("##"):
            branch = ln[2:].strip()
        else:
            files.append(ln)
    head = f"[git:status] {branch or '(未知分支)'} · {len(files)} 处改动"
    if len(files) > MAX_STATUS_FILES:
        shown = files[:MAX_STATUS_FILES]
        return True, head + "\n" + "\n".join(shown) + f"\n（仅显示前 {MAX_STATUS_FILES} 个）"
    if not files:
        return True, head + "\n（工作区干净）"
    return True, head + "\n" + "\n".join(files)


def diff(work_dir: str, path: str = "", stat_only: bool = False) -> tuple[bool, str]:
    """`git diff`：未暂存改动。path 限单个路径；stat_only 只给统计。

    未跟踪（untracked）文件不在 diff 里 —— 看 status。暂存区（staged）
    的改动另起一段给出 stat。
    """
    if stat_only:
        ok, out = _run_git(["diff", "--stat"], work_dir)
        if not ok:
            return False, out
        return True, "[git:diff --stat]\n" + (out.strip() or "（无未暂存改动）")
    argv: list[str] = ["diff"]
    if path:
        err = _inside_repo(path, work_dir)
        if err:
            return False, err
        argv += ["--", path]
    ok, out = _run_git(argv, work_dir)
    if not ok:
        return False, out
    text = out.strip()
    if not text:
        text = "（该范围无未暂存改动；未跟踪文件看 git:status）"
    elif len(text) > MAX_DIFF_CHARS:
        text = text[:MAX_DIFF_CHARS] + f"\n（diff 共 {len(out.strip())} 字符，仅显示前 {MAX_DIFF_CHARS}）"
    return True, "[git:diff]\n" + text


def log(work_dir: str, n: int = DEFAULT_LOG_N) -> tuple[bool, str]:
    """`git log`：最近 n 条（1–50），一行一条：短 hash / 日期 / 作者 / 标题。"""
    try:
        count = int(n)
    except (TypeError, ValueError):
        count = DEFAULT_LOG_N
    count = max(1, min(count, MAX_LOG_N))
    ok, out = _run_git(
        ["log", f"-{count}", "--pretty=format:%h %ad %an %s", "--date=short"],
        work_dir,
    )
    if not ok:
        return False, out
    body = out.strip() or "（无提交）"
    return True, f"[git:log] 最近 {count} 条\n" + body
