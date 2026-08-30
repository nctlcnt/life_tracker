# 运维验证台账

本文件回答三个问题：最近一次代码测试何时全部通过、生产进程何时确认正常、灾备恢复何时真正演练成功。

## 状态定义

- **PASS**：命令在所列时间实际成功，且有明确输出证据。
- **PARTIAL**：只验证了链路的一部分，不能推导完整能力可用。
- **FAIL**：检查实际失败。
- **NOT TESTED**：没有可核实的近期演练记录。
- **BLOCKED**：检查环境权限不足，结果无效，必须在正确环境重跑。

“容器在运行”“R2 有对象”或“Litestream 日志没有报错”都不能代替恢复演练。

## 当前基线

最后更新：2026-08-30 03:52 UTC

| 检查项 | 最近执行时间（UTC） | 状态 | 结果/证据 | 下次动作 |
|---|---|---|---|---|
| Python 全部自动测试 | 2026-08-30 03:20 | PASS | `.venv/bin/python -m pytest -q`：513 passed、1 warning，84.14s（含 Prompt runtime preview 回归） | 每次部署前重跑 |
| 前端生产构建 | 2026-08-30 03:20 | PASS | `npm run build --prefix frontend`：Vite 生产构建成功，2045 modules transformed | 每次部署前重跑 |
| npm 干净安装 | 2026-07-17 03:58 | PASS | `npm ci` 安装 287 packages；audit findings 与 07-11 相同，仍待审查 | 审查 audit findings；依赖变更后重跑 |
| npm Docker builder | 2026-08-30 03:50 | PASS | `make deploy-local` 构建 `life-tracker:local` 成功并重建 production app | Dockerfile/依赖变更后重跑 |
| Production health endpoint | 2026-08-30 03:51 | PASS | `/internal/health` 与容器内鉴权 `/api/health` 均返回 `{"status":"ok"}`；只读 Prompt preview API 返回 5 条轨道 | 每次部署后重跑 |
| Production 容器状态 | 2026-08-30 03:51 | PASS | app `(healthy)`；Discord bot、scheduler、outbound、batcher、tool worker apply、heartbeat 全部启动，启动日志零 error/exception | 每次部署后重跑 |
| SQLite quick check | 2026-08-30 03:51 | PASS | `PRAGMA quick_check` → `ok`；outbox、批次与待投递结果均无非终态记录 | 每次部署后重跑 |
| Litestream 写入 R2 | 2026-08-30 03:50 | PASS | 部署后新 WAL segments 已写入 R2（generation f8f62c83c2d3df4f，index 00000453/00000454） | 每次部署后检查复制；每季度恢复演练 |
| 本地 SQLite 快照恢复 | 2026-08-29 09:55 | PASS | `scripts/backup_and_verify.py --label pre-lt178-production`：在线快照 39.5 MB，`integrity_check` → `ok`，28 张表行数逐张比对通过 | 每次破坏性迁移前重跑 |
| R2/Litestream 完整恢复 | 2026-07-11 13:30 | PASS | 从 R2 恢复到全新 `/tmp` 路径；integrity check、数据新鲜度和 API-only smoke test 均通过 | 2026-10 前重跑，或 Litestream/R2 变更后立即重跑 |
| 网络监听/路由审计 | 2026-08-30 03:51 | PASS | `infra audit`：全部干净；production 8080 仍只监听 `127.0.0.1`；临时 9001 已停止并注销 | 每次部署后重跑 |
| Dashboard/API 鉴权 | 2026-07-17 06:55 | PASS | 自动验收全部通过；用户随后从真实浏览器确认登录和 Dashboard 使用“完全正常” | 每次鉴权/路由变更后重跑 |
| Staging 启动与隔离 | 2026-08-26 11:01 | PASS | 外部 Dockge compose 已改为 `127.0.0.1:9001→8081`；容器 healthy，internal/auth health 200，测试 Discord Bot 上线，`infra audit` clean | 完成 LT-170 人工并发场景后再部署 production |
| memory.md 独立异地备份 | 未知 | NOT TESTED | `data/memory.md` 已成为记忆权威存储（07-17 上线），Litestream 只覆盖 SQLite；迁移期靠 legacy 表 shadow 兜底 | LT-132 验收前建立独立备份并演练恢复 |

