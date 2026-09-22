#!/usr/bin/env python3
"""假 Serein —— 本机离线验证 Hook 契约用。

它不是 Serein 的替代品，只是在没有 VPS 可连时，让你能在本机确认
「请求形状对不对」和「spike 的降级/登记路径会不会炸」。

它实现两个端点（形状照 Serein docs/hook-integration.md）：
    POST /api/hook/recall        → {"ok":true,"recalled_ids":[...],"additional_context":"...","injected":false}
    POST /v1/host/deliveries     → 记录交付，重复 receipt 幂等，换 ID 则拒绝

启动：
    python docs/dev-fake-serein.py            # 监听 127.0.0.1:8799
然后：
    SEREIN_BASE_URL=http://127.0.0.1:8799 SEREIN_GATEWAY_KEY=fake python spike.py "测试"
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = 8799
# 假语料：模拟 Serein 返回的 additional_context 形状
FAKE_CONTEXT = (
    "[Serein Gateway Full Recall]\n"
    "Event · 2026-09-18 读书会\n"
    "你们约好周三晚上七点在街角书店碰面，对方说会带一本《夜航西飞》。\n"
    "Scene · 2026-09-20 深夜通话\n"
    "你提到最近项目压力大，连续几天加班到凌晨。"
)

_deliveries: dict[str, list[str]] = {}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def do_POST(self) -> None:  # noqa: N802
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            self._send(401, {"ok": False, "error": "缺少 Bearer token"})
            return

        payload = self._read_json()

        if self.path == "/api/hook/recall":
            print(f"[fake-serein] recall session={payload.get('session_id')!r} "
                  f"query={payload.get('query')!r} delivered={payload.get('delivered_ids')}")
            delivered = set(payload.get("delivered_ids") or [])
            # 模拟冷却：已交付过的卡片本轮不再出现
            recalled = [i for i in ["scene:demo_bookclub", "event:demo_latenight"] if i not in delivered]
            self._send(200, {
                "ok": True,
                "recalled_ids": recalled,
                "additional_context": FAKE_CONTEXT if recalled else "",
                "injected": False,
            })
            return

        if self.path == "/v1/host/deliveries":
            receipt = str(payload.get("receipt_id") or "")
            ids = list(payload.get("delivered_ids") or [])
            if not receipt:
                self._send(400, {"ok": False, "error": "缺少 receipt_id"})
                return
            if receipt in _deliveries:
                if _deliveries[receipt] != ids:
                    print(f"[fake-serein] 拒绝：receipt {receipt[:16]}… 换了 ID 集合")
                    self._send(409, {"ok": False, "error": "同一 receipt 不允许更换 delivered_ids"})
                    return
                print(f"[fake-serein] 幂等重试 receipt={receipt[:16]}…")
                self._send(200, {"ok": True, "idempotent": True})
                return
            _deliveries[receipt] = ids
            print(f"[fake-serein] 登记 receipt={receipt[:16]}… ids={ids}（累计 {len(_deliveries)} 条）")
            self._send(200, {"ok": True})
            return

        self._send(404, {"ok": False, "error": f"未知路径 {self.path}"})

    def log_message(self, *args) -> None:  # 静音默认访问日志
        return


def main() -> int:
    print(f"假 Serein 启动于 http://127.0.0.1:{PORT}")
    print("端点：POST /api/hook/recall · POST /v1/host/deliveries")
    print("任意非空 Bearer token 均可通过。Ctrl+C 退出。\n")
    try:
        HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
