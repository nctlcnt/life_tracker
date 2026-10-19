"""消息卡片：决定每条消息属于哪张卡片、什么时候进入对话历史。

规则
- Discord 主线聊天是一整条线，不属于任何卡片。
- AI 主动发的消息（check-in、提醒、系统通知……）各自开一张 pending 卡片，
  先存在 card_pending_messages，不进 conversation_messages。
- 用户在卡片的 Discord thread 里说话：卡片转为 active，里面的消息按原顺序
  写进 conversation_messages，然后才写用户的这条消息。
- AI 对用户的回复归入触发消息的卡片；conversation 工具批次以最后一条
  用户消息为准，check-in 工具批次归入这次 check-in 的卡片。来源不属于任何
  卡片时，回复留在主线。归入的卡片还是 pending 时，回复和开场白一样先存在卡片里。
- 过期：没有 thread 的 pending 卡片到期直接删除；开了 thread 的 pending 卡片
  保留，因为 thread 还在；active 卡片到期只在界面上隐去，内容保留。
  每次有新消息都会把过期时间往后推。

上下文仍然按 conversation_messages 的 id 线性排列，thread 里的消息也写主频道
的 channel_id，所以 compact、tool batch 的 cursor 都不需要知道卡片和 thread 的存在。
"""
from __future__ import annotations

import inspect
import uuid
from datetime import datetime, timedelta, timezone

from bot.logger import get_logger

logger = get_logger(__name__)

CARD_TTL = timedelta(days=3)

# 这些来源是对用户消息的回应，跟着触发消息走；其余来源都算 AI 主动发起，各开一张卡片。
REPLY_SOURCE_TYPES = frozenset({
    "chat", "chat_error", "chat_tool_feedback", "tool_batch",
})

# send_proactive_message 的默认 source_id。多次发送共用它会被并进同一张卡片，
# 所以遇到它时换成每次发送唯一的 id。
_UNSPECIFIED_SOURCE_IDS = frozenset({"", "unknown"})


def is_proactive(source_type: str | None) -> bool:
    return source_type is not None and source_type not in REPLY_SOURCE_TYPES


def unique_source_id(source_id: str | None) -> str:
    """缺省的 source_id 换成唯一值；同一次发送的多个分段要共用返回值。"""
    if source_id is None or str(source_id) in _UNSPECIFIED_SOURCE_IDS:
        return f"auto:{uuid.uuid4().hex}"
    return str(source_id)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


