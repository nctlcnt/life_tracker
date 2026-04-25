# Notes + Memory 重构（2026Q2 原 Phase 1）— 已推翻

**归档日期**：2026-04-25
**来源**：`plans/2-specs/2026Q2-consolidation.md` 原 Phase 1 章节，已从主 spec 抽离归档。

## 推翻理由

原 Phase 1 的核心主张：把 `events.notes` 拆成独立的 `notes` 表（first-class），memory 瘦身为"纯偏好"，再加每日 summary cron 写入 `notes(source='ai_summary')`。

实际推翻了两层：

1. **notes 表 first-class 化没必要**。
   过去做过的事情的备注 = `events.notes` 已经够用；未来事情的备注 = 要么挂在 planned event（events 表）上，要么挂在 deadline 上。"考试需要先去排队"这种 note 自然落在 events.notes，不需要一张独立的 notes 表。详见与之并行的考量：deadline 是否要加 notes —— 结论也是不加，因为 deadline 是紧迫性标签，notes 都归到 events.notes 一处。

2. **memory 不该收紧到"纯偏好"**。
   memory 的边界其实不该按"是不是长期偏好"切，应该按"AI 之后会不会用得上"切。临时但重要的笔记（"明天考试要带准考证"）放 memory 里没毛病，过期了再 delete。这跟原方案"事件、进度、待办一律走 add_note，memory 只放偏好"刚好相反——继续容纳备忘录式的内容才是 memory 的实际价值。

副作用：

- daily summary cron 失去落点（原计划写到 notes(source='ai_summary')）。如果以后真要做摘要，写到 memory 即可，但当前没强诉求，**整个 cron 也搁置**。
- Block 3（memories）的 prompt 注入维持现状，不重构成 `memories_and_summaries`。
- 原 Phase 1 实施清单里所有 11 步全部不做。

## 执行历史

短暂跑过一版实现：commit `d352adb feat(notes): refactor note management`（建 notes 表、3 个 note 工具、events tools 去掉 notes 参数、prompt 改写）。当晚 review 时发现上述根本问题，即刻 revert（`0f7184c`）+ DB 清理脚本（`scripts/rollback_notes_migration.py`，跑完即弃）+ merge 进 main。所以 main 历史里有 `d352adb` 和 `0f7184c` 这对相互抵消的 commit，留作回滚见证。

## 原 Phase 1 内容（备查）

以下是从 `2026Q2-consolidation.md` 抽出的原始 Phase 1 章节，仅作历史留档，**不要据此实施**。

---

## Phase 1：Notes + Memory 重构（优先级 1 + 4 + 5）

**目标**：建立一张跟 timeline 解耦的 `notes` 表；memories 瘦身为"纯偏好"；每日 AI 自动摘要以 rolling 方式代替现在被当作"备忘录"使用的 memory 机制。

### 1.1 数据建模

**新表**：

```sql
CREATE TABLE notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    date       TEXT    NOT NULL,              -- YYYY-MM-DD，归属哪一天
    content    TEXT    NOT NULL,
    source     TEXT    NOT NULL,              -- 'user' | 'ai_summary' | 'ai_auto'
    created_at TEXT    DEFAULT (datetime('now')),
    updated_at TEXT    DEFAULT (datetime('now'))
);
CREATE INDEX idx_notes_date   ON notes(date);
CREATE INDEX idx_notes_source ON notes(source);
```

**source 枚举**：
- `user`：用户显式要 AI 记的、或前端手动加的
- `ai_summary`：凌晨 cron 对当日的总结（rolling 吞并）
- `ai_auto`：AI 在对话中自主判断"值得记一笔"而写的（低频、保守）

**memories 表**：不动 schema，但 prompt 指引改为"只记偏好/长期事实"。这只是使用口径的变化，老数据保持原样（后续视情况人工或 cron 清理）。

