#!/usr/bin/env python3
"""对照实验：真实鼠标点击「深度思考」开关，验证点击链路本身是否通。

## 为什么需要这个对照

`click_attach_real.py` 显示：真实鼠标点击「附件」后没有弹出对话框。
但「没弹」有两种解释，必须先排除第二种：

  · 附件功能坏了
  · 点击根本没打到窗口（坐标偏移、窗口被遮挡、DPI 缩放）

「深度思考」开关是最干净的对照组：
  · 纯前端状态切换（`send({t:'setChat', thinking: ...})`）
  · 可逆，点两次回到原状态
  · 零网络请求，不会污染账号
  · 有明确的可见反馈：按钮 `.on` 类 + 前端会回推状态

判据：点一下后，`chipThinking` 的 ToggleState 或界面上可见的
状态文本发生变化。变了 → 点击链路通，问题在附件；没变 → 点击本身没生效。

用法：
    python tools/e2e/diag_click_control.py
"""
from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from ctypes import wintypes

PROC_NAME = "DeepSeekAgent.Web"

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.SetCursorPos.restype = wintypes.BOOL
user32.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                               wintypes.DWORD, ctypes.c_void_p]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetCursorPos.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
SW_RESTORE = 9

PS = r"""
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$root = [System.Windows.Automation.AutomationElement]::RootElement
$c = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ProcessIdProperty, __PID__)
$w = $root.FindFirst([System.Windows.Automation.TreeScope]::Children, $c)
if (-not $w) { 'NOWIN'; exit }
$null = $w.FindAll([System.Windows.Automation.TreeScope]::Descendants,
                   [System.Windows.Automation.Condition]::TrueCondition)
Start-Sleep -Milliseconds 300

function State($aid) {
    $e = $w.FindFirst([System.Windows.Automation.TreeScope]::Descendants,
        (New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::AutomationIdProperty, $aid)))
    if (-not $e) { return "$aid=NOTFOUND" }
    $r = $e.Current.BoundingRectangle
    $tp = $null
    $ts = ''
    if ($e.TryGetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern, [ref]$tp)) {
        $ts = $tp.Current.ToggleState.ToString()
    }
    return ("{0}|x={1}|y={2}|w={3}|h={4}|toggle={5}|name={6}" -f
        $aid, [int]$r.X, [int]$r.Y, [int]$r.Width, [int]$r.Height, $ts, $e.Current.Name)
}

State 'chipThinking'
State 'btnAttach'
$w.Current.NativeWindowHandle
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


def ps(script: str, pid: int) -> list[str]:
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script.replace("__PID__", str(pid))],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    return [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]


def cursor() -> tuple[int, int]:
    p = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def click_at(x: int, y: int) -> None:
    user32.SetCursorPos(x, y)
    time.sleep(0.25)
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, None)
    time.sleep(0.07)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, None)


def parse_state(line: str) -> dict[str, str]:
    parts = line.split("|")
    d = {"id": parts[0]}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            d[k] = v
    return d


def main() -> int:
    pid = find_pid()
    print("进程 PID:", pid)

    before = ps(PS, pid)
    if not before or "NOWIN" in before[0]:
        print("[错误] 找不到窗口")
        return 1
    hwnd = int(before[-1]) if before[-1].isdigit() else 0
    chip = parse_state(before[0])
    print("点击前 chipThinking:", before[0])

    old = cursor()
    if hwnd:
        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.4)

    cx = int(chip["x"]) + int(chip["w"]) // 2
    cy = int(chip["y"]) + int(chip["h"]) // 2
    print(f"\n--- 点击 chipThinking 中心 ({cx},{cy}) ---")
    click_at(cx, cy)
    time.sleep(1.2)

    after = ps(PS, pid)
    print("点击后 chipThinking:", after[0])

    a = parse_state(after[0])
    changed = (chip.get("toggle") != a.get("toggle")) or (chip.get("name") != a.get("name"))
    print("\n状态变化:", changed)

    # 再点一次恢复原状
    print("\n--- 再点一次恢复 ---")
    click_at(cx, cy)
    time.sleep(1.2)
    restored = ps(PS, pid)
    print("恢复后 chipThinking:", restored[0])

    user32.SetCursorPos(*old)
    print("\n光标已归位")

    if changed:
        print("\n[结论] 真实点击链路正常 —— 问题定位在「附件」功能本身")
        return 0
    print("\n[结论] 真实点击没有产生任何状态变化 —— 点击链路本身有问题")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
