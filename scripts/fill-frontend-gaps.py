#!/usr/bin/env python3
"""补齐前端缺失的静态资源。

第一轮 import-frontend.py 只看保留页面的 <script>/<link>，会漏掉三类：
  1. /public/ 下的图片（AIIcon.png、icon-192.png、小组件/录音.png）
  2. 被 SKIP_PATTERNS 挡掉、但页面仍在引用的 js（chatroom-markdown.js 等）
  3. JS 里动态加载的资源（loadScript('xxx') / '=/static/xxx'）

这个脚本做两件事：
  * 静态：扫描所有 HTML 的 src/href
  * 动态：正则扫 JS 里出现的 /static/ 与 /public/ 字符串
然后只下载本地缺的那些。

用法：
    py scripts/fill-frontend-gaps.py --dry-run
    py scripts/fill-frontend-gaps.py
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = "death34018-hue/AionsHome"
BRANCH = "main"
SRC_PREFIX = "aion-chat/static"
PUBLIC_PREFIX = "public"
ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "app" / "static"

# 这些体积大又没被页面引用，明确不补
NEVER = re.compile(r"(three\.min\.js|\.mp4$|\.webm$|uploads/)")

STATIC_REF = re.compile(r"""["'](/static/[^"'\s?#]+)""")
PUBLIC_REF = re.compile(r"""["'](/public/[^"'\s?#]+)""")
HTML_REF = re.compile(r"""(?:src|href)=["'](/(?:static|public|manifest)[^"'\s?#]*)["']""")


def fetch(url: str, binary: bool = False):
    # 中文文件名（如 /public/小组件/录音.png）必须百分号编码，
    # 否则 urllib 会抛 UnicodeEncodeError。
    url = urllib.parse.quote(url, safe=":/?&=#%[]@!$'()*+,;")
    req = urllib.request.Request(url, headers={"User-Agent": "aion-import"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    return data if binary else data.decode("utf-8", "replace")


def repo_tree() -> list[str]:
    url = f"https://api.github.com/repos/{REPO}/git/trees/{BRANCH}?recursive=1"
    data = json.loads(fetch(url))
    return [item["path"] for item in data.get("tree", []) if item["type"] == "blob"]


def local_path_for(ref: str) -> Path | None:
    """把 /static/x 或 /public/x 映射到 app/static 下的位置。"""
    ref = ref.split("?", 1)[0].split("#", 1)[0]
    if ref.startswith("/static/"):
        return DEST / ref[len("/static/"):]
    if ref.startswith("/public/"):
        return DEST / "public" / ref[len("/public/"):]
    if ref.startswith("/manifest.json") or ref.startswith("/sw.js"):
        return DEST / ref.lstrip("/")
    return None


def remote_path_for(ref: str) -> str | None:
    """把引用路径映射到 AionsHome 仓库里的真实路径。

    实测的目录布局（容易猜错，这里写清楚）：
        /static/x      → aion-chat/static/x
        /public/x      → public/x            ← 注意：在**仓库根目录**，不在 aion-chat 下
        /manifest.json → aion-chat/static/manifest.json
    """
    ref = ref.split("?", 1)[0].split("#", 1)[0]
    if ref.startswith("/static/"):
        return f"{SRC_PREFIX}/{ref[len('/static/'):]}"
    if ref.startswith("/public/"):
        return f"{PUBLIC_PREFIX}/{ref[len('/public/'):]}"
    if ref in ("/manifest.json", "/sw.js"):
        return f"{SRC_PREFIX}{ref}"
    return None


def collect_refs() -> dict[str, set[str]]:
    """返回 {引用路径: {出现在哪些文件}}"""
    refs: dict[str, set[str]] = {}

    def add(ref: str, source: str) -> None:
        refs.setdefault(ref, set()).add(source)

    for path in sorted(DEST.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(DEST).as_posix()
        if path.suffix in (".html", ".htm"):
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for match in HTML_REF.finditer(text):
                add(match.group(1), rel)
        elif path.suffix == ".js":
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for pattern in (STATIC_REF, PUBLIC_REF):
                for match in pattern.finditer(text):
                    add(match.group(1), rel)
    return refs


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    refs = collect_refs()
    print(f"扫描本地前端，发现 {len(refs)} 个不同引用")

    missing = {ref: src for ref, src in refs.items() if not (local_path_for(ref) or Path()).exists()
               or not local_path_for(ref).exists()}
    print(f"其中本地缺失 {len(missing)} 个\n")

    if not missing:
        print("没有缺失，无需补。")
        return 0

    print("读取远端清单以确认可下载…")
    tree = set(repo_tree())
    downloadable: list[tuple[str, str]] = []
    unavailable: list[tuple[str, set[str]]] = []
    for ref, sources in sorted(missing.items()):
        if NEVER.search(ref):
            continue
        remote = remote_path_for(ref)
        if remote and remote in tree:
            downloadable.append((ref, remote))
        else:
            unavailable.append((ref, sources))

    print(f"\n可下载 {len(downloadable)} 个：")
    for ref, remote in downloadable:
        print(f"  {ref}   ← {remote}")

    if unavailable:
        print(f"\n远端也没有（页面引用但 AionsHome 里不存在）{len(unavailable)} 个：")
        for ref, sources in unavailable:
            print(f"  {ref}   （被 {', '.join(sorted(sources))[:60]} 引用）")

    if args.dry_run:
        print("\n--dry-run：未下载。")
        return 0

    raw_base = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/"
    ok = 0
    for ref, remote in downloadable:
        target = local_path_for(ref)
        if target is None:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            target.write_bytes(fetch(raw_base + remote, binary=True))
            ok += 1
            print(f"  [{ok}/{len(downloadable)}] {ref}", end="\r")
        except Exception as exc:  # noqa: BLE001
            print(f"\n  [失败] {ref}: {type(exc).__name__}")
    print(f"\n补齐 {ok} 个文件")

    # 更新 manifest
    manifest_path = DEST / "_import-manifest.json"
    if manifest_path.exists():
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        data.setdefault("gap_filled", []) 
        for ref, remote in downloadable:
            data["gap_filled"].append({"ref": ref, "remote": remote})
        data["unavailable"] = [{"ref": r, "used_by": sorted(s)} for r, s in unavailable]
        manifest_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    if unavailable:
        print("\n注意：下面的引用在页面上存在但远端没有，浏览器会 404。")
        print("这些多半是被砍掉的功能（taobao / 语音通话 / 串门）的残留引用。")
        for ref, _ in unavailable[:20]:
            print(f"  {ref}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
