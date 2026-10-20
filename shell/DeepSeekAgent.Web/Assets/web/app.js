'use strict';

/* =========================================================================
   DeepSeek Agent Desktop · 界面层（唯一真源）
   -------------------------------------------------------------------------
   这一层只认 window.Bridge（见 bridge.js），**不直接碰 window.chrome.webview**。
   于是同一份 HTML 有两种活法：
     · 在 WebView2 里   → 走真宿主（C# 的 WebBridge）
     · 双击浏览器打开    → 走 mock.js 的假数据（?scene=normal|running|long|bulk|breaker）
   换宿主只换 bridge.js 那一层，本文件一行不用动。

   与宿主的协议（两个方向，都是 JSON）：
     宿主 → 界面：ready | hist | item | chg | clear | log | status | convs | models | attached | err
     界面 → 宿主：hello | submit | cancel | newConv | setChat | setModel |
                 identity | listRemote | attachSession | handoff | win |
                 switchConv | renameConv | delConv | syncConv | reset | replay

   末尾六个是 2026-10-18 补的：宿主侧（WebBridge.Handle）早就支持，
   前端一直没接 —— 用户够不到「重命名 / 删除 / 同步 / 重置熔断 / 重放」。
   接的时候发现两个真问题（不是「缺个按钮」那么简单）：
     ① 列表项的 id 是**服务端** id，而 switchConv / renameConv / syncConv
        在宿主侧按**本地** id 工作 —— 所以列表必须同时推 localId，
        否则这三个动作发过去只会静默失败；
     ② delConv 原本调的是 DeleteConversationAsync（删本地文件），
        但列表里全是服务端会话，拿 remote_id 去删本地必然找不到。
        已改成 DeleteRemoteSessionAsync（删服务端 + 解绑本地）。

   握手方向：**前端主动发 hello**（见文件末尾的启动段），宿主的 ready 是冗余的第二条路。
   这样就不存在「宿主发 ready 时前端监听器还没装好 → 永远停在启动中」的死锁窗口。

   这一层的几个约定，参考的是本机 DeepSeek Harness（Electron）桌面版的做法：
     · 前端只认一个全局端口对象，不直接碰宿主 API —— 对应它的 contextBridge
     · 宿主对每条来路消息校验来源 origin，不只在加载页面时校验一次
     · 页面带严格 CSP（connect-src 'none'），因为页面确实不发任何网络请求
     · 派生视图（任务列表 / 右栏）的重建合并到一帧，不逐条重建

   安全约定（重要）：**所有来自引擎的文本一律走 textContent / createTextNode**，
   绝不用 innerHTML 拼外部数据。只有本文件里写死的 SVG 常量才走 innerHTML。
   引擎回来的正文里可能含 < > & 或 ``` 围栏，走 innerHTML 就是一个现成的注入面。
   ========================================================================= */

const Bridge = window.Bridge;
const send = (m) => Bridge.send(m);
const MOCK = Bridge.kind === 'mock';

const $ = (id) => document.getElementById(id);

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

/* ---------------------- 屏外块的占位高度估算 ----------------------
   `content-visibility:auto` 让屏外块不排版，代价是它们必须先按一个
   猜的高度占位（`contain-intrinsic-size`）。猜得准不准，直接决定
   滚动条长度、滚动中总高会不会膨胀、以及视口内的内容会不会跳。

   CSS 里兜底是 `auto 110px`。对长对话那是错的：740 块真实总高
   626406px，110px 估算只有 81400px（低估 87%）。实测后果是滚动中
   总高单向膨胀 +33% 且不回退、视口锚点漂移 1960px。

   所以按 kind 与字符数给每块算一个估（h = a + b·chars）。

   **系数怎么来的，必须分清两类，否则会重犯「拿合成数据外推」的错**：

   · `Goal` / `Assistant` —— 只能来自**真实对话**。这两个走
     `renderRich()` 渲染 Markdown，代码块、列表、表格的行高与纯文本
     完全不同；用户消息还受 `.msg.me .bubble{max-width:78%}` 约束。
     用「字字字…」这种合成串去拟合，斜率会偏 48%（实测 0.657 vs
     0.444）。现有系数来自 107 万字 / 740 块真实样本，R² = 0.9997
     与 1.0000，在真实 WebView2 里复测总偏差 +0.2% / +0.0%。

   · 其余 10 种 —— 结构固定（固定 padding + 单行标题 + 可选正文），
     与正文语义无关，可以用合成串测。数值由
     `tools/e2e/probe_webview2_kinds.py` 在真实 WebView2 里量得。

   **`Thought` 原来的 32px 是错的**：折叠态只有一行 `.thought-head`，
   实测恒 18px 且与字数完全无关（R²=1.000 在这里的含义是「零斜率」，
   不是「拟合得好」—— 当初把它读成了后者）。

   **`Info` 原来的 54px 也是错的**：实测单行 38px。而且原来只列了 4 种
   kind，其余 8 种 `intrinsicHeight()` 返回 null → 退回 CSS 的 110px。
   注释写的是「交给 CSS 兜底」，实际是**静默的高估**：`turn` 真值 18px
   被估成 110px（6.1 倍），`sys` 真值 38px 被估成 110px（2.9 倍）。
   一段含大量状态行与分隔线的对话，滚动条会比真实内容长好几倍。

   `Assistant` 的截距是负数（短消息的 padding 抵掉了行高），必须夹
   下限 —— 否则极短的消息会算出负高度，占位塌成 0。 */
const INTRINSIC = {
  // —— 真实对话拟合（Markdown 渲染，不可用合成数据替代）——
  Goal: [95.9, 0.4444],        // 用户消息，R²=0.9997
  Assistant: [-25.4, 0.7163],  // 模型回答，R²=1.0000

  // —— 固定结构，真实 WebView2 实测 ——
  Thought: [18.0, 0.0],        // 折叠态只有一行 .thought-head，与字数无关
  ToolCall: [42.0, 0.0],       // 折叠态只有一行 .tool-head
  TurnDivider: [18.0, 0.0],    // 阶梯而非线性：空文本 1px，有标签恒 18px
                               // （`.turn .tx` 被两侧 `.ln` 挤压，100 字也不换行）

  // —— 状态行（.sys）：38px 起，每行约 80 字 ——
  // 换行是离散的，线性拟合在阶梯上必然有误差。取值偏向长文本：
  // 低估会让滚动中总高单向膨胀（+33%，锚点漂移），那是更糟的体验。
  Thinking: [38.0, 0.4211],
  Gate: [38.0, 0.4211],
  Breaker: [38.0, 0.4211],
  Error: [38.0, 0.4211],
  Notice: [38.0, 0.4211],
  Summary: [38.0, 0.4211],
  Info: [38.0, 0.4211],
};
/** 只兜「负值」，不是全局最小值。
 *
 * 早先写的是 `Math.max(24, ...)`，本意是防 `Assistant` 的负截距算出负数
 * 高度。但它被无条件应用到所有 kind，于是 `Thought`（真值 18px）与
 * `TurnDivider`（真值 18px）永远被抬到 24 —— 11 个 thought 块就多占
 * 66px。绝对值不大，但这是**设计错误**：floor 的职责是「别让占位塌成
 * 负数」，不是「给所有块定一个下限」。
 *
 * 空内容（`Goal`/`Assistant` 的 text 为空）确实会算出负值，那时用 1px
 * 就够 —— 真实渲染出来的空块也几乎不占高。 */
const INTRINSIC_MIN = 1;

/** 未知 kind 也要给个估 —— 不能返回 null 让它退回 CSS 的 110px。
 *
 * 110px 是给「完全不知道这是什么」的极端兜底，比任何真实块都高。
 * 未知 kind 用状态行的单行高度起步更安全：真的长内容会靠
 * `contain-intrinsic-size:auto` 在滚动过后自动校正，而一开始就
 * 按 110px 占位会让滚动条先长出一大截再缩回去。 */
const INTRINSIC_FALLBACK = [38.0, 0.4211];

function intrinsicHeight(it) {
  const m = INTRINSIC[it.kind] || INTRINSIC_FALLBACK;
  const n = (it.text || '').length;
  return Math.max(INTRINSIC_MIN, Math.round(m[0] + m[1] * n));
}

/* ---------------------------- 内联 SVG 常量 ----------------------------
   全部是本文件写死的字符串，不含任何外部数据 —— 这是它们可以走 innerHTML
   而正文不可以的原因。 */

const WHALE_PATH =
  'M2.6 13.2c0-4.5 3.7-8.2 8.2-8.2 3.6 0 6.7 2.3 7.8 5.6.3.8 1.1 1.4 2.3 1.4' +
  '-1 1.7-2.5 2.7-4.2 2.9-1.3 2.5-3.8 4.3-6.8 4.3H4.1c-.8 0-1.5-.7-1.5-1.5v-4.5z';

/** 头像用：class="fill" 让它撑满容器（.msg .av svg.fill / .ava-prev svg.fill / .pick svg.fill）。 */
const SVG_WHALE =
  '<svg class="fill" viewBox="0 0 24 24" fill="none">' +
  '<path d="' + WHALE_PATH + '" fill="#fff"/>' +
  '<circle cx="8.6" cy="12.4" r="1.15" fill="#2B6CF6"/></svg>';

const SVG_SPIN =
  '<svg width="16" height="16" viewBox="0 0 20 20" fill="none">' +
  '<circle cx="10" cy="10" r="7.6" stroke="#2B6CF6" stroke-width="2" ' +
  'stroke-dasharray="11 7" class="spin"/></svg>';

const SVG_OK =
  '<svg width="16" height="16" viewBox="0 0 20 20" fill="none">' +
  '<circle cx="10" cy="10" r="8.4" fill="#E7F7ED"/>' +
  '<path d="M6.4 10.2l2.5 2.5 4.7-5" stroke="#16A34A" stroke-width="1.8" ' +
  'stroke-linecap="round" stroke-linejoin="round"/></svg>';

const SVG_FAIL =
  '<svg width="16" height="16" viewBox="0 0 20 20" fill="none">' +
  '<circle cx="10" cy="10" r="8.4" fill="#FDECEC"/>' +
  '<path d="M7.2 7.2l5.6 5.6M12.8 7.2l-5.6 5.6" stroke="#DC2626" ' +
  'stroke-width="1.8" stroke-linecap="round"/></svg>';

const CARET_PATH = 'M6 9.5l6 6 6-6';

/** 思考块：.thought-head .tcaret 是它唯一的挂点。 */
const SVG_CARET_T =
  '<svg class="tcaret" width="15" height="15" viewBox="0 0 24 24" fill="none">' +
  '<path d="' + CARET_PATH + '" stroke="currentColor" stroke-width="1.7" ' +
  'stroke-linecap="round" stroke-linejoin="round"/></svg>';

/** 工具卡：.tool.collapsed .tool-caret 靠这个类名翻转。 */
const SVG_CARET_TOOL =
  '<svg class="tool-caret" width="15" height="15" viewBox="0 0 24 24" fill="none">' +
  '<path d="' + CARET_PATH + '" stroke="currentColor" stroke-width="1.7" ' +
  'stroke-linecap="round" stroke-linejoin="round"/></svg>';

/* 状态行图标。设计稿的 .sys 结构就是「图标 + span<b>标题</b> · 正文」，
   三种底色（默认琥珀 / .ok / .bad）共用同一套排版，只换图形。 */
const SVG_SYS_INFO =
  '<svg width="15" height="15" viewBox="0 0 24 24" fill="none">' +
  '<circle cx="12" cy="12" r="8.6" stroke="currentColor" stroke-width="1.6"/>' +
  '<path d="M12 8.5v5M12 16.6h.01" stroke="currentColor" stroke-width="1.9" ' +
  'stroke-linecap="round"/></svg>';

const SVG_SYS_OK =
  '<svg width="15" height="15" viewBox="0 0 24 24" fill="none">' +
  '<circle cx="12" cy="12" r="8.6" stroke="currentColor" stroke-width="1.6"/>' +
  '<path d="M8.2 12.3l2.5 2.5 5-5.4" stroke="currentColor" stroke-width="1.9" ' +
  'stroke-linecap="round" stroke-linejoin="round"/></svg>';

const SVG_SYS_BAD =
  '<svg width="15" height="15" viewBox="0 0 24 24" fill="none">' +
  '<path d="M12 3.9l8.5 14.9H3.5z" stroke="currentColor" stroke-width="1.6" ' +
  'stroke-linejoin="round"/>' +
  '<path d="M12 9.6v4.2M12 16.4h.01" stroke="currentColor" stroke-width="1.9" ' +
  'stroke-linecap="round"/></svg>';

/* 右栏条目的图标（.ti.blue / .violet / .teal / .ink 决定底色） */
const SVG_TI_SPIN =
  '<svg class="spin" width="15" height="15" viewBox="0 0 20 20" fill="none">' +
  '<circle cx="10" cy="10" r="7.4" stroke="currentColor" stroke-width="2" ' +
  'stroke-dasharray="11 7"/></svg>';

const SVG_TI_OK =
  '<svg width="15" height="15" viewBox="0 0 20 20" fill="none">' +
  '<path d="M5.4 10.4l3 3 6.2-6.6" stroke="currentColor" stroke-width="2" ' +
  'stroke-linecap="round" stroke-linejoin="round"/></svg>';

const SVG_TI_FAIL =
  '<svg width="15" height="15" viewBox="0 0 20 20" fill="none">' +
  '<path d="M6.4 6.4l7.2 7.2M13.6 6.4l-7.2 7.2" stroke="currentColor" ' +
  'stroke-width="2" stroke-linecap="round"/></svg>';

const SVG_FOLDER =
  '<svg width="16" height="16" viewBox="0 0 24 24" fill="none">' +
  '<path d="M3.6 7.2A2.2 2.2 0 015.8 5h3.1l2 2.2h7.3a2.2 2.2 0 012.2 2.2v7.4a2.2 2.2 0 01-2.2 2.2H5.8a2.2 2.2 0 01-2.2-2.2z" ' +
  'stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg>';

/* 朗读按钮的两个图形。切换语义靠换图标表达，**不靠文字** ——
   气泡下方那行空间很窄，中文字号压到 11px 已经很挤，
   再用「朗读 / 停止」两个词会让按钮宽度跳动，视觉上像在闪。 */
const SVG_SPEAK =
  '<svg width="13" height="13" viewBox="0 0 24 24" fill="none">' +
  '<path d="M4.6 9.4h3.2L12.6 5.6v12.8L7.8 14.6H4.6z" stroke="currentColor" ' +
  'stroke-width="1.6" stroke-linejoin="round"/>' +
  '<path d="M16 9.2c.9 1.6.9 4 0 5.6M18.6 7c1.7 2.8 1.7 7.2 0 10" ' +
  'stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>';

const SVG_SPEAK_STOP =
  '<svg width="13" height="13" viewBox="0 0 24 24" fill="none">' +
  '<rect x="6.6" y="6.6" width="10.8" height="10.8" rx="1.8" ' +
  'stroke="currentColor" stroke-width="1.7"/></svg>';

/* ---------------------------- 运行时状态 ---------------------------- */

const streamEl = $('stream');
const innerEl = $('inner');
const monInner = $('monInner');
const inputEl = $('input');

const items = [];             // 全部条目，按到达顺序（transcript 的真身）
const nodeOf = new Map();     // seq -> DOM 节点

let running = false;
let status = null;
let stick = true;             // 是否跟随底部
let rbTab = 'tools';          // 右栏当前页

