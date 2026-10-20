#!/usr/bin/env python3
"""诊断附件对话框：用 EnumWindows 逐帧观察点击前后的窗口集合。

## 上一版为什么不可信

`diag_attach.py` 用 PowerShell `Add-Type` 编译 C# 辅助类来枚举窗口，
但本机沙箱拒绝编译：

    Add-Type : A required privilege is not held by the client

于是 `[W]::List()` 每次都抛 TypeNotFound，控制台只剩「窗口数没有变化」
这句话 —— **它说的不是「没有新窗口」，是「枚举根本没跑」**。
这是一次典型的假阴性：工具坏了，结论却看起来像产品坏了。

## 这一版的做法

纯 Python `ctypes` 调 `user32.EnumWindows`，不需要编译任何东西。
每 200ms 采样一次该进程的全部顶层窗口（**含不可见窗口** ——
对话框在创建初期是隐藏的），记录集合变化。

关键：**默认不按 Esc**。上一版无条件发 Esc，可能在对话框刚建好、
还没被采样到的时候就被关掉了。这一版只在确认对话框存在后，
用 `WM_CLOSE` 定向关闭它。

用法：
    python tools/e2e/diag_attach_enum.py
"""
from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

PROC_NAME = "DeepSeekAgent.Web"

user32 = ctypes.WinDLL("user32", use_last_error=True)

user32.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                            ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.IsWindowEnabled.argtypes = [wintypes.HWND]
user32.IsWindowEnabled.restype = wintypes.BOOL
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.SetCursorPos.restype = wintypes.BOOL
user32.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                               wintypes.DWORD, ctypes.c_void_p]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetCursorPos.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.restype = wintypes.BOOL

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
WM_CLOSE = 0x0010

ENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def windows_of(pid: int) -> dict[int, dict]:
    found: dict[int, dict] = {}

    def cb(hwnd, _):
        p = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid:
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            txt = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, txt, 512)
            r = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            found[int(hwnd)] = {
                "cls": cls.value,
                "text": txt.value,
                "vis": bool(user32.IsWindowVisible(hwnd)),
                "en": bool(user32.IsWindowEnabled(hwnd)),
                "rect": (r.left, r.top, r.right, r.bottom),
            }
        return True

    user32.EnumWindows(ENUMPROC(cb), 0)
    return found


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


PS_POS = r"""
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$root=[System.Windows.Automation.AutomationElement]::RootElement
$c=New-Object System.Windows.Automation.PropertyCondition(
  [System.Windows.Automation.AutomationElement]::ProcessIdProperty, __PID__)
$w=$root.FindFirst([System.Windows.Automation.TreeScope]::Children,$c)
$null=$w.FindAll([System.Windows.Automation.TreeScope]::Descendants,
  [System.Windows.Automation.Condition]::TrueCondition)
Start-Sleep -Milliseconds 300
$e=$w.FindFirst([System.Windows.Automation.TreeScope]::Descendants,
  (New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::AutomationIdProperty,'btnAttach')))
if (-not $e) { 'NOTFOUND'; exit }
$r=$e.Current.BoundingRectangle
[int]($r.X+$r.Width/2)
[int]($r.Y+$r.Height/2)
$w.Current.NativeWindowHandle
"""


def fmt(ws: dict[int, dict]) -> str:
    lines = []
    for h, d in ws.items():
        lines.append(f"    hwnd={h} vis={d['vis']} en={d['en']} "
                     f"cls={d['cls']!r} text={d['text']!r} rect={d['rect']}")
    return "\n".join(lines) if lines else "    (无)"


def main() -> int:
    pid = find_pid()
    print("PID:", pid)

    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command", PS_POS.replace("__PID__", str(pid))],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    if not lines or "NOTFOUND" in lines[0]:
        print("[错误] 找不到 btnAttach")
        return 1
    cx, cy = int(lines[0]), int(lines[1])
    hwnd_main = int(lines[2]) if len(lines) > 2 else 0
    print(f"btnAttach 中心: ({cx},{cy})  主窗口 hwnd={hwnd_main}")

    before = windows_of(pid)
    print(f"\n=== 点击前（{len(before)} 个窗口）===")
    print(fmt(before))

    p = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    old = (p.x, p.y)

    if hwnd_main:
        user32.SetForegroundWindow(hwnd_main)
        time.sleep(0.35)
    user32.SetCursorPos(cx, cy)
    time.sleep(0.2)
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, None)
    time.sleep(0.07)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, None)
    print("\n已真实点击，开始逐帧采样…")

    appeared: dict[int, dict] = {}
    for i in range(30):  # 6 秒
        time.sleep(0.2)
        now = windows_of(pid)
        new = {h: d for h, d in now.items() if h not in before}
        if new:
            appeared = new
            print(f"\n=== 第 {i * 0.2:.1f}s 发现新窗口 ===")
            print(fmt(new))
            break
    else:
        print("\n=== 6 秒内没有新窗口出现 ===")

    # 只在确实有新窗口时才关它；没有就不动
    if appeared:
        for h, d in appeared.items():
            if d["vis"]:
                print(f"关闭 hwnd={h} (WM_CLOSE)")
                user32.PostMessageW(h, WM_CLOSE, 0, 0)
        time.sleep(0.6)
    else:
        print("（没有新窗口，未发送任何关闭指令）")

    user32.SetCursorPos(*old)
    print("光标已归位")

    if appeared:
        print("\n[结论] 点击确实弹出了窗口 —— 之前的 NODLG 是枚举工具失效造成的假阴性")
        return 0
    print("\n[结论] 点击后 6 秒内该进程没有创建任何新窗口")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
