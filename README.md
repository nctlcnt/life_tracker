# Life Tracker

Single-user, self-hosted life-tracking assistant.

It combines a Discord bot, AI-driven logging, scheduled reminders, SQLite storage, and a React dashboard. You chat with the bot naturally; it records useful events, memories, todos, reminders, and project activity in the background.

## 功能概览

- **Discord 对话记录**：通过自然语言记录日常活动、想法、项目进展和提醒。
- **AI 自动整理**：根据聊天内容判断是否写入 timeline、memory、todo、deadline 或 reminder。
- **主动提醒与调度**：支持随机 check-in、定时提醒、早晚例行提示等。
- **项目追踪**：记录 Focus 事件，按项目展示热力图、周视图和甘特视图。
- **Web Dashboard**：查看 timeline、week view、projects、todos、memories、reminders、AI traces 和 admin 面板。
- **多 AI Provider**：支持 Claude、OpenAI、Gemini 和 relay endpoint，可在运行时切换 preset / fallback。
- **天气辅助**：支持配置默认位置，也可通过 Discord 查询指定地址天气。

## 基础架构

```text
.
├── main.py                 # 单进程入口：启动 Discord bot、scheduler、FastAPI
├── config.py               # 读取 config.json，管理 AI preset 和运行态配置
├── bot/                    # Discord bot、AI dispatch、scheduler、SQLite access、prompts/tools
├── api/                    # FastAPI API 与前端静态资源服务
├── frontend/               # React + Vite + TypeScript dashboard
├── scripts/                # 本地维护、清理、调试脚本
├── docs/                   # 部署和数据库文档
├── data/                   # SQLite 数据与运行态文件，Docker volume 挂载
├── Dockerfile              # 前端构建 + Python runtime 多阶段镜像
├── docker-compose.yml      # 本地 Docker 启动
├── docker-compose.prod.yml # 生产部署
└── docker-compose.staging.yml
```

运行模型：

- 一个 Python 进程通过 `asyncio` 同时运行 Discord bot、scheduler 和 FastAPI。
- FastAPI 提供 REST API，并在生产镜像中服务 `frontend/dist`。
- SQLite 文件保存在 `data/life_tracker.db`；长期个人记忆保存在 `data/memory.md`。
- 配置从 `config.json` 读取；敏感信息不提交到仓库。
- 生产环境可选用 Litestream 将 SQLite 复制到 Cloudflare R2。

## Docker 启动

先准备配置文件：

```bash
cp config.example.json config.json
```

填好 `config.json` 里的 Discord token、用户/频道 ID、AI preset、server port 等字段。

本地启动：

```bash
make dev
```

等容器启动后打开：

```text
http://localhost:8080
```

停止：

```bash
make down
```

查看日志：

```bash
make logs
```

只构建镜像：

```bash
make build
```

生产和 staging 部署细节见 [docs/deploy.md](docs/deploy.md)。

## Prompt 初始化与自定义

Prompt 正文保存在 SQLite 的 `prompt_sections` 表里，不提交到 Git。新安装时，如果表为空，系统会自动从 `docs/default-prompts.json` 导入一份默认 prompt；之后可以在 Admin 里按自己的工作流调整。

备份当前 prompt：

```bash
docker compose exec app python -m scripts.export_prompts
```

备份文件默认写到 `data/backups/prompts/`，会随 Docker volume 持久化，且不会被 Git 跟踪。

恢复到默认 prompt：

```bash
docker compose exec app python -m scripts.import_prompts docs/default-prompts.json --apply
```

从备份恢复：

```bash
docker compose exec app python -m scripts.import_prompts data/backups/prompts/prompts-YYYYMMDDTHHMMSSZ.json --apply
```
