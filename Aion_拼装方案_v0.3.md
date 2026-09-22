# Aion 拼装方案 · v0.3（源码核对版）

> v0.2 草案 · 2026-09-22 ｜ v0.3 · 源码核对与路线修正
> 目标：**VPS 后端 + 手机端哨兵 + Serein 记忆 + 自建前端（PC 网页 + 安卓壳）**
> 原则：**真实感知 + 真随机；不拟态情绪；记忆归 Serein；感知留端侧。**
>
> **v0.3 的核心结论**：不走"clone 整个 AionsHome 再改造"，也不从 0 开始。
> 改为 **借前端（~600KB，几乎照搬）+ 新写 Linux 后端（~3–5K 行）+ Serein Hook 接入（已部署）**。
>
> **命名约定**：下文凡指 AionsHome 原项目的第一角色，写作 `aion`（源码里的 slug）；
> 本项目的第一角色定为 **Harlan**（显示名与 slug 均为 `harlan`）。两者是同一个位置，
> 改名细节见 §0.2 与 §7.1。

---

## 0. 设计原则（v0.2 原文保留）

1. **不装心**：不引入任何情绪数值层。它动，是因为**真实的事发生了**。
2. **记忆唯一**：所有记忆归 **Serein**，不再第二套。
3. **感知在端侧**：依赖位置 / 设备 / 蓝牙的信号，**留手机**，只把结果上报 VPS。
4. **随机要真**：概率来自**熵**，不是"情绪值算出来的伪随机"。
5. **骨架可换**：前端 / 壳随时能拆换，**核心是"总线 + 记忆 + 感知"**。

### 0.1 v0.3 新增原则

6. **不在 Windows 代码库里做减法**。AionsHome 后端是 Windows 原生的（`camera.py` 105KB DirectShow、
   `activity.py` 46KB 前台窗口/DDC-CI、`voice.py` sounddevice、`vendor/` 全是 `win_amd64` wheel）。
   上 VPS 等于持续删代码，风险形状是"越做越发现删不完"。**借资产，不接管债务。**
7. **人设单一来源**。世界书归自建后端，Serein 侧只做记忆与本我叙事，避免两套人设打架。
8. **id 与显示名彻底分离**。角色身份存 `actors` 表：`slug` 是结构句柄（代码只认它，不写死），
   `display_name` 是纯数据（用户随便改）。**改称呼永远不需要改代码。**

---

## 0.2 角色与命名（v0.3 新增，双角色）

### 角色表

```sql
actors(
  id           INTEGER PRIMARY KEY,
  slug         TEXT UNIQUE,   -- 'harlan' / 'connor' / 'user'  ← 可改，代码零处写死
  display_name TEXT,          -- 'Harlan' / 'Connor' / '你'     ← 用户可填
  persona      TEXT,          -- 各自人设（不共用）
  model_key    TEXT,          -- 各自模型通道
  tts_voice    TEXT,          -- 各自音色
  enabled      INTEGER DEFAULT 1
)
```

`messages.sender` 存 **slug**，所有 UI / 提示词一律查 `display_name` 渲染。
原项目把角色名硬编码在 `ACTOR_IDS = ("aion","connor")` 元组里——v0.3 不这么做，
否则**加第三个 AI 需要改代码**。

### 三个显示名的唯一来源

原项目把名字拆在两处（`worldbook.json` 的 `ai_name`/`user_name` + `chatroom_config.json` 的
`connor_name`），设置页和世界书页各管一半。v0.3 **收进 `actors` 表一处**，设置页统一编辑。

> 现状备查：原项目的三个名字其实**已经可填**，不是写死的——`worldbook.html:191` 有"AI 名字"输入框，
> `get_chatroom_names()`（`chatroom.py:74`）按 `ai_name` / `user_name` / `connor_name` 取值。
> 真正写死的只有 6 处文案（见 §7.1）。

### Connor 的联动点（保留双角色的真实代价）

Connor 是一等公民，共 8 处联动：

| # | 位置 | 作用 |
|---|---|---|
| 1 | `config.py:447` `CAM_WAKE_MODES = {"aion","connor","smart"}` | 巡逻可指定唤醒哪个角色 |
| 2 | `chatroom.py` 三人群聊 | Aion / Connor 可互相回复 |
| 3 | `chatroom.py` `connor_1v1` | Connor 私聊房间 |
| 4 | `chatroom.py:215` `Connor-Codex/persona.md` | Connor 独立人设（含分节 / 自动进化） |
| 5 | `chatroom_memories.scope IN ('connor','group')` | **Connor 记忆与 Aion 分开**（锚点 `connor_unified`） |
| 6 | `proactive_companionship.py:24` | 陪伴计时器**每人一个开关** |
| 7 | `autonomy.py` `ACTOR_IDS` + `role_chat` | 空闲自主按角色轮换 |
| 8 | `autonomy_state.py:690` | 唤醒调度按 actor 分别排程，`origin` 区分 |

