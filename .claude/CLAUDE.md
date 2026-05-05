# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository. 

This repository is a personal project for building a life tracking system using Discord and AI. The codebase is developed and maintained by an individual, and is not intended for public use or contribution. 

When working with this codebase, please keep the following in mind:

## ui style
Clean, gentle aesthetic with Morandi-inspired muted tones — soft grays, dusty pinks, sage greens, and warm beiges. Use generous whitespace to let content breathe. Avoid harsh contrasts or saturated colors. Reference `frontend/src/styles/theme.css` for defined colors and fonts.

plan文件夹整理方案：
三类文件的生命周期规则
类型 什么时候 建什么时候动 什么时候死
idea 灵感来了 2分钟写完几乎不改,想补充就直接在下面加段落 升级成 spec 时整体删掉
spec 决定要做这个功能了 只增量修改,绝不另存为v2 被整体推翻时 → 挪去 archive
todo 开工前 每天划掉已完成的功能 做完直接删

```
plans/
├── 00-index.md          # 总览,项目大脑缓存
├── ideas/               # 想法池:随便扔,允许混乱
│   ├── energy-slot.md
│   ├── gamification.md
│   └── ...
├── specs/               # 方案:每个功能当前的"权威版本",只有一份
│   ├── energy-slot.md
│   └── ...
├── todos/               # 执行清单:短命,做完就删
│   └── energy-slot-layer1.md
└── archive/             # 坟墓:被推翻的旧版本
    └── energy-slot-2026-03-15.md
```

已做的功能放在`./devlog.md`里，按照日期倒序记录，方便回顾和总结。
---

## 项目简介

Life Tracker 是一个基于 Discord + AI 的个人生活轨迹记录系统。用户通过 Discord 随意聊天，AI 自动从对话中提取活动信息，生成结构化的时间轴记录，并提供主动提醒、时间管理、以及长期记忆功能。前端为 React + Vite 组件化前端，通过 FastAPI 提供数据。

### 核心架构

```
Discord ↔ Python 进程 (Bot + AI Router + SQLite + FastAPI) ↔ React 前端
```

### 技术栈

- **后端**: Python, discord.py, httpx, FastAPI, SQLite
- **前端**: React + Vite + Tailwind，由 FastAPI 静态挂载在 `/app/`
- **部署**: Docker → 云服务器
- **AI 动态路由**: `config.json` 维护 presets 表，运行时通过 `/model` `/fallback` 斜杠命令切换 active/fallback preset，状态持久化到 `data/active_preset.json`。支持的 provider：
  - Anthropic 原生 API (`claude`)
  - OpenAI 官方 SDK (`openai`)
  - OpenAI 兼容中转站 (`relay`)
  - Google Gemini 原生 API (`gemini`)

### 核心模块

