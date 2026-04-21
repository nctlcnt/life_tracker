"""
AI 引擎模块 (Gemini 版本)
负责调用 Google Gemini API，处理 tool calling
"""
import httpx
import re
from bot.tools import TOOLS
from bot.prompts import PromptParts
from bot.database import Database
from bot.ai_provider_error import AIProviderError
from bot.ai_engine_base import (
    _execute_tool,
    chat as _base_chat, scheduled_action as _base_scheduled_action,
    simple_completion as _base_simple_completion,
)
from bot.logger import get_logger
from bot import test_mode
from config import Preset

logger = get_logger(__name__)


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


def _convert_to_gemini_format(messages: list[dict]) -> list[dict]:
    """将内部消息格式转换为 Gemini 原生格式"""
    gemini_messages = []
    for m in messages:
        role = "user" if m["role"] == "user" else "model"
        if isinstance(m["content"], list):
            gemini_messages.append({"role": role, "parts": m["content"]})
        else:
            gemini_messages.append({"role": role, "parts": [{"text": m["content"]}]})
    return gemini_messages


async def _call_with_tools(db: Database, prompt: PromptParts | None, messages: list[dict],
                           preset: Preset,
                           send_callback=None, tool_callback=None,
                           tool_names: set | None = None) -> str:
    """使用 httpx 直接调用 Gemini REST API。

    preset: 当前激活的 AI preset，提供 api_key 与 model。
    """
    api_key = preset.api_key
    model = preset.model
    model_name = model if "gemini" in model.lower() else "gemini-2.0-flash"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"

    # 拍平 PromptParts 为单个字符串（全量）；中间轮用精简版省 token，最后一轮用全量
    system_prompt = prompt.flatten() if prompt else ""
    concise_prompt = prompt.concise().flatten() if prompt else None

    # 转换工具格式
    def convert_type(schema):
        if not isinstance(schema, dict):
            return schema
        new_schema = {}
        for k, v in schema.items():
            if k == "type" and isinstance(v, str):
                new_schema[k] = v.upper()
            elif isinstance(v, dict):
                new_schema[k] = convert_type(v)
            elif isinstance(v, list):
                new_schema[k] = [convert_type(i) for i in v]
            else:
                new_schema[k] = v
        return new_schema

    # 按 tool_names 过滤工具子集
    source_tools = TOOLS
    if tool_names is not None:
        source_tools = [t for t in TOOLS if t["function"]["name"] in tool_names]

    gemini_tools = []
    if source_tools:
        decls = []
        for t in source_tools:
            decls.append({
                "name": t["function"]["name"],
                "description": t["function"]["description"],
                "parameters": convert_type(t["function"]["parameters"])
            })
        gemini_tools = [{"functionDeclarations": decls}]

    all_texts = []  # 收集发送过的文本
    sent_display_texts = set()

    test_mode.ensure_handler_state()

    async with httpx.AsyncClient() as client:
        current_messages = [m.copy() for m in messages]
        is_intermediate = False  # 首轮发全量；有过工具调用后切精简

        for round_idx in range(5):
            current_prompt = (concise_prompt if is_intermediate and concise_prompt
                              else system_prompt)
            gemini_payload = {
                "systemInstruction": {
                    "parts": [{"text": current_prompt}]
                },
                "contents": _convert_to_gemini_format(current_messages),
            }
            if gemini_tools:
                gemini_payload["tools"] = gemini_tools

            test_mode.log_prompt("gemini", model, gemini_payload, round_num=round_idx + 1)

            resp = await client.post(url, json=gemini_payload, timeout=60.0)
            if resp.status_code != 200:
                logger.error(f"❌ Gemini API Error: {resp.text[:500]}")
                raise AIProviderError(f"Gemini API 错误 ({resp.status_code}): {resp.text[:200]}")

            data = resp.json()
            test_mode.log_response("gemini", model, data, round_num=round_idx + 1)

            candidates = data.get("candidates", [])
            if not candidates:
                raise AIProviderError("Gemini API 未返回有效 candidates")

            first_candidate = candidates[0]
            parts = first_candidate.get("content", {}).get("parts", [])

            text_parts = []
            tool_calls = []

            for part in parts:
                if "text" in part:
                    text_parts.append(part["text"])
                elif "functionCall" in part:
                    tool_calls.append(part["functionCall"])

            round_text = "\n".join(text_parts).strip()
            
            # 提取并记录 <think> / <thinking> 块
            think_blocks = re.findall(r'<think(?:ing)?>(.*?)</think(?:ing)?>', round_text, flags=re.DOTALL)
            if think_blocks:
                think_content = "\n".join(b.strip() for b in think_blocks if b.strip())
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
                return "\n".join(all_texts)

            # 中间轮：文字也直接发给用户（每一轮文字 = 给她看的）
            if display_text and display_text not in sent_display_texts:
                logger.info(f"💬 发送回复:\n{display_text}")
                if send_callback:
                    await send_callback(display_text)
                sent_display_texts.add(display_text)
                all_texts.append(display_text)

            current_messages.append({
                "role": "model",
                "content": parts
            })

            # 执行工具
            tool_responses = []
            called_names = []
            for tc in tool_calls:
                func_name = tc.get("name")
                func_args = tc.get("args", {})
                
                desc = next((t["function"].get("description", "") for t in TOOLS if t["function"]["name"] == func_name), "")
                desc_first = desc.split("。")[0] if desc else ""
                logger.info(f"🛠️ 调用工具: {func_name} | {desc_first}")
                logger.info(f"   参数: {func_args}")
                
                result = _execute_tool(db, func_name, func_args)
                called_names.append(func_name)
                tool_responses.append({
                    "functionResponse": {
                        "name": func_name,
                        "response": result
                    }
                })
            current_messages.append({
                "role": "user",
                "content": tool_responses
            })

            is_intermediate = True  # 已有过工具调用，后续轮次用精简 prompt

            if tool_callback and called_names:
                await tool_callback(called_names)

        return "\n".join(all_texts) or "（内部错误：工具调用次数过多）"
