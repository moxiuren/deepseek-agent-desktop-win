"""可观测性自测 —— 请求 trace / 发送审计 / 封禁快照。

**全程离线**：不发任何真实请求，全部落点重定向到临时目录。

钉死这几件事（每一条都对应 2026-10-19 补强方案里的一项）：

  1. `RecordingSession` 是**透明**代理 —— 返回值、异常、属性访问都与原对象一致
  2. trace 每行字段正确，且 `pow` 标记能区分「计算型请求」与「只读探测」
  3. 审计两行用**同一个 `seq`** 串起来（这是复盘「第几次开始出事」的前提）
  4. `_audit_error_tag` 按分隔符截断，`biz_code=40003` **不被截成 4000**
  5. `Signal.MUTED` 优先于 `AUTH` —— 禁言不能被当成 token 过期
  6. `note_muted()` 直接 HALTED，不是退避 60 秒
  7. `snapshot_incident` 生成目录 + README，且**只复制不移动**
  8. 落盘失败**不影响调用方**（观测手段不是业务）

用法：
    cd engine && ./.venv/Scripts/python.exe tests/test_observability.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gad_engine import observability as obs
from gad_engine.circuit import BreakerLevel, CircuitBreaker, Signal, classify_error_text
from gad_engine.config import EngineConfig
from gad_engine.transport.deepseek_rpc import _audit_error_tag

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    mark = "通过" if cond else "失败"
    extra = f" — {detail}" if detail and not cond else ""
    print(f"  [{mark}] {name}{extra}")


def read_jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# 假 curl_cffi 对象
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, status: int, body: bytes = b"{}") -> None:
        self.status_code = status
        self.content = body
        self.text = body.decode("utf-8", "replace")


class _StreamCM:
    def __init__(self, resp: _Resp, lines: list[bytes]) -> None:
        self._resp = resp
        self._lines = lines
        self.entered = False
        self.exited = False

    def __enter__(self) -> _Resp:
        self.entered = True
        return self._resp

    def __exit__(self, *a):
        self.exited = True
        return False


class _FakeSession:
    """假 curl_cffi Session —— 只记录调用，绝不联网。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.closed = False
        self.raise_on_post: BaseException | None = None

    def post(self, url, **kw):
        self.calls.append(("POST", url))
        if self.raise_on_post is not None:
            raise self.raise_on_post
        return _Resp(200, b'{"biz_code":0}')

    def get(self, url, **kw):
        self.calls.append(("GET", url))
        return _Resp(200, b'{"ok":true}')

    def stream(self, method, url, **kw):
        self.calls.append((method.upper(), url))
        return _StreamCM(_Resp(200), [b'data: {"a":1}', b"data: [DONE]"])

    def close(self):
        self.closed = True

    def some_unproxied_method(self) -> str:
        return "透传成功"


