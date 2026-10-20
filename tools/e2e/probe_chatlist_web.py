#!/usr/bin/env python3
"""只读探测：Web 壳左侧会话列表的结构。

## 为什么不能照搬 WPF 壳的做法

两套壳的会话列表语义完全不同：

  · WPF 壳：「本地 / 网页版」双页签，是**两个数据源**的并列
  · Web 壳：单一列表源（账号下的服务端会话），每条带「已接管 / 未接管」标记

所以 WPF 壳那套「切页签 → 点列表项」的驱动在 Web 壳上必然失效。
必须重新探测：列表项是什么控件类型、带什么 AutomationId、状态怎么表达。

## 探测策略（四级降级，同 MidTexts 的思路）

`<div id="chatList">` 是无语义角色的容器，Chromium 不暴露它的
AutomationId（和 `stream` 一样）。但列表项是有角色的，所以：

  1. 先找所有 ListItem / Button / Link 角色的元素，带 x < 200 的（左栏）
  2. 打印它们的 Name（会话标题）、AutomationId、位置
  3. 同时用几何法读左栏全部文本（含「已接管 / 未接管」标记）

用法：
    python tools/e2e/probe_chatlist_web.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from web_common import find_pid, run_ps  # noqa: E402

BODY = r"""
$sb = New-Object System.Text.StringBuilder

# --- 1. 左栏带语义角色的元素 ---
$all = $w.FindAll([System.Windows.Automation.TreeScope]::Descendants,
                  [System.Windows.Automation.Condition]::TrueCondition)
[void]$sb.AppendLine('=== 左栏（x < 200）带角色的元素 ===')
$n = 0
foreach ($e in $all) {
    try {
        $b = $e.Current.BoundingRectangle
        if ($b.Width -le 0 -or $b.X -ge 200) { continue }
        $ct = $e.Current.ControlType.ProgrammaticName.Replace('ControlType.','')
        $nm = $e.Current.Name
        $aid = $e.Current.AutomationId
        if (-not $nm -and -not $aid) { continue }
        if ($ct -eq 'Text') { continue }   # 文本单独统计
        $one = $nm
        if ($one.Length -gt 60) { $one = $one.Substring(0,60) + '...' }
        [void]$sb.AppendLine(('  {0,-12} aid={1,-16} y={2,4} x={3,4} | {4}' -f $ct, $aid, [int]$b.Y, [int]$b.X, $one))
        $n++
    } catch { }
}
[void]$sb.AppendLine('  合计: ' + $n)
[void]$sb.AppendLine('')

# --- 2. 左栏全部文本（含状态标记）---
[void]$sb.AppendLine('=== 左栏文本（按 y 排序）===')
$txt = @()
foreach ($e in $all) {
    try {
        if ($e.Current.ControlType.ProgrammaticName -ne 'ControlType.Text') { continue }
        if (-not $e.Current.Name) { continue }
        $b = $e.Current.BoundingRectangle
        if ($b.Width -le 0 -or $b.X -ge 200) { continue }
        $txt += [pscustomobject]@{ Y = [int]$b.Y; X = [int]$b.X; T = $e.Current.Name }
    } catch { }
}
foreach ($m in ($txt | Sort-Object Y, X)) {
    $one = $m.T
    if ($one.Length -gt 70) { $one = $one.Substring(0,70) + '...' }
    [void]$sb.AppendLine(('  y={0,4} x={1,4} | {2}' -f $m.Y, $m.X, $one))
}
[void]$sb.AppendLine('  合计: ' + $txt.Count)
[void]$sb.AppendLine('')

# --- 3. 顶层按钮（新建 / 刷新等）---
[void]$sb.AppendLine('=== 已知 id 的控件位置 ===')
foreach ($aid in @('btnNew','btnRefresh','chatList','btnAttach','input','btnRun')) {
    $e = Find $aid
    if ($e) {
        $b = $e.Current.BoundingRectangle
        [void]$sb.AppendLine(('  {0,-12} y={1,4} x={2,4} w={3,4} h={4,3} shown={5}' -f $aid, [int]$b.Y, [int]$b.X, [int]$b.Width, [int]$b.Height, (Shown $e)))
    } else {
        [void]$sb.AppendLine(('  {0,-12} NOT FOUND' -f $aid))
    }
}

Save $sb.ToString()
"""


def main() -> int:
    pid = find_pid()
    print("进程 PID:", pid)
    out, code = run_ps(BODY, "probe_chatlist")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
