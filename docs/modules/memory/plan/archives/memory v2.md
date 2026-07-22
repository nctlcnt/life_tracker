# Plan v2 — Deadline-first 小助理的 Living Memory + Agent Audit 架构

## 0. 核心目标

当前 bot 的主要价值不是“无限聊天记忆”，而是：

1. 准确追踪 deadline、reminder、timeline；
2. 在聊天中识别哪些信息值得记录；
3. 保持朋友感和个性化连续性；
4. 能审计 AI 今天有没有漏记、错记、乱用工具；
5. 让旧状态自然过期，不让过时 memory 永远定义用户。

因此 v2 不再把重点放在“滚动压缩完整对话”，而是改成：

> 硬事实进 ledger，软状态进 living memory，AI 行为进 audit log，reflection 只改进工具使用和互动方式。

---

## 1. 总体架构

系统分成四层。

### Layer 1：Conversation Log

保存原始对话和 assistant 回复，是所有后续提取、审计、回放的 source of truth。

用途：

* 回放当天聊天；
* 给 memory extractor / deadline extractor 提供原始材料；
* 给 auditor 检查 AI 是否漏用工具；
* 未来可重新生成 memory。

保留现有 `conversation_messages`，但需要补强：

* user 消息落库；
* assistant 逻辑回复落库；
* assistant 回复如果被 Discord 拆成多个 chunk，`conversation_messages` 里仍然只存一条完整逻辑回复；
* metadata 里记录 Discord message ids、引用、附件、发送状态等。

---

### Layer 2：State Ledger

这是 deadline / timeline / reminder 的权威状态层。

所有时间、deadline、提醒、课程安排、timeline 事件，都必须进入结构化 ledger，不能靠 memory summary 作为权威来源。

核心原则：

> AI 可以负责识别和提议，但最终状态必须由工具写入数据库，并且有 source evidence。

建议包含：

* `deadlines`
* `deadline_events`
* `timeline_items`
* `timeline_events`
* `reminders`
* `deadline_candidates`

其中 `deadline_candidates` 用于处理不确定信息。比如用户说“老师好像把 quiz 延到下周三了”，AI 不应该直接覆盖旧 deadline，而应该先创建 candidate，标记 confidence / tentative / needs_confirmation。

---

### Layer 3：Living Memory

这是软记忆层，不保存 deadline 权威事实，而保存用户当前状态、偏好、open loops、未开始事项、互动偏好。

它不是“对话摘要”，而是“有生命周期的个人状态记忆”。

适合保存：

* 用户最近在推进什么项目；
* 用户想做但还没开始的事情；
* 用户近期状态，例如压力大、睡眠不好、最近不太开心；
* 用户偏好的回答方式；
* 用户反复表达的喜好；
* 用户对 bot 的期待；
* 当前 open loops。

不适合保存：

* 已经进入 deadline ledger 的具体 due date；
* 一次性闲聊；
* 模型自行推断出的性格标签；
* 过时状态；
* 没有未来用途的碎片信息。

Living Memory 的关键不是“越长越好”，而是：

> active memory 要贴近当前用户状态，过期 memory 要 fading / archived / superseded。

---

### Layer 4：Agent Audit + Reflection

这是行为审计层。

它不直接修改 deadline 或 memory，而是检查：

* AI 有没有漏掉应该调用的工具；
* AI 回复里有没有承诺“我记下了”，但实际没落库；
* deadline / reminder 是否存在明显矛盾；
* timeline 是否遗漏；
* 用户体验上是否太啰嗦、太主动、太少确认；
* 哪些行为策略应该反哺进 prompt。

Reflection 不是“AI 自我忏悔日记”，而是操作规程补丁。

例如：

* 用户提到“改到 / 延期 / 下周 / due”时，应优先调用 deadline candidate 工具；
* 如果回复中承诺提醒，必须确认 reminder 工具调用成功；
* 对不确定时间要标记 tentative，不要静默写成 confirmed；
* 用户偏好先给结论，再解释。

---

