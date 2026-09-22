"""AionsHome 兼容适配层。

移植过来的前端调用的是 AionsHome 的 API 形状（31 个端点），
而我们后端只有 1 个能对上。**改前端不如改后端** —— 前端 243KB 且经实战验证，
重写它的调用面会引入一堆新 bug。

所以这个模块把前端的调用翻译成我们的内部实现。

核心契约（实测自 chat.js，不是猜的）：

    GET    /api/conversations                    → {conversations:[...]}
    POST   /api/conversations                    → 新建会话
    GET    /api/conversations/{id}/messages      → 历史分页（支持 before）
    POST   /api/conversations/{id}/send          → ★ 发消息，返回 SSE
    GET    /api/worldbook                        → 人设
    GET    /api/models                           → {models:[...]}

SSE 事件形状（前端 _processSSEStream 解析）：
    {"type":"start","id":"msg_xxx"}
    {"type":"chunk","content":"增量文字"}
    {"type":"replace","content":"整段替换"}
    {"type":"stream_error","message":"..."}
    {"type":"done", ...}                 ← 前端没处理，但我们发一个无害

前端用 client_id 做多端定向，用 created_at 做排序（秒，浮点）。
"""

from __future__ import annotations

import asyncio
import json
import re

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.core.ids import new_id, now_ts
from app.db import Database

router = APIRouter(prefix="/api", tags=["aionshome-compat"])

# 由 main.py 注入，避免循环导入
_state = None

# 指令剥离：**流式阶段就要剥**，不能等 done。
#
# 为什么：前端每收到一个 chunk 就直接 append 到气泡上渲染。如果把带指令的
# 原始文本一路发过去，用户会看到 "[NEXT_CHAT:NONE]" 这种字样（实测出现过）。
# 管道里的 strip() 只作用于落库的最终文本，救不了已经渲染出去的增量。
#
# 用一个可能不完整的尾部缓冲区：以 "[" 开头的尾巴先不发，
# 等下一个 chunk 到了再判断它是真指令还是普通方括号文字。
_DIRECTIVE = re.compile(r"\[(?:NEXT_CHAT|ALARM|REMINDER|WEB_SEARCH|WEB_EXTRACT|"
                        r"CAM_CHECK|TOY|MUSIC|SONG|SELFIE|DRAW|POI_SEARCH|"
                        r"Monitor|SCHEDULE_DEL|SCHEDULE_LIST|HEART|SVAKOM|"
                        r"查看动态|转账)[^\]]*\]", re.IGNORECASE)
_OPEN_BRACKET_TAIL = re.compile(r"\[[^\[\]]*$")


class _StreamFilter:
    """流式指令剥离器。hold back 可能是半个指令的尾部。"""

    def __init__(self) -> None:
        self._buffer = ""

    def feed(self, piece: str) -> str:
        self._buffer += piece
        # 尾部可能是半条指令（以 [ 开头却没等到 ]），先留着
        hold = ""
        tail = _OPEN_BRACKET_TAIL.search(self._buffer)
        if tail:
            hold = self._buffer[tail.start():]
            self._buffer = self._buffer[:tail.start()]
        emit = _DIRECTIVE.sub("", self._buffer)
        self._buffer = hold
        return emit

    def flush(self) -> str:
        """流结束时把剩余缓冲吐出来（若是残缺指令则丢掉）。"""
        rest = _DIRECTIVE.sub("", self._buffer)
        self._buffer = ""
        if _OPEN_BRACKET_TAIL.search(rest) and "[" in rest:
            # 只有半个方括号，不像正常正文，丢弃
            return rest.split("[", 1)[0]
        return rest


def bind(state) -> None:
    global _state
    _state = state


def _db() -> Database:
    if _state is None or _state.db is None:
        raise HTTPException(503, "服务未就绪")
    return _state.db


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


# ─────────────────────────────────────────────────────────────
# 会话
# ─────────────────────────────────────────────────────────────

