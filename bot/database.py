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
        self._on_reminder_added = None  # 回调：新增提醒时通知 scheduler
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
                group_id TEXT,                       -- 同一件事的多条 reminder 共享
                priority TEXT DEFAULT 'normal',      -- low / normal / high
                status TEXT DEFAULT 'pending',       -- pending / triggered / cancelled
                done INTEGER DEFAULT 0,              -- 兼容旧数据
                created_at TEXT DEFAULT (datetime('now'))
            );

            -- 记忆表（AI 的持久记忆）
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now')),
                source TEXT DEFAULT 'ai'
            );

            -- Todo 表（用户个人待办，不经过 AI）
            CREATE TABLE IF NOT EXISTS todos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                done INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now')),
                done_at TEXT
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
            
        try:
            conn.execute("ALTER TABLE reminders ADD COLUMN group_id TEXT")
            conn.execute("ALTER TABLE reminders ADD COLUMN priority TEXT DEFAULT 'normal'")
            conn.execute("ALTER TABLE reminders ADD COLUMN status TEXT DEFAULT 'pending'")
            # 兼容：将过去的 done 转为 status
            conn.execute("UPDATE reminders SET status = 'triggered' WHERE done = 1 AND status = 'pending'")
            conn.execute("UPDATE reminders SET status = 'pending' WHERE done = 0 AND status = 'pending'")
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

    def add_reminder(self, trigger_time: str, action: str, group_id: str = None,
                     priority: str = "normal") -> int:
        """添加一个提醒"""
        conn = self._get_conn()
        cursor = conn.execute(
            "INSERT INTO reminders (trigger_time, action, group_id, priority) VALUES (?, ?, ?, ?)",
            (trigger_time, action, group_id, priority)
        )
        conn.commit()
        reminder_id = cursor.lastrowid
        conn.close()
        # 通知 scheduler 重新计算倒计时
        if self._on_reminder_added:
            self._on_reminder_added()
        return reminder_id

    def get_pending_reminders(self) -> list[dict]:
        """获取所有未完成且已到时间的提醒"""
        now = datetime.now().isoformat()
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM reminders WHERE status = 'pending' AND trigger_time <= ?",
            (now,)
        ).fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def mark_reminder_done(self, reminder_id: int):
        """标记提醒为已触发"""
        conn = self._get_conn()
        # 同时更新 done=1 保持前端兼容
        conn.execute("UPDATE reminders SET status = 'triggered', done = 1 WHERE id = ?", (reminder_id,))
        conn.commit()
        conn.close()

    def cancel_reminders_by_group(self, group_id: str) -> int:
        """取消某个 group 下所有 pending 的 reminder，返回取消条数"""
        if not group_id:
            return 0
        conn = self._get_conn()
        cursor = conn.execute(
            "UPDATE reminders SET status = 'cancelled', done = 1 WHERE group_id = ? AND status = 'pending'",
            (group_id,)
        )
        conn.commit()
        affected = cursor.rowcount
        conn.close()
        return affected

    def get_pending_reminders_by_group(self, group_id: str) -> list[dict]:
        """查询某个 group 下还剩多少 pending 的 reminder"""
        if not group_id:
            return []
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM reminders WHERE group_id = ? AND status = 'pending' ORDER BY trigger_time",
            (group_id,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def count_reminders_in_group(self, group_id: str) -> int:
        """统计某个 group 下总共有多少条 reminder"""
        if not group_id:
            return 0
        conn = self._get_conn()
        row = conn.execute(
            "SELECT COUNT(*) FROM reminders WHERE group_id = ?",
            (group_id,)
        ).fetchone()
        conn.close()
        return row[0] if row else 0

    def list_active_reminders(self) -> list[dict]:
        """列出所有 pending 的 reminder（给 AI 看/给用户查）"""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM reminders WHERE status = 'pending' ORDER BY trigger_time ASC"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_next_reminder_time(self) -> Optional[str]:
        """获取下一条待触发 reminder 的 trigger_time（含 hidden），用于 scheduler 倒计时"""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT MIN(trigger_time) as next_time FROM reminders WHERE status = 'pending'"
        ).fetchone()
        conn.close()
        return row["next_time"] if row and row["next_time"] else None

    # ============ 记忆系统 ============

    def get_all_memories(self) -> list[dict]:
        """获取所有记忆，按时间倒序，最多20条"""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM memories ORDER BY created_at DESC LIMIT 20"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def add_memory(self, content: str, source: str = 'ai') -> int:
        """
        添加记忆。超过20条时自动清理最旧的。
        优先删 ai 来源的，保留 user 来源的。
        """
        conn = self._get_conn()
        count = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        if count >= 20:
            conn.execute("""
                DELETE FROM memories WHERE id = (
                    SELECT id FROM memories
                    ORDER BY source = 'user' ASC, created_at ASC
                    LIMIT 1
                )
            """)
        cursor = conn.execute(
            "INSERT INTO memories (content, source) VALUES (?, ?)",
            (content, source)
        )
        conn.commit()
        memory_id = cursor.lastrowid
        conn.close()
        return memory_id

    def delete_memory(self, memory_id: int):
        """删除一条记忆"""
        conn = self._get_conn()
        conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        conn.commit()
        conn.close()

    def update_memory(self, memory_id: int, content: str):
        """更新记忆内容，同时刷新 created_at 防止被自动清理"""
        conn = self._get_conn()
        conn.execute(
            "UPDATE memories SET content = ?, created_at = datetime('now') WHERE id = ?",
            (content, memory_id)
        )
        conn.commit()
        conn.close()

    # ============ Todo ============

    def add_todo(self, content: str) -> int:
        conn = self._get_conn()
        cursor = conn.execute(
            "INSERT INTO todos (content) VALUES (?)", (content,)
        )
        conn.commit()
        todo_id = cursor.lastrowid
        conn.close()
        return todo_id

    def get_todos(self, include_done: bool = False) -> list[dict]:
        conn = self._get_conn()
        if include_done:
            rows = conn.execute(
                "SELECT * FROM todos ORDER BY done ASC, created_at DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM todos WHERE done = 0 ORDER BY created_at DESC"
            ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def complete_todo(self, todo_id: int) -> bool:
        conn = self._get_conn()
        cursor = conn.execute(
            "UPDATE todos SET done = 1, done_at = datetime('now') WHERE id = ? AND done = 0",
            (todo_id,)
        )
        conn.commit()
        affected = cursor.rowcount
        conn.close()
        return affected > 0

    def delete_todo(self, todo_id: int) -> bool:
        conn = self._get_conn()
        cursor = conn.execute("DELETE FROM todos WHERE id = ?", (todo_id,))
        conn.commit()
        affected = cursor.rowcount
        conn.close()
        return affected > 0
