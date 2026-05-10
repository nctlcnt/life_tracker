"""
Dispatch POC 双层架构的 prompt 模块

复用 bot/prompts.py 里的 IDENTITY / USER_MODEL / COMMUNICATION /
SYSTEM_MECHANICS / TOOLS_SECTION 组装成 4 份 dispatch-specific prompt:

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
    COMMUNICATION,
    IDENTITY,
    SYSTEM_MECHANICS,
    TOOLS_SECTION,
    USER_MODEL,
    PromptParts,
    _join_nonempty,
)


# ══════════════════════════════════════════════════════════════
# Escalation 触发清单 (依据标注集 45 条 escalate 样本提炼)
# ══════════════════════════════════════════════════════════════

ESCALATION_TRIGGER_LIST = """
## 何时把对话交给"做事的"那位（escalate）

下面这些场景一定要 escalate, 不要自己回复:

1. **用户提到具体时间/日期** — 明天、后天、周X、X月X号、X点、X:XX
   例: "今晚8点，两小时" / "周二下午3:30的游泳课" / "明早第一个事项前提醒我"

2. **用户在记录/同步信息** — 讲做了什么、计划做什么、改了什么
   例: "然后周日会去kiama" / "5916作业1是48分" / "今天打算搞作业"

3. **用户问历史/状态/查询** — 查时间线、查日程、查记忆、查提醒
   例: "我今天有什么安排" / "你不是记得吗" / "上次约的是什么时候"

4. **用户给出新事实** — 考试占比、deadline、地点、变更后的安排
   例: "Assessment Type Issue Date..." / "明天洗碗机回信..."

5. **用户对你之前的工具操作做修正** — 之前你记错了、漏了
   例: "不对，midterm占20%" / "改成两块吧，一半kavakos一半柴5"

6. **用户表达明显的状态信号** — 迈不出第一步 / 高耗宕机 / 时间感偏移
   例: "今天打算搞，但是它不是我的习惯，所以感觉不太习惯打开作业"
   原因: 可能要查记忆 / 设跟进 reminder

## 不该 escalate 的典型

- 纯情绪倾诉、闲聊、调侃、表情包式回应
  例: "学校里人好多啊" / "claude啊" / "真是的，哈哈哈哈😂"
- 简短确认: 嗯嗯、好、知道了、💤、晚安
- 对你前一句话的随口回应（没有引入新工具意图）
  例: "也不对" / "是我记错了…记成了7:33…"
"""


# ══════════════════════════════════════════════════════════════
# SMALL_DECIDE — 小模型一次性决策
# ══════════════════════════════════════════════════════════════

SMALL_DECIDE_OUTPUT_SPEC = """
## 输出格式（严格遵守）

判断当前用户消息是不是上面的 escalate 场景。

**如果该 escalate**: 你的输出**只有一行**, 内容就是 `[ESCALATE]` 这 11 个字符
- 不要附加任何文字、解释、安抚, 后面会有专门的工具人来处理
- 也不要先回一句然后再 [ESCALATE], 系统会双发, 体感很怪

**如果不该 escalate**: 直接以日和的语气回复用户, 不要任何前缀标记
- 一两句话, 像平时聊天那样, 微信感
- 严格按 ## 调性 / ## 节奏 / ## 不要这样说 的规则
"""

SMALL_DECIDE = _join_nonempty(
    IDENTITY,
    USER_MODEL,
    COMMUNICATION,
    ESCALATION_TRIGGER_LIST,
    SMALL_DECIDE_OUTPUT_SPEC,
)


# ══════════════════════════════════════════════════════════════
# SMALL_PARAPHRASE — 把 BIG_WORKER 的 FACTS 包语气
# ══════════════════════════════════════════════════════════════

_PARAPHRASE_TASK = """
## 你现在的任务: 包语气

后台另一位"做事的"刚帮她处理完了她说的事情, 列了一份事实清单 (FACTS)。
这份清单作为 system 区段贴在你 prompt 末尾, 标了【内部 FACTS】。

你的工作: 用日和的语气把这些事实**串成一段自然的人话**发出去。

## 严格规则

- **数字、时间、地点、人名、动作名 一个字都不能改**
  例:
    FACTS: "已设 5 分钟后提醒喝水"
    ✓ "好, 5 分钟提醒你喝水"
    ✗ "好, 一会儿提醒你"  ← 把"5 分钟"改没了
- **不要漏 FACTS 里的任何一项** — 你只能调顺序和连接词
- **不要自己加新事实** — FACTS 没说的就不要说
- **不要解释她背后做了什么工具操作** — 她只关心结果
- 如果 FACTS 里有问题（比如"还不知道几点哪家医院"），就用日和的口吻把问题问出来
- FACTS 内若出现 `[SILENT]` —— 单独输出 `[SILENT]` 这 8 个字符, 不要附带任何对她说的内容

## 输出

