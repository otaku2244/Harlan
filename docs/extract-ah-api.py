#!/usr/bin/env python3
"""从 AionsHome 的 README 里抽出 API 一览，作为适配层的规格。

用法：py docs/extract-ah-api.py
输出：docs/aionshome-api-reference.md
"""
from __future__ import annotations

import re
import sys
import urllib.request
from pathlib import Path

URL = "https://raw.githubusercontent.com/death34018-hue/AionsHome/main/aion-chat/README.md"
OUT = Path(__file__).resolve().parent / "aionshome-api-reference.md"

WANT_SECTIONS = ("对话/消息", "设置/世界书/状态", "记忆库", "文件管理",
                 "日程/闹铃", "SSE 事件类型", "WebSocket 事件类型", "消息角色说明")


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    req = urllib.request.Request(URL, headers={"User-Agent": "aion-doc"})
    with urllib.request.urlopen(req, timeout=90) as resp:
        text = resp.read().decode("utf-8", "replace")

    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith("## API 一览")), None)
    if start is None:
        print("README 里找不到 '## API 一览'")
        return 1
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    block = lines[start:end]

    # 按 ### 分节，只保留关心的
    sections: list[tuple[str, list[str]]] = []
    current: tuple[str, list[str]] | None = None
    for line in block:
        if line.startswith("### "):
            current = (line[4:].strip(), [])
            sections.append(current)
        elif current is not None:
            current[1].append(line)

    kept = [(name, body) for name, body in sections
            if any(want in name for want in WANT_SECTIONS)]

    out = [
        "# AionsHome API 参考（自动抽取，不要手改）",
        "",
        f"来源：{URL}",
        "",
        "用途：移植过来的前端调用的是这些端点的形状。",
        "后端适配层要按它们实现，**不要去改前端**（前端 243KB 且经实战验证）。",
        "",
    ]
    for name, body in kept:
        out.append(f"## {name}")
        out.append("")
        rows = [line for line in body if line.strip().startswith("|")]
        out.extend(rows or ["（本节无表格）"])
        out.append("")

    OUT.write_text("\n".join(out), encoding="utf-8")
    print(f"已写出 {OUT}")
    print(f"保留 {len(kept)} 个小节：")
    for name, body in kept:
        rows = len([line for line in body if line.strip().startswith("|")])
        print(f"  {name}  （{rows} 行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
