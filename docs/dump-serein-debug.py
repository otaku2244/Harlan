#!/usr/bin/env python3
"""把 Serein `recall` 返回的完整 debug 结构落盘，用于定位「候选池为什么是空的」。

不猜字段路径，直接把整棵 debug 树写成 JSON 文件，再按需查。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = os.environ.get("SEREIN_BASE_URL", "").rstrip("/")
KEY = os.environ.get("SEREIN_GATEWAY_KEY", "")
OUT = Path(__file__).resolve().parent.parent / ".serein-debug"


def call(query: str, session: str) -> dict:
    url = f"{BASE}/api/hook/recall"
    payload = {"query": query, "session_id": session, "max_notes": 2, "delivered_ids": []}
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST")
    req.add_header("Authorization", f"Bearer {KEY}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        return {"_http_error": exc.code, "_body": exc.read().decode("utf-8", "replace")[:500]}


def walk(node, path="", hits=None):
    """找出所有含关键词的字段，不预设结构。"""
    if hits is None:
        hits = []
    keys_of_interest = (
        "count", "status", "reason", "pool", "candidate", "suppress", "threshold",
        "total", "empty", "scope", "eligible", "rejected", "snapshot",
    )
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}" if path else key
            if isinstance(value, (dict, list)):
                walk(value, child, hits)
            else:
                if any(word in key.lower() for word in keys_of_interest):
                    hits.append((child, value))
    elif isinstance(node, list):
        for index, item in enumerate(node[:3]):
            walk(item, f"{path}[{index}]", hits)
    return hits


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    if not BASE or not KEY:
        print("需要 SEREIN_BASE_URL 与 SEREIN_GATEWAY_KEY", file=sys.stderr)
        return 2

    OUT.mkdir(exist_ok=True)
    queries = [
        ("bookclub", "上次我们说的读书会定在什么时候？"),
        ("latenight", "你还记得我上周跟你提过的那件事吗"),
    ]
    for name, query in queries:
        data = call(query, "diag-probe")
        path = OUT / f"{name}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n=== {name}: {query!r} ===")
        print(f"顶层键: {list(data.keys())}")
        print(f"recalled_ids: {data.get('recalled_ids')}")
        print(f"完整 debug 已存: {path}")

        hits = walk(data.get("debug"))
        print(f"含关键字段的条目（{len(hits)} 个）:")
        for key, value in hits[:40]:
            print(f"    {key:<62} {value}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
