"""文件系统工具 —— read_file / replace_in_file / list_dir。

为什么单独一层
--------------
`local_cmd` 是 PowerShell，图灵完备，这些事它都能做。但「能做」不等于
「能可靠地做」：改一行代码用 shell 要同时处理正则元字符、编码、全局替换、
转义四件事，模型经常只做对三件。

本层把「定位 + 改一处」做成一个**原子操作**，把四件事收进代码。

三个工具的共同约定
------------------
* 路径一律走 pathlib，正反斜杠都接受（Windows 两种都常见）
* 文件编码统一按 UTF-8 读写。**不是 UTF-8 就明确报错，不猜** ——
  猜错编码去写文件，症状是「中文注释变乱码」，而这一步极难被归因
* 写回时**保留原编码的原貌**：无 BOM 的文件写回也不加 BOM
* 换行符**逐字节保留**：读写全程走 bytes，绝不经过 Python 的换行翻译 ——
  否则 Windows 上 `write_text` 会把 \\n 变成 \\r\\n，CRLF 文件被写成 \\r\\r\\n
  （这是实测踩到的坑）
* 输出给模型看的文本，第一行永远是状态（成功/失败 + 关键数字），
  后续才是内容。模型据此就能判断要不要重试

为什么 replace_in_file 用字面量而不是正则
------------------------------------------
代码里的美元符号、反斜杠、方括号、圆括号全是正则元字符。让模型在「改什么内容」
之上**再**正确地转义一层正则，是给它加一道它经常做错的题。字面量匹配不需要
转义，模型只需说出它**看见的原文**。
"""

from __future__ import annotations

import contextlib
import os
import re
from datetime import datetime
from pathlib import Path

from . import tools_diff

#: read_file 默认显示行数。
DEFAULT_READ_LIMIT = 200
#: read_file 单次上限 —— 防止模型一次把整个大文件灌进上下文。
MAX_READ_LIMIT = 2000
#: list_dir 默认递归深度。
DEFAULT_LIST_DEPTH = 2
#: list_dir 深度上限（与 executor 的扫描纪律一致）。
MAX_LIST_DEPTH = 4

#: replace_in_file 的 body 分隔标记（沿用 git 冲突标记，模型熟悉）。
MARK_OLD = "<<<<<<< OLD"
MARK_SEP = "======="
MARK_NEW = ">>>>>>> NEW"

#: 把「路径 + 末尾的 key=value 参数」拆开。
#: 只认**末尾连续的** kv —— 这样含空格的路径不会被误拆：
#: `C:/My Folder/x.py offset=10` -> ("C:/My Folder/x.py", {"offset":"10"})
_ARGS_TAIL_RE = re.compile(r"(\s+[A-Za-z_]\w*=\S*)+$")

_UTF8_BOM = b"\xef\xbb\xbf"


def split_path_args(spec: str) -> tuple[str, dict[str, str]]:
    """把 `C:/path/x.py offset=10 limit=50` 拆成 (路径, 参数字典)。"""
    s = (spec or "").strip()
    m = _ARGS_TAIL_RE.search(s)
    if not m:
        return s, {}
    path = s[: m.start()].strip()
    args: dict[str, str] = {}
    for kv in m.group(0).split():
        k, _, v = kv.partition("=")
        args[k.lower()] = v
    return path, args


