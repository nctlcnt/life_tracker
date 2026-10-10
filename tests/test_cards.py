"""消息卡片：没被回复的主动消息不进对话历史。

背景：AI 的 check-in 很多时候没人回，以前每一条都写进 conversation_messages，
之后 memory 和 reflection 读到的大多是 AI 的单方面发言。

check-in 的对话在 Discord thread 里进行；主频道的普通聊天不归属任何卡片。
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


def _user(db, cards, discord_id, content, reply_to=None, thread=None):
    assignment = cards.assign_user_message(CHANNEL, reply_to, thread_id=thread)
    card_id = assignment.card_id if assignment else None
    db.add_conversation_message(**_msg(discord_id, content, "user", reply_to),
                                card_id=card_id)
    return card_id


def _thread_for(db, source_id, thread_id):
    """模拟 bot 发出 check-in 之后从这条消息上开了 thread。"""
    card = db.get_card_by_source("check_in", source_id)
    db.set_card_thread(card["id"], thread_id)
    return card["id"]


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


def test_ai_reply_joins_the_card_and_plain_messages_stay_in_the_main_chat(db, cards):
    _send(db, cards, 1, "checkin1", "check_in", "c1")
    card_id = _user(db, cards, 10, "回复", reply_to="1")
    _send(db, cards, 11, "AI 的回答", "chat", "10")
    # 新 check-in 来了，但没有 reply 它
    _send(db, cards, 12, "checkin2", "check_in", "c2")
    # 主频道里直接发消息：普通陪聊，不归属任何卡片
    assert _user(db, cards, 13, "接着说") is None
    _send(db, cards, 14, "陪聊的回答", "chat", "13")

    assert _history(db) == [
        ("checkin1", card_id), ("回复", card_id),
        ("AI 的回答", card_id), ("接着说", None), ("陪聊的回答", None),
    ]


def test_all_chunks_of_one_proactive_send_share_a_card(db, cards):
    _send(db, cards, 1, "part 1", "check_in", "c1")
    _send(db, cards, 2, "part 2", "check_in", "c1")
    card_id = _user(db, cards, 10, "ok", reply_to="1")
    assert _history(db) == [("part 1", card_id), ("part 2", card_id), ("ok", card_id)]


def test_plain_message_in_the_main_channel_opens_no_card(db, cards):
    assert _user(db, cards, 10, "hi") is None
    assert cards.list_active(CHANNEL, include_faded=True) == []


def test_replying_to_an_earlier_message_goes_back_to_its_card(db, cards):
    _send(db, cards, 1, "poll3", "check_in", "p3")
    poll = _user(db, cards, 10, "回 poll3", reply_to="1")
    _send(db, cards, 2, "checkin4", "check_in", "c4")
    checkin = _user(db, cards, 11, "回 checkin4", reply_to="2")
    back = _user(db, cards, 12, "回到 poll3", reply_to="10")

    assert back == poll != checkin


def test_pending_cards_are_deleted_after_three_days(db, cards, clock):
    _send(db, cards, 1, "没人回", "check_in", "c1")
    clock.advance(CARD_TTL + timedelta(minutes=1))
    assert cards.purge_expired() == 1
    assert db.get_card_by_source("check_in", "c1") is None


def test_reply_to_a_purged_card_is_plain_chat(db, cards, clock):
    """引用的卡片已经过期删除：当作主频道的普通聊天，引用内容已经内联在正文里。"""
    _send(db, cards, 1, "没人回", "check_in", "c1")
    clock.advance(CARD_TTL + timedelta(minutes=1))
    card_id = _user(db, cards, 10, "晚了一点才回", reply_to="1")
    assert card_id is None
    assert _history(db) == [("晚了一点才回", None)]


def test_active_cards_fade_but_keep_their_history(db, cards, clock):
    _send(db, cards, 1, "checkin1", "check_in", "c1")
    _thread_for(db, "c1", "1")
    first = _user(db, cards, 10, "回复", thread="1")
    clock.advance(CARD_TTL + timedelta(minutes=1))
    cards.purge_expired()

    assert db.get_card(first) is not None
    assert cards.list_active(CHANNEL) == []
    # 隔了几天又在同一个 thread 里说话：还是这张卡片，重新亮起来
    assert _user(db, cards, 11, "新的一天", thread="1") == first
    assert [c["id"] for c in cards.list_active(CHANNEL)] == [first]
    assert [content for content, _ in _history(db)] == ["checkin1", "回复", "新的一天"]


def test_activity_pushes_the_expiry_back(db, cards, clock):
    _send(db, cards, 1, "checkin1", "check_in", "c1")
    card_id = _thread_for(db, "c1", "1")
    assert _user(db, cards, 10, "hi", thread="1") == card_id
    clock.advance(timedelta(days=2))
    assert _user(db, cards, 11, "还在聊", thread="1") == card_id
    clock.advance(timedelta(days=2))
    assert _user(db, cards, 12, "还是这张", thread="1") == card_id
    assert db.get_card(card_id)["expires_at"] == (
        clock.now + CARD_TTL).isoformat(timespec="seconds")


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
        assert _user(db, cards, 11, 'still here', reply_to='1') == card_id


@pytest.mark.parametrize('source_type', ['chat', 'chat_error', 'chat_tool_feedback', 'tool_batch'])
def test_delayed_reply_keeps_source_card_after_switch_and_restart(db, cards, source_type):
    from bot.async_pipeline.tool_batches import ToolBatchRepository

    _send(db, cards, 5, 'checkin A', 'check_in', 'c0')
    # 她自己对还在 pending 的 check-in 点了 Create Thread：thread id 等于消息 id
    first = _user(db, cards, 10, 'question A', thread='5')
    assert first == db.get_card_by_source('check_in', 'c0')['id']
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
    assert second != first
    restarted = CardService(db)
    _send(db, restarted, 30, 'late answer A', source_type, source_id)
    assert db.find_card_id_by_discord_message('30') == first


def test_checkin_tool_batch_result_goes_to_the_main_history(db, cards):
    from bot.async_pipeline.tool_batches import ToolBatchRepository

    _send(db, cards, 1, 'checkin1', 'check_in', 'c1')
    _user(db, cards, 10, 'hello', reply_to='1')
    batch, _ = ToolBatchRepository(db).create_check_in_batch(
        channel_id=CHANNEL, source_ref='check_in:c1',
        payload={'prompt': 'check in'}, execution_mode='apply')
    _send(db, cards, 30, 'check-in result', 'tool_batch', batch['id'])
    assert db.find_card_id_by_discord_message('30') is None


# ── check-in thread ────────────────────────────────────────────────────────

def test_first_message_in_a_check_in_thread_activates_the_card(db, cards):
    _send(db, cards, 1, "checkin1", "check_in", "c1")
    card_id = _thread_for(db, "c1", "1")

    assignment = cards.assign_user_message(CHANNEL, None, thread_id="1")

    assert assignment.card_id == card_id
    assert assignment.thread_id == "1"
    assert not assignment.created
    assert db.get_card(card_id)["status"] == "active"
    assert [c for c, _ in _history(db)] == ["checkin1"]


def test_replying_in_the_main_channel_points_at_the_check_in_thread(db, cards):
    _send(db, cards, 1, "checkin1", "check_in", "c1")
    card_id = _thread_for(db, "c1", "1")

    assignment = cards.assign_user_message(CHANNEL, "1")

    assert assignment.card_id == card_id
    assert assignment.thread_id == "1"


def test_replying_to_a_reminder_stays_in_the_main_channel(db, cards):
    """提醒类消息不开 thread：在主频道 reply 了才进主对话。"""
    _send(db, cards, 1, "该交作业了", "reminder", "r1")

    assignment = cards.assign_user_message(CHANNEL, "1")

    assert assignment.thread_id is None
    assert db.get_card(assignment.card_id)["status"] == "active"


def test_a_thread_without_a_card_gets_a_new_thread_card(db, cards):
    first = cards.assign_user_message(CHANNEL, None, thread_id="555")
    again = cards.assign_user_message(CHANNEL, None, thread_id="555")

    card = db.get_card(first.card_id)
    assert first.created and not again.created
    assert again.card_id == first.card_id
    assert (card["source_type"], card["status"], card["thread_id"]) == (
        "thread", "active", "555")


def test_talking_in_a_thread_whose_pending_card_was_purged(db, cards, clock):
    _send(db, cards, 1, "没人回", "check_in", "c1")
    old = _thread_for(db, "c1", "1")
    clock.advance(CARD_TTL + timedelta(minutes=1))

    assignment = cards.assign_user_message(CHANNEL, None, thread_id="1")

    assert db.get_card(old) is None
    assert assignment.created
    assert db.get_card(assignment.card_id)["thread_id"] == "1"


def test_list_active_hides_faded_cards(db, cards, clock):
    _send(db, cards, 1, "checkin1", "check_in", "c1")
    old = _user(db, cards, 10, "回 checkin1", reply_to="1")
    clock.advance(CARD_TTL + timedelta(minutes=1))
    _send(db, cards, 2, "checkin2", "check_in", "c2")
    recent = _user(db, cards, 11, "回 checkin2", reply_to="2")

    listed = cards.list_active(CHANNEL)

    ids = [c["id"] for c in listed]
    assert recent in ids
    # 过期隐去的卡片不出现在列表里（内容仍然保留在历史中）
    assert old not in ids
    assert [c["label"] for c in listed if c["id"] == recent] == ["check_in2"]


def test_list_active_is_empty_before_any_reply(db, cards):
    _send(db, cards, 1, "checkin1", "check_in", "c1")
    assert cards.list_active(CHANNEL) == []
