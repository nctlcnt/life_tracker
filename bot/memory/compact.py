"""后台 compact worker（LT-135）。

窗口装配（context_window.assemble_window）标记 needs_compact 后，由这里
异步生成摘要并写回 app_state。设计约束：

- **单飞**：每个 channel 同时只允许一个 compact 在跑；期间明文照常增长，
  完成写回后下一次装配自然呈现新窗口（原子切换）。
- **失败冷却**：compact 失败（模型挂/超时）后冷却一段时间再试，
  期间窗口靠硬上限兜底，聊天不受影响。
- **摘要重新生成**：旧摘要 + 折叠明文 → 新摘要（目标 ≤ SUMMARY_TARGET_TOKENS），
  不追加、不膨胀；超长有截断守护。
- **模型 admin 可设**：app_state["compact_preset"] 存 preset 名，
  未设置/失效回落 active preset。

注意：对 AI 引擎的 import 必须留在函数内（ai_engine_base 反向依赖
bot.memory 包，模块级 import 会成环）。
"""
from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import config
from bot.logger import get_logger
from bot.timezone_state import get_timezone
from bot.memory.markdown_repository import estimate_tokens
from bot.memory.context_window import (
    ContextWindow, load_summary_state, save_summary_state,
)

logger = get_logger(__name__)

COMPACT_PRESET_STATE_KEY = "compact_preset"
SUMMARY_TARGET_TOKENS = 1500
COMPACT_COOLDOWN_SECONDS = 300.0
# 后台任务不赶时间；生成耗时贴着聊天路径的 120s 默认值会因方差偶发超时
COMPACT_REQUEST_TIMEOUT_SECONDS = 300.0

SUMMARY_TITLE = "# 对话连续性摘要"

SUMMARY_SECTIONS = (
    "## 当前对话焦点",
    "## 活跃话题轨迹",
    "## 局部结论、修正与否定",
    "## 指代与承接",
    "## 历史话题钩子",
)

_RELATIVE_TIME_PATTERNS = (
    re.compile(r"今天|今日|昨天|昨日|前天|明天|明日|后天|大后天"),
    re.compile(r"(?:上|下|这|本)(?:个)?(?:周|星期|月|个月|季度|学期|年)"),
    re.compile(r"(?:周|星期)[一二三四五六日天末]"),
    re.compile(r"近期|最近|过几天|几天后|稍后|一会儿|月底|年底"),
    re.compile(
        r"\b(?:today|tonight|yesterday|tomorrow|later|recently|"
        r"next\s+(?:week|month|year)|last\s+(?:week|month|year)|"
        r"this\s+(?:week|month|year)|"
        r"monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        re.IGNORECASE,
    ),
)

# channel_id → 进行中的 compact task（单飞）；channel_id → 上次尝试时刻（冷却）
_inflight: dict[str, asyncio.Task] = {}
_last_attempt: dict[str, float] = {}


# ── compact preset（admin 可设） ──────────────────────────────────────────

def get_compact_preset_name(db) -> str | None:
    """当前设置的 compact preset 名；未设置或已失效返回 None。"""
    name = db.get_state(COMPACT_PRESET_STATE_KEY)
    if name and name in config.PRESETS:
        return name
    return None


def get_compact_preset(db):
    """解析实际使用的 preset：设置有效用设置，否则回落 active。"""
    name = get_compact_preset_name(db)
    if name:
        return config.PRESETS[name]
    return config.get_active()


def set_compact_preset(db, name: str | None) -> None:
    """设置/清空 compact preset。name 必须是现存 preset，None/空串 = 清空回落 active。"""
    if not name:
        db.set_state(COMPACT_PRESET_STATE_KEY, "")
        return
    if name not in config.PRESETS:
        raise ValueError(f"unknown preset: {name}")
    db.set_state(COMPACT_PRESET_STATE_KEY, name)


# ── 摘要生成 ─────────────────────────────────────────────────────────────

def _compact_time_context() -> tuple[str, str]:
    timezone_name = get_timezone() or config.TIMEZONE or "UTC"
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        timezone_name = "UTC"
        zone = ZoneInfo("UTC")
    generated_at = datetime.now(zone).strftime("%Y-%m-%d %H:%M:%S")
    return generated_at, timezone_name