| 文件 | 职责 |
|---|---|
| `main.py` | 入口，asyncio.gather 启动 Bot + Scheduler + FastAPI；支持 `--test`（测试模式）和 `--api-only`（跳过 Bot/Scheduler，仅起 FastAPI 用于本地前端调试） |
| `config.py` | 从 `config.json` 加载配置；维护 AI presets 表（`Preset` dataclass）、运行时 active/fallback 状态读写、时区/天气/日志等 |
| `bot/discord_bot.py` | Discord 收发消息，支持解析用户的引用/回复消息，过滤非目标用户；注册斜杠命令 `/todo` `/weather` `/model` `/fallback` `/tz` |
| `bot/ai_engine.py` | AI 引擎路由器，按 `config.get_active().provider` 加载对应引擎模块；带 fallback preset 自动重试 |
| `bot/ai_engine_base.py` | 四个引擎共享的逻辑：动态上下文构建、消息格式处理、工具执行、chat/scheduled_action 高层流程 |
| `bot/ai_engine_claude.py` | Claude 原生调用引擎（仅实现 `_call_with_tools`，其余委托 base） |
| `bot/ai_engine_openai.py` | OpenAI 官方 SDK 调用引擎（仅实现 `_call_with_tools`，其余委托 base） |
| `bot/ai_engine_relay.py` | OpenAI 格式中转站调用引擎（仅实现 `_call_with_tools`，其余委托 base） |
| `bot/ai_engine_gemini.py` | Gemini 原生 REST API 调用引擎（仅实现 `_call_with_tools`，其余委托 base） |
| `bot/ai_provider_error.py` | 自定义异常类 `AIProviderError`，统一表示 AI 服务商调用失败 |
| `bot/tools.py` | 工具定义 (OpenAI / Anthropic 两种 Schema，共 10 个工具) + `TOOL_POST_HINTS`；category 三分法枚举 Focus/Routine/Chill，log_timeline_event 含 project_name 字段，可选 `status='planned'` 标记未来 dummy event；`cancel_planned_event` 把 planned 标为 cancelled |
| `bot/prompts.py` | Prompt 集中管理：6 个正交 section（IDENTITY / USER_MODEL / SYSTEM_MECHANICS / COMMUNICATION / PROTOCOLS / TOOLS_SECTION，chat/poll 完全共用）、`PromptParts` dataclass（三层缓存结构）、`build_prompt()`；各引擎通过 `to_claude_blocks()` / `flatten()` 消费；`PROACTIVE_PROMPT` / `REMINDER_PROMPT` / `BEDTIME_PROMPT` / `MORNING_PROMPT` / `TOOL_POST_HINTS` 也在此定义 |
| `bot/scheduler.py` | 两个并发循环 + asyncio.Lock：Timer 循环（随机轮询+睡前）+ Reminder 循环（数据库提醒倒计时+Event 唤醒） |
| `bot/database.py` | SQLite DB 操作 (events, messages, reminders, memories, todos, deadlines)；events 表含 project_name 字段，可空 `status` 列：NULL=已发生，`planned`=未来 dummy，`cancelled`=已取消的 planned |
| `bot/merge.py` | 事件合并模块，将相邻同 content+category 的事件合并为时间段 |
| `bot/weather.py` | 天气查询模块，使用 tomorrow.io API（需 `weather.api_key`，免费档 500 calls/day），早上时段注入天气数据 |
| `bot/timezone_state.py` | 进程时区管理：通过 `os.environ['TZ']` + `time.tzset()` 控制本地时间；启动时从 `data/active_tz.json` 读取，运行时通过 `/tz` 切换并持久化 |
| `bot/test_mode.py` | 测试模式：`python main.py --test` 启动后捕获所有日志和 AI prompt payload 写入 JSONL |
| `bot/logger.py` | 集中日志配置，其他模块 `get_logger(__name__)` 统一获取，支持 RotatingFileHandler |
| `api/server.py` | FastAPI 接口：`/api/timeline`(合并后), `/api/events`, `/api/categories`, `/api/memories`, `/api/reminders`, `/api/todos`, `/api/deadlines`, `/api/projects/heatmap` |
| `frontend/` | React + Vite + Tailwind 组件化前端：日视图编辑式 Dashboard (`Dashboard`，内嵌 `MultiLaneTimeline`)、周视图 (`WeekView`)、Project Overview (`ProjectOverview`)、通用列表 (`ItemList`) |
| `scripts/` | 辅助脚本：`cleanup.py`（数据清理）、`dev.sh` / `dev_pull.sh`（本地 api-only 调试 + 从 R2 拉取生产 DB）、`extract_dispatch_samples.py` / `parse_dispatch_labels.py`（dispatch POC 标注工具） |

### 消息进程与数据流

系统中的消息触发和流转路径主要分为以下几种场景。所有通过 AI 处理的路径都可能经历多轮 Tool Calling 机制，每一轮的文字都会发给用户、直到模型停止调用工具为止。