## 已知未闭环事项

1. `life.purrden.cc` 仍是公网路由，但 Dashboard/API 已有应用层鉴权；Cloudflare Access 仍可作为额外防线，不再是保密性的唯一前置。
2. ~~外部 Dockge staging 权威 compose 的端口绑定尚未满足 VPS 私有监听纪律~~ 已于 2026-08-26 改为私有监听；该 staging 后续已清空下线。2026-08-30 的 Prompt 临时预览复用登记端口 `9001` 并绑定 WireGuard，production 验证后已停止并注销。
3. ~~本地 SQLite `.backup` 快照的恢复流程尚未单独演练~~ 已于 2026-08-23 演练通过，见下方演练记录；工具固化为 `scripts/backup_and_verify.py`。
4. 2026-07-12 已决定暂不备份 `data/ai_traces/*.jsonl`。主机完全损坏时允许丢失 JSONL 原始 trace；SQLite 中的 `ai_runs`/`tool_calls` 仍由 Litestream 保护。
5. `npm ci` 报告 1 low、2 moderate、2 high；需单独运行 `npm audit` 评估可达性和升级影响，禁止未经审查直接 `--force`。

## 演练记录

### 2026-08-30 03:52 UTC — Prompt runtime preview 部署到 production

- 内容：PR #21 / merge commit `0a1db04`；后台新增只读“模型实际看到什么”查看器，覆盖 chat、check-in、execution、result expression 与 planned timeline renderer 五条轨道，并显示工具 schema、Prompt 来源、统计和 section inventory。
- 部署方式：`make deploy-local` 从最新 `main` 重建并重启 Dockge 管理的 production app；无数据库迁移，不改变现有双轨执行行为。
- 部署前：513 个 Python 测试通过；前端生产构建通过；outbox、tool batches 与待投递结果均排空；在线快照 `data/life_tracker.db.bak-20260830-034944` 的 `integrity_check` → `ok`。
- 部署后：app healthy；内部与鉴权 health 均 200；只读 Prompt preview API 返回 `read_only=true` 和 5 条轨道；Discord bot、scheduler、outbound consumer、batcher、tool worker 与 heartbeat 全部启动；SQLite `quick_check` → `ok`；Litestream 新 WAL 已写入 R2；公网 Dashboard 返回 200；`infra audit` clean，8080 仅绑定 `127.0.0.1`。
- 临时环境清理：停止并移除 `life-tracker-prompt-preview` 容器及其网络，注销 WireGuard 预览端口 9001；未删除隔离数据目录和本地预览镜像。

### 2026-08-29 09:58 UTC — LT-178 双层 worker 部署到 production

- 内容：PR #20 / merge commit `907bdc6`；聊天与工具双层 worker、durable tool-call ledger、batch/heartbeat/outbox 生命周期、shadow/apply 和静默/反应/消息降级路径。
- 部署方式：生产 `config.json` 明确启用 `outbound_queue_enabled`、`tool_worker_enabled`、`tool_worker_apply`；`make deploy-local` 重建并重启 Dockge 管理的 `life-tracker:local`。
- 部署前：509 个 Python 测试通过；前端生产构建与 `git diff --check` 通过；outbox、tool batches 与待投递结果均已排空。
- 备份：`data/backups/pre-lt178-production-20260829-095550.db`，39.5 MB；恢复校验 `integrity_check` → `ok`，28 张表行数一致。
- Cutover：执行轨从 production 当前消息尾部 1777 开始，不回放旧机制消息；运行状态 `enabled`，worker 模式 `apply`。
- 部署后：app healthy；内部与鉴权 health 均 200；Discord bot、scheduler、outbound consumer、batcher、tool worker 与 heartbeat 全部启动；数据库 `quick_check` → `ok`；无非终态队列；Litestream 新 WAL 已写入 R2；`infra audit` clean，8080 仅绑定 `127.0.0.1`。

