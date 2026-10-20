#!/usr/bin/env python3
"""旅程 4：接管一条「未接管」的网页版会话，并验证接管后能继续对话。

## 为什么这条旅程必须走真实鼠标，而不能用 UIA Invoke

Web 壳的会话列表项在 DOM 里是 `<div class="chat">`：

    const row = el('div', 'chat' + (c.active ? ' on' : ''));
    row.addEventListener('click', () => send({ t: 'attachSession', id: c.id }));

**没有 role、没有 tabindex、没有 data-id**。后果在 UIA 侧非常具体：

  · 51 个会话项全部只以 `ControlType.Text` 出现（只是两段文字，
    不是可点击控件）
  · 没有 InvokePattern，`Invoke()` 无从调用
  · 没有 AutomationId，`Find('...')` 无从定位

所以只能按几何位置（x ≈ 59 落在左栏）找文字，再用真实鼠标点它。
这本身也是一条**可访问性缺陷**的证据：屏幕阅读器读不出「这是可点
的会话项」，键盘用户无法 Tab 到它。WPF 壳用原生控件没有这个问题。

## 为什么必须先滚动

列表项的 y 坐标一直排到 3064，而窗口高度只有 715。UIA 会照实报告
屏幕外元素的内容坐标，所以「能找到」不等于「能点到」——

    点屏幕外的坐标 = 点空。

`.chatlist{flex:1;overflow-y:auto}` 是 DOM 内部滚动容器，Chromium
不向 UIA 暴露它的 ScrollPattern（探测结果：只有 RootWebArea 有
ScrollPattern 且 VerticallyScrollable=False）。所以用鼠标滚轮：
把光标悬在左栏上，发 `MOUSEEVENTF_WHEEL`，每滚一格重读一次坐标，
直到目标行的 y 落进可见区。

## 判据分两层

  · **引擎层**（结构化，权威）：日志新增行里出现
    `"attached": true` 且带 `"remote_count"`（复用路径没有这个字段）
  · **界面层**（用户可见）：列表项副标题从「未接管」变成「已接管」，
    中栏出现历史消息

两层都过才算通过 —— 只过引擎层说明「后端接上了但界面没反映」，
那同样是缺陷。

用法：
    python tools/e2e/journey4_attach_web.py --probe    # 只滚动定位，不点击
    python tools/e2e/journey4_attach_web.py            # 滚动 + 点击接管
"""
from __future__ import annotations

import ctypes
import os
import sys
import time
from ctypes import wintypes
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from web_common import find_pid, log_offset, read_new_events, run_ps  # noqa: E402

user32 = ctypes.WinDLL("user32", use_last_error=True)

user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.SetCursorPos.restype = wintypes.BOOL
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetCursorPos.restype = wintypes.BOOL
user32.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                               wintypes.DWORD, ctypes.c_void_p]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_WHEEL = 0x0800
WHEEL_DELTA = 120


# ---------------------------------------------------------------- UIA 探测

# 读左栏全部文本，标出每一项「是不是未接管」。输出格式刻意做成
# 定宽可解析（y 坐标在前），Python 侧用正则拆。
#
# 为什么把窗口 rect 也带回来：UIA 报告的是内容坐标，判断「可见」
# 必须拿窗口边界去比，而不是拍一个固定的 y 上限。
BODY_SCAN = r"""
$sb = New-Object System.Text.StringBuilder
$wb = $w.Current.BoundingRectangle
[void]$sb.AppendLine(('WIN|{0}|{1}|{2}|{3}' -f [int]$wb.X, [int]$wb.Y, [int]$wb.Width, [int]$wb.Height))
[void]$sb.AppendLine(('HWND|{0}' -f $w.Current.NativeWindowHandle))

$all = $w.FindAll([System.Windows.Automation.TreeScope]::Descendants,
                  [System.Windows.Automation.Condition]::TrueCondition)
$rows = @()
foreach ($e in $all) {
    try {
        if ($e.Current.ControlType.ProgrammaticName -ne 'ControlType.Text') { continue }
        $n = $e.Current.Name
        if (-not $n) { continue }
        $r = $e.Current.BoundingRectangle
        if ($r.Width -le 0 -or $r.X -ge 200) { continue }
        $rows += [pscustomobject]@{ Y=[int]$r.Y; X=[int]$r.X; W=[int]$r.Width; T=$n }
    } catch { }
}
foreach ($m in ($rows | Sort-Object Y, X)) {
    $t = $m.T -replace "`r", ' ' -replace "`n", ' '
    [void]$sb.AppendLine(('ROW|{0}|{1}|{2}|{3}' -f $m.Y, $m.X, $m.W, $t))
}
Save $sb.ToString()
"""