// 本轮挂着的附件（**绝对路径**）。路径由宿主弹 OpenFileDialog 给，前端只存
// 不读内容 —— 真正读字节、上传、挂 ref_file_ids 的是引擎。
// 为什么要存在前端而不是 C#：附件条要能显示、能删、能随输入框一起「发出去才
// 消费」，这些都是界面状态；C# 那边持有列表就会变成两处状态同步。
let attachments = [];

/* ---------------------------- 收消息 ---------------------------- */

Bridge.onMessage(handle);

/**
 * 握手。**由前端发起**，不等宿主的 ready。
 *
 * 原来的协议是「宿主发 ready → 前端回 hello」。问题在于这是个死锁窗口：
 * 宿主在 NavigationCompleted 里发 ready，只要那一刻前端的监听器还没装好
 * （脚本被拦、资源 404、加载慢），这条消息就永远丢了，前端会一直停在
 * 「引擎启动中…」，而且不报任何错 —— 它在等一条永远不会再来的消息。
 *
 * DeepSeek Harness 用 __DSH_BOOT_READY__ 这个 deferred 解决同一类问题
 * （见其 lib/main-host 的 READY_MARKUP：让入口先 await 一个 promise，
 *  保证注入的状态一定比消费方先到）。我们这边更简单：宿主在 Navigate
 * 之前就已经挂好了 WebMessageReceived，所以页面脚本一跑就能发 hello。
 *
 * 宿主的 ready 仍然认（保留成第二条路），helloSent 保证只握手一次。
 */
let helloSent = false;

function sayHello() {
  if (helloSent) return;
  helloSent = true;
  send({ t: 'hello' });

  // 问一次朗读后端。**不 await、不阻塞握手** —— 它只影响「点朗读按钮
  // 时走哪条路」，而用户不会在握手那一瞬间点朗读。等它回来再渲染气泡
  // 会让首屏多等一个来回，得不偿失。
  if (window.Tts) {
    window.Tts.refreshEngine().then(() => {
      // 音色面板若已经打开着，后端刚确定，需要重读一次数据源
      // （Fish 与 DeepSeek 的音色列表是两套完全不同的接口）。
      const pop = $('voicePop');
      if (pop && !pop.hidden) openVoicePanel();
    }).catch(() => {});
  }
}

function handle(m) {
  if (!m || typeof m !== 'object') return;
  switch (m.t) {
    case 'ready': sayHello(); break;
    case 'hist': onHistory(m); break;
    case 'item': onItem(m.item); break;
    case 'chg': onChanged(m.item); break;
    // 数据层超过上限、最旧的一条被宿主裁掉。必须跟着删 DOM ——
    // 否则超长对话里数据稳在 600 条、节点却一直涨（实测 740 个）。
    case 'rm': onRemoved(m.seq); break;
    case 'clear': onClear(); break;
    case 'log': onLog(m.log); break;
    case 'status': onStatus(m.running, m.s); break;
    case 'convs': onConvs(m); break;
    case 'models': onModels(m); break;
    // 传输档位清单。宿主在 hello 之后主动推一次，切档位后再推一次。
    // `ready=false` 表示引擎没连上，这份清单不可信 —— 置灰并等下一次。
    case 'transports': onTransports(m); break;
    case 'transportSet': onTransportSet(m); break;
    // 宿主弹完文件选择器回来的回执（{t:'pickFiles'} 的应答）。
    case 'files': onPickedFiles(m.files); break;
    // 接管账号下的会话。成功与否宿主都会重推会话列表（active 跟着变），
    // 这里不需要切视图 —— 列表本来就只有服务端这一个来源。
    case 'attached': break;
    // 身份面板：idstate 是无关联号的状态推送，idres 是某条命令的结果
    // （靠 req 找回它的回调，见「身份 / 凭据」一节），idNeedsLogin 是
    // 宿主发现凭据不可用时主动要求弹面板。
    case 'idstate': idApplyState(m.s, m.error); break;
    case 'idres': idOnResult(m); break;
    case 'idNeedsLogin':
      openIdentity();
      idMessage('凭据已不可用 —— 请重新登录（或用「手动粘贴 token」补一个）。', true);
      break;
    case 'err': onError(m.text); break;
    // 朗读的三条应答（tts / ttsVoices / ttsVoice）由 tts.js 自己接 ——
    // 它按 `req` 关联号找回各自的 Promise，界面层不需要知道细节。
    case 'tts':
    case 'ttsVoices':
    case 'ttsVoice':
      if (window.Tts) window.Tts.handle(m);
      break;
  }
}

function onHistory(m) {
  onClear();
  (m.items || []).forEach(onItem);
  (m.logs || []).forEach(onLog);
  onStatus(m.running, m.s);
  stick = true;
  requestAnimationFrame(() => { streamEl.scrollTop = streamEl.scrollHeight; });
}

function onItem(it) {
  if (!it) return;
  items.push(it);
  const node = renderItem(it);
  if (node) {
    if (it.seq != null) node.dataset.seq = it.seq;
    // 屏外占位高度：必须在 append 之前设好。append 之后再设等于先按
    // 110px 排一次版、再改成估算值，白排一遍。
    const ih = intrinsicHeight(it);
    if (ih != null) node.style.containIntrinsicSize = 'auto ' + ih + 'px';
    innerEl.appendChild(node);
    if (it.seq != null) nodeOf.set(it.seq, node);
  }
  scheduleFlush();
  scrollIfStick();
}

function onChanged(it) {
  if (!it) return;

  // 没见过这条 seq 就当作新条目 —— 必须**直接 return**。
  // 原先是先 items.push(it) 再调 onItem(it)，而 onItem 自己也会 push，
  // 于是同一个 seq 在 items 里出现两次：任务列表会多算一步、
  // 右栏会多一行、「复制全文」会重复一段。这个分支平时走不到
  // （chg 总跟在 item 后面），但历史被截断或消息乱序时就会走到。
  const node = nodeOf.get(it.seq);
  if (!node) { onItem(it); return; }

  const i = items.findIndex((x) => x.seq === it.seq);
  if (i >= 0) items[i] = it;

  patchItem(node, it);
  scheduleFlush();
  scrollIfStick();
}

/** 宿主裁掉了最旧的一条 —— 跟着删节点，保持 DOM 与数据层一致。
 *
 * 只有 `seq` 一个字段，因为节点是靠 `nodeOf` 索引的；不需要重画任何东西。
 * `items` 数组也要同步，否则「复制全文」会把已经裁掉的历史再导出一遍。 */
function onRemoved(seq) {
  if (seq == null) return;
  const node = nodeOf.get(seq);
  if (node) {
    node.remove();
    nodeOf.delete(seq);
  }
  const i = items.findIndex((x) => x.seq === seq);
  if (i >= 0) items.splice(i, 1);
  scheduleFlush();
}

function onClear() {
  items.length = 0;
  nodeOf.clear();
  innerEl.textContent = '';
  scheduleFlush();
}

function onLog(log) {
  if (!log) return;
  const lv = String(log.level || '');
  const line = el('div', 'mon-line');
  // 记住等级，切换「只看重要」时不用重新解析文本
  line.dataset.lv = lv.toLowerCase();
  line.append(
    el('span', 'at', log.at),
    el('span', 'lv ' + lv.toLowerCase(), lv),
    el('span', 'tx', log.text)
  );
  // Faint = 每 5 秒一条的状态快照（[节奏] / [会话]）。默认收起：
  // 它们全是本地读数，混在真日志里只会把有用的行挤走。
  if (line.dataset.lv === 'faint' && $('monHideFaint').checked) {
    line.classList.add('mon-dim');
  }
  monInner.appendChild(line);
  if (monInner.children.length > 800) monInner.removeChild(monInner.firstChild);
  if (!$('monitor').hidden) monInner.scrollTop = monInner.scrollHeight;
}

// 「只看重要」开关：已渲染的行不重排，只切 class —— 取消勾选时历史立刻回来。
$('monHideFaint').addEventListener('change', () => {
  const hide = $('monHideFaint').checked;
  monInner.querySelectorAll('.mon-line').forEach((ln) => {
    ln.classList.toggle('mon-dim', hide && ln.dataset.lv === 'faint');
  });
});

function onError(text) {
  innerEl.appendChild(sysLine('错误', text, 'bad'));
  scrollIfStick();
}

/* 轻量提示条。与 onError 的分工：
 *
 *   onError   → 往对话流里插一条红行，语义是「这一轮任务出错了」，
 *               会永久留在流水里、也会进历史
 *   本函数    → 右下角浮一条几秒后自己消失的提示，语义是「你刚才那个
 *               小动作没成功」（朗读失败、复制失败…）
 *
 * 两者的区别不是视觉风格，是**归属**：朗读是气泡上的一个局部操作，
 * 失败时往全局流水里插红行会让用户以为整轮对话坏了。
 *
 * 只保留一条：连点几次出错的朗读，堆一屏提示比不提示更烦。 */
let toastTimer = 0;
function appToast(text) {
  if (!text) return;
  let n = $('appToast');
  if (!n) {
    n = el('div', 'toast');
    n.id = 'appToast';
    document.body.appendChild(n);
  }
  n.textContent = text;
  n.classList.add('show');
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    toastTimer = 0;
    n.classList.remove('show');
  }, 3200);
}
window.AppToast = appToast;

/* ---------------------------- 渲染分派 ---------------------------- */

function renderItem(it) {
  switch (it.kind) {
    case 'Goal': return msgUser(it);
    case 'Assistant': return msgBot(it);
    case 'Thought': return thoughtBlock(it);
    case 'ToolCall': return toolCard(it);
    case 'TurnDivider': return turnLine(it);
    case 'Thinking': return sysLine('思考中', it.text, 'idle');
    case 'Gate': return sysLine('闸门放行', it.text, 'ok');
    case 'Breaker': return sysLine('熔断', it.text, 'bad');
    case 'Error': return sysLine('错误', it.text, 'bad');
    case 'Notice': return sysLine('提示', it.text, 'warn');
    case 'Summary': return sysLine('摘要', it.text, 'idle');
    case 'Info': return sysLine('信息', it.text, 'idle');
    default: return sysLine(it.kind || '事件', it.text, 'idle');
  }
}

/* ---------------------------- 各类型渲染 ---------------------------- */

function msgUser(it) {
  const d = el('div', 'msg me');
  const av = el('span', 'av', '我');
  const b = el('div', 'bubble');
  b.textContent = it.text || '';
  // .msg.me 是 row-reverse，所以先 av 后 bubble 渲染出来才是「气泡在左、头像在右」
  d.append(av, b);
  return d;
}

function msgBot(it) {
  const d = el('div', 'msg bot');
  const av = el('span', 'av');
  fillAvatar(av);
  const b = el('div', 'bubble');
  renderRich(b, it.text || '', !!it.streaming);

  // 气泡包一层 .body：.msg 是 flex 行（头像 + 气泡），
  // 朗读条要挂在**气泡下方**而不是头像右侧 —— 没有这层的话
  // 它会被当成第三个 flex 项排到右边去。
  const body = el('div', 'body');
  body.appendChild(b);
  const bar = ttsBar(it);
  if (bar) body.appendChild(bar);

  d.append(av, body);
  return d;
}

/* ---------------------------- 朗读 ----------------------------
   只有「本轮定稿的那块助手气泡」带 messageId（见 AgentHost 的 turn_end）。
   没有 id 就不画按钮 —— 画一个点了会报错的按钮比不画更糟。 */
function ttsBar(it) {
  const mid = it.messageId || '';
  if (!mid || it.streaming) return null;

  const bar = el('div', 'tts-bar');
  const btn = el('button', 'tts-btn');
  btn.type = 'button';
  btn.dataset.mid = mid;
  btn.dataset.seq = it.seq == null ? '' : String(it.seq);
  // 正文也挂上：Fish 后端是无状态合成，必须把原文发过去。
  // 挂在 dataset 上而不是闭包里捕获 —— 流式气泡定稿后文本还会被
  // `renderRich` 改写，闭包捕获的是**渲染那一刻**的旧文本。
  btn.dataset.text = it.text || '';
  applyTtsButton(btn);
  btn.addEventListener('click', () => {
    if (!window.Tts) return;
    const seq = Number(btn.dataset.seq);
    // 同一条再点一次 = 停止。这个切换语义在 Tts.play 里判断，
    // 这里不做判断 —— 否则「正在播放的其实是另一条」时会出现
    // 点了本条的按钮却把别人停掉的双重动作。
    window.Tts.play(btn.dataset.mid, seq, btn.dataset.text || '');
  });
  bar.appendChild(btn);
  return bar;
}

/** 按当前播放状态刷新**所有**朗读按钮。
 *
 * 全局刷新而不是只改被点的那一个：同一时刻只可能有一条在朗读，
 * 于是任何状态变化都要把「原来在播的那条」的按钮也翻回「朗读」——
 * 只改自己会让上一条的停止图标一直挂着，看起来像两条都在播。 */
function applyTtsButton(btn) {
  const phase = window.Tts ? window.Tts.phase : 'idle';
  const activeSeq = window.Tts ? window.Tts.seq : null;
  const mine = btn.dataset.seq === '' ? null : Number(btn.dataset.seq);
  const on = phase !== 'idle' && mine != null && mine === activeSeq;

  btn.classList.toggle('on', on);
  btn.classList.toggle('busy', phase === 'loading' && on);
  btn.title = on ? '停止朗读' : '朗读这条消息';
  btn.innerHTML = on ? SVG_SPEAK_STOP : SVG_SPEAK;
  btn.appendChild(el('span', null, on ? '停止' : '朗读'));
}

function refreshTtsButtons() {
  document.querySelectorAll('.tts-btn').forEach(applyTtsButton);
}

window.addEventListener('tts-state', refreshTtsButtons);

function thoughtBlock(it) {
  const th = it.thought || {};
  const d = el('div', 'thought');
  const head = el('div', 'thought-head');
  head.appendChild(el('span', 'ttext', th.header || '思考过程'));
  head.insertAdjacentHTML('beforeend', SVG_CARET_T);
  head.addEventListener('click', () => {
    d.classList.toggle('collapsed');
    d.dataset.userToggled = '1';
  });

  const body = el('div', 'thought-body', it.text || '');
  d.append(head, body);
  // 流式思考默认展开（正在发生的要看得到），结束后自动收起成一行
  if (!th.streaming) d.classList.add('collapsed');
  return d;
}

function toolCard(it) {
  const t = it.tool || {};
  const d = el('div', 'tool');

  const head = el('div', 'tool-head');
  head.append(
    el('span', 'ts'),
    el('span', 'tool-badge', t.kind || 'tool'),
    el('span', 'tool-cmd', t.subject || ''),
    el('span', 'tool-time', '')
  );
  head.insertAdjacentHTML('beforeend', SVG_CARET_TOOL);

  // 手动点过就记住 —— 之后自动折叠/展开**不得把用户的选择顶回去**。
  // 与 WPF 壳 ToolCardView.Expanded 是同一套语义。
  head.addEventListener('click', () => {
    d.classList.toggle('collapsed');
    d.dataset.userToggled = '1';
  });

  const out = el('div', 'tool-out');
  d.append(head, out);

  applyToolState(d, t);
  return d;
}

function turnLine(it) {
  const d = el('div', 'turn');
  d.append(el('span', 'ln'), el('span', 'tx', it.text || ''), el('span', 'ln'));
  return d;
}

/** 状态行：图标 + 「<b>标题</b> · 正文」。tone 取 ok / bad / warn / idle。 */
function sysLine(title, text, tone) {
  const cls = tone === 'bad' ? 'sys bad'
    : tone === 'ok' ? 'sys ok'
    : tone === 'idle' ? 'sys idle'
    : 'sys';
  const d = el('div', cls);
  d.insertAdjacentHTML('beforeend',
    tone === 'bad' ? SVG_SYS_BAD : tone === 'ok' ? SVG_SYS_OK : SVG_SYS_INFO);

  const span = document.createElement('span');
  span.appendChild(el('b', null, title));
  if (text) span.appendChild(document.createTextNode(' · ' + text));
  d.appendChild(span);
  return d;
}

