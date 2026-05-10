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

            -- 应用状态（进程无关的小型 kv，重启后恢复）
            -- 目前存：target_channel_id（最近一次活跃的 Discord 频道，用于冷启动主动消息）
            CREATE TABLE IF NOT EXISTS app_state (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT DEFAULT (datetime('now'))
            );

            -- Deadline 表（结构化截止日期，系统自动计算倒计时）
            CREATE TABLE IF NOT EXISTS deadlines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                due_time TEXT NOT NULL,
                status TEXT DEFAULT 'active',
                created_at TEXT DEFAULT (datetime('now'))
            );

            -- 手动管理的项目清单。
            -- AI prompt 只能读取这里的项目，不再从 events.project_name 自动反推/新增。
            CREATE TABLE IF NOT EXISTS projects (
                name TEXT PRIMARY KEY,
                created_at TEXT DEFAULT (datetime('now'))
            );

            -- 归档项目（手动管理，仅存项目名）
            -- 归档后不会再出现在 AI prompt 的现有项目列表里，
            -- 前端 ProjectOverview 默认隐藏，可切换显示。事件本身不动。
            CREATE TABLE IF NOT EXISTS archived_projects (
                project_name TEXT PRIMARY KEY,
                archived_at TEXT DEFAULT (datetime('now'))
            );

            -- 前端管理的 prompt 覆盖层。
            -- 代码里的 prompt 仍是默认值；这里只存用户改过的 section。
            CREATE TABLE IF NOT EXISTS prompt_overrides (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT DEFAULT (datetime('now'))
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
            conn.execute("ALTER TABLE events ADD COLUMN is_parallel INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass

        try:
            conn.execute("ALTER TABLE events ADD COLUMN project_name TEXT")
        except sqlite3.OperationalError:
            pass

        try:
            conn.execute("ALTER TABLE events DROP COLUMN energy_type")
        except sqlite3.OperationalError:
            pass

        # Planned event 功能已废弃：先删除遗留的 planned/cancelled 行，再 drop 列。
        try:
            conn.execute("DELETE FROM events WHERE status IS NOT NULL")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE events DROP COLUMN status")
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

        # 一次性兼容迁移：项目曾经只存在于 events.project_name。
        # 新版本改为用户手动项目表后，需要把已有历史项目注册进去，避免升级后 Project Overview 变空。
        conn.execute("""
            INSERT OR IGNORE INTO projects (name)
            SELECT DISTINCT project_name
            FROM events
            WHERE category = 'Focus'
              AND project_name IS NOT NULL
              AND project_name != ''
        """)

        conn.commit()
        conn.close()

    # ============ Prompt 管理 ============

    def get_prompt_overrides(self) -> dict[str, str]:
        """返回所有前端保存过的 prompt section 覆盖。"""
        conn = self._get_conn()
        rows = conn.execute("SELECT key, value FROM prompt_overrides ORDER BY key").fetchall()
        conn.close()
        return {row["key"]: row["value"] for row in rows}

    def list_prompt_overrides(self) -> list[dict]:
        """返回 prompt 覆盖详情，包含更新时间，给 Admin UI 展示。"""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT key, value, updated_at FROM prompt_overrides ORDER BY key"
        ).fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def set_prompt_override(self, key: str, value: str) -> bool:
        """保存单个 prompt 覆盖。返回是否发生变化。"""
        k = (key or "").strip()
        v = (value or "").strip()
        if not k or not v:
            return False
        conn = self._get_conn()
        current = conn.execute(
            "SELECT value FROM prompt_overrides WHERE key = ?",
            (k,)
        ).fetchone()
        conn.execute(
            "INSERT INTO prompt_overrides (key, value, updated_at) VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = datetime('now')",
            (k, v)
        )
        conn.commit()
        conn.close()
        return current is None or current["value"] != v

    def delete_prompt_override(self, key: str) -> bool:
        """删除单个 prompt 覆盖，使该 section 回到代码默认值。"""
        k = (key or "").strip()
        if not k:
            return False
        conn = self._get_conn()
        cursor = conn.execute("DELETE FROM prompt_overrides WHERE key = ?", (k,))
        conn.commit()
        changed = cursor.rowcount > 0
        conn.close()
        return changed

    # ============ 时间轴事件 ============

    def add_event(self, start_time: str, end_time: Optional[str],
                  content: str, category: str = "uncategorized",
                  notes: Optional[str] = None, session_id: Optional[int] = None,
                  is_parallel: bool = False,
                  project_name: Optional[str] = None) -> int:
        """添加一条时间轴事件，返回 event id。"""
        conn = self._get_conn()
        cursor = conn.execute(
            "INSERT INTO events (start_time, end_time, content, category, notes, session_id, is_parallel, project_name) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (start_time, end_time, content, category, notes, session_id, 1 if is_parallel else 0, project_name)
        )
        conn.commit()
        event_id = cursor.lastrowid
        conn.close()
        return event_id

    def delete_event(self, event_id: int) -> bool:
        """删除一条时间轴事件。返回是否成功。"""
        conn = self._get_conn()
        cursor = conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
        conn.commit()
        affected = cursor.rowcount
        conn.close()
        return affected > 0

    def find_similar_events(self, content: str, category: Optional[str],
                            start: str, end: str) -> list[dict]:
        """
        在给定时间窗口内查找 content（和可选 category）相同的事件。
        用于 AI 新建前的重复检测。content 必须完全匹配（已在 prompt 里要求保持简洁一致）。
        """
        conn = self._get_conn()
        if category:
            rows = conn.execute(
                "SELECT * FROM events WHERE content = ? AND category = ? "
                "AND start_time >= ? AND start_time <= ? ORDER BY start_time",
                (content, category, start, end)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM events WHERE content = ? "
                "AND start_time >= ? AND start_time <= ? ORDER BY start_time",
                (content, start, end)
            ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def update_event(self, event_id: int, **fields) -> bool:
        """更新指定事件的字段，只更新传入的字段。返回是否成功（event_id 存在）。"""
        allowed = {"end_time", "content", "category", "notes", "session_id", "is_parallel", "project_name"}
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
        """查询时间范围内的事件（包括跨日事件：start_time 在范围之前但 end_time 在范围内的）"""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM events WHERE "
            "(start_time >= ? AND start_time <= ?) "
            "OR (start_time < ? AND end_time > ?) "
            "ORDER BY start_time",
            (start, end, start, start)
        ).fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def get_event_by_id(self, event_id: int) -> Optional[dict]:
        """根据 ID 获取单条事件"""
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        conn.close()
        return dict(row) if row else None

    def get_all_categories(self) -> list[str]:
        """获取所有已有的分类"""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT DISTINCT category FROM events ORDER BY category"
        ).fetchall()
        conn.close()
        return [row["category"] for row in rows]

    def get_all_project_names(self, include_archived: bool = False) -> list[dict]:
        """获取手动创建的项目名及真实 Focus 事件数。

        默认排除已归档项目——AI prompt 取动态上下文时不该再看到归档项目，避免它继续被复用。
        项目清单来自 projects 表，不从历史事件自动反推，保证 AI 只能看到用户建立的项目。
        """
        conn = self._get_conn()
        if include_archived:
            rows = conn.execute(
                "SELECT p.name AS project_name, COUNT(e.id) AS cnt "
                "FROM projects p "
                "LEFT JOIN events e ON e.project_name = p.name "
                "AND e.category = 'Focus' "
                "GROUP BY p.name ORDER BY cnt DESC, p.created_at DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT p.name AS project_name, COUNT(e.id) AS cnt "
                "FROM projects p "
                "LEFT JOIN events e ON e.project_name = p.name "
                "AND e.category = 'Focus' "
                "WHERE p.name NOT IN (SELECT project_name FROM archived_projects) "
                "GROUP BY p.name ORDER BY cnt DESC, p.created_at DESC"
            ).fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def project_exists(self, project_name: str) -> bool:
        """检查项目是否在手动项目清单中。"""
        name = (project_name or "").strip()
        if not name:
            return False
        conn = self._get_conn()
        row = conn.execute("SELECT 1 FROM projects WHERE name = ?", (name,)).fetchone()
        conn.close()
        return row is not None

    def add_project(self, project_name: str) -> bool:
        """创建一个项目，返回是否新增（已存在则 False）。"""
        name = (project_name or "").strip()
        if not name:
            return False
        conn = self._get_conn()
        cursor = conn.execute(
            "INSERT OR IGNORE INTO projects (name) VALUES (?)",
            (name,)
        )
        conn.commit()
        changed = cursor.rowcount > 0
        conn.close()
        return changed

    def delete_project(self, project_name: str) -> bool:
        """从手动项目清单删除项目，不删除历史事件。"""
        name = (project_name or "").strip()
        if not name:
            return False
        conn = self._get_conn()
        cursor = conn.execute("DELETE FROM projects WHERE name = ?", (name,))
        conn.execute("DELETE FROM archived_projects WHERE project_name = ?", (name,))
        conn.commit()
        changed = cursor.rowcount > 0
        conn.close()
        return changed

    def rename_project(self, old_name: str, new_name: str) -> bool:
        """重命名项目，并同步历史事件与归档状态。"""
        old = (old_name or "").strip()
        new = (new_name or "").strip()
        if not old or not new or old == new:
            return False
        conn = self._get_conn()
        exists = conn.execute("SELECT 1 FROM projects WHERE name = ?", (old,)).fetchone()
        target_exists = conn.execute("SELECT 1 FROM projects WHERE name = ?", (new,)).fetchone()
        if not exists or target_exists:
            conn.close()
            return False
        conn.execute("UPDATE projects SET name = ? WHERE name = ?", (new, old))
        conn.execute("UPDATE events SET project_name = ? WHERE project_name = ?", (new, old))
        conn.execute("DELETE FROM archived_projects WHERE project_name = ?", (new,))
        conn.execute("UPDATE archived_projects SET project_name = ? WHERE project_name = ?", (new, old))
        conn.commit()
        conn.close()
        return True

    def get_archived_project_names(self) -> list[str]:
        """已归档项目名列表，按归档时间倒序。"""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT a.project_name FROM archived_projects a "
            "JOIN projects p ON p.name = a.project_name "
            "ORDER BY a.archived_at DESC"
        ).fetchall()
        conn.close()
        return [row["project_name"] for row in rows]

    def archive_project(self, project_name: str) -> bool:
        """把项目名加进归档表，返回是否新增（已归档则 False，幂等）。"""
        name = (project_name or "").strip()
        if not name or not self.project_exists(name):
            return False
        conn = self._get_conn()
        cursor = conn.execute(
            "INSERT OR IGNORE INTO archived_projects (project_name) VALUES (?)",
            (name,)
        )
        conn.commit()
        changed = cursor.rowcount > 0
        conn.close()
        return changed

    def unarchive_project(self, project_name: str) -> bool:
        """从归档表移除，返回是否真的删了一行。"""
        name = (project_name or "").strip()
        if not name:
            return False
        conn = self._get_conn()
        cursor = conn.execute(
            "DELETE FROM archived_projects WHERE project_name = ?",
            (name,)
        )
        conn.commit()
        changed = cursor.rowcount > 0
        conn.close()
        return changed

    def get_ongoing_events(self, limit: int = 5) -> list[dict]:
        """获取最近的未结束事件（end_time 为空），按 start_time 倒序。"""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM events WHERE end_time IS NULL "
            "ORDER BY start_time DESC LIMIT ?",
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

    def cancel_reminder_by_id(self, reminder_id: int) -> bool:
        """按 id 精准取消单条 pending reminder（用于 AI 去重）。
        只对 status='pending' 的条目生效；已触发/已取消的不会被重复动。
        返回是否命中。"""
        conn = self._get_conn()
        cursor = conn.execute(
            "UPDATE reminders SET status = 'cancelled', done = 1 WHERE id = ? AND status = 'pending'",
            (reminder_id,)
        )
        conn.commit()
        affected = cursor.rowcount
        conn.close()
        return affected > 0

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

    def set_todo_done(self, todo_id: int, done: bool) -> bool:
        conn = self._get_conn()
        if done:
            cursor = conn.execute(
                "UPDATE todos SET done = 1, done_at = datetime('now') WHERE id = ?",
                (todo_id,)
            )
        else:
            cursor = conn.execute(
                "UPDATE todos SET done = 0, done_at = NULL WHERE id = ?",
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

    # ============ 应用状态 KV ============

    def set_state(self, key: str, value: str):
        """写入/更新一条应用状态"""
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO app_state (key, value, updated_at) VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = datetime('now')",
            (key, value)
        )
        conn.commit()
        conn.close()

    def get_state(self, key: str) -> Optional[str]:
        """读取一条应用状态，不存在返回 None"""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT value FROM app_state WHERE key = ?", (key,)
        ).fetchone()
        conn.close()
        return row["value"] if row else None

    # ============ Deadline 管理 ============

    def add_deadline(self, title: str, due_time: str) -> int:
        """添加一条 deadline，返回 id"""
        conn = self._get_conn()
        cursor = conn.execute(
            "INSERT INTO deadlines (title, due_time) VALUES (?, ?)",
            (title, due_time)
        )
        conn.commit()
        deadline_id = cursor.lastrowid
        conn.close()
        return deadline_id

    def complete_deadline(self, deadline_id: int) -> bool:
        """标记 deadline 为已完成"""
        conn = self._get_conn()
        cursor = conn.execute(
            "UPDATE deadlines SET status = 'completed' WHERE id = ? AND status = 'active'",
            (deadline_id,)
        )
        conn.commit()
        affected = cursor.rowcount
        conn.close()
        return affected > 0

    def delete_deadline(self, deadline_id: int) -> bool:
        """删除一条 deadline"""
        conn = self._get_conn()
        cursor = conn.execute("DELETE FROM deadlines WHERE id = ?", (deadline_id,))
        conn.commit()
        affected = cursor.rowcount
        conn.close()
        return affected > 0

    def get_active_deadlines(self) -> list[dict]:
        """获取所有 active 的 deadline，按 due_time 正序"""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM deadlines WHERE status = 'active' ORDER BY due_time ASC"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def expire_past_deadlines(self) -> int:
        """将已过期的 active deadline 标记为 expired，返回影响行数"""
        now = datetime.now().isoformat()
        conn = self._get_conn()
        cursor = conn.execute(
            "UPDATE deadlines SET status = 'expired' WHERE status = 'active' AND due_time < ?",
            (now,)
        )
        conn.commit()
        affected = cursor.rowcount
        conn.close()
        return affected
