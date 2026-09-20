"""「今天」的 memo 范围计算 + prompt 注入用的格式化取数。

memos 表的 CRUD 在 bot/database.py。这里单独放的两个函数都依赖
bot.timezone_state 这个可运行时切换（/tz）的进程状态：放进 Database 会让
持久层依赖运行时时区，放进 bot/prompts.py 又违反那个模块自称的
"non-user-specific" 边界，所以单独成一个小模块。
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from bot import timezone_state
from bot.database import Database, normalize_utc_iso

# 和 App 的 LogicalDay 一致：一天从本地凌晨 4 点开始，到第二天凌晨 4 点结束。
# 注意 bot.database.Database.get_today_events() 用的是 0 点到 23:59，
# 和这里不一致——两者只在凌晨 0-4 点之间看到的"今天"会不同，本次不改前者。
DAY_CUTOFF_HOUR = 4


def today_memo_range_utc(now: datetime | None = None) -> tuple[str, str]:
    """本地"今天"（凌晨 4 点切分）对应的 UTC 起止时间，用于 list_memos_between。

    用 datetime.combine(..., tzinfo=ZoneInfo) 而不是"当前时间的 offset"，
    是因为夏令时切换日两者的 offset 不同，只有 ZoneInfo 能按日期查出正确值。

    now 为 naive 时按"已经是 timezone_state 当前时区的本地时间"解释（和
    format_countdown 等既有调用点的约定一致），不去看进程的 TZ 环境变量，
    这样只改 timezone_state 的返回值就能让这个函数在测试里可控。
    """
    tz = ZoneInfo(timezone_state.get_timezone())
    if now is None:
        local_now = datetime.now(tz)
    elif now.tzinfo is None:
        local_now = now.replace(tzinfo=tz)
    else:
        local_now = now.astimezone(tz)
    day = (local_now - timedelta(hours=DAY_CUTOFF_HOUR)).date()
    start = datetime.combine(day, time(DAY_CUTOFF_HOUR), tzinfo=tz)
    end = datetime.combine(day + timedelta(days=1), time(DAY_CUTOFF_HOUR), tzinfo=tz)
    return normalize_utc_iso(start.isoformat()), normalize_utc_iso(end.isoformat())


def _local_time_label(occurred_at_utc: str) -> str:
    tz = ZoneInfo(timezone_state.get_timezone())
    dt = datetime.fromisoformat(occurred_at_utc.replace("Z", "+00:00"))
    return dt.astimezone(tz).strftime("%H:%M")


def get_today_memos_for_prompt(db: Database, now: datetime | None = None) -> list[dict]:
    """今天、未删除的 memo，附加 local_time（HH:MM）供 prompt 展示。

    AI 需要的是本地时间，表里存的是 UTC，所以在这里转换一次，调用方
    直接把结果传给 build_prompt(today_memos=...) 即可。

    地点字段故意不传：地点只给 App 显示用，不进入 AI 上下文。
    """
    start, end = today_memo_range_utc(now)
    memos = db.list_memos_between(start, end)
    return [
        {"id": m["id"], "content": m["content"], "occurred_at": m["occurred_at"],
         "local_time": _local_time_label(m["occurred_at"])}
        for m in memos
    ]
