#!/usr/bin/env python3
"""Web 壳（WebView2）UIA 驱动。

## 与 WPF 壳的驱动有什么不同

1. **进程名不同**：`DeepSeekAgent.Web`（WPF 壳是 `DeepSeekAgent.Desktop`）。
   两者 namespace 相同、共用 AgentHost，只有 AssemblyName 区分。

2. **Chromium 的 accessibility tree 是惰性的**。第一次 UIA 连接会触发构建，
   但每次新进程连接都可能从零开始。所以这里**每次操作前先全量遍历一次**
   预热树 —— 否则 `FindFirst` 按 AutomationId 会返回 null，看起来像
   「元素不存在」，实际只是树还没建。

3. **纯文本 span 不暴露 AutomationId**。Chromium 只给有语义角色的元素
   （button / input / select）暴露 id，`<span id="pillText">` 会被压成
   无名 Text 节点。要读它们只能靠 Text 节点顺序或父容器定位。

4. **HTML 的 `id` → UIA 的 `AutomationId`**。这意味着前端 DOM 的 id
   就是天然的测试锚点，改动 id 等于破坏测试契约。

用法：
    python tools/uia/web.py dump            全量树
    python tools/uia/web.py ids             只看有 AutomationId 的元素
    python tools/uia/web.py text            全部文本节点（按 DOM 顺序）
    python tools/uia/web.py click <aid>     点按钮
    python tools/uia/web.py set <aid> <v>   给输入框设值
    python tools/uia/web.py state           读关键状态（pill / 标题 / 会话数）
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROC_NAME = "DeepSeekAgent.Web"

PS_HEAD = r"""
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$root = [System.Windows.Automation.AutomationElement]::RootElement
$c = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ProcessIdProperty, __PID__)
$w = $root.FindFirst([System.Windows.Automation.TreeScope]::Children, $c)
if (-not $w) { throw "NO WINDOW (process __PID__)" }

# 预热：强制 Chromium 构建 accessibility tree。
# 不预热的话，紧接着的 FindFirst 常常拿到 null —— 树是惰性建的。
$null = $w.FindAll([System.Windows.Automation.TreeScope]::Descendants,
                    [System.Windows.Automation.Condition]::TrueCondition)
Start-Sleep -Milliseconds 300

function Save($s) {
    [System.IO.File]::WriteAllText($env:UIA_OUT, $s, (New-Object System.Text.UTF8Encoding($false)))
}
function Find($aid) {
    $w.FindFirst([System.Windows.Automation.TreeScope]::Descendants,
        (New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::AutomationIdProperty, $aid)))
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
        raise SystemExit(f"进程 {PROC_NAME} 未运行 —— 先启动 "
                         f"shell/DeepSeekAgent.Web/bin/Debug/net8.0-windows/DeepSeekAgent.Web.exe")
    return int(t)


