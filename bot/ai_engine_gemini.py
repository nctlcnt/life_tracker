"""
AI 引擎模块 (Gemini 版本)
负责调用 Google Gemini API，处理 tool calling，执行工具
适配 Gemini 原生的发送格式 (REST API)
"""
import json
import httpx
from bot.tools import TOOLS, SYSTEM_PROMPT
from bot.database import Database
import config


def _build_dynamic_context(db: Database) -> str:
    """构建动态上下文（记忆 + 进行中事件）"""
    parts = []

    # 记忆
    memories = db.get_all_memories()
    if memories:
        lines = [f"- [id={m['id']}] {m['content']}" for m in memories]
        parts.append("【你现在记着的事】\n" + "\n".join(lines))

    # 进行中的事件
    ongoing = db.get_ongoing_events(limit=5)
    if ongoing:
        lines = [
            f"- [ID={e['id']}] {e['start_time']} | {e['category']} | {e['content']}"
            + (f" | 备注: {e['notes']}" if e.get('notes') else "")
            for e in ongoing
        ]
        parts.append(
            f"【当前进行中的事件（end_time 为空）】\n" + "\n".join(lines) + "\n"
            f"如果用户提到的活动与上述事件相同，请用 update_timeline_event 更新 end_time，不要新建。"
        )

    return "\n\n".join(parts)


async def chat(db: Database, user_message: str, timestamp: str,
               send_callback=None) -> str:
    pending_id = db.add_pending_message(user_message, timestamp)
    db.add_message("user", f"[{timestamp}] {user_message}")

    messages = _build_messages(db)
    dynamic_ctx = _build_dynamic_context(db)

    pending = db.get_pending_messages()
    earlier_pending = pending[:-1] if pending else []
    if earlier_pending:
        lines = "\n".join(f"- [{p['timestamp']}] {p['content']}" for p in earlier_pending)
        if dynamic_ctx:
            dynamic_ctx += f"\n\n【注意】以下消息之前因服务不可用未能处理，请一并处理：\n{lines}"
        else:
            dynamic_ctx = f"【注意】以下消息之前因服务不可用未能处理，请一并处理：\n{lines}"

    reply = await _call_with_tools(
        db, SYSTEM_PROMPT, messages,
        send_callback=send_callback,
        dynamic_context=dynamic_ctx or None,
        model=config.CHAT_MODEL
    )

    db.add_message("assistant", reply)
    db.delete_pending_message(pending_id)
    return reply


async def proactive_check(db: Database, timestamp: str,
                          send_callback=None) -> str | None:
    recent = db.get_recent_messages(limit=10)
    if not recent:
        return None

    dynamic_ctx = _build_dynamic_context(db)

    messages = [
        *[{"role": m["role"], "content": m["content"]} for m in recent],
        {
            "role": "user",
            "content": (
                f"[系统轮询 {timestamp}]\n"
                f"现在是悉尼时间 {timestamp}。根据对话上下文、当前时间"
                f"和你的记忆，选择一个行动：\n\n"
                f"1. **聊几句**：接之前话题、随便扯点什么、"
                f"对她提到过的事表示好奇，像朋友发微信一样\n"
                f"2. **关心一下**：该吃饭了、该休息了、之前说哪里不舒服\n"
                f"3. **提一嘴记忆里的事**：临近的 deadline、"
                f"之前说要注意的事，自然带出来别像念清单\n"
                f"4. **[SILENT]**：仅限以下情况——"
                f"用户明确说了要睡觉 / 30分钟内刚聊过 / "
                f"凌晨2点到早上8点\n\n"
                f"大多数时候选 1-3，找个自然的切入点。"
                f"不要打招呼或问'在吗'，直接说内容。"
            )
        }
    ]

    messages = _ensure_valid_messages(messages)

    reply = await _call_with_tools(
        db, SYSTEM_PROMPT, messages,
        dynamic_context=dynamic_ctx or None,
        model=config.POLL_MODEL
    )

    if reply and "[SILENT]" not in reply:
        if send_callback:
            await send_callback(reply)
        db.add_message("assistant", reply)
        return reply
    return None


async def reminder_action(db: Database, action: str, timestamp: str,
                          send_callback=None) -> str:
    recent = db.get_recent_messages(limit=10)
    dynamic_ctx = _build_dynamic_context(db)

    messages = [
        *[{"role": m["role"], "content": m["content"]} for m in recent],
        {
            "role": "user",
            "content": f"[提醒触发 {timestamp}] 之前设置的提醒已到时间。\n"
                       f"提醒内容：{action}\n"
                       f"请根据这个提醒向用户发消息。\n"
                       f"⚠️ 警告：这是最终触发回合，你必须直接在回复中说出提醒内容，绝对不要用 set_reminder 再把同样的提醒设一遍！"
        }
    ]

    messages = _ensure_valid_messages(messages)

    reply = await _call_with_tools(
        db, SYSTEM_PROMPT, messages, send_callback,
        dynamic_context=dynamic_ctx or None,
        model=config.POLL_MODEL
    )
    db.add_message("assistant", reply)
    return reply


def _ensure_valid_messages(messages: list[dict]) -> list[dict]:
    if not messages:
        return messages

    while messages and messages[0]["role"] != "user":
        messages = messages[1:]

    merged = []
    for msg in messages:
        if merged and merged[-1]["role"] == msg["role"]:
            merged[-1]["content"] += "\n" + msg["content"]
        else:
            merged.append(msg.copy())

    return merged