## 2. 数据库设计

### 2.1 `conversation_messages`

已有表保留，但需要确认字段足够表达 source truth。

建议字段：

```sql
id INTEGER PRIMARY KEY
channel_id TEXT NOT NULL
discord_message_id TEXT
role TEXT NOT NULL              -- user / assistant / system
content TEXT NOT NULL
created_at TEXT NOT NULL
metadata_json TEXT
```

建议新增唯一约束：

```sql
UNIQUE(channel_id, discord_message_id)
```

assistant 回复如果被拆 chunk，`content` 存完整逻辑回复，`metadata_json` 存实际发送的 Discord message ids。

---

### 2.2 `ai_runs`

每次 AI 调用记录一条。

```sql
id INTEGER PRIMARY KEY
run_id TEXT UNIQUE NOT NULL
channel_id TEXT NOT NULL
user_message_id INTEGER
trigger_type TEXT              -- user_message / reminder_tick / bedtime / audit / memory_extract
model TEXT
prompt_version TEXT
started_at TEXT
finished_at TEXT
status TEXT                    -- success / failed / cancelled
input_token_count INTEGER
output_token_count INTEGER
metadata_json TEXT
```

用途：

* 知道 AI 为什么被调用；
* 关联 tool calls、assistant messages、state changes；
* 给 daily auditor 做审计。

---

### 2.3 `tool_calls`

每次工具调用记录一条。

```sql
id INTEGER PRIMARY KEY
run_id TEXT NOT NULL
tool_name TEXT NOT NULL
arguments_json TEXT NOT NULL
result_json TEXT
status TEXT                    -- success / failed / skipped
error TEXT
started_at TEXT
finished_at TEXT
side_effect_type TEXT           -- read / write / notify / external
created_state_change_id INTEGER
```

用途：

* 检查 AI 是否调用了正确工具；
* 检查工具参数；
* 检查回复承诺和实际工具行为是否一致。

---

### 2.4 `assistant_messages`

记录 AI 最后发给用户的内容。

```sql
id INTEGER PRIMARY KEY
run_id TEXT NOT NULL
channel_id TEXT NOT NULL
content TEXT NOT NULL
discord_message_ids_json TEXT
created_at TEXT
send_status TEXT
```

用途：

* auditor 对照“AI 说了什么”和“AI 实际做了什么”；
* 检查是否出现“我记下了”但没有工具调用。

---

### 2.5 `state_changes`

所有结构化状态变化都进入账本。

```sql
id INTEGER PRIMARY KEY
run_id TEXT
entity_type TEXT               -- deadline / timeline / reminder / memory
entity_id INTEGER
change_type TEXT               -- create / update / delete / confirm / archive / supersede
before_json TEXT
after_json TEXT
source_message_id INTEGER
source_tool_call_id INTEGER
created_at TEXT
```

用途：

* 回溯一个 deadline 为什么变成现在这样；
* 审计 AI 是否错误更新状态；
* 支持未来 rollback / replay。

---

### 2.6 `deadline_candidates`

用于处理不确定 deadline 信息。

```sql
id INTEGER PRIMARY KEY
channel_id TEXT NOT NULL
source_message_id INTEGER
candidate_type TEXT            -- create / update / cancel / complete
title TEXT
course_code TEXT
due_at TEXT
timezone TEXT
confidence REAL
status TEXT                    -- pending / applied / rejected / needs_confirmation
reason TEXT
created_at TEXT
resolved_at TEXT
```

原则：

* 高置信度可以自动 applied；
* 中低置信度进入 pending / needs_confirmation；
* 不要让 AI 静默覆盖重要 deadline。

---

### 2.7 `personal_memories`

正式 living memory。

```sql
id INTEGER PRIMARY KEY
channel_id TEXT NOT NULL
memory_type TEXT               -- preference / current_state / open_loop / project_intent / interaction_style / temporary_context
content TEXT NOT NULL
status TEXT                    -- active / fading / archived / superseded / rejected
confidence REAL
importance INTEGER
first_seen_at TEXT
last_seen_at TEXT
valid_from TEXT
valid_until TEXT
review_after TEXT
source_message_ids_json TEXT
evidence_count INTEGER
superseded_by_id INTEGER
created_at TEXT
updated_at TEXT
```

