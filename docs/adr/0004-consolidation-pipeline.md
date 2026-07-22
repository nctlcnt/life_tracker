# ADR-0004: 记忆巩固管线 —— 让 bot 发现我没说出口的事,并经我确认后记住

- 状态:Accepted
- 日期:2026-07-21(rev 4,收敛 proposal、revision 与确认协议;
  2026-07-22 增补候选表泛化到 curator 来源)
- 关联:ADR-0001(四层记忆模型,本 ADR 在其上加一条巩固回路)、
  ADR-0003(证据组模型,确认流程依赖它,实施顺序 0003 先行)
- 决策者:项目所有者(与 Claude 多轮设计讨论 + 代码级 review 后定稿)

> 本 ADR 刻意用平实语言书写。术语第一次出现时给一句解释,之后照常使用。

## 背景

长期数据库只收"已经明确成立的事实"。但有一类知识永远不会以事实
的形式被说出来:我说过五次"今天去看日出了,很开心",却从没说过
"我喜欢看日出"。按现行规则,这五条消息每一条都不足以入库,于是
这件事永远进不了数据库——哪怕我自己都未必意识到它。

靠检索(RAG)补不上这个洞:检索是"拿当前话题找相近旧消息",
话题不来,模式永远沉底;就算模型当场看出规律,发现也是一次性的,
不沉淀、不可纠错。

底线不动:ADR-0001 禁止把 AI 推理出来的结论直接存成事实。任何
解法都必须在这条线内成立。

代码级 review 修正了初稿的四个关键假设,本版已吸收:

1. **现有向量不是"每条消息一个语义坐标"**:检索用的 embedding
   输入包含当前消息**及其前 4 条**(为召回效果设计),相邻向量
   大量重叠,且 assistant 消息远多于 user 消息——直接聚类很可能
   聚出"bot 的重复话术",而不是我的经历。
2. **图片目前没有进 embedding 链路**:当前只发送文本 input。
   模型名带 VL 不等于链路已多模态。
3. **猜想不是记忆**:队列状态不能塞进 canonical memory 的
   status(active/superseded/archived)。
4. **现有 apply 校验器不适用**:它强制证据落在 curator 本批
   message 区间内,而聚类证据跨越数月历史。

## 决策

在四层模型之上加一条**巩固回路**,六个部分:

**一、不新建原始信号存储。**
曾考虑给库加"episodic"(单次经历型)记忆暂存日出类消息,否决。
原始信号留在消息表和向量里,只被聚类定期消化。

**二、聚类,但聚类单位是"user 消息",向量单独算。**
聚类就是"把语义坐标相近的消息自动归堆"(算法用 HDBSCAN:不用
预先猜堆数,允许把孤立消息标成噪音扔掉)。基于 review 发现 1,
**检索向量与聚类向量分家**:

- 聚类只针对 user 消息(用户经历才是巩固对象,bot 的话术不是);
- 为 consolidation 单独计算 user-event embedding:聚类的事实单元
  始终是该条 user 消息,但短回复可以带**最少必要语境**消歧。
  优先级是:显式 reply 指向的问句 → 紧邻且明确被回答的 assistant
  问句 → user 原文本身;仍无法确定含义的消息留作噪音,不强行
  聚类。assistant 文本只帮助 embedding 理解"写完啦"在回答什么,
  永远不算用户事实或候选 evidence。模型仍用 SiliconFlow 的
  Qwen3-VL-Embedding-8B(与检索同一模型、避免双空间语义漂移;
  embedding 是全账单最便宜项,重算一遍成本可忽略);
- 向量存独立列/表,与检索向量互不覆盖;
- shadow 期可拿"现有向量 + 仅取 user 行 + 相邻去重"做一组对照
  实验,验证专用向量是否确有必要——但主方案是专用向量。

频率约每月一次,纯 CPU 计算,VPS 秒级完成。

**附注(对应 review 发现 2):当前链路只 embed 文本,消息里的
图片附件停留在 metadata,未进入向量空间。选用 VL 模型是为将来
留的能力,不是已兑现的功能。"图片消息参与聚类"(拍的日出照片
飘进日出堆)列为后续工作,前置条件是多模态采集链路。**

**三、稳定的是候选 lineage,不是每次变化的成员集合。**
HDBSCAN 的簇编号在数据增长后会分裂、合并、重排——"这个堆上个月
问过了"不能靠簇编号来记。也不能把成员集合 hash 叫稳定指纹:
一条新 evidence 加入,hash 就会变化。本 ADR 把三种身份拆开:

- `lineage_id`:跨 run 的语义身份,表示"仍在讨论同一个潜在规律";
- `candidate_revision`:某一版不可变 proposal,其 hash 覆盖提炼文本、
  scope 与 source snapshot;
