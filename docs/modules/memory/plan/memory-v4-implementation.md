# Memory v4 implementation plan

- 状态：active
- 最后更新：2026-07-22
- 目标设计：[Memory v4 architecture design](memory-v4-design.md)
- 架构决定：[ADR-0005](../0005-memory-trust-boundaries.md)

## 目标

把当前“聊天读取 `data/memory.md`，curator 另写 `personal_memories`”的过渡状态，
收敛为一条可审计的 Memory v4 闭环：

```text
message -> proposal -> validate -> canonical/candidate
        -> user confirmation when needed -> shared atomic apply
        -> prompt digest/search
```

本文只管理实施顺序、blocker 和验收条件。数据模型和状态语义以 architecture
design 为准；issue 状态和负责人以 Linear 为准。

## 当前基线

已经具备：

- `conversation_messages` 原始消息与 compact/history search；
- `personal_memories`、扁平 `personal_memory_sources` 和 `curator_cursors`；
- curator strict JSON parser、冻结区间、quote 校验、run 绑定；
- dry-run proposal、单事务 apply/cursor 和 scheduler shadow/auto-apply 开关；
- curator 固定批次盲评工具。

尚未具备：

- evidence group 三表与共享 mutation boundary；
- asserted/inferred 路由和统一 candidate layer；
- 候选提问、回应绑定和确认晋升；
- `personal_memories` 的 prompt digest/search 消费；
- consolidation 的 user-event embedding、snapshot、lineage 和 clustering；
- 从 `data/memory.md` 到 v4 canonical store 的最终切换与旧路径退役。

## 实施原则

1. 每个阶段先补 characterization/contract tests，再迁移生产写路径。
2. 新写入不能绕过 shared mutation boundary；旧路径在切换完成前保持可回滚。
3. 先让 curator 的 asserted claims 闭环，再接 candidate confirmation，最后接
   consolidation；不要反过来用大功能驱动基础 schema。
4. shadow 结果不等于生产能力。只有消费者接入、恢复路径和真实 UI/聊天验收
   完成后，阶段才算 production-ready。
5. 所有 destructive migration 先备份并做临时路径恢复演练。

## Phase 0：冻结 contract 和迁移基线

工作：

- 为当前 `personal_memories`、sources、cursor 和 apply 行为补 characterization
  tests；
- 明确 canonical mutation JSON Schema：create、update/replace、attach evidence、
  archive/dispute；
- 将代码枚举与 DB `CHECK` 的一致性变成自动测试；
- 记录生产表计数、active rows、source role 分布和 assistant-only source；
- 为旧 `data/memory.md` 与 `personal_memories` 建立一次性映射/差异报告，不在此
  阶段自动合并语义。

验收：

- 当前有效和失败路径都有测试保护；
- schema-contract drift 会在 CI 失败；
- 可以从备份恢复到临时 DB 并通过 integrity check；
- 没有改变生产 memory 或 cursor。

## Phase 1：Evidence group 和 shared mutation

工作：

- 新建 evidence groups、members、relations 三表；
- 实现 `member_role` 与 `evidence_role` 两个正交维度；
- 从 `apply_curator_batch` 剥离共享事务 mutation；
- 在共享边界校验 user assertion、连续 quote、context 绑定、relation 唯一性和
  supersession 完整性；
- curator adapter 负责连续消息区间/run/cursor 校验，校验通过后调用 shared
  mutation；
- 添加 `attach_evidence`，强化记忆时不强迫改写 summary；
- 将旧 sources 保守迁为 legacy groups，不猜测不存在的问答分组。

验收：

- curator、未来 candidate apply 和 Dashboard 没有第二套 memory 写入逻辑；
- assistant-only evidence 不能生成新的 canonical fact；
- 任一晚期失败会回滚 memory、evidence 和 cursor；
- 旧 source 数量和 message linkage 可对账，ambiguous legacy 保持待审；
- 现有 curator tests 与新增 migration/contract tests 全部通过。

## Phase 2：Claim 路由和 candidate layer

工作：

- curator proposal 增加 `basis`、`gap`、`alternatives`、`scope`；
- asserted claim 走 canonical mutation，inferred/supported claim 走 candidate；
- 建立统一 candidate 表和不可变 revision；支持 `origin = curator |
  consolidation`；
- 建立状态转换守卫和幂等 operation id；
- canonical 不增加模型自报 truth confidence；candidate 排序字段与 canonical
  truth 语义隔离；
- 在 shadow 数据上检查 basis 误分，重点防止 inferred 被错误直写。

验收：

- inferred/supported proposal 不可能通过普通 curator apply 写入 canonical；
- 重放同一 operation 不产生重复 candidate 或 memory；
- revision 内容变化会得到新 identity，旧批准不能套用；
- candidate 与 canonical 状态枚举互不复用。

