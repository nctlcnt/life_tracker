"""
Prompt assembly helpers.

Prompt text is stored in SQLite (`prompt_sections`) and edited through the
admin UI. This module only keeps stable section keys, prompt composition, and
small non-user-specific runtime hints.

架构（LT-129：单一整体模板 + 占位符注入）：
- `main_template` section = 完整 system prompt 正文，人格/规则散文直接写在里面，
  系统知识通过 9 个占位符注入（{tools} {projects} {memories} {relevant_history}
  {today_timeline} {pending_reminders} {deadlines} {weather} {calendar}）。
- 渲染时按占位符的 cache tier 把模板切成 ≤4 个单调段（PLACEHOLDER_TIERS），
  对应 Anthropic cache_control 的 4 个上限——默认模板排序下分块结果与旧的
  四层 PromptParts 完全一致，模板乱序时仍正确、只是 cache 效率下降。
- 占位符替换只认 _MAIN_PLACEHOLDER_RE 里的已知 token，字面 {}（颜文字、
  JSON 示例）原样保留；占位符的值不做二次扫描。

⚠️ 静态 prompt **不随 mode 变化**——chat / poll 共享完全相同的 system prompt，
   最大化 1h ephemeral cache 命中率。模式差异通过 scheduler 模板
   （PROACTIVE / REMINDER / BEDTIME / MORNING）在 user message 里标识。

各引擎消费 PromptParts 的方法：
- Claude: prompt.to_claude_blocks() → 最多 4 个 cached system block
- Gemini/Relay: prompt.flatten() → 单个字符串
- 中间轮省 token: prompt.concise().flatten()（{tools} 展开为空）

"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass


PROMPT_SECTION_LABELS = {
    "main_template": "MAIN_TEMPLATE",
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


# ── main_template 占位符注册表 ──────────────────────────────────
#
# tier = cache 分块层级（1 稳定 → 4 高频）。render_blocks() 按"从头到当前
# 位置见过的最大 tier"切段，默认模板排序下与旧四层 block 划分逐字节一致。
PLACEHOLDER_TIERS: dict[str, int] = {
    "tools": 1,
    "projects": 2,
    "memories": 3,
    "relevant_history": 3,
    "today_timeline": 4,
    "pending_reminders": 4,
    "deadlines": 4,
    "weather": 4,
    "calendar": 4,
}
MAIN_TEMPLATE_PLACEHOLDERS = frozenset(PLACEHOLDER_TIERS)

# 已内联进 main_template 正文的旧散文 section（DB 行保留、UI 隐藏，用于回滚）。
# 不含 tools：concise() 需要在工具多轮中间轮抽掉它，所以保持独立 section。
LEGACY_STRUCTURED_KEYS = ("identity", "user_model", "system_mechanics",
                          "communication", "protocols")

_MAIN_PLACEHOLDER_RE = re.compile(r"\{(" + "|".join(PLACEHOLDER_TIERS) + r")\}")


def synthesize_main_template(sections: dict[str, str]) -> str:
    """从旧结构化 section 合成等价的 main_template。

    迁移和运行时 fallback（main_template 为空）共用：5 段散文按旧 Block 1
    顺序内联，9 个占位符按旧 Block 1-4 顺序排列，渲染结果与旧拼装逐字节一致。
    """
    parts = [p for k in LEGACY_STRUCTURED_KEYS if (p := (sections.get(k) or "").strip())]
    parts += ["{tools}", "{projects}", "{memories}", "{relevant_history}",
              "{today_timeline}", "{pending_reminders}", "{deadlines}",
              "{weather}", "{calendar}"]
    return "\n\n".join(parts)


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
    单一整体模板 + 占位符展开值（LT-129）。

    template 是 main_template 原文（含 {memories} 等占位符），values 是 9 个
    占位符的已格式化文本（可为空串）。render_blocks() 按 PLACEHOLDER_TIERS
    把渲染结果切成 ≤4 个单调段——占位符按稳定→易变排列时，切段结果与旧的
    静态/projects/memories/动态四层完全一致：
    - 记忆更新只失效 memories 所在段，动态数据更新只失效尾段，
      前缀段保持 Anthropic ephemeral cache 命中。
    - pending_reminders 注入尾段的目的：让 AI 一眼看到队列里已有什么
      follow-up，避免被聊天历史带回去重复 set 同一件事。

    Gemini/Relay 用 flatten() 拍平成单个字符串（不参与 prompt caching）。
    """
    mode: str  # "chat" | "poll"，仅用于调用方上游决策（如 DB 取数），不影响 prompt 内容
    template: str
    values: dict[str, str]

    def render_blocks(self) -> list[tuple[int, str]]:
        """渲染模板并按 cache tier 切段，返回 [(tier, text), ...]（≤4 段，非空）。

        切段规则：占位符替换的同时，追踪"从头到当前位置见过的最大 tier"，
        tier 上升就开新段；段间的字面文本归前一段（【】标题紧跟其数据）。
        tier 单调递增 ∈ {1..4}，所以最多 4 段。占位符的值只替换一次、
        不参与二次扫描（值里出现 {memories} 字样不会被展开）。
        """
        segments: list[list[str]] = [[]]
        tiers: list[int] = [1]
        pos = 0
        for m in _MAIN_PLACEHOLDER_RE.finditer(self.template):
            segments[-1].append(self.template[pos:m.start()])
            name = m.group(1)
            tier = PLACEHOLDER_TIERS[name]
            if tier > tiers[-1]:
                segments.append([])
                tiers.append(tier)
            segments[-1].append(self.values.get(name, ""))
            pos = m.end()
        segments[-1].append(self.template[pos:])

        out = []
        for tier, parts in zip(tiers, segments):
            # 空占位符留下的连续空行压回双换行 + 段级 strip，
            # 与旧 _join_nonempty 跳过空段的行为逐字节等价
            text = _CLEANUP_RE.sub("\n\n", "".join(parts)).strip()
            if text:
                out.append((tier, text))
        return out

    def flatten(self) -> str:
        """拍平为单个字符串（Gemini / Relay 用）。"""
        return _join_nonempty(*(text for _, text in self.render_blocks()))

    def to_claude_blocks(self) -> list[dict]:
        """
        构建 Anthropic system blocks（最多 4 个 cached block，上限即 cache_control 最大值）。

        顺序 = 稳定 → 易变（render_blocks 的 tier 单调性保证），
        前缀匹配最大化命中。
        """
        return [
            {
                "type": "text",
                "text": text,
                "cache_control": {"type": "ephemeral", "ttl": "1h"},
            }
            for _, text in self.render_blocks()
        ]

    def concise(self) -> PromptParts:
        """返回 {tools} 展开为空的副本（中间轮省 token）。"""
        c = copy.copy(self)
        c.values = dict(self.values, tools="")
        return c


