#!/usr/bin/env python3
"""Web 壳会话列表可访问性验证。

## 这条缺陷是怎么发现的

旅程 4 用 UIA 读窗口结构时，51 个会话项**全部**是 `ControlType.Text` ——
屏幕阅读器会把它们念成标题，用户不会知道可以点；键盘也 Tab 不到。
WPF 壳用的是真按钮，没这问题，所以这是**重构到 HTML 时丢掉的语义**。

## 为什么分两层验

  · **DOM 层**（CDP）：角色、tabindex、aria-*、键盘事件是否真的接上
  · **UIA 层**（Windows Automation）：WebView2 有没有把 DOM 语义
    如实翻译给辅助技术

只验 DOM 是不够的 —— DOM 上写了 `role="button"` 不等于 Chromium 会把它
暴露成 `ControlType.Button`。反过来只验 UIA 也不够 —— 出了问题分不清
是「没写」还是「写了但没被采纳」。两层都验才能定位。

## 安全

键盘激活会触发 `attachSession`（**会出网**）。所以激活测试前必须先把
`Bridge.send` 换成 spy —— 复用 journey8 的三重护栏：

  1. 出口换成 spy，拦在 JS 层
  2. `Bridge.send` 不可覆写时**直接抛错中止**，绝不退化成真发送
  3. 探针前后各读一次引擎日志 `run_end` 计数，有增长就报红

用法：
    python tools/e2e/verify_a11y_chatlist.py
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from playwright.sync_api import sync_playwright  # noqa: E402

import web_common  # noqa: E402


#: 读 DOM 层的可访问性事实。只读，不改状态。
JS_DOM = """
() => {
  const box = document.getElementById('chatList');
  if (!box) return { error: 'chatList 不存在' };
  const rows = Array.from(box.querySelectorAll('.chat'));
  const first = rows[0];
  const actives = rows.filter(r => r.getAttribute('aria-current') === 'true');
  return {
    groupRole: box.getAttribute('role'),
    groupLabel: box.getAttribute('aria-label'),
    rowCount: rows.length,
    // 每个必需属性各自统计，缺哪条一眼看得出来
    roleButton: rows.filter(r => r.getAttribute('role') === 'button').length,
    tabbable: rows.filter(r => r.tabIndex === 0).length,
    hasDataId: rows.filter(r => r.dataset && r.dataset.id).length,
    // 选中项：要么 0 个（没有当前会话），要么恰好 1 个
    ariaCurrent: actives.length,
    // 视觉高亮与 aria-current 必须一致 —— 不一致就是「说一套做一套」
    visualOn: rows.filter(r => r.classList.contains('on')).length,
    sample: first ? {
      role: first.getAttribute('role'),
      tabIndex: first.tabIndex,
      dataId: first.dataset ? first.dataset.id : null,
      title: first.querySelector('.t') ? first.querySelector('.t').textContent : null,
    } : null,
  };
}
"""

#: 用真实键盘事件激活第一行，出口换成 spy。
#:
#: 为什么用 dispatchEvent 而不是 page.keyboard：后者会真的把按键送进
#: Chromium 的输入管线，理论上更真实 —— 但这里要验的是「app.js 里那个
#: keydown handler 有没有接对」，dispatchEvent 走的正是同一条 DOM 事件路径。
#: 而且出口已掐，不存在出网风险。
#:
#: 两种键都测：Enter 和 Space。只测 Enter 会漏掉「空格没反应」——
#: 而空格恰恰是屏幕阅读器用户的默认激活键。
JS_ACTIVATE = """
async (key) => {
  const realSend = window.Bridge.send;
  const seen = [];
  window.Bridge.send = (m) => { seen.push(m); };
  if (window.Bridge.send === realSend) {
    throw new Error('出口掐不住（Bridge.send 不可覆写），拒绝执行激活探测');
  }

  const row = document.querySelector('#chatList .chat');
  if (!row) { window.Bridge.send = realSend; return { error: '没有会话项' }; }

  row.focus();
  const focused = document.activeElement === row;
  const ev = new KeyboardEvent('keydown', {
    key: key, bubbles: true, cancelable: true,
  });
  row.dispatchEvent(ev);

  await new Promise(r => setTimeout(r, 60));
  window.Bridge.send = realSend;

  const attachs = seen.filter(m => m && m.t === 'attachSession');
  return {
    focused,
    defaultPrevented: ev.defaultPrevented,
    attachCount: attachs.length,
    attachId: attachs.length ? attachs[0].id : null,
    rowDataId: row.dataset.id,
  };
}
"""

#: UIA 探针：统计窗口里 ControlType.Button 的数量与名字。
#: 判据是「会话项的标题出现在 Button 名字里」—— 说明 WebView2 真的把
#: DOM 的 role="button" 翻译成了 UIA 的 Button。
PS_UIA = r"""
$all = $w.FindAll([System.Windows.Automation.TreeScope]::Descendants,
                  [System.Windows.Automation.Condition]::TrueCondition)
