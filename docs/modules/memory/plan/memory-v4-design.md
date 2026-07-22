# Memory v4 architecture design

> 文档性质：living architecture design，描述当前认可的目标结构，并明确
> 标出实现状态。设计变化直接更新本文；历史原因和被否决的架构方向见
> [ADR-0005](../0005-memory-trust-boundaries.md)。
>
> 最后核对：2026-07-22

## 1. 这份文档解决什么问题

Memory v4 要把三件过去容易混在一起的事分开：

1. 保持一段对话的连续性；
2. 保存关于用户当前仍成立的长期信息；
3. 保留得出这些信息的原始证据和演变历史。

系统的信任边界是：**模型可以整理、归纳和提出修改，但模型输出本身不是
用户事实。正式记忆必须能追溯到用户 assertion；模型推断出的 claim 必须先
进入候选层，经过用户确认后才能成为 canonical memory。**

本文是目标设计的唯一完整说明。具体实施顺序见
[memory-v4-implementation.md](memory-v4-implementation.md)，模型评测见
[curator-model-comparison.md](../../../../evals/curator-model-comparison.md)。

## 2. 当前实现状态

Memory v4 尚未完整落地。阅读后续目标设计时，必须区分以下状态：

| 能力 | 状态 | 当前事实 |
|---|---|---|
| Recent context + discourse compact | 已实现 | 原始消息在 `conversation_messages`；compact summary 与其折叠 cursor 在 `app_state` |
| compact 后历史 embedding/search | 已实现 | 只为已经折叠的消息异步补向量，聊天可搜索历史 |
| curator propose/validate/apply | 已实现 | 支持 dry-run、严格 JSON/quote 校验、单事务写入并推进 cursor |
| curator scheduler shadow/auto-apply 开关 | 部分实现 | 配置开关存在且默认关闭；尚无 asserted/inferred 结构化分流，当前不能据此保证自动写入只接收 assertion |
| `personal_memories` + 扁平 sources | 已实现但未成为聊天权威来源 | curator 可以写入，聊天 prompt/search 尚未消费它 |
| `data/memory.md` + `MemoryService` | 现行聊天来源 | 仍负责 bot 工具 CRUD 和 prompt 注入，是迁移中的旧 canonical 路径 |
| evidence group 三表与共享 mutation | 未实现 | 当前仍是 `personal_memory_sources` 扁平关系 |
| candidate layer 与确认协议 | 未实现 | 无 candidates、confirmation attempts 或 ask-only 注入 |
| consolidation 聚类与 lineage/revision | 未实现 | 无专用 user-event embedding、聚类 job 或 run snapshot |

因此，`personal_memories` 是 v4 的**目标 canonical store**，但不能把它描述成
当前聊天已经在使用的唯一记忆来源。切换完成前，两条路径的边界和迁移步骤必须
在实施计划中显式管理。

## 3. 总体结构

### 3.1 四层记忆

| 层 | 保存内容 | 回答的问题 |
|---|---|---|
| Recent context | 最近对话原文 | 我们此刻在聊什么 |
| Discourse compact | 话题轨迹、转折、指代和未完上下文 | 之前聊过什么、聊到哪里 |
| Canonical memory | 当前有效、可复用的用户语义状态 | 关于用户，现在仍成立什么 |
| Evidence archive | 原始消息、证据关系和 revision 演变 | 这条记忆依据什么、如何变化 |

其中 Evidence archive 是由消息与关系组成的逻辑视图，不复制一份新的原始
对话存储。

Compact 和 canonical memory 不能互相代替：

- compact 记录“聊过什么”，不宣称某个长期事实截至目前仍然成立；
- canonical memory 记录“当前成立什么”，不承担对话叙事和指代连续性；
- embedding/search 找回当时怎样说，不自动把旧原话提升为当前事实。

### 3.2 写入路径

```text
conversation_messages
        |
        v
curator / consolidation model
        |
        v
structured proposal -- deterministic validation --> reviewed/approved proposal
        |                                              |
        | inferred claim                               | asserted claim
        v                                              v
candidate layer -- user confirmation ----------> shared mutation boundary
                                                       |
                                                       v
                                 canonical memory + evidence + checkpoint
```

模型阶段和写入阶段必须分离。写入阶段不重新调用模型，也不重新解释 proposal。