# ── 动态段落格式化函数 ──────────────────────────────────────────

LABEL_MEMORIES = "【你现在记着的事】"
LABEL_RELEVANT_HISTORY = "【可能相关的历史片段】"
LABEL_TODAY_TIMELINE = "【今天完整 Timeline（已发生生活轨迹）】"
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


def _format_memories(memories: list[dict] | None,
                     memory_markdown: str | None = None) -> str:
    if memory_markdown and memory_markdown.strip():
        return f"{LABEL_MEMORIES}\n{memory_markdown.strip()}"
    if not memories:
        return ""
    lines = [f"- [id={m['id']}] {m['content']}" for m in memories]
    return f"{LABEL_MEMORIES}\n" + "\n".join(lines)


def _format_relevant_history(snippets: list[dict] | None) -> str:
    """memory v3 Part B2：语义检索命中的历史对话片段。
    每条展示 embedding_context（带前几轮上下文的拼接文本），短消息才有主题信息。"""
    if not snippets:
        return ""
    parts = []
    for s in snippets:
        fragment = (s.get("embedding_context") or s.get("content") or "").strip()
        if fragment:
            parts.append(fragment)
    if not parts:
        return ""
    hint = "（系统按语义相关度从过往对话自动检索，可能混入无关内容，自行判断取舍）"
    return f"{LABEL_RELEVANT_HISTORY}{hint}\n" + "\n---\n".join(parts)


def _format_today_timeline(events: list[dict] | None) -> str:
    if not events:
        return f"{LABEL_TODAY_TIMELINE}\n- 今天还没有 timeline 记录"
    lines = []
    for e in events:
        cat_part = e['category']
        if e.get("project_name"):
            cat_part += f" [{e['project_name']}]"
        end = e.get("end_time") or "进行中"
        line = f"- [ID={e['id']}] {e['start_time']} -> {end} | {cat_part} | {e['content']}"
        if e.get("notes"):
            line += f" | 备注: {e['notes']}"
        lines.append(line)
    return f"{LABEL_TODAY_TIMELINE}\n" + "\n".join(lines)


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
    memory_markdown: str | None = None,
    relevant_history: list[dict] | None = None,
    today_timeline: list[dict] | None = None,
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
    # main_template 为空时（迁移未跑/回滚窗口）现场从旧结构化 section 合成，
    # 保证任何数据状态下都能渲染出与旧拼装一致的 prompt
    template = prompt_sections["main_template"] or synthesize_main_template(prompt_sections)
    return PromptParts(
        mode=mode,
        template=template,
        values={
            "tools": prompt_sections["tools"],
            "projects": _format_projects(projects),
            "memories": _format_memories(memories, memory_markdown),
            "relevant_history": _format_relevant_history(relevant_history),
            "today_timeline": _format_today_timeline(today_timeline),
            "pending_reminders": _format_pending_reminders(pending_reminders),
            "deadlines": _format_deadlines(deadlines),
            "weather": _format_weather(weather),
            "calendar": _format_calendar(calendar),
        },
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
        "[决策辅助] 刚查了 pending reminder。程序会在触发时自动合并临近提醒；"
        "如果用户明确要替换或取消已有提醒，"
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
