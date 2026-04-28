# Dispatch POC：双层模型架构（隔离测试频道）

> 创建：2026-04-28
> 关系：是 [`2026Q2-consolidation.md::Phase 2`](2026Q2-consolidation.md) 的细化设计 + 验证方法论。Phase 2 总纲列了 3 种 `ESCALATE_STRATEGY`，本 POC 收敛为"关键词 pre-filter + 小模型 decide"混合策略，并在独立 Discord 频道隔离验证，**不动 prod 主路径**。

---

## 目标

在不影响 prod 主路径的前提下，验证 Flash（小模型）+ Smart（大模型）双层 dispatch 架构能不能跑得动。

为什么要做：成本压缩 + 让小模型只承担它能稳定承担的事（语气/闲聊），把"日期推理 / 工具决策 / 状态判断"这些它会出错的事关给大模型。

为什么先做 POC：记忆里 [`feedback_realtime_no_small_models.md`](../../../.claude/projects/-Users-chachaya-dev-life-tracker-life-tracker/memory/feedback_realtime_no_small_models.md) 写明了"realtime 主路径禁用小模型"，是用户实测过 small model 日期推理常错后立的红线。这条记忆在本 POC 期间**继续对 prod 生效**。POC 验证通过后才讨论是否动 prod。

---

## 测试基础设施：第二个 bot 进程

走 `2026Q2-consolidation.md::Phase 2` 之外的最简方案——零代码改动 prod，纯环境隔离：

- 新建一个 Discord Application + bot token
- 把测试 bot 邀请进一个**专用测试频道**（与 prod 频道完全分离）
- 跑一份独立进程：

  ```bash
  DISCORD_TOKEN=<test_bot_token> \
  DB_PATH=data/life_tracker_test.db \
  API_PORT=8001 \
  AI_PROVIDER=<test_provider> \
  python main.py
  ```

- prod 进程完全不受影响

工作量 ≈ 30 分钟（建 Discord App + 抄一份 .env + docker-compose 加一个 service）。

**target_channel_id 自动认领机制**（`bot/discord_bot.py:86-88`）保证测试 bot 第一条消息进的频道就是它的归属，不与 prod bot 冲突。

---

## 数据流

```
user_msg
    │
    ▼
[读取 escalate_state]  per-channel 会话级状态，默认 close
    │
    ├─ open ──► BIG_WORKER ──► SMALL_PARAPHRASE ──► reply
    │   (跳过 DECIDE 和 pre-filter，多轮信息收集 / 连续工具操作期间)
    │   BIG_WORKER 输出 [ESCALATE_STATE]=close 时翻回 normal
    │
    └─ close ──► [关键词 regex pre-filter]
                    │
                    ├─ 命中 ──► BIG_WORKER ──► SMALL_PARAPHRASE ──► reply
                    │  (提醒|记一下|别忘了|几分钟后|分钟后叫|半小时后|小时后)
                    │
                    └─ 未命中 ──► SMALL_DECIDE
                                    │
                                    ├─ escalate ──► BIG_WORKER ──► SMALL_PARAPHRASE ──► reply
                                    │
                                    └─ 直接回复 (闲聊/情绪/persona 反应) ──► reply
```

关键设计原则：

- **`escalate_state` 用于多轮粘滞**。一次 escalate 涉及多个 turn 时（多轮信息收集、连续工具操作），把状态做成 per-channel 持久标志，避免每轮都让小模型重新判断 escalate（这是它最弱的环节）
- **状态翻转完全由 BIG_WORKER 控制**——它每轮输出 `[ESCALATE_STATE]` 字段决定下一轮走哪条路；缺省视为 close（fail-safe）
- **open 期间仍过 SMALL_PARAPHRASE**（不是直连大模型对用户说话）。语气始终是小模型，**对话风格不切换**，这是选 Option (b) 而非 Option (a) 的理由
- **关键词 pre-filter 仅在 close 状态生效**。避免在已 escalate 的会话里又被关键词重复触发产生绕路
- **关键词清单要精确**，避免回顾性语境误触发（如"上次你提醒我那个事真好"）

