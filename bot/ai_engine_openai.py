"""
AI 引擎模块 (OpenAI 官方版)
用 openai 官方 SDK (AsyncOpenAI) 直接调用 OpenAI 官方 API。

与 relay 引擎的区别：
- 使用官方 SDK（自带重试、错误类型化）
- 打印 token cache 命中日志（对齐 Claude 引擎的 log 形态）
- base_url 默认走 SDK 默认值（api.openai.com），preset.base_url 非空时可覆盖
"""
import hashlib
import json
import re
from openai import AsyncOpenAI, OpenAIError
from bot.tools import get_tools
from bot.prompts import build_tool_round_hint, PromptParts
from bot.database import Database
from bot.ai_provider_error import AIProviderError
from bot.ai_engine_base import (
    _execute_tool_async,
    chat as _base_chat, scheduled_action as _base_scheduled_action,
    simple_completion as _base_simple_completion,
)
from bot.logger import get_logger
from bot import test_mode, trace
from config import Preset

logger = get_logger(__name__)


def _section_fingerprints(prompt: PromptParts | None) -> list[dict]:
    """对渲染后的各 block 算 char_count + sha256[:8]，用于 cache 命中追踪。

    OpenAI 的 prompt cache 是 prefix-based 且自动的，不像 Anthropic 有显式 block 边界；
    这里的 fingerprint 仅用于调试时快速判断"哪段内容变了"。
    """
    if prompt is None:
        return []
    return [
        {
            "i": i,
            "tier": tier,
            "chars": len(text),
            "hash": hashlib.sha256(text.encode("utf-8")).hexdigest()[:8],
        }
        for i, (tier, text) in enumerate(prompt.render_blocks())
    ]


# 懒加载客户端缓存：按 (api_key, base_url) 存储
_clients: dict[tuple[str, str], AsyncOpenAI] = {}


def _get_client(api_key: str, base_url: str) -> AsyncOpenAI:
    key = (api_key, base_url)
    if key not in _clients:
        kwargs = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        _clients[key] = AsyncOpenAI(**kwargs)
    return _clients[key]


async def chat(db: Database, messages: list[dict], preset: Preset,
               send_callback=None, tool_callback=None) -> str:
    return await _base_chat(db, messages, _call_with_tools, preset,
                            send_callback=send_callback, tool_callback=tool_callback)


async def scheduled_action(db: Database, prompt: str, timestamp: str,
                           history: list[dict], preset: Preset,
                           send_callback=None, allow_silent: bool = False,
                           trigger: str | None = None,
                           tool_profile: str | None = None,
                           check_in_name: str | None = None,
                           context_config: dict | None = None) -> str | None:
    return await _base_scheduled_action(db, prompt, timestamp, history, _call_with_tools,
                                        preset,
                                        send_callback=send_callback,
                                        allow_silent=allow_silent, trigger=trigger,
                                        tool_profile=tool_profile,
                                        check_in_name=check_in_name,
                                        context_config=context_config)


async def simple_completion(prompt: str, preset: Preset) -> str:
    return await _base_simple_completion(prompt, _call_with_tools, preset)


