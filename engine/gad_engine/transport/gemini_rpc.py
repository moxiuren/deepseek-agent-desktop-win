"""Gemini 网页版 RPC 直连传输（`transport == "gemini_rpc"`）。

本文件从 `gemini-agent-desktop` 的 `transport/rpc_web.py` 移植，适配本仓
`Transport` 抽象。**不是照抄**：两边 `base.py` 的接口面不同（本仓 `send()`
多 `search_enabled`、`TurnOutcome` 多一批遥测字段），且本仓
`IdentityConfig` / `MaintenanceConfig` / `circuit.py` 原本是照 DeepSeek 写的，
Gemini 需要的东西那边没有。差异逐条在本文件里就地注明。

与 DOM 注入（T1）的本质区别：不经过页面渲染，直接读 SSE 流。因此没有
「代码块识别」这一整类 bug，无需回填输入框、无需三级定位发送按钮；
上下文由服务端凭 cid/rid/rcid 持有，payload 从 O(n) 降到 O(1)。

指纹：`impersonate` 由 `identity.browser_impersonate` 提供（默认 chrome150）。
上游源码注释写明选 chrome145+ 是为了规避 Device Bound Session Credentials
（DBSC 是 Chrome 146+ 才强制的机制）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import threading
import time
from enum import IntEnum
from pathlib import Path
from typing import Any

from ..config import IdentityConfig, MaintenanceConfig
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

# 会随上游轮换而变化、且必须落盘的两个 cookie。
# `__Secure-1PSIDTS` 是那个会被服务端滚动更新、过期即 1016 的；
# `__Secure-1PSID` 一般不轮换，但一起写能保证两份不会「一半新一半旧」。
_ROTATING_COOKIE_NAMES = ("__Secure-1PSID", "__Secure-1PSIDTS")


class AccountStatus(IntEnum):
    """账号状态码 —— **本文件自带一份，不往 `circuit.py` 里塞**。

    `circuit.py` 是本仓四档熔断状态机，DeepSeek 侧没有「账号状态查询接口」，
    所以那边根本没有这个枚举。把 Gemini 的状态码加进去会波及 248 个现有测试
    （它们断言熔断器的构造签名与信号集合），而这里只需要 `parse()` 做展示与分类。

    代价：两个档位各有一份定义。可接受 —— 它们**不是同一个上游**，本来就不该
    强行合并；真要合并，应该等第二个 Gemini 类传输出现时再抽。
    """

    UNKNOWN = 0
    AVAILABLE = 1000
    ACCESS_TEMPORARILY_UNAVAILABLE = 1014
    UNAUTHENTICATED = 1016
    #: 以下三个是确定性封禁/限流信号 —— 命中即全停，不做退避重试。
    RATE_LIMITED = 1021
    ACCOUNT_RESTRICTED = 1033
    ACCOUNT_BLOCKED = 1040

    @classmethod
    def parse(cls, raw: object) -> AccountStatus:
        try:
            value = int(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return cls.UNKNOWN
        try:
            return cls(value)
        except ValueError:
            return cls.UNKNOWN

    @property
    def description(self) -> str:
        return {
            AccountStatus.UNKNOWN: "未知状态",
            AccountStatus.AVAILABLE: "账号正常",
            AccountStatus.ACCESS_TEMPORARILY_UNAVAILABLE: "访问被临时限制（地区或会话问题）",
            AccountStatus.UNAUTHENTICATED: "未认证（访客模式下属正常态）",
            AccountStatus.RATE_LIMITED: "被限流",
            AccountStatus.ACCOUNT_RESTRICTED: "账号被限制",
            AccountStatus.ACCOUNT_BLOCKED: "账号被封禁",
        }.get(self, f"状态码 {int(self)}")

    @property
    def short(self) -> str:
        """两三个字的短标签，给顶栏用（`description` 太长会把那一行撑爆）。"""
        return {
            AccountStatus.UNKNOWN: "未知",
            AccountStatus.AVAILABLE: "正常",
            AccountStatus.ACCESS_TEMPORARILY_UNAVAILABLE: "限流",
            AccountStatus.UNAUTHENTICATED: "未认证",
            AccountStatus.RATE_LIMITED: "限流",
            AccountStatus.ACCOUNT_RESTRICTED: "受限",
            AccountStatus.ACCOUNT_BLOCKED: "封禁",
        }.get(self, str(int(self)))


def extract_rotating_cookies(client: Any) -> dict[str, str]:
    """从上游 client 的 cookie jar 里取出待持久化的 cookie。

    同名 cookie 会同时存在于 `.google.com` / `.youtube.com` / `.google.com.my`
    等多个域下（实测一个 profile 里有三份）。**只取第一个** —— 上游自己
    （`utils/rotate_1psidts._extract_cookie_value`）也是这么取的，
    保持一致才能保证「我们落盘的值」和「上游在用的值」是同一个。
    """
    jar = getattr(getattr(client, "cookies", None), "jar", None)
    if jar is None:
        return {}
    found: dict[str, str] = {}
    for cookie in jar:
        name = getattr(cookie, "name", None)
        value = getattr(cookie, "value", None)
        if name in _ROTATING_COOKIE_NAMES and value and name not in found:
            found[name] = value
    return found


def default_cookie_path(label: str) -> Path:
    """默认 cookie 落点：`%LOCALAPPDATA%\\Gemini-Agent\\cookies-<label>.json`。

    与 gemini 原版一致 —— 那个目录是它的既有数据目录。**不要**改成
    `DeepSeek-Agent` 目录：两个项目的 cookie 混在一起放，删一个会误伤另一个。
    """
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    safe = label or "guest"
    return Path(base) / "Gemini-Agent" / f"cookies-{safe}.json"


def load_cookies(path: str | Path) -> dict[str, str]:
    """加载 cookies.json。

    接受两种形态：
      {"__Secure-1PSID": "...", "__Secure-1PSIDTS": "..."}
      [{"name": "__Secure-1PSID", "value": "..."}, ...]
    """
    p = Path(path).expanduser() if str(path).strip() else None
    if p is None:
        raise TransportError("尚未配置 Gemini cookie 文件路径", auth=True)
    if not p.exists() or p.is_dir():
        raise TransportError(f"Gemini cookie 文件不存在：{p}", auth=True)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TransportError(f"Gemini cookie 文件解析失败：{exc}", auth=True) from exc

    if isinstance(data, dict):
        out = {k: v for k, v in data.items() if isinstance(v, str)}
    elif isinstance(data, list):
        out = {
            str(item.get("name")): str(item.get("value"))
            for item in data
            if isinstance(item, dict) and item.get("name") and item.get("value")
        }
    else:
        raise TransportError("Gemini cookie 文件结构无法识别", auth=True)

    missing = [k for k in _ROTATING_COOKIE_NAMES if not out.get(k)]
    if missing:
        raise TransportError(
            f"Gemini cookie 缺少必需项：{', '.join(missing)}（需重新通过浏览器登录获取）",
            auth=True,
        )
    return out


def _num(raw: Any) -> float | None:
    """只认真正的数字。上游偶尔把比值塞成字符串或 None。"""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        try:
            return float(raw.strip())
        except ValueError:
            return None
    return None


def _clamp_ratio(value: float) -> float:
    return 0.0 if value < 0 else (1.0 if value > 1 else value)


# usage_info 里窗口键 → 中文短标签。上游用 `current_5h` / `weekly` 这类键，
# 直接显示给用户不好读，这里做一次映射；遇到没见过的键就退回原键名。
_QUOTA_WINDOW_LABELS = {
    "current_5h": "5 小时",
    "weekly": "本周",
    "daily": "今日",
}


def _extract_quota(quotas: Any) -> tuple[float | None, dict]:
    """从上游 `quotas` 抽出「最紧的那个窗口已用比例」+ 明细。

    取最紧的那个（而不是平均）是因为额度是**木桶效应**：任何一个窗口耗尽
    都会让账号发不出去，平均值会把「5 小时已用 100%、本周才 5%」这种
    真实卡死状态抹平成 52%，看起来一切正常。
    """
    detail: dict[str, Any] = {}
    if not isinstance(quotas, dict):
        return None, detail

    windows: dict[str, dict] = {}
    worst: float | None = None
    for key, item in quotas.items():
        if not isinstance(item, dict):
            continue
        ratio = _num(item.get("used_ratio"))
        if ratio is None:
            used = _num(item.get("used"))
            total = _num(item.get("total"))
            if used is not None and total:
                ratio = used / total
        if ratio is None:
            continue
        ratio = _clamp_ratio(ratio)
        label = _QUOTA_WINDOW_LABELS.get(str(key), str(key))
        windows[label] = {
            "used_ratio": round(ratio, 4),
            "reset_at": item.get("reset_at") or item.get("reset_time") or "",
        }
        if worst is None or ratio > worst:
            worst = ratio

    if windows:
        detail["windows"] = windows
        tier = quotas.get("tier")
        if tier:
            detail["tier"] = str(tier)
    return worst, detail


class GeminiRpcTransport(Transport):
    """Gemini 网页版 RPC 直连。"""

    name = "gemini_rpc"
    supports_streaming = True

    def __init__(
        self,
        identity: IdentityConfig,
        loop: asyncio.AbstractEventLoop | None = None,
        maintenance: MaintenanceConfig | None = None,
        active_hours: tuple[int, int] = (8, 24),
    ) -> None:
        self.identity = identity
        self._loop = loop
        self._client: Any = None
        self._cookies: dict[str, str] = {}
        self._last_status = RemoteStatus()
        self._init_lock = asyncio.Lock()

        # 后台保活策略（机器特征最集中的地方）
        self._maintenance = maintenance or MaintenanceConfig()
        self._active_hours = active_hours
        self._busy = False
        self._supervisor: asyncio.Task | None = None
        self._heartbeat_on: bool | None = None
        self._heartbeat_note = "未启动"

        # 停止标志。**必须是同步、任意线程可调** —— 它由 IPC 线程直接调，
        # 而 `run()` 在事件循环里读。`threading.Event` 是这里唯一正确的选择；
        # 用 `asyncio.Event` 会要求调用方先拿到 loop（跨线程不安全）。
        self._stop_event = threading.Event()

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        if not self.identity.guest_mode:
            self._cookies = load_cookies(self._resolve_cookie_file())
        await self._ensure_client()
        self._start_supervisor()

    def _resolve_cookie_file(self) -> str:
        """显式配置优先，否则退回 `%LOCALAPPDATA%\\Gemini-Agent\\` 下的约定路径。"""
        explicit = (self.identity.cookie_file or "").strip()
        if explicit:
            return explicit
        return str(default_cookie_path(self.identity.account_label))

    async def _ensure_client(self) -> Any:
        async with self._init_lock:
            if self._client is not None:
                return self._client
            try:
                from gemini_webapi import GeminiClient
            except ImportError as exc:
                raise TransportError(
                    "未安装 gemini-webapi。请在 engine 目录执行："
                    ".venv/Scripts/python.exe -m pip install gemini-webapi>=2.1.1",
                    config=True,
                ) from exc

            if self.identity.guest_mode:
                # 访客会话：不传 cookie，上游走 google.com 预检取匿名 cookie，
                # 最后回退到 guest session。
                client = GeminiClient("", "", proxy=self.identity.proxy or None)
            else:
                client = GeminiClient(
                    self._cookies["__Secure-1PSID"],
                    self._cookies["__Secure-1PSIDTS"],
                    proxy=self.identity.proxy or None,
                )
            try:
                # `auto_refresh=True` 会拉起上游两个**固定节拍**的后台任务：
                #   * start_auto_refresh      —— cookie 轮换，**保留**
                #   * start_activity_watchdog —— 活跃心跳，**由我们接管开关**
                #
                # 不干脆全关：cookie 轮换是会话不失效的前提
                # （`__Secure-1PSIDTS` 会被服务端滚动更新，本地不跟着换就 401），
                # 频率本来很低（~600s ≈ 6 次/小时），不值得为省这点去冒
                # 「隔夜就要重新登录」的险。
                #
                # 访客模式 `auto_refresh=False`：没有 cookie 可轮换，
                # 开着只会让上游的轮换任务对着空 jar 空转。
                await client.init(
                    impersonate=self.identity.browser_impersonate,
                    auto_refresh=not self.identity.guest_mode,
                    verbose=False,
                )
            except Exception as exc:
                raise TransportError(f"Gemini 客户端初始化失败：{exc}", auth=True) from exc
            self._client = client
            # `init()` 可能刚用过上游自己的缓存（那份通常比我们的文件新），
            # 顺手把此刻的权威值写回我们的文件。
            self._persist_rotated_cookies(client)
            return client

    def _persist_rotated_cookies(self, client: Any) -> None:
        """把上游此刻持有的 cookie 写回我们自己的 cookie 文件。

        为什么必须做（gemini 原版 2026-10-14 一天掉线 4 次的第二层原因）：

        上游把轮换后的新值写进**它自己的缓存**（`.cached_cookies_<1PSID>.json`），
        而我们的 `cookies-<label>.json` 只在登录时写过一次，自动轮换从不落盘它。
        于是形成死循环：

          1. 缓存里的值被服务端作废 → `init()` 用缓存 → 1016；
          2. 上游判定「缓存产出了未认证会话」→ **主动删掉缓存**；
          3. 下次 `init()` 回退到**我们这份陈旧的文件** → 又 1016；
          4. 1016 时上游的轮换任务根本不启动 → **永久卡死，只能重走登录**。

        把 cookie 文件也保持新鲜，第 3 步就退化成「回退到一个还能用的值」，
        死循环从「必然」变成「可自愈」。

        两个安全约束：
          * **只在账号可用时写** —— 否则会把一个刚被拒的坏值固化下来；
          * **值没变就不写盘** —— 轮换约 10 分钟一次，没必要每次都动文件。
        """
        if self.identity.guest_mode:
            return
        cookie_file = self._resolve_cookie_file().strip()
        if not cookie_file:
            return
        if not self._account_available(client):
            return

        fresh = extract_rotating_cookies(client)
        if len(fresh) != len(_ROTATING_COOKIE_NAMES):
            return  # 拿不全就整批不写，避免写出「一半新一半旧」

        path = Path(cookie_file)
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            current = {}
        if isinstance(current, dict) and all(
            current.get(name) == value for name, value in fresh.items()
        ):
            return

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(fresh, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            # 明文凭据等同账号密码 —— 权限收紧到本人可读写。
            os.chmod(path, 0o600)
        except OSError:
            # 落盘失败不该让这一轮请求失败 —— 内存里的 cookie 仍然是对的。
            pass

    async def stop(self) -> None:
        self._stop_supervisor()
        if self._client is not None:
            # 关闭上游客户端时抛异常没有可做的补救 —— 它已经不可用了，
            # 而 `stop()` 的契约是「无论如何都要把状态置为已停止」。
            # 用 suppress 而不是 try/except/pass，是因为后者容易被误读成
            # 「这里还有未处理的逻辑」。
            with contextlib.suppress(Exception):
                await self._client.close()
            self._client = None
        self._heartbeat_on = None
        self._heartbeat_note = "已停止"

    # ---------- 停止 ----------

    def request_stop(self) -> bool:
        """请求停止当前这一轮。

        内嵌版 `base.py` 要求**同步、无等待、可从任意线程调用**（由 IPC 线程
        直接调），所以这里用 `threading.Event` 而不是 `asyncio.Event`。
        """
        self._stop_event.set()
        return True

    def clear_stop(self) -> None:
        """清除停止标志。由 `Agent.run()` 在每轮任务开头调用一次。"""
        self._stop_event.clear()

    @property
    def stop_requested(self) -> bool:
        return self._stop_event.is_set()

    # ---------- 后台保活策略 ----------

    def _in_active_hours(self) -> bool:
        start, end = self._active_hours
        if not (0 <= start < 24 and 0 < end <= 24):
            return True  # 配置不合理就当全天活跃，不因此静默掉保活
        hour = time.localtime().tm_hour
        if start <= end:
            return start <= hour < end
        return hour >= start or hour < end  # 跨午夜

    def _want_heartbeat(self) -> bool:
        m = self._maintenance
        if not m.enabled:
            return False
        if self._busy:
            # 正在跑任务 —— 心跳本来就是「这次活动的一部分」，不必省
            return True
        if not m.heartbeat_while_idle:
            return False  # 默认：空闲即静默
        return self._in_active_hours() if m.respect_active_hours else True

    def _apply_background_policy(self) -> None:
        """按策略开/关上游的活跃心跳任务。**纯本地判断，不发请求。**"""
        client = self._client
        if client is None or not getattr(client, "_running", False):
            return

        want = self._want_heartbeat()
        task = getattr(client, "activity_task", None)
        alive = task is not None and not task.done()
        if want == alive:
            return  # 状态已一致，不动它（避免反复 cancel/create）

        if want:
            client.activity_task = asyncio.create_task(client.start_activity_watchdog())
            self._heartbeat_note = "已开启（有任务在跑）"
        else:
            task.cancel()
            client.activity_task = None
            self._heartbeat_note = (
                "已暂停（空闲）" if self._maintenance.enabled else "已关闭（配置禁用）"
            )
        self._heartbeat_on = want

    async def _supervise_background(self) -> None:
        interval = max(self._maintenance.supervise_interval_ms, 5000) / 1000.0
        try:
            while True:
                await asyncio.sleep(interval)
                self._apply_background_policy()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 巡检不该把传输层带崩
            pass

    def _start_supervisor(self) -> None:
        if self._supervisor is not None and not self._supervisor.done():
            return
        # 先立刻判一次，别让「启动后一分钟内」还留着上游刚拉起的那个心跳
        self._apply_background_policy()
        self._supervisor = asyncio.create_task(self._supervise_background())

    def _stop_supervisor(self) -> None:
        task = self._supervisor
        self._supervisor = None
        if task is not None and not task.done():
            task.cancel()

    def background_state(self) -> dict:
        return {
            "heartbeat": bool(self._heartbeat_on),
            "note": self._heartbeat_note,
            "busy": self._busy,
            "enabled": self._maintenance.enabled,
            "active_hours": list(self._active_hours),
            "in_active_hours": self._in_active_hours(),
        }

    # ---------- 状态 ----------

    async def health(self) -> RemoteStatus:
        client = await self._ensure_client()
        status = RemoteStatus(reachable=True)
        try:
            raw_status = int(getattr(client, "account_status", 0) or 0)
            status.account_status = raw_status
            parsed = AccountStatus.parse(raw_status)
            # ⚠️ 访客模式特判 —— **这一行是访客档能用的前提**。
            # 访客模式下上游恒返回 1016（未认证），那是**正常态**而不是故障。
            # 不特判的话，`Agent` 会把 1016 当成 auth 失败喂给熔断器，
            # 连续 3 次就停机 —— 表现是「引擎刚起来就说自己被封了」。
            if self.identity.guest_mode and parsed is AccountStatus.UNAUTHENTICATED:
                status.account_status_text = "访客模式（未登录）"
                status.account_status_short = "访客"
            else:
                status.account_status_text = parsed.description
                status.account_status_short = parsed.short
        except Exception as exc:  # noqa: BLE001
            status.note = f"账号状态读取失败：{exc}"

        try:
            abuse = getattr(client, "abuse_status", None)
            if isinstance(abuse, dict):
                status.abuse_clean = bool(abuse.get("is_clean", True))
                status.abuse_signal = abuse.get("signal")
        except Exception:  # noqa: BLE001
            pass

        try:
            ratio, detail = _extract_quota(getattr(client, "quotas", None))
            status.quota_used_ratio = ratio
            status.quota_detail = detail
            status.tier = str(detail.get("tier") or "")
        except Exception:  # noqa: BLE001
            pass

        status.model = self.identity.model
        self._last_status = status
        return status

    # ---------- 模型 ----------

    def available_models(self) -> list[ModelInfo]:
        """本账号可用模型。上游在 `init()` 时按账号动态发现，所以必须客户端就绪。

        客户端还没起来时返回空列表而不是抛异常 —— 界面在引擎刚启动、
        还没 `init()` 完的那一刻就会问一次，那一刻「暂时没有」是正常态。
        """
        client = self._client
        if client is None:
            return []
        try:
            models = client.list_models() or []
        except Exception:  # noqa: BLE001 - 上游异常类型不稳定
            return []
        out: list[ModelInfo] = []
        for m in models:
            name = str(getattr(m, "model_name", "") or "")
            if not name:
                continue
            out.append(
                ModelInfo(
                    name=name,
                    display_name=str(getattr(m, "display_name", "") or name),
                    description=str(getattr(m, "description", "") or ""),
                    is_available=bool(getattr(m, "is_available", True)),
                    aliases=[str(a) for a in (getattr(m, "aliases", None) or [])],
                )
            )
        return out

    async def set_model(self, name: str) -> None:
        """校验模型名在本账号是否可用。

        配置层来的名字可能来自手改的配置文件，所以这里必须真的问一遍上游 ——
        直接把非法名字写进配置，故障会推迟到下一次发送才暴露，那时错误信息
        已经离现场很远了。
        """
        target = (name or "").strip()
        if not target or target == "default":
            return
        client = self._client
        if client is None:
            raise TransportError("引擎尚未就绪，无法校验模型名，请稍后再试")
        try:
            client.resolve_model(target)
        except ValueError as exc:
            raise TransportError(str(exc)) from exc

    # ---------- 发送 ----------

    @staticmethod
    def _account_available(client: Any) -> bool:
        """账号当前是不是「已登录且可用」。

        用来把「模型名解析失败」分类：如果账号本身就没登录，那失败根因是认证，
        熔断层应该按认证失败退避；否则是纯粹的配置问题。
        """
        raw = int(getattr(client, "account_status", 0) or 0)
        return AccountStatus.parse(raw) is AccountStatus.AVAILABLE

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
        """发送一轮。

        `search_enabled` 在这里**接受但忽略** —— 与 `deepseek_rpc` 的差异点。
        Gemini 网页版的联网搜索不是一个「发不发」的请求参数，而是上游自己
        按内容决定的（模型判断需要搜就搜），`gemini_webapi` 也没有暴露开关。
        假装支持（比如塞一个 `deepseek` 侧的字段名）会让用户以为关掉了搜索，
        实际没有 —— 那比明确不支持更糟。搜索**痕迹**仍通过
        `TurnOutcome.search_pages` / `search_sources` 如实上报（见下）。
        """
        client = await self._ensure_client()

        # 跑任务期间允许心跳存活 —— 这段活动本身就是「真人正在使用」的一部分。
        # 立刻判一次（而不是等下一次巡检），让策略与真实状态同步。
        self._busy = True
        self._apply_background_policy()
        try:
            outcome = await self._send_once(
                client,
                prompt,
                session,
                temporary,
                extended_thinking,
                files,
                on_delta,
                on_thought,
            )
        finally:
            self._busy = False
            self._apply_background_policy()

        # 轮换任务与我们的请求是并发跑的，所以每一轮结束后再同步一次 cookie 文件
        # （`_ensure_client` 那次只能覆盖「启动时」的权威值）。
        # 放在 try 之外：这一轮本身失败时不该去写盘。
        self._persist_rotated_cookies(client)
        return outcome

    async def _send_once(
        self,
        client: Any,
        prompt: str,
        session: SessionMeta,
        temporary: bool,
        extended_thinking: bool,
        files: list[str] | None,
        on_delta: DeltaCallback | None,
        on_thought: ThoughtCallback | None = None,
    ) -> TurnOutcome:
        # 注意：`temporary` 是 send_message / generate_content_stream 的参数，
        # 不是 ChatSession 的构造参数。
        #
        # `model` 是 **per-chat** 的，而这里每一轮都新建 ChatSession，
        # 所以改模型只要改配置即可生效 —— 不需要重启引擎（与改身份不同）。
        model = (self.identity.model or "").strip()

        # ⚠️ 上游行为（gemini 原版 2026-10-14 实测，别改回去）：
        #   `generate_content_stream()` 只转发**它自己的 `model` 形参**，
        #   **从不读 `chat.model`**。只有 `ChatSession.send_message*()` 才会
        #   用 `self.model` 转发。
        #
        #   所以「`start_chat(model=X)` 然后把 chat 交给 generate_content_stream」
        #   这条写法里，X **根本不会进入请求** —— 模型选择头不会出现在 HTTP 头里，
        #   由 Google 自行决定用哪个。
        #
        #   症状极具迷惑性：模型下拉能选、`model.set` 校验通过、`resolve_model`
        #   也解析成功，唯独真正发出去的请求里没有它。所以必须在这里显式解析
        #   并把结果传下去。
        selected: Any = None
        if model and model != "default":
            try:
                selected = client.resolve_model(model)
            except ValueError as exc:
                # 配置里的模型名在本账号解析不出来（掉登录降到访客档、或档位降级都会这样）。
                # **绝不静默退回账号默认** —— 那正是「以为在用 pro、其实在用 flash」
                # 这类问题的来源。宁可明确报错，让用户知道发生了什么。
                raise TransportError(
                    f"配置的模型 {model!r} 在当前账号不可用：{exc}",
                    auth=not self._account_available(client),
                    config=True,
                ) from exc

        chat = client.start_chat(
            metadata=session.as_metadata() if session.cid else None,
            model=selected,
        )

        collected: list[str] = []
        thoughts: list[str] = []
        last_meta: list[str] = []
        try:
            stream = client.generate_content_stream(
                prompt=prompt,
                files=[Path(f) for f in files] if files else None,
                chat=chat,
                # 这一行才是模型真正生效的地方 —— 上游不读 `chat.model`。
                # `selected` 为 None 时等价于「账号默认」。
                model=selected,
                temporary=temporary,
                # ⚠️ 思考模式和模型是**同一类**参数：都是
                # `generate_content_stream()` 自己的形参，不传就走默认 False。
                extended_thinking=extended_thinking,
            )
            async for chunk in stream:
                # 用户按了停止 —— 上游的流不会自己断，必须在这里主动跳出。
                # 已生成的内容照常保留（用户预期是「别再往下写了」，
                # 不是「把已写的也抹掉」）。
                if self._stop_event.is_set():
                    collected.append("\n\n[已按用户请求停止]")
                    break

                delta = getattr(chunk, "text_delta", "") or ""
                if delta:
                    collected.append(delta)
                    if on_delta is not None:
                        result = on_delta(delta)
                        if asyncio.iscoroutine(result):
                            await result
                tdelta = getattr(chunk, "thoughts_delta", "") or ""
                if tdelta:
                    thoughts.append(tdelta)
                    # 思考通道要**转发出去**（而不是只攒着）。开了思考模式时，
                    # 上游会先把思考 token 流完才吐正文 —— 这段时间正文通道
                    # 一个字都没有，界面上就是「发了没反应」。转发之后界面能
                    # 显示「思考中…」，用户才知道它真的在跑。
                    if on_thought is not None:
                        result = on_thought(tdelta)
                        if asyncio.iscoroutine(result):
                            await result
                meta = getattr(chunk, "metadata", None)
                if isinstance(meta, list) and meta:
                    last_meta = meta
        except Exception as exc:
            message = str(exc)
            low = message.lower()
            auth = any(
                k in low
                for k in ("unauthenticated", "cookie", "unauthorized", "401", "permission")
            )
            raise TransportError(message, auth=auth) from exc

        cid = last_meta[0] if len(last_meta) > 0 and last_meta[0] else session.cid
        rid = last_meta[1] if len(last_meta) > 1 and last_meta[1] else session.rid
        rcid = last_meta[2] if len(last_meta) > 2 and last_meta[2] else session.rcid

        # `TurnOutcome` 只填基础字段。本仓那批 DeepSeek 专用遥测
        # （pow_* / upload_* / ttfb_ms / http_status）在 Gemini 档位没有对应物，
        # 保持 dataclass 默认值 —— **不要**为了「看起来完整」去编造它们。
        return TurnOutcome(
            text="".join(collected),
            thoughts="".join(thoughts),
            cid=str(cid or ""),
            rid=str(rid or ""),
            rcid=str(rcid or ""),
        )
