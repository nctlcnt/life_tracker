# Plan: 废掉 appointment，把"到场型安排"合并进 timeline events 作为 dummy event

## Context

原先计划把 `deadlines` + `appointments` 合成 `schedule` 表。重新思考后：

- **deadline 保留**——"要完成的任务 + 倒计时"这个抽象独立有用，继续独立存在。
- **appointment 废除**——它本质就是"未来某个时间点的一个 event"，完全可以复用 `events` 表，只是多一个 `status='planned'` 标记区分"还没真发生的虚 event"。

**核心语义**：planned 是 timeline 里的 dummy event（虚影），和真实 event 在同一时间轴上并行存在、互不影响：
- 不自动转 actual（时间过了也不转）
- 与实际事件无数据关联（计划看 2h ≠ 真发生 2h，两条独立记录）
- category / project_name 与真实 event **完全对称**（必填、同一套枚举），以便将来接精力管理系统
- 前端在同一 timeline 上用不同视觉区分三种状态

这个 plan **取代** `plans/plan-future-timeline.md`。

**已锁死的设计约束**（用户已确认，不重新讨论）：
1. `events.status` 可空：`NULL` = 已发生真实 event（现有全部数据）；`planned` = 未来 dummy；`cancelled` = 取消的 planned（仍显示）。
2. planned **永远不自动转** actual——过时间就继续是 planned，显示位置随时间轴滚过"现在"线。
3. `category` / `project_name` 约束和真实 event 一致（Focus 必填 project_name）。
4. AI 只创建 planned / cancel planned；**硬删除只能在前端操作**。
5. reminder 完全解耦——AI 如需到点提醒，自行 `set_reminder`，不和 planned event 绑定。
6. deadline 表 + 工具 + API + 前端 panel **全部不动**。
7. reminder 保持独立触发器定位**不动**。
8. 不与 `plan-event-notes-split.md` 纠缠——那是同分支独立工作。

---

## 1. Schema 改动：`events` 加 `status` 列

```sql
ALTER TABLE events ADD COLUMN status TEXT;
-- NULL        = 已发生真实 event（默认）
-- 'planned'   = 未来 dummy event
-- 'cancelled' = 已取消的 planned（仍保留展示）
```

老数据**不做任何迁移**——`status` 直接 NULL，语义上等同"已发生"。

`appointments` 表**废弃**，做一次性清除：`DROP TABLE IF EXISTS appointments;`（单用户 + 已有 `life_tracker.db.bak-*` 备份，无需迁移数据进 events，appointment 原有数据就让它消失）。

迁移位置：`bot/database.py::_init_tables` 末尾加幂等 `ALTER` + `DROP`（用 `PRAGMA table_info(events)` 判断 status 列是否已存在）。

---

## 2. Tool 层变化

**删 3 个 appointment 工具**：`add_appointment` / `list_appointments` / `delete_appointment`

**`log_timeline_event` 加可选 `status` 参数**：

```json
"status": {
    "type": "string",
    "enum": ["planned"],
    "description": "仅当记录的是未来虚 event（到场型安排，比如约看牙、泡澡、和朋友喝咖啡）时填 'planned'。真实发生过的 event 不要填。"
}
```

注意 enum 只允许 `planned`——`cancelled` 由另一工具负责、NULL 由省略该字段实现。

**新增 `cancel_planned_event` 工具**：

```json
{
    "name": "cancel_planned_event",
    "description": "取消一个尚未发生的 planned event（她说不去了 / 改主意了）。事件仍保留在 timeline 上供她回看，但标为 cancelled。只对 status='planned' 的 event 有效。",
    "parameters": {
        "event_id": {"type": "integer", "description": "要取消的 planned event id"}
    },
    "required": ["event_id"]
}
```

**不提供 AI 侧的"删除"工具**——删除只在前端。

### 改动文件

