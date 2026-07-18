import asyncio
import json
from datetime import datetime, timezone

import pytest

from bot.database import Database
from bot.ai_engine_base import simple_completion
from bot import trace
from config import Preset
from bot.memory.curator import (
    build_curator_prompt,
    load_curator_interval,
    parse_curator_batch,
)
from bot.memory.personal_repository import PersonalMemoryRepository


CHANNEL = "channel-1"
CURATOR = "memory-curator-v1"


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "curator.db"))


@pytest.fixture
def repository(db):
    return PersonalMemoryRepository(db)


def _message(db, n, *, channel=CHANNEL, role="user", content=None):
    return db.add_conversation_message(
        discord_message_id=f"{channel}-{n}",
        channel_id=channel,
        role=role,
        content=content or f"原话-{n}",
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def _run(db, run_id="curator-run", batch=None):
    final_text = json.dumps(
        (batch or parse_curator_batch({"operations": []})).to_dict(),
        ensure_ascii=False,
    )
    db.save_ai_run(
        run_id=run_id,
        trigger="curator",
        model="curator-model",
        provider="relay",
        started_at="2026-07-17T00:00:00",
        finished_at="2026-07-17T00:00:01",
        status="success",
        error=None,
        final_text=final_text,
        tool_calls=[],
    )
    return run_id


def _create_batch(message_id, **overrides):
    operation = {
        "action": "create",
        "summary": "用户喜欢简短回复",
        "memory_type": "preference",
        "reason": "用户明确表达稳定偏好",
        "sources": [{
            "message_id": message_id,
            "quote": "回复短一点",
            "evidence_role": "supports",
        }],
    }
    operation.update(overrides)
    return parse_curator_batch({"operations": [operation]})


def _apply(repository, batch, *, upto, visible, run_id="curator-run", after=0):
    return repository.apply_curator_batch(
        batch,
        curator_name=CURATOR,
        channel_id=CHANNEL,
        after_message_id=after,
        upto_message_id=upto,
        visible_message_ids=set(visible),
        run_id=run_id,
        curator_model="curator-model",
    )


def test_parser_rejects_markdown_unknown_fields_and_invalid_shapes():
    with pytest.raises(ValueError, match="not valid JSON"):
        parse_curator_batch("```json\n{\"operations\":[]}\n```")
    with pytest.raises(ValueError, match="unknown fields"):
        parse_curator_batch({"operations": [], "comment": "looks good"})
    with pytest.raises(ValueError, match="no content changes"):
        parse_curator_batch({"operations": [{
            "action": "update",
            "memory_id": 1,
            "reason": "changed",
            "sources": [{"message_id": 1, "evidence_role": "supports"}],
        }]})
    with pytest.raises(ValueError, match="memory_type is invalid"):
        _create_batch(1, memory_type="invented")


def test_prompt_and_interval_expose_stable_evidence_ids(db):
    first = _message(db, 1, content="第一句话")
    second = _message(db, 2, role="assistant", content="第二句话")

    messages = load_curator_interval(
        db, channel_id=CHANNEL, after_message_id=0, limit=10)
    prompt = build_curator_prompt(
        messages=messages, memories=[], now="2026-07-17T00:00:00+00:00")

    assert [message["id"] for message in messages] == [first, second]
    assert f'"message_id":{first}' in prompt
    assert "第一句话" in prompt
    assert '"created_at":' in prompt
    assert "当前 UTC 时间是 2026-07-17T00:00:00+00:00" in prompt


def test_apply_create_and_cursor_are_one_transaction(repository, db):
    message_id = _message(db, 1, content="以后回复短一点")
    batch = _create_batch(message_id)
    run_id = _run(db, batch=batch)

    result = _apply(
        repository, batch, upto=message_id, visible={message_id}, run_id=run_id)

    assert result["applied"] is True
    memory = repository.get(result["created_ids"][0])
    assert memory["summary"] == "用户喜欢简短回复"
    assert memory["source_message_ids"] == [message_id]
    cursor = repository.get_cursor(CURATOR, CHANNEL)
    assert cursor["last_message_id"] == message_id
    assert cursor["last_successful_run_id"] == run_id


def test_invalid_evidence_is_rejected_without_writes(repository, db):
    message_id = _message(db, 1, content="以后回复短一点")
    other_id = _message(db, 2, channel="other", content="回复短一点")
    batch = _create_batch(other_id)
    run_id = _run(db, batch=batch)

    with pytest.raises(ValueError, match="outside visible interval"):
        _apply(
            repository, batch, upto=message_id,
            visible={message_id}, run_id=run_id)

    assert repository.list() == []
    assert repository.get_cursor(CURATOR, CHANNEL)["last_message_id"] == 0


def test_mismatched_quote_is_rejected(repository, db):
    message_id = _message(db, 1, content="以后回复详细一点")
    batch = _create_batch(message_id)
    run_id = _run(db, batch=batch)

    with pytest.raises(ValueError, match="quote does not match"):
        _apply(
            repository, batch, upto=message_id,
            visible={message_id}, run_id=run_id)


def test_duplicate_batch_is_idempotent(repository, db):
    message_id = _message(db, 1, content="以后回复短一点")
    batch = _create_batch(message_id)
    run_id = _run(db, batch=batch)
    first = _apply(
        repository, batch, upto=message_id, visible={message_id}, run_id=run_id)

    second = _apply(
        repository, batch, upto=message_id, visible={message_id}, run_id=run_id)

    assert first["applied"] is True
    assert second["duplicate"] is True
    assert len(repository.list()) == 1


def test_late_failure_rolls_back_prior_operations_and_cursor(repository, db):
    message_id = _message(db, 1, content="以后回复短一点")
    payload = _create_batch(message_id).to_dict()
    payload["operations"].append({
        "action": "archive",
        "memory_id": 9999,
        "reason": "测试事务回滚",
        "sources": [{
            "message_id": message_id,
            "quote": "回复短一点",
            "evidence_role": "supports",
        }],
    })
    batch = parse_curator_batch(json.dumps(payload, ensure_ascii=False))
    run_id = _run(db, batch=batch)

    with pytest.raises(ValueError, match="unknown memory_id"):
        _apply(
            repository, batch, upto=message_id,
            visible={message_id}, run_id=run_id)

    assert repository.list() == []
    assert repository.get_cursor(CURATOR, CHANNEL)["last_message_id"] == 0


def test_empty_batch_advances_cursor_and_cursor_cannot_skip(repository, db):
    first = _message(db, 1, content="普通闲聊")
    second = _message(db, 2, content="仍然是普通闲聊")
    empty = parse_curator_batch({"operations": []})
    run_id = _run(db, batch=empty)

    result = _apply(
        repository, empty, upto=second, visible={first, second}, run_id=run_id)
    assert result["applied"] is True
    assert repository.get_cursor(CURATOR, CHANNEL)["last_message_id"] == second

    third = _message(db, 3, content="新消息")
    _run(db, "curator-run-2", batch=empty)
    with pytest.raises(ValueError, match="cursor changed"):
        _apply(
            repository, empty, after=0, upto=third, visible={third},
            run_id="curator-run-2")


def test_visible_interval_cannot_omit_messages(repository, db):
    first = _message(db, 1, content="第一条")
    second = _message(db, 2, content="第二条")
    empty = parse_curator_batch({"operations": []})
    run_id = _run(db, batch=empty)

    with pytest.raises(ValueError, match="visible interval mismatch"):
        _apply(
            repository, empty, upto=second, visible={second}, run_id=run_id)

    assert repository.get_cursor(CURATOR, CHANNEL)["last_message_id"] == 0


def test_empty_visible_interval_cannot_advance_to_unknown_id(repository, db):
    empty = parse_curator_batch({"operations": []})
    run_id = _run(db, batch=empty)

    with pytest.raises(ValueError, match="last visible message id"):
        _apply(repository, empty, upto=999, visible=set(), run_id=run_id)

    assert repository.get_cursor(CURATOR, CHANNEL)["last_message_id"] == 0


def test_apply_requires_successful_curator_trace(repository, db):
    message_id = _message(db, 1, content="以后回复短一点")
    batch = _create_batch(message_id)
    db.save_ai_run(
        run_id="oneshot-run",
        trigger="oneshot",
        model="curator-model",
        provider="relay",
        started_at="2026-07-17T00:00:00",
        finished_at="2026-07-17T00:00:01",
        status="success",
        error=None,
        final_text="{}",
        tool_calls=[],
    )

    with pytest.raises(ValueError, match="not a successful curator run"):
        _apply(
            repository, batch, upto=message_id,
            visible={message_id}, run_id="oneshot-run")


def test_apply_rejects_proposal_modified_after_trace(repository, db):
    message_id = _message(db, 1, content="以后回复短一点")
    traced = _create_batch(message_id)
    modified = _create_batch(message_id, summary="被修改过的提案")
    run_id = _run(db, batch=traced)

    with pytest.raises(ValueError, match="does not match"):
        _apply(
            repository, modified, upto=message_id,
            visible={message_id}, run_id=run_id)

    assert repository.list() == []
    assert repository.get_cursor(CURATOR, CHANNEL)["last_message_id"] == 0


def test_curator_completion_persists_trace_and_returns_run_id(db, monkeypatch, tmp_path):
    monkeypatch.setattr(trace, "_TRACE_DIR", tmp_path / "traces")
    preset = Preset(
        name="curator-test",
        provider="relay",
        api_key="test",
        base_url="https://example.invalid",
        model="curator-model",
    )

    async def fake_call(call_db, prompt, messages, **kwargs):
        assert call_db is None
        assert kwargs["tool_names"] == set()
        return '{"operations":[]}'

    raw, run_id = asyncio.run(simple_completion(
        "curate", fake_call, preset,
        trigger="curator", db=db, return_run_id=True,
    ))

    conn = db._get_conn()
    row = conn.execute("SELECT * FROM ai_runs WHERE id = ?", (run_id,)).fetchone()
    conn.close()
    assert raw == '{"operations":[]}'
    assert row["trigger"] == "curator"
    assert row["status"] == "success"
    assert row["final_text"] == raw
