#!/usr/bin/env python3
"""旅程 7：超长对话 —— Web 壳前端的真实渲染成本与首屏延迟。

## 为什么这条旅程不能沿用 mock 的 `bulk` 场景

`mock.js` 的 `bulk` 是 **600 条短消息**（每条约 20 字）。它证明的是
「600 个节点能铺下去」，证明不了用户真正的负载形状：

    本机最大的真实对话  20261015-232516-4504.json
      724 条消息 / 1624 KB / 重放行数 739 / 正文合计 107 万字
      单条最长 40887 字

WPF 侧的 `--replaycheck` 在注释里为此专门挨过一次打：「拿探针 ⑤ 的合成数据
外推（21 万字）到真实样本（82 万字），外推出来的数字没有任何意义」。
Web 壳这次不重犯 —— 数据一律取自真实对话文件。

## 为什么在真实 Chrome 里跑，而不是在 WebView2 里

WebView2 里没有可编程的测量入口：它的 DevTools 协议端口默认不开
（`MainWindow.xaml.cs` 只设了 `userDataFolder` 与虚拟主机映射，没有
`--remote-debugging-port`），而 `Bridge.kind` 一旦是 `webview2`，前端就不走
mock、必须等宿主推 `hist` —— 从外部无从注入。

所以改用**同一份前端资源 + 同一份真实数据**，在 Chromium 内核里直接驱动。
这不是「模拟」：`index.html` / `app.js` / `app.css` 是产品代码本身，
`window.Bridge.emit({t:'hist',...})` 就是 C# `SendHistory()` 推下来的那条消息，
载荷按 `WebBridge.Item()` 的契约逐字段构造。

**必须声明的边界**：Chrome 与 WebView2 共用 Chromium 渲染引擎，布局 / 样式 /
`content-visibility` 的行为一致；但**合成器与绘制层**可能有差异（WebView2
嵌在 WPF 窗口里，Chrome 是独立窗口）。本脚本量的是**主线程上的 DOM 与布局
成本**，这一层两者同构。真实滚动手感需在真窗口里人工确认。

## 判据为什么是这几条

用户按下「切对话」之后，只有一件事能被他感知：**多久看到内容**。
`AgentHost.ReplayConversation` 的设计是「首批同步铺，余下按 40000 字 / 50 块
分批补」，所以必须分开量：

  1. **首屏** —— 从 `hist` 消息进来到中栏真的有内容、且排完一次版。
     这是用户等的那一段，判据钉 600 ms（与 WPF 侧 `--replaycheck` 同一个预算）
  2. **铺完总计** —— 全量铺完要多久。它长一点可以接受（用户在读第一屏），
     但超过 10 秒就说明分页的固定开销失控
  3. **裁切行为** —— 739 行 > `MaxTranscript = 600`，**头部必然被裁**。
     这不是 bug，是设计；但必须确认裁的是「最早的」，不是随机裁
  4. **滚动到底是否贴底** —— 用户切过去应该看到最新一条，不是最老的

用法：
    python tools/e2e/journey7_longconv_web.py
    python tools/e2e/journey7_longconv_web.py --headed     # 看得见窗口
    python tools/e2e/journey7_longconv_web.py --file <路径> # 指定对话文件
"""
from __future__ import annotations

import functools
import http.server
import json
import os
import socket
import statistics
import sys
import threading
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parents[2] / "shell" / "DeepSeekAgent.Web" / "Assets" / "web"
CONV_DIR = Path(os.environ["LOCALAPPDATA"]) / "DeepSeek-Agent" / "conversations"

#: 与 `AgentHost.ReplayCharBudget` / `ReplayBatchSize` 保持一致。
#: 这里**重新实现**分页切批（而不是调用 C#），因为本脚本跑在浏览器侧；
#: 若哪天产品改了这两个常量，本脚本会与真实行为脱节 —— 所以下面有一道
#: 自检：把切批结果和真实样本的期望批数对上（见 `check_batch_math`）。
REPLAY_CHAR_BUDGET = 40000
REPLAY_BATCH_SIZE = 50
MAX_TRANSCRIPT = 600

BUDGET_FIRST_MS = 600.0
BUDGET_TOTAL_MS = 10000.0


# ---------------------------------------------------------------- 数据准备

def pick_file() -> Path:
    best, best_size = None, 0
    for p in CONV_DIR.glob("*.json"):
        if p.name == "index.json":
            continue
        n = p.stat().st_size
        if n > best_size:
            best, best_size = p, n
    if best is None:
        raise SystemExit(f"对话目录里没有 .json：{CONV_DIR}")
    return best


def build_rows(conv: dict) -> list[dict]:
    """按 `AgentHost.ReplayConversation` 的规则把 messages 拍平成「重放行」。

    **这一层是 724 行，不是 739 行** —— 切批在这一层做。对应关系：

        role=user      → Goal
        role=assistant → Assistant（Thoughts 挂在同一个 row 上，不拆）
        role=error     → Error
        其余           → Notice
    text 为空的消息直接跳过（产品侧 `if (text.Length == 0) continue`）。

    每行的 `chars` = Text.Length + Thoughts.Length —— 这是 `ReplayBatchEnd`
    累加的那个数，也是我第一版搞错的地方。
    """
    rows: list[dict] = []
    for m in conv.get("messages") or []:
        text = m.get("text") or ""
        if not text:
            continue
        role = m.get("role") or ""
        thoughts = (m.get("thoughts") or "") if role == "assistant" else ""
        kind = {"user": "Goal", "assistant": "Assistant", "error": "Error"}.get(role, "Notice")
        rows.append({"kind": kind, "text": text, "thoughts": thoughts,
                     "chars": len(text) + len(thoughts)})
    return rows


def rows_to_items(rows: list[dict]) -> list[dict]:
    """把「重放行」展开成前端真正收到的 TranscriptItem 序列。

    对应 `AgentHost.AddReplayRow`：assistant 行若带 thoughts，
    **先**发一条 Thought，再发 Assistant。其余一行一条。
    """
    out: list[dict] = []
    seq = 0
    for r in rows:
        th = r["thoughts"]
        if r["kind"] == "Assistant" and th.strip():
            seq += 1
            out.append({
                "seq": seq, "kind": "Thought", "text": th, "detail": "",
                "tool": None,
                # 重放的思考块一律折叠（`AddReplayRow` 里 `Streaming = false`）
                "thought": {
                    "streaming": False, "chars": len(th), "waited": 0,
                    "header": f"思考过程（{len(th)} 字）· 点这里展开",
                },
            })
        seq += 1
        out.append({"seq": seq, "kind": r["kind"], "text": r["text"], "detail": "",
                    "tool": None, "thought": None})
    return out


