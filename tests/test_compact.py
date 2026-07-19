"""LT-135 compact worker（bot/memory/compact.py）的行为契约。

摘要生成用假 simple_completion，不打真实端点。
"""
import asyncio
import re

import pytest

import config
from config import Preset
from bot.database import Database
import bot.memory.compact as compact
from bot.memory.compact import (
    COMPACT_PRESET_STATE_KEY, SUMMARY_TARGET_TOKENS,
    SUMMARY_SECTIONS, SUMMARY_TITLE,
    build_compact_prompt, get_compact_preset, get_compact_preset_name,
    rebuild_compact_summary, run_compact, schedule_compact, set_compact_preset,
)
from bot.memory.context_window import (
    assemble_window, load_summary_state, save_summary_state,
)

CHANNEL = "chan-1"


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "compact_test.db"))


@pytest.fixture(autouse=True)
def _reset_worker_state(monkeypatch):
    compact._inflight.clear()
    compact._last_attempt.clear()
    # Compact contract tests do not call the external embedding endpoint; the
    # worker itself has a dedicated test module.
    monkeypatch.setattr(
        "bot.memory.history_embedding.schedule_compacted_embeddings",
        lambda *args, **kwargs: False,
    )
    yield
    compact._inflight.clear()
    compact._last_attempt.clear()


def _add(db, role, content, n):
    metadata = {"current_content": f"[2026-07-17 10:00] {content}"} if role == "user" else None
    return db.add_conversation_message(
        discord_message_id=f"m{n}", channel_id=CHANNEL, role=role,
        content=content, created_at="2026-07-16T23:30:00+00:00",
        metadata=metadata,
    )


def _render_summary(prompt, content):
    generated_at = re.search(r"当前时间：([^\n]+)", prompt).group(1)
    timezone_name = re.search(r"时区：([^\n]+)", prompt).group(1)
    parts = [f"{SUMMARY_TITLE}\n> 生成基准：{generated_at} ({timezone_name})"]
    for index, heading in enumerate(SUMMARY_SECTIONS):
        body = f"- {content}" if index == 2 else "- 无"
        parts.append(f"{heading}\n{body}")
    return "\n\n".join(parts)


def _fake_completion(reply="聊了考试和午饭。", calls=None, *, raw=False):
    async def fake(prompt, preset, **kwargs):
        if calls is not None:
            calls.append({"prompt": prompt, "preset": preset})
        return reply if raw else _render_summary(prompt, reply)
    return fake


# ── compact preset（admin 可设） ─────────────────────────────────────────

def test_compact_preset_defaults_to_active(db):
    assert get_compact_preset_name(db) is None
    assert get_compact_preset(db) is config.get_active()


def test_compact_preset_set_get_clear(db):
    some_name = next(iter(config.PRESETS))
    set_compact_preset(db, some_name)
    assert get_compact_preset_name(db) == some_name
    assert get_compact_preset(db) is config.PRESETS[some_name]

    set_compact_preset(db, None)
    assert get_compact_preset_name(db) is None


def test_compact_preset_rejects_unknown(db):
    with pytest.raises(ValueError):
        set_compact_preset(db, "no-such-preset")


def test_stale_configured_preset_falls_back_to_active(db):
    db.set_state(COMPACT_PRESET_STATE_KEY, "deleted-preset")
    assert get_compact_preset_name(db) is None
    assert get_compact_preset(db) is config.get_active()


def test_compact_prompt_anchors_time_and_requires_fixed_template():
    prompt = build_compact_prompt(
        "旧摘要写着下周取货",
        [{
            "id": 7,
            "role": "user",
            "content": "明天去学校",
            "created_at": "2026-07-16T23:30:00+00:00",
        }],
        generated_at="2026-07-18 10:00:00",
        timezone_name="Australia/Sydney",
    )

    assert "当前时间：2026-07-18 10:00:00" in prompt
    assert "时区：Australia/Sydney" in prompt
    assert "message_id=7 created_at=2026-07-16T23:30:00+00:00" in prompt
    assert "无明确锚点的时间表达" in prompt
    assert "无法确定时写“日期不确定”" in prompt
    assert prompt.index(SUMMARY_SECTIONS[0]) < prompt.index(SUMMARY_SECTIONS[-1])


