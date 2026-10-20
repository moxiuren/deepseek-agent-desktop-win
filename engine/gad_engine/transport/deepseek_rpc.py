"""T3 —— DeepSeek 网页版 RPC 直连传输层。

**本方案的核心实现**。不跑网页版、不注入 DOM、不开浏览器（取凭据除外），
直接对 `chat.deepseek.com` 的私有接口发请求。

链路（2026-10-14 23:51 实测走通）
--------------------------------

```
POST /api/v0/chat_session/create      body {}                  → session_id
POST /api/v0/chat/create_pow_challenge {"target_path": <目标>}  → challenge
     ↓ 解 PoW（Keccak-f[1600] round 1..23，多进程）
POST /api/v0/chat/completion          带 x-ds-pow-response      → SSE 流
```

与原版（`deepseek-mac/windows/DeepSeekApiClient.cs`）的关键差异
-----------------------------------------------------------

| 项 | 原版 | 本实现 |
|---|---|---|
| TLS 指纹 | .NET Schannel（裸奔） | `curl_cffi` `impersonate="chrome150"` |
| `user-agent` | 写死 `Chrome/133.0.0.0` | 与 impersonate 版本**联动**，不写死 |
| `x-client-locale` | 写死 `zh_CN` | `en_US`（实测真实值） |
| 创建会话 | 从页面嗅探 | 显式调 `chat_session/create` |
| `thinking`/`search` | 恒 `true` | 由配置决定（实测真实为按需 `false`） |
| 解析 | 页面 HTML 反推 | 直读 SSE 文本流 |

**附件上传（2026-10-18 接通，同日真链路验证通过）**

`send(files=[...])` 会先逐个调 `POST /api/v0/file/upload_file`（multipart，
字段名 `file`），拿到 `biz_data.id` 后填进 `completion` 的 `ref_file_ids`。
**上传有自己的 PoW**，`target_path` 与发送那份不同、不能互换 —— 详见
`_upload_file` 与 `docs/20261018-文件上传交接.md`。

真链路实测结论（`tools/verify_upload_live.py` + `tools/verify_upload_edge.py`，
2026-10-18，账号 `mo*****xx@example.com`）：

| 问题 | 实测答案 |
|---|---|
| 上传 PoW difficulty | **144000**，与 completion 完全相同 |
| 未解析完能否直接发 | **能**。上传立刻返回 `status=PENDING`，直接挂进 `ref_file_ids` 发出，模型照样读到内容（暗号测试命中） |
| `FileParseStatus` 取值 | 至少见过 `PENDING`。**不需要轮询** —— 服务端在 completion 里同步等解析完 |
| referer | **不参与校验**。根路径 `/` 与会话 URL `/a/chat/s/<cid>` 都接受 |
| `x-file-size` 单位 | 字节（回显的 `file_size` 与本地 `stat().st_size` 一致） |
| 多文件 | 支持，`ref_file_ids` 按 `files` 顺序对应；两个文件的暗号同时命中 |
| 二进制 | 能上传（返回 id + PENDING），且服务端**确实提取了**（PNG 被识别为"白色空白图片"） |
| 空文件 / 不存在 | 客户端在发请求前就拦下（`_upload_file` 里，省掉白烧的 PoW） |

**待办（已知未实现，勿当成已完成）**
  * `temporary`（临时对话不落历史）—— DeepSeek 侧的接口未知，当前一律落历史
  * 上传**仍不做** `fetch_files` 轮询 —— 已确认不必，但若服务端某天改成异步解析
    再拒发，从这里接（路径与参数见 `FETCH_FILES_PATH`）。
  * 思考通道的 SSE 路径**未经实测**（未抓到 thinking 开启的真实流），
    按「路径含 `thinking`」启发式分流 —— 见 `_SseParser._delta`
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import inspect
import json
import os
import re
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import observability as obs
from ..pow import Challenge, solve
from ..session import SessionMeta
from .base import (
    DeltaCallback,
    ModelInfo,
    RemoteStatus,
    ThoughtCallback,
    Transport,
    TransportError,
    TurnOutcome,
)

BASE = "https://chat.deepseek.com"
CHALLENGE_PATH = "/api/v0/chat/create_pow_challenge"
COMPLETION_PATH = "/api/v0/chat/completion"
#: 附件上传（2026-10-18 逆向自 `main.a0ce88b684.js`，见
#: `docs/20261018-文件上传交接.md`）。
#:
#: **它有自己的 PoW** —— `target_path` 参与签名，与 `completion` 的那份
#: 不能互换。忘了这条的表现是 HTTP 403，而不是任何可读的业务错误。
UPLOAD_PATH = "/api/v0/file/upload_file"
#: 上传后查解析进度。第一版**不用它**（见 §2.10「状态枚举未查清」），
#: 但保留常量：一旦服务端拒绝未解析完的文件，轮询就从这里接。
FETCH_FILES_PATH = "/api/v0/file/fetch_files"

#: 附件条数与单文件大小的**客户端兜底值**（交接文档 §2.13）。
#:
#: 真实限额来自 `model_configs` 的 `file_feature.max_input_file_count` /
#: `max_upload_file_size`，**可能比这里小** —— 我们没接那个接口，所以这
#: 两个数只用来「提前拦掉明显超限的」，不作为权威判据。真值更小时由服务端
#: 拒绝，错误照样能透出来。
UPLOAD_MAX_COUNT = 50
UPLOAD_MAX_BYTES = 100 * 1024 * 1024
#: 服务端把「鉴权失败」报成 **HTTP 200 + 这个业务码**，而不是 401/403。
#: body 形如 `{"code":40003,"msg":"Authorization Failed (invalid token)","data":null}`。
#: 任何「看状态码判鉴权」的代码在 DeepSeek 上都会失效 —— 见 `_probe_once`。
AUTH_FAILED_BIZ_CODE = 40003

#: 账号被处置（封禁 / 停用）的响应特征。DeepSeek 不提供状态接口，
#: 只能从业务错误原文里识别。原文实测有两种形态：
#:
#: 1. **封禁**（2026-10-17 之前的记录）：
#:    `Due to violation of user policies, your account has been suspended
#:     until October 20, 2026 00:53.`
#: 2. **禁言**（2026-10-17 10:10 实测，`chat/completion` 直接回非 SSE JSON）：
#:    `{"code":0,"msg":"","data":{"biz_code":5,"biz_msg":"user is muted",
#:      "biz_data":{"is_muted":1,"mute_until":1792428836.75}}}`
#:
#: 第 2 种是**本轮踩到的坑**：`biz_msg` 里既没有 suspended 也没有
#: violation，只写 `user is muted`。之前只认第 1 种，于是封禁期界面
#: 一切正常、发消息却永远「流为空」——用户只能得出「软件坏了」这个错误结论。
#: **必须与「token 过期」分开** —— 否则界面会一直提示「重新登录」，
#: 用户反复登录也永远好不了。
SUSPENDED_MARKERS = (
    "suspended",
    "violation of user policies",
    "account has been suspended",
    "muted",
    "禁言",
    "账号已被封禁",
    "账号被处置",
)


def looks_suspended(text: str) -> bool:
    """响应原文是否在说「账号被处置」，而不是「凭据过期」。"""
    low = (text or "").lower()
    return any(m.lower() in low for m in SUSPENDED_MARKERS)


def mute_until_text(biz_data: Any) -> str:
    """把 `biz_data.mute_until`（Unix 秒）转成本地可读时间。

    没有这个字段、或值不合法时返回空串 —— 界面上少一行截止时间，
    总好过显示一个 `1792428836` 让用户自己去算。
    """
    if not isinstance(biz_data, dict):
        return ""
    ts = biz_data.get("mute_until")
    if not isinstance(ts, (int, float)) or ts <= 0:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
    except (OSError, ValueError, OverflowError):
        return ""


def _audit_error_tag(err: object) -> str:
    """把异常 / 错误文本压成**可 grep 的短标签**，写进审计的 `error` 字段。

    **为什么不在审计里存完整原文**：完整异常链（含上游返回的 JSON、堆栈）
    在引擎日志里有；审计是给「按天/按小时统计失败类型」用的，
    每行几十字节才扫得动。这里只回答「哪一类」。

    分类顺序与 `circuit.classify_error_text` 保持一致的优先级 ——
    否则同一件事在审计里叫一个名字、在熔断里叫另一个，复盘时对不上。
    """
    if isinstance(err, BaseException):
        # 传输层异常：优先用结构化的状态码与业务码，比解析文本可靠。
        status = int(getattr(err, "status", 0) or 0)
        if status:
            return f"http_{status}"
        if getattr(err, "suspended", False):
            return "suspended"
        text = str(err)
    else:
        text = str(err or "")

    low = text.lower()
    if "suspended" in low or "muted" in low or "禁言" in text or "封禁" in text:
        return "suspended"
    if "biz_code=" in text:
        # 只取码值 —— 例如 `biz_code=5` / `biz_code=40003`。
        # **必须按分隔符截，不能按固定长度截**：`biz_code=` 本身占 9 个字符，
        # 固定截 12 个只剩 3 位给码值，`biz_code=40003` 会被截成 `biz_code=4000`
        # —— 而 40003 恰好是「账号被处置」的业务码，截断后分类直接错档。
        idx = text.find("biz_code=")
        tail = text[idx:]
        for sep in ("：", "，", ",", "；", ";", "）", ")", " ", "\n"):
            tail = tail.split(sep)[0]
        return tail.strip()
    if "429" in text or "限流" in text:
        return "http_429"
    if "未认证" in text or "401" in text or "403" in text:
        return "http_401"
    if "Server busy" in text or "generation_timeout" in text:
        return "server_busy"
    if "业务错误" in text:
        return "biz_error"
    # 兜底：异常类名 + 前 40 字。不写完整原文，保持审计可扫。
    name = type(err).__name__ if isinstance(err, BaseException) else "error"
    return f"{name}:{text[:40]}".strip(":")


def describe_biz_failure(d: dict) -> tuple[bool, str]:
    """把 `data.biz_code` 业务失败翻译成 (是否账号处置, 可读细节)。

    两个调用点（SSE 解析器 / 普通 JSON 响应）共用 —— 否则一处补了
    `is_muted`、另一处没补，同一个账号在两条路径上会给出不同结论。
    """
    msg = str(d.get("biz_msg") or "")
    biz_data = d.get("biz_data")
    is_muted = isinstance(biz_data, dict) and bool(biz_data.get("is_muted"))
    suspended = looks_suspended(msg) or is_muted
    until = mute_until_text(biz_data)
    detail = f"{msg}（截止 {until}）" if until else msg
    return suspended, detail

#: 真实鉴权探测的放行开关。**默认拒绝**。
#:
#: ★ 2026-10-17 事故（必须留着这段，别再犯）：
#:   账号 `shaomo17` 被 DeepSeek 封禁 ——
#:   "Due to violation of user policies, your account has been suspended
#:    until October 20, 2026"。
#:
#:   直接原因几乎可以确定是**自动化鉴权探测**：为了让「坏凭据必须被拒」
#:   这条断言有真凭据可依，当时的做法是**反复拿真实接口去试** —— 包括故意
#:   构造 `'x'*64`、`'y'*64`、随机 32 位串去撞鉴权。`_probe_once` 走的是
#:   `create_pow_challenge`，也就是**每次探测都是一次带鉴权头的真实请求**，
#:   还叠着 `impersonate` 的 Chrome 指纹伪装。同一 IP 上「浏览器指纹 + 高频 +
#:   拿垃圾 token 撞鉴权 + 从不真正对话」，这是风控眼里最标准的自动化特征。
#:   判罚不冤，是我自己把账号试没的。
#:
#:   所以这个开关是**默认关**的：
#:     * 产品外壳（用户手动点「刷新并验证 / 保存并验证 / 登录收口」）
#:       启动引擎时会带上 `GAD_ALLOW_LIVE_AUTH=1` —— 见 `EngineClient.cs`。
#:     * 测试、临时探针、一次性脚本**一律打不出去**。真想打，必须显式
#:       `set GAD_ALLOW_LIVE_AUTH=1`，逼出一次「我知道我在打真实接口」的决定。
#:
#:   这条闸门只拦「探测」。真正的对话（`chat/completion`）不受影响 ——
#:   那是产品本来的用途，也是用户自己要发的。
ALLOW_LIVE_ENV = "GAD_ALLOW_LIVE_AUTH"
#: 会话列表。**GET**（POST 版本回 405 + `allow=GET,HEAD`）。
SESSION_LIST_PATH = "/api/v0/chat_session/fetch_page"

#: `fetch_page` 的 `count` 硬边界（2026-10-18 实测，见 `_list_sessions_blocking`）。
#: 超上限时服务端**静默返回空列表**，所以必须在客户端拦住。
SESSION_LIST_MIN_COUNT = 2
SESSION_LIST_MAX_COUNT = 100
#: 某会话的完整历史。**GET**，需要 `chat_session_id` 查询参数。
HISTORY_PATH = "/api/v0/chat/history_messages"
#: 新建会话。**POST**，请求体是空的 `{}`（2026-10-18 实测：
#: 没有任何「临时对话」标志位，所以 `ChatConfig.temporary` 至今无法转发）。
SESSION_CREATE_PATH = "/api/v0/chat_session/create"
#: 删除会话。**POST**，请求体 `{"chat_session_id": "<id>"}`。
#:
#: 2026-10-18 净零实验确认它**真的会删**（`tools/verify_delete_session.py`：
#: 建一条 → history 查得到 → 删 → history 回 `invalid chat session id`）。
#: 注意它对**不存在的 id 也返回 `biz_code=0`** —— 幂等语义，
#: 所以「没报错」不等于「删掉了一条真实存在的会话」。
SESSION_DELETE_PATH = "/api/v0/chat_session/delete"
#: 当前账号信息。**GET**，鉴权头就够。
#:
#: 2026-10-17 实测（真 token，HTTP 200）：
#: `{"code":0,"data":{"biz_code":0,"biz_data":{
#:    "id":"c518c60d-...","email":"mo*****xx@example.com",
#:    "chat":{"is_muted":0,"mute_until":null}}}}`
#:
#: 两个用途，都很关键：
#:   1. **账号名兜底** —— 页面 DOM 里捞不到邮箱时，这里一定有（打码形式）。
#:   2. **禁言状态** —— 不必发一条 `completion` 才知道被禁言，读这个接口就行。
ACCOUNT_INFO_PATH = "/api/v0/users/current"
CLIENT_VERSION = "2.5.0"

# ---------- 朗读（TTS）----------
#
# 2026-10-18 从网页版 bundle 逆向（`main.js` 模块 89791 + `web-tts.*.js`）。
# 完整协议与证据见 `docs/20261018-朗读功能可行性方案.md`。
#
# **这不是浏览器语音包**：前端 bundle 里 `speechSynthesis` 出现 0 次，
# 而 `decoderKind` / `firstFrameLatencyMs` / `underrunCount` / `rejected_code`
# 这些只在服务端音频流场景才存在的概念全都有。所以它跟我们用的传输层
# 是同一套鉴权、同一套 TLS 指纹 —— 没有理由为它另起一条通道。
#
# 传输层只负责**控制面**（签票 + 音色），不碰音频流：
# 音频是 Opus 帧，解码在 WebView2 里是原生能力（WebCodecs），
# Python 侧要多装 40 MB 的 PyAV 才能做同样的事。所以流交给前端直连，
# 这里只把「连流所需的三个值」交出去。

#: 签发一次性票据。**POST**，请求体 `{"scope":"tts"}`，返回 36 字符的 ticket。
#: 票据与 scope 绑定、短时效、只对单条消息有效 —— 它不是账号 token，
#: 所以可以安全地交给前端（前端本来拿不到 token，见 `WebBridge` 的注释）。
TTS_TICKET_PATH = "/api/v0/auth/ticket"
#: 音色列表。**GET**，返回 4 个音色（实测全部支持中文，其中两个含粤语）。
TTS_VOICES_PATH = "/api/v0/chat/tts/voices"
#: 切换音色。**POST**，请求体 `{"voice_id":"mira"}`。这是**账号级偏好**，
#: 会写进服务端，网页版下次朗读也用这个音色。
TTS_VOICE_PATH = "/api/v0/chat/tts/voice"

#: 服务端 TTS 错误码（模块 89791 的 `me`）。收到 `finish` 事件时带 `code`。
#:
#: 前三个是「重试有意义」的，后四个不是 —— 7/8 是语种问题，9 是内容问题，
#: 4/5 是配额问题（重试只会更快撞墙）。界面据此决定要不要给「再试一次」。
TTS_CODES: dict[int, str] = {
    0: "SUCCESS",
    1: "INTERNAL_ERROR",
    2: "INVALID_INPUT",
    3: "SERVICE_ERROR",
    4: "QUOTA_EXCEEDED",
    5: "RATE_LIMIT_REACHED",
    6: "NO_CONTENT",
    7: "UNSUPPORTED_LANGUAGE",
    8: "VOICE_UNSUPPORTED_LANGUAGE",
    9: "CONTENT_FILTER",
}

# **UA 与 impersonate 必须联动** —— 原版翻车的根因之一就是 UA 声称 Chrome
# 而 TLS 握手不像 Chrome。这里从 impersonate 版本号派生 UA，两者不可能漂移。
IMPERSONATE = "chrome150"
_IMPERSONATE_VER = IMPERSONATE.removeprefix("chrome")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    f"(KHTML, like Gecko) Chrome/{_IMPERSONATE_VER}.0.0.0 Safari/537.36"
)

#: **客户端提示（Client Hints）—— 版本号必须与 UA / TLS 三者同源。**
#:
#: 2026-10-17 防封禁复盘（`docs/20261017-防封禁研究分析.md` §2.1）：
#: 真实浏览器对**每个**请求都发这三个头（`docs/evidence/probe-real-request-20261014-234856.json`
#: 全量抓包，15–17 个头），而我们**一个都不发**。一个「声称是 Chrome 的 XHR」
#: 却不带客户端提示，是教科书级的自动化特征。
#:
#: ⚠️ **已知残留差异（无法用代码消除）**：真实网页版现在是 **Chrome 155**，
#: 而本机 `curl_cffi 0.16.3` 的 impersonate 最高只到 **chrome150**（实测枚举确认，
#: 没有 chrome155）。所以这里只能整体停在 150。**不能**把 UA / 客户端提示
#: 写成 155 而 TLS 是 150 —— 那是自相矛盾，比「整体落后一个版本」更糟。
#: Chrome 150 是真实存在、仍在被大量使用的版本，不是伪造值。
#:
#: 品牌串里的 `Not(A:Brand";v="24"` 取自真实抓包（155）的形态 —— 它是 Chrome
#: 的 GREASE 品牌，版本号在 150 上是否仍为 24 **未验证**，但它是可选的噪声品牌，
#: 服务端不据它判版本。见报告的「待登录后核实」清单。
_SEC_CH_UA = (
    f'"Google Chrome";v="{_IMPERSONATE_VER}", '
    f'"Chromium";v="{_IMPERSONATE_VER}", '
    '"Not(A:Brand";v="24"'
)
SEC_CH_UA_MOBILE = "?0"
SEC_CH_UA_PLATFORM = '"Windows"'

#: `accept-language` —— **必须与 `x-client-locale: en_US` 同源**。
#:
#: 2026-10-17 23:18 全量抓包（`docs/evidence/probe-real-request-20261017-231833.json`，
#: Chrome 155.0.8059.80）实测值就是 `en-US,en;q=0.9`，不是 `zh-CN`。
#: 此前我们**一个 accept-language 都不发** —— 真实 Chrome 对每个 XHR 都会发，
#: 这是比 `sec-ch-ua` 更硬的一条自动化特征。
ACCEPT_LANGUAGE = "en-US,en;q=0.9"

_DEFAULT_DEVICE_FILE = Path(__file__).resolve().parents[2] / ".device-id"

#: 显式指定 `x-device-id` 的环境变量。**优先级最高**。
#:
#: 2026-10-17 防封禁复盘 §2.3：同一个账号上出现了**两个设备身份** ——
#: 真实浏览器是 `c4a9d011-…`，我们的引擎自己造了 `5e02088e-…`。
#: 「同一账号、两个设备」本身就是聚合层（L4）的可疑信号。
#:
#: ✅ **来源已于 2026-10-17 23:18 全量抓包确认**（此前是「未确认」，不要沿用旧结论）：
#:   * 请求头里的 `x-device-id` == `localStorage["deepseek-device-id:chat"]`
#:     （实测值 `<device-id>`）；
#:   * ⚠️ **别搞错**：`__tea_cache_tokens_20006317` / `__appKit_userInfo` 里的那个 UUID
#:     （`c518c60d-…`）是**账号 id**，与 `users/current` 的 `data.biz_data.id` 一致，
#:     **不是** device-id；`__ds_remote_feature_did`（`46f8e795-…`）是另一个用途的 id。
#:     三个 UUID 长得一样，只有 `deepseek-device-id:chat` 是真的。
#:
#: 取用方式：登录收口（`browser_login.py` 读完 token 后）顺带读这个 localStorage 键，
#: 写进本地 `.device-id` 文件 —— 见 `_load_device_id` 的优先级。
#: 环境变量这一档保留给「临时借用某个身份」的场景（它**不落盘**）。
DEVICE_ID_ENV = "GAD_DEVICE_ID"

#: 浏览器里 device-id 的 localStorage 键名（实测确认，见 `DEVICE_ID_ENV` 说明）。
DEVICE_ID_LS_KEY = "deepseek-device-id:chat"


def _tz_offset_seconds() -> str:
    """本机 UTC 偏移（秒，东八区 = 28800）。不写死，跟随系统时区。"""
    return str(-time.timezone if not time.daylight else -time.altzone)


def save_device_id(value: str, path: Path | None = None) -> bool:
    """把浏览器里的 device-id 落盘，让后续请求复用**同一个设备身份**。

    调用方：登录收口（`browser_login.py` 读完 token 后顺带读
    `localStorage["deepseek-device-id:chat"]`）。返回是否写入成功。

    **只在值确实不同时才写** —— 避免每次登录都动一次文件（多端同步/备份会看见噪音）。
    值必须是 UUID 形态，防止把空串或脏值写进去后永久污染身份。
    """
    v = (value or "").strip()
    if not v or len(v) < 32:
        return False
    target = path or _DEFAULT_DEVICE_FILE
    try:
        if target.exists() and target.read_text(encoding="utf-8").strip() == v:
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(v, encoding="utf-8")
        return True
    except OSError:
        return False


def _i18n_pick(value: object, fallback: object, last: str) -> str:
    """从 i18n 字典里取一个可显示的字符串。

    服务端的名称/描述是 `{"zh": "贝壳", "en": "Mira"}` 这种字典，不是裸字符串。
    直接 `str(v.get("name"))` 会得到空 —— 界面上就只能显示 id。

    优先级：中文 > 英文 > 任意语言 > `fallback` > `last`。
    `fallback` 是给「服务端某天改回裸字符串」留的兼容路径，
    `last` 是最后兜底（通常是 id），保证永远有东西可显示。
    """
    if isinstance(value, dict):
        for key in ("zh", "en"):
            got = value.get(key)
            if isinstance(got, str) and got.strip():
                return got.strip()
        for got in value.values():
            if isinstance(got, str) and got.strip():
                return got.strip()
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(fallback, str) and fallback.strip():
        return fallback.strip()
    return last


def _load_device_id(path: Path) -> str:
    """持久设备 UUID —— 真实浏览器生成一次后复用，不能每轮换新。

    优先级：`GAD_DEVICE_ID` 环境变量 > 本地持久文件 > 新生成。
    环境变量这一档是给「复用浏览器身份」用的（见 `DEVICE_ID_ENV` 的说明）；
    它**不落盘** —— 借来的身份不该写进我们自己的文件，否则一旦用户取消该变量，
    就会在「借来的 id」和「文件里的旧 id」之间来回横跳，反而多出一个身份。

    文件里那个值正常来自 `save_device_id()`（登录时从浏览器抄来的）。
    如果文件是**旧版本自造的**（此前引擎自己 `uuid4()` 生成），它会一直生效 ——
    下次登录会被浏览器里的真值覆盖，不需要手工清理。
    """
    env = os.environ.get(DEVICE_ID_ENV, "").strip()
    if env:
        return env
    try:
        if path.exists():
            v = path.read_text(encoding="utf-8").strip()
            if v:
                return v
        v = str(uuid.uuid4())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(v, encoding="utf-8")
        return v
    except OSError:
        # 落盘失败不该阻断主流程 —— 退回进程内临时 id
        return str(uuid.uuid4())


async def fetch_account_info_async(
    token: str, *, timeout: float = 20.0
) -> dict[str, Any]:
    """用 token 读当前账号信息。**只读 GET，不创建会话、不发对话。**

    返回 `{email, account_id, is_muted, mute_until, mute_until_text}`。

    为什么需要它（2026-10-17）：
      * 登录收口时页面 DOM 里**捞不到邮箱**（侧边栏折叠 / 渲染未完成），
        `account_label` 因此是空的，界面上看不到「登的是哪个号」。
        服务端一定知道这个 token 属于谁 —— 问它比猜 DOM 可靠。
      * 顺带解决了「禁言状态只有发消息才知道」：`chat.is_muted` 直接给答案，
        不用为了显示状态而消耗一条真实对话。

    **邮箱是打码形式**（`mo*****xx@example.com`）—— 这是 DeepSeek 的保护策略，
    照原样显示，不尝试还原。

    失败时抛异常，由调用方决定是「降级为空」还是「报错」。
    """
    headers = {
        "accept": "*/*",
        "authorization": f"Bearer {token}",
        "content-type": "application/json",
        "origin": BASE,
        "referer": BASE + "/",
        "user-agent": USER_AGENT,
        "x-client-bundle-id": "com.deepseek.chat",
        "x-client-locale": "en_US",
        "x-client-platform": "web",
        "x-client-version": CLIENT_VERSION,
        "x-client-timezone-offset": _tz_offset_seconds(),
    }
    from curl_cffi import requests as creq

    session = creq.Session(impersonate=IMPERSONATE)
    try:
        resp = await asyncio.to_thread(
            session.get, BASE + ACCOUNT_INFO_PATH, headers=headers, timeout=timeout
        )
    finally:
        session.close()

    if resp.status_code != 200:
        raise RuntimeError(f"读账号信息失败：HTTP {resp.status_code}")
    try:
        payload = resp.json()
    except ValueError as exc:
        raise RuntimeError("账号信息不是合法 JSON") from exc

    # 两层 `code` 都要看：外层是网关的，`biz_code` 是业务的。
    # 只看 HTTP 200 会把「鉴权过了但业务拒绝」当成有效凭据 —— 正是要避免的假绿。
    if payload.get("code") not in (0, None):
        raise RuntimeError(
            f"账号信息返回 code={payload.get('code')}：{payload.get('msg') or ''}"
        )
    data = payload.get("data") or {}
    if data.get("biz_code") not in (0, None):
        raise RuntimeError(
            f"账号信息返回 biz_code={data.get('biz_code')}：{data.get('biz_msg') or ''}"
        )

    biz = data.get("biz_data") or {}
    chat = biz.get("chat") or {}
    return {
        "email": str(biz.get("email") or ""),
        "account_id": str(biz.get("id") or ""),
        "is_muted": bool(chat.get("is_muted")),
        "mute_until": chat.get("mute_until"),
        "mute_until_text": mute_until_text(chat),
    }


#: SEARCH 片段收尾时的状态文本。实测形如 `Found 12 web pages`（英文、固定格式）。
#: 它是**搜索状态**而不是回答 —— 混进正文用户会看到一句英文噪声。
_SEARCH_META_RE = re.compile(r"^Found\s+(\d+)\s+web\s+pages?\s*$", re.IGNORECASE)

#: fragment 类型（2026-10-15 实测值域，**比想象宽得多**）：
#:
#: ```
#: RESPONSE      正文
#: THINK         思考草稿   ← 注意是 `THINK`，不是 `THINKING`
#: SEARCH        搜索状态（content 就是 `Found 12 web pages`）
#: TOOL_OPEN     工具打开
#: TOOL_SEARCH   搜索工具
#: ```
#:
#: **判据必须是「正文白名单」，不能是「思考黑名单」。** 原来的实现是
#: `if type == "THINKING": 思考 else: 正文`，于是 `THINK` / `TOOL_OPEN` /
#: `TOOL_SEARCH` / `SEARCH` **全部落进正文** —— 实测（14:33）用户看到的回答
#: 开头是模型的内心独白，中间还夹一句 `Found 17 web pages`。
#:
#: 白名单的代价是「未知类型不进正文」。这个方向是对的：**宁可少给，也不能
#: 把思维链当回答递给用户** —— 思维链里可能带工具围栏，被误执行后果更重。
_REPLY_FRAG_TYPES = frozenset({"RESPONSE"})
_THINK_FRAG_TYPES = frozenset({"THINK", "THINKING", "THOUGHT", "REASONING"})
_SEARCH_FRAG_TYPES = frozenset({"SEARCH"})
_TOOL_FRAG_TYPES = frozenset({"TOOL_OPEN", "TOOL_SEARCH", "TOOL_CLOSE", "TOOL_RESULT"})


@dataclass
class _CancelToken:
    """协作式取消标志 —— **跨线程、无阻塞、只读一次布尔**。

    ## 为什么不用 `Task.cancel()` 直接停

    `transport.send()` 把阻塞读取跑在 `run_in_executor` 的线程里
    （`curl_cffi` 是同步 API）。`asyncio` 的取消**只能中断 `await` 点**，
    中断不了一个正在 `resp.iter_lines()` 里阻塞的线程 —— 线程会继续跑到流结束。
    结果就是：用户点了「停止」，界面上的状态变了，但引擎仍在读、仍在计费、
    仍在往对话里塞字。**这是最糟的一种「假停止」。**

    所以真正的停止点必须在**读循环内部**：每读一行检查一次标志。

    ## 为什么不用 `threading.Event`

    功能上完全等价，但 `Event.is_set()` 是一次方法调用 + 锁检查，
    而这里每收到一行 SSE 就要问一次（一条长回复几千行）。
    `bool` 的读取是单条 `LOAD_ATTR`，且 CPython 里读布尔属性天然是原子的 ——
    不需要锁，也不会看到「半个 True」。

    ## 为什么标志设在**传输层实例**上而不是 `send()` 的参数里

    界面按「停止」时手里只有引擎的 RPC 句柄，拿不到那次 `send()` 的局部变量。
    标志必须挂在双方都能摸到的地方，也就是 transport 实例。

    ⚠️ **同一个实例同时只能有一轮在跑**（引擎是单轮串行的，见 `Agent.run`）。
    若将来支持并发多轮，这个标志必须改成「每轮一个 token」。
    """

    __slots__ = ("_flag",)

    def __init__(self) -> None:
        self._flag = False

    def request(self) -> None:
        self._flag = True

    def clear(self) -> None:
        self._flag = False

    @property
    def requested(self) -> bool:
        return self._flag


@dataclass
class _Acc:
    """SSE 累积器。

    组装规则（2026-10-15 实测）：**快照 + 后续增量**。

    流的第一批帧里有一帧全量对象，`fragments[].content` 是**当时的完整正文**
    （例如 `"1"`）；其后的 `p/v` 增量帧接着它往后拼（`"\\n"`、`"2"`…）。
    所以最终正文 = 最后一次快照内容 + 快照之后的全部增量。
    """

    snapshot_text: str = ""
    pending_text: list[str] = field(default_factory=list)
    snapshot_thoughts: str = ""
    pending_thoughts: list[str] = field(default_factory=list)
    #: 已经通过 `emit` 送出去的正文 / 思考，**分片存放**。
    #: 存在的唯一理由是**和快照对齐**：快照帧可能携带增量通道从未送过的内容，
    #: 靠它算出「还差哪一段没发」，见 `_SseParser._emit_snapshot_gap`。
    #:
    #: ⚠️ **必须是 list，不能是 str。** 这两个字段每收到一个增量帧就追加一次，
    #: 而 str 的 `+=` 每次都会复制已有全部内容 —— 一条 1 万字回复按帧累积，
    #: 总复制量约 n²/2 字符（5000 万字），全程持续压在事件循环上。
    #: 分片存放后热路径只有一次 append；需要整串的地方（只有快照对齐，
    #: 一轮流里通常 1–2 次）才 `"".join()` 一次。
    emitted_text: list[str] = field(default_factory=list)
    emitted_thoughts: list[str] = field(default_factory=list)

    def emitted(self, channel: str) -> str:
        """把「已发出的」拼成整串。**只给快照对齐用** —— 别放进每帧的热路径。"""
        return "".join(
            self.emitted_thoughts if channel == "thought" else self.emitted_text
        )
    request_message_id: int | None = None
    response_message_id: int | None = None
    status: str = ""
    title: str = ""
    # 服务端在 `event: hint` 里回的错误（实测 2026-10-15：
    # `{"type":"error","content":"Server busy...","finish_reason":"generation_timeout"}`）
    error: str = ""
    finish_reason: str = ""
    # ---- 联网搜索（2026-10-15 实测）----
    # 开了 `search_enabled` 之后，流里会多出 SEARCH 片段与 `fragments/-1/results`：
    #   * `search_triggered: true`  —— 快照里的标志位
    #   * SEARCH 片段的 content 是 **`Found 12 web pages`**（状态，不是回答）
    #   * results 是来源列表（url / title / snippet / cite_index …）
    # 这些**都不能进正文** —— 混进去用户会看到英文噪声，见 `_SseParser._note_search`。
    search_triggered: bool = False
    search_pages: int = 0
    search_results: list[dict] = field(default_factory=list)
    #: 见过但**没有归入正文**的 fragment 类型（TOOL_OPEN 之类）。
    #: 只为诊断 —— 出现没见过的类型时能一眼看出来，而不是靠猜。
    skipped_frag_types: set[str] = field(default_factory=set)

    def reply_text(self) -> str:
        return self.snapshot_text + "".join(self.pending_text)

    def thought_text(self) -> str:
        return self.snapshot_thoughts + "".join(self.pending_thoughts)


class _SseParser:
    """DeepSeek completion 的 SSE 状态机。

    **关键：这是粘性路径协议。** 只有增量序列的**第一帧**带 `p`（路径）与 `o`（操作），
    后续帧只有 `{"v": "..."}` —— 沿用上一帧的路径与操作。若只认带 `p` 的帧，
    会丢掉除首帧以外的全部正文（本项目 08:24 实测踩过）。

    帧型一览（实测）：

    ```
    event: ready      data: {"request_message_id":1,"response_message_id":2,...}
    event: update_session / title / close
    data: {"v": {"response": {...}}}                      ← 全量快照
    data: {"p":"...","o":"APPEND","v":"..."}              ← 增量首帧
    data: {"v": "..."}                                    ← 增量后续帧（粘性）
    data: {"p":"response","o":"BATCH","v":[{...},{...}]}  ← 批量子帧
    data: {"p":"response/status","o":"SET","v":"FINISHED"}
    ```
    """

    def __init__(self, acc: _Acc, emit: Callable[[str, Any], None]) -> None:
        self.acc = acc
        self.emit = emit
        self.cur_path = ""
        self.cur_op = "APPEND"
        #: 当前生效的 SSE `event:` 名（粘性，见 `feed_line`）。
        self._line_event: str | None = None
        #: 最近一次见到的 fragment 类型。
        #: **必需**：增量帧的路径是 `response/fragments/-1/content`，`-1` 指「最后一个
        #: fragment」—— 它是 SEARCH 还是 RESPONSE，光看路径看不出来，只能靠快照/
        #: 追加时记下的类型来判定。判定错了，搜索状态会混进正文。
        self._last_frag_type = ""

    # -- 入口 --

    def feed(self, event: str | None, data: str) -> None:
        try:
            obj = json.loads(data)
        except ValueError:
            return
        if not isinstance(obj, dict):
            return

        # 业务错误：服务端可能直接回非 SSE 的 JSON（HTTP 200 但 biz 失败）
        d = obj.get("data")
        if isinstance(d, dict) and d.get("biz_code"):
            suspended, detail = describe_biz_failure(d)
            raise TransportError(
                ("账号被处置（禁言/封禁）：" if suspended else "completion 业务错误 ")
                + f"biz_code={d.get('biz_code')}：{detail}",
                fatal=False,
                auth=suspended,
                suspended=suspended,
            )

        if event == "ready":
            if isinstance(obj.get("request_message_id"), int):
                self.acc.request_message_id = obj["request_message_id"]
            if isinstance(obj.get("response_message_id"), int):
                self.acc.response_message_id = obj["response_message_id"]
            return
        if event == "title":
            if isinstance(obj.get("content"), str):
                self.acc.title = obj["content"]
            return
        if event == "hint":
            # 服务端错误通道。**别当成空回复** —— 这里带的是真实原因。
            if obj.get("type") == "error":
                self.acc.error = str(obj.get("content") or "服务端返回错误")
                self.acc.finish_reason = str(obj.get("finish_reason") or "")
            return
        if event == "close":
            return

        # 带路径的帧（含 BATCH）
        if isinstance(obj.get("p"), str):
            self._frame(obj)
            return

        v = obj.get("v")
        # 粘性增量帧：只有 v，沿用上一帧路径
        if isinstance(v, str) and self.cur_path:
            self._delta(self.cur_path, self.cur_op, v)
            return
        # 全量快照
        if isinstance(v, dict):
            self._snapshot(v)

    # -- 内部 --

    def _frame(self, obj: dict, parent: str = "") -> None:
        raw_path = obj["p"]
        # **BATCH 的子帧路径是相对父路径的**（实测 `response/fragments/-1` 的 BATCH 里
        # 子帧写的是裸 `content` / `status`）。不拼接就会把 `content` 当成顶层路径，
        # 于是「搜索片段的收尾文本」被误判成正文（2026-10-15 实测踩过）。
        path = f"{parent}/{raw_path}" if parent and "/" not in raw_path else raw_path
        op = obj.get("o") or self.cur_op
        v = obj.get("v")

        if op == "BATCH" and isinstance(v, list):
            for sub in v:
                if isinstance(sub, dict) and isinstance(sub.get("p"), str):
                    self._frame(sub, path)
            return

        low = path.lower()

        # 搜索来源列表（`response/fragments/-1/results`）—— 记下来，不进正文
        if low.endswith("/results") and isinstance(v, list):
            self._ingest_results(v)
            return

        if not isinstance(v, str):
            if low.endswith("status"):
                self.acc.status = str(v)
            elif low.endswith("fragments") and isinstance(v, list):
                # 新片段进场。实测（搜索轮次）：SEARCH 收尾后，RESPONSE 片段以
                # `{"p":"fragments","o":"APPEND","v":[{id:3,type:"RESPONSE","content":"上海"}]}`
                # 追加 —— **它的 content 就是正文的开头**，丢掉正文会缺字。
                self._ingest_fragments(v)
            return

        if low.endswith("status"):
            self.acc.status = v
            return

        # 只对正文/思考路径建立粘性
        if "thinking" in low or low.endswith("content"):
            self.cur_path = path
            self.cur_op = op
            self._delta(path, op, v)

    def _delta(self, path: str, op: str, value: str) -> None:
        low = path.lower()
        ftype = self._last_frag_type
        is_content = low.endswith("content")

        if "thinking" in low or ftype in _THINK_FRAG_TYPES:
            bucket, channel, is_text = self.acc.pending_thoughts, "thought", False
        elif ftype in _SEARCH_FRAG_TYPES:
            # 搜索状态（`Found N web pages`）—— 不进正文
            self._note_search(value)
            return
        elif is_content and (ftype in _REPLY_FRAG_TYPES or not ftype):
            bucket, channel, is_text = self.acc.pending_text, "delta", True
        else:
            # 其余类型（TOOL_OPEN / TOOL_SEARCH / 未知）一律不进正文。
            # 记下类型名，方便下次看到没见过的值时一眼定位。
            if ftype:
                self.acc.skipped_frag_types.add(ftype)
            return

        if op == "APPEND":
            bucket.append(value)
            if is_text:
                self.acc.emitted_text.append(value)
            else:
                self.acc.emitted_thoughts.append(value)
            self.emit(channel, value)
        else:  # SET 等：整体替换
            bucket.clear()
            bucket.append(value)
            # 整体替换会把此前流出的内容作废 —— 对齐基准跟着重设，
            # 否则下一次快照对齐会以为「这些都发过了」而漏掉。
            # 重置成 `[快照, 新值]` 两片，等价于旧实现的「快照 + 新值」整串。
            if is_text:
                self.acc.emitted_text = [self.acc.snapshot_text, value]
            else:
                self.acc.emitted_thoughts = [self.acc.snapshot_thoughts, value]

    def _snapshot(self, v: dict) -> None:
        resp = v.get("response")
        if not isinstance(resp, dict):
            return
        mid = resp.get("message_id")
        if isinstance(mid, int):
            self.acc.response_message_id = mid
        st = resp.get("status")
        if isinstance(st, str) and st:
            self.acc.status = st
        if resp.get("search_triggered") is not None:
            self.acc.search_triggered = bool(resp.get("search_triggered"))
        for frag in resp.get("fragments") or []:
            if not isinstance(frag, dict):
                continue
            ftype = str(frag.get("type") or "").upper()
            if ftype:
                # 记住「最后一个 fragment 是什么」—— 增量帧的 `-1` 靠它解析
                self._last_frag_type = ftype
            content = frag.get("content")
            if not isinstance(content, str):
                continue
            if ftype in _THINK_FRAG_TYPES:
                self.acc.snapshot_thoughts = content
                self.acc.pending_thoughts.clear()
                self._emit_snapshot_gap("thought")
            elif ftype in _SEARCH_FRAG_TYPES:
                # 搜索片段：content 是状态文本（`Found N web pages`），不是回答
                self._note_search(content)
            elif ftype in _REPLY_FRAG_TYPES or not ftype:
                self.acc.snapshot_text = content
                self.acc.pending_text.clear()
                self._emit_snapshot_gap("delta")
            else:
                # TOOL_* 等 —— 不进正文，只记类型名供诊断
                self.acc.skipped_frag_types.add(ftype)

    def _emit_snapshot_gap(self, channel: str) -> None:
        """把「只在快照里、从未走过增量通道」的正文补发出去。

        **为什么必需**（2026-10-15 14:15 实测）：短回复可能**整段只出现在快照帧**，
        一个增量帧都没有。此时 `reply_text()` 能拼出正文，但 `on_delta` 一次都不会
        被调用 —— 界面看不到这段字，编排层用增量拼出的 `final_text` 也是空的，
        用户看到的就是「消息发出去了，但一个字都没回来」。

        补发按「已 emit 的前缀」算差值：快照通常是在原内容之后追加，
        只有被重写（不是简单追加）时才整段重发。
        """
        if channel == "thought":
            full, seen = self.acc.snapshot_thoughts, self.acc.emitted("thought")
        else:
            full, seen = self.acc.snapshot_text, self.acc.emitted("reply")
        if not full or full == seen:
            return
        if full.startswith(seen):
            new = full[len(seen) :]
            if channel == "thought":
                self.acc.emitted_thoughts.append(new)
            else:
                self.acc.emitted_text.append(new)
        else:
            # 快照被重写，无法用增量表达 —— 整段补发，并把对齐基准重置到新内容。
            # 重置成单片 `[full]`：语义是「已经发出的内容就是这一整串」，
            # 后续 `emitted()` 拼出来与旧实现完全一致。
            new = full
            if channel == "thought":
                self.acc.emitted_thoughts = [full]
            else:
                self.acc.emitted_text = [full]
        self.emit(channel, new)

    # -- 联网搜索（2026-10-15 实测新增）--
    #
    # 开了 `search_enabled` 之后，流里会多出三样东西，**一样都不能进正文**：
    #   * `search_triggered: true`（快照标志位）
    #   * `response/fragments/-1/results`（来源列表：url / title / snippet / cite_index）
    #   * SEARCH 片段的 content —— 实测就是 `Found 12 web pages`
    # 原来的实现里，前两样被静默丢弃（信息白丢），第三样被误判成正文（污染回答）。

    def _note_search(self, text: str) -> None:
        """SEARCH 片段的 content —— 是搜索状态，不是回答。"""
        body = (text or "").strip()
        if not body:
            return
        m = _SEARCH_META_RE.match(body)
        if m:
            self.acc.search_pages = int(m.group(1))
        self.acc.search_triggered = True

    def _ingest_results(self, items: list) -> None:
        """搜索来源列表。记下来 —— 界面可据此显示「已联网，N 个来源」。"""
        added = 0
        for it in items:
            if isinstance(it, dict):
                self.acc.search_results.append(it)
                added += 1
        if added:
            self.acc.search_triggered = True

    def _ingest_fragments(self, frags: list) -> None:
        """`response.fragments` 的 APPEND —— 新片段进场。

        实测（2026-10-15 搜索轮次）：SEARCH 片段收尾后，RESPONSE 片段以
        `{"p":"fragments","o":"APPEND","v":[{"id":3,"type":"RESPONSE","content":"上海"}]}`
        追加进来。**它的 content 就是正文的开头** —— 原来的实现把它整个丢掉了
        （路径是裸 `fragments`，既不以 status 结尾也不是 content，直接 return），
        于是回答会缺开头几个字。
        """
        for frag in frags:
            if not isinstance(frag, dict):
                continue
            ftype = str(frag.get("type") or "").upper()
            if ftype:
                self._last_frag_type = ftype
            content = frag.get("content")
            if not isinstance(content, str) or not content:
                continue
            if ftype in _THINK_FRAG_TYPES:
                self.acc.pending_thoughts.append(content)
                self.acc.emitted_thoughts.append(content)
                self.emit("thought", content)
            elif ftype in _SEARCH_FRAG_TYPES:
                self._note_search(content)
            elif ftype in _REPLY_FRAG_TYPES or not ftype:
                self.acc.pending_text.append(content)
                self.acc.emitted_text.append(content)
                self.emit("delta", content)
            else:
                # TOOL_* 等 —— 不进正文，只记类型名供诊断
                self.acc.skipped_frag_types.add(ftype)

    # -- 行级入口（供不同档位共用）--

    def feed_line(self, line: str) -> bool:
        """喂一行 SSE 原始文本，返回 False 表示流已结束（收到 `[DONE]`）。

        **为什么把它从 `_stream_completion` 里提出来**：浏览器档位
        （`chrome_cdp.py`）拿到的也是同一种 SSE 字节流，只是获取方式从
        「curl 读 socket」变成了「页面 `fetch` 读 ReadableStream，再经 CDP
        binding 推回来」。若两边各写一份行解析，**漂移是必然的** —— 一边
        补了「HTTP 200 但回裸 JSON」的分支、另一边没补，同一个账号在两条
        路径上会给出不同结论（这正是 `describe_biz_failure` 当初被抽出来的原因）。

        行协议本身很简单，但有三处容易写错，都在下面注释里标了。
        """
        # ① 空行是 SSE 的帧分隔符，不是数据。
        if not line:
            return True

        # ② `event:` 是**粘性**的：它作用于其后所有的 `data:`，直到下一个
        #    `event:`。现有实现就是这么宽松处理的（严格 SSE 规范里一个
        #    event 只管紧随其后的那一帧），**保持一致**，别在这里「修正」——
        #    服务端实测依赖这个宽松行为。
        if line.startswith("event: "):
            self._line_event = line[7:].strip()
            return True

        if not line.startswith("data: "):
            # ③ 服务端在 HTTP 200 下**可能直接回一段裸 JSON**，不是 SSE。
            #    实测 2026-10-17：账号被禁言时整条响应就是
            #    `{"code":0,"msg":"","data":{"biz_code":5,"biz_msg":"user is muted",...}}`。
            #    交给 parser 判 biz_code（它认得 `data.biz_code`），
            #    否则「账号被禁言」会被伪装成「流为空」。
            stripped = line.strip()
            if stripped.startswith("{"):
                self.feed(None, stripped)
            return True

        data = line[6:].strip()
        if data == "[DONE]":
            return False
        if not data:
            return True
        self.feed(self._line_event, data)
        return True


class DeepSeekRpcTransport(Transport):
    """DeepSeek 网页版私有接口的直连传输层。"""

    name = "deepseek-rpc"
    supports_streaming = True

    def __init__(
        self,
        *,
        token: str = "",
        token_provider: Callable[[], str] | None = None,
        device_id_path: Path | None = None,
        model: str = "default",
        search_enabled: bool = False,
        workers: int | None = None,
        timeout: float = 120.0,
        proxy: str | None = None,
        health_probe: bool = False,
        pow_prefetch: bool = True,
        pow_prefetch_count: int = 2,
        pow_expire_margin_sec: float = 30.0,
    ) -> None:
        self._token = token
        self._token_provider = token_provider
        self._device_path = device_id_path or _DEFAULT_DEVICE_FILE
        self._device_id = ""
        self._model = model
        self._search_enabled = search_enabled
        self._workers = workers
        self._timeout = timeout
        self._proxy = proxy
        # 主动健康探测默认关闭 —— DeepSeek 没有专用的账号状态接口，
        # 每次探测都得打一个真实业务接口，属额外机器特征（见 README 的取舍）。
        self._health_probe = health_probe

        # PoW 预取缓存（2026-10-18）—— 详见 `gad_engine/pow_cache.py` 与
        # `docs/20261017-防封禁研究分析.md` §2.5。真实网页版一次并发取 3 个
        # challenge 并缓存复用；我们每次同步解 5.1–7.7 秒，这是目前测到的
        # 最硬的行为差异。预取把大多数发送的 PoW 开销降到 0。
        self._pow_prefetch = bool(pow_prefetch)
        self._pow_prefetch_count = max(0, int(pow_prefetch_count))
        self._pow_expire_margin = float(pow_expire_margin_sec)
        self._pow_cache = None  # PowCache | None

        # 「停止生成」的协作式标志（2026-10-18，T-19）。
        # 挂实例上而不是 send() 局部变量：界面按停止时拿不到那次调用的栈帧。
        # 详见 `_CancelToken` 的类注释 —— 尤其「为什么不能靠 Task.cancel()」。
        self._cancel = _CancelToken()

        self._session = None  # curl_cffi Session
        self._started_at = 0.0
        self._last_status = RemoteStatus()
        #: 账号被处置（封禁）的最近一次证据。由 `verify_credentials` /
        #: `send` 捕获到 `TransportError.suspended` 时置位；界面据此显示
        #: 「账号被处置」而不是「未登录 / 重新登录」。
        self._account_suspended = False
        self._suspended_detail = ""
        #: 发送审计的单调序号 —— 把「发出前」与「结束后」两行串起来。
        self._audit_seq = 0
        #: 封禁快照是否已经拍过。**只拍一次** —— 禁言期间每次请求都失败，
        #: 不去重就会在几秒内生成几十个几乎相同的目录。
        self._incident_snapshot_done = False

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        from curl_cffi import requests as creq

        token = await self._resolve_token()
        if not token:
            raise TransportError("未取得 DeepSeek token —— 未登录或凭据失效", auth=True)

        self._session = obs.RecordingSession(creq.Session(impersonate=IMPERSONATE))
        self._device_id = _load_device_id(self._device_path)
        self._started_at = time.time()

        # PoW 预取：后台线程，延迟 `initial_delay_sec` 才开始 —— 启动瞬间
        # 就开 8 个进程解 PoW 会把启动拖慢，而那时用户还没发消息。
        if self._pow_prefetch and self._pow_prefetch_count > 0:
            from ..pow_cache import PowCache

            cache = PowCache(
                capacity=self._pow_prefetch_count,
                expire_margin_sec=self._pow_expire_margin,
            )
            cache.start(self._prefetch_one)
            self._pow_cache = cache

    async def stop(self) -> None:
        # 先停预取线程 —— 它可能正在解一个 PoW，此时关 session 会让它
        # 拿不到 challenge（虽然不影响正确性，但会白记一次 failed）。
        if self._pow_cache is not None:
            with contextlib.suppress(Exception):
                self._pow_cache.stop()
            self._pow_cache = None

        if self._session is not None:
            with contextlib.suppress(Exception):
                self._session.close()
            self._session = None

    async def _resolve_token(self) -> str:
        if self._token_provider is not None:
            r = self._token_provider()
            if inspect.isawaitable(r):
                r = await r
            return (r or "").strip()
        return self._token.strip()

    def _require_session(self):
        if self._session is None:
            raise TransportError("transport 未 start()", fatal=True)
        return self._session

    # ---------- 头部 ----------

    def _headers(
        self,
        token: str,
        referer: str | None = None,
        *,
        with_origin: bool = False,
    ) -> dict[str, str]:
        """构造请求头。**每一项都有实测依据**。

        依据：2026-10-17 23:18 全量抓包
        `docs/evidence/probe-real-request-20261017-231833.json`
        （Chrome 155.0.8059.80，经 `Network.requestWillBeSentExtraInfo` 取真实上网头，
        `history_messages` 25 个头 / `completion` 30 个头）。

        本轮由实测结清的三条：

        1. **`accept-language: en-US,en;q=0.9`** —— 真实浏览器每个 XHR 都发，
           我们此前**一个都不发**。值不是 `zh-CN`，与 `x-client-locale: en_US` 同源。
        2. **`sec-fetch-site/mode/dest`** —— 实测 `same-origin` / `cors` / `empty`，
           同样是每个 XHR 都发。
        3. **`origin` 按接口区分** —— 实测 `completion`（POST）**有**
           `origin: https://chat.deepseek.com`，而 `history_messages`（GET）**没有**。
           此前我们对**所有**请求都发 origin，「同源 GET 却带 origin」是真实客户端
           不会出现的组合。现在默认不带，只有 `completion` 显式要求才带。

        `priority: u=1, i` 同为实测所见（Fetch Priority），一并补上。
        """
        h = {
            "accept": "*/*",
            "accept-language": ACCEPT_LANGUAGE,
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
            "priority": "u=1, i",
            # ★ 客户端提示 —— 真实浏览器每个请求都发，我们此前一个都不发。
            #   版本号从 `IMPERSONATE` 派生，与 UA / TLS 同源，不允许漂移。
            "sec-ch-ua": _SEC_CH_UA,
            "sec-ch-ua-mobile": SEC_CH_UA_MOBILE,
            "sec-ch-ua-platform": SEC_CH_UA_PLATFORM,
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "referer": referer or (BASE + "/"),
            "user-agent": USER_AGENT,
            "x-client-bundle-id": "com.deepseek.chat",
            "x-client-locale": "en_US",
            "x-client-platform": "web",
            "x-client-version": CLIENT_VERSION,
            "x-client-timezone-offset": _tz_offset_seconds(),
            "x-device-id": self._device_id,
            "x-device-model": "",
        }
        if with_origin:
            h["origin"] = BASE
        return h

    # ---------- 状态 ----------

    async def verify_credentials(self) -> tuple[bool, str]:
        """用一次极轻的 `create_pow_challenge` 验证 token 是否仍然有效。

        返回 `(ok, 说明)`。**不创建会话、不发消息** —— 只验鉴权。

        **为什么需要它**（阶段 3 的核心）：DeepSeek 的 `401` **无法区分
        「token 过期」与「账号被处置」**（见 `circuit.py`）。如果不在取凭据时
        先验一次，用户会看到「登录成功」却在第一次对话时炸，而且分不清原因。
        验一次能把这两种情况在**最早的时刻**分开：
          * 验不通 + 浏览器里能读到新 token ⇒ 只是过期，刷新即可
          * 验不通 + 浏览器里读到的也一样 ⇒ 才是账号侧问题
        """
        self._require_session()
        token = await self._resolve_token()
        if not token:
            return False, "无 token"
        try:
            await asyncio.to_thread(self._probe_once, token)
        except TransportError as exc:
            if exc.suspended:
                self._account_suspended = True
                self._suspended_detail = str(exc)
                self._snapshot_incident_once("suspended")
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001
            return False, f"验证异常：{exc}"
        # 探测通过 = 账号可用，清掉旧的处置标记
        self._account_suspended = False
        self._suspended_detail = ""
        return True, "create_pow_challenge 200"

    async def health(self) -> RemoteStatus:
        st = RemoteStatus(reachable=False, model=self._model)
        try:
            token = await self._resolve_token()
        except Exception as exc:  # noqa: BLE001
            st.note = f"取 token 失败：{exc}"
            return st
        if not token:
            st.note = "无 token"
            return st

        st.reachable = True
        st.account_status = 0
        st.account_status_short = "正常"
        st.account_status_text = "有 token（未做主动探测）"
        if self._account_suspended:
            st.account_status_short = "账号被处置"
            st.account_status_text = self._suspended_detail or "账号被服务端处置（封禁）"

        if self._health_probe:
            ok, detail = await self.verify_credentials()
            st.note = f"主动探测：{detail}"
            if not ok:
                st.reachable = False
                if "401" in detail or "403" in detail or "未认证" in detail:
                    st.account_status = 1016
                    st.account_status_short = "未认证"
                    st.account_status_text = detail

        st.note = (
            (st.note + " · ") if st.note else ""
        ) + "DeepSeek 无额度/账号状态接口，L4 只能事后刹车"
        self._last_status = st
        return st

    def _probe_once(self, token: str) -> None:
        # ★ 真实鉴权探测的闸门 —— **默认拒绝**，理由见文件上方 ALLOW_LIVE_ENV 的说明。
        #   放在发请求之前的第一行：宁可不验，也不能再拿真实接口去试。
        if os.environ.get(ALLOW_LIVE_ENV) != "1":
            raise TransportError(
                f"已阻止真实鉴权探测（{ALLOW_LIVE_ENV} 未设为 1）。"
                "这是为了不再触发风控 —— 2026-10-17 账号被封的直接原因就是"
                "自动化探测真实接口。产品外壳会自己带上这个变量；"
                "测试 / 探针 / 一次性脚本不该真打接口，请用假会话"
                "（`tp._require_session = lambda: _Sess(...)`）来验判定逻辑。",
                auth=False,
            )
        s = self._require_session()
        resp = s.post(
            BASE + CHALLENGE_PATH,
            headers=self._headers(token),
            json={"target_path": COMPLETION_PATH},
            timeout=15,
        )
        if resp.status_code in (401, 403):
            raise TransportError(f"探测被拒：HTTP {resp.status_code}", auth=True)
        if resp.status_code != 200:
            raise TransportError(f"探测异常：HTTP {resp.status_code}")

        # ★ 关键：DeepSeek 把「鉴权失败」报成 **HTTP 200 + 业务码 40003**，
        #   不是 401/403：
        #       {"code":40003,"msg":"Authorization Failed (invalid token)","data":null}
        #   只看状态码的话，这个探测**对任何非空串都返回「通过」** ——
        #   实测 `'x'*64`、`'y'*64`、随机 32 位串全部「验证通过」。
        #   后果不是「少验一次」那么轻：坏凭据一路绿灯，最后把原来那份可用的
        #   覆盖掉（真实发生过的数据损坏，见 identity.login.finish 的注释）。
        #   所以这里必须看业务码，并把鉴权类失败标成 auth=True，
        #   好让熔断层按「认证失败」而不是「RPC 抖动」退避。
        try:
            body = resp.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            code = body.get("code")
            if code not in (0, None):
                msg = str(body.get("msg") or "")
                low = msg.lower()
                suspended = looks_suspended(msg)
                is_auth = (
                    code == AUTH_FAILED_BIZ_CODE
                    or "authorization failed" in low
                    or "invalid token" in low
                    or "unauthorized" in low
                    or suspended
                )
                raise TransportError(
                    ("账号被处置（封禁）：" if suspended else "探测被拒：")
                    + f"业务码 {code} {msg}".strip(),
                    auth=is_auth,
                    suspended=suspended,
                )

    def available_models(self) -> list[ModelInfo]:
        # 原版从不切模型（全项目只有 "default" 一个值）—— 不臆造其它档位。
        return [ModelInfo(name="default", display_name="DeepSeek（默认）", is_available=True)]

    async def set_model(self, name: str) -> None:
        name = (name or "").strip()
        if name and name not in {"default"}:
            raise TransportError(f"不支持的模型：{name}", config=True)
        self._model = name or "default"

    def background_state(self) -> dict:
        state = {
            "transport": self.name,
            "impersonate": IMPERSONATE,
            "device_id": self._device_id,
            "uptime_sec": round(time.time() - self._started_at, 1) if self._started_at else 0,
            "health_probe": self._health_probe,
            "account_suspended": self._account_suspended,
            "suspended_detail": self._suspended_detail,
            "note": "无后台保活/心跳（DeepSeek 侧不需要 cookie 轮换）",
        }
        # PoW 预取缓存的实时状态。
        #
        # **为什么必须暴露**：没有它，验证「预取到底有没有在工作」只能去数
        # 进程、看 CPU —— 2026-10-18 实测就是这么干的，而且数进程还数错了
        # （spawn 出来的 worker 命令行不含项目名，按名字过滤会漏）。
        # 有了这几项，一次 status 就能回答：缓存几条、产了几条、用了几条、
        # 过期几条、失败几次。
        state["pow_cache"] = self._pow_cache.stats() if self._pow_cache is not None else None
        return state

    # ---------- 发送 ----------

    def request_stop(self) -> bool:
        """请求停止当前这一轮。**同步、无等待、可从任意线程调用**。

        返回 `True` 表示「标志已置位」，`False` 表示当前根本没有在跑的轮次
        （没有轮次时置位会污染下一轮 —— 所以由 `send()` 自己在开头 clear，
        这里不做多余判断，只负责置位）。

        调用后发生什么：读循环在**下一行 SSE 到达时**退出，已收到的正文
        照常返回（`TurnOutcome.cancelled=True`）。延迟上限 = 上游两次吐字
        的间隔，通常是几十毫秒。

        **不会立刻断 TCP** —— 主动断流会让上游记录一次异常断开，
        与真实用户点「停止」的行为不一致。真实网页版点停止也是读到
        当前行为止再关。
        """
        self._cancel.request()
        return True

    def clear_stop(self) -> None:
        """清除停止标志。由 `Agent.run()` 在每轮任务开头调用。

        **不在 `send()` 里清**：用户可能在「工具执行阶段」按停止 ——
        那时没有任何 `send()` 在跑。若清零点在 `send()` 入口，标志会在
        下一次 `send()` 一开始就被清掉，用户的停止请求被静默吞掉。
        """
        self._cancel.clear()

    async def send(
        self,
        prompt: str,
        session: SessionMeta,
        *,
        temporary: bool = True,
        extended_thinking: bool = False,
        search_enabled: bool | None = None,
        files: list[str] | None = None,
        on_delta: DeltaCallback | None = None,
        on_thought: ThoughtCallback | None = None,
    ) -> TurnOutcome:
        self._require_session()

        # ★ 真实发送的闸门 —— **默认拒绝**，与 `_probe_once` 共用同一把开关。
        #
        # 2026-10-17 事故复盘（`docs/20261017-防封禁研究分析.md` §4-P0-1）的直接结论：
        # 上一轮只把闸门加在 `_probe_once` 上，于是 `smoke_*.py` / `verify_*.py`
        # 这些脚本**照样能发真实消息** —— 闸门加在一个函数上，就只保护走那个
        # 函数的调用方，而这里有第二个入口。
        #
        # **产品不受影响**：外壳启动引擎时带 `GAD_ALLOW_LIVE_AUTH=1`
        # （见 `shell/Shared/EngineClient.cs`）。
        # 被拦下的是测试 / 探针 / 一次性脚本 —— 这正是我们要的失败方向。
        if os.environ.get(ALLOW_LIVE_ENV) != "1":
            raise TransportError(
                f"已阻止真实发送（{ALLOW_LIVE_ENV} 未设为 1）。"
                "这是为了不再触发风控 —— 2026-10-17 账号被封的直接原因就是"
                "自动化脚本反复打真实接口。产品外壳会自己带上这个变量；"
                "测试 / 探针 / 一次性脚本不该真发消息，请用桩传输层"
                "（`tests/` 里的 `_StubTransport` 范式）来验逻辑。",
                auth=False,
            )

        if files:
            # 客户端侧只做**宽松兜底**，不当权威校验。
            #
            # 真实限额来自 `model_configs` 的 `file_feature`（可能小于这里的
            # 默认值），但我们没接那个接口。所以这里只拦「明显超限」：
            # 超限时服务端本来也会拒，但那时**已经白烧掉一次上传 PoW**
            # （5 秒量级）。提前拦，省的是用户的实际等待，并给出可读原因。
            #
            # 宁可放过、不可误伤：真值更小时由服务端拒绝，我们照常报错。
            if len(files) > UPLOAD_MAX_COUNT:
                raise TransportError(
                    f"一条消息最多挂 {UPLOAD_MAX_COUNT} 个附件（本次 {len(files)} 个）",
                    config=True,
                )
            too_big = []
            for fp in files:
                try:
                    n = Path(fp).stat().st_size
                except OSError:
                    continue  # 不存在/无权限交给 `_upload_file` 报更准确的错
                if n > UPLOAD_MAX_BYTES:
                    too_big.append(f"{Path(fp).name}（{n / 1048576:.1f} MB）")
            if too_big:
                raise TransportError(
                    f"附件超过 {UPLOAD_MAX_BYTES // 1048576} MB 上限：{'、'.join(too_big)}",
                    config=True,
                )

        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()

        # 这里**不清**停止标志 —— 清零点在 `Agent.run()` 开头（见 `clear_stop`）。
        # 在 `send()` 里清会让「用户在上一轮的工具阶段按停止」这个请求被吞掉。
        #
        # 也不在这里取局部引用：真正用它的是 `_send_blocking_inner`（在 worker
        # 线程里跑），它直接读 `self._cancel`。在这里存个局部变量只是多一层
        # 无意义的间接 —— 而且会误导读者以为这个变量会传到线程里去。

        def emit(kind: str, payload: Any) -> None:
            loop.call_soon_threadsafe(q.put_nowait, (kind, payload))

        fut = loop.run_in_executor(
            None,
            self._send_blocking,
            prompt,
            session,
            bool(extended_thinking),
            # 每轮现取（`None` = 沿用构造时的默认值）。**不能只在构造时定死** ——
            # 那样用户在界面上切了开关，下一轮不会生效，得重启引擎才行。
            bool(self._search_enabled if search_enabled is None else search_enabled),
            list(files) if files else [],
            emit,
        )

        outcome = TurnOutcome()
        while True:
            kind, payload = await q.get()
            if kind == "delta":
                if on_delta is not None:
                    r = on_delta(payload)
                    if inspect.isawaitable(r):
                        await r
            elif kind == "thought":
                if on_thought is not None:
                    r = on_thought(payload)
                    if inspect.isawaitable(r):
                        await r
            elif kind == "meta":
                if payload.get("cid"):
                    session.cid = payload["cid"]
            elif kind == "done":
                outcome = payload
                break
            elif kind == "error":
                await fut  # 让线程真正收尾（异常已在 worker 内捕获）
                raise payload

        await fut
        # 会话三元组滚动更新 —— 必须回填，否则下一轮 parent_message_id 为空，
        # 会在同一个 chat_session 里新建 root（原版树损坏 40003 的根因）。
        # **按 `rid` 判定而非按文本** —— 服务端已分配 assistant 消息 id 就说明
        # 这一轮在服务端已经成立（正文可能为空/被截断），会话状态仍要推进。
        #
        # ⚠️ 这里**只补 `rid` / `rcid` / `last_used_at`，不碰 `turns`**。
        # `turns` 是「轮次计数」，只有幂等的赋值（上面几个）才能安全地两处都写；
        # 计数不是幂等的。原先这里也对 `turns` 做了自增，而
        # `SessionManager.update_from_response()` 又加了一次 —— 同一个
        # `SessionMeta` 对象一轮被加 **2**（2026-10-15 实测：发 1 轮 turns=2）。
        # 后果是 `max_turns_per_session=25` 实际约 **12 轮**就轮换，而且轮换会
        # 悄悄把上下文换掉。**轮次计数的唯一归属方是编排层**（它才知道
        # 「一个用户轮次包含几次往返」），传输层只负责把锚点补回去。
        if outcome.rid:
            session.rid = outcome.rid
            session.rcid = outcome.rcid or session.rcid
            session.last_used_at = time.time()
        return outcome

    # ---------- 阻塞侧（在线程里跑，curl_cffi 是同步 API） ----------

    def _send_blocking(
        self,
        prompt: str,
        session: SessionMeta,
        extended_thinking: bool,
        search_enabled: bool,
        files: list[str],
        emit: Callable[[str, Any], None],
    ) -> TurnOutcome:
        t0 = time.monotonic()
        audit_base: dict = {}
        try:
            outcome = self._send_blocking_inner(
                prompt, session, extended_thinking, search_enabled, files, emit,
                audit_base=audit_base,
            )
        except BaseException as exc:  # noqa: BLE001
            suspended = bool(getattr(exc, "suspended", False))
            if suspended:
                self._account_suspended = True
                self._suspended_detail = str(exc)
                self._snapshot_incident_once("suspended")
            # 异常路径也补审计 —— 失败的那次上游照样看见了。
            if audit_base:
                self._audit_done(
                    audit_base,
                    ok=False,
                    http_status=int(getattr(exc, "status", 0) or 0),
                    error=_audit_error_tag(exc),
                    total_ms=(time.monotonic() - t0) * 1000.0,
                )
            emit("error", exc)
            return TurnOutcome(error=str(exc), http_status=int(getattr(exc, "status", 0) or 0))
        # 流里回的错误（HTTP 200 + `event: hint`）也算失败 —— 熔断层走的就是这条。
        if audit_base:
            self._audit_done(
                audit_base,
                ok=outcome.ok,
                http_status=outcome.http_status,
                error=_audit_error_tag(outcome.error) if outcome.error else "",
                total_ms=(time.monotonic() - t0) * 1000.0,
                ttfb_ms=outcome.ttfb_ms or None,
                pow_ms=outcome.pow_solve_ms,
                pow_cached=outcome.pow_cached,
                cancelled=outcome.cancelled,
            )
        emit("done", outcome)
        return outcome

    def _send_blocking_inner(
        self,
        prompt: str,
        session: SessionMeta,
        extended_thinking: bool,
        search_enabled: bool,
        files: list[str],
        emit: Callable[[str, Any], None],
        *,
        audit_base: dict | None = None,
    ) -> TurnOutcome:
        s = self._require_session()
        token = self._token_sync()

        # 1) 会话：新会话必须由服务端创建
        if not session.cid:
            sid = self._create_session(s, token)
            session.cid = sid
            session.rid = ""
            session.rcid = ""
            emit("meta", {"cid": sid})
        sid = session.cid
        # ★ referer 一律用会话 URL —— **包括首轮**。
        #
        # 这里曾经被我改错过一次，记下来免得再改回去：
        # 我当时的推理是「会话刚建、浏览器还停在 `/`，首轮报会话 URL 是报了个
        # 没访问过的地址」，于是把首轮改成根路径。**这是推断，不是实测，而且
        # 被实测推翻了**：
        #
        #   `docs/evidence/probe-real-request-20261014-234856.json`（全量抓包）
        #   里那条 `completion` 的 `parent_message_id` 是 `null` —— **就是首条
        #   消息** —— 而它的 referer 是
        #   `https://chat.deepseek.com/a/chat/s/43656d83-…`（**会话 URL**）。
        #   同一份抓包里 `chat_session/create` 的 referer 也是会话 URL。
        #
        # 结论：真实网页版是**先 pushState 到会话 URL，再发请求**（SPA 里
        # `document.URL` 早就是会话页了）。所以「首轮用根」反而制造了一个
        # 真实客户端不会出现的组合。**首轮 = 会话 URL。**
        referer = f"{BASE}/a/chat/s/{sid}"

        # 2) 挑战 + 3) 解 PoW —— **优先用后台预取的结果**。
        #
        # 2026-10-18 实测（文档 §2.5）：真实网页版一次并发取 3 个 challenge
        # 并全部解好缓存，第二次发送 0 个 challenge 请求、0 ms 求解。
        # 命中缓存时 `challenge_ms` 与 `pow_ms` 都记 0，用 `pow_cached`
        # 区分 —— 否则「0 秒解完」在日志里看起来像测量坏了。
        t_challenge = time.monotonic()
        cached = self._pow_cache.take() if self._pow_cache is not None else None
        if cached is not None:
            solved = cached
            challenge_ms = 0.0
            pow_cached = True
        else:
            ch = self._fetch_challenge(s, token, referer)
            challenge_ms = (time.monotonic() - t_challenge) * 1000.0
            solved = solve(ch, COMPLETION_PATH, workers=self._workers)
            pow_cached = False
        pow_header = base64.b64encode(
            json.dumps(
                {k: v for k, v in solved.items() if not k.startswith("_")},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).decode("ascii")

        # 3.5) 附件上传 —— **每个文件一次独立的 PoW + 一次 multipart POST**。
        #
        # 放在 completion 的 PoW **之后**：预取命中时那一步是 0 成本，而上传
        # 是同步解（5 秒量级）。若反过来先上传，这几秒可能把预取来的
        # challenge 熬到过期，等于把「0 ms」白扔了。
        #
        # 上传失败**不降级成「不带附件继续发」** —— 用户明确挂了附件，
        # 悄悄丢掉是最坏的结果（模型会答非所问，而界面上看起来一切正常）。
        ref_file_ids: list[str] = []
        uploaded: list[dict] = []
        upload_ms = 0.0
        if files:
            t_upload = time.monotonic()
            for fp in files:
                biz = self._upload_file(
                    s,
                    token,
                    referer,
                    fp,
                    thinking=bool(extended_thinking),
                    model=self._model or "default",
                )
                uploaded.append(biz)
                ref_file_ids.append(str(biz["id"]))
            upload_ms = (time.monotonic() - t_upload) * 1000.0

        # 4) 流式完成
        # 审计落在**真正发出去之前** —— 它数的是「我们向上游发了多少次」，
        # 与请求是否成功无关（失败的那次也照样被上游看见了）。
        # 返回的记录回填给调用方（`_send_blocking`），结束后补第二行。
        rec = self._audit_send(sid, prompt, files=uploaded, upload_ms=upload_ms)
        if audit_base is not None:
            audit_base.update(rec)
        outcome = self._stream_completion(
            s,
            token,
            referer,
            pow_header,
            sid,
            prompt,
            session,
            extended_thinking,
            search_enabled,
            emit,
            ref_file_ids=ref_file_ids,
            # 直接把实例上的 token 传进去，不额外加参数链 ——
            # `_send_blocking` 已经跑在 `run_in_executor` 的线程里，
            # 而这个 token 是 `send()` 在**同一轮开始前**刚 clear 过的那个。
            cancel=self._cancel,
        )
        outcome.upload_ms = round(upload_ms, 1)
        outcome.uploaded_files = [
            {
                "path": str(fp),
                "id": str(biz.get("id") or ""),
                "status": biz.get("status"),
                "file_name": biz.get("file_name", ""),
                "file_size": biz.get("file_size"),
            }
            # `strict=True`：两者长度必然相同（上面每个 fp 都 append 了一项），
            # 不同就说明有 bug —— 静默截断会让「少了一个附件」无从察觉。
            for fp, biz in zip(files or [], uploaded, strict=True)
        ]
        # 把 PoW 的本地耗时回填到 outcome，由编排层上报（见 `agent.py` 的
        # `pow_stats` 事件）。这几个数**只存在于 `solved` 的 `_` 前缀里** ——
        # 上面的 `pow_header` 组装会按 `startswith("_")` 把它们全部滤掉
        # （那是刻意的：它们不属于协议 payload）。所以必须在过滤之后、
        # 从 `solved` 原样取出来，否则它们就此消失。
        #
        # **为什么不在这里直接 `emit`**：本函数的 `emit` 走的是 `send()` 里那个
        # 队列，而 `send()` 的消费循环只认 delta / thought / meta / done / error，
        # 其它 kind 会被**静默丢掉**。挂到 `outcome` 上则天然跟着返回值走到
        # 编排层 —— 与 `search_pages` / `search_sources` 走的是同一条路。
        outcome.challenge_ms = round(challenge_ms, 1)
        # `pow_ms` 衡量的是**这一轮用户等了多久**，不是「这个 challenge
        # 当初算了多久」。命中缓存时必须报 0 —— 否则日志里会出现
        # 「cached=True 但 pow_ms=6646」这种自相矛盾的记录（2026-10-18
        # 端到端实测踩到）。原始求解耗时保留在 `pow_solve_ms` 里作诊断。
        outcome.pow_ms = 0.0 if pow_cached else float(solved.get("_elapsed_ms", 0.0))
        outcome.pow_solve_ms = float(solved.get("_elapsed_ms", 0.0))
        outcome.pow_mode = str(solved.get("_mode", ""))
        outcome.pow_workers = int(solved.get("_workers", 0))
        outcome.pow_trials = int(solved.get("_trials", 0))
        outcome.pow_pool_startup_ms = float(solved.get("_pool_startup_ms", 0.0))
        outcome.pow_pool_created = bool(solved.get("_pool_created", False))
        #: 本轮 PoW 是否**命中后台预取缓存**。命中时 `challenge_ms` / `pow_ms`
        #: 都是 0 —— 这是真实的 0，不是没测到。
        outcome.pow_cached = pow_cached
        return outcome

    def _token_sync(self) -> str:
        if self._token_provider is not None:
            r = self._token_provider()
            if inspect.isawaitable(r):
                raise TransportError("token_provider 是协程，请用同步 provider 或改走 start()", config=True)
            return (r or "").strip()
        return self._token.strip()

    # ---------- 本地审计 ----------

    def _audit_send(
        self,
        sid: str,
        prompt: str,
        *,
        files: list[dict] | None = None,
        upload_ms: float = 0.0,
    ) -> dict:
        """把每一次**真实发送**落一行 append-only 审计（JSONL）。

        **为什么需要它**（2026-10-17 事故复盘 §1.1 / §P0-3）：
        事故里**无法回答**「过去 24 小时到底发了多少、谁发的」——
        `pacing.stats.sends` 是内存计数器、进程一退就没；直连脚本完全不计数。
        于是复盘只能翻 102 份日志重建，而且重建出来的还是**下限**。
        有了这一行，那个问题变成一次 grep：

            grep -c '"phase": "send"' logs/sends-*.jsonl

        **2026-10-19 扩成两行**：本函数只记「发出前」那一行，返回的记录交给
        `_audit_done` 在结束后补第二行（同 `seq`）。原版只有一行、没有结果，
        于是复盘时「这 14 次里哪几次失败了」无从回答 —— 而这正是判断
        「风控是在第几次开始动手」的关键。

        落点由 `GAD_SEND_AUDIT` 覆盖（可指向文件），默认在引擎日志同目录。
        **审计失败绝不能影响发送** —— 底层 `append_jsonl` 已吞掉所有异常。
        """
        self._audit_seq += 1
        return obs.audit_send(
            seq=self._audit_seq,
            cid=sid,
            prompt_chars=len(prompt or ""),
            files=files,
            upload_ms=upload_ms,
        )

    def _audit_done(
        self,
        base: dict,
        *,
        ok: bool,
        http_status: int = 0,
        error: str = "",
        total_ms: float = 0.0,
        ttfb_ms: float | None = None,
        pow_ms: float = 0.0,
        pow_cached: bool = False,
        cancelled: bool = False,
    ) -> None:
        """发送结束后补一行 —— 成败、HTTP 状态、总耗时、PoW 耗时。

        `error` 传**分类后的短文本**（`biz_code=5` / `HTTP 429` / 异常类型），
        完整原文在引擎日志里，审计保持可 grep。
        """
        obs.audit_done(
            base,
            ok=ok,
            http_status=http_status,
            error=error,
            total_ms=total_ms,
            ttfb_ms=ttfb_ms,
            pow_ms=pow_ms,
            pow_cached=pow_cached,
            cancelled=cancelled,
        )

    def _snapshot_incident_once(self, tag: str) -> None:
        """账号被处置时拍一次现场快照（**每次进程生命周期只拍一次**）。

        禁言/封禁期间每一次发送都会失败并抛 `suspended=True`，
        不去重就会在几秒内生成几十个几乎相同的目录。第一次的现场最干净
        （还没有被后续重试污染），所以只保第一次。
        """
        if self._incident_snapshot_done:
            return
        self._incident_snapshot_done = True
        path = obs.snapshot_incident(tag)
        if path:
            # 写 stderr 而不是 stdout：stdout 是 JSON-RPC 通道，多打一行
            # 会让壳的解析器收到一条无法识别的消息。
            with contextlib.suppress(Exception):
                print(f"[incident] 封禁现场快照已保存：{path}", file=sys.stderr)

    def _create_session(self, s, token: str) -> str:
        resp = s.post(
            BASE + SESSION_CREATE_PATH,
            headers=self._headers(token),
            json={},
            timeout=self._timeout,
        )
        self._raise_for_status(resp, "chat_session/create")
        body = resp.json()
        self._raise_for_biz(body, "chat_session/create")
        biz = (body.get("data") or {}).get("biz_data") or {}
        sid = ((biz.get("chat_session") or {}).get("id")) or biz.get("id") or ""
        if not sid:
            raise TransportError(f"create_session 响应里没有 id：{json.dumps(body)[:300]}", fatal=True)
        return sid

    # ---------- 只读：会话列表 / 会话历史 ----------
    #
    # 这两个接口是 2026-10-15 实测发现的，**此前从未被探测过**。
    # 判据：POST 版本回 `405 + allow=GET,HEAD`（API 层 JSON），而未注册路径
    # 会被 SPA 兜底吞成 `200 + HTML` —— 两者可区分，所以能断定路径存在。
    #
    # 它们让「切回旧会话时把服务端历史拉回来」成为可能；同时也是拿到**真实
    # token 用量**（消息里的 `accumulated_token_usage`）的唯一途径 ——
    # SSE 流里**没有** usage 字段（见 `base.TurnOutcome`）。

    async def list_sessions(self, count: int = 30) -> list[dict]:
        """列出账号下的会话。只读：不建会话、不发消息。

        ⚠️ **这个接口没有可用的分页**（2026-10-18 实测，T-20）。
        详见 `SESSION_LIST_MAX_COUNT` 与 `_list_sessions_blocking` 的说明。
        """
        return await asyncio.to_thread(self._list_sessions_blocking, count)

    def _list_sessions_blocking(self, count: int) -> list[dict]:
        """拉会话列表。

        ## 2026-10-18 实测的两个硬约束（T-20，证据见
        `docs/evidence/session-paging-20261018.md`）

        **① `updated_at` 游标无效。**
        带上「本页最旧那条的 updated_at」再请求，返回的是**完全相同的一批**
        （10 条全重复，且全部 `updated_at >= 游标`）。服务端**静默忽略**
        这个参数 —— 不报错、不警告，返回照旧。这是本类接口最典型的陷阱。

        **② `count > 100` 返回空列表，不是报错。**
        实测边界：

        | count | 返回 |
        |---|---|
        | 1 | **0 条**（异常，见下） |
        | 2–5 | 与 count 相同 |
        | 100 | 74 条（账号实际总数） |
        | 101 / 120 / 150 / 199 / 200 / 500 | **0 条** |

        失败形态是「空列表 + code=0」—— 上层会把它读成「账号下没有会话」，
        而不是「参数超限」。**这是最危险的一种失败**：它让「功能坏了」
        看起来像「一切正常」。

        所以这里**主动夹住上限并显式报错**，绝不让超限请求发出去。

        `count=1` 也返回 0 条，原因不明（不是超限，1 远小于 100）。
        可能是服务端把 `count=1` 当成了「不分页」的特殊值。既然没有证据，
        这里不猜测 —— 只把下限设成 2（实测 2 正常），并在文档里记下这个异常。
        """
        if count < SESSION_LIST_MIN_COUNT or count > SESSION_LIST_MAX_COUNT:
            raise TransportError(
                f"会话列表条数必须在 {SESSION_LIST_MIN_COUNT}–"
                f"{SESSION_LIST_MAX_COUNT} 之间（收到 {count}）。"
                f"超出上限时服务端会**静默返回空列表**而不是报错，"
                f"所以这里提前拦住，避免「读不到会话」被误判成「没有会话」。",
                config=True,
            )
        s = self._require_session()
        token = self._token_sync()
        resp = s.get(
            BASE + SESSION_LIST_PATH,
            headers=self._headers(token),
            params={"count": int(count)},
            timeout=self._timeout,
        )
        body = self._read_json(resp, "chat_session/fetch_page")
        biz = (body.get("data") or {}).get("biz_data") or {}
        return biz.get("chat_sessions") or []

    async def fetch_history(self, chat_session_id: str) -> dict:
        """拉某个会话的完整历史。只读。

        返回 `biz_data`，形如
        `{chat_session: {...}, chat_messages: [...], cache_control, cache_reset_at}`。
        `chat_session.current_message_id` 就是**续接锚点**（等于末条 message_id）。
        """
        return await asyncio.to_thread(self._fetch_history_blocking, chat_session_id)

    def _fetch_history_blocking(self, cid: str) -> dict:
        if not cid:
            raise TransportError("fetch_history 需要 chat_session_id", config=True)
        s = self._require_session()
        token = self._token_sync()
        resp = s.get(
            BASE + HISTORY_PATH,
            headers=self._headers(token, f"{BASE}/a/chat/s/{cid}"),
            params={"chat_session_id": cid},
            timeout=self._timeout,
        )
        body = self._read_json(resp, "chat/history_messages")
        return (body.get("data") or {}).get("biz_data") or {}

    # ---------- 朗读（TTS）控制面 ----------
    #
    # 三个方法都不碰音频流：音频由前端直连 `wss://chat.deepseek.com/api/v0/chat/tts/`
    # 拿，引擎只签票。理由见 `TTS_TICKET_PATH` 上方的注释。
    #
    # ⚠️ 这三个方法会**真的打上游接口**，与 `ALLOW_LIVE_ENV` 那套闸门无关 ——
    #    它们是产品功能，不是探测。但正因为如此，调用方（`AgentHost`）必须
    #    接受「用户点一次朗读就消耗一次配额」这个事实，不要拿它做轮询或预热。

    async def fetch_tts_ticket(self, chat_session_id: str, message_id: str) -> str:
        """签一张一次性朗读票据。

        `chat_session_id` / `message_id` **不参与这个请求**，但这里仍然要求传 ——
        因为票据的可用范围由服务端按「这个 scope 能访问哪些资源」决定，
        而调用方必须先自己确认「这条消息确实属于当前账号的当前会话」，
        否则会拿到一张能签但用不了的票（连流时回 `code 2 INVALID_INPUT`）。

        所以签名里的两个参数是**前置条件的显式化**，不是冗余。
        """
        return await asyncio.to_thread(
            self._fetch_tts_ticket_blocking, chat_session_id, message_id
        )

    def _fetch_tts_ticket_blocking(self, cid: str, mid: str) -> str:
        if not cid:
            raise TransportError("fetch_tts_ticket 需要 chat_session_id", config=True)
        if not mid:
            raise TransportError("fetch_tts_ticket 需要 message_id", config=True)
        s = self._require_session()
        token = self._token_sync()
        resp = s.post(
            BASE + TTS_TICKET_PATH,
            headers=self._headers(token, f"{BASE}/a/chat/s/{cid}"),
            json={"scope": "tts"},
            timeout=self._timeout,
        )
        body = self._read_json(resp, "auth/ticket")
        ticket = str(((body.get("data") or {}).get("biz_data") or {}).get("ticket") or "")
        if not ticket:
            # 结构变了（不是空字符串的正常情况）—— 报出来，别让下游拿空票去连流，
            # 那样失败会发生在 WS 握手阶段，错误信息离真正的原因很远。
            raise TransportError(
                f"auth/ticket 没返回 ticket：{json.dumps(body)[:300]}", config=True
            )
        return ticket

    async def list_tts_voices(self) -> list[dict]:
        """列出可用音色。只读，**不消耗朗读配额**。"""
        return await asyncio.to_thread(self._list_tts_voices_blocking)

    def _list_tts_voices_blocking(self) -> list[dict]:
        s = self._require_session()
        token = self._token_sync()
        resp = s.get(
            BASE + TTS_VOICES_PATH,
            headers=self._headers(token),
            timeout=self._timeout,
        )
        body = self._read_json(resp, "chat/tts/voices")
        biz = (body.get("data") or {}).get("biz_data") or {}
        raw = biz.get("voices") or biz.get("voice_list") or []

        # **用户当前选的音色**。2026-10-19 实测确认它在 `biz_data` 顶层，
        # 不在每个 voice 对象里。
        #
        # 这个字段曾漏掉，代价很大：前端只能拿每个音色的 `is_default` 判断
        # 「谁被选中」，而 `is_default` 是**出厂默认**（永远只有 mira 为 true），
        # 不是当前选择。结果是切换明明写成功了，面板却永远显示 mira 选中 ——
        # 刷新一次就"变回" mira，用户会以为功能坏了。
        #
        # 两者的区别：`default_voice_id` = 出厂默认（mira）；
        # `current_voice_id` = 用户选择（可被 `POST /chat/tts/voice` 改）。
        current = str(biz.get("current_voice_id") or "")

        out: list[dict] = []
        for v in raw:
            if not isinstance(v, dict):
                continue
            vid = str(v.get("voice_id") or v.get("id") or "")
            if not vid:
                continue
            out.append(
                {
                    "voice_id": vid,
                    # 名称与描述都是 i18n 字典（`{"zh": "贝壳", "en": "Mira"}`）。
                    # 直接取 `v["name"]` 永远是空 —— 那样界面上显示的是
                    # `mira` 而不是「贝壳」。优先中文，回退英文，再回退 id。
                    "name": _i18n_pick(v.get("name_i18n"), v.get("name"), vid),
                    "desc": _i18n_pick(
                        v.get("description_i18n"), v.get("desc") or v.get("description"), ""
                    ),
                    "gender": str(v.get("gender") or ""),
                    "languages": list(v.get("languages") or []),
                    # 出厂默认（不可改，永远 mira）。保留是为了让 UI 能标注
                    # 「默认音色」，**不能**拿它当选中态。
                    "is_default": bool(v.get("is_default")),
                    # 当前选中 —— 界面上「打勾」只能看这个。
                    "is_current": bool(current) and vid == current,
                    # 试听直链 —— 网页版就用它，我们照搬，省一次合成。
                    "demo_urls": dict(v.get("demo_urls") or {}),
                }
            )
        return out

    async def set_tts_voice(self, voice_id: str) -> None:
        """切换朗读音色。**写操作**：改的是账号级偏好，网页版也跟着变。"""
        await asyncio.to_thread(self._set_tts_voice_blocking, voice_id)

    def _set_tts_voice_blocking(self, voice_id: str) -> None:
        if not voice_id:
            raise TransportError("set_tts_voice 需要 voice_id", config=True)
        s = self._require_session()
        token = self._token_sync()
        resp = s.post(
            BASE + TTS_VOICE_PATH,
            headers=self._headers(token),
            json={"voice_id": voice_id},
            timeout=self._timeout,
        )
        self._read_json(resp, "chat/tts/voice")

    # ---------- 写：会话生命周期 ----------

    async def create_session(self) -> str:
        """显式新建一个空会话，返回 id。

        与 `send()` 内部的自动建会话走**同一个** `_create_session` ——
        请求形态只有一种，不该有两份实现。

        ⚠️ 新建出来的会话是「空会话」：`title=null, title_type="WIP"`，
        **不会出现在 `list_sessions` 的返回里**（列表只列有内容的会话）。
        要确认它真的存在，得用 `fetch_history(sid)`（`biz_code=0` 即存在）。
        """
        return await asyncio.to_thread(self._create_session_sync)

    def _create_session_sync(self) -> str:
        s = self._require_session()
        token = self._token_sync()
        return self._create_session(s, token)

    async def delete_session(self, chat_session_id: str) -> None:
        """永久删除一个服务端会话。

        **不可逆**。上层必须先做二次确认 —— 传输层只执行，不交互。

        幂等：服务端对不存在的 id 也回 `biz_code=0`。所以本方法
        「不抛异常」只意味着「服务端接受了这次调用」，不意味着
        「确实删掉了一条存在的会话」。要验证得靠 `fetch_history` 复查。
        """
        await asyncio.to_thread(self._delete_session_blocking, chat_session_id)

    def _delete_session_blocking(self, cid: str) -> None:
        if not cid:
            # 空 id 绝不发出去 —— 少数接口把「缺省」解释成「全部」，
            # 那是「一条命令清空账号」。宁可在这里炸。
            raise TransportError("delete_session 需要 chat_session_id", config=True)
        s = self._require_session()
        token = self._token_sync()
        resp = s.post(
            BASE + SESSION_DELETE_PATH,
            headers=self._headers(token, f"{BASE}/a/chat/s/{cid}"),
            json={"chat_session_id": cid},
            timeout=self._timeout,
        )
        # 走统一的 `_read_json`：状态码 + `code` + `biz_code` 三层都校验，
        # 非 JSON 会被翻成「路径可能已变更」，不会静默吞掉。
        self._read_json(resp, "chat_session/delete")

    def _read_json(self, resp, where: str) -> dict:
        """校验状态码 + 业务码，并保证返回 dict。

        **非 JSON 一律当成「路径已失效」**：接口没注册时服务端会拿 SPA 首页
        （`200 + HTML`）兜底，那时 `resp.json()` 抛的是 `ValueError` ——
        报错里看不出「接口没了」，只会让人以为是自己解析写错了。
        """
        self._raise_for_status(resp, where)
        try:
            body = resp.json()
        except ValueError as exc:
            raise TransportError(
                f"{where} 返回的不是 JSON（路径可能已变更）：{resp.text[:200]!r}",
                config=True,
            ) from exc
        self._raise_for_biz(body, where)
        return body

    def _fetch_challenge(
        self,
        s,
        token: str,
        referer: str,
        target_path: str = COMPLETION_PATH,
    ) -> Challenge:
        """取一个 PoW challenge。

        `target_path` **不是装饰**：它参与 challenge 的签发，也参与
        `x-ds-pow-response` 的签名内容。上传文件要取的是
        `target_path=UPLOAD_PATH` 的那一份，拿 `COMPLETION_PATH` 的来用
        会被服务端判签名不符（HTTP 403，且没有可读的业务错误）。

        默认值保持 `COMPLETION_PATH` —— 后台预取（`_prefetch_one`）与
        健康探测都走默认，它们的语义就是「为下一次发送备货」。
        """
        resp = s.post(
            BASE + CHALLENGE_PATH,
            headers=self._headers(token, referer),
            json={"target_path": target_path},
            timeout=self._timeout,
        )
        self._raise_for_status(resp, "create_pow_challenge")
        body = resp.json()
        self._raise_for_biz(body, "create_pow_challenge")
        biz = (body.get("data") or {}).get("biz_data") or {}
        try:
            return Challenge.from_api(biz["challenge"])
        except (KeyError, ValueError) as exc:
            raise TransportError(f"challenge 结构异常：{exc}", fatal=True) from exc

    def _upload_file(
        self,
        s,
        token: str,
        referer: str,
        path: str,
        *,
        thinking: bool = False,
        model: str = "default",
    ) -> dict:
        """上传一个文件，返回 `biz_data`（含 `id` / `status` / `file_name`）。

        协议来源：`docs/20261018-文件上传交接.md` §2（逆向自
        `main.a0ce88b684.js`）。三条最容易踩的：

        1. **上传有自己的 PoW**，`target_path=UPLOAD_PATH`，与发消息那份
           不能互换。这里**同步解、不进 `PowCache`** —— 上传频率远低于
           发送，为它维护第二个缓存池不值得（交接文档 §3.1.4 选 A）。
        2. **不要手写 `content-type`**。curl 的 MIME API 需要自己生成带
           boundary 的头；`_headers()` 默认塞的是 `application/json`，
           不删就会发出一个没有 boundary 的 multipart。
        3. **`files=` 参数在 curl_cffi 里不可用**（0.16.3 的
           `utils.py:785` 直接 `NotImplementedError`），必须用
           `CurlMime` + `multipart=`。交接文档里那段示例代码照抄会炸。

        「Text extraction only」：服务端只提取文本，不是原文件存储。
        发 PDF / 图片得到的是提取后的文本。
        """
        from curl_cffi import CurlMime

        p = Path(path)
        if not p.is_file():
            raise TransportError(f"附件不存在或不是文件：{path}", config=True)
        data = p.read_bytes()
        if not data:
            # 空文件上传上去也只会得到「No text extracted」，还白烧一次 PoW。
            raise TransportError(f"附件是空文件，没有内容可提取：{path}", config=True)

        # ① 上传专用的 PoW（target_path 不同，不能复用 completion 的缓存）
        ch = self._fetch_challenge(s, token, referer, target_path=UPLOAD_PATH)
        solved = solve(ch, UPLOAD_PATH, workers=self._workers)
        pow_header = base64.b64encode(
            json.dumps(
                {k: v for k, v in solved.items() if not k.startswith("_")},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).decode("ascii")

        # ② 头：PoW + 三个上传专属头
        headers = self._headers(token, referer)
        headers["x-ds-pow-response"] = pow_header
        headers["x-thinking-enabled"] = "1" if thinking else "0"
        headers["x-model-type"] = model
        headers["x-file-size"] = str(len(data))
        # ★ 必须删 —— 见本函数 docstring 第 2 条。
        headers.pop("content-type", None)

        # ③ body：multipart/form-data，字段名是 `file`（不是 files/upload）
        mp = CurlMime()
        try:
            mp.addpart(name="file", data=data, filename=p.name)
            resp = s.post(
                BASE + UPLOAD_PATH,
                headers=headers,
                multipart=mp,
                timeout=self._timeout,
            )
        finally:
            # `close()` 只是释放 libcurl 的 multipart 句柄，失败不影响已经
            # 发出去的请求结果 —— 吞掉是安全的，但不能让它盖住真正要抛的异常。
            with contextlib.suppress(Exception):
                mp.close()

        self._raise_for_status(resp, "upload_file")
        try:
            body = resp.json()
        except ValueError as exc:
            raise TransportError(
                f"upload_file 返回的不是 JSON（路径可能已变更）：{resp.text[:200]!r}",
                config=True,
            ) from exc
        self._raise_for_biz(body, "upload_file")

        biz = (body.get("data") or {}).get("biz_data") or {}
        if not biz.get("id"):
            # 拿到 `code:0` 却没有 id，说明响应结构与逆向结论不一致 ——
            # 报结构异常而不是「上传失败」，免得下次有人去查网络。
            raise TransportError(
                f"upload_file 响应缺 id（结构可能已变）：{json.dumps(body)[:300]}",
                config=True,
            )
        return biz

    def _prefetch_one(self):
        """后台预取一个 challenge 并解好（供 `PowCache` 调用）。

        **为什么用独立 session**：主 session（`self._session`）是给 `send()`
        用的。后台线程并发碰它既有线程安全问题，一次失败还可能污染主
        session 的连接状态。新建/关闭的成本（约 150–1500 ms，取决于是否
        复用 TLS）远小于一次 5 秒的求解，不值得省。

        **referer 用站点根**：`create_pow_challenge` 的 referer 只是 HTTP 头，
        不参与 challenge 内容；预取时还不知道未来会话的 URL。真实网页版
        取 challenge 时的 referer 是会话 URL，但那是它已经在会话页里的
        自然结果 —— 这里用根路径不制造矛盾（我们每次发送仍然用会话 URL）。
        """
        from curl_cffi import requests as creq

        token = self._token_sync()
        if not token:
            return None

        # 预取用的 session 也套 trace —— **后台静默打的请求最需要被记录**：
        # 用户看不见它，但它照样是「这个 IP 在上游留下的足迹」。
        s = obs.RecordingSession(creq.Session(impersonate=IMPERSONATE))
        try:
            ch = self._fetch_challenge(s, token, BASE + "/")
        finally:
            with contextlib.suppress(Exception):
                s.close()

        solved = solve(ch, COMPLETION_PATH, workers=self._workers)

        # `expire_at` 实测是毫秒时间戳（13 位，如 1792254708602）。
        # 留一道兼容：万一是秒级（10 位）也能正确换算。
        expire_ts = float(ch.expire_at)
        if expire_ts > 1e11:
            expire_ts /= 1000.0
        return expire_ts, solved

    def _stream_completion(
        self,
        s,
        token: str,
        referer: str,
        pow_header: str,
        sid: str,
        prompt: str,
        session: SessionMeta,
        extended_thinking: bool,
        search_enabled: bool,
        emit: Callable[[str, Any], None],
        *,
        ref_file_ids: list[str] | None = None,
        cancel: _CancelToken | None = None,
    ) -> TurnOutcome:
        # ★ `accept` **不要改成 `text/event-stream`**。
        #
        # 2026-10-17 23:18 实测（`completion` 30 个头）：真实网页版的 `accept` 就是
        # `*/*` —— 页面用 `fetch` + `ReadableStream` 读 SSE，只有 `EventSource`
        # 才会把 accept 设成 `text/event-stream`。我们此前发 `text/event-stream`，
        # 等于声称「我是 EventSource」，而真实客户端从不用它。
        #
        # `origin` 只有 `completion` 有（同批抓包里 `history_messages` 没有）。
        h = self._headers(token, referer, with_origin=True)
        h["x-ds-pow-response"] = pow_header

        parent_raw = session.parent_message_id  # 新会话为 None
        # **必须是 int** —— 传字符串 "2" 会被服务端拒（实测第二轮 HTTP 非 200）。
        #
        # 非空但**非数字**的 `rid` 是「会话状态已损坏」的信号。原先这里写的是
        # `int(x) if x.isdigit() else None` —— 它把这种情况**静默**降级成
        # `parent=None`，于是服务端会在同一个 `chat_session` 里另起一棵 root 树：
        # 上下文悄悄断掉，界面一切正常，只是模型突然「失忆」。
        # 这种「静默断链」比报错恶劣得多（用户只会得出「消息发丢了」这个错误结论），
        # 所以改为**明确抛错**。`config=True` 让它不计入 RPC 错误计数 —— 否则会把
        # 真正的原因（锚点坏了）埋在「RPC 连续失败」后面。
        if parent_raw is not None and not str(parent_raw).isdigit():
            raise TransportError(
                f"会话续接锚点异常：parent_message_id={parent_raw!r} 不是数字，"
                "本地记录的会话状态可能已损坏。重新同步该会话即可修复"
                "（切到该对话后点「同步」，或对网页版会话重新「接管」）。",
                config=True,
            )
        parent = int(parent_raw) if parent_raw else None
        payload = {
            "chat_session_id": sid,
            "parent_message_id": parent,
            "model_type": None if parent else (self._model or "default"),
            "prompt": prompt,
            # **必须是数组**，元素是 `upload_file` 响应里的 `biz_data.id`。
            # 没有附件时也要带空数组（真实客户端一直带这个字段）。
            "ref_file_ids": list(ref_file_ids or []),
            "thinking_enabled": bool(extended_thinking),
            "search_enabled": bool(search_enabled),
            "action": None,
            "preempt": False,
        }

        acc = _Acc()
        parser = _SseParser(acc, emit)
        # 调试用：`DS_SSE_DUMP=<dir 或 path>` 时把原始 SSE 行原样落盘。
        # 每次请求单独一个文件（否则第二轮会截断第一轮的转储）。
        # 增量格式未经充分实测，出问题先看这个文件，别靠猜。
        dump = None
        dump_path = os.environ.get("DS_SSE_DUMP", "")
        if dump_path:
            try:
                p = Path(dump_path)
                if p.is_dir() or not p.suffix:
                    p.mkdir(parents=True, exist_ok=True)
                    p = p / f"sse-{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 1000}.txt"
                # 不用 `with`：这份 dump 要跨下面整段流式循环存活，并在
                # `finally` 里关闭（见 1853 行附近）。改成 `with` 会把整个
                # 流式解析块再缩进一层，收益为零。
                dump = open(p, "w", encoding="utf-8")  # noqa: SIM115
            except OSError:
                dump = None
        # 首字节耗时 —— 从发出到拿到响应对象。这是判断「服务端愿不愿意理你」
        # 的关键指标（过载时会挂 ~68 秒才回），而总耗时里大部分是生成时间。
        t_req = time.monotonic()
        ttfb_ms = 0.0
        http_status = 0
        try:
            with s.stream(
                "POST", BASE + COMPLETION_PATH, headers=h, json=payload, timeout=self._timeout
            ) as resp:
                ttfb_ms = (time.monotonic() - t_req) * 1000.0
                http_status = int(getattr(resp, "status_code", 0) or 0)
                if resp.status_code != 200:
                    # curl_cffi 的 Response 没有 .read()，用 .content
                    raw = resp.content or b""
                    self._raise_for_status_text(resp.status_code, raw, "chat/completion")
                for raw_line in resp.iter_lines():
                    # ★ 停止生成的实际生效点。
                    #
                    # 每行问一次 —— 这条路径一轮要跑几千次，所以用属性读取
                    # 而不是 `Event.is_set()`（见 `_CancelToken` 的注释）。
                    # `break` 之后走 `finally` 关掉流、再由 `send()` 那边
                    # 把已收到的正文当作正常结果返回：**已生成的内容不丢**，
                    # 这是用户点「停止」时的合理预期（不是「取消 = 什么都没发生」）。
                    if cancel is not None and cancel.requested:
                        acc.finish_reason = "cancelled"
                        break
                    if not raw_line:
                        continue
                    line = (
                        raw_line.decode("utf-8", "replace")
                        if isinstance(raw_line, bytes)
                        else raw_line
                    )
                    if dump is not None:
                        dump.write(line + "\n")
                    # ★ 行解析统一走 `feed_line` —— 浏览器档位
                    #   （`chrome_cdp.py`）用的是同一个入口，两边不会漂移。
                    if not parser.feed_line(line):
                        break
        finally:
            if dump is not None:
                dump.close()

        text = acc.reply_text()
        outcome = TurnOutcome(
            text=text,
            thoughts=acc.thought_text(),
            cid=sid,
            rid=str(acc.response_message_id) if acc.response_message_id else "",
            rcid=str(acc.request_message_id) if acc.request_message_id else "",
            # 搜索痕迹单独带出 —— 它不属于正文（见 `_note_search`）
            search_pages=acc.search_pages,
            search_sources=len(acc.search_results),
            # 流读到一半被用户停掉 —— 与「服务端自己结束」是两件事，
            # 上层要能区分（前者不该计入熔断、不该报错）。
            cancelled=acc.finish_reason == "cancelled",
            http_status=http_status,
            ttfb_ms=round(ttfb_ms, 1),
        )
        if not text:
            if acc.finish_reason == "cancelled":
                # 用户主动停止，且**一个字都还没收到**。这不是错误 ——
                # 报成 `error` 会让熔断层把它计成一次 RPC 失败，
                # 连点几次「停止」就足以把传输层降级掉。
                outcome.cancelled = True
            elif acc.error:
                # 服务端给了明确原因（如 Server busy / generation_timeout），别丢
                suffix = f"（finish_reason={acc.finish_reason}）" if acc.finish_reason else ""
                outcome.error = f"服务端错误：{acc.error}{suffix}"
            elif acc.status:
                outcome.error = f"未收到正文（status={acc.status}）"
            else:
                outcome.error = "未收到正文（流为空）"
        return outcome

    # ---------- 错误分类 ----------

    @staticmethod
    def _raise_for_biz(body: dict, where: str) -> None:
        if body.get("code") not in (0, None):
            raise TransportError(f"{where} 业务错误：{json.dumps(body)[:300]}", fatal=False)
        d = body.get("data")
        if isinstance(d, dict) and d.get("biz_code"):
            suspended, detail = describe_biz_failure(d)
            raise TransportError(
                (f"{where} 账号被处置（禁言/封禁）：" if suspended else f"{where} 业务错误 ")
                + f"biz_code={d['biz_code']}：{detail}",
                fatal=False,
                auth=suspended,
                suspended=suspended,
            )

    @staticmethod
    def _raise_for_status_text(status: int, raw: bytes, where: str) -> None:
        head = raw[:300]
        # `status=` 让审计与熔断层拿到结构化状态码，不必再从文本里正则抠。
        if status in (401, 403):
            raise TransportError(f"{where} 未认证：HTTP {status} {head!r}", auth=True, status=status)
        if status == 429:
            raise TransportError(f"{where} 限流：HTTP 429 {head!r}", fatal=False, status=status)
        raise TransportError(f"{where} 失败：HTTP {status} {head!r}", fatal=False, status=status)

    def _raise_for_status(self, resp, where: str) -> None:
        if resp.status_code == 200:
            return
        try:
            raw = resp.content or b""
        except Exception:  # noqa: BLE001
            raw = b""
        self._raise_for_status_text(resp.status_code, raw, where)
