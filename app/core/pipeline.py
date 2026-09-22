"""对话主流程 —— 把记忆、上下文、模型、指令、落库串成一条线。

这是 P0 的核心：一次 `run_turn` 走完

    召回 → 组装 → 流式生成 → 剥离指令 → 执行 → 续轮 → 落库 → 登记交付

它以**异步事件流**的形式产出结果，HTTP 路由负责把事件转成 SSE、
同时广播到 WebSocket。这样"可重放"和"多端同步"是同一份数据。

为什么把指令续轮放在这里而不是路由里：
续轮要重新组装上下文（把指令执行结果作为新材料回灌），这是编排逻辑，不是传输逻辑。
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from app.adapters.model import ModelClient, ModelError
from app.adapters.serein import SereinMemory
from app.config import Settings, settings as default_settings
from app.core import context as context_mod
from app.core.context import RecallResult
from app.core.directives import DirectiveContext, DirectiveRegistry
from app.db import Database


@dataclass
class TurnEvent:
    """主流程产出的一个事件。`type` 与 WebSocket 协议一致。"""

    type: str
    data: dict = field(default_factory=dict)


@dataclass
class TurnResult:
    """一次完整对话的结果，供调用方落库/广播。"""

    visible_text: str = ""
    recalled_ids: list[str] = field(default_factory=list)
    directive_rounds: int = 0
    capability_events: list[dict] = field(default_factory=list)
    recall_ok: bool = False
    error: str = ""


class ChatPipeline:
    def __init__(
        self,
        db: Database,
        memory: SereinMemory,
        model: ModelClient,
        registry: DirectiveRegistry,
        settings: Settings | None = None,
    ) -> None:
        self.db = db
        self.memory = memory
        self.model = model
        self.registry = registry
        self.settings = settings or default_settings

    async def recall_for_turn(
        self, window_id: str, query: str
    ) -> RecallResult:
        """召回。失败一律降级为空结果 —— 记忆不可用不该让对话发不出去。"""
        return await self.memory.recall(query, window_id)

    async def run_turn(
        self,
        *,
        conv_id: str,
        ai_slug: str,
        window_id: str,
        user_text: str,
        perception: str = "",
        turn: int = 0,
    ) -> AsyncIterator[TurnEvent]:
        """执行一轮完整对话，逐步产出事件。

        事件顺序：
            recall_ok / recall_empty
            stream_start
            stream_delta × N
            capability（若有指令执行）
            stream_end
            (续轮时重复 stream_start → stream_end)
            delivery
            done
        """
        result = TurnResult()

        # ── 1. 记忆召回 ──────────────────────────────
        recall = await self.recall_for_turn(window_id, user_text)
        result.recall_ok = recall.ok
        result.recalled_ids = list(recall.recalled_ids)
        if recall.ok:
            yield TurnEvent("recall", {"ok": True, "count": len(recall.recalled_ids),
                                       "ids": recall.recalled_ids})
        else:
            reason = recall.error or recall.skipped or "未知原因"
            yield TurnEvent("recall", {"ok": False, "reason": reason})

        # ── 2. 取历史（不含本轮，本轮由 user_text 传入）──
        history_rows = await self.db.messages(conv_id, limit=self.settings.history_limit)
        history = [
            {"role": row["role"], "content": row["content"]}
            for row in history_rows
            if row["content"].strip()
        ]
        history.append({"role": "user", "content": user_text})

        # ── 3. 生成 + 指令续轮 ───────────────────────
        visible_parts: list[str] = []
        followup_material = ""
        rounds = 0

        while True:
            assembled = await context_mod.assemble(
                self.db,
                ai_slug=ai_slug,
                recall=recall,
                perception=perception if rounds == 0 else "",
                history=history,
            )

            if rounds == 0:
                yield TurnEvent("stream_start", {"actor": ai_slug})

            try:
                raw = ""
                async for piece in self.model.stream(assembled.messages):
                    raw += piece
                    yield TurnEvent("stream_delta", {"text": piece})
            except ModelError as exc:
                result.error = str(exc)
                yield TurnEvent("error", {"message": str(exc), "fatal": True})
                # 保留已产出的内容，不丢
                if raw.strip():
                    visible_parts.append(self.registry.strip(raw, self.registry.parse(raw)))
                break

            directives = self.registry.parse(raw)
            visible = self.registry.strip(raw, directives)
            visible_parts.append(visible)

            if not directives:
                break

            if rounds + 1 > self.settings.max_directive_rounds:
                result.directive_rounds = rounds
                yield TurnEvent("error", {"message": "指令续轮超过上限，已停止", "fatal": False})
                break

            # 执行指令；把结果作为下一轮的新材料
            ctx = DirectiveContext(db=self.db, conv_id=conv_id, origin=ai_slug, turn=turn)
            materials: list[str] = []
            for directive in directives:
                outcome = await self.registry.execute(directive, ctx)
                if outcome.event:
                    result.capability_events.append(outcome.event)
                    yield TurnEvent("capability", outcome.event)
                if outcome.note:
                    yield TurnEvent("capability_note", {"key": directive.key, "note": outcome.note,
                                                        "ok": outcome.ok})
                if outcome.followup:
                    materials.append(outcome.followup)

            if not materials:
                # 没有需要回灌的材料（例如 [NEXT_CHAT:5] 只是排程）——不必续轮。
                # 注意 rounds 不能在这里 +1：没续轮就是没续轮，计数要如实反映。
                break

            rounds += 1
            result.directive_rounds = rounds

            followup_material = "\n\n".join(materials)
            history.append({"role": "assistant", "content": visible})
            history.append({
                "role": "user",
                "content": "[系统] 上一条指令的执行结果如下，请基于它继续回复，"
                           "不要重复你已经说过的话：\n" + followup_material,
            })
            yield TurnEvent("stream_start", {"actor": ai_slug, "round": rounds + 1})

        result.visible_text = "\n".join(p for p in visible_parts if p).strip()
        yield TurnEvent("stream_end", {"actor": ai_slug, "text": result.visible_text,
                                       "rounds": result.directive_rounds})

        # ── 4. 登记交付（只在模型完整成功后）─────────
        if result.visible_text and result.recalled_ids and not result.error:
            delivery = await self.memory.record_delivery(
                window_id, result.recalled_ids, turn, user_text
            )
            yield TurnEvent("delivery", delivery)

        yield TurnEvent("done", {
            "visible_text": result.visible_text,
            "recalled_ids": result.recalled_ids,
            "directive_rounds": result.directive_rounds,
            "capability_events": result.capability_events,
            "error": result.error,
        })


def turn_index(history_rows: list[dict]) -> int:
    """用历史条数当轮次号。仅用于回执 ID 的稳定性，不要求精确。"""
    return len(history_rows) + 1
