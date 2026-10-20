"""Gemini 传输档位自测（不依赖 pytest，直接跑）。

覆盖：
  AccountStatus 解析 · quota 木桶效应 · 停止标志跨线程 ·
  **访客模式下 1016 归一为正常态**（这条是访客档能用的前提）·
  send 忽略 search_enabled 且不把它泄进上游调用 · 模型解析失败时明确报错 ·
  未装 gemini-webapi 时的错误分类 · create_transport 注册与未知档位拒绝

用法：
    cd engine && ./.venv/Scripts/python.exe tests/test_gemini_transport.py
"""

from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gad_engine.config import EngineConfig, IdentityConfig, MaintenanceConfig
from gad_engine.session import SessionMeta
from gad_engine.transport import create_transport
from gad_engine.transport.base import RemoteStatus, TurnOutcome
from gad_engine.transport.gemini_rpc import (
    AccountStatus,
    GeminiRpcTransport,
    _extract_quota,
    default_cookie_path,
)


# ---------- 极简断言 ----------

_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        _failures.append(name)
        print(f"  FAIL  {name}  {detail}")


def eq(name: str, got, want) -> None:
    check(name, got == want, f"got={got!r} want={want!r}")


# ---------- 假上游 ----------


class FakeModel:
    def __init__(self, name: str) -> None:
        self.model_name = name
        self.display_name = name.upper()
        self.description = ""
        self.is_available = True
        self.aliases: list[str] = []


class FakeChunk:
    def __init__(self, text: str = "", thought: str = "", meta=None) -> None:
        self.text_delta = text
        self.thoughts_delta = thought
        self.metadata = meta


class FakeClient:
    """只实现 GeminiRpcTransport 真正会碰到的那些成员。"""

    def __init__(self, status: int = 1000, chunks=None) -> None:
        self.account_status = status
        self.abuse_status = {"is_clean": True, "signal": None}
        self.quotas = None
        self._running = True
        self.activity_task = None
        self.cookies = None
        self.access_token = "fake"
        self._chunks = chunks or [FakeChunk(text="hello", meta=["c1", "r1", "q1"])]
        # 记录上游收到的参数 —— 用来验证「没把不该传的东西传下去」
        self.gen_kwargs: dict = {}
        self.started_chat_with: dict = {}

    def list_models(self):
        return [FakeModel("gemini-pro"), FakeModel("gemini-flash")]

    def resolve_model(self, name: str):
        if name not in ("gemini-pro", "gemini-flash"):
            raise ValueError(f"未知模型 {name}")
        return FakeModel(name)

    def start_chat(self, **kwargs):
        self.started_chat_with = kwargs
        return object()

    def generate_content_stream(self, **kwargs):
        self.gen_kwargs = kwargs

        async def _gen():
            for c in self._chunks:
                yield c

        return _gen()


def make_transport(guest: bool = True, model: str = "default", status: int = 1000,
                   chunks=None) -> tuple[GeminiRpcTransport, FakeClient]:
    t = GeminiRpcTransport(
        IdentityConfig(guest_mode=guest, model=model),
        maintenance=MaintenanceConfig(),
    )
    fake = FakeClient(status=status, chunks=chunks)
    t._client = fake  # 绕过 _ensure_client，避免真的去 import gemini_webapi
    return t, fake


# ---------- 用例 ----------


def case_account_status() -> None:
    print("\n[AccountStatus]")
    eq("1000 -> 正常", AccountStatus.parse(1000).short, "正常")
    eq("1016 -> 未认证", AccountStatus.parse(1016).short, "未认证")
    eq("1021 -> 限流", AccountStatus.parse(1021).short, "限流")
    eq("1040 -> 封禁", AccountStatus.parse(1040).short, "封禁")
    eq("未知码不炸", AccountStatus.parse(9999), AccountStatus.UNKNOWN)
    eq("字符串数字可解析", AccountStatus.parse("1016"), AccountStatus.UNAUTHENTICATED)
    eq("None 不炸", AccountStatus.parse(None), AccountStatus.UNKNOWN)
    eq("乱码不炸", AccountStatus.parse("abc"), AccountStatus.UNKNOWN)


