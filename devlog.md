# Devlog — 已解决的技术难题

> 从 `00-index.md` 搬出,按时间倒序累积;新条目加在顶部。

## 2026-04-22

- **RhythmView 新前端视图 + 五 Tab 导航**(commit `f388040`):新增 `frontend/src/app/components/RhythmView.tsx`(625 行)+ `rhythm.css`(506 行),实现 Rhythm 时间管理日视图——三泳道(Chill / Focus / Routine)竖向时间轴 + 精力成本 + 意图块(intent)可视化,Morandi 配色系统。`App.tsx` 同步扩展导航为五 Tab:日 | 周 | Project Overview | 记忆 | Rhythm,其中记忆 Tab 从日视图方块内提升为独立页面,`appointments` state 同步接入但 API 端点待补齐。RhythmView 当前使用 seed 演示数据,尚未对接 `/api/` 真实数据。
- **PROTOCOLS section 临时下线**(commit `265a431`):`bot/prompts.py` 中将 PROTOCOLS section 整块注释掉,同步清理残留行内注释。动机:观察到 4 个协议信号在实际对话中触发效果不稳定,暂时移除以隔离影响,后续实测后决定是精简重写还是恢复。system prompt 其余 5 个 section(IDENTITY / USER_MODEL / SYSTEM_MECHANICS / COMMUNICATION / TOOLS_SECTION)不受影响。

## 2026-04-21

- **消息时间戳正则过滤**:`bot/discord_bot.py` 在拼接历史消息时通过正则移除时间戳前缀,避免 AI 读取历史时看到双重时间戳(Discord 消息本身带一个、`fetch_history` 格式化又加一个),降低上下文噪音。
- **工具描述大幅瘦身**(commit `0d3863f`):对 `bot/tools.py` 和 `bot/prompts.py` 进行大幅精简(共减少约 96 行 / 192 行改 96 行),去除重复和冗余措辞,将格式细节内联到 JSON Schema、Why/When 策略保留在 `TOOLS_SECTION`,解决 Phase 3 "替代优化:精简工具描述" 工作项。
- **AI 引擎 SILENT 消息处理优化**(commit `6e925e3`):三引擎(Claude / Gemini / Relay)统一改进 `[SILENT]` 标记检测逻辑,`ai_engine_base.py` 增强 text chunk 处理管道(含时间戳剥离),确保 `[SILENT]` 信号不会泄漏给用户消息流;proactive prompt 结构同步精简。
- **AI Preset 管理系统**(commit `f15ffb4`):`config.py` 引入 `Preset` dataclass + `PRESETS` 字典,`config.json` 从单组 `{provider, api_key, model}` 升级为命名 presets 表,支持多套配置按名切换;`bot/discord_bot.py` 新增 `/model [name]`(查看 / 切换主 preset)和 `/fallback <name|off>`(切换 / 关闭 fallback)两个 slash 命令,带 autocomplete;活跃 preset 状态持久化到 `data/active_preset.json`,进程重启后保持。
- **调度器轮询策略重构**(commit `03a10e8`):用"以上次 AI 调用时间戳为基准 + 45–55 分钟随机区间"替换原先的随机 1–60 分钟全局间隔;任何 chat / poll / reminder / bedtime 调用均重置基准,避免近时间段反复轮询浪费 token;轮询时历史拉取从 20 条缩减到 8 条(poll 只需判断是否开口,不需深上下文)。
- **移除 `set_reminder` TOOL_POST_HINT**(commit `1ed0ce9`):从 `TOOL_POST_HINTS` 中删除 `set_reminder` 的去重自检提示,节省每轮 tool_result 后的 token 消耗;提醒去重职责改由计划中的夜间清理任务(而非每次 set 后立即触发 `list_reminders` 自检)承担。

## 2026-04-18

