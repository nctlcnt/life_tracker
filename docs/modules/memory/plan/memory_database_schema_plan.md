# 记忆数据库结构与状态更新方案

> 本文是**表结构与操作契约层**的具体方案：字段、JSON 形状、校验清单。
> 上层的架构理由、状态语义和信任边界见 [memory-v4-design.md](memory-v4-design.md)；
> 术语以 [GLOSSARY.md](../GLOSSARY.md) 为准。本文与那两份冲突时，改本文。
>
> 最后核对：2026-08-22（按单表状态阶梯方向修订）

## 1. 目标和边界

本方案只处理长期记忆的创建、补充证据、替换、状态计算和状态晋升。聊天消息仍然先写入现有的 Message DB。后台 curator 异步读取新消息和相关旧记忆，然后返回结构化的操作建议。后台代码验证这些建议，并在一个 Transaction 中修改记忆数据库。

AI 只能提出操作建议。AI 不能分配 `memory_id`，不能直接决定最终 `status`，也不能直接把一条记忆改成 `confirmed`。

本方案不增加 `importance` 和 `valid_until`。系统在读取记忆时，先按 `status` 查到注入权限决定这条能不能用、怎么用，再按相关性、更新时间和询问记录决定使用顺序。

## 2. 数据库结构

长期记忆使用**两张表**：`personal_memories` 保存记忆条目本身，`personal_memory_sources` 保存它与原始消息的证据关联。

需要澄清一处早期表述：本文旧版写过"`sources` 字段保存消息引用，因此不增加独立的证据表"。这句话既不符合现状也不符合目标设计——`personal_memory_sources` 已经是一张独立的表，原始消息与关联关系必须可查询、可审计。下面 JSON 里的 `sources` 只是**curator 提案的传输形状**，不是存储形状；apply 阶段把它拆成 `personal_memory_sources` 的行。

### 2.1 记忆条目

```json
{
  "memory_id": 10,
  "claim": "用户认为自己在深夜更容易集中精力",
  "memory_type": "pattern",
  "status": "provisional",
  "basis": "supported",
  "stability": "unknown",
  "scope": "general_pattern",
  "gap": "用户描述过几次深夜效率高，但没有直接表达这是长期规律。",
  "alternatives": [],
  "reason": "解释为什么值得长期记住，给用户看的管理字段",
  "superseded_by": null,
  "last_asked_at": null,
  "ask_count": 0,
  "created_at": "2026-07-17T00:00:00Z",
  "updated_at": "2026-07-17T00:00:00Z"
}
```

**这不是当前表结构，是目标结构。** 当前 `personal_memories` 的实际列是
`id`、`summary`、`quote`、`reason`、`memory_type`、`status`、`superseded_by`、
`curator_model`、`embedding`、`embedding_model` 和时间戳。差距有四处：

- 列名是 `summary`，规范名是 `claim`；
- `status` 的 `CHECK` 仍是 `active` / `superseded` / `archived` 三值；
- `basis`、`scope`、`stability`、`gap`、`alternatives` 五个字段都不存在；
- `curator_model` 存的是模型名，而 curator 身份的键应当是 `curator_name`（profile 键）。

补齐这些字段是实施计划的 Phase 1，也是开启受控 auto-apply 的前置。

### 2.2 字段定义

`claim` 必须只包含一个可以独立判断真假的陈述。如果一句总结包含两个可能具有不同证据、状态或稳定性的陈述，curator 必须把它们拆成两条记忆。

`memory_type` 的最终枚举**未定**（设计文档未决问题 1）。代码当前实际使用的是
`identity` / `preference` / `interaction_style` / `current_state` / `plan` /
`open_loop` / `temporary_context` / `general`，权威定义在 `bot/memory/curator.py` 的
`CURATOR_MEMORY_TYPE_GUIDE`。过渡期维持这八个取值不动，等枚举拍板后一次迁移。

`status` 使用以下五个取值，含义与对应的注入权限见设计文档第 5 节：

- `hypothesis`：证据只够合理猜想，权限 `probe_only`；
- `provisional`：有明显支持但没有用户确认事件，权限 `hedge`；
- `confirmed`：存在用户确认事件，权限 `assert`；
- `disputed`：存在未解决的硬冲突且尚无替代 claim，权限 `probe_only` 或 `hidden`；
- `superseded`：已被新条目替代，权限 `hidden`。

`basis` 描述当前全部证据与 claim 的关系：

