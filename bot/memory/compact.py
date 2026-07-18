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

SUMMARY_TITLE = "# 对话上下文摘要"
SUMMARY_SECTIONS = (
    "## 当前仍有效的状态",
    "## 未完成事项",
    "## 稳定事实与偏好",
    "## 最近经历",
    "## 已完成或已失效",
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
        "- 只写截至生成基准仍然成立的状态；没有则写“无”",
        "",
        SUMMARY_SECTIONS[1],
        "- 事项；绝对日期或“日期不确定”；当前进度",
        "",
        SUMMARY_SECTIONS[2],
        "- 跨时间仍稳定、未来对话可复用的事实或偏好",
        "",
        SUMMARY_SECTIONS[3],
        "- YYYY-MM-DD：已经发生且仍有近期上下文价值的经历",
        "",
        SUMMARY_SECTIONS[4],
        "- YYYY-MM-DD：已结束、取消或过期，但仍有上下文价值的事项",
    ])


def build_compact_prompt(old_summary: str, messages: list[dict], *,
                         generated_at: str | None = None,
                         timezone_name: str | None = None) -> str:
    if generated_at is None or timezone_name is None:
        generated_at, timezone_name = _compact_time_context()
    transcript = "\n".join(
        f"[message_id={m['id']} created_at={m.get('created_at') or 'unknown'}] "
        f"{'用户' if m['role'] == 'user' else '助理'}: {m['content']}"
        for m in messages
    )
    parts = [
        "你在为一个私人助理维护对话上下文摘要。请把输入重新整理成一份完整、"
        "有时间锚点、可直接用于未来对话的摘要。",
        f"【生成基准】\n当前时间：{generated_at}\n时区：{timezone_name}",
    ]
    if old_summary:
        parts.append(f"【已有摘要（更早的对话）】\n{old_summary}")
    parts.append(f"【需要并入摘要的新对话】\n{transcript}")
    parts.append(
        "要求：\n"
        "1. 输出一份完整的新摘要，覆盖已有摘要与新对话的全部要点（不是增量补丁）。\n"
        "2. 每条消息的 created_at 是解释原文中相对时间的唯一锚点；日期计算使用上面的时区。\n"
        "3. 输出中禁止使用无锚点相对时间，包括今天、昨天、明天、后天、上周、下周、"
        "周五、近期、月底等。能确定时改成 YYYY-MM-DD 或 YYYY-MM-DD HH:MM；"
        "不能可靠确定时写“日期不确定”，绝不猜测。\n"
        "4. 已经过期、完成或取消的安排不得放在“当前仍有效”或“未完成事项”；"
        "只有仍有上下文价值时才移入“已完成或已失效”。\n"
        "5. 旧摘要也不是事实权威：按生成基准重新判断时效，不能照抄旧摘要中的相对时间。\n"
        "6. 不要编造原文没有的信息；不确定就不写。越旧的经历越粗略，优先保留未完成事项、"
        "仍有效状态、稳定事实与偏好。\n"
        f"7. 严格使用下方 Markdown 模板及标题顺序；每节没有内容写“- 无”。"
        f"控制在 {SUMMARY_TARGET_TOKENS} tokens 以内，不要代码围栏或模板外说明。"
    )
    parts.append(
        "【必须严格使用的输出模板】\n" +
        _summary_template(generated_at, timezone_name)
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

    summary = (await simple_completion(prompt, preset) or "").strip()
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
        summary = (await simple_completion(repair_prompt, preset) or "").strip()
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
