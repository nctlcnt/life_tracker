# Life Tracker — 项目简介与开发清单

## 项目简介

Life Tracker 是一个基于 Discord + AI 的个人生活轨迹记录系统。用户通过 Discord 随意聊天，AI 自动从对话中提取活动信息，生成结构化的时间轴记录，并提供主动提醒、时间管理、以及长期记忆功能。前端为单文件静态 HTML Timeline Dashboard，通过 FastAPI 提供数据。

### 核心架构

```
Discord ↔ Python 进程 (Bot + AI Router + SQLite + FastAPI) ↔ React 前端
```

### 技术栈

- **后端**: Python, discord.py, httpx, FastAPI, SQLite
- **前端**: 单文件静态 HTML（内嵌 CSS + JS），由 FastAPI 静态挂载在 `/app/`
- **部署**: Docker → 云服务器
- **AI 动态路由**: 支持随时通过环境变量切换后端：
  - Anthropic 原生 API (`claude`)
  - OpenAI 兼容中转站 (`relay`)
  - Google Gemini 原生 API (`gemini`)

### 核心模块

| 文件 | 职责 |
|---|---|
| `main.py` | 入口，asyncio.gather 启动 Bot + Scheduler + FastAPI |
| `bot/discord_bot.py` | Discord 收发消息，支持解析用户的引用/回复消息，过滤非目标用户 |
| `bot/ai_engine.py` | AI 引擎路由器，根据 `AI_PROVIDER` 分发请求 |
| `bot/ai_engine_base.py` | 三个引擎共享的逻辑：动态上下文构建、消息格式处理、工具执行、chat/scheduled_action 高层流程 |
| `bot/ai_engine_claude.py` | Claude 原生调用引擎（仅实现 `_call_with_tools`，其余委托 base） |
| `bot/ai_engine_relay.py` | OpenAI 格式中转站调用引擎（仅实现 `_call_with_tools`，其余委托 base） |
| `bot/ai_engine_gemini.py` | Gemini 原生 REST API 调用引擎（仅实现 `_call_with_tools`，其余委托 base） |
| `bot/tools.py` | 工具定义大合集 (OpenAI / Anthropic 两种 Schema，共 9 个工具) + System Prompt；category 已改为三分法枚举 Focus/Routine/Chill，log_timeline_event 新增 project_name 字段 |
| `bot/scheduler.py` | 两个并发循环 + asyncio.Lock：Timer 循环（随机轮询+睡前）+ Reminder 循环（数据库提醒倒计时+Event 唤醒） |
| `bot/database.py` | SQLite DB 操作 (events, messages, reminders, memories, todos, deadlines)；events 表含 project_name 字段（三分法阶段新增） |
| `bot/merge.py` | 事件合并模块，将相邻同 content+category 的事件合并为时间段 |
| `api/server.py` | FastAPI 接口：`/api/timeline`(合并后), `/api/events`, `/api/categories`, `/api/memories`, `/api/reminders`, `/api/todos` |
| `frontend/index.html` | 单文件静态前端：时间轴日视图 + 记忆/提醒/待办管理页，支持分类筛选、并行事件展示 |
| `config.py` | 环境变量加载，分离 `CHAT_MODEL` 和 `POLL_MODEL` 降低成本 |

### 消息进程与数据流

系统中的消息触发和流转路径主要分为以下几种场景。所有通过 AI 处理的路径最终都会经历多轮 Tool Calling 机制，由中间轮的“内心独白”流转到最终的纯文本回复。

#### 1. 用户主动发消息
- **触发点**: 用户在 Discord 发送任意常规文字（或引用回复）。
- **使用 Prompt**: 全量 `SYSTEM_PROMPT`（包括人设、各种响应规则、记忆调度指引等）。
- **处理模型**: 配置的 `CHAT_MODEL`。
- **流转路径**:
  Discord `on_message` → `fetch_history(limit=20)` 拉取历史 → `ai_engine.chat` 注入动态上下文（记忆、正在进行的事件、待触发提醒等） → **生成 AI 工具调用或思考** → 触发各类 tool (如 `log_timeline_event` 等) → 中间轮输出内部独白并在 DB 按需写入 → 最终纯文本结果推给用户。

