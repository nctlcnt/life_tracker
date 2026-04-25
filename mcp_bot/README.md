# mcp_bot — Bot B（MCP-based 查询入口）

并行 Discord application（独立于现有 Bot A），通过 MCP 协议读 Obsidian vault + life-tracker SQLite。当前阶段 **Phase 1 MVP：只读、单次查询、无对话历史**。

> Spec: `plans/2-specs/mcp-bot.md`
> Todo: `plans/3-todos/mcp-bot-phase1.md`
> Bot A（现有 Discord bot）保持原样、互不影响。

## 当前实施状态

- [x] Step 0 — `mcp>=1.0.0` 已装
- [x] Step 1 — `bot/database.py` 开 WAL；`config.py` 加 `MCP_BOT_TOKEN` / `MCP_BOT_MODEL` / `OBSIDIAN_VAULT_PATH`
- [x] Step 2 — `obsidian_search.py`（纯函数）
- [x] Step 3 — `obsidian_mcp_server.py`（MCP server #1）
- [x] Step 4 — `lifetracker_mcp_server.py`（MCP server #2）
- [ ] Step 5 — `mcp_client.py`（多 server stdio client 包装）
- [ ] Step 6 — `prompts.py`
- [ ] Step 7 — `agent.py`（Anthropic SDK + tool_use 循环）
- [ ] Step 8 — `discord_client.py`（DM-only）
- [ ] Step 9 — `main.py`（串起来）

## 架构

```
Discord (DM)
    │
    ▼
mcp_bot.discord_client  ──→  mcp_bot.agent  ──→  Anthropic Messages API
                                │
                                ├─ stdio ──→  mcp_bot.obsidian_mcp_server  ──→  Obsidian vault
                                └─ stdio ──→  mcp_bot.lifetracker_mcp_server  ──→  bot.database (read-only)
```

两个 MCP server 是 main.py 的子进程；agent 用 mcp_client 路由到正确 server。

## 文件清单

| 文件 | 角色 |
|---|---|
| `obsidian_search.py` | 纯函数：search_notes / read_note，路径穿越防护 |
| `obsidian_mcp_server.py` | MCP server #1（stdio），暴露 obsidian 工具 |
| `lifetracker_mcp_server.py` | MCP server #2（stdio），暴露 9 个 DB 只读工具，import `bot.database` |
| `mcp_client.py` | TODO：包装 mcp SDK，spawn 两 server，统一 list_tools / call_tool |
| `agent.py` | TODO：Anthropic API + tool_use 循环；user message 头注入当前时间 |
| `discord_client.py` | TODO：DM-only（`channel.type == private` + `author.id == ALLOWED_USER_ID`） |
| `prompts.py` | TODO：极简 system prompt（查询助手定位） |
| `main.py` | TODO：asyncio entry，启 Discord + spawn MCP server |

## 配置依赖

`config.json` 必须存在：

```json
"mcp_bot": {
  "discord_token": "<Bot B application token>",
  "model": "claude-sonnet-4-6"
},
"obsidian": {
  "vault_path": "/path/to/your/vault"
}
```

`allowed_user_id` 复用顶层 `discord.allowed_user_id`。

**Discord Developer Portal** 必须做的事：
- 该 application **Bot tab** 勾选 **MESSAGE CONTENT INTENT**（privileged）
- Bot 至少加入一个 server（Discord 强制要求 bot 在 ≥1 个 server 才能被 DM）
- 不需要 SERVER MEMBERS / PRESENCE intent

## 启动方式（最终）

```bash
# 终端 A：Bot A 照常
python main.py

# 终端 B：Bot B（step 9 完成后才能用）
python -m mcp_bot.main
```

两个进程独立、互不依赖。任一退出不影响另一个。

## 当前可跑的本地验证

下述命令在仓库根目录执行（venv 已激活或用绝对路径调 venv python）。

### 1. WAL 模式确认

```bash
.venv/bin/python -c "
from bot.database import Database
import config
db = Database(config.DB_PATH)
mode = db._get_conn().execute('PRAGMA journal_mode').fetchone()[0]
print(mode)
"
# 期望输出：wal
```

### 2. obsidian_search 纯函数

