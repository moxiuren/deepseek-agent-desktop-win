#!/usr/bin/env python3
"""探测 Web 壳「附件」按钮弹出的原生文件对话框结构。

## 为什么要单独探测

`PickFilesForUpload()` 用的是 `Microsoft.Win32.OpenFileDialog`，那是
Win32 通用对话框（Common Item Dialog），**不是 WPF 控件**。它的 UIA 树
与主窗口完全独立，且是**模态**的 —— 脚本一旦卡住，主窗口就一直不可交互。

所以先探测结构，再写旅程脚本。探测本身带兜底：无论成败最后都按 Esc，
确保对话框一定关掉。

用法：
    python tools/e2e/probe_attach_dialog.py
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROC_NAME = "DeepSeekAgent.Web"

PS = r"""
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

$sb = New-Object System.Text.StringBuilder

# 1. 点「附件」——Invoke 是异步投递，不等它返回就继续
$b = $w.FindFirst([System.Windows.Automation.TreeScope]::Descendants,
    (New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::AutomationIdProperty, 'btnAttach')))
if (-not $b) { Save 'NO btnAttach'; exit 1 }
$ip = $null
if (-not $b.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$ip)) {
    Save 'NO InvokePattern'; exit 1
}
$ip.Invoke()
[void]$sb.AppendLine('已点 btnAttach，等对话框…')

# 2. 轮询顶层窗口，找新的那个（对话框）
$dlg = $null
for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Milliseconds 150
    $kids = $root.FindAll([System.Windows.Automation.TreeScope]::Children,
                          [System.Windows.Automation.Condition]::TrueCondition)
    foreach ($k in $kids) {
        try {
            $nm = $k.Current.Name
            $cls = $k.Current.ClassName
            if ($k.Current.NativeWindowHandle -ne 0 -and
                ($cls -match '#32770' -or $nm -match '附件|附件|选择要发给')) {
                $dlg = $k
                [void]$sb.AppendLine('找到对话框: name=' + $nm + ' class=' + $cls)
                break
            }
        } catch { }
    }
    if ($dlg) { break }
}

if (-not $dlg) {
    [void]$sb.AppendLine('未找到对话框窗口。全部顶层窗口：')
    $kids = $root.FindAll([System.Windows.Automation.TreeScope]::Children,
                          [System.Windows.Automation.Condition]::TrueCondition)
    foreach ($k in $kids) {
        try {
            [void]$sb.AppendLine(('  hwnd={0} class={1} | {2}' -f
                $k.Current.NativeWindowHandle, $k.Current.ClassName, $k.Current.Name))
        } catch { }
    }
    Save $sb.ToString()
    # 兜底：无论如何按 Esc
    [System.Windows.Forms.SendKeys]::SendWait('{ESC}')
    exit 1
}

# 3. 导出对话框内部结构
[void]$sb.AppendLine('')
[void]$sb.AppendLine('--- 对话框内部元素 ---')
$all = $dlg.FindAll([System.Windows.Automation.TreeScope]::Descendants,
                    [System.Windows.Automation.Condition]::TrueCondition)
[void]$sb.AppendLine('元素总数: ' + $all.Count)
$i = 0
foreach ($e in $all) {
    $i++
    try {
        $ct  = $e.Current.ControlType.ProgrammaticName -replace 'ControlType\.',''
        $aid = $e.Current.AutomationId
        $nm  = $e.Current.Name
        if ($nm -and $nm.Length -gt 60) { $nm = $nm.Substring(0,60) + '...' }
        # 只打印可能有用的：有 aid 的、或 Edit/Button/ComboBox
        if ($aid -or $ct -in @('Edit','Button','ComboBox','List','ListItem')) {
            [void]$sb.AppendLine(('#{0,-3} {1,-10} aid={2,-10} | {3}' -f $i,$ct,$aid,$nm))
        }
    } catch { }
}

Save $sb.ToString()

# 4. 兜底关闭：Esc
Start-Sleep -Milliseconds 200
[System.Windows.Forms.SendKeys]::SendWait('{ESC}')
[void]$sb.AppendLine('')
[void]$sb.AppendLine('已按 Esc 关闭对话框。')
Save $sb.ToString()
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


def main() -> int:
    out = Path(os.environ["TEMP"]) / "probe_attach_dialog.txt"
    if out.exists():
        out.unlink()
    env = dict(os.environ)
    env["UIA_OUT"] = str(out)

    # SendKeys 需要 System.Windows.Forms
    script = ("Add-Type -AssemblyName System.Windows.Forms\n"
              + PS.replace("__PID__", str(find_pid())))
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
        timeout=90,
    )
    if out.exists():
        sys.stdout.write(out.read_text(encoding="utf-8"))
    else:
        print("(no output)")
    if proc.stderr.strip():
        print("[stderr]", proc.stderr.strip()[:800])
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
