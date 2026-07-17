# 数据库结构说明

文件位置：`data/life_tracker.db`（SQLite，单文件）。所有 schema 在 `bot/database.py::Database._init_tables` 里用 `CREATE TABLE IF NOT EXISTS` 初始化，旧数据库通过 `ALTER TABLE ADD COLUMN` 在 `_init_tables` 末尾平滑迁移。时间字段统一使用 ISO 8601 字符串。

---

## events — 时间轴事件（核心表）

承载所有真实发生与计划中的活动，是日视图、周视图、Project Overview 的数据源。

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | 自增主键 |
| `start_time` | TEXT NOT NULL | ISO 8601 |
| `end_time` | TEXT | ISO 8601；为空表示进行中 |
| `content` | TEXT NOT NULL | 简短描述 |
| `category` | TEXT | 三分法枚举：`Focus` / `Routine` / `Chill`，旧数据可能是 `uncategorized` |
| `notes` | TEXT | 自由备注（迁移列） |
| `session_id` | INTEGER | 同一段连续工作的会话 id（迁移列，目前少用） |
| `is_parallel` | INTEGER | 0/1，并行事件标记（迁移列） |
| `project_name` | TEXT | 仅 Focus 类有意义；Project Overview / 甘特图 按此聚合 |
| `status` | TEXT | `NULL` = 已发生真实事件；`planned` = 未来 dummy；`cancelled` = 取消的 planned |
| `created_at` | TEXT | 入库时间，默认 `datetime('now')` |

**约束/语义**：
- `end_time IS NULL AND status IS NULL` → "正在进行的真实事件"，注入 prompt Block 4。
- `status='planned'` 的 dummy 事件由 `cancel_planned_event` 标记为 `cancelled`，**不**真正删除（前端仍展示打叉）。
- merge 模块（`bot/merge.py`）只对 `status IS NULL` 的相邻同 content+category 事件合并显示，原始行不动。

---

## messages — 聊天记录

供 AI 拉取最近 N 条作为上下文。

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | |
| `role` | TEXT NOT NULL | `user` / `assistant` |
| `content` | TEXT NOT NULL | |
| `timestamp` | TEXT | 默认 `datetime('now')` |

`get_recent_messages(limit)` 倒序取后反转为时间正序。无清理策略，长期会膨胀（目前 ~2.3k 行）。

---

## conversation_messages — Discord 原始会话日志

append-only 保存通过 Discord 收发的原始消息，作为后续 Context Builder、rolling compact、RAG、replay/debug 的 source of truth。旧 `messages` 表仍保留作兼容备份。

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | 自增主键 |
| `discord_message_id` | TEXT UNIQUE | Discord message id；用于幂等去重 |
| `channel_id` | TEXT NOT NULL | Discord channel id |
| `guild_id` | TEXT | DM 时为空 |
| `author_id` | TEXT | 发送者 id |
| `author_name` | TEXT | 发送者展示名快照 |
| `role` | TEXT NOT NULL | `user` / `assistant` / `system` |
| `content` | TEXT NOT NULL | 原始消息内容 |
| `created_at` | TEXT NOT NULL | Discord 消息创建时间，ISO 8601 |
| `reply_to_message_id` | TEXT | 引用/回复的 Discord message id |
| `metadata_json` | TEXT | JSON 附加信息，例如富化后的 prompt 文本 |
| `embedding` | TEXT | JSON float 数组（memory v3 B2，迁移列）；后台异步补写，NULL = 未算/失败 |
| `embedding_context` | TEXT | 实际拿去 embed 的拼接文本（该消息 + 前 4 条上下文），检索命中后原样展示给 AI |
| `embedding_model` | TEXT | 算 embedding 用的模型名；检索只比对同模型行，换模型后旧向量自然失效 |

当前接入点：

- `on_message` 过滤通过后写入 inbound user message。
- `_send_chat_chunks` 在 `channel.send()` 返回后写入 outbound assistant message，并记录实际 Discord message id。
- 重复 `discord_message_id` 使用 `INSERT OR IGNORE` 忽略，保证重放/重复处理不会重复插入。
- 每次写入后 `_spawn_embedding_task` 起后台任务补 embedding（`bot/embeddings.py`，
  provider 由 config.json `ai.embedding` 决定，失败静默留空）。

检索路径（memory v3 Part B2）：`chat()` 用当前用户消息 embed 出 query 向量，
`get_relevant_conversation_snippets()` 按 `relevance(cosine) + 0.1 × recency(0.995^小时)`
打分、排除最近 20 条窗口、同段对话去重后取 top-5，注入 system prompt Block 3
（`【可能相关的历史片段】`）。存量数据用 `scripts/backfill_embeddings.py` 一次性回填。

---

