# Classifier 分流架构：轻量路由 → 专用处理器

## Context

当前架构里，所有用户消息都进同一个 `chat()`，走 Opus 4.6 + 完整工具集 + 4-level cache。优点是统一、cache 命中率高；缺点是「今天吃了沙拉」这种结构化记录也用 Opus 打，cost 高。

用户提出新架构：入口加一个便宜的 classifier 小模型，按意图路由到不同的专用处理器——饮食/习惯用轻量模型、焦虑干预用更大模型、普通聊天走现有路径。用户的顾虑是「多标签会 token 爆炸」，已决定走**单标签 + 层级路由 escalation**，由下游处理器在必要时主动调工具升级。

---

## 决策清单（已和用户对齐）

| 项 | 选择 |
|---|---|
| Classifier 职责 | 只输出路由标签（不做结构化提取） |
| 多标签策略 | 单标签优先，冲突时由下游 `escalate_to_assistant` 工具触发层级升级 |
| 结构化记录路径 | 精简版 chat（小模型 + 精简 prompt + 单工具），而非绕过 AI 直写库 |
| 助理干预路径 | 更大模型（Opus + extended thinking）+ 更共情的 prompt + 更多上下文 |
| 主 chat（普通聊天） | **完全不变**。直接复用现有 `chat()`，system prompt 字节级不动，保 cache |
| 处理器范围（首批） | `diet`（饮食）、`assistant`（焦虑干预）、`general`（现有 chat） |
| Classifier 模型 | Gemini Flash（新加，不复用主引擎） |

---

## 架构变化

```
旧：on_message ──────────────────────────────────────► chat() [Opus, 全工具, 4-level cache]

新：on_message ──► classifier(Flash) ──► diet(Haiku/Flash) ──┐
                                   ├──► assistant(Opus+think) ─┼──► send to Discord
                                   └──► general = 现有 chat() ─┘ （cache 保持）
                                                  ▲
                                             escalate_to_assistant 工具
                                             （任何下游可触发，切到 assistant 处理器）
```

---

## Classifier 设计细节

### 输入
- **必须含历史上下文**。"嗯"、"继续"、"好" 孤立无法分类。
- 给 classifier **最后 5 条**消息（user+assistant 混合），足够消歧且不贵。远少于主 chat 的 20 条。
- 不加时间戳前缀、不加 memory/ongoing/deadline 注入——classifier 只需要消息语义。

### 输出
- 严格 JSON：`{"route": "diet" | "assistant" | "general"}`
- **不允许多标签**。若模棱两可，classifier 必须选一个（默认 `general`）。

### Prompt 极简（≤300 字符）
列出三个标签的判定要点，要求只回 JSON。不带人设、不带工具、不带任何可缓存结构（Flash 每次全量重算就行，本来就便宜）。

### Fallback 规则
- Gemini Flash API 失败 → **默认路由到 `general`**（走原 chat，行为完全等同于没加 classifier）。
- 超时阈值设 3 秒（Flash 正常 <1s）。超时 → general。
- Classifier JSON 解析失败 → general。
- 这条规则要在 `classifier.py` 里硬编码，不能让分类错误把主链路带下水。

---

## 处理器设计

### 统一签名（关键约束）
所有三个处理器必须同签名，才能被 test_mode、tool_callback、send_callback 等机制透明捕获：

```python
async def process(
    db: Database,
    ai_messages: list[dict],
    send_callback,
    tool_callback,
) -> str
```

### `general` 处理器
- **直接引用现有 `bot.ai_engine.chat`**，不包装、不新增抽象层。
- System prompt 必须字节级与旧版一致，否则 4-level cache 全失效。
- **验收标准**：对一条普通聊天消息，新架构产生的 Anthropic API 请求 body 与旧架构完全一致（尤其 system blocks 的 4 个 cache_control 节点）。

### `diet` 处理器
- 新文件 `bot/processor_diet.py`。
- 模型：Haiku 4.5（POLL_MODEL 同级或更低，新增 config 项 `DIET_MODEL`，默认 `claude-haiku-4-5-20251001`）。
- Prompt：最小化 IDENTITY（1-2 句）+ 一条工具使用约束（"遇到食物/饮水记录调 log_timeline_event，category=Routine"）+ `escalate_to_assistant` 触发条件（"如果用户在表达情绪、不是在记录，调此工具"）。不带 PROTOCOLS、不带 USER_MODEL、不带 memory 注入。预计 system ~500 字符。
- 工具集：`log_timeline_event` + `escalate_to_assistant`（新工具，见下）。**不给**其他 9 个工具。
- 复用 `ai_engine_base` 的多轮 tool calling 循环？**不**——直接写一个薄的 1-2 轮循环。复用会把 `_build_prompt` 那套重的东西拖进来，违反"精简"初衷。但工具执行仍走 `_execute_tool`，避免重复实现。