#### 2. 斜杠指令 (Slash Commands)
- **触发点**: 用户发送 `/todo`、`/weather` 等指令消息。
- **使用 Prompt**: 视业务而定。`/todo` 纯后端 CRUD，无须 Prompt；`/weather` 使用一段独立的极简 Prompt 要求生成穿衣建议，无需工具支持。
- **处理模型**: 轻量级的 `POLL_MODEL` (若涉及 AI 生成)。
- **流转路径**:
  Discord `on_message` 检测指令前缀 → 路由至相应 Command 处理器 → 获取外部数据或本地 DB 操作 → 生成结论并发送回 Discord。

#### 3. 随机轮询 (Proactive Check)
- **触发点**: 定时器 `Timer` 循环以 1~60 分钟随机间隔内存倒计时唤醒。
- **使用 Prompt**: `PROACTIVE_PROMPT`（要求 AI 在“聊几句”、“关心”、“提记忆”、“保持沉默：[SILENT]”内四选一）。
- **处理模型**: 成本考虑走 `POLL_MODEL`。
- **流转路径**:
  `_timer_loop` 唤醒 → `_do_proactive_check()` 提供当前时间作为入参 → 带入所有历史信息调用 `scheduled_action(allow_silent=True)` → 思考是否应该打扰 → 若输出包含 `[SILENT]` 则阻断发送；否则作为自然插入消息送达用户。

#### 4. 提醒调度 / 睡前提醒
- **触发点**: 数据库提醒倒计时结束（`Reminder` 循环），或精确的夜间硬性睡前触发段（`Timer` 循环）。
- **使用 Prompt**: 
  - 提醒用到 `REMINDER_PROMPT`（注入具体行动与优先级，要求强制直接回复内容且不得重复使用 `set_reminder`）；
  - 睡前用到 `BEDTIME_PROMPT`。
- **处理模型**: `POLL_MODEL`。
- **流转路径**:
  循环唤醒 → 提取具体 Action / Context 调用 `scheduled_action()` → AI 根据业务可能会自己调 `update_timeline_event` 或者 `delete_reminder` 做去重 → 生成纯文本结果发出提醒弹窗消息。

#### 流程脑图

```mermaid
graph TD
    %% 触发源
    UserMsg(((用户发送消息)))
    SlashCmd((/斜杠指令))
    RandPoll((随机轮询唤醒))
    RemindTrigger((Reminder/睡前触发))

    %% 核心控制流
    subgraph Discord 接入层
        Router{指令拦截与分发}
        SlashHandler[独立指令处理器<br/>如 /todo, /weather]
    end

    subgraph Scheduler 调度层
        TimerLoop[Timer 循环<br/>随机间隔/睡前]
        ReminderLoop[Reminder 循环<br/>DB 倒计时到期]
    end

    subgraph AI Engine 引擎交互层
        Chat[ai_engine.chat<br/>走 CHAT_MODEL]
        SchedAct[scheduled_action<br/>走 POLL_MODEL]
        
        SystemP(SYSTEM_PROMPT<br/>+ 动态上下文与实时记忆)
        ProActP(PROACTIVE_PROMPT<br/>四选一决策/静默决策)
        RemindP(REMINDER/BEDTIME_PROMPT<br/>动作上下文指引)
        
        subgraph 多轮思考循环
            ToolThink{大模型推理决策}
            Tools[触发对应 Tool 调用<br/>读写SQLite]
            InnerLog(🧠 中间轮: 内心独白留档)
        end
    end

    %% 分支与连接
    UserMsg -->|判断前缀| Router
    Router -- 普通对话 --> Chat
    Router -- 斜杠 / 开头 --> SlashCmd --> SlashHandler
    SlashHandler --> FinalOut

    RandPoll -.-> TimerLoop --> ProActP --> SchedAct
    RemindTrigger -.DB 到期.-> ReminderLoop --> RemindP --> SchedAct
    RemindTrigger -.夜间定时.-> TimerLoop -.睡前逻辑.-> RemindP
    
    Chat -. 注入全量 .-> SystemP --> ToolThink
    SchedAct -. 注入轮询/提醒规则 .-> ToolThink
    
    ToolThink -- 输出带工具调用 --> Tools
    Tools --> InnerLog
    InnerLog -- 携带 Action 返回结果重新提问 --> ToolThink
    
    ToolThink -- 无工具意图 / 且回答完毕 --> FinalResp[生成回复文本]
    
    %% 出口
    FinalResp --> CheckSilent{判定文字包含<br/>[SILENT] ?}
    CheckSilent -- 是 --> Drop[中止发送保持静默]
    CheckSilent -- 否 --> FinalOut(((向 Discord<br/>推送可见反馈)))
```

