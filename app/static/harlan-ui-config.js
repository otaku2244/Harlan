/* Harlan 前端可见性配置（自动生成，不要手改）
 *
 * 源文件: harlan-ui.json
 * 重新生成: py scripts/apply-frontend-config.py
 *
 * 隐藏统计: {"sidebar": 5, "apps": 22, "plusMenu": 10}
 *
 * 为什么不改原版 chat.js：它是 AionsHome 的 243KB 单文件，
 * 手工删按钮容易漏且难回退。这里只做"不给入口"，底层代码不动，
 * 所以不会因为删错而崩，想恢复改 JSON 即可。
 */
(function () {
  'use strict';
  var CONFIG = {
  "sidebar": {
    "+ 新对话": true,
    "openSubPage('/settings')": true,
    "openSubPage('/diary')": true,
    "openSubPage('/')": true,
    "openWalletPanel()": false,
    "openSubPage('/toys')": false,
    "openStarredPanel()": false,
    "openSystemLog()": false,
    "openFileManager()": false
  },
  "apps": {
    "chat": true,
    "memory": true,
    "diary": true,
    "worldbook": true,
    "settings": true,
    "schedule": true,
    "moments": true,
    "chatroom": false,
    "theater": false,
    "dateTheater": false,
    "ghost-forest": false,
    "gift": false,
    "fund": false,
    "reading": false,
    "english-corner": false,
    "music-station": false,
    "album": false,
    "taobao": false,
    "xhs-lite": false,
    "lounge-friends": false,
    "doudizhu": false,
    "wishes": false,
    "whisper": false,
    "capabilities": false,
    "playground": false,
    "seeky": false,
    "wallpaper": false,
    "pet": false,
    "hug": false
  },
  "plusMenu": {
    "密语": false,
    "礼物": false,
    "钱包": false,
    "转账": false,
    "许愿": false,
    "相册": false,
    "音乐": false,
    "点歌": false,
    "视频通话": false,
    "系统日志": false
  }
};

  function hidden(map, key) { return map && map[key] === false; }

  /* 1. 按 onclick 属性精确隐藏侧边栏按钮 */
  function hideSidebar() {
    document.querySelectorAll('[onclick]').forEach(function (el) {
      var code = el.getAttribute('onclick') || '';
      Object.keys(CONFIG.sidebar).forEach(function (pattern) {
        if (CONFIG.sidebar[pattern] !== false) return;
        if (code.indexOf(pattern.replace(/\(\)$/, '')) !== -1) {
          el.style.display = 'none';
          el.setAttribute('data-harlan-hidden', 'sidebar');
        }
      });
    });
  }

  /* 2. 主页网格：给被隐藏的应用打标记，CSS 负责不显示 */
  function markApps() {
    var hiddenIds = Object.keys(CONFIG.apps).filter(function (k) {
      return CONFIG.apps[k] === false;
    });
    if (!hiddenIds.length) return;
    window.__harlanHiddenApps = hiddenIds;
    document.querySelectorAll('[data-app-id]').forEach(function (el) {
      if (hiddenIds.indexOf(el.getAttribute('data-app-id')) !== -1) {
        el.style.display = 'none';
      }
    });
  }

  /* 3. ＋ 菜单：按文案子串隐藏 */
  function hidePlusMenu() {
    var words = Object.keys(CONFIG.plusMenu).filter(function (k) {
      return CONFIG.plusMenu[k] === false;
    });
    if (!words.length) return;
    document.querySelectorAll('#plusMenu *').forEach(function (el) {
      if (el.children.length) return;                 // 只看叶子节点
      var text = (el.textContent || '').trim();
      if (!text) return;
      for (var i = 0; i < words.length; i++) {
        if (text.indexOf(words[i]) !== -1) {
          var row = el.closest('button,li,a,div[onclick]') || el;
          row.style.display = 'none';
          row.setAttribute('data-harlan-hidden', 'plus-menu');
          return;
        }
      }
    });
  }

  /* 4. 主页追加样式：如果 APPS 渲染出的格子没有 data-app-id，
        退化成"按文案隐藏"，匹配 name 字段 */
  function hideAppsByLabel() {
    var labels = Object.keys(CONFIG.apps).filter(function (k) {
      return CONFIG.apps[k] === false;
    });
    if (!labels.length) return;
    document.querySelectorAll('.app-item,.app-grid > *,.grid-item').forEach(function (el) {
      var text = (el.textContent || '').trim();
      if (!text) return;
      var name = text.split('\n')[0].trim();
      if (name && labels.indexOf(name) !== -1) el.style.display = 'none';
    });
  }

  function applyAll() {
    try { hideSidebar(); } catch (e) {}
    try { markApps(); } catch (e) {}
    try { hideAppsByLabel(); } catch (e) {}
    try { hidePlusMenu(); } catch (e) {}
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', applyAll);
  } else {
    applyAll();
  }
  /* 侧边栏与网格是动态渲染的，延迟再跑几次 */
  [300, 1000, 2500].forEach(function (ms) { setTimeout(applyAll, ms); });

  window.__harlanUI = {
    config: CONFIG,
    apply: applyAll,
    isHiddenApp: function (id) { return hidden(CONFIG.apps, id); }
  };
})();
