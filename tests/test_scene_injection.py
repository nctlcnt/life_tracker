"""场景状态进入 prompt、并由下一个 check-in 终止的契约。"""
import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from config import Preset
from bot import ai_engine_base
from bot.database import Database
from bot.memory import scene_state
from bot.prompts import PromptParts


CHANNEL = "channel-1"
T0 = datetime(2026, 8, 24, 20, 0, 0)


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "inject.db"))


def _prompt():
    return PromptParts(
        mode="chat",
        template="人设正文。\n\n{memories}\n\n{deadlines}",
        values={"memories": "【记忆】- 喜欢编程", "deadlines": "【Deadline】- 无"},
    )


def test_suffix_lands_at_the_very_end(db):
    """动态场景必须追加到末尾，避免打掉前面的稳定 cache 前缀。"""
    text = _prompt().with_suffix("【当前场景】晚间采访").flatten()

    assert text.rstrip().endswith("【当前场景】晚间采访")
    assert text.index("人设正文") < text.index("【当前场景】")


def test_suffix_does_not_mutate_the_original(db):
    original = _prompt()
    original.with_suffix("【当前场景】晚间采访")

    assert "当前场景" not in original.flatten()


def test_empty_suffix_is_a_noop(db):
    original = _prompt()
    assert original.with_suffix("") is original
    assert original.with_suffix("   \n ") is original


def test_suffix_survives_concise(db):
    """工具轮改发 concise() 时仍应知道当前场景。"""
    p = _prompt().with_suffix("【当前场景】晚间采访")
    assert "【当前场景】晚间采访" in p.concise().flatten()


def test_no_scene_means_no_suffix(db):
    assert scene_state.load(db, CHANNEL, T0) is None

    scene = scene_state.load(db, CHANNEL, T0)
    text = (
        _prompt().flatten()
        if scene is None
        else _prompt().with_suffix(scene.as_prompt_block()).flatten()
    )

    assert "当前场景" not in text


def test_chat_reads_scene_without_mutating_it(db, monkeypatch):
    """普通对话应注入场景，但不能推进计数或改写持久状态。"""
    channel = str(ai_engine_base.config.CHANNEL_ID)
    original = scene_state.start(
        db, channel, check_in_name="interview", description="采访场景", now=T0
    )
    raw = db.get_state(f"scene:{channel}")
    seen = {}

    class FakeMemory:
        async def build_context(self, **kwargs):
            return SimpleNamespace(relevant_history=[])

    async def no_context():
        return None

    async def fake_call(db_arg, prompt_parts, messages, **kwargs):
        seen["prompt"] = prompt_parts.flatten()
        return "正常回复"

    monkeypatch.setattr(ai_engine_base, "is_morning", lambda: False)
    monkeypatch.setattr(ai_engine_base, "get_calendar_context", no_context)
    monkeypatch.setattr(
        ai_engine_base, "_build_prompt", lambda *args, **kwargs: _prompt()
    )
    monkeypatch.setattr(ai_engine_base.trace, "start", lambda *args, **kwargs: None)
    monkeypatch.setattr(ai_engine_base.trace, "finalize", lambda *args, **kwargs: None)

    preset = Preset(
        name="scene-test",
        provider="relay",
        api_key="sk-test",
        base_url="https://relay.example.com",
        model="test-model",
    )
    result = asyncio.run(ai_engine_base.chat(
        db,
        [{"role": "user", "content": "继续聊"}],
        fake_call,
        preset,
        memory_service=FakeMemory(),
    ))

    assert result == "正常回复"
    assert "【当前场景】" in seen["prompt"]
    assert "采访场景" in seen["prompt"]
    assert scene_state.load(db, channel, T0) == original
    assert db.get_state(f"scene:{channel}") == raw


def _run_scheduled(db, monkeypatch, *, trigger, track_scene=False):
    """运行一个无外部 I/O 的 scheduled_action，并记录模型可见的场景。"""
    seen = {}

    async def fake_call(
        db_arg, prompt_parts, messages, *, tool_names=None, **kwargs
    ):
        seen["scene"] = scene_state.load(
            db_arg, str(ai_engine_base.config.CHANNEL_ID), T0
        )
        seen["tool_names"] = tool_names
        return "[SILENT]"

    monkeypatch.setattr(ai_engine_base.trace, "start", lambda *args, **kwargs: None)
    monkeypatch.setattr(ai_engine_base.trace, "finalize", lambda *args, **kwargs: None)

    preset = Preset(
        name="scene-test",
        provider="relay",
        api_key="sk-test",
        base_url="https://relay.example.com",
        model="test-model",
    )
    asyncio.run(ai_engine_base.scheduled_action(
        db,
        "测试调度",
        "2026-08-24 20:00",
        [],
        fake_call,
        preset,
        allow_silent=True,
        trigger=trigger,
        tool_profile="poll" if trigger != "reminder" else "reminder_safe",
        track_scene=track_scene,
        context_config={"include_weather": False, "include_calendar": False},
    ))
    return seen


def test_check_in_clears_old_scene_before_model_call(db, monkeypatch):
    channel = str(ai_engine_base.config.CHANNEL_ID)
    scene_state.start(
        db, channel, check_in_name="old", description="旧场景", now=T0
    )

    seen = _run_scheduled(db, monkeypatch, trigger="check_in", track_scene=True)

    assert seen["scene"] is None
    assert "set_scene" in seen["tool_names"]
    assert scene_state.load(db, channel, T0) is None


def test_reminder_does_not_end_the_scene(db, monkeypatch):
    channel = str(ai_engine_base.config.CHANNEL_ID)
    original = scene_state.start(
        db, channel, check_in_name="interview", description="采访场景", now=T0
    )

    seen = _run_scheduled(db, monkeypatch, trigger="reminder")

    assert seen["scene"] == original
    assert "set_scene" not in seen["tool_names"]
    assert scene_state.load(db, channel, T0) == original


def test_every_layer_of_scheduled_action_accepts_the_same_kwargs():
    """三层 scheduled_action 的关键字参数必须一致。"""
    import inspect

    from bot import ai_engine, ai_engine_openai_compat

    def kwargs_of(fn):
        return {
            name
            for name, p in inspect.signature(fn).parameters.items()
            if p.kind is inspect.Parameter.KEYWORD_ONLY
            or (
                p.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
                and p.default is not inspect.Parameter.empty
            )
        }

    router = kwargs_of(ai_engine.scheduled_action)
    adapter = kwargs_of(ai_engine_openai_compat.scheduled_action)
    base = kwargs_of(ai_engine_base.scheduled_action)

    assert router <= adapter, f"适配层缺少: {sorted(router - adapter)}"
    assert router <= base, f"实现层缺少: {sorted(router - base)}"