# ── run_compact ──────────────────────────────────────────────────────────

def test_run_compact_folds_and_persists(db, monkeypatch):
    ids = [_add(db, "user", f"消息{i}", i) for i in range(5)]
    calls = []
    monkeypatch.setattr(
        "bot.ai_engine_openai_compat.simple_completion",
        _fake_completion("摘要：聊了 0-2。", calls))

    ok = asyncio.run(run_compact(db, CHANNEL, fold_upto_id=ids[2]))

    assert ok
    state = load_summary_state(db, CHANNEL)
    assert "摘要：聊了 0-2。" in state["summary"]
    assert state["upto_message_id"] == ids[2]
    assert state["model"] == config.get_active().model
    # 只折叠 fold_upto 及更早的消息
    prompt = calls[0]["prompt"]
    assert "消息2" in prompt and "消息3" not in prompt
    assert "created_at=2026-07-16T23:30:00+00:00" in prompt


def test_run_compact_includes_continuous_prefix_beyond_thousand_rows(db, monkeypatch):
    ids = [_add(db, "user", f"消息-{i}", i) for i in range(1005)]
    calls = []
    monkeypatch.setattr(
        "bot.ai_engine_openai_compat.simple_completion",
        _fake_completion("完整摘要", calls),
    )

    ok = asyncio.run(run_compact(db, CHANNEL, fold_upto_id=ids[-2]))

    assert ok
    prompt = calls[0]["prompt"]
    assert "消息-0" in prompt
    assert "消息-1003" in prompt
    assert "消息-1004" not in prompt
    assert load_summary_state(db, CHANNEL)["upto_message_id"] == ids[-2]


def test_explicit_message_page_starts_at_oldest_unprocessed_row(db):
    ids = [_add(db, "user", f"消息-{i}", i) for i in range(6)]

    page = db.get_ai_messages_after(CHANNEL, ids[0], limit=2)

    assert [item["id"] for item in page] == ids[1:3]


def test_second_compact_feeds_old_summary(db, monkeypatch):
    ids = [_add(db, "user", f"消息{i}", i) for i in range(6)]
    save_summary_state(db, CHANNEL, summary="旧摘要内容",
                       upto_message_id=ids[1], model="m", updated_at="t")
    calls = []
    monkeypatch.setattr(
        "bot.ai_engine_openai_compat.simple_completion",
        _fake_completion("新摘要", calls))

    ok = asyncio.run(run_compact(db, CHANNEL, fold_upto_id=ids[4]))

    assert ok
    prompt = calls[0]["prompt"]
    # 旧摘要进 prompt；折叠范围 = (旧upto, fold_upto]
    assert "旧摘要内容" in prompt
    assert "消息2" in prompt and "消息4" in prompt
    assert "消息1" not in prompt and "消息5" not in prompt
    assert load_summary_state(db, CHANNEL)["upto_message_id"] == ids[4]


def test_rebuild_compact_uses_full_history_and_replaces_state_atomically(db, monkeypatch):
    ids = [_add(db, "user", f"消息-{i}", i) for i in range(1005)]
    save_summary_state(db, CHANNEL, summary="旧的不完整摘要",
                       upto_message_id=ids[-2], model="old", updated_at="old")
    calls = []
    monkeypatch.setattr(
        "bot.ai_engine_openai_compat.simple_completion",
        _fake_completion("完整重建摘要", calls),
    )

    ok = asyncio.run(rebuild_compact_summary(db, CHANNEL, ids[-2]))

    assert ok
    prompt = calls[0]["prompt"]
    assert "旧的不完整摘要" not in prompt
    assert "【已有摘要（更早的对话）】" not in prompt
    assert "消息-0" in prompt and "消息-1003" in prompt
    state = load_summary_state(db, CHANNEL)
    assert "完整重建摘要" in state["summary"]
    assert state["upto_message_id"] == ids[-2]


