#!/usr/bin/env python3
"""验证阈值调整效果 —— 用库内确定相关的人名做对照。

对照组：
  * "江清漪"      —— 库内人物，重排分应当很高
  * 库内关系话题   —— 中等相关
  * 库外无关话题   —— 应当仍被挡住

用法：
    py docs/verify-recall-threshold.py [关键词...]
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("SEREIN_BASE_URL", "").rstrip("/")
KEY = os.environ.get("SEREIN_GATEWAY_KEY", "")

DEFAULT_QUERIES = [
    ("人名", "江清漪"),
    ("人名+话题", "江清漪 最近在做什么"),
    ("关系张力", "我们之间那种界限模糊、又不愿擦除的感觉"),
    ("依赖", "你半夜跑来长篇大论证明自己赢了"),
    ("库外话题", "上次我们说的读书会定在什么时候"),
]


def recall(query: str, session: str = "verify-threshold") -> dict:
    url = f"{BASE}/api/hook/recall"
    payload = {"query": query, "session_id": session, "max_notes": 2, "delivered_ids": []}
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST")
    req.add_header("Authorization", f"Bearer {KEY}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        return {"_error": exc.code, "_body": exc.read().decode("utf-8", "replace")[:400]}


def extract(data: dict) -> dict:
    debug = data.get("debug") or {}
    live = debug.get("typed_event_scene_live") or {}
    retrieval = live.get("candidate_retrieval") or {}
    admission = live.get("admission") or {}
    candidates = admission.get("candidates") or live.get("candidates") or []

    vec = [c.get("candidate_score") for c in candidates
           if isinstance(c.get("candidate_score"), (int, float))]
    rer = [(c.get("rerank_score"), c) for c in candidates
           if isinstance(c.get("rerank_score"), (int, float))]
    rer_sorted = sorted(rer, key=lambda x: -(x[0] or 0))
    return {
        "status": live.get("status"),
        "vector_ranked": retrieval.get("vector_ranked_count"),
        "candidates": len(candidates),
        "threshold": admission.get("direct_threshold"),
        "vec_range": (min(vec), max(vec)) if vec else None,
        "rer_range": (min(r for r, _ in rer), max(r for r, _ in rer)) if rer else None,
        "top": rer_sorted[:3],
        "selected": live.get("selected_refs") or [],
    }


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    if not BASE or not KEY:
        print("需要 SEREIN_BASE_URL 与 SEREIN_GATEWAY_KEY", file=sys.stderr)
        return 2

    queries = DEFAULT_QUERIES
    if len(sys.argv) > 1:
        queries = [("自定义", " ".join(sys.argv[1:]))] + DEFAULT_QUERIES

    for index, (label, query) in enumerate(queries):
        data = recall(query, session=f"verify-{index}")
        print(f"\n{'=' * 66}\n=== [{label}] {query!r}")
        if "_error" in data:
            print(f"  HTTP {data['_error']}: {data.get('_body')}")
            continue

        ids = data.get("recalled_ids") or []
        ctx = data.get("additional_context") or ""
        print(f"  ✓ recalled_ids : {ids}")
        print(f"  ✓ 上下文长度   : {len(ctx)} 字")
        print(f"  injected       : {data.get('injected')}")

        info = extract(data)
        print(f"  状态           : {info['status']}")
        print(f"  向量命中       : {info['vector_ranked']}")
        print(f"  进入候选       : {info['candidates']}")
        if info["vec_range"]:
            print(f"  向量分区间     : {info['vec_range'][0]:.4f} ~ {info['vec_range'][1]:.4f}")
        if info["rer_range"]:
            print(f"  重排分区间     : {info['rer_range'][0]:.4f} ~ {info['rer_range'][1]:.4f}")
        print(f"  准入阈值       : {info['threshold']}")

        if info["top"]:
            print("  重排 Top3:")
            for score, item in info["top"]:
                title = item.get("title") or item.get("ref") or "?"
                reason = item.get("reason") or ""
                print(f"    {score:.4f}  {reason:<38} {str(title)[:44]}")

        if ctx:
            print("  注入的上下文（前 400 字）:")
            for line in ctx[:400].splitlines():
                print(f"    | {line}")

    print(f"\n{'=' * 66}\n结论")
    print("  有 recalled_ids 就说明阈值调整生效了。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
