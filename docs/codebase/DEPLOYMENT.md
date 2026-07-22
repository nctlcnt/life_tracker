# Life Tracker 部署与灾备

本文记录当前 VPS 上的真实运维方式，不是通用云服务器安装教程。测试、构建和灾备演练的最近结果统一记录在 [OPERATIONS-LOG.md](../OPERATIONS-LOG.md)。

## 1. 当前生产拓扑

| 项目 | 当前值 |
|---|---|
| 仓库 | `/home/ubuntu/stacks/life_tracker` |
| 生产编排 | Dockge 管理的 `compose.yaml` |
| 应用镜像 | 从当前工作树构建的 `life-tracker:local` |
| 应用监听 | 宿主 `127.0.0.1:8080` → 容器 `8080` |
| 数据库 | `data/life_tracker.db`（SQLite WAL） |
| 配置 | `config.json`；不得提交到 Git |
| 环境变量 | `.env` → `.env.prod` 软链接 |
| 灾备 | Litestream sidecar 持续复制 SQLite 到 Cloudflare R2 |
| 重启策略 | app 与 Litestream 均为 `unless-stopped` |
| 健康检查 | 容器内 `http://127.0.0.1:8080/internal/health` |

`compose.yaml` 是 Dockge 和 `make deploy-local` 共用的生产权威文件。目录中同时存在 `docker-compose.yml`，所以直接运行 `docker compose` 会提示发现多个配置文件；它会选择 `compose.yaml`。本地开发必须显式使用 `-f docker-compose.yml`。

### 网络边界

Dashboard/API 使用应用层 API key + `HttpOnly` session cookie 鉴权，同时保留宿主
绑定、WireGuard 和受控 Cloudflare 路由作为网络边界：

- 应用端口只允许绑定 `127.0.0.1` 或 `10.66.66.1`，禁止 `0.0.0.0`。
- 不通过公网 IP 加端口访问服务。
- Google Calendar OAuth 回调使用 `https://oauth.purrden.cc/api/calendar/oauth/callback`。
- `infra overview` 当前还登记了 `life.purrden.cc → 127.0.0.1:8080`。公网访问先经过应用层登录；仍可额外叠加 Cloudflare Access。
- `.env.prod` 必须有至少 32 字符的 `LIFE_TRACKER_API_KEY`，且生产保持 `LIFE_TRACKER_COOKIE_SECURE=true`。缺失或过短时所有受保护 API 会 fail closed（503），不会静默放行。
- 所有 `/api/*` 默认受保护；仅登录/session/logout 和 Google Calendar OAuth callback 精确豁免。程序化调用发送 `X-API-Key`，浏览器登录后使用 30 天有效的签名 cookie。
- 不手工创建 Cloudflare tunnel；发布和下线统一使用 `infra publish` / `infra unpublish`。

每次部署后必须确认实际监听仍为 `127.0.0.1:8080`，并运行 `infra audit`。如果受限环境无法读取 systemd 或 socket，审计结果不得记为通过，应在正常宿主 shell 重跑。

## 2. 部署策略

这是单用户服务，不要求每次改动都完整走 dev → staging → release。按风险选择流程：

| 变更类型 | 推荐流程 |
|---|---|
| 文档、纯样式、低风险 UI | 本地测试/构建后直接部署 production |
| 小范围 Python 修复、非破坏性 API 变更 | 全部自动测试 + 前端构建 + 数据库快照后直接部署 |
| SQLite schema/migration、scheduler、Discord 消息流、AI tools/prompt 组装 | 先 staging，再部署 production |
| 依赖大版本、网络/认证、Litestream/恢复逻辑 | staging + 恢复演练 + 明确回滚点 |
| 希望得到不可变镜像和版本标签 | 使用 GHCR release 流程 |

无论采用哪条路径，部署前都要查看 `git status --short`，确认没有把不相关的工作区改动带入镜像。

## 3. 日常本地构建部署

### 3.1 部署前验证

```bash
cd /home/ubuntu/stacks/life_tracker

git status --short
.venv/bin/python -m pytest -q
npm ci --prefix frontend
npm run build --prefix frontend
git diff --check
```