核心原则：

* 只有 active memory 进入 prompt；
* fading 可选进入 prompt，但权重低；
* archived 不进入 prompt；
* superseded 只保留历史，不代表当前状态。

---

### 2.8 `memory_candidates`

每日 extractor 提取候选，不直接写正式 memory。

```sql
id INTEGER PRIMARY KEY
channel_id TEXT NOT NULL
candidate_type TEXT
content TEXT NOT NULL
confidence REAL
importance INTEGER
source_message_ids_json TEXT
suggested_memory_type TEXT
suggested_valid_until TEXT
suggested_review_after TEXT
status TEXT                    -- pending / accepted / rejected / merged
created_by TEXT                -- daily_extractor / auditor / user / manual
created_at TEXT
```

用途：

* 避免一次闲聊污染长期 memory；
* 让 weekly curator 合并、去重、接受或拒绝候选；
* 方便 debug 为什么保存了某条 memory。

---

### 2.9 `audit_findings`

daily auditor 的发现。

```sql
id INTEGER PRIMARY KEY
audit_date TEXT NOT NULL
channel_id TEXT NOT NULL
finding_type TEXT              -- missed_tool / possible_error / behavior / inconsistency
severity TEXT                  -- low / medium / high
content TEXT NOT NULL
evidence_json TEXT
suggested_action_json TEXT
status TEXT                    -- open / accepted / rejected / auto_fixed
created_at TEXT
resolved_at TEXT
```

用途：

* 保存 AI 每天发现的问题；
* 第二天可以向用户确认；
* 高风险发现不自动修改数据库。

---

### 2.10 `behavior_adjustments`

进入 prompt 的短行为调整。

```sql
id INTEGER PRIMARY KEY
channel_id TEXT NOT NULL
content TEXT NOT NULL
source_audit_id INTEGER
priority INTEGER
status TEXT                    -- active / archived
valid_until TEXT
created_at TEXT
updated_at TEXT
```

例如：

```text
用户非常重视 deadline 准确性。涉及日期、due、提醒时，优先使用工具，不要只靠聊天记忆。
```

---

## 3. Memory 整理流程

v2 不做“日摘要 → 周摘要 → 月摘要”的无脑压缩，而做：

> 每日 extract，每周 curate，每月 decay。

---

### 3.1 每日：Memory Extractor

每天从当天 conversation_messages 中提取 memory candidates。

输入：

* 当天 user / assistant 消息；
* 当天 tool_calls；
* 当天 state_changes；
* 已有 active memories。

输出：

* 新 memory candidates；
* 可能需要强化的旧 memory；
* 可能过期的旧 memory；
* 不应保存的碎片信息。

每日 extractor 不直接改 `personal_memories`，只写 `memory_candidates`。

提取原则：

保存：

* 反复出现的偏好；
* 明确表达的未来计划；
* 想做但还没做的 open loop；
* 当前状态；
* 对 bot 的使用偏好。

不保存：

* deadline 权威事实；
* 单次闲聊；
* 无未来用途的情绪碎片；
* 模型脑补的人格判断。

---

### 3.2 每周：Memory Curator

每周处理 `memory_candidates`：

* 合并重复候选；
* 强化已有 memory；
* 接受重要候选进入 `personal_memories`；
* 拒绝碎片候选；
* 生成更清晰、更稳定的 open loop / current state。

例如：

多个候选：

```text
用户在考虑改 bot memory。
用户担心聊天记忆影响 deadline 准确性。
用户想加 agent audit log。
```

可以合并成：

```text
用户正在设计一个带朋友感的 deadline assistant，当前重点是把 deadline ledger、living memory 和 agent audit 分离，以提高时间追踪准确性。
```

---

### 3.3 每月：Memory Decay / Archive

