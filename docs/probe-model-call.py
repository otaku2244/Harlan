#!/usr/bin/env python3
"""单独测模型调用 —— 定位 /api/diagnostics 里 model.ok=False 的原因。

怀疑：诊断用的 max_tokens=8 太小，被流式协议截断成空回复。
用法：py docs/probe-model-call.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.env import load_env_file  # noqa: E402

load_env_file()

from app.adapters.model import ModelClient  # noqa: E402
from app.config import settings  # noqa: E402


async def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    print(f"base_url      = {settings.model_base_url}")
    print(f"model         = {settings.model_name}")
    print(f"enabled       = {ModelClient(settings).enabled}")
    print(f"retries       = {settings.model_retries}")
    print()

    client = ModelClient(settings)
    for max_tokens in (None, 4, 8, 16, 32, 128):
        label = "未限制" if max_tokens is None else str(max_tokens)
        try:
            reply = await client.complete(
                [{"role": "user", "content": "只回三个字：我在呢"}],
                max_tokens=max_tokens,
            )
            print(f"  max_tokens={label:<6} → {len(reply):>3} 字  {reply[:50]!r}")
        except Exception as exc:  # noqa: BLE001
            print(f"  max_tokens={label:<6} → 失败 {type(exc).__name__}: {str(exc)[:100]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