---

## 4 份 Prompt

| Prompt | 用途 | 内容 |
|---|---|---|
| `SMALL_DECIDE` | 小模型第一次 call，决定 escalate or 直接回复 | IDENTITY + USER_MODEL + COMMUNICATION + escalation 触发清单 + "不命中就按人格回复" |
| `SMALL_PARAPHRASE` | 小模型第二次 call，给大模型产出包语气 | IDENTITY + USER_MODEL + COMMUNICATION + "下面是要发给用户的事实，调成日和的语气，**数字/时间/动作不许改**" |
| `BIG_WORKER` | 大模型，做工具决策和执行 | SYSTEM_MECHANICS + TOOLS_SECTION + 精简 USER_MODEL（事实部分）+ 完整状态注入（memories / ongoing / deadlines / planned / pending_reminders / weather）+ 输出格式约束（含 `[ESCALATE_STATE]` 字段） |
| `ESCALATION_TRIGGER_LIST` | 嵌入 SMALL_DECIDE 内 | 5-8 条具体场景白话描述（见下文「实施步骤 0」） |

**为什么 SMALL_PARAPHRASE 不带 escalation 清单**：第二次 call 已经决定好走大模型了，不需要再判断 escalate；prompt 越短小模型越聚焦"包语气"这一件事。

**PROTOCOLS 4 信号怎么处理**：状态识别（深度专注/迈不出第一步/高耗宕机/时间感偏移）放小模型——这是人格判断；识别到状态后**强制 escalate**，让大模型决定要不要落工具（查记忆、设提醒等）。

---

## 协议契约：BIG_WORKER 输出格式

大模型的最终输出**结构化三段**：

```
[ESCALATE_STATE]
close      # 任务一次性完成，下一轮回到 SMALL_DECIDE 流程
           # 或填 open：要继续抓信息 / 接着干，下一轮直接进 BIG_WORKER

[ACTIONS]
- set_reminder(5min, 喝水)
- list_reminders 复查无重复

[FACTS_TO_CONVEY]
- 已设 5 分钟后提醒喝水
- 没有重复提醒
```

- `ESCALATE_STATE` → 控制 escalate_state 翻转；缺省视为 `close`（fail-safe，避免"忘记关"导致一直贵）
- `ACTIONS` → 进 log / test_mode JSONL，**给用户做审计/调试用**，不进小模型
- `FACTS_TO_CONVEY` → 是小模型唯一看到的内容；SMALL_PARAPHRASE 把这些事实串成人话发出去

**为什么这样切**：

- 用户原诉求："输出做了什么会让我更放心" → ACTIONS 满足
- 用户另一诉求："不要给大模型加太多语气处理要求" → BIG_WORKER 完全不管语气
- 我（assistant）的担忧："小模型自由发挥会丢/改事实" → FACTS_TO_CONVEY 是结构化清单，漏掉一项肉眼可见，比给小模型自由文本更可控

**SMALL_PARAPHRASE 的 messages 入参格式**：

```python
[
    *recent_history,
    {"role": "user", "content": user_msg},
    {"role": "system" 或 fake assistant, "content":
        f"[内部] 大模型已处理完毕，建议回复内容：\n{facts_to_convey}"},
]
```

不要让小模型看到大模型的 tool call 细节和 tool_result——它会被这些信息污染开始解释技术细节。只给"应当说什么"，不给"做了什么"。

---

## Escalate State 生命周期

`escalate_state` 是 per-channel 的会话级状态（POC 用 SQLite `state` 表，key = `escalate_state:<channel_id>`，值 `open` / `close`，默认 `close`），用于支持多轮信息收集和连续工具操作。

**典型用例**——多轮信息收集：

