"""
AI 引擎模块 (Gemini 版本)
用 google-genai 官方 SDK 调用 Gemini API，处理 tool calling。

外壳（chat / scheduled_action / simple_completion）和 ai_engine_base 不变；
工具定义沿用 bot/tools.py::TOOLS（OpenAI 格式），转一层薄 schema 给 SDK。
"""
import re
from google import genai
from google.genai import types
from google.genai import errors as genai_errors
from bot.tools import TOOLS, get_tools
from bot.prompts import PromptParts
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


# 懒加载客户端缓存：按 api_key 存储
_clients: dict[str, genai.Client] = {}


def _get_client(api_key: str) -> genai.Client:
    if api_key not in _clients:
        _clients[api_key] = genai.Client(api_key=api_key)
    return _clients[api_key]


def _convert_type(schema):
    """OpenAI schema 的 type 字段是小写（"object" / "string"），
    Gemini 的 Schema 期望大写（OBJECT / STRING）。递归转换。"""
    if not isinstance(schema, dict):
        return schema
    new_schema = {}
    for k, v in schema.items():
        if k == "type" and isinstance(v, str):
            new_schema[k] = v.upper()
        elif isinstance(v, dict):
            new_schema[k] = _convert_type(v)
        elif isinstance(v, list):
            new_schema[k] = [_convert_type(i) for i in v]
        else:
            new_schema[k] = v
    return new_schema


def _build_gemini_tool(tool_names: set | None) -> types.Tool | None:
    source_tools = get_tools(tool_names)
    if not source_tools:
        return None
    decls = [
        types.FunctionDeclaration(
            name=t["function"]["name"],
            description=t["function"]["description"],
            parameters=_convert_type(t["function"]["parameters"]),
        )
        for t in source_tools
    ]
    return types.Tool(function_declarations=decls)


def _messages_to_contents(messages: list[dict]) -> list[types.Content]:
    """把内部消息格式（role + content:str）转为 SDK 的 types.Content。
    入口 messages 都是 str content（_ensure_valid_messages 已保证）。"""
    contents = []
    for m in messages:
        role = "user" if m["role"] == "user" else "model"
        text = m["content"] if isinstance(m["content"], str) else str(m["content"])
        contents.append(types.Content(role=role, parts=[types.Part(text=text)]))
    return contents


def _to_loggable(obj):
    """把 SDK pydantic 对象转成 JSON-friendly dict 给 test_mode 日志用。"""
    if hasattr(obj, "model_dump"):
        return obj.model_dump(exclude_none=True, mode="json")
    return obj


async def _call_with_tools(db: Database, prompt: PromptParts | None, messages: list[dict],
                           preset: Preset,
                           send_callback=None, tool_callback=None,
                           tool_names: set | None = None) -> str:
    """通过 google-genai SDK 调用 Gemini，处理多轮 tool calling。"""
    client = _get_client(preset.api_key)
    model = preset.model
    model_name = model if "gemini" in model.lower() else "gemini-2.0-flash"

    # 拍平 PromptParts；中间轮用精简版省 token，最后一轮用全量
    system_prompt = prompt.flatten() if prompt else ""
    concise_prompt = prompt.concise().flatten() if prompt else None

    gemini_tool = _build_gemini_tool(tool_names)
    tools_arg = [gemini_tool] if gemini_tool else None

    contents: list[types.Content] = _messages_to_contents(messages)
    all_texts: list[str] = []
    sent_display_texts: set[str] = set()
    is_intermediate = False  # 首轮发全量；有过工具调用后切精简

    test_mode.ensure_handler_state()

    for round_idx in range(5):
        current_prompt = (concise_prompt if is_intermediate and concise_prompt
                          else system_prompt)
        config = types.GenerateContentConfig(
            system_instruction=current_prompt,
            tools=tools_arg,
        )

        test_mode.log_prompt("gemini", model, {
            "model": model_name,
            "system_instruction": current_prompt,
            "contents": [_to_loggable(c) for c in contents],
            "tools": _to_loggable(gemini_tool) if gemini_tool else None,
        }, round_num=round_idx + 1)

        try:
            response = await client.aio.models.generate_content(
                model=model_name,
                contents=contents,
                config=config,
            )
        except genai_errors.APIError as e:
            logger.error(f"❌ Gemini API Error: {e}")
            raise AIProviderError(f"Gemini API 错误: {e}") from e

        test_mode.log_response("gemini", model, _to_loggable(response),
                               round_num=round_idx + 1)

        candidates = response.candidates or []
        if not candidates:
            raise AIProviderError("Gemini API 未返回有效 candidates")

        assistant_content = candidates[0].content
        parts = (assistant_content.parts if assistant_content else None) or []

        text_parts = []
        tool_calls = []
        for part in parts:
            if getattr(part, "text", None):
                text_parts.append(part.text)
            elif getattr(part, "function_call", None):
                tool_calls.append(part.function_call)

        round_text = "\n".join(text_parts).strip()

        # 提取并记录 <think> / <thinking> 块
        think_blocks = re.findall(r'<think(?:ing)?>(.*?)</think(?:ing)?>', round_text, flags=re.DOTALL)
        think_content = "\n".join(b.strip() for b in think_blocks if b.strip()) if think_blocks else ""
        if think_content:
            logger.info(f"🤔 思考:\n{think_content}")

        display_text = re.sub(r'<think(?:ing)?>.*?</think(?:ing)?>', '', round_text, flags=re.DOTALL).strip()
        usage_log = _to_loggable(getattr(response, "usage_metadata", None))

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
                usage=usage_log, stop_reason="end_turn",
            )
            return "\n".join(all_texts)

        # 中间轮：文字也直接发给用户（每一轮文字 = 给她看的）
        if display_text and display_text not in sent_display_texts:
            logger.info(f"💬 发送回复:\n{display_text}")
            if send_callback:
                await send_callback(display_text)
            sent_display_texts.add(display_text)
            all_texts.append(display_text)

        # 把 assistant 的完整 Content（含 function_call parts）追加进对话
        contents.append(assistant_content)

        # 执行工具，构造 functionResponse parts
        tool_response_parts = []
        called_names = []
        trace_tool_calls = []
        trace_tool_results = []
        for fc in tool_calls:
            func_name = fc.name
            func_args = dict(fc.args) if fc.args else {}

            desc = next((t["function"].get("description", "") for t in TOOLS
                         if t["function"]["name"] == func_name), "")
            desc_first = desc.split("。")[0] if desc else ""
            logger.info(f"🛠️ 调用工具: {func_name} | {desc_first}")
            logger.info(f"   参数: {func_args}")

            result = await _execute_tool_async(db, func_name, func_args)
            called_names.append(func_name)
            trace_tool_calls.append({"name": func_name, "input": func_args})
            trace_tool_results.append({"name": func_name, "result": result})
            tool_response_parts.append(
                types.Part.from_function_response(name=func_name, response=result)
            )

        contents.append(types.Content(role="user", parts=tool_response_parts))
        is_intermediate = True

        trace.add_round(
            raw_output=round_text, think=think_content, visible_text=display_text,
            tool_calls=trace_tool_calls, tool_results=trace_tool_results,
            usage=usage_log, stop_reason="tool_use",
        )

        if tool_callback and called_names:
            await tool_callback(called_names)

    return "\n".join(all_texts) or "（内部错误：工具调用次数过多）"
