"""卡片在 Discord 里显示成 thread。

主动消息开 thread 发在里面；用户在 thread 里说话时卡片转为 active；回答和
表情回应发回同一个 thread。对话历史里的 channel_id 一律是主频道。
"""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import discord
import pytest

import config
import bot.discord_bot as discord_bot_module
from bot.database import Database
from bot.discord_bot import LifeTrackerBot
from bot.memory import MemoryService

CHANNEL = 123
USER = 42


class FakeSent:
    def __init__(self, message_id, content, channel):
        self.id = message_id
        self.content = content
        self.channel = channel
        self.guild = None
        self.author = None
        self.reference = None
        self.type = "default"
        self.created_at = datetime(2026, 10, 19, tzinfo=timezone.utc)


class Ids:
    def __init__(self):
        self.value = 1000

    def next(self):
        self.value += 1
        return self.value


class FakeThread(discord.Thread):
    # discord.Thread 的 __init__ 需要真实的连接状态，这里只保留用到的几样
    def __init__(self, thread_id, parent_id, ids):
        self.id = thread_id
        self.parent_id = parent_id
        self._ids = ids
        self.sent = []
        self.added_users = []
        self.reactions = []

    async def send(self, content):
        self.sent.append(content)
        return FakeSent(self._ids.next(), content, self)

    async def add_user(self, user):
        self.added_users.append(user.id)

    def get_partial_message(self, message_id):
        thread = self

        class Partial:
            async def add_reaction(self, reaction):
                thread.reactions.append((message_id, reaction))

        return Partial()


class FakeChannel:
    def __init__(self, ids, *, forbid_threads=False):
        self.id = CHANNEL
        self._ids = ids
        self.sent = []
        self.threads = []
        self.reactions = []
        self.forbid_threads = forbid_threads

    async def send(self, content):
        self.sent.append(content)
        return FakeSent(self._ids.next(), content, self)

    async def create_thread(self, *, name, type, auto_archive_duration):
        if self.forbid_threads:
            raise discord.Forbidden(
                SimpleNamespace(status=403, reason="Forbidden"), "Missing Access")
        thread = FakeThread(self._ids.next(), self.id, self._ids)
        thread.name = name
        thread.auto_archive_duration = auto_archive_duration
        self.threads.append(thread)
        return thread

    def get_partial_message(self, message_id):
        channel = self

        class Partial:
            async def add_reaction(self, reaction):
                channel.reactions.append((message_id, reaction))

        return Partial()


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "threads.db"))


@pytest.fixture
def env(db, monkeypatch):
    monkeypatch.setattr(config, "CHANNEL_ID", CHANNEL)
    monkeypatch.setattr(config, "ALLOWED_USER_ID", USER)
    # 卡片总结会在后台调模型，这里不需要
    monkeypatch.setattr(discord_bot_module, "schedule_summary", lambda *_: None)

    ids = Ids()
    channel = FakeChannel(ids)
    bot = LifeTrackerBot(db, MemoryService(db))
    monkeypatch.setattr(bot, "_is_typing_cooling_down", lambda _channel_id: True)

    async def wait_until_ready():
        return None

    def get_channel(channel_id):
        if channel_id == channel.id:
            return channel
        return next((t for t in channel.threads if t.id == channel_id), None)

    async def fetch_channel(channel_id):
        raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "gone")

    monkeypatch.setattr(bot, "wait_until_ready", wait_until_ready)
    monkeypatch.setattr(bot, "get_channel", get_channel)
    monkeypatch.setattr(bot, "fetch_channel", fetch_channel)
    return SimpleNamespace(bot=bot, channel=channel, ids=ids)


def _deliver(env, delivery_id, content, source_type, source_id, kind="message",
             target=None):
    delivery = {
        "id": delivery_id, "kind": kind, "channel_id": str(CHANNEL),
        "content": content if kind == "message" else None,
        "reaction": content if kind == "reaction" else None,
        "target_discord_message_id": target,
        "source_type": source_type, "source_id": source_id,
    }
    return asyncio.run(env.bot.deliver_outbound(delivery))


def _user_message(env, channel, content):
    return SimpleNamespace(
        id=env.ids.next(), content=content, channel=channel, guild=None,
        author=SimpleNamespace(id=USER), reference=None, type="default",
        created_at=datetime(2026, 10, 19, 1, tzinfo=timezone.utc),
    )


def _history(db):
    conn = db._get_conn()
    try:
        return [(r["content"], r["card_id"], r["channel_id"]) for r in conn.execute(
            "SELECT content, card_id, channel_id FROM conversation_messages ORDER BY id")]
    finally:
        conn.close()


def test_proactive_message_opens_a_thread_and_reuses_it(db, env):
    # 内置的 morning check-in，label 是 "Morning check-in"
    _deliver(env, 1, "早上好", "check_in", "morning:2026-10-19T08:30")
    _deliver(env, 2, "今天有什么安排？", "check_in", "morning:2026-10-19T08:30")

    assert len(env.channel.threads) == 1
    thread = env.channel.threads[0]
    assert thread.sent == ["早上好", "今天有什么安排？"]
    assert env.channel.sent == []
    assert thread.name.startswith("Morning check-in · ")
    assert thread.auto_archive_duration == 4320
    assert thread.added_users == [USER]

    card = db.get_card_by_source("check_in", "morning:2026-10-19T08:30")
    assert card["status"] == "pending"
    assert card["discord_thread_id"] == str(thread.id)
    # 没回复之前不进对话历史
    assert _history(db) == []


