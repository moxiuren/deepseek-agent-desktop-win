#!/usr/bin/env python3
"""临时诊断：定位 tts.js 在离线替身环境里断在哪一环。"""
from __future__ import annotations

import functools, http.server, json, socket, sys, threading, time
from pathlib import Path

ROOT = Path(r"C:\Users\Admin\Documents\Projects\deepseek-agent-desktop")
WEB_DIR = ROOT / "shell/DeepSeekAgent.Web" / "Assets" / "web"
FIX = ROOT / "tools" / "e2e" / "fixtures" / "opus_packets.json"
packets = [bytes.fromhex(h) for h in json.loads(FIX.read_text(encoding="utf-8"))["packets"]]

s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
h = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(WEB_DIR))
h.log_message = lambda *a, **k: None
srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), h)
threading.Thread(target=srv.serve_forever, daemon=True).start()

STUB = r"""
(() => {
  const listeners = [], sent = [];
  const wv = {
    postMessage(msg) { sent.push(msg); },
    addEventListener(t, fn) { if (t === 'message') listeners.push(fn); },
    removeEventListener(t, fn) { const i = listeners.indexOf(fn); if (i >= 0) listeners.splice(i, 1); },
  };
  window.chrome = window.chrome || {};
  window.chrome.webview = wv;
  window.__hostSend = (m) => { for (const fn of listeners.slice()) { try { fn({ data: m }); } catch (e) { console.error(e); } } };
  window.__hostSent = sent;
  window.__caps = {
    AudioDecoder: typeof window.AudioDecoder,
    AudioContext: typeof window.AudioContext,
    WebSocket: typeof window.WebSocket,
  };
})();
"""

REC = r"""
(() => {
  const Real = window.AudioContext;
  const orig = AudioBufferSourceNode.prototype.start;
  window.__rec = { started: 0, samples: 0, chunks: [] };
  class Fast extends Real { get currentTime() { return super.currentTime * 20; } }
  window.AudioContext = Fast;
  AudioBufferSourceNode.prototype.start = function (w, o, d) {
    window.__rec.started++;
    if (this.buffer) { window.__rec.samples += this.buffer.length;
      window.__rec.chunks.push({ len: this.buffer.length, rate: this.buffer.sampleRate }); }
    return orig.call(this, w, o, d);
  };
})();
"""

from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome", headless=True)
    pg = b.new_page()
    logs = []
    pg.on("console", lambda m: logs.append(f"[{m.type}] {m.text}"))
    pg.on("pageerror", lambda e: logs.append(f"[pageerror] {e}"))
    pg.add_init_script(STUB)
    pg.add_init_script(REC)

    ws_events = []

    def handler(ws):
        ws_events.append("connected " + ws.url[:120])
        ws.on_message(lambda m: ws_events.append("up:" + str(m)[:100]))
        ws.send(json.dumps({"event": "ready", "audio_id": "a1", "format": "opus"}))
        for i, pkt in enumerate(packets):
            ws.send((i + 1).to_bytes(4, "big") + pkt)
            time.sleep(0.02)
        ws.send(json.dumps({"event": "finish", "code": 0, "msg": ""}))
        ws_events.append("sent all")

    pg.route_web_socket("wss://chat.deepseek.com/**", handler)
    pg.goto(f"http://127.0.0.1:{port}/index.html", wait_until="load")
    pg.wait_for_timeout(400)

    print("caps:", pg.evaluate("window.__caps"))
    print("kind:", pg.evaluate("window.Bridge.kind"))

    pg.evaluate("window.Bridge.emit({t:'clear'})")
    pg.evaluate("""() => window.__hostSend({t:'status',running:false,s:{}})""")
    pg.evaluate("""() => window.__hostSend({t:'item', item:{seq:1,kind:'Assistant',
        text:'测试朗读', detail:'', messageId:'msg-x', tool:null, thought:null}})""")
    pg.wait_for_timeout(200)

    pg.evaluate("""() => {
        const orig = window.chrome.webview.postMessage;
        window.chrome.webview.postMessage = function (m) {
            orig.call(this, m);
            if (m && m.t === 'tts') setTimeout(() => window.__hostSend({
                t:'tts', req:m.req, messageId:m.messageId, sid:'sess-1',
                ticket:'tk-1', error:''}), 20);
        };
    }""")
    pg.click(".tts-btn")
    for i in range(6):
        pg.wait_for_timeout(500)
        print(f"--- t={i*0.5+0.5}s ---")
        print("  phase:", pg.evaluate("window.Tts.phase"))
        print("  rec:", pg.evaluate("window.__rec"))
    print("\nWS 事件:", ws_events[:6], "...共", len(ws_events))
    print("\n控制台:")
    for l in logs[-25:]:
        print("  ", l)
    b.close()
srv.shutdown()