- **Prompt 结构性重构 + chat/poll 完全 unify**(`refactor/prompt-sections`):原先散布在 `PERSONA / RESPONSE_CORE / RESPONSE_CHAT / RESPONSE_POLL / TIME_PERCEPTION_* / TOOL_GUIDELINES_*` 共 8 个有交叉关系的常量,重排为 6 个正交 section(IDENTITY / USER_MODEL / SYSTEM_MECHANICS / COMMUNICATION / PROTOCOLS / TOOLS_SECTION),chat / poll **共享字节一致的 static prompt**。关键决策:
  - **Hybrid 去标签化**:USER_MODEL 保留 `ADHD / 执行功能障碍` 作为概念挂载(借用模型底层知识),同时配套"⚠️ 绝对禁止约束"负向语气屏蔽医疗感;PROTOCOLS 的 4 个信号名去临床化(`Hyperfocus`→"深度专注中"、`启动困难`→"迈不出第一步"、`断电`→"高耗后的宕机"、`时间感知漂移`→"时间感偏移"),每个信号内部按主动/被动动作分叉。
  - **Cache 优先级战略**:放弃原计划的 `INITIATION_CHAT/POLL`。根因实测观察到 chat→poll 切换时 `cache_read=0`;MEMORY 里的 Claude cache 优先级策略强约束要求 static prompt 不随 mode 变化。模式差异改由 scheduler 模板前缀(`[内部触发…]` / `[约定跟进触发…]`)在 user message 里标识。`ai_engine_base._build_prompt` 也改为 chat/poll 统一传 reminders,让 Block 3 也跨模式一致。
  - **Tool 职责边界清晰化**:格式细节(ISO 8601、Focus/Routine/Chill 枚举、project_name 前缀规则)留在 `bot/tools.py` 的 JSON Schema description 里;Why/When 高层策略(何时该 log、去重判断、reminder 密集度与优先级、memory vs deadline 区分)留在 `TOOLS_SECTION` 里。
  - 后续监控与建议:
    1. **实测 cache hit rate**:观察跨模式切换时 `cache_read` 是否接近完全命中(预期 static 5444 字符 + semi 层全部命中)。
    2. **Fallback 预案**:若大模型仍因 `ADHD` 关键词展现"临床感/爹味说教",按 plan §74-75 退回纯净方案(删除概念挂载,仅保留现象描述)。
    3. **Claude 路径未来优化方向**:行文瘦身降 token(保持结构稳定),**不再重建决策框架**;模型层面 Opus → Sonnet 评估作为独立工作项。
    4. **Provider-specific prompt**:`build_prompt()` 已留 `TODO(provider-prompt)` / `_PROVIDER_SECTIONS` 扩展点,Gemini 等可能需要更简短直接的指令风格,不要覆盖 Claude 版本。
    5. **PROTOCOLS 信号 D(时间感偏移)**:相比其他 3 个信号,该识别特征在实际对话中信号最弱,需实测验证是否能稳定触发;若无效可合并或删除。
    6. **scheduler 前缀识别准确性**:INITIATION 消失后,AI 对当前是主动轮询还是被动回复的判断完全依赖 scheduler 模板前缀,需观察识别稳定性。
- **`energy_type`(chill/drain 子标签)整体撤销**:用户自述无法精准自评蓄水/漏水状态,前端蓄水漏水图表几乎不查看,保留只会增加 AI 分类负担。通过 `refactor/drop-energy-type` 分支将 `energy_type` 字段从 DB(SQLite `DROP COLUMN` 热删,兼容 SQLite 3.35+)、`tools.py` 两套 schema(OpenAI + Anthropic)、`prompts.py` 各 prompt 段落(TIME_PERCEPTION / TOOL_GUIDELINES / PROACTIVE_PROMPT)、前端 `ChillDrainChart` 组件及 `MultiLaneTimeline` drain 染色全部移除。Focus/Routine/Chill 三分法(`category` 字段)保留不变。
- **项目名复用强化(`LABEL_PROJECTS` 动态注入)**:AI 记录 Focus 事件时因大小写、修饰词差异频繁新建重复项目。新增 `【现有项目列表(Focus 用,严格优先复用)】` 动态段(`LABEL_PROJECTS`),由 `_build_dynamic_context()` 拉取已有项目名注入提示词动态层(`PromptParts.projects` 字段),并在 `TOOL_GUIDELINES_CHAT` 加强"新建前必须先看列表、同义即复用"规则。

## 2026-04-11

