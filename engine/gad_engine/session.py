"""L3 会话层（DeepSeek 语义）。

**与 gemini 版的差别只在语义，不在结构** —— 上层（协议层 / 执行层 / UI）拿到的
仍是 `[cid, rid, rcid]` 三元组，所以 `transport/base.py` 一行不改。

三元组在 DeepSeek 侧的映射
--------------------------

| 字段 | DeepSeek 对应物 | 说明 |
|---|---|---|
| `cid`  | `chat_session_id` | **服务端创建**（`/api/v0/chat_session/create`），客户端不能自编 |
| `rid`  | 上一轮的 `response_message_id` | 下一轮作为 `parent_message_id` 回传，串起对话树 |
| `rcid` | 上一轮的 `request_message_id` | 本地留存，暂不参与请求 |

**为什么 `cid` 必须由服务端给**（实测，2026-10-14 23:45）：客户端自编 uuid4 时
`/api/v0/chat/completion` 回 HTTP 200 + `{"code":0,"data":{"biz_code":1,
"biz_msg":"invalid chat session id"}}` —— **状态码是 200，业务是失败**。

原版困境（本层要处理的核心风险）
--------------------------------

原版（`deepseek-mac`）是 DOM 注入，无状态，每轮重发全量历史；而 `parent_message_id`
链一旦复用不当（同一 `chat_session_id` 配 `parent_message_id: null`）就会创建多个
root → 会话树损坏 → **全局吊销 40003**。原版只能妥协到「25 轮 / 20 分钟」复用。

直连模式**是否仍触发 40003 尚未实测**（列在评估报告的待验证清单里）。所以在拿到
结论之前，本层默认沿用原版的保守上限，并把阈值做成可配 —— 一旦 0.3 存活对照证明
直连下不会树损坏，再放宽。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class SessionMeta:
    cid: str = ""    # chat_session_id（服务端创建）
    rid: str = ""    # 上一轮 response_message_id → 下轮 parent_message_id
    rcid: str = ""   # 上一轮 request_message_id（留存备用）
    temporary: bool = True
    turns: int = 0
    #: 这条会话是**接管**来的（用户在 DeepSeek 网页版里建的，不是本客户端新建的）。
    #:
    #: 语义只有一个：**不自动轮换**。理由是它直接决定「我现在的输入进没进那个会话」——
    #: 接管的往往是一条已经聊了很多轮的长会话，如果按 `max_turns` / `max_age` 轮换，
    #: 用户点进来发第一条消息就会被送进一个**全新的空白会话**：界面看着正常，
    #: 上下文却全没了。用户点进来的目的恰恰是「继续这条」，所以必须禁掉轮换。
    attached: bool = False
    created_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)

    @property
    def is_new(self) -> bool:
        return not self.cid

    @property
    def is_established(self) -> bool:
        """已建立且已有过至少一轮往返。"""
        return bool(self.cid and self.rid)

    @property
    def parent_message_id(self) -> str | None:
        """下一轮要带的 `parent_message_id`。

        **未建立时必须是 `None`**（新会话首轮）；已建立时是上一轮的
        `response_message_id`。原版树损坏的根因就是这两个状态混用。
        """
        return self.rid or None

    def as_metadata(self) -> list[str | None]:
        """兼容 gemini 版上层（三元组形式）。"""
        return [self.cid or None, self.rid or None, self.rcid or None]

    def to_dict(self) -> dict:
        return {
            "cid": self.cid,
            "rid": self.rid,
            "rcid": self.rcid,
            "temporary": self.temporary,
            "attached": self.attached,
            "turns": self.turns,
            "age_sec": round(time.time() - self.created_at, 1),
        }


@dataclass
class SessionEvent:
    rotated: bool
    reason: str
    session: SessionMeta


class SessionManager:
    """会话生命周期管理。

    轮换条件：
      1. **轮次上限**（默认 25，沿用原版保守值）—— 防会话树形态过于异常
      2. **时长上限**（默认 20 分钟，沿用原版）—— 原版经验值
      3. 显式请求 —— 换话题时，或用户按「接力新会话」

    `max_turns <= 0` / `max_age_sec <= 0` 表示该维度不限制。

    **`auto_rotate=False`（默认）时，条件 1、2 都不生效** —— 什么时候换会话
    由用户决定（界面上的「接力新会话」按钮，换的时候会把上下文压成摘要带过去）。
    关掉的理由见 `PacingConfig.auto_rotate`：自动轮换是**静默**的，用户只会
    看到「模型突然失忆」。显式轮换（`rotate()`）不受这个开关影响。
    """

    def __init__(
        self,
        max_turns: int = 25,
        max_age_sec: float = 20 * 60,
        temporary: bool = True,
        auto_rotate: bool = True,
    ) -> None:
        self.max_turns = max_turns
        self.max_age_sec = max_age_sec
        self.temporary = temporary
        self.auto_rotate = auto_rotate
        self._meta = SessionMeta(temporary=temporary)
        self._rotation_log: list[str] = []

    @property
    def meta(self) -> SessionMeta:
        return self._meta

    def should_rotate(self) -> tuple[bool, str]:
        if not self._meta.cid:
            return False, ""
        # 接管的会话不自动轮换 —— 见 `SessionMeta.attached` 的说明。
        # 放在最前面：接管的会话往往 `turns` / `age` 早就超了，必须先于那两条判断。
        if self._meta.attached:
            return False, ""
        # 自动轮换被关掉（默认）→ 轮次 / 时长都不再作为轮换依据。
        # 显式轮换走 `rotate()`，不经这里。
        if not self.auto_rotate:
            return False, ""
        if self.max_turns > 0 and self._meta.turns >= self.max_turns:
            return True, f"已达会话轮次上限（{self.max_turns} 轮）"
        if self.max_age_sec > 0:
            age = time.time() - self._meta.created_at
            if age >= self.max_age_sec:
                return True, f"已达会话时长上限（{self.max_age_sec / 60:.0f} 分钟）"
        return False, ""

    def rotate(self, reason: str = "显式轮换") -> SessionEvent:
        self._rotation_log.append(f"[{time.strftime('%H:%M:%S')}] {reason}")
        self._meta = SessionMeta(temporary=self.temporary)
        return SessionEvent(rotated=True, reason=reason, session=self._meta)

    def maybe_rotate(self) -> SessionEvent | None:
        need, reason = self.should_rotate()
        if need:
            return self.rotate(reason)
        return None

    def restore(self, meta: SessionMeta) -> SessionMeta:
        """整体接管一份（持久化恢复出来的）会话元数据。

        与 `adopt()` 的差别是**语义边界**，不是实现：
          * `adopt(cid)` 用于「服务端刚给我新建了一个会话」—— 只接 `cid`，
            `rid` 必须保持空（那是首轮语义）
          * `restore(meta)` 用于「切回一个已有对话 / 重启恢复」—— **连 `rid`
            一起接管**。少了 `rid`，`parent_message_id` 就是 `None`，
            下一轮会被服务端当成新会话的根 —— 上下文接不上，等于白切。
        """
        self._meta = meta
        return self._meta

    def adopt(self, cid: str) -> SessionMeta:
        """采纳一个服务端新创建的 session（首轮创建后调用）。

        只改 `cid`，**不动 `rid`** —— 新会话的 `rid` 必须保持空，
        这样 `parent_message_id` 才是 `None`（首轮语义）。
        """
        self._meta.cid = cid
        self._meta.rid = ""
        self._meta.rcid = ""
        self._meta.last_used_at = time.time()
        return self._meta

    def update_from_response(
        self,
        cid: str | None = None,
        rid: str | None = None,
        rcid: str | None = None,
    ) -> SessionMeta:
        """用服务端返回的三元组滚动更新（一轮成功之后调用）。"""
        if cid:
            self._meta.cid = cid
        if rid:
            self._meta.rid = rid
        if rcid:
            self._meta.rcid = rcid
        self._meta.turns += 1
        self._meta.last_used_at = time.time()
        return self._meta

    def snapshot(self) -> dict:
        data = self._meta.to_dict()
        data["max_turns"] = self.max_turns
        data["max_age_sec"] = self.max_age_sec
        # 界面据此说明「会话不会自己换」—— 否则用户看到轮次一直涨会以为
        # 迟早会被悄悄轮换掉，从而不敢长聊。
        data["auto_rotate"] = self.auto_rotate
        data["rotations"] = list(self._rotation_log[-10:])
        return data
