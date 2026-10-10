"""check-in 在 Discord thread 里进行。

check-in 发在主频道，bot 随即从这条消息上开一个 thread；对话都在 thread 里，
主频道只剩普通的陪聊。thread 里的消息在 DB 里仍记在主频道下，只靠 card_id 区分，
所以 compact、工具批次、长期记忆这些按频道工作的流程照样看得到它们。

这里用假的 Discord 对象。FakeThread 继承 discord.Thread，是因为代码里用
isinstance 判断一条消息是不是在 thread 里。
"""
import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import discord
import pytest

import config
import bot.discord_bot as discord_bot_module
from bot.database import Database
from bot.discord_bot import (
    ACTIVE_THREAD_ARCHIVE_MINUTES,
    PENDING_THREAD_ARCHIVE_MINUTES,
    LifeTrackerBot,
    _record_sent_chunk,
    _send_chat_chunks,
    _thread_name,
)
from bot.memory import MemoryService
from bot.memory.context_window import assemble_card_window, save_summary_state

CHANNEL_ID = 100
USER_ID = 1
CREATED_AT = datetime(2026, 10, 10, 9, 0, tzinfo=timezone.utc)


class FakeGuild:
    id = 9

    def __init__(self):
        self.threads = {}

    def get_thread(self, thread_id):
        return self.threads.get(int(thread_id))

    async def fetch_channel(self, channel_id):
        return self.threads[int(channel_id)]


class _Sender:
    """主频道和 thread 共用的发送逻辑：记下发出去的内容，返回 FakeSent。"""

    def _init_sender(self, guild):
        self.guild = guild
        self.sent = []

    async def send(self, content):
        sent = FakeSent(_next_id(), content, self, self.guild)
        self.sent.append(sent)
        return sent


class FakeChannel(_Sender):
    def __init__(self, guild, channel_id=CHANNEL_ID):
        self.id = channel_id
        self._init_sender(guild)
        self.messages = {}

    async def fetch_message(self, message_id):
        return self.messages[int(message_id)]


class FakeThread(_Sender, discord.Thread):
    # discord.Thread 里这几个是只读 property；在子类里换成普通属性
    guild = None
    parent = None
    starter_message = None

    def __init__(self, thread_id, parent, guild, auto_archive_duration):
        self.id = thread_id
        self.parent_id = parent.id
        self.parent = parent
        self.auto_archive_duration = auto_archive_duration
        self.edits = []
        self._init_sender(guild)

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        if "auto_archive_duration" in kwargs:
            self.auto_archive_duration = kwargs["auto_archive_duration"]


class FakeSent:
    author = None
    reference = None
    type = "default"
    created_at = CREATED_AT

    def __init__(self, message_id, content, channel, guild):
        self.id = message_id
        self.content = content
        self.channel = channel
        self.guild = guild
        if isinstance(channel, FakeChannel):
            channel.messages[message_id] = self

    async def create_thread(self, *, name, auto_archive_duration):
        thread = FakeThread(self.id, self.channel, self.guild, auto_archive_duration)
        thread.created_name = name
        self.guild.threads[self.id] = thread
        return thread


class FakeAuthor:
    id = USER_ID

    def __str__(self):
        return "ina"


class FakeReference:
    def __init__(self, message_id):
        self.message_id = message_id


class FakeUserMessage:
    type = "default"
    created_at = CREATED_AT
    guild = None

    def __init__(self, content, channel, reply_to=None):
        self.id = _next_id()
        self.content = content
        self.channel = channel
        self.author = FakeAuthor()
        self.reference = FakeReference(reply_to) if reply_to else None


_ids = iter(range(1000, 100000))


def _next_id():
    return next(_ids)


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "threads.db"))


@pytest.fixture
def memory(db):
    return MemoryService(db)


@pytest.fixture(autouse=True)
def single_channel(monkeypatch):
    monkeypatch.setattr(config, "CHANNEL_ID", CHANNEL_ID)
    monkeypatch.setattr(config, "ALLOWED_USER_ID", USER_ID)
    # 总结要调模型，这里不需要
    monkeypatch.setattr(discord_bot_module, "schedule_summary", lambda *_a, **_k: False)