def batch_ends(chars: list[int]) -> list[int]:
    """逐字复刻 `AgentHost.ReplayBatchEnd`（`AgentHost.cs:1779`）。

    真实实现（注意判定顺序，第一版就是在这里错的）：

        take = end - next;
        if (take >= batchSize) break;
        if (take > 0 && used + chars[end] > charBudget) break;   // 先判再加
        used += chars[end];
        end++;

    **`take > 0` 是「至少收一条」**：一条 4 万字的消息一进来就超预算，
    若不无条件收下它，游标原地不动 → `PumpReplay` 的循环体一次都不跑 →
    `BeginInvoke` 无限自我重排，UI 线程被空转任务占满。

    **判定顺序决定批次边界**：必须是「加进去会超就停」而不是「加进去
    之后发现超了就停」—— 后者每批多收一条。第一版写成后者，同一份样本
    切出 26 批，而 WPF 实测是 30 批。
    """
    ends, start, n = [], 0, len(chars)
    while start < n:
        end, used = start, 0
        while end < n:
            take = end - start
            if take >= REPLAY_BATCH_SIZE:
                break
            if take > 0 and used + chars[end] > REPLAY_CHAR_BUDGET:
                break
            used += chars[end]
            end += 1
        ends.append(end)
        start = end
    return ends


def check_batch_math(rows: list[dict]) -> list[int]:
    """把本脚本的切批与 WPF 侧实测对一下，防止常量漂移。

    ## 三个数字必须一一对上，否则说明我理解的模型是错的

    `--replaycheck` 对同一份样本（2026-10-18 20:0x 用当前构建重跑）报：

        首批（同步段）· 7 条
        补批次数 · 29 次

    我的复刻切出的是「首批 6 行 · 总 31 批」。两个数字看似差 1，
    其实是**三处各自的 ±1 抵消后的结果**，必须逐条拆开：

      · 「首批 7 条」= 6 行 + **1 条 Info 头**。
        `ReplayConversation` 在拍平**之前**先
        `Add(TranscriptKind.Info, $"对话「{title}」")`，而门禁读的是
        `host.Transcript.Count`（含那条头）。切批只认 messages，不含头。
      · 「补批 29 次」= 总 31 批 − 首批 1 − **末批 1**。
        `PumpReplay` 铺完最后一批时 `FinishReplay` 后直接 `return`，
        **不触发** `ReplayBatchPumped` —— 所以计数器永远少 1。

    第一版按「首批 7 行 · 总 30 批」去比，于是永远对不上，
    还一度怀疑是切批公式错了。**基准读数必须拆到它真正的来源**。

    返回**行级**的批次边界（[6, ...]），调用方再折算成 item 索引。
    """
    ends = batch_ends([r["chars"] for r in rows])
    first, total = ends[0], len(ends)
    # 折算成门禁会报出来的那两个数
    gate_first = first + 1          # + Info 头
    gate_pumped = total - 2         # - 首批 - 末批（末批不触发事件）
    ok = gate_first == 7 and gate_pumped == 29
    print(f"  切批自检：{len(rows)} 行 · 首批 {first} 行 · 总 {total} 批"
          f"  →  门禁口径 首批 {gate_first} 条 / 补批 {gate_pumped} 次"
          f"（实测 7 / 29）{'  [对齐]' if ok else '  [警告]'}")
    if not ok:
        print("  [警告] 与 WPF 侧实测不一致 —— 产品常量或拍平规则可能变了，"
              "本脚本的结论不再代表真实行为")
    return ends


def rows_to_item_bounds(rows: list[dict], row_ends: list[int]) -> list[int]:
    """把「行边界」折算成「item 索引边界」。

    一行展开成 1 或 2 个 item（带 thoughts 的 assistant 是 2 个），
    所以第 k 批结束于「前 row_ends[k] 行一共产出多少个 item」。

    再 +1 是那条 Info 标题行：它由 `ReplayConversation` 在拍平之前
    就 `Add` 进去，因此始终排在第一个 item 位置、且**不参与切批**。
    """
    out: list[int] = []
    for re_ in row_ends:
        out.append(1 + sum(2 if (r["kind"] == "Assistant" and r["thoughts"].strip()) else 1
                           for r in rows[:re_]))
    return out


def header_item(conv: dict) -> dict:
    """`ReplayConversation` 铺的第一条：`Info · 对话「<标题>」`。

    它不是装饰 —— 它占一个 DOM 节点、占一行高度，也计入
    `Transcript.Count`。门禁报的「首批 7 条」里就有它。
    """
    return {"seq": 0, "kind": "Info", "text": f"对话「{conv.get('title') or '新对话'}」",
            "detail": "", "tool": None, "thought": None}


# ---------------------------------------------------------------- 浏览器侧

JS_RENDER = r"""
async ([items, plan, maxTranscript]) => {
  const t0 = performance.now();
  const Bridge = window.Bridge;
  const inner = document.getElementById('inner');
  const stream = document.getElementById('stream');

  // 与产品一致：先 clear，再逐条 item 推下去。
  Bridge.emit({ t: 'clear' });
  Bridge.emit({ t: 'status', running: false, s: {} });

  // 宿主侧 `TrimTranscript()` 在超过 `maxTranscript` 时逐条裁掉最旧的，
  // 并广播 `{t:'rm', seq}`。这里必须**照做**，否则量的就不是产品的行为。
  //
  // 这一条以前是漏的：脚本只发 item，于是 DOM 涨到 740 个而判据报
  // 「超上限」—— 产品那边其实早就在裁数据了，只是没通知前端。
  // 判据要能分辨「宿主没裁」与「裁了但没通知前端」这两种完全不同的缺陷。
  const emitted = [];
  const pushItem = (it) => {
    Bridge.emit({ t: 'item', item: it });
    emitted.push(it);
    while (emitted.length > maxTranscript) {
      const old = emitted.shift();
      Bridge.emit({ t: 'rm', seq: old.seq });
    }
  };

  const n = items.length;

  // ---- 首批：同步推完 ----
  const firstEnd = plan[0];
  for (let i = 0; i < firstEnd; i++) pushItem(items[i]);
  const tFirstEmit = performance.now();

  // 等两帧，让样式与布局真的算完（content-visibility:auto 下首屏才定型）
  await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
  const tFirstPaint = performance.now();

  // ---- 余下：按批补，每批之间让出一帧 ----
  let cursor = firstEnd, batches = 1;
  for (let b = 1; b < plan.length; b++) {
    const end = plan[b];
    for (let i = cursor; i < end; i++) pushItem(items[i]);
    cursor = end; batches++;
    // 让渲染帧跑完再补下一批 —— 对应 C# 的 DispatcherPriority.Background
    await new Promise(r => requestAnimationFrame(r));
  }
  await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
  const tDone = performance.now();

  const nodes = inner.children.length;
  // 裁切：产品侧 TrimTranscript 保证 Transcript.Count <= 600。
  // 前端是纯 Append（onItem 只 push + appendChild），所以超长对话下
  // DOM 会**全部**保留 —— 这一条正是本旅程要量的东西。
  const firstText = inner.firstElementChild ? inner.firstElementChild.textContent.slice(0, 40) : '';
  const lastText = inner.lastElementChild ? inner.lastElementChild.textContent.slice(0, 40) : '';
  const scrollH = stream.scrollHeight;
  const scrollTop = stream.scrollTop;

  return {
    totalItems: n,
    firstBatch: firstEnd,
    batches: batches,
    firstEmitMs: tFirstEmit - t0,
    firstPaintMs: tFirstPaint - t0,
    totalMs: tDone - t0,
    domNodes: nodes,
    firstText, lastText,
    scrollHeight: scrollH,
    scrollTop: scrollTop,
    maxTranscript,
  };
}
"""

