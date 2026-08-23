# Memory v4 implementation plan

- 状态：active
- 最后更新：2026-08-22（按单表状态阶梯方向重划阶段）
- 目标设计：[memory-v4-design.md](memory-v4-design.md)
- 术语：[GLOSSARY.md](../GLOSSARY.md)
- 已发生的实现演进：[DEVELOPMENT-LOG.md](../DEVELOPMENT-LOG.md)

## 目标

把当前"聊天读取 `data/memory.md`，curator 另写 `personal_memories`"的过渡状态，
收敛为一条可审计的闭环：

```text
message -> proposal -> validate -> 单事务写入（含代码计算的 status）
        -> 按注入权限分档读取 -> 聊天消费
        -> 需要时提问 -> 用户回答成为新证据 -> 重算 status
```

本文只管理实施顺序、阻塞项和验收条件。数据模型和状态语义以设计文档为准；
issue 状态和负责人以 Linear 为准。

## 与上一版实施计划的差别

上一版的 Phase 1 是"evidence group 三表"，Phase 2 是"claim 路由和 candidate layer"。
这两个阶段随 2026-07-23 的单表方向作废，理由见设计文档第 1.1 节。重划后的差别是：

| 上一版 | 本版 |
|---|---|
| Phase 1 建 evidence groups / members / relations 三张表 | 证据组降级为 `personal_memory_sources` 上的组标识加 `is_assertion` 布尔列，且不再是第一个阶段 |
| Phase 2 建 `memory_candidates` 表，按 `basis` 把 claim 路由到两张表 | 取消。`basis` 保留为 `personal_memories` 的字段，参与计算 `status`，不决定存哪张表 |
| Phase 3 的"确认协议"是写入前的闸门 | 确认变成写入后的证据补充，因此从"前置闸门"挪到了靠后的阶段 |
| 不变量靠写入侧拦截 | 不变量移到读取侧：低 status 的记忆拿不到可断言的注入权限 |

另有一处顺序调整，是 2026-08-22 拍板的：**状态阶梯字段必须落在受控 auto-apply 之前**。
原因是当前表里没有任何字段能标记"这一行是模型推断出来的"，而聊天读取侧一旦接通就会
把所有行当作用户事实。先开 auto-apply 再补字段，会先写进一批无法分类的数据。
代价是 LT-136 的收尾边界要放宽——它原本明确写着"不在收尾时半途扩 schema"。

## 当前基线

已经具备：

- `conversation_messages` 原始消息，compact 与 `search_history`；
- `personal_memories`、扁平 `personal_memory_sources` 和 `curator_cursors`；
- curator 的严格 JSON 解析、冻结区间、quote 校验、run 绑定；
- dry-run proposal、单事务 apply 与 cursor 推进、scheduler 的 shadow / auto-apply 开关；
- curator 固定批次盲评工具 `scripts/run_curator_blind_eval.py`。

尚未具备：

- 状态阶梯字段（`basis`、`scope`、`stability`、`gap`、`alternatives`）与五值 `status`；
- 由代码计算 `status` 的逻辑；
- 证据组标识与 `is_assertion`；
- 按注入权限分档的读取与 prompt 装配；
- `personal_memories` 的检索能力与聊天消费；
- 事后确认循环（ask 的 metadata 绑定、priority 队列、晋升到 `confirmed`）；
- consolidation 的 user-event embedding、snapshot、lineage 与聚类；
- 从 `data/memory.md` 到 `personal_memories` 的最终切换与旧路径退役。

### 现有数据的实际情况

截至 2026-08-22，`data/life_tracker.db` 里：

- `personal_memories` 6 行，`status` 全部是 `active`；
- `personal_memory_sources` 8 行，全部带 quote，其中 7 行 `supports`、1 行 `contextualizes`；
- `curator_cursors` 1 行：`memory-curator-v1` 在一个频道上停在 `last_message_id = 240`。

数据量小到可以逐行人工重审，因此**状态迁移不构成阶段性阻塞**。

