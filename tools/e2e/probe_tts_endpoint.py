#!/usr/bin/env python3
"""逆向 DeepSeek 网页版「朗读」的完整链路 —— 只读探测，不发送聊天消息。

## 结论（2026-10-18 实测）

网页版的朗读**不是**浏览器 Web Speech API，是**服务端流式 TTS**：

    POST /api/v0/auth/ticket          {scope:"tts"}  -> ticket (36 字符)
    GET  /api/v0/chat/tts/voices                     -> 音色列表（4 个，全支持中文）
    POST /api/v0/chat/tts/voice       {voice_id}     -> 切换音色
    WSS  /api/v0/chat/tts/?chat_session_id=&message_id=&ticket=&mode=manual&format=opus

## 为什么能确定是服务端合成

前端 bundle（`web-tts.9ddcbab81d.js`，40 KB）里 `speechSynthesis` 出现 **0 次**，
而下面这些**只在服务端音频流场景**才存在的概念全都有：

    decoderKind / firstFrameLatencyMs / sampleRate / underrunCount
    WebCodecs AudioDecoder({codec:"opus", sampleRate:24000, channels:1})
    WebAssembly opus 解码器兜底（chunk 61271）
    rejected_code: QUOTA_EXCEEDED / RATE_LIMIT_REACHED / NO_CONTENT / UNSUPPORTED_LANGUAGE

Web Speech API 没有「解码器」「首帧延迟」「欠载」「配额」这些概念 —— 它是
把文本丢给操作系统语音包，同步吐 PCM，不存在网络流。

## 音频帧格式

二进制帧 = 4 字节大端 seq + Opus 负载（`new DataView(e).getUint32(0, false)`）。
采样率 24 kHz 单声道，前端用 WebCodecs 或 wasm 解码后喂 AudioContext。

## 服务端事件（文本帧）

    {"event":"ready",  "audio_id":..., "format":"opus"}
    {"event":"finish", "code":0, "msg":""}
    {"event":"finish", "code":6, "msg":"no_content"}
    {"event":"finish", "code":5, "msg":"rate_limit_reached"}

错误码表（模块 89791 的 `me`）：0 SUCCESS / 1 INTERNAL_ERROR / 2 INVALID_INPUT /
3 SERVICE_ERROR / 4 QUOTA_EXCEEDED / 5 RATE_LIMIT_REACHED / 6 NO_CONTENT /
7 UNSUPPORTED_LANGUAGE / 8 VOICE_UNSUPPORTED_LANGUAGE / 9 CONTENT_FILTER

客户端上行（JSON 文本帧）：

    {"event":"ack",    "received_seq":N, "played_seq":M}
    {"event":"abort",  "reason":"..."}
    {"event":"finish"}

## 本脚本做什么

只做只读验证：取 ticket -> 连 WS -> 记录收到的帧类型/大小/序号。
不发送任何聊天消息，不打印 token 内容（只打印长度）。

用法：
    python tools/e2e/probe_tts_endpoint.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "engine"))

from gad_engine.transport.chrome_cdp import ChromeCdpTransport  # noqa: E402

#: 探测用的会话（城市光污染文章，实测存在的 4 条之一）。
SID = "<session-id>"

#: 整个链路在页面上下文里跑：借页面自己的 fetch/WebSocket，
#: 自动带上 origin、cookie、UA —— 比在 Python 侧重放请求头更接近真实前端。
JS = r"""(async () => {
  const raw = localStorage.getItem('userToken') || '';
  let tok=''; try { const o=JSON.parse(raw); tok=(o&&o.value)?o.value:raw; } catch(e){ tok=raw; }
  const H = {'Authorization':'Bearer '+tok};
  const SID = '__SID__';

  // 1) 历史消息 -> 找到最后一条有正文的助手消息
  const hj = await (await fetch('/api/v0/chat/history_messages?chat_session_id='+SID,
      {headers:H, credentials:'include'})).json();
  const msgs = hj.data.biz_data.chat_messages;
  const asst = msgs.filter(m => m.role === 'ASSISTANT');
  const last = asst[asst.length-1];
  const text = (last.fragments||[]).filter(f=>f.type==='RESPONSE').map(f=>f.content||'').join('');
  if (!text) return JSON.stringify({step:'pick', ok:false, fragTypes:(last.fragments||[]).map(f=>f.type)});

  // 2) 取 TTS ticket（scope=tts，36 字符）
  const tj = await (await fetch('/api/v0/auth/ticket', {method:'POST', headers:{...H,'Content-Type':'application/json'},
      body: JSON.stringify({scope:'tts'}), credentials:'include'})).json();
  const ticket = tj.data.biz_data.ticket;

  // 3) 按 bundle 里 gY() 的真实顺序拼 query：
  //    streamParams 先，再 ticket / mode / format
  const qs = new URLSearchParams();
  qs.set('chat_session_id', SID);
  qs.set('message_id', String(last.message_id));
  qs.set('ticket', ticket);
  qs.set('mode', 'manual');
  qs.set('format', 'opus');
  const url = 'wss://' + location.host + '/api/v0/chat/tts/?' + qs.toString();

  // 4) 连流，只统计帧，不保存音频内容
  const acc = {binCount:0, binBytes:0, seqs:[], texts:[], end:null};
  await new Promise(resolve => {
    let ws, settled=false;
    const done = o => { if(settled) return; settled=true; try{ws&&ws.close()}catch(e){}; acc.end=o; resolve(); };
    try { ws = new WebSocket(url); } catch(e){ return done({err:'ctor:'+e.message}); }
    ws.binaryType='arraybuffer';
    ws.onmessage = ev => {
      if (typeof ev.data === 'string') {
        acc.texts.push(ev.data.slice(0,200));
        if (/"event"\s*:\s*"finish"/.test(ev.data)) done({reason:'finish'});
      } else {
        acc.binCount++; acc.binBytes += ev.data.byteLength;
        if (ev.data.byteLength>=4 && acc.seqs.length<5) acc.seqs.push(new DataView(ev.data).getUint32(0,false));
        if (acc.binCount >= 40) done({reason:'enough'});
      }
    };
    ws.onclose = e => done({reason:'close', code:e.code});
    ws.onerror = () => done({reason:'error'});
    setTimeout(() => done({reason:'timeout'}), 25000);
  });
  return JSON.stringify({
    step:'e2e', ok: acc.binCount>0,
    messageId: last.message_id, textLen: text.length, textHead: text.slice(0,60),
    binFrames: acc.binCount, binBytes: acc.binBytes, seqHead: acc.seqs,
    textFrames: acc.texts, wsEnd: acc.end,
    avgBytesPerFrame: acc.binCount ? Math.round(acc.binBytes/acc.binCount) : 0,
  });
})()"""


async def main() -> int:
    tok_path = ROOT / "engine" / ".token"
    if not tok_path.exists():
        print("[错误] 找不到 engine/.token")
        return 2
    token = tok_path.read_text(encoding="utf-8").strip()
    # 只打印长度与布尔值 —— 绝不打印 token 内容或前缀
    print(f"[凭据] .token 长度 {len(token)} · 非空={bool(token)}")

    t = ChromeCdpTransport(token_provider=lambda: token, offscreen=True)
    out: dict[str, object] = {}
    try:
        print("\n=== 启动真 Chrome（离屏）===")
        await t.start()
        cdp = t._cdp
        await cdp.navigate("https://chat.deepseek.com/")
        await asyncio.sleep(4)

        print("\n=== 取 ticket / 连 WS / 收帧 ===")
        r = await cdp.eval(JS.replace("__SID__", SID), await_promise=True, timeout=120)
        out["e2e"] = json.loads(r) if isinstance(r, str) else r
        print(json.dumps(out["e2e"], ensure_ascii=False, indent=1))
    finally:
        print("\n=== 收尾：关闭浏览器 ===")
        await t.stop()
        print("  已关闭")

    # 判定
    e2e = out.get("e2e") or {}
    if e2e.get("ok"):
        print("\n[通过] 收到 Opus 音频帧，服务端 TTS 可用")
        return 0
    frames = e2e.get("textFrames") or []
    if any("rate_limit" in f for f in frames):
        print("\n[警告] 协议正确，但账号当前被限流（code 5 rate_limit_reached）")
        return 3
    if any("no_content" in f for f in frames):
        print("\n[警告] 协议正确，但该消息无可朗读内容（code 6 no_content）")
        return 4
    print("\n[错误] 未收到音频帧，见上方 textFrames")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
