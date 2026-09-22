"""WebSocket 多端同步 —— 一台后端，手机 / PC 同时在线。

协议是一个信封：`{"type": "...", "data": {...}}`。

为什么第一天就要有信封：AionsHome 的经验是，后来加的能力
（TTS 分片、搜索状态、摄像头指示器、玩具指令回执）都是**新的事件类型**。
前端只要对未知 type 静默忽略，后端就能随时加事件而不破坏旧客户端。

服务端 → 客户端：
    hello            连接建立，带 client_id 与在线数
    clients_changed  在线客户端数变化
    message          新消息落库后广播
    stream_start     某个 actor 开始流式回复
    stream_delta     流式增量
    stream_end       流式结束（带最终可见文本）
    capability       能力执行的状态事件（如 {"type":"next_chat","minutes":5}）
    error            服务端错误

架构要点：**每个连接一个专职发送任务**，消息先进 asyncio.Queue。
理由（都是实测踩出来的）：

  1. 对同一个 socket 从多个任务并发发送会挂住 —— 广播是 HTTP 请求协程发起的，
     而连接由 WS 端点协程持有，跨任务同时 send 会死锁。
  2. 一旦队列积压（手机切后台不读消息），不能阻塞广播方，
     所以队列有上限，满了就丢弃**最旧的**流式增量：
     断开前最后几帧比开头的字更重要。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import WebSocket

from app.core.ids import new_id

# 每个连接的发送队列上限。流式回复会产生大量 delta，积压时丢弃最旧的。
QUEUE_MAX = 200


class _Client:
    """一条连接。queue + 专职发送任务。"""

    __slots__ = ("id", "websocket", "queue", "task")

    def __init__(self, client_id: str, websocket: WebSocket) -> None:
        self.id = client_id
        self.websocket = websocket
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=QUEUE_MAX)
        self.task: asyncio.Task | None = None


class ConnectionManager:
    def __init__(self) -> None:
        self._clients: dict[str, _Client] = {}
        self._lock = asyncio.Lock()

    # ── 连接生命周期 ────────────────────────────────

    async def connect(self, websocket: WebSocket, client_id: str | None = None) -> str:
        await websocket.accept()
        cid = client_id or new_id("cli")
        client = _Client(cid, websocket)
        async with self._lock:
            previous = self._clients.pop(cid, None)
            self._clients[cid] = client
        if previous is not None and previous.task is not None:
            previous.task.cancel()
        # 发送任务在 accept 之后、同一个事件循环里创建
        client.task = asyncio.create_task(self._sender(client))
        return cid

    async def disconnect(self, client_id: str) -> None:
        async with self._lock:
            client = self._clients.pop(client_id, None)
        if client is not None and client.task is not None:
            client.task.cancel()

    async def _sender(self, client: _Client) -> None:
        """专职发送：串行地把队列里的消息写进 socket。"""
        try:
            while True:
                payload = await client.queue.get()
                await client.websocket.send_text(payload)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — 发不出去即视为断开
            await self.disconnect(client.id)

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def client_ids(self) -> list[str]:
        return list(self._clients)

    # ── 发送 ────────────────────────────────────────

    def _enqueue(self, client: _Client, payload: dict) -> bool:
        """入队。满了丢最旧的 —— 优先保住最新的内容。"""
        text = json.dumps(payload, ensure_ascii=False)
        try:
            client.queue.put_nowait(text)
            return True
        except asyncio.QueueFull:
            try:
                client.queue.get_nowait()          # 丢最旧
                client.queue.put_nowait(text)
                return True
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                return False

    def send_to(self, client_id: str, payload: dict) -> bool:
        """入队一条给指定客户端。**不 await**，所以不会被慢客户端拖住。"""
        client = self._clients.get(client_id)
        if client is None:
            return False
        return self._enqueue(client, payload)

    async def broadcast(self, payload: dict, exclude: str | None = None) -> int:
        """广播给所有在线客户端。返回入队成功数。

        因为只是入队（不 await 网络写），一个卡住的客户端不会拖慢其他人。
        """
        targets = [c for cid, c in self._clients.items() if cid != exclude]
        return sum(1 for client in targets if self._enqueue(client, payload))

    async def broadcast_event(self, type_: str, data: Any = None,
                              exclude: str | None = None) -> int:
        return await self.broadcast({"type": type_, "data": data}, exclude=exclude)


manager = ConnectionManager()
