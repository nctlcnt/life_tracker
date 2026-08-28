"""LT-177：心跳的补漏、投递、收尾与报警。"""

import asyncio
import itertools

import pytest

from bot.async_pipeline import (
    BatchHeartbeat,
    OutboundDeliveryRepository,
    OutboundQueue,
    ToolBatchRepository,
)
from bot.database import Database


CHANNEL = "chan-1"

# 每个测试都从 1 开始，这样 _say 写出的 discord-N 与它返回的行 id 一一对应，
# 断言里可以直接用行 id 拼出目标消息的 Discord id。
_sequence = itertools.count(1)

# 与「默认生成一个 id」区分开：显式传 None 表示这条消息就是没有 Discord id。
_AUTO = object()


@pytest.fixture(autouse=True)
def _reset_sequence():
    global _sequence
    _sequence = itertools.count(1)


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "heartbeat.db"))


@pytest.fixture
def repository(db):
    return ToolBatchRepository(db)


@pytest.fixture
def outbound(db):
    async def _transport(_delivery):
        return ["discord-out"]
    return OutboundQueue(OutboundDeliveryRepository(db), _transport)


@pytest.fixture
def alerts():
    return []


@pytest.fixture
def heartbeat(db, repository, outbound, alerts):
    return BatchHeartbeat(
        db, repository, outbound, channel_ids=[CHANNEL],
        execution_mode="apply", period_seconds=0.05,
        on_alert=lambda kind, payload: alerts.append((kind, payload)))


def _say(db, role, content="...", *, channel_id=CHANNEL,
         discord_message_id=_AUTO) -> int:
    n = next(_sequence)
    return db.add_conversation_message(
        discord_message_id=(f"discord-{n}" if discord_message_id is _AUTO
                            else discord_message_id),
        channel_id=channel_id, role=role, content=content,
        created_at=f"2026-08-28T10:{n % 60:02d}:00")


def _deliveries(db) -> list[dict]:
    conn = db._get_conn()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM outbound_deliveries ORDER BY id")]
    finally:
        conn.close()


def _finish(repository, *, delivery_kind, result=None):
    """把当前排着的那一批领走并结束，模拟 worker 干完了活。"""
    claimed = repository.claim_next()
    assert claimed is not None
    repository.mark_completed(claimed["id"], claimed["lease_token"],
                              delivery_kind=delivery_kind, result=result)
    return claimed


# --- 补漏 -------------------------------------------------------------------


def test_a_tick_opens_a_batch_for_new_messages(db, repository, heartbeat):
    said = _say(db, "user", "刚吃完午饭")
    result = asyncio.run(heartbeat.tick())
    assert result["planned"][0]["action"] == "created"
    assert repository.open_conversation_batch(CHANNEL)[
        "through_message_id"] == said


def test_a_tick_with_nothing_to_do_is_harmless(db, heartbeat):
    result = asyncio.run(heartbeat.tick())
    assert result["planned"][0]["action"] == "idle"
    assert result["delivered"] == []
    assert result["reaped"] == []


def test_every_configured_channel_is_planned(db, repository, outbound):
    heartbeat = BatchHeartbeat(db, repository, outbound,
                               channel_ids=[CHANNEL, "chan-2"],
                               execution_mode="apply")
    _say(db, "user", "这边")
    _say(db, "user", "那边", channel_id="chan-2")
    result = asyncio.run(heartbeat.tick())
    assert {p["channel_id"] for p in result["planned"]} == {CHANNEL, "chan-2"}
    assert all(p["action"] == "created" for p in result["planned"])


# --- 投递 -------------------------------------------------------------------


def test_a_routine_write_is_delivered_as_a_reaction(db, repository, heartbeat):
    said = _say(db, "user", "刚吃完午饭")
    asyncio.run(heartbeat.tick())
    batch = _finish(repository, delivery_kind="reaction")

    result = asyncio.run(heartbeat.tick())
    assert result["delivered"] == [batch["id"]]

    delivery = _deliveries(db)[0]
    assert delivery["kind"] == "reaction"
    assert delivery["reaction"] == "✅"
    assert delivery["target_discord_message_id"] == f"discord-{said}"
    assert delivery["source_type"] == "tool_batch"
    assert repository.get(batch["id"])["delivery_status"] == "queued"


def test_something_worth_saying_is_delivered_as_a_message(
        db, repository, heartbeat):
    _say(db, "user", "帮我设个提醒")
    asyncio.run(heartbeat.tick())
    batch = _finish(repository, delivery_kind="message",
                    result={"say": "提醒设好了"})

    asyncio.run(heartbeat.tick())
    delivery = _deliveries(db)[0]
    assert delivery["kind"] == "message"
    assert delivery["content"] == "提醒设好了"
    assert repository.get(batch["id"])["delivery_status"] == "queued"


