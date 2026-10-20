#!/usr/bin/env python3
"""主题端到端验证：在真实 WebView2 里检查两套主题的实际呈现。

## 为什么不能只靠 verify_theme_contrast.py

那个脚本算的是**源码里写的值**。它证明不了三件事：

  1. 值有没有真的生效 —— 变量名拼错、选择器优先级被压、被后面的规则覆盖，
     源码全是对的，渲染出来却是别的颜色
  2. `data-theme` 切换有没有真的改变界面 —— 可能只改了属性、CSS 没跟上
  3. 首屏有没有闪 —— 深色用户开窗看到一帧浅色，这是只有真实加载才暴露的问题

所以这里用 CDP 连上运行中的壳，读 `getComputedStyle` 拿**实际生效值**。

## 判据

  · 两套主题的 --bg 必须不同（切换真的生效了）
  · 每套主题的 --ink / --bg 对比度必须 ≥ 4.5
  · 切换按钮的图标必须跟着换（月亮/太阳）
  · 首屏：重新加载后 data-theme 必须在第一帧就正确（无闪）

## 安全

本脚本**只读**，不点发送、不触发任何 Bridge.send，因此不会产生真实网络请求。
这是 2026-10-18 四次误发事故后定下的硬要求：任何触碰 UI 的探针，
先证明自己不会让消息出网。

用法：
    python tools/e2e/verify_theme_live.py
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from playwright.sync_api import sync_playwright  # noqa: E402


#: 读取当前实际生效的关键颜色与主题属性。只读，不改任何状态。
JS_READ = """
() => {
  const cs = getComputedStyle(document.documentElement);
  const bodyCs = getComputedStyle(document.body);
  const v = (n) => cs.getPropertyValue(n).trim();
  const btn = document.getElementById('btnTheme');
  const sun = btn && btn.querySelector('.ic-sun');
  const moon = btn && btn.querySelector('.ic-moon');
  return {
    dataTheme: document.documentElement.dataset.theme || null,
    colorScheme: cs.colorScheme,
    vars: {
      bg: v('--bg'), surface: v('--surface'), ink: v('--ink'),
      ink2: v('--ink-2'), ink3: v('--ink-3'), ink4: v('--ink-4'),
      brand: v('--brand'), onBrand: v('--on-brand'),
      scroll: v('--scroll'), codeInk: v('--code-ink'),
    },
    bodyBg: bodyCs.backgroundColor,
    bodyColor: bodyCs.color,
    // 图标的实际显示状态 —— display 是 none 说明被 CSS 藏了
    sunShown: sun ? getComputedStyle(sun).display !== 'none' : null,
    moonShown: moon ? getComputedStyle(moon).display !== 'none' : null,
    btnTitle: btn ? btn.title : null,
  };
}
"""

#: 切主题。**点真实按钮**，而不是直接调 ThemeBoot.set()。
#:
#: 为什么这个区别很重要：直接调 set() 只改 dataset 与 localStorage，
#: 跳过了 app.js 里更新按钮提示文字那段。初版就是这么写的，于是
#: 深色下按钮 title 仍是「切换到深色」—— 探针自己造成的假阴性。
#: 点按钮才会走完整路径：翻转判断 → set() → 更新 title。
#:
#: 按钮点击不会触发 Bridge.send（主题纯前端），所以不存在出网风险。
JS_SET = """
(t) => {
  const cur = document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light';
  if (cur !== t) document.getElementById('btnTheme').click();
  return document.documentElement.dataset.theme;
}
"""


def parse_rgb(s: str) -> tuple[int, int, int] | None:
    """把 'rgb(18, 21, 26)' / '#12151A' 统一成三元组。"""
    s = s.strip()
    if s.startswith("#"):
        h = s.lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    if s.startswith("rgb"):
        nums = s[s.find("(") + 1:s.find(")")].split(",")
        return tuple(int(float(n)) for n in nums[:3])  # type: ignore[return-value]
    return None


def rel_lum(rgb: tuple[int, int, int]) -> float:
    def lin(c: float) -> float:
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = rgb
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def contrast(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    la, lb = rel_lum(a), rel_lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def cdp_pages(port: int) -> list[dict]:
    d = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=3))
    return [t for t in d if t.get("type") == "page"]


def main() -> int:
    port = 9333
    pages = cdp_pages(port)
    if not pages:
        print("[错误] 没有可用的页面")
        return 2
    print(f"接管: {pages[0]['url']}")

    all_ok = True
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        page = browser.contexts[0].pages[0]

        # 先刷新一次：刚部署的 theme-boot.js 只有重新加载才会被引用。
        # 这也是本脚本能在「改了文件但壳没重启」的情况下验证新代码的原因 ——
        # 页面是 https://app.local 映射到磁盘目录，reload 就会读到新文件。
        page.reload(wait_until="domcontentloaded")
        page.wait_for_timeout(200)
        if not page.evaluate("() => typeof window.ThemeBoot"):
            print("[错误] ThemeBoot 未加载 —— theme-boot.js 没部署到位？")
            browser.close()
            return 2

        snaps = {}
        for theme in ("light", "dark"):
            got = page.evaluate(JS_SET, theme)
            page.wait_for_timeout(120)
            snap = page.evaluate(JS_READ)
            snaps[theme] = snap

            print(f"\n=== {theme} ===")
            print(f"  data-theme      : {snap['dataTheme']}")
            print(f"  color-scheme    : {snap['colorScheme']}")
            print(f"  body 背景        : {snap['bodyBg']}")
            print(f"  body 前景        : {snap['bodyColor']}")
            print(f"  --bg            : {snap['vars']['bg']}")
            print(f"  --surface       : {snap['vars']['surface']}")
            print(f"  --ink           : {snap['vars']['ink']}")
            print(f"  图标 太阳/月亮    : {snap['sunShown']} / {snap['moonShown']}")
            print(f"  按钮提示         : {snap['btnTitle']}")

            # 判据 1：data-theme 真的落到了 DOM 上
            if snap["dataTheme"] != theme:
                print(f"  [失败] data-theme 未生效，实际 {snap['dataTheme']!r}")
                all_ok = False

            # 判据 2：正文对比度（用**实际渲染值**，不是源码值）
            fg = parse_rgb(snap["bodyColor"])
            bg = parse_rgb(snap["bodyBg"])
            if fg and bg:
                r = contrast(fg, bg)
                ok = r >= 4.5
                all_ok = all_ok and ok
                print(f"  正文对比度       : {r:.2f} {'[通过]' if ok else '[失败] 需 >=4.5'}")
            else:
                print(f"  [错误] 解析不了颜色: {snap['bodyColor']} / {snap['bodyBg']}")
                all_ok = False

            # 判据 3：图标互斥（一个显示、一个隐藏）
            if theme == "dark":
                want_sun, want_moon = True, False
            else:
                want_sun, want_moon = False, True
            icon_ok = snap["sunShown"] == want_sun and snap["moonShown"] == want_moon
            all_ok = all_ok and icon_ok
            print(f"  图标互斥         : {'[通过]' if icon_ok else '[失败]'}")

            # 判据 4：提示文字说的是「点了会变成什么」，不是「现在是什么」。
            # 这一条很容易写反或忘记更新 —— 初版就是 set() 绕过按钮，
            # 深色下仍显示「切换到深色」，等于告诉用户「点了我还是深色」。
            want_title = "切换到浅色" if theme == "dark" else "切换到深色"
            title_ok = snap["btnTitle"] == want_title
            all_ok = all_ok and title_ok
            print(f"  提示文字         : {snap['btnTitle']!r} "
                  f"{'[通过]' if title_ok else f'[失败] 应为 {want_title!r}'}")

        # 判据 4：两套主题必须真的不同
        print("\n=== 主题差异 ===")
        diff_ok = True
        for key in ("bg", "surface", "ink", "brand", "scroll", "codeInk"):
            a = snaps["light"]["vars"][key].upper()
            b = snaps["dark"]["vars"][key].upper()
            changed = a != b
            diff_ok = diff_ok and changed
            print(f"  --{key:<10} 浅 {a:<10} 深 {b:<10} {'[变]' if changed else '[未变 ← 缺陷]'}")
        all_ok = all_ok and diff_ok

        # 判据 5：首屏无闪。重新加载后立刻读 data-theme —— theme-boot.js
        # 在 <head> 同步执行，所以 DOMContentLoaded 时它必须已经设好。
        print("\n=== 首屏（深色）===")
        page.evaluate(JS_SET, "dark")
        page.reload(wait_until="domcontentloaded")
        first = page.evaluate("() => document.documentElement.dataset.theme")
        boot_ok = first == "dark"
        all_ok = all_ok and boot_ok
        print(f"  DOMContentLoaded 时 data-theme: {first!r} "
              f"{'[通过] 无闪' if boot_ok else '[失败] 首屏闪浅色'}")

        # 复位成浅色，别把界面留在测试状态
        page.evaluate(JS_SET, "light")
        browser.close()

    print()
    print("判定:", "[通过] 两套主题在真实渲染环境里都正确" if all_ok else "[失败] 见上方")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
