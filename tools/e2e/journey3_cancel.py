#!/usr/bin/env python3
"""端到端旅程 3：长回答中途停止。

判据：
  1. 点击运行后进入运行态（BtnRun 消失、BtnCancel 出现）
  2. 在流式输出**进行中**点 BtnCancel
  3. 3 秒内回到空闲态（BtnRun 回来、BtnCancel 消失）
  4. 引擎日志出现取消记录，且之后 chunk 不再增长

为什么整个流程塞进**一个 PowerShell 进程**：每次 run_ps 都要冷启动
powershell.exe（约 0.6-1.0 秒），而这里需要在几十毫秒粒度上观察按钮切换。
往返式轮询会把「运行中」这个窗口整个错过 —— 上一版等了 4 秒才点取消，
模型早已答完，测到的是「一轮跑完」而不是「取消」。
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
import time
from pathlib import Path

PROC_NAME = "DeepSeekAgent.Desktop"
LOG_DIR = Path(os.environ["LOCALAPPDATA"]) / "DeepSeek-Agent" / "logs"


def find_pid() -> int:
    """按进程名解析 PID。

    原来这里硬编码 24248，重启一次应用就全线失效 —— 测试脚本里任何
    「上一次运行留下的具体数值」都是定时炸弹。
    """
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"(Get-Process -Name '{PROC_NAME}' -ErrorAction SilentlyContinue | "
         f"Select-Object -First 1).Id"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    text = out.stdout.strip()
    if not text.isdigit():
        raise SystemExit(f"进程 {PROC_NAME} 未运行")
    return int(text)

MSG = "请用大约 800 字详细讲解 TCP 三次握手的原理，为什么需要三次而不是两次，并举例说明真实网络中可能出现的异常情况。"

# 整段流程在 PowerShell 内跑完：写输入 → 点运行 → 轮询运行态 → 立刻取消 → 轮询空闲态
DRIVER = r"""
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$root = [System.Windows.Automation.AutomationElement]::RootElement
$c = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ProcessIdProperty, %PID%)
$w = $root.FindFirst([System.Windows.Automation.TreeScope]::Children, $c)
if ($w -eq $null) { throw "NO WINDOW" }

function Find($aid) {
    $w.FindFirst([System.Windows.Automation.TreeScope]::Descendants,
        (New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::AutomationIdProperty, $aid)))
}
function IsShown($e) { if ($e) { -not $e.Current.IsOffscreen } else { $false } }
function St {
    "run=" + (IsShown (Find 'BtnRun')) + " cancel=" + (IsShown (Find 'BtnCancel'))
}

$sb = New-Object System.Text.StringBuilder
$sw = [System.Diagnostics.Stopwatch]::StartNew()
function Log($s) { [void]$sb.AppendLine(("{0,6:0.00}s  {1}" -f $sw.Elapsed.TotalSeconds, $s)) }

Log ("INITIAL  " + (St))

# --- 1. 写入问题 ---
$i = Find 'TxtInput'
$vp = $null
if (-not $i.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$vp)) { throw "NO ValuePattern" }
$vp.SetValue($env:MSG_TEXT)
Log ("SET      len=" + $vp.Current.Value.Length)

# --- 2. 点运行 ---
$run = Find 'BtnRun'
$ip = $null
if (-not $run.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$ip)) { throw "NO InvokePattern on BtnRun" }
$ip.Invoke()
Log ("CLICK    BtnRun")

# --- 3. 轮询进入运行态（20ms 粒度，最多 4 秒）---
$entered = $false
$tEnter = 0.0
while ($sw.Elapsed.TotalSeconds -lt 4.0) {
    $cancel = Find 'BtnCancel'
    if (IsShown $cancel) { $entered = $true; $tEnter = $sw.Elapsed.TotalSeconds; break }
    Start-Sleep -Milliseconds 20
}
Log ("ENTER    " + (St) + "  entered=" + $entered + " at=" + ("{0:0.00}s" -f $tEnter))

if (-not $entered) {
    [System.IO.File]::WriteAllText($env:UIA_OUT, $sb.ToString(), (New-Object System.Text.UTF8Encoding($false)))
    exit 1
}

# --- 4. 立刻点取消（此时模型正在流式输出）---
$cancel = Find 'BtnCancel'
$ipc = $null
if (-not $cancel.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$ipc)) { throw "NO InvokePattern on BtnCancel" }
$ipc.Invoke()
$tCancel = $sw.Elapsed.TotalSeconds
Log ("CLICK    BtnCancel")

# --- 5. 轮询回到空闲态（最多 6 秒）---
$back = $false
$tBack = 0.0
while ($sw.Elapsed.TotalSeconds -lt ($tCancel + 6.0)) {
    if ((IsShown (Find 'BtnRun')) -and -not (IsShown (Find 'BtnCancel'))) {
        $back = $true; $tBack = $sw.Elapsed.TotalSeconds; break
    }
    Start-Sleep -Milliseconds 20
}
Log ("BACK     " + (St) + "  back=" + $back + " after=" + ("{0:0.00}s" -f ($tBack - $tCancel)))

[System.IO.File]::WriteAllText($env:UIA_OUT, $sb.ToString(), (New-Object System.Text.UTF8Encoding($false)))
"""


def latest_log() -> Path:
    files = sorted(glob.glob(str(LOG_DIR / "engine-*.log")), key=os.path.getmtime)
    return Path(files[-1])


def count_chunks(path: Path) -> int:
    n = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if '"type": "chunk"' in line or '"type":"chunk"' in line:
            n += 1
    return n


def grep_cancel(path: Path) -> list[str]:
    hits = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        low = line.lower()
        if "cancel" in low or "abort" in low or "stop" in low:
            hits.append(line[:200])
    return hits[-8:]


def main() -> int:
    log_before = latest_log()
    print("=== 旅程 3：长回答中途停止 ===")
    print("日志文件:", log_before.name)
    print("取消前 chunk 数:", count_chunks(log_before))

    out = Path(os.environ["TEMP"]) / "e2e_j3.txt"
    if out.exists():
        out.unlink()
    env = dict(os.environ)
    env["UIA_OUT"] = str(out)
    env["MSG_TEXT"] = MSG

    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command", DRIVER.replace("%PID%", str(find_pid()))],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
    )

    body = out.read_text(encoding="utf-8").strip() if out.exists() else "(no output)"
    print("\n--- 驱动时序 ---")
    print(body)
    if proc.returncode != 0:
        print("\n[失败] 驱动退出码:", proc.returncode)
        if proc.stderr.strip():
            print(proc.stderr.strip()[:800])
        return 1

    print("\n--- 取消后观察 ---")
    time.sleep(1.0)
    n1 = count_chunks(latest_log())
    time.sleep(3.0)
    n2 = count_chunks(latest_log())
    print(f"取消后 1s chunk 数: {n1}")
    print(f"取消后 4s chunk 数: {n2}")
    print("是否仍在增长:", n2 > n1)

    print("\n--- 日志中的取消痕迹 ---")
    hits = grep_cancel(latest_log())
    if hits:
        for h in hits:
            print(" ", h)
    else:
        print("  (未找到 cancel/abort/stop 关键词)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
