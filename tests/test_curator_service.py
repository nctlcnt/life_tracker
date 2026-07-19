"""curator_service 共享管线的行为契约：修复轮、auto_apply、无消息路径。

模型调用用假 simple_completion（同时负责写 ai_runs 行，模拟 trace 落库）。
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

VALID_OUTPUT = json.dumps({"operations": [{
    "action": "create",
    "summary": "用户喜欢简短回复",
    "memory_type": "preference",
    "reason": "用户明确表达稳定偏好",
    "sources": [{
        "message_id": None,  # 由测试注入
        "quote": "回复短一点",
        "evidence_role": "supports",
    }],
}]}, ensure_ascii=False)


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "service.db"))


@pytest.fixture
def repository(db):
    return PersonalMemoryRepository(db)


@pytest.fixture
def preset():
    return Preset(name="fake", provider="relay", model="fake-model",
                  api_key="k", base_url="http://example.invalid")


def _message(db, n, *, content=None):
    return db.add_conversation_message(
        discord_message_id=f"{CHANNEL}-{n}",
        channel_id=CHANNEL,
        role="user",
        content=content or f"原话-{n}",
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def _valid_output(message_id: int) -> str:
    payload = json.loads(VALID_OUTPUT)
    payload["operations"][0]["sources"][0]["message_id"] = message_id
    return json.dumps(payload, ensure_ascii=False)


def _fake_completion(db, outputs: list[str], calls: list | None = None):
    """依序返回 outputs；每次调用写一条 curator ai_runs（模拟 trace 落库）。"""
    counter = {"n": 0}

    async def fake(prompt, preset, *, trigger="oneshot", db_=None, **kwargs):
        index = counter["n"]
        counter["n"] += 1
        if calls is not None:
            calls.append(prompt)
        run_id = f"fake-run-{index}"
        db.save_ai_run(
            run_id=run_id, trigger="curator", model=preset.model,
            provider=preset.provider, started_at="t", finished_at="t",
            status="success", error=None,
            final_text=outputs[min(index, len(outputs) - 1)], tool_calls=[])
        return outputs[min(index, len(outputs) - 1)], run_id

    return fake


def test_repair_round_recovers_fenced_output(db, repository, preset, monkeypatch):
    message_id = _message(db, 1, content="以后回复短一点")
    fenced = "```json\n" + _valid_output(message_id) + "\n```"
    calls = []
    monkeypatch.setattr(
        "bot.ai_engine_openai_compat.simple_completion",
        _fake_completion(db, [fenced, _valid_output(message_id)], calls))

    result = asyncio.run(curator_service.propose_batch(
        db, repository, channel_id=CHANNEL, preset=preset))

    assert result["status"] == "validated"
    assert result["repaired"] is True
    assert len(calls) == 2
    # 修复 prompt 必须携带错误、失败输出与原始任务（quote 校验依赖原文）
    assert "校验错误" in calls[1] and "```json" in calls[1]
    assert "以后回复短一点" in calls[1]
    # dry-run 不落库
    assert repository.list(status="active") == []
    assert repository.get_cursor("memory-curator-v1", CHANNEL)["last_message_id"] == 0


def test_repair_disabled_or_still_failing_raises(db, repository, preset, monkeypatch):
    message_id = _message(db, 1, content="以后回复短一点")
    fenced = "```json\n" + _valid_output(message_id) + "\n```"

    monkeypatch.setattr(
        "bot.ai_engine_openai_compat.simple_completion",
        _fake_completion(db, [fenced, fenced]))
    with pytest.raises(ValueError, match="not valid JSON"):
        asyncio.run(curator_service.propose_batch(
            db, repository, channel_id=CHANNEL, preset=preset))

    monkeypatch.setattr(
        "bot.ai_engine_openai_compat.simple_completion",
        _fake_completion(db, [fenced]))
    with pytest.raises(ValueError, match="not valid JSON"):
        asyncio.run(curator_service.propose_batch(
            db, repository, channel_id=CHANNEL, preset=preset, repair=False))


def test_auto_apply_persists_and_advances_cursor(db, repository, preset, monkeypatch):
    message_id = _message(db, 1, content="以后回复短一点")
    monkeypatch.setattr(
        "bot.ai_engine_openai_compat.simple_completion",
        _fake_completion(db, [_valid_output(message_id)]))

    result = asyncio.run(curator_service.propose_batch(
        db, repository, channel_id=CHANNEL, preset=preset, auto_apply=True))

    assert result["mode"] == "auto-apply"
    assert result["apply_result"]["applied"] is True
    memories = repository.list(status="active")
    assert len(memories) == 1 and memories[0]["summary"] == "用户喜欢简短回复"
    cursor = repository.get_cursor("memory-curator-v1", CHANNEL)
    assert cursor["last_message_id"] == message_id


def test_auto_apply_empty_batch_still_advances_cursor(db, repository, preset, monkeypatch):
    message_id = _message(db, 1)
    monkeypatch.setattr(
        "bot.ai_engine_openai_compat.simple_completion",
        _fake_completion(db, ['{"operations":[]}']))

    result = asyncio.run(curator_service.propose_batch(
        db, repository, channel_id=CHANNEL, preset=preset, auto_apply=True))

    assert result["apply_result"]["applied"] is True
    assert repository.list(status="active") == []
    assert repository.get_cursor(
        "memory-curator-v1", CHANNEL)["last_message_id"] == message_id


def test_no_messages_short_circuits_without_model_call(db, repository, preset, monkeypatch):
    called = []
    monkeypatch.setattr(
        "bot.ai_engine_openai_compat.simple_completion",
        _fake_completion(db, ["should-not-run"], called))

    result = asyncio.run(curator_service.propose_batch(
        db, repository, channel_id=CHANNEL, preset=preset))

    assert result["status"] == "no_messages"
    assert called == []