JS_SCROLL = r"""
async () => {
  const stream = document.getElementById('stream');
  const t0 = performance.now();
  stream.scrollTop = stream.scrollHeight;
  await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
  const t1 = performance.now();
  return {
    scrollTop: stream.scrollTop,
    scrollHeight: stream.scrollHeight,
    clientHeight: stream.clientHeight,
    ms: t1 - t0,
  };
}
"""

#: 逐帧滚动采样 —— 这才是用户真正的体感。
#:
#: 为什么不能只看「滚到底要多久」：那一次是**单步跳转**，浏览器可以直接
#: 跳到目标偏移、只排一次版。真实用户是一格一格滚轮往回翻，每翻一屏
#: 都要重新布局一批新进入视口的节点。`content-visibility:auto` 的收益
#: 恰恰在这一层 —— 屏外节点跳过布局，但**滚动时它们会陆续进场**，
#: 而 `contain-intrinsic-size:auto 110px` 的高度是**估算值**，
#: 估算偏差会导致滚动条跳动（设计稿实测 93894 → 84228，约 10%）。
#:
#: 采样方式：用 rAF 连续记录帧间隔，同时每帧把 scrollTop 往回推一屏。
#: 一帧超过 16.7ms 就算掉帧；超过 50ms 用户能明确感到卡顿。
JS_SCROLL_SWEEP = r"""
async (frames) => {
  const stream = document.getElementById('stream');
  stream.scrollTop = stream.scrollHeight;
  await new Promise(r => requestAnimationFrame(r));

  const gaps = [];
  const tops = [];
  let last = performance.now();
  const step = Math.max(200, stream.clientHeight * 0.8);

  for (let i = 0; i < frames; i++) {
    await new Promise(r => requestAnimationFrame(r));
    const now = performance.now();
    gaps.push(now - last);
    last = now;
    // 往回翻（用户看历史的方向）
    stream.scrollTop = Math.max(0, stream.scrollTop - step);
    tops.push(stream.scrollTop);
  }

  gaps.sort((a, b) => a - b);
  const p = q => gaps[Math.min(gaps.length - 1, Math.floor(gaps.length * q))];
  // 高度估算偏差：滚动过程中 scrollHeight 会不会跳变
  return {
    frames: gaps.length,
    medMs: p(0.5),
    p95Ms: p(0.95),
    maxMs: gaps[gaps.length - 1],
    over16: gaps.filter(g => g > 16.7).length,
    over50: gaps.filter(g => g > 50).length,
    finalTop: stream.scrollTop,
    scrollHeight: stream.scrollHeight,
    clientHeight: stream.clientHeight,
  };
}
"""

#: 只切 CV 规则，不重铺 DOM —— 用于「同一份已铺好的内容」上做滚动对照。
#:
#: 必须能独立于 `JS_RENDER_MODE` 调用：`JS_RENDER_MODE` 会 clear + 重铺，
#: 而滚动性能要量的正是「内容已就位后，翻历史时每帧的布局成本」。
JS_SET_CV = r"""
(mode) => {
  const marker = '--gad-cv-override';
  let sh = null;
  for (const s of document.styleSheets) {
    try { if (s.href && s.href.includes('app.css')) { sh = s; break; } } catch (e) {}
  }
  if (!sh) return { ok: false, why: 'app.css not found' };
  try {
    const rs = sh.cssRules;
    for (let i = rs.length - 1; i >= 0; i--) {
      if (rs[i].cssText && rs[i].cssText.includes(marker)) sh.deleteRule(i);
    }
    if (mode === 'off') {
      sh.insertRule('.msg,.tool,.turn,.sys{content-visibility:visible !important;'
                  + 'contain-intrinsic-size:none !important;' + marker + ':1}', rs.length);
    }
  } catch (e) { return { ok: false, why: e.message }; }
  const e = document.querySelector('.msg,.tool,.turn,.sys');
  return { ok: true, computed: e ? getComputedStyle(e).contentVisibility : 'n/a' };
}
"""

#: 量化「高度估算偏差」对**视觉**的实际影响。
#:
#: 上面那轮采样发现 scrollHeight 在回滚中从 101454 涨到 135066（+33%），
#: 而真实内容高度是 626346 px —— 估算值连真值的 1/5 都不到。
#: 但 scrollHeight 变大本身不等于用户看得见问题 —— 关键是**视口里那段
#: 内容的绝对位置有没有漂**。
#:
#: 做法：把 scrollTop 固定在一个值，读某个已知节点的 getBoundingClientRect，
#: 然后触发一次「让更多节点被测量」的操作，再读同一节点的位置。
#: 两次之差就是用户眼睛能看到的跳动量。
JS_ANCHOR_DRIFT = r"""
async () => {
  const stream = document.getElementById('stream');
  const inner = document.getElementById('inner');

  // 滚到中间某处，让一部分节点已测量、一部分还没
  const anchorScrollTop = Math.floor(stream.scrollHeight * 0.5);
  stream.scrollTop = anchorScrollTop;
  await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));

  const nodes = inner.children;
  // 挑一个与视口**有交集**的节点当锚。
  //
  // 原先的写法只认「top 落在视口内」的节点，在长对话里会找不到 ——
  // 真实数据里有单块高 19061px 的回答，视口经常整个落在某一块内部，
  // 那一块的 top 远在视口上方、bottom 远在下方，于是被漏掉。
  // 判据改成「有交集」即可：只要它相对视口的偏移会变，就说明发生了漂移。
  const vr = stream.getBoundingClientRect();
  let anchor = null;
  for (const n of nodes) {
    const b = n.getBoundingClientRect();
    if (b.height > 0 && b.bottom > vr.top && b.top < vr.bottom) { anchor = n; break; }
  }
  if (!anchor) return { error: '找不到与视口相交的锚点' };

  const before = anchor.getBoundingClientRect().top;
  const h0 = stream.scrollHeight;

  // 让尚未测量的节点被强制测量：滚到顶再滚回来。
  // 回滚必须用**同一个 scrollTop 数值**，不能用「再次 scrollHeight 的一半」——
  // scrollHeight 在滚动过程中会变，比例落点会跑到别处，量到的就不是
  // 「同一位置的前后差」，而是「两个不同位置的差」。
  stream.scrollTop = 0;
  await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
  stream.scrollTop = anchorScrollTop;
  await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));

  const after = anchor.getBoundingClientRect().top;
  return {
    driftPx: after - before,
    scrollHeightBefore: h0,
    scrollHeightAfter: stream.scrollHeight,
    growthPct: h0 ? (stream.scrollHeight - h0) / h0 * 100 : 0,
  };
}
"""

