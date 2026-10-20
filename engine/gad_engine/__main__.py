"""引擎入口 —— stdio JSON-RPC。

与 UI 之间用换行分隔的 JSON 消息（与 MCP 同构），便于单独测试引擎：
    echo '{"id":1,"method":"status"}' | python -m gad_engine

与 gemini 版的差别（**不是抄漏**）
----------------------------------

gemini 那边是**多账号 cookie**模型：`account.set` / `account.list` / `account.use` /
`account.remove` / `account.guest`，每个账号一个 `cookies-<label>.json`。

DeepSeek 这边是**单 token**：凭据存在 `localStorage.userToken`，取回来就一个串。
所以：

  * `account.*` → **`identity.*`**（本项目没有「多账号」这个概念，只有一个身份）
  * **没有 `account.guest`** —— DeepSeek 无访客模式（必须登录）
  * 新增 `identity.login.start/poll/finish/cancel/status` —— 浏览器取凭据的完整流程
  * 新增 `identity.refresh` —— 重新验证缓存 token（**验证**是本档位的必需品，见下）

★ 为什么身份层多了「验证」这一步
--------------------------------

DeepSeek 的 `401` **无法区分「token 过期」与「账号被处置」**（见 `circuit.py`）。
所以取到凭据之后必须用一次极轻的 `create_pow_challenge` 确认它真的能用 ——
否则用户会看到「登录成功」却在第一次对话时炸，而且**报错指向错误的方向**。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import platform
import shutil
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path
from typing import Any

from .agent import AgentRuntime
from .browser_login import BrowserLoginError, BrowserLoginSession
from .config import EngineConfig, default_config_path
from .identity import (
    clear_token,
    load_token,
    normalize_token,
    read_token_from_cdp_async,
    save_token,
    validate_token_async,
)
from .protocol import (
    DEFAULT_PROMPT_TEMPLATE,
    PROMPT_PLACEHOLDERS,
    missing_placeholders,
    render_prompt,
)
from .transport.deepseek_rpc import fetch_account_info_async, looks_suspended

PROTOCOL_VERSION = "1"


def _flag(value: Any) -> bool:
    """把 RPC 参数里的开关值归一成 bool。

    不能直接 `bool(value)`：界面侧可能把开关序列化成字符串发过来，
    而 `bool("false")` 是 **True** —— 那会让「关掉思考模式」变成「打开」，
    而且错得很安静。所以字符串要按内容判断。
    """
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


class EngineServer:
    def __init__(self) -> None:
        self.cfg = EngineConfig.load()
        self.runtime: AgentRuntime | None = None
        self._task: asyncio.Task | None = None
        self._out_lock = asyncio.Lock()
        self._login: BrowserLoginSession | None = None
        # 「刷新身份」正在跑。防重入：连点两次不该起两个无头浏览器抢同一个 profile。
        self._refreshing = False
        # 账号处置证据。**必须存在 Engine 侧** —— `_drop_runtime()` 会丢掉
        # AgentRuntime，而刷新验证正好走那条路；只存 Agent 上的话，
        # 验证完界面会退回「运行正常」。
        self._account_suspended = False
        self._suspended_detail = ""
        # `_emit_event` 派出去的写入任务。**必须留引用**：事件循环对任务的
        # 持有是弱引用，不留的话任务可能在跑完前被 GC 掉，表现为「偶尔丢事件」。
        # 这个集合兼作「还没写完的任务」，写完自己移出。
        self._emit_tasks: set[asyncio.Task] = set()

    # ---------- 输出 ----------

    async def _send(self, payload: dict) -> None:
        async with self._out_lock:
            text = json.dumps(payload, ensure_ascii=False)
            # 代理字符（U+D800–U+DFFF）无法编码成 UTF-8 —— 它们通常来自
            # 子进程输出的 `surrogateescape` 解码（Windows 上中文输出被
            # 按错误编码读回时就会出现）。不清理的话，**整条 RPC 结果写不出去**，
            # 表现为「run 报 UnicodeEncodeError」，把真正的结果埋掉。
            # 2026-10-17 实测：一次正常回复 + 工具输出就撞上了这个。
            text = text.encode("utf-8", "replace").decode("utf-8")
            sys.stdout.write(text + "\n")
            sys.stdout.flush()

    async def _emit(self, kind: str, data: dict) -> None:
        await self._send({"type": "event", "event": kind, "data": data})

    def _emit_event(self, kind: str, data: dict) -> None:
        """同步回调（由 AgentRuntime 在事件循环内调用）。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._emit(kind, data))
        # 留引用，跑完移除（见 `_emit_tasks` 的说明）。
        self._emit_tasks.add(task)
        task.add_done_callback(self._emit_tasks.discard)

    # ---------- 运行时 ----------

    async def _ensure_runtime(self) -> AgentRuntime:
        if self.runtime is None:
            self.runtime = AgentRuntime(self.cfg, on_event=self._emit_event)
            if self._account_suspended:
                self.runtime.mark_account_suspended(self._suspended_detail)
        return self.runtime

    def _mark_suspension(self, detail: str) -> None:
        """账号被服务端处置。Engine 与运行时各存一份（后者可能还没建）。"""
        self._account_suspended = True
        self._suspended_detail = detail
        if self.runtime is not None:
            self.runtime.mark_account_suspended(detail)

    def _clear_suspension(self) -> None:
        self._account_suspended = False
        self._suspended_detail = ""
        if self.runtime is not None:
            self.runtime.clear_account_suspension()

    async def _drop_runtime(self) -> None:
        """停掉并丢弃运行时 —— 身份变了必须重建传输层。"""
        if self.runtime is not None:
            with contextlib.suppress(Exception):
                await self.runtime.stop()
            self.runtime = None

    def _transport_catalog(self) -> list[dict]:
        """本机可选的传输档位，连同**此刻能不能用**。

        ## 为什么由引擎回答，而不是让界面写死一张表

        「能不能用」取决于三件引擎才知道的事：
          1. `gemini-webapi` 装没装（`requirements.txt` 里有了，但老 venv 可能没有）
          2. Gemini 的 cookie 文件在不在（路径规则在 `gemini_rpc.default_cookie_path`）
          3. 当前是访客模式还是登录态

        界面写死的话，这三项任何一项变化都要改 C# 与 JS 两处，而且必然漂移。
        引擎这边一行 `importlib.util.find_spec` 就能给出权威答案。

        ## `available` 与 `ready` 的区别

          * `available` —— 这个档位在**本机**能不能跑（依赖装没装）。
            为 False 时下拉框要置灰：选了也只会得到一条启动错误。
          * `ready` —— 装了，而且**当前配置**足以连上（cookie 文件存在 / 有 token）。

        **`ready=False` 不是「不能选」** —— 用户选它，然后去补凭据，正是
        正常流程。所以两个字段都要给，让界面能区分「置灰」与「选了但要提示」。
        """
        import importlib.util

        items: list[dict] = [
            {
                "name": "deepseek_rpc",
                "label": "DeepSeek 网页版",
                "available": True,
                # token 走惰性提供者，装配时读不到也不影响 —— 启动后
                # 若真没有，`start()` 会抛 auth 错，界面据此提示登录。
                "ready": True,
                "note": "默认档位。需要登录；无访客模式。",
            },
            {
                "name": "chrome",
                "label": "Chrome 真实内核",
                "available": True,
                "ready": True,
                "note": "headful Chrome 驱动，窗口可移出屏幕。",
            },
        ]

        has_lib = importlib.util.find_spec("gemini_webapi") is not None
        guest = bool(self.cfg.identity.guest_mode)
        cookie_file = ""
        if not guest:
            try:
                from .transport.gemini_rpc import default_cookie_path

                cookie_file = str(default_cookie_path(self.cfg.identity.account_label))
            except Exception:  # noqa: BLE001 - 缺依赖时连导入都可能失败
                cookie_file = ""
        cookie_ok = bool(cookie_file) and Path(cookie_file).exists()

        items.append(
            {
                "name": "gemini_rpc",
                "label": "Gemini 网页版",
                "available": has_lib,
                "ready": has_lib and (guest or cookie_ok),
                # **不要把 cookie 路径原样吐给前端** —— 路径里含账号邮箱
                # （`cookies-<label>.json` 的 label 就是账号邮箱），而前端日志会进
                # 用户可见的界面。只给「有没有」。
                "guest": guest,
                "hasCookie": cookie_ok,
                "note": (
                    "访客模式：不登录，零账号连坐风险，仅 gemini-flash-lite。"
                    if guest
                    else "需要 Gemini cookie（含 __Secure-1PSID 与 __Secure-1PSIDTS）。"
                ),
            }
        )
        return items

    def _identity_snapshot(self) -> dict:
        """当前身份状态。**不含 token 本体** —— 它不该出现在事件流里。"""
        path = self.cfg.resolved_token_file()
        token = load_token(path)
        # 处置标记以 **runtime 为准**：`run()` 撞上禁言时是 Agent 先知道，
        # 而 EngineServer 自己的缓存只在 `identity.refresh` / `identity.set`
        # 时更新。只看后者的话，发一条消息撞上禁言、再刷新状态，界面仍是
        # 「运行正常」—— 2026-10-17 实测正是如此（gate_reason 对了，
        # status.identity.account_suspended 却是 false）。
        suspended = self._account_suspended
        detail = self._suspended_detail
        if self.runtime is not None:
            try:
                state = self.runtime._background_state()
            except Exception:  # noqa: BLE001 - 状态查询不该让身份快照失败
                state = {}
            if state.get("account_suspended"):
                suspended = True
                detail = state.get("suspended_detail") or detail
        return {
            "token_file": str(path),
            "has_token": bool(token),
            "token_len": len(token),
            "account_label": self.cfg.identity.account_label,
            "impersonate": self.cfg.identity.browser_impersonate,
            "model": self.cfg.identity.model,
            "mode": self.cfg.identity.mode_label,
            "notices": self.cfg.identity.notices(),
            # 账号是否被服务端处置（封禁）。与 has_token 无关 ——
            # 被封的账号通常仍有 token，只看 has_token 会误报「已登录」。
            "account_suspended": suspended,
            "suspended_detail": detail,
            "violations": self.cfg.identity.violations(),
        }

    # ---------- 方法 ----------

    async def handle(self, msg: dict) -> dict:
        method = str(msg.get("method") or "")
        params = msg.get("params") or {}

        if method == "initialize":
            return {
                "protocol": PROTOCOL_VERSION,
                "transport": self.cfg.transport,
                "config_path": str(default_config_path()),
                "config": json.loads(json.dumps(self.cfg, default=lambda o: o.__dict__)),
                "identity": self._identity_snapshot(),
            }

        # ---------- 配置 ----------

        if method == "config.get":
            return {"config": json.loads(json.dumps(self.cfg, default=lambda o: o.__dict__))}

        if method == "config.set":
            merged = dict(json.loads(json.dumps(self.cfg, default=lambda o: o.__dict__)))
            for section, value in (params or {}).items():
                if isinstance(value, dict) and isinstance(merged.get(section), dict):
                    merged[section].update(value)
                else:
                    merged[section] = value
            prev_transport = (self.cfg.transport or "").strip().lower()
            self.cfg = EngineConfig.from_dict(merged)
            path = self.cfg.save()

            # **换档位必须清掉账号处置标记。**
            #
            # `_account_suspended` 记的是「这个账号被服务端处置了」。
            # 它是 EngineServer 级的，`_drop_runtime()` 故意不碰它
            # （见 __init__ 的注释：它要比 runtime 活得久）。
            #
            # 但档位一换，那句话的**主语就变了** —— DeepSeek 账号被封，
            # 与 Gemini 访客会话毫无关系。不清的话界面会一直显示
            # 「账号被处置」，而用户切到访客档后其实能正常说话。
            # 反过来（Gemini 撞 401 后切回 DeepSeek）也一样：
            # 旧的失败证据不该跟着新档位走。
            new_transport = (self.cfg.transport or "").strip().lower()
            if new_transport != prev_transport:
                self._clear_suspension()

            await self._drop_runtime()
            return {"saved": str(path)}

        if method == "config.path":
            return {"path": str(default_config_path())}

        if method == "config.transports":
            return {"items": self._transport_catalog()}

        # ---------- 初始提示词 ----------
        #
        # 提示词是**每轮现拼**的（`AgentRuntime._bootstrap_prompt`），所以改完
        # **下一轮就生效**，不需要重启引擎 —— 对「反复调提示词」这件事很关键。
        #
        # 模板里留 `{{protocol}}` 而不是让用户整段自由写：工具调用协议必须和
        # `protocol.py` 的解析器逐条对应，改坏了工具就整个失效，而且**失效方式很隐蔽**
        # （模型开始正常聊天、什么都不做）。所以协议由程序注入，用户只能决定它放哪。

        if method == "prompt.get":
            template = self.cfg.read_prompt_template()
            work_dir, shell_hint = "", "shell"
            if self.runtime is not None:
                snap = self.runtime.executor.snapshot()
                work_dir = snap.get("work_dir", "")
                shell_hint = Path(str(snap.get("shell", "") or "")).name or "shell"
            return {
                "path": str(self.cfg.prompt_template_path()),
                "template": template,
                "default_template": DEFAULT_PROMPT_TEMPLATE,
                "is_default": template.strip() == DEFAULT_PROMPT_TEMPLATE.strip(),
                "placeholders": list(PROMPT_PLACEHOLDERS),
                "unknown_placeholders": missing_placeholders(template),
                # 预览用和真实运行**同一套**渲染，免得「预览好看、实际不一样」。
                "preview": render_prompt(
                    template,
                    os_name=f"{platform.system()} {platform.release()}",
                    work_dir=work_dir or str(Path.home()),
                    shell_hint=shell_hint,
                    soul=self.cfg.read_soul(),
                    max_turns=self.cfg.max_tool_turns,
                ),
            }

        if method == "prompt.set":
            template = str(params.get("template") or "")
            if not template.strip():
                # 空模板会让模型完全不知道要输出工具围栏 —— 那种坏法用户看不出来。
                raise ValueError("提示词不能为空（清空会让模型不再输出工具调用）")
            path = self.cfg.prompt_template_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(template, encoding="utf-8")
            return {
                "saved": True,
                "path": str(path),
                "unknown_placeholders": missing_placeholders(template),
            }

        if method == "prompt.reset":
            path = self.cfg.prompt_template_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(DEFAULT_PROMPT_TEMPLATE, encoding="utf-8")
            return {
                "reset": True,
                "path": str(path),
                "template": DEFAULT_PROMPT_TEMPLATE,
                "unknown_placeholders": [],
            }

        # ---------- 模型 ----------
        #
        # 模型是**会话级**设置，与身份不同：改它只写配置、不重启运行时，
        # 因为它是 per-chat 参数，下一轮发送时自然生效。

        if method == "models.list":
            if self.runtime is None:
                return {"models": [], "current": self.cfg.identity.model}
            return {
                "models": self.runtime.available_models(),
                "current": self.cfg.identity.model,
            }

        if method == "model.set":
            name = str(params.get("name") or "")
            runtime = await self._ensure_runtime()
            try:
                current = await runtime.set_model(name)
            except Exception as exc:
                raise ValueError(str(exc)) from exc
            return {"current": current}

        # ---------- 权限 ----------
        #
        # 「只读 / 可写 / 可执行」三档 + dry-run。改完**不重启运行时** ——
        # 权限在 `_execute` 每次分发前实时读配置，没有缓存副本。
        if method == "permissions.set":
            runtime = await self._ensure_runtime()
            return await runtime.set_permissions(
                level=None if params.get("level") is None else str(params["level"]),
                dry_run=(
                    None if params.get("dry_run") is None
                    else _flag(params["dry_run"])
                ),
            )

        if method == "chat.set":
            runtime = await self._ensure_runtime()
            return await runtime.set_chat(
                temporary=None if params.get("temporary") is None else _flag(params["temporary"]),
                extended_thinking=(
                    None if params.get("extended_thinking") is None
                    else _flag(params["extended_thinking"])
                ),
                search_enabled=(
                    None if params.get("search_enabled") is None
                    else _flag(params["search_enabled"])
                ),
                show_thinking=(
                    None if params.get("show_thinking") is None
                    else _flag(params["show_thinking"])
                ),
            )

        # ---------- 对话与历史（持久化）----------
        #
        # 「多对话 / 保留历史 / 删除记录」的接口面。
        # 所有涉及**当前对话**的写操作都走 `runtime` —— 它是当前对话的唯一
        # 权威持有者（理由见 `agent.py` 里 store 的注释）。这里刻意不暴露
        # 「直接改文件」的口子，避免出现两个副本互相覆盖。

        if method == "conversation.list":
            runtime = await self._ensure_runtime()
            return {"items": runtime.list_conversations()}

        if method == "conversation.current":
            runtime = await self._ensure_runtime()
            return runtime.current_conversation()

        if method == "conversation.new":
            runtime = await self._ensure_runtime()
            return runtime.new_conversation()

        if method == "conversation.load":
            runtime = await self._ensure_runtime()
            conv = runtime.load_conversation(str(params.get("id") or ""))
            if conv is None:
                raise ValueError("对话不存在或已损坏")
            return conv

        # 从服务端拉该对话的历史（只读请求）。**与 load 分开**：
        # load 是零延迟的本地切换，sync 要一次网络往返 —— 合成一个会让
        # 切对话被网络卡住，而切对话本来就该是瞬时的。
        if method == "conversation.sync":
            runtime = await self._ensure_runtime()
            return await runtime.sync_conversation(str(params.get("id") or ""))

        # 接力换会话：把当前上下文压成摘要，开一条新会话继续。
        # **会联网**（`mode=model` 时要在当前会话里问一次摘要），所以外壳侧
        # 超时给得比本地那批宽。`mode` 可选（`model` / `local`），缺省用配置里的。
        if method == "conversation.handoff":
            runtime = await self._ensure_runtime()
            mode = params.get("mode")
            return await runtime.handoff_conversation(
                str(mode).strip().lower() if isinstance(mode, str) and mode.strip() else None
            )

        # ---------- 服务端会话（网页版历史）----------
        #
        # 这两条是**唯一会主动读账号历史**的接口面：
        #   * remote_list —— 列出账号下所有服务端会话（含网页版里建的），
        #                    并标出哪些已被本地接管
        #   * attach      —— 把其中一条接管成本地对话（拉全量历史 + 校正锚点）
        # 两者都要联网，所以超时给得比本地那批宽（外壳侧 60s）。

        if method == "conversation.remote_list":
            runtime = await self._ensure_runtime()
            return await runtime.list_remote_sessions(int(params.get("count") or 50))

        if method == "conversation.attach":
            runtime = await self._ensure_runtime()
            return await runtime.attach_session(str(params.get("remote_id") or ""))

        # 删除服务端会话（**不可逆**）。外壳侧必须先弹二次确认 —— 引擎不做交互，
        # 只做执行与解绑。返回 `{"deleted", "unbound", "local_id", "reason"}`。
        if method == "conversation.remote_delete":
            runtime = await self._ensure_runtime()
            return await runtime.delete_remote_session(
                str(params.get("remote_id") or "")
            )

        # ---------- 朗读（TTS）----------
        #
        # 引擎只做控制面：签票 + 音色。**音频流不走引擎** —— 前端拿到
        # ticket 后直连 `wss://chat.deepseek.com/api/v0/chat/tts/`，
        # 用 WebView2 自带的 WebCodecs 解 Opus。理由见
        # `transport/deepseek_rpc.py` 的 `TTS_TICKET_PATH` 注释。
        #
        # 三条都要联网，外壳侧超时按 remote 那批给（60s）。

        if method == "tts.ticket":
            runtime = await self._ensure_runtime()
            return await runtime.prepare_tts(str(params.get("message_id") or ""))

        if method == "tts.voices":
            runtime = await self._ensure_runtime()
            return await runtime.list_tts_voices()

        if method == "tts.voice":
            runtime = await self._ensure_runtime()
            return await runtime.set_tts_voice(str(params.get("voice_id") or ""))

        if method == "conversation.rename":
            runtime = await self._ensure_runtime()
            renamed = runtime.rename_conversation(
                str(params.get("id") or ""), str(params.get("title") or "")
            )
            if renamed is None:
                raise ValueError("对话不存在")
            return renamed

        if method == "conversation.delete":
            runtime = await self._ensure_runtime()
            return runtime.delete_conversation(str(params.get("id") or ""))

        # ---------- 长期记忆 ----------

        if method == "memory.status":
            runtime = await self._ensure_runtime()
            return runtime.memory_status()

        if method == "memory.get":
            runtime = await self._ensure_runtime()
            return {"text": runtime.read_memory(), "status": runtime.memory_status()}

        if method == "memory.set":
            runtime = await self._ensure_runtime()
            return runtime.write_memory(str(params.get("text") or ""))

        if method == "memory.append":
            runtime = await self._ensure_runtime()
            return runtime.append_memory(
                str(params.get("text") or ""), str(params.get("section") or "")
            )

        if method == "memory.clear":
            runtime = await self._ensure_runtime()
            return runtime.clear_memory()

        # ---------- 身份（L1）----------
        #
        # 与 gemini 的 `account.*` 对应，但语义是**单身份**。

        if method == "identity.status":
            snap = self._identity_snapshot()
            snap["login_active"] = self._login is not None
            return snap

        if method == "identity.set":
            # 手动粘贴兜底：用户在 DevTools 里复制 userToken 交过来。
            #
            # ★ 顺序是「归一 → 粗筛 → 验证 → 落盘」，**不能反**。
            # 原来用的是 save_manual_token()（它内部就落盘了），于是验证还没做
            # 就已经把原来那份可用的 token 覆盖掉了。存一个用不了的 token 比不存
            # 更糟：用户以为登录好了，直到第一次对话才炸，而且报错指向别处。
            raw = str(params.get("token") or "")
            token = normalize_token(raw)
            if not token:
                raise ValueError("粘贴内容里没有可识别的 token")
            if len(token) < 20:
                raise ValueError(f"token 形态异常（len={len(token)}）—— 请确认复制完整")
            ok, detail = await validate_token_async(token)
            if not ok:
                if looks_suspended(detail):
                    self._mark_suspension(detail)
                return {
                    "saved": "",
                    "token_len": len(token),
                    "valid": False,
                    "detail": detail,
                    "suspended": self._account_suspended,
                }
            self._clear_suspension()
            path = save_token(token, self.cfg.resolved_token_file())
            await self._drop_runtime()
            return {
                "saved": str(path),
                "token_len": len(token),
                "valid": True,
                "detail": detail,
            }

        if method == "identity.login.start":
            if self._login is not None:
                return {"already_running": True, "port": self._login.port}
            sess = BrowserLoginSession(
                profile_dir=Path(self.cfg.identity.chrome_profile_dir).expanduser()
                if self.cfg.identity.chrome_profile_dir
                else _default_profile_dir(),
                port=int(self.cfg.identity.debug_port or 9333),
                browser_path=self.cfg.identity.chrome_binary,
            )
            try:
                info = sess.start()
            except BrowserLoginError as exc:
                raise ValueError(str(exc)) from exc
            self._login = sess
            return {"started": True, **info}

        if method == "identity.login.poll":
            if self._login is None:
                return {"state": "no_session"}
            result = await self._login.poll()
            # **绝不把 token 本体放进事件流** —— 只回长度与状态。
            if "token" in result:
                result = dict(result)
                result["token_len"] = len(str(result.pop("token") or ""))
            return result

        if method == "identity.login.finish":
            if self._login is None:
                raise ValueError("没有进行中的登录会话")
            sess = self._login
            self._login = None
            try:
                result = await sess.poll()
                if result.get("state") != "ready":
                    return {
                        "ok": False,
                        "state": result.get("state"),
                        "detail": "还没有取到可用的登录态",
                    }
                token = normalize_token(result.get("token"))
                label = str(result.get("account_label") or "")
            finally:
                try:
                    await sess.close_browser()
                except Exception:  # noqa: BLE001
                    sess._kill()

            if not token:
                # 浏览器里没有登录态（未登录时 localStorage.userToken 是
                # {"value":null,...}）。明确说出来，别让用户对着一个
                # 「验证不通过」猜原因。
                return {
                    "ok": False,
                    "saved": "",
                    "token_len": 0,
                    "account_label": label,
                    "detail": "浏览器里没有有效的登录态 —— 请在打开的浏览器窗口里"
                              "登录 DeepSeek，然后再点一次。",
                }

            # ★ 先验后存。原来是「先 save_token 再 validate_token」—— 验证失败
            # 也已经把原来那份**可用的** token 覆盖掉了，用户从「能用」直接掉到
            # 「不能用」。这是真实发生过的数据损坏，不是理论风险。
            ok, detail = await validate_token_async(token)
            if not ok:
                return {
                    "ok": False,
                    "saved": "",
                    "token_len": len(token),
                    "account_label": label,
                    "detail": detail,
                    # 原来那份还在（没被覆盖）—— 前端可以据此安抚用户。
                    "kept_previous": bool(load_token(self.cfg.resolved_token_file())),
                }

            if not label:
                # 页面 DOM 里捞不到邮箱（侧边栏折叠 / 渲染未完成 / 账号是手机号注册）
                # 时直接问服务端 —— 它是唯一知道「这个 token 属于谁」的地方。
                # 顺带把禁言状态也带回来：不必为了显示状态而发一条真实对话。
                try:
                    info = await fetch_account_info_async(token)
                except Exception:  # noqa: BLE001 - best-effort，失败不阻断登录
                    info = {}
                label = str(info.get("email") or "")
                if info.get("is_muted"):
                    until = str(info.get("mute_until_text") or "")
                    self._mark_suspension(
                        "账号被处置（禁言）" + (f"：截止 {until}" if until else "")
                    )
                elif info:
                    self._clear_suspension()

            path = save_token(token, self.cfg.resolved_token_file())
            if label:
                self.cfg.identity.account_label = label
                self.cfg.save()
            await self._drop_runtime()
            return {
                "ok": True,
                "saved": str(path),
                "token_len": len(token),
                "account_label": label,
                "detail": detail,
            }

        if method == "identity.login.cancel":
            sess = self._login
            self._login = None
            if sess is None:
                return {"cancelled": False}
            try:
                await sess.close_browser()
            except Exception:  # noqa: BLE001
                sess._kill()
            return {"cancelled": True}

        if method == "identity.refresh":
            # 重新验证缓存 token；不行就顺带用 CDP 从已开的浏览器补一次
            # （不弹新窗口 —— 只连已经开着调试端口的那个）。
            #
            # `probe_only`：只验证缓存，失败就返回，不去碰 CDP。启动自检走这条 ——
            # 启动路径上不该因为浏览器没开而多等一轮连接超时。
            probe_only = _flag(params.get("probe_only"))
            port = int(self.cfg.identity.debug_port or 9333)
            cache_detail = "没有缓存 token"
            token = load_token(self.cfg.resolved_token_file())
            if token:
                # **一次 `users/current` 同时解决三件事**：验 token、取账号名、
                # 读禁言状态。原来这里先 `create_pow_challenge` 再读账号信息 ——
                # 启动一次要发两个带 impersonate 的请求，对风控不友好也没必要
                # （2026-10-17 封号事故的教训：请求越像「自动化探测」越危险）。
                #
                # 只有它失败时才回头用 `create_pow_challenge` 判原因 ——
                # 正常启动永远只发这一个请求。
                info: dict = {}
                probe_error = ""
                try:
                    info = await fetch_account_info_async(token)
                except Exception as exc:  # noqa: BLE001 - 失败要能降级，不能炸
                    probe_error = str(exc)

                if info:
                    email = str(info.get("email") or "")
                    if email and email != self.cfg.identity.account_label:
                        self.cfg.identity.account_label = email
                        self.cfg.save()
                    if info.get("is_muted"):
                        until = str(info.get("mute_until_text") or "")
                        self._mark_suspension(
                            "账号被处置（禁言）" + (f"：截止 {until}" if until else "")
                        )
                    else:
                        # 服务端明确说没被禁言 —— 只有这时才允许清标记。
                        self._clear_suspension()
                    # 启动自检只是「看一眼」：token 没变，不该重建运行时。
                    # 重建会让紧随其后的 status 返回 started=false，
                    # 界面从「运行中」掉成「引擎启动中…」。
                    if not probe_only:
                        await self._drop_runtime()
                    return {
                        "ok": True,
                        "source": "cache",
                        "detail": "users/current 200",
                        "account_label": self.cfg.identity.account_label,
                        "suspended": self._account_suspended,
                    }

                # `users/current` 没成功（401 / 网络 / 返回异常）——
                # 用原来的探测判「凭据过期」还是「账号被处置」。
                ok, cache_detail = await validate_token_async(token)
                if ok:
                    # 探测通过但读不到账号信息：凭据是好的，只是这一跳没成。
                    # 不要把账号判成坏的 —— 界面显示「运行中」比「凭据失效」更接近事实。
                    if not probe_only:
                        await self._drop_runtime()
                    return {
                        "ok": True,
                        "source": "cache",
                        "detail": f"{cache_detail}（账号信息读取失败：{probe_error}）",
                        "account_label": self.cfg.identity.account_label,
                        "suspended": self._account_suspended,
                    }
                # 验证失败要分清「凭据过期」还是「账号被处置」——
                # 后者重登多少次都没用，界面必须直说，并把原文带上。
                if looks_suspended(cache_detail):
                    self._mark_suspension(cache_detail)
            if probe_only:
                return {
                    "ok": False,
                    "source": "cache",
                    "detail": cache_detail,
                    "suspended": self._account_suspended,
                }
            # 缓存不行 → 试试已开的调试端口（不弹新窗口）
            try:
                fresh = await read_token_from_cdp_async(port)
            except BrowserLoginError as exc:
                # 两条失败原因分开说。合成一句「没有可用的调试端口」会误导 ——
                # 端口可能是好的，只是那个浏览器里根本没登录（这正是
                # 「怎么修」的关键信息：去那个窗口里登录，而不是去查端口）。
                return {
                    "ok": False,
                    "source": "cache",
                    "detail": f"缓存 token 不可用（{cache_detail}）；"
                              f"调试端口 {port} 也没能补上：{exc}",
                    "suspended": self._account_suspended,
                }
            ok, detail = await validate_token_async(fresh)
            if ok:
                self._clear_suspension()
                save_token(fresh, self.cfg.resolved_token_file())
                await self._drop_runtime()
            elif looks_suspended(detail):
                self._mark_suspension(detail)
            return {
                "ok": ok,
                "source": "cdp",
                "detail": detail,
                "suspended": self._account_suspended,
            }

        if method == "identity.clear":
            removed = clear_token(self.cfg.resolved_token_file())
            await self._drop_runtime()
            return {"removed": removed}

        if method == "identity.logout":
            # **「换账号」≠「清 token」。**
            #
            # 浏览器登录用的是持久化 profile（默认
            # `%LOCALAPPDATA%\DeepSeek-Agent\chrome-profile`），里面存着上一个
            # 账号在 chat.deepseek.com 的 cookie。只删 `.token` 的话，用户再点
            # 「打开浏览器登录」，Chrome 带着旧 cookie 起来 —— 直接就是原账号，
            # 用户根本没有换号的机会，而界面上看不出任何异常。
            #
            # 所以退出登录必须把专用 profile 一起清掉。它是本程序独占的目录，
            # 删掉后下一次登录会重新建（`BrowserLoginSession.start` 里有 mkdir）。
            removed = clear_token(self.cfg.resolved_token_file())
            await self._drop_runtime()

            # 账号展示名也要清 —— 它是配置里手填的字段，不会随 token 消失。
            # 不清的话，换号之后界面仍然写着「账号模式 · shaomo17」，
            # 用户会以为没退成功（2026-10-17 实测：清 token 后 status.mode 仍是旧账号）。
            if self.cfg.identity.account_label:
                self.cfg.identity.account_label = ""
                self.cfg.save()

            profile = (
                Path(self.cfg.identity.chrome_profile_dir).expanduser()
                if self.cfg.identity.chrome_profile_dir
                else _default_profile_dir()
            )
            removed_profile = False
            cleared_login_state = False
            if profile.exists():
                removed_profile, failed = _clear_chrome_login_state(profile)
                # 整目录删掉 = 登录态必然清掉；降级路径则要求没有失败项。
                cleared_login_state = removed_profile or not failed
                if failed:
                    raise ValueError(
                        "浏览器登录态没清干净：" + "；".join(failed[:3])
                        + "。请先关掉那个专用 Chrome 窗口再试一次。"
                    )
            return {
                "removed_token": removed,
                "removed_profile": removed_profile,
                "cleared_login_state": cleared_login_state,
                "profile": str(profile),
            }

        # ---------- 运行时 ----------

        if method == "start":
            runtime = await self._ensure_runtime()
            await runtime.start()
            return runtime.status()

        if method == "status":
            if self.runtime is None:
                return {
                    "started": False,
                    "identity": self._identity_snapshot(),
                    "transport": {"name": self.cfg.transport, "ready": False},
                }
            snap = self.runtime.status()
            snap["started"] = True
            snap["identity"] = self._identity_snapshot()
            return snap

        if method == "run":
            runtime = await self._ensure_runtime()
            _f = params.get("files")
            report = await runtime.run(
                str(params.get("goal") or ""),
                max_turns=params.get("max_turns"),
                files=([str(x) for x in _f if str(x).strip()] if isinstance(_f, list) else None),
            )
            return report.to_dict()

        if method == "run.async":
            goal = str(params.get("goal") or "")
            max_turns = params.get("max_turns")
            # 用户手动挂的附件（绝对路径列表）。界面上选的文件经这条路进来，
            # 与 agent 自己用 `attach` 工具产出的附件汇到同一个出口。
            # 只做类型兜底 —— 路径是否真存在由传输层在上传前判定，
            # 那里能给出更准确的错误（「不存在」vs「是目录」vs「空文件」）。
            raw_files = params.get("files")
            files = (
                [str(f) for f in raw_files if str(f).strip()]
                if isinstance(raw_files, list)
                else None
            )

            async def _job() -> None:
                try:
                    runtime = await self._ensure_runtime()
                    report = await runtime.run(goal, max_turns=max_turns, files=files)
                    await self._emit("run.completed", report.to_dict())
                except asyncio.CancelledError:
                    # ★ 硬取消也必须让界面收到结束事件（2026-10-18，T-19）。
                    #
                    # 原实现是裸 `raise` —— 任务被 `cancel()` 后界面**什么都收不到**，
                    # 就永远停在「运行中」这个假状态，直到用户手动刷新。
                    # 这是「静默失效」的典型形态：看起来还在跑，其实早停了。
                    #
                    # `run.cancel` 里已经先试过优雅停止；走到这里说明宽限期内
                    # 没停住（卡在 PoW / 上传），已生成的正文拿不回来了 ——
                    # 但至少要让界面知道结束了、以及**为什么**。
                    await self._emit(
                        "run.failed",
                        {"error": "cancelled", "cancelled": True,
                         "detail": "已停止（未能在宽限期内优雅退出，本轮已生成内容未保留）"},
                    )
                    raise
                except BaseException as exc:  # noqa: BLE001
                    # 必须捕 `BaseException`：后台任务漏出任何东西，界面就会
                    # 永远停在「运行中」这个假状态。
                    await self._emit(
                        "run.failed", {"error": f"{type(exc).__name__}: {exc}"}
                    )

            self._task = asyncio.create_task(_job())
            return {"started": True}

        if method == "run.cancel":
            # ★ 两级停止（2026-10-18，T-19）。
            #
            # **先请传输层优雅停**，再考虑硬取消。原实现只有 `task.cancel()`，
            # 而 `asyncio` 的取消**中断不了**正在 `resp.iter_lines()` 里阻塞的
            # 线程（`curl_cffi` 是同步 API，跑在 `run_in_executor` 里）。
            # 后果：界面显示「已停止」，但引擎仍在读流、仍在计费、
            # 仍在往对话里塞字 —— 最糟的一种「假停止」。
            #
            # 优雅停止的收益不只是「真的停了」：已生成的正文会**保留**在
            # `outcome.text` 里，`stop_reason` 是 `cancelled` 而不是
            # `transport_error`，熔断计数不受影响。硬取消做不到任何一条。
            graceful = False
            if self.runtime is not None and self._task is not None and not self._task.done():
                try:
                    graceful = bool(self.runtime.transport.request_stop())
                except Exception:  # noqa: BLE001 - 停止路径绝不能再抛错
                    graceful = False

            if graceful:
                # 给读循环一点时间在下一行 SSE 处退出。
                #
                # 上限取 2.5 秒：上游吐字的间隔实测是几十毫秒，长思考模式下
                # 首字可能要等很久 —— 但**那种情况本来就没内容可保**，
                # 2.5 秒之后硬取消与优雅停止的结果等价。
                #
                # 用 `asyncio.wait` 而不是 `await self._task`：后者会把任务的
                # 异常重新抛到这里，而停止路径不该关心任务是正常结束还是抛错。
                with contextlib.suppress(Exception):
                    await asyncio.wait({self._task}, timeout=2.5)

            if self._task is not None and not self._task.done():
                # 兜底：优雅停止没在宽限期内生效（例如卡在 PoW 求解、
                # 或卡在上传附件 —— 这两处都在 `send()` 内部且不读 SSE）。
                self._task.cancel()
            self._task = None
            return {"cancelled": True, "graceful": graceful}

        if method == "stop":
            await self._drop_runtime()
            return {"stopped": True}

        if method == "breaker.reset":
            if self.runtime is not None:
                self.runtime.breaker.reset()
                return self.runtime.breaker.snapshot()
            return {"level": "closed", "reason": "未运行"}

        if method == "shutdown":
            if self._login is not None:
                try:
                    await self._login.close_browser()
                except Exception:  # noqa: BLE001
                    self._login._kill()
                self._login = None
            await self._drop_runtime()
            return {"__shutdown__": True}

        raise ValueError(f"未知方法：{method!r}")

    # ---------- 主循环 ----------

    async def serve(self) -> int:
        """读取循环。

        用「阻塞读线程 + asyncio 队列」而不是 `loop.connect_read_pipe`：
        后者在 Windows Proactor 事件循环下接管 stdin 管道会抛
        `OSError [WinError 6] The handle is invalid`，且 EOF 时直接崩栈。
        读线程方案跨平台一致。
        """
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[str | None] = asyncio.Queue()

        def reader() -> None:
            try:
                for raw in sys.stdin:
                    loop.call_soon_threadsafe(queue.put_nowait, raw)
            except (OSError, ValueError):
                pass
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        threading.Thread(target=reader, name="gad-stdin", daemon=True).start()

        while True:
            raw = await queue.get()
            if raw is None:
                break
            text = raw.strip()
            if not text:
                continue
            try:
                msg = json.loads(text)
            except ValueError as exc:
                await self._send({"type": "error", "error": f"JSON 解析失败：{exc}"})
                continue

            msg_id = msg.get("id")
            try:
                result = await self.handle(msg)
            except Exception as exc:  # noqa: BLE001 - 引擎不得因单条消息崩溃
                await self._send(
                    {
                        "id": msg_id,
                        "type": "error",
                        "error": str(exc),
                        "trace": traceback.format_exc(limit=3),
                    }
                )
                continue

            shutdown = bool(result.pop("__shutdown__", False))
            await self._send({"id": msg_id, "type": "result", "result": result})
            if shutdown:
                break

        return 0


