# Memory 系统术语表

> 目的：Memory 相关的文档、代码注释和讨论必须使用本表定义的术语。发现文档用词与本表冲突时，以本表为准并回头修文档。
> 每个词条标注决定状态：**已定**（各文档一致、无争议）、**倾向**（本轮讨论达成方向、尚未写回设计文档）、**待定**（仍有未决分歧）、**弃用**（禁止在新文字中使用）。
> 最后更新：2026-08-22

## 使用规则

1. 英文术语是规范名（与代码字段一致），中文括注只帮助阅读，不作为第二个规范名。
2. 枚举取值（如 `hypothesis`、`asserted`）只用来指代字段取值，不得借用为日常形容词。泛指"AI 猜的东西"时写"推断类记忆"，不写"hypothesis"。
3. 新增概念先加词条再使用；改变某词含义必须同时更新本表和引用它的文档。
4. **2026-08-22 状态更新**：07-23 那一轮标为「倾向」的词条已经写回
   [`plan/memory-v4-design.md`](plan/memory-v4-design.md)，因此升为「已定」；
   [`plan/memory_database_schema_plan.md`](plan/memory_database_schema_plan.md)
   随后也已按同一方向修订，`score` 从字段定义中移除、降为未决问题 4 的候选方案之一，
   verification model 明确标注为未决问题 5。三份文档现已一致。
5. 「已定」只表示**当前各文档一致、无争议**，不表示已经在代码里实现。
   实现状态一律看设计文档第 2 节的状态表，不要从本表的「已定」推断功能已上线。

## 一、存储与分层

| 术语 | 定义 | 状态 |
|---|---|---|
| Message DB / `conversation_messages` | 追加式原始消息表，一切证据的最终来源。内部 `conversation_messages.id` 是唯一合法的消息引用；平台（Discord）message id 由 adapter 映射，不进入记忆系统。 | 已定 |
| compact（对话折叠摘要） | 有损的对话轨迹摘要，只负责对话连续性。**不是**长期记忆的证据来源，curator 只读原始消息。 | 已定 |
| canonical store / `personal_memories` | 长期记忆的权威存储表。注意：**canonical 指"权威存储"，不等于"可当事实使用"**——后者由每条记忆的 `status` 决定（见第五节）。 | 已定（单表方向） |
| embedding | 消息向量索引，用于检索"当时怎么说的"。检索结果不自动成为当前事实。 | 已定 |

## 二、记忆条目与字段

| 术语 | 定义 | 状态 |
|---|---|---|
| memory（记忆条目） | `personal_memories` 的一行：一个 claim 加元数据和证据关系。任何 status 的行都叫记忆条目。 | 已定 |
| claim（陈述） | 记忆条目所断言的那句话，必须是单一的、可独立判断真假的陈述。**claim 是内容，不携带可信度**——可信度由 status 表达。 | 已定 |
| `memory_type` | 业务分类枚举：`fact` / `preference` / `pattern` / `goal` / `constraint` / `relationship`。最终枚举待定。 | 待定 |
| `basis` | 当前全部证据与 claim 的**关系类型**，取值 `asserted` / `supported` / `inferred`。它是路由标签，不是置信度分数。 | 已定 |
| `scope` | claim 覆盖的范围枚举：`specific_event` / `specific_item` / `entity` / `category` / `general_pattern`。最终枚举待定。 | 待定 |
| `stability` | 时间稳定性：`temporary` / `unknown` / `stable`。描述"这件事本身会不会随时间变"，不描述证据强弱。 | 已定 |
| `gap`（证据缺口） | 自然语言说明"当前证据为什么还撑不满这个 claim"。`basis = asserted` 时原则上为空。 | 已定 |
| alternatives（备选陈述） | 与当前 claim 竞争的其他合理陈述，纯文本，**不是记忆条目**。用途仅限生成确认问题的选项；用户选中某条时经 `supersede` 转正。 | 已定 |
| `superseded_by` | 指向替代本条的新记忆条目 id。 | 已定 |

## 三、证据模型

| 术语 | 定义 | 状态 |
|---|---|---|
| evidence（证据） | 支撑或反对某个 claim 的消息引用。规范单位是证据组，不是单条消息。 | 已定 |
| evidence group（证据组） | **一份**证据 = 一组消息（至少一条 assertion，加零或多条 context）。它是"独立证据计数"的单位：同一轮对话里重复三遍只算一组。组边界来自 curator 操作的 `sources[]` 数组，组 id 在 apply 时由代码分配。 | 已定 |
| assertion / `is_assertion` | 消息在证据组**内部**的角色：用户直接表达内容的那条消息。只有 user 消息可以是 assertion；assistant 消息永远只能是 context。实现为布尔列 `is_assertion`（替代旧设计的 `member_role`）。 | 已定 |
| context（语境消息） | 证据组内为 assertion 提供解释所需上下文的消息（典型：assistant 的问句）。context 自己不能确立任何事实。 | 已定 |
| `evidence_role` | 证据组对**某条记忆**的作用：`supports` / `contradicts` / `supersedes` / `contextualizes`。挂在组与记忆的关系上；同一组可以对不同记忆有不同 evidence_role。 | 已定 |
| quote（引文） | 消息 `content` 的连续原文子串，validator 机械校验。允许忽略空格和大小写差异，不允许 AI 改写。 | 已定 |
| 独立证据（组） | 不同对话场合、不同事件来源的证据组。第一版的分组即"每个 curator 操作的 sources 为一组"；跨操作的独立性判定规则待定。 | 待定 |

