#!/usr/bin/env python3
"""用 Python 发一次真实对话，验证中文编码端到端正确。

PowerShell 的 Invoke-WebRequest 传 body 时会破坏 UTF-8（实测把「江清漪」
变成 æ±æ¸æ¼ª），所以中文场景必须用 Python 或文件传原始字节来测。

用法：py docs/verify-utf8-roundtrip.py
"""
from __future__ import annotations

import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8080"
SENTENCES = [
    "江清漪 最近怎么样",
    "你还记得我上周跟你提过的那件事吗",
]


def post(path: str, payload: dict, timeout: int = 180) -> tuple[int, str]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=body, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8")


def get(path: str) -> dict:
    req = urllib.request.Request(BASE + path, headers={"User-Agent": "utf8-check"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def parse_sse(text: str) -> tuple[dict, str, str]:
    types: dict[str, int] = {}
    chunks: list[str] = []
    error = ""
    for line in text.splitlines():
        if not line.startswith("data: "):
            continue
        try:
            item = json.loads(line[6:])
        except json.JSONDecodeError:
            continue
        kind = item.get("type", "?")
        types[kind] = types.get(kind, 0) + 1
        if kind == "chunk":
            chunks.append(item.get("content", ""))
        if kind == "stream_error":
            error = item.get("message", "")
    return types, "".join(chunks), error


def looks_mojibake(text: str) -> bool:
    """判断是否出现 UTF-8 被当 Latin-1 解码的典型字符。"""
    markers = ("Ã", "Â", "æ", "å", "ç", "è", "é", "ä", "æ±")
    return any(m in text for m in markers)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    failures = 0
    for sentence in SENTENCES:
        print("=" * 66)
        print(f"发送：{sentence}")
        print("=" * 66)

        conv = json.loads(
            urllib.request.urlopen(
                urllib.request.Request(
                    BASE + "/api/conversations",
                    data=json.dumps({"title": "utf8-check"}).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                ),
                timeout=30,
            ).read().decode()
        )
        conv_id = conv["id"]

        status, text = post(f"/api/conversations/{conv_id}/send",
                            {"content": sentence, "context_limit": 20})
        types, reply, error = parse_sse(text)
        print(f"  HTTP {status}  事件 {sorted(types)}")
        if error:
            print(f"  ✗ stream_error: {error}")
            failures += 1

        print(f"  回复：{reply.strip()}")

        # 落库校验：用户消息必须与发送内容逐字一致
        data = get(f"/api/conversations/{conv_id}/messages")
        user_msgs = [m for m in data.get("messages", []) if m["role"] == "user"]
        stored = user_msgs[0]["content"] if user_msgs else ""
        same = stored == sentence
        print(f"  落库用户消息：{stored!r}")
        print(f"  {'✓' if same else '✗'} 与发送内容一致")
        if not same:
            failures += 1
            print(f"     发送: {sentence!r}")
            print(f"     存储: {stored!r}")
            print(f"     疑似 mojibake: {looks_mojibake(stored)}")

        if looks_mojibake(reply):
            print("  ✗ 回复里出现 mojibake 特征字符")
            failures += 1
        print()

    if failures:
        print(f"发现 {failures} 处编码问题")
        return 1
    print("中文编码端到端正确：发送 → 落库 → 模型回复 全部保持原文。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