@router.get("/conversations")
async def list_conversations() -> dict:
    rows = await _db().query(
        "SELECT id, title, created_at, updated_at FROM conversations ORDER BY updated_at DESC"
    )
    # 前端用 last_message 做副标题，没有就留空
    for row in rows:
        last = await _db().query_one(
            "SELECT content FROM messages WHERE conv_id = ? ORDER BY created_at DESC LIMIT 1",
            (row["id"],),
        )
        row["last_message"] = (last or {}).get("content", "")[:60]
    return {"conversations": rows}


class NewConversation(BaseModel):
    title: str = "新对话"


@router.post("/conversations")
async def create_conversation(body: NewConversation | None = None) -> dict:
    conv_id = new_id("conv")
    title = (body.title if body else "") or "新对话"
    row = await _db().ensure_conversation(conv_id, title)
    return {"conversation": row, **row}


@router.put("/conversations/{conv_id}")
async def rename_conversation(conv_id: str, body: dict) -> dict:
    title = str(body.get("title") or "").strip()
    if title:
        await _db().execute("UPDATE conversations SET title = ? WHERE id = ?", (title, conv_id))
    row = await _db().query_one("SELECT * FROM conversations WHERE id = ?", (conv_id,))
    if row is None:
        raise HTTPException(404, "会话不存在")
    return row


@router.delete("/conversations/{conv_id}")
async def delete_conversation(conv_id: str) -> dict:
    await _db().execute("DELETE FROM messages WHERE conv_id = ?", (conv_id,))
    await _db().execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
    return {"ok": True}


# ─────────────────────────────────────────────────────────────
# 消息
# ─────────────────────────────────────────────────────────────

def _wire_message(row: dict, display_name: str = "") -> dict:
    """转成前端期望的形状。

    created_at 必须是**秒级浮点** —— 前端拿它做排序与分页游标
    （new Date(m.created_at * 1000)）。
    """
    item = Database.decode_message(row)
    item.setdefault("attachments", [])
    item.setdefault("meta", {})
    if display_name:
        item["display_name"] = display_name
    return item


@router.get("/conversations/{conv_id}/messages")
async def get_messages(conv_id: str, limit: int = Query(50, ge=1, le=200),
                       before: float | None = None) -> dict:
    db = _db()
    rows = await db.messages(conv_id, limit=limit, before=before)
    out = []
    for row in rows:
        out.append(_wire_message(row, await db.display_name(row["sender"])))
    return {"messages": out, "has_more": len(rows) == limit}


@router.patch("/messages/{msg_id}/star")
async def star_message(msg_id: str) -> dict:
    # 星标功能后端未实现，但不该让前端报错
    return {"ok": True, "id": msg_id, "starred": False, "note": "星标功能未实现"}


@router.patch("/messages/{msg_id}/feedback")
async def message_feedback(msg_id: str, body: dict) -> dict:
    return {"ok": True, "id": msg_id, "note": "反馈功能未实现"}


@router.delete("/messages/{msg_id}")
async def delete_message(msg_id: str) -> dict:
    await _db().execute("DELETE FROM messages WHERE id = ?", (msg_id,))
    return {"ok": True}


# ─────────────────────────────────────────────────────────────
# 发消息（SSE）—— 核心
# ─────────────────────────────────────────────────────────────

class SendRequest(BaseModel):
    content: str = ""
    attachments: list[str] = []
    context_limit: int = 30
    temperature: float | None = None
    max_tokens: int | None = None
    whisper_mode: bool = False
    tts_enabled: bool = False
    tts_voice: str = ""
    client_id: str = ""


