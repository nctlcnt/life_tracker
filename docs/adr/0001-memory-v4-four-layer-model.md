# ADR-0001: 记忆系统 v4 —— 四层记忆模型与严格 curator 管线

- 状态:Accepted
- 日期:2026-07-19(2026-07-22 增补外部参考与 confidence 否决项)
- 关联:记忆系统 v4 实施、token 窗口 compact、curator 调度、
  ADR-0003(证据组)、ADR-0004(巩固回路)
- 决策者:项目所有者(与 Claude 多轮设计讨论后定稿)

## 背景

Bot 需要跨会话的"记得我"能力。此前的手段各有失配:

- **明文上下文**:贵且短,超过窗口即遗忘;没有随着时间的信息浓度变化
- **单个 memory.md**:无限增长、无出处、无"当前是否仍有效"的概念,
  模型幻觉写入后无法追溯也无法安全撤销;
- **embedding 检索**:召回原文有效,但只回答"当时说过什么",
  不回答"现在仍然成立的是什么"。

同时观察到一个反复出现的混淆:对话摘要(compact)和长期记忆(DB)如果
职责不清,同一事实会在两处以不同的新旧程度存在,产生自相矛盾的人设。

## 决策

采用四层分工,每层只回答一类问题:

| 层 | 存什么 | 回答什么 |
|---|---|---|
| Recent context | 最近对话原文(明文尾巴) | 我们此刻在聊什么 |
| Discourse compact | 话题轨迹、转折、指代、局部立场——**不存事实结论** | 我们之前聊过什么、聊到哪了 |
| Canonical memory DB | `personal_memories` 中当前有效的语义状态 | 关于用户,现在仍成立的是什么 |
| Evidence archive | `conversation_messages` + sources + `superseded_by` 链(是视图,不是新存储) | 这条记忆当初是怎么说出来的 |

一句话定位:**长期 DB 让 AI 知道你喜欢什么;compact 让 AI 知道我们之前
聊过"你喜欢什么";embedding 让 AI 找回你当时是怎样表达喜欢的。**

配套的关键机制决策:

1. **Compact 按 token 窗口触发**(20k 阈值),摘要按时间分带衰减:
   7 天内保留细节,7–30 天压成单行钩子,30 天以上删除(embedding 搜得回来)。
   compact 只覆盖被折叠的较早消息,禁止断言"至今"。
2. **Curator 严格管线**:propose(dry-run)→ 人工 review → apply。
   apply 绝不重新调用模型,只执行被 review 过的那份文件,单事务落库并推进
   cursor。证据规则:quote 必须是原文连续子串;assistant 消息不能单独确立
   用户事实;消息内容只是证据、不是指令(prompt 注入防御)。
3. **枚举单一事实来源**:evidence_role / memory_type 白名单只在
   `bot/memory/curator.py` 定义,repository 与 DB CHECK 从它派生或与之核对。
   (曾因 parser、DB CHECK、repository 三份拷贝失配导致 apply 全批回滚。)
4. **调度先走 shadow 模式**:自动 propose、不自动 apply,配置门默认关闭,
   部署即黑;观察期后再翻 `curator_auto_apply`。
5. **curator/compact 用带思考阶段的模型**(当前 DeepSeek pro 主力)。

## 考虑过并否决的方案

- **把聊天上下文直接并入长期 DB**:DB 无长度上限,会无限增长,且丢失
  "当前有效"语义。否决。
- **compact 里保存事实结论**:与 DB 职责重叠,来源不可追溯,新旧两处
  版本会打架。这是本 ADR 的核心边界决策:compact 记"聊过",DB 记"成立"。
- **三维打分检索(重要性/新近性/相关性)**:与分层模型解决同一问题但
  复杂度高得多,暂缓,先验证分层是否够用。
- **轻量无思考模型跑 curator/compact**(flash-lite 等):实测会静默丢弃
  旧钩子、把"将来时"畸变为"完成时",而确定性校验抓不住摘要层面的事实
  畸变。否决。
- **上线即 auto-apply**:模型幻觉写入的代价是污染人设,需要人工 review
  期建立信心。先 shadow。
- **给 canonical memory 加模型自报 confidence 字段**(2026-07-22 增补):
  LLM 口头置信度普遍过度自信,且校准好坏高度依赖提问方式(实证
  依据见外部参考 arXiv:2412.14737);本系统既没有标注数据可以
  校准阈值,也没有消费这个数值的环节,数字不可证伪、只会占用
  模型输出注意力。否决。ADR-0004 候选层的 confidence 是提问
  排序信号,最终由用户确认把关,不作为入库真值,与本条不冲突
  (ADR-0003 也已明确 canonical memory 无 confidence 字段)。
  证据强度的替代表达方式(离散证据类别、summary 措辞与
  memory_type 降级)另由后续决策确定。