#: 注：早期版本这里有一个 `JS_RENDER_NO_CV`，用 `document.createElement('style')`
#: 注入覆盖样式。**那个写法在本产品里根本不生效** —— index.html 的 CSP 声明
#: `style-src 'self'`，内联 <style> 的 `sheet` 恒为 null，规则进不了 CSSOM。
#: 已删除，改用 CSSOM 的 `insertRule`（见 `JS_RENDER_MODE` / `JS_SET_CV`）。
#: 保留这段说明是为了让后来者不要再用内联 style 那条路。

#: A/B 交替复测。**单次对照不可信** —— 第二次跑天然占便宜：
#: 样式表已解析、CSSOM 已建、JIT 已热、字体已加载。
#: 第一次测出「关掉更快」完全可能是这个顺序效应。
#:
#: 所以这里做 ABAB… 交替，各取中位数。只有当两组分布**明显分离**
#: 时才下结论；若区间重叠，只能说「无显著差异」。
#:
#: ## 为什么不能用 `document.createElement('style')` 注入
#:
#: `index.html` 声明了 `Content-Security-Policy: style-src 'self'`。
#: 内联 `<style>` 因此被 CSP 拦下：元素在 DOM 里，但 `st.sheet === null`，
#: **规则一条都没进 CSSOM**，computed style 纹丝不动。
#:
#: 这个坑吃了两次：第一版对照跑出「关掉 content-visibility 也照样膨胀
#: +33.1%」，看着像"证明它不是元凶"，实际上两组跑的都是 `auto` ——
#: 假阴性。现在改用 CSSOM 直接改同源样式表，绕过 CSP 且必然生效。
#:
#: 生效性由 `JS_CV_AUDIT` 复核，不靠假设。
JS_RENDER_MODE = r"""
async ([items, plan, mode]) => {
  // 通过 CSSOM 增删规则 —— CSP 的 style-src 'self' 允许改同源样式表，
  // 只禁止新建内联 <style>。
  const marker = '--gad-cv-override';
  const findSheet = () => {
    for (const sh of document.styleSheets) {
      try { if (sh.href && sh.href.includes('app.css')) return sh; } catch (e) {}
    }
    return null;
  };
  const dropOverride = () => {
    const sh = findSheet();
    if (!sh) return;
    try {
      const rs = sh.cssRules;
      for (let i = rs.length - 1; i >= 0; i--) {
        if (rs[i].cssText && rs[i].cssText.includes('--gad-cv-override')) sh.deleteRule(i);
      }
    } catch (e) {}
  };
  dropOverride();
  if (mode === 'off') {
    const sh = findSheet();
    if (sh) {
      try {
        sh.insertRule('.msg,.tool,.turn,.sys{content-visibility:visible !important;'
                    + 'contain-intrinsic-size:none !important;--gad-cv-override:1}', sh.cssRules.length);
      } catch (e) { return { error: 'insertRule failed: ' + e.message }; }
    } else {
      return { error: 'app.css stylesheet not found' };
    }
  }

  const t0 = performance.now();
  const Bridge = window.Bridge;
  const inner = document.getElementById('inner');

  Bridge.emit({ t: 'clear' });
  Bridge.emit({ t: 'status', running: false, s: {} });

  // 与产品一致：宿主超过 `MaxTranscript` 时逐条裁最旧的并广播 rm。
  const MAXT = 600;
  const emitted = [];
  const pushItem = (it) => {
    Bridge.emit({ t: 'item', item: it });
    emitted.push(it);
    while (emitted.length > MAXT) {
      const old = emitted.shift();
      Bridge.emit({ t: 'rm', seq: old.seq });
    }
  };

  const firstEnd = plan[0];
  for (let i = 0; i < firstEnd; i++) pushItem(items[i]);
  await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
  const firstPaintMs = performance.now() - t0;

  let cursor = firstEnd;
  for (let b = 1; b < plan.length; b++) {
    const end = plan[b];
    for (let i = cursor; i < end; i++) pushItem(items[i]);
    cursor = end;
    await new Promise(r => requestAnimationFrame(r));
  }
  await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));

  const stream = document.getElementById('stream');
  const res = {
    firstPaintMs,
    totalMs: performance.now() - t0,
    domNodes: inner.children.length,
    scrollHeight: stream.scrollHeight,
  };
  return res;
}
"""


#: 批量插入的**真实收益场景** —— 一次灌 600 块，不切批、不插 rAF。
#:
#: 为什么必须单独量这个：`JS_RENDER_MODE` 的 `firstPaintMs` 只覆盖首批 7 条，
#: 量不到 CV 声称的收益（app.css 注释原文：「实测 600 块首屏插入 134ms → 4.7ms」）。
#: 那是**一次性铺满 600 块**的成本，和我跑的分批 31 批不是同一个动作。
#:
#: 如果只按 `firstPaintMs` 判「CV 无显著差异」就删规则，可能正好删掉
#: 唯一有用的场景。所以这里补一组同口径对照：同步灌 600 块，
#: 量从第一次 emit 到两帧后（排版落定）的耗时。
JS_BULK_INSERT = r"""
async ([items, n, mode]) => {
  const marker = '--gad-cv-override';
  const findSheet = () => {
    for (const sh of document.styleSheets) {
      try { if (sh.href && sh.href.includes('app.css')) return sh; } catch (e) {}
    }
    return null;
  };
  const sh = findSheet();
  if (!sh) return { error: 'app.css stylesheet not found' };
  try {
    const rs = sh.cssRules;
    for (let i = rs.length - 1; i >= 0; i--) {
      if (rs[i].cssText && rs[i].cssText.includes(marker)) sh.deleteRule(i);
    }
    if (mode === 'off') {
      sh.insertRule('.msg,.tool,.turn,.sys{content-visibility:visible !important;'
                  + 'contain-intrinsic-size:none !important;' + marker + ':1}', rs.length);
    }
  } catch (e) { return { error: e.message }; }

  const Bridge = window.Bridge;
  Bridge.emit({ t: 'clear' });
  Bridge.emit({ t: 'status', running: false, s: {} });
  await new Promise(r => requestAnimationFrame(r));

  const inner = document.getElementById('inner');
  const t0 = performance.now();
  // 同步灌满 —— 这是最坏情况：一次 JS 任务里插 n 块。
  // n 取的就是 `MaxTranscript`（600），正好卡在上限，不会触发裁剪，
  // 所以这里不需要模拟 rm —— 这一组量的是**插入成本**，不是裁剪正确性。
  for (let i = 0; i < n && i < items.length; i++) Bridge.emit({ t: 'item', item: items[i] });
  const emitMs = performance.now() - t0;
  // 强制一次同步布局：CV 开着时这里只排视口内的块，关掉则全排
  const h = inner.offsetHeight;
  const layoutMs = performance.now() - t0;
  await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
  return {
    emitMs, layoutMs, paintMs: performance.now() - t0,
    domNodes: inner.children.length, innerHeight: h,
  };
}
"""


