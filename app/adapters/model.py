"""模型适配器 —— OpenAI 兼容端点的流式调用。

支持两种模式：
  * `stream(...)`  逐块产出文本（给 SSE 转发用）
  * `complete(...)` 收集完整回复（给指令续轮用）

两个实测得来的注意事项：

1. **trust_env**：模型端点在公网时可能需要系统代理，
   与 Serein（Tailscale 内网，强制不走代理）相反，所以分开配置。

2. **间歇性 502 与"小 max_tokens 返回空"**：走 Serein 网关时实测到
   * 同一 payload 时而 200 时而 502 —— 重试即恢复
   * `max_tokens` 偏小时（4/8/16/32）会返回**空内容**且不报错，
     约 128 以上才正常
   两条都会让用户看到一条**没有任何解释的空回复**。所以这里：
     * 带退避重试，且只在"还没吐出任何内容"时重试
     * 空回复**不算成功** —— 值得重试一次，因为可能是网关抖动
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import httpx

from app.config import Settings, settings as default_settings


class ModelError(RuntimeError):
    pass


# 哪些状态码值得重试：网关/上游抖动，而不是我们的请求有问题
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class ModelClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or default_settings

    @property
    def enabled(self) -> bool:
        return self.settings.model_enabled

    @property
    def max_attempts(self) -> int:
        return max(1, self.settings.model_retries)

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
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        """流式产出文本增量。

        非流式的错误在开始时就能发现；流中途断开则向上抛 ModelError，
        调用方负责决定是否保留已产出的部分。
        """
        if not self.enabled:
            raise ModelError("模型未配置（MODEL_BASE_URL / MODEL_NAME）")

        overrides: dict = {}
        if temperature is not None:
            overrides["temperature"] = temperature
        if max_tokens is not None:
            overrides["max_tokens"] = max_tokens

        url = f"{self.settings.model_base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.settings.model_api_key}",
            "Content-Type": "application/json",
        }
        timeout = httpx.Timeout(self.settings.model_timeout, connect=15.0)

        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            emitted = False
            try:
                async with httpx.AsyncClient(
                    timeout=timeout, trust_env=self.settings.model_trust_env
                ) as client:
                    async with client.stream(
                        "POST", url, headers=headers,
                        json=self._payload(messages, True, **overrides),
                    ) as resp:
                        if resp.status_code >= 400:
                            body = (await resp.aread()).decode("utf-8", "replace")[:400]
                            if resp.status_code in RETRYABLE_STATUS and attempt < self.max_attempts:
                                last_error = ModelError(
                                    f"模型返回 HTTP {resp.status_code}（第 {attempt} 次）: {body}"
                                )
                                break       # 跳出 with，走重试
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
                                emitted = True
                                yield piece

                if not emitted and attempt < self.max_attempts:
                    # 200 但一个字都没有。实测这在 Serein 网关上是**可重试**的
                    # （小 max_tokens 或上游抖动都会这样），不重试的话
                    # 用户会收到一条没有解释的空回复。
                    last_error = ModelError(f"模型返回空内容（第 {attempt} 次）")
                    await asyncio.sleep(min(2 ** (attempt - 1), 4))
                    continue
                return          # 正常结束（或有内容但未耗尽重试）
            except (httpx.RequestError, ModelError) as exc:
                last_error = exc
                # 已经吐出内容就不重试 —— 重试会导致前端看到重复文本
                if emitted or attempt >= self.max_attempts:
                    raise
            if attempt < self.max_attempts:
                await asyncio.sleep(min(2 ** (attempt - 1), 4))

        if last_error is not None:
            raise ModelError(f"重试 {self.max_attempts} 次仍失败：{last_error}")

    async def complete(self, messages: list[dict], **overrides) -> str:
        """收集完整回复。指令续轮用它。"""
        chunks: list[str] = []
        async for piece in self.stream(messages, **overrides):
            chunks.append(piece)
        return "".join(chunks)
