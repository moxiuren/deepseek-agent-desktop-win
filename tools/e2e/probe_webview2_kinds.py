#!/usr/bin/env python3
"""在真实 WebView2 里测出每种 kind 的**真实占位高度**，供 `INTRINSIC` 定系数。

## 为什么要一个专门的测量脚本

`app.js` 的 `INTRINSIC` 表只覆盖 4 种 kind，其余 8 种走 `app.css` 的
`auto 110px` 兜底。110px 对 `turn`（真值 18px）高估 6.1 倍、对 `sys`
（真值 38px）高估 2.9 倍 —— 一段含大量状态行与分隔线的对话，滚动条
会比真实内容长好几倍。

而现有 4 个系数里有 3 个也不准（`Thought` 32 vs 真值 18、`Info` 54 vs 38）。

## 测量纪律（踩过的坑，全部写进代码）

1. **每次只注入一块**，注入前 `clear` 并等两帧。
   第一版把 10 种 kind × 6 种长度一次性灌进去，结果 `Info`/`Thinking`
   量到 72/128 —— 因为上一轮实验残留的节点 seq 落在同一区间，`byseq`
   索引撞车，量到的是别的块。**一个实验一次 clear，seq 全局唯一递增。**
2. **`offsetHeight` 会强制布局**，对 `content-visibility:auto` 的屏外
   元素也一样 —— 这正是我们要的真值。但要注意：块必须在 DOM 里且
   `display` 不为 none。
3. **文本长度要覆盖换行临界点**。`.sys` 单行 38px、三行 158px，中间
   有 2 行档。只测 0 和 300 会漏掉中间态。

用法：
    python tools/e2e/probe_webview2_kinds.py
"""
from __future__ import annotations

import json
import statistics
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from playwright.sync_api import sync_playwright  # noqa: E402

#: 注入一块、量一块。`seq` 由 Python 侧全局递增，杜绝撞车。
JS_ONE = """
async ([seq, kind, text]) => {
  const B = window.Bridge;
  B.emit({ t: 'clear' });
  await new Promise(r => requestAnimationFrame(r));
  await new Promise(r => requestAnimationFrame(r));
  B.emit({ t: 'item', item: {
    seq, kind, text, detail: '', tool: null, thought: null,
  }});
  await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
  const inner = document.getElementById('inner');
  const el = inner.children[0];
  if (!el) return { error: '没有渲染出节点', n: inner.children.length };
  return {
    real: el.offsetHeight,
    cls: el.className,
    inline: el.style.containIntrinsicSize || '',
    // textContent 不受 content-visibility 影响（innerText 会，因为要布局）
    textLen: (el.textContent || '').length,
    n: inner.children.length,
  };
}
"""

#: 所有 `renderItem` 会产出的 kind（见 app.js 的 switch）。
KINDS = [
    "Goal", "Assistant", "Thought", "ToolCall", "TurnDivider",
    "Thinking", "Gate", "Breaker", "Error", "Notice", "Summary", "Info",
]

#: 覆盖换行临界点的长度档。中文在 659px 宽、12.5px 字号下
#: 每行约 45-50 字，所以 0/40/100/300 能覆盖 1~7 行。
LENGTHS = [0, 40, 100, 300]


def main() -> int:
    try:
        d = json.load(urllib.request.urlopen("http://127.0.0.1:9333/json/list"))
        pages = [t for t in d if t.get("type") == "page"]
        if not pages:
            raise SystemExit("[错误] 没有可接管页面")
        url = pages[0]["url"]
    except Exception as e:
        raise SystemExit(f"[错误] CDP 不可达：{e}\n"
                         f"启动 Web 壳前设 WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS="
                         f"--remote-debugging-port=9333")

    print(f"接管: {url}")

    seq = 900000
    table: dict[str, list[tuple[int, int, str]]] = {}

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp("http://127.0.0.1:9333")
        page = browser.contexts[0].pages[0]
        vp = page.evaluate("() => ({w: innerWidth, h: innerHeight})")
        width = page.evaluate(
            "() => document.getElementById('inner').clientWidth")
        print(f"视口 {vp['w']}x{vp['h']} / 内容宽 {width}px\n")

        for kind in KINDS:
            table[kind] = []
            for L in LENGTHS:
                seq += 1
                r = page.evaluate(JS_ONE, [seq, kind, "字" * L])
                if r.get("error"):
                    print(f"[警告] {kind} L={L}: {r['error']}")
                    continue
                table[kind].append((L, r["real"], r["inline"]))

        browser.close()

    # ---- 输出表 ----
    hdr = f"{'kind':<12}" + "".join(f"{('L=' + str(L)):>8}" for L in LENGTHS)
    hdr += f"{'斜率':>9}{'现用':>10}"
    print(hdr)
    print("-" * len(hdr))

    CUR = {"Goal": "95.9+0.4444n", "Assistant": "-25.4+0.7163n",
           "Thought": "32", "Info": "54"}
    slopes: dict[str, float] = {}

    for kind in KINDS:
        rows = table[kind]
        cells = ""
        for L in LENGTHS:
            v = next((h for (ll, h, _) in rows if ll == L), None)
            cells += f"{v if v is not None else '?':>8}"
        # 线性斜率（最小二乘）
        pts = [(L, h) for (L, h, _) in rows if h > 0]
        if len(pts) >= 2:
            n = len(pts)
            sx = sum(x for x, _ in pts)
            sy = sum(y for _, y in pts)
            sxx = sum(x * x for x, _ in pts)
            sxy = sum(x * y for x, y in pts)
            den = n * sxx - sx * sx
            slope = (n * sxy - sx * sy) / den if den else 0.0
        else:
            slope = 0.0
        slopes[kind] = slope
        print(f"{kind:<12}{cells}{slope:>9.4f}{CUR.get(kind, '(缺)'):>10}")

    # ---- 给出建议系数 ----
    print("\n建议系数（h = a + b * chars，a 取 L=0 实测）：")
    print(f"{'kind':<12}{'a':>8}{'b':>10}{'说明':<20}")
    for kind in KINDS:
        rows = table[kind]
        a = next((h for (L, h, _) in rows if L == 0), None)
        b = slopes.get(kind, 0.0)
        if a is None:
            continue
        note = ""
        if abs(b) < 0.001:
            note = "固定高度（与字数无关）"
        else:
            note = f"每字 {b:.4f} px"
        print(f"{kind:<12}{a:>8}{b:>10.4f}  {note}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