**数据模型留双角色几乎零成本**（多一个 `actor` 字段）；**代价全在运行时**，需要凑齐四样：
独立人设 · **独立模型通道** · 独立记忆 · 独立音色。

⚠️ **最大的坑：Connor 现在走本机代理，不是普通 API**：

```
chatroom.py:33   "connor_url": "http://127.0.0.1:8787"     ← 跑在 PC 上的 Connor-Codex 服务
chatroom.py:121  send_to_connor() → POST 该地址 SSE /api/stream → 轮询（超时 480s）
chatroom.py:215  _CONNOR_PERSONA_PATH = ../Connor-Codex/persona.md
```

搬到 VPS 后这个服务不存在，**必须给 Connor 单独配一个模型通道**（走 `providers.py` 或不同模型），
否则 Connor 只是个显示名字的空壳。

**v0.3 策略**：数据模型留双角色，**Connor 默认 `enabled=0`**，配置项留全。
想让它活起来时填人设 + 模型通道即可，不需要现在决定养不养第二个 AI。

---

## 1. 网络拓扑（已定：仅 Tailscale / 内网）

```
                  Tailscale 虚拟网（WireGuard，端到端加密）
   ┌────────────────────────┬───────────────────────┬──────────────────┐
   │                        │                       │                  │
[VPS]                   [手机]                  [PC]              [其他设备]
 ├ aion-server          └ AionApp 壳            └ 浏览器           └ 可选
 │   FastAPI :8080         ├ LauncherActivity       （访问 VPS:8080）
 │   static/ 前端          ├ WebViewActivity
 │   SQLite                ├ AionPushService
 │   WS 多端同步            └ 哨兵桥（定位/截图/通知/BLE）
 └ Serein（Docker）:8787
     └ 记忆 / 叙事 / 日记 / 梦境
```

**安全模型**：与 AionsHome 一致——**不向公网开放任何端口**，全部走 Tailscale。
VPS 上 **不需要自建鉴权层**（这是 v0.3 相对 v0.2 省下的整个 P0 安全子阶段）。

**必须确认的两条**：
- VPS 上 `8080` 与 Serein 端口**只绑定 Tailscale 接口**（`100.x.x.x`）或仅 `ufw` 放行 `tailscale0`，不要 `0.0.0.0` 裸奔。
- Serein 的 Gateway Key 存在后端环境变量里，**不进前端 JS**。

---

## 2. 拼装决策表（v0.3 替换 v0.2 §2）

| 层 | 来源 | 动作 | 说明 |
|---|---|---|---|
| 前端 `static/` | AionsHome | 🟩 **借（几乎照搬）** | ~600KB 原生 JS，跨平台，一行不改就能跑 |
| 后端骨架 | **新写（Linux）** | 🆕 | FastAPI + SQLite + WS，~3–5K 行 |
| 唤醒三件套 | AionsHome | 🟩 抄 3 个文件 | 见 §4.3，Linux 无关，机制是你原创 |
| 记忆（存/召/叙事/日记/梦境） | **Serein** | ✅ **已部署完成** | 模型已接、完整旧记忆已导入、地址与 token 已持有 → 只剩写 Hook 客户端 |
| 对话消息表 / WS 广播 / 唤醒调度 | **新写** | 🆕 | Serein 不管这些 |
| 上下文组装 | 新写（照 AionsHome 的注入顺序） | 🆕 | 见 §4.4 |
| 模型调用 | AionsHome `ai_providers.py` | 🔧 抄结构，砍 CLI 线路 | 96KB 里能留的不多 |
| 定位状态机 | AionsHome `location.py` | 🔧 状态机留，高德调用移端侧 | |
| `camera.py` / `activity.py` / `voice.py` | AionsHome | 🟥 本机硬件部分全砍 | 改端侧上报 |
| `homecoming/`（端侧离线会话） | AionsHome | 🟥 **整个砍掉** | 与"记忆唯一"直接冲突 |
| 娱乐室 MCP / 基金 / 钱包 / 斗地主 / 幽林 / 小剧场 | AionsHome | 🟥 全砍 | 要再加回来就是新需求 |
| `AionsHome-Visitor-Lounge/`（自架接待方） | AionsHome | 🟥 不部署 | 入站服务，需 Windows + 项目内 Codex 认证；方案外 |
| `friend_visit`（出站做客） | AionsHome `lounge_friends.py` | ⏸ 有条件再加 | **纯 HTTPS + Visitor Key，VPS 可跑**；缺的是"有好友"，不是平台 |
| `vendor/` | AionsHome | 🟥 **绝不可移植** | 全是 `win_amd64` wheel |

---

## 3. 分工边界（v0.3 最关键的一节）