def test_a_batch_with_nothing_to_deliver_is_left_alone(
        db, repository, heartbeat):
    _say(db, "user", "随便聊聊")
    asyncio.run(heartbeat.tick())
    _finish(repository, delivery_kind="none")

    result = asyncio.run(heartbeat.tick())
    assert result["delivered"] == []
    assert _deliveries(db) == []


def test_a_delivery_is_handed_off_only_once(db, repository, heartbeat):
    _say(db, "user", "刚吃完午饭")
    asyncio.run(heartbeat.tick())
    _finish(repository, delivery_kind="reaction")

    assert len(asyncio.run(heartbeat.tick())["delivered"]) == 1
    assert asyncio.run(heartbeat.tick())["delivered"] == []
    assert len(_deliveries(db)) == 1


def test_a_repeat_handoff_is_absorbed_by_the_dedupe_key(
        db, repository, heartbeat):
    """同一批结果交付两次，发送队列里仍然只有一条。

    这正是「先入队再推进」这个顺序成立的前提：进程若在入队之后、推进
    状态之前退出，那一行仍是 pending，下一轮会重新交一次，而这条测试
    证明重新交是幂等的。反过来的顺序会让那条投递停在 queued 却从没入
    过队，永久丢失。
    """
    _say(db, "user", "刚吃完午饭")
    asyncio.run(heartbeat.tick())
    batch = _finish(repository, delivery_kind="reaction")

    pending = repository.pending_deliveries()[0]
    assert asyncio.run(heartbeat._deliver_one(pending)) is True
    assert len(_deliveries(db)) == 1

    # 直接改库把状态退回 pending，模拟推进那一步没有落库。advance_delivery
    # 本身不接受退回 pending——那不是一次合法的推进，只是这里要造的故障。
    conn = db._get_conn()
    conn.execute("UPDATE tool_batches SET delivery_status = 'pending' "
                 "WHERE id = ?", (batch["id"],))
    conn.commit()
    conn.close()
    assert asyncio.run(heartbeat._deliver_one(pending)) is True
    assert len(_deliveries(db)) == 1
    assert repository.get(batch["id"])["delivery_status"] == "queued"


def test_one_bad_delivery_does_not_block_the_others(
        db, repository, heartbeat, alerts, monkeypatch):
    """一条投递出意外，后面几条照样要交出去。"""
    # 这里只排批、不投递：跑整轮 tick 的话第一批会先被交出去，
    # 后面就造不出「两条都等着投递」的局面了。
    _say(db, "user", "第一件事")
    heartbeat._plan()
    first = _finish(repository, delivery_kind="message",
                    result={"say": "第一句"})
    _say(db, "user", "第二件事")
    heartbeat._plan()
    second = _finish(repository, delivery_kind="message",
                     result={"say": "第二句"})

    original = heartbeat.outbound.enqueue_message

    async def flaky(*args, **kwargs):
        if kwargs.get("source_id") == first["id"]:
            raise RuntimeError("入队时炸了")
        return await original(*args, **kwargs)

    monkeypatch.setattr(heartbeat.outbound, "enqueue_message", flaky)
    delivered = asyncio.run(heartbeat._deliver())

    assert delivered == [second["id"]]
    assert ("delivery_failed", {"batch_id": first["id"],
                                "error": "入队时炸了"}) in alerts
    # 出错的那条没有推进状态，下一轮会重新交
    assert repository.get(first["id"])["delivery_status"] == "pending"


def test_a_missing_reaction_target_fails_the_delivery_with_an_alert(
        db, repository, heartbeat, alerts):
    """找不到目标消息时报警并标记失败，不能让异常掀掉整轮心跳。"""
    _say(db, "user", "刚吃完午饭", discord_message_id=None)
    asyncio.run(heartbeat.tick())
    batch = _finish(repository, delivery_kind="reaction")

    result = asyncio.run(heartbeat.tick())
    assert result["delivered"] == []
    assert repository.get(batch["id"])["delivery_status"] == "failed"
    assert _deliveries(db) == []
    assert [kind for kind, _ in alerts] == ["delivery_target_missing"]


def test_a_message_delivery_without_content_fails_with_an_alert(
        db, repository, heartbeat, alerts):
    _say(db, "user", "帮我设个提醒")
    asyncio.run(heartbeat.tick())
    batch = _finish(repository, delivery_kind="message", result={"wrote": []})

    asyncio.run(heartbeat.tick())
    assert repository.get(batch["id"])["delivery_status"] == "failed"
    assert [kind for kind, _ in alerts] == ["delivery_content_missing"]


def test_a_check_in_result_is_delivered_too(db, repository, heartbeat):
    """主动联系没有消息区间，但一样会有话要说。"""
    batch, _ = repository.create_check_in_batch(
        channel_id=CHANNEL, source_ref="check_in:1:09:00",
        payload={"prompt": "随手说一句"}, execution_mode="apply")
    claimed = repository.claim_next()
    repository.mark_completed(claimed["id"], claimed["lease_token"],
                              delivery_kind="message",
                              result={"say": "今天天气不错"})

    assert asyncio.run(heartbeat.tick())["delivered"] == [batch["id"]]
    assert _deliveries(db)[0]["content"] == "今天天气不错"