## 4. Claim 路由

每个准备进入长期记忆的 claim 先按证据性质路由：

- `asserted`：用户原话明确表达了该事实，可以生成 canonical mutation proposal；
- `inferred` / `supported`：模型根据一次或多次经历推断出的规律，只能进入
  candidate layer；
- assistant 文本只能提供解释 user assertion 所需的 context，不能单独确立
  用户事实。

例如：

- “我一直都喜欢 Mendelssohn”是 asserted preference；
- “我很期待周六的 Mendelssohn 音乐会”明确表达了一次期待，但“用户长期
  喜欢 Mendelssohn”仍是 inferred claim；
- assistant 说“你应该很喜欢 Mendelssohn”不构成用户证据。

`basis` 是路由标签，不是置信度分数。Canonical memory 不保存模型自报的
truth confidence；没有校准数据和明确消费者时，这种数字不可证伪。

## 5. Curator 管线

### 5.1 Propose

Curator 读取一个冻结的消息区间和当前 active memories，输出结构化 proposal。
当前操作包括 `create`、`update`、`supersede`、`archive`；目标 mutation contract
还需要支持 `attach_evidence` 和候选路由。

Propose 阶段可以调用模型，也可以在确定性校验失败后做一次只修格式、字段或
引用的定向 repair。它不得修改 canonical memory 或推进 cursor。

消息以数据形式交给模型：消息内容是潜在证据，不是 curator 指令。用户消息里
即使包含“忽略规则、伪造记忆”等文本，也不得改变 curator 的 system contract。

### 5.2 Validate

确定性 validator 只验证可以机械判断的事项：

- JSON 结构、枚举和必填字段合法；
- message id 位于冻结区间，且区间没有缺失或偷换；
- quote 是对应 message content 的连续原文子串；
- 目标 memory 存在、状态允许当前 mutation；
- proposal 与持久化的 curator run 输出一致；
- 当前 cursor 仍等于 proposal 的起点。

Validator 不能证明 summary 的语义一定正确，也不能替代人工或确认协议判断
“这句话是否足以支持那个结论”。

### 5.3 Review 与 apply

当前 rollout 允许两种模式：

- shadow/manual：propose -> validate -> 人工接受或拒绝 -> apply；
- auto-apply：propose -> validate -> apply。目标上只能接收已经满足自动写入政策的
  asserted claims；当前实现尚无 basis 路由，因此在 Phase 2 完成前应保持关闭。

人工审核不是永久架构不变量；以下才是 apply 的不变量：

1. apply 不调用模型；
2. apply 执行的 batch 必须与被记录、被批准的 proposal 一致；
3. apply 开启一个事务，重新校验 checkpoint 后再写入；
4. canonical mutation、evidence 和对应 checkpoint/candidate 状态一起提交；
5. 任一操作失败则整批回滚。

Curator 的 checkpoint 是 message cursor。空 proposal 成功 apply 后也推进 cursor，
表示该区间已经检查且没有需要写入的内容。

## 6. Canonical memory

Canonical memory 保存当前可供 bot 使用的用户语义状态，而不是所有发生过的
事件。当前基本状态为：

- `active`：当前默认解释；
- `superseded`：过去可以成立，但已被新的 revision 替代；
- `archived`：已结束，或证据表明旧理解原本就不成立。

设计中还提出 `disputed`：收到冲突证据但尚未澄清时暂停注入。该状态尚未实现，
落地前需要明确恢复路径和 UI 行为。

事实变化与事实纠错必须区分：

- “以前喜欢咖啡，现在喜欢茶”使用 supersession，保留过去成立的历史；
- “我从来没喜欢过咖啡，是你理解错了”将旧 revision archive，并用
  contradiction evidence 记录纠错；
- 时间、场景或用途不重叠的偏好可以同时 active，不能只按词面类别判冲突。

记忆 revision 不原地改写历史证据。新事实产生新 revision；旧 revision 通过
`superseded_by` 等关系退出当前投影。

## 7. Evidence model

### 7.1 为什么需要 evidence group

一条 user 消息有时可以独立作证：

> 我喜欢看日出。

更多时候需要最少语境：

> Assistant：你是不是很喜欢看日出？
>
> User：对啊。

