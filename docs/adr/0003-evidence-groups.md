# ADR-0003: 证据组模型 —— evidence 扩展为带双维角色的消息组

- 状态:Accepted
- 日期:2026-07-21(rev 4,补充多对多证据关系与可变事实语义)
- 关联:ADR-0001(细化其决策 2 的证据规则,不推翻)、ADR-0004
  (consolidation 管线,其确认流程依赖本 ADR)
- 决策者:项目所有者(与 Claude 多轮设计讨论 + 代码级 review 后定稿)

## 背景

ADR-0001 的证据规则要求:quote 必须是原文连续子串,assistant 消息
不能单独确立用户事实。现状 `personal_memory_sources` 已支持一条
memory 挂多条 message(1:N),每条 source 携带 evidence_role
(supports / contradicts / supersedes / contextualizes)。

设计 consolidation 确认流程时暴露出缺口:确认产生的用户消息信息
密度极低。典型场景:

> Bot:"你是不是很喜欢看日出?"
> 用户:"对啊"

"对啊"作为证据无法自证含义,语义完全依附于前一条 assistant 消息。
现有模型里没有办法表达"这条 assistant 消息只是语境、不确立事实",
也没有办法把一问一答**作为一个整体**绑定到记忆上。该缺口不限于
确认场景:日常对话中大量用户消息依赖上文才能解读("那家店我又
去了""还是选第二个吧"),中文对话尤甚。

关键观察:ADR-0001 规则的措辞是 assistant 消息不能"**单独**"确立
用户事实。它禁止"AI 说了就算数",从未禁止 assistant 消息作为语境
出场。本 ADR 是对这两个字的形式化,而非规则的放松。

Review 进一步指出初稿的三处结构错误,本版已纠正:

1. "消息在组内的结构角色"(assertion/context)与"证据如何作用于
   事实"(supports/contradicts/…)是**两个正交维度**,不能塞进
   同一个枚举——否则无法表达"这条消息是 assertion,同时
   contradicts 一条旧记忆"。
2. 仅给 source 行加角色字段无法表达"组":区分不了同一条 memory
   下的两个独立问答组,也无法落实"每组至少一条 assertion"的
   约束(SQLite CHECK 不能跨行、不能 join)。组需要自己的身份。
3. 证据组不是某条 memory 的附属物。同一句"以前喜欢咖啡,现在
   喜欢茶"既支持新的当前记忆,又使旧记忆退出当前状态;若把
   `memory_id` 和 `evidence_role` 固定在组上,只能复制证据或丢掉
   其中一层关系。

## 决策

**一、角色拆成两个正交维度,各自独立枚举:**

- `member_role`(结构维度):`assertion` | `context`
  ——这条消息在证据组里扮演什么。
- `evidence_role`(语义维度):`supports` | `contradicts` |
  `supersedes` | `contextualizes`——这组证据如何作用于某条 memory。
  现有枚举原样保留,不扩充、不混入。

两个枚举都只在 `bot/memory/curator.py` 定义,repository 与应用层
校验一律 import(遵循 ADR-0001 决策 3)。DB CHECK 是 SQL 字面量、
无法 import,属于不得不存在的拷贝——以 schema-枚举一致性测试
断言核对,不靠人工同步(现状缺这条测试,列入实施工作)。

**二、证据组独立于 memory,schema 明确为三张表:**

```
personal_memory_evidence_groups
  (group_id, created_at, legacy, ...)

personal_memory_evidence_members
  (group_id, message_id, member_role, quote, ...)

personal_memory_evidence_relations
  (group_id, memory_id, evidence_role, created_at, ...)
```

证据组是一份不可变的"证词":成员描述原话及其必要语境;relation
描述这份证词如何作用于某条 memory。`member_role` 挂在成员上,
`evidence_role` 挂在 group-memory relation 上。同一条 memory 可以
关联任意多个组,同一个组也可以关联任意多条 memory,都不限制为
"旧、新各一组"。

证据组一旦被 apply 就不原地改写。新的消息形成新组和新 relation;
需要修正旧分组时,走有审计记录的迁移或退役流程,不静默篡改历史。

**三、区分"过去成立"与"过去就不成立"。**

事实会随时间和场景改变。`superseded` 只表示一条 memory 不再是
**当前默认解释**,不表示过去的用户或过去的证据消失。四种 relation
的语义固定为:

- `supports`:支持该 memory 在其时间、场景和用途范围内成立;
- `supersedes`:旧 memory 在过去可以成立,但新证据表明它不再代表
  当前默认状态;
- `contradicts`:在重叠的时间、场景和用途范围内不能同时成立,
  或用户明确纠正旧理解本来就是错的;
- `contextualizes`:补充或收窄适用范围,不直接肯定或否定。

因此"以前喜欢咖啡,现在喜欢茶"可以由同一个 group 同时
`supersedes` 旧的咖啡 memory、`supports` 新的茶 memory。若后来
又喜欢咖啡,创建新的咖啡 revision,不重新激活旧 revision。revision
链可以任意长;多个按时间或场景区分的 active memory 也可以并存。
被明确纠正为"过去就不成立"的旧 memory 进入 `archived`,并由
`contradicts` relation 指向纠正证据;不要把它标成表示"过去成立、
后来变化"的 `superseded`。新的正确说法若存在,另建 active memory,
同一 evidence group 可以同时 `contradicts` 旧说法并 `supports`
新说法。

系统判断两条记忆是否真正冲突时,比较的是时间、场景和用途是否
重叠,不是词面类别是否相同。例如"工作提神时以前喝咖啡,现在吃
煎蛋"可以是一条演变;"喜欢咖啡"和"喜欢煎蛋"也可以只是两个
并存偏好。AI 在 proposal 中提出 scope 与 relation;确定性校验器
只检查引用、结构和状态合法,不冒充语义裁判。

**四、组不变量在共享写入边界校验,不指望 DB CHECK:**

SQLite CHECK 无法跨行检查"每组至少一条 assertion",也无法 join
`conversation_messages.role` 验证"assertion 必须是 user 消息"。
因此组级不变量全部落在 repository / mutation 的共享写入边界
(确定性代码,非模型),DB CHECK 只保留单行可查的部分(枚举值合法性)。
curator、consolidation、Dashboard 和未来写入者都必须经过该边界,
不能各自实现一套宽严不同的校验。规则:

1. assertion 必须对应 user 消息;quote 是该消息原文连续子串
   (ADR-0001 规则原样保留)。
2. 每组至少一条 assertion——"assistant 不能单独确立事实"的
   形式化。
3. context message 必须真实存在并早于 assertion。显式 reply 关联
   存在时必须与记录的 message relation 一致;没有 reply 时,普通
   省略句只能引用仍处于连续回答窗口内的上文。具体窗口由
   ADR-0004 的回应协议定义。
4. 每条 relation 的 group 与 memory 必须存在;同一 group-memory-
   evidence_role 组合不得重复;每个非 legacy group 至少关联一条
   memory。`supersedes` 必须同时产生或指向一个新的当前 revision,
   不能只把旧事实退役而不给出当前解释。

"context 是否确有必要、是否只取最少语境、是否把 assistant 推测
偷渡成事实"属于语义政策,不能假装由确定性代码完全判断。proposal
生成 prompt 和人工 review 必须按这三条审查;共享 validator 负责
上述可机械验证的不变量。无论语义判断结果如何,assistant 文本都
只能帮助解释 user assertion,不能单独成为事实证据。

强化已有 memory 使用显式的 `attach_evidence` mutation:只新增
group 与 relation,不伪造 canonical confidence,也不要求为了附加
证据而改写 summary。canonical memory 当前没有 confidence 字段;
consolidation proposal 的 confidence 属于候选层,见 ADR-0004。

**五、存量迁移:legacy 分级,不做机械转换。**

初稿"全部迁移为单成员 assertion 组"在真实数据上不成立:生产中
既有 assistant source 位于 user source 之后,也有同一 memory 下
不同 evidence_role。旧表还没有 group 身份,迁移程序不能凭相邻或
同 role 就发明原本不存在的分组。迁移规则改为:

- 每条旧 source 先迁成一个独立 `legacy: true` group,并建立指向
  原 memory、保留原 evidence_role 的 relation;不在迁移时猜哪些
  source 属于同一问答;
- assistant 消息可机械标为 context;user 消息只有在原文本身能
  独立表达事实时才可标 assertion。含省略、指代或语义不清的 user
  source 保持 `member_role = NULL` 的 legacy 状态,等待重审;
- 满足全部新不变量并经重审的 group 才去除 legacy 标记;重审可以
  合并/拆分 group、改判角色、补充证据或退役错误关系;
- legacy 组允许暂时豁免新不变量,但不能被新 proposal 引用为已经
  验证的证据。新写入一律完整校验,legacy 只减不增。

## 考虑过并否决的方案

- **allow assistant 消息作 assertion**:放弃 ADR-0001 核心防线,
  模型幻觉可自我确证入库。否决。
- **把角色塞进现有 evidence_role 枚举**(初稿方案):两个正交
  维度混入一个枚举,表达力残缺且污染现有语义。否决。
- **只加角色字段、不建组表**(初稿方案):无组身份则组级约束
  无处安放,多组场景无法表达。否决。
- **把 memory_id / evidence_role 放在 group 上**:同一份用户原话
  无法同时作用于旧、新两条 memory,只能复制证据。否决,改为
  group-memory 多对多 relation。
- **问答拼接为合成消息作证据**:破坏连续子串规则,合成物无真实
  message_id,审计链断裂。否决。
- **存量机械迁移为单成员 assertion**(初稿方案):与生产数据
  冲突。否决,改 legacy 分级。
- **按 memory 或 evidence_role 自动合并存量 sources**:旧数据没有
  group 身份,机械合并会发明证据关系。否决,先逐 source 迁为 legacy。
- **组不变量写成 DB CHECK**:SQLite 能力之外,强行实现会退化成
  触发器魔法,可读性与可移植性都差。否决,应用层校验。

## 后果

正面:

- 确认流程与省略/指代类消息获得合规入库路径;
- "assistant 不能单独确立事实"从 prompt 约定升级为 apply 前的
  确定性拦截;
- 双维角色让"一条 assertion 同时 contradicts 旧记忆"这类真实
  场景可以精确表达;
- 组身份使一条记忆的多次独立佐证(多个问答组)结构清晰、可
  逐组审计;
- 历史 revision 与当前投影分离,偏好、身份等可变事实可以反复
  演变,而不抹掉过去真实存在过的状态。

代价与已知限制:

- schema 迁移涉及三张新表 + 存量数据分级重审,人工重审队列的
  消化速度受 ADR-0001 已知的人肉环节限制;
- parser / repository / 校验器三处同步演进,迁移顺序必须以
  curator.py 枚举为先(ADR-0001 有三份拷贝失配全批回滚的前科);
- 组级校验只存在于共享 mutation 边界,绕过该边界直写 DB 的路径
  不受保护——需靠"写路径唯一"纪律兜底,这是接受的信任边界;
- 最少语境判断与 reply 强信号带有平台适配成本,换平台时需重做
  message relation 映射,但不能放松 assertion 不变量。

## 后续工作(不改变本决策)

- 实施工作拆分:双枚举定义、三张新表迁移脚本、共享写入边界的
  组校验器、`attach_evidence` mutation、schema-枚举一致性测试
  (断言 DB CHECK 与 curator.py 枚举一致,含现存 evidence_role
  的缺口)、存量 legacy 分级与重审队列、curator prompt 更新
  (教会模型输出组与多对多 relation);
- 确认流程的绑定协议(candidate_id / question_message_id /
  reply 关联等)在 ADR-0004 中定义。
