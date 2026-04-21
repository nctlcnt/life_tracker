# Life Tracker — 开发进度

## 近期计划更新

以下计划文件在最近 7 天内有更新（截至 2026-04-21）：

- `Plan-energy.md` — 2026-04-14 创建，2026-04-15 更新；**2026-04-18 正式归档**：精力调度三分法 chill/drain 子标签（`energy_type` 字段与 `ChillDrainChart`）已通过 `refactor/drop-energy-type` 分支整体移除，Focus/Routine/Chill 三分法保留。后续第四、五阶段（情绪评分 / 数据洞察扩展）转入 Merlin 路线。
- `plan-Merlin.md` — 2026-04-15 创建并更新，Merlin 精力调度引擎系统架构 v3（离线特征抽取管道、双轨运行机制、分阶段路线图 M1–M4+、LLM 抽取器 benchmark 方案 `bot/merlin/evals/`）
- `plan-prompt.md` — 2026-04-15 新增，**2026-04-18 更新进度与策略**；已决定放弃 Claude 路径上的「分层决策框架」（Step 1/2/3/4），优先保障 prompt caching 命中率；**2026-04-18 正式归档并由 `plan-prompt-new.md` 替代**：6 个正交 section（IDENTITY / USER_MODEL / SYSTEM_MECHANICS / COMMUNICATION / PROTOCOLS / TOOLS_SECTION）已完整落地，chat/poll 完全 unify，prompt 重构计划全部完成。
- `Plan-Obsidian-Claude-Code.md` — 2026-04-14 新增，Obsidian 课业笔记接入方案（`query_obsidian` 工具 + `obsidian_mcp_server.py`，日和 bot 与 Claude Code 共享同一 `bot/obsidian_search.py` 逻辑）
- `ai-ai-token-eventual-kahn.md` — 2026-04-21 新增，Classifier 分流架构设计方案：轻量 Gemini Flash classifier → 专用处理器（`diet` / `assistant` / `general` 三路径），单标签路由 + `escalate_to_assistant` escalation 机制；新增 `bot/classifier.py`、`bot/processor_diet.py`、`bot/processor_assistant.py`、`bot/processor_general.py`、`bot/router.py` 文件规划；**尚未实施**。

---

## 已解决的技术难题