def test_rebuild_failure_preserves_existing_state(db, monkeypatch):
    ids = [_add(db, "user", f"消息-{i}", i) for i in range(3)]
    save_summary_state(db, CHANNEL, summary="仍可用的旧摘要",
                       upto_message_id=ids[1], model="old", updated_at="old")
    before = load_summary_state(db, CHANNEL)

    async def boom(prompt, preset, **kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr("bot.ai_engine_openai_compat.simple_completion", boom)

    assert not asyncio.run(rebuild_compact_summary(db, CHANNEL, ids[1]))
    assert load_summary_state(db, CHANNEL) == before


def test_rebuild_does_not_clobber_state_changed_during_generation(db, monkeypatch):
    ids = [_add(db, "user", f"消息-{i}", i) for i in range(4)]
    save_summary_state(db, CHANNEL, summary="旧摘要",
                       upto_message_id=ids[1], model="old", updated_at="old")

    async def concurrent_update(prompt, preset, **kwargs):
        save_summary_state(db, CHANNEL, summary="并发产生的新摘要",
                           upto_message_id=ids[2], model="new", updated_at="new")
        return "本次重建摘要"

    monkeypatch.setattr(
        "bot.ai_engine_openai_compat.simple_completion", concurrent_update)

    assert not asyncio.run(rebuild_compact_summary(db, CHANNEL, ids[2]))
    assert load_summary_state(db, CHANNEL)["summary"] == "并发产生的新摘要"


def test_run_compact_failure_keeps_state_and_sets_cooldown(db, monkeypatch):
    ids = [_add(db, "user", f"消息{i}", i) for i in range(3)]

    async def boom(prompt, preset, **kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr("bot.ai_engine_openai_compat.simple_completion", boom)

    ok = asyncio.run(run_compact(db, CHANNEL, fold_upto_id=ids[2]))

    assert not ok
    assert load_summary_state(db, CHANNEL) is None
    assert CHANNEL in compact._last_attempt  # 失败进入冷却


def test_run_compact_success_clears_cooldown(db, monkeypatch):
    ids = [_add(db, "user", f"消息{i}", i) for i in range(3)]
    monkeypatch.setattr(
        "bot.ai_engine_openai_compat.simple_completion", _fake_completion())

    assert asyncio.run(run_compact(db, CHANNEL, fold_upto_id=ids[2]))
    assert CHANNEL not in compact._last_attempt


def test_run_compact_stale_fold_is_noop(db, monkeypatch):
    ids = [_add(db, "user", f"消息{i}", i) for i in range(3)]
    save_summary_state(db, CHANNEL, summary="已有", upto_message_id=ids[2],
                       model="m", updated_at="t")

    called = []
    monkeypatch.setattr(
        "bot.ai_engine_openai_compat.simple_completion",
        _fake_completion(calls=called))

    ok = asyncio.run(run_compact(db, CHANNEL, fold_upto_id=ids[1]))
    assert not ok
    assert called == []  # 游标已越过，连模型都不该调


def test_empty_reply_rejected(db, monkeypatch):
    ids = [_add(db, "user", "hi", 1)]
    monkeypatch.setattr(
        "bot.ai_engine_openai_compat.simple_completion", _fake_completion("   ", raw=True))
    assert not asyncio.run(run_compact(db, CHANNEL, fold_upto_id=ids[0]))
    assert load_summary_state(db, CHANNEL) is None


def test_relative_time_or_wrong_template_rejected_without_state_change(db, monkeypatch):
    message_id = _add(db, "user", "明天去学校", 1)
    calls = 0

    async def relative_time(prompt, preset, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _render_summary(prompt, "下周去学校")
        return _render_summary(prompt, "2026-07-21 去学校")

    monkeypatch.setattr(
        "bot.ai_engine_openai_compat.simple_completion", relative_time)
    assert asyncio.run(run_compact(db, CHANNEL, fold_upto_id=message_id))
    assert "2026-07-21 去学校" in load_summary_state(db, CHANNEL)["summary"]
    assert calls == 2


def test_invalid_repair_is_rejected_without_state_change(db, monkeypatch):
    message_id = _add(db, "user", "明天去学校", 1)
    calls = 0

    async def always_invalid(prompt, preset, **kwargs):
        nonlocal calls
        calls += 1
        return _render_summary(prompt, "下周去学校")

    monkeypatch.setattr(
        "bot.ai_engine_openai_compat.simple_completion", always_invalid)
    assert not asyncio.run(run_compact(db, CHANNEL, fold_upto_id=message_id))
    assert load_summary_state(db, CHANNEL) is None
    assert calls == 2


def test_wrong_template_repair_failure_preserves_state(db, monkeypatch):
    message_id = _add(db, "user", "明天去学校", 1)

    monkeypatch.setattr(
        "bot.ai_engine_openai_compat.simple_completion",
        _fake_completion("没有固定模板", raw=True),
    )
    assert not asyncio.run(run_compact(db, CHANNEL, fold_upto_id=message_id))
    assert load_summary_state(db, CHANNEL) is None


def test_oversized_summary_truncated(db, monkeypatch):
    ids = [_add(db, "user", "hi", 1)]
    monkeypatch.setattr(
        "bot.ai_engine_openai_compat.simple_completion",
        _fake_completion("摘" * (SUMMARY_TARGET_TOKENS * 4)))

    assert asyncio.run(run_compact(db, CHANNEL, fold_upto_id=ids[0]))
    from bot.memory import estimate_tokens
    saved = load_summary_state(db, CHANNEL)["summary"]
    assert estimate_tokens(saved) <= SUMMARY_TARGET_TOKENS * 2 + 2


# ── schedule_compact（单飞 + 冷却） ──────────────────────────────────────

def _window_needing_compact(db, monkeypatch, n=10):
    [_add(db, "user", "聊" * 20, i) for i in range(n)]
    monkeypatch.setattr(config, "CONTEXT_COMPACT_THRESHOLD_TOKENS", 300)
    return assemble_window(db, CHANNEL)


def test_schedule_compact_runs_in_background(db, monkeypatch):
    window = _window_needing_compact(db, monkeypatch)
    monkeypatch.setattr(
        "bot.ai_engine_openai_compat.simple_completion", _fake_completion("摘要"))

    async def scenario():
        started = schedule_compact(db, CHANNEL, window)
        assert started
        await compact._inflight[CHANNEL]

    asyncio.run(scenario())
    state = load_summary_state(db, CHANNEL)
    assert "摘要" in state["summary"]
    assert state["upto_message_id"] == window.fold_upto_id


def test_schedule_compact_single_flight(db, monkeypatch):
    window = _window_needing_compact(db, monkeypatch)
    gate = asyncio.Event()

    async def slow(prompt, preset, **kwargs):
        await gate.wait()
        return "摘要"

    monkeypatch.setattr("bot.ai_engine_openai_compat.simple_completion", slow)

    async def scenario():
        assert schedule_compact(db, CHANNEL, window)
        # 第一个还在跑：拒绝并发第二个
        assert not schedule_compact(db, CHANNEL, window)
        gate.set()
        await compact._inflight[CHANNEL]

    asyncio.run(scenario())


def test_schedule_compact_respects_failure_cooldown(db, monkeypatch):
    window = _window_needing_compact(db, monkeypatch)

    async def boom(prompt, preset, **kwargs):
        raise RuntimeError("down")

    monkeypatch.setattr("bot.ai_engine_openai_compat.simple_completion", boom)

    async def scenario():
        assert schedule_compact(db, CHANNEL, window)
        await compact._inflight[CHANNEL]
        # 失败后冷却期内不再起新任务
        assert not schedule_compact(db, CHANNEL, window)

    asyncio.run(scenario())


def test_schedule_compact_noop_without_need(db):
    from bot.memory.context_window import ContextWindow

    async def scenario():
        assert not schedule_compact(db, CHANNEL, ContextWindow())

    asyncio.run(scenario())


def test_schedule_compact_sync_context_skips(db, monkeypatch):
    window = _window_needing_compact(db, monkeypatch)
    # 无事件循环（同步上下文）：安静跳过，不抛异常
    assert not schedule_compact(db, CHANNEL, window)


def test_service_context_window_triggers_compact(db, monkeypatch):
    from bot.memory import MemoryService
    service = MemoryService(db)
    [_add(db, "user", "聊" * 20, i) for i in range(10)]
    monkeypatch.setattr(config, "CONTEXT_COMPACT_THRESHOLD_TOKENS", 300)
    monkeypatch.setattr(
        "bot.ai_engine_openai_compat.simple_completion", _fake_completion("摘要"))

    async def scenario():
        window = service.context_window(CHANNEL)
        assert window.needs_compact
        task = compact._inflight.get(CHANNEL)
        assert task is not None
        await task

    asyncio.run(scenario())
    assert "摘要" in load_summary_state(db, CHANNEL)["summary"]
