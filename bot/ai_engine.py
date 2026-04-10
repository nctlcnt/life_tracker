"""
AI 引擎模块
负责调用大模型 API，处理 tool calling，执行工具
适配 Anthropic Claude API 格式
"""
import json
from anthropic import AsyncAnthropic
from bot.tools import TOOLS_ANTHROPIC, SYSTEM_PROMPT
from bot.database import Database
import config

client = AsyncAnthropic(
    api_key=config.AI_API_KEY,
    base_url=config.AI_BASE_URL
)


async def chat(db: Database, user_message: str, timestamp: str,
               send_callback=None) -> str:
    """
    处理用户消息的完整流程：
    1. 构建上下文（system prompt + 历史消息）
    2. 调用大模型
    3. 如果大模型返回 tool_call，执行工具并再次调用
    4. 每轮有文本就立刻通过 send_callback 发出
    5. 返回所有轮次的完整回复文本（用于保存到数据库）
    """
    # 先登记为待处理，防止 AI 调用失败时消息丢失
    pending_id = db.add_pending_message(user_message, timestamp)

    # 保存用户消息到对话记录（AI 可见）
    db.add_message("user", f"[{timestamp}] {user_message}")

    # 构建消息列表（静态 prompt 可缓存，动态上下文每次变化）
    static_prompt, dynamic_context, messages = _build_messages(db, user_message, timestamp)

    # 调用大模型（可能需要多轮 tool calling）
    reply = await _call_with_tools(db, static_prompt, messages, send_callback,
                                    dynamic_context=dynamic_context)

    # 保存 AI 回复到数据库
    db.add_message("assistant", reply)

    # 处理成功，删除 pending 记录
    db.delete_pending_message(pending_id)

    return reply


async def proactive_check(db: Database, timestamp: str,
                          send_callback=None) -> str | None:
    """
    随机轮询时调用：让 AI 根据上下文决定是否需要主动发消息
    返回 None 表示不需要发消息
    """
    recent = db.get_recent_messages(limit=10)
    if not recent:
        return None

    messages = [
        *[{"role": m["role"], "content": m["content"]} for m in recent],
        {
            "role": "user",
            "content": (
                f"[系统轮询 {timestamp}]\n"
                f"现在是悉尼时间 {timestamp}。根据对话上下文和当前时间，"
                f"选择一个合适的行动：\n\n"
                f"1. **聊几句**：分享一个有趣的想法、接着之前的话题聊、"
                f"随便扯点什么，像朋友发微信一样自然\n"
                f"2. **关心一下**：该吃饭了、该休息了、之前说肚子疼好点没\n"
                f"3. **提醒/催促**：之前说要做什么但可能忘了，或者待办事件里有快到期的\n"
                f"4. **[SILENT]**：仅在以下情况使用——"
                f"用户明确说了要睡觉/刚聊过没多久(30分钟内)/"
                f"凌晨2点到早上8点之间\n\n"
                f"大多数时候你应该选 1-3，找个自然的切入点聊。"
                f"不要打招呼或问'在吗'，直接说内容。"
            )
        }
    ]

    # 确保消息不以 assistant 开头（Anthropic 要求第一条必须是 user）
    messages = _ensure_valid_messages(messages)

    # 构建动态上下文（待办事件等）
    dynamic_parts = []
    pending_events = db.get_active_pending_events()
    if pending_events:
        lines = "\n".join(
            f"- [ID={e['id']}] {e['description']}"
            + (f" | 截止: {e['due_date']}" if e.get('due_date') else "")
            + (f" | 备注: {e['notes']}" if e.get('notes') else "")
            for e in pending_events
        )
        dynamic_parts.append(f"【当前待办事件】\n{lines}")

    dynamic_context = "\n\n".join(dynamic_parts) if dynamic_parts else None

    # 不传 send_callback，因为需要先检查 [SILENT] 再决定是否发送
    reply = await _call_with_tools(db, SYSTEM_PROMPT, messages,
                                    dynamic_context=dynamic_context)

    if "[SILENT]" in reply:
        return None

    # 确认不是 SILENT 后再发送
    if reply and send_callback:
        await send_callback(reply)

    db.add_message("assistant", reply)
    return reply


async def reminder_action(db: Database, action: str, timestamp: str,
                          send_callback=None) -> str:
    """
    提醒触发时调用：让 AI 根据提醒内容决定做什么
    """
    recent = db.get_recent_messages(limit=10)
    messages = [
        *[{"role": m["role"], "content": m["content"]} for m in recent],
        {
            "role": "user",
            "content": f"[提醒触发 {timestamp}] 之前设置的提醒已到时间，"
                       f"提醒内容：{action}。请根据这个提醒采取行动。"
        }
    ]

    messages = _ensure_valid_messages(messages)

    reply = await _call_with_tools(db, SYSTEM_PROMPT, messages, send_callback)
    db.add_message("assistant", reply)
    return reply


