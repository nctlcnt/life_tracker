"""消息卡片：没被回复的主动消息不进对话历史。

背景：AI 的 check-in 很多时候没人回，以前每一条都写进 conversation_messages，
之后 memory 和 reflection 读到的大多是 AI 的单方面发言。
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from bot.cards import CARD_TTL, CardService
from bot.database import Database
from bot.discord_bot import _record_sent_chunk
from bot.memory import MemoryService

CHANNEL = "123"


class Clock:
    def __init__(self):
        self.now = datetime(2026, 9, 27, 10, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, delta):
        self.now += delta


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "cards.db"))


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def cards(db, clock):
    return CardService(db, now=clock)


def _msg(discord_id, content, role="assistant", reply_to=None):
    return dict(
        discord_message_id=str(discord_id), channel_id=CHANNEL, role=role,
        content=content, created_at="2026-09-27T10:00:00+00:00",
        reply_to_message_id=reply_to,
    )


def _send(db, cards, discord_id, content, source_type, source_id):
    """模拟一段 AI 消息发送成功后的记录。"""
    memory = MemoryService(db)
    return asyncio.run(cards.record_outbound(
        _msg(discord_id, content), source_type=source_type,
        source_id=source_id, ingest=memory.ingest_message,
    ))


def _user(db, cards, discord_id, content, reply_to=None):
    card_id = cards.card_for_user_message(CHANNEL, reply_to)
    db.add_conversation_message(**_msg(discord_id, content, "user", reply_to),
                                card_id=card_id)
    return card_id


def _history(db):
    conn = db._get_conn()
    try:
        return [(r["content"], r["card_id"]) for r in conn.execute(
            "SELECT content, card_id FROM conversation_messages ORDER BY id")]
    finally:
        conn.close()


def test_unanswered_check_in_stays_out_of_history(db, cards):
    _send(db, cards, 1, "早上好，今天有什么安排？", "check_in", "morning:1")
    assert _history(db) == []
    assert db.get_card_by_source("check_in", "morning:1")["status"] == "pending"


def test_reply_moves_the_card_into_history_before_the_reply(db, cards):
    _send(db, cards, 1, "checkin1", "check_in", "c1")
    _send(db, cards, 2, "checkin2", "check_in", "c2")
    _send(db, cards, 3, "checkin3", "check_in", "c3")

    card_id = _user(db, cards, 10, "回 checkin2", reply_to="2")

    assert _history(db) == [("checkin2", card_id), ("回 checkin2", card_id)]
    assert cards.current_card_id() == card_id


def test_ai_reply_and_follow_up_messages_join_the_current_card(db, cards):
    _send(db, cards, 1, "checkin1", "check_in", "c1")
    card_id = _user(db, cards, 10, "回复", reply_to="1")
    _send(db, cards, 11, "AI 的回答", "chat", "10")
    # 新 check-in 来了，但没有 reply 它
    _send(db, cards, 12, "checkin2", "check_in", "c2")
    # 直接发消息：仍然留在当前卡片
    assert _user(db, cards, 13, "接着说") == card_id

    assert _history(db) == [
        ("checkin1", card_id), ("回复", card_id),
        ("AI 的回答", card_id), ("接着说", card_id),
    ]


def test_all_chunks_of_one_proactive_send_share_a_card(db, cards):
    _send(db, cards, 1, "part 1", "check_in", "c1")
    _send(db, cards, 2, "part 2", "check_in", "c1")
    card_id = _user(db, cards, 10, "ok", reply_to="1")
    assert _history(db) == [("part 1", card_id), ("part 2", card_id), ("ok", card_id)]


def test_plain_message_without_current_card_opens_a_user_card(db, cards):
    card_id = _user(db, cards, 10, "hi")
    assert db.get_card(card_id)["source_type"] == "user"
    assert db.get_card(card_id)["status"] == "active"
    assert cards.current_card_id() == card_id


def test_switching_back_to_an_earlier_card(db, cards):
    _send(db, cards, 1, "poll3", "check_in", "p3")
    poll = _user(db, cards, 10, "回 poll3", reply_to="1")
    _send(db, cards, 2, "checkin4", "check_in", "c4")
    checkin = _user(db, cards, 11, "回 checkin4", reply_to="2")
    back = _user(db, cards, 12, "回到 poll3", reply_to="10")

    assert back == poll != checkin
    assert cards.current_card_id() == poll


def test_pending_cards_are_deleted_after_three_days(db, cards, clock):
    _send(db, cards, 1, "没人回", "check_in", "c1")
    clock.advance(CARD_TTL + timedelta(minutes=1))
    assert cards.purge_expired() == 1
    assert db.get_card_by_source("check_in", "c1") is None


def test_reply_to_a_purged_card_opens_a_new_card(db, cards, clock):
    _send(db, cards, 1, "没人回", "check_in", "c1")
    clock.advance(CARD_TTL + timedelta(minutes=1))
    card_id = _user(db, cards, 10, "晚了一点才回", reply_to="1")
    assert db.get_card(card_id)["source_type"] == "user"
    assert _history(db) == [("晚了一点才回", card_id)]


def test_active_cards_fade_but_keep_their_history(db, cards, clock):
    _send(db, cards, 1, "checkin1", "check_in", "c1")
    first = _user(db, cards, 10, "回复", reply_to="1")
    clock.advance(CARD_TTL + timedelta(minutes=1))
    cards.purge_expired()

    assert db.get_card(first) is not None
    # 当前卡片已经隐去：直接发消息会开新卡片
    second = _user(db, cards, 11, "新的一天")
    assert second != first
    assert [content for content, _ in _history(db)] == ["checkin1", "回复", "新的一天"]


def test_activity_pushes_the_expiry_back(db, cards, clock):
    card_id = _user(db, cards, 10, "hi")
    clock.advance(timedelta(days=2))
    assert _user(db, cards, 11, "还在聊") == card_id
    clock.advance(timedelta(days=2))
    assert _user(db, cards, 12, "还是这张") == card_id


def test_default_source_id_does_not_merge_unrelated_sends(db, cards):
    """send_proactive_message 的默认 source_id 是 "unknown"，不能让所有主动消息挤进一张卡片。"""
    _send(db, cards, 1, "a", "scheduled", "unknown")
    _send(db, cards, 2, "b", "scheduled", "unknown")
    conn = db._get_conn()
    try:
        count = conn.execute("SELECT COUNT(*) FROM conversation_cards").fetchone()[0]
    finally:
        conn.close()
    assert count == 2


def test_record_sent_chunk_without_source_type_keeps_old_behaviour(db):
    class Sent:
        id = 99
        content = "hello"
        guild = None
        author = None
        reference = None
        type = "default"
        created_at = datetime(2026, 9, 27, tzinfo=timezone.utc)

        class channel:
            id = 123

    asyncio.run(_record_sent_chunk(db, Sent(), "assistant"))
    assert _history(db) == [("hello", None)]


def test_record_sent_chunk_routes_check_ins_to_a_pending_card(db):
    class Sent:
        id = 100
        content = "check-in"
        guild = None
        author = None
        reference = None
        type = "default"
        created_at = datetime(2026, 9, 27, tzinfo=timezone.utc)

        class channel:
            id = 123

    asyncio.run(_record_sent_chunk(
        db, Sent(), "assistant", source_type="check_in", source_id="morning:1"))
    assert _history(db) == []
    assert db.get_card_by_source("check_in", "morning:1")["status"] == "pending"
