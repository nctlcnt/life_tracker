# Plan: Obsidian 课业笔记接入 Bot 与 Claude Code

## Context

日和 bot 目前只能访问 life-tracker 自己的 SQLite 数据（时间轴、记忆、deadline）。用户希望让 AI 在聊天时也能按需查阅 Obsidian 课业笔记。同时，Claude Code CLI 也应能读笔记。

目标架构：
- `obsidian_search.py`（纯 Python 搜索逻辑）被两侧共享
- 日和 bot → 通过 `query_obsidian` tool 直接调用搜索函数（无 IPC）
- Claude Code → 通过 MCP server（stdio）使用同一逻辑
- 搜索能力：文件夹/tag 过滤 + 关键词全文检索（语义检索留作后续）

## Files to Create

### 1. `bot/obsidian_search.py` — 核心搜索逻辑
共享模块，bot 和 MCP server 都 import 这里。

函数：
- `search_notes(vault_path, query, folder=None, tags=None, max_results=8) -> list[dict]`
  - 遍历 vault 目录下所有 `.md` 文件
  - 解析 YAML frontmatter 提取 `tags` 字段（用 `re` 而非重依赖）
  - 按 `folder`（路径前缀过滤）和 `tags`（必须包含所有指定 tag）过滤
  - 对 title（文件名）和 content 做大小写不敏感关键词匹配
  - 返回：`[{path, title, tags, snippet, matched_lines}]`，snippet 为匹配行附近 200 字
- `read_note(vault_path, relative_path) -> dict`
  - 读取指定路径的笔记完整内容（最多 3000 字，防止塞爆上下文）
  - 返回：`{path, title, content, tags}`

### 2. `obsidian_mcp_server.py` — MCP Server（项目根目录）
供 Claude Code 通过 MCP protocol（stdio）使用。

- 依赖 `mcp` SDK（`pip install mcp`）
- 从 `config.json` 读取 `obsidian.vault_path`
- 暴露两个 MCP tool：`search_notes` 和 `read_note`
- 入口：`if __name__ == "__main__": mcp.run(transport="stdio")`

## Files to Modify

### 3. `bot/tools.py`
在 TOOLS 和 TOOLS_ANTHROPIC 末尾追加两个工具定义：

**`query_obsidian`**（主搜索入口）
```python
{
  "name": "query_obsidian",
  "description": "在 Obsidian 课业笔记库中搜索。当用户问到某门课的内容、概念解释、公式、作业要求时调用。支持按文件夹或 tag 缩小范围后关键词检索。返回匹配笔记列表和片段。",
  "parameters/input_schema": {
    "query": str (required),       # 关键词
    "folder": str (optional),      # 限定 vault 子文件夹，如 "COMP9417"
    "tags": list[str] (optional),  # 必须包含这些 tag，如 ["ml", "lecture"]
  }
}
```

**`read_obsidian_note`**（精读单篇）
```python
{
  "name": "read_obsidian_note",
  "description": "读取一篇 Obsidian 笔记的完整内容。先用 query_obsidian 找到 path，再用这个工具精读。",
  "parameters/input_schema": {
    "path": str (required),  # 从 query_obsidian 返回的 path 字段
  }
}
```

工具子集：仅加入 chat 场景（不加入 POLL/REMINDER/SET_TOOL_NAMES），因为是重型查询。

### 4. `bot/ai_engine_base.py`
在 `_execute_tool()` 函数末尾追加两个 elif 分支：

```python
elif tool_name == "query_obsidian":
    from bot.obsidian_search import search_notes
    vault_path = config.OBSIDIAN_VAULT_PATH
    results = search_notes(vault_path, args["query"],
                           folder=args.get("folder"),
                           tags=args.get("tags"))
    return {"success": True, "results": results, "count": len(results)}

elif tool_name == "read_obsidian_note":
    from bot.obsidian_search import read_note
    vault_path = config.OBSIDIAN_VAULT_PATH
    note = read_note(vault_path, args["path"])
    return {"success": True, **note}
```

### 5. `config.py`
添加 `OBSIDIAN_VAULT_PATH` 常量，从 `config.json` 的 `obsidian.vault_path` 字段读取。
若未配置则返回空字符串（工具执行时检查并返回友好错误）。

### 6. `config.json`
添加配置段：
```json
"obsidian": {
  "vault_path": "/Users/chachaya/path/to/your/vault"
}
```
**用户需要填入实际 vault 路径。**

### 7. `requirements.txt`
添加：`mcp>=1.0.0`

### 8. `~/.claude/settings.json`
在已有 `{"advisorModel": "opus"}` 中追加：
```json
{
  "advisorModel": "opus",
  "mcpServers": {
    "obsidian": {
      "command": "python",
      "args": ["/Users/chachaya/dev/life-tracker/life-tracker/obsidian_mcp_server.py"]
    }
  }
}
```

### 9. `bot/prompts.py`（`TOOL_GUIDELINES_CHAT`）
在工具说明末尾追加一段关于 `query_obsidian` 的使用时机说明，让 AI 知道何时主动调用。

## 数据流

```
用户："帮我回忆一下 COMP9417 Week 5 的 gradient descent"
  │
  ▼
日和 AI → tool_use: query_obsidian(query="gradient descent", folder="COMP9417")
  │
  ▼
_execute_tool() → obsidian_search.search_notes(vault_path, ...)
  │
  ▼
返回 [{title, path, snippet}, ...]
  │
  ▼
AI → tool_use: read_obsidian_note(path="COMP9417/Week5-GradientDescent.md")
  │
  ▼
返回完整笔记内容（≤3000字）
  │
  ▼
AI 综合笔记内容回答用户

Claude Code 同路径：
  ~/.claude/settings.json → MCP server → obsidian_search.py
```

## Verification

1. 手动测试 `bot/obsidian_search.py`：
   ```bash
   python -c "from bot.obsidian_search import search_notes; print(search_notes('/path/to/vault', 'gradient descent', folder='COMP9417'))"
   ```

2. 测试 MCP server 启动：
   ```bash
   python obsidian_mcp_server.py
   ```
   期望：server 在 stdio 上等待，无报错

3. 在 Discord 中向日和发送：
   "帮我看一下[课程名] [主题]的笔记" → 观察 AI 是否发起 `query_obsidian` tool call（✅ reaction 不会触发，因为不在 SET_TOOL_NAMES）

4. 在 Claude Code 中验证：
   `/mcp` 命令查看 obsidian server 是否在线，然后提问课程相关内容

## Out of Scope（本次不做）

- 语义向量搜索（embedding-based）
- Obsidian 笔记的写入/修改
- 笔记变更的实时索引
