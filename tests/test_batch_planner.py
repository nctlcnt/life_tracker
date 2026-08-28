"""LT-176：游标与建批规划。"""

import itertools

import pytest

from bot.async_pipeline import ToolBatchRepository, plan_next_batch
from bot.database import Database


CHANNEL = "chan-1"


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "planner.db"))


@pytest.fixture
def repository(db):
    return ToolBatchRepository(db)


_sequence = itertools.count(1)


def _say(db, role, content="...", *, channel_id=CHANNEL) -> int:
    """写一条会话消息，返回它的行 id。"""
    n = next(_sequence)
    return db.add_conversation_message(
        discord_message_id=f"discord-{n}",
        channel_id=channel_id,
        role=role,
        content=content,
        created_at=f"2026-08-28T10:{n % 60:02d}:00",
    )


def _plan(db, repository, **overrides):
    kwargs = {"channel_id": CHANNEL, "execution_mode": "shadow"}
    kwargs.update(overrides)
    return plan_next_batch(db, repository, **kwargs)


# --- 游标本身 ---------------------------------------------------------------


def test_a_fresh_channel_starts_at_zero(repository):
    assert repository.get_cursor(CHANNEL) == 0


def test_the_cursor_only_moves_forward(repository):
    assert repository.advance_cursor(CHANNEL, 10) is True
    assert repository.get_cursor(CHANNEL) == 10
    assert repository.advance_cursor(CHANNEL, 5) is False
    assert repository.advance_cursor(CHANNEL, 10) is False
    assert repository.get_cursor(CHANNEL) == 10
    assert repository.advance_cursor(CHANNEL, 11) is True
    assert repository.get_cursor(CHANNEL) == 11


def test_cursors_are_scoped_per_channel_and_worker(db, repository):
    repository.advance_cursor(CHANNEL, 10)
    assert repository.get_cursor("chan-2") == 0
    assert ToolBatchRepository(db, worker_name="other").get_cursor(CHANNEL) == 0


# --- 规划 -------------------------------------------------------------------


def test_no_messages_means_nothing_to_do(db, repository):
    assert _plan(db, repository)["action"] == "idle"


def test_a_user_message_opens_a_batch(db, repository):
    said = _say(db, "user", "刚吃完午饭")
    result = _plan(db, repository)
    assert result["action"] == "created"
    batch = result["batch"]
    assert batch["after_message_id"] == 0
    assert batch["through_message_id"] == said
    assert batch["last_user_message_id"] == said
    assert batch["execution_mode"] == "shadow"


def test_the_range_freezes_at_the_last_message_not_the_last_user_message(
        db, repository):
    """区间的终点是窗口末尾，即使末尾是日和自己的话。"""
    _say(db, "assistant", "在忙吗")
    user = _say(db, "user", "刚吃完午饭")
    tail = _say(db, "assistant", "吃的什么")
    batch = _plan(db, repository)["batch"]
    assert batch["through_message_id"] == tail
    assert batch["last_user_message_id"] == user


def test_only_the_latest_user_message_is_recorded_as_the_reaction_target(
        db, repository):
    _say(db, "user", "第一句")
    second = _say(db, "user", "第二句")
    batch = _plan(db, repository)["batch"]
    assert batch["last_user_message_id"] == second


def test_a_tail_of_her_own_messages_never_opens_a_batch(db, repository):
    """她自己说过的话不是事实，不能触发动手，但游标要越过它们。"""
    _say(db, "assistant", "在忙吗")
    last = _say(db, "assistant", "帮你记下了")
    result = _plan(db, repository)
    assert result["action"] == "skipped"
    assert result["skipped"] == 2
    assert repository.get_cursor(CHANNEL) == last
    assert repository.open_conversation_batch(CHANNEL) is None


def test_skipping_her_own_tail_does_not_swallow_a_later_user_message(
        db, repository):
    _say(db, "assistant", "在忙吗")
    assert _plan(db, repository)["action"] == "skipped"
    said = _say(db, "user", "在的")
    result = _plan(db, repository)
    assert result["action"] == "created"
    assert result["batch"]["last_user_message_id"] == said


def test_system_messages_alone_do_not_open_a_batch(db, repository):
    _say(db, "system", "服务重启")
    assert _plan(db, repository)["action"] == "skipped"


def test_planning_waits_while_a_batch_is_still_open(db, repository):
    _say(db, "user", "第一句")
    first = _plan(db, repository)
    assert first["action"] == "created"

    _say(db, "user", "第二句")
    second = _plan(db, repository)
    assert second["action"] == "waiting"
    assert second["batch"]["id"] == first["batch"]["id"]


# --- 游标与批次终态 ---------------------------------------------------------


