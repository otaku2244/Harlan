"""功能接口 —— 新前端需要的、原兼容层没覆盖的部分。

命名约定：贴在 /api 下，用**直白的名词**（days/日程、moments/朋友圈），
不再模仿 AionsHome 的历史命名，因为这是给新前端用的、我们自己定的契约。

接口一览：
    日程    GET/POST /api/schedules        DELETE /api/schedules/{id}
    日记    GET /api/diaries              GET /api/diaries/{id}   （代理到 Serein）
    朋友圈  GET/POST /api/moments         DELETE /api/moments/{id}
            POST /api/moments/{id}/replies  POST /api/moments/{id}/like
    记忆    GET /api/recalls              GET /api/recalls/stats
    设置    GET/PUT /api/settings
    诊断    GET /api/diagnostics
"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.adapters.mcp import McpError
from app.core.ids import now_ts
from app.db import Database

router = APIRouter(prefix="/api", tags=["features"])

_state = None


def bind(state) -> None:
    global _state
    _state = state


def _db() -> Database:
    if _state is None or _state.db is None:
        raise HTTPException(503, "服务未就绪")
    return _state.db


# ─────────────────────────────────────────────────────────────
# 日程 / 闹铃（复用唤醒总线的 schedules 表）
# ─────────────────────────────────────────────────────────────
#
# 关键认识：日程和闹铃**不是新东西** —— 唤醒总线的 schedules 表本来就在跑
# proactive / idle / alarm / reminder。模型用 [ALARM:...] 写入的项，
# 在这里列出来就是"日程"。所以不需要新表，只需要读写接口。

SCHEDULE_TYPES = {"alarm", "reminder", "proactive", "idle"}


@router.get("/schedules")
async def list_schedules(include_done: bool = False,
                         limit: int = Query(100, ge=1, le=500)) -> dict:
    db = _db()
    if include_done:
        rows = await db.query(
            "SELECT * FROM schedules ORDER BY trigger_at DESC LIMIT ?", (limit,))
    else:
        rows = await db.query(
            "SELECT * FROM schedules WHERE status = 'active' ORDER BY trigger_at LIMIT ?",
            (limit,))
    now = now_ts()
    items = []
    for row in rows:
        try:
            payload = json.loads(row.get("payload_json") or "{}")
        except json.JSONDecodeError:
            payload = {}
        items.append({
            "id": row["id"],
            "type": row["type"],
            "origin": row["origin"],
            "status": row["status"],
            "trigger_at": row["trigger_at"],
            "in_seconds": round(row["trigger_at"] - now, 1),
            "content": payload.get("content", ""),
            "minutes": payload.get("minutes"),
            "payload": payload,
            "created_at": row["created_at"],
        })
    return {"schedules": items, "count": len(items), "now": now}


class ScheduleIn(BaseModel):
    type: str = "reminder"
    trigger_at: float | None = None
    delay_minutes: int | None = None
    content: str = ""
    origin: str = ""            # 留空则用第一个启用的 AI 角色


@router.post("/schedules")
async def create_schedule(body: ScheduleIn) -> dict:
    db = _db()
    if body.type not in SCHEDULE_TYPES:
        raise HTTPException(400, f"未知类型 {body.type}，可选：{sorted(SCHEDULE_TYPES)}")

    origin = body.origin
    if not origin:
        row = await db.query_one(
            "SELECT slug FROM actors WHERE kind='ai' AND enabled=1 ORDER BY id LIMIT 1")
        if row is None:
            raise HTTPException(400, "没有启用的 AI 角色")
        origin = row["slug"]

    if body.trigger_at is not None:
        trigger_at = float(body.trigger_at)
    elif body.delay_minutes is not None:
        trigger_at = now_ts() + max(1, int(body.delay_minutes)) * 60
    else:
        raise HTTPException(400, "需要 trigger_at 或 delay_minutes")

    payload = {"content": body.content} if body.content else {}
    schedule_id = await db.schedule(body.type, trigger_at, origin, payload)
    return {"id": schedule_id, "type": body.type, "origin": origin,
            "trigger_at": trigger_at, "in_seconds": round(trigger_at - now_ts(), 1)}


@router.delete("/schedules/{schedule_id}")
async def cancel_schedule(schedule_id: str) -> dict:
    db = _db()
    row = await db.query_one("SELECT * FROM schedules WHERE id = ?", (schedule_id,))
    if row is None:
        raise HTTPException(404, "日程不存在")
    if row["status"] != "active":
        raise HTTPException(409, f"该日程状态是 {row['status']}，无法取消")
    await db.execute("UPDATE schedules SET status = 'cancelled' WHERE id = ?", (schedule_id,))
    return {"id": schedule_id, "status": "cancelled"}


# ─────────────────────────────────────────────────────────────
# 日记（代理到 Serein —— 日记归 Serein 管，我们不建第二套）
# ─────────────────────────────────────────────────────────────
#
# 为什么是代理而不是自己存：
#   Serein 里已经有 20 篇日记（旧库迁移过来的），而且它有"暗房"等功能。
#   我们另建一张表就等于制造第二套日记，和 v0.3「记忆唯一」的原则冲突。
#   注意 Serein 的 /api/* 是网页登录态（Gateway Key 会 401），
#   所以必须走 MCP（/serein/mcp 接受静态 Bearer Key）。


@router.get("/diaries")
async def list_diaries(limit: int = Query(20, ge=1, le=50),
                       offset: int = Query(0, ge=0),
                       query: str = "",
                       date: str = "") -> dict:
    if _state is None or _state.mcp is None:
        raise HTTPException(503, "服务未就绪")
    try:
        data = await _state.mcp.read_diary(
            limit=limit, offset=offset or None,
            query=query or None, date=date or None)
    except McpError as exc:
        # Serein 不可用不该让页面崩，返回空列表 + 原因
        return {"entries": [], "count": 0, "error": str(exc)}
    return {
        "entries": data["entries"],
        "count": data["count"],
        "mode": data["mode"],
        "offset": offset,
        "has_more": data["count"] > offset + len(data["entries"]),
    }


@router.get("/diaries/{diary_id}")
async def get_diary(diary_id: int) -> dict:
    if _state is None or _state.mcp is None:
        raise HTTPException(503, "服务未就绪")
    try:
        data = await _state.mcp.read_diary(diary_id=diary_id)
    except McpError as exc:
        raise HTTPException(502, f"读取日记失败：{exc}") from exc
    if not data["entries"]:
        raise HTTPException(404, f"日记 {diary_id} 不存在")
    return data["entries"][0]


# ─────────────────────────────────────────────────────────────
# 朋友圈
# ─────────────────────────────────────────────────────────────


@router.get("/moments")
async def list_moments(limit: int = Query(30, ge=1, le=100),
                       before: float | None = None) -> dict:
    db = _db()
    rows = await db.moments(limit=limit, before=before)
    for row in rows:
        row["display_name"] = await db.display_name(row["author"])
    return {"moments": rows, "has_more": len(rows) == limit}


class MomentIn(BaseModel):
    content: str = Field(min_length=1)
    author: str = "user"
    attachments: list[dict] = []


@router.post("/moments")
async def create_moment(body: MomentIn) -> dict:
    db = _db()
    row = await db.add_moment(body.author, body.content, body.attachments, source="manual")
    await _state.multi.broadcast_event("moment_created", {
        **row, "display_name": await db.display_name(body.author)})
    return {**row, "display_name": await db.display_name(body.author)}


@router.delete("/moments/{moment_id}")
async def delete_moment(moment_id: str) -> dict:
    db = _db()
    if not await db.delete_moment(moment_id):
        raise HTTPException(404, "动态不存在")
    await _state.multi.broadcast_event("moment_deleted", {"id": moment_id})
    return {"id": moment_id, "deleted": True}


class ReplyIn(BaseModel):
    content: str = Field(min_length=1)
    author: str = "user"


@router.post("/moments/{moment_id}/replies")
async def reply_moment(moment_id: str, body: ReplyIn) -> dict:
    db = _db()
    reply = await db.add_moment_reply(moment_id, body.author, body.content)
    if reply is None:
        raise HTTPException(404, "动态不存在")
    await _state.multi.broadcast_event("moment_replied", {
        "id": moment_id, "reply": reply})
    return {"id": moment_id, "reply": reply}


@router.post("/moments/{moment_id}/like")
async def like_moment(moment_id: str, delta: int = 1) -> dict:
    db = _db()
    likes = await db.like_moment(moment_id, delta)
    if likes is None:
        raise HTTPException(404, "动态不存在")
    await _state.multi.broadcast_event("moment_liked", {"id": moment_id, "likes": likes})
    return {"id": moment_id, "likes": likes}


# ─────────────────────────────────────────────────────────────
# 记忆召回日志（让"它在想什么"可见）
# ─────────────────────────────────────────────────────────────


@router.get("/recalls")
async def list_recalls(limit: int = Query(50, ge=1, le=200),
                       conv_id: str = "",
                       source: str = "") -> dict:
    db = _db()
    rows = await db.recall_log(limit=limit, conv_id=conv_id or None,
                               source=source or None)
    return {"recalls": rows, "count": len(rows)}


@router.get("/recalls/stats")
async def recall_stats() -> dict:
    return await _db().recall_stats()


# ─────────────────────────────────────────────────────────────
# 设置读写
# ─────────────────────────────────────────────────────────────


@router.get("/settings")
async def get_settings() -> dict:
    """聚合设置。**绝不返回密钥本身**，只返回"是否已配置"和掩码。"""
    from app.config import settings

    db = _db()
    actors = await db.actors()
    caps = await db.capabilities()
    return {
        "actors": [
            {"slug": a["slug"], "display_name": a["display_name"], "kind": a["kind"],
             "enabled": bool(a["enabled"]), "has_persona": bool(a["persona"])}
            for a in actors
        ],
        "capabilities": [
            {"key": c["key"], "label": c["label"], "directive": c["directive"],
             "enabled": bool(c["enabled"])}
            for c in caps
        ],
        "model": {
            "base_url": settings.model_base_url,
            "name": settings.model_name,
            "configured": settings.model_enabled,
            "key_masked": settings.mask(settings.model_api_key),
            "retries": settings.model_retries,
        },
        "serein": {
            "base_url": settings.serein_base_url,
            "configured": settings.serein_enabled,
            "key_masked": settings.mask(settings.serein_gateway_key),
            "max_notes": settings.serein_max_notes,
        },
        "scheduler": {
            "enabled": settings.scheduler_enabled,
            "poll_seconds": settings.scheduler_poll_seconds,
            "idle_min_minutes": settings.idle_min_minutes,
            "idle_max_minutes": settings.idle_max_minutes,
        },
        "runtime": {
            "wake_conv_id": settings.wake_conv_id,
            "history_limit": settings.history_limit,
            "max_directive_rounds": settings.max_directive_rounds,
        },
    }


class SettingsPatch(BaseModel):
    """只允许改这些**运行时可调**项；密钥只能通过 .env 改（避免从网页写密钥）。"""

    idle_min_minutes: int | None = None
    idle_max_minutes: int | None = None
    scheduler_enabled: bool | None = None
    history_limit: int | None = None
    max_directive_rounds: int | None = None


@router.put("/settings")
async def update_settings(body: SettingsPatch) -> dict:
    """改运行时设置。

    ⚠️ 有意不提供"改 API Key"的接口：密钥从网页写入会留下难以追踪的副本，
    而且 .env 才是唯一真相。要换 Key 请改 .env 后重启。
    """
    import os

    from app.config import settings

    applied: dict = {}
    for field in ("idle_min_minutes", "idle_max_minutes", "history_limit",
                  "max_directive_rounds"):
        value = getattr(body, field)
        if value is not None:
            setattr(settings, field, max(1, int(value)))
            applied[field] = getattr(settings, field)

    if body.scheduler_enabled is not None:
        settings.scheduler_enabled = bool(body.scheduler_enabled)
        applied["scheduler_enabled"] = settings.scheduler_enabled

    # 持久化到 settings 表，重启后仍生效
    if applied:
        db = _db()
        await db.set_setting("runtime_overrides", json.dumps(applied, ensure_ascii=False))
    return {"applied": applied, "note": "改 .env 才能修改密钥；本接口只调运行时参数"}


# ─────────────────────────────────────────────────────────────
# 诊断
# ─────────────────────────────────────────────────────────────


@router.get("/diagnostics")
async def diagnostics() -> dict:
    """一眼看出各依赖通不通。前端"设置"页用它显示状态灯。"""
    from app.config import settings

    db = _db()
    out: dict = {"time": now_ts()}

    # 数据库
    try:
        await db.query("SELECT 1")
        out["db"] = {"ok": True}
    except Exception as exc:  # noqa: BLE001
        out["db"] = {"ok": False, "error": str(exc)}

    # Serein（召回 + MCP 两条链路分开测，它们故障模式不同）
    if settings.serein_enabled:
        try:
            recall = await _state.memory.recall("诊断 ping", "diagnostics",
                                                log=False)
            out["serein_hook"] = {"ok": recall.ok or bool(recall.skipped is None),
                                  "note": recall.error or recall.skipped or "可达"}
        except Exception as exc:  # noqa: BLE001
            out["serein_hook"] = {"ok": False, "error": str(exc)}
        try:
            tools = await _state.mcp.list_tools()
            out["serein_mcp"] = {"ok": True, "tools": len(tools)}
        except Exception as exc:  # noqa: BLE001
            out["serein_mcp"] = {"ok": False, "error": str(exc)}
    else:
        out["serein_hook"] = {"ok": False, "note": "未配置"}
        out["serein_mcp"] = {"ok": False, "note": "未配置"}

    # 模型：真发一次最小请求
    if settings.model_enabled:
        try:
            # ⚠️ 不要用很小的 max_tokens。实测 Serein 网关在 max_tokens 偏小时
            # 会返回**空内容**（不报错，就是空），min 值大约在 128 附近：
            #     max_tokens=4/8/16/32 → 0 字
            #     max_tokens=128/不限   → 正常
            # 用 8 会让诊断永远报"模型不可用"，是误报。
            reply = await _state.model.complete(
                [{"role": "user", "content": "只回两个字：在的"}], max_tokens=256)
            out["model"] = {"ok": bool(reply), "reply_chars": len(reply),
                            "reply": reply[:40]}
        except Exception as exc:  # noqa: BLE001
            out["model"] = {"ok": False, "error": str(exc)}
    else:
        out["model"] = {"ok": False, "note": "未配置"}

    out["scheduler"] = {"running": bool(_state.scheduler and _state.scheduler.running)}
    out["recall_stats"] = await db.recall_stats()
    return out