### 2026-08-26 11:01 UTC — LT-170 统一发送队列部署到 staging

- 内容：commit `0902838` / PR #14；共享 `GenerationGate`、SQLite `outbound_deliveries`、单 consumer、同频道队首顺序、lease fencing/恢复、失败重试与分阶段开关。
- 部署目标：独立 staging Bot、`config.dev.json` 和 `data-dev/life_tracker.db`；只启用 `outbound_queue_enabled`，tool worker/apply 保持关闭；production 未改动。
- 部署前：285 个 Python 测试通过；前端生产构建通过；staging 在线备份 `data-dev/life_tracker.db.bak-20260826-110014`，`integrity_check` → `ok`。
- 网络修正：`infra allocate life-tracker-staging api` 登记端口 9001；外部 Dockge compose 从 `8081:8081` 改为 `127.0.0.1:${PORT}:8081`，`.env` 写入 `PORT=9001`；未创建公网路由。
- 部署后：容器 healthy；`/internal/health` 与鉴权 `/api/health` 均 200；日志确认 `OutboundQueue consumer 已启动`、统一发送队列 enabled、测试 Bot `CC#7632` 上线；outbox 表已创建且无非终态 delivery；数据库 `quick_check` → `ok`；`infra audit` → clean。
- 未覆盖：真实 Discord 上“聊天生成中触发 check-in/reminder”、超过 2000 字分片与 ✅ reaction 的人工顺序验收，留给本轮用户测试。

### 2026-08-23 13:38 UTC — 记忆状态阶梯与种子数据部署

- 内容：6 个 commit，`2a51d43`..`0d2b78e`。记忆表状态阶梯（三值→五值）、onboarding 种子入库、按注入权限分档读取（**开关默认关闭**）、设计文档重写，以及工具轮第二轮起改发精简 system prompt。
- 部署方式：`make deploy-local`（`compose.yaml`，Dockge 管理的 prod 栈，自动读 `.env.prod`）。
- 部署前：236 个 Python 测试通过；快照 `pre-onboarding-seed-20260823-045043.db`，integrity `ok`，24 张表行数比对通过。
- 部署后：容器 `(healthy)`；`/internal/health` 200；Discord bot 上线（ひより#5775 → channel 1503879130679083028）；scheduler 启动；启动日志零 error / exception；`PRAGMA quick_check` → `ok`；Litestream 恢复写入（generation `f8f62c83c2d3df4f`）。
- **本次唯一生效的行为变化是工具轮的精简 system prompt**（以及 `add_deadline` 的一条 post-hint）。记忆读取侧由 `memory.read_from_db` 开关控制，当前为 `False`，仍读 `data/memory.md`。刻意这样安排：两批都会改变可感知的行为，同时上线就无法归因。
- 数据库迁移在此之前就已经跑过（做种子入库时 `Database()` 初始化触发），因此部署前存在一段**旧代码配新库**的版本错位。当时无害——`curator_auto_enabled` 为 `False`，没有代码路径读写 `personal_memories`——本次部署消除了该错位。
- 未覆盖：真实 Discord 对话中的多轮工具调用行为尚未验证，这正是精简 system prompt 影响的路径，需要用户实际使用后确认。

### 2026-08-23 00:58 UTC — 本地 SQLite 快照恢复演练（首次）

