"""
Prompt assembly helpers.

Prompt text is stored in SQLite (`prompt_sections`) and edited through the
admin UI. This module only keeps stable section keys, prompt composition, and
small non-user-specific runtime hints.

架构（6 个正交 section，chat / poll 完全共用）：
- IDENTITY / USER_MODEL / SYSTEM_MECHANICS / COMMUNICATION / PROTOCOLS / TOOLS
- PromptParts dataclass 按变化频率分四层（静态 / 稳定上下文 / 记忆 / 高频动态），
  对应 Anthropic cache_control 的 4 个上限，build_prompt() 一步构建。

⚠️ 静态 prompt **不随 mode 变化**——chat / poll 共享完全相同的 system prompt，
   最大化 1h ephemeral cache 命中率。模式差异通过 scheduler 模板
   （PROACTIVE / REMINDER / BEDTIME / MORNING）在 user message 里标识。

各引擎消费 PromptParts 的方法：
- Claude: prompt.to_claude_blocks() → 最多 4 个 cached system block
- Gemini/Relay: prompt.flatten() → 单个字符串
- 中间轮省 token: prompt.concise().flatten()（去掉 TOOLS 段）

"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass


PROMPT_SECTION_LABELS = {
    "identity": "IDENTITY",
    "user_model": "USER_MODEL",
    "system_mechanics": "SYSTEM_MECHANICS",
    "communication": "COMMUNICATION",
    "protocols": "PROTOCOLS",
    "tools": "TOOLS_SECTION",
    "proactive_gemini": "PROACTIVE_PROMPT_GEMINI",
    "proactive_claude": "PROACTIVE_PROMPT_CLAUDE",
    "reminder": "REMINDER_PROMPT",
    "bedtime": "BEDTIME_PROMPT",
    "morning": "MORNING_PROMPT",
    "weather_report": "WEATHER_REPORT_PROMPT",
    "dispatch_escalation_trigger": "DISPATCH_ESCALATION_TRIGGER_LIST",
    "dispatch_small_decide_output": "DISPATCH_SMALL_DECIDE_OUTPUT_SPEC",
    "dispatch_paraphrase_task": "DISPATCH_PARAPHRASE_TASK",
    "dispatch_big_worker_output": "DISPATCH_BIG_WORKER_OUTPUT_SPEC",
}


def empty_prompt_sections() -> dict[str, str]:
    """Return all known prompt sections with empty values."""
    return {key: "" for key in PROMPT_SECTION_LABELS}


# ══════════════════════════════════════════════════════════════
# PromptParts dataclass + build_prompt()
# ══════════════════════════════════════════════════════════════
#
# 注意：不存在独立的 INITIATION section。
# chat / poll 共享完全相同的 system prompt（跨模式 cache 100% 命中）。
# 模式差异由 scheduler 模板（PROACTIVE_PROMPT / REMINDER_PROMPT / BEDTIME_PROMPT /
# MORNING_PROMPT）在 user message 里自然标识，AI 据此识别当前是主动轮询还是被动回复。

_CLEANUP_RE = re.compile(r"\n{3,}")


def _join_nonempty(*parts: str) -> str:
    """连接非空段落，用双换行分隔，清理多余空行。"""
    joined = "\n\n".join(p.strip() for p in parts if p and p.strip())
    return _CLEANUP_RE.sub("\n\n", joined).strip()


@dataclass
class PromptParts:
    """
    按变化频率分四层的结构化 prompt（对应 Anthropic 4 个 cache_control 上限）。

    Block 1 (static)：identity + user_model + system_mechanics + communication +
           protocols + tools（几乎不变，chat / poll 完全相同）
    Block 2 (stable context)：projects（项目列表几乎不增删）
    Block 3 (memories)：memories（比 projects 变化略频繁，独立成 block 避免
           因记忆更新连带 invalidate Block 2 的 cache）
    Block 4 (volatile)：ongoing + pending_reminders + deadlines + weather + calendar（高频变化）

    pending_reminders 注入 Block 4 的目的：让 AI 一眼看到队列里已有什么 follow-up，
    避免被聊天历史带回去重复 set 同一件事；也让"主动 follow-up"策略有兜底。

    Gemini/Relay 用 flatten() 拍平成单个字符串（不参与 prompt caching）。
    """
    mode: str  # "chat" | "poll"，仅用于调用方上游决策（如 DB 取数），不影响 prompt 内容

    # 静态层（chat / poll 完全共用）
    identity: str
    user_model: str
    system_mechanics: str
    communication: str
    protocols: str
    tools: str | None  # None = concise 模式（中间轮省 token）

    # 半动态层（拆成两个 block 以隔离 invalidate 影响面）
    projects: str = ""
    memories: str = ""

    # 动态层
    ongoing: str = ""
    pending_reminders: str = ""
    deadlines: str = ""
    weather: str = ""
    calendar: str = ""

    def static_text(self) -> str:
        """Block 1：所有静态段落。"""
        parts = [
            self.identity,
            self.user_model,
            self.system_mechanics,
            self.communication,
            self.protocols,
        ]
        if self.tools:
            parts.append(self.tools)
        return _join_nonempty(*parts)

    def stable_context_text(self) -> str:
        """Block 2：projects（低频变化）。"""
        return self.projects

    def memories_text(self) -> str:
        """Block 3：memories（单独成 block，避免牵连 Block 2）。"""
        return self.memories

    def dynamic_text(self) -> str:
        """Block 4：ongoing + pending_reminders + deadlines + weather + calendar（高频变化）。"""
        return _join_nonempty(
            self.ongoing,
            self.pending_reminders,
            self.deadlines,
            self.weather,
            self.calendar,
        )

    def flatten(self) -> str:
        """拍平为单个字符串（Gemini / Relay 用）。"""
        return _join_nonempty(
            self.static_text(),
            self.stable_context_text(),
            self.memories_text(),
            self.dynamic_text(),
        )

    def to_claude_blocks(self) -> list[dict]:
        """
        构建 Anthropic system blocks（最多 4 个 cached block，上限即 cache_control 最大值）。

        顺序 = 稳定 → 易变，前缀匹配最大化命中：
        - Block 1: 静态（identity/user_model/.../tools）
        - Block 2: projects（稳定上下文）
        - Block 3: memories（单独块，记忆更新不影响 Block 2）
        - Block 4: ongoing + deadlines + weather + calendar（高频变化，失效只影响此块）
        """
        blocks = []
        for text in (
            self.static_text(),
            self.stable_context_text(),
            self.memories_text(),
            self.dynamic_text(),
        ):
            if text:
                blocks.append({
                    "type": "text",
                    "text": text,
                    "cache_control": {"type": "ephemeral", "ttl": "1h"},
                })
        return blocks

    def concise(self) -> PromptParts:
        """返回去掉 tools 段的副本（中间轮省 token）。"""
        c = copy.copy(self)
        c.tools = None
        return c


# ── 动态段落格式化函数 ──────────────────────────────────────────

LABEL_MEMORIES = "【你现在记着的事】"
LABEL_ONGOING = "【当前进行中的事件（end_time 为空）】"
LABEL_DEADLINES = "【待完成的 Deadline】"
LABEL_WEATHER = "【今日天气】"
LABEL_CALENDAR = "【Google Calendar（今天 + 未来 7 天，计划中的日程）】"
LABEL_PROJECTS = "【现有项目列表（Focus 用，只能引用这里已有的项目）】"
LABEL_PENDING_REMINDERS = "【待触发的 Reminder（你自己设的 follow-up 队列）】"

WEATHER_CONTEXT_SUFFIX = "可以自然地提一下天气，但不要像天气预报一样念数据。"


def format_countdown(due_time_str: str) -> str:
    """
    根据 due_time 和当前时间计算倒计时文本。

    < 24h → "⚠️ 剩余 8h"
    1-7 天 → "⏳ 剩余 2天14h"
    > 7 天 → "⏳ 剩余 12天"
    已过期 → "⚠️ 已过期 3h"
    """
    from datetime import datetime
    try:
        # 尝试解析 ISO 8601
        due = datetime.fromisoformat(due_time_str)
        # 项目约定 datetime.now() 用进程本地时区（由 /tz 控制），统一在 naive 域里比
        if due.tzinfo is not None:
            due = due.astimezone().replace(tzinfo=None)
        now = datetime.now()
        delta = due - now
        total_hours = delta.total_seconds() / 3600

        if total_hours < 0:
            # 已过期
            past_hours = abs(total_hours)
            if past_hours < 24:
                return f"⚠️ 已过期 {int(past_hours)}h"
            return f"⚠️ 已过期 {int(past_hours / 24)}天"
        elif total_hours < 24:
            return f"⚠️ 剩余 {int(total_hours)}h"
        elif total_hours < 24 * 7:
            days = int(total_hours // 24)
            hours = int(total_hours % 24)
            return f"⏳ 剩余 {days}天{hours}h"
        else:
            days = int(total_hours / 24)
            return f"⏳ 剩余 {days}天"
    except (ValueError, TypeError):
        return "⏳ 时间格式异常"


def _format_memories(memories: list[dict] | None) -> str:
    if not memories:
        return ""
    lines = [f"- [id={m['id']}] {m['content']}" for m in memories]
    return f"{LABEL_MEMORIES}\n" + "\n".join(lines)


def _format_ongoing(ongoing: list[dict] | None) -> str:
    if not ongoing:
        return ""
    lines = []
    for e in ongoing:
        cat_part = e['category']
        if e.get("project_name"):
            cat_part += f" [{e['project_name']}]"
        line = f"- [ID={e['id']}] {e['start_time']} | {cat_part} | {e['content']}"
        if e.get("notes"):
            line += f" | 备注: {e['notes']}"
        lines.append(line)
    return f"{LABEL_ONGOING}\n" + "\n".join(lines)


def _format_weather(weather: str | None) -> str:
    if not weather:
        return ""
    return f"{LABEL_WEATHER}\n{weather}\n{WEATHER_CONTEXT_SUFFIX}"


def _format_calendar(calendar: str | None) -> str:
    if not calendar:
        return ""
    return f"{LABEL_CALENDAR}\n{calendar}"


def _format_projects(projects: list[dict] | None) -> str:
    if not projects:
        return f"{LABEL_PROJECTS}\n- 无"
    lines = [f"- {p['project_name']}" for p in projects]
    return f"{LABEL_PROJECTS}\n" + "\n".join(lines)


def _format_deadlines(deadlines: list[dict] | None) -> str:
    if not deadlines:
        return ""
    lines = []
    for d in deadlines:
        countdown = format_countdown(d["due_time"])
        line = f"- [id={d['id']}] {d['title']} | 📅 {d['due_time']} | {countdown}"
        lines.append(line)
    return f"{LABEL_DEADLINES}\n" + "\n".join(lines)


def _format_pending_reminders(pending: list[dict] | None) -> str:
    if not pending:
        return ""
    lines = []
    for r in pending:
        countdown = format_countdown(r["trigger_time"])
        head = f"- [id={r['id']}"
        if r.get("group_id"):
            head += f", group={r['group_id']}"
        head += "]"
        line = f"{head} {r['trigger_time']} | {countdown} | {r.get('priority', 'normal')} | {r['action']}"
        lines.append(line)
    return f"{LABEL_PENDING_REMINDERS}\n" + "\n".join(lines)


def build_prompt(
    mode: str,
    *,
    provider: str = "claude",
    memories: list[dict] | None = None,
    ongoing: list[dict] | None = None,
    weather: str | None = None,
    calendar: str | None = None,
    deadlines: list[dict] | None = None,
    projects: list[dict] | None = None,
    pending_reminders: list[dict] | None = None,
    sections: dict[str, str] | None = None,
) -> PromptParts:
    """
    一步构建完整的 PromptParts 对象。

    mode:     "chat"（她的对话）或 "poll"（调度主动聊天）。
              仅透传给 PromptParts.mode，不影响静态 prompt 内容——chat / poll
              共享完全相同的 system prompt 以最大化 cache 命中率。
              模式差异由 scheduler 模板（PROACTIVE/REMINDER/BEDTIME/MORNING）
              在 user message 里标识。
    provider: AI 引擎标识（"claude" / "gemini" / "relay"），预留参数。
    其余参数：从 DB 取来的原始数据，由内部 _format_* 函数格式化。
    """
    _ = provider  # 预留参数，暂时未使用
    prompt_sections = empty_prompt_sections()
    if sections:
        for key, value in sections.items():
            if key in prompt_sections and value:
                prompt_sections[key] = value.strip()
    return PromptParts(
        mode=mode,
        identity=prompt_sections["identity"],
        user_model=prompt_sections["user_model"],
        system_mechanics=prompt_sections["system_mechanics"],
        communication=prompt_sections["communication"],
        protocols=prompt_sections["protocols"],
        tools=prompt_sections["tools"],
        memories=_format_memories(memories),
        deadlines=_format_deadlines(deadlines),
        projects=_format_projects(projects),
        ongoing=_format_ongoing(ongoing),
        pending_reminders=_format_pending_reminders(pending_reminders),
        weather=_format_weather(weather),
        calendar=_format_calendar(calendar),
    )


# ══════════════════════════════════════════════════════════════
# 工具多轮调用：注入到下一轮的系统提示
# ══════════════════════════════════════════════════════════════
#
# 设计：SYSTEM_MECHANICS 已经讲清楚了"每一轮文字都会发给她"这条规则，
# 所以这里只做极短指针、不重复规则本身。

TOOL_ROUND_REMINDER = "[系统提示] 上一轮你说的话已经发出去了，不要重复。调工具时可以顺口说一句你在做什么。"

# 每个工具在 tool_result 之后的"定向后置提示"。命中了才追加，没命中就只发
# TOOL_ROUND_REMINDER。作用：把"使用 X 工具后应该怎样判断"这类规则精准投递，
# 而不是塞进全局 SYSTEM_PROMPT 每次请求都带。
TOOL_POST_HINTS = {
    "list_reminders": (
        "[决策辅助] 刚查了 pending reminder。如果要 set_reminder：同一件事复用已有 group_id；"
        "清单里已有 action 相近且 trigger_time 在 ±30 分钟内的条目就不要再 set；"
        "要替换旧的先 delete_reminder（单条）或 cancel_reminders（整组）再 set。"
    ),
    # set_reminder 后不做去重自检：pending reminder 列表已经常驻 Block 4
    # 上下文，模型每次进入新一轮就能看到队列，不需要再 round-trip 一次 list。
}


def build_tool_round_hint(tool_names_called) -> str:
    """构造 tool_result 后注入的系统提示 = TOOL_ROUND_REMINDER + 命中的 per-tool hints"""
    extras = []
    seen = set()
    for name in tool_names_called:
        if name in seen:
            continue
        seen.add(name)
        hint = TOOL_POST_HINTS.get(name)
        if hint:
            extras.append(hint)
    if not extras:
        return TOOL_ROUND_REMINDER
    return TOOL_ROUND_REMINDER + "\n\n" + "\n\n".join(extras)


def get_prompt_template(key: str, sections: dict[str, str] | None = None) -> str:
    """Fetch a single prompt template from DB-loaded sections."""
    if sections is None:
        sections = empty_prompt_sections()
    if key not in sections:
        raise KeyError(key)
    return (sections[key] or "").strip()


def get_proactive_prompt(provider: str, sections: dict[str, str] | None = None) -> str:
    """按 provider 返回对应的轮询模板。gemini 走 <think> 框架版，其他走选项式版。"""
    key = "proactive_gemini" if provider.lower().strip() == "gemini" else "proactive_claude"
    return get_prompt_template(key, sections)