**events.notes 字段**：不做数据迁移（不做任何"搬运"）。字段保留在 DB 里兜底老数据显示，但：
- API 响应里不再返回 `notes` 字段
- 前端不再渲染
- `log_timeline_event` / `update_timeline_event` 工具签名移除 `notes` 参数
- 后续某次大清理再 `DROP COLUMN`（不在本 phase）

### 1.2 AI 工具

**新增**：
- `add_note(content, date?)`：默认 date = 今天 (AEST)，可指定未来/过去任意日期。`source='user'`（AI 代用户记的）或 `'ai_auto'`（AI 自己的判断）由工具参数区分，或者用两个独立工具 `add_note_for_user` / `add_note_auto`（**建议用一个工具 + 显式 `source` 参数**，省一个工具）
- `update_note(note_id, content)`：覆盖
- `delete_note(note_id)`

**修改**：
- `log_timeline_event`：去掉 `notes` 参数
- `update_timeline_event`：去掉 `notes` 参数；描述里加一句"要记感想/备注，去 `add_note`"
- `save_memory` / `update_memory`：描述里加"只记用户的长期偏好/不变事实（例：喜欢冷咖啡、讨厌语音消息）。事件、进度、待办一律走 `add_note`"

**工具数量**：从现 10 个变 12 个（+3 note，-1 已经不存在的 delete_timeline_event 假设已在前一轮删）。在 `bot/tools.py` 的 `TOOLS` / `TOOLS_ANTHROPIC` 两套 schema 同步。

### 1.3 Daily Summary Cron（替代现在 memory 当备忘录的用法）

**触发**：每日凌晨 4am（AEST），跟 role-split 原 Role C 清理任务合并成一个 cron。

**流程**：
1. 取昨天的 events（含 merged）+ 昨天新增的 `notes WHERE source='user'`
2. 喂给 Flash / Haiku（便宜模型），prompt 简洁：
   ```
   基于以下材料为用户生成一段 ≤200 字的摘要。摘要要点：
   - 今天做了什么（Focus 为主）
   - 有什么未解决/延续的事（要放到明天 prompt 里提醒用）
   - 情绪/状态信号（如果能看出来）
   只输出摘要文本，不要标题、不要列表符号。
   ```
3. 写入 `notes (date=昨天, source='ai_summary', content=摘要文本)`
4. 如果同一 date 已有 `ai_summary` 行：更新（`updated_at = now`）
5. 可选：同时扫最近 7 天的 `ai_summary` 看是否 >7 条，老的删（保持滑动窗口）

**实现位置**：`bot/scheduler.py` 加 `_do_daily_summary()`；`bot/prompts.py` 加 `DAILY_SUMMARY_PROMPT`；`bot/ai_engine_base.py` 加 `simple_summary(prompt, data)` 或复用 `simple_completion`。

### 1.4 Prompt 注入改造（`bot/ai_engine_base.py::_build_prompt`）

**当前 Block 3（memories）** 改成 **Block 3（memories + 最近摘要）**：
```
【你现在记着的事】
<memories 列表（纯偏好）>

【最近几天的轨迹回顾】
2026-04-22: <ai_summary 昨天>
2026-04-21: <ai_summary 前天>
2026-04-20: <ai_summary 大前天>
(最多 3 天，按需调)
```

`PromptParts.memories` 字段改名为 `memories_and_summaries` 并重构内容。Block 3 继续独立 cache，因为这两块都是低频更新。

**TOOLS_SECTION 新增 Notes 段**：
```
## Notes（add_note / update_note / delete_note）
- notes 是用户的每日流水 / 自由笔记，跟 timeline event 完全独立
- 用户口述【X 月 X 日要做 Y】且不是现在正在做 → add_note(date=那天, content=Y)
- 用户说了当天的想法/感想且跟某个 ongoing event 无强关联 → add_note(date=今天, content=想法)
- 以前写 notes 到 event 上的场景，现在一律走这里
- source 参数：用户让你记的 → 'user'；你自己判断值得记的 → 'ai_auto'（谨慎用）
- 已结束的事件要加感想也走这里，不要动已结束的 event
```