- `bot/tools.py`:
  - OpenAI schema（约 line 12 起 `TOOLS`）：`log_timeline_event` 加 `status` 字段；删 `add_appointment` / `list_appointments` / `delete_appointment` 三段；加 `cancel_planned_event` 一段
  - Anthropic schema（约 line 631 起 `TOOLS_ANTHROPIC`）：同步相同改动
  - `SET_TOOL_NAMES`（chat-mode 白名单）：去掉 `add_appointment` / `delete_appointment`，加 `cancel_planned_event`（`log_timeline_event` 已在）
  - `POLL_TOOL_NAMES`：去掉 appointment 三件套，加 `cancel_planned_event`
  - `TOOL_POST_HINTS`（如有 appointment 相关键）：清理
- `bot/ai_engine_base.py::_execute_tool`（约 line 124 起）：
  - 删 appointment 三件套分支
  - `log_timeline_event` 分支透传新 `status` 参数给 `db.log_event`
  - 新增 `cancel_planned_event` 分支调用 `db.cancel_planned_event`

---

## 3. Prompt 层变化

**`bot/prompts.py`**：

- 概念边界块（约 line 109-112）：
  - 删 `appointment` 那条
  - 在 `log_timeline_event` / timeline event 描述里补一句："未来要发生的到场型安排（看牙、泡澡、约饭等）也用 `log_timeline_event` 记录，额外传 `status='planned'`——它是 timeline 上的 dummy 条目，和真实事件并行不冲突。"

- `TOOLS_SECTION`（约 line 250-268）：
  - 删 `## Appointment` 整段
  - `## Timeline` 段里加一个 "Planned event" 子段：
    - 何时用：她告诉你未来要去做/参加某事，你想帮她记下
    - category 照常必填（看牙 = Routine，约朋友喝咖啡 = Chill，预约学习小组 = Focus + project_name）
    - **禁止混用**：真实已发生的 event 绝对不加 `status`；未来 event 必须加 `status='planned'`
    - cancel：她说"算了不去了" / "取消" → 调 `cancel_planned_event`（不是删——痕迹保留）
    - 时间过了怎么办：不做任何操作，不调 log_timeline_event 补"真实版"，除非她主动汇报"去了"/"没去"

- `LABEL_APPOINTMENTS`（line 398）+ `_format_appointments`（line 490）+ `PromptParts.appointments` 字段：**删**
- 新增 `LABEL_PLANNED = "【未来安排（planned events）】"` + `_format_planned_events`：
  ```
  - [id={id}] {start_time}[→ {end_time}] {category}[/{project_name}] | {content} | {countdown}[ | {notes}]
  ```
  `format_countdown` 复用不变
- `PromptParts` 加字段 `planned_events`（Block 4）
- `build_prompt` signature：去掉 `appointments=`，加 `planned_events=`
- 其他静态段落清理（grep `appointment` 全部位置并替换/删除）：
  - line 127（COMMUNICATION 若有提）
  - line 210（批量规划示例若有提）
  - line 603（PROACTIVE_PROMPT 若有提）
  - line 625（MORNING_PROMPT 若有提）
- 有没有提"cancelled planned events"要让 AI 看到？**不注入**——cancelled 的给前端看，AI 不需要反复感知（避免 Block 4 膨胀）。只注入 `status='planned'` 的。

**`bot/ai_engine_base.py::_build_prompt`**（约 line 65-97）：
- 删 `mark_past_appointments() + get_active_appointments()` 组
- 新增 `db.get_planned_events()`（status='planned'，按 start_time 升序；不含 cancelled），传给 `build_prompt(..., planned_events=...)`
- deadlines 相关部分**不动**

---

## 4. API 层变化

`api/server.py`：

