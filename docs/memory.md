# 滚动会话记忆 + 每日自我反思

## Context（为什么做这个）

当前对话上下文是**硬性的"最近 20 条"窗口**，每次 AI 调用时从 Discord 频道历史现拉
（`bot/discord_bot.py:165` 的 `_fetch_history_as_messages(..., limit=20)`，poll 路径 8 条、
bedtime/reminder 20 条）。超过 20 条的内容直接丢失，bot 没有跨天/长期的对话连续性。

目标分两块：

1. **滚动会话记忆**：取消"最近 20 条"硬上限，改成
   *最近 N 条原文（工作窗口）+ 更早内容压成滚动摘要 + 按天/周再压缩的分层摘要*，
   实现"有效无限上下文"且 token 成本可控。压缩后的会话记忆**异步进 Redis**（热层），
   SQLite 仍是持久 source of truth。**先不上 RAG**——聊天太碎，对碎片原文做向量召回精度差；
   摘要本身就解决了无限上下文，RAG 留作后续阶段（且将来只对摘要做，不对碎片原文做）。
2. **每日自我反思**：每天结束时 bot 反思"自己今天做得好不好"（有没有过度打扰、漏接、
   语气是否合适），**存库 + 回灌进 prompt** 形成轻量自我改进闭环。

> 范围澄清（已和用户确认）：这次只改**对话上下文窗口**。
> 长期事实表 `memories`（AI 主动 `save_memory` 的最多 20 条事实、单独占 Block 3）维持现状不动。

## 已确认的关键事实（代码现状）

- 上下文来源 = **Discord 现拉**，不是 DB。`conversation_messages` 表（`database.py:47`）
  早已 append-only 落库（user 消息在 `discord_bot.py:143` 写入；带 `metadata_json` 存了
  `content_to_send`/`current_content`），**但目前完全没被用于上下文**——文档明确说它是
  "未来 Context Builder / rolling compact / RAG 的 source of truth"。本计划就是把它启用。
- Prompt 分 4 个 `cache_control` block（`prompts.py:151` `to_claude_blocks`）：
  Block1 静态 / Block2 projects / **Block3 memories** / Block4 高频动态。
  **Anthropic 最多 4 个 cache 断点**——所以摘要 + 反思**必须并进 Block 3**，不能新增第 5 个 cached block。
- 已有便宜的一次性 LLM 调用 `simple_completion()`（`ai_engine_base.py:282`）+ fallback 机制，
  压缩/反思直接复用，不必新建引擎。
- scheduler 已有每日定时能力（`_calc_bedtimes` / `_do_bedtime_reminder`，`scheduler.py:127/175`）
  和 async 循环 + `_ai_lock`，反思和压缩 job 挂这里即可。
- 无 Redis、无向量库；SQLite-only + Litestream（仅 prod）复制到 R2。端口约定：8080=prod、8081=dev。

---

## Part A — 滚动会话记忆

### 数据模型（SQLite，持久层）

新增 `conversation_summaries` 表（`bot/database.py` schema 区，紧挨 `conversation_messages`）：

```
id INTEGER PK
channel_id TEXT NOT NULL
level INTEGER NOT NULL          -- 0=滚动摘要(块级) 1=按天 2=按周
period_start TEXT, period_end TEXT
covers_until_msg_id INTEGER     -- watermark：已被摘要覆盖到的最大 conversation_messages.id
content TEXT NOT NULL
msg_count INTEGER
created_at TEXT, updated_at TEXT
```

对应 DB 方法（与现有 `add_conversation_message` 同风格，raw sqlite3）：
- `get_rolling_summary(channel_id)` / `upsert_rolling_summary(...)`（level=0，单条滚动维护）
- `get_unsummarized_messages(channel_id, after_id, limit)`（取 watermark 之后的原文）
- `list_summaries(channel_id, level, ...)`、`add_daily_summary` / `add_weekly_summary`
- watermark 存进 `conversation_summaries.covers_until_msg_id`（无需额外 app_state）

> 同时确保 **assistant 回复也写进 `conversation_messages`**：现在 AI 回复走 `messages` 表 +
> `_record_sent_message`（`discord_bot.py:319`）；检查 `_send_chat_chunks`/`_record_sent_message`
> 是否已落 `conversation_messages`，没有就补上（user/assistant 都要在同一张表，摘要才完整）。

### Redis（热层，异步）

新增 `bot/redis_client.py`（`redis.asyncio`，URL 走 config；**Redis 不可用时优雅降级**，
直接回落 SQLite，保证 dev 无 Redis 也能跑）：
- 缓存"已组装好的会话上下文"：当前滚动摘要文本 + 工作窗口快照（key 如 `ctx:{channel}:summary`）。
- 维护未摘要消息计数器/触发位，给压缩 job 当 trigger。
- 写入是 **async fire-and-forget**：持久写 SQLite 之后再异步刷 Redis，不挡请求路径
  （满足"异步存入 Redis"）。SQLite 始终是 source of truth + Litestream 备份，Redis 丢了能重建。
- docker-compose 加 `redis` 服务：**prod / staging 各一份**（或同实例不同 db），开 AOF 持久化；
  URL 进 `.env.prod` / `config.json` / `config.dev.json`，严守 8080/8081 与 config 挂载约定。

### 压缩 job（scheduler async 任务）

