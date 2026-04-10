# Life Tracker Bot

一个基于 Discord + AI 的生活轨迹记录系统。

## 架构

```
Discord ↔ Python进程(Bot + AI + DB + API) ↔ React前端
```

## 项目结构

```
life-tracker/
├── bot/
│   ├── __init__.py
│   ├── discord_bot.py    # Discord 机器人：收发消息
│   ├── ai_engine.py      # AI 引擎：调用大模型 + tool calling
│   ├── scheduler.py      # 定时调度：随机轮询 + 提醒队列
│   ├── database.py       # 数据库：SQLite 操作
│   └── tools.py          # AI 可调用的工具定义
├── api/
│   └── server.py         # FastAPI 接口：给前端提供数据
├── frontend/             # React 前端（单独部署）
│   └── (稍后搭建)
├── main.py               # 入口：启动所有模块
├── config.py             # 配置：Token、API Key 等
├── requirements.txt
├── Dockerfile
└── .env.example
```

## 快速开始

1. 复制 `.env.example` 为 `.env`，填入你的 Token
2. `pip install -r requirements.txt`
3. `python main.py`

## 环境变量

- `DISCORD_TOKEN` - Discord Bot Token
- `AI_API_KEY` - AI 模型 API Key (OpenAI / Anthropic)
- `AI_MODEL` - 使用的模型名称
- `ALLOWED_USER_ID` - 你的 Discord 用户 ID（只响应你的消息）
