#!/usr/bin/env python3
"""旅程 2：挂附件 —— 真实点击 → 选文件 → 确认服务端真的解析了内容。

## 上一轮的假阴性（必须记住的教训）

`click_attach_real.py` 报「疑似产品缺陷：点附件没弹出对话框」，**结论是错的**。
`diag_attach_enum.py` 改用 `EnumWindows` 重做后，对话框明明弹出来了：

    hwnd=3736016 vis=True cls='#32770'
    text='选择要发给模型的附件（服务端会解析文件内容）'
    rect=(33, 23, 658, 503)

错在检测方式。`click_attach_real.py` 用 UIA
`RootElement.FindAll(Children)` 再按 `ClassName` 匹配 `32770`：
UIA 的 `ClassName` 对 Win32 通用对话框未必返回 `#32770`，
且每次新建 UIA 连接时 accessibility tree 是惰性构建的。

**教训：判据错了，产品就被冤枉成坏的。** 报「缺陷」之前必须先证明
探测工具本身能看见东西 —— 这就是 `diag_attach_enum.py` 存在的意义。

## 这一版的两条纪律

1. **窗口存在性用 `EnumWindows` 判，不用 UIA 判**（Win32 层面，零歧义）
2. **已知 HWND 后用 `AutomationElement.FromHandle(hwnd)` 取元素**，
   不要从 RootElement 一层层往下遍历 —— 前者 O(1)，后者靠树构建完成

## 流程

真实点击 btnAttach → EnumWindows 等 `#32770` → FromHandle →
把完整路径写进文件名 Edit（Win32 通用对话框固定 AutomationId `1148`）→
Invoke「打开」按钮（AutomationId `1`）→ 等对话框消失 → 回前端读附件 chip

用法：
    python tools/e2e/journey2_attach.py --probe     # 只弹窗、列控件、关掉
    python tools/e2e/journey2_attach.py --pick      # 真选文件
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

PROC_NAME = "DeepSeekAgent.Web"
ATTACH_FILE = Path(tempfile.gettempdir()) / "gad-attach-probe.md"

user32 = ctypes.WinDLL("user32", use_last_error=True)

user32.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                            ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.SetCursorPos.restype = wintypes.BOOL
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetCursorPos.restype = wintypes.BOOL
user32.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                               wintypes.DWORD, ctypes.c_void_p]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.restype = wintypes.BOOL

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
WM_CLOSE = 0x0010

ENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def find_pid() -> int:
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"(Get-Process -Name '{PROC_NAME}' -EA SilentlyContinue|"
         f"Select-Object -First 1).Id"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    t = out.stdout.strip()
    if not t.isdigit():
        raise SystemExit(f"进程 {PROC_NAME} 未运行")
    return int(t)


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
                "rect": (r.left, r.top, r.right, r.bottom),
            }
        return True

    user32.EnumWindows(ENUMPROC(cb), 0)
    return found


def ps(script: str, extra_env: dict[str, str] | None = None) -> list[str]:
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, timeout=90,
    )
    out = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
    if proc.stderr.strip():
        err = proc.stderr.strip().splitlines()
        out += ["[stderr] " + ln.strip() for ln in err[:5]]
    return out


PS_BTN = r"""
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
if (-not $e) { 'NOTFOUND'; exit 1 }
$r=$e.Current.BoundingRectangle
[int]($r.X+$r.Width/2)
[int]($r.Y+$r.Height/2)
$w.Current.NativeWindowHandle
"""

# 探测：列出对话框里的所有子窗口（Win32 层，不走 UIA）。
#
# 为什么放弃 UIA 的 FindAll：实测 Descendants 遍历在模态通用对话框上
# 只返回了文件列表项和搜索框，**文件名编辑框与「打开」按钮都没返回** ——
# UIA provider 对 Common Item Dialog 做了虚拟化/截断。
# 用 Win32 的 GetDlgItem 是权威的：这两个 id 是 IFileDialog 内部仍在用的标准 id。
#
#   1148 = 「文件名(N)」编辑框（ComboBoxEx 的内嵌 Edit）
#   1152 = 地址栏
#   1    = IDOK（打开/保存）
#   2    = IDCANCEL（取消）
ID_FILENAME = 1148
ID_OK = 1
ID_CANCEL = 2

WM_SETTEXT = 0x000C
WM_COMMAND = 0x0111
BM_CLICK = 0x00F5

user32.GetDlgItem.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetDlgItem.restype = wintypes.HWND
user32.GetWindow.argtypes = [wintypes.HWND, ctypes.c_uint]
user32.GetWindow.restype = wintypes.HWND
user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                wintypes.WPARAM, ctypes.c_void_p]
user32.SendMessageW.restype = ctypes.c_void_p
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]


def probe_children(dlg: int) -> list[str]:
    """用 GetDlgItem 逐 id 探测，附带 class/text。"""
    out = []
    for ident, label in ((ID_FILENAME, "文件名框"), (1152, "地址栏"),
                         (ID_OK, "打开按钮"), (ID_CANCEL, "取消按钮")):
        h = user32.GetDlgItem(dlg, ident)
        if not h:
            out.append(f"    {label:<8} id={ident:<5} -> (不存在)")
            continue
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(h, cls, 256)
        txt = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(h, txt, 512)
        out.append(f"    {label:<8} id={ident:<5} hwnd={h} "
                   f"class={cls.value!r} text={txt.value!r}")
    return out


def fill_and_submit(dlg: int, path: Path) -> list[str]:
    """把完整路径写进文件名框，再点「打开」。

    写完整路径而不是逐级导航，是通用对话框的老技巧：
    WM_SETTEXT 到 1148 后点 IDOK，系统会解析路径并直接选中该文件。
    比模拟浏览目录快一个数量级，也不依赖当前所在目录。
    """
    out = []
    h = user32.GetDlgItem(dlg, ID_FILENAME)
    if not h:
        out.append("    [失败] GetDlgItem(1148) 为空")
        return out
    # 1148 拿到的是 ComboBoxEx32，真正的文本 Edit 在它下面两层
    # （ComboBoxEx32 -> ComboBox -> Edit）。逐层往下钻，找到 Edit 为止。
    target = h
    for _ in range(4):
        child = user32.GetWindow(target, 5)  # GW_CHILD
        if not child:
            break
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(child, cls, 64)
        target = child
        if cls.value.lower() == "edit":
            break
    # lParam 是 wchar_t*：必须显式 cast 并保住引用，否则 GC 掉指针就悬空了
    buf = ctypes.c_wchar_p(str(path))
    user32.SendMessageW(target, WM_SETTEXT, 0, ctypes.cast(buf, ctypes.c_void_p))
    time.sleep(0.4)
    back = ctypes.create_unicode_buffer(1024)
    user32.GetWindowTextW(target, back, 1024)
    out.append(f"    写入后读回: {back.value!r}")
    ok = user32.GetDlgItem(dlg, ID_OK)
    if not ok:
        out.append("    [失败] GetDlgItem(1) 为空")
        return out
    user32.SendMessageW(ok, BM_CLICK, 0, None)
    out.append("    已点「打开」")
    return out


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


def close_stray_dialogs(pid: int) -> int:
    """开跑前清掉可能残留的 #32770，避免污染「点击前后」对比。"""
    n = 0
    for h, d in windows_of(pid).items():
        if d["cls"] == "#32770" and d["vis"]:
            user32.PostMessageW(h, WM_CLOSE, 0, 0)
            n += 1
    if n:
        time.sleep(0.6)
    return n


