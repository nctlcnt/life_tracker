"""LT-135 窗口接线契约：base chat 的 exclude_recent 联动 + trace 窗口可见性。"""
import asyncio

import pytest

import bot.ai_engine_base as base
from bot import trace
from bot.database import Database
from bot.memory.context_window import ContextWindow
from config import Preset


class _FakeMemory:
    """最小 MemoryService 替身：记录 build_context 收到的参数。"""

    def __init__(self):
        self.build_context_calls = []

    async def build_context(self, **kwargs):
        self.build_context_calls.append(kwargs)

        class _Ctx:
            relevant_history = None

        return _Ctx()

    def list_durable(self):
        return []

    def durable_markdown(self):
        return ""


@pytest.fixture
def chat_env(monkeypatch, tmp_path):
    db = Database(str(tmp_path / "wiring_test.db"))
    memory = _FakeMemory()
    monkeypatch.setattr(base, "is_morning", lambda: False)

    async def no_calendar():
        return None

    monkeypatch.setattr(base, "get_calendar_context", no_calendar)
    # trace 落盘导到 tmp（避免写进仓库 data/）
    monkeypatch.setattr(trace, "_TRACE_DIR", tmp_path / "traces")

    captured = {}

    async def fake_call(db_, prompt, messages, preset=None, **kwargs):
        captured["entry"] = trace._current.get()
        return "好的"

    preset = Preset(name="t", provider="relay", api_key="k",
                    base_url="https://x", model="m")

    def run(window=None):
        return asyncio.run(base.chat(
            db, [{"role": "user", "content": "[2026-07-17 10:00] 在吗"}],
            fake_call, preset, memory_service=memory, window=window,
        ))

    yield run, memory, captured
    trace._current.set(None)


def test_exclude_recent_follows_window_tail(chat_env):
    run, memory, captured = chat_env
    window = ContextWindow(tail_count=7, tail_tokens=700, total_tokens=900,
                           summary_present=True, summary_tokens=200)

    run(window=window)

    assert memory.build_context_calls[0]["exclude_recent"] == 7
    assert captured["entry"]["window"] == window.trace_info()


def test_no_window_falls_back_to_fixed_20(chat_env):
    run, memory, captured = chat_env

    run(window=None)

    assert memory.build_context_calls[0]["exclude_recent"] == 20
    assert captured["entry"]["window"] is None