def case_quota() -> None:
    print("\n[quota 木桶效应]")
    ratio, detail = _extract_quota(
        {"current_5h": {"used_ratio": 1.0}, "weekly": {"used_ratio": 0.05}}
    )
    # 取最紧的那个窗口，而不是平均值 —— 平均值会把「5 小时已耗尽」
    # 这种真实卡死状态抹平成 52%，看起来一切正常。
    eq("取最紧窗口", ratio, 1.0)
    eq("明细含两个窗口", len(detail.get("windows", {})), 2)

    ratio2, _ = _extract_quota({"weekly": {"used": 50, "total": 200}})
    eq("used/total 可换算", ratio2, 0.25)

    ratio3, detail3 = _extract_quota(None)
    eq("None -> None", ratio3, None)
    eq("None -> 空明细", detail3, {})

    ratio4, _ = _extract_quota({"weekly": {"used_ratio": 1.7}})
    eq("超过 1 被夹住", ratio4, 1.0)


def case_stop_flag() -> None:
    print("\n[停止标志]")
    t, _ = make_transport()
    eq("初始未置位", t.stop_requested, False)
    eq("request_stop 返回 True", t.request_stop(), True)
    eq("置位后为 True", t.stop_requested, True)
    t.clear_stop()
    eq("clear_stop 后复位", t.stop_requested, False)

    # **必须能从另一个线程调** —— 它由 IPC 线程直接调，不是事件循环线程。
    result: list[bool] = []
    th = threading.Thread(target=lambda: result.append(t.request_stop()))
    th.start()
    th.join(timeout=5)
    eq("跨线程可用", result, [True])
    t.clear_stop()


def case_guest_health() -> None:
    print("\n[访客模式 1016 归一]")

    async def _run():
        # 访客 + 1016：必须是「正常态」，不能把 1016 当成故障原文报上去。
        t, _ = make_transport(guest=True, status=1016)
        st = await t.health()
        eq("访客 reachable", st.reachable, True)
        eq("访客短标签", st.account_status_short, "访客")

        # 账号模式 + 1016：照实报「未认证」—— 那边它是真故障。
        t2, _ = make_transport(guest=False, status=1016)
        st2 = await t2.health()
        eq("账号模式短标签", st2.account_status_short, "未认证")

        # 账号模式 + 1000
        t3, _ = make_transport(guest=False, status=1000)
        st3 = await t3.health()
        eq("正常账号短标签", st3.account_status_short, "正常")

    asyncio.run(_run())


def case_send_ignores_search() -> None:
    print("\n[send 语义]")

    async def _run():
        t, fake = make_transport()
        out: list[str] = []
        thoughts: list[str] = []
        fake._chunks = [
            FakeChunk(text="你", thought="想"),
            FakeChunk(text="好", thought="了", meta=["cid1", "rid1", "rcid1"]),
        ]
        outcome = await t.send(
            "hi",
            SessionMeta(),
            temporary=True,
            extended_thinking=True,
            search_enabled=True,          # 接受
            on_delta=lambda d: out.append(d),
            on_thought=lambda d: thoughts.append(d),
        )
        eq("正文拼接", outcome.text, "你好")
        eq("思考拼接", outcome.thoughts, "想了")
        eq("delta 回调", "".join(out), "你好")
        eq("thought 回调", "".join(thoughts), "想了")
        eq("cid 回传", outcome.cid, "cid1")
        eq("rid 回传", outcome.rid, "rid1")
        eq("rcid 回传", outcome.rcid, "rcid1")
        check("outcome.ok", outcome.ok)
        eq("temporary 转发", fake.gen_kwargs.get("temporary"), True)
        eq("extended_thinking 转发", fake.gen_kwargs.get("extended_thinking"), True)
        # **关键**：search_enabled 不能被塞进上游调用 —— 上游没有这个参数，
        # 传了要么报 TypeError，要么被 **kwargs 吞掉变成静默无效。
        check(
            "search_enabled 未泄进上游",
            "search_enabled" not in fake.gen_kwargs,
            f"keys={sorted(fake.gen_kwargs)}",
        )

    asyncio.run(_run())


def case_model_strict() -> None:
    print("\n[模型名解析失败不静默退回]")

    async def _run():
        t, _ = make_transport(model="gemini-not-exist")
        try:
            await t.send("hi", SessionMeta())
        except Exception as exc:  # noqa: BLE001
            check("非法模型明确报错", "不可用" in str(exc), str(exc)[:80])
            check("标为 config 错误", getattr(exc, "config", False) is True)
        else:
            check("非法模型明确报错", False, "没有抛异常 —— 会静默用错模型")

        # default / 空 都不解析，直接走上游默认
        t2, fake2 = make_transport(model="default")
        await t2.send("hi", SessionMeta())
        eq("default 不解析", fake2.started_chat_with.get("model"), None)

    asyncio.run(_run())


