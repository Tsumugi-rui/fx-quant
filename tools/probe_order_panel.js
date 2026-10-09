// =====================================================================
// 下单面板结构探测（临时诊断用）
//
// 目的：确认为什么「找不到货币对选择框」。
//
// 用法：
//   1. 打开游戏页面，F12 → Console
//   2. 若提示「不允许粘贴」，先输入 allow pasting 回车
//   3. 粘贴本文件全部内容，回车
//   4. 把输出截图发我（尤其关注 [selects] 和 [inputs] 两段）
//
// 注意：本脚本只读 DOM，不做任何修改，不点任何按钮。
// =====================================================================
(function () {
  'use strict';

  var doc = document;
  if (!doc.querySelector('.pair-row')) {
    for (var i = 0; i < window.frames.length; i++) {
      try {
        var d = window.frames[i].document;
        if (d && d.querySelector('.pair-row')) { doc = d; break; }
      } catch (e) { /* 跨域跳过 */ }
    }
  }

  function line(s) { console.log(s); }

  line('%c===== 下单面板结构探测 =====', 'color:#4ade80;font-weight:bold');
  line('当前 document: ' + (doc === document ? '顶层' : 'iframe 内'));
  line('');

  // ---- 1. 所有 select ----
  var selects = doc.querySelectorAll('select');
  line('%c[selects] 共 ' + selects.length + ' 个', 'color:#60a5fa;font-weight:bold');
  selects.forEach(function (s, i) {
    line('  [' + i + '] id=' + JSON.stringify(s.id)
      + ' name=' + JSON.stringify(s.name)
      + ' class=' + JSON.stringify(String(s.className).slice(0, 60))
      + ' aria=' + JSON.stringify(s.getAttribute('aria-label'))
      + ' 选项数=' + s.options.length
      + ' 可见=' + (s.offsetParent !== null));
    var opts = [...s.options].slice(0, 3).map(function (o) { return o.value; });
    line('      前 3 选项: ' + JSON.stringify(opts));
    line('      outerHTML: ' + s.outerHTML.slice(0, 200));
  });

  // ---- 2. 所有 input ----
  var inputs = doc.querySelectorAll('input');
  line('%c[inputs] 共 ' + inputs.length + ' 个', 'color:#60a5fa;font-weight:bold');
  inputs.forEach(function (n, i) {
    line('  [' + i + '] type=' + JSON.stringify(n.type)
      + ' id=' + JSON.stringify(n.id)
      + ' name=' + JSON.stringify(n.name)
      + ' class=' + JSON.stringify(String(n.className).slice(0, 60))
      + ' value=' + JSON.stringify(n.value)
      + ' 可见=' + (n.offsetParent !== null));
  });

  // ---- 3. id 精确查找（脚本里用的选择器）----
  line('%c[check] 脚本选择器逐条测试', 'color:#fbbf24;font-weight:bold');
  var sels = [
    '#mobilePairSelect', 'select[aria-label="选择货币对"]',
    '.order-panel select', 'select[name="select-one"]', 'select',
    '#marginInput', 'input[name="margin"]', '.order-panel input[type="number"]',
    '#leverageRange', '#leverageInput', 'input[name="leverage"]',
    '.order-panel input[type="range"]',
  ];
  sels.forEach(function (s) {
    var hit = null;
    try { hit = doc.querySelector(s); } catch (e) { hit = null; }
    line('  ' + (hit ? '✔' : '✘') + '  ' + s
      + (hit ? '  → <' + hit.tagName.toLowerCase() + ' id=' + JSON.stringify(hit.id) + '>' : ''));
  });

  // ---- 4. 找「做多/做空」按钮，反推面板容器 ----
  line('%c[buttons] 交易按钮', 'color:#fbbf24;font-weight:bold');
  var allBtns = doc.querySelectorAll('button, [role="button"]');
  var tradeBtns = [];
  allBtns.forEach(function (b) {
    var t = (b.textContent || '').trim();
    var c = String(b.className || '');
    if (/做多|做空|买入|卖出|long|short|buy|sell/i.test(t + ' ' + c)) {
      tradeBtns.push(b);
    }
  });
  line('  候选按钮 ' + tradeBtns.length + ' 个');
  tradeBtns.slice(0, 6).forEach(function (b, i) {
    line('  [' + i + '] class=' + JSON.stringify(String(b.className).slice(0, 80))
      + ' text=' + JSON.stringify((b.textContent || '').trim().slice(0, 20))
      + ' 可见=' + (b.offsetParent !== null));
    // 向上找 3 层父节点，帮助定位面板容器
    var p = b.parentElement;
    for (var k = 0; k < 3 && p; k++) {
      line('      ↑' + (k + 1) + ' <' + p.tagName.toLowerCase()
        + ' class=' + JSON.stringify(String(p.className).slice(0, 60))
        + ' id=' + JSON.stringify(p.id) + '>');
      p = p.parentElement;
    }
  });

  // ---- 5. 面板容器搜索 ----
  line('%c[containers] 疑似下单面板容器', 'color:#fbbf24;font-weight:bold');
  ['.order-panel', '[class*="order"]', '[class*="trade"]', '[class*="panel"]']
    .forEach(function (s) {
      var els = doc.querySelectorAll(s);
      if (els.length) {
        line('  ' + s + ' → ' + els.length + ' 个');
        [...els].slice(0, 5).forEach(function (e) {
          line('     <' + e.tagName.toLowerCase()
            + ' class=' + JSON.stringify(String(e.className).slice(0, 70)) + '>'
            + ' 可见=' + (e.offsetParent !== null)
            + ' 内含select=' + e.querySelectorAll('select').length
            + ' input=' + e.querySelectorAll('input').length);
        });
      } else {
        line('  ' + s + ' → 0 个');
      }
    });

  line('');
  line('%c===== 探测结束，请截图 =====', 'color:#4ade80;font-weight:bold');
})();