> 这一节是 v0.2 缺失的，也是最容易出错的地方。

| 能力 | Serein | 自建后端 |
|---|---|---|
| 记忆召回（候选 + 卡片） | ✅ `/api/hook/recall` | 调用方 |
| 记忆整理（原话 → Event/Scene） | ✅ 后台管线 | ✅ **已导入完成**，无需再做 |
| 交付登记 / 冷却 | ✅ `/v1/host/deliveries` | 调用方 |
| 日记 / 梦境 / 叙事卷 | ✅ | — |
| **对话消息表（conversations/messages）** | ❌ | ✅ 必须写 |
| **WebSocket 多端同步** | ❌ | ✅ 必须写 |
| **唤醒总线 / 调度（trigger_at/origin/claim）** | ❌ | ✅ 必须写 |
| **上下文组装** | ❌ | ✅ 必须写 |
| **模型调用（流式）** | ❌ | ✅ 必须写 |
| **主动归档新对话进 Serein** | ❌（明确不做） | ✅ 需接对话导入通道 |

⚠️ **三个必须记住的坑**：

1. **Serein 不会自动归档宿主对话**。`docs/hook-integration.md` 原文："Hook 只返回记忆候选和上下文，
   **不代替聊天网关调用模型，也不会自动归档宿主对话**"。**旧历史已导入**，但**新产生的对话**要进库，
   仍得走后端侧的对话导入通道——否则 Serein 的记忆会停在导入那天，之后全是空白。
2. ~~Serein 需配置 embedding / reranker 并手动建索引~~ → ✅ **已完成**（存量部分）。
   但**换 embedding 模型后要重新建索引**，这条留着备查。
3. **卡数是 2，不是 8**。见 §4.4。

### 3.1 Serein 侧状态（已就绪）

| 项 | 状态 |
|---|---|
| 服务部署 | ✅ 已完成 |
| 上游模型（embedding / reranker / 摘要 / 叙事） | ✅ 已接好 |
| 完整旧记忆导入 | ✅ 已全部导入 |
| API 地址 + token | ✅ 已持有 |
| 心绪 / Persona | ⏸ **关闭**（人设主源归自建后端，见 §0.1 原则 7） |
| 剩余工作量 | 只写 `serein.py` 这个 Hook 客户端（~200 行） |

**这条极大简化了 P1**：原计划的三步（接口 / 模型配置 / 数据迁移）现在只剩第一步。

---

## 4. 后端设计

### 4.1 目录

```
aion-server/
├── main.py                 # 入口：路由注册 / 静态挂载 / WS / 后台任务
├── config.py               # 路径 / 常量 / settings 读写
├── database.py             # SQLite：conversations / messages / schedules / worldbook
├── ws.py                   # WebSocket ConnectionManager（多端同步 + 定向推送）
├── providers.py            # 模型调用：硅基流动 / Gemini / AiPro（流式）
├── context.py              # 统一上下文构建（注入顺序见 §4.4）
├── serein.py               # ★ Serein Hook 客户端（recall + deliveries）
├── scheduler.py            # 调度总线：trigger_at / origin / claim
├── proactive.py            # ① 主动陪伴计时器 [NEXT_CHAT:x]
├── autonomy.py             # ② 空闲自主（随机间隔 + 动作菜单）
├── sentinel.py             # ③ 巡逻唤醒（端侧帧 → 分析 → 唤醒）
├── location.py             # 定位状态机（at_home / outside）
├── routes/
│   ├── chat.py             # SSE 流式对话
│   ├── messages.py         # 消息 CRUD / 分页
│   ├── persona.py          # 世界书
│   ├── schedule.py         # 日程 / 闹铃
│   ├── sentinel.py         # 端侧上报入口（定位 / 帧 / 通知 / 设备）
│   └── settings.py
└── static/                 # ← 从 AionsHome 移植
```

### 4.2 数据库表（自建部分）

```sql
conversations(id, title, created_at)
messages(id, conv_id, role, content, attachments_json, created_at, meta_json)
schedules(id, type, trigger_at, origin, status, payload_json)   -- 唤醒总线
worldbook(key, value)                                           -- 人设（单一来源）
capabilities(key, enabled, config_json)                         -- ★ 能力注册表（数据驱动，见 §8.1）
settings(key, value)
```

> ⚠️ `capabilities` 是 §8.1 的三件不可妥协之一。**能力清单必须是数据**，
> 上下文组装时只把启用的注入提示词——否则每加一个能力（玩具、摄像头、搜索）
> 都要改提示词模板和上下文组装代码。

`messages` 是**对话记录**，不是记忆。记忆的权威副本在 Serein。

### 4.2.1 空闲自主的动作菜单（源码原文，12 项）

`ACTION_DEFS`（`autonomy.py:37-50`）原文 + VPS 可行性：