class CardService:
    def __init__(self, db, *, now=None):
        self.db = db
        # 测试里可以注入固定的「现在」
        self._now = now or (lambda: datetime.now(timezone.utc))

    # --- 状态 ---------------------------------------------------------------

    def _times(self) -> tuple[str, str]:
        now = self._now()
        return _iso(now), _iso(now + CARD_TTL)

    def _is_faded(self, card: dict) -> bool:
        now, _ = self._times()
        return card["expires_at"] <= now

    def _touch(self, card_id: int) -> None:
        now, expires = self._times()
        self.db.touch_card(card_id, now=now, expires_at=expires)

    def ensure_card(self, channel_id: str, source_type: str, source_id: str) -> dict:
        """主动消息的卡片：不存在就新建一张 pending 卡片。"""
        card = self.db.get_card_by_source(source_type, source_id)
        if card is None:
            now, expires = self._times()
            card_id = self.db.create_card(
                channel_id=str(channel_id), source_type=source_type,
                source_id=source_id, status="pending",
                now=now, expires_at=expires,
            )
            logger.info(f"🗂️ 新卡片 #{card_id}（{source_type}），等待回复")
            card = self.db.get_card(card_id)
        return card

    def destination_card(self, channel_id: str, *, source_type: str | None,
                         source_id: str | None,
                         target_discord_message_id: str | None = None) -> dict | None:
        """一次发送应该显示在哪张卡片里；None 表示发在主线。

        主动消息 → 它自己的卡片（必要时新建），source_id 要先经过 unique_source_id。
        表情回应 → 被回应的那条消息所在的卡片。
        回复类消息 → 触发消息所在的卡片。
        """
        if is_proactive(source_type):
            self.purge_expired()
            return self.ensure_card(channel_id, source_type, str(source_id))
        if target_discord_message_id is not None:
            card_id = self.db.find_card_id_by_discord_message(
                str(target_discord_message_id))
        else:
            card_id = self.db.find_reply_card_id(source_type, source_id)
        return self.db.get_card(card_id) if card_id is not None else None

    # --- 列表 ----------------------------------------------------------------

    def list_active(self, channel_id: str | None, limit: int = 25,
                    include_faded: bool = False) -> list[dict]:
        """活跃卡片列表，附带卡片名，最近的排在前面。

        include_faded=True 时连已经过期隐去的卡片一起返回，并打上 faded 标记。
        App 需要它们才能做渐隐效果。
        """
        self.purge_expired()
        cards = self.db.list_active_cards(
            str(channel_id) if channel_id is not None else None, limit=limit)
        labels = self.db.get_card_labels([c["id"] for c in cards])
        result = []
        for card in cards:
            faded = self._is_faded(card)
            if faded and not include_faded:
                continue
            item = dict(card)
            item["label"] = labels.get(int(card["id"]), f"card{card['id']}")
            item["faded"] = faded
            result.append(item)
        return result

    def purge_expired(self) -> int:
        now, _ = self._times()
        removed = self.db.purge_expired_pending_cards(now)
        if removed:
            logger.info(f"🗂️ 删除 {removed} 张过期未回复的卡片")
        return removed

    # --- 用户消息 -------------------------------------------------------------

    def enter_card(self, card_id: int) -> int | None:
        """用户在某张卡片里说话，写入这条消息之前调用。

        pending 卡片转为 active，里面 AI 的消息先写进对话历史；active 卡片只把
        过期时间往后推。卡片不存在（pending 卡片已经过期删除）时返回 None，
        调用方把消息当作主线消息写入。
        """
        self.purge_expired()
        card = self.db.get_card(int(card_id))
        if card is None:
            return None
        card_id = int(card["id"])
        if card["status"] == "pending":
            now, expires = self._times()
            self.db.activate_card(card_id, now=now, expires_at=expires)
            logger.info(f"🗂️ 卡片 #{card_id} 被回复，进入对话历史")
        else:
            self._touch(card_id)
        return card_id

    # --- AI 发出的消息 ---------------------------------------------------------

    async def record_outbound(self, message: dict, *, source_type: str | None,
                              source_id: str | None, ingest) -> None:
        """AI 发出的一段消息该去哪里。

        ingest(**message) 写进 conversation_messages（MemoryService.ingest_message）。
        source_type 为 None 时（旧调用方、测试）保持原行为：直接进对话历史。
        """
        if is_proactive(source_type):
            self.purge_expired()
            card = self.ensure_card(
                message["channel_id"], source_type, unique_source_id(source_id))
            if card["status"] == "active":
                # 同一次主动发送的后续分段到达前，用户已经回复了前面的分段
                await _maybe_await(ingest(**message, card_id=card["id"]))
                self._touch(card["id"])
                return
            self.db.add_card_pending_message(card["id"], **message)
            self._touch(card["id"])
            return

        # 回答属于触发它的消息所在的卡片；conversation 工具批次以最后一条
        # 用户消息为归属。触发消息在主线上时，回答也留在主线。
        card_id = self.db.find_reply_card_id(source_type, source_id)
        if card_id is None:
            await _maybe_await(ingest(**message))
            return
        self._touch(card_id)
        card = self.db.get_card(card_id)
        if card is not None and card["status"] == "pending":
            # 例如 check-in 工具批次的结果：用户还没在卡片里说过话
            self.db.add_card_pending_message(card_id, **message)
            return
        await _maybe_await(ingest(**message, card_id=card_id))


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value
