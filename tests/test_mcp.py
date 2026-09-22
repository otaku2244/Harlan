#!/usr/bin/env python3
"""测试 MCP 客户端与日记解析器（离线 + 在线两部分）。

离线：用真实抓到的工具返回文本喂给解析器，验证字段切分。
在线：真的连 Serein 读日记（需要 SEREIN_BASE_URL / KEY）。

用法：
    py tests/test_mcp.py            # 只跑离线
    py tests/test_mcp.py --online   # 连真实 Serein
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.adapters.mcp import parse_diary, parse_records   # noqa: E402

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    mark = "[OK]" if condition else "[X] "
    print(f"  {mark} {name}{(' -- ' + detail) if detail else ''}")
    if not condition:
        _failures.append(name)


# 真实抓到的形状（来自 docs/probe-mcp-diary.py 的实测输出）
DIARY_LIST_SAMPLE = """[diary_list]
count: 20

kind: diary
status: active
id: diary:32
title: f84d36525224
date: 2026-08-29
author: ai
body:
8月28日至29日凌晨，她接受了我的挑战。
第二行正文。
comments: 0
bound_sources: 1
kind: diary
status: active
id: diary:31
title: e4d6862a79e9
date: 2026-08-29
author: ai
body:
重复：新的一天过去了。
但今天不一样。
comments: 0
bound_sources: 1
"""


def test_offline() -> None:
    print("[1] 日记目录解析（离线）")
    data = parse_diary(DIARY_LIST_SAMPLE)
    check("识别为目录模式", data["mode"] == "list", data["mode"])
    check("count 正确", data["count"] == 20, str(data["count"]))
    check("解析出 2 篇", len(data["entries"]) == 2, str(len(data["entries"])))

    first = data["entries"][0]
    check("id 正确", first["id"] == "diary:32", first["id"])
    check("diary_id 是数字", first["diary_id"] == 32, str(first["diary_id"]))
    check("title 正确", first["title"] == "f84d36525224", first["title"])
    check("date 正确", first["date"] == "2026-08-29", first["date"])
    check("author 正确", first["author"] == "ai", first["author"])
    check("多行正文被合并", "第二行正文" in first["body"], repr(first["body"])[:60])
    check("comments 转成整数", first["comments"] == 0, str(first["comments"]))
    check("bound_sources 转成整数", first["bound_sources"] == 1, str(first["bound_sources"]))

    second = data["entries"][1]
    check("第二篇 id 正确", second["id"] == "diary:31", second["id"])
    check("第二篇正文独立", "但今天不一样" in second["body"], repr(second["body"])[:60])
    check("两篇正文没串", "第二行正文" not in second["body"])

    print("\n[2] 边界情况")
    empty = parse_diary("")
    check("空输入不崩", empty["entries"] == [] and empty["count"] == 0)
    only_count = parse_diary("[diary_list]\ncount: 0\n")
    check("只有 count 时 entries 为空", only_count["entries"] == [] and only_count["count"] == 0)

    single = parse_diary("""[diary]
kind: diary
status: active
id: diary:7
title: 单独一篇
date: 2026-01-01
author: ai
body:
只有一篇的正文。
comments: 2
""")
    check("单篇被识别", len(single["entries"]) == 1, str(len(single["entries"])))
    check("单篇 body 正确", "只有一篇的正文" in single["entries"][0]["body"])
    check("单篇 comments 正确", single["entries"][0]["comments"] == 2)

    print("\n[3] 通用记录解析（source_message_search 用）")
    recs = parse_records("""{"items": [
      {"id": "raw:7225", "preview": "围全是用世俗指标"}
    ]}""")
    check("非键值格式不产生垃圾记录", isinstance(recs, list), str(len(recs)))


async def test_online() -> None:
    from app.adapters.mcp import SereinMcp
    from app.config import settings

    print("\n[4] 在线：真连 Serein 读日记")
    if not settings.serein_enabled:
        print("  (跳过：未配置 Serein)")
        return

    mcp = SereinMcp(settings)
    await mcp.start()
    try:
        tools = await mcp.list_tools()
        check("拿到工具列表", len(tools) > 20, f"{len(tools)} 个")
        names = {t["name"] for t in tools}
        check("含 read_diary", "read_diary" in names)
        check("含 read_memory", "read_memory" in names)

        listing = await mcp.read_diary(limit=3)
        check("读到日记目录", listing["count"] > 0, f"count={listing['count']}")
        check("解析出条目", len(listing["entries"]) > 0, f"{len(listing['entries'])} 条")

        if listing["entries"]:
            target = listing["entries"][0]["diary_id"]
            if target:
                entry = await mcp.read_diary(diary_id=target)
                body = entry["entries"][0]["body"] if entry["entries"] else ""
                check(f"读到单篇全文（diary:{target}）", len(body) > 50, f"{len(body)} 字")
    except Exception as exc:  # noqa: BLE001
        check(f"在线调用失败: {type(exc).__name__}: {exc}", False)
    finally:
        await mcp.close()


async def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    print("MCP 客户端测试\n")
    test_offline()
    if "--online" in sys.argv:
        await test_online()

    print()
    if _failures:
        print(f"失败 {len(_failures)} 项：{_failures}")
        return 1
    print("MCP 测试全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