#: 总高膨胀探针。
#:
#: 同一个 DOM，`scrollHeight` 会在滚动之后**变大且不自己恢复**。
#:
#: 归因必须靠对照实验，不能靠猜 —— 上一版直接写成「这是
#: `content-visibility` 造成的」，然后被自己的对照推翻（两组都膨胀）。
#: 现在 `probe_on` / `probe_off` 各跑一次同一套滚动动作，
#: 只有"开启才膨胀、关掉不膨胀"才能把责任归给 `content-visibility`。
#:
#: 用户侧的表现：滚一圈之后滚动条变短了（总长变长）、当前阅读位置会跳。
JS_HEIGHT_PROBE = r"""
async () => {
  const stream = document.getElementById('stream');
  const h = () => stream.scrollHeight;
  const top = () => stream.scrollTop;
  const frame = () => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));

  const out = { h0: h(), t0: top() };

  stream.scrollTop = stream.scrollHeight;      // 滚到底
  await frame();
  out.h1 = h(); out.t1 = top();

  // 逐帧往回滚 40 帧（模拟用户往上翻）
  const step = Math.max(200, stream.clientHeight * 0.8);
  for (let i = 0; i < 40; i++) {
    stream.scrollTop = Math.max(0, stream.scrollTop - step);
    await new Promise(r => requestAnimationFrame(r));
  }
  out.h2 = h(); out.t2 = top();

  stream.scrollTop = stream.scrollHeight;      // 滚回底部
  await frame();
  out.h3 = h(); out.t3 = top();

  stream.scrollTop = 0;                        // 滚到顶
  await frame();
  out.h4 = h(); out.t4 = top();

  return out;
}
"""


#: 独立几何测量：不信任 `scrollHeight`，直接累加每个子元素的真实盒高。
#:
#: `scrollHeight` 在滚动前后会变，且**开着 content-visibility 时两个值都错**
#: （101454 / 135066，真值是 626346）。所以必须另找一条独立路径裁决 ——
#: 关掉 content-visibility 后再量一次，那才是无估算的几何真值。
#:
#: 这里用三种互不依赖的算法交叉验证同一个状态：
#:
#:   1. `offsetHeight` 累加   —— 每个元素的实际盒高，最接近"内容真实占用"
#:   2. 末元素 `offsetTop + offsetHeight` —— 内容总长（含最后一个元素底部）
#:   3. `getBoundingClientRect().height` 累加 —— 视觉盒高，含变换
#:
#: 三者一致才说明测的是几何真值，而不是某个 CSS 属性引入的估算值。
JS_TRUE_HEIGHT = r"""
async () => {
  const stream = document.getElementById('stream');
  const inner = document.getElementById('inner');
  const kids = Array.from(inner.children);

  const sumOffset = kids.reduce((a, e) => a + e.offsetHeight, 0);
  const sumRect = kids.reduce((a, e) => a + e.getBoundingClientRect().height, 0);
  const last = kids[kids.length - 1];
  const lastBottom = last ? last.offsetTop + last.offsetHeight : 0;

  return {
    count: kids.length,
    scrollHeight: stream.scrollHeight,
    sumOffset, sumRect, lastBottom,
    // inner 自身的盒高（不受子元素 content-visibility 影响的独立读数）
    innerOffsetHeight: inner.offsetHeight,
    innerScrollHeight: inner.scrollHeight,
  };
}
"""


#: 覆盖样式到底生效没有 —— 不能假设。
#:
#: 上一轮的对照实验得出「关掉 content-visibility 也照样膨胀 +33.1%」，
#: 但那个结论的前提是 `<style>` 真的改了 computed style。如果选择器没命中
#: （例如消息块的实际 class 不是 `.msg`），两组测的其实是同一个东西，
#: 「无差异」就成了假阴性。
#:
#: 所以先读几个屏外元素的 computed `content-visibility`，再下结论。
JS_CV_AUDIT = r"""
() => {
  const kids = Array.from(document.getElementById('inner').children);
  const sample = kids.filter((_, i) => i % 97 === 0).slice(0, 8);
  return {
    total: kids.length,
    classes: [...new Set(kids.map(e => e.className))].slice(0, 10),
    samples: sample.map(e => ({
      cls: e.className,
      cv: getComputedStyle(e).contentVisibility,
      cis: getComputedStyle(e).containIntrinsicSize,
      h: e.offsetHeight,
    })),
  };
}
"""


def scroll_ab(page, items: list[dict], plan: list[int], rounds: int = 4) -> dict:
    """滚动流畅度 A/B：同一份已铺好的内容，只切 CV 规则，交替翻历史。

    这是**决定要不要修**的关键实验。已知开启 CV 有三条缺陷（高度低估
    83.8% / 单向膨胀 +33% / 锚点漂移 1960px），但 CV 存在的理由就是
    「屏外节点跳过布局，滚动更快」。如果关掉之后滚动明显变卡，
    那就不能简单删规则 —— 得换一个不估算高度的方案。

    量法：每轮先按当前模式铺好全量内容，然后从底部逐帧往上翻，
    记录帧间隔分布。中位帧间隔与 p95 才是用户手感。
    """
    runs: list[dict] = []
    for i in range(rounds):
        mode = "off" if i % 2 == 0 else "on"
        page.evaluate("window.Bridge.emit({t:'clear'})")
        page.wait_for_timeout(120)
        page.evaluate(JS_RENDER_MODE, [items, plan, mode])
        sw = page.evaluate(JS_SCROLL_SWEEP, 90)
        runs.append({"mode": mode, "i": i, **sw})

    on = [r for r in runs if r["mode"] == "on"]
    off = [r for r in runs if r["mode"] == "off"]

    def agg(xs: list[dict], key: str) -> dict:
        v = [x[key] for x in xs]
        return {"med": statistics.median(v), "min": min(v), "max": max(v), "all": v}

    return {
        "runs": runs,
        "on": {k: agg(on, k) for k in ("medMs", "p95Ms", "maxMs", "over16", "over50")},
        "off": {k: agg(off, k) for k in ("medMs", "p95Ms", "maxMs", "over16", "over50")},
    }


