# 统一 AI Audit + Memory Curator 实施计划

> 状态：设计计划，尚未实施。
>
> **2026-07-17 更新**：记忆部分被「记忆系统 v4」（Linear LT-133 epic）取代——
> curator 的确定性校验、evidence、cursor 幂等思想已继承进 v4 的异步入库
> worker（LT-136）；第 7 章 markdown apply pipeline 及 §5.7/5.8 中围绕
> memory.md 文档 hash 的设计**作废**（长期记忆改回 SQLite 表存储）。
> chat trace 数据库化（Phase 1-3/6、§5.1-5.6）仍是有效设计，作为独立
> track 后置，未拍板实施时间。
>
> 目标：把一次 AI 行为从输入、上下文、Prompt、模型轮次、工具调用、消息发送，
> 一直到异步 curator 修改长期记忆的全过程，保存为可查询、可关联、可回放的结构化审计链。

## LT-136 首版人工闸门（当前实现）

首版只提供手动命令，不接入 `main.py` 或 scheduler，因此部署后不会自动运行：

```bash
# 生成并校验 proposal；只写 trigger=curator 的审计，不改长期记忆/cursor
.venv/bin/python scripts/run_memory_curator.py \
  --limit 200 --output data/curator_proposals/review.json

# 人工审阅后，精确消费同一份 proposal；不会再次调用模型
.venv/bin/python scripts/run_memory_curator.py \
  --apply-proposal data/curator_proposals/review.json
```

`apply` 会重新校验证据频道、冻结消息区间、quote 原文、目标 memory 状态，
并要求 proposal operations 与对应 `ai_runs.final_text` 完全一致。全部 memory 变更和
cursor 推进在同一个 SQLite `BEGIN IMMEDIATE` 事务中完成；任一操作失败则整批回滚。

## 1. 背景与问题

项目目前已经存在两套审计数据：

1. `data/ai_traces/<date>.jsonl`
   - 保存完整 Prompt、history、每轮模型输出、tool call/result 和最终文本；
   - Trace Viewer 目前直接读取这些文件；
   - 当前约 69 个文件、61 MB，继续加入 curator 日志后会越来越难检索和关联。
2. SQLite `ai_runs` / `tool_calls`
   - 每次 AI 调用和工具调用已经结构化落库；
   - 但只保存摘要，没有 Prompt、memory snapshot、每轮响应和 Discord delivery；
   - 无法独立回答“模型当时看见了什么，以及用户最终收到了什么”。

长期记忆正在迁移为 Markdown canonical source。未来计划由独立 curator model 异步整理，
聊天 bot 主要读取记忆，不负责在对话中自由维护长期记忆。

因此需要一套统一审计模型，把两条链连起来：

```text
用户消息
  -> 读取 memory / structured state / history
  -> 组装 Prompt
  -> 模型 rounds
  -> tool calls
  -> Discord delivery
  -> conversation_messages
  -> curator 读取新增消息
  -> curator 提议 memory changes
  -> 校验并修改 memory.md
```

## 2. 目标

系统应能结构化回答以下问题：

- 某次回复读取了哪些长期记忆？哪些因为 token budget 被排除？
- 当时完整 Prompt、最近消息、相关历史、deadline/reminder 状态是什么？
- 模型经历了几轮响应，调用了哪些工具，每次参数和结果是什么？
- 模型生成的文字是否真的发送成功？是否被 `[SILENT]` 抑制或被拆成多条 Discord 消息？
- curator 处理了哪一段对话、读取了哪个版本的 `memory.md`？
- curator 为什么新增、更新、合并、归档或删除某条记忆？证据来自哪些原始消息？
- 某条记忆经历过哪些版本，是否能恢复到修改前状态？
- 进程中途崩溃时，一次 memory change 到底已经写入文件，还是仍未执行？

## 3. 非目标

本计划不做：