但这 6 行有一个必须先处理的问题：它们的 `curator_model` 分别是 `deepseek-v4-pro`（1 行）
和 `glm-5.2`（5 行）。也就是说，**表里已经混进了两个不同 curator profile 的输出**，
而设计文档第 10 节要求记忆条目按 profile 隔离。

**建议的处置办法：清空这 6 行，在 Phase 1 完成之后用冻结的 `memory-curator-v2`
从头重跑一遍。** 理由有四条：

1. 这 6 行是选型期的产物，来自两个不同模型，无论指派给哪个 profile 都是错的。
   指派给 v2 等于伪造来源；指派给两个 legacy profile，它们就永远不会是 active profile，
   等于留一批不会被注入、却仍要参与查重的死数据。
2. 它们不含任何原始消息里没有的信息。`conversation_messages` 是追加式的，
   240 条原文全在，重跑不丢东西。
3. Phase 1 会加上 `basis`、`scope`、`stability`、`gap`、`alternatives` 五个字段。
   保留这 6 行意味着要为它们逐行人工补齐这些字段；重跑则让它们从一开始就是完整的。
4. 重跑本身就是 LT-136 想要的验证——用真实数据跑通整条管线。

这条处置与 cursor 的处理方式是耦合的：**选择重跑，`memory-curator-v2` 的 cursor 就从
0 开始，不要把 v1 的 240 复制过去**；只有选择保留，才需要复制 cursor 位置。

执行时的约束：删除前先备份数据库，并且删除动作需要单独确认——本文只记录决定，
不构成删除授权。

这件事在 Phase 1 收尾、Phase 2 开始之前处理。

## 实施原则

1. 每个阶段先补 characterization / contract 测试，再迁移生产写路径。
2. 新写入不能绕过共享写入边界；旧路径在切换完成前保持可回滚。
3. 不变量在读取侧。任何"开放写入"的阶段，都必须先确认对应的读取侧分档已经就位。
4. shadow 结果不等于生产能力。只有消费者接入、恢复路径和真实聊天验收完成后，
   阶段才算可以上生产。
5. 所有破坏性迁移先备份，并做一次临时路径的恢复演练。
6. 未决问题不得由实现代码替我们默认掉。设计文档第 12 节的五条，
   拍板之前只能显式写成配置或显式拒绝，不能悄悄选一个值。

## Phase 0：契约冻结与迁移基线 —— **已完成（2026-08-23）**

**不改变任何行为，只建保护网。**

实际交付：

- `tests/test_memory_schema_contract.py`：把代码常量与数据库 `CHECK` 的一致性
  变成自动测试，并额外钉住两件事——`memory_type` 故意不加 DB `CHECK`
  （枚举未拍板，加了就是造第二份副本），以及新库与存量库升级后的列集合必须相同；
- `tests/test_memory_targetability.py`：把"curator 看得见哪些记忆""哪些记忆
  还能被操作命中"写成不依赖具体状态字符串的行为测试，为状态阶梯改造保底；
- `scripts/backup_and_verify.py`：用 SQLite 在线备份 API 拍快照并演练恢复；
- 快照 `data/backups/pre-status-ladder-20260823-005850.db`，`integrity_check` → `ok`，24 张表行数逐张比对通过。
  这是本项目**第一次**真正演练本地快照恢复，运维台账对应条目已从
  `NOT TESTED` 改为 `PASS`。

一项按建议后移：`data/memory.md` 与 `personal_memories` 的映射差异报告。
现有 6 行是两个 profile 混合的产物且已决定清空重跑，此刻做差异比对是在跟一批
即将丢弃的数据对账，没有可行动的结论。挪到重跑之后（Phase 2 尾段）再做。

工作：

- 为当前 `personal_memories`、sources、cursor 和 apply 行为补 characterization 测试；
- 把代码里的枚举与数据库 `CHECK` 的一致性变成自动测试（`CURATOR_MEMORY_TYPES`、
  `CURATOR_ACTIONS`、`CURATOR_EVIDENCE_ROLES` 与建表语句逐个核对）；
- 记录生产表的行数、状态分布、`evidence_role` 分布和仅有 assistant 来源的记录；
- 为 `data/memory.md` 与 `personal_memories` 做一次映射差异报告，本阶段不合并语义；
- 备份并演练一次恢复。

