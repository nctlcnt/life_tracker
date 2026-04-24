# Role Split Plan

把目前单一"日和（陪聊女友 + 后台记录）"模式拆成多个独立 role，主要为了：
1. **省钱**：每个 role 的 system prompt 极简，cache 命中率最大化；非交互的批处理走便宜模型
2. **职责清晰**：用户的"管理时间"需求和"被推一把"需求由不同人格处理，不再相互稀释
3. **去除"系统单方面记录"的体感**：用户希望和系统有"来有回"，记录不是后台静默发生的副作用

实施分支：`feature/role-split`

---

## 三个 Role

| Role | 触发场景 | 人格 | system prompt 内容 | 工具 | 模型 |
|---|---|---|---|---|---|
| **A. 时间管理助手** | 用户主动："帮我记一下…"、"明天忙不忙"、查日程；或系统主动询问状态（"你今天在做什么？"） | 无人格，工具人口吻 | USER_MODEL（仅基础信息） + 工具说明 + 时间格式规则 | 全套（log/query/update/delete timeline、reminder、deadline、memory） | 主模型（Claude） |
| **B. 拖延助力人格** | 检测到拖延信号（PROTOCOLS 的信号 B/C/D/E）；或用户主动求助："不想做"、"算了"、"明天再说" | 完整人格（日和） | IDENTITY + USER_MODEL（含神经回路段） + COMMUNICATION + PROTOCOLS（仅 B/C/D/E） | 极少（read-only：query_timeline / list 类） | 主模型（Claude） |
| **C. 夜间清理任务** | 凌晨 4 点 cron | 无人格，纯工具 | 工具说明 + 去重规则 | 全套（重点 delete/update） | Gemini Flash 或 Haiku（实验对比） |

---

## 路由器（Router）

入口在 `on_message` 里，每条用户消息都先过路由器分类，再分发到对应 role：

```
user message → classifier → {A | B | direct_chat}
                              ↓        ↓        ↓
                          时间助手  助力人格  闲聊（保留？）
```

**关键决策点**：
- 路由器用什么模型？倾向 Gemini Flash / Haiku（轻量）。已有 [bot/ai_engine_base.py](bot/ai_engine_base.py) 的 classifier 架构（参见 commit `400c4b2`）可复用
- 分类失败的兜底：默认走 Role B（人格更通用，不会答非所问；纯工具调用反而冷冰冰）
- 是否保留"纯闲聊"路径？需要进一步确认（见下文"待确认问题"）

---

## 阶段划分

### 阶段 1：Role C（夜间清理任务）

**优先做这个**——最独立、不动现有 chat 路径、能立刻验证"无人格 + 仅工具"prompt 的可行性。

#### 设计决策（**已确认**）

1. **触发时机**：固定凌晨 4 点 cron。理由：用户睡眠 11pm-1am，4 点足够安全；省一次"AI 判断她睡了没"的调用
2. **清理范围**：第一版只做两件
   - **Reminders 去重**：扫 pending reminders，发现 group_id 相近 / action 重复 / trigger 时间窗 ±30min 重叠 → 合并或删除
   - **Memories 整理**：扫所有 memories（上限 20），过期的删、近似的合并、超过 20 条精简
3. **暂不做**：
   - Timeline events 持久化合并（[bot/merge.py](bot/merge.py) 现在是查询时合并，不写库）
   - Deadline 清理（系统会自动 expire）
4. **数据喂法**：一次性灌全量到 user message，让 AI 一轮调一堆工具搞定（追求确定性，不要多轮探索）
5. **模型**：新增 `cleanup` preset key（在 config.json 里），如果未配则回退到 active preset。两次跑分别试 Gemini Flash 和 Haiku，看效果
6. **Prompt 风格**：完全独立的 `CLEANUP_PROMPT`，不走 `build_prompt()` 的 PromptParts 体系

#### Prompt 草稿

