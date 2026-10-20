#!/usr/bin/env python3
"""只读检查：附件是否真的挂到了输入区。

## 为什么需要这一步

`journey2_attach.py --pick` 的「写入后读回」是空串。这有两种解释：

  · 路径真的没写进去（写到了 ComboBoxEx 而不是内层 Edit）
  · 写进去了，但跨进程 `GetWindowText` 拿不到内层 Edit 的文本
    （ComboBoxEx 的文本由内部维护，未必走 WM_GETTEXT）

**两种解释对产品的影响完全不同**，所以不能靠对话框内部的自述来判断，
必须看前端的可观测状态：输入区上方是否出现了附件 chip。

`app.js` 的 `onPickedFiles(list)` 会做去重、过滤空串后 `renderAttach()`，
附件 chip 落在 `#attach` 容器里 —— 那是纯 div，没有语义角色，
所以仍然用 `MidTexts` 的几何法读文本。

用法：
    python tools/e2e/check_attach_state.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from web_common import find_pid, run_ps  # noqa: E402

BODY = r"""
$sb = New-Object System.Text.StringBuilder
function Log($s) { [void]$sb.AppendLine($s) }

$mid = MidTexts
Log ('中栏文本节点=' + $mid.Count)
[void]$sb.AppendLine('')
[void]$sb.AppendLine('--- 中栏全部文本 ---')
foreach ($m in $mid) {
    $one = $m.T
    if ($one.Length -gt 120) { $one = $one.Substring(0,120) + '...' }
    [void]$sb.AppendLine(('  y={0,4} x={1,4} | {2}' -f $m.Y, $m.X, $one))
}
[void]$sb.AppendLine('')

# 输入区附件 chip 的关键词。app.js 的 renderAttach 会把文件名渲染成 chip，
# 通常带一个移除按钮；这里只按文件名匹配，不猜 DOM 结构。
$hit = @()
foreach ($m in $mid) {
    if ($m.T -match 'gad-attach-probe|\.md') { $hit += $m }
}
[void]$sb.AppendLine('--- 附件痕迹 ---')
if ($hit.Count -eq 0) {
    [void]$sb.AppendLine('  (中栏没有出现文件名)')
} else {
    foreach ($h in $hit) { [void]$sb.AppendLine(('  y={0,4} x={1,4} | {2}' -f $h.Y, $h.X, $h.T)) }
}
[void]$sb.AppendLine('')
[void]$sb.AppendLine('附件痕迹条数: ' + $hit.Count)

# 错误提示（onError 会追加 sysLine('错误', ...)）
$err = @()
foreach ($m in $mid) { if ($m.T -match '^错误|错误：|失败') { $err += $m.T } }
[void]$sb.AppendLine('错误提示条数: ' + $err.Count)
foreach ($e in $err) { [void]$sb.AppendLine('  ! ' + $e) }

Save $sb.ToString()
"""


def main() -> int:
    pid = find_pid()
    out, code = run_ps(BODY, "attach_state")
    print(out)
    if code != 0:
        print("[警告] PowerShell 退出码", code)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
