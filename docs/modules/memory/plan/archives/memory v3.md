# Memory & Audit 实施清单 v3（收窄版，可直接开工）

## 0. 这份文档是什么

`docs/memory.md`（v1：滚动摘要 + 分层压缩 + 每日反思）和 `docs/memory v2.md`
（v2：Conversation Log / State Ledger / Living Memory / Audit 四层架构）都还在仓库里，
方向互相矛盾。v2 对 v1 的否定是对的——bot 的核心价值是准确的 deadline/timeline
追踪，不是"无限聊天记忆"。但 v2 的实现方案本身又过度设计了：10 张新表 +
extractor/curator/auditor 三条自动化流水线，对单人单频道场景来说是给多用户产品
准备的治理机制。

v3 = 保留 v2 的四层思想，把实现砍到一个人周末就能上线的规模。经过讨论后，
Part B 从"轻量升级 memories 表"演化成"两层记忆"：小而精的永久事实表 +
对全部对话历史做自动 embedding 检索，理由见 §3。

**处理建议**：v1 直接作废可删；v2 作为"四层怎么分"的架构参考可以留着，
但机制层面以本文件为准。两个文件都是未跟踪的新文件，我不会自己删，
你确认方向没问题后告诉我要不要删。

对应 Linear：[LT-125](https://linear.app/chachas/issue/LT-125)，分支 `feat/LT-125-memory-v3`。

---

## 实施进度（持续更新）

- ✅ **Part A（可追溯性）**：`ai_runs`/`tool_calls` 两张表 + `trace.py` 落库逻辑已实现，
  写了端到端脚本验证（trace 生命周期 → 建 run → 记 tool_calls）跑通。
- ✅ **Part B1（memories 收窄）**：`memory_type`/`valid_until` 字段、去掉 FIFO 20 条硬删、
  `get_all_memories`/`add_memory`/`update_memory` 都已改造，`save_memory`/`update_memory`
  工具 schema 和 description 也同步更新（明确告诉 AI 这张表只存永久事实，日常进展不用它存）。
  Python 脚本验证过期过滤 + 部分更新 + 清空 valid_until 三个路径都正确。
- ✅ **Part D（Memory 管理 UI）**：`api/server.py` 加了 `PATCH /api/memories/{id}`；
  `frontend/src/app/App.tsx` 的 `MemoryPage` 加了 hover 编辑/删除按钮 + 编辑弹窗
  （改 content/memory_type/valid_until，可以清空 valid_until 改回永久）。
  后端 API 用真实 HTTP 请求（起了一个指向隔离测试库的 FastAPI 实例，curl 全流程）验证过。
  前端页面因为这台机器上装 Playwright/chromium 需要 `sudo apt-get` 装系统依赖，
  没有擅自执行，所以**这部分还没有过真实浏览器点击验证**，需要你自己在界面里点一下
  编辑/删除确认交互顺手。
- ✅ **Part B2（对话日志自动 embedding 检索）**：已实现并用真实数据验证。
  与 §3 原方案的几处偏差（都是实测后的修正，不是设计漂移）：
  - **Provider 选了智谱 embedding-3（1024 维）而不是 OpenAI**：config.json 里
    `glm` preset 的智谱 key 可以直接复用；中文对话场景智谱效果好；从悉尼 VPS
    实测延迟 ~520-700ms（写入路径异步无感，检索路径一次调用可接受）。实现是
    OpenAI 兼容的 `ai.embedding` 配置块（`config.json`），想换 OpenAI 官方或本地
    Ollama 只改 config 不改代码。本地架设（Ollama + bge-m3）评估过：24GB 内存
    跑得动，但要多维护一个常驻服务，和本文件"砍到最小"的精神冲突，先不做。
  - **多存一列 `embedding_model`**：检索只比对同模型的行，将来换 embedding
    模型时旧向量自然失效、后台任务逐渐用新模型补齐，不会新旧向量混算。
  - **打分公式校准**（对智谱 embedding-3，用 960 条真实对话实测）：
    `score = cosine + 0.1 × 0.995^距今小时`，`min_relevance = 0.55`。
    该模型下任意无关中文配对 cosine 就有 0.45~0.52，相关内容 0.65+；
    recency 权重实测 0.25 会让"最近但一般相关"压过"三周前但高度相关"，降到 0.1。
  - **同段对话去重保留 id 最大的命中行**（不是分最高的）：embedding_context
    向前拼接，id 大的行覆盖整段内容，段落开头那条反而看不到后文。
  - **加了 `scripts/backfill_embeddings.py`**：给存量消息一次性回填 embedding
    （幂等可续传，WAL 模式下 bot 在线也能跑）。已在 prod 库副本上全量跑通
    （960 行零失败，约 100 秒）；**部署后需要对 prod 库正式跑一次**，否则检索
    对部署前的历史失明。
  - 端到端验证脚本全部通过：写入→异步 embedding 落库→跨话题检索命中→
    短消息（"弄好了"）带上下文编码→坏 key/禁用时静默降级不影响聊天。
    真实数据抽查："ECON5111 seminar 什么时候"精准命中原提醒消息，
    "量子色动力学"零命中（噪音被阈值挡住）。

---

## 1. 范围

做：
- **Part A** — AI 行为可追溯性（run / tool_call 记账）
- **Part B1** — `memories` 表收窄为"永久事实"，去掉 20 条 FIFO 硬删
- **Part B2** — 对话日志（`conversation_messages`）自动 embedding + 检索，
  解决"AI 忘了主动存"这整类问题
- **Part C**（可选，优先级最低）— 模糊时间提示，规则触发，不建表

明确不做（原因见对应小节）：
- v1 整个方案：滚动摘要、分层压缩、每日反思
- Redis、向量索引/向量库（暴力 cosine 扫描在当前数据量下够用）
- `memory_candidates` + weekly curator + monthly decay 状态机
- daily auditor（AI 审计 AI）+ `audit_findings` + `behavior_adjustments`
- 独立的 `assistant_messages` 表（并入 `conversation_messages` 即可）

---

## 2. Part A：可追溯性 —— 复用 `trace.py` 现有的生命周期

### 关键发现

`bot/trace.py` 已经有一套完整的 run 生命周期：`start()`（trace.py:99）→
`add_round()`（trace.py:131，每轮工具调用/结果都在里面）→ `finalize()`
（trace.py:157）。三个调用点在 `bot/ai_engine_base.py`：`simple_completion`
(:287)、`chat` (:327)、`scheduled_action` (:392)。目前 `finalize()` 只把
entry 写进 `data/ai_traces/<date>.jsonl`（`_write`，trace.py:168），
JSONL 里其实已经有 `ai_runs` + `tool_calls` 需要的全部数据，只是格式不可查询。

**所以不用重新埋点，只要让 `finalize()` 多做一步：把同一份 entry 也写一份结构化摘要进 SQLite。**

### 数据库改动（`bot/database.py`）

```sql
CREATE TABLE IF NOT EXISTS ai_runs (
    id TEXT PRIMARY KEY,        -- 复用 trace entry["id"]
    trigger TEXT NOT NULL,      -- chat / oneshot / scheduled / poll / reminder / bedtime ...
    model TEXT,
    provider TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT,                -- success / failed
    error TEXT,
    final_text TEXT
);

CREATE TABLE IF NOT EXISTS tool_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES ai_runs(id),
    round_n INTEGER,
    tool_name TEXT NOT NULL,
    arguments_json TEXT,
    result_json TEXT,
    success INTEGER,            -- 从 result.get("success") 推断，取不到就 NULL
    created_at TEXT
);
```

`state_changes` 先不建：deadline/timeline 的 before/after 其实可以从
`tool_calls.arguments_json` + `result_json` 反推出来。等真的需要"这个
deadline 的完整变更历史"这种查询、反推成本太高时再单独加表，现在加就是
重复记录同一份数据。

### 代码改动

1. `bot/trace.py`
   - `finalize()` 签名加 `db: Database | None = None`
   - 新增 `_persist_to_db(entry, db)`：写一行 `ai_runs`，遍历
     `entry["rounds"]` 里的 `tool_calls`/`tool_results` 各写一行
     `tool_calls`；内部包 try/except，沿用文件开头注释里
     "写失败永远不抛异常"的原则，`db=None` 时直接跳过
2. `bot/ai_engine_base.py`
   - 三处 `trace.finalize(...)` 调用（:295/:298、:341/:344、:430/:433）
     都补上 `db=db`——这三个函数本来就有 `db` 参数，顺手传即可

不改 `bot/ai_engine_claude.py` / `_openai.py` / `_gemini.py` / `_relay.py`：
`_execute_tool`（`ai_engine_base.py:127`）已经是四个引擎共用的唯一执行点，
`add_round` 也已经在这四个文件里各自调用了——数据在 `add_round` 传进来的
参数里已经齐了，不需要改动这四个引擎文件本身。

### 验收

- 触发一次带工具调用的对话（比如加一个 deadline），`tool_calls` 表里能查到
  这次调用的完整参数和结果
- `ai_runs` 表能查到这次 chat 的 trigger / model / 耗时 / 成功状态
- 某处调用点漏传 `db` 或传 `None`，bot 不崩，只是那次没落库

---

## 3. Part B：两层记忆 —— 永久事实 vs 自动检索的对话日志

### 为什么拆成两层

最初想法是给 `memories` 表加 recency/importance/relevance 打分检索
（Park et al. *Generative Agents* 的经典设计）。但讨论后发现真正的 bug
（"昨天说文件夹建好了，今天让你建文件夹"）根源不是"检索算法不够聪明"，
而是**那句话压根没被存进 `memories`**——只在最近 20 条对话窗口里，
隔天就滚出去了。再聪明的检索也捞不到从没存过的东西。

`conversation_messages` 表其实已经在无条件记录每一句话
（`discord_bot.py:151` 用户消息、`:261` assistant 消息），只是完全没被
用于检索。所以正确的分工是：

- **B1（永久事实）**：`memories` 表继续靠 AI 主动 `save_memory`，
  但用途收窄成"不会因为一阵子没提就该消失"的东西——长期偏好、身份信息
  （"喜欢简短回复""在读 xx 专业"）。这类事实**不应该做 recency 衰减**
  （六个月前存的偏好不该因为"最近没提"就检索不到），所以不需要打分机制，
  维持"全部展示"就够，只是不再 FIFO 硬删 20 条。
- **B2（对话日志自动检索）**：真正解决"日常进展/casual fact"的遗忘问题。
  每条已经存在 `conversation_messages` 里的消息顺手算一份 embedding，
  聊天时用当前消息去检索最相关的历史片段。**AI 不需要判断"这句话该不该记"**
  ——存是无条件的，聪明的部分只在检索这一步。

### B1：`memories` 表改动

```sql
ALTER TABLE memories ADD COLUMN memory_type TEXT;   -- 自由文本，不强制枚举
ALTER TABLE memories ADD COLUMN valid_until TEXT;   -- NULL = 永久
```

- `bot/database.py`：`add_memory` 去掉 20 条 FIFO 硬删逻辑
  （`database.py:847-857`）；`get_all_memories` 改成
  `WHERE valid_until IS NULL OR valid_until > datetime('now')`
- `bot/tools.py`：`save_memory`/`update_memory` schema 加
  `memory_type`、`valid_until` 两个可选参数，description 里说明这个表
  现在只用来存"长期不变的事实"，日常进展交给系统自动处理（见 B2），
  减少 AI 觉得"什么都要往这存"的负担

不需要 embedding / importance：这张表定位就是小、精、稳定，靠 AI 偶尔
显式调用维护，不需要打分排序。

### B2：对话日志自动 embedding + 检索

**写入路径**（后台异步，不阻塞发消息）：

1. `conversation_messages` 表加两列：
   ```sql
   ALTER TABLE conversation_messages ADD COLUMN embedding TEXT;         -- JSON 数组
   ALTER TABLE conversation_messages ADD COLUMN embedding_context TEXT; -- 实际拿去 embed 的拼接文本
   ```
2. 新模块 `bot/embeddings.py`：
   - `async def embed_text(text: str) -> list[float] | None`，用项目里
     已有的 `openai` 依赖（`AsyncOpenAI` + `text-embedding-3-small`，
     config.json 里已经配了 openai 的 preset/key，不用装新库、不用新密钥）；
     失败返回 `None`，不抛异常
   - 纯 Python 余弦相似度 helper（数据量小，不需要 numpy）
3. **关键点——不要 embed 孤立的一句话**：只 embed 单条短消息会丢上下文
   （比如"弄好了"三个字，embedding 完全捕捉不到"作业文件夹"这个主题，
   之后怎么查都查不到）。所以 embedding 前先拼接同一 channel 最近
   1-2 轮作为上下文，再整体 embed；`embedding_context` 存这个拼接后的
   文本，方便调试，也方便检索命中后原样展示给 AI 看。
4. 挂载点：`bot/discord_bot.py` 里 `add_conversation_message` 调用之后
   （用户消息 :151、assistant 消息 :261/:764），fire-and-forget 起一个
   asyncio task 算 embedding 再 `UPDATE` 回该行——失败/超时不影响正常
   发消息流程。

**检索路径**：

- `bot/ai_engine_base.py` 的 `chat()`（:302），取当前用户消息，
  `await embed_text(...)` 得到 query embedding（这是本方案里唯一一次
  同步等待的 embedding 调用，几十到一百多毫秒，可接受）
- 新增 `db.get_relevant_conversation_snippets(query_embedding, channel_id, limit=5)`：
  扫 `embedding IS NOT NULL` 的行，`score = recency_decay(距今小时数) + relevance(cosine)`
  （两项，不需要 importance——原始日志没有精选，重要性用相关度体现），
  取 top-5；**排除掉已经在当前工作窗口内的消息**（避免 AI 看到重复内容），
  用 `created_at` 早于工作窗口起点或按 id 排除最近 N 条即可
- `bot/prompts.py`：新增 `LABEL_RELEVANT_HISTORY = "【可能相关的历史片段】"`，
  拼进 **Block 3**（跟 memories 一起，紧邻 `memories_text()` 之后）——
  不新增第 5 个 cache block，沿用 Anthropic 4 个 cache_control 上限的约束

**cache 影响**：Block 3 现在每轮内容都可能因为检索结果不同而变化，
会持续 cache miss。4 个 block 的 `cache_control` 各自独立
（`prompts.py:151`），只丢 Block 3 这一块的缓存收益，Block 1/2/4 不受影响。

**规模注意**：`conversation_messages` 会随时间无限增长，检索时全表扫描
算 cosine 相似度是 O(N)。当前个人使用量级下完全没问题；如果将来涨到
几万条以上导致检索明显变慢，再考虑限制扫描窗口（比如只扫最近 90 天）
或引入 `sqlite-vec` 之类的索引——现在不用为这个预先设计。

### 验收

- 说一句带具体项目/作业名称的进展（"CS101 作业文件夹建好了"），几分钟后
  查 `conversation_messages`，确认该行 `embedding` 已经不为空
- 换个话题聊几轮后，重新提到同一个作业，确认 prompt 里
  "【可能相关的历史片段】"包含了那句"文件夹建好了"，而不是让 AI 又提示
  "该建文件夹了"
- 一条很短、缺上下文的消息（比如单独一句"弄好了"）也能在
  `embedding_context` 里看到它带着前几轮上下文一起被编码
- 停掉/网络失败时 `embed_text` 返回 `None`，那条消息 `embedding` 留空，
  不影响正常聊天

---

## 4. Part C（可选，优先级最低）：模糊时间提示

**先不做**，除非你已经真的遇到过"AI 把不确定的时间当成确定的直接改了
deadline"这种事故。真要做，最小实现不是建 `deadline_candidates` 表，
而是在 `add_deadline` / 更新 deadline 的工具 description 里加一句规则
（比如"如果用户原话包含'好像/可能/大概/待确认'，回复里必须明确说这是
待确认，不要当成已确认"），靠 prompt 约束，不需要数据库状态机。
等这个规则型方案实测不够用了，再考虑要不要落库追踪 candidate 状态。

---

## 4a. Part D：Memory 管理 UI（手动整理）

### 现状

Dashboard 已经有一个 "Memory" 标签页（`frontend/src/app/App.tsx` 的
`MemoryPage`，`ViewMode` 里的 `'memory'`），从 `/api/memories`
（`api/server.py:132`）拉数据，但只是只读展示（`memories.slice(0, 12)` +
"show all" 展开），没有编辑/删除入口。后端 `db.update_memory`
（`database.py:872`）已经存在，只是没接对应的 API 路由——create
（`POST /api/memories`）和 delete（`DELETE /api/memories/{id}`）都已经有了。

### 改动

- `api/server.py`：新增 `PATCH /api/memories/{memory_id}`，body 支持
  `content`/`memory_type`/`valid_until`（都可选，只更新传入的字段），
  调用 `db.update_memory` —— 需要顺手把 `update_memory` 签名从"只能改
  content"扩展成支持这三个字段的部分更新
- `frontend/src/app/App.tsx`：`MemoryPage` 每一项加编辑（inline 或弹窗，
  参考 `Dashboard.tsx` 里 `ActionModal` 的现成模式）和删除按钮；编辑表单
  暴露 `memory_type`（自由文本输入或下拉常见值）和 `valid_until`
  （日期选择，留空 = 永久）

这部分依赖 Part B1 的数据库字段（`memory_type`/`valid_until`）先落地，
但和 Part A / B2 互不阻塞，可以并行做。

---

## 5. 文件改动清单

- `bot/database.py`：
  - 新增 `ai_runs`、`tool_calls` 两张表及写入方法
  - `memories` 表加 `memory_type`/`valid_until`，去掉 20 条 FIFO 硬删
  - `conversation_messages` 表加 `embedding`/`embedding_context`
  - 新增 `get_relevant_conversation_snippets(query_embedding, channel_id, limit)`
- `bot/trace.py`：`finalize()` 加 `db` 参数，新增 `_persist_to_db()`
- `bot/ai_engine_base.py`：三处 `trace.finalize()` 调用补 `db=db`；
  `chat()` 里加 query embedding 计算，传入 `_build_prompt` 并注入检索结果
- `bot/embeddings.py`（新）：`embed_text()` + 余弦相似度 helper
- `bot/discord_bot.py`：`add_conversation_message` 之后挂后台 embedding 任务
- `bot/tools.py`：`save_memory`/`update_memory` schema 加
  `memory_type`/`valid_until`，description 说明用途收窄
- `bot/prompts.py`：新增 `LABEL_RELEVANT_HISTORY`，拼进 Block 3
- `docs/database.md`：补 `ai_runs`/`tool_calls` 两张新表、
  `memories`/`conversation_messages` 新字段说明
- `api/server.py`：新增 `PATCH /api/memories/{memory_id}`
- `frontend/src/app/App.tsx`：`MemoryPage` 加编辑/删除入口

---

## 6. 明确不做（重申，避免以后又绕回去）

- v1 整个方案：滚动会话摘要、分层压缩、每日自我反思
- Redis、正式向量库/索引（暴力 cosine 扫描够用，等真的慢了再说）
- `memory_candidates` + weekly curator + monthly decay 状态机
  （B2 的 recency 衰减是检索时实时算的公式，不是批量 AI 决策 job，两者不是一回事）
- daily auditor（用 AI 审计 AI 的行为）+ `audit_findings` +
  `behavior_adjustments`
- 独立 `assistant_messages` 表
- `deadline_candidates` 状态机（除非 Part C 的规则型方案实测不够用）

以上任何一项，等真的因为"记忆/审计"这件事吃过一次实际的亏
（bot 说记下了但没记、deadline 被静默改错、聊天记忆污染了判断），
再回来加，不要提前建。