def _summary_template(generated_at: str, timezone_name: str) -> str:
    return "\n".join([
        SUMMARY_TITLE,
        f"> 生成基准：{generated_at} ({timezone_name})",
        "",
        SUMMARY_SECTIONS[0],
        "- 折叠区间结束时正在讨论什么，以及此后最自然的承接方向；控制在 1–3 条",
        "",
        SUMMARY_SECTIONS[1],
        "- 仍在影响当前对话的话题，以及话题如何推进或转向",
        "- 记录“聊过什么”和“讨论进行到哪里”，不要在这里维护完整事实档案",
        "",
        SUMMARY_SECTIONS[2],
        "- 只记录继续当前对话所必需的局部结论、用户修正、否定过的方向或暂时采用的假设",
        "- 不要收录与当前对话无关的长期决定或全局未完成事项",
        "",
        SUMMARY_SECTIONS[3],
        "- 当前仍可能出现的模糊指代，以及它们具体指向什么",
        "- 例如：“这个数据库”指长期记忆库；“刚才的方案”指按需检索方案",
        "",
        SUMMARY_SECTIONS[4],
        "- YYYY-MM-DD｜曾讨论的话题；需要细节时的建议检索词；可选来源 message_id",
        "- 这里只证明该话题曾被讨论，不证明其中的具体事实",
    ])

def build_compact_prompt(
    old_summary: str,
    messages: list[dict],
    *,
    generated_at: str | None = None,
    timezone_name: str | None = None,
) -> str:
    if generated_at is None or timezone_name is None:
        generated_at, timezone_name = _compact_time_context()

    transcript = "\n".join(
        (
            f"[message_id={m['id']} "
            f"created_at={m.get('created_at') or 'unknown'}] "
            f"{'用户' if m['role'] == 'user' else '助理'}: {m['content']}"
        )
        for m in messages
    )

    parts = [
        (
            "你在维护一份“对话连续性 compact”。它是一种模糊的情节记忆，"
            "用于在旧消息离开上下文后，帮助私人助理知道最近聊过什么、"
            "话题如何推进、当前指代什么，以及何时应搜索长期记忆。\n\n"
            "它不是长期记忆库，不是用户画像，不是项目数据库，也不是任务清单。"
        ),
        (
            f"【生成基准】\n"
            f"当前时间：{generated_at}\n"
            f"时区：{timezone_name}"
        ),
    ]

    if old_summary:
        parts.append(
            "【已有 compact（更早对话形成的模糊上下文）】\n"
            f"{old_summary}"
        )

    parts.append(
        "【需要并入 compact 的新对话】\n"
        f"{transcript}"
    )

    parts.append(
        "【核心目标】\n"
        "假设旧对话原文即将从模型上下文中移除。请生成一份新的 compact，"
        "使模型仍能自然理解紧接着出现的后续消息。\n\n"
        "注意：compact 只覆盖被折叠的较早消息；在最终窗口里，它之后还跟着"
        "一段更近的明文对话。不要断言「至今」「目前仍」「停滞至今」等延续到"
        "当前时刻的状态——「当前对话焦点」描述的是折叠区间结束时对话进行到的"
        "位置，此后的走向以 compact 后面的明文为准。\n\n"
        "compact 应能回答：\n"
        "1. 最近在聊什么？\n"
        "2. 讨论是怎样走到当前位置的？\n"
        "3. 用户刚刚确认、修正或否定了什么？\n"
        "4. “这个”“那个”“之前的方案”等指代什么？\n"
        "5. 哪些旧话题只需要留下一个可搜索的钩子？\n\n"
    )

    parts.append(
        "【生成要求】\n"
        "1. 输出一份完整的新 compact。「完整」指足以维持对话连续性，"
        "而非覆盖历史中的全部事实。\n\n"
        "2. 主动删除不再影响当前对话的内容。不得因为某条内容存在于旧 compact 中，"
        "就默认继续保留。\n\n"
        "3. compact 记录的是对话情节，而不是长期事实。\n\n"
        "4. 一个历史话题钩子只证明“这个话题曾被讨论过”。它不能被当作其中具体事实、"
        "偏好、决定或状态的证据。需要具体内容时，未来的助理必须先搜索记忆。\n\n"
        "5. 稳定事实、长期偏好、全局任务和 open questions 原则上不要写入 compact。"
        "只有当它们正被最近对话直接使用，且删除后会导致下一轮无法理解时，"
        "才可作为临时局部背景简短出现。\n\n"
        "6. “局部结论、修正与否定”只保存当前话题需要的内容。\n\n"
        "7. 时间衰减规则：\n"
        "   - 生成基准前 7 天内：仍相关的话题可以保留 1–2 行对话细节。\n"
        "   - 7–30 天：压缩成单行话题钩子，不保留具体事实列表。\n"
        "   - 超过 30 天：除非仍直接影响当前活跃话题，否则删除；"
        "能够通过长期记忆或原始消息搜索恢复即可。\n\n"
        "8. 当前仍持续进行的同一话题，不因超过 30 天而自动删除；"
        "但只保留当前阶段和检索入口，不复述完整历史。\n\n"
        "9. 每条消息的 created_at 是解释原文中相对时间的唯一时间锚点。"
        "日期计算使用上方时区。\n\n"
        "10. 输出中不要使用今天、昨天、明天、上周、下周、周五、月底、前几天等"
        "无明确锚点的时间表达。能确定时转换为 YYYY-MM-DD 或 "
        "YYYY-MM-DD HH:MM；无法确定时写“日期不确定”，不要猜测。\n\n"
        "11. 旧 compact 不是事实权威，也不是必须继承的清单。新对话中的明确修正"
        "优先于旧 compact。\n\n"
        "12. 不要编造长期记忆 ID、数据库记录或不存在的来源。可以使用输入中真实存在的"
        "message_id 作为检索线索。\n\n"
        "13. 同一话题不要分别以“状态”“偏好”“经历”等形式重复出现。优先保留一条"
        "最能帮助继续对话的描述。\n\n"
        f"14. 严格使用下方 Markdown 模板和标题顺序。每节没有内容时写“- 无”。"
        f"输出控制在 {SUMMARY_TARGET_TOKENS} tokens 以内，不要添加代码围栏或模板外说明。"
    )

    parts.append(
        "【必须严格使用的输出模板】\n"
        + _summary_template(generated_at, timezone_name)
    )

    return "\n\n".join(parts)


