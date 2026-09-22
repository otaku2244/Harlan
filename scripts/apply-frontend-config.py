#!/usr/bin/env python3
"""把 harlan-ui.json 编译成前端用的隐藏脚本。

为什么不直接改 chat.js / chat.html：
    那些文件是 AionsHome 原版的（chat.js 243KB），手工删按钮容易漏、也难回退。
    这里改成"数据驱动 + 运行时隐藏"：
      * 想恢复某个功能 -> 改 JSON 里一个 true，刷新即可
      * 不改动原版文件，将来重新同步上游也不会冲突
      * 隐藏只是不给入口，底层代码仍在，所以不会因为删错而崩

产物：app/static/harlan-ui-config.js
用法：
    py scripts/apply-frontend-config.py [--check]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "harlan-ui.json"
OUTPUT = ROOT / "app" / "static" / "harlan-ui-config.js"


def build_js(cfg: dict) -> str:
    payload = {
        "sidebar": {k: v for k, v in cfg.get("sidebar", {}).items() if not k.startswith("_")},
        "apps": {k: v for k, v in cfg.get("apps", {}).items() if not k.startswith("_")},
        "plusMenu": {k: v for k, v in cfg.get("plusMenu", {}).items() if not k.startswith("_")},
    }
    hidden_counts = {name: sum(1 for v in section.values() if v is False)
                     for name, section in payload.items()}
    return f"""/* Harlan 前端可见性配置（自动生成，不要手改）
 *
 * 源文件: harlan-ui.json
 * 重新生成: py scripts/apply-frontend-config.py
 *
 * 隐藏统计: {json.dumps(hidden_counts, ensure_ascii=False)}
 *
 * 为什么不改原版 chat.js：它是 AionsHome 的 243KB 单文件，
 * 手工删按钮容易漏且难回退。这里只做"不给入口"，底层代码不动，
 * 所以不会因为删错而崩，想恢复改 JSON 即可。
 */
(function () {{
  'use strict';
  var CONFIG = {json.dumps(payload, ensure_ascii=False, indent=2)};

  function hidden(map, key) {{ return map && map[key] === false; }}

  /* 1. 按 onclick 属性精确隐藏侧边栏按钮 */
  function hideSidebar() {{
    document.querySelectorAll('[onclick]').forEach(function (el) {{
      var code = el.getAttribute('onclick') || '';
      Object.keys(CONFIG.sidebar).forEach(function (pattern) {{
        if (CONFIG.sidebar[pattern] !== false) return;
        if (code.indexOf(pattern.replace(/\\(\\)$/, '')) !== -1) {{
          el.style.display = 'none';
          el.setAttribute('data-harlan-hidden', 'sidebar');
        }}
      }});
    }});
  }}

  /* 2. 主页网格：给被隐藏的应用打标记，CSS 负责不显示 */
  function markApps() {{
    var hiddenIds = Object.keys(CONFIG.apps).filter(function (k) {{
      return CONFIG.apps[k] === false;
    }});
    if (!hiddenIds.length) return;
    window.__harlanHiddenApps = hiddenIds;
    document.querySelectorAll('[data-app-id]').forEach(function (el) {{
      if (hiddenIds.indexOf(el.getAttribute('data-app-id')) !== -1) {{
        el.style.display = 'none';
      }}
    }});
  }}

  /* 3. ＋ 菜单：按文案子串隐藏 */
  function hidePlusMenu() {{
    var words = Object.keys(CONFIG.plusMenu).filter(function (k) {{
      return CONFIG.plusMenu[k] === false;
    }});
    if (!words.length) return;
    document.querySelectorAll('#plusMenu *').forEach(function (el) {{
      if (el.children.length) return;                 // 只看叶子节点
      var text = (el.textContent || '').trim();
      if (!text) return;
      for (var i = 0; i < words.length; i++) {{
        if (text.indexOf(words[i]) !== -1) {{
          var row = el.closest('button,li,a,div[onclick]') || el;
          row.style.display = 'none';
          row.setAttribute('data-harlan-hidden', 'plus-menu');
          return;
        }}
      }}
    }});
  }}

  /* 4. 主页追加样式：如果 APPS 渲染出的格子没有 data-app-id，
        退化成"按文案隐藏"，匹配 name 字段 */
  function hideAppsByLabel() {{
    var labels = Object.keys(CONFIG.apps).filter(function (k) {{
      return CONFIG.apps[k] === false;
    }});
    if (!labels.length) return;
    document.querySelectorAll('.app-item,.app-grid > *,.grid-item').forEach(function (el) {{
      var text = (el.textContent || '').trim();
      if (!text) return;
      var name = text.split('\\n')[0].trim();
      if (name && labels.indexOf(name) !== -1) el.style.display = 'none';
    }});
  }}

  function applyAll() {{
    try {{ hideSidebar(); }} catch (e) {{}}
    try {{ markApps(); }} catch (e) {{}}
    try {{ hideAppsByLabel(); }} catch (e) {{}}
    try {{ hidePlusMenu(); }} catch (e) {{}}
  }}

  if (document.readyState === 'loading') {{
    document.addEventListener('DOMContentLoaded', applyAll);
  }} else {{
    applyAll();
  }}
  /* 侧边栏与网格是动态渲染的，延迟再跑几次 */
  [300, 1000, 2500].forEach(function (ms) {{ setTimeout(applyAll, ms); }});

  window.__harlanUI = {{
    config: CONFIG,
    apply: applyAll,
    isHiddenApp: function (id) {{ return hidden(CONFIG.apps, id); }}
  }};
}})();
"""


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true",
                        help="只检查配置，不写文件")
    args = parser.parse_args()

    if not CONFIG.exists():
        print(f"找不到配置文件：{CONFIG}")
        return 1

    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    js = build_js(cfg)

    for section in ("apps", "sidebar", "plusMenu"):
        data = {k: v for k, v in cfg.get(section, {}).items() if not k.startswith("_")}
        shown = [k for k, v in data.items() if v]
        hidden = [k for k, v in data.items() if not v]
        print(f"[{section}]  显示 {len(shown)} 个，隐藏 {len(hidden)} 个")
        if shown:
            print(f"   显示: {', '.join(shown[:12])}"
                  + (" …" if len(shown) > 12 else ""))

    if args.check:
        print("\n--check：未写文件。")
        return 0

    OUTPUT.write_text(js, encoding="utf-8")
    print(f"\n已写出 {OUTPUT.relative_to(ROOT)}  ({len(js)} 字节)")
    print("刷新浏览器即可生效（Ctrl+Shift+R 强制刷新）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