```
turn 1:  user: "明天去看医生"
         escalate_state=close → SMALL_DECIDE → escalate
         BIG_WORKER:
           [ESCALATE_STATE] open
           [ACTIONS] log_timeline_event(status=planned, content=看医生, notes=时间地点 TBD)
           [FACTS_TO_CONVEY] 还不知道几点哪家医院
         SMALL_PARAPHRASE → "诶几点的呀？哪家医院？"
         escalate_state ← open

turn 2:  user: "11 点中央医院"
         escalate_state=open → 跳过 DECIDE，直接 BIG_WORKER
         BIG_WORKER:
           [ESCALATE_STATE] open
           [ACTIONS] update_timeline_event(time, location)
           [FACTS_TO_CONVEY] 还差通勤时间
         SMALL_PARAPHRASE → "那走过去要多久？"
         escalate_state 持平 open

turn 3:  user: "走路 20 分钟"
         escalate_state=open → 跳过 DECIDE，直接 BIG_WORKER
         BIG_WORKER:
           [ESCALATE_STATE] close
           [ACTIONS] update_timeline_event(notes), set_reminder(出门提醒)
           [FACTS_TO_CONVEY] 已记 11 点中央医院、20 分钟通勤、设了 10:30 出门提醒
         SMALL_PARAPHRASE → "好，10 点半提醒你出门"
         escalate_state ← close
```

**保护机制**（防卡死在 open）：

- **超时强制 close**：open 状态下无新消息超过 N 分钟（默认 10 min）→ 下一条进来 reset 为 close 起点
- **轮次上限强制 close**：连续 K 轮（默认 10）BIG_WORKER 都没 close → 第 K+1 轮强制视为 close
- **缺省 close**：BIG_WORKER 输出里没 `[ESCALATE_STATE]` 字段 → 视为 close
- **进程重启**：state 表持久化，重启不丢；但启动时若发现 open 已超时直接 reset

---

## 工具分配

| 工具 | 谁能调 | 备注 |
|---|---|---|
| 全部 10 个工具 | BIG_WORKER | 维持现状 |
| 无 | SMALL_DECIDE | 决策完直接回 escalate signal 或人话 |
| 无 | SMALL_PARAPHRASE | 只产出最终回复文字 |

不让小模型直接调任何工具，简化 POC 测试面。如果以后发现 set_reminder / log_timeline_event 这种高频低风险工具放在小模型能显著降延迟，再迭代。

---

## 失败模式与监控

| 失败 | 风险等级 | 监控方法 |
|---|---|---|
| **False negative**：该 escalate 没 escalate（漏记提醒、漏更新时间线） | 高危——静默故障 | 离线扫日志，凡用户消息含"提醒/记一下/帮我看看/别忘了/几分钟后"等关键词但当轮没 escalate → 标记审计 |
| **False positive**：不该 escalate 也 escalate | 低——只浪费一次大模型 call | 不主动监控，看 token 成本 |
| **Summary 失真**：小模型改了 FACTS 里的数字/时间/事实 | 中——错误用户能感知但不一定能发现 | 离线对比 SMALL_PARAPHRASE 输出 vs FACTS_TO_CONVEY，diff 关键词（数字、时间表达、人名）漂移 |
| **延迟劣化**：双层 hop 让简单提醒从 1-2s → 3-5s | 中——UX 影响 | 记录 e2e latency；Discord typing indicator 自带覆盖，不加"稍等"预声明 |
| **Stuck open**：BIG_WORKER 永不 close → 每轮都贵 | 中——成本累积 | 监控连续 open 轮次；超阈值（K=10）强制 close + 告警 |
| **Premature close**：BIG_WORKER 半路 close 把多轮任务扔回 SMALL_DECIDE | 高——容易漏 escalate 静默故障 | 监控 close 后下一轮 SMALL_DECIDE 是否又 escalate；不该 close 的样本进审计集 |
| **Ping-pong**：open/close/open/close 在几轮内反复横跳 | 低——浪费但不出错 | 计数翻转频率，单 session > N 次警告 |

新增数据：BIG_WORKER 的 ESCALATE_STATE / ACTIONS / FACTS_TO_CONVEY 全量进 test_mode JSONL，方便事后审计和 diff。

---

## 实施步骤

**整体策略**：本地 API 先验证 → 通过再上 Discord。前 4 步全程不需要 bot 进程。

### 步骤 0（先做）：离线标注 escalation 触发样本

