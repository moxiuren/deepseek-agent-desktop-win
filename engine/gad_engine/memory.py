"""跨会话长期记忆。

它解决什么
----------

`store.py` 让**同一个**对话在重启后能接回去。但它解决不了另一件事：
**开一个全新对话时，模型对之前发生过什么一无所知。**

DeepSeek 的服务端会话树是隔离的 —— 新 `chat_session_id` 就是一张白纸。
所以「换个话题、开个新对话」这个动作，会把这台机器上积累的一切（用户是谁、
在做什么项目、习惯什么风格）一起清空。本层就是那个跨会话的载体。

**为什么是 Markdown 文件而不是数据库**
  * 与既有的 `soul_file`（Agent-Soul.md）同构 —— 同一套心智模型，不加新概念
  * 用户能直接用编辑器打开、手改、纳入 Git 或 Obsidian 管理
  * 出问题时肉眼可读，不需要工具才能诊断

**为什么不做「自动把对话总结进记忆」**
最诱人的设计是每轮结束让模型自己总结一段塞进记忆。这里**刻意不做**，理由是
这个项目的硬约束：每多发一次请求就多一次被 `Server busy` / 429 打回的机会，
而记忆注入本身不需要模型参与。所以记忆的写入是**用户显式动作** ——
界面上「记住这条」按钮，或直接编辑文件。零额外请求，零熔断风险。
（如果将来要做自动摘要，应该做成显式开关，并把额外请求纳入 `pacing` 节流。）

注入形态
--------

`as_prompt_block()` 产出的是一个**自解释**的段落：不只给内容，还告诉模型
这是什么、该怎么对待它。少了后半句，模型会倾向于把记忆内容复述给用户 ——
那是纯粹的噪声。
"""

from __future__ import annotations

import os
from pathlib import Path

from .config import app_data_dir

#: 默认文件名。
MEMORY_NAME = "memory.md"

#: 默认注入上限（字符）。**必须有个上限** —— 记忆是会长的，而提示词每轮都要
#: 发出去。没有上限的话，某天它会悄悄把提示词挤爆，症状是「模型开始变笨」，
#: 而没人会想到是记忆文件太长。
DEFAULT_MAX_CHARS = 4000

_HEADER = "# 长期记忆"


class MemoryStore:
    """长期记忆的读写。所有方法都不抛 IO 异常 —— 记忆坏掉不该让引擎起不来。"""

    def __init__(
        self,
        path: Path | str | None = None,
        max_chars: int = DEFAULT_MAX_CHARS,
    ) -> None:
        self._path = Path(path) if path else (app_data_dir() / MEMORY_NAME)
        self.max_chars = int(max_chars) if max_chars and max_chars > 0 else DEFAULT_MAX_CHARS

    # ---------- 基本读写 ----------

    def path(self) -> Path:
        return self._path

    def exists(self) -> bool:
        return self._path.exists()

    def read(self) -> str:
        try:
            return self._path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return ""

    def write(self, text: str) -> Path:
        """整体覆盖（原子写）。空内容写空文件，不删文件 —— 删了用户就找不到路径了。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        body = text if text is not None else ""
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, self._path)
        return self._path

    def append(self, text: str, section: str = "") -> Path:
        """追加一条记忆。

        `section` 为空则追加到文件末尾；指定则追加到 `## <section>` 这一节下
        （节不存在就在末尾新建）。用分节是为了让记忆**可读** —— 一坨流水账
        到后面连写它的人都不想看。
        """
        entry = (text or "").strip()
        if not entry:
            return self.path()

        # 已带列表符号 / 标题符号的，尊重用户自己的写法，不重复加 "- "。
        if not entry.startswith(("-", "*", "+", "#", ">")):
            entry = f"- {entry}"

        body = self.read()
        if section:
            body = self._append_under_section(body, section.strip(), entry)
        else:
            base = body.rstrip()
            body = f"{base}\n{entry}\n" if base else f"{_HEADER}\n\n{entry}\n"
        return self.write(body)

    def clear(self) -> Path:
        return self.write("")

    # ---------- 分节处理 ----------

    @staticmethod
    def _append_under_section(body: str, section: str, entry: str) -> str:
        """把 `entry` 插到 `## <section>` 这一节的末尾。"""
        lines = body.splitlines()
        head = f"## {section}"

        start = -1
        for i, line in enumerate(lines):
            if line.strip() == head:
                start = i
                break

        if start < 0:
            # 这一节还不存在 → 在末尾新建。
            base = body.rstrip()
            if base:
                return f"{base}\n\n{head}\n{entry}\n"
            return f"{_HEADER}\n\n{head}\n{entry}\n"

        # 找到这一节的结束位置（下一个同级或更高级标题）。
        end = len(lines)
        for j in range(start + 1, len(lines)):
            stripped = lines[j].lstrip()
            if stripped.startswith("## ") or stripped.startswith("# "):
                end = j
                break

        # 回退掉该节末尾的空行，把新条目贴在最后一条内容后面。
        insert_at = end
        while insert_at > start + 1 and not lines[insert_at - 1].strip():
            insert_at -= 1

        lines.insert(insert_at, entry)
        return "\n".join(lines).rstrip() + "\n"

    # ---------- 注入 ----------

    def as_prompt_block(self) -> str:
        """产出注入系统提示词的段落。没有记忆时返回空串。"""
        text = self.read().strip()
        if not text:
            return ""

        truncated = False
        if len(text) > self.max_chars:
            text = text[: self.max_chars].rstrip()
            truncated = True

        tail = "（记忆较长，此处只注入了开头部分。）\n" if truncated else ""
        return (
            f"{_HEADER}\n\n"
            f"{text}\n\n"
            f"{tail}"
            "以上是你在过去对话里记下的事实与偏好。**把它当作已知前提**，"
            "不要复述、不要向用户确认；只有在它与用户本轮输入**直接冲突**时，"
            "才以本轮输入为准并说明你改了什么。"
        )

    # ---------- 状态 ----------

    def status(self) -> dict:
        text = self.read()
        return {
            "path": str(self._path),
            "exists": self._path.exists(),
            "chars": len(text),
            "max_chars": self.max_chars,
            "over_limit": len(text) > self.max_chars,
            "lines": len(text.splitlines()) if text else 0,
            "updated_at": (
                self._path.stat().st_mtime if self._path.exists() else 0.0
            ),
        }