- 触发原因：记忆表状态阶梯改造需要重建 `personal_memories`（SQLite 无法修改 `CHECK`，只能重建表），属于破坏性迁移，按纪律必须先备份并演练恢复。
- 工具：新增 `scripts/backup_and_verify.py`。它用 `sqlite3` 的在线备份 API 而不是 `cp`——生产库是 WAL 模式且 bot 在跑，直接复制文件可能拿到缺少 WAL 尾部的中间状态。
- 快照：`data/backups/pre-status-ladder-20260823-005850.db`，39.4 MB。
- 恢复演练：把快照拷到临时目录打开，`PRAGMA integrity_check` → `ok`；再与源库逐表比对行数，24 张表全部一致。
- 为什么要比行数：`integrity_check` 只证明文件结构没坏，证明不了内容还在。快照拍错源、拍到空库这类问题只有比对行数才会暴露。
- 结论：本条从 `NOT TESTED` 改为 `PASS`。这是该项目**第一次**真正演练本地快照恢复；此前只验证过 R2/Litestream 路径（2026-07-11）。
- 未覆盖：本次只验证了「快照可以打开且内容完整」，**没有**验证「用这个快照启动应用并正常服务」。真要做完整回滚时，还需要停容器、换文件、起容器这一段。

### 2026-07-11 13:29 UTC — Litestream/R2 隔离恢复演练

- Litestream 版本：0.3.13
- 来源 replica：Cloudflare R2，generation `f8f62c83c2d3df4f`
- 恢复目标：全新临时目录 `/tmp/life-tracker-drill-20260711-1329`
- 恢复范围：snapshot index 232 + WAL，恢复到 index 274
- 开始/完成时间：13:30:06 / 13:30:18 UTC；约 12 秒
- Production 最新 conversation 时间：`2026-07-11T13:27:56.629000+00:00`
- 恢复库最新 conversation 时间：`2026-07-11T13:27:56.629000+00:00`
- 实测 RPO：关键表最新时间一致（在本次比较精度下为 0）
- Production/恢复文件大小：33,542,144 bytes / 33,542,144 bytes
- `PRAGMA integrity_check`：`ok`；journal mode：`wal`
- 关键数据：conversation_messages 1249、events 534、ai_runs 363、prompt_sections 17
- 隔离实例：独立 Compose project `life-tracker-drill`，`--api-only`，仅绑定 `127.0.0.1:9005`
- Smoke test：health、version、timeline、memories、todos、projects、prompts 读取通过；容器 healthy
- Trace Viewer：`/api/traces/dates` 为空，因为 JSONL trace 文件不在 SQLite/R2 replica 中；SQLite `ai_runs` 已恢复
- Discord/scheduler：未启动（日志明确确认 api-only）
- 是否触碰 production：否；production app 全程 healthy，Litestream 全程 running，挂载保持 `data/`
- Staging 原数据：`data-dev/` 未替换、未修改
- 清理：临时容器与网络已删除；端口 9005 已从 infra 注销
- 结果：PASS
- 后续动作：2026-10 前完成下一次季度演练；JSONL 仅在成为产品数据源或出现审计保留要求时重新评估

## 每次部署记录模板

### 2026-07-18 01:31 UTC — feat/LT-136-memory-curator-worker@7d911fe（compact 绝对时间 + 固定模板）

- 操作者：Codex（chacha 授权）
- 部署来源：`life-tracker:local`
- Git commits：`4d71880`、`7d911fe`
- 工作区是否干净：部署时是；本台账在部署验证后更新
- 风险分类：中（改变 compact prompt 和摘要写回校验，并全量重建现有摘要）
- 部署前快照：`data/backups/pre-compact-timefix-20260718-012157.db`；`integrity_check` → `ok`
- Python tests：156 通过 / 0 失败；Frontend build：PASS
- Production health：`/internal/health` 200；app healthy，restart=0；实际监听 `127.0.0.1:8080`
- Compact preset：尊重 Admin 配置 `relay-gemini-3-pro-preview`；实际模型 `gemini-3.1-pro-preview`
- 重建：从 1298 条原始消息全量重建到 cursor 1298（输入约 87,103 tokens）；第一次输出因“近期”被拒绝且旧摘要保持不变，加入同 preset 单次定向修正后重建成功
- 摘要验收：固定五段 Markdown 模板；生成基准 `2026-07-18 11:31:34 (Australia/Sydney)`；正文无今天/明天/下周/周几/近期等无锚点相对时间
- 数据边界：明文尾巴 110 条（id 1299..1408），tail embedding 0；折叠区间 embedding 1298/1298；SQLite `quick_check` → `ok`
- 异常与回滚：格式/时间校验失败最多定向修正一次，仍失败则拒绝写回。回滚代码到 `a9584ec`，摘要状态可从部署前快照恢复。

