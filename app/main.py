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

from fastapi import FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.adapters.model import ModelClient
from app.adapters.serein import SereinMemory
from app.config import BASE_DIR, settings
from app.core.directives import build_default_registry
from app.core.ids import new_id, now_ts
from app.core.pipeline import ChatPipeline, turn_index
from app.core.scheduler import WakeScheduler
from app.db import Database
from app.env import load_env_file
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
        # 兼容层用它广播（前端按 AionsHome 的 msg_created 事件名收消息）
        self.multi = manager

    @property
    def ready(self) -> bool:
        return self.pipeline is not None


state = AppState()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 注意：.env 已在 app/__init__.py 里加载（必须早于 app.config 实例化 settings）。
    # 这里再调一次是无害的幂等兜底，别把它当成唯一入口。
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

# AionsHome 兼容适配层：移植来的前端调的是它的 API 形状（31 个端点）。
# 放在 /api 下，与下面的原生路由不冲突（路径不重叠）。
from app import routes_compat  # noqa: E402

routes_compat.bind(state)
app.include_router(routes_compat.router)


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
        # 诊断用：直接暴露读到的值（不含密钥），排查配置问题时很有用
        "config_seen": {
            "serein_base_url": settings.serein_base_url or "(空)",
            "serein_key_len": len(settings.serein_gateway_key),
            "model_base_url": settings.model_base_url or "(空)",
            "model_name": settings.model_name or "(空)",
            "ai_display_name": settings.default_ai_name,
        },
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
# 前端诊断：把浏览器里的报错收回来
# ─────────────────────────────────────────────────────────────
#
# 为什么需要这个：前端初始化外面包了 .catch(e => console.warn(...))，
# 出错只写进 Console 的警告里，界面上完全看不出来（表现为"点了没反应"）。
# 而服务端**验证不了 JS 运行时** —— 我只能测资源是否 200。
#
# 所以让页面把 window.onerror / unhandledrejection 回传到这里，
# 我用 GET /api/debug/frontend-errors 就能读到，不必再让用户截图。

_frontend_errors: list[dict] = []
_FRONTEND_ERROR_LIMIT = 50


class FrontendError(BaseModel):
    message: str = ""
    source: str = ""
    lineno: int = 0
    colno: int = 0
    stack: str = ""
    kind: str = "error"          # error | unhandledrejection | log
    url: str = ""


@app.post("/api/debug/frontend-error")
async def report_frontend_error(body: FrontendError) -> dict:
    _frontend_errors.append({
        "at": now_ts(), **body.model_dump(),
    })
    del _frontend_errors[:-_FRONTEND_ERROR_LIMIT]
    print(f"[frontend:{body.kind}] {body.message} @ {body.source}:{body.lineno}", flush=True)
    return {"ok": True}


@app.get("/api/debug/frontend-errors")
async def get_frontend_errors(clear: bool = False) -> dict:
    items = list(_frontend_errors)
    if clear:
        _frontend_errors.clear()
    return {"count": len(items), "errors": items}


@app.delete("/api/debug/frontend-errors")
async def clear_frontend_errors() -> dict:
    count = len(_frontend_errors)
    _frontend_errors.clear()
    return {"cleared": count}


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
# 静态前端
# ─────────────────────────────────────────────────────────────
#
# 移植过来的前端用**绝对路径**引用资源，所以下面这几个挂载点是硬需求，
# 不是可选优化：
#     /static/xxx        ← <script src="/static/chat.js">
#     /public/xxx        ← <img src="/public/AIIcon.png">
#     /manifest.json     ← PWA 清单（从根路径提供，作用域才覆盖全站）
#
# app/static/ 这一层目录是"网页根"，但 URL 前缀不是 —— 别把它们搞成同一个挂载点。

STATIC_DIR = BASE_DIR / "app" / "static"

