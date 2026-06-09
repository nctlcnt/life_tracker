"""
AI 引擎公共模块
提取三个引擎（Claude / Gemini / Relay）共享的逻辑：
- 动态上下文构建
- 消息格式处理
- 工具执行
- chat / scheduled_action 高层流程
"""
from bot.tools import TOOLS
from bot.prompts import build_prompt, PromptParts
from bot.weather import is_morning, get_weather_brief
from bot.database import Database
from bot.logger import get_logger
from bot import trace
from config import Preset
import config

logger = get_logger(__name__)


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


def _build_prompt(db: Database, mode: str, provider: str = "claude",
                  weather: str | None = None) -> PromptParts:
    """
    从 DB 取数据，构建完整的 PromptParts 对象。

    mode:     "chat"（用户对话）或 "poll"（调度主动聊天）
    provider: AI 引擎标识，透传给 build_prompt（预留 provider-specific prompt 扩展）
    """
    memories = db.get_all_memories()
    ongoing = db.get_ongoing_events(limit=5)

    # Deadline：先自动过期，再取 active
    db.expire_past_deadlines()
    deadlines = db.get_active_deadlines()

    projects = db.get_all_project_names()

    # pending reminders 注入 Block 4：让 AI 看到自己已设的 follow-up 队列，
    # 避免被聊天历史带回去重复 set 同一件事。
    pending_reminders = db.list_active_reminders()

    return build_prompt(
        mode,
        provider=provider,
        sections=db.get_prompt_sections(),
        memories=memories or None,
        ongoing=ongoing or None,
        weather=weather,
        deadlines=deadlines or None,
        projects=projects or None,
        pending_reminders=pending_reminders or None,
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
        category = args.get("category", "uncategorized")
        project_name = (args.get("project_name") or "").strip() or None
        if category == "Focus":
            if not project_name:
                return {
                    "success": False,
                    "message": "Focus 事件必须填写 project_name，且只能使用【现有项目列表】里的项目。",
                }
            if not db.project_exists(project_name):
                return {
                    "success": False,
                    "message": f"未知项目: {project_name}。只能使用用户手动建立的项目；不能自行新建项目名。",
                }
        else:
            project_name = None
        event_id = db.add_event(
            start_time=args["start_time"],
            end_time=args.get("end_time"),
            content=args["content"],
            category=category,
            notes=args.get("notes"),
            session_id=args.get("session_id"),
            is_parallel=False,
            project_name=project_name,
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
        fields = {k: args[k] for k in ("end_time", "content", "category") if k in args}
        if "project_name" in args:
            project_name = (args.get("project_name") or "").strip() or None
            if not project_name or not db.project_exists(project_name):
                return {
                    "success": False,
                    "message": f"未知项目: {project_name}。只能使用用户手动建立的项目；不能自行新建项目名。",
                }
            fields["project_name"] = project_name
        if fields.get("category") == "Focus" and "project_name" not in fields:
            existing = db.get_event_by_id(args["event_id"])
            project_name = (existing or {}).get("project_name")
            if not project_name or not db.project_exists(project_name):
                return {
                    "success": False,
                    "message": "更新为 Focus 事件时必须提供有效 project_name，且只能使用【现有项目列表】里的项目。",
                }
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


async def simple_completion(prompt: str, call_with_tools_fn, preset: Preset) -> str:
    """
    轻量 AI 调用：无工具、无历史消息、无动态上下文。
    用于天气报告等独立的一次性生成任务。
    """
    messages = [{"role": "user", "content": prompt}]
    trace.start(trigger="oneshot", model=preset.model, provider=preset.provider,
                prompt_parts=None, messages=messages)
    try:
        reply = await call_with_tools_fn(
            None, None, messages,
            preset=preset,
            tool_names=set(),  # 空集 → 不传工具
        )
        trace.finalize(final_text=reply)
        return reply
    except Exception as e:
        trace.finalize(error=f"{type(e).__name__}: {e}")
        raise


async def chat(db: Database, messages: list[dict],
               call_with_tools_fn, preset: Preset,
               send_callback=None, tool_callback=None) -> str:
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
    prompt = _build_prompt(db, "chat", provider=preset.provider, weather=weather)

    trace.start(trigger="chat", model=preset.model, provider=preset.provider,
                prompt_parts=prompt, messages=messages)
    try:
        # 调用大模型（可能需要多轮 tool calling）
        reply = await call_with_tools_fn(
            db, prompt, messages,
            send_callback=send_callback,
            tool_callback=tool_callback,
            preset=preset,
        )

        # 备份 AI 回复到 DB
        db.add_message("assistant", reply)

        trace.finalize(final_text=reply)
        return reply
    except Exception as e:
        trace.finalize(error=f"{type(e).__name__}: {e}")
        raise


async def scheduled_action(db: Database, prompt: str, timestamp: str,
                           history: list[dict], call_with_tools_fn,
                           preset: Preset,
                           send_callback=None,
                           allow_silent: bool = False,
                           trigger: str | None = None) -> str | None:
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

    prompt_parts = _build_prompt(db, "poll", provider=preset.provider, weather=weather)

    messages = [
        *history,
        {"role": "user", "content": prompt}
    ]
    messages = _ensure_valid_messages(messages)

    # 注：tool_names 不再过滤，chat / poll 共用全量 tools，
    # 避免 tools 字段差异导致 prompt cache 前缀 miss。

    trace.start(trigger=trigger or "scheduled", model=preset.model, provider=preset.provider,
                prompt_parts=prompt_parts, messages=messages)
    final_reply: str | None = None
    try:
        if allow_silent:
            # 逐轮过滤 [SILENT]：中间轮的真实文字立即发送，[SILENT] 静默吞掉。
            # 修复：旧逻辑把 send_callback=None 传入 _call_with_tools，导致工具轮
            # 的文字全部攒到最后；而最终拼接结果一旦包含 [SILENT] 就整体丢弃。
            sent_texts: list[str] = []

            async def _silent_filter(text: str):
                if "[SILENT]" not in text:
                    sent_texts.append(text)
                    if send_callback:
                        await send_callback(text)

            await call_with_tools_fn(
                db, prompt_parts, messages,
                send_callback=_silent_filter,
                preset=preset,
            )

            if sent_texts:
                final_reply = "\n".join(sent_texts)
        else:
            reply = await call_with_tools_fn(
                db, prompt_parts, messages,
                send_callback=send_callback,
                preset=preset,
            )

            if reply and "[SILENT]" not in reply:
                final_reply = reply

        if final_reply:
            db.add_message("assistant", final_reply)
        trace.finalize(final_text=final_reply)
        return final_reply
    except Exception as e:
        trace.finalize(error=f"{type(e).__name__}: {e}")
        raise