/* ---------------------------- 局部更新 ---------------------------- */

function patchItem(node, it) {
  // 流式回答是**边写边长**的：`Assistant` 的文本每几十毫秒就变一次。
  // 占位高度如果不跟着更新，整段回答会一直按首句的长度占位 ——
  // 屏外时看不出来，一旦滚回来看就是内容溢出占位、下面的块全被顶开。
  // 放在 switch 之前，所有 kind 一起覆盖。
  const ih = intrinsicHeight(it);
  if (ih != null) node.style.containIntrinsicSize = 'auto ' + ih + 'px';

  switch (it.kind) {
    case 'Assistant': {
      const b = node.querySelector('.bubble');
      if (b) renderRich(b, it.text || '', !!it.streaming);
      // 朗读条：流式期间没有（没有 messageId 也就没有条），
      // 定稿那一次 chg 会带着 messageId 来 —— 那时才补上。
      // 用「有没有 / 对不对」而不是「无条件重建」：chg 每秒来很多次，
      // 每次都重建按钮会让它一闪一闪，正在悬停的鼠标也会丢。
      const bar = node.querySelector('.tts-bar');
      const mid = it.messageId || '';
      if (!mid || it.streaming) {
        if (bar) bar.remove();
      } else if (!bar) {
        const nb = ttsBar(it);
        const body = node.querySelector('.body');
        if (nb && body) body.appendChild(nb);
      } else {
        const btn = bar.querySelector('.tts-btn');
        if (btn) {
          // 正文无条件同步：定稿后文本仍可能被改写（例如 markdown
          // 从「半截代码块」变成完整块），而 Fish 念的是 dataset 里
          // 那一份 —— 不同步就会念出旧内容。
          btn.dataset.text = it.text || '';
          if (btn.dataset.mid !== mid) {
            btn.dataset.mid = mid;
            btn.dataset.seq = it.seq == null ? '' : String(it.seq);
            applyTtsButton(btn);
          }
        }
      }
      break;
    }
    case 'Thought': {
      const body = node.querySelector('.thought-body');
      if (body) body.textContent = it.text || '';
      const tx = node.querySelector('.ttext');
      if (tx && it.thought) tx.textContent = it.thought.header || '';
      // 流式结束自动收起；用户手动展开过就不动它
      if (it.thought && !it.thought.streaming && !node.dataset.userToggled) {
        node.classList.add('collapsed');
      }
      break;
    }
    case 'ToolCall': {
      applyToolState(node, it.tool || {});
      break;
    }
    default: {
      const d = node.querySelector('span:last-child') || node;
      d.textContent = it.text || '';
      break;
    }
  }
}

function toolTimeText(t) {
  const s = ((t.elapsedMs || 0) / 1000).toFixed(1) + 's';
  if (t.running) return '运行中 ' + s;
  let out = s + ' · ' + (t.failed ? '失败' : '完成');
  if (t.exitCode) out += ' · 退出码 ' + t.exitCode;
  return out;
}

/** 工具卡的两态：运行中默认展开看实时输出，完成后自动收成一行。 */
function applyToolState(node, t) {
  const ts = node.querySelector('.ts');
  if (ts) ts.innerHTML = t.running ? SVG_SPIN : (t.failed ? SVG_FAIL : SVG_OK);

  const time = node.querySelector('.tool-time');
  if (time) {
    time.textContent = toolTimeText(t);
    time.classList.toggle('live', !!t.running);
  }

  const cmd = node.querySelector('.tool-cmd');
  if (cmd && t.subject) cmd.textContent = t.subject;

  const badge = node.querySelector('.tool-badge');
  if (badge && t.kind) badge.textContent = t.kind;

  // 用户手动干预过就以他的选择为准
  if (!node.dataset.userToggled) {
    node.classList.toggle('running', !!t.running);
    node.classList.toggle('collapsed', !t.running);
  }

  const out = node.querySelector('.tool-out');
  if (!out) return;
  out.textContent = '';
  if (t.running) {
    const live = el('div', 'out-live');
    live.innerHTML = '<i></i>实时输出';
    out.appendChild(live);
  }
  const pre = document.createElement('pre');
  pre.textContent = t.output || (t.running ? '（等待输出…）' : '（无输出）');
  out.appendChild(pre);
}

/* ---------------------------- 富文本 ---------------------------- */

/**
 * 只认 ``` 围栏的轻量扫描 —— 不引 Markdown 库。
 *
 * 为什么不引：瓶颈在渲染不在解析（历史实测 RichTextBox 39.5ms/块被门禁拦下），
 * 而 Web 侧引一个 Markdown 库会带来解析成本 + XSS 面（多数库默认输出 HTML）。
 * 这里只做代码块 + 行内代码 + 段落，全部走 DOM API，零注入面。
 *
 * 代价是代码块没有语法高亮（设计稿的 .code pre .c/.k/.s 用不上）。
 * 要加就得引入 tokenizer 并回到 innerHTML 拼接 —— 那正是上面要避开的，
 * 所以宁可保持单色。
 */
function renderRich(host, text, streaming) {
  host.textContent = '';
  const parts = String(text).split('```');
  for (let i = 0; i < parts.length; i++) {
    const part = parts[i];
    if (i % 2 === 1) {
      const nl = part.indexOf('\n');
      let lang = nl >= 0 ? part.slice(0, nl).trim() : '';
      let code = nl >= 0 ? part.slice(nl + 1) : part;

      // 模型偶尔吐出「``` 后面没换行、直接跟正文」的畸形块。
      // 那时 `nl === -1`，整段既是 lang 又是 code —— 代码被渲染两遍
      // （一遍塞进 `.lg` 标签槽、一遍进 `pre`），高度凭空多一倍。
      //
      // 真实语言名不可能长过十几个字符。超了就当它是正文：
      // 语言槽退回 `text`，内容原样进 `pre`。这样畸形输入只会让
      // 代码块看起来「没标语言」，而不是把块撑高。
      //
      // 这个分支还顺带修掉一个占位误差源：`seq=429` 那块把
      // 102 字的文件路径当成语言名，`.lg` 换行成 3 行、`code-head`
      // 撑到 55px，而 `pre` 是空的 —— 估算 98px vs 实测 158px。
      if (!lang || lang.length > 24 || /\s/.test(lang)) {
        if (lang) code = lang + (code ? '\n' + code : '');
        lang = '';
      }

      host.appendChild(codeBlock(lang, code));
    } else if (part.trim()) {
      appendParagraphs(host, part);
    }
  }
  if (streaming) host.appendChild(el('span', 'cursor'));
}

function appendParagraphs(host, text) {
  text.split(/\n{2,}/).forEach((raw) => {
    const t = raw.replace(/^\n+|\n+$/g, '');
    if (!t) return;
    const p = document.createElement('p');
    appendInline(p, t);
    host.appendChild(p);
  });
}

function appendInline(host, text) {
  const re = /`([^`]+)`/g;
  let last = 0;
  let m;
  const flush = (s) => {
    s.split('\n').forEach((ln, i) => {
      if (i) host.appendChild(document.createElement('br'));
      if (ln) host.appendChild(document.createTextNode(ln));
    });
  };
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) flush(text.slice(last, m.index));
    host.appendChild(el('span', 'icode', m[1]));
    last = m.index + m[0].length;
  }
  if (last < text.length) flush(text.slice(last));
}

function codeBlock(lang, code) {
  const body = code.replace(/\n+$/, '');
  const wrap = el('div', 'code');
  const head = el('div', 'code-head');
  const cp = el('button', 'cp', '复制');
  cp.addEventListener('click', () => copyText(body, cp, '复制'));
  head.append(el('span', 'lg', lang || 'text'), cp);

  const pre = document.createElement('pre');
  pre.textContent = body;
  wrap.append(head, pre);
  return wrap;
}

/* ---------------------------- 剪贴板 ---------------------------- */

function copyText(text, btn, idleLabel) {
  const done = () => {
    if (!btn) return;
    btn.textContent = '已复制';
    setTimeout(() => { btn.textContent = idleLabel; }, 1200);
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done).catch(() => fallbackCopy(text, done));
  } else {
    fallbackCopy(text, done);
  }
}

function fallbackCopy(text, done) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.cssText = 'position:fixed;left:-9999px;top:0';
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand('copy'); done(); } catch (e) { /* 忽略 */ }
  document.body.removeChild(ta);
}

/**
 * 把「派生视图」的重建合并到一帧里。
 *
 * 为什么必须有：宿主灌历史时是**一口气推几百条** item 的。如果每条都立刻
 * 重建任务列表和右栏，就是 600 次「全量过滤 items + 造 40 行 DOM」——
 * 实测 600 条要 1.6 秒（O(n²)）。合并到一帧之后同一批只重建一次。
 *
 * 任务列表和右栏都是**投影**，不是数据源，晚一帧画出来没有任何语义损失。
 */
let flushPending = false;

/**
 * rAF 和定时器双保险。
 *
 * 只用 rAF 有个坑：**窗口被遮挡或最小化时合成器停了，rAF 就不触发**，
 * 于是右栏和任务列表会一直停在旧状态，直到窗口重新可见。定时器在被遮挡时
 * 只是被节流（约 1s 一次），不会停，所以拿它兜底。两个都挂上，谁先到谁干活，
 * flushPending 保证只干一次。
 */
function scheduleFlush() {
  if (flushPending) return;
  flushPending = true;
  const run = () => {
    if (!flushPending) return;
    flushPending = false;
    refreshTodo();
    if (rbTab === 'tools') renderTools();
    else if (rbTab === 'proj') renderProj();
  };
  requestAnimationFrame(run);
  setTimeout(run, 32);
}

/* ---------------------------- 任务列表 ---------------------------- */

/**
 * 从 transcript 投影出「当前这一轮的步骤」。
 *
 * 这是**投影**，不是新通道 —— 数据全部来自已有的 ToolCall 条目，
 * 引擎协议一行没动。起点取最后一个 Goal（用户那一轮的开始）。
 */
function currentSteps() {
  let start = 0;
  for (let i = items.length - 1; i >= 0; i--) {
    if (items[i].kind === 'Goal') { start = i + 1; break; }
  }
  return items.slice(start).filter((x) => x.kind === 'ToolCall' && x.tool);
}

function refreshTodo() {
  const box = $('todo');
  const steps = currentSteps();

  // 只在「运行中且这一轮确实有工具步骤」时出现
  if (!running || steps.length === 0) { box.hidden = true; return; }
  box.hidden = false;

  const doneCount = steps.filter((s) => !s.tool.running).length;
  const cur = steps.find((s) => s.tool.running);
  const curIdx = Math.min(doneCount + (cur ? 1 : 0), steps.length);

  $('tdCur').textContent = cur ? (cur.tool.label || cur.tool.subject || '执行中') : '本轮已完成';
  $('tdStep').textContent = '第 ' + Math.max(curIdx, 1) + ' / ' + steps.length + ' 步';
  $('todoBar').style.width = (doneCount / steps.length * 100).toFixed(1) + '%';

  const list = $('todoList');
  list.textContent = '';
  steps.forEach((s, i) => {
    const state = s.tool.running ? 'doing' : (s.tool.failed ? 'fail' : 'done');
    const row = el('div', 'td-item ' + state);
    const ic = el('span', 'tdi');
    ic.innerHTML = s.tool.running ? SVG_SPIN : (s.tool.failed ? SVG_FAIL : SVG_OK);
    row.append(
      ic,
      el('span', 'tdt', s.tool.label || s.tool.subject || ('步骤 ' + (i + 1))),
      el('span', 'tds', s.tool.running ? '进行中…' : (s.tool.failed ? '失败' : '已完成'))
    );
    list.appendChild(row);
  });
}

/* ---------------------------- 状态 ---------------------------- */

function onStatus(isRunning, s) {
  running = !!isRunning;
  status = s || status;

  const pill = $('pill');
  const txt = $('pillText');
  pill.className = 'pill';
  let label = '运行正常';
  let tone = 'ok';

  // 优先级与 WPF 壳的 HealthPill 逐条对齐：
  //   停机 > 熔断 > 链路不可用 > 未登录 > 闸门未放行 > 服务端忙 > 正常。
  // 别写成「不是 none 就算熔断」—— 引擎的常态值是 "closed"（闭路 = 正常），
  // 那样会一直显示「已熔断」（这个 bug 实测出现过）。
  if (!s || !s.started) {
    tone = 'idle'; label = '引擎启动中…';
  } else if (s.circuitLevel === 'halted') {
    tone = 'bad'; label = '已停机 · 需人工介入';
  } else if (s.circuitLevel === 'open') {
    tone = 'bad'; label = '熔断中';
  } else if (!s.transportReady) {
    tone = 'bad'; label = '链路不可用';
  } else if (s.accountSuspended) {
    // 账号被服务端处置（封禁）。**排在「未登录」之前** —— 处置的账号
    // 通常仍有 token，若落到 hasToken 分支就会被判成「正常」，
    // 而那正是用户看到的假象：显示运行正常，实际一个字都发不出去。
    tone = 'bad'; label = '账号被处置';
  } else if (!s.hasToken) {
    tone = 'warn'; label = s.identityRefreshing ? '正在刷新凭据…' : '未登录';
  } else if (!s.gateAllowed) {
    const secs = Math.round((s.gateWaitMs || 0) / 100) / 10;
    // L4 是硬性拒绝，不是「等一会儿就好」，配色要能区分出来
    tone = /L4/.test(s.gateReason || '') ? 'bad' : 'warn';
    label = secs > 0 ? '闸门等待 ' + secs + 's' : '闸门阻止';
  } else if (s.busyStreak > 0) {
    tone = 'warn'; label = '服务端忙 ' + s.busyStreak + '/3';
  } else if (running) {
    tone = 'ok'; label = '运行中';
  }
  pill.classList.add(tone);
  if (running && tone === 'ok') pill.classList.add('run');
  txt.textContent = label;

  if (s) {
    $('mhSub').textContent = s.convTitle || s.thinkingMode || '—';
    $('chipThinking').classList.toggle('on', !!s.thinking);
    $('chipSearch').classList.toggle('on', !!s.search);
    if (s.model) $('selModel').value = s.model;

    const name = s.accountSuspended
      ? '账号被处置'
      : (s.hasToken ? (s.identityLabel || '本地凭据') : '未登录 · 点此登录');
    $('footName').textContent = name;
    $('footAvatar').textContent = (s.identityLabel || '?').trim().charAt(0).toUpperCase() || '?';
    // 未登录时页脚整块转琥珀。凭据失效是**阻塞性**故障（一个字都发不出去），
    // 不能让用户在一堆绿色里自己找出口。账号被处置同理，且更严重。
    $('footIdentity').classList.toggle('warn', !s.hasToken || !!s.accountSuspended);
  }

  $('btnRun').hidden = running;
  $('btnCancel').hidden = !running;
  // 熔断重置只在熔断/停机时露出来。`halted` 也放出来 —— 那是更严重的一档，
  // 用户更需要一个「试着恢复」的入口；点下去会不会成功由宿主决定，界面不猜。
  const breakerDown = !!s && (s.circuitLevel === 'open' || s.circuitLevel === 'halted');
  $('btnResetBreaker').hidden = !breakerDown;
  // 身份面板开着时，状态推送里新出现的「账号被处置」要立刻反映到面板上 ——
  // 否则用户得关掉重开才看得到。
  if (idOpen && idLast) idApplyState(idLast);
  scheduleFlush();
  syncConf();
}

/* ---------------------------- 会话列表 ----------------------------

   **列表只有一个来源：账号下真实存在的服务端会话**（`chat_session/fetch_page`）。

   为什么不再列本地对话：`conversations/*.json` 不记录归属账号。换号之后，
   上一个账号的对话会继续列在这里 —— 点进去发消息，引擎会拿着**新 token**
   去请求一个不属于它的 `chat_session_id`（`deepseek_rpc.py` 里
   `if not session.cid` 才新建，否则直接复用本地 cid），服务端必然拒绝
   （同类场景实测：HTTP 400 + 空 body，界面只显示「未收到正文」）。
   而且「跨账号访问他人会话」在风控眼里是异常模式。

   所以本地文件降级成**缓存**：接管过的会话仍在本地留一份（离线可看），
   但列表的事实来源只有服务端。换号后列表自动跟着账号变 ——
   不需要给对话标 `account_id`，也就不存在「这条属于谁」的分类问题。

   代价：新建对话在**发出第一条消息之前**不会出现在列表里（它还没有
   `chat_session_id`）。这与 ChatGPT 的行为一致，不是缺陷。 */

