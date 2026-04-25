"""Agent loop: Anthropic Messages API + MCP tool_use 多轮循环。

每次 run() 处理一条独立查询。无对话历史、无流式、无 reaction。
"""
from __future__ import annotations

from datetime import datetime

from anthropic import AsyncAnthropic

import config
from mcp_bot.mcp_client import MCPClient
from mcp_bot.prompts import SYSTEM_PROMPT

MAX_TOOL_ROUNDS = 6
MAX_TOKENS = 2048


class Agent:
    """单次查询 agent：一条 user 消息 → N 轮 tool_use → 最终文字回复。"""

    def __init__(self, mcp_client: MCPClient):
        self._mcp = mcp_client
        preset = config.PRESETS.get(config.MCP_BOT_MODEL)
        if preset is None:
            raise RuntimeError(
                f"MCP_BOT_MODEL '{config.MCP_BOT_MODEL}' 不在 config.PRESETS；"
                f"已知 presets: {list(config.PRESETS.keys())}"
            )
        if preset.provider != "claude":
            raise RuntimeError(
                f"Bot B agent 当前仅支持 provider=claude；"
                f"preset '{config.MCP_BOT_MODEL}' 是 {preset.provider}"
            )
        self._anthropic = AsyncAnthropic(api_key=preset.api_key)
        self._model = preset.model

    async def run(self, user_text: str) -> str:
        now_iso = datetime.now().astimezone().isoformat(timespec="seconds")
        messages: list[dict] = [
            {"role": "user", "content": f"[当前时间: {now_iso}]\n{user_text}"}
        ]
        text_chunks: list[str] = []

        for _round in range(MAX_TOOL_ROUNDS):
            resp = await self._anthropic.messages.create(
                model=self._model,
                system=SYSTEM_PROMPT,
                tools=self._mcp.tools_schema,
                messages=messages,
                max_tokens=MAX_TOKENS,
            )

            assistant_content: list[dict] = []
            tool_calls: list[tuple[str, str, dict]] = []
            for block in resp.content:
                if block.type == "text":
                    text_chunks.append(block.text)
                    assistant_content.append({"type": "text", "text": block.text})
                elif block.type == "tool_use":
                    assistant_content.append({
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    })
                    tool_calls.append((block.id, block.name, block.input))

            messages.append({"role": "assistant", "content": assistant_content})

            if resp.stop_reason != "tool_use":
                break

            tool_results: list[dict] = []
            for tool_id, tool_name, tool_args in tool_calls:
                result_text = await self._mcp.call_tool(tool_name, tool_args)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": result_text,
                })
            messages.append({"role": "user", "content": tool_results})
        else:
            text_chunks.append(f"\n[已达到工具调用上限 {MAX_TOOL_ROUNDS} 轮，回复可能不完整]")

        reply = "\n\n".join(c for c in text_chunks if c.strip())
        return reply or "(空回复)"
