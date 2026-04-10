# Life Tracker

通过 Discord 和 AI 记录日常生活轨迹的个人助手。和它聊天，它会在后台自动记录活动、设置提醒、管理记忆，同时通过网页前端查看时间线。

## 功能

- **Discord 对话**：像和朋友聊天一样，AI 会自动判断是否记录事件
- **时间线记录**：活动按时间轴存储，支持分类、并行事件、进行中状态
- **提醒系统**：AI 主动识别提醒时机，到点推送 Discord 消息
- **记忆管理**：AI 记住重要信息（deadline、偏好等），跨对话保持上下文
- **网页前端**：查看每日时间线、浏览存储的记忆和提醒

## 快速开始

```bash
cp .env.example .env      # 填入 Token 和 API Key
pip install -r requirements.txt
python main.py
```

访问 `http://localhost:8080` 查看前端。

## 环境变量

| 变量 | 说明 |
|------|------|
| `DISCORD_TOKEN` | Discord Bot Token |
| `AI_API_KEY` | OpenAI 或兼容 API 的密钥 |
| `AI_MODEL` | 模型名称，默认 `gpt-4` |
| `ALLOWED_USER_ID` | Discord 用户 ID（单用户模式） |
| `API_PORT` | FastAPI 端口，默认 `8080` |

## 项目结构

```
├── bot/
│   ├── discord_bot.py   # Discord 收发消息
│   ├── ai_engine.py     # AI 对话 + tool calling
│   ├── scheduler.py     # 随机 check-in + 提醒轮询
│   ├── database.py      # SQLite 操作
│   └── tools.py         # AI 工具定义 + 系统提示词
├── api/server.py        # FastAPI REST 接口
├── frontend/index.html  # 网页前端
└── main.py              # 入口，asyncio.gather 启动所有服务
```

## Docker

```bash
docker build -t life-tracker .
docker run -e DISCORD_TOKEN=... -e AI_API_KEY=... life-tracker
```
