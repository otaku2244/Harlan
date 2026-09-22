#!/usr/bin/env python3
"""核对前端调用的 API 与后端实际路由是否对得上。

前端是移植来的，它调的是 AionsHome 的 API 形状；我们只实现了一部分。
这个脚本把两边摊开对照，避免"页面能打开但一发消息就 404"。

用法：
    py docs/verify-api-contract.py
"""
from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

BASE = "http://127.0.0.1:8080"
STATIC = Path(__file__).resolve().parent.parent / "app" / "static"

# 前端几种调用写法
PATTERNS = [
    re.compile(r"""api\(\s*["'](GET|POST|PUT|PATCH|DELETE)["']\s*,\s*[`"']([^`"'$]+)"""),
    re.compile(r"""api\(\s*[`"']([^`"'$]*?/[^`"'$]*)[`"']"""),
    re.compile(r"""fetch\(\s*[`"'](/api/[^`"'$?]+)"""),
    re.compile(r"""fetch\(\s*[`"']/api[`"']\s*\+"""),
]


def collect_frontend_calls() -> dict[str, set[str]]:
    """返回 {路径模板: {出现在哪些文件}}"""
    calls: dict[str, set[str]] = defaultdict(set)
    for path in sorted(STATIC.rglob("*.js")):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        rel = path.relative_to(STATIC).as_posix()
        for pattern in PATTERNS:
            for match in pattern.finditer(text):
                groups = [g for g in match.groups() if g]
                if not groups:
                    continue
                candidate = groups[-1]
                if not candidate.startswith("/"):
                    continue
                # 模板化的部分统一成 {}，便于和后端路由比对
                normalized = re.sub(r"\$\{[^}]*\}", "{}", candidate)
                calls[normalized.rstrip("/") or "/"].add(rel)
    return calls


def backend_routes() -> set[str]:
    req = urllib.request.Request(BASE + "/openapi.json",
                                 headers={"User-Agent": "verify"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        spec = json.loads(resp.read().decode("utf-8"))
    return set(spec.get("paths", {}).keys())


def matches(call: str, route: str) -> bool:
    """把前端的 {} 占位与后端的 {param} 对齐比较。"""
    call_parts = call.strip("/").split("/")
    route_parts = route.strip("/").split("/")
    if len(call_parts) != len(route_parts):
        return False
    for a, b in zip(call_parts, route_parts):
        if a == "{}" or (b.startswith("{") and b.endswith("}")):
            continue
        if a != b:
            return False
    return True


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    try:
        routes = backend_routes()
    except Exception as exc:  # noqa: BLE001
        print(f"读不到后端路由（服务没起？）: {exc}")
        return 1

    calls = collect_frontend_calls()
    print(f"前端调用点 {len(calls)} 个，后端路由 {len(routes)} 个\n")

    ok, missing = [], []
    for call in sorted(calls):
        hit = next((r for r in routes if matches(call, r)), None)
        (ok if hit else missing).append((call, hit, sorted(calls[call])))

    print("=" * 70)
    print(f"能对上（{len(ok)} 个）")
    print("=" * 70)
    for call, hit, _ in ok:
        print(f"  [OK] {call:<40} → {hit}")

    print("\n" + "=" * 70)
    print(f"对不上（{len(missing)} 个）—— 页面能开，但一并用到就会 404")
    print("=" * 70)
    for call, _, sources in missing:
        print(f"  [X]  {call}")
        print(f"       调用方: {', '.join(sources)[:80]}")

    print("\n" + "=" * 70)
    print("后端已有但前端没用的路由")
    print("=" * 70)
    unused = [r for r in sorted(routes)
              if not any(matches(c, r) for c in calls) and r.startswith("/api")]
    for route in unused[:30]:
        print(f"  {route}")

    print()
    if missing:
        print("结论：需要给这些缺的端点写适配层（或从后端返回前端期望的形状），")
        print("      否则前端会在这些功能上报错。核心聊天链路是否可用要看")
        print("      /api/conversations 与消息发送相关的几个是否在『能对上』里。")
    else:
        print("结论：前端调用的端点全部有实现。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
