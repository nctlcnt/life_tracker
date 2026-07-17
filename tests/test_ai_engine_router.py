"""LT-134 characterization tests：冻结 bot/ai_engine.py 路由层的 fallback 行为。

引擎合并期间这些行为必须保持不变：
- active preset 成功 → fallback 不被触碰
- active 抛 AIProviderError → 自动切 fallback preset 重跑
- 没有 fallback → AIProviderError 原样抛出
- 非 AIProviderError 异常 → 不走 fallback，直接抛
- chat / scheduled_action / simple_completion 的参数透传 + preset 注入
"""
import asyncio
from types import SimpleNamespace

import pytest

import config
from config import Preset
import bot.ai_engine as ai_engine
from bot.ai_provider_error import AIProviderError


def _preset(name: str, provider: str) -> Preset:
    return Preset(name=name, provider=provider, api_key=f"key-{name}",
                  base_url="https://example.com", model=f"model-{name}")


class _FakeEngine:
    """记录调用参数的假引擎模块。fail=True 时抛 AIProviderError。"""

    def __init__(self, reply: str, fail: bool = False):
        self.reply = reply
        self.fail = fail
        self.calls: list[dict] = []

    async def _respond(self, method: str, args: tuple, kwargs: dict):
        self.calls.append({"method": method, "args": args, "kwargs": kwargs})
        if self.fail:
            raise AIProviderError(f"{self.reply} unavailable")
        return self.reply

    async def chat(self, *args, **kwargs):
        return await self._respond("chat", args, kwargs)

    async def scheduled_action(self, *args, **kwargs):
        return await self._respond("scheduled_action", args, kwargs)

    async def simple_completion(self, *args, **kwargs):
        return await self._respond("simple_completion", args, kwargs)


def _install(monkeypatch, active_engine, fallback_engine=None,
             active_provider="relay", fallback_provider="openai"):
    """把假引擎接进路由：active/fallback preset + provider→engine 映射。"""
    active = _preset("primary", active_provider)
    fallback = _preset("backup", fallback_provider) if fallback_engine else None
    engines = {active_provider: active_engine}
    if fallback_engine:
        engines[fallback_provider] = fallback_engine

    monkeypatch.setattr(config, "get_active", lambda: active)
    monkeypatch.setattr(config, "get_fallback", lambda: fallback)
    monkeypatch.setattr(ai_engine, "_load_engine", lambda provider: engines[provider])
    return active, fallback


def test_active_success_does_not_touch_fallback(monkeypatch):
    active_engine = _FakeEngine("primary reply")
    fallback_engine = _FakeEngine("backup reply")
    active, _ = _install(monkeypatch, active_engine, fallback_engine)

    result = asyncio.run(ai_engine.simple_completion("hello"))

    assert result == "primary reply"
    assert fallback_engine.calls == []
    assert active_engine.calls[0]["kwargs"]["preset"] is active


def test_provider_error_switches_to_fallback_preset(monkeypatch):
    active_engine = _FakeEngine("primary", fail=True)
    fallback_engine = _FakeEngine("backup reply")
    _, fallback = _install(monkeypatch, active_engine, fallback_engine)

    result = asyncio.run(ai_engine.simple_completion("hello"))

    assert result == "backup reply"
    assert len(active_engine.calls) == 1
    assert fallback_engine.calls[0]["kwargs"]["preset"] is fallback


def test_provider_error_without_fallback_reraises(monkeypatch):
    active_engine = _FakeEngine("primary", fail=True)
    _install(monkeypatch, active_engine, fallback_engine=None)

    with pytest.raises(AIProviderError):
        asyncio.run(ai_engine.simple_completion("hello"))


def test_non_provider_error_does_not_fallback(monkeypatch):
    class _Boom(_FakeEngine):
        async def simple_completion(self, *args, **kwargs):
            raise RuntimeError("bug, not a provider outage")

    fallback_engine = _FakeEngine("backup reply")
    _install(monkeypatch, _Boom("primary"), fallback_engine)

    with pytest.raises(RuntimeError):
        asyncio.run(ai_engine.simple_completion("hello"))
    assert fallback_engine.calls == []


def test_chat_forwards_arguments_and_injects_preset(monkeypatch):
    active_engine = _FakeEngine("ok")
    active, _ = _install(monkeypatch, active_engine)

    db = object()
    messages = [{"role": "user", "content": "hi"}]
    send_cb = object()
    tool_cb = object()
    memory = object()

    result = asyncio.run(ai_engine.chat(
        db, messages, send_callback=send_cb, tool_callback=tool_cb,
        memory_service=memory,
    ))

    assert result == "ok"
    call = active_engine.calls[0]
    assert call["method"] == "chat"
    assert call["args"] == (db, messages)
    assert call["kwargs"] == {
        "preset": active,
        "send_callback": send_cb,
        "tool_callback": tool_cb,
        "memory_service": memory,
    }


def test_scheduled_action_forwards_arguments(monkeypatch):
    active_engine = _FakeEngine("ok")
    active, _ = _install(monkeypatch, active_engine)

    db = object()
    history = [{"role": "user", "content": "早"}]

    asyncio.run(ai_engine.scheduled_action(
        db, "去喝水", "2026-07-17 10:00", history,
        allow_silent=True, trigger="poll", tool_profile="poll",
        check_in_name="morning", context_config={"include_weather": False},
    ))

    call = active_engine.calls[0]
    assert call["method"] == "scheduled_action"
    assert call["args"] == (db, "去喝水", "2026-07-17 10:00", history)
    assert call["kwargs"]["preset"] is active
    assert call["kwargs"]["allow_silent"] is True
    assert call["kwargs"]["trigger"] == "poll"
    assert call["kwargs"]["tool_profile"] == "poll"
    assert call["kwargs"]["check_in_name"] == "morning"
    assert call["kwargs"]["context_config"] == {"include_weather": False}
