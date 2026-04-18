# Prompt 重构执行计划（AI 执行版）

## 🎯 目标

对现有 prompt 系统进行结构性优化，实现：

1. 工具调用决策统一（避免行为漂移）
2. prompt 与 tool 职责解耦
3. 降低 token 与认知负担
4. 提高多模型稳定性（Claude / GPT / Gemini）

---

## 📊 当前进度（同步于 2026-04-18）

> ⚠️ **架构决定已变更**：Claude engine 路径**有意放弃了"分层决策 / policy 入口"方向**，优先保障 prompt caching 命中率。2026-04-15 的 `5321bbf` / `a10096b`（Steps 1-2 落地）于 2026-04-17 被 `01b22c2`（"unify chat/poll prompt"）回退是有意为之，不是 regression。

### 为什么回退

- Claude 以 1h ephemeral cache 复用 static + semi-dynamic 两层 block；分层决策结构越细，越容易因语义微调（枚举项增减、措辞调整、引用的动态字段改名）整块 invalidate。
- 统一 chat / poll 为同一套 prompt 之后，两条 code path 共享同一个 cached 前缀，命中率进一步提升。
- 实际观察下来 Claude 不需要 Step A/B/C 那种显式步骤化引导也能稳定做对，因此省掉它换 cache 稳定性更划算。

### 当前运行成本（Claude Opus + 1h cache + 全量 prompt）

- **花费**：约 $0.1736 / 小时
- **cache 命中率**：~85%
- **模型**：Opus
- 可以试 Sonnet 进一步降成本，但那属于模型选型，不在本 prompt 优化计划内。
- 下一阶段的 prompt 优化重点转向 **精简行文、降低全量 prompt 的 token 量**（不改变结构），而不是重建分层决策框架。

### 2026-04-18 已完成的瘦身：移除 chill/drain 子标签

`refactor/drop-energy-type` 分支把 TIME_PERCEPTION 里的 "蓄水 vs 漏水" 段、TOOL_GUIDELINES 里的 `energy_type` 字段说明、PROACTIVE_PROMPT 里的蓄水/漏水分支，以及 tools.py 两套 schema 的 `energy_type` 参数全部删除（连带 DB 列、`ChillDrainChart` 组件、`MultiLaneTimeline` 的 drain 染色一并清理）。动机：用户自述无法精准自评状态，前端也一直不看这张图。Focus/Routine/Chill 三分法保留。

### 各 Step 当前状态

| Step | 状态 | 说明 |
|---|---|---|
| 1. TOOL DECISION POLICY | ❌ 放弃（Claude） | Claude 不再需要。Gemini / Relay 未决。 |
| 2. 抽出 TIMELINE / REMINDER STRATEGY | ❌ 放弃（Claude） | 同上，`tools.py` 描述里保留场景与去重规则。 |
| 3. 移除 `<think>` 机制 | ❌ 未做，且方向相反 | `RESPONSE_CORE` 反而更强调中间轮独白 / 最后一轮 `<think>` 规则，`ai_engine_base.py::split_thinking()` 仍在链路上。若确定保留需把 plan 此项删掉。 |
| 4. 简化 RESPONSE_CORE | ❌ 未做 | 如果 Step 3 保留 `<think>` 方案，这里"删除"目标要相应改成"只在不损伤 cache 的前提下做局部清理"。 |
| 5. 上下文注入策略 | ✅ 基本达成 | 代码注入规则与计划表一致；"筛选后的关键信息"这行已不在 `prompts.py` 中。 |
| 6. 工具语义轻量化 | ❌ 未开始 | 计划本身标记为可选/延后；对 Claude 同样是"不碰就是最好的"。 |
| 7. 验证 | ❌ 未执行 | 已失去原有依赖；留到下次真正动 prompt 时再跑。 |

### Token 占用分析（来源：`data/token_analytics.html`）

**静态层 Top 3（瘦身优先级从高到低）**：

