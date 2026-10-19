"""场景状态进入 prompt 的契约：场景属于卡片，只在卡片的 thread 里注入。"""
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


def _run_chat(db, monkeypatch, *, card_id=None, window=None):
    """运行一次无外部 I/O 的 chat，返回模型看到的 system prompt。"""
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
        window=window,
        card_id=card_id,
    ))
    assert result == "正常回复"
    return seen["prompt"]


def _interview_card(db):
    """一张被回复过的采访 check-in 卡片，带着它自己的场景。"""
    card_id = db.create_card(
        channel_id="123", source_type="check_in", source_id="interview:t1",
        status="active", now="2026-08-24T10:00:00+00:00",
        expires_at="2099-01-01T00:00:00+00:00",
    )
    scope = scene_state.card_scope("check_in", "interview:t1")
    scene = scene_state.start(
        db, scope, check_in_name="interview", description="采访场景", now=T0
    )
    return card_id, scope, scene


def test_chat_in_a_card_thread_reads_its_scene_without_mutating_it(db, monkeypatch):
    """在卡片的 thread 里聊天应注入这张卡片的场景，但不能改写持久状态。"""
    card_id, scope, original = _interview_card(db)
    raw = db.get_state(f"scene:{scope}")

    prompt = _run_chat(db, monkeypatch, card_id=card_id)

    assert "【当前场景】" in prompt and "采访场景" in prompt
    assert "【当前卡片】" in prompt and "check_in1" in prompt
    assert scene_state.load(db, scope, T0) == original
    assert db.get_state(f"scene:{scope}") == raw


def test_main_line_chat_does_not_see_card_scenes(db, monkeypatch):
    _interview_card(db)

    prompt = _run_chat(db, monkeypatch)

    assert "当前场景" not in prompt
    assert "当前卡片" not in prompt


def test_card_prefix_is_explained_only_when_card_messages_are_visible(db, monkeypatch):
    with_cards = SimpleNamespace(has_card_messages=True, tail_count=1,
                                 trace_info=lambda: {})
    without = SimpleNamespace(has_card_messages=False, tail_count=1,
                              trace_info=lambda: {})

    assert "【对话里的卡片】" in _run_chat(db, monkeypatch, window=with_cards)
    assert "【对话里的卡片】" not in _run_chat(db, monkeypatch, window=without)


def test_set_scene_writes_into_the_card_scope(db):
    scope = scene_state.card_scope("check_in", "interview:t1")
    result = ai_engine_base._execute_tool(
        db, "set_scene",
        {"description": "晚间采访，刚问了晚饭", "_check_in_name": "interview",
         "_scene_scope": scope})

    assert result["success"] is True
    assert scene_state.load(db, scope, T0).description == "晚间采访，刚问了晚饭"
    assert scene_state.load(db, str(ai_engine_base.config.CHANNEL_ID), T0) is None


def test_set_scene_without_a_card_is_refused(db):
    result = ai_engine_base._execute_tool(
        db, "set_scene", {"description": "采访", "_check_in_name": "interview"})
    assert result["success"] is False


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


def test_check_in_leaves_other_card_scenes_alone(db, monkeypatch):
    """场景属于各自的卡片，新的 check-in 不再清除旧场景。"""
    scope = scene_state.card_scope("check_in", "interview:t1")
    original = scene_state.start(
        db, scope, check_in_name="interview", description="旧场景", now=T0
    )

    seen = _run_scheduled(db, monkeypatch, trigger="check_in", track_scene=True)

    assert "set_scene" in seen["tool_names"]
    assert scene_state.load(db, scope, T0) == original


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