- `run_snapshot`:某次聚类的不可变输入,记录模型、参数、成员清单
  与聚类结果。

AI 产出的新 proposal 通过规范化主题、scope 与语义相似度匹配现有
lineage;匹配失败才建新 lineage。分裂/合并时显式记录 lineage 关系,
不偷偷复用一个 id。这样旧判断可复现,新 evidence 又能形成同一
lineage 的新 revision。

查重闸门:新一轮聚出的模式先与现存记忆和历史 lineage 比对。
已入库且语义未变的,用 `attach_evidence` proposal 追加证据;语义或
scope 已变化的,产生新的 memory revision proposal;全新的才建立
新 lineage。rejected / suppressed 只约束当时的 candidate revision
与 evidence horizon,不能永久封死一个主题。出现更晚 evidence 时
允许创建新 revision,偏好、身份等可变事实都可以重新成为候选。

**四、猜想进独立的候选表,不碰 canonical memory。**
基于 review 发现 3:猜想还不是记忆,不配拥有 memory status。
新建独立存储:

```
consolidation_candidates
  status: proposed | asked | approved | applied | rejected | suppressed
  (candidate_id, lineage_id, candidate_revision, 提炼文本, scope,
   confidence, confidence_rationale, source_snapshot_id, prompt_version,
   applied_memory_id, ...)
```

canonical memory 的 status(active/superseded/archived)原样
不动。候选状态枚举与 curator 枚举同待遇:单一来源定义,派生
核对。`rejected` 表示用户否认或纠正某一 revision;`suppressed`
表示人工或策略决定暂不展示某一 revision。两者都只约束当时的
evidence horizon,不能压制同 lineage 的未来 revision。

聚类只负责给出簇及确定性统计。随后由 AI 阅读簇内 user evidence,
提炼规律、scope、建议的 memory mutation 与 relation,并输出
`confidence` 及简短理由;整份结果以 `proposed` 状态落候选表,
不进入 canonical memory。簇大小、时间跨度、紧密度等确定性指标
作为模型输入并单独保存,不能冒充最终语义 confidence。

`candidate_revision` 是内容 hash:提炼文本、scope 或 source snapshot
任一变化都产生不可变的新 revision,旧 revision 的确认一律不能
套用。通过有效性与去重检查的新 supporting evidence 形成同 lineage
的新 revision,其 confidence 必须高于上一 revision,从而自然获得
下一次询问机会;重复或无效 evidence 不产生 revision。若新 evidence
改变或否定了规律,AI 必须改 proposal / scope,此时不适用单调增加
约束。

**队列机制**:`effective_priority` 由 proposal confidence 与时间
衰减计算,读时计算、不落库。每轮最多注入 top-k 条 `proposed`
revision,但确定性工具限制一轮最多询问一条,避免连环发问。同一
revision 一旦成功发问就置 `asked`,不得仅因冷却期结束自动重问;
只有更新的 evidence 形成新 revision 后才能再次进入 `proposed`。

没接话不是一条需要记录的 resolution:候选保持 `asked`,不生成
回应记录、不改变 canonical memory,也不扣真值 confidence。随着
时间推移,它的有效优先级自然衰减;没有 `cooling_down` / `expired`
这类为沉默额外制造的状态。

