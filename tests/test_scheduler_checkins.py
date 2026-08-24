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
        name="custom_poll",
        label="Custom poll",
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


def test_after_ai_call_checkin_is_gated_only_by_its_own_enabled_flag(tmp_path, monkeypatch):
    """曾经有一个全局 TTL 开关能在 enabled=1 的情况下静默拦掉这类 check-in。

    那个开关已经取消，现在唯一的门禁就是这一行自己的 enabled 字段。
    """
    db = _make_db(tmp_path)
    check_in_id = db.create_check_in(
        name="custom_poll",
        label="Custom poll",
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
    assert [item["name"] for _, item in times] == ["custom_poll"]

    db.update_check_in(check_in_id, enabled=False)
    assert scheduler._calc_checkin_times(datetime(2026, 1, 1, 12, 5, 0)) == []


def test_after_ai_call_checkin_recovers_when_baseline_is_stale(tmp_path, monkeypatch):
    """基准（上次 AI 调用）过旧时不能把一个过去的时刻写进 last_scheduled_for。

    写了以后 _scheduled_after_ai_call_time 每轮都会把它原样读回来、每轮都被丢弃，
    这个 check-in 就再也不会触发了。正确行为是以当前时刻为基准重算。
    """
    db = _make_db(tmp_path)
    check_in_id = db.create_check_in(
        name="custom_poll",
        label="Custom poll",
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

    # now 已经比「基准 + 间隔」晚得多：12:00 + 10min = 12:10 早就过去了
    now = datetime(2026, 1, 1, 14, 0, 0)
    times = scheduler._calc_checkin_times(now)

    assert len(times) == 1
    assert times[0][0] == datetime(2026, 1, 1, 14, 10, 0)
    assert db.get_check_in(check_in_id)["last_scheduled_for"] == "2026-01-01T14:10:00"


def test_after_ai_call_checkin_reschedules_expired_persisted_time(tmp_path, monkeypatch):
    """进程重启错过了已排程时刻时，要重新排一次，而不是卡在过去的死值上。"""
    db = _make_db(tmp_path)
    check_in_id = db.create_check_in(
        name="custom_poll",
        label="Custom poll",
        enabled=True,
        schedule_type="after_ai_call",
        interval_min_minutes=10,
        interval_max_minutes=20,
        prompt_template="Current timestamp: {timestamp}",
        tool_profile="poll",
    )
    # 已排在 12:10，但进程直到 13:00 才重新算——12:10 已经错过了
    db.set_check_in_last_scheduled(check_in_id, "2026-01-01T12:10:00")
    scheduler = _make_scheduler(db)
    scheduler._last_ai_call_ts = datetime(2026, 1, 1, 12, 0, 0)
    monkeypatch.setattr(scheduler_module.random, "randint", lambda _a, _b: 10 * 60)

    times = scheduler._calc_checkin_times(datetime(2026, 1, 1, 13, 0, 0))

    assert len(times) == 1
    assert times[0][0] == datetime(2026, 1, 1, 13, 10, 0)
    assert db.get_check_in(check_in_id)["last_scheduled_for"] == "2026-01-01T13:10:00"


def test_ttl_followup_is_permanent_and_other_defaults_are_deletable(tmp_path):
    """ttl_followup 删不掉；random_poll / morning / bedtime_* 可删除且删掉不复活。"""
    db_path = str(tmp_path / "life_tracker_defaults.db")
    db = Database(db_path)

    by_name = {item["name"]: item for item in db.list_check_ins()}
    assert by_name["ttl_followup"]["built_in"] is True
    # 默认关闭：开着会和 random_poll 抢同一个「上次 AI 调用」基准
    assert by_name["ttl_followup"]["enabled"] is False
    for name in ("random_poll", "morning", "bedtime_1", "bedtime_2"):
        assert by_name[name]["built_in"] is False, f"{name} 应该可删除"
        assert by_name[name]["enabled"] is True, f"{name} 应该默认开启"

    assert db.delete_check_in("ttl_followup") is False
    assert db.get_check_in("ttl_followup") is not None

    assert db.delete_check_in("random_poll") is True
    assert db.get_check_in("random_poll") is None

    # 重新打开数据库（等价于重启 bot）：被删掉的默认项不能被重新灌回来，
    # 但永久内置项必须还在
    Database(db_path)
    assert db.get_check_in("random_poll") is None
    assert db.get_check_in("ttl_followup") is not None


def test_ttl_followup_interval_is_hardcoded_and_cannot_be_changed(tmp_path, monkeypatch):
    """TTL 内置项的间隔硬编码 45-55min：调度器不读数据库，写库也改不动。"""
    db_path = str(tmp_path / "life_tracker_locked.db")
    db = Database(db_path)
    db.update_check_in("ttl_followup", enabled=True)
    for check_in in db.list_check_ins():
        if check_in["name"] != "ttl_followup":
            db.update_check_in(check_in["id"], enabled=False)

    # 想把间隔改成 1-2min：被静默丢弃，其余字段照常更新
    db.update_check_in(
        "ttl_followup",
        interval_min_minutes=1,
        interval_max_minutes=2,
        label="Renamed TTL",
    )
    stored = db.get_check_in("ttl_followup")
    assert stored["interval_min_minutes"] == 45
    assert stored["interval_max_minutes"] == 55
    assert stored["label"] == "Renamed TTL", "锁的只有间隔，别的字段要能改"

    # 调度器用的也是硬编码值，不是数据库里的值
    captured = {}

    def fake_randint(low, high):
        captured["range"] = (low, high)
        return 45 * 60

    monkeypatch.setattr(scheduler_module.random, "randint", fake_randint)
    scheduler = _make_scheduler(db)
    scheduler._last_ai_call_ts = datetime(2026, 1, 1, 12, 0, 0)
    times = scheduler._calc_checkin_times(datetime(2026, 1, 1, 12, 5, 0))

    assert captured["range"] == (45 * 60, 55 * 60)
    assert [item["name"] for _, item in times] == ["ttl_followup"]

    # 直接改库绕过 update_check_in 也不影响调度，启动时还会被同步回来
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE check_ins SET interval_min_minutes = 1, interval_max_minutes = 2 "
        "WHERE name = 'ttl_followup'"
    )
    conn.commit()
    conn.close()
    Database(db_path)
    stored = db.get_check_in("ttl_followup")
    assert (stored["interval_min_minutes"], stored["interval_max_minutes"]) == (45, 55)


def test_legacy_db_downgrades_default_checkins_to_deletable(tmp_path):
    """存量库里 random_poll / morning / bedtime_* 是 built_in=1，升级后必须变成可删除。"""
    import sqlite3

    db_path = str(tmp_path / "life_tracker_legacy.db")
    db = Database(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE check_ins SET built_in = 1")
    conn.execute(
        "INSERT OR REPLACE INTO app_state (key, value, updated_at) "
        "VALUES ('checkin_ttl_followup_enabled', '0', datetime('now'))"
    )
    conn.execute("DELETE FROM app_state WHERE key = ?", (Database.CHECK_IN_SEED_FLAG,))
    conn.commit()
    conn.close()

    db = Database(db_path)
    by_name = {item["name"]: item for item in db.list_check_ins()}
    assert by_name["ttl_followup"]["built_in"] is True
    for name in ("random_poll", "morning", "bedtime_1", "bedtime_2"):
        assert by_name[name]["built_in"] is False

    # 遗留的全局开关键要被清掉，避免以后有人再去读它
    assert db.get_state("checkin_ttl_followup_enabled") is None


def test_schedule_change_does_not_move_the_ai_call_baseline(tmp_path, monkeypatch):
    """改 check-in 配置不是一次 AI 调用，不能重置 after_ai_call 的基准时刻。

    以前 Admin 页面的每一次保存都走 notify_ai_call_done，于是用户每存一次盘，
    随机轮询的倒计时就整整推迟一个间隔。
    """
    db = _make_db(tmp_path)
    check_in_id = db.create_check_in(
        name="custom_poll",
        label="Custom poll",
        enabled=True,
        schedule_type="after_ai_call",
        interval_min_minutes=10,
        interval_max_minutes=20,
        prompt_template="Current timestamp: {timestamp}",
        tool_profile="poll",
    )
    scheduler = _make_scheduler(db)
    baseline = datetime(2026, 1, 1, 12, 0, 0)
    scheduler._last_ai_call_ts = baseline
    monkeypatch.setattr(scheduler_module.random, "randint", lambda _a, _b: 10 * 60)
    scheduler._calc_checkin_times(datetime(2026, 1, 1, 12, 5, 0))
    assert db.get_check_in(check_in_id)["last_scheduled_for"] == "2026-01-01T12:10:00"

    # 只改内容（reschedule=False）：基准不动，已排好的时刻也保留
    scheduler.notify_schedule_changed()
    assert scheduler._last_ai_call_ts == baseline
    assert db.get_check_in(check_in_id)["last_scheduled_for"] == "2026-01-01T12:10:00"
    assert scheduler._timer_event.is_set()

    # 改调度参数（reschedule=True）：基准仍然不动，但旧时刻按旧参数算的，要作废
    scheduler.notify_schedule_changed(reschedule=True)
    assert scheduler._last_ai_call_ts == baseline
    assert db.get_check_in(check_in_id)["last_scheduled_for"] is None


def test_ai_call_done_still_resets_baseline_and_reschedules(tmp_path, monkeypatch):
    """真正的 AI 调用完成时，基准和已排时刻都要重置——这是省钱策略的前提。"""
    db = _make_db(tmp_path)
    check_in_id = db.create_check_in(
        name="custom_poll",
        label="Custom poll",
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
    scheduler._calc_checkin_times(datetime(2026, 1, 1, 12, 5, 0))

    scheduler.notify_ai_call_done()

    assert scheduler._last_ai_call_ts > datetime(2026, 1, 1, 12, 0, 0)
    assert db.get_check_in(check_in_id)["last_scheduled_for"] is None
    assert scheduler._timer_event.is_set()


def test_check_in_api_only_forces_reschedule_for_schedule_fields(tmp_path):
    """PATCH 只改内容时不该作废已排好的触发时刻，改调度参数时必须作废。"""
    import api.server as server

    db = _make_db(tmp_path)
    check_in_id = db.create_check_in(
        name="custom_poll",
        label="Custom poll",
        enabled=True,
        schedule_type="after_ai_call",
        interval_min_minutes=10,
        interval_max_minutes=20,
        prompt_template="Current timestamp: {timestamp}",
        tool_profile="poll",
    )
    calls: list[bool] = []
    original_db = server.db
    original_callback = server._check_in_changed_callback
    server.db = db
    server.set_check_in_changed_callback(lambda reschedule: calls.append(reschedule))
    try:
        asyncio.run(server.update_check_in(
            str(check_in_id), {"prompt_template": "New timestamp: {timestamp}"}))
        assert calls == [False]

        asyncio.run(server.update_check_in(
            str(check_in_id), {"interval_min_minutes": 30}))
        assert calls == [False, True]

        asyncio.run(server.update_check_in(str(check_in_id), {"enabled": False}))
        assert calls == [False, True, True]
    finally:
        server.db = original_db
        server.set_check_in_changed_callback(original_callback)


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
        track_scene,
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
                "track_scene": track_scene,
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
    # track_scene 默认关闭：多数 check-in 说完就结束，不需要延续场景，
    # 关着就不给 set_scene 工具、零成本。
    assert captured["track_scene"] is False
    assert captured["check_in_name"] == "custom_review"
    assert captured["context_config"] == {
        "include_weather": False,
        "include_calendar": True,
    }

    stored = db.get_check_in(check_in_id)
    assert stored["last_fired_at"] is not None
    assert stored["last_scheduled_for"] is None


def test_trigger_check_in_now_runs_disabled_check_in_without_marking_fired(
    tmp_path, monkeypatch
):
    """Admin 手动触发：绕过 enabled 门禁，但不能污染真实调度状态。"""
    db = _make_db(tmp_path)
    check_in_id = db.create_check_in(
        name="manual_probe",
        label="Manual probe",
        enabled=False,
        schedule_type="window",
        time_start="09:00",
        time_end="10:00",
        prompt_template="{timestamp} | {name}",
        tool_profile="none",
        allow_silent=True,
    )
    calls = []

    async def fake_scheduled_action(db_arg, prompt, timestamp, history, **kwargs):
        calls.append({"prompt": prompt, "timestamp": timestamp, **kwargs})
        return "probe reply"

    monkeypatch.setattr(scheduler_module, "scheduled_action", fake_scheduled_action)
    scheduler = _make_scheduler(db)

    result = asyncio.run(scheduler.trigger_check_in_now(check_in_id))

    # enabled=False 依然执行，这正是「启用前先测一下」的用途
    assert len(calls) == 1
    assert calls[0]["check_in_name"] == "manual_probe"
    assert result["found"] is True
    assert result["ok"] is True
    assert result["reply"] == "probe reply"
    assert result["error"] is None
    assert result["latency_ms"] >= 0

    # 不写 last_fired_at，否则 _already_fired_for_window 会抑制当天真正的定时触发
    stored = db.get_check_in(check_in_id)
    assert stored["last_fired_at"] is None


def test_trigger_check_in_now_reports_error_instead_of_swallowing(tmp_path, monkeypatch):
    """AI 报错时必须返回 ok=False，不能让测试按钮永远显示成功。"""
    db = _make_db(tmp_path)
    check_in_id = db.create_check_in(
        name="failing_probe",
        label="Failing probe",
        enabled=True,
        schedule_type="window",
        time_start="09:00",
        time_end="10:00",
        prompt_template="{timestamp}",
        tool_profile="none",
    )

    async def failing_scheduled_action(*args, **kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(scheduler_module, "scheduled_action", failing_scheduled_action)
    scheduler = _make_scheduler(db)

    result = asyncio.run(scheduler.trigger_check_in_now(check_in_id))

    assert result["ok"] is False
    assert "provider down" in result["error"]
    assert db.get_check_in(check_in_id)["last_fired_at"] is None


def test_trigger_check_in_now_returns_not_found_for_unknown_id(tmp_path):
    db = _make_db(tmp_path)
    scheduler = _make_scheduler(db)

    assert asyncio.run(scheduler.trigger_check_in_now(999999)) == {"found": False}
