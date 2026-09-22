#!/usr/bin/env python3
"""对本地运行的服务做一次真实端到端验收。

需要服务已在 127.0.0.1:8080 运行，且 .env 配好了 Serein 与模型。

验证：
  1. 配置确实读到了
  2. 说话（SSE）→ 记忆注入 → 落库
  3. 主动开口（/api/wake）→ 落库 → 排下一次
  4. 空闲自主的 rest 分支

用法：py docs/e2e-live-check.py
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8080"


def call(method: str, path: str, body: dict | None = None, timeout: int = 180):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")[:400]
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"


def parse_sse(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        if line.startswith("data:"):
            try:
                out.append(json.loads(line[5:].strip()))
            except json.JSONDecodeError:
                pass
    return out


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    print("=" * 66)
    print("1. 配置")
    print("=" * 66)
    status, health = call("GET", "/healthz")
    if status != 200 or not isinstance(health, dict):
        print(f"  服务不可用（HTTP {status}）: {health}")
        return 1
    seen = health.get("config_seen") or {}
    print(f"  serein_configured = {health.get('serein_configured')}")
    print(f"  model_configured  = {health.get('model_configured')}")
    print(f"  serein_base_url   = {seen.get('serein_base_url')}")
    print(f"  model_name        = {seen.get('model_name')}")

    print("\n" + "=" * 66)
    print("2. 说话（SSE）+ 记忆注入")
    print("=" * 66)
    started = time.time()
    status, text = call("POST", "/api/chat",
                        {"message": "江清漪 最近怎么样", "conv_id": "live-e2e"})
    events = parse_sse(text) if isinstance(text, str) else []
    kinds = [e["type"] for e in events]
    print(f"  HTTP {status}  事件 {kinds}")
    recall = next((e for e in events if e["type"] == "recall"), None)
    if recall:
        print(f"  召回: ok={recall['data'].get('ok')} 条数={recall['data'].get('count')}")
    end = next((e for e in events if e["type"] == "stream_end"), None)
    reply = (end or {}).get("data", {}).get("text", "")
    print(f"  耗时 {time.time() - started:.1f}s")
    print(f"  回复: {reply}")

    delivery = next((e for e in events if e["type"] == "delivery"), None)
    if delivery:
        print(f"  交付登记: {delivery['data'].get('note') or delivery['data']}")

    print("\n" + "=" * 66)
    print("3. 主动开口（proactive）")
    print("=" * 66)
    status, result = call("POST", "/api/wake", {"kind": "proactive"})
    print(f"  HTTP {status}  {json.dumps(result, ensure_ascii=False)}")

    print("\n" + "=" * 66)
    print("4. 它说过的话（唤醒会话）")
    print("=" * 66)
    _, data = call("GET", "/api/conversations/harlan/messages")
    for item in (data or {}).get("messages", []):
        meta = item.get("meta") or {}
        wake = meta.get("wake") or {}
        tag = f"  <{wake.get('type')}/{wake.get('action')}>" if wake else ""
        print(f"  [{item['sender']}] {item['content']}{tag}")
        if wake.get("reason"):
            print(f"        原因: {wake['reason']}")

    print("\n" + "=" * 66)
    print("5. 唤醒总线")
    print("=" * 66)
    _, wakes = call("GET", "/api/wakes")
    print(f"  调度器运行中: {(wakes or {}).get('scheduler_running')}")
    for item in (wakes or {}).get("pending", []):
        print(f"  待触发: {item['type']}  还有 {item['in_seconds'] / 60:.1f} 分钟")
    for item in (wakes or {}).get("recent", [])[:5]:
        print(f"  最近:   {item['type']}  {item['status']}")

    print("\n" + "=" * 66)
    print("6. 空闲自主（rest 分支：不应产生消息）")
    print("=" * 66)
    _, before = call("GET", "/api/conversations/harlan/messages")
    n_before = len((before or {}).get("messages", []))
    status, result = call("POST", "/api/wake", {"kind": "idle"})
    _, after = call("GET", "/api/conversations/harlan/messages")
    n_after = len((after or {}).get("messages", []))
    spoke = (result or {}).get("spoke") if isinstance(result, dict) else None
    print(f"  HTTP {status}  spoke={spoke}  消息数 {n_before} → {n_after}")
    if spoke:
        print("  （它选择说点什么，属于正常分支）")
    else:
        print("  ✓ 选择休息，没有打扰用户")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
