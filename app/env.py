"""极简 .env 读取 —— 刻意**不导入任何 app 内部模块**。

为什么单独一个文件：`app.config` 在模块导入时就实例化 `settings = Settings()`，
所以 .env 必须在 `app.config` 被导入**之前**加载。

如果把这个函数放在 app.config 里，那么 `from app.config import load_env_file`
这一行本身就会先执行完整个 app.config（包括实例化 settings），
.env 就来不及生效 —— 实测过：服务能起，但配置全是空的。

所以这里不 import app.*，由 app/__init__.py 在最早时机调用。
"""

from __future__ import annotations

import os
from pathlib import Path

# 仓库根目录：app/env.py -> app/ -> 根
BASE_DIR = Path(__file__).resolve().parent.parent


def load_env_file(path: Path | None = None) -> Path | None:
    """读取 .env 写入 os.environ。已存在的环境变量优先（不覆盖）。

    返回实际读取的文件路径；没读到返回 None。
    """
    target = path or (BASE_DIR / ".env")
    if not target.exists():
        return None
    for raw in target.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
    return target
