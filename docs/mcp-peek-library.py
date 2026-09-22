#!/usr/bin/env python3
"""用 MCP 读 Serein 库里到底存了什么（不是能不能召回，而是有什么）。

要回答的问题：
  1. 原话档案里有没有东西？（source_message_search）
  2. 日记里是什么内容、什么年代？（read_diary）
  3. 库里有没有跟查询相关的主题？

用法：
    py docs/mcp-peek-library.py
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


def rpc(method: str, params: dict | None = None, notify: bool = False, rid: int = 1):
    global _session_id
    payload: dict = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        payload["params"] = params
    if not notify:
        payload["id"] = rid
    req = urllib.request.Request(MCP_URL, data=json.dumps(payload).encode("utf-8"), method="POST")
    req.add_header("Authorization", f"Bearer {KEY}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    if _session_id:
        req.add_header("Mcp-Session-Id", _session_id)
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            sid = resp.headers.get("Mcp-Session-Id")
            if sid:
                _session_id = sid
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return {"_http_error": exc.code, "_body": exc.read().decode("utf-8", "replace")[:400]}
    except Exception as exc:  # noqa: BLE001
        return {"_error": f"{type(exc).__name__}: {exc}"}
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
    return {"_unparsed": body[:300]}


def call_tool(name: str, args: dict, rid: int = 2) -> str:
    result = rpc("tools/call", {"name": name, "arguments": args}, rid=rid)
    if not result:
        return "(无响应)"
    if "result" in result:
        blocks = result["result"].get("content") or []
        parts = []
        for block in blocks:
            parts.append(block.get("text") or json.dumps(block, ensure_ascii=False))
        text = "\n".join(parts)
        if result["result"].get("isError"):
            return "[工具报错] " + text
        return text
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
                       "clientInfo": {"name": "lib-peek", "version": "0.1"}})
    rpc("notifications/initialized", {}, notify=True)

    print("=" * 66)
    print("1. 原话档案：搜常见字，看有没有原始对话导入")
    print("=" * 66)
    for term in ("我", "你", "的"):
        out = call_tool("source_message_search", {"query": term, "limit": 3}, rid=11)
        head = out[:600].replace("\n", "\n     ")
        print(f"\n  搜 {term!r}:\n     {head}")

    print("\n" + "=" * 66)
    print("2. 日记：看内容主题与时间跨度")
    print("=" * 66)
    out = call_tool("read_diary", {"limit": 20}, rid=12)
    print(out[:2500])

    print("\n" + "=" * 66)
    print("3. 收藏（若有）")
    print("=" * 66)
    print(call_tool("read_favorites", {"limit": 5}, rid=13)[:800])

    print("\n" + "=" * 66)
    print("4. 用 MCP 的 recall_memory 试一次（看它自己的重排诊断）")
    print("=" * 66)
    print(call_tool("recall_memory", {"query": "读书会"}, rid=14)[:1500])

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