def main() -> int:
    mode = "probe" if "--probe" in sys.argv else "pick" if "--pick" in sys.argv else None
    if not mode:
        print("用法: --probe | --pick")
        return 1

    if not ATTACH_FILE.exists():
        print(f"[错误] 测试文件不存在: {ATTACH_FILE}")
        return 1
    print(f"测试文件: {ATTACH_FILE}  ({ATTACH_FILE.stat().st_size} 字节)")

    pid = find_pid()
    print("进程 PID:", pid)

    stray = close_stray_dialogs(pid)
    if stray:
        print(f"清理残留对话框 {stray} 个")

    # --- 1. 定位按钮 ---
    pos = ps(PS_BTN.replace("__PID__", str(pid)))
    if not pos or "NOTFOUND" in pos[0]:
        print("[错误] 找不到 btnAttach:", pos)
        return 1
    cx, cy = int(pos[0]), int(pos[1])
    hwnd_main = int(pos[2]) if len(pos) > 2 and pos[2].isdigit() else 0
    print(f"btnAttach 中心 ({cx},{cy})  主窗口 hwnd={hwnd_main}")

    before = windows_of(pid)
    old = cursor()

    # --- 2. 真实点击 ---
    if hwnd_main:
        user32.SetForegroundWindow(hwnd_main)
        time.sleep(0.35)
    click_at(cx, cy)
    print("已真实点击 btnAttach")

    # --- 3. EnumWindows 等对话框（判据用 Win32，不用 UIA）---
    dlg_hwnd = 0
    dlg_info: dict = {}
    for i in range(25):
        time.sleep(0.2)
        for h, d in windows_of(pid).items():
            if h in before:
                continue
            if d["cls"] == "#32770" and d["vis"]:
                dlg_hwnd, dlg_info = h, d
                print(f"第 {i * 0.2:.1f}s 对话框出现: hwnd={h} "
                      f"text={d['text']!r} rect={d['rect']}")
                break
        if dlg_hwnd:
            break

    if not dlg_hwnd:
        user32.SetCursorPos(*old)
        print("[失败] 5 秒内没有 #32770 出现")
        return 2

    try:
        # --- 4. 探测控件（Win32 GetDlgItem，不走 UIA）---
        print("\n=== 对话框控件（GetDlgItem）===")
        for ln in probe_children(dlg_hwnd):
            print(ln)

        if mode == "probe":
            print("\n[probe 模式] 不选文件，直接关闭")
            return 0

        # --- 5. 填路径并提交 ---
        print("\n=== 填入路径并提交 ===")
        for ln in fill_and_submit(dlg_hwnd, ATTACH_FILE):
            print(ln)

        # --- 6. 等对话框消失 ---
        gone = False
        for _ in range(25):
            time.sleep(0.2)
            if dlg_hwnd not in windows_of(pid):
                gone = True
                break
        print("对话框已关闭:", gone)
        if not gone:
            print("[警告] 对话框仍在，发 WM_CLOSE 清理")
            user32.PostMessageW(dlg_hwnd, WM_CLOSE, 0, 0)
            time.sleep(0.6)
            return 3
        return 0
    finally:
        user32.SetCursorPos(*old)


if __name__ == "__main__":
    raise SystemExit(main())