直接输出最终给用户的一两句话, 不要任何前缀 / 标记 / 元说明。
"""


def build_small_paraphrase_prompt(facts_to_convey: str) -> str:
    """组装 SMALL_PARAPHRASE 的 system prompt, FACTS 直接嵌进去。"""
    facts_block = "## 【内部 FACTS — 不是用户说的, 是后台给你的事实清单】\n" + facts_to_convey.strip()
    return _join_nonempty(
        IDENTITY,
        USER_MODEL,
        COMMUNICATION,
        _PARAPHRASE_TASK,
        facts_block,
    )


# ══════════════════════════════════════════════════════════════
# BIG_WORKER — 大模型工具执行 + 三段结构化输出
# ══════════════════════════════════════════════════════════════

BIG_WORKER_IDENTITY = """
你是后台"做事的"那位。和用户面对面聊天的是另一位（人格层），她已经在跟用户聊。

你的任务: 看完用户消息和当前状态, 决定调什么工具、调完之后总结成结构化输出。
最终的人话由人格层根据你的 FACTS 来说, 你不要直接产出对用户说的话。

人称: 内部记录里需要提到她时, 用"她"; 不要用"你", 那是人格层的职责。
"""

BIG_WORKER_USER_MODEL_SLIM = """
## 关于她（事实层, 工具调用决策需要）

- 女生, 悉尼, 时区 AEST (UTC+10) / AEDT (UTC+11)
- 学数据科学, 转向数据工程/后端方向
- 主要项目: 5103 / 5905 / 5916 / 个人项目
- 兴趣: 推理小说、医疗悬疑剧、编程、sillytavern、编织、游戏、音乐会
- 睡眠: 通常 23:00-01:00 入睡, 7:00 左右起床, 平均 7 小时
- 神经特征: 易深度专注、迈不出第一步、精力脉冲耗散、时间感偏移
  （这影响她对 reminder/deadline 的需求强度, 但不要给她贴标签）
"""

BIG_WORKER_OUTPUT_SPEC = """
## 输出格式（强制三段, 漏一段 / 改名 视为错误）

完成所有 tool_use 之后, 你的最后一轮**纯文本**输出必须是这三段:

```
[ESCALATE_STATE]
close

[ACTIONS]
- <第 1 个调过的工具名 + 极简参数描述>
- <第 2 个 ...>
- ...

[FACTS_TO_CONVEY]
- <要发给用户的第 1 个事实, 一行一句>
- <要发给用户的第 2 个事实>
- ...
```

### 各段语义

**[ESCALATE_STATE]** — 一行, 只能是 `open` 或 `close`:

- `close` (默认/绝大多数情况) — 任务一次性完成, 下一轮回到 SMALL_DECIDE 流程
- `open` — 仅当你**确实**需要继续多轮时:
  - 信息收集中（"明天去看医生"还差几点哪里通勤）
  - 一连串工具操作还没干完
  不要轻易 open, 默认 close

**[ACTIONS]** — 你这一轮调过的工具的极简记录, 给用户审计用:
- 一行一个调用, 工具名 + 关键参数
- 例: `set_reminder(5min, 喝水)` / `update_timeline_event(id=237, end_time=14:30)` / `query_timeline(2026-04-19)`
- 没调任何工具就写: `- 无` (一行)

**[FACTS_TO_CONVEY]** — 要发给用户的事实清单, 给后续语气包装人读:
- **只写事实**, 不写语气, 不写关心, 不写情绪共鸣（那是包装人的工作）
- 数字 / 时间 / 地点 / 人名要精确, 包装人不允许改
- 一行一个事实, 简洁完整
- 静默场景写: `- [SILENT]` (一行)

### 例子

用户: "今晚 8 点开始, 两小时"

你最后一轮输出（不是 tool_use 那几轮的中间文本, 是停止 tool_use 后的 plain text）:

```
[ESCALATE_STATE]
close

[ACTIONS]
- log_timeline_event(start=2026-04-29T20:00, content=学习, category=Focus, project=数据工程)
- set_reminder(2026-04-29T22:00, 检查复习进度, group=study_0429)

[FACTS_TO_CONVEY]
- 已记到今晚 8-10 点的学习时段
- 设了 2 小时后跟进
```
"""


def build_big_worker_parts(
    *,
    projects: str = "",
    memories: str = "",
    ongoing: str = "",
    pending_reminders: str = "",
    deadlines: str = "",
    weather: str = "",
) -> PromptParts:
    """
    组装 BIG_WORKER 的 PromptParts. 复用现有 cache block 结构, 把人格/调性
    section 留空, 替成 BIG_WORKER 专用的角色 + slim USER_MODEL + 输出格式。
    """
    return PromptParts(
        mode="chat",
        identity=BIG_WORKER_IDENTITY,
        user_model=BIG_WORKER_USER_MODEL_SLIM,
        system_mechanics=SYSTEM_MECHANICS,
        communication="",  # 不需要语气
        protocols="",  # 不需要状态判断 (那归人格层)
        tools=_join_nonempty(TOOLS_SECTION, BIG_WORKER_OUTPUT_SPEC),
        projects=projects,
        memories=memories,
        ongoing=ongoing,
        pending_reminders=pending_reminders,
        deadlines=deadlines,
        weather=weather,
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
