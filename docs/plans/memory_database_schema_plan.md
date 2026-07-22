# 记忆数据库结构与状态更新方案

- 版本:rev 2(2026-07-22,与 ADR-0001/0003/0004 对齐后的修订版)
- 关联:ADR-0001(四层模型与 curator 管线)、ADR-0003(证据组,
  本方案的证据结构完全遵循它)、ADR-0004(巩固管线,本方案泛化其
  候选表并复用其确认协议,见该 ADR 2026-07-22 增补)
- rev 1 → rev 2 的主要变更见文末"修订记录"

## 0. 两分钟读懂本方案(写给未来的自己)

整个设计只回答一个问题:**AI 从聊天里看出来的东西,什么时候可以
算"记住了"?** 答案是一条分界线:**用户亲口说过的,直接记;AI
推测出来的,先问,问到用户点头才记。**

用一个例子把全流程走一遍:

1. 你说"嗯嗯,我很期待mendelssohn的"。curator 事后读到这句,它
   能确定的只有"你期待这场音乐会",但它猜你可能喜欢 Mendelssohn
   这位作曲家。这是推测,不是你说过的话,所以不能直接写进正式
   记忆库,只能作为一条**候选**排队——和聚类管线发现的"你好像
   喜欢看日出"排在同一个队伍里。这就是"候选表泛化"的意思。
2. 候选在确认之前,bot 只被允许拿它做一件事:找个自然的时机问
   你一句,比如"你是喜欢 Mendelssohn,还是主要期待这场演出?"
   (ask-only)。它不许在确认前就说"既然你喜欢 Mendelssohn,
   推荐你听……"。
3. 你回答"对,我一直都挺喜欢Mendelssohn的"。这句是你的原话,
   系统把问句和答句打包成一份证据(ADR-0003 的证据组),候选
   晋升为正式记忆。从此 bot 可以把"用户喜欢 Mendelssohn"当
   事实用。
4. 半年后你说"其实我现在不怎么听他了"。旧记忆不删除:新证据把
   旧记忆标成 `superseded`(过去成立,现在变了),换上新的当前
   版本;如果你的意思是"当初就理解错了",旧记忆标 `archived`。
   一时分辨不清时,先标 `disputed`,暂停使用,等下一次澄清。

为什么这么设计,三句话:

- 分数和置信度这类数字都被否决了(ADR-0001 增补):模型自报的
  数字不可信,自己算的分数又没有环节消费,所以晋升只认一个
  信号——**你亲口确认**。
- basis(asserted / inferred / supported)不是打分,只是**分流
  开关**:asserted 走直接入库那条路,另外两个走候选排队那条路。
- 每条记忆都能追溯到原话(证据组),错了可以安全退役,不会出现
  "AI 幻觉写进人设还删不掉"的情况。

哪份文档管哪件事:ADR-0001 管分层大原则,ADR-0003 管证据怎么存,
ADR-0004 管候选怎么排队、怎么问,本方案管 curator 具体怎么写库。
以后回来看,先读本节,需要细节再往下跳。

## 1. 目标和边界

本方案处理 curator 管线对长期记忆的写入:创建、补充证据、替换、
争议处理,以及推断类内容从候选层晋升为正式记忆的路径。聊天消息
仍然先写入现有 Message DB;后台 curator 异步读取新消息和相关旧
记忆,返回结构化的操作建议;后台代码验证这些建议,并在同一个
Transaction 中写入数据库。

AI 只能提出操作建议。AI 不能分配 memory_id,不能决定内容最终落在
哪一层,也不能直接修改 status。

本方案不引入 importance 和 valid_until,也不引入任何计分机制
(rev 1 的 score 设计整体暂缓,理由见"待决问题")。

当前阶段 apply 仍然经过人工 review(ADR-0001 决策 2)。人工 review
是模型选型期的临时措施;本方案的验证规则按照"未来 auto-apply 时
仍然成立"的标准设计。

## 2. 分层与路由:断言进正式库,推断进候选层

写入位置只由一条二元规则决定:

- 用户明确表达过的内容(`basis = asserted`,quote 校验通过):
  按现行 curator 路径直接写入 canonical memory(`personal_memories`)。