### 中间轮 vs 最后一轮（多轮 tool calling 机制）

一次 chat/scheduled_action 可能跨多个 AI 轮次，直到模型停止 tool_use、只输出纯文本为止。

- **中间轮**（本轮还有 tool_use）：模型输出的文字是**内心独白 / 自言自语**，仅记在日志
  （`🧠 内心独白: ...`），**不通过 send_callback 发给用户**，也不计入 `all_texts` 最终回复。
  模型可以在独白里做推理、自检、决策（比如"这条 reminder 重复了，我应该 delete_reminder id=3"）。
- **最后一轮**（本轮没有 tool_use，`stop_reason=end_turn` 或 `not tool_calls`）：
  输出的文字才真正发给用户，也是 `chat()` / `scheduled_action()` 的返回值。

这套机制由三份代码共同维护：
- `bot/tools.py::TOOL_ROUND_REMINDER` — 每轮 tool_result 之后追加的系统提示，告知模型独白 vs. 发送的区分
- `bot/tools.py::PROMPT_RESPONSE_GUIDELINES` 的"中间轮 vs 最后一轮"段 — 写在 SYSTEM_PROMPT 里的硬规则
- `bot/ai_engine_{claude,gemini,relay}.py` — 在中间轮只 log 不 send_callback 不 append

### 工具 × 后置提示（TOOL_POST_HINTS）

`bot/tools.py::TOOL_POST_HINTS` 为特定工具定义的"定向后置提示"。每轮 tool_result 被拼回
模型时，除了全局的 `TOOL_ROUND_REMINDER`，引擎还会追加本轮实际调用的工具命中的 hint，
让"使用 X 工具后应该怎么判断"这类规则精准投递，避免塞进全局 system prompt 又被忽略。

当前命中的工具：
- `list_reminders` → 决策辅助：查完清单后若要 set_reminder，先比对 group_id / 时间窗
- `set_reminder` → 去重自检：set 后对比【待触发的跟进计划】，发现重复立即 `delete_reminder` 精准删

helper: `build_tool_round_hint(called_names)` 在三个引擎里统一调用，拼出本轮的完整追加文本。

### Prompt 模块化

`bot/tools.py` 里的 `SYSTEM_PROMPT` 由四段拼成：

| 段名 | 内容 |
|---|---|
| `PROMPT_PERSONA` | 关于她（用户画像）+ 你是谁（AI 人设） |
| `PROMPT_RESPONSE_GUIDELINES` | 回复风格、中间轮 vs 最后一轮机制、斜杠命令响应识别、消息节奏、主动聊天规则 |
| `PROMPT_TIME_PERCEPTION` | 时间感知辅助、hyperfocus 保护 |
| `PROMPT_TOOL_GUIDELINES` | 时间轴记录规则、平行事件、提醒策略（含 delete_reminder 去重）、记忆管理 |

`SYSTEM_PROMPT_CONCISE`（去掉 `PROMPT_TOOL_GUIDELINES`）用于中间轮切换以节省 token。
Gemini 引擎在 `round_idx > 0` 且命中 `PERSONA_MARKER` 时会自动切换到精简版。

`_build_dynamic_context()` 每次调用注入（不参与 prompt caching）：
- 【你现在记着的事】— 从 `memories` 表取全部
- 【当前进行中的事件】— `end_time IS NULL` 的活动，带重复检查规则
- 【待触发的跟进计划】— 所有 pending reminder，**带 `id=` 前缀** 让 AI 知道怎么 `delete_reminder`
- 【今日天气】— 早上时段调 `get_weather_brief()`

### 调度 Prompt 模板

`bot/scheduler.py` 里三段模板分别对应：
- `PROACTIVE_PROMPT` — 随机轮询：给 AI 四选一（聊几句 / 关心 / 提一嘴记忆 / [SILENT]）
- `REMINDER_PROMPT` — 提醒触发：注入 action + 优先级 + group 进度，并硬规定"禁止再 set 相同内容"
- `BEDTIME_PROMPT` — 睡前提醒：22:30-00:00 随机两次

### 已解决的技术难题

