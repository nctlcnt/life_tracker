import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from bot.async_pipeline import (
    GenerationGate,
    OutboundDeliveryRepository,
    OutboundQueue,
)
from bot.async_pipeline.repository import DeliveryDedupeConflict
from bot.database import Database


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "outbound.db"))


@pytest.fixture
def repository(db):
    return OutboundDeliveryRepository(db)


def test_outbound_schema_matches_the_lt170_contract(db):
    conn = db._get_conn()
    try:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'outbound_deliveries'"
        ).fetchone()["sql"]
        columns = {
            row["name"] for row in conn.execute(
                "PRAGMA table_info(outbound_deliveries)")
        }
        indexes = {
            row["name"] for row in conn.execute(
                "PRAGMA index_list(outbound_deliveries)")
        }
    finally:
        conn.close()

    assert columns == {
        "id", "channel_id", "kind", "content", "reaction",
        "target_discord_message_id", "source_type", "source_id",
        "dedupe_key", "status", "attempt_count", "available_at",
        "locked_at", "lease_token", "discord_message_ids_json",
        "last_error", "created_at", "updated_at", "sent_at",
    }
    assert "idx_outbound_deliveries_channel_order" in indexes
    assert "'pending', 'sending', 'retry_wait'" in sql
    assert "'sent', 'failed'" in sql


def test_enqueue_is_idempotent_and_rejects_dedupe_key_reuse(repository):
    first, created = repository.enqueue_message(
        channel_id="channel", content="hello", source_type="chat",
        source_id="message-1", dedupe_key="chat:message-1:1")
    again, created_again = repository.enqueue_message(
        channel_id="channel", content="hello", source_type="chat",
        source_id="message-1", dedupe_key="chat:message-1:1")

    assert created is True
    assert created_again is False
    assert again["id"] == first["id"]

    with pytest.raises(DeliveryDedupeConflict):
        repository.enqueue_message(
            channel_id="channel", content="different", source_type="chat",
            source_id="message-1", dedupe_key="chat:message-1:1")


def test_retrying_channel_head_blocks_later_delivery_only_in_that_channel(repository):
    first, _ = repository.enqueue_message(
        channel_id="a", content="a1", source_type="test",
        source_id="a1", dedupe_key="a1")
    repository.enqueue_message(
        channel_id="a", content="a2", source_type="test",
        source_id="a2", dedupe_key="a2")
    other, _ = repository.enqueue_message(
        channel_id="b", content="b1", source_type="test",
        source_id="b1", dedupe_key="b1")
    now = datetime.now(timezone.utc) + timedelta(seconds=1)

    claimed = repository.claim_next(now=now)
    assert claimed["id"] == first["id"]
    assert repository.mark_retry(
        claimed["id"], claimed["lease_token"], "temporary",
        delay_seconds=60, now=now)

    claimed_other = repository.claim_next(now=now)
    assert claimed_other["id"] == other["id"]
    assert repository.claim_next(now=now) is None


def test_expired_lease_is_recovered_and_old_owner_is_fenced(repository):
    item, _ = repository.enqueue_message(
        channel_id="a", content="hello", source_type="test",
        source_id="1", dedupe_key="lease")
    claimed_at = datetime.now(timezone.utc) + timedelta(seconds=1)
    claimed = repository.claim_next(now=claimed_at)

    recovered = repository.recover_expired_sending(
        lease_seconds=300,
        now=claimed_at + timedelta(minutes=6),
    )
    assert recovered == [item["id"]]

    reclaimed = repository.claim_next(
        now=claimed_at + timedelta(minutes=6))
    assert reclaimed["lease_token"] != claimed["lease_token"]
    assert repository.mark_sent(
        claimed["id"], claimed["lease_token"], ["stale"]
    ) is False
    assert repository.mark_sent(
        reclaimed["id"], reclaimed["lease_token"], ["fresh"]
    ) is True
    assert repository.get(item["id"])["discord_message_ids"] == ["fresh"]


