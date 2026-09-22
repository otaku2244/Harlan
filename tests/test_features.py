#!/usr/bin/env python3
"""P4 功能接口测试 —— 日程 / 朋友圈 / 召回日志 / 设置 / 诊断。

不联网、不需要 VPS。日记接口需要 Serein，单独在 --online 时测。

运行：py tests/test_features.py
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


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    print("P4 功能接口测试\n")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        import os
        os.environ["AION_SKIP_ENV"] = "1"
        os.environ["AION_DB_PATH"] = str(Path(tmp) / "feat.db")
        os.environ["SCHEDULER_ENABLED"] = "0"      # 避免后台任务干扰
        for key in ("SEREIN_BASE_URL", "SEREIN_GATEWAY_KEY", "MODEL_BASE_URL",
                    "MODEL_NAME", "MODEL_API_KEY"):
            os.environ.pop(key, None)

        from fastapi.testclient import TestClient

        from app import main as main_mod

        with TestClient(main_mod.app) as client:
            test_schedules(client)
            test_moments(client)
            test_recalls(client, main_mod)
            test_settings(client)
            test_diagnostics(client)

    print()
    if _failures:
        print(f"失败 {len(_failures)} 项：{_failures}")
        return 1
    print("P4 功能接口测试全部通过。")
    return 0


def test_schedules(client) -> None:
    print("[1] 日程 / 闹铃（复用唤醒总线的 schedules 表）")
    body = client.get("/api/schedules").json()
    check("列表可读", "schedules" in body and body["count"] == 0, str(body["count"]))

    # 按相对时间创建
    resp = client.post("/api/schedules", json={
        "type": "reminder", "delay_minutes": 30, "content": "喝水"})
    check("创建成功", resp.status_code == 200, resp.text[:120])
    item = resp.json()
    check("返回 id", bool(item.get("id")))
    check("in_seconds 约为 1800", 1700 < item["in_seconds"] < 1900, str(item["in_seconds"]))
    check("origin 自动填了角色", item["origin"] == "harlan", item["origin"])

    body = client.get("/api/schedules").json()
    check("列表里有 1 条", body["count"] == 1, str(body["count"]))
    check("content 透传", body["schedules"][0]["content"] == "喝水")

    # 绝对时间
    resp = client.post("/api/schedules", json={
        "type": "alarm", "trigger_at": 4102444800.0, "content": "很久以后"})
    check("绝对时间创建成功", resp.status_code == 200)

    # 取消
    resp = client.delete(f"/api/schedules/{item['id']}")
    check("取消成功", resp.status_code == 200 and resp.json()["status"] == "cancelled")
    check("重复取消返回 409",
          client.delete(f"/api/schedules/{item['id']}").status_code == 409)
    check("不存在的日程返回 404",
          client.delete("/api/schedules/nope").status_code == 404)

    body = client.get("/api/schedules").json()
    check("取消后列表只剩 1 条", body["count"] == 1, str(body["count"]))
    body = client.get("/api/schedules?include_done=true").json()
    check("include_done 能看到已取消的", body["count"] == 2, str(body["count"]))

    # 参数校验
    check("未知类型返回 400",
          client.post("/api/schedules", json={"type": "nope"}).status_code == 400)
    check("缺时间返回 400",
          client.post("/api/schedules", json={"type": "alarm"}).status_code == 400)


def test_moments(client) -> None:
    print("\n[2] 朋友圈")
    body = client.get("/api/moments").json()
    check("初始为空", body["moments"] == [])

    resp = client.post("/api/moments", json={"content": "今天天气不错"})
    check("发布成功", resp.status_code == 200, resp.text[:120])
    moment = resp.json()
    check("返回 id", bool(moment.get("id")))
    check("带显示名", moment.get("display_name") == "你", str(moment.get("display_name")))
    check("初始 0 赞", moment["likes"] == 0)

    # AI 也能发
    resp = client.post("/api/moments", json={"content": "我也觉得", "author": "harlan"})
    check("AI 可发布", resp.status_code == 200)
    check("AI 显示名正确", resp.json()["display_name"] == "Harlan",
          resp.json().get("display_name"))

    body = client.get("/api/moments").json()
    check("列表有 2 条", len(body["moments"]) == 2, str(len(body["moments"])))
    check("按时间倒序", body["moments"][0]["content"] == "我也觉得",
          body["moments"][0]["content"])

    # 回复
    mid = moment["id"]
    resp = client.post(f"/api/moments/{mid}/replies", json={"content": "是啊", "author": "harlan"})
    check("回复成功", resp.status_code == 200)
    check("回复内容正确", resp.json()["reply"]["content"] == "是啊")

    body = client.get("/api/moments").json()
    target = next(m for m in body["moments"] if m["id"] == mid)
    check("回复已落库", len(target["replies"]) == 1, str(len(target["replies"])))
    check("回复带作者", target["replies"][0]["author"] == "harlan")

    # 点赞
    resp = client.post(f"/api/moments/{mid}/like")
    check("点赞成功", resp.status_code == 200 and resp.json()["likes"] == 1)
    client.post(f"/api/moments/{mid}/like")
    body = client.get("/api/moments").json()
    target = next(m for m in body["moments"] if m["id"] == mid)
    check("点赞累加", target["likes"] == 2, str(target["likes"]))
    client.post(f"/api/moments/{mid}/like?delta=-1")
    body = client.get("/api/moments").json()
    target = next(m for m in body["moments"] if m["id"] == mid)
    check("可取消点赞", target["likes"] == 1, str(target["likes"]))

    # 删除
    check("删除成功", client.delete(f"/api/moments/{mid}").status_code == 200)
    check("删除后 404", client.delete(f"/api/moments/{mid}").status_code == 404)
    check("不存在的动态回复返回 404",
          client.post("/api/moments/nope/replies", json={"content": "x"}).status_code == 404)
    check("空内容被拒",
          client.post("/api/moments", json={"content": ""}).status_code == 422)


def test_recalls(client, main_mod) -> None:
    print("\n[3] 记忆召回日志")
    db_path = main_mod.state.db.path
    import asyncio
    import sqlite3

    # 直接写日志（模拟一次成功 + 一次失败）
    async def seed() -> None:
        await main_mod.state.db.log_recall(
            window_id="w1", actor="harlan", query="读书会", ok=True,
            ids=["scene:a", "scene:b"], context="上次约好周三", source="chat",
            conv_id="c1")
        await main_mod.state.db.log_recall(
            window_id="w1", actor="harlan", query="无关话题", ok=True,
            ids=[], context="", source="chat", conv_id="c1")
        await main_mod.state.db.log_recall(
            window_id="w2", actor="harlan", query="断线时", ok=False,
            ids=[], error="连不上 Serein", source="wake", conv_id="")

    asyncio.run(seed())

    body = client.get("/api/recalls").json()
    check("读到 3 条", body["count"] == 3, str(body["count"]))
    check("ids 已解析成数组", isinstance(body["recalls"][0]["ids"], list))
    check("按时间倒序", body["recalls"][0]["query"] == "断线时",
          body["recalls"][0]["query"])

    check("可按 source 过滤",
          client.get("/api/recalls?source=wake").json()["count"] == 1)
    check("可按会话过滤",
          client.get("/api/recalls?conv_id=c1").json()["count"] == 2)

    stats = client.get("/api/recalls/stats").json()
    check("总数正确", stats["total"] == 3, str(stats["total"]))
    check("成功数正确", stats["ok"] == 2, str(stats["ok"]))
    check("命中数正确（有卡片的）", stats["hit"] == 1, str(stats["hit"]))
    check("失败数正确", stats["failed"] == 1, str(stats["failed"]))
    check("卡片总数正确", stats["total_cards"] == 2, str(stats["total_cards"]))
    check("命中率计算正确", abs(stats["hit_rate"] - 0.333) < 0.01, str(stats["hit_rate"]))


def test_settings(client) -> None:
    print("\n[4] 设置读写")
    body = client.get("/api/settings").json()
    for section in ("actors", "capabilities", "model", "serein", "scheduler", "runtime"):
        check(f"含 {section} 段", section in body)

    check("★ 不泄漏模型密钥", "api_key" not in json.dumps(body.get("model", {})),
          json.dumps(body.get("model", {}))[:80])
    check("★ 不泄漏 Serein 密钥", "gateway_key" not in json.dumps(body.get("serein", {})))
    check("密钥以掩码形式给出",
          body["serein"]["key_masked"] == "(未设置)", body["serein"]["key_masked"])
    check("角色显示名可读", any(a["display_name"] == "Harlan" for a in body["actors"]))
    check("能力清单可读", len(body["capabilities"]) >= 2, str(len(body["capabilities"])))

    resp = client.put("/api/settings", json={"idle_min_minutes": 45, "history_limit": 60})
    check("更新成功", resp.status_code == 200, resp.text[:120])
    applied = resp.json()["applied"]
    check("idle_min_minutes 已应用", applied.get("idle_min_minutes") == 45, str(applied))
    check("history_limit 已应用", applied.get("history_limit") == 60)

    body = client.get("/api/settings").json()
    check("改动反映在读接口", body["scheduler"]["idle_min_minutes"] == 45,
          str(body["scheduler"]["idle_min_minutes"]))

    # 非法值被夹住
    client.put("/api/settings", json={"idle_min_minutes": -5})
    body = client.get("/api/settings").json()
    check("负数被夹到 1", body["scheduler"]["idle_min_minutes"] == 1,
          str(body["scheduler"]["idle_min_minutes"]))

    # 不允许通过接口写密钥
    resp = client.put("/api/settings", json={"model_api_key": "hack"})
    check("接口不接受密钥字段（被忽略）", resp.status_code == 200 and
          "model_api_key" not in resp.json()["applied"], resp.text[:120])


def test_diagnostics(client) -> None:
    print("\n[5] 诊断")
    resp = client.get("/api/diagnostics")
    check("可访问", resp.status_code == 200, resp.text[:120])
    body = resp.json()
    for key in ("db", "serein_hook", "serein_mcp", "model", "scheduler", "recall_stats"):
        check(f"含 {key}", key in body)
    check("数据库连通", body["db"]["ok"] is True)
    check("Serein 未配置时如实报告", body["serein_hook"]["ok"] is False)
    check("模型未配置时如实报告", body["model"]["ok"] is False)
    check("含召回统计", "hit_rate" in body["recall_stats"])


if __name__ == "__main__":
    raise SystemExit(main())