# --- 收尾 -------------------------------------------------------------------


def test_a_batch_out_of_retries_is_given_up_with_an_alert(
        db, repository, heartbeat, alerts):
    said = _say(db, "user", "记一下这件事")
    asyncio.run(heartbeat.tick())
    for _ in range(3):
        claimed = repository.claim_next()
        repository.mark_retry(claimed["id"], claimed["lease_token"], "上游超时")

    result = asyncio.run(heartbeat.tick())
    assert len(result["reaped"]) == 1
    batch = repository.get(result["reaped"][0])
    assert batch["status"] == "failed"
    assert batch["last_error"] == "重试预算用完"
    assert [kind for kind, _ in alerts] == ["batch_exhausted"]
    # 终态就要推进游标，否则下一轮会一直重排同一段消息
    assert repository.get_cursor(CHANNEL) == said


def test_a_batch_still_within_budget_is_left_alone(db, repository, heartbeat):
    _say(db, "user", "记一下这件事")
    asyncio.run(heartbeat.tick())
    claimed = repository.claim_next()
    repository.mark_retry(claimed["id"], claimed["lease_token"], "上游超时")

    assert asyncio.run(heartbeat.tick())["reaped"] == []
    assert repository.get(claimed["id"])["status"] == "retry_wait"


# --- 报警 -------------------------------------------------------------------


def test_a_growing_backlog_raises_an_alert(db, repository, outbound, alerts):
    heartbeat = BatchHeartbeat(
        db, repository, outbound, channel_ids=[CHANNEL],
        execution_mode="apply", backlog_threshold=3,
        on_alert=lambda kind, payload: alerts.append((kind, payload)))
    for n in range(3):
        _say(db, "user", f"第 {n} 句")

    result = asyncio.run(heartbeat.tick())
    assert result["backlog"] == [{"channel_id": CHANNEL, "pending": 3}]
    assert ("backlog", {"channel_id": CHANNEL, "pending": 3}) in alerts


def test_a_short_backlog_stays_quiet(db, repository, outbound, alerts):
    heartbeat = BatchHeartbeat(
        db, repository, outbound, channel_ids=[CHANNEL],
        execution_mode="apply", backlog_threshold=10,
        on_alert=lambda kind, payload: alerts.append((kind, payload)))
    _say(db, "user", "就一句")
    assert asyncio.run(heartbeat.tick())["backlog"] == []
    assert alerts == []


def test_an_alert_callback_that_throws_does_not_break_the_tick(
        db, repository, outbound):
    heartbeat = BatchHeartbeat(
        db, repository, outbound, channel_ids=[CHANNEL],
        execution_mode="apply", backlog_threshold=1,
        on_alert=lambda *_: (_ for _ in ()).throw(RuntimeError("告警本身炸了")))
    _say(db, "user", "一句话")
    assert asyncio.run(heartbeat.tick())["backlog"] != []


# --- 循环 -------------------------------------------------------------------


def test_the_loop_runs_and_stops(db, repository, heartbeat):
    async def scenario():
        task = asyncio.create_task(heartbeat.run())
        await asyncio.sleep(0.15)
        assert heartbeat.running is True
        await heartbeat.stop()
        await asyncio.wait_for(task, timeout=1)
        assert heartbeat.running is False

    _say(db, "user", "刚吃完午饭")
    asyncio.run(scenario())
    assert repository.open_conversation_batch(CHANNEL) is not None


def test_a_failing_tick_does_not_stop_the_loop(
        db, repository, heartbeat, alerts, monkeypatch):
    """一轮出错不能让心跳整个停摆，那才是真正的静默失败。"""
    calls = []

    async def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("这一轮炸了")
        return {"planned": [], "reaped": [], "delivered": [], "backlog": []}

    monkeypatch.setattr(heartbeat, "tick", flaky)

    async def scenario():
        task = asyncio.create_task(heartbeat.run())
        await asyncio.sleep(0.2)
        await heartbeat.stop()
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(scenario())
    assert len(calls) >= 2
    assert ("heartbeat_tick_failed", {"error": "这一轮炸了"}) in alerts


def test_waking_the_heartbeat_does_not_require_a_second_loop(heartbeat):
    heartbeat.wake()
    assert heartbeat._wake.is_set()


def test_starting_twice_is_refused(heartbeat):
    async def scenario():
        task = asyncio.create_task(heartbeat.run())
        await asyncio.sleep(0.05)
        with pytest.raises(RuntimeError):
            await heartbeat.run()
        await heartbeat.stop()
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(scenario())
