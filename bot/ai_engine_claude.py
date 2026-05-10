"""
AI 引擎模块 (Claude 原生版)
负责调用 Anthropic Claude API，处理 tool calling
"""
import hashlib
import json
import re
from anthropic import AsyncAnthropic
from bot.tools import get_tools, to_anthropic_tools
from bot.prompts import build_tool_round_hint, PromptParts
from bot.database import Database
from bot.ai_provider_error import AIProviderError
from bot.ai_engine_base import (
    _execute_tool,
    chat as _base_chat, scheduled_action as _base_scheduled_action,
    simple_completion as _base_simple_completion,
)
from bot.logger import get_logger
from bot import test_mode, trace
from config import Preset

logger = get_logger(__name__)


def _block_fingerprints(blocks: list[dict]) -> list[dict]:
    """为每个 system block 算 char_count + sha256[:8]，用于 cache 命中追踪。"""
    prints = []
    for i, b in enumerate(blocks):
        text = b.get("text", "") if isinstance(b, dict) else ""
        prints.append({
            "i": i,
            "chars": len(text),
            "hash": hashlib.sha256(text.encode("utf-8")).hexdigest()[:8],
        })
    return prints

# 懒加载客户端缓存：按 api_key 存储，避免重复创建
_clients: dict[str, AsyncAnthropic] = {}


def _get_client(api_key: str) -> AsyncAnthropic:
    if api_key not in _clients:
        # 1h TTL 需要显式开启 extended-cache-ttl beta
        _clients[api_key] = AsyncAnthropic(
            api_key=api_key,
            default_headers={"anthropic-beta": "extended-cache-ttl-2025-04-11"},
        )
    return _clients[api_key]


async def chat(db: Database, messages: list[dict], preset: Preset,
               send_callback=None, tool_callback=None) -> str:
    return await _base_chat(db, messages, _call_with_tools, preset,
                            send_callback=send_callback, tool_callback=tool_callback)


async def scheduled_action(db: Database, prompt: str, timestamp: str,
                           history: list[dict], preset: Preset,
                           send_callback=None, allow_silent: bool = False,
                           trigger: str | None = None) -> str | None:
    return await _base_scheduled_action(db, prompt, timestamp, history, _call_with_tools,
                                        preset,
                                        send_callback=send_callback,
                                        allow_silent=allow_silent, trigger=trigger)


async def simple_completion(prompt: str, preset: Preset) -> str:
    return await _base_simple_completion(prompt, _call_with_tools, preset)


