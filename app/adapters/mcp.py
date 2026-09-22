"""Serein MCP 客户端 —— 手动实现，不引入 mcp SDK。

为什么不用官方 SDK：
    我们只需要 initialize / tools/list / tools/call 三个方法，
    而 Streamable HTTP 的握手很简单（实测可行）。多加一个依赖不值当，
    尤其这个项目要部署到 VPS，依赖越少越稳。

为什么需要它：
    Serein 的 `/api/*` 用网页登录态，Gateway Key 会被 401。
    但 `/serein/mcp` 接受静态 Bearer Key —— 读日记、读记忆都得走这条。

协议要点（实测）：
    POST /serein/mcp  initialize            → 响应头带 Mcp-Session-Id
    后续请求带 Mcp-Session-Id 头
    Accept 必须同时含 application/json 和 text/event-stream
    响应可能是 SSE 包装（`data: {...}`），也可能直接是 JSON
"""

from __future__ import annotations

import json
import re

import httpx

from app.config import Settings, settings as default_settings

# 工具的返回是**文本**，不是 JSON。形状例如：
#     [diary_list]
#     count: 20
#     <空行>
#     kind: diary
#     status: active
#     id: diary:32
#     title: xxx
#     date: 2026-08-29
#     author: ai
#     body:
#     （正文多行）
#     comments: 0
#     bound_sources: 1
# 所以需要一个"按条目切分"的解析器。
_FIELD = re.compile(r"^([a-z_]+):\s?(.*)$")


class McpError(RuntimeError):
    pass


