"""Discord listener for Bot B：DM-only、单用户、单次查询。

不支持多轮对话、reaction、scheduler。每条 DM 即一次独立 agent.run。
"""
from __future__ import annotations

import discord

import config
from bot.logger import get_logger
from mcp_bot.agent import Agent

log = get_logger("mcp_bot.discord_client")

_DISCORD_HARD_LIMIT = 2000
_CHUNK_LIMIT = 1900  # 留点余量


def _split_message(text: str, max_len: int = _CHUNK_LIMIT) -> list[str]:
    """切长消息：优先按段落（\n\n）→ 行（\n）→ 硬切。"""
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break
        cut = remaining.rfind("\n\n", 0, max_len)
        if cut <= 0:
            cut = remaining.rfind("\n", 0, max_len)
        if cut <= 0:
            cut = max_len
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    return chunks


class _Listener(discord.Client):
    def __init__(self, agent: Agent, intents: discord.Intents):
        super().__init__(intents=intents)
        self._agent = agent

    async def on_ready(self) -> None:
        log.info(
            "Bot B logged in as %s (id=%s); guilds=%s",
            self.user, self.user.id, [g.name for g in self.guilds],
        )

    async def on_message(self, message: discord.Message) -> None:
        if message.author == self.user or message.author.bot:
            return
        if message.channel.type != discord.ChannelType.private:
            return
        if message.author.id != config.ALLOWED_USER_ID:
            return

        content = message.content.strip()
        if not content:
            return

        log.info("query from %s: %s", message.author, content[:80])
        try:
            reply = await self._agent.run(content)
        except Exception as e:
            log.exception("agent error")
            await message.channel.send(f"[error] {type(e).__name__}: {e}")
            return

        for chunk in _split_message(reply):
            await message.channel.send(chunk)


class DiscordClient:
    """Bot B 的 Discord 入口包装。"""

    def __init__(self, agent: Agent, token: str):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.dm_messages = True
        self._client = _Listener(agent, intents)
        self._token = token

    async def start(self) -> None:
        await self._client.start(self._token)

    async def close(self) -> None:
        if not self._client.is_closed():
            await self._client.close()