## reminders — 提醒队列

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | |
| `trigger_time` | TEXT NOT NULL | ISO 8601 |
| `action` | TEXT NOT NULL | 提醒文案 |
| `group_id` | TEXT | 同一件事多条提醒共享，便于批量取消（迁移列） |
| `priority` | TEXT | `low` / `normal` / `high`（迁移列） |
| `status` | TEXT | `pending` / `triggered` / `cancelled`（迁移列，权威字段） |
| `done` | INTEGER | 0/1，旧字段，仅为前端兼容同步保留 |
| `created_at` | TEXT | |

**调度语义**：scheduler 从 `MIN(trigger_time) WHERE status='pending'` 算下一次唤醒；触发后 `mark_reminder_done` 同时写 `status='triggered'` 和 `done=1`。AI 去重靠 `cancel_reminder_by_id` / `cancel_reminders_by_group`，仅对 `status='pending'` 生效。

---

## memory.md — AI 持久记忆（权威存储）

长期个人记忆的 canonical source 是 `data/memory.md`。文件包含可读的 Markdown
section，以及用于保持 API 兼容的隐藏 metadata comment（稳定 id、source、type、有效期）。

- `MarkdownMemoryRepository` 使用文件锁和同目录临时文件 + `os.replace` 原子写入；
- Prompt 读取时移除 metadata，只注入完整 Markdown 条目；
- 默认预算为 4000 estimated tokens，超限时只按完整条目裁剪，不截断单条内容；
- deadline、todo、reminder、timeline 和原始 conversation 仍以 SQLite 为权威来源；
- 配置项为 `memory.path` / `memory.token_budget`，默认 `data/memory.md` / `4000`。

## memories — 迁移期只读回滚影子

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | |
| `content` | TEXT NOT NULL | |
| `created_at` | TEXT | |
| `source` | TEXT | `ai`（默认）/ `user`（用户主动让记的） |
| `memory_type` | TEXT | 自由文本分类，不强制枚举（memory v3 B1，迁移列） |
| `valid_until` | TEXT | 过期时间；NULL = 永久（memory v3 B1，迁移列） |

该表已经不再参与应用读取。Markdown migration window 内，每次 Markdown CRUD
会把当前内容完整 shadow write 到本表，使现有 Litestream SQLite 备份仍能作为回滚来源。
生产验证、Markdown 独立异地备份和恢复演练完成后，应按 Linear cleanup issue 删除本表
以及 `Database` 中的 legacy CRUD/shadow 方法。

---

## todos — 用户待办

不经过 AI，纯前端管理。

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | |
| `content` | TEXT NOT NULL | |
| `done` | INTEGER | 0/1 |
| `created_at` | TEXT | |
| `done_at` | TEXT | 完成时间，未完成为 NULL |

`set_todo_done(id, done)` 切换状态时同步维护 `done_at`（true 写 now，false 清空）。

---

## deadlines — 结构化截止日期

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | |
| `title` | TEXT NOT NULL | |
| `due_time` | TEXT NOT NULL | ISO 8601 |
| `status` | TEXT | `active` / `completed` / `expired` |
| `created_at` | TEXT | |

`expire_past_deadlines()` 把 `active && due_time < now` 批量标为 `expired`，由前端或定期任务调用。Active deadline 注入 prompt Block 4，带倒计时。

---

## prompt_sections — Prompt 管理

前端 Admin 页面可编辑 prompt sections。prompt 正文保存在本地 SQLite，不再作为源码提交到 GitHub。

| 字段 | 类型 | 说明 |
|---|---|---|
| `key` | TEXT PK | 稳定 section key，例如 `identity`、`tools`、`reminder` |
| `label` | TEXT NOT NULL | 前端展示名 |
| `value` | TEXT NOT NULL | prompt 正文 |
| `updated_at` | TEXT | 更新时间 |

当前可编辑 section 的 key/label 定义在 `bot/prompts.py::PROMPT_SECTION_LABELS`。代码只负责声明 section、校验模板占位符和组装 prompt，不再保存私有 prompt 正文。

新数据库初始化时，如果所有 prompt section 都为空，会自动从 `docs/default-prompts.json` 导入一份默认 prompt。之后可以通过 Admin 页面改成自己的工作流。

Prompt 备份/恢复走 JSON：

```bash
python -m scripts.export_prompts
python -m scripts.import_prompts docs/default-prompts.json --apply
python -m scripts.import_prompts data/backups/prompts/prompts-YYYYMMDDTHHMMSSZ.json --apply
```

默认导出目录是 `data/backups/prompts/`，随 Docker volume 一起持久化，并被 `.gitignore` 通过 `data/` 忽略；真实私有 prompt 备份不要提交到 Git。

Docker 环境中可以在容器内运行同样的命令：