# 中栏消息区（几何法，同 MidTexts），用于确认历史被拉回来了。
BODY_MID = r"""
$sb = New-Object System.Text.StringBuilder
$all = $w.FindAll([System.Windows.Automation.TreeScope]::Descendants,
                  [System.Windows.Automation.Condition]::TrueCondition)
$rows = @()
foreach ($e in $all) {
    try {
        if ($e.Current.ControlType.ProgrammaticName -ne 'ControlType.Text') { continue }
        $n = $e.Current.Name
        if (-not $n) { continue }
        $r = $e.Current.BoundingRectangle
        if ($r.Width -le 0 -or $r.X -le 200) { continue }
        $rows += [pscustomobject]@{ Y=[int]$r.Y; X=[int]$r.X; T=$n }
    } catch { }
}
foreach ($m in ($rows | Sort-Object Y, X)) {
    $t = $m.T -replace "`r", ' ' -replace "`n", ' '
    if ($t.Length -gt 160) { $t = $t.Substring(0,160) + '...' }
    [void]$sb.AppendLine(('MID|{0}|{1}|{2}' -f $m.Y, $m.X, $t))
}
Save $sb.ToString()
"""

# 把光标停到左栏中心，然后滚轮。放这里是因为 PowerShell 做不了
# 真实鼠标事件（Add-Type -Language CSharp 在本机被沙箱拒绝）。
def wheel(x: int, y: int, notches: int) -> None:
    user32.SetCursorPos(x, y)
    time.sleep(0.12)
    for _ in range(abs(notches)):
        # 向下滚 = 内容上移 = delta 取负
        d = -WHEEL_DELTA if notches > 0 else WHEEL_DELTA
        user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, d & 0xFFFFFFFF, None)
        time.sleep(0.06)


def click(x: int, y: int) -> None:
    user32.SetCursorPos(x, y)
    time.sleep(0.2)
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, None)
    time.sleep(0.07)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, None)


def parse_rows(text: str) -> tuple[dict, list[dict]]:
    """把 BODY_SCAN 的输出拆成 (窗口信息, 行列表)。"""
    win: dict = {}
    rows: list[dict] = []
    for ln in text.splitlines():
        if ln.startswith("WIN|"):
            _, x, y, w, h = ln.split("|", 4)
            win = {"x": int(x), "y": int(y), "w": int(w), "h": int(h)}
        elif ln.startswith("HWND|"):
            win["hwnd"] = int(ln.split("|", 1)[1])
        elif ln.startswith("ROW|"):
            _, y, x, w, t = ln.split("|", 4)
            rows.append({"y": int(y), "x": int(x), "w": int(w), "t": t})
    return win, rows


def first_unbound(rows: list[dict]) -> dict | None:
    """找第一条「未接管」的会话。

    列表项是成对出现的两行：

        y= 836 | 列出目录并读首行        ← 标题
        y= 857 | 未接管 · 10-18 14:59    ← 副标题

    所以先定位副标题，再往上找最近的标题行（y 更小且相邻）。
    """
    for i, r in enumerate(rows):
        if r["t"].startswith("未接管"):
            for j in range(i - 1, -1, -1):
                prev = rows[j]
                if r["y"] - prev["y"] > 40:
                    break
                if not prev["t"].startswith(("已接管", "未接管")):
                    return {"title": prev["t"], "title_y": prev["y"],
                            "state_y": r["y"], "x": prev["x"], "w": prev["w"]}
    return None


def visible(win: dict, y: int) -> bool:
    """内容坐标 y 是否落在窗口可见区（留 30px 余量避开标题栏/底边）。"""
    top = win["y"] + 40
    bottom = win["y"] + win["h"] - 30
    return top <= y <= bottom


def step(title: str) -> None:
    print()
    print("=" * 64)
    print(title)
    print("=" * 64)


