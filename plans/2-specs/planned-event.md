# Plan: 在 timeline events 上引入 planned 状态作为 dummy event

## Context

原先计划把 `deadlines` + `appointments` 合成 `schedule` 表。重新思考后：

- **deadline 保留**——"要完成的任务 + 倒计时"这个抽象独立有用，继续独立存在。
- **appointment 不做**——它本质就是"未来某个时间点的一个 event"，可以用 `events` 表 + `status='planned'` 标记"还没真发生的虚 event"一步到位。

**分支状态说明**（2026-04-22 重写 plan 时核查）：
- 本分支 `feature/planned-event` 从 `main` 切出。main 上后端**从未有过** `appointments` 表、CRUD、tools 或 API endpoint——所以无需"废除"。
- 但 main 前端 `App.tsx` / `ItemList.tsx` 里有历史遗留：fetch `/api/appointments`（404）、定义了 `type='appointment'` 分支、UI panel 叫"已安排"。这是旧 plan 迭代中前端先跑一步、后端没跟上的残迹，**需要清理掉**。
- `feature/role-split` 分支上倒是真实现了完整的 appointment 后端（ce9c702），但**那条线就此放弃**，不合入 main。

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

迁移位置：`bot/database.py::_init_tables` 末尾追加 `try: conn.execute("ALTER TABLE events ADD COLUMN status TEXT"); except sqlite3.OperationalError: pass`（沿用现有幂等模式，line 93 起那一串）。

本分支没有 appointments 表——main 从未建过。无 DROP 动作。

---

## 2. Tool 层变化

main 上 tools.py **没有** appointment 工具，不用删。

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
  - OpenAI schema（line 12 起 `TOOLS`）：`log_timeline_event` 加 `status` 字段；加 `cancel_planned_event` 一段
  - Anthropic schema（对应位置）：同步相同改动
  - **不改** `SET_TOOL_NAMES` / `POLL_TOOL_NAMES` / `REMINDER_TOOL_NAMES`：
    - `SET_TOOL_NAMES` 仅供 discord_bot.py 判断"是否加 ✅ 反应"——语义是"新建型工具"。`cancel_planned_event` 是取消不是新建，不加。
    - 另两个集合 `ai_engine_base.py:311` 注释明确说"tool_names 不再过滤，chat/poll 共用全量 tools"，实际已无约束效果。`log_timeline_event` 本就不在这些集合里，`cancel_planned_event` 同样不加。
- `bot/ai_engine_base.py::_execute_tool`：
  - `log_timeline_event` 分支透传新 `status` 参数给 `db.add_event`
  - 新增 `cancel_planned_event` 分支调用 `db.cancel_planned_event(event_id)`，按 bool 返回 `{"success": True/False, "message": ...}`（对 non-planned / 不存在的 id 返回 False + 提示，参照现有 `complete_deadline` / `delete_reminder` 模式）

---

## 3. Prompt 层变化

**`bot/prompts.py`**（main 上没有 appointment 引用，只做加法 + 部分段落加 planned 说明）：

- 概念边界块：在 `log_timeline_event` / timeline event 描述里补一句："未来要发生的到场型安排（看牙、泡澡、约饭等）也用 `log_timeline_event` 记录，额外传 `status='planned'`——它是 timeline 上的 dummy 条目，和真实事件并行不冲突。"

- `TOOLS_SECTION`：`## Timeline` 段里加一个 "Planned event" 子段：
  - 何时用：她告诉你未来要去做/参加某事，你想帮她记下
  - category 照常必填（看牙 = Routine，约朋友喝咖啡 = Chill，预约学习小组 = Focus + project_name）
  - **禁止混用**：真实已发生的 event 绝对不加 `status`；未来 event 必须加 `status='planned'`
  - cancel：她说"算了不去了" / "取消" → 调 `cancel_planned_event`（不是删——痕迹保留）
  - 时间过了怎么办：不做任何操作，不调 log_timeline_event 补"真实版"，除非她主动汇报"去了"/"没去"

- 新增 `LABEL_PLANNED = "【未来安排（planned events）】"` + `_format_planned_events`：
  ```
  - [id={id}] {start_time}[→ {end_time}] {category}[/{project_name}] | {content} | {countdown}[ | {notes}]
  ```
  `format_countdown` 复用不变
- `PromptParts` 加字段 `planned_events`（Block 4）
- `build_prompt` signature：加 `planned_events=`
- cancelled planned events **不注入** prompt（避免 Block 4 膨胀，AI 无需反复感知）——只注入 `status='planned'` 的

**`bot/ai_engine_base.py::_build_prompt`**：
- 新增 `db.get_planned_events()`（status='planned'，按 start_time 升序；不含 cancelled），传给 `build_prompt(..., planned_events=...)`
- deadlines 相关部分**不动**

---

## 4. API 层变化（关键）

`/api/timeline` 响应从 `{segments, count}` 扩展成：