每月检查 active memories：

* 有没有新证据支持；
* 是否已经过期；
* 是否被新状态替代；
* 是否应该 fading；
* 是否应该 archived；
* 是否需要向用户确认。

例如：

```text
用户最近一段时间不太开心。
```

如果后续没有再出现类似证据，可以从 active → fading。
如果用户明确说最近好多了，可以 supersede。
如果长期不相关，可以 archive。
archived memory 不进入 prompt。

---

## 4. Deadline / Timeline 处理流程

deadline 和 timeline 不走 living memory。

### 4.1 用户消息进入后

系统先保存 user message 到 `conversation_messages`。

然后进入工具意图判断：

```json
{
  "needs_tool": true,
  "tool_intents": [
    {
      "type": "deadline_update",
      "confidence": 0.82,
      "reason": "用户提到 quiz 延到下周五"
    }
  ],
  "chat_mode": "friendly"
}
```

这个判断结果也应进入 `ai_runs.metadata_json` 或独立表，方便 auditor 检查。

---

### 4.2 deadline 信息处理

如果用户消息包含明确 deadline：

* 调用 deadline extractor；
* 写入 `deadline_candidates`；
* 高置信度应用到 `deadlines`；
* 所有变化写入 `state_changes`；
* assistant 回复必须基于工具结果。

如果信息不完整：

* 标记 candidate 为 `needs_confirmation`；
* bot 向用户确认；
* 不要静默创建 confirmed deadline。

---

### 4.3 回复一致性

如果 assistant 回复中出现：

```text
我记下了
我会提醒你
我帮你更新了
```

那么必须满足：

* 有对应成功 tool_call；
* 有对应 state_change；
* 或明确说明“我先标成待确认”。

daily auditor 应重点检查这一点。

---

## 5. Agent Audit / Reflection 流程

### 5.1 每日 Auditor

每天收集：

* 当天 conversation_messages；
* 当天 ai_runs；
* 当天 tool_calls；
* 当天 assistant_messages；
* 当天 state_changes；
* 当前 deadlines / reminders / timeline；
* 当前 active memories。

输出：

```json
{
  "missed_tool_opportunities": [],
  "possible_state_errors": [],
  "behavior_adjustments": [],
  "user_experience_notes": []
}
```

---

### 5.2 审计结果处理

`missed_tool_opportunities`：

* 进入 `audit_findings`；
* 中高风险时第二天询问用户确认；
* 不直接自动改 deadline。

`possible_state_errors`：

* 进入 `audit_findings`；
* 高风险保持 open；
* 需要用户确认后再改状态。

`behavior_adjustments`：

* 可写入 `behavior_adjustments`；
* 进入 prompt；
* 数量限制，避免 prompt 变重。

`user_experience_notes`：

* 可合并进 interaction_style memory；
* 或进入短期 behavior adjustment。

---

## 6. Prompt 组装策略

v2 的 prompt 需要按模式区分。

---

### 6.1 Deadline Mode

触发条件：

用户问 due、deadline、作业、考试、quiz、reminder、timeline、今天要做什么、最近有什么要交等。

Prompt 内容优先级：

1. system rules；
2. 当前时间和 timezone；
3. deadline / reminder / timeline ledger；
4. pending candidates；
5. 当前用户消息；
6. 最近相关原文；
7. 少量 interaction_style；
8. 少量 behavior_adjustments；
9. living memory 只放高度相关内容。

明确规则：

```text
deadline / reminder / timeline 的事实必须以数据库 ledger 为准。
living memory 不能作为时间事实的权威来源。
如果日期或时间不确定，必须标记 tentative 或询问用户。
```

---

### 6.2 Chat Mode

用户只是聊天、倾诉、讨论想法、问普通问题时使用。

Prompt 内容：

* 最近原文；
* active living memories；
* current_state；
* open_loops；
* interaction_style；
* behavior_adjustments；
* 少量 deadline context，如果和当前话题相关。

