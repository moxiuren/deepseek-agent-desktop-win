#!/usr/bin/env python3
"""探测 ConvScroll 里的消息内容。

## 为什么要单独一个工具

前几轮用「读 Text 控件的 Name」来观察对话内容，结果主区域永远是 0 条 ——
差点被判定成「接管后历史消息没渲染」这个产品缺陷。

真相是：消息体是**只读 TextBox**（UIA 类型 `Edit`），文本存在
`ValuePattern.Current.Value` 里，`Current.Name` 恒为空字符串。
用 `ControlType.Text` 过滤等于给自己戴眼罩。

这个工具按 `ConvScroll` 容器收口，逐条读 Edit 的真实文本，用于端到端
旅程里「确认界面真的显示了什么」。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROC_NAME = "DeepSeekAgent.Desktop"

PS = r"""
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$root = [System.Windows.Automation.AutomationElement]::RootElement
$c = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ProcessIdProperty, __PID__)
$w = $root.FindFirst([System.Windows.Automation.TreeScope]::Children, $c)
if (-not $w) { throw "NO WINDOW" }

function Save($s) {
    [System.IO.File]::WriteAllText($env:UIA_OUT, $s, (New-Object System.Text.UTF8Encoding($false)))
}

$sb = New-Object System.Text.StringBuilder

$conv = $w.FindFirst([System.Windows.Automation.TreeScope]::Descendants,
  (New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::AutomationIdProperty, 'ConvScroll')))
if (-not $conv) { Save 'NO ConvScroll'; exit 1 }

[void]$sb.AppendLine('ConvScroll rect: ' + $conv.Current.BoundingRectangle.ToString())
[void]$sb.AppendLine('')

$edits = $conv.FindAll([System.Windows.Automation.TreeScope]::Descendants,
  (New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
    [System.Windows.Automation.ControlType]::Edit)))

[void]$sb.AppendLine('消息块数: ' + $edits.Count)
[void]$sb.AppendLine('')

$idx = 0
foreach ($e in $edits) {
    $idx++
    $r = $e.Current.BoundingRectangle
    $txt = ''
    $vp = $null
    if ($e.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$vp)) {
        $txt = $vp.Current.Value
    } else {
        $tp = $null
        if ($e.TryGetCurrentPattern([System.Windows.Automation.TextPattern]::Pattern, [ref]$tp)) {
            $txt = $tp.DocumentRange.GetText(4000)
        }
    }
    $len = $txt.Length
    # 换行压成一行，避免输出被撑爆
    $one = ($txt -split "`r?`n") -join ' / '
    if ($one.Length -gt 200) { $one = $one.Substring(0, 200) + ' ...[全 ' + $len + ' 字]' }
    [void]$sb.AppendLine('[' + $idx + '] y=' + [int]$r.Y + ' h=' + [int]$r.Height + ' len=' + $len)
    [void]$sb.AppendLine('    ' + $one)
    [void]$sb.AppendLine('')
}

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
    out = Path(os.environ["TEMP"]) / "probe_conv.txt"
    if out.exists():
        out.unlink()
    env = dict(os.environ)
    env["UIA_OUT"] = str(out)

    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command", PS.replace("__PID__", str(find_pid()))],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
    )
    if out.exists():
        sys.stdout.write(out.read_text(encoding="utf-8"))
    else:
        print("(no output)")
        if proc.stderr.strip():
            print(proc.stderr.strip()[:1000])
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
