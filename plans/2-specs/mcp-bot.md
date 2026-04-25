# Plan: MCP-based Bot B（并行只读查询入口）

## Context

现有 Discord bot（下称 **Bot A**，`bot/discord_bot.py` + scheduler + ai_engine）保持原样不动。新增一个独立的 Discord application（下称 **Bot B**），作为只读查询入口，通过 MCP 协议访问：

- Obsidian vault（全 markdown，PDF/课件已人工总结后入库）
- 现有 SQLite 数据库的只读视图（events / memories / deadlines / todos）

Bot B 不做：多轮对话、scheduler、reminder、写数据库、proactive 主动联系。

**长期方向**：Bot B 逐步建立起 Bot A 的等价功能（含写能力），最终二选一或职责分化。本 spec 只覆盖 **Phase 1 MVP（只读查询）**。

## 关键决策（已与用户对齐）

| # | 决策 | 取值 | 理由 |
|---|---|---|---|
| 1 | Agent 循环实现 | 手写 loop（Anthropic SDK + mcp Python SDK 做 client） | 复用现有 `ai_engine_claude` 模式，0 新黑盒依赖 |
| 2 | `obsidian_search.py` 位置 | `mcp_bot/` 内独立 | YAGNI；旧 spec `obsidian-claude-code.md` 还没实施，不提前抽公共模块 |
| 3 | 代码位置 | 同 repo，`mcp_bot/` 子模块，独立 entry | 复用 `config.py` / `bot/logger.py`，但启停完全独立 |
| 4 | SQLite 访问方式 | `mcp_bot` 直接 `from bot.database import Database` 调读方法 | DB schema 是真实边界，import 等于复用现有读 API，避免 schema 漂移 |
| 5 | MCP server 数量 | 2 个（`obsidian_mcp_server.py` + `lifetracker_mcp_server.py`） | Obsidian server 长期可独立给 Claude Code CLI 用；DB server 项目专属 |
| 6 | 多轮对话 | 不支持，每条消息独立 | 用户明确"只是查询入口" |

## 前置必做：开 WAL 模式

**修改 `bot/database.py::_get_conn()`**（这是本方案对现有代码的唯一改动）：

```python
def _get_conn(self) -> sqlite3.Connection:
    conn = sqlite3.connect(self.db_path)
    conn.execute("PRAGMA journal_mode=WAL")  # 新增：允许并发读不阻塞写
    conn.row_factory = sqlite3.Row
    return conn
```

WAL 模式让 Bot A 的写不阻塞 Bot B 的读，且 reader 看到一致 snapshot。第一次启动会在 db 文件旁生成 `-wal` / `-shm` 文件，正常现象。

**重要语义**：`journal_mode=WAL` 是**持久化文件属性**，一次设置后在 DB 文件本身留存，所以 Bot A 之后所有连接也自动是 WAL 模式（无需在 Bot A 单独开启）。idempotent，重复 PRAGMA 无副作用。

> **Phase 2 风险预告**：若 Bot B 也要写，WAL 仍只允许一个 writer。届时需引入显式锁或单 writer 进程模型，本 spec 不展开。

## 文件结构

```
mcp_bot/
├── __init__.py
├── main.py                       # asyncio entry：起 Discord client + spawn 两个 MCP server 子进程
├── discord_client.py             # 收消息 → agent.run(text) → 单条回复
├── agent.py                      # Anthropic SDK + tool_use 循环 + 多 server MCP client
├── mcp_client.py                 # 包装 mcp SDK：连接两个 stdio server，统一 list_tools / call_tool
├── obsidian_search.py            # search_notes / read_note（纯函数）
├── obsidian_mcp_server.py        # MCP server：暴露 obsidian 工具
├── lifetracker_mcp_server.py     # MCP server：暴露 DB 只读工具（import bot.database）
└── prompts.py                    # 极简 system prompt
```