def _validate_compact_summary(summary: str, *, generated_at: str,
                              timezone_name: str) -> str:
    summary = summary.strip()
    lines = [line.rstrip() for line in summary.splitlines()]
    if not lines or lines[0].strip() != SUMMARY_TITLE:
        raise ValueError(f"summary must start with {SUMMARY_TITLE!r}")
    expected_baseline = f"> 生成基准：{generated_at} ({timezone_name})"
    if len(lines) < 2 or lines[1].strip() != expected_baseline:
        raise ValueError("summary generation baseline is missing or incorrect")

    positions = []
    for heading in SUMMARY_SECTIONS:
        matches = [index for index, line in enumerate(lines) if line.strip() == heading]
        if len(matches) != 1:
            raise ValueError(f"summary must contain heading exactly once: {heading}")
        positions.append(matches[0])
    if positions != sorted(positions):
        raise ValueError("summary headings are out of order")

    relative_terms: list[str] = []
    for line in lines[2:]:
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        for pattern in _RELATIVE_TIME_PATTERNS:
            for match in pattern.finditer(text):
                term = match.group(0)
                if term not in relative_terms:
                    relative_terms.append(term)
    if relative_terms:
        raise ValueError(
            "summary contains unanchored relative time: " +
            ", ".join(relative_terms))
    return "\n".join(lines).strip()


def _build_summary_repair_prompt(
    summary: str, *, validation_error: str,
    generated_at: str, timezone_name: str,
) -> str:
    return "\n\n".join([
        "下面是一份对话上下文摘要候选。它的事实内容已经生成，但没有通过确定性格式/时间校验。"
        "请只做最小必要修正，不添加事实、不改变事项状态、不重新总结。",
        f"【生成基准】\n当前时间：{generated_at}\n时区：{timezone_name}",
        f"【校验错误】\n{validation_error}",
        "修正规则：\n"
        "1. 保留固定标题、生成基准、五个 section 及其顺序。\n"
        "2. 删除或改写校验错误列出的无锚点相对时间。候选中已有可靠绝对日期时使用该日期；"
        "无法从候选本身确定时，删除相对时间修饰或写“日期不确定”，绝不猜日期。\n"
        "3. 输出中不得出现今天、昨天、明天、后天、上周、下周、周几、近期、最近、月底等"
        "相对时间。\n"
        "4. 只输出修正后的完整 Markdown 摘要，不要代码围栏或解释。",
        "【必须严格使用的输出模板】\n" +
        _summary_template(generated_at, timezone_name),
        "【待修正摘要】\n" + summary,
    ])


