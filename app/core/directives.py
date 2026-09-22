"""指令解析与执行 —— P0 不可妥协的第三条：预留"指令 → 执行 → 续轮"。

AionsHome 的能力机制是这样的：

    模型输出 [WEB_SEARCH:今天的新闻]
      → 后端把指令**从给用户看的文本里剥离**（不能读出来）
      → 执行（搜索）
      → 结果作为下一条上下文回灌
      → 再调一次模型，让它基于结果说人话（"续轮"）

玩具/摄像头/搜索全靠这个骨架。现在只注册少量能力，但骨架必须在。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable

# handler 签名：async (args: str, ctx: DirectiveContext) -> DirectiveOutcome
DirectiveHandler = Callable[[str, "DirectiveContext"], Awaitable["DirectiveOutcome"]]


@dataclass
class Directive:
    key: str            # 'web_search' / 'next_chat' / ...
    raw: str            # 完整匹配文本，用于从可见文本里剥除
    args: str           # 冒号后的内容；无参数的指令为空串
    start: int
    end: int


@dataclass
class DirectiveOutcome:
    """一次指令执行的结果。"""

    # 需要回灌给模型的内容。为空表示不需要续轮（如 [NEXT_CHAT:5] 只是排个程）
    followup: str = ""
    # 需要广播给前端的状态，例如 {"type": "web_search", "query": "..."}
    event: dict | None = None
    # 是否算执行成功（失败时也记账，便于诊断）
    ok: bool = True
    note: str = ""


@dataclass
class DirectiveContext:
    """执行 handler 时能拿到的东西。故意保持窄：handler 不该直接碰模型或 WS。"""

    db: object = None
    conv_id: str = ""
    origin: str = ""            # 哪个角色输出的（actors.slug）
    turn: int = 0
    extras: dict = field(default_factory=dict)


class DirectiveRegistry:
    """能力注册表。**加能力 = register() 一次，不改解析器和主循环。**

    注意与数据库 `capabilities` 表的分工：
      * 表决定"这个能力现在**对模型可见**吗"（提示词里要不要列出）
      * 注册表决定"遇到这个指令**怎么执行**"
    两者都可缺省：表里启用但没有 handler，执行阶段会记录一条 note 而不是崩。
    """

    def __init__(self) -> None:
        self._handlers: dict[str, tuple[re.Pattern[str], DirectiveHandler]] = {}

    def register(self, key: str, pattern: str, handler: DirectiveHandler) -> None:
        self._handlers[key] = (re.compile(pattern, re.IGNORECASE | re.DOTALL), handler)

    def keys(self) -> list[str]:
        return sorted(self._handlers)

    def has(self, key: str) -> bool:
        return key in self._handlers

    def parse(self, text: str) -> list[Directive]:
        """找出文本里所有已注册的指令，按出现位置排序。"""
        found: list[Directive] = []
        for key, (pattern, _) in self._handlers.items():
            for match in pattern.finditer(text):
                args = (match.group(1) if match.groups() else "") or ""
                found.append(
                    Directive(
                        key=key,
                        raw=match.group(0),
                        args=args.strip(),
                        start=match.start(),
                        end=match.end(),
                    )
                )
        found.sort(key=lambda d: (d.start, d.end))

        # 去掉重叠匹配（例如两个 handler 的模式都能匹配同一段），保留靠前的
        deduped: list[Directive] = []
        cursor = -1
        for item in found:
            if item.start >= cursor:
                deduped.append(item)
                cursor = item.end
        return deduped

    @staticmethod
    def strip(text: str, directives: list[Directive]) -> str:
        """把指令从给用户看的文本里剥掉，并清理多余空白。

        模型不能把自己的工具指令念给用户听——AionsHome 专门写了流式过滤器做这件事。
        """
        if not directives:
            return text
        out, cursor = [], 0
        for item in directives:
            out.append(text[cursor:item.start])
            cursor = item.end
        out.append(text[cursor:])
        cleaned = "".join(out)
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        return cleaned.strip()

    async def execute(
        self, directive: Directive, ctx: DirectiveContext
    ) -> DirectiveOutcome:
        entry = self._handlers.get(directive.key)
        if entry is None:
            return DirectiveOutcome(ok=False, note=f"能力 {directive.key} 未注册")
        try:
            return await entry[1](directive.args, ctx)
        except Exception as exc:  # noqa: BLE001 — 单个能力失败不能拖垮整轮对话
            return DirectiveOutcome(ok=False, note=f"{directive.key} 执行失败: {type(exc).__name__}: {exc}")


# ─────────────────────────────────────────────────────────────
# 内置能力
# ─────────────────────────────────────────────────────────────

async def handle_next_chat(args: str, ctx: DirectiveContext) -> DirectiveOutcome:
    """[NEXT_CHAT:x] / [NEXT_CHAT:NONE] —— 主动陪伴计时器。

    这是 v0.2 §3.2 的机制②：模型自己决定"几分钟后再找我"。
    不产生 followup（不需要续轮），只是往唤醒总线排一个 trigger_at。
    到点由调度器 claim → 组装上下文 → 调模型 → 广播。
    """
    from app.core.ids import now_ts

    value = (args or "").strip().upper()
    db = ctx.db
    if db is None:
        return DirectiveOutcome(ok=False, note="没有数据库上下文")

    # 先清掉这个角色所有待触发的 proactive，避免叠加
    await db.cancel_schedules("proactive", origin=ctx.origin)

    if value in {"NONE", ""}:
        return DirectiveOutcome(event={"type": "next_chat", "minutes": None})

    try:
        minutes = int(value)
    except ValueError:
        return DirectiveOutcome(ok=False, note=f"无法解析 NEXT_CHAT 参数 {args!r}")

    minutes = max(1, min(60, minutes))      # 原项目 clamp 到 1–60 分钟
    trigger_at = now_ts() + minutes * 60
    await db.schedule("proactive", trigger_at, ctx.origin, {"minutes": minutes})
    return DirectiveOutcome(event={"type": "next_chat", "minutes": minutes})


async def handle_alarm(args: str, ctx: DirectiveContext) -> DirectiveOutcome:
    """[ALARM:datetime|内容] —— 闹铃。到点由调度器唤醒并开口。"""
    from app.core.ids import now_ts

    if ctx.db is None:
        return DirectiveOutcome(ok=False, note="没有数据库上下文")
    raw = (args or "").strip()
    if "|" not in raw:
        return DirectiveOutcome(ok=False, note="ALARM 需要 datetime|内容 格式")
    when_text, _, content = raw.partition("|")
    trigger_at = parse_when(when_text.strip())
    if trigger_at is None:
        return DirectiveOutcome(ok=False, note=f"无法解析时间 {when_text!r}")
    if trigger_at <= now_ts():
        return DirectiveOutcome(ok=False, note="闹铃时间已过去")
    await ctx.db.schedule("alarm", trigger_at, ctx.origin, {"content": content.strip()})
    return DirectiveOutcome(event={"type": "alarm_set", "at": trigger_at, "content": content.strip()})


def parse_when(text: str) -> float | None:
    """解析时间。支持 `YYYY-MM-DD HH:MM` 与 `HH:MM`（今天/明天）。解析失败返回 None。"""
    import datetime as dt

    text = (text or "").strip()
    now = dt.datetime.now()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M"):
        try:
            return dt.datetime.strptime(text, fmt).timestamp()
        except ValueError:
            continue
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            parsed = dt.datetime.strptime(text, fmt).time()
        except ValueError:
            continue
        candidate = now.replace(hour=parsed.hour, minute=parsed.minute,
                                second=getattr(parsed, "second", 0), microsecond=0)
        if candidate <= now:                 # 已过点则算明天
            candidate += dt.timedelta(days=1)
        return candidate.timestamp()
    return None


def build_default_registry() -> DirectiveRegistry:
    """注册内置能力。注释掉的能力是"骨架已备，实现待搬"。"""
    registry = DirectiveRegistry()
    registry.register("next_chat", r"\[NEXT_CHAT:([^\]\n]*)\]", handle_next_chat)
    registry.register("alarm", r"\[ALARM:([^\]\n]+)\]", handle_alarm)
    # 待搬（P5）：模式先留着，说明骨架能承载它们
    # registry.register("web_search", r"\[WEB_SEARCH:([^\]\n]+)\]", handle_web_search)
    # registry.register("camera_check", r"\[CAM_CHECK\]", handle_camera_check)
    # registry.register("toy", r"\[TOY:([^\]\n]+)\]", handle_toy)
    return registry
