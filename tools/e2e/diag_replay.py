#!/usr/bin/env python3
"""诊断「重放」链路：前端发得出、宿主答得出、前端画得出。

## 这个脚本存在的理由：记录三次测量错误

它最初是为「重放无效」写的，结论是「宿主没响应」。**那个结论是假的。**
三次错误都出在测量本身，而不是产品：

  ① **出口被自己掐死**。第一版把 `Bridge.send` 换成 `(m) => spy.push(m)`，
     于是消息**根本没进宿主**。`outbound=1` 只证明「前端调了 send」，
     我却拿它当「宿主收到但没响应」的证据。更糟的是那个 spy 函数引用
     了 `window.__out`，脚本结束一删，下一次点击直接抛
     `TypeError: Cannot read properties of undefined (reading 'push')` ——
     页面从此发不出任何消息，后续每一轮测量都在污染上叠加污染。

  ② **测错了容器**。渲染目标是 `#inner`（`app.js:229`），我测的是
     `#stream`。`#stream` 只有一个子元素就是 `#inner`，所以
     `streamChildren` 永远是 1，看起来像「什么都没渲染」。

  ③ **没看元素是否可见**。当时页面停在「监视器」视图，
     `#stream.hidden === true`。就算 `#inner` 里已经铺满消息，
     从 `#stream` 量也是 0。

## 正确的测法

  · **不碰出口**。重放是只读操作（`conversation.current`），
    不需要护栏。要护栏的是 `delConv` / `reset` 这类会改状态的。
  · **量 `#inner.children.length`**，那才是渲染目标。
  · **入站监听要能摘**。保存 handler 引用，结束时
    `removeEventListener` —— 否则多跑几次会叠加出成倍的重复消息。
  · **判定用宿主产物**：`clear` 到了 = `ReplayConversation` 跑了
    （它第一件事就是 `ClearTranscript()` → `TranscriptCleared` → `SendCleared()`）。

## 只读

全程只点 `btnReplay`。不发消息、不删会话、不重置熔断。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ws_cdp import CdpPage  # noqa: E402

CDP_PORT = 9333

#: 挂入站监听。**保存 handler 引用**，否则结束时摘不掉，
#: 多跑几轮会让每条消息被记录 N 次（第一版就撞上了）。
JS_ARM = """
() => {
  // 出口不动。这是本脚本最重要的一条：任何「记录发送」的包装都会
  // 改变被测对象的行为，而重放根本不需要护栏。
  if (window.__diagHook) window.chrome.webview.removeEventListener('message', window.__diagHook);
  window.__diagIn = [];
  window.__diagHook = (e) => {
    let d = e.data;
    try { if (typeof d === 'string') d = JSON.parse(d); } catch (_) {}
    window.__diagIn.push(d);
    if (window.__diagIn.length > 2000) window.__diagIn.shift();
  };
  window.chrome.webview.addEventListener('message', window.__diagHook);
  return { armed: true, kind: window.Bridge && window.Bridge.kind };
}
"""

JS_DISARM = """
() => {
  if (window.__diagHook) {
    window.chrome.webview.removeEventListener('message', window.__diagHook);
    window.__diagHook = null;
  }
  window.__diagIn = [];
  return true;
}
"""

#: 量渲染**目标**（#inner），不是它的父容器（#stream）。
JS_SNAP = """
() => {
  const inner = document.getElementById('inner');
  const stream = document.getElementById('stream');
  const mon = document.getElementById('monitor');
  const active = document.querySelector('#chatList .chat.on');
  const rows = inner ? Array.from(inner.children) : [];
  return {
    innerChildren: rows.length,
    innerTextLen: inner ? inner.innerText.length : -1,
    streamHidden: stream ? stream.hidden : null,
    monitorHidden: mon ? mon.hidden : null,
    mhTitle: (document.getElementById('mhTitle') || {}).textContent,
    activeTitle: active ? (active.querySelector('.t') || {}).textContent : null,
    activeLocalId: active ? (active.dataset.localId || null) : null,
  };
}
"""

JS_VIEW_CHAT = """
() => {
  const b = document.querySelector('.nav-item[data-view="chat"]');
  if (b) b.click();
  return { switched: !!b, streamHidden: document.getElementById('stream').hidden };
}
"""

JS_CLICK_REPLAY = """
() => {
  const b = document.getElementById('btnReplay');
  if (!b) return { found: false };
  window.__diagIn.length = 0;
  try { b.click(); return { found: true, clicked: true, visible: !!(b.offsetWidth || b.offsetHeight) }; }
  catch (e) { return { found: true, clicked: false, err: String(e) }; }
}
"""

JS_DRAIN = """
() => {
  const a = window.__diagIn || [];
  window.__diagIn = [];
  return a;
}
"""


def main() -> int:
    d = json.load(urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/list", timeout=3))
    pages = [t for t in d if t.get("type") == "page"]
    if not pages:
        print(f"[错误] CDP {CDP_PORT} 上没有页面")
        return 2

    with CdpPage(pages[0]["webSocketDebuggerUrl"]) as page:
        def call(src: str):
            # CDP 的 Runtime.evaluate 不会自动调用 `() => {}`，
            # 必须显式包成 IIFE，否则只求值出一个函数对象。
            return page.evaluate(f"({src})()")

        print("=== 0. 挂监听（不动出口）===")
        print("  ", call(JS_ARM))

        # 先切到对话流视图 —— 否则 stream 是 hidden，一切看起来都像空的。
        print("\n=== 1. 切到对话流 ===")
        print("  ", call(JS_VIEW_CHAT))
        time.sleep(0.3)

        before = call(JS_SNAP)
        print("\n=== 2. 点击前 ===")
        for k, v in before.items():
            print(f"   {k:16} = {v!r}")

        print("\n=== 3. 点 btnReplay ===")
        click = call(JS_CLICK_REPLAY)
        print("  ", click)

        all_msgs: list[dict] = []
        t0 = time.time()
        for mark in (1, 3, 8):
            while time.time() - t0 < mark:
                time.sleep(0.2)
            chunk = call(JS_DRAIN)
            all_msgs.extend(chunk)
            print(f"   t={mark}s  新增 {len(chunk)}（累计 {len(all_msgs)}）")

        time.sleep(0.5)
        after = call(JS_SNAP)
        print("\n=== 4. 点击后 ===")
        for k, v in after.items():
            print(f"   {k:16} = {v!r}")

        from collections import Counter
        kinds = Counter(m.get("t") for m in all_msgs if isinstance(m, dict))
        print("\n=== 5. 判定 ===")
        print("   入站类型分布:", dict(kinds))

        got_clear = kinds.get("clear", 0) > 0
        got_item = kinds.get("item", 0) > 0
        # **不能拿「节点数增长」当判据**：重放的是同一份对话，先清后建，
        # 前后节点数天然相等（16 -> 16）。正确判据是「重放产出的 item
        # 条数与最终节点数对得上」，这才能区分「重建成功」与「什么都没做」。
        rendered = after["innerChildren"]
        matched = got_item and rendered == kinds.get("item", 0)

        print(f"   收到 clear          : {got_clear}")
        print(f"   收到 item           : {kinds.get('item', 0)} 条")
        print(f"   #inner 子节点       : {before['innerChildren']} -> {rendered}")
        print(f"   item 数与节点数一致 : {matched}")
        print(f"   顶栏标题            : {after['mhTitle']}")

        if not got_clear:
            print("\n   [失败] 没有 clear —— ReplayConversation 没跑。")
            ok = False
        elif not got_item:
            print("\n   [失败] 有 clear 但没有 item —— 当前对话没有可重放的消息。")
            ok = rendered > 0
        elif not matched:
            print(f"\n   [失败] 收到 {kinds.get('item', 0)} 条 item，"
                  f"但 #inner 只有 {rendered} 个节点 —— 前端渲染断链。")
            ok = False
        else:
            print("\n   [通过] 前端发得出、宿主答得出、前端画得出。")
            ok = True

        call(JS_DISARM)
        print("\n   监听已摘（不会给下一轮留下重复 hook）。")
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