```bash
.venv/bin/python -c "
from mcp_bot.obsidian_search import search_notes
import config
print(len(search_notes(config.OBSIDIAN_VAULT_PATH, '')), 'items in vault')
"
```

### 3. obsidian_mcp_server 端到端（stdio + mcp client）

```bash
.venv/bin/python <<'EOF'
import asyncio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    params = StdioServerParameters(
        command=".venv/bin/python",
        args=["-m", "mcp_bot.obsidian_mcp_server"],
    )
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            print([t.name for t in (await s.list_tools()).tools])

asyncio.run(main())
EOF
# 期望：['search_notes', 'read_note']
```

### 4. lifetracker_mcp_server 端到端

```bash
.venv/bin/python <<'EOF'
import asyncio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    params = StdioServerParameters(
        command=".venv/bin/python",
        args=["-m", "mcp_bot.lifetracker_mcp_server"],
    )
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            print([t.name for t in (await s.list_tools()).tools])
            r = await s.call_tool("weekly_summary", {})
            print(r.content[0].text[:200])

asyncio.run(main())
EOF
# 期望：9 个工具 + weekly_summary 真实输出
```

### 5. Bot B Discord 登录（不开 message_content）

```bash
.venv/bin/python <<'EOF'
import asyncio, discord, config
async def main():
    c = discord.Client(intents=discord.Intents.default())
    @c.event
    async def on_ready():
        print(f"ok: {c.user} guilds={[g.name for g in c.guilds]}")
        await c.close()
    await asyncio.wait_for(c.start(config.MCP_BOT_TOKEN), timeout=15)
asyncio.run(main())
EOF
```

## MCP 工具速查

### Obsidian server (`obsidian`)

| Tool | 入参 | 说明 |
|---|---|---|
| `search_notes` | query, folder?, tags?, max_results? | 大小写不敏感全文检索 + 标题/folder/tags 过滤 |
| `read_note` | path | 读取笔记，截断至 3000 字 |

### Lifetracker server (`lifetracker`)

| Tool | 入参 | 说明 |
|---|---|---|
| `query_events` | start (ISO), end (ISO) | 时间段事件 |
| `query_ongoing_events` | limit? | end_time IS NULL 的真实事件 |
| `query_planned_events` | — | 未来 planned events |
| `list_memories` | — | 全部 memory |
| `list_active_deadlines` | — | 未完成 deadline |
| `list_todos` | include_done? | todo 列表 |
| `list_categories` | — | DISTINCT category |
| `list_project_names` | — | Focus 类项目名 + 计数 |
| `weekly_summary` | — | 最近 7 天聚合（by_category/by_project/top_events） |

## 故障排查

| 症状 | 原因 / 修法 |
|---|---|
| `PrivilegedIntentsRequired` | Discord Developer Portal 没开 MESSAGE CONTENT INTENT |
| `LoginFailure` | token 错或被 reset，去 Developer Portal → Bot → Reset Token |
| MCP server 启动后立刻退出 | 检查 cwd 是否在 repo 根（不在则 `import config` 找不到 `config.json`） |
| MCP server 报 `ModuleNotFoundError: mcp` | 用了系统 python 而非 `.venv/bin/python` |
| 两个 bot 同时跑时报 `database is locked` | WAL 没生效（少见）；确认 `bot/database.py::_get_conn` 有 `PRAGMA journal_mode=WAL` |
| Bot B DM 收不到内容（content 为空） | 同 #1：MESSAGE CONTENT INTENT 未开 |
| 笔记搜不到任何东西 | `config.OBSIDIAN_VAULT_PATH` 配错；`config.OBSIDIAN_VAULT_PATH` 路径下没有 `.md` 文件 |

## 设计决策快查

跟 Bot A 的差异为什么是这些（详见 spec）：

- **手写 agent loop** 而非 Claude Agent SDK：复用 `bot/ai_engine_claude` 模式，零新黑盒依赖
- **直接 `from bot.database import`**：DB schema 是真实边界，import = 复用读 API，避免 schema 漂移
- **两个 MCP server** 而不是合并：obsidian server 长期可独立给 Claude Code CLI 用
- **DM-only**：用户决定走私聊，不开新频道
- **每条消息独立、无 history**：用户明确"只是查询入口"
- **当前时间注入 user message 头部**而非 system prompt：每次都不同，不破坏 prompt cache
