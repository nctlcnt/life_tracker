import sqlite3
from datetime import datetime, timezone

import pytest

from bot.database import Database
from bot.memory.personal_repository import PersonalMemoryRepository


CHANNEL = "channel-1"


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "personal_memory.db"))


@pytest.fixture
def repository(db):
    return PersonalMemoryRepository(db)


def _message(db, n, *, channel=CHANNEL):
    return db.add_conversation_message(
        discord_message_id=f"{channel}-{n}", channel_id=channel,
        role="user", content=f"原话-{n}",
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def _create(repository, message_ids, **overrides):
    fields = {
        "channel_id": CHANNEL,
        "summary": "用户喜欢简短回复",
        "reason": "稳定的交互偏好",
        "memory_type": "preference",
        "curator_model": "curator-model",
        "sources": [
            {"conversation_message_id": message_id, "quote": f"证据-{message_id}"}
            for message_id in message_ids
        ],
    }
    fields.update(overrides)
    return repository.create(**fields)


def test_schema_is_created_without_reusing_legacy_memories(db):
    conn = db._get_conn()
    tables = {
        row["name"] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    conn.close()

    assert {"memories", "personal_memories", "personal_memory_sources",
            "curator_cursors"}.issubset(tables)


def test_existing_database_upgrade_preserves_legacy_memory(tmp_path):
    path = tmp_path / "existing.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT NOT NULL, "
        "created_at TEXT, source TEXT, memory_type TEXT, valid_until TEXT)"
    )
    conn.execute(
        "INSERT INTO memories (id, content, source) VALUES (7, '旧记忆', 'ai')"
    )
    conn.commit()
    conn.close()

    upgraded = Database(str(path))

    conn = upgraded._get_conn()
    legacy = conn.execute("SELECT id, content FROM memories").fetchall()
    new_count = conn.execute("SELECT count(*) AS n FROM personal_memories").fetchone()["n"]
    conn.close()
    assert [(row["id"], row["content"]) for row in legacy] == [(7, "旧记忆")]
    assert new_count == 0


def test_create_roundtrips_normalized_sources(repository, db):
    ids = [_message(db, 1), _message(db, 2)]

    memory_id = _create(repository, ids, embedding=[1.0, 2.0],
                        embedding_model="embedding-model")
    memory = repository.get(memory_id)

    assert memory["summary"] == "用户喜欢简短回复"
    # 新建记忆默认落在阶梯最低档：证据还没被汇总、status 还没被真正算出来
    # 之前，它只应拥有最低权限。
    assert memory["status"] == "hypothesis"
    assert memory["embedding"] == [1.0, 2.0]
    assert memory["source_message_ids"] == ids
    assert [source["quote"] for source in memory["sources"]] == [
        f"证据-{ids[0]}", f"证据-{ids[1]}"
    ]


def test_create_rejects_cross_channel_or_missing_evidence_atomically(repository, db):
    valid = _message(db, 1)
    other = _message(db, 2, channel="other-channel")

    with pytest.raises(ValueError, match="unknown source_message_ids"):
        _create(repository, [valid, other])

    assert repository.list() == []


def test_create_requires_evidence_and_valid_role(repository, db):
    message_id = _message(db, 1)
    with pytest.raises(ValueError, match="at least one source"):
        _create(repository, [], sources=[])
    with pytest.raises(ValueError, match="invalid evidence_role"):
        _create(repository, [message_id], sources=[{
            "conversation_message_id": message_id,
            "evidence_role": "invented",
        }])


def test_list_filters_status_and_type(repository, db):
    first = _create(repository, [_message(db, 1)])
    second = _create(
        repository, [_message(db, 2)], summary="当前课程计划",
        memory_type="plan",
    )
    # 独立的 archive 状态已取消：归档的语义是「不再成立但没有替代 claim」，
    # 在新阶梯里就是 disputed。
    assert repository.set_status(first, "disputed")

    assert [item["id"] for item in repository.list(status="hypothesis")] == [second]
    assert [item["id"] for item in repository.list(memory_type="preference")] == [first]


def test_summary_edit_invalidates_embedding(repository, db):
    memory_id = _create(
        repository, [_message(db, 1)], embedding=[1.0],
        embedding_model="old-model",
    )

    assert repository.update_content(memory_id, summary="更新后的偏好")
    memory = repository.get(memory_id)

    assert memory["summary"] == "更新后的偏好"
    assert memory["embedding"] is None
    assert memory["embedding_model"] is None


def test_supersede_requires_another_existing_memory(repository, db):
    old = _create(repository, [_message(db, 1)])
    replacement = _create(repository, [_message(db, 2)], summary="新的偏好")

    with pytest.raises(ValueError, match="another memory"):
        repository.set_status(old, "superseded", superseded_by=old)
    with pytest.raises(sqlite3.IntegrityError):
        repository.set_status(old, "superseded", superseded_by=9999)

    assert repository.set_status(old, "superseded", superseded_by=replacement)
    memory = repository.get(old)
    assert memory["status"] == "superseded"
    assert memory["superseded_by"] == replacement


def test_cursor_is_channel_scoped_and_monotonic(repository):
    assert repository.get_cursor("memory-curator", CHANNEL)["last_message_id"] == 0
    assert repository.advance_cursor("memory-curator", CHANNEL, 10)
    assert not repository.advance_cursor("memory-curator", CHANNEL, 9)
    assert repository.get_cursor("memory-curator", CHANNEL)["last_message_id"] == 10
    assert repository.get_cursor("memory-curator", "other")["last_message_id"] == 0
