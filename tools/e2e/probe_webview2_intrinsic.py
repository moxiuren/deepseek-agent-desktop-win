#!/usr/bin/env python3
"""在**真实 WebView2 里**验证 `intrinsicHeight()` 的估算是否成立。

## 为什么需要这个探针

`journey7_longconv_web.py` 在 Playwright + Chrome 里量到了：
估算总高 +1.9%、锚点漂移 +0.0 px、批量插入保留 15.5 倍收益。
但那个脚本自己写明了边界 ——「真实滚动手感需在真窗口里人工确认」。

更硬的一条边界是**视口尺寸**：`INTRINSIC` 的系数（px / 字）来自
1003x715 视口、479px 内容宽度下的线性拟合。每行能放多少字取决于宽度，
窗口一变，px/字 就变。WebView2 窗口尺寸由 `MainWindow.xaml` 决定，
和 Chrome 测试窗口不一样 —— 所以系数可能只在测试视口下成立。

## 这个探针怎么绕进 WebView2

`MainWindow.xaml.cs` 用默认的 `CoreWebView2Environment.CreateAsync()`，
没有传 `AdditionalBrowserArguments`，所以环境变量
`WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS` 会生效。启动时设

    WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=9333

就能用 CDP 接管真实产品页（`https://app.local/index.html`）。

## 量什么

1. **内容宽度** —— 决定 px/字 的那个量
2. **估算 vs 实测** —— 对当前已有的每个块，取
   `getComputedStyle(el).containIntrinsicSize`（我们设的值）与 `el.offsetHeight`
   （真值），算比值分布
3. **CSS 兜底是否被覆盖** —— 若某个块的 inline style 缺失，它会退回
   `app.css` 的 `auto 110px`，那比值会是灾难性的 0.1x 量级
4. **滚动中总高是否单向膨胀** —— 滚到底再滚回来，看 `scrollHeight` 变化

用法：
    python tools/e2e/probe_webview2_intrinsic.py
    python tools/e2e/probe_webview2_intrinsic.py --port 9333
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


#: 逐个块取「估算值 / 实测值」。
#:
#: `containIntrinsicSize` 在元素已测量后会变成 `auto <px>` 里的 px 被真实
#: 尺寸替换 —— Chromium 的 `getComputedStyle` 返回的是**声明值**（我们设的），
#: 不是校正后的值，所以这里读到的是我们算的那个数。
#: 若 inline style 没设，读到的是 CSS 里 `auto 110px` 的 110。
JS_COLLECT = """
() => {
  const inner = document.getElementById('inner');
  if (!inner) return { error: '找不到 #inner' };
  const stream = document.getElementById('stream');
  const nodes = [...inner.children];
  const rows = [];
  for (const el of nodes) {
    const cs = getComputedStyle(el);
    const decl = cs.containIntrinsicSize || '';
    const m = decl.match(/([\\d.]+)px/);
    const est = m ? parseFloat(m[1]) : null;
    const real = el.offsetHeight;
    rows.push({
      cls: el.className,
      kind: (el.dataset && el.dataset.kind) || '',
      inline: el.style.containIntrinsicSize || '',
      est,
      real,
      chars: (el.innerText || '').length,
    });
  }
  const cs = inner ? getComputedStyle(inner) : null;
  return {
    rows,
    contentWidth: inner ? inner.clientWidth : null,
    viewport: { w: window.innerWidth, h: window.innerHeight },
    scrollHeight: stream ? stream.scrollHeight : null,
    scrollTop: stream ? stream.scrollTop : null,
  };
}
"""

#: 滚到底再滚回来，看总高是否单向膨胀。
#:
#: 「单向」是关键词：`contain-intrinsic-size:auto <n>px` 在元素测量后会
#: 用真值替换估算，所以总高**应该**在滚动中变化一次然后稳定。若滚回原处
#: 后总高显著大于出发时，说明估算值系统性偏低、且校正只增不减。
JS_SWEEP = """
async () => {
  const stream = document.getElementById('stream');
  if (!stream) return { error: '找不到 #stream' };
  const raf = () => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
  const home = stream.scrollTop;
  const h0 = stream.scrollHeight;
  stream.scrollTop = 0; await raf();
  const hTop = stream.scrollHeight;
  stream.scrollTop = stream.scrollHeight; await raf();
  const hBot = stream.scrollHeight;
  stream.scrollTop = home; await raf();
  const hBack = stream.scrollHeight;
  return { home, h0, hTop, hBot, hBack, growth: hBack - h0 };
}
"""


def cdp_ws(port: int) -> str:
    d = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list"))
    pages = [t for t in d if t.get("type") == "page"]
    if not pages:
        raise SystemExit("[错误] 没有可接管的页面目标")
    return pages[0]["webSocketDebuggerUrl"], pages[0]["url"]


def pct(v: float) -> str:
    return f"{v * 100:+.1f}%"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9333)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    ws, url = cdp_ws(a.port)
    print(f"接管目标: {url}")
    print(f"CDP: {ws}")

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{a.port}")
        ctx = browser.contexts[0]
        page = ctx.pages[0]
        print(f"页面标题: {page.title()}")

        data = page.evaluate(JS_COLLECT)
        if data.get("error"):
            print(f"[错误] {data['error']}")
            return 2

        rows = [r for r in data["rows"] if r["est"] and r["real"]]
        print()
        print(f"视口            : {data['viewport']['w']} x {data['viewport']['h']}")
        print(f"内容宽度        : {data['contentWidth']} px")
        print(f"块数            : {len(data['rows'])}（可比值 {len(rows)}）")

        if not rows:
            print("[警告] 当前没有可比的块 —— 先切进一个有内容的对话再跑。")
            return 3

        # 按 kind / class 分组统计比值，找出「估算系统性偏小」的那一类
        groups: dict[str, list[float]] = {}
        for r in rows:
            key = r["kind"] or r["cls"].split()[0]
            groups.setdefault(key, []).append(r["est"] / r["real"])

        print()
        print("估算 / 实测（1.00 为完美；<0.8 说明低估，>1.3 说明高估）：")
        for k in sorted(groups):
            v = groups[k]
            med = statistics.median(v)
            flag = ""
            if med < 0.80:
                flag = "  <-- 低估"
            elif med > 1.30:
                flag = "  <-- 高估"
            print(f"  {k:<12} n={len(v):<4} 中位 {med:.3f}  区间 "
                  f"{min(v):.2f}~{max(v):.2f}{flag}")

        # 兜底检测：inline style 缺失的块会退回 CSS 的 110px
        noinline = [r for r in data["rows"] if not r["inline"]]
        print()
        print(f"inline 占位缺失  : {len(noinline)} / {len(data['rows'])}")
        if noinline:
            print("  [警告] 以下块会退回 app.css 的 auto 110px 兜底：")
            for r in noinline[:8]:
                print(f"    {r['cls']:<10} 实测 {r['real']:>6}px  "
                      f"声明 {r['est']}px")

        est_sum = sum(r["est"] for r in rows)
        real_sum = sum(r["real"] for r in rows)
        print()
        print(f"总高（参与比对的块）:")
        print(f"  估算合计 {est_sum:>9.0f} px")
        print(f"  实测合计 {real_sum:>9.0f} px")
        print(f"  偏差     {pct((est_sum - real_sum) / real_sum)}")

        sweep = page.evaluate(JS_SWEEP)
        if not sweep.get("error"):
            print()
            print("滚动往返后的总高变化：")
            print(f"  出发 {sweep['h0']:>9} px")
            print(f"  到顶 {sweep['hTop']:>9} px")
            print(f"  到底 {sweep['hBot']:>9} px")
            print(f"  回到出发位置 {sweep['hBack']:>9} px")
            g = sweep["growth"]
            ratio = g / sweep["h0"] if sweep["h0"] else 0
            verdict = "通过" if abs(ratio) <= 0.05 else "超标"
            print(f"  净增 {g:>+9} px ({pct(ratio)})  [{verdict}]")

        if a.json:
            print()
            print(json.dumps({"data": data, "sweep": sweep},
                             ensure_ascii=False)[:2000])

        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
