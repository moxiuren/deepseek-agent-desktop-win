#!/usr/bin/env python3
"""Web 壳端到端旅程驱动。

## 为什么不用 WPF 那套 ui.ps1

两套壳的 UIA 结构完全不同：
  · WPF 壳：原生 AutomationPeer，控件类型是 `Button` / `Edit` / `List`
  · Web 壳：Chromium accessibility tree，HTML `id` → `AutomationId`，
    纯文本 span 不暴露 id，树是惰性构建的

共用一套驱动只会两头不讨好。这里复用 tools/uia/web.py 的预热逻辑，
但把「探测 + 操作 + 观察」放进**同一个 UIA 连接**，避免每次重连都要
重建 accessibility tree（重建期间 FindFirst 返回 null，看起来像元素消失）。

## 一次只跑一条旅程

真实账号、真实额度。每条旅程跑通一次就停，不重试 —— 反复重试既有
封号风险，也会污染服务端会话历史。
"""
from __future__ import annotations

import glob
import os
import subprocess
import sys
import time
from pathlib import Path

PROC_NAME = "DeepSeekAgent.Web"
LOG_DIR = Path(os.environ["LOCALAPPDATA"]) / "DeepSeek-Agent" / "logs"

PS_HEAD = r"""
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$root = [System.Windows.Automation.AutomationElement]::RootElement
$c = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ProcessIdProperty, __PID__)
$w = $root.FindFirst([System.Windows.Automation.TreeScope]::Children, $c)
if (-not $w) { throw "NO WINDOW" }

$null = $w.FindAll([System.Windows.Automation.TreeScope]::Descendants,
                   [System.Windows.Automation.Condition]::TrueCondition)
Start-Sleep -Milliseconds 250

function Save($s) {
    [System.IO.File]::WriteAllText($env:UIA_OUT, $s, (New-Object System.Text.UTF8Encoding($false)))
}
function Find($aid) {
    $w.FindFirst([System.Windows.Automation.TreeScope]::Descendants,
        (New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::AutomationIdProperty, $aid)))
}
function Shown($e) { if ($e) { -not $e.Current.IsOffscreen } else { $false } }
function Texts {
    $all = $w.FindAll([System.Windows.Automation.TreeScope]::Descendants,
                      [System.Windows.Automation.Condition]::TrueCondition)
    $r = @()
    foreach ($e in $all) {
        try {
            if ($e.Current.ControlType.ProgrammaticName -eq 'ControlType.Text' -and $e.Current.Name) {
                $r += $e.Current.Name
            }
        } catch { }
    }
    return $r
}
# 中栏消息区。`<div id="stream">` 没有语义角色，Chromium 不暴露它的
# AutomationId —— 但它的文本子节点仍在树里、仍带 BoundingRectangle。
# 所以按几何位置筛（x 落在中栏区间），而不是按容器 id 找。
function MidTexts {
    $all = $w.FindAll([System.Windows.Automation.TreeScope]::Descendants,
                      [System.Windows.Automation.Condition]::TrueCondition)
    $r = @()
    foreach ($e in $all) {
        try {
            if ($e.Current.ControlType.ProgrammaticName -eq 'ControlType.Text' -and $e.Current.Name) {
                $b = $e.Current.BoundingRectangle
                if ($b.X -gt 200 -and $b.Width -gt 0) {
                    $r += [pscustomobject]@{ Y = [int]$b.Y; X = [int]$b.X; T = $e.Current.Name }
                }
            }
        } catch { }
    }
    return ($r | Sort-Object Y, X)
}
"""


def find_pid() -> int:
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"(Get-Process -Name '{PROC_NAME}' -ErrorAction SilentlyContinue | "
         f"Select-Object -First 1).Id"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    t = out.stdout.strip()
    if not t.isdigit():
        raise SystemExit(f"进程 {PROC_NAME} 未运行")
    return int(t)


def run_ps(body: str, tag: str, timeout: int = 180) -> tuple[str, int]:
    out = Path(os.environ["TEMP"]) / f"web_e2e_{tag}.txt"
    if out.exists():
        out.unlink()
    env = dict(os.environ)
    env["UIA_OUT"] = str(out)

    script = PS_HEAD.replace("__PID__", str(find_pid())) + body
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, timeout=timeout,
    )
    body_out = out.read_text(encoding="utf-8") if out.exists() else ""
    if not body_out and proc.stderr.strip():
        body_out = "[stderr]\n" + proc.stderr.strip()[:1500]
    return body_out, proc.returncode


