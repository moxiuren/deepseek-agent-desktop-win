"""传输层注册表 —— 档位可插拔的关键。

`Transport` 是唯一抽象；换档位只换这里的实现，上层一行不动。
"""

from __future__ import annotations

from ..config import EngineConfig
from .base import (
    DeltaCallback,
    ModelInfo,
    RemoteStatus,
    ThoughtCallback,
    Transport,
    TransportError,
    TurnOutcome,
)
from .deepseek_rpc import (
    SESSION_LIST_MAX_COUNT,
    SESSION_LIST_MIN_COUNT,
    DeepSeekRpcTransport,
)

__all__ = [
    "SESSION_LIST_MAX_COUNT",
    "SESSION_LIST_MIN_COUNT",
    "DeepSeekRpcTransport",
    "DeltaCallback",
    "ModelInfo",
    "RemoteStatus",
    "ThoughtCallback",
    "Transport",
    "TransportError",
    "TurnOutcome",
    "create_transport",
]


def create_transport(cfg: EngineConfig) -> Transport:
    """按配置装配传输适配器。换档位只需改这里。

    **token 走惰性提供者，不在装配时读取。** 理由是身份获取可能要弹浏览器
    （`identity.ensure_token`），而装配发生在引擎启动的第一秒 —— 那时弹窗
    既突兀又会把启动卡住。所以这里只给一个「从缓存文件读」的提供者：
      * 有缓存 → 直接用
      * 没缓存 / 失效 → `start()` 抛 auth 错 → 界面据此提示「需要登录」，
        再由显式的登录动作去拉浏览器（见 `__main__` 的 `account.login.*`）
    """
    from ..identity import load_token

    kind = (cfg.transport or "deepseek_rpc").strip().lower()

    if kind in ("deepseek_rpc", "rpc", "t3"):
        token_file = cfg.resolved_token_file()
        return DeepSeekRpcTransport(
            token_provider=lambda: load_token(token_file),
            device_id_path=_device_path(cfg),
            model=cfg.identity.model or "default",
            search_enabled=bool(cfg.chat.search_enabled),
            proxy=cfg.identity.proxy or None,
            health_probe=bool(cfg.circuit.health_probe_enabled),
            pow_prefetch=bool(cfg.pow.prefetch),
            pow_prefetch_count=int(cfg.pow.prefetch_count),
            pow_expire_margin_sec=float(cfg.pow.expire_margin_sec),
        )

    if kind in ("chrome", "chrome_cdp", "browser", "t4"):
        # 真实 Chrome 内核（headful，窗口可移出屏幕）。
        # 与 deepseek_rpc 共用 challenge / PoW / SSE 解析，只换「谁把字节发出去」。
        from .chrome_cdp import ChromeCdpTransport

        return ChromeCdpTransport(
            token_provider=lambda: load_token(cfg.resolved_token_file()),
            device_id_path=_device_path(cfg),
            model=cfg.identity.model or "default",
            search_enabled=bool(cfg.chat.search_enabled),
        )

    if kind in ("webview", "t1"):
        # T1 骨架已落地（transport/webview.py）；完整 DOM 注入移植排阶段 5。
        # start() 会抛 fatal TransportError，明确告知未实现，避免误用。
        from .webview import WebViewTransport

        return WebViewTransport()

    if kind in ("gemini_rpc", "gemini", "g_rpc"):
        # Gemini 网页版 RPC 直连（`transport/gemini_rpc.py`）。
        #
        # **必须惰性导入** —— 该模块顶层 `from gemini_webapi import ...`
        # 是延后到 `_ensure_client()` 的（见文件内注释），但即便如此，
        # 把它放在这里的 import 语句仍然只在真的选了 gemini 档位时才执行。
        # 这样没装 `gemini-webapi` 的机器上用 deepseek_rpc 完全不受影响，
        # 而选了 gemini 却没装依赖时会得到一条明确的 `TransportError`，
        # 而不是「整个 transport 包导入失败 → 引擎起不来」这种归因困难的坏法。
        from .gemini_rpc import GeminiRpcTransport

        return GeminiRpcTransport(
            identity=cfg.identity,
            maintenance=cfg.maintenance,
            # 活跃时段来自 pacing —— 与 gemini 原版同源（它也是从 pacing 取）。
            active_hours=(
                int(cfg.pacing.active_start_hour),
                int(cfg.pacing.active_end_hour),
            ),
        )

    raise TransportError(
        f"未知的传输档位：{cfg.transport!r}"
        "（可选 deepseek_rpc / chrome / webview / gemini_rpc）",
        config=True,
    )


def _device_path(cfg: EngineConfig):
    """设备 id 的落点 —— 跟 token 放同一个目录，便于整体搬迁/清理。"""
    from pathlib import Path

    return Path(cfg.resolved_token_file()).parent / ".device-id"