`npm ci` 严格使用 `frontend/package-lock.json`。依赖未变且已经安装时，可以只运行 `npm run build --prefix frontend`，但 CI/正式发布仍应从干净安装验证。

### 3.2 创建在线 SQLite 快照

数据库处于 WAL 模式，不要用普通 `cp` 把正在写入的主文件当作可靠在线快照。使用 SQLite backup API：

```bash
cd /home/ubuntu/stacks/life_tracker
backup="data/life_tracker.db.bak-$(date -u +%Y%m%d-%H%M%S)"
sqlite3 data/life_tracker.db ".backup '$backup'"
sqlite3 "$backup" "PRAGMA integrity_check;"
```

只有输出 `ok` 才继续部署。备份属于敏感个人数据，保持在受限主机/R2 内，不要提交或上传到公开位置。

### 3.3 部署

```bash
make deploy-local
```

它等价于使用 `compose.yaml` 从当前工作树重新构建并启动生产栈。它会直接影响 production，不会创建 Git tag，也没有自动生成的镜像回滚版本。

### 3.4 部署后验证

```bash
docker compose ps
curl -fsS http://127.0.0.1:8080/internal/health
curl -fsS -H "X-API-Key: $LIFE_TRACKER_API_KEY" http://127.0.0.1:8080/api/health
docker compose logs --tail 100 app
docker compose logs --tail 100 litestream
sqlite3 data/life_tracker.db "PRAGMA quick_check;"
infra audit
```

通过标准：

- app 为 `healthy`，Litestream 为 `Up`；
- internal health 和携带 API key 的 `/api/health` 都返回 `{"status":"ok"}`；
- 应用日志无启动 Traceback；
- `PRAGMA quick_check` 返回 `ok`；
- Litestream 日志有近期 `wal segment written`，且无持续上传错误；
- 8080 仍只监听 `127.0.0.1`；
- 将结果追加到 `docs/operations-validation.md`。

## 4. Staging

Staging 是独立的 Dockge 栈，权威文件不在本仓库：

```text
/home/ubuntu/stacks/life-tracker-staging/compose.yaml
```

本仓库的 `docker-compose.staging.yml` 仅为历史参考，禁止从它启动，否则会与 Dockge 栈冲突。

Staging 使用：

- 第二个 Discord bot；
- `config.dev.json`；
- `data-dev/life_tracker.db`；
- 端口 8081；
- bind-mounted 源码与 `frontend/dist`；
- 不运行 Litestream，不复制到生产 R2。

完整重建（依赖或镜像层变化）：

```bash
cd /home/ubuntu/stacks/life-tracker-staging
docker compose up -d --build
docker compose ps
docker compose logs --tail 100 app
curl -fsS http://127.0.0.1:8081/internal/health
```

快速迭代（Python 或前端代码变化）：

```bash
cd /home/ubuntu/stacks/life_tracker
npm run build --prefix frontend

cd /home/ubuntu/stacks/life-tracker-staging
docker compose restart app
```

注意：当前 staging compose 的端口写法是 `8081:8081`，Docker 默认会绑定 `0.0.0.0`，不符合本 VPS 纪律。在再次启用 staging 前，应先改成 `127.0.0.1:8081:8081` 或按需要绑定 `10.66.66.1:8081:8081`，然后用 `infra audit` 验证。

## 5. 可选：版本化 GHCR 发布

需要不可变版本、跨机器部署或清晰镜像回滚点时使用：

```bash
make release VERSION=vX.Y.Z
gh run list --workflow Release --limit 5
make deploy VERSION=vX.Y.Z
```

Tag 会触发 GitHub Actions 构建 `linux/amd64` 和 `linux/arm64` 镜像，并推送：

- `ghcr.io/nctlcnt/life_tracker:vX.Y.Z`
- `ghcr.io/nctlcnt/life_tracker:stable`

`make deploy` 使用 `docker-compose.prod.yml`，而 Dockge 的 `compose.yaml` 仍声明本地镜像 `life-tracker:local`。因此 GHCR 部署后若再从 Dockge 重建，可能切回本地镜像。使用该流程时必须记录当前部署来源，并避免把两种编排方式当成同一个状态。