启动：`python -m mcp_bot.main`，与 `python main.py` 完全独立。

## 模块详述

### `mcp_bot/obsidian_search.py`

纯 Python，无外部依赖（标准库 `pathlib` / `re` 即可）。

- `search_notes(vault_path, query, folder=None, tags=None, max_results=8) -> list[dict]`
  - 遍历 vault 下所有 `.md`
  - 解析 YAML frontmatter 的 `tags` 字段（用 `re`，不引入 PyYAML）
  - 过滤：`folder`（路径前缀）、`tags`（必须全包含）
  - 大小写不敏感关键词匹配标题 + 内容
  - 返回 `[{path, title, tags, snippet, matched_lines}]`，snippet 取匹配行前后约 200 字
- `read_note(vault_path, relative_path) -> dict`
  - 完整读文件，最多 3000 字（截断防止吃爆上下文）
  - 返回 `{path, title, content, tags}`

### `mcp_bot/obsidian_mcp_server.py`

- 依赖 `mcp>=1.0.0`
- 从 `config.py::OBSIDIAN_VAULT_PATH` 读 vault 路径
- 暴露两个 MCP tool：`search_notes`、`read_note`
- 入口：`if __name__ == "__main__": mcp.run(transport="stdio")`

### `mcp_bot/lifetracker_mcp_server.py`

- 实例化 `Database(config.DB_PATH)`，**只调用读方法**
- 暴露的 MCP 工具（每个映射到一个 `database.py` 方法）：

| MCP tool | 入参 | 底层调用 |
|---|---|---|
| `query_events` | `start: ISO8601`, `end: ISO8601` | `db.get_events(start, end)` |
| `query_ongoing_events` | （无） | `db.get_ongoing_events()` |
| `query_planned_events` | （无） | `db.get_planned_events()` |
| `list_memories` | （无） | `db.get_all_memories()` |
| `list_active_deadlines` | （无） | `db.get_active_deadlines()` |
| `list_todos` | `include_done: bool = False` | `db.get_todos(include_done)` |
| `list_categories` | （无） | `db.get_all_categories()` |
| `list_project_names` | （无） | `db.get_all_project_names()` |
| `weekly_summary` | （无） | 内部组合：取过去 7 天 `get_events` + 按 category/project 聚合 |

`weekly_summary` 是语义糖，专门服务"我近一周做了什么"这类高频问句，省得 agent 每次都自己组合 + 让回复更稳。聚合输出形如：

```json
{
  "range": ["2026-04-18T00:00:00", "2026-04-25T00:00:00"],
  "by_category": {"Focus": "32h", "Routine": "12h", "Chill": "8h"},
  "by_project": {"life-tracker": "18h", "COMP9417": "9h", ...},
  "top_events": [{title, start, duration, category, project_name}, ...]
}
```

### `mcp_bot/mcp_client.py`

包装 `mcp` SDK 的 client。职责：

- 启动时 spawn 两个 MCP server 子进程（stdio）
- 拉取并合并两个 server 的工具列表，转换为 Anthropic `tools` schema 格式
- 提供 `call_tool(name, args)` 路由到正确 server
- 退出时清理子进程

### `mcp_bot/agent.py`

照搬 `bot/ai_engine_claude.py::_call_with_tools` 的循环骨架，简化为：

```python
async def run(user_text: str) -> str:
    # 注入当前时间，让 agent 能正确解析"上周/今天/最近三天"
    now_iso = datetime.now().astimezone().isoformat(timespec="seconds")
    messages = [{
        "role": "user",
        "content": f"[当前时间: {now_iso}]\n{user_text}",
    }]
    final_text_chunks = []
    for _ in range(MAX_TOOL_ROUNDS):  # MAX_TOOL_ROUNDS = 6 起步
        resp = await anthropic.messages.create(
            model=config.MCP_BOT_MODEL,  # 默认 claude-sonnet-4-6
            system=SYSTEM_PROMPT,
            tools=mcp_client.tools_schema,
            messages=messages,
            max_tokens=2048,
        )
        for block in resp.content:
            if block.type == "text":
                final_text_chunks.append(block.text)
            elif block.type == "tool_use":
                result = await mcp_client.call_tool(block.name, block.input)
                # append assistant + tool_result, 进下一轮
        if resp.stop_reason != "tool_use":
            break
    return "\n".join(final_text_chunks)
```