- **跨平台中转站兼容问题**：放弃脆弱的 OpenAI SDK，改用原生 `httpx` 发送请求并自行解析处理工具调用，彻底解决 `str object has no choices` 等诡异闭环问题。
- **防止提醒死循环套娃**：修复 AI 收到"提醒触发信号"时由于认知偏差而再次调用 `set_reminder` 导致的无限弹窗 Bug。
- **降低闲时轮询成本**：将聊天与轮询的模型分为 `CHAT_MODEL` 和 `POLL_MODEL`，并在 `POLL_TOOL_NAMES` / `REMINDER_TOOL_NAMES` 里硬编码收窄工具子集，减少轮询时的 prompt 开销。
- **三引擎 DRY 重构**：将 Claude/Gemini/Relay 三个引擎的重复逻辑（动态上下文构建、消息格式处理、工具执行、chat/proactive_check/reminder_action 流程）提取到 `ai_engine_base.py`，各引擎只保留自己的 `_call_with_tools` 实现。
- **前端时区 Bug**：`shiftDate()` 使用 `toISOString()` 导致 UTC 偏移，在 AEST 时区下日期导航跳两天或原地不动，改用本地日期格式化修复。
- **Prompt 缓存与性能优化**：启用了 Anthropic Prompt Caching，静态 system_prompt 标记 `cache_control`、动态上下文不缓存；日志里持续监控 cache hit rate（实测 73-85%）。
- **集中日志化**：通过 `bot/logger.py` 实施全局配置管理，每个模块通过 `get_logger(__name__)` 拿自己的 logger。
- **中间轮独白化**：多轮 tool calling 的中间轮文本由"即时发给用户"改成"仅作 AI 内心独白"，让模型可以放心做自检、推理、去重决策而不污染用户消息流。由 `TOOL_ROUND_REMINDER` + `PROMPT_RESPONSE_GUIDELINES` + 三引擎共同维护。
- **Reminder 去重能力缺口**：发现 AI 重复 set_reminder 后试图"保留最新的"但无法删除旧条目（set 只新增，cancel 会一锅端整个 group），新增 `delete_reminder` 工具按 id 精准删单条 pending，配合【待触发的跟进计划】里的 `id=` 展示和 `TOOL_POST_HINTS[set_reminder]` 的去重自检提示形成闭环。
- **三分法 Schema 迁移**：将 category 从自由文本（休息/工作/娱乐等）迁移为严格枚举 Focus/Routine/Chill，events 表新增 project_name 字段。两套工具 schema（OpenAI + Anthropic）同步更新，热迁移方式保留旧数据。旧分类在前端颜色兜底映射，存量数据正常显示。
- **消息路由（Router）评估不实施**（2026-04-11 验证）：Claude prompt caching 实测 cache hit rate 73-76%，Router 省掉的 token 净收益约 $0.0009/次，不值得；且用户消息天然多意图混合，预分类误判会导致功能缺失。AI 拿全部工具+完整上下文自行决策即是更强的内置 Router。
- **Prompt 集中管理迁移（`bot/prompts.py` + `PromptParts`）**：将原先散布于 `bot/tools.py` 的所有 prompt 常量与 System Prompt 集中到 `bot/prompts.py`，引入 `PromptParts` dataclass 实现三层缓存结构（静态层 + 半动态层参与 prompt caching，动态层每次调用重建）。Claude 引擎通过 `to_claude_blocks()` 消费，Relay/Gemini 通过 `flatten()` 消费，中间轮省 token 时调用 `concise().flatten()`。`PROACTIVE_PROMPT` / `REMINDER_PROMPT` / `BEDTIME_PROMPT` 也统一移入此模块。
- **工具调用 Discord Reaction 反馈**：AI 调用写入类工具（log/update/delete/save 等）时，Bot 自动在 AI 消息上追加 ✅ reaction；query 类查询工具不触发，避免 reaction 泛滥。写入工具白名单维护于 `bot/discord_bot.py`。
- **Docker 容器化支持**：多阶段构建 Dockerfile（Node.js 前端编译 + Python 后端运行）、`docker-compose.yml`（开发）和 `docker-compose.prod.yml`（生产），支持 `config.json` 挂载和镜像发布流程。
- **Prompt 结构性重构 + chat/poll 完全 unify**（2026-04-18，`refactor/prompt-sections`）：原先散布在 `PERSONA / RESPONSE_CORE / RESPONSE_CHAT / RESPONSE_POLL / TIME_PERCEPTION_* / TOOL_GUIDELINES_*` 共 8 个有交叉关系的常量，重排为 6 个正交 section（IDENTITY / USER_MODEL / SYSTEM_MECHANICS / COMMUNICATION / PROTOCOLS / TOOLS_SECTION），chat / poll **共享字节一致的 static prompt**。关键决策：
  - **Hybrid 去标签化**：USER_MODEL 保留 `ADHD / 执行功能障碍` 作为概念挂载（借用模型底层知识），同时配套"⚠️ 绝对禁止约束"负向语气屏蔽医疗感；PROTOCOLS 的 4 个信号名去临床化（`Hyperfocus`→"深度专注中"、`启动困难`→"迈不出第一步"、`断电`→"高耗后的宕机"、`时间感知漂移`→"时间感偏移"），每个信号内部按主动/被动动作分叉。
  - **Cache 优先级战略**：放弃原计划的 `INITIATION_CHAT/POLL`。根因实测观察到 chat→poll 切换时 `cache_read=0`；MEMORY 里的 Claude cache 优先级策略强约束要求 static prompt 不随 mode 变化。模式差异改由 scheduler 模板前缀（`[内部触发…]` / `[约定跟进触发…]`）在 user message 里标识。`ai_engine_base._build_prompt` 也改为 chat/poll 统一传 reminders，让 Block 3 也跨模式一致。
  - **Tool 职责边界清晰化**：格式细节（ISO 8601、Focus/Routine/Chill 枚举、project_name 前缀规则）留在 `bot/tools.py` 的 JSON Schema description 里；Why/When 高层策略（何时该 log、去重判断、reminder 密集度与优先级、memory vs deadline 区分）留在 `TOOLS_SECTION` 里。
  - 后续监控与建议：
    1. **实测 cache hit rate**：观察跨模式切换时 `cache_read` 是否接近完全命中（预期 static 5444 字符 + semi 层全部命中）。
    2. **Fallback 预案**：若大模型仍因 `ADHD` 关键词展现"临床感/爹味说教"，按 plan §74-75 退回纯净方案（删除概念挂载，仅保留现象描述）。
    3. **Claude 路径未来优化方向**：行文瘦身降 token（保持结构稳定），**不再重建决策框架**；模型层面 Opus → Sonnet 评估作为独立工作项。
    4. **Provider-specific prompt**：`build_prompt()` 已留 `TODO(provider-prompt)` / `_PROVIDER_SECTIONS` 扩展点，Gemini 等可能需要更简短直接的指令风格，不要覆盖 Claude 版本。
    5. **PROTOCOLS 信号 D（时间感偏移）**：相比其他 3 个信号，该识别特征在实际对话中信号最弱，需实测验证是否能稳定触发；若无效可合并或删除。
    6. **scheduler 前缀识别准确性**：INITIATION 消失后，AI 对当前是主动轮询还是被动回复的判断完全依赖 scheduler 模板前缀，需观察识别稳定性。
