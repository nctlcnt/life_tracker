import asyncio
from datetime import datetime

import bot.scheduler as scheduler_module
from bot.database import Database
from bot.scheduler import Scheduler


async def _noop_send(_message: str) -> None:
    return None


def _make_db(tmp_path) -> Database:
    db = Database(str(tmp_path / "life_tracker_test.db"))
    for check_in in db.list_check_ins():
        db.update_check_in(check_in["id"], enabled=False)
    return db


def _make_scheduler(db: Database) -> Scheduler:
    return Scheduler(db, _noop_send)


def test_after_ai_call_checkin_schedules_once_and_reuses_time(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    check_in_id = db.create_check_in(
        name="ttl_followup",
        label="TTL follow-up",
        enabled=True,
        schedule_type="after_ai_call",
        interval_min_minutes=10,
        interval_max_minutes=20,
        prompt_template="Current timestamp: {timestamp}",
        tool_profile="poll",
    )
    scheduler = _make_scheduler(db)
    scheduler._last_ai_call_ts = datetime(2026, 1, 1, 12, 0, 0)
    monkeypatch.setattr(scheduler_module.random, "randint", lambda _a, _b: 10 * 60)

    times = scheduler._calc_checkin_times(datetime(2026, 1, 1, 12, 5, 0))

    assert len(times) == 1
    assert times[0][0] == datetime(2026, 1, 1, 12, 10, 0)
    stored = db.get_check_in(check_in_id)
    assert stored["last_scheduled_for"] == "2026-01-01T12:10:00"

    def fail_if_rescheduled(_a, _b):
        raise AssertionError("TTL check-in should reuse last_scheduled_for")

    monkeypatch.setattr(scheduler_module.random, "randint", fail_if_rescheduled)
    times = scheduler._calc_checkin_times(datetime(2026, 1, 1, 12, 6, 0))

    assert len(times) == 1
    assert times[0][0] == datetime(2026, 1, 1, 12, 10, 0)


def test_after_ai_call_checkin_respects_global_ttl_toggle(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    db.create_check_in(
        name="ttl_followup",
        label="TTL follow-up",
        enabled=True,
        schedule_type="after_ai_call",
        interval_min_minutes=10,
        interval_max_minutes=20,
        prompt_template="Current timestamp: {timestamp}",
        tool_profile="poll",
    )
    db.set_ttl_followup_enabled(False)
    scheduler = _make_scheduler(db)
    scheduler._last_ai_call_ts = datetime(2026, 1, 1, 12, 0, 0)
    monkeypatch.setattr(
        scheduler_module.random,
        "randint",
        lambda _a, _b: (_ for _ in ()).throw(AssertionError("should not schedule")),
    )

    assert scheduler._calc_checkin_times(datetime(2026, 1, 1, 12, 5, 0)) == []


def test_window_checkin_schedules_inside_window_and_reuses_time(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    check_in_id = db.create_check_in(
        name="morning_custom",
        label="Morning custom",
        enabled=True,
        schedule_type="window",
        time_start="09:00",
        time_end="10:00",
        prompt_template="Current timestamp: {timestamp}",
        tool_profile="poll",
    )
    scheduler = _make_scheduler(db)
    monkeypatch.setattr(scheduler_module.random, "randint", lambda _a, _b: 15 * 60)

    times = scheduler._calc_checkin_times(datetime(2026, 1, 1, 8, 0, 0))

    assert len(times) == 1
    assert times[0][0] == datetime(2026, 1, 1, 9, 15, 0)
    stored = db.get_check_in(check_in_id)
    assert stored["last_scheduled_for"] == "2026-01-01T09:15:00"

    monkeypatch.setattr(
        scheduler_module.random,
        "randint",
        lambda _a, _b: (_ for _ in ()).throw(AssertionError("should reuse window time")),
    )
    times = scheduler._calc_checkin_times(datetime(2026, 1, 1, 8, 30, 0))

    assert len(times) == 1
    assert times[0][0] == datetime(2026, 1, 1, 9, 15, 0)


def test_window_checkin_last_fired_at_skips_same_window(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    check_in_id = db.create_check_in(
        name="morning_custom",
        label="Morning custom",
        enabled=True,
        schedule_type="window",
        time_start="09:00",
        time_end="10:00",
        prompt_template="Current timestamp: {timestamp}",
        tool_profile="poll",
    )
    db.mark_check_in_fired(check_in_id, "2026-01-01T09:20:00")
    scheduler = _make_scheduler(db)
    monkeypatch.setattr(scheduler_module.random, "randint", lambda _a, _b: 0)

    times = scheduler._calc_checkin_times(datetime(2026, 1, 1, 10, 30, 0))

    assert len(times) == 1
    assert times[0][0] == datetime(2026, 1, 2, 9, 0, 0)


def test_do_check_in_passes_execution_profile_and_marks_fired(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    check_in_id = db.create_check_in(
        name="custom_review",
        label="Custom review",
        enabled=True,
        schedule_type="window",
        time_start="09:00",
        time_end="10:00",
        prompt_template="{timestamp} | {name} | {label} | {instructions}",
        instructions="review current priorities",
        context_config={"include_weather": False, "include_calendar": True},
        tool_profile="none",
        allow_silent=False,
    )
    captured = {}

    async def fake_scheduled_action(
        db_arg,
        prompt,
        timestamp,
        history,
        *,
        send_callback,
        allow_silent,
        trigger,
        tool_profile,
        check_in_name,
        context_config,
        window,
    ):
        captured.update(
            {
                "db": db_arg,
                "prompt": prompt,
                "timestamp": timestamp,
                "history": history,
                "send_callback": send_callback,
                "allow_silent": allow_silent,
                "trigger": trigger,
                "tool_profile": tool_profile,
                "check_in_name": check_in_name,
                "context_config": context_config,
                "window": window,
            }
        )
        return "ok"

    monkeypatch.setattr(scheduler_module, "scheduled_action", fake_scheduled_action)
    scheduler = _make_scheduler(db)

    asyncio.run(
        scheduler._do_check_in(
            db.get_check_in(check_in_id),
            "2026-01-01 12:00",
        )
    )

    assert captured["db"] is db
    assert captured["prompt"] == (
        "2026-01-01 12:00 | custom_review | Custom review | "
        "review current priorities"
    )
    assert captured["timestamp"] == "2026-01-01 12:00"
    assert captured["history"] == []
    # history 即窗口消息（LT-135）：空频道 → 空窗口
    assert captured["window"].messages == []
    assert captured["window"].tail_count == 0
    assert captured["allow_silent"] is False
    assert captured["trigger"] == "check_in"
    assert captured["tool_profile"] == "none"
    assert captured["check_in_name"] == "custom_review"
    assert captured["context_config"] == {
        "include_weather": False,
        "include_calendar": True,
    }

    stored = db.get_check_in(check_in_id)
    assert stored["last_fired_at"] is not None
    assert stored["last_scheduled_for"] is None