**先做这一步，不要直接写 prompt**。从 prod 的 `bot/test_mode.py` 抓的 JSONL（已盘点：26 个文件，226 条唯一用户消息）里抽样人工标"该 escalate / 不该 escalate"。建议抽样：

- 必标层：23 条关键词命中样本（"提醒/记一下/明天/deadline" 等显式工具意图）
- 采样层：从 203 条灰区里随机抽 30 条（闲聊/情绪/状态信号/弱意图）
- 共 ~50 条，作为 SMALL_DECIDE 的回归集

产出：

- `scripts/extract_dispatch_samples.py`：从 JSONL 抽样 + 输出 markdown 标注表（含当时 AI 的实际响应作为 ground-truth proxy）
- `plans/2-specs/dispatch-escalation-triggers.md`：标注结果 + 触发清单白话总结

### 步骤 1：4 份 prompt 草稿

依据步骤 0 的标注集写：

- `bot/prompts.py` 加 `SMALL_DECIDE` / `SMALL_PARAPHRASE` / `BIG_WORKER` 三个常量（或拆 `prompts_dispatch.py`）
- ESCALATION_TRIGGER_LIST 用白话写在 SMALL_DECIDE 内
- BIG_WORKER 输出格式约束：明确要求三段 markdown（`[ESCALATE_STATE]` / `[ACTIONS]` / `[FACTS_TO_CONVEY]`），SMALL_PARAPHRASE 只读 FACTS

### 步骤 2：dispatch engine

- 新文件 `bot/ai_engine_dispatch.py`
- `class DispatchEngine`：组合 Flash + Smart
- `chat()` 方法实现：读 escalate_state → 若 open 直接 BIG_WORKER；若 close 走 pre-filter → SMALL_DECIDE → BIG_WORKER → SMALL_PARAPHRASE 的完整数据流
- escalate_state 持久化：复用 `db.get_state` / `db.set_state`（K-V 表），key = `escalate_state:<channel_id>`
- BIG_WORKER 输出三段解析（约束 prompt + 容错 parser，缺字段 default close）
- 超时/轮次保护机制（10 min / 10 turn）
- 失败兜底：任何一层失败都直接告诉用户"这次没记住哦，再说一遍"——不再降级到 single engine

### 步骤 3：离线 replay 验证（**核心验证步骤**）

不需要 bot，纯 API 调用。落地：`scripts/dispatch_replay.py`

**输入**：fixture JSONL（每行一条 user_msg + ts + 可选 expected_escalate）。POC 期至少两份 fixture：

- `fixtures/4-21-grades.jsonl`：4-21 那段聊作业分数的多轮对话（验证多轮信息收集 + escalate_state 翻转）
- `fixtures/escalation-50.jsonl`：步骤 0 标注的 ~50 条样本（验证 SMALL_DECIDE 准确率）

**每轮要 log 的维度**：

| 维度 | 内容 |
|---|---|
| 路由 | 当前 escalate_state / regex 命中 / SMALL_DECIDE 输出 / state 翻转记录 |
| 模型间通讯 | SMALL_DECIDE / BIG_WORKER / SMALL_PARAPHRASE 各自的 prompt + response 全文 |
| Tool 调用 | name / args / result / 第几轮 |
| Token 用量 | 每个 API call 的 input / output / **cache_read** / **cache_creation** |
| Cache 命中 | Anthropic 的 `cache_read_input_tokens` / Gemini 的 `cached_content_token_count`，两家都直接给 |
| 延迟 | 每个 API call 的 wall clock + per-turn e2e |
| FACTS 失真 | SMALL_PARAPHRASE 输出 vs FACTS_TO_CONVEY 的关键词 diff（数字/日期/人名漂移检测） |

**输出**：

- `data/replay_logs/<run_id>/trace.jsonl`：每轮一条详细 trace
- `data/replay_logs/<run_id>/report.md`：汇总 cache hit %、平均 latency、SMALL_DECIDE 准确率、FACTS 失真率

**通过条件**（待步骤 5 的实测调整）：

