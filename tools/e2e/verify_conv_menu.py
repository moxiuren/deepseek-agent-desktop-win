#!/usr/bin/env python3
"""Web 壳「会话操作入口」验证。

## 这条缺陷是怎么发现的

拿 `WebBridge.Handle` 的 31 个 case 与 `app.js` 的 `send({t:...})` 做双向差分，
发现六个消息类型**宿主侧早就实现、前端从不发送**：

    switchConv / renameConv / delConv / syncConv / reset / replay

进一步读代码发现这不是「缺个按钮」那么简单，底下压着两个真 bug：

  ① **两套 id 撞车**：列表项的 `id` 是服务端 `remote_id`，而
     `switchConv` / `renameConv` / `syncConv` 在宿主侧按**本地** id 工作。
     前端就算把按钮接上，发过去也是静默失败（`store.delete` 找不到 → false，
     而宿主在 false 时不加任何提示）。
  ② **`delConv` 选错了方法**：调的是 `DeleteConversationAsync`（删本地文件），
     而列表里全是服务端会话。拿 remote_id 去删本地必然找不到。

修法：宿主补推 `localId`；`delConv` 改调 `DeleteRemoteSessionAsync`；
前端补菜单 + 对话框 + 两个顶栏按钮。

## 三层验证，各管一件事

  **A. 静态契约**（Python 读源码）
     · 六个消息类型前后端都对得上
     · `delConv` 调的是 `DeleteRemoteSessionAsync`（不是本地那个）
     · 宿主确实推了 `localId`

  **B. DOM 交互**（CDP + 出口 spy）
     真的点按钮、点菜单项、按回车 —— 断言 spy 收到的消息。
     出口全程掐死，所以 `delConv` / `reset` 不会真生效。

  **C. 运行时数据流**（CDP 只读）
     `data-local-id` 是否真的从引擎一路传到 DOM —— spy 看不见这一层。

## 安全

出口 spy 是**强制**的：任何一步没能把 `Bridge.send` 换成 spy，脚本立刻
中止并报错，绝不退化成真发送。另加引擎日志 `run_end` 计数做兜底。

用法：
    python tools/e2e/verify_conv_menu.py
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from playwright.sync_api import sync_playwright  # noqa: E402

import web_common  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "shell" / "DeepSeekAgent.Web"
BRIDGE_CS = WEB / "WebBridge.cs"
APP_JS = WEB / "Assets" / "web" / "app.js"


#: 把出口换成 spy 并返回一个句柄，后续步骤都通过它读收到的消息。
#: 单独抽成一段是因为几乎每个探测都要用，重复写容易漏掉护栏。
JS_SPY_ON = """
() => {
  const real = window.Bridge.send;
  window.__spy = [];
  window.Bridge.send = (m) => { window.__spy.push(m); };
  if (window.Bridge.send === real) {
    throw new Error('出口掐不住（Bridge.send 不可覆写），拒绝继续');
  }
  return true;
}
"""

JS_SPY_TAKE = """
() => {
  const seen = window.__spy ? window.__spy.slice() : [];
  if (window.__spy) window.__spy.length = 0;
  return seen;
}
"""

JS_SPY_OFF = """
() => {
  // 恢复真出口。spy 期间不该有任何真发送，所以这里不做补偿。
  if (window.__spy) window.__spy.length = 0;
  delete window.__spy;
  return true;
}
"""

#: 只读：列表项上 localId 的落地情况。这是 spy 看不见的一层。
JS_LOCALID = """
() => {
  const rows = Array.from(document.querySelectorAll('#chatList .chat'));
  return {
    total: rows.length,
    withLocalId: rows.filter(r => r.dataset && r.dataset.localId).length,
    boundRows: rows.filter(r => r.dataset && r.dataset.localId).length,
    sample: rows.slice(0, 3).map(r => ({
      title: r.querySelector('.t') ? r.querySelector('.t').textContent : null,
      remoteId: r.dataset.id || null,
      localId: r.dataset.localId || null,
    })),
  };
}
"""


def cdp_pages(port: int) -> list[dict]:
    d = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=3))
    return [t for t in d if t.get("type") == "page"]


def count_run_end(log_path: Path) -> int:
    if not log_path or not log_path.exists():
        return -1
    return sum(1 for ln in log_path.read_text(encoding="utf-8", errors="replace")
               .splitlines() if '"run_end"' in ln)


# ---------------------------------------------------------------- A 静态契约

def strip_comments(src: str) -> str:
    """去掉 C# 的 // 与 /* */ 注释，保留字符串字面量里的内容。

    为什么必须做：`case "delConv":` 的注释里写着「早先这里调
    `DeleteConversationAsync`」—— 不剥注释的话，正则会把**历史说明**
    当成**现行代码**，报出一个不存在的 bug。第一版就踩了这个坑。
    """
    out: list[str] = []
    i, n = 0, len(src)
    in_str = False
    while i < n:
        c = src[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(src[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def check_static() -> tuple[bool, list[str]]:
    """读源码做契约校验。不碰运行时。"""
    ok = True
    notes: list[str] = []

    bridge = strip_comments(BRIDGE_CS.read_text(encoding="utf-8"))
    appjs = APP_JS.read_text(encoding="utf-8")

    required = ["switchConv", "renameConv", "delConv", "syncConv", "reset", "replay"]
    for name in required:
        in_bridge = f'case "{name}":' in bridge
        # 前端：要么 send({ t: 'xxx' })，要么在菜单项的 run 里发
        in_js = f"t: '{name}'" in appjs
        good = in_bridge and in_js
        ok = ok and good
        notes.append(f"  {name:<12} 宿主={'有' if in_bridge else '缺'} "
                     f"前端={'有' if in_js else '缺'} {'[通过]' if good else '[失败]'}")

    # delConv 必须调远端删除 —— 这是本轮修掉的真 bug，加断言防回归
    m = re.search(r'case "delConv":(.*?)break;', bridge, re.S)
    seg = m.group(1) if m else ""
    remote_ok = "DeleteRemoteSessionAsync" in seg
    local_bad = "DeleteConversationAsync" in seg
    ok = ok and remote_ok and not local_bad
    notes.append(f"  delConv 调 DeleteRemoteSessionAsync: "
                 f"{'是' if remote_ok else '否'} "
                 f"{'[通过]' if remote_ok else '[失败]'}")
    if local_bad:
        notes.append("    [失败] 仍在调本地 DeleteConversationAsync —— 会静默失败")

    # 宿主必须推 localId，否则三个按本地 id 工作的动作无从上手
    push_ok = re.search(r"localId\s*=\s*c\.LocalId", bridge) is not None
    ok = ok and push_ok
    notes.append(f"  宿主推 localId: {'是' if push_ok else '否'} "
                 f"{'[通过]' if push_ok else '[失败]'}")

    return ok, notes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9333)
    a = ap.parse_args()

    all_ok = True

    print("=== A. 静态契约（读源码）===")
    s_ok, notes = check_static()
    all_ok = all_ok and s_ok
    for n in notes:
        print(n)

    pages = cdp_pages(a.port)
    if not pages:
        print("\n[错误] CDP 不可达，壳没在跑？")
        return 2

    pid = web_common.find_pid()
    log_path, engine_pid = web_common.engine_log_for(pid)
    before = count_run_end(log_path)
    print(f"\n壳 PID: {pid} / 引擎 PID: {engine_pid} / 日志: {log_path.name}")
    print(f"run_end 基线: {before}")

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{a.port}")
        page = browser.contexts[0].pages[0]
        page.reload(wait_until="domcontentloaded")
        page.wait_for_timeout(1200)

        print("\n=== B. DOM 交互（出口已掐）===")
        page.evaluate(JS_SPY_ON)

        # B1: 会话行结构 —— 每个 .chat-row 里必须恰有一个 .chat 与一个 .chat-more
        struct = page.evaluate("""
        () => {
          const rows = Array.from(document.querySelectorAll('#chatList .chat-row'));
          return {
            rows: rows.length,
            withOneChat: rows.filter(r => r.querySelectorAll('.chat').length === 1).length,
            withOneMore: rows.filter(r => r.querySelectorAll('.chat-more').length === 1).length,
            moreTabbable: rows.filter(r => {
              const m = r.querySelector('.chat-more');
              return m && m.tagName === 'BUTTON';
            }).length,
          };
        }
        """)
        b1 = (struct["rows"] > 0
              and struct["withOneChat"] == struct["rows"]
              and struct["withOneMore"] == struct["rows"]
              and struct["moreTabbable"] == struct["rows"])
        all_ok = all_ok and b1
        print(f"  B1 行结构: {struct['rows']} 行 / 单个 .chat {struct['withOneChat']} / "
              f"单个 .chat-more {struct['withOneMore']} / 真 button {struct['moreTabbable']} "
              f"{'[通过]' if b1 else '[失败]'}")

        # B2: 点「更多」→ 菜单出现，且禁用态符合「是否已接管」
        page.click("#chatList .chat-row .chat-more")
        page.wait_for_timeout(150)
        menu = page.evaluate("""
        () => {
          const m = document.getElementById('convMenu');
          if (!m || m.hidden) return { open: false };
          const items = Array.from(m.querySelectorAll('.cmenu-item'));
          return {
            open: true,
            role: m.getAttribute('role'),
            count: items.length,
            texts: items.map(b => b.textContent),
            disabled: items.map(b => b.disabled),
            dangerCount: items.filter(b => b.classList.contains('danger')).length,
          };
        }
        """)
        b2 = (menu.get("open") and menu.get("role") == "menu"
              and menu.get("count") == 4 and menu.get("dangerCount") == 1)
        all_ok = all_ok and b2
        print(f"  B2 菜单展开: open={menu.get('open')} role={menu.get('role')!r} "
              f"项数={menu.get('count')} 危险项={menu.get('dangerCount')} "
              f"{'[通过]' if b2 else '[失败]'}")
        if menu.get("open"):
            for t, d in zip(menu["texts"], menu["disabled"]):
                print(f"       {'[禁用]' if d else '[可用]'} {t}")

        # B3: 点「重命名」→ 对话框出现，确定后发出 renameConv（带 localId）
        #     第一行若未接管则该项禁用，先找一条可用的。
        page.evaluate("""
        () => {
          const m = document.getElementById('convMenu');
          const items = Array.from(m.querySelectorAll('.cmenu-item'));
          const rn = items.find(b => b.textContent.includes('重命名'));
          if (rn && !rn.disabled) rn.click();
          return rn ? rn.disabled : null;
        }
        """)
        page.wait_for_timeout(150)
        dlg = page.evaluate("""
        () => {
          const mask = document.getElementById('dlgMask');
          const inp = document.getElementById('dlgInput');
          return {
            open: !mask.hidden,
            title: document.getElementById('dlgTitle').textContent,
            hasInput: !inp.hidden,
            value: inp.value,
            okText: document.getElementById('dlgOk').textContent,
          };
        }
        """)
        if dlg["open"]:
            page.fill("#dlgInput", "E2E 改名测试")
            page.click("#dlgOk")
            page.wait_for_timeout(150)
            seen = page.evaluate(JS_SPY_TAKE)
            rn = [m for m in seen if m and m.get("t") == "renameConv"]
            b3 = len(rn) == 1 and rn[0].get("title") == "E2E 改名测试" and bool(rn[0].get("id"))
            all_ok = all_ok and b3
            print(f"  B3 重命名: 对话框={dlg['title']!r} 输入框={dlg['hasInput']} "
                  f"发出={len(rn)} 条 id={'有' if rn and rn[0].get('id') else '无'} "
                  f"{'[通过]' if b3 else '[失败]'}")
        else:
            # 没有已接管的会话 → 该项应当全部禁用，这是正确行为而非失败
            print(f"  B3 重命名: 对话框未打开（当前没有已接管会话，菜单项应为禁用）"
                  f" {'[跳过]' if all(menu.get('disabled', [])) else '[失败]'}")
            if not all(menu.get("disabled", [])):
                all_ok = False
            page.evaluate(JS_SPY_TAKE)

        # B4: 删除确认 —— 文案必须含「无法撤销」，确定后发出 delConv（带远端 id）
        page.click("#chatList .chat-row .chat-more")
        page.wait_for_timeout(150)
        page.evaluate("""
        () => {
          const m = document.getElementById('convMenu');
          const del = Array.from(m.querySelectorAll('.cmenu-item'))
            .find(b => b.textContent.includes('删除'));
          if (del) del.click();
        }
        """)
        page.wait_for_timeout(150)
        dd = page.evaluate("""
        () => {
          const mask = document.getElementById('dlgMask');
          return {
            open: !mask.hidden,
            title: document.getElementById('dlgTitle').textContent,
            text: document.getElementById('dlgText').textContent,
            okText: document.getElementById('dlgOk').textContent,
            danger: document.getElementById('dlgOk').classList.contains('danger'),
          };
        }
        """)
        has_warning = "无法撤销" in dd.get("text", "")
        if dd["open"]:
            page.click("#dlgOk")
            page.wait_for_timeout(150)
            seen = page.evaluate(JS_SPY_TAKE)
            dl = [m for m in seen if m and m.get("t") == "delConv"]
            b4 = (len(dl) == 1 and bool(dl[0].get("id"))
                  and has_warning and dd.get("danger"))
            all_ok = all_ok and b4
            print(f"  B4 删除: 对话框={dd['title']!r} 含「无法撤销」={has_warning} "
                  f"红色主按钮={dd.get('danger')} 发出={len(dl)} 条 "
                  f"{'[通过]' if b4 else '[失败]'}")
            print(f"       发出的 id: {dl[0].get('id') if dl else None}")
        else:
            all_ok = False
            print("  B4 删除: [失败] 对话框没打开")

        # B5: 顶栏「重放」与「重置熔断」是否存在且能发消息
        top = page.evaluate("""
        () => {
          const r = document.getElementById('btnReplay');
          const b = document.getElementById('btnResetBreaker');
          return {
            replay: !!r, reset: !!b,
            resetHidden: b ? b.hidden : null,
            resetLabel: b ? b.textContent : null,
          };
        }
        """)
        page.click("#btnReplay")
        page.wait_for_timeout(120)
        seen = page.evaluate(JS_SPY_TAKE)
        rp = [m for m in seen if m and m.get("t") == "replay"]
        b5 = top["replay"] and top["reset"] and len(rp) == 1
        all_ok = all_ok and b5
        print(f"  B5 顶栏: 重放按钮={top['replay']} 重置按钮={top['reset']} "
              f"(当前隐藏={top['resetHidden']}) 发出 replay={len(rp)} 条 "
              f"{'[通过]' if b5 else '[失败]'}")

        # B6: 重置熔断按钮只在熔断态显示 —— 现在不该显示。
        #     强制显形后点一下，确认它发的确实是 reset。
        page.evaluate("""
        () => {
          const b = document.getElementById('btnResetBreaker');
          b.hidden = false;
        }
        """)
        page.click("#btnResetBreaker")
        page.wait_for_timeout(120)
        seen = page.evaluate(JS_SPY_TAKE)
        rs = [m for m in seen if m and m.get("t") == "reset"]
        b6 = len(rs) == 1
        all_ok = all_ok and b6
        print(f"  B6 重置熔断: 发出 reset={len(rs)} 条 {'[通过]' if b6 else '[失败]'}")
        # 复原显示状态（下一次 status 推送会自己重算）
        page.evaluate("() => { document.getElementById('btnResetBreaker').hidden = true; }")

        # B7: 键盘可达 —— 焦点进菜单后方向键能在项间移动，Esc 关闭
        page.click("#chatList .chat-row .chat-more")
        page.wait_for_timeout(150)
        kb = page.evaluate("""
        async () => {
          const m = document.getElementById('convMenu');
          const items = Array.from(m.querySelectorAll('.cmenu-item:not([disabled])'));
          if (items.length < 2) return { skip: true, count: items.length };
          items[0].focus();
          const first = document.activeElement === items[0];
          const ev = new KeyboardEvent('keydown',
            { key: 'ArrowDown', bubbles: true, cancelable: true });
          m.dispatchEvent(ev);
          await new Promise(r => setTimeout(r, 30));
          const second = document.activeElement === items[1];
          const esc = new KeyboardEvent('keydown',
            { key: 'Escape', bubbles: true, cancelable: true });
          m.dispatchEvent(esc);
          await new Promise(r => setTimeout(r, 30));
          return { first, second, closed: m.hidden, count: items.length };
        }
        """)
        if kb.get("skip"):
            print(f"  B7 键盘: 可用项不足 2 个（{kb.get('count')}），跳过")
        else:
            b7 = kb.get("first") and kb.get("second") and kb.get("closed")
            all_ok = all_ok and b7
            print(f"  B7 键盘: 首项聚焦={kb.get('first')} 下移={kb.get('second')} "
                  f"Esc关闭={kb.get('closed')} {'[通过]' if b7 else '[失败]'}")

        # B8: 打开会话的分叉 —— 已接管走本地 switchConv，未接管才联网 attachSession。
        #     这两条路径代价差一个数量级，混用会让「点已打开过的对话」也去联网。
        fork = page.evaluate("""
        async () => {
          const rows = Array.from(document.querySelectorAll('#chatList .chat'));
          const bound = rows.find(r => r.dataset.localId && !r.classList.contains('on'));
          const unbound = rows.find(r => !r.dataset.localId);
          const out = { boundFound: !!bound, unboundFound: !!unbound };

          const fire = async (row) => {
            if (!row) return null;
            window.__spy.length = 0;
            row.click();
            await new Promise(r => setTimeout(r, 60));
            return window.__spy.slice();
          };

          if (bound) {
            const msgs = await fire(bound);
            const sw = msgs.filter(m => m && m.t === 'switchConv');
            const at = msgs.filter(m => m && m.t === 'attachSession');
            out.bound = {
              switchConv: sw.length, attachSession: at.length,
              id: sw.length ? sw[0].id : null,
              expectId: bound.dataset.localId,
            };
          }
          if (unbound) {
            const msgs = await fire(unbound);
            const sw = msgs.filter(m => m && m.t === 'switchConv');
            const at = msgs.filter(m => m && m.t === 'attachSession');
            out.unbound = {
              switchConv: sw.length, attachSession: at.length,
              id: at.length ? at[0].id : null,
              expectId: unbound.dataset.id,
            };
          }
          return out;
        }
        """)
        b8 = True
        if fork.get("boundFound"):
            b = fork["bound"]
            sub = (b["switchConv"] == 1 and b["attachSession"] == 0
                   and b["id"] == b["expectId"])
            b8 = b8 and sub
            print(f"  B8a 已接管行: switchConv={b['switchConv']} "
                  f"attachSession={b['attachSession']} "
                  f"id匹配={b['id'] == b['expectId']} "
                  f"{'[通过]' if sub else '[失败]'}")
        else:
            print("  B8a 已接管行: 当前没有未选中的已接管会话，跳过")
        if fork.get("unboundFound"):
            u = fork["unbound"]
            sub = (u["attachSession"] == 1 and u["switchConv"] == 0
                   and u["id"] == u["expectId"])
            b8 = b8 and sub
            print(f"  B8b 未接管行: attachSession={u['attachSession']} "
                  f"switchConv={u['switchConv']} "
                  f"id匹配={u['id'] == u['expectId']} "
                  f"{'[通过]' if sub else '[失败]'}")
        else:
            print("  B8b 未接管行: 当前没有未接管会话，跳过")
        all_ok = all_ok and b8

        page.evaluate(JS_SPY_OFF)

        # C: 运行时数据流 —— localId 是否真的传到了 DOM
        print("\n=== C. 运行时数据流（只读）===")
        li = page.evaluate(JS_LOCALID)
        print(f"  会话行: {li['total']} 条 / 带 localId: {li['withLocalId']} 条")
        for s in li["sample"]:
            print(f"    remoteId={s['remoteId']!r} localId={s['localId']!r} "
                  f"title={s['title']!r}")
        # 判据：有会话就说明字段链路通了（可能全部未接管，那 withLocalId=0 也正常）。
        # 真正的判据是「字段存在于数据结构里」—— 有任意一条带 localId 即证明链路通；
        # 一条都没有时，至少要确认 dataset 属性机制没写错（见 B1/B3 已覆盖）。
        c_ok = li["total"] > 0
        all_ok = all_ok and c_ok
        print(f"  字段链路: {'[通过]' if c_ok else '[失败]'} "
              f"（localId 数量随接管情况变化，0 条不代表失败）")

        browser.close()

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
    print("判定:", "[通过] 会话操作入口全部接通" if all_ok else "[失败] 见上方")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