- **跨平台中转站兼容问题**：放弃脆弱的 OpenAI SDK，改用原生 `httpx` 发送请求并自行解析处理工具调用，彻底解决 `str object has no choices` 等诡异闭环问题。
- **防止提醒死循环套娃**：修复 AI 收到 "提醒触发信号" 时由于认知偏差而再次调用 `set_reminder` 导致的无限弹窗 Bug。
- **降低闲时轮询成本**：将聊天与轮询的模型分为 `CHAT_MODEL` 和 `POLL_MODEL`，并在 `POLL_TOOL_NAMES` / `REMINDER_TOOL_NAMES` 里硬编码收窄工具子集，减少轮询时的 prompt 开销。
- **三引擎 DRY 重构**：将 Claude/Gemini/Relay 三个引擎的重复逻辑（动态上下文构建、消息格式处理、工具执行、chat/proactive_check/reminder_action 流程）提取到 `ai_engine_base.py`，各引擎只保留自己的 `_call_with_tools` 实现。
- **前端时区 Bug**：`shiftDate()` 使用 `toISOString()` 导致 UTC 偏移，在 AEST 时区下日期导航跳两天或原地不动，改用本地日期格式化修复。
- **Prompt 缓存与性能优化**：启用了 Anthropic Prompt Caching，静态 system_prompt 标记 `cache_control`、动态上下文不缓存；日志里持续监控 cache hit rate（实测 73-85%）。
- **集中日志化**：通过 `bot/logger.py` 实施全局配置管理，每个模块通过 `get_logger(__name__)` 拿自己的 logger。
- **中间轮独白化**：多轮 tool calling 的中间轮文本由"即时发给用户"改成"仅作 AI 内心独白"，让模型可以放心做自检、推理、去重决策而不污染用户消息流。由 `TOOL_ROUND_REMINDER` + `PROMPT_RESPONSE_GUIDELINES` + 三引擎共同维护。
- **Reminder 去重能力缺口**：发现 AI 重复 set_reminder 后试图"保留最新的"但无法删除旧条目（set 只新增，cancel 会一锅端整个 group），新增 `delete_reminder` 工具按 id 精准删单条 pending，配合【待触发的跟进计划】里的 `id=` 展示和 `TOOL_POST_HINTS[set_reminder]` 的去重自检提示形成闭环。
- **三分法 Schema 迁移**：将 category 从自由文本（休息/工作/娱乐等）迁移为严格枚举 Focus/Routine/Chill，events 表新增 project_name 字段。两套工具 schema（OpenAI + Anthropic）同步更新，热迁移方式保留旧数据。旧分类在前端颜色兜底映射，存量数据正常显示。

---

## 开发清单

### Phase 1 — 核心循环 ✅ 已完成

- [x] Discord Bot 收发消息及黑名单拦截
- [x] 解析用户的 Discord Quote (引用) 并附加上下文
- [x] AI Engine 路由及多平台适配 (Claude/Relay/Gemini)
- [x] SQLite 存储结构搭建 (events / messages / reminders)
- [x] 随机轮询 + 提醒调度器并行机制
- [x] log_timeline_event 与 update_timeline_event 工具开发
- [x] 多轮 tool calling 自主运转，中间轮文本即时发送给用户
- [x] API 稳定与灵活配置，聊天大模型与监控模型剥离

### Phase 2 — 记忆 + 智能提醒 ✅ 核心已完成

- [x] **持久化记忆系统 (memories 表)**
  - [x] save_memory: AI 主动写入重要事项（deadline、习惯）。满 20 条自动清理。
  - [x] delete_memory: 事情过去后 AI 自主遗忘。
  - [x] update_memory: 动态更新现有事件。
  - [x] 动态注入：在不破坏 Prompt 缓存的前提下，无缝把"你现在记着的事"输入给 AI。
- [x] **避免唠叨机制**：系统内置警报，不再向用户复读同一条临近提醒事项。
- [x] **Reminder 改造：从闹钟到预约轮询**
  - reminders 表新增字段：group_id, priority (low/normal/high), status (pending/triggered/cancelled)
  - 重写 set_reminder 工具描述：不是闹钟通知，是 AI 给自己安排的 follow-up
  - 同一件事的 reminder 共享 group_id，支持批量取消 (cancel_reminders)
  - database.py 新增：cancel_reminders_by_group, get_pending_reminders_by_group, list_active_reminders
  - scheduler 触发时注入 group 进度、优先级等上下文
  - _build_dynamic_context 注入 pending reminders 概览，防止 AI 重复设置
- [x] **多层级重要提醒**
  - priority: low/normal/high，high 优先级即使刚聊过也要提
  - deadline 类消息 AI 主动规划多条 reminder，越临近越密集
- [x] **睡前提醒循环**
  - 每晚 22:30-23:30 和 23:30-00:00 各随机一次，复用 reminder_action 流程