Chat Mode 可以更有朋友感，但仍然需要后台判断是否有工具机会。

---

### 6.3 Prompt 中的 Memory Block

建议格式：

```text
【活跃个人状态记忆】
- current_state: ...
- open_loop: ...
- preference: ...
- interaction_style: ...

【近期行为调整】
- 涉及日期、due、提醒时，优先使用工具，不要只靠聊天记忆。
- 回复中承诺“记下/提醒”前，必须确保工具调用成功。
```

限制：

* active memories 最多 10 条；
* behavior adjustments 最多 8 条；
* deadline mode 下 memory 更少；
* archived memory 不进入 prompt。

---

## 7. Redis / RAG / 分层摘要的处理

v2 暂时不把 Redis 放在核心路径。

### 暂不做

* Redis 作为 correctness 依赖；
* RAG；
* 对原始聊天碎片做向量召回；
* 复杂日/周/月自然语言摘要链；
* 把所有 reflection 全量塞进 prompt。

### 可以后续做

* Redis 缓存组装好的 prompt context；
* 对 active memories / weekly curated memories 做 embedding；
* memory dashboard；
* audit dashboard；
* 手动接受 / 拒绝 memory candidates。

---

## 8. 文件改动清单

### 必做

* `bot/database.py`

  * 新增 `ai_runs`
  * 新增 `tool_calls`
  * 新增 `assistant_messages`
  * 新增 `state_changes`
  * 新增 `deadline_candidates`
  * 新增 `personal_memories`
  * 新增 `memory_candidates`
  * 新增 `audit_findings`
  * 新增 `behavior_adjustments`
  * 补 assistant 逻辑回复落 `conversation_messages`

* `bot/ai_engine_base.py`

  * 每次 AI 调用创建 `ai_run`
  * 记录 prompt_version、model、token usage
  * 透传 prompt selector 所需数据

* `bot/toolchain.py` 或对应工具调用层

  * 所有 tool call 写入 `tool_calls`
  * 所有写操作生成 `state_changes`
  * 工具失败也记录

* `bot/discord_bot.py`

  * user message 落 `conversation_messages`
  * assistant 完整逻辑回复落 `conversation_messages`
  * Discord chunk ids 写入 metadata

* `bot/memory_extractor.py`

  * 每日提取 `memory_candidates`

* `bot/memory_curator.py`

  * 每周合并、接受、拒绝候选
  * 每月 decay / archive / supersede

* `bot/auditor.py`

  * 每日检查 tool use、state consistency、reply consistency
  * 写入 `audit_findings`
  * 生成 `behavior_adjustments`

* `bot/prompts.py`

  * 增加 living memory block
  * 增加 behavior adjustments block
  * 区分 deadline mode / chat mode 的 prompt context

* `bot/scheduler.py`

  * 每日 memory extractor
  * 每日 auditor
  * 每周 curator
  * 每月 decay

* `docs/database.md`

  * 补新表说明

* `docs/default-prompts.json`

  * 增加 memory extraction prompt
  * 增加 memory curation prompt
  * 增加 audit prompt

---

## 9. 推荐实施顺序

### Phase 1：先做可追踪性

目标：先让 AI 的行为可审计。

做：

* `ai_runs`
* `tool_calls`
* `assistant_messages`
* `state_changes`
* assistant 回复完整落库
* 所有工具调用统一记录

暂不做：

* memory curator；
* monthly decay；
* Redis；
* RAG。

验收：

* 任意一条 assistant 回复都能追溯到对应 run；
* 任意一次工具调用都能看到参数和结果；
* 任意一个 deadline 更新都能看到 before / after / source。

---

### Phase 2：做 deadline / timeline candidate 化

目标：让时间事实更稳。

做：

* `deadline_candidates`
* tentative / needs_confirmation 流程
* “我记下了 / 我会提醒”一致性检查
* deadline mode prompt

验收：

* 不确定 deadline 不会静默覆盖 confirmed deadline；
* 回复承诺和数据库状态一致；
* auditor 能发现“说了提醒但没创建 reminder”。

