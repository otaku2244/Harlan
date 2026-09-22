#!/usr/bin/env python3
"""用 MCP 协议直接读 Serein 的库，确认里面到底有什么。

Hook 只回答"能不能召回"，不回答"库里有什么"。这个脚本走 MCP
（Streamable HTTP）调用只读工具，把库存样本拉出来。

用法：
    py docs/mcp-peek-serein.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("SEREIN_BASE_URL", "").rstrip("/")
KEY = os.environ.get("SEREIN_GATEWAY_KEY", "")
MCP_URL = f"{BASE}/serein/mcp"

_session_id: str | None = None


def rpc(method: str, params: dict | None = None, notify: bool = False) -> dict | None:
    global _session_id
    payload: dict = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        payload["params"] = params
    if not notify:
        payload["id"] = 1

    req = urllib.request.Request(
        MCP_URL, data=json.dumps(payload).encode("utf-8"), method="POST"
    )
    req.add_header("Authorization", f"Bearer {KEY}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    if _session_id:
        req.add_header("Mcp-Session-Id", _session_id)

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            sid = resp.headers.get("Mcp-Session-Id")
            if sid:
                _session_id = sid
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return {"_http_error": exc.code, "_body": exc.read().decode("utf-8", "replace")[:400]}
    except Exception as exc:  # noqa: BLE001
        return {"_error": f"{type(exc).__name__}: {exc}"}

    if not body.strip():
        return None  # 通知没有响应
    # Streamable HTTP 可能用 SSE 包装
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return {"_unparsed": body[:300]}


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    if not BASE or not KEY:
        print("需要 SEREIN_BASE_URL 与 SEREIN_GATEWAY_KEY", file=sys.stderr)
        return 2

    print(f"MCP: {MCP_URL}\n")

    init = rpc("initialize", {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "aion-probe", "version": "0.1"},
    })
    print(f"initialize: {json.dumps(init, ensure_ascii=False)[:300] if init else None}")
    if init and "_http_error" in init:
        print("\nMCP 握手失败。可能原因：")
        print("  - 该实例未暴露 MCP")
        print("  - MCP 需要 OAuth（网页授权）而非静态 Key")
        print("  - 路径不是 /serein/mcp")
        return 1

    rpc("notifications/initialized", {}, notify=True)

    tools = rpc("tools/list", {})
    if not tools or "result" not in tools:
        print(f"tools/list 失败: {json.dumps(tools, ensure_ascii=False)[:400]}")
        return 1

    items = tools["result"].get("tools") or []
    print(f"\n可用工具 {len(items)} 个：")
    for item in items:
        print(f"  - {item['name']:<28} {(item.get('description') or '')[:70]}")

    # 挑只读工具探查库存
    read_only = [t["name"] for t in items
                 if any(w in t["name"] for w in ("list", "read", "find", "recent", "count", "stats"))]
    print(f"\n候选只读工具: {read_only}")

    for name in read_only[:6]:
        print(f"\n--- 调用 {name} ---")
        result = rpc("tools/call", {"name": name, "arguments": {}})
        if result and "result" in result:
            content = result["result"].get("content") or []
            for block in content[:3]:
                text = block.get("text") or json.dumps(block, ensure_ascii=False)
                print(text[:1200])
        else:
            print(json.dumps(result, ensure_ascii=False)[:300] if result else "无响应")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
