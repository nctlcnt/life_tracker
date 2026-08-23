"""哪些记忆还能被 curator 看见、被操作命中——单表状态阶梯改造的保护网。

改造前 `status` 只有 active / superseded / archived，"可操作"等价于 `active`；
改造后有五个取值（hypothesis / provisional / confirmed / disputed / superseded），
**除 `superseded` 之外全部仍然可被操作命中**，因为低状态记忆照样存在，只是注入
权限受限。

这组测试刻意不写死状态字符串，只用两个 repository 辅助函数表达意图：
「刚创建、还没被替代的记忆」和「已被替代的记忆」。因此改造把三值换成五值时，
它们仍然测同一件事，并且能抓住两个最容易犯的错：

1. 喂给模型的「已有记忆」清单如果继续按 `active` 过滤，改造后会变成空集，
   curator 看不见自己写过的低状态记忆，下一批会重复 create 一遍；
2. 「可被操作命中」如果被误收窄成只剩 `confirmed`，低状态记忆就再也无法补证据，
   永远升不上去。
"""
import asyncio
import json
from datetime import datetime, timezone

import pytest

from bot.database import Database
from bot.memory import curator_service
from bot.memory.personal_repository import PersonalMemoryRepository
from config import Preset

CHANNEL = "channel-1"


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "targetable.db"))


@pytest.fixture
def repository(db):
    return PersonalMemoryRepository(db)


@pytest.fixture
def preset():
    return Preset(name="fake", provider="relay", model="fake-model",
                  api_key="k", base_url="http://example.invalid")


def _message(db, n, *, content=None):
    return db.add_conversation_message(
        discord_message_id=f"{CHANNEL}-{n}", channel_id=CHANNEL, role="user",
        content=content or f"原话-{n}",
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def _fresh_memory(repository, db, n, *, summary="用户喜欢简短回复"):
    """建一条刚写入、尚未被替代的记忆——它必须始终可被 curator 看见和命中。"""
    message_id = _message(db, n)
    return repository.create(
        channel_id=CHANNEL, summary=summary, reason="稳定偏好",
        memory_type="preference", curator_model="curator-model",
        sources=[{"conversation_message_id": message_id, "quote": f"原话-{n}"}],
    )


def _fake_completion(db, output: str, calls: list):
    async def fake(prompt, preset, *, trigger="oneshot", db_=None, **kwargs):
        calls.append(prompt)
        db.save_ai_run(
            run_id="run-0", trigger="curator", model=preset.model,
            provider=preset.provider, started_at="t", finished_at="t",
            status="success", error=None, final_text=output, tool_calls=[])
        return output, "run-0"
    return fake


def test_curator_sees_existing_memories_in_its_task_prompt(
        db, repository, preset, monkeypatch):
    """喂给模型的「已有记忆」清单必须包含刚写入的记忆。

    这是查重的前提：curator 靠这份清单判断「这条已经有了，不要再 create」。
    清单一旦变空，同一个事实每批都会被重新创建一遍。
    """
    memory_id = _fresh_memory(repository, db, 1, summary="用户喜欢简短回复")
    _message(db, 2, content="今天天气不错")
    calls = []
    monkeypatch.setattr("bot.ai_engine_openai_compat.simple_completion",
                        _fake_completion(db, '{"operations":[]}', calls))

    asyncio.run(curator_service.propose_batch(
        db, repository, channel_id=CHANNEL, preset=preset))

    task_prompt = calls[0]
    assert "用户喜欢简短回复" in task_prompt
    assert f'"memory_id":{memory_id}' in task_prompt


def test_superseded_memory_disappears_from_the_prompt(
        db, repository, preset, monkeypatch):
    """被替代的记忆不再进入清单——它已经不是当前说法，不该参与查重。"""
    old_id = _fresh_memory(repository, db, 1, summary="用户住在墨尔本")
    new_id = _fresh_memory(repository, db, 2, summary="用户住在悉尼")
    repository.set_status(old_id, "superseded", superseded_by=new_id)
    _message(db, 3, content="随口一句")
    calls = []
    monkeypatch.setattr("bot.ai_engine_openai_compat.simple_completion",
                        _fake_completion(db, '{"operations":[]}', calls))

    asyncio.run(curator_service.propose_batch(
        db, repository, channel_id=CHANNEL, preset=preset))

    task_prompt = calls[0]
    assert "用户住在悉尼" in task_prompt
    assert "用户住在墨尔本" not in task_prompt


def test_operation_can_target_a_fresh_memory(db, repository):
    """刚写入、未被替代的记忆必须能被操作命中（否则它永远无法补证据、升状态）。"""
    memory_id = _fresh_memory(repository, db, 1)
    conn = db._get_conn()
    try:
        item = repository._require_targetable_memory(conn, memory_id)
    finally:
        conn.close()
    assert item["id"] == memory_id


def test_operation_cannot_target_a_superseded_memory(db, repository):
    """被替代的记忆是唯一不可再被命中的——它的位置已经让给新条目了。"""
    old_id = _fresh_memory(repository, db, 1)
    new_id = _fresh_memory(repository, db, 2, summary="用户改口了")
    repository.set_status(old_id, "superseded", superseded_by=new_id)

    conn = db._get_conn()
    try:
        with pytest.raises(ValueError, match="superseded"):
            repository._require_targetable_memory(conn, old_id)
    finally:
        conn.close()
