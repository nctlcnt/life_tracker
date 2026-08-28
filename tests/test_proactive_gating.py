"""LT-174：主动联系的无回应门禁。

门禁状态全部从 conversation_messages 推导，所以这些用例都直接往会话日志里
写消息，然后断言 check-in 是被放行还是被跳过。不需要构造额外的状态表，也
正因为如此，"重启之后状态还在吗"这个问题在这里等价于"换一个 Database 实例
读同一个文件还成立吗"。
"""
import asyncio
from datetime import datetime

from bot.database import Database


async def _noop_send(_message: str, **_metadata) -> None:
    return None


def _make_db(tmp_path) -> Database:
    db = Database(str(tmp_path / "gating_test.db"))
    for check_in in db.list_check_ins():
        db.update_check_in(check_in["id"], enabled=False)
    return db


def _add(db: Database, role: str, content: str, *, mid: str,
         source_type: str | None = None, source_id: str | None = None,
         channel_id: str = "chan-1") -> int | None:
    metadata = {"message_type": "MessageType.default"}
    if source_type:
        metadata["source_type"] = source_type
    if source_id:
        metadata["source_id"] = source_id
    return db.add_conversation_message(
        discord_message_id=mid,
        channel_id=channel_id,
        role=role,
        content=content,
        created_at=datetime(2026, 8, 27, 12, 0, 0).isoformat(),
        metadata=metadata,
    )


# ── proactive_backlog 的推导 ────────────────────────────────────────

def test_backlog_is_zero_when_user_spoke_last(tmp_path):
    db = _make_db(tmp_path)
    _add(db, "assistant", "早呀", mid="1", source_type="check_in", source_id="morning:x")
    _add(db, "user", "早", mid="2")

    assert db.proactive_backlog("chan-1")["unanswered"] == 0


def test_backlog_counts_proactive_messages_after_last_user_message(tmp_path):
    db = _make_db(tmp_path)
    _add(db, "user", "在的", mid="1")
    _add(db, "assistant", "问一句", mid="2", source_type="check_in", source_id="random_poll:a")
    _add(db, "assistant", "再问一句", mid="3", source_type="check_in", source_id="random_poll:b")

    assert db.proactive_backlog("chan-1")["unanswered"] == 2


def test_unmarked_history_is_not_counted_as_proactive(tmp_path):
    """LT-174 之前的消息没有来源标记，不能当成主动消息。

    否则门禁一上线就会因为历史里那些没标记的回复而永远锁死。
    """
    db = _make_db(tmp_path)
    _add(db, "user", "你好聪明", mid="1")
    _add(db, "assistant", "嘿嘿，校准成功", mid="2")   # 无来源标记的历史回复

    assert db.proactive_backlog("chan-1")["unanswered"] == 0


def test_chat_replies_do_not_count_as_proactive(tmp_path):
    """聊天回复是被动的，不该让下一次主动联系被拦住。"""
    db = _make_db(tmp_path)
    _add(db, "user", "在吗", mid="1")
    _add(db, "assistant", "在的", mid="2", source_type="chat", source_id="msg-1")

    assert db.proactive_backlog("chan-1")["unanswered"] == 0


def test_backlog_is_scoped_per_channel(tmp_path):
    db = _make_db(tmp_path)
    _add(db, "user", "这边说话了", mid="1", channel_id="chan-1")
    _add(db, "assistant", "那边自言自语", mid="2", channel_id="chan-2",
         source_type="check_in", source_id="random_poll:a")

    assert db.proactive_backlog("chan-1")["unanswered"] == 0
    assert db.proactive_backlog("chan-2")["unanswered"] == 1


def test_backlog_survives_a_restart(tmp_path):
    """换一个 Database 实例读同一个文件，结论必须一样。"""
    db = _make_db(tmp_path)
    _add(db, "user", "在的", mid="1")
    _add(db, "assistant", "问一句", mid="2", source_type="check_in", source_id="random_poll:a")
    assert db.proactive_backlog("chan-1")["unanswered"] == 1

    reopened = Database(str(tmp_path / "gating_test.db"))
    assert reopened.proactive_backlog("chan-1")["unanswered"] == 1