- 4-21 fixture 跑下来 escalate_state 翻转符合预期，无 stuck open / premature close
- escalation-50 fixture SMALL_DECIDE 准确率 > 85%
- FACTS 关键词漂移 < 5%

**不通过则迭代 prompt 重跑**——这是 POC 的主战场。

### 步骤 4：测试基础设施（第二 bot 进程）

仅当步骤 3 离线验证通过后再做。

- 建第二个 Discord App + bot token
- 邀请进测试频道
- 抄 `.env` 改成 `DISCORD_TOKEN_TEST` / `DB_PATH=data/life_tracker_test.db` / `API_PORT=8001` / `DISPATCH_MODE=on`
- docker-compose 加一个 `bot-test` service（或本地直接跑）
- 验证：测试 bot 能收发消息、写入测试 DB、prod bot 完全不受影响

### 步骤 5：路由 + 配置

- `config.py` 加 `DISPATCH_MODE=on/off`，**测试 bot 进程默认 on，prod 进程保持 off**
- `bot/ai_engine.py` 路由器加分支：`DISPATCH_MODE=on` → DispatchEngine，否则原引擎

### 步骤 6：实测 2 周

- 每天在测试频道用真实场景跑（设提醒、log 时间线、问历史、闲聊、状态信号、多轮信息收集）
- 对比：相同 input 在 prod 主路径 vs 测试频道的输出差异
- 监控失败模式表所有维度

### 步骤 7：决策

- 双 e2e latency 可接受 + FACTS 失真率 < X% + escalation 准确率 > Y% + escalate_state 健康（无 stuck/premature）→ 讨论推 prod 路径
- 否则归档本 spec，保留经验教训

---

## 未决事项

- **关键词正则的精确范围**：先列一版，标注集校验 false-trigger 率，迭代
- **escalation 触发清单具体内容**：步骤 0 的产出
- **小模型选什么**：Gemini Flash Lite vs Sonnet Haiku 等，等步骤 0 后再选
- **大模型选什么**：Sonnet 还是 Opus；POC 期 Opus 保稳
- **ACTIONS / FACTS_TO_CONVEY / ESCALATE_STATE 是 markdown 还是 JSON**：JSON 更稳定但 prompt 复杂；markdown 软规约容易让模型自由发挥；POC 先用 markdown，失真高再切 JSON
- **escalate_state 自动 close 阈值**：N min（默认 10）/ K 轮（默认 10），实测调整
- **escalate_state 翻转粒度**：是否要在 BIG_WORKER 提示里给"什么情况该 open / 什么情况该 close"明确清单，还是让模型自由判断；POC 先给清单防止判断飘
- **状态信号（PROTOCOLS）放小模型识别还是 prompt 里完全删除**：本 spec 倾向"小模型识别 → 强制 escalate"，但识别准确率未知
- **决策门槛具体数字**：失真率、准确率、延迟的 threshold 等数据出来后定
- **通过后推 prod 的路径**：env flag、灰度、A/B？留到决策时再讨论

---

## 已明确放弃的方向

- **Post-check 关键词兜底**：会出现 bot 双发，UX 不能接受
- **小模型直接产出 final reply**（不走大模型路径）：用户实测日期推理常错（见记忆 `feedback_realtime_no_small_models.md`），prod 路径不接受
- **大模型直接产出 final_reply 给小模型润色**：assistant 一开始建议过，被用户调整为"大模型只产出做了什么 + 事实清单"。理由：用户想要 audit 性，不想给大模型加语气负担
- **小模型做多轮信息收集**（"看医生 → 自动追问几点哪里通勤"）：会把工具手册搬回小模型 prompt，违背"小模型只做语气、大模型管工具"的核心切分。改为 escalate_state=open 让大模型多轮做这件事
- **Option (a) escalate 期间跳过 SMALL_PARAPHRASE，让大模型直接说话**：会让对话风格在 escalate / 非 escalate 之间明显切换，破坏小模型作为唯一"声音"的一致性。**确定走 Option (b)**：escalate 期间也过 SMALL_PARAPHRASE，只跳过 SMALL_DECIDE
