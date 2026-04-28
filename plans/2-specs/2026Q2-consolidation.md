# 2026 Q2 整合重构 Plan

来源：`plans/1-ideas/inspiration.md` 里的几段想法 + 对应 review。本文件是这一轮重构的总纲，剩余 3 个 Phase。

实施分支建议：每个 Phase 一个独立分支，merge 回 `main` 后再开下一个。

> **2026-04-25 变更**：原 Phase 1（Notes + Memory 重构）已整体推翻并归档到
> [`4-archive/notes-memory-split-2026-04-25.md`](../4-archive/notes-memory-split-2026-04-25.md)。
> 现存 Phase 编号沿用原序号（2/3/4），不重排。

---

## 与历史 plan 的关系

| 历史 plan | 状态 | 说明 |
|---|---|---|
| `event-notes-split-2026-04-23.md` | **废止** | 原方案是把 `events.notes` 拆成 `event_notes` 表挂在 event 上；后续替代方案（daily_notes 独立表）也已推翻，最终保留 `events.notes` 现状 |
| `notes-memory-split-2026-04-25.md` | **废止** | Q2 原 Phase 1。归档时已写明推翻理由（events.notes + memory 现状已够用，notes 不需 first-class）|
| `role-split-2026-04-23.md` | **部分废止** | Role A/B 拆分被 Phase 2 的 Flash+Smart 分发取代；Role C 夜间清理原计划并入 Phase 1 的 daily summary cron，Phase 1 推翻后整体搁置；阶段 3（主动询问重写）保留思路并入 Phase 3 |
| `ai-token-classifier-2026-04-23.md` | **部分吸收** | Classifier 架构的思路并入 Phase 2，但改为"Flash 主聊 + 工具意图 escalate 到 Smart"而非"Classifier 前置 → 多处理器并列" |
| `energy-2026-04-18.md` | 保持归档 | 精力槽方向在 Phase 4 重新立题，不依赖旧的 energy_type 字段 |
| `merlin.md` / `planned-event.md` / `prompt-sections-2026-04-18.md` | 不受影响 | 独立线索，与本轮不冲突 |

---

## Phase 1：[已推翻]

参见 [`4-archive/notes-memory-split-2026-04-25.md`](../4-archive/notes-memory-split-2026-04-25.md)。

---

## Phase 2：Dispatch 机制 AB POC（优先级 2）

> **2026-04-28**：本 Phase 的细化设计 + 隔离测试方法论拆到独立 spec [`dispatch-poc.md`](dispatch-poc.md)，由"prod env-flag 切换"调整为"第二 bot 进程隔离测试 → 验证通过再讨论 prod"。本 Phase 总纲保留作为背景上下文，**实际推进以 dispatch-poc.md 为准**。

**目标**：加一个 `DISPATCH_MODE` 开关，开 = 走 Flash（人格/聊天） + Smart（工具/日程）的双模型路径；关 = 现有单模型路径。用户**用开关做每日 AB 对照**（比如奇数日开、偶数日关），观察 token / 体感 / bug。

### 2.1 架构

```
DISPATCH_MODE=off (baseline)
  on_message ──► chat() [Opus 主模型, 全工具, 5/6 段 prompt]

DISPATCH_MODE=on (POC)
  on_message ──► [router by ESCALATE_STRATEGY] ──► flash + smart 组合路径
```

不管哪种策略，共同框架都是：
- **Flash**（Gemini Flash Lite）：面向用户的人格层，拿 IDENTITY + USER_MODEL + COMMUNICATION
- **Smart**（Sonnet / Opus / Gemini Pro 三选一）：纯工具/日程层，拿 SYSTEM_MECHANICS + TOOLS_SECTION + 精简 USER_MODEL
- **失败承认**：任何一层失败就直接告诉用户"这次没记住哦，你再说一遍"。不再兜底降级（降级会降低回复质量）

**三种 `ESCALATE_STRATEGY`**：