def _default_profile_dir() -> Path:
    from .browser_login import default_profile_dir

    return default_profile_dir()


#: 专用 Chrome profile 里与「登录态」直接相关的路径。
#:
#: 换账号只需要清这些。整目录删不掉时（`BrowserMetrics/*.pma` 被 Chrome 的
#: crashpad 句柄占着，实测会抛 `WinError 5`）就降级成清这些 ——
#: 而不是报「清不掉」把用户堵死在原地。
_LOGIN_STATE_PATHS = (
    "Default/Cookies",
    "Default/Cookies-journal",
    "Default/Local Storage",
    "Default/Session Storage",
    "Default/IndexedDB",
    "Default/Preferences",
    "Default/Secure Preferences",
    "Default/Login Data",
    "Default/Web Data",
    "Default/History",
)


def _clear_chrome_login_state(profile: Path) -> tuple[bool, list[str]]:
    """清掉专用 profile 的登录态。

    返回 `(整目录是否已删除, 失败项)`。整目录能删就整删（最干净）；
    删不掉时降级为逐个清 `_LOGIN_STATE_PATHS`，失败项原样返回给调用方。
    """
    try:
        shutil.rmtree(profile)
        return True, []
    except OSError:
        pass

    failed: list[str] = []
    for rel in _LOGIN_STATE_PATHS:
        target = profile / rel
        if not target.exists():
            continue
        try:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        except OSError as exc:
            failed.append(f"{rel}（{exc}）")
    return False, failed


