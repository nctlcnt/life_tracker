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
    OutboundDeliveryRepository,
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


def _message(db, channel_id, role, content):
    return db.add_conversation_message(
        discord_message_id=f"{channel_id}:{role}:{content}",
        channel_id=channel_id,
        role=role,
        content=content,
        created_at=datetime.now(timezone.utc).isoformat(),
    )


# --- 聊天批次 ---------------------------------------------------------------


def test_first_enable_cuts_cursor_over_to_the_latest_existing_message(
        db, repository):
    old_user = _message(db, "chan-1", "user", "旧机制已经处理过")
    old_assistant = _message(db, "chan-1", "assistant", "旧回复")

    cutovers = repository.prepare_runtime(
        enabled=True, channel_ids=["chan-1"])

    assert cutovers == {"chan-1": old_assistant}
    assert repository.get_cursor("chan-1") == old_assistant
    assert repository.open_conversation_batch("chan-1") is None
    assert old_user < old_assistant


def test_enabled_restart_preserves_messages_not_yet_processed(db, repository):
    old_id = _message(db, "chan-1", "user", "cutover 前")
    repository.prepare_runtime(enabled=True, channel_ids=["chan-1"])
    new_id = _message(db, "chan-1", "user", "cutover 后待处理")

    cutovers = repository.prepare_runtime(
        enabled=True, channel_ids=["chan-1"])

    assert cutovers == {}
    assert repository.get_cursor("chan-1") == old_id
    assert new_id > repository.get_cursor("chan-1")


