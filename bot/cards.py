"""消息卡片：决定每条消息属于哪张卡片、什么时候进入对话历史。

规则
- AI 主动发的消息（check-in、提醒、系统通知……）各自开一张 pending 卡片，
  先存在 card_pending_messages，不进 conversation_messages。
- check-in 发出时，bot 会从这条消息上开一个 Discord thread（见 THREAD_SOURCE_TYPES），
  这张卡片的对话都在 thread 里进行。thread 里第一次有人说话，卡片才转为 active。
- 用户 reply 某张卡片里的消息：卡片转为 active，里面的消息按原顺序写进
  conversation_messages，然后才写用户的回复。卡片有 thread 时，bot 在 thread 里回应。
- 用户在主频道直接发消息（不 reply）：不归属任何卡片，就是普通的陪聊。
- AI 对用户的回复归入触发消息的卡片；conversation 工具批次以最后一条
  用户消息为准；找不到来源卡片时直接进对话历史。
- 过期：pending 卡片到期直接删除；active 卡片到期只在界面上隐去，内容保留。
  每次有新消息都会把过期时间往后推。

thread 里的消息在 DB 里仍然记在主频道（channel_id = 主频道 id）下，只靠 card_id
区分。这样 compact、工具批次、长期记忆和语义检索这些按频道工作的流程不用改，
thread 里聊到的事情照样会被它们看到；主频道的上下文也能看到 thread 里的对话，
前面带着卡片名。只有 thread 自己的上下文窗口按 card_id 取消息。
"""
from __future__ import annotations

import inspect
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from bot.logger import get_logger

logger = get_logger(__name__)

CARD_TTL = timedelta(days=3)

# 这些来源是对用户消息的回应，归入触发它的卡片；其余来源都算 AI 主动发起，各开一张卡片。
REPLY_SOURCE_TYPES = frozenset({
    "chat", "chat_error", "chat_tool_feedback", "tool_batch",
})

# 发出之后要开 thread 的主动消息。reminder、system_alert、curator 通知这类不需要
# 展开聊，仍然留在主频道：没回复就不进历史，在主频道 reply 了才写进主对话。
THREAD_SOURCE_TYPES = frozenset({"check_in"})

# 用户在一个找不到卡片的 thread 里说话时（pending 卡片已经过期删除、或者 thread
# 是她自己从别的消息上开的），为这个 thread 新开的卡片用这个来源类型。
THREAD_SOURCE_TYPE = "thread"

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


def opens_thread(source_type: str | None) -> bool:
    return source_type in THREAD_SOURCE_TYPES


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class CardAssignment:
    """一条用户消息归属到哪张卡片。

    created：卡片是为这个 thread 新开的，里面还没有 AI 的开场白（bot 据此去
      Discord 取 thread 的起始消息补进历史）。
    """
    card_id: int
    thread_id: str | None
    created: bool = False


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

    # --- 列表 -----------------------------------------------------------------

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

    def assign_user_message(self, channel_id: str,
                            reply_to_message_id: str | None, *,
                            thread_id: str | None = None) -> CardAssignment | None:
        """用户消息写入之前调用：确定它属于哪张卡片，必要时先把卡片转正。

        thread_id：消息发在哪个 thread 里（主频道里的消息传 None）。
        返回 None 表示这是主频道的普通聊天，不归属任何卡片。
        """
        self.purge_expired()
        channel_id = str(channel_id)

        if thread_id is not None:
            thread_id = str(thread_id)
            card = self.db.get_card_by_thread(thread_id)
            if card is None:
                # 从消息上开出来的 thread，id 和那条消息相同。她自己对一条还在
                # pending 的消息点了 Create Thread 时会走到这里。
                card_id = self.db.find_card_id_by_discord_message(thread_id)
                card = self.db.get_card(card_id) if card_id is not None else None
                if card is not None and card.get("thread_id") is None:
                    self.db.set_card_thread(int(card["id"]), thread_id)
                else:
                    card = None
            if card is None:
                now, expires = self._times()
                card_id = self.db.create_card(
                    channel_id=channel_id, source_type=THREAD_SOURCE_TYPE,
                    source_id=thread_id, status="active",
                    now=now, expires_at=expires, thread_id=thread_id,
                )
                logger.info(f"🗂️ 新卡片 #{card_id}（thread {thread_id}）")
                return CardAssignment(card_id, thread_id, created=True)
            self._activate_or_touch(card)
            return CardAssignment(int(card["id"]), thread_id)

        if reply_to_message_id:
            card_id = self.db.find_card_id_by_discord_message(str(reply_to_message_id))
            if card_id is not None:
                card = self.db.get_card(card_id)
                self._activate_or_touch(card)
                return CardAssignment(card_id, card.get("thread_id"))
            # 引用的消息找不到卡片：它所在的 pending 卡片已经过期删除，或者是卡片
            # 功能上线前的旧消息。当作主频道的普通聊天，引用内容已经内联在
            # current_content 里了。
        return None

    def _activate_or_touch(self, card: dict) -> None:
        card_id = int(card["id"])
        if card["status"] == "pending":
            now, expires = self._times()
            self.db.activate_card(card_id, now=now, expires_at=expires)
            logger.info(f"🗂️ 卡片 #{card_id} 被回复，进入对话历史")
            return
        self._touch(card_id)

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

        # 回答属于触发它的那条消息所在的卡片；conversation 工具批次以最后一条
        # 用户消息为归属。主频道的普通聊天没有卡片，回答也直接进对话历史。
        card_id = self.db.find_reply_card_id(source_type, source_id)
        if card_id is None:
            await _maybe_await(ingest(**message))
            return
        self._touch(card_id)
        await _maybe_await(ingest(**message, card_id=card_id))


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value