async def _complete_validated_summary(
    prompt: str, preset, *, generated_at: str, timezone_name: str,
) -> str:
    from bot.ai_engine_openai_compat import simple_completion

    summary = (await simple_completion(
        prompt, preset,
        request_timeout=COMPACT_REQUEST_TIMEOUT_SECONDS) or "").strip()
    if not summary:
        raise ValueError("compact returned an empty summary")

    try:
        validated = _validate_compact_summary(
            summary,
            generated_at=generated_at,
            timezone_name=timezone_name,
        )
    except ValueError as first_error:
        logger.warning(f"⚠️ compact 首次输出未通过校验，定向修正一次: {first_error}")
        repair_prompt = _build_summary_repair_prompt(
            summary,
            validation_error=str(first_error),
            generated_at=generated_at,
            timezone_name=timezone_name,
        )
        summary = (await simple_completion(
            repair_prompt, preset,
            request_timeout=COMPACT_REQUEST_TIMEOUT_SECONDS) or "").strip()
        if not summary:
            raise ValueError("compact repair returned an empty summary")
        validated = _validate_compact_summary(
            summary,
            generated_at=generated_at,
            timezone_name=timezone_name,
        )

    shortened = _truncate_summary(validated)
    return _validate_compact_summary(
        shortened,
        generated_at=generated_at,
        timezone_name=timezone_name,
    )


def _truncate_summary(summary: str) -> str:
    """长度守护：优先缩短最长 section，同时保留固定模板骨架。"""
    limit = SUMMARY_TARGET_TOKENS * 2
    if estimate_tokens(summary) <= limit:
        return summary

    lines = summary.splitlines()
    heading_indexes = [
        index for index, line in enumerate(lines)
        if line.strip() in SUMMARY_SECTIONS
    ]
    if len(heading_indexes) != len(SUMMARY_SECTIONS):
        return summary

    prefix = lines[:heading_indexes[0]]
    sections: list[tuple[str, str]] = []
    for position, start in enumerate(heading_indexes):
        end = heading_indexes[position + 1] if position + 1 < len(heading_indexes) else len(lines)
        body = "\n".join(lines[start + 1:end]).strip() or "- 无"
        sections.append((lines[start].strip(), body))

    def render() -> str:
        chunks = ["\n".join(prefix).strip()]
        chunks.extend(f"{heading}\n{body}" for heading, body in sections)
        return "\n\n".join(chunks).strip()

    result = render()
    while estimate_tokens(result) > limit:
        index = max(range(len(sections)), key=lambda item: len(sections[item][1]))
        heading, body = sections[index]
        if len(body) <= 16:
            break
        sections[index] = (heading, body[:max(12, int(len(body) * 0.8))].rstrip() + "…")
        result = render()
    logger.warning(f"⚠️ compact 摘要超长（按 section 截断到 ~{limit}tk）")
    return result


async def run_compact(db, channel_id: str, fold_upto_id: int) -> bool:
    """执行一次 compact：折叠 (当前upto, fold_upto_id] 进新摘要。

    返回是否成功写回。任何失败只记日志不上抛——聊天主流程不能被 compact 拖垮。
    """
    channel_id = str(channel_id)
    _last_attempt[channel_id] = time.monotonic()

    state = load_summary_state(db, channel_id)
    old_summary = state["summary"] if state else ""
    upto = int(state.get("upto_message_id", 0)) if state else 0
    if fold_upto_id <= upto:
        return False  # 已被更早完成的 compact 覆盖，无事可做

    folded = db.get_ai_messages_after(
        channel_id, upto, upto_id=fold_upto_id,
    )
    if not folded:
        return False

    generated_at, timezone_name = _compact_time_context()
    prompt = build_compact_prompt(
        old_summary, folded,
        generated_at=generated_at, timezone_name=timezone_name,
    )
    preset = get_compact_preset(db)
    logger.info(
        f"🧾 compact 开始: {len(folded)} 条 → 摘要 "
        f"(preset={preset.name}, upto {upto}→{fold_upto_id})")
    try:
        summary = await _complete_validated_summary(
            prompt, preset,
            generated_at=generated_at,
            timezone_name=timezone_name,
        )
    except Exception as e:
        logger.warning(f"⚠️ compact 失败（{COMPACT_COOLDOWN_SECONDS:.0f}s 后可重试）: "
                       f"{type(e).__name__}: {e}")
        return False

    save_summary_state(
        db, channel_id, summary=summary, upto_message_id=fold_upto_id,
        model=preset.model,
        updated_at=datetime.now().isoformat(timespec="seconds"),
    )
    # 成功清除冷却：暴涨场景下允许下一轮立即接上；冷却只惩罚失败
    _last_attempt.pop(channel_id, None)
    from bot.memory.history_embedding import schedule_compacted_embeddings
    schedule_compacted_embeddings(db, channel_id, fold_upto_id)
    logger.info(f"🧾 compact 完成: {estimate_tokens(summary)}tk，"
                f"窗口游标推进到 id={fold_upto_id}")
    return True


