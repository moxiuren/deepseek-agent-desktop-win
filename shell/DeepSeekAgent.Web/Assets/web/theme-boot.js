'use strict';

/* ============================================================================
   主题首屏引导
   ----------------------------------------------------------------------------
   这个文件**必须**在 <head> 里同步加载（不能 defer / async），且必须在
   app.css 之后、<body> 之前。它只做一件事：在第一次绘制前把 data-theme
   写到 <html> 上。

   为什么值得单开一个文件
   ----------------------
   主题如果等到 app.js 才应用，用户会看到一帧浅色再跳成深色 —— 深色用户
   每次开窗都被闪一下。要避免，就必须在**第一次 paint 之前**定下主题。

   而 CSP 是 `script-src 'self'`（见 index.html 的 meta），不允许内联
   `<script>`。所以不能把这几行塞进 HTML 的 <head> 里 —— 只能作为同源外部
   文件，用同步 script 标签加载。同步 script 会阻塞 HTML 解析，正好保证
   「样式表已解析、body 还没渲染」这个时间窗。

   为什么是 localStorage 而不是让宿主存
   ------------------------------------
   Web 壳**没有**链接 UiPrefsStore.cs（那是 WPF 壳的），宿主侧目前也没有
   任何主题相关的存储与消息。而这里的诉求是「第一个 paint 前读到值」——
   走 Bridge 问宿主是异步的，赶不上。localStorage 是同步的，且 WebView2
   会把它持久化到 userDataFolder 里，和窗口状态一样跟着这台机器走。

   这与「界面偏好不该进引擎 config」是同一条理由：主题只跟这台机器有关，
   跟账号无关。
   ============================================================================ */

(function () {
  var KEY = 'dsa.theme';        // 'light' | 'dark' | 不存在（= 跟随系统）
  var root = document.documentElement;

  /** 把偏好解析成具体主题。认不出来的值一律当「跟随系统」。 */
  function resolve(pref) {
    if (pref === 'light' || pref === 'dark') return pref;
    try {
      return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches
        ? 'dark' : 'light';
    } catch (e) {
      return 'light';
    }
  }

  var pref = null;
  try { pref = localStorage.getItem(KEY); } catch (e) { /* 隐私模式 / 存储被禁 */ }

  root.dataset.theme = resolve(pref);

  // 暴露给 app.js：它需要知道「用户手动选过没有」，才能在系统主题变化时
  // 决定要不要跟着变。这里不用模块，直接挂一个只读的小对象。
  window.ThemeBoot = {
    KEY: KEY,
    /** 用户手动选过的值；null 表示跟随系统。 */
    pref: function () { return pref; },
    resolve: resolve,
    /** app.js 点按钮时调用：写偏好 + 立刻落到 DOM。 */
    set: function (next) {
      pref = (next === 'light' || next === 'dark') ? next : null;
      try {
        if (pref) localStorage.setItem(KEY, pref);
        else localStorage.removeItem(KEY);
      } catch (e) { /* 存不下就只在本次会话生效，不影响功能 */ }
      root.dataset.theme = resolve(pref);
    },
  };
})();
