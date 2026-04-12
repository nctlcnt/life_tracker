"""
AI 引擎模块 (Claude 原生版)
负责调用 Anthropic Claude API，处理 tool calling
"""
import json
from anthropic import AsyncAnthropic
from bot.tools import TOOLS_ANTHROPIC
from bot.prompts import build_tool_round_hint, PromptParts
from bot.database import Database
from bot.ai_engine_base import (
    _execute_tool, split_thinking,
    chat as _base_chat, scheduled_action as _base_scheduled_action,
    simple_completion as _base_simple_completion,
)
from bot.logger import get_logger
import config

logger = get_logger(__name__)

client = AsyncAnthropic(
    api_key=config.AI_API_KEY
)


async def chat(db: Database, messages: list[dict],
               send_callback=None, tool_callback=None) -> str:
    return await _base_chat(db, messages, _call_with_tools, send_callback, tool_callback)


async def scheduled_action(db: Database, prompt: str, timestamp: str,
                           history: list[dict],
                           send_callback=None, allow_silent: bool = False,
                           trigger: str | None = None) -> str | None:
    return await _base_scheduled_action(db, prompt, timestamp, history, _call_with_tools,
                                        send_callback, allow_silent, trigger)


async def simple_completion(prompt: str) -> str:
    return await _base_simple_completion(prompt, _call_with_tools)


async def _call_with_tools(db: Database, prompt: PromptParts | None, messages: list[dict],
                           send_callback=None, tool_callback=None,
                           model: str | None = None, tool_names: set | None = None) -> str:
    """
    调用 Anthropic Claude，处理可能的多轮 tool calling。
    中间轮的文本通过 send_callback 发送。

    使用 Anthropic prompt caching：PromptParts 的三层直接映射到 3 个 cached system block。
    """
    # 构建 system blocks（3 个 cached block）
    system_blocks = prompt.to_claude_blocks() if prompt else []

    # 按 tool_names 过滤工具子集
    tools = TOOLS_ANTHROPIC
    if tool_names is not None:
        tools = [t for t in TOOLS_ANTHROPIC if t["name"] in tool_names]

    all_texts = []  # 收集所有轮次的文本

    if not model:
        model = getattr(config, 'CHAT_MODEL', 'claude-3-opus-20240229')

    for _ in range(5):  # 最多 5 轮 tool calling，防止死循环
        kwargs = dict(
            model=model,
            max_tokens=4096,
            messages=messages,
        )
        if system_blocks:
            kwargs["system"] = system_blocks
        if tools:
            kwargs["tools"] = tools

        response = await client.messages.create(**kwargs)

        # Token usage & prompt caching 验证
        if hasattr(response, 'usage'):
            u = response.usage
            cache_create = getattr(u, 'cache_creation_input_tokens', 0) or 0
            cache_read = getattr(u, 'cache_read_input_tokens', 0) or 0
            total_input = u.input_tokens + cache_create + cache_read
            logger.info(f"📊 Token usage: input={u.input_tokens}, output={u.output_tokens}, total_input={total_input}")
            logger.info(f"   cache_creation={cache_create}, cache_read={cache_read}")
            if total_input > 0:
                logger.info(f"   cache_hit_rate={cache_read / total_input * 100:.1f}%")

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
                user_text, thinking = split_thinking(round_text)
                if thinking:
                    logger.info(f"🧠 最后一轮独白（已剥离）:\n{thinking.strip()}")
                if user_text:
                    if send_callback:
                        await send_callback(user_text)
                    all_texts.append(user_text)
            return "\n".join(all_texts)

        # 中间轮：文本视为内心独白，不发给用户、不计入最终回复
        if response.stop_reason == "tool_use" and tool_uses:
            if round_text:
                # 中间轮的 <think> 或 <thinking> 标签可有可无，有的话只记干净版本到日志
                _u, _t = split_thinking(round_text)
                monologue = (_t or _u or round_text).strip()
                logger.info(f"🧠 内心独白:\n{monologue}")

            # 把 assistant 的完整回复加入消息（包含 text + tool_use）
            messages.append({"role": "assistant", "content": response.content})

            # 执行每个 tool，收集结果
            tool_results = []
            called_names = []
            for tool_use in tool_uses:
                desc = next((t.get("description", "") for t in TOOLS_ANTHROPIC if t["name"] == tool_use.name), "")
                desc_first = desc.split("。")[0] if desc else ""
                logger.info(f"🛠️ 调用工具: {tool_use.name} | {desc_first}")
                logger.info(f"   参数: {tool_use.input}")
                
                result = _execute_tool(db, tool_use.name, tool_use.input)
                called_names.append(tool_use.name)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "content": json.dumps(result, ensure_ascii=False)
                })

            # 把 tool 结果作为 user 消息加入；同时附加一条 system 风格的 text block
            # 提醒模型别在下一轮重复已经说过的话，并夹带命中工具的定向 post-hint
            round_hint = build_tool_round_hint(called_names)
            messages.append({
                "role": "user",
                "content": tool_results + [
                    {"type": "text", "text": round_hint}
                ],
            })

            if tool_callback and called_names:
                await tool_callback()

    return "（内部错误：工具调用次数过多）"
