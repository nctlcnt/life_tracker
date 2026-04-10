"""
数据库模块：SQLite 操作
存储时间轴事件、聊天记录、提醒等
"""
import sqlite3
import os
import json
from datetime import datetime
from typing import Optional


class Database:
    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.db_path = db_path
        self._init_tables()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_tables(self):
        conn = self._get_conn()
        conn.executescript("""
            -- 时间轴事件表（核心数据）
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                start_time TEXT NOT NULL,          -- ISO 8601 格式
                end_time TEXT,                      -- ISO 8601 格式，可为空（进行中）
                content TEXT NOT NULL,              -- 事件描述
                category TEXT DEFAULT 'uncategorized', -- 分类
                created_at TEXT DEFAULT (datetime('now'))
            );

            -- 聊天记录表（保存上下文，供 AI 回顾）
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT NOT NULL,                 -- 'user' 或 'assistant'
                content TEXT NOT NULL,
                timestamp TEXT DEFAULT (datetime('now'))
            );

            -- 提醒队列表
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trigger_time TEXT NOT NULL,          -- 触发时间 ISO 8601
                action TEXT NOT NULL,                -- 要执行的动作描述
                done INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now'))
            );

            -- 未处理消息表（AI 不可见，和聊天记录隔离）
            CREATE TABLE IF NOT EXISTS pending_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                timestamp TEXT NOT NULL
            );

            -- 待办事件表（upcoming deadlines / events）
            CREATE TABLE IF NOT EXISTS pending_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                due_date TEXT,                       -- 截止/发生日期 ISO 8601，可为空
                description TEXT NOT NULL,            -- 事件描述
                notes TEXT,                           -- 补充信息
                done INTEGER DEFAULT 0,               -- 0=待办, 1=已完成/已过期
                created_at TEXT DEFAULT (datetime('now'))
            );
        """)
        conn.commit()
        # 兼容已有数据库：尝试加列，已存在则忽略
        try:
            conn.execute("ALTER TABLE events ADD COLUMN notes TEXT")
        except sqlite3.OperationalError:
            pass
            
        try:
            conn.execute("ALTER TABLE events ADD COLUMN session_id INTEGER")
        except sqlite3.OperationalError:
            pass
        conn.commit()
        conn.close()

    # ============ 时间轴事件 ============

    def add_event(self, start_time: str, end_time: Optional[str],
                  content: str, category: str = "uncategorized",
                  notes: Optional[str] = None, session_id: Optional[int] = None) -> int:
        """添加一条时间轴事件，返回 event id"""
        conn = self._get_conn()
        cursor = conn.execute(
            "INSERT INTO events (start_time, end_time, content, category, notes, session_id) VALUES (?, ?, ?, ?, ?, ?)",
            (start_time, end_time, content, category, notes, session_id)
        )
        conn.commit()
        event_id = cursor.lastrowid
        conn.close()
        return event_id

    def update_event(self, event_id: int, **fields) -> bool:
        """更新指定事件的字段，只更新传入的字段。返回是否成功（event_id 存在）。"""
        allowed = {"end_time", "content", "category", "notes", "session_id"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return False
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [event_id]
        conn = self._get_conn()
        cursor = conn.execute(
            f"UPDATE events SET {set_clause} WHERE id = ?", values
        )
        conn.commit()
        affected = cursor.rowcount
        conn.close()
        return affected > 0

    def get_events(self, start: str, end: str) -> list[dict]:
        """查询时间范围内的事件"""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM events WHERE start_time >= ? AND start_time <= ? ORDER BY start_time",
            (start, end)
        ).fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def get_all_categories(self) -> list[str]:
        """获取所有已有的分类"""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT DISTINCT category FROM events ORDER BY category"
        ).fetchall()
        conn.close()
        return [row["category"] for row in rows]

    def get_ongoing_events(self, limit: int = 5) -> list[dict]:
        """获取最近的未结束事件（end_time 为空），按 start_time 倒序"""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM events WHERE end_time IS NULL ORDER BY start_time DESC LIMIT ?",
            (limit,)
        ).fetchall()
        conn.close()
        return [dict(row) for row in rows]

    # ============ 聊天记录 ============

    def add_message(self, role: str, content: str):
        """保存一条聊天记录"""
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO messages (role, content) VALUES (?, ?)",
            (role, content)
        )
        conn.commit()
        conn.close()

    def get_recent_messages(self, limit: int = 20) -> list[dict]:
        """获取最近的聊天记录（用于构建 AI 上下文）"""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT role, content, timestamp FROM messages ORDER BY id DESC LIMIT ?",
            (limit,)
        ).fetchall()
        conn.close()
        # 反转为时间正序
        return [dict(row) for row in reversed(rows)]

    # ============ 提醒队列 ============

    def add_reminder(self, trigger_time: str, action: str) -> int:
        """添加一个提醒"""
        conn = self._get_conn()
        cursor = conn.execute(
            "INSERT INTO reminders (trigger_time, action) VALUES (?, ?)",
            (trigger_time, action)
        )
        conn.commit()
        reminder_id = cursor.lastrowid
        conn.close()
        return reminder_id

    def get_pending_reminders(self) -> list[dict]:
        """获取所有未完成且已到时间的提醒"""
        now = datetime.now().isoformat()
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM reminders WHERE done = 0 AND trigger_time <= ?",
            (now,)
        ).fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def mark_reminder_done(self, reminder_id: int):
        """标记提醒为已完成"""
        conn = self._get_conn()
        conn.execute("UPDATE reminders SET done = 1 WHERE id = ?", (reminder_id,))
        conn.commit()
        conn.close()

    # ============ 未处理消息队列 ============

    def add_pending_message(self, content: str, timestamp: str) -> int:
        """记录一条因服务不可用而未能处理的用户消息"""
        conn = self._get_conn()
        cursor = conn.execute(
            "INSERT INTO pending_messages (content, timestamp) VALUES (?, ?)",
            (content, timestamp)
        )
        conn.commit()
        pending_id = cursor.lastrowid
        conn.close()
        return pending_id

    def get_pending_messages(self) -> list[dict]:
        """获取所有未处理的消息，按时间正序"""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM pending_messages ORDER BY id"
        ).fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def delete_pending_message(self, pending_id: int):
        """删除一条已处理的待处理消息"""
        conn = self._get_conn()
        conn.execute("DELETE FROM pending_messages WHERE id = ?", (pending_id,))
        conn.commit()
        conn.close()

    # ============ 待办事件（Pending Events） ============

    def add_pending_event(self, description: str,
                          due_date: Optional[str] = None,
                          notes: Optional[str] = None) -> int:
        """添加一个待办事件，返回 event id"""
        conn = self._get_conn()
        cursor = conn.execute(
            "INSERT INTO pending_events (due_date, description, notes) VALUES (?, ?, ?)",
            (due_date, description, notes)
        )
        conn.commit()
        event_id = cursor.lastrowid
        conn.close()
        return event_id

    def get_active_pending_events(self) -> list[dict]:
        """获取所有未完成的待办事件，按 due_date 排序（NULL 排最后）"""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM pending_events WHERE done = 0 "
            "ORDER BY CASE WHEN due_date IS NULL THEN 1 ELSE 0 END, due_date"
        ).fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def complete_pending_event(self, event_id: int) -> bool:
        """标记待办事件为已完成"""
        conn = self._get_conn()
        cursor = conn.execute(
            "UPDATE pending_events SET done = 1 WHERE id = ? AND done = 0",
            (event_id,)
        )
        conn.commit()
        affected = cursor.rowcount
        conn.close()
        return affected > 0

    def update_pending_event(self, event_id: int, **fields) -> bool:
        """更新待办事件的字段"""
        allowed = {"due_date", "description", "notes"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return False
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [event_id]
        conn = self._get_conn()
        cursor = conn.execute(
            f"UPDATE pending_events SET {set_clause} WHERE id = ? AND done = 0", values
        )
        conn.commit()
        affected = cursor.rowcount
        conn.close()
        return affected > 0