def _finish_current(repository, now=None, **kwargs):
    claimed = repository.claim_next()
    assert claimed is not None
    return claimed, repository.mark_completed(
        claimed["id"], claimed["lease_token"], **kwargs)


def test_finishing_a_batch_advances_the_cursor(db, repository):
    said = _say(db, "user", "刚吃完午饭")
    _plan(db, repository)
    assert repository.get_cursor(CHANNEL) == 0

    _finish_current(repository, result={"wrote": ["timeline"]},
                    delivery_kind="reaction")
    assert repository.get_cursor(CHANNEL) == said


def test_an_empty_batch_advances_the_cursor_too(db, repository):
    """判断为不需要动手时也要推进，否则每一轮都会把老消息重排一遍。"""
    said = _say(db, "user", "随便聊聊")
    _plan(db, repository)
    _finish_current(repository)
    assert repository.get_cursor(CHANNEL) == said
    assert _plan(db, repository)["action"] == "idle"


def test_a_failed_batch_also_advances_the_cursor(db, repository):
    """失败同样是终态，不推进的话下一轮会一直重排同一段消息。

    这一批里的活确实没有做，但那要靠告诉用户来处理（第 4.3 节的降级
    阶梯），不能靠让游标停在原地无限重试。
    """
    said = _say(db, "user", "记一下这件事")
    _plan(db, repository)
    claimed = repository.claim_next()
    repository.mark_failed(claimed["id"], claimed["lease_token"], "重试用完")
    assert repository.get_cursor(CHANNEL) == said
    assert _plan(db, repository)["action"] == "idle"


def test_a_batch_waiting_to_retry_does_not_advance_the_cursor(db, repository):
    """重试不是终态，这一批还会再跑，游标不能往前走。"""
    _say(db, "user", "记一下这件事")
    _plan(db, repository)
    claimed = repository.claim_next()
    repository.mark_retry(claimed["id"], claimed["lease_token"], "上游超时")
    assert repository.get_cursor(CHANNEL) == 0
    assert _plan(db, repository)["action"] == "waiting"


def test_a_check_in_batch_leaves_the_cursor_alone(db, repository):
    """主动联系没有消息区间，与游标无关。"""
    _say(db, "user", "刚吃完午饭")
    batch, _ = repository.create_check_in_batch(
        channel_id=CHANNEL, source_ref="check_in:1:09:00",
        payload={"prompt": "随手说一句"}, execution_mode="shadow")
    claimed = repository.claim_next()
    assert claimed["id"] == batch["id"]
    repository.mark_completed(claimed["id"], claimed["lease_token"])
    assert repository.get_cursor(CHANNEL) == 0


def test_the_next_batch_starts_where_the_previous_one_stopped(db, repository):
    first_said = _say(db, "user", "第一句")
    _plan(db, repository)
    _finish_current(repository)

    second_said = _say(db, "user", "第二句")
    result = _plan(db, repository)
    assert result["action"] == "created"
    assert result["batch"]["after_message_id"] == first_said
    assert result["batch"]["through_message_id"] == second_said


def test_the_cursor_and_the_outcome_move_together(db, repository, monkeypatch):
    """游标推进失败时，终态也不能写进去。

    两者分两次写的话，中间崩溃会留下「批次已经结束、但游标停在它前面」
    的状态，下一轮规划会把同一段消息重新排一遍。
    """
    _say(db, "user", "刚吃完午饭")
    _plan(db, repository)
    claimed = repository.claim_next()

    def boom(*_args, **_kwargs):
        raise RuntimeError("写游标时断电")

    monkeypatch.setattr(repository, "_advance_cursor", boom)
    with pytest.raises(RuntimeError):
        repository.mark_completed(claimed["id"], claimed["lease_token"])

    assert repository.get(claimed["id"])["status"] == "running"
    assert repository.get_cursor(CHANNEL) == 0


# --- 频道之间互不影响 -------------------------------------------------------


def test_channels_are_planned_independently(db, repository):
    _say(db, "user", "这边说话")
    _plan(db, repository)

    other = _say(db, "user", "那边说话", channel_id="chan-2")
    result = _plan(db, repository, channel_id="chan-2")
    assert result["action"] == "created"
    assert result["batch"]["through_message_id"] == other
    assert repository.get_cursor(CHANNEL) == 0


def test_the_plan_limit_freezes_only_part_of_a_long_backlog(db, repository):
    ids = [_say(db, "user", f"第 {n} 句") for n in range(5)]
    result = _plan(db, repository, limit=3)
    assert result["batch"]["through_message_id"] == ids[2]

    _finish_current(repository)
    assert _plan(db, repository, limit=3)["batch"][
        "through_message_id"] == ids[4]
