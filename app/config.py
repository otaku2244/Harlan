"""配置 —— 全部来自环境变量，带开发默认值。

约定：
  * 密钥只从环境变量读，绝不写进代码或前端
  * 默认值面向"本机开发"，VPS 上用 .env 覆盖
"""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


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


def load_env_file(path: Path | None = None) -> None:
    """极简 .env 读取（不引入 python-dotenv）。已存在的环境变量优先。"""
    target = path or (BASE_DIR / ".env")
    if not target.exists():
        return
    for raw in target.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


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

        # ── 对话行为 ────────────────────────────────
        # 指令续轮的最大次数，防止模型反复吐指令导致死循环
        self.max_directive_rounds = _int("MAX_DIRECTIVE_ROUNDS", 3)
        # 注入模型的历史消息条数上限
        self.history_limit = _int("HISTORY_LIMIT", 40)

        # ── 服务 ────────────────────────────────────
        self.host = os.environ.get("AION_HOST", "127.0.0.1")
        self.port = _int("AION_PORT", 8080)

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