#### 1. 用户主动发消息
- **触发点**: 用户在 Discord 发送任意常规文字（或引用回复）。
- **使用 Prompt**: 全量 `SYSTEM_PROMPT`（包括人设、各种响应规则、记忆调度指引等）。
- **处理模型**: 当前 active preset 的 model（chat / poll 共用同一个 preset，由 `/model` 切换）。
- **流转路径**: Discord `on_message` → `fetch_history(limit=20)` 拉取历史 → `ai_engine.chat` 注入动态上下文（记忆、正在进行的事件、待触发提醒等） → AI 按需调用 tool 并在任意轮次输出文字回复（每一轮文字都会实时 send 给用户）→ 模型停止调工具后结束。

#### 2. 斜杠指令 (Slash Commands)
- **触发点**: 用户发送 `/todo`、`/weather` 等指令消息。
- **流转路径**: Discord `on_message` 检测指令前缀 → 路由至相应 Command 处理器 → 获取外部数据或本地 DB 操作 → 生成结论并发送回 Discord。

#### 3. 随机轮询 (Proactive Check)
- **触发点**: 定时器 `Timer` 循环以 1~60 分钟随机间隔内存倒计时唤醒。
- **使用 Prompt**: `PROACTIVE_PROMPT`（要求 AI 在"聊几句"、"关心"、"提记忆"、"保持沉默：[SILENT]"内四选一）。
- **流转路径**: `_timer_loop` 唤醒 → `_do_proactive_check()` → `scheduled_action(allow_silent=True)` → 若输出包含 `[SILENT]` 则阻断发送；否则送达用户。

#### 4. 提醒调度 / 睡前提醒
- **触发点**: 数据库提醒倒计时结束（`Reminder` 循环），或精确的夜间硬性睡前触发段（`Timer` 循环）。
- **使用 Prompt**: `REMINDER_PROMPT` / `BEDTIME_PROMPT`。
- **流转路径**: 循环唤醒 → 提取具体 Action / Context 调用 `scheduled_action()` → AI 根据业务可能自调工具做去重 → 生成纯文本结果发出提醒消息。

### 多轮 Tool Calling 机制

一次 chat/scheduled_action 可能跨多个 AI 轮次，直到模型停止 tool_use、只输出纯文本为止。

**每一轮的文字都会原样 send 给用户**——支持"边说边调工具"（例如先回一句"好，五分钟看着呢"，同一轮再 set_reminder）。不想说话时就只调工具、不输出文字。模型若要完全沉默由 scheduler 的 `[SILENT]` 机制处理，不依赖"中间轮独白"这种隐式约定。

维护点：`bot/prompts.py::TOOL_ROUND_REMINDER`、四个引擎的每轮文字发送逻辑。

### 工具后置提示（TOOL_POST_HINTS）

`bot/tools.py::TOOL_POST_HINTS` 为特定工具定义"定向后置提示"，在每轮 tool_result 拼回模型时附加，精准投递工具后决策规则。

当前命中的工具：
- `list_reminders` → 决策辅助：查完清单后若要 set_reminder，先比对 group_id / 时间窗
- `set_reminder` → 去重自检：set 后如担心重复可调 `list_reminders` 看 pending，发现重复立即 `delete_reminder` 精准删

helper: `build_tool_round_hint(called_names)` 在四个引擎里统一调用。

### Prompt 模块化

所有 prompt 集中在 `bot/prompts.py`，静态系统指令由 6 个正交 section 拼成，**chat / poll 完全共用**（0 模式差异，最大化 cache 命中）：

| 段名 | 内容 |
|---|---|
| `IDENTITY` | 【日和】人设 + "关于她的现象"元指令（读用户观察时去标签化） |
| `USER_MODEL` | 基础信息 + Hybrid 去标签化用户画像（概念挂载 + 负向语气约束） |
| `SYSTEM_MECHANICS` | 多轮 tool calling 说明（每轮文字 = 给她看的）、时间戳格式、换行分条、斜杠命令输出识别 |
| `COMMUNICATION` | 调性、基本反应模式、对话示范、节奏——所有"怎么说话"规则的唯一出处 |
| `PROTOCOLS` | 4 个去临床化状态信号（深度专注/迈不出第一步/高耗宕机/时间感偏移），每个内部按主动/被动动作分叉 |
| `TOOLS_SECTION` | 工具调用纪律 + Why/When 策略（格式细节如 ISO 8601、category 枚举、project_name 前缀在 `bot/tools.py` 的 JSON Schema 里） |

