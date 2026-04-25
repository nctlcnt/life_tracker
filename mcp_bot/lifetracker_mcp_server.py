"""MCP server: 只读访问 life-tracker SQLite 数据库（stdio transport）。

被 mcp_bot.main 作为子进程 spawn。所有工具均只读，不会写库。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from mcp.server.fastmcp import FastMCP

import config
from bot.database import Database

mcp = FastMCP("lifetracker")
_db = Database(config.DB_PATH)


@mcp.tool()
def query_events(start: str, end: str) -> list[dict]:
    """查询某时间段内的所有事件（包括跨过区间边界的）。

    Args:
        start: ISO 8601 起点（含），例如 "2026-04-18T00:00:00"
        end: ISO 8601 终点（不含）

    Returns:
        事件列表。每项含 id / start_time / end_time / content / category /
        project_name / notes / status / session_id / is_parallel。
    """
    return _db.get_events(start, end)


@mcp.tool()
def query_ongoing_events(limit: int = 5) -> list[dict]:
    """查询当前正在进行的事件（end_time IS NULL 且 status IS NULL）。"""
    return _db.get_ongoing_events(limit=limit)


@mcp.tool()
def query_planned_events() -> list[dict]:
    """查询近期/未来的 planned events（status='planned'，含 24h grace period 内的）。"""
    return _db.get_planned_events()


@mcp.tool()
def list_memories() -> list[dict]:
    """列出所有 memory 条目（AI 长期记忆）。"""
    return _db.get_all_memories()


@mcp.tool()
def list_active_deadlines() -> list[dict]:
    """列出所有未完成的 deadline（status='active'）。"""
    return _db.get_active_deadlines()


@mcp.tool()
def list_todos(include_done: bool = False) -> list[dict]:
    """列出 todo。默认只包含未完成项。"""
    return _db.get_todos(include_done=include_done)


@mcp.tool()
def list_categories() -> list[str]:
    """列出数据库中出现过的所有 category 值。"""
    return _db.get_all_categories()


@mcp.tool()
def list_project_names() -> list[dict]:
    """列出所有 Focus 类项目名（按事件计数降序）。"""
    return _db.get_all_project_names()


def _duration_minutes(start_iso: str, end_iso: str | None) -> int:
    if not end_iso:
        return 0
    try:
        s = datetime.fromisoformat(start_iso)
        e = datetime.fromisoformat(end_iso)
        return max(0, int((e - s).total_seconds() / 60))
    except (ValueError, TypeError):
        return 0


def _fmt_minutes(m: int) -> str:
    h, mm = divmod(m, 60)
    if h and mm:
        return f"{h}h {mm}m"
    if h:
        return f"{h}h"
    return f"{mm}m"


@mcp.tool()
def weekly_summary() -> dict:
    """最近 7 天的活动聚合：按 category / project_name 汇总时长 + 排名靠前的事件。

    便捷工具：服务"我近一周做了什么"这类高频问句，省得 agent 每次都 query_events
    再自己聚合。只统计 status IS NULL 的真实事件（跳过 planned / cancelled）。

    Returns:
        {
            "range": [start_iso, end_iso],
            "by_category": {category: "Xh Ym"},
            "by_project": {project_name: "Xh Ym"},
            "top_events": [{title, start, duration_min, category, project_name}, ...],
            "total_events": int,
        }
    """
    now = datetime.now().astimezone()
    start = now - timedelta(days=7)
    events = _db.get_events(
        start.isoformat(timespec="seconds"),
        now.isoformat(timespec="seconds"),
    )

    by_category: dict[str, int] = {}
    by_project: dict[str, int] = {}
    enriched: list[tuple[int, dict]] = []
    for e in events:
        if e.get("status"):
            continue
        mins = _duration_minutes(e["start_time"], e.get("end_time"))
        cat = e.get("category") or "(unknown)"
        by_category[cat] = by_category.get(cat, 0) + mins
        proj = e.get("project_name")
        if proj:
            by_project[proj] = by_project.get(proj, 0) + mins
        enriched.append((mins, e))

    enriched.sort(key=lambda t: t[0], reverse=True)
    top = [
        {
            "title": e["content"],
            "start": e["start_time"],
            "duration_min": mins,
            "category": e.get("category"),
            "project_name": e.get("project_name"),
        }
        for mins, e in enriched[:5]
    ]

    return {
        "range": [
            start.isoformat(timespec="seconds"),
            now.isoformat(timespec="seconds"),
        ],
        "by_category": {
            k: _fmt_minutes(v)
            for k, v in sorted(by_category.items(), key=lambda kv: -kv[1])
        },
        "by_project": {
            k: _fmt_minutes(v)
            for k, v in sorted(by_project.items(), key=lambda kv: -kv[1])
        },
        "top_events": top,
        "total_events": len(events),
    }


if __name__ == "__main__":
    mcp.run()
