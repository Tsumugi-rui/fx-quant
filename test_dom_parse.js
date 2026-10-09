/**
 * DOM 解析逻辑测试。
 *
 * 用从真实游戏页面抓取的结构做夹具，验证 browser_bridge.user.js
 * 里的读取/写入逻辑是否正确。
 *
 * 运行（先在本目录 npm install jsdom）：
 *   npm install jsdom
 *   node test_dom_parse.js
 *
 * 注意：此测试只验证「解析与操作逻辑」，
 * 不连接真实页面、不发起任何交易。
 */

const { JSDOM } = require('jsdom');

// ---------------------------------------------------------------------------
// 夹具：照抄真实页面结构（来自用户 Console 探测输出）
// ---------------------------------------------------------------------------
const PAIRS = [
  ['EUR/USD', '欧元 / 美元', '0.99026', '-0.18%', 'negative'],
  ['GBP/USD', '英镑 / 美元', '1.20630', '-0.05%', 'negative'],
  ['USD/JPY', '美元 / 日元', '152.583', '-0.11%', 'negative'],
  ['USD/CHF', '美元 / 瑞郎', '0.86122', '-0.22%', 'negative'],
  ['EUR/CHF', '欧元 / 瑞郎', '0.97532', '-0.04%', 'negative'],
  ['AUD/USD', '澳元 / 美元', '0.66660', '+0.07%', 'positive'],
  ['USD/CAD', '美元 / 加元', '1.38776', '+0.19%', 'positive'],
];

const pairRows = PAIRS.map(([id, name, price, chg, cls], i) => `
  <button class="pair-row${i === 0 ? ' selected' : ''}">
    <span class="pair-names"><strong>${id}</strong><small>${name}</small></span>
    <span class="pair-numbers">
      <strong>${price}</strong><small class="${cls}">${chg}</small>
    </span>
  </button>`).join('');

const positionRows = `
  <div class="position-row">
    <span class="position-pair">EUR/USD</span>
    <span class="position-side">多</span>
    <span class="position-margin">$500.00 保证金</span>
    <span class="position-notional">$10,000.00 仓位 0.99760</span>
    <span class="position-entry">开仓价格 0.99026</span>
    <span class="position-current">当前价格 0.99026</span>
    <span class="position-pnl">-$73.57 浮动盈亏</span>
    <button class="panel-x lite">平仓</button>
  </div>`;

const HTML = `<!DOCTYPE html><html><body>
  <div class="app-shell"><main><div class="workspace">
    <div class="panel market-watch">${pairRows}</div>
    <div class="news-ticker">
      <span class="news-ticker-tag">快讯</span>
      <span class="news-ticker-body">【就业数据】新增就业明显降温，交易者下调紧缩预期</span>
    </div>
    <aside class="side-right-column">
      <span class="money-large" id="money">$10,000.00</span>
      <span class="panel order-panel">
        <select id="mobilePairSelect" aria-label="选择货币对">
          ${PAIRS.map(([id]) => `<option value="${id}">${id} · ${id}</option>`).join('')}
        </select>
        <input id="marginInput" type="number" value="500">
        <input id="leverageRange" type="range" value="20">
        <div class="order-preview">开仓手续费$1.50约可承受反向波动 4.00%</div>
        <div class="trade-buttons">
          <button class="trade-button long">∧ 做多 / 买入</button>
          <button class="trade-button short">∨ 做空 / 卖出</button>
        </div>
      </span>
    </aside>
    <div class="panel positions-panel">
      <div class="position-list">${positionRows}</div>
    </div>
    <button class="text-button active">模拟数据</button>
    <button class="text-button">真实数据</button>
    <button class="text-button ticker-more">新闻中心</button>
  </div></main></div>
</body></html>`;

// ---------------------------------------------------------------------------
// 被测逻辑：从 user.js 抽出的纯函数（保持与源文件一致）
// ---------------------------------------------------------------------------
const PAIR_IDS = PAIRS.map(([id]) => id);

function toNum(s) {
  if (s == null) return 0;
  const m = String(s).replace(/[,\s$%]/g, '').match(/-?\d+(\.\d+)?/);
  return m ? parseFloat(m[0]) : 0;
}

function parsePairRow(row) {
  const strongs = [...row.querySelectorAll('strong')].map((s) => s.textContent.trim());
  if (strongs.length < 2) return null;
  const pair = strongs[0].replace(/\s/g, '');
  const price = toNum(strongs[1]);
  if (!PAIR_IDS.includes(pair) || price <= 0) return null;
  return { pair, price };
}

