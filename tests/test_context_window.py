"""LT-135 token 窗口装配（bot/memory/context_window.py）的行为契约。

纯装配逻辑：不涉及 AI 调用，不涉及行为切换。
"""
import json
import re
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


def test_window_never_drops_history_beyond_legacy_thousand_row_limit(db, monkeypatch):
    ids = [_add(db, "user", f"消息-{n}", n) for n in range(1005)]
    monkeypatch.setattr(config, "CONTEXT_COMPACT_THRESHOLD_TOKENS", 1_000_000)

    w = assemble_window(db, CHANNEL)

    assert w.tail_count == 1005
    assert w.messages[0]["content"].endswith("消息-0")
    assert w.messages[-1]["content"].endswith("消息-1004")
    assert w.last_message_id == ids[-1]


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


TS_PREFIX = re.compile(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}\] ")


def test_assistant_messages_carry_timestamp_prefix(db):
    """assistant 侧也要带 [时间] 前缀。

    _ensure_valid_messages 会把连续同角色消息拼成一条：用户长时间不回复时，
    几天的主动消息塌缩成一整块无日期文本，模型分不清哪句是刚说的，于是把
    同样的问候重复发一遍。前缀是那块文本里唯一的时间锚点。
    """
    _add(db, "assistant", "早呀", 1)
    w = assemble_window(db, CHANNEL)
    content = w.messages[-1]["content"]
    assert TS_PREFIX.match(content), content
    assert content.endswith("早呀")


def test_user_current_content_not_double_prefixed(db):
    """user 消息入库时已带前缀（current_content），不能再加一次。"""
    _add(db, "user", "在吗", 1)
    content = assemble_window(db, CHANNEL).messages[-1]["content"]
    assert content == "[2026-07-17 10:00] 在吗"


def test_merged_assistant_run_keeps_one_anchor_per_message(db):
    """连续 assistant 消息合并后，每条各自的时间锚点都要留在块里。"""
    from bot.ai_engine_base import _ensure_valid_messages

    _add(db, "user", "在吗", 1)
    for n, text in enumerate(["早呀", "该睡了", "早呀（第二天）"], start=2):
        _add(db, "assistant", text, n)
    merged = _ensure_valid_messages(list(assemble_window(db, CHANNEL).messages))
    blob = merged[-1]["content"]
    assert len(re.findall(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}\]", blob)) == 3
    assert "早呀" in blob and "该睡了" in blob


# ── 消息卡片：上下文里的卡片名与末尾引用 ────────────────────────────────────

def _card(db, source_type, source_id, status="active"):
    """建一张卡片，返回 id。"""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    later = datetime(2099, 1, 1, tzinfo=timezone.utc).isoformat(timespec="seconds")
    return db.create_card(channel_id=CHANNEL, source_type=source_type,
                          source_id=source_id, status=status,
                          now=now, expires_at=later)


def _add_in_card(db, role, content, n, card_id, reply_to=None):
    """写一条归属于某张卡片的会话消息。"""
    metadata = None
    if role == "user":
        metadata = {"current_content": f"[2026-07-17 10:00] {content}"}
    return db.add_conversation_message(
        discord_message_id=f"m{n}", channel_id=CHANNEL, role=role,
        content=content, created_at=datetime.now(timezone.utc).isoformat(),
        reply_to_message_id=reply_to, metadata=metadata, card_id=card_id,
    )


def test_card_label_joins_the_existing_timestamp_prefix(db):
    """带卡片的消息前缀变成 [卡片名 · 时间]，完整日期仍然保留。"""
    card_id = _card(db, "check_in", "ci-1")
    _add_in_card(db, "assistant", "早呀", 1, card_id)

    content = assemble_window(db, CHANNEL).messages[-1]["content"]
    assert content.startswith("[check_in1 · ")
    # 完整日期仍然留在前缀里（时间锚点不能因为加了卡片名就退化成 9/27）
    assert re.match(r"^\[check_in1 · \d{4}-\d{2}-\d{2} \d{2}:\d{2}\] ", content), content
    assert content.endswith("早呀")


def test_messages_without_card_keep_the_plain_prefix(db):
    """卡片上线之前的历史没有归属，前缀必须原样不动。"""
    _add(db, "user", "在吗", 1)
    assert assemble_window(db, CHANNEL).messages[-1]["content"] == "[2026-07-17 10:00] 在吗"