class SereinMcp:
    """Serein 的 MCP 客户端。所有方法都不抛异常，失败时返回带 error 的结果。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or default_settings
        self.enabled = bool(self.settings.serein_base_url and self.settings.serein_gateway_key)
        self._client: httpx.AsyncClient | None = None
        self._session: str | None = None
        self._initialized = False

    @property
    def url(self) -> str:
        return f"{self.settings.serein_base_url}/serein/mcp"

    async def start(self) -> None:
        if self._client is None:
            # trust_env=False：Serein 在 Tailscale 内网，不该走系统代理。
            # 本机连公网地址时也遇到过 httpx 默认配置 ReadTimeout（见 spike 的踩坑）。
            self._client = httpx.AsyncClient(timeout=60.0, trust_env=False)

    async def close(self) -> None:
        if self._client is not None:
            client, self._client = self._client, None
            await client.aclose()
        self._session = None
        self._initialized = False

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.settings.serein_gateway_key}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session:
            headers["Mcp-Session-Id"] = self._session
        return headers

    async def _rpc(self, method: str, params: dict | None = None,
                   notify: bool = False, rid: int = 1) -> dict | None:
        if self._client is None:
            await self.start()
        payload: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if not notify:
            payload["id"] = rid
        try:
            resp = await self._client.post(self.url, headers=self._headers(), json=payload)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise McpError(f"MCP HTTP {exc.response.status_code}: "
                           f"{exc.response.text[:200]}") from exc
        except httpx.RequestError as exc:
            raise McpError(f"连不上 Serein MCP: {exc}") from exc

        sid = resp.headers.get("Mcp-Session-Id") or resp.headers.get("mcp-session-id")
        if sid:
            self._session = sid
        body = resp.text
        if not body.strip():
            return None
        # 响应可能是 SSE 包装
        for line in body.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                line = line[5:].strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
        raise McpError(f"MCP 返回无法解析：{body[:200]}")

    async def ensure_initialized(self) -> None:
        if self._initialized:
            return
        await self._rpc("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "harlan-backend", "version": "0.1"},
        }, rid=1)
        await self._rpc("notifications/initialized", {}, notify=True)
        self._initialized = True

    async def list_tools(self) -> list[dict]:
        await self.ensure_initialized()
        result = await self._rpc("tools/list", {}, rid=2)
        return ((result or {}).get("result") or {}).get("tools") or []

    async def call(self, name: str, arguments: dict | None = None) -> str:
        """调用工具，返回文本结果。工具自身报错时抛 McpError。"""
        await self.ensure_initialized()
        result = await self._rpc("tools/call",
                                 {"name": name, "arguments": arguments or {}}, rid=3)
        if result is None:
            return ""
        if "_http" in result:
            raise McpError(f"MCP HTTP {result['_http']}")
        if "error" in result:
            raise McpError(str(result["error"])[:300])
        payload = result.get("result") or {}
        blocks = payload.get("content") or []
        text = "\n".join(b.get("text") or "" for b in blocks if isinstance(b, dict))
        if payload.get("isError"):
            raise McpError(text[:300] or "工具执行失败")
        return text

    # ── 日记 ────────────────────────────────────────

    async def read_diary(self, diary_id: int | None = None, date: str | None = None,
                         query: str | None = None, limit: int | None = None,
                         offset: int | None = None) -> dict:
        """读日记。

        * 不传 diary_id → 返回目录（每篇的元信息 + 摘要）
        * 传 diary_id   → 返回单篇全文
        """
        args: dict = {}
        if diary_id is not None:
            args["diary_id"] = int(diary_id)
        if date:
            args["date"] = date
        if query:
            args["query"] = query
        if limit is not None:
            args["limit"] = int(limit)
        if offset is not None:
            args["offset"] = int(offset)

        text = await self.call("read_diary", args)
        return parse_diary(text)

    async def read_memory(self, identifier: str, with_evidence: bool = False) -> dict:
        text = await self.call("read_memory",
                               {"identifier": identifier, "with_evidence": with_evidence})
        return {"raw": text}

    async def source_search(self, query: str, limit: int = 5) -> list[dict]:
        text = await self.call("source_message_search", {"query": query, "limit": limit})
        return parse_records(text)


# ─────────────────────────────────────────────────────────────
# 文本解析：工具的返回是 YAML 风格的键值块，不是 JSON
# ─────────────────────────────────────────────────────────────

def parse_records(text: str) -> list[dict]:
    """把 `key: value` 块切成记录数组。多行值（如 body）归到最后一个键。"""
    records: list[dict] = []
    current: dict | None = None
    last_key: str | None = None

    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.startswith("[") and line.endswith("]"):
            continue                      # 形如 [diary_list] 的分节标题
        if re.match(r"^(count|total):\s*\d+$", line):
            continue                      # 汇总行
        match = _FIELD.match(line)
        if match:
            key, value = match.group(1), match.group(2)
            if key in {"kind", "id"} and current is not None and last_key in {"bound_sources", "body"}:
                # 新记录开始
                records.append(current)
                current = {}
            if current is None:
                current = {}
            current[key] = value.strip()
            last_key = key
        elif current is not None and last_key:
            # 续行（正文）
            current[last_key] = (current[last_key] + "\n" + line).strip()
    if current:
        records.append(current)
    return records


def parse_diary(text: str) -> dict:
    """解析 read_diary 的返回。"""
    records = parse_records(text)
    count_match = re.search(r"count:\s*(\d+)", text or "")
    mode = "list" if (text or "").startswith("[diary_list]") else "entry"

    entries = []
    for item in records:
        diary_id = item.get("id", "")
        numeric = None
        m = re.match(r"diary:(\d+)", diary_id)
        if m:
            numeric = int(m.group(1))
        entries.append({
            "id": diary_id,
            "diary_id": numeric,
            "kind": item.get("kind", "diary"),
            "status": item.get("status", ""),
            "title": item.get("title", ""),
            "date": item.get("date", ""),
            "author": item.get("author", ""),
            "body": item.get("body", ""),
            "comments": _as_int(item.get("comments")),
            "bound_sources": _as_int(item.get("bound_sources")),
        })
    return {
        "mode": mode,
        "count": int(count_match.group(1)) if count_match else len(entries),
        "entries": entries,
        "raw": text,
    }


def _as_int(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None
