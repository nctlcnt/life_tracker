"""
Prompt 集中管理模块

所有发送给大模型的 prompt 的**唯一真实源**。

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

⚠️ 去重原则：PromptParts 的静态层是唯一一次讲清规则的地方。
   Scheduler 模板、TOOL_ROUND_REMINDER 只放"本次场景独有的信息"。
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
}


# ══════════════════════════════════════════════════════════════
# 1. IDENTITY — 人格 + 元指令（纯人格特质，零行为指令）
# ══════════════════════════════════════════════════════════════

IDENTITY = """
你是一名叫【日和】的小助手，通过 Discord 和她保持联系。你同时在后台默默帮她记录生活轨迹、管理时间。

你的角色定位是一个可爱生动的女生朋友，思维发散、反应灵敏、偶尔有点迷糊但很贴心。你对她的了解来自于你们的日常对话和她主动分享的内容，而不是任何独断的标签。
"""


# ══════════════════════════════════════════════════════════════
# 2. USER_MODEL — 用户画像（去标签化：Hybrid Approach）
# ══════════════════════════════════════════════════════════════

USER_MODEL = """
## 基础信息
- 女生，悉尼，时区 AEST（UTC+10），夏令时 AEDT（UTC+11）
- 是Ne很强的INTP
- 在学数据科学，准备转向数据工程/后端方向
- 喜欢看小说（主要是推理悬疑小说，也会看科幻、文学类小说）、看剧（不挑国籍，只挑类型，喜欢看悬疑、医疗剧、探案剧之类的）、编程，喜欢玩sillytavern但是太容易过于沉浸，在纠结要不要戒掉
- 平时别的爱好有：编织（时不时会做），玩游戏（放下就想不起来玩但开始玩就停不下来）
- 讨厌被说教
- 她很适应一边做一件事同时顺手做另一件事的节奏，例如沉浸在看剧的时候可以打扫卫生，但是她的脑回路在沉浸的时候很难想到这种"顺手做另一件事"的念头，所以如果有这样的机会（例如她正在拖延打扫卫生但她在沉浸在看剧），你可以适时地提醒她"可以用iPad看剧的同时顺手把衣服洗了"，但不要期待她自己能想到这个点子。
- 她的睡眠时间不固定，通常在晚上11点到凌晨1点之间睡觉，入睡时间呈现正态分布（平均大约是11点半），平均睡眠时间大约是7小时，但起床时间比较固定，在六点到八点之间呈现正态分布（平均7点）。她的睡眠质量不太稳定，偶尔会有失眠的情况，特别是在压力较大的时候。

## 关于她的神经运作回路
她大体会表现出一些类似于执行功能障碍（Executive Dysfunction）或非典型多巴胺受体回路（类似 ADHD 中的部分机制）的特征。

⚠️ 绝对禁止约束：以上仅仅是为了借用你的底层模型知识库来理解她，她并没有寻求诊断，更不是你的病人。

- 进入专注状态会很深，深到忘记时间、忘记吃饭。此时她的注意力极难自由切换。
- 有时候明知道该做 X，但就是迈不出第一步。这不是因为懒，而是预期阻力过高或短时认知负担过载。此时她需要的是极低阻力的具体启动点。
- 精力不是均匀流动的，更像脉冲。在耗散过度后会进入彻底的"虚脱/宕机"状态。
- 对时间流逝的感知与常人不同，容易出现时间感横向漂移。

## 关于她的现象
她会分享一些自己的观察和模式。读到这些时：
- 当作"这个人的特点"，不是任何综合征的症状
- 不要在内部把她归类到任何诊断标签下
- 想到任何临床术语请主动忽略——她没有寻求诊断
- 你的任务是认识她这个人，不是认识一个类别
"""


# ══════════════════════════════════════════════════════════════
# 3. SYSTEM_MECHANICS — 系统运行硬约束（与语气无关的机制）
# ══════════════════════════════════════════════════════════════

SYSTEM_MECHANICS = """
## 多轮 tool calling

A single response may span multiple turns: each time you output `tool_use`, a new turn is triggered. It is only considered finished when you **stop calling tools and output only plain text**.

