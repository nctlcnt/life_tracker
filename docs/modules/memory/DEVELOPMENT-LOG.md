# Memory 模块开发日志

> 本文记录 Memory 模块已经发生的实现演进，不描述未来架构。目标设计与后续施工分别见 [`memory-v4-design.md`](plan/memory-v4-design.md) 和 [`memory-v4-implementation.md`](plan/memory-v4-implementation.md)。
> recorded by codex.

最后核对：2026-08-22。时间线按新到旧排列。

## 证据与状态口径

- Git commit 用来确认代码实际落地；Linear `Done` 用来补充 issue 的目标和关闭状态。
- 运维结论只引用 [`docs/OPERATIONS-LOG.md`](../../OPERATIONS-LOG.md) 中实际执行过的测试、部署和生产 smoke test。
- **完成**：相关 issue 为 `Done`，且 Git 中存在对应实现；若有生产验收记录，会另外注明。
- **部分完成**：已有代码或测试，但 issue 尚未 `Done`，或真实消费者、迁移、生产验收仍缺失。
- **历史实现**：当时确实上线或合入，后来已被另一条实现替代；它不是当前架构说明。
- Linear 的状态不是实现证据。issue 描述中的目标若没有对应代码或验证记录，不写成已完成事实。

## 当前结论

截至 2026-08-22，原始消息写入数据库、token window、持久化 compact 和 `search_history` 已有 `Done` issue 与 Git 实现证据；compact 区间的异步 embedding 也已落地代码和测试，但仍属于未完成的 `LT-136`。长期记忆的当前生产读取源仍是 `data/memory.md`。

Memory v4 的 `personal_memories`、`personal_memory_sources`、`curator_cursors`、严格 proposal 校验、人工 apply 和 scheduler shadow 已经有实现，但 `LT-136` 仍为 `In Progress`。这些表尚未成为聊天的 canonical memory 读取源，因此不能把 Memory v4 写成已经完成。

开发曾在 2026-07-22 暂停于 curator 多模型 proposal 的人工审查阶段。审查真实输出时发现，已有设计没有覆盖部分实际情况；继续扩展写入和消费链路会把尚未厘清的语义固化进 schema 与 workflow，因此先停止 Memory 功能开发，转而更新 v4 architecture design 和 implementation plan。该暂停是一次架构校正，不代表现有已上线的 context、compact 或 Markdown memory 路径被撤回。

**该文档更新已于 2026-08-22 完成**（见下方同日条目）：设计方向改为单表状态阶梯加事后确认循环，design 与 implementation 两份文档已按新方向重写，术语表的「倾向」词条相应升为「已定」。这只是设计文档就位，不是功能落地——`LT-136` 仍为 `In Progress`，长期记忆的生产读取源依然是 `data/memory.md`。

证据组、事后确认循环、consolidation，以及从 Markdown 切换到数据库读取源，都仍属于后续实施范围。详见 [`memory-v4-implementation.md`](plan/memory-v4-implementation.md)。

## 2026-08-22 — 设计方向重写与 curator profile 冻结（文档与契约，未上生产）

- **Linear**：`LT-133` / `LT-136` 仍为 `In Progress`；同日修订了 `LT-133`、`LT-136`、`LT-137` 三个 issue 的描述。
- **性质**：本条记录的是**文档与代码契约**的变化，没有改变任何生产数据，也没有开启自动写入。
- **设计文档**：`memory-v4-design.md` 与 `memory-v4-implementation.md` 按 2026-07-23 拍板的单表状态阶梯方向重写。取消独立候选层、claim 跨表路由和 evidence group 三表；证据组降级为 `personal_memory_sources` 上的组标识加 `is_assertion`。最重要的变化是不变量从写入闸门移到读取权限——推断类内容照常写入，但低 `status` 只有受限的注入权限。
- **顺序调整**：状态阶梯字段（`basis` 与五值 `status` 等）改为受控 auto-apply 的前置，即在 `LT-136` 收尾之前插入一个阶段。原因是表里没有任何字段能标记"这一行是模型推断出来的"，先开自动写入会先写进一批无法分类的数据。
- **curator 契约**：`memory_type`、`action`、`evidence_role` 三个枚举原本在 prompt 里被手抄了第二份，现已全部改为从代码常量渲染；相关测试由断言字面小标题改为断言枚举覆盖与指令数据分离。
- **prompt 冻结**：修掉两处确定性校验冲突——"禁止英文双引号"与"quote 必须逐字一致"互相矛盾（现已把 `quote` 显式排除），以及 supersede 漏写"必须给出新的 summary 和 memory_type"。`CURATOR_NAME` 随之升到 `memory-curator-v2`，因为按术语表的定义改 prompt 就是换 profile。
- **发现的数据问题**：`personal_memories` 现有 6 行来自两个不同模型（`deepseek-v4-pro` 1 行、`glm-5.2` 5 行），profile 已经混合，而 `curator_model` 存的是模型名，无法机械推回 profile 键。处置办法（清空重跑）已写入实施计划，**尚未执行**。
- **仍未完成**：仍然没有证据证明非空 proposal 已在受控生产 auto-apply 后正确写入并推进 cursor。本条不改变 `LT-136` 的完成状态。

