"""LT-178 tool execution, replay, shadow/apply, and result expression."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from bot.async_pipeline import ToolBatchRepository, ToolResultExpresser, ToolWorker
from bot.async_pipeline.batch_planner import plan_next_batch
from bot.async_pipeline.worker_prompts import build_tool_worker_system
from bot.database import Database
from bot.memory import MemoryService


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


async def echo_expression(_db, _system, messages):
    payload = json.loads(messages[-1]["content"].split("\n", 1)[1])
    return "；".join(payload["facts"]), "expression-run"


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


def output(outcome, facts=(), terms=(), *, supersedes=False):
    return json.dumps(
        {
            "outcome": outcome,
            "facts": list(facts),
            "verbatim_terms": list(terms),
            "supersedes_previous": supersedes,
        },
        ensure_ascii=False,
    )


def test_tool_worker_prompt_is_persona_free_but_keeps_tool_policy(db):
    db.set_prompt_section("main_template", "PRIVATE PERSONA MARKER")
    db.set_prompt_section("tools", "APPLICATION TOOL POLICY MARKER")

    prompt = build_tool_worker_system(db)

    assert "portable background tool worker" in prompt
    assert "APPLICATION TOOL POLICY MARKER" in prompt
    assert "PRIVATE PERSONA MARKER" not in prompt


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
        return output("facts", ["已记录 12:00 的午饭"], ["12:00"]), "run-1"

    asyncio.run(make_worker(db, repository, model).process(batch))

    done = repository.get(batch["id"])
    assert done["delivery_kind"] == "reaction"
    assert done["delivery_status"] == "pending"
    assert len(repository.calls(batch["id"])) == 1
    assert len(db.get_today_events()) == 1


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
        return output("facts", ["拟记录午饭"]), "shadow-run"

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


def test_successful_call_is_replayed_after_model_output_retry(db, repository):
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
            return "not json", "bad-run"
        return output("facts", ["午饭已记录"]), "good-run"

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
    assert model_attempts == 2


def test_reminder_result_is_expressed_with_latest_context_and_exact_terms(
    db, repository
):
    add_message(db, "user", "明天早上提醒我交作业")
    batch = claim_conversation(db, repository)
    captured = {}
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
            "facts",
            [f"已设置 {trigger_time} 的交作业提醒"],
            [trigger_time, "交作业"],
        ), "tool-run"

    worker = make_worker(db, repository, model, expression=expression)
    asyncio.run(worker.process(batch))

    done = repository.get(batch["id"])
    assert done["delivery_kind"] == "message"
    assert done["result"]["say"] == f"好，{trigger_time} 提醒你交作业"
    assert "Business tools are handled by a separate background worker" in captured[
        "system"
    ]
    assert "BACKEND_RESULT" in captured["messages"][-1]["content"]


def test_expression_falls_back_to_facts_if_model_drops_verbatim_term(
    db, repository
):
    async def lossy(_db, _system, _messages):
        return "好，明早提醒你", "lossy-run"

    expresser = ToolResultExpresser(db, lossy, memory_service=MemoryService(db))
    text = asyncio.run(
        expresser.express(
            channel_id=CHANNEL,
            batch_id="batch-1",
            facts=("已设置 2026-08-29T08:00:00 的提醒",),
            verbatim_terms=("2026-08-29T08:00:00",),
        )
    )
    assert text == "已设置 2026-08-29T08:00:00 的提醒"


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
        return output("unable", ["还缺少日期，无法处理三点"], ["三点"]), "r1"

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
            ["已改按明天下午四点理解，但仍缺少要做的事"],
            ["明天下午四点"],
            supersedes=True,
        ), "r2"

    worker = make_worker(db, repository, second_model)
    asyncio.run(worker.process(second))

    assert repository.get(first["id"])["delivery_status"] == "superseded"
    assert repository.get(second["id"])["supersedes_batch_id"] == first["id"]
    assert [item["id"] for item in repository.pending_deliveries()] == [second["id"]]