### 2026-07-18 00:29 UTC — feat/LT-136-memory-curator-worker@ff9135f（人工闸门 curator 上线）

- 操作者：Codex（chacha 授权）
- 部署来源：`life-tracker:local`
- Git commits：`1c1607f`、`ff9135f`
- 工作区是否干净：部署时是；本台账在部署验证后更新
- 风险分类：中（新增 curator 审计和可选 apply path；未接入 scheduler）
- 部署前快照：`data/backups/pre-lt136-curator-20260718-001309.db`；`integrity_check` → `ok`
- Python tests：152 通过 / 0 失败；Frontend build：PASS
- Production health：`/internal/health` 200；app healthy，restart=0；实际监听 `127.0.0.1:8080`
- SQLite：`quick_check` → `ok`；dry-run 后 `personal_memories=0`、`personal_memory_sources=0`、`curator_cursors=0`
- Curator smoke：容器内 `glm`，冻结 `conversation_messages.id=1..10`，run `96f93c0f107f`，严格校验通过，`operations=[]`
- 自动写入：关闭；没有运行 `--apply-proposal`，没有接入 `main.py` / scheduler
- 异常与回滚：active `kiro` 大批次超时、`glm` 50 条首次返回 markdown fence，均未写 memory/cursor；随后加入 message `created_at`/当前 UTC freshness guard。回滚代码到 `768a08d`，DB 无 curator mutation 需要回退。

### 2026-07-17 13:32 UTC — main@f9f20c8（LT-135 聊天上下文 token 窗口 + 自动 compact 上线）

- 操作者：Claude Code（用户确认「要！」）
- 部署来源：`life-tracker:local`
- Git commit：`f9f20c8`（main，已推送 origin）
- 工作区是否干净：是
- 风险分类：中（聊天历史取用机制全量切换；无 DB schema 变更，复用 app_state）
- 部署前快照：`data/backups/pre-lt135-20260717-133220.db`；`integrity_check` → `ok`
- Python tests：114 通过 / 0 失败（新增 34 个窗口装配/compact/接线测试）
- Frontend build：PASS（deploy-local Docker frontend-builder；AdminPanel compact 下拉 + TraceViewer 窗口展示）
- Production health：PASS（`/internal/health` 200；无 key `/api/health` 401；带 key 200）
- 容器状态：app 重建后 Up (healthy)；Litestream 未重建、Up 10 days；Discord bot 上线（ひより#5775）；scheduler 正常
- SQLite quick check：`ok`；journal mode=`wal`
- Litestream 最近成功复制时间：2026-07-17 13:33:51 UTC
- 实际监听：`127.0.0.1:8080`
- `infra audit`：PASS for life-tracker；5 个告警属 staging/llm-gateway/未登记工具进程，与部署前基线一致
- Smoke test：新端点 `GET/PUT /api/admin/compact-preset` 正常（设置为 `gemini-flash-lite-paid`）；prod 数据只读装配窗口正常
- **Bootstrap compact（受控执行）**：prod 全量历史 66,357tk（1000 条），首次装配会硬裁 714 条并在聊天中后台触发大 compact——改为部署后立即受控执行：flash-lite 一次折叠到 id=1298，摘要 963tk，装配后窗口 8,866tk（摘要 + 97 条明文），needs_compact=False、无硬裁。摘要内容人工抽查正确（学期任务/睡眠规律/健康关注点）
- 预期变化：每次聊天请求的 history 从固定 ~20 条（1-3k tk）变为 8-20k tk 弹性窗口，prompt 总量上升属设计内（24k 预算）；compact 稳态约每 12k tk 增量触发一次
- 异常与回滚：无异常。回滚路径 = `git checkout 8fbcf18` + `make deploy-local`（窗口/摘要状态在 app_state，旧代码不读该键，直接回退安全）

