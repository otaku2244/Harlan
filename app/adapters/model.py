"""模型适配器 —— OpenAI 兼容端点的流式调用。

支持两种模式：
  * `stream(...)`  逐块产出文本（给 SSE 转发用）
  * `complete(...)` 收集完整回复（给指令续轮用）

注意 `trust_env`：模型端点在公网时可能需要系统代理，
与 Serein（Tailscale 内网，强制不走代理）相反，所以分开配置。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

from app.config import Settings, settings as default_settings


class ModelError(RuntimeError):
    pass


class ModelClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or default_settings

    @property
    def enabled(self) -> bool:
        return self.settings.model_enabled

    def _payload(self, messages: list[dict], stream: bool, **overrides) -> dict:
        payload = {
            "model": self.settings.model_name,
            "messages": messages,
            "stream": stream,
        }
        payload.update(overrides)
        return payload

    async def stream(
        self,
        messages: list[dict],
        *,
        temperature: float | None = None,
    ) -> AsyncIterator[str]:
        """流式产出文本增量。

        非流式的错误在开始时就能发现；流中途断开则向上抛 ModelError，
        调用方负责决定是否保留已产出的部分。
        """
        if not self.enabled:
            raise ModelError("模型未配置（MODEL_BASE_URL / MODEL_NAME）")

        overrides = {}
        if temperature is not None:
            overrides["temperature"] = temperature

        url = f"{self.settings.model_base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.settings.model_api_key}",
            "Content-Type": "application/json",
        }
        timeout = httpx.Timeout(self.settings.model_timeout, connect=15.0)

        async with httpx.AsyncClient(timeout=timeout, trust_env=self.settings.model_trust_env) as client:
            async with client.stream(
                "POST", url, headers=headers, json=self._payload(messages, True, **overrides)
            ) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", "replace")[:500]
                    raise ModelError(f"模型返回 HTTP {resp.status_code}: {body}")
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        delta = json.loads(data)["choices"][0].get("delta") or {}
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue
                    piece = delta.get("content")
                    if piece:
                        yield piece

    async def complete(self, messages: list[dict], **overrides) -> str:
        """收集完整回复。指令续轮用它。"""
        chunks: list[str] = []
        async for piece in self.stream(messages, **overrides):
            chunks.append(piece)
        return "".join(chunks)