@pytest.fixture
def guild():
    return FakeGuild()


@pytest.fixture
def main(guild):
    return FakeChannel(guild)


def _send_check_in(db, memory, channel, text, source_id="morning:1"):
    return asyncio.run(_send_chat_chunks(
        channel, text, db=db, memory_service=memory,
        source_type="check_in", source_id=source_id, use_typing=False))


def _rows(db, sql, params=()):
    conn = db._get_conn()
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


# ── 发出 check-in ──────────────────────────────────────────────────────────

def test_check_in_opens_a_thread_from_its_message(db, memory, main, guild):
    _send_check_in(db, memory, main, "早上好呀\n今天有什么安排？")

    [sent] = main.sent
    thread = guild.threads[sent.id]
    card = db.get_card_by_source("check_in", "morning:1")
    assert card["thread_id"] == str(sent.id)
    assert card["status"] == "pending"
    assert thread.created_name == "早上好呀"
    assert thread.auto_archive_duration == PENDING_THREAD_ARCHIVE_MINUTES
    # 没人回之前不进对话历史
    assert _rows(db, "SELECT * FROM conversation_messages") == []


def test_later_messages_of_the_same_check_in_go_into_its_thread(db, memory, main, guild):
    _send_check_in(db, memory, main, "早上好呀")
    _send_check_in(db, memory, main, "对了，今天要下雨")

    assert [s.content for s in main.sent] == ["早上好呀"]
    thread = guild.threads[main.sent[0].id]
    assert [s.content for s in thread.sent] == ["对了，今天要下雨"]
    # thread 里发的消息也记在主频道下，而且属于同一张卡片
    pending = _rows(db, "SELECT channel_id, metadata_json FROM card_pending_messages ORDER BY id")
    assert [p["channel_id"] for p in pending] == [str(CHANNEL_ID)] * 2
    assert json.loads(pending[1]["metadata_json"])["discord_channel_id"] == str(thread.id)


def test_a_long_check_in_continues_inside_the_thread(db, memory, main, guild):
    _send_check_in(db, memory, main, "a" * 2000 + "b" * 10)

    assert [s.content for s in main.sent] == ["a" * 2000]
    assert [s.content for s in guild.threads[main.sent[0].id].sent] == ["b" * 10]


def test_check_in_stays_in_the_channel_when_the_thread_cannot_be_opened(db, memory, guild):
    """比如 bot 没有开 thread 的权限：消息留在频道里，卡片照常是 pending。"""
    class NoThreadSent(FakeSent):
        async def create_thread(self, **_kwargs):
            raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"),
                                    "Missing Permissions")

    class Channel(FakeChannel):
        async def send(self, content):
            sent = NoThreadSent(_next_id(), content, self, self.guild)
            self.sent.append(sent)
            return sent

    channel = Channel(guild)
    _send_check_in(db, memory, channel, "早上好")

    assert [s.content for s in channel.sent] == ["早上好"]
    card = db.get_card_by_source("check_in", "morning:1")
    assert card["status"] == "pending"
    assert card["thread_id"] is None


def test_reminders_do_not_open_threads(db, memory, main, guild):
    asyncio.run(_send_chat_chunks(
        main, "该交作业了", db=db, memory_service=memory,
        source_type="reminder", source_id="r1", use_typing=False))

    assert guild.threads == {}
    assert db.get_card_by_source("reminder", "r1")["thread_id"] is None


def test_record_sent_chunk_files_thread_messages_under_the_main_channel(db, memory, main, guild):
    _send_check_in(db, memory, main, "早上好")
    thread = guild.threads[main.sent[0].id]
    sent = FakeSent(_next_id(), "直接写进历史", thread, guild)

    asyncio.run(_record_sent_chunk(db, sent, "assistant"))

    [row] = _rows(db, "SELECT channel_id, metadata_json FROM conversation_messages")
    assert row["channel_id"] == str(CHANNEL_ID)
    assert json.loads(row["metadata_json"])["discord_channel_id"] == str(thread.id)


# ── 在 thread 里聊 ─────────────────────────────────────────────────────────