| 策略 | 触发 Smart 的时机 | 适合的 Smart 模型 | 单条成本估算 | 漏记风险 |
|---|---|---|---|---|
| `conditional_flash` | Flash 自己判断是否调 `escalate_to_scheduler` 工具 | 贵模型（Opus） | 纯聊天几乎 $0；escalate 时 $0.05-0.08 | 高（Flash 判断力差） |
| `always_smart` | 每条消息都过 Smart；Smart 返回 `{tool_result, chat_suggestion}`；Flash 把 chat_suggestion 用人格语气重写 | 中价位（Sonnet） | 每条 $0.003-0.008 | 极低 |
| `rule_based` | on_message 先本地 regex 匹配（"记一下/明天/提醒/deadline/X 点"等）。命中 → 走 Smart；未命中 → 走 Flash（仍挂 `escalate_to_scheduler` 作兜底） | 任意 | 在前两者之间 | 中等 |

**注意**：策略选什么跟 Smart 选什么强耦合。Smart=Opus 时 `conditional_flash` 划算；Smart=Sonnet 时 `always_smart` 可能反而便宜且稳定。需要数据驱动选择（见 2.5）。

### 2.2 Prompt 拆分

现在的 6 段 section 要能按"面向 Flash / 面向 Smart"切两个子集：

| Section | Flash (人格) | Smart (工具) |
|---|---|---|
| IDENTITY | ✅ | ❌ |
| USER_MODEL | ✅（完整，含画像） | ✅（精简版：年龄 + 时区 + 核心约束） |
| SYSTEM_MECHANICS | ❌ | ✅ |
| COMMUNICATION | ✅ | ❌ |
| PROTOCOLS | ❌（Phase 3 整体删掉，POC 期间不带） | ❌ |
| TOOLS_SECTION | ❌（只留 `escalate_to_scheduler` 的极简说明） | ✅ |

实现：`PromptParts` 加两个方法 `to_chat_only()` / `to_schedule_only()`，各返回一个只含相关 section 的 PromptParts。

**注意**：现在的 PromptParts 是"4 层 cache block"结构，切子集时要保持 cache 友好。最简单做法：Flash 和 Smart **各自维护独立的 cache**，初期不追求 cache 命中率（反正是 POC），后续再优化。

### 2.3 配置 + 实现

**`config.py`**：
```python
DISPATCH_MODE = os.getenv("DISPATCH_MODE", "off")  # "off" | "on"
ESCALATE_STRATEGY = os.getenv("ESCALATE_STRATEGY", "always_smart")  # "conditional_flash" | "always_smart" | "rule_based"
CHAT_MODEL_LITE = os.getenv("CHAT_MODEL_LITE", "gemini-2.0-flash-lite")
SCHEDULE_MODEL_SMART = os.getenv("SCHEDULE_MODEL_SMART", "claude-sonnet-4-6")
```
默认值取 `always_smart + Sonnet` 是基于 2.5 估算之前的初步判断，启动 POC 前用真实数据校正。

**新文件 `bot/ai_engine_dispatch.py`**：
- `class DispatchEngine`：组合一个 Flash engine + 一个 Smart engine
- `async def chat(db, messages, send_cb)`：先 Flash，检测 `escalate_to_scheduler` 工具调用，转 Smart
- `async def scheduled_action(...)`：直接走 Smart（proactive/reminder 不需要人格层）

**`bot/ai_engine.py`**（现 router）：根据 `DISPATCH_MODE` 返回 `DispatchEngine` 或原 single engine。

**`bot/tools.py`**：加 `escalate_to_scheduler` 工具定义（`user_request: str`, `context: str`）。这个工具只在 DISPATCH_MODE=on 时注册给 Flash。

### 2.4 日切 AB 脚本

或者更简单：用户每天晚上手动改 `.env` 重启容器一次。POC 阶段不必自动化。

### 2.5 Phase 2 启动前：数据先估算

**动机**：三种 `ESCALATE_STRATEGY` + 三种 Smart 模型组合起来是 9 种搭配，跑盲测浪费时间。先用历史 messages 做离线估算收敛默认值。