## 四、Curator 管线与操作

| 术语 | 定义 | 状态 |
|---|---|---|
| curator | 后台整理模型及其管线：读取冻结消息区间和相关旧记忆，输出 proposal。 | 已定 |
| curator profile / `curator_name` | 一个 curator 候选身份 = 模型 + preset + prompt 版本的组合，键为 `curator_name`。cursor、记忆条目、查重、`operation_id` 都按 profile 隔离。 | 已定 |
| proposal（提案） | 一次 curator run 输出的结构化操作集合。模型只能提案，不能写库。 | 已定 |
| operation（操作） | proposal 中的一项：`create` / `attach_evidence` / `supersede`。不提供通用 `update`。`archive` 不保留：纠错时用户陈述的新事实（含否定形式）作为替代 claim 走 supersede；纯否认、无新事实时停在 `disputed`（hidden）。 | 已定 |
| `operation_id` | 操作的幂等键，唯一约束需带 profile 命名空间。 | 已定 |
| validate（确定性校验） | 代码对 proposal 的机械校验：JSON Schema、消息存在性、quote 子串、目标记忆状态等。不判断语义正确性。 | 已定 |
| apply / shared mutation boundary（共享写入边界） | 唯一的写库入口：在一个 Transaction 中提交记忆、证据和 cursor，不调用模型、不重新解释 proposal。 | 已定 |
| cursor / checkpoint | curator 的消息游标，按 `(curator_name, channel_id)` 记录处理进度。空 proposal 成功 apply 后同样推进。 | 已定 |
| repair（定向修复） | 确定性校验失败后，用同一模型只修格式/字段/引用的一次重试。 | 已定 |

## 五、状态与晋升

| 术语 | 定义 | 状态 |
|---|---|---|
| `status` | 记忆条目的使用权等级：`hypothesis` / `provisional` / `confirmed` / `disputed` / `superseded`。由代码计算，AI 不得直接提交。 | 已定（单表状态阶梯） |
| injection permission（注入权限） | 聊天模型对一条记忆的使用权限，由 status 唯一映射：`assert`（作为事实直接使用）/ `hedge`（带限定语使用）/ `probe_only`（只能用来生成确认问题）/ `hidden`（不进入 prompt）。权限只回答"能不能用、怎么用"；先问哪条由 priority 决定，两个轴不混。 | 已定（2026-07-23 拍板映射） |
| `hypothesis` | status 取值：证据只够合理猜想。权限 `probe_only`，不得注入为事实或个性化依据；在待确认队列中的先后由 priority 决定。 | 已定 |
| `provisional` | status 取值：有明显支持但未经用户确认。权限 `hedge`：可带限定语引用（"你之前好像提过……"），不得作为确定事实。 | 已定 |
| `confirmed` | status 取值：**存在用户确认事件**（见 confirmation）。权限 `assert`。 | 已定 |
| `disputed` | status 取值：存在未解决的硬冲突，**且尚无替代 claim**——一旦用户给出新事实即走 supersede 变为 `superseded`。权限分两种情形：模型推断出的软冲突 → `probe_only`（值得向用户澄清一次）；用户已明确否认 → `hidden`（不再追问，出现新证据才重新处理）。 | 已定（权限细则待确认） |
| `superseded` | status 取值：已被新条目替代，权限 `hidden`，仅供回溯。与 `disputed` 的分界：有没有新 claim 顶上来。 | 已定 |
| promotion（晋升） | status 向上迁移这件事。核心已定：`confirmed` 只能由用户确认事件驱动，证据积累不能替代确认。仍待定的只剩 `hypothesis` → `provisional` 的判定规则（`basis = supported` 直接进入？`inferred` 需要几组独立证据？）。 | 待定（范围已缩小） |
| confirmation（用户确认） | 用户对确认问题的明确肯定回复，或自发的等价直接表达。它是一个**事件**，不是状态。 | 已定 |
| ask / 确认问题 | bot 就某条推断类记忆向用户提出的问题，消息 metadata 绑定 `memory_id` 和 alternatives。每次回复最多问一条。 | 已定 |
| verification model（验证模型） | 记忆首次满足晋升 confirmed 条件时做检查的模型（查 scope 是否超出证据、alternatives 是否排除）。只能返回结论，不写库。是否保留、豁免哪些路径待定。 | 待定 |
| score | rev 1 的数值计分机制。2026-07-23 状态表拍板后已无消费者：status 由事件和证据类型决定，排队由 priority 负责。除非 `hypothesis` → `provisional` 规则最终选用计分，否则转入弃用。新文字不要用"分数"描述可信度。 | 待定（大概率弃用） |
| priority（排队优先级） | 待确认队列的排序值（相关性、新近度、ask 次数）。**只管先问谁，不携带真值语义**，与 status/晋升无关。 | 已定 |