@pytest.fixture
def bot(db, memory, monkeypatch):
    instance = LifeTrackerBot(db, memory)
    monkeypatch.setattr(instance, "_is_typing_cooling_down", lambda _channel_id: True)
    return instance


@pytest.fixture
def fake_chat(monkeypatch):
    calls = []

    async def chat(_db, messages, *, send_callback, **_kwargs):
        calls.append([m["content"] for m in messages])
        await send_callback("好呀，注意防晒")

    monkeypatch.setattr(discord_bot_module, "chat", chat)
    return calls


def test_talking_in_the_thread_stays_in_the_thread(db, memory, main, guild, bot, fake_chat):
    # 主频道里先有一段普通陪聊，thread 的上下文里不应该出现它
    asyncio.run(bot.on_message(FakeUserMessage("主频道里随便聊聊", main)))
    main.sent.clear()
    fake_chat.clear()
    _send_check_in(db, memory, main, "今天打算做什么？")
    thread = guild.threads[main.sent[0].id]

    asyncio.run(bot.on_message(FakeUserMessage("下午去爬山", thread)))

    assert [s.content for s in thread.sent] == ["好呀，注意防晒"]
    assert [s.content for s in main.sent] == ["今天打算做什么？"]
    [context] = fake_chat
    assert any("今天打算做什么？" in c for c in context)
    assert any("下午去爬山" in c for c in context)
    assert not any("主频道里随便聊聊" in c for c in context)
    # 回复过之后 thread 保持 3 天
    assert thread.auto_archive_duration == ACTIVE_THREAD_ARCHIVE_MINUTES

    card = db.get_card_by_source("check_in", "morning:1")
    assert card["status"] == "active"
    rows = _rows(db, "SELECT content, channel_id, card_id FROM conversation_messages "
                     "WHERE card_id IS NOT NULL ORDER BY id")
    assert [r["content"] for r in rows] == ["今天打算做什么？", "下午去爬山", "好呀，注意防晒"]
    assert {r["channel_id"] for r in rows} == {str(CHANNEL_ID)}
    assert {r["card_id"] for r in rows} == {card["id"]}


def test_main_chat_still_sees_what_was_said_in_threads(db, memory, main, guild, bot, fake_chat):
    _send_check_in(db, memory, main, "今天打算做什么？")
    thread = guild.threads[main.sent[0].id]
    asyncio.run(bot.on_message(FakeUserMessage("下午去爬山", thread)))
    fake_chat.clear()

    asyncio.run(bot.on_message(FakeUserMessage("我出发啦", main)))

    [context] = fake_chat
    assert any("下午去爬山" in c and "check_in1" in c for c in context)
    assert main.sent[-1].content == "好呀，注意防晒"


def test_replying_to_a_check_in_in_the_main_channel_answers_in_its_thread(
        db, memory, main, guild, bot, fake_chat):
    _send_check_in(db, memory, main, "今天打算做什么？")
    check_in = main.sent[0]
    thread = guild.threads[check_in.id]

    asyncio.run(bot.on_message(FakeUserMessage("下午去爬山", main, reply_to=check_in.id)))

    assert [s.content for s in main.sent] == ["今天打算做什么？"]
    assert [s.content for s in thread.sent] == ["好呀，注意防晒"]
    assert db.get_card_by_source("check_in", "morning:1")["status"] == "active"


def test_a_thread_without_a_card_picks_up_its_starter_message(
        db, memory, main, guild, bot, fake_chat):
    """pending 卡片过期删掉之后才在 thread 里说话：起始消息从 Discord 取回来补进上下文。"""
    starter = FakeSent(_next_id(), "周末要不要去海边？", main, guild)
    thread = asyncio.run(starter.create_thread(name="x", auto_archive_duration=1440))

    asyncio.run(bot.on_message(FakeUserMessage("好啊", thread)))

    [context] = fake_chat
    assert any("周末要不要去海边？" in c for c in context)
    card = db.get_card_by_thread(str(thread.id))
    assert card["source_type"] == "thread"
    assert [r["content"] for r in _rows(
        db, "SELECT content FROM conversation_messages WHERE card_id = ? ORDER BY id",
        (card["id"],))] == ["周末要不要去海边？", "好啊", "好呀，注意防晒"]


