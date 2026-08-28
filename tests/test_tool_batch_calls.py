"""LT-176：单次工具调用的记录与幂等键。"""

import pytest

from bot.async_pipeline import ToolBatchRepository
from bot.database import Database


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "calls.db"))


@pytest.fixture
def repository(db):
    return ToolBatchRepository(db)


@pytest.fixture
def batch(repository):
    item, _ = repository.create_conversation_batch(
        channel_id="chan-1", after_message_id=0, through_message_id=10,
        last_user_message_id=10, execution_mode="apply")
    return item


def _record(repository, batch, index, *, tool_name="log_timeline_event",
            arguments=None, result=None, succeeded=True):
    return repository.record_call(
        batch["id"], index, tool_name=tool_name,
        arguments=arguments if arguments is not None else {"text": "吃了午饭"},
        result=result, succeeded=succeeded)


def test_a_call_is_recorded_with_its_arguments_and_result(repository, batch):
    record, written = _record(
        repository, batch, 0, result={"id": 42})
    assert written is True
    assert record["tool_name"] == "log_timeline_event"
    assert record["arguments"] == {"text": "吃了午饭"}
    assert record["result"] == {"id": 42}
    assert record["succeeded"] is True


def test_a_successful_call_is_never_overwritten(repository, batch):
    """幂等的核心：这一次做过了，重放时不会再做第二遍。"""
    _record(repository, batch, 0, result={"id": 42})
    record, written = _record(
        repository, batch, 0, arguments={"text": "别的内容"}, result={"id": 99})
    assert written is False
    assert record["result"] == {"id": 42}
    assert record["arguments"] == {"text": "吃了午饭"}


def test_a_failed_call_can_be_replaced_by_a_later_retry(repository, batch):
    """失败不能把这个序号永久占住，否则重试写不进结果。"""
    failed, written = _record(
        repository, batch, 0, result={"error": "上游超时"}, succeeded=False)
    assert written is True
    assert failed["succeeded"] is False

    retried, written = _record(repository, batch, 0, result={"id": 42})
    assert written is True
    assert retried["succeeded"] is True
    assert retried["result"] == {"id": 42}

    # 成功之后就固定了
    _, written = _record(repository, batch, 0, result={"id": 99})
    assert written is False


def test_identical_content_at_different_indexes_is_kept(repository, batch):
    """同一天又吃了一顿麻辣烫是合法的重复写入。

    键取「批次 id + 第几次调用」而不是内容哈希，正是为了不把这种合法的
    重复当成重试挡掉。
    """
    same = {"text": "吃了麻辣烫"}
    first, first_written = _record(repository, batch, 0, arguments=same)
    second, second_written = _record(repository, batch, 1, arguments=same)
    assert (first_written, second_written) == (True, True)
    assert first["arguments"] == second["arguments"]
    assert len(repository.calls(batch["id"])) == 2


def test_completed_calls_lists_only_the_successful_ones(repository, batch):
    _record(repository, batch, 0, result={"id": 1})
    _record(repository, batch, 1, result={"error": "失败"}, succeeded=False)
    _record(repository, batch, 2, result={"id": 3})

    done = repository.completed_calls(batch["id"])
    assert sorted(done) == [0, 2]
    assert done[0]["result"] == {"id": 1}
    assert len(repository.calls(batch["id"])) == 3


def test_a_resumed_batch_knows_what_it_already_did(repository, batch):
    """崩溃之后重新领到同一批：前面成功的几次要跳过，失败的要重做。"""
    _record(repository, batch, 0, result={"id": 1})
    _record(repository, batch, 1, result={"error": "断电"}, succeeded=False)

    done = repository.completed_calls(batch["id"])
    assert 0 in done and 1 not in done


def test_calls_are_scoped_to_their_batch(repository, batch):
    other, _ = repository.create_check_in_batch(
        channel_id="chan-1", source_ref="check_in:1:09:00",
        payload={"prompt": "x"}, execution_mode="apply")
    _record(repository, batch, 0)
    assert repository.calls(other["id"]) == []
    assert repository.completed_calls(other["id"]) == {}


def test_an_untouched_batch_has_no_calls(repository, batch):
    assert repository.calls(batch["id"]) == []
    assert repository.completed_calls(batch["id"]) == {}


@pytest.mark.parametrize("overrides", [
    {"index": -1},
    {"tool_name": "  "},
])
def test_bad_arguments_are_rejected(repository, batch, overrides):
    index = overrides.pop("index", 0)
    with pytest.raises(ValueError):
        _record(repository, batch, index, **overrides)


def test_a_call_without_arguments_or_result_is_allowed(repository, batch):
    record, written = repository.record_call(
        batch["id"], 0, tool_name="search_memory", succeeded=True)
    assert written is True
    assert record["arguments"] is None
    assert record["result"] is None
