# ADR-0005: 记忆分层与信任边界

- 状态: Accepted
- 日期: 2026-07-22
- 取代: ADR-0001、ADR-0003、ADR-0004 中混合记录的架构决定
- 当前设计: [Memory v4 architecture design](plan/memory-v4-design.md)

## 背景

对话原文、对话摘要、长期用户事实和模型推断承担不同职责。把它们混在一个
无限增长的 memory document 或同一个 prompt block 中，会造成来源不清、时效
冲突和模型幻觉自我确证。

同时，LLM 适合从对话中提出结构化语义变更，却不能作为“关于用户的事实”的
最终证人。系统需要明确模型、用户原话和确定性代码各自拥有的权限。

## 决策

采用以下长期边界：

1. **分层**：recent context、discourse compact、canonical memory 和 evidence
   archive 分别承担即时上下文、对话连续性、当前用户事实和来源追溯。
2. **Canonical 唯一性**：compact 和历史检索不保存或产生“当前仍成立”的权威
   用户事实；同一当前事实只在 canonical memory 中拥有权威版本。
3. **Evidence before belief**：canonical memory 必须可追溯到用户 assertion。
   Assistant 消息可以提供必要语境，但不能单独确立用户事实。
4. **推断隔离**：模型推断出的 claim 在用户确认前只能进入独立 candidate
   layer，不得作为 canonical memory 或个性化依据使用。
5. **提案与写入分离**：模型只生成 proposal。确定性代码验证 proposal，并在
   一个事务中提交 canonical mutation、evidence 和对应 checkpoint；apply 不重新
   调用模型或重新解释 proposal。
6. **历史可追溯**：事实变化通过 revision/supersession 表达，不用静默覆盖或删除
   证据历史。

Evidence group 的表结构、candidate 状态机、确认绑定协议、curator batch size、
聚类算法和模型选型属于可演进设计或实施策略，不由本 ADR 固定。

## 考虑过并否决的方案

- **一个 memory document 同时保存对话和长期事实**：无限增长，缺少当前状态和
  来源边界。
- **让 compact 保存事实结论**：compact 与 canonical memory 会形成两个不同步的
  权威版本。
- **让模型推断直接写入 canonical memory**：模型可以用自己的猜测为自己作证，
  无法建立可靠信任边界。
- **只保留向量检索，不维护 canonical memory**：能够回答“当时说过什么”，不能
  稳定回答“现在仍然成立什么”。
- **给 canonical memory 使用模型自报 truth confidence**：没有校准数据和明确
  消费者时不可证伪，不能代替用户证据和确定性验证。

## 后果

正面：

- 当前事实、对话连续性和历史证据各有唯一职责；
- 模型错误可以被拒绝、纠正和追溯，不会自动成为用户画像；
- curator、consolidation 和未来人工编辑可以复用同一写入信任边界。

代价：

- 需要独立的 canonical、evidence、candidate 和 checkpoint 概念；
- 语义是否被原话支持不能完全机械判断，仍需用户确认、人工审核或受限的
  auto-apply policy；
- 系统迁移期间必须明确区分当前实现和目标架构。
