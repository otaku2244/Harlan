#!/usr/bin/env python3
"""诊断 Serein 召回：区分「没有数据」「查询太泛」「被过滤」三种情况。

对每个查询打印 debug 里的关键决策点，而不是只看 recalled_ids 是否为空。
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
    ("泛查询", "测试"),
    ("具体事件", "上次我们说的读书会定在什么时候？"),
    ("私人指代", "你还记得我上周跟你提过的那件事吗"),
    ("技术闲聊", "帮我看看这个 Python 报错"),
    ("原话追问", "把你那天说的原话再说一遍"),
    ("名字", "Harlan 最近在忙什么"),
]


def call(path: str, payload: dict, method: str = "POST") -> tuple[int, dict | str]:
    url = f"{BASE}{path}"
    data = json.dumps(payload).encode("utf-8") if payload else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {KEY}")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "aion-probe")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(body)
            except json.JSONDecodeError:
                return resp.status, body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, body
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"


def summarize(data: dict) -> dict:
    """从 debug 里抠出决策要点。字段名以实测为准，缺就报缺。"""
    out: dict = {}
    out["ok"] = data.get("ok")
    out["recalled_ids"] = data.get("recalled_ids")
    out["context_len"] = len(data.get("additional_context") or "")

    debug = data.get("debug") or {}
    out["injected"] = data.get("injected")
    out["mode"] = debug.get("mode")

    sem = debug.get("semantic_recall_debug") or {}
    out["route"] = sem.get("route")
    out["route_action"] = sem.get("route_action")
    out["sem_score"] = sem.get("score")

    live = sem.get("typed_event_scene_live") or {}
    out["status"] = live.get("status")
    out["method"] = live.get("method")
    out["injected_live"] = live.get("injected")

    retrieval = live.get("candidate_retrieval") or {}
    out["retrieval_status"] = retrieval.get("status")
    out["retrieval_reason"] = retrieval.get("reason")
    out["candidates"] = retrieval.get("candidate_count")
    out["vector_ranked"] = retrieval.get("vector_ranked_count")
    out["base_pool"] = retrieval.get("base_pool_count")
    out["suppressed"] = retrieval.get("snapshot_suppressed")

    policy = live.get("candidate_policy") or {}
    out["pool_limit"] = policy.get("pool_limit")

    rerank = live.get("surface_reranker_gate") or {}
    out["rerank_applied"] = rerank.get("applied")
    out["rerank_reason"] = rerank.get("reason")

    out["direct_threshold"] = (live.get("reason"), sem.get("direct_threshold"))
    return {k: v for k, v in out.items() if v is not None}


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    if not BASE or not KEY:
        print("需要 SEREIN_BASE_URL 与 SEREIN_GATEWAY_KEY", file=sys.stderr)
        return 2

    print(f"Serein: {BASE}\n")

    # 先看实例整体状态
    print("=== 实例状态 ===")
    for path in ("/ready", "/api/history", "/api/raw-archive"):
        code, data = call(path, None, method="GET")
        if isinstance(data, dict):
            keys = list(data.keys())[:8]
            print(f"  GET {path:<20} HTTP {code}  顶层键: {keys}")
        else:
            head = str(data)[:90].replace("\n", " ")
            print(f"  GET {path:<20} HTTP {code}  {head}")

    print("\n=== 逐查询诊断 ===")
    for label, query in QUERIES:
        code, data = call("/api/hook/recall", {
            "query": query, "session_id": "probe-diag", "max_notes": 2, "delivered_ids": [],
        })
        print(f"\n[{label}] {query!r}  → HTTP {code}")
        if not isinstance(data, dict):
            print(f"  非 JSON 返回: {str(data)[:200]}")
            continue
        for key, value in summarize(data).items():
            print(f"    {key:<18} {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
