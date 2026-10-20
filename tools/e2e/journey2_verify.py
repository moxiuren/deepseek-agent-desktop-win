#!/usr/bin/env python3
"""旅程 2 下半段：新建对话 → 挂附件 → 发消息 → 验证服务端真的解析了内容。

## 判据设计（这是整条旅程的关键）

「附件发出去了」不等于「服务端读了内容」。三种可能都符合前者：

  1. 服务端解析了文件，模型能看到全文 —— **产品目标行为**
  2. 只传了文件名/大小，模型看不到内容 —— 半成品
  3. 前端把附件当普通文本塞进提示词 —— 那就不叫附件了

区分它们的唯一办法是**内容里放只有读过文件才知道的字符串**。
`gad-attach-probe.md` 里有三个校验值：

    校验码 A：ZQ7X-4419
    校验码 B：芒果与齿轮的编号是 8391
    校验码 C：本项目内部代号为「晨雾计划」

问模型「文件里三个校验码分别是什么」，能说出任意两个 → 判据 1 成立。

## 为什么必须由 Python 编排

三步各自的执行环境不同，且顺序有依赖：

  1. 新建对话 —— UIA（PowerShell 内联）
  2. 挂附件   —— 真实鼠标 + 模态 Win32 对话框（Python ctypes，见 journey2_attach.py）
  3. 发消息   —— UIA（PowerShell 内联）

而且 `btnNew` 会清空输入区（含已挂附件），所以挂附件必须排在新建之后。
塞进同一个 PowerShell 会话做不到第 2 步，只能由 Python 串。

用法：
    python tools/e2e/journey2_verify.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from web_common import find_pid, run_ps  # noqa: E402

MSG = "请读取我附上的文件，把里面的三个校验码原样列出来，不要推测。"


def ps_body(src: str) -> str:
    """给每段 PowerShell 加上 sb/Log 的脚手架。"""
    return ("$sb = New-Object System.Text.StringBuilder\n"
            "function Log($s) { [void]$sb.AppendLine($s) }\n"
            "function Invoke($e) {\n"
            "  if (-not $e) { return $false }\n"
            "  $p = $null\n"
            "  if ($e.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$p)) {\n"
            "    $p.Invoke(); return $true\n"
            "  }\n"
            "  return $false\n"
            "}\n") + src


BODY_NEW = ps_body(r"""
$new = Find 'btnNew'
if (-not $new) { Log 'NO btnNew'; Save $sb.ToString(); exit 1 }
Log ('CLICK btnNew = ' + (Invoke $new))
Start-Sleep -Milliseconds 1200
$mid = MidTexts
Log ('新建后中栏文本节点=' + $mid.Count)
Save $sb.ToString()
""")

BODY_SEND = ps_body(r"""
$t0 = Get-Date

# 确认附件 chip 已在输入区
$mid0 = MidTexts
$hasChip = $false
foreach ($m in $mid0) { if ($m.T -match 'gad-attach-probe') { $hasChip = $true } }
Log ('附件 chip 在位 = ' + $hasChip)
if (-not $hasChip) { Log '附件未挂上'; Save $sb.ToString(); exit 2 }

$inp = Find 'input'
if (-not $inp) { Log 'NO input'; Save $sb.ToString(); exit 3 }
$vp = $inp.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
$vp.SetValue($env:MSG_TEXT)
Start-Sleep -Milliseconds 700
Log 'SET done'

$run = Find 'btnRun'
Log ('CLICK btnRun = ' + (Invoke $run))

$done = $false
for ($i = 0; $i -lt 150; $i++) {
    Start-Sleep -Milliseconds 500
    $mid = MidTexts
    foreach ($m in $mid) { if ($m.T -match '本轮结束') { $done = $true; break } }
    if ($done) { break }
}
$elapsed = ((Get-Date) - $t0).TotalSeconds
Log ('DONE = ' + $done + '  耗时=' + [math]::Round($elapsed,2) + 's')

$mid = MidTexts
[void]$sb.AppendLine('')
[void]$sb.AppendLine('--- 中栏（消息区）---')
foreach ($m in $mid) {
    $one = $m.T
    if ($one.Length -gt 220) { $one = $one.Substring(0,220) + '...' }
    [void]$sb.AppendLine(('  y={0,4} x={1,4} | {2}' -f $m.Y, $m.X, $one))
}
[void]$sb.AppendLine('')

$all = ($mid | ForEach-Object { $_.T }) -join "`n"
$hasA = $all -match 'ZQ7X-4419'
$hasB = $all -match '8391'
$hasC = $all -match '晨雾计划'
$hits = 0
if ($hasA) { $hits++ }
if ($hasB) { $hits++ }
if ($hasC) { $hits++ }

[void]$sb.AppendLine('--- 断言 ---')
[void]$sb.AppendLine('  校验码 A (ZQ7X-4419) : ' + $hasA)
[void]$sb.AppendLine('  校验码 B (8391)      : ' + $hasB)
[void]$sb.AppendLine('  校验码 C (晨雾计划)  : ' + $hasC)
[void]$sb.AppendLine('  命中数               : ' + $hits + ' / 3')
[void]$sb.AppendLine('  判定                 : ' + $(if ($hits -ge 2) { '通过（服务端确实解析了内容）' } else { '不通过（附件内容未送达模型）' }))

Save $sb.ToString()
""")


def step(title: str) -> None:
    print()
    print("=" * 60)
    print(title)
    print("=" * 60)


def main() -> int:
    pid = find_pid()
    os.environ["MSG_TEXT"] = MSG
    print("进程 PID:", pid)
    print("消息:", MSG)

    step("1/3 新建对话")
    out, _ = run_ps(BODY_NEW, "j2_new")
    print(out)
    if "NO btnNew" in out:
        return 1

    step("2/3 挂附件（真实鼠标 + Win32 模态对话框）")
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(HERE / "journey2_attach.py"), "--pick"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=180,
    )
    print(proc.stdout)
    if proc.returncode != 0:
        print("[失败] 挂附件退出码", proc.returncode)
        return 2

    step("3/3 发送并验证")
    out, _ = run_ps(BODY_SEND, "j2_send", timeout=300)
    print(out)

    if "通过（服务端确实解析了内容）" in out:
        return 0
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