### `assistant` 处理器
- 新文件 `bot/processor_assistant.py`。
- 模型：Opus 4.6（复用 `CHAT_MODEL`，无需新配置）。
- **启用 extended thinking**（budget_tokens ~2000）。这是与 `general` 路径的核心差异。
- Prompt：完整 IDENTITY + USER_MODEL + 强化版 PROTOCOLS（特别是"迈不出第一步"、"高耗宕机"、"时间感偏移"段落加重）+ COMMUNICATION。
- 上下文注入：memory 全量 + **最近 24h 的 timeline events**（新增的上下文，general 路径没有）。
- 工具集：完整 9 个工具。需要能 set_reminder、save_memory 等。
- 复用现有 `_call_with_tools`？可以，但传入一个不同的 PromptParts。改动集中在 prompt 构建，不触 Claude 引擎本身。

### Escalation 机制（层级路由实现）
- 新增工具 `escalate_to_assistant`。**定义在 `bot/processor_diet.py` 内部**，不写进 `bot/tools.py`。
  - 理由：如果写进共享 `tools.py`，`general` 路径会通过现有的工具加载机制看到它（Claude 引擎构造 `tools=` 时从 tools.py 全量读），就可能被调用——与设计相悖。
  - diet processor 自行构造 tool schema 传给 Claude API。`_execute_tool` 也不需要知道这个工具（diet processor 在多轮循环里自己拦截 tool_use 分支）。
- **只给 `diet` 和未来其他结构化处理器使用**。`general` 和 `assistant` 拿不到此工具。
- schema 极简：无参数，或一个可选 `reason: string`。
- 执行逻辑（在 `processor_diet.py` 的 tool 执行分支里）：
  1. 中止当前处理器的后续轮次。
  2. **当前处理器不发任何文字给用户**（silent handoff — 避免双回复尴尬）。
  3. **在 ai_messages 末尾追加一条合成的 user-side 注解**：
     ```
     [内部分流: diet 处理器已记录「沙拉」(或其他 log_timeline_event 的 content 值) 到 timeline，用户消息含情绪信号，现切换至助理模式处理情绪部分]
     ```
     如果 diet 这一轮同时调了 `log_timeline_event`，注解里要列出所有已记录的 content；没调就只说"切换至助理模式"。
  4. 调用 `processor_assistant.process(db, ai_messages_augmented, send_callback, tool_callback)`。
- **为什么不直接传原 ai_messages**：assistant 看不到 diet 的工具调用，可能重复记录，或多花一次 `query_timeline` 检查。合成注解让 assistant 读为用户侧上下文，自然避免重复且语气不受影响。
- **为什么不传 diet 的 tool_use/tool_result turns**：那些是 Claude 以 diet 的 system prompt + 工具集生成的内容，assistant 用不同 system prompt + 不同工具集去 reason 是奇怪的状态，容易出幻觉。
- **约定**：diet processor 的 prompt 里明确写"如果要 escalate，**不要**先回复用户，直接调工具；可以同一轮里先调 log_timeline_event 再调 escalate_to_assistant"。这条要作为 prompt 里的硬规则。

---

## 双回复防御（advisor 指出的坑）

场景："我吃了沙拉，但是我今天真的好累好想哭" → classifier 选 `diet`（匹配到"吃了沙拉"）→ diet 处理器发现情绪信号 → escalate。

**防御**：silent handoff 就是唯一答案。diet 处理器**禁止**在 escalate 前发文字。违反的话 diet 发一句"好，沙拉记上了"，assistant 再发"等等，你听起来不太对"——双重人格。

技术保证：diet 处理器的多轮循环里，检测到 `escalate_to_assistant` 调用后，**丢弃当前轮次的所有 text content**，只执行工具（log_timeline_event 若在同一轮也照常执行，饮食记录该落库还是落库），然后立即返回并调 assistant。

---

## Token 成本估算

| 路径 | Classifier | 处理器 | 合计 |
|---|---|---|---|
| **旧 general（未加 classifier）** | — | ~2000 in + 200 out (Opus, cache hit) | Opus ~$0.01 |
| **新 general** | ~500 in + 20 out (Flash) | 同上 | Flash ~$0.0001 + Opus ~$0.01 ≈ 基本持平 |
| **新 diet** | ~500 in + 20 out (Flash) | ~800 in + 100 out (Haiku) | Flash ~$0.0001 + Haiku ~$0.001 ≈ **降 10 倍** |
| **新 assistant** | ~500 in + 20 out (Flash) | ~3000 in + thinking 2000 + 300 out (Opus) | Flash ~$0.0001 + Opus ~$0.03 ≈ **涨 3 倍** |
| **diet → escalate → assistant** | Flash + Haiku + Opus thinking | | ≈ 3.1 倍普通 general |

**结论**：整体 token 走势取决于消息分布。典型场景（大量饮食/运动记录）会净省；焦虑时段 assistant 升级会更贵但这是刻意的。**没有 token 爆炸**，因为单标签 + silent escalation 保证任何一条消息最多穿过 2 个处理器。

上述数字全是**估算**，未实测。实施后应加日志统计每条消息的 token 消耗，验证后再决定是否要给 assistant 加 `thinking_budget` 自适应。

---

## 关键文件改动