## Phase 3：确认协议和 ask-only 注入

工作：

- 实现 outbound intent、平台发送、post-send 回填和 confirmation attempt；
- 协议内部统一使用 `conversation_messages.id`；
- 实现显式 reply 绑定和受限的隐式连续回答窗口；
- 实现 confirmed/corrected/rejected/unrelated 分类；
- 将 proposed candidates 放入独立 ask-only prompt block；
- 限制一次最多问一个 candidate，同一 revision 不因冷却结束自动重问；
- confirmed/corrected 只生成 approved apply proposal，最终仍走 shared mutation；
- candidate applied 状态和 `applied_memory_id` 与 memory/evidence 同事务提交。

验收：

- 发送失败不会留下假的 `asked`；崩溃后 intent 可以对账或重试；
- 数周后的随机“对”不会误绑定旧问题；显式 reply 仍可重新绑定；
- 未确认 candidate 不参与个性化建议或用户事实断言；
- corrected 以用户实际措辞建立新 revision，不把原猜想伪装成事实；
- UI/真实 Discord 路径完成端到端验收。

## Phase 4：Canonical memory 消费和旧路径切换

工作：

- 为 active `personal_memories` 提供稳定 digest；
- 实现 canonical memory search，并与 history search 区分结果类型；
- prompt 明确分隔 compact、canonical memories、history snippets 和 hypotheses；
- 定义 disputed/superseded/archived 的读取政策；
- 比较 `data/memory.md` 与 v4 rows，人工解决冲突后执行一次性切换；
- 切换 bot memory tools/API/Admin 到 shared mutation 或明确设为只读；
- 完成备份、恢复、回滚演练后退役 Markdown shadow/legacy CRUD。

验收：

- 新聊天确实读取 v4 canonical memory，不再只证明 API 能查表；
- active memory 能影响回答，superseded/archived/disputed 不会伪装为当前事实；
- bot tool 更新使用稳定 memory handle；
- 浏览器和 Discord 真实路径通过；
- 切换与回滚步骤写入 operations validation。

## Phase 5：Consolidation shadow

前置条件：Phase 1-3 完成。Phase 4 的 canonical search 至少可供查重使用。

工作：

- 为 user messages 生成独立 user-event embedding，不覆盖检索向量；
- 保存 embedding model/version 和可重建输入；
- 实现 clustering run snapshot、候选 lineage/revision 和分裂/合并关系；
- 先以 HDBSCAN 作为实验基线，但将算法和参数留在配置/实验层；
- 与 active memories、历史 lineage 和 candidate revision 查重；
- shadow 只保存 snapshot/candidate，不注入、不提问、不 apply；
- 使用真实数据人工审查 cluster coherence、proposal scope、重复率和 assistant
  contamination。

验收：

- 同一 snapshot 可以重放并解释候选来源；
- 聚类只以 user event 为事实单元，assistant 最多提供最少 embedding context；
- 新 evidence 不会因为 cluster id 变化丢失 lineage 历史；
- 参数调整有评测记录，不把初值写成架构保证；
- shadow 观察达到预定样本量后，另行决定是否开放提问。

## Phase 6：Consolidation rollout

工作：

- 将通过 shadow 质量门的 consolidation candidates 接入统一 ask-only 队列；
- 使用 candidate 专属 validator 校验 revision、snapshot 和状态；
- 复用 shared mutation，不复用 curator 的连续 message cursor validator；
- 分别开放 candidate 注入、主动提问和 apply，保留独立开关和回滚点；
- 监控孤儿 outbound intent、重复问题、错误绑定和用户拒绝率。

验收：

- consolidation 推断未经用户 assertion 无法进入 canonical；
- 每条 applied memory 可追到 candidate revision、confirmation attempt、用户回答
  和原始 pattern evidence；
- 重试不会重复 apply；
- 关闭 consolidation 不影响 curator、chat 或 canonical memory 读取。

## 暂不纳入

- 图片/附件的多模态 embedding 与聚类；
- 用 importance/recency/relevance 三维评分替代当前分层；
- canonical truth confidence；
- 因沉默自动降低真值置信度或反复追问；
- 在没有真实规模压力前静默把 active canonical 注入改成 top-k。

## 完成定义

Memory v4 只有同时满足以下条件才算完成：

- canonical、evidence、candidate 和 checkpoint 共享一致的事务边界；
- bot 的真实聊天路径读取 v4 canonical memory；
- inferred claim 必须经过用户确认；
- 每条新 canonical memory 都可追溯到 user assertion；
- 数据迁移、备份、恢复、回滚和浏览器/Discord 验收均有记录；
- 旧 `data/memory.md` canonical 路径已明确退役，而不是与新 DB 无限双轨。
