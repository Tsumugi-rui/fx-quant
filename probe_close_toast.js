// =====================================================================
// 平仓提示探测脚本（临时诊断用）
//
// 用法：
//   1. 打开游戏页面，按 F12 → Console
//   2. 若 Console 顶部提示「不允许粘贴」，先输入 allow pasting 回车
//   3. 整个文件粘贴进去回车
//   4. 然后【手动点一次平仓按钮】
//   5. 脚本会自动抓出新出现的浮层，并把结构打印到 Console
//   6. 把打印结果截图发我
//
// 原理：MutationObserver 监听 body 新增节点，对比平仓前后，
//       任何新出现的、position:fixed/absolute 的浮层都会被记录。
// =====================================================================
(function () {
  'use strict';

  // 定位游戏所在 document（游戏可能在 iframe 里）
  var doc = document;
  if (!doc.querySelector('.position-row')) {
    for (var i = 0; i < window.frames.length; i++) {
      try {
        var d = window.frames[i].document;
        if (d && d.querySelector('.position-row')) { doc = d; break; }
      } catch (e) { /* 跨域跳过 */ }
    }
  }
  if (!doc.querySelector('.position-row')) {
    console.warn('[probe] 当前 document 找不到 .position-row，请确认在游戏 iframe 内运行');
  }

  console.log('%c[probe] 已开始监听平仓提示', 'color:#4ade80;font-weight:bold');

  // ---- 先扫一遍「已存在但可能隐藏」的候选容器 ----
  // 很多框架的 toast 容器是常驻 DOM、靠 display/opacity 显隐的。
  var CANDIDATE_SEL = [
    '[class*="toast"]', '[class*="Toast"]',
    '[class*="message"]', '[class*="Message"]',
    '[class*="notify"]', '[class*="Notify"]',
    '[class*="tip"]', '[class*="Tip"]',
    '[class*="modal"]', '[class*="Modal"]',
    '[class*="dialog"]', '[class*="Dialog"]',
    '[class*="popup"]', '[class*="Popup"]',
    '[class*="overlay"]', '[class*="Overlay"]',
    '[class*="snackbar"]', '[class*="Snackbar"]',
    '[role="alert"]', '[role="dialog"]', '[aria-live]',
  ];
  console.log('[probe] ── 阶段 1：扫描现有候选容器 ──');
  var foundContainer = false;
  CANDIDATE_SEL.forEach(function (sel) {
    doc.querySelectorAll(sel).forEach(function (el) {
      foundContainer = true;
      var cs = doc.defaultView.getComputedStyle(el);
      console.log('%c[probe] 命中容器 ' + sel, 'color:#60a5fa', {
        display: cs.display,
        visibility: cs.visibility,
        opacity: cs.opacity,
        position: cs.position,
        zIndex: cs.zIndex,
        text: (el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 60),
        outerHTML: el.outerHTML.slice(0, 300),
      });
    });
  });
  if (!foundContainer) {
    console.log('[probe] 未发现常见 toast/modal 容器（可能在平仓时才动态创建）');
  }

  console.log('%c[probe] ── 阶段 2：现在请【手动点一次平仓按钮】… ──',
    'color:#fbbf24;font-weight:bold');

  var snapshot = new Set();
  doc.querySelectorAll('*').forEach(function (el) { snapshot.add(el); });

  function describe(el) {
    var cls = String(el.className || '').slice(0, 120);
    var id = el.id ? '#' + el.id : '';
    var txt = (el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 80);
    return {
      tag: el.tagName.toLowerCase(),
      id: id,
      cls: cls,
      text: txt,
      html: el.outerHTML.slice(0, 400),
    };
  }

  var reported = new Set();

  var obs = new MutationObserver(function (muts) {
    muts.forEach(function (m) {
      m.addedNodes.forEach(function (n) {
        if (n.nodeType !== 1) return;
        // 只关心看起来像浮层的元素
        var cs = doc.defaultView.getComputedStyle(n);
        var pos = cs.position;
        var z = parseInt(cs.zIndex, 10) || 0;
        var isOverlay = pos === 'fixed' || (pos === 'absolute' && z >= 10);
        var txt = (n.textContent || '').trim();

        if (!txt) return;
        if (reported.has(n)) return;
        reported.add(n);

        if (isOverlay || /平仓|成交|盈亏|成功|确认|关闭/.test(txt)) {
          console.log('%c[probe] 检测到浮层/提示 ★', 'color:#fbbf24;font-weight:bold',
            { position: pos, zIndex: z });
          console.log(JSON.stringify(describe(n), null, 2));

          // 顺带打印它内部所有可点击元素（关闭按钮线索）
          var btns = n.querySelectorAll('button, [role="button"], a, span[class*="close"], [class*="close"]');
          if (btns.length) {
            console.log('[probe] 内部可点击元素：');
            btns.forEach(function (b, i) {
              console.log('  [' + i + ']', describe(b));
            });
          }

          // 也扫一下同类名兄弟元素，方便确认识别选择器
          var parent = n.parentElement;
          if (parent) {
            console.log('[probe] 父节点：', describe(parent));
          }
        }
      });
    });
  });

  obs.observe(doc.body, { childList: true, subtree: true });

  // 兜底：3 秒后无结果也给个提示
  setTimeout(function () {
    console.log('%c[probe] 监听中…如果已平仓但没打印任何东西，'
      + '说明提示不是新增节点（可能是复用已有元素显隐），'
      + '请把这条消息截图给我。', 'color:#94a3b8');
  }, 3000);

  window.__fxProbeStop = function () {
    obs.disconnect();
    console.log('[probe] 已停止监听');
  };
})();
