# 云上部署 / 升级

线上 life-tracker 跑在一台 VPS 上的 docker compose 里，镜像从 GitHub Container Registry 拉取。本文涵盖**首次安装**、**日常升级流程**和**云上 staging 环境**。

## 首次安装

### 前置条件

- 一台运行 Ubuntu 22.04+ 的 VPS
- 域名（可选，用于 HTTPS）
- GitHub 仓库已推送（Actions 从这里触发构建）

### 1. 在 GitHub 上启用包权限

GitHub Actions 把镜像推送到 **GitHub Container Registry (ghcr.io)**：

1. 仓库 → **Settings** → **Actions** → **General** → **Workflow permissions** 选 **Read and write permissions**，保存
2. 仓库为 Private 时，镜像也是私有的，VPS 拉取前需要 PAT：
   - **Settings** → **Developer settings** → **Personal access tokens** → **Fine-grained tokens**
   - **Generate new token**，Repository access 选这个仓库
   - Permissions 只勾 **Packages** → **Read-only**
   - 复制 token（只显示一次）

### 2. 发布第一个版本

```bash
# 本地，确保所有改动已 commit/push
git push origin main

# 打 tag 触发 GitHub Actions 构建
make release VERSION=v1.0.0
```

到 GitHub → **Actions** 页面确认 workflow 跑通（约 2-5 分钟）。

### 3. 在 VPS 上安装 Docker 并登录镜像仓库

SSH 进服务器后：

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker

docker --version
docker compose version

# 用第一步生成的 PAT 登录 ghcr，凭证保存在 ~/.docker/config.json
echo "<你的_PAT>" | docker login ghcr.io -u <你的GitHub用户名> --password-stdin
```

### 4. 上传 compose 与配置

```bash
# 服务器上 clone 仓库
git clone git@github.com:nctlcnt/life_tracker.git ~/life-tracker
cd ~/life-tracker
mkdir -p data

# 本地另开终端，把填好密钥的 config.json 拷过去
scp config.json user@your-server:~/life-tracker/config.json
```

或者服务器上 `cp config.example.json config.json` 后 `nano config.json` 直接填。

> **必须先放好 `~/life-tracker/config.json` 再起容器。** Compose 会以只读方式挂载到 `/app/config.json`，文件不存在 docker 会把它当目录创建，启动反而更乱。

### 5. 起容器

```bash
cd ~/life-tracker
echo "VERSION=v1.0.0" > .env.prod      # 锁定版本
docker compose --env-file .env.prod -f docker-compose.prod.yml up -d
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml logs -f
```

访问 `http://你的服务器IP:8080` 验证。

### 6. （可选）Nginx + HTTPS 反代

```bash
sudo apt install -y nginx certbot python3-certbot-nginx
```

`/etc/nginx/sites-available/life-tracker`：

```nginx
server {
    listen 80;
    server_name your-domain.com;

    location / {
        proxy_pass         http://127.0.0.1:8080;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/life-tracker /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d your-domain.com    # 自动改 nginx 配置申请证书
```

完成后访问 `https://your-domain.com`。服务器重启后容器会自动拉起（`restart: unless-stopped`）。

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

## Staging 环境（同机 8081）

服务器上同时跑一个独立的 staging stack 验证未发布的改动，不影响 prod。

**前置**：在 [Discord Developer Portal](https://discord.com/developers/applications) 申请第二个 Application & Bot Token 作为测试 bot——同一 token 不能在两个进程同时跑，共用 bot 也会让真实聊天和测试输出混在一起。

### 一次性设置

```bash
cd ~/life-tracker
git pull

# 1. 准备测试 config（独立 token + 独立端口）
cp config.json config.dev.json
nano config.dev.json
```

至少改三处：
- `discord.token` → 第二个 bot 的 token
- `discord.allowed_user_id` → 你自己（可与 prod 一致）
- `server.port` → `8081`

建议改：
- `ai.presets` 切到便宜模型（Haiku / Gemini Flash 等），dev 聊天不烧 prod 配额
- 测试 bot 邀请到独立服务器或单独频道，不要和 prod bot 同频

```bash
# 2. 准备独立数据目录（dev SQLite 不被 Litestream 同步到 R2）
mkdir -p data-dev
```

`config.dev.json` 和 `data-dev/` 都在 `.gitignore` / `.dockerignore`，不会进 git 也不会进镜像。

### 启停

```bash
# 启动 / 重新构建（改了源码后用）
docker compose -f docker-compose.staging.yml up -d --build

# 看日志
docker compose -f docker-compose.staging.yml logs -f app

# 改了 Python / dist 后只需要 restart（compose 已 bind-mount 源码）
docker compose -f docker-compose.staging.yml restart app

# 停止
docker compose -f docker-compose.staging.yml down
```

Staging stack 通过 compose project name `life-tracker-staging` 与 prod 隔离，互不影响。前端访问 `http://你的IP:8081`。

### 与 prod 的隔离边界

| 资源 | prod | staging |
|---|---|---|
| Discord Bot | bot1（`config.json`） | bot2（`config.dev.json`） |
| 端口 | 8080 | 8081 |
| 数据库 | `data/life_tracker.db` | `data-dev/life_tracker.db` |
| Litestream（→ R2 备份） | 跑 | 不跑 |
| Compose project | 默认（目录名） | `life-tracker-staging` |
| 镜像来源 | `ghcr.io/...:vX.Y.Z`（不可变） | 本地 `build: .`（每次从源码构建） |

## 参考

- [Makefile](../Makefile) — `release` / `deploy` 两个 target 的定义
- [docker-compose.prod.yml](../docker-compose.prod.yml) — 生产环境 compose 文件
- [docker-compose.staging.yml](../docker-compose.staging.yml) — staging 环境 compose 文件
- [.github/workflows/release.yml](../.github/workflows/release.yml) — CI 构建配置