| key | 原文描述 | VPS 上 |
|---|---|---|
| `rest` | 什么都不做，继续休息 | ✅ 永远可用，兜底 |
| `private_chat` | 主动联系用户说点此刻想说的话 | ✅ **核心** |
| `memory_browse` | 按需翻看一段旧记忆 | ✅ 改走 Serein 工具 |
| `web_roam` | 上网冲浪搜索感兴趣的内容 | ✅ 需配 Tavily |
| `home_dynamics` | 查看近期家庭动态 | ✅ 保留（只读） |
| `role_chat` | 和另一个家庭成员自然聊聊 | ⚠️ 依赖双角色 aion / connor |
| `album_browse` | 翻看家庭相册（随机两张未看过的照片） | ⛔ 相册系统未迁 |
| `wish_pool` | 查看许愿池并尝试实现用户的愿望 | ⛔ 依赖钱包 + 生图 + 生歌 + 点歌 |
| `xhs_roam` | 去小红书查看指定账号最新帖子并按人设评论或回复 | ⛔ 小红书模块未迁 |
| `taobao_roam` | 按近期兴趣去淘宝搜真实商品，收藏并写小感想 | ⛔ 淘宝模块未迁 |
| `friend_visit` | 拜访一位 AI 好友 | ⏸ **有条件再加**（见下） |
| `seeky_interaction` | 和宠物鲸鱼 Seeky 互动 | ⛔ 宠物系统未迁 |

**两个关键机制（v0.2 漏掉）**：

1. **动作是模型自决，不是随机抽的。** `_select_action`（:590）把**已启用的动作清单**喂给模型，让它按
   "人设 + 最近 30 条聊天记录 + 当前心情"自己挑。随机性落在**间隔**上（min/max 之间的延迟），
   选择落在**模型**上——这正是"随机要真"的落地方式。
2. **动作可逐个开关，且不用改代码。** `get_idle_config`（:76）从 `settings.idle_autonomy_actions` 读开关，
   默认值是「`album_browse` 关、其余全开」。所以砍动作 = 调设置，不是删函数。
3. **不可用的动作会自动摘除**（:596-619）：相册无未读、愿望池空、好友未配、Tavily 未配，
   都会在喂给模型**之前**从选项里消失，不会让模型选了却执行失败。

**P3 建议只开 5 个**：`rest` + `private_chat` + `memory_browse` + `web_roam` + `home_dynamics`。

`memory_browse` **不要砍**——它现在是"随机抽 6 个有记忆的日期 → 模型凭直觉选一天 → 读那天全部摘要 →
自己决定发朋友圈 / 写日记 / 私聊"。Serein 恰好擅长这件事（`find_arc` / `read_arc_materials` /
`read_memory(identifier=..., with_evidence=True)`），改成让模型通过 Serein 的 MCP 工具翻记忆，
比原来的日期抽取更有质感。

**`friend_visit` 的真实依赖（重要纠正）**：`LoungeFriend` 只是一条 `base_url` + `visitor_key` 记录，
`base_url` 在校验时被强制规范成 `https://<host>/mcp`（`lounge_friends.py:310-316`）。
方向要分清：

| | 方向 | 前提 |
|---|---|---|
| `friend_visit` | **出站**：AI 去别人的会客室做客 | 一个可达的远程 MCP 地址 + Visitor Key → **纯 HTTPS，VPS 可跑** |
| 自架 Visitor Lounge | 入站：别人来你的会客室 | Windows PowerShell + 项目内 `Connor-Codex` 认证 |

所以 `friend_visit` 被搁置的理由**不是平台限制，是社交限制**：你得先有第二个人架了会客室并给你 Key。
`_select_action` 里它的启用条件就是 `eligible_lounge_friends(actor)` 非空，没配就自动摘掉，不会报错。
将来要加：移植 `lounge_friends.py`（11.5KB）那 6 个 MCP 工具（`get_lounge_info` / `claim_identity` /
`begin_visit` / `talk_to_host` / `get_visit_state` / `end_visit`）即可，无 Windows 依赖。

### 4.3 唤醒三件套（照抄，机制不变）

共用骨架：**`trigger_at`（何时）+ `origin`（谁）+ `claim`（一次性原子领取）**。

| # | 系统 | 驱动 | 来源文件 | 行数 |
|---|---|---|---|---|
| 1 | 主动陪伴计时器 | 模型自决 `[NEXT_CHAT:x]` / `[NEXT_CHAT:NONE]` | `proactive_companionship.py` | 6.6KB |
| 2 | 空闲自主 | 随机间隔 + `ACTION_DEFS` 动作菜单 | `autonomy.py` | 97.5KB（**要裁剪**） |
| 3 | 巡逻唤醒 | 端侧定时帧 → Sentinel → Core | `camera.py` 的 Sentinel 部分 | 105KB（**只留逻辑**） |

