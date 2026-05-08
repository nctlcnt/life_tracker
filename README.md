# Life Tracker

通过 Discord 和 AI 记录日常生活轨迹的个人助手。和它聊天，它会在后台自动记录活动、设置提醒、管理记忆，同时通过网页前端查看时间线。

## 功能

- **Discord 对话**：像和朋友聊天一样，AI 会自动判断是否记录事件
- **时间线记录**：活动按时间轴存储，支持分类、并行事件、进行中状态
- **提醒系统**：AI 主动识别提醒时机，到点推送 Discord 消息
- **记忆管理**：AI 记住重要信息（deadline、偏好等），跨对话保持上下文
- **网页前端**：查看每日时间线、浏览存储的记忆、待办和提醒

## 本地开发

```bash
cp config.example.json config.json      # 填入 Discord Token、AI API Key 等

# 方式一：直接运行（需要本地有 Python 3.12+ 和 Node/pnpm）
pip install -r requirements.txt
cd frontend && pnpm install && pnpm build && cd ..
python main.py

# 开启测试模式（记录所有日志和 AI prompt payload 到 data/test_logs/）
python main.py --test

# 方式二：Docker（推荐，无需本地配置环境）
make dev                  # 等价于 docker compose up --build
```

访问 `http://localhost:8080` 查看前端。

## 配置

所有配置集中在 `config.json`（不进 git、不进镜像，由部署侧挂载）。结构参考 `config.example.json`：

```jsonc
{
  "discord": {
    "token": "...",                  // 必填
    "allowed_user_id": 0             // 必填，单用户模式
  },
  "ai": {
    "default_preset": "claude-opus", // 必填，必须在 presets 里
    "default_fallback": "",          // 可选，主 preset 失败时降级用
    "presets": {                     // 至少一条；运行时用 /model 切换
      "claude-opus": {
        "provider": "claude",        // claude / relay / gemini / openai
        "api_key": "...",
        "base_url": "",              // 仅 relay 需要
        "model": "claude-opus-4-6"
      }
    }
  },
  "server": { "port": 8080 },
  "weather": { "api_key": "", "location": "-33.8688,151.2093" },  // tomorrow.io；空 key 静默降级
  "poll": { "min_seconds": 60, "max_seconds": 3600 },
  "log": { "level": "INFO", "file": null }
}
```

运行时通过 Discord 斜杠命令 `/model`、`/fallback` 在 presets 之间切换，状态持久化到 `data/active_preset.json`。

## 项目结构

```
├── bot/
│   ├── discord_bot.py      # Discord 收发消息
│   ├── ai_engine*.py       # AI 对话 + tool calling（多 provider）
│   ├── scheduler.py        # 随机 check-in + 提醒轮询
│   ├── database.py         # SQLite 操作
│   ├── tools.py            # AI 工具定义
│   ├── prompts.py          # 系统提示词
│   └── test_mode.py        # 测试模式：捕获日志和 AI prompt 到 JSONL 文件
├── api/server.py           # FastAPI REST 接口 + 静态文件托管
├── frontend/               # React + Vite + TypeScript 前端
├── main.py                 # 入口，asyncio.gather 启动所有服务
├── config.py               # 从 config.json 读取配置 + 运行时 preset 切换
└── data/                   # SQLite 数据库 + active_preset 状态（挂载到容器外）
```

## Docker 与版本管理

项目使用多阶段构建：Node 阶段编译前端，Python 阶段运行服务。

### 发布一个新版本

```bash
# 本地测试没问题后，打标签并推送
make release VERSION=v1.0.0
```

GitHub Actions 会自动构建并推送两个镜像标签：

| 标签 | 含义 |
|------|------|
| `ghcr.io/nctlcnt/life_tracker:v1.0.0` | 不可变，永久存档 |
| `ghcr.io/nctlcnt/life_tracker:stable` | 始终指向最新稳定版 |

### 在服务器上部署 / 升级

服务器上直接用 docker compose 即可，不需要 `make`：

```bash
# 跟随 :stable（默认）
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d

# 锁定到具体版本
VERSION=v1.0.0 docker compose -f docker-compose.prod.yml pull
VERSION=v1.0.0 docker compose -f docker-compose.prod.yml up -d

# 出问题了，回滚到旧版本
VERSION=v0.9.0 docker compose -f docker-compose.prod.yml up -d
```