@router.post("/conversations/{conv_id}/send")
async def send_message(conv_id: str, body: SendRequest, request: Request) -> StreamingResponse:
    if _state is None or _state.pipeline is None:
        raise HTTPException(503, "服务未就绪")
    db = _db()
    pipeline = _state.pipeline

    text = (body.content or "").strip()
    attachments = [{"type": "image", "url": u} for u in (body.attachments or [])]
    if not text and not attachments:
        raise HTTPException(400, "消息内容为空")

    await db.ensure_conversation(conv_id, title=text[:30] or "新对话")
    actor = await db.query_one(
        "SELECT slug FROM actors WHERE kind='ai' AND enabled=1 ORDER BY id LIMIT 1"
    )
    if actor is None:
        raise HTTPException(400, "没有启用的 AI 角色")
    ai_slug = actor["slug"]
    display_name = await db.display_name(ai_slug)

    # 落库用户消息；ID 由前端乐观更新过（temp_user），这里生成正式的
    user_msg = await db.add_message(conv_id, "user", "user", text, attachments=attachments)

    window_id = f"{conv_id}:{ai_slug}"
    history_rows = await db.messages(conv_id, limit=body.context_limit or 30)
    turn = len(history_rows)

    async def stream():
        # 让前端先把乐观更新的用户消息替换成正式 ID
        yield _sse({"type": "user_saved", "id": user_msg["id"],
                    "content": text, "created_at": user_msg["created_at"]})

        ai_id = new_id("msg")
        yield _sse({"type": "start", "id": ai_id})

        collected: list[str] = []
        filt = _StreamFilter()
        try:
            async for event in pipeline.run_turn(
                conv_id=conv_id,
                ai_slug=ai_slug,
                window_id=window_id,
                user_text=text,
                perception="",
                turn=turn,
            ):
                if await request.is_disconnected():
                    break
                kind = event.type
                if kind == "stream_delta":
                    piece = filt.feed(event.data.get("text", ""))
                    if piece:
                        collected.append(piece)
                        yield _sse({"type": "chunk", "content": piece})
                elif kind == "capability":
                    yield _sse({"type": "capability", "data": event.data})
                elif kind == "capability_note":
                    yield _sse({"type": "debug", "text": event.data.get("note", "")})
                elif kind == "error":
                    yield _sse({"type": "stream_error", "message": event.data.get("message", "")})
                elif kind == "recall":
                    yield _sse({"type": "debug", "text": json.dumps(event.data, ensure_ascii=False)})
                elif kind == "done":
                    tail = filt.flush()
                    if tail:
                        collected.append(tail)
                        yield _sse({"type": "chunk", "content": tail})
                    # 以管道给出的可见文本为准（它做了完整剥离），
                    # 前端拼接结果与落库内容才不会不一致
                    visible = event.data.get("visible_text") or "".join(collected)
                    saved = await db.add_message(
                        conv_id, ai_slug, "assistant", visible,
                        meta={"recalled_ids": event.data.get("recalled_ids") or [],
                              "directive_rounds": event.data.get("directive_rounds", 0)},
                    )
                    # 广播给其它端（同一浏览器多标签 / 手机）
                    await _state.multi.broadcast_event("msg_created", {
                        **_wire_message(saved, display_name), "proactive": False,
                    })
                    yield _sse({"type": "done", "id": saved["id"],
                                "content": visible, "created_at": saved["created_at"]})
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            yield _sse({"type": "stream_error",
                        "message": f"{type(exc).__name__}: {exc}"})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"},
    )


# ─────────────────────────────────────────────────────────────
# 启动时必调（缺了页面会报错）
# ─────────────────────────────────────────────────────────────

@router.get("/worldbook")
async def get_worldbook() -> dict:
    """前端启动时拉人设。字段名必须与 AionsHome 一致，否则页面读不到名字。"""
    db = _db()
    actors = await db.actors()
    ai = next((a for a in actors if a["slug"] == "harlan"), None) or \
         next((a for a in actors if a["kind"] == "ai"), None)
    user = next((a for a in actors if a["kind"] == "user"), None)
    return {
        "ai_name": (ai or {}).get("display_name", "Harlan"),
        "user_name": (user or {}).get("display_name", "你"),
        "ai_persona": (ai or {}).get("persona", ""),
        "user_persona": f"用户是「{(user or {}).get('display_name', '你')}」。",
        "system_prompt": "",
        "system_prompt_enabled": True,
        "persona_schema_version": 1,
        "ai_persona_sections": {},
        "user_persona_sections": {},
        "creative_rules": "",
        "persona_section_locks": {},
        "persona_evolution_enabled": False,
    }


