#!/usr/bin/env python3
"""摸清 Serein MCP 的日记接口形状，作为代理层的规格。

不要猜字段名 —— 直接调用并把原始返回落盘。

用法：py docs/probe-mcp-diary.py
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
_session: str | None = None


def rpc(method: str, params: dict | None = None, notify: bool = False, rid: int = 1):
    global _session
    payload: dict = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        payload["params"] = params
    if not notify:
        payload["id"] = rid
    req = urllib.request.Request(f"{BASE}/serein/mcp",
                                data=json.dumps(payload).encode(), method="POST")
    req.add_header("Authorization", f"Bearer {KEY}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    if _session:
        req.add_header("Mcp-Session-Id", _session)
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            sid = resp.headers.get("Mcp-Session-Id")
            if sid:
                _session = sid
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return {"_http": exc.code, "_body": exc.read().decode("utf-8", "replace")[:300]}
    if not body.strip():
        return None
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return {"_raw": body[:300]}


def call_tool(name: str, args: dict, rid: int = 2) -> str:
    result = rpc("tools/call", {"name": name, "arguments": args}, rid=rid)
    if not result:
        return "(无响应)"
    if "result" in result:
        blocks = result["result"].get("content") or []
        return "\n".join(b.get("text") or json.dumps(b, ensure_ascii=False) for b in blocks)
    return json.dumps(result, ensure_ascii=False)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if not BASE or not KEY:
        print("需要 SEREIN_BASE_URL 与 SEREIN_GATEWAY_KEY", file=sys.stderr)
        return 2

    rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "diary-probe", "version": "0.1"}})
    rpc("notifications/initialized", {}, notify=True)

    # 工具的输入 schema 决定了代理层能传什么
    tools = rpc("tools/list", {}, rid=3)
    for item in (tools or {}).get("result", {}).get("tools", []):
        if item["name"] in ("read_diary", "memo_list", "read_memory"):
            print(f"=== {item['name']} schema ===")
            print(json.dumps(item.get("inputSchema", {}), ensure_ascii=False, indent=2)[:900])
            print()

    print("=== read_diary 默认（列目录）===")
    default = call_tool("read_diary", {}, rid=10)
    print(default[:1500])

    print("\n=== 取第一篇全文 ===")
    # 从目录里抠出第一个 id
    import re
    ids = re.findall(r"id:\s*(\S+)", default)
    if ids:
        first = ids[0]
        print(f"（用 id={first}）")
        full = call_tool("read_diary", {"diary_id": first}, rid=11)
        print(full[:2000])

    OUT.mkdir(exist_ok=True)
    (OUT / "mcp-diary.json").write_text(json.dumps({
        "default": default,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已落盘 {OUT / 'mcp-diary.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