def _build_messages(db: Database) -> list[dict]:
    recent = db.get_recent_messages(limit=10)
    messages = [{"role": m["role"], "content": m["content"]} for m in recent]
    return _ensure_valid_messages(messages)


def _convert_to_gemini_format(messages: list[dict]) -> list[dict]:
    """将内部消息格式转换为 Gemini 原生格式"""
    gemini_messages = []
    for m in messages:
        # Gemini 的角色是 'user' 和 'model'
        role = "user" if m["role"] == "user" else "model"
        
        # 内部处理只发文本的简单转换
        # 如果内部记录已经是 Gemini 发送格式的对象，则直接保留（例如下面包含 tool_calls 的回合）
        if isinstance(m["content"], list):
            gemini_messages.append({"role": role, "parts": m["content"]})
        else:
            gemini_messages.append({"role": role, "parts": [{"text": m["content"]}]})
            
    return gemini_messages


async def _call_with_tools(db: Database, system_prompt: str, messages: list[dict],
                           send_callback=None, dynamic_context: str | None = None,
                           model: str | None = None) -> str:
    """
    使用 httpx 直接调用 Gemini REST API
    """
    api_key = config.AI_API_KEY
    if not model:
        model = getattr(config, 'CHAT_MODEL', 'gemini-2.0-flash')
        
    # 如果用户配置里写的是 claude，就回退到 gemini-2.0-flash
    model_name = model if "gemini" in model.lower() else "gemini-2.0-flash"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"

    # 合并 System Prompt
    full_system_prompt = system_prompt
    if dynamic_context:
        full_system_prompt += "\n\n" + dynamic_context

    # 提取 OpenAI 格式并转换为 Gemini 格式的 tools
    def convert_type(schema):
        """将 OpenAI schema 转为 Gemini 接受的大写 TYPE"""
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

    gemini_tools = [{"functionDeclarations": []}]
    for t in TOOLS:
        gemini_tools[0]["functionDeclarations"].append({
            "name": t["function"]["name"],
            "description": t["function"]["description"],
            "parameters": convert_type(t["function"]["parameters"])
        })

    pending_text = None  # 暂存中间轮文本

    async with httpx.AsyncClient() as client:
        # 复制一份 messages 防止污染原始数据
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
            
            # 检查返回
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
            finish_reason = first_candidate.get("finishReason", "")

            # 最后一轮（没有 tool_call，正常结束）
            if not tool_calls:
                final_text = round_text or pending_text or ""
                if final_text and send_callback:
                    await send_callback(final_text)
                return final_text

            # 如果有 tool_call，则需要中间轮处理
            if round_text:
                print(f"💭 中间轮文本（暂存）: {round_text}")
                pending_text = round_text

            # 把大模型的完整返回记录到消息体中
            current_messages.append({
                "role": "model",  # _convert_to_gemini_format 已经做了映射，内部先用 assistant，但 parts 包含完整结构
                "content": parts  # 存放 parts 列表，_convert_to_gemini_format 碰到 list 会直接包在 parts 里
            })

            # 执行每个 tool，并准备回调数据
            tool_responses = []
            for tc in tool_calls:
                func_name = tc.get("name")
                # Gemini 的 args 可能为空，需要处理
                func_args = tc.get("args", {})
                
                result = _execute_tool(db, func_name, func_args)
                
                # Gemini 原生回调格式
                tool_responses.append({
                    "functionResponse": {
                        "name": func_name,
                        "response": result
                    }
                })

            # 把执行结果作为用户输入传回
            current_messages.append({
                "role": "user",
                "content": tool_responses
            })

        return "\n".join(text_parts) or "（内部错误：工具调用次数过多）"


def _execute_tool(db: Database, tool_name: str, args: dict) -> dict:
    """工具执行逻辑，同 Claude 版本"""
    if tool_name == "log_timeline_event":
        event_id = db.add_event(
            start_time=args["start_time"],
            end_time=args.get("end_time"),
            content=args["content"],
            category=args.get("category", "uncategorized"),
            notes=args.get("notes"),
            session_id=args.get("session_id")
        )
        old_id = args.get("session_id")
        if old_id:
            db.update_event(old_id, session_id=old_id)
        return {"success": True, "event_id": event_id, "message": "事件已记录"}

    elif tool_name == "set_reminder":
        reminder_id = db.add_reminder(
            trigger_time=args["trigger_time"],
            action=args["action"]
        )
        return {"success": True, "reminder_id": reminder_id, "message": "提醒已设置"}

    elif tool_name == "query_timeline":
        events = db.get_events(start=args["start"], end=args["end"])
        return {"success": True, "events": events, "count": len(events)}

    elif tool_name == "update_timeline_event":
        fields = {k: args[k] for k in ("end_time", "content", "category", "notes") if k in args}
        ok = db.update_event(args.get("event_id"), **fields)
        if ok:
            return {"success": True, "message": "事件已更新"}
        return {"success": False, "message": f"未找到 event_id={args.get('event_id')}"}

    elif tool_name == "save_memory":
        memory_id = db.add_memory(args["content"])
        return {"status": "ok", "memory_id": memory_id}

    elif tool_name == "delete_memory":
        db.delete_memory(args.get("memory_id"))
        return {"status": "ok"}

    elif tool_name == "update_memory":
        db.update_memory(args.get("memory_id"), args["content"])
        return {"status": "ok"}

    else:
        return {"success": False, "message": f"未知工具: {tool_name}"}