## 2026-07-17 至 2026-07-20 — v4 curator 基础设施（部分完成）

- **Linear**：[`LT-136`](https://linear.app/chachas/issue/LT-136) `In Progress`，不在 `Done` list。
- **主要 commits**：`be62f49`、`768a08d`、`1c1607f`、`ff9135f`、`846fc8e`、`5dfac7c`、`8acd0f2`、`66289bc`、`a7dceb9`、`563f74e`、`6ac4860`。
- **实际落地**：compact 成功后只为已折叠区间异步补 embedding；新增 `personal_memories`、`personal_memory_sources`、`curator_cursors` repository；实现 curator 的冻结消息区间、strict JSON proposal、证据校验、dry-run、人工 apply，以及 scheduler shadow/auto-apply 开关。
- **安全边界**：proposal 中的 quote 必须是对应原消息 `content` 的连续子串；消息文本只作为证据数据传入，不作为 curator 指令；人工 `--apply-proposal FILE` 读取并执行审核过的 JSON，不调用模型。memory mutation、source 写入和 cursor 推进由 `apply_curator_batch` 放在同一事务中。
- **验证**：`1c1607f` 增加 curator apply/rollback/幂等测试；`8acd0f2` 增加 scheduler service 测试。2026-07-18 的生产记录只验证了人工闸门 dry-run，且明确记载未 apply、未接 scheduler；后续 scheduler commits 没有对应的生产验收记录。
- **暂停点**：在人工比较不同模型生成的 curator proposal 时，发现部分真实情况无法由当时的架构和 schema 正确表达。为避免把评测中暴露的问题继续带入 canonical 写入链路，Memory 开发在此暂停，工作重心转为更新 architecture design 和 implementation plan；模型评测尚未形成可用于继续 rollout 的冻结结论。
- **仍未完成**：没有证据证明非空 proposal 已在受控生产 auto-apply 后正确写入数据库并推进 cursor；`personal_memories` 也尚未接入聊天 prompt、`search_memory` 或 check-in 消费。因此这批提交是可运行的基础设施，不是已完成的长期记忆闭环。

## 2026-07-17 至 2026-07-18 — LT-135 token window 与 compact（完成）

- **Linear**：[`LT-135`](https://linear.app/chachas/issue/LT-135) `Done`，完成于 2026-07-17。
- **主要 commits**：`ae437d9`、`b37bdb0`、`f9f20c8`、`a6bbdc7`、`e62edf1`、`4d71880`、`7d911fe`、`7280141`。
- **实际落地**：聊天与 check-in 从固定最近 20 条切换为 token window；窗口由持久化 compact summary 和明文尾巴组成；达到阈值时后台触发 compact；Admin 可选择 compact preset；TraceViewer 展示窗口组成。
- **后续修正**：修复超过单批上限时的 cursor 连续性；增加全量摘要原子重建；compact 输出改为固定模板和绝对时间锚点；格式或时间校验失败时只用同一 preset 定向修正一次，再失败则拒绝写回。
- **验证**：2026-07-17 生产部署记录包含 114 个 Python tests、frontend build、health、SQLite quick check 和一次受控 bootstrap compact；2026-07-18 的修正部署记录包含 156 个 tests、全量重建和相对时间检查。
- **边界**：compact 是有损的 discourse summary，不是长期记忆的 canonical evidence；长期记忆 curator 仍只读取原始 `conversation_messages`。

## 2026-07-17 — LT-134 单一 AI 引擎前置（完成，Memory 邻接）

- **Linear**：[`LT-134`](https://linear.app/chachas/issue/LT-134) `Done`。
- **主要 commits**：`e5018c9`、`3308e78`、`8fbcf18`。
- **实际落地**：先用 contract tests 冻结 provider、fallback 和 trace 行为，再删除 Claude、Gemini、OpenAI、Relay 四套分叉 adapter，收敛为 OpenAI-compatible 单引擎。
- **与 Memory 的关系**：这一步没有新增记忆能力，但让 compact、curator 和普通聊天复用同一条 completion/trace 路径，减少后续 Memory v4 的 provider 分支面。
- **验证**：2026-07-17 生产记录包含 84 个 tests、frontend build，以及 active、fallback、Gemini OpenAI-compatible 三个 preset 的真实 smoke test。

## 2026-07-17 — 长期记忆迁移到 Markdown（已上线的过渡实现）

- **Linear**：没有对应的独立 `Done` issue；后续退役该层由非 Done 的 `LT-132` 跟踪。
- **主要 commit**：`daad992`。
- **实际落地**：引入 `MemoryService`、repository abstraction 和 `MarkdownMemoryRepository`；`data/memory.md` 成为 durable memory 的 canonical source，原 SQLite `memories` 表在迁移期作为 rollback shadow；API、bot tools、prompt 注入和 scheduler 改走统一 service。
- **验证**：2026-07-17 生产 smoke test 对比了 Markdown 与迁移前 SQLite 的 18 条记录，内容逐条一致；memory API 和 document API 可读。
- **边界**：Litestream 只备份 SQLite，不覆盖 `data/memory.md`；运维台账仍把 Markdown 独立异地备份列为 `NOT TESTED`。该路径当前仍在使用，但目标是被 v4 canonical DB store 替代，而不是长期双轨。

## 2026-07-08 — LT-130 检索修复与主动历史检索（完成）

- **Linear**：[`LT-130`](https://linear.app/chachas/issue/LT-130) `Done`，完成于 2026-07-17。
- **主要 commits**：`2771513`、`15953aa`；同 issue 的 poll 开关在 `2667b4c`，但不是 Memory 核心能力。
- **实际落地**：将 embedding 检索阈值从硬编码移入模型配置；按 Qwen3-VL-Embedding-8B 的实测分数把阈值校准为 `0.50`；增加 `scripts/calibrate_embedding_threshold.py`；新增 `search_history` tool，让模型可用自然语言 query 主动检索窗口外历史。
- **验证与尾项**：2026-07-17 的部署 smoke test 覆盖 API、bot 和 scheduler 基线，但运维记录仍把 Discord 上的 `search_history` 实际使用列为待用户验证。Linear `Done` 因此只能说明 issue 已关闭，不能补足这一项真实交互证据。
- **后来变化**：v3 原本在消息写入后立即生成 embedding；`be62f49` 后改为只 embedding 已被 compact 折叠的区间，`search_history` 也以 compact cursor 为硬边界，避免重复返回仍在明文窗口中的消息。

## 2026-07-03 至 2026-07-04 — LT-125 Memory v3（完成，部分已被替代）

- **Linear**：[`LT-125`](https://linear.app/chachas/issue/LT-125) `Done`，完成于 2026-07-17。
- **主要 commits**：`f4b6182`、`5d23fe3`。
- **实际落地**：为 durable memory 增加 `memory_type`、`valid_until`，取消 20 条 FIFO 上限；为 `conversation_messages` 增加 embedding 字段和语义召回；将 trace 结构化写入 `ai_runs`、`tool_calls`；Memory 页面增加编辑、删除和分类能力；`valid_until` 统一为 UTC datetime text。
- **验证证据**：commit 同时加入 embedding backfill 脚本、数据库/API/UI 改动；Linear 的验收目标与这些实现相符。
- **历史状态**：v3 的 SQLite durable memory 随后由 `daad992` 的 Markdown canonical 替代；逐消息即时 embedding 随后由 compact 后异步 embedding 替代。`conversation_messages`、结构化 trace 和语义召回基础仍被 v4 继续使用。

## 2026-07-04 — LT-129 单一 prompt 模板（完成，Memory 邻接）

- **Linear**：[`LT-129`](https://linear.app/chachas/issue/LT-129) `Done`。
- **主要 commit**：`91f6593`。
- **实际落地**：将分散的 prompt sections 收敛为可编辑的单一模板，并以 placeholders 注入 memories、相关历史和动态上下文；增加模板渲染、边界条件和旧 prompt parity tests。
- **与 Memory 的关系**：它确定了记忆进入模型上下文的消费接口，但没有改变 memory 的存储或证据语义。

## 2026-06-09 至 2026-06-22 — 原始消息日志成为上下文来源（完成）

- **Linear**：[`LT-100`](https://linear.app/chachas/issue/LT-100) `Done`；[`LT-127`](https://linear.app/chachas/issue/LT-127) `Done`。
- **主要 commits**：`f299aa6`、`566d4b3`。
- **实际落地**：新增 append-only `conversation_messages`，记录 Discord inbound/outbound 消息并提供按 channel 读取 helper；随后 scheduler 和 Discord 路径从实时拉 Discord history 改为读取本地会话记录。
- **时间差异**：`LT-100` 在 Linear 的完成时间是 2026-05-29，而可定位的 Git commit 日期是 2026-06-09。本文以 Git 日期作为代码落地时间，不用 issue 的关闭时间替代提交证据。
- **演进意义**：这张表后来成为 token window、compact、history embedding 和 curator evidence 的共同原始数据源。

## 2026-05-09 — LT-49 AI trace（完成，审计基础）

- **Linear**：[`LT-49`](https://linear.app/chachas/issue/LT-49) `Done`。
- **主要 commit**：`1ef2b47`。
- **实际落地**：新增按日 JSONL trace、AI 各轮原始输出与 tool call/result 捕获、trace API 和前端 TraceViewer；trace 写失败不阻断主聊天流程。
- **与 Memory 的关系**：这不是记忆存储，但它为后来的 compact/curator trigger、模型选择、窗口组成和 proposal 调试提供了观测基础。v3 又把其中部分结构化写入 SQLite。

## 2026-05-08 — LT-33 Memory 列表展开（完成，UI 小改）

- **Linear**：[`LT-33`](https://linear.app/chachas/issue/LT-33) `Done`。
- **主要 commit**：`30543eb`。
- **实际落地**：Dashboard 的 Memory 卡片从只显示前 6 条改为可展开/收起全部记录。它只改变展示，不改变 memory schema 或读取语义。

## 2026-04-10 — 第一版 durable memory（历史实现）

- **Linear**：未找到直接对应的 `Done` issue。
- **主要 commit**：`2244c00`。
- **实际落地**：新增 SQLite `memories` 表和 `save_memory`、`update_memory`、`delete_memory` tools，将用户偏好与 deadline 作为显式长期记忆注入 AI。
- **当时限制**：读取仅取最近 20 条，新增第 21 条时 FIFO 删除最旧记录；是否保存完全依赖模型主动调用 tool，没有 conversation retrieval 或 provenance。
- **历史状态**：这条实现先被 LT-125 的 Memory v3 扩展，再被 Markdown canonical 和 v4 curator 路径逐步替代；它只用于解释系统为何演进，不代表当前设计。

## 仅有设计记录、尚不能计为实现

- `165c8a9`、`e7b4e3f`、`8aded81` 等 ADR/design commits 记录了 Memory v4、固定批次模型比较、evidence group 与 consolidation 方向；它们是设计证据，不是功能完成证据。
- 被删除的 ADR-0005 曾集中记录消息、模型输出、审核文件和 apply 的信任边界；这些仍有效的目标约束现在以 [`memory-v4-design.md`](plan/memory-v4-design.md) 为准。当前 curator 已实现其中一部分，但完整 v4 shared mutation/candidate/confirmation 仍以代码和 Linear 状态为准。
- `LT-136` 之后的 `search_memory`、check-in prefetch、prompt 全量重构、Markdown 退役，以及 evidence group/consolidation 相关工作，不在本次 Linear `Done` 证据范围内，不能写入“已完成”。
