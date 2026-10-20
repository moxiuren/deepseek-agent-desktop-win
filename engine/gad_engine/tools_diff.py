"""diff 生成 —— 编辑工具的「预览」能力。

为什么单独一层
--------------
三个编辑工具（replace_in_file / multi_edit / apply_patch）都需要在**落盘前**
把「要改什么」展示出来。diff 生成是纯函数（旧文本 → 新文本 → diff 字符串），
与编辑逻辑无关，所以抽出来单测。

用途
----
* `preview=1` 时：算出新内容 → 生成 diff → 返回给模型/用户看 → **不写盘**
* 正常编辑成功后：也在结果里附一小段 diff 摘要（让你看到确实改了什么）

设计取舍
--------
* 用标准库 `difflib`，不自己写 diff 算法 —— 自写的一定会有边界 bug。
* 展示用 diff **归一化换行**为 \\n（CRLF 在 diff 里显示成多余字符）。真正的
  写盘仍按文件原换行 —— 两者职责不同，互不影响。
* 输出有行数上限：一个大 diff 可能几百行，全塞进上下文没意义。
"""

from __future__ import annotations

import difflib

#: diff 展示的最大行数。超出截断 —— 大改动全塞进上下文是噪声。
MAX_DIFF_LINES = 200
#: 默认上下文行数。
DEFAULT_CONTEXT = 3


def _normalize_newlines(text: str) -> str:
    """把 CRLF / CR 统一成 \\n —— 仅供 diff 展示。"""
    return (text or "").replace("\r\n", "\n").replace("\r", "\n")


def unified_diff(
    old: str,
    new: str,
    path: str = "file",
    context: int = DEFAULT_CONTEXT,
    max_lines: int = MAX_DIFF_LINES,
) -> str:
    """生成 unified diff（展示用）。

    old == new 时返回空串（调用方据此判断「没有变化」）。
    """
    old_n = _normalize_newlines(old)
    new_n = _normalize_newlines(new)
    if old_n == new_n:
        return ""

    old_lines = old_n.splitlines(keepends=True)
    new_lines = new_n.splitlines(keepends=True)

    diff = list(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=context,
            lineterm="\n",
        )
    )
    text = "".join(diff).rstrip("\n")
    if not text:
        return ""

    lines = text.split("\n")
    if len(lines) > max_lines:
        head = "\n".join(lines[:max_lines])
        return f"{head}\n…（diff 还有 {len(lines) - max_lines} 行，已截断）"
    return text


def diff_stats(old: str, new: str) -> tuple[int, int]:
    """返回 (新增行数, 删除行数)。用于「+3 -1」这样的摘要。"""
    old_n = _normalize_newlines(old)
    new_n = _normalize_newlines(new)
    if old_n == new_n:
        return 0, 0
    added = removed = 0
    for line in difflib.unified_diff(
        old_n.splitlines(), new_n.splitlines(), n=0, lineterm=""
    ):
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return added, removed


def format_preview(path: str, diff: str) -> str:
    """把 diff 包成给模型/用户看的预览文本。"""
    return (
        f"[预览 · 未写入] {path}\n\n"
        f"{diff}\n\n"
        "以上是这次编辑**将要**做的改动，**尚未写入**。\n"
        "确认无误后，去掉 `preview=1` 重新调用即可应用。"
    )
