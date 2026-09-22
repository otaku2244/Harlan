#!/usr/bin/env python3
"""按依赖清单从 AionsHome 抽取前端到 app/static/。

为什么不 git clone / 下 zip：
  整个仓库含 vendor/*.whl（几十 MB，且全是 Windows 专用包）和娱乐模块，
  下载又慢又没用。这里只取真正被用到的文件。

要处理的三类引用（实测形状）：
    /static/chat.js?v=ai-edit-20260920   → app/static/chat.js      （要剥查询串）
    /manifest.json                        → app/static/manifest.json（在 static/ 里）
    /public/icon-192.png                  → app/static/public/icon-192.png

用法：
    py scripts/import-frontend.py --dry-run
    py scripts/import-frontend.py
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = "death34018-hue/AionsHome"
BRANCH = "main"
SRC_PREFIX = "aion-chat/static"
PUBLIC_PREFIX = "public"
ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "app" / "static"

# 要移植的页面（v0.3 §7 清单，砍掉娱乐与未实现端侧功能）
KEEP_PAGES = [
    "chat.html", "home.html", "settings.html", "worldbook.html",
    "memory.html", "diary.html", "moments.html", "schedule.html",
    "location.html", "monitor-logs.html", "camera.html", "activity-logs.html",
]

# 明确不要的：娱乐模块 / 未实现的端侧功能 / 明显无关
SKIP_PATTERNS = [
    r"^doudizhu", r"^theater", r"^ghost-forest", r"^gift", r"^fund",
    r"^reading", r"^wallpaper", r"^playground", r"^music", r"^pet",
    r"^hug", r"^lounge", r"^chatroom", r"^english-corner", r"^heart-whispers",
    r"^security-access", r"^app-supervision", r"^capabilities\.html",
    r"^family-dynamics", r"^memory-compression", r"^date_theater",
    r"^taobao", r"^checkin", r"^wallet", r"^wishes", r"^seeky",
    r"^band", r"^miband", r"^ring", r"^infrared",
    r"three\.min\.js", r"video-call", r"voice-call", r"hug_pillow",
]

# 用户上传的媒体（体积大、且是别人的私人素材）
SKIP_DIRS = ("uploads/", "niche-assets/")


def fetch(url: str, binary: bool = False):
    req = urllib.request.Request(url, headers={"User-Agent": "aion-import"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    return data if binary else data.decode("utf-8", "replace")


def repo_tree() -> list[str]:
    url = f"https://api.github.com/repos/{REPO}/git/trees/{BRANCH}?recursive=1"
    data = json.loads(fetch(url))
    if data.get("truncated"):
        print("  ⚠ GitHub 返回的 tree 被截断，可能漏文件")
    return [item["path"] for item in data.get("tree", []) if item["type"] == "blob"]


def normalize_ref(ref: str) -> tuple[str, str] | None:
    """把 HTML 里的引用规整成 (来源前缀, 相对名)。非本地资源返回 None。

    '/static/chat.js?v=x'  → ('aion-chat/static', 'chat.js')
    '/manifest.json'        → ('aion-chat/static', 'manifest.json')
    '/public/icon-192.png'  → ('public', 'icon-192.png')
    'https://...'           → None
    """
    ref = ref.strip()
    if not ref or ref.startswith(("http://", "https://", "//", "data:", "#")):
        return None
    ref = ref.split("?", 1)[0].split("#", 1)[0]
    if ref.startswith("/static/"):
        return SRC_PREFIX, ref[len("/static/"):]
    if ref.startswith("/public/"):
        # 前端按 /public/xxx 引用，但 AionsHome 前端目录里是 static/public/xxx
        return f"{SRC_PREFIX}/public", ref[len("/public/"):]
    if ref.startswith("/"):
        return SRC_PREFIX, ref.lstrip("/")
    return SRC_PREFIX, ref


def local_refs(html: str) -> set[tuple[str, str]]:
    refs: set[tuple[str, str]] = set()
    for pattern in (r'<script[^>]+src=["\']([^"\']+)["\']',
                    r'<link[^>]+href=["\']([^"\']+)["\']'):
        for match in re.finditer(pattern, html, re.IGNORECASE):
            parsed = normalize_ref(match.group(1))
            if parsed:
                refs.add(parsed)
    return refs


def wanted(name: str) -> bool:
    if any(name.startswith(d) for d in SKIP_DIRS):
        return False
    return not any(re.search(p, name) for p in SKIP_PATTERNS)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    print(f"读取 {REPO} 文件清单…")
    try:
        tree = repo_tree()
    except urllib.error.HTTPError as exc:
        print(f"  GitHub API 失败 HTTP {exc.code}（频率限制？稍后重试）")
        return 1

    raw_base = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/"
    # 远端可用的相对名 → 完整路径
    available: dict[tuple[str, str], str] = {}
    for path in tree:
        if path.startswith(SRC_PREFIX + "/"):
            available[(SRC_PREFIX, path[len(SRC_PREFIX) + 1:])] = path
        elif path.startswith(PUBLIC_PREFIX + "/"):
            available[(PUBLIC_PREFIX, path[len(PUBLIC_PREFIX) + 1:])] = path
    static_count = sum(1 for k in available if k[0] == SRC_PREFIX)
    print(f"  远端 static/ {static_count} 个，public/ {len(available) - static_count} 个")

    # ── 从保留页面解析依赖 ──────────────────────────
    wanted_files: set[tuple[str, str]] = set()
    for page in KEEP_PAGES:
        key = (SRC_PREFIX, page)
        if key not in available:
            print(f"  ⚠ 远端没有 {page}，跳过")
            continue
        try:
            html = fetch(raw_base + available[key])
        except Exception as exc:  # noqa: BLE001
            print(f"  ⚠ 读取 {page} 失败: {type(exc).__name__}")
            continue
        refs = local_refs(html)
        wanted_files.add(key)
        wanted_files |= refs
        print(f"  {page}: {len(refs)} 个依赖")

    # ── 过滤 ────────────────────────────────────────
    targets: list[tuple[str, str]] = []
    missing: list[tuple[str, str]] = []
    for key in sorted(wanted_files):
        prefix, name = key
        if not wanted(name):
            continue
        if key in available:
            targets.append(key)
        else:
            missing.append(key)

    total_bytes = sum(
        int(re.search(r'"size":\s*(\d+)', "{}").group(1)) if False else 0 for _ in targets
    )
    print(f"\n计划移植 {len(targets)} 个文件")
    # 按目录分组显示，便于核对
    by_dir: dict[str, list[str]] = {}
    for prefix, name in targets:
        group = name.rsplit("/", 1)[0] if "/" in name else "(根)"
        by_dir.setdefault(group, []).append(name.rsplit("/", 1)[-1])
    for group in sorted(by_dir):
        names = sorted(by_dir[group])
        shown = ", ".join(names[:10]) + (f" … 共 {len(names)} 个" if len(names) > 10 else "")
        print(f"  {group}/  {shown}")

    if missing:
        print(f"\n以下依赖在远端找不到（引用时写错或已删除）：{len(missing)} 个")
        for _, name in missing[:15]:
            print(f"  {name}")

    if args.dry_run:
        print("\n--dry-run：未下载。")
        return 0

    # ── 下载 ────────────────────────────────────────
    DEST.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, dict] = {}
    total = 0
    for index, (prefix, name) in enumerate(targets, 1):
        target = DEST / name
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            payload = fetch(raw_base + available[(prefix, name)], binary=True)
        except Exception as exc:  # noqa: BLE001
            print(f"\n  [跳过] {name}: {type(exc).__name__}")
            continue
        target.write_bytes(payload)
        total += len(payload)
        manifest[name] = {
            "bytes": len(payload),
            "remote": available[(prefix, name)],
            "used_by": [],
        }
        if index % 5 == 0 or index == len(targets):
            print(f"  {index}/{len(targets)}  {total // 1024} KB", end="\r")

    # 记录引用关系，便于以后清理孤儿文件
    for page in KEEP_PAGES:
        path = DEST / page
        if not path.exists():
            continue
        for _, name in local_refs(path.read_text(encoding="utf-8")):
            if name in manifest:
                manifest[name]["used_by"].append(page)

    (DEST / "_import-manifest.json").write_text(json.dumps({
        "source": f"https://github.com/{REPO}/tree/{BRANCH}/{SRC_PREFIX}",
        "imported_at": __import__("time").strftime("%Y-%m-%d %H:%M:%S"),
        "files": manifest,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n完成：{len(manifest)} 个文件，{total / 1024:.0f} KB → {DEST}")
    orphans = [n for n, m in manifest.items()
               if not m["used_by"] and n.endswith((".js", ".css"))]
    if orphans:
        print(f"\n未被移植页面直接引用的 js/css（{len(orphans)} 个）：")
        for name in sorted(orphans)[:25]:
            print(f"  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
