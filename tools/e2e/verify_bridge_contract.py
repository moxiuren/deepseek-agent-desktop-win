#!/usr/bin/env python3
"""Web 壳双向消息契约检查 —— 静态扫描，不需要壳在跑。

## 为什么 Web 壳需要这道门禁，而 WPF 壳不需要

WPF 壳的「前端」是 C# 自己写的：按钮直接绑方法，编译器会拦下拼错的成员名。
Web 壳的前端是 JS，跟宿主之间只有**字符串**：
    JS:  send({ t: 'switchConv', id: ... })     → postMessage
    C#:  switch (type) { case "switchConv": ... }

字符串拼错，编译器一个字都不会说。用户看到的是「点了没反应」——
没有异常、没有日志、没有红色，只有沉默。这类缺陷在 WPF 壳里根本不存在，
所以它是 Web 壳独有的，必须单独设闸。

## 两个方向都要查

**前端 → 宿主**：前端发了宿主不认识的 t → 消息石沉大海。
    这类最常见（改名字时只改一边）。

**宿主 → 前端**：宿主发了前端不认识的 t → 视图不更新。
    比上一种更阴险：数据是对的，只是画不出来。用户会以为「功能坏了」。

## 为什么用静态扫描而不是运行时探针

运行时探针只能覆盖「测试当时点过的按钮」。真正的漏网之鱼恰恰是
没被点到的那些 —— 而契约缺陷的特点是**静态可判定**：把所有字符串
收齐做集合差，比跑一遍界面更全、更快、且不需要壳在跑。

运行时探针仍有价值（它验的是「这条链路真的通」），但那是旅程测试的活。
这道闸的职责是：**任何一次改动后，两个方向都不许出现孤儿消息**。

用法：
    python tools/e2e/verify_bridge_contract.py
    python tools/e2e/verify_bridge_contract.py --verbose
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BRIDGE = ROOT / "shell" / "DeepSeekAgent.Web" / "WebBridge.cs"
APPJS = ROOT / "shell" / "DeepSeekAgent.Web" / "Assets" / "web" / "app.js"
BRIDGEJS = ROOT / "shell" / "DeepSeekAgent.Web" / "Assets" / "web" / "bridge.js"

#: 其余「会跟宿主说话」的前端模块。**新增一个就要在这里登记一个** ——
#: 漏登记的话，那个文件里拼错的 `t` 不会被任何门禁拦住，而这正是
#: 本脚本存在的全部理由。
#:
#: `tts.js` 的三条命令（tts / ttsVoices / ttsVoice）目前走的是变量路径
#: （`send(Object.assign({ t: kind, req }, payload))`），静态扫不出字面量，
#: 所以它们会出现在「宿主支持但前端未直接发送」的参考列表里 —— 那是**预期**的，
#: 不是缺陷。把它登记进来是为了覆盖将来在这里写死的那些 `t`。
EXTRA_FRONT = [
    ROOT / "shell" / "DeepSeekAgent.Web" / "Assets" / "web" / "tts.js",
]

#: 前端发消息的写法有三种，都要认：
#:   send({ t: 'x' })          —— app.js 里的主写法
#:   send({ t: "x" })
#:   Bridge.send({ t: 'x' })
#: 以及 t 值放在变量里的（如 send({ t: kind })），那种静态查不到，
#: 所以下面还要单独找「宿主认识但前端从没发过」的类型，人工确认它们
#: 是不是走了变量路径。
RE_T_SINGLE = re.compile(r"""\bt\s*:\s*'([A-Za-z][A-Za-z0-9]*)'""")
RE_T_DOUBLE = re.compile(r"""\bt\s*:\s*"([A-Za-z][A-Za-z0-9]*)\"""")

#: 宿主侧的 case 分支。
RE_CASE = re.compile(r"""case\s+"([A-Za-z][A-Za-z0-9]*)"\s*:""")

#: 宿主**发往**前端。WebBridge 里统一走 Send(...) 或 Post(...)，
#: 形态是 new { t = "xxx", ... }。
RE_OUT_T = re.compile(r"""\bt\s*=\s*"([A-Za-z][A-Za-z0-9]*)\"""")

