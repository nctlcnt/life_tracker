"""LT-178 tool execution, replay, shadow/apply, and result expression."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from bot.async_pipeline import ToolBatchRepository, ToolResultExpresser, ToolWorker
from bot.async_pipeline.batch_planner import plan_next_batch
from bot.async_pipeline.worker_prompts import (
    build_result_expression_system,
    build_tool_worker_system,
    result_expression_request,
)
from bot.database import Database
from bot.memory import MemoryService
from bot.prompts import TOOL_ROUND_REMINDER


CHANNEL = "tool-worker-channel"


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "worker.db"))


@pytest.fixture
def repository(db):
    return ToolBatchRepository(db)


def add_message(db, role, content):
    return db.add_conversation_message(
        discord_message_id=f"{role}:{content}:{datetime.now().timestamp()}",
        channel_id=CHANNEL,
        role=role,
        content=content,
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def claim_conversation(db, repository, *, mode="apply"):
    plan = plan_next_batch(
        db,
        repository,
        channel_id=CHANNEL,
        execution_mode=mode,
    )
    assert plan["action"] == "created"
    claimed = repository.claim_next()
    assert claimed is not None
    return claimed


def claim_check_in(db, repository, *, mode="apply"):
    repository.create_check_in_batch(
        channel_id=CHANNEL,
        source_ref="check_in:1:2026-08-29T09:00",
        payload={
            "check_in_name": "afternoon",
            "timestamp": "2026-08-29 09:00",
            "prompt": "看看她在做什么",
        },
        execution_mode=mode,
    )
    claimed = repository.claim_next()
    assert claimed is not None
    return claimed


async def echo_expression(_db, _system, messages):
    payload = json.loads(messages[-1]["content"].split("\n", 1)[1])
    parts = [item["operation"] for item in payload["execution_results"]]
    parts.extend(item["value"] for item in payload["important_information"])
    return "；".join(parts), "expression-run"


class ProviderInterrupted(RuntimeError):
    """工具已经调完、provider 才掉线：这是会让整批重试的中断。

    输出不合契约不再走这条路——那由就地重写 JSON 处理，不推倒整批。
    """


def make_worker(
    db,
    repository,
    model_runner,
    *,
    expression=echo_expression,
    max_attempts=3,
):
    memory = MemoryService(db)
    expresser = ToolResultExpresser(
        db, expression, memory_service=memory
    )
    return ToolWorker(
        db,
        repository,
        model_runner,
        expresser,
        memory_service=memory,
        max_attempts=max_attempts,
        idle_poll_seconds=0.01,
    )


def execution(operation, status="succeeded", **details):
    return {"operation": operation, "status": status, "details": details}


def important(label, value):
    return {"label": label, "value": value}


def output(outcome, results=(), information=(), *, supersedes=False):
    return json.dumps(
        {
            "outcome": outcome,
            "execution_results": list(results),
            "important_information": list(information),
            "supersedes_previous": supersedes,
        },
        ensure_ascii=False,
    )


def test_tool_worker_prompt_is_persona_free_but_keeps_tool_policy(db):
    db.set_prompt_section("main_template", "PRIVATE PERSONA MARKER")
    db.set_prompt_section("tools", "APPLICATION TOOL POLICY MARKER")

    prompt = build_tool_worker_system(db)

    assert "portable background tool worker" in prompt
    assert "assistant's private internal work" in prompt
    assert "not a separate person or speaker" in prompt
    assert "APPLICATION TOOL POLICY MARKER" in prompt
    assert "PRIVATE PERSONA MARKER" not in prompt


def test_tool_results_are_the_same_assistants_internal_activity(db):
    prompt = build_result_expression_system(db)
    flat_prompt = " ".join(prompt.split())
    request = result_expression_request(
        outcome="completed",
        execution_results=(execution("记录写代码", time="11:57"),),
        important_information=(important("开始时间", "11:57"),),
    )

    assert "your own thought or completed action" in flat_prompt
    assert "address the current user directly as you/你" in flat_prompt
    assert "not a message from the user or another speaker" in request
    assert "你自己的脑内活动和行动结果" in TOOL_ROUND_REMINDER
    assert "称对方为“你”" in TOOL_ROUND_REMINDER


def test_empty_batch_advances_cursor_without_delivery(db, repository):
    row_id = add_message(db, "user", "嗯嗯")
    batch = claim_conversation(db, repository)

    async def model(*_args, **_kwargs):
        return output("empty"), "tool-run"

    asyncio.run(make_worker(db, repository, model).process(batch))

    done = repository.get(batch["id"])
    assert done["status"] == "completed"
    assert done["delivery_kind"] == "none"
    assert repository.get_cursor(CHANNEL) == row_id


def test_apply_executes_routine_write_and_requests_one_reaction(db, repository):
    add_message(db, "user", "刚吃完午饭")
    batch = claim_conversation(db, repository)
    event_start = datetime.now().replace(
        hour=12, minute=0, second=0, microsecond=0
    ).isoformat()

    async def model(_db, _system, _messages, *, tool_executor, **_kwargs):
        result = await tool_executor(
            "log_timeline_event",
            {
                "start_time": event_start,
                "content": "吃午饭",
                "category": "Routine",
            },
            0,
        )
        assert result["success"] is True
        return output(
            "completed",
            [execution("记录午饭", time="12:00")],
            [important("时间", "12:00")],
        ), "run-1"

    asyncio.run(make_worker(db, repository, model).process(batch))

    done = repository.get(batch["id"])
    assert done["delivery_kind"] == "reaction"
    assert done["delivery_status"] == "pending"
    assert len(repository.calls(batch["id"])) == 1
    assert len(db.get_today_events()) == 1


def test_created_database_id_cannot_be_marked_as_important(db, repository):
    add_message(db, "user", "记录午饭")
    batch = claim_conversation(db, repository)
    event_start = datetime.now().replace(
        hour=12, minute=0, second=0, microsecond=0
    ).isoformat()

    async def model(_db, _system, _messages, *, tool_executor, **_kwargs):
        result = await tool_executor(
            "log_timeline_event",
            {
                "start_time": event_start,
                "content": "吃午饭",
                "category": "Routine",
            },
            0,
        )
        return output(
            "completed",
            [execution("记录午饭", event_id=result["event_id"])],
            [important("事件编号", str(result["event_id"]))],
        ), "tool-run"

    asyncio.run(make_worker(db, repository, model).process(batch))

    assert repository.get(batch["id"])["status"] == "retry_wait"
    assert len(db.get_today_events()) == 1


def test_failed_routine_write_is_explained_instead_of_reacted_to(
        db, repository):
    add_message(db, "user", "删除不存在的事件")
    batch = claim_conversation(db, repository)

    async def model(_db, _system, _messages, *, tool_executor, **_kwargs):
        result = await tool_executor(
            "delete_timeline_event", {"event_id": 999999}, 0)
        assert result["success"] is False
        return output(
            "unable",
            [execution("删除事件", "failed", event_id=999999,
                       reason="没有找到对应事件")],
        ), "run-failed-write"

    asyncio.run(make_worker(db, repository, model).process(batch))

    done = repository.get(batch["id"])
    assert done["delivery_kind"] == "message"
    assert done["result"]["say"] == "删除事件"


def test_shadow_records_proposal_without_mutating_business_tables(db, repository):
    add_message(db, "user", "刚吃完午饭")
    batch = claim_conversation(db, repository, mode="shadow")
    event_start = datetime.now().replace(
        hour=12, minute=0, second=0, microsecond=0
    ).isoformat()

    async def model(_db, _system, _messages, *, tool_executor, **_kwargs):
        result = await tool_executor(
            "log_timeline_event",
            {
                "start_time": event_start,
                "content": "吃午饭",
                "category": "Routine",
            },
            0,
        )
        assert result["shadow"] is True
        return output("completed", [execution("记录午饭")]), "shadow-run"

    asyncio.run(make_worker(db, repository, model).process(batch))

    done = repository.get(batch["id"])
    assert done["delivery_kind"] == "none"
    assert db.get_today_events() == []
    assert repository.calls(batch["id"])[0]["succeeded"] is True


def test_shadow_failure_is_audited_but_never_delivered(db, repository):
    add_message(db, "user", "刚吃完午饭")
    batch = claim_conversation(db, repository, mode="shadow")

    async def invalid_model(*_args, **_kwargs):
        return "not json", "shadow-bad-run"

    worker = make_worker(
        db, repository, invalid_model, max_attempts=1
    )
    asyncio.run(worker.process(batch))

    done = repository.get(batch["id"])
    assert done["status"] == "failed"
    assert done["delivery_kind"] == "none"
    assert done["delivery_status"] == "not_needed"
    assert repository.pending_deliveries() == []


def test_repeated_side_effect_is_deduplicated_after_model_output_retry(
    db, repository
):
    """重试时模型若又发一遍同样的写入，副作用不可以发生第二次。"""
    add_message(db, "user", "刚吃完午饭")
    first = claim_conversation(db, repository)
    model_attempts = 0
    event_start = datetime.now().replace(
        hour=12, minute=0, second=0, microsecond=0
    ).isoformat()

    async def model(_db, _system, _messages, *, tool_executor, **_kwargs):
        nonlocal model_attempts
        model_attempts += 1
        await tool_executor(
            "log_timeline_event",
            {
                "start_time": event_start,
                "content": "吃午饭",
                "category": "Routine",
            },
            0,
        )
        if model_attempts == 1:
            raise ProviderInterrupted("provider dropped after the tool call")
        return output("completed", [execution("记录午饭")]), "good-run"

    worker = make_worker(db, repository, model)
    asyncio.run(worker.process(first))
    assert repository.get(first["id"])["status"] == "retry_wait"
    assert len(db.get_today_events()) == 1

    reclaimed = repository.claim_next(
        now=datetime.now(timezone.utc) + timedelta(seconds=2)
    )
    asyncio.run(worker.process(reclaimed))

    assert repository.get(first["id"])["status"] == "completed"
    assert len(db.get_today_events()) == 1
    assert len(repository.calls(first["id"])) == 1
    assert model_attempts == 2


def test_retry_continues_without_replaying_durable_calls(db, repository):
    add_message(db, "user", "午饭记录再补一句")
    first = claim_conversation(db, repository)
    model_attempts = 0
    event_id = None
    retry_prior_calls = []
    event_start = datetime.now().replace(
        hour=12, minute=0, second=0, microsecond=0
    ).isoformat()

    async def model(_db, _system, messages, *, tool_executor, **_kwargs):
        nonlocal event_id, model_attempts
        model_attempts += 1
        if model_attempts == 1:
            result = await tool_executor(
                "log_timeline_event",
                {
                    "start_time": event_start,
                    "content": "吃午饭",
                    "category": "Routine",
                },
                0,
            )
            event_id = result["event_id"]
            raise ProviderInterrupted("provider dropped after the tool call")

        prior_calls = json.loads(messages[-1]["content"]).get(
            "PRIOR_TOOL_CALLS"
        )
        retry_prior_calls.append(prior_calls)
        # 模型不重放已完成的调用，直接续接；provider 的序号仍从 0 开始。
        await tool_executor(
            "update_timeline_event",
            {"event_id": event_id, "notes": "自己做的炒饭"},
            0,
        )
        return output("completed", [execution("补充午饭记录")]), "good-run"

    asyncio.run(make_worker(db, repository, model).process(first))
    reclaimed = repository.claim_next(
        now=datetime.now(timezone.utc) + timedelta(seconds=2)
    )
    # 进程重启会让 provider 的序号重新从 0 开始，账本身份必须仍然连续。
    asyncio.run(make_worker(db, repository, model).process(reclaimed))

    done = repository.get(first["id"])
    calls = repository.calls(first["id"])
    assert done["status"] == "completed"
    assert [(item["call_index"], item["tool_name"]) for item in calls] == [
        (0, "log_timeline_event"),
        (1, "update_timeline_event"),
    ]
    assert retry_prior_calls[0][0]["tool_name"] == "log_timeline_event"
    assert retry_prior_calls[0][0]["succeeded"] is True
    assert db.get_event_by_id(event_id)["notes"] == "自己做的炒饭"


def test_retry_may_switch_tools_at_the_resumed_position(db, repository):
    """重试时改调另一个工具是正当续接，不再让整批失败。

    线上真实失败的形状：第一次尝试查完提醒就中断，重试时模型接着要设提醒。
    旧的位置绑定会把这种续接判成冲突，这里确认它现在可以走通。
    """
    add_message(db, "user", "等下提醒我喝水")
    first = claim_conversation(db, repository)
    model_attempts = 0
    trigger_time = (
        datetime.now() + timedelta(hours=1)
    ).replace(second=0, microsecond=0).isoformat()

    async def model(_db, _system, _messages, *, tool_executor, **_kwargs):
        nonlocal model_attempts
        model_attempts += 1
        if model_attempts == 1:
            await tool_executor("list_reminders", {}, 0)
            raise ProviderInterrupted("provider dropped after the tool call")
        await tool_executor(
            "set_reminder",
            {"trigger_time": trigger_time, "action": "喝水", "priority": "low"},
            0,
        )
        return output("completed", [execution("设好提醒")]), "good-run"

    asyncio.run(make_worker(db, repository, model).process(first))
    reclaimed = repository.claim_next(
        now=datetime.now(timezone.utc) + timedelta(seconds=2)
    )
    asyncio.run(make_worker(db, repository, model).process(reclaimed))

    assert repository.get(first["id"])["status"] == "completed"
    assert [
        (item["call_index"], item["tool_name"])
        for item in repository.calls(first["id"])
    ] == [(0, "list_reminders"), (1, "set_reminder")]
    assert len(db.list_active_reminders()) == 1


def test_empty_outcome_after_a_read_only_call_stays_silent(db, repository):
    """只查了查、什么都没动，「没什么可说的」就是合法终态，而且不出声。

    线上形状：用户随口说了句「喝了喝了～」，worker 查一下有没有待处理的提醒，
    列表是空的。旧规则不许它把这报成 empty，逼着它要么编个事实、要么谎称失
    败，三次重试全部撞墙，最后降级发出一条毫无根据的道歉。
    """
    add_message(db, "user", "喝了喝了～")
    batch = claim_conversation(db, repository)
    expressed = []

    async def expression(_db, _system, _messages):
        expressed.append(_messages)
        return "不该发出来的话", "expression-run"

    async def model(_db, _system, _messages, *, tool_executor, **_kwargs):
        await tool_executor("list_reminders", {}, 0)
        return output("empty"), "run-1"

    worker = make_worker(db, repository, model, expression=expression)
    asyncio.run(worker.process(batch))

    done = repository.get(batch["id"])
    assert done["status"] == "completed"
    assert done["attempt_count"] == 1
    assert not done["last_error"]
    assert done["delivery_kind"] == "none"
    assert expressed == []


def test_chat_track_silence_downgrades_a_write_to_a_reaction(db, repository):
    """聊天轨说这话刚才已经讲过了，就别逼它再讲一遍。

    线上形状：用户说困，聊天轨已经回过「去眯一会儿吧」，工具轨随后设好跟进
    提醒。表达层判断不必重复，返回 [SILENT]，旧代码把这当成失败，三次耗尽后
    发出一条「操作没成功」的道歉——提醒其实好好地设着。
    """
    add_message(db, "user", "好困哦")
    batch = claim_conversation(db, repository)
    trigger_time = (
        datetime.now() + timedelta(minutes=30)
    ).replace(second=0, microsecond=0).isoformat()

    async def expression(_db, _system, _messages):
        return "[SILENT]", "expression-run"

    async def model(_db, _system, _messages, *, tool_executor, **_kwargs):
        await tool_executor(
            "set_reminder",
            {"trigger_time": trigger_time, "action": "看看缓过来没", "priority": "low"},
            0,
        )
        return output("completed", [execution("设好跟进提醒")]), "run-1"

    worker = make_worker(db, repository, model, expression=expression)
    asyncio.run(worker.process(batch))

    done = repository.get(batch["id"])
    assert done["status"] == "completed"
    assert done["attempt_count"] == 1
    assert not done["last_error"]
    # 动过数据，所以留个反应作痕迹，但不说话
    assert done["delivery_kind"] == "reaction"
    assert len(db.list_active_reminders()) == 1


def test_chat_track_silence_after_a_pure_lookup_says_nothing(db, repository):
    """什么都没做还打个勾反而让人猜，所以纯查询的静默就是彻底安静。"""
    add_message(db, "user", "喝了喝了～")
    batch = claim_conversation(db, repository)

    async def expression(_db, _system, _messages):
        return "[SILENT]", "expression-run"

    async def model(_db, _system, _messages, *, tool_executor, **_kwargs):
        await tool_executor("list_reminders", {}, 0)
        return output("completed", [execution("查了提醒")]), "run-1"

    worker = make_worker(db, repository, model, expression=expression)
    asyncio.run(worker.process(batch))

    done = repository.get(batch["id"])
    assert done["status"] == "completed"
    assert done["delivery_kind"] == "none"


def test_exhausted_retries_do_not_apologise_for_finished_work(db, repository):
    """重试用尽不等于事情没做成，做成了就不该道歉。

    线上形状：两条 timeline 写入都成功了，卡住的是收尾。旧代码一律报 unable，
    于是用户收到「后台出了点小故障没操作成功」，而记录其实好好地躺在时间线上。
    """
    add_message(db, "user", "记一下午饭")
    batch = claim_conversation(db, repository)
    spoken = []
    event_start = datetime.now().replace(
        hour=12, minute=0, second=0, microsecond=0
    ).isoformat()

    async def expression(_db, _system, _messages):
        spoken.append(1)
        return "不该说出口的那句道歉", "expression-run"

    async def model(_db, _system, _messages, *, tool_executor, **_kwargs):
        await tool_executor(
            "log_timeline_event",
            {
                "start_time": event_start,
                "content": "吃午饭",
                "category": "Routine",
            },
            0,
        )
        raise ProviderInterrupted("provider dropped after the tool call")

    worker = make_worker(
        db, repository, model, expression=expression, max_attempts=1
    )
    asyncio.run(worker.process(batch))

    done = repository.get(batch["id"])
    assert done["status"] == "completed"
    # 动过数据，留个反应作痕迹，但一个字都不说
    assert done["delivery_kind"] == "reaction"
    assert spoken == []
    assert len(db.get_today_events()) == 1
    # 收场是降级来的，原因必须留着，否则和顺利完成的批次在 status 上没区别
    assert "ProviderInterrupted" in (done["last_error"] or "")


def test_exhausted_retries_still_speak_when_a_tool_really_failed(db, repository):
    """确实有东西没做成，那句话必须说出去。"""
    add_message(db, "user", "删掉那条不存在的记录")
    batch = claim_conversation(db, repository)

    async def model(_db, _system, _messages, *, tool_executor, **_kwargs):
        result = await tool_executor(
            "delete_timeline_event", {"event_id": 999999}, 0
        )
        assert result["success"] is False
        raise ProviderInterrupted("provider dropped after the failed call")

    worker = make_worker(db, repository, model, max_attempts=1)
    asyncio.run(worker.process(batch))

    done = repository.get(batch["id"])
    assert done["status"] == "completed"
    assert done["delivery_kind"] == "message"
    assert json.loads(done["result_json"])["outcome"] == "unable"


def test_exhausted_retries_stay_silent_when_nothing_was_done(db, repository):
    """一次工具都没调成，就没有什么可道歉的。"""
    add_message(db, "user", "随便说一句")
    batch = claim_conversation(db, repository)
    spoken = []

    async def expression(_db, _system, _messages):
        spoken.append(1)
        return "多余的道歉", "expression-run"

    async def model(*_args, **_kwargs):
        raise ProviderInterrupted("provider dropped before any tool call")

    worker = make_worker(
        db, repository, model, expression=expression, max_attempts=1
    )
    asyncio.run(worker.process(batch))

    done = repository.get(batch["id"])
    assert done["status"] == "completed"
    assert done["delivery_kind"] == "none"
    assert spoken == []


def test_malformed_output_is_repaired_in_place(db, repository):
    """输出不合契约只重写那份 JSON，不推倒整批重来。

    工具已经调完、副作用已经产生，重跑整批既浪费又会让用户收到一条与事实不
    符的失败反馈。
    """
    add_message(db, "user", "记一下午饭")
    batch = claim_conversation(db, repository)
    rounds = []
    event_start = datetime.now().replace(
        hour=12, minute=0, second=0, microsecond=0
    ).isoformat()

    async def model(_db, _system, messages, *, tool_executor, **_kwargs):
        rounds.append(messages)
        if len(rounds) == 1:
            await tool_executor(
                "log_timeline_event",
                {
                    "start_time": event_start,
                    "content": "吃午饭",
                    "category": "Routine",
                },
                0,
            )
            return "这不是 JSON", "bad-run"
        return output("completed", [execution("记录午饭")]), "good-run"

    asyncio.run(make_worker(db, repository, model).process(batch))

    done = repository.get(batch["id"])
    assert done["status"] == "completed"
    # 整批没有重来：领取次数仍是 1
    assert done["attempt_count"] == 1
    assert len(rounds) == 2
    assert len(db.get_today_events()) == 1
    assert len(repository.calls(batch["id"])) == 1
    # 重写轮能看到自己上次写了什么、被拒的理由是什么
    replayed = [item["content"] for item in rounds[1]]
    assert "这不是 JSON" in replayed
    assert any("OUTPUT_REJECTED" in item for item in replayed)


def test_repair_round_cannot_call_tools_again(db, repository):
    """重写轮不给工具：该调的已经调完，再给一次就会重复写入。"""
    add_message(db, "user", "记一下午饭")
    batch = claim_conversation(db, repository)
    rounds = []
    event_start = datetime.now().replace(
        hour=12, minute=0, second=0, microsecond=0
    ).isoformat()

    async def model(_db, _system, _messages, *, tool_executor, **_kwargs):
        rounds.append(1)
        await tool_executor(
            "log_timeline_event",
            {
                "start_time": event_start,
                "content": "吃午饭",
                "category": "Routine",
            },
            0,
        )
        return "这不是 JSON", "bad-run"

    worker = make_worker(db, repository, model, max_attempts=1)
    asyncio.run(worker.process(batch))

    # 第二轮想再写一次，被挡下来了，时间线上仍然只有第一轮那条
    assert len(rounds) == 2
    assert len(db.get_today_events()) == 1


def test_repair_gives_up_after_the_limit(db, repository):
    """重写次数用完才回到整批重试，不会无限重写下去。"""
    add_message(db, "user", "随便说一句")
    batch = claim_conversation(db, repository, mode="shadow")
    rounds = []

    async def model(*_args, **_kwargs):
        rounds.append(1)
        return "始终不是 JSON", "bad-run"

    worker = make_worker(db, repository, model, max_attempts=1)
    asyncio.run(worker.process(batch))

    # 一次正常输出 + 两次重写
    assert len(rounds) == 3
    assert repository.get(batch["id"])["status"] == "failed"


def test_provider_index_reset_within_one_attempt_does_not_collide(
    db, repository
):
    """同一次尝试里 provider 序号归零，账本身份必须继续往后排。

    preset 回退会在工具已经执行之后重跑整个调用壳，provider 的序号因此在同
    一次尝试内重新从 0 开始。序号若跟着 provider 走，第二个工具就会撞上第一
    个刚写下的账本行。
    """
    add_message(db, "user", "记一下午饭，再提醒我喝水")
    batch = claim_conversation(db, repository)
    event_start = datetime.now().replace(
        hour=12, minute=0, second=0, microsecond=0
    ).isoformat()
    trigger_time = (
        datetime.now() + timedelta(hours=1)
    ).replace(second=0, microsecond=0).isoformat()

    async def model(_db, _system, _messages, *, tool_executor, **_kwargs):
        await tool_executor(
            "log_timeline_event",
            {
                "start_time": event_start,
                "content": "吃午饭",
                "category": "Routine",
            },
            0,
        )
        # preset 回退后 provider 重新从 0 计数，这里刻意再传一次 0。
        await tool_executor(
            "set_reminder",
            {"trigger_time": trigger_time, "action": "喝水", "priority": "low"},
            0,
        )
        return output("completed", [execution("都办好了")]), "run-1"

    asyncio.run(make_worker(db, repository, model).process(batch))

    assert repository.get(batch["id"])["status"] == "completed"
    assert [
        (item["call_index"], item["tool_name"])
        for item in repository.calls(batch["id"])
    ] == [(0, "log_timeline_event"), (1, "set_reminder")]
    assert len(db.get_today_events()) == 1
    assert len(db.list_active_reminders()) == 1


def test_read_only_call_is_re_executed_on_retry(db, repository):
    """只读工具不参与去重，重跑一次可以拿到最新数据。"""
    add_message(db, "user", "我有哪些提醒")
    first = claim_conversation(db, repository)
    model_attempts = 0

    async def model(_db, _system, _messages, *, tool_executor, **_kwargs):
        nonlocal model_attempts
        model_attempts += 1
        await tool_executor("list_reminders", {}, 0)
        if model_attempts == 1:
            raise ProviderInterrupted("provider dropped after the tool call")
        return output(
            "completed",
            [execution("查了提醒")],
            [important("提醒数量", "0")],
        ), "good-run"

    asyncio.run(make_worker(db, repository, model).process(first))
    reclaimed = repository.claim_next(
        now=datetime.now(timezone.utc) + timedelta(seconds=2)
    )
    asyncio.run(make_worker(db, repository, model).process(reclaimed))

    assert repository.get(first["id"])["status"] == "completed"
    assert [
        (item["call_index"], item["tool_name"])
        for item in repository.calls(first["id"])
    ] == [(0, "list_reminders"), (1, "list_reminders")]


def test_retry_with_changed_arguments_writes_a_second_record(db, repository):
    """已知取舍：重试时模型改了主意，两次写入都会留在时间线上。

    参数指纹只认完全相同的调用，所以内容不同的第二次写入会照常执行。这是
    为了不误伤「先记午饭、再记洗澡」这类正当续接而付出的代价：第一次的副
    作用本来就已经发生，旧的位置绑定同样拦不住它，只是额外毁掉整个批次。
    """
    add_message(db, "user", "记录午饭")
    first = claim_conversation(db, repository)
    model_attempts = 0
    event_start = datetime.now().replace(
        hour=12, minute=0, second=0, microsecond=0
    ).isoformat()

    async def model(_db, _system, _messages, *, tool_executor, **_kwargs):
        nonlocal model_attempts
        model_attempts += 1
        content = "吃午饭" if model_attempts == 1 else "吃炒饭"
        await tool_executor(
            "log_timeline_event",
            {
                "start_time": event_start,
                "content": content,
                "category": "Routine",
            },
            0,
        )
        if model_attempts == 1:
            raise ProviderInterrupted("provider dropped after the tool call")
        return output("completed", [execution("记录午饭")]), "good-run"

    asyncio.run(make_worker(db, repository, model).process(first))
    reclaimed = repository.claim_next(
        now=datetime.now(timezone.utc) + timedelta(seconds=2)
    )
    asyncio.run(make_worker(db, repository, model).process(reclaimed))

    assert repository.get(first["id"])["status"] == "completed"
    assert sorted(item["content"] for item in db.get_today_events()) == [
        "吃午饭", "吃炒饭",
    ]


def test_reminder_result_is_expressed_with_latest_context_and_exact_terms(
    db, repository
):
    add_message(db, "user", "明天早上提醒我交作业")
    batch = claim_conversation(db, repository)
    captured = {}
    scheduler_wakeups = []

    def scheduler_wakeup():
        scheduler_wakeups.append(len(db.list_active_reminders()))

    db._on_reminder_added = scheduler_wakeup
    trigger_time = (
        datetime.now() + timedelta(days=1)
    ).replace(hour=8, minute=0, second=0, microsecond=0).isoformat()

    async def expression(_db, system, messages):
        captured["system"] = system
        captured["messages"] = messages
        return f"好，{trigger_time} 提醒你交作业", "expression-run"

    async def model(_db, _system, _messages, *, tool_executor, **_kwargs):
        await tool_executor(
            "set_reminder",
            {
                "trigger_time": trigger_time,
                "action": "交作业",
            },
            0,
        )
        return output(
            "completed",
            [execution("设置交作业提醒", trigger_time=trigger_time)],
            [important("提醒时间", trigger_time), important("事项", "交作业")],
        ), "tool-run"

    worker = make_worker(db, repository, model, expression=expression)
    asyncio.run(worker.process(batch))

    done = repository.get(batch["id"])
    assert done["delivery_kind"] == "message"
    assert done["result"]["say"] == f"好，{trigger_time} 提醒你交作业"
    assert "private execution track runs separately inside" in captured["system"]
    assert "EXECUTION_TRACK_RESULT" in captured["messages"][-1]["content"]
    assert scheduler_wakeups == [1]


def test_expression_naturally_formats_structured_important_information(
    db, repository
):
    async def lossy(_db, _system, _messages):
        return "好，明早提醒你", "lossy-run"

    expresser = ToolResultExpresser(db, lossy, memory_service=MemoryService(db))
    text = asyncio.run(expresser.express(
        channel_id=CHANNEL,
        outcome="completed",
        execution_results=(execution("设置提醒"),),
        important_information=(
            important("提醒时间", "2026-08-29T08:00:00"),
        ),
    ))
    assert text == "好，明早提醒你"


def test_expression_withholds_a_leaked_private_identifier(db):
    """带了内部 ID 的那句话不发出去，但也不因此毁掉整批。

    对泄露的正确反应是拦下这句话。旧代码抛错，于是整批重试到耗尽再道歉一
    句——既没拦住下一次，还平白告诉用户出了故障。
    """
    async def leaky(_db, _system, messages):
        assert "event_id" not in messages[-1]["content"]
        assert "reminder_id" not in messages[-1]["content"]
        return "记好了，event_id=715，16:20 提醒你喝水", "leaky-run"

    expresser = ToolResultExpresser(db, leaky, memory_service=MemoryService(db))
    said = asyncio.run(expresser.express(
        channel_id=CHANNEL,
        outcome="completed",
        execution_results=(execution(
            "记录咖啡并设置喝水提醒",
            event_id=715,
            reminder_id=452,
        ),),
        important_information=(important("提醒时间", "16:20"),),
    ))
    assert said is None


def test_check_in_silence_has_nowhere_to_put_a_reaction(db, repository):
    """check_in 没有用户消息可贴，静默就只能是彻底安静。

    反应贴在批次覆盖的最后一条用户消息上，check_in 批次的
    `last_user_message_id` 是空的。若仍然要求贴反应，投递会在找不到目标时
    失败并报警。
    """
    batch = claim_check_in(db, repository)
    trigger_time = (
        datetime.now() + timedelta(hours=1)
    ).replace(second=0, microsecond=0).isoformat()

    async def expression(_db, _system, _messages):
        return "[SILENT]", "expression-run"

    async def model(_db, _system, _messages, *, tool_executor, **_kwargs):
        await tool_executor(
            "set_reminder",
            {"trigger_time": trigger_time, "action": "跟进", "priority": "low"},
            0,
        )
        return output("completed", [execution("设好提醒")]), "run-1"

    worker = make_worker(db, repository, model, expression=expression)
    asyncio.run(worker.process(batch))

    done = repository.get(batch["id"])
    assert done["status"] == "completed"
    assert done["last_user_message_id"] is None
    assert done["delivery_kind"] == "none"


def test_assistant_rows_are_context_not_authorized_input(db, repository):
    add_message(db, "assistant", "我帮你记下了三点")
    add_message(db, "user", "其实只是随口说说")
    batch = claim_conversation(db, repository)
    captured = {}

    async def model(_db, _system, messages, **_kwargs):
        captured.update(json.loads(messages[-1]["content"]))
        return output("empty"), "tool-run"

    asyncio.run(make_worker(db, repository, model).process(batch))

    assert [item["role"] for item in captured["AUTHORIZED_NEW_INPUT"]] == [
        "user"
    ]
    assert [item["role"] for item in captured["CONTEXT_ONLY"]] == [
        "assistant"
    ]


def test_new_correction_holds_then_supersedes_old_pending_result(db, repository):
    add_message(db, "user", "三点")
    first = claim_conversation(db, repository)

    async def first_model(*_args, **_kwargs):
        return output(
            "unable",
            [execution("解析提醒时间", "blocked", missing="日期")],
        ), "r1"

    worker = make_worker(db, repository, first_model)
    asyncio.run(worker.process(first))
    assert [item["id"] for item in repository.pending_deliveries()] == [first["id"]]

    add_message(db, "user", "改成明天下午四点")
    # The old text must not leave while the correcting user row is unprocessed.
    assert repository.pending_deliveries() == []
    second = claim_conversation(db, repository)

    async def second_model(_db, _system, messages, **_kwargs):
        envelope = json.loads(messages[-1]["content"])
        assert envelope["PRIOR_UNDELIVERED_RESULT"]["batch_id"] == first["id"]
        return output(
            "unable",
            [execution("设置提醒", "blocked", missing="提醒事项")],
            [important("提醒时间", "明天下午四点")],
            supersedes=True,
        ), "r2"

    worker = make_worker(db, repository, second_model)
    asyncio.run(worker.process(second))

    assert repository.get(first["id"])["delivery_status"] == "superseded"
    assert repository.get(second["id"])["supersedes_batch_id"] == first["id"]
    assert [item["id"] for item in repository.pending_deliveries()] == [second["id"]]
