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

## memories — AI 持久记忆

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | |
| `content` | TEXT NOT NULL | |
| `created_at` | TEXT | |
| `source` | TEXT | `ai`（默认）/ `user`（用户主动让记的） |

**容量上限 20 条**：`add_memory` 超量时按 `(source='user' ASC, created_at ASC)` 排序删最旧——优先牺牲 AI 来源、保留用户来源。`update_memory` 会同时刷新 `created_at`，避免被自动清理。

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

- `events.project_name` 没有独立 projects 表，所有项目名通过 `SELECT DISTINCT project_name FROM events WHERE category='Focus'` 聚合（`get_all_project_names`）。
- `reminders.group_id` 没有独立 groups 表，AI 自由生成字符串作为分组 key。
- `messages` 与 `events` 完全解耦，AI 从聊天里提取活动后单独写 `events`。

---

## 索引

目前**全表均无显式索引**（`SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'` 返回空）。在当前数据量级（events 207 / messages 2.3k / reminders 200）下查询是 O(n) 全表扫描，仍在毫秒内。若 messages、events 继续增长，可优先考虑：

- `events(start_time)`、`events(status, start_time)` —— 时间窗查询和 planned 列表
- `reminders(status, trigger_time)` —— scheduler 的 MIN 查询
- `messages(id DESC)` —— 最近 N 条（id 已 PK，效果近似有索引）

---

## 迁移策略

`_init_tables` 末尾通过一系列 `try/except sqlite3.OperationalError: pass` 包住的 `ALTER TABLE` 实现增量迁移：列已存在就静默跳过。新增字段的标准做法是在最后追加一段同款 try/except，**不要**修改 `CREATE TABLE` 里的初始定义（否则现有数据库不会回填该列）。
