#!/usr/bin/env python3
"""检查 Serein 召回诊断里的 reranker_error 字段。

Serein 的 chat_observation 会把 recall_diagnostics 暴露出来，其中包含：
    status / reason / suppressed / reranker_error / pre_cooldown_selected_refs

只要 reranker_error 非空，就说明重排请求失败了（而不是分数低）。
用法：
    py docs/check-serein-reranker.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("SEREIN_BASE_URL", "").rstrip("/")
KEY = os.environ.get("SEREIN_GATEWAY_KEY", "")

QUERIES = [
    "上次我们说的读书会定在什么时候？",
    "你还记得我上周跟你提过的那件事吗",
    "我们第一次聊天的时候聊了什么",
]


def recall(query: str) -> dict:
    url = f"{BASE}/api/hook/recall"
    payload = {"query": query, "session_id": "rerank-probe", "max_notes": 2, "delivered_ids": []}
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST")
    req.add_header("Authorization", f"Bearer {KEY}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        return {"_error": exc.code, "_body": exc.read().decode("utf-8", "replace")[:300]}


def find_all(node, key: str, path="", out=None):
    if out is None:
        out = []
    if isinstance(node, dict):
        for k, v in node.items():
            child = f"{path}.{k}" if path else k
            if k == key:
                out.append((child, v))
            find_all(v, key, child, out)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            find_all(item, key, f"{path}[{i}]", out)
    return out


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    if not BASE or not KEY:
        print("需要 SEREIN_BASE_URL 与 SEREIN_GATEWAY_KEY", file=sys.stderr)
        return 2

    any_error = False
    for query in QUERIES:
        data = recall(query)
        print(f"\n=== {query!r}")
        if "_error" in data:
            print(f"  请求失败 HTTP {data['_error']}: {data.get('_body')}")
            continue

        print(f"  recalled_ids : {data.get('recalled_ids')}")
        print(f"  context 长度 : {len(data.get('additional_context') or '')}")

        # 1. 专门找 reranker_error
        errors = find_all(data, "reranker_error")
        if errors:
            for path, value in errors:
                marker = "  <<< 有错误" if value else ""
                print(f"  {path} = {value!r}{marker}")
                if value:
                    any_error = True
        else:
            print("  诊断里没有 reranker_error 字段（可能是名字不同或未暴露）")

        # 2. 把所有含 'error'/'diag' 的字段都列出来，避免漏掉改名后的字段
        for word in ("error", "diagnostic"):
            hits = find_all(data, word)
            for path, value in find_all(data, word):
                pass
        diag = []
        for key_name in ("recall_diagnostics", "diagnostics"):
            diag.extend(find_all(data, key_name))
        for path, value in diag:
            print(f"  {path} = {json.dumps(value, ensure_ascii=False)[:300]}")

        # 3. 顺带看 max rerank 分
        live = data.get("debug", {}).get("typed_event_scene_live") or {}
        candidates = (live.get("admission") or {}).get("candidates") or live.get("candidates") or []
        scores = [c.get("rerank_score") for c in candidates if isinstance(c.get("rerank_score"), (int, float))]
        if scores:
            print(f"  重排分 {min(scores):.4f} ~ {max(scores):.4f}（{len(scores)} 个候选）")

    print()
    if any_error:
        print(">>> 结论：重排请求本身报错了。按错误码排查（http_4xx=配置/Key，timeout=网络，request_failed=连接）。")
    else:
        print(">>> 结论：没有捕获到 reranker_error。若重排分仍接近 0，更可能是")
        print("    —— 重排 endpoint 不是官方 api.siliconflow.cn，导致 instruction 未附带，")
        print("       Qwen3-Reranker 在缺少 instruction 时会给出极低分。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