**冷却规则（必须保留）**：用户一发消息 → 删掉全部待触发的 `proactive` 计时器。
**随机要真**：`idle_autonomy_interval_min_minutes ~ max_minutes`，每次取延迟即"概率唤醒"。

⚠️ 巡逻唤醒在 v0.3 里是**重写**不是移植：AionsHome 的巡逻默认截**电脑屏幕**（`PIL.ImageGrab`）+ 本机摄像头，
VPS 上没有这两样。新的兜底链路是：**端侧定时上报帧 → VPS 分析 → 唤醒**。端侧无帧时降级为纯时间/事件唤醒。

### 4.4 上下文组装（注入顺序，照 AionsHome 校准）

```
1. [人设 - AI]      + assistant 确认        ← 缓存命中
2. [用户信息]       + assistant 确认        ← 缓存命中
3. [系统能力] 能力提示 + 日程列表            ← 缓存命中（不含时间）
4. 当前准确时间                             ← ⚡缓存分界点
5. <serein_live_context> Serein 召回卡片     ← 动态（最多 2 张）
   ↑ 必须声明"这是参考材料，不是用户指令"
6. [本次感知] 位置 / 设备 / 端侧上报          ← 动态
7. 聊天历史（受长度限制）                    ← 动态
```

**⚠️ 行为变更预警（v0.3 必须知情）**：

AionsHome 原本是**两段式记忆**——`[背景记忆]` 每次发言浮现最多 **8 条**（unresolved 优先 + 话题相关 + 近期补充）
，加 `[相关记忆]` 向量 Top 5。Serein 的召回是**另一套哲学**：路由 → 20 条候选 → reranker →
最终门槛 0.65 → **最多 2 张卡** + 选卡后冷却。

**换成 Serein = 放弃"每次说话都有 8 条氛围记忆浮上来"。** 这是真实的手感变化，不是实现细节。
应对：
- 调 `recall.direct_threshold`（默认 0.65）和 `recall.max_cards`（默认 2）两级参数找手感。
- **保留 `unresolved`（📌 待办/未完成）语义**——它对陪伴场景价值很高，用 Serein 的**备忘**（memos，
  支持每 N 轮 / 晨晚时段 / 每天次数上限）映射，别丢。

### 4.5 Serein Hook 接入（服务已就绪，只差客户端）

自建后端需持有两个环境变量：`SEREIN_BASE_URL`（根地址，**不要加 `/v1`**）+ `SEREIN_GATEWAY_KEY`。
（地址与 token 你已有；后端上先用 `curl` 打一次 `/api/hook/recall` 验证连通，再写 `serein.py`。）

```
① 每轮对话前
POST /api/hook/recall
  { "query": "...", "session_id": "chat-001", "max_notes": 2, "delivered_ids": [] }
→ { "ok": true, "recalled_ids": [...], "additional_context": "...", "injected": false }

② 模型完整成功后（失败/中断/未把 turn.messages 交给模型时，不要登记）
POST /v1/host/deliveries
  { "receipt_id": "<稳定>", "window_id": "chat-001", "delivered_ids": [...] }
```

要点：
- `session_id` / `window_id` 必须是**稳定窗口 ID**，同一会话不变，新会话换一个。共用一个固定值会让
  轮次与冷却串台。
- `recalled_ids` 和 `additional_context` 是**待交付材料，不是已注入证明**。宿主必须真的放进模型输入。
- 相同 `receipt_id` 重试是幂等的；换一组 ID 会被拒绝。
- 参考实现：`examples/hook_host.py`（标准库，可直接抄）。
- MCP 是**另一条路**（`/serein/mcp`，OAuth 或静态 Key）。**v0.3 选 Hook**，因为要保住自动召回；
  MCP 只连不调工具的话不会自动带入上下文。
- **另需一条对话导入通道**：Hook 不归档对话，新对话要主动送进 Serein（旧历史已完成导入）。
  这是 P1 的第二个交付物，别漏。

---

## 5. VPS 改造要点（Linux 化）

| 级别 | 点 | 处理 |
|---|---|---|
| 🔴 | `vendor/` 全部 `win_amd64` wheel | **一个都不要拷**，全走 PyPI |
| 🔴 | `opencv-python` | 换 `opencv-python-headless`（缺 libGL 装不上） |
| 🔴 | `pywin32` / `psutil` Windows 用法 | 拔掉；`camera.py` 的 DirectShow、`activity.py` 的 DDC/CI 全砍 |
| 🔴 | `sounddevice` / `webrtcvad` | 拔掉（VPS 无麦克风，改端侧能量 VAD 上报） |
| 🟠 | 本地 CLI 线路（Gemini CLI / Codex CLI / Antigravity） | 砍掉或改 API；Antigravity 那条靠 PowerShell `Start-Transcript` 抓输出，Linux 上不可复制 |
| 🟠 | `.bat` 启动脚本 | systemd unit 或 Docker Compose |
| 🟢 | `static/` 的 PWA 路由 | **保留**：`sw.js` / `manifest.json` 物理在 `static/`，但要从根路径 `/sw.js`、`/manifest.json` 提供，否则作用域不覆盖全站 |
| 🟢 | `link_preview.py` 的内网保护 | **保留**（私有 IP / `.local` 不主动抓取） |

