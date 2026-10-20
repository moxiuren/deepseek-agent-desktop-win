#!/usr/bin/env python3
"""旅程 8：断网 / 熔断 → 降级行为。

## 这条旅程要回答的问题

前 7 条旅程验的都是「一切正常时能不能用」。但用户真正会骂人的时刻是**出故障时**：

  · 断网了，界面说了什么？还是那个绿色的「运行正常」吗？
  · 熔断了，我还能不能点发送？点了会怎样？
  · 账号被处置了，我看得出来吗？

`app.js:onStatus()` 里有一条六档优先级链（停机 > 熔断 > 链路不可用 > 账号被处置 >
未登录 > 闸门），注释写着「与 WPF 壳的 HealthPill 逐条对齐」。**这条链从来没有被
端到端验证过** —— 单测覆盖的是引擎侧的 `CircuitBreaker` 状态机（`test_gates.py`），
不是前端的呈现。

## 为什么用 CDP 注入而不是真断网

真断网有两个问题：

  1. **会污染风控计数。** 引擎把网络异常计进 `note_rpc_error`，连续 3 次就标记
     降级到 T1 通道。为了测一条前端文案去动真实账号的熔断计数，代价不对等。
  2. **不可复现。** 拔网线/改 hosts 的状态组合太多（DNS 失败、TCP 超时、TLS 失败、
     连接被重置），每种走的分支不同，跑一次只能覆盖一种。

所以这里从**页面侧**注入 `status` 消息：

    Bridge.emit({ t: 'status', running: false, s: {...} })

这与宿主 `WebBridge.SendStatus()` 推下来的消息**走完全相同的代码路径**
（`bridge.js` 的 `emit()` → `handlers` → `onStatus()`）。所以量到的是真实呈现逻辑，
不是模拟。这和 `probe_webview2_bulk.py` 用 `Bridge.emit({t:'item'})` 验证渲染是同一个道理。

## 判据

对每一档状态，检查三件事：

  1. **胶囊文案** —— 用户第一眼看到的那句话对不对
  2. **胶囊配色** —— `bad`（红）/ `warn`（琥珀）/ `ok`（绿）/ `idle`（灰）
  3. **发送是否被拦** —— 熔断/停机时点发送，前端必须拦住而不是发出去

第 3 条是这条旅程的核心：**文案对了但按钮还能点，等于没做降级。**

用法：
    python tools/e2e/journey8_degrade_web.py
    python tools/e2e/journey8_degrade_web.py --port 9333
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from playwright.sync_api import sync_playwright  # noqa: E402

#: 正常态的基线。每一档只改它关心的那几个字段，其余保持「一切正常」——
#: 否则测不出优先级（例如「既熔断又未登录」到底显示哪个）。
BASE = {
    "started": True,
    "mode": "rpc",
    "transport": "rpc",
    "transportReady": True,
    "circuitLevel": "closed",
    "circuitReason": "正常",
    "lastSignal": "",
    "degrade": "none",
    "hasToken": True,
    "identityLabel": "测试账号",
    "identityRefreshing": False,
    "accountSuspended": False,
    "suspendedDetail": "",
    "model": "deepseek-v4",
    "gateAllowed": True,
    "gateReason": "",
    "gateWaitMs": 0,
    "busyStreak": 0,
    "sessionCid": "cid-test",
    "sessionTurns": 0,
    "sessionMaxTurns": 50,
    "sessionTemporary": False,
    "convTitle": "旅程 8 降级验证",
    "convTurns": 0,
    "memoryChars": 0,
    "memoryEnabled": False,
    "thinking": False,
    "search": False,
    "showThinking": True,
    "thinkingMode": "标准",
}

#: 每一档：期望文案 / 期望配色 / 发送是否应被拦。
#:
#: 顺序有意义 —— 它同时是优先级链的验证顺序。`onStatus` 用 if/else if 串起来，
#: 所以「靠前的条件命中后，后面的不再判」。下面的用例刻意构造了重叠状态
#: （例如 halted 同时 accountSuspended），验证的正是「靠前的赢」。
CASES = [
    {
        "name": "引擎启动中",
        "patch": {"started": False},
        "expect_label": "引擎启动中…",
        "expect_tone": "idle",
        "expect_blocked": False,   # 启动中不拦发送，只是没就绪
    },
    {
        "name": "已停机（同时账号被处置 —— 验优先级）",
        "patch": {"circuitLevel": "halted", "circuitReason": "连续 3 次认证失败",
                  "accountSuspended": True, "gateAllowed": False, "gateReason": "L4 硬拒绝"},
        "expect_label": "已停机 · 需人工介入",
        "expect_tone": "bad",
        "expect_blocked": True,
    },
    {
        "name": "熔断中",
        "patch": {"circuitLevel": "open", "circuitReason": "服务端拒绝生成",
                  "gateAllowed": False, "gateReason": "[L4] 退避 30 分钟"},
        "expect_label": "熔断中",
        "expect_tone": "bad",
        "expect_blocked": True,
    },
    {
        "name": "链路不可用（断网）",
        "patch": {"transportReady": False, "transport": "不可用"},
        "expect_label": "链路不可用",
        "expect_tone": "bad",
        "expect_blocked": False,   # 断网不拦 —— 引擎会自己报错，前端不该假装知道
    },
    {
        "name": "账号被处置",
        "patch": {"accountSuspended": True, "suspendedDetail": "账号状态异常"},
        "expect_label": "账号被处置",
        "expect_tone": "bad",
        "expect_blocked": False,
    },
    {
        "name": "未登录",
        "patch": {"hasToken": False},
        "expect_label": "未登录",
        "expect_tone": "warn",
        "expect_blocked": False,
    },
    {
        "name": "闸门等待 12.3s",
        "patch": {"gateAllowed": False, "gateReason": "[L2] 节奏限制", "gateWaitMs": 12300},
        "expect_label": "闸门等待 12.3s",
        "expect_tone": "warn",
        "expect_blocked": False,
    },
    {
        "name": "服务端忙 2/3",
        "patch": {"busyStreak": 2},
        "expect_label": "服务端忙 2/3",
        "expect_tone": "warn",
        "expect_blocked": False,
    },
    {
        "name": "运行中",
        "patch": {},
        "running": True,
        "expect_label": "运行中",
        "expect_tone": "ok",
        "expect_blocked": False,
    },
    {
        "name": "运行正常（基线）",
        "patch": {},
        "expect_label": "运行正常",
        "expect_tone": "ok",
        "expect_blocked": False,
    },
]

#: 注入一档状态并读回呈现结果。
#:
#: `Bridge.emit` 是 `bridge.js` 暴露的公开方法，宿主推的每条消息都走它，
#: 所以这里注入与真实推送**走完全相同的代码路径**。
JS_PROBE = """
async ([s, running]) => {
  window.Bridge.emit({ t: 'status', running, s });

  // 让 onStatus 里的同步逻辑跑完。它没有异步分支，一帧足够；
  // 两帧是为了避开「同一帧内读到旧样式」的边界。
  await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));

  const pill = document.getElementById('pill');
  const txt = document.getElementById('pillText');
  const btnRun = document.getElementById('btnRun');
  const input = document.getElementById('input');

  // 读实际生效的配色类，而不是读我们以为设的那个 ——
  // `pill.className = 'pill'` 先清空再加，若逻辑写错会只剩 'pill'。
  const cls = [...pill.classList];

  // 发送按钮是否可点。`hidden` 与 `disabled` 是两回事：
  // 运行中按钮整个隐藏（换成「停止」），故障时则应该保持可见但禁用。
  const sendState = {
    runHidden: btnRun.hidden,
    runDisabled: btnRun.disabled,
    inputDisabled: input ? input.disabled : null,
  };

  return {
    label: txt.textContent,
    tone: cls.filter(c => ['ok', 'warn', 'bad', 'idle', 'run'].includes(c)),
    rawClass: pill.className,
    sendState,
    // 状态胶囊的 title / aria 有没有把原因带出来
    pillTitle: pill.getAttribute('title') || '',
  };
}
"""

#: 真的点一次发送，看前端怎么处理。
#:
#: ## 这个探针必须先把出口掐死（2026-10-18 两次真实误发事故后的强制要求）
#:
#: 初版直接点按钮，然后用「对话流节点数有没有增长」当作「有没有发出去」。
#: **这是错的，而且造成了真实事故**：CDP 注入的 `status` 只改了前端的
#: `status` 变量，宿主的真实状态仍是 `closed` / `gateAllowed=true`。于是
#: `submit` 一路送到 C#，C# 按真实状态放行，引擎真的向 DeepSeek 发了请求。
#:
#: 引擎日志留痕（同一个错误犯了两次）：
#:
#:   · `goal: "旅程8 降级探测"` 的 `run_end` **3 次**（20:51 / 20:53 ×2）
#:   · `goal: "宿主门禁验证（不应出网）"` 的 `run_end` **1 次**（20:59）
#:
#: 第二次尤其不可原谅 —— 第一条教训已经写进注释了，却又用「验证宿主侧逻辑」
#: 的名义点了一次按钮。**结论必须记住：CDP 注入永远无法验证宿主侧行为。**
#: 宿主读的是引擎推来的 `status`，注入改不到它。想验宿主逻辑，只能
#: 单元测试或直接构造 `HostStatus`，不能靠点按钮。
#:
#: 两处教训：
#:
#:   1. **判据错**。`grew` 量的是「宿主有没有往对话流加节点」，不是
#:      「前端有没有发出 submit」。宿主拒绝提交时也会加一条提示节点，
#:      所以 `grew > 0` 既可能是发出去、也可能是被拒绝 —— 无法区分。
#:   2. **出口没掐**。前端只要没拦住，消息就真的出网了。
#:
#: 现在的做法：**先把 `Bridge.send` 换成只记录不转发的 spy**，再点按钮。
#: 无论前端拦不拦，消息都不可能到达 C#，不可能产生真实请求。
#: 于是量到的是纯粹的**前端行为**，与宿主、引擎、网络全部隔离。
#:
#: ## 三重护栏（防的是「后来的我又忘了」）
#:
#:   1. 出口换成 spy —— 拦在 JS 层
#:   2. `Bridge.send` 若是只读/不可覆写，**直接抛错中止**，绝不退化成真发送
#:   3. 探针前后各读一次引擎日志 `run_end` 计数，有任何增长就报红
JS_TRY_SEND = """
async () => {
  // 护栏 1 + 2：先把出口换成 spy。赋不上就立刻中止 ——
  // 宁可这条判据「测不了」，也不能在没掐住出口的情况下点按钮。
  const realSend = window.Bridge.send;
  const seen = [];
  window.Bridge.send = (m) => { seen.push(m); };
  if (window.Bridge.send === realSend) {
    throw new Error('出口掐不住（Bridge.send 不可覆写），拒绝执行发送探测');
  }

  const input = document.getElementById('input');
  const btn = document.getElementById('btnSend') || document.getElementById('btnRun');
  const beforeValue = input ? input.value : '';

  if (input) { input.value = '旅程8 降级探测'; input.dispatchEvent(new Event('input')); }
  if (btn) btn.click();
  await new Promise(r => setTimeout(r, 200));

  const submits = seen.filter(m => m && m.t === 'submit');
  const stillThere = input ? input.value : null;

  // 还原出口，并清掉我们塞进去的草稿
  window.Bridge.send = realSend;
  if (input) { input.value = beforeValue; input.dispatchEvent(new Event('input')); }

  return {
    // 前端有没有把 submit 交给宿主（0 = 界面层就拦住了）
    submitCount: submits.length,
    // 输入框内容是否被保留（用户的字没被吃掉）
    stillThere,
    inputConsumed: stillThere !== '旅程8 降级探测',
    sawTypes: seen.map(m => m && m.t),
  };
}
"""


def cdp_ok(port: int) -> tuple[bool, str]:
    try:
        d = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=3))
        pages = [t for t in d if t.get("type") == "page"]
        return (bool(pages), pages[0]["url"] if pages else "")
    except Exception as e:  # noqa: BLE001
        return (False, str(e))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9333)
    a = ap.parse_args()

    ok, url = cdp_ok(a.port)
    if not ok:
        print(f"[错误] CDP 不可达：{url}")
        print("启动 Web 壳前设 WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS="
              "--remote-debugging-port=9333")
        return 2
    print(f"接管: {url}")

    # 护栏 3：探针前后各读一次引擎日志，任何 run_end 增长都说明出口没掐住。
    # 这是「事后可证伪」的一道保险 —— 前两道都在 JS 里，这道在进程外。
    import web_common as wc
    pid = wc.find_pid()
    mark = wc.log_offset_for(pid) if pid else None

    results = []
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{a.port}")
        page = browser.contexts[0].pages[0]

        for case in CASES:
            s = dict(BASE)
            s.update(case["patch"])
            running = case.get("running", False)

            got = page.evaluate(JS_PROBE, [s, running])

            label_ok = got["label"] == case["expect_label"]
            tone_ok = case["expect_tone"] in got["tone"]

            # 发送行为：只有需要验的档位才点，避免无谓地搅动界面。
            #
            # 判据是 `inputConsumed`（输入框被清空 = 被当成已发出），
            # **不是** `submitCount === 0`。这是 2026-10-18 修 `doSend()` 时
            # 定下的语义：硬性阻断时照常提交（宿主有通道给出可见原因），
            # 只是不吃掉用户已经打好的字。所以「提交了 + 输入框还在」是正确，
            # 「提交了 + 输入框空了」才是缺陷。
            blocked = None
            send_info = None
            if case["expect_blocked"]:
                send_info = page.evaluate(JS_TRY_SEND)
                blocked = send_info["inputConsumed"] is False

            results.append({
                "name": case["name"],
                "label": got["label"],
                "expect_label": case["expect_label"],
                "label_ok": label_ok,
                "tone": got["tone"],
                "expect_tone": case["expect_tone"],
                "tone_ok": tone_ok,
                "blocked": blocked,
                "expect_blocked": case["expect_blocked"],
                "send": send_info,
            })

        # 复位成正常态，别把界面留在熔断状态
        page.evaluate(JS_PROBE, [dict(BASE), False])
        browser.close()

    # 护栏 3 的判定：引擎日志里有没有新的 run_end
    leaked = 0
    if mark is not None:
        import time as _t
        _t.sleep(1)
        ev = wc.read_new_events(mark)
        leaked = sum(1 for e in ev
                     if 'run_end' in str(e) or 'run.completed' in str(e))
        if leaked:
            print(f"\n[错误] 探针期间引擎跑了 {leaked} 轮 —— 出口没掐住，"
                  f"消息真的出网了。这是严重缺陷，不是测试通过。")

    # ---- 输出 ----
    print()
    print(f"{'档位':<28}{'胶囊文案':<22}{'配色':<10}{'拦截':<8}判定")
    print("-" * 84)
    all_ok = True
    for r in results:
        tone_s = "/".join(r["tone"]) or "(无)"
        if r["expect_blocked"]:
            blk = "拦住" if r["blocked"] else "放行"
            blk_ok = r["blocked"] is True
        else:
            blk = "—"
            blk_ok = True
        good = r["label_ok"] and r["tone_ok"] and blk_ok
        all_ok = all_ok and good
        mark = "[通过]" if good else "[失败]"
        print(f"{r['name']:<28}{r['label']:<22}{tone_s:<10}{blk:<8}{mark}")

    # 失败详情
    bad = [r for r in results
           if not (r["label_ok"] and r["tone_ok"]
                   and (r["blocked"] is True if r["expect_blocked"] else True))]
    if bad:
        print("\n失败详情：")
        for r in bad:
            if not r["label_ok"]:
                print(f"  {r['name']}: 文案 {r['label']!r} != 期望 {r['expect_label']!r}")
            if not r["tone_ok"]:
                print(f"  {r['name']}: 配色 {r['tone']} 不含 {r['expect_tone']!r}")
            if r["expect_blocked"] and r["blocked"] is not True:
                print(f"  {r['name']}: 输入框被消费（用户打的字被吃掉）—— {r['send']}")

    print()
    print(f"判定: {'通过' if all_ok else '未通过'} "
          f"({sum(1 for r in results if r['label_ok'] and r['tone_ok'])}/{len(results)} 档文案+配色正确)")
    print(f"出口护栏: {'[通过] 引擎零新增轮次' if not leaked else f'[错误] 泄漏 {leaked} 轮'}")
    all_ok = all_ok and not leaked

    # 附：真实网络错误时的引擎侧行为（只读日志，不主动制造故障）
    print()
    print("引擎侧历史故障信号（只读日志，未主动制造）：")
    print("  说明：本脚本不真断网 —— 会污染风控计数。真实网络异常的引擎侧处理")
    print("        由 engine/tests/test_gates.py 覆盖（状态机层），")
    print("        前端呈现由本脚本覆盖（页面层）。两层合起来才是完整的降级链路。")

    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
