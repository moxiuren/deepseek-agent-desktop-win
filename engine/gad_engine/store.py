"""L2.5 对话仓库（持久化层）。

为什么需要这一层
----------------

在此之前，`SessionManager` 只维护**一个**会话的三元组，而且**全在内存里** ——
进程一退，`chat_session_id` 就没了。后果有两个，都很硬：

1. **关掉应用再打开，对话从零开始。** 服务端那条会话树还在（它有自己的存活期），
   但客户端把钥匙丢了，再也接不回去。
2. **界面上看不到任何历史。** 消息只活在 `ConvPanel` 的视觉树里，换个会话就没了。

本层要同时解决这两件事 —— 它们本来就是一件事的两面：**把对话落盘**。

存储布局
--------

    <app_data>/conversations/
        index.json          列表索引（渲染列表只读它，不必读 N 个文件）
        <id>.json           一个对话一个文件

**为什么一个对话一个文件，而不是一个大 JSON**：
  * 单个文件损坏只损失一个对话，不会连坐
  * 列表渲染走索引，与对话体量解耦（100 个对话也是 O(1) 读）
  * 删除 / 重命名是文件系统操作，不需要重写全部历史

**索引与实体必然可能不一致**（崩在两次写之间）。所以：
  * 索引只当**缓存**用，任何一条对不上实体就跳过
  * 提供 `rebuild_index()` 从实体重建，启动时若发现实体比索引多也会自动重建

写入策略
--------

**原子写**（临时文件 + `os.replace`）。直接 `write_text` 覆盖的话，写到一半掉电
会留下一个截断的 JSON —— 下次启动那个对话就整条读不出来了。`os.replace` 在
Windows 与 POSIX 上都是原子的。

id 与安全
---------

id 由本地生成（`YYYYMMDD-HHMMSS-xxxx`），可读、天然按时间有序。但 RPC 面会把
id 从界面传回来 —— 那是**外部输入**，必须校验：只允许 `[A-Za-z0-9_-]`。
不校验的话，`"../../config"` 这样的 id 能读到任意文件。
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .config import app_data_dir
from .session import SessionMeta

#: 索引文件结构版本。将来结构变了靠它判断要不要迁移。
SCHEMA_VERSION = 1

#: 对话存放目录名（在 `<app_data>` 下）。`EngineConfig.conversations_dir()` 引用它。
CONVERSATIONS_DIR = "conversations"

INDEX_NAME = "index.json"

#: id 白名单。**这是安全边界，不是格式偏好** —— 见模块文档。
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")

#: 自动标题的最大字数（中文字符）。
_TITLE_LEN = 24

#: 列表预览的最大字数。
_PREVIEW_LEN = 72


@dataclass
class Message:
    """一条对话消息。

    `role` 取值刻意与外壳的 `TranscriptKind` 对得上，但**不直接用它的枚举名** ——
    引擎不该知道界面怎么画。当前映射：

    | role | 外壳渲染成 |
    |---|---|
    | `user` | `Goal`（用户气泡） |
    | `assistant` | `Assistant`（回答气泡） |
    | `notice` | `Notice`（灰色提示） |
    | `error` | `Error`（红色错误） |

    `cid` / `rid` 记的是**这一轮结束时的服务端三元组** —— 它让「某个对话当时
    接的是哪条会话树」可追溯，而不只是当前状态。排查「为什么这个对话突然
    开始没有上下文」时，靠的就是这个。
    """

    role: str
    text: str
    ts: float = field(default_factory=time.time)
    cid: str = ""
    rid: str = ""
    error: str = ""
    tool_calls: int = 0
    turns: int = 0
    stop_reason: str = ""
    thoughts: str = ""

    @classmethod
    def from_dict(cls, data: Any) -> Message | None:
        if not isinstance(data, dict):
            return None
        text = data.get("text")
        if not isinstance(text, str):
            return None
        return cls(
            role=str(data.get("role") or "notice"),
            text=text,
            ts=float(data.get("ts") or 0.0),
            cid=str(data.get("cid") or ""),
            rid=str(data.get("rid") or ""),
            error=str(data.get("error") or ""),
            tool_calls=int(data.get("tool_calls") or 0),
            turns=int(data.get("turns") or 0),
            stop_reason=str(data.get("stop_reason") or ""),
            thoughts=str(data.get("thoughts") or ""),
        )


@dataclass
class Conversation:
    """一个对话（含全部消息与服务端会话三元组）。"""

    id: str
    title: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    messages: list[Message] = field(default_factory=list)
    #: 服务端会话三元组。**这是「重启后还能接上原来那条会话树」的关键。**
    session: SessionMeta = field(default_factory=SessionMeta)
    #: 这个对话创建时的形态快照（思考 / 联网 / 模型）。切换对话时回填界面，
    #: 否则「在 A 对话开了联网，切到 B 对话，顶栏还勾着」会误导。
    settings: dict[str, Any] = field(default_factory=dict)
    #: 上一次从服务端同步历史的时刻（`time.time()`）。0 = 从未同步过。
    #: 只用来给界面显示「上次同步：x 分钟前」，不参与任何判断。
    synced_at: float = 0.0
    #: **接力摘要**（见 `compact.py`）：这条对话是从另一条对话「接力」过来的，
    #: 这里存着那条对话压缩出来的上下文。它会被注入到**每一轮**的系统提示词里
    #: （与长期记忆同一处理方式），所以新会话一开口就记得之前发生过什么。
    #:
    #: 空串 = 普通对话（不是接力来的）。
    #:
    #: ⚠️ **不能叫 `summary`** —— 本类已经有一个 `summary()` 方法（返回列表用的
    #: 轻量摘要 dict）。字段与它同名时，`@dataclass` 收集默认值拿到的是那个
    #: **函数对象**，于是 `self.summary` 变成绑定方法，`to_dict()` 一落盘就
    #: 报「Object of type function is not JSON serializable」。
    #: 这个坑很隐蔽：类型注解写的是 `str`，静态检查看不出来。
    handoff_summary: str = ""
    #: 摘要来自哪条对话（本地 id）。只用于溯源 —— 界面上可以说清「这段上下文
    #: 是从哪条对话带过来的」，排查「为什么它记得某件事」时也靠它。
    handoff_from: str = ""
    #: 摘要生成的时刻。
    handoff_at: float = 0.0

    # ---------- 统计 ----------

    @property
    def turn_count(self) -> int:
        """用户轮次（用户消息条数），不是消息总条数。"""
        return sum(1 for m in self.messages if m.role == "user")

    def preview(self) -> str:
        """列表里显示的一行摘要 —— 取最后一条有内容的消息。"""
        for msg in reversed(self.messages):
            if msg.text.strip():
                line = " ".join(msg.text.strip().splitlines())
                return line[:_PREVIEW_LEN]
        return "（空对话）"

    # ---------- 序列化 ----------

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "messages": [asdict(m) for m in self.messages],
            "session": asdict(self.session),
            "settings": dict(self.settings),
            "synced_at": self.synced_at,
            "handoff_summary": self.handoff_summary,
            "handoff_from": self.handoff_from,
            "handoff_at": self.handoff_at,
        }

    def summary(self) -> dict:
        """给列表用的轻量摘要（**不含 messages**）。

        `session_cid` 是服务端会话 id —— 「读取网页版会话」时要靠它把远端会话
        与本地对话对上（哪条已经被接管）。放在这里而不是让调用方逐条读实体，
        是因为列表可能有很多条，而每条实体都带着全部消息。
        """
        return {
            "id": self.id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "turn_count": self.turn_count,
            "message_count": len(self.messages),
            "preview": self.preview(),
            "session_cid": self.session.cid,
            # 列表里显示「接力」标记用。摘要正文不放进列表 —— 它可能有一两千字，
            # 而列表可能有很多条。
            "has_handoff": bool(self.handoff_summary.strip()),
        }

    @classmethod
    def from_dict(cls, data: Any) -> Conversation | None:
        if not isinstance(data, dict):
            return None
        cid = data.get("id")
        if not isinstance(cid, str) or not _ID_RE.match(cid):
            return None
        messages: list[Message] = []
        for raw in data.get("messages") or []:
            msg = Message.from_dict(raw)
            if msg is not None:
                messages.append(msg)
        return cls(
            id=cid,
            title=str(data.get("title") or ""),
            created_at=float(data.get("created_at") or 0.0),
            updated_at=float(data.get("updated_at") or 0.0),
            messages=messages,
            session=_session_from_dict(data.get("session")),
            settings=dict(data.get("settings") or {}),
            synced_at=float(data.get("synced_at") or 0.0),
            handoff_summary=str(data.get("handoff_summary") or ""),
            handoff_from=str(data.get("handoff_from") or ""),
            handoff_at=float(data.get("handoff_at") or 0.0),
        )


def _session_from_dict(data: Any) -> SessionMeta:
    """从落盘数据恢复会话三元组。字段缺失 / 类型不对时退回空 meta。

    刻意**不抛异常** —— 一个字段坏掉不该让整个对话读不出来，
    那比丢一个字段糟糕得多。
    """
    if not isinstance(data, dict):
        return SessionMeta()

    def _s(key: str) -> str:
        value = data.get(key)
        return value if isinstance(value, str) else ""

    def _f(key: str, default: float) -> float:
        try:
            return float(data.get(key) or default)
        except (TypeError, ValueError):
            return default

    return SessionMeta(
        cid=_s("cid"),
        rid=_s("rid"),
        rcid=_s("rcid"),
        temporary=bool(data.get("temporary", True)),
        # 缺字段按「非接管」处理 —— 旧对话本来就不是接管的。
        attached=bool(data.get("attached", False)),
        turns=int(_f("turns", 0)),
        created_at=_f("created_at", time.time()),
        last_used_at=_f("last_used_at", time.time()),
    )


def _atomic_write(path: Path, text: str) -> None:
    """原子写：临时文件 + `os.replace`。

    同目录的临时文件 —— `os.replace` 要求同一文件系统才保证原子性。

    `errors="replace"`：文本里可能混入**代理字符**（U+D800–U+DFFF），
    它们通常来自子进程输出的 `surrogateescape` 解码（Windows 上中文被按
    错误编码读回时会出现）。这类字符无法编码成 UTF-8，直接写会抛
    `UnicodeEncodeError` —— 而且是在**写对话历史**这种关键路径上，
    后果是整个 `run` 失败、用户看到一条编码错误而不是回复。
    2026-10-17 实测撞上过：历史里混进一个 `\\udcaa` 后，此后每轮
    写入都失败。宁可把坏字符替换成 `?`，也不能让历史写不进去。
    """
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", errors="replace")
    os.replace(tmp, path)


class ConversationStore:
    """对话仓库。所有方法都不抛「文件不存在」这类异常 —— 界面不该为存储报错。"""

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root else (app_data_dir() / CONVERSATIONS_DIR)
        self.root.mkdir(parents=True, exist_ok=True)

    # ---------- 路径与 id ----------

    def _path(self, cid: str) -> Path:
        """把 id 映射成文件路径。**先校验再拼路径** —— 这是安全边界。"""
        if not _ID_RE.match(cid or ""):
            raise ValueError(f"非法的对话 id：{cid!r}")
        return self.root / f"{cid}.json"

    @staticmethod
    def new_id() -> str:
        """可读且按时间有序的 id：`20261015-195230-a3f9`。"""
        return f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"

    @staticmethod
    def auto_title(text: str) -> str:
        """从首条用户消息生成标题（取首行、压空白、截断）。"""
        line = (text or "").strip()
        if not line:
            return "新对话"
        line = " ".join(line.splitlines()[0].split())
        if not line:
            return "新对话"
        return line if len(line) <= _TITLE_LEN else line[:_TITLE_LEN] + "…"

    # ---------- 索引 ----------

    def _index_path(self) -> Path:
        return self.root / INDEX_NAME

    def _read_index(self) -> dict:
        try:
            data = json.loads(self._index_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"version": SCHEMA_VERSION, "active": "", "items": []}
        if not isinstance(data, dict):
            return {"version": SCHEMA_VERSION, "active": "", "items": []}
        items = data.get("items")
        return {
            "version": int(data.get("version") or SCHEMA_VERSION),
            "active": str(data.get("active") or ""),
            "items": items if isinstance(items, list) else [],
        }

    def _write_index(self, index: dict) -> None:
        _atomic_write(
            self._index_path(),
            json.dumps(index, ensure_ascii=False, indent=2) + "\n",
        )

    def _sync_index_entry(self, index: dict, conv: Conversation) -> None:
        """把某个对话的摘要写进索引（存在则更新，不存在则插入）。"""
        entry = conv.summary()
        items = index.setdefault("items", [])
        for i, item in enumerate(items):
            if isinstance(item, dict) and item.get("id") == conv.id:
                items[i] = entry
                return
        items.append(entry)

    # ---------- 列表 ----------

    def list_summaries(self) -> list[dict]:
        """全部对话摘要，按最近更新倒序。**以磁盘实体为准**，索引只做缓存。"""
        index = self._read_index()
        by_id = {
            str(it.get("id")): it
            for it in index.get("items", [])
            if isinstance(it, dict) and isinstance(it.get("id"), str)
        }

        out: list[dict] = []
        seen: set[str] = set()
        for path in self.root.glob("*.json"):
            if path.name == INDEX_NAME:
                continue
            cid = path.stem
            if not _ID_RE.match(cid):
                continue  # 不是本仓库的文件（含 .tmp 之类），忽略
            seen.add(cid)
            cached = by_id.get(cid)
            if cached is not None and "preview" in cached:
                out.append(dict(cached))
                continue
            # 索引里没有（或结构太旧）→ 读实体补一条
            conv = self.get(cid)
            if conv is not None:
                out.append(conv.summary())

        out.sort(key=lambda d: float(d.get("updated_at") or 0.0), reverse=True)

        # 索引落后于实体时顺手重建一次 —— 但**不在这里写盘**，
        # 避免「只读列表」这个动作产生副作用。由 ensure_active/rebuild 负责落盘。
        if seen != set(by_id):
            for item in out:
                item["stale_index"] = True
        return out

    def active_id(self) -> str:
        cid = self._read_index().get("active") or ""
        if cid and self._path_exists(cid):
            return cid
        return ""

    def _path_exists(self, cid: str) -> bool:
        try:
            return self._path(cid).exists()
        except ValueError:
            return False

    def set_active(self, cid: str) -> bool:
        if not self._path_exists(cid):
            return False
        index = self._read_index()
        index["active"] = cid
        self._write_index(index)
        return True

    # ---------- 服务端会话绑定 ----------
    #
    # 本地对话与服务端 `chat_session_id` 的对应关系。读取网页版会话时靠它
    # 回答两个问题：这条远端会话有没有被接管？接管成了哪条本地对话？

    def session_bindings(self) -> dict[str, str]:
        """服务端 `chat_session_id` → 本地对话 id。

        优先走索引里的 `session_cid`（列表页只需要这个字段，不必读实体）；
        索引里没有（升级前的旧条目）才回退读一次实体。同一条服务端会话
        若被多条本地对话指向（不该发生），保留最先遇到的那条。
        """
        out: dict[str, str] = {}
        for item in self.list_summaries():
            cid = str(item.get("id") or "")
            if not cid:
                continue
            remote = item.get("session_cid")
            if not isinstance(remote, str) or not remote:
                conv = self.get(cid)
                remote = conv.session.cid if conv is not None else ""
            if remote:
                out.setdefault(remote, cid)
        return out

    def find_by_session_cid(self, session_cid: str) -> Conversation | None:
        """按服务端 `chat_session_id` 找本地对话（找不到返回 None）。

        「接管网页版会话」靠它做**幂等**：同一个服务端会话只该绑定一个本地
        对话，重复接管要退化成「切过去」，而不是再建一条、让同一条会话在
        列表里出现两次。
        """
        target = (session_cid or "").strip()
        if not target:
            return None
        cid = self.session_bindings().get(target)
        return self.get(cid) if cid else None

    # ---------- 对话 ----------

    def create(self, title: str = "", settings: dict | None = None) -> Conversation:
        """新建对话并设为当前。"""
        conv = Conversation(
            id=self.new_id(),
            title=title or "新对话",
            settings=dict(settings or {}),
        )
        self.save(conv)
        index = self._read_index()
        index["active"] = conv.id
        self._write_index(index)
        return conv

    def get(self, cid: str) -> Conversation | None:
        """读一个对话。不存在或损坏都返回 None —— 由调用方决定怎么提示。"""
        try:
            path = self._path(cid)
        except ValueError:
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return Conversation.from_dict(data)

    def save(self, conv: Conversation) -> None:
        """整体落盘 + 更新索引。对话体量不大，全量重写换来的是结构简单。"""
        conv.updated_at = time.time()
        _atomic_write(
            self._path(conv.id),
            json.dumps(conv.to_dict(), ensure_ascii=False, indent=2) + "\n",
        )
        index = self._read_index()
        self._sync_index_entry(index, conv)
        self._write_index(index)

    def append(self, conv: Conversation, msg: Message) -> Conversation:
        """追加一条消息并落盘（**落盘是即时的** —— 崩溃不该丢已经发生的对话）。"""
        conv.messages.append(msg)
        # 首条用户消息顺手生成标题：用户不该被迫先给对话起名。
        if msg.role == "user" and (not conv.title or conv.title == "新对话"):
            conv.title = self.auto_title(msg.text)
        self.save(conv)
        return conv

    def rename(self, cid: str, title: str) -> Conversation | None:
        """改名。**返回更新后的对象**（改不动返回 None）。

        为什么返回对象而不是 bool：`save()` 是「以传入对象为准整体覆盖」，
        而本方法是「读-改-写」。两者混用会丢数据 —— 调用方若还攥着一个
        改名之前的旧引用，下一次 `save()` 就会把这次改名冲掉。
        返回新对象，调用方就有办法把引用刷新过来。

        更稳的用法是：**当前对话的重命名走 `AgentRuntime`**（它是当前对话的
        唯一权威持有者），由它改自己的对象再落盘，根本不给「两个副本」留机会。
        """
        conv = self.get(cid)
        if conv is None:
            return None
        conv.title = (title or "").strip()[:80] or "新对话"
        self.save(conv)
        return conv

    def delete(self, cid: str) -> bool:
        """删除对话。若删的是当前对话，自动切到最近一个；一个都不剩则新建。"""
        try:
            path = self._path(cid)
        except ValueError:
            return False
        if not path.exists():
            return False
        try:
            path.unlink()
        except OSError:
            return False

        index = self._read_index()
        index["items"] = [
            it for it in index.get("items", [])
            if not (isinstance(it, dict) and it.get("id") == cid)
        ]
        if index.get("active") == cid:
            remaining = self.list_summaries()
            index["active"] = str(remaining[0]["id"]) if remaining else ""
        self._write_index(index)

        if not index["active"]:
            self.create()  # 永远留一个对话，界面不必处理「零对话」这个态
        return True

    def ensure_active(self) -> Conversation:
        """启动时调用：返回当前对话；没有就新建一个。

        这是「重启后还能接上」的入口 —— 恢复出来的 `Conversation` 里带着
        上次的 `SessionMeta`，`agent` 拿它去续接服务端那条会话树。
        """
        cid = self.active_id()
        if cid:
            conv = self.get(cid)
            if conv is not None:
                return conv

        summaries = self.list_summaries()
        if summaries:
            conv = self.get(str(summaries[0]["id"]))
            if conv is not None:
                self.set_active(conv.id)
                return conv
        return self.create()

    # ---------- 维护 ----------

    def rebuild_index(self) -> int:
        """从磁盘实体重建索引。返回重建出的对话数。"""
        items: list[dict] = []
        for path in self.root.glob("*.json"):
            if path.name == INDEX_NAME or not _ID_RE.match(path.stem):
                continue
            conv = self.get(path.stem)
            if conv is not None:
                items.append(conv.summary())
        items.sort(key=lambda d: float(d.get("updated_at") or 0.0), reverse=True)

        index = self._read_index()
        known = {str(it.get("id")) for it in items}
        index["items"] = items
        if index.get("active") not in known:
            index["active"] = str(items[0]["id"]) if items else ""
        index["version"] = SCHEMA_VERSION
        self._write_index(index)
        return len(items)

    def stats(self) -> dict:
        """给状态面板用。"""
        summaries = self.list_summaries()
        return {
            "root": str(self.root),
            "count": len(summaries),
            "active": self.active_id(),
        }
