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
cp .env.example .env      # 填入 Token 和 API Key

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

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `DISCORD_TOKEN` | Discord Bot Token | 必填 |
| `ALLOWED_USER_ID` | Discord 用户 ID（单用户模式） | 必填 |
| `AI_PROVIDER` | AI 提供商：`claude` / `relay` / `gemini` | `claude` |
| `AI_API_KEY` | 对应提供商的 API Key | 必填 |
| `CHAT_MODEL` | 对话模型 | `claude-opus-4-6` |
| `POLL_MODEL` | 轮询模型 | `claude-3-5-sonnet-latest` |
| `AI_BASE_URL` | 中转站地址（仅 `relay` 模式） | — |
| `API_PORT` | FastAPI 端口 | `8080` |
| `LOG_LEVEL` | 日志级别 | `INFO` |

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
├── config.py               # 从环境变量读取配置
└── data/                   # SQLite 数据库（挂载到容器外）
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
| `ghcr.io/chachaya/life-tracker:v1.0.0` | 不可变，永久存档 |
| `ghcr.io/chachaya/life-tracker:stable` | 始终指向最新稳定版 |

### 在服务器上部署 / 升级

```bash
make deploy VERSION=v1.0.0   # 拉取指定版本并重启

# 升级
make deploy VERSION=v1.1.0

# 出问题了，回滚
make deploy VERSION=v1.0.0
```

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

在 VPS 上创建项目目录并上传必要文件：

```bash
mkdir -p ~/life-tracker/data
cd ~/life-tracker
```

从本地上传（在本机执行）：

```bash
scp docker-compose.prod.yml Makefile .env.example user@your-server:~/life-tracker/
```

或者直接在服务器上创建 `.env`：

```bash
# 在服务器上
cd ~/life-tracker
cp .env.example .env
nano .env                              # 填入真实的 Token 和 API Key
```

`.env` 最少需要填写：

```bash
DISCORD_TOKEN=你的_discord_bot_token
AI_API_KEY=你的_api_key
ALLOWED_USER_ID=你的_discord_user_id
```

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