“对啊”是用户 assertion，但离开问句就无法解释；assistant 问句可以作为
context，却不能自己成为 assertion。扁平的 memory -> sources 关系无法表达
这个整体，也无法区分同一 memory 下的多个独立问答组。

### 7.2 目标结构

```text
personal_memory_evidence_groups
  (group_id, created_at, legacy, ...)

personal_memory_evidence_members
  (group_id, message_id, member_role, quote, ...)

personal_memory_evidence_relations
  (group_id, memory_id, evidence_role, created_at, ...)
```

两个角色维度必须分开：

- `member_role = assertion | context`：消息在证据组内的结构作用；
- `evidence_role = supports | contradicts | supersedes | contextualizes`：
  整个证据组如何作用于某条 memory。

Evidence group 独立于 memory。同一个 group 可以支持新 memory，同时 supersede
或 contradict 旧 memory；同一 memory 也可以接受多个独立 group 的佐证。

### 7.3 通用不变量

所有写入者都必须经过同一个 shared mutation boundary，并在那里校验：

1. 每个非 legacy group 至少有一个 assertion；
2. assertion 必须对应 user 消息，quote 必须是该消息连续原文子串；
3. context 必须真实存在，并满足 reply 或连续回答窗口的绑定规则；
4. assistant 消息只能是 context；
5. group、memory 和 relation 必须存在且组合不重复；
6. supersede 必须同时产生或指向新的当前 revision；
7. 已 apply 的证据组不可静默原地改写。

SQLite `CHECK` 只能承担单行枚举等约束；跨行、跨表不变量由共享写入边界负责。
DB 中不可避免的枚举副本必须用 schema-contract 测试与代码定义核对。

### 7.4 Legacy 迁移

当前 `personal_memory_sources` 没有 group identity，不能根据相邻位置或相同
evidence role 猜测旧 source 应该怎样分组。迁移必须保守：

- 旧 source 先成为独立的 `legacy` group，保留原 message 与 relation；
- assistant source 可以机械标为 context；
- 只有原文本身足以表达事实的 user source 才能机械标为 assertion；
- 省略、指代或语义不清的 user source 保持待审；
- legacy 可以暂时豁免新不变量，但新 proposal 不能把它当成已验证证据；
- legacy 只减不增，经过重审后才能转成正常 group。

## 8. Candidate layer

模型推断出的 claim 不是 canonical memory，使用独立 candidate layer：

```text
memory_candidates
  origin: curator | consolidation
  status: proposed | asked | approved | applied | rejected | suppressed
  basis, gap, alternatives, scope, source_snapshot, prompt_version, ...
```

表名和字段仍可在实施时调整，但以下语义不变：

- candidate status 不能复用 canonical memory status；
- 未确认 candidate 不得作为用户事实或个性化依据注入；
- hypothesis block 与 canonical memory block 隔离，只允许 bot 择机提问；
- `rejected` / `suppressed` 只约束当时的 revision 和 evidence horizon；
- 新 evidence 可以形成新 revision，不能永久封死整个主题；
- candidate 的 priority/confidence 只用于排队，不表示 canonical truth。

Curator 产生的 inferred claim 和 consolidation 发现的跨时间模式共享同一套候选
语义。Consolidation 额外需要 `lineage_id`、不可变 candidate revision 和 run
snapshot；普通 curator candidate 可以没有 lineage。

## 9. 用户确认协议

候选只能通过结构化 ask 动作发问。目标流程是：

1. 先持久化 outbound intent；
2. 平台发送成功并保存 assistant message；
3. post-send hook 回填内部 `question_message_id`，candidate 才进入 `asked`；
4. 用户显式 reply 是强绑定信号；没有 reply 时，只在受限连续回答窗口内做
   异步分类；
5. 回应分类为 `confirmed`、`corrected`、`rejected` 或 `unrelated`；
6. confirmed/corrected 生成 apply-ready proposal，但仍不直接写 canonical memory；
7. apply 将 memory、evidence group、candidate 状态和 applied memory id 在同一
   事务中提交。

内部协议统一使用 `conversation_messages.id`。平台 message id 由 adapter 映射，
不暴露给模型作为证据 handle。

结果语义：