#: **宿主主动推送**的类型。有些消息前端永远不该主动请求 ——
#: 宿主在合适的时机自己推。把它们算进「前端应该发」会误报成死代码。
#:
#: 实测踩到的：`models` 曾经被报成「宿主支持但前端从没发过」，
#: 而实际上 WebBridge 在 `case "hello"` 里已经 `_ = RefreshModelsAsync()`，
#: 握手完就把列表推下来了。**推送型消息不该要求前端主动发。**
PUSH_ONLY = {
    "models",       # hello 握手后由宿主推（WebBridge.cs 的 case "hello"）
    "idLabel",      # 身份标识快照，随 idstate 一起推
    "refreshConvs", # 旧协议名，保留兼容（源码注释已标明），前端改发 listRemote
    # 档位清单：hello 后推一次，每次 setTransport 后再推一次。
    # 前端确实有一个 `case "transports"` 的**请求**入口（宿主侧同名 case），
    # 但目前没人调用 —— 推送已经覆盖全部场景，留请求入口是为了将来
    # 「配置页被打开时主动刷新」。所以它不是死代码，是尚未接线的入口。
    "transports",
}

#: 前端**接收**宿主消息。每个会收消息的模块各有一个 `handle(m)`，里面是
#: `switch (m.t) { case 'xxx': ... }`。**不能全局扫 `case 'x'`** ——
#: 文件里还有别的 switch（例如 transcript kind 分派），
#: 它们的 case 是 'Assistant' / 'Thinking' 这种，会把集合撑大，
#: 让真正的孤儿藏在一堆噪声里。
#:
#: 两种写法都要认：
#:   function handle(m) { ... }   —— app.js 的顶层函数
#:   handle(m) { ... }            —— tts.js 里的对象方法简写
#: 早先只认第一种，结果 tts.js 里 6 个新 case 全被判成「前端没处理」。
RE_HANDLE_START = re.compile(r"(?:function\s+handle|handle)\s*\(\s*(\w+)\s*\)\s*\{")
RE_CASE_ANY = re.compile(r"""case\s+['"]([A-Za-z][A-Za-z0-9]*)['"]\s*:""")


def handle_bodies(src: str) -> list[str]:
    """取出每个 `handle(m)` 的函数体，按花括号配平切。

    **为什么不用 `(.*?)\\n\\}` 这种非贪婪匹配**：对象方法简写的闭合是
    `    },`（带缩进、带逗号），`\\n\\}` 根本匹配不上；放宽成 `\\n\\s*\\}`
    又会在内层第一个闭合行提前截断，把后半段的 case 漏掉。
    配平对两种形态都成立，且不依赖缩进风格。
    """
    out: list[str] = []
    for m in RE_HANDLE_START.finditer(src):
        start = m.end() - 1          # 指向开头的 '{'
        depth = 0
        for j in range(start, len(src)):
            c = src[j]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    out.append(src[start + 1:j])
                    break
    return out


def read(p: Path) -> str:
    if not p.exists():
        raise SystemExit(f"[错误] 找不到 {p}")
    return p.read_text(encoding="utf-8", errors="replace")