async def rebuild_compact_summary(db, channel_id: str,
                                  fold_upto_id: int) -> bool:
    """从原始消息完整重建摘要，成功前保持现有窗口状态不变。

    这是修复错误/旧版 cursor 的运维入口，不用于日常增量 compact。生成期间若
    另一个 compact 已更新 state，则拒绝覆盖，留给操作者基于新状态重试。
    """
    channel_id = str(channel_id)
    initial_state = load_summary_state(db, channel_id)
    initial_upto = int(initial_state.get("upto_message_id", 0)) if initial_state else 0
    if fold_upto_id < initial_upto:
        logger.warning(
            f"⚠️ compact 重建目标不能后退: current={initial_upto}, target={fold_upto_id}")
        return False

    folded = db.get_ai_messages_after(
        channel_id, 0, upto_id=fold_upto_id,
    )
    if not folded or folded[-1]["id"] != fold_upto_id:
        logger.warning(
            f"⚠️ compact 重建目标不是该 channel 的有效消息: id={fold_upto_id}")
        return False

    preset = get_compact_preset(db)
    generated_at, timezone_name = _compact_time_context()
    prompt = build_compact_prompt(
        "", folded,
        generated_at=generated_at, timezone_name=timezone_name,
    )
    logger.info(
        f"🧾 compact 全量重建开始: {len(folded)} 条 → 摘要 "
        f"(preset={preset.name}, target={fold_upto_id})")
    try:
        summary = await _complete_validated_summary(
            prompt, preset,
            generated_at=generated_at,
            timezone_name=timezone_name,
        )
    except Exception as e:
        logger.warning(
            f"⚠️ compact 全量重建失败，保留原状态: {type(e).__name__}: {e}")
        return False

    if load_summary_state(db, channel_id) != initial_state:
        logger.warning("⚠️ compact 重建期间 state 已变化，拒绝覆盖较新的摘要")
        return False

    save_summary_state(
        db, channel_id, summary=summary, upto_message_id=fold_upto_id,
        model=preset.model,
        updated_at=datetime.now().isoformat(timespec="seconds"),
    )
    from bot.memory.history_embedding import schedule_compacted_embeddings
    schedule_compacted_embeddings(db, channel_id, fold_upto_id)
    logger.info(
        f"🧾 compact 全量重建完成: {estimate_tokens(summary)}tk，"
        f"连续覆盖 {len(folded)} 条，cursor={fold_upto_id}")
    return True


def schedule_compact(db, channel_id: str, window: ContextWindow) -> bool:
    """按窗口装配结果决定是否后台起一个 compact。单飞 + 冷却，永不抛异常。

    返回 True = 本次真的新起了任务。同步上下文（无事件循环）直接跳过，
    下次异步装配时自然补上。
    """
    channel_id = str(channel_id)
    if not window.needs_compact or not window.fold_upto_id:
        return False
    task = _inflight.get(channel_id)
    if task is not None and not task.done():
        return False
    last = _last_attempt.get(channel_id)
    if last is not None and time.monotonic() - last < COMPACT_COOLDOWN_SECONDS:
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    new_task = loop.create_task(run_compact(db, channel_id, window.fold_upto_id))
    _inflight[channel_id] = new_task
    new_task.add_done_callback(
        lambda t: _inflight.pop(channel_id, None) if _inflight.get(channel_id) is t else None)
    return True