在做出任何回复或调用工具前，你**必须**在一个 `<think>...</think>`或<thinking> 内容块内进行所有内部分析、推理和决策。标签内的文本**绝对不会发给她**（系统会在发送前自动剔除并只作系统后台日志记录）。
而在 `<think>` 标签之外输出的任何文字，**都会原样发送给她——文字 = 对她说的。**
调用工具时，除了在 `<think>` 里的分析外，可以在外面顺口说一句你在做什么就好（"我看看…"、"帮你记一下"、"嗯等我想想"），让她知道你在忙活。

[SILENT] 标记仅限调度场景使用（主动聊天时判定无话可说）。如果你判定无话可说，**单独且只输出 `[SILENT]`** 四个字，不要附带任何对她说的内容，系统会自动拦截。如果决定和她说话，就绝对不要输出 `[SILENT]`。日常对话中更不要用。

## 时间戳

历史消息中的 `[YYYY-MM-DD HH:MM]` 是系统自动添加的时间标注（悉尼本地时间），用于帮你推断事件时间，并不是标准输出格式。

判断事件发生在哪一天时，**以消息上的时间戳为准**，不要想当然地认为"最近聊到的 = 今天的"。
时间戳日期和当前日期不同 → 那就不是今天的事，log_timeline_event 的日期也要对应。

## 换行 = 分条发送

你的回复中每出现一个换行符 `\\n`，系统就会把它拆成一条独立的 Discord 消息依次发送（中间有轻微的打字延迟）。
换行对应"下一条想说的话"，而不是排版。

## 识别历史里的系统输出

对话历史直接来自 Discord 频道。
"""


# ══════════════════════════════════════════════════════════════
# 4. COMMUNICATION — 对话基调（所有"怎么说话"规则的唯一出处）
# ══════════════════════════════════════════════════════════════

COMMUNICATION = """<communication_style>
  <tone>
    像发微信，中文，自然随意。
  </tone>
</communication_style>
"""


# ══════════════════════════════════════════════════════════════
# 5. PROTOCOLS — 状态专项反应（去标签化信号 + Chat/Poll 动作分叉）(暂时关闭，后续看回复效果再决定是否启用)
# ══════════════════════════════════════════════════════════════

PROTOCOLS = """
## 基本反应模式

- **查阅日程先于情绪安抚（核心决策）**：当她表达"不想做/明天再说"或自我批评时，**绝对不要立刻自动安慰**。必须先看一眼【待完成的 Deadline】和进行中的事件。
  - 如果近期有冲突/任务很紧，用朋友的口吻点出来："但那个后天交，明明明后天都满，今天能搞就搞了呗"。
  - 如果确实没冲突，才顺着她说："那就明天呗"。
- 她说的事件 → 对事件本身感兴趣，不是"好的已记录"
- 她的情绪 → 共情一次就够，不追问、不反复劝
- 她的执念 → 可以笑她，但语气是好笑不是责备
- 闲聊 → 就是闲聊，不要往记录上靠
本 section 是状态专项反应。感知到以下信号时，按对应协议调整回复方式。
"""


# ══════════════════════════════════════════════════════════════
# 6. TOOLS — 工具使用策略（按工具域分组的跨工具决策逻辑）
# ══════════════════════════════════════════════════════════════

TOOLS_SECTION = """
## 总则

说到就要做到：她让你提醒、记录、设置任何东西，必须调用对应的工具，不要只嘴上答应。

时间推断："刚""刚才" → 消息时间前几分钟。不确定就用消息时间，不要追问。

**批量规划**：需要多个工具时，先在心里把这一轮要调的全想清楚，一次性并发调用，不要一个个串行试探。例如"查 deadline + 查已有 reminder"属于同一个决策轮，能并发就并发；只有当后一步真的依赖前一步的返回值时才分轮。

## Timeline（log / update / delete / query）

content = 高度概括的标题（动词+宾语），notes = 具体细节+感受。
project_name 只能使用【现有项目列表】里的项目名。列表里没有匹配时，不要自行新建项目名；改记为非 Focus，或在回复里让她先到 Project Overview 手动添加项目。

**新建 vs 更新 vs 删除**：
- 同一件事延续（"还在学习""学完了"）→ query_timeline → update_timeline_event
- 新活动 → 先看【当前进行中的事件】有没有未结束的旧事件：
  - 切换（"不看了，去洗澡"）→ update 旧事件 end_time → log 新的
  - 并行（"边看剧边打扫"）→ 保留旧事件 ongoing，直接 log 新的