```bash
docker compose exec app python -m scripts.export_prompts
docker compose exec app python -m scripts.import_prompts docs/default-prompts.json --apply
```

---

## ai_runs / tool_calls — AI 行为可追溯性（memory v3 Part A）

每次 AI 调用（chat / oneshot / scheduled / poll / reminder / bedtime …）在 `ai_runs`
记一行，run 内的每次工具调用在 `tool_calls` 记一行。数据来自 `bot/trace.py` 的
run 生命周期（`start` → `add_round` → `finalize`），`finalize(db=...)` 时落库；
JSONL 调试文件（`data/ai_traces/`）仍然照写，SQLite 里是可查询的结构化摘要。
落库失败只记日志不抛异常，漏传 `db` 时跳过。

| ai_runs 字段 | 类型 | 说明 |
|---|---|---|
| `id` | TEXT PK | 复用 trace entry id |
| `trigger` | TEXT NOT NULL | chat / scheduled / poll / reminder / bedtime … |
| `model` / `provider` | TEXT | 当时的 preset 信息 |
| `started_at` / `finished_at` | TEXT | |
| `status` | TEXT | success / failed |
| `error` | TEXT | 失败时的异常摘要 |
| `final_text` | TEXT | 最终回复文本 |

| tool_calls 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | |
| `run_id` | TEXT NOT NULL | REFERENCES ai_runs(id)，有索引 |
| `round_n` | INTEGER | 第几轮工具调用 |
| `tool_name` | TEXT NOT NULL | |
| `arguments_json` / `result_json` | TEXT | 完整参数与结果 |
| `success` | INTEGER | 从 result 推断，取不到为 NULL |
| `created_at` | TEXT | |

---

## app_state — 进程无关的小型 KV

| 字段 | 类型 | 说明 |
|---|---|---|
| `key` | TEXT PK | |
| `value` | TEXT | |
| `updated_at` | TEXT | |

目前仅存一条：`target_channel_id`（最近活跃的 Discord 频道 id），用于冷启动时主动消息发到正确频道。`set_state` 用 `ON CONFLICT(key) DO UPDATE` upsert。

---

## pending_messages — 已废弃

| 字段 | 类型 |
|---|---|
| `id` | INTEGER PK |
| `content` | TEXT |
| `timestamp` | TEXT |

代码里没有读写路径，当前 0 行。保留是为了不动旧库；下次清理时可以删。

---

## appointments — 历史遗留

| 字段 | 类型 |
|---|---|
| `id` | INTEGER PK |
| `title` | TEXT NOT NULL |
| `scheduled_time` | TEXT NOT NULL |
| `note` | TEXT |
| `status` | TEXT (`active` / `past` / ...) |
| `created_at` | TEXT |

`_init_tables` 已不再创建该表，`bot/database.py` / `api/server.py` / `bot/tools.py` 均无引用。现存 6 行是从旧版本残留的数据，功能已被 `events` 的 `status='planned'` 替代。同样可以在下次清理时一并删表。

---

## 表间关系

无 SQL 外键约束，关系靠应用层维护：

- `events.project_name` 只保存事件引用的项目名；AI 可见项目来自独立的 `projects` 表，由用户手动创建/删除/改名，不再从事件自动反推。
- `reminders.group_id` 没有独立 groups 表，AI 自由生成字符串作为分组 key。
- `messages` 与 `events` 完全解耦，AI 从聊天里提取活动后单独写 `events`。
- `conversation_messages` 与结构化表解耦，是原始会话日志；未来 compact/RAG 从它派生摘要和 memory chunks。
- `prompt_sections` 是本地配置数据，不跟代码版本绑定；部署新库时需要先通过 Admin 页面填入各 section。

---

## 索引

目前**全表均无显式索引**（`SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'` 返回空）。在当前数据量级（events 207 / messages 2.3k / reminders 200）下查询是 O(n) 全表扫描，仍在毫秒内。若 messages、events 继续增长，可优先考虑：

- `events(start_time)`、`events(status, start_time)` —— 时间窗查询和 planned 列表
- `reminders(status, trigger_time)` —— scheduler 的 MIN 查询
- `messages(id DESC)` —— 最近 N 条（id 已 PK，效果近似有索引）
- `conversation_messages(channel_id, created_at)` —— Context Builder 按频道取最近上下文
- `prompt_sections.key` —— 主键自动索引

---

## 迁移策略

`_init_tables` 末尾通过一系列 `try/except sqlite3.OperationalError: pass` 包住的 `ALTER TABLE` 实现增量迁移：列已存在就静默跳过。新增字段的标准做法是在最后追加一段同款 try/except，**不要**修改 `CREATE TABLE` 里的初始定义（否则现有数据库不会回填该列）。
