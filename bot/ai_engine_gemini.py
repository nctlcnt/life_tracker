"""
AI 引擎模块 (Gemini 版本)
负责调用 Google Gemini API，处理 tool calling
"""
import httpx
from bot.tools import TOOLS
from bot.database import Database
from bot.ai_engine_base import (
    _execute_tool,
    chat as _base_chat, scheduled_action as _base_scheduled_action,
)
import config


async def chat(db: Database, user_message: str, timestamp: str,
               send_callback=None) -> str:
    return await _base_chat(db, user_message, timestamp, _call_with_tools, send_callback)


async def scheduled_action(db: Database, prompt: str, timestamp: str,
                           send_callback=None, allow_silent: bool = False) -> str | None:
    return await _base_scheduled_action(db, prompt, timestamp, _call_with_tools,
                                        send_callback, allow_silent)


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
    if tool_names:
        source_tools = [t for t in TOOLS if t["function"]["name"] in tool_names]

    gemini_tools = [{"functionDeclarations": []}]
    for t in source_tools:
        gemini_tools[0]["functionDeclarations"].append({
            "name": t["function"]["name"],
            "description": t["function"]["description"],
            "parameters": convert_type(t["function"]["parameters"])
        })

    all_texts = []  # 收集所有轮次的文本

    async with httpx.AsyncClient() as client:
        current_messages = [m.copy() for m in messages]

        for _ in range(5):
            gemini_payload = {
                "systemInstruction": {
                    "parts": [{"text": full_system_prompt}]
                },
                "contents": _convert_to_gemini_format(current_messages),
                "tools": gemini_tools
            }

            resp = await client.post(url, json=gemini_payload, timeout=60.0)
            if resp.status_code != 200:
                print(f"❌ Gemini API Error: {resp.text}")
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
                print(f"💬 中间轮文本: {round_text}")
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

            current_messages.append({
                "role": "user",
                "content": tool_responses
            })

        return "\n".join(all_texts) or "（内部错误：工具调用次数过多）"
