"""旧 prompt 拼装逻辑的冻结副本（摘自 commit 5d23fe3 的 bot/prompts.py）。

golden parity test 的 oracle：LT-129 之前的四层 block 拼装。
⚠️ 不随主代码演化，永远不要"顺手同步"这里的逻辑——它冻结的就是历史行为。
_format_* 格式化函数没有冻结：它们在 LT-129 中未变，测试直接复用主代码里的，
这样 oracle 只覆盖真正被改掉的"拼装"部分。
"""
import copy
import re

_CLEANUP_RE = re.compile(r"\n{3,}")


def _join_nonempty(*parts: str) -> str:
    joined = "\n\n".join(p.strip() for p in parts if p and p.strip())
    return _CLEANUP_RE.sub("\n\n", joined).strip()


class LegacyPromptParts:
    def __init__(self, *, identity, user_model, system_mechanics, communication,
                 protocols, tools, projects="", memories="", relevant_history="",
                 today_timeline="", pending_reminders="", deadlines="",
                 weather="", calendar=""):
        self.identity = identity
        self.user_model = user_model
        self.system_mechanics = system_mechanics
        self.communication = communication
        self.protocols = protocols
        self.tools = tools  # None = concise
        self.projects = projects
        self.memories = memories
        self.relevant_history = relevant_history
        self.today_timeline = today_timeline
        self.pending_reminders = pending_reminders
        self.deadlines = deadlines
        self.weather = weather
        self.calendar = calendar

    def static_text(self) -> str:
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
        return self.projects

    def memories_text(self) -> str:
        return _join_nonempty(self.memories, self.relevant_history)

    def dynamic_text(self) -> str:
        return _join_nonempty(
            self.today_timeline,
            self.pending_reminders,
            self.deadlines,
            self.weather,
            self.calendar,
        )

    def flatten(self) -> str:
        return _join_nonempty(
            self.static_text(),
            self.stable_context_text(),
            self.memories_text(),
            self.dynamic_text(),
        )

    def to_claude_blocks(self) -> list[dict]:
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

    def concise(self) -> "LegacyPromptParts":
        c = copy.copy(self)
        c.tools = None
        return c


def legacy_from_formatted(sections: dict[str, str], values: dict[str, str]) -> LegacyPromptParts:
    """按旧 build_prompt 的方式构造：sections 是 strip 后的散文，values 是
    _format_* 的输出（与新 PromptParts.values 同一份数据）。"""
    return LegacyPromptParts(
        identity=sections.get("identity", ""),
        user_model=sections.get("user_model", ""),
        system_mechanics=sections.get("system_mechanics", ""),
        communication=sections.get("communication", ""),
        protocols=sections.get("protocols", ""),
        tools=sections.get("tools", ""),
        projects=values["projects"],
        memories=values["memories"],
        relevant_history=values["relevant_history"],
        today_timeline=values["today_timeline"],
        pending_reminders=values["pending_reminders"],
        deadlines=values["deadlines"],
        weather=values["weather"],
        calendar=values["calendar"],
    )