def run_powcheck(*, full: bool = False) -> int:
    """PoW 本地开销门禁 —— **全程零上游请求**。

    为什么这道门禁可以完全离线：PoW 求解只需要一个 `Challenge` 对象，而
    challenge 的每个字段（`salt` / `expire_at` / `difficulty` / `challenge` 哈希）
    都能自己构造 —— 求解过程本身**不碰网络**。对一个「连发会撞服务端软限流」
    的项目来说，能离线跑的判据是硬性优点：随时可跑、不消耗账号。

    判据分两类，**刻意分开**：
      * **正确性** —— 用 C# 侧的金标准向量硬判定，失败即红。
      * **耗时** —— 只报告 + 设**宽松**上限，避免慢机器误红。
        唯一一条硬性耗时判据是「多进程不得等满启动超时」，因为那是
        `wait()` 漏掉 `return_when=FIRST_COMPLETED` 的特征（见 `pow.py`）。

    报告落 `%TEMP%\\gad-powcheck.txt`（与外壳的 `gad-*.txt` 同一套约定），
    退出码 0/1。`--full` 会额外跑 `selftest()` 的线上真实向量（较慢，约半分钟）。
    """
    from . import pow as powmod

    lines: list[str] = []
    ok = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok
        if not cond:
            ok = False
        # `detail` **只在失败时**打出来 —— 它是「为什么红」的解释
        # （例如「疑似 wait() 退回了 ALL_COMPLETED」），挂在绿行上会读成失败。
        lines.append(f"{'OK  ' if cond else 'FAIL'} {name}" + (f" —— {detail}" if detail and not cond else ""))

    lines.append("PoW 本地开销门禁（零上游请求）")
    lines.append(f"  python {sys.version.split()[0]} · cpu {os.cpu_count()} · cwd {Path.cwd()}")

    # ---------- 1. 正确性：哈希金标准向量 ----------
    want = "a77d69cad11ee53b776e0b5fb61ba36074e67eaaa1b6644ef5006deb6be55453"
    got = powmod.ds_hash_hex(b"3c1b67cba98a8235c00d_1788633061248_0")
    check("哈希向量吻合（C# 侧金标准）", got == want, f"期望 {want} 实得 {got}")

    # ---------- 2/3. 求解：答案固定在 nonce=5000（跨分片） ----------
    #
    # 为什么不把答案放在 0：答案在分片 0 时多进程会立刻返回，**测不到**
    # 「搜索真的会往后走」。5000 落在分片 1（chunk_size=4096），
    # 既便宜（几千次哈希）又能覆盖跨分片这条路径。
    answer_at = 5000
    salt, expire, difficulty = "powcheck", 1789148689858, 144000
    ch = powmod.Challenge(
        algorithm="DeepSeekHashV1",
        challenge=powmod.ds_hash_hex(f"{salt}_{expire}_{answer_at}".encode()),
        salt=salt,
        difficulty=difficulty,
        signature="",
        expire_at=expire,
    )

    t0 = time.monotonic()
    single = powmod.solve(ch, "/api/v0/chat/completion", workers=1)
    single_ms = (time.monotonic() - t0) * 1000.0
    check("单进程解出正确答案", single["answer"] == answer_at, f"实得 {single['answer']}")
    lines.append(f"     单进程 {single_ms:7.0f} ms · mode={single['_mode']}")

    t0 = time.monotonic()
    multi = powmod.solve(ch, "/api/v0/chat/completion")
    multi_ms = (time.monotonic() - t0) * 1000.0
    check("多进程解出正确答案", multi["answer"] == answer_at, f"实得 {multi['answer']}")
    lines.append(
        f"     多进程 {multi_ms:7.0f} ms · mode={multi['_mode']}×{multi['_workers']}"
        f" · 首片返回 {multi.get('_pool_startup_ms', 0.0):.0f} ms"
        f" · 池{'新建' if multi.get('_pool_created') else '复用'}"
    )

    # 唯一的硬性耗时判据 —— `wait()` 退回 `ALL_COMPLETED` 的特征是
    # **等满 `startup_timeout`（10 秒）**，与答案位置无关。2026-10-16 实测过。
    # 阈值取 6000ms：正常路径约 0.4~2 秒，留有 3 倍余量。
    check(
        "多进程没有等满启动超时（FIRST_COMPLETED 回归守护）",
        multi_ms < 6000.0,
        f"耗时 {multi_ms:.0f}ms —— 疑似 pow.py 的 wait() 退回了 ALL_COMPLETED",
    )

    # ---------- 4. 基准（只报告，不设硬判据） ----------
    n = 3000
    bench_prefix = f"bench_{expire}_".encode()
    t0 = time.monotonic()
    powmod._search_chunk((0, n, bench_prefix, bytes(32)))  # 目标永不命中 = 纯吞吐
    per_hash = (time.monotonic() - t0) * 1000.0 / n
    lines.append(
        f"     单核 {per_hash:.3f} ms/hash ⇒ difficulty={difficulty} 时"
        f"单核最坏 {difficulty * per_hash / 1000:.0f}s、期望 {difficulty / 2 * per_hash / 1000:.0f}s"
    )

    # ---------- 5. 可选：线上真实向量（慢） ----------
    if full:
        t0 = time.monotonic()
        sok, smsg = powmod.selftest()
        check(f"selftest（线上真实向量 80804）：{smsg}", sok)
        lines.append(f"     耗时 {time.monotonic() - t0:.1f}s")
    else:
        lines.append("     （未跑线上真实向量；加 `--full` 可跑，约半分钟）")

    lines.append("")
    lines.append("== PoW 检查通过 ==" if ok else "== PoW 检查未通过 ==")

    text = "\n".join(lines)
    try:
        report = Path(tempfile.gettempdir()) / "gad-powcheck.txt"
        report.write_text(text, encoding="utf-8")
    except OSError:
        pass
    if hasattr(sys.stdout, "reconfigure"):
        with contextlib.suppress(Exception):
            sys.stdout.reconfigure(encoding="utf-8")
    print(text)
    return 0 if ok else 1