## 六、运行模式与模型选择

| 术语 | 定义 | 状态 |
|---|---|---|
| manual review（人工审核） | 当前模式：proposal 经人工接受后 apply。是模型选型期的临时闸门，不是永久架构。 | 已定 |
| shadow | 只写不问不注入的试运行模式：proposal 正常 apply 进该 profile 的记忆集，但不触发提问、不进聊天 prompt。 | 已定 |
| auto-apply | validate 通过即 apply，无人工闸门。开启前提：按 status 区分的注入政策已实现（不变量在读取侧）。 | 已定 |
| model selection / 盲评 | 多个 curator profile 对同批消息各自产出记忆集，匿名化后人工打分选优。`run_curator_blind_eval.py` 是其工具。选型必须发生在提问循环开启之前。 | 已定 |
| active profile | 当前被选中、唯一向聊天注入并拥有提问权的 curator profile。 | 已定 |

## 七、易混词辨析

**claim ≠ hypothesis ≠ inferred。** claim 是陈述内容本身，任何记忆都有；`hypothesis` 是 status 枚举值（使用权最低档）；`inferred` 是 basis 枚举值（证据关系类型）。"AI 推测出来的东西"规范说法是"推断类记忆"（basis 为 inferred 或 supported 的条目），不说"一个 hypothesis"。**predict / 预测 一词禁用**——系统里没有对未来的预测这个概念。

**asserted（basis 取值）≠ assertion（组内角色）。** 词根相同但挂在不同层：`is_assertion` 是消息级——这条消息是用户的直接表达；`basis = asserted` 是记忆级——存在某条 assertion 的内容与 claim 基本等价。有 assertion 不代表 basis 是 asserted：用户直接说的可能只是 claim 的弱化版本，那是 `supported`。

**supersede（操作）/ `superseded`（status）/ `supersedes`（evidence_role）。** 一次 supersede 操作产生三个落点：新记忆条目建立；旧条目 status 变为 `superseded`；同一证据组对新条目的 evidence_role 是 `supports`、对旧条目是 `supersedes`。三个词形各说各的字段，不互换。

**confirmed（status）/ confirmation（事件）/ verification model（检查模型）。** 用户的 confirmation 事件是晋升 confirmed 的输入；verification model 是晋升前的额外检查者。"确认"一词只用于用户行为，模型做的那步叫"验证"。

**候选（candidate）现在只指 curator 候选模型。** 旧设计中"candidate"指独立候选层（`memory_candidates` 表）的条目；单表方向下该层不存在，推断类内容就是低 status 的记忆条目。为避免混淆，规定："候选"默认指 model selection 中的 curator profile；指记忆时必须说"hypothesis 记忆 / 推断类记忆"。

**disputed / superseded / archive 的分工。** 分界只有一条：**有没有新 claim 顶替**。用户给出新事实（包括否定形式——"我从来没喜欢过咖啡"本身断言了"用户不喜欢咖啡"这个新事实）→ supersede，旧条目 `superseded`；只有冲突或否认、没有新事实 → 停在 `disputed`。因此独立的 archive 状态和操作已取消，"曾经成立后来变了"与"从来就没成立过"的区别由替代 claim 的内容和证据表达，不再用两个状态区分。

## 八、弃用词

| 弃用词 | 原因与替代 |
|---|---|
| source（证据引用） | 旧扁平表 `personal_memory_sources` 的概念，无组身份。新文字用"证据组 / 证据组成员"。字段名 `sources[]` 在 curator JSON contract 中保留，但行文不再用它指代证据。 |
| `member_role` | 二值枚举实为布尔，由 `is_assertion` 替代。 |
| ask-only | "ask / 确认问题"仍指问题本身，不作权限名。 |
| candidate layer / `memory_candidates` | 独立候选层随单表方向弃用（若晋升机制讨论推翻单表方向，本条回滚）。 |
| Evidence archive（第四层旧名） | 与 embedding 层混用导致歧义，四层表述以设计文档修订后为准。 |
| 落库、兜底、对齐 等 | 沟通纪律禁用词，写"写入数据库"、"异常处理"、"保持一致"。 |
