'use strict';

/* ============================================================================
   Mock 宿主 —— 只在没有 WebView2 宿主时启用
   ----------------------------------------------------------------------------
   把「假数据」收成产品代码的一部分：双击 index.html 就能看到完整界面，
   调样式不用起引擎、不用编译 C#。在 WebView2 里这段代码会被加载但不执行
   （bridge.js 只有在拿不到 window.chrome.webview 时才调 start）。

   场景用 URL 指定：index.html?scene=running
     normal  默认 · 一轮跑完（思考块 + 两个工具 + 正文 + 代码块 + 闸门）
     running 有工具在跑（任务列表 + 实时输出 + 取消按钮）
     long    超长正文与超长工具输出（测换行与滚动）
     bulk    600 条消息（测渲染性能与滚动）
     breaker 熔断中（测健康胶囊的坏状态配色）
     nologin 凭据失效（测页脚琥珀态 + 身份面板的未登录分支）
     needlogin 宿主主动要求弹身份面板（AgentHost.IdentityNeedsLogin 那条路）
   ============================================================================ */

(function () {
  const q = new URLSearchParams(location.search);
  const SCENE = q.get('scene') || location.hash.replace('#', '') || 'normal';

  let seq = 0;
  const it = (kind, o) => Object.assign({ seq: ++seq, kind, text: '', detail: '' }, o);

  const TOOL_OK = { running: false, elapsedMs: 120, exitCode: 0, failed: false };

  /* ------------------------------ 场景数据 ------------------------------ */

  function sceneNormal() {
    const items = [
      it('TurnDivider', { text: '第 12 轮' }),
      it('Goal', { text: '帮我看看这个项目的依赖和入口，然后跑一次自检。' }),
      it('Thought', {
        text: '先确认外壳项目的 csproj，看有没有第三方依赖；再确认引擎入口和 venv 位置。\n' +
              '如果 csproj 里一个 PackageReference 都没有，那「零第三方依赖」这句话就是可验证的，不是自称的。',
        thought: { streaming: false, chars: 96, waited: 0, header: '思考过程（96 字）· 点这里展开' },
      }),
      it('ToolCall', {
        tool: Object.assign({
          kind: 'read_file', label: '读取文件', subject: 'DeepSeekAgent.Desktop.csproj',
          note: '', output: '<Project Sdk="Microsoft.NET.Sdk">\n  <PropertyGroup>\n' +
            '    <TargetFramework>net8.0-windows</TargetFramework>\n    <UseWPF>true</UseWPF>\n' +
            '  </PropertyGroup>\n</Project>',
        }, TOOL_OK),
      }),
      it('ToolCall', {
        tool: Object.assign({
          kind: 'local_cmd', label: '执行命令', subject: '.\\DeepSeekAgent.Desktop.exe --selfcheck',
          note: '', output: '[通过] 窗口构造 · 0 异常\n[通过] 可复制断言 · 18 块全可选中\n' +
            '[通过] 工具卡片三态 · 运行中 / 完成 / 失败',
        }, TOOL_OK),
      }),
      it('Assistant', {
        text: '已经成功连接到本地的 **deepseek agent desktop** 项目。整体结构是「引擎 + 外壳」两层，依赖很干净：\n\n' +
              '外壳 `shell/DeepSeekAgent.Desktop` —— WPF，零第三方依赖\n' +
              '引擎 `engine/gad_engine` —— Python，stdio JSON-RPC\n\n' +
              '这个项目的自检有四道门禁，任一非 0 即为失败：\n\n' +
              '```powershell\n# 四道门禁，任一非 0 即为失败\n' +
              '.\\DeepSeekAgent.Desktop.exe --selfcheck\n' +
              '.\\DeepSeekAgent.Desktop.exe --layout-audit\n' +
              '.\\DeepSeekAgent.Desktop.exe --perfcheck\n```\n\n' +
              '外壳负责进程生命周期与 `JSON-RPC` 编解码，渲染层只认 `TranscriptItem`，两者边界很干净。',
      }),
      it('Gate', { text: '闸门放行 · 等待 320ms' }),
    ];
    return { items, running: false };
  }

  function sceneRunning() {
    const s = sceneNormal();
    s.running = true;
    // 把最后一个工具改成运行中
    const last = s.items.filter((x) => x.kind === 'ToolCall').pop();
    last.tool.running = true;
    last.tool.elapsedMs = 2400;
    last.tool.output = '[通过] 窗口构造 · 0 异常\n[通过] 可复制断言 · 18 块全可选中\n[运行] layout-audit';
    s.items = s.items.filter((x) => x.kind !== 'Gate');
    return s;
  }

  function sceneLong() {
    const para = '这一段是刻意拉长的正文，用来验证换行、滚动条与高度估算是否稳。' +
      '引擎回来的正文是纯文本，前端按空行切段、按反引号切行内代码与代码块。'.repeat(3);
    const lines = [];
    for (let i = 1; i <= 60; i++) lines.push(`[${String(i).padStart(3, '0')}] 输出行 · ${'x'.repeat(40)}`);
    return {
      running: false,
      items: [
        it('Goal', { text: '给我一段超长输出，测一下滚动。' }),
        it('ToolCall', {
          tool: Object.assign({
            kind: 'local_cmd', label: '执行命令', subject: '.\run-long.ps1', note: '',
            output: lines.join('\n'),
          }, TOOL_OK),
        }),
        it('Assistant', { text: Array(6).fill(para).join('\n\n') }),
        it('Notice', { text: '这是一条长提示：' + '内容'.repeat(80) }),
      ],
    };
  }

  function sceneBulk() {
    const items = [];
    for (let i = 0; i < 600; i++) {
      const m = i % 5;
      if (m === 0) items.push(it('Goal', { text: `第 ${i / 5 + 1} 轮：读一下 ${i} 号文件。` }));
      else if (m === 1) items.push(it('Assistant', { text: `第 ${i} 条的回复正文，长度中等，用来压测首屏插入与滚动。` }));
      else if (m === 2) items.push(it('ToolCall', {
        tool: Object.assign({ kind: 'read_file', label: '读取文件', subject: `src/file-${i}.cs`, note: '', output: `// ${i}\nclass C${i} {}\n` }, TOOL_OK),
      }));
      else if (m === 3) items.push(it('Thought', {
        text: `第 ${i} 条思考内容。`, thought: { streaming: false, chars: 12, waited: 0, header: '思考过程（12 字）· 点这里展开' },
      }));
      else items.push(it('Info', { text: `第 ${i} 条系统信息` }));
    }
    return { items, running: false };
  }

  const SCENES = {
    normal: sceneNormal, running: sceneRunning, long: sceneLong,
    bulk: sceneBulk, breaker: sceneNormal,
    // 凭据失效（当前真实状态就是它）→ 页脚转琥珀、面板显示未登录
    nologin: sceneNormal,
    // 宿主主动要求弹面板（AgentHost.IdentityNeedsLogin 那条路）
    needlogin: sceneNormal,
    // 登录进行到一半，会话没了（引擎回 no_session）
    loginlost: sceneNormal,
  };

  /* ------------------------------ 身份（假数据） ------------------------------
     真宿主那边这套流程要起浏览器、联网验证，无宿主环境下验不了。这里把
     「等待登录 → 读到登录态 → 取走凭据」整条链模拟出来，让面板的每一段
     文案都有机会真的出现过（一上来就 ready 的话，settling / 等待那两段
     永远看不到，等于没验）。 */
  const NO_TOKEN = SCENE === 'nologin' || SCENE === 'needlogin';

  const ID = {
    hasToken: !NO_TOKEN,
    label: NO_TOKEN ? '' : 'shaomo17',
    impersonate: 'chrome150',
    // 照实写**完整绝对路径** —— 引擎回的就是这个。写成短名会让
    // 「路径太长把面板撑出滚动条」这个真机问题在 mock 里测不出来。
    tokenFile: 'C:\\Users\\Admin\\Documents\\Projects\\deepseek-agent-desktop\\engine\\.token',
    polls: 0,
    // 引擎 identity.status 里的 login_active —— 面板靠它纠正「已有会话在跑」
    loginActive: false,
  };

  const idStateMsg = () => ({
    t: 'idstate',
    s: {
      hasToken: ID.hasToken,
      label: ID.label,
      impersonate: ID.impersonate,
      tokenFile: ID.tokenFile,
      loginActive: ID.loginActive,
    },
  });

  function status(over) {
    return Object.assign({
      started: true, mode: '思考模式', transport: 'deepseek-rpc', transportReady: true,
      circuitLevel: 'closed', circuitReason: '正常', lastSignal: '', degrade: 'none',
      hasToken: ID.hasToken, identityLabel: ID.label, identityRefreshing: false, model: 'default',
      gateAllowed: true, gateReason: '', gateWaitMs: 0, busyStreak: 0,
      sessionCid: 'demo', sessionTurns: 378, sessionMaxTurns: 600, sessionTemporary: false,
      convTitle: '读取本地 deepseek agent desktop', convTurns: 378,
      memoryChars: 0, memoryEnabled: true,
      thinking: true, search: false, showThinking: true, thinkingMode: '思考模式',
    }, over);
  }

  const CONVS = [
    { id: 'a', title: '读取本地 deepseek agent desktop', active: true, turns: 378, preview: '', subtitle: '21:00 · 378 轮' },
    { id: 'b', title: '你是什么模型', active: false, turns: 2, preview: '', subtitle: '19:51 · 2 轮' },
    { id: 'c', title: '连接 webcodex 目录', active: false, turns: 16, preview: '', subtitle: '19:24 · 16 轮' },
    { id: 'd', title: '检查 Git 状态', active: false, turns: 6, preview: '', subtitle: '18:47 · 6 轮' },
  ];

  /** 网页版会话（要联网才有，所以只在用户点「网页版」时才"读"出来）。 */
  // `localId` 是接管后落地的本地对话 id —— 与真宿主推的字段一致
  // （WebBridge.SendRemoteSessions）。缺了它，浏览器预览里
  // 「重命名 / 同步」会被判成未接管而禁用，看不出这两项其实能用。
  const REMOTES = [
    { id: 'r-8831', localId: 'c-0001', title: '帮我看下这段报错', active: false, bound: true, turns: 0, preview: '', subtitle: '已接管 · 今天 20:12' },
    { id: 'r-8790', localId: '', title: '周末去哪玩', active: false, bound: false, turns: 0, preview: '', subtitle: '未接管 · 昨天 22:40' },
    { id: 'r-8755', localId: '', title: 'Pandas 分组聚合怎么写', active: false, bound: false, turns: 0, preview: '', subtitle: '未接管 · 10-14 09:03' },
  ];

  const convsMsg = (scope) => ({
    t: 'convs',
    scope,
    items: (scope === 'remote' ? REMOTES : CONVS).map((c) => Object.assign({ kind: scope }, c)),
  });

  const LOGS = [
    { seq: 1, at: '21:00:01', level: 'Info', text: '引擎已启动 · python 3.13.12' },
    { seq: 2, at: '21:00:02', level: 'Info', text: 'JSON-RPC 握手完成' },
    { seq: 3, at: '21:00:04', level: 'Warn', text: '目标档位 600 轮，当前 378 轮' },
  ];

  /* ------------------------------ 生命周期 ------------------------------ */

  let B = null;

  function fire(msg) { B.emit(msg); }

  function start(bridge) {
    B = bridge;
    // 复刻真宿主：WebView2 就绪后主动发 ready，前端收到才回 hello。
    setTimeout(() => fire({ t: 'ready' }), 20);
  }

  function handle(msg) {
    if (!msg || typeof msg !== 'object') return;
    switch (msg.t) {
      case 'hello': {
        const scene = (SCENES[SCENE] || sceneNormal)();
        const st = SCENE === 'breaker'
          ? status({ circuitLevel: 'open', circuitReason: '连续 3 次认证失败', gateAllowed: false, gateReason: 'L4 硬拒绝' })
          : status();
        setTimeout(() => {
          fire({ t: 'hist', items: scene.items, logs: LOGS, running: scene.running, s: st });
          fire(convsMsg('remote'));
          fire({ t: 'models', ready: true, current: 'default', items: [
            { name: 'default', display: 'DeepSeek（默认）', desc: '', available: true },
            { name: 'deepseek-reasoner', display: 'DeepSeek Reasoner', desc: '', available: false },
          ] });
        }, 30);
        // 宿主主动弹面板那条路：延迟一下，好确认「是它自己弹出来的」，
        // 而不是页面一加载就摆在那儿。
        if (SCENE === 'needlogin') setTimeout(() => fire({ t: 'idNeedsLogin' }), 1200);
        break;
      }

      // 浏览器里没有 C# 的 OpenFileDialog，这里回一条**假路径**，
      // 让「附件条 + 删除 + 纯附件发送」这套界面在 mock 里也能走通。
      // 真宿主下这条消息永远不会到前端（路径来自真实弹窗）。
      case 'pickFiles':
        setTimeout(() => fire({ t: 'files', files: ['C:\\演示\\季度报告.pdf'] }), 120);
        break;

      // 让 demo 能点得动：发一条就回一条假的
      case 'submit': {
        // 纯附件（goal 为空）也要有一条可见气泡，否则界面看起来像什么都没发生。
        fire({ t: 'item', item: it('Goal', {
          text: msg.goal || ('（仅附件 · ' + ((msg.files || []).length) + ' 个文件）'),
        }) });
        fire({ t: 'status', running: true, s: status() });
        setTimeout(() => {
          fire({ t: 'item', item: it('Assistant', {
            text: '（演示数据）收到：「' + msg.goal + '」\n\n这是 mock 宿主的假回复 —— 真正跑起来时，这里会是引擎的输出。',
          }) });
          fire({ t: 'status', running: false, s: status() });
        }, 600);
        break;
      }

      case 'cancel':
        // 注意是 {t:'item'}，不是 {t:'Notice'} —— 后者是条目类型，不是消息类型，
        // 发成顶层消息前端会当未知类型丢掉（曾经就是这么写的）。
        fire({ t: 'item', item: it('Notice', { text: '（演示数据）已取消' }) });
        fire({ t: 'status', running: false, s: status() });
        break;

      case 'refreshConvs':
        fire(convsMsg('remote'));
        break;

      // 网页版列表要联网 —— 真宿主是「点了才拉」，这里也故意延迟一下，
      // 好让前端的「正在读取…」态真的出现过。
      case 'listRemote':
        setTimeout(() => fire(convsMsg('remote')), 320);
        break;

      case 'attachSession': {
        const r = REMOTES.find((x) => x.id === msg.id);
        if (!r) { fire({ t: 'attached', ok: false }); break; }
        r.bound = true;
        r.active = true;
        CONVS.forEach((c) => { c.active = false; });
        fire({ t: 'attached', ok: true });
        fire(convsMsg('remote'));
        fire({ t: 'status', running: false, s: status({
          convTitle: r.title, convTurns: 0,
        }) });
        break;
      }

      case 'switchConv': {
        CONVS.forEach((c) => { c.active = c.id === msg.id; });
        fire(convsMsg('remote'));
        fire({ t: 'status', running: false, s: status({
          convTitle: (CONVS.find((c) => c.active) || {}).title || '（未命名）',
          convTurns: (CONVS.find((c) => c.active) || {}).turns || 0,
        }) });
        break;
      }

      case 'newConv':
        CONVS.forEach((c) => { c.active = false; });
        fire({ t: 'clear' });
        fire(convsMsg('remote'));
        fire({ t: 'status', running: false, s: status({ convTitle: '新会话', convTurns: 0 }) });
        break;

      case 'setChat':
        fire({ t: 'status', running: false, s: status({
          thinking: msg.thinking != null ? msg.thinking : true,
          search: msg.search != null ? msg.search : false,
          showThinking: msg.showThinking != null ? msg.showThinking : true,
        }) });
        break;

      case 'identity':
        fire({ t: 'status', running: false, s: status({ identityRefreshing: true }) });
        setTimeout(() => fire({ t: 'status', running: false, s: status() }), 700);
        break;

      /* ---------- 身份 / 凭据 ---------- */
      case 'idStatus':
        fire(idStateMsg());
        break;

      case 'idLabel':
        ID.label = msg.label;
        fire({ t: 'idres', req: msg.req, kind: 'label', ok: true, label: msg.label });
        fire(idStateMsg());
        break;

      case 'idSet': {
        const valid = String(msg.token || '').length >= 8;
        fire({ t: 'idres', req: msg.req, kind: 'set', ok: true, valid,
          detail: valid ? '账号 shaomo17 · 凭据有效' : '凭据被服务端拒绝（401）' });
        if (valid) { ID.hasToken = true; fire(idStateMsg()); }
        break;
      }

      case 'idRefresh':
        // 真宿主会先往对话流里写一条 Notice/Error，再由桥把它捞出来随结果回来。
        // 这里照做 —— 顺带也验了对话流那条渲染路径没坏。
        if (ID.hasToken) {
          const t1 = '凭据已刷新（来源 cache）· 账号 shaomo17 · 凭据有效';
          fire({ t: 'item', item: it('Notice', { text: t1 }) });
          fire({ t: 'idres', req: msg.req, kind: 'refresh', ok: true, text: t1 });
        } else {
          const t2 = '凭据不可用：凭据已被服务端作废（401）\n' +
            '可以用「打开浏览器登录」重新取一次，或手动粘贴 token。';
          fire({ t: 'item', item: it('Error', { text: t2 }) });
          fire({ t: 'idres', req: msg.req, kind: 'refresh', ok: false, text: t2 });
        }
        break;

      case 'idClear':
        ID.hasToken = false;
        fire({ t: 'idres', req: msg.req, kind: 'clear', ok: true });
        fire(idStateMsg());
        break;

      case 'idLogout':
        ID.hasToken = false;
        fire({ t: 'idres', req: msg.req, kind: 'logout', ok: true, removedProfile: true });
        fire(idStateMsg());
        break;

      case 'idLoginStart':
        // 引擎已经有一个会话时只回 already_running + port，**没有 browser**
        // （见 gad_engine/__main__.py）—— 这里照实模拟，好让前端那句
        // 「复用了已经打开的那个浏览器」真的被测到。
        if (ID.loginActive) {
          fire({ t: 'idres', req: msg.req, kind: 'loginStart', ok: true,
            browser: '', port: 9333, reused: true });
          break;
        }
        ID.polls = 0;
        ID.loginActive = true;
        // loginlost 场景：模拟「浏览器进程被外部结束 / 引擎重启过」——
        // 第一次轮询还在 waiting，之后会话就没了，引擎会回 no_session。
        // 不模拟的话这条分支只能靠读代码相信。
        ID.dropAfter = SCENE === 'loginlost' ? 1 : null;
        fire({ t: 'idres', req: msg.req, kind: 'loginStart', ok: true,
          browser: 'Chrome', port: 9333, reused: false });
        fire(idStateMsg());
        break;

      case 'idLoginPoll': {
        if (!ID.loginActive) {
          fire({ t: 'idres', req: msg.req, kind: 'loginPoll', ok: true, state: 'no_session', error: '' });
          break;
        }
        ID.polls += 1;
        if (ID.dropAfter != null && ID.polls > ID.dropAfter) {
          ID.loginActive = false;
          fire({ t: 'idres', req: msg.req, kind: 'loginPoll', ok: true, state: 'no_session', error: '' });
          break;
        }
        // 前两次 waiting、第三次 settling、之后 ready —— 让等待态真的出现过
        const st = ID.polls < 3 ? 'waiting' : (ID.polls === 3 ? 'settling' : 'ready');
        fire({ t: 'idres', req: msg.req, kind: 'loginPoll', ok: true, state: st, error: '' });
        break;
      }

      case 'idLoginFinish':
        ID.loginActive = false;
        ID.hasToken = true;
        if (!ID.label) ID.label = 'shaomo17';
        fire({ t: 'idres', req: msg.req, kind: 'loginFinish', ok: true,
          detail: '账号 shaomo17 · 凭据有效', label: ID.label });
        fire(idStateMsg());
        break;

      case 'idLoginCancel':
        ID.loginActive = false;
        fire({ t: 'idres', req: msg.req, kind: 'loginCancel', ok: true });
        fire(idStateMsg());
        break;

      // 窗口操作在 mock 里只打日志（浏览器里没有窗口可操作）
      case 'win':
        console.info('[mock] 窗口操作：', msg.op);
        break;

      default:
        break;
    }
  }

  window.MockBridge = { start, handle, scene: SCENE };
})();
