"""
AI 引擎模块 (中转站版)
使用 httpx 直接调用 OpenAI 兼容 API（中转站）
"""
import json
import httpx
from bot.tools import TOOLS, SYSTEM_PROMPT
from bot.database import Database
from bot.ai_engine_base import (
    _execute_tool,
    chat as _base_chat, scheduled_action as _base_scheduled_action,
    simple_completion as _base_simple_completion,
)
import config


async def chat(db: Database, user_message: str, timestamp: str,
               send_callback=None) -> str:
    return await _base_chat(db, user_message, timestamp, _call_with_tools, send_callback)


async def scheduled_action(db: Database, prompt: str, timestamp: str,
                           send_callback=None, allow_silent: bool = False) -> str | None:
    return await _base_scheduled_action(db, prompt, timestamp, _call_with_tools,
                                        send_callback, allow_silent)


async def simple_completion(prompt: str) -> str:
    return await _base_simple_completion(prompt, _call_with_tools)


async def _call_with_tools(db: Database, system_prompt: str, messages: list[dict],
                           send_callback=None, dynamic_context: str | None = None,
                           model: str | None = None, tool_names: set | None = None) -> str:
    """用 httpx 直接调用 OpenAI 兼容的中转站 API。"""
    if not model:
        model = config.CHAT_MODEL

    base_url = config.AI_BASE_URL.rstrip("/")
    if not base_url.endswith("/v1"):
        base_url = base_url + "/v1"
    url = f"{base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {config.AI_API_KEY}",
        "Content-Type": "application/json"
    }

    print(f"🌐 Relay URL: {url}")

    # system prompt 合并动态上下文
    full_system = system_prompt
    if dynamic_context:
        full_system += "\n\n" + dynamic_context

    # 按 tool_names 过滤工具子集
    tools = TOOLS
    if tool_names is not None:
        tools = [t for t in TOOLS if t["function"]["name"] in tool_names]

    full_messages = [{"role": "system", "content": full_system}] + list(messages)
    all_texts = []  # 收集所有轮次的文本

    async with httpx.AsyncClient() as client:
        for _ in range(5):
            payload = {
                "model": model,
                "max_tokens": 4096,
                "messages": full_messages,
            }
            if tools:
                payload["tools"] = tools

            resp = await client.post(url, json=payload, headers=headers, timeout=120.0)

            print(f"🌐 Relay status: {resp.status_code}")
            print(f"🌐 Relay body (first 500): {resp.text[:500]}")

            if resp.status_code != 200:
                print(f"❌ Relay API Error ({resp.status_code}): {resp.text[:500]}")
                return f"（内部错误：中转站 API 请求失败 {resp.status_code}）"

            try:
                data = resp.json()
            except Exception as e:
                print(f"❌ JSON 解析失败: {e}, body={resp.text[:200]}")
                return f"（内部错误：中转站返回非 JSON 内容）"

            print(f"🤖 raw response keys: {list(data.keys())}")

            choices = data.get("choices", [])
            if not choices:
                return "（内部错误：中转站未返回内容）"

            choice = choices[0]
            message = choice.get("message", {})
            finish_reason = choice.get("finish_reason", "")

            print(f"🤖 finish_reason: {finish_reason}")

            round_text = (message.get("content") or "").strip()
            tool_calls = message.get("tool_calls") or []

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

            # 把 assistant 的完整消息加入
            assistant_msg = {
                "role": "assistant",
                "tool_calls": tool_calls
            }
            if round_text:
                assistant_msg["content"] = round_text
            full_messages.append(assistant_msg)

            # 执行每个 tool
            for tc in tool_calls:
                func = tc.get("function", {})
                func_name = func.get("name", "")
                try:
                    func_args = json.loads(func.get("arguments", "{}"))
                except json.JSONDecodeError:
                    func_args = {}

                result = _execute_tool(db, func_name, func_args)

                full_messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": json.dumps(result, ensure_ascii=False)
                })

    return "（内部错误：工具调用次数过多）"
