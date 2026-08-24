"""
AI 引擎公共模块
引擎实现（ai_engine_openai_compat）之上的共享逻辑：
- 动态上下文构建
- 消息格式处理
- 工具执行
- chat / scheduled_action 高层流程
"""
import asyncio
from datetime import datetime

from bot.tools import POLL_TOOL_NAMES, REMINDER_TOOL_NAMES, TOOLS
from bot.memory.personal_repository import PersonalMemoryRepository
from bot.prompts import build_prompt, format_memory_tiers, PromptParts
from bot.memory import MemoryRecallDisabled, MemoryRecallUnavailable, MemoryService
from bot.memory import scene_state
from bot.weather import is_morning, get_weather_brief
from bot.google_calendar import CalendarNotConfigured, CalendarQueryError, get_calendar_context
from bot.database import Database
from bot.logger import get_logger
from bot import trace
from config import Preset
import config

logger = get_logger(__name__)


def _memory_service(db: Database,
                    service: MemoryService | None = None) -> MemoryService:
    return service or getattr(db, "_memory_service", None) or MemoryService(db)


# 触发源标签
_TRIGGER_LABELS = {
    "poll": "🔄 随机轮询",
    "reminder": "🔔 提醒触发",
    "bedtime": "😴 睡前提醒",
    "check_in": "📋 Check-in",
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


def _build_prompt(db: Database, mode: str,
                  weather: str | None = None,
                  calendar: str | None = None,
                  relevant_history: list[dict] | None = None,
                  context_config: dict | None = None,
                  memory_service: MemoryService | None = None) -> PromptParts:
    """
    从 DB 取数据，构建完整的 PromptParts 对象。

    mode: "chat"（用户对话）或 "poll"（调度主动聊天）
    """
    context_config = context_config or {}

    def include(key: str) -> bool:
        return context_config.get(key, True)

    memory = _memory_service(db, memory_service)
    memories, memory_markdown, memory_tiers = [], "", ""
    if include("include_memories"):
        if config.MEMORY_READ_FROM_DB:
            # 按注入权限取，不按 status 取：两者一一对应，但对应关系是设计决定，
            # 不该被每个调用点各记一份（见 status_ladder）。
            repository = PersonalMemoryRepository(db)
            memory_tiers = format_memory_tiers(
                asserted=repository.list_by_permission("assert"),
                hedged=repository.list_by_permission("hedge"),
                display_name=config.USER_DISPLAY_NAME)
        else:
            memories = memory.list_durable()
            memory_markdown = memory.durable_markdown()
    today_timeline = db.get_today_events() if include("include_today_timeline") else []

    # Deadline：先自动过期，再取 active
    deadlines = []
    if include("include_deadlines"):
        db.expire_past_deadlines()
        deadlines = db.get_active_deadlines()

    projects = db.get_all_project_names() if include("include_projects") else []

    # pending reminders 注入 Block 4：让 AI 看到自己已设的 follow-up 队列，
    # 避免被聊天历史带回去重复 set 同一件事。
    pending_reminders = db.list_active_reminders() if include("include_pending_reminders") else []
    if not include("include_relevant_history"):
        relevant_history = None

    return build_prompt(
        mode,
        sections=db.get_prompt_sections(),
        memories=memories or None,
        # 分档渲染好的整块直接当 markdown 传：_format_memories 的第一条分支就是
        # "有现成文本就原样用"，两条来源共用同一个占位符，互斥。
        memory_markdown=(memory_tiers or memory_markdown) or None,
        relevant_history=relevant_history or None,
        today_timeline=today_timeline or None,
        weather=weather,
        calendar=calendar,
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


async def _execute_tool_async(db: Database, tool_name: str, args: dict,
                              memory_service: MemoryService | None = None) -> dict:
    """
    工具执行的统一入口（各引擎调用这里）。

    search_history 要调 embedding API，必须走 async 路径（同步 HTTP 会阻塞
    event loop，卡住 Discord 心跳）；其余工具都是本地 DB 操作，委托同步 _execute_tool。
    """
    if tool_name == "search_history":
        return await _search_history(db, args, memory_service=memory_service)
    return _execute_tool(db, tool_name, args, memory_service=memory_service)


async def _search_history(db: Database, args: dict,
                          memory_service: MemoryService | None = None) -> dict:
    """AI 主动语义检索历史对话（poll 没有用户消息做锚点，让 AI 自己写 query）。"""
    query = (args.get("query") or "").strip()
    if not query:
        return {"success": False, "message": "query 不能为空"}
    memory = _memory_service(db, memory_service)
    try:
        snippets = await memory.recall(
            query, str(config.CHANNEL_ID), limit=5, exclude_recent=20
        )
    except MemoryRecallDisabled:
        return {"success": False, "message": "语义检索未启用（embedding 未配置）"}
    except MemoryRecallUnavailable:
        return {"success": False, "message": "embedding 调用失败，稍后再试"}
    if not snippets:
        return {"success": True, "count": 0, "snippets": [],
                "message": "没有检索到相关历史片段（可能没聊过，或换个说法再试一次）"}
    return {
        "success": True,
        "count": len(snippets),
        "snippets": [
            {
                "fragment": s.get("embedding_context") or s.get("content") or "",
                "relevance": s.get("relevance"),
            }
            for s in snippets
        ],
    }


def _execute_tool(db: Database, tool_name: str, args: dict,
                  memory_service: MemoryService | None = None) -> dict:
    """执行具体的工具调用，返回结果"""
    if tool_name == "set_scene":
        # 场景摘要写进会话状态，供后续几轮注入。这里只存不判断——
        # 有效期（来回数、静默超时、被新场景覆盖）全部由 scene_state 机械判定，
        # 不让模型决定"场景什么时候结束"（那不可靠）。
        description = str(args.get("description") or "").strip()
        if not description:
            return {"success": False, "message": "description 不能为空"}
        scene = scene_state.start(
            db, str(config.CHANNEL_ID),
            check_in_name=str(args.get("_check_in_name") or "unknown"),
            description=description,
            now=datetime.now(),
        )
        return {"success": True, "message": "场景已记录",
                "turns_budget": scene.max_turns}

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
        try:
            reminder_id = db.add_reminder(
                trigger_time=args["trigger_time"],
                action=args["action"],
                group_id=args.get("group_id"),
                priority=args.get("priority", "normal")
            )
        except ValueError as e:
            return {"success": False, "message": str(e)}
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

    elif tool_name == "query_calendar":
        from bot import google_calendar
        try:
            events = google_calendar.list_events(
                start=args["start"],
                end=args["end"],
                query=args.get("query"),
                max_results=args.get("max_results", 50),
                cache_seconds=google_calendar.QUERY_CALENDAR_CACHE_SECONDS,
            )
            return {"success": True, "events": events, "count": len(events)}
        except CalendarNotConfigured:
            return {
                "success": False,
                "message": "Google Calendar 未配置/未授权，先跑 scripts/google_calendar_auth.py",
            }
        except CalendarQueryError as e:
            return {"success": False, "message": str(e)}

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
        memory = _memory_service(db, memory_service)
        memory_id = memory.save_durable(
            args["content"],
            memory_type=args.get("memory_type") or None,
            valid_until=args.get("valid_until") or None,
        )
        return {"status": "ok", "memory_id": memory_id}

    elif tool_name == "delete_memory":
        memory = _memory_service(db, memory_service)
        memory.delete_durable(args["memory_id"])
        return {"status": "ok"}

    elif tool_name == "update_memory":
        memory = _memory_service(db, memory_service)
        fields = {"content": args["content"]}
        if "memory_type" in args:
            fields["memory_type"] = args["memory_type"]
        if "valid_until" in args:
            fields["valid_until"] = args["valid_until"] or None
        memory.update_durable(args["memory_id"], **fields)
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


async def simple_completion(prompt: str, call_with_tools_fn, preset: Preset,
                            *, trigger: str = "oneshot",
                            db: Database | None = None,
                            return_run_id: bool = False,
                            max_output_tokens: int | None = None,
                            request_timeout: float | None = None,
                            system_text: str | None = None,
                            temperature: float | None = None):
    """
    轻量 AI 调用：无工具、无历史消息、无动态上下文。
    用于天气报告等独立的一次性生成任务。
    system_text 提供时作为 system 块发送（引擎侧接受已拍平的字符串），
    用于 curator 等需要指令/数据分离的任务。
    temperature 仅在显式给出时随请求发送（判断类任务用低值稳定输出）。
    """
    messages = [{"role": "user", "content": prompt}]
    trace_messages = (
        [{"role": "system", "content": system_text}] if system_text else []
    ) + messages
    trace_entry = trace.start(
        trigger=trigger, model=preset.model, provider=preset.provider,
        prompt_parts=None, messages=trace_messages)
    try:
        reply = await call_with_tools_fn(
            None, system_text, messages,
            preset=preset,
            tool_names=set(),  # 空集 → 不传工具
            max_output_tokens=max_output_tokens,
            request_timeout=request_timeout,
            temperature=temperature,
        )
        trace.finalize(final_text=reply, db=db)
        return (reply, trace_entry["id"]) if return_run_id else reply
    except Exception as e:
        trace.finalize(error=f"{type(e).__name__}: {e}", db=db)
        raise


async def chat(db: Database, messages: list[dict],
               call_with_tools_fn, preset: Preset,
               send_callback=None, tool_callback=None,
               memory_service: MemoryService | None = None,
               window=None) -> str:
    """
    处理用户消息的完整流程。
    messages: 调用方（discord_bot）已经装配好的 token 窗口消息列表，
              最后一条应该是当前用户发来的消息（含时间戳前缀）。
    call_with_tools_fn: 引擎的 _call_with_tools 实现。
    window: 装配窗口的 ContextWindow（LT-135）。用于语义检索去重范围
            （exclude_recent = 实际明文条数）与 trace 窗口可见性；
            None 时退回固定 20 条语义（兼容旧调用方/测试）。

    DB 只作为备份：调用方在进入这里之前应已把当前用户消息写入 messages 表；
    这里只负责把 AI 回复写回 DB 做备份。

    使用 build_prompt("chat")：完整工具指南 + Chat 版时间感知。
    """
    # 规范化消息序列（首条必须是 user，合并连续同角色）
    messages = _ensure_valid_messages(messages)

    # 早上时段查天气
    weather = await get_weather_brief() if is_morning() else None
    calendar = await get_calendar_context()

    # memory v3 Part B2：用当前用户消息做语义检索，捞出工作窗口外的相关历史片段。
    # channel_id 直接用 config.CHANNEL_ID——bot 是单频道的（on_message 已过滤）。
    # exclude_recent 与窗口实际明文条数对齐（LT-135），避免检回窗口里已有的内容；
    # 检索失败/禁用时 relevant_history 为 None，聊天流程不受影响。
    memory = _memory_service(db, memory_service)
    memory_context = await memory.build_context(
        query=messages[-1]["content"] if messages else None,
        channel_id=str(config.CHANNEL_ID),
        include_memories=False,
        include_relevant_history=True,
        recall_limit=5,
        exclude_recent=window.tail_count if window else 20,
    )
    relevant_history = memory_context.relevant_history

    # 构建 PromptParts（静态 + 动态上下文一步到位）
    prompt = _build_prompt(db, "chat", weather=weather,
                           calendar=calendar, relevant_history=relevant_history,
                           memory_service=memory)

    # 场景状态：让"我们正在做什么"在用户回复之后仍然存在。
    #
    # check-in 的模板是作为最后一条 user 消息追加的，用户一回复它就被推远，
    # 语气随之掉回去。这里把 check-in 时记下的那句场景摘要接到 system 末尾，
    # 每轮都在，不必重发整份模板。
    #
    # touch 放在这里而不是回复之后：无论这次调用成功与否，这一个来回都已经
    # 发生了。放在成功分支里会让失败的轮次不计数，场景实际存活时间超出预算。
    scene = scene_state.touch(db, str(config.CHANNEL_ID), datetime.now())
    if scene is not None:
        prompt = prompt.with_suffix(scene.as_prompt_block())

    trace.start(trigger="chat", model=preset.model, provider=preset.provider,
                prompt_parts=prompt, messages=messages,
                window=window.trace_info() if window else None)
    try:
        # 调用大模型（可能需要多轮 tool calling）
        reply = await call_with_tools_fn(
            db, prompt, messages,
            send_callback=send_callback,
            tool_callback=tool_callback,
            preset=preset,
            memory_service=memory,
        )

        # 备份 AI 回复到 DB
        db.add_message("assistant", reply)

        trace.finalize(final_text=reply, db=db)
        return reply
    except Exception as e:
        trace.finalize(error=f"{type(e).__name__}: {e}", db=db)
        raise


async def scheduled_action(db: Database, prompt: str, timestamp: str,
                           history: list[dict], call_with_tools_fn,
                           preset: Preset,
                           send_callback=None,
                           allow_silent: bool = False,
                           trigger: str | None = None,
                           tool_profile: str | None = None,
                           check_in_name: str | None = None,
                           context_config: dict | None = None,
                           memory_service: MemoryService | None = None,
                           window=None,
                           track_scene: bool = False) -> str | None:
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
    memory_service = _memory_service(db, memory_service)
    label = _TRIGGER_LABELS.get(trigger, f"📋 调度({trigger})")
    if check_in_name:
        label = f"{label} [{check_in_name}]"
    logger.info(f"{label} ▸ scheduled_action 开始 [{timestamp}]")

    # 注：历史为空不代表"没聊过"——也可能是 bot 刚重启或 Discord 历史拉取失败。
    # 此时仍然继续调用 AI，让它基于 memory / 待触发提醒 / 当前时间 / 天气等
    # 动态上下文自主决定要不要说话。真觉得没得说，它可以返回 [SILENT]。

    # 早上时段查天气
    context_config = context_config or {}
    include_weather = context_config.get("include_weather", True)
    include_calendar = context_config.get("include_calendar", True)
    weather = await get_weather_brief() if include_weather and is_morning() else None
    calendar = await get_calendar_context() if include_calendar else None

    prompt_parts = _build_prompt(
        db, "poll", weather=weather,
        calendar=calendar, context_config=context_config,
        memory_service=memory_service,
    )

    if check_in_name:
        prompt = f"[check_in:{check_in_name}]\n{prompt}"
    messages = [
        *history,
        {"role": "user", "content": prompt}
    ]
    messages = _ensure_valid_messages(messages)

    tool_names = None
    profile = tool_profile
    if profile is None:
        if trigger == "reminder":
            profile = "reminder_safe"
        elif trigger in {"poll", "bedtime", "check_in"}:
            profile = "poll"

    if profile == "reminder_safe":
        tool_names = REMINDER_TOOL_NAMES
    elif profile == "poll":
        tool_names = POLL_TOOL_NAMES
    elif profile == "none":
        tool_names = set()

    # set_scene 只在本次 check-in 明确打开 track_scene 时才可用。
    # 不放进 POLL_TOOL_NAMES 之类的固定集合：那样每次主动消息都会带上它，
    # 而多数 check-in（睡前、morning）说完就结束，不需要延续场景。
    if track_scene and tool_names is not None:
        tool_names = set(tool_names) | {"set_scene"}

    trace.start(trigger=trigger or "scheduled", model=preset.model, provider=preset.provider,
                prompt_parts=prompt_parts, messages=messages,
                window=window.trace_info() if window else None)
    final_reply: str | None = None
    try:
        # 逐轮过滤 [SILENT]：中间轮的真实文字立即发送，[SILENT] 静默吞掉。
        # 两个分支共用，sent_texts 因此是「真正发给用户的内容」的唯一记录。
        sent_texts: list[str] = []

        async def _silent_filter(text: str):
            if "[SILENT]" not in text:
                sent_texts.append(text)
                if send_callback:
                    await send_callback(text)

        reply = await call_with_tools_fn(
            db, prompt_parts, messages,
            send_callback=_silent_filter,
            preset=preset,
            tool_names=tool_names,
            memory_service=memory_service,
        )

        if allow_silent:
            if sent_texts:
                final_reply = "\n".join(sent_texts)
        elif reply and "[SILENT]" not in reply:
            final_reply = reply
        elif sent_texts:
            # AI 在工具轮已经把话说出去了，最后一轮才回 [SILENT]。消息确实
            # 送达了，返回 None 会让调用方误判「没发出去」——reminder 因此
            # 不被 mark_reminder_done，留在 pending 且触发时间已过，
            # _reminder_loop 会以 wait=0 立刻重入，每轮都白烧一次 AI 调用。
            final_reply = "\n".join(sent_texts)

        if final_reply:
            db.add_message("assistant", final_reply)
        trace.finalize(final_text=final_reply, db=db)
        return final_reply
    except Exception as e:
        trace.finalize(error=f"{type(e).__name__}: {e}", db=db)
        raise