### 2026-07-17 09:51 UTC — main@3308e78（LT-134 引擎合并：四 provider 引擎收敛为单一 OpenAI 兼容实现）

- 操作者：Claude Code（用户授权「合并到main直接部署」）
- 部署来源：`life-tracker:local`
- Git commit：`3308e78`（main，已推送 origin）
- 工作区是否干净：是
- 风险分类：中（AI 引擎全量替换；无 DB schema 变更、无鉴权/路由变更）
- 部署前快照：未拍——本次无 DB 迁移，部署不触碰数据库；部署后 `quick_check` → `ok` 补验
- Python tests：84 通过 / 0 失败（LT-134 新增引擎/路由/trace 契约测试）
- Frontend build：PASS（deploy-local Docker frontend-builder）
- Production health：PASS（`/internal/health` 200；无 key `/api/health` 401；带 key 200）
- 容器状态：app 重建后 Up (healthy)；Litestream 未重建、Up 10 days；Discord bot 上线（ひより#5775）；scheduler 正常排程（bedtime/reminder/calendar 刷新）
- SQLite quick check：`ok`；journal mode=`wal`
- Litestream 最近成功复制时间：2026-07-17 09:51:32 UTC
- 实际监听：`127.0.0.1:8080`
- `infra audit`：PASS for life-tracker；5 个告警属 staging/llm-gateway/未登记工具进程，与部署前基线一致
- Smoke test：容器内经统一引擎实测 `POST /api/admin/presets/test`——kiro（active，4391ms）、glm（fallback，4714ms）、gemini-flash-lite-paid（已迁 Google OpenAI 兼容端点，1334ms）均 `ok=true`；公网 `life.purrden.cc` 无鉴权 401 正常
- 配套 config.json 变更（无 git 记录）：3 个原生 Gemini preset 迁移到 `generativelanguage.googleapis.com/v1beta/openai`（同 key 同模型，迁移前逐一实测含 tools 可达）；删除过期 preset `new_api`（指向 localhost:3000 的旧部署）
- 异常与回滚：无异常。回滚路径 = `git checkout 8c5c69d` + `make deploy-local`（无 DB 迁移可直接回退；迁移后的 Gemini preset 是 relay provider，旧代码同样能跑，config 无需随代码回退）

### 2026-07-17 06:32 UTC — main@d0fb413（LT-139 Dashboard/API 应用层鉴权上线）

- 操作者：Codex（用户授权“开始做 139”）
- 部署来源：`life-tracker:local`
- Git commit：`d0fb413`（main）
- 工作区是否干净：是（`.env.prod` 为 ignored runtime secret）
- 风险分类：高（公网 Dashboard/API 鉴权与健康检查切换）
- 部署前快照：`data/backups/pre-lt139-20260717-063213.db`；`integrity_check` → `ok`
- Python tests：57 通过 / 0 失败
- Frontend build：PASS（Docker frontend-builder，2038 modules）
- Production health：PASS（`/internal/health` 200；带 key `/api/health` 200）
- 容器状态：app Up (healthy)；Litestream 未重建、Up 10 days；Discord bot/scheduler 正常
- SQLite quick check：`ok`；journal mode=`wal`
- Litestream 最近成功复制时间：2026-07-17 06:32:51 UTC
- 实际监听：`127.0.0.1:8080`
- `infra audit`：PASS for life-tracker；5 个告警属于停用的 staging/llm-gateway 或当前工具进程
- Smoke test：公网未授权 `/api/memories` GET/POST、`/docs`、`/openapi.json` 均 401；`X-API-Key` 200；登录 cookie 200；logout 后重新 401；cookie 为 Secure/HttpOnly/SameSite=Strict；OAuth callback 到达原 handler
- 用户验收：2026-07-17 06:55 UTC，真实浏览器登录与 Dashboard 使用完全正常
- 异常与回滚：无应用异常；旧镜像 `life-tracker:rollback-pre-lt139-20260717-063213`；DB 快照见上；部署中仅遇沙箱无法 socket/读取 systemd，均在宿主权限下重验通过