挂在 `bot/compaction.py`（新模块），由 `scheduler` 的 timer 循环驱动：
- **滚动压缩**：当 watermark 之后的未摘要消息数超过阈值（如保留最近 ~15 条原文当工作窗口、
  更早的累计到 ~20 条就触发），用 `simple_completion`（便宜模型/fallback preset）把"旧滚动摘要 +
  这批原文"折叠成新的滚动摘要，`upsert_rolling_summary` 并推进 watermark。
- **分层压缩**：每日/每周由反思触发点顺带做——level0 → level1（按天）、level1 → level2（按周），
  老内容越压越粗。这就是用户说的"把几块记忆再压缩"。

### 上下文组装改动

- `bot/prompts.py`：`PromptParts` 加字段 `conversation_summary`（+ Part B 的 `self_reflection`），
  新增 `LABEL_CONV_SUMMARY = "【之前聊过的事（摘要）】"` 和 `_format_conversation_summary`；
  **把它们拼进 Block 3**（`memories_text()` 里 memories 之后追加摘要 + 反思），保持 4 个 cache 断点不变。
  摘要只在压缩时变化、反思每天变一次，cadence 和 memories 接近，并块对 cache 命中友好。
- `bot/ai_engine_base.py`：`_build_prompt` 增取 `get_rolling_summary`（Redis 命中优先、miss 回 SQLite）
  和最近反思，透传给 `build_prompt`。
- **工作窗口**：第一阶段保持从 Discord 现拉最近 N 条（低风险，保留时间戳前缀/`已执行✅`/引用富化等现有处理），
  摘要只覆盖"工作窗口更早"的内容（以 watermark 为界，少量重叠无害）。
  把 `_fetch_history_as_messages` / scheduler 的 `limit` 收敛成一个 config 常量（默认 ~15-20），别再散落写死。

---

## Part B — 每日自我反思

- 新 prompt section：`PROMPT_SECTION_LABELS` 加 `"daily_reflection": "DAILY_REFLECTION_PROMPT"`
  （`prompts.py:30`），默认正文进 `docs/default-prompts.json`（首启自动灌入，见 `database.py:190-201`）。
- 新表 `reflections`（id / ref_date UNIQUE / content / created_at）+ `save_reflection` /
  `get_recent_reflections(n)`。
- 新模块 `bot/reflection.py`（仿 `google_calendar.py` 的 feature-module 形态）：
  `run_daily_reflection(db)` 收集当天素材——今天的会话摘要+原文、bot 自己发的主动消息、
  当天 `events`、触发过的 `reminders`——拼 `daily_reflection` 模板，跑 `simple_completion`
  做自我批评，`save_reflection` 落库（**不发 Discord**，纯后台）。
- scheduler 触发：每天一次、深夜/收尾时段（仿 `_calc_bedtimes` 加 `_calc_reflection_time`，
  用 `app_state` 存 `last_reflection_date` 去重，保证一天只跑一次）。
- 回灌：`_build_prompt` 取最近 1–3 条反思，经 Part A 的 `self_reflection` 字段进 Block 3，
  让 bot 据自我批评逐步修正。

---

## 改动文件清单

- `bot/database.py`：`conversation_summaries` + `reflections` 两张表及方法；补 assistant 落
  `conversation_messages`。
- `bot/redis_client.py`（新）：async Redis 连接 + 上下文缓存/计数 + 优雅降级。
- `bot/compaction.py`（新）：滚动压缩 + 按天/周分层压缩。
- `bot/reflection.py`（新）：`run_daily_reflection`。
- `bot/prompts.py`：`conversation_summary` / `self_reflection` 进 Block 3；新 LABEL + `_format_*`；
  扩 `build_prompt` 签名；加 `daily_reflection` section。
- `bot/ai_engine_base.py`：`_build_prompt` 取摘要 + 反思并透传。
- `bot/scheduler.py`：压缩 tick + 每日反思触发；工作窗口 limit 收敛成常量。
- `bot/discord_bot.py`：消息异步镜像进 Redis；统一工作窗口 limit。
- `main.py`：初始化/关闭 Redis client，注入 scheduler/bot。
- `requirements.txt`：`redis>=5`。
- `docker-compose.prod.yml` / `docker-compose.staging.yml`：加 `redis` 服务（prod/staging 分离，开 AOF）。
- `config.py` / `config.json` / `config.dev.json`：Redis URL、压缩阈值、反思时间、工作窗口大小。
- `docs/default-prompts.json`：`DAILY_REFLECTION_PROMPT` 默认正文。
- `docs/database.md`：补新表说明。

## 明确不做（本期）

- RAG / 向量库：推迟；将来只对**摘要**做，不对碎片原文做。
- 长期事实表 `memories` 的 20 上限：不动。
- 把工作窗口来源从 Discord 全切到 `conversation_messages`/Redis：留作后续（先 hybrid 降风险）。

---

## 验证（端到端）

1. **起 dev（8081）+ Redis**：连发 >阈值条消息 → 确认 `conversation_messages` 增长、
   压缩 job 在 `conversation_summaries` 写出 level0 摘要、watermark 推进。
2. **看 prompt**：下一轮 AI 调用经 `bot/trace`（已记录 `prompt_parts`）确认 system 含
   "【之前聊过的事（摘要）】"；`ai_engine_claude` 的 cache 命中率日志确认仍命中（4 block 未破）。
3. **反思**：临时把反思时间设到近几分钟（或加临时 `/reflect` 调试命令）→ 确认 `reflections`
   落库、下一轮 prompt 含反思段。
4. **Redis 容错**：停掉 Redis → bot 不崩、回落 SQLite 仍能组装上下文。
5. **分层**：跑日/周 rollup → 确认 level1/level2 摘要生成、老 level0 被收敛。