def test_persisted_pending_delivery_survives_queue_restart(repository):
    async def scenario():
        delivered = []

        async def transport(item):
            delivered.append(item["content"])
            return [f"discord-{item['id']}"]

        first_queue = OutboundQueue(repository, transport)
        pending = await first_queue.enqueue_message(
            channel_id="a", content="survives", source_type="test",
            source_id="restart", dedupe_key="restart",
            wait_for_delivery=False,
        )
        assert pending.status == "pending"

        restarted_queue = OutboundQueue(
            repository, transport, idle_poll_seconds=0.01)
        runner = asyncio.create_task(restarted_queue.run())
        receipt = await restarted_queue.enqueue_message(
            channel_id="a", content="survives", source_type="test",
            source_id="restart", dedupe_key="restart",
        )
        await restarted_queue.stop()
        await runner

        assert receipt.delivered
        assert delivered == ["survives"]

    asyncio.run(scenario())


def test_terminal_failure_releases_the_next_delivery(repository):
    async def scenario():
        attempts = []

        async def transport(item):
            attempts.append(item["content"])
            if item["content"] == "broken":
                raise RuntimeError("Discord unavailable")
            return [str(item["id"])]

        queue = OutboundQueue(
            repository,
            transport,
            max_attempts=2,
            retry_base_seconds=0,
            idle_poll_seconds=0.01,
        )
        runner = asyncio.create_task(queue.run())
        broken_task = asyncio.create_task(queue.enqueue_message(
            channel_id="a", content="broken", source_type="test",
            source_id="broken", dedupe_key="broken",
        ))
        good_task = asyncio.create_task(queue.enqueue_message(
            channel_id="a", content="good", source_type="test",
            source_id="good", dedupe_key="good",
        ))
        broken, good = await asyncio.gather(broken_task, good_task)
        await queue.stop()
        await runner

        assert broken.status == "failed"
        assert good.delivered
        assert attempts == ["broken", "broken", "good"]
        assert repository.non_terminal_ids() == []

    asyncio.run(scenario())


def test_one_delivery_finishes_all_chunks_before_the_next(repository):
    async def scenario():
        from bot.discord_bot import _send_chat_chunks

        sent_chunks = []

        class Sent:
            def __init__(self, message_id):
                self.id = message_id

        class Channel:
            async def send(self, content):
                sent_chunks.append(content)
                await asyncio.sleep(0)
                return Sent(len(sent_chunks))

        channel = Channel()

        async def transport(item):
            return await _send_chat_chunks(
                channel, item["content"], use_typing=False)

        queue = OutboundQueue(
            repository, transport, idle_poll_seconds=0.01)
        runner = asyncio.create_task(queue.run())
        first_task = asyncio.create_task(queue.enqueue_message(
            channel_id="a", content="a" * 2001, source_type="test",
            source_id="first", dedupe_key="chunked:first",
        ))
        second_task = asyncio.create_task(queue.enqueue_message(
            channel_id="a", content="second", source_type="test",
            source_id="second", dedupe_key="chunked:second",
        ))
        first, second = await asyncio.gather(first_task, second_task)
        await queue.stop()
        await runner

        assert first.discord_message_ids == ("1", "2")
        assert second.discord_message_ids == ("3",)
        assert sent_chunks == ["a" * 2000, "a", "second"]

    asyncio.run(scenario())