**步骤（一次性脚本，落地到 `scripts/estimate_dispatch_cost.py`）**：
1. 从 `messages` 表导出最近 7-14 天的 `role='user'` 消息（跳过系统/AI 消息）
2. 对每条消息做三种标注：
   - **关键词命中**：regex 匹配"记一下|帮我|明天|提醒|几点|deadline|日程|安排|todo"等。命中 = 需要 Smart
   - **Flash 模拟判断**：跑一次 Flash Lite，给 `escalate_to_scheduler` 工具，看它调不调（小样本跑，比如 100 条够了）
   - **人工 spot check**：随机抽 30 条人工判断"是否真的需要工具"作为 ground truth
3. 套三种策略的 token 单价计算月成本矩阵：
   ```
              conditional_flash   always_smart   rule_based
   Opus       $X.XX              $X.XX          $X.XX
   Sonnet     $X.XX              $X.XX          $X.XX
   Gemini Pro $X.XX              $X.XX          $X.XX
   ```
   （每个 cell 附带漏记概率估计）
4. 输出一份 markdown 报告到 `plans/dispatch-cost-estimate.md`，用户据此决定默认组合
5. 同时输出：Flash 的 escalate 判断准确率（漏 escalate 的占比、乱 escalate 的占比）→ 决定 `conditional_flash` 是否可用

**做这一步的前提**：messages 表里有足够真实对话样本（已经在持续累积，随时可跑）。

### 2.6 指标收集

每轮对话记录到一张新表 `dispatch_metrics`（或直接写 JSONL 日志）：
```
timestamp, dispatch_mode, flash_tokens_in, flash_tokens_out, smart_tokens_in, smart_tokens_out,
tool_calls_count, total_latency_ms, escalated (bool), fallback_triggered (bool)
```

用户观察 2 周后决定：`DISPATCH_MODE` 默认改 on 还是 off，或者需不需要调整 escalate 策略。

### 2.7 Phase 2 实施步骤

1. `PromptParts` 加 `to_chat_only()` / `to_schedule_only()`
2. `bot/tools.py` 加 `escalate_to_scheduler` 工具
3. `bot/ai_engine_dispatch.py` 新建：组合 Flash + Smart
4. `config.py` + `.env.example` 加 `DISPATCH_MODE` 等变量
5. `bot/ai_engine.py` 路由器里加分支
6. `bot/discord_bot.py::on_message` 保持不动（engine 层透明）
7. 指标日志：最简版用 structured log（不建新表）
8. 手动测：
   - 纯聊天消息 → 只有 Flash 调用、无 escalate
   - "帮我记一下今天做了 X" → Flash escalate → Smart 调 `log_timeline_event` → Flash 转述"好，记了"
   - Flash 错误不调 escalate（漏记） → 观察后续能不能用户补救
   - 超时 fallback → 降级到 Smart 单跑
9. Commit：
   - `feat(prompts): promptparts chat/schedule subset views`
   - `feat(engine): dispatch engine with flash+smart routing`
   - `feat(config): dispatch_mode and escalate_strategy flags`
   - `chore(metrics): log dispatch token stats`
   - `chore(scripts): offline cost estimator for escalate strategies`

### 2.8 风险

- **策略 1 `conditional_flash`**：Flash 漏 escalate / 乱 escalate。缓解：初期给 few-shot 例子；2.5 数据估算会先暴露 Flash 判断力
- **策略 2 `always_smart`**：纯闲聊也付 Smart 钱，长期成本看用户聊天频率。缓解：精简 Smart 的 prompt，保 cache 命中率
- **策略 3 `rule_based`**：regex 漏匹或误匹。缓解：规则里加"兜底 Flash 仍挂 escalate 工具"，双保险
- **通用**：双 API 调用延迟 +50%~100%。escalate 时可以让 Flash 先回一句"帮你记一下…"争取感知时间
- **通用**：Cache 命中率短期下跌。POC 稳定后再做 prompt cache 优化
- **通用**：两层模型错误互相掩盖。完整 tool call / response 打进日志，方便事后归因

---

## Phase 3：删除 Role B / 启动困难指导员（优先级 6）

**前提**：用户已自测 "删 PROTOCOLS 段后聊天质量没变甚至更好"。

### 3.1 改动范围