- **`energy_type`（chill/drain 子标签）整体撤销**：用户自述无法精准自评蓄水/漏水状态，前端蓄水漏水图表几乎不查看，保留只会增加 AI 分类负担。通过 `refactor/drop-energy-type` 分支将 `energy_type` 字段从 DB（SQLite `DROP COLUMN` 热删，兼容 SQLite 3.35+）、`tools.py` 两套 schema（OpenAI + Anthropic）、`prompts.py` 各 prompt 段落（TIME_PERCEPTION / TOOL_GUIDELINES / PROACTIVE_PROMPT）、前端 `ChillDrainChart` 组件及 `MultiLaneTimeline` drain 染色全部移除。Focus/Routine/Chill 三分法（`category` 字段）保留不变。
- **项目名复用强化（`LABEL_PROJECTS` 动态注入）**：AI 记录 Focus 事件时因大小写、修饰词差异频繁新建重复项目。新增 `【现有项目列表（Focus 用，严格优先复用）】` 动态段（`LABEL_PROJECTS`），由 `_build_dynamic_context()` 拉取已有项目名注入提示词动态层（`PromptParts.projects` 字段），并在 `TOOL_GUIDELINES_CHAT` 加强"新建前必须先看列表、同义即复用"规则。
- **消息时间戳正则过滤**（2026-04-21）：`bot/discord_bot.py` 在拼接历史消息时通过正则移除时间戳前缀，避免 AI 读取历史时看到双重时间戳（Discord 消息本身带一个、`fetch_history` 格式化又加一个），降低上下文噪音。
- **工具描述大幅瘦身**（2026-04-21，commit `0d3863f`）：对 `bot/tools.py` 和 `bot/prompts.py` 进行大幅精简（共减少约 96 行 / 192 行改 96 行），去除重复和冗余措辞，将格式细节内联到 JSON Schema、Why/When 策略保留在 `TOOLS_SECTION`，解决 Phase 3 "替代优化：精简工具描述" 工作项。
- **AI 引擎 SILENT 消息处理优化**（2026-04-21，commit `6e925e3`）：三引擎（Claude / Gemini / Relay）统一改进 `[SILENT]` 标记检测逻辑，`ai_engine_base.py` 增强 text chunk 处理管道（含时间戳剥离），确保 `[SILENT]` 信号不会泄漏给用户消息流；proactive prompt 结构同步精简。
- **AI Preset 管理系统**（2026-04-21，commit `f15ffb4`）：`config.py` 引入 `Preset` dataclass + `PRESETS` 字典，`config.json` 从单组 `{provider, api_key, model}` 升级为命名 presets 表，支持多套配置按名切换；`bot/discord_bot.py` 新增 `/model [name]`（查看 / 切换主 preset）和 `/fallback <name|off>`（切换 / 关闭 fallback）两个 slash 命令，带 autocomplete；活跃 preset 状态持久化到 `data/active_preset.json`，进程重启后保持。
- **调度器轮询策略重构**（2026-04-21，commit `03a10e8`）：用"以上次 AI 调用时间戳为基准 + 45–55 分钟随机区间"替换原先的随机 1–60 分钟全局间隔；任何 chat / poll / reminder / bedtime 调用均重置基准，避免近时间段反复轮询浪费 token；轮询时历史拉取从 20 条缩减到 8 条（poll 只需判断是否开口，不需深上下文）。
- **移除 `set_reminder` TOOL_POST_HINT**（2026-04-21，commit `1ed0ce9`）：从 `TOOL_POST_HINTS` 中删除 `set_reminder` 的去重自检提示，节省每轮 tool_result 后的 token 消耗；提醒去重职责改由计划中的夜间清理任务（而非每次 set 后立即触发 `list_reminders` 自检）承担。

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
  - 可通过 `/timezone` 斜杠命令或聊天让 AI 自动更新

### Phase 3 — 斜杠命令 + 扩展

- [x] **斜杠命令路由**
  - 普通消息 → AI 时间管理主流程
  - /斜杠命令 → 各自的处理函数
- [x] **/todo — 待办事项管理**
  - 通过 `/todo add/list/all/done/del` 进行列表级管理
  - 与 AI 流程解耦，用于不依赖时间触发且非记忆块的独立事项
- [x] **/weather — 天气查询 + 穿衣建议**
  - 调用 wttr.in 获取天气数据，通过 `simple_completion`（POLL_MODEL，无工具）生成穿衣建议
  - 复用 `bot/weather.py` 的数据获取，AI 根据温度/体感/降雨推荐具体衣物
  - （2026-04-15 增强）新增逐时预报展示与防晒建议（紫外线指数 + 防晒推荐）
