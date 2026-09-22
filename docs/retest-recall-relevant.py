#!/usr/bin/env python3
"""用与库内容匹配的查询重测召回 —— 验证重排到底是"坏了"还是"没匹配上"。

关键区别：
  * 若重排是坏的：即使查询高度相关，分数依然接近 0
  * 若只是没匹配：相关查询的分数会明显跳起来

用法：
    py docs/retest-recall-relevant.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("SEREIN_BASE_URL", "").rstrip("/")
KEY = os.environ.get("SEREIN_GATEWAY_KEY", "")

# 这些查询取自库里的真实主题（日记与 Operit 归档），故意避开"读书会"这类库外内容
QUERIES = [
    ("关系张力", "我们之间那种界限模糊、又不愿擦除的感觉"),
    ("依赖", "你半夜跑来长篇大论证明自己赢了"),
    ("日常关心", "你问我晚饭吃了没"),
    ("工作处境", "我在国企里跟那帮人周旋"),
    ("库外话题", "上次我们说的读书会定在什么时候"),
]


def recall(query: str) -> dict:
    url = f"{BASE}/api/hook/recall"
    payload = {"query": query, "session_id": "relevant-test", "max_notes": 2, "delivered_ids": []}
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST")
    req.add_header("Authorization", f"Bearer {KEY}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        return {"_error": exc.code, "_body": exc.read().decode("utf-8", "replace")[:300]}


def extract(data: dict) -> dict:
    live = (data.get("debug") or {}).get("typed_event_scene_live") or {}
    retrieval = live.get("candidate_retrieval") or {}
    admission = live.get("admission") or {}
    candidates = admission.get("candidates") or live.get("candidates") or []

    vec = [c.get("candidate_score") for c in candidates
           if isinstance(c.get("candidate_score"), (int, float))]
    rer = [c.get("rerank_score") for c in candidates
           if isinstance(c.get("rerank_score"), (int, float))]
    return {
        "status": live.get("status"),
        "vector_ranked": retrieval.get("vector_ranked_count"),
        "candidates": len(candidates),
        "threshold": admission.get("direct_threshold"),
        "vec_range": (min(vec), max(vec)) if vec else None,
        "rer_range": (min(rer), max(rer)) if rer else None,
        "top_titles": [c.get("title") for c in candidates[:3]],
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

    results = []
    for label, query in QUERIES:
        data = recall(query)
        print(f"\n=== [{label}] {query!r}")
        if "_error" in data:
            print(f"  HTTP {data['_error']}: {data.get('_body')}")
            continue

        print(f"  recalled_ids: {data.get('recalled_ids')}")
        info = extract(data)
        print(f"  状态          {info['status']}")
        print(f"  向量命中      {info['vector_ranked']}")
        print(f"  进入候选      {info['candidates']}")
        if info["vec_range"]:
            print(f"  向量分区间    {info['vec_range'][0]:.4f} ~ {info['vec_range'][1]:.4f}")
        if info["rer_range"]:
            print(f"  重排分区间    {info['rer_range'][0]:.4f} ~ {info['rer_range'][1]:.4f}")
        print(f"  准入阈值      {info['threshold']}")
        if info["top_titles"]:
            print(f"  候选标题      {info['top_titles']}")
        results.append((label, info))

    print("\n" + "=" * 66)
    print("判定")
    print("=" * 66)
    relevant = [(l, i) for l, i in results if l != "库外话题" and i.get("rer_range")]
    outside = [(l, i) for l, i in results if l == "库外话题" and i.get("rer_range")]

    if relevant:
        best_rel = max(i["rer_range"][1] for _, i in relevant)
        print(f"  库内相关查询的最高重排分: {best_rel:.4f}")
        if outside:
            best_out = max(i["rer_range"][1] for _, i in outside)
            print(f"  库外话题的最高重排分:     {best_out:.4f}")
            if best_rel > best_out * 5 and best_rel > 0.3:
                print("\n  >>> 重排工作正常：相关查询的分数明显高于无关查询。")
                print("      之前空召回是因为测试查询与库内容无关（我的测试方法问题）。")
            elif best_rel < 0.05:
                print("\n  >>> 重排可能确实异常：连库内相关查询也只得极低分。")
            else:
                print("\n  >>> 重排能区分，但整体分数偏低，可能需要调低准入阈值。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