```
你是一个数据清理工具。任务是去重和整理 reminders / memories。
不要输出对话性文字，只调工具。完成后输出 [DONE]。

## 工具说明
[复用 TOOLS_SECTION 的 Reminder + Memory 段，去掉调性词]

## 当前数据
### Pending Reminders
{reminders_dump}

### Memories
{memories_dump}

### 今日 Events（仅供参考，不清理）
{today_events_dump}

## 去重规则
- Reminders:
  - 同 group_id 多条且 action 重复 → 保留最早的，delete 其余
  - 不同 group_id 但 action 相近且 trigger ±30min 内 → 保留最早，delete 其余
  - action 与今日 events 矛盾（已经做完了）→ 全 delete
- Memories:
  - 同主题多条 → update 合并到一条
  - 含明确日期且日期已过 → delete
  - 总数 > 20 → 优先删信息密度低的
```

#### 实施步骤

1. 在 config.json schema 里加 `ai.presets.cleanup`（可选）
2. [bot/prompts.py] 新增 `CLEANUP_PROMPT` 模板和数据格式化函数
3. [bot/scheduler.py] 新增 `_do_nightly_cleanup()` 方法 + 在 `_timer_loop` 里加 4am 触发
4. 复用现有 `scheduled_action` 框架？还是写一个独立的 `cleanup_action`？
   - 独立写更清晰：因为不需要 send_callback、不需要 history、不需要 PromptParts
   - 写一个 `bot/ai_engine_base.py::cleanup_action(prompt, preset)` 接口，类似 `simple_completion` 但带工具
5. 加 cleanup 完成后的日志（删了几条 reminders / 整理了几条 memories）

**验证标准**：
- 跑 1 周，每天看 log 确认没误删
- token 用量 < 5000 input / 1000 output（Haiku 报价下成本可忽略）

---

### 阶段 2：Role A 和 Role B 的拆分（含路由器）

**前置条件**：阶段 1 跑稳定后再做。

#### 待确认问题

1. **是否保留纯闲聊路径？**
   - 用户原话："其实不是主动找话聊，而是主动询问我的状态"
   - 解读：poll 循环不再"找话题"，改成"问候 + 询问状态"。这归 Role A 还是 Role B？
   - **倾向**：归 Role A（询问状态本质是为了"记录今天的活动"），但措辞要友好不冷冰冰
   - 那 PROACTIVE_PROMPT / MORNING_PROMPT 要重写
2. **Role A 说话的口吻**
   - 用户原话："不需要 ai 有人格"——但这指的是 prompt 内容上不灌人格，**输出风格**是"工具人式简洁"还是"日和的简洁版"？
   - **倾向**：保持日和的语气词和称呼习惯（"嗯""好""帮你记一下"），但不带情绪共情、不抛话题。Discord 用户体验上还是同一个"日和"
3. **Role A 和 B 在 Discord 上是同一身份吗？**
   - **倾向**：是。同一个 bot 头像，路由对用户透明
4. **路由器分类错了怎么办？**
   - 比如用户说"我今天累死了"——这是情绪宣泄（Role B）还是要记录"今天很累"（Role A）？
   - **倾向**：训练路由器时倾向归 B，因为漏掉记录可补救（用户可以说"帮我记一下"），但情绪被工具人冷处理伤害更大

#### Prompt 拆分草稿

**Role A（时间管理助手）**：
```
你是用户的时间管理助手。任务：帮她记录、查询、提醒、跟进 deadline。

## 用户基础信息
- 女生，悉尼，AEST/AEDT
- 在学数据科学

## 你的口吻
- 简洁、像微信发消息
- 不抛新话题、不共情、不评判
- 工具调用前可以顺口说一句"帮你记一下"

## 工具说明
[完整 TOOLS_SECTION]

## 时间规则
[现 SYSTEM_MECHANICS 的时间戳段]
```

**Role B（拖延助力人格）**：
```
[完整 IDENTITY]
[完整 USER_MODEL]
[完整 COMMUNICATION]
[PROTOCOLS 的 B/C/D/E 信号]

## 工具
你可以查（query_timeline / list_reminders）但不能写。
需要记录请告诉用户"我帮你记一下"，由系统转发到时间助手。
```