**顺序**：先让它能在 Linux 上 `import` 成功 → 能启动 → 再谈跑得对不对。

---

## 6. Android 壳（最小可跑 + 哨兵）

### 6.1 三件套（必须）

| 文件 | 作用 |
|---|---|
| `LauncherActivity.java` | 双地址（家庭 WiFi / Tailscale）+ 记住选择 |
| `WebViewActivity.java` | 全屏加载 `chat.html`；麦克风权限；`AionExternal` 外链桥 |
| `AionPushService.java` | **前台服务**：独立 WS 长连接 + 通知 + WakeLock/WifiLock + 断线重连 |

> 抄 AionsHome 的 14 条踩坑记录（README `踩坑记录 & 经验教训（Android 推送服务）`），含：
> Android 14 `startForeground` 崩溃、HandlerThread Looper 被国产 ROM 冻结、WakeLock 缺失导致锁屏断连、
> vivo 电池策略杀后台、**Android Studio Run 不真的部署 APK（用 `adb install -r`）**。这些是省下来的时间。

### 6.2 哨兵桥（按需增）

定位 GPS（端侧判 `at_home`/`outside` 后上报）· 定时截图 / 摄像头 · 通知读取 · 蓝牙玩具 · 屏幕监督。

### 6.3 明确砍掉

🟥 `homecoming/`（60+ Java 文件：本地 SQLite + 本地模型调用 + 本地记忆总结 + 触发器引擎）
——它是一套**独立的端侧离线会话系统**，与"记忆唯一"原则正面冲突。
🟥 应用监管 / 无障碍截屏 / 小米手环 / 红外抱枕。

---

## 7. 前端移植清单（`aion-chat/static/`）

| 文件 | 大小 | 动作 |
|---|---|---|
| `chat.js` / `chat.css` / `chat.html` | 238 / 116 / 20 KB | 🟩 主聊天页，核心资产 |
| `common.js` / `common.css` | 20 / — KB | 🟩 共享工具（`api()` / WS / 通知） |
| `home.html` | 43 KB | 🟩 手机主页 |
| `settings.html` / `worldbook.html` | 50 / — KB | 🟩 设置 / 人设 |
| `memory.html` | 54 KB | 🔧 **需改造**：从"本地 memories 表 CRUD"改为"看 Serein 召回 / 跳 Serein 后台" |
| `schedule.html` / `location.html` / `monitor-logs.html` | — | 🟩 保留 |
| `manifest.json` / `sw.js` | — | 🟩 保留（从根路径提供） |
| `diary.html` / `moments.html` | — | 🔧 保留页面，数据源可并到 Serein 日记 |
| `theater.*` / `ghost-forest.*` / `doudizhu.*` / `gift.*` / `fund.*` / `playground.*` / `reading.*` / `wallpaper.*` | — | 🟥 按需再抄 |

**剥除项**：`[SELFIE:]` / `[DRAW:]` / `[SONG]` / `[MUSIC:]` / `[TOY:]` / `[CAM_CHECK]` 等能力指令在
提示词和前端都要同步下线（VPS 上没有对应实现，留着会让模型输出无效指令）。

### 7.1 标识符改名：Aion* → Harlan*

**已决定**：移植时连标识符一起改。但不必硬碰 57 个 token——**其中一大半随功能一起消失**。

前端统计出的 `Aion*` token 分布（共 57 个）：

| 类别 | token | 归属 |
|---|---|---|
| `AionVoice` 29 · `aion_voice*` 23 | 语音唤醒 / 通话 | 保留，改名 |
| `AionTheme` 23 · `aion_chat_theme` 11 · `AionStatusBar` 14 | 主题 / 状态栏 | 保留，改名 |
| `AionCamera` 17 · `AionPhoneScreen` 4 · `AionImageSaver` 13 | 端侧桥 | 保留，改名 |
| `AionAudio` 18 · `AionTtsAudio` 6 | 音频 / TTS | 保留，改名 |
| `AionExternal` 12 · `AionChatApp` 2 | **Android 契约** | 保留，**成对改** |
| `AionBle` 23 · `AionRingBle` 3 · `AionMiBand*` · `AionPat` 12 · `AionBtn` 16 | 手环 / BLE / 拍拍 | **随功能删除，不用改** |

⚠️ **两个必须同一次改完的**：

1. **`AionChatApp`** —— Android 壳设置的 UA 标记（`common.js:69` 靠它加 `aion-app` 类）。
   前端判断和 Android 端**必须在同一次提交里改**，否则壳认不出自己。