**时间注入决策**：放在 user message 头部而非 system prompt，因为每次调用都不同——若塞 system prompt 会破坏 prompt cache 命中（虽然 Bot B 暂未启用 cache，但这是默认良好实践）。

不做：流式输出、分块发送、reaction 触发逻辑（这些是 Bot A 的复杂度）。

### `mcp_bot/discord_client.py`

照搬 `bot/discord_bot.py` 但精简：

- **只接受私聊（DM）**：`message.channel.type == discord.ChannelType.private`
- **只接受授权用户**：`message.author.id == config.ALLOWED_USER_ID`（复用现有顶层配置）
- 上面两条不满足直接 ignore（不报错、不回复）
- `on_message`：直接 `agent.run(message.content)` → `message.channel.send(reply)`
- 无 message history、无 reaction、无 scheduler、无 typing indicator

**为何只走 DM**：用户决定用私聊，不再开新频道。这意味着 Bot B 不需要 `channel_id` 配置；任何服务器频道里的提及都不响应（即便有人 invite 到群）。

### `mcp_bot/prompts.py`

```python
SYSTEM_PROMPT = """你是一个查询助手。用户会问关于他自己的事情：
- 笔记内容（通过 search_notes / read_note 查 Obsidian vault）
- 历史活动记录、记忆、deadline、todo（通过 query_events 等工具查数据库）

工作方式：
- 收到问题后，调用合适的工具拿数据，再综合回复。
- 一次回复一段话，简洁直接。不要寒暄、不要多轮试探。
- 时间范围相关的问题（"上周"、"今天"、"最近三天"）自己换算成 ISO8601 区间。
- 找不到就直说找不到。"""
```

不带"日和"人设、不带 protocols、不带 communication style——这是个工具型 bot，不是陪伴型。

### `mcp_bot/main.py`

```python
async def main():
    mcp_client = MCPClient([
        ("obsidian", ["python", "-m", "mcp_bot.obsidian_mcp_server"]),
        ("lifetracker", ["python", "-m", "mcp_bot.lifetracker_mcp_server"]),
    ])
    await mcp_client.start()
    agent = Agent(mcp_client)
    discord = DiscordClient(agent, token=config.MCP_BOT_TOKEN, ...)
    try:
        await discord.run()
    finally:
        await mcp_client.stop()

if __name__ == "__main__":
    asyncio.run(main())
```

**主进程不直接持有 Database 实例**——`lifetracker_mcp_server` 子进程在自己进程内 `Database(config.DB_PATH)`，每个连接都会跑一次 `PRAGMA journal_mode=WAL`（idempotent），不需要主进程预热。

## 配置变更

### `config.json` 新增（已落到文件，token 占位）

```json
"mcp_bot": {
  "discord_token": "<TODO: 填入 Bot B 的 Discord application token>",
  "model": "claude-sonnet-4-6"
},
"obsidian": {
  "vault_path": "/Users/chachaya/dev/projects/my-data-repo"
}
```

- 不需要 `channel_id`：Bot B 走 DM，按 `discord.allowed_user_id` 过滤即可
- `obsidian.vault_path` 跟旧 spec 路径同（如旧 spec 后续也实施，无冲突）

### `config.py` 新增常量

```python
MCP_BOT_TOKEN = _cfg["mcp_bot"]["discord_token"]
MCP_BOT_MODEL = _cfg["mcp_bot"].get("model", "claude-sonnet-4-6")
OBSIDIAN_VAULT_PATH = _cfg["obsidian"]["vault_path"]
# ALLOWED_USER_ID 已存在（顶层 discord.allowed_user_id），无需新增
```