ACTIONS: dict[str, str] = {
    "dump": r"""
$all = $w.FindAll([System.Windows.Automation.TreeScope]::Descendants,
                  [System.Windows.Automation.Condition]::TrueCondition)
$sb = New-Object System.Text.StringBuilder
[void]$sb.AppendLine('元素总数: ' + $all.Count)
[void]$sb.AppendLine('')
$i = 0
foreach ($e in $all) {
    $i++
    try {
        $ct  = $e.Current.ControlType.ProgrammaticName -replace 'ControlType\.',''
        $aid = $e.Current.AutomationId
        $nm  = $e.Current.Name
        $cls = $e.Current.ClassName
        if ($nm -and $nm.Length -gt 70) { $nm = $nm.Substring(0,70) + '...' }
        [void]$sb.AppendLine(('#{0,-4} {1,-12} aid={2,-16} cls={3,-20} | {4}' -f $i,$ct,$aid,$cls,$nm))
    } catch { }
}
Save $sb.ToString()
""",

    "ids": r"""
$all = $w.FindAll([System.Windows.Automation.TreeScope]::Descendants,
                  [System.Windows.Automation.Condition]::TrueCondition)
$sb = New-Object System.Text.StringBuilder
$map = @{}
foreach ($e in $all) {
    try { if ($e.Current.AutomationId) { $map[$e.Current.AutomationId] = $e } } catch { }
}
[void]$sb.AppendLine('有 AutomationId 的元素: ' + $map.Count)
[void]$sb.AppendLine('')
foreach ($k in ($map.Keys | Sort-Object)) {
    $e = $map[$k]
    $ct = $e.Current.ControlType.ProgrammaticName -replace 'ControlType\.',''
    $nm = $e.Current.Name
    if ($nm -and $nm.Length -gt 50) { $nm = $nm.Substring(0,50) + '...' }
    $off = $e.Current.IsOffscreen
    [void]$sb.AppendLine(('  {0,-18} {1,-10} offscreen={2,-5} | {3}' -f $k,$ct,$off,$nm))
}
Save $sb.ToString()
""",

    "text": r"""
$all = $w.FindAll([System.Windows.Automation.TreeScope]::Descendants,
                  [System.Windows.Automation.Condition]::TrueCondition)
$sb = New-Object System.Text.StringBuilder
$i = 0
foreach ($e in $all) {
    try {
        if ($e.Current.ControlType.ProgrammaticName -eq 'ControlType.Text') {
            $nm = $e.Current.Name
            if ($nm) {
                if ($nm.Length -gt 120) { $nm = $nm.Substring(0,120) + '...' }
                $i++
                [void]$sb.AppendLine(('[{0}] {1}' -f $i,$nm))
            }
        }
    } catch { }
}
[void]$sb.AppendLine('')
[void]$sb.AppendLine('文本节点数: ' + $i)
Save $sb.ToString()
""",

    "state": r"""
$sb = New-Object System.Text.StringBuilder
# pill 是 <span class="pill"><i/><b id="pillText">…</b></span>
# pillText 没有语义角色，id 不暴露；退而求其次读 pill 的子孙文本
$pill = Find 'pill'
if ($pill) {
    [void]$sb.AppendLine('PILL class = ' + $pill.Current.ClassName)
    $ts = $pill.FindAll([System.Windows.Automation.TreeScope]::Descendants,
        (New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
            [System.Windows.Automation.ControlType]::Text)))
    foreach ($t in $ts) { if ($t.Current.Name) { [void]$sb.AppendLine('  pillText = ' + $t.Current.Name) } }
} else { [void]$sb.AppendLine('PILL = <NOT FOUND>') }

# 会话列表：chatList 是无角色的 div，id 不暴露。用「账号下」这个标签定位其后
# 的兄弟文本 —— 这是当前树里唯一稳定的锚。
$all = $w.FindAll([System.Windows.Automation.TreeScope]::Descendants,
                  [System.Windows.Automation.Condition]::TrueCondition)
$texts = @()
foreach ($e in $all) {
    try { if ($e.Current.ControlType.ProgrammaticName -eq 'ControlType.Text' -and $e.Current.Name) {
        $texts += $e.Current.Name
    } } catch { }
}
[void]$sb.AppendLine('')
[void]$sb.AppendLine('文本节点总数: ' + $texts.Count)
[void]$sb.AppendLine('')
[void]$sb.AppendLine('--- 前 40 条文本 ---')
$n = 0
foreach ($t in $texts) {
    $one = $t
    if ($one.Length -gt 80) { $one = $one.Substring(0,80) + '...' }
    [void]$sb.AppendLine('  ' + $one)
    $n++; if ($n -ge 40) { break }
}
Save $sb.ToString()
""",

    "click": r"""
$e = Find $env:UIA_TARGET
if (-not $e) { Save ('NOT FOUND: ' + $env:UIA_TARGET); exit 1 }
$ip = $null
if (-not $e.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$ip)) {
    # HTML 元素常常只有 Invoke 或只有 Toggle/SelectionItem，逐条试
    $tp = $null
    if ($e.TryGetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern, [ref]$tp)) {
        $tp.Toggle(); Save ('TOGGLED: ' + $env:UIA_TARGET); exit 0
    }
    Save ('NO InvokePattern: ' + $env:UIA_TARGET + ' (type=' + $e.Current.ControlType.ProgrammaticName + ')')
    exit 1
}
$ip.Invoke()
Save ('CLICKED: ' + $env:UIA_TARGET + ' | ' + $e.Current.Name)
""",

    "set": r"""
$e = Find $env:UIA_TARGET
if (-not $e) { Save ('NOT FOUND: ' + $env:UIA_TARGET); exit 1 }
$vp = $null
if (-not $e.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$vp)) {
    Save ('NO ValuePattern: ' + $env:UIA_TARGET); exit 1
}
$vp.SetValue($env:UIA_VALUE)
Save ('SET: ' + $env:UIA_TARGET + ' len=' + $vp.Current.Value.Length)
""",
}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in ACTIONS:
        print(__doc__)
        return 1

    action = sys.argv[1]
    out = Path(os.environ["TEMP"]) / "web_uia_out.txt"
    if out.exists():
        out.unlink()

    env = dict(os.environ)
    env["UIA_OUT"] = str(out)
    env["UIA_TARGET"] = sys.argv[2] if len(sys.argv) > 2 else ""
    env["UIA_VALUE"] = sys.argv[3] if len(sys.argv) > 3 else ""

    script = PS_HEAD.replace("__PID__", str(find_pid())) + ACTIONS[action]
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
    )

    if out.exists():
        sys.stdout.write(out.read_text(encoding="utf-8"))
    else:
        print("(no output)")
        if proc.stderr.strip():
            print(proc.stderr.strip()[:1200])
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
