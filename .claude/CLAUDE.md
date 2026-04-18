# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository. 

This repository is a personal project for building a life tracking system using Discord and AI. The codebase is developed and maintained by an individual, and is not intended for public use or contribution. 

When working with this codebase, please keep the following in mind:

## ui style
Clean, gentle aesthetic with Morandi-inspired muted tones — soft grays, dusty pinks, sage greens, and warm beiges. Use generous whitespace to let content breathe. Avoid harsh contrasts or saturated colors. Reference `frontend/src/styles/theme.css` for defined colors and fonts.

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
- **AI 动态路由**: 支持随时通过环境变量切换后端：
  - Anthropic 原生 API (`claude`)
  - OpenAI 兼容中转站 (`relay`)
  - Google Gemini 原生 API (`gemini`)

### 核心模块

| 文件 | 职责 |
|---|---|
| `main.py` | 入口，asyncio.gather 启动 Bot + Scheduler + FastAPI |
| `config.py` | 环境变量加载，分离 `CHAT_MODEL` 和 `POLL_MODEL` 降低成本 |
| `bot/discord_bot.py` | Discord 收发消息，支持解析用户的引用/回复消息，过滤非目标用户 |
| `bot/ai_engine.py` | AI 引擎路由器，根据 `AI_PROVIDER` 分发请求 |
| `bot/ai_engine_base.py` | 三个引擎共享的逻辑：动态上下文构建、消息格式处理、工具执行、chat/scheduled_action 高层流程 |
| `bot/ai_engine_claude.py` | Claude 原生调用引擎（仅实现 `_call_with_tools`，其余委托 base） |
| `bot/ai_engine_relay.py` | OpenAI 格式中转站调用引擎（仅实现 `_call_with_tools`，其余委托 base） |
| `bot/ai_engine_gemini.py` | Gemini 原生 REST API 调用引擎（仅实现 `_call_with_tools`，其余委托 base） |
| `bot/ai_provider_error.py` | 自定义异常类 `AIProviderError`，统一表示 AI 服务商调用失败 |
| `bot/tools.py` | 工具定义 (OpenAI / Anthropic 两种 Schema，共 9 个工具) + `TOOL_POST_HINTS`；category 三分法枚举 Focus/Routine/Chill，log_timeline_event 含 project_name 字段 |
| `bot/prompts.py` | Prompt 集中管理：所有 prompt 段落常量、`PromptParts` dataclass（三层缓存结构）、`build_prompt()`；各引擎通过 `to_claude_blocks()` / `flatten()` 消费；`PROACTIVE_PROMPT` / `REMINDER_PROMPT` / `BEDTIME_PROMPT` / `TOOL_POST_HINTS` 也在此定义 |
| `bot/scheduler.py` | 两个并发循环 + asyncio.Lock：Timer 循环（随机轮询+睡前）+ Reminder 循环（数据库提醒倒计时+Event 唤醒） |
| `bot/database.py` | SQLite DB 操作 (events, messages, reminders, memories, todos, deadlines)；events 表含 project_name 字段 |
| `bot/merge.py` | 事件合并模块，将相邻同 content+category 的事件合并为时间段 |
| `bot/weather.py` | 天气查询模块，使用 wttr.in 免费 API（无需 key），早上时段（6-10点）注入天气数据 |
| `bot/test_mode.py` | 测试模式：`/start-test` 开启后捕获所有日志和 AI prompt payload 写入 JSONL；`/stop-test` 结束并命名文件 |
| `bot/logger.py` | 集中日志配置，其他模块 `get_logger(__name__)` 统一获取，支持 RotatingFileHandler |
| `api/server.py` | FastAPI 接口：`/api/timeline`(合并后), `/api/events`, `/api/categories`, `/api/memories`, `/api/reminders`, `/api/todos`, `/api/deadlines`, `/api/projects/heatmap` |
| `frontend/` | React + Vite + Tailwind 组件化前端：时间轴日视图 (`MultiLaneTimeline`)、周视图 (`WeekView`)、Project Overview (`ProjectOverview`)、甘特图 (`GanttChart`)、时间分布饼图 (`TimeDistribution`)、通用列表 (`ItemList`) |
| `scripts/` | 辅助脚本：`cleanup.py`（数据清理）、`test_api.py`（接口测试） |

### 消息进程与数据流

系统中的消息触发和流转路径主要分为以下几种场景。所有通过 AI 处理的路径最终都会经历多轮 Tool Calling 机制，由中间轮的"内心独白"流转到最终的纯文本回复。

