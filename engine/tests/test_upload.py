"""附件上传自测（离线：不碰网络、不跑真求解）。

守护四件事 —— 每一条都对应交接文档里一个「照抄会炸」的坑：

1. **multipart 用 `CurlMime`，不用 `files=`**。
   curl_cffi 0.16.3 的 `utils.py:785` 对 `files=` 直接 `NotImplementedError`，
   而 `docs/20261018-文件上传交接.md` §3.1.2 的示例代码用的正是 `files=`。
   这条测试会真的构造一次 `CurlMime` 并断言 libcurl 认了这个 form。

2. **`content-type` 必须被删掉**。
   `_headers()` 默认塞 `application/json`；留着它，服务端收到的是一个
   **没有 boundary 的 multipart**，解析必然失败，而且报错信息毫无指向性。

3. **上传的 PoW 用 `UPLOAD_PATH`，不是 `COMPLETION_PATH`**。
   `target_path` 参与签名，两者不能互换。混用表现为 HTTP 403，
   没有任何可读的业务错误。

4. **上传失败不许静默降级**。
   用户明确挂了附件，悄悄丢掉是最坏结果 —— 模型答非所问，界面却一切正常。

用法：
    cd engine && ./.venv/Scripts/python.exe tests/test_upload.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gad_engine.transport import deepseek_rpc as R
from gad_engine.transport.base import TransportError

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    mark = "通过" if cond else "失败"
    extra = f" —— {detail}" if detail and not cond else ""
    print(f"  [{mark}] {name}{extra}")


# ---------- 桩：把网络层换成记录器 ----------


class _FakeResp:
    def __init__(self, status: int = 200, body: dict | None = None, text: str = "") -> None:
        self.status_code = status
        self._body = body if body is not None else {}
        self.content = text.encode("utf-8") if text else b""
        self.text = text

    def json(self) -> dict:
        return self._body


class _EmptyStream:
    """一个立刻结束的 SSE 流（`iter_lines` 一条都不给）。"""

    status_code = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_lines(self):
        return iter(())

    @property
    def content(self):
        return b""


class _FakeSession:
    """只记录调用，不联网。按 URL 后缀分派响应。"""

    def __init__(self, upload_body: dict | None = None) -> None:
        self.calls: list[dict] = []
        self.streams: list[dict] = []
        self._upload_body = upload_body or {
            "code": 0,
            "data": {
                "biz_code": 0,
                "biz_msg": "",
                "biz_data": {
                    "id": "file-abc123",
                    "status": 0,
                    "file_name": "hello.txt",
                    "file_size": 21,
                },
            },
        }

    def post(self, url: str, **kw) -> _FakeResp:
        self.calls.append({"url": url, **kw})
        if url.endswith(R.UPLOAD_PATH):
            return _FakeResp(body=self._upload_body)
        if url.endswith("/api/v0/chat_session/create"):
            return _FakeResp(body={"code": 0, "data": {"biz_code": 0, "biz_data": {"id": "cid-1"}}})
        if url.endswith(R.CHALLENGE_PATH):
            return _FakeResp(
                body={
                    "code": 0,
                    "data": {
                        "biz_code": 0,
                        "biz_data": {
                            "challenge": {
                                "algorithm": "DeepSeekHashV1",
                                "challenge": "00" * 32,
                                "salt": "s",
                                "difficulty": 100,
                                "signature": "sig",
                                "expire_at": 1792254708602,
                            }
                        },
                    },
                }
            )
        raise AssertionError(f"桩 session 收到了未预期的 URL：{url}")

    def stream(self, method: str, url: str, **kw) -> _EmptyStream:
        self.streams.append({"method": method, "url": url, **kw})
        return _EmptyStream()

    def close(self) -> None:
        pass


def make_transport() -> R.DeepSeekRpcTransport:
    return R.DeepSeekRpcTransport(token="t", device_id_path=Path(tempfile.gettempdir()) / "gad-test-device")


class _SolveSpy:
    """替换 `solve()`：立即返回，并记下收到的 target_path。"""

    def __init__(self) -> None:
        self.targets: list[str] = []
        self._real = R.solve

    def __call__(self, ch, target_path, **kw):
        self.targets.append(target_path)
        return {
            "algorithm": "DeepSeekHashV1",
            "challenge": "00" * 32,
            "salt": "s",
            "answer": 7,
            "signature": "sig",
            "target_path": target_path,
            "_elapsed_ms": 1.0,
            "_mode": "single",
            "_workers": 1,
            "_trials": 8,
        }


def with_solve_spy(fn):
    """临时把模块级 `solve` 换成桩，跑完还原。"""
    spy = _SolveSpy()
    orig = R.solve
    R.solve = spy  # type: ignore[assignment]
    try:
        fn(spy)
    finally:
        R.solve = orig  # type: ignore[assignment]


def write_tmp(content: str = "这是测试文件内容", name: str = "hello.txt") -> Path:
    d = Path(tempfile.mkdtemp(prefix="gad-upload-"))
    p = d / name
    p.write_text(content, encoding="utf-8")
    return p


# ---------- 1. 成功路径 ----------


def scenario_success() -> None:
    print("\n[1] 上传成功路径")

    def body(spy: _SolveSpy) -> None:
        tp = make_transport()
        s = _FakeSession()
        p = write_tmp()
        biz = tp._upload_file(s, "tok", R.BASE + "/a/chat/s/x", str(p))

        check("返回 biz_data", biz.get("id") == "file-abc123", repr(biz))
        check(
            "PoW 用的是 UPLOAD_PATH",
            spy.targets == [R.UPLOAD_PATH],
            f"实际 {spy.targets}",
        )

        ch_calls = [c for c in s.calls if c["url"].endswith(R.CHALLENGE_PATH)]
        check("先取了一次 challenge", len(ch_calls) == 1, f"{len(ch_calls)} 次")
        check(
            "challenge 的 target_path 是 UPLOAD_PATH",
            ch_calls and ch_calls[0]["json"] == {"target_path": R.UPLOAD_PATH},
            repr(ch_calls[0]["json"]) if ch_calls else "无",
        )

        up = [c for c in s.calls if c["url"].endswith(R.UPLOAD_PATH)]
        check("发了一次上传", len(up) == 1)
        if not up:
            return
        c = up[0]

        # ★ 坑 2：content-type 必须被删
        check(
            "content-type 已从上传头里移除",
            "content-type" not in {k.lower() for k in c["headers"]},
            f"实际有：{[k for k in c['headers'] if k.lower() == 'content-type']}",
        )
        # ★ 坑 1：用 multipart 而不是 files
        check("用了 multipart（不是 files=）", "multipart" in c and "files" not in c)
        mp = c.get("multipart")
        check("multipart 是 CurlMime 实例", type(mp).__name__ == "CurlMime", type(mp).__name__)

        # 三个上传专属头
        h = {k.lower(): v for k, v in c["headers"].items()}
        check("带了 x-ds-pow-response", "x-ds-pow-response" in h)
        check("x-thinking-enabled = 0（未开思考）", h.get("x-thinking-enabled") == "0", h.get("x-thinking-enabled"))
        check("x-model-type = default", h.get("x-model-type") == "default", h.get("x-model-type"))
        check(
            "x-file-size 是字节数",
            h.get("x-file-size") == str(p.stat().st_size),
            f"{h.get('x-file-size')} vs {p.stat().st_size}",
        )

        # PoW 头能被 base64 解开，且 target_path 正确
        import base64 as b64
        import json as _json

        try:
            pow_obj = _json.loads(b64.b64decode(h["x-ds-pow-response"]).decode("utf-8"))
        # `b64decode` 的 binascii.Error、`decode` 的 UnicodeDecodeError、
        # `json.loads` 的 JSONDecodeError 全都是 ValueError 的子类 ——
        # 用具体基类而不是 Exception，既满足「断言失败就报断言失败」的意图，
        # 也不会把 KeyError / AttributeError 这类真 bug 一起吞成测试失败。
        except ValueError as exc:
            check("PoW 头可解析", False, str(exc))
            return
        check("PoW 头可解析", True)
        check(
            "PoW 头里的 target_path 是 UPLOAD_PATH",
            pow_obj.get("target_path") == R.UPLOAD_PATH,
            repr(pow_obj.get("target_path")),
        )
        check("PoW 头里没有 `_` 前缀的计量字段", not any(k.startswith("_") for k in pow_obj), repr(list(pow_obj)))

    with_solve_spy(body)


# ---------- 2. 失败不降级 ----------


def scenario_failures() -> None:
    print("\n[2] 失败路径 —— 必须抛错，不许静默跳过附件")

    def body(spy: _SolveSpy) -> None:
        tp = make_transport()

        # 2a. 业务码失败
        s = _FakeSession(
            upload_body={
                "code": 0,
                "data": {"biz_code": 7, "biz_msg": "file too large", "biz_data": {}},
            }
        )
        p = write_tmp()
        try:
            tp._upload_file(s, "tok", R.BASE + "/", str(p))
            check("biz_code 非 0 时抛 TransportError", False, "没有抛")
        except TransportError as exc:
            check("biz_code 非 0 时抛 TransportError", True)
            check("错误里带上了 biz 信息", "biz_code" in str(exc), str(exc)[:120])

        # 2b. 成功码但没有 id（响应结构变了）
        s2 = _FakeSession(upload_body={"code": 0, "data": {"biz_code": 0, "biz_data": {}}})
        try:
            tp._upload_file(s2, "tok", R.BASE + "/", str(p))
            check("缺 id 时抛 TransportError", False, "没有抛")
        except TransportError as exc:
            check("缺 id 时抛 TransportError", True)
            check("缺 id 报的是「结构可能已变」", "结构" in str(exc), str(exc)[:120])

        # 2c. 文件不存在 —— 必须在**发请求之前**就拦下（否则白烧一次 PoW）
        s3 = _FakeSession()
        try:
            tp._upload_file(s3, "tok", R.BASE + "/", str(p.parent / "不存在.txt"))
            check("文件不存在时抛 TransportError", False, "没有抛")
        except TransportError:
            check("文件不存在时抛 TransportError", True)
        check("文件不存在时不发任何请求", s3.calls == [], f"{len(s3.calls)} 次")

        # 2d. 空文件 —— 同理
        empty = write_tmp("", "empty.txt")
        s4 = _FakeSession()
        try:
            tp._upload_file(s4, "tok", R.BASE + "/", str(empty))
            check("空文件时抛 TransportError", False, "没有抛")
        except TransportError:
            check("空文件时抛 TransportError", True)
        check("空文件时不发任何请求", s4.calls == [], f"{len(s4.calls)} 次")

    with_solve_spy(body)


# ---------- 3. completion 请求体 ----------


def scenario_payload() -> None:
    print("\n[3] completion 请求体的 ref_file_ids")
    # 直接读源码层面的构造太脆，改为真跑一次 `_stream_completion` 的
    # payload 构造？—— 那要 mock SSE 流，成本远高于收益。
    # 这里只做**结构断言**：`ref_file_ids` 字段存在且默认是空数组，
    # 且签名接受 `ref_file_ids` 关键字参数。
    import inspect

    from gad_engine.session import SessionMeta

    sig = inspect.signature(R.DeepSeekRpcTransport._stream_completion)
    check(
        "_stream_completion 接受 ref_file_ids",
        "ref_file_ids" in sig.parameters,
        str(list(sig.parameters)),
    )
    check(
        "ref_file_ids 是 keyword-only 且有默认值",
        sig.parameters["ref_file_ids"].kind is inspect.Parameter.KEYWORD_ONLY
        and sig.parameters["ref_file_ids"].default is None,
    )

    sig2 = inspect.signature(R.DeepSeekRpcTransport._upload_file)
    check("_upload_file 存在且可调用", callable(R.DeepSeekRpcTransport._upload_file))
    check(
        "_upload_file 的 thinking / model 有默认值",
        sig2.parameters["thinking"].default is False
        and sig2.parameters["model"].default == "default",
    )

    # ★ 真跑一次 payload 构造 —— 签名对了不等于发出去的 body 对了。
    #   用桩流（空 SSE）捕获 `json=` 参数，直接看服务端会收到什么。
    captured: dict = {}

    class _EmptyStream:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def iter_lines(self):
            return iter(())

        @property
        def content(self):
            return b""

    class _StreamSession:
        def stream(self, method, url, **kw):
            captured.update({"method": method, "url": url, **kw})
            return _EmptyStream()

    tp = make_transport()
    tp._device_id = "dev"
    session = SessionMeta(temporary=True)
    session.cid = "cid-1"
    session.rid = "1"
    tp._stream_completion(
        _StreamSession(),
        "tok",
        R.BASE + "/a/chat/s/cid-1",
        "pow-header",
        "cid-1",
        "这个文件里写了什么？",
        session,
        False,
        False,
        lambda k, p: None,
        ref_file_ids=["file-abc123", "file-def456"],
    )
    payload = captured.get("json") or {}
    check(
        "completion 请求体带 ref_file_ids",
        payload.get("ref_file_ids") == ["file-abc123", "file-def456"],
        repr(payload.get("ref_file_ids")),
    )
    check(
        "ref_file_ids 是 list 不是 str",
        isinstance(payload.get("ref_file_ids"), list),
        type(payload.get("ref_file_ids")).__name__,
    )

    # 不传时也必须是空数组（真实客户端一直带这个字段）
    captured.clear()
    tp._stream_completion(
        _StreamSession(),
        "tok",
        R.BASE + "/a/chat/s/cid-1",
        "pow-header",
        "cid-1",
        "hi",
        session,
        False,
        False,
        lambda k, p: None,
    )
    check(
        "无附件时 ref_file_ids 是空数组（不是缺字段）",
        (captured.get("json") or {}).get("ref_file_ids") == [],
        repr((captured.get("json") or {}).get("ref_file_ids")),
    )

    # 常量正确性
    check("UPLOAD_PATH 正确", R.UPLOAD_PATH == "/api/v0/file/upload_file", R.UPLOAD_PATH)
    check("FETCH_FILES_PATH 正确", R.FETCH_FILES_PATH == "/api/v0/file/fetch_files", R.FETCH_FILES_PATH)
    check(
        "两个 PoW 目标路径不同（不能复用）",
        R.UPLOAD_PATH != R.COMPLETION_PATH,
    )


# ---------- 4. send() 不再无条件抛错 ----------


def scenario_send_gate() -> None:
    print("\n[4] send() 的 files 分支已移除硬抛错")
    import inspect

    src = inspect.getsource(R.DeepSeekRpcTransport.send)
    check(
        "send() 里不再有「附件上传尚未实现」",
        "附件上传尚未实现" not in src,
    )
    check(
        "send() 把 files 传给了后台线程",
        "list(files) if files else []" in src,
    )
    # 真实发送闸门必须还在 —— 这次改动不能顺手把防封禁的闸门弄丢
    check(
        "真实发送闸门仍在（ALLOW_LIVE_ENV）",
        "ALLOW_LIVE_ENV" in src,
    )


# ---------- 5. 限额兜底 ----------


def scenario_limits() -> None:
    print("\n[5] 限额兜底 —— 超限时必须在发请求前拦下")

    import asyncio
    import os

    from gad_engine.session import SessionMeta

    tp = make_transport()
    tp._session = object()  # 绕过 _require_session（真发闸门在此之前）
    session = SessionMeta(temporary=True)

    # 闸门要求 ALLOW_LIVE_AUTH=1 —— 这里要验的是**闸门之后**的限额检查，
    # 所以临时置位。限额检查在真正发请求之前，不会产生任何网络调用。
    old = os.environ.get(R.ALLOW_LIVE_ENV)
    os.environ[R.ALLOW_LIVE_ENV] = "1"
    try:
        # 5a. 条数超限
        try:
            asyncio.run(
                tp.send(
                    "x",
                    session,
                    temporary=True,
                    files=["a.txt"] * (R.UPLOAD_MAX_COUNT + 1),
                )
            )
            check("条数超限时抛 TransportError", False, "没有抛")
        except TransportError as exc:
            check("条数超限时抛 TransportError", True)
            check("错误里给了上限数字", str(R.UPLOAD_MAX_COUNT) in str(exc), str(exc)[:100])

        # 5b. 单文件超限（用稀疏文件，不真占 100MB 磁盘）
        d = Path(tempfile.mkdtemp(prefix="gad-upload-big-"))
        big = d / "big.bin"
        with open(big, "wb") as fh:
            fh.truncate(R.UPLOAD_MAX_BYTES + 1)
        try:
            asyncio.run(tp.send("x", session, temporary=True, files=[str(big)]))
            check("单文件超限时抛 TransportError", False, "没有抛")
        except TransportError as exc:
            check("单文件超限时抛 TransportError", True)
            check("错误里带上文件名", "big.bin" in str(exc), str(exc)[:120])
        finally:
            big.unlink(missing_ok=True)

    finally:
        if old is None:
            os.environ.pop(R.ALLOW_LIVE_ENV, None)
        else:
            os.environ[R.ALLOW_LIVE_ENV] = old

    # 5c. 正常文件走**完整**的 send() 路径（桩 session 提供 post + stream），
    #     断言：① 限额放行；② 上传真的发生；③ 文件 id 真的进了 completion 请求体。
    #
    #     这一条是整组里唯一「端到端」的 —— 前面几条都只验了单点。
    def body(spy: _SolveSpy) -> None:
        tp2 = make_transport()
        s = _FakeSession()
        tp2._session = s
        ok_file = write_tmp("正常内容", "ok.txt")

        old2 = os.environ.get(R.ALLOW_LIVE_ENV)
        os.environ[R.ALLOW_LIVE_ENV] = "1"
        try:
            outcome = asyncio.run(
                tp2.send("这个文件里写了什么？", SessionMeta(temporary=True), temporary=True,
                         files=[str(ok_file)])
            )
        finally:
            if old2 is None:
                os.environ.pop(R.ALLOW_LIVE_ENV, None)
            else:
                os.environ[R.ALLOW_LIVE_ENV] = old2

        ups = [c for c in s.calls if c["url"].endswith(R.UPLOAD_PATH)]
        check("正常文件不被限额拦下，且真的发了上传", len(ups) == 1, f"{len(ups)} 次上传")

        comp = [c for c in s.streams if c["url"].endswith(R.COMPLETION_PATH)]
        check("真的发了一次 completion", len(comp) == 1, f"{len(comp)} 次")
        if comp:
            refs = (comp[0].get("json") or {}).get("ref_file_ids")
            check(
                "上传得到的 file id 进了 completion 的 ref_file_ids",
                refs == ["file-abc123"],
                repr(refs),
            )
        check("outcome 记了 upload_ms", outcome.upload_ms >= 0.0, str(outcome.upload_ms))
        check(
            "outcome 记了 uploaded_files",
            len(outcome.uploaded_files) == 1 and outcome.uploaded_files[0]["id"] == "file-abc123",
            repr(outcome.uploaded_files),
        )
        # PoW：上传用 UPLOAD_PATH、发送用 COMPLETION_PATH，两份都在
        check(
            "两份 PoW 各按自己的 target_path 求解",
            sorted(spy.targets) == sorted([R.UPLOAD_PATH, R.COMPLETION_PATH]),
            repr(spy.targets),
        )

    with_solve_spy(body)


def main() -> int:
    print("=" * 60)
    print("附件上传自测（离线）")
    print("=" * 60)

    scenario_success()
    scenario_failures()
    scenario_payload()
    scenario_send_gate()
    scenario_limits()

    print("\n" + "=" * 60)
    print(f"通过 {len(PASS)} / 失败 {len(FAIL)}")
    if FAIL:
        for name in FAIL:
            print(f"  [失败] {name}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
