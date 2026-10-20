"""L1 身份层 —— 取得、保存、**验证** DeepSeek 的 Bearer token。

**这是全项目唯一需要浏览器的环节。** 取完即可关掉，对话全程不再需要它。

为什么绕不开
------------

DeepSeek 的凭据存在 `localStorage.userToken`（**不是 cookie**），而 `localStorage`
只有页面里的 JS 能读到。原版（`deepseek-mac`）靠 WebView2 的 `ExecuteScriptAsync`
读它 —— 那是整个项目**唯一**的 WebView2 依赖点。要做「原生前台、不跑网页版」，
就必须把这唯一一处也拿掉。本模块用 CDP 顶替（见 `browser_login.py`）。

四条取凭据的路径（按优先级）
----------------------------

1. **显式传入** —— 调用方直接给 token（测试 / 高级用法）
2. **本地缓存文件** —— `engine/.token`（无人值守的长期任务靠它）
3. **CDP 自动读取** —— 连一个已开调试端口的浏览器（开发期最常用）
4. **拉起浏览器登录** —— 起独立 profile 的 Chrome，等登录，读走，**取完即关**
   （生产形态；这是「原生前台」里唯一会弹窗的一步）

外加一条**手动粘贴兜底**：浏览器方案彻底失败时，用户可以从 DevTools 里把
token 复制出来交给 `save_manual_token()`。

★ 为什么必须「验证」而不只是「读取」
------------------------------------

DeepSeek 的 `401` **无法区分「token 过期」与「账号被处置」**（见 `circuit.py`）。
如果不验就存，用户会看到「登录成功」却在第一次对话时炸 —— 而且分不清原因。
`validate_token()` 用一次极轻的 `create_pow_challenge`（不建会话、不发消息）
把这件事在**最早的时刻**问清楚，并给出可操作的建议。

> **安全提示**：`.token` 是**明文凭据**，等同账号密码。它只落本机项目目录，
> 不进 Vault、不进任何记忆文件、不提交到版本库。失效后删掉重取即可。
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from dataclasses import dataclass
from pathlib import Path

from .browser_login import (
    DEFAULT_PORT,
    BrowserLoginError,
    BrowserLoginSession,
    normalize_token,
    read_token_and_label,
    read_token_from_cdp,
    read_token_from_cdp_async,
)

DEFAULT_CDP_PORT = DEFAULT_PORT
DEFAULT_TOKEN_FILE = Path(__file__).resolve().parents[1] / ".token"

# 向后兼容：老调用方（探针）用的是这个异常名
IdentityError = BrowserLoginError


# ---------- 存取 ----------


def save_token(token: str, path: Path = DEFAULT_TOKEN_FILE) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(normalize_token(token), encoding="utf-8")
    return path


def load_token(path: Path = DEFAULT_TOKEN_FILE) -> str:
    if not path.exists():
        return ""
    return normalize_token(path.read_text(encoding="utf-8"))


def clear_token(path: Path = DEFAULT_TOKEN_FILE) -> bool:
    if path.exists():
        path.unlink()
        return True
    return False


def save_manual_token(raw: str, path: Path = DEFAULT_TOKEN_FILE) -> tuple[Path, str]:
    """手动粘贴兜底。

    用户在 DevTools 里看到的形态五花八门，`normalize_token` 负责归一：
    裸串 / `{"value": "..."}` / 带引号 / 带 `Bearer ` 前缀 都能吃。
    返回 `(落盘路径, 归一后的 token)`，调用方可以据此回显确认。
    """
    token = normalize_token(raw)
    if not token:
        raise IdentityError("粘贴内容里没有可识别的 token")
    if len(token) < 20:
        raise IdentityError(f"token 形态异常（len={len(token)}）—— 请确认复制完整")
    return save_token(token, path), token


# ---------- 验证 ----------


async def validate_token_async(token: str, *, timeout: float = 15.0) -> tuple[bool, str]:
    """`validate_token` 的协程形态。

    **在 `Engine.handle()` 里必须用这个** —— handle 是 `async def`，跑在事件
    循环里；在里面调同步版会触发 `asyncio.run()` 的「cannot be called from a
    running event loop」，而那个异常被下面那个 `except` 兜住之后，表现出来
    只是「验证不通过：……」——**看着像凭据坏了，其实是代码坏了**。
    """
    from .transport.deepseek_rpc import DeepSeekRpcTransport

    tp = DeepSeekRpcTransport(token=token, timeout=timeout)
    try:
        await tp.start()
        try:
            return await tp.verify_credentials()
        finally:
            await tp.stop()
    except Exception as exc:  # noqa: BLE001
        return False, f"验证失败：{exc}"


def validate_token(token: str, *, timeout: float = 15.0) -> tuple[bool, str]:
    """用一次极轻的请求验证 token 是否仍然有效。返回 `(ok, 说明)`。

    **不创建会话、不发消息** —— 只调 `create_pow_challenge`。
    这条链路同时验证了三件事：token 有效、TLS 指纹被接受、请求头被接受。

    同步入口，给同步调用方（探针 / `ensure_token` / 一次性脚本）用。
    它自己判断当前有没有事件循环：
      * 没有 → 直接 `asyncio.run`；
      * 有   → 丢到独立线程里跑（那条线程没有正在跑的循环，`asyncio.run` 合法）。
    绝不嵌套循环，也绝不让调用方因为「在不在循环里」而拿到不同结果。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(validate_token_async(token, timeout=timeout))

    box: dict[str, tuple[bool, str]] = {}

    def _worker() -> None:
        try:
            box["r"] = asyncio.run(validate_token_async(token, timeout=timeout))
        except Exception as exc:  # noqa: BLE001
            box["r"] = (False, f"验证失败：{exc}")

    t = threading.Thread(target=_worker, name="validate-token", daemon=True)
    t.start()
    t.join(timeout + 10.0)
    return box.get("r", (False, "验证超时（独立线程未在限期内返回）"))