- 删 `/api/appointments`（约 line 128-140）
- `/api/events` 和 `/api/timeline`：确保返回 `status` 字段，**不过滤任何 status**（三种一起返回，前端分视觉）
- `/api/deadlines` 不动
- `bot/merge.py::merge_events`：检查是否按 content+category 合并——planned event 也可能被错误合并进相邻真实 event。
  - **规则**：只合并 `status IS NULL` 的行；planned / cancelled 绝不参与合并
  - 在 `/api/timeline` 的合并入口前过滤一次：planned + cancelled 走直通返回，只有 NULL 的进 merge 逻辑，最后三组拼回

---

## 5. Database 层变化

`bot/database.py`：

- `log_event(...)` 加可选 `status=None` 参数，INSERT 时写入
- 删 appointment 相关的所有 CRUD：`add_appointment` / `get_active_appointments` / `mark_past_appointments` / `delete_appointment`
- 新增：
  - `cancel_planned_event(event_id) -> bool`：仅当 `status='planned'` 时更新为 `cancelled`
  - `get_planned_events() -> list[dict]`：`WHERE status='planned' ORDER BY start_time ASC`
- `get_events(...)` / timeline 相关查询：确认 SELECT `status` 字段一起返回

---

## 6. 前端变化

**`frontend/src/app/App.tsx`**：
- 删 `appointments` state + fetch + `ItemList` panel
- deadlines panel 不动
- 腾出来的 panel 格位（原 appointments）：**不填新东西**（留白 / 或后续给 RhythmView 用）——不在本次 scope 内决定
- timeline 相关 fetch 只要现有 `/api/timeline` 能带出 status 就行，无需新 endpoint

**`frontend/src/app/components/ItemList.tsx`**：
- 删 `type='appointment'` 分支和对应 icon

**`frontend/src/app/components/MultiLaneTimeline.tsx`**（核心视觉改动）：
- event 块渲染根据 `status` 字段分 3 种样式：
  - `status == null`（真实 event）：现状不变（实心彩色块）
  - `status === 'planned'`：同位置、同尺寸，**虚线 border + 填充半透明 / 白色 / opacity 0.35**，文字照常
  - `status === 'cancelled'`：**灰色虚线 border + 不填充（透明背景）**，文字灰色 / 删除线可选
- 时间定位逻辑（`timeToRatio`）完全复用，不区分 status
- tooltip / 点击交互如有，planned/cancelled 文案后缀"(计划)"/"(已取消)"

**`frontend/src/app/components/WeekView.tsx`**：
- 检查有没有引用 appointment——如果 `DayCard` 有 appointment 标记就去掉。deadline 标记不动。
- 周视图的 day 块里，planned event 要不要显示？**本次不加**，保持现状，留作后续。

---

## 7. 其他模块影响

- `bot/scheduler.py`：不引用 appointment，无改动。planned event 不触发推送——AI 需要到点提醒就自己 `set_reminder`。
- `bot/merge.py`：**需要改**（见 §4 末段，只对 status IS NULL 做合并）
- `.claude/CLAUDE.md`：
  - 术语表：
    - `appointment` 整行**删除**
    - `event` 行加一句："`status` 可空：NULL=已发生；`planned`=未来虚 event（timeline 上的 dummy，到场型安排走这个）；`cancelled`=取消的 planned（仍展示）"
  - 原则段："memory 只存'关于她'的信息，有时间点的安排必须入 deadline / appointment" → "…必须入 deadline / 带 planned 状态的 timeline event"
  - 模块表：
    - `bot/tools.py` 行描述：删 "含 appointment 三件套"，改 "含 cancel_planned_event；log_timeline_event 支持 status='planned'"
    - `api/server.py` 行：去掉 `/api/appointments`
    - `bot/database.py` 行：events 表字段描述加 status；删 appointments 表
  - Block 4 描述：`ongoing + deadlines + appointments + weather` → `ongoing + deadlines + planned_events + weather`
  - 动态注入段：【已安排的事项】那条改成【未来安排（planned events）】并说明数据源从 events 表 status='planned' 过滤

---

## 8. Commit 阶段