验收：

- 当前的成功路径和失败路径都有测试保护；
- 枚举漂移会让 CI 失败；
- 可以从备份恢复到临时数据库并通过完整性检查；
- 没有改变生产的记忆或 cursor。

## Phase 1：状态阶梯字段（LT-136 的新前置）

这是 2026-08-22 新插入的阶段。目的只有一个：**让表能够表达"这一行有多可信"**，
从而使后续的自动写入不会产生无法分类的数据。

按风险分成两片，**加法先行、重写在后**：加字段是纯加法，出错也只是多几列 NULL；
把 `status` 的 `CHECK` 从三值换成五值则要重建整张表，还要处理自引用外键和
取值不在新枚举里的存量行，风险等级完全不同，不该混在一次改动里。

### 1a：加法部分 —— **已完成（2026-08-23）**

- `personal_memories` 增加 `basis`、`scope`、`stability`、`gap`、`alternatives`
  五列，建表语句与存量库加列路径同时改，由 schema contract 测试盯住两者一致；
- `basis` 和 `scope` **故意不加 DB `CHECK`**：两者的枚举尚未拍板，加了就是
  造第二份副本，枚举定下时还要多一次建表重写；
- 新增 `bot/memory/status_ladder.py`：五值阶梯常量、status 到注入权限的映射、
  `compute_status()`。全是纯函数，不碰数据库，因此可以先于建表重写落地并测试；
- 未决问题 4（`hypothesis` 与 `provisional` 的分界）在代码里体现为
  `UNDECIDED_DEFAULT_STATUS`，一律落到 `hypothesis`（权限最低），
  并有测试标记它是占位符而非结论；
- 把"可被操作命中"从"必须等于 `active`"改写为"不在 `UNTARGETABLE_STATUSES` 里"，
  写成排除式而不是白名单，五值扩容时新状态自动保持可命中；
- 修掉一个潜伏的行为 bug：curator 取"已有记忆"清单原本按 `status="active"` 精确筛，
  换成五值后会查出空集，curator 看不见自己写的低状态记忆，下一批会把同一个
  事实重新 create 一遍。现改为 `exclude_statuses`。
- 迁移已在生产快照的副本上试跑：6 行全部保留、新列为 NULL、`integrity_check` → `ok`。

**注意**：生产库会在 bot 下次重启时拿到这五列（`Database.__init__` 跑迁移）。
这是加列，向后兼容，老行的新列为 NULL，读取侧必须容忍 NULL。

### 1b：`status` 三值换五值 —— 未开始

工作：

- `status` 的 `CHECK` 从 `active / superseded / archived` 换成设计文档第 5 节的五个取值。
  SQLite 无法直接修改 `CHECK`，只能重建表；`personal_memory_sources` 的
  `evidence_role` 扩容已经有同样做法的先例，照它办，注意 `superseded_by` 的自引用外键；
- curator 的 JSON 契约增加对应字段，validator 增加字段间一致性校验
  （`basis = inferred` 时 `gap` 不得为空，`basis = asserted` 时 `gap` 原则上为空，
  模型不得提交 `status`）；
- 存量行迁移：现有 6 行 `status = 'active'`，这个取值不在新枚举里，
  重建表时会被 `CHECK` 拒绝，因此必须先映射。**保守映射到 `hypothesis`**
  （权限最低，且这些行本来就要清空重跑），不要映射到看起来更"正常"的档位；
- 把 `status_ladder.compute_status()` 接进 apply 路径，取代当前的固定 `active`；
- `operation_id` 的唯一约束加上 profile 命名空间（这个字段目前**尚未实现**，
  属于净新增的契约工作）；
- 处理现有 6 行的 profile 混合问题（见上文"现有数据的实际情况"）。