def test_shared_gate_orders_chat_before_proactive_generation(repository):
    """Integration boundary: a proactive generation cannot enqueue before chat.

    The consumer never takes the gate, so chat may wait for its delivery while
    still holding the generation lock without deadlocking.
    """
    async def scenario():
        gate = GenerationGate()
        delivered = []
        chat_started = asyncio.Event()
        release_chat = asyncio.Event()

        async def transport(item):
            delivered.append(item["content"])
            await asyncio.sleep(0)
            return [str(item["id"])]

        queue = OutboundQueue(
            repository, transport, idle_poll_seconds=0.01)
        runner = asyncio.create_task(queue.run())

        async def chat_generation():
            async with gate:
                chat_started.set()
                await release_chat.wait()
                return await queue.enqueue_message(
                    channel_id="a", content="chat", source_type="chat",
                    source_id="user-1", dedupe_key="chat:user-1:1")

        async def proactive_generation():
            await chat_started.wait()
            async with gate:
                return await queue.enqueue_message(
                    channel_id="a", content="proactive",
                    source_type="check_in", source_id="morning",
                    dedupe_key="check-in:morning:1")

        chat_task = asyncio.create_task(chat_generation())
        proactive_task = asyncio.create_task(proactive_generation())
        await chat_started.wait()
        await asyncio.sleep(0)
        assert delivered == []
        release_chat.set()

        chat_receipt, proactive_receipt = await asyncio.gather(
            chat_task, proactive_task)
        await queue.stop()
        await runner

        assert chat_receipt.delivered and proactive_receipt.delivered
        assert delivered == ["chat", "proactive"]

    asyncio.run(scenario())


def test_bot_chat_and_scheduler_checkin_share_generation_gate(
        db, repository, monkeypatch):
    """The real Bot/Scheduler producer boundaries use the same injected gate."""
    import config
    import bot.discord_bot as discord_bot_module
    import bot.scheduler as scheduler_module
    from bot.discord_bot import LifeTrackerBot
    from bot.memory import MemoryService
    from bot.scheduler import Scheduler

    async def scenario():
        gate = GenerationGate()
        delivered = []
        chat_started = asyncio.Event()
        release_chat = asyncio.Event()

        async def transport(item):
            delivered.append(item["content"])
            return [str(item["id"])]

        queue = OutboundQueue(
            repository, transport, idle_poll_seconds=0.01)
        memory = MemoryService(db)
        bot = LifeTrackerBot(db, memory, generation_gate=gate)
        bot.set_outbound_queue(queue)
        monkeypatch.setattr(bot, "_is_typing_cooling_down", lambda _channel_id: True)
        scheduler = Scheduler(
            db, bot.send_proactive_message,
            memory_service=memory, generation_gate=gate)
        monkeypatch.setattr(config, "CHANNEL_ID", 123)

        async def fake_chat(_db, _messages, *, send_callback, **_kwargs):
            chat_started.set()
            await release_chat.wait()
            await send_callback("chat reply")

        async def fake_scheduled_action(
                _db, _prompt, _timestamp, _history, *, send_callback, **_kwargs):
            await send_callback("check-in reply")
            return "check-in reply"

        monkeypatch.setattr(discord_bot_module, "chat", fake_chat)
        monkeypatch.setattr(
            scheduler_module, "scheduled_action", fake_scheduled_action)

        class Message:
            id = 77

            class Channel:
                id = 123

            channel = Channel()

        check_in_id = db.create_check_in(
            name="integration_check_in",
            label="Integration check-in",
            enabled=True,
            schedule_type="window",
            time_start="09:00",
            time_end="10:00",
            prompt_template="hello",
            tool_profile="none",
        )

        runner = asyncio.create_task(queue.run())

        async def chat_flow():
            async with bot.generation_gate:
                await bot._generate_chat_response(Message())

        chat_task = asyncio.create_task(chat_flow())
        await chat_started.wait()
        checkin_task = asyncio.create_task(scheduler._do_check_in(
            db.get_check_in(check_in_id), "2026-08-26 12:00"))
        await asyncio.sleep(0)
        assert delivered == []
        release_chat.set()

        await asyncio.gather(chat_task, checkin_task)
        await queue.stop()
        await runner

        assert bot.generation_gate is scheduler.generation_gate is gate
        assert delivered == ["chat reply", "check-in reply"]

    asyncio.run(scenario())
