#!/usr/bin/env python3
"""P3 调度器测试 —— 不联网、不用真实时间。

覆盖：
  * 到期项被领取并触发主动发言
  * 模型没自定时间 → 按配置的随机区间排下一次
  * 模型用 [NEXT_CHAT:x] 自定 → 不覆盖它
  * rest 动作 → 不打扰用户，但仍然排下一次
  * 未到点不触发；并发只有一个领到
  * 指令被剥离、能力被登记
  * 异常不拖垮循环

运行：py tests/test_scheduler.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Settings                       # noqa: E402
from app.core.directives import build_default_registry  # noqa: E402
from app.core.scheduler import WakeScheduler          # noqa: E402
from app.db import Database                           # noqa: E402
from app.ws import ConnectionManager                  # noqa: E402

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    mark = "[OK]" if condition else "[X] "
    print(f"  {mark} {name}{(' -- ' + detail) if detail else ''}")
    if not condition:
        _failures.append(name)


class FakeMemory:
    """假记忆服务：可控制召回结果，记录登记调用。"""

    def __init__(self, ids: list[str] | None = None, context: str = "") -> None:
        self.ids = ids or []
        self.context = context
        self.recall_calls: list[tuple[str, str]] = []
        self.deliveries: list[tuple[str, list[str]]] = []
        self.fail = False

    async def recall(self, query, window_id, **kwargs):
        from app.core.context import RecallResult
        self.recall_calls.append((query, window_id))
        if self.fail:
            return RecallResult(ok=False, error="模拟召回失败")
        return RecallResult(ok=bool(self.ids), recalled_ids=list(self.ids),
                            additional_context=self.context)

    async def record_delivery(self, window_id, ids, turn, text):
        self.deliveries.append((window_id, list(ids)))
        return {"ok": True, "receipt_id": "fake"}


class FakeModel:
    def __init__(self, script: list[list[str]] | None = None) -> None:
        self.script = list(script or [])
        self.calls: list[list[dict]] = []
        self.fail_next = False

    def push(self, chunks: list[str]) -> None:
        self.script.append(chunks)

    async def stream(self, messages, **kwargs):
        self.calls.append(messages)
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("模拟模型异常")
        chunks = self.script.pop(0) if self.script else ["（默认）"]
        for piece in chunks:
            yield piece


class FakeClock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def make_env(tmp: Path, **settings_overrides):
    # 每个用例必须用自己的子目录，否则共用同一份 db 文件会互相污染
    tmp = Path(tmp)
    tmp.mkdir(parents=True, exist_ok=True)
    db = Database(tmp / "sched.db")
    await db.connect()
    await db.seed_actors("Harlan", "你")
    settings = Settings()
    settings.scheduler_enabled = False
    settings.scheduler_poll_seconds = 5
    settings.idle_min_minutes = 60
    settings.idle_max_minutes = 60
    settings.wake_conv_id = "harlan"
    for key, value in settings_overrides.items():
        setattr(settings, key, value)
    clock = FakeClock()
    memory = FakeMemory()
    model = FakeModel()
    ws = ConnectionManager()
    scheduler = WakeScheduler(db, memory, model, build_default_registry(), ws, settings,
                              clock=clock, sleep=lambda _s: asyncio.sleep(0))
    return db, settings, clock, memory, model, ws, scheduler


async def test_proactive_wake(tmp: Path) -> None:
    print("\n[1] proactive 唤醒：模型自决时间到点后主动开口")
    db, settings, clock, memory, model, ws, scheduler = await make_env(tmp)
    memory.ids = ["scene:demo"]
    memory.context = "上次约好周三见面。"

    await db.schedule("proactive", clock() - 1, "harlan", {"minutes": 10})
    model.push(["我刚才在整理书架，突然想到⸺你上次说的那件事有着落了吗？"])

    fired = await scheduler.tick()
    check("有 1 项被触发", len(fired) == 1, str(fired))
    check("触发了主动发言", fired and fired[0].get("spoke") is True, str(fired))
    check("动作是 private_chat", fired and fired[0].get("action") == "private_chat")

    rows = await db.messages("harlan")
    check("消息已落库", len(rows) == 1, f"实际 {len(rows)}")
    check("落库角色正确", rows and rows[0]["sender"] == "harlan")
    check("内容是模型输出", rows and "书架" in rows[0]["content"])
    meta = Database.decode_message(rows[0])["meta"]
    check("meta 标注了唤醒信息", meta.get("wake", {}).get("type") == "proactive", str(meta))
    check("meta 记录了召回 ID", meta.get("recalled_ids") == ["scene:demo"])

    check("召回被调用", len(memory.recall_calls) == 1)
    check("窗口 ID 稳定", memory.recall_calls[0][1] == "wake:harlan",
          memory.recall_calls[0][1])
    check("记忆已登记交付", len(memory.deliveries) == 1 and memory.deliveries[0][1] == ["scene:demo"])

    # 提示词里应包含"为什么现在开口"
    system = model.calls[0][0]["content"]
    check("提示词含到点原因", "自己安排的时间到了" in system or "到点事件" in system,
          system[:120])
    check("提示词含召回内容", "周三见面" in system)

    await db.close()


async def test_reschedule(tmp: Path) -> None:
    print("\n[2] 重排下一次：模型自定优先，否则随机区间")
    db, settings, clock, memory, model, ws, scheduler = await make_env(tmp)

    # 模型没自定时间 → 用配置区间（60 分钟）
    await db.schedule("proactive", clock() - 1, "harlan", {})
    model.push(["我在。"])
    await scheduler.tick()
    next_idle = await db.query_one(
        "SELECT * FROM schedules WHERE type='idle' AND status='active' ORDER BY created_at DESC LIMIT 1"
    )
    check("排出了下一次 idle", next_idle is not None)
    if next_idle:
        delay = json.loads(next_idle["payload_json"])["delay_seconds"]
        check("延迟等于配置区间", delay == 60 * 60, f"{delay}s")
        check("触发时间在未来", next_idle["trigger_at"] > clock())

    # 模型自定 15 分钟 → 应生成 proactive，而不是 idle
    await db.execute("DELETE FROM schedules")
    await db.schedule("proactive", clock() - 1, "harlan", {})
    model.push(["那我十五分钟后再找你。[NEXT_CHAT:15]"])
    await scheduler.tick()
    rows = await db.query("SELECT type, payload_json, status FROM schedules WHERE status='active'")
    types = sorted(r["type"] for r in rows)
    check("模型自定生成了 proactive", "proactive" in types, str(rows))
    proactive = [r for r in rows if r["type"] == "proactive"]
    if proactive:
        check("自定时间为 15 分钟",
              json.loads(proactive[0]["payload_json"]).get("minutes") == 15,
              proactive[0]["payload_json"])
    idles = [r for r in rows if r["type"] == "idle"]
    check("不再额外排 idle（不覆盖模型决定）", len(idles) == 0, str(rows))

    await db.close()


async def test_rest_action(tmp: Path) -> None:
    print("\n[3] rest：什么都不做，不打扰用户")
    db, settings, clock, memory, model, ws, scheduler = await make_env(tmp)
    await db.schedule("idle", clock() - 1, "harlan", {})
    model.push(["[NEXT_CHAT:NONE]"])

    fired = await scheduler.tick()
    rows = await db.messages("harlan")
    check("没有产生消息（不打扰）", len(rows) == 0, f"实际 {len(rows)}")
    check("本次不发言", fired and fired[0].get("spoke") is False, str(fired))
    check("仍然排了下一次", await db.query_one(
        "SELECT id FROM schedules WHERE status='active' AND type='idle'") is not None)

    # 模型在 idle 里说 [NEXT_CHAT:NONE] 应被识别为"不想说话"
    print("  （模型用 NONE 表达不想说话）")
    await db.close()


async def test_not_due_and_concurrent(tmp: Path) -> None:
    print("\n[4] 未到点 / 并发领取")
    db, settings, clock, memory, model, ws, scheduler = await make_env(tmp)

    await db.schedule("proactive", clock() + 3600, "harlan", {})
    fired = await scheduler.tick()
    check("未到点不触发", fired == [], str(fired))

    await db.execute("DELETE FROM schedules")
    await db.schedule("proactive", clock() - 1, "harlan", {})
    model.push(["并发测试。"])
    results = await asyncio.gather(scheduler.tick(), scheduler.tick())
    total = sum(len(r) for r in results)
    check("并发只触发一次", total == 1, f"实际 {total}")
    rows = await db.messages("harlan")
    check("只产生一条消息", len(rows) == 1, f"实际 {len(rows)}")

    await db.close()


async def test_directive_and_persona(tmp: Path) -> None:
    print("\n[5] 指令剥离与角色名")
    db, settings, clock, memory, model, ws, scheduler = await make_env(tmp)
    await db.execute("UPDATE actors SET display_name='阿岚' WHERE slug='harlan'")
    await db.schedule("alarm", clock() - 1, "harlan", {"content": "该吃药了"})
    model.push(["到点了，记得吃药。顺便我二十分钟后再看看你。[NEXT_CHAT:20]"])

    fired = await scheduler.tick()
    rows = await db.messages("harlan")
    content = rows[0]["content"]
    check("指令不在可见文本里", "[NEXT_CHAT" not in content, content)
    check("自然文本保留", "记得吃药" in content)
    check("alarm 也会排下一次", await db.query_one(
        "SELECT id FROM schedules WHERE status='active' AND type='proactive'") is not None)
    check("alarm 动作被识别", fired and fired[0].get("action") == "alarm", str(fired))
    check("提示词用显示名而非 slug",
          "阿岚" in model.calls[0][0]["content"] and "harlan" not in model.calls[0][0]["content"])

    # 未注册的能力不应崩
    await db.execute("DELETE FROM schedules")
    await db.schedule("reminder", clock() - 1, "harlan", {"content": "开会"})
    model.push(["该开会了。[TOY:5]"])       # TOY 未注册
    fired = await scheduler.tick()
    check("未注册能力不影响发言", fired and fired[0].get("spoke") is True, str(fired))

    await db.close()


async def test_resilience(tmp: Path) -> None:
    print("\n[6] 抗故障")
    db, settings, clock, memory, model, ws, scheduler = await make_env(tmp)

    # 模型抛异常
    await db.schedule("proactive", clock() - 1, "harlan", {})
    model.fail_next = True
    fired = await scheduler.tick()
    check("模型异常被捕获", fired and fired[0]["ok"] is False, str(fired))
    check("异常后仍排了下一次", await db.query_one(
        "SELECT id FROM schedules WHERE status='active'") is not None)

    # 召回失败仍能开口
    await db.execute("DELETE FROM schedules")
    memory.fail = True
    await db.schedule("proactive", clock() - 1, "harlan", {})
    model.push(["我在这儿。"])
    fired = await scheduler.tick()
    check("记忆不可用仍能主动开口", fired and fired[0].get("spoke") is True, str(fired))
    check("未登记召回（因为没有召回）", len(memory.deliveries) == 0)

    # tick 异常不应让循环退出
    await db.close()


async def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    print("P3 调度器测试")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        root = Path(tmpdir)
        await test_proactive_wake(root / "t1")
        await test_reschedule(root / "t2")
        await test_rest_action(root / "t3")
        await test_not_due_and_concurrent(root / "t4")
        await test_directive_and_persona(root / "t5")
        await test_resilience(root / "t6")

    print()
    if _failures:
        print(f"失败 {len(_failures)} 项：{_failures}")
        return 1
    print("P3 调度器测试全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
