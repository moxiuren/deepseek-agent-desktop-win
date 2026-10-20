#!/usr/bin/env python3
"""检查输入区坐标上到底是哪个窗口 —— 排除「被遮挡」这个解释。

## 为什么要查这个

对照实验的结论很不寻常：

  · 真实点击「最大化」按钮 → 窗口尺寸 1003→1067，**有效**
  · 真实点击「深度思考」开关 → 无任何变化
  · 真实点击「附件」按钮   → 没有弹出对话框

标题栏有效、输入区无效，最朴素的解释是**输入区被某个窗口盖住了**。
`WindowFromPoint` 直接回答这个问题：给定屏幕坐标，返回该点最上层的窗口句柄。
再对比目标窗口的句柄，就知道点击落在谁身上。

顺带用 `GetAncestor(GA_ROOT)` 把子窗口（WebView2 的 Chromium 渲染子 HWND）
归到顶层窗口，否则会误判成「点到了别的程序」。

用法：
    python tools/e2e/diag_window_from_point.py
"""
from __future__ import annotations

import ctypes
import subprocess
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)

user32.WindowFromPoint.argtypes = [wintypes.POINT]
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
user32.GetAncestor.restype = wintypes.HWND
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]

GA_ROOT = 2


def info(hwnd: int) -> str:
    if not hwnd:
        return "<null>"
    cls = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, cls, 256)
    txt = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, txt, 512)
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return (f"hwnd={hwnd} pid={pid.value} class={cls.value!r} "
            f"text={txt.value!r} rect=({r.left},{r.top},{r.right},{r.bottom})")


PS = r"""
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$root=[System.Windows.Automation.AutomationElement]::RootElement
$c=New-Object System.Windows.Automation.PropertyCondition(
  [System.Windows.Automation.AutomationElement]::ProcessIdProperty, __PID__)
$w=$root.FindFirst([System.Windows.Automation.TreeScope]::Children,$c)
$null=$w.FindAll([System.Windows.Automation.TreeScope]::Descendants,
  [System.Windows.Automation.Condition]::TrueCondition)
Start-Sleep -Milliseconds 300
foreach ($aid in @('btnAttach','chipThinking','input','winMax','btnRun')) {
    $e=$w.FindFirst([System.Windows.Automation.TreeScope]::Descendants,
      (New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::AutomationIdProperty,$aid)))
    if ($e) {
        $r=$e.Current.BoundingRectangle
        '{0}|{1}|{2}|{3}|{4}' -f $aid,[int]$r.X,[int]$r.Y,[int]$r.Width,[int]$r.Height
    } else { "$aid|NOTFOUND" }
}
'ROOT|' + $w.Current.NativeWindowHandle
"""


def find_pid() -> int:
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "(Get-Process -Name 'DeepSeekAgent.Web' -EA SilentlyContinue|"
         "Select-Object -First 1).Id"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    t = out.stdout.strip()
    if not t.isdigit():
        raise SystemExit("进程未运行")
    return int(t)


def main() -> int:
    pid = find_pid()
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command", PS.replace("__PID__", str(pid))],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    root_hwnd = 0
    targets: dict[str, tuple[int, int, int, int]] = {}
    for ln in lines:
        parts = ln.split("|")
        if parts[0] == "ROOT":
            root_hwnd = int(parts[1])
        elif len(parts) == 5 and parts[1] != "NOTFOUND":
            targets[parts[0]] = tuple(int(x) for x in parts[1:])

    print("目标窗口:", info(root_hwnd))
    print()
    user32.SetForegroundWindow(root_hwnd)
    time.sleep(0.4)

    for name, (x, y, w, h) in targets.items():
        cx, cy = x + w // 2, y + h // 2
        pt = wintypes.POINT(cx, cy)
        hw = user32.WindowFromPoint(pt)
        top = user32.GetAncestor(hw, GA_ROOT) if hw else 0
        mark = "OK " if top == root_hwnd else "!! "
        print(f"{mark}{name:<12} ({cx},{cy})")
        print(f"    WindowFromPoint -> {info(hw)}")
        print(f"    顶层祖先         -> {info(top)}")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
