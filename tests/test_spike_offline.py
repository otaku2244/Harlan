#!/usr/bin/env python3
"""在线集成测试 —— 把假 Serein 和 spike 串起来，验证完整 Hook 流程。

与 --self-test 的区别：
    --self-test  只验证结构（组装、降级、状态），不联网
    本文件       真的发 HTTP、真的走召回 → 组装 → 登记交付，验证契约与冷却

本机可跑（无需 VPS、无需真 Key）。它证明不了"你的 Serein 是这个形状"，
但它能证明"**如果** Serein 是这个形状，spike 的每个分支都正确"。

运行：
    py tests/test_spike_offline.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
from http.server import HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    mark = "[OK]" if condition else "[X] "
    print(f"  {mark} {name}{(' -- ' + detail) if detail else ''}")
    if not condition:
        _failures.append(name)


def load_module(path: Path, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    fake = load_module(ROOT / "docs" / "dev-fake-serein.py", "fake_serein")
    spike = load_module(ROOT / "spike.py", "spike_mod")

    # 环境变量要在 Config 实例化之前设好
    server = HTTPServer(("127.0.0.1", 0), fake.Handler)      # 0 = 让系统分配空闲端口
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    os.environ["SEREIN_BASE_URL"] = f"http://127.0.0.1:{port}"
    os.environ["SEREIN_GATEWAY_KEY"] = "fake-key-for-test"
    os.environ["SEREIN_WINDOW_ID"] = f"test-{os.getpid()}"
    os.environ["SPIKE_PROVIDER"] = "echo"                    # 不调真模型
    os.environ["AI_DISPLAY_NAME"] = "Harlan"
    os.environ["AI_PERSONA"] = "你是一个沉稳、话不多的人。"

    print(f"假 Serein 监听 127.0.0.1:{port}\n")

    cfg = spike.Config()
    hook = spike.SereinHook(cfg)

    # ── 1. 连通性与鉴权 ──────────────────────────────
    print("[1] 召回接口")
    recall = hook.recall("上次读书会定在什么时候？", [])
    check("recall 返回 ok", recall.get("ok") is True, json.dumps(recall, ensure_ascii=False)[:120])
    check("召回带回了 ID", bool(recall.get("recalled_ids")), str(recall.get("recalled_ids")))
    check("召回带回了上下文", bool(recall.get("additional_context")))
    check("injected 为 false（待交付材料，非已注入）", recall.get("injected") is False)

    # ── 2. 上下文组装 ────────────────────────────────
    print("\n[2] 上下文组装")
    messages = spike.build_messages(
        cfg, recall, "在家", [{"role": "user", "content": "上次读书会定在什么时候？"}]
    )
    system = messages[0]["content"]
    check("召回正文进入 system", "读书会" in system)
    check("外层的 serein 标记存在", "<serein_live_context>" in system)
    check("明确声明不是用户指令", "不是用户指令" in system)

    # ── 3. 登记交付 ──────────────────────────────────
    print("\n[3] 交付登记")
    recalled_ids = list(recall["recalled_ids"])
    first = hook.record_delivery(recalled_ids, turn=1, user_text="上次读书会定在什么时候？")
    check("首次登记成功", first.get("ok") is True, str(first))
    check("返回了稳定 receipt", bool(first.get("receipt_id")), str(first.get("receipt_id")))
    check("登记后立即落盘冷却状态",
          set(recalled_ids).issubset(set(spike.load_delivered(cfg.window_id))),
          str(spike.load_delivered(cfg.window_id)))

    # ── 4. 幂等性（同 receipt 重试）───────────────────
    print("\n[4] 幂等重试")
    retry = hook.record_delivery(recalled_ids, turn=1, user_text="上次读书会定在什么时候？")
    check("同 receipt 重试仍成功", retry.get("ok") is True, str(retry))
    check("重试得到同一个 receipt", retry.get("receipt_id") == first.get("receipt_id"))

    # ── 5. 换 ID 集合必须被拒 ────────────────────────
    print("\n[5] 换 ID 集合应被拒绝")
    conflict = hook.record_delivery(["scene:something_else"], turn=1,
                                    user_text="上次读书会定在什么时候？")
    check("换 ID 被拒绝（非 ok）", conflict.get("ok") is False, str(conflict))

    # ── 6. 冷却：已交付的卡不应再出现 ────────────────
    print("\n[6] 冷却行为")
    delivered = spike.load_delivered(cfg.window_id)
    check("交付历史已落盘", set(recalled_ids).issubset(set(delivered)), str(delivered))
    second = hook.recall("再说一次读书会", delivered)
    check("已交付的卡不再召回", not (set(second.get("recalled_ids") or []) & set(recalled_ids)),
          str(second.get("recalled_ids")))

    # ── 7. Serein 不可达时必须降级而不是崩 ───────────
    print("\n[7] 不可达降级")
    broken_cfg = spike.Config()
    broken_cfg.serein_base_url = "http://127.0.0.1:1"        # 必然拒绝连接
    broken_cfg.serein_gateway_key = "x"
    broken = spike.SereinHook(broken_cfg).recall("测试", [])
    check("连不上时不抛异常", broken.get("ok") is False)
    check("连不上时仍可继续组装上下文",
          "serein_live_context" not in spike.build_messages(
              cfg, broken, "", [{"role": "user", "content": "hi"}])[0]["content"])

    # ── 8. 鉴权缺失 ──────────────────────────────────
    print("\n[8] 鉴权")
    no_auth_cfg = spike.Config()
    no_auth_cfg.serein_base_url = f"http://127.0.0.1:{port}"
    no_auth_cfg.serein_gateway_key = ""
    check("无 Key 时不发请求", spike.SereinHook(no_auth_cfg).enabled is False)

    server.shutdown()

    print()
    if _failures:
        print(f"集成测试失败 {len(_failures)} 项：{_failures}")
        return 1
    print("集成测试全部通过（假 Serein）。真链路仍须在 VPS 上验证。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