### 2026-07-17 04:08 UTC — main@7d4ec52（Markdown memory + check-in 框架 + LT-130 检索修复上线）

- 操作者：Claude Code（chacha 授权）
- 部署来源：`life-tracker:local`
- Git commit：`7d4ec52`（main）
- 工作区是否干净：是
- 风险分类：中（长期记忆存储从 SQLite 切换到 `data/memory.md`）
- 部署前快照：`data/backups/pre-deploy-20260717.db`；`quick_check` → `ok`
- Python tests：46 通过 / 0 失败
- Frontend build：PASS
- Production health：PASS
- 容器状态：app 重建后 Up (healthy)；Litestream 未重建，Up 10 days
- SQLite quick check：`ok`；journal mode=`wal`
- Litestream 最近成功复制时间：2026-07-17 04:08:31 UTC
- 实际监听：`127.0.0.1:8080`
- `infra audit`：PASS（告警均属其他栈）
- Smoke test：`/api/health` ok；`/api/memories` 返回 18 条；`/api/memory-document` 18/18 条、1136/4000 tokens、与部署前 `memories` 表逐条比对一致；`/api/check-ins` 返回 4 个内置项（enabled 状态与部署前一致）；Discord bot 上线、scheduler 正常排程
- 异常与回滚：无异常。回滚路径 = checkout 旧 commit 重建镜像 + `pre-deploy-20260717.db` 快照；记忆可回退读 legacy `memories` 表（迁移期 shadow 保持同步）
- 待用户验证：Discord 实测 memory CRUD 与 `search_history`（LT-130/131 收尾项）；prompt trace 确认注入的记忆不含 metadata 注释（LT-132 验收项）

复制以下段落到本节顶部，保留历史记录，不要覆盖旧记录：

```markdown
### YYYY-MM-DD HH:MM UTC — <版本/commit/部署说明>

- 操作者：
- 部署来源：`life-tracker:local` / `ghcr.io/...:vX.Y.Z`
- Git commit：
- 工作区是否干净：
- 风险分类：低 / 中 / 高
- 部署前快照：文件名；`integrity_check` 结果
- Python tests：通过数 / 失败数
- Frontend build：PASS / FAIL
- Production health：PASS / FAIL
- 容器状态：
- SQLite quick check：
- Litestream 最近成功复制时间：
- 实际监听：
- `infra audit`：PASS / FAIL / BLOCKED
- Smoke test：
- 异常与回滚：
```

## 灾备演练记录模板

只有完成真实恢复后才能填写 PASS：

```markdown
### YYYY-MM-DD HH:MM UTC — Litestream/R2 恢复演练

- Litestream 版本：
- 来源 replica：R2（不要记录 secret）
- 恢复目标：全新临时路径
- 目标恢复点：
- 开始/结束时间：
- 恢复耗时：
- Production 最新数据时间：
- 恢复库最新数据时间：
- 实测 RPO：
- 文件大小：
- `PRAGMA integrity_check`：
- 关键表/行数抽查：
- 隔离实例 smoke test：
- 是否触碰 production：否（必须）
- 结果：PASS / FAIL
- 问题与后续动作：
```

## 更新纪律

- 时间统一使用 UTC，避免服务器时区与应用时区混淆。
- 不在本文件写 token、API key、R2 endpoint secret、OAuth code 或私人消息正文。
- 所有 PASS 必须来自实际命令结果；推测只能记为 PARTIAL 或 NOT TESTED。
- 失败记录保留，修复后新增一条验证记录，不回写历史为成功。
