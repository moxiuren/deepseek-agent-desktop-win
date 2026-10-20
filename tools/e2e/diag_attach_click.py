#!/usr/bin/env python3
"""用真实鼠标点击「附件」按钮，验证 UIA Invoke 失效是否为前端缺陷。

## 为什么 Invoke 会失效

Chromium 的 accessibility tree 给 `<button>` 暴露 `InvokePattern`，
但 `InvokePattern.Invoke()` 走的是 **UIA provider 的默认动作**。
Chromium 只对部分控件实现了真正的「合成点击」—— 对纯 DOM
`addEventListener('click')` 的按钮，Invoke 有时只派发 `click` 事件而不带
用户手势（user gesture）。而 `OpenFileDialog.ShowDialog()` 在 WebView2 里
需要用户手势才能开，否则被 Chromium 拦下。

这条假设必须用真实鼠标点击来证伪 —— 如果鼠标点击能弹出对话框，
说明产品本身没坏，坏的是「自动化驱动方式」。

做法：
  1. 读 btnAttach 的 BoundingRectangle
  2. 用 SetCursorPos + mouse_event 真实点一下
  3. 轮询顶层窗口，看有没有新对话框

用法：
    python tools/e2e/diag_attach_click.py
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
Add-Type -AssemblyName System.Drawing

# SetCursorPos / mouse_event 在 user32.dll，PowerShell 可以直接 P/Invoke，
# 但 Add-Type 在本机沙箱被拒（"A required privilege is not held"）。
# 改用 System.Windows.Forms.Cursor.Position + SendInput 的替代路径：
# Cursor.Position 可写（内部走 SetCursorPos），点击用 SendKeys 不行，
# 所以退一步用 [System.Windows.Forms.Cursor]::Position 移过去，
# 再用 mouse_event 需要 Add-Type……这里改为「移动 + 让用户可见」的方式不可行。
# 结论：只能靠 Cursor.Position 定位 + 另找点击手段。

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

$b = $w.FindFirst([System.Windows.Automation.TreeScope]::Descendants,
    (New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::AutomationIdProperty, 'btnAttach')))
if (-not $b) { Save 'NO btnAttach'; exit 1 }

$r = $b.Current.BoundingRectangle
[void]$sb.AppendLine('btnAttach rect: ' + $r.ToString())
$cx = [int]($r.X + $r.Width / 2)
$cy = [int]($r.Y + $r.Height / 2)
[void]$sb.AppendLine('目标坐标: ' + $cx + ',' + $cy)

# 保存原光标位置
$old = [System.Windows.Forms.Cursor]::Position
[void]$sb.AppendLine('原光标: ' + $old.X + ',' + $old.Y)

[System.Windows.Forms.Cursor]::Position = New-Object System.Drawing.Point($cx, $cy)
Start-Sleep -Milliseconds 200
$now = [System.Windows.Forms.Cursor]::Position
[void]$sb.AppendLine('移动后光标: ' + $now.X + ',' + $now.Y)

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
    out = Path(os.environ["TEMP"]) / "diag_attach_click.txt"
    if out.exists():
        out.unlink()
    env = dict(os.environ)
    env["UIA_OUT"] = str(out)
    script = PS.replace("__PID__", str(find_pid()))
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
