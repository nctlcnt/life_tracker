"""
AI 引擎模块（LT-134 统一 OpenAI 兼容实现）

四个 provider 引擎合并后的唯一实现：httpx 直接调用 OpenAI Chat Completions
格式端点。所有 preset（官方 OpenAI / 中转站 / 各家兼容端点）都走这里，
差异只剩 base_url + model + api_key。

行为基线来自 ai_engine_relay（production active 路径），补充两点：
- base_url 为空时默认官方 OpenAI 端点（原 openai SDK 引擎的行为）
- max_tokens 被 400 拒绝且提示 max_completion_tokens 时（GPT-5 系推理模型），
  换参数重试一次并在本次调用内记住选择；中转站/兼容端点不受影响
"""
import json
import httpx
import re
from bot.tools import get_tools
from bot.prompts import build_tool_round_hint, PromptParts
from bot.database import Database
from bot.memory import MemoryService
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

# base_url 留空 = 官方 OpenAI（use_v1_suffix 逻辑会补上 /v1）
_DEFAULT_BASE_URL = "https://api.openai.com"


def _endpoint_url(preset: Preset) -> str:
    base_url = (preset.base_url or _DEFAULT_BASE_URL).rstrip("/")
    if preset.use_v1_suffix and not base_url.endswith("/v1"):
        base_url = base_url + "/v1"
    return f"{base_url}/chat/completions"


async def chat(db: Database, messages: list[dict], preset: Preset,
               send_callback=None, tool_callback=None, memory_service=None) -> str:
    return await _base_chat(db, messages, _call_with_tools, preset,
                            send_callback=send_callback, tool_callback=tool_callback,
                            memory_service=memory_service)


async def scheduled_action(db: Database, prompt: str, timestamp: str,
                           history: list[dict], preset: Preset,
                           send_callback=None, allow_silent: bool = False,
                           trigger: str | None = None,
                           tool_profile: str | None = None,
                           check_in_name: str | None = None,
                           context_config: dict | None = None,
                           memory_service=None) -> str | None:
    return await _base_scheduled_action(db, prompt, timestamp, history, _call_with_tools,
                                        preset,
                                        send_callback=send_callback,
                                        allow_silent=allow_silent, trigger=trigger,
                                        tool_profile=tool_profile,
                                        check_in_name=check_in_name,
                                        context_config=context_config,
                                        memory_service=memory_service)


async def simple_completion(prompt: str, preset: Preset) -> str:
    return await _base_simple_completion(prompt, _call_with_tools, preset)


def _log_usage(usage: dict | None) -> None:
    """cache 命中日志：兼容 OpenAI 官方（prompt_tokens_details.cached_tokens）
    与部分中转站直接平铺 cached_tokens 的返回。"""
    if not isinstance(usage, dict):
        return
    prompt_tokens = usage.get("prompt_tokens") or 0
    completion_tokens = usage.get("completion_tokens") or 0
    details = usage.get("prompt_tokens_details") or {}
    cached = (details.get("cached_tokens") if isinstance(details, dict) else 0) \
        or usage.get("cached_tokens") or 0
    logger.info(f"📊 Token usage: input={prompt_tokens}, output={completion_tokens}, "
                f"cached={cached}")
    if prompt_tokens > 0:
        logger.info(f"   cache_hit_rate={cached / prompt_tokens * 100:.1f}%")


