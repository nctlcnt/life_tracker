# Event / Notes 拆分 Plan

把当前 `events` 表里的 `notes` 字段拆出来独立成 `event_notes` 表，并把 AI 工具收紧成"过去的事件不能动"。

实施分支：`feature/role-split`（与 role split 共用，因为后续 role split 的 prompt 拆分依赖这一改动）

执行顺序：**先做这个，再做 role split 阶段 1（夜间清理）**。理由：
- 清理逻辑会针对 `events` + `event_notes` 两张表设计，先拆完省得清理工具再改一遍
- Role A 的工具说明也会因这次重构大幅简化

---

## 核心动机

**当前问题**：[bot/database.py](bot/database.py) 的 `events` 表里 `notes` 是个累积字符串字段（[bot/ai_engine_base.py:170-174](bot/ai_engine_base.py#L170-L174) 用 `existing.notes + "\n" + 新 notes` 拼接）。AI 想加一句感想 → 调 `update_timeline_event` 改 notes → 经常顺手把 content/category/project_name 也改掉。根本原因：**只有"update"这一个工具**，导致"加感想"这种纯追加操作和"修改事实"共用了同一条路径。

**心智模型转变**：
- **当前**：notes 是 "event 的属性"
- **目标**：notes 是**用户意识流的时间线**，挂到 event 上只是为了记录"你当时在做什么背景下产生了这个想法"。`event_notes` 表本质是按时间排序的流水

---

## 已确认的设计决策

### 1. add_event_note 的约束（方案 a，最严）

- `event_id` **必须**指向 ongoing event（`end_time IS NULL`）
- note.timestamp = 写入时刻（"现在"），不是用户口述的"下午 3 点"
- 只能从 prompt 里注入的【当前进行中的事件】列表里选 event_id，**不需要也不应该**额外调 query_timeline
- **AI 会乱跨 event 加 note**：如果用户说"今天下午做得不错"且当下在洗澡 → 挂到"洗澡"这个 ongoing event，note 内容是"想到下午的 X 做得不错"。**这是有意设计**：保留"什么背景下产生了这个反思"的信息

### 2. 没有 ongoing event 时怎么办

- AI 必须**先推理一个大概的 event** 调 `log_timeline_event` 创建（比如用户大半夜说"好饿"且没有 ongoing event → AI 推理"在床上 / 在客厅 / 正在准备睡觉"等创建一个事件），**再** add_note 到这个新事件
- 不允许 event_id 为 null 的"游离 note"

### 3. update_timeline_event 的约束

- **只能修改 ongoing event**（`end_time IS NULL` 的事件）
- 一旦事件已结束（end_time 已设），任何字段都不能再 update
- 唯一的例外：**关闭事件本身**（设 end_time）算合法的"最后一次 update"
- 即：从"开始"到"结束"之间的整个生命周期可以随便改；一旦结束就冻结

### 4. notes 字段去留

**完全删除** `events.notes` 字段。所有 note 都进 `event_notes` 表。
- `log_timeline_event` 的工具签名去掉 `notes` 参数。如果创建时有初始信息，要么写进 content，要么单独再调 add_event_note
- 历史数据迁移见下文

### 5. 删除"AI 的整理功能"

`update_timeline_event` 和 `delete_timeline_event` **从 AI 可调用工具列表里移除**（除了 ongoing event 的合法 update 之外）。
- AI 不能 update 已结束事件
- AI 不能 delete 任何事件
- 整理 / 去重 / 修正只由后续的**夜间清理引擎**（Role C）做。Role C 不算"AI 的日常工具"，是离线批处理

具体方案：
- 保留 `update_timeline_event` 工具，但**内部强制校验** `end_time IS NULL`，否则返回错误
- **完全删除** `delete_timeline_event` 工具（从 `bot/tools.py` 的 TOOLS_ANTHROPIC / TOOLS 列表里移除）
- DB 层的 `delete_event` 函数保留（夜间清理 / 后端 API 还会用），只是不暴露给 AI

### 6. API/前端策略（推荐方案）

API 层做 join，把每个 event 的 notes 列表打包返回：
```json
{
  "id": 123,
  "start_time": "...",
  "end_time": null,
  "content": "学 React",
  "category": "Focus",
  "project_name": "life-tracker",
  "notes": [
    {"id": 1, "timestamp": "2026-04-21T14:32:00", "content": "卡在 useEffect 死循环"},
    {"id": 2, "timestamp": "2026-04-21T15:10:00", "content": "好饿，想到下午的 X 做得不错"}
  ]
}
```
前端从读 `e.notes`（字符串）改成读 `e.notes` 数组，逐条渲染时间戳 + 内容。

---

## 数据建模

### 新表 schema

```sql
CREATE TABLE event_notes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    INTEGER NOT NULL,
    timestamp   TEXT    NOT NULL,        -- ISO 8601, 写入时刻（不是用户口述时间）
    content     TEXT    NOT NULL,
    FOREIGN KEY (event_id) REFERENCES events(id) ON DELETE CASCADE
);
CREATE INDEX idx_event_notes_event_id ON event_notes(event_id);
CREATE INDEX idx_event_notes_timestamp ON event_notes(timestamp);
```

### events 表变化

- `notes` 字段从 schema 里**删除**（迁移见下文）
- 其他字段保持不变

### 迁移脚本（一次性）

```sql
-- 1. 创建新表
CREATE TABLE event_notes (...);

-- 2. 把 events.notes 拆成 event_notes 行
--    每条 events.notes 按 \n 切分，每段成一行
--    timestamp 用 events.start_time（无更精确信息可用）
INSERT INTO event_notes (event_id, timestamp, content)
SELECT e.id, e.start_time, trim(value)
FROM events e, json_each('["' || replace(replace(e.notes, '"', '\"'), char(10), '","') || '"]')
WHERE e.notes IS NOT NULL AND trim(e.notes) != '';

-- 3. drop events.notes
ALTER TABLE events DROP COLUMN notes;
```

⚠️ SQLite 的 `ALTER TABLE DROP COLUMN` 需要 3.35+（2021 年 3 月）。如果版本不够，走 rebuild table 的老办法。

⚠️ 上面的 JSON 切分写法可能不稳，实际迁移用 Python 脚本更安全：
```python
# scripts/migrate_event_notes.py
for event in db.execute("SELECT id, start_time, notes FROM events WHERE notes IS NOT NULL"):
    for line in event["notes"].split("\n"):
        line = line.strip()
        if line:
            db.execute("INSERT INTO event_notes (event_id, timestamp, content) VALUES (?, ?, ?)",
                       (event["id"], event["start_time"], line))
```

---

## 工具签名变化

### 新增

**`add_event_note`**:
```json
{
  "name": "add_event_note",
  "description": "给当前进行中的事件追加一条时间戳备注（你的想法 / 用户的感受 / 旁白）。注意：event_id 必须是【当前进行中的事件】列表里的某一个；如果列表为空，先 log_timeline_event 创建一个再来 add。timestamp 由系统自动设为当前时间，不需要你传。",
  "input_schema": {
    "type": "object",
    "properties": {
      "event_id": {"type": "integer", "description": "ongoing event 的 ID（end_time IS NULL）"},
      "content": {"type": "string", "description": "备注内容"}
    },
    "required": ["event_id", "content"]
  }
}
```

### 修改

**`log_timeline_event`**:
- 移除 `notes` 参数
- 描述里加一句："如果有初始备注，创建后立即 add_event_note 一条"

**`update_timeline_event`**:
- 移除 `notes` 参数
- 描述里加一句："只能更新 ongoing event（end_time IS NULL）。已结束事件任何字段都不能改"
- 内部 `_execute_tool` 实现里加校验：先 query event，若 end_time 已存在则返回 `{"success": false, "message": "event 已结束，不能修改"}`

### 删除

**`delete_timeline_event`**:
- 从 `bot/tools.py` 的 `TOOLS_ANTHROPIC` 和 `TOOLS` 列表里完全移除
- `bot/ai_engine_base.py::_execute_tool` 里对应的 elif 分支移除
- DB 层的 `delete_event` 函数保留

---

## Prompt 改动（[bot/prompts.py](bot/prompts.py)）

### TOOLS_SECTION 的 Timeline 段简化

当前的"新建 vs 更新 vs 删除"逻辑大幅简化：
```
## Timeline（log / update / add_note / query）

content = 高度概括的标题（动词+宾语）。
project_name 严格优先复用【现有项目列表】。

**生命周期**：
- 新活动 → log_timeline_event。先看【当前进行中的事件】，切换场景就 update 旧的 end_time 再 log 新的；并行就保留旧的直接 log 新的
- ongoing 中 → update_timeline_event 改任何字段（content/category/end_time/project_name），只要 end_time 还是 null
- 用户提到当下感受 / 想法 / 旁白 → add_event_note 挂到 ongoing event。如果没有 ongoing，先 log 一个推理出来的事件再 add
- 已结束的事件 → 任何字段都不能改、不能加 note。要修正错误等夜间清理引擎处理

**add_event_note 的取 event_id**：
- 直接从【当前进行中的事件】列表里选最匹配的（不必 query_timeline）
- 用户口述的"下午 3 点的 X"和当下时间不符 → 挂到当下 ongoing event，note 内容写"想到下午的 X..."

**禁止**：
- 不要 update 已结束的事件（系统会拒绝）
- 不要试图删除事件（你没这个权限）
```

### LABEL_ONGOING 的 prompt 注入要带提示

[ai_engine_base.py:65-92](ai_engine_base.py#L65-L92) 的 `_format_ongoing` 输出格式已经带 `[ID=xxx]`，AI 拿 ID 没问题。考虑在 LABEL 标题里加一句："add_event_note 必须用这里的某个 ID"。

---

## API/前端改动

### [api/server.py](api/server.py)

`/api/timeline`、`/api/events` 的 SQL 查询从单表 SELECT 改成 LEFT JOIN + GROUP_CONCAT 或两次查询拼接。

推荐两次查询拼接（更易维护）：
```python
events = db.execute("SELECT * FROM events WHERE ...").fetchall()
event_ids = [e["id"] for e in events]
notes = db.execute(
    "SELECT * FROM event_notes WHERE event_id IN (?, ?, ...) ORDER BY timestamp",
    event_ids
).fetchall()
notes_by_event = defaultdict(list)
for n in notes:
    notes_by_event[n["event_id"]].append({"id": n["id"], "timestamp": n["timestamp"], "content": n["content"]})
for e in events:
    e["notes"] = notes_by_event.get(e["id"], [])
```

### 前端

`MultiLaneTimeline` / `WeekView` / `ProjectOverview` / 等组件里所有读 `event.notes` 的地方：
- 从字符串渲染改成数组渲染：`event.notes.map(n => <div>{n.timestamp}: {n.content}</div>)`
- 兼容空数组（旧 events 迁移后 notes 为 [] 是正常的）

需要 grep 一下前端里 `\.notes` 的所有出现位置，逐个改。

### [bot/merge.py](bot/merge.py)

合并相邻同 content+category 的事件时，notes 字段处理需要改：
- 当前：把多个 events 的 notes 字符串拼接
- 之后：合并查询时把 event_notes 也按时间排序合到一起返回（或前端按 event_id 列表反查）

---

## 实施步骤

### 步骤 0：DB schema + 迁移脚本
- [ ] [bot/database.py] 在 `_init_schema` 里加 `CREATE TABLE event_notes` 和索引
- [ ] [bot/database.py] 加 DB 操作方法：
  - `add_event_note(event_id, content) -> int`
  - `get_event_notes(event_id) -> list[dict]`
  - `get_notes_for_events(event_ids: list[int]) -> dict[int, list[dict]]`（API 用）
  - `delete_event_notes_by_event(event_id)`（清理用）
- [ ] [scripts/migrate_event_notes.py] 写迁移脚本：把 events.notes 切分成 event_notes 行
- [ ] 备份 DB → 跑迁移 → 校验：旧 notes 行数 vs 新 event_notes 行数对得上
- [ ] [bot/database.py] 从 events schema 删 notes 字段（迁移成功后）

### 步骤 1：工具层改造
- [ ] [bot/tools.py] 加 `add_event_note` 工具定义（OpenAI + Anthropic 两套 schema）
- [ ] [bot/tools.py] `log_timeline_event` 移除 notes 参数
- [ ] [bot/tools.py] `update_timeline_event` 移除 notes 参数
- [ ] [bot/tools.py] **完全删除** `delete_timeline_event` 工具
- [ ] [bot/ai_engine_base.py::_execute_tool] 加 `add_event_note` 分支
- [ ] [bot/ai_engine_base.py::_execute_tool] `update_timeline_event` 分支加 ongoing 校验
- [ ] [bot/ai_engine_base.py::_execute_tool] 删除 `delete_timeline_event` 分支

### 步骤 2：Prompt 更新
- [ ] [bot/prompts.py] TOOLS_SECTION 的 Timeline 段重写（按上面草稿）
- [ ] [bot/prompts.py] LABEL_ONGOING 标题里加一句"add_event_note 必须用这里的 ID"

### 步骤 3：API + 前端
- [ ] [api/server.py] `/api/timeline` `/api/events` 改成返回 notes 数组
- [ ] grep 前端所有 `\.notes` 用法，逐个改成数组渲染
- [ ] [bot/merge.py] 合并逻辑里的 notes 处理改成数组拼接

### 步骤 4：测试 + 验证
- [ ] 手动测：开一个事件 → add 几条 note → 看前端是否正确显示
- [ ] 手动测：尝试让 AI update 一个已结束事件 → 应该返回错误
- [ ] 手动测：用户说"好饿"且没有 ongoing → AI 应先 log 一个事件再 add_note
- [ ] 跑现有 API 测试 [scripts/test_api.py](scripts/test_api.py) 看有没有回归

### 步骤 5：清理
- [ ] [progress.md] 记一笔
- [ ] [.claude/CLAUDE.md] 模块表里 `bot/tools.py` 描述更新（工具数量从 9 改成 9，名字变化）
- [ ] [.claude/CLAUDE.md] events 字段描述更新（去掉 notes）
- [ ] commit 分阶段：
  - `feat(db): add event_notes table and migration`
  - `feat(tools): split event notes from timeline events`
  - `refactor(prompts): simplify timeline section after notes split`
  - `feat(api): return notes as array per event`
  - `feat(frontend): render event notes as timestamped list`

---

## 待确认（动手前最后一遍 checklist）

- [x] add_note 只能挂 ongoing event（方案 a）
- [x] 无 ongoing 时 AI 先推理 log 一个事件再 add
- [x] update_timeline_event 只能改 ongoing
- [x] delete_timeline_event 从 AI 工具列表完全移除
- [x] events.notes 字段完全删除
- [x] API 返回 notes 数组
- [ ] notes 字段排序：按 timestamp asc 还是 desc？（推荐 asc，符合"流水"心智模型）
- [ ] 前端 notes 数组渲染样式（推荐每条独立一行 + 小字时间戳）
- [ ] 是否需要 `update_event_note` / `delete_event_note` 工具给 AI？（推荐**否**：note 是流水，写错了就再写一条新的纠正；删除留给夜间清理）

---

## 与 role split 的衔接

这次重构完成后：
- **Role A**（时间助手）的工具集变成：log_timeline_event / update_timeline_event（限 ongoing）/ add_event_note / query_timeline / set_reminder / list_reminders / cancel_reminders / save_memory / update_memory / delete_memory / add_deadline / complete_deadline / delete_deadline。共 12 个，去掉了 delete_timeline_event
- **Role B**（拖延助力人格）只保留 read-only：query_timeline / list_reminders。需要写时调 Role A 转交（如何转交是 role split 阶段的话题）
- **Role C**（夜间清理）拥有所有工具 + 直接 DB 访问权限（清理过去的 events 和 notes）