**路由器 prompt（极短）**：
```
分类用户消息到 A / B / C / DIRECT：
- A: 含明确的记录/查询/提醒/deadline 意图（"帮我记一下""明天有什么""提醒我"）
- B: 情绪、拖延信号（"不想做""累了""算了"）、求助
- C: 用户主动触发清理（罕见）
- DIRECT: 都不是 → 走 B 兜底

只输出 A/B/C/DIRECT 一个字符，不要解释。
```

#### 实施步骤

1. [bot/prompts.py] 拆出 `ROLE_A_PROMPT` / `ROLE_B_PROMPT` / `ROUTER_PROMPT`
2. 新增 router 模块（或复用现有 classifier）
3. [bot/discord_bot.py] `on_message` 改成：先调 router → 分发到对应 role 的 chat 函数
4. PROACTIVE_PROMPT 重写：从"找话聊"改成"问候 + 询问状态"，归 Role A
5. **不删旧的合一 prompt**：作为 fallback / 实验 baseline 保留
6. 实验对比：拆分前后的 token 用量、cache 命中率、用户体验

#### 风险

- **路由器分类错的代价**：可能比单 prompt 自适应更高
- **Role 之间无法共享上下文**：用户说"帮我记一下今天累死了" → 走 A 记录，但失去了 Role B 共情的机会。可能需要 A 在记录后调 B（"我已经记了，要不要聊聊？"）→ 但这又增加成本
- **Cache miss**：从 1 套 prompt 变 3 套，每套独立 cache。但每套都更短，理论上总成本仍下降

---

### 阶段 3（潜在）：Role A 主动询问机制重设计

把现有 `PROACTIVE_PROMPT` / `MORNING_PROMPT` / `BEDTIME_PROMPT` 重新设计成 Role A 风格的"问候 + 询问"，让用户感觉记录是"对话产物"而非"后台监控"。

**核心改动**：
- PROACTIVE：从"找个话题聊"改成"问一句你现在在干嘛"——回答自然产出 timeline 记录
- MORNING：从"盘点今天"改成"早上好，今天打算做什么？" + 用户回答 → log 计划
- BEDTIME：保留，但改成"今天感觉怎么样？"而不是"该睡了"

这一步等阶段 2 落地后再细化。

---

## 待办（汇总）

### 阶段 1（当前）
- [ ] 在 config.json 加可选的 `cleanup` preset
- [ ] [bot/prompts.py] 新增 `CLEANUP_PROMPT` 和数据格式化函数
- [ ] [bot/ai_engine_base.py] 新增 `cleanup_action(prompt, preset)` 接口
- [ ] [bot/scheduler.py] 新增 `_do_nightly_cleanup()` + 4am 触发逻辑
- [ ] 测试：手动触发跑一次，确认行为符合预期
- [ ] 跑 1 周观察日志，调整去重规则阈值

### 阶段 2（阶段 1 稳定后）
- [ ] 确认上述 4 个待确认问题
- [ ] 拆分 system prompt 为 ROLE_A / ROLE_B
- [ ] 实现路由器（用 Gemini Flash 或 Haiku）
- [ ] 重写 `on_message` 流程
- [ ] A/B 对比：拆分前后的 token 用量、用户体验

### 阶段 3（最后）
- [ ] 重新设计 PROACTIVE / MORNING / BEDTIME 的措辞
- [ ] 验证"记录由对话产出"的用户体感

---

## 设计原则记录

- **省钱不是唯一目标，体感优先**：拆 role 是因为单一人格在"管理"和"陪伴"之间无法两全。省钱是副作用
- **路由错误的非对称代价**：把情绪误归到工具人路径 >> 把记录意图误归到陪伴路径。路由器倾向偏向陪伴
- **批处理任务零人格**：Role C 完全工具化，不掺感情，确保确定性
