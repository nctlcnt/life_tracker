"""消息卡片：没被回复的主动消息不进对话历史，主线聊天不属于任何卡片。

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


def _user(db, cards, discord_id, content, in_card_of=None):
    """模拟一条用户消息。in_card_of 是卡片里某条消息的 Discord id：给了就表示
    在那张卡片里说话，不给就是主线消息。返回这条消息归属的卡片 id。"""
    card_id = None
    if in_card_of is not None:
        card_id = cards.enter_card(db.find_card_id_by_discord_message(in_card_of))
    db.add_conversation_message(**_msg(discord_id, content, "user"),
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

    card_id = _user(db, cards, 10, "回 checkin2", in_card_of="2")

    assert _history(db) == [("checkin2", card_id), ("回 checkin2", card_id)]
    assert db.get_card(card_id)["status"] == "active"


def test_main_line_messages_do_not_belong_to_any_card(db, cards):
    """主线聊天是一整条线：直接发消息不开卡片，AI 的回答也留在主线。"""
    assert _user(db, cards, 10, "hi") is None
    _send(db, cards, 11, "AI 的回答", "chat", "10")

    assert _history(db) == [("hi", None), ("AI 的回答", None)]
    conn = db._get_conn()
    try:
        count = conn.execute("SELECT COUNT(*) FROM conversation_cards").fetchone()[0]
    finally:
        conn.close()
    assert count == 0


def test_ai_reply_follows_the_card_of_the_triggering_message(db, cards):
    _send(db, cards, 1, "checkin1", "check_in", "c1")
    card_id = _user(db, cards, 10, "回复", in_card_of="1")
    _send(db, cards, 11, "AI 的回答", "chat", "10")
    # 新 check-in 来了，但没有在它里面说话：它留在 pending，主线消息不受影响
    _send(db, cards, 12, "checkin2", "check_in", "c2")
    assert _user(db, cards, 13, "主线上说一句") is None

    assert _history(db) == [
        ("checkin1", card_id), ("回复", card_id),
        ("AI 的回答", card_id), ("主线上说一句", None),
    ]


def test_all_chunks_of_one_proactive_send_share_a_card(db, cards):
    _send(db, cards, 1, "part 1", "check_in", "c1")
    _send(db, cards, 2, "part 2", "check_in", "c1")
    card_id = _user(db, cards, 10, "ok", in_card_of="1")
    assert _history(db) == [("part 1", card_id), ("part 2", card_id), ("ok", card_id)]


def test_pending_cards_are_deleted_after_three_days(db, cards, clock):
    _send(db, cards, 1, "没人回", "check_in", "c1")
    clock.advance(CARD_TTL + timedelta(minutes=1))
    assert cards.purge_expired() == 1
    assert db.get_card_by_source("check_in", "c1") is None


def test_pending_cards_with_a_thread_survive_expiry(db, cards, clock):
    """thread 在 Discord 里还在，几天后在里面回复仍然要能找到卡片。"""
    _send(db, cards, 1, "没人回", "check_in", "c1")
    card_id = db.get_card_by_source("check_in", "c1")["id"]
    db.set_card_thread(card_id, "777")
    clock.advance(CARD_TTL + timedelta(days=2))

    assert cards.purge_expired() == 0
    assert db.find_card_id_by_thread("777") == card_id
    assert _user(db, cards, 10, "晚了几天才回", in_card_of="1") == card_id
    assert _history(db) == [("没人回", card_id), ("晚了几天才回", card_id)]


def test_destination_card(db, cards):
    """主动消息新建自己的卡片；回复类消息找触发消息的卡片，主线上的找不到。"""
    created = cards.destination_card(CHANNEL, source_type="check_in", source_id="c1")
    assert created["status"] == "pending"
    assert cards.destination_card(
        CHANNEL, source_type="check_in", source_id="c1")["id"] == created["id"]

    _user(db, cards, 10, "主线")
    assert cards.destination_card(CHANNEL, source_type="chat", source_id="10") is None
    assert cards.destination_card(
        CHANNEL, source_type="chat_tool_feedback", source_id="10",
        target_discord_message_id="10") is None


def test_entering_a_purged_card_falls_back_to_the_main_line(db, cards, clock):
    _send(db, cards, 1, "没人回", "check_in", "c1")
    card_id = db.get_card_by_source("check_in", "c1")["id"]
    clock.advance(CARD_TTL + timedelta(minutes=1))

    assert cards.enter_card(card_id) is None
    assert _history(db) == []


def test_active_cards_fade_but_keep_their_history(db, cards, clock):
    _send(db, cards, 1, "checkin1", "check_in", "c1")
    first = _user(db, cards, 10, "回复", in_card_of="1")
    clock.advance(CARD_TTL + timedelta(minutes=1))
    cards.purge_expired()

    assert db.get_card(first) is not None
    assert [content for content, _ in _history(db)] == ["checkin1", "回复"]


def test_activity_pushes_the_expiry_back(db, cards, clock):
    _send(db, cards, 1, "checkin1", "check_in", "c1")
    card_id = _user(db, cards, 10, "hi", in_card_of="1")
    clock.advance(timedelta(days=2))
    assert _user(db, cards, 11, "还在聊", in_card_of="1") == card_id
    clock.advance(timedelta(days=2))
    assert _user(db, cards, 12, "还是这张", in_card_of="1") == card_id
    assert not cards.list_active(CHANNEL)[0]["faded"]


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
        _user(db, cards, 10, 'reply', in_card_of='1')
    clock.advance(timedelta(days=2))
    _send(db, cards, 2, 'later chunk', 'check_in', 'c1')
    card = db.get_card(card_id)
    assert card['expires_at'] == (clock.now + CARD_TTL).isoformat(timespec='seconds')
    assert card['last_active_at'] == clock.now.isoformat(timespec='seconds')
    clock.advance(timedelta(days=2))
    assert cards.purge_expired() == 0
    assert db.get_card(card_id) is not None
    if active:
        assert _user(db, cards, 11, 'still here', in_card_of='1') == card_id


@pytest.mark.parametrize('source_type', ['chat', 'chat_error', 'chat_tool_feedback', 'tool_batch'])
def test_delayed_reply_keeps_source_card_across_other_cards_and_restart(db, cards, source_type):
    from bot.async_pipeline.tool_batches import ToolBatchRepository

    _send(db, cards, 1, 'question', 'check_in', 'c1')
    first = _user(db, cards, 10, 'question A', in_card_of='1')
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
    # 回答送达之前，用户先去另一张卡片和主线上说了话，进程也重启过
    _send(db, cards, 2, 'other question', 'check_in', 'c2')
    second = _user(db, cards, 20, 'answer B', in_card_of='2')
    _user(db, cards, 21, 'main line')
    restarted = CardService(db)
    _send(db, restarted, 30, 'late answer A', source_type, source_id)
    assert second != first
    assert db.find_card_id_by_discord_message('30') == first


def test_main_line_reply_stays_on_the_main_line_after_entering_a_card(db, cards):
    """主线消息的回答晚到时，用户已经在某张卡片里说过话，回答也不能被卡片吸走。"""
    _user(db, cards, 10, 'main line question')
    _send(db, cards, 1, 'checkin1', 'check_in', 'c1')
    _user(db, cards, 11, 'in card', in_card_of='1')
    _send(db, cards, 30, 'late main line answer', 'chat', '10')
    assert db.find_card_id_by_discord_message('30') is None


def test_checkin_tool_batch_result_stays_on_the_main_line(db, cards):
    from bot.async_pipeline.tool_batches import ToolBatchRepository

    batch, _ = ToolBatchRepository(db).create_check_in_batch(
        channel_id=CHANNEL, source_ref='check_in:c1',
        payload={'prompt': 'check in'}, execution_mode='apply')
    _send(db, cards, 30, 'check-in result', 'tool_batch', batch['id'])
    assert _history(db) == [('check-in result', None)]


def test_list_active_hides_faded_cards(db, cards, clock):
    _send(db, cards, 1, "checkin1", "check_in", "c1")
    old = _user(db, cards, 10, "回 checkin1", in_card_of="1")
    clock.advance(CARD_TTL + timedelta(minutes=1))
    _send(db, cards, 2, "checkin2", "check_in", "c2")
    recent = _user(db, cards, 11, "回 checkin2", in_card_of="2")

    listed = cards.list_active(CHANNEL)

    ids = [c["id"] for c in listed]
    assert recent in ids
    # 过期隐去的卡片不出现在列表里（内容仍然保留在历史中）
    assert old not in ids
    assert [c["label"] for c in listed if c["id"] == recent] == ["check_in2"]
    assert old in [c["id"] for c in cards.list_active(CHANNEL, include_faded=True)]


def test_list_active_is_empty_before_any_reply(db, cards):
    _send(db, cards, 1, "checkin1", "check_in", "c1")
    assert cards.list_active(CHANNEL) == []
