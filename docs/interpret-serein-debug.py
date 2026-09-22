#!/usr/bin/env python3
"""从已落盘的 Serein debug 里确认：重排分数是否塌缩成 0。

用法：
    py docs/interpret-serein-debug.py [debug 目录]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

KEYS = ("typed_event_scene_live", "semantic_recall_debug")


def find_live(node, depth=0):
    """不预设嵌套路径，递归找出含 candidates/admission 的那棵子树。"""
    if depth > 6:
        return None
    if isinstance(node, dict):
        if "candidates" in node and ("admission" in node or "candidate_retrieval" in node):
            return node
        for value in node.values():
            hit = find_live(value, depth + 1)
            if hit is not None:
                return hit
    elif isinstance(node, list):
        for item in node:
            hit = find_live(item, depth + 1)
            if hit is not None:
                return hit
    return None


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".serein-debug")
    files = sorted(root.glob("*.json"))
    if not files:
        print(f"{root} 下没有 debug 文件", file=sys.stderr)
        return 1

    verdicts: list[str] = []
    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        print(f"\n{'=' * 64}\n### {path.name}")

        debug = data.get("debug") or {}
        print(f"  debug 顶层键: {list(debug.keys())}")

        live = find_live(debug)
        if live is None:
            print("  找不到含 candidates 的子树")
            continue

        retrieval = live.get("candidate_retrieval") or {}
        admission = live.get("admission") or {}
        candidates = admission.get("candidates") or live.get("candidates") or []

        print(f"  live.status            {live.get('status')}")
        print(f"  向量命中               {retrieval.get('vector_ranked_count')}")
        print(f"  进入候选               {retrieval.get('candidate_count')}")
        print(f"  候选池上限             {(live.get('candidate_policy') or {}).get('pool_limit')}")
        suppressed = retrieval.get("snapshot_suppressed") or {}
        if suppressed:
            print(f"  被抑制                 {suppressed}")
        print(f"  准入阈值               {admission.get('direct_threshold')}")
        print(f"  重排是否执行           {live.get('surface_reranker_gate', {}).get('applied')}")

        if not candidates:
            print("  没有候选记录")
            continue

        print(f"  {'向量分':>8}  {'重排分':>8}  原因")
        vec_scores, rerank_scores = [], []
        for item in candidates[:12]:
            vs = item.get("candidate_score")
            rs = item.get("rerank_score")
            if isinstance(vs, (int, float)):
                vec_scores.append(vs)
            if isinstance(rs, (int, float)):
                rerank_scores.append(rs)
            print(f"  {vs!s:>8}  {rs!s:>8}  {item.get('reason')}")

        if rerank_scores:
            top = max(rerank_scores)
            print(f"\n  向量分区间   {min(vec_scores):.4f} ~ {max(vec_scores):.4f}")
            print(f"  重排分区间   {min(rerank_scores):.4f} ~ {top:.4f}")
            threshold = admission.get("direct_threshold") or 0.65
            if top < 0.05:
                verdicts.append(f"{path.name}: 重排分数塌缩（最大 {top:.4f}）")
                print("  >>> 判定：重排模型返回的分数全部接近 0 —— 重排环节失效，"
                      "无论库里有什麼都不可能通过准入")
            elif top < threshold:
                verdicts.append(f"{path.name}: 重排分偏低（最大 {top:.4f} < {threshold}）")
                print(f"  >>> 判定：重排分低于阈值 {threshold}，属正常未命中")
            else:
                print("  >>> 判定：有候选通过重排，属正常召回")

    print(f"\n{'=' * 64}\n结论")
    if verdicts:
        for line in verdicts:
            print(f"  - {line}")
    else:
        print("  未发现异常")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