- **消息路由(Router)评估不实施**:Claude prompt caching 实测 cache hit rate 73-76%,Router 省掉的 token 净收益约 $0.0009/次,不值得;且用户消息天然多意图混合,预分类误判会导致功能缺失。AI 拿全部工具+完整上下文自行决策即是更强的内置 Router。

## 更早(未精确日期)

- **跨平台中转站兼容问题**:放弃脆弱的 OpenAI SDK,改用原生 `httpx` 发送请求并自行解析处理工具调用,彻底解决 `str object has no choices` 等诡异闭环问题。
- **防止提醒死循环套娃**:修复 AI 收到"提醒触发信号"时由于认知偏差而再次调用 `set_reminder` 导致的无限弹窗 Bug。
- **降低闲时轮询成本**:将聊天与轮询的模型分为 `CHAT_MODEL` 和 `POLL_MODEL`,并在 `POLL_TOOL_NAMES` / `REMINDER_TOOL_NAMES` 里硬编码收窄工具子集,减少轮询时的 prompt 开销。
- **三引擎 DRY 重构**:将 Claude/Gemini/Relay 三个引擎的重复逻辑(动态上下文构建、消息格式处理、工具执行、chat/proactive_check/reminder_action 流程)提取到 `ai_engine_base.py`,各引擎只保留自己的 `_call_with_tools` 实现。
- **前端时区 Bug**:`shiftDate()` 使用 `toISOString()` 导致 UTC 偏移,在 AEST 时区下日期导航跳两天或原地不动,改用本地日期格式化修复。
- **Prompt 缓存与性能优化**:启用了 Anthropic Prompt Caching,静态 system_prompt 标记 `cache_control`、动态上下文不缓存;日志里持续监控 cache hit rate(实测 73-85%)。
- **集中日志化**:通过 `bot/logger.py` 实施全局配置管理,每个模块通过 `get_logger(__name__)` 拿自己的 logger。
- **中间轮独白化**:多轮 tool calling 的中间轮文本由"即时发给用户"改成"仅作 AI 内心独白",让模型可以放心做自检、推理、去重决策而不污染用户消息流。由 `TOOL_ROUND_REMINDER` + `PROMPT_RESPONSE_GUIDELINES` + 三引擎共同维护。
- **Reminder 去重能力缺口**:发现 AI 重复 set_reminder 后试图"保留最新的"但无法删除旧条目(set 只新增,cancel 会一锅端整个 group),新增 `delete_reminder` 工具按 id 精准删单条 pending,配合【待触发的跟进计划】里的 `id=` 展示和 `TOOL_POST_HINTS[set_reminder]` 的去重自检提示形成闭环。
- **三分法 Schema 迁移**:将 category 从自由文本(休息/工作/娱乐等)迁移为严格枚举 Focus/Routine/Chill,events 表新增 project_name 字段。两套工具 schema(OpenAI + Anthropic)同步更新,热迁移方式保留旧数据。旧分类在前端颜色兜底映射,存量数据正常显示。
- **Prompt 集中管理迁移(`bot/prompts.py` + `PromptParts`)**:将原先散布于 `bot/tools.py` 的所有 prompt 常量与 System Prompt 集中到 `bot/prompts.py`,引入 `PromptParts` dataclass 实现三层缓存结构(静态层 + 半动态层参与 prompt caching,动态层每次调用重建)。Claude 引擎通过 `to_claude_blocks()` 消费,Relay/Gemini 通过 `flatten()` 消费,中间轮省 token 时调用 `concise().flatten()`。`PROACTIVE_PROMPT` / `REMINDER_PROMPT` / `BEDTIME_PROMPT` 也统一移入此模块。
- **工具调用 Discord Reaction 反馈**:AI 调用写入类工具(log/update/delete/save 等)时,Bot 自动在 AI 消息上追加 ✅ reaction;query 类查询工具不触发,避免 reaction 泛滥。写入工具白名单维护于 `bot/discord_bot.py`。
- **Docker 容器化支持**:多阶段构建 Dockerfile(Node.js 前端编译 + Python 后端运行)、`docker-compose.yml`(开发)和 `docker-compose.prod.yml`(生产),支持 `config.json` 挂载和镜像发布流程。
