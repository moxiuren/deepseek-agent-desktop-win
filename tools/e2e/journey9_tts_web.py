#!/usr/bin/env python3
"""朗读（TTS）前端全链路验证 —— 零额度、零网络、可重复。

## 这道闸要证明什么

`tts.js` 是整条朗读链路里**唯一没有被任何测试覆盖过**的一段：
引擎侧签票有 `probe_tts_endpoint.py` 实测过，宿主侧三条命令能编译通过，
但前端那 500 行「连 WS → 解 Opus → 排播放 → 回 ack」从头到尾没跑过一次。
它写的时候只能靠静态分析，而静态分析证明不了「能不能出声」。

真实点一次朗读会消耗额度、且账号此前触发过限流（code 5）。
所以这里换个办法：**让浏览器把服务端扮演掉**。

    · 前端资源是产品代码本身（index.html / app.js / tts.js / app.css）
    · 宿主也是假的 —— 页面里塞一个 `chrome.webview` 桩，前端就以为
      自己在 WebView2 里，走真 `postMessage` 路径、不走 mock
    · TTS 服务端用 Playwright 的 `route_web_socket` 接管，
      回放预先从 ffmpeg 生成的**真 Opus 包**（不是伪音频）
    · 页面里装一台「录音机」：把送进 `AudioBufferSourceNode.start()`
      的每一块音频的样本抓下来

最后检查的是**真的把声音排进了播放设备**：抓到的样本数与时长对得上，
且 ack 的 `played_seq` 真的在推进（不是虚报）。

## 为什么必须桩掉宿主，而不是直接用 mock

`bridge.js` 的判断是 `window.chrome.webview` 在不在。用 mock 的话
`Bridge.kind === 'mock'`，`tts.js` 发出去的三条命令会进 `MockBridge.handle`，
而 mock 里根本没有这三个 case —— 消息石沉大海，`play()` 的 Promise 永远
不 resolve，测出来是「卡在 loading」，跟真实缺陷长得一模一样，无法区分。

桩一个 `chrome.webview` 反而**更接近真实**：postMessage 真的被调用、
消息真的被序列化、前端的 `addEventListener('message')` 真的被触发。

## 覆盖的路径

  1. 按钮**只在**定稿且带 messageId 的助手气泡上出现
  2. 点击 → 请求票据 → 连 WS → 收到 ready
  3. 二进制帧解析（4 字节大端 seq + Opus 负载）→ 解码 → 播放
  4. 预缓冲（首帧后等 400ms 才开声，短消息不死等）
  5. ack 上行：`received_seq` 与 `played_seq` 都在推进，且 played <= received
  6. 播放结束（finish code=0）→ 状态回 idle、按钮复位
  7. 再点一次 = 停止 → 发 abort、队列清空
  8. 服务端拒绝（code 5 限流）→ toast 出中文提示、不进对话流水
  9. 按钮语义：切换播放时旧按钮复位、新按钮点亮

用法：
    python tools/e2e/journey9_tts_web.py
    python tools/e2e/journey9_tts_web.py --headed    # 看得见窗口
"""
from __future__ import annotations

import functools
import http.server
import json
import socket
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = ROOT / "shell" / "DeepSeekAgent.Web" / "Assets" / "web"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "opus_packets.json"

#: 前端拿到的 messageId 长什么样 —— 真实的是服务端返回的字符串 id。
MID = "msg-test-0001"


# ---------------------------------------------------------------- 页面侧

#: 在 index.html 加载**之前**注入的宿主桩。
#:
#: 为什么用 `add_init_script` 而不是在 goto 之后 `evaluate`：
#: `bridge.js` 是同步执行的 IIFE，它在脚本加载那一刻就检查
#: `window.chrome.webview` 并决定走哪条路。goto 之后再补已经晚了 ——
#: `Bridge.kind` 已经固化成 'mock'。
STUB_HOST = r"""
(() => {
  const listeners = [];
  const sent = [];          // 前端 → 宿主 的全部消息，供测试断言
  const wv = {
    postMessage(msg) { sent.push(msg); },
    addEventListener(type, fn) { if (type === 'message') listeners.push(fn); },
    removeEventListener(type, fn) {
      const i = listeners.indexOf(fn);
      if (i >= 0) listeners.splice(i, 1);
    },
  };
  window.chrome = window.chrome || {};
  window.chrome.webview = wv;

  // 测试脚本用它把「宿主发来的消息」推进页面。
  window.__hostSend = (m) => {
    for (const fn of listeners.slice()) {
      try { fn({ data: m }); } catch (e) { console.error('[host-stub]', e); }
    }
  };
  window.__hostSent = sent;
})();
"""

#: 录音机：把送进 AudioBufferSourceNode 的每一块音频抓下来。
#:
#: 为什么不直接听：headless Chrome 没有音频设备，`AudioContext` 仍会跑
#: （Chromium 用 null sink），但耳朵听不到。抓样本是唯一能自动判定的证据 ——
#: 它能区分「真的解码出了 PCM」与「只是状态机走了一遍」。
#:
#: 同时把 `currentTime` 的快进也接管掉：真实播放 0.8 秒的音频要等 0.8 秒，
#: 而这里的判据是「音频被排进了时间轴并推进到队尾」，不是「等够真实时长」。
#: 所以让时钟以 20 倍速走 —— 这是**唯一**的失真点，且它不改变代码路径。
RECORDER = r"""
(() => {
  const RealCtx = window.AudioContext;
  const origStart = AudioBufferSourceNode.prototype.start;

  const rec = {
    chunks: [],       // [{frames, rate, startAt, peak}]
    samples: 0,       // 累计样本数
    started: 0,       // start() 调用次数
    ctxs: 0,
    peak: 0,          // 所有块里的最大绝对振幅
  };
  window.__rec = rec;

  // 让 AudioContext 的时钟走得快一些，否则一条 0.8 秒的音频要真等 0.8 秒。
  const SPEED = 20;
  class FastCtx extends RealCtx {
    constructor(...a) { super(...a); rec.ctxs++; }
    get currentTime() { return super.currentTime * SPEED; }
  }
  window.AudioContext = FastCtx;
  // tts.js 里也写了 `new AudioContext({sampleRate:24000})`，
  // 上面这层包装对带参数的调用同样生效（子类透传 ...a）。

  AudioBufferSourceNode.prototype.start = function (when, offset, dur) {
    const b = this.buffer;
    if (b) {
      // 取整块的**峰值**而不是开头几个样本。
      // 440Hz 正弦波的第一个周期起点天然接近零，抓 head 会得到一串
      // -0.0 / 0，判「非全零」时只能靠运气靠后面某块偶然命中 ——
      // 那是假通过。峰值对「真 PCM」与「静音」有强区分度。
      const d = b.getChannelData(0);
      let pk = 0;
      for (let i = 0; i < d.length; i++) {
        const v = d[i] < 0 ? -d[i] : d[i];
        if (v > pk) pk = v;
      }
      if (pk > rec.peak) rec.peak = pk;
      rec.chunks.push({
        frames: b.length,
        rate: b.sampleRate,
        channels: b.numberOfChannels,
        startAt: when,
        peak: +pk.toFixed(4),
      });
      rec.samples += b.length;
    }
    rec.started++;
    return origStart.call(this, when, offset, dur);
  };
})();
"""

#: 抓 toast 文案 —— 失败路径的判据。
CAPTURE_TOAST = r"""
(() => {
  window.__toasts = [];
  let real = window.AppToast;
  Object.defineProperty(window, 'AppToast', {
    configurable: true,
    get() { return (t) => { window.__toasts.push(String(t)); if (real) real(t); }; },
    set(v) { real = v; },
  });
})();
"""


def make_items() -> list[dict]:
    """按 `WebBridge.Item()` 的字段契约造两条助手气泡。

    第一条带 messageId（定稿，应该有朗读按钮），
    第二条不带（例如重放出来的历史消息，不该有按钮）。
    这正是要验的「按钮只在有 id 时出现」。
    """
    return [
        {"seq": 1, "kind": "Goal", "text": "你好，介绍一下你自己", "detail": "",
         "messageId": "", "tool": None, "thought": None},
        {"seq": 2, "kind": "Assistant",
         "text": "我是 DeepSeek Agent。这是一条**可以朗读**的回答，用来验证整条链路。",
         "detail": "", "messageId": MID, "tool": None, "thought": None},
        {"seq": 3, "kind": "Assistant",
         "text": "这条来自历史重放，没有服务端消息 id，因此不该出现朗读按钮。",
         "detail": "", "messageId": "", "tool": None, "thought": None},
    ]


#: Chromium 有一张硬编码的**禁用端口表**（ERR_UNSAFE_PORT），撞上就直接
#: 拒绝导航，页面连字节都不会下载。这张表里的号段散落在常见范围里：
#: 1, 7, 9, 11, 13, 15, 17, 19, 20, 21, 22, 23, 25, 37, 42, 43, 53, 69, 77,
#: 79, 87, 95, 101, 102, 103, 104, 109, 110, 111, 113, 115, 117, 119, 123,
#: 135, 137, 139, 143, 161, 179, 389, 427, 465, 512, 513, 514, 515, 526, 530,
#: 531, 532, 540, 548, 554, 556, 563, 587, 601, 636, 989, 990, 993, 995,
#: 1719, 1720, 1723, 2049, 3659, 4045, 5060, 5061, 6000, 6566, 6665-6669,
#: 6697, 10080。
#:
#: 随机绑 0 号端口的写法本身没错，但**有概率**落进这张表 —— 一旦落进去，
#: 报错是 `ERR_UNSAFE_PORT`，看起来像环境问题，实际是选端口没避开雷区。
#: 实测就撞到过 6669（IRC），所以这里改成「随机 + 过滤 + 重试」。
_UNSAFE_PORTS = {
    1, 7, 9, 11, 13, 15, 17, 19, 20, 21, 22, 23, 25, 37, 42, 43, 53, 69, 77,
    79, 87, 95, 101, 102, 103, 104, 109, 110, 111, 113, 115, 117, 119, 123,
    135, 137, 139, 143, 161, 179, 389, 427, 465, 512, 513, 514, 515, 526, 530,
    531, 532, 540, 548, 554, 556, 563, 587, 601, 636, 989, 990, 993, 995,
    1719, 1720, 1723, 2049, 3659, 4045, 5060, 5061, 6000, 6566,
    6665, 6666, 6667, 6668, 6669, 6697, 10080,
}


def serve(directory: Path) -> tuple[http.server.ThreadingHTTPServer, int]:
    """起一个静态文件服务，返回 (server, port)。

    端口必须在 Chromium 的可用范围内 —— 见 `_UNSAFE_PORTS` 的说明。
    连续撞雷的概率极低，但仍然重试几次而不是直接失败。
    """
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(directory))
    handler.log_message = lambda *a, **k: None  # type: ignore[assignment]

    for _ in range(20):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        if port in _UNSAFE_PORTS:
            continue
        try:
            srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
        except OSError:
            continue  # 关掉探测 socket 到真正 bind 之间被别的进程抢了
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv, port

    raise SystemExit("[错误] 连续 20 次都没能绑到 Chromium 允许的端口")


# ---------------------------------------------------------------- WS 服务端替身

def install_tts_server(page, packets: list[bytes], *, code: int = 0,
                       msg: str = "", pace: float = 0.02,
                       record: dict | None = None) -> None:
    """接管 `wss://chat.deepseek.com/api/v0/chat/tts/`。

    ## 为什么能在 http 页面上拦到 wss

    `route_web_socket` 拦的是**客户端发起连接这个动作**，不真的建 TCP。
    页面 origin 是 `http://127.0.0.1:PORT`，但 tts.js 里写死了
    `wss://chat.deepseek.com/...` —— Playwright 照样拦得到，因为它在
    浏览器内部把 WebSocket 对象换成了受控实现。

    ## 回放节奏

    按帧间隔 20ms（Opus 帧长）推。太快会让前端来不及解码、看不出
    预缓冲逻辑；太慢（真实速度）又没必要 —— 这里量的是代码路径，
    不是服务端吞吐。

    ## 参数

    `code` / `msg` —— finish 帧的错误码。传 5 就是「限流」，用来验失败路径。
    `record` —— 传入一个 dict，会把上行帧与连接 URL 记进去，供断言。
    """
    import time

    rec = record if record is not None else {}

    def handler(ws) -> None:
        # ws.url 里带着 ticket / message_id，前端拼得对不对可以在这里判。
        rec["url"] = ws.url
        ws.on_message(lambda m: rec.setdefault("uplink", []).append(m))

        ws.send(json.dumps({"event": "ready", "audio_id": "test-audio-1", "format": "opus"}))
        for i, pkt in enumerate(packets):
            # 4 字节大端 seq + 裸 Opus 负载，与真实帧格式一致。
            ws.send((i + 1).to_bytes(4, "big") + pkt)
            time.sleep(pace)
        ws.send(json.dumps({"event": "finish", "code": code, "msg": msg}))

    page.route_web_socket("wss://chat.deepseek.com/**", handler)


def check_build_sync() -> tuple[bool, str]:
    """确认输出目录里的前端资源与源目录**逐字节一致**。

    ## 为什么这必须是第一道判据

    这个壳的前端是 `Content ... CopyToOutputDirectory="PreserveNewest"`，
    WebView2 通过 `SetVirtualHostNameToFolderMapping` 从
    `bin/<配置>/net8.0-windows/Assets/web` **读磁盘**。

    于是有个只在真实运行时才暴露的断层：
      · `node --check` 查的是**源目录**
      · 本文件下面所有断言跑的也是**源目录**（本地 http 服务指向它）
      · 而用户双击运行的那个 exe 读的是**输出目录**

    改完文件忘了 `dotnet build`，前三项全绿，真实壳里跑的却是旧代码。
    实测踩过一次：加的音色按钮在真实 WebView2 里根本不存在，而旅程
    测试 29 条全通过 —— 测试对象和运行对象不是同一份文件。

    `PreserveNewest` 只在构建时比较时间戳，不构建就永远不复制。
    所以这道检查不能省。
    """
    out = (ROOT / "shell" / "DeepSeekAgent.Web" / "bin" / "Release"
           / "net8.0-windows" / "Assets" / "web")
    if not out.exists():
        return False, f"输出目录不存在（还没构建过？）：{out}"

    names = ["index.html", "app.js", "tts.js", "app.css", "bridge.js", "theme-boot.js"]
    bad = []
    for n in names:
        src = WEB_DIR / n
        dst = out / n
        if not src.exists():
            continue
        if not dst.exists():
            bad.append(f"{n}: 输出目录缺文件")
        elif src.read_bytes() != dst.read_bytes():
            bad.append(f"{n}: 源 {src.stat().st_size}B vs 输出 {dst.stat().st_size}B")

    if bad:
        return False, "；".join(bad) + "  → 请先 dotnet build -c Release"
    return True, f"{len(names)} 个资源逐字节一致"


def main() -> int:
    from playwright.sync_api import sync_playwright

    if not FIXTURE.exists():
        print(f"[错误] 缺夹具 {FIXTURE}（应由 fixtures 生成步骤产出）")
        return 2
    packets = [bytes.fromhex(h) for h in json.loads(FIXTURE.read_text(encoding="utf-8"))["packets"]]
    items = make_items()

    srv, port = serve(WEB_DIR)
    ok_all = True
    report: list[tuple[str, bool, str]] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok_all
        ok_all = ok_all and cond
        report.append((name, cond, detail))

    #: 宿主桩的应答开关。不同场景需要宿主回不同的东西：
    #: 正常票据 / 错误文案 / 什么都不回（用来测等待中被打断）。
    #:
    #: 音色的三条命令（ttsVoices / ttsVoice）也在这里应答 —— 它们不消耗
    #: 朗读配额，但走的是同一个 postMessage 通道，桩不接就会一直挂着。
    HOST_REPLY = r"""
    () => {
      window.__replyMode = 'ok';
      window.__voiceWrites = [];
      // **契约按真实服务端来，不按我们希望的来。**
      //
      // 2026-10-19 实测：`isDefault` 是出厂默认（永远只有 mira 为 true），
      // 与「用户当前选哪个」是两件事；后者是服务端的 `current_voice_id`，
      // 宿主序列化成 `isCurrent`。桩如果拿 isDefault 冒充选中态，
      // 就会和曾经的线上实现一起错 —— 测试全绿而真实面板永远显示 mira。
      let currentVoice = 'mira';
      const mkVoices = () => [
        { id: 'mira',   name: '贝壳', desc: '清晰自然', gender: 'female',
          languages: ['普通话'], isDefault: true,  isCurrent: currentVoice === 'mira',   demoUrl: '' },
        { id: 'echo',   name: '白浪', desc: '低沉沉稳', gender: 'male',
          languages: ['普通话'], isDefault: false, isCurrent: currentVoice === 'echo',   demoUrl: '' },
        { id: 'stella', name: '海星', desc: '明亮轻快', gender: 'female',
          languages: ['普通话', '粤语'], isDefault: false, isCurrent: currentVoice === 'stella', demoUrl: '' },
      ];
      window.__voices = mkVoices();

      const orig = window.chrome.webview.postMessage;
      window.chrome.webview.postMessage = function (msg) {
        orig.call(this, msg);
        if (!msg) return;
        const mode = window.__replyMode;

        if (msg.t === 'ttsVoices') {
          // 每次读都重新算 isCurrent —— 服务端的行为就是这样，
          // 回读能看到刚写进去的值。
          setTimeout(() => window.__hostSend(
            { t: 'ttsVoices', req: msg.req, items: mkVoices(), error: '' }), 20);
          return;
        }
        if (msg.t === 'ttsVoice') {
          window.__voiceWrites.push(msg.id);
          // **写入要真的改变状态**。桩只回 ok 而不改 currentVoice 的话，
          // 「写成功但 UI 没变」这类缺陷就永远测不出来。
          currentVoice = msg.id;
          // 宿主回报的 id 才是真的选中的那个 —— 前端必须用它，
          // 而不是点击时记下的那个。
          setTimeout(() => window.__hostSend(
            { t: 'ttsVoice', req: msg.req, ok: true, id: msg.id, error: '' }), 20);
          return;
        }
        if (msg.t !== 'tts') return;
        if (mode === 'silent') return;
        setTimeout(() => window.__hostSend(
          mode === 'error'
            ? { t: 'tts', req: msg.req, messageId: msg.messageId,
                sid: '', ticket: '', error: '朗读太频繁，请稍后再试' }
            : { t: 'tts', req: msg.req, messageId: msg.messageId,
                sid: 'sess-test-1', ticket: 'ticket-test-abcdef', error: '' }
        ), 20);
      };
    }
    """

    def seed(page) -> None:
        """重置页面到「三条气泡、无朗读状态」。"""
        page.evaluate("() => { if (window.Tts.phase !== 'idle') window.Tts.stop(); }")
        page.evaluate("window.Bridge.emit({t:'clear'})")
        page.evaluate("""(items) => {
            window.__hostSend({ t: 'status', running: false, s: {} });
            for (const it of items) window.__hostSend({ t: 'item', item: it });
        }""", items)
        page.evaluate("() => { window.__rec.started = 0; window.__rec.samples = 0; "
                      "window.__rec.chunks.length = 0; window.__rec.peak = 0; "
                      "window.__toasts.length = 0; }")
        page.wait_for_timeout(150)

    def btn_state(page) -> dict:
        return page.evaluate("""() => {
            const bs = [...document.querySelectorAll('.tts-btn')];
            return {
                count: bs.length,
                on: bs.filter(b => b.classList.contains('on')).map(b => b.dataset.seq),
                texts: bs.map(b => b.textContent),
                phase: window.Tts.phase,
                seq: window.Tts.seq,
            };
        }""")

    # ---- 构建同步：测试对象必须就是用户运行的那份 ----
    sync_ok, sync_detail = check_build_sync()
    check("输出目录与源目录逐字节一致", sync_ok, sync_detail)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless="--headed" not in sys.argv)
            page = browser.new_page(viewport={"width": 1003, "height": 715})
            errs: list[str] = []
            page.on("pageerror", lambda e: errs.append(str(e)))

            page.add_init_script(STUB_HOST)
            page.add_init_script(RECORDER)
            page.add_init_script(CAPTURE_TOAST)
            wsrec: dict = {}
            install_tts_server(page, packets, record=wsrec)
            # 注意：route_web_socket 必须在 goto **之前**注册。之后再挂不生效，
            # 实测表现为连接直接 close:1006、帧全丢。

            page.goto(f"http://127.0.0.1:{port}/index.html", wait_until="load")
            page.wait_for_timeout(400)
            page.evaluate(HOST_REPLY)

            # ---- 0. 走的是「真宿主」路径，不是 mock ----
            kind = page.evaluate("window.Bridge.kind")
            check("前端识别为 webview2（非 mock）", kind == "webview2", f"kind={kind}")

            # ---- 1. 按钮挂点：只在带 messageId 的定稿气泡上出现 ----
            seed(page)
            st0 = btn_state(page)
            check("只有带 messageId 的气泡出现朗读按钮", st0["count"] == 1, f"按钮数={st0['count']}")

            bar_info = page.evaluate("""() => {
                const b = document.querySelector('.tts-btn');
                if (!b) return null;
                const msg = b.closest('.msg');
                return { mid: b.dataset.mid, seq: b.dataset.seq,
                         text: msg ? msg.querySelector('.bubble').textContent.slice(0, 20) : '' };
            }""")
            check("按钮挂在正确的气泡、带正确 messageId",
                  bool(bar_info) and bar_info["mid"] == MID, f"{bar_info}")

            # ---- 2. 完整播放：连 WS → 解码 → 排播放 → 收尾 ----
            page.click(".tts-btn")
            page.wait_for_timeout(1800)

            rec = page.evaluate("window.__rec")
            st1 = btn_state(page)

            check("解码并排入播放的音频块 > 0", rec["started"] > 0, f"start() {rec['started']} 次")
            check("累计样本数覆盖约 0.8 秒 @24k", rec["samples"] > 15000,
                  f"{rec['samples']} 样本 ≈ {rec['samples']/24000:.2f}s")
            check("音频块采样率为 24kHz",
                  all(c["rate"] == 24000 for c in rec["chunks"]),
                  f"采样率集合 {sorted({c['rate'] for c in rec['chunks']})}")
            check("解出的是真 PCM 而非静音（峰值 > 0.1）", rec["peak"] > 0.1,
                  f"峰值 {rec['peak']}")
            check("播放结束后状态回到 idle", st1["phase"] == "idle", f"phase={st1['phase']}")
            check("按钮复位成「朗读」",
                  all("朗读" in t for t in st1["texts"]), f"文案={st1['texts']}")

            # ---- 3. ack 上行：received_seq 与 played_seq 都推进，且 played <= received ----
            uplink = [json.loads(x) for x in wsrec.get("uplink", []) if isinstance(x, str)]
            acks = [u for u in uplink if u.get("event") == "ack"]
            recv_max = max((a.get("received_seq", -1) for a in acks), default=-1)
            play_max = max((a.get("played_seq", -1) for a in acks), default=-1)
            check("ack 上行存在且 received_seq 推进到末帧",
                  len(acks) > 0 and recv_max == len(packets),
                  f"{len(acks)} 条 ack, received_seq 最大 {recv_max} / 共 {len(packets)} 帧")
            check("played_seq 真的在推进（不是虚报）", play_max > 0, f"played_seq 最大 {play_max}")
            check("played_seq 不超过 received_seq",
                  play_max <= recv_max, f"played={play_max} received={recv_max}")

            ws_url = wsrec.get("url") or ""
            check("WS 连接带齐 ticket / mode / format",
                  all(k in ws_url for k in ("ticket=ticket-test-abcdef", "mode=manual", "format=opus")),
                  ws_url.split("?")[-1][:80] if "?" in ws_url else ws_url[:80])

            # ---- 4. 再点一次 = 停止：发 abort、按钮复位 ----
            # 让服务端慢慢推，保证点击时还在播
            wsrec.clear()
            install_tts_server(page, packets, pace=0.35, record=wsrec)
            page.click(".tts-btn")
            page.wait_for_timeout(500)
            mid_play = btn_state(page)
            check("播放中按钮显示「停止」且点亮",
                  mid_play["phase"] == "playing" and len(mid_play["on"]) == 1,
                  f"phase={mid_play['phase']} on={mid_play['on']} texts={mid_play['texts']}")

            page.click(".tts-btn")
            page.wait_for_timeout(400)
            st2 = btn_state(page)
            aborts = sum(1 for x in wsrec.get("uplink", [])
                         if isinstance(x, str) and '"abort"' in x)
            check("再点一次发出 abort 并回到 idle",
                  aborts >= 1 and st2["phase"] == "idle",
                  f"abort {aborts} 条, phase={st2['phase']}")
            check("停止后按钮全部复位成「朗读」",
                  all("朗读" in t for t in st2["texts"]) and not st2["on"],
                  f"文案={st2['texts']} on={st2['on']}")

            # ---- 5. 服务端拒绝（code=5 限流）→ 中文 toast，不进对话流水 ----
            install_tts_server(page, [], code=5, msg="rate limited", record=wsrec)
            seed(page)
            n_before = page.evaluate("document.querySelectorAll('.msg').length")
            page.click(".tts-btn")
            page.wait_for_timeout(700)
            toasts = page.evaluate("window.__toasts")
            n_after = page.evaluate("document.querySelectorAll('.msg').length")
            st3 = btn_state(page)
            check("服务端拒绝时弹出中文提示",
                  any("频繁" in t for t in toasts), f"toast={toasts}")
            check("朗读失败不污染对话流水", n_after == n_before,
                  f"{n_before} → {n_after} 条")
            check("失败后状态回到 idle、按钮复位",
                  st3["phase"] == "idle" and all("朗读" in t for t in st3["texts"]),
                  f"phase={st3['phase']} texts={st3['texts']}")

            # ---- 6. 宿主返回错误（拿不到票据）→ 同样只 toast ----
            install_tts_server(page, packets, record=wsrec)
            seed(page)
            page.evaluate("() => { window.__replyMode = 'error'; }")
            page.click(".tts-btn")
            page.wait_for_timeout(500)
            toasts2 = page.evaluate("window.__toasts")
            st4 = btn_state(page)
            check("拿不到票据时给出提示且复位",
                  any("频繁" in t for t in toasts2) and st4["phase"] == "idle",
                  f"toast={toasts2} phase={st4['phase']}")
            page.evaluate("() => { window.__replyMode = 'ok'; }")

            # ---- 7. 切换播放：旧按钮复位、新按钮点亮 ----
            # 给第三条气泡补一个 messageId，制造「两条都能读」的局面
            seed(page)
            page.evaluate("""(mid) => {
                window.__hostSend({ t: 'item', item: {
                    seq: 3, kind: 'Assistant', text: '第二条可朗读的回答。',
                    detail: '', messageId: mid, tool: null, thought: null } });
            }""", "msg-test-0002")
            page.wait_for_timeout(150)
            install_tts_server(page, packets, pace=0.35, record=wsrec)
            btns = page.evaluate("document.querySelectorAll('.tts-btn').length")
            page.click(f'.tts-btn[data-mid="{MID}"]')
            page.wait_for_timeout(400)
            first = btn_state(page)
            page.click('.tts-btn[data-mid="msg-test-0002"]')
            page.wait_for_timeout(500)
            second = btn_state(page)
            check("有两条可朗读气泡", btns == 2, f"按钮数={btns}")
            check("切换播放时旧按钮复位、新按钮点亮",
                  first["on"] == ["2"] and second["on"] == ["3"],
                  f"先 {first['on']} → 后 {second['on']}")

            # ---- 8. 音色面板：读取 → 选中 → 切换 ----
            page.evaluate("() => { if (window.Tts.phase !== 'idle') window.Tts.stop(); }")
            page.click("#btnVoice")
            page.wait_for_timeout(400)

            panel = page.evaluate("""() => {
                const pop = document.getElementById('voicePop');
                const rows = [...pop.querySelectorAll('.vp-item')];
                return {
                    open: !pop.hidden,
                    expanded: document.getElementById('btnVoice')
                        .getAttribute('aria-expanded'),
                    names: rows.map(r => r.querySelector('.vp-name').textContent),
                    on: rows.filter(r => r.classList.contains('on'))
                             .map(r => r.dataset.id),
                    tags: rows.map(r => [...r.querySelectorAll('.vp-tag')]
                             .map(t => t.textContent)),
                };
            }""")
            check("音色面板能打开并列出全部音色",
                  panel["open"] and len(panel["names"]) == 3,
                  f"open={panel['open']} names={panel['names']}")
            check("当前音色（mira）被标为选中",
                  panel["on"] == ["mira"], f"选中={panel['on']}")
            check("音色带性别与语言标签",
                  panel["tags"] and "粤语" in panel["tags"][2],
                  f"{panel['tags']}")

            page.click('.vp-item[data-id="stella"]')
            page.wait_for_timeout(400)
            after = page.evaluate("""() => {
                const rows = [...document.querySelectorAll('.vp-item')];
                return {
                    on: rows.filter(r => r.classList.contains('on'))
                             .map(r => r.dataset.id),
                    writes: window.__voiceWrites,
                };
            }""")
            check("切换音色发出写请求且选中态跟着变",
                  after["writes"] == ["stella"] and after["on"] == ["stella"],
                  f"写={after['writes']} 选中={after['on']}")

            # **回归判据**：选中态必须来自回读的 isCurrent，不能来自 isDefault。
            # 曾经的实现拿 isDefault 当选中态，而它是出厂默认（永远 mira），
            # 于是切换写成功了、UI 却永远显示 mira —— 且不报任何错。
            # 桩现在写入会真的改 currentVoice，所以这条能抓到该缺陷。
            cur = page.evaluate("""() => {
                const rows = [...document.querySelectorAll('.vp-item')];
                return {
                    on: rows.filter(r => r.classList.contains('on')).map(r => r.dataset.id),
                    defaultTag: rows.filter(r =>
                        [...r.querySelectorAll('.vp-desc')].some(d => d.textContent === '默认'))
                        .map(r => r.dataset.id),
                };
            }""")
            check("选中态跟随服务端回读（isCurrent 而非 isDefault）",
                  cur["on"] == ["stella"], f"选中={cur['on']}")
            check("出厂默认与当前选中是两个独立标记",
                  cur["defaultTag"] == ["mira"],
                  f"默认标签={cur['defaultTag']} 选中={cur['on']}")

            # 点已选中的音色不该再发一次写 —— 那是白改一次账号偏好
            page.click('.vp-item[data-id="stella"]')
            page.wait_for_timeout(250)
            writes2 = page.evaluate("window.__voiceWrites")
            check("重复点击已选中的音色不重复写",
                  len(writes2) == 1, f"写请求 {writes2}")

            # Esc 关闭
            page.keyboard.press("Escape")
            page.wait_for_timeout(150)
            closed = page.evaluate("document.getElementById('voicePop').hidden")
            check("Esc 能关闭音色面板", closed is True, f"hidden={closed}")

            check("页面无异常", not errs, "; ".join(errs[:3]))
            browser.close()
    finally:
        srv.shutdown()

    print("=" * 66)
    print("旅程 9 · 朗读（TTS）前端全链路 —— 离线替身服务端")
    print("=" * 66)
    for name, cond, detail in report:
        print(f"  [{'通过' if cond else '错误'}] {name}" + (f"  ({detail})" if detail else ""))
    print("-" * 66)
    print("  判定: " + ("通过" if ok_all else "不通过"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