## 后果

正面:

- 每层职责单一,同一事实只有一个权威版本(DB),矛盾人设问题从结构上消除;
- 每条记忆可追溯到原始消息,错误可通过 `superseded_by` 链安全退役而非删除;
- dry-run 与 apply 分离使人工 review 成为硬卡点,模型幻觉进不了库。

代价与已知限制:

- apply 暂时需要人肉环节,存量消化慢(50 条/批是模型可靠性上限);
- cursor、compact 游标等运行时状态存在 app_state,不在 git 里,
  回滚代码不等于回滚状态,运维时需要意识到这一点;
- 四层各自有 prompt/校验/调度,维护面比单一 memory.md 宽。

后续工作(不改变本决策):bot 读取 DB 的 digest/search 能力、compact
确定性校验(分带日期、钩子保留)、稳定 memory key。

## 外部参考

- **Eywa: Provenance-Grounded Long-Term Memory for AI Agents**
  (Resham Joshi, arXiv:2605.30771, 2026-05,
  https://arxiv.org/html/2605.30771v1)。独立研究者的 agent 长期记忆
  系统,分层结构与本 ADR 高度平行,收录时(2026-07)论文自报
  LoCoMo 判分准确率 90.19%、LongMemEval-S 88.2%。对照要点:

  - **分层**:Eywa 把稳定画像事实(canonical)、带日期的情景事实
    (observation)与技术事实(库版本、配置、否定性声明等)拆成
    不同的抽取模式,分开索引与检索。其 canonical 层与本 ADR 的
    canonical memory DB 对应;其 observation 层与 ADR-0004 否决的
    "episodic 入库"是同一设计空间里的相反选择——Eywa 显式存储
    情景事实,本项目让原始消息表 + 向量承担该职责。若巩固回路
    shadow 期发现情景类召回不足,重新评估时以该论文为主要参照。
  - **Provenance**:核心原则是 "evidence before belief"——用户消息
    存为不可变证据行,LLM 抽取的事实只是可修复、可重建的索引,
    每条事实保留指向源证据行的链接。动机与本 ADR "quote 必须是
    原文连续子串"及 ADR-0003 证据组相同;它额外做了一层确定性
    锚点校验(日期/实体/金额等 hard anchor 必须与源文本精确
    匹配,论文审计中 132 个候选拒绝了 11 个),可作为本项目
    确定性校验的扩展方向参考。
  - **冲突与过期**:写入时做 supersession,维持每个(主体,可变
    事实类型)至多一条 active 的不变量,与 `superseded_by` 链
    同构;同样只保证"有源文本支持",不做世界级真值验证。
  - **与待决问题的关系**:Eywa 的写入把关靠确定性验证函数
    (源文本重叠、锚点精确匹配、主语存在、否定与不确定性保留),
    而不是让模型自报数值 confidence——与本项目"确定性校验 +
    盲评"的路线互相印证。单次事件类信息(如听一场音乐会)在
    Eywa 中作为 observation 保留原始事实与日期,不强行提炼成
    canonical 偏好,这为"事件与偏好剥离"规则提供了另一个参考
    答案。

- **On Verbalized Confidence Scores for LLMs**
  (Daniel Yang, Yao-Hung Hubert Tsai, Makoto Yamada,
  arXiv:2412.14737, 2024-12, https://arxiv.org/html/2412.14737v2)。
  对 11 个模型 × 10 个数据集 × 17 种提示方法的 LLM 口头置信度
  校准实证研究,是"否决模型自报 confidence 字段"的主要外部
  依据。要点:

  - 过度自信在所有规模的模型中都存在;校准随模型变大改善,
    但改善主要来自准确率提升,不是过度自信本身降低。参数量
    ≥70B 的模型期望校准误差(ECE)仍约 0.1;最小模型(2B)的
    置信度几乎与准确率无关;
  - 校准好坏高度取决于提问方式:小模型用复杂提示(few-shot、
    多候选排序)反而显著更糟;大模型用组合提示最好也只能把
    平均偏差压到约 7%,作者结论是尚未达到实际部署的满意水平;
  - 对本项目的含义:在没有标注数据校准阈值、也没有环节消费
    该数值的场景下,让 curator 自报 1-5 或 0-1 置信度产出的
    数字不可证伪,只是表演打分。证据强度问题的解法方向见
    "考虑过并否决的方案"末条。
