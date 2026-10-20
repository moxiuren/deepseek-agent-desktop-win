#!/usr/bin/env python3
"""真实鼠标点击 Web 壳「附件」按钮，验证对话框能否弹出。

## 为什么需要这个工具

UIA 的 `InvokePattern.Invoke()` 对 Web 壳的 `btnAttach` **完全无效** ——
点击前后顶层窗口数不变、主窗口没有被模态阻塞，说明 Chromium 根本没把
这次 Invoke 当成用户手势派发给 DOM 的 click listener。

而 `PickFilesForUpload()` 里是 `OpenFileDialog.ShowDialog(this)`。
WebView2 在**没有用户手势**的情况下会拦掉这类同步阻塞调用。

所以要区分两件事：
  · 产品缺陷：真实用户点也弹不出来
  · 驱动缺陷：只有自动化点不出来，人手点正常

判据只能是真实鼠标事件。PowerShell 的 `Add-Type` 在本机被拒
（"A required privilege is not held by the client"），改走 Python `ctypes`
直接 P/Invoke `user32.dll` —— 不需要编译 C#。

用法：
    python tools/e2e/click_attach_real.py
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

PROC_NAME = "DeepSeekAgent.Web"

user32 = ctypes.WinDLL("user32", use_last_error=True)

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004

user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.SetCursorPos.restype = wintypes.BOOL
user32.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                               wintypes.DWORD, ctypes.c_void_p]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetCursorPos.restype = wintypes.BOOL

# 前台激活：不做这一步，点击会打到当前前台窗口而不是目标窗口 ——
# 目标窗口在后台时 SetCursorPos + mouse_event 点的是「屏幕上那个位置最上层的窗口」。
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL
user32.GetForegroundWindow.restype = wintypes.HWND

SW_RESTORE = 9


def get_cursor() -> tuple[int, int]:
    p = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def move(x: int, y: int) -> None:
    user32.SetCursorPos(x, y)


def click() -> None:
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, None)
    time.sleep(0.06)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, None)


PS_FIND = r"""
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$root = [System.Windows.Automation.AutomationElement]::RootElement
$c = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ProcessIdProperty, __PID__)
$w = $root.FindFirst([System.Windows.Automation.TreeScope]::Children, $c)
$null = $w.FindAll([System.Windows.Automation.TreeScope]::Descendants,
                   [System.Windows.Automation.Condition]::TrueCondition)
Start-Sleep -Milliseconds 250
$b = $w.FindFirst([System.Windows.Automation.TreeScope]::Descendants,
    (New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::AutomationIdProperty, 'btnAttach')))
if (-not $b) { 'NOTFOUND'; exit }
$r = $b.Current.BoundingRectangle
# 第一行坐标，第二行 hwnd（用于前台激活）
[int]($r.X + $r.Width/2), [int]($r.Y + $r.Height/2), $w.Current.NativeWindowHandle
"""

PS_WINDOWS = r"""
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$root = [System.Windows.Automation.AutomationElement]::RootElement
$kids = $root.FindAll([System.Windows.Automation.TreeScope]::Children,
                      [System.Windows.Automation.Condition]::TrueCondition)
$r = @()
foreach ($k in $kids) {
    try {
        if ($k.Current.ClassName -match '32770|#32770' -or $k.Current.Name -match '选择要发给模型的附件') {
            $r += ('DLG|' + $k.Current.ClassName + '|' + $k.Current.Name)
        }
    } catch { }
}
if ($r.Count -eq 0) { 'NODLG' } else { $r -join "`n" }
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


def ps(script: str, pid: int) -> str:
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script.replace("__PID__", str(pid))],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    return (proc.stdout or "").strip()


def main() -> int:
    pid = find_pid()
    print("进程 PID:", pid)

    pos = ps(PS_FIND, pid)
    if "NOTFOUND" in pos:
        print("[错误] 找不到 btnAttach")
        return 1
    lines = [ln.strip() for ln in pos.splitlines() if ln.strip()]
    cx, cy = int(lines[0]), int(lines[1])
    hwnd = int(lines[2]) if len(lines) > 2 else 0
    print("btnAttach 中心:", cx, cy, " hwnd:", hwnd)

    old = get_cursor()
    print("原光标:", old)

    # 前台激活。不做这一步，点击打到的是「那个屏幕坐标上最上层的窗口」，
    # 目标窗口在后台时就是打到别的窗口上 —— 上一版 NODLG 很可能就是这个原因。
    if hwnd:
        fg_before = user32.GetForegroundWindow()
        user32.ShowWindow(hwnd, SW_RESTORE)
        ok = user32.SetForegroundWindow(hwnd)
        time.sleep(0.4)
        print(f"前台激活: SetForegroundWindow={bool(ok)} "
              f"fg_before={fg_before} fg_after={user32.GetForegroundWindow()}")

    print("\n--- 真实鼠标点击 ---")
    move(cx, cy)
    time.sleep(0.25)
    click()
    time.sleep(2.0)

    res = ps(PS_WINDOWS, pid)
    print("对话框检测:", res)

    # 兜底：无论有没有对话框，按 Esc 关掉，并把光标放回原处
    keybd_event = user32.keybd_event
    keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD,
                            ctypes.c_void_p]
    VK_ESCAPE = 0x1B
    KEYEVENTF_KEYUP = 0x0002
    time.sleep(0.3)
    keybd_event(VK_ESCAPE, 0, 0, None)
    time.sleep(0.05)
    keybd_event(VK_ESCAPE, 0, KEYEVENTF_KEYUP, None)
    time.sleep(0.5)

    move(*old)
    print("已按 Esc，光标已归位")

    if "NODLG" in res:
        print("\n[结论] 真实鼠标点击也没弹出对话框 —— 疑似产品缺陷")
        return 2
    print("\n[结论] 真实鼠标点击弹出了对话框 —— UIA Invoke 是驱动方式的问题")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