**模式差异的唯一通道**：scheduler 模板（`PROACTIVE_PROMPT` / `REMINDER_PROMPT` / `BEDTIME_PROMPT` / `MORNING_PROMPT`）在 user message 里带 `[内部触发…]` / `[约定跟进触发…]` 等前缀，AI 据此识别当前是主动轮询还是被动回复。system prompt 不再带 mode-specific section。

`PromptParts` dataclass 按变化频率分四层，对应 Anthropic `cache_control` 的 4 个上限：

- **Block 1（静态）**：IDENTITY + USER_MODEL + SYSTEM_MECHANICS + COMMUNICATION + PROTOCOLS + TOOLS_SECTION（几乎不变，~5444 字符）
- **Block 2（稳定上下文）**：deadlines + projects（低频变化：projects 几乎不增删，deadline 仅在新增/完成时变）
- **Block 3（记忆）**：memories（独立成 block，避免记忆更新连带 invalidate Block 2 的 cache）
- **Block 4（高频动态）**：ongoing + pending_reminders + deadlines + planned_events + weather

各引擎消费方式：
- Claude: `prompt.to_claude_blocks()` → 最多 4 个 cached system block（chat ↔ poll 切换 100% cache hit）
- OpenAI / Relay / Gemini: `prompt.flatten()` → 单个字符串
- 中间轮省 token: `prompt.concise().flatten()`（去掉 `TOOLS_SECTION`）

`_build_prompt()` 动态注入内容（见 `ai_engine_base._build_prompt`）：
- 【你现在记着的事】— 从 `memories` 表取全部（Block 3）
- 【现有项目列表】— 从 events 表聚合 Focus 类 project_name（Block 2）
- 【当前进行中的事件】— `end_time IS NULL` 且 `status IS NULL` 的真实活动（Block 4）
- 【待完成的 Deadline】— 过滤 active 状态，带倒计时（Block 4）
- 【未来安排（planned events）】— events 表 `status='planned'` 过滤，按 start_time 升序，带倒计时（Block 4）
- 【待触发的 Reminder】— `db.list_active_reminders()` 取所有 status='pending' 的，按 trigger_time 升序，带倒计时（Block 4）
- 【今日天气】— 早上时段调 `bot/weather.py::get_weather_brief()`（Block 4）

**为什么 pending reminders 注入 Block 4**：聊天历史里"几小时前她说要 xxx"这种触发消息会反复出现在 fetch_history(20) 窗口内，AI 看不到队列状态就容易再 set 一次同款。注入到 Block 4 让 AI 一眼看到自己已设的 follow-up，避免重复 set；也是"主动 follow-up（默认开启）"策略的去重兜底。代价是 Block 4 cache 失效频率上升一点（每次 set/cancel/触发都失效），但 Block 4 本来就是最易变层，影响有限。

**不注入**：cancelled planned events。已取消的只给前端看，AI 不需要反复感知。

### 调度 Prompt 模板

均定义在 `bot/prompts.py`：
- `PROACTIVE_PROMPT` — 随机轮询：给 AI 四选一（聊几句 / 关心 / 提一嘴记忆 / [SILENT]）
- `REMINDER_PROMPT` — 提醒触发：注入 action + 优先级 + group 进度，硬规定"禁止再 set 相同内容"
- `BEDTIME_PROMPT` — 睡前提醒：22:30-00:00 随机两次

## Linear connection
本项目的 issue 跟踪在 Linear 上
每次get issue时，使用两个步骤：
1. Linear:get_issue(id="LIN-123")          # 拿 issue 主体
2. Linear:list_comments(issueId="LIN-123") # 再拿评论列表
评论列表里面是需求变更记录，和一些讨论，甚至有时会有新的需求冒出来，比description更活跃，所以需要单独拿，并且评估和implement。

如果推送改动的comment，不要说技术细节，用自然语言描述改动的scope、内容和原因，方便非技术人员理解。