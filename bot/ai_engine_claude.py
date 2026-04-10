"""
AI 引擎模块 (Claude 原生版)
负责调用 Anthropic Claude API，处理 tool calling
"""
import json
from anthropic import AsyncAnthropic
from bot.tools import TOOLS_ANTHROPIC
from bot.database import Database
from bot.ai_engine_base import (
    _execute_tool,
    chat as _base_chat, proactive_check as _base_proactive_check,
    reminder_action as _base_reminder_action,
)
import config

client = AsyncAnthropic(
    api_key=config.AI_API_KEY
)


async def chat(db: Database, user_message: str, timestamp: str,
               send_callback=None) -> str:
    return await _base_chat(db, user_message, timestamp, _call_with_tools, send_callback)


async def proactive_check(db: Database, timestamp: str,
                          send_callback=None) -> str | None:
    return await _base_proactive_check(db, timestamp, _call_with_tools, send_callback)


async def reminder_action(db: Database, action: str, timestamp: str,
                          send_callback=None) -> str:
    return await _base_reminder_action(db, action, timestamp, _call_with_tools, send_callback)


async def _call_with_tools(db: Database, system_prompt: str, messages: list[dict],
                           send_callback=None, dynamic_context: str | None = None,
                           model: str | None = None) -> str:
    """
    调用 Anthropic Claude，处理可能的多轮 tool calling。
    中间轮的文本通过 send_callback 发送。

    使用 Anthropic prompt caching：静态 system_prompt 标记 cache_control，
    动态 dynamic_context 不缓存。
    """
    # 构建 system blocks，静态部分开启 prompt caching
    system_blocks = [
        {
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral"}
        }
    ]
    if dynamic_context:
        system_blocks.append({
            "type": "text",
            "text": dynamic_context
        })

    all_texts = []  # 收集所有轮次的文本

    if not model:
        model = getattr(config, 'CHAT_MODEL', 'claude-3-opus-20240229')

    for _ in range(5):  # 最多 5 轮 tool calling，防止死循环
        response = await client.messages.create(
            model=model,
            max_tokens=4096,
            system=system_blocks,
            messages=messages,
            tools=TOOLS_ANTHROPIC,
        )

        print(f"🤖 stop_reason: {response.stop_reason}")
        print(f"🤖 content: {response.content}")

        # 提取文本回复和 tool 调用
        text_parts = []
        tool_uses = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_uses.append(block)

        round_text = "\n".join(text_parts).strip()

        # 最后一轮
        if response.stop_reason == "end_turn":
            if round_text:
                if send_callback:
                    await send_callback(round_text)
                all_texts.append(round_text)
            return "\n".join(all_texts)

        # 中间轮：发送文本，继续处理 tool calling
        if response.stop_reason == "tool_use" and tool_uses:
            if round_text:
                print(f"💬 中间轮文本: {round_text}")
                if send_callback:
                    await send_callback(round_text)
                all_texts.append(round_text)

            # 把 assistant 的完整回复加入消息（包含 text + tool_use）
            messages.append({"role": "assistant", "content": response.content})

            # 执行每个 tool，收集结果
            tool_results = []
            for tool_use in tool_uses:
                result = _execute_tool(db, tool_use.name, tool_use.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "content": json.dumps(result, ensure_ascii=False)
                })

            # 把 tool 结果作为 user 消息加入
            messages.append({"role": "user", "content": tool_results})

    return "（内部错误：工具调用次数过多）"