- [x] **三引擎共享逻辑抽取**
  - chat/scheduled_action 高层流程、动态上下文构建、工具执行统一到 ai_engine_base.py
- [x] **调度器重构：消除重复消息 + 精确触发**
  - 三个并发循环合并为两个：Timer 循环（随机轮询+睡前）+ Reminder 循环（DB 提醒倒计时）
  - asyncio.Lock 防止并发 AI 调用导致消息重复
  - asyncio.Event 在新增提醒时唤醒 reminder 循环重新计算
  - proactive_check + reminder_action 合并为 scheduled_action 统一入口
  - Timer 循环纯内存，不碰 DB；Reminder 循环只管 DB 提醒，天然隔离
- [ ] **主动提醒策略进阶**
  - AI 在用户提到截止点时自动 set_reminder（基本支持，但可强化上下文连贯）
  - 关联循环："我看两集就回来" → 自动设小号提醒
- [ ] **每日总结（通过 reminder 实现）**
  - AI 在首次启动或用户要求时，set_reminder 中午 + 睡前两条
  - 触发后 AI 读取今日事件生成回顾，然后自动 set_reminder 明天同一时间的下一条
  - 不需要单独的循环机制，复用 reminder 系统
- [x] **上下文归档与截断**
  - 统一上下文窗口为最近 20 条 Discord 频道消息（`fetch_history(limit=20)`），覆盖 chat / 随机轮询 / 睡前提醒 / reminder 触发所有路径
- [ ] **时区支持**
  - 当前硬编码悉尼时间（AEST），睡前提醒、轮询时间判断、天气城市均依赖硬编码
  - 改为从 config 读取时区和城市，所有时间计算统一使用 timezone-aware datetime
  - 用户换市/时区时只需改一个配置
  - 可通过 `/timezone` 斜杠命令或聊天让 AI 自动更新（AI 检测到用户提及搬家/旅行时主动提醒）

### Phase 3 — 斜杠命令 + 扩展

- [x] **斜杠命令路由**
  - 普通消息 → AI 时间管理主流程
  - /斜杠命令 → 各自的处理函数
- [x] **/todo — 待办事项管理**
  - 通过 `/todo add/list/all/done/del` 进行列表级管理
  - 与 AI 流程解耦，用于不依赖时间触发且非记忆块的独立事项
- [x] **/weather — 天气查询 + 穿衣建议**
  - 调用 wttr.in 获取天气数据，通过 `simple_completion`（POLL_MODEL，无工具）生成穿��建议
  - 复用 `bot/weather.py` 的数据获取，AI 根据温度/体感/降雨推荐具体衣物
- [x] **测试模式（`--test` 启动参数）**
  - `python main.py --test` 激活，进程退出时自动结束
  - 记录范围：全量应用日志（`life_tracker.*` logger）+ 每次 AI API 调用的完整 payload（system / messages / tools）
  - 三个 AI 引擎均已接入（Claude / Relay / Gemini），支持多轮 tool calling 的每轮独立记录
  - 输出：`data/test_logs/<end_ts>.jsonl`，交错 `"type":"log"` 和 `"type":"ai_prompt"` 条目
- [ ] **/cleanup — 今日 Timeline 整理**
  - 触发后 AI 调用 `query_timeline` 拉取今天到目前为止的所有事件
  - 以问答方式逐步确认：时间推断是否正确、内容标题是否准确、category 是否合适、重复/残留事件是否需要删除
  - 用户可以口语回答（"对""不对，是下午两点""改成 Focus"），AI 持续调用 `update_timeline_event` / `delete_timeline_event` 直到整理完毕
  - 实现为斜杠命令 `/cleanup`，触发后进入一个独立对话流，完成后退出
  - 不影响正常的 chat 流程；整理完成后 AI 给一个简短的今日时间分布总结
  - 不需要全量prompt，不需要人格化，纯工具调用 + 结果确认的闭环流程
- [ ] **/bookmark url — 收藏文章**
  - URL 解析（trafilatura / newspaper3k）提取正文存入独立库