function readPrices(doc) {
  const out = {};
  doc.querySelectorAll('.pair-row').forEach((row) => {
    const r = parsePairRow(row);
    if (r) out[r.pair] = r.price;
  });
  return out;
}

function readMoney(doc) {
  const el = doc.querySelector('span.money-large');
  const equity = el ? toNum(el.textContent) : 0;
  return { equity, cash: equity };
}

function readPositions(doc) {
  const list = [];
  doc.querySelectorAll('.position-row').forEach((row, idx) => {
    const txt = (row.textContent || '').replace(/\s+/g, ' ');
    let pair = '';
    for (const p of PAIR_IDS) { if (txt.includes(p)) { pair = p; break; } }
    if (!pair) return;
    const side = /空|short/i.test(txt) ? 'short' : 'long';
    // 保证金优先取「保证金」左侧金额（真实页面是「$500.00 保证金」），
    // 取不到再取右侧，避免误取「$10,000.00 仓位」这个名义价值
    const marginBefore = txt.match(/\$([\d,]+(?:\.\d+)?)\s*保证金/);
    const marginAfter = txt.match(/保证金[^-\d]*([\d,]+(?:\.\d+)?)/);
    const margin = marginBefore
      ? toNum(marginBefore[1])
      : (marginAfter ? toNum(marginAfter[1]) : 0);
    const entryM = txt.match(/开仓价[格]?[^-\d]*([\d.]+)/);
    const entry = entryM ? toNum(entryM[1]) : 0;
    const pnlMatches = [...txt.matchAll(/([+-])\$?([\d,]+(?:\.\d+)?)/g)];
    let pnl = 0;
    if (pnlMatches.length) {
      const last = pnlMatches[pnlMatches.length - 1];
      pnl = (last[1] === '-' ? -1 : 1) * toNum(last[2]);
    }
    list.push({ id: `pos-${idx}-${pair}`, pair, side, margin, entry, pnl });
  });
  return list;
}

function readMode(doc) {
  const act = doc.querySelector('.text-button.active');
  const t = act ? (act.textContent || '').trim() : '';
  if (t.includes('真实')) return 'real';
  if (t.includes('模拟')) return 'sim';
  return 'unknown';
}

function readNews(doc) {
  const el = doc.querySelector('.news-ticker') || doc.querySelector('.news-ticker-body');
  if (!el) return '';
  return (el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 300);
}

// ---------------------------------------------------------------------------
// 提示浮层自动关闭 —— 与 browser_bridge.user.js 中同名逻辑保持一致
// ---------------------------------------------------------------------------
const CLOSE_WORDS = [
  '确定', '关闭', '知道了', '我知道了', '好的', '知道了。',
  'OK', 'Ok', 'ok', 'Close', 'close',
  '×', '✕', '✖', '✗', '╳', '⨯',
];
const NEVER_CLOSE = [
  '.position-row', '.pair-row', '.order-panel', '.trade-button',
  '.money-large', '.news-ticker', '.position-list',
];
const DANGER_BTN = /trade|order|buy|sell|long|short|submit|place|做多|做空|买入|卖出|下单/;

function findCloseButton(root) {
  const btns = root.querySelectorAll('button, [role="button"], a');
  for (const b of btns) {
    if (b.disabled) continue;
    const t = (b.textContent || '').trim();
    const cls = String(b.className || '');
    const attr = (b.getAttribute('aria-label') || '') + ' '
      + (b.getAttribute('title') || '');
    const blob = `${t} ${cls} ${attr}`;
    if (DANGER_BTN.test(blob)) continue;
    if (t && CLOSE_WORDS.includes(t)) return b;
    if (/close|dismiss/i.test(cls + ' ' + attr)) return b;
  }
  return null;
}

function looksLikeDialog(el, win) {
  if (!el || el.nodeType !== 1) return false;
  if (el === el.ownerDocument.body || el === el.ownerDocument.documentElement) return false;
  for (const sel of NEVER_CLOSE) {
    try {
      if (el.matches(sel)) return false;
      if (el.querySelector(sel)) return false;
    } catch (e) { /* 忽略 */ }
  }
  const cs = win.getComputedStyle(el);
  if (cs.display === 'none' || cs.visibility === 'hidden') return false;
  const op = parseFloat(cs.opacity);
  if (!Number.isNaN(op) && op < 0.2) return false;
  if (cs.pointerEvents === 'none') return false;
  const z = parseInt(cs.zIndex, 10) || 0;
  const isOverlay = cs.position === 'fixed' || (cs.position === 'absolute' && z >= 100);
  if (!isOverlay) return false;
  let r;
  try { r = el.getBoundingClientRect(); } catch (e) { return false; }
  if (!r || r.width === 0 || r.height === 0) return false;
  if (r.width > win.innerWidth * 0.9 && r.height > win.innerHeight * 0.8) return false;
  return !!findCloseButton(el);
}