- 保存或推断 provider 未返回的隐藏 chain-of-thought；
- 用 audit 数据替代 deadline、todo、reminder、timeline 等业务权威数据；
- 让 curator 无约束地直接重写 `memory.md`；
- 一开始就让 curator 自动修改所有记忆；
- 为了写审计日志而阻塞或中断普通聊天回复；
- 在首期引入外部日志平台、消息队列或独立数据库服务。

## 4. 核心决策

### 4.1 SQLite 是权威审计存储

结构化审计以现有 SQLite 为 source of truth：

- 可按时间、run、tool、memory、message 和状态查询；
- 与 `conversation_messages`、deadline 等本地数据直接关联；
- 已被 Litestream 复制到 R2；
- 单用户规模不需要 Elasticsearch / ClickHouse / PostgreSQL。

JSONL 保留，但降级为：

- SQLite 写入异常时的 best-effort 原始副本；
- 调试和离线导出格式；
- schema 迁移前的历史数据来源；
- 短期轮转文件，而不是 Dashboard 的长期查询后端。

### 4.2 规范化核心字段，复杂 payload 保留 JSON

需要过滤、关联、排序的字段单独建列，例如：

- `run_type`、`status`、`round_n`、`tool_name`；
- `memory_id`、`operation`、`confidence`；
- `conversation_message_id`、`delivery_status`。

Provider-specific payload、usage、上下文配置等不稳定结构保存在 JSON 列。

### 4.3 大文本按 hash 去重

完整 Prompt、memory document、raw output 等大文本不在每行重复保存。
统一写入 `audit_artifacts`：

```text
sha256(content) -> audit_artifacts.id
```

run/context/memory revision 只引用 artifact id。相同 memory 版本和相同 cached Prompt 前缀只存一份。

### 4.4 两种可靠性等级

普通聊天审计是 best-effort：

- 审计写入失败要记录 error/metric；
- 不能因为 audit DB 临时失败而阻止 bot 回复用户。

记忆修改审计是 mandatory：

- curator 无法建立 `proposed` change 时，不允许修改 `memory.md`；
- 无 evidence、before hash 不匹配或校验失败时，不允许 apply；
- 每次人工、API、脚本和 curator 修改都必须经过同一个 apply pipeline。

## 5. 统一数据模型

### 5.1 `audit_runs`

所有 AI 调用和后台审计任务的根节点。由现有 `ai_runs` 演进而来。

```sql
CREATE TABLE audit_runs (
    id TEXT PRIMARY KEY,
    run_type TEXT NOT NULL,       -- chat/check_in/reminder/curator/oneshot
    trigger TEXT,
    parent_run_id TEXT REFERENCES audit_runs(id),
    trigger_message_id INTEGER REFERENCES conversation_messages(id),
    model TEXT,
    provider TEXT,
    status TEXT NOT NULL,         -- running/success/failed/recovered
    started_at TEXT NOT NULL,
    finished_at TEXT,
    error_type TEXT,
    error_message TEXT,
    metadata_json TEXT
);
```

`parent_run_id` 用于表达：

- fallback provider run 属于哪个原 run；
- curator run 由哪个 scheduler/check-in run 启动；
- retry/recovery 与原失败 run 的关系。

### 5.2 `audit_artifacts`

内容寻址的大文本存储。

```sql
CREATE TABLE audit_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256 TEXT NOT NULL,
    artifact_type TEXT NOT NULL,  -- prompt/memory_document/raw_output/history
    content TEXT NOT NULL,
    char_count INTEGER NOT NULL,
    token_estimate INTEGER,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(sha256, artifact_type)
);
```

第一阶段直接存 TEXT。只有真实体积或查询性能出现问题后，才考虑压缩 BLOB。

### 5.3 `context_snapshots`

记录一次 run 实际读取并注入模型的上下文。

```sql
CREATE TABLE context_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES audit_runs(id),
    prompt_artifact_id INTEGER REFERENCES audit_artifacts(id),
    memory_artifact_id INTEGER REFERENCES audit_artifacts(id),
    history_artifact_id INTEGER REFERENCES audit_artifacts(id),
    memory_document_hash TEXT,
    included_memory_ids_json TEXT,
    omitted_memory_ids_json TEXT,
    relevant_history_ids_json TEXT,
    structured_context_json TEXT,
    prompt_values_json TEXT,
    context_config_json TEXT,
    token_estimate INTEGER,
    created_at TEXT DEFAULT (datetime('now'))
);
```

