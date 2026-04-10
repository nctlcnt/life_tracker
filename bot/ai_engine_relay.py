"""
AI 引擎模块 (中转站版)
使用 httpx 直接调用 OpenAI 兼容 API（中转站）
不依赖 OpenAI SDK，兼容性最好
"""
import json
import httpx
from bot.tools import TOOLS, SYSTEM_PROMPT
from bot.database import Database
import config


def _build_dynamic_context(db: Database) -> str:
    """构建动态上下文（记忆 + 进行中事件）"""
    parts = []

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

    # 待触发提醒计划
    reminders = db.list_active_reminders()
    if reminders:
        lines = [f"- [{r['priority']}] {r['trigger_time']} | {r['action']} (group: {r.get('group_id', '无')})" for r in reminders]
        parts.append("【待触发的跟进计划】\n" + "\n".join(lines))

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


async def _call_with_tools(db: Database, system_prompt: str, messages: list[dict],
                           send_callback=None, dynamic_context: str | None = None,
                           model: str | None = None) -> str:
    """
    用 httpx 直接调用 OpenAI 兼容的中转站 API。
    不走 OpenAI SDK，兼容性最好。
    """
    if not model:
        model = config.CHAT_MODEL

    base_url = config.AI_BASE_URL.rstrip("/")
    # 自动补 /v1 如果 base_url 里没有
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

    # 构建完整的消息列表
    full_messages = [{"role": "system", "content": full_system}] + list(messages)

    pending_text = None

    async with httpx.AsyncClient() as client:
        for _ in range(5):
            payload = {
                "model": model,
                "max_tokens": 4096,
                "messages": full_messages,
                "tools": TOOLS,
            }

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
                final_text = round_text or pending_text or ""
                if final_text and send_callback:
                    await send_callback(final_text)
                return final_text

            # 中间轮：暂存文本
            if round_text:
                print(f"💭 中间轮文本（暂存）: {round_text}")
                pending_text = round_text

            # 把 assistant 的完整消息加入（清理空 content 和非标准字段）
            assistant_msg = {
                "role": "assistant",
                "tool_calls": tool_calls
            }
            # content 必须非空才加，否则中转站会报 "text content blocks must be non-empty"
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


def _execute_tool(db: Database, tool_name: str, args: dict) -> dict:
    """工具执行逻辑"""
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
            action=args["action"],
            group_id=args.get("group_id"),
            priority=args.get("priority", "normal")
        )
        return {"success": True, "reminder_id": reminder_id, "message": "提醒已设置"}

    elif tool_name == "cancel_reminders":
        count = db.cancel_reminders_by_group(args.get("group_id"))
        return {"success": True, "cancelled_count": count, "message": "相关提醒已取消"}

    elif tool_name == "list_reminders":
        rems = db.list_active_reminders()
        return {"success": True, "reminders": rems, "count": len(rems)}

    elif tool_name == "query_timeline":
        events = db.get_events(start=args["start"], end=args["end"])
        return {"success": True, "events": events, "count": len(events)}

    elif tool_name == "update_timeline_event":
        fields = {k: args[k] for k in ("end_time", "content", "category", "notes") if k in args}
        ok = db.update_event(args["event_id"], **fields)
        if ok:
            return {"success": True, "message": "事件已更新"}
        return {"success": False, "message": f"未找到 event_id={args['event_id']}"}

    elif tool_name == "save_memory":
        memory_id = db.add_memory(args["content"])
        return {"status": "ok", "memory_id": memory_id}

    elif tool_name == "delete_memory":
        db.delete_memory(args["memory_id"])
        return {"status": "ok"}

    elif tool_name == "update_memory":
        db.update_memory(args["memory_id"], args["content"])
        return {"status": "ok"}

    else:
        return {"success": False, "message": f"未知工具: {tool_name}"}
