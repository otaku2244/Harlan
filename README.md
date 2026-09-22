# Aion / Harlan — 后端

人机恋前端 + 后端。VPS 上跑 FastAPI 后端 + Serein 记忆，手机端做哨兵，前端从 AionsHome 移植。

设计文档见 [`Aion_拼装方案_v0.3.md`](Aion_拼装方案_v0.3.md)。

---

## 当前进度

```
[✅] P-1  spike.py 尖刺
[✅] P1   Serein 真链路验证：召回 → 注入 → 登记 → 冷却（已实测）
[✅] P3   唤醒调度：后台循环 / 原子领取 / 生成广播 / 重排下一次
[  ] P3'  空闲自主的动作实现（memory_browse / web_roam 的具体执行）
[  ] P0'  前端移植（待确认前端来源）
[  ] P1'  新对话归档进 Serein（Operit 已在上游做，待确认是否复用）
[  ] P2   Android 壳 + 哨兵
[  ] P4   自建唤醒（世界之窗 / 概率 / 兜底）
[  ] P5   能力指令（CAM_CHECK / 音乐 / 搜索）
[  ] P6   端侧硬件（玩具 / 摄像头）
```

---

## 跑起来

```bash
pip install httpx fastapi uvicorn

cp .env.example .env      # 填 Serein 与模型
uvicorn app.main:app --host 127.0.0.1 --port 8080
```

| 端点 | 说明 |
|---|---|
| `GET /healthz` | 健康检查（含 Serein / 模型是否已配置） |
| `GET /api/bootstrap` | 前端启动快照（角色 / 能力 / 会话） |
| `GET /api/conversations/{id}/messages` | 消息分页 |
| `POST /api/chat` | 发消息，返回 **SSE** 事件流 |
| `PATCH /api/capabilities/{key}` | 开关能力（等于改提示词里列出什么） |
| `GET /api/wakes` | 唤醒总线现状（待触发 + 最近触发） |
| `POST /api/wake` | **手动让角色醒一次**（调试用，不用等 2 小时） |
| `GET /ws` | WebSocket 多端同步 |

`POST /api/chat` 的事件类型：`recall` `stream_start` `stream_delta`
`capability` `capability_note` `stream_end` `delivery` `done` `error`。

**同一条事件流也会广播到 WebSocket**，所以"单端重放"和"多端同步"是同一份数据。

### 主动开口（P3）

后台调度器每 30 秒轮询唤醒总线，到点就组装上下文、调模型、广播：

```
schedules 表：trigger_at（何时）+ origin（谁）
      ↓ 轮询
claim_due() 原子领取（并发下只有一个能拿到）
      ↓
组装（人设 + 记忆召回 + 本次感知 =「为什么现在开口」）
      ↓
流式生成 → 剥离指令 → 落库 → WS 广播
      ↓
排下一次（模型自决优先，否则随机间隔）
```

四类唤醒：`proactive`（模型上一轮用 `[NEXT_CHAT:x]` 自定）、`idle`（空闲自主）、
`alarm`、`reminder`。

**想立刻看到效果，不用等 2 小时**：

```bash
curl -X POST localhost:8080/api/wake -H 'Content-Type: application/json' \
  -d '{"kind":"proactive"}'

# 带内容的闹铃
curl -X POST localhost:8080/api/wake -H 'Content-Type: application/json' \
  -d '{"kind":"alarm","content":"该吃药了"}'
```

**冷却规则**（照 AionsHome）：用户一发消息，该角色的全部 `proactive` 计时器立即取消，
避免"刚聊完又冒头"。

**空闲自主的动作菜单**（模型自选，可逐个开关）：

| key | 说明 | VPS 上 |
|---|---|---|
| `rest` | 什么都不做，继续休息 | ✅ 兜底 |
| `private_chat` | 主动联系用户说点此刻想说的话 | ✅ 核心 |
| `memory_browse` | 翻看一段旧记忆 | ✅ |
| `web_roam` | 上网看看感兴趣的东西 | ✅ 需联网搜索能力 |
| `home_dynamics` | 查看近期家庭动态 | ✅ |

---

## 部署到 VPS

仓库：https://github.com/otaku2244/Harlan

```bash
# 1. 拉代码（建议放在纯 ASCII 路径下，省掉一整类编码问题）
git clone https://github.com/otaku2244/Harlan.git ~/harlan
cd ~/harlan

# 2. 依赖
python3 -m venv .venv
. .venv/bin/activate
pip install httpx fastapi uvicorn

# 3. 配置
cp .env.example .env
$EDITOR .env      # 填 SEREIN_BASE_URL / SEREIN_GATEWAY_KEY / 模型三项

# 4. ★ 第一步永远是验证真链路，不是起服务
python spike.py "今天有点累"
```