def _ensure_valid_messages(messages: list[dict]) -> list[dict]:
    """
    Anthropic 要求：
    1. 第一条消息必须是 user
    2. user 和 assistant 必须交替出现（不能连续两条 user 或两条 assistant）
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
            # 合并内容
            merged[-1]["content"] += "\n" + msg["content"]
        else:
            merged.append(msg.copy())

    return merged


def _build_messages(db: Database, user_message: str, timestamp: str) -> tuple[str, str | None, list[dict]]:
    """
    构建发给大模型的 system prompt 和消息列表
    返回 (static_prompt, dynamic_context, messages)
    - static_prompt: 不变的 SYSTEM_PROMPT，用于 prompt caching
    - dynamic_context: 每次变化的上下文（pending 消息、进行中事件），不缓存
    """
    recent = db.get_recent_messages(limit=10)

    # 构建动态上下文
    dynamic_parts = []

    # 检查是否有历史未处理消息
    pending = db.get_pending_messages()
    earlier_pending = pending[:-1] if pending else []
    if earlier_pending:
        lines = "\n".join(
            f"- [{p['timestamp']}] {p['content']}" for p in earlier_pending
        )
        dynamic_parts.append(
            f"【注意】以下消息之前因服务不可用未能处理，请一并处理：\n{lines}"
        )

    # 附加当前未结束的事件，帮助 AI 判断是更新还是新建
    ongoing = db.get_ongoing_events(limit=5)
    if ongoing:
        lines = "\n".join(
            f"- [ID={e['id']}] {e['start_time']} | {e['category']} | {e['content']}"
            + (f" | 备注: {e['notes']}" if e.get('notes') else "")
            for e in ongoing
        )
        dynamic_parts.append(
            f"【当前进行中的事件（end_time 为空）】\n{lines}\n"
            f"如果用户提到的活动与上述事件相同，请用 update_timeline_event 更新 end_time，不要新建。"
        )

    # 附加待办事件列表
    pending_events = db.get_active_pending_events()
    if pending_events:
        lines = "\n".join(
            f"- [ID={e['id']}] {e['description']}"
            + (f" | 截止: {e['due_date']}" if e.get('due_date') else "")
            + (f" | 备注: {e['notes']}" if e.get('notes') else "")
            for e in pending_events
        )
        dynamic_parts.append(
            f"【当前待办事件】\n{lines}"
        )

    dynamic_context = "\n\n".join(dynamic_parts) if dynamic_parts else None

    messages = [{"role": m["role"], "content": m["content"]} for m in recent]
    messages = _ensure_valid_messages(messages)

    return SYSTEM_PROMPT, dynamic_context, messages


async def _call_with_tools(db: Database, system_prompt: str, messages: list[dict],
                           send_callback=None, dynamic_context: str | None = None) -> str:
    """
    调用 Anthropic Claude，处理可能的多轮 tool calling。
    每轮产生的文本会立刻通过 send_callback 发送到 Discord，
    同时收集所有轮次的文本，最终返回完整回复（用于保存到数据库）。

    使用 Anthropic prompt caching：静态 system_prompt 标记 cache_control，
    动态 dynamic_context 不缓存。
    """
    # 构建 system blocks，静态部分开启 prompt caching
    system_blocks = [
        {
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral"}
        }
    ]
    if dynamic_context:
        system_blocks.append({
            "type": "text",
            "text": dynamic_context
        })

    all_text_parts = []  # 收集所有轮次的文本

    for _ in range(5):  # 最多 5 轮 tool calling，防止死循环
        response = await client.messages.create(
            model=config.AI_MODEL,
            max_tokens=4096,
            system=system_blocks,
            messages=messages,
            tools=TOOLS_ANTHROPIC,
        )

        print(f"🤖 stop_reason: {response.stop_reason}")
        print(f"🤖 content: {response.content}")

        # 提取文本回复和 tool 调用
        text_parts = []
        tool_uses = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_uses.append(block)

        # 本轮有文本就立刻发出去（去重，防止 AI 在最后一轮重复之前说过的话）
        round_text = "\n".join(text_parts).strip()
        if round_text and round_text not in all_text_parts:
            if send_callback:
                await send_callback(round_text)
            all_text_parts.append(round_text)

        # 如果没有 tool call，返回所有文本
        if response.stop_reason == "end_turn":
            return "\n".join(all_text_parts) or ""

        # 处理 tool calls
        if response.stop_reason == "tool_use" and tool_uses:
            # 把 assistant 的完整回复加入消息（包含 text + tool_use）
            messages.append({"role": "assistant", "content": response.content})

            # 执行每个 tool，收集结果
            tool_results = []
            for tool_use in tool_uses:
                result = _execute_tool(db, tool_use.name, tool_use.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "content": json.dumps(result, ensure_ascii=False)
                })

            # 把 tool 结果作为 user 消息加入
            messages.append({"role": "user", "content": tool_results})

    return "\n".join(all_text_parts) or "（内部错误：工具调用次数过多）"


def _execute_tool(db: Database, tool_name: str, args: dict) -> dict:
    """
    执行具体的工具调用，返回结果
    这里是 AI 的结构化输出真正落地的地方
    """
    if tool_name == "log_timeline_event":
        event_id = db.add_event(
            start_time=args["start_time"],
            end_time=args.get("end_time"),
            content=args["content"],
            category=args.get("category", "uncategorized"),
            notes=args.get("notes"),
            session_id=args.get("session_id")
        )
        # 如果这是恢复旧事件，确保旧事件的 session_id 也被设置成它自己
        # 注意 update_event 更新的是旧事件
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
        ok = db.update_event(args["event_id"], **fields)
        if ok:
            return {"success": True, "message": "事件已更新"}
        return {"success": False, "message": f"未找到 event_id={args['event_id']}"}

    elif tool_name == "add_pending_event":
        event_id = db.add_pending_event(
            description=args["description"],
            due_date=args.get("due_date"),
            notes=args.get("notes")
        )
        return {"success": True, "event_id": event_id, "message": "待办事件已添加"}

    elif tool_name == "complete_pending_event":
        ok = db.complete_pending_event(args["event_id"])
        if ok:
            return {"success": True, "message": "待办事件已标记完成"}
        return {"success": False, "message": f"未找到待办事件 id={args['event_id']} 或已完成"}

    else:
        return {"success": False, "message": f"未知工具: {tool_name}"}