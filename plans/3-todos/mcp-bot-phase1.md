# Todo: MCP Bot B Phase 1 实施

> Spec: `plans/2-specs/mcp-bot.md`
> 原则：每步独立可验证，跑通再下一步；做完整步划掉

## 0. 准备 ✅

- [x] **依赖**：`requirements.txt` 已含 `mcp>=1.0.0`，`.venv` 实际安装 1.27.0
- [x] **快速确认**：`.venv/bin/pip show mcp` 输出 `Version: 1.27.0`

## 1. WAL + config 常量（基础设施）✅

- [x] **改 `bot/database.py::_get_conn()`**：加 `conn.execute("PRAGMA journal_mode=WAL")`
- [x] **改 `config.py`**：新增 `MCP_BOT_TOKEN` / `MCP_BOT_MODEL` / `OBSIDIAN_VAULT_PATH`（用 `.get()` 容错，让没配置 mcp_bot 段的环境也能跑 Bot A）
- [x] **验证**：`PRAGMA journal_mode` 返回 `wal`；三个常量都正确加载
- [ ] **遗留**：Bot A 实际运行时再确认不退化（你下次启 Bot A 留意一下，发一句话回复正常即可）

## 2. `mcp_bot/obsidian_search.py`（纯函数）✅

- [x] `mcp_bot/__init__.py` + `obsidian_search.py`
- [x] `search_notes` 支持 query / folder / tags 过滤，按命中频次 + 标题加权排序
- [x] `read_note` 截断 3000 字 + 路径穿越防护（`relative_to(vault)`）
- [x] **验证**：临时 fixture 9 个用例全过（关键词/folder/tags/隐藏目录跳过/截断/路径穿越/不存在）；真实 vault 跑通无报错

## 3. `mcp_bot/obsidian_mcp_server.py`（MCP server #1）✅

- [x] 用 `FastMCP` + `@mcp.tool()` 装饰器暴露 `search_notes` / `read_note`
- [x] schema 由签名 + docstring 自动生成
- [x] **验证**：mcp 客户端 stdio 握手 → list_tools 返回两个工具 → call_tool 真实命中 vault 文件、错误路径走 `error` 字段

## 4. `mcp_bot/lifetracker_mcp_server.py`（MCP server #2）✅

- [x] 9 个 MCP tool 全部到位（query_events / ongoing / planned / memories / deadlines / todos / categories / project_names / weekly_summary）
- [x] 模块级 `_db = Database(config.DB_PATH)`
- [x] **验证**：mcp client 端到端连接，list_tools 返回 9 个；weekly_summary 输出真实数据（98h Routine / 27h Chill / 23h Focus）；query_events for today 11 events
- [x] **WAL 并发证明**：Bot A 同时在跑（PID 93267），lifetracker_mcp_server 并发读零 lock 报错

## 5. `mcp_bot/mcp_client.py`（多 server stdio client）✅

- [x] `MCPClient([(name, argv), ...])` 用 `AsyncExitStack` 管理多个 stdio session
- [x] start 时拉两个 server 的 `list_tools`、合并到 Anthropic 风格 `tools_schema`、构建 `tool_name → server` 路由表
- [x] tool name 冲突时抛错（保护设计）；`call_tool` 把 MCP 内容拍平为字符串、`isError` 加 `[tool error]` 前缀
- [x] **验证**（heredoc，不写临时文件）：11 工具合并、跨 server 路由正确、unknown tool 优雅降级、clean shutdown

## 6. `mcp_bot/prompts.py`

- [ ] 写 `SYSTEM_PROMPT`（参考 spec 中段，简短直接的查询助手定位）
- [ ] **验证**：`python -c "from mcp_bot.prompts import SYSTEM_PROMPT; print(SYSTEM_PROMPT)"`

## 7. `mcp_bot/agent.py`（核心循环）

- [ ] 实现 `Agent(mcp_client)` 类
- [ ] `run(user_text)`：
  - prepend `[当前时间: {datetime.now().astimezone().isoformat(timespec='seconds')}]\n` 到 user message
  - 调 `anthropic.AsyncAnthropic.messages.create(model=config.MCP_BOT_MODEL, system=SYSTEM_PROMPT, tools=mcp_client.tools_schema, messages=...)`
  - 处理 `tool_use` 块 → `await mcp_client.call_tool(...)` → append `tool_result` → 下一轮
  - `MAX_TOOL_ROUNDS = 6` 兜底
  - 收集所有 text 块拼接为最终回复
- [ ] **验证**：扩展 step 5 的 smoke 脚本：
  ```python
  from mcp_bot.agent import Agent
  agent = Agent(c)
  reply = await agent.run("我有哪些 memory？")
  print(reply)
  ```
  预期：agent 调 `list_memories`，返回总结性中文回复

## 8. `mcp_bot/discord_client.py`

- [ ] 用 `discord.Client(intents=...)`，开启 `messages` + `message_content` + `dm_messages` intents
- [ ] `on_message` 三道过滤：忽略自己的消息、`channel.type != ChannelType.private` 直接 return、`author.id != config.ALLOWED_USER_ID` 直接 return
- [ ] 通过过滤后：`async with channel.typing(): reply = await agent.run(content); await channel.send(reply)`
- [ ] 长回复处理：超过 2000 字按段切（Discord 单条上限）
- [ ] **验证**：先在 main 串起来后端到端测（见 step 9）

## 9. `mcp_bot/main.py`（串起来 + 端到端）

- [ ] `asyncio.run(main())` entry
- [ ] 启 mcp_client → 建 agent → 启 discord client
- [ ] try/finally 确保异常退出时 mcp_client.stop()
- [ ] **端到端验证**（保留 Bot A 同时运行）：
  - 终端 A：`python main.py`（Bot A 照常跑）
  - 终端 B：`python -m mcp_bot.main`
  - **Discord 应用里前往 Bot B 的 application 设置**：
    - `Bot` tab → 开 `MESSAGE CONTENT INTENT`
    - `OAuth2` 生成 invite link 时勾 `bot` scope（DM 不需要 servers，但 Discord 要求 bot 至少在一个 server 才能被 DM——加到一个测试服务器即可，无需 channel）
  - 在 Discord 私聊给 Bot B 发：
    1. "我有哪些 memory？" → 期望调 `list_memories`，回答列出
    2. "我今天做了什么" → 期望调 `query_events` 限定今日，回答列出
    3. "我笔记里关于 X 写了什么" → 期望先 `search_notes` 再 `read_note`，回答综合
    4. "我有哪些 deadline" → 期望调 `list_active_deadlines`
  - 验证 Bot A 完全不受影响：在 Bot A 频道说话，正常回复
  - 同时跑 1 分钟，无 sqlite lock 报错

## 10. 收尾

- [x] ~~删 `mcp_bot/_smoke_client.py`~~ —— 没建（用 heredoc 替代）
- [ ] commit 一次（建议 feat 分支：`feat/mcp-bot-phase1`，多 commit 按各步骤分）
- [ ] 更新 `devlog.md` 记录上线日期 + 简短总结
- [ ] 更新 `plans/00-index.md`：从 `🟢 进行中` 移到 `📦 已完成`
- [ ] 删本 todo 文件（`plans/3-todos/mcp-bot-phase1.md`）

## 风险记录（实施过程中发现请补在这里）

- [待填]