- [ ] **/summarize url — AI 摘要文章**
- [ ] **/归档 — 手动触发上下文摘要**
- [ ] **喝水 / 身体状态询问**：偶尔插入的关心型提醒
- [ ] **饮食记录分析**
- [~] **消息路由（Router）** — 已评估，不实施
  - 原方案：用轻量模型预分类消息，按类别组装不同 prompt 模块和工具子集
  - 不实施原因（2026-04-11 验证）：
    - Claude prompt caching 实测 cache hit rate 73-76%，system prompt + tools（~4,809 tokens）几乎全部命中缓存
    - 缓存价是原价 1/10，Router 省掉的 token 本就是缓存价，净收益约 $0.0009/次，不值得
    - 用户消息天然多意图混合（"学完了去看剧" = update + log + cancel），预分类误判会导致功能缺失
    - AI 拿全部工具 + 完整上下文自行决定用哪个，本身就是更强的内置 Router
- [ ] **替代优化：轮询/提醒路径工具子集**
  - proactive_check / reminder_action 不需要全部 9 个工具，硬编码子集即可（不需要 Router）
  - proactive_check 主要用：set_reminder, save_memory, query_timeline
  - reminder_action 主要用：save_memory, cancel_reminders, query_timeline
  - 收益：减少 AI 选错工具的概率（成本影响小，但提升准确率）
- [ ] **替代优化：精简工具描述**
  - 当前 9 个工具描述共 ~4,800 tokens，部分描述冗余可压缩
  - 例如 set_reminder 描述 ~400 tokens，可压缩到 ~200 tokens
- [ ] **替代优化：验证 & 监控 prompt caching**
  - 已在 ai_engine_claude.py 加入 usage 日志（cache_creation / cache_read / hit_rate）
  - 持续观察缓存命中率，确保 5 分钟 TTL 内的调用都命中

### Phase 4 — 前端 Dashboard

- [x] **静态 HTML 时间轴日视图** (`frontend/index.html`)
  - 日期导航（左右翻页 + 日历选择 + 今天按钮）
  - 分类筛选 chips，按类别颜色区分
  - 重叠事件并行展示（parallel-row 布局）
  - 活动切换标记（↯ 类别A → 类别B）
  - 统计面板：活动片段数、注意力切换数、有效学习时长
  - 进行中事件标记
- [x] **管理页面扩展**（tab 切换）
  - 记忆列表（支持删除）
  - 提醒列表（按状态筛选：待执行/已触发/已取消）
  - 待办列表（查看未完成、已完成的待办事项）
- [x] **事件合并 API** (`bot/merge.py` + `/api/timeline`)
  - 相邻同 content+category 事件合并为时间段
- [x] **React 重构**：静态 HTML 已迁移为 React + Vite + Tailwind 组件化前端
- [x] **周视图** (`WeekView.tsx`)：按周展示各天活动分布
- [~] **日视图重构（`feature/phase1-tricat-schema` 分支，骨架已完成）**
  - [x] 日视图新布局：左 1/4 + 右 3/4（上：比例图区域 / 下：2×2 四方块）
  - [x] 移除 GanttChart 和 TimeDistribution，以占位符替换（标注 coming soon）
  - [x] 记忆 / 提醒 / 待办 / Deadlines 改为 2×2 方块布局（完全可用）
  - [ ] 多泳道时间轴实现：Focus / Routine / Chill 三条竖向泳道，支持并发显示
  - [ ] 蓄水/漏水比例图实现：展示今日 Chill vs Drain 时长，含 [蓄水]/[漏水] 筛选 tag
- [~] **新 Tab：Project Overview（占位符已上线）**
  - [x] Project Overview Tab 入口已在导航中
  - [ ] GitHub 式项目热力图：Y 轴 = Project，X 轴 = 近 90 天，格子深浅 = 当天投入分钟数
  - [ ] 后续可扩展 Streak、趋势、精力雷达图等
- [x] **导航 Tab（三 Tab）**：日 | 周 | Project Overview
- [ ] **部署到 Vercel / Netlify**

### Phase 5 — 数据科学 + 分析 (Portfolio)

- [ ] **情绪分析管道**
  - emotion_analysis 表：基于原始 note 重跑情绪分类树（Russell 环形 / Ekman）
- [ ] **时间模式分析**
  - 作息规律、工作效率高峰、拖延趋势，打点频次（碎片化活检）
- [ ] **NLP 主题聚类**：对记录做 embedding 分析核心焦点
- [ ] **每周自动报告**：合并数据、洞察并在 Discord 输出报表

### Phase 6 — 部署

- [ ] **Docker 容器化改造**
- [ ] **云服务器上云 (EC2/DO等)**
- [x] **Prompt Caching 深度优化** (基于 Anthropic 的缓存机制进一步压低成本)