@router.put("/worldbook")
async def put_worldbook(body: dict) -> dict:
    """保存人设。这是"名字全开放"的接线点：改这里就是改显示名，代码不动。"""
    db = _db()
    ai_name = str(body.get("ai_name") or "").strip()
    user_name = str(body.get("user_name") or "").strip()
    persona = str(body.get("ai_persona") or "")
    if ai_name:
        await db.execute("UPDATE actors SET display_name = ? WHERE slug = 'harlan'", (ai_name,))
    if user_name:
        await db.execute("UPDATE actors SET display_name = ? WHERE slug = 'user'", (user_name,))
    if persona:
        await db.execute("UPDATE actors SET persona = ? WHERE slug = 'harlan'", (persona,))
    return await get_worldbook()


@router.get("/models")
async def get_models() -> list:
    """前端拿它填模型下拉框。

    ⚠️ 必须返回**裸数组**，不能包成 {"models":[...]}。
    实测教训：前端是
        [models, worldBook, conversations] = await Promise.all([...])
        renderModelSelect() → models.filter(...)
    包一层对象会让 models.filter 报 "is not a function"，
    整个 init() 中断 —— 表现是"页面能开但点发送没反应"，
    而且错误被 .catch() 吞进 console.warn，界面上完全看不出来。
    """
    from app.config import settings
    name = settings.model_name or "（未配置）"
    return [{"id": name, "name": name, "vision": False}]


@router.get("/chatroom/config")
async def chatroom_config() -> dict:
    """聊天室页用的配置。我们没移植聊天室，返回最小形状即可。"""
    db = _db()
    connor = await db.actor_by_slug("connor")
    return {
        "connor_name": (connor or {}).get("display_name", "Connor"),
        "connor_url": "",
        "connor_persona": (connor or {}).get("persona", ""),
        "tts_aion_voice": "",
        "tts_connor_voice": "",
    }


@router.get("/settings/temperature")
async def get_temperature() -> dict:
    return {"temperature": 0.8}


@router.put("/settings/temperature")
async def put_temperature(body: dict) -> dict:
    return {"temperature": body.get("temperature", 0.8)}


@router.get("/settings/gemini-cli-tools")
async def get_cli_tools() -> dict:
    # 本机 CLI 线路在 VPS 上不可用（见 v0.3 §5）
    return {"tools": [], "enabled": False}


@router.get("/files")
async def list_files() -> dict:
    return {"files": [], "note": "文件管理未移植"}


@router.get("/sync/changes")
async def sync_changes(after: float = 0, limit: int = 200) -> dict:
    """前端用它做增量同步。我们暂时返回空，靠 WebSocket 推送。

    返回空是安全的：前端不会因此丢消息，只是不做补拉。
    """
    return {"changes": [], "after": now_ts(), "has_more": False}


@router.get("/proactive-companionship")
async def proactive_status() -> dict:
    """前端启动时读主动陪伴开关状态。"""
    db = _db()
    actors = await db.actors(kind="ai", enabled_only=True)
    return {"aion": {"enabled": True, "next_at": ""},
            "connor": {"enabled": False, "next_at": ""},
            "actors": [a["slug"] for a in actors]}


@router.get("/memories/by-conv/{conv_id}")
async def memories_by_conv(conv_id: str) -> dict:
    # 记忆在 Serein 里，不在本地表；记忆页自己会去调别的接口
    return {"memories": [], "note": "记忆由 Serein 管理"}
