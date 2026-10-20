"""可观测性 —— 请求级 trace、发送审计、封禁现场快照。

**为什么单独一个模块**（2026-10-19）：

2026-10-17 账号被禁言时，复盘发现**回答不了**最基本的问题：
「出事前那一次请求，上游回了什么？」

引擎日志里只有壳↔引擎的 JSON-RPC 转储（`HH:mm:ss.fff [out] {完整 JSON}`），
传输层的 HTTP 往返 —— 状态码 / 耗时 / 响应字节数 —— **一个字节都没落盘**。
于是那次复盘的结论只能停在「17 分钟发了 10 条」，说不出「哪一条触发的」。

本模块提供三个收口点，全部**只记元数据、不记内容**：

  * `RecordingSession` —— 给 curl_cffi Session 套一层，记录每次 HTTP 往返
  * `audit_send` / `audit_done` —— 一次发送的「发出前 / 结束后」两行
  * `snapshot_incident` —— 检测到账号处置时，把最近日志打包成现场

**为什么不记请求体 / 响应体**：审计的目的是复盘「我们的行为模式」
（频率、峰值、失败率、信号序列），不是给对话内容再存一份副本。
落盘的东西越多，泄露面越大，而复盘价值不增加。

**为什么审计要分两行**（`phase=send` / `phase=done`）：

`send` 必须在**真正发出去之前**落 —— 它数的是「我们向上游发了多少次」，
与请求成败无关（失败的那次上游也看见了）。如果只在结束后记，
进程中途崩溃就会漏掉已经打出去的请求。

而「成功没有、为什么失败、首字节多久」只有结束后才知道。
所以用同一个 `seq` 串两行，一行一个时点。

计数改成：

    grep -c '"phase": "send"' logs/sends-*.jsonl
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

#: 请求 trace 的落点覆盖（指向文件）。
TRACE_ENV = "GAD_TRACE_LOG"
#: 发送审计的落点覆盖（指向文件）。
AUDIT_ENV = "GAD_SEND_AUDIT"
#: 设为 `0` / `off` / `false` 可整体关掉请求 trace（审计不受影响）。
TRACE_ENABLE_ENV = "GAD_TRACE"


def logs_dir() -> Path:
    """日志根目录 —— 与引擎日志、审计、崩溃日志同一个地方。"""
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return Path(base) / "DeepSeek-Agent" / "logs"


def _resolve(env_key: str, prefix: str) -> Path:
    """解析落点：环境变量优先，否则 `<logs>/<prefix>-YYYYMMDD.jsonl`。"""
    override = os.environ.get(env_key, "").strip()
    if override:
        return Path(override)
    d = logs_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{prefix}-{time.strftime('%Y%m%d')}.jsonl"


def append_jsonl(path: Path, rec: dict[str, Any]) -> None:
    """追加一行。**失败绝不影响调用方** —— 观测手段不是业务。

    磁盘满 / 文件被占 / 路径不可写，一律静默。发送流程不能因为
    「日志写不进去」而失败 —— 那会把观测问题变成可用性问题。
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


# ---------------------------------------------------------------------------
# 请求 trace
# ---------------------------------------------------------------------------

_trace_seq_lock = threading.Lock()
_trace_seq = 0


def _next_trace_seq() -> int:
    global _trace_seq
    with _trace_seq_lock:
        _trace_seq += 1
        return _trace_seq


def trace_enabled() -> bool:
    v = os.environ.get(TRACE_ENABLE_ENV, "").strip().lower()
    return v not in ("0", "off", "false", "no")


def _path_of(url: str) -> str:
    """从完整 URL 取 path —— 不记域名，不记查询串（查询串可能带 id）。"""
    try:
        rest = url.split("://", 1)[-1]
        slash = rest.find("/")
        path = rest[slash:] if slash >= 0 else "/"
    except Exception:  # noqa: BLE001
        return "?"
    return path.split("?", 1)[0]