### 新增
| 文件 | 内容 |
|---|---|
| [bot/classifier.py](bot/classifier.py) | `async def classify(messages: list[dict]) -> str` — 调 Gemini Flash，返回 `"diet"` / `"assistant"` / `"general"`。内嵌 fallback。 |
| [bot/processor_diet.py](bot/processor_diet.py) | `async def process(db, ai_messages, send_callback, tool_callback) -> str` — 精简版处理，Haiku + 2 个工具。 |
| [bot/processor_assistant.py](bot/processor_assistant.py) | 同签名——Opus + thinking + 强化 prompt + 完整工具。 |
| [bot/processor_general.py](bot/processor_general.py) | 薄薄一层，直接 `return await chat(db, ai_messages, send_callback, tool_callback)`。只是为了签名统一。 |
| [bot/router.py](bot/router.py) | `async def route_and_process(db, ai_messages, send_callback, tool_callback)` — 调 classifier → 派发到 processor。 |

### 修改
| 文件 | 修改内容 |
|---|---|
| [bot/discord_bot.py:128](bot/discord_bot.py#L128) | `await chat(...)` → `await route_and_process(...)`。这是入口的**唯一一行**改动。 |
| [bot/ai_engine_claude.py](bot/ai_engine_claude.py) | 验证 extended thinking 参数（`thinking={"type":"enabled","budget_tokens":2000}`）能否透传——若 `_call_with_tools` 没有该入参，新增一行 `thinking` kwarg 直通 `messages.create`。实施时先 diff，必要时补。 |
| [config.py](config.py) | 新增 `CLASSIFIER_PROVIDER`（默认 `"gemini"`）、`CLASSIFIER_MODEL`（默认 `"gemini-2.5-flash"`，实施前核实当前稳定 Flash id）、`CLASSIFIER_API_KEY`、`DIET_MODEL`（默认 `"claude-haiku-4-5-20251001"`）。 |
| [config.example.json](config.example.json) | 同步新增字段。 |

### 不动
- `bot/ai_engine_base.py` - `chat()` 函数、`_build_prompt`、`_call_with_tools` 循环全部不变。
- `bot/prompts.py` - PromptParts 不动，section 不动。`processor_diet` 和 `processor_assistant` 各自内联精简/强化版 prompt 字符串，**不写进 prompts.py**（避免污染主 chat 的 cache 结构）。
- `bot/scheduler.py` - proactive / reminder / bedtime 依然走 `scheduled_action`，**不经过 classifier**。

---

## 复用的现有函数（不要重写）

| 函数 | 路径 | 用途 |
|---|---|---|
| `_execute_tool` | `bot/ai_engine_base.py` | 所有处理器执行工具都走它，别重复实现 |
| `build_tool_round_hint` | `bot/prompts.py` | 多轮 tool_result 后的提示，diet/assistant 都要用 |
| `chat` | `bot/ai_engine.py` | `processor_general` 直接调 |
| `_fetch_history_as_messages` | `bot/discord_bot.py:144` | classifier 的上下文也从这里拿（取最后 5 条，传给 classifier） |

---

## 开放问题（实施时要继续定）

1. **运动/体育记录**处理器：用户在问题里圈了但这版不实现。等 diet 和 assistant 稳定后作为第二批。
2. **Classifier 训练/校准**：初期硬编码 prompt，观察一周错分率。如果 >10% 考虑加 few-shot examples。
3. **测试模式集成**：`bot/test_mode.py` 目前只抓主链路 prompt payload。新增 classifier 和 diet/assistant 的 payload 要不要也抓？倾向抓（都是 AI 调用），但要确认 `test_mode` 的 hook 点。

---

## 验证方案

**端到端测试矩阵**（用 Discord 实际发消息验证）：

| 输入 | 期望 route | 期望行为 |
|---|---|---|
| "今天吃了沙拉" | `diet` | Haiku 调 log_timeline_event，给简短回复 |
| "又饿又累，好烦" | `assistant` | Opus 触发 thinking，共情回复 |
| "最近在看《奥本海默》" | `general` | 走原 chat，Opus cache 命中 |
| "我吃了沙拉，但其实我想哭" | `diet` → escalate → `assistant` | **单一回复**（assistant 发的），timeline 记录沙拉 |
| "嗯" | `general`（兜底） | 走原 chat |
| （拔 Gemini API key 模拟故障）"今天吃了饭" | `general`（fallback） | 走原 chat，用户无感知 |

**定量验证**（实施后 24h 观察）：
1. Anthropic 控制台确认 general 路径 cache hit rate 与实施前持平（应 >90%）。
2. 日志统计每条消息的 `route` 分布，预期 diet 占 20-40%，assistant 占 <10%，general 占 >50%。
3. 日志统计 classifier fallback 触发次数，应 <1%。

**回归测试**（不能坏的）：
1. `/todo`、`/weather` 斜杠命令：完全不经过新链路。
2. Scheduler 的 proactive/reminder/bedtime：完全不经过 classifier。
3. 主 chat 的 4-level cache 分块：用 test_mode 抓一条普通消息的 payload，diff 新旧架构的 system blocks，必须完全一致。