`structured_context_json` 保存当时注入的：

- active deadlines；
- pending reminders；
- today timeline；
- projects；
- calendar/weather；
- 其他动态上下文。

这是 audit snapshot，不是这些业务对象的新权威副本。

### 5.4 `model_rounds`

每次 provider round 一行。

```sql
CREATE TABLE model_rounds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES audit_runs(id),
    round_n INTEGER NOT NULL,
    raw_output_artifact_id INTEGER REFERENCES audit_artifacts(id),
    visible_text TEXT,
    reasoning_summary TEXT,
    usage_json TEXT,
    stop_reason TEXT,
    started_at TEXT,
    finished_at TEXT,
    UNIQUE(run_id, round_n)
);
```

`reasoning_summary` 只保存 provider 明确返回或应用主动要求模型生成的摘要。
不保存、猜测或重构隐藏 chain-of-thought。

### 5.5 `tool_calls`

扩展现有表，而不是另建第二套工具日志。

```sql
CREATE TABLE tool_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES audit_runs(id),
    round_id INTEGER REFERENCES model_rounds(id),
    round_n INTEGER,
    provider_call_id TEXT,
    tool_name TEXT NOT NULL,
    arguments_json TEXT,
    result_json TEXT,
    success INTEGER,
    error TEXT,
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
```

### 5.6 `message_deliveries`

单独记录“实际尝试发送给用户”的动作。

```sql
CREATE TABLE message_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT REFERENCES audit_runs(id),
    conversation_message_id INTEGER REFERENCES conversation_messages(id),
    channel_id TEXT,
    delivery_index INTEGER,
    content TEXT,
    discord_message_id TEXT,
    status TEXT NOT NULL,       -- attempted/sent/failed/suppressed
    suppression_reason TEXT,    -- silent/empty/deduplicated
    error TEXT,
    attempted_at TEXT,
    sent_at TEXT
);
```

这张表区分：

- 模型生成成功但发送失败；
- `[SILENT]` 被主动吞掉；
- 2000 字限制导致的一次回复多条 delivery；
- tool round 中间文本和最终文本分别发送；
- Discord 返回 message id 后是否成功写入 conversation log。

### 5.7 `memory_revisions`

保存每次成功 apply 后的完整 memory document 版本。

```sql
CREATE TABLE memory_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_hash TEXT NOT NULL UNIQUE,
    artifact_id INTEGER NOT NULL REFERENCES audit_artifacts(id),
    source_run_id TEXT REFERENCES audit_runs(id),
    previous_revision_id INTEGER REFERENCES memory_revisions(id),
    token_estimate INTEGER,
    created_at TEXT DEFAULT (datetime('now'))
);
```

Markdown 仍是运行时 canonical source；revision 表提供历史、diff 和恢复依据。

### 5.8 `memory_changes`

每个逻辑 memory operation 一行。

```sql
CREATE TABLE memory_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES audit_runs(id),
    operation TEXT NOT NULL,       -- add/update/delete/archive/merge/split
    memory_id INTEGER,
    target_memory_ids_json TEXT,
    before_json TEXT,
    after_json TEXT,
    reason TEXT NOT NULL,
    confidence REAL,
    status TEXT NOT NULL,          -- proposed/validated/applied/rejected/failed
    rejection_reason TEXT,
    before_document_hash TEXT NOT NULL,
    after_document_hash TEXT,
    proposed_at TEXT NOT NULL,
    applied_at TEXT
);
```

`merge` 可用 `target_memory_ids_json` 记录被合并的多个旧 memory id；
`memory_id` 表示合并后保留的主条目。

### 5.9 `memory_change_evidence`

把变更与原始证据连接起来。