**`bot/prompts.py`**：
- 删除 `PROTOCOLS` section 常量和相关 block 拼接（以及对应的去临床化 4 个信号：深度专注 / 迈不出第一步 / 高耗宕机 / 时间感偏移）
- `PromptParts` 删 `protocols` 字段
- `build_prompt()` 不再拼 protocols
- 6 段 → 5 段：IDENTITY / USER_MODEL / SYSTEM_MECHANICS / COMMUNICATION / TOOLS_SECTION

**`PROACTIVE_PROMPT` / `BEDTIME_PROMPT` / `MORNING_PROMPT`**：
- 去掉"观察拖延/状态信号"相关语句
- 语气保持"问候 + 询问现在在做什么"，不加"激励 / 建议 / 提醒你别再拖了"
- 如果用户现在发牢骚，AI 只做"听着、嗯嗯回应"，不主动给建议

**`bot/ai_engine_base.py`**：
- `_build_prompt` 如果有 protocols 相关的条件注入，一并删

**CLAUDE.md**：
- 项目级 `.claude/CLAUDE.md` 里"6 个正交 section"改成"5 个正交 section"
- PROTOCOLS 那一行删除

### 3.2 Phase 3 实施步骤

1. grep 所有 `PROTOCOLS` / `protocols` 的引用位置
2. 按上面改动范围逐个删
3. 跑几天观察聊天质量和轮询感觉
4. 更新 `.claude/CLAUDE.md`
5. Commit：
   - `refactor(prompts): drop PROTOCOLS section and procrastination coaching language`

### 3.3 风险

- 删掉后 AI 遇到用户发牢骚时可能"冷处理"：先观察，必要时在 COMMUNICATION 加一句"用户抱怨/发牢骚时只回应情绪，不要给建议"

---

## Phase 4：精力槽（优先级 3，研究驱动）

### 4.1 立题

用户原话（inspiration #3）：
> 添加精力槽模式，我会写一些我觉得会把我整个精力槽清空的事件，或者好几个事叠加清空精力槽的事件。给 AI 一个工具，在安排我的时间和精力之前看看是否合理。
> 但是这个还需要调研，因为感觉会需要一些得出结果，然后收到反馈，然后调整结果，再做出预测的一些像是回归分析的东西。

### 4.2 设计思路（按用户自述重组）

**核心原则**（用户原话）：
- 不想给活动赋具体数字，**只想排序**（"吃饭移到睡觉上面"）
- 每天自己填**今天精力满值**（比如 7/10）
- 选今天要安排的活动**只用名字**（比如"吃饭""睡觉""学习"），app 自动按排序扣减
- 自知"很少老老实实计划一切" → 工具必须低摩擦，不能每天逼着填

这不再是"AI 自动匹配 events 估算"的思路，而是**前端为主 + 拖拽交互 + 手动触发**的工具。

---

**方向 A（本 Phase 首选）：排序式 palette + 前端手动 planner**

#### 数据建模

```sql
-- 活动 palette（全局，用户维护）
CREATE TABLE energy_activities (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL UNIQUE,
    rank_pos   REAL    NOT NULL,            -- 排序位置，越大越耗能（用 REAL 允许插入到两个现有之间）
    created_at TEXT    DEFAULT (datetime('now'))
);

-- 每日精力槽（每天一条）
CREATE TABLE daily_energy (
    date       TEXT    PRIMARY KEY,         -- YYYY-MM-DD
    max_energy REAL    NOT NULL,            -- 用户自评的今日满值 0-10
    notes      TEXT,
    updated_at TEXT    DEFAULT (datetime('now'))
);

-- 每日活动计划（拖进 Today 区的活动）
CREATE TABLE daily_energy_plan (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT    NOT NULL,
    activity_id INTEGER NOT NULL REFERENCES energy_activities(id),
    count       INTEGER NOT NULL DEFAULT 1, -- 允许同 activity 多次（如多顿饭）
    created_at  TEXT    DEFAULT (datetime('now'))
);
```

**rank_pos 用 REAL 的好处**：拖拽插入到"吃饭（2.0）和睡觉（1.0）中间"时，直接塞 1.5，不用 rebalance 整张表。

#### 成本归一化

rank_pos → cost 的映射只在显示和计算时用，不存 DB：
- 方案 a（线性）：最小 rank 映射到 cost 0，最大 rank 映射到 cost max_energy（上限 10）
- 方案 b（只显示条形，不给数字）：前端只显示 "相对耗能条" 的长度，忠于"我只会排序不会打分"的直觉

