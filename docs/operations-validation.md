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

最后更新：2026-07-18 00:30 UTC

| 检查项 | 最近执行时间（UTC） | 状态 | 结果/证据 | 下次动作 |
|---|---|---|---|---|
| Python 全部自动测试 | 2026-07-18 00:08 | PASS | `.venv/bin/python -m pytest -q`：152 passed，24.19s（含 curator transaction/evidence/cursor 测试） | 每次部署前重跑 |
| 前端生产构建 | 2026-07-18 00:29 | PASS | `make deploy-local` Docker frontend-builder 构建成功 | 每次部署前重跑 |
| npm 干净安装 | 2026-07-17 03:58 | PASS | `npm ci` 安装 287 packages；audit findings 与 07-11 相同，仍待审查 | 审查 audit findings；依赖变更后重跑 |
| npm Docker builder | 2026-07-18 00:29 | PASS | `make deploy-local` 构建 `life-tracker:local` 成功（含 frontend-builder） | Dockerfile/依赖变更后重跑 |
| Production health endpoint | 2026-07-18 00:30 | PASS | `/internal/health` 200 | 每次部署后重跑 |
| Production 容器状态 | 2026-07-18 00:30 | PASS | app `(healthy)`，restart=0；Discord bot/scheduler 启动日志正常 | 每次部署后重跑 |
| SQLite quick check | 2026-07-18 00:30 | PASS | `PRAGMA quick_check` → `ok`；部署前快照 `pre-lt136-curator-20260718-001309.db` integrity=`ok` | 每次部署后重跑 |
| Litestream 写入 R2 | 2026-07-17 13:33 | PASS | 部署后 `wal segment written`（position 00000261/00000262） | 每次部署后检查复制；每季度恢复演练 |
| 本地 SQLite 快照恢复 | 未知 | NOT TESTED | 部署前快照 `pre-deploy-20260717.db` quick_check=ok，但未演练恢复启动 | 下次高风险部署前演练 |
| R2/Litestream 完整恢复 | 2026-07-11 13:30 | PASS | 从 R2 恢复到全新 `/tmp` 路径；integrity check、数据新鲜度和 API-only smoke test 均通过 | 2026-10 前重跑，或 Litestream/R2 变更后立即重跑 |
| 网络监听/路由审计 | 2026-07-17 13:34 | PASS | `infra audit`：life-tracker 监听 `127.0.0.1:8080` 无异常；5 个告警均属 staging/llm-gateway/未登记工具进程，与部署前基线一致 | 每次部署后重跑 |
| Dashboard/API 鉴权 | 2026-07-17 06:55 | PASS | 自动验收全部通过；用户随后从真实浏览器确认登录和 Dashboard 使用“完全正常” | 每次鉴权/路由变更后重跑 |
| Staging 启动与隔离 | 未知 | NOT TESTED | 外部 Dockge staging compose 仍使用 `8081:8081`，可能绑定所有接口 | 修正权威 compose 后再验证 |
| memory.md 独立异地备份 | 未知 | NOT TESTED | `data/memory.md` 已成为记忆权威存储（07-17 上线），Litestream 只覆盖 SQLite；迁移期靠 legacy 表 shadow 兜底 | LT-132 验收前建立独立备份并演练恢复 |

## 已知未闭环事项

1. `life.purrden.cc` 仍是公网路由，但 Dashboard/API 已有应用层鉴权；Cloudflare Access 仍可作为额外防线，不再是保密性的唯一前置。
2. 外部 Dockge staging 权威 compose 的端口绑定尚未满足 VPS 私有监听纪律；仓库内历史参考文件已收紧，但不能替代外部文件。
3. 本地 SQLite `.backup` 快照的恢复流程尚未单独演练；R2/Litestream 恢复已于 2026-07-11 通过。
4. 2026-07-12 已决定暂不备份 `data/ai_traces/*.jsonl`。主机完全损坏时允许丢失 JSONL 原始 trace；SQLite 中的 `ai_runs`/`tool_calls` 仍由 Litestream 保护。
5. `npm ci` 报告 1 low、2 moderate、2 high；需单独运行 `npm audit` 评估可达性和升级影响，禁止未经审查直接 `--force`。

## 演练记录

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