def main() -> int:
    probe_only = "--probe" in sys.argv

    pid = find_pid()
    print("进程 PID:", pid, "（Web 壳）")

    step("1/4 扫描左栏，定位第一条未接管的会话")
    out, _ = run_ps(BODY_SCAN, "j4_scan")
    win, rows = parse_rows(out)
    if not win:
        print("[失败] 读不到窗口信息：")
        print(out[:1200])
        return 1
    print(f"窗口: x={win['x']} y={win['y']} w={win['w']} h={win['h']} "
          f"hwnd={win.get('hwnd')}")
    print(f"左栏文本行数: {len(rows)}")

    target = first_unbound(rows)
    if not target:
        print("[失败] 左栏里没有「未接管」的会话 —— 无法测试接管")
        print("  已接管条目数:", sum(1 for r in rows if r["t"].startswith("已接管")))
        return 2
    print(f"目标: {target['title']!r}")
    print(f"  标题 y={target['title_y']}  状态行 y={target['state_y']}  x={target['x']}")
    print(f"  当前可见: {visible(win, target['title_y'])}")

    # --- 滚动到可见区 ---
    if not visible(win, target["title_y"]):
        print("\n目标在视口外，开始滚轮定位…")
        if win.get("hwnd"):
            user32.SetForegroundWindow(win["hwnd"])
            time.sleep(0.35)
        wx = win["x"] + 100          # 左栏横向中心（窗口 x=33，左栏宽 ~200）
        wy = win["y"] + win["h"] // 2
        for attempt in range(1, 13):
            wheel(wx, wy, 2)         # 一次两格，比一格一格快且仍是「人手动」
            time.sleep(0.25)
            out, _ = run_ps(BODY_SCAN, "j4_scan2")
            win, rows = parse_rows(out)
            t2 = first_unbound(rows)
            if not t2:
                print(f"  第 {attempt} 次滚动后找不到目标（可能已滚过头）")
                continue
            target = t2
            print(f"  第 {attempt} 次滚动后: {target['title']!r} "
                  f"y={target['title_y']} 可见={visible(win, target['title_y'])}")
            if visible(win, target["title_y"]):
                break
        else:
            print("[失败] 12 次滚动内没能把目标带进可见区")
            return 3

    if probe_only:
        print("\n[probe 模式] 定位成功，不点击")
        return 0

    step("2/4 真实点击该会话（触发 attachSession）")
    mark = log_offset()
    cx = target["x"] + max(40, target["w"] // 2)
    cy = target["title_y"] + 8
    if win.get("hwnd"):
        user32.SetForegroundWindow(win["hwnd"])
        time.sleep(0.3)
    click(cx, cy)
    print(f"已点击 ({cx},{cy}) —— {target['title']!r}")

    step("3/4 等引擎日志出现 attach 结果")
    attached = None
    deadline = time.time() + 60
    import json as _json
    while time.time() < deadline:
        time.sleep(0.4)
        # attach 的返回是 RPC `result`，**没有 `event` 字段** ——
        # `read_new_events()` 专门筛 event，会把它跳过。所以这里扫原文。
        path, off = mark
        with path.open("rb") as f:
            f.seek(off)
            raw = f.read().decode("utf-8", errors="replace")
        for line in raw.splitlines():
            i = line.find('{"id"')
            if i < 0:
                continue
            try:
                d = _json.loads(line[i:])
            except Exception:
                continue
            r = d.get("result") if isinstance(d, dict) else None
            if isinstance(r, dict) and r.get("attached") is True:
                attached = r
                break
        if attached:
            break

    if not attached:
        print("[失败] 60 秒内日志里没有 attached=true 的 RPC 结果")
        print("  —— 可能点击没命中会话行，或引擎侧报错")
        return 4

    print("--- attach 结果（结构化）---")
    print(f"  attached       : {attached.get('attached')}")
    print(f"  reused         : {attached.get('reused')}")
    print(f"  remote_count   : {attached.get('remote_count')}")
    print(f"  anchor         : {attached.get('anchor')}")
    print(f"  system_prompt  : {attached.get('system_prompt_chars')} 字")
    print(f"  local id       : {attached.get('id')}")

    step("4/4 界面层验证：列表状态变了没有、中栏有没有历史")
    time.sleep(1.5)
    out, _ = run_ps(BODY_SCAN, "j4_after")
    win2, rows2 = parse_rows(out)
    same = [r for r in rows2 if r["t"] == target["title"]]
    print(f"同名标题出现 {len(same)} 次")
    for r in same:
        # 该标题下方最近的副标题
        nxt = [x for x in rows2 if x["y"] > r["y"] and x["y"] - r["y"] <= 40]
        state = nxt[0]["t"] if nxt else "(无副标题)"
        print(f"  y={r['y']} | {r['t']}  →  {state}")

    out_mid, _ = run_ps(BODY_MID, "j4_mid")
    mids = [ln for ln in out_mid.splitlines() if ln.startswith("MID|")]
    print(f"\n中栏文本节点: {len(mids)}")
    for ln in mids[:14]:
        print("  " + ln[4:])

    # --- 断言 ---
    print()
    print("--- 断言 ---")
    ok_engine = attached.get("attached") is True
    ok_new = attached.get("reused") is False
    ok_hist = (attached.get("remote_count") or 0) > 0
    ok_ui = len(mids) > 0
    print(f"  引擎接管成功              : {ok_engine}")
    print(f"  是新建接管（非复用）      : {ok_new}")
    print(f"  拉回历史条数 > 0          : {ok_hist}（{attached.get('remote_count')} 条）")
    print(f"  中栏有内容                : {ok_ui}（{len(mids)} 个文本节点）")
    passed = ok_engine and ok_new and ok_hist and ok_ui
    print(f"  判定                      : {'通过' if passed else '不通过'}")
    return 0 if passed else 5


if __name__ == "__main__":
    raise SystemExit(main())
