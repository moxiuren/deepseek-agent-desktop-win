"""传输层抽象。

**这是本方案唯一的可插拔点。** T3（RPC 直连）是首个实现，T1（WebView 注入）
是降级实现，T4（官方 API / Gemini CLI）可作为第三实现。

上层的协议层、闸门层、执行层、UI 完全不受档位选择影响 —— 换档位只需换一个
适配器，其余代码一行不动。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from ..session import SessionMeta

DeltaCallback = Callable[[str], Awaitable[None] | None]
# 思考通道的增量回调。**和 `DeltaCallback` 分开**是有意的：思考内容属于
# 「过程」不属于「回答」，界面要能分别处理（正文进对话流、思考进状态提示），
# 而且思考内容**不应**进入工具调用解析（`ToolCallParser` 只看正文）。
ThoughtCallback = Callable[[str], Awaitable[None] | None]


class TransportError(RuntimeError):
    """传输层异常。区分可重试与致命由闸门层根据错误类型判断。

    三个标志位是给熔断层分类用的，含义互不重叠：

    * `auth`   —— 认证问题（cookie 失效 / 未登录），熔断层按认证失败退避
    * `fatal`  —— 不可恢复，直接停机
    * `config` —— **配置本身不成立**（例如配置里的模型名当前账号没有）。
      它不是 RPC 抖动，也不是认证失败，所以**不能计入 `note_rpc_error`** ——
      否则用户手改错一个模型名，重试三次就会把传输层降级掉，
      而真正的原因（名字写错了）反而被埋在「RPC 连续失败」后面。
    """

    def __init__(
        self,
        message: str,
        *,
        auth: bool = False,
        fatal: bool = False,
        config: bool = False,
        suspended: bool = False,
        status: int = 0,
    ) -> None:
        super().__init__(message)
        self.auth = auth
        self.fatal = fatal
        self.config = config
        #: 账号被服务端处置（封禁 / 停用）。**必须与「token 过期」分开** ——
        #: 两者都报 40003/401，但处置是账号级、有明确到期时间，用户需要看到
        #: 原文与截止时间，而不是被误导去反复重新登录。
        self.suspended = suspended
        #: HTTP 状态码（0 = 非 HTTP 层错误，例如解析失败）。
        #: 加这个字段是为了让**发送审计**能记下「这次失败是 429 还是 401」——
        #: 原来这些信息只存在于异常文本里，审计只能记一句模糊的 `error`。
        self.status = int(status or 0)


@dataclass
class RemoteStatus:
    """远端账号状态 —— L4 熔断层的输入。"""

    reachable: bool = False
    account_status: int = 0
    account_status_text: str = ""
    # 短标签（「正常」/「未认证」），给顶栏那一行用；长描述进 ToolTip。
    account_status_short: str = ""
    abuse_clean: bool | None = None
    abuse_signal: str | None = None
    quota_used_ratio: float | None = None
    quota_detail: dict = field(default_factory=dict)
    tier: str = ""
    model: str = ""
    note: str = ""


@dataclass
class ModelInfo:
    """一个账号可用模型的可展示信息。"""

    name: str
    display_name: str = ""
    description: str = ""
    is_available: bool = True
    aliases: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "display_name": self.display_name or self.name,
            "description": self.description,
            "is_available": self.is_available,
            "aliases": list(self.aliases),
        }


@dataclass
class TurnOutcome:
    text: str = ""
    thoughts: str = ""
    cid: str = ""
    rid: str = ""
    rcid: str = ""
    error: str = ""
    #: 联网搜索的痕迹（只有开了 `search_enabled` 且服务端真的搜了才有值）。
    #: `search_pages` 来自 SEARCH 片段的状态文本（`Found 12 web pages`），
    #: `search_sources` 是来源列表长度。**这两个数字不属于正文** ——
    #: 正文里混进「Found 12 web pages」是实测踩过的坑。
    search_pages: int = 0
    search_sources: int = 0
    #: 工作量证明（PoW）的**本地**耗时拆解 —— 仅 `deepseek-rpc` 档位有值，
    #: 其它档位留空。放在 `TurnOutcome` 上而不是另开一个对象，是因为它随
    #: 每一次 `send()` 产生、和 outcome 同生命周期（与 `search_pages` 同理）。
    #:
    #: **为什么必须单独上报**：`sent` 事件在 `transport.send()` **之前**就发出去了，
    #: 而取 challenge 与解 PoW 都发生在 `send()` **内部** —— 所以 `sent` 报的
    #: `gate_wait_ms` / `pre_send_ms` / `local_total_ms` **一个都不含它**。
    #: 而这段是**纯本地空耗**（2026-10-16 实测单次数秒），必须能被看见。
    challenge_ms: float = 0.0          # 取 challenge（一次 HTTP）的耗时
    pow_ms: float = 0.0                # 解 PoW 的耗时（含建池 + 搜索 + 收尾）
    pow_mode: str = ""                 # multi / single / degraded
    pow_workers: int = 0               # 实际使用的进程数
    pow_trials: int = 0                # 命中 nonce 的位置 + 1（顺序语义，非真实哈希数）
    pow_pool_startup_ms: float = 0.0   # 建池 → 首个分片返回
    #: 这一轮是否**新建**了进程池（2026-10-17 起池常驻复用）。
    #: `True` ⇒ `pow_pool_startup_ms` 含真实 spawn 成本；
    #: `False` ⇒ 复用了上一轮的池，该值只剩「首个分片的计算时间」。
    pow_pool_created: bool = False
    #: 本轮 PoW 是否**命中后台预取缓存**（2026-10-18 起）。
    #: 命中时 `challenge_ms` 与 `pow_ms` 都是 0 —— 这是**真实的 0**，
    #: 不是没测到。真实网页版第二次发送也是 0（文档 §2.5）。
    pow_cached: bool = False
    #: 这个 challenge **当初求解**花了多久。命中缓存时 `pow_ms` 是 0，
    #: 但这项仍有值 —— 用来观察「预取的那次算得多快」，不是用户等待时间。
    pow_solve_ms: float = 0.0

    #: 本轮是否被**用户主动停止**（2026-10-18，T-19）。
    #:
    #: 与 `error` 互斥：停止不是故障。已生成的内容照常保留在 `text` 里
    #: （用户点「停止」的预期是「别再往下写了」，不是「把已写的也抹掉」）。
    #: 上层必须**不**把它计入熔断与 RPC 错误计数 —— 否则连点几次停止
    #: 就足以把传输层降级掉，而真正的原因是用户自己按了按钮。
    cancelled: bool = False

    #: 本轮附件的上传耗时（毫秒）。上传**每个文件各要一次独立 PoW**
    #: （`target_path=/api/v0/file/upload_file`，与发送的那份不通用），
    #: 所以这段是纯本地空耗，必须能和 `pow_ms` 分开看 —— 否则「发一条带
    #: 附件的消息等了 12 秒」会被误读成 PoW 变慢了。
    upload_ms: float = 0.0
    #: 本轮实际挂上的附件（`upload_file` 的 `biz_data` 摘要）。
    #: 每项含 `path` / `id` / `status` / `file_name` / `file_size`。
    #: `id` 就是 `ref_file_ids` 里的元素 —— 出问题时拿它去对照服务端。
    uploaded_files: list[dict] = field(default_factory=list)
    #: `completion` 那一次 HTTP 的状态码（0 = 没走到发请求就失败了）。
    #: 给审计用 —— 复盘时要能回答「失败那次是 429 还是 401」。
    http_status: int = 0
    #: 首字节耗时（毫秒）。**这是判断「服务端愿不愿意理你」的关键指标**：
    #: 2026-10-15 实测过载时服务端会把连接挂 ~68 秒才回，而总耗时里
    #: 大部分是模型生成时间，看不出这个区别。
    ttfb_ms: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def searched(self) -> bool:
        return bool(self.search_pages or self.search_sources)


class Transport(ABC):
    name: str = "base"
    supports_streaming: bool = False

    @abstractmethod
    async def start(self) -> None:
        """建立连接 / 加载凭据。"""

    @abstractmethod
    async def stop(self) -> None:
        """释放资源。"""

    @abstractmethod
    async def health(self) -> RemoteStatus:
        """查询远端账号状态（供 L4 熔断层主动轮询）。"""

    def available_models(self) -> list[ModelInfo]:
        """本账号可用的模型列表。

        默认返回空列表 —— 不是所有档位都支持模型选择（T1 WebView 注入就没有
        这个概念），所以它**不是**抽象方法，不强迫降级实现去写一个假的。
        界面见到空列表就当「本档位不支持选模型」处理。
        """
        return []

    async def set_model(self, name: str) -> None:
        """切换本会话使用的模型。空字符串 = 用账号默认。

        默认空实现，理由同 `available_models`。真正的校验由实现方负责 ——
        参数是配置层来的，可能来自手改的配置文件，不能假设它一定合法。
        """
        return None

    def background_state(self) -> dict:
        """后台维护（保活心跳 / cookie 轮换）的当前状态，供界面展示。

        默认空字典 —— 不是所有档位都有后台任务，理由同 `available_models`。

        **为什么要把它暴露出来**：机器特征最大的问题就是「不可见」。
        用户看不到程序在空闲时还在按固定节拍联网，也就无从判断「省不省」。
        暴露出来之后，界面能直接显示「后台心跳：已暂停（空闲）」。
        """
        return {}

    # ---------- 停止生成（2026-10-18，T-19） ----------
    #
    # 默认实现是**空操作**，不是抛错 —— 与上面几个「默认抛错」的能力刻意不同。
    # 理由：停止是**安全方向**的操作。档位不支持时，「点了没反应」比
    # 「点了报错」更糟，但比「为了报错而让停止按钮变成错误弹窗」好得多。
    # 界面照常显示「已请求停止」，用户不会被一个技术错误拦住。

    def request_stop(self) -> bool:
        """请求停止当前这一轮。返回是否真的置了标志。

        实现方应保证**同步、无等待、可从任意线程调用** —— 它由 IPC 线程
        直接调，不能是 `async`，否则调用方还得先 await 一次调度。
        """
        return False

    def clear_stop(self) -> None:
        """清除停止标志。**由 `Agent.run()` 在每轮任务开头调用一次。**

        为什么清零点不放在 `send()` 里：标志要能覆盖「用户在工具执行阶段
        按停止」这种情况 —— 那时没有任何 `send()` 在跑，若清零点在
        `send()` 入口，标志会在**下一次** `send()` 一开始就被清掉，
        用户的停止请求被静默吞掉。放在 `run()` 开头则语义清晰：
        **一次任务一次清零**，任务内的停止请求一定生效。
        """
        return None

    @abstractmethod
    async def send(
        self,
        prompt: str,
        session: SessionMeta,
        *,
        temporary: bool = True,
        extended_thinking: bool = False,
        search_enabled: bool = False,
        files: list[str] | None = None,
        on_delta: DeltaCallback | None = None,
        on_thought: ThoughtCallback | None = None,
    ) -> TurnOutcome:
        """发送一轮，返回模型输出与更新后的会话三元组。

        三个开关都由 `ChatConfig` 提供，属于**会话级**设置（改它不重启引擎）：

        * `temporary`         —— 临时对话（不写入网页版历史）
        * `extended_thinking` —— 思考模式（更强的推理档位）
        * `search_enabled`    —— 联网搜索（对应网页版顶栏那个搜索按钮）

        它们必须由实现方显式转发给上游。**默认值只是兜底，不是语义**：
        上游这几个参数默认都是关闭，而本项目默认要「一般对话」—— 所以调用方
        一定要把配置里的值传进来，别依赖这里的默认值。

        `on_thought` 是思考通道的增量回调（见 `ThoughtCallback`）。**实现方必须
        转发它**，否则开了思考模式之后界面会长时间一片空白 —— 上游先把思考
        token 流完才吐正文，实测「只回复两个字」这种题也要 ~90 秒，而这段时间
        正文通道一个字都没有。用户看到的「发了没反应」就是这么来的。
        """

    # ---------- 只读：服务端会话 ----------
    #
    # **默认实现直接抛错，不是 pass。** 这两个能力依赖具体的上游接口，
    # 不是每个档位都有；默认抛错能把「切了档位之后功能静默失效」变成一声明响。
    # 静默失效是本项目最想避免的失败形态 —— 它让界面看起来一切正常。

    async def list_sessions(self, count: int = 30) -> list[dict]:
        """列出账号下的会话（只读）。档位不支持时抛 `TransportError`。"""
        raise TransportError(
            f"传输档位 {self.name!r} 不支持列出会话", config=True
        )

    async def fetch_history(self, chat_session_id: str) -> dict:
        """拉某个会话的完整历史（只读）。档位不支持时抛 `TransportError`。"""
        raise TransportError(
            f"传输档位 {self.name!r} 不支持读取会话历史", config=True
        )

    # ---------- 写：服务端会话生命周期 ----------
    #
    # `create` / `delete` 是 2026-10-18 才进抽象层的。在此之前，
    # `chat_session/create` 只在 `send()` 内部被隐式调用（没有会话时自动建），
    # 而 `chat_session/delete` **根本没有被实现** —— 账号里堆的测试会话
    # 只能靠网页版手工删。
    #
    # 两者同样用「默认抛错」而不是 `pass`：删除是不可逆操作，
    # 静默失效会让人以为删了、其实没删（或反过来）。宁可炸。

    async def create_session(self) -> str:
        """显式新建一个空会话，返回它的 id。

        与 `send()` 内部的自动建会话**不是同一件事**：那个是「没有会话就顺手建
        一个」，返回值被 `send()` 吃掉；这个是给上层（会话管理界面）用的，
        调用方需要拿到 id 自己持有。

        档位不支持时抛 `TransportError`。
        """
        raise TransportError(
            f"传输档位 {self.name!r} 不支持新建会话", config=True
        )

    async def delete_session(self, chat_session_id: str) -> None:
        """永久删除一个服务端会话。**不可逆**。

        调用方（界面）必须自己先做二次确认 —— 传输层不做交互，只做执行。
        服务端对不存在的 id 也返回成功（幂等），所以「没报错」不等于
        「删掉了一条真实存在的会话」。

        档位不支持时抛 `TransportError`。
        """
        raise TransportError(
            f"传输档位 {self.name!r} 不支持删除会话", config=True
        )

    # ---------- 朗读（TTS）控制面 ----------
    #
    # 默认全部「不支持」—— 只有 `DeepSeekRpcTransport` 实现了它们。
    # 这样写的理由：T4（真 Chrome）理论上也能做（页面里 `fetch` 就行），
    # 但那需要把 ticket 塞进页面上下文再取回，多一层状态同步；
    # 而 T3 已经是默认档位，先只做 T3，别为一条还没人用的路径增加复杂度。
    # 将来真要做，就在对应子类里覆盖这三个方法 —— 上层代码一个字不用改。

    async def fetch_tts_ticket(self, chat_session_id: str, message_id: str) -> str:
        """签一张一次性朗读票据（36 字符）。

        票据与 scope 绑定、短时效、只对单条消息有效 —— **不是账号 token**，
        所以可以交给前端。档位不支持时抛 `TransportError`。
        """
        raise TransportError(
            f"传输档位 {self.name!r} 不支持朗读", config=True
        )

    async def list_tts_voices(self) -> list[dict]:
        """列出朗读音色。只读，不消耗朗读配额。

        档位不支持时抛 `TransportError`。
        """
        raise TransportError(
            f"传输档位 {self.name!r} 不支持朗读", config=True
        )

    async def set_tts_voice(self, voice_id: str) -> None:
        """切换朗读音色（账号级偏好，网页版同步生效）。

        档位不支持时抛 `TransportError`。
        """
        raise TransportError(
            f"传输档位 {self.name!r} 不支持朗读", config=True
        )