**本阶段不动操作集合。** 设计文档第 7.2 节规定的三种操作
（`create` / `attach_evidence` / `supersede`）与代码现状不一致：代码里现在是
`create` / `update` / `supersede` / `archive`，`attach_evidence` 还不存在。
这套迁移放在 Phase 6，和证据组一起做，因为 `attach_evidence` 的意义正是"只加证据、
不改写 claim"，而这在没有证据组标识之前表达不出来。**因此 Phase 1 到 Phase 5 期间，
curator 补强一条记忆的唯一办法仍然是 `update`，`archive` 也仍然可用。**
这段时间里 `update` 会不会破坏"记忆可追溯到证据"这个约束，需要在 Phase 1 的
validator 里加一条限制：`update` 不得在不追加证据的情况下改写 claim。

还有两处命名要处理，**它们会改动 Dashboard 和 API**：

- `summary` 列改名为 `claim`（术语表的规范名）。这是纯粹的改名，内容不变，可以机械完成。
  **时机：紧接 1b 之后单独提交，不要拖到 Phase 3。** 已核实 `api/` 和
  `frontend/src` 目前都还没有消费 `personal_memories`，所以现在改的影响面是零；
  一旦 Phase 3 在这张表上建起 Dashboard，同一次改名就要连带改接口和前端。
  之所以不和 1b 合并，是因为把机械改名混进语义改动会让 diff 没法审。
- `curator_model` 改成 `curator_name`。**这一处不是改名，现有内容无法机械迁移。**
  该列现在存的是模型名（`deepseek-v4-pro`、`glm-5.2`），而 profile 键是"模型加 preset
  加 prompt 版本"的组合——同一个模型可能在不同 prompt 版本下跑过，光看模型名推不回
  profile。因此这一列只能配合上面那 6 行的处置一起决定：人工为每一行指定 profile，
  或者连同这些行一起清空重跑。

阻塞项：

- 设计文档第 12 节的未决问题 1（`memory_type` 枚举）和 2（`scope` 枚举）。
  **2026-08-22 已定的处置办法**（枚举本身仍未拍板，这里定的只是过渡期怎么办）：
  `memory_type` 维持代码现有的八个取值不动，等枚举拍板后一次迁移，不中途换一半；
  `scope` 先按术语表的候选取值落地，并在代码里显式标为暂定。
  这样 Phase 1 不必连带做一次记忆类型的重新分类。
- 未决问题 4（`hypothesis` 到 `provisional` 的分界）。在拍板前，`status` 计算函数里
  这一段必须显式抛错或显式落到 `hypothesis`，并留注释说明这是待决默认值，
  不能写成一个看起来像结论的阈值。

验收：

- 新字段有 schema-contract 测试；
- `status` 由代码计算，模型提交 `status` 会被 validator 拒绝；
- 权限映射是纯函数，五个 status 各有用例；
- 现有 6 行完成重审，每一行的新 `status` 有明确理由记录；
- 重放同一个 `operation_id` 不产生第二条记忆。

## Phase 2：LT-136 收尾——scheduler 与受控 auto-apply

工作：

- 冻结 curator profile（模型、preset、prompt 版本三者一起冻结）。
  **prompt 侧已于 2026-08-22 完成**：修掉了两处确定性校验冲突——"禁止英文双引号"
  与"quote 必须逐字一致"互相矛盾（现已把 `quote` 显式排除在该约束之外），
  以及 supersede 漏写"必须给出新的 summary 和 memory_type"。
  同时 `CURATOR_NAME` 升到 `memory-curator-v2`，因为改 prompt 就是换 profile。
  仍需冻结的是模型与 preset；
- 部署 scheduler，先验证自动 propose 的批次范围、trace、幂等和崩溃恢复；
- 受控开启 auto-apply，验证一个**非空** proposal 能原子写入并推进 cursor。

验收（这是 LT-136 的关闭条件，epic 里写明）：

- 至少一个非空 proposal 在生产受控自动写入成功，且 cursor 正确推进；
- curator worker 崩溃重启不重复写入、不丢区间；
- 重复信息不产生重复记忆；
- trace 可区分 curator 批次的范围和结果；
- Dashboard 能看到新写入记忆的 claim、reason、status 和原文来源。

需要注意：这一步是**在生产环境上开启自动写入**，不是本地测试。开启前需要单独确认，
并准备好关闭开关和回滚步骤。

## Phase 3：LT-137——读取侧接通