def test_reenable_skips_messages_handled_while_worker_was_disabled(
        db, repository):
    repository.prepare_runtime(enabled=True, channel_ids=["chan-1"])
    repository.prepare_runtime(enabled=False, channel_ids=["chan-1"])
    old_path_id = _message(db, "chan-1", "user", "关闭期间由旧路径处理")

    cutovers = repository.prepare_runtime(
        enabled=True, channel_ids=["chan-1"])

    assert cutovers == {"chan-1": old_path_id}
    assert repository.get_cursor("chan-1") == old_path_id


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
    """两个调用同时领同一批，只有一个能拿到。

    两个线程在栅栏上会合之后才发起领取，否则线程启动本身就比一次
    SQLite 事务慢得多，两次调用会前后错开，这条测试也就退化成
    「已经在跑的批次不会被再领一次」，证明不了并发下的互斥。
    """
    _conversation(repository)
    results = []
    barrier = threading.Barrier(2)

    def claim():
        barrier.wait()
        results.append(ToolBatchRepository(db).claim_next(now=NOW))

    threads = [threading.Thread(target=claim) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    claimed = [r for r in results if r is not None]
    assert len(claimed) == 1
    assert claimed[0]["attempt_count"] == 1


# --- 终态 -------------------------------------------------------------------


def _claimed(repository):
    _conversation(repository)
    return repository.claim_next(now=NOW)


def test_completing_stores_the_result_and_clears_the_lease(repository):
    batch = _claimed(repository)
    assert repository.mark_completed(
        batch["id"], batch["lease_token"],
        result={"wrote": ["timeline"]}, delivery_kind="reaction",
        last_run_id="run-1", now=_later(5)) is True

    done = repository.get(batch["id"])
    assert done["status"] == "completed"
    assert done["result"] == {"wrote": ["timeline"]}
    assert done["delivery_kind"] == "reaction"
    assert done["delivery_status"] == "pending"
    assert done["last_run_id"] == "run-1"
    assert done["completed_at"] is not None
    assert done["lease_token"] is None
    assert done["locked_at"] is None


def test_an_empty_result_needs_no_delivery(repository):
    batch = _claimed(repository)
    assert repository.mark_completed(
        batch["id"], batch["lease_token"], now=_later(5)) is True
    done = repository.get(batch["id"])
    assert (done["delivery_kind"], done["delivery_status"]) == (
        "none", "not_needed")


def test_a_result_that_needs_saying_is_marked_pending(repository):
    batch = _claimed(repository)
    repository.mark_completed(
        batch["id"], batch["lease_token"], delivery_kind="message",
        result={"say": "提醒设好了"}, now=_later(5))
    done = repository.get(batch["id"])
    assert (done["delivery_kind"], done["delivery_status"]) == (
        "message", "pending")


def test_completing_rejects_an_unknown_delivery_kind(repository):
    batch = _claimed(repository)
    with pytest.raises(ValueError):
        repository.mark_completed(
            batch["id"], batch["lease_token"], delivery_kind="embed")


def test_failing_keeps_the_error_and_needs_no_delivery(repository):
    batch = _claimed(repository)
    assert repository.mark_failed(
        batch["id"], batch["lease_token"], "重试预算用完", now=_later(5)) is True
    done = repository.get(batch["id"])
    assert done["status"] == "failed"
    assert done["last_error"] == "重试预算用完"
    assert done["completed_at"] is not None
    assert done["delivery_status"] == "not_needed"


def test_retrying_releases_the_lease_and_waits(repository):
    batch = _claimed(repository)
    assert repository.mark_retry(
        batch["id"], batch["lease_token"], "上游超时",
        retry_after_seconds=120, now=_later(5)) is True

    waiting = repository.get(batch["id"])
    assert waiting["status"] == "retry_wait"
    assert waiting["last_error"] == "上游超时"
    assert waiting["lease_token"] is None
    assert repository.claim_next(now=_later(60)) is None

    again = repository.claim_next(now=_later(200))
    assert again["id"] == batch["id"]
    assert again["attempt_count"] == 2


@pytest.mark.parametrize("finish", [
    lambda repo, b, token: repo.mark_completed(b, token),
    lambda repo, b, token: repo.mark_failed(b, token, "x"),
    lambda repo, b, token: repo.mark_retry(b, token, "x"),
])
def test_a_stale_token_cannot_finish_the_batch(repository, finish):
    """过期持有者缓过来也写不进结果，这正是 fencing 要防的。"""
    _conversation(repository)
    first = repository.claim_next(now=NOW)
    second = repository.claim_next(now=_later(LEASE_SECONDS + 1))
    assert finish(repository, first["id"], first["lease_token"]) is False
    assert repository.get(first["id"])["status"] == "running"
    assert finish(repository, second["id"], second["lease_token"]) is True


def test_a_batch_cannot_be_finished_twice(repository):
    batch = _claimed(repository)
    token = batch["lease_token"]
    assert repository.mark_completed(batch["id"], token) is True
    assert repository.mark_completed(batch["id"], token) is False


def test_finishing_frees_the_channel_for_the_next_batch(repository):
    batch = _claimed(repository)
    repository.mark_completed(batch["id"], batch["lease_token"])
    _, created = _conversation(
        repository, after_message_id=20, through_message_id=30,
        last_user_message_id=25)
    assert created is True


# --- 交付与运维查询 ---------------------------------------------------------


def test_pending_deliveries_lists_only_what_still_needs_sending(repository):
    said = _claimed(repository)
    repository.mark_completed(said["id"], said["lease_token"],
                              delivery_kind="message", now=_later(5))
    silent, _ = _check_in(repository)
    claimed_silent = repository.claim_next(now=_later(10))
    repository.mark_completed(claimed_silent["id"],
                              claimed_silent["lease_token"], now=_later(15))

    pending = repository.pending_deliveries()
    assert [item["id"] for item in pending] == [said["id"]]


def test_advancing_a_delivery_is_a_one_way_step(repository):
    batch = _claimed(repository)
    repository.mark_completed(batch["id"], batch["lease_token"],
                              delivery_kind="message", now=_later(5))

    assert repository.advance_delivery(
        batch["id"], from_status="pending", to_status="queued") is True
    # 心跳重复触发时，同一条结果不会被再送一次
    assert repository.advance_delivery(
        batch["id"], from_status="pending", to_status="queued") is False
    assert repository.advance_delivery(
        batch["id"], from_status="queued", to_status="sent") is True
    assert repository.get(batch["id"])["delivery_status"] == "sent"
    assert repository.pending_deliveries() == []


def test_advancing_rejects_an_unknown_status(repository):
    with pytest.raises(ValueError):
        repository.advance_delivery("x", from_status="pending",
                                    to_status="delivered")


def test_non_terminal_queries_track_what_is_still_open(db, repository):
    assert repository.non_terminal_count() == 0
    assert repository.non_terminal_ids() == []

    batch = _claimed(repository)
    check_in, _ = _check_in(repository)
    assert repository.non_terminal_count() == 2
    assert set(repository.non_terminal_ids()) == {batch["id"], check_in["id"]}

    repository.mark_completed(batch["id"], batch["lease_token"])
    assert repository.non_terminal_ids() == [check_in["id"]]

    other = ToolBatchRepository(db, worker_name="other_worker")
    assert other.non_terminal_count() == 0


def test_non_terminal_queries_can_guard_apply_rollback(repository):
    apply_batch, _ = _conversation(repository, execution_mode="apply")
    shadow_batch, _ = _check_in(repository, execution_mode="shadow")

    assert repository.non_terminal_count(execution_mode="apply") == 1
    assert repository.non_terminal_ids(execution_mode="apply") == [
        apply_batch["id"]
    ]
    assert repository.non_terminal_ids(execution_mode="shadow") == [
        shadow_batch["id"]
    ]

    with pytest.raises(ValueError, match="execution_mode"):
        repository.non_terminal_ids(execution_mode="invalid")


def test_unsettled_deliveries_also_block_worker_rollback(repository):
    _conversation(repository, execution_mode="apply")
    apply_batch = repository.claim_next(now=NOW)
    repository.mark_completed(
        apply_batch["id"],
        apply_batch["lease_token"],
        delivery_kind="message",
    )
    _check_in(
        repository,
        source_ref="check_in:1:2026-08-28T10:00",
        execution_mode="shadow",
    )
    shadow_batch = repository.claim_next(now=NOW)
    repository.mark_completed(
        shadow_batch["id"],
        shadow_batch["lease_token"],
        delivery_kind="reaction",
    )

    assert repository.non_terminal_ids() == []
    assert repository.unsettled_delivery_ids(execution_mode="apply") == [
        apply_batch["id"]
    ]
    assert set(repository.unsettled_delivery_ids()) == {
        apply_batch["id"],
        shadow_batch["id"],
    }

    assert repository.advance_delivery(
        apply_batch["id"], from_status="pending", to_status="queued"
    )
    assert repository.unsettled_delivery_ids(execution_mode="apply") == [
        apply_batch["id"]
    ]
    assert repository.advance_delivery(
        apply_batch["id"], from_status="queued", to_status="sent"
    )
    assert repository.unsettled_delivery_ids(execution_mode="apply") == []


def test_queued_delivery_reconciles_from_the_persistent_outbox(db, repository):
    batch = _claimed(repository)
    repository.mark_completed(
        batch["id"], batch["lease_token"], delivery_kind="message"
    )
    assert repository.advance_delivery(
        batch["id"], from_status="pending", to_status="queued"
    )

    outbound = OutboundDeliveryRepository(db)
    delivery, _ = outbound.enqueue_message(
        channel_id="chan-1",
        content="done",
        source_type="tool_batch",
        source_id=batch["id"],
        dedupe_key=f"tool_batch:{batch['id']}:message",
    )
    claimed = outbound.claim_next(now=_later(10))
    assert claimed["id"] == delivery["id"]
    assert outbound.mark_sent(
        claimed["id"], claimed["lease_token"], ["discord-1"]
    )

    assert repository.reconcile_queued_deliveries() == {batch["id"]: "sent"}
    assert repository.get(batch["id"])["delivery_status"] == "sent"
    assert repository.reconcile_queued_deliveries() == {}


def test_advancing_to_not_needed_is_rejected(repository):
    """not_needed 只能在结束时一次写定，推进到它会与 delivery_kind 矛盾。"""
    batch = _claimed(repository)
    repository.mark_completed(batch["id"], batch["lease_token"],
                              delivery_kind="message", now=_later(5))
    with pytest.raises(ValueError):
        repository.advance_delivery(batch["id"], from_status="pending",
                                    to_status="not_needed")
    assert repository.get(batch["id"])["delivery_status"] == "pending"


# --- 重试预算用完 -----------------------------------------------------------


def _exhaust(repository, attempts):
    """反复领取并标记重试，把 attempt_count 堆到指定次数。"""
    for _ in range(attempts):
        claimed = repository.claim_next(now=NOW)
        assert claimed is not None
        repository.mark_retry(claimed["id"], claimed["lease_token"], "上游超时")


def test_reaping_gives_up_on_a_batch_that_used_its_budget(repository):
    batch, _ = _conversation(repository)
    _exhaust(repository, 3)

    reaped = repository.reap_exhausted(max_attempts=3)
    assert [item["id"] for item in reaped] == [batch["id"]]
    done = repository.get(batch["id"])
    assert done["status"] == "failed"
    assert done["last_error"] == "重试预算用完"
    assert done["completed_at"] is not None


def test_reaping_leaves_a_batch_still_within_budget(repository):
    batch, _ = _conversation(repository)
    _exhaust(repository, 2)
    assert repository.reap_exhausted(max_attempts=3) == []
    assert repository.get(batch["id"])["status"] == "retry_wait"


@pytest.mark.parametrize("status", ["pending", "running", "completed"])
def test_reaping_only_touches_batches_waiting_to_retry(
        db, repository, status):
    """只有 retry_wait 没有持有者，其余状态都不能从外面改。"""
    batch, _ = _conversation(repository)
    _set_status(db, batch["id"], status)
    conn = db._get_conn()
    conn.execute("UPDATE tool_batches SET attempt_count = 9 WHERE id = ?",
                 (batch["id"],))
    conn.commit()
    conn.close()
    assert repository.reap_exhausted(max_attempts=3) == []
    assert repository.get(batch["id"])["status"] == status


def test_reaping_advances_the_cursor_like_any_other_terminal_state(
        repository):
    batch, _ = _conversation(repository)
    _exhaust(repository, 3)
    repository.reap_exhausted(max_attempts=3)
    assert repository.get_cursor("chan-1") == batch["through_message_id"]


def test_reaping_a_check_in_batch_leaves_the_cursor_alone(repository):
    _check_in(repository)
    _exhaust(repository, 3)
    reaped = repository.reap_exhausted(max_attempts=3)
    assert len(reaped) == 1
    assert repository.get_cursor("chan-1") == 0


def test_reaping_is_scoped_to_one_worker(db, repository):
    _conversation(repository)
    _exhaust(repository, 3)
    other = ToolBatchRepository(db, worker_name="other_worker")
    assert other.reap_exhausted(max_attempts=3) == []
    assert len(repository.reap_exhausted(max_attempts=3)) == 1
