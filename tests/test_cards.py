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


@pytest.mark.parametrize('active', [False, True])
def test_proactive_followup_chunks_extend_expiry(db, cards, clock, active):
    _send(db, cards, 1, 'first', 'check_in', 'c1')
    card_id = db.get_card_by_source('check_in', 'c1')['id']
    if active:
        _user(db, cards, 10, 'reply', reply_to='1')
    clock.advance(timedelta(days=2))
    _send(db, cards, 2, 'later chunk', 'check_in', 'c1')
    card = db.get_card(card_id)
    assert card['expires_at'] == (clock.now + CARD_TTL).isoformat(timespec='seconds')
    assert card['last_active_at'] == clock.now.isoformat(timespec='seconds')
    clock.advance(timedelta(days=2))
    assert cards.purge_expired() == 0
    assert db.get_card(card_id) is not None
    if active:
        assert _user(db, cards, 11, 'still here') == card_id


@pytest.mark.parametrize('source_type', ['chat', 'chat_error', 'chat_tool_feedback', 'tool_batch'])
def test_delayed_reply_keeps_source_card_after_switch_and_restart(db, cards, source_type):
    from bot.async_pipeline.tool_batches import ToolBatchRepository

    first = _user(db, cards, 10, 'question A')
    source_id = '10'
    if source_type == 'tool_batch':
        conn = db._get_conn()
        row_id = conn.execute(
            "SELECT id FROM conversation_messages WHERE discord_message_id = '10'"
        ).fetchone()[0]
        conn.close()
        batch, _ = ToolBatchRepository(db).create_conversation_batch(
            channel_id=CHANNEL, after_message_id=0, through_message_id=row_id,
            last_user_message_id=row_id, execution_mode='apply')
        source_id = batch['id']
    _send(db, cards, 1, 'other question', 'check_in', 'c1')
    second = _user(db, cards, 20, 'answer B', reply_to='1')
    restarted = CardService(db)
    _send(db, restarted, 30, 'late answer A', source_type, source_id)
    assert db.find_card_id_by_discord_message('30') == first
    assert restarted.current_card_id() == second


def test_checkin_tool_batch_keeps_current_card_policy(db, cards):
    from bot.async_pipeline.tool_batches import ToolBatchRepository

    card_id = _user(db, cards, 10, 'hello')
    batch, _ = ToolBatchRepository(db).create_check_in_batch(
        channel_id=CHANNEL, source_ref='check_in:c1',
        payload={'prompt': 'check in'}, execution_mode='apply')
    _send(db, cards, 30, 'check-in result', 'tool_batch', batch['id'])
    assert db.find_card_id_by_discord_message('30') == card_id


# ── /conversation 指令用到的入口 ──────────────────────────────────────────

def test_start_new_card_becomes_the_current_card(db, cards):
    _send(db, cards, 1, "早上好", "check_in", "c1")
    first = _user(db, cards, 10, "回 checkin1", reply_to="1")

    fresh = cards.start_new_card(CHANNEL)

    assert fresh != first
    assert cards.current_card_id() == fresh
    # 之后不 reply 直接说话，进的是新卡片
    assert _user(db, cards, 11, "接着说") == fresh


def test_switch_to_changes_the_current_card(db, cards):
    _send(db, cards, 1, "checkin1", "check_in", "c1")
    _send(db, cards, 2, "checkin2", "check_in", "c2")
    first = _user(db, cards, 10, "回 checkin1", reply_to="1")
    second = _user(db, cards, 11, "回 checkin2", reply_to="2")
    assert cards.current_card_id() == second

    cards.switch_to(first)

    assert cards.current_card_id() == first
    assert _user(db, cards, 12, "接着说") == first


def test_switch_to_rejects_a_card_that_was_never_replied_to(db, cards):
    _send(db, cards, 1, "checkin1", "check_in", "c1")
    pending = db.get_card_by_source("check_in", "c1")["id"]

    with pytest.raises(ValueError):
        cards.switch_to(pending)


def test_switch_to_rejects_a_missing_card(db, cards):
    with pytest.raises(ValueError):
        cards.switch_to(9999)


def test_list_active_marks_the_current_card_and_hides_faded_ones(db, cards, clock):
    _send(db, cards, 1, "checkin1", "check_in", "c1")
    old = _user(db, cards, 10, "回 checkin1", reply_to="1")
    clock.advance(CARD_TTL + timedelta(minutes=1))
    _send(db, cards, 2, "checkin2", "check_in", "c2")
    current = _user(db, cards, 11, "回 checkin2", reply_to="2")

    listed = cards.list_active(CHANNEL)

    ids = [c["id"] for c in listed]
    assert current in ids
    # 过期隐去的卡片不出现在列表里（内容仍然保留在历史中）
    assert old not in ids
    assert [c["is_current"] for c in listed if c["id"] == current] == [True]
    assert [c["label"] for c in listed if c["id"] == current] == ["check_in2"]


def test_list_active_is_empty_before_any_reply(db, cards):
    _send(db, cards, 1, "checkin1", "check_in", "c1")
    assert cards.list_active(CHANNEL) == []