def bulk_insert_ab(page, items: list[dict], n: int = 600, rounds: int = 10) -> dict:
    """一次灌 600 块的 A/B —— 验证 CV 注释里「134ms → 4.7ms」是否还成立。

    这是 CV 唯一可能的正面理由。分批铺的 `firstPaintMs` 只含首批 7 条，
    天然量不到它的收益，不能拿那组数字决定删不删。
    """
    for _ in range(2):          # 热身，丢弃
        page.evaluate(JS_BULK_INSERT, [items, n, "on"])
        page.wait_for_timeout(120)

    runs: list[dict] = []
    for i in range(rounds):
        mode = "off" if i % 2 == 0 else "on"
        res = page.evaluate(JS_BULK_INSERT, [items, n, mode])
        runs.append({"mode": mode, "i": i, **res})

    on = [r for r in runs if r["mode"] == "on"]
    off = [r for r in runs if r["mode"] == "off"]

    def agg(xs: list[dict], key: str) -> dict:
        v = [x[key] for x in xs if isinstance(x.get(key), (int, float))]
        if not v:
            return {"med": 0.0, "min": 0.0, "max": 0.0, "all": []}
        return {"med": statistics.median(v), "min": min(v), "max": max(v), "all": v}

    return {
        "runs": runs,
        "on": {k: agg(on, k) for k in ("emitMs", "layoutMs", "paintMs")},
        "off": {k: agg(off, k) for k in ("emitMs", "layoutMs", "paintMs")},
    }


def ab_test(page, items: list[dict], plan: list[int], rounds: int = 10) -> dict:
    """交替跑「开着 content-visibility」与「关掉」各 rounds/2 次。

    为什么必须交替而不是「先测 A 再测 B」：
    第二次跑天然占便宜 —— 样式表已解析、CSSOM 已建、JIT 已热、
    字体已加载、GC 堆已长起来。第一版单次对照测出「关掉快 2.1 倍」，
    完全可能是「它是第二次跑」造成的，而不是 `content-visibility` 的锅。

    所以先热身两轮（丢弃），再 ABAB… 交替，各取中位数与全区间。
    只有当两组分布**明显分离**时才下结论。
    """
    # 热身：让样式表、JIT、字体都进入稳态，且不计入结果
    for _ in range(2):
        page.evaluate("window.Bridge.emit({t:'clear'})")
        page.wait_for_timeout(150)
        page.evaluate(JS_RENDER_MODE, [items, plan, "on"])
        page.wait_for_timeout(150)

    # 对照组的总高探针：关掉 content-visibility 后，同一套滚动动作
    # 会不会也把总高撑大？如果不会，那 +33% 就是 content-visibility 的直接责任。
    page.evaluate("window.Bridge.emit({t:'clear'})")
    page.wait_for_timeout(150)
    page.evaluate(JS_RENDER_MODE, [items, plan, "off"])
    cv_audit_off = page.evaluate(JS_CV_AUDIT)
    probe_off = page.evaluate(JS_HEIGHT_PROBE)

    page.evaluate("window.Bridge.emit({t:'clear'})")
    page.wait_for_timeout(150)
    page.evaluate(JS_RENDER_MODE, [items, plan, "on"])
    cv_audit_on = page.evaluate(JS_CV_AUDIT)
    probe_on = page.evaluate(JS_HEIGHT_PROBE)

    # 几何真值：开启 / 关掉各量一次，都取"刚铺完"与"回滚之后"两个点。
    # 关掉时没有任何估算，量到的就是真实内容高度 —— 这是裁决基准。
    geo: dict[str, dict] = {}
    for mode in ("on", "off"):
        page.evaluate("window.Bridge.emit({t:'clear'})")
        page.wait_for_timeout(150)
        page.evaluate(JS_RENDER_MODE, [items, plan, mode])
        geo[f"{mode}_fresh"] = page.evaluate(JS_TRUE_HEIGHT)
        page.evaluate(JS_HEIGHT_PROBE)          # 触发膨胀
        geo[f"{mode}_after"] = page.evaluate(JS_TRUE_HEIGHT)
    geo_fresh, geo_after = geo["on_fresh"], geo["on_after"]

    runs: list[dict] = []
    for i in range(rounds):
        mode = "off" if i % 2 == 0 else "on"
        page.evaluate("window.Bridge.emit({t:'clear'})")
        page.wait_for_timeout(120)
        res = page.evaluate(JS_RENDER_MODE, [items, plan, mode])
        runs.append({"mode": mode, "i": i, **res})

    on = [r for r in runs if r["mode"] == "on"]
    off = [r for r in runs if r["mode"] == "off"]

    def agg(xs: list[dict], key: str) -> dict:
        v = [x[key] for x in xs]
        return {"med": statistics.median(v), "min": min(v), "max": max(v), "all": v}

    return {
        "runs": runs,
        "on": {k: agg(on, k) for k in ("firstPaintMs", "totalMs", "scrollHeight")},
        "off": {k: agg(off, k) for k in ("firstPaintMs", "totalMs", "scrollHeight")},
        "probe_on": probe_on,
        "probe_off": probe_off,
        "geo": geo,
        "geo_fresh": geo_fresh,
        "geo_after": geo_after,
        "cv_audit_on": cv_audit_on,
        "cv_audit_off": cv_audit_off,
    }


def serve(directory: Path) -> tuple[http.server.ThreadingHTTPServer, int]:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(directory))
    # 静音访问日志（默认会往 stderr 刷每一行，淹没报告）
    handler.log_message = lambda *a, **k: None  # type: ignore[assignment]
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


