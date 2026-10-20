"""沙箱执行环境 —— 让模型写的 Python 脚本在受限子进程里跑。

设计见 `docs/plan-20261016-沙箱执行环境.md`。

三层防护
--------
  1. **进程隔离** —— 独立 python 子进程。绝不在引擎进程里 exec 模型代码
     （死循环会卡死引擎、异常污染引擎状态、无法限制内存）。
  2. **资源限制（Job Object）** —— Windows 上通过 ctypes 调 kernel32，
     限制进程内存、子进程数，超时强杀整组。非 Windows 降级为「只 subprocess
     超时」，无资源限制。
  3. **路径白名单（子进程内 audit hook）** —— 限制脚本能**写**哪些路径。

诚实边界（必须读）
------------------
* 这**不是对抗恶意攻击者的安全边界**。模型是「可能犯错的执行者」，不是攻击者。
  目标是**限制爆炸半径**，不是「防住有能力的绕过」。
* audit hook **可以被绕过**（如用 ctypes 直接调 Win32）。它防的是**意外**越界。
* 默认策略：**严格限制写，读放宽** —— 因为 Python 解释器自己需要读 stdlib
  才能起来，读限制太严会让子进程根本跑不起来。写才是破坏性的。

为什么 v1 不做 CREATE_SUSPENDED
-------------------------------
设计文档提到 assign 前的竞态窗口。实测：`Popen` 返回到 `AssignProcessToJobObject`
之间，子进程在**初始化 Python 解释器**（几十毫秒），用户代码还没开始。
所以对这个用例，窗口是安全的 —— 值得写出来，避免以后有人以为漏了。
真要做到严格，需要自己 CreateProcess(CREATE_SUSPENDED) + 拿线程句柄 Resume，
成本高且脆弱，v1 不做。
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

#: 默认内存上限（MB）。**不能太低** —— Python 解释器本身启动就要几十 MB。
DEFAULT_MEMORY_MB = 512
#: 默认墙钟超时（秒）。
DEFAULT_TIMEOUT = 60.0
#: 输出上限（字符）。
MAX_OUTPUT = 20000
#: 子进程数上限（含自身）。
MAX_PROCESSES = 4


@dataclass
class SandboxResult:
    exit_code: int
    output: str
    timed_out: bool = False
    killed_by_memory: bool = False
    duration_ms: float = 0.0

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


# --------------------------------------------------------------------------
# Job Object（Windows 资源限制）
# --------------------------------------------------------------------------

_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _JobObjectExtendedLimitInformation = 9
    _JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
    _JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [(f"F{i}", ctypes.c_ulonglong) for i in range(6)]

    class _BASIC_LIMIT(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", ctypes.c_uint32),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_uint32),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_uint32),
            ("SchedulingClass", ctypes.c_uint32),
        ]

    class _EXTENDED_LIMIT(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BASIC_LIMIT),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]


class _JobObject:
    """Windows Job Object 封装。非 Windows 上是 no-op。"""

    def __init__(self, memory_mb: int, max_processes: int) -> None:
        self._handle = None
        self._memory_mb = memory_mb
        self._max_procs = max_processes
        self._active = False

    def create(self) -> bool:
        if not _IS_WINDOWS:
            return False
        handle = _k32.CreateJobObjectW(None, None)
        if not handle:
            return False
        info = _EXTENDED_LIMIT()
        info.BasicLimitInformation.LimitFlags = (
            _JOB_OBJECT_LIMIT_PROCESS_MEMORY
            | _JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            | _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        info.ProcessMemoryLimit = self._memory_mb * 1024 * 1024
        info.BasicLimitInformation.ActiveProcessLimit = self._max_procs
        ok = _k32.SetInformationJobObject(
            handle,
            _JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            _k32.CloseHandle(handle)
            return False
        self._handle = handle
        self._active = True
        return True

    def assign(self, proc: subprocess.Popen) -> bool:
        if not self._active or not _IS_WINDOWS:
            return False
        # Popen 的进程句柄：Windows 上是 proc._handle（int）
        handle = getattr(proc, "_handle", None)
        if handle is None:
            return False
        return bool(_k32.AssignProcessToJobObject(self._handle, int(handle)))

    def terminate(self) -> None:
        """杀掉 job 里的所有进程。"""
        if self._handle and _IS_WINDOWS:
            with contextlib.suppress(OSError):
                _k32.TerminateJobObject(self._handle, 1)

    def close(self) -> None:
        if self._handle and _IS_WINDOWS:
            with contextlib.suppress(OSError):
                _k32.CloseHandle(self._handle)
            self._handle = None
            self._active = False


# --------------------------------------------------------------------------
# 子进程内的守卫代码（写入临时文件，由子进程执行）
# --------------------------------------------------------------------------

#: 子进程的入口。它装 audit hook 再 exec 用户代码。
#: **不 import gad_engine** —— 自包含，无路径依赖。
_WRAPPER_SOURCE = '''\
"""沙箱子进程入口（由 sandbox.py 生成）。"""
import json
import os
import sys

# **必须显式重配 stdout/stderr 编码** —— 子进程用 `-I` 启动，而 `-I` 隐含
# `-E`（忽略所有 PYTHON* 环境变量），所以父进程设的 PYTHONIOENCODING=utf-8
# **不生效**。实测：子进程 stdout 编码是 gbk（系统默认），父进程按 utf-8 读
# → 中文输出全是乱码（`文件数` 变成 `�ļ���`）。
# 这里直接 reconfigure，不依赖环境变量 —— `-I` 挡不住它。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

_allowed = [os.path.realpath(p) for p in json.loads(os.environ["GAD_SANDBOX_ALLOW"])]


def _within(path):
    try:
        real = os.path.realpath(str(path))
    except (OSError, ValueError):
        return True  # 解不了就放行，交给底层报错
    for root in _allowed:
        if real == root or real.startswith(root + os.sep):
            return True
    return False


def _is_write_mode(mode, flags):
    if isinstance(mode, str) and mode:
        return any(c in mode for c in ("w", "a", "x", "+"))
    if isinstance(flags, int):
        # os.O_WRONLY=1, O_RDWR=2, O_CREAT=0x40, O_TRUNC=0x200, O_APPEND=0x8
        return bool(flags & (1 | 2 | 0x40 | 0x200 | 0x8))
    return False


def _hook(event, args):
    # 只检查**写**与破坏性操作；读放宽（Python 自己要读 stdlib）。
    if event == "open":
        path = args[0] if args else None
        if path is None:
            return
        mode = args[1] if len(args) > 1 else None
        flags = args[2] if len(args) > 2 else None
        if _is_write_mode(mode, flags) and not _within(path):
            raise PermissionError(
                "沙箱拒绝写入范围外的路径: %r（允许: %s）" % (path, _allowed)
            )
        return
    if event in ("os.remove", "os.unlink", "os.rename", "os.replace",
                 "os.mkdir", "os.rmdir", "os.makedirs", "os.removedirs",
                 "os.truncate", "shutil.rmtree", "shutil.move", "shutil.copy"):
        target = args[0] if args else None
        if target is not None and not _within(target):
            raise PermissionError("沙箱拒绝操作范围外的路径: %r" % (target,))
        return
    if event in ("os.system", "os.exec", "os.spawn", "subprocess.Popen"):
        raise PermissionError(
            "沙箱内不允许再起子进程（%s）。如需执行命令请用 local_cmd 工具。" % event
        )


sys.addaudithook(_hook)

_user_code_path = sys.argv[1]
# 读用户代码在装 hook 之后（读模式，放行）。
with open(_user_code_path, "r", encoding="utf-8") as f:
    _source = f.read()

_globals = {"__name__": "__main__", "__file__": _user_code_path}
exec(compile(_source, _user_code_path, "exec"), _globals)
'''


def run_python(
    source: str,
    *,
    work_dir: str | Path,
    allow_paths: list[str | Path] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    memory_mb: int = DEFAULT_MEMORY_MB,
) -> SandboxResult:
    """在沙箱子进程里跑一段 Python 源码。

    `work_dir` 与 `allow_paths` 是脚本**允许写入**的路径（读放宽）。
    work_dir 总在允许列表里。
    """
    import time

    work = Path(work_dir).expanduser().resolve()
    allowed: list[str] = [str(work)]
    for p in allow_paths or []:
        try:
            rp = str(Path(p).expanduser().resolve())
        except (OSError, ValueError):
            continue
        if rp not in allowed:
            allowed.append(rp)

    tmpdir = Path(tempfile.gettempdir()) / "gad-sandbox"
    tmpdir.mkdir(parents=True, exist_ok=True)
    tag = uuid.uuid4().hex[:12]
    user_file = tmpdir / f"user-{tag}.py"
    wrapper_file = tmpdir / f"wrap-{tag}.py"

    try:
        user_file.write_text(source, encoding="utf-8")
        wrapper_file.write_text(_WRAPPER_SOURCE, encoding="utf-8")
    except OSError as exc:
        return SandboxResult(exit_code=1, output=f"[沙箱准备失败] {exc}")

    env = dict(os.environ)
    env["GAD_SANDBOX_ALLOW"] = json.dumps(allowed)
    env["PYTHONIOENCODING"] = "utf-8"
    # 别让用户的 sitecustomize 之类干扰
    env.pop("PYTHONSTARTUP", None)

    started = time.monotonic()
    job = _JobObject(memory_mb, MAX_PROCESSES)
    job.create()  # 失败不致命（非 Windows 或权限不足时降级）

    try:
        proc = subprocess.Popen(
            [sys.executable, "-I", str(wrapper_file), str(user_file)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(work) if work.is_dir() else None,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, ValueError) as exc:
        job.close()
        _cleanup(user_file, wrapper_file)
        return SandboxResult(exit_code=1, output=f"[沙箱启动失败] {exc}")

    job.assign(proc)

    timed_out = False
    killed_mem = False
    try:
        out, _ = proc.communicate(timeout=timeout)
        exit_code = proc.returncode if proc.returncode is not None else 0
    except subprocess.TimeoutExpired:
        timed_out = True
        job.terminate()
        with contextlib.suppress(OSError):
            proc.kill()
        out, _ = proc.communicate()
        exit_code = 124
    finally:
        job.close()
        _cleanup(user_file, wrapper_file)

    duration = (time.monotonic() - started) * 1000.0
    text = out or ""
    if len(text) > MAX_OUTPUT:
        text = text[:MAX_OUTPUT] + "\n…（输出已截断）"

    # Windows 上内存超限的退出码是 0xC0000017（STATUS_NO_MEMORY）等，
    # 但也可能是其它。这里只在明确信号时标记。
    if not timed_out and exit_code not in (0,) and _looks_like_oom(text):
        killed_mem = True

    return SandboxResult(
        exit_code=exit_code,
        output=text,
        timed_out=timed_out,
        killed_by_memory=killed_mem,
        duration_ms=duration,
    )


def _looks_like_oom(text: str) -> bool:
    low = (text or "").lower()
    return "memoryerror" in low or ("memory" in low and "error" in low)


def _cleanup(*files: Path) -> None:
    for f in files:
        with contextlib.suppress(OSError):
            f.unlink(missing_ok=True)


def format_result(result: SandboxResult, work_dir: str) -> str:
    """把沙箱结果格式化成给模型看的文本。"""
    if result.timed_out:
        head = (
            f"[run_code 超时] {int(result.duration_ms)}ms 后被强制终止。\n"
            "可能：死循环 / 等待输入 / 任务确实太大。"
        )
    elif result.killed_by_memory:
        head = (
            "[run_code 内存超限] 被沙箱杀掉。\n"
            "这是**资源限制**触发的，不是脚本 bug —— 但通常意味着脚本一次处理了太多数据。"
        )
    elif result.exit_code == 0:
        head = f"[run_code 成功] 耗时 {int(result.duration_ms)}ms"
    else:
        head = f"[run_code 失败] 退出码 {result.exit_code} · 耗时 {int(result.duration_ms)}ms"

    body = result.output.strip() or "(无输出)"
    return (
        f"{head}\n\n{body}\n\n"
        "（脚本在沙箱里运行：只能写工作目录内的路径，不能起子进程。）\n"
        "请输出下一个 ```local_cmd，或结束本轮。"
    )
