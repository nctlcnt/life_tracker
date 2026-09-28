"""卡片一句话总结。

/conversation list 和 switch 要在 Discord autocomplete 里显示每张卡片在聊什么，
而 autocomplete 必须在 3 秒内返回，来不及现场调模型。所以总结在卡片有新对话之后
在后台生成，写进 conversation_cards.summary，打开列表时只读这一列。

和 bot/memory/compact.py 的分工一样：规则和读写在 bot/cards.py，这里只负责
「调模型生成一句话」这件事，失败永远不影响聊天本身。
"""
from __future__ import annotations

import asyncio

from bot.logger import get_logger

logger = get_logger(__name__)

SUMMARY_MAX_CHARS = 60
SUMMARY_REQUEST_TIMEOUT_SECONDS = 20
# 生成总结时最多读卡片里的多少条消息（一句话总结不需要读全）
SUMMARY_MESSAGE_LIMIT = 12

# card_id → 进行中的生成任务，避免同一张卡片连发几条消息时重复调用模型
_inflight: dict[int, asyncio.Task] = {}
# 生成过程中又来了新消息的卡片：生成结束后要再跑一次，否则总结停留在这一批
# 消息到齐之前的那个快照上。compact 有游标可以判断自己是否落后，总结没有，
# 所以只能靠这里补一次。
_rerun: set[int] = set()


def _build_prompt(messages: list[dict]) -> str:
    lines = []
    for message in messages:
        speaker = "我" if message["role"] == "user" else "助理"
        lines.append(f"{speaker}：{message['content']}")
    transcript = "\n".join(lines)
    return (
        "下面是一段对话的片段。请用一句话概括这段对话在聊什么，"
        f"不超过 {SUMMARY_MAX_CHARS} 个字。\n"
        "只输出这一句话本身，不要加引号、标题、解释或标点以外的任何修饰。\n"
        "如果内容太少看不出主题，就直接概括已经出现的那句话。\n\n"
        f"{transcript}"
    )


async def refresh_card_summary(db, card_id: int) -> bool:
    """给一张卡片重新生成总结。返回是否真的写入了新的总结。"""
    from bot.ai_engine_openai_compat import simple_completion
    from bot.memory.compact import get_compact_preset

    card_id = int(card_id)
    messages = db.get_card_tail_messages(card_id, limit=SUMMARY_MESSAGE_LIMIT)
    if not messages:
        return False
    try:
        preset = get_compact_preset(db)
        text = await simple_completion(
            _build_prompt(messages), preset,
            trigger="card_summary",
            request_timeout=SUMMARY_REQUEST_TIMEOUT_SECONDS,
        )
    except Exception as e:
        logger.warning(f"⚠️ 卡片 #{card_id} 总结生成失败: {type(e).__name__}: {e}")
        return False
    # 只取第一行：模型偶尔会在总结后面再补一段解释。纯空白时 splitlines()
    # 返回空列表，所以不能直接取下标。
    lines = (text or "").strip().splitlines()
    summary = lines[0].strip() if lines else ""
    if not summary:
        logger.warning(f"⚠️ 卡片 #{card_id} 的总结是空的，保留原值")
        return False
    if len(summary) > SUMMARY_MAX_CHARS:
        summary = summary[:SUMMARY_MAX_CHARS]
    db.set_card_summary(card_id, summary)
    logger.info(f"🗂️ 卡片 #{card_id} 总结已更新：{summary}")
    return True


def schedule_summary(db, card_id: int | None) -> bool:
    """卡片有新对话之后在后台补总结。同一张卡片不并发，不阻塞，永不抛异常。

    返回 True = 这次真的新起了一个任务。正在生成的时候又调进来，说明有新消息
    是这一次读不到的，于是记下来、等它结束再补跑一轮（一次，不累积）。没有
    事件循环（同步调用方、测试）时直接跳过，下一次有新消息时自然补上。
    """
    if card_id is None:
        return False
    try:
        card_id = int(card_id)
        loop = asyncio.get_running_loop()
    except (RuntimeError, TypeError, ValueError):
        return False

    task = _inflight.get(card_id)
    if task is not None and not task.done():
        # 正在生成的那一次读不到这条新消息，记下来，等它结束再补一次
        _rerun.add(card_id)
        return False

    def _done(finished: asyncio.Task) -> None:
        if _inflight.get(card_id) is finished:
            _inflight.pop(card_id, None)
        if card_id in _rerun:
            _rerun.discard(card_id)
            schedule_summary(db, card_id)

    new_task = loop.create_task(refresh_card_summary(db, card_id))
    _inflight[card_id] = new_task
    new_task.add_done_callback(_done)
    return True
