/* Harlan 前端错误收集器 —— 必须最先加载
 *
 * 为什么要它：前端初始化外面包着
 *     return init().catch(e => console.warn('[chat] initialization failed:', e));
 * 出错只写进 Console 的**警告**里，界面上完全看不出来 ——
 * 表现就是"页面能打开，但点发送没反应"。
 *
 * 而服务端验证不了 JS 运行时（只能测资源是否 200），所以让页面
 * 把错误回传到 /api/debug/frontend-error，开发者用
 *     GET /api/debug/frontend-errors
 * 就能读到，不需要用户截图 Console。
 *
 * 设计约束：
 *   - 不能抛错（本身出错会引发递归，所以整体 try 包裹 + 一次性标记）
 *   - 不能阻塞页面（用 sendBeacon / fetch keepalive，不 await）
 *   - 要能捕获**脚本加载失败**（用 window.addEventListener('error', ..., true)
 *     才能在捕获阶段拿到资源错误；window.onerror 拿不到）
 */
(function () {
  'use strict';
  if (window.__harlanErrorReporterInstalled) return;
  window.__harlanErrorReporterInstalled = true;

  var sent = 0;
  var LIMIT = 20;                       // 防止某个错误疯狂刷屏

  function report(payload) {
    if (sent >= LIMIT) return;
    sent += 1;
    try {
      payload.url = String(location.href || '');
      var body = JSON.stringify(payload);
      if (navigator.sendBeacon) {
        navigator.sendBeacon('/api/debug/frontend-error',
                             new Blob([body], { type: 'application/json' }));
      } else {
        fetch('/api/debug/frontend-error', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: body,
          keepalive: true
        }).catch(function () {});
      }
      // 顺带在页面角上显示一个小提示，让用户知道出了问题（可选，失败也不影响）
      try { showBadge(payload.message); } catch (e) {}
    } catch (e) { /* 收集器自身绝不能抛 */ }
  }

  function showBadge(message) {
    if (document.getElementById('__harlan_err_badge')) return;
    var el = document.createElement('div');
    el.id = '__harlan_err_badge';
    el.style.cssText = 'position:fixed;left:8px;bottom:8px;z-index:99999;' +
      'max-width:70vw;padding:6px 10px;border-radius:8px;font-size:12px;' +
      'background:rgba(200,40,40,.92);color:#fff;font-family:monospace;' +
      'white-space:pre-wrap;word-break:break-all;opacity:.92';
    el.textContent = '前端报错：' + String(message).slice(0, 160) +
      '\n（已记录，可在 /api/debug/frontend-errors 查看）';
    el.title = '点击复制到剪贴板';
    el.onclick = function () {
      try { navigator.clipboard.writeText(String(message)); } catch (e) {}
    };
    (document.body || document.documentElement).appendChild(el);
  }

  // 1. 脚本/资源加载失败：必须用捕获阶段才能在 window 上拿到
  window.addEventListener('error', function (event) {
    var target = event.target;
    if (target && target !== window && (target.src || target.href)) {
      report({
        kind: 'resource',
        message: '资源加载失败: ' + (target.src || target.href),
        source: String(target.src || target.href || ''),
        stack: '<' + (target.tagName || '?') + '>'
      });
      return;
    }
    report({
      kind: 'error',
      message: event.message || String(event.error || 'unknown error'),
      source: String(event.filename || ''),
      lineno: event.lineno || 0,
      colno: event.colno || 0,
      stack: event.error && event.error.stack ? String(event.error.stack) : ''
    });
  }, true);

  // 2. 未处理的 Promise 拒绝（async 函数里 throw 会走到这里）
  window.addEventListener('unhandledrejection', function (event) {
    var reason = event.reason;
    report({
      kind: 'unhandledrejection',
      message: reason && reason.message ? reason.message : String(reason),
      stack: reason && reason.stack ? String(reason.stack) : '',
      source: 'promise'
    });
  });

  // 3. 把 console.error / console.warn 也记下来
  //    因为 init() 的错误是被 .catch(e => console.warn(...)) 吞掉的，
  //    不拦 console 就看不到它。
  ['error', 'warn'].forEach(function (level) {
    var original = console[level];
    if (typeof original !== 'function') return;
    console[level] = function () {
      try {
        var parts = Array.prototype.map.call(arguments, function (a) {
          if (a instanceof Error) return a.message + (a.stack ? '\n' + a.stack : '');
          if (typeof a === 'object') { try { return JSON.stringify(a); } catch (e) { return String(a); } }
          return String(a);
        });
        var text = parts.join(' ');
        // 只回传看起来像"初始化失败"的，避免噪音
        if (/initialization failed|is not defined|Cannot read|undefined is not|TypeError|ReferenceError/i.test(text)) {
          report({ kind: 'console.' + level, message: text.slice(0, 600), source: 'console' });
        }
      } catch (e) {}
      return original.apply(console, arguments);
    };
  });
})();
