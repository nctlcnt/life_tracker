"""多 MCP server stdio 客户端：spawn 子进程、合并工具清单、按工具名路由 call_tool。

Agent 看到的是统一的 Anthropic 风格 tools_schema；它不需要知道某个工具来自哪个 server。
"""
from __future__ import annotations

from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class MCPClient:
    """启动多个 MCP server 子进程，统一暴露工具调用接口。

    用法：
        client = MCPClient([("obsidian", [sys.executable, "-m", "mcp_bot.obsidian_mcp_server"]), ...])
        await client.start()
        ...  # client.tools_schema 给 Anthropic API 用；await client.call_tool(name, args)
        await client.stop()
    """

    def __init__(self, servers: list[tuple[str, list[str]]]):
        self._servers = servers
        self._stack: AsyncExitStack | None = None
        self._sessions: dict[str, ClientSession] = {}
        self._tool_routes: dict[str, str] = {}
        self.tools_schema: list[dict] = []

    async def start(self) -> None:
        self._stack = AsyncExitStack()
        await self._stack.__aenter__()
        try:
            for name, argv in self._servers:
                params = StdioServerParameters(command=argv[0], args=argv[1:])
                read, write = await self._stack.enter_async_context(stdio_client(params))
                session = await self._stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                self._sessions[name] = session

                tools = await session.list_tools()
                for t in tools.tools:
                    if t.name in self._tool_routes:
                        raise RuntimeError(
                            f"tool name conflict: '{t.name}' from both "
                            f"'{self._tool_routes[t.name]}' and '{name}'"
                        )
                    self._tool_routes[t.name] = name
                    self.tools_schema.append({
                        "name": t.name,
                        "description": t.description or "",
                        "input_schema": t.inputSchema,
                    })
        except Exception:
            await self.stop()
            raise

    async def call_tool(self, name: str, args: dict[str, Any]) -> str:
        server = self._tool_routes.get(name)
        if not server:
            return f"[error] unknown tool: {name}"
        session = self._sessions[server]
        result = await session.call_tool(name, args)

        parts: list[str] = []
        for c in result.content:
            text = getattr(c, "text", None)
            parts.append(text if text is not None else str(c))
        body = "\n".join(parts)

        if result.isError:
            return f"[tool error] {body}"
        return body

    async def stop(self) -> None:
        if self._stack is not None:
            await self._stack.__aexit__(None, None, None)
            self._stack = None
        self._sessions.clear()