这一步形成"异步写入 → 读取 → 消费"的闭环。

工作：

- 实现 `search_memory` 工具：自然语言 query 向量检索长期记忆；
  长期记忆的相关度阈值必须单独校准，不得沿用对话语料的阈值；
- `search_history` 的职责收敛为"compact 摘要的原始细节补充"：
  以 `context_summary.upto_message_id` 为硬上界，明文尾巴零命中，
  不再用固定的 `exclude_recent=20` 猜窗口范围；
- **下线 `save_memory` / `update_memory` / `delete_memory` 三个写工具**：
  聊天模型永久没有直接写记忆的权限；
- check-in 执行前主动预查：按 check-in 主题检索长期记忆和近期的 plan 类记忆；
- 按注入权限分档注入，见下面的说明。

**这里有一处 Linear 描述需要同步修改。** LT-137 现在写的是"纯聊天每轮全量注入所有
`active` canonical memories，作为已确认的独立用户卡片"。在新方向下这句话有两处不成立：
`active` 已经不是 status 取值；而"作为已确认"对 `hypothesis` 和 `provisional` 的行是错的。
正确的表述是按注入权限分档：

- `assert` 权限的记忆作为已确认信息注入；
- `hedge` 权限的记忆必须带限定语，与前者分块，不能混在同一段文本里；
- `probe_only` 权限的记忆不进事实段落，只进待确认队列；
- `hidden` 权限的记忆不进 prompt。

验收：

- 实际聊天 trace 里能看到分档后的记忆块，`assert` 与 `hedge` 分属不同块；
- `hidden` 权限的记忆零命中；
- `search_history` 的所有结果 id 都不大于当时的 compact cursor；
- 聊天模型没有任何路径可以修改长期记忆；
- check-in 能针对长期记忆内容自然提问，trace 可见预查结果。

## Phase 4：LT-156——prompt 装配层

**不依赖上面任何一步，可以随时并行开工。** 它决定"规则写在哪、谁覆盖谁"，
不决定写什么内容。

工作按 LT-156 的描述执行，这里只记与 Memory 的接口：分档后的记忆块需要在装配层里
有确定的位置和确定的优先级，不能由各条 check-in 的模板各自复制一份。

## Phase 5：LT-138——prompt 内容重写

**必须等 Phase 3 和 Phase 4 都完成。** Phase 3 决定记忆以什么形态进入 prompt，
Phase 4 决定它写在哪一层。顺序反了会把内容重写两遍，因为两者都动 `main_template`。

工作：把记忆使用规则写进 Phase 4 定下的结构，包括三档注入权限各自的措辞约定
（什么时候可以直接断言、什么时候必须带限定语、什么时候只能提问）。

## Phase 6：证据组标识与共享写入边界

工作：

- `personal_memory_sources` 增加组标识和 `is_assertion` 布尔列；
  组标识在 apply 阶段由代码分配，模型不能提交也不能引用已存在的组；
- curator 的 JSON 契约用位置编码（`message_id` 加 `context_message_ids[]`），
  apply 阶段翻译成 `is_assertion`，见设计文档第 6.2 节；
- 从 `apply_curator_batch` 里剥离出共享写入边界，在那里集中校验设计文档
  第 6.4 节的七条不变量；
- 完成操作集合迁移：增加 `attach_evidence`，取消 `update` 和 `archive`
  （Phase 1 推迟到这里的那一项）；
- 旧记录保守迁为 legacy 组，不猜测不存在的问答分组。

阻塞项：未决问题 3（跨操作的独立证据判定规则）。第一版可以只做"每个操作的
`sources[]` 算一组"，但要在代码里显式标注这是第一版规则。

验收：

- curator 和 Dashboard 没有第二套记忆写入逻辑；
- 只有 assistant 来源的证据不能建立新的用户事实；
- 任何一步失败都会回滚记忆、证据和 cursor；
- 旧记录的数量和消息关联可对账，语义不清的 legacy 保持待审。

## Phase 7：退役 Markdown 记忆层（LT-132）

前置：Phase 3 和 Phase 6。epic 里明确要求在退役 Markdown 之前完成证据组工作。

工作：

