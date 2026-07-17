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
import time
from datetime import datetime

import config
from bot.logger import get_logger
from bot.memory.markdown_repository import estimate_tokens
from bot.memory.context_window import (
    ContextWindow, load_summary_state, save_summary_state,
)

logger = get_logger(__name__)

COMPACT_PRESET_STATE_KEY = "compact_preset"
SUMMARY_TARGET_TOKENS = 1500
COMPACT_COOLDOWN_SECONDS = 300.0

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

def build_compact_prompt(old_summary: str, messages: list[dict]) -> str:
    transcript = "\n".join(
        f"{'用户' if m['role'] == 'user' else '助理'}: {m['content']}"
        for m in messages
    )
    parts = ["你在为一个私人助理维护对话记忆。请把下面的内容压缩成一份连贯的对话摘要。"]
    if old_summary:
        parts.append(f"【已有摘要（更早的对话）】\n{old_summary}")
    parts.append(f"【需要并入摘要的新对话】\n{transcript}")
    parts.append(
        "要求：\n"
        "1. 输出一份完整的新摘要，覆盖已有摘要与新对话的全部要点（不是增量补丁）。\n"
        "2. 优先保留：正在进行的事情与约定、用户的状态与安排（deadline/计划/情绪）、"
        "值得记住的事实、尚未解决的话题。\n"
        "3. 按时间先后组织，旧内容可以更粗略，越近的内容保留越多细节。\n"
        "4. 不要编造原文没有的信息；不确定就不写。\n"
        f"5. 控制在 {SUMMARY_TARGET_TOKENS} tokens 以内的纯文本，不要任何前后缀说明。"
    )
    return "\n\n".join(parts)


def _truncate_summary(summary: str) -> str:
    """长度守护：模型不听话时按字符硬截，保底不让摘要反噬窗口预算。"""
    limit = SUMMARY_TARGET_TOKENS * 2
    if estimate_tokens(summary) <= limit:
        return summary
    lo, hi = 0, len(summary)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if estimate_tokens(summary[:mid]) <= limit:
            lo = mid
        else:
            hi = mid - 1
    logger.warning(f"⚠️ compact 摘要超长（截断到 ~{limit}tk）")
    return summary[:lo] + "…"


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

    prompt = build_compact_prompt(old_summary, folded)
    preset = get_compact_preset(db)
    logger.info(
        f"🧾 compact 开始: {len(folded)} 条 → 摘要 "
        f"(preset={preset.name}, upto {upto}→{fold_upto_id})")
    try:
        # 函数内 import：避免 bot.memory ←→ ai_engine_base 模块级循环
        from bot.ai_engine_openai_compat import simple_completion
        summary = await simple_completion(prompt, preset)
    except Exception as e:
        logger.warning(f"⚠️ compact 失败（{COMPACT_COOLDOWN_SECONDS:.0f}s 后可重试）: "
                       f"{type(e).__name__}: {e}")
        return False

    summary = (summary or "").strip()
    if not summary:
        logger.warning("⚠️ compact 返回空摘要，放弃本轮")
        return False
    summary = _truncate_summary(summary)

    save_summary_state(
        db, channel_id, summary=summary, upto_message_id=fold_upto_id,
        model=preset.model,
        updated_at=datetime.now().isoformat(timespec="seconds"),
    )
    # 成功清除冷却：暴涨场景下允许下一轮立即接上；冷却只惩罚失败
    _last_attempt.pop(channel_id, None)
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
    prompt = build_compact_prompt("", folded)
    logger.info(
        f"🧾 compact 全量重建开始: {len(folded)} 条 → 摘要 "
        f"(preset={preset.name}, target={fold_upto_id})")
    try:
        from bot.ai_engine_openai_compat import simple_completion
        summary = await simple_completion(prompt, preset)
    except Exception as e:
        logger.warning(
            f"⚠️ compact 全量重建失败，保留原状态: {type(e).__name__}: {e}")
        return False

    summary = (summary or "").strip()
    if not summary:
        logger.warning("⚠️ compact 全量重建返回空摘要，保留原状态")
        return False
    summary = _truncate_summary(summary)

    if load_summary_state(db, channel_id) != initial_state:
        logger.warning("⚠️ compact 重建期间 state 已变化，拒绝覆盖较新的摘要")
        return False

    save_summary_state(
        db, channel_id, summary=summary, upto_message_id=fold_upto_id,
        model=preset.model,
        updated_at=datetime.now().isoformat(timespec="seconds"),
    )
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