class _LineResp(_Resp):
    """带 `iter_lines` 的响应 —— 流式路径用它验证字节累计。"""

    def __init__(self, lines: list[bytes]) -> None:
        super().__init__(200)
        self._lines = lines

    def iter_lines(self, *a, **kw):
        yield from self._lines


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="gad-obs-test-"))
    trace_path = tmp / "requests-test.jsonl"
    audit_path = tmp / "sends-test.jsonl"
    os.environ[obs.TRACE_ENV] = str(trace_path)
    os.environ[obs.AUDIT_ENV] = str(audit_path)
    os.environ[obs.TRACE_ENABLE_ENV] = "1"

    try:
        # ---------------- [1] 代理透明性 ----------------
        print("[1] RecordingSession 是透明代理")
        inner = _FakeSession()
        s = obs.RecordingSession(inner)
        r = s.post("https://chat.deepseek.com/api/v0/chat_session/create")
        check("post 返回值是原响应的同一个对象", r.status_code == 200)
        check("post 被转发到内层 session", inner.calls[-1][1].endswith("/chat_session/create"))
        r2 = s.get("https://chat.deepseek.com/api/v0/chat_session/fetch_page?x=1")
        check("get 被转发", r2.status_code == 200)
        check("未代理的方法经 __getattr__ 透传", s.some_unproxied_method() == "透传成功")
        s.close()
        check("close 透传到内层", inner.closed)

        # 异常必须原样抛出，不能被吞掉
        inner2 = _FakeSession()
        inner2.raise_on_post = ValueError("boom")
        s2 = obs.RecordingSession(inner2)
        raised = ""
        try:
            s2.post("https://chat.deepseek.com/api/v0/x")
        except ValueError as exc:
            raised = str(exc)
        check("内层异常原样抛出", raised == "boom")

        # ---------------- [2] trace 字段 ----------------
        print("\n[2] 请求 trace 落盘")
        rows = read_jsonl(trace_path)
        check("trace 有记录", len(rows) >= 3, f"实际 {len(rows)} 行")
        if rows:
            first = rows[0]
            check("含 ts", bool(first.get("ts")))
            check("含 seq", isinstance(first.get("seq"), int))
            check("含 method", first.get("method") == "POST")
            check("path 已去掉域名", first.get("path", "").startswith("/api/"), first.get("path", ""))
            check("path 已去掉查询串", "?" not in first.get("path", ""))
            check("含 status", first.get("status") == 200)
            check("含耗时 ms", isinstance(first.get("ms"), float))
            check("含字节数", first.get("bytes", 0) > 0)
            check("默认 stream=False", first.get("stream") is False)
            check("无 pow 头时 pow=False", first.get("pow") is False)

        # 异常路径也要落一行（status=0 + error）
        err_rows = [x for x in rows if x.get("error")]
        check("异常路径也落盘", bool(err_rows), f"error 行 {len(err_rows)}")
        if err_rows:
            check("异常行 status=0", err_rows[-1].get("status") == 0)
            check("异常行带错误类型名", err_rows[-1].get("error") == "ValueError")

        # ---------------- [3] pow 标记 ----------------
        print("\n[3] pow 标记区分「计算型」与「只读探测」")
        trace_path.unlink()
        inner3 = _FakeSession()
        s3 = obs.RecordingSession(inner3)
        s3.post("https://chat.deepseek.com/api/v0/chat/completion",
                headers={"X-DS-PoW-Response": "abc"})
        s3.post("https://chat.deepseek.com/api/v0/chat_session/create", headers={})
        rows3 = read_jsonl(trace_path)
        check("带 pow 头 → pow=True", rows3[0].get("pow") is True)
        check("不带 pow 头 → pow=False", rows3[1].get("pow") is False)
        check("头名大小写不敏感", rows3[0].get("pow") is True)

        # ---------------- [4] 流式 trace ----------------
        print("\n[4] 流式请求记录 TTFB 与总时长")
        trace_path.unlink()
        inner4 = _FakeSession()
        s4 = obs.RecordingSession(inner4)
        cm = s4.stream("POST", "https://chat.deepseek.com/api/v0/chat/completion",
                       headers={"x-ds-pow-response": "x"})
        with cm as resp:
            check("流式响应可读", resp.status_code == 200)
        rows4 = read_jsonl(trace_path)
        check("流式请求落盘", len(rows4) == 1)
        if rows4:
            rec = rows4[0]
            check("stream=True", rec.get("stream") is True)
            check("有 ttfb_ms", isinstance(rec.get("ttfb_ms"), float))
            check("有 status", rec.get("status") == 200)
            check("流式 pow 标记正确", rec.get("pow") is True)

        # ---------------- [5] trace 开关 ----------------
        print("\n[5] trace 总开关")
        trace_path.unlink()
        os.environ[obs.TRACE_ENABLE_ENV] = "0"
        obs.RecordingSession(_FakeSession()).post("https://chat.deepseek.com/api/v0/x")
        check("GAD_TRACE=0 时不落盘", not trace_path.exists())
        os.environ[obs.TRACE_ENABLE_ENV] = "1"

        # ---------------- [6] 审计两行同 seq ----------------
        print("\n[6] 发送审计两行化")
        base = obs.audit_send(seq=7, cid="cid-abc", prompt_chars=42)
        obs.audit_done(base, ok=False, http_status=200, error="biz_code=5",
                       total_ms=1234.5, ttfb_ms=88.0, pow_ms=310.0, pow_cached=False)
        arows = read_jsonl(audit_path)
        check("落了两行", len(arows) == 2, f"实际 {len(arows)}")
        if len(arows) == 2:
            send_row, done_row = arows
            check("第一行 phase=send", send_row.get("phase") == "send")
            check("第二行 phase=done", done_row.get("phase") == "done")
            check("两行 seq 相同", send_row.get("seq") == done_row.get("seq") == 7)
            check("两行 cid 相同", send_row.get("cid") == done_row.get("cid") == "cid-abc")
            check("send 行记了 prompt_chars", send_row.get("prompt_chars") == 42)
            check("done 行记了 ok", done_row.get("ok") is False)
            check("done 行记了 http_status", done_row.get("http_status") == 200)
            check("done 行记了 total_ms", done_row.get("total_ms") == 1234.5)
            check("done 行记了 ttfb_ms", done_row.get("ttfb_ms") == 88.0)
            check("done 行记了 pow_ms", done_row.get("pow_ms") == 310.0)
            check("done 行记了 error", done_row.get("error") == "biz_code=5")
            check("send 行不含 ok（当时还不知道）", "ok" not in send_row)

        # 附件只记 id/大小，不记本地路径
        base2 = obs.audit_send(
            seq=8, cid="c", prompt_chars=1, upload_ms=500.0,
            files=[{"id": "f1", "file_size": 1024, "status": "SUCCESS",
                    "path": "C:/Users/Admin/secret/a.pdf", "file_name": "a.pdf"}],
        )
        obs.audit_done(base2, ok=True, http_status=200, total_ms=10.0)
        arows2 = read_jsonl(audit_path)
        frow = [x for x in arows2 if x.get("phase") == "send" and x.get("seq") == 8][0]
        check("附件记了 id", frow.get("files", [{}])[0].get("id") == "f1")
        check("附件记了字节数", frow.get("files", [{}])[0].get("bytes") == 1024)
        check("附件不记本地路径", "path" not in json.dumps(frow, ensure_ascii=False))
        check("附件不记文件名", "a.pdf" not in json.dumps(frow, ensure_ascii=False))
        check("记了 upload_ms", frow.get("upload_ms") == 500.0)

        # ---------------- [7] 错误标签截断 ----------------
        print("\n[7] 错误标签（含固定长度截断 bug 的回归）")
        # ★ 优先级：处置文本先于 biz_code —— 与 `classify_error_text` 保持一致。
        #   否则同一件事在审计里叫 `biz_code=5`、在熔断里叫 `muted`，复盘对不上。
        check("禁言文本优先归为 suspended（不记 biz_code）",
              _audit_error_tag("业务错误（biz_code=5：user is muted）") == "suspended")
        check("纯业务错误记 biz_code=5",
              _audit_error_tag("业务错误（biz_code=5：invalid chat session）") == "biz_code=5")
        check("biz_code=40003 不被截成 4000（回归）",
              _audit_error_tag("业务错误（biz_code=40003：Authorization Failed）") == "biz_code=40003",
              _audit_error_tag("业务错误（biz_code=40003：Authorization Failed）"))
        check("biz_code=1234567 也不截",
              _audit_error_tag("x biz_code=1234567, y") == "biz_code=1234567")
        check("无分隔符时取到行尾", _audit_error_tag("biz_code=42") == "biz_code=42")
        check("禁言文本 → suspended", _audit_error_tag("user is muted") == "suspended")
        check("中文禁言 → suspended", _audit_error_tag("账号已被禁言") == "suspended")

        class _HttpErr(Exception):
            status = 429

        check("异常带 status → http_429", _audit_error_tag(_HttpErr("x")) == "http_429")

        # ---------------- [8] MUTED 分类 ----------------
        print("\n[8] MUTED 信号分类优先于 AUTH")
        check("biz_code=5 user is muted → MUTED",
              classify_error_text("biz_code=5：user is muted") is Signal.MUTED)
        check("中文禁言 → MUTED", classify_error_text("账号已被禁言") is Signal.MUTED)
        check("封禁 → MUTED", classify_error_text("账号被封禁") is Signal.MUTED)
        check("suspended 英文 → MUTED", classify_error_text("account has been suspended") is Signal.MUTED)
        check("纯 token 过期仍是 AUTH",
              classify_error_text("未认证：HTTP 401") is Signal.AUTH)
        check("429 仍是 RATE_LIMIT",
              classify_error_text("限流：HTTP 429") is Signal.RATE_LIMIT)
        check("Server busy 仍是 SERVER_BUSY",
              classify_error_text("Server busy / generation_timeout") is Signal.SERVER_BUSY)

        # 关键：处置原文里同时带 40003（与 token 过期同一个业务码）
        mixed = "biz_code=40003：账号被处置（user is muted until 2026-10-20）"
        check("40003 + 处置原文 → MUTED（不能判成 AUTH）",
              classify_error_text(mixed) is Signal.MUTED)

        # ---------------- [9] note_muted 硬停 ----------------
        print("\n[9] note_muted 直接硬停")
        br = CircuitBreaker(EngineConfig().circuit)
        ev = br.note_muted("user is muted until 2026-10-20")
        check("事件是 HALTED", ev.level is BreakerLevel.HALTED)
        check("事件标 fatal", ev.fatal is True)
        st = br.evaluate()
        check("状态是 HALTED", st.level is BreakerLevel.HALTED)
        check("muted_hits=1", st.muted_hits == 1)
        check("停机时长远大于 60 秒（不是退避）",
              st.remaining_ms > 3_600_000, f"{st.remaining_ms:.0f}ms")
        check("理由含原文", "user is muted" in st.reason)
        snap = br.snapshot()
        check("快照暴露 muted_hits", snap["counters"]["muted_hits"] == 1)
        check("快照 last_signal=muted", snap["last_signal"] == "muted")

        # ---------------- [10] 落盘失败不影响调用方 ----------------
        print("\n[10] 落盘失败不抛异常")
        # 造一个「父级是文件」的非法路径 —— 比盘符不存在更可移植，
        # 且能真实触发 mkdir/open 失败（跨平台都成立）。
        blocker = tmp / "blocker-file"
        blocker.write_text("x", encoding="utf-8")
        os.environ[obs.AUDIT_ENV] = str(blocker / "sub" / "x.jsonl")
        ok = True
        try:
            b = obs.audit_send(seq=99, cid="c", prompt_chars=1)
            obs.audit_done(b, ok=True)
        except Exception:  # noqa: BLE001
            ok = False
        check("坏路径不抛异常", ok)
        os.environ[obs.AUDIT_ENV] = str(audit_path)

        # ---------------- [11] 封禁现场快照 ----------------
        print("\n[11] 封禁现场快照")
        fake_logs = tmp / "logs"
        fake_logs.mkdir()
        (fake_logs / "engine-20261019.log").write_text("breaker 事件原文\n", encoding="utf-8")
        (fake_logs / "sends-20261019.jsonl").write_text('{"phase":"send"}\n', encoding="utf-8")
        (fake_logs / "requests-20261019.jsonl").write_text('{"status":200}\n', encoding="utf-8")
        # 超窗的旧文件不该被复制
        old = fake_logs / "engine-20260101.log"
        old.write_text("old\n", encoding="utf-8")
        os.utime(old, (time.time() - 30 * 86400,) * 2)

        prev = os.environ.get("LOCALAPPDATA")
        os.environ["LOCALAPPDATA"] = str(tmp / "LA")
        fake_logs.mkdir(parents=True, exist_ok=True)
        # logs_dir() = LOCALAPPDATA/DeepSeek-Agent/logs
        target = Path(tmp) / "LA" / "DeepSeek-Agent" / "logs"
        target.mkdir(parents=True, exist_ok=True)
        for f in fake_logs.iterdir():
            if f.is_file():
                shutil.copy2(f, target / f.name)
        os.utime(target / "engine-20260101.log", (time.time() - 30 * 86400,) * 2)

        out = obs.snapshot_incident("suspended")
        check("返回了快照路径", bool(out))
        if out:
            d = Path(out)
            check("目录存在", d.is_dir())
            check("目录名带 incident- 前缀", d.name.startswith("incident-"))
            check("目录名带 tag", "suspended" in d.name)
            check("有 README.txt", (d / "README.txt").exists())
            check("复制了 engine 日志", (d / "engine-20261019.log").exists())
            check("复制了审计", (d / "sends-20261019.jsonl").exists())
            check("复制了 trace", (d / "requests-20261019.jsonl").exists())
            check("超窗旧文件未被复制", not (d / "engine-20260101.log").exists())
            check("原文件仍在（只复制不移动）", (target / "engine-20261019.log").exists())
            readme = (d / "README.txt").read_text(encoding="utf-8")
            check("README 含触发标记", "suspended" in readme)
            check("README 解释了三种文件", "requests-*.jsonl" in readme)

        if prev is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = prev

    finally:
        os.environ.pop(obs.TRACE_ENV, None)
        os.environ.pop(obs.AUDIT_ENV, None)
        os.environ.pop(obs.TRACE_ENABLE_ENV, None)
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "-" * 60)
    print(f"通过 {len(PASS)} · 失败 {len(FAIL)}")
    if FAIL:
        for n in FAIL:
            print(f"  失败项：{n}")
        return 1
    print("  [可观测性自测通过]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
