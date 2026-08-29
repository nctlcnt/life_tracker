"""LT-178: 30s silence, 60s cap, and forced batch creation."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from bot.async_pipeline import BatchCoordinator, ToolBatchRepository, plan_due_batch
from bot.database import Database


NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
CHANNEL = "chan-178"


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "batcher.db"))


@pytest.fixture
def repository(db):
    return ToolBatchRepository(db)


def add(db, role, content, at):
    return db.add_conversation_message(
        discord_message_id=f"{role}:{content}:{at.isoformat()}",
        channel_id=CHANNEL,
        role=role,
        content=content,
        created_at=at.isoformat(),
    )


def due(db, repository, *, now=NOW, force=False):
    return plan_due_batch(
        db,
        repository,
        channel_id=CHANNEL,
        execution_mode="shadow",
        now=now,
        force=force,
    )


def test_a_fresh_message_waits_for_thirty_seconds(db, repository):
    add(db, "user", "刚发的", NOW - timedelta(seconds=29))

    waiting = due(db, repository)

    assert waiting["action"] == "waiting"
    assert waiting["reason"] == "silence_window"
    assert waiting["due_in_seconds"] == pytest.approx(1)
    assert repository.open_conversation_batch(CHANNEL) is None


def test_thirty_seconds_of_silence_creates_the_batch(db, repository):
    row_id = add(db, "user", "已经安静", NOW - timedelta(seconds=30))

    outcome = due(db, repository)

    assert outcome["action"] == "created"
    assert outcome["reason"] == "silence"
    assert outcome["batch"]["last_user_message_id"] == row_id


def test_new_messages_reset_silence_but_not_the_sixty_second_cap(db, repository):
    first = NOW - timedelta(seconds=60)
    add(db, "user", "三点", first)
    add(db, "user", "不对，四点", NOW - timedelta(seconds=5))

    outcome = due(db, repository)

    assert outcome["action"] == "created"
    assert outcome["reason"] == "max_wait"
    assert outcome["batch"]["through_message_id"] == 2


def test_silent_forces_a_current_batch_without_waiting(db, repository):
    row_id = add(db, "user", "今天还有什么提醒", NOW)

    outcome = due(db, repository, force=True)

    assert outcome["action"] == "created"
    assert outcome["reason"] == "forced"
    assert outcome["batch"]["last_user_message_id"] == row_id


def test_silent_force_survives_an_older_open_batch(db, repository):
    first_id = add(db, "user", "先处理这个", NOW - timedelta(seconds=30))
    coordinator = BatchCoordinator(
        db,
        repository,
        channel_ids=[CHANNEL],
        execution_mode="shadow",
    )
    first = coordinator.plan_channel(CHANNEL, now=NOW)["batch"]
    claimed = repository.claim_next()
    assert claimed["id"] == first["id"]

    second_id = add(db, "user", "今天还有什么提醒", NOW)
    waiting = coordinator.force(CHANNEL)
    assert waiting["reason"] == "open_batch"

    assert repository.mark_completed(
        claimed["id"], claimed["lease_token"], now=NOW
    )
    outcome = coordinator.plan_channel(CHANNEL, now=NOW)

    assert repository.get_cursor(CHANNEL) == first_id
    assert outcome["action"] == "created"
    assert outcome["reason"] == "forced"
    assert outcome["batch"]["last_user_message_id"] == second_id


def test_assistant_only_tail_is_skipped_immediately(db, repository):
    row_id = add(db, "assistant", "我帮你记下了", NOW)

    outcome = due(db, repository)

    assert outcome["action"] == "skipped"
    assert repository.get_cursor(CHANNEL) == row_id


def test_check_in_is_created_immediately_and_wakes_worker(db, repository):
    ready = []
    coordinator = BatchCoordinator(
        db,
        repository,
        channel_ids=[CHANNEL],
        execution_mode="shadow",
        on_batch_ready=lambda batch: ready.append(batch["id"]),
    )

    first, created = coordinator.create_check_in(
        channel_id=CHANNEL,
        source_ref="check_in:7:2026-08-28T12:00",
        payload={"prompt": "采访开始"},
    )
    second, created_again = coordinator.create_check_in(
        channel_id=CHANNEL,
        source_ref="check_in:7:2026-08-28T12:00",
        payload={"prompt": "采访开始"},
    )

    assert created is True and created_again is False
    assert first["id"] == second["id"]
    assert ready == [first["id"]]


def test_run_loop_uses_the_low_latency_timer(db, repository):
    async def scenario():
        ready = asyncio.Event()
        coordinator = BatchCoordinator(
            db,
            repository,
            channel_ids=[CHANNEL],
            execution_mode="shadow",
            silence_seconds=0.01,
            max_wait_seconds=1,
            idle_poll_seconds=1,
            on_batch_ready=lambda _batch: ready.set(),
        )
        runner = asyncio.create_task(coordinator.run())
        row_id = add(db, "user", "低延迟", datetime.now(timezone.utc))
        coordinator.notify_user_message(CHANNEL, row_id)
        await asyncio.wait_for(ready.wait(), timeout=0.5)
        await coordinator.stop()
        await runner

        assert repository.open_conversation_batch(CHANNEL) is not None

    asyncio.run(scenario())
