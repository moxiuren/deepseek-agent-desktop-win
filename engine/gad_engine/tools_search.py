"""检索工具 —— grep（按内容）/ glob（按文件名）。

为什么需要它们
--------------
`list_dir` 给的是「有什么」，但看懂一个陌生代码库靠的是「哪里用到了 X」。
用 `local_cmd` 跑 `Select-String` 能做，但每次都要模型自己拼 PowerShell 参数、
自己处理编码、自己过滤 node_modules —— 而这些是**每次都要做对**的，
不该压在模型身上。

与 tools_fs 的关系
------------------
tools_fs 管**单个文件/目录**，本模块管**遍历一棵树找东西**。
两者都返回 (ok, text)，接线方式完全一致。

零依赖
------
纯 Python（re + pathlib + os.walk），不依赖 ripgrep / fd。
代价是大目录树下比原生工具慢，所以有**结果数上限**和**文件大小上限**兜底 ——
宁可少给几条，也不让一次搜索把引擎卡住。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from .tools_fs import _read_text, _to_int, split_path_args

#: grep 默认最多返回多少条命中。
DEFAULT_GREP_MAX = 100
#: grep 单次上限。
MAX_GREP_MAX = 1000
#: 单个文件超过这么大就跳过（避免在一个巨大的日志/压缩包上卡死）。
MAX_FILE_BYTES = 2 * 1024 * 1024
#: glob 默认 / 上限。
DEFAULT_GLOB_MAX = 200
MAX_GLOB_MAX = 2000

#: 遍历时跳过的目录名（任何层级）。
SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build",
    ".next", ".nuxt", "target", "bin", "obj", ".idea", ".vs",
})

#: 明显是二进制的扩展名 —— 直接跳过，不读内容。
BINARY_EXT = frozenset({
    ".exe", ".dll", ".so", ".dylib", ".bin", ".pyc", ".pyo", ".class",
    ".zip", ".gz", ".tar", ".7z", ".rar", ".jar", ".war",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".svg",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".mp3", ".mp4", ".avi", ".mkv", ".mov", ".wav", ".flac",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".db", ".sqlite", ".sqlite3", ".lock",
})


def _iter_files(root: Path, glob_pat: str):
    """遍历 root 下的文件，跳过 SKIP_DIRS 与二进制扩展名。

    用 os.walk（而不是 Path.rglob）是为了**能剪枝** —— rglob 会走进
    node_modules 再一层层往下，即使你根本不想要那些结果。
    """
    for dirpath, dirnames, filenames in os.walk(root):
        # 原地改 dirnames 即可剪枝 —— 这是 os.walk 的标准用法。
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if Path(name).suffix.lower() in BINARY_EXT:
                continue
            if glob_pat and glob_pat != "*" and not Path(name).match(glob_pat):
                continue
            yield Path(dirpath) / name


def _compile_pattern(pattern: str, fixed: bool, ignore_case: bool) -> re.Pattern | None:
    flags = re.IGNORECASE if ignore_case else 0
    try:
        if fixed:
            return re.compile(re.escape(pattern), flags)
        return re.compile(pattern, flags)
    except re.error:
        return None


def grep(spec: str) -> tuple[bool, str]:
    """按内容搜索。

    spec 形如 `grep:<正则> [path=... glob=... max=N fixed=1 ignore_case=1]`。

    **fixed=1 时按字面量匹配** —— 搜 `$foo` / `a[0]` 这类含元字符的串时，
    模型不必自己转义。这是刻意提供的出口，与 replace_in_file 同一个理由。
    """
    raw, args = split_path_args(spec)
    if not raw:
        return False, "[grep 失败] 缺少搜索模式。用法：grep:<正则> [path=... glob=...]"

    pattern = raw
    root = Path(args.get("path", ".")).expanduser()
    if not root.exists():
        return False, f"[grep 失败] 搜索路径不存在：{root}"
    if not root.is_dir():
        return False, f"[grep 失败] 搜索路径不是目录：{root}"

    fixed = args.get("fixed", "").lower() in ("1", "true", "yes")
    ignore_case = args.get("ignore_case", "").lower() in ("1", "true", "yes")
    max_hits = max(1, min(_to_int(args, "max", DEFAULT_GREP_MAX), MAX_GREP_MAX))
    glob_pat = args.get("glob", "")

    rx = _compile_pattern(pattern, fixed, ignore_case)
    if rx is None:
        return False, f"[grep 失败] 正则表达式无效：{pattern!r}（可加 fixed=1 按字面量搜）"

    hits: list[str] = []
    files_scanned = 0
    truncated = False

    for fp in _iter_files(root, glob_pat):
        try:
            if fp.stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        text, _enc, _err = _read_text(fp)
        if text is None:
            continue
        files_scanned += 1
        for lineno, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                rel = _rel(fp, root)
                hits.append(f"{rel}:{lineno}: {line.strip()[:240]}")
                if len(hits) >= max_hits:
                    truncated = True
                    break
        if truncated:
            break

    head = (
        f"[grep 成功] 模式 {pattern!r} · 扫描 {files_scanned} 个文件 · "
        f"命中 {len(hits)} 条"
    )
    if truncated:
        head += f"（已达上限 {max_hits}，用 max=N 放宽）"
    if not hits:
        return True, f"{head}\n\n(无命中)"
    return True, f"{head}\n\n" + "\n".join(hits)


def _rel(fp: Path, root: Path) -> str:
    try:
        return str(fp.relative_to(root))
    except ValueError:
        return str(fp)


def glob_search(spec: str) -> tuple[bool, str]:
    """按文件名/路径模式搜索。

    spec 形如 `glob:<模式> [root=... max=N]`，模式如 `**/*.py` 或 `*.md`。
    没有 `**` 时按「文件名匹配」处理（等价于 grep 的 glob 参数）。
    """
    raw, args = split_path_args(spec)
    if not raw:
        return False, "[glob 失败] 缺少模式。用法：glob:**/*.py [root=...]"

    root = Path(args.get("root", ".")).expanduser()
    if not root.exists():
        return False, f"[glob 失败] 根目录不存在：{root}"
    if not root.is_dir():
        return False, f"[glob 失败] 根目录不是目录：{root}"

    max_hits = max(1, min(_to_int(args, "max", DEFAULT_GLOB_MAX), MAX_GLOB_MAX))

    hits: list[str] = []
    truncated = False

    # 简化处理：把模式拆成「是否含目录部分」。
    # 含 `/` 或 `**` → 按相对路径匹配；否则只匹配文件名。
    by_path = ("/" in raw) or ("**" in raw)

    for fp in _iter_files(root, "" if by_path else raw):
        if by_path:
            rel = _rel(fp, root).replace(os.sep, "/")
            if not Path(rel).match(raw) and not _dblstar_match(rel, raw):
                continue
        hits.append(_rel(fp, root).replace(os.sep, "/"))
        if len(hits) >= max_hits:
            truncated = True
            break

    head = f"[glob 成功] 模式 {raw!r} · 命中 {len(hits)} 个文件"
    if truncated:
        head += f"（已达上限 {max_hits}）"
    if not hits:
        return True, f"{head}\n\n(无匹配)"
    return True, f"{head}\n\n" + "\n".join(sorted(hits))


def _dblstar_match(rel: str, pattern: str) -> bool:
    """`**/` 的匹配（pathlib.match 对 `**/x` 的处理不一致，自己兜一层）。"""
    if pattern.startswith("**/"):
        tail = pattern[3:]
        if Path(rel).match(tail):
            return True
        if rel.endswith(tail.lstrip("*")):
            return True
    return False