def log_offset() -> tuple[Path, int]:
    """记录当前日志的字节位置，用于「只看这之后新发生的事件」。

    为什么必须记偏移：engine 日志是追加写的，同一个文件里躺着历史轮次。
    直接 grep 会把上一轮的 delta 当成这一轮的，判据就失效了。
    """
    p = latest_log()
    return p, p.stat().st_size


def read_new_events(mark: tuple[Path, int]) -> list[dict]:
    """读 mark 之后新增的日志行，解析出 JSON 事件。

    返回 [{"ts":..., "event":..., "data":{...}}, ...]。
    解析失败的行（例如多行堆栈）跳过，不抛异常 —— 日志不是给机器读的，
    偶尔会有非 JSON 内容混进来。
    """
    import json as _json
    path, off = mark
    if not path.exists():
        return []
    with path.open("rb") as f:
        f.seek(off)
        raw = f.read()
    text = raw.decode("utf-8", errors="replace")
    out: list[dict] = []
    for line in text.splitlines():
        i = line.find("{")
        if i < 0:
            continue
        try:
            d = _json.loads(line[i:])
        except Exception:
            continue
        if isinstance(d, dict) and "event" in d:
            out.append({"ts": line[:i].strip(), "event": d["event"],
                        "data": d.get("data") or {}})
    return out


def latest_log() -> Path:
    files = sorted(glob.glob(str(LOG_DIR / "engine-*.log")), key=os.path.getmtime)
    return Path(files[-1])


# ---------------------------------------------------------------- 日志归属
#
# `latest_log()` 按 mtime 排序 —— **在两个壳同时运行时是错的**。
#
# 2026-10-18 实测：WPF 壳（PID 31392）与 Web 壳（PID 24208）各起了一个
# gad_engine，各写一份日志。两个引擎都在每秒推 status，所以两个文件的
# mtime 来回交错，「谁最新」在两次调用之间就会翻转。
#
# 后果不是报错，而是**判据读错了对象**：旅程 4b 的基线读到了 WPF 引擎的
# 会话 `94399d4a`，而消息实际发进了 Web 引擎的 `bc4f11ef`，于是
# 「cid 变了」被误判成会话漂移 —— 其实只是读错了文件。
#
# 修法：按进程归属绑定。日志文件名里的时间戳就是引擎创建时刻，与
# gad_engine 进程的 StartTime 对比，秒级匹配即唯一确定。

PS_ENGINE_START = r"""
$shellPid = __PID__
$kids = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.ParentProcessId -eq $shellPid }
foreach ($k in $kids) {
    if ($k.CommandLine -notlike '*gad_engine*') { continue }
    $p = Get-Process -Id $k.ProcessId -ErrorAction SilentlyContinue
    if ($p) { '{0}|{1:yyyyMMdd-HHmmss}' -f $k.ProcessId, $p.StartTime }
}
"""


def engine_log_for(shell_pid: int) -> tuple[Path, int]:
    """返回 (该壳自己的引擎日志, 引擎 PID)。

    找不到就退回 `latest_log()` —— 单壳运行时两者等价，且不要让测试
    因为探测失败而整条挂掉。但会打印一行警告，避免静默走错分支。
    """
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         PS_ENGINE_START.replace("__PID__", str(shell_pid))],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    for ln in (proc.stdout or "").splitlines():
        ln = ln.strip()
        if "|" not in ln:
            continue
        pid_s, stamp = ln.split("|", 1)
        cand = LOG_DIR / f"engine-{stamp}.log"
        if cand.exists():
            return cand, int(pid_s)
    print(f"[警告] 未能按 PID 绑定引擎日志，退回 latest_log()（多壳并行时不可靠）")
    return latest_log(), -1


def log_offset_for(shell_pid: int) -> tuple[Path, int]:
    """`log_offset()` 的按 PID 版本 —— 多壳并行时必须用这个。"""
    p, _ = engine_log_for(shell_pid)
    return p, p.stat().st_size


def tail_log(path: Path, since_lines: int, keywords: tuple[str, ...],
             limit: int = 20) -> list[str]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    hits = []
    for ln in lines[since_lines:]:
        low = ln.lower()
        if any(k in low for k in keywords):
            hits.append(ln[:280])
            if len(hits) >= limit:
                break
    return hits