- 推断类内容(`basis = inferred` 或 `supported`):一律写入候选层
  (ADR-0004 的 `consolidation_candidates`,泛化后同时接收 curator
  来源),等待用户确认后按 approved → applied 晋升。

因为晋升由"用户确认"这个事件驱动,而不是分数累积,所以 basis 从
rev 1 的"score 输入"变成了"路由开关"。basis 标错的后果是内容走错
队列,而不是置信度虚高:asserted 被误标成 inferred 时,只是多问
用户一次;推断被误标成 asserted 时,当前由人工 review 拦截,
auto-apply 之后由待决问题 1 的等价性检查承担。

### 2.1 canonical memory

`personal_memories` 保持现有结构与状态枚举(active / superseded /
archived),本方案新增一个状态值:

- `disputed`:一条 active 记忆收到了 `contradicts` 方向的证据组,
  冲突尚未解决。disputed 记忆暂停注入聊天 prompt,等待澄清。

证据一律通过 ADR-0003 的三张表(evidence_groups / members /
relations)关联。rev 1 的内嵌 `sources` 字段作废。

### 2.2 候选层(泛化的 candidates 表)

`consolidation_candidates` 增加来源字段 `origin`
(`curator` | `consolidation`),并增加以下由 curator 提供的字段:

- `basis`:`inferred` | `supported`(asserted 不会进候选层)。
  - `inferred`:claim 主要来自推断,仍然存在明显解释空间。
  - `supported`:证据没有直接复述 claim,但已经提供较强支持。
- `gap`:自然语言说明当前证据为什么还不能完全支持 claim。
- `alternatives`:其他仍然合理的 claim 文本,用于生成选择式确认
  问题。它们不是正式记忆。
- `scope`:claim 的适用范围枚举(specific_event / specific_item /
  entity / category / general_pattern,最终枚举见待决问题)。

curator 来源的候选不参与聚类的 lineage 匹配,`lineage_id` 留空;
revision 机制(内容 hash,内容或证据变化产生新 revision)对两种
来源通用。候选在确认前以 source snapshot 形式保存证据引用
(message_id + quote),正式的证据组与 relation 在 applied 时按
ADR-0004 决策五统一生成。

候选层的状态机、确认协议、注入隔离区全部复用 ADR-0004:未确认
候选 ask-only——只能用于择机提出一个确认问题,禁止用于个性化建议
或断言用户事实。rev 1 设想的"provisional 带限定语参与聊天"搁置;
如果 shadow 期观察到候选长期积压、问不出去,再显式增加该档位,
届时只改 prompt 隔离区的措辞,不动存储结构。

## 3. Curator 返回的操作

Curator 只允许返回三种操作:`create`、`attach_evidence` 和
`supersede`。没有需要处理的信息时,返回空的 `operations` 数组。
不提供通用的 `update`:如果 curator 可以提交任意字段修改,后台
代码无法判断它改动了哪些语义。

每个操作携带全局唯一的 `operation_id`,用于异步重试时的幂等保护。

### 3.1 `create`

claim 必须只包含一个可以独立判断真假的陈述。如果一句总结包含两个
可能具有不同证据或状态的陈述,curator 必须把它们拆成两条。

后台根据 `basis` 路由:asserted 直接写 canonical(此时 `gap` 与
`alternatives` 必须为空),inferred / supported 进候选层。

证据按 ADR-0003 的组结构提交:

```json
{
  "action": "create",
  "operation_id": "run-42-op-1",
  "memory": {
    "claim": "用户可能喜欢 Mendelssohn 的音乐",
    "memory_type": "preference",
    "basis": "inferred",
    "scope": "entity",
    "gap": "用户明确表达的是对一场音乐会的期待，尚未说明这种期待是否来自对作曲家的普遍偏好。",
    "alternatives": [
      "用户只期待这场具体音乐会",
      "用户喜欢的是 Mendelssohn 小提琴协奏曲",
      "用户主要期待该场演奏者"
    ]
  },
  "evidence_groups": [
    {
      "members": [
        { "message_id": 1102, "member_role": "context", "quote": "这周末的音乐会你期待哪首？" },
        { "message_id": 1103, "member_role": "assertion", "quote": "嗯嗯，我很期待mendelssohn的" }
      ],
      "evidence_role": "supports"
    }
  ]
}
```