def main() -> int:
    from playwright.sync_api import sync_playwright

    path = Path(sys.argv[sys.argv.index("--file") + 1]) if "--file" in sys.argv else pick_file()
    conv = json.loads(path.read_text(encoding="utf-8"))
    rows = build_rows(conv)
    items = [header_item(conv)] + rows_to_items(rows)

    print("=" * 70)
    print("旅程 7 · 超长对话在 Web 壳前端的真实成本")
    print("=" * 70)
    print(f"  样本文件 : {path.name}  ({path.stat().st_size / 1024:.0f} KB)")
    print(f"  原始消息 : {len(conv.get('messages') or [])} 条")
    print(f"  重放行数 : {len(rows)} 行 → {len(items)} 个 item（Thought 拆行）")
    total_chars = sum(len(i['text']) for i in items)
    print(f"  正文字数 : {total_chars:,} 字")
    print(f"  单条最长 : {max((len(i['text']) for i in items), default=0):,} 字")
    row_ends = check_batch_math(rows)
    plan = rows_to_item_bounds(rows, row_ends)
    print(f"  item 边界: 首批 {plan[0]} 个 · 末批到 {plan[-1]} 个（应为 {len(items)}）")

    srv, port = serve(WEB_DIR)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless="--headed" not in sys.argv)
            page = browser.new_page(viewport={"width": 1003, "height": 715})
            errs: list[str] = []
            page.on("pageerror", lambda e: errs.append(str(e)))
            page.goto(f"http://127.0.0.1:{port}/index.html", wait_until="load")
            page.wait_for_timeout(600)

            # 先确认前端真的加载起来了（mock 场景会自己铺一批数据，先清掉）
            page.evaluate("window.Bridge.emit({t:'clear'})")
            page.wait_for_timeout(150)

            r = page.evaluate(JS_RENDER, [items, plan, MAX_TRANSCRIPT])
            hp = page.evaluate(JS_HEIGHT_PROBE)
            sc = page.evaluate(JS_SCROLL)
            sweep = page.evaluate(JS_SCROLL_SWEEP, 90)
            drift = page.evaluate(JS_ANCHOR_DRIFT)
            ab = ab_test(page, items, plan)
            # 修不修的前置证据：关掉 CV 之后，滚动会不会变卡。
            # 只有这组数字不劣于开启，删规则才是净收益。
            sab = scroll_ab(page, items, plan)
            # CV 唯一可能的正面理由：一次灌 600 块时跳过屏外布局。
            # 必须单独量 —— 分批铺的首屏指标量不到这个场景。
            bab = bulk_insert_ab(page, items, min(600, len(items)))
            browser.close()
    finally:
        srv.shutdown()

    print()
    print("-" * 70)
    print("  渲染结果")
    print("-" * 70)
    print(f"  首批条数       : {r['firstBatch']} 条")
    print(f"  总批次数       : {r['batches']} 批")
    print(f"  首屏（推完）   : {r['firstEmitMs']:8.1f} ms")
    print(f"  首屏（排完版） : {r['firstPaintMs']:8.1f} ms   <- 用户真正等的那一段")
    print(f"  全量铺完       : {r['totalMs']:8.1f} ms")
    print(f"  DOM 节点数     : {r['domNodes']} 个（产品 Transcript 上限 {MAX_TRANSCRIPT}）")
    print(f"  首条文本       : {r['firstText']!r}")
    print(f"  末条文本       : {r['lastText']!r}")
    print()
    print(f"  滚动到底       : {sc['ms']:.1f} ms · scrollTop={sc['scrollTop']:.0f} / "
          f"scrollHeight={sc['scrollHeight']:.0f} · 视口 {sc['clientHeight']:.0f}")
    print()
    print(f"  总高膨胀探针（同一 DOM，只滚不改内容）：")
    print(f"    刚铺完          : {hp['h0']:.0f} px")
    print(f"    滚到底          : {hp['h1']:.0f} px  ({hp['h1']-hp['h0']:+.0f})")
    print(f"    往回滚 40 帧    : {hp['h2']:.0f} px  ({hp['h2']-hp['h0']:+.0f})")
    print(f"    滚回底部        : {hp['h3']:.0f} px  ({hp['h3']-hp['h0']:+.0f})")
    print(f"    滚到顶          : {hp['h4']:.0f} px  ({hp['h4']-hp['h0']:+.0f})")
    print()
    print(f"  逐帧往回滚 {sweep['frames']} 帧：")
    print(f"    帧间隔 中位 {sweep['medMs']:6.1f} ms · p95 {sweep['p95Ms']:6.1f} ms · "
          f"最大 {sweep['maxMs']:6.1f} ms")
    print(f"    掉帧(>16.7ms) {sweep['over16']:3} 帧 · 明显卡顿(>50ms) {sweep['over50']:3} 帧")
    print(f"    总高 {sweep['scrollHeight']:.0f} px · 视口 {sweep['clientHeight']:.0f} px")
    print()
    if "error" in drift:
        print(f"  高度锚定     : {drift['error']}")
    else:
        print(f"  高度估算偏差 : scrollHeight {drift['scrollHeightBefore']:.0f} → "
              f"{drift['scrollHeightAfter']:.0f} px（{drift['growthPct']:+.1f}%）")
        print(f"                 视口内锚点漂移 {drift['driftPx']:+.1f} px  <- 用户眼睛看到的跳动")
    print()
    print(f"  A/B 交替复测（各 {len(ab['on']['firstPaintMs']['all'])} 轮，交替进行）：")
    print(f"    {'指标':<14}{'开启 content-visibility':<34}{'关掉':<34}")
    for key, label, unit in (("firstPaintMs", "首屏（排完版）", "ms"),
                             ("totalMs", "全量铺完", "ms"),
                             ("scrollHeight", "总高", "px")):
        a, b = ab["on"][key], ab["off"][key]
        fmt = (lambda d: f"中位 {d['med']:8.1f}  区间 [{d['min']:.1f}, {d['max']:.1f}] {unit}")
        print(f"    {label:<14}{fmt(a):<34}{fmt(b):<34}")
    separated = (ab["on"]["firstPaintMs"]["max"] < ab["off"]["firstPaintMs"]["min"]
                 or ab["off"]["firstPaintMs"]["max"] < ab["on"]["firstPaintMs"]["min"])
    print(f"    首屏两组区间{'完全分离（差异显著）' if separated else '重叠（无显著差异，单次对照不可信）'}")
    print(f"    逐轮原始值：")
    for x in ab["runs"]:
        print(f"      #{x['i']+1:2} {'开启' if x['mode']=='on' else '关掉'}  "
              f"首屏 {x['firstPaintMs']:7.1f} ms · 铺完 {x['totalMs']:7.1f} ms · "
              f"总高 {x['scrollHeight']:.0f} px")

    print()
    print(f"  批量插入 600 块 · 对照（同步灌满，最坏情况）：")
    print(f"    {'指标':<16}{'开启 content-visibility':<32}{'关掉':<32}")
    for key, label, unit in (("emitMs", "JS 插入", "ms"),
                             ("layoutMs", "强制同步布局", "ms"),
                             ("paintMs", "两帧后落定", "ms")):
        a, b = bab["on"][key], bab["off"][key]
        fmt = (lambda d: f"中位 {d['med']:7.1f}  区间 [{d['min']:.1f}, {d['max']:.1f}] {unit}")
        print(f"    {label:<16}{fmt(a):<32}{fmt(b):<32}")
    bulk_gain = bab["on"]["layoutMs"]["med"]
    bulk_loss = bab["off"]["layoutMs"]["med"]
    cv_worth_it = bulk_gain > 0 and bulk_loss > bulk_gain * 1.5
    print(f"    → CV 在批量插入上{'有' if cv_worth_it else '没有'}实质收益"
          f"（强制布局 {bulk_gain:.1f} → {bulk_loss:.1f} ms，"
          f"{'快' if cv_worth_it else '只差'} {bulk_loss/bulk_gain:.1f}×）"
          if bulk_gain else "    → 无数据")

    print()
    print(f"  滚动流畅度 · 对照（同一套逐帧回滚 90 帧）：")
    print(f"    {'指标':<16}{'开启 content-visibility':<32}{'关掉':<32}")
    for key, label, unit in (("medMs", "帧间隔中位", "ms"),
                             ("p95Ms", "帧间隔 p95", "ms"),
                             ("maxMs", "最长帧", "ms"),
                             ("over16", "掉帧(>16.7ms)", "帧"),
                             ("over50", "卡顿(>50ms)", "帧")):
        a, b = sab["on"][key], sab["off"][key]
        fmt = (lambda d: f"中位 {d['med']:7.1f}  区间 [{d['min']:.1f}, {d['max']:.1f}] {unit}")
        print(f"    {label:<16}{fmt(a):<32}{fmt(b):<32}")
    # 关掉 CV 的滚动是否「不劣于」开启：中位帧间隔不比开启差 10% 以上，
    # 且没有多出 >50ms 的卡顿帧。满足才允许删规则。
    off_med = sab["off"]["medMs"]["med"]
    on_med = sab["on"]["medMs"]["med"]
    off_stall = sab["off"]["over50"]["max"]
    on_stall = sab["on"]["over50"]["max"]
    scroll_cost_free = (off_med <= on_med * 1.10) and (off_stall <= on_stall)
    print(f"    → 关掉后滚动{'不劣于开启' if scroll_cost_free else '明显更差'}"
          f"（中位 {on_med:.1f} → {off_med:.1f} ms，"
          f"最长卡顿帧 {on_stall} → {off_stall}）")

    print()
    print(f"  总高膨胀 · 对照（同一套滚动动作）：")
    for label, key in (("开启 content-visibility", "probe_on"),
                       ("关掉 content-visibility", "probe_off")):
        q = ab[key]
        grow = q["h2"] - q["h0"]
        pct = 100.0 * grow / q["h0"] if q["h0"] else 0.0
        print(f"    {label:<26} 刚铺完 {q['h0']:.0f} → 回滚后 {q['h2']:.0f} "
              f"（{grow:+.0f} px, {pct:+.1f}%）")
    cv_blame = ab["probe_on"]["h2"] > ab["probe_on"]["h0"] * 1.05 and \
               ab["probe_off"]["h2"] <= ab["probe_off"]["h0"] * 1.02

    print()
    print(f"  覆盖样式生效性审计：")
    for label, key in (("开启", "cv_audit_on"), ("关掉", "cv_audit_off")):
        a = ab[key]
        cvs = sorted({s["cv"] for s in a["samples"]})
        print(f"    {label}  子元素 class: {a['classes']}  "
              f"→ computed content-visibility = {cvs}")

    print()
    print(f"  几何真值裁决（不依赖 scrollHeight）：")
    for label, key in (("开启 · 刚铺完", "on_fresh"), ("开启 · 回滚后", "on_after"),
                       ("关掉 · 刚铺完", "off_fresh"), ("关掉 · 回滚后", "off_after")):
        g = ab["geo"][key]
        print(f"    {label:<14} scrollHeight {g['scrollHeight']:>7.0f} · "
              f"ΣoffsetHeight {g['sumOffset']:>7.0f} · "
              f"ΣgetBoundingClientRect {g['sumRect']:>7.0f} · "
              f"inner.offsetHeight {g['innerOffsetHeight']:>7.0f}")
    real_h = ab["geo"]["off_fresh"]["innerOffsetHeight"]
    est_h = ab["geo"]["on_fresh"]["innerOffsetHeight"]
    if real_h:
        print(f"    → 真实内容高度 {real_h:.0f} px；开启时估算 {est_h:.0f} px "
              f"（低估 {100.0*(real_h-est_h)/real_h:.1f}%）")
    if errs:
        print(f"  [错误] 页面异常 {len(errs)} 条：")
        for e in errs[:5]:
            print("    " + e[:200])

    print()
    print("-" * 70)
    print("  判据")
    print("-" * 70)
    ok_first = r["firstPaintMs"] <= BUDGET_FIRST_MS
    ok_total = r["totalMs"] <= BUDGET_TOTAL_MS
    ok_trim = r["domNodes"] <= MAX_TRANSCRIPT
    # 贴底：scrollTop + clientHeight 应该 ≈ scrollHeight（容差 8px）
    at_bottom = abs((sc["scrollTop"] + sc["clientHeight"]) - sc["scrollHeight"]) <= 8
    ok_bottom = at_bottom
    ok_err = not errs
    # 滚动流畅度：中位帧间隔 <= 20ms（约 50fps）且无 >50ms 的长帧
    ok_scroll_med = sweep["medMs"] <= 20.0
    ok_scroll_stall = sweep["over50"] == 0

    print(f"  首屏 <= {BUDGET_FIRST_MS:.0f} ms              : {ok_first}"
          f"（{r['firstPaintMs']:.1f} ms）")
    print(f"  全量铺完 <= {BUDGET_TOTAL_MS:.0f} ms          : {ok_total}"
          f"（{r['totalMs']:.1f} ms）")
    print(f"  DOM 节点 <= Transcript 上限 {MAX_TRANSCRIPT}   : {ok_trim}"
          f"（{r['domNodes']} 个）")
    print(f"  滚到底后贴底                  : {ok_bottom}"
          f"（差 {sc['scrollHeight'] - sc['scrollTop'] - sc['clientHeight']:.0f} px）")
    print(f"  回滚中位帧间隔 <= 20 ms       : {ok_scroll_med}"
          f"（{sweep['medMs']:.1f} ms）")
    print(f"  回滚无 >50 ms 长帧            : {ok_scroll_stall}"
          f"（{sweep['over50']} 帧）")
    print(f"  回滚后总高不膨胀(<=5%)        : {not cv_blame}"
          f"（开启 {ab['probe_on']['h2']-ab['probe_on']['h0']:+.0f} px / "
          f"关掉 {ab['probe_off']['h2']-ab['probe_off']['h0']:+.0f} px）")
    print(f"  关掉 CV 滚动不劣于开启        : {scroll_cost_free}"
          f"（中位 {sab['on']['medMs']['med']:.1f} → {sab['off']['medMs']['med']:.1f} ms）")
    print(f"  页面无异常                    : {ok_err}")

    passed = all((ok_first, ok_total, ok_trim, ok_bottom,
                  ok_scroll_med, ok_scroll_stall, ok_err))
    print()
    print(f"  判定: {'通过' if passed else '不通过'}")
    if not ok_trim:
        print()
        print("  [说明] DOM 节点超上限不是渲染错误，而是一条**产品缺陷**：")
        print("         AgentHost.TrimTranscript 只裁数据层（Transcript.Count），")
        print("         前端 onItem 是纯 Append，从不删节点。所以超长对话里")
        print("         数据只留 600 条、DOM 却留着全部 —— 两边对不上。")
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