- 比较 `data/memory.md` 与数据库里的行，人工解决冲突后执行一次性切换；
- 把 bot 的记忆工具、API 和 Admin 切到共享写入边界，或明确设为只读；
- 完成备份、恢复和回滚演练后，退役 Markdown 路径。

验收：

- 聊天确实只读数据库，不再读 Markdown；
- 切换与回滚步骤写入运维记录；
- 浏览器和 Discord 的真实路径通过验收。

## Phase 8：事后确认循环

**这一步之前，只有用户自发直接表达过的记忆能够到达 `confirmed`**（设计文档第 5.1 节的
第二条识别路径）。模型推断出来的记忆会一直停在 `hypothesis` 或 `provisional`，
除非用户碰巧自己说出来。这不是故障，但意味着在 Phase 8 之前，推断类记忆事实上
只能被用来提问或带限定语引用。

前置：Phase 6（第一条识别路径需要证据组和 `is_assertion`）。

工作：

- ask 的 assistant 消息 metadata 绑定 `memory_id` 和 alternatives；
- 实现 priority 排序（相关性、新近度、已提问次数），维护 `last_asked_at` 和 `ask_count`；
- 每次回复最多问一条；沉默不算否认，不自动重问；
- 用户回答进入后续 curator 批次，由 curator 提出 `attach_evidence` 或 `supersede`；
- 代码识别确认事件并重算 `status`；
- 用户选中某条 alternative 时，经 `supersede` 转成正式记忆。

阻塞项：未决问题 5（verification model 的去留），以及 `disputed` 权限分档的细则
（"什么算用户已明确否认"需要一个可机械判定的标准）。

验收：

- 提问失败或发送失败不会留下错误状态；
- 数周之后一句随口的"对"不会误绑定到旧问题；
- 确认之后 status 确实升到 `confirmed`，且升级理由可追溯到具体的证据组；
- 纠错路径产生新的替代 claim，旧条目变 `superseded`，而不是把原猜想伪装成事实。

## Phase 9：Consolidation

属于独立的后续 epic（LT-147），不阻塞前面任何阶段。
在单表方向下它不再需要独立的候选语义，产出的也是普通记忆条目，
只是 `basis` 通常是 `inferred`，算出来的 status 较低。

前置：Phase 6 的共享写入边界，以及 Phase 3 的检索能力（供查重使用）。

工作与验收沿用原计划的 consolidation shadow 与 rollout 两段，
但"候选表""候选状态机"相关的表述全部按单表方向重写。

## 需要同步修改的 Linear 描述

重划阶段之后，以下 issue 的描述与当前方向不一致，需要更新：

1. **LT-136**：收尾边界写着"不在收尾时半途扩 schema"，而 Phase 1 正是在收尾前扩 schema。
2. **LT-137**：写着"全量注入所有 `active` canonical memories，作为已确认的独立用户卡片"，
   需要改成按注入权限分档。
3. **LT-133 与 LT-137** 都引用了 `ADR-0003` / `ADR-0004` 作为架构决定的依据，
   但 `docs/adr/` 目录下的 ADR 已经在 commit `33c1304` 的文档重组中全部删除，
   ADR-0005 也在本次工作中删除。这些引用现在指向不存在的文件，
   应改为指向设计文档和术语表。

## 暂不纳入

- 图片和附件的多模态 embedding 与聚类；
- 用 importance / recency / relevance 三维评分替代当前的分层；
- 模型自报的置信度数字；
- 因为沉默就自动降低可信度，或反复追问；
- 在没有真实规模压力之前，静默把注入改成 top-k。

## 完成定义

Memory v4 只有同时满足以下条件才算完成：

- 记忆、证据和 cursor 共享一致的事务边界；
- 聊天的真实路径读取数据库，且按注入权限分档；
- 推断类记忆无法在没有用户确认事件的情况下获得可断言的注入权限；
- 每条记忆都可追溯到具体的原始消息和连续原文子串；
- 数据迁移、备份、恢复、回滚以及浏览器和 Discord 的验收都有记录；
- 旧的 `data/memory.md` 路径已经明确退役，而不是与数据库无限双轨。
