# 云上部署 / 升级

线上 life-tracker 跑在一台 VPS 上的 docker compose 里，镜像从 GitHub Container Registry 拉取。本文是**日常升级流程**——首次安装服务器、装 docker、登录 ghcr、配 nginx + HTTPS 等一次性步骤见 [README 的 VPS 部署指南](../README.md#vps-部署指南)。

## 部署纪律：staging first

**任何未发布的工作树改动只能先上 8081 staging 测试，不能直接部署到 8080 prod。**

固定规则：

1. 本地/服务器工作树改动 → `docker compose -f docker-compose.staging.yml up -d --build`
2. 在 `http://<server>:8081/app/` 验证功能和 API。
3. 验证通过后，走 release：`make release VERSION=vX.Y.Z`，等 GitHub Actions 推送 GHCR 镜像。
4. prod 只用 registry release 镜像升级：`make deploy VERSION=vX.Y.Z`。

不要把 `docker-compose.local.yml` 或 `make deploy-local` 用在 prod 日常发布上。它会从当前工作树构建 `life-tracker:local` 并重启 8080，绕过 tag / CI / GHCR，容易把未验证的 dev 代码打到生产。

## 镜像与版本

`docker-compose.prod.yml` 里 image 字段是：

```yaml
image: ghcr.io/nctlcnt/life_tracker:${VERSION:-stable}
```

每次 `make release VERSION=vX.Y.Z` 后，GitHub Actions 同时 push 两个 tag：

| Tag | 含义 |
|---|---|
| `:vX.Y.Z` | 不可变，永久存档 |
| `:stable` | 始终指向最新稳定版 |

**生产推荐 pin 显式版本**——出问题时回滚明确，不会因为下次 `pull` 自动滚版。在部署目录下放一个 `.env.prod`：

```bash
echo "VERSION=v1.3.0" > .env.prod
```

后续 compose 命令统一加 `--env-file .env.prod`，或直接用 Makefile 提供的 `make deploy VERSION=...`。

## 升级（routine）

### 1. 备份数据库

Litestream 的 R2 复制是流式的，本地再多一份文件级快照保险：

```bash
cd ~/life-tracker
cp data/life_tracker.db data/life_tracker.db.bak-$(date +%Y%m%d-%H%M%S)
```

### 2. 切版本

前置条件：对应版本已经在 staging 验证过，并且 GHCR 上已有该 release image。

最简方式（推荐）：用 Makefile

```bash
make deploy VERSION=v1.3.0
```

等价于：

```bash
VERSION=v1.3.0 docker compose -f docker-compose.prod.yml pull
VERSION=v1.3.0 docker compose -f docker-compose.prod.yml up -d
```

只 `app` 容器会被重建，`litestream` 持续运行。

### 3. 验证

```bash
# 容器状态：app 应该是 Up X seconds (healthy)
docker compose -f docker-compose.prod.yml ps

# 启动日志：找 FastAPI / Discord bot 启动消息，无 Traceback
docker compose -f docker-compose.prod.yml logs --tail 80 app

# Health endpoint
curl -sf http://localhost:8080/api/health && echo OK

# 浏览器打开前端确认 UI 正常；Network 面板检查关键资源 200
```

启动到 `(healthy)` 通常 15-30 秒（compose healthcheck 间隔 30s，start_period 15s）。

## 回滚

发现新版有问题，直接切回上一个稳定版本：

```bash
make deploy VERSION=v1.2.0
```

如果新版动过 DB 且回滚后异常，恢复刚才的备份：

```bash
docker compose -f docker-compose.prod.yml down
cp data/life_tracker.db.bak-<timestamp> data/life_tracker.db
make deploy VERSION=v1.2.0
```

需要注意：恢复 DB 会丢掉新版运行期间产生的数据。如果 Litestream 已经把这段数据流到 R2，可以等 R2 那份冷却几分钟再决定如何 reconcile。

## 清理

升级稳定后清掉 dangling 镜像腾空间：

```bash
docker image prune -f
```

如果想保留旧版 layer 做热回滚（再次 pull 不需重新拉网络），跳过这步。

## 故障排查

### `pull` 报 `unauthorized` / `denied`

ghcr 是私有仓库。第一次部署或 PAT 过期时需要重新登录：

```bash
echo "<你的_PAT>" | docker login ghcr.io -u <你的GitHub用户名> --password-stdin
```

PAT 在 GitHub → Settings → Developer settings → Personal access tokens 生成，scope 只勾 **read:packages**。

### `up -d` 后容器一直 `unhealthy`

```bash
docker compose -f docker-compose.prod.yml logs app --tail 200
```

常见原因：
- `config.json` 没挂上或写错（Discord token、AI key 缺失，启动报错）
- `data/` 目录权限不对（容器内是 root，宿主一般也是 root，本来不该出问题；如果你换过用户登录服务器再 pull，可能要 `sudo chown` 一下）
- 8080 端口被占（`sudo lsof -i :8080`）

### 镜像构建失败（GitHub Actions）

去 GitHub Actions 页面看 workflow log。常见：tag 写错（必须 `vX.Y.Z` 三段式），或者 PAT 权限不够。本地补救：

```bash
git tag -d v1.3.0          # 删本地
git push origin :v1.3.0    # 删远端
make release VERSION=v1.3.1
```

## 参考

- [Makefile](../Makefile) — `release` / `deploy` 两个 target 的定义
- [docker-compose.prod.yml](../docker-compose.prod.yml) — 生产环境 compose 文件
- [.github/workflows/release.yml](../.github/workflows/release.yml) — CI 构建配置
- [README — VPS 部署指南](../README.md#vps-部署指南) — 首次安装 VPS / Docker / nginx / HTTPS