# ---------- 解析 ----------


@dataclass
class TokenResult:
    token: str
    source: str
    valid: bool = False
    detail: str = ""
    account_label: str = ""
    saved_to: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.token) and self.valid


def resolve_token(
    *,
    explicit: str = "",
    token_file: Path = DEFAULT_TOKEN_FILE,
    cdp_port: int | None = DEFAULT_CDP_PORT,
) -> tuple[str, str]:
    """按「显式 → 文件 → CDP」顺序取 token，返回 `(token, 来源说明)`。

    **不做验证**（保持轻量，探针每轮都调它）。要验证请用 `ensure_token()`。
    """
    if normalize_token(explicit):
        return normalize_token(explicit), "显式传入"
    cached = load_token(token_file)
    if cached:
        return cached, f"文件 {token_file.name}"
    if cdp_port:
        return read_token_from_cdp(cdp_port), f"CDP:{cdp_port}"
    raise IdentityError("无可用 token（既未传参、也无缓存、且未启用 CDP）")


def ensure_token(
    *,
    explicit: str = "",
    token_file: Path = DEFAULT_TOKEN_FILE,
    cdp_port: int | None = None,
    profile_dir: Path | None = None,
    browser_path: str = "",
    allow_browser: bool = True,
    validate: bool = True,
    login_timeout_sec: float = 300.0,
    on_status=None,
) -> TokenResult:
    """**生产形态的入口**：拿到一个「已确认可用」的 token。

    流程：
      1. 显式 / 缓存 → 有就验一次
      2. 验证不过（或没有）→ 拉起独立 profile 的浏览器，等登录，读走，**关掉浏览器**
      3. 再验一次；仍不过则如实返回失败与原因（不假装成功）

    `on_status(kind, payload)` 会在关键节点回调，供界面显示进度：
      `launch` / `waiting` / `settling` / `got_token` / `validated` / `closing` / `failed`

    `allow_browser=False` 时只走前两步 —— 给「无人值守、不能弹窗」的场景用。
    """

    def status(kind: str, payload: dict | None = None) -> None:
        if on_status:
            with contextlib.suppress(Exception):
                on_status(kind, payload or {})

    # ---- 1) 显式 / 缓存 ----
    token = normalize_token(explicit) or load_token(token_file)
    source = "显式传入" if normalize_token(explicit) else (f"文件 {token_file.name}" if token else "")

    if token and validate:
        ok, detail = validate_token(token)
        if ok:
            status("validated", {"source": source, "detail": detail})
            return TokenResult(token=token, source=source, valid=True, detail=detail)
        status("failed", {"source": source, "detail": detail})
        if not allow_browser:
            return TokenResult(
                token=token,
                source=source,
                valid=False,
                detail=f"缓存 token 验证不过：{detail}（且未允许拉起浏览器）",
            )
    elif token:
        return TokenResult(token=token, source=source, valid=True, detail="未验证（validate=False）")

    # ---- 2) 拉起浏览器 ----
    if not allow_browser:
        return TokenResult(
            token="", source="", valid=False, detail="无可用 token，且未允许拉起浏览器"
        )

    sess = BrowserLoginSession(
        profile_dir=profile_dir or _default_profile_dir(),
        port=cdp_port if cdp_port is not None else DEFAULT_CDP_PORT,
        browser_path=browser_path,
    )
    try:
        info = sess.start()
        status("launch", info)
    except BrowserLoginError as exc:
        status("failed", {"detail": str(exc)})
        return TokenResult(token="", source="浏览器", valid=False, detail=str(exc))

    async def _wait() -> dict:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + login_timeout_sec
        last: dict = {"state": "waiting"}
        while loop.time() < deadline:
            last = await sess.poll()
            if last["state"] in ("ready", "no_browser", "error"):
                return last
            status(last["state"], last)
            await asyncio.sleep(1.0)
        last = dict(last)
        last["state"] = "timeout"
        return last

    try:
        result = asyncio.run(_wait())
    except Exception as exc:  # noqa: BLE001
        result = {"state": "error", "error": str(exc)}

    # **顺序要紧**：先把取到的 token 与账号名报出去，再报「正在关浏览器」。
    # （早先版本把关浏览器写在 `finally` 里，于是 `closing` 排在 `got_token`
    #   之前 —— 界面会先显示「正在关闭浏览器」再显示「已取到凭据」，很怪。）
    token = normalize_token(result.get("token")) if result.get("state") == "ready" else ""
    label = str(result.get("account_label") or "")
    if token:
        status("got_token", {"len": len(token), "account_label": label})

    status("closing", {})
    with contextlib.suppress(Exception):
        asyncio.run(sess.close_browser())

    if not token:
        detail = {
            "timeout": f"等待登录超时（{login_timeout_sec:.0f}s）—— 请在浏览器窗口里完成登录",
            "no_browser": "浏览器被关闭了",
            "error": result.get("error") or "读取 token 出错",
        }.get(str(result.get("state")), f"未取到 token（state={result.get('state')}）")
        status("failed", {"detail": detail})
        return TokenResult(token="", source="浏览器", valid=False, detail=detail)

    if validate:
        ok, detail = validate_token(token)
        if not ok:
            status("failed", {"detail": detail})
            return TokenResult(
                token=token,
                source="浏览器",
                valid=False,
                detail=f"取到 token 但验证不过：{detail}",
                account_label=label,
            )
    else:
        detail = "未验证（validate=False）"

    saved = save_token(token, token_file)
    status("validated", {"source": "浏览器", "detail": detail})
    return TokenResult(
        token=token,
        source="浏览器",
        valid=True,
        detail=detail,
        account_label=label,
        saved_to=str(saved),
    )


def _default_profile_dir() -> Path:
    from .browser_login import default_profile_dir

    return default_profile_dir()


__all__ = [
    "DEFAULT_CDP_PORT",
    "DEFAULT_TOKEN_FILE",
    "BrowserLoginError",
    "BrowserLoginSession",
    "IdentityError",
    "TokenResult",
    "clear_token",
    "ensure_token",
    "load_token",
    "normalize_token",
    "read_token_and_label",
    "read_token_from_cdp",
    "read_token_from_cdp_async",
    "resolve_token",
    "save_manual_token",
    "save_token",
    "validate_token",
    "validate_token_async",
]