async def _call_with_tools(db: Database, prompt: PromptParts | None, messages: list[dict],
                           preset: Preset,
                           send_callback=None, tool_callback=None,
                           tool_names: set | None = None) -> str:
    """
    调用 Anthropic Claude，处理可能的多轮 tool calling。
    中间轮的文本通过 send_callback 发送。

    使用 Anthropic prompt caching：PromptParts 的三层直接映射到 3 个 cached system block。

    preset: 当前激活的 AI preset，提供 api_key 与 model。
    """
    client = _get_client(preset.api_key)
    model = preset.model

    # 构建 system blocks（3 个 cached block）
    system_blocks = prompt.to_claude_blocks() if prompt else []
    block_prints = _block_fingerprints(system_blocks)

    # 按 tool_names 过滤工具子集，并转换为 Anthropic schema
    tools = to_anthropic_tools(get_tools(tool_names))

    # tools 字段是 cache 前缀的最前段（顺序：tools → system → messages），
    # 一旦变化下游全部 invalidate，所以打 fingerprint 帮助追踪 chat/poll 一致性
    tools_hash = hashlib.sha256(
        json.dumps(tools, sort_keys=True).encode("utf-8")
    ).hexdigest()[:8] if tools else "none"
    logger.info(f"🧩 tools: count={len(tools)} hash={tools_hash}")

    all_texts = []  # 收集发送过的文本（拼接最终返回结果）
    sent_display_texts = set()  # 去重集合

    test_mode.ensure_handler_state()

    for round_idx in range(5):  # 最多 5 轮 tool calling，防止死循环
        kwargs = dict(
            model=model,
            max_tokens=4096,
            messages=messages,
        )
        if system_blocks:
            kwargs["system"] = system_blocks
        if tools:
            kwargs["tools"] = tools

        # 附带 system block 指纹供 log_viewer 做 cache 命中追踪
        test_mode.log_prompt("claude", model, kwargs, round_num=round_idx + 1,
                             extra={"system_block_fingerprints": block_prints})

        try:
            response = await client.messages.create(**kwargs)
        except Exception as e:
            raise AIProviderError(f"Claude API 调用失败: {e}") from e

        # Token usage & prompt caching 验证
        usage_log = None
        if hasattr(response, 'usage'):
            u = response.usage
            cache_create = getattr(u, 'cache_creation_input_tokens', 0) or 0
            cache_read = getattr(u, 'cache_read_input_tokens', 0) or 0
            total_input = u.input_tokens + cache_create + cache_read
            logger.info(f"📊 Token usage: input={u.input_tokens}, output={u.output_tokens}, total_input={total_input}")
            logger.info(f"   cache_creation={cache_create}, cache_read={cache_read}")
            if total_input > 0:
                logger.info(f"   cache_hit_rate={cache_read / total_input * 100:.1f}%")
            fp_str = " · ".join(f"blk{p['i']}={p['chars']}c[{p['hash']}]" for p in block_prints)
            logger.info(f"   system blocks: {fp_str if fp_str else '(none)'}")
            usage_log = {
                "input_tokens": u.input_tokens,
                "output_tokens": u.output_tokens,
                "cache_creation_input_tokens": cache_create,
                "cache_read_input_tokens": cache_read,
            }

        # 记录 AI 响应到测试日志
        content_log = []
        for block in response.content:
            if hasattr(block, 'type'):
                if block.type == "text":
                    content_log.append({"type": "text", "text": block.text})
                elif block.type == "tool_use":
                    content_log.append({"type": "tool_use", "id": block.id, "name": block.name, "input": block.input})
        test_mode.log_response("claude", model, {
            "stop_reason": response.stop_reason,
            "content": content_log,
            "usage": usage_log,
        }, round_num=round_idx + 1)

        # 提取文本回复和 tool 调用
        text_parts = []
        tool_uses = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_uses.append(block)

        round_text = "\n".join(text_parts).strip()

        # 提取并记录 <think> / <thinking> 块（Claude 4.x 有时会自发用 <thinking>）
        think_blocks = re.findall(r'<think(?:ing)?>(.*?)</think(?:ing)?>', round_text, flags=re.DOTALL)
        think_content = "\n".join(b.strip() for b in think_blocks if b.strip()) if think_blocks else ""
        if think_content:
            logger.info(f"🤔 思考:\n{think_content}")

        display_text = re.sub(r'<think(?:ing)?>.*?</think(?:ing)?>', '', round_text, flags=re.DOTALL).strip()

        # 最后一轮
        if response.stop_reason == "end_turn":
            if display_text and display_text not in sent_display_texts:
                logger.info(f"💬 发送回复:\n{display_text}")
                if send_callback:
                    await send_callback(display_text)
                sent_display_texts.add(display_text)
                all_texts.append(display_text)
            trace.add_round(
                raw_output=round_text, think=think_content, visible_text=display_text,
                tool_calls=[], tool_results=[],
                usage=usage_log, stop_reason="end_turn",
            )
            return "\n".join(all_texts)

        # 中间轮：文字也直接发给用户（每一轮文字 = 给她看的）
        if response.stop_reason == "tool_use" and tool_uses:
            if display_text and display_text not in sent_display_texts:
                logger.info(f"💬 发送回复:\n{display_text}")
                if send_callback:
                    await send_callback(display_text)
                sent_display_texts.add(display_text)
                all_texts.append(display_text)

            # 把 assistant 的完整回复加入消息（包含 text + tool_use）
            messages.append({"role": "assistant", "content": response.content})

            # 执行每个 tool，收集结果
            tool_results = []
            called_names = []
            trace_tool_calls = []
            trace_tool_results = []
            for tool_use in tool_uses:
                desc = next((t.get("description", "") for t in tools if t["name"] == tool_use.name), "")
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
                trace_tool_calls.append({"name": tool_use.name, "input": tool_use.input, "id": tool_use.id})
                trace_tool_results.append({"name": tool_use.name, "tool_use_id": tool_use.id, "result": result})

            # 把 tool 结果作为 user 消息加入；同时附加一条 system 风格的 text block
            # 提醒模型别在下一轮重复已经说过的话，并夹带命中工具的定向 post-hint
            round_hint = build_tool_round_hint(called_names)
            messages.append({
                "role": "user",
                "content": tool_results + [
                    {"type": "text", "text": round_hint}
                ],
            })

            trace.add_round(
                raw_output=round_text, think=think_content, visible_text=display_text,
                tool_calls=trace_tool_calls, tool_results=trace_tool_results,
                usage=usage_log, stop_reason="tool_use",
            )

            if tool_callback and called_names:
                await tool_callback(called_names)

    return "（内部错误：工具调用次数过多）"
