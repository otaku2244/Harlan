"""Aion / Harlan 后端。

⚠️ 前两行是**有意的、必须的顺序保证**，别调整：

    app.config 在模块导入时就实例化 `settings = Settings()`。
    所以 .env 必须在 app.config 被导入**之前**加载。

    这两个 import 的顺序不能换：
        1. from app.env import load_env_file   ← 不依赖任何 app 内部模块
        2. load_env_file()                     ← 此时 os.environ 就绪
        3. 之后才允许导入 app.config / app.main

    踩过的坑：最初写成 `from app.config import load_env_file`，
    那一行本身就会先执行完 app.config（含实例化 settings），
    结果是服务能起来但配置全空 —— serein_configured / model_configured 都是 False。
"""

from app.env import load_env_file  # noqa: E402  必须先于 app.config

import os as _os  # noqa: E402

# AION_SKIP_ENV=1 时不读 .env。
# 测试用：有些用例要验证"未配置时如何降级"，本机存在 .env 会让它们失去意义。
if _os.environ.get("AION_SKIP_ENV", "") in {"", "0", "false"}:
    load_env_file()

import sys as _sys  # noqa: E402

# 诊断：AION_ENV_TRACE=1 时打印加载结果（不含密钥）
if _os.environ.get("AION_ENV_TRACE", "") not in {"", "0", "false"}:
    from app.env import BASE_DIR as _BASE_DIR  # noqa: E402

    print(
        f"[app] .env 加载完成 BASE_DIR={_BASE_DIR} "
        f"SEREIN_BASE_URL={_os.environ.get('SEREIN_BASE_URL', '(空)')!r} "
        f"MODEL_NAME={_os.environ.get('MODEL_NAME', '(空)')!r}",
        file=_sys.stderr,
        flush=True,
    )
