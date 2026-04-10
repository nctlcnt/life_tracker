"""
数据清理脚本
合并数据库中的重复事件记录

使用方法：
    python -m scripts.cleanup          # 预览模式（只显示，不修改）
    python -m scripts.cleanup --apply  # 执行合并
"""
import sqlite3
import shutil
import sys
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "life_tracker.db")


def find_duplicate_groups(conn: sqlite3.Connection) -> list[list[dict]]:
    """
    查找可合并的事件组。
    规则：content + category 完全相同 且 start_time 相近的事件归为一组。
    """
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM events ORDER BY content, category, start_time"
    ).fetchall()
    events = [dict(r) for r in rows]

    groups = []
    if not events:
        return groups

    current_group = [events[0]]
    for i in range(1, len(events)):
        prev = events[i - 1]
        curr = events[i]

        # 同一 content + category → 归入同组
        if prev["content"] == curr["content"] and prev["category"] == curr["category"]:
            current_group.append(curr)
        else:
            if len(current_group) > 1:
                groups.append(current_group)
            current_group = [curr]

    if len(current_group) > 1:
        groups.append(current_group)

    return groups


def merge_group(group: list[dict]) -> dict:
    """
    从一组重复事件中计算出合并后的最终值。
    - start_time: 取最早的
    - end_time: 取最晚的（如果全部为 None 则保留 None）
    - notes: 合并所有非空 notes
    """
    start_time = min(e["start_time"] for e in group)

    end_times = [e["end_time"] for e in group if e["end_time"]]
    end_time = max(end_times) if end_times else None

    all_notes = [e["notes"] for e in group if e.get("notes")]
    notes = "\n".join(all_notes) if all_notes else None

    return {
        "start_time": start_time,
        "end_time": end_time,
        "notes": notes,
    }


def preview(groups: list[list[dict]]):
    """预览将要合并的事件组"""
    if not groups:
        print("✅ 没有发现重复事件，数据已经很干净！")
        return

    print(f"发现 {len(groups)} 组重复事件：\n")
    for i, group in enumerate(groups, 1):
        merged = merge_group(group)
        ids = [e["id"] for e in group]
        keep_id = ids[0]
        delete_ids = ids[1:]

        print(f"── 第 {i} 组: {group[0]['content']} ({group[0]['category']}) ──")
        for e in group:
            marker = "✔ 保留" if e["id"] == keep_id else "✖ 删除"
            end = e["end_time"] or "进行中"
            print(f"  [{marker}] id={e['id']}  {e['start_time']} → {end}")

        print(f"  → 合并后: {merged['start_time']} → {merged['end_time'] or '进行中'}")
        print()


def apply(conn: sqlite3.Connection, groups: list[list[dict]]):
    """执行合并：保留每组第一条，更新其值，删除其余"""
    total_deleted = 0
    for group in groups:
        merged = merge_group(group)
        keep_id = group[0]["id"]
        delete_ids = [e["id"] for e in group[1:]]

        # 更新保留的那条
        conn.execute(
            "UPDATE events SET start_time = ?, end_time = ?, notes = ? WHERE id = ?",
            (merged["start_time"], merged["end_time"], merged["notes"], keep_id)
        )

        # 删除其余
        placeholders = ",".join("?" * len(delete_ids))
        conn.execute(
            f"DELETE FROM events WHERE id IN ({placeholders})",
            delete_ids
        )
        total_deleted += len(delete_ids)

    conn.commit()
    print(f"✅ 完成！删除了 {total_deleted} 条重复记录。")


def main():
    db_path = os.path.abspath(DB_PATH)
    if not os.path.exists(db_path):
        print(f"❌ 数据库文件不存在: {db_path}")
        sys.exit(1)

    do_apply = "--apply" in sys.argv

    if do_apply:
        # 先备份
        backup_path = db_path + ".bak"
        shutil.copy2(db_path, backup_path)
        print(f"📦 已备份到 {backup_path}\n")

    conn = sqlite3.connect(db_path)
    groups = find_duplicate_groups(conn)

    if do_apply:
        preview(groups)
        if groups:
            print("正在执行合并...\n")
            apply(conn, groups)
    else:
        preview(groups)
        if groups:
            print("以上为预览。要执行合并请运行：")
            print("  python -m scripts.cleanup --apply")

    conn.close()


if __name__ == "__main__":
    main()
