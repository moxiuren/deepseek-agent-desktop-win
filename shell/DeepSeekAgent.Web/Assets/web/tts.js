'use strict';

/* ============================================================================
   朗读（TTS）
   ----------------------------------------------------------------------------
   DeepSeek 网页版的「朗读」是**服务端流式 TTS**：先把消息正文合成 Opus 音频，
   边合成边通过 WebSocket 推下来，客户端边收边解边放。它**不是**浏览器的
   speechSynthesis（`main.js` 里 `speechSynthesis` 出现 0 次），也不是本地
   edge-tts —— 音色、语言、断句全在服务端。

   分工（这是整个设计的关键，别把音频搬到 C# 或 Python 侧）：

     引擎  → 签票（POST /api/v0/auth/ticket，scope=tts），只做控制面
     前端  → 拿票直连 wss://chat.deepseek.com/api/v0/chat/tts/，
             用 WebView2 自带的 WebCodecs 解 Opus，用 WebAudio 播

   为什么音频不走宿主：解 Opus 在 Python 侧要额外背 40MB 级解码库
   （PyAV / opuslib），而 Chromium 内核本来就带硬件加速解码器；更要紧的是
   **流控必须跟着播放进度走** —— ack 里的 `played_seq` 只有真正在放的那一端
   才知道，搬回宿主就得让宿主反过来问前端「放到哪了」，绕一圈没有收益。

   协议（逆向自 `web-tts.9ddcbab81d.js`，详见
   docs/20261018-朗读功能可行性方案.md）：

     下行  二进制帧 = 4 字节大端 seq + Opus 负载（24kHz 单声道）
          文本帧   = {"event":"ready","audio_id":…,"format":"opus"}
                     {"event":"finish","code":N,"msg":"…"}
     上行  {"event":"ack","received_seq":N,"played_seq":M}
           {"event":"abort","reason":"…"}
           {"event":"finish"}

   ack 是**流控不是心跳**：`received_seq`（网络收到哪）与 `played_seq`
   （喇叭放到哪）分开报，服务端才能区分「客户端网络慢」和「客户端播放慢」，
   分别调整推流节奏。只报一个的话，两种完全不同的卡顿会被当成同一种。
   ============================================================================ */

(function () {
  const send = (m) => window.Bridge.send(m);

  /** 服务端错误码 → 用户能看懂的一句话。
   *
   * 这张表来自 `web-tts` bundle 里的枚举，**不是猜的**。文案刻意区分
   * 「重试有用」（限流、服务异常）与「重试没用」（没正文、语言不支持）——
   * 不区分的话，用户会对一条空消息反复点朗读，每次都被同样拒绝。 */
  const CODES = {
    0: '',
    1: '服务端内部错误',
    2: '请求参数不对',
    3: '朗读服务暂时不可用',
    4: '朗读额度已用完',
    5: '朗读太频繁，请稍后再试',
    6: '这条消息没有可朗读的内容',
    7: '这条消息的语言不支持朗读',
    8: '当前音色不支持这条消息的语言',
    9: '内容被安全策略拦下了',
  };

  const WS_BASE = 'wss://chat.deepseek.com/api/v0/chat/tts/';

  /** 预缓冲：等够这么多毫秒的音频再开声。
   *
   * 不缓冲直接放，首帧一到就开声，几乎必然立刻欠载 —— 解码器还没追上，
   * 下一帧还没到，声音就断一下。实测这类「开声即卡」比晚 300ms 出声难听得多。 */
  const PREBUFFER_MS = 400;

  const state = {
    /** 'idle' | 'loading'（等票 / 等合成）| 'connecting' | 'playing' */
    phase: 'idle',
    /** 正在朗读的那条气泡的 seq（前端用它给按钮上「停止」态）。 */
    seq: null,
    messageId: '',
    voiceId: '',
    /** 诊断数字：欠载次数。只在控制台可见，不上界面。 */
    underruns: 0,
    /** 当前朗读后端：'deepseek'（签票 + 直连 WS）或 'fish'（宿主 HTTP）。
     *
     * 启动时向宿主问一次。**默认 'fish'** —— 与宿主侧 TtsConfig 的默认一致。
     * 两边默认值必须相同，否则「宿主还没回话」的那一瞬间会按错的分支走。 */
    engine: 'fish',
  };

  /** Fish 路径的播放节点。与 DeepSeek 路径的 `sources` 分开管理 ——
   * 两条路互斥，但停止时各自清各自的，避免「停 Fish 把 WS 的 source 也停了」。 */
  let fishSource = null;

  let ws = null;
  let decoder = null;
  let ctx = null;
  let gain = null;

  /** 已排进播放队列的帧，用于算 `played_seq`：{seq, endAt}（单位秒，ctx 时钟）。 */
  let scheduled = [];
  let nextStart = 0;
  let firstFrameAt = 0;      // performance.now()，用于预缓冲判定
  let startRequested = false;

  let lastReceived = -1;
  let lastPlayed = -1;
  let ackTimer = 0;

  /** 服务端推流结束（收到 finish code=0）。
   *
   * 以前这个标记挂在 `ws.__serverDone` 上，跟着连接对象走。但收尾判断
   * 需要同时看「解码是否排空」，而解码与连接是两件事，挂在 ws 上会让
   * 判断读起来像「连接结束了没」，掩盖真正的条件。 */
  let serverDone = false;

  /** 解码器已 flush 完成 —— 所有已入队的帧都走完了 output 回调。
   *
   * **没有这个标记就会丢音**：`decode()` 只是入队，真正出 PCM 是异步的。
   * 服务端推完 40 帧立刻发 finish 时，解码队列里还压着几十帧没吐，
   * 此时 `scheduled` 与 `pendingAudio` 都是空的 —— 只看这两个会误判
   * 「放完了」，直接 cleanup 并把 phase 打回 idle；随后迟到的解码输出
   * 全被 `onAudioData` 的 idle 早退丢掉，用户听到的是**一片安静**。 */
  let decodeFlushed = false;

  /** 这一轮的所有 source —— 停止时要一个个 stop()，否则已经排进去的还会继续响。 */
  const sources = new Set();

  let reqSeq = 0;
  const pending = new Map();   // req -> {kind, cb}

  const toast = (text) => {
    // app.js 的 onError 是「任务级错误」的呈现（对话流里一条红行）。
    // 朗读失败不该往流水里插行 —— 用户点的是气泡上的小按钮，
    // 报错就该出现在气泡附近。这里用轻量 toast，见下方 showToast。
    showToast(text);
  };

  // ---------- 与宿主的三条往返 ----------
  //
  // 都带 `req` 关联号：朗读按钮可以在多条气泡上先后被点，没有关联号
  // 就分不清回来的票据属于哪一次请求，A 气泡的票会挂到 B 气泡上 ——
  // 听起来像「念错了内容」，实际是串了线。

  // 三条命令各自的发法。**写成字面量而不是 `send({t: kind})`**：
  // `tools/e2e/verify_bridge_contract.py` 是静态扫描，变量路径它扫不到 ——
  // 于是拼错的命令名不会红，用户只会看到「点了没反应」。
  // 这个文件里唯一允许出现 `t` 字面量的地方就是这里。
  const SENDERS = {
    tts: (req, p) => send({ t: 'tts', req, messageId: p.messageId }),
    ttsVoices: (req) => send({ t: 'ttsVoices', req }),
    ttsVoice: (req, p) => send({ t: 'ttsVoice', req, id: p.id }),
    // Fish 直连的三条。同样写字面量 —— 理由见上方注释：
    // verify_bridge_contract.py 是静态扫描，变量路径它扫不到。
    ttsFish: (req, p) => send({ t: 'ttsFish', req, text: p.text, voice: p.voice || '' }),
    ttsFishVoices: (req) => send({ t: 'ttsFishVoices', req }),
    ttsFishVoice: (req, p) => send({ t: 'ttsFishVoice', req, id: p.id }),
    ttsEngine: (req) => send({ t: 'ttsEngine', req }),
    ttsSettings: (req) => send({ t: 'ttsSettings', req }),
    ttsSettingsSet: (req, p) => send({
      t: 'ttsSettingsSet', req,
      engine: p.engine || '', base: p.base || '', model: p.model || '',
      format: p.format || '', apiKey: p.apiKey || '',
    }),
  };

  function request(kind, payload) {
    return new Promise((resolve) => {
      const req = 'tts' + (++reqSeq);
      pending.set(req, resolve);
      SENDERS[kind](req, payload || {});
    });
  }

  // ---------- 对外 ----------

  const Tts = {
    /** 当前状态，界面用它决定按钮显示「朗读」还是「停止」。 */
    get phase() { return state.phase; },
    get seq() { return state.seq; },

    /** 宿主推回来的消息分发（app.js 的 handle 里转进来）。 */
    handle(m) {
      switch (m.t) {
        case 'tts':
        case 'ttsVoices':
        case 'ttsVoice':
        case 'ttsFish':
        case 'ttsFishVoices':
        case 'ttsFishVoice':
        case 'ttsEngine':
        case 'ttsSettings':
        case 'ttsSettingsSet': {
          const cb = pending.get(m.req);
          if (cb) { pending.delete(m.req); cb(m); }
          break;
        }
      }
    },

    /** 当前后端。界面据此决定音色面板走哪套数据。 */
    get engine() { return state.engine; },

    /** 启动时问一次宿主用哪个后端。失败就保持默认，不阻塞任何事。 */
    async refreshEngine() {
      try {
        const r = await request('ttsEngine', {});
        if (r && r.engine) state.engine = r.engine === 'deepseek' ? 'deepseek' : 'fish';
      } catch { /* 保持默认 */ }
      return state.engine;
    },

    /** 朗读一条消息。同一条再点一次 = 停止（按钮语义是切换）。
     *
     * `text` 是**消息原文**。DeepSeek 那条路只用 messageId（服务端自己
     * 知道内容），Fish 这条路必须把正文发过去 —— 它是无状态合成。
     * 调用方（app.js）两条都给，本函数按后端取用。 */
    async play(messageId, seq, text) {
      if (!messageId && !text) return;
      if (state.phase !== 'idle' && state.seq === seq) { Tts.stop(); return; }
      if (state.phase !== 'idle') Tts.stop();

      state.phase = 'loading';
      state.seq = seq;
      state.messageId = messageId;
      notify();

      if (state.engine === 'fish') return playFish(text, seq);

      if (!messageId) { fail('这条消息没有服务端 id，无法朗读'); return; }
      let res;
      try {
        res = await request('tts', { messageId });
      } catch (e) {
        fail('朗读请求失败：' + (e && e.message ? e.message : e));
        return;
      }
      // 等待期间用户可能已经点了停止 / 换了另一条
      if (state.messageId !== messageId || state.phase !== 'loading') return;

      if (res.error || !res.ticket) {
        fail(res.error || '没能拿到朗读票据');
        return;
      }
      openStream(res.sid, res.messageId || messageId, res.ticket);
    },

    stop,

    /** 音色列表。按后端分流 —— 两条路的音色体系完全无关。 */
    voices() {
      return state.engine === 'fish' ? request('ttsFishVoices', {})
                                     : request('ttsVoices', {});
    },

    /** 切换音色。 */
    setVoice(id) {
      return state.engine === 'fish' ? request('ttsFishVoice', { id })
                                     : request('ttsVoice', { id });
    },

    /** 朗读配置（设置面板用）。key 只回 hasKey，不回明文。 */
    settings() { return request('ttsSettings', {}); },
    saveSettings(p) { return request('ttsSettingsSet', p || {}); },
  };

  // ---------- Fish 路径 ----------
  //
  // 与 DeepSeek 那条的本质区别：Fish 是**一次性 HTTP**，宿主把整段音频
  // 合成完才回来。没有 WS、没有 seq、没有 ack 流控 —— 所以这里不需要
  // 预缓冲，也不需要 ack 定时器。代价是首声延迟 = 合成时间（实测 1~2 秒），
  // 而 DeepSeek 是边合成边推，首声更快。
  //
  // 这个取舍是有意的：Fish 走的是独立免费额度，不消耗 DeepSeek 的朗读
  // 配额，也就不参与封号风控。慢一点换「不碰主账号」，值得。

  async function playFish(text, seq) {
    const body = String(text || '');
    if (!body.trim()) { fail('这条消息没有可朗读的内容'); return; }

    let res;
    try {
      res = await request('ttsFish', { text: body });
    } catch (e) {
      fail('朗读请求失败：' + (e && e.message ? e.message : e));
      return;
    }
    // 等待期间用户可能已经停止 / 换了另一条
    if (state.seq !== seq || state.phase !== 'loading') return;

    if (res.error || !res.audio) {
      fail(res.error || '没能拿到朗读音频');
      return;
    }

    let bytes;
    try {
      bytes = base64ToBytes(res.audio);
    } catch (e) {
      fail('音频数据损坏：' + (e && e.message ? e.message : e));
      return;
    }

    const c = ensureContext();
    if (c.state === 'suspended') c.resume().catch(() => {});

    let buf;
    try {
      // `decodeAudioData` 能直接吃 mp3/wav 的完整文件 —— 这是 Fish 与
      // DeepSeek 的关键差异：那边是裸 Opus 帧，必须 WebCodecs 手动解；
      // 这边是带容器头的完整音频，WebAudio 自带解码器就能处理。
      // 用 slice 而不是直接传 bytes.buffer：ArrayBuffer 可能被复用，
      // 且 decodeAudioData 会**转移**（detach）传入的 buffer。
      buf = await c.decodeAudioData(bytes.buffer.slice(
        bytes.byteOffset, bytes.byteOffset + bytes.byteLength));
    } catch (e) {
      fail('音频解码失败：' + (e && e.message ? e.message : e));
      return;
    }

    // 解码是异步的，期间可能已被停止
    if (state.seq !== seq || state.phase !== 'loading') return;

    try {
      fishSource = c.createBufferSource();
      fishSource.buffer = buf;
      fishSource.connect(gain || c.destination);
      fishSource.onended = () => {
        fishSource = null;
        // **只在这条还是当前那条时才收尾** —— 用户连点两条时，
        // 前一条的 onended 会晚于新一条的 play 到达，无条件收尾会把
        // 新那条的状态也打回 idle，按钮显示「朗读」而声音还在放。
        if (state.seq === seq && state.phase === 'playing') finishUp();
      };
      state.phase = 'playing';
      notify();
      fishSource.start(0);
    } catch (e) {
      fail('播放失败：' + (e && e.message ? e.message : e));
    }
  }

  /** base64 → Uint8Array。atob 返回的是「每个字符一个字节」的二进制串，
   * 必须逐字符取 charCodeAt，不能当 UTF-8 文本处理。 */
  function base64ToBytes(b64) {
    const bin = atob(b64);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }

  window.Tts = Tts;

  // ---------- 连接与解码 ----------

  function openStream(sid, messageId, ticket) {
    state.phase = 'connecting';
    notify();

    // URL 参数顺序照着网页版 `gY()` 拼：会话参数在前，票/模式/格式在后。
    // **顺序不是随便的** —— 续播时 `audio_id` / `received_seq` / `played_seq`
    // 追加在末尾，服务端按位置做兼容解析；把 ticket 挪到前面会让续播参数
    // 落到它不认识的位置上。
    const q = new URLSearchParams();
    q.set('chat_session_id', sid || '');
    q.set('message_id', String(messageId));
    q.set('ticket', ticket);
    q.set('mode', 'manual');
    q.set('format', 'opus');

    try {
      ws = new WebSocket(WS_BASE + '?' + q.toString());
    } catch (e) {
      fail('连接朗读服务失败：' + e.message);
      return;
    }
    ws.binaryType = 'arraybuffer';

    ws.onopen = () => {
      state.phase = 'playing';
      notify();
    };

    ws.onmessage = (ev) => {
      if (typeof ev.data === 'string') { onTextFrame(ev.data); return; }
      onBinaryFrame(ev.data);
    };

    ws.onerror = () => {
      // onerror 之后必然跟 onclose，错误提示统一在 onclose 里给，
      // 免得同一件事弹两次。
    };

    ws.onclose = () => {
      // 正常收尾（收到 finish / 用户停止）时 phase 已经置回 idle，
      // 这里只在「异常断开」时补一句。
      if (state.phase === 'playing' || state.phase === 'connecting') {
        fail('朗读连接中断');
      }
    };
  }

  function onTextFrame(text) {
    let msg;
    try { msg = JSON.parse(text); } catch { return; }
    if (msg.event === 'finish') {
      const code = Number(msg.code || 0);
      if (code !== 0) {
        // 服务端明确拒绝（限流 / 没正文 / 语言不支持）。
        // 这时可能已经放了一部分，也可能一帧都没来 —— 两种都要收干净。
        fail(CODES[code] || ('朗读失败（code ' + code + '）' + (msg.msg ? '：' + msg.msg : '')));
        return;
      }
      // code=0 = 「推流结束」。但**推流结束 ≠ 播完了**，中间还隔着两层：
      //   ① 解码器队列里可能压着几十帧还没吐出 PCM（decode 是异步的）
      //   ② 播放队列里可能还有几百毫秒没放完
      // 所以这里只标位 + 让解码器把队列排空，收尾交给 maybeFinish()。
      serverDone = true;
      drainDecoder();
    } else if (msg.event === 'ready') {
      // 服务端确认建流成功。这里不做额外动作：真正的开始信号是第一帧音频。
    }
  }

  function onBinaryFrame(buf) {
    if (!(buf instanceof ArrayBuffer) || buf.byteLength <= 4) return;

    // 帧格式：4 字节**大端** seq + Opus 负载。
    // 大端不是随便定的 —— `new DataView(buf).getUint32(0, false)` 里那个
    // false 就是「不要小端」。写反了 seq 会变成天文数字，ack 报上去
    // 服务端会认为客户端丢了几十亿帧，直接断流。
    const seq = new DataView(buf).getUint32(0, false);
    const payload = new Uint8Array(buf, 4);

    if (lastReceived < 0) firstFrameAt = performance.now();
    lastReceived = seq;

    ensureDecoder();
    if (!decoder) return;

    try {
      decoder.decode(new EncodedAudioChunk({
        type: 'key',            // Opus 每帧都可独立解码，没有「帧间依赖」
        timestamp: seq * 20000, // 微秒。只用于输出排序，实际时长以 AudioData.duration 为准
        duration: 20000,
        data: payload,
      }));
    } catch (e) {
      console.warn('[tts] 解码入队失败：', e);
    }

    scheduleAck();
  }

  function ensureDecoder() {
    if (decoder) return;
    if (typeof window.AudioDecoder !== 'function') {
      fail('当前内核不支持 WebCodecs，无法朗读（需要 WebView2 109+）');
      return;
    }
    try {
      // 采样率与声道数必须和上游一致（24kHz 单声道）。
      // Opus 不需要 `description`（那是容器里的 OpusHead；裸流用默认值）。
      decoder = new AudioDecoder({
        output: onAudioData,
        error: (e) => {
          console.warn('[tts] 解码器错误：', e);
          fail('音频解码失败：' + (e && e.message ? e.message : e));
        },
      });
      decoder.configure({ codec: 'opus', sampleRate: 24000, numberOfChannels: 1 });
    } catch (e) {
      decoder = null;
      fail('音频解码器初始化失败：' + e.message);
    }
  }

  function ensureContext() {
    if (ctx) return ctx;
    try {
      // 直接用 24kHz 建上下文，省一次重采样。设备不支持时浏览器会自己
      // 内部重采样，不会抛 —— 这里的 catch 只是兜极端情况。
      ctx = new AudioContext({ sampleRate: 24000 });
    } catch {
      ctx = new AudioContext();
    }
    gain = ctx.createGain();
    gain.connect(ctx.destination);
    return ctx;
  }

  function onAudioData(data) {
    try {
      if (state.phase === 'idle') return;   // 已经停了，迟到的帧丢掉

      const c = ensureContext();
      // 自动播放策略：点击朗读就是用户手势，但上下文仍可能是 suspended
      // （WebView2 里偶发）。resume 是幂等的，多调无妨。
      if (c.state === 'suspended') c.resume().catch(() => {});

      const frames = data.numberOfFrames;
      const rate = data.sampleRate;
      const dur = (data.duration != null ? data.duration : (frames / rate) * 1e6) / 1e6;

      const buf = c.createBuffer(1, frames, rate);
      data.copyTo(buf.getChannelData(0), { planeIndex: 0, format: 'f32-planar' });

      // 这一块音频对应哪一帧：入队时我们把 seq 编码进了 timestamp（seq*20000），
      // 在这里还原出来。用它算 `played_seq` 才是准的 —— 用「当前收到的最新帧」
      // 顶替会让 ack 虚报进度，服务端据此以为客户端放得比实际快。
      const seqOf = Math.round((data.timestamp || 0) / 20000);

      // 预缓冲：第一帧到达后先攒够 PREBUFFER_MS 再开声。
      // 攒的时候音频已经在解码、在内存里，不会丢。
      //
      // `serverDone` 时不再等 —— 服务端已经推完了，不会再有新帧来填这个
      // 缓冲窗口，继续等只是白让用户多等几百毫秒。短消息（一句「好的」）
      // 走的就是这条路。
      if (!startRequested) {
        if (!serverDone && performance.now() - firstFrameAt < PREBUFFER_MS) {
          pendingAudio.push({ buf, dur, seq: seqOf });
          return;
        }
        startRequested = true;
        flushPendingAudio(c);
        enqueue(c, buf, dur, seqOf);
        return;
      }

      enqueue(c, buf, dur, seqOf);
    } finally {
      // AudioData 是**必须手动 close 的** —— 它持有解码器内部缓冲，
      // 不关的话每帧泄漏一块原生内存，几分钟的朗读就能把进程拖垮。
      data.close();
    }
  }

  const pendingAudio = [];

  function flushPendingAudio(c) {
    for (const p of pendingAudio) enqueue(c, p.buf, p.dur, p.seq);
    pendingAudio.length = 0;
  }

  /** 把解码器里还没吐出来的帧全部排空。
   *
   * `flush()` 的语义是「处理完队列里所有已入队的帧，然后 resolve」——
   * 它返回时，这一批帧的 output 回调都已经跑过了。这正是收尾判断需要
   * 的前置条件：只有解码排空，`scheduled` / `pendingAudio` 才是可信的。
   *
   * 不 flush 直接判空会漏音（见 `decodeFlushed` 的说明）。
   * 解码器已经关掉（用户中途停止）时 flush 会抛，这里吞掉即可。 */
  function drainDecoder() {
    if (!decoder || decoder.state === 'closed') { decodeFlushed = true; maybeFinish(); return; }
    try {
      decoder.flush()
        .then(() => { decodeFlushed = true; maybeFinish(); })
        .catch(() => { decodeFlushed = true; maybeFinish(); });
    } catch {
      decodeFlushed = true;
      maybeFinish();
    }
  }

  /** 三个条件全满足才算这一条读完了。
   *
   *   ① 服务端推流结束（finish code=0）
   *   ② 解码器排空（所有帧都已变成 AudioData）
   *   ③ 播放队列放空（`scheduled` 空，且没有在等预缓冲的）
   *
   * 少任何一条就收尾，都会丢音或提前掐断。 */
  function maybeFinish() {
    if (!serverDone || !decodeFlushed) return;
    if (scheduled.length || pendingAudio.length) return;
    finishUp();
  }

  function enqueue(c, buf, dur, seqOf) {
    const src = c.createBufferSource();
    src.buffer = buf;
    src.connect(gain);

    const now = c.currentTime;
    // 欠载：上一帧早该放完了，下一帧才到。把时间轴拉回「现在」，
    // 而不是继续按原计划排 —— 否则欠载会累积成越来越大的延迟，
    // 用户听到的是「声音比画面慢好几秒」。
    if (nextStart < now) {
      if (nextStart > 0) state.underruns++;
      nextStart = now + 0.02;
    }
    if (nextStart === 0) nextStart = now + 0.02;

    const startAt = nextStart;
    nextStart += dur;

    src.start(startAt);
    sources.add(src);
    src.onended = () => { sources.delete(src); };

    scheduled.push({ seq: seqOf, endAt: startAt + dur });
    scheduleAck();
  }

  // ---------- ack 流控 ----------

  function scheduleAck() {
    if (ackTimer) return;
    // 100ms 一次。网页版实测节拍在 80–120ms 之间 —— 更快没有收益
    // （服务端按帧批量调度），更慢会让它以为客户端卡了而放慢推流。
    ackTimer = setTimeout(sendAck, 100);
  }

  function sendAck() {
    ackTimer = 0;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;

    const c = ctx;
    const now = c ? c.currentTime : 0;
    // 播放进度：把「已经放完的」从队头摘掉，最后一个摘掉的 seq 就是 played。
    while (scheduled.length && scheduled[0].endAt <= now) {
      lastPlayed = scheduled.shift().seq;
    }

    try {
      ws.send(JSON.stringify({
        event: 'ack',
        received_seq: lastReceived,
        played_seq: lastPlayed,
      }));
    } catch { /* 连接已在关闭途中 */ }

    // 推流已结束、解码已排空、队列也放空 → 这一条读完了
    if (serverDone && decodeFlushed && !scheduled.length && !pendingAudio.length) {
      finishUp();
      return;
    }

    // **自我续期**。不续的话会有个很隐蔽的卡死：
    // 定时器靠「有新帧进来」重启（enqueue / onBinaryFrame 里都调 scheduleAck），
    // 但最后一帧放完时，恰恰不会再有任何新帧 —— 于是 sendAck 不再被调用，
    // 状态永久停在 playing、按钮永久显示「停止」。
    // 只在还有东西要等的时候续期，收尾后自然停下。
    if (scheduled.length || pendingAudio.length || serverDone) scheduleAck();
  }

  // ---------- 收尾 ----------

  function finishUp() {
    cleanup();
    state.phase = 'idle';
    state.seq = null;
    state.messageId = '';
    notify();
  }

  function fail(message) {
    cleanup();
    state.phase = 'idle';
    state.seq = null;
    state.messageId = '';
    notify();
    toast(message);
  }

  function stop() {
    if (ws && ws.readyState === WebSocket.OPEN) {
      try { ws.send(JSON.stringify({ event: 'abort', reason: 'user' })); } catch { /* ignore */ }
    }
    cleanup();
    state.phase = 'idle';
    state.seq = null;
    state.messageId = '';
    notify();
  }

  function cleanup() {
    if (ackTimer) { clearTimeout(ackTimer); ackTimer = 0; }

    // Fish 的播放节点：单独停，且**先摘 onended 再停** ——
    // stop() 会触发 onended，那条回调会把状态再打回 idle 一次，
    // 而此时 stop() 自己已经在收尾了，重复收尾会让按钮闪一下。
    if (fishSource) {
      const s = fishSource;
      fishSource = null;
      s.onended = null;
      try { s.stop(); } catch { /* 没在播的会抛 InvalidStateError */ }
      try { s.disconnect(); } catch { /* ignore */ }
    }

    if (ws) {
      // 先摘回调再关 —— 否则 close 会触发 onclose 里的「连接中断」，
      // 用户主动停止时弹一条「中断」提示是误导。
      ws.onmessage = null;
      ws.onclose = null;
      ws.onerror = null;
      ws.onopen = null;
      try { ws.close(); } catch { /* ignore */ }
      ws = null;
    }

    if (decoder) {
      try { decoder.close(); } catch { /* ignore */ }
      decoder = null;
    }

    for (const s of sources) {
      try { s.stop(); } catch { /* 已经停过的会抛 InvalidStateError */ }
    }
    sources.clear();

    // AudioContext 不关 —— 关掉再建要重新走一次音频设备初始化（实测 100ms+），
    // 而用户连读几条是常态。留着它空闲时不耗电。
    if (ctx && ctx.state === 'running') {
      // 让已经排进硬件队列的声音立刻静音：把 gain 归零再恢复，
      // 比 stop() 每个 source 更快生效（source.stop 只影响还没播的）。
      try {
        gain.gain.cancelScheduledValues(ctx.currentTime);
        gain.gain.setValueAtTime(0, ctx.currentTime);
        gain.gain.setValueAtTime(1, ctx.currentTime + 0.02);
      } catch { /* ignore */ }
    }

    scheduled = [];
    pendingAudio.length = 0;
    nextStart = 0;
    firstFrameAt = 0;
    startRequested = false;
    serverDone = false;
    decodeFlushed = false;
    lastReceived = -1;
    lastPlayed = -1;
  }

  // ---------- 界面通知 ----------

  /** 状态变了就通知界面重画朗读按钮。
   *
   * 为什么不在这里直接改 DOM：tts.js 不知道气泡节点长什么样（那是 app.js
   * 的职责）。它只广播「状态变了」，由 app.js 决定画成什么样 —— 与
   * Bridge 的分层一致：谁拥有视图，谁负责渲染。 */
  function notify() {
    try {
      window.dispatchEvent(new CustomEvent('tts-state', { detail: { phase: state.phase, seq: state.seq } }));
    } catch { /* 老内核没有 CustomEvent 构造器的情况不存在，兜一下 */ }
  }

  /** 轻量 toast。app.js 里已有一套错误呈现（对话流里的红行），
   * 但朗读失败属于「这条气泡上的操作没成功」，不该污染流水。 */
  function showToast(text) {
    if (window.AppToast) { window.AppToast(text); return; }
    console.warn('[tts]', text);
  }
})();
