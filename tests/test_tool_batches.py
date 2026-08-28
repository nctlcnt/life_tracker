"""LT-175 第一步：tool_batches 的表结构与约束。

这一步只验证数据库自己拦得住哪些写入。领取、续租、终态这些操作属于
后面几步，本文件不涉及。
"""

import sqlite3
import uuid

import pytest

from bot.database import Database


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "tool_batches.db"))


@pytest.fixture
def conn(db):
    connection = db._get_conn()
    yield connection
    connection.close()


CONVERSATION = {
    "worker_name": "tool_worker",
    "channel_id": "chan-1",
    "source_kind": "conversation",
    "after_message_id": 10,
    "through_message_id": 20,
    "last_user_message_id": 18,
    "execution_mode": "shadow",
}

CHECK_IN = {
    "worker_name": "tool_worker",
    "channel_id": "chan-1",
    "source_kind": "check_in",
    "source_ref": "check_in:1:2026-08-28T09:00",
    "input_json": '{"prompt": "..."}',
    "execution_mode": "shadow",
}


def _insert(conn, base: dict, **overrides) -> str:
    """插入一行，overrides 里的 None 表示把该列显式置空。"""
    row = {"id": uuid.uuid4().hex, **base, **overrides}
    columns = ", ".join(row)
    placeholders = ", ".join("?" for _ in row)
    conn.execute(
        f"INSERT INTO tool_batches ({columns}) VALUES ({placeholders})",
        tuple(row.values()),
    )
    conn.commit()
    return row["id"]


# --- 表结构 ----------------------------------------------------------------


def test_table_and_indexes_exist(conn):
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(tool_batches)")}
    assert {
        "id", "worker_name", "channel_id", "source_kind", "source_ref",
        "after_message_id", "through_message_id", "last_user_message_id",
        "input_json", "execution_mode", "status", "attempt_count",
        "available_at", "locked_at", "lease_token", "last_run_id",
        "result_json", "delivery_kind", "delivery_status",
        "supersedes_batch_id", "last_error", "created_at", "updated_at",
        "completed_at",
    } == columns

    indexes = {r["name"] for r in conn.execute("PRAGMA index_list(tool_batches)")}
    assert "uq_tool_batches_open_conversation" in indexes
    assert "idx_tool_batches_claim" in indexes
    assert "idx_tool_batches_channel_through" in indexes


def test_defaults_are_pending_and_not_needed(conn):
    batch_id = _insert(conn, CONVERSATION)
    row = conn.execute(
        "SELECT * FROM tool_batches WHERE id = ?", (batch_id,)).fetchone()
    assert row["status"] == "pending"
    assert row["attempt_count"] == 0
    assert row["delivery_kind"] == "none"
    assert row["delivery_status"] == "not_needed"
    assert row["available_at"] is not None


# --- conversation 来源 ------------------------------------------------------


def test_conversation_batch_is_accepted(conn):
    assert _insert(conn, CONVERSATION)


@pytest.mark.parametrize("overrides", [
    {"after_message_id": None},
    {"through_message_id": None},
    {"source_ref": "check_in:1:x"},
    {"input_json": "{}"},
])
def test_conversation_batch_rejects_wrong_shape(conn, overrides):
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, CONVERSATION, **overrides)


def test_through_message_id_may_not_precede_after(conn):
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, CONVERSATION, after_message_id=20, through_message_id=10)


def test_single_message_batch_is_allowed(conn):
    """区间可以只覆盖一条消息。

    起点是开区间，所以这时 after 必须是那条消息的前一个 id；
    两端写成相等的话区间为空，反而放不进任何一条消息。
    """
    assert _insert(conn, CONVERSATION, after_message_id=9,
                   through_message_id=10, last_user_message_id=10)


def test_an_empty_range_cannot_carry_a_user_message(conn):
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, CONVERSATION, after_message_id=10,
                through_message_id=10, last_user_message_id=10)


@pytest.mark.parametrize("last_user_message_id", [10, 21])
def test_last_user_message_must_fall_inside_the_range(
        conn, last_user_message_id):
    """起点是开区间，终点是闭区间：等于 after 或超过 through 都不合法。"""
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, CONVERSATION, last_user_message_id=last_user_message_id)


def test_last_user_message_may_equal_through(conn):
    assert _insert(conn, CONVERSATION, last_user_message_id=20)


def test_conversation_batch_may_omit_last_user_message(conn):
    assert _insert(conn, CONVERSATION, last_user_message_id=None)


# --- check_in 来源 ----------------------------------------------------------


def test_check_in_batch_is_accepted(conn):
    assert _insert(conn, CHECK_IN)


@pytest.mark.parametrize("overrides", [
    {"source_ref": None},
    {"input_json": None},
    {"after_message_id": 10},
    {"through_message_id": 20},
    {"last_user_message_id": 18},
])
def test_check_in_batch_rejects_wrong_shape(conn, overrides):
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, CHECK_IN, **overrides)


