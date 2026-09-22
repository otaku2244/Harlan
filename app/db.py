"""数据库 —— SQLite，七张表。

关键设计（对应 v0.3 §8.1 的三条结构底线）：

  1. `capabilities` 是**数据**。加玩具/摄像头 = 插一行 + 写 handler，不改提示词模板。
  2. `messages.attachments_json` 第一天就有。图片/语音/音乐卡/指令回执全靠它。
  3. `schedules` 是唤醒总线：trigger_at（何时）+ origin（谁）+ claim（一次性领取）。

角色身份用 slug 而非自增 id 做业务键，见 v0.3 §0.2：
slug 存在表里，代码不写死；display_name 纯数据，改名不动代码。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable, Iterable, TypeVar

from app.core.ids import new_id, now_ts

T = TypeVar("T")

SCHEMA_V1 = """
-- 角色：Harlan / Connor / 用户自己，都是这张表的行
-- slug 是结构句柄（代码只认它，但不写死具体值）；display_name 是纯数据
CREATE TABLE IF NOT EXISTS actors (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    slug          TEXT NOT NULL UNIQUE,
    display_name  TEXT NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'ai',      -- 'ai' | 'user'
    persona       TEXT NOT NULL DEFAULT '',
    model_key     TEXT NOT NULL DEFAULT '',        -- 各自模型通道（Connor 的坑落在这）
    tts_voice     TEXT NOT NULL DEFAULT '',
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    REAL NOT NULL
);

-- 会话
CREATE TABLE IF NOT EXISTS conversations (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);

-- 消息。sender 存 slug，显示时查 actors.display_name
-- attachments_json 第一天就存在，哪怕永远是 []
CREATE TABLE IF NOT EXISTS messages (
    id               TEXT PRIMARY KEY,
    conv_id          TEXT NOT NULL,
    sender           TEXT NOT NULL,                -- actors.slug 或 'system'
    role             TEXT NOT NULL,                -- user | assistant | system
    content          TEXT NOT NULL DEFAULT '',
    attachments_json TEXT NOT NULL DEFAULT '[]',
    meta_json        TEXT NOT NULL DEFAULT '{}',
    created_at       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conv_id, created_at);

-- 能力注册表：数据驱动，不是代码里的常量
-- key 例：'next_chat' / 'alarm' / 'web_search' / 'camera_check' / 'toy'
CREATE TABLE IF NOT EXISTS capabilities (
    key         TEXT PRIMARY KEY,
    label       TEXT NOT NULL DEFAULT '',
    directive   TEXT NOT NULL DEFAULT '',          -- 提示词里展示的指令形态
    enabled     INTEGER NOT NULL DEFAULT 0,
    config_json TEXT NOT NULL DEFAULT '{}',
    sort_order  INTEGER NOT NULL DEFAULT 100
);