这条 `basis = inferred`,进候选层,evidence_groups 暂存为 source
snapshot。同样的结构在 `basis = asserted` 时直接进 canonical,
证据组立即写入三张表。

### 3.2 `attach_evidence`

给现有对象补充证据。target 可以是 canonical memory,也可以是候选:

- target 是 canonical memory:新建证据组和 relation(ADR-0003 的
  attach_evidence mutation)。`contradicts` 方向的证据组会把 active
  记忆置为 `disputed`。
- target 是候选:证据并入 source snapshot,内容或证据变化产生新的
  candidate revision,按 ADR-0004 的规则重新获得提问机会。
- 用户在普通对话中自发说出了与候选 claim 基本等价的内容时,curator
  提交 `proposed_basis = asserted` 的 attach_evidence;后台把它
  等价于确认回复处理,候选进入 approved → applied 流程。

curator 可以同时提出新的 `gap` 和 `alternatives`。alternatives 的
排除必须留痕:每次从 alternatives 移除条目时,操作 log 记录排除
依据的 message_id,供后续验证环节查证。

```json
{
  "action": "attach_evidence",
  "operation_id": "run-53-op-2",
  "target": { "type": "candidate", "id": 7 },
  "evidence_groups": [
    {
      "members": [
        { "message_id": 1249, "member_role": "context", "quote": "你之前说很期待Mendelssohn？" },
        { "message_id": 1250, "member_role": "assertion", "quote": "对，我一直都挺喜欢Mendelssohn的" }
      ],
      "evidence_role": "supports"
    }
  ],
  "proposed_basis": "asserted",
  "proposed_gap": null,
  "proposed_alternatives": []
}
```

### 3.3 `supersede`

只作用于 canonical memory,处理 claim 本身已经不准确的情况。后台
在同一个 Transaction 中先写入替代记忆,再把旧记忆置为
`superseded`,`superseded_by` 指向新记忆。

ADR-0003 决策三的语义区分保留:"过去成立、后来变化"用 supersede;
"过去就不成立、被用户纠正"应把旧记忆置为 `archived` 并挂
`contradicts` 证据组。同一个证据组可以同时 `contradicts` 旧记忆、
`supports` 新记忆。

supersede 也是 `disputed` 的主要出口:curator 在后续批次里读到
用户澄清时,提出 supersede,用更准确的 claim 替换 disputed 记忆。

## 4. 后台验证

后台代码必须完成以下验证后才能写库:

1. JSON 符合固定 Schema,拒绝未知字段和非法枚举值。枚举的单一
   来源仍然是 `bot/memory/curator.py`(ADR-0001 决策 3)。
2. target 存在,且该记忆或候选出现在本次 curator run 的输入清单
   里("curator 已读取过"校验;输入清单的持久化实现见待决问题)。
3. 所有 message_id 真实存在;quote 是对应消息正文的连续子串,
   可以忽略空格和大小写差异,不接受改写。
4. ADR-0003 的组不变量在共享写入边界统一校验:assertion 成员必须
   对应 user 消息;每个证据组至少一条 assertion;context 消息必须
   真实存在并早于 assertion。
5. claim 的单一陈述检查:无法通过简单规则确认为单一陈述时,拒绝
   该操作并记录原因,不自动拆分(从严策略的调整见待决问题)。
6. 字段一致性:`basis` 为 inferred / supported 时,`gap` 不能为空;
   `basis` 为 asserted 时,`gap` 和 `alternatives` 必须为空;
   status 和晋升结果不允许由 AI 提交。
7. `operation_id` 具有唯一约束,异步重试不会产生重复写入。
8. 查重:`create` 和 `supersede` 的 replacement 都必须与现有
   active claim 做标准化比较(claim 文本、memory_type、scope);
   curator 来源的候选还要与现存候选查重,重复时应改走
   attach_evidence。
9. supersede 的替代记忆先验证、先写入,旧记忆的状态变更在同一个
   Transaction 内完成,不允许出现"旧记忆已失效、新记忆尚未建立"
   的中间状态。

## 5. 状态与晋升

canonical memory 的状态转移:

