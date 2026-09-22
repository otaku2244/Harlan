"""配置 —— 全部来自环境变量，带开发默认值。

约定：
  * 密钥只从环境变量读，绝不写进代码或前端
  * 默认值面向"本机开发"，VPS 上用 .env 覆盖
"""

from __future__ import annotations

import os
from pathlib import Path

# BASE_DIR 与 .env 加载器住在 app/env.py（那个文件刻意不依赖任何 app 内部模块，
# 因为 .env 必须在 app.config 被导入之前生效 —— 见 app/__init__.py 的说明）。
from app.env import BASE_DIR


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


class Settings:
    """进程级配置。测试里直接改属性即可。"""

    def __init__(self) -> None:
        # ── 数据库 ──────────────────────────────────
        self.db_path = Path(os.environ.get("AION_DB_PATH", "") or (BASE_DIR / "data" / "aion.db"))

        # ── Serein（记忆）────────────────────────────
        self.serein_base_url = os.environ.get("SEREIN_BASE_URL", "").rstrip("/")
        self.serein_gateway_key = os.environ.get("SEREIN_GATEWAY_KEY", "")
        self.serein_max_notes = _int("SEREIN_MAX_NOTES", 2)
        # 交付冷却只保留最近 N 次（Serein 文档建议最近五次成功交付）
        self.delivery_window = _int("SEREIN_DELIVERY_WINDOW", 5)

        # ── 模型 ────────────────────────────────────
        self.model_base_url = os.environ.get("MODEL_BASE_URL", "").rstrip("/")
        self.model_api_key = os.environ.get("MODEL_API_KEY", "")
        self.model_name = os.environ.get("MODEL_NAME", "")
        # 模型端点在公网时可能需要系统代理；Serein 在 Tailscale 内网，永远不走代理
        self.model_trust_env = _bool("MODEL_TRUST_ENV", True)
        self.model_timeout = _int("MODEL_TIMEOUT", 120)
        # 间歇性 502/429 的退避重试次数。走 Serein 网关时实测会遇到。
        self.model_retries = _int("MODEL_RETRIES", 3)

        # ── 对话行为 ────────────────────────────────
        # 指令续轮的最大次数，防止模型反复吐指令导致死循环
        self.max_directive_rounds = _int("MAX_DIRECTIVE_ROUNDS", 3)
        # 注入模型的历史消息条数上限
        self.history_limit = _int("HISTORY_LIMIT", 40)

        # ── 服务 ────────────────────────────────────
        self.host = os.environ.get("AION_HOST", "127.0.0.1")
        self.port = _int("AION_PORT", 8080)

        # ── 唤醒调度（P3）────────────────────────────
        # 轮询间隔。30 秒与 AionsHome 的 ScheduleManager 一致。
        self.scheduler_poll_seconds = _int("SCHEDULER_POLL_SECONDS", 30)
        # 空闲自主的随机间隔区间（分钟）。原项目默认 120，且最小 clamp 到 5。
        self.idle_min_minutes = _int("IDLE_MIN_MINUTES", 120)
        self.idle_max_minutes = _int("IDLE_MAX_MINUTES", 120)
        # 主动开口落在哪个会话里（前端据此显示）
        self.wake_conv_id = os.environ.get("WAKE_CONV_ID", "harlan")
        # 记忆窗口前缀：每个角色一个稳定窗口，避免冷却串台
        self.wake_window_prefix = os.environ.get("WAKE_WINDOW_PREFIX", "wake")
        # 是否启用后台调度（测试里关掉，避免后台任务干扰）
        self.scheduler_enabled = _bool("SCHEDULER_ENABLED", True)
        # 主动开口的输出上限。主动开口通常只有一两句话，
        # 限长能省 token，也避免它长篇大论。
        self.wake_max_tokens = _int("WAKE_MAX_TOKENS", 400)

        # ── 角色默认（首次建库时写入 actors 表）─────
        self.default_ai_name = os.environ.get("AI_DISPLAY_NAME", "Harlan")
        self.default_user_name = os.environ.get("USER_DISPLAY_NAME", "你")
        self.default_persona = os.environ.get("AI_PERSONA", "")

    def mask(self, secret: str) -> str:
        if not secret:
            return "(未设置)"
        if len(secret) <= 8:
            return "***"
        return f"{secret[:4]}…{secret[-4:]}（长度 {len(secret)}）"

    @property
    def serein_enabled(self) -> bool:
        return bool(self.serein_base_url and self.serein_gateway_key)

    @property
    def model_enabled(self) -> bool:
        return bool(self.model_base_url and self.model_name)


settings = Settings()

# 诊断：AION_ENV_TRACE=1 时打印实例化时刻读到的值（不含密钥）
if os.environ.get("AION_ENV_TRACE", "") not in {"", "0", "false"}:
    import sys as _sys

    print(
        f"[app/config] 实例化 settings：SEREIN_BASE_URL="
        f"{settings.serein_base_url or '(空)'!r} MODEL_NAME={settings.model_name or '(空)'!r}",
        file=_sys.stderr,
        flush=True,
    )
