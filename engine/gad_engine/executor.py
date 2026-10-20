"""本地执行层 —— 从 deepseek-agent-desktop 的 MainWindow.xaml.cs 移植。

关键设计（全部沿用原版实战结论）：

* **常驻进程**：不是每条命令起一次 PowerShell（那要 300ms+），而是持有一个
  长驻进程，用哨兵行做输出分帧。原版用的是 .NET Runspace，这里用等价方案。
* **实时流**：读取线程逐行回调，不是等命令结束才拿结果。
* **输出路由三档**：交给 OutputRouter。
* **超时对齐**：外层 180s 停 pipeline；进程卡死时整进程重启（因为管道已脏）。
* **扫描纪律**：无深度限制的递归扫描**直接拒绝**，且不消耗队列槽 —— 拒绝必须
  返回明确退出码，静默丢弃会让模型一直等。
* **队列闸门**：120s 排队超时 -> 退出码 124。
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .config import ExecutorConfig
from .router import OutputRouter, RoutedOutput

SENTINEL = "__GAD_CMD_END__"

_SCAN_RE = re.compile(r"\b(get-childitem|gci|dir|ls|tree)\b", re.IGNORECASE)

_PS_CANDIDATES = (
    r"C:\Program Files\PowerShell\7\pwsh.exe",
    r"C:\Program Files\PowerShell\7-preview\pwsh.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WindowsApps\pwsh.exe"),
)


@dataclass
class CommandResult:
    command: str
    exit_code: int
    output: str
    timed_out: bool = False
    rejected: bool = False
    reason: str = ""
    duration_ms: float = 0.0
    routed: RoutedOutput | None = None
    stream_chunks: int = 0

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.rejected


def check_scan_discipline(command: str) -> str | None:
    """返回拒绝理由；None 表示通过。`#scan-ok` 可显式豁免。"""
    if "#scan-ok" in command:
        return None
    low = command.lower()
    if "-recurse" not in low:
        return None
    if "-depth" in low:
        return None
    if _SCAN_RE.search(command):
        return (
            "检测到无深度限制的递归扫描。请加 `-Depth 3`（或更小），"
            "或在命令中加 `#scan-ok` 显式豁免。"
        )
    return None


#: `resolve_shell()` 的进程内缓存。键是 `explicit`（通常是配置里的
#: `shell_binary`，绝大多数时候是空串）。
#:
#: **为什么要缓存**：这个函数被 `snapshot()` 调用，而 `snapshot()` 会被
#: `_bootstrap_prompt`、`status()`（外壳每 5 秒一次）、`_write_file`、
#: `_run_code`、`_run_git` 反复调用。每次都是一串 `Path.exists()` + 两次
#: `shutil.which()` —— 在 Windows 上每次约 0.1–1 ms，纯浪费。
#:
#: **为什么可以缓存**：解释器路径在进程生命周期内不会变。用户换了 shell 装法
#: 需要重启引擎才生效 —— 这是可以接受的（改配置本来也要重启）。
#:
#: 注意缓存的是**结果**，异常不缓存：找不到解释器是真实故障，下次还要再找一遍
#: （也许用户刚好装上了）。
_SHELL_CACHE: dict[str, str] = {}


def resolve_shell(explicit: str = "") -> str:
    hit = _SHELL_CACHE.get(explicit)
    if hit is not None:
        return hit
    if explicit and Path(explicit).exists():
        _SHELL_CACHE[explicit] = explicit
        return explicit
    for cand in _PS_CANDIDATES:
        if cand and Path(cand).exists():
            _SHELL_CACHE[explicit] = cand
            return cand
    found = shutil.which("pwsh") or shutil.which("powershell")
    if found:
        _SHELL_CACHE[explicit] = found
        return found
    raise FileNotFoundError("未找到 PowerShell（pwsh / powershell 均不可用）")


def _script_dir() -> Path:
    path = Path(tempfile.gettempdir()) / "deepseek-agent" / "scripts"
    path.mkdir(parents=True, exist_ok=True)
    return path


class LocalExecutor:
    def __init__(
        self,
        cfg: ExecutorConfig,
        on_stream: Callable[[str], None] | None = None,
        on_state: Callable[[str, dict], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.router = OutputRouter(cfg)
        self._on_stream = on_stream
        self._on_state = on_state or (lambda _k, _v: None)

        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._lock = threading.Semaphore(1)
        self._done = threading.Event()

        self._lines: list[str] = []
        self._lines_lock = threading.Lock()
        self._exit_code = 0
        self._stream_chunks = 0
        self._stream_capped = False
        self._proc_died = False
        self._running = False

        #: 执行通道的「代数」。每次 `start()` / `stop()` 递增。
        #:
        #: **为什么需要它**：`_done` 是进程级单例 Event，被三种语义共用 ——
        #: 「命令跑完」「命令超时」「执行器重启」。`restart()`（cancel 走这条路）
        #: 会 stop + start，而 `start()` 里的 `_bootstrap` 复用的正是同一个
        #: `_done`，于是正在等某条命令的 `_run_locked` 会被**别人的**信号唤醒，
        #: 拿到的是 `GAD_EXECUTOR_READY` 启动横幅。代数让等待者能分辨
        #: 「是我的命令结束了」还是「我的执行通道已经不存在了」。
        #:
        #: 与 PoW 那边的 `_stop_gen` 是同一个思路：共享状态必须带「这一轮是谁的」标识。
        self._epoch = 0
        self._epoch_lock = threading.Lock()

        #: 执行通道被重置前最后一批输出行。用于在「命令被强制中止」时
        #: 仍能把取消前已产生的输出交给调用方 —— 否则用户只看到一句
        #: 「已中止」，却看不到命令中止前打印到哪一步。
        self._partial = ""

    # ---------- 生命周期 ----------

    def start(self) -> None:
        # ⚠️ 不能只判 `is not None`：进程已死但对象还挂着时（读线程已置
        # `_proc_died`），这里直接 return 会让 `run()` 往一根死管道里写，
        # 然后白等一整个 command_timeout。必须先看进程是否还活着。
        if self._proc is not None and self._proc.poll() is None:
            return
        if self._proc is not None:
            # 尸体还挂着：先收掉，否则下面的 _reader 会重复启动。
            self._reap()

        shell = resolve_shell(self.cfg.shell_binary)
        work_dir = self.cfg.work_dir or str(Path.home())

        self._proc = subprocess.Popen(
            [
                shell,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                "-",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            cwd=work_dir if Path(work_dir).is_dir() else None,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._running = True
        proc = self._proc
        self._reader = threading.Thread(
            target=self._read_loop, args=(proc,), name="gad-exec-reader", daemon=True
        )
        self._reader.start()

        self._bootstrap(work_dir)
        self._on_state("executor", {"status": "started", "shell": shell, "cwd": work_dir})

    def stop(self) -> None:
        # 先把当前输出存起来 —— restart 之后 `_lines` 会被 bootstrap 清空，
        # 被中止的命令就只剩一句「已中止」，看不到中止前跑到了哪一步。
        with self._lines_lock:
            self._partial = "\n".join(self._lines)
            self._lines.clear()
        self._running = False
        proc = self._proc
        self._proc = None
        # 换代 + 唤醒：正在 `_wait_done` 上等这条命令的线程必须**立刻**知道
        # 「你的执行通道已经没了」，而不是被 `start()` 里 bootstrap 的信号
        # 误唤醒，然后把 `GAD_EXECUTOR_READY` 当成命令输出。
        self._bump_epoch()
        if proc is None:
            self._done.set()
            self._on_state("executor", {"status": "stopped"})
            return
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
        self._on_state("executor", {"status": "stopped"})

    def _reap(self) -> None:
        """收掉一个已经死掉的进程对象。不触发状态回调，不发 epoch。

        只在 `start()` 里用：进程已死但 `self._proc` 还挂着时，必须先清理，
        否则新读线程会和旧读线程共用 `_lines`。
        """
        proc = self._proc
        self._proc = None
        self._running = False
        if proc is None:
            return
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()

    def _bump_epoch(self) -> int:
        with self._epoch_lock:
            self._epoch += 1
            return self._epoch

    def _wait_done(self, limit: float, my_epoch: int) -> bool:
        """等命令结束。返回 True = 我的命令结束了；False = 超时或通道被重置。

        为什么不能直接 `self._done.wait(limit)`：`_done` 被 `start()` 里的
        `_bootstrap` 复用 —— 一次 `restart()` 就会让正在等的线程被别人的信号
        唤醒，于是「命令成功结束、输出是启动横幅」这种假成功。轮询 epoch
        把「谁唤醒了 Event」这个 Event 本身表达不了的信息补上。

        50ms 的轮询间隔只在长命令期间存在，短命令仍是一次 `wait()` 就返回。
        """
        deadline = time.monotonic() + limit
        while True:
            if self._epoch != my_epoch:
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self._done.is_set() and self._epoch == my_epoch
            if self._done.wait(timeout=min(0.05, remaining)):
                # Event 可能被 bootstrap 设过，但 epoch 没变就还是我的命令。
                return self._epoch == my_epoch

    def restart(self) -> None:
        self.stop()
        self.start()

    # ---------- 执行 ----------

    def run(self, command: str, timeout_ms: int | None = None) -> CommandResult:
        started = time.monotonic()

        rejection = check_scan_discipline(command)
        if rejection:
            return CommandResult(
                command=command,
                exit_code=1,
                output=f"[已拒绝] {rejection}",
                rejected=True,
                reason=rejection,
                duration_ms=(time.monotonic() - started) * 1000.0,
            )

        acquired = self._lock.acquire(timeout=self.cfg.queue_timeout_ms / 1000.0)
        if not acquired:
            return CommandResult(
                command=command,
                exit_code=124,
                output=(
                    f"[本地执行排队超时（{self.cfg.queue_timeout_ms // 1000} 秒），"
                    "上一条命令仍在运行，请稍后重试]"
                ),
                reason="queue_timeout",
                duration_ms=(time.monotonic() - started) * 1000.0,
            )

        try:
            if self._proc is None or self._proc.poll() is not None:
                self.start()
            return self._run_locked(command, started, timeout_ms)
        finally:
            self._lock.release()

    def _run_locked(
        self, command: str, started: float, timeout_ms: int | None
    ) -> CommandResult:
        assert self._proc is not None and self._proc.stdin is not None

        with self._lines_lock:
            self._lines.clear()
        self._partial = ""
        self._exit_code = 0
        self._stream_chunks = 0
        self._stream_capped = False
        self._proc_died = False
        self._done.clear()
        # 记下「这条命令属于哪一代执行通道」。restart() 会 bump epoch，
        # 等待者据此分辨「我的命令结束了」和「我的通道被 reset 了」。
        my_epoch = self._epoch

        self._on_state("command_start", {"command": command})

        script = _script_dir() / f"cmd-{uuid.uuid4().hex[:12]}.ps1"
        try:
            script.write_text(command, encoding="utf-8-sig")
        except OSError as exc:
            return CommandResult(
                command=command,
                exit_code=1,
                output=f"[无法写入命令脚本] {exc}",
                duration_ms=(time.monotonic() - started) * 1000.0,
            )

        try:
            try:
                self._proc.stdin.write(self._invoke_line(script) + "\n")
                self._proc.stdin.flush()
            except (OSError, ValueError) as exc:
                self.restart()
                return CommandResult(
                    command=command,
                    exit_code=1,
                    output=f"[执行通道异常，已重启执行器] {exc}",
                    duration_ms=(time.monotonic() - started) * 1000.0,
                )

            limit = (timeout_ms or self.cfg.command_timeout_ms) / 1000.0
            finished = self._wait_done(limit, my_epoch)

            if not finished:
                # ⚠️ 顺序很关键：**先**取输出，**再** restart。
                # `stop()` 会把 `_lines` 存进 `_partial` 后清空，`start()` 里的
                # `_bootstrap` 又会往里写 `GAD_EXECUTOR_READY`。先 restart 再
                # collect，拿到的是启动横幅而不是命令中止前的真实输出。
                channel_reset = self._epoch != my_epoch
                partial = (self._collect() or self._partial).strip()
                self.restart()
                if channel_reset:
                    # 不是超时，是执行通道被别处（cancel / restart）重置了。
                    # 不能报成 timeout —— 用户会以为命令跑满了 180 秒。
                    note = "[命令被中止：执行通道已被重置（会话变量已丢失）]"
                    return CommandResult(
                        command=command,
                        exit_code=124,
                        output=f"{partial}\n{note}" if partial else note,
                        reason="channel_reset",
                        duration_ms=(time.monotonic() - started) * 1000.0,
                        stream_chunks=self._stream_chunks,
                    )
                note = f"[命令超时（{int(limit)} 秒），pipeline 已中止]"
                return CommandResult(
                    command=command,
                    exit_code=124,
                    output=f"{partial}\n{note}" if partial else note,
                    timed_out=True,
                    reason="timeout",
                    duration_ms=(time.monotonic() - started) * 1000.0,
                    stream_chunks=self._stream_chunks,
                )

            raw = self._collect()
            if self._proc_died:
                self.restart()
                return CommandResult(
                    command=command,
                    exit_code=1,
                    output=raw + "\n[执行进程意外退出，已自动重启（会话变量已丢失）]",
                    reason="proc_died",
                    duration_ms=(time.monotonic() - started) * 1000.0,
                    stream_chunks=self._stream_chunks,
                )

            routed = self.router.route(raw)
            return CommandResult(
                command=command,
                exit_code=self._exit_code,
                output=routed.text,
                duration_ms=(time.monotonic() - started) * 1000.0,
                routed=routed,
                stream_chunks=self._stream_chunks,
            )
        finally:
            with contextlib.suppress(OSError):
                script.unlink(missing_ok=True)

    # ---------- 内部 ----------

    def _invoke_line(self, script: Path) -> str:
        """构造调用行。

        两个坑，都由实测得出：

        1. **为什么用临时 .ps1 文件而不是 base64 + Invoke-Expression**
           实测 `exit` 在裸执行、`& { }` 脚本块、`try` 包裹、ScriptBlock.Create
           四种方式下**都会杀掉常驻会话**；只有「写成 .ps1 文件再 `&` 调用」时，
           `exit` 只终止该脚本，会话存活且 $LASTEXITCODE 正确回传。
           模型迟早会输出 `exit`，不防就会丢掉整个会话状态。

        2. **为什么必须套 try/catch**
           `throw` 这类终止性错误若不被捕获，会中断整条 stdin 命令行，
           哨兵行永远不会写出 —— 表现为白等一个完整超时（180s）后强制重启。
           捕获后至少能立刻拿到错误文本并继续。
        """
        return (
            "$global:LASTEXITCODE=0;"
            f"try {{ & '{script}' }}"
            "catch { Write-Output ('[STDERR] ' + $_.Exception.Message);"
            " $global:LASTEXITCODE=1 };"
            "$__gad_e=if($null -eq $LASTEXITCODE){0}else{$LASTEXITCODE};"
            f"Write-Output ('{SENTINEL}'+$__gad_e)"
        )

    def _bootstrap(self, work_dir: str) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        init = (
            "$ProgressPreference='SilentlyContinue';"
            "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
            "$OutputEncoding=[Text.Encoding]::UTF8;"
            f"Set-Location -LiteralPath '{work_dir}' -ErrorAction SilentlyContinue;"
            "Write-Output 'GAD_EXECUTOR_READY'"
        )
        script = _script_dir() / f"init-{uuid.uuid4().hex[:12]}.ps1"
        self._done.clear()
        try:
            script.write_text(init, encoding="utf-8-sig")
            self._proc.stdin.write(self._invoke_line(script) + "\n")
            self._proc.stdin.flush()
            # 必须等执行完再删脚本 —— 否则 PowerShell 还没读到文件就被 unlink 了
            self._done.wait(timeout=15)
        except (OSError, ValueError):
            pass
        finally:
            with contextlib.suppress(OSError):
                script.unlink(missing_ok=True)
        with self._lines_lock:
            self._lines.clear()

    def _read_loop(self, proc: subprocess.Popen) -> None:
        """读线程。`proc` 由 `start()` 显式传入，**不要**读 `self._proc`。

        ⚠️ 为什么必须传参：restart 之后 `self._proc` 已经是新进程了，旧读线程
        若从实例属性取，会把「新进程的 stdout」当成自己的、或者在新命令跑着时
        把 `_proc_died` 置位 —— 一次 cancel 就污染下一条命令。
        """
        if proc.stdout is None:
            return
        try:
            for line in proc.stdout:
                stripped = line.rstrip("\r\n")
                if stripped.startswith(SENTINEL):
                    tail = stripped[len(SENTINEL) :].strip()
                    try:
                        self._exit_code = int(tail)
                    except ValueError:
                        self._exit_code = 0
                    self._done.set()
                    continue
                self._append(stripped)
        except (OSError, ValueError):
            pass
        finally:
            # 只在自己仍是当前进程时才报「意外退出」——否则那是主动 stop()，
            # 不是故障。
            if self._proc is proc and self._running:
                self._proc_died = True
                self._append("[执行进程已退出]")
                self._done.set()

    def _append(self, line: str) -> None:
        with self._lines_lock:
            self._lines.append(line)
        if self._stream_chunks < self.cfg.max_stream_chunks:
            self._stream_chunks += 1
            if self._on_stream:
                with contextlib.suppress(Exception):
                    self._on_stream(line)
        elif not self._stream_capped:
            self._stream_capped = True
            if self._on_stream:
                with contextlib.suppress(Exception):
                    self._on_stream("[实时流达到上限，后续输出完成后统一显示]")

    def _collect(self) -> str:
        with self._lines_lock:
            return "\n".join(self._lines)

    def snapshot(self) -> dict:
        proc = self._proc
        # `shell` 一并报出去：提示词要按它生成语法（见 agent._bootstrap_prompt），
        # 界面也要显示「当前用哪个解释器」。取不到就退回配置值，不抛异常。
        try:
            shell = resolve_shell(self.cfg.shell_binary)
        except FileNotFoundError:
            shell = self.cfg.shell_binary or ""
        return {
            "running": proc is not None and proc.poll() is None,
            "shell": shell,
            "queue_timeout_ms": self.cfg.queue_timeout_ms,
            "command_timeout_ms": self.cfg.command_timeout_ms,
            "work_dir": self.cfg.work_dir or os.path.expanduser("~"),
        }
