# Prompt 重构执行计划（AI 执行版）

## 🎯 目标

对现有 prompt 系统进行结构性优化，实现：

1. 工具调用决策统一（避免行为漂移）
2. prompt 与 tool 职责解耦
3. 降低 token 与认知负担
4. 提高多模型稳定性（Claude / GPT / Gemini）

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