def test_messages_in_other_channels_threads_are_ignored(db, guild, bot, fake_chat):
    other = FakeChannel(guild, channel_id=555)
    starter = FakeSent(_next_id(), "别的频道", other, guild)
    thread = asyncio.run(starter.create_thread(name="x", auto_archive_duration=1440))

    asyncio.run(bot.on_message(FakeUserMessage("hi", thread)))

    assert fake_chat == []
    assert thread.sent == []


def test_thread_is_renamed_once_with_the_first_summary(db, memory, main, guild, bot, monkeypatch):
    _send_check_in(db, memory, main, "今天打算做什么？")
    thread = guild.threads[main.sent[0].id]
    card_id = db.get_card_by_source("check_in", "morning:1")["id"]
    monkeypatch.setattr(bot, "get_channel", lambda _id: main)

    asyncio.run(bot._rename_thread_after_summary(card_id, "下午去爬山的计划", None))
    asyncio.run(bot._rename_thread_after_summary(card_id, "爬山回来很累", "下午去爬山的计划"))

    assert thread.edits == [{"name": "下午去爬山的计划"}]


def test_reactions_find_the_thread_their_message_lives_in(db, memory, main, guild, bot):
    _send_check_in(db, memory, main, "今天打算做什么？")
    thread = guild.threads[main.sent[0].id]
    db.add_conversation_message(
        discord_message_id="777", channel_id=str(CHANNEL_ID), role="user",
        content="下午去爬山", created_at=CREATED_AT.isoformat(),
        metadata={"discord_channel_id": str(thread.id)})

    assert bot._actual_discord_channel_id("777") == str(thread.id)
    assert bot._actual_discord_channel_id("missing") is None


# ── thread 的上下文窗口 ─────────────────────────────────────────────────────

def test_card_window_is_channel_summary_plus_the_card(db, memory):
    db.add_conversation_message(
        discord_message_id="1", channel_id=str(CHANNEL_ID), role="user",
        content="主频道的话", created_at=CREATED_AT.isoformat())
    card_id = db.create_card(
        channel_id=str(CHANNEL_ID), source_type="check_in", source_id="c1",
        status="active", now="2026-10-10T09:00:00+00:00",
        expires_at="2026-10-13T09:00:00+00:00", thread_id="2")
    db.add_conversation_message(
        discord_message_id="2", channel_id=str(CHANNEL_ID), role="assistant",
        content="今天打算做什么？", created_at=CREATED_AT.isoformat(), card_id=card_id)
    db.add_conversation_message(
        discord_message_id="3", channel_id=str(CHANNEL_ID), role="user",
        content="下午去爬山", created_at=CREATED_AT.isoformat(), card_id=card_id)
    # 摘要已经覆盖到卡片的消息，卡片的原话仍然要给全
    save_summary_state(db, str(CHANNEL_ID), summary="最近在准备考试",
                       upto_message_id=3, model="m", updated_at="now")

    window = assemble_card_window(db, str(CHANNEL_ID), card_id)

    contents = [m["content"] for m in window.messages]
    assert "最近在准备考试" in contents[0]
    assert [c.split("] ", 1)[-1] for c in contents[1:]] == ["今天打算做什么？", "下午去爬山"]
    assert not any("主频道的话" in c for c in contents)
    assert window.needs_compact is False
    assert window.tail_count == 2


def test_thread_name_uses_the_first_line():
    assert _thread_name("\n  早上好呀  \n今天呢") == "早上好呀"
    assert _thread_name("") == "check-in"
    assert _thread_name("字" * 50) == "字" * 40 + "…"


def test_threads_she_opened_herself_keep_their_name(db, main, guild, bot, monkeypatch):
    starter = FakeSent(_next_id(), "随手记一下", main, guild)
    thread = asyncio.run(starter.create_thread(name="我起的名字", auto_archive_duration=1440))
    card_id = bot.cards.assign_user_message(
        str(CHANNEL_ID), None, thread_id=str(thread.id)).card_id
    monkeypatch.setattr(bot, "get_channel", lambda _id: main)

    asyncio.run(bot._rename_thread_after_summary(card_id, "模型的总结", None))

    assert thread.edits == []