### `requirements.txt` 新增

```
mcp>=1.0.0
```

`anthropic` / `discord.py` 已存在，复用。

## 数据流

### 笔记查询

```
"COMP9417 Week 5 gradient descent 是什么"
  ↓ Discord (Bot B application) → mcp_bot.discord_client
  ↓ agent.run(text)
  ↓ Anthropic API → tool_use: search_notes(query="gradient descent", folder="COMP9417")
  ↓ mcp_client → obsidian_mcp_server (stdio) → obsidian_search.search_notes()
  ↓ 返回匹配列表
  ↓ Anthropic API → tool_use: read_note(path="COMP9417/Week5.md")
  ↓ 返回完整内容
  ↓ Anthropic API → text
  ↓ Discord 回复
```

### 活动查询

```
"我这周都干了什么"
  ↓ agent.run(text)
  ↓ Anthropic API → tool_use: weekly_summary()
  ↓ mcp_client → lifetracker_mcp_server (stdio) → bot.database.Database.get_events(...)
  ↓ 聚合后返回
  ↓ Anthropic API → text
  ↓ Discord 回复
```

## Verification

1. **WAL 启用**：在 main.py 启动 Bot A 一次后，检查 db 文件目录有 `*-wal` `*-shm` 副产品。
2. **MCP server 单跑**：
   ```bash
   python -m mcp_bot.obsidian_mcp_server  # 应该静默等 stdio
   python -m mcp_bot.lifetracker_mcp_server
   ```
3. **obsidian_search 单测**：
   ```bash
   python -c "from mcp_bot.obsidian_search import search_notes; print(search_notes('/Users/chachaya/dev/projects/my-data-repo', '关键词'))"
   ```
4. **并发安全**：先启 Bot A（`python main.py`），再启 Bot B（`python -m mcp_bot.main`）。在 Bot A 频道说话产生写入；同时 Bot B 频道发"近一周都做了什么"，应该秒回，无锁等待报错。
5. **端到端**：
   - Bot B 频道发"我今天做了什么"→ 期望 agent 调 `query_events` 限定今日 → 文字回复列出活动
   - Bot B 频道发"我笔记里关于 X 写了什么"→ 期望调 `search_notes` 然后 `read_note` → 综合回复
   - Bot B 频道发"我有哪些 deadline"→ 期望调 `list_active_deadlines` → 文字回复

## Out of Scope（本期不做）

- Bot B 的写能力（add_event / set_reminder / add_memory / ...）
- 多轮对话 / 上下文记忆
- Scheduler / proactive / reminder 触发
- 语义检索（embedding）
- PDF / 非 markdown 文件解析（用户已决定先人工总结成 md 入 vault）
- 共享 `obsidian_search.py` 给 Bot A（旧 `obsidian-claude-code.md` spec 仍然搁置；如果将来 Bot A 也要 query_obsidian，再决定抽到 `shared/`）
- Bot A 的退场或合并

## Phase 2 占位（不实施，仅记录）

未来 Bot B 要写时需要解决的问题：
- 双 writer 并发 → 引入文件锁 / 单 writer 进程 / 消息队列
- Bot A scheduler 唤醒机制：reminder 数据本身在 `reminders` 表，scheduler 启动时读 + 在内存维护下一个唤醒时刻。Bot B 若新增 reminder，Bot A 的内存 wakeup 计时器不会自动刷新 → 要么 scheduler 周期性 poll DB，要么走显式 IPC 通知（pipe / signal / fs notify），要么 reminder 写入只走单进程
- 工具复用：考虑把 `bot/tools.py` 的工具实现抽到 `shared/tools_impl/`，两 bot 各自包装成 MCP tool / 直调函数