async def _call_with_tools(db: Database, prompt: PromptParts | None, messages: list[dict],
                           preset: Preset,
                           send_callback=None, tool_callback=None,
                           tool_names: set | None = None,
                           memory_service: MemoryService | None = None) -> str:
    """httpx 直接调用 OpenAI 兼容端点，处理多轮 tool calling。"""
    model = preset.model
    url = _endpoint_url(preset)
    headers = {
        "Authorization": f"Bearer {preset.api_key}",
        "Content-Type": "application/json"
    }

    logger.info(f"🌐 OpenAI-compat URL: {url}")

    # 拍平 PromptParts 为单个字符串
    full_system = prompt.flatten() if prompt else ""

    # 按 tool_names 过滤工具子集
    tools = get_tools(tool_names)

    full_messages = [{"role": "system", "content": full_system}] + list(messages)
    all_texts = []  # 收集发送过的文本
    sent_display_texts = set()  # 去重集合
    # GPT-5 系官方模型拒绝 max_tokens；被明确拒绝后本次调用内换用新参数
    token_param = "max_tokens"

    test_mode.ensure_handler_state()

    async with httpx.AsyncClient() as client:
        for round_idx in range(5):
            while True:
                payload = {
                    "model": model,
                    token_param: 4096,
                    "messages": full_messages,
                }
                if tools:
                    payload["tools"] = tools

                test_mode.log_prompt(preset.provider, model, payload,
                                     round_num=round_idx + 1)

                try:
                    resp = await client.post(url, json=payload, headers=headers,
                                             timeout=120.0)
                except httpx.HTTPError as e:
                    logger.error(f"❌ Request failed: {type(e).__name__}: {e}")
                    raise AIProviderError(
                        f"OpenAI-compat request failed: {type(e).__name__}: {e}") from e

                if (resp.status_code == 400 and token_param == "max_tokens"
                        and "max_completion_tokens" in resp.text):
                    logger.info("🔁 端点拒绝 max_tokens，改用 max_completion_tokens 重试")
                    token_param = "max_completion_tokens"
                    continue
                break

            logger.info(f"🌐 status: {resp.status_code}")
            logger.info(f"🌐 body (first 500): {resp.text[:500]}")

            if resp.status_code != 200:
                logger.error(f"❌ API Error ({resp.status_code}): {resp.text[:500]}")
                raise AIProviderError(
                    f"OpenAI-compat API 错误 ({resp.status_code}): {resp.text[:200]}")

            try:
                data = resp.json()
            except Exception as e:
                logger.error(f"❌ JSON 解析失败: {e}, body={resp.text[:200]}")
                raise AIProviderError(f"OpenAI-compat 返回非 JSON 内容: {e}") from e

            test_mode.log_response(preset.provider, model, data,
                                   round_num=round_idx + 1)
            logger.info(f"🤖 raw response keys: {list(data.keys())}")

            choices = data.get("choices", [])
            if not choices:
                return "（内部错误：中转站未返回内容）"

            choice = choices[0]
            message = choice.get("message", {})
            finish_reason = choice.get("finish_reason", "")

            logger.info(f"🤖 finish_reason: {finish_reason}")

            round_text = (message.get("content") or "").strip()
            tool_calls = message.get("tool_calls") or []

            # 提取并记录 <think> / <thinking> 块
            think_blocks = re.findall(r'<think(?:ing)?>(.*?)</think(?:ing)?>', round_text, flags=re.DOTALL)
            think_content = "\n".join(b.strip() for b in think_blocks if b.strip()) if think_blocks else ""
            if think_content:
                logger.info(f"🤔 思考:\n{think_content}")

            display_text = re.sub(r'<think(?:ing)?>.*?</think(?:ing)?>', '', round_text, flags=re.DOTALL).strip()
            usage_log = data.get("usage")
            _log_usage(usage_log)

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

            # 中间轮：文字也直接发给用户（每一轮文字 = 给她看的）
            if display_text and display_text not in sent_display_texts:
                logger.info(f"💬 发送回复:\n{display_text}")
                if send_callback:
                    await send_callback(display_text)
                sent_display_texts.add(display_text)
                all_texts.append(display_text)

            # 把 assistant 的完整消息加入
            assistant_msg = {
                "role": "assistant",
                "tool_calls": tool_calls
            }
            if round_text:
                assistant_msg["content"] = round_text
            full_messages.append(assistant_msg)

            # 执行每个 tool
            called_names = []
            trace_tool_calls = []
            trace_tool_results = []
            for tc in tool_calls:
                func = tc.get("function", {})
                func_name = func.get("name", "")
                try:
                    func_args = json.loads(func.get("arguments", "{}"))
                except json.JSONDecodeError:
                    func_args = {}

                result = await _execute_tool_async(
                    db, func_name, func_args, memory_service=memory_service
                )
                called_names.append(func_name)
                tc_id = tc.get("id", "")
                trace_tool_calls.append({"name": func_name, "input": func_args, "id": tc_id})
                trace_tool_results.append({"name": func_name, "tool_use_id": tc_id, "result": result})

                full_messages.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": json.dumps(result, ensure_ascii=False)
                })

            # 在所有 tool 消息之后追加一条 system 风格的 user 消息
            # 夹带命中工具的定向 post-hint（TOOL_ROUND_REMINDER + per-tool 决策辅助）
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