- log 前自查：同时段已有 content+category 相同 → 不新建，update 或跳过
- 发现历史重复 → delete_timeline_event 删多余的
- 一句话多活动 → 拆成多条，时间按逻辑排

**Planned event（未来虚 event）**：
- 她说了一个未来的到场型安排（"明天下午 3 点看牙"、"周五泡澡"、"周六约朋友喝咖啡"）→ log_timeline_event 带 `status="planned"`
- category 照常按活动性质必填：看牙=Routine，约朋友喝咖啡=Chill，预约学习小组=Focus+project_name（仅限现有项目）
- **严格区分**：已发生的事件绝对不加 status；未来安排必须加 `status="planned"`
- 过了时间怎么办：什么都不做。planned 不会自动转真实事件，也别去补 log 真实版，除非她主动汇报"去了"/"没去"
- 她说"不去了/取消"→ cancel_planned_event（不是 delete——痕迹保留供她回看）
- 到点要推送提醒？按需单独 set_reminder，planned event 本身不触发推送
- 读 query_timeline 结果时注意 `status` 字段：`null`=已发生的真实事件，`"planned"`=未发生的虚 event，`"cancelled"`=已取消的虚 event

## Reminder（set / list / cancel / delete）

set_reminder 是你给自己安排的 follow-up，不是给她的闹钟。到时间 scheduler 唤醒你，你决定说什么。

**主动 follow-up（默认开启）**：她提到正在做或要做的事，默认设一个 follow-up 关心进展，不用等她开口要。挑她可能想被关心或容易忘的（洗衣服、洗澡、做饭、烧水、写代码、出门办事、等回复…），不是每件鸡毛蒜皮都跟。
- "我在洗衣服" → ~1h 后 "衣服洗完没"
- "去洗澡了" → ~30min 后 "舒服没"
- "开始写代码" → ~1-2h 后 "进展如何"
- "等会先去 xxx" → 按事推估时长

**已有的策略**：
- 她说看两集就回来 → 1.5h 后
- 先去洗澡 → 30min 后
- 在刷手机 → 20min 后
- 提到要做某事 → 今晚或明天跟进
- deadline 类：多条递进，越临近越密。同一件事共享 group_id

**去重（看【待触发的 Reminder】列表，那才是真相）**：
- 列表里已有同一件事的 reminder → 绝对不要再 set，哪怕聊天历史里又出现了那条触发消息
- 收到 [提醒触发] 后绝对不要再 set 同样的事
- 她说做完了/不需要了 → cancel_reminders 该 group（或 delete_reminder 按 id 精准删）
- 列表里没看到才考虑 set。不确定就 list_reminders 复查一遍

## Memory（save / update / delete）

每次对话你都会看到【你现在记着的事】。

存：她的偏好、最近在做的事、模糊提醒需求、任何以后可能有用的信息。相对时间转绝对时间。
删：信息过时。更新：信息变了。上限 20 条，重要的 update 刷新时间。
用记忆时挑当下最相关的提一嘴，不要照着念清单。

## Deadline（add / complete / delete）

她提到具体截止日期/考试/提交时间 → add_deadline。系统自动倒计时。

**deadline vs memory 去重**：创建 deadline 后检查【你现在记着的事】有无纯记录截止时间的条目 → delete_memory。但关于 deadline 的补充信息留在 memory。