- `inferred`：claim 主要来自推断，仍然存在明显解释空间；
- `supported`：证据没有直接复述 claim，但已经提供较强支持；
- `asserted`：用户直接表达或明确确认了与 claim 基本等价的内容。

它是关系类型，不是置信度分数。它参与计算 `status`，但不决定这条记忆存在哪里——单表方向下所有记忆都在同一张表。

`stability` 使用 `temporary`、`unknown` 和 `stable`，描述时间稳定性，不描述证据强度。

`scope` 的最终枚举**未定**（设计文档未决问题 2）。候选取值是 `specific_event`、`specific_item`、`entity`、`category` 和 `general_pattern`。这个字段参与查重，枚举定下后改动成本高，所以先按候选取值落地并在代码里标为暂定。

`gap` 使用自然语言说明当前证据为什么还不能完全支持 claim。系统不使用细分的 gap 类型。`basis` 为 `asserted` 时，`gap` 原则上应当为 `null`。注意它与 `reason` 不是一回事：`gap` 说的是"证据还差什么"，`reason` 说的是"为什么值得记住"，两者并存。

`alternatives` 保存其他仍然合理的 claim 文本。它们不是正式记忆，不占 `memory_id`，不进入检索。唯一用途是生成确认问题的选项；用户选中某条时通过 `supersede` 转成正式记忆。

**本文旧版的 `score` 字段已移除。** 状态表拍板后它没有消费者：`status` 由事件和证据类型决定，排队由 priority 负责。它作为未决问题 4 的一个候选方案保留在第 5.2 节，除非那里最终选用计分，否则转入弃用。

### 2.3 证据关联

`personal_memory_sources` 保存记忆与 `conversation_messages` 的关联，每行有
`memory_id`、`conversation_message_id`、`quote`、`evidence_role` 和时间戳。

目标结构还需要增加两样（实施计划 Phase 6）：

- **证据组标识**：一份证据是一组消息，不是一条。组是"独立证据计数"的单位——同一轮对话里重复三遍只算一组。组边界就是 curator 每个操作的 `sources[]`，组 id 在 apply 时由代码分配，模型不能提交也不能引用已存在的组。
- **`is_assertion` 布尔列**：标记这条消息是不是用户的直接表达。每组至少一条 assertion；assistant 消息永远只能是 context。

curator 的 JSON 用位置编码表达同一件事（`message_id` 加 `context_message_ids[]`），apply 阶段翻译成 `is_assertion`。两种编码的关系见设计文档第 6.2 节。

## 3. Curator 返回的操作

curator 只允许返回三种操作：`create`、`attach_evidence` 和 `supersede`。没有需要处理的信息时，返回空的 `operations` 数组。

**注意代码尚未迁移到这套操作集合。** 当前实现的四个操作是 `create` / `update` / `supersede` / `archive`：`attach_evidence` 还不存在，而本节要取消的 `update` 和 `archive` 都还在用。迁移安排在实施计划的 Phase 6，因为 `attach_evidence` 的意义正是"只加证据、不改写 claim"，这在没有证据组标识之前表达不出来。

### 3.1 `create`

用于创建一个不存在的新 claim。

```json
{
  "action": "create",
  "operation_id": "run-42-op-1",
  "memory": {
    "claim": "用户可能喜欢 Mendelssohn 的音乐",
    "memory_type": "preference",
    "basis": "inferred",
    "stability": "unknown",
    "scope": "entity",
    "gap": "用户明确表达的是对一场音乐会的期待，尚未说明这种期待是否来自对作曲家的普遍偏好。",
    "alternatives": [
      "用户只期待这场具体音乐会",
      "用户喜欢的是 Mendelssohn 小提琴协奏曲",
      "用户主要期待该场演奏者"
    ]
  },
  "sources": [
    {
      "message_id": 1103,
      "context_message_ids": [1102],
      "quote": "嗯嗯，我很期待mendelssohn的",
      "evidence_role": "supports"
    }
  ]
}
```

这个例子正好说明单表方向的意义：`basis` 是 `inferred`，所以代码算出来的 `status` 会是 `hypothesis`，权限 `probe_only`——它照常写进数据库，但聊天只能拿它去生成确认问题，不能当作用户喜欢 Mendelssohn 的事实使用。

### 3.2 `attach_evidence`

用于给现有 claim 增加支持或反对证据。curator 可以同时提出新的 `basis`、`gap` 和 `alternatives`，但 `status` 仍由后台代码计算。