**TOOLS_SECTION Memory 段改写**：
```
## Memory（save_memory / update_memory / delete_memory）
- 只记用户的长期偏好、不变事实、性格/习惯/禁忌
  - 好例：喜欢冷咖啡；讨厌语音消息；凌晨两点后无法思考
  - 坏例（应走 notes）：今天很累；明天有考试；下周三见朋友
- 上限 20 条，旧的用户偏好失效就 delete 或 update
```

### 1.5 API + 前端

**新 API**：
- `GET /api/notes?date=YYYY-MM-DD` — 单日
- `GET /api/notes/week?start=YYYY-MM-DD` — 一周，返回 `{date: [notes...]}` dict
- `POST /api/notes` — 前端可手动加（不经过 AI）
- `PATCH /api/notes/:id` / `DELETE /api/notes/:id`

**前端新 tab `NotesView`**（或整合进现有 RhythmView/WeekView 的 layout）：
- 按周展示，每天一个 column
- 每个 column 里**混合渲染**：
  - 当天的 `events` 开始时间 + content（灰色小字，只读；仅显示，不写库）
  - 当天的 `notes`（按 created_at 排序）
- 源数据依然两张表，前端做 UI 层 join；API 可以直接暴露合并好的结构给前端省事（`GET /api/notes/week` 内部 join events）

**Review 里的说明**：用户同意前端把 event 时间混进 notes 视图来显示，但**后端两张表不动**。

### 1.6 User messages 存储（原优先级 5）

**结论**：依赖 Discord 官方 Data Package 导出（用户 → Discord 设置 → 隐私 → 请求数据；数周后得到 zip 含全部消息）。自建 `messages` 表不再作为"用户回顾"的主来源。

**现有 messages 表**：
- 不删、不扩展功能
- 继续作为 AI 上下文冷备份

**未来可选**：如果发现 Data Package 不够（比如想按日期检索），再考虑在前端加一个 `/api/messages/review` 页面。本 phase 不做。

### 1.7 Phase 1 实施步骤

1. `bot/database.py` 加 `notes` 表 + 操作函数：`add_note` / `update_note` / `delete_note` / `get_notes_by_date` / `get_notes_by_week` / `get_recent_summaries(n=7)`
2. `bot/tools.py` 加 3 个 note 工具（2 套 schema）；`log/update_timeline_event` 去掉 notes 参数
3. `bot/ai_engine_base.py::_execute_tool` 加 3 个 note 分支；去掉 update_timeline_event 的 notes 处理
4. `bot/prompts.py` 改 TOOLS_SECTION（加 Notes 段、改 Memory 段）；`PromptParts` 字段重命名；`_build_prompt` 改 Block 3 内容
5. `bot/scheduler.py` 加 `_do_daily_summary` + 4am 触发
6. `bot/prompts.py` 加 `DAILY_SUMMARY_PROMPT` + format 函数
7. `api/server.py` 加 `/api/notes` 相关路由
8. `frontend/` 新增 NotesView（暂时可以先做简单版：单日列表，周视图下个迭代）
9. 前端所有 `event.notes` 引用去掉（grep 检查）
10. 手动测：
    - AI 听到"明天有考试" → 应调 `add_note(date=明天, ...)`，不应 save_memory
    - AI 听到"我讨厌语音消息" → save_memory
    - 跑一次 summary cron 手动触发，看是否正确生成 ai_summary 行
    - 前端 NotesView 能显示 user note + ai_summary（前者区分颜色，后者可能折叠）
11. Commit 分阶段：
    - `feat(db): add notes table`
    - `feat(tools): add note tools, strip notes from timeline event tools`
    - `feat(prompts): notes vs memory separation, daily summary prompt`
    - `feat(scheduler): daily summary cron`
    - `feat(api): /api/notes routes`
    - `feat(frontend): notes view`