$buttons = @()
foreach ($e in $all) {
    try {
        if ($e.Current.ControlType.ProgrammaticName -eq 'ControlType.Button') {
            $buttons += $e.Current.Name
        }
    } catch {}
}
Save (ConvertTo-Json -InputObject @{ buttonNames = $buttons } -Depth 3 -Compress)
"""


def cdp_pages(port: int) -> list[dict]:
    d = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=3))
    return [t for t in d if t.get("type") == "page"]


def count_run_end(log_path: Path) -> int:
    """数引擎日志里 `run_end` 事件的累计条数。

    这是出口护栏的最后一道：不管前端怎么绕，真出网就一定有 run_end。
    """
    if not log_path or not log_path.exists():
        return -1
    n = 0
    for ln in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if '"run_end"' in ln:
            n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9333)
    a = ap.parse_args()

    pages = cdp_pages(a.port)
    if not pages:
        print("[错误] CDP 不可达，壳没在跑？")
        return 2
    print(f"接管: {pages[0]['url']}")

    pid = web_common.find_pid()
    log_path, engine_pid = web_common.engine_log_for(pid)
    before = count_run_end(log_path)
    print(f"壳 PID: {pid} / 引擎 PID: {engine_pid}")
    print(f"引擎日志: {log_path.name}")
    print(f"run_end 基线: {before}")

    all_ok = True
    titles: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{a.port}")
        page = browser.contexts[0].pages[0]

        page.reload(wait_until="domcontentloaded")
        page.wait_for_timeout(900)

        print("\n=== DOM 层 ===")
        dom = page.evaluate(JS_DOM)
        if dom.get("error"):
            print(f"  [错误] {dom['error']}")
            browser.close()
            return 2
        print(f"  容器 role          : {dom['groupRole']}")
        print(f"  容器 aria-label    : {dom['groupLabel']}")
        print(f"  会话项数           : {dom['rowCount']}")
        print(f"  role=button        : {dom['roleButton']} / {dom['rowCount']}")
        print(f"  tabindex=0         : {dom['tabbable']} / {dom['rowCount']}")
        print(f"  data-id            : {dom['hasDataId']} / {dom['rowCount']}")
        print(f"  aria-current=true  : {dom['ariaCurrent']}（视觉 .on: {dom['visualOn']}）")
        if dom["sample"]:
            s = dom["sample"]
            print(f"  样本               : role={s['role']!r} tabIndex={s['tabIndex']} "
                  f"dataId={s['dataId']!r} title={s['title']!r}")

        checks = [
            ("容器 role=group", dom["groupRole"] == "group"),
            ("容器有 aria-label", bool(dom["groupLabel"])),
            ("每项 role=button", dom["roleButton"] == dom["rowCount"] and dom["rowCount"] > 0),
            ("每项可 Tab", dom["tabbable"] == dom["rowCount"]),
            ("每项有 data-id", dom["hasDataId"] == dom["rowCount"]),
            ("aria-current 与视觉高亮一致", dom["ariaCurrent"] == dom["visualOn"]),
        ]
        print()
        for name, ok in checks:
            all_ok = all_ok and ok
            print(f"  {name:<30} {'[通过]' if ok else '[失败]'}")

        # 记下前几条标题，供 UIA 层比对
        titles = page.evaluate(
            "() => Array.from(document.querySelectorAll('#chatList .chat .t'))"
            ".slice(0, 5).map(n => n.textContent)"
        )

        print("\n=== 键盘激活（出口已掐）===")
        for key, label in (("Enter", "Enter"), (" ", "Space")):
            r = page.evaluate(JS_ACTIVATE, key)
            if r.get("error"):
                print(f"  {label:<6} [错误] {r['error']}")
                all_ok = False
                continue
            ok = (r["focused"] and r["attachCount"] == 1
                  and r["attachId"] == r["rowDataId"])
            all_ok = all_ok and ok
            print(f"  {label:<6} 焦点={r['focused']} "
                  f"attachSession={r['attachCount']} "
                  f"id匹配={r['attachId'] == r['rowDataId']} "
                  f"阻止默认={r['defaultPrevented']} "
                  f"{'[通过]' if ok else '[失败]'}")

        browser.close()

    print("\n=== UIA 层 ===")
    out, rc = web_common.run_ps(PS_UIA, "a11y")
    if rc != 0 or not out.strip():
        print(f"  [警告] UIA 探测失败 rc={rc}")
        print(f"  {out[:300]}")
    else:
        try:
            data = json.loads(out)
            names = data.get("buttonNames") or []
            print(f"  ControlType.Button 数量: {len(names)}")
            # 判据：会话标题出现在某个 Button 的名字里
            hits = [t for t in titles if any(t and t in n for n in names)]
            print(f"  会话标题命中 Button: {len(hits)} / {len(titles)}")
            for t in titles[:5]:
                print(f"    {'[命中]' if t in hits else '[未命中]'} {t!r}")
            ok = len(hits) >= max(1, len(titles) - 1)
            all_ok = all_ok and ok
            print(f"  {'[通过]' if ok else '[失败]'} 会话项已暴露为 Button")
        except Exception as e:  # noqa: BLE001
            print(f"  [警告] 解析 UIA 输出失败: {e}")
            print(f"  原始输出: {out[:300]}")

    print("\n=== 出口护栏 ===")
    after = count_run_end(log_path)
    print(f"run_end: {before} -> {after}")
    if before >= 0 and after >= 0:
        if after > before:
            print(f"  [失败] 有 {after - before} 条真实消息出网！")
            all_ok = False
        else:
            print("  [通过] 无增长")

    print()
    print("判定:", "[通过] 会话列表可访问性达标" if all_ok else "[失败] 见上方")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
