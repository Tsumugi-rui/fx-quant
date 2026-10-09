// ==UserScript==
// @name         fxquant 游戏桥接（DOM 版）
// @namespace    fxquant.bridge
// @version      0.6.0
// @description  把《FX 简单!》游戏页面的行情与交易操作暴露给本地 fxquant 量化引擎（自动关闭交易提示）
// @author       fxquant
// @match        https://www.bilibilitoy.com/toy/fx-simple*
// @match        https://www.bilibili.com/toy/fx-simple*
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// @run-at       document-idle
// ==/UserScript==

/**
 * 关于 frame：游戏本体在第二层 iframe 里
 * --------------------------------------
 * 实测扫出的完整页面层级：
 *
 *   TOP  www.bilibili.com/toy/fx-simple/index.html
 *    ├─ iframe[0]  www.bilibilitoy.com/toy/fx-simple/<id>/index.html
 *    │             ← ★ 游戏 DOM 在这里 ★
 *    └─ iframe[1]  s1.hdslb.com/bfs/seed/jinkela/.../iframe.html
 *                  ← 通信中转空壳，无游戏内容
 *
 * 三层互不同源，top 层读不到游戏 DOM。
 * 因此 @match 直接匹配 **游戏本体所在域名**（bilibilitoy.com），
 * 脚本会在那个 iframe 内部运行 —— 此时 document 就是游戏本体，
 * 选择器才能正常工作。
 *
 * 注意：s1.hdslb.com 那个中转壳不需要匹配，已移除。
 */

/**
 * 工作原理（DOM 版）
 * ------------------
 * 本脚本不调用游戏内部任何 JS 接口，而是像真人玩家一样：
 *   - 读：从页面 DOM 里读货币对报价、账户权益、持仓明细、新闻文本
 *   - 写：设置下单表单的 select/input，然后点击买卖按钮
 *
 * 为什么是 DOM 而不是内部接口
 * --------------------------
 * 经实测，游戏页面没有暴露 document.modelContext 之类的编程接口
 * （无 iframe、无全局对象）。页面就是普通 DOM。
 * 走 DOM 路线反而更干净：它做的事情和一个人用眼睛看价格、
 * 用鼠标点按钮完全一致，不触碰任何内部实现。
 *
 * 页面结构（实测确认）
 * -------------------
 *   价格行   button.pair-row            "EUR/USD 欧元/美元 0.99026 -0.18%"
 *            内部：strong[0]=货币对  strong[1]=价格
 *   下单表单 select#mobilePairSelect     选货币对
 *            input#marginInput           保证金
 *            input#leverageRange         杠杆
 *            button.trade-button.long    "∧ 做多 / 买入"（点击即成交）
 *            button.trade-button.short   "∨ 做空 / 卖出"
 *   持仓     div.position-row           每行一笔持仓
 *            button.panel-x.lite         "平仓"
 *   权益     span.money-large            "$10,000.00"
 *   新闻     div.news-ticker / .news-ticker-tag / .news-ticker-body
 *   提示浮层 类名不稳定，用「浮层结构 + 关闭按钮文案」识别后自动关闭
 *
 * 注意：DevTools Console 打印出的 name（select-one / margin / leverage）
 * 是浏览器给无 name 元素**生成的回退名**，不是真实属性。
 * 真实 id 见上表，选择器一律以 id 为准，旧名只作兜底。
 *
 * 安全说明
 * --------
 * - 只与本机 127.0.0.1 通讯，不向外发送任何数据
 * - 使用握手拿到的 token 校验，防止其它网页误连
 * - 只读取与交易相关的 DOM，不采集页面其它信息
 */