// 测试用：在指定 doc 里扫描关闭，返回 {clicked, targets}
function dismissDialogs(doc, win) {
  const candidates = [];
  doc.querySelectorAll('body *').forEach((el) => {
    let cs;
    try { cs = win.getComputedStyle(el); } catch (e) { return; }
    if (cs.position !== 'fixed' && cs.position !== 'absolute') return;
    if (cs.display === 'none' || cs.visibility === 'hidden') return;
    const z = parseInt(cs.zIndex, 10) || 0;
    if (cs.position === 'absolute' && z < 100) return;
    candidates.push({ el, z });
  });
  candidates.sort((a, b) => b.z - a.z);
  let clicked = 0;
  const targets = [];
  for (const { el } of candidates) {
    if (clicked >= 3) break;
    if (!looksLikeDialog(el, win)) continue;
    const btn = findCloseButton(el);
    if (!btn) continue;
    btn.__wouldClick = true;
    targets.push(el);
    clicked += 1;
  }
  return { clicked, targets };
}

// ---------------------------------------------------------------------------
// 运行
// ---------------------------------------------------------------------------
function main() {
  const dom = new JSDOM(HTML);
  const doc = dom.window.document;
  const fails = [];
  const check = (name, cond, got) => {
    if (cond) { console.log(`  [OK]   ${name}`); }
    else { console.log(`  [FAIL] ${name}  实际=${JSON.stringify(got)}`); fails.push(name); }
  };

  console.log('DOM 解析逻辑测试');
  console.log('='.repeat(60));

  console.log('\n[1] 价格提取');
  const prices = readPrices(doc);
  check('7 个货币对全部解析出', Object.keys(prices).length === 7, Object.keys(prices).length);
  check('EUR/USD = 0.99026', prices['EUR/USD'] === 0.99026, prices['EUR/USD']);
  check('GBP/USD = 1.20630', prices['GBP/USD'] === 1.20630, prices['GBP/USD']);
  check('USD/JPY = 152.583', prices['USD/JPY'] === 152.583, prices['USD/JPY']);
  check('AUD/USD = 0.66660', prices['AUD/USD'] === 0.66660, prices['AUD/USD']);
  check('没有把涨跌幅误当成价格', !Object.values(prices).some((v) => v < 0.5 && v > 0.1 && v !== 0.66660), prices);

  console.log('\n[2] 账户金额');
  const money = readMoney(doc);
  check('权益 = 10000', money.equity === 10000, money.equity);

  console.log('\n[3] 持仓解析');
  const positions = readPositions(doc);
  check('解析出 1 笔持仓', positions.length === 1, positions.length);
  if (positions[0]) {
    const p = positions[0];
    check('货币对 EUR/USD', p.pair === 'EUR/USD', p.pair);
    check('方向 long', p.side === 'long', p.side);
    check('保证金 500', p.margin === 500, p.margin);
    check('开仓价 0.99026', p.entry === 0.99026, p.entry);
    check('浮动盈亏 -73.57', p.pnl === -73.57, p.pnl);
  }

  console.log('\n[4] 模式与新闻');
  check('模式识别为 sim', readMode(doc) === 'sim', readMode(doc));
  const news = readNews(doc);
  check('新闻文本非空', news.length > 0, news.slice(0, 30));
  check('新闻含关键词', news.includes('就业'), news.slice(0, 40));

  console.log('\n[5] 表单元素定位（按真实 id）');
  check('找到 #mobilePairSelect', !!doc.querySelector('#mobilePairSelect'));
  check('找到 #marginInput', !!doc.querySelector('#marginInput'));
  check('找到 #leverageRange', !!doc.querySelector('#leverageRange'));
  check('找到做多按钮', !!doc.querySelector('.trade-button.long'));
  check('找到做空按钮', !!doc.querySelector('.trade-button.short'));
  check('找到平仓按钮', !!(doc.querySelector('.position-row button.panel-x')));
  // 验证多选择器回退链：id 优先
  const sel = doc.querySelector('#mobilePairSelect') ||
    doc.querySelector('select[aria-label="选择货币对"]');
  check('多选择器回退链可用', !!sel && sel.options.length === 7);

  console.log('\n[7] 提示浮层自动关闭');

  // jsdom 不做真实布局，getBoundingClientRect 恒为 0。
  // 这里给所有 fixed/absolute 元素打一个合理的桩尺寸，
  // 使「尺寸必须明显小于视口」这一判定可用。
  function stubRects(win, w, h) {
    win.document.querySelectorAll('body *').forEach((el) => {
      const cs = win.getComputedStyle(el);
      if (cs.position !== 'fixed' && cs.position !== 'absolute') return;
      el.getBoundingClientRect = () => ({
        width: w || 300, height: h || 200, left: 100, top: 100,
        right: (w || 300) + 100, bottom: (h || 200) + 100,
      });
    });
  }

  // 7.1 典型的「确定」提示框 —— 应该被关
  {
    const d = new JSDOM(`<body>
      <div id="app"></div>
      <div class="modal-mask" style="position:fixed;z-index:999;left:40%;top:40%">
        <div class="modal-box">
          <p>平仓成功，本次盈亏 +$18.78</p>
          <button class="btn-primary">确定</button>
        </div>
      </div>
    </body>`).window;
    stubRects(d);
    const r = dismissDialogs(d.document, d);
    check('「确定」提示框被识别', r.clicked >= 1, r.clicked);
  }

  // 7.2 「×」关闭按钮 —— 应该被关
  {
    const d = new JSDOM(`<body>
      <div class="toast-container" style="position:fixed;z-index:1000;right:20px;top:20px">
        <span>订单已成交</span>
        <button class="close">×</button>
      </div>
    </body>`).window;
    stubRects(d);
    const r = dismissDialogs(d.document, d);
    check('「×」提示框被识别', r.clicked >= 1, r.clicked);
  }

  // 7.3 类名带 close 的按钮（英文界面）—— 应该被关
  {
    const d = new JSDOM(`<body>
      <div style="position:fixed;z-index:500;left:30%;top:30%">
        <span>Trade closed successfully</span>
        <button class="btn-close">Good</button>
      </div>
    </body>`).window;
    stubRects(d);
    const r = dismissDialogs(d.document, d);
    check('英文 close 类名按钮被识别', r.clicked >= 1, r.clicked);
  }

  // 7.4 绝对定位但 z-index 太低 —— 不是浮层，不该动
  {
    const d = new JSDOM(`<body>
      <div style="position:absolute;z-index:1">
        <span>普通提示</span><button>确定</button>
      </div>
    </body>`).window;
    stubRects(d);
    const r = dismissDialogs(d.document, d);
    check('低层级元素不被误关', r.clicked === 0, r.clicked);
  }

  // 7.5 隐藏的提示框 —— 不该动
  {
    const d = new JSDOM(`<body>
      <div style="position:fixed;z-index:999;display:none">
        <span>平仓成功</span><button>确定</button>
      </div>
    </body>`).window;
    stubRects(d);
    const r = dismissDialogs(d.document, d);
    check('隐藏元素不被误关', r.clicked === 0, r.clicked);
  }

  // 7.6 核心界面（含持仓行）—— 绝不误关
  {
    const d = new JSDOM(`<body>
      <div class="workspace" style="position:fixed;z-index:500;left:0;top:0">
        <div class="position-row">
          <span>EUR/USD 多 $500.00 保证金 开仓价格 0.99</span>
          <button class="panel-x">平仓</button>
        </div>
        <button>确定</button>
      </div>
    </body>`).window;
    stubRects(d);
    const r = dismissDialogs(d.document, d);
    check('含持仓行的容器不被误关', r.clicked === 0, r.clicked);
  }

  // 7.7 下单面板 —— 绝不误关
  {
    const d = new JSDOM(`<body>
      <div class="order-panel" style="position:fixed;z-index:600">
        <button class="trade-button long">做多</button>
        <button>确定</button>
      </div>
    </body>`).window;
    stubRects(d);
    const r = dismissDialogs(d.document, d);
    check('下单面板不被误关', r.clicked === 0, r.clicked);
  }

  // 7.8 整页大小的浮层 —— 不是提示框，不该动
  {
    const d = new JSDOM(`<body>
      <div style="position:fixed;z-index:100;left:0;top:0">
        <button>确定</button>
      </div>
    </body>`).window;
    stubRects(d, 1024, 768);   // 整页尺寸
    const r = dismissDialogs(d.document, d);
    check('整页容器不被误关', r.clicked === 0, r.clicked);
  }

  // 7.9 多个提示同时存在 —— 全部关闭
  {
    const d = new JSDOM(`<body>
      <div style="position:fixed;z-index:999;left:10%;top:10%">
        <span>平仓成功</span><button>确定</button>
      </div>
      <div style="position:fixed;z-index:998;left:10%;top:40%">
        <span>开仓成功</span><button>关闭</button>
      </div>
    </body>`).window;
    stubRects(d);
    const r = dismissDialogs(d.document, d);
    check('多个提示全部被识别', r.clicked === 2, r.clicked);
  }

  // -------------------------------------------------------------------
  // ★ 安全回归测试 —— 绝不能点到交易按钮
  // -------------------------------------------------------------------

  // 7.10 浮层里有「确认下单」按钮 —— 绝不能点
  {
    const d = new JSDOM(`<body>
      <div style="position:fixed;z-index:999;left:30%;top:30%">
        <p>请确认本次下单</p>
        <button class="confirm-btn">确认下单</button>
      </div>
    </body>`).window;
    stubRects(d);
    const r = dismissDialogs(d.document, d);
    check('「确认下单」按钮不被误点', r.clicked === 0, r.clicked);
  }

  // 7.11 浮层里「confirm」类名按钮（旧版会误点）—— 必须不点
  {
    const d = new JSDOM(`<body>
      <div style="position:fixed;z-index:999;left:30%;top:30%">
        <p>交易确认</p>
        <button class="confirm">确认</button>
      </div>
    </body>`).window;
    stubRects(d);
    const r = dismissDialogs(d.document, d);
    check('confirm 类名按钮不被误点', r.clicked === 0, r.clicked);
  }

  // 7.12 浮层里同时有危险按钮和关闭按钮 —— 只点关闭
  {
    const d = new JSDOM(`<body>
      <div style="position:fixed;z-index:999;left:30%;top:30%">
        <p>订单确认</p>
        <button class="btn-submit">提交订单</button>
        <button class="btn-close">关闭</button>
      </div>
    </body>`).window;
    stubRects(d);
    const submit = d.document.querySelector('.btn-submit');
    let submitClicked = false;
    submit.addEventListener('click', () => { submitClicked = true; });
    const r = dismissDialogs(d.document, d);
    check('有危险按钮时只点关闭',
      r.clicked === 1 && !submitClicked, { clicked: r.clicked, submitClicked });
  }

  // 7.13 文案模糊匹配陷阱：「确认平仓」不在精确列表里，不点
  {
    const d = new JSDOM(`<body>
      <div style="position:fixed;z-index:999;left:30%;top:30%">
        <button>确认平仓</button>
      </div>
    </body>`).window;
    stubRects(d);
    const r = dismissDialogs(d.document, d);
    check('「确认平仓」不被误点', r.clicked === 0, r.clicked);
  }

  // 7.14 禁用的关闭按钮 —— 不点
  {
    const d = new JSDOM(`<body>
      <div style="position:fixed;z-index:999;left:30%;top:30%">
        <button disabled>确定</button>
      </div>
    </body>`).window;
    stubRects(d);
    const r = dismissDialogs(d.document, d);
    check('禁用的关闭按钮不被点', r.clicked === 0, r.clicked);
  }

  // 7.15 最多只关 3 个（防止异常页面无限点击）
  {
    const rows = Array.from({ length: 6 }, (_, i) => `
      <div style="position:fixed;z-index:${900 + i};left:5%;top:${5 + i * 10}%">
        <button>确定</button>
      </div>`).join('');
    const d = new JSDOM(`<body>${rows}</body>`).window;
    stubRects(d);
    const r = dismissDialogs(d.document, d);
    check('单轮最多关闭 3 个', r.clicked === 3, r.clicked);
  }

  console.log('\n[6] 边界情况');
  // 空页面不应抛异常
  const empty = new JSDOM('<body></body>').window.document;
  let threw = false;
  try { readPrices(empty); readPositions(empty); readMoney(empty); readNews(empty); }
  catch (e) { threw = true; }
  check('空页面不抛异常', !threw);

  // 负价格（异常数据）应被过滤
  const badDom = new JSDOM(`<body><button class="pair-row">
    <span><strong>EUR/USD</strong><small>x</small></span>
    <span><strong>-1.5</strong><small>y</small></span></button></body>`).window.document;
  check('非法规格被过滤', Object.keys(readPrices(badDom)).length === 0);

  console.log('\n' + '='.repeat(60));
  if (fails.length) {
    console.log(`失败 ${fails.length} 项：${fails.join(', ')}`);
    return 1;
  }
  console.log('全部通过');
  return 0;
}

process.exit(main());