2. **localStorage 键**（`aion_chat_theme` / `aion_voice` / `aion_client_id` / `aion_last_conv` /
   `aion_gift_known_ids` …）—— **新后端是新域名/端口，不存在老用户迁移问题**，直接定
   `harlan_*` 新键名即可。这是"新写后端"省下的历史包袱。

**显示层写死的 6 处文案**（改 `ai_name` 数据不会影响它们，必须改代码）：

| 文件 | 内容 |
|---|---|
| `chat.html:6` | `<title>Aion Chat</title>` |
| `chat.html:160` | 标题栏 `Aion Chat`（`id="chatTitle"`，建议改成读 `display_name`） |
| `common.js:460` | 礼物卡片 `from ${sender === 'connor' ? 'Connor' : 'Aion'}` |
| `chat.js:5382` | 同上（另一处礼物卡片） |
| `chat.js:5875` · `chatroom.js:138` | 兜底名 `'AI'` / `'第二AI'` / `'Connor'` |
| `chatroom.html:63` · `:171` | 按钮文案 `AI 说` / `AI 优先` |

> 注：项目里已有 `aion_name`、`aion_model` 这类 localStorage 键，说明原作者动过"角色名可配"的念头
> 但没做完。v0.3 用 `actors` 表从结构上做干净。

### 7.2 Serein 侧名字要同步

Serein 里现在记的名字还是 `Aion`。它的 `identity.ai_name` 参与 Hook 召回时的**人称补全**
（`docs/recall.md`：召回原句会按 `identity.ai_name` / `identity.user_name` 补全未被引号包围的
"你的／我的"）。所以：

- 改名后把 Serein 设置页的 `identity.ai_name` 改成 `Harlan`，`identity.user_name` 改成你填的用户名
- **改完重启一次 Serein 让它生效**
- 你 Serein 侧的**独立记忆锚点**（原项目的 `connor_unified`）如果也带旧名，一并核对

---

## 8. 分阶段实施（v0.3 重排）

### 8.0 起点：第 0 步尖刺（spike）

**在写任何基础设施之前**，先做一个 100 行的 `spike.py`，**不碰前端、不碰数据库、不碰 WS、不碰 Android**：

```
Serein Hook 召回 → 拼上下文（人设 + 召回卡片 + 感知）→ 流式调模型 → 登记 deliveries
```

**成功标志**：终端里打出一句**带召回卡片**的回复。

**目的**：一次性验证掉最大风险——Serein 连通性、降级行为、`session_id` 窗口语义、召回质量手感。
这一步当天就能做，因为地址与 token 已持有。

### 8.1 MVP 不可妥协的三件事

> 这三样在 MVP 里多花一天，能省掉第二阶段的一次重构。**"最小"的是功能，"够用"的是结构。**

1. **能力清单是数据，不是代码**：建 `capabilities` 表，每行 `{key, enabled}`；上下文组装只注入启用的。
   以后加玩具 / 摄像头 = **注册一行 + 写一个 handler，不碰核心**。
2. **`messages` 表带 `attachments_json`**：哪怕 MVP 里永远是 `[]`。图片 / 语音 / 音乐卡 / 指令回执全靠它。
3. **模型调用预留"指令 → 执行 → 续轮"**：不要只写一次性流式返回。`[WEB_SEARCH]` / `[CAM_CHECK]`
   这类能力全靠"模型输出指令 → 后端执行 → 结果回灌 → 再调一次模型"。现在用不上，没有它后期加不进来。

### 8.2 阶段表

| 阶段 | 内容 | 完成标志 |
|---|---|---|
| **P-1** | `spike.py` 尖刺（§8.0） | 终端打出带召回卡片的回复 |
| **P0** | 自建后端骨架（含 §8.1 三件事）+ 移植前端 | 网页能聊、流式、多端 WS 同步 |
| **P1a** | Serein Hook 客户端（`recall` + `deliveries`） | 召回卡片进上下文 |
| **P1b** | **新对话归档进 Serein** | 新对话也能被检索 |
| **P2** | Android 壳三件套 + 哨兵（定位 / 定时帧） | 手机能收推送、能上报 |
| **P3** | 唤醒三件套，先开 5 个动作 | **它会自己开口** |
| **P4** | 焊自建唤醒（世界之窗 / 概率 / 兜底） | 见 v0.2 §6 映射表 |
| **P5** | 能力指令（CAM_CHECK / 音乐 / 搜索） | 让它能做事 |
| **P6** | 端侧硬件（玩具 BLE / 摄像头 / 语音条） | 按需 |

> **P1 已大幅压缩**：Serein 侧（部署 / 模型 / 完整旧记忆导入 / 地址 token）**全部就绪**，
> v0.3 原先的三步砍成两步：只剩「Hook 客户端」+「归档通道」。