| 段落 | Tokens | 备注 |
|---|---|---|
| `TOOL_GUIDELINES_CHAT` | 995 | 最大头；"记录规则 / 提醒策略 / 记忆管理 / DDL 管理"四块都长，合并重复说明收益最大 |
| `TIME_PERCEPTION_CHAT` | 482 | （2026-04-18 删除蓄水/漏水段后已下降，待下次分析重测）过渡困难、情绪捕捉段落仍可压 |
| `RESPONSE_CORE` | 459 | 最重要的规则 + 中间轮/最后一轮 + 斜杠命令识别 + 消息节奏 + 时间戳 |

**动态层**：`memories` 占比最高——条数上限（20）已经限了，但**单条长度无限制**，容易被 AI 写成长段描述。

### 建议下一步

1. **Step 1 / 2 / 3 / 4** 如果只面向 Claude，就直接从 plan 里删掉或标记为"废弃（cache 优先）"，避免将来误以为是待办。
2. 如需针对 Gemini / Relay 做 provider-specific 分层决策，走 `prompts.py::build_prompt()` 里已经留的 `TODO(provider-prompt)` + `_PROVIDER_SECTIONS` 路线，而不是再在 Claude 版本上叠层。
3. **静态层行文瘦身**（按 Top 3 顺序进攻，保持结构稳定不破 cache）：
   - `TOOL_GUIDELINES_CHAT`：合并重合 bullet、去掉冗余"比如/例如"、长条件句改短句+枚举。
   - `TIME_PERCEPTION_CHAT`：过渡困难、情绪捕捉的重复说明只讲一次（蓄水/漏水段已于 2026-04-18 移除）。
   - `RESPONSE_CORE`：凝练中间轮/最后一轮的规则，系统输出识别段可以压缩。
4. **动态层：限制单条 memory 长度**（候选方案）：
   - 在 `save_memory` / `update_memory` 的 tool description 里加一句"单条 ≤ N 字"（软约束）。
   - 或在 `bot/database.py` 写入时硬截断；或在 `_format_memories` 渲染时截断。
   - 需要选一个既能降 token 又不丢语义的 N（初步感觉 40–60 字够用）。
5. 模型维度：可以单独评估 Opus → Sonnet，但在本 plan 之外。

---

## 🧭 总体策略（必须遵守）

* 不删除现有功能，只做“结构重排”
* 不改变用户体验（语气、风格保持一致）
* 所有改动优先保证“行为稳定性”
* 避免引入新的复杂机制

---

# 🧱 Step 1：引入唯一的工具决策入口（最高优先级）

## 操作

在 `prompts.py` 的 `TOOL_GUIDELINES_CHAT` 开头新增 `TOOL DECISION POLICY` section，替换掉现有分散的决策逻辑。

合并策略：以**多步 chain（A/B/C）** 作为主框架，把原有分类决策树的逻辑内嵌到 Step B 里。