def record_request(
    *,
    seq: int,
    method: str,
    path: str,
    status: int,
    ms: float,
    nbytes: int = 0,
    ttfb_ms: float | None = None,
    stream: bool = False,
    pow: bool = False,
    error: str | None = None,
) -> None:
    """落一行请求 trace。

    `pow=True` 表示请求头里带了 `x-ds-pow-response` —— 这是「一次真实的
    计算型请求」的标记，与「只读探测」在风控眼里不是一回事，必须能分开看。
    """
    if not trace_enabled():
        return
    rec: dict[str, Any] = {
        "ts": _now_iso(),
        "pid": os.getpid(),
        "seq": seq,
        "method": method,
        "path": path,
        "status": status,
        "ms": round(float(ms), 1),
        "bytes": int(nbytes),
        "stream": bool(stream),
        "pow": bool(pow),
    }
    if ttfb_ms is not None:
        rec["ttfb_ms"] = round(float(ttfb_ms), 1)
    if error:
        rec["error"] = error
    append_jsonl(_resolve(TRACE_ENV, "requests"), rec)


def _has_pow(headers: Any) -> bool:
    if not headers:
        return False
    try:
        return any(str(k).lower() == "x-ds-pow-response" for k in headers)
    except Exception:  # noqa: BLE001
        return False


class _RecordingResponse:
    """代理流式响应对象，顺便累计字节数。

    只包装 `iter_lines` —— 那是 `_stream_completion` 唯一读数据的方式。
    其它属性（`status_code` / `content` / `text`）原样透传。
    """

    __slots__ = ("_owner", "_resp")

    def __init__(self, resp: Any, owner: _RecordingStream) -> None:
        self._resp = resp
        self._owner = owner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._resp, name)

    def iter_lines(self, *args: Any, **kwargs: Any):
        for line in self._resp.iter_lines(*args, **kwargs):
            if isinstance(line, bytes):
                self._owner.nbytes += len(line)
            elif isinstance(line, str):
                self._owner.nbytes += len(line.encode("utf-8", "replace"))
            yield line


class _RecordingStream:
    """`Session.stream()` 返回的上下文管理器 —— 记录首字节与总时长。

    **流式的耗时语义与普通请求不同**：`ms` 是整段流读完的时长，
    `ttfb_ms` 才是「服务端多久开始回」。后者是判断「服务端愿不愿意理你」
    的关键指标 —— 2026-10-15 实测服务端过载时会把连接挂 ~68 秒才回。

    记录点放在 `__exit__` 而不是 `__enter__`：只有退出时才拿得到
    「流读了多久」。异常退出也照样记（`error` 字段区分）。
    """

    __slots__ = ("_cm", "_method", "_path", "_pow", "_seq", "_status", "_t0", "nbytes", "ttfb_ms")

    def __init__(
        self,
        cm: Any,
        seq: int,
        method: str,
        path: str,
        pow: bool,
        t0: float,
    ) -> None:
        self._cm = cm
        self._seq = seq
        self._method = method
        self._path = path
        self._pow = pow
        self._t0 = t0
        self.nbytes = 0
        self.ttfb_ms: float | None = None
        self._status = 0

    def __enter__(self) -> _RecordingResponse:
        resp = self._cm.__enter__()
        self.ttfb_ms = (time.monotonic() - self._t0) * 1000.0
        self._status = int(getattr(resp, "status_code", 0) or 0)
        return _RecordingResponse(resp, self)

    def __exit__(self, exc_type, exc, tb):
        try:
            return self._cm.__exit__(exc_type, exc, tb)
        finally:
            ms = (time.monotonic() - self._t0) * 1000.0
            record_request(
                seq=self._seq,
                method=self._method,
                path=self._path,
                status=self._status,
                ms=ms,
                nbytes=self.nbytes,
                ttfb_ms=self.ttfb_ms,
                stream=True,
                pow=self._pow,
                error=(exc_type.__name__ if exc_type else None),
            )


