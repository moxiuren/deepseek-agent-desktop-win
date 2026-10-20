#!/usr/bin/env python3
"""诊断：点「附件」到底发生了什么。

## 上一版探测失败的原因假设

上一版用 UIA `InvokePattern.Invoke()` 点 `btnAttach`，然后轮询
`AutomationElement.RootElement` 的子窗口。结果：既没找到对话框，
主窗口也**没有**被模态阻塞（`IsEnabled=True`）—— 说明对话框压根没弹。

三种可能：
  A. UIA Invoke 没触发 Chromium 的 click listener（但 btnNew 成功了，存疑）
  B. 点了，宿主 `PickFiles()` 返回空（对话框瞬间被关）
  C. 点了，宿主报错（前端收到 `{t:'err'}`）

这一版绕开 UIA，用纯 Win32 `EnumWindows` 数本进程的顶层窗口 ——
它不依赖目标进程的 UI 线程响应，能区分「窗口存在但 UIA 看不见」
与「窗口真的不存在」。

同时读前端 `attach` 容器有没有出现药丸，读 `#toast` 之类的错误提示。

用法：
    python tools/e2e/diag_attach.py
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
Add-Type -AssemblyName System.Windows.Forms

# --- Win32 枚举：不依赖 UIA ---
$sig = @"
using System;
using System.Text;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public class W {
    public delegate bool EnumProc(IntPtr h, IntPtr l);
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr l);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
    [DllImport("user32.dll")] public static extern int GetClassName(IntPtr h, StringBuilder s, int n);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowTextW(IntPtr h, StringBuilder s, int n);
    [DllImport("user32.dll")] public static extern bool IsWindowEnabled(IntPtr h);
    public static List<string> List(uint target) {
        var r = new List<string>();
        EnumWindows((h, l) => {
            uint pid; GetWindowThreadProcessId(h, out pid);
            if (pid == target) {
                var c = new StringBuilder(256); GetClassName(h, c, 256);
                var t = new StringBuilder(512); GetWindowTextW(h, t, 512);
                r.Add(string.Format("hwnd={0} vis={1} en={2} class={3} | {4}",
                    h, IsWindowVisible(h), IsWindowEnabled(h), c, t));
            }
            return true;
        }, IntPtr.Zero);
        return r;
    }
}
"@
Add-Type -TypeDefinition $sig -Language CSharp

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
$pid2 = [uint32]__PID__

[void]$sb.AppendLine('=== 点击前 ===')
foreach ($s in [W]::List($pid2)) { [void]$sb.AppendLine('  ' + $s) }

# --- 点附件 ---
$b = $w.FindFirst([System.Windows.Automation.TreeScope]::Descendants,
    (New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::AutomationIdProperty, 'btnAttach')))
if (-not $b) { Save 'NO btnAttach'; exit 1 }
$ip = $null
if (-not $b.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$ip)) {
    Save 'NO InvokePattern'; exit 1
}
$ip.Invoke()
[void]$sb.AppendLine('')
[void]$sb.AppendLine('已 Invoke btnAttach')

# --- 用 Win32 轮询，不用 UIA ---
$found = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Milliseconds 200
    $list = [W]::List($pid2)
    if ($list.Count -gt 1) {
        [void]$sb.AppendLine('')
        [void]$sb.AppendLine('=== 第 ' + $i + ' 次轮询发现多窗口 ===')
        foreach ($s in $list) { [void]$sb.AppendLine('  ' + $s) }
        $found = $true
        break
    }
}

if (-not $found) {
    [void]$sb.AppendLine('')
    [void]$sb.AppendLine('=== 6 秒内窗口数没有变化 ===')
    foreach ($s in [W]::List($pid2)) { [void]$sb.AppendLine('  ' + $s) }
}

# --- 读前端有没有错误提示 / 附件药丸 ---
[void]$sb.AppendLine('')
[void]$sb.AppendLine('=== 前端文本（找错误 / 文件名）===')
$null = $w.FindAll([System.Windows.Automation.TreeScope]::Descendants,
                   [System.Windows.Automation.Condition]::TrueCondition)
$all = $w.FindAll([System.Windows.Automation.TreeScope]::Descendants,
                  [System.Windows.Automation.Condition]::TrueCondition)
foreach ($e in $all) {
    try {
        if ($e.Current.ControlType.ProgrammaticName -eq 'ControlType.Text' -and $e.Current.Name) {
            $nm = $e.Current.Name
            if ($nm -match '附件|失败|错误|无法|gad-attach|md$') {
                [void]$sb.AppendLine('  ' + $nm)
            }
        }
    } catch { }
}

Save $sb.ToString()

# --- 兜底：若对话框确实开了，Esc 关掉 ---
Start-Sleep -Milliseconds 100
[System.Windows.Forms.SendKeys]::SendWait('{ESC}')
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
    out = Path(os.environ["TEMP"]) / "diag_attach.txt"
    if out.exists():
        out.unlink()
    env = dict(os.environ)
    env["UIA_OUT"] = str(out)

    pid = find_pid()
    script = PS.replace("__PID__", str(pid))
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
        timeout=120,
    )
    if out.exists():
        sys.stdout.write(out.read_text(encoding="utf-8"))
    else:
        print("(no output)")
    if proc.stderr.strip():
        print("[stderr]", proc.stderr.strip()[:1000])
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