**候选表泛化(2026-07-22 增补)**:候选表不再是聚类管线专属。
curator 在日常批次里做出的对话内推断(basis 为 inferred /
supported 的 claim,例如从"我很期待 Mendelssohn 的"推出"用户
可能喜欢 Mendelssohn")与聚类猜想是同一种对象——AI 提出、用户
尚未确认的关于用户的 claim。本决策"猜想不配拥有 memory status"
的论证并不依赖猜想来自聚类,因此两种来源共用同一张候选表、同一套
状态机、同一个确认协议(决策五)和同一个 ask-only 注入隔离区
(配套一)。实现上:表增加 `origin` 字段(`curator` |
`consolidation`)及 `basis` / `gap` / `alternatives` / `scope`
字段;curator 来源候选不参与 lineage 匹配(`lineage_id` 为空),
revision 机制两种来源通用。用户明确断言的内容(asserted)不进
候选表,仍按 curator 现行路径直写 canonical memory。细化方案见
`docs/plans/memory_database_schema_plan.md`(rev 2)。

**五、确认要有协议,不能靠"猜是在回答谁"。**
基于 review 发现 8,bot 抛出猜想必须走结构化路径:

- bot 只能通过 `ask_consolidation_candidate` 动作提问。动作先持久化
  outbound intent;Discord 发送并保存消息后,统一的 post-send hook
  回填 `question_message_id` 并把 revision 从 `proposed` 改为 `asked`。
  现有普通模型 tool call 执行时尚未产生 Discord output id,因此不能
  假设 tool 自己已经知道这个 id。发送失败或进程崩溃时,outbound
  intent 留待重试或对账;只有 question message 已持久化后才允许
  进入 `asked`,不能留下一个实际没问过的 `asked` candidate。外部
  Discord 发送与本地 DB 不能组成原子事务,实现不得虚构 exactly-once;
- 成功提问落 confirmation_attempts 表:
  `candidate_id, candidate_revision, outbound_intent_id,
  question_message_id, asked_at`;
- **协议内所有消息 id 一律用内部数据库 id**
  (`conversation_messages.id`):Discord message id 对所有模型
  不可见,只存在于数据层;reply 关联的原始字段
  (`reply_to_message_id`,存 Discord id)到内部 id 的换算由
  确定性代码完成,模型永远接触不到平台 id;
- Discord reply 是可选但很强的确定性绑定信号,不是用户必须采用的
  交互。没有 reply 时,异步分类任务读取尚未解决的 confirmation
  attempt、问题文本和之后的 user 消息;聊天中的 scheduler / bot
  插队消息不自动切断绑定,但单靠"紧邻一条"也不能直接判定。隐式
  绑定只在问题后的连续回答窗口内成立;出现明确无关的 user 消息后,
  更晚的普通消息不得再回绑旧问题,避免数周后一句随机"对"误确认。
  用户日后显式 reply 旧问题仍可重新建立确定性绑定;
- 回应只分四种有效结果:`confirmed` / `corrected` / `rejected` /
  `unrelated`。`confirmed` 把当前 revision 置 `approved`并生成
  apply-ready proposal;`corrected` 不把原猜想当事实,而是严格从
  用户实际措辞生成一个新的 `approved` revision 及 apply-ready
  proposal,原 revision 记为 rejected;`rejected` 只压制当前
  revision / evidence horizon;
  `unrelated` 与没有回应一样不落 resolution,candidate 保持
  `asked`。只有前三种写 `response_message_id, resolution,
  resolved_at`;
- confirmed / corrected proposal 按 ADR-0003 生成 evidence group:
  AI 问句仅作 context,用户回答作 assertion,触发聚类的源消息分别
  建独立 group,默认只作 `contextualizes` 模式语境;只有某条原话
  本身明确表达该事实时才可 `supports`。入库依据始终是用户确认或
  纠正的原话,不是 AI 从旧消息归纳出的结论。分类任务只产 proposal
  与候选状态,永远不直写记忆库。

**六、apply:复用事务,不复用校验器。**
基于 review 发现 4/7:现有 validator 强制证据在本批 message
区间内,聚类证据跨月,天然不满足。因此:

- consolidation validator 负责其专属前提:candidate revision 未变、
  状态是 `approved`、source snapshot 全部真实存在;ADR-0003 的通用
  证据组不变量则由共享 mutation / repository 边界统一校验,不能
  只在 consolidation 或 curator 入口各校验一次;
- 校验通过后复用底层单事务 mutation。**能力前置**:现状 mutation
  与 curator 专属校验(run 核对、cursor 推进)交织在
  `apply_curator_batch` 内;接入前必须剥离出所有写入者共用的事务
  接口,curator、consolidation 与 Dashboard 都不能绕过它;
- canonical mutation、evidence groups / relations 写入、candidate
  `approved → applied` 与 `applied_memory_id` 回填必须在同一 DB
  事务完成。这样 approved 可以安全重试,applied 可证明落到了哪条
  memory,崩溃不会留下"状态显示成功但记忆没写入"的半成品。同一
  revision 最多 apply 一次;聚类进度使用 run snapshot,与 curator
  message cursor 无关。

**配套一:猜想注入有独立的 prompt 隔离区。**
top-k proposal **不与普通记忆混在同一个注入块里**——否则 bot 会在确认
前就把它当事实用("既然你喜欢日出,推荐你……")。hypothesis
单独成块,prompt 写死三条:它未经验证;只能用于择机提出一个
确认问题;禁止用它做个性化建议或断言用户事实。

**配套二:canonical 与 proposal 是两个注入维度。**

- 所有 `active` canonical memory 作为已确认的"用户卡片"全量注入;
  `superseded` / `archived` revision 不伪装成当前事实,需要回顾历史
  时再检索。若未来规模大到无法全量注入,必须另作架构决策,不能
  在实现里静默改成 top-k;
- 未确认的 `proposed` candidate 按 `effective_priority` 只取 top-k,
  放进独立 hypothesis block。它只能用于选择一次确认提问,禁止用作
  个性化建议或用户事实;
- canonical memory 的全文检索 / 语义检索仍可作为补充出口,但不
  替代当前的全量 active 注入。

**上线方式:shadow 先行**。shadow 可以持久化 run snapshot、lineage
和 candidate 供审计与调参,但不把 proposal 注入聊天、不主动提问、
不写 canonical memory。观察数轮后分别放开注入/提问与 apply——与
ADR-0001 决策 4 同一纪律。

## 考虑过并否决的方案

- **episodic 类型入库**:消息与向量本就存在,再存一份是重复
  建设,还向长期库引入噪音。否决。
- **放松"禁止 AI 推理入库"**:不必要。确认流程使最终入库证据
  是用户原话;人工 review 可以修改或压制 proposal,但不能绕过
  用户 assertion 把 derived 结论直接写入 canonical memory。
- **直接用检索向量聚类**(初稿方案):上下文窗口混叠 +
  assistant 占比失衡,会聚出 bot 话术。否决,专用 user-event
  向量。
- **候选状态塞进 memory status**(初稿方案):猜想不是记忆,
  混用污染 canonical 语义。否决,独立候选表。
- **复用现有 apply validator**(初稿隐含假设):message 区间
  约束与跨月证据冲突。否决,独立 validator + 复用事务。
- **要求用户必须 Discord reply**:真实交互里显式 reply 很少,
  不能让确认机制依赖它才工作。否决;reply 是强信号,无 reply 时
  走异步语义分类。
- **被无视扣 confidence**:混淆调度信号与真值信号,污染下游
  排序。否决,拆 confidence 与调度优先级。
- **同一 asked revision 冷却后自动重问**:把沉默当成许可,容易
  骚扰用户。否决;必须有新 evidence 形成新 revision。
- **只做读时推理 / 只做对话内发现 / 换小 embedding 模型省钱**:
  理由同初稿(一次性不可审计 / 跨话题模式撞不到 / 差价一杯
  奶茶还要全库重刷),均否决。

## 后果

正面:

- bot 获得"发现我没说出口的偏好"的能力,每条经我亲口或亲手
  确认,幻觉进不了库;
- 全链路可审计:run snapshot + lineage/revision + 提问/回应绑定 +
  证据组,每一步可复现;
- proposal 注入由 top-k 控制;canonical 当前按已确认的产品语义
  全量注入,规模上限需要持续观测;
- 候选表 / 独立 validator / prompt 隔离区三道墙,使巩固系统
  与 canonical 记忆之间的边界从"约定"变成"结构"。

代价与已知限制:

- 新增维护面:聚类 job、专用向量列、lineage / revision、候选表与状态机、
  confirmation_attempts、独立 validator、run 快照;
- user-event 向量需对存量 user 消息回填一遍(成本极低但要写
  脚本);
- 调参项:簇参数(min_cluster_size 初值 3)、lineage 匹配阈值、
  proposal top-k 与时间衰减函数,初值均为拍脑袋,shadow 期实测;
- active canonical memory 增长会增加 prompt token;达到实际预算上限
  时必须另立 ADR 决定分层/检索策略,本 ADR 不预先静默截断;
- reply 关联依赖 Discord 能力,换平台需重审确认协议;
- outbound intent 降低但不能消灭"Discord 已发送、DB 尚未回填时
  进程崩溃"的重复/孤儿消息风险;实现需用平台 nonce(若可用)或发送
  后对账收敛,并监控未绑定 intent;
- 图片不参与聚类(现状),多模态采集为独立后续工作;
- prompt 与参数进 git,proposal 记录生成时的 prompt 版本号,
  质量问题可追溯到 prompt 版本。

## 后续工作(不改变本决策)

- 实施工作按能力依赖拆分:user-event 向量回填 → 聚类 job 与
  run snapshot → lineage/revision 与查重 → 候选表及状态机 →
  proposal 注入隔离区与 outbound-binding 提问动作 → 回应绑定与
  四分类任务 → 从 `apply_curator_batch` 剥离共享单事务 mutation
  接口 → consolidation validator 接入 apply。ADR 只记录这些稳定
  的能力前置,不引用会改名、拆分或迁移的项目管理 ticket;
- 完整写入回路依赖 ADR-0003 的证据组与共享 mutation 边界先可用;
  生产查重与注入还依赖 canonical memory 已具备 embedding/search
  和 active-memory 注入消费能力。user-event embedding、shadow 聚类
  和 proposal 质量实验可以在这些前置完成前独立推进,但不能提前
  提问或写 canonical memory;
- 历史 user 消息的 consolidation embedding 回填完成后,用真实数据
  跑第一轮 shadow 聚类,人工
  检查前几条候选,作为调参基准;
- 附注:多模态采集(图片进 embedding 与聚类)待主回路稳定后
  另行评估。