def test_user_speaking_again_resets_the_backlog(tmp_path):
    db = _make_db(tmp_path)
    _add(db, "user", "在的", mid="1")
    for i in range(5):
        _add(db, "assistant", f"自言自语 {i}", mid=f"a{i}",
             source_type="check_in", source_id=f"random_poll:{i}")
    assert db.proactive_backlog("chan-1")["unanswered"] == 5

    _add(db, "user", "我回来了", mid="99")
    assert db.proactive_backlog("chan-1")["unanswered"] == 0


# ── 门禁决策 ────────────────────────────────────────────────────────

def _scheduler_with_channel(db, monkeypatch, channel_id="chan-1"):
    import bot.scheduler as scheduler_module
    from bot.scheduler import Scheduler
    monkeypatch.setattr(scheduler_module.config, "CHANNEL_ID", channel_id,
                        raising=False)
    return Scheduler(db, _noop_send)


def test_exempt_check_in_is_never_gated(tmp_path, monkeypatch):
    """morning / bedtime 这类固定问候显式豁免，无人回应也照常发。"""
    db = _make_db(tmp_path)
    scheduler = _scheduler_with_channel(db, monkeypatch)
    _add(db, "user", "在的", mid="1")
    _add(db, "assistant", "自言自语", mid="2", source_type="check_in", source_id="random_poll:a")

    exempt = {"name": "morning", "skip_when_unanswered": 0}
    assert scheduler._unanswered_skip_reason(exempt) is None


def test_gated_check_in_is_skipped_when_nobody_replied(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    scheduler = _scheduler_with_channel(db, monkeypatch)
    _add(db, "user", "在的", mid="1")
    _add(db, "assistant", "自言自语", mid="2", source_type="check_in", source_id="random_poll:a")

    gated = {"name": "random_poll", "skip_when_unanswered": 1}
    assert scheduler._unanswered_skip_reason(gated) is not None


def test_gated_check_in_runs_once_after_the_user_speaks(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    scheduler = _scheduler_with_channel(db, monkeypatch)
    gated = {"name": "random_poll", "skip_when_unanswered": 1}

    _add(db, "user", "我回来了", mid="1")
    assert scheduler._unanswered_skip_reason(gated) is None

    _add(db, "assistant", "那我问一句", mid="2",
         source_type="check_in", source_id="random_poll:a")
    assert scheduler._unanswered_skip_reason(gated) is not None


def test_gate_stays_open_when_the_backlog_query_fails(tmp_path, monkeypatch):
    """查询出错不能让主动联系整个停摆。"""
    db = _make_db(tmp_path)
    scheduler = _scheduler_with_channel(db, monkeypatch)

    def boom(_channel_id):
        raise RuntimeError("db is busy")

    monkeypatch.setattr(db, "proactive_backlog", boom)
    gated = {"name": "random_poll", "skip_when_unanswered": 1}
    assert scheduler._unanswered_skip_reason(gated) is None


# ── 迁移与配置 ──────────────────────────────────────────────────────

def test_migration_gates_chat_style_check_ins_only(tmp_path):
    """随机轮询和采访默认纳入门禁，固定问候默认豁免。"""
    db = _make_db(tmp_path)
    by_name = {c["name"]: c for c in db.list_check_ins()}

    assert by_name["random_poll"]["skip_when_unanswered"] == 1
    for name in ("morning", "bedtime_1"):
        assert by_name[name]["skip_when_unanswered"] == 0


def test_the_flag_is_editable(tmp_path):
    db = _make_db(tmp_path)
    check_in = db.list_check_ins()[0]

    db.update_check_in(check_in["id"], skip_when_unanswered=True)
    assert db.get_check_in(check_in["id"])["skip_when_unanswered"] == 1

    db.update_check_in(check_in["id"], skip_when_unanswered=False)
    assert db.get_check_in(check_in["id"])["skip_when_unanswered"] == 0
