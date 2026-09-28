"""卡片一句话总结（bot/card_summary.py）。

总结必须是后台预先写好的：Discord autocomplete 要求 3 秒内返回，
打开 /conversation list 的时候来不及现场调模型。
"""
import asyncio

import pytest

import bot.ai_engine_openai_compat as ai_compat
from bot.card_summary import (
    SUMMARY_MAX_CHARS, refresh_card_summary, schedule_summary,
)
from bot.database import Database

CHANNEL = "123"


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "card_summary.db"))


@pytest.fixture
def card(db):
    card_id = db.create_card(
        channel_id=CHANNEL, source_type="check_in", source_id="c1",
        status="active", now="2026-09-27T10:00:00+00:00",
        expires_at="2099-01-01T00:00:00+00:00",
    )
    db.add_conversation_message(
        discord_message_id="1", channel_id=CHANNEL, role="assistant",
        content="今天打算做什么？", created_at="2026-09-27T10:00:00+00:00",
        card_id=card_id,
    )
    db.add_conversation_message(
        discord_message_id="2", channel_id=CHANNEL, role="user",
        content="下午去爬山", created_at="2026-09-27T10:01:00+00:00",
        card_id=card_id,
    )
    return card_id


def _stub_completion(monkeypatch, result):
    """替换掉真实模型调用，返回它收到的 prompt 供断言。"""
    seen = {}

    async def fake(prompt, preset, **kwargs):
        seen["prompt"] = prompt
        seen["kwargs"] = kwargs
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(ai_compat, "simple_completion", fake)
    return seen


def test_summary_is_written_from_the_card_conversation(db, card, monkeypatch):
    seen = _stub_completion(monkeypatch, "下午要去爬山")

    assert asyncio.run(refresh_card_summary(db, card)) is True

    assert db.get_card(card)["summary"] == "下午要去爬山"
    # 两侧的话都要进 prompt，否则总结只反映 AI 的单方面发言
    assert "今天打算做什么？" in seen["prompt"]
    assert "下午去爬山" in seen["prompt"]


def test_empty_card_does_not_call_the_model(db, monkeypatch):
    empty = db.create_card(
        channel_id=CHANNEL, source_type="check_in", source_id="empty",
        status="active", now="2026-09-27T10:00:00+00:00",
        expires_at="2099-01-01T00:00:00+00:00",
    )
    seen = _stub_completion(monkeypatch, "不应该被调用")

    assert asyncio.run(refresh_card_summary(db, empty)) is False
    assert "prompt" not in seen


def test_model_failure_keeps_the_previous_summary(db, card, monkeypatch):
    db.set_card_summary(card, "旧总结")
    _stub_completion(monkeypatch, RuntimeError("上游 429"))

    assert asyncio.run(refresh_card_summary(db, card)) is False
    assert db.get_card(card)["summary"] == "旧总结"


def test_empty_model_output_keeps_the_previous_summary(db, card, monkeypatch):
    db.set_card_summary(card, "旧总结")
    _stub_completion(monkeypatch, "   ")

    assert asyncio.run(refresh_card_summary(db, card)) is False
    assert db.get_card(card)["summary"] == "旧总结"


def test_overlong_summary_is_truncated(db, card, monkeypatch):
    _stub_completion(monkeypatch, "啰" * (SUMMARY_MAX_CHARS + 30))

    assert asyncio.run(refresh_card_summary(db, card)) is True
    assert len(db.get_card(card)["summary"]) == SUMMARY_MAX_CHARS


def test_only_the_first_line_is_kept(db, card, monkeypatch):
    _stub_completion(monkeypatch, "下午要去爬山\n（这是多余的解释）")

    assert asyncio.run(refresh_card_summary(db, card)) is True
    assert db.get_card(card)["summary"] == "下午要去爬山"


def test_schedule_summary_without_an_event_loop_is_a_no_op(db, card):
    """同步调用方（测试、脚本）不该因为没有事件循环就报错。"""
    assert schedule_summary(db, card) is False
    assert schedule_summary(db, None) is False


def test_schedule_summary_does_not_pile_up_parallel_calls(db, card, monkeypatch):
    """同一张卡片连来几条消息时不会并发调模型，但生成期间的新消息要补一次。

    正在跑的那一次读不到后来的消息，如果直接丢掉这个请求，总结就永远停在
    旧快照上——下一条消息到来之前没有任何东西会再触发它。
    """
    calls = []

    async def fake(prompt, preset, **kwargs):
        calls.append(prompt)
        await asyncio.sleep(0.01)
        return "下午要去爬山"

    monkeypatch.setattr(ai_compat, "simple_completion", fake)

    async def scenario():
        first = schedule_summary(db, card)
        second = schedule_summary(db, card)   # 生成期间又来一条
        await asyncio.sleep(0.2)
        return first, second

    first, second = asyncio.run(scenario())

    # 第二次没有并发起任务，但结束之后补跑了一轮
    assert first is True and second is False
    assert len(calls) == 2
    assert db.get_card(card)["summary"] == "下午要去爬山"


def test_schedule_summary_reruns_only_once_per_burst(db, card, monkeypatch):
    """生成期间连来好几条，也只补一次，不会攒成一串调用。"""
    calls = []

    async def fake(prompt, preset, **kwargs):
        calls.append(prompt)
        await asyncio.sleep(0.01)
        return "下午要去爬山"

    monkeypatch.setattr(ai_compat, "simple_completion", fake)

    async def scenario():
        schedule_summary(db, card)
        for _ in range(5):
            schedule_summary(db, card)
        await asyncio.sleep(0.3)

    asyncio.run(scenario())

    assert len(calls) == 2
