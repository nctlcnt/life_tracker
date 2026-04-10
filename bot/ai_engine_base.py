"""
AI 引擎公共模块
提取三个引擎（Claude / Gemini / Relay）共享的逻辑：
- 动态上下文构建
- 消息格式处理
- 工具执行
- chat / proactive_check / reminder_action 高层流程
"""
from bot.tools import SYSTEM_PROMPT
from bot.database import Database
import config


def _build_dynamic_context(db: Database) -> str:
    """
    构建动态上下文（记忆 + 进行中事件 + 待触发提醒），每次调用注入。
    """
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

    # 待触发提醒计划
    reminders = db.list_active_reminders()
    if reminders:
        lines = [f"- [{r['priority']}] {r['trigger_time']} | {r['action']} (group: {r.get('group_id', '无')})" for r in reminders]
        parts.append("【待触发的跟进计划】\n" + "\n".join(lines))

    return "\n\n".join(parts)


def _ensure_valid_messages(messages: list[dict]) -> list[dict]:
    """
    确保消息列表合法：
    1. 第一条消息必须是 user
    2. 合并连续的同角色消息
    """
    if not messages:
        return messages

    # 确保第一条是 user
    while messages and messages[0]["role"] != "user":
        messages = messages[1:]

    # 合并连续的同角色消息
    merged = []
    for msg in messages:
        if merged and merged[-1]["role"] == msg["role"]:
            merged[-1]["content"] += "\n" + msg["content"]
        else:
            merged.append(msg.copy())

    return merged


def _build_messages(db: Database) -> list[dict]:
    """构建发给大模型的消息列表（纯历史消息）。"""
    recent = db.get_recent_messages(limit=10)
    messages = [{"role": m["role"], "content": m["content"]} for m in recent]
    return _ensure_valid_messages(messages)


def _execute_tool(db: Database, tool_name: str, args: dict) -> dict:
    """执行具体的工具调用，返回结果"""
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


# ── 高层流程：chat / proactive_check / reminder_action ──────────────


async def chat(db: Database, user_message: str, timestamp: str,
               call_with_tools_fn, send_callback=None) -> str:
    """
    处理用户消息的完整流程。
    call_with_tools_fn: 各引擎的 _call_with_tools 实现。
    """
    # 先登记为待处理，防止 AI 调用失败时消息丢失
    pending_id = db.add_pending_message(user_message, timestamp)

    # 保存用户消息到对话记录
    db.add_message("user", f"[{timestamp}] {user_message}")

    # 构建消息列表
    messages = _build_messages(db)

    # 构建动态上下文
    dynamic_ctx = _build_dynamic_context(db)

    # 检查是否有历史未处理消息
    pending = db.get_pending_messages()
    earlier_pending = pending[:-1] if pending else []
    if earlier_pending:
        lines = "\n".join(
            f"- [{p['timestamp']}] {p['content']}" for p in earlier_pending
        )
        if dynamic_ctx:
            dynamic_ctx += f"\n\n【注意】以下消息之前因服务不可用未能处理，请一并处理：\n{lines}"
        else:
            dynamic_ctx = f"【注意】以下消息之前因服务不可用未能处理，请一并处理：\n{lines}"

    # 调用大模型（可能需要多轮 tool calling）
    reply = await call_with_tools_fn(
        db, SYSTEM_PROMPT, messages,
        send_callback=send_callback,
        dynamic_context=dynamic_ctx or None,
        model=config.CHAT_MODEL
    )

    # 保存 AI 回复到数据库
    db.add_message("assistant", reply)

    # 处理成功，删除 pending 记录
    db.delete_pending_message(pending_id)

    return reply


async def proactive_check(db: Database, timestamp: str,
                          call_with_tools_fn, send_callback=None) -> str | None:
    """
    随机轮询时调用：让 AI 根据上下文决定是否需要主动发消息。
    返回 None 表示不需要发消息。
    """
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

    # 不传 send_callback，因为需要先检查 [SILENT] 再决定是否发送
    reply = await call_with_tools_fn(
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
                          call_with_tools_fn, send_callback=None) -> str:
    """提醒触发时调用：让 AI 根据提醒内容决定做什么"""
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

    reply = await call_with_tools_fn(
        db, SYSTEM_PROMPT, messages, send_callback,
        dynamic_context=dynamic_ctx or None,
        model=config.POLL_MODEL
    )
    db.add_message("assistant", reply)
    return reply
