"""LT-175 第二步：建批与读取。

领取、续租、终态属于后面几步，本文件只覆盖 ToolBatchRepository 现在
提供的那几个方法。
"""

import threading
from datetime import datetime, timedelta, timezone

import pytest

from bot.async_pipeline import (
    TOOL_WORKER,
    BatchSourceConflict,
    ToolBatchRepository,
)
from bot.async_pipeline.repository import _utc_text
from bot.async_pipeline.tool_batches import LEASE_SECONDS
from bot.database import Database


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "tool_batches.db"))


@pytest.fixture
def repository(db):
    return ToolBatchRepository(db)


def _conversation(repository, **overrides):
    kwargs = {
        "channel_id": "chan-1",
        "after_message_id": 10,
        "through_message_id": 20,
        "last_user_message_id": 18,
        "execution_mode": "shadow",
    }
    kwargs.update(overrides)
    return repository.create_conversation_batch(**kwargs)


def _check_in(repository, **overrides):
    kwargs = {
        "channel_id": "chan-1",
        "source_ref": "check_in:1:2026-08-28T09:00",
        "payload": {"prompt": "随手说一句"},
        "execution_mode": "shadow",
    }
    kwargs.update(overrides)
    return repository.create_check_in_batch(**kwargs)


def _set_status(db, batch_id, status):
    conn = db._get_conn()
    conn.execute("UPDATE tool_batches SET status = ? WHERE id = ?",
                 (status, batch_id))
    conn.commit()
    conn.close()


# --- 聊天批次 ---------------------------------------------------------------


def test_conversation_batch_is_created_with_the_expected_fields(repository):
    batch, created = _conversation(repository)
    assert created is True
    assert batch["worker_name"] == TOOL_WORKER
    assert batch["source_kind"] == "conversation"
    assert (batch["after_message_id"], batch["through_message_id"]) == (10, 20)
    assert batch["last_user_message_id"] == 18
    assert batch["status"] == "pending"
    assert batch["attempt_count"] == 0
    assert batch["delivery_kind"] == "none"
    assert batch["source_ref"] is None
    assert batch["input"] is None


def test_the_same_range_returns_the_existing_batch(repository):
    first, _ = _conversation(repository)
    second, created = _conversation(repository)
    assert created is False
    assert second["id"] == first["id"]


def test_a_finished_range_is_not_rebuilt(db, repository):
    """区间唯一约束不区分状态，同一段消息不会被处理第二遍。"""
    first, _ = _conversation(repository)
    _set_status(db, first["id"], "completed")
    second, created = _conversation(repository)
    assert created is False
    assert second["id"] == first["id"]
    assert second["status"] == "completed"


def test_a_new_range_waits_while_the_previous_batch_is_open(repository):
    """频道里还有没干完的一批时，新区间拿回的是那一批，不是新建。"""
    first, _ = _conversation(repository)
    second, created = _conversation(
        repository, after_message_id=20, through_message_id=30,
        last_user_message_id=25)
    assert created is False
    assert second["id"] == first["id"]
    assert (second["after_message_id"], second["through_message_id"]) == (10, 20)


@pytest.mark.parametrize("terminal_status", ["completed", "failed"])
def test_a_new_range_starts_once_the_previous_batch_ends(
        db, repository, terminal_status):
    first, _ = _conversation(repository)
    _set_status(db, first["id"], terminal_status)
    second, created = _conversation(
        repository, after_message_id=20, through_message_id=30,
        last_user_message_id=25)
    assert created is True
    assert second["id"] != first["id"]


def test_channels_do_not_block_each_other(repository):
    first, _ = _conversation(repository)
    second, created = _conversation(repository, channel_id="chan-2")
    assert created is True
    assert second["id"] != first["id"]


def test_a_batch_may_cover_only_assistant_messages(repository):
    """区间里没有用户消息时 last_user_message_id 为空，仍然可以建批。"""
    batch, created = _conversation(repository, last_user_message_id=None)
    assert created is True
    assert batch["last_user_message_id"] is None


# --- check-in 批次 ----------------------------------------------------------


def test_check_in_batch_is_created_with_its_payload(repository):
    batch, created = _check_in(repository)
    assert created is True
    assert batch["source_kind"] == "check_in"
    assert batch["source_ref"] == "check_in:1:2026-08-28T09:00"
    assert batch["input"] == {"prompt": "随手说一句"}
    assert batch["after_message_id"] is None
    assert batch["last_user_message_id"] is None


