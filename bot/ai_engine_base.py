"""
AI 引擎公共模块
提取三个引擎（Claude / Gemini / Relay）共享的逻辑：
- 动态上下文构建
- 消息格式处理
- 工具执行
- chat / scheduled_action 高层流程
"""
import re
from bot.tools import SYSTEM_PROMPT, POLL_TOOL_NAMES, REMINDER_TOOL_NAMES, SCHEDULED_TOOL_NAMES
from bot.weather import is_morning, get_weather_brief
from bot.database import Database
from bot.logger import get_logger
import config

logger = get_logger(__name__)


# 最后一轮输出里 AI 的内部思考要用 <think>...</think> 包起来。
# 引擎在发送前调用 split_thinking() 剥离标签内容，只把标签外的纯文本发给用户。
_THINK_BLOCK = re.compile(r"<think>(.*?)</think>", re.DOTALL | re.IGNORECASE)


def split_thinking(text: str) -> tuple[str, str]:
    """拆分 <think>...</think> 独白块和用户可见文本。
    返回 (user_text, thinking_text)；两者都可能为空字符串。
    - user_text: 标签外的文字，清理掉多余空行，给用户看 / 写入 DB 备份
    - thinking_text: 标签内的文字，只用于日志追踪
    """
    if not text:
        return "", ""
    thinking_parts = [m.group(1).strip() for m in _THINK_BLOCK.finditer(text)]
    cleaned = _THINK_BLOCK.sub("", text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    thinking = "\n\n".join(p for p in thinking_parts if p)
    return cleaned, thinking


def _build_dynamic_context(db: Database, weather: str | None = None) -> str:
    """
    构建动态上下文（记忆 + 进行中事件 + 待触发提醒 + 天气），每次调用注入。
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
        lines = []
        for e in ongoing:
            parallel_tag = " [平行]" if e.get("is_parallel") else ""
            line = f"- [ID={e['id']}]{parallel_tag} {e['start_time']} | {e['category']} | {e['content']}"
            if e.get("notes"):
                line += f" | 备注: {e['notes']}"
            lines.append(line)
        parts.append(
            "【当前进行中的事件（end_time 为空）】\n" + "\n".join(lines) + "\n"
            "⚠️ 新建事件前先扫一眼这里：\n"
            "- 如果是同一件事的继续 → update_timeline_event 更新 end_time，不要新建\n"
            "- 如果是同时进行的次要活动（一心二用） → log_timeline_event 加 is_parallel=true\n"
            "- 如果发现已有完全重复的条目 → 用 delete_timeline_event 清理多余的那条"
        )

    # 待触发提醒计划
    reminders = db.list_active_reminders()
    if reminders:
        lines = [
            f"- [id={r['id']}] [{r['priority']}] {r['trigger_time']} | {r['action']} "
            f"(group: {r.get('group_id', '无')})"
            for r in reminders
        ]
        parts.append(
            "【待触发的跟进计划】\n" + "\n".join(lines) + "\n"
            "⚠️ 去重规则：set_reminder 只新增不覆盖。打算新设前先扫这里——"
            "同 group 或同内容+相近时间已经存在就不要再 set；"
            "已经多设了就用 delete_reminder 按 id 精准删掉重复的那条。"
        )

    # 天气（早上时段注入）
    if weather:
        parts.append(f"【今日天气】\n{weather}\n可以自然地提一下天气，但不要像天气预报一样念数据。")

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


def _execute_tool(db: Database, tool_name: str, args: dict) -> dict:
    """执行具体的工具调用，返回结果"""
    if tool_name == "log_timeline_event":
        event_id = db.add_event(
            start_time=args["start_time"],
            end_time=args.get("end_time"),
            content=args["content"],
            category=args.get("category", "uncategorized"),
            notes=args.get("notes"),
            session_id=args.get("session_id"),
            is_parallel=bool(args.get("is_parallel", False)),
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

    elif tool_name == "delete_reminder":
        rid = args.get("reminder_id")
        ok = db.cancel_reminder_by_id(rid)
        if ok:
            return {"success": True, "reminder_id": rid, "message": "该条 reminder 已删除"}
        return {"success": False, "reminder_id": rid,
                "message": f"未找到 status=pending 的 reminder id={rid}（可能已触发或已取消）"}

    elif tool_name == "list_reminders":
        rems = db.list_active_reminders()
        return {"success": True, "reminders": rems, "count": len(rems)}

    elif tool_name == "query_timeline":
        events = db.get_events(start=args["start"], end=args["end"])
        return {"success": True, "events": events, "count": len(events)}

    elif tool_name == "update_timeline_event":
        fields = {k: args[k] for k in ("end_time", "content", "category", "notes") if k in args}
        if "is_parallel" in args:
            fields["is_parallel"] = 1 if bool(args["is_parallel"]) else 0
        ok = db.update_event(args["event_id"], **fields)
        if ok:
            return {"success": True, "message": "事件已更新"}
        return {"success": False, "message": f"未找到 event_id={args['event_id']}"}

    elif tool_name == "delete_timeline_event":
        ok = db.delete_event(args["event_id"])
        if ok:
            return {"success": True, "message": "事件已删除"}
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


# ── 高层流程：chat / scheduled_action / simple_completion ──────────────


async def simple_completion(prompt: str, call_with_tools_fn) -> str:
    """
    轻量 AI 调用：无工具、无历史消息、无动态上下文。
    用于天气报告等独立的一次性生成任务。
    """
    messages = [{"role": "user", "content": prompt}]
    reply = await call_with_tools_fn(
        None, "", messages,
        model=config.POLL_MODEL,
        tool_names=set(),  # 空集 → 不传工具
    )
    return reply


async def chat(db: Database, messages: list[dict],
               call_with_tools_fn, send_callback=None) -> str:
    """
    处理用户消息的完整流程。
    messages: 调用方（discord_bot）已经从 Discord 历史构造好的消息列表，
              最后一条应该是当前用户发来的消息（含时间戳前缀）。
    call_with_tools_fn: 各引擎的 _call_with_tools 实现。

    DB 只作为备份：调用方在进入这里之前应已把当前用户消息写入 messages 表；
    这里只负责把 AI 回复写回 DB 做备份。
    """
    # 规范化消息序列（首条必须是 user，合并连续同角色）
    messages = _ensure_valid_messages(messages)

    # 早上时段查天气
    weather = await get_weather_brief() if is_morning() else None

    # 构建动态上下文（记忆 + 进行中事件 + 待触发提醒 + 天气）
    dynamic_ctx = _build_dynamic_context(db, weather=weather)

    # 调用大模型（可能需要多轮 tool calling）
    reply = await call_with_tools_fn(
        db, SYSTEM_PROMPT, messages,
        send_callback=send_callback,
        dynamic_context=dynamic_ctx or None,
        model=config.CHAT_MODEL
    )

    # 备份 AI 回复到 DB
    db.add_message("assistant", reply)

    return reply


async def scheduled_action(db: Database, prompt: str, timestamp: str,
                           history: list[dict], call_with_tools_fn,
                           send_callback=None,
                           allow_silent: bool = False) -> str | None:
    """
    统一的调度入口：处理主动聊天、提醒触发、睡前提醒等所有非用户消息的 AI 调用。

    prompt: 注入给 AI 的调度指令（作为最后一条 user 消息追加到 history 后面）
    history: 调用方从 Discord 拉来的对话历史（不含本次调度指令）
    allow_silent: 是否允许 AI 返回 [SILENT] 不发消息（随机轮询时为 True）
    返回 None 表示 AI 选择不发消息。
    """
    # 注：历史为空不代表"没聊过"——也可能是 bot 刚重启或 Discord 历史拉取失败。
    # 此时仍然继续调用 AI，让它基于 memory / 待触发提醒 / 当前时间 / 天气等
    # 动态上下文自主决定要不要说话。真觉得没得说，它可以返回 [SILENT]。

    # 早上时段查天气
    weather = await get_weather_brief() if is_morning() else None

    dynamic_ctx = _build_dynamic_context(db, weather=weather)

    messages = [
        *history,
        {"role": "user", "content": prompt}
    ]
    messages = _ensure_valid_messages(messages)

    # 允许 silent 时不传 send_callback，需要先检查 [SILENT]
    reply = await call_with_tools_fn(
        db, SYSTEM_PROMPT, messages,
        send_callback=None if allow_silent else send_callback,
        dynamic_context=dynamic_ctx or None,
        model=config.POLL_MODEL,
        tool_names=SCHEDULED_TOOL_NAMES,
    )

    if reply and "[SILENT]" not in reply:
        if allow_silent and send_callback:
            await send_callback(reply)
        db.add_message("assistant", reply)  # 备份
        return reply
    return None
