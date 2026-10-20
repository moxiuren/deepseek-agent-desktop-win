"""多编辑工具 —— multi_edit（单文件多处）/ apply_patch（多文件 unified diff）。

它们与 replace_in_file 的区别
------------------------------
  * `replace_in_file` —— 单文件、单处（或同串多处），一次落盘
  * `multi_edit`      —— **单文件、多处不同**的编辑，一次落盘
  * `apply_patch`     —— **多文件**，unified diff 格式

**为什么多编辑要一次落盘（原子性）**：连续调 N 次 replace_in_file，若第 3 次
失败，前 2 次已经写进磁盘了 —— 文件处于一个「改了一半」的中间态，可能语法都
不成立。multi_edit / apply_patch 先把所有编辑在内存里试算，**全部成功才写**，
任一处失败则整体不动。这是它们的核心价值，不是「省几次调用」。

复用 tools_fs 的底层
---------------------
编码探测、换行处理、原子写 —— 全部复用 `tools_fs` 的实现，不重写。
这样三个工具的「写文件」语义完全一致（都逐字节写、都保留 CRLF、都不加 BOM）。

apply_patch 的格式
------------------
标准 unified diff（`git diff` 的输出格式）。模型对这个格式很熟。
支持多文件、每个文件多个 hunk。**只做「修改已有文件」** —— 新增/删除文件
由 write_file / delete_path 负责，不在这里混。

诚实边界
--------
* 不解析 diff 的「上下文行号」做模糊定位 —— 按**精确内容**匹配 hunk 的
  上下文与增删行。行号只用于报错定位，不用于匹配（行号会因前面的 hunk 而漂移）。
* 不做三方合并 —— 冲突就是冲突，明确报错。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import tools_diff
from .tools_fs import (
    MARK_NEW,
    MARK_OLD,
    MARK_SEP,
    _atomic_write,
    _match_newline,
    _read_text,
    _to_newline,
    parse_edit_body,
    split_path_args,
)

#: multi_edit 的 body 里，多个编辑块之间的分隔标记。
EDIT_SEP = "===== EDIT ====="

#: 单次 multi_edit / apply_patch 允许的最大编辑数 —— 防止一个巨大 patch 卡住。
MAX_EDITS = 100


# --------------------------------------------------------------------------
# multi_edit
# --------------------------------------------------------------------------


def parse_multi_edit_body(body: str) -> list[tuple[str, str]] | None:
    """把 multi_edit 的 body 解析成多个 (old, new) 对。

    格式：多个 replace_in_file 风格的三段块，用 `===== EDIT =====` 分隔。

        <<<<<<< OLD
        ...原文1...
        =======
        ...新文1...
        >>>>>>> NEW
        ===== EDIT =====
        <<<<<<< OLD
        ...原文2...
        =======
        ...新文2...
        >>>>>>> NEW

    只写一个块（没有分隔符）也合法 —— 退化成单处编辑。
    """
    text = (body or "").strip()
    if not text:
        return None
    # 按分隔标记切开
    chunks = re.split(r"^\s*" + re.escape(EDIT_SEP) + r"\s*$", text, flags=re.MULTILINE)
    edits: list[tuple[str, str]] = []
    for chunk in chunks:
        parsed = parse_edit_body(chunk)
        if parsed is None:
            return None
        old, new = parsed
        if old == "":
            return None  # 空原文会匹配到文件开头，拒绝
        edits.append((old, new))
    return edits or None


def multi_edit(spec: str, body: str) -> tuple[bool, str]:
    """在一个文件里做**多处**编辑，原子落盘。

    spec 形如 `C:/path/x.py [expect=每个块各出现次数]`（expect 对每个块单独生效）。
    body 见 `parse_multi_edit_body`。

    **顺序执行**：第 2 个块的匹配发生在第 1 个块应用**之后**的文本上。
    这与「读一次原文、各自独立替换」不同 —— 后者在块之间内容重叠时会出错。
    顺序语义更符合直觉（就是「从上到下依次改」）。
    """
    raw_path, args = split_path_args(spec)
    if not raw_path:
        return False, "[multi_edit 失败] 缺少路径。"

    edits = parse_multi_edit_body(body)
    if edits is None:
        return False, (
            "[multi_edit 失败] body 格式不对。需要一或多个三段块，"
            f"块之间用 `{EDIT_SEP}` 分隔。单个块格式：\n"
            f"{MARK_OLD}\n<原文>\n{MARK_SEP}\n<新文>\n{MARK_NEW}"
        )
    if len(edits) > MAX_EDITS:
        return False, f"[multi_edit 失败] 编辑数 {len(edits)} 超过上限 {MAX_EDITS}。"

    path = Path(raw_path).expanduser()
    if not path.exists():
        return False, f"[multi_edit 失败] 文件不存在：{path}"
    if path.is_dir():
        return False, f"[multi_edit 失败] 这是目录不是文件：{path}"

    text, encoding, err = _read_text(path)
    if text is None:
        return False, f"[multi_edit 失败] {path}\n{err}"

    newline = _match_newline(text)
    current = text
    applied = 0

    for idx, (old, new) in enumerate(edits, 1):
        needle = _to_newline(old, newline)
        repl = _to_newline(new, newline)
        n = current.count(needle)
        if n == 0:
            return False, (
                f"[multi_edit 失败] 第 {idx} 个块未找到原文（{path}）。\n"
                "**整个编辑未落盘** —— 文件保持原样。\n"
                "可能是：内容已改过 / 有不可见空白差异 / 前一个块改动了这段。\n"
                "建议用 read_file 确认当前内容。"
            )
        if n > 1:
            return False, (
                f"[multi_edit 失败] 第 {idx} 个块原文出现 {n} 次（{path}）。\n"
                "**整个编辑未落盘**。multi_edit 要求每个块的原文唯一，"
                "否则无法确定改哪一处。请把原文写得更具体。"
            )
        current = current.replace(needle, repl, 1)
        applied += 1

    # preview=1：算改动但不写盘
    if str(args.get("preview", "")).lower() in ("1", "true", "yes"):
        diff = tools_diff.unified_diff(text, current, path.name)
        return True, tools_diff.format_preview(str(path), diff)

    werr = _atomic_write(path, current, encoding)
    if werr:
        return False, f"[multi_edit 失败] 写入错误：{werr}"

    added, removed = tools_diff.diff_stats(text, current)
    stat = f" · +{added} -{removed}" if (added or removed) else ""
    return True, f"[multi_edit 成功] {path} · 应用 {applied} 处编辑{stat}"


# --------------------------------------------------------------------------
# apply_patch
# --------------------------------------------------------------------------


@dataclass
class Hunk:
    """一个 diff hunk。"""

    #: 旧文件里的起始行（仅用于报错定位）。
    old_start: int
    old_lines: list[str] = field(default_factory=list)   # 上下文 + 删除行（前缀 ' ' 或 '-'）
    new_lines: list[str] = field(default_factory=list)   # 上下文 + 新增行（前缀 ' ' 或 '+'）


@dataclass
class FilePatch:
    """一个文件的改动。"""

    path: str
    hunks: list[Hunk] = field(default_factory=list)


_HEADER_RE = re.compile(r"^---\s+(.+?)(?:\t.*)?$")
_PLUS_RE = re.compile(r"^\+\+\+\s+(.+?)(?:\t.*)?$")
_HUNK_RE = re.compile(r"^@@\s+-(\d+)(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s+@@")


def parse_unified_diff(text: str) -> list[FilePatch] | None:
    """解析 unified diff。失败返回 None。

    识别 `--- a/path` / `+++ b/path` / `@@ -N,M +N,M @@`。
    `---` / `+++` 后面的 `a/` `b/` 前缀会被剥掉。
    """
    lines = (text or "").splitlines()
    patches: list[FilePatch] = []
    i = 0
    current_file: FilePatch | None = None
    current_hunk: Hunk | None = None

    def _clean(p: str) -> str:
        p = p.strip()
        if p in ("/dev/null",):
            return ""
        if p.startswith(("a/", "b/")):
            return p[2:]
        return p

    while i < len(lines):
        line = lines[i]

        m_old = _HEADER_RE.match(line)
        if m_old and i + 1 < len(lines) and _PLUS_RE.match(lines[i + 1]):
            # 新文件开始 —— 收尾上一个 hunk
            if current_hunk is not None and current_file is not None:
                current_file.hunks.append(current_hunk)
                current_hunk = None
            new_path = _clean(_PLUS_RE.match(lines[i + 1]).group(1))  # type: ignore[union-attr]
            old_path = _clean(m_old.group(1))
            current_file = FilePatch(path=new_path or old_path)
            patches.append(current_file)
            i += 2
            continue

        m_hunk = _HUNK_RE.match(line)
        if m_hunk:
            if current_file is None:
                return None  # hunk 出现在文件头之前 —— 格式错
            if current_hunk is not None:
                current_file.hunks.append(current_hunk)
            current_hunk = Hunk(old_start=int(m_hunk.group(1)))
            i += 1
            continue

        if current_hunk is not None:
            if line.startswith("+"):
                current_hunk.new_lines.append(line[1:])
            elif line.startswith("-"):
                current_hunk.old_lines.append(line[1:])
            elif line.startswith(" "):
                current_hunk.old_lines.append(line[1:])
                current_hunk.new_lines.append(line[1:])
            elif line.startswith("\\"):
                pass  # "\ No newline at end of file" —— 忽略
            # 空行：可能是 hunk 内的空上下文行（某些 diff 省略前导空格）
            elif line == "":
                current_hunk.old_lines.append("")
                current_hunk.new_lines.append("")
        i += 1

    if current_hunk is not None and current_file is not None:
        current_file.hunks.append(current_hunk)

    return patches or None


def _hunk_old_block(hunk: Hunk) -> str:
    """hunk 的「旧内容」——上下文 + 删除行。"""
    return "\n".join(hunk.old_lines)


def _hunk_new_block(hunk: Hunk) -> str:
    """hunk 的「新内容」——上下文 + 新增行。"""
    return "\n".join(hunk.new_lines)


#: apply_patch 的 spec 只有 `root=` 一个参数。**不能复用 split_path_args** ——
#: 那个要求 key=value 前有空白，而这里整个 spec 就是 `root=...`（无位置参数），
#: 实测它会返回 ('root=C:/x', {})，root 整个丢失 → 文件找不到。
#: 同一个坑在 run_tests 的 cmd= 上踩过一次（见 tools_tests.py）。
_ROOT_RE = re.compile(r"(?:^|\s)root=")
#: 下一个 key=value 的起点（用于让 root 的值在它之前停下）。
_NEXT_KEY_RE = re.compile(r"\s+[A-Za-z_]\w*=")
#: preview 开关。
_PREVIEW_RE = re.compile(r"(?:^|\s)preview=(?:1|true|yes)", re.IGNORECASE)


def _parse_root(spec: str) -> str:
    """解析 apply_patch 的 root=。

    值从 `root=` 到**下一个 ` key=` 之前**（或结尾）。路径可含空格，
    但不能把后面的 `preview=1` 一起吞进去 —— 早先取到结尾，于是
    `root=C:/x preview=1` 的 root 变成了 `C:/x preview=1`（路径不存在）。
    """
    s = (spec or "").strip()
    m = _ROOT_RE.search(s)
    if not m:
        return ""
    rest = s[m.end():]
    # 在下一个 ` key=` 处截断
    nxt = _NEXT_KEY_RE.search(rest)
    return (rest[: nxt.start()] if nxt else rest).strip()


def _has_preview(spec: str) -> bool:
    """apply_patch 的 preview 开关。"""
    return bool(_PREVIEW_RE.search(spec or ""))


def apply_patch(spec: str, body: str) -> tuple[bool, str]:
    """应用一个 unified diff，改多个文件，原子落盘。

    spec 形如 `apply_patch [root=基准目录]`。body 是 diff 正文。
    文件路径相对于 root（默认当前工作目录）。

    **原子性**：所有文件的所有 hunk 先在内存里试算，全部成功才写盘。
    任一处失败 → 一个文件都不动。
    """
    root_arg = _parse_root(spec)
    root = Path(root_arg).expanduser() if root_arg else Path(".")

    patches = parse_unified_diff(body)
    if not patches:
        return False, (
            "[apply_patch 失败] 没解析出有效的 diff。\n"
            "需要标准 unified diff 格式（`--- a/x` / `+++ b/x` / `@@ ... @@`）。"
        )

    total_hunks = sum(len(p.hunks) for p in patches)
    if total_hunks > MAX_EDITS:
        return False, f"[apply_patch 失败] hunk 数 {total_hunks} 超过上限 {MAX_EDITS}。"
    if total_hunks == 0:
        return False, "[apply_patch 失败] diff 里没有任何 hunk。"

    # ---------- 第一阶段：全部试算，不写盘 ----------
    results: list[tuple[Path, str, str]] = []  # (path, 新内容, 编码)
    for fp in patches:
        if not fp.path:
            return False, "[apply_patch 失败] 某个文件头缺少路径。"
        target = (root / fp.path).resolve()
        # 简单的逃逸检查 —— 详细策略由 agent 的 safety 层负责，这里兜底
        try:
            target.relative_to(root.resolve())
        except ValueError:
            return False, f"[apply_patch 失败] 路径逃出基准目录：{fp.path}"

        if not target.exists():
            return False, (
                f"[apply_patch 失败] 文件不存在：{fp.path}\n"
                "（apply_patch 只改已有文件；新增文件请用 write_file）"
            )
        if target.is_dir():
            return False, f"[apply_patch 失败] 这是目录不是文件：{fp.path}"

        text, encoding, err = _read_text(target)
        if text is None:
            return False, f"[apply_patch 失败] {fp.path}\n{err}"

        newline = _match_newline(text)
        current = text

        for hidx, hunk in enumerate(fp.hunks, 1):
            old_block = _to_newline(_hunk_old_block(hunk), newline)
            new_block = _to_newline(_hunk_new_block(hunk), newline)
            if old_block == "":
                return False, (
                    f"[apply_patch 失败] {fp.path} 第 {hidx} 个 hunk 的旧内容为空，拒绝执行。"
                )
            n = current.count(old_block)
            if n == 0:
                return False, (
                    f"[apply_patch 失败] {fp.path} 第 {hidx} 个 hunk 未匹配到内容"
                    f"（原 diff 标记的行号 {hunk.old_start}）。\n"
                    "**所有文件都未改动**。可能是文件已变，或上下文行不全。\n"
                    "建议先 read_file 确认当前内容。"
                )
            if n > 1:
                return False, (
                    f"[apply_patch 失败] {fp.path} 第 {hidx} 个 hunk 匹配到 {n} 处"
                    "（不唯一）。**所有文件都未改动**。\n"
                    "请在 diff 里包含更多上下文行以消除歧义。"
                )
            current = current.replace(old_block, new_block, 1)

        results.append((target, current, encoding))

    # ---------- 第二阶段（preview）：算 diff 但不写盘 ----------
    if _has_preview(spec):
        blocks: list[str] = []
        for target, content, _enc in results:
            old_text, _e, _err = _read_text(target)
            diff = tools_diff.unified_diff(old_text or "", content, target.name)
            if diff:
                blocks.append(diff)
        body_text = "\n\n".join(blocks) if blocks else "(无变化)"
        return True, (
            f"[预览 · 未写入] 将改动 {len(results)} 个文件 · {total_hunks} 个 hunk\n\n"
            f"{body_text}\n\n"
            "以上是这次 patch **将要**做的改动，**尚未写入**。\n"
            "确认无误后，去掉 `preview=1` 重新调用即可应用。"
        )

    # ---------- 第二阶段：全部成功，一次性写盘 ----------
    for target, content, encoding in results:
        werr = _atomic_write(target, content, encoding)
        if werr:
            return False, (
                f"[apply_patch 失败] 写入 {target} 出错：{werr}\n"
                "注意：在此之前已写入的文件不会回滚 —— 可能出现部分应用。"
            )

    files_changed = ", ".join(str(t.name) for t, _, _ in results)
    return True, (
        f"[apply_patch 成功] 改动 {len(results)} 个文件 · {total_hunks} 个 hunk\n"
        f"{files_changed}"
    )