```sql
CREATE TABLE memory_change_evidence (
    change_id INTEGER NOT NULL REFERENCES memory_changes(id),
    conversation_message_id INTEGER NOT NULL REFERENCES conversation_messages(id),
    quote TEXT,
    evidence_role TEXT,            -- supports/contradicts/supersedes
    PRIMARY KEY(change_id, conversation_message_id, evidence_role)
);
```

### 5.10 `curator_cursors`

记录 curator 已处理范围，保证幂等和可续跑。

```sql
CREATE TABLE curator_cursors (
    curator_name TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    last_message_id INTEGER,
    last_successful_run_id TEXT REFERENCES audit_runs(id),
    updated_at TEXT DEFAULT (datetime('now'))
);
```

cursor 只在整个 curator run 成功结束后推进。

## 6. 普通聊天的审计时序

### 6.1 用户消息进入

1. 写入 `conversation_messages`，拿到稳定 message id；
2. 建立 `audit_runs(status=running, run_type=chat)`；
3. `trigger_message_id` 指向当前用户消息。

### 6.2 Context Builder

1. 读取当前 `memory.md` 并计算 hash；
2. 记录全部 active memory id；
3. 按 token budget 计算 included / omitted memory ids；
4. 读取 recent messages 和 embedding relevant history；
5. 读取 deadline/reminder/timeline/calendar 等结构化上下文；
6. 完成 Prompt render；
7. 大文本写入/复用 `audit_artifacts`；
8. 写入一条 `context_snapshots`。

必须保存“实际送进 provider 的 Prompt”，不能只保存模板或理论输入。

### 6.3 模型和工具轮次

每轮 provider response：

1. 写 `model_rounds`；
2. 每个 tool call 写 `tool_calls(started_at)`；
3. 工具结束后补 result/success/finished_at；
4. fallback/retry 通过新的 child run 表达，不覆盖原失败记录。

### 6.4 Discord 发送

每个发送 chunk：

1. 发送前写/缓存 `attempted`；
2. 成功后补 Discord message id 和 `sent`；
3. 发送失败写 `failed + error`；
4. `[SILENT]`、空文本写 `suppressed`；
5. 成功发送的消息继续写入 `conversation_messages` 并建立关联。

### 6.5 Run 完成

更新 `audit_runs.status/finished_at/error`。JSONL 同时写一份完整 export，作为短期 fallback。

## 7. Curator 审计与修改协议

### 7.1 Curator 输入

每次 curator run 读取：

- cursor 之后的新 `conversation_messages`；
- 这些消息关联的 chat audit runs、tool calls 和 deliveries；
- 当前 `memory.md` 和 hash；
- 当前 active memory 条目；
- 可选的结构化状态摘要，但不把 memory 当 deadline 权威来源。

输入范围必须写入 run metadata：

```json
{
  "channel_id": "...",
  "from_message_id": 1820,
  "to_message_id": 1874,
  "before_memory_hash": "...",
  "curator_mode": "daily"
}
```

### 7.2 Curator 输出

模型不能直接返回整篇任意 Markdown。它只返回严格 operations：

```json
{
  "operations": [
    {
      "action": "update",
      "memory_id": 141,
      "content": "PTE trial 已参加，不再需要准备。",
      "reason": "用户明确表示已经参加",
      "confidence": 0.96,
      "evidence_message_ids": [1861]
    }
  ]
}
```

### 7.3 确定性校验

应用代码必须检查：

- operation 和字段是否符合 schema；
- memory id 是否存在；
- evidence message 是否属于本 run 可见范围；
- quote 是否能在原消息中找到或明确标为摘要；
- before document hash 是否仍等于当前文件；
- 更新后 Markdown 能否解析；
- memory id 是否唯一；
- token budget 是否满足，或是否需要 archive/compact；
- curator 是否越权修改 deadline/todo/reminder/timeline。

### 7.4 Proposed-first 协议

每次修改按以下状态机执行：

```text
proposed
  -> validated
  -> atomic file replace
  -> revision persisted
  -> applied
```

具体顺序：

