"""
AI 引擎公共模块
提取三个引擎（Claude / Gemini / Relay）共享的逻辑：
- 动态上下文构建
- 消息格式处理
- 工具执行
- chat / scheduled_action 高层流程
"""
import re
from bot.tools import POLL_TOOL_NAMES, REMINDER_TOOL_NAMES, SCHEDULED_TOOL_NAMES, TOOLS
from bot.prompts import build_prompt, PromptParts
from bot.weather import is_morning, get_weather_brief
from bot.database import Database
from bot.logger import get_logger
import config

logger = get_logger(__name__)


# 最后一轮输出里 AI 的内部思考要用 <think>...</think> 或 <thinking>...</thinking> 包起来。
# 引擎在发送前调用 split_thinking() 剥离标签内容，只把标签外的纯文本发给用户。
_THINK_BLOCK = re.compile(r"<(?:think|thinking)>(.*?)</(?:think|thinking)>", re.DOTALL | re.IGNORECASE)

# 触发源标签
_TRIGGER_LABELS = {
    "poll": "🔄 随机轮询",
    "reminder": "🔔 提醒触发",
    "bedtime": "😴 睡前提醒",
}

# 按工具名索引描述（取描述的第一句话作为简要说明）
_TOOL_DESC_MAP: dict[str, str] = {}
for _t in TOOLS:
    _fn = _t["function"]
    _desc = _fn["description"]
    # 取第一句话（句号、句号+换行、或前 40 字符）
    _short = _desc.split("。")[0].split("\n")[0][:50]
    _TOOL_DESC_MAP[_fn["name"]] = _short


def format_tool_calls_summary(called_names: list[str], called_args: list[dict] | None = None) -> str:
    """构建中间轮的工具调用摘要日志。

    输出格式示例:
      🔧 log_timeline_event(start_time='...', content='看剧') — 记录一条生活轨迹时间轴事件
      🔧 set_reminder(trigger_time='...') — 预约一次未来的主动联系
    """
    lines = []
    for i, name in enumerate(called_names):
        desc = _TOOL_DESC_MAP.get(name, "")
        args_str = ""
        if called_args and i < len(called_args):
            # 只取前 3 个 key=value，每个 value 截断到 30 字符
            pairs = []
            for k, v in list(called_args[i].items())[:3]:
                vs = str(v)
                if len(vs) > 30:
                    vs = vs[:27] + "..."
                pairs.append(f"{k}={vs!r}")
            args_str = ", ".join(pairs)
        sig = f"{name}({args_str})" if args_str else name
        line = f"🔧 {sig}"
        if desc:
            line += f" — {desc}"
        lines.append(line)
    return "\n".join(lines)