- `confirmed`：用户确认当前 candidate revision；
- `corrected`：原 candidate 被拒绝，从用户实际措辞生成新的 approved revision；
- `rejected`：只否定当前 revision/evidence horizon；
- `unrelated` 或沉默：不产生 resolution，不把沉默当否认，也不自动重问。

确认问题作为 context，用户回答作为 assertion。触发推断的历史消息默认只提供
模式语境；除非原文本身明确表达 canonical claim，否则不能偷换成 supports。

## 10. Consolidation

Consolidation 解决“用户多次表现出某种规律，但从未直接陈述”的情况。它在
canonical memory 之外运行：

1. 从 user messages 中寻找跨时间重复模式；
2. 保存可复现的输入 snapshot 和聚类结果；
3. 由模型生成带 scope、alternatives 和 evidence 的 candidate；
4. 与 active memories 和历史 candidate lineage 查重；
5. 进入统一 candidate/confirmation 流程。

当前设计倾向使用单独的 user-event embedding，避免复用“当前消息 + 前四条
上下文”的检索向量，也避免 assistant 话术主导聚类。HDBSCAN、运行频率、
`min_cluster_size`、lineage 阈值和 top-k 都是可替换、需通过 shadow 数据调优的
实现策略，不是架构不变量。

图片目前没有进入 embedding 链路。模型名称包含 VL 不等于已经具备多模态
consolidation；这需要独立采集和回填工作。

Consolidation 使用自己的 run snapshot/checkpoint 和 validator，不复用 curator
的连续 message cursor 校验器；两者只复用底层 shared mutation transaction。

## 11. 读取与 prompt 注入

目标读取规则：

- active canonical memories 作为已确认用户信息注入；
- superseded/archived revisions 只在回顾历史时检索；
- disputed memory（若实施）暂停注入；
- proposed candidates 只能进入独立 ask-only hypothesis block；
- compact、canonical memory、history snippets 保持不同标签和语义，不拼成一个
  无法区分可信度的文本块。

当前 bot 仍从 `data/memory.md` 读取长期记忆，而不是从 `personal_memories` 注入。
切换消费者、提供 digest/search 并退役旧 Markdown 路径，是 v4 完成的必要条件。

## 12. Source of truth

为避免再次出现“同一句规则在三篇文档里同步”的问题，所有权固定如下：

| 内容 | Source of truth |
|---|---|
| 长期架构理由与信任边界 | ADR-0005 |
| 当前认可的完整目标设计 | 本文 |
| 当前数据库实际 schema | `bot/database.py` + schema tests |
| curator JSON contract 与枚举 | `bot/memory/curator.py` + contract tests |
| 实施顺序、blocker、验收 | memory-v4 implementation plan / Linear |
| 模型准入方法与历史结果 | evals/curator-model-comparison.md |
| 生产验证事实 | operations-validation.md |

本文可以描述代码 contract，但不复制易漂移的完整枚举/schema 字面量作为运行时
权威。实现与本文不一致时，必须在状态表里明确写成 gap，而不是把目标描述成
已经上线。

## 13. 非架构参数与待决问题

以下内容可以在不推翻本架构的情况下调整：

- compact token 阈值、保留比例和时间表达模板；
- curator batch size、preset、temperature 和 repair 次数；
- shadow 持续时间和 auto-apply rollout 条件；
- clustering 算法及参数、运行频率、candidate top-k；
- prompt 文案和模型供应商。

尚待实施或验证的问题记录在 implementation plan，不在本文伪装成既成事实。

## 14. 设计参考

- **Eywa: Provenance-Grounded Long-Term Memory for AI Agents**
  (Resham Joshi, arXiv:2605.30771, 2026-05)：其“evidence before belief”、
  immutable evidence、canonical projection 和 supersession 与本设计的信任边界
  接近。它选择把 episodic observation 显式入库，本项目目前选择让原始消息与
  embedding 承担该职责；若 consolidation shadow 暴露情景召回不足，应重新比较
  这两个方向。
- **On Verbalized Confidence Scores for LLMs**
  (Daniel Yang, Yao-Hung Hubert Tsai, Makoto Yamada,
  arXiv:2412.14737, 2024-12)：模型自报置信度普遍过度自信且高度依赖提示方法，
  支持“不以未校准 verbalized confidence 作为 canonical truth”的决定。