```json
{
  "action": "attach_evidence",
  "operation_id": "run-53-op-2",
  "target_memory_id": 10,
  "sources": [
    {
      "message_id": 1250,
      "context_message_ids": [1249],
      "quote": "对，我一直都挺喜欢Mendelssohn的",
      "evidence_role": "supports"
    }
  ],
  "proposed_basis": "asserted",
  "proposed_gap": null,
  "proposed_alternatives": []
}
```

### 3.3 `supersede`

用于处理 claim 本身已经不准确的情况。后台代码必须先创建替代记忆，然后再把旧记忆改为 `superseded`，两个写入动作必须位于同一个 Transaction 中。

```json
{
  "action": "supersede",
  "operation_id": "run-61-op-1",
  "target_memory_id": 10,
  "replacement": {
    "claim": "用户主要期待该场小提琴演奏者，对 Mendelssohn 本人没有明显偏好",
    "memory_type": "preference",
    "basis": "asserted",
    "stability": "unknown",
    "scope": "specific_event",
    "gap": null,
    "alternatives": []
  },
  "sources": [
    {
      "message_id": 1310,
      "context_message_ids": [1309],
      "quote": "我主要是期待那个小提琴家，Mendelssohn其实一般",
      "evidence_role": "supports"
    }
  ]
}
```

纠错也走这条路径。"我从来没喜欢过咖啡"本身就断言了"用户不喜欢咖啡"这个新事实，因此它是 supersede，不需要一个单独的 archive 操作。只有当用户否认了旧记忆却拿不出新事实时，那条记忆才停在 `disputed`。

## 4. 后台验证逻辑

后台代码必须先完成以下验证，然后才能修改数据库。

1. 验证 JSON 是否符合固定的 JSON Schema，拒绝未知字段和非法枚举值。
2. 验证 `target_memory_id` 是否存在，并确认 curator 在本次请求中已经读取过该记忆。
3. 验证所有 `message_id` 和 `context_message_ids` 是否存在，且位于本批冻结区间内。
4. 验证 `quote` 是否可以在对应消息正文中找到。可以忽略空格和大小写差异，但不能接受由 AI 改写后的句子。
5. 验证每条 claim 是否只包含一个核心陈述。无法通过简单规则判断时，拒绝该操作并记录原因，而不是自动拆分。
6. 验证字段之间的一致性。`basis` 为 `inferred` 时 `gap` 不能为空；`basis` 为 `asserted` 时 `gap` 原则上应当为空；**`status` 不允许由 AI 提交**。
7. 防止重复执行同一操作。`operation_id` 必须有唯一约束，且**该约束要带 curator profile 的命名空间**——否则两个 profile 各自跑同一批消息会互相撞键。
8. 防止创建完全相同的活跃 claim。至少需要比较标准化后的 `claim`、`memory_type` 和 `scope`。
9. 处理 `supersede` 时必须先验证替代记忆。只有替代记忆成功写入后，才能修改旧记忆的状态。

第 6 条在操作集合迁移完成之前还需要一条临时限制：**`update` 不得在不追加证据的情况下改写 claim**。因为 `attach_evidence` 尚未落地，这段时间里补强一条记忆只能用 `update`，必须防止它变成一个可以随意改写语义的通用入口。

## 5. 状态计算

`status` 在每次 apply 时由代码根据该记忆的**全部**证据重新计算，不在旧值上做增量修改。

### 5.1 已定的规则

- `basis = asserted`，且该 assertion 构成一次用户确认事件 ⇒ `confirmed`；
- 存在未解决的硬冲突，且没有替代 claim ⇒ `disputed`；
- `supersede` 操作成功完成 ⇒ 旧条目 `superseded`；
- 其余情形落在 `hypothesis` 和 `provisional` 之间，分界规则见 5.2。

"用户确认事件"的两条识别路径见设计文档第 5.1 节。其中"回答确认问题"那条还缺一条绑定规则（显式回复是强信号，没有回复时限定在受限的连续回答窗口内），在事后确认循环落地之前不成立。

**证据数量的累积不能替代确认。** 无论积累多少组 `supported` 证据，记忆都停在 `provisional`。

### 5.2 `hypothesis` 与 `provisional` 的分界（未决问题 4）

**这一条尚未拍板，下面记录的是候选方案，不是结论。** 在拍板之前，`status` 计算函数里这一段必须显式落到 `hypothesis` 并留注释说明这是待决默认值，不能写成一个看起来像结论的阈值。

候选方案甲（规则型）：`basis = supported` 直接进入 `provisional`；`basis = inferred` 需要累积若干组独立证据才进入。

候选方案乙（计分型，即本文旧版的 score 机制）：

