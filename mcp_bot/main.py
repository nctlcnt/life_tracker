"""Bot B 主入口（独立于 Bot A 的 main.py）。

用法：
    python -m mcp_bot.main
"""
from __future__ import annotations

import asyncio
import sys

import config
from bot.logger import setup_logging, get_logger

setup_logging(config.LOG_LEVEL, config.LOG_FILE)
log = get_logger("mcp_bot.main")

from mcp_bot.agent import Agent
from mcp_bot.discord_client import DiscordClient
from mcp_bot.mcp_client import MCPClient


async def main() -> None:
    if not config.MCP_BOT_TOKEN:
        log.error("MCP_BOT_TOKEN 为空，请检查 config.json.mcp_bot.discord_token")
        sys.exit(1)

    mcp_client = MCPClient([
        ("obsidian", [sys.executable, "-m", "mcp_bot.obsidian_mcp_server"]),
        ("lifetracker", [sys.executable, "-m", "mcp_bot.lifetracker_mcp_server"]),
    ])
    log.info("正在启动 MCP servers...")
    await mcp_client.start()
    log.info("已加载 MCP 工具: %s", [t["name"] for t in mcp_client.tools_schema])

    discord_client: DiscordClient | None = None
    try:
        agent = Agent(mcp_client)
        discord_client = DiscordClient(agent, config.MCP_BOT_TOKEN)
        log.info("正在连接 Discord (Bot B)...")
        await discord_client.start()
    finally:
        if discord_client is not None:
            await discord_client.close()
        log.info("正在关闭 MCP servers...")
        await mcp_client.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("收到中断信号，已退出")
