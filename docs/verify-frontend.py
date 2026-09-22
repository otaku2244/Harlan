#!/usr/bin/env python3
"""验证移植后的前端能否被服务端正确提供。

浏览器能不能跑我看不到，但"每个被引用的资源是否返回 200"是能测的 ——
而这恰好是最容易出错的部分（绝对路径 /static 与 /public 的挂载）。

用法：
    py docs/verify-frontend.py            # 需要服务在 127.0.0.1:8080
"""
from __future__ import annotations

import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8080"
STATIC = Path(__file__).resolve().parent.parent / "app" / "static"

HTML_REF = re.compile(r"""(?:src|href)=["'](/(?:static|public|manifest|sw)[^"'\s?#]*)["']""")
JS_REF = re.compile(r"""["'](/(?:static|public)[^"'\s?#]+)["']""")


def head(path: str) -> tuple[int, int]:
    """返回 (状态码, 内容长度)。"""
    req = urllib.request.Request(BASE + urllib.parse.quote(path),
                                 method="GET",
                                 headers={"User-Agent": "verify"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, len(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, 0
    except Exception:  # noqa: BLE001
        return 0, 0


def collect_refs() -> dict[str, set[str]]:
    refs: dict[str, set[str]] = {}

    def add(ref: str, source: str) -> None:
        refs.setdefault(ref.split("?", 1)[0], set()).add(source)

    for path in sorted(STATIC.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(STATIC).as_posix()
        if path.suffix in (".html", ".htm"):
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for m in HTML_REF.finditer(text):
                add(m.group(1), rel)
        elif path.suffix == ".js":
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for m in JS_REF.finditer(text):
                add(m.group(1), rel)
    return refs


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    print("=" * 66)
    print("1. 页面路由")
    print("=" * 66)
    pages = ["/", "/chat", "/home", "/settings", "/worldbook", "/memory",
             "/diary", "/moments", "/schedule", "/location", "/monitor-logs",
             "/camera", "/activity-logs", "/manifest.json"]
    page_bad = []
    for page in pages:
        code, size = head(page)
        mark = "OK " if code == 200 else "X  "
        print(f"  [{mark}] {page:<18} {code}  {size:>7} 字节")
        if code != 200:
            page_bad.append(page)

    print("\n" + "=" * 66)
    print("2. 被引用的静态资源")
    print("=" * 66)
    refs = collect_refs()
    bad = []
    for ref in sorted(refs):
        code, size = head(ref)
        if code != 200:
            bad.append((ref, code, sorted(refs[ref])))
    print(f"  共 {len(refs)} 个引用，失败 {len(bad)} 个")
    for ref, code, sources in bad:
        print(f"  [X] {ref}  HTTP {code}")
        print(f"      被引用于: {', '.join(sources)[:90]}")

    print("\n" + "=" * 66)
    print("结论")
    print("=" * 66)
    if not page_bad and not bad:
        print("  全部 200。前端资源路径正确，可以在浏览器里打开看效果了。")
    else:
        if page_bad:
            print(f"  页面失败 {len(page_bad)} 个: {page_bad}")
        if bad:
            print(f"  资源失败 {len(bad)} 个（多半是被砍掉功能的残留引用，浏览器控制台会报 404）")
    return 0 if not page_bad else 1


if __name__ == "__main__":
    import urllib.parse  # noqa: E402
    raise SystemExit(main())