**倾向方案 a**，因为 Today 区需要显示"剩余 = max - sum(cost)"给具体数字。

#### 前端交互（新 tab `EnergyView`）

```
┌──────────────────────┬───────────────────────────────┐
│ Palette (活动库)     │ Today (2026-04-23)            │
│ ┌───────────────┐    │                               │
│ │ 睡觉          │    │ 满值 [ 7 ] /10  [保存]        │
│ │ 吃饭          │    │                               │
│ │ 散步          │    │ ━━━━━━━━━━ 剩余 2.5           │
│ │ 跟朋友聊天    │    │                               │
│ │ 看剧          │    │ 今天安排：                    │
│ │ 写代码 1 小时 │    │  - 吃饭 ×3  (cost 1.5)        │
│ │ 学一整天      │    │  - 写代码 1 小时 (cost 3)     │
│ └───────────────┘    │                               │
│ [+ 新增活动]         │                               │
└──────────────────────┴───────────────────────────────┘
```

交互细节：
- 左侧可上下拖重排（=> 更新 rank_pos）
- 左侧拖到右侧 Today 区 → 加一条 plan
- 右侧条目点击 + / - 调整 count，或删除
- 右侧顶部输入 max_energy 触发保存到 `daily_energy`
- 剩余 < 0 时条变红提示

#### AI 端（最小介入）

**只加一个 read-only 工具** `get_today_energy_status(date?)`，返回：
```json
{"max": 7, "planned_cost_sum": 4.5, "remaining": 2.5, "activities": ["吃饭 ×3", "写代码 1 小时"]}
```

`TOOLS_SECTION` 加一句指引："用户安排未来事件（`status='planned'`）前，可以调 `get_today_energy_status(date)` 看看那天 remaining 够不够。如果超 → 提醒用户"。

**不新增写工具**。活动库维护、日计划填写都是前端纯手动（这也是用户明确要求"工具低摩擦"的体现）。

---

**方向 B（观望）：自动关联 timeline event**

把实际 logged events 自动跟 activity palette 做 fuzzy 匹配 → 自动消耗 budget，用户不用手动拖。

**不做的理由**：用户原话"很少老老实实计划一切"。连拖 activity 都嫌累的用户，自动匹配反而引入错误匹配的风险（"学半小时"被算成"学一整天"）。

**做的条件**：方向 A 跑 2-4 周，若用户拖动活跃度高且 pattern 稳定，再考虑加自动匹配。

---

**方向 C（你问的"回归分析"，简单解释）**

**原理**：
- 你每天记"今天做了什么" + "今天整体感觉几分（1-10）"
- 坚持 30-60 天后，程序用数学拟合出系数：比如 "每次吃饭 -0.5 分、每小时学习 -0.8 分、每小时睡眠 +0.5 分"
- 这些系数**自动替代**方向 A 里你手动排的 rank_pos，不用你再维护活动 palette

**技术是什么**：**线性回归**。sklearn 几行代码就能跑：
```python
X = [[睡觉时长, 学习时长, 吃饭次数, ...], ...]  # 每行 = 一天
y = [今天精力打分, ...]                        # 每行 = 那天你给自己打的分
model = LinearRegression().fit(X, y)
# model.coef_ 就是每个活动对精力的影响系数
```

输出：每个活动自动得到个人化的 cost 系数，比你手动排序更精准。

**现在不做的理由**：
1. 前提是"每天打分"。你自述"很少老老实实计划一切" → 打分大概率也坚持不下去
2. 冷启动期（前 30-60 天）没数据，期间还是要靠方向 A
3. 方向 A 的排序已经够表达"哪些活动更耗能"这个粗粒度信息，回归主要是解决"精确到 0.1 分"的问题——但你说你不在乎精确数字，只在乎相对排序

**做的条件**：方向 A 跑半年以上且你自己愿意每天打 1-10 分；否则直接跳过。

---

**方向 D（远期）：睡眠 / recovery 因子自动推算 max_energy**