def test_the_same_trigger_is_idempotent_across_restarts(repository):
    """调度器重启之后重复建批，拿回的是同一批。"""
    first, _ = _check_in(repository)
    second, created = _check_in(repository)
    assert created is False
    assert second["id"] == first["id"]


def test_different_triggers_of_one_check_in_are_separate_batches(repository):
    first, _ = _check_in(repository)
    second, created = _check_in(
        repository, source_ref="check_in:1:2026-08-28T21:00")
    assert created is True
    assert second["id"] != first["id"]


def test_reusing_a_source_ref_for_other_content_is_rejected(repository):
    _check_in(repository)
    with pytest.raises(BatchSourceConflict):
        _check_in(repository, payload={"prompt": "别的内容"})
    with pytest.raises(BatchSourceConflict):
        _check_in(repository, channel_id="chan-2")


def test_check_in_batches_do_not_occupy_the_conversation_slot(repository):
    """主动联系和聊天各走各的，前者不会挡住后者建批。"""
    _check_in(repository)
    _, created = _conversation(repository)
    assert created is True


def test_an_open_check_in_batch_does_not_block_another_trigger(repository):
    first, _ = _check_in(repository)
    second, created = _check_in(
        repository, source_ref="check_in:2:2026-08-28T21:00")
    assert created is True
    assert first["status"] == second["status"] == "pending"


# --- execution_mode 在建批时冻结 --------------------------------------------


def test_execution_mode_is_frozen_at_creation(repository):
    """开关中途切换不影响已经建好的批次。"""
    first, _ = _check_in(repository, execution_mode="shadow")
    second, created = _check_in(repository, execution_mode="apply")
    assert created is False
    assert second["id"] == first["id"]
    assert second["execution_mode"] == "shadow"


# --- 读 ---------------------------------------------------------------------


def test_get_returns_none_for_an_unknown_id(repository):
    assert repository.get("does-not-exist") is None


def test_get_parses_the_stored_payload(repository):
    batch, _ = _check_in(repository)
    assert repository.get(batch["id"])["input"] == {"prompt": "随手说一句"}


def test_open_conversation_batch_tracks_the_channel_state(db, repository):
    assert repository.open_conversation_batch("chan-1") is None
    batch, _ = _conversation(repository)
    assert repository.open_conversation_batch("chan-1")["id"] == batch["id"]
    assert repository.open_conversation_batch("chan-2") is None
    _set_status(db, batch["id"], "completed")
    assert repository.open_conversation_batch("chan-1") is None


@pytest.mark.parametrize("open_status", ["pending", "running", "retry_wait"])
def test_open_conversation_batch_covers_every_non_terminal_status(
        db, repository, open_status):
    batch, _ = _conversation(repository)
    _set_status(db, batch["id"], open_status)
    assert repository.open_conversation_batch("chan-1")["id"] == batch["id"]


# --- 参数校验 ---------------------------------------------------------------


@pytest.mark.parametrize("overrides", [
    {"channel_id": "  "},
    {"execution_mode": "dry_run"},
    {"after_message_id": 20, "through_message_id": 10},
    {"last_user_message_id": 10},   # 等于 after，起点是开区间
    {"last_user_message_id": 21},   # 超过 through
])
def test_conversation_batch_rejects_bad_arguments(repository, overrides):
    with pytest.raises(ValueError):
        _conversation(repository, **overrides)


@pytest.mark.parametrize("overrides", [
    {"channel_id": ""},
    {"execution_mode": "dry_run"},
    {"source_ref": " "},
    {"payload": {}},
    {"payload": "not a dict"},
])
def test_check_in_batch_rejects_bad_arguments(repository, overrides):
    with pytest.raises(ValueError):
        _check_in(repository, **overrides)


def test_a_rejected_argument_leaves_no_row_behind(db, repository):
    with pytest.raises(ValueError):
        _conversation(repository, execution_mode="dry_run")
    conn = db._get_conn()
    assert conn.execute("SELECT COUNT(*) FROM tool_batches").fetchone()[0] == 0
    conn.close()


# --- 领取与 fencing ---------------------------------------------------------


# 建批时 available_at 与 created_at 由数据库按真实时间写入，所以基准时刻
# 必须晚于它们，否则待领取的批次会被判成「还没到时候」。取一小时之后，
# 测试因此不绑定任何具体日期。
NOW = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=1)


def _later(seconds: float) -> datetime:
    return NOW + timedelta(seconds=seconds)