- `basis = inferred` 基础分 1，`supported` 基础分 2，`asserted` 基础分 4；
- 每增加一组独立的支持证据加 1 分，最多加 2 分；
- 每出现一组明确的反对证据减 3 分；
- 分数 ≤ 1 为 `hypothesis`，2 或 3 为 `provisional`。

选用乙就需要恢复 `score` 字段，并且必须按全部证据重算而不是在旧分上增减；不选乙则 `score` 转入弃用。

两个方案都依赖"独立证据组数"这个量，而**跨操作的独立性判定规则同样未定**（未决问题 3）。第一版的分组规则是"每个 curator 操作的 `sources[]` 算一组"，但两个不同批次的操作可能引用同一场对话的同一件事。这两个未决问题耦合，宜一起决定。

## 6. Confirmed 前的验证（未决问题 5）

**是否保留这一步尚未决定。** 下面记录的是上一版设计的方案。

上一版的做法是：记忆首次达到 `confirmed` 条件时调用一个验证模型，检查 claim 是否受到证据支持、`scope` 是否超过证据范围、`alternatives` 是否已被用户排除。验证模型不能直接修改数据库，只能返回三种结论之一：

- `approve`：后台代码把 status 改为 `confirmed`；
- `keep_provisional`：保留 `provisional`，并更新 `gap` 或 `alternatives`；
- `require_supersede`：当前 claim 的范围或内容不准确，curator 必须在后续任务中提出 `supersede`。

单表方向下需要重新判断的是：这一步还有没有必要，以及哪些路径可以豁免。特别是用户直接回答确认问题这条路径——用户刚刚明确说了"对"，再让另一个模型验证一次是否属于多余的环节，需要一个明确结论。

## 7. 读取与确认流程

聊天按每条记忆的 `status` 查到注入权限，再按权限决定怎么用：

- `assert`：作为已确认信息直接注入；
- `hedge`：必须带限定语，且与 `assert` 分块，不能混在同一段文本里；
- `probe_only`：不进事实段落，只进待确认队列；
- `hidden`：不进 prompt。

权限只回答"能不能用、怎么用"。**先问哪一条由 priority 决定**——相关性、更新时间、`last_asked_at` 和 `ask_count` 都进 priority，不进权限。系统每次回复最多确认一条记忆。

定时 check-in 没有新的用户消息时，应当使用最近一段对话或近期主题摘要计算相关性。如果没有合理的上下文，不应当为了确认记忆而强行提问。沉默不算否认，也不自动重问。

提出确认问题时，assistant 消息的 metadata 应当记录对应的 `memory_id` 和 alternatives。这只解决"用户在回答哪一条"的绑定问题；用户答的是肯定还是否定，仍然由 curator 在语义层判断，并体现为它提出的 `evidence_role` 或直接提出 `supersede`。

## 8. 需要补充的前置工作

1. 定义 `memory_type` 和 `scope` 的最终枚举（未决问题 1、2）。
2. 定义"独立证据组"的跨操作判定规则（未决问题 3）。
3. 决定 `hypothesis` 到 `provisional` 的分界，并连带决定 `score` 的去留（未决问题 4）。
4. 决定 verification model 的去留与豁免路径（未决问题 5）。
5. 为三种操作编写 JSON Schema。
6. 修改聊天消息 metadata，使系统可以保存被确认的 `memory_id` 和 alternatives。
7. 定义 ask 与用户回答的绑定规则（显式回复加受限连续回答窗口）。

## 9. 代码风险

1. 如果 curator 可以提交任意 `update`，后台代码将很难判断它修改了哪些语义，因此不应提供通用的 `update` 操作。在迁移完成之前，用第 4 节末尾那条临时限制顶住。
2. 如果系统只按消息数量计算证据，多次重复同一句话会错误地抬高证据强度。这是证据组存在的理由。
3. 如果允许 AI 提交 `status`，不同模型和不同提示词会产生不稳定结果。
4. 如果 `supersede` 不在同一个 Transaction 中创建替代记忆并更新旧记忆，系统可能出现旧记忆已经失效但新记忆尚未建立的中间状态。
5. 如果确认问题没有保存对应的 `memory_id`，curator 可能把用户回复关联到错误的记忆。
6. 如果 `operation_id` 的唯一约束不带 profile 命名空间，多 profile 并行评测时会互相撞键。
7. 如果状态阶梯字段没有先于受控 auto-apply 落地，会先写进一批无法分类的记忆，之后必须人工重新分类。
