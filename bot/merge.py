"""
事件合并模块
将原始的打卡记录合并为完整的时间段

合并规则：
1. 按 start_time 排序
2. 相邻事件如果 content + category 完全相同 → 合并为一段
3. 合并后取最早 start_time、最晚 end_time
4. 没有 end_time 的事件 → 视为"进行中"，直到下一个不同事件开始
5. 最后一条事件如果没有 end_time → 保留 None（仍在进行）
"""
from datetime import datetime
from typing import Optional


def merge_events(events: list[dict]) -> list[dict]:
    """
    将原始事件列表合并为不重叠的时间段。

    参数:
        events: get_events() 返回的原始事件列表（已按 start_time 排序）

    返回:
        合并后的时间段列表，每段包含:
        - start_time: 最早的开始时间
        - end_time: 最晚的结束时间（或 None 表示进行中）
        - content: 事件描述（取第一条）
        - category: 事件分类
        - notes: 合并所有非空 notes
        - event_ids: 原始记录 ID 列表
        - check_in_count: 打卡次数
    """
    if not events:
        return []

    segments = []
    current = _start_segment(events[0])

    for i in range(1, len(events)):
        event = events[i]

        if _should_merge(current, event):
            # 同一活动，扩展当前段
            _extend_segment(current, event)
        else:
            # 不同活动，先结束当前段，再开始新段
            _close_segment(current, event["start_time"])
            segments.append(current)
            current = _start_segment(event)

    # 最后一段
    segments.append(current)

    return segments


def _start_segment(event: dict) -> dict:
    """从一个事件创建新的时间段"""
    return {
        "start_time": event["start_time"],
        "end_time": event.get("end_time"),
        "content": event["content"],
        "category": event.get("category", "uncategorized"),
        "notes": event.get("notes") or None,
        "project_name": event.get("project_name") or None,
        "energy_type": event.get("energy_type") or None,
        "event_ids": [event["id"]],
        "check_in_count": 1,
    }


def _should_merge(current_segment: dict, next_event: dict) -> bool:
    """
    判断下一个事件是否应该合并到当前段。
    规则：content 和 category 完全相同时合并。
    """
    return (
        current_segment["content"] == next_event["content"]
        and current_segment["category"] == next_event.get("category", "uncategorized")
    )


def _extend_segment(segment: dict, event: dict):
    """将事件合并到当前段中"""
    segment["event_ids"].append(event["id"])
    segment["check_in_count"] += 1

    # 更新 end_time：取最晚的
    event_end = event.get("end_time")
    if event_end:
        if segment["end_time"] is None or event_end > segment["end_time"]:
            segment["end_time"] = event_end

    # 如果事件的 start_time 更晚，也可能需要扩展 end_time
    # （比如后一条打卡虽然没 end_time 但 start_time 比当前 end_time 晚）
    if segment["end_time"] and event["start_time"] > segment["end_time"]:
        segment["end_time"] = None  # 重新变为进行中

    # 合并 notes
    event_notes = event.get("notes")
    if event_notes:
        if segment["notes"]:
            segment["notes"] += f"\n{event_notes}"
        else:
            segment["notes"] = event_notes

    # project_name：保留第一个非空值（已在 _start_segment 设置）
    if not segment.get("project_name") and event.get("project_name"):
        segment["project_name"] = event["project_name"]

    # energy_type：drain 优先（若任何子事件是漏水则整段标记为漏水）
    ev_et = event.get("energy_type")
    if ev_et == "drain":
        segment["energy_type"] = "drain"
    elif ev_et and not segment.get("energy_type"):
        segment["energy_type"] = ev_et


def _close_segment(segment: dict, next_start_time: str):
    """
    结束当前段。
    如果当前段没有 end_time，用下一个不同事件的 start_time 作为结束时间。
    """
    if segment["end_time"] is None:
        segment["end_time"] = next_start_time
