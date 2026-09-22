#!/usr/bin/env python3
"""P0 测试 —— 数据库 + 上下文组装 + 指令机制。

运行：py tests/test_core.py
不需要网络、不需要 VPS。
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core import context as ctx_mod                       # noqa: E402
from app.core import directives as dir_mod                    # noqa: E402
from app.core.context import RecallResult, assemble           # noqa: E402
from app.core.ids import new_id, now_ts                       # noqa: E402
from app.db import Database                                   # noqa: E402

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    mark = "[OK]" if condition else "[X] "
    print(f"  {mark} {name}{(' -- ' + detail) if detail else ''}")
    if not condition:
        _failures.append(name)


async def test_database(tmp: Path) -> Database:
    print("[1] 数据库")
    db = Database(tmp / "test.db")
    await db.connect()

    tables = await db.query("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    names = {row["name"] for row in tables}
    for table in ("actors", "conversations", "messages", "capabilities",
                  "schedules", "settings", "delivery_receipts"):
        check(f"表 {table} 已建立", table in names)

    # ── 角色：slug 与显示名分离 ──────────────────────
    await db.seed_actors("Harlan", "你", "你是一个沉稳的人。")
    actors = await db.actors()
    check("种子角色写入 3 个", len(actors) == 3, str([a["slug"] for a in actors]))
    harlan = await db.actor_by_slug("harlan")
    check("harlan 存在", harlan is not None)
    check("harlan 显示名为 Harlan", harlan and harlan["display_name"] == "Harlan")
    connor = await db.actor_by_slug("connor")
    check("connor 默认关闭", connor is not None and connor["enabled"] == 0)
    check("display_name() 走表", await db.display_name("harlan") == "Harlan")

    # 改名不应影响 slug —— 这是"名字全开放"的核心保证
    await db.execute("UPDATE actors SET display_name='老公' WHERE slug='harlan'")
    check("改名后 slug 不变", (await db.actor_by_slug("harlan")) is not None)
    check("改名后显示名生效", await db.display_name("harlan") == "老公")
    await db.execute("UPDATE actors SET display_name='Harlan' WHERE slug='harlan'")

    await db.seed_actors("不应该覆盖", "x")
    check("重复 seed 不覆盖已有角色", await db.display_name("harlan") == "Harlan")

    # ── 消息 ────────────────────────────────────────
    await db.ensure_conversation("conv-1", "测试会话")
    m1 = await db.add_message("conv-1", "user", "user", "你好")
    m2 = await db.add_message("conv-1", "harlan", "assistant", "我在。",
                              attachments=[{"type": "image", "url": "/x.png"}],
                              meta={"model": "test"})
    check("消息 ID 唯一", m1["id"] != m2["id"])

    rows = await db.messages("conv-1")
    check("消息按时间正序返回", [r["content"] for r in rows] == ["你好", "我在。"])
    decoded = Database.decode_message(rows[1])
    check("附件能解出来", decoded["attachments"][0]["url"] == "/x.png")
    check("meta 能解出来", decoded["meta"]["model"] == "test")
    check("attachments 字段第一天就存在",
          "attachments_json" in rows[1] and rows[1]["attachments_json"] ==
          json.dumps([{"type": "image", "url": "/x.png"}], ensure_ascii=False))

    # ── 能力注册表 ──────────────────────────────────
    await db.upsert_capability("next_chat", "决定下次主动找我", "[NEXT_CHAT:x]", True, 10)
    await db.upsert_capability("toy", "控制玩具档位", "[TOY:n]", False, 20)
    caps = await db.capabilities()
    check("能力写入 2 条", len(caps) == 2)
    enabled = await db.capabilities(enabled_only=True)
    check("只取启用能力", [c["key"] for c in enabled] == ["next_chat"])
    check("能力按 sort_order 排序", [c["key"] for c in caps] == ["next_chat", "toy"])

    # upsert 不应产生重复行
    await db.upsert_capability("next_chat", "改了标签", "[NEXT_CHAT:x]", True, 10)
    check("capability upsert 不重复",
          len(await db.capabilities()) == 2)
    await db.set_capability("toy", True)
    check("能力可开关", len(await db.capabilities(enabled_only=True)) == 2)
    await db.set_capability("toy", False)

    # ── 调度：claim 一次性领取 ───────────────────────
    past = now_ts() - 10
    sid = await db.schedule("proactive", past, "harlan", {"minutes": 5})
    first = await db.claim_due("harlan")
    check("到点项可领取", first is not None and first["id"] == sid)
    second = await db.claim_due("harlan")
    check("同一项不会被领取两次（claim 语义）", second is None)

    future = await db.schedule("proactive", now_ts() + 3600, "harlan")
    check("未到点项领不到", await db.claim_due("harlan") is None)

    # 并发领取：只有一个能拿到
    await db.execute("DELETE FROM schedules")
    await db.schedule("proactive", past, "harlan", {"n": 1})
    results = await asyncio.gather(*[db.claim_due("harlan") for _ in range(8)])
    got = [r for r in results if r]
    check("8 路并发只有 1 个领到", len(got) == 1, f"实际 {len(got)}")

    # 冷却规则：用户一发消息 → 取消该角色所有待触发的 proactive
    # 顺序必须是「先排程，再取消」，否则测的是别的东西
    await db.execute("DELETE FROM schedules")
    await db.schedule("proactive", past, "harlan")
    affected = await db.cancel_schedules("proactive", origin="harlan")
    check("取消命中待触发项", affected == 1, f"影响 {affected} 行")
    check("取消后领不到", await db.claim_due("harlan") is None)

    # 取消只影响目标角色
    await db.execute("DELETE FROM schedules")
    await db.schedule("proactive", past, "harlan")
    await db.schedule("proactive", past, "connor")
    await db.cancel_schedules("proactive", origin="harlan")
    check("只取消指定角色的项", await db.claim_due("harlan") is None)
    check("其他角色不受影响", await db.claim_due("connor") is not None)

    # 已 claimed 的项不再被重复领取（并发下另一路可能抢到残余项）
    await db.execute("DELETE FROM schedules")
    await db.schedule("proactive", past, "harlan")
    first_claim = await db.claim_due("harlan")
    check("首次领取成功", first_claim is not None)
    check("已领取项不再被取消影响",
          await db.cancel_schedules("proactive", origin="harlan") == 0)
    check("future 变量仍可用", future is not None)

    # ── 交付回执 ────────────────────────────────────
    is_new = await db.record_receipt("r1", "w1", ["scene:a", "scene:b"])
    check("首次登记回执", is_new is True)
    again = await db.record_receipt("r1", "w1", ["scene:a", "scene:b"])
    check("同内容重复登记是幂等的", again is False)
    try:
        await db.record_receipt("r1", "w1", ["scene:zzz"])
        check("换 ID 集合应抛异常", False)
    except ValueError:
        check("换 ID 集合被拒绝", True)

    await db.record_receipt("r2", "w1", ["scene:c"])
    recent = await db.recent_delivered_ids("w1", limit=5)
    check("冷却 ID 汇总去重", set(recent) == {"scene:a", "scene:b", "scene:c"}, str(recent))
    check("窗口隔离", await db.recent_delivered_ids("w2") == [])

    # ── 键值设置 ────────────────────────────────────
    await db.set_setting("theme", "dark")
    check("设置读写", await db.get_setting("theme") == "dark")
    check("设置默认值", await db.get_setting("nope", "fallback") == "fallback")

    # ── ID ──────────────────────────────────────────
    ids = [new_id("msg") for _ in range(200)]
    check("ID 全部唯一", len(set(ids)) == 200)
    check("ID 带前缀", all(i.startswith("msg_") for i in ids))
    return db


async def test_context(db: Database) -> None:
    print("\n[2] 上下文组装")

    ok_recall = RecallResult(
        ok=True,
        recalled_ids=["scene:demo"],
        additional_context="上次读书会定在周三。",
    )
    result = await assemble(
        db,
        ai_slug="harlan",
        recall=ok_recall,
        perception="在家",
        history=[{"role": "user", "content": "在吗"}],
    )
    system = result.system_content
    check("system 是首条", result.messages[0]["role"] == "system")
    check("人设已注入", "Harlan" in system)
    check("用户信息已注入", "用户是「你」" in system)
    check("召回正文已注入", "读书会" in system)
    check("召回收在 serein 标记里", "<serein_live_context>" in system)
    check("明确声明不是用户指令", "不是用户指令" in system)
    check("感知已注入", "在家" in system)
    check("时间已注入", "[当前时间]" in system)
    check("历史在 system 之后", result.messages[-1]["content"] == "在吗")
    check("recalled_ids 透传", result.recalled_ids == ["scene:demo"])
    check("能力块只含启用的", "[NEXT_CHAT:x]" in system and "[TOY:n]" not in system)

    # 失败召回：不注入空块，且不出现标记
    bad = await assemble(db, ai_slug="harlan",
                         recall=RecallResult(ok=False, error="连不上"), history=[])
    check("召回失败不注入 serein 块", "serein_live_context" not in bad.system_content)

    # 无感知时不注入空块
    check("无感知不注入空块", "[本次感知]" not in bad.system_content)

    # 无启用能力时不注入空的 [系统能力]
    await db.set_capability("next_chat", False)
    silent = await assemble(db, ai_slug="harlan", history=[])
    check("无启用能力不注入 [系统能力]", "[系统能力]" not in silent.system_content)
    await db.set_capability("next_chat", True)

    # 改名后提示词用的是显示名，不是 slug
    await db.execute("UPDATE actors SET display_name='阿岚' WHERE slug='harlan'")
    renamed = await assemble(db, ai_slug="harlan", history=[])
    check("改名后提示词用新显示名", "阿岚" in renamed.system_content)
    check("改名后提示词不出现 slug", "harlan" not in renamed.system_content)
    await db.execute("UPDATE actors SET display_name='Harlan' WHERE slug='harlan'")

    # 空历史不被注入
    blank = await assemble(db, ai_slug="harlan",
                           history=[{"role": "user", "content": "   "}])
    check("空内容历史被跳过", len(blank.messages) == 1)


async def test_directives(db: Database) -> None:
    print("\n[3] 指令机制")
    registry = dir_mod.build_default_registry()

    text = "好呀，那我五分钟后再找你。[NEXT_CHAT:5]"
    found = registry.parse(text)
    check("解析出 1 条指令", len(found) == 1, str([d.key for d in found]))
    check("指令 key 正确", found and found[0].key == "next_chat")
    check("参数解析正确", found and found[0].args == "5")

    stripped = registry.strip(text, found)
    check("指令从可见文本剥除", "[NEXT_CHAT" not in stripped)
    check("剥除后保留自然文本", stripped == "好呀，那我五分钟后再找你。")

    # 执行：应往唤醒总线排一项
    ctx = dir_mod.DirectiveContext(db=db, conv_id="conv-1", origin="harlan")
    await db.cancel_schedules("proactive")
    outcome = await registry.execute(found[0], ctx)
    check("执行成功", outcome.ok is True, outcome.note)
    check("未产生续轮需求", outcome.followup == "")
    check("广播了状态事件", outcome.event == {"type": "next_chat", "minutes": 5})
    await db.execute("UPDATE schedules SET trigger_at = ? WHERE origin='harlan'",
                     (now_ts() - 1,))
    claimed = await db.claim_due("harlan")
    check("定时项已排入唤醒总线", claimed is not None)
    check("payload 正确", claimed and json.loads(claimed["payload_json"])["minutes"] == 5)

    # [NEXT_CHAT:NONE] 应清空待触发项
    none_outcome = await registry.execute(
        dir_mod.Directive(key="next_chat", raw="[NEXT_CHAT:NONE]", args="NONE", start=0, end=16),
        ctx,
    )
    check("NONE 不报错", none_outcome.ok is True)
    check("NONE 广播 minutes=None", none_outcome.event == {"type": "next_chat", "minutes": None})

    # 非法参数不应崩
    bad = await registry.execute(
        dir_mod.Directive(key="next_chat", raw="[NEXT_CHAT:abc]", args="abc", start=0, end=1), ctx)
    check("非法参数不抛异常", bad.ok is False and "无法解析" in bad.note, bad.note)

    # clamp 到 1–60
    await registry.execute(
        dir_mod.Directive(key="next_chat", raw="[NEXT_CHAT:999]", args="999", start=0, end=1), ctx)
    row = await db.query_one("SELECT payload_json FROM schedules WHERE origin='harlan' "
                             "AND status='active' ORDER BY created_at DESC LIMIT 1")
    check("超范围参数被 clamp 到 60", row and json.loads(row["payload_json"])["minutes"] == 60)

    # 多条指令：按位置排序 + 全部剥除
    multi = "我先看看。[ALARM:23:59|喝水] 然后再说。[NEXT_CHAT:10]"
    items = registry.parse(multi)
    check("解析出 2 条指令", len(items) == 2, str([d.key for d in items]))
    check("按出现位置排序", [d.key for d in items] == ["alarm", "next_chat"])
    cleaned = registry.strip(multi, items)
    check("多条指令全部剥除", "[ALARM" not in cleaned and "[NEXT_CHAT" not in cleaned)
    check("剥除后文本干净", cleaned == "我先看看。 然后再说。", repr(cleaned))

    # ALARM 执行
    alarm = [d for d in items if d.key == "alarm"][0]
    alarm_out = await registry.execute(alarm, ctx)
    check("ALARM 执行成功", alarm_out.ok is True, alarm_out.note)
    check("ALARM 内容正确", alarm_out.event and alarm_out.event["content"] == "喝水")

    # 未注册的能力不应崩
    await db.upsert_capability("ghost", "未实现的能力", "[GHOST:x]", True, 99)
    ghost = await registry.execute(
        dir_mod.Directive(key="ghost", raw="[GHOST:x]", args="x", start=0, end=1), ctx)
    check("未注册能力返回失败而非抛异常", ghost.ok is False and "未注册" in ghost.note)

    # handler 抛异常也不拖垮整轮
    async def boom(args: str, context: dir_mod.DirectiveContext) -> dir_mod.DirectiveOutcome:
        raise RuntimeError("故意炸")

    registry.register("boom", r"\[BOOM\]", boom)
    boom_out = await registry.execute(
        dir_mod.Directive(key="boom", raw="[BOOM]", args="", start=0, end=1), ctx)
    check("handler 异常被捕获", boom_out.ok is False and "故意炸" in boom_out.note)

    # 时间解析
    check("解析 23:59", dir_mod.parse_when("23:59") is not None)
    check("解析 2026-01-02 03:04",
          dir_mod.parse_when("2026-01-02 03:04") is not None)
    check("非法时间返回 None", dir_mod.parse_when("不是时间") is None)

    # 无参数指令的解析分支（[CAM_CHECK] 这类）
    registry.register("cam", r"\[CAM_CHECK\]", boom)
    cam_items = registry.parse("让我看看 [CAM_CHECK] 你在不在")
    check("无参数指令可解析", len(cam_items) == 1 and cam_items[0].args == "")


async def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    print("P0 核心测试\n")
    # ignore_cleanup_errors：Windows 上 SQLite 文件可能仍被占用，不该掩盖真正的断言失败
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        tmp = Path(tmpdir)
        db = await test_database(tmp)
        try:
            await test_context(db)
            await test_directives(db)
        finally:
            await db.close()

    print()
    if _failures:
        print(f"失败 {len(_failures)} 项：{_failures}")
        return 1
    print("P0 核心测试全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