1. 建立 `memory_changes(status=proposed)` 和 evidence；
2. 确定性校验，通过后标记 `validated`；
3. 对 `memory.md` 加锁，再次校验 before hash；
4. 在内存中应用全部 operations；
5. 写临时文件并 `fsync + os.replace`；
6. 计算 after hash，写 `memory_revisions`；
7. 把 changes 标记为 `applied`；
8. 更新 curator cursor；
9. 释放文件锁。

任何一步失败：

- change 标记 `failed` 或 `rejected`；
- 不推进 cursor；
- 不允许返回“已经记住”；
- 保留 error 和当时 before/after hash。

### 7.5 崩溃恢复

启动时扫描长期处于 `validated` 的 change：

- 文件 hash == before hash：文件还没改，标记 failed，可重试；
- 文件 hash == after hash：文件已改但 DB 未完成，补 revision 并标记 applied；
- 两者都不等：标记 conflict，禁止自动继续，等待人工检查。

## 8. 人工和 API 修改也必须审计

以下入口不得直接写 Markdown：

- curator；
- Dashboard Memory editor；
- API `PUT/PATCH/DELETE memory`；
- migration/maintenance scripts；
- 未来可能恢复的 bot memory tools。

统一调用：

```python
MemoryService.apply_operations(
    actor_type="curator|user|migration|bot",
    source_run_id=...,
    operations=[...],
)
```

人工修改可把 `confidence` 留空，reason 使用 `manual edit`，仍要保存 before/after 和 revision。

## 9. API 与 Dashboard

### 9.1 API

新增结构化查询：

```text
GET /api/audit/runs
GET /api/audit/runs/{run_id}
GET /api/audit/runs/{run_id}/timeline
GET /api/audit/memory-changes
GET /api/audit/memories/{memory_id}/history
GET /api/audit/artifacts/{artifact_id}
POST /api/audit/memory-changes/{id}/approve
POST /api/audit/memory-changes/{id}/reject
```

列表 API 默认不返回大文本 artifact；展开单个 run 时按需加载。

### 9.2 Audit Timeline UI

一个 run 按因果顺序展示：

```text
Trigger
  用户消息 / scheduler 指令

Context
  memory hash
  included memory ids
  omitted memory ids
  relevant history
  structured state
  rendered Prompt

Model
  round 1
  tool call -> result
  round 2

Delivery
  attempted -> sent / failed / suppressed

Downstream
  此消息后来被哪个 curator run 处理
  导致了哪些 memory changes
```

### 9.3 Memory History UI

按 memory id 展示：

- 当前内容；
- 所有 revision/diff；
- add/update/merge/archive/delete 操作；
- curator reason/confidence；
- evidence 原消息；
- approve/reject 状态；
- 恢复某个 revision 的入口。

### 9.4 隐私与访问控制

Audit UI 会包含完整私人对话、长期记忆和 Prompt，敏感度高于普通 dashboard。

上线前必须满足：

- 服务只绑定允许的本地/WireGuard 地址，或增加 admin authentication；
- API 不在日志中打印完整 artifact；
- 默认列表隐藏大文本和敏感内容，只显示摘要；
- 导出和下载操作有明确权限边界；
- 不把 audit 数据发送到新的外部服务。

## 10. JSONL 保留与轮转

数据库化后 JSONL 策略：

- 新 run 继续 best-effort 写 JSONL；
- 只保留最近 14 天未压缩文件；
- 更旧文件 gzip 或确认 DB + R2 备份后删除；
- 增加按 `run_id` 从 DB 导出完整 JSON 的命令；
- Dashboard 不再扫描 JSONL；
- 迁移完成前保留当前 69 个文件，不立即删除。

需要注意：Litestream 复制 SQLite，但不会复制独立 JSONL。数据库化后反而能让核心审计进入现有 R2 灾备链。

## 11. 实施阶段

### Phase 0：冻结 schema 与基线

- [ ] 为现有 JSONL schema 和 DB schema 写 fixture；
- [ ] 记录当前 trace 数量、体积和典型 run；
- [ ] 明确哪些 provider 字段必须保留；
- [ ] 为隐私/API access 做上线决策。