def test_claiming_marks_the_batch_running(repository):
    batch, _ = _conversation(repository)
    claimed = repository.claim_next(now=NOW)
    assert claimed["id"] == batch["id"]
    assert claimed["status"] == "running"
    assert claimed["attempt_count"] == 1
    assert claimed["lease_token"]
    assert claimed["locked_at"] is not None


def test_claiming_an_empty_table_returns_none(repository):
    assert repository.claim_next(now=NOW) is None


def test_a_running_batch_is_not_claimed_again_while_the_lease_holds(
        repository):
    _conversation(repository)
    first = repository.claim_next(now=NOW)
    assert first is not None
    assert repository.claim_next(now=_later(LEASE_SECONDS - 1)) is None


def test_an_expired_lease_is_reclaimed_on_the_next_claim(repository):
    """卡住的批次：上一个持有者把它标成 running 之后进程退出了。"""
    _conversation(repository)
    first = repository.claim_next(now=NOW)
    second = repository.claim_next(now=_later(LEASE_SECONDS + 1))
    assert second is not None
    assert second["id"] == first["id"]
    assert second["attempt_count"] == 2
    assert second["lease_token"] != first["lease_token"]


def test_the_previous_holder_can_no_longer_write(repository):
    """fencing 的意义：过期持有者缓过来也改不动这一行。"""
    _conversation(repository)
    first = repository.claim_next(now=NOW)
    second = repository.claim_next(now=_later(LEASE_SECONDS + 1))
    assert repository.renew_lease(
        first["id"], first["lease_token"], now=_later(LEASE_SECONDS + 2)
    ) is False
    assert repository.renew_lease(
        second["id"], second["lease_token"], now=_later(LEASE_SECONDS + 2)
    ) is True


def test_renewing_pushes_the_expiry_out(repository):
    _conversation(repository)
    claimed = repository.claim_next(now=NOW)
    assert repository.renew_lease(
        claimed["id"], claimed["lease_token"], now=_later(60)) is True
    # 续租之后，原本该过期的时刻还不会被回收
    assert repository.claim_next(now=_later(LEASE_SECONDS + 1)) is None
    assert repository.claim_next(now=_later(LEASE_SECONDS + 61)) is not None


def test_renewing_an_unknown_or_finished_batch_fails(db, repository):
    _conversation(repository)
    claimed = repository.claim_next(now=NOW)
    assert repository.renew_lease("does-not-exist", "x", now=NOW) is False
    assert repository.renew_lease(claimed["id"], "wrong-token", now=NOW) is False
    _set_status(db, claimed["id"], "completed")
    assert repository.renew_lease(
        claimed["id"], claimed["lease_token"], now=NOW) is False


@pytest.mark.parametrize("terminal_status", ["completed", "failed"])
def test_terminal_batches_are_never_claimed(db, repository, terminal_status):
    batch, _ = _conversation(repository)
    _set_status(db, batch["id"], terminal_status)
    assert repository.claim_next(now=_later(86400)) is None


def test_a_batch_waiting_to_retry_is_not_claimed_early(db, repository):
    batch, _ = _conversation(repository)
    conn = db._get_conn()
    conn.execute(
        "UPDATE tool_batches SET status = 'retry_wait', available_at = ? "
        "WHERE id = ?",
        (_utc_text(_later(120)), batch["id"]))
    conn.commit()
    conn.close()
    assert repository.claim_next(now=_later(60)) is None
    assert repository.claim_next(now=_later(180))["id"] == batch["id"]


def test_batches_are_claimed_in_creation_order(repository):
    first, _ = _check_in(repository, source_ref="check_in:1:09:00")
    second, _ = _check_in(repository, source_ref="check_in:1:21:00")
    assert repository.claim_next(now=NOW)["id"] == first["id"]
    assert repository.claim_next(now=NOW)["id"] == second["id"]


def test_a_worker_only_claims_its_own_batches(db, repository):
    _conversation(repository)
    other = ToolBatchRepository(db, worker_name="other_worker")
    assert other.claim_next(now=NOW) is None
    assert repository.claim_next(now=NOW) is not None


def test_two_concurrent_claims_do_not_both_win(db, repository):
    """两个调用同时领同一批，只有一个能拿到。"""
    _conversation(repository)
    results = []

    def claim():
        results.append(ToolBatchRepository(db).claim_next(now=NOW))

    threads = [threading.Thread(target=claim) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    claimed = [r for r in results if r is not None]
    assert len(claimed) == 1
    assert claimed[0]["attempt_count"] == 1