分 3 个 commit，每个都保证 app 可启动：

1. **`feat(db): add status column to events, retire appointments table`**
   - `bot/database.py`：events 表 ALTER + DROP appointments + log_event 加 status + 新 CRUD + 删旧 appointment CRUD
   - `bot/tools.py`：log_timeline_event 加 status 参数 + 新 cancel_planned_event + 删 appointment 三件套 + 白名单
   - `bot/ai_engine_base.py`：`_execute_tool` 分支 + `_build_prompt` 数据源切换
   - `bot/prompts.py`：format/label/PromptParts/build_prompt + 静态段落清理
   - `bot/merge.py`：只合并 status IS NULL 的行
   - `api/server.py`：删 `/api/appointments`、确保 timeline/events 带 status
   - `.claude/CLAUDE.md`：术语表 / 模块表 / Block 4 / 动态注入段

2. **`feat(frontend): render planned and cancelled events on timeline`**
   - `App.tsx`、`ItemList.tsx`、`MultiLaneTimeline.tsx`（三状态视觉区分，核心）、`WeekView.tsx`（清理 appointment 引用）

3. **`docs: retire plan-future-timeline.md`**
   - 删 `plans/plan-future-timeline.md`

---

## 9. 验证

1. 停服、备份 DB。`sqlite3 data/life_tracker.db "SELECT COUNT(*) FROM appointments;"` 记录旧行数
2. 起服。日志无 error；`sqlite3 ... "PRAGMA table_info(events);"` 确认多了 status 列；`SELECT name FROM sqlite_master WHERE type='table' AND name='appointments';` 为空
3. 既有 events 全部 `status IS NULL`，前端 timeline 显示和之前一模一样
4. `curl localhost:8000/api/deadlines` 不受影响
5. `curl localhost:8000/api/timeline` 返回字段含 status
6. Discord：「明天下午 3 点看牙」→ AI 调 `log_timeline_event` 带 `status='planned'`、category='Routine'、未来 start_time → DB 验证行存在
7. 前端 timeline：planned 事件显示为虚线半透明块，位置在未来
8. Discord：「不去看牙了」→ AI 调 `cancel_planned_event` → 行 status 变 cancelled → 前端变灰色虚线空心块
9. Discord：「周一前交 Spark 作业」→ AI 调 `add_deadline`（deadline 仍走独立通道），不走 planned event
10. 确认下一次 prompt Block 4 有【未来安排（planned events）】段，cancelled 的不出现
11. merge 测试：手动插一条 status='planned' 的事件和相邻真实事件 content 相同 → `/api/timeline` 不合并

---

## 关键文件清单

```
bot/database.py                              # events 加 status + 迁移 + 新 CRUD + 删旧
bot/tools.py                                 # log_timeline_event 加 status + cancel_planned_event + 删旧
bot/ai_engine_base.py                        # _execute_tool + _build_prompt
bot/prompts.py                               # label / _format_planned_events / PromptParts / 静态段落
bot/merge.py                                 # 只合并 status IS NULL
api/server.py                                # 删 /api/appointments、events/timeline 带 status
frontend/src/app/App.tsx
frontend/src/app/components/ItemList.tsx
frontend/src/app/components/MultiLaneTimeline.tsx   # 核心新视觉：三状态区分
frontend/src/app/components/WeekView.tsx
.claude/CLAUDE.md                            # 术语表 / 模块表 / Block 4 / 动态注入段
plans/plan-future-timeline.md                # 最终 commit 删除
```

## 超出范围

- `RhythmView.tsx` 接真实数据
- `event_notes` 独立表（见 `plan-event-notes-split.md`）
- `plan-role-split.md` 的 Role A/B/C 拆分
- reminder 与 planned event 的耦合（明确保留独立）
- 周视图里 planned event 的标记
- planned event 过时间后的任何自动化处理
- AI 主动规划能力（原 plan-future-timeline 想做的）
