#!/usr/bin/env python3
"""在**真实 WebView2**里铺满 600 块，量化 `INTRINSIC` 系数的偏差。

## 为什么要单独做这一件事

`probe_webview2_intrinsic.py` 只能量当前会话里已渲染的块 —— 真实会话
通常只有个位数条消息，样本太小，量不出系数是否随窗口宽度漂移。

而 `journey7_longconv_web.py` 虽然在 Chrome 里铺了 600 块，但它跑在
1003x715 视口、479px 内容宽度下；真实 WebView2 是 1192x703、659px 宽。
**每行能放多少字取决于宽度**，所以 px/字 系数必然不同。

这个脚本用 CDP 接管真实产品页，通过 `Bridge.emit()`（公开方法）注入
`hist` 消息，把真实的 724 条对话铺进产品自己的 DOM，然后逐个块对比
「我们声明的占位高度」与「浏览器实测高度」。

## 为什么不改产品代码来测

`Bridge.emit` 是 `bridge.js` 暴露的公开方法（`handlers` 数组分发），
C# 推下来的每条消息都经过它。从这里注入与宿主推送走**完全相同的代码路径**
—— `handle()` → `onItem()` → `renderItem()` → `intrinsicHeight()`。
所以量到的是真实行为，不是模拟。

用法：
    python tools/e2e/probe_webview2_bulk.py
    python tools/e2e/probe_webview2_bulk.py --port 9333 --n 600
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from playwright.sync_api import sync_playwright  # noqa: E402

from journey7_longconv_web import (  # noqa: E402
    CONV_DIR,
    build_rows,
    pick_file,
    rows_to_items,
)

#: 注入 hist 并等铺完。
#:
#: `hist` 是宿主推全量历史的入口（`WebBridge.SendHistory`）。这里一次性
#: 灌进去，而不是照 `ReplayConversation` 那样分批 —— 分批是宿主的性能
#: 策略，与本探针要量的「估算精度」无关，反而会引入时序噪声。
JS_INJECT = """
async ([items, maxTranscript]) => {
  const Bridge = window.Bridge;
  Bridge.emit({ t: 'clear' });
  await new Promise(r => requestAnimationFrame(r));

  const t0 = performance.now();
  const emitted = [];
  for (const it of items) {
    Bridge.emit({ t: 'item', item: it });
    emitted.push(it);
    // 宿主 `TrimTranscript()` 在超上限时逐条裁最旧的，并广播 rm。
    // 必须照做 —— 否则 DOM 会涨到 739 个，量到的宽度/高度就不是
    // 产品在真实上限下的样子。
    while (emitted.length > maxTranscript) {
      const old = emitted.shift();
      Bridge.emit({ t: 'rm', seq: old.seq });
    }
  }
  const emitMs = performance.now() - t0;

  // 等两帧让布局落定
  await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
  const inner = document.getElementById('inner');
  return {
    emitMs,
    layoutMs: performance.now() - t0,
    nodes: inner ? inner.children.length : -1,
    width: inner ? inner.clientWidth : -1,
    innerH: inner ? inner.offsetHeight : -1,
  };
}
"""

#: 逐块取声明值与实测值。
#:
#: 关键点：`getComputedStyle(el).containIntrinsicSize` 返回的是**声明值**
#: （我们 inline 设的那个），元素测量后 Chromium 不会改写它。所以这里读到
#: 的 est 就是 `intrinsicHeight()` 算出来的数。
JS_MEASURE = """
() => {
  const inner = document.getElementById('inner');
  const rows = [];
  for (const el of inner.children) {
    const decl = getComputedStyle(el).containIntrinsicSize || '';
    const m = decl.match(/([\\d.]+)px/);
    if (!m) continue;
    const est = parseFloat(m[1]);
    // offsetHeight 对 content-visibility:auto 的屏外元素**会强制布局**。
    // 这是本探针想要的：必须拿到真值才能比。
    const real = el.offsetHeight;
    if (real <= 0) continue;
    rows.push({
      cls: el.className.split(' ')[0],
      inline: el.style.containIntrinsicSize || '',
      est, real,
      chars: (el.innerText || '').length,
    });
  }
  return { rows, width: inner.clientWidth };
}
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9333)
    ap.add_argument("--n", type=int, default=600)
    ap.add_argument("--max-transcript", type=int, default=600)
    a = ap.parse_args()

    f = pick_file()
    conv = json.loads(f.read_text(encoding="utf-8"))
    rows = build_rows(conv)
    items = rows_to_items(rows)
    print(f"样本: {f.name}")
    print(f"  消息 {len(conv.get('messages') or [])} 条 / 重放行 {len(rows)} / "
          f"前端块 {len(items)}")
    print(f"  正文 {sum(len(m.get('text') or '') for m in conv.get('messages') or []):,} 字")

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{a.port}")
        page = browser.contexts[0].pages[0]
        vp = page.evaluate("() => ({w: innerWidth, h: innerHeight})")
        print(f"\n真实视口: {vp['w']} x {vp['h']}")

        inj = page.evaluate(JS_INJECT, [items[:a.n], a.max_transcript])
        print(f"注入: {inj['emitMs']:.1f} ms 落定 / {inj['layoutMs']:.1f} ms 含布局")
        print(f"  DOM 块 {inj['nodes']} / 内容宽度 {inj['width']} px / "
              f"总高 {inj['innerH']} px")

        m = page.evaluate(JS_MEASURE)
        print(f"  可测块 {len(m['rows'])} / 宽度 {m['width']} px")

        rows2 = m["rows"]
        groups: dict[str, list[float]] = {}
        for r in rows2:
            groups.setdefault(r["cls"], []).append(r["est"] / r["real"])

        print("\n估算 / 实测（1.00 为完美）：")
        for k in sorted(groups):
            v = groups[k]
            med = statistics.median(v)
            flag = "  <-- 低估" if med < 0.80 else ("  <-- 高估" if med > 1.30 else "")
            print(f"  {k:<10} n={len(v):<4} 中位 {med:.3f}  "
                  f"区间 {min(v):.2f}~{max(v):.2f}{flag}")

        est_sum = sum(r["est"] for r in rows2)
        real_sum = sum(r["real"] for r in rows2)
        dev = (est_sum - real_sum) / real_sum if real_sum else 0
        print(f"\n总高: 估算 {est_sum:,} px / 实测 {real_sum:,} px / 偏差 {dev*100:+.1f}%")

        # 曾在这里算过一个「按内容宽度缩放」的校正量（479/实际宽度），
        # 理由是系数来自 479px 宽下的拟合，视口变宽后每行字更多、高度更小。
        # **实测证明这个假设是错的，已删除**：同一份数据在 479px（Chrome）
        # 与 659px（真实 WebView2）下用同一组系数都是 +0.1% 偏差 —— 因为
        # 系数拟合自这份数据本身的换行行为，宽度效应已经包含在里面。
        # 强行按宽度缩放反而把偏差放大到 -27.2%。
        #
        # 保留这段说明是为了防止后来者重新加回这个「看起来合理」的校正。

        # 低估的块要单独列 —— 低估会让滚动中总高**单向膨胀**
        # （`contain-intrinsic-size:auto` 只校正不缩小），那是比高估
        # 更糟的体验：滚动条一路变长，视口内容被顶开。
        low = [r for r in rows2 if r["est"] < r["real"] * 0.8]
        print(f"\n估算 < 实测×0.8 的块: {len(low)}")
        for r in sorted(low, key=lambda x: x["est"] / x["real"])[:8]:
            print(f"  {r['cls']:<10} chars={r['chars']:<6} "
                  f"est={r['est']:<6} real={r['real']:<7} "
                  f"ratio={r['est'] / r['real']:.2f}")

        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