### Phase 1：完整 Chat Trace 数据库化

- [ ] 建 `audit_runs`、`audit_artifacts`、`context_snapshots`、`model_rounds`；
- [ ] 扩展 `tool_calls`；
- [ ] `trace.start/add_round/finalize` 同时写完整结构化数据；
- [ ] 验证 DB 与 JSONL 对同一 run 的语义一致；
- [ ] 保持 JSONL Viewer 不变作为回滚路径。

### Phase 2：Delivery 审计

- [ ] 建 `message_deliveries`；
- [ ] 覆盖普通 reply、主动消息、reminder、fallback error；
- [ ] 覆盖 `[SILENT]`、分段发送、Discord 失败；
- [ ] 关联 outbound `conversation_messages`。

### Phase 3：历史 JSONL 导入

- [ ] 写幂等 importer，按 trace id 去重；
- [ ] 导入现有 JSONL；
- [ ] 输出成功/跳过/损坏统计；
- [ ] 随机抽样对比 Trace Viewer；
- [ ] 不因一条坏 JSON 中断整个导入。

### Phase 4：Curator propose-only

- [ ] 建 `memory_revisions`、`memory_changes`、evidence、cursor；
- [ ] curator 读取新消息并输出严格 operations；
- [ ] 只保存 `proposed/rejected`，暂不修改 Markdown；
- [ ] Dashboard 展示 proposal、reason 和 evidence；
- [ ] 运行至少一周，统计 precision 和人工接受率。

### Phase 5：受控 Apply

- [ ] 实现 `MemoryService.apply_operations()`；
- [ ] 文件锁、before hash、atomic replace；
- [ ] revision 与 change 状态机；
- [ ] 崩溃恢复和 conflict 处理；
- [ ] 先只允许人工 approve；
- [ ] 稳定后再允许高置信度自动 apply。

### Phase 6：Audit Dashboard 切换数据库

- [ ] run 列表和过滤从 SQLite 查询；
- [ ] run timeline 关联 context/round/tool/delivery/change；
- [ ] memory history/diff 页面；
- [ ] artifact 按需加载；
- [ ] JSONL Viewer 保留一个发布周期后移除主入口。

### Phase 7：Retention 与灾备

- [ ] JSONL 14 天轮转或 gzip；
- [ ] 验证 audit tables 被 Litestream 恢复；
- [ ] 验证 memory.md 独立备份；
- [ ] 完成一次 DB + Markdown 联合恢复演练；
- [ ] 制定 artifact 保留周期和手动清理命令。

## 12. 验收标准

### 12.1 一次 Chat Run 可完整回放

给定一个 Discord user message id，可以查到：

- 唯一 chat run；
- 当时读取的 memory document hash；
- included/omitted memories；
- relevant history 和 structured context；
- provider 实际 Prompt；
- 全部 model rounds；
- 全部 tool call/result；
- 最终 delivery 状态和 Discord message id。

### 12.2 一次 Curator Run 可完整解释

给定一个 curator run id，可以查到：

- 输入消息范围和 cursor；
- before memory revision/hash；
- curator Prompt 和输出；
- 每个 proposed operation；
- reason/confidence/evidence；
- validation/rejection 原因；
- after revision/hash；
- cursor 是否推进。

### 12.3 一条 Memory 可完整追溯

给定 memory id，可以看到从创建到当前的全部变更，以及每次变更的 run 和 evidence。

### 12.4 一致性

- 不存在 applied change 但缺少 after revision；
- 不存在 cursor 已推进但 run 未 success；
- 不存在 sent delivery 但既无 Discord id 也无明确兼容原因；
- 同一 run/round/tool provider call 不重复入库；
- 同一 artifact 内容只保存一次；
- audit 写入失败不会阻断聊天；
- mandatory audit 写入失败会阻止 memory mutation。

### 12.5 性能

