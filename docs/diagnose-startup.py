#!/usr/bin/env python3
"""模拟浏览器启动流程，找出哪一步会卡住/失败，导致"点发送没反应"。

背景（从 chat.js 读出来的）：
    init() 第一件事是
        const bootstrap = Promise.all([
            api("GET", "/api/models"),
            api("GET", "/api/worldbook"),
            api("GET", "/api/conversations"),
        ]);
        [models, worldBook, conversations] = await bootstrap;
    任一失败或**超时**（api() 默认 15s）就会抛出，init() 中断。
    中断之后 currentConvId 保持 null，而 send() 第一行是
        if (...) || !currentConvId || sending) return;
    —— 静默返回，表现就是"点发送没反应"。

所以这里把 init() 之后会用到的接口按顺序全部打一遍，记录状态与耗时。
耗时异常的（接近 15s）就是嫌疑人。

用法：
    py docs/diagnose-startup.py
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8080"

# 按 init() 的真实调用顺序
ENDPOINTS = [
    ("GET", "/api/models", None),
    ("GET", "/api/worldbook", None),
    ("GET", "/api/conversations", None),
    ("GET", "/api/chatroom/config", None),
    ("GET", "/api/proactive-companionship", None),
    ("GET", "/api/tts/voices", None),
    ("GET", "/api/starred-messages", None),
    ("GET", "/api/settings/temperature", None),
    ("GET", "/api/files", None),
    # 前端用于增量同步；返回慢会拖住界面
    ("GET", "/api/sync/changes?after=0&limit=200", None),
]


def call(method: str, path: str, body: dict | None = None, timeout: int = 20):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "startup-diag")
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, round(time.time() - started, 2), len(raw), None
    except urllib.error.HTTPError as exc:
        return exc.code, round(time.time() - started, 2), 0, exc.read()[:120].decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return 0, round(time.time() - started, 2), 0, f"{type(exc).__name__}: {exc}"


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    print(f"模拟前端启动流程（服务 {BASE}）\n")
    print(f"  {'接口':<44} {'状态':>5} {'耗时':>7}  说明")
    print("  " + "-" * 74)

    slow: list[str] = []
    failed: list[tuple[str, str]] = []

    for method, path, body in ENDPOINTS:
        status, elapsed, size, error = call(method, path, body)
        if status == 200:
            note = f"{size} 字节"
        elif status == 0:
            note = f"连接失败: {error}"
            failed.append((path, note))
        else:
            note = f"HTTP {status} {error or ''}"
            failed.append((path, note))
        if elapsed >= 5:
            slow.append(f"{path} ({elapsed}s)")
        mark = "OK" if status == 200 else "X "
        print(f"  [{mark}] {path:<42} {status:>5} {elapsed:>6}s  {note}")

    print()
    if slow:
        print("⚠️ 慢接口（可能拖住 Promise.all，api() 上限 15s）：")
        for item in slow:
            print(f"   {item}")
    if failed:
        print("⚠️ 失败接口（init() 若 await 到它们会中断）：")
        for path, note in failed:
            print(f"   {path}  → {note}")

    # 关键结论
    print()
    print("=" * 76)
    critical = ["/api/models", "/api/worldbook", "/api/conversations"]
    bad_critical = [p for p, _ in failed if p in critical]
    if bad_critical:
        print("结论：init() 直接依赖的三个接口里有失败的：")
        for p in bad_critical:
            print(f"   {p}")
        print("      这就是「点发送没反应」的原因 —— init() 中断，currentConvId 为 null。")
    elif slow:
        print("结论：三个关键接口都通，但有慢接口。若慢接口在 init() 的 await 链上，")
        print("      仍可能造成长时间卡住（表现为页面能开但按钮没反应）。")
    else:
        print("结论：启动流程涉及的后端接口全部正常且都很快。")
        print("      那么「点发送没反应」的原因在**浏览器端**，需要看 Console 的报错。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
