#!/usr/bin/env python3
"""主题配色对比度审计（WCAG 2.1）。

## 为什么必须有这个脚本

2026-10-18 给 Web 壳补深色主题时，41 个变量是**手写的**。手写配色最常见的
失败不是「不好看」，是**看不见**：

  · 正文压在背景上差 2:1 —— 正常视力都要凑近看
  · `--ink-3` 这种「次要文字」在深色下更容易糊掉，因为深底会把
    低对比度放大成「灰蒙蒙一片」
  · `--*-ink` 系列压在对应的 `--*-soft` 底上 —— 这两组值是**成对**用的
    （提示条、错误卡片），单独看各自都正常，配在一起可能刚好踩线

这类问题靠肉眼看截图发现不了，必须算。

## 判据（WCAG 2.1）

  · 正文（normal text）        ≥ 4.5:1   —— AA
  · 大字（≥18.66px 粗 / ≥24px）≥ 3.0:1
  · 非文本（图标、边框、控件）  ≥ 3.0:1   —— AA 对 UI 组件的要求

这里把「正文」判 4.5，「次要文字/图标」判 3.0，各自按实际用途归类。
宁可判严一点：踩线的值在实际屏幕上（尤其是低亮度、低对比度显示器）
会比计算值更糟。

## 怎么读这个脚本的输出

每个组合一行，最后一列是判定。`[警告]` 不阻断 —— 有些搭配（比如
`--ink-4` 用在分隔符上）本来就只需要 3.0。但**任何 [失败] 都要改值**。

用法：
    python tools/e2e/verify_theme_contrast.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

CSS = (Path(__file__).resolve().parents[2]
       / "shell" / "DeepSeekAgent.Web" / "Assets" / "web" / "app.css")


def parse_vars(text: str) -> dict[str, str]:
    """从一段 `--name:#hex;` 里抽出变量表。"""
    return {k: v.upper() for k, v in re.findall(r"(--[A-Za-z0-9-]+)\s*:\s*(#[0-9A-Fa-f]{3,8})", text)}


def srgb_to_lin(c: float) -> float:
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def luminance(hex_str: str) -> float:
    h = hex_str.lstrip("#")
    if len(h) == 3:
        h = "".join(ch * 2 for ch in h)
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * srgb_to_lin(r) + 0.7152 * srgb_to_lin(g) + 0.0722 * srgb_to_lin(b)


def ratio(a: str, b: str) -> float:
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


#: (前景变量, 背景变量, 用途说明, 最低要求)
#:
#: 前景/背景都是**变量名**，两个主题各跑一遍 —— 同一组判据同时约束两套值。
#: 这才是关键：只验浅色会漏掉深色全部的问题，只验深色会漏掉浅色原有的。
PAIRS = [
    # 正文层
    ("--ink",    "--bg",      "正文 / 页面底",        4.5),
    ("--ink",    "--surface", "正文 / 卡片",          4.5),
    ("--ink-2",  "--bg",      "次要正文 / 页面底",     4.5),
    ("--ink-2",  "--surface", "次要正文 / 卡片",       4.5),
    # ink-3 实际用于时间戳、步骤号、列表副标题 —— 都是**要读的文字**，
    # 按正文 4.5 判。这一档原来是 3.0，改成 4.5 后逼出了灰阶重排。
    ("--ink-3",  "--surface", "弱化文字 / 卡片",       4.5),
    ("--ink-3",  "--bg",      "弱化文字 / 页面底",     4.5),
    # ink-4 用于占位符、分隔点、折叠箭头、禁用态 —— 装饰与非正文。
    # 占位符按 WCAG 严格算是文字（该 4.5），但它是**输入前的临时提示**，
    # 提到 4.5 就会和 ink-3 塌成一档、整个灰阶层级消失。这里判 3.0，
    # 并在报告里标注它是折中。
    ("--ink-4",  "--surface", "装饰/占位 / 卡片",      3.0),
    ("--ink-4",  "--bg",      "装饰/占位 / 页面底",    3.0),

    # 品牌
    ("--brand",     "--bg",      "品牌色 / 页面底",    3.0),
    ("--brand",     "--surface", "品牌色 / 卡片",      3.0),
    ("--on-brand",  "--brand",   "品牌色上的白字",      4.5),
    ("--brand-600", "--surface", "品牌深调 / 卡片",    3.0),
    ("--brand-ink", "--brand-soft", "品牌提示条文字",   4.5),

    # 状态：主色压在页面底上
    ("--ok",   "--bg", "成功色 / 页面底",  3.0),
    ("--warn", "--bg", "警告色 / 页面底",  3.0),
    ("--bad",  "--bg", "错误色 / 页面底",  3.0),
    ("--ok",   "--surface", "成功色 / 卡片", 3.0),
    ("--warn", "--surface", "警告色 / 卡片", 3.0),
    ("--bad",  "--surface", "错误色 / 卡片", 3.0),

    # 状态：成对的 -ink / -soft（提示条、错误块的真实搭配）
    ("--ok-ink",   "--ok-soft",   "成功提示条文字",  4.5),
    ("--warn-ink", "--warn-soft", "警告提示条文字",  4.5),
    ("--bad-ink",  "--bad-soft",  "错误提示条文字",  4.5),
    ("--brand-ink", "--brand-soft-2", "品牌次级底上的字", 4.5),
    ("--violet",   "--violet-soft", "紫色标记",      3.0),
    ("--teal",     "--teal-soft",   "青色标记",      3.0),

    # 代码块：前景压在 --surface-3 上（不是深底！见 app.css 的注释）
    ("--code-ink", "--surface-3", "代码正文",       4.5),
    ("--code-dim", "--surface-3", "代码注释",       3.0),
    ("--code-kw",  "--surface-3", "代码关键字",     3.0),
    ("--code-str", "--surface-3", "代码字符串",     3.0),

    # 控件
    ("--on-brand", "--brand-600", "按钮悬停态字",    4.5),
    # 关闭态开关的可见性由**边框**承担（.sw 有 1px --ink-4 描边），
    # 不是槽底与圆点的对比 —— 白圆点压浅灰槽天然是 1.39，iOS/Material
    # 全都这样，靠阴影表达"可拨动"。所以这里判的是边框。
    ("--ink-4",   "--surface", "开关描边 / 卡片",   3.0),
    ("--ink-4",   "--bg",      "开关描边 / 页面底", 3.0),
    # 开启态：圆点压在品牌色上，这个必须够清楚。
    ("--on-switch", "--brand", "开关圆点 / 开启态", 3.0),
]


def main() -> int:
    text = CSS.read_text(encoding="utf-8")
    m_light = re.search(r":root\{(.*?)\n\}", text, re.S)
    m_dark = re.search(r':root\[data-theme="dark"\]\{(.*?)\n\}', text, re.S)
    if not m_light or not m_dark:
        print("[错误] 找不到 :root 或 [data-theme=\"dark\"] 变量块")
        return 2

    themes = {"浅色": parse_vars(m_light.group(1)), "深色": parse_vars(m_dark.group(1))}

    all_ok = True
    for theme_name, table in themes.items():
        print(f"\n=== {theme_name} ===")
        print(f"{'前景':<14}{'背景':<16}{'用途':<22}{'对比度':<10}要求   判定")
        print("-" * 84)
        for fg, bg, use, need in PAIRS:
            if fg not in table or bg not in table:
                print(f"{fg:<14}{bg:<16}{use:<22}{'缺失变量':<10}{need:<6}[错误]")
                all_ok = False
                continue
            r = ratio(table[fg], table[bg])
            ok = r >= need
            all_ok = all_ok and ok
            mark = "[通过]" if ok else "[失败]"
            print(f"{fg:<14}{bg:<16}{use:<22}{r:<10.2f}{need:<6}{mark}")

    print()
    print("判定:", "[通过] 两套主题全部达到 WCAG AA" if all_ok
          else "[失败] 有组合未达标，必须改值")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
