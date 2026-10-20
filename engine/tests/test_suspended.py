"""账号处置（封禁）识别自测。

**全程离线**：不发任何真实请求。`_probe_once` 的假会话返回伪造响应，
只验判定逻辑与状态暴露。

钉死：
  1. `looks_suspended` 能认出封禁原文，且不把「token 无效」误判成封禁
  2. 探测响应为封禁文案 → `TransportError.suspended=True`
  3. `verify_credentials` 失败后 `background_state()` 暴露处置标记与原文
  4. 「token 无效」不会被标成处置（两者都是 40003，必须分开）
  5. 探测成功后清掉旧的处置标记

用法：
    cd engine && ./.venv/Scripts/python.exe tests/test_suspended.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gad_engine.transport.base import TransportError
from gad_engine.transport.deepseek_rpc import (
    ALLOW_LIVE_ENV,
    DeepSeekRpcTransport,
    _Acc,
    _SseParser,
    describe_biz_failure,
    looks_suspended,
    mute_until_text,
)

PASS: list[str] = []
FAIL: list[str] = []

BAN_TEXT = (
    "Due to violation of user policies, your account has been suspended "
    "until October 20, 2026 00:53."
)


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    mark = "通过" if cond else "失败"
    extra = f" — {detail}" if detail and not cond else ""
    print(f"  [{mark}] {name}{extra}")


class _Resp:
    status_code = 200

    def __init__(self, body: dict) -> None:
        self._body = body

    def json(self) -> dict:
        return self._body


class _Sess:
    """假 curl_cffi 会话：只记录调用，绝不联网。"""

    def __init__(self, body: dict) -> None:
        self.body = body
        self.calls: list[str] = []

    def post(self, url, **kw):
        self.calls.append(url)
        return _Resp(self.body)


def make_transport(body: dict) -> tuple[DeepSeekRpcTransport, _Sess]:
    tp = DeepSeekRpcTransport(token="fake-token-for-offline-test")
    sess = _Sess(body)
    tp._require_session = lambda: sess  # type: ignore[assignment]
    return tp, sess


def main() -> int:
    print("[1] 文本判定")
    check("封禁原文识别", looks_suspended(BAN_TEXT))
    check("中文处置文案识别", looks_suspended("账号已被封禁"))
    check("token 无效不算处置", not looks_suspended("Authorization Failed (invalid token)"))
    check("空文本不算处置", not looks_suspended(""))

    os.environ[ALLOW_LIVE_ENV] = "1"  # 放行闸门，但会话是假的，不会真打接口

    print("\n[2] 探测响应 = 封禁")
    tp, sess = make_transport(
        {"code": 40003, "msg": BAN_TEXT, "data": None}
    )
    ok, detail = asyncio.run(tp.verify_credentials())
    check("验证失败", ok is False)
    check("错误文本标了处置", "账号被处置" in detail, detail)
    check("错误文本保留原文", "suspended" in detail, detail)
    check("只发了一次请求", len(sess.calls) == 1, str(sess.calls))
    bg = tp.background_state()
    check("状态暴露处置标记", bg["account_suspended"] is True)
    check("状态暴露原文", "suspended" in bg["suspended_detail"])

    print("\n[3] 探测响应 = token 无效（不得误判）")
    tp2, _ = make_transport(
        {"code": 40003, "msg": "Authorization Failed (invalid token)", "data": None}
    )
    ok2, detail2 = asyncio.run(tp2.verify_credentials())
    check("验证失败", ok2 is False)
    check("未标成处置", tp2.background_state()["account_suspended"] is False)
    check("错误文本仍是鉴权失败", "探测被拒" in detail2, detail2)

    print("\n[4] 探测成功清掉旧标记")
    tp3, _ = make_transport({"code": 0, "msg": "", "data": {"challenge": "x"}})
    tp3._account_suspended = True
    tp3._suspended_detail = "旧证据"
    ok3, detail3 = asyncio.run(tp3.verify_credentials())
    check("验证通过", ok3 is True, detail3)
    check("处置标记被清除", tp3.background_state()["account_suspended"] is False)

    print("\n[5] health 透出处置")
    tp4, _ = make_transport({"code": 40003, "msg": BAN_TEXT, "data": None})
    asyncio.run(tp4.verify_credentials())
    st = asyncio.run(tp4.health())
    check("health 短标签为账号被处置", st.account_status_short == "账号被处置", st.account_status_short)
    check("health 文本含原文", "suspended" in st.account_status_text, st.account_status_text)

    print("\n[6] 禁言形态（2026-10-17 10:10 真实抓包原文）")
    # 真实响应不是 SSE，而是 HTTP 200 + 一段裸 JSON。文案里既没有
    # suspended 也没有 violation，只写 `user is muted` + `mute_until`。
    # 这一形态之前被整条静默丢弃，最终报「未收到正文（流为空）」。
    check("user is muted 算处置", looks_suspended("user is muted"))
    muted_ok, muted_detail = describe_biz_failure(
        {"biz_msg": "user is muted", "biz_data": {"is_muted": 1, "mute_until": 1792428836.75}}
    )
    check("禁言判为处置", muted_ok is True)
    check("细节带可读截止时间", "2026-10-20 00:53" in muted_detail, muted_detail)
    check("无 mute_until 时不编造时间", mute_until_text({"is_muted": 1}) == "")
    check("mute_until 非数字时降级为空", mute_until_text({"mute_until": "soon"}) == "")

    raw = (
        '{"code":0,"msg":"","data":{"biz_code":5,"biz_msg":"user is muted",'
        '"biz_data":{"is_muted":1,"mute_until":1792428836.75}}}'
    )
    parser = _SseParser(_Acc(), lambda kind, payload: None)
    raised: TransportError | None = None
    try:
        parser.feed(None, raw)
    except TransportError as exc:
        raised = exc
    check("裸 JSON 业务错误被解析器捕获", raised is not None)
    if raised is not None:
        check("异常标了 suspended", raised.suspended is True)
        check("异常文案含禁言原文", "user is muted" in str(raised), str(raised))

    print("\n" + "-" * 60)
    print(f"通过 {len(PASS)} · 失败 {len(FAIL)}")
    if FAIL:
        print("  [账号处置自测未通过]")
        return 1
    print("  [账号处置自测通过]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