`spike.py` 的五步全过，才说明 Serein 真的通了。**别跳过这一步直接起服务**——
它能在几分钟内告诉你"地址错 / Key 错 / 索引没建"，而起了服务再排查要慢得多。

四套离线测试也可以在 VPS 上跑一遍，确认 Python 版本与依赖没问题：

```bash
python spike.py --self-test
python tests/test_spike_offline.py
python tests/test_core.py
python tests/test_http.py
```

### 跑服务

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8080
```

⚠️ **监听地址按网络方案定**（v0.3 §1 已定：仅 Tailscale / 内网）：

| 场景 | `--host` |
|---|---|
| 只走 Tailscale | VPS 的 Tailscale 地址（`100.x.x.x`） |
| 本机调试 | `127.0.0.1` |
| ⛔ 不要 | `0.0.0.0`（会把无鉴权的接口暴露到公网） |

### 常驻（systemd）

```ini
# /etc/systemd/system/harlan.service
[Unit]
Description=Harlan backend
After=network-online.target

[Service]
User=YOUR_USER
WorkingDirectory=/home/YOUR_USER/harlan
EnvironmentFile=/home/YOUR_USER/harlan/.env
ExecStart=/home/YOUR_USER/harlan/.venv/bin/uvicorn app.main:app \
  --host 100.x.x.x --port 8080
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now harlan
journalctl -u harlan -f
```

---

## 本机开发

### 推送改动

本机已配好 git（`C:\Program Files\Git`）、SSH/HTTPS 凭据存在 Windows 凭据管理器里，
推送不会再要求登录：

```powershell
$env:Path = "C:\Program Files\Git\cmd;" + $env:Path   # 仅当 PATH 未刷新时
git add -A
git commit -m "说明这次改了什么"
git push
```

> 仓库的 `core.autocrlf=input`：**提交时把 CRLF 转成 LF**。
> 这是为 VPS（Linux）准备的，别改成 `true`，否则脚本会带上 `\r` 而在 Linux 上执行失败。

### 备用推送方式（没有 git 时）

`scripts/push_to_github.py` 走 GitHub API 建 commit，不需要 git：

```powershell
$env:GITHUB_TOKEN = "github_pat_..."    # fine-grained，Contents: Read and write
py scripts/push_to_github.py --dry-run  # 先看清单
py scripts/push_to_github.py
```

---

## 测试（全部不需要网络）

```bash
python spike.py --self-test          # 尖刺结构自检，15 项
python tests/test_spike_offline.py   # 尖刺 × 假 Serein，在线契约
python tests/test_core.py            # P0 核心：数据库 / 上下文 / 指令
python tests/test_http.py            # P0 HTTP：SSE / 续轮 / 落库 / WS 广播
```

---

## 文件

| 文件 | 作用 |
|---|---|
| `spike.py` | P-1 尖刺（可独立运行，用于验证真链路） |
| `app/main.py` | FastAPI 应用：路由 + 生命周期 |
| `app/config.py` | 配置（全部来自环境变量） |
| `app/db.py` | SQLite 七张表 + 线程安全封装 |
| `app/ws.py` | WebSocket 多端同步（每连接一个发送任务） |
| `app/core/ids.py` | 可排序 ID |
| `app/core/context.py` | 上下文组装 |
| `app/core/directives.py` | 指令注册表 + 解析/执行 |
| `app/core/pipeline.py` | 对话主流程（含指令续轮） |
| `app/adapters/serein.py` | Serein 记忆适配器 |
| `app/adapters/model.py` | 模型流式调用 |
| `docs/dev-fake-serein.py` | 假 Serein |

### 三条结构底线（P0 就位，**别在后续改动里省掉**）

1. **能力清单是数据** —— `capabilities` 表 + `DirectiveRegistry`。
   表决定"对模型可见吗"，注册表决定"怎么执行"。加能力 = 插一行 + `register()`，**不碰核心**。
2. **`messages.attachments_json`** —— 图片/语音/音乐卡/指令回执全靠它。
3. **指令 → 执行 → 续轮** —— `pipeline.run_turn()`。
   排程类指令（`[NEXT_CHAT:5]`）不续轮；回灌类指令（`[WEB_SEARCH:...]`）会带着结果再调一次模型。

---

## 本机快速验证（不需要 VPS、不需要真 Key）

```bash
# 结构自检：不联网，15 项
python spike.py --self-test

