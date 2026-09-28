"""消息卡片：决定每条消息属于哪张卡片、什么时候进入对话历史。

规则
- AI 主动发的消息（check-in、提醒、系统通知……）各自开一张 pending 卡片，
  先存在 card_pending_messages，不进 conversation_messages。
- 用户 reply 某张卡片里的消息：卡片转为 active，里面的消息按原顺序写进
  conversation_messages，然后才写用户的回复。这张卡片成为「当前卡片」。
- 用户直接发消息（不 reply）：进入当前卡片；没有当前卡片、或当前卡片已经
  过期隐去时，自动开一张新卡片。
- AI 对用户的回复归入触发消息的卡片；conversation 工具批次以最后一条
  用户消息为准。发送前切卡不改变归属；无法解析来源时仍进入当前卡片。
- 过期：pending 卡片到期直接删除；active 卡片到期只在界面上隐去，内容保留。
  每次有新消息都会把过期时间往后推。

上下文仍然按 conversation_messages 的 id 线性排列，所以 compact、tool batch
的 cursor 都不需要知道卡片的存在。
"""
from __future__ import annotations

import inspect
import uuid
from datetime import datetime, timedelta, timezone

from bot.logger import get_logger

logger = get_logger(__name__)

CARD_TTL = timedelta(days=3)
CURRENT_CARD_STATE_KEY = "current_card_id"

# 这些来源是对用户消息的回应，进入当前卡片；其余来源都算 AI 主动发起，各开一张卡片。
REPLY_SOURCE_TYPES = frozenset({
    "chat", "chat_error", "chat_tool_feedback", "tool_batch",
})

# 用户自己开的卡片（直接发消息、以后的 /conversation new）
USER_SOURCE_TYPE = "user"

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

    def current_card_id(self) -> int | None:
        raw = self.db.get_state(CURRENT_CARD_STATE_KEY)
        if not raw:
            return None
        try:
            card = self.db.get_card(int(raw))
        except ValueError:
            return None
        return int(card["id"]) if card else None

    def _set_current(self, card_id: int) -> None:
        self.db.set_state(CURRENT_CARD_STATE_KEY, str(card_id))

    def _is_faded(self, card: dict) -> bool:
        now, _ = self._times()
        return card["expires_at"] <= now

    def _touch(self, card_id: int) -> None:
        now, expires = self._times()
        self.db.touch_card(card_id, now=now, expires_at=expires)

    def _new_user_card(self, channel_id: str) -> int:
        now, expires = self._times()
        card_id = self.db.create_card(
            channel_id=channel_id, source_type=USER_SOURCE_TYPE,
            source_id=f"user:{uuid.uuid4().hex}", status="active",
            now=now, expires_at=expires,
        )
        logger.info(f"🗂️ 新卡片 #{card_id}（用户开启）")
        return card_id

    # --- /conversation 指令用的入口 ------------------------------------------

    def start_new_card(self, channel_id: str) -> int:
        """开一张新的空卡片并切换过去。"""
        self.purge_expired()
        card_id = self._new_user_card(str(channel_id))
        self._set_current(card_id)
        return card_id

    def switch_to(self, card_id: int) -> dict:
        """切换当前卡片。卡片不存在、不是 active 或已经隐去时抛 ValueError。"""
        self.purge_expired()
        card = self.db.get_card(int(card_id))
        if card is None:
            raise ValueError(f"卡片 #{card_id} 不存在")
        if card["status"] != "active":
            raise ValueError(f"卡片 #{card_id} 还没有被回复过，不能切换过去")
        if self._is_faded(card):
            raise ValueError(f"卡片 #{card_id} 已经过期隐去了")
        self._touch(int(card_id))
        self._set_current(int(card_id))
        return card

    def list_active(self, channel_id: str, limit: int = 25) -> list[dict]:
        """活跃卡片列表，附带卡片名和「是不是当前卡片」，最近的排在前面。"""
        self.purge_expired()
        cards = self.db.list_active_cards(str(channel_id), limit=limit)
        labels = self.db.get_card_labels([c["id"] for c in cards])
        current = self.current_card_id()
        result = []
        for card in cards:
            if self._is_faded(card):
                continue
            item = dict(card)
            item["label"] = labels.get(int(card["id"]), f"card{card['id']}")
            item["is_current"] = int(card["id"]) == current
            result.append(item)
        return result

    def purge_expired(self) -> int:
        now, _ = self._times()
        removed = self.db.purge_expired_pending_cards(now)
        if removed:
            logger.info(f"🗂️ 删除 {removed} 张过期未回复的卡片")
        return removed

    # --- 用户消息 -------------------------------------------------------------

    def card_for_user_message(self, channel_id: str,
                              reply_to_message_id: str | None) -> int:
        """用户消息写入之前调用：确定它属于哪张卡片，必要时先把卡片转正。"""
        self.purge_expired()
        channel_id = str(channel_id)

        if reply_to_message_id:
            card_id = self.db.find_card_id_by_discord_message(str(reply_to_message_id))
            if card_id is not None:
                card = self.db.get_card(card_id)
                if card["status"] == "pending":
                    now, expires = self._times()
                    self.db.activate_card(card_id, now=now, expires_at=expires)
                    logger.info(f"🗂️ 卡片 #{card_id} 被回复，进入对话历史")
                else:
                    self._touch(card_id)
                self._set_current(card_id)
                return card_id
            # 引用的消息找不到卡片：它所在的 pending 卡片已经过期删除，或者是
            # 卡片功能上线前的旧消息。用户是有意 reply 的，所以开一张新卡片，
            # 而不是接进当前卡片。引用内容本身已经写在 current_content 里。
            card_id = self._new_user_card(channel_id)
            self._set_current(card_id)
            return card_id

        current = self.current_card_id()
        if current is not None:
            card = self.db.get_card(current)
            if card["status"] == "active" and not self._is_faded(card):
                self._touch(current)
                return current
        card_id = self._new_user_card(channel_id)
        self._set_current(card_id)
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
            source_id = unique_source_id(source_id)
            card = self.db.get_card_by_source(source_type, source_id)
            if card is None:
                now, expires = self._times()
                card_id = self.db.create_card(
                    channel_id=message["channel_id"], source_type=source_type,
                    source_id=source_id, status="pending",
                    now=now, expires_at=expires,
                )
                logger.info(f"🗂️ 新卡片 #{card_id}（{source_type}），等待回复")
                card = self.db.get_card(card_id)
            if card["status"] == "active":
                # 同一次主动发送的后续分段到达前，用户已经回复了前面的分段
                await _maybe_await(ingest(**message, card_id=card["id"]))
                self._touch(card["id"])
                return
            self.db.add_card_pending_message(card["id"], **message)
            self._touch(card["id"])
            return

        # 在途回答属于触发它的消息；用户切卡不应搬走旧问题的回答。
        # conversation 工具批次以最后一条用户消息为归属。
        # check-in 批次及无来源的旧消息保持现有分类/兼容行为。
        card_id = self.db.find_reply_card_id(source_type, source_id)
        if card_id is None and source_type is not None:
            card_id = self.current_card_id()
        if card_id is None:
            await _maybe_await(ingest(**message))
            return
        self._touch(card_id)
        await _maybe_await(ingest(**message, card_id=card_id))


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value
