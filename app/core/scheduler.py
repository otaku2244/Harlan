"""唤醒调度器 —— 让角色能主动开口。

这是"聊天框"与"人"的分界线：没有它，Harlan 永远只能被动回复。

v0.2 §3.3 的共用骨架在这里落地：

    schedules 表：trigger_at（何时）+ origin（谁）+ status
          ↓ 每 30 秒轮询
    claim_due()：原子领取一个到期项（并发下只有一个能拿到）
          ↓
    组装上下文（人设 + 记忆召回 + 本次感知 = "为什么现在开口"）
          ↓
    流式调模型 → 剥离指令 → 落库 → WebSocket 广播 → 投递到该 origin 的窗口
          ↓
    排下一次（模型自决优先，否则随机间隔）

两类唤醒：

    proactive  主动陪伴计时器 —— 模型上一轮用 [NEXT_CHAT:x] 自己定的时间
    idle       空闲自主     —— 用户长时间没说话，随机间隔到了
    alarm      闹铃         —— 到点必须开口，带 payload.content
    reminder   日程提醒     —— 同上

**冷却规则**（照 AionsHome）：用户一发消息就取消该角色的全部 proactive 计时器，
避免"刚聊完又冒头"。这条在 routes 里执行（见 app/main.py 的 chat 处理）。

为什么随机间隔放在这里而不是模型：
    「随机要真」——间隔来自随机数，**选择做什么**才交给模型。
    这正是 v0.2 原则 4 的落地方式。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import time
from collections.abc import Awaitable, Callable

from app.adapters.model import ModelClient
from app.adapters.serein import SereinMemory
from app.config import Settings, settings as default_settings
from app.core import context as context_mod
from app.core.directives import DirectiveContext, DirectiveRegistry
from app.db import Database
from app.ws import ConnectionManager

# 空闲自主的动作菜单（照 AionsHome autonomy.py 的 ACTION_DEFS，只保留 VPS 可跑的）
IDLE_ACTIONS: dict[str, str] = {
    "rest": "什么都不做，继续休息（本次不打扰用户）",
    "private_chat": "主动联系用户，说点此刻想说的话",
    "memory_browse": "翻看一段旧记忆，然后决定要不要提起",
    "web_roam": "上网看看感兴趣的东西（需要联网搜索能力）",
    "home_dynamics": "查看近期家庭动态",
}


class WakeScheduler:
    """后台唤醒循环。

    依赖注入的 `clock` 与 `sleep` 让测试可以完全脱离真实时间。
    """

    def __init__(
        self,
        db: Database,
        memory: SereinMemory,
        model: ModelClient,
        registry: DirectiveRegistry,
        ws: ConnectionManager,
        settings: Settings | None = None,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.db = db
        self.memory = memory
        self.model = model
        self.registry = registry
        self.ws = ws
        self.settings = settings or default_settings
        self._clock = clock
        self._sleep = sleep
        self._rng = rng or random.Random()

        self._task: asyncio.Task | None = None
        self._running = False
        self.EXCEPTION_BACKOFF = 60.0

    # ── 生命周期 ────────────────────────────────────

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            task, self._task = self._task, None
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _loop(self) -> None:
        """轮询循环。异常不能让它死掉 —— 一次失败不该让角色从此哑掉。"""
        interval = max(5.0, float(self.settings.scheduler_poll_seconds))
        while self._running:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                print(f"[scheduler] tick 失败 {type(exc).__name__}: {exc}", flush=True)
                with contextlib.suppress(asyncio.CancelledError):
                    await self._sleep(self.EXCEPTION_BACKOFF)
                continue
            with contextlib.suppress(asyncio.CancelledError):
                await self._sleep(interval)

    # ── 一轮 ────────────────────────────────────────

    async def tick(self) -> list[dict]:
        """检查所有启用角色，领取并处理到期项。返回本次触发的记录。"""
        fired: list[dict] = []
        for actor in await self.db.actors(kind="ai", enabled_only=True):
            slug = actor["slug"]
            # 一个 tick 内可能有多项同时到期，逐项领取直到没有
            while True:
                item = await self.db.claim_due(slug, now=self._clock())
                if item is None:
                    break
                try:
                    result = await self._fire(item, actor)
                except Exception as exc:  # noqa: BLE001
                    result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                    if item["type"] != "idle":
                        await self._reschedule_idle(slug)
                fired.append({"schedule_id": item["id"], "type": item["type"], **result})
        return fired

    async def _fire(self, item: dict, actor: dict) -> dict:
        """处理一个到期项：组装 → 生成 → 落库 → 广播 → 排下一次。"""
        slug = actor["slug"]
        display = actor["display_name"]
        window_id = f"{self.settings.wake_window_prefix}:{slug}"
        kind = item["type"]
        payload = self._payload(item)
        decision = self._decide_action(kind, item, payload)

        # rest 是"什么都不做"：记录一次决策，但不打扰用户
        if decision["action"] == "rest":
            await self._broadcast("wake_rest", {"actor": slug, "reason": decision["reason"]})
            await self._reschedule_idle(slug)
            return {"ok": True, "spoke": False, "action": "rest", "reason": decision["reason"]}

        perception = decision["perception"]
        recall = await self.memory.recall(decision["query"], window_id)
        assembled = await context_mod.assemble(
            self.db,
            ai_slug=slug,
            recall=recall,
            perception=perception,
            history=[{"role": "user", "content": decision["instruction"]}],
        )

        await self._broadcast("wake_start", {
            "actor": slug, "display_name": display, "type": kind,
            "reason": decision["reason"], "minutes": payload.get("minutes"),
        })

        text = ""
        try:
            async for piece in self.model.stream(assembled.messages,
                                                 max_tokens=self.settings.wake_max_tokens):
                text += piece
                await self._broadcast("wake_delta", {"actor": slug, "text": piece})
        except Exception as exc:  # noqa: BLE001
            await self._broadcast("wake_error", {"actor": slug, "error": str(exc)})
            await self._reschedule_idle(slug)
            return {"ok": False, "error": str(exc), "action": decision["action"]}

        directives = self.registry.parse(text)
        visible = self.registry.strip(text, directives)
        await self._execute(directives, slug)

        if not visible:
            await self._broadcast("wake_empty", {"actor": slug})
            await self._reschedule_idle(slug)
            return {"ok": True, "spoke": False, "action": decision["action"]}

        saved = await self.db.add_message(
            self.settings.wake_conv_id, slug, "assistant", visible,
            meta={
                "wake": {"type": kind, "reason": decision["reason"], "action": decision["action"]},
                "recalled_ids": list(recall.recalled_ids),
            },
        )
        await self.ws.broadcast_event("message", {
            **Database.decode_message(saved), "display_name": display,
            "proactive": True,
        })

        # 登记记忆交付（模型已完整输出，且这条会真的展示给用户）
        if recall.recalled_ids:
            await self.memory.record_delivery(
                window_id, list(recall.recalled_ids), int(self._clock()), visible
            )

        scheduled = self._model_set_next(directives)
        await self._reschedule_idle(slug, model_decision=scheduled)
        return {
            "ok": True, "spoke": True, "action": decision["action"],
            "chars": len(visible), "next_from_model": scheduled,
        }

    # ── 决策 ────────────────────────────────────────

    def _decide_action(self, kind: str, item: dict, payload: dict) -> dict:
        """决定这次唤醒做什么，以及"为什么现在开口"的感知文本。"""
        if kind == "alarm":
            content = payload.get("content") or "（未指定内容）"
            return {
                "action": "alarm",
                "reason": f"闹铃：{content}",
                "query": content,
                "perception": f"[到点事件]\n你设的闹铃到时间了。当时记下的内容是：{content}",
                "instruction": "闹铃到点了。用你自己的方式开口，直接对他说，不要解释这是系统触发。",
            }
        if kind == "reminder":
            content = payload.get("content") or "（未指定内容）"
            return {
                "action": "reminder",
                "reason": f"日程提醒：{content}",
                "query": content,
                "perception": f"[到点事件]\n你之前设的日程提醒到时间了：{content}",
                "instruction": "提醒时间到了。自然地提起来，不要写成系统通知。",
            }
        if kind == "proactive":
            minutes = payload.get("minutes")
            # 手动触发（POST /api/wake）不带 minutes，别渲染成 "None 分钟"
            if isinstance(minutes, int):
                reason = f"你自己定的 {minutes} 分钟后回来说话"
            else:
                reason = "到点回来看看他"
            return {
                "action": "private_chat",
                "reason": reason,
                "query": "",
                "perception": (
                    "[到点事件]\n这是你自己上一轮决定的——过一会儿再找他。"
                    "现在时间到了，你可以决定要不要开口，以及说什么。"
                ),
                "instruction": (
                    "现在是你自己安排的时间到了。结合最近的对话，说点此刻真想说的。"
                    "如果这一刻其实没什么想说的，只回一个 [NEXT_CHAT:NONE]，不必勉强。"
                ),
            }

        # idle：空闲自主，把动作菜单交给模型，但 rest 只能由这里判定
        menu = "\n".join(f"    - {k}: {v}" for k, v in IDLE_ACTIONS.items())
        return {
            "action": "idle",
            "reason": "空闲自主唤醒",
            "query": "",
            "perception": (
                "[空闲自主]\n用户已经有一段时间没说话了，你可能在忙，也可能在休息。\n"
                "你被自主意识唤醒，可以做点什么，也可以什么都不做。\n"
                f"可选的事：\n{menu}"
            ),
            "instruction": (
                "你被自主意识唤醒。从上面的事里选一件你真的想做的。\n"
                "如果决定什么都不做，只回 [NEXT_CHAT:NONE]。\n"
                "如果决定找他说话，就直接说你此刻想说的话，不要说明这是自动唤醒。"
            ),
        }

    def _model_set_next(self, directives: list) -> int | None:
        """模型在本次输出里有没有用 [NEXT_CHAT:x] 自己定下次时间。"""
        for directive in directives:
            if directive.key != "next_chat":
                continue
            value = (directive.args or "").strip().upper()
            if value in {"NONE", ""}:
                return None
            try:
                return max(1, min(60, int(value)))
            except ValueError:
                return None
        return None

    # ── 重排下一次 ──────────────────────────────────

    async def _reschedule_idle(self, slug: str, model_decision: int | None = None) -> float:
        """排下一次空闲自主。

        优先级：模型自决 > 配置的随机区间。
        「随机要真」：间隔由 random 决定，选择做什么才交给模型。
        """
        existing = await self.db.query_one(
            "SELECT id FROM schedules WHERE origin = ? AND type = 'proactive' "
            "AND status = 'active' LIMIT 1",
            (slug,),
        )
        if existing is not None:
            return 0.0  # 模型已经排好了，别覆盖它

        if model_decision is not None:
            delay = model_decision * 60.0
        else:
            low = max(5, self.settings.idle_min_minutes)
            high = max(low, self.settings.idle_max_minutes)
            delay = self._rng.randint(low, high) * 60.0

        trigger_at = self._clock() + delay
        await self.db.schedule("idle", trigger_at, slug, {"delay_seconds": int(delay)})
        return trigger_at

    async def run_actor_now(self, slug: str, kind: str = "proactive",
                            payload: dict | None = None) -> dict:
        """立刻让某个角色醒一次（手动触发，用于调试和测试）。

        走的是和后台循环**完全相同的路径**（排程 → 领取 → 生成 → 广播），
        所以手动跑的结果能代表真实行为。
        """
        actor = await self.db.actor_by_slug(slug)
        if actor is None:
            return {"ok": False, "error": f"没有角色 {slug}"}
        schedule_id = await self.db.schedule(kind, self._clock(), slug, payload or {})
        item = await self.db.query_one("SELECT * FROM schedules WHERE id = ?", (schedule_id,))
        if item is None:
            return {"ok": False, "error": "排程失败"}
        return await self._fire(item, actor)

    async def _execute(self, directives: list, slug: str) -> None:
        ctx = DirectiveContext(db=self.db, conv_id=self.settings.wake_conv_id,
                               origin=slug, turn=int(self._clock()))
        for directive in directives:
            outcome = await self.registry.execute(directive, ctx)
            if outcome.event:
                await self._broadcast("capability", {**outcome.event, "actor": slug})

    # ── 工具 ────────────────────────────────────────

    @staticmethod
    def _payload(item: dict) -> dict:
        try:
            value = json.loads(item.get("payload_json") or "{}")
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            return {}

    async def _broadcast(self, type_: str, data: dict) -> None:
        await self.ws.broadcast_event(type_, data)