> 也可以用 `make deploy VERSION=v1.0.0`，那只是上面两行的薄封装。

---

## VPS 部署指南

### 前置条件

- 一台运行 Ubuntu 22.04+ 的 VPS
- 域名（可选，用于 HTTPS）
- GitHub 仓库已推送（Actions 从这里触发构建）

---

### 第一步：在 GitHub 上启用包权限

GitHub Actions 会把镜像推送到 **GitHub Container Registry (ghcr.io)**，需要先确认权限：

1. 进入仓库 → **Settings** → **Actions** → **General**
2. 找到 **Workflow permissions**，选择 **Read and write permissions**，保存

仓库是 Private，镜像也会是私有的，VPS 拉取镜像前需要先登录。

生成一个只读的 Personal Access Token（PAT）：

1. GitHub → **Settings** → **Developer settings** → **Personal access tokens** → **Fine-grained tokens**
2. **Generate new token**，Repository access 选 **Only select repositories** → 选这个仓库
3. Permissions 里只勾 **Packages** → **Read-only**
4. 生成后复制 token（只显示一次）

---

### 第二步：发布第一个版本

在本地执行：

```bash
# 确保所有改动已 commit 并 push 到 main
git push origin main

# 打标签，触发 GitHub Actions 构建镜像
make release VERSION=v1.0.0
```

去 GitHub → **Actions** 页面确认 workflow 运行成功（约 2-5 分钟）。

---

### 第三步：在 VPS 上安装 Docker 并登录镜像仓库

SSH 进入服务器后执行：

```bash
# 安装 Docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker                          # 或重新登录使权限生效

# 验证
docker --version
docker compose version

# 登录 ghcr.io（用第一步生成的 PAT，凭证会保存在 ~/.docker/config.json）
echo "你的_PAT" | docker login ghcr.io -u 你的GitHub用户名 --password-stdin
```

---

### 第四步：上传配置文件

在 VPS 上创建项目目录：

```bash
mkdir -p ~/life-tracker/data
cd ~/life-tracker
```

只需要两个文件落到服务器：`docker-compose.prod.yml`（描述容器怎么跑）和 `config.json`（含密钥，运行时挂载进容器）。最简方式是直接 clone 仓库，再把本地填好的 `config.json` 拷上去：

```bash
# 服务器上
git clone git@github.com:nctlcnt/life_tracker.git ~/life-tracker
cd ~/life-tracker
mkdir -p data

# 本地另开终端，把填好密钥的 config.json scp 过去
scp config.json user@your-server:~/life-tracker/config.json
```

或者在服务器上 `cp config.example.json config.json` 然后 `nano config.json` 直接填。

> **必须存在 `~/life-tracker/config.json` 再起容器**。compose 把它以只读方式挂载到 `/app/config.json`，文件不存在 docker 反而会把它当成目录创建，启动会更乱。

---

### 第五步：部署

```bash
cd ~/life-tracker

# 拉取镜像并启动
VERSION=v1.0.0 docker compose -f docker-compose.prod.yml up -d

# 查看状态
docker compose -f docker-compose.prod.yml ps

# 查看日志
docker compose -f docker-compose.prod.yml logs -f
```

访问 `http://你的服务器IP:8080` 确认服务正常。

---

### 第六步（可选）：用 Nginx + HTTPS 反代

如果你有域名，可以用 Nginx 做反向代理并通过 Certbot 申请证书：

```bash
sudo apt install -y nginx certbot python3-certbot-nginx
```

创建 Nginx 配置 `/etc/nginx/sites-available/life-tracker`：

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

# 申请 HTTPS 证书（自动修改 Nginx 配置）
sudo certbot --nginx -d your-domain.com
```

完成后访问 `https://your-domain.com`。

---

### 日常运维

```bash
# 升级到新版本
VERSION=v1.1.0 docker compose -f docker-compose.prod.yml pull
VERSION=v1.1.0 docker compose -f docker-compose.prod.yml up -d

# 回滚
VERSION=v1.0.0 docker compose -f docker-compose.prod.yml up -d

# 查看日志
docker compose -f docker-compose.prod.yml logs -f --tail=100

# 手动备份数据库
cp ~/life-tracker/data/life_tracker.db ~/life-tracker/data/life_tracker.db.bak

# 重启服务
docker compose -f docker-compose.prod.yml restart
```

服务器重启后容器会自动拉起（`restart: unless-stopped`）。