(function () {
  'use strict';

  const HOST = 'http://127.0.0.1:8765';
  const POLL_MS = 200;

  /**
   * 是否自动关闭平仓/开仓后的提示浮层。
   *
   * 这是纯 UI 便利功能，不参与任何交易决策。
   * 想手动看提示时把它改成 false 即可。
   *
   * 当前设为 false：该功能识别规则尚未在真实页面上验证过，
   * 为避免误点交易按钮，先关闭。
   */
  const AUTO_DISMISS = false;

  let TOKEN = null;
  let running = true;
  const stats = { polls: 0, done: 0, errors: 0 };

  // ---------------------------------------------------------------------
  // 悬浮状态面板
  // ---------------------------------------------------------------------
  const panel = document.createElement('div');
  panel.id = 'fxquant-bridge-panel';
  panel.style.cssText = [
    'position:fixed', 'right:12px', 'bottom:12px', 'z-index:999999',
    'background:rgba(20,45,42,.92)', 'color:#d8f2e8',
    'font:12px/1.6 -apple-system,"Microsoft YaHei",sans-serif',
    'padding:10px 14px', 'border-radius:10px', 'min-width:200px',
    'box-shadow:0 6px 22px rgba(0,0,0,.3)', 'pointer-events:none',
    'border:1px solid rgba(140,220,190,.35)',
  ].join(';');
  panel.innerHTML = '<b>fxquant 桥接</b><div id="fxq-line">正在初始化…</div>';

  function mountPanel() {
    if (!document.body) return;
    if (!document.getElementById('fxquant-bridge-panel')) {
      document.body.appendChild(panel);
    }
  }

  function setLine(text, color) {
    mountPanel();
    const el = document.getElementById('fxq-line');
    if (el) {
      el.textContent = text;
      el.style.color = color || '#d8f2e8';
    }
  }

  // ---------------------------------------------------------------------
  // 页面读取层：从 DOM 提取公开信息
  // ---------------------------------------------------------------------
  const PAIR_IDS = [
    'EUR/USD', 'GBP/USD', 'USD/JPY', 'USD/CHF',
    'EUR/CHF', 'AUD/USD', 'USD/CAD',
  ];

  /** 把带千分位的数字字符串转成 float。 */
  function toNum(s) {
    if (s == null) return 0;
    const m = String(s).replace(/[,\s$%]/g, '').match(/-?\d+(\.\d+)?/);
    return m ? parseFloat(m[0]) : 0;
  }

  /** 从一行 pair-row 里读出「货币对 -> 价格」。 */
  function parsePairRow(row) {
    const strongs = [...row.querySelectorAll('strong')]
      .map((s) => s.textContent.trim());
    if (strongs.length < 2) return null;
    const pair = strongs[0].replace(/\s/g, '');
    const price = toNum(strongs[1]);
    if (!PAIR_IDS.includes(pair) || price <= 0) return null;
    return { pair, price };
  }

  /** 读取全部货币对报价。 */
  function readPrices() {
    const out = {};
    document.querySelectorAll('.pair-row').forEach((row) => {
      const r = parsePairRow(row);
      if (r) out[r.pair] = r.price;
    });
    return out;
  }

  /** 读取账户权益 / 现金。 */
  function readMoney() {
    const el = document.querySelector('span.money-large')
      || document.querySelector('.side-right-column .money-large');
    const equity = el ? toNum(el.textContent) : 0;
    return { equity, cash: equity };
  }

  /**
   * 读取持仓。
   *
   * position-row 的文本形如：
   *   "EUR/USD多$500.00 保证金 $10,000.00 仓位 0.99760
   *    开仓价格 0.99026 当前价格 0.99026 -$73.57 浮动盈亏"
   *
   * 注意：不同版本措辞可能微调，因此全部用正则宽松匹配，
   * 匹配不到就跳过该行，不猜测值。
   */
  function readPositions() {
    const list = [];
    document.querySelectorAll('.position-row').forEach((row, idx) => {
      const txt = (row.textContent || '').replace(/\s+/g, ' ');

      // 货币对
      let pair = '';
      for (const p of PAIR_IDS) {
        if (txt.includes(p)) { pair = p; break; }
      }
      if (!pair) return;

      // 方向：中文「多」/「空」，或英文 long/short
      const side = /空|short/i.test(txt) ? 'short' : 'long';

      // 保证金：紧跟「保证金」前后的金额
      //
      // 真实页面文本形如「多$500.00 保证金 $10,000.00 仓位 …」，
      // 即金额在「保证金」之前。而「$10,000.00 仓位」是名义价值，
      // 不是保证金，必须避免误取。
      // 因此优先取「保证金」左侧最近的金额，取不到再取右侧。
      const marginBefore = txt.match(/\$([\d,]+(?:\.\d+)?)\s*保证金/);
      const marginAfter = txt.match(/保证金[^-\d]*([\d,]+(?:\.\d+)?)/);
      const margin = marginBefore
        ? toNum(marginBefore[1])
        : (marginAfter ? toNum(marginAfter[1]) : 0);

      // 开仓价格
      const entryM = txt.match(/开仓价[格]?[^-\d]*([\d.]+)/);
      const entry = entryM ? toNum(entryM[1]) : 0;

      // 浮动盈亏：找最后一个带符号的 $ 金额
      const pnlMatches = [...txt.matchAll(/([+-])\$?([\d,]+(?:\.\d+)?)/g)];
      let pnl = 0;
      if (pnlMatches.length) {
        const last = pnlMatches[pnlMatches.length - 1];
        pnl = (last[1] === '-' ? -1 : 1) * toNum(last[2]);
      }

      list.push({
        id: `pos-${idx}-${pair}`,
        pair,
        side,
        margin,
        entry,
        pnl,
        row,
      });
    });
    return list;
  }

  /** 读取最近的新闻快讯（仅文本，不含解析）。 */
  function readNews() {
    const el = document.querySelector('.news-ticker')
      || document.querySelector('.news-ticker-body');
    if (!el) return '';
    return (el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 300);
  }

  /**
   * 当前模式。
   *
   * 实测：游戏页面的模式按钮并没有稳定的「选中」类名可依赖，
   * 且这些按钮可能根本不在当前 DOM（例如由其它 UI 层渲染）。
   * 因此这里只是尽力而为：
   *   - 能识别出「真实数据」就返回 real
   *   - 其余情况一律返回 sim
   *
   * 模式只影响日志显示，不参与任何交易决策，
   * 所以识别不出时不应报 unknown 干扰判断。
   */
  function readMode() {
    const btns = [...document.querySelectorAll(
      '.text-button, [class*="mode"] button, [class*="tab"] button')];
    for (const b of btns) {
      const t = (b.textContent || '').trim();
      const cls = String(b.className || '');
      const selected = /active|selected|checked|current/i.test(cls);
      if (selected && t.includes('真实')) return 'real';
    }
    // 退化判断：页面出现「下一交易日」按钮说明是真实数据模式
    const hasNextDay = [...document.querySelectorAll('button')].some(
      (b) => (b.textContent || '').includes('下一交易日')
        && b.offsetParent !== null);
    if (hasNextDay) return 'real';
    return 'sim';
  }

  /** 组装一份完整游戏状态。 */
  function readGameState() {
    const money = readMoney();
    return {
      mode: readMode(),
      cash: money.cash,
      equity: money.equity,
      prices: readPrices(),
      positions: readPositions().map((p) => ({
        id: p.id, pair: p.pair, side: p.side,
        margin: p.margin, leverage: 0, entry: p.entry, pnl: p.pnl,
      })),
      news: readNews(),
    };
  }
  // ---------------------------------------------------------------------
  // 页面写入层：像玩家一样操作
  // ---------------------------------------------------------------------

  /**
   * 找元素：优先用最精确的选择器，逐个尝试到命中为止。
   *
   * 为什么需要多选择器：实测发现游戏的下单表单元素
   * **没有 name 属性**，只有 id：
   *   <select id="mobilePairSelect" aria-label="选择货币对">
   *   <input  id="marginInput"   type="number">
   *   <input  id="leverageRange" type="range">
   *
   * 早期探测时 Console 显示的 `name="select-one"` /
   * `name="margin"` 是浏览器给无 name 元素生成的**回退名**，
   * 并非真实属性，用它做选择器会全部落空。
   * 因此这里以 id 为主，同时保留 name 选择器作兼容。
   */
  function pick(selectors) {
    for (const sel of selectors) {
      const el = document.querySelector(sel);
      if (el) return el;
    }
    return null;
  }

  /** 货币对选择框。 */
  function findPairSelect() {
    return pick([
      '#mobilePairSelect',
      'select[aria-label="选择货币对"]',
      '.order-panel select',
      'select[name="select-one"]',
      'select',
    ]);
  }

  /** 保证金输入框。 */
  function findMarginInput() {
    return pick([
      '#marginInput',
      'input[name="margin"]',
      '.order-panel input[type="number"]',
    ]);
  }

  /** 杠杆输入框（滑块或数字框）。 */
  function findLeverageInput() {
    return pick([
      '#leverageRange',
      '#leverageInput',
      'input[name="leverage"]',
      '.order-panel input[type="range"]',
    ]);
  }

  /** 用原生 setter 改输入框的值，确保触发框架的变更监听。 */
  function setNativeValue(el, value) {
    const proto = el instanceof HTMLSelectElement
      ? HTMLSelectElement.prototype
      : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
    if (setter) {
      setter.call(el, String(value));
    } else {
      el.value = String(value);
    }
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  }

  /** 在 select 里选中指定货币对。 */
  function selectPair(pair) {
    const sel = findPairSelect();
    if (!sel) return { ok: false, error: '找不到货币对选择框' };
    const target = [...sel.options].find(
      (o) => o.value === pair || (o.textContent || '').includes(pair));
    if (!target) return { ok: false, error: `选择框里没有 ${pair}` };
    setNativeValue(sel, target.value);
    return { ok: true };
  }

  /** 设置保证金与杠杆。 */
  function setOrderParams(margin, leverage) {
    const mIn = findMarginInput();
    if (!mIn) return { ok: false, error: '找不到保证金输入框' };
    setNativeValue(mIn, Math.round(margin));
    const lIn = findLeverageInput();
    if (lIn && leverage) setNativeValue(lIn, Math.round(leverage));
    return { ok: true };
  }

  /** 点击下单按钮（一次点击即成交，无二次确认）。 */
  function clickTrade(side) {
    const sel = side === 'long'
      ? '.trade-button.long'
      : '.trade-button.short';
    const btn = document.querySelector(sel);
    if (!btn) return { ok: false, error: `找不到下单按钮 ${side}` };
    if (btn.disabled) return { ok: false, error: `下单按钮不可用（可能余额不足）` };
    btn.click();
    return { ok: true };
  }

  /** 等待 DOM 满足条件（页面点击后渲染不是同步的）。 */
  function waitFor(fn, timeout = 1500, step = 60) {
    return new Promise((resolve) => {
      const t0 = Date.now();
      const tick = () => {
        let v = null;
        try { v = fn(); } catch (e) { v = null; }
        if (v) return resolve(v);
        if (Date.now() - t0 >= timeout) return resolve(null);
        setTimeout(tick, step);
      };
      tick();
    });
  }

  /**
   * 开仓：选对 → 设参数 → 点按钮 → 等页面渲染出新持仓。
   *
   * 注意点击后 DOM 不是立即更新的，必须等一下再回读，
   * 否则会拿到过期的持仓列表（拿不到成交价）。
   */
  async function placeTrade(pair, side, margin, leverage) {
    const beforeCount = document.querySelectorAll('.position-row').length;

    const s1 = selectPair(pair);
    if (!s1.ok) return s1;
    const s2 = setOrderParams(margin, leverage);
    if (!s2.ok) return s2;
    const s3 = clickTrade(side);
    if (!s3.ok) return s3;

    // 等新持仓出现（数量增加），最多等 1.5 秒
    await waitFor(
      () => document.querySelectorAll('.position-row').length > beforeCount);

    // 回读该货币对的持仓，给引擎真实的成交价
    // ★ 回读要立刻做，不能被清理提示的逻辑插队/延迟
    let entry = 0;
    let id = '';
    const rows = readPositions();
    for (let i = rows.length - 1; i >= 0; i--) {
      if (rows[i].pair === pair) { entry = rows[i].entry; id = rows[i].id; break; }
    }

    // 开仓也可能弹提示（下单成功/余额不足等），后台清理，不阻塞
    if (AUTO_DISMISS) purgeSoon(600);

    if (!id) {
      return { ok: false, error: `点击后未检测到 ${pair} 新持仓，可能余额不足或按钮被禁用` };
    }
    return { ok: true, id, entry, fee: 0, remainingCash: readMoney().cash };
  }

  /**
   * 平仓：按货币对定位持仓行，点它的「平仓」按钮。
   *
   * ID 的格式是 `pos-<序号>-<货币对>`，但序号会随列表刷新而变，
   * 因此**不用序号定位**，只用货币对匹配 —— 货币对在持仓期间是稳定的。
   * （一个货币对同时持有多笔的情况，取第一行）
   */
  async function closeTrade(positionId) {
    const m = String(positionId).match(/^pos-\d+-(.+)$/);
    const pair = m ? m[1] : String(positionId);

    const findRow = () => {
      for (const r of document.querySelectorAll('.position-row')) {
        if ((r.textContent || '').includes(pair)) return r;
      }
      return null;
    };

    const row = findRow();
    if (!row) return { ok: false, error: `找不到 ${pair} 的持仓` };

    const countBefore = document.querySelectorAll('.position-row').length;
    // 平仓前先记下浮动盈亏，供引擎核对（成交后 DOM 会消失，读不到了）
    const posBefore = readPositions().find((p) => p.pair === pair);
    const pnlHint = posBefore ? posBefore.pnl : 0;

    const btn = row.querySelector('button.panel-x')
      || [...row.querySelectorAll('button')]
        .find((b) => (b.textContent || '').includes('平仓'));
    if (!btn) return { ok: false, error: `找不到 ${pair} 的平仓按钮` };

    btn.click();

    // 等该持仓行消失
    await waitFor(
      () => document.querySelectorAll('.position-row').length < countBefore
        || !findRow());

    // 平仓后可能弹「平仓成功」提示，起一个后台清理窗口把它关掉。
    // 注意是**非阻塞**的：不 await，交易流程继续走。
    if (AUTO_DISMISS) purgeSoon(1500);

    return { ok: true, pnl: pnlHint, cash: readMoney().cash };
  }

  // ---------------------------------------------------------------------
  // 自动关闭提示浮层
  // ---------------------------------------------------------------------
  /**
   * 平仓（有时开仓也是）之后，游戏会弹一个提示框，
   * 通常带一个「确定 / 关闭 / ×」按钮，需要玩家手动点掉。
   * 量化程序连续交易时会不断弹，既遮挡界面也拖慢节奏，
   * 这里自动把它关掉。
   *
   * === 两条红线与此功能的关系 ===
   * 这里处理的是 **UI 元素**（浮层、按钮），不是行情算法。
   * 它和「读价格 DOM」「点买卖按钮」性质相同 —— 都是界面操作，
   * 不涉及任何随机数公式 / 内部状态 / 未公开数据。
   * 关掉一个提示框不会给策略带来任何信息优势。
   *
   * === 为什么要写得这么"宽" ===
   * 游戏前端是打包过的，提示框的类名不可预知且可能随版本变化。
   * 与其写死一个 `.toast-close`，不如用「结构特征 + 按钮文案」
   * 双重特征来识别，这样跨版本更稳：
   *
   *   1. 候选浮层 = 定位为 fixed/absolute 且 z-index 较高的块
   *   2. 内部存在文案匹配「确定/关闭/知道了/×」的按钮
   *   3. 浮层文本不是核心界面（不含价格表/持仓/下单面板特征）
   *
   * 识别不出时**什么都不做**，绝不误关正常界面。
   */

  // 关闭按钮的精确文案。
  //
  // ★ 安全要点：这里只收「明确表示关闭」的词，并且要求**精确相等**
  //   （trim 后 ===），不用 includes 模糊匹配。
  //   原因：模糊匹配会把「确认下单」「确认平仓」这类**交易按钮**也命中，
  //   一旦误点就可能产生非预期的交易 —— 这是本功能最高危的坑。
  const CLOSE_WORDS = [
    '确定', '关闭', '知道了', '我知道了', '好的', '知道了。',
    'OK', 'Ok', 'ok', 'Close', 'close',
    '×', '✕', '✖', '✗', '╳', '⨯', '✕',
  ];

  // 「绝不动」的容器特征：核心交易界面。
  // 命中的元素**自身或其后代**含这些类时不处理。
  const NEVER_CLOSE = [
    '.position-row', '.pair-row', '.order-panel', '.trade-button',
    '.money-large', '.news-ticker', '.position-list',
  ];

  // 「绝不动」的按钮：即使文案像关闭，只要类名/属性像交易动作也跳过。
  // 这是第二道闸，专门防「高仿关闭按钮」。
  const DANGER_BTN = /trade|order|buy|sell|long|short|submit|place|做多|做空|买入|卖出|下单/;

  /**
   * 判断元素是否是一个**安全的**可关闭提示浮层。
   *
   * 相比初版全面收紧，只保留最可信的信号：
   *   ① 不是核心交易界面（自身及后代都不含 NEVER_CLOSE）
   *   ② 是浮层（fixed，或 absolute 且 z-index 较高）
   *   ③ 尺寸明显小于视口（排除整页容器）
   *   ④ 内部有**精确文案**的关闭按钮（且该按钮不带交易类名）
   */
  function looksLikeDialog(el) {
    if (!el || el.nodeType !== 1) return false;
    if (el === document.body || el === document.documentElement) return false;

    // ① 核心界面保护
    for (const sel of NEVER_CLOSE) {
      try {
        if (el.matches(sel)) return false;
        if (el.querySelector(sel)) return false;
      } catch (e) { /* 忽略非法选择器 */ }
    }

    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') return false;
    const op = parseFloat(cs.opacity);
    if (!Number.isNaN(op) && op < 0.2) return false;
    if (cs.pointerEvents === 'none') return false;

    // ② 浮层判定
    const z = parseInt(cs.zIndex, 10) || 0;
    const isOverlay = cs.position === 'fixed'
      || (cs.position === 'absolute' && z >= 100);
    if (!isOverlay) return false;

    // ③ 尺寸必须明显小于视口
    let r;
    try { r = el.getBoundingClientRect(); } catch (e) { return false; }
    if (!r || r.width === 0 || r.height === 0) return false;
    if (r.width > window.innerWidth * 0.9 && r.height > window.innerHeight * 0.8) {
      return false;
    }

    // ④ 有安全的关闭按钮
    return !!findCloseButton(el);
  }

  /**
   * 在容器内找一个**安全的**关闭按钮。
   *
   * 只接受两种信号：
   *   a) 文本 trim 后**精确等于** CLOSE_WORDS 中的词
   *   b) class 含 close / dismiss（**不含 confirm** —— confirm 太像交易确认）
   *
   * 命中 DANGER_BTN 的按钮一律跳过。
   */
  function findCloseButton(root) {
    const btns = root.querySelectorAll('button, [role="button"], a');
    for (const b of btns) {
      if (b.disabled) continue;

      const t = (b.textContent || '').trim();
      const cls = String(b.className || '');
      const attr = (b.getAttribute('aria-label') || '') + ' '
        + (b.getAttribute('title') || '');
      const blob = `${t} ${cls} ${attr}`;

      // 危险按钮：像交易动作的一律不动
      if (DANGER_BTN.test(blob)) continue;

      // a) 精确文案
      if (t && CLOSE_WORDS.includes(t)) return b;

      // b) class / aria 明确是关闭
      if (/close|dismiss/i.test(cls + ' ' + attr)) return b;
    }
    return null;
  }

  /**
   * 扫描并关闭当前所有**安全的**提示浮层，返回关闭个数。
   *
   * 按 z-index 从高到低处理：先关最上层的提示，
   * 关掉后下层元素才可能「露出来」被识别。最多关 3 个，
   * 避免在异常页面上无限循环点击。
   */
  function dismissDialogs() {
    let closed = 0;

    // 收集候选：浮层定位 + 当前可见
    const candidates = [];
    document.querySelectorAll('body *').forEach((el) => {
      let cs;
      try { cs = getComputedStyle(el); } catch (e) { return; }
      if (cs.position !== 'fixed' && cs.position !== 'absolute') return;
      if (cs.display === 'none' || cs.visibility === 'hidden') return;
      const z = parseInt(cs.zIndex, 10) || 0;
      if (cs.position === 'absolute' && z < 100) return;
      candidates.push({ el, z });
    });

    // 高层级优先
    candidates.sort((a, b) => b.z - a.z);

    for (const { el } of candidates) {
      if (closed >= 3) break;
      if (!looksLikeDialog(el)) continue;
      const btn = findCloseButton(el);
      if (!btn) continue;
      try {
        btn.click();
        closed += 1;
      } catch (e) { /* 点击失败跳过 */ }
    }
    return closed;
  }

  /**
   * 触发一段「后台清理窗口」，在指定时长内反复清理提示。
   *
   * ★ 非阻塞：这里**不 await、不 sleep 主流程**。
   *   初版是 `await suppressDialogs(1500)`，会把 closeTrade 所在的
   *   整个轮询循环卡住 1.5 秒，导致任务队列堆积。
   *   现在改为起一个后台定时器，立即返回，交易流程不受影响。
   *
   * 提示可能异步弹出、也可能连续弹多个，所以在时间窗内反复清理。
   */
  function purgeSoon(durationMs) {
    const deadline = Date.now() + (durationMs || 1200);
    const timer = setInterval(() => {
      try {
        dismissDialogs();
      } catch (e) { /* 清理失败不影响主流程 */ }
      if (Date.now() >= deadline) clearInterval(timer);
    }, 150);
    // 兜底：5 秒后无论如何停掉，防止异常场景下定时器泄漏
    setTimeout(() => clearInterval(timer), (durationMs || 1200) + 3000);
    return timer;
  }

  // ---------------------------------------------------------------------
  // 通讯
  // ---------------------------------------------------------------------
  /**
   * 与本地桥接服务通讯。
   *
   * === 双通道设计（为什么需要两个）===
   * 游戏页面是 https，桥接服务是 http://127.0.0.1。
   * 浏览器默认会拦截 https 页面发往 http 的请求（mixed content），
   * Chrome 142+ 还会加一层「本地网络访问」权限。
   *
   * 通道 1：GM_xmlhttpRequest（优先）
   *   油猴扩展提供的特权请求，**不受页面混合内容与 CORS 限制**，
   *   是跨源访问本机服务最可靠的方式。
   *
   * 通道 2：原生 fetch + targetAddressSpace:'loopback'
   *   无 GM API 时的降级方案。依赖规范的「私有 IP 字面值豁免」
   *   以及用户授予的本地网络访问权限。
   *
   * 启动时自动探测哪个通道可用。
   */
  let CHANNEL = null;   // 'gm' | 'fetch'

  /** 用 GM_xmlhttpRequest 发一个请求，返回 Promise<{status, text}>。 */
  function gmRequest(path, method, body) {
    return new Promise((resolve, reject) => {
      const fn = (typeof GM_xmlhttpRequest === 'function')
        ? GM_xmlhttpRequest
        : (typeof GM !== 'undefined' && GM.xmlHttpRequest)
          ? GM.xmlHttpRequest.bind(GM)
          : null;
      if (!fn) return reject(new Error('GM_xmlhttpRequest 不可用'));
      fn({
        method: method || 'GET',
        url: `${HOST}${path}`,
        headers: body ? { 'Content-Type': 'application/json' } : undefined,
        data: body || undefined,
        timeout: 8000,
        onload: (r) => resolve({ status: r.status, text: r.responseText }),
        onerror: () => reject(new Error('GM 请求失败')),
        ontimeout: () => reject(new Error('GM 请求超时')),
      });
    });
  }

  /** 统一请求入口：先试 GM，失败则回退原生 fetch。 */
  async function request(path, options) {
    const opts = options || {};
    const method = opts.method || 'GET';

    if (CHANNEL === 'gm' || CHANNEL === null) {
      try {
        const r = await gmRequest(path, method, opts.body);
        CHANNEL = 'gm';
        return JSON.parse(r.text);
      } catch (e) {
        if (CHANNEL === 'gm') throw e;
        // 否则继续尝试 fetch
      }
    }

    // 原生 fetch 回退
    const fopts = { cache: 'no-store', method };
    if (opts.headers) fopts.headers = opts.headers;
    if (opts.body) fopts.body = opts.body;
    try { fopts.targetAddressSpace = 'loopback'; } catch (e) { /* 忽略 */ }
    const resp = await fetch(`${HOST}${path}`, fopts);
    CHANNEL = 'fetch';
    return resp.json();
  }

  async function handshake() {
    try {
      const j = await request('/handshake');
      if (j && j.token) { TOKEN = j.token; return true; }
    } catch (e) { /* 服务未启动或被拦截 */ }
    return false;
  }

  async function poll() {
    if (!TOKEN) return;
    try {
      const j = await request(`/poll?token=${TOKEN}`);
      stats.polls++;
      if (j && j.task) await runTask(j.task);
    } catch (e) { /* 忽略瞬时错误 */ }
  }

  /** 执行一个来自 Python 引擎的任务。 */
  async function runTask(task) {
    let payload;
    try {
      const params = task.params || {};
      let res;
      switch (task.tool) {
        case 'read_fx_game_state':
          res = readGameState();
          break;
        case 'place_fx_trade':
          // placeTrade / closeTrade 是异步的（点击后要等 DOM 渲染），
          // 必须 await，否则拿到的是 Promise 而不是结果对象
          res = await placeTrade(params.pair, params.side,
            params.margin, params.leverage);
          break;
        case 'close_fx_trade':
          res = await closeTrade(params.id);
          break;
        case 'advance_real_fx_day': {
          // 真实数据模式才有「下一交易日」按钮；模拟模式行情自动跑
          const next = [...document.querySelectorAll('button')].find((b) =>
            (b.textContent || '').includes('下一交易日')
            && b.offsetParent !== null);
          if (next) { next.click(); res = { ok: true, advanced: true }; }
          else { res = { ok: true, advanced: false, note: '当前模式无需手动推进' }; }
          break;
        }
        default:
          res = { ok: false, error: `未知任务 ${task.tool}` };
      }
      payload = (res && res.ok === false)
        ? { ok: false, error: res.error || '操作失败' }
        : { ok: true, data: res };
      if (payload.ok) stats.done++; else stats.errors++;
    } catch (err) {
      payload = { ok: false, error: String((err && err.message) || err) };
      stats.errors++;
    }
    payload.id = task.id;
    try {
      await request('/result', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
    } catch (e) { /* 服务已关闭 */ }
  }

  // ---------------------------------------------------------------------
  // 主循环
  // ---------------------------------------------------------------------
  async function main() {
    const inFrame = window.top !== window.self;

    // 顶层实例静默退出。
    //
    // 游戏本体在 iframe 里，顶层 document 永远不会有 .pair-row。
    // 如果顶层也显示面板，会与 iframe 内的面板重复，造成干扰。
    // 因此顶层实例只在等待若干秒后仍找不到价格行时，直接不显示面板。
    if (!inFrame) {
      // 短暂等待，兼容「游戏确实渲染在顶层」的其它页面形态
      let found = false;
      for (let i = 0; i < 5; i++) {
        if (document.querySelectorAll('.pair-row').length > 0) { found = true; break; }
        await new Promise((r) => setTimeout(r, 400));
      }
      if (!found) return;   // 顶层无游戏，静默退出，不挂面板
    }

    mountPanel();

    // 持续等待页面渲染出价格行。
    //
    // 为什么不设超时放弃：游戏由前端框架异步渲染，
    // 页面可能在脚本注入后很久才画出价格行（切换模式、路由跳转等
    // 也会重绘）。一旦放弃就再也不会恢复，所以要一直等。
    let waited = 0;
    while (running) {
      const n = document.querySelectorAll('.pair-row').length;
      if (n > 0) break;
      waited += 1;
      // 前 3 秒每秒提示一次，之后改成每 10 秒，避免刷屏
      if (waited <= 3 || waited % 10 === 0) {
        const where = inFrame ? 'iframe' : '顶层';
        setLine(`[${where}] 等待游戏就绪… (${waited}s)`, '#f0d68a');
      }
      await new Promise((r) => setTimeout(r, 1000));
    }
    if (!running) return;

    const pairCount = Object.keys(readPrices()).length;
    if (pairCount === 0) {
      setLine(`找到 ${document.querySelectorAll('.pair-row').length} 行但解析失败`, '#ef8ba0');
    }

    // 连接本地桥接服务（服务没开就一直重试）
    if (!(await handshake())) {
      // 区分「服务没开」和「被浏览器拦截」：前者连不上，
      // 后者会抛混合内容/LNA 相关错误。这里把可能原因一并提示。
      setLine(`[${inFrame ? 'iframe' : '顶层'}] 已就绪(${pairCount}对)\n`
        + '本地服务未连上，请确认：\n'
        + '① Python 端已运行 bridge/live\n'
        + '② 浏览器弹出「本地网络访问」时点允许', '#f0d68a');
      while (running) {
        await new Promise((r) => setTimeout(r, 2000));
        if (await handshake()) break;
      }
      if (!running) return;
    }

    setLine(`已连接 · ${pairCount} 个货币对`, '#8ce0be');

    while (running) {
      await poll();
      await new Promise((r) => setTimeout(r, POLL_MS));

      // 低频兜底：每 25 次轮询（约 5 秒）清一次残留提示。
      // 主要清理「没有走 closeTrade 路径」的提示
      // （例如游戏自己弹的行情快讯弹窗、或玩家手动操作留下的框）。
      if (stats.polls % 25 === 0 && AUTO_DISMISS) {
        dismissDialogs();
      }

      if (stats.polls % 25 === 0) {
        setLine(`已连接 · 已执行 ${stats.done} 次`
          + (stats.errors ? ` · 失败 ${stats.errors}` : ''), '#8ce0be');
      }
    }
  }

  window.addEventListener('beforeunload', () => { running = false; });
  main();
})();
