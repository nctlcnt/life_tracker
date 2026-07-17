import asyncio
from datetime import datetime, timezone

import bot.memory.service as memory_module
from bot.database import Database
from bot.memory import MarkdownMemoryRepository, MemoryRecallDisabled, MemoryService


def _make_service(tmp_path) -> MemoryService:
    db = Database(str(tmp_path / "memory_test.db"))
    durable = MarkdownMemoryRepository(
        str(tmp_path / "memory.md"), legacy_repository=db
    )
    return MemoryService(db, durable_repository=durable)


def test_durable_memory_crud_preserves_repository_behavior(tmp_path):
    memory = _make_service(tmp_path)

    memory_id = memory.save_durable(
        "喜欢简短回复", memory_type="preference", valid_until="2027-01-01"
    )
    saved = next(item for item in memory.list_durable() if item["id"] == memory_id)

    assert saved["content"] == "喜欢简短回复"
    assert saved["memory_type"] == "preference"

    memory.update_durable(memory_id, content="喜欢先说结论", valid_until=None)
    updated = next(item for item in memory.list_durable() if item["id"] == memory_id)
    assert updated["content"] == "喜欢先说结论"
    assert updated["valid_until"] is None

    memory.delete_durable(memory_id)
    assert all(item["id"] != memory_id for item in memory.list_durable())


def test_ingest_message_exposes_same_recent_context(tmp_path, monkeypatch):
    memory = _make_service(tmp_path)
    monkeypatch.setattr(memory_module.config, "EMBEDDING_ENABLED", False)

    async def ingest():
        await memory.ingest_message(
            discord_message_id="1",
            channel_id="channel-1",
            role="user",
            content="作业已经交了",
            created_at=datetime.now(timezone.utc).isoformat(),
            metadata={"current_content": "[2026-07-16 10:00] 作业已经交了"},
        )

    asyncio.run(ingest())

    assert memory.recent_messages("channel-1", limit=20) == [
        {"role": "user", "content": "[2026-07-16 10:00] 作业已经交了"}
    ]


def test_ingest_message_schedules_embedding_through_service(tmp_path, monkeypatch):
    memory = _make_service(tmp_path)
    called = []
    monkeypatch.setattr(memory_module.config, "EMBEDDING_ENABLED", True)

    async def fake_embed(row_id, channel_id):
        called.append((row_id, channel_id))
        return True

    monkeypatch.setattr(memory, "embed_message", fake_embed)

    async def ingest():
        row_id = await memory.ingest_message(
            discord_message_id="2",
            channel_id="channel-2",
            role="assistant",
            content="收到",
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        await asyncio.gather(*memory._embedding_tasks)
        return row_id

    row_id = asyncio.run(ingest())
    assert called == [(row_id, "channel-2")]


def test_recall_delegates_embedding_and_repository_scan(tmp_path, monkeypatch):
    memory = _make_service(tmp_path)
    captured = {}
    monkeypatch.setattr(memory_module.config, "EMBEDDING_ENABLED", True)
    monkeypatch.setattr(memory_module.config, "EMBEDDING_MODEL", "test-model")
    monkeypatch.setattr(memory_module.config, "EMBEDDING_MIN_RELEVANCE", 0.42)

    async def fake_embed(query):
        captured["query"] = query
        return [1.0, 0.0]

    async def run_inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    def fake_recall(vector, channel_id, **kwargs):
        captured.update(vector=vector, channel_id=channel_id, kwargs=kwargs)
        return [{"content": "命中的旧对话", "relevance": 0.9}]

    monkeypatch.setattr(memory_module.embeddings, "embed_text", fake_embed)
    monkeypatch.setattr(memory_module.asyncio, "to_thread", run_inline)
    monkeypatch.setattr(
        memory.repository, "get_relevant_conversation_snippets", fake_recall
    )

    result = asyncio.run(memory.recall("提交作业", "channel-3", limit=3))

    assert result[0]["content"] == "命中的旧对话"
    assert captured == {
        "query": "提交作业",
        "vector": [1.0, 0.0],
        "channel_id": "channel-3",
        "kwargs": {
            "model": "test-model",
            "limit": 3,
            "exclude_recent": 20,
            "min_relevance": 0.42,
        },
    }


def test_recall_reports_disabled_embedding(tmp_path, monkeypatch):
    memory = _make_service(tmp_path)
    monkeypatch.setattr(memory_module.config, "EMBEDDING_ENABLED", False)

    try:
        asyncio.run(memory.recall("anything", "channel-4"))
    except MemoryRecallDisabled:
        pass
    else:
        raise AssertionError("disabled recall should be explicit to tool callers")
