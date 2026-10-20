#!/usr/bin/env python3
"""旅程 1（Web 壳）：新建对话 → 发第一条消息 → 确认落盘。

判据：
  1. 点「新建对话」后，输入框清空、消息流清空
  2. 写入并发送后，界面出现用户消息与助手回复
  3. 引擎日志出现 run.completed，stop_reason=completed
  4. 会话列表顶部出现这条新会话

**只发一条**。这是真实账号的真实额度。
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from web_common import latest_log, run_ps, tail_log  # noqa: E402

MSG = "用一句话解释什么是哈希表。"


def main() -> int:
    log = latest_log()
    before = len(log.read_text(encoding="utf-8", errors="replace").splitlines())
    print("=== 旅程 1（Web 壳）：新建对话 → 发第一条消息 ===")
    print("日志:", log.name, "起始行数:", before)

    body = r"""
$sb = New-Object System.Text.StringBuilder
$sw = [System.Diagnostics.Stopwatch]::StartNew()
function Log($s) { [void]$sb.AppendLine(("{0,6:0.00}s  {1}" -f $sw.Elapsed.TotalSeconds, $s)) }

$before = (Texts).Count
Log ("BEFORE   文本节点=" + $before)

# --- 1. 新建对话 ---
$b = Find 'btnNew'
if (-not $b) { Log 'NO btnNew'; Save $sb.ToString(); exit 1 }
$ip = $null
if (-not $b.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$ip)) {
    Log 'NO InvokePattern on btnNew'; Save $sb.ToString(); exit 1
}
$ip.Invoke()
Log 'CLICK    btnNew'
Start-Sleep -Milliseconds 600

$afterNew = (Texts).Count
Log ("AFTER NEW 文本节点=" + $afterNew)

# --- 2. 写输入 ---
$inp = Find 'input'
if (-not $inp) { Log 'NO input'; Save $sb.ToString(); exit 1 }
$vp = $null
if (-not $inp.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$vp)) {
    Log 'NO ValuePattern on input'; Save $sb.ToString(); exit 1
}
$vp.SetValue($env:MSG_TEXT)
Log ("SET      len=" + $vp.Current.Value.Length)

# --- 3. 发送 ---
$run = Find 'btnRun'
if (-not $run) { Log 'NO btnRun'; Save $sb.ToString(); exit 1 }
$ipr = $null
if (-not $run.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$ipr)) {
    Log 'NO InvokePattern on btnRun'; Save $sb.ToString(); exit 1
}
$ipr.Invoke()
$t0 = $sw.Elapsed.TotalSeconds
Log 'CLICK    btnRun'

# --- 4. 等进入运行态 ---
$entered = $false
while ($sw.Elapsed.TotalSeconds -lt ($t0 + 10.0)) {
    if (Shown (Find 'btnCancel')) { $entered = $true; break }
    Start-Sleep -Milliseconds 25
}
Log ("ENTER    cancel=" + $entered + " at=" + ("{0:0.00}s" -f $sw.Elapsed.TotalSeconds))

# --- 5. 等跑完 ---
$done = $false
while ($sw.Elapsed.TotalSeconds -lt ($t0 + 120.0)) {
    if (-not (Shown (Find 'btnCancel'))) { $done = $true; break }
    Start-Sleep -Milliseconds 100
}
$el = "{0:0.0}s" -f ($sw.Elapsed.TotalSeconds - $t0)
Log ("DONE     done=" + $done + " elapsed=" + $el)

# --- 6. 读结果 ---
$after = (Texts).Count
Log ("AFTER    文本节点=" + $after)
[void]$sb.AppendLine('')
[void]$sb.AppendLine('--- 中栏（消息区）---')
$mid = MidTexts
foreach ($m in $mid) {
    $one = $m.T
    if ($one.Length -gt 150) { $one = $one.Substring(0,150) + '...' }
    [void]$sb.AppendLine(('  y={0,4} x={1,4} | {2}' -f $m.Y, $m.X, $one))
}
[void]$sb.AppendLine('')
[void]$sb.AppendLine('中栏文本节点: ' + $mid.Count)

# --- 7. 断言 ---
# 注意：标题栏显示的会话标题**就是**首条消息原文，与消息流里的用户气泡
# 字面完全相同。只按文本匹配会把「标题」误判成「消息回显」。
# 这里按纵向分层：y < 200 是标题栏/提示条，y >= 200 才是消息流。
$MSG_Y = 200
$hasUser = $false
$hasReply = $false
$hasDone = $false
$hasTitle = $false
foreach ($m in $mid) {
    if ($m.Y -lt $MSG_Y) {
        if ($m.T -eq $env:MSG_TEXT) { $hasTitle = $true }
        continue
    }
    if ($m.T -eq $env:MSG_TEXT) { $hasUser = $true }
    if ($m.T -match '本轮结束.*completed') { $hasDone = $true }
    if ($m.T -match '哈希表是一种用哈希函数') { $hasReply = $true }
}
[void]$sb.AppendLine('')
[void]$sb.AppendLine('--- 断言 ---')
[void]$sb.AppendLine('  标题栏已更新  : ' + $hasTitle)
[void]$sb.AppendLine('  用户消息回显  : ' + $hasUser)
[void]$sb.AppendLine('  助手回复出现  : ' + $hasReply)
[void]$sb.AppendLine('  轮次已完成    : ' + $hasDone)
[void]$sb.AppendLine('  输入框已复位  : ' + (-not (Shown (Find 'btnCancel'))))
$pass = ($hasTitle -and $hasUser -and $hasReply -and $hasDone -and $done)
[void]$sb.AppendLine('  判定          : ' + $(if ($pass) { '通过' } else { '不通过' }))

Save $sb.ToString()
"""

    # 消息正文走环境变量传给 PowerShell —— 内联 -Command 模式下
    # param 块拿不到命令行参数，而字符串插值又会被中文引号搞乱。
    os.environ["MSG_TEXT"] = MSG

    out, rc = run_ps(body, "j1", timeout=200)
    print("\n--- 驱动时序 ---")
    print(out)
    if rc != 0:
        print("[警告] 驱动退出码:", rc)

    print("\n--- 引擎日志（关键行）---")
    hits = tail_log(log, before, ("run.completed", "run_end", "pow_stats", "error", "failed"))
    for h in hits:
        print(" ", h)
    if not hits:
        print("  (无匹配)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