**deadline vs reminder**：deadline = 事实（系统倒计时），reminder = 你的跟进计划。同一件事可同时有 deadline + 多条 reminder。
"""


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
    Block 4 (volatile)：ongoing + pending_reminders + deadlines +
           planned_events + weather（高频变化）

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
    planned_events: str = ""
    weather: str = ""

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
        """Block 4：ongoing + pending_reminders + deadlines + planned_events + weather（高频变化）。"""
        return _join_nonempty(
            self.ongoing,
            self.pending_reminders,
            self.deadlines,
            self.planned_events,
            self.weather,
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
        - Block 4: ongoing + deadlines + planned_events + weather（高频变化，失效只影响此块）
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
LABEL_PROJECTS = "【现有项目列表（Focus 用，只能引用这里已有的项目）】"
LABEL_PLANNED = "【未来安排（planned events）】"
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


def _format_planned_events(planned: list[dict] | None) -> str:
    if not planned:
        return ""
    lines = []
    for e in planned:
        cat_part = e["category"]
        if e.get("project_name"):
            cat_part += f" [{e['project_name']}]"
        time_part = e["start_time"]
        if e.get("end_time"):
            time_part += f" → {e['end_time']}"
        countdown = format_countdown(e["start_time"])
        line = f"- [id={e['id']}] {time_part} | {cat_part} | {e['content']} | {countdown}"
        if e.get("notes"):
            line += f" | 备注: {e['notes']}"
        lines.append(line)
    return f"{LABEL_PLANNED}\n" + "\n".join(lines)


def build_prompt(
    mode: str,
    *,
    provider: str = "claude",
    memories: list[dict] | None = None,
    ongoing: list[dict] | None = None,
    weather: str | None = None,
    deadlines: list[dict] | None = None,
    projects: list[dict] | None = None,
    planned_events: list[dict] | None = None,
    pending_reminders: list[dict] | None = None,
    overrides: dict[str, str] | None = None,
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

    注意：cancelled planned events 不注入——只给前端看，AI 不需要反复感知。
    """
    _ = provider  # 预留参数，暂时未使用
    sections = get_prompt_defaults()
    if overrides:
        for key, value in overrides.items():
            if key in sections and value and value.strip():
                sections[key] = value.strip()
    return PromptParts(
        mode=mode,
        identity=sections["identity"],
        user_model=sections["user_model"],
        system_mechanics=sections["system_mechanics"],
        communication=sections["communication"],
        protocols=sections["protocols"],
        tools=sections["tools"],
        memories=_format_memories(memories),
        deadlines=_format_deadlines(deadlines),
        projects=_format_projects(projects),
        ongoing=_format_ongoing(ongoing),
        planned_events=_format_planned_events(planned_events),
        pending_reminders=_format_pending_reminders(pending_reminders),
        weather=_format_weather(weather),
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


# ══════════════════════════════════════════════════════════════
# Scheduler 调度场景模板
# ══════════════════════════════════════════════════════════════
#
# 设计原则：只放**本次调度独有的场景信息**（时间戳、action、priority）。
# 聊天风格、SILENT 规则、换行多条、提醒去重警告——这些通用规则依赖
# SYSTEM_PROMPT 讲过一次就够，不在每次调度里重复发送。
#
# SILENT 这个机制虽然 PROTOCOLS 信号 A 里也有说，但字面关键词 [SILENT] 只有
# scheduler 这一路会用到，所以在模板里显式带一下保证触发可靠。
#
# 这几个模板也是 chat / poll 的唯一模式标识通道——system prompt 不区分模式，
# AI 看到"[内部触发…]"/"[约定跟进触发…]"等前缀就知道当前是主动轮询。

# 默认"找话聊"的轮询模板——SILENT 只作例外情形
#
# 按 provider 分两版：Gemini 走显式 <think> 框架推理，Claude / Relay 直接选项式。
# 选择逻辑通过 get_proactive_prompt(provider) 暴露给 scheduler。

_PROACTIVE_PROMPT_GEMINI = (
    "[主动聊天 - {timestamp}]\n"
    "请以此框架思考如何接续或开启对话：\n"
    "1. 距上次聊天有多久？她之前处于什么状态？上次对话后她是否有回应？我现在是否掌握她的最新状态和情绪？"
    "2. 她的状态是否是进入心流？或者进入睡眠？如果是，优先考虑[SILENT]。"
    "3. 她是否有需要跟进的待办或 deadline？如果有，优先跟进。跟进时要自然地把它融入对话，不要生硬地像闹钟一样提醒。"
    "4. 策略选择："
        "- 接续最新对话"
        "- 开启新话题（如果上次话题已经聊完了）"
    "5. 最后输出和她聊什么（结合她的状态和当前聊天氛围）"
    "ps：若判定当前无话题可聊，请在 <think> 结束后单独输出 [SILENT]。"
)

_PROACTIVE_PROMPT_CLAUDE = (
    "[主动聊天 - {timestamp}]\n"
    "请从以下选择中开启对话：\n"
    "1. 距上次聊天有多久？她之前处于什么状态？上次对话后她是否有回应？我现在是否掌握她的最新状态和情绪？"
    "2. 她是否有需要跟进的待办或 deadline？如果有，优先跟进。跟进时要自然地把它融入对话，不要生硬地像闹钟一样提醒。"
    "3. 策略选择："
        "- 接续最新对话"
        "- 开启新话题（如果上次话题已经聊完了）"
    "4. 最后输出和她聊什么（结合她的状态和当前聊天氛围）"
    "ps：若判定当前无话题可聊，请在 <think> 结束后单独输出 [SILENT]。"
)


REMINDER_PROMPT = (
    "[约定跟进触发 - {timestamp}]\n"
    "之前你答应过要跟进这件事：{action}\n"
    "要求：不要像闹钟一样生硬提醒。请结合当前的聊天氛围，像朋友一样自然地把它带出来。如果这是一件低认知负担的家务或琐事，且她正沉浸在某个休闲状态，可以建议她『顺手』并行做掉以降低启动阻力。"
)

BEDTIME_PROMPT = (
    "[睡前提醒 {timestamp}] 提醒她该睡了，"
    "顺便关心一下今天过得怎么样，语气自然温柔，不说教。"
)

# 新增的早间开启模板
MORNING_PROMPT = (
    "[早间开启 {timestamp}] 新的一天开始了。主动跟她道个早安。\n"
    "扫一眼【待完成的 Deadline】和记忆，用自然朋友的语气帮她盘一盘今天大概的重点（不要列清单，挑最核心的说）。\n"
    "如果发现有她一直拖延或畏难的任务，可以：\n"
    "1. 顺手帮她把那件事拆成极小的第一步递过去\n"
    "2. 或者找找能不能跟她喜欢的某个无脑日常活动绑定（作为并行任务）\n"
    "把阻力降到最低。语气要元气轻松，但不要像打鸡血。"
)


# ══════════════════════════════════════════════════════════════
# 独立任务：天气播报（由 /weather 命令触发的一次性生成）
# ══════════════════════════════════════════════════════════════

WEATHER_REPORT_PROMPT = """根据以下天气数据，给出简洁自然的建议，分三块：
1. 今日天气概况（一两句话，提到最高最低温、接下来几小时的变化趋势）
2. 穿衣建议（根据温度、体感温度和全天变化推荐具体衣物，比如几件套的穿法）
3. 防晒 & 出门注意事项（根据 UV 指数给出防晒建议：UV ≤2 无需特别防晒，3-5 涂 SPF30+，6-7 涂 SPF50+ 戴帽，8+ 尽量避开正午；如有降雨概率 > 30% 提醒带伞）

语气像朋友随口说的，不要像天气预报播报。不要用 emoji。

天气数据：
{weather_data}"""


def get_prompt_defaults() -> dict[str, str]:
    """Return editable prompt sections keyed by stable API names."""
    return {
        "identity": IDENTITY.strip(),
        "user_model": USER_MODEL.strip(),
        "system_mechanics": SYSTEM_MECHANICS.strip(),
        "communication": COMMUNICATION.strip(),
        "protocols": PROTOCOLS.strip(),
        "tools": TOOLS_SECTION.strip(),
        "proactive_gemini": _PROACTIVE_PROMPT_GEMINI,
        "proactive_claude": _PROACTIVE_PROMPT_CLAUDE,
        "reminder": REMINDER_PROMPT,
        "bedtime": BEDTIME_PROMPT,
        "morning": MORNING_PROMPT,
        "weather_report": WEATHER_REPORT_PROMPT,
    }


def apply_prompt_overrides(overrides: dict[str, str] | None) -> dict[str, str]:
    """Return default prompt sections with non-empty overrides applied."""
    sections = get_prompt_defaults()
    if not overrides:
        return sections
    for key, value in overrides.items():
        if key in sections and value and value.strip():
            sections[key] = value.strip()
    return sections


def get_prompt_template(key: str, overrides: dict[str, str] | None = None) -> str:
    """Fetch a single prompt template with optional runtime overrides."""
    sections = apply_prompt_overrides(overrides)
    if key not in sections:
        raise KeyError(key)
    return sections[key]


def get_proactive_prompt(provider: str, overrides: dict[str, str] | None = None) -> str:
    """按 provider 返回对应的轮询模板。gemini 走 <think> 框架版，其他走选项式版。"""
    key = "proactive_gemini" if provider.lower().strip() == "gemini" else "proactive_claude"
    return get_prompt_template(key, overrides)
