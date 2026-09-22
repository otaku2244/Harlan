#!/usr/bin/env python3
"""P0 HTTP 层集成测试 —— 用假模型跑完整一轮对话。

覆盖：启动、bootstrap、发消息（SSE）、指令续轮、落库、多端 WS 广播、冷却、能力开关。

不联网、不需要 VPS、不需要真 Key。

运行：py tests/test_http.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    mark = "[OK]" if condition else "[X] "
    print(f"  {mark} {name}{(' -- ' + detail) if detail else ''}")
    if not condition:
        _failures.append(name)


class FakeModel:
    """按脚本产出的假模型。每次 stream() 消费下一条脚本。"""

    def __init__(self, scripts: list[list[str]] | None = None) -> None:
        self.scripts = list(scripts or [])
        self.calls: list[list[dict]] = []
        self.enabled = True

    def push(self, chunks: list[str]) -> None:
        self.scripts.append(chunks)

    async def stream(self, messages, **overrides):
        self.calls.append(messages)
        chunks = self.scripts.pop(0) if self.scripts else ["（默认回复）"]
        for piece in chunks:
            yield piece

    async def complete(self, messages, **overrides) -> str:
        out = []
        async for piece in self.stream(messages, **overrides):
            out.append(piece)
        return "".join(out)


def parse_sse(text: str) -> list[dict]:
    events = []
    for line in text.splitlines():
        if line.startswith("data:"):
            try:
                events.append(json.loads(line[5:].strip()))
            except json.JSONDecodeError:
                continue
    return events


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    print("P0 HTTP 集成测试\n")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        import os
        os.environ["AION_DB_PATH"] = str(Path(tmp) / "http.db")
        os.environ["AION_HOST"] = "127.0.0.1"
        os.environ.pop("SEREIN_BASE_URL", None)     # 走"记忆未配置"的降级路径
        os.environ.pop("SEREIN_GATEWAY_KEY", None)
        os.environ.pop("MODEL_BASE_URL", None)

        from fastapi.testclient import TestClient

        from app import main as main_mod

        with TestClient(main_mod.app) as client:
            test_health(client)
            test_bootstrap(client)
            fake = FakeModel()
            # 换模型要同时改 pipeline 与 scheduler —— 它们是两个独立引用。
            # 生产代码里这层耦合已收进 AppState，测试这里显式同步。
            main_mod.state.pipeline.model = fake
            main_mod.state.scheduler.model = fake
            test_chat_plain(client, fake)
            test_chat_directive(client, fake, main_mod)
            test_capability_toggle(client)
            test_wake_endpoints(client, main_mod)
            test_websocket(client, main_mod)

    print()
    if _failures:
        print(f"失败 {len(_failures)} 项：{_failures}")
        return 1
    print("P0 HTTP 集成测试全部通过。")
    return 0


def test_health(client) -> None:
    print("[1] 健康检查")
    resp = client.get("/healthz")
    check("healthz 200", resp.status_code == 200)
    body = resp.json()
    check("service 标识正确", body.get("service") == "aion-server")
    check("Serein 未配置被识别", body.get("serein_configured") is False)


def test_bootstrap(client) -> None:
    print("\n[2] bootstrap 快照")
    body = client.get("/api/bootstrap").json()
    slugs = [a["slug"] for a in body["actors"]]
    check("角色含 harlan/user", "harlan" in slugs and "user" in slugs, str(slugs))
    check("connor 默认关闭",
          any(a["slug"] == "connor" and a["enabled"] == 0 for a in body["actors"]))
    caps = {c["key"]: c["enabled"] for c in body["capabilities"]}
    check("next_chat 默认启用", caps.get("next_chat") is True)
    check("toy 默认关闭（骨架已备未实现）", caps.get("toy") is False, str(caps))
    check("显示名可读", any(a["display_name"] == "Harlan" for a in body["actors"]))


def test_chat_plain(client, fake: FakeModel) -> None:
    print("\n[3] 普通一轮对话")
    fake.push(["我在", "的。"])
    resp = client.post("/api/chat", json={"message": "在吗", "conv_id": "c1"})
    check("chat 返回 200", resp.status_code == 200)
    check("content-type 是 SSE", "text/event-stream" in resp.headers.get("content-type", ""))

    events = parse_sse(resp.text)
    kinds = [e["type"] for e in events]
    check("有 recall 事件（降级）", "recall" in kinds, str(kinds))
    recall_evt = next(e for e in events if e["type"] == "recall")
    check("记忆未配置时降级不报错", recall_evt["data"]["ok"] is False)
    check("有 stream_start", "stream_start" in kinds)
    check("有 stream_delta", kinds.count("stream_delta") == 2, str(kinds))
    check("有 stream_end", "stream_end" in kinds)
    check("有 done", "done" in kinds)

    end = next(e for e in events if e["type"] == "stream_end")
    check("可见文本拼接正确", end["data"]["text"] == "我在的。", end["data"]["text"])
    check("无指令时不续轮", end["data"]["rounds"] == 0)

    # 落库检查
    rows = client.get("/api/conversations/c1/messages").json()["messages"]
    check("消息已落库 2 条", len(rows) == 2, f"实际 {len(rows)}")
    check("用户消息在库", rows[0]["role"] == "user" and rows[0]["content"] == "在吗")
    check("助手消息在库", rows[1]["role"] == "assistant" and rows[1]["content"] == "我在的。")
    check("助手消息 sender 是 slug", rows[1]["sender"] == "harlan")
    check("attachments 字段存在", rows[1]["attachments"] == [])

    # 提示词检查：人设 + 能力都在 system 里
    system = fake.calls[-1][0]["content"]
    check("提示词含显示名", "Harlan" in system)
    check("提示词含启用的能力", "[NEXT_CHAT:x]" in system)
    check("提示词不含未启用的能力", "[TOY:" not in system)


def test_chat_directive(client, fake: FakeModel, main_mod) -> None:
    print("\n[4] 指令：排程类（不续轮）")
    # 第一轮吐指令，脚本里还备了第二句 —— 但它**不该被消费**
    fake.push(["那我十分钟后再找你。[NEXT_CHAT:10]"])
    fake.push(["这一句不应该被用到。"])
    before_calls = len(fake.calls)
    resp = client.post("/api/chat", json={"message": "我先忙会儿", "conv_id": "c1"})
    events = parse_sse(resp.text)
    kinds = [e["type"] for e in events]

    check("有 capability 事件", "capability" in kinds, str(kinds))
    cap = next(e for e in events if e["type"] == "capability")
    check("能力事件内容正确", cap["data"] == {"type": "next_chat", "minutes": 10}, str(cap["data"]))
    check("排程类指令不续轮（stream_start 只 1 次）", kinds.count("stream_start") == 1, str(kinds))
    check("只调用了 1 次模型", len(fake.calls) - before_calls == 1)
    check("rounds 为 0（未续轮）",
          next(e for e in events if e["type"] == "stream_end")["data"]["rounds"] == 0)

    end = next(e for e in events if e["type"] == "stream_end")
    check("指令不在可见文本里", "[NEXT_CHAT" not in end["data"]["text"], end["data"]["text"])
    check("可见文本是干净的", end["data"]["text"] == "那我十分钟后再找你。", end["data"]["text"])

    import sqlite3
    conn = sqlite3.connect(str(main_mod.state.db.path))
    conn.row_factory = sqlite3.Row
    sched = [dict(r) for r in conn.execute("SELECT * FROM schedules").fetchall()]
    conn.close()
    check("唤醒项已排入总线", len(sched) == 1, str(sched))
    check("唤醒项 origin 正确", sched and sched[0]["origin"] == "harlan")
    check("唤醒项 payload 正确",
          sched and json.loads(sched[0]["payload_json"]).get("minutes") == 10)

    # 用户发下一条消息应清掉待触发计时器（v0.2 §3.2 冷却规则）
    fake.push(["嗯。"])
    client.post("/api/chat", json={"message": "我回来了", "conv_id": "c1"})
    conn = sqlite3.connect(str(main_mod.state.db.path))
    conn.row_factory = sqlite3.Row
    after = [dict(r) for r in conn.execute("SELECT status FROM schedules").fetchall()]
    conn.close()
    check("用户发言后计时器被取消",
          all(r["status"] == "cancelled" for r in after), str(after))

    print("\n[4b] 指令：回灌类（必须续轮）")
    fake.scripts.clear()          # 清掉上一节残留的脚本，避免吃掉别人的回复

    # 注册一个会产生 followup 的能力，验证"指令 → 执行 → 回灌 → 续轮"真的能跑
    async def fake_lookup(args, ctx):
        from app.core.directives import DirectiveOutcome
        return DirectiveOutcome(followup=f"查到的资料：{args} 的答案是 42。",
                                event={"type": "demo_lookup", "query": args})

    main_mod.state.pipeline.registry.register("demo_lookup", r"\[DEMO:([^\]\n]+)\]", fake_lookup)
    fake.push(["我查一下。[DEMO:生命的意义]"])
    fake.push(["查到了，是 42。"])
    before_calls = len(fake.calls)
    resp = client.post("/api/chat", json={"message": "帮我查个东西", "conv_id": "c4"})
    events = parse_sse(resp.text)
    kinds = [e["type"] for e in events]

    check("回灌类指令触发续轮（stream_start 2 次）", kinds.count("stream_start") == 2, str(kinds))
    check("调用了 2 次模型", len(fake.calls) - before_calls == 2,
          f"实际 {len(fake.calls) - before_calls}")
    check("rounds 记为 1",
          [e for e in events if e["type"] == "stream_end"][-1]["data"]["rounds"] == 1)

    ends = [e for e in events if e["type"] == "stream_end"]
    check("两轮的可见文本都被保留",
          "我查一下。" in ends[-1]["data"]["text"] and "是 42" in ends[-1]["data"]["text"],
          ends[-1]["data"]["text"])
    check("指令不出现在可见文本", "[DEMO" not in ends[-1]["data"]["text"])

    # 第二次调用模型的上下文里应带上执行结果
    second_call = fake.calls[-1]
    joined = "\n".join(m["content"] for m in second_call)
    check("续轮上下文含执行结果", "42" in joined and "指令" in joined, joined[-160:])
    check("续轮上下文含首次回复（避免重复说）", "我查一下。" in joined)

    # 续轮上限：模型反复吐回灌指令时不能无限循环
    fake.scripts.clear()
    main_mod.state.pipeline.settings.max_directive_rounds = 2
    for _ in range(6):
        fake.push(["[DEMO:循环]"])
    fake.push(["收尾。"])
    resp = client.post("/api/chat", json={"message": "死循环测试", "conv_id": "c5"})
    ev = parse_sse(resp.text)
    rounds = [e for e in ev if e["type"] == "stream_end"][-1]["data"]["rounds"]
    check("续轮受上限约束", rounds <= 2, f"rounds={rounds}")
    check("超限时报了非致命错误",
          any(e["type"] == "error" and not e["data"].get("fatal") for e in ev),
          str([e for e in ev if e["type"] == "error"]))
    main_mod.state.pipeline.settings.max_directive_rounds = 3
    fake.scripts.clear()


def test_capability_toggle(client) -> None:
    print("\n[5] 能力开关（数据驱动）")
    resp = client.patch("/api/capabilities/toy", json={"enabled": True})
    check("开启 toy 成功", resp.status_code == 200 and resp.json()["enabled"] is True)
    caps = {c["key"]: c["enabled"] for c in client.get("/api/bootstrap").json()["capabilities"]}
    check("toy 已启用", caps["toy"] is True)

    client.patch("/api/capabilities/toy", json={"enabled": False})
    caps = {c["key"]: c["enabled"] for c in client.get("/api/bootstrap").json()["capabilities"]}
    check("toy 可关闭", caps["toy"] is False)
    check("不存在的能力返回 404",
          client.patch("/api/capabilities/nope", json={"enabled": True}).status_code == 404)


def test_wake_endpoints(client, main_mod) -> None:
    print("\n[5b] 唤醒端点（P3）")

    wakes = client.get("/api/wakes").json()
    check("wakes 返回 pending/recent", "pending" in wakes and "recent" in wakes)
    check("报告调度器状态", "scheduler_running" in wakes, str(wakes.get("scheduler_running")))

    # 手动触发一次主动开口
    main_mod.state.pipeline.model.push(["刚看到窗外下雨了，想起你说过喜欢雨。"])
    resp = client.post("/api/wake", json={"actor": "harlan", "kind": "proactive"})
    check("触发返回 200", resp.status_code == 200, resp.text[:150])
    body = resp.json()
    check("确实开口了", body.get("spoke") is True, str(body))
    check("动作正确", body.get("action") == "private_chat", str(body))

    # 消息应落在唤醒会话里
    rows = client.get("/api/conversations/harlan/messages").json()["messages"]
    check("主动消息已落库", len(rows) >= 1, f"实际 {len(rows)}")
    if rows:
        check("sender 是 harlan", rows[-1]["sender"] == "harlan")
        check("meta 标注为主动", (rows[-1].get("meta") or {}).get("wake", {}).get("type") == "proactive",
              str(rows[-1].get("meta")))

    # rest 应当不产生消息
    before = len(client.get("/api/conversations/harlan/messages").json()["messages"])
    main_mod.state.pipeline.model.push(["[NEXT_CHAT:NONE]"])
    resp = client.post("/api/wake", json={"actor": "harlan", "kind": "idle"})
    after = len(client.get("/api/conversations/harlan/messages").json()["messages"])
    check("rest 不产生消息", after == before, f"{before} → {after}")

    # 闹铃带内容
    main_mod.state.pipeline.model.push(["到点了，该吃药了。"])
    resp = client.post("/api/wake",
                       json={"actor": "harlan", "kind": "alarm", "content": "吃药"})
    check("闹铃触发成功", resp.json().get("spoke") is True, resp.text[:150])

    # 参数校验
    check("未知类型返回 400",
          client.post("/api/wake", json={"kind": "nope"}).status_code == 400)

    # 手动触发后应排了下一次
    wakes = client.get("/api/wakes").json()
    check("已排下一次唤醒", len(wakes["pending"]) >= 1, str(wakes["pending"]))


def test_websocket(client, main_mod) -> None:
    print("\n[6] WebSocket 多端同步")

    def drain(ws, wanted: str | None = None, limit: int = 25) -> list[dict]:
        """读取直到出现 wanted（或读够 limit 条）。"""
        got: list[dict] = []
        for _ in range(limit):
            try:
                msg = ws.receive_json()
            except Exception:
                break
            got.append(msg)
            if wanted and msg.get("type") == wanted:
                break
        return got

    with client.websocket_connect("/ws?client_id=phone") as phone:
        hello = phone.receive_json()
        check("收到 hello", hello["type"] == "hello")
        check("client_id 回显", hello["data"]["client_id"] == "phone")

        with client.websocket_connect("/ws?client_id=pc") as pc:
            pc.receive_json()                                  # pc 的 hello
            events = drain(phone, wanted="clients_changed", limit=10)
            changed = [e for e in events if e["type"] == "clients_changed"]
            check("第二端接入有广播", bool(changed), str([e["type"] for e in events]))
            check("在线数正确", changed and changed[-1]["data"]["clients"] == 2,
                  str(changed[-1] if changed else None))

            # 从 HTTP 发消息，phone 应通过 WS 收到 message 广播
            main_mod.state.pipeline.model.push(["广播测试。"])
            client.post("/api/chat", json={"message": "广播一下", "conv_id": "c2"})
            got = drain(phone, wanted="message", limit=25)
            types = [e["type"] for e in got]
            check("另一端口收到 message 广播", "message" in types, str(types))
            msg = next((e for e in got if e["type"] == "message"), None)
            check("广播带显示名", msg and msg["data"].get("display_name") == "Harlan",
                  str(msg["data"].get("display_name") if msg else None))
            check("广播含流式事件", "stream_start" in types and "stream_end" in types, str(types))

            # 心跳
            phone.send_json({"type": "ping"})
            pong = [e for e in drain(phone, wanted="pong", limit=10) if e["type"] == "pong"]
            check("心跳有响应", bool(pong))

    # 全部断开后在线数应归零
    check("断开后在线数归零", main_mod.manager.client_count == 0,
          str(main_mod.manager.client_count))


if __name__ == "__main__":
    raise SystemExit(main())