def test_each_proactive_send_gets_its_own_thread(env):
    _deliver(env, 1, "提醒 A", "reminder", "batch:1")
    _deliver(env, 2, "提醒 B", "reminder", "batch:2")
    assert [t.sent for t in env.channel.threads] == [["提醒 A"], ["提醒 B"]]
    assert env.channel.threads[0].name.startswith("提醒 · ")


def test_default_source_id_keeps_the_same_thread_on_retry(db, env):
    """缺省 source_id 的投递重试时不能每次开一个新 thread。"""
    _deliver(env, 7, "第一次", "scheduled", "unknown")
    _deliver(env, 7, "重试", "scheduled", "unknown")
    assert len(env.channel.threads) == 1
    assert db.get_card_by_source("scheduled", "delivery:7") is not None


def test_replying_in_the_thread_activates_the_card(db, env, monkeypatch):
    _deliver(env, 1, "早上好", "check_in", "morning:t1")
    thread = env.channel.threads[0]

    async def fake_chat(_db, messages, *, send_callback, **_kwargs):
        await send_callback("那就去吧")

    monkeypatch.setattr(discord_bot_module, "chat", fake_chat)
    asyncio.run(env.bot.on_message(_user_message(env, thread, "下午去爬山")))

    card_id = db.get_card_by_source("check_in", "morning:t1")["id"]
    assert db.get_card(card_id)["status"] == "active"
    assert _history(db) == [
        ("早上好", card_id, str(CHANNEL)),
        ("下午去爬山", card_id, str(CHANNEL)),
        ("那就去吧", card_id, str(CHANNEL)),
    ]
    assert thread.sent == ["早上好", "那就去吧"]
    assert env.channel.sent == []


def test_main_line_chat_stays_out_of_cards(db, env, monkeypatch):
    async def fake_chat(_db, messages, *, send_callback, **_kwargs):
        await send_callback("嗯嗯")

    monkeypatch.setattr(discord_bot_module, "chat", fake_chat)
    asyncio.run(env.bot.on_message(_user_message(env, env.channel, "在吗")))

    assert _history(db) == [("在吗", None, str(CHANNEL)), ("嗯嗯", None, str(CHANNEL))]
    assert env.channel.sent == ["嗯嗯"]
    assert env.channel.threads == []


def test_threads_not_opened_by_a_card_are_ignored(db, env, monkeypatch):
    called = []

    async def fake_chat(*_args, **_kwargs):
        called.append(True)

    monkeypatch.setattr(discord_bot_module, "chat", fake_chat)
    stray = FakeThread(9999, CHANNEL, env.ids)
    asyncio.run(env.bot.on_message(_user_message(env, stray, "随便聊聊")))

    assert called == []
    assert _history(db) == []


def test_tool_batch_reply_and_reaction_go_to_the_card_thread(db, env):
    from bot.async_pipeline.tool_batches import ToolBatchRepository

    _deliver(env, 1, "早上好", "check_in", "morning:t1")
    thread = env.channel.threads[0]
    card_id = env.bot.cards.enter_card(
        db.get_card_by_source("check_in", "morning:t1")["id"])
    row_id = db.add_conversation_message(
        discord_message_id="555", channel_id=str(CHANNEL), role="user",
        content="记一下跑步", created_at="2026-10-19T01:00:00+00:00",
        card_id=card_id,
    )
    batch, _ = ToolBatchRepository(db).create_conversation_batch(
        channel_id=str(CHANNEL), after_message_id=0, through_message_id=row_id,
        last_user_message_id=row_id, execution_mode="apply")

    _deliver(env, 2, "✅", "tool_batch", batch["id"], kind="reaction", target="555")
    _deliver(env, 3, "记好了", "tool_batch", batch["id"])

    assert thread.reactions == [(555, "✅")]
    assert thread.sent[-1] == "记好了"
    assert env.channel.reactions == [] and env.channel.sent == []


def test_check_in_tool_result_joins_the_pending_card(db, env):
    """check-in 工具批次的结果发到这次 check-in 的 thread；卡片还没被回复时先存在卡片里。"""
    from bot.async_pipeline.tool_batches import ToolBatchRepository

    _deliver(env, 1, "早上好", "check_in", "morning:t1")
    batch, _ = ToolBatchRepository(db).create_check_in_batch(
        channel_id=str(CHANNEL), source_ref="check_in:1:t1",
        payload={"prompt": "hi", "card_source_id": "morning:t1"},
        execution_mode="apply")
    _deliver(env, 2, "记录失败了", "tool_batch", batch["id"])

    assert env.channel.threads[0].sent == ["早上好", "记录失败了"]
    assert _history(db) == []
    card = db.get_card_by_source("check_in", "morning:t1")
    env.bot.cards.enter_card(card["id"])
    assert [c for c, _, _ in _history(db)] == ["早上好", "记录失败了"]


def test_missing_thread_permission_still_delivers_in_the_main_channel(db, env):
    env.channel.forbid_threads = True
    _deliver(env, 1, "早上好", "check_in", "morning:t1")
    assert env.channel.sent == ["早上好"]
    assert db.get_card_by_source("check_in", "morning:t1")["discord_thread_id"] is None


def test_typing_in_a_card_thread_counts_as_typing(env):
    _deliver(env, 1, "早上好", "check_in", "morning:t1")
    now = datetime.now(timezone.utc)
    asyncio.run(env.bot.on_typing(
        env.channel.threads[0], SimpleNamespace(id=USER), now))
    assert env.bot.is_user_typing()
