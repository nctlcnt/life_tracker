"""scheduled_action 的送达语义：返回值 = 真正发给用户的内容。

AI 可以在工具轮里先把话说出去，最后一轮再回 [SILENT]。旧逻辑在
allow_silent=False 时只看最后一轮，于是返回 None，调用方误判「没发出去」：
reminder 不被 mark_reminder_done，留在 pending 且触发时间已过，
_reminder_loop 以 wait=0 立刻重入，每轮白烧一次 AI 调用。
"""
import asyncio

import pytest

import bot.ai_engine_base as engine
import bot.trace as trace
from bot.database import Database
from config import Preset


PRESET = Preset(name="test", provider="relay", api_key="k",
                base_url="http://localhost", model="test-model")


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "delivery_test.db"))


@pytest.fixture(autouse=True)
def _no_external_context(monkeypatch, tmp_path):
    """掐掉天气/日历的网络调用，只留下送达判定这条路径。

    trace 默认写 data/ai_traces（生产目录），一并改到 tmp——写失败会被
    trace._write 吞掉，不隔离的话测试是靠目录没权限才没污染真实数据。
    """
    async def _none():
        return None
    monkeypatch.setattr(engine, "get_weather_brief", _none)
    monkeypatch.setattr(engine, "get_calendar_context", _none)
    monkeypatch.setattr(trace, "_TRACE_DIR", tmp_path / "ai_traces")


def _run(db, call_with_tools_fn, *, allow_silent=False):
    sent: list[str] = []

    async def _send(text: str):
        sent.append(text)

    reply = asyncio.run(engine.scheduled_action(
        db, "prompt", "2026-08-02 08:30", [], call_with_tools_fn,
        preset=PRESET, send_callback=_send, allow_silent=allow_silent,
        trigger="reminder",
    ))
    return reply, sent


def test_tool_round_text_counts_as_delivered_despite_silent_final(db):
    """工具轮发过话、末轮 [SILENT] → 返回已发内容，调用方据此标记 reminder done。"""
    async def fake_call(_db, _parts, _messages, send_callback=None, **_kw):
        await send_callback("早呀 Cece～")
        return "[SILENT]"

    reply, sent = _run(db, fake_call)

    assert reply == "早呀 Cece～"
    assert sent == ["早呀 Cece～"]


def test_silent_alone_is_not_delivered(db):
    """一句话都没发出去，才算没送达。"""
    async def fake_call(_db, _parts, _messages, send_callback=None, **_kw):
        await send_callback("[SILENT]")
        return "[SILENT]"

    reply, sent = _run(db, fake_call)

    assert reply is None
    assert sent == []  # [SILENT] 在引擎层就被吞掉，不该走到 send_callback


def test_normal_final_reply_passes_through(db):
    """常规路径不受影响：末轮有正文就用末轮的。"""
    async def fake_call(_db, _parts, _messages, send_callback=None, **_kw):
        await send_callback("正文")
        return "正文"

    reply, sent = _run(db, fake_call)

    assert reply == "正文"
    assert sent == ["正文"]


def test_allow_silent_still_joins_sent_texts(db):
    """check-in 侧（allow_silent=True）行为不变：拼接实际发出的各轮文字。"""
    async def fake_call(_db, _parts, _messages, send_callback=None, **_kw):
        await send_callback("第一句")
        await send_callback("[SILENT]")
        await send_callback("第二句")
        return "[SILENT]"

    reply, sent = _run(db, fake_call, allow_silent=True)

    assert reply == "第一句\n第二句"
    assert sent == ["第一句", "第二句"]
