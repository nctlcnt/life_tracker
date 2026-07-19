# ADR-0001: 记忆系统 v4 —— 四层记忆模型与严格 curator 管线

- 状态:Accepted
- 日期:2026-07-19
- 关联:Linear LT-133(epic)、LT-135(token 窗口 compact)、LT-136(curator 调度)
- 决策者:项目所有者(与 Claude 多轮设计讨论后定稿)

## 背景

Bot 需要跨会话的"记得我"能力。此前的手段各有失配:

- **明文上下文**:贵且短,超过窗口即遗忘;
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
5. **curator/compact 用带思考阶段的模型**(当前 glm-5.2 主力)。

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

后续工作(不改变本决策):LT-137(bot 读取 DB 的 digest/search 工具)、
LT-143(compact 确定性校验:分带日期、钩子保留)、LT-144(memory_key)。
