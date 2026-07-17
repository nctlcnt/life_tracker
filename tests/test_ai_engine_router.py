"""bot/ai_engine.py 入口层的 fallback 行为契约（LT-134 合并后单引擎版）。

引擎只有一个实现，fallback = 换 fallback preset 重试同一引擎：
- active preset 成功 → fallback 不被触碰
- active 抛 AIProviderError → 用 fallback preset 重试
- 没有 fallback → AIProviderError 原样抛出
- 非 AIProviderError 异常 → 不重试，直接抛
- chat / scheduled_action / simple_completion 的参数透传 + preset 注入
"""
import asyncio

import pytest

import config
from config import Preset
import bot.ai_engine as ai_engine
from bot.ai_provider_error import AIProviderError


def _preset(name: str) -> Preset:
    return Preset(name=name, provider="relay", api_key=f"key-{name}",
                  base_url="https://example.com", model=f"model-{name}")


class _FakeEngine:
    """记录调用参数的假引擎模块。fail_presets 里的 preset 抛 AIProviderError。"""

    def __init__(self, reply: str = "ok", fail_presets: set[str] | None = None):
        self.reply = reply
        self.fail_presets = fail_presets or set()
        self.calls: list[dict] = []

    async def _respond(self, method: str, args: tuple, kwargs: dict):
        self.calls.append({"method": method, "args": args, "kwargs": kwargs})
        preset = kwargs["preset"]
        if preset.name in self.fail_presets:
            raise AIProviderError(f"{preset.name} unavailable")
        return f"{self.reply}:{preset.name}"

    async def chat(self, *args, **kwargs):
        return await self._respond("chat", args, kwargs)

    async def scheduled_action(self, *args, **kwargs):
        return await self._respond("scheduled_action", args, kwargs)

    async def simple_completion(self, *args, **kwargs):
        return await self._respond("simple_completion", args, kwargs)


def _install(monkeypatch, engine, *, with_fallback=True):
    active = _preset("primary")
    fallback = _preset("backup") if with_fallback else None
    monkeypatch.setattr(config, "get_active", lambda: active)
    monkeypatch.setattr(config, "get_fallback", lambda: fallback)
    monkeypatch.setattr(ai_engine, "_engine", engine)
    return active, fallback


def test_active_success_does_not_retry(monkeypatch):
    engine = _FakeEngine("reply")
    active, _ = _install(monkeypatch, engine)

    result = asyncio.run(ai_engine.simple_completion("hello"))

    assert result == "reply:primary"
    assert len(engine.calls) == 1
    assert engine.calls[0]["kwargs"]["preset"] is active


def test_provider_error_retries_with_fallback_preset(monkeypatch):
    engine = _FakeEngine("reply", fail_presets={"primary"})
    _, fallback = _install(monkeypatch, engine)

    result = asyncio.run(ai_engine.simple_completion("hello"))

    assert result == "reply:backup"
    assert len(engine.calls) == 2
    assert engine.calls[0]["kwargs"]["preset"].name == "primary"
    assert engine.calls[1]["kwargs"]["preset"] is fallback


def test_provider_error_without_fallback_reraises(monkeypatch):
    engine = _FakeEngine(fail_presets={"primary"})
    _install(monkeypatch, engine, with_fallback=False)

    with pytest.raises(AIProviderError):
        asyncio.run(ai_engine.simple_completion("hello"))
    assert len(engine.calls) == 1


def test_non_provider_error_does_not_retry(monkeypatch):
    class _Boom(_FakeEngine):
        async def simple_completion(self, *args, **kwargs):
            self.calls.append({"method": "simple_completion"})
            raise RuntimeError("bug, not a provider outage")

    engine = _Boom()
    _install(monkeypatch, engine)

    with pytest.raises(RuntimeError):
        asyncio.run(ai_engine.simple_completion("hello"))
    assert len(engine.calls) == 1


def test_chat_forwards_arguments_and_injects_preset(monkeypatch):
    engine = _FakeEngine()
    active, _ = _install(monkeypatch, engine)

    db = object()
    messages = [{"role": "user", "content": "hi"}]
    send_cb = object()
    tool_cb = object()
    memory = object()

    result = asyncio.run(ai_engine.chat(
        db, messages, send_callback=send_cb, tool_callback=tool_cb,
        memory_service=memory,
    ))

    assert result == "ok:primary"
    call = engine.calls[0]
    assert call["method"] == "chat"
    assert call["args"] == (db, messages)
    assert call["kwargs"] == {
        "preset": active,
        "send_callback": send_cb,
        "tool_callback": tool_cb,
        "memory_service": memory,
    }


def test_scheduled_action_forwards_arguments(monkeypatch):
    engine = _FakeEngine()
    active, _ = _install(monkeypatch, engine)

    db = object()
    history = [{"role": "user", "content": "早"}]

    asyncio.run(ai_engine.scheduled_action(
        db, "去喝水", "2026-07-17 10:00", history,
        allow_silent=True, trigger="poll", tool_profile="poll",
        check_in_name="morning", context_config={"include_weather": False},
    ))

    call = engine.calls[0]
    assert call["method"] == "scheduled_action"
    assert call["args"] == (db, "去喝水", "2026-07-17 10:00", history)
    assert call["kwargs"]["preset"] is active
    assert call["kwargs"]["allow_silent"] is True
    assert call["kwargs"]["trigger"] == "poll"
    assert call["kwargs"]["tool_profile"] == "poll"
    assert call["kwargs"]["check_in_name"] == "morning"
    assert call["kwargs"]["context_config"] == {"include_weather": False}