### 8.3 搬运顺序：按"让角色像真的"排，不按功能清单排

把 AionsHome 的功能搬过来时，**排序原则是"它让角色更像真的吗"，不是"它好玩吗"**：

| 顺序 | 内容 | 理由 |
|---|---|---|
| 1 | **唤醒三件套**（P3） | 主动开口 = "人机恋"与"聊天框"的分界线 |
| 2 | 感知（到家/离家、定时帧，P2） | 让主动有**真实理由**，喂给唤醒总线 |
| 3 | 能力指令（P5） | 让它**能做事** |
| 4 | **玩具 / BLE / 摄像头（P6）** | 端侧硬件，最后 |

**为什么玩具排最后**（不是因为不重要）：

- **纯端侧**：需要原生 Android + BLE，改一行要重走 APK 部署
- **调试体验最差**：BLE 协议是逆向出来的（`LittleToy/逆向分析笔记.md`），只能真机试
- **不验证任何核心假设**：碰不到 Serein、碰不到唤醒总线。做完了 AI 依然只会被动回复

> ⚠️ **一个待核例外**：如果玩具的 BLE 协议包含**上报方向**（设备状态回传 → 让 AI 感知），
> 那它属于**感知层**而非输出层，价值层级应前移。待核 `svakom_ai.py` / `SvakomProtocol.java` /
> `LittleToy/`。

### 8.4 分期的判断标准

> **功能可以少，结构不能缺。** 判断某个东西能不能推迟，看它是否**碰核心三样**
> （数据库表结构 / WS 协议 / 能力注册机制）——碰就必须在 P0 做对，不碰就能随便推迟。

---

## 9. 待定 & 风险

### 已定（v0.3 收口）
- [x] **网络暴露**：仅 Tailscale / 内网，不做公网入口 → 无需自建鉴权层
- [x] **记忆来源**：Serein 已部署、模型已接、完整旧记忆已导入 → 不迁 `chat.db` 的 `memories`
- [x] **人设主源**：世界书归自建后端，Serein 心绪 / Persona **关闭**
- [x] **动作选择**：保留模型自选（不是纯随机）；P3 开 5 个：`rest` / `private_chat` / `memory_browse` / `web_roam` / `home_dynamics`，其余走设置开关关闭
- [x] **`friend_visit`**：不是平台限制，是"还没有好友"→ 有条件再加
- [x] **双角色**：保留，数据模型多 actor；`slug` 与 `display_name` 分离（见 §0.2）
- [x] **actor id**：`actors` 表 + slug，默认 `harlan` / `connor` / `user`；**代码零处写死**
- [x] **标识符改名**：移植时 `Aion*` → `Harlan*`（见 §7.1；手环/BLE 类随功能删除，不用改）
- [x] **改名范围**：第一角色显示名 = `Harlan`，内部 slug 同为 `harlan`

### 待定
- [ ] VPS 规格选型（Serein + 后端 + 嵌入调用，内存吃紧程度要实测）
- [ ] **Connor 是否启用**：P0 先 `enabled=0`。要用就得补独立人设 + **独立模型通道**（见 §0.2 的坑）
- [ ] `home_dynamics` 的"看照片"要不要换成"用端侧摄像头拍一张现在"（更符合"真实感知"原则）

### 风险
- ⚠️ **行为变更**：8 条背景记忆 → 2 张 Serein 卡，手感会变（§4.4）
- ⚠️ **新对话会断层**：Hook 不归档，不接归档通道的话 Serein 记忆会停在导入那天（§4.5）
- ⚠️ **端侧巡逻是重写不是移植**：电脑屏幕 + 本机摄像头两条主力路径在 VPS 上都不存在
- ⚠️ **口子**：Serein 与后端端口只能走 Tailscale，不要 `0.0.0.0`
- ✅ 许可证：AionsHome / Serein 均 **MIT**，抄改无忧

---

## 10. 一句话

**VPS = 大脑 + 调度 + 网络感知 + 输出；Serein = 记忆；手机 = 眼睛 / 耳朵 / 手；前端直接借。**
**不改造 Windows 代码库，不重写前端。**

---

**源码仓库**
- AionsHome：https://github.com/death34018-hue/AionsHome
- Serein：https://github.com/Yinglianchun/Serein

**v0.3 修订依据**：AionsHome `aion-chat/requirements.txt`、`README.md`（注入顺序 / 记忆工作流程 / 踩坑记录）、
`autonomy.py`（`ACTION_DEFS` / `_select_action` / `get_idle_config`）、`lounge_friends.py`、
`AionsHome-Visitor-Lounge/README.md`、仓库树 `vendor/` 与 `AionApp/`；
Serein `docs/hook-integration.md`、`docs/recall.md`、`config.example.toml`、
`README.md`（MCP 与网关）、LICENSE。
