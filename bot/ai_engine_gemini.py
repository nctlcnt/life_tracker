"""
AI 引擎模块 (Gemini 版本)
负责调用 Google Gemini API，处理 tool calling
"""
import httpx
from bot.tools import (
    TOOLS, TOOL_ROUND_REMINDER, SYSTEM_PROMPT_CONCISE,
    WRITE_ONLY_TOOL_NAMES, PERSONA_MARKER,
)
from bot.database import Database
from bot.ai_engine_base import (
    _execute_tool,
    chat as _base_chat, scheduled_action as _base_scheduled_action,
    simple_completion as _base_simple_completion,
)
from bot.logger import get_logger
import config

logger = get_logger(__name__)


async def chat(db: Database, messages: list[dict],
               send_callback=None) -> str:
    return await _base_chat(db, messages, _call_with_tools, send_callback)


async def scheduled_action(db: Database, prompt: str, timestamp: str,
                           history: list[dict],
                           send_callback=None, allow_silent: bool = False) -> str | None:
    return await _base_scheduled_action(db, prompt, timestamp, history, _call_with_tools,
                                        send_callback, allow_silent)


async def simple_completion(prompt: str) -> str:
    return await _base_simple_completion(prompt, _call_with_tools)


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


async def _call_with_tools(db: Database, system_prompt: str, messages: list[dict],
                           send_callback=None, dynamic_context: str | None = None,
                           model: str | None = None, tool_names: set | None = None) -> str:
    """使用 httpx 直接调用 Gemini REST API"""
    api_key = config.AI_API_KEY
    if not model:
        model = getattr(config, 'CHAT_MODEL', 'gemini-2.0-flash')

    model_name = model if "gemini" in model.lower() else "gemini-2.0-flash"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"

    # 合并 System Prompt
    full_system_prompt = system_prompt
    if dynamic_context:
        full_system_prompt += "\n\n" + dynamic_context

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

    all_texts = []  # 收集所有轮次的文本

    async with httpx.AsyncClient() as client:
        current_messages = [m.copy() for m in messages]

        for round_idx in range(5):
            # 动态决定当前轮次的 System Prompt
            # 如果是中间轮次（>0），并且基础模板是核心人格模板，就换成精简版以节省大量由于"解释怎么使用工具"导致的 Token 浪费
            current_prompt = full_system_prompt
            if round_idx > 0 and PERSONA_MARKER in system_prompt:
                current_prompt = SYSTEM_PROMPT_CONCISE
                if dynamic_context:
                    current_prompt += "\n\n" + dynamic_context

            gemini_payload = {
                "systemInstruction": {
                    "parts": [{"text": current_prompt}]
                },
                "contents": _convert_to_gemini_format(current_messages),
            }
            if gemini_tools:
                gemini_payload["tools"] = gemini_tools

            resp = await client.post(url, json=gemini_payload, timeout=60.0)
            if resp.status_code != 200:
                logger.error(f"❌ Gemini API Error: {resp.text}")
                return f"（内部错误：API 请求失败 {resp.status_code}）"

            data = resp.json()

            candidates = data.get("candidates", [])
            if not candidates:
                return "（内部错误：Gemini 未返回内容）"

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

            # 最后一轮（没有 tool_call）
            if not tool_calls:
                if round_text:
                    if send_callback:
                        await send_callback(round_text)
                    all_texts.append(round_text)
                return "\n".join(all_texts)

            # 中间轮：发送文本，继续处理 tool calling
            if round_text:
                logger.info(f"💬 中间轮文本: {round_text}")
                if send_callback:
                    await send_callback(round_text)
                all_texts.append(round_text)

            current_messages.append({
                "role": "model",
                "content": parts
            })

            # 执行工具
            tool_responses = []
            for tc in tool_calls:
                func_name = tc.get("name")
                func_args = tc.get("args", {})
                result = _execute_tool(db, func_name, func_args)
                tool_responses.append({
                    "functionResponse": {
                        "name": func_name,
                        "response": result
                    }
                })
            # 在 functionResponse 之后追加一条 text part 作为系统提示，
            # 防止模型在下一轮重复之前已发送的内容
            tool_responses.append({"text": TOOL_ROUND_REMINDER})

            current_messages.append({
                "role": "user",
                "content": tool_responses
            })

            # 如果本次调用的全部是无状态的"单向写入工具"，且已有文本回复，
            # 第二轮通常只会生成类似"好了"的废话废 token，直接提前结束
            is_all_write = all(tc.get("name") in WRITE_ONLY_TOOL_NAMES for tc in tool_calls)
            if is_all_write and round_text:
                logger.info("⚡ 检测到全写入操作且已有文本回复，主动跳过后续无意义的 API 请求以节省 token")
                return "\n".join(all_texts)

        return "\n".join(all_texts) or "（内部错误：工具调用次数过多）"