def main() -> int:
    # 门禁：PoW 本地开销（**零上游请求**，见 `run_powcheck` 的说明）。
    # 放在最前面 —— 它不需要起 RPC 服务，也不该被任何启动逻辑拖慢。
    if "--powcheck" in sys.argv:
        return run_powcheck(full="--full" in sys.argv)

    # 诊断开关：设了 `GAD_DUMP_STACKS=<秒>` 就每隔那么久把所有线程的栈打到
    # stderr。排查「RPC 不返回、但事件循环还活着」这类问题时，这是**唯一**
    # 能看到 worker 线程卡在哪的手段 —— 引擎跑在子进程里，没有交互式调试器
    # 可用，而 `run_in_executor` 里的线程出错时既不冒泡也不打日志。
    interval = os.environ.get("GAD_DUMP_STACKS")
    if interval:
        try:
            import faulthandler

            faulthandler.enable()
            faulthandler.dump_traceback_later(float(interval), repeat=True)
        except Exception:  # noqa: BLE001 - 诊断开关本身不得影响启动
            pass

    # RPC 是 stdin/stdout 两条管道，**两端都必须显式 UTF-8**。
    #
    # 只设 stdout 是不够的：Windows 的默认编码是 cp936，引擎会把外壳发来的
    # UTF-8 中文按 GBK 解码 —— goal 直接变成乱码。2026-10-17 实测：
    # `只回复两个字：收到` 读进来成了 `??\ufffd???\ufffd??\ufffd\u05d6\ufffd...`，
    # 模型收到的就是这串东西，用户只会看到「它答非所问」。
    #
    # 外壳（.NET `StreamReader`）默认就是 UTF-8，所以这里显式对齐不会冲突。
    for _stream in (sys.stdin, sys.stdout):
        if hasattr(_stream, "reconfigure"):
            with contextlib.suppress(Exception):
                _stream.reconfigure(encoding="utf-8")

    server = EngineServer()
    try:
        return asyncio.run(server.serve())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
