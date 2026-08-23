"""`after_ai_call` 类型的 check-in 必须遵守时段限制。

背景：`window` 类型本来就只在 time_start~time_end 之间触发，但 `after_ai_call`
以前完全不读这两列。间隔从上一次 AI 调用起算，所以睡前那次对话就足以把下一次
主动消息推到凌晨——实测 2026 年 8 月在悉尼时间 02、03、04 点各响过一次。
之所以只有一次而不是每天，是因为夜里没有 AI 调用可以挂靠，不是因为有保护。
"""
from datetime import datetime

import pytest

from bot.scheduler import Scheduler


class _Clamp:
    """只借 Scheduler 的两个纯方法，不启动调度循环。"""

    # _parse_hhmm 在 Scheduler 上是 staticmethod；直接赋值会被当成实例方法
    # 绑定，多吃一个 self，所以要重新包一层。
    _parse_hhmm = staticmethod(Scheduler._parse_hhmm)
    _clamp_to_active_window = Scheduler._clamp_to_active_window


@pytest.fixture
def clamp():
    return _Clamp()


def _at(hh, mm=0):
    return datetime(2026, 8, 23, hh, mm)


def test_no_window_configured_keeps_the_original_time(clamp):
    """两列为空 = 不限时段，保持旧行为。已有的 check-in 不能因为这次改动
    突然被推迟。"""
    check_in = {"time_start": None, "time_end": None}
    assert clamp._clamp_to_active_window(check_in, _at(3)) == _at(3)


def test_time_inside_the_window_is_untouched(clamp):
    check_in = {"time_start": "09:00", "time_end": "22:00"}
    assert clamp._clamp_to_active_window(check_in, _at(14, 30)) == _at(14, 30)


def test_late_night_is_pushed_to_the_next_morning(clamp):
    """这是本次改动要解决的那个场景：睡前对话把下一次主动消息推到凌晨。"""
    check_in = {"time_start": "09:00", "time_end": "22:00"}

    assert clamp._clamp_to_active_window(check_in, _at(3)) == _at(9)


def test_after_the_window_closes_it_waits_for_tomorrow(clamp):
    """当天窗口已经过去时必须推到明天，不能推回今天早上——
    那会得到一个位于过去的时刻，这一轮又会被丢弃，check-in 永久卡住。"""
    check_in = {"time_start": "09:00", "time_end": "22:00"}

    result = clamp._clamp_to_active_window(check_in, _at(23, 30))

    assert result == datetime(2026, 8, 24, 9, 0)
    assert result > _at(23, 30)


def test_window_boundaries_are_inclusive(clamp):
    check_in = {"time_start": "09:00", "time_end": "22:00"}
    assert clamp._clamp_to_active_window(check_in, _at(9)) == _at(9)
    assert clamp._clamp_to_active_window(check_in, _at(22)) == _at(22)


def test_window_crossing_midnight_is_treated_as_one_span(clamp):
    """跨零点的时段（例如夜班 22:00~06:00）语义是「这段之内可以响」，
    不是「22:00 到 06:00 这段禁止」。判断要分成当天 start 之后、
    次日 end 之前两段，否则整个夜班窗口会被判成时段外。"""
    check_in = {"time_start": "22:00", "time_end": "06:00"}

    assert clamp._clamp_to_active_window(check_in, _at(23)) == _at(23)
    assert clamp._clamp_to_active_window(check_in, _at(2)) == _at(2)
    # 白天落在时段外，推到当晚 22:00
    assert clamp._clamp_to_active_window(check_in, _at(14)) == _at(22)


def test_a_stale_persisted_time_is_also_clamped(clamp):
    """库里存着的时刻同样要过时段检查。

    时段是后加的（或被改窄了），所以库里可能存着一个当时合法、现在越界的时刻。
    如果只检查重新计算的那条路径，这个旧时刻会被原样读回来直接触发，
    新设的时段就形同虚设——实测给「随手说一句」加上 09:00-22:00 之后，
    它仍然按改之前存的 00:37 排队。
    """
    check_in = {"time_start": "09:00", "time_end": "22:00"}
    stale = datetime(2026, 8, 24, 0, 37, 50)

    assert clamp._clamp_to_active_window(check_in, stale) == \
        datetime(2026, 8, 24, 9, 0)
