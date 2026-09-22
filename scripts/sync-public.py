#!/usr/bin/env python3
"""把 AionsHome 根目录 public/ 整体同步过来（按体积筛掉娱乐素材）。

为什么不是"按引用补"（fill-frontend-gaps.py 的方式）：
    主页的功能图标路径是在 JS 里**拼接**出来的（funIcon_00NN_xxx.png），
    静态扫描扫不到，结果 24 个图标全缺 —— 正是"图标显示失败"的原因。

所以这里反过来做：**整个 public/ 拉下来**，只按体积和目录排除。

体积分布（实测）：
    132 个文件共 96.4 MB —— 但其中 51 个 >500KB 就占了 86.2 MB，
    全是"去约会小剧场"的透明视频（.webm/.mp4）与 4~6MB 的大图。
    剩下 81 个 ≤500KB 只有 10.2 MB，却包含全部图标与音效。

所以：小文件全要，大文件与娱乐素材目录整体排除。

用法：
    py scripts/sync-public.py --dry-run
    py scripts/sync-public.py
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

REPO = "death34018-hue/AionsHome"
BRANCH = "main"
PREFIX = "public"
ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "app" / "static" / "public"

# 超过这个体积就不要（图标都远小于它；超出的基本是视频和大图）
MAX_BYTES = 500 * 1024

# 已砍掉功能的素材目录
SKIP_DIRS = ("去约会小剧场素材/", "card/")     # card 是斗地主音效

# 即使超过阈值也要（页面直接引用、缺了会明显报错）
FORCE_KEEP = {"BackGround.png", "BackGroundN.png"}


def fetch(url: str, binary: bool = False):
    url = urllib.parse.quote(url, safe=":/?&=#%[]@!$'()*+,;")
    req = urllib.request.Request(url, headers={"User-Agent": "aion-sync"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = resp.read()
    return data if binary else data.decode("utf-8", "replace")


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-kb", type=int, default=MAX_BYTES // 1024,
                        help=f"单文件体积上限（KB），默认 {MAX_BYTES // 1024}")
    parser.add_argument("--force-all", action="store_true",
                        help="忽略体积与目录限制，全部下载（约 96MB）")
    args = parser.parse_args()

    max_bytes = args.max_kb * 1024
    print(f"读取 {REPO} 的 public/ 清单…")
    data = json.loads(fetch(
        f"https://api.github.com/repos/{REPO}/git/trees/{BRANCH}?recursive=1"))
    files = [i for i in data.get("tree", [])
             if i["type"] == "blob" and i["path"].startswith(PREFIX + "/")]
    print(f"  远端 {len(files)} 个文件，"
          f"{sum(f.get('size') or 0 for f in files) / 1024 / 1024:.1f} MB")

    keep: list[dict] = []
    skip_reason: dict[str, int] = {}
    for item in files:
        rel = item["path"][len(PREFIX) + 1:]
        size = item.get("size") or 0
        if args.force_all:
            keep.append(item)
            continue
        if any(rel.startswith(d) for d in SKIP_DIRS):
            skip_reason["已砍功能的素材目录"] = skip_reason.get("已砍功能的素材目录", 0) + 1
            continue
        if size > max_bytes and rel not in FORCE_KEEP:
            skip_reason["超过体积上限"] = skip_reason.get("超过体积上限", 0) + 1
            continue
        keep.append(item)

    keep_bytes = sum(f.get("size") or 0 for f in keep)
    print(f"\n将同步 {len(keep)} 个文件（{keep_bytes / 1024 / 1024:.1f} MB）")
    for reason, count in skip_reason.items():
        print(f"  跳过 {count} 个：{reason}")

    show = [f for f in keep if (f.get("size") or 0) > 200 * 1024]
    if show:
        print("\n其中较大的（>200KB）：")
        for f in sorted(show, key=lambda x: -(x.get("size") or 0))[:15]:
            print(f"  {(f.get('size') or 0) / 1024:7.0f} KB  {f['path'][len(PREFIX) + 1:]}")

    if args.dry_run:
        print("\n--dry-run：未下载。")
        return 0

    DEST.mkdir(parents=True, exist_ok=True)
    ok, failed, total = 0, [], 0
    for index, item in enumerate(keep, 1):
        rel = item["path"][len(PREFIX) + 1:]
        target = DEST / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            payload = fetch(
                f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/{item['path']}",
                binary=True)
        except Exception as exc:  # noqa: BLE001
            failed.append((rel, type(exc).__name__))
            continue
        target.write_bytes(payload)
        total += len(payload)
        ok += 1
        if index % 10 == 0 or index == len(keep):
            print(f"  {index}/{len(keep)}  {total / 1024 / 1024:.1f} MB", end="\r")

    print(f"\n完成：{ok} 个文件，{total / 1024 / 1024:.1f} MB → {DEST}")
    if failed:
        print(f"失败 {len(failed)} 个：")
        for rel, err in failed[:10]:
            print(f"  {rel}: {err}")

    # 清单
    (DEST.parent / "_public-manifest.json").write_text(json.dumps({
        "source": f"https://github.com/{REPO}/tree/{BRANCH}/{PREFIX}",
        "synced": ok,
        "skipped": skip_reason,
        "max_kb": args.max_kb,
        "note": "小文件全量同步；大文件与已砍功能的素材目录被排除",
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