def strip_comments(src: str) -> str:
    """去掉 JS 的 // 与 /* */ 注释。

    **不这么做会漏掉一整类假阳性**：代码里到处是这种注释 ——
        → {t:'pickFiles'} → 宿主回 {t:'files', files:[…]}
        // 结果由宿主回 {t:'files'} 或 {t:'err'}
    它们描述的是**宿主发来的**消息，却被正则当成「前端发送」。
    实测第一版因此报出 5 个假孤儿（err / files / ready / idres / idstate），
    全部来自注释。

    假阳性比漏报更危险：门禁红了但产品没问题，几次之后人就学会无视红灯。
    """
    # 块注释先处理（可以跨行），再处理行注释。
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    # 行注释：不能吃掉字符串里的 //（如 'https://...'）。
    # 简化处理：只剥掉「前面不是冒号」的 //，够用且不误伤 URL。
    src = re.sub(r"(?<![:\w])//[^\n]*", "", src)
    return src


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true", help="打印全部集合")
    a = ap.parse_args()

    bridge_src = read(BRIDGE)
    app_src_raw = read(APPJS)
    bjs_src_raw = read(BRIDGEJS) if BRIDGEJS.exists() else ""

    # mock.js 是**模拟宿主**：它的 `fire({t:'files'})` 是宿主在说话，
    # 不是前端在发送。混进来会产生假孤儿。所以只扫真实前端代码。
    app_src = strip_comments(app_src_raw)
    bjs_src = strip_comments(bjs_src_raw)
    front_src = app_src + "\n" + bjs_src
    for extra in EXTRA_FRONT:
        if extra.exists():
            front_src += "\n" + strip_comments(extra.read_text(encoding="utf-8", errors="replace"))

    host_accepts = set(RE_CASE.findall(bridge_src))
    front_sends = set(RE_T_SINGLE.findall(front_src)) | set(RE_T_DOUBLE.findall(front_src))

    host_sends = set(RE_OUT_T.findall(bridge_src))

    # 只扫各个 handle() 函数体里的 case —— 见 RE_HANDLE_START 的说明。
    # 扫的是**合并后的前端源码**（app.js + bridge.js + EXTRA_FRONT），
    # 因为接收方不止 app.js：tts.js 自己也有一个 handle。
    bodies = handle_bodies(front_src)
    if not bodies:
        print("[错误] 前端源码里找不到任何 handle(m) 函数 —— 入口改名了？")
        return 2
    front_accepts: set[str] = set()
    for b in bodies:
        front_accepts |= set(RE_CASE_ANY.findall(b))

    print("=" * 62)
    print("Web 壳双向消息契约")
    print("=" * 62)
    print(f"  宿主接受 {len(host_accepts):2} 种 · 前端发送 {len(front_sends):2} 种")
    print(f"  宿主发送 {len(host_sends):2} 种 · 前端接受 {len(front_accepts):2} 种")

    # ---- 方向一：前端发了，宿主不认 ----
    orphan_up = sorted(front_sends - host_accepts)
    # ---- 方向二：宿主发了，前端不认 ----
    orphan_down = sorted(host_sends - front_accepts)
    # ---- 宿主支持但前端从没发（可能是变量路径，也可能真是死代码）----
    unused = sorted(host_accepts - front_sends - PUSH_ONLY)

    ok = True

    print("\n[方向一] 前端发送 → 宿主接受")
    if orphan_up:
        ok = False
        print(f"  [错误] {len(orphan_up)} 种消息前端发了但宿主没有对应 case：")
        for t in orphan_up:
            print(f"         · {t}  —— 点击后静默无响应")
    else:
        print("  [通过] 无孤儿消息")

    print("\n[方向二] 宿主发送 → 前端接受")
    if orphan_down:
        ok = False
        print(f"  [错误] {len(orphan_down)} 种消息宿主发了但前端没有处理：")
        for t in orphan_down:
            print(f"         · {t}  —— 数据到了但界面不更新")
    else:
        print("  [通过] 无孤儿消息")

    print("\n[参考] 宿主支持但前端未直接发送（可能是变量路径，非缺陷）")
    if unused:
        for t in unused:
            print(f"         · {t}")
    else:
        print("         无")

    print("\n[参考] 推送型（宿主主动发，前端不该主动请求）")
    for t in sorted(PUSH_ONLY & host_accepts):
        print(f"         · {t}")

    if a.verbose:
        print("\n[明细] 宿主接受:", " ".join(sorted(host_accepts)))
        print("[明细] 前端发送:", " ".join(sorted(front_sends)))
        print("[明细] 宿主发送:", " ".join(sorted(host_sends)))
        print("[明细] 前端接受:", " ".join(sorted(front_accepts)))

    print("\n" + "=" * 62)
    print("[通过] 双向契约一致" if ok else "[错误] 契约存在缺口，见上")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
