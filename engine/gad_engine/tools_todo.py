"""任务清单 —— todo:write / todo:read。

为什么需要它
------------
长任务里模型容易「跑着跑着忘了最初要干嘛」。把计划**外化**成一份可读可改的
清单，比让它记在上下文里可靠得多 —— 清单在磁盘上，切会话、压缩、重启都不丢。

为什么按对话隔离
----------------
`todo` 是**当前这件事**的进度，不是跨会话的长期事实（那个是 memory）。
所以每个对话一份，文件按会话 id 命名。这样开新对话就是新清单，
切回旧对话又能看到当时的进度。

为什么不放数据库
----------------
与 memory 同理：Markdown 文件用户能直接打开看、手改、纳入版本管理。
一个「AI 记的待办」如果只有工具能读，出问题时连诊断都没法诊断。
"""

from __future__ import annotations

import contextlib
import os
import re
from pathlib import Path

#: 清单文件大小上限 —— 清单不该无限长，长了说明该拆任务了。
MAX_TODO_CHARS = 8000

#: 勾选状态的正则：`- [ ] xxx` / `- [x] xxx`
_ITEM_RE = re.compile(r"^\s*[-*+]\s*\[( |x|X)\]\s*(.+?)\s*$")


def todo_dir(app_data: Path) -> Path:
    """清单存放目录。"""
    d = app_data / "todos"
    d.mkdir(parents=True, exist_ok=True)
    return d


def todo_path_for(app_data: Path, conversation_id: str) -> Path:
    """某条对话的清单文件路径。

    conversation_id 理论上是我们自己生成的（`conv-xxxx` 形态），但仍做一次
    清洗 —— 万一将来 id 来源变了，不该出现路径穿越。
    """
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", conversation_id or "default")
    return todo_dir(app_data) / f"{safe}.md"


def _atomic_write(path: Path, text: str) -> str:
    tmp = path.with_name(path.name + ".gad-tmp")
    try:
        tmp.write_bytes(text.encode("utf-8"))
        os.replace(tmp, path)
        return ""
    except OSError as exc:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        return str(exc)


def summarize_progress(text: str) -> tuple[int, int]:
    """数出 (已完成, 总数)。用于状态行 —— 一眼看到进度。"""
    done = total = 0
    for line in (text or "").splitlines():
        m = _ITEM_RE.match(line)
        if m:
            total += 1
            if m.group(1).lower() == "x":
                done += 1
    return done, total


def todo_write(path: Path, body: str) -> tuple[bool, str]:
    """覆盖写入清单。body 是 markdown 正文。"""
    text = (body or "").strip()
    if not text:
        return False, "[todo:write 失败] 内容为空。至少写一条 `- [ ] 任务`。"

    if len(text) > MAX_TODO_CHARS:
        return False, (
            f"[todo:write 失败] 清单过长（{len(text)} 字符，上限 {MAX_TODO_CHARS}）。"
            "请精简，或把大任务拆成多个对话。"
        )

    if not path.parent.exists():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return False, f"[todo:write 失败] 无法创建目录：{exc}"

    err = _atomic_write(path, text + "\n")
    if err:
        return False, f"[todo:write 失败] 写入错误：{err}"

    done, total = summarize_progress(text)
    return True, f"[todo:write 成功] 已更新清单（{done}/{total} 完成）\n{path}"


def todo_read(path: Path) -> tuple[bool, str]:
    """读取清单。不存在时返回空清单提示（不是错误）。"""
    if not path.exists():
        return True, "[todo:read] 当前对话还没有任务清单。用 todo:write 创建。"

    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return False, f"[todo:read 失败] {exc}"

    if not text.strip():
        return True, "[todo:read] 清单为空。用 todo:write 创建。"

    done, total = summarize_progress(text)
    head = f"[todo:read 成功] 进度 {done}/{total}\n{path}"
    return True, f"{head}\n\n{text.rstrip()}"