-- 唤醒总线：trigger_at（何时）+ origin（谁）+ status（claim 用原子更新）
CREATE TABLE IF NOT EXISTS schedules (
    id           TEXT PRIMARY KEY,
    type         TEXT NOT NULL,                    -- proactive | idle | alarm | reminder
    trigger_at   REAL NOT NULL,
    origin       TEXT NOT NULL,                    -- actors.slug
    status       TEXT NOT NULL DEFAULT 'active',   -- active | claimed | done | cancelled
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_schedules_due ON schedules(status, trigger_at);

-- 键值配置
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 记忆交付回执：同一 window + receipt 幂等，避免重复登记
-- 这张表的另一作用是**冷却**：已交付的卡片 ID 不重复注入
CREATE TABLE IF NOT EXISTS delivery_receipts (
    receipt_id    TEXT PRIMARY KEY,
    window_id     TEXT NOT NULL,
    delivered_ids TEXT NOT NULL DEFAULT '[]',
    created_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_receipts_window ON delivery_receipts(window_id, created_at);
"""


class Database:
    """SQLite 封装。

    ⚠️ 线程安全：`asyncio.to_thread` 会把工作派给**多个**线程，而所有线程共用同一个
    SQLite 连接。`check_same_thread=False` 只是关掉了 Python 的检查，并没有让连接变成
    线程安全 —— 并发访问会抛 `InterfaceError: bad parameter or other API misuse`。
    所以所有数据库调用都经过 `_run()`，由一把锁串行化。

    SQLite 单文件写入本来就是串行的，加锁不损失真实吞吐；
    真要并发读写就该换 WAL + 独立连接池，那是另一个量级的工程。

    用法：
        db = Database(path); await db.connect()
        await db.execute("INSERT ...", (...))
        rows = await db.query("SELECT ...", (...))
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    # ── 生命周期 ────────────────────────────────────

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await asyncio.to_thread(self._open)
        await self._migrate()

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")     # 允许读写并发，多端同步必需
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    async def close(self) -> None:
        if self._conn is not None:
            conn, self._conn = self._conn, None
            await asyncio.to_thread(conn.close)

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("数据库未连接，请先 await db.connect()")
        return self._conn

    async def _run(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """在专用线程里执行一次数据库操作，全程持锁。

        锁必须在**读取 self._conn 之前**拿到，否则 close() 可能与访问交错。
        """

        def worker() -> T:
            with self._lock:
                if self._conn is None:
                    raise RuntimeError("数据库未连接，请先 await db.connect()")
                return fn(self._conn)

        return await asyncio.to_thread(worker)

    async def _migrate(self) -> None:
        def run(conn: sqlite3.Connection) -> None:
            conn.executescript(SCHEMA_V1)
            conn.commit()

        await self._run(run)

    # ── 基础操作 ────────────────────────────────────

    async def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        bound = self._bind(params)

        def run(conn: sqlite3.Connection) -> int:
            cur = conn.execute(sql, bound)
            conn.commit()
            return cur.rowcount

        return await self._run(run)

    async def execute_many(self, sql: str, rows: list[tuple]) -> None:
        def run(conn: sqlite3.Connection) -> None:
            conn.executemany(sql, rows)
            conn.commit()

        await self._run(run)

    async def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        bound = self._bind(params)

        def run(conn: sqlite3.Connection) -> list[dict]:
            cur = conn.execute(sql, bound)
            return [dict(row) for row in cur.fetchall()]

        return await self._run(run)

    async def query_one(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        rows = await self.query(sql, params)
        return rows[0] if rows else None

    @staticmethod
    def _bind(params: Iterable[Any]) -> Any:
        """绑定参数。

        ⚠️ 这里挡掉一个非常隐蔽的坑：如果拿 `tuple(dict)` 去绑定，
        得到的是**键**而不是值，于是每个字段会被写成它自己的列名
        （实测：messages.id 变成字面量 'id'，第二条就撞主键）。
        dict 必须原样交给 sqlite3，它才会按命名参数正确取值。
        """
        return params if isinstance(params, dict) else tuple(params)

    async def query_one(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        rows = await self.query(sql, params)
        return rows[0] if rows else None

    # ── 角色 ────────────────────────────────────────

    async def seed_actors(self, ai_name: str, user_name: str, persona: str = "") -> None:
        """首次建库时写入默认角色。已存在则不覆盖（不破坏用户的改名）。"""
        existing = await self.query("SELECT slug FROM actors")
        if existing:
            return
        now = now_ts()
        await self.execute_many(
            "INSERT INTO actors (slug, display_name, kind, persona, enabled, created_at) "
            "VALUES (?,?,?,?,?,?)",
            [
                # slug 是结构句柄；用户改 display_name 不会影响任何代码
                ("harlan", ai_name, "ai", persona, 1, now),
                ("connor", "Connor", "ai", "", 0, now),   # 第二角色默认关闭（v0.3 §0.2）
                ("user", user_name, "user", "", 1, now),
            ],
        )

    async def actors(self, kind: str | None = None, enabled_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM actors"
        clauses, params = [], []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if enabled_only:
            clauses.append("enabled = 1")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        return await self.query(sql, params)

    async def actor_by_slug(self, slug: str) -> dict | None:
        return await self.query_one("SELECT * FROM actors WHERE slug = ?", (slug,))

    async def display_name(self, slug: str) -> str:
        """把 slug 渲染成显示名。所有 UI / 提示词都必须走这里。"""
        row = await self.actor_by_slug(slug)
        return row["display_name"] if row else slug

    async def _primary_ai_slug(self) -> str:
        row = await self.query_one(
            "SELECT slug FROM actors WHERE kind = 'ai' AND enabled = 1 ORDER BY id LIMIT 1"
        )
        if row is None:
            raise RuntimeError("没有启用的 AI 角色")
        return row["slug"]

    # ── 会话与消息 ──────────────────────────────────
    async def ensure_conversation(self, conv_id: str, title: str = "") -> dict:
        row = await self.query_one("SELECT * FROM conversations WHERE id = ?", (conv_id,))
        if row:
            return row
        now = now_ts()
        await self.execute(
            "INSERT INTO conversations (id, title, created_at, updated_at) VALUES (?,?,?,?)",
            (conv_id, title, now, now),
        )
        row = await self.query_one("SELECT * FROM conversations WHERE id = ?", (conv_id,))
        assert row is not None
        return row

    async def add_message(
        self,
        conv_id: str,
        sender: str,
        role: str,
        content: str,
        attachments: list[dict] | None = None,
        meta: dict | None = None,
        msg_id: str | None = None,
    ) -> dict:
        message = {
            "id": msg_id or new_id("msg"),
            "conv_id": conv_id,
            "sender": sender,
            "role": role,
            "content": content,
            "attachments_json": json.dumps(attachments or [], ensure_ascii=False),
            "meta_json": json.dumps(meta or {}, ensure_ascii=False),
            "created_at": now_ts(),
        }
        await self.execute(
            "INSERT INTO messages (id, conv_id, sender, role, content, attachments_json, "
            "meta_json, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                message["id"], message["conv_id"], message["sender"], message["role"],
                message["content"], message["attachments_json"], message["meta_json"],
                message["created_at"],
            ),
        )
        await self.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?", (message["created_at"], conv_id)
        )
        return message

    async def messages(self, conv_id: str, limit: int = 40, before: float | None = None) -> list[dict]:
        """取最近 limit 条，按时间正序返回（方便直接喂给模型）。"""
        if before is None:
            rows = await self.query(
                "SELECT * FROM messages WHERE conv_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
                (conv_id, limit),
            )
        else:
            rows = await self.query(
                "SELECT * FROM messages WHERE conv_id = ? AND created_at < ? "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                (conv_id, before, limit),
            )
        return list(reversed(rows))

    @staticmethod
    def decode_message(row: dict) -> dict:
        """把 JSON 字段解出来，供 API / 上下文组装使用。"""
        out = dict(row)
        try:
            out["attachments"] = json.loads(out.pop("attachments_json", "[]") or "[]")
        except json.JSONDecodeError:
            out["attachments"] = []
        try:
            out["meta"] = json.loads(out.pop("meta_json", "{}") or "{}")
        except json.JSONDecodeError:
            out["meta"] = {}
        return out

    # ── 能力注册表 ──────────────────────────────────

    async def capabilities(self, enabled_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM capabilities"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY sort_order, key"
        return await self.query(sql)

    async def upsert_capability(
        self, key: str, label: str, directive: str, enabled: bool, sort_order: int = 100
    ) -> None:
        await self.execute(
            "INSERT INTO capabilities (key, label, directive, enabled, sort_order) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET label=excluded.label, directive=excluded.directive, "
            "enabled=excluded.enabled, sort_order=excluded.sort_order",
            (key, label, directive, 1 if enabled else 0, sort_order),
        )

    async def set_capability(self, key: str, enabled: bool) -> None:
        await self.execute(
            "UPDATE capabilities SET enabled = ? WHERE key = ?", (1 if enabled else 0, key)
        )

    # ── 调度（唤醒总线）─────────────────────────────

    async def schedule(
        self,
        type_: str,
        trigger_at: float,
        origin: str,
        payload: dict | None = None,
        schedule_id: str | None = None,
    ) -> str:
        """排一个唤醒项。

        注意列数与值数必须对齐：`status` 用 schema 默认值（'active'），
        不要在这里多塞一个占位值 —— SQLite 按位置绑定，错位会把 payload
        写进 status 列，取消与领取逻辑就全失效了。
        """
        sid = schedule_id or new_id("sch")
        await self.execute(
            "INSERT INTO schedules (id, type, trigger_at, origin, payload_json, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (sid, type_, trigger_at, origin, json.dumps(payload or {}, ensure_ascii=False), now_ts()),
        )
        return sid

    async def claim_due(self, origin: str, now: float | None = None) -> dict | None:
        """原子领取一个到期的调度项。

        'claim' 是一次性领取：用带条件的 UPDATE 抢占 status，只有抢到的那一方拿到行。
        两个协程同时到点也只有一个能成功（SQLite 写锁 + status 条件保证）。

        用 `RETURNING` 拿到**本次真正被改的那一行**，而不是"再查一次最新的"——
        后者在并发下会读到别人刚抢走的行。
        """
        moment = now_ts() if now is None else now

        def run(conn: sqlite3.Connection) -> dict | None:
            row = conn.execute(
                "UPDATE schedules SET status = 'claimed' WHERE id = ("
                "  SELECT id FROM schedules WHERE origin = ? AND status = 'active' "
                "  AND trigger_at <= ? ORDER BY trigger_at LIMIT 1"
                ") AND status = 'active' "
                "RETURNING *",
                (origin, moment),
            ).fetchone()
            conn.commit()
            return dict(row) if row else None

        return await self._run(run)

    async def cancel_schedules(self, type_: str, origin: str | None = None) -> int:
        """取消待触发项。主动陪伴的冷却规则靠它：
        用户一发消息 → 删掉全部待触发的 proactive 计时器。"""
        if origin:
            return await self.execute(
                "UPDATE schedules SET status='cancelled' WHERE status='active' AND type=? AND origin=?",
                (type_, origin),
            )
        return await self.execute(
            "UPDATE schedules SET status='cancelled' WHERE status='active' AND type=?", (type_,)
        )

    # ── 记忆交付回执 ────────────────────────────────

    async def recent_delivered_ids(self, window_id: str, limit: int = 5) -> list[str]:
        """取该窗口最近成功交付的召回 ID，用于传给 Serein 做冷却。"""
        rows = await self.query(
            "SELECT delivered_ids FROM delivery_receipts WHERE window_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (window_id, limit),
        )
        seen: list[str] = []
        for row in rows:
            try:
                for item in json.loads(row["delivered_ids"] or "[]"):
                    if item not in seen:
                        seen.append(item)
            except json.JSONDecodeError:
                continue
        return seen

    async def record_receipt(self, receipt_id: str, window_id: str, delivered_ids: list[str]) -> bool:
        """登记交付回执。返回 False 表示这个 receipt 已存在（幂等重试）。

        若同一 receipt 携带了不同的 ID 集合，抛 ValueError ——
        这对应 Serein 的拒绝语义，绝不能静默接受。
        """
        existing = await self.query_one(
            "SELECT delivered_ids FROM delivery_receipts WHERE receipt_id = ?", (receipt_id,)
        )
        if existing is not None:
            try:
                previous = json.loads(existing["delivered_ids"] or "[]")
            except json.JSONDecodeError:
                previous = []
            if previous != list(delivered_ids):
                raise ValueError(
                    f"receipt {receipt_id} 已存在且携带了不同的 delivered_ids "
                    f"(原 {previous} / 新 {list(delivered_ids)})"
                )
            return False
        await self.execute(
            "INSERT INTO delivery_receipts (receipt_id, window_id, delivered_ids, created_at) "
            "VALUES (?,?,?,?)",
            (receipt_id, window_id, json.dumps(list(delivered_ids), ensure_ascii=False), now_ts()),
        )
        return True

    # ── 键值设置 ────────────────────────────────────

    async def get_setting(self, key: str, default: str = "") -> str:
        row = await self.query_one("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else default

    async def set_setting(self, key: str, value: str) -> None:
        await self.execute(
            "INSERT INTO settings (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