镜像回滚：

```bash
make deploy VERSION=vPREVIOUS
```

镜像回滚不会自动回滚数据库 schema 或数据。

## 6. 灾备与恢复

### 6.1 灾备层级

| 层级 | 用途 | 不能替代什么 |
|---|---|---|
| SQLite 在线快照 | 部署前快速回滚 | 异机灾难恢复 |
| Litestream → R2 | 主机/磁盘损坏后的异地恢复 | 已验证的恢复流程 |
| Git tag / GHCR 镜像 | 恢复应用代码和依赖 | SQLite 数据恢复 |
| `config.json` / OAuth 凭据 | 恢复运行配置 | 数据库或镜像 |

### Trace 灾备范围决策（2026-07-12）

`data/ai_traces/*.jsonl` 暂不纳入独立灾备。这是有意接受的取舍，不是遗漏：

- Litestream 只复制 `life_tracker.db`，因此会保护结构化的 `ai_runs` 和 `tool_calls`；
- JSONL 保存更完整的调试 payload，但当前不为它增加 R2 同步、归档或额外恢复流程；
- 主机或磁盘完全损坏时，允许丢失 JSONL 历史，Trace Viewer 的日期/原始详情可能不完整；
- 该决定不影响 conversation、timeline、memory、todo、prompt 和结构化 AI run/tool-call 数据的恢复；
- 当 JSONL 成为产品功能的数据源、出现必须保留的审计需求，或其不可替代价值明显提高时，再重新评估备份。

“Litestream 正在成功上传”只证明复制路径工作，不证明恢复一定成功。只有从 R2 恢复到全新临时路径、通过完整性检查并能启动临时实例后，才可将灾备恢复记为通过。

### 6.2 本地快照回滚

先停止写入者和复制进程：

```bash
cd /home/ubuntu/stacks/life_tracker
docker compose stop app litestream
```

保留故障现场，再恢复已验证快照：

```bash
mv data/life_tracker.db data/life_tracker.db.failed-$(date -u +%Y%m%d-%H%M%S)
rm -f data/life_tracker.db-wal data/life_tracker.db-shm
cp data/life_tracker.db.bak-<timestamp> data/life_tracker.db
sqlite3 data/life_tracker.db "PRAGMA integrity_check;"
docker compose up -d app litestream
```

`rm` 仅用于与已经移走的故障数据库配套的 WAL/SHM；执行前必须确认容器已停止且主数据库已经保留。恢复后完成第 3.4 节全部检查。

### 6.3 R2 恢复演练原则

不要直接覆盖 production 数据库。演练必须恢复到 `/tmp` 或专用临时目录：

1. 记录 production 当前时间、数据库大小和最新 Litestream 成功上传时间。
2. 使用与 production 相同版本的 Litestream，从 R2 恢复到全新临时路径。
3. 对恢复文件运行 `PRAGMA integrity_check`。
4. 检查关键表存在、最近数据时间合理，并与预期恢复点比较。
5. 使用隔离配置和未登记临时端口（如需启动服务，必须先 `infra allocate`）进行只读 smoke test。
6. 删除临时实例前记录 RPO、恢复耗时、命令版本和结果到验证台账。

具体 restore 命令依赖当前 `litestream.yml` 和 Litestream 版本。每次演练前先运行 `litestream restore -h` 核对语法，不把未经验证的示例命令直接用于 production。

### 6.4 使用 staging 做隔离恢复演练

Staging 可以用于恢复后的应用级 smoke test，但不要覆盖 `data-dev/`，也不要以完整 bot/scheduler 模式启动恢复库。采用“独立临时目录 + 临时 compose override + `--api-only`”：

- production 的 `data/`、容器和 Litestream 全程不停止、不改写；
- staging 原本的 `data-dev/` 和配置文件不改写；
- 恢复库放在 `/tmp/life-tracker-drill-<timestamp>/`；
- 演练容器挂载恢复库和 `config.dev.json`，但通过 `--api-only` 禁用 Discord 与 scheduler；
- 演练端口必须先用 `infra allocate` 分配，并只绑定 `127.0.0.1`；结束后 `infra unregister`；
- 演练 compose 使用独立 project name，不复用或停止正常 staging 容器。