- `active → disputed`:挂上 `contradicts` 方向的证据组。
- `disputed → superseded`:出口一,supersede(澄清表明原 claim
  需要纠正)。
- `disputed → active`:出口二(待决),澄清表明原 claim 无误时,
  经 attach_evidence 抬回,抬回的判定规则与证据强度机制一起设计。
- `active / disputed → archived`:被用户纠正为"过去就不成立"。

候选层完全按 ADR-0004 的状态机:proposed → asked → approved →
applied,以及 rejected / suppressed。晋升的唯一驱动力是用户确认
(或 3.2 节的等价自发断言),没有任何分数阈值。

## 6. 确认流程

只有一套确认协议,即 ADR-0004 决策五:ask 动作先持久化 outbound
intent,post-send hook 回填 question_message_id,回应在连续回答
窗口内绑定,分类为 confirmed / corrected / rejected / unrelated
四种结果。curator 来源候选与聚类候选进同一个提问队列,按
effective_priority 排序,一轮最多问一条。

rev 1 第 7 节"消息 metadata 记录 memory_id"的方案作废:tool call
执行时 Discord 消息 id 尚不存在,metadata 无法在发送时写入。该
问题 ADR-0004 已经用 outbound intent + 回填解决,不需要第二套机制。

alternatives 在提问时用于生成选择式问题;用户排除某个 alternative
后,排除动作和依据消息的 id 写入操作 log。

## 7. 待决问题

1. **验证模型的去留与定位**。候选晋升已由用户确认把关,不需要
   模型验证。剩下的问题是:asserted 直写 canonical 这条路,在
   人工 review 退出后由谁把关。rev 1 第 6 节的验证模型可以转型为
   这条路径的 auto-apply 闸门,专门检查 quote 与 claim 是否基本
   等价(即 asserted 是否名副其实)。如果采用,必须避免 rev 1
   "只在首次达到条件时触发"的漏洞:每次 basis 升级或证据集变化
   都应重新触发检查。
2. **disputed 的第二个出口**(见第 5 节):抬回 active 的判定规则。
3. **证据强度机制**。rev 1 的 score 设计整体暂缓。将来如果候选
   排序需要比 confidence + 时间衰减更精细的信号,再重新设计,且
   必须遵守 ADR-0001 增补的约束:不引入模型自报的入库真值。
4. **单一陈述检查的松紧**。当前从严(规则无法确认时拒绝),
   shadow 期统计误拒率后再决定是否放宽。
5. **curator run 输入清单的持久化**。"已读取过"校验需要记录每次
   run 注入了哪些记忆和候选,可以复用 ADR-0004 run_snapshot 的
   思路,具体结构实施时定。
6. **`memory_type` 和 `scope` 的最终枚举**。
7. **`stability` 字段的去留**。rev 1 用它描述时间稳定性,但
   ADR-0003 决策三已经用时间、场景、用途的重叠比较处理事实演变,
   两者是否重复,需要单独评估;本版暂不加该字段。

## 8. 前置工作

1. `consolidation_candidates` 表迁移:增加 `origin`、`basis`、
   `gap`、`alternatives`、`scope` 字段,`lineage_id` 允许为空。
2. 为三种操作编写 JSON Schema,证据部分按 ADR-0003 的组结构。
3. 把 ask 动作从聚类专用泛化为通用的候选提问动作。
4. 操作 log 支持 alternatives 排除记录。
5. ADR-0003 列出的共享写入边界与组校验器是本方案的硬前置,实施
   顺序在前。

## 修订记录

- rev 2(2026-07-22):与 ADR-0003/0004 对齐。废弃 rev 1 的内嵌
  sources 结构(改用 ADR-0003 三表证据组)、hypothesis/provisional/
  confirmed 五级状态阶梯与 score 计分(改为 basis 二元路由 + 用户
  确认晋升)、独立的确认流程(并入 ADR-0004 协议)。保留三操作
  模型、operation_id 幂等、gap / alternatives / scope 字段(移入
  候选层)和 disputed 状态。
- rev 1(2026-07-22):单张记忆表 + 内嵌 sources + 五级状态阶梯 +
  score 计分的初稿(备份见 git 之外的会话记录)。