---

### Phase 3：做 living memory candidate

目标：从聊天中提取值得记住的软状态。

做：

* `memory_candidates`
* 每日 memory extractor
* 基础 `personal_memories`
* active memories 进入 prompt

验收：

* 碎片闲聊不会直接进入正式 memory；
* deadline 事实不会进入 living memory；
* open loops / preferences / current_state 能被提取。

---

### Phase 4：做 weekly curator + monthly decay

目标：让 memory 会生长，也会过期。

做：

* weekly curator
* duplicate merge
* reinforce / supersede / archive
* monthly decay

验收：

* 旧状态不再长期污染 prompt；
* 类似 memory 会合并；
* 已完成 open loop 能 archived。

---

### Phase 5：做 daily auditor 反哺 prompt

目标：让 AI 越来越会用工具，且更符合用户偏好。

做：

* `audit_findings`
* `behavior_adjustments`
* daily auditor
* prompt 中注入近期行为调整

验收：

* auditor 能发现漏用工具；
* 行为调整简短进入 prompt；
* reflection 不直接修改 deadline 事实。

---

## 10. 明确不做

本期不做：

* RAG；
* 原始聊天碎片 embedding；
* Redis 作为核心状态；
* “日摘要压周摘要压月摘要”的连续自然语言压缩链；
* 让 AI 自由决定长期 memory；
* 让 reflection 直接改 deadline；
* archived memory 进入 prompt；
* 把 deadline 事实存进 living memory 作为权威来源。

---

# 当前需要决策的地方

## A. 产品边界决策

### 1. 你的 bot 到底是单用户单频道，还是未来多频道 / 多用户？

选项：

* A1：只服务你自己，一个主频道；
* A2：服务你自己，但有多个 Discord channel；
* A3：未来可能多人使用。

推荐：按 A2 设计。
即所有表都保留 `channel_id`，但暂时不复杂化权限系统。

---

### 2. deadline / timeline 是否允许 AI 高置信度自动写入？

选项：

* B1：AI 只创建 candidate，所有变化都问你确认；
* B2：高置信度自动写入，中低置信度问确认；
* B3：AI 可以自由写入和更新。

推荐：B2。
这是体验和准确性的平衡。

---

### 3. “下周五 / 明天 / 周三”这种相对时间，是否默认使用 Australia/Sydney？

选项：

* C1：默认 Australia/Sydney；
* C2：每次都要求明确 timezone；
* C3：根据课程 / 用户所在地动态判断。

推荐：C1。
但每次落库都必须保存 timezone，并在不确定时标 tentative。

---

## B. Memory 策略决策

### 4. memory candidate 是每日自动进正式 memory，还是每周 curator 再决定？

选项：

* D1：每日直接进 `personal_memories`；
* D2：每日只进 candidate，每周 curator 接受 / 合并；
* D3：所有 memory 都需要你手动确认。

推荐：D2。
不容易污染 memory，也不会太打扰你。

---

### 5. active memory 进入 prompt 的上限是多少？

建议默认：

* preference：最多 5 条；
* current_state：最多 3 条；
* open_loop：最多 5 条；
* interaction_style：最多 5 条；
* behavior_adjustments：最多 8 条。

需要你决定：你想要 bot 更“记得你”，还是 prompt 更轻？

推荐：先用上面的保守上限。

---

### 6. current_state 类 memory 多久 review？

例如“最近压力大 / 最近不开心 / 最近在纠结某项目”。

选项：

* F1：14 天 review；
* F2：30 天 review；
* F3：90 天 review。

推荐：默认 30 天 review，90 天 valid_until。
情绪状态不要永久保存。

---

### 7. open_loop 类 memory 多久 review？

例如“想改 bot memory / 想做课程管理 skill / 想整理 Linear”。

选项：

* G1：14 天；
* G2：30 天；
* G3：90 天。

推荐：30 天 review，90 天 valid_until。
超过 90 天没再出现就 fading。

---

## C. Audit 策略决策