def case_missing_dependency() -> None:
    print("\n[依赖缺失的错误分类]")

    async def _run():
        # 本 venv 里没装 gemini-webapi，直接让它走 _ensure_client 的真实路径
        t = GeminiRpcTransport(IdentityConfig(guest_mode=True))
        try:
            await t.start()
        except Exception as exc:  # noqa: BLE001
            text = str(exc)
            check(
                "提示装依赖（或已装则初始化失败）",
                "gemini-webapi" in text or "初始化失败" in text,
                text[:100],
            )
            check("标为 config 或 auth", bool(getattr(exc, "config", False) or getattr(exc, "auth", False)))
        else:
            # 装了依赖的情况：能起来就算过
            await t.stop()
            check("依赖已装且能启动", True)

    asyncio.run(_run())


def case_registry() -> None:
    print("\n[注册表]")
    t = create_transport(EngineConfig(transport="gemini_rpc"))
    eq("gemini_rpc 装配", type(t).__name__, "GeminiRpcTransport")
    eq("name", t.name, "gemini_rpc")

    for alias in ("gemini", "g_rpc"):
        tt = create_transport(EngineConfig(transport=alias))
        eq(f"别名 {alias}", type(tt).__name__, "GeminiRpcTransport")

    # 默认档位不受影响
    d = create_transport(EngineConfig())
    eq("默认仍是 deepseek", type(d).__name__, "DeepSeekRpcTransport")

    try:
        create_transport(EngineConfig(transport="nope"))
    except Exception as exc:  # noqa: BLE001
        check("未知档位被拒", "未知的传输档位" in str(exc))
    else:
        check("未知档位被拒", False)


def case_cookie_path() -> None:
    print("\n[cookie 落点]")
    p = default_cookie_path("abc")
    check("在 Gemini-Agent 目录下", "Gemini-Agent" in str(p), str(p))
    eq("文件名带 label", p.name, "cookies-abc.json")
    # 空 label 不能生成 `cookies-.json` 这种半截名字
    eq("空 label 退回 guest", default_cookie_path("").name, "cookies-guest.json")


def case_config_compat() -> None:
    print("\n[配置兼容]")
    # 旧 config.json 里没有 guest_mode / cookie_file —— `_filter` 必须丢掉它们
    # 而不是炸掉，且 dataclass 用默认值。
    cfg = EngineConfig.from_dict({"identity": {"account_label": "x", "unknown_key": 1}})
    eq("旧配置可加载", cfg.identity.account_label, "x")
    eq("guest_mode 默认 False", cfg.identity.guest_mode, False)
    eq("cookie_file 默认空", cfg.identity.cookie_file, "")

    cfg2 = EngineConfig.from_dict(
        {"identity": {"guest_mode": True, "cookie_file": "D:/c.json"}}
    )
    eq("新字段可读", cfg2.identity.guest_mode, True)
    eq("cookie_file 可读", cfg2.identity.cookie_file, "D:/c.json")

    # MaintenanceConfig 新增字段同理
    cfg3 = EngineConfig.from_dict({"maintenance": {"heartbeat_while_idle": True}})
    eq("维护策略可读", cfg3.maintenance.heartbeat_while_idle, True)
    eq("巡检间隔默认", cfg3.maintenance.supervise_interval_ms, 30_000)


def case_background_policy() -> None:
    print("\n[后台心跳策略]")
    t, fake = make_transport()
    # 默认 enabled=False -> 永远不要心跳
    eq("默认不心跳", t._want_heartbeat(), False)

    t._maintenance = MaintenanceConfig(enabled=True, heartbeat_while_idle=False)
    eq("空闲时静默", t._want_heartbeat(), False)
    t._busy = True
    eq("跑任务时开", t._want_heartbeat(), True)
    t._busy = False

    t._maintenance = MaintenanceConfig(
        enabled=True, heartbeat_while_idle=True, respect_active_hours=False
    )
    eq("允许空闲心跳", t._want_heartbeat(), True)

    st = t.background_state()
    check("background_state 有 note", "note" in st)
    check("background_state 有 busy", "busy" in st)


def main() -> int:
    print("=" * 60)
    print("Gemini 传输档位自测")
    print("=" * 60)

    case_account_status()
    case_quota()
    case_stop_flag()
    case_guest_health()
    case_send_ignores_search()
    case_model_strict()
    case_missing_dependency()
    case_registry()
    case_cookie_path()
    case_config_compat()
    case_background_policy()

    print("\n" + "=" * 60)
    if _failures:
        print(f"失败 {len(_failures)} 项：")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())