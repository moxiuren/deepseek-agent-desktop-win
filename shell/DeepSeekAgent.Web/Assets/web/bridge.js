'use strict';

/* ============================================================================
   通信端口
   ----------------------------------------------------------------------------
   界面代码（app.js）只认这一个对象，**不直接碰 window.chrome.webview**。
   于是同一份 HTML 有两种活法：
     · 在 WebView2 里      → 走真宿主（C# 的 WebBridge）
     · 双击用浏览器打开     → 走 mock.js 的假数据
   换宿主只换这一层，界面代码一行都不用动。

   协议（两个方向，都是 JSON）：
     C# → JS：{t:"ready"|"hist"|"item"|"chg"|"clear"|"log"|"status"|"convs"|"models"|"err"}
     JS → C#：{t:"hello"|"submit"|"cancel"|"newConv"|"switchConv"|"setChat"|"win"|...}
   ============================================================================ */

(function () {
  const wv = (window.chrome && window.chrome.webview) || null;
  const handlers = [];

  const Bridge = {
    /** 'webview2' 或 'mock'。界面可据此提示"这是演示数据"。 */
    kind: wv ? 'webview2' : 'mock',

    /** 界面 → 宿主 */
    send(msg) {
      if (wv) { wv.postMessage(msg); return; }
      if (window.MockBridge) window.MockBridge.handle(msg);
    },

    /**
     * 界面 → 宿主，**附带 File 对象本体**。拖拽与粘贴附件走这条。
     *
     * 为什么普通 send() 不够：它只能传 JSON 可序列化的东西，而浏览器的
     * File 对象不是 —— 安全模型只给内容、不给本地路径，序列化过去只剩个
     * 空壳（name / size / type），宿主拿不到「文件在哪」。
     *
     * 但 WebView2 不是纯浏览器，它嵌在宿主进程里，为此提供了专有通道：
     *   chrome.webview.postMessageWithAdditionalObjects(msg, [file])
     *     → C# WebMessageReceivedEventArgs.AdditionalObjects
     *     → CoreWebView2File.Path  ← 就是磁盘上的绝对路径
     * 官方文档对 CoreWebView2File.Path 的原话：
     *   "You can use this object to obtain the path of a File dropped on WebView2."
     *
     * 于是拖进来的文件**不需要**读内容、不需要 base64 穿过桥 —— 宿主直接
     * 拿路径，交给引擎去读。这与「附件」按钮（C# 弹 OpenFileDialog）殊途同归。
     *
     * 界面层（app.js）不直接碰 chrome.webview，仍然只认 Bridge —— 这条通道
     * 收敛在这里，和 send() 并列。
     *
     * **必须 try/catch，这不是防御性编程，是主路径**：
     * 拖进来的若是内存 Blob（截图、从网页直接拖的图），WebView2 会在
     * **同步调用里**抛 TypeError：
     *
     *     additional File object is not a file on the disk.
     *
     * 消息根本到不了宿主。不接住的话，异常会冒进事件监听器被浏览器吞掉，
     * 用户看到的就是「拖了完全没反应」—— 最糟的一种失败。
     *
     * 这条错误同时说明了 WebView2 的语义边界：**只有真实存在于磁盘的文件
     * 才能通过**。所以宿主收到的路径一定是磁盘上有的东西，不需要再怀疑。
     *
     * @returns {{ok: boolean, reason?: string}}
     *   ok=true  → 已交给宿主，等它回 {t:'files'} 或 {t:'err'}
     *   reason='unsupported' → 没有这条通道（浏览器预览 / mock）
     *   reason='not-on-disk' → WebView2 判定不是磁盘文件
     */
    sendFiles(msg, files) {
      if (!wv || typeof wv.postMessageWithAdditionalObjects !== 'function') {
        return { ok: false, reason: 'unsupported' };
      }
      try {
        wv.postMessageWithAdditionalObjects(msg, files);
        return { ok: true };
      } catch (e) {
        // 唯一已知的抛出原因就是「不是磁盘文件」。真出现别的错，
        // 也不该把异常漏出去 —— 上层只需要知道「没发成」。
        console.warn('[bridge] postMessageWithAdditionalObjects 失败：', e);
        return { ok: false, reason: 'not-on-disk', error: e };
      }
    },

    /** 订阅「宿主 → 界面」 */
    onMessage(fn) { handlers.push(fn); },

    /** 内部：把一条宿主消息分发给所有订阅者。单个订阅者抛错不影响其它。 */
    emit(msg) {
      for (const fn of handlers) {
        try { fn(msg); } catch (e) { console.error('[bridge] 订阅者抛错：', e); }
      }
    },
  };

  window.Bridge = Bridge;

  if (wv) {
    // 真宿主：C# 在 NavigationCompleted 之后主动发 {t:"ready"}，
    // 前端收到才回 hello 去要历史。这一步不能省 —— 少了它前端会一直停在
    // 「启动中…」，而且不报任何错（它在等一条永远不会来的消息）。
    wv.addEventListener('message', (ev) => Bridge.emit(ev.data));
  } else if (window.MockBridge) {
    window.MockBridge.start(Bridge);
  }
})();
