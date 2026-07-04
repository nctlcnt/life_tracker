"""
Dispatch POC 双层架构的 prompt 模块

复用 DB 里的 prompt sections 组装 dispatch-specific prompt:

- SMALL_DECIDE        — 小模型决策: escalate (输出 [ESCALATE]) 或直接闲聊
- SMALL_PARAPHRASE    — 小模型把 BIG_WORKER 的 FACTS 包语气
- BIG_WORKER          — 大模型工具执行 + 三段结构化输出
- ESCALATION_TRIGGER_LIST — 触发清单白话版, 嵌入 SMALL_DECIDE 内

详见 plans/2-specs/dispatch-poc.md
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from bot.prompts import (
    PromptParts,
    empty_prompt_sections,
    _join_nonempty,
)


# ══════════════════════════════════════════════════════════════
# SMALL_DECIDE — 小模型一次性决策
# ══════════════════════════════════════════════════════════════

def build_small_decide_prompt(sections: dict[str, str] | None = None) -> str:
    """Build SMALL_DECIDE from DB-managed prompt sections."""
    s = empty_prompt_sections()
    if sections:
        s.update({k: v for k, v in sections.items() if k in s})
    return _join_nonempty(
        s["identity"],
        s["user_model"],
        s["communication"],
        s["dispatch_escalation_trigger"],
        s["dispatch_small_decide_output"],
    )


# ══════════════════════════════════════════════════════════════
# SMALL_PARAPHRASE — 把 BIG_WORKER 的 FACTS 包语气
# ══════════════════════════════════════════════════════════════

def build_small_paraphrase_prompt(facts_to_convey: str,
                                  sections: dict[str, str] | None = None) -> str:
    """组装 SMALL_PARAPHRASE 的 system prompt, FACTS 直接嵌进去。"""
    s = empty_prompt_sections()
    if sections:
        s.update({k: v for k, v in sections.items() if k in s})
    facts_block = "## 【内部 FACTS — 不是用户说的, 是后台给你的事实清单】\n" + facts_to_convey.strip()
    return _join_nonempty(
        s["identity"],
        s["user_model"],
        s["communication"],
        s["dispatch_paraphrase_task"],
        facts_block,
    )


# ══════════════════════════════════════════════════════════════
# BIG_WORKER — 大模型工具执行 + 三段结构化输出
# ══════════════════════════════════════════════════════════════

def build_big_worker_parts(
    *,
    sections: dict[str, str] | None = None,
    projects: str = "",
    memories: str = "",
    today_timeline: str = "",
    pending_reminders: str = "",
    deadlines: str = "",
    weather: str = "",
) -> PromptParts:
    """
    组装 BIG_WORKER 的 PromptParts. 复用现有 cache block 结构, 把人格/调性
    section 留空, 替成 DB 里的系统机制和工具策略 + 输出格式。
    """
    s = empty_prompt_sections()
    if sections:
        s.update({k: v for k, v in sections.items() if k in s})
    # 人格/调性留空：模板只保留系统机制散文 + 工具与数据占位符
    template = _join_nonempty(
        s["system_mechanics"],
        "{tools}",
        "{projects}",
        "{memories}",
        "{today_timeline}",
        "{pending_reminders}",
        "{deadlines}",
        "{weather}",
    )
    return PromptParts(
        mode="chat",
        template=template,
        values={
            "tools": _join_nonempty(s["tools"], s["dispatch_big_worker_output"]),
            "projects": projects,
            "memories": memories,
            "relevant_history": "",
            "today_timeline": today_timeline,
            "pending_reminders": pending_reminders,
            "deadlines": deadlines,
            "weather": weather,
            "calendar": "",
        },
    )


# ══════════════════════════════════════════════════════════════
# 输出 parser — 把模型的 raw text 解成结构
# ══════════════════════════════════════════════════════════════

ESCALATE_MARKER = "[ESCALATE]"
SILENT_MARKER = "[SILENT]"


@dataclass
class SmallDecideResult:
    """SMALL_DECIDE 的解析结果。"""
    escalate: bool
    chat_text: str  # 不 escalate 时填用户回复; escalate 时为空


def parse_small_decide_output(raw: str) -> SmallDecideResult:
    """解析 SMALL_DECIDE 的输出: 第一非空行是 [ESCALATE] → 升级, 否则全部当回复。"""
    text = raw.strip()
    if not text:
        return SmallDecideResult(escalate=False, chat_text="")
    first_line = text.splitlines()[0].strip()
    if first_line == ESCALATE_MARKER:
        return SmallDecideResult(escalate=True, chat_text="")
    return SmallDecideResult(escalate=False, chat_text=text)


@dataclass
class BigWorkerOutput:
    """BIG_WORKER 的结构化输出。"""
    escalate_state: str  # "open" | "close"
    actions: list[str]
    facts: list[str]
    raw: str  # 原始文本, 调试用

    @property
    def is_silent(self) -> bool:
        return any(SILENT_MARKER in f for f in self.facts)


_SECTION_HEADER_RE = re.compile(r"^\[(ESCALATE_STATE|ACTIONS|FACTS_TO_CONVEY)\]\s*$", re.MULTILINE)


def parse_big_worker_output(raw: str) -> BigWorkerOutput:
    """
    解析 BIG_WORKER 的三段输出, 缺字段 fail-safe:
    - escalate_state 缺 / 非 open|close → close
    - actions / facts 缺 → 空列表
    """
    sections: dict[str, str] = {}
    matches = list(_SECTION_HEADER_RE.finditer(raw))
    for i, m in enumerate(matches):
        name = m.group(1)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        sections[name] = raw[start:end].strip()

    state_raw = sections.get("ESCALATE_STATE", "").strip().lower()
    state = state_raw if state_raw in ("open", "close") else "close"

    def parse_bullets(text: str) -> list[str]:
        items = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("- "):
                items.append(line[2:].strip())
            elif line.startswith("-"):
                items.append(line[1:].strip())
            else:
                items.append(line)
        return [x for x in items if x and x != "无"]

    return BigWorkerOutput(
        escalate_state=state,
        actions=parse_bullets(sections.get("ACTIONS", "")),
        facts=parse_bullets(sections.get("FACTS_TO_CONVEY", "")),
        raw=raw,
    )


def format_facts_for_paraphrase(facts: list[str]) -> str:
    """把 BIG_WORKER 解析出的 facts 列表格式化, 喂给 SMALL_PARAPHRASE。"""
    if not facts:
        return SILENT_MARKER
    return "\n".join(f"- {f}" for f in facts)
