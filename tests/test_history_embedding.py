import asyncio
from datetime import datetime, timezone

import pytest

import config
from bot.database import Database
import bot.memory.history_embedding as worker


CHANNEL = "channel-1"


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "history_embedding.db"))


@pytest.fixture(autouse=True)
def reset_worker():
    worker._inflight.clear()
    worker._requested_upto.clear()
    yield
    worker._inflight.clear()
    worker._requested_upto.clear()


def _add(db, n):
    return db.add_conversation_message(
        discord_message_id=f"m{n}", channel_id=CHANNEL, role="user",
        content=f"消息-{n}", created_at=datetime.now(timezone.utc).isoformat(),
    )


def test_backfill_only_processes_rows_at_or_before_compact_cursor(db, monkeypatch):
    ids = [_add(db, n) for n in range(6)]
    monkeypatch.setattr(config, "EMBEDDING_ENABLED", True)
    monkeypatch.setattr(config, "EMBEDDING_MODEL", "new-model")
    called = []

    async def fake_embed(database, row_id, channel_id):
        called.append(row_id)
        database.update_conversation_embedding(row_id, [1.0], "ctx", "new-model")
        return True

    monkeypatch.setattr(worker.embeddings, "embed_and_store", fake_embed)

    result = asyncio.run(worker.backfill_compacted_embeddings(db, CHANNEL, ids[3]))

    assert called == ids[:4]
    assert result == {"attempted": 4, "completed": 4, "failed": 0,
                      "upto_id": ids[3]}
    assert db.get_conversation_ids_needing_embedding(
        CHANNEL, upto_id=ids[-1], model="new-model") == ids[4:]


def test_backfill_is_idempotent_and_reembeds_old_models(db, monkeypatch):
    ids = [_add(db, n) for n in range(3)]
    db.update_conversation_embedding(ids[0], [1.0], "ctx", "new-model")
    db.update_conversation_embedding(ids[1], [1.0], "ctx", "old-model")
    monkeypatch.setattr(config, "EMBEDDING_ENABLED", True)
    monkeypatch.setattr(config, "EMBEDDING_MODEL", "new-model")
    called = []

    async def fake_embed(database, row_id, channel_id):
        called.append(row_id)
        database.update_conversation_embedding(row_id, [2.0], "ctx", "new-model")
        return True

    monkeypatch.setattr(worker.embeddings, "embed_and_store", fake_embed)

    asyncio.run(worker.backfill_compacted_embeddings(db, CHANNEL, ids[-1]))
    second = asyncio.run(worker.backfill_compacted_embeddings(db, CHANNEL, ids[-1]))

    assert called == ids[1:]
    assert second["attempted"] == 0


def test_failed_row_waits_for_next_worker_run_without_spinning(db, monkeypatch):
    ids = [_add(db, n) for n in range(2)]
    monkeypatch.setattr(config, "EMBEDDING_ENABLED", True)
    monkeypatch.setattr(config, "EMBEDDING_MODEL", "model")
    attempts = []

    async def fail_first(database, row_id, channel_id):
        attempts.append(row_id)
        if row_id == ids[0]:
            return False
        database.update_conversation_embedding(row_id, [1.0], "ctx", "model")
        return True

    monkeypatch.setattr(worker.embeddings, "embed_and_store", fail_first)

    first = asyncio.run(worker.backfill_compacted_embeddings(db, CHANNEL, ids[-1]))

    assert attempts == ids
    assert first["failed"] == 1
    assert db.get_conversation_ids_needing_embedding(
        CHANNEL, upto_id=ids[-1], model="model") == [ids[0]]


def test_schedule_is_single_flight(db, monkeypatch):
    row_id = _add(db, 1)
    monkeypatch.setattr(config, "EMBEDDING_ENABLED", True)
    gate = asyncio.Event()

    async def slow(database, channel_id, upto_id):
        await gate.wait()
        return {}

    monkeypatch.setattr(worker, "backfill_compacted_embeddings", slow)

    async def scenario():
        assert worker.schedule_compacted_embeddings(db, CHANNEL, row_id)
        assert not worker.schedule_compacted_embeddings(db, CHANNEL, row_id)
        gate.set()
        await next(iter(worker._inflight.values()))

    asyncio.run(scenario())


def test_schedule_clears_legacy_tail_embeddings(db, monkeypatch):
    ids = [_add(db, n) for n in range(4)]
    for row_id in ids:
        db.update_conversation_embedding(row_id, [1.0], "ctx", "model")
    monkeypatch.setattr(config, "EMBEDDING_ENABLED", True)
    monkeypatch.setattr(config, "EMBEDDING_MODEL", "model")

    async def scenario():
        assert worker.schedule_compacted_embeddings(db, CHANNEL, ids[1])
        await next(iter(worker._inflight.values()))

    asyncio.run(scenario())

    assert db.get_conversation_ids_needing_embedding(
        CHANNEL, upto_id=ids[-1], model="model") == ids[2:]


def test_newer_cursor_is_coalesced_while_worker_is_running(db, monkeypatch):
    ids = [_add(db, n) for n in range(4)]
    monkeypatch.setattr(config, "EMBEDDING_ENABLED", True)
    calls = []
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    async def controlled(database, channel_id, upto_id):
        calls.append(upto_id)
        if len(calls) == 1:
            first_started.set()
            await release_first.wait()
        return {}

    monkeypatch.setattr(worker, "backfill_compacted_embeddings", controlled)

    async def scenario():
        assert worker.schedule_compacted_embeddings(db, CHANNEL, ids[1])
        await first_started.wait()
        assert not worker.schedule_compacted_embeddings(db, CHANNEL, ids[3])
        release_first.set()
        await next(iter(worker._inflight.values()))

    asyncio.run(scenario())

    assert calls == [ids[1], ids[3]]