- run 列表不读取大 artifact；
- 单 run 展开只查询该 run 关联数据；
- 普通聊天审计不新增外部网络调用；
- artifact hash 去重后，重复 Prompt/memory 不重复占用大文本空间；
- curator 在后台运行，不阻塞 Discord heartbeat 和用户聊天。

## 13. 测试计划

### 单元测试

- artifact hash 去重；
- context snapshot included/omitted ids；
- tool call 与 round 关联；
- delivery 状态机；
- curator operation schema；
- evidence 校验；
- before hash conflict；
- revision 链；
- cursor 幂等推进；
- crash recovery 三种 hash 分支。

### 集成测试

- chat -> Prompt -> tool -> delivery -> DB audit；
- fallback provider 保留父子 run；
- `[SILENT]` 产生 suppressed delivery；
- curator propose-only 不修改文件；
- approve -> atomic Markdown update -> revision -> applied；
- DB 写失败时聊天仍可回复；
- DB 写失败时 memory mutation 被拒绝。

### 迁移测试

- JSONL importer 可重复运行；
- 旧 schema trace 兼容；
- 损坏行隔离；
- 导入数量和日期统计；
- 随机抽样 JSONL 与 DB 展示一致。

### 恢复演练

- 从 Litestream 恢复 audit DB；
- 从独立备份恢复 `memory.md`；
- 校验 memory revision hash；
- 处理一个停在 `validated` 的 change；
- 验证 UI 仍能展示恢复前的因果链。

## 14. 预期代码改动

主要文件：

- `bot/database.py`：schema、audit repository、查询方法；
- `bot/trace.py`：完整结构化持久化、artifact 去重；
- `bot/memory/service.py`：统一 `apply_operations()`；
- `bot/memory/curator.py`：异步 curator pipeline；
- `bot/memory/models.py`：operation/context/revision 类型；
- `bot/ai_engine_base.py`：run/context audit 生命周期；
- 各 `bot/ai_engine_*.py`：provider round/tool timing；
- `bot/discord_bot.py`：delivery audit；
- `bot/scheduler.py`：curator 调度和 cursor；
- `api/server.py`：audit 查询、proposal approve/reject；
- `frontend/src/app/components/TraceViewer.tsx`：DB run timeline；
- 新增 Memory History / Curator Review UI；
- `scripts/import_trace_jsonl.py`：历史导入；
- `scripts/export_audit_run.py`：单 run 导出；
- `docs/database.md`：最终 schema 文档；
- `docs/deploy.md`：备份、轮转和恢复流程。

## 15. 风险与防护

### Audit DB 体积增长

防护：artifact hash 去重、列表不取正文、JSONL 轮转、后续按真实数据决定压缩。

### 敏感数据泄露

防护：Audit API 权限、本地/WireGuard 边界、默认折叠正文、不把 artifact 打进普通日志。

### Curator 误改记忆

防护：strict operations、evidence、before hash、propose-only 起步、人工 approve、revision 可恢复。

### 文件与数据库双写不原子

防护：proposed-first 状态机、atomic replace、before/after hash、启动恢复流程。

### 审计影响聊天稳定性

防护：普通 chat audit best-effort；memory mutation audit mandatory；二者错误策略明确分离。

### 记录了“看起来像 reasoning”的不可靠内容

防护：只保存 provider 实际返回内容和显式 reasoning summary，不推断隐藏思维过程。

## 16. 最终完成定义

只有同时满足以下条件，才能认为统一 Audit + Curator 完成：

- Trace Dashboard 已不依赖 JSONL 扫描；
- chat/check-in/reminder/curator 都使用统一 run id 模型；
- context、round、tool、delivery、memory change 可以在 UI 串成一条时间线；
- 每次 memory 修改都有 before/after、reason、actor、run 和 evidence；
- curator 支持 propose-only、人工 approve、冲突拒绝和崩溃恢复；
- 历史 JSONL 已幂等导入并完成抽样校验；
- DB 与 Markdown 联合恢复演练通过；
- JSONL 已降级为短期 fallback/export，而非权威查询层。
