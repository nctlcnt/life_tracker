"""静音时段只挡 after_ai_call 的主动轮询。

时段内不会调用 AI：以前夜里每次轮询都要花一次调用，只为了让 AI 回一个 [SILENT]。
reminder / DDL 提醒走另一条循环，window 类型的 check-in 也不经过这里，都不受影响。
"""
from datetime import datetime

import pytest

from bot.scheduler import DEFAULT_QUIET_HOURS, Scheduler


class _FakeDB:
    def __init__(self, quiet_hours=None):
        self._state = {} if quiet_hours is None else {"quiet_hours": quiet_hours}

    def get_state(self, key):
        return self._state.get(key)


class _Clamp:
    """只借 Scheduler 的纯方法，不启动调度循环。"""

    _parse_hhmm = staticmethod(Scheduler._parse_hhmm)
    _push_out_of_quiet_hours = staticmethod(Scheduler._push_out_of_quiet_hours)
    _clamp_to_active_window = Scheduler._clamp_to_active_window
    _clamp_to_allowed_time = Scheduler._clamp_to_allowed_time
    _quiet_hours = Scheduler._quiet_hours

    def __init__(self, quiet_hours=None):
        self.db = _FakeDB(quiet_hours)


def _at(hh, mm=0, day=23):
    return datetime(2026, 8, day, hh, mm)


NO_WINDOW = {"time_start": None, "time_end": None}


def test_default_is_midnight_to_six():
    assert DEFAULT_QUIET_HOURS == ("00:00", "06:00")
    assert _Clamp()._quiet_hours() == ("00:00", "06:00")


def test_poll_inside_quiet_hours_waits_until_six():
    clamp = _Clamp()
    assert clamp._clamp_to_allowed_time(NO_WINDOW, _at(0, 40)) == _at(6)
    assert clamp._clamp_to_allowed_time(NO_WINDOW, _at(5, 59)) == _at(6)


def test_times_outside_quiet_hours_are_untouched():
    clamp = _Clamp()
    assert clamp._clamp_to_allowed_time(NO_WINDOW, _at(23, 50)) == _at(23, 50)
    # 结束边界不算静音：06:00 可以响
    assert clamp._clamp_to_allowed_time(NO_WINDOW, _at(6)) == _at(6)


def test_quiet_hours_crossing_midnight():
    clamp = _Clamp("23:00-06:00")
    assert clamp._clamp_to_allowed_time(NO_WINDOW, _at(23, 30)) == _at(6, day=24)
    assert clamp._clamp_to_allowed_time(NO_WINDOW, _at(3)) == _at(6)
    assert clamp._clamp_to_allowed_time(NO_WINDOW, _at(22, 59)) == _at(22, 59)


def test_active_window_and_quiet_hours_combine():
    """check-in 时段从 05:00 开始：先被推到 05:00，又落进静音时段，最后是 06:00。"""
    clamp = _Clamp()
    check_in = {"time_start": "05:00", "time_end": "22:00"}
    assert clamp._clamp_to_allowed_time(check_in, _at(23)) == _at(6, day=24)


def test_quiet_hours_can_be_turned_off():
    clamp = _Clamp("off")
    assert clamp._quiet_hours() is None
    assert clamp._clamp_to_allowed_time(NO_WINDOW, _at(2)) == _at(2)


@pytest.mark.parametrize("raw", ["garbage", "25:00-06:00", "00:00"])
def test_invalid_setting_falls_back_to_default(raw):
    """格式错误时退回默认值而不是关闭，避免夜里重新开始发消息。"""
    assert _Clamp(raw)._quiet_hours() == DEFAULT_QUIET_HOURS