def split_thinking(text: str) -> tuple[str, str]:
    """拆分 <think>...</think> 或 <thinking>...</thinking> 独白块和用户可见文本。
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


def _build_prompt(db: Database, mode: str, provider: str = "claude",
                  weather: str | None = None) -> PromptParts:
    """
    从 DB 取数据，构建完整的 PromptParts 对象。

    mode:     "chat"（用户对话）或 "poll"（调度主动聊天）
    provider: AI 引擎标识，透传给 build_prompt（预留 provider-specific prompt 扩展）
    """
    memories = db.get_all_memories()
    ongoing = db.get_ongoing_events(limit=5)
    # poll 模式不传 reminders：提醒到时间自会触发，AI 不需要看待触发清单
    reminders = db.list_active_reminders() if mode == "chat" else None

    # Deadline：先自动过期，再取 active
    db.expire_past_deadlines()
    deadlines = db.get_active_deadlines()

    return build_prompt(
        mode,
        provider=provider,
        memories=memories or None,
        ongoing=ongoing or None,
        reminders=reminders or None,
        weather=weather,
        deadlines=deadlines or None,
    )


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
            is_parallel=False,
            project_name=args.get("project_name"),
            energy_type=args.get("energy_type"),
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
        fields = {k: args[k] for k in ("end_time", "content", "category", "project_name", "energy_type") if k in args}
        # notes 追加模式：新 notes 拼接到已有内容后面
        if "notes" in args and args["notes"]:
            existing = db.get_event_by_id(args["event_id"])
            if existing and existing.get("notes"):
                fields["notes"] = existing["notes"] + "\n" + args["notes"]
            else:
                fields["notes"] = args["notes"]
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

    elif tool_name == "add_deadline":
        deadline_id = db.add_deadline(
            title=args["title"],
            due_time=args["due_time"],
        )
        return {"success": True, "deadline_id": deadline_id, "message": "Deadline 已记录"}

    elif tool_name == "complete_deadline":
        ok = db.complete_deadline(args["deadline_id"])
        if ok:
            return {"success": True, "message": "Deadline 已标记完成"}
        return {"success": False, "message": f"未找到 active 的 deadline_id={args['deadline_id']}"}

    elif tool_name == "delete_deadline":
        ok = db.delete_deadline(args["deadline_id"])
        if ok:
            return {"success": True, "message": "Deadline 已删除"}
        return {"success": False, "message": f"未找到 deadline_id={args['deadline_id']}"}

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
        None, None, messages,
        model=config.POLL_MODEL,
        tool_names=set(),  # 空集 → 不传工具
    )
    return reply


async def chat(db: Database, messages: list[dict],
               call_with_tools_fn, send_callback=None, tool_callback=None,
               provider: str = "claude") -> str:
    """
    处理用户消息的完整流程。
    messages: 调用方（discord_bot）已经从 Discord 历史构造好的消息列表，
              最后一条应该是当前用户发来的消息（含时间戳前缀）。
    call_with_tools_fn: 各引擎的 _call_with_tools 实现。

    DB 只作为备份：调用方在进入这里之前应已把当前用户消息写入 messages 表；
    这里只负责把 AI 回复写回 DB 做备份。

    使用 build_prompt("chat")：完整工具指南 + Chat 版时间感知。
    """
    # 规范化消息序列（首条必须是 user，合并连续同角色）
    messages = _ensure_valid_messages(messages)

    # 早上时段查天气
    weather = await get_weather_brief() if is_morning() else None

    # 构建 PromptParts（静态 + 动态上下文一步到位）
    prompt = _build_prompt(db, "chat", provider=provider, weather=weather)

    # 调用大模型（可能需要多轮 tool calling）
    reply = await call_with_tools_fn(
        db, prompt, messages,
        send_callback=send_callback,
        tool_callback=tool_callback,
        model=config.CHAT_MODEL
    )

    # 备份 AI 回复到 DB
    db.add_message("assistant", reply)

    return reply


async def scheduled_action(db: Database, prompt: str, timestamp: str,
                           history: list[dict], call_with_tools_fn,
                           send_callback=None,
                           allow_silent: bool = False,
                           trigger: str | None = None,
                           provider: str = "claude") -> str | None:
    """
    统一的调度入口：处理主动聊天、提醒触发、睡前提醒等所有非用户消息的 AI 调用。

    prompt: 注入给 AI 的调度指令（作为最后一条 user 消息追加到 history 后面）
    history: 调用方从 Discord 拉来的对话历史（不含本次调度指令）
    allow_silent: 是否允许 AI 返回 [SILENT] 不发消息（随机轮询时为 True）
    trigger: 触发源标签 ("poll" / "reminder" / "bedtime")，用于日志区分
    返回 None 表示 AI 选择不发消息。

    使用 build_prompt("poll")：完整时间感知 + Poll 版回复规则。
    """
    # 日志：标明触发源
    label = _TRIGGER_LABELS.get(trigger, f"📋 调度({trigger})")
    logger.info(f"{label} ▸ scheduled_action 开始 [{timestamp}]")

    # 注：历史为空不代表"没聊过"——也可能是 bot 刚重启或 Discord 历史拉取失败。
    # 此时仍然继续调用 AI，让它基于 memory / 待触发提醒 / 当前时间 / 天气等
    # 动态上下文自主决定要不要说话。真觉得没得说，它可以返回 [SILENT]。

    # 早上时段查天气
    weather = await get_weather_brief() if is_morning() else None

    prompt_parts = _build_prompt(db, "poll", provider=provider, weather=weather)

    messages = [
        *history,
        {"role": "user", "content": prompt}
    ]
    messages = _ensure_valid_messages(messages)

    # 允许 silent 时不传 send_callback，需要先检查 [SILENT]
    reply = await call_with_tools_fn(
        db, prompt_parts, messages,
        send_callback=None if allow_silent else send_callback,
        model=config.POLL_MODEL,
        tool_names=SCHEDULED_TOOL_NAMES,
    )

    if reply and "[SILENT]" not in reply:
        if allow_silent and send_callback:
            await send_callback(reply)
        db.add_message("assistant", reply)  # 备份
        return reply
    return None