async def _call_with_tools(db: Database, prompt: PromptParts | None, messages: list[dict],
                           preset: Preset,
                           send_callback=None, tool_callback=None,
                           tool_names: set | None = None) -> str:
    """调用 OpenAI 官方 API，处理可能的多轮 tool calling。

    使用 OpenAI 自动 prompt caching（≥1024 token 的前缀自动缓存），
    响应 usage 里的 cached_tokens 反映命中情况。
    """
    client = _get_client(preset.api_key, preset.base_url)
    model = preset.model

    # 拍平 PromptParts 为单个 system string
    full_system = prompt.flatten() if prompt else ""
    section_prints = _section_fingerprints(prompt)

    # 按 tool_names 过滤工具子集
    tools = get_tools(tool_names)

    tools_hash = hashlib.sha256(
        json.dumps(tools, sort_keys=True).encode("utf-8")
    ).hexdigest()[:8] if tools else "none"
    logger.info(f"🧩 tools: count={len(tools)} hash={tools_hash}")

    full_messages = [{"role": "system", "content": full_system}] + list(messages)
    all_texts = []
    sent_display_texts = set()

    test_mode.ensure_handler_state()

    for round_idx in range(5):
        kwargs = dict(
            model=model,
            max_completion_tokens=4096,
            messages=full_messages,
        )
        if tools:
            kwargs["tools"] = tools

        test_mode.log_prompt("openai", model, kwargs, round_num=round_idx + 1,
                             extra={"section_fingerprints": section_prints})

        try:
            response = await client.chat.completions.create(**kwargs)
        except OpenAIError as e:
            raise AIProviderError(f"OpenAI API 调用失败: {e}") from e

        # Token usage & prompt cache 日志
        usage_log = None
        if response.usage is not None:
            u = response.usage
            prompt_tokens = u.prompt_tokens or 0
            completion_tokens = u.completion_tokens or 0
            # cached_tokens 在 prompt_tokens_details 子对象里（SDK 较新版本）
            details = getattr(u, "prompt_tokens_details", None)
            cached = getattr(details, "cached_tokens", 0) if details else 0
            logger.info(
                f"📊 Token usage: input={prompt_tokens}, output={completion_tokens}, "
                f"cached={cached}"
            )
            if prompt_tokens > 0:
                logger.info(f"   cache_hit_rate={cached / prompt_tokens * 100:.1f}%")
            fp_str = " · ".join(
                f"blk{p['i']}={p['chars']}c[{p['hash']}]" for p in section_prints
            )
            logger.info(f"   system sections: {fp_str if fp_str else '(none)'}")
            usage_log = {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cached_tokens": cached,
            }

        choices = response.choices or []
        if not choices:
            return "（内部错误：OpenAI 未返回内容）"

        choice = choices[0]
        message = choice.message
        finish_reason = choice.finish_reason or ""

        round_text = (message.content or "").strip()
        tool_calls = message.tool_calls or []

        test_mode.log_response("openai", model, {
            "finish_reason": finish_reason,
            "content": round_text,
            "tool_calls": [
                {"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments}
                for tc in tool_calls
            ],
            "usage": usage_log,
        }, round_num=round_idx + 1)

        # 提取并记录 <think> / <thinking> 块
        think_blocks = re.findall(r'<think(?:ing)?>(.*?)</think(?:ing)?>', round_text, flags=re.DOTALL)
        think_content = "\n".join(b.strip() for b in think_blocks if b.strip()) if think_blocks else ""
        if think_content:
            logger.info(f"🤔 思考:\n{think_content}")

        display_text = re.sub(r'<think(?:ing)?>.*?</think(?:ing)?>', '', round_text, flags=re.DOTALL).strip()

        # 最后一轮（没有 tool_call）
        if not tool_calls:
            if display_text and display_text not in sent_display_texts:
                logger.info(f"💬 发送回复:\n{display_text}")
                if send_callback:
                    await send_callback(display_text)
                sent_display_texts.add(display_text)
                all_texts.append(display_text)
            trace.add_round(
                raw_output=round_text, think=think_content, visible_text=display_text,
                tool_calls=[], tool_results=[],
                usage=usage_log, stop_reason=finish_reason,
            )
            return "\n".join(all_texts)

        # 中间轮：文字也直接发给用户
        if display_text and display_text not in sent_display_texts:
            logger.info(f"💬 发送回复:\n{display_text}")
            if send_callback:
                await send_callback(display_text)
            sent_display_texts.add(display_text)
            all_texts.append(display_text)

        # 把 assistant 的完整消息加入（OpenAI 要求 tool_calls 回传时带上原始 shape）
        assistant_msg = {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in tool_calls
            ],
        }
        if round_text:
            assistant_msg["content"] = round_text
        full_messages.append(assistant_msg)

        # 执行每个 tool
        called_names = []
        trace_tool_calls = []
        trace_tool_results = []
        for tc in tool_calls:
            func_name = tc.function.name
            try:
                func_args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                func_args = {}

            result = await _execute_tool_async(db, func_name, func_args)
            called_names.append(func_name)
            trace_tool_calls.append({"name": func_name, "input": func_args, "id": tc.id})
            trace_tool_results.append({"name": func_name, "tool_use_id": tc.id, "result": result})

            full_messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(result, ensure_ascii=False),
            })

        # 附加 TOOL_ROUND_REMINDER + per-tool 决策辅助
        full_messages.append({
            "role": "user",
            "content": build_tool_round_hint(called_names),
        })

        trace.add_round(
            raw_output=round_text, think=think_content, visible_text=display_text,
            tool_calls=trace_tool_calls, tool_results=trace_tool_results,
            usage=usage_log, stop_reason=finish_reason,
        )

        if tool_callback and called_names:
            await tool_callback(called_names)

    return "\n".join(all_texts) or "（内部错误：工具调用次数过多）"