class RecordingSession:
    """curl_cffi `Session` 的透明代理 —— 记录每一次 HTTP 往返。

    **为什么要代理而不是在调用点埋点**：调用点有 11 个且还在增加
    （上传 / TTS / 历史 / 删除 / 会话创建…），逐点埋点必然漏。
    代理是唯一收口点，新接口自动被覆盖。

    只代理实际用到的四个方法（`post` / `get` / `stream` / `close`），
    其余属性经 `__getattr__` 原样透传 —— 将来 curl_cffi 加了新方法，
    这里不需要跟着改（只是不记 trace）。

    **线程安全**：`_prefetch_one` 会在后台线程用独立 session，
    但每个 `RecordingSession` 实例只被一个线程使用；`_seq` 的锁只是
    防御性的（同一实例被并发调用时序号不会撞）。
    """

    __slots__ = ("_inner", "_lock", "_seq")

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self._seq = 0
        self._lock = threading.Lock()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def _next_seq(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    def _record_plain(self, seq: int, method: str, url: str, headers: Any,
                      t0: float, resp: Any, exc: BaseException | None) -> None:
        ms = (time.monotonic() - t0) * 1000.0
        if exc is not None:
            record_request(
                seq=seq, method=method, path=_path_of(url), status=0,
                ms=ms, pow=_has_pow(headers), error=type(exc).__name__,
            )
            return
        status = int(getattr(resp, "status_code", 0) or 0)
        nbytes = 0
        try:
            raw = getattr(resp, "content", b"") or b""
            nbytes = len(raw)
        except Exception:  # noqa: BLE001
            nbytes = 0
        record_request(
            seq=seq, method=method, path=_path_of(url), status=status,
            ms=ms, nbytes=nbytes, pow=_has_pow(headers),
        )

    def post(self, url: str, **kwargs: Any):
        seq = self._next_seq()
        headers = kwargs.get("headers")
        t0 = time.monotonic()
        try:
            resp = self._inner.post(url, **kwargs)
        except BaseException as exc:
            self._record_plain(seq, "POST", url, headers, t0, None, exc)
            raise
        self._record_plain(seq, "POST", url, headers, t0, resp, None)
        return resp

    def get(self, url: str, **kwargs: Any):
        seq = self._next_seq()
        headers = kwargs.get("headers")
        t0 = time.monotonic()
        try:
            resp = self._inner.get(url, **kwargs)
        except BaseException as exc:
            self._record_plain(seq, "GET", url, headers, t0, None, exc)
            raise
        self._record_plain(seq, "GET", url, headers, t0, resp, None)
        return resp

    def stream(self, method: str, url: str, **kwargs: Any):
        seq = self._next_seq()
        cm = self._inner.stream(method, url, **kwargs)
        return _RecordingStream(
            cm, seq, method.upper(), _path_of(url),
            _has_pow(kwargs.get("headers")), time.monotonic(),
        )

    def close(self) -> None:
        self._inner.close()


# ---------------------------------------------------------------------------
# 发送审计
# ---------------------------------------------------------------------------


def audit_path() -> Path:
    return _resolve(AUDIT_ENV, "sends")


def audit_send(
    *,
    seq: int,
    cid: str,
    prompt_chars: int,
    files: list[dict] | None = None,
    upload_ms: float = 0.0,
) -> dict[str, Any]:
    """发送**之前**落一行 —— 它数的是「向上游发了几次」，与成败无关。"""
    rec: dict[str, Any] = {
        "phase": "send",
        "ts": _now_iso(),
        "pid": os.getpid(),
        "seq": seq,
        "cid": cid,
        "prompt_chars": int(prompt_chars),
    }
    # 附件也计入审计 —— 一次上传是一次独立的 `upload_file` 请求
    # （还各带一份自己的 PoW），不记就低估了「向上游发了多少次」。
    # **只记 id / 大小，不记文件名全路径**：审计是给复盘用的，
    # 不该顺带泄露本地目录结构。
    if files:
        rec["files"] = [
            {
                "id": str(f.get("id") or ""),
                "bytes": f.get("file_size"),
                "status": f.get("status"),
            }
            for f in files
        ]
        rec["upload_ms"] = round(float(upload_ms), 1)
    append_jsonl(audit_path(), rec)
    return rec


def audit_done(
    base: dict[str, Any],
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
    """发送**结束后**落第二行，用同一个 `seq` 串起来。

    `error` 只存**分类后的短文本**（`biz_code=5` / `HTTP 429` / 异常类型名），
    不存完整异常链 —— 完整原文在引擎日志与 trace 里，审计保持可 grep。
    """
    rec: dict[str, Any] = {
        "phase": "done",
        "ts": _now_iso(),
        "pid": base.get("pid"),
        "seq": base.get("seq"),
        "cid": base.get("cid"),
        "ok": bool(ok),
        "cancelled": bool(cancelled),
        "http_status": int(http_status),
        "total_ms": round(float(total_ms), 1),
        "pow_ms": round(float(pow_ms), 1),
        "pow_cached": bool(pow_cached),
    }
    if ttfb_ms is not None:
        rec["ttfb_ms"] = round(float(ttfb_ms), 1)
    if error:
        rec["error"] = str(error)[:200]
    append_jsonl(audit_path(), rec)


# ---------------------------------------------------------------------------
# 封禁现场快照
# ---------------------------------------------------------------------------


def snapshot_incident(tag: str, *, hours: int = 24, keep_bytes: int = 8 * 1024 * 1024) -> str:
    """把最近的日志打包成 `incident-YYYYMMDD-HHMMSS/` 目录，返回路径。

    触发点：检测到 `looks_suspended()` 为真时（见 `deepseek_rpc` 的发送路径）。

    **为什么不压缩成 zip**：现场要能直接用记事本打开看一眼。
    压缩只是省磁盘，而事故现场最缺的是「立刻能读」。

    **只复制、不移动**：原日志继续正常写入，快照是独立副本。
    事故可能持续数小时，移动会把后续证据弄丢。

    失败返回空串 —— 快照是辅助手段，不能因为它坏了而影响主流程。
    """
    try:
        src = logs_dir()
        if not src.is_dir():
            return ""
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safe_tag = "".join(c if c.isalnum() or c in "-_" else "-" for c in tag)[:40]
        dst = src / f"incident-{stamp}-{safe_tag}" if safe_tag else src / f"incident-{stamp}"
        dst.mkdir(parents=True, exist_ok=True)

        cutoff = time.time() - hours * 3600
        budget = keep_bytes
        copied = 0
        # 按修改时间倒序 —— 磁盘紧张时优先保住最近的现场。
        files = sorted(
            (p for p in src.iterdir() if p.is_file() and p.suffix in (".log", ".jsonl")),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for p in files:
            try:
                if p.stat().st_mtime < cutoff:
                    continue
                if p.stat().st_size > budget:
                    continue
                shutil.copy2(p, dst / p.name)
                budget -= p.stat().st_size
                copied += 1
            except Exception:  # noqa: BLE001
                continue

        (dst / "README.txt").write_text(
            f"封禁现场快照 · {stamp}\n"
            f"触发标记：{tag}\n"
            f"时间窗口：最近 {hours} 小时\n"
            f"复制文件：{copied} 个\n\n"
            "文件说明：\n"
            "  engine-*.log     壳↔引擎的 JSON-RPC 全量转储（含 breaker 事件原文）\n"
            "  sends-*.jsonl    发送审计（phase=send 是发出前，phase=done 是结果）\n"
            "  requests-*.jsonl 请求 trace（每次 HTTP 往返的状态码 / 耗时 / 字节数）\n",
            encoding="utf-8",
        )
        return str(dst)
    except Exception:  # noqa: BLE001
        return ""
