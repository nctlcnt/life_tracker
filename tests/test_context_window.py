"""LT-135 token 窗口装配（bot/memory/context_window.py）的行为契约。

纯装配逻辑：不涉及 AI 调用，不涉及行为切换。
"""
import json
from datetime import datetime, timezone

import pytest

import config
from bot.database import Database
from bot.memory.context_window import (
    HARD_CAP_RATIO, KEEP_RATIO, STATE_KEY_PREFIX, SUMMARY_LABEL,
    assemble_window, load_summary_state, message_tokens, save_summary_state,
)

CHANNEL = "chan-1"


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "ctx_test.db"))


def _add(db, role, content, n=None):
    """写一条会话消息，返回行 id。user 消息带 current_content（模拟真实入库）。"""
    metadata = None
    if role == "user":
        metadata = {"current_content": f"[2026-07-17 10:00] {content}"}
    return db.add_conversation_message(
        discord_message_id=None if n is None else f"m{n}",
        channel_id=CHANNEL, role=role, content=content,
        created_at=datetime.now(timezone.utc).isoformat(),
        metadata=metadata,
    )


def test_empty_channel_returns_empty_window(db):
    w = assemble_window(db, CHANNEL)
    assert w.messages == []
    assert w.total_tokens == 0
    assert not w.needs_compact
    assert not w.summary_present
    assert w.last_message_id == 0


def test_plain_tail_formatting_and_accounting(db):
    _add(db, "user", "早上好", 1)
    _add(db, "assistant", "早呀，今天有什么安排？", 2)

    w = assemble_window(db, CHANNEL)

    assert [m["role"] for m in w.messages] == ["user", "assistant"]
    # user 消息用 metadata.current_content（带时间戳前缀）
    assert w.messages[0]["content"] == "[2026-07-17 10:00] 早上好"
    assert w.tail_count == 2
    assert w.total_tokens == sum(message_tokens(m["content"]) for m in w.messages)
    assert not w.needs_compact
    assert not w.summary_present


def test_summary_state_roundtrip_and_injection(db):
    id1 = _add(db, "user", "旧消息（应被摘要覆盖）", 1)
    id2 = _add(db, "user", "新消息", 2)
    save_summary_state(db, CHANNEL, summary="她最近在准备考试。",
                       upto_message_id=id1, model="test-model",
                       updated_at="2026-07-17T10:00:00")

    state = load_summary_state(db, CHANNEL)
    assert state["upto_message_id"] == id1
    assert state["model"] == "test-model"

    w = assemble_window(db, CHANNEL)
    # 摘要条在最前，带标签；upto 之前的消息不再出现在明文里
    assert w.summary_present
    assert w.messages[0]["role"] == "user"
    assert w.messages[0]["content"].startswith(SUMMARY_LABEL)
    assert "考试" in w.messages[0]["content"]
    assert [m["content"] for m in w.messages[1:]] == ["[2026-07-17 10:00] 新消息"]
    assert w.upto_message_id == id1
    assert w.last_message_id == id2


def test_corrupt_summary_state_ignored(db):
    db.set_state(f"{STATE_KEY_PREFIX}{CHANNEL}", "not-json{{{")
    _add(db, "user", "hi", 1)
    w = assemble_window(db, CHANNEL)
    assert not w.summary_present
    assert w.tail_count == 1


def test_threshold_marks_needs_compact_with_fold_cut(db, monkeypatch):
    # 10 条消息每条 32tk（20 CJK + 时间戳前缀），全量 320tk；
    # 阈值 300 → 触发 compact；硬上限 360 ≥ 320 → 明文不裁
    ids = [_add(db, "user", "聊" * 20, n) for n in range(10)]
    monkeypatch.setattr(config, "CONTEXT_COMPACT_THRESHOLD_TOKENS", 300)

    w = assemble_window(db, CHANNEL)

    assert w.needs_compact
    # keep 预算 = 300×0.4 = 120tk → 保留最新 3 条；fold 切点 = 第 4 新那条
    per = message_tokens(w.messages[-1]["content"])
    keep_n = int(300 * KEEP_RATIO) // per
    assert keep_n == 3
    assert w.fold_upto_id == ids[-(keep_n + 1)]
    # 阈值与硬上限之间：明文不裁，照常全量
    assert w.hard_trimmed == 0
    assert w.tail_count == 10


def test_hard_cap_trims_oldest_plaintext(db, monkeypatch):
    ids = [_add(db, "user", "聊" * 200, n) for n in range(10)]
    monkeypatch.setattr(config, "CONTEXT_COMPACT_THRESHOLD_TOKENS", 100)

    w = assemble_window(db, CHANNEL)

    per = message_tokens(w.messages[-1]["content"])
    cap = int(100 * HARD_CAP_RATIO)
    fit = max(cap // per, 1)
    assert w.hard_trimmed == 10 - fit
    assert w.tail_count == fit
    # 裁的是最老的；最新一条永远保留
    assert w.messages[-1]["content"].endswith("聊" * 200)
    assert w.last_message_id == ids[-1]
    assert w.needs_compact


def test_summary_never_trimmed_and_last_message_survives(db, monkeypatch):
    id1 = _add(db, "user", "旧", 1)
    _add(db, "user", "很长的新消息" + "聊" * 300, 2)
    save_summary_state(db, CHANNEL, summary="摘" * 200, upto_message_id=id1,
                       model="m", updated_at="t")
    monkeypatch.setattr(config, "CONTEXT_COMPACT_THRESHOLD_TOKENS", 50)

    w = assemble_window(db, CHANNEL)

    # cap 远小于内容量：摘要仍在、最后一条明文仍在（永不裁到空）
    assert w.summary_present
    assert w.messages[0]["content"].startswith(SUMMARY_LABEL)
    assert len(w.messages) == 2


def test_caller_budget_trims_but_compact_uses_global_threshold(db, monkeypatch):
    [_add(db, "user", "聊" * 200, n) for n in range(10)]
    monkeypatch.setattr(config, "CONTEXT_COMPACT_THRESHOLD_TOKENS", 100000)

    w = assemble_window(db, CHANNEL, max_tokens=150)

    # 小预算（random_poll 场景）：明文被裁小
    per = message_tokens(w.messages[-1]["content"])
    assert w.tail_count == max(150 // per, 1)
    # 但全局阈值远未到 → 不触发 compact（小窗口调用不误触发）
    assert not w.needs_compact


def test_all_tail_fits_keep_budget_skips_compact(db, monkeypatch):
    # 巨大摘要 + 少量明文：超阈值来自摘要本身，折叠无意义，不触发
    id1 = _add(db, "user", "旧", 1)
    _add(db, "user", "新消息", 2)
    save_summary_state(db, CHANNEL, summary="摘" * 2000, upto_message_id=id1,
                       model="m", updated_at="t")
    monkeypatch.setattr(config, "CONTEXT_COMPACT_THRESHOLD_TOKENS", 500)

    w = assemble_window(db, CHANNEL)
    assert not w.needs_compact
    assert w.fold_upto_id == 0


def test_trace_info_contract(db):
    _add(db, "user", "hi", 1)
    info = assemble_window(db, CHANNEL).trace_info()
    assert set(info) == {"summary_present", "summary_tokens", "tail_count",
                         "tail_tokens", "total_tokens", "upto_message_id",
                         "needs_compact", "hard_trimmed"}


def test_memory_service_delegation(db):
    from bot.memory import MemoryService
    service = MemoryService(db)
    _add(db, "user", "hi", 1)
    w = service.context_window(CHANNEL)
    assert w.tail_count == 1