if STATIC_DIR.is_dir():
    # /static/* → app/static/*
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # /public/* → app/static/public/*
    _public = STATIC_DIR / "public"
    if _public.is_dir():
        app.mount("/public", StaticFiles(directory=str(_public)), name="public")

    def _page(name: str) -> FileResponse:
        return FileResponse(str(STATIC_DIR / name))

    @app.get("/")
    async def index() -> FileResponse:
        """主页：手机风格的应用网格。"""
        home = STATIC_DIR / "home.html"
        return FileResponse(str(home if home.exists() else STATIC_DIR / "chat.html"))

    @app.get("/manifest.json")
    async def pwa_manifest() -> FileResponse:
        # 必须从根路径提供，Service Worker / PWA 作用域才覆盖全站
        return _page("manifest.json")

    @app.get("/sw.js")
    async def service_worker() -> Response:
        """Service Worker。

        返回 204 让浏览器**跳过注册**，而不是报错。理由：
          * 开发阶段 SW 会缓存旧页面，改完代码刷新看到的是旧的，非常迷惑
          * 我们还没有离线需求，SW 现在只有副作用
        以后要做 PWA 离线时，把文件放进来就会自动开始提供。
        """
        target = STATIC_DIR / "sw.js"
        if target.exists() and target.stat().st_size > 0:
            return FileResponse(str(target), media_type="application/javascript")
        return Response(status_code=204)

    # 各功能页：/chat → chat.html，/settings → settings.html …
    PAGE_ROUTES = [
        "chat", "home", "settings", "worldbook", "memory", "diary", "moments",
        "schedule", "location", "monitor-logs", "camera", "activity-logs",
    ]
    for _name in PAGE_ROUTES:
        _file = STATIC_DIR / f"{_name}.html"
        if not _file.exists():
            continue

        def _make(target: Path):
            async def _route() -> FileResponse:
                return FileResponse(str(target))
            return _route

        app.get(f"/{_name}", name=f"page_{_name.replace('-', '_')}")(_make(_file))

    # 未移植的功能页：显式给出说明页，而不是让它 404 或白屏
    #
    # 主页网格是 AionsHome 原版的，注册了 33 个入口，我们只移植了 12 个。
    # 不做处理的话，用户点其余 21 个会看到 "Not Found"，像坏了一样。
    from fastapi.responses import HTMLResponse

    NOT_MIGRATED = {
        "chatroom": "聊天室（多角色群聊）",
        "theater": "小剧场（角色扮演）",
        "date-theater": "去约会",
        "ghost-forest": "奥罗斯幽林（TRPG）",
        "heart-whispers": "心语",
        "wishes": "许愿池",
        "wallet": "钱包",
        "gift": "爱的印记",
        "fund": "奥罗斯财团（基金）",
        "reading": "陪伴阅读",
        "english-corner": "学习角",
        "music-station": "点歌台",
        "album": "相册",
        "taobao": "逛淘宝",
        "xhs-lite": "小红书",
        "lounge-friends": "好友串门",
        "doudizhu": "斗地主",
        "toys": "密语时刻",
        "capabilities": "工具与能力",
        "playground": "娱乐室",
        "seeky": "Seeky",
        "wallpaper": "动态壁纸",
        "pet": "宠物",
        "hug": "爱的抱抱",
    }

    @app.get("/{page_name}", include_in_schema=False)
    async def not_migrated_page(page_name: str) -> HTMLResponse:
        label = NOT_MIGRATED.get(page_name)
        if label is None:
            raise HTTPException(404, "Not Found")
        return HTMLResponse(f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{label} — 未启用</title>
<style>
  body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
       background:#241f1c;color:#f2e9e4;font-family:system-ui,-apple-system,"PingFang SC",sans-serif}}
  .card{{max-width:420px;padding:32px 28px;text-align:center}}
  h1{{font-size:20px;margin:0 0 12px}}
  p{{color:#b9aaa0;line-height:1.7;margin:0 0 8px;font-size:14px}}
  a{{display:inline-block;margin-top:20px;padding:10px 22px;border-radius:20px;
     background:#ff8359;color:#241f1c;text-decoration:none;font-weight:600;font-size:14px}}
</style></head><body><div class="card">
  <h1>{label}</h1>
  <p>这个功能在当前部署里<strong>没有启用</strong>。</p>
  <p>它依赖 Windows 本机能力、或属于暂未移植的娱乐模块。</p>
  <a href="/">← 回到主页</a>
</div></body></html>""")
