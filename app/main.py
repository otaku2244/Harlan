"""FastAPI 应用 —— P0 骨架。

路由：
    GET  /healthz                     健康检查
    GET  /api/bootstrap               角色 / 能力 / 会话快照（前端启动时拉一次）
    GET  /api/conversations           会话列表
    GET  /api/conversations/{id}/messages   消息分页
    POST /api/chat                    发消息，返回 SSE 流
    GET  /ws                          WebSocket 多端同步

设计：`/api/chat` 用 SSE 返回，**同一条事件流也广播到 WebSocket**。
这样"单端重放"和"多端同步"是同一份数据，不会出现两条路径行为不一致。
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.adapters.model import ModelClient
from app.adapters.serein import SereinMemory
from app.config import BASE_DIR, load_env_file, settings
from app.core.directives import build_default_registry
from app.core.ids import new_id, now_ts
from app.core.pipeline import ChatPipeline, turn_index
from app.core.scheduler import WakeScheduler
from app.db import Database
from app.ws import manager

# 首次启动时注册的能力清单（enabled 决定它对模型是否可见 / 是否出现在提示词里）
DEFAULT_CAPABILITIES = [
    ("next_chat", "决定下次主动找我", "[NEXT_CHAT:x]  x=1~60 分钟；不想再找我则 [NEXT_CHAT:NONE]", True, 10),
    ("alarm", "给自己设一个闹铃，到点会主动开口", "[ALARM:YYYY-MM-DD HH:MM|内容]", True, 20),
    # 以下是"骨架已备、实现待搬"，先注册为关闭状态（v0.3 §8.3 搬运顺序）
    ("web_search", "联网搜索最新信息", "[WEB_SEARCH:查询内容]", False, 30),
    ("camera_check", "主动看一眼端侧摄像头", "[CAM_CHECK]", False, 40),
    ("toy", "控制端侧玩具档位", "[TOY:1~9] / [TOY:STOP]", False, 50),
]


class AppState:
    def __init__(self) -> None:
        self.db: Database | None = None
        self.memory: SereinMemory | None = None
        self.model: ModelClient | None = None
        self.pipeline: ChatPipeline | None = None
        self.scheduler: WakeScheduler | None = None

    @property
    def ready(self) -> bool:
        return self.pipeline is not None


state = AppState()


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_env_file()

    db = Database(settings.db_path)
    await db.connect()
    await db.seed_actors(settings.default_ai_name, settings.default_user_name,
                         settings.default_persona)
    for key, label, directive, enabled, order in DEFAULT_CAPABILITIES:
        await db.upsert_capability(key, label, directive, enabled, order)

    memory = SereinMemory(db, settings)
    await memory.start()
    model = ModelClient(settings)
    registry = build_default_registry()
    pipeline = ChatPipeline(db, memory, model, registry, settings)
    scheduler = WakeScheduler(db, memory, model, registry, manager, settings)
    # 两者共享同一个 model 实例；但运行中替换模型时必须两边一起改，
    # 所以这里提供一个单一入口，避免以后漏改一处。
    state.model = model
    pipeline.model = scheduler.model = model

    state.db, state.memory, state.model = db, memory, model
    state.pipeline, state.scheduler = pipeline, scheduler

    if settings.scheduler_enabled:
        scheduler.start()
    try:
        yield
    finally:
        await scheduler.stop()
        await memory.close()
        await db.close()
        state.db = state.memory = state.model = None
        state.pipeline = state.scheduler = None


app = FastAPI(title="Aion / Harlan", version="0.1.0", lifespan=lifespan)


# ─────────────────────────────────────────────────────────────
# 基础
# ─────────────────────────────────────────────────────────────

@app.get("/healthz")
async def healthz() -> dict:
    return {
        "status": "ok",
        "service": "aion-server",
        "time": now_ts(),
        "db": state.db.path.name if state.db else None,
        "serein_configured": settings.serein_enabled,
        "model_configured": settings.model_enabled,
        "ws_clients": manager.client_count,
    }


@app.get("/api/bootstrap")
async def bootstrap() -> dict:
    """前端启动时拉一次的聚合快照。"""
    if not state.db:
        raise HTTPException(503, "服务未就绪")
    actors = await state.db.actors()
    capabilities = await state.db.capabilities()
    conversations = await state.db.query(
        "SELECT id, title, updated_at FROM conversations ORDER BY updated_at DESC LIMIT 50"
    )
    return {
        "actors": [
            {k: row[k] for k in ("slug", "display_name", "kind", "enabled")} for row in actors
        ],
        "capabilities": [
            {"key": row["key"], "label": row["label"], "directive": row["directive"],
             "enabled": bool(row["enabled"])}
            for row in capabilities
        ],
        "conversations": conversations,
        "settings": {
            "serein_configured": settings.serein_enabled,
            "model_configured": settings.model_enabled,
        },
    }


@app.get("/api/conversations")
async def conversations() -> dict:
    if not state.db:
        raise HTTPException(503, "服务未就绪")
    rows = await state.db.query(
        "SELECT id, title, created_at, updated_at FROM conversations ORDER BY updated_at DESC"
    )
    return {"conversations": rows}


@app.get("/api/conversations/{conv_id}/messages")
async def messages(conv_id: str, limit: int = Query(50, ge=1, le=200),
                   before: float | None = None) -> dict:
    if not state.db:
        raise HTTPException(503, "服务未就绪")
    rows = await state.db.messages(conv_id, limit=limit, before=before)
    decoded = [Database.decode_message(row) for row in rows]
    return {"messages": decoded, "has_more": len(rows) == limit}


# ─────────────────────────────────────────────────────────────
# 能力开关（数据驱动，改这里就等于改提示词里列出什么）
# ─────────────────────────────────────────────────────────────

class CapabilityPatch(BaseModel):
    enabled: bool


@app.patch("/api/capabilities/{key}")
async def patch_capability(key: str, body: CapabilityPatch) -> dict:
    if not state.db:
        raise HTTPException(503, "服务未就绪")
    existing = await state.db.query_one("SELECT key FROM capabilities WHERE key = ?", (key,))
    if not existing:
        raise HTTPException(404, f"能力 {key} 不存在")
    await state.db.set_capability(key, body.enabled)
    await manager.broadcast_event("capability_changed", {"key": key, "enabled": body.enabled})
    return {"key": key, "enabled": body.enabled}


# ─────────────────────────────────────────────────────────────
# 对话（SSE）
# ─────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    conv_id: str = "main"
    actor: str = ""            # 留空则用第一个启用的 AI 角色
    perception: str = ""


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@app.post("/api/chat")
async def chat(body: ChatRequest, request: Request) -> StreamingResponse:
    if not state.pipeline or not state.db:
        raise HTTPException(503, "服务未就绪")

    db = state.db
    pipeline = state.pipeline

    actor_slug = body.actor
    if not actor_slug:
        row = await db.query_one(
            "SELECT slug FROM actors WHERE kind='ai' AND enabled=1 ORDER BY id LIMIT 1"
        )
        if row is None:
            raise HTTPException(400, "没有启用的 AI 角色")
        actor_slug = row["slug"]

    await db.ensure_conversation(body.conv_id, title=body.message[:30])
    await db.add_message(body.conv_id, "user", "user", body.message)

    history_rows = await db.messages(body.conv_id, limit=settings.history_limit)
    turn = turn_index(history_rows)
    window_id = f"{body.conv_id}:{actor_slug}"

    async def event_stream():
        collected: list[str] = []
        try:
            async for event in pipeline.run_turn(
                conv_id=body.conv_id,
                ai_slug=actor_slug,
                window_id=window_id,
                user_text=body.message,
                perception=body.perception,
                turn=turn,
            ):
                if await request.is_disconnected():
                    break
                if event.type == "stream_delta":
                    collected.append(event.data.get("text", ""))
                # 逐条转 SSE
                yield _sse({"type": event.type, "data": event.data})
                # 同时广播到其它端（多端同步）
                if event.type in {"stream_start", "stream_end", "message", "capability"}:
                    await manager.broadcast_event(event.type, event.data)
                elif event.type == "done":
                    visible = event.data.get("visible_text") or ""
                    if visible:
                        saved = await db.add_message(
                            body.conv_id, actor_slug, "assistant", visible,
                            meta={"recalled_ids": event.data.get("recalled_ids") or [],
                                  "directive_rounds": event.data.get("directive_rounds", 0)},
                        )
                        await manager.broadcast_event(
                            "message", {**Database.decode_message(saved),
                                        "display_name": await db.display_name(actor_slug)}
                        )
                    # 用户一发消息 → 清掉这个角色的主动陪伴计时器（v0.2 §3.2 冷却规则）
                    await db.cancel_schedules("proactive", origin=actor_slug)
                    await manager.broadcast_event("done", event.data)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — 流里出错要告诉客户端，不能静默断
            yield _sse({"type": "error", "data": {"message": f"{type(exc).__name__}: {exc}"}})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ─────────────────────────────────────────────────────────────
# 唤醒（P3）
# ─────────────────────────────────────────────────────────────

class WakeRequest(BaseModel):
    actor: str = ""                    # 留空则用第一个启用的 AI 角色
    kind: str = "proactive"            # proactive | idle | alarm | reminder
    content: str = ""                  # alarm / reminder 的内容


@app.get("/api/wakes")
async def list_wakes(limit: int = Query(30, ge=1, le=200)) -> dict:
    """查看唤醒总线现状：待触发 + 最近已触发。"""
    if not state.db:
        raise HTTPException(503, "服务未就绪")
    pending = await state.db.query(
        "SELECT id, type, origin, trigger_at, payload_json FROM schedules "
        "WHERE status = 'active' ORDER BY trigger_at LIMIT ?",
        (limit,),
    )
    recent = await state.db.query(
        "SELECT id, type, origin, trigger_at, status FROM schedules "
        "WHERE status != 'active' ORDER BY trigger_at DESC LIMIT ?",
        (limit,),
    )
    now = now_ts()
    for row in pending:
        row["in_seconds"] = round(row["trigger_at"] - now, 1)
    return {"pending": pending, "recent": recent, "scheduler_running":
            bool(state.scheduler and state.scheduler.running)}


@app.post("/api/wake")
async def trigger_wake(body: WakeRequest) -> dict:
    """手动让角色醒一次。

    不用等真实的 2 小时，也不用改数据库 —— 可以立刻看到它主动开口。
    走的是和后台循环完全相同的路径，所以结果有代表性。
    """
    if not state.scheduler or not state.db:
        raise HTTPException(503, "服务未就绪")

    slug = body.actor
    if not slug:
        row = await state.db.query_one(
            "SELECT slug FROM actors WHERE kind='ai' AND enabled=1 ORDER BY id LIMIT 1"
        )
        if row is None:
            raise HTTPException(400, "没有启用的 AI 角色")
        slug = row["slug"]

    if body.kind not in {"proactive", "idle", "alarm", "reminder"}:
        raise HTTPException(400, f"未知的唤醒类型 {body.kind}")

    payload = {"content": body.content} if body.content else {}
    result = await state.scheduler.run_actor_now(slug, body.kind, payload)
    return {"actor": slug, "kind": body.kind, **result}


# ─────────────────────────────────────────────────────────────
# WebSocket 多端同步
# ─────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, client_id: str | None = None) -> None:
    cid = await manager.connect(websocket, client_id)
    hello_sent = False
    try:
        # hello 直接 await 发，保证"连接后第一条一定是 hello"
        await websocket.send_text(json.dumps({
            "type": "hello",
            "data": {"client_id": cid, "clients": manager.client_count, "time": now_ts()},
        }, ensure_ascii=False))
        hello_sent = True
        await manager.broadcast_event("clients_changed", {"clients": manager.client_count},
                                      exclude=cid)
        while True:
            # 带超时等待：广播由其它协程入队、由本连接的发送任务写出，
            # 这里只负责处理上行。超时的含义是"暂时没有上行数据"，不是断开。
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if payload.get("type") == "ping":
                await websocket.send_text(json.dumps({"type": "pong", "data": {"time": now_ts()}}))
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        pass
    finally:
        await manager.disconnect(cid)
        if hello_sent:
            await manager.broadcast_event("clients_changed", {"clients": manager.client_count})


# ─────────────────────────────────────────────────────────────
# 静态前端（移植后放在 app/static/）
# ─────────────────────────────────────────────────────────────

STATIC_DIR = BASE_DIR / "app" / "static"
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(str(STATIC_DIR / "home.html"))