### 8. auditor 发现问题后，是自动修复还是第二天问你？

选项：

* H1：全部问你；
* H2：低风险自动修，高风险问你；
* H3：全部自动修。

推荐：H2。

低风险例子：

* reminder 晚于 deadline；
* assistant 说“我记下了”但没有 state_change，可以生成 open finding。

高风险例子：

* 修改 due date；
* 删除 deadline；
* 把 tentative 改 confirmed。

高风险必须问你。

---

### 9. auditor 是否每天都跑？

选项：

* I1：每天深夜跑；
* I2：只有当天有聊天 / 工具调用才跑；
* I3：手动触发。

推荐：I2。
当天没有新消息就不用跑，省 token。

---

### 10. auditor 用同一个 AI，还是另一个更便宜 / 更严格的模型？

选项：

* J1：同一个模型；
* J2：便宜模型；
* J3：更强模型但低频跑；
* J4：便宜模型先审，高风险再用强模型复审。

推荐：J4。
但 v1 可以先用 `simple_completion()` 的 fallback preset。

---

## D. Prompt / 模式决策

### 11. 要不要明确分 deadline mode 和 chat mode？

选项：

* K1：不分，统一 prompt；
* K2：轻量 router 分两种模式；
* K3：复杂多模式，例如 deadline / chat / planning / audit。

推荐：K2。
先分 deadline mode 和 chat mode 就够了。

---

### 12. deadline mode 下要不要喂 current_state？

选项：

* L1：不喂，只喂 ledger；
* L2：只喂 interaction_style 和 behavior_adjustments；
* L3：也喂 current_state，让回复更有朋友感。

推荐：L2。
deadline mode 先保证准，再保证温柔。

---

### 13. chat mode 下是否也要做后台 tool intent 判断？

选项：

* M1：不做，聊天就是聊天；
* M2：做轻量判断，有时间 / deadline / reminder 信号再触发工具；
* M3：每句话都跑完整 extractor。

推荐：M2。
这样既能聊天，又不会漏掉任务信息。

---

## E. 工程实现决策

### 14. 先做 run log，还是先做 memory candidate？

选项：

* N1：先做 memory；
* N2：先做 run log；
* N3：一起做。

推荐：N2。
没有 run log，后面 auditor 和 debug 都会很痛苦。

---

### 15. Redis 现在要不要加？

选项：

* O1：现在加；
* O2：先不加；
* O3：只作为可选 cache。

推荐：O2。
当前真正重要的是 ledger、log、audit，不是缓存。

---

### 16. 是否需要 memory dashboard / review UI？

选项：

* P1：不做，只用数据库；
* P2：先做简单命令查看 active memories / candidates；
* P3：做完整前端。

推荐：P2。
先加 Discord debug command 或 CLI，比完整前端划算。

---

### 17. 是否保留原来的 `memories` 表？

选项：

* Q1：保留不动，v2 新建 `personal_memories`；
* Q2：迁移旧 `memories` 到新表；
* Q3：直接废弃旧表。

推荐：Q1。
先不破坏现有逻辑，等 v2 稳定后再迁移。

---

## F. 行为体验决策

### 18. bot 发现 memory candidate 后，要不要主动告诉你？

选项：

* R1：不告诉，后台整理；
* R2：重要 memory 才问；
* R3：每天汇报新增 memory；
* R4：全部需要你确认。

推荐：R2。
比如涉及敏感状态、长期偏好、重要 open loop 时再问。

---

### 19. bot 第二天要不要汇报 audit findings？

选项：

* S1：不汇报，只内部改进；
* S2：只汇报中高风险；
* S3：每天发完整审计报告。

推荐：S2。
避免打扰，但关键错误要暴露。

---

### 20. “朋友感”和“准确性”冲突时优先谁？

推荐规则：

```text
deadline / reminder / timeline 场景：准确性优先。
chat / 情绪 / 想法讨论场景：朋友感优先。
不确定时：先准确，再温柔。
```

这一条建议写进 system prompt。
