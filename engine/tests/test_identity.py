"""L1 身份层自测（**不需要浏览器**）。

浏览器相关的部分（真拉起 Chrome / CDP 读取）在 `tools/smoke_identity.py` 里做端到端。

用法：
    cd engine && ./.venv/Scripts/python.exe tests/test_identity.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gad_engine import identity as I
from gad_engine.browser_login import (
    BrowserLoginError,
    cdp_alive,
    extract_email,
    find_browser,
)
from gad_engine.config import EngineConfig

PASS: list[str] = []
FAIL: list[str] = []

GOOD = "TESTONLYfake" + "x" * 52  # 64 字符，与实测 token 长度一致；前缀是假值


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'通过' if cond else '失败'}] {name}" + (f" —— {detail}" if detail and not cond else ""))


def main() -> int:
    print("=" * 70)
    print("身份层自测（无浏览器）")
    print("=" * 70)

    # ---------- 1. token 归一化 ----------
    print("\n[1] token 归一化（用户手粘时会遇到的各种形态）")
    check("裸串", I.normalize_token(GOOD) == GOOD)
    check("JSON 形态（localStorage 真实形态）", I.normalize_token(f'{{"value":"{GOOD}"}}') == GOOD)
    check("带双引号", I.normalize_token(f'"{GOOD}"') == GOOD)
    check("带单引号", I.normalize_token(f"'{GOOD}'") == GOOD)
    check("带 Bearer 前缀", I.normalize_token(f"Bearer {GOOD}") == GOOD)
    check("dict 输入", I.normalize_token({"value": GOOD}) == GOOD)
    check("空输入 → 空串", I.normalize_token("") == "" and I.normalize_token(None) == "")
    check("前后空白被去掉", I.normalize_token(f"  {GOOD}  \n") == GOOD)

    # 未登录时 localStorage.userToken 就是这一串。**曾经被当成 token 用**：
    # 解析成功 → p.value 为 null → 旧代码落到 `return String(raw)` 把整段
    # JSON（30 字节）返回，一路绿灯，最后把原来那份可用的凭据覆盖掉。
    # 这一条是那次数据损坏的回归测试，别删。
    check(
        "未登录的 localStorage 原样 → 空串（不是整段 JSON）",
        I.normalize_token('{"value":null,"__version":"0"}') == "",
        repr(I.normalize_token('{"value":null,"__version":"0"}')),
    )
    check("value 为空串 → 空串", I.normalize_token('{"value":"","__version":"0"}') == "")
    check("没有 value 键 → 空串", I.normalize_token('{"__version":"0"}') == "")
    check(
        "长得像 JSON 但解析不了 → 仍按裸串（不能把这条退路弄丢）",
        I.normalize_token("{不是合法 json") == "{不是合法 json",
    )

    # ---------- 2. 存取 ----------
    print("\n[2] token 存取")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / ".token"
        check("不存在时读出空串", I.load_token(p) == "")

        saved = I.save_token(GOOD, p)
        check("保存成功", saved.exists())
        check("读回一致", I.load_token(p) == GOOD)
        check("保存时也走归一化", I.load_token(p) == GOOD)

        # 保存 JSON 形态，读回应是裸串
        I.save_token(f'{{"value":"{GOOD}"}}', p)
        check("存 JSON 形态 → 读回裸串", I.load_token(p) == GOOD)

        check("清除成功", I.clear_token(p) and not p.exists())
        check("清除不存在的文件返回 False", I.clear_token(p) is False)

    # ---------- 3. 手动粘贴兜底 ----------
    print("\n[3] 手动粘贴兜底")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / ".token"
        path, tok = I.save_manual_token(f'{{"value":"{GOOD}"}}', p)
        check("粘贴 JSON 形态可用", tok == GOOD and path.exists())
        try:
            I.save_manual_token("   ", p)
            check("空粘贴应报错", False, "居然没报错")
        except BrowserLoginError:
            check("空粘贴报错", True)
        try:
            I.save_manual_token("short", p)
            check("过短粘贴应报错", False, "居然没报错")
        except BrowserLoginError:
            check("过短粘贴报错", True)

    # ---------- 4. 解析优先级 ----------
    print("\n[4] 解析优先级（显式 → 文件 → CDP）")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / ".token"
        I.save_token("CACHED" + "x" * 58, p)

        tok, src = I.resolve_token(explicit=GOOD, token_file=p, cdp_port=None)
        check("显式优先于文件", tok == GOOD and "显式" in src, f"{src}")

        tok, src = I.resolve_token(token_file=p, cdp_port=None)
        check("无显式时用文件", tok.startswith("CACHED") and "文件" in src, f"{src}")

        p.unlink()
        try:
            I.resolve_token(token_file=p, cdp_port=None)
            check("都无 → 报错", False, "居然没报错")
        except BrowserLoginError:
            check("都无 → 报错", True)

    # ---------- 5. 浏览器发现 ----------
    print("\n[5] 浏览器发现")
    try:
        info = find_browser("")
        check(f"找到浏览器：{info.name}", bool(info.path), info.path)
    except BrowserLoginError as exc:
        check("找到浏览器", False, str(exc))
    try:
        find_browser(r"C:\definitely\not\here.exe")
        check("显式路径不存在时应报错", False, "居然没报错")
    except BrowserLoginError:
        check("显式路径不存在 → 报错", True)

    # ---------- 6. CDP 探测（无浏览器时应安全返回 None） ----------
    print("\n[6] CDP 探测")
    check("端口无服务 → None（不抛异常）", cdp_alive(59999, timeout=0.5) is None)

    # ---------- 7. 邮箱提取（账号名兜底） ----------
    print("\n[7] 账号名兜底")
    check("从文本提取邮箱", extract_email("登录为 user@example.com 的账号") == "user@example.com")
    check("无邮箱 → 空串", extract_email("没有邮箱的文本") == "")

    # ---------- 8. 与配置层对接 ----------
    print("\n[8] 与配置层对接")
    cfg = EngineConfig()
    check("默认 token 文件 = identity 默认", cfg.resolved_token_file() == I.DEFAULT_TOKEN_FILE, str(cfg.resolved_token_file()))
    cfg.identity.token_file = r"C:\custom\tok.txt"
    check("配置可覆盖 token 路径", str(cfg.resolved_token_file()).endswith("tok.txt"), str(cfg.resolved_token_file()))

    # ---------- 9. ensure_token 的降级行为 ----------
    print("\n[9] ensure_token 降级行为（不允许拉浏览器时）")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / ".token"
        res = I.ensure_token(token_file=p, cdp_port=None, allow_browser=False)
        check("无凭据且不允许拉浏览器 → 明确失败", not res.ok and "未允许拉起浏览器" in res.detail, res.detail)

    # ---------- 10. 验证必须是**真的** ----------
    #
    # 这里钉住两个曾经同时存在、并且互相掩护的缺陷：
    #   ① `validate_token` 里的 `asyncio.run()` 在事件循环里必然抛错，而它的
    #      调用点（identity.set / identity.refresh / identity.login.finish）
    #      **全在 async handle 里** —— 于是所有验证路径都「验证不通过」，
    #      看着像凭据坏了，其实是代码坏了。
    #   ② `_probe_once` 只看 HTTP 状态码，而 DeepSeek 把鉴权失败报成
    #      **200 + 业务码 40003** —— 于是它对任何非空串都返回「通过」，
    #      实测 'x'*64 / 'y'*64 / 随机 32 位串全部假通过。
    # ① 让验证永远失败、② 让验证永远成功，两者叠加的净效果是「验证这一步
    # 完全没用」，而坏凭据会一路绿灯把好凭据覆盖掉（真实发生过的数据损坏）。
    print("\n[10] 验证必须是真的（事件循环 + 业务码 40003）")

    import asyncio

    from gad_engine.transport.deepseek_rpc import (
        AUTH_FAILED_BIZ_CODE,
        DeepSeekRpcTransport,
        TransportError,
    )

    class _Resp:
        def __init__(self, status: int, body, raw: str | None = None) -> None:
            self.status_code = status
            self._body = body
            self.text = raw if raw is not None else str(body)

        def json(self):
            if isinstance(self._body, Exception):
                raise self._body
            return self._body

    class _Sess:
        def __init__(self, resp: _Resp) -> None:
            self._resp = resp

        def post(self, *a, **k):
            return self._resp

    # ★ 闸门回归钉子（2026-10-17 封号事故）
    #
    # 引擎的 `_probe_once` 现在**默认拒绝**打真实接口。当年就是为了让
    # 「坏凭据必须被拒」这条断言有真凭据可依，反复拿真实接口去试 ——
    # 包括故意构造 'x'*64 / 'y'*64 / 随机串去撞鉴权。结果账号 shaomo17 被
    # DeepSeek 以「违反用户政策」封禁到 2026-10-20。
    #
    # 所以这里先验「默认必须拒绝」，**再**放行本节后续的假会话测试。
    # 顺序不能反：放行之后再验，就验不出「默认是关的」了。
    import os as _os

    from gad_engine.transport.deepseek_rpc import ALLOW_LIVE_ENV

    _saved_env = _os.environ.pop(ALLOW_LIVE_ENV, None)
    try:
        _tp_gate = DeepSeekRpcTransport(token="x" * 64, timeout=1)
        _tp_gate._require_session = lambda: _Sess(_Resp(200, {"code": 0, "data": {}}))
        try:
            _tp_gate._probe_once("x" * 64)
            check("★ 默认不放行真实鉴权探测（封号事故回归钉子）", False,
                  "竟然放行了 —— 闸门失效")
        except TransportError as exc:
            check("★ 默认不放行真实鉴权探测（封号事故回归钉子）",
                  "已阻止真实鉴权探测" in str(exc), str(exc)[:140])
    finally:
        if _saved_env is not None:
            _os.environ[ALLOW_LIVE_ENV] = _saved_env

    # 本节以下全用**假会话**（`tp._require_session` 被换掉），不会发真实请求。
    # 显式过闸只是为了让执行流走到判定逻辑那一层。
    _os.environ[ALLOW_LIVE_ENV] = "1"

    def probe(status: int, body, raw: str | None = None):
        tp = DeepSeekRpcTransport(token=GOOD, timeout=1)
        tp._require_session = lambda: _Sess(_Resp(status, body, raw))
        try:
            tp._probe_once(GOOD)
            return None
        except TransportError as exc:
            return exc

    check("正常响应（code=0）→ 通过", probe(200, {"code": 0, "data": {"algorithm": "x"}}) is None)

    bad = probe(
        200,
        {"code": AUTH_FAILED_BIZ_CODE, "msg": "Authorization Failed (invalid token)", "data": None},
    )
    check(
        "★ HTTP 200 + 业务码 40003 → 判为鉴权失败（本次数据损坏的根因）",
        bad is not None and bad.auth is True,
        repr(bad),
    )

    e401 = probe(401, {})
    check("HTTP 401 → 判为鉴权失败", e401 is not None and e401.auth is True)

    e500 = probe(500, {})
    check("HTTP 500 → 失败，但**不**算鉴权问题（不能污染熔断分类）",
          e500 is not None and e500.auth is False)

    check("响应不是 JSON → 不误判为鉴权失败",
          probe(200, ValueError("no json"), raw="<html>") is None)

    async def _in_loop_async() -> tuple[bool, str]:
        # 空 token → 不发网络请求、直接明确失败；关键是**不能**抛 asyncio 错
        return await I.validate_token_async("")

    ok_a, detail_a = asyncio.run(_in_loop_async())
    check("★ 在事件循环里调 validate_token_async 不炸",
          ok_a is False and "验证失败" in detail_a, detail_a)

    async def _in_loop_sync() -> tuple[bool, str]:
        return I.validate_token("")

    ok_s, detail_s = asyncio.run(_in_loop_sync())
    check("同步版在事件循环里也不炸（自动走独立线程）",
          ok_s is False and "验证失败" in detail_s, detail_s)

    check("CDP 读取有协程形态（identity.refresh 在循环里必须用它）",
          hasattr(I, "read_token_from_cdp_async"))

    print("\n" + "-" * 70)
    print(f"通过 {len(PASS)} · 失败 {len(FAIL)}")
    for f in FAIL:
        print(f"  失败：{f}")
    print("  [身份层自测通过]" if not FAIL else "  [身份层自测未通过]")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