```
## TOOL DECISION POLICY（唯一决策入口）

每次收到用户消息，按以下三步循环执行，允许多轮 tool call：

---

### Step A：先确认现有状态（优先用上下文）

系统每轮会随 system prompt 注入（chat 模式）：
- **【当前进行中的事件】**：ongoing timeline events，直接可用（最多5条，按开始时间倒序）
- **【你现在记着的事】**：memories，直接可用（最多20条，按创建时间倒序）
- **【待触发的跟进计划】**：**全量注入**，所有 pending reminders 均可直接读取
- **【待完成的 Deadline】**：deadlines，直接可用（全量，无上限）

> ⚠️ poll 模式例外：reminders 不注入（调度时 AI 不需要看跟进清单）

**操作规则**：
- 判断 ongoing 状态 → 直接读【当前进行中的事件】，**不需要调用 query_timeline**
- 判断历史记录（用户问"今天做了什么"等）→ 才调用 query_timeline
- 准备 set_reminder 时 → 直接对照【待触发的跟进计划】判断是否重复，**无需额外调用 list_reminders**
- set_reminder 完成后 → 系统会自动触发后置去重提示，AI 可借此自查是否产生重复

---

### Step B：决定调用哪些工具（分类决策）

根据 Step A 获取的信息，按以下分类判断：

**用户提到了具体活动（正在做 / 刚做完）？**
- 有对应 ongoing event → update_timeline_event（延续/结束/添加notes）
- 没有 → log_timeline_event（新建）
- 同时段已有内容一致的记录 → 不重复新建，直接跳过或 update notes

**用户提到了未来要做的事 / 需要跟进的事？**
- 直接对照【待触发的跟进计划】确认无重复，再 set_reminder
- 同一件事共享 group_id，多条密度由 AI 根据紧急程度自行判断：
  - deadline 类（考试/面试）：越临近越密，参见 deadline 多条安排规则
  - 非 deadline 类（”看完就回来”/”要去买东西”）：1-2 条即可，不需要每天提醒
- 如果【待触发的跟进计划】已有相同内容 → 不 set，避免重复

> **设计备注**：当前 deadline 和 reminder 是分开的两套工具。对于有明确截止日期的事，`add_deadline` 负责结构化存储，reminder 负责跟进节奏——两者分工明确，不需要合并为”日程”。非 deadline 类的跟进提醒直接用 reminder 即可，AI 自行判断条数和间隔。

**用户提到了 deadline / 重要日期？**
- add_deadline（存结构化截止时间）
- 同步检查 memory 里是否有纯记时间的重复条目 → 有则 delete_memory

**用户提到了偏好 / 计划 / 以后有用的信息？**
- save_memory / update_memory

**用户在查询（今天做了什么 / 还有什么提醒）？**
- query_timeline / list_reminders

**以上都不符合（闲聊、情绪表达、哈哈哈）？**
- 不调用任何工具，正常回复

---

### Step C：检查是否还需要继续

每次 tool call 结束后判断：

- 还有未完成的状态修改？→ 继续
- 刚刚的操作可能引入重复或冲突？→ 先 query 确认，再决定是否 delete
- 所有状态已一致？→ 停止 tool call，输出最终回复

---

### ⚠️ 关键原则

1. **优先用已注入的上下文，不重复查询**——ongoing / memory / deadline 已在 system prompt 里，直接读；只有 reminders 和历史 timeline 需要主动查
2. **发现重复 → 主动清理（delete_reminder / delete_timeline_event）**
3. **每步只做一件清晰的事**
4. **允许多步，不要急着结束**
5. **所有状态一致后再输出最终回复**

---

### ❌ 禁止行为

- ⚠️ **不看 ongoing 就直接 log**（会产生重复事件）
- ⚠️ **不对照【待触发的跟进计划】就 set_reminder**（会产生重复提醒）
- 明明需要 update 却新建（会产生重复事件）
- 收到 [提醒触发] 消息后再次 set_reminder 相同内容（死循环）
```

---

## 同步修改（非常重要）

完成上述新增后，删除以下地方的**冗余决策逻辑**（保留说明性内容，删除决策判断）：

- `TOOL_GUIDELINES_CHAT` 中「什么时候该记录」整段（已被 Step B 覆盖）
- `TOOL_GUIDELINES_CHAT` 中「新建 vs 更新 vs 删除（重复检测）」的决策部分（已被 Step B + Step C 覆盖，保留 content/notes 格式规则）
- `TOOL_GUIDELINES_CHAT` 中「提醒策略 → 什么时候设 reminder」（已被 Step B 覆盖，保留 group_id / priority / deadline 多条安排的说明）
- `RESPONSE_CHAT` 末尾「说到就要做到：…必须调用对应的工具」（已被 TOOL DECISION POLICY 覆盖）

---

# 🧱 Step 2：收敛 Reminder / Timeline 策略到 Prompt（从 tool 移出）

## 操作

在 prompt 中新增两个独立 section：

---

### ## TIMELINE STRATEGY

只保留：

* 如何拆分事件
* content vs notes 区分
* update vs create 判断原则

删除：

* “什么时候记录”（交给 TOOL DECISION POLICY）

---

### ## REMINDER STRATEGY

包含：

* 使用场景（deadline / follow-up）
* group_id 规则
* 去重策略
* priority 逻辑

---

## 同步修改

在 tools.py 中：

### set_reminder / log_timeline_event

只保留：

