#!/usr/bin/env python3
"""旅程 4 后半段：接管后能否继续对话。

判据：
  1. TxtInput 可写入
  2. 点运行后进入运行态
  3. 引擎日志出现 completion，且带 x-ds-pow-response（说明走的是同一条会话）
  4. 界面出现新的消息块

**一次只发一条**：这是真实账号的真实会话，反复重试既有封号风险，
也会污染服务端的会话历史。测一次拿到证据就停。
"""
from __future__ import annotations

import glob
import os
import subprocess
import sys
import time
from pathlib import Path

PROC_NAME = "DeepSeekAgent.Desktop"
LOG_DIR = Path(os.environ["LOCALAPPDATA"]) / "DeepSeek-Agent" / "logs"

MSG = "谢谢，这个价格里包含运输费用吗？"

PS = r"""
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$root = [System.Windows.Automation.AutomationElement]::RootElement
$c = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ProcessIdProperty, __PID__)
$w = $root.FindFirst([System.Windows.Automation.TreeScope]::Children, $c)
if (-not $w) { throw "NO WINDOW" }

function Find($aid) {
    $w.FindFirst([System.Windows.Automation.TreeScope]::Descendants,
        (New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::AutomationIdProperty, $aid)))
}
function Shown($e) { if ($e) { -not $e.Current.IsOffscreen } else { $false } }

$sb = New-Object System.Text.StringBuilder
$sw = [System.Diagnostics.Stopwatch]::StartNew()
function Log($s) { [void]$sb.AppendLine(("{0,6:0.00}s  {1}" -f $sw.Elapsed.TotalSeconds, $s)) }
function Save() {
    [System.IO.File]::WriteAllText($env:UIA_OUT, $sb.ToString(),
        (New-Object System.Text.UTF8Encoding($false)))
}

Log ("INITIAL  run=" + (Shown (Find 'BtnRun')) + " cancel=" + (Shown (Find 'BtnCancel')))

$i = Find 'TxtInput'
$vp = $null
if (-not $i.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$vp)) {
    Log 'NO ValuePattern on TxtInput'; Save; exit 1
}
$vp.SetValue($env:MSG_TEXT)
Log ("SET      len=" + $vp.Current.Value.Length)

$run = Find 'BtnRun'
$ip = $null
if (-not $run.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$ip)) {
    Log 'NO InvokePattern on BtnRun'; Save; exit 1
}
$ip.Invoke()
$tRun = $sw.Elapsed.TotalSeconds
Log 'CLICK    BtnRun'

# 等进入运行态
$entered = $false
while ($sw.Elapsed.TotalSeconds -lt ($tRun + 8.0)) {
    if (Shown (Find 'BtnCancel')) { $entered = $true; break }
    Start-Sleep -Milliseconds 20
}
Log ("ENTER    cancel=" + (Shown (Find 'BtnCancel')) + " entered=" + $entered)

# 等跑完（回到空闲态），最多 120 秒
$done = $false
while ($sw.Elapsed.TotalSeconds -lt ($tRun + 120.0)) {
    if ((Shown (Find 'BtnRun')) -and -not (Shown (Find 'BtnCancel'))) { $done = $true; break }
    Start-Sleep -Milliseconds 100
}
$elapsed = "{0:0.0}s" -f ($sw.Elapsed.TotalSeconds - $tRun)
Log ("BACK     run=" + (Shown (Find 'BtnRun')) + " done=" + $done + " elapsed=" + $elapsed)

Save
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


def latest_log() -> Path:
    files = sorted(glob.glob(str(LOG_DIR / "engine-*.log")), key=os.path.getmtime)
    return Path(files[-1])


def main() -> int:
    log = latest_log()
    before_lines = len(log.read_text(encoding="utf-8", errors="replace").splitlines())
    print("=== 旅程 4 后半段：接管后继续对话 ===")
    print("日志:", log.name, " 起始行数:", before_lines)

    out = Path(os.environ["TEMP"]) / "j4_continue.txt"
    if out.exists():
        out.unlink()
    env = dict(os.environ)
    env["UIA_OUT"] = str(out)
    env["MSG_TEXT"] = MSG

    t0 = time.time()
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command", PS.replace("__PID__", str(find_pid()))],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
        timeout=180,
    )
    print("\n--- 驱动时序 ---")
    print(out.read_text(encoding="utf-8").strip() if out.exists() else "(no output)")
    print(f"\n墙钟总耗时: {time.time() - t0:.1f}s")
    if proc.returncode != 0 and proc.stderr.strip():
        print("[stderr]", proc.stderr.strip()[:600])

    print("\n--- 新增日志行（关键字段）---")
    lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    new = lines[before_lines:]
    hits = 0
    for ln in new:
        low = ln.lower()
        if any(k in low for k in ("pow_cache", "pow_ms", "challenge_ms", "stop_reason",
                                  "x-ds-pow", "completion", "error", "failed")):
            print(" ", ln[:300])
            hits += 1
            if hits >= 25:
                break
    if hits == 0:
        print("  (无匹配行，新增行数:", len(new), ")")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