def test_same_check_in_trigger_cannot_be_inserted_twice(conn):
    """同一次触发只能建一批，调度器重启之后重复建批会被数据库拒绝。"""
    _insert(conn, CHECK_IN)
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, CHECK_IN)


def test_different_triggers_of_the_same_check_in_are_separate_batches(conn):
    _insert(conn, CHECK_IN, source_ref="check_in:1:2026-08-28T09:00")
    assert _insert(conn, CHECK_IN, source_ref="check_in:1:2026-08-28T21:00")


# --- 枚举与取值范围 ---------------------------------------------------------


@pytest.mark.parametrize("column, value", [
    ("source_kind", "reminder"),
    ("execution_mode", "dry_run"),
    ("status", "cancelled"),
    ("delivery_kind", "embed"),
])
def test_enum_columns_reject_unknown_values(conn, column, value):
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, CONVERSATION, **{column: value})


def test_delivery_status_rejects_unknown_values(conn):
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, CONVERSATION, delivery_kind="message",
                delivery_status="delivered")


def test_attempt_count_may_not_be_negative(conn):
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, CONVERSATION, attempt_count=-1)


# --- 交付要求的一致性 -------------------------------------------------------


@pytest.mark.parametrize("delivery_kind, delivery_status", [
    ("none", "not_needed"),
    ("reaction", "pending"),
    ("message", "pending"),
    ("message", "sent"),
])
def test_consistent_delivery_pairs_are_accepted(
        conn, delivery_kind, delivery_status):
    assert _insert(conn, CONVERSATION, delivery_kind=delivery_kind,
                   delivery_status=delivery_status)


@pytest.mark.parametrize("delivery_kind, delivery_status", [
    ("none", "pending"),
    ("message", "not_needed"),
    ("reaction", "not_needed"),
])
def test_inconsistent_delivery_pairs_are_rejected(
        conn, delivery_kind, delivery_status):
    """要么不需要交付，要么两列都说明交付方式，不允许只写一半。"""
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, CONVERSATION, delivery_kind=delivery_kind,
                delivery_status=delivery_status)


# --- 唯一约束与部分唯一索引 -------------------------------------------------


def test_the_same_message_range_cannot_be_claimed_twice(conn):
    """同一段消息区间只允许存在一批，与状态无关。

    这条约束和下面那条部分唯一索引管的是两件事：这条防的是同一段消息
    被处理两遍（哪怕上一批已经结束），那条防的是同一个频道同时开着
    两批没干完的活。
    """
    first = _insert(conn, CONVERSATION)
    conn.execute("UPDATE tool_batches SET status = 'completed' WHERE id = ?",
                 (first,))
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, CONVERSATION)


def test_only_one_open_conversation_batch_per_channel(conn):
    _insert(conn, CONVERSATION)
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, CONVERSATION, after_message_id=20,
                through_message_id=30, last_user_message_id=25)


@pytest.mark.parametrize("terminal_status", ["completed", "failed"])
def test_a_terminal_batch_frees_the_channel(conn, terminal_status):
    first = _insert(conn, CONVERSATION)
    conn.execute("UPDATE tool_batches SET status = ? WHERE id = ?",
                 (terminal_status, first))
    conn.commit()
    assert _insert(conn, CONVERSATION, after_message_id=20,
                   through_message_id=30, last_user_message_id=25)


@pytest.mark.parametrize("open_status", ["pending", "running", "retry_wait"])
def test_every_non_terminal_status_keeps_the_channel_occupied(
        conn, open_status):
    _insert(conn, CONVERSATION, status=open_status)
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, CONVERSATION, after_message_id=20,
                through_message_id=30, last_user_message_id=25)


def test_other_channels_are_unaffected(conn):
    _insert(conn, CONVERSATION)
    assert _insert(conn, CONVERSATION, channel_id="chan-2")


def test_check_in_batches_do_not_occupy_the_conversation_slot(conn):
    """部分唯一索引只约束 conversation；主动联系可以与聊天批次并存。"""
    _insert(conn, CONVERSATION)
    assert _insert(conn, CHECK_IN)
    assert _insert(conn, CHECK_IN, source_ref="check_in:2:2026-08-28T21:00")


def test_a_different_worker_name_defeats_the_channel_constraint(conn):
    """这条约束按 worker_name 分组判断，所以取值必须全局统一。

    这不是想要的行为，而是把「worker_name 用同一个常量」这条要求
    钉在测试里：一旦有人写了别的值，这条索引就不再起作用。
    """
    _insert(conn, CONVERSATION)
    assert _insert(conn, CONVERSATION, worker_name="other_worker",
                   after_message_id=20, through_message_id=30,
                   last_user_message_id=25)


# --- 外键的现状 -------------------------------------------------------------


def test_foreign_keys_are_declared_but_not_enforced(conn):
    """记录当前行为：_get_conn() 不开 PRAGMA foreign_keys，两个引用不校验。

    打开它会同时影响库里所有的表，属于独立决定，不在 LT-175 范围内。
    """
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 0
    assert _insert(conn, CONVERSATION, last_run_id="run-does-not-exist",
                   supersedes_batch_id="batch-does-not-exist")