* 简要用途说明
* 参数解释

删除：

* 使用场景说明
* 去重规则
* 调用策略

---

# 🧱 Step 3：移除 `<think>` 机制（改为 runtime 控制）

## 操作

删除：

* 所有 `<think>` 标签相关规则
* “中间轮 / 最后一轮”复杂描述

---

## 替代机制（由系统实现，不写进 prompt）

运行时保证：

* 有 tool call 的轮次 → 不输出给用户
* 最后一轮 → 只允许自然语言输出

---

## Prompt 中保留一句简化规则：

```
当你调用工具时，不需要对用户说任何话。
当你结束工具调用后，用自然语气回复用户。
```

---

# 🧱 Step 4：简化 RESPONSE_CORE（降低认知负担）

## 操作

保留：

* 语气风格（朋友感）
* 基本交互原则

删除或压缩：

* 重复出现的行为约束
* 与工具无关的细碎规则

目标：

> RESPONSE_CORE 控制“怎么说话”，不控制“做什么决策”

---

# 🧱 Step 5：上下文注入策略（Context Injection）

## 当前各字段的注入规则（代码层已确认）

| 字段 | 上限 | chat 模式 | poll 模式 | 备注 |
|---|---|---|---|---|
| memories | 20 条（`created_at DESC`） | ✅ 注入 | ✅ 注入 | 代码不做相关性过滤；未来可改为 AI 判断保留最相关10条 |
| ongoing events | 5 条（`start_time DESC`） | ✅ 注入 | ✅ 注入 | 目前够用；量大后再考虑筛选 |
| reminders | 无上限（全量） | ✅ 注入 | ❌ 不注入 | poll 模式下 AI 不需要看跟进清单 |
| deadlines | 无上限（全量） | ✅ 注入 | ✅ 注入 | 全量保留，影响 reminder 安排节奏 |

---

## 同步删除

移除 prompt 中原有的”你看到的是筛选后的关键信息，不是全部数据”这行说明——该描述与实际不符。

---

# 🧱 Step 6：工具语义轻量化（可选，延后）

## 当前不修改工具结构，仅调整描述

目标：

* tool = 能力接口
* prompt = 决策逻辑

---

# 🚀 部署与回滚（直接 Python 运行）

## 开始前：固定稳定版

```bash
git tag stable-before-prompt-refactor
```

## 开发分支

```bash
git checkout -b feature/prompt-refactor
# 按本文档各 Step 修改 tools.py / prompts.py
```

## 本地快速验证（--test 模式）

```bash
python main.py --test
# 手动发几条消息跑 Step 7 的验证场景，确认基本行为正确
```

## 上线测试（跑满一天）

```bash
git commit -m "feat(prompt): restructure tool decision policy"

# 停掉当前进程（Ctrl+C 或关掉那个窗口）
# 切到 feature 分支（如果还没 checkout）
git checkout feature/prompt-refactor

# 正常启动（不加 --test）
python main.py
```

## 一天后评估

**行为变好** → 合并回 main：

```bash
git checkout main
git merge feature/prompt-refactor
git tag stable-after-prompt-refactor
```

**行为变差** → 立刻回滚（30 秒）：

```bash
# Ctrl+C 停掉当前进程
git checkout stable-before-prompt-refactor
python main.py
```

---

# 🧪 Step 7：验证（必须执行）

对以下场景进行测试：

1. 用户说：“我去洗澡”
   → 应触发 set_reminder

2. 用户说：“刚学完两个小时数据科学”
   → 应 update timeline

3. 用户说：“好无聊”
   → 不调用任何工具

4. 用户说：“周五考试”
   → add_deadline + set_reminder（多条）

---

# 📌 最终结果（你应达到的状态）

* 所有“是否调用工具”的逻辑只存在于一个地方
* tool description 变得简洁稳定
* prompt 长度减少，但行为更稳定
* 多轮调用不依赖 prompt hack（如 <think>）

---

# 🧾 一句话总结

将系统从：

→ “prompt 驱动一切”

改为：

→ “prompt 决策 + tool 执行 + runtime 控制”
