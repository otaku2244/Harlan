"""Serein 记忆适配器 —— Hook 客户端。

只做两件事（契约见 Serein `docs/hook-integration.md`）：

    POST /api/hook/recall        → 待交付材料（**不是**已注入证明）
    POST /v1/host/deliveries     → 模型完整成功后才登记

设计要点（P-1 尖刺实测踩出来的）：

  * `trust_env=False` 是必须的。httpx 默认会读环境里的代理/元数据，
    连内网地址（Tailscale 100.x）会 ReadTimeout。实测：默认超时，关掉立刻 200。
  * 本适配器**不加载历史**、**不拉原话** —— Serein 明确不会自动归档宿主对话，
    所以新对话要由后端主动送进归档通道（P1b）。
  * 降级是设计内的：连不上 / 401 / 召回为空，都返回 RecallResult(ok=False) 而不抛异常。
    调用方照常继续对话。
"""

from __future__ import annotations

import hashlib
import json

import httpx

from app.config import Settings, settings as default_settings
from app.core.context import RecallResult
from app.db import Database


class SereinMemory:
    def __init__(self, db: Database, settings: Settings | None = None) -> None:
        self.db = db
        self.settings = settings or default_settings
        self.enabled = self.settings.serein_enabled
        self._client: httpx.AsyncClient | None = None

    # ── 生命周期 ────────────────────────────────────

    async def start(self) -> None:
        if self._client is None:
            # 见模块 docstring：内网地址必须 trust_env=False
            self._client = httpx.AsyncClient(timeout=30.0, trust_env=False)

    async def close(self) -> None:
        if self._client is not None:
            client, self._client = self._client, None
            await client.aclose()

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("SereinMemory 未启动，请先 await start()")
        return self._client

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.serein_gateway_key}",
            "Content-Type": "application/json",
        }

    # ── 召回 ────────────────────────────────────────

    async def recall(
        self,
        query: str,
        window_id: str,
        max_notes: int | None = None,
        extra_delivered: list[str] | None = None,
    ) -> RecallResult:
        """召回。冷却 ID 从本地回执表读取（不依赖 Serein 的交付历史）。"""
        if not self.enabled:
            return RecallResult(ok=False, skipped="Serein 未配置")

        delivered = await self.db.recent_delivered_ids(window_id, self.settings.delivery_window)
        for item in extra_delivered or []:
            if item not in delivered:
                delivered.append(item)

        payload = {
            "query": query,
            "session_id": window_id,
            "max_notes": max_notes if max_notes is not None else self.settings.serein_max_notes,
            "delivered_ids": delivered,
        }
        try:
            resp = await self.client.post(
                f"{self.settings.serein_base_url}/api/hook/recall",
                headers=self._headers(),
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as exc:
            body = exc.response.text[:300]
            return RecallResult(ok=False, error=f"HTTP {exc.response.status_code}: {body}")
        except httpx.RequestError as exc:
            return RecallResult(ok=False, error=f"连不上 Serein: {exc}")
        except json.JSONDecodeError as exc:
            return RecallResult(ok=False, error=f"Serein 返回的不是 JSON: {exc}")

        if not isinstance(data, dict):
            return RecallResult(ok=False, error="Serein 返回结构异常（非对象）")

        return RecallResult(
            ok=bool(data.get("ok")),
            recalled_ids=[str(i) for i in (data.get("recalled_ids") or [])],
            additional_context=str(data.get("additional_context") or ""),
            injected=bool(data.get("injected")),
            error="" if data.get("ok") else str(data.get("error") or "Serein 返回 ok=false"),
        )

    # ── 交付登记 ────────────────────────────────────

    @staticmethod
    def receipt_id(window_id: str, turn: int, user_text: str) -> str:
        """稳定回执 ID：同一轮重试必须是同一个值。

        换 ID 会被 Serein 拒绝（幂等语义要求 receipt 与内容绑定）。
        """
        digest = hashlib.sha256(f"{window_id}|{turn}|{user_text}".encode("utf-8")).hexdigest()
        return f"aion-{digest[:24]}"

    async def record_delivery(
        self, window_id: str, delivered_ids: list[str], turn: int, user_text: str
    ) -> dict:
        """登记实际交付的召回 ID。

        调用时机：**模型完整成功之后**。失败、中断、没把材料真的交给模型，都不要登记。

        返回 {"ok": bool, "receipt_id": str, "note": str}。
        本地回执表**先写**：即使 Serein 不可达，冷却状态也保住了（不会同一张卡连轮重复）。
        """
        if not delivered_ids:
            return {"ok": False, "note": "本轮无召回，无需登记"}

        receipt = self.receipt_id(window_id, turn, user_text)

        # 先落本地：登记成功后进程崩溃也不丢冷却
        try:
            is_new = await self.db.record_receipt(receipt, window_id, delivered_ids)
        except ValueError as exc:
            return {"ok": False, "receipt_id": receipt, "note": f"回执冲突: {exc}"}

        if not self.enabled:
            return {"ok": True, "receipt_id": receipt, "note": "Serein 未配置，仅记本地"}

        payload = {
            "receipt_id": receipt,
            "window_id": window_id,
            "delivered_ids": list(delivered_ids),
        }
        try:
            resp = await self.client.post(
                f"{self.settings.serein_base_url}/v1/host/deliveries",
                headers=self._headers(),
                json=payload,
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            return {"ok": False, "receipt_id": receipt, "local": is_new,
                    "note": f"HTTP {exc.response.status_code}: {exc.response.text[:200]}"}
        except httpx.RequestError as exc:
            return {"ok": False, "receipt_id": receipt, "local": is_new,
                    "note": f"连不上 Serein: {exc}"}

        note = "已登记" if is_new else "幂等重试（此前已登记）"
        return {"ok": True, "receipt_id": receipt, "note": note}

    # ── 归档（P1b 占位）──────────────────────────────

    async def archive_turn(self, window_id: str, user_text: str, assistant_text: str) -> dict:
        """把一轮对话送进 Serein 归档。

        ⚠️ 尚未实现：Serein 的 Hook **不会自动归档宿主对话**，
        原话要经过独立的"对话导入"通道。在 P1b 接上之前，
        Serein 的记忆会停在最后一次导入那天 —— 这是已知缺口，不是 bug。
        """
        return {"ok": False, "note": "归档通道未接（P1b 待做）"}