#### 1. 用户主动发消息
- **触发点**: 用户在 Discord 发送任意常规文字（或引用回复）。
- **使用 Prompt**: 全量 `SYSTEM_PROMPT`（包括人设、各种响应规则、记忆调度指引等）。
- **处理模型**: 配置的 `CHAT_MODEL`。
- **流转路径**: Discord `on_message` → `fetch_history(limit=20)` 拉取历史 → `ai_engine.chat` 注入动态上下文（记忆、正在进行的事件、待触发提醒等） → 生成 AI 工具调用或思考 → 触发各类 tool (如 `log_timeline_event` 等) → 中间轮输出内部独白并在 DB 按需写入 → 最终纯文本结果推给用户。

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

### 多轮 Tool Calling 机制（中间轮 vs 最后一轮）

一次 chat/scheduled_action 可能跨多个 AI 轮次，直到模型停止 tool_use、只输出纯文本为止。

- **中间轮**（本轮还有 tool_use）：模型输出的文字是**内心独白**，仅记在日志（`🧠 内心独白: ...`），不通过 send_callback 发给用户，不计入最终回复。模型可在独白里做推理、自检、去重决策。
- **最后一轮**（本轮没有 tool_use）：输出的文字才真正发给用户。

维护点：`bot/tools.py::TOOL_ROUND_REMINDER`、`PROMPT_RESPONSE_GUIDELINES` 的对应段、三个引擎的中间轮处理逻辑。

### 工具后置提示（TOOL_POST_HINTS）

`bot/tools.py::TOOL_POST_HINTS` 为特定工具定义"定向后置提示"，在每轮 tool_result 拼回模型时附加，精准投递工具后决策规则。

当前命中的工具：
- `list_reminders` → 决策辅助：查完清单后若要 set_reminder，先比对 group_id / 时间窗
- `set_reminder` → 去重自检：set 后对比【待触发的跟进计划】，发现重复立即 `delete_reminder` 精准删

helper: `build_tool_round_hint(called_names)` 在三个引擎里统一调用。

### Prompt 模块化

所有 prompt 集中在 `bot/prompts.py`，`SYSTEM_PROMPT` 由四段拼成：

| 段名 | 内容 |
|---|---|
| `PROMPT_PERSONA` | 关于她（用户画像）+ 你是谁（AI 人设） |
| `PROMPT_RESPONSE_GUIDELINES` | 回复风格、中间轮 vs 最后一轮机制、斜杠命令响应识别、消息节奏、主动聊天规则 |
| `PROMPT_TIME_PERCEPTION` | 时间感知辅助、hyperfocus 保护 |
| `PROMPT_TOOL_GUIDELINES` | 时间轴记录规则、平行事件、提醒策略（含 delete_reminder 去重）、记忆管理 |

`PromptParts` dataclass 按变化频率分三层：

- **静态层**（参与 prompt caching）：`PROMPT_PERSONA` + `PROMPT_RESPONSE_GUIDELINES` + `PROMPT_TIME_PERCEPTION`
- **半动态层**（参与 prompt caching）：`PROMPT_TOOL_GUIDELINES`（工具规则较稳定）
- **动态层**（不参与 caching）：`_build_dynamic_context()` 每次调用注入

各引擎消费方式：
- Claude: `prompt.to_claude_blocks()` → 3 个 cached system block
- Gemini / Relay: `prompt.flatten()` → 单个字符串
- 中间轮省 token: `prompt.concise().flatten()`（去掉 `PROMPT_TOOL_GUIDELINES`）

`_build_dynamic_context()` 动态注入内容：
- 【你现在记着的事】— 从 `memories` 表取全部
- 【当前进行中的事件】— `end_time IS NULL` 的活动，带重复检查规则
- 【待触发的跟进计划】— 所有 pending reminder，**带 `id=` 前缀**
- 【今日天气】— 早上时段调 `bot/weather.py::get_weather_brief()`

### 调度 Prompt 模板

均定义在 `bot/prompts.py`：
- `PROACTIVE_PROMPT` — 随机轮询：给 AI 四选一（聊几句 / 关心 / 提一嘴记忆 / [SILENT]）
- `REMINDER_PROMPT` — 提醒触发：注入 action + 优先级 + group 进度，硬规定"禁止再 set 相同内容"
- `BEDTIME_PROMPT` — 睡前提醒：22:30-00:00 随机两次