let convs = null;          // null = 还没拉过
let convFailed = false;
let convBusy = false;

function onConvs(m) {
  // 宿主仍可能推本地列表（`scope: 'local'`）—— 直接忽略，列表只认服务端。
  if (m.scope !== 'remote') return;
  convs = m.items || [];
  convFailed = !!m.failed;
  convBusy = false;
  renderConvs();
}

function renderConvs() {
  const box = $('chatList');
  box.textContent = '';

  // 数量写进 aria-label：屏幕阅读器进入这个 group 时会先报
  // 「账号下的会话，共 51 条」，用户才知道要 Tab 多久。
  // 三种非正常态（失败 / 加载中 / 空）各自报各自的状态，不报数量。
  const setLabel = (s) => box.setAttribute('aria-label', s);

  // 失败优先说「失败」—— 「没有会话」会让用户以为自己没数据，
  // 而实际上他只是凭据过期了（原因已经写进对话流里那条红色错误行）。
  if (convFailed) {
    setLabel('账号下的会话，读取失败');
    box.appendChild(el('div', 'side-empty', '读取失败（原因见对话流）'));
    return;
  }
  if (convs === null) {
    setLabel('账号下的会话，正在读取');
    box.appendChild(el('div', 'side-empty', '正在读取账号下的会话…'));
    return;
  }
  if (!convs.length) {
    setLabel('账号下的会话，暂无');
    box.appendChild(el('div', 'side-empty', '账号下还没有会话'));
    return;
  }

  setLabel('账号下的会话，共 ' + convs.length + ' 条');

  convs.forEach((c) => {
    // 外层是「可点击的控件」，内层再嵌一个「更多操作」按钮。
    //
    // 为什么按钮不能直接放进 role="button" 的行里：嵌套可交互控件在
    // 可访问性树里是非法的 —— 屏幕阅读器会把内层按钮吞掉（报「按钮」时
    // 不报里面还有东西），键盘焦点也会在两层之间打架。所以拆成：
    //   .chat-row（布局容器，无角色）
    //     ├ .chat     role=button   ← 接管会话
    //     └ .chat-more role=button  ← 打开操作菜单
    const wrap = el('div', 'chat-row' + (c.active ? ' on' : ''));

    const row = el('div', 'chat' + (c.active ? ' on' : ''));

    // 这一行是「可点击的控件」，不是一段静态文字。为什么必须显式声明：
    // <div> 默认没有角色，WebView2 的 UIA 树会把它报成 ControlType.Text ——
    // 屏幕阅读器读到「城市光污染文章」，用户以为那只是标题，不会去点；
    // 键盘也 Tab 不到（div 不进 tab 序列）。WPF 壳用的是真按钮，没这问题，
    // 这是重构到 HTML 时丢掉的语义。
    //
    // 为什么不用 role="listitem"：那样只说明「这是列表里的一项」，
    // 不说明「可以按」。可点性比列表结构更关键 —— 用 role="button"。
    // 容器侧用 role="group" + aria-label 补回「这是一组会话」的上下文。
    row.setAttribute('role', 'button');
    row.tabIndex = 0;                       // 进入 Tab 序列
    row.dataset.id = c.id;                  // 供测试与排查定位，不用再靠标题猜
    // 本地对话 id 单独存一份：切换 / 重命名 / 同步三个动作在宿主侧都按
    // **本地** id 工作，而 `id` 是服务端 id。未接管的会话没有本地 id。
    if (c.localId) row.dataset.localId = c.localId;
    // aria-current 让屏幕阅读器报「当前项」，对应视觉上的 .on 高亮。
    if (c.active) row.setAttribute('aria-current', 'true');

    row.appendChild(el('div', 't', c.title || '（未命名）'));
    row.appendChild(el('div', 'm', c.subtitle || ''));

    // 打开这条对话。两条路径，代价差一个数量级：
    //
    //   · **已接管**（有 localId）→ `switchConv`：纯本地读盘
    //     （`ConversationLoadAsync`）+ 重放，零网络往返。宿主再在后台
    //     悄悄同步一次服务端历史，不挡切换。
    //   · **未接管** → `attachSession`：必须联网把服务端历史拉全量回来，
    //     顺便校正续接锚点。慢，但这是唯一能把线上会话接进本地的路径。
    //
    // 早先只有 attachSession 一条路，于是「点一条已经打开过的对话」
    // 也要走一遍联网接管 —— 明明本地就有全量历史。实测那条路径在
    // 弱网下要等几秒，用户会以为点击没生效而反复点。
    const attach = () => (c.localId
      ? send({ t: 'switchConv', id: c.localId })
      : send({ t: 'attachSession', id: c.id }));
    row.addEventListener('click', attach);
    // 键盘等价路径。role="button" 的语义要求 Enter/Space 都触发 ——
    // 只处理 Enter 会让「空格按下去没反应」，而这正是屏幕阅读器用户的默认操作。
    row.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ' || e.key === 'Spacebar') {
        e.preventDefault();   // 空格默认会滚动容器，必须拦掉
        attach();
      }
    });

    // 「更多操作」。为什么做成独立按钮而不是右键菜单：
    // 右键在触控板和键盘上都不好触发，而且屏幕阅读器用户根本发现不了
    // 「这里有右键菜单」。常驻一枚小按钮，键盘能 Tab 到，才谈得上可用。
    const more = el('button', 'chat-more');
    more.type = 'button';
    more.title = '更多操作';
    more.setAttribute('aria-haspopup', 'menu');
    more.setAttribute('aria-label', `「${c.title || '未命名'}」的更多操作`);
    more.innerHTML =
      '<svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true">'
      + '<circle cx="8" cy="3" r="1.35" fill="currentColor"/>'
      + '<circle cx="8" cy="8" r="1.35" fill="currentColor"/>'
      + '<circle cx="8" cy="13" r="1.35" fill="currentColor"/></svg>';
    more.addEventListener('click', (e) => {
      e.stopPropagation();      // 别把「打开菜单」当成「接管会话」
      openConvMenu(more, c);
    });

    wrap.appendChild(row);
    wrap.appendChild(more);
    box.appendChild(wrap);
  });
}

/* ---------------------- 会话行的操作菜单 ----------------------

   四个动作里，**只有接管（attachSession）能作用于未接管的会话**。
   另外三个都按本地 id 工作：

     · 切换（switchConv）  —— 本地已有这条对话时才有意义
     · 重命名（renameConv）—— 只改本地标题，服务端没有改名接口
     · 同步（syncConv）    —— 从服务端重拉历史，得有本地对话才能存

   所以未接管的行里，这三项要**禁用并说明原因**，而不是藏起来。
   藏起来的话用户不知道有这个功能；禁用 + 一句原因，用户知道
   「先接管，然后就能改」。

   删除（delConv）是唯一按**服务端 id** 工作的 —— 它删的就是账号里那条。 */

let convMenuFor = null;      // 当前菜单绑定的会话项

function openConvMenu(anchor, c) {
  const menu = $('convMenu');
  convMenuFor = c;
  menu.textContent = '';

  const bound = !!c.localId;

  const items = [
    {
      key: 'attach', text: c.active ? '这条已经打开' : '打开这条对话',
      disabled: !!c.active,
      hint: bound ? '本地已有这条对话，瞬时切换' : '需要联网把服务端历史拉回来',
      // 与行点击走同一条分叉：已接管走本地，未接管才联网
      run: () => (bound
        ? send({ t: 'switchConv', id: c.localId })
        : send({ t: 'attachSession', id: c.id })),
    },
    {
      key: 'rename', text: '重命名…',
      disabled: !bound,
      hint: bound ? '只改本地标题，网页版里不会跟着变' : '先打开这条对话才能重命名',
      run: () => askRename(c),
    },
    {
      key: 'sync', text: '从服务端同步历史',
      disabled: !bound,
      hint: bound ? '补齐在网页版里聊过的内容' : '先打开这条对话才能同步',
      run: () => send({ t: 'syncConv', id: c.localId }),
    },
    { key: 'sep', sep: true },
    {
      key: 'del', text: '从账号里删除…',
      danger: true,
      run: () => askDelete(c),
    },
  ];

  for (const it of items) {
    if (it.sep) {
      const line = el('div', 'cmenu-sep');
      line.setAttribute('role', 'separator');
      menu.appendChild(line);
      continue;
    }
    const b = el('button', 'cmenu-item' + (it.danger ? ' danger' : ''));
    b.type = 'button';
    b.setAttribute('role', 'menuitem');
    b.tabIndex = -1;
    b.textContent = it.text;
    if (it.disabled) {
      b.disabled = true;
      b.setAttribute('aria-disabled', 'true');
    }
    // 说明文字做成 title 而不是第二行：菜单要窄，两行会把「删除」挤出视口。
    // 屏幕阅读器能读到 title，鼠标悬停也能看到。
    if (it.hint) b.title = it.hint;
    b.addEventListener('click', () => {
      closeConvMenu();
      if (!it.disabled) it.run();
    });
    menu.appendChild(b);
  }

  // 定位：贴着按钮左下角，超出视口就翻到上方。
  // 用 fixed + 视口坐标 —— 列表可滚动，绝对定位会被 overflow 裁掉。
  menu.hidden = false;
  menu.style.left = '0px';
  menu.style.top = '0px';
  const a = anchor.getBoundingClientRect();
  const m = menu.getBoundingClientRect();
  let left = a.right - m.width;
  let top = a.bottom + 4;
  if (left < 8) left = 8;
  if (top + m.height > window.innerHeight - 8) top = a.top - m.height - 4;
  menu.style.left = Math.round(left) + 'px';
  menu.style.top = Math.round(top) + 'px';

  // 焦点进菜单第一项 —— 键盘用户可以立刻用方向键走
  const first = menu.querySelector('.cmenu-item:not([disabled])');
  if (first) first.focus();
}

function closeConvMenu() {
  const menu = $('convMenu');
  if (menu.hidden) return;
  menu.hidden = true;
  convMenuFor = null;
}

/* ---------------------- 通用小对话框 ----------------------

   openDialog 只负责「问」，不负责「做」。动作由调用方在 onOk 里执行 ——
   这样同一个对话框既能问「新标题是什么」，也能问「确定删吗」。 */

let dlgOnOk = null;
let dlgOnCancel = null;

function openDialog({ title, text, input, value, okText, danger, onOk }) {
  $('dlgTitle').textContent = title;
  $('dlgText').textContent = text || '';
  const inp = $('dlgInput');
  inp.hidden = !input;
  inp.value = input ? (value || '') : '';
  $('dlgOk').textContent = okText || '确定';
  $('dlgOk').classList.toggle('danger', !!danger);
  $('dlgMask').hidden = false;
  dlgOnOk = onOk;
  dlgOnCancel = null;
  // 有输入框就聚焦并全选：重命名的默认值是旧标题，用户通常想整个换掉
  if (input) setTimeout(() => { inp.focus(); inp.select(); }, 0);
  else setTimeout(() => $('dlgOk').focus(), 0);
}

function closeDialog() {
  $('dlgMask').hidden = true;
  dlgOnOk = null;
  dlgOnCancel = null;
}

function askRename(c) {
  openDialog({
    title: '重命名对话',
    // 必须说清楚「只改本地」——服务端没有改名接口，谎称会同步
    // 会让用户去网页版找那条改过的标题，然后发现没变。
    text: '新标题（只改本地的这条记录，网页版里不会跟着变）：',
    input: true,
    value: c.title || '',
    okText: '保存',
    onOk: () => {
      const t = $('dlgInput').value.trim();
      if (!t) return;                 // 空标题直接忽略，不关对话框
      closeDialog();
      send({ t: 'renameConv', id: c.localId, title: t });
    },
  });
}

function askDelete(c) {
  openDialog({
    title: '从账号里删除会话',
    // 文案照抄 WPF 壳（MainWindow.xaml.cs BtnDeleteRemote_Click）的要点：
    // 不可逆、影响范围（网页版一起消失）、本地绑定会怎样。
    // 少一句「无法撤销」，用户就会以为还能找回来。
    text: `「${c.title || '未命名'}」会从账号里永久删除，网页版里也会一起消失，无法撤销。`
      + (c.localId
        ? '\n\n本地这条对话不会被删，但会解除绑定 —— 之后发的消息会新建一条会话。'
        : '\n\n本地没有绑定这条会话，不受影响。'),
    okText: '删除',
    danger: true,
    onOk: () => {
      closeDialog();
      send({ t: 'delConv', id: c.id });
    },
  });
}

/** 重新联网拉一次账号下的会话列表。 */
function refreshConvs() {
  if (convBusy) return;
  convBusy = true;
  convFailed = false;      // 把「读取失败」换回「正在读取…」
  renderConvs();
  send({ t: 'listRemote' });
}

function onModels(m) {
  const sel = $('selModel');
  sel.textContent = '';
  const def = document.createElement('option');
  def.value = '';
  def.textContent = '账号默认';
  sel.appendChild(def);
  (m.items || []).forEach((mo) => {
    const o = document.createElement('option');
    o.value = mo.name;
    o.textContent = mo.display || mo.name || '（默认）';
    if (!mo.available) o.disabled = true;
    sel.appendChild(o);
  });
  sel.value = m.current || '';
}

/* ---------------------------- 传输档位 ---------------------------- */

/**
 * 档位清单回填。
 *
 * ## 为什么选项不写死在 index.html 里
 *
 * 「这个档位能不能用」取决于三件只有引擎知道的事：`gemini-webapi` 装没装、
 * Gemini 的 cookie 文件在不在、当前是不是访客模式。写死的话这三项任何一项
 * 变化都要改 HTML，而且必然与引擎实际状态漂移 —— 用户会看到「可以选」
 * 但选完起不来。
 *
 * ## `available` 与 `ready` 不能合成一个
 *
 *   · `available=false` —— 依赖没装，选了必然失败。**置灰**。
 *   · `ready=false` —— 装了但还没配好（没 cookie）。**可以选**，因为
 *     用户正是要选它、然后去补凭据。锁死它反而堵住了配置路径。
 *
 * 原生 `<select>` 对 `option:disabled` 的支持不统一（Safari 会忽略），
 * 所以可用性主要靠下面那行 note 说明，`disabled` 只是尽力而为。
 */
function onTransports(m) {
  const sel = $('selTransport');
  if (!sel) return;
  const items = m.items || [];

  sel.textContent = '';
  if (!items.length) {
    const o = document.createElement('option');
    o.value = '';
    o.textContent = m.ready === false ? '引擎未就绪' : '没有可用档位';
    sel.appendChild(o);
    sel.disabled = true;
    transportCatalog = [];
    transportNote('');
    return;
  }

  items.forEach((it) => {
    const o = document.createElement('option');
    o.value = it.name;
    o.textContent = it.label || it.name;
    if (it.available === false) o.disabled = true;
    sel.appendChild(o);
  });

  // 用 `m.current`（引擎报的裸名）选中，不用 `status.transport` ——
  // 后者在链路不可用时会被宿主改写成中文「不可用」（见 ApplyStatus）。
  sel.value = m.current || '';
  sel.disabled = false;
  transportCatalog = items;
  transportNote(describeTransport(sel.value, items));
}

/** 当前选中项的说明文案。不可用 / 未配置要用不同措辞，别混成一句。 */
function describeTransport(name, items) {
  const it = (items || []).find((x) => x.name === name);
  if (!it) return '';
  if (it.available === false) {
    return (it.label || name) + ' 在这台机器上还不可用：' + (it.note || '缺少依赖') +
      '（切换过去会启动失败）';
  }
  if (it.ready === false) {
    return '尚未配置：' + (it.note || '缺少凭据') + '。选它之后去补凭据即可。';
  }
  return it.note || '';
}

function transportNote(text) {
  const el = $('transportNote');
  if (!el) return;
  el.textContent = text || '';
  el.classList.toggle('warn', /不可用|尚未配置|失败/.test(text || ''));
}

/** 切档位的结果回执。成功时宿主会同时重推 status 与清单，这里只报失败。 */
function onTransportSet(m) {
  if (m.ok) return;
  const it = (transportCatalog || []).find((x) => x.name === m.name);
  transportNote('切换到 ' + ((it && it.label) || m.name) + ' 失败 —— 详情见左侧对话流。');
}

/* ---------------------------- 滚动跟随 ---------------------------- */

streamEl.addEventListener('scroll', () => {
  const gap = streamEl.scrollHeight - streamEl.scrollTop - streamEl.clientHeight;
  stick = gap < 40;
});

/**
 * 跟随底部。**只读一次 scrollHeight、只写一次 scrollTop，而且都在下一帧**。
 *
 * 之前是同步写 + RAF 里再校一次 —— 批量灌 600 条时等于每插一条就强制一次
 * 布局（O(n²) 的另一个来源）。写操作合并到一帧里，观感一样，成本降到 O(1)。
 */
let scrollPending = false;

function scrollIfStick() {
  if (!stick || scrollPending) return;
  scrollPending = true;
  const run = () => {
    if (!scrollPending) return;
    scrollPending = false;
    if (!stick) return;
    streamEl.scrollTop = streamEl.scrollHeight;
    // content-visibility:auto 下 scrollHeight 是估算值，下一帧再校一次
    requestAnimationFrame(() => {
      if (stick) streamEl.scrollTop = streamEl.scrollHeight;
    });
  };
  requestAnimationFrame(run);
  setTimeout(run, 32);        // 与 scheduleFlush 同理：遮挡时 rAF 会停
}

/* ---------------------------- 输入 ---------------------------- */

function autoGrow() {
  inputEl.style.height = 'auto';
  // 设计稿给 textarea 定了 min-height:56px / max-height:150px，这里跟着它走
  inputEl.style.height = Math.min(Math.max(inputEl.scrollHeight, 56), 150) + 'px';
}

inputEl.addEventListener('input', autoGrow);

inputEl.addEventListener('keydown', (e) => {
  // 中文输入法回车 = 「确认候选词」，不能当发送。
  // 少了这道闸，打中文时按回车会把半截拼音（zhongwen）直接发出去。
  //
  // 为什么要判两个条件，而不是只判 isComposing：
  //   确认候选词那一下，Chromium 给的 keydown 是
  //     { key:'Enter', isComposing:true, keyCode:229 }  ← isComposing 挡得住
  //   但还流传着另一种形态 —— 输入法已经结束合成、isComposing 归 false，
  //   而被输入法吃掉的那次按键在 keyCode 上仍留 229：
  //     { key:'Enter', isComposing:false, keyCode:229 } ← isComposing 挡不住
  //   实测（headless 探针，KeyboardEventInit 确实接受 keyCode）确认这条会漏，
  //   漏出去的正是半截拼音。所以再补一道 keyCode !== 229。
  //
  //   为什么补这一道是安全的：真按回车 keyCode 是 13；keyCode 229 的定义就是
  //   「这次按键被输入法消费了」。所以 229 只可能出现在"不该发送"的场景里。
  const imeComposing = e.isComposing || e.keyCode === 229;
  if (e.key === 'Enter' && !e.shiftKey && !imeComposing) {
    e.preventDefault();
    doSend();
  }
});

function doSend() {
  const text = inputEl.value.trim();
  // 空输入且无附件：确实没什么可发的，安静返回。
  // 注意判的是**两者皆空** —— 只发文件、一个字不写是合法用法（WPF 壳同一条语义）。
  if (!text && attachments.length === 0) return;

  // ⚠️ 这里**不能**再拦 `running`。这是 WPF 壳刻意修掉的老写法：
  //   它原来的 `if (goal.Length == 0 || Host.Running) return;` 是静默返回 ——
  //   上一轮还在跑时按 Enter「什么都不发生」：输入框没清、对话流没提示，
  //   用户只能得出「Enter 坏了」。而实际上最常见的原因就是「上一轮还没跑完」
  //   （思考模式下一轮约 1 分钟），期间按多少次 Enter 都是这一句话。
  //   更糟的是这道判断挡在宿主之前，把宿主里那条友好提示也一并吞掉了。
  //   现在：照常提交，由宿主输出可见原因（AgentHost.SubmitAsync 里那两条 Add）。
  //
  // 但「清空输入框」仍然只在**本轮发得出去**时才做 —— 发不出去就留着用户
  // 已经打好的字，别让一次提示把他辛苦输的内容吃掉。
  // 这一条与 WPF 壳的 Host.CanSubmit 是同一个语义（CanSubmit == !Running）。
  //
  // ⚠️ **熔断 / 停机也必须走「不消费」这条路**（2026-10-18 旅程 8 实测）：
  //   原来只判 `!running`，于是熔断时点发送会清空输入框、消费掉附件，
  //   然后把 `submit` 交给宿主 —— 而引擎在门禁处直接拒绝，一个字都没发出去。
  //   用户看到的是「我打的字没了，什么都没发生」。
  //
  //   更隐蔽的是引擎侧 `_await_gate()` 的 gate 事件**只在理由变化时上报一次**。
  //   熔断期间反复点发送，理由一直是同一句，所以第二次之后界面上连那条
  //   gate 提示都不会再出现 —— 用户彻底失去反馈。
  //
  //   所以这里不是「加个 if 拦住」（那会重蹈静默返回的覆辙），而是
  //   **照常提交、但不清空**：宿主仍有通道给出可见原因，用户的输入也还在。
  const blocked = isHardBlocked();
  if (!running && !blocked) {
    inputEl.value = '';
    autoGrow();
    stick = true;
    // 附件与输入框同一条 gate：发得出去才消费（清空输入框 = 发得出去）。
    // 发不出去时附件留着，下一次还能发 —— 别让一次「上一轮还在跑」把用户
    // 刚挑好的五个文件悄悄吃掉。
    const files = attachments.slice();
    attachments = [];
    renderAttach();
    send({ t: 'submit', goal: text, files });
    return;
  }
  // running 或硬性阻断时照常提交（宿主会给可见提示），附件**不消费**。
  send({ t: 'submit', goal: text, files: attachments.slice() });
}

/** 当前状态是否属于「本轮一定发不出去」。
 *
 * 只判**硬性阻断**，不判 `transportReady` / `hasToken` / `accountSuspended`：
 *
 *   · 链路不可用、未登录、账号被处置 —— 这些确实也发不出去，但它们的恢复
 *     往往是「下一秒就好了」（重连成功 / 用户刚点了登录 / 服务端解封）。
 *     在这种状态下扣住输入框，用户会以为软件卡死。让提交走完、由引擎报错，
 *     用户反而能从错误里看出到底哪一步不通。
 *
 *   · 熔断（open）与停机（halted）不同：它们**有明确的退避时长**，
 *     期间无论点多少次都不可能成功。这才是该扣住输入的状态。
 *
 * 与 `onStatus()` 的胶囊优先级链用的是同一套判据 —— 两处必须一致，
 * 否则会出现「胶囊显示熔断中、但输入框照常清空」的自相矛盾。 */
function isHardBlocked() {
  const lv = status && status.circuitLevel;
  return lv === 'open' || lv === 'halted';
}

/* ---------------------------- 附件条 ----------------------------
   三种进法，最后都落到同一个 `attachments`（绝对路径表）：

     ① 「附件」按钮  → 宿主弹 OpenFileDialog
                        → {t:'pickFiles'} → 宿主回 {t:'files', files:[…]}
     ② 拖文件进窗口  → postMessageWithAdditionalObjects → 宿主回同一格式
     ③ Ctrl+V 粘贴   → 同 ②（dataTransfer 里同样有 File 对象）

   ②③ 能成立的关键，是一条**曾经被我写错过的**事实：

     浏览器的 File 对象确实只有内容、没有本地路径 —— 这条对纯浏览器成立。
     但 WebView2 **不是**纯浏览器，它嵌在宿主进程里，为此专门开了一条旁路：

         chrome.webview.postMessageWithAdditionalObjects(msg, [file])
           → C# WebMessageReceivedEventArgs.AdditionalObjects
           → CoreWebView2File.Path    ← 磁盘绝对路径

     官方文档对 Path 的原话：
       "You can use this object to obtain the path of a File dropped on WebView2."

     而且 `CoreWebView2Controller.AllowExternalDrop` 默认就是 true，
     **不需要任何额外开关**。

   （此处原来写着「②③ 在这个壳里做不到，要开 AdditionalBrowserArguments
     且行为随版本变」—— 那是没查证的臆断，已由 `.tmp/verify/probe-drop-api.py`
     实测推翻：9 项全通过。）

   所以三种进法殊途同归：**前端一个字节都不读**，只把路径交给引擎，
   由引擎自己去读文件、算哈希、上传。 */

const attachEl = $('attach');

function renderAttach() {
  attachEl.textContent = '';
  if (attachments.length === 0) {
    attachEl.hidden = true;
    return;
  }
  attachEl.hidden = false;
  attachments.forEach((path, i) => {
    // 只显示文件名 —— 完整路径可能很长，放 title 里悬停看。
    const name = path.split(/[\\/]/).pop() || path;
    const pill = el('span', 'ap');
    pill.title = path;
    pill.appendChild(el('span', null, name));
    const x = el('button', 'x', '×');
    x.type = 'button';
    x.title = '移除这个附件';
    x.addEventListener('click', () => {
      attachments.splice(i, 1);
      renderAttach();
    });
    pill.appendChild(x);
    attachEl.appendChild(pill);
  });
}

/** 宿主回的选择结果。去重 + 过滤空串 —— 同一个文件选两次不该出现两个药丸。 */
function onPickedFiles(list) {
  if (!Array.isArray(list)) return;
  for (const raw of list) {
    const p = String(raw || '').trim();
    if (!p || attachments.includes(p)) continue;
    attachments.push(p);
  }
  renderAttach();
}

/* ---------- 拖拽 / 粘贴进附件条 ---------- */

/**
 * 把一批 File 对象交给宿主换绝对路径。
 *
 * 这里**只传 File 对象本身**，不读内容、不转 base64 —— 宿主从
 * `AdditionalObjects` 里直接拿 `CoreWebView2File.Path`（详见 bridge.js
 * 的 sendFiles 注释）。回执走 `{t:'files'}`，与「附件」按钮同一个通道，
 * 所以 `onPickedFiles()` 不用改。
 *
 * @param {FileList|File[]} fileList
 */
function submitFiles(fileList) {
  const files = Array.from(fileList || []);
  if (files.length === 0) return;

  // 不在这里判「是不是目录」。Chromium 给目录的 File 是 size=0、type=''，
  // 而这与「没有扩展名的空文件」完全无法区分 —— 猜错就会把合法文件挡掉。
  // 判据交给宿主与引擎：它们拿得到真实路径，`Directory.Exists` /
  // `Path.is_file()` 是一句确定的系统调用，不需要猜。

  const r = Bridge.sendFiles({ t: 'droppedFiles' }, files);

  if (r.ok) {
    // 结果由宿主回 {t:'files'} 或 {t:'err'} —— 这里不预设成功。
    return;
  }

  if (r.reason === 'unsupported') {
    // 浏览器预览 / mock：没有旁路通道，File 拿不到路径。
    // 必须**说出来**，不能让用户以为拖成功了。
    onError('当前环境不支持拖拽附件（浏览器预览模式）。'
          + '请用桌面版，或点「附件」按钮。');
    return;
  }

  // ---- not-on-disk：WebView2 判定「这批里至少有一个不是磁盘文件」 ----
  //
  // 典型场景：拖 5 个文件，其中 1 个是从网页上直接拖来的图。整批会被
  // 一次性拒绝（WebView2 的校验是原子的），用户看到 5 个全没进去。
  //
  // 所以整批失败后**逐个重试**：每个单独发一次，能过的过、不能过的收集起来
  // 报一次。代价是 N 次调用，但 N 通常是个位数，而且这是失败路径。
  if (files.length === 1) {
    onError(`「${files[0].name}」不是磁盘上的文件（网页图片 / 截图属于内存数据），`
          + '没法作为附件。请先存成文件再拖，或点「附件」按钮选择。');
    return;
  }

  const bad = [];
  let good = 0;
  for (const f of files) {
    const one = Bridge.sendFiles({ t: 'droppedFiles' }, [f]);
    if (one.ok) good += 1;
    else bad.push(f.name);
  }

  if (good === 0) {
    onError(`${files.length} 个文件都不是磁盘上的文件（网页图片 / 截图属于内存数据），`
          + '没法作为附件。请先存成文件再拖，或点「附件」按钮选择。');
  } else if (bad.length > 0) {
    // 部分成功时也要说清楚 —— 否则用户以为全进去了，发送时才发现少一个。
    onError(`已加入 ${good} 个；以下 ${bad.length} 个不是磁盘文件，已跳过：`
          + bad.join('、'));
  }
}

/**
 * 拖拽的默认行为必须挡掉，否则 WebView2 会把文件当成导航目标：
 * 拖一张图进窗口，整个界面会被替换成那张图（而且回不去，因为没有地址栏）。
 * `dragover` 也要 preventDefault —— 只挡 drop 不够，浏览器在 dragover
 * 阶段就决定「这里能不能放」，不挡的话连 drop 事件都不会触发。
 */
let dragDepth = 0;   // dragenter/dragleave 成对计数：进入子元素也会触发 leave

function isFileDrag(e) {
  const dt = e.dataTransfer;
  if (!dt) return false;
  // types 里含 'Files' 才是拖文件。拖选中的文字进来不该弹附件提示。
  const types = Array.from(dt.types || []);
  if (types.includes('Files')) return true;
  // 老版本 Chromium 拖文件时 types 是空表、但 items 里有 kind='file' 的项。
  // WebView2 用的是 Chromium 内核，版本随运行时走，这条兜底成本极低。
  if (types.length === 0 && dt.items) {
    return Array.from(dt.items).some((it) => it.kind === 'file');
  }
  return false;
}

window.addEventListener('dragenter', (e) => {
  if (!isFileDrag(e)) return;
  e.preventDefault();
  dragDepth += 1;
  document.body.classList.add('dropping');
});

window.addEventListener('dragover', (e) => {
  if (!isFileDrag(e)) return;
  e.preventDefault();
  // dropEffect='copy' 让光标显示「复制」而不是「移动」——
  // 用户拖的是「把文件交给这个应用」，不是「把文件搬走」。
  try { e.dataTransfer.dropEffect = 'copy'; } catch { /* 只读时忽略 */ }
});

window.addEventListener('dragleave', (e) => {
  // 这里**故意不查 isFileDrag**。Chromium 在 dragleave 阶段有时已经把
  // `dataTransfer.types` 清空了，查了就会漏减 —— 高亮永远消不掉。
  // 用 dragDepth > 0 当唯一判据：只要之前在 enter 里加过，这里就该减。
  if (dragDepth === 0) return;
  dragDepth = Math.max(0, dragDepth - 1);
  if (dragDepth === 0) document.body.classList.remove('dropping');
});

window.addEventListener('drop', (e) => {
  if (!isFileDrag(e)) return;
  // 不 preventDefault 的话 WebView2 会拿这个文件去导航 —— 界面直接变成
  // 文件内容，且因为窗口没有地址栏，用户退不回来。
  e.preventDefault();
  dragDepth = 0;
  document.body.classList.remove('dropping');
  submitFiles(e.dataTransfer.files);
});

/**
 * Ctrl+V 粘贴文件。
 *
 * 与拖拽是同一件事的两种触发：从资源管理器 Ctrl+C 一个文件，在窗口里
 * Ctrl+V，`clipboardData.files` 里就是那个 File 对象，走同一条旁路。
 *
 * 但要和**粘贴文本**区分开 —— 用户往输入框里粘一段文字是最高频的操作，
 * 绝不能因为这里装了监听就干扰它。判据是 `clipboardData.files.length`：
 * 只有真带文件时才拦截；否则直接 return，让浏览器按默认行为把文字插进
 * textarea（连 preventDefault 都不能调）。
 *
 * 另外：截图粘贴（Win+Shift+S 后 Ctrl+V）走的是 clipboardData.items 里的
 * image/png Blob，**没有本地文件**。这种不在这里处理 —— 宿主会收到一个
 * 空路径列表并给出可读提示（见 WebBridge 的 droppedFiles 分支）。
 * 与其静默失败，不如让用户知道「截图得先存成文件」。
 */
window.addEventListener('paste', (e) => {
  const dt = e.clipboardData;
  if (!dt || !dt.files || dt.files.length === 0) return;   // 粘文本：不插手
  e.preventDefault();
  submitFiles(dt.files);
});

$('btnRun').addEventListener('click', doSend);
$('btnAttach').addEventListener('click', () => send({ t: 'pickFiles' }));
$('btnCancel').addEventListener('click', () => send({ t: 'cancel' }));
$('btnNew').addEventListener('click', () => send({ t: 'newConv' }));
$('btnHandoff').addEventListener('click', () => send({ t: 'handoff' }));
// 页脚两块分得很清楚：
//   身份（头像 + 名字）→ 开身份面板，能做登录/粘贴/清除
//   小刷新图标          → 只重新验一次，**不弹面板**（对应 WPF 壳的 BtnRefreshIdentity）
// 后者保留是有理由的：健康胶囊显示「未登录」时，用户的下一个动作一定是
// 「怎么修」，把它藏进面板就变成两跳。
$('footIdentity').addEventListener('click', () => openIdentity());
$('btnRefreshId').addEventListener('click', () => send({ t: 'identity' }));

/* ---------------------------- 主题切换 ----------------------------
   首屏由 theme-boot.js 在 <head> 里同步定好（防闪），这里只负责
   「点按钮 → 翻到另一档」。

   为什么不在这里也读一次 localStorage：theme-boot 已经把解析结果挂到
   window.ThemeBoot 上了，再读一次就有两个事实源 —— 两边哪天不一致，
   按钮图标和实际配色就会对不上。 */

/** 当前实际生效的主题（不是用户偏好）。 */
function currentTheme() {
  return document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light';
}

/** 把主题落到 DOM，并把按钮提示改成「点了会变成什么」。 */
function applyTheme(theme) {
  window.ThemeBoot.set(theme);
  $('btnTheme').title = currentTheme() === 'dark' ? '切换到浅色' : '切换到深色';
}

$('btnTheme').addEventListener('click', () => {
  // 点一下 = 明确选边。从「跟随系统」点进去时，翻转的是**当前生效值** ——
  // 系统是深色、界面也是深色，点一下就该变浅色，而不是「先固定成深色」。
  // 这是最容易写错的一处：拿 pref 判就会变成「点一下没反应」。
  applyTheme(currentTheme() === 'dark' ? 'light' : 'dark');
});

// 跟随系统：用户没手动选过时，系统切主题要跟着变。
// 手动选过就固定住 —— 用户的显式选择优先于系统偏好，这是通行做法。
try {
  const mq = window.matchMedia('(prefers-color-scheme: dark)');
  const onSysChange = () => {
    if (window.ThemeBoot.pref() === null) {
      document.documentElement.dataset.theme = mq.matches ? 'dark' : 'light';
    }
  };
  // addEventListener 在旧内核上是 addListener。WebView2 用的是新内核，
  // 但用浏览器打开预览时可能是旧的，两个都试一下不亏。
  if (mq.addEventListener) mq.addEventListener('change', onSysChange);
  else if (mq.addListener) mq.addListener(onSysChange);
} catch (e) { /* matchMedia 不可用时就不跟随，不影响手动切换 */ }

// 初始化按钮提示文字（图标由 CSS 按 data-theme 二选一，不用 JS 管）
$('btnTheme').title = currentTheme() === 'dark' ? '切换到浅色' : '切换到深色';
// 齿轮 = 从账号重新读一次会话列表（要联网）
$('btnSyncConvs').addEventListener('click', refreshConvs);

/* ============================ 朗读音色面板 ============================

   音色是**账号级偏好**（改一次，网页版也变），不是本地设置。所以这里
   不做本地缓存 —— 每次打开面板都从账号重新读，避免「本地显示 A、
   实际在放 B」这种最伤人的不一致。

   面板是异步填的，三个状态都要画：
     · 读取中    —— 明确写「正在读取」，不要留空框
     · 读取失败  —— 把宿主给的 error 原样显示，不吞
     · 成功      —— 列表 + 当前选中打勾

   `Tts.voices()` / `Tts.setVoice()` 在 tts.js 里已经备好（走 req 关联号，
   不会跟朗读请求串线），这里只负责画和点。 */

let voiceItems = null;      // 缓存本次打开时读到的列表（null = 还没读）
let voiceBusy = false;      // 防止连点两次发出两个写请求

function renderVoices() {
  const box = $('voiceList');
  box.innerHTML = '';

  if (voiceItems === null) {
    box.appendChild(el('div', 'vp-hint', '正在读取音色…'));
    return;
  }
  if (voiceItems.error) {
    const e = el('div', 'vp-err', voiceItems.error);
    box.appendChild(e);
    return;
  }
  if (!voiceItems.items || !voiceItems.items.length) {
    box.appendChild(el('div', 'vp-hint', '这个账号没有可用音色'));
    return;
  }

  for (const v of voiceItems.items) {
    // **选中态只能看 `isCurrent`**。`isDefault` 是出厂默认（永远 mira），
    // 拿它当选中态会让面板永远显示 mira —— 切换明明成功了也看不出来。
    const row = el('button', 'vp-item' + (v.isCurrent ? ' on' : ''));
    row.type = 'button';
    row.dataset.id = v.id || '';

    const main = el('div', 'vp-main');
    main.appendChild(el('span', 'vp-name', v.name || v.id || '（未命名）'));

    // 性别与语言是**并列的标签**，不是一句描述 —— 用 meta 行排开，
    // 窄屏下能自然换行，不会把音色名挤掉。
    const meta = el('div', 'vp-meta');
    if (v.gender) {
      meta.appendChild(el('span', 'vp-tag', v.gender === 'male' ? '男声'
        : v.gender === 'female' ? '女声' : v.gender));
    }
    for (const lang of (v.languages || [])) {
      meta.appendChild(el('span', 'vp-tag', lang));
    }
    // Fish 的音色没有 gender / languages，只有一串自由标签
    // （male / old / character-voice…）。最多显示 3 个 —— 全列出来
    // 会把音色名挤成一行省略号，反而看不出是哪个。
    for (const tag of (v.tags || []).slice(0, 3)) {
      meta.appendChild(el('span', 'vp-tag', tag));
    }
    // 出厂默认是**另一件事**，用弱化的文字标注，不占用勾选位。
    if (v.isDefault) meta.appendChild(el('span', 'vp-desc', '默认'));
    if (v.desc) meta.appendChild(el('span', 'vp-desc', v.desc));
    main.appendChild(meta);

    row.appendChild(main);
    if (v.isCurrent) {
      const tick = el('span', 'vp-tick');
      tick.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none">'
        + '<path d="M5 12.5l4.5 4.5L19 7" stroke="currentColor" stroke-width="2.2" '
        + 'stroke-linecap="round" stroke-linejoin="round"/></svg>';
      row.appendChild(tick);
    }

    row.addEventListener('click', () => pickVoice(v));
    box.appendChild(row);
  }
}

async function pickVoice(v) {
  if (voiceBusy || !v || !v.id) return;
  if (v.isCurrent) return;          // 已经选中的再点一次没有意义，也不该写一次账号偏好
  if (!window.Tts) return;

  voiceBusy = true;
  $('voiceList').classList.add('busy');
  try {
    const res = await window.Tts.setVoice(v.id);
    if (res && res.ok) {
      // **写完必须回读**，不能拿本地猜的状态直接改 UI。
      //
      // 2026-10-19 实测踩过：`POST /chat/tts/voice` 返回 `code:0, biz_code:0`
      // 但当时前端用 `isDefault` 判断选中态，而那是出厂默认（永远 mira）——
      // 于是「写成功 → UI 没变 → 用户以为坏了」。更隐蔽的是反过来：
      // 如果服务端静默忽略请求，本地改状态同样会让 UI 显示成功。
      // 只有回读，UI 才等于**持久层**的事实。
      //
      // Fish 后端同理，只是「持久层」从服务端换成了本地 tts.json ——
      // 回读仍然是必要的：写盘可能失败（权限、磁盘满），而那条路径
      // 的错误目前是静默吞掉的（见 TtsConfigStore.Save）。
      const fresh = await window.Tts.voices();
      if (fresh && !fresh.error && fresh.items) {
        voiceItems = { items: fresh.items, error: '' };
      } else {
        // 回读失败也不能让界面停在「什么都没发生」——
        // 用服务端回报的 id 做一次本地兜底，并保留错误提示。
        const chosen = res.id || v.id;
        for (const it of (voiceItems.items || [])) it.isCurrent = (it.id === chosen);
      }
      renderVoices();
    } else {
      const msg = (res && res.error) ? res.error : '切换音色失败';
      if (window.AppToast) window.AppToast(msg);
    }
  } finally {
    voiceBusy = false;
    $('voiceList').classList.remove('busy');
  }
}

async function openVoicePanel() {
  const pop = $('voicePop');
  pop.hidden = false;
  $('btnVoice').setAttribute('aria-expanded', 'true');

  // 每次打开都重读：音色可能在网页版被改过，本地缓存会撒谎。
  voiceItems = null;
  renderVoices();
  if (!window.Tts) {
    voiceItems = { error: '朗读模块未加载' };
    renderVoices();
    return;
  }
  try {
    const res = await window.Tts.voices();
    // 面板可能已经被关掉了 —— 迟到的结果不该把列表刷出来
    if (pop.hidden) return;
    voiceItems = res || { error: '没有收到音色数据' };
  } catch (e) {
    voiceItems = { error: String(e && e.message ? e.message : e) };
  }
  renderVoices();
}

function closeVoicePanel() {
  $('voicePop').hidden = true;
  $('btnVoice').setAttribute('aria-expanded', 'false');
}

$('btnVoice').addEventListener('click', (e) => {
  e.stopPropagation();   // 否则立刻被下面的「点别处关闭」接住，开了又关
  if ($('voicePop').hidden) openVoicePanel(); else closeVoicePanel();
});
$('voiceClose').addEventListener('click', closeVoicePanel);

// 点面板外面关闭。用捕获阶段之外的方式挂 —— 面板内的点击
// （包括音色行）必须能穿过，所以判断是否落在面板内。
document.addEventListener('click', (e) => {
  const pop = $('voicePop');
  if (pop.hidden) return;
  if (pop.contains(e.target) || $('btnVoice').contains(e.target)) return;
  closeVoicePanel();
});

// Esc 关闭，与其它弹层保持一致
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !$('voicePop').hidden) closeVoicePanel();
});

$('chipThinking').addEventListener('click', () => {
  send({ t: 'setChat', thinking: !$('chipThinking').classList.contains('on') });
});
$('chipSearch').addEventListener('click', () => {
  send({ t: 'setChat', search: !$('chipSearch').classList.contains('on') });
});
$('selModel').addEventListener('change', (e) => {
  send({ t: 'setModel', name: e.target.value });
});

$('btnCopyAll').addEventListener('click', () => {
  const text = items
    .filter((x) => x.kind === 'Goal' || x.kind === 'Assistant')
    .map((x) => (x.kind === 'Goal' ? '> ' + x.text : x.text))
    .join('\n\n');
  copyText(text, $('btnCopyAll'), '复制全文');
});

/* 任务列表：点头部展开/收起 */
$('todoHead').addEventListener('click', () => $('todo').classList.toggle('collapsed'));

/* 熔断重置：只在熔断态可见（见 onStatus）。点完不做本地状态猜测 ——
   成功与否由宿主推回来的 status 决定，界面不自己把胶囊改绿。 */
$('btnResetBreaker').addEventListener('click', () => send({ t: 'reset' }));

/* 重放当前对话：纯本地读盘 + 重绘，不发网络请求（AgentHost.ReplayCurrentAsync）。
   用途是「界面看着不对」时的兜底 —— 切完对话、或者渲染出了怪东西，点一下重画。 */
$('btnReplay').addEventListener('click', () => send({ t: 'replay' }));

/* 菜单：点别处关闭。用 mousedown 而不是 click —— click 会在「点到菜单项」
   之后才冒泡上来，那时菜单已经被项自己关掉了，无害；但用 mousedown 更早，
   能防止「按下按钮 → 菜单被关 → 松开时落到下面的行上」这种误触。 */
document.addEventListener('mousedown', (e) => {
  const menu = $('convMenu');
  if (menu.hidden) return;
  if (menu.contains(e.target)) return;
  if (e.target.closest && e.target.closest('.chat-more')) return;
  closeConvMenu();
});

/* 菜单键盘：方向键上下走，Home/End 到两端，Esc 关闭并回到触发按钮。 */
$('convMenu').addEventListener('keydown', (e) => {
  const items = Array.from($('convMenu').querySelectorAll('.cmenu-item:not([disabled])'));
  if (!items.length) return;
  const i = items.indexOf(document.activeElement);
  if (e.key === 'ArrowDown') {
    e.preventDefault();
    items[(i + 1) % items.length].focus();
  } else if (e.key === 'ArrowUp') {
    e.preventDefault();
    items[(i - 1 + items.length) % items.length].focus();
  } else if (e.key === 'Home') {
    e.preventDefault();
    items[0].focus();
  } else if (e.key === 'End') {
    e.preventDefault();
    items[items.length - 1].focus();
  } else if (e.key === 'Escape') {
    e.preventDefault();
    e.stopPropagation();      // 别让全局 Esc 顺手把身份面板也关了
    closeConvMenu();
  }
});

/* 通用对话框 */
$('dlgClose').addEventListener('click', closeDialog);
$('dlgCancel').addEventListener('click', closeDialog);
$('dlgOk').addEventListener('click', () => { if (dlgOnOk) dlgOnOk(); });
$('dlgMask').addEventListener('mousedown', (e) => {
  if (e.target === $('dlgMask')) closeDialog();
});
$('dlgInput').addEventListener('keydown', (e) => {
  // 回车提交是输入框的肌肉记忆。Esc 由全局处理器关。
  if (e.key === 'Enter') { e.preventDefault(); if (dlgOnOk) dlgOnOk(); }
});

/* 左栏导航：对话流 / 监视器 */
document.querySelectorAll('.nav-item').forEach((b) => {
  b.addEventListener('click', () => {
    document.querySelectorAll('.nav-item').forEach((x) => x.classList.remove('on'));
    b.classList.add('on');
    const mon = b.dataset.view === 'monitor';
    $('stream').hidden = mon;
    $('monitor').hidden = !mon;
    $('mhTitle').textContent = mon ? '监视器' : '对话流';
    $('btnCopyAll').hidden = mon;      // 监视器里没有「全文」可复制
    $('btnHandoff').hidden = mon;
    if (mon) monInner.scrollTop = monInner.scrollHeight;
    else scrollIfStick();
  });
});

/* ---------------------------- 右栏 ---------------------------- */

document.querySelectorAll('.rb-tab').forEach((b) => {
  b.addEventListener('click', () => {
    document.querySelectorAll('.rb-tab').forEach((x) => x.classList.remove('on'));
    b.classList.add('on');
    rbTab = b.dataset.tab;
    showPanel(rbTab);
  });
});

function showPanel(tab) {
  document.querySelectorAll('.rb-panel').forEach((p) => {
    p.hidden = p.dataset.panel !== tab;
  });
  // 点页签要立刻见效，不走合并（合并是给批量灌数据用的）
  if (tab === 'tools') renderTools();
  else if (tab === 'proj') renderProj();
  else syncConf();
}

/* 右栏的刷新统一走 scheduleFlush()（见「刷新合并」一节）——
   之前在这里留了个 refreshRight()，被 onItem 逐条调用，600 条就是 1.6 秒。 */

/** 右栏条目：图标 + 标题/副标题 + 状态徽章。点一下跳到对话流里那张卡。 */
function toolRow(s, onClick) {
  const t = s.tool || {};
  const row = el('div', 'titem');
  const ic = el('span', 'ti ' + (t.running ? 'blue' : t.failed ? 'ink' : 'teal'));
  ic.innerHTML = t.running ? SVG_TI_SPIN : (t.failed ? SVG_TI_FAIL : SVG_TI_OK);

  const tt = el('span', 'tt');
  tt.appendChild(el('span', 'a', t.label || t.kind || '工具'));
  tt.appendChild(el('span', 'b', t.subject || ''));

  const bd = el('span', 'badge ' + (t.running ? 'run' : t.failed ? 'bad' : 'ok'),
    t.running ? '运行中' : t.failed ? '失败' : '完成');

  row.append(ic, tt, bd);
  if (onClick) row.addEventListener('click', onClick);
  return row;
}

function focusItem(seq) {
  const node = nodeOf.get(seq);
  if (!node) return;
  node.scrollIntoView({ block: 'center', behavior: 'smooth' });
  node.classList.add('flash');
  setTimeout(() => node.classList.remove('flash'), 900);
}

function renderTools() {
  const box = $('toolList');
  if (!box) return;
  box.textContent = '';
  const all = items.filter((x) => x.kind === 'ToolCall' && x.tool);
  if (!all.length) {
    box.appendChild(el('div', 'empty-state', '还没有工具调用'));
    return;
  }
  all.slice(-40).reverse().forEach((s) => {
    box.appendChild(toolRow(s, () => focusItem(s.seq)));
  });
}

function renderProj() {
  const pb = $('projBody');
  const rb = $('runBody');
  if (!pb || !rb) return;
  pb.textContent = '';
  rb.textContent = '';

  const s = status || {};

  // 项目卡：会话标识
  const card = el('div', 'pcard');
  const row = el('div', 'row');
  const ic = el('span', 'ti blue');
  ic.innerHTML = SVG_FOLDER;
  row.append(ic, el('span', 'nm', s.convTitle || '当前会话'));
  card.appendChild(row);
  card.appendChild(el('span', 'pt', 'cid ' + (s.sessionCid || '—')));
  pb.appendChild(card);

  // 轮数计量
  const turns = s.sessionTurns || 0;
  const max = s.sessionMaxTurns || 0;
  const st = el('div', 'stat');
  const sr = el('div', 'row');
  sr.appendChild(el('span', 'sl', '会话轮数'));
  st.appendChild(sr);
  st.appendChild(el('div', 'sd', turns + (max ? ' / ' + max : '') + ' 轮'));
  if (max) {
    const pct = Math.min(100, turns / max * 100);
    const meter = el('div', 'meter');
    const i = document.createElement('i');
    i.style.width = pct.toFixed(1) + '%';
    if (pct >= 90) i.style.background = 'var(--bad)';
    else if (pct >= 70) i.style.background = 'var(--warn)';
    meter.appendChild(i);
    st.appendChild(meter);
  }
  pb.appendChild(st);

  // 链路 / 记忆
  const kv = el('div');
  kv.appendChild(kvRow('传输', s.transport || '—'));
  kv.appendChild(kvRow('链路', s.transportReady ? '已就绪' : '未就绪'));
  if (s.accountSuspended) {
    // 原文照抄（含截止时间）—— 处置的判据在服务端，界面不自己改写。
    kv.appendChild(kvRow('账号', s.suspendedDetail || '账号被服务端处置'));
  }
  kv.appendChild(kvRow('记忆', s.memoryEnabled ? ((s.memoryChars || 0) + ' 字') : '已关闭'));
  pb.appendChild(kv);

  // 本轮步骤
  const steps = currentSteps();
  if (!steps.length) {
    rb.appendChild(el('div', 'empty-state', '本轮还没有工具调用'));
    return;
  }
  steps.forEach((x) => rb.appendChild(toolRow(x, () => focusItem(x.seq))));
}

function kvRow(k, v) {
  const d = el('div', 'kv');
  d.appendChild(el('span', null, k));
  d.appendChild(el('b', null, String(v)));
  return d;
}

/* ---------------------------- 配置页（静态骨架 + 同步） ---------------------------- */

function setSw(id, on) {
  const b = $(id);
  if (b) b.classList.toggle('on', !!on);
}

/** 最近一次收到的档位清单。切档位的回执要用它把 `name` 翻成中文标签。 */
let transportCatalog = [];

function syncConf() {
  const s = status || {};
  setSw('swThinking', !!s.thinking);
  setSw('swSearch', !!s.search);
  setSw('swShowThinking', s.showThinking == null ? true : !!s.showThinking);
}

// 档位下拉。只在值真的变了才发 —— `change` 在「重新选中同一项」时
// 不会触发，所以不需要额外的去重判断。
//
// **不在这里做确认弹窗**：切档位会新建一条对话，但旧对话仍在列表里、
// 随时能切回去，代价可逆。加弹窗只会让常用动作多一次点击。
(() => {
  const sel = $('selTransport');
  if (!sel) return;
  sel.addEventListener('change', () => {
    const name = sel.value;
    if (!name) return;
    transportNote(describeTransport(name, transportCatalog));
    // **不可用的档位也照样发出去。** 让引擎给出具体错误（缺哪个包、
    // 哪个文件不存在），比前端自己拼一句「不可用」有用得多 ——
    // 而且那正是用户修复时需要的原文。宿主会回 `transportSet{ok:false}`，
    // 引擎的报错同时进对话流。
    send({ t: 'setTransport', name });
  });
})();

[
  ['swThinking', 'thinking'],
  ['swSearch', 'search'],
  ['swShowThinking', 'showThinking'],
].forEach(([id, key]) => {
  const b = $(id);
  if (!b) return;
  b.addEventListener('click', () => {
    const on = !b.classList.contains('on');
    b.classList.toggle('on', on);
    const m = { t: 'setChat' };
    m[key] = on;
    send(m);
  });
});

/* ---------------------------- 头像 ---------------------------- */

const AVA_KEY = 'gad.avatarKey';
const AVA_IMG = 'gad.avatarImg';

let avatarKey = 'whale';
let avatarImg = '';
try {
  avatarKey = localStorage.getItem(AVA_KEY) || 'whale';
  avatarImg = localStorage.getItem(AVA_IMG) || '';
} catch (e) { /* 隐私模式禁 localStorage，退回默认头像 */ }

/** 造一个头像节点（撑满父容器）。三种来源：默认鲸鱼 / 首字母 / 自定义图片。 */
function avatarNode() {
  if (avatarKey === 'custom' && avatarImg) {
    const img = document.createElement('img');
    img.src = avatarImg;
    img.alt = '';
    img.style.cssText = 'width:100%;height:100%;object-fit:cover;display:block';
    return img;
  }
  if (avatarKey === 'initial') return document.createTextNode('D');
  const span = document.createElement('span');
  span.innerHTML = SVG_WHALE;              // 本文件常量，非外部数据
  return span.firstChild;
}

function fillAvatar(host) {
  if (!host) return;
  host.textContent = '';
  host.appendChild(avatarNode());
}

function applyAvatar() {
  document.querySelectorAll('.msg.bot .av').forEach(fillAvatar);
  fillAvatar($('avaPrev'));
  document.querySelectorAll('#avaPicks .pick[data-avatar]').forEach((b) => {
    b.classList.toggle('on', b.dataset.avatar === avatarKey);
  });
}

document.querySelectorAll('#avaPicks .pick[data-avatar]').forEach((b) => {
  b.addEventListener('click', () => {
    avatarKey = b.dataset.avatar;
    try { localStorage.setItem(AVA_KEY, avatarKey); } catch (e) { /* 忽略 */ }
    applyAvatar();
  });
});

$('avaUpload').addEventListener('click', () => $('avaFile').click());

$('avaFile').addEventListener('change', (e) => {
  const f = e.target.files && e.target.files[0];
  if (!f) return;
  const r = new FileReader();
  r.onload = () => {
    avatarImg = String(r.result || '');
    avatarKey = 'custom';
    try {
      localStorage.setItem(AVA_IMG, avatarImg);
      localStorage.setItem(AVA_KEY, avatarKey);
    } catch (err) { /* 图太大超配额：只在本次会话生效 */ }
    applyAvatar();
  };
  r.readAsDataURL(f);
  e.target.value = '';
});

/* ---------------------------- 身份 / 凭据 ----------------------------
   对应 WPF 壳的 IdentityDialog（IdentityDialog.xaml.cs）：命令与宿主
   WebBridge 里的 id* 分支一一对应，业务判断全在 AgentHost / 引擎里
   （校验、落盘、重建运行时），这里只负责收发与显示。

   为什么每条命令都要带 req 关联号：面板里可能同时有两件事在跑
   （保存标识 + 登录轮询），而结果都从同一个 {t:'idres'} 通道回来。
   没有关联号，就会把「登录成功」显示在「保存标识」那一行上。

   状态推送（{t:'idstate'}）是**不带**关联号的 —— 它是一个全局快照，
   任何时候到都能直接刷新面板，不需要归属。 */

const idPending = new Map();     // req -> 回调
let idReq = 0;
let idBusy = false;              // 一条短请求在跑（保存标识 / 验证 / 清除）
let idOpen = false;
let idLast = null;               // 最近一次身份快照（onStatus 更新时要重渲染）
let idClearArmed = 0;            // 「清除」的两段式确认计时器
let idLogoutArmed = 0;           // 「退出登录」的两段式确认计时器
const idLogin = { active: false, startedAt: 0, poll: 0, tick: 0 };

/** 发一条命令并登记回调。cb 为 null 表示「发出去就行，不等结果」。 */
function idCall(msg, cb) {
  const req = ++idReq;
  if (cb) idPending.set(req, cb);
  send(Object.assign({ req }, msg));
}

/** 结果分发。找不到归属就丢掉 —— 多半是面板已经关了。 */
function idOnResult(m) {
  const cb = idPending.get(m.req);
  if (!cb) return;
  idPending.delete(m.req);
  cb(m);
}

function idMessage(text, bad) {
  const box = $('idMsg');
  box.textContent = text || '';
  box.className = 'idmsg' + (text ? (bad ? ' bad' : ' ok') : '');
  box.hidden = !text;
}

/**
 * 凭据文件的路径只留最后两段。
 *
 * 引擎回的 `token_file` 是**完整绝对路径**（例如
 * `C:\Users\Admin\Documents\Projects\deepseek-agent-desktop\engine\.token`）。
 * 原样显示会把 `.idnow` 那行撑成两三行 —— 那行是「一眼看清当前身份」的地方，
 * 被路径挤成三行就本末倒置了。
 * 最后两段（`…\engine\.token`）已经足够指明是哪个文件。
 *
 * 注：上一轮我是拿「路径太长把维护按钮顶到折叠线以下」当理由加的这个函数。
 * 那个理由**已经过时** —— 维护动作现在在 `.idfoot` 里，不随内容滚动，
 * 再长的路径也顶不掉它。但截短本身仍然值得留着：它解决的是可读性，
 * 不是布局。留着，但别再拿布局当借口。
 */
function shortPath(p) {
  const parts = String(p || '').split(/[\\/]+/).filter(Boolean);
  if (parts.length <= 2) return String(p || '');
  return '…\\' + parts.slice(-2).join('\\');
}

/**
 * 按钮可用性。两件事各自独立：
 *   idBusy        一条短请求在跑
 *   idLogin.active 登录等待在跑
 * 两者都会占用引擎/浏览器，所以都能挡住别的操作。
 *
 * ★ 但「取消登录」例外 —— 它只在**有登录可取消**时才可用，绝不跟着
 *   idBusy 一起灰掉。WPF 壳把 BtnLoginCancel 也绑进了 SetBusy 的
 *   `IsEnabled = !busy`，结果是「等待登录」阶段用户唯一能做的就是关掉
 *   整个对话框（那时 busy 恒为 true）。这里不跟。
 */
function idSyncEnabled() {
  const block = idBusy || idLogin.active;
  ['idSaveLabel', 'idRefresh', 'idClear', 'idLogout', 'idManualToggle', 'idSave', 'idBrowserLogin']
    .forEach((id) => { const b = $(id); if (b) b.disabled = block; });
  $('idLoginCancel').disabled = !idLogin.active;
}

function idBusySet(on, text) {
  idBusy = on;
  $('idBusy').textContent = on ? (text || '') : '';
  idSyncEnabled();
}

/** 身份快照 → 面板。s 为 null 表示宿主读不出来（error 里是原因）。 */
function idApplyState(s, error) {
  if (!s) {
    $('idMode').textContent = '未知';
    $('idChip').textContent = '读取失败';
    $('idChip').className = 'idchip bad';
    $('idNowHint').textContent = error || '读取身份状态失败';
    return;
  }
  idLast = s;

  const on = !!s.hasToken;
  // 账号处置的判据在**全局 status**（由传输层响应置位），不在身份快照里 ——
  // identity.status 只读 token 文件，永远不知道账号被封。这里把两者拼起来，
  // 否则面板会显示「已登录」，而实际上一个字都发不出去。
  const suspended = !!(s.account_suspended || (status && status.accountSuspended));
  const suspendedDetail = s.suspended_detail || (status && status.suspendedDetail) || '';

  if (suspended) {
    $('idMode').textContent = '账号被处置';
    $('idChip').textContent = '账号被处置';
    $('idChip').className = 'idchip bad';
  } else {
    $('idMode').textContent = on ? '已登录' : '未登录';
    $('idChip').textContent = on ? '已登录' : '未登录';
    $('idChip').className = 'idchip ' + (on ? 'ok' : 'warn');
  }

  const parts = [];
  if (suspended) parts.push('账号被处置：' + (suspendedDetail || '服务端已封禁 / 停用'));
  if (s.label) parts.push('账号：' + s.label);
  if (s.impersonate) parts.push('指纹档位：' + s.impersonate);
  if (s.tokenFile) parts.push('凭据文件：' + shortPath(s.tokenFile));
  $('idNowHint').textContent = parts.length ? parts.join(' · ') : '尚未取得凭据';

  // 回填**只在用户没动过输入框时**做。每次保存/刷新之后宿主都会推一次
  // 快照，无条件回填会把用户正在敲的字抹掉。

  // 宿主说「已有一个登录会话在进行中」，而面板这边不知道 —— 把等待态补回来。
  // 不补的话用户会看到一个「打开浏览器登录」按钮，点下去引擎只会回
  // already_running，界面上什么都不会变（看起来像按钮坏了）。
  if (s.loginActive && !idLogin.active) {
    idEnterLogin('宿主那边已有一个登录会话在进行中 —— 请在弹出的浏览器窗口里完成登录。');
  }
}

function openIdentity() {
  if (idOpen) return;
  idOpen = true;
  idMessage('');
  $('idMask').hidden = false;
  // 每次打开都重读一次快照：页脚那枚小刷新可能刚改过状态
  send({ t: 'idStatus' });
  const box = $('idLabel');
  if (box) setTimeout(() => box.focus(), 0);
}

function closeIdentity() {
  if (!idOpen) return;
  // 面板关了但登录浏览器还开着的话，会留下一个没人管的 Chrome。
  // WPF 壳在 IdentityDialog.OnClosed 里做同一件事（SafeCancelLoginAsync）。
  if (idLogin.active) idCall({ t: 'idLoginCancel' }, null);
  idStopLogin();
  idPending.clear();          // 面板关了，在途结果的回调不再有意义
  idOpen = false;
  $('idMask').hidden = true;
  idMessage('');
}

// ---------- 浏览器登录 ----------

/**
 * 进入「等待登录」态。
 *
 * 单独抽出来是因为有两个入口：① 用户点了「打开浏览器登录」；
 * ② 面板打开时宿主说「已经有一个会话在跑」（见 idApplyState）。
 * 计时器用 if(!...) 守着 —— 两个入口都走到也不会叠出两套定时器。
 */
function idEnterLogin(detail) {
  idLogin.active = true;
  idLogin.startedAt = Date.now();
  $('idBrowserIdle').hidden = true;
  $('idLoginWait').hidden = false;
  $('idLoginTitle').textContent = '等待登录…';
  $('idLoginDetail').textContent = detail || '';
  $('idLoginElapsed').textContent = '';
  // 2 秒轮询登录态 / 1 秒走计时 —— 与 WPF 壳的 _pollTimer / _elapsedTimer 同频
  if (!idLogin.poll) idLogin.poll = setInterval(idPollLogin, 2000);
  if (!idLogin.tick) idLogin.tick = setInterval(idTickLogin, 1000);
  idSyncEnabled();
}

function idTickLogin() {
  if (!idLogin.active) return;
  const sec = Math.floor((Date.now() - idLogin.startedAt) / 1000);
  $('idLoginElapsed').textContent = '已等待 ' + sec + ' 秒';
}

function idPollLogin() {
  if (!idLogin.active) return;
  idCall({ t: 'idLoginPoll' }, (m) => {
    if (!idLogin.active) return;    // 期间被取消了
    switch (m.state) {
      case 'ready':
        // 拿到登录态 → 立刻收口（引擎会验证 + 落盘 + 关浏览器）
        if (idLogin.poll) { clearInterval(idLogin.poll); idLogin.poll = 0; }
        if (idLogin.tick) { clearInterval(idLogin.tick); idLogin.tick = 0; }
        $('idLoginTitle').textContent = '正在取走凭据并验证…';
        $('idLoginDetail').textContent = '浏览器会被自动关掉，之后对话不再需要它。';
        idFinishLogin();
        break;

      case 'settling':
        $('idLoginDetail').textContent = '已读到登录态，正在确认它稳定…';
        break;

      case 'waiting':
        // 常态：还没登录。不刷文案，只让计时器走。
        break;

      case 'no_browser':
        idStopLogin();
        idMessage('浏览器被关掉了 —— 登录已取消。', true);
        break;

      case 'no_session':
        // 引擎那边已经没有登录会话了（浏览器进程被外部结束、或引擎重启过）。
        // 与 no_browser 同类，必须收口 —— 不收的话「等待登录…」会一直转下去，
        // 而用户完全看不出为什么。
        idStopLogin();
        idMessage('宿主那边的登录会话已经不存在了 —— 请重新点「打开浏览器登录」。', true);
        break;

      case 'error':
      default:
        idStopLogin();
        idMessage('读取凭据出错：' + (m.error || m.state || '未知原因'), true);
        break;
    }
  });
}

function idFinishLogin() {
  idCall({ t: 'idLoginFinish' }, (m) => {
    idStopLogin();
    if (m.ok) {
      idMessage(
        '凭据已保存并验证通过' + (m.label ? '（' + m.label + '）' : '') +
        (m.detail ? ' · ' + m.detail : ''),
        false);
    } else {
      // 引擎已把凭据落盘并验过一次；验不过通常意味着它已被服务端作废。
      idMessage(
        '取到了凭据但验证不通过：' + (m.detail || '') +
        '\n这通常意味着该凭据已被服务端作废 —— 请重新登录一次。',
        true);
    }
  });
}

function idStopLogin() {
  idLogin.active = false;
  if (idLogin.poll) { clearInterval(idLogin.poll); idLogin.poll = 0; }
  if (idLogin.tick) { clearInterval(idLogin.tick); idLogin.tick = 0; }
  $('idLoginWait').hidden = true;
  $('idBrowserIdle').hidden = false;
  idBusySet(false, '');
}

// ---------- 面板事件 ----------

$('idClose').addEventListener('click', closeIdentity);
$('idClose2').addEventListener('click', closeIdentity);

// 点遮罩空白处也能关 —— 但**只在没有事情在跑的时候**。
// 登录等到一半被一次误点取消掉，代价比多点一次关闭按钮大得多。
$('idMask').addEventListener('mousedown', (e) => {
  if (e.target !== $('idMask')) return;
  if (idBusy || idLogin.active) return;
  closeIdentity();
});

document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  // 通用对话框在最上层，Esc 先关它 —— 否则删确认框还开着，身份面板先关了。
  if (!$('dlgMask').hidden) { closeDialog(); return; }
  // 有事情在跑时 Esc 不关 —— 和遮罩点击同一条理由：一次误按不该把进行到
  // 一半的登录取消掉。这时要关只能点「关闭」（那是明确的意图，会连带取消登录）。
  if (idOpen && !idBusy && !idLogin.active) closeIdentity();
});



$('idRefresh').addEventListener('click', () => {
  idBusySet(true, '正在重新验证…');
  idMessage('');
  idCall({ t: 'idRefresh' }, (m) => {
    idBusySet(false, '');
    // text 是宿主那条结论的原话（「凭据已刷新（来源 X）· detail」或
    // 「凭据不可用：detail」）—— 原样显示，不改写。
    // 宿主那边这个方法返回 void，结论只进对话流；面板是遮罩看不见对话流，
    // 所以由桥把它捞出来随结果一起回来。
    idMessage(m.text || (m.ok ? '凭据已刷新' : '凭据不可用'), !m.ok);
  });
});

$('idClear').addEventListener('click', () => {
  const b = $('idClear');
  if (!idClearArmed) {
    // 破坏性动作，两段式确认：第一次点只把按钮变成「确认清除？」，
    // 3 秒内再点一次才真的清。不引原生 confirm() —— 它会阻塞渲染进程，
    // 而且在 WebView2 里的样式跟界面完全不是一回事。
    b.textContent = '确认清除？';
    b.classList.add('danger');
    idClearArmed = setTimeout(() => {
      idClearArmed = 0;
      b.textContent = '清除本地凭据';
      b.classList.remove('danger');
    }, 3000);
    return;
  }
  clearTimeout(idClearArmed);
  idClearArmed = 0;
  b.textContent = '清除本地凭据';
  b.classList.remove('danger');

  idBusySet(true, '正在清除…');
  idMessage('');
  idCall({ t: 'idClear' }, (m) => {
    idBusySet(false, '');
    idMessage(m.ok ? '已清除本地凭据。' : (m.text || '清除失败'), !m.ok);
  });
});

$('idLogout').addEventListener('click', () => {
  const b = $('idLogout');
  if (!idLogoutArmed) {
    // 破坏性动作，两段式确认（同 idClear 的理由：不引原生 confirm）。
    // 这个动作会删掉 35 MB 的专用浏览器 profile，比「清除本地凭据」更重。
    b.textContent = '确认退出？';
    b.classList.add('danger');
    idLogoutArmed = setTimeout(() => {
      idLogoutArmed = 0;
      b.textContent = '退出登录';
      b.classList.remove('danger');
    }, 3000);
    return;
  }
  clearTimeout(idLogoutArmed);
  idLogoutArmed = 0;
  b.textContent = '退出登录';
  b.classList.remove('danger');

  idBusySet(true, '正在退出…');
  idMessage('');
  idCall({ t: 'idLogout' }, (m) => {
    idBusySet(false, '');
    if (!m.ok) {
      idMessage(m.text || '退出失败', true);
      return;
    }
    idMessage(
      (m.removedProfile || m.clearedLoginState)
        ? '已退出登录：本地凭据与浏览器登录态都已清除，现在可以换账号登录。'
        : '已清除本地凭据（浏览器登录态本来就不存在），现在可以换账号登录。'
    );
  });
});

$('idBrowserLogin').addEventListener('click', () => {
  idBusySet(true, '正在打开浏览器…');
  idMessage('');
  idCall({ t: 'idLoginStart' }, (m) => {
    if (!m.ok) {
      idBusySet(false, '');
      idMessage(m.text || '打开浏览器失败', true);
      return;
    }
    const port = m.port || '—';
    // reused 时引擎只回 {already_running:true, port:N}，**没有 browser 字段**
    // （见 gad_engine/__main__.py 的 identity.login.start）。照常拼句会得到
    // 「已在 （调试端口 9333）」，所以这里分两句说。
    idEnterLogin(m.reused
      ? '复用了已经打开的那个浏览器（调试端口 ' + port + '）。请在那个窗口里登录；' +
        '检测到登录态后这里会自动继续。'
      : '已在 ' + (m.browser || '浏览器') + '（调试端口 ' + port + '）打开 chat.deepseek.com。' +
        '请在那个窗口里登录；检测到登录态后这里会自动继续。');
    idBusySet(false, '');
  });
});

$('idLoginCancel').addEventListener('click', () => {
  if (idLogin.active) idCall({ t: 'idLoginCancel' }, null);
  idStopLogin();
  idMessage('已取消这次登录。', false);
});

$('idManualToggle').addEventListener('click', () => {
  const box = $('idManual');
  box.hidden = !box.hidden;
  if (!box.hidden) $('idToken').focus();
});

$('idSave').addEventListener('click', () => {
  const token = $('idToken').value.trim();
  if (!token) { idMessage('请先粘贴 token。', true); return; }
  idBusySet(true, '正在保存并验证…');
  idMessage('');
  idCall({ t: 'idSet', token }, (m) => {
    idBusySet(false, '');
    if (!m.ok) { idMessage(m.text || '保存失败', true); return; }
    if (m.valid) {
      $('idToken').value = '';
      idMessage('已保存并验证通过' + (m.detail ? ' · ' + m.detail : ''), false);
    } else if (m.suspended) {
      // 封禁 ≠ 凭据过期：重登不会解除，而且每次重登都是一次真实请求。
      idMessage(
        '账号被服务端处置：' + (m.detail || '') +
        '\n封禁期内重新登录不会解除限制 —— 请等处置到期，不要反复重试。',
        true);
    } else {
      // **不假装成功** —— 存一个用不了的 token 比不存更糟：用户会以为
      // 登录好了，直到第一次对话才炸，而且报错指向错误的方向。
      idMessage(
        '验证不通过：' + (m.detail || '') + '\n该凭据可能已被服务端作废，请重新登录一次。',
        true);
    }
  });
});

// 面板按钮的初始可用性也要摆正。
// idSyncEnabled() 原来只在 idBusySet / idEnterLogin 里被调 —— 也就是说
// 页面刚加载、还没开过面板时，它一次都没跑过。而 index.html 的
// #idLoginCancel 没写 disabled 属性，于是初始态是「取消登录」可用，
// 而 idLogin.active 是 false：状态与界面说的不是一回事。
// 目前看不出后果（它所在的 #idLoginWait 初始是 hidden 的），但这类
// 「状态说 X、界面说 Y」正是以后出怪 bug 的温床，一行摆正。
idSyncEnabled();

/* ---------------------------- 自绘标题栏 ----------------------------

   WebView2 里没有 -webkit-app-region（那是 Electron 的），拖拽必须自己在
   JS 里起头，再让 C# 的 DragMove() 接管。按钮/胶囊/下拉不算拖拽起点。 */

const winOp = (op) => send({ t: 'win', op });
const isChrome = (e) => !!e.target.closest('button,select,a,input,.pill');

$('winMin').addEventListener('click', () => winOp('min'));
$('winMax').addEventListener('click', () => winOp('max'));
$('winClose').addEventListener('click', () => winOp('close'));

(function wireTitlebarDrag() {
  const tb = $('titlebar');
  if (!tb) return;
  tb.addEventListener('mousedown', (e) => {
    if (e.button !== 0 || isChrome(e)) return;
    // 双击的第二次按下别起拖 —— 否则会先 DragMove 一下再触发 dblclick，
    // 观感上就是「最大化前先抖一下」。
    if (e.detail >= 2) return;
    e.preventDefault();               // 否则会开始选文字
    winOp('drag');
  });
  tb.addEventListener('dblclick', (e) => {
    if (isChrome(e)) return;
    winOp('max');                     // 双击标题栏 = 最大化/还原
  });
})();

/* ---------------------------- 演示数据提示 ---------------------------- */

(function initHint() {
  const h = $('hintRight');
  if (!h) return;
  if (!MOCK) { h.hidden = true; return; }
  const scenes = ['normal', 'running', 'long', 'bulk', 'breaker', 'nologin', 'needlogin'];
  const cur = (window.MockBridge && window.MockBridge.scene) || 'normal';
  h.textContent = '演示数据 · 场景 ' + cur + ' · 点这里换';
  h.title = '这是 mock 宿主的假数据，不是引擎输出。点一下切到下一个场景。';
  h.style.cursor = 'pointer';
  h.addEventListener('click', () => {
    const i = scenes.indexOf(cur);
    location.search = '?scene=' + scenes[(i + 1) % scenes.length];
  });
})();

/* ---------------------------- 启动 ---------------------------- */

showPanel('tools');
applyAvatar();
autoGrow();
renderConvs();     // 先画「正在读取…」，免得左栏在第一批数据回来前是空的

// 主动握手。这一行必须在所有监听器都接好之后 —— 它一发出去，
// 宿主的 hist / convs / models 就会回来，而那些消息由 handle() 接。
sayHello();