# 在线集成测试：真的走 HTTP，对假 Serein 跑召回 → 组装 → 登记 → 冷却
python tests/test_spike_offline.py
```

两者全绿只说明**代码结构正确**，不代表你的 Serein 可用。真链路必须在 VPS 上验证。

---

## 在 VPS 上跑尖刺（真正的验证）

```bash
cp .env.example .env
vim .env          # 填 SEREIN_BASE_URL / SEREIN_GATEWAY_KEY / 模型三项

python spike.py "今天有点累"
```

期望看到五步：

```
[1/5] 召回（窗口 spike-001）…     ✓ 召回 2 条：['scene:...', 'event:...']
[2/5] 组装上下文…                 ✓ 8 条消息，system 1234 字
[3/5] 流式调模型…                 <流式输出>
[4/5] 登记交付…                   ✓ 已登记 receipt=...
[5/5] 完成
```

**这一步要盯的三件事**：

1. **召回是否真的返回内容** —— 返回空且不报错，说明 Serein 侧索引或模型没配好
2. **`injected` 是否为 false** —— 按 Hook 文档它应是 false（待交付材料 ≠ 已注入）
3. **卡片手感** —— 最多 2 张，和你以前"每次 8 条背景记忆"比一比，够不够

只想看组装结果、不花钱调模型：

```bash
python spike.py --provider echo "随便说点什么"
```

---

## 部署方式（重要）

⚠️ **不要"本机写完一次性搬上 VPS"。** 原因：

| 本机（开发机） | VPS |
|---|---|
| ✅ Python 3.13 | ✅ 唯一能连到 Serein 的地方 |
| ❌ 无 Docker（跑不了 Serein 本地副本） | ✅ Linux（真实部署环境） |
| ❌ 无 Tailscale（连不到 VPS） | ✅ 有 Git |
| ❌ 无 Git | |

所以：**本机写代码 + 离线自检；VPS 跑验证 + 部署。**

推荐流程：

```
本机  py spike.py --self-test && py tests/test_spike_offline.py
      py -m py_compile spike.py                 ← 至少确认能编译
  ↓   （用你选的方式传）
VPS   python spike.py "测试"                    ← 真链路验证
```

代码本身是**平台中立**的（纯 Python、无 Windows 依赖、路径用 `pathlib`），
所以搬运不会有兼容问题——**问题只在于"在哪验证"**。

### 已经踩到的两个环境坑（记下来，别人也会踩）

1. **httpx 默认 `trust_env=True` 会连内网地址超时。** 本机实测：连 `127.0.0.1` 的
   `ReadTimeout`，关掉立刻 200。所以 `SereinHook` 强制 `trust_env=False`——
   Serein 在 Tailscale 内网，本来就不该走 HTTP 代理。模型端点相反（可能在公网），
   所以那里保留 `trust_env`，可用 `MODEL_TRUST_ENV=0` 关掉。
2. **Windows 控制台默认 GBK**，非 ASCII 符号会 `UnicodeEncodeError`。
   `spike.py` 启动时强制 stdout/stderr 走 UTF-8；在 Linux 上无副作用。

---

## 配置项

| 变量 | 说明 |
|---|---|
| `SEREIN_BASE_URL` | Serein 实例根地址，**不要加 `/v1`** |
| `SEREIN_GATEWAY_KEY` | 安装时生成的 Key。留在服务端，不进前端 JS |
| `SEREIN_WINDOW_ID` | **稳定窗口 ID**：同一会话不变，新会话换一个。共用一个固定值会让轮次与冷却串台 |
| `SEREIN_MAX_NOTES` | 单轮最多带几张卡（Serein 上限 2） |
| `MODEL_BASE_URL` / `MODEL_API_KEY` / `MODEL_NAME` | OpenAI 兼容端点 |
| `SPIKE_PROVIDER` | `openai` 真调模型；`echo` 只打印上下文 |
| `AI_DISPLAY_NAME` | 角色显示名，默认 `Harlan` |
| `AI_PERSONA` | 人设正文（正式版归 `actors` 表） |

---

## 下一步（P0）要守的三条结构底线

P-1 验证通过后进入 P0。这三样**在 MVP 里就不能省**，否则第二阶段是重构而不是搬运：

1. **能力清单是数据**（`capabilities` 表）—— 加玩具/摄像头 = 注册一行 + 写 handler，不碰核心
2. **`messages.attachments_json`** —— 哪怕永远是 `[]`。图片/语音/音乐卡/指令回执全靠它
3. **模型调用预留"指令 → 执行 → 续轮"** —— `[WEB_SEARCH]` / `[CAM_CHECK]` 这类能力的基础