演练步骤：

1. 记录 production 数据库最新业务时间、文件大小和 Litestream 最近上传时间。
2. 用 Litestream 0.3.13 从 R2 恢复到全新临时目录。
3. 运行 `PRAGMA integrity_check` 并比较关键表及最新时间，计算实际 RPO。
4. 创建临时 compose 文件，挂载恢复目录到 `/app/data`，使用 `config.dev.json`，命令设为 `python main.py --api-only`。
5. 以独立 project 启动一个临时容器，检查 health、version、timeline、memories、todos、projects、prompts 等只读端点；结构化 trace 用 SQLite `ai_runs`/`tool_calls` 验证，不要求 JSONL Trace Viewer 日期恢复。
6. 检查容器日志没有 schema、SQLite 或启动异常。
7. 停止并删除临时容器；确认 production 仍 healthy、Litestream 仍运行、`data/` 与 `data-dev/` 路径未被替换。
8. 将恢复点、RPO、耗时和结果写入 `operations-validation.md`。

临时 compose 的核心约束示例：

```yaml
services:
  app:
    build: /home/ubuntu/stacks/life_tracker
    command: ["python", "main.py", "--api-only"]
    ports:
      - "127.0.0.1:<allocated-port>:8081"
    volumes:
      - /tmp/life-tracker-drill-<timestamp>:/app/data
      - /home/ubuntu/stacks/life_tracker/config.dev.json:/app/config.json:ro
```

如果配置内 `server.port` 不是 8081，端口映射的容器端必须使用配置中的真实端口。演练不测试 Discord、scheduler、AI provider 或写操作；这些属于 staging 功能测试，不属于数据库可恢复性验证。

## 7. 进程与故障排查

```bash
docker compose ps
docker compose logs --tail 200 app
docker compose logs --tail 200 litestream
curl -fsS http://127.0.0.1:8080/internal/health
curl -fsS -H "X-API-Key: $LIFE_TRACKER_API_KEY" http://127.0.0.1:8080/api/health
sqlite3 data/life_tracker.db "PRAGMA quick_check;"
infra overview
infra audit
```

常见问题：

- app unhealthy：检查 `config.json`、数据库权限、端口占用和启动 Traceback。
- Discord token 无效：应用会保留 FastAPI，日志会明确记录 bot 登录失败。
- 主 AI provider 不可用：检查日志是否成功切换 fallback；fallback 成功不等于主 provider 健康。
- Litestream `Up` 但没有近期上传：确认数据库最近是否有写入；有写入却无 `wal segment written` 才是异常信号。
- `infra audit` 与 health 冲突：确认是否因当前 shell 没有 systemd/socket 权限；在正常宿主环境重跑，不能忽略。

## 8. 运维验证频率

| 检查 | 最低频率 |
|---|---|
| pytest、前端构建、`git diff --check` | 每次部署前 |
| app health、容器状态、SQLite quick check | 每次部署后 |
| Litestream 最新成功复制日志 | 每次部署后，至少每月一次 |
| SQLite 在线快照并校验 | 每次高风险部署前 |
| R2 完整恢复演练 | 每季度，或 Litestream/存储配置变更后 |
| 网络监听和公网路由审计 | 每次网络/compose 变更后，至少每月一次 |
| 验证台账更新 | 每次执行上述检查后立即更新 |

## 9. 相关文件

- `compose.yaml`：Dockge 管理的生产权威栈
- `docker-compose.prod.yml`：GHCR 版本化部署
- `docker-compose.yml`：显式选择的本地开发栈
- `/home/ubuntu/stacks/life-tracker-staging/compose.yaml`：staging 权威栈
- `litestream.yml`：R2 复制配置
- `.github/workflows/release.yml`：tag release 构建
- `docs/operations-validation.md`：测试、健康和灾备演练台账