def test_card_ordinal_skips_pending_cards(db):
    """pending 卡片会被过期清理整行删掉，不能占用序号，否则卡片名会漂移。"""
    _card(db, "check_in", "ci-pending", status="pending")
    second = _card(db, "check_in", "ci-active")
    _add_in_card(db, "assistant", "早呀", 1, second)

    assert assemble_window(db, CHANNEL).messages[-1]["content"].startswith("[check_in1 · ")


def test_card_ordinal_is_per_source_type(db):
    """序号按来源类型各自计数。"""
    first = _card(db, "check_in", "ci-1")
    second = _card(db, "check_in", "ci-2")
    other = _card(db, "reminder", "rm-1")
    _add_in_card(db, "assistant", "早呀", 1, first)
    _add_in_card(db, "assistant", "该睡了", 2, second)
    _add_in_card(db, "assistant", "提醒到了", 3, other)

    labels = [m["content"].split(" · ")[0].lstrip("[")
              for m in assemble_window(db, CHANNEL).messages]
    assert labels == ["check_in1", "check_in2", "reminder1"]


def test_reference_names_the_card_without_repeating_the_inline_quote(db):
    """discord_bot 已经内联过引用片段时，末尾只补卡片名，不再引用一遍原话。"""
    card_id = _card(db, "check_in", "ci-1")
    _add_in_card(db, "assistant", "今天打算做什么？", 1, card_id)
    db.add_conversation_message(
        discord_message_id="m2", channel_id=CHANNEL, role="user",
        content="写代码", created_at=datetime.now(timezone.utc).isoformat(),
        reply_to_message_id="m1", card_id=card_id,
        metadata={"current_content": '[2026-07-17 10:05] [回复 你说过 的消息: "今天打算做什么？"]\n写代码'},
    )

    w = assemble_window(db, CHANNEL)
    reference = w.messages[-2]
    assert reference == {"role": "user", "content": "【上一条在回复卡片 check_in1】"}
    assert "今天打算做什么？" not in reference["content"]
    assert w.messages[-1]["content"].endswith("写代码")


def test_reference_quotes_the_target_when_inline_quote_is_missing(db):
    """抓取 Discord 原消息失败时不会有内联引用，末尾要补上原话。"""
    card_id = _card(db, "check_in", "ci-1")
    _add_in_card(db, "assistant", "今天打算做什么？", 1, card_id)
    _add_in_card(db, "user", "写代码", 2, card_id, reply_to="m1")

    reference = assemble_window(db, CHANNEL).messages[-2]
    assert "check_in1" in reference["content"]
    assert "今天打算做什么？" in reference["content"]


def test_reference_pulls_more_rounds_when_target_is_folded(db):
    """被回复的消息已折进摘要时，明文里看不到它，末尾多引用几条原话。"""
    card_id = _card(db, "check_in", "ci-1")
    old_ids = [
        _add_in_card(db, "assistant", "第一句", 1, card_id),
        _add_in_card(db, "assistant", "第二句", 2, card_id),
        _add_in_card(db, "assistant", "第三句", 3, card_id),
    ]
    save_summary_state(db, CHANNEL, summary="早前摘要",
                       upto_message_id=old_ids[-1], model="m",
                       updated_at="2026-07-17T10:00:00+00:00")
    _add_in_card(db, "user", "回应一下", 4, card_id, reply_to="m3")

    reference = assemble_window(db, CHANNEL).messages[-2]
    assert "早于摘要分界线" in reference["content"]
    assert "第三句" in reference["content"] and "第二句" in reference["content"]


def test_reference_is_not_counted_as_a_real_tail_message(db):
    """末尾引用是合成消息：不算进 tail_count，但要算进 total_tokens。"""
    card_id = _card(db, "check_in", "ci-1")
    _add_in_card(db, "assistant", "今天打算做什么？", 1, card_id)
    _add_in_card(db, "user", "写代码", 2, card_id, reply_to="m1")

    w = assemble_window(db, CHANNEL)
    assert w.tail_count == 2
    assert len(w.messages) == 3
    assert w.total_tokens == sum(message_tokens(m["content"]) for m in w.messages)


def test_no_reference_when_the_last_message_is_not_a_reply(db):
    card_id = _card(db, "check_in", "ci-1")
    _add_in_card(db, "assistant", "今天打算做什么？", 1, card_id)
    _add_in_card(db, "user", "写代码", 2, card_id)

    assert len(assemble_window(db, CHANNEL).messages) == 2