def _to_int(args: dict[str, str], key: str, default: int) -> int:
    raw = args.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _read_text(path: Path) -> tuple[str | None, str, str]:
    """读文件为文本。返回 (文本, 编码, 错误信息)。

    编码探测**看实际字节**：只有真的带 BOM 才用 utf-8-sig。
    若一律用 utf-8-sig 探测，普通 UTF-8 文件也会「成功」，而写回时
    `utf-8-sig` 会**凭空加上 BOM** —— 这是实测踩到的坑（文件被改了无关的字节）。
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return None, "", f"读取错误：{exc}"

    if raw.startswith(_UTF8_BOM):
        try:
            return raw.decode("utf-8-sig"), "utf-8-sig", ""
        except UnicodeDecodeError:
            return None, "", "BOM 声称 UTF-8 但内容不是合法 UTF-8。"
    try:
        return raw.decode("utf-8"), "utf-8", ""
    except UnicodeDecodeError:
        return None, "", (
            "不是 UTF-8 编码（可能是 GBK / UTF-16）。当前工具只支持 UTF-8，"
            "请先用 local_cmd 转码后再改。"
        )


def _atomic_write(path: Path, text: str, encoding: str) -> str:
    """原子写：先写同目录临时文件再 os.replace。返回错误信息（成功为空串）。

    **必须走 write_bytes**：`Path.write_text` 在 Windows 上会把 \\n 翻译成
    \\r\\n，导致 CRLF 文件被写成 \\r\\r\\n、LF 文件悄悄变 CRLF。逐字节写
    才能保证「只改该改的，其余一个字节不动」。
    """
    tmp = path.with_name(path.name + ".gad-tmp")
    try:
        tmp.write_bytes(text.encode(encoding))
        os.replace(tmp, path)
        return ""
    except OSError as exc:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        return str(exc)
    except UnicodeEncodeError as exc:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        return f"编码回写失败：{exc}"


# --------------------------------------------------------------------------
# read_file
# --------------------------------------------------------------------------


def read_file(spec: str) -> tuple[bool, str]:
    """读文件片段（带行号）。

    spec 形如 `C:/path/x.py offset=100 limit=50`。offset 是**行号**（从 1 起）。
    """
    raw_path, args = split_path_args(spec)
    if not raw_path:
        return False, "[read_file 失败] 缺少路径。用法：read_file:<路径> [offset=N] [limit=M]"

    path = Path(raw_path).expanduser()
    if not path.exists():
        return False, f"[read_file 失败] 文件不存在：{path}"
    if path.is_dir():
        return False, f"[read_file 失败] 这是目录不是文件：{path}。请用 list_dir。"

    text, encoding, err = _read_text(path)
    if text is None:
        return False, f"[read_file 失败] {path}\n{err}"

    # splitlines 会吃掉行尾换行，用于展示正好；行号按显示顺序给。
    lines = text.splitlines()
    total = len(lines)

    offset = max(1, _to_int(args, "offset", 1))
    limit = max(1, min(_to_int(args, "limit", DEFAULT_READ_LIMIT), MAX_READ_LIMIT))
    start = min(offset - 1, total)
    end = min(start + limit, total)

    width = len(str(total)) if total else 1
    body = "\n".join(
        f"{start + i + 1:>{width}}\t{ln}" for i, ln in enumerate(lines[start:end])
    )

    head = (
        f"[read_file 成功] {path}\n"
        f"共 {total} 行 · 显示 {start + 1}-{end} 行 · 编码 {encoding}"
    )
    if end < total:
        head += f"\n（还有 {total - end} 行未显示，用 offset={end + 1} 继续读）"
    return True, f"{head}\n\n{body}" if body else f"{head}\n\n(空文件)"


# --------------------------------------------------------------------------
# replace_in_file
# --------------------------------------------------------------------------


def parse_edit_body(body: str) -> tuple[str, str] | None:
    """从围栏 body 里解析 OLD / NEW 两段。失败返回 None。

    期望格式（三个标记各占一行）：
        <<<<<<< OLD
        ...原文...
        =======
        ...新文...
        >>>>>>> NEW
    """
    lines = (body or "").splitlines()
    oi = si = ni = -1
    for i, ln in enumerate(lines):
        s = ln.strip()
        if oi < 0:
            if s == MARK_OLD:
                oi = i
        elif si < 0:
            if s == MARK_SEP:
                si = i
        elif ni < 0 and s == MARK_NEW:
            ni = i
            break
    if oi < 0 or si < 0 or ni < 0:
        return None
    old = "\n".join(lines[oi + 1 : si])
    new = "\n".join(lines[si + 1 : ni])
    return old, new


def _match_newline(text: str) -> str:
    """探测文本的主换行风格。"""
    return "\r\n" if "\r\n" in text else "\n"


def _to_newline(s: str, newline: str) -> str:
    """把任意换行风格规整成目标风格（先统一成 \\n 再套目标）。"""
    return s.replace("\r\n", "\n").replace("\r", "\n").replace("\n", newline)


def replace_in_file(spec: str, body: str) -> tuple[bool, str]:
    """在文件里做**字面量**替换。

    spec 形如 `C:/path/x.py [count=N] [expect=M]`：
      * `count`  替换几处，默认 1；`count=0` 表示全部替换
      * `expect` 期望原文出现几次（用于防止「以为只有一处其实有七处」）
    body 见 `parse_edit_body`。

    **原文本一个字节都不动，只替换命中的那段**：读进来是什么换行风格，
    写回去还是什么风格。文件里其余部分的字节保持原样。
    """
    raw_path, args = split_path_args(spec)
    if not raw_path:
        return False, "[replace_in_file 失败] 缺少路径。"

    parsed = parse_edit_body(body)
    if parsed is None:
        return False, (
            "[replace_in_file 失败] body 格式不对。需要三个标记各占一行：\n"
            f"{MARK_OLD}\n<原文>\n{MARK_SEP}\n<新文>\n{MARK_NEW}"
        )
    old, new = parsed
    if old == "":
        return False, "[replace_in_file 失败] 原文为空 —— 空串会匹配到文件开头，拒绝执行。"

    path = Path(raw_path).expanduser()
    if not path.exists():
        return False, f"[replace_in_file 失败] 文件不存在：{path}"
    if path.is_dir():
        return False, f"[replace_in_file 失败] 这是目录不是文件：{path}"

    text, encoding, err = _read_text(path)
    if text is None:
        return False, f"[replace_in_file 失败] {path}\n{err}"

    newline = _match_newline(text)
    needle = _to_newline(old, newline)
    repl = _to_newline(new, newline)

    n = text.count(needle)
    if n == 0:
        return False, (
            f"[replace_in_file 失败] 未找到要替换的原文（{path}）。\n"
            "可能是：内容已经改过 / 有不可见空白差异 / 换行符不一致。\n"
            "建议先用 read_file 确认当前内容的准确写法。"
        )

    expect_raw = args.get("expect")
    if expect_raw not in (None, ""):
        try:
            expect = int(expect_raw)
        except ValueError:
            expect = -1
        if expect >= 0 and n != expect:
            return False, (
                f"[replace_in_file 失败] 原文出现 {n} 次，与 expect={expect} 不符。"
                "为避免改错位置，已中止。请用 read_file 核对后重试。"
            )

    count = _to_int(args, "count", 1)
    if count <= 0:
        result = text.replace(needle, repl)
        replaced = n
    else:
        result = text.replace(needle, repl, count)
        replaced = min(count, n)

    # preview=1：算出改动但**不写盘**，返回 diff 供确认。
    if str(args.get("preview", "")).lower() in ("1", "true", "yes"):
        diff = tools_diff.unified_diff(text, result, path.name)
        return True, tools_diff.format_preview(str(path), diff)

    werr = _atomic_write(path, result, encoding)
    if werr:
        return False, f"[replace_in_file 失败] 写入错误：{werr}"

    note = ""
    if n > replaced:
        note = f"\n（原文共出现 {n} 次，只替换了前 {replaced} 次；全部替换用 count=0）"
    # 成功时附一行 diff 摘要（+X -Y），让你看到确实改了什么。
    added, removed = tools_diff.diff_stats(text, result)
    stat = f" · +{added} -{removed}" if (added or removed) else ""
    return True, f"[replace_in_file 成功] {path} · 替换 {replaced} 处{stat}{note}"


# --------------------------------------------------------------------------
# list_dir
# --------------------------------------------------------------------------


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    for unit in ("K", "M", "G"):
        n /= 1024.0
        if n < 1024 or unit == "G":
            return f"{n:.1f}{unit}"
    return f"{n:.1f}G"


def _fmt_time(ts: float) -> str:
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except (OSError, ValueError):
        return "?"


def list_dir(spec: str) -> tuple[bool, str]:
    """列目录（树形，带大小与修改时间）。

    spec 形如 `C:/path depth=2`。depth 上限 MAX_LIST_DEPTH。
    """
    raw_path, args = split_path_args(spec)
    if not raw_path:
        raw_path = "."
    path = Path(raw_path).expanduser()
    if not path.exists():
        return False, f"[list_dir 失败] 路径不存在：{path}"
    if not path.is_dir():
        return False, f"[list_dir 失败] 不是目录：{path}。请用 read_file。"

    depth = max(0, min(_to_int(args, "depth", DEFAULT_LIST_DEPTH), MAX_LIST_DEPTH))

    lines: list[str] = []
    dir_count = 0
    file_count = 0

    def walk(cur: Path, level: int, prefix: str) -> None:
        nonlocal dir_count, file_count
        try:
            entries = sorted(
                cur.iterdir(), key=lambda p: (p.is_file(), p.name.lower())
            )
        except OSError as exc:
            lines.append(f"{prefix}[无法读取: {exc}]")
            return
        for i, entry in enumerate(entries):
            last = i == len(entries) - 1
            branch = "`-- " if last else "|-- "
            try:
                st = entry.stat()
                size = _fmt_size(st.st_size) if entry.is_file() else "<dir>"
                when = _fmt_time(st.st_mtime)
            except OSError:
                size, when = "?", "?"
            if entry.is_dir():
                dir_count += 1
                lines.append(f"{prefix}{branch}{entry.name}/  {when}")
                if level < depth:
                    walk(entry, level + 1, prefix + ("    " if last else "|   "))
            else:
                file_count += 1
                lines.append(f"{prefix}{branch}{entry.name}  {size}  {when}")

    walk(path, 0, "")
    head = (
        f"[list_dir 成功] {path}\n"
        f"深度 {depth} · 目录 {dir_count} 个 · 文件 {file_count} 个"
    )
    tree = "\n".join(lines) if lines else "(空目录)"
    return True, f"{head}\n\n{tree}"
