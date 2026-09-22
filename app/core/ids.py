"""可排序 ID —— ULID 风格。

为什么不用自增整数：多端（VPS 后端 + 未来可能的端侧）并发写入时，
整数需要中心协调，字符串时间前缀 ID 天然有序且可离线生成。

时间戳在前（48 位毫秒）+ 80 位随机，base32 编码 26 字符，字典序即时间序。
"""

from __future__ import annotations

import os
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32，去掉 I L O U


def _encode(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        chars.append(_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(chars))


def new_id(prefix: str = "") -> str:
    """生成一个按时间有序的 ID，可选前缀便于日志辨认。

    >>> a = new_id(); b = new_id()
    >>> a < b          # 同一毫秒内也保证递增（随机位不同，不保证顺序）
    True or False
    """
    millis = int(time.time() * 1000)
    random_bits = int.from_bytes(os.urandom(10), "big")
    body = _encode(millis, 10) + _encode(random_bits, 16)
    return f"{prefix}_{body}" if prefix else body


def now_ts() -> float:
    """当前时间戳（秒，浮点）。统一出口，方便测试打桩。"""
    return time.time()