```json
{
  "segments": [...],         // 只含 status IS NULL 的 merge 后段
  "planned_events": [...],   // status='planned'，原始 event 结构，不合并
  "cancelled_events": [...], // status='cancelled'，原始 event 结构，不合并
  "count": <segments count>
}
```

这样前端一个 fetch 拿到三组，各自渲染不同样式。

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

- `_init_tables` 末尾追加幂等 `ALTER TABLE events ADD COLUMN status TEXT`（沿用现有 try/except OperationalError 模式）
- `add_event(...)` 加可选 `status=None` 参数，INSERT 时写入
- 新增：
  - `cancel_planned_event(event_id) -> bool`：仅当 `status='planned'` 时更新为 `cancelled`
  - `get_planned_events() -> list[dict]`：`WHERE status='planned' ORDER BY start_time ASC`
- `get_events(...)`、`get_event_by_id(...)`：SELECT * 已自动带出 status，无需改
- **不改** `update_event` 的 allowed 集合——目前没有工具需要改 status，等真有需求再加

---

## 6. 前端变化

**`frontend/src/app/App.tsx`**：
- 删 `appointments` state、fetch `/api/appointments`、对应 `ItemList` panel——main 上这些都是 404 的历史遗留
- deadlines panel 不动
- 腾出来的 panel 格位：留空，不填新东西（不在本次 scope）
- 现有 `/api/timeline` 会自动带出新的 status 字段，无需新 endpoint

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

- `bot/scheduler.py`：无改动。planned event 不触发推送——AI 需要到点提醒就自己 `set_reminder`。
- `bot/merge.py`：**不改**。`merge_events` 本就只保留固定字段、不关心 status。过滤发生在 `api/server.py` 的 `/api/timeline` handler——只把 `status IS NULL` 的 raw event 送进 merge，planned / cancelled 原样直通返回。
- `api/server.py::get_projects_heatmap`：过滤掉 `status is not None` 的行——planned Focus 不应计入真实时长
- `.claude/CLAUDE.md`（main 版本——无 appointment 行）：
  - 术语表 `event` 行加一句："`status` 可空：NULL=已发生；`planned`=未来虚 event（timeline 上的 dummy，到场型安排走这个）；`cancelled`=取消的 planned（仍展示）"
  - 原则段/模块表如有 "有时间点的安排必须入 deadline" 之类表述，补 "或带 planned 状态的 timeline event"
  - `bot/tools.py` 模块行描述：补 "cancel_planned_event；log_timeline_event 支持 status='planned'"
  - 动态注入段：加【未来安排（planned events）】，数据源 events 表 status='planned'

---

## 8. Commit 阶段

分 3 个 commit，每个都保证 app 可启动：

1. **`feat(events): add planned status for dummy future events`**
   - `bot/database.py`：events 表 ALTER + add_event 加 status + 新 CRUD（cancel_planned_event / get_planned_events）
   - `bot/tools.py`：log_timeline_event 加 status 参数 + 新 cancel_planned_event（白名单不改）
   - `bot/ai_engine_base.py`：`_execute_tool` 新分支 + `_build_prompt` 注入 planned_events
   - `bot/prompts.py`：新 LABEL_PLANNED + _format_planned_events + PromptParts.planned_events + build_prompt + TOOLS_SECTION 补 Planned event 子段
   - `api/server.py`：/api/timeline 过滤 status IS NULL 走 merge，planned/cancelled 直通；/api/projects/heatmap 过滤 status is not None
   - `.claude/CLAUDE.md`：术语表 / 动态注入段

2. **`feat(frontend): render planned and cancelled events on timeline`**
   - `App.tsx`（清理 /api/appointments 残迹）、`ItemList.tsx`（删 appointment 分支）、`MultiLaneTimeline.tsx`（三状态视觉区分，核心）、`WeekView.tsx`（如有 appointment 引用清理）

3. **`docs: retire plan-future-timeline.md`**
   - 删 `plans/plan-future-timeline.md`

---

## 9. 验证

1. 停服、备份 DB
2. 起服。日志无 error；`sqlite3 ... "PRAGMA table_info(events);"` 确认多了 status 列
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
bot/database.py                              # events 加 status + add_event 加 status + 新 CRUD
bot/tools.py                                 # log_timeline_event 加 status + cancel_planned_event
bot/ai_engine_base.py                        # _execute_tool + _build_prompt
bot/prompts.py                               # LABEL_PLANNED / _format_planned_events / PromptParts / TOOLS_SECTION
api/server.py                                # /api/timeline 过滤 + heatmap 过滤
frontend/src/app/App.tsx                     # 清理 /api/appointments 残迹
frontend/src/app/components/ItemList.tsx     # 删 appointment type 分支
frontend/src/app/components/MultiLaneTimeline.tsx   # 核心新视觉：三状态区分
frontend/src/app/components/WeekView.tsx     # 若有 appointment 引用则清理
.claude/CLAUDE.md                            # 术语表 / 动态注入段
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