- 引入昨日消耗 → 今天 max 自动衰减
- 睡眠时长加成、chill 活动回血
- 需要睡眠数据来源（HealthKit 接入 or 手动填）

**不做的理由**：每天让用户自己填 max_energy 已经够了，自动推算引入的复杂度 >> 收益。

---

### 4.3 本 Phase 只做方向 A

**实施步骤**：
1. `bot/database.py` 加 3 张表（`energy_activities` / `daily_energy` / `daily_energy_plan`）+ CRUD
2. `api/server.py` 加路由：
   - `GET/POST/PATCH/DELETE /api/energy/activities`（palette 维护，PATCH 用于改 rank_pos）
   - `GET/PUT /api/energy/daily/:date`（每日 max_energy）
   - `GET/POST/PATCH/DELETE /api/energy/plan/:date`（每日计划）
3. 前端 `EnergyView` 组件：
   - Palette 区（drag-drop 重排 + 新增 / 删除）
   - Today 区（max 输入 + drop zone + count 调整 + 剩余条）
   - 可以用 `@dnd-kit/core` 或 `react-beautiful-dnd` 做拖拽
4. `bot/tools.py` 加 read-only 工具 `get_today_energy_status`
5. `bot/prompts.py` TOOLS_SECTION 加使用指引
6. 手动测：
   - 新增 5 个活动，拖拽排序
   - 填今天 max = 7
   - 拖 3 个活动进 Today，count 调整
   - 验证剩余数字正确（max - sum(count × cost)）
   - 跟 AI 说"明天我安排一整天学习"，AI 应调 `get_today_energy_status(明天)` → 结合 max 和剩余判断是否提醒用户

**工作量估计**：2-3 个工作日（前端 drag-drop 占大头）。

---

### 4.4 待观察 / 未决

- 用户会不会真的每天打开 `EnergyView` 拖活动？→ 方向 A 成败的根本。跑 2 周观察使用频率
- rank_pos → cost 的归一化公式：线性 vs 非线性（相邻高耗能活动的差距应该更大？）
- 超支时只前端红条提示，还是也让 bot 主动 ping？倾向只前端（bot 主动会让用户反感）
- 是否让 bot 每天睡前问"今天实际做了什么" → 反向填 `daily_energy_plan`？（独立的睡前互动，不依赖已推翻的 daily_summary 路径）
- `daily_energy.max_energy` 留空时的默认值：上周均值 or 固定 7？

---

## 总时间估计（粗）

| Phase | 预估工作量 | 备注 |
|---|---|---|
| Phase 2 | 4-6 个工作日 | 离线成本估算脚本 + 双模型 engine + prompt 拆分 + 三策略切换 + AB 指标 |
| Phase 3 | 半天 | 主要是 prompt 删除和验证 |
| Phase 4 MVP | 2-3 个工作日 | 方向 A：3 张表 + API + 前端 drag-drop palette + read-only 工具 |

顺序建议：**Phase 3 → Phase 2 → Phase 4**。

理由：
- Phase 3 早做的话，可以在 Phase 2 POC 的 prompt 拆分里直接用 5 段结构，不用后期再改
- Phase 2 是最大改动，留在 Phase 3 稳定之后做
- Phase 4 相对独立，最后加

---

## 未决事项

**Phase 2**
- 默认 `ESCALATE_STRATEGY` + Smart 模型的组合 → 由 2.5 离线估算脚本决定，启动 POC 前拍板
- `conditional_flash` 的 escalate 实现：Flash 工具调用 vs 输出特殊标记？建议前者
- AB 切换频率：按天翻转 vs 按周翻转？按天样本多但噪声大；按周更稳定但观察慢。倾向按天
- `always_smart` 策略下 Smart 返回结构体 `{tool_result, chat_suggestion}` 的协议怎么定？需要 Smart 被 prompt 约束输出固定 JSON 格式，失败兜底策略待定

**Phase 4**
- rank_pos → cost 归一化用线性还是非线性？本 MVP 用线性，观察用户反馈
- `daily_energy.max_energy` 留空时默认 7 还是上周均值？
- 是否把"睡前问今天做了什么反向填 plan"做成一个互动？（可以，但不在本 phase 做）