- [x] **/model 与 /fallback — AI Preset 动态切换**（2026-04-21）
  - `/model [name]`：列出状态或按名切换主 preset（带 autocomplete）
  - `/fallback <name|off>`：切换或关闭 fallback preset
  - 活跃状态持久化到 `data/active_preset.json`
- [x] **测试模式（`--test` 启动参数）**
  - `python main.py --test` 激活，进程退出时自动结束
  - 记录范围：全量应用日志 + 每次 AI API 调用的完整 payload（system / messages / tools）及 AI 响应内容
  - 三个 AI 引擎均已接入，支持多轮 tool calling 的每轮独立记录
  - 输出：`data/test_logs/<end_ts>.jsonl`，交错 `"type":"log"` / `"type":"ai_prompt"` / `"type":"ai_response"` 条目
  - viewer 支持按会话分组查看
- [ ] **/cleanup — 今日 Timeline 整理**
  - 触发后 AI 调用 `query_timeline` 拉取今天到目前为止的所有事件
  - 以问答方式逐步确认：时间推断是否正确、内容标题是否准确、category 是否合适、重复/残留事件是否需要删除
  - 用户可以口语回答，AI 持续调用 `update_timeline_event` / `delete_timeline_event` 直到整理完毕
  - 不影响正常的 chat 流程；整理完成后 AI 给一个简短的今日时间分布总结
- [ ] **/bookmark url — 收藏文章**
  - URL 解析（trafilatura / newspaper3k）提取正文存入独立库
- [ ] **/summarize url — AI 摘要文章**
- [ ] **/归档 — 手动触发上下文摘要**
- [ ] **喝水 / 身体状态询问**：偶尔插入的关心型提醒
- [ ] **饮食记录分析**
- [ ] **替代优化：轮询/提醒路径工具子集**
  - proactive_check / reminder_action 不需要全部工具，硬编码子集可提升准确率
- [x] **替代优化：精简工具描述**（2026-04-21，commit `0d3863f`）
  - `bot/tools.py` + `bot/prompts.py` 大幅精简，去除冗余措辞，格式细节内联 JSON Schema

### Phase 4 — 前端 Dashboard

- [x] **静态 HTML 时间轴日视图** → 已迁移为 React + Vite + Tailwind 组件化前端
- [x] **React 重构**：React + Vite + Tailwind 组件化前端
- [x] **周视图** (`WeekView.tsx`)：按周展示各天活动分布
- [x] **管理页面扩展**（tab 切换）
  - 记忆列表（支持删除）
  - 提醒列表（按状态筛选：待执行/已触发/已取消）
  - 待办列表（查看未完成、已完成的待办事项）
- [x] **事件合并 API** (`bot/merge.py` + `/api/timeline`)
  - 相邻同 content+category 事件合并为时间段
- [x] **导航 Tab（三 Tab）**：日 | 周 | Project Overview
- [~] **日视图重构（`feature/phase1-tricat-schema` 分支，骨架已完成）**
  - [x] 日视图新布局：左 1/4 时间轴 + 右 3/4 的 2×2 四方块
  - [x] 移除 GanttChart 和 TimeDistribution，以占位符替换
  - [x] 记忆 / 提醒 / 待办 / Deadlines 改为 2×2 方块布局（完全可用）
  - [x] 多泳道时间轴实现：Focus / Routine / Chill 三条竖向泳道，支持并发显示
- [~] **新 Tab：Project Overview（占位符已上线）**
  - [x] Project Overview Tab 入口已在导航中
  - [x] GitHub 式项目热力图：Y 轴 = Project，X 轴 = 近 90 天，格子深浅 = 当天投入分钟数
  - [ ] 后续可扩展 Streak、趋势、精力雷达图等
- [ ] **部署到 Vercel / Netlify**

### Phase 5 — 数据科学 + 分析 (Portfolio)

- [ ] **情绪分析管道**
  - emotion_analysis 表：基于原始 note 重跑情绪分类树（Russell 环形 / Ekman）
- [ ] **时间模式分析**
  - 作息规律、工作效率高峰、拖延趋势，打点频次（碎片化活检）
- [ ] **NLP 主题聚类**：对记录做 embedding 分析核心焦点
- [ ] **每周自动报告**：合并数据、洞察并在 Discord 输出报表

### Phase 6 — 部署

- [x] **Docker 容器化支持**（多阶段构建 Dockerfile + docker-compose.yml / docker-compose.prod.yml）
- [ ] **云服务器上云 (EC2/DO 等)**
- [x] **Prompt Caching 深度优化** (基于 Anthropic 的缓存机制进一步压低成本)
