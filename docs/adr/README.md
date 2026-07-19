# 架构决策记录(ADR)

记录本项目中**难以逆转、且有真实备选被否掉**的架构决策。
过程性讨论留在 Linear issue/comment 里;ADR 是讨论沉淀后的定论,两边互相链接。

## 约定

- 文件名 `NNNN-短横线-标题.md`,编号递增,永不复用;
- 结构:背景 → 决策 → 考虑过并否决的方案 → 后果(正面与代价都写);
- ADR 不改写历史:决策变了就写新 ADR 取代旧的,旧的把状态改为
  `Superseded by ADR-NNNN`,原文保留;
- 状态取值:Proposed / Accepted / Superseded by ADR-NNNN / Deprecated。

## 什么时候写

满足任意一条就值得写:

- 引入或替换一个存储层、数据流方向、外部依赖;
- 否决了一个"大家第一反应都会想到"的方案(这条最有价值);
- 未来的自己会问"当初为什么这么做"。

纯实现细节(改个函数、换个库版本)不写 ADR。

## 索引

| 编号 | 标题 | 状态 |
|---|---|---|
| [0001](0001-memory-v4-four-layer-model.md) | 记忆系统 v4 —— 四层记忆模型与严格 curator 管线 | Accepted |
| [0002](0002-fixed-batch-model-comparison.md) | 模型选型用同批消息对照评测 | Accepted |
