"""Integration boundaries that make LT-178 reachable from the live process."""

import asyncio
from datetime import datetime, timezone

import bot.discord_bot as discord_bot_module
import bot.scheduler as scheduler_module
from bot.async_pipeline import BatchCoordinator, ToolBatchRepository
from bot.database import Database
from bot.discord_bot import LifeTrackerBot
from bot.memory import MemoryService, scene_state
from bot.scheduler import Scheduler


class CoordinatorSpy:
    def __init__(self):
        self.messages = []
        self.forced = []

    def notify_user_message(self, channel_id, row_id):
        self.messages.append((channel_id, row_id))

    def force(self, channel_id):
        self.forced.append(channel_id)
        return {"action": "created"}


def make_db(tmp_path):
    return Database(str(tmp_path / "wiring.db"))


def test_on_message_notifies_batcher_only_after_durable_insert(
    tmp_path, monkeypatch
):
    db = make_db(tmp_path)
    memory = MemoryService(db)
    coordinator = CoordinatorSpy()
    bot = LifeTrackerBot(
        db,
        memory,
        batch_coordinator=coordinator,
        tool_worker_apply=True,
    )
    monkeypatch.setattr(discord_bot_module.config, "ALLOWED_USER_ID", 42)
    monkeypatch.setattr(discord_bot_module.config, "CHANNEL_ID", 123)

    generated = []

    async def fake_generate(message):
        generated.append(message.id)

    monkeypatch.setattr(bot, "_generate_chat_response", fake_generate)

    class Author:
        id = 42

        def __str__(self):
            return "user-42"

    class Channel:
        id = 123

    class Message:
        id = 9001
        author = Author()
        channel = Channel()
        guild = None
        reference = None
        content = "刚吃完午饭"
        created_at = datetime.now(timezone.utc)
        type = "default"

    asyncio.run(bot.on_message(Message()))

    assert generated == [9001]
    assert len(coordinator.messages) == 1
    channel_id, row_id = coordinator.messages[0]
    assert channel_id == "123"
    assert db.get_conversation_messages_after("123", 0)[0]["id"] == row_id


def test_apply_chat_has_no_tools_and_silent_forces_batch(
    tmp_path, monkeypatch
):
    db = make_db(tmp_path)
    memory = MemoryService(db)
    coordinator = CoordinatorSpy()
    bot = LifeTrackerBot(
        db,
        memory,
        batch_coordinator=coordinator,
        tool_worker_apply=True,
    )
    monkeypatch.setattr(bot, "_is_typing_cooling_down", lambda _id: True)

    async def fake_chat(_db, _messages, *, send_callback, tool_names, **_kwargs):
        assert tool_names == set()
        await send_callback("[SILENT]")
        return "[SILENT]"

    monkeypatch.setattr(discord_bot_module, "chat", fake_chat)

    class Channel:
        id = 123

    class Message:
        id = 77
        channel = Channel()

    asyncio.run(bot._generate_chat_response(Message()))

    assert coordinator.forced == ["123"]


def test_check_in_creates_tool_batch_and_chat_side_uses_no_tools(
    tmp_path, monkeypatch
):
    db = make_db(tmp_path)
    memory = MemoryService(db)
    repository = ToolBatchRepository(db)
    ready = []
    coordinator = BatchCoordinator(
        db,
        repository,
        channel_ids=["123"],
        execution_mode="apply",
        on_batch_ready=lambda batch: ready.append(batch["id"]),
    )
    scheduler = Scheduler(
        db,
        lambda *_args, **_kwargs: None,
        memory_service=memory,
        batch_coordinator=coordinator,
        tool_worker_apply=True,
    )
    monkeypatch.setattr(scheduler_module.config, "CHANNEL_ID", 123)
    check_in_id = db.create_check_in(
        name="interview",
        label="Interview",
        enabled=True,
        schedule_type="window",
        time_start="12:00",
        time_end="13:00",
        prompt_template="{timestamp} interview",
        tool_profile="poll",
        track_scene=True,
    )
    scene_state.start(
        db,
        "123",
        check_in_name="old",
        description="旧场景",
        now=datetime.now(),
    )
    captured = {}

    async def fake_scheduled(
        _db, _prompt, _timestamp, _history, **kwargs
    ):
        captured.update(kwargs)
        return None

    monkeypatch.setattr(scheduler_module, "scheduled_action", fake_scheduled)

    result = asyncio.run(
        scheduler._do_check_in(
            db.get_check_in(check_in_id), "2026-08-28 12:30"
        )
    )

    assert result["ok"] is True
    assert captured["tool_profile"] == "none"
    assert captured["clear_scene"] is False
    assert captured["track_scene"] is False
    assert scene_state.load(db, "123", datetime.now()) is None
    assert len(ready) == 1
    batch = repository.get(ready[0])
    assert batch["source_kind"] == "check_in"
    assert batch["execution_mode"] == "apply"
    assert batch["input"]["track_scene"] is True


def test_apply_reminder_expression_also_has_no_business_tools(
    tmp_path, monkeypatch
):
    db = make_db(tmp_path)
    memory = MemoryService(db)
    delivered = []

    async def send(text, **_kwargs):
        delivered.append(text)

    scheduler = Scheduler(
        db,
        send,
        memory_service=memory,
        tool_worker_apply=True,
    )
    scheduler._running = True
    monkeypatch.setattr(scheduler_module.config, "CHANNEL_ID", 123)
    conn = db._get_conn()
    try:
        cursor = conn.execute(
            "INSERT INTO reminders (trigger_time, action) VALUES (?, ?)",
            ("2020-01-01T08:00:00", "交作业"),
        )
        reminder_id = cursor.lastrowid
        conn.commit()
    finally:
        conn.close()
    captured = {}

    async def fake_scheduled(
        _db, _prompt, _timestamp, _history, *, send_callback, **kwargs
    ):
        captured.update(kwargs)
        await send_callback("到时间交作业了")
        return "到时间交作业了"

    monkeypatch.setattr(scheduler_module, "scheduled_action", fake_scheduled)

    asyncio.run(scheduler._process_due_reminders())

    assert captured["tool_profile"] == "none"
    assert delivered == ["到时间交作业了"]
    assert reminder_id not in {
        item["id"] for item in db.list_active_reminders()
    }
