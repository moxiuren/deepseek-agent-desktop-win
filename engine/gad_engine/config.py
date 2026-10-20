"""配置层（DeepSeek 版）。

四组配置对应四层闸门：
  IdentityConfig  -> L1 身份层
  PacingConfig    -> L2 节奏层
  CircuitConfig   -> L4 熔断层
  ExecutorConfig  -> 执行层
L3 会话层由 SessionManager 自行管理，无需用户配置。

另外三组不参与闸门判断，只决定「请求长什么样」与「提示词从哪来」：
  ChatConfig        -> 单轮请求形态（临时对话 / 思考模式 / 联网搜索）
  PromptConfig      -> 初始提示词模板
  MaintenanceConfig -> 后台维护（本档位为空）

**与 gemini 版的三处结构性差异**（不是抄漏，是 DeepSeek 侧真的不一样）
--------------------------------------------------------------------

1. **没有访客模式。** Gemini 可以不登录（零账号可封，爆炸半径为零）；DeepSeek
   **必须登录**，token 存在 `localStorage.userToken`。所以 `IdentityConfig`
   里没有 `guest_mode` —— 这不是没实现，是这个选项在 DeepSeek 侧不存在。
   ⇒ 「封号」在这里**一定是账号级连坐**，没有逃生舱。

2. **没有 cookie。** 鉴权走 `Authorization: Bearer <token>`，全项目零 Cookie
   （2026-10-14 实测）。所以 gemini 版那一大段「cookie 轮换缓存落点」的逻辑
   整段不存在，取而代之的是一个 token 文件。

3. **没有主动遥测通道。** Gemini 有 `otAQ7b` / `GPRiHf` / `qpEbW` 三个接口能
   **事前**读到账号状态；DeepSeek 已知接口只有 3 个（`chat/completion` /
   `chat/create_pow_challenge` / `chat_session/create`），**没有任何额度或账号
   状态接口**。所以 L4 熔断层在这里**只能事后刹车**。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from .identity import DEFAULT_TOKEN_FILE
from .permissions import PermissionConfig
from .protocol import DEFAULT_PROMPT_TEMPLATE

APP_DIR_NAME = "DeepSeek-Agent"
CONFIG_FILE_NAME = "config.json"


def app_data_dir() -> Path:
    """%LOCALAPPDATA%\\DeepSeek-Agent（非 Windows 时退回用户目录）。"""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
    path = Path(base) / APP_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_config_path() -> Path:
    """配置文件的默认路径。可用环境变量 `GAD_CONFIG_PATH` 覆盖。

    **为什么要有这个覆盖口**：同进程的测试可以直接把 `EngineConfig` 换成
    `sandbox()`，但 `test_engine_rpc.py` 起的是**真子进程**（`python -m
    gad_engine`），换不了对象 —— 只能从入口处把路径指走。不指的话子进程
    会加载**用户的真实 `config.json`**，而 `chat.set` / `model.set` 内部有
    `self.cfg.save()`。2026-10-16 实测踩到：该测试用 `chat.set` 验
    「字符串 'true' 被正确解析」，把用户的 `extended_thinking` 写成了 `true`。
    """
    override = os.environ.get("GAD_CONFIG_PATH")
    if override:
        return Path(override)
    return app_data_dir() / CONFIG_FILE_NAME


def _filter(cls: type, data: Any) -> dict:
    """只保留目标 dataclass 认识的键，避免旧配置多出字段时炸掉。"""
    if not isinstance(data, dict):
        return {}
    known = {f.name for f in fields(cls)}
    return {k: v for k, v in data.items() if k in known}


@dataclass
class IdentityConfig:
    """L1 身份层 —— **DeepSeek 侧没有「零风险档」**。

    这里的每一项都在影响同一件事：**这个客户端看起来像不像那个真浏览器**。
    原版（`deepseek-mac`）栽的就是这一层 —— 它用 .NET `HttpClient`（Schannel 的
    TLS 栈）发请求，UA 却写死 `Chrome/133`。**声称 Chrome、握手不像 Chrome**，
    是风控里最经典也最容易命中的特征。

    本项目的对策是 `browser_impersonate` + **UA 与它联动**（见
    `transport/deepseek_rpc.py`：UA 从 impersonate 版本号派生，两者不可能漂移）。
    """

    # 账号标识（仅用于日志与凭据文件命名，不参与请求）
    account_label: str = ""

    # token 缓存文件。留空 = 用项目内的 `.token`（见 identity.DEFAULT_TOKEN_FILE）。
    #
    # **它是明文凭据**，等同账号密码：只落本机、不进 Vault、不进记忆文件。
    # 失效后删掉重取即可（用 CDP 从已登录的浏览器读一次）。
    token_file: str = ""

    # 取 token 用的浏览器（阶段 3）。**只用于取凭据，对话全程不需要它。**
    chrome_profile_dir: str = ""
    chrome_binary: str = ""
    debug_port: int = 9333

    # TLS + HTTP/2 指纹伪装档位。**必须与 `user_agent` 联动** ——
    # 本项目的 UA 由它派生，不单独配置，就是为了防止「UA 说 155、TLS 说 150」。
    # 可选值取决于 `curl_cffi` 版本（实测本机最高 chrome150）。
    browser_impersonate: str = "chrome150"

    proxy: str = ""

    # 是否要求这是专用账号（不与其它自动化共用）。
    # DeepSeek 没有访客模式 ⇒ 封号一定是账号级，所以这个提示比 Gemini 侧更重要。
    require_dedicated_account: bool = True

    # 会话使用的模型名。原版全项目只有 `default` 一个值（实测），不臆造其它档位。
    model: str = "default"

    # ---------- Gemini 档位专用 ----------
    #
    # 下面两项只在 `transport` 指向 `gemini_rpc` 时有意义。放在这里而不是
    # 新开一个 `GeminiConfig`，是为了让 `EngineConfig.from_dict` 与
    # `_filter()` 的既有逻辑原样复用 —— 旧配置文件里没有这两个键时，
    # `_filter` 会丢弃它们、dataclass 用默认值，升级不需要迁移脚本。
    #
    # 访客模式：不传任何 cookie，上游走匿名会话（仅 `gemini-flash-lite`）。
    # **访客模式下 1016 UNAUTHENTICATED 是正常态** —— `GeminiRpcTransport.health()`
    # 会把它归一成「可达」，否则熔断器会在第一次健康检查时把自己锁死，
    # 而现象是「引擎起不来、什么都做不了」，极难归因到「访客模式没特判」。
    guest_mode: bool = False

    # Gemini 的 cookie 文件（需含 `__Secure-1PSID` + `__Secure-1PSIDTS`）。
    # 留空 = 用 `%LOCALAPPDATA%\Gemini-Agent\cookies-<account_label>.json`。
    cookie_file: str = ""

    def violations(self) -> list[str]:
        """返回**阻塞性**违规项。空列表表示身份层配置合规。"""
        out: list[str] = []
        # **访客模式下这条前提不成立。**
        #
        # `require_dedicated_account` 问的是「这个自动化会不会连累主号」——
        # 而访客模式压根没有登录，没有账号可连累，答案是天然满足。
        #
        # 不短路会踩一个很难归因的坑：全新安装时 `account_label` 为空，
        # 用户切到 Gemini 访客档 → L1 判定违规 → `GateDecision.fatal=True`
        # → 引擎起得来、状态也正常，但**一个字都发不出去**，而报错文案
        # 指向「专用账号标识」。用户会去填一个访客模式根本不需要的字段，
        # 填完还是不生效（填了也不影响访客会话）。
        if (
            self.require_dedicated_account
            and not self.guest_mode
            and not self.account_label
        ):
            out.append("未声明专用账号标识：无法确认与主号隔离（DeepSeek 无访客模式，封号即账号级）")
        return out

    def notices(self) -> list[str]:
        """非阻塞性提示。

        2026-10-17：按用户要求移除「DeepSeek 无访客模式」那条启动提示 ——
        它每次启动都会进对话流，属于噪音；账号级风险已由 L1 闸门与身份面板承担。
        """
        return []

    @property
    def mode_label(self) -> str:
        # 访客模式优先判 —— `guest_mode=True` 时 `account_label` 通常是空的，
        # 直接走下面那行会显示成「账号模式 · 未命名」，与事实相反。
        # 2026-10-19 实测踩到：配置里 `guest_mode: true`，`status` 却报「账号模式」。
        if self.guest_mode:
            return "访客模式"
        return f"账号模式 · {self.account_label or '未命名'}"


@dataclass
class ChatConfig:
    """单轮请求形态 —— 决定「这一轮请求长什么样」。

    这几项与 `identity.model` 同属**会话级**设置：改它不重启引擎，
    下一轮发送自然生效（它们都写在每轮请求的请求体里，不是启动参数）。

    注意与 `identity` 的区别：`identity` 改的是「用哪个账号」，那必须重启；
    这里改的是「用这个账号怎么说话」，下一轮就变。
    """

    # 临时对话（不写入网页版历史）。
    #
    # **⚠️ 未实现。** DeepSeek 侧的对应接口/字段尚未查明（实测
    # `chat_session/create` 的请求体是空的 `{}`，没有临时标志位）。
    # 当前无论设什么值，会话**都会**出现在账号历史里。留这个字段是为了
    # 让上层接口稳定，等查明后再接。
    temporary: bool = False

    # 思考模式。对应请求体的 `thinking_enabled`。
    #
    # **实测（2026-10-14）**：真实网页版在用户没开时发的是 `false`（按需开关），
    # 而原版 `DeepSeekApiClient.cs` **恒写 `true`** —— 真实用户不会每次都开满，
    # 恒开本身可能就是机器特征。所以这里默认 `False`，跟随真实行为。
    extended_thinking: bool = False

    # 联网搜索。对应请求体的 `search_enabled`。理由同上：按需，不恒开。
    search_enabled: bool = False

    # 思考过程（上游 `THINK` 片段）是否输出到界面 / 写入本地历史。
    #
    # **2026-10-15 之前这里是「不输出」的** —— 传输层其实一直把思考原文
    # 装在 `TurnOutcome.thoughts` 里交回，但 `agent.py` 的 `on_thought`
    # 只上报「已思考多少字 + 最近 120 字」，界面也只有一个「思考中…（已 N 字）」
    # 的心跳条。当时的理由是「思考内容不是回答，塞进对话流会淹掉正文」。
    #
    # 用户明确要看到思考原文，所以改成默认开：思考时把原文增量推给界面，
    # 界面渲染成一个**可折叠**的「思考过程」块 —— 正文一开始流就自动收起，
    # 既能看到，又不会把回答顶下去。关掉则完全回到老行为（只有心跳）。
    #
    # 一个开关同时管「显示」和「入库」：分开配的话会出现「界面显示但不入库」
    # 这种半吊子状态，切回对话时思考又没了，反而更难解释。
    show_thinking: bool = True


@dataclass
class PacingConfig:
    """L2 节奏层 —— 目标不是限流，是「像不像人」。

    间隔用 log-normal 而非固定值：固定间隔本身就是机器特征，
    均匀抖动仍然是个窄分布，log-normal 更接近真人敲字—等待—阅读的形态。

    **DeepSeek 侧这条特别要紧**：实测（2026-10-15）连发 17 分钟内 9 次
    `completion` 就撞上服务端 68 秒「生成超时」。原版的按分钟限流记录
    （`2026-09-12 Messages too frequent`）也是同一个机制。
    """

    base_gap_ms: float = 2500.0
    sigma: float = 0.55
    floor_gap_ms: float = 200.0
    max_gap_ms: float = 20000.0

    window_ms: int = 10000
    max_burst: int = 2
    hold_after_burst_ms: int = 12000
    burst_penalty_ms: int = 2500

    #: 昼夜节律窗口。**2026-10-18 起默认取消**（原为 8/24）。
    #:
    #: 取消原因：这一层原本是想让发送时间「像人」——夜里不活动。
    #: 但实现上 `evaluate()` 命中非活跃时段只返回 `_hold(60_000)`，
    #: 而判定条件本身是「当前小时是否落在窗口内」——
    #: 在 0:00–8:00 之间这个条件**永远不会成立**，于是每 60 秒重判一次、
    #: 无限静默等待：消息发不出去、界面一直显示「运行中」、点取消也无效
    #: （卡在 sleep 里）。实测 `session.turns` 停在 0，一条都发不出。
    #:
    #: 而且「夜间不活动」这个假设本身站不住：真人也会半夜用，
    #: 反倒是「某个账号从不在夜里出现」才更像脚本。
    #: 真要恢复，把 start/end 改回 8/24 即可 —— 但请先把上面的
    #: 死等逻辑改成「明确拒绝 + 可取消」，否则会原样复现。
    active_start_hour: int = 0
    active_end_hour: int = 24

    read_pause_every: int = 6
    read_pause_min_ms: int = 4000
    read_pause_max_ms: int = 9000

    #: ★ **单小时发送硬顶**（0 = 关闭）。
    #:
    #: 2026-10-17 事故复盘（`docs/20261017-防封禁研究分析.md`）补的缺口：
    #: 从 102 份引擎日志里按 `pacing.stats.sends` 重建的真实发送量是
    #: **峰值 75 次/小时**。而上面那组参数（`max_burst=2` + `window_ms=10000`
    #: + `hold_after_burst_ms=12000`）的理论上限约 **600 次/小时** ——
    #: 也就是说节奏层**根本挡不住**「一小时 75 次」这种量级。
    #:
    #: **★ 但 75 这个数一开始被我误读了，这里记下来。**
    #: 我最初按「75 次/小时」直接定了 `hourly_max = 20`，理由写的是
    #: 「20 是重度真人用户的量级」—— 那句话**没有测量支撑，是错的**。
    #: 补测（`.tmp/per_run.py`，只读日志）之后才看清单位：
    #:
    #:   * `sends` 数的是 **agent 轮次**，不是**用户消息**：
    #:     `max_tool_turns` 默认 **30** ⇒ **一条用户消息最多合法地消耗 30 次发送**；
    #:   * 实测 **405 次发送 / 80 条用户消息 = 平均 5.1 次/条**；
    #:     单条消息消耗 **中位 5 · p90 24 · 最大 61**；
    #:   * 峰值那一小时（75 次）其实是 **10 条用户消息** ——
    #:     是**正常重度使用**，不是机器特征。
    #:
    #: ⇒ **`hourly_max = 20` 会把正常任务掐断在中间**（p90 的一条消息就要 24 次）。
    #:   这个数当时就错了，是小莫指出「20 基本上不能用」之后重新量才发现的。
    #:
    #: 现值 **150 = 实测峰值的 2 倍**，定位从「限制用量」改成「**兜住失控**」：
    #:   * 正常使用（含峰值 75）**永不触发**；
    #:   * 真正的失控（卡住重试、脚本循环）几百次/小时，会被它拦住。
    #:
    #: 语义是**排队，不是拒绝**：一小时到量就等最早那次滑出窗口。
    #: 想更严就调小，想关就设 0 —— 但**先想清楚单位是「发送」而不是「消息」**。
    hourly_max: int = 150

    #: 长停顿：每 N 次发送插一次长休（0 = 关闭）。
    #: 与 `read_pause_every` 的区别：那个是 4–9 秒的「读一读」，这个是分钟级的
    #: 「人去做别的事了」。默认关 —— 它明显影响体验，由用户自己决定开不开。
    long_pause_every: int = 0
    long_pause_min_ms: int = 60000
    long_pause_max_ms: int = 300000

    #: **是否自动轮换会话（默认关闭 —— 把「什么时候换会话」交给用户）。**
    #:
    #: 关掉是刻意的，理由有两个：
    #:
    #:   ① **自动轮换是静默的。** 它新建一条服务端会话，把上下文整个换掉，而
    #:      界面**一点变化都没有** —— 用户只会觉得「模型突然失忆了」。实测
    #:      2026-10-15 的 `turns` 重复计数 bug 就是这么被发现的：配置写 25 轮，
    #:      实际约 12 轮就悄悄换了会话。
    #:   ② **现在有更好的做法。** 界面上有「接力新会话」按钮 —— 用户自己决定
    #:      什么时候换，换的时候**先把上下文压成摘要注入新会话**（见 `compact.py`）。
    #:      这比「到点就静默失忆」好得多，而且不需要猜一个安全的轮数上限。
    #:
    #: 打开它 = 恢复原来的保守行为（`max_turns_per_session` /
    #: `max_session_age_sec` 生效）。**注意**：接管的会话（`SessionMeta.attached`）
    #: 在任何情况下都不自动轮换，与这一项无关。
    auto_rotate: bool = False

    # 单会话最大轮次。**原版是 25 轮 / 20 分钟** —— 那是它为了躲
    # 「同会话配 parent_message_id: null 会新建 root → 会话树损坏 → 全局吊销 40003」
    # 而做的妥协。直连模式下 40003 是否仍触发**尚未长期验证**，所以先沿用保守值。
    #
    # **只有 `auto_rotate=True` 时才起作用。**
    max_turns_per_session: int = 25

    # 单会话最长存活（秒）。同上，沿用原版 20 分钟。同样只在 `auto_rotate` 打开时生效。
    max_session_age_sec: int = 20 * 60


@dataclass
class PowConfig:
    """PoW 预取缓存（2026-10-18 起）。

    **为什么单独一段**：它既不是节奏（`pacing`）也不是身份（`identity`）——
    它是传输层的行为优化。2026-10-18 实测（`docs/20261017-防封禁研究分析.md`
    §2.5）：真实网页版一次**并发取 3 个** challenge 并全部解好缓存，
    第二次发送 0 个 challenge 请求、0 ms 求解；我们每次同步解 5.1–7.7 秒。
    这是目前测到的最硬的行为差异。

    `prefetch=False` 可一键退回「每次发送同步取 + 同步解」的旧行为，
    便于对照与排障。
    """

    #: 是否在后台预取并缓存已解好的 PoW。
    prefetch: bool = True
    #: 缓存容量。与网页版「一次取 3 个」同量级；2 个已能覆盖连续两次发送。
    prefetch_count: int = 2
    #: 剩余寿命低于该值的缓存条目直接丢弃 —— 宁可 miss 去同步解，
    #: 也不发一个快过期的（服务端 `expire_after` 实测 300000 ms = 5 分钟）。
    expire_margin_sec: int = 30


@dataclass
class MaintenanceConfig:
    """后台维护 —— **本档位为空，这是有意的**。

    Gemini 侧这一层是「机器特征最集中的地方」：上游库会拉起两个固定节拍的后台任务
    （cookie 轮换 `600s±15s` + 活跃心跳 `60~120s`），实测**空转 130 秒就发 1 次请求、
    折算 30~60 次/小时且 24 小时不停**，还和项目自己的「8:00–24:00 活跃时段」自相矛盾。

    DeepSeek 侧**一个都不需要**：

      * **无 cookie** ⇒ 没有 cookie 轮换
      * **无长连接/心跳概念** ⇒ 没有保活心跳

    所以「用户不用它的时候，它一个字都不发」在这里是**天然成立**的，
    不需要像 Gemini 那样专门去接管上游的心跳。

    保留这个 dataclass 只为让上层接口稳定（`EngineConfig` 仍持有它）。
    """

    enabled: bool = False
    note: str = "本档位无后台任务（无 cookie 轮换、无心跳）"

    # ---------- Gemini 档位专用 ----------
    #
    # 下面三项只在 `transport == "gemini_rpc"` 时被读取。DeepSeek 档位
    # `enabled=False`，整个 `_apply_background_policy()` 直接短路，不看它们。
    #
    # 背景（见 gemini 版 README）：上游库 `init(auto_refresh=True)` 会拉起
    # **两个固定节拍**的后台任务 —— cookie 轮换（600s±15s）与活跃心跳
    # （60~120s，实测空转 130 秒就发 1 次、折合 30~60 次/小时且 24 小时不停）。
    # 心跳是「机器特征」最集中的地方，所以由我们接管开关：
    #   * cookie 轮换**保留**（关掉会隔夜掉登录）
    #   * 心跳**空闲即静默**，只在有任务在跑时开着
    #
    # ⚠️ **`enabled` 只管心跳，管不到 cookie 轮换。** 2026-10-19 源码核实：
    # `gemini_rpc.py` 全文只碰上游的 `activity_task`（三处：
    # `_apply_background_policy` 的读 / 建 / 取消），**`refresh_task` 一次都
    # 没有出现**。轮换任务由 `_ensure_client()` 里的 `init(auto_refresh=...)`
    # 控制，而那个实参只跟 `guest_mode` 有关，与本类的 `enabled` 无关。
    #
    # 换句话说：Gemini 档位下把 `enabled` 设成 False（默认值）**不会**导致
    # 隔夜掉登录。真会掉登录的是 `guest_mode` 切换 —— 访客本来就没有 cookie。
    # 这一点曾经被误读成「隐患」，记在这里防止再踩。
    heartbeat_while_idle: bool = False
    #
    # 心跳是否遵守「活跃时段」。默认 True —— 深夜零流量本身就是人味，
    # 而固定节拍全天不停恰恰是最容易识别的机器特征。
    respect_active_hours: bool = True
    #
    # 巡检间隔（毫秒）。低于 5000 会被实现方夹到 5 秒 —— 巡检本身不发请求，
    # 但也没必要比这更密。
    supervise_interval_ms: int = 30_000


@dataclass
class CircuitConfig:
    """L4 熔断层 —— **DeepSeek 侧只能事后刹车**。

    Gemini 有 `otAQ7b` / `GPRiHf` / `qpEbW` 三个通道能**事前**读到账号状态，
    所以那边有 `abuse_freeze_ms` / `quota_*` 这类「提前刹车」的参数。

    DeepSeek 已知接口只有 3 个，**没有任何额度或账号状态接口** ⇒ 主动遥测不可得。
    能依赖的信号只有响应本身：

      * `401` / `403`            → 认证失败（可能是 token 过期，也可能是被处置）
      * `429`                    → 限流
      * `Server busy` + `generation_timeout` → **生成阶段被掐**（实测 2026-10-15）
      * `biz_code != 0`          → 业务错误
    """

    # 认证失败退避（指数）
    auth_backoff_ms: int = 60_000
    auth_backoff_max_ms: int = 300_000

    # 硬停机（命中确定性封禁信号后）
    hard_stop_ms: int = 86_400_000

    # ---- 生成超时退避（2026-10-15 实测新增）----
    #
    # 服务端在过载/软限流时**不报错**，而是把连接挂 68 秒后回
    # `{"type":"error","content":"Server busy...","finish_reason":"generation_timeout"}`。
    # 这是一个**前置的、可观测的**降级信号 —— 正好补上「没有主动遥测」的短板：
    # 它给了「在被封之前刹车」的窗口。
    busy_backoff_ms: int = 1_800_000        # 单次命中后退避 30 分钟
    busy_streak_cooldown: int = 3           # 连续 3 次 → 长冷却
    busy_cooldown_ms: int = 21_600_000      # 长冷却 6 小时

    # 连续 N 次 RPC 异常 → 判定传输层不健康
    degrade_after_rpc_errors: int = 3

    # 状态轮询间隔。**注意**：DeepSeek 没有状态接口，所以这个轮询只能靠
    # 打真实业务接口来「顺便」探测，属额外机器特征 —— 默认关（见 transport 的
    # `health_probe`）。
    status_poll_ms: int = 60_000
    health_probe_enabled: bool = False


@dataclass
class ExecutorConfig:
    """执行层参数。数值全部沿用 deepseek-agent-desktop 的实战取值。"""

    work_dir: str = ""
    command_timeout_ms: int = 180_000
    queue_timeout_ms: int = 120_000

    direct_output_limit: int = 16_000
    head_tail_chunk: int = 7_500
    hard_output_limit: int = 5 * 1024 * 1024

    max_stream_chunks: int = 1000
    scan_depth_limit: int = 3

    shell_binary: str = ""

    #: 相邻的**只读**工具调用是否并发执行（read_file / grep / glob /
    #: list_dir / web_fetch / web_search）。有副作用的工具永远串行 ——
    #: 它们的顺序有语义。关掉它就回到全串行（排查并行引入的问题时用）。
    parallel_reads: bool = True


@dataclass
class PromptConfig:
    """初始提示词从哪来。

    为什么要做成配置：这个软件要能给别人用，而「初始提示词」是最需要让每个用户
    自己改的东西 —— 人格、语气、边界、要不要让它先问一句再动手，没有一套写法
    适合所有人。所以它不再是代码里的常量，而是一个**可编辑的模板文件**。
    """

    #: 模板文件路径。留空 = 用默认位置 `<app_data>/prompts/system-prompt.md`。
    template_file: str = ""

    #: 文件不存在时，是否把内置默认模板写过去（首次运行自动生成一份可编辑的）。
    #: 只影响「文件不存在」这一种情况 —— 用户清空或改坏之后**不会**被悄悄覆盖回来。
    seed_default: bool = True


@dataclass
class MemoryConfig:
    """跨会话长期记忆（见 `memory.py`）。

    **为什么是「开关 + 文件 + 上限」三个字段**：
      * `enabled` —— 记忆注入是往每轮提示词里加东西，有些用法（纯一次性任务）
        不想被历史偏好干扰，得能整体关掉
      * `file` —— 留空用默认位置，但允许指向一个被 Git / Obsidian 管理的文件，
        这样记忆就能跨机器同步（本机是双机环境，这一点是刚需）
      * `max_chars` —— 记忆会长，而提示词每轮都要发。没有上限，某天它会悄悄
        把提示词挤爆，症状是「模型开始变笨」，没人会想到是记忆文件太长
    """

    enabled: bool = True
    #: 记忆文件路径。留空 = `<app_data>/memory.md`。
    file: str = ""
    #: 是否注入系统提示词。关掉后记忆仍在，只是这轮不带。
    inject: bool = True
    #: 注入上限（字符）。超出部分截断，并在提示词里注明。
    max_chars: int = 4000


@dataclass
class CompactConfig:
    """换会话时的上下文压缩（见 `compact.py`）。

    **为什么是「开关 + 模式 + 上限」三个字段**：
      * `enabled` —— 压缩默认开，但用户换话题时可能就想要一张白纸，得能关。
        关掉后「接力新会话」退化成「新建对话」（旧对话仍留在列表里）。
      * `mode` —— 决定摘要怎么来：
          - `model`：在当前会话里问一次（服务端已持有完整上下文，只发一句
            指令）。质量最高，代价是**一次请求** —— 走 `PacingGate`，失败
            自动降级到 `local`。
          - `local`：纯规则抽取，零请求。防封优先、或不想让上游看到
            「总结对话」这种动作时用。
      * `max_chars` —— 摘要要注入到新会话**每一轮**的提示词里。没有上限的话，
        多代接力之后它会悄悄把提示词挤爆，而症状是「模型开始变笨」。
    """

    enabled: bool = True
    #: `model`（问一次模型）| `local`（纯规则，零请求）。
    mode: str = "model"
    #: 摘要注入上限（字符）。超出截断并注明。
    max_chars: int = 1500


@dataclass
class McpConfig:
    """MCP（Model Context Protocol）服务器。

    **为什么用「原始 dict 列表」而不是强类型**：MCP server 的启动形态
    千差万别（npx / uvx / python / 本地 exe…），配置字段以官方 `mcpServers`
    段为准。强类型化只会让用户抄现成配置时多一层翻译，且每次官方加字段
    都要改代码。这里只做**透传 + 校验**，把解释交给 `mcp.McpServerConfig`。
    """

    enabled: bool = False
    #: 每个元素形如 `{"command": "npx", "args": [...], "env": {...}}`。
    #: 键就是 server 名。
    servers: dict = field(default_factory=dict)


@dataclass
class EngineConfig:
    identity: IdentityConfig = field(default_factory=IdentityConfig)
    chat: ChatConfig = field(default_factory=ChatConfig)
    pacing: PacingConfig = field(default_factory=PacingConfig)
    circuit: CircuitConfig = field(default_factory=CircuitConfig)
    executor: ExecutorConfig = field(default_factory=ExecutorConfig)
    maintenance: MaintenanceConfig = field(default_factory=MaintenanceConfig)
    prompt: PromptConfig = field(default_factory=PromptConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    compact: CompactConfig = field(default_factory=CompactConfig)
    mcp: McpConfig = field(default_factory=McpConfig)
    pow: PowConfig = field(default_factory=PowConfig)
    #: 工具权限分级 + dry-run。默认 `exec` / 关闭 —— 与升级前行为一致。
    #: 详见 `permissions.py` 模块头。
    permissions: PermissionConfig = field(default_factory=PermissionConfig)

    transport: str = "deepseek_rpc"
    soul_file: str = ""
    max_tool_turns: int = 30

    #: 这份配置**是从哪个文件加载的**。`save()` 不显式传路径时写回这里，
    #: 而不是无条件写 `default_config_path()`。
    #:
    #: **为什么必须记这个**：`save()` 以前无条件写默认路径，于是
    #: 「在内存里造一份 cfg（其路径字段指向临时目录）→ 调某个内部会 save 的方法」
    #: 就会把**临时路径永久写进用户的真实 config.json**。
    #: 2026-10-16 实测踩到：`test_thinking.py` 的 `scenario_toggle` 调
    #: `rt.set_chat()` 验证「开关能中途改」，而 `set_chat` 内部有一句
    #: `self.cfg.save()`；测试的 `make_cfg()` 把模板与记忆指向
    #: `<临时目录>/toggle/`，于是真实配置被写成那个路径，而临时目录随即被删。
    #: 用户侧的表现是「启动后提示词与记忆都读不到」。
    #:
    #: 类型用 `str | None` 而不是 `Path | None`：`__main__.py` 会用
    #: `json.dumps(cfg, default=lambda o: o.__dict__)` 把配置塞进 RPC 响应，
    #: `Path` 在那条路径上不可序列化，会直接把 `initialize` 打崩。
    source_path: str | None = None

    @classmethod
    def from_dict(cls, data: Any) -> EngineConfig:
        if not isinstance(data, dict):
            return cls()
        return cls(
            identity=IdentityConfig(**_filter(IdentityConfig, data.get("identity"))),
            chat=ChatConfig(**_filter(ChatConfig, data.get("chat"))),
            pacing=PacingConfig(**_filter(PacingConfig, data.get("pacing"))),
            circuit=CircuitConfig(**_filter(CircuitConfig, data.get("circuit"))),
            executor=ExecutorConfig(**_filter(ExecutorConfig, data.get("executor"))),
            maintenance=MaintenanceConfig(
                **_filter(MaintenanceConfig, data.get("maintenance"))
            ),
            prompt=PromptConfig(**_filter(PromptConfig, data.get("prompt"))),
            memory=MemoryConfig(**_filter(MemoryConfig, data.get("memory"))),
            compact=CompactConfig(**_filter(CompactConfig, data.get("compact"))),
            mcp=McpConfig(**_filter(McpConfig, data.get("mcp"))),
            pow=PowConfig(**_filter(PowConfig, data.get("pow"))),
            permissions=PermissionConfig(
                **_filter(PermissionConfig, data.get("permissions"))
            ),
            transport=str(data.get("transport") or "deepseek_rpc"),
            soul_file=str(data.get("soul_file") or ""),
            max_tool_turns=int(data.get("max_tool_turns") or 30),
        )

    @classmethod
    def load(cls, path: Path | str | None = None) -> EngineConfig:
        p = Path(path) if path else default_config_path()
        if not p.exists():
            cfg = cls()
            cfg.source_path = str(p)      # 文件还没有，但记住「以后该写这里」
            return cfg
        try:
            cfg = cls.from_dict(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            cfg = cls()
        cfg.source_path = str(p)
        return cfg

    def save(self, path: Path | str | None = None) -> Path:
        # 优先级：**显式传的路径 > 这份配置的来源路径 > 默认路径**。
        # 中间那一层是测试隔离的关键 —— 见 `source_path` 的注释。
        p = Path(path) if path else Path(self.source_path or default_config_path())
        p.parent.mkdir(parents=True, exist_ok=True)
        # 必须用 `asdict`：它会**递归**把嵌套的 IdentityConfig / ChatConfig / …
        # 转成 dict。手写 `getattr` 只做一层，嵌套 dataclass 会原样留在里面，
        # `json.dumps` 直接抛 `TypeError: Object of type IdentityConfig is not
        # JSON serializable`。所以是「用 asdict，再摘掉 source_path」。
        data = {k: v for k, v in asdict(self).items() if k != "source_path"}
        p.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.source_path = str(p)
        return p

    @classmethod
    def sandbox(cls, tmp: Path | str) -> EngineConfig:
        """造一份**完全隔离**的配置：`save()` 只会写进 `tmp`，绝不碰真实 AppData。

        **测试必须用它，不要裸 `EngineConfig()`。** 因为 `set_chat` / `set_model`
        这些方法内部会 `self.cfg.save()`，而裸构造的 cfg 没有来源路径，
        `save()` 就会落到用户的**真实 `config.json`**。
        2026-10-16 实测踩到：`test_thinking.py` 调 `rt.set_chat()` 验证
        「开关能中途改」，把 `<临时目录>/toggle/` 写进了用户配置，
        而临时目录随即被删 —— 用户侧的表现是「提示词与记忆都读不到」。
        """
        cfg = cls()
        cfg.source_path = str(Path(tmp) / "config.json")
        return cfg

    def read_soul(self) -> str:
        """读取 Agent-Soul.md（存在且够长才用）。"""
        if not self.soul_file:
            return ""
        try:
            text = Path(self.soul_file).expanduser().read_text(encoding="utf-8")
        except OSError:
            return ""
        return text if len(text) > 200 else ""

    # ---------- 路径解析 ----------

    def resolved_token_file(self) -> Path:
        """当前身份**实际**用的 token 文件路径。

        抽成方法而不是各处硬编码：探针、引擎、UI 必须指向同一个文件 ——
        否则会出现「在设置里刷新了 token，但引擎还在读旧文件」这种最难查的故障。
        """
        configured = (self.identity.token_file or "").strip()
        if configured:
            return Path(configured).expanduser()
        return DEFAULT_TOKEN_FILE

    def prompt_template_path(self) -> Path:
        """模板文件的实际路径。配置留空就用默认位置。"""
        configured = (self.prompt.template_file or "").strip()
        if configured:
            return Path(configured).expanduser()
        return app_data_dir() / "prompts" / "system-prompt.md"

    def memory_path(self) -> Path:
        """长期记忆文件的实际路径。配置留空就用默认位置。

        和 `prompt_template_path()` 同样的语义：允许指向一个被外部管理
        （Git / Obsidian / 同步盘）的文件 —— 本机是双机环境，记忆要能跨机器走。
        """
        configured = (self.memory.file or "").strip()
        if configured:
            return Path(configured).expanduser()
        # 延迟导入：memory.py 依赖 config.py，模块级反向导入会成环。
        from .memory import MEMORY_NAME

        return app_data_dir() / MEMORY_NAME

    def conversations_dir(self) -> Path:
        """对话持久化目录（见 `store.py`）。"""
        from .store import CONVERSATIONS_DIR

        return app_data_dir() / CONVERSATIONS_DIR

    def read_prompt_template(self, seed: bool | None = None) -> str:
        """读初始提示词模板；文件不存在时（可选）播种一份内置默认。

        两条刻意的设计：
        ① **播种只发生在「文件不存在」时**。用户清空或改坏之后不会被悄悄覆盖回来 ——
           一个会在背后改回你写的东西的配置项，没人敢用。
        ② **读到空文件时退回默认**。空提示词会让模型完全不知道要输出工具围栏，
           表现是「它开始正常聊天、什么都不做」，而用户根本看不出是提示词被清空了。
        """
        path = self.prompt_template_path()
        if not path.exists():
            if seed is None:
                seed = self.prompt.seed_default
            if not seed:
                return DEFAULT_PROMPT_TEMPLATE
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(DEFAULT_PROMPT_TEMPLATE, encoding="utf-8")
            except OSError:
                return DEFAULT_PROMPT_TEMPLATE

        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return DEFAULT_PROMPT_TEMPLATE
        return text if text.strip() else DEFAULT_PROMPT_TEMPLATE
