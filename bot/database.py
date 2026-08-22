"""
数据库模块：SQLite 操作
存储时间轴事件、聊天记录、提醒等
"""
import sqlite3
import os
import json
from datetime import datetime, time, timezone
from typing import Optional

from bot.embeddings import CONTEXT_MESSAGES, cosine_similarity, recency_weight


def _normalize_memory_valid_until(value: str | None) -> str | None:
    """Normalize memory expiry to SQLite UTC datetime text, or None for permanent."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None

    try:
        if len(raw) == 10:
            # Frontend date inputs send YYYY-MM-DD. Treat that as valid through the
            # end of the selected UTC day, not expired at that day's midnight.
            dt = datetime.combine(datetime.fromisoformat(raw).date(), time.max)
        else:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt.isoformat(sep=" ", timespec="seconds")
    except ValueError:
        # Keep unparseable values untouched so existing callers can still see/edit
        # what was supplied; the SELECT path uses datetime() and won't rely on
        # lexicographic ordering for these strings.
        return raw


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

            -- Discord 原始会话日志（append-only），用于后续 Context Builder / compact / replay。
            -- 旧 messages 表只保留 role/content/timestamp 兼容；这里保存完整 Discord 元数据。
            CREATE TABLE IF NOT EXISTS conversation_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_message_id TEXT UNIQUE,
                channel_id TEXT NOT NULL,
                guild_id TEXT,
                author_id TEXT,
                author_name TEXT,
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant', 'system')),
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                reply_to_message_id TEXT,
                metadata_json TEXT,
                embedding TEXT,             -- JSON float 数组，后台异步补写，NULL = 还没算/失败
                embedding_context TEXT,     -- 实际拿去 embed 的拼接文本（含前几条消息上下文）
                embedding_model TEXT        -- 算这条 embedding 用的模型，检索时只比对同模型的行
            );

            CREATE INDEX IF NOT EXISTS idx_conversation_messages_channel_created
                ON conversation_messages(channel_id, created_at);

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

            -- 前端管理的 prompt 正文。prompt 内容不再放进 Git。
            CREATE TABLE IF NOT EXISTS prompt_sections (
                key TEXT PRIMARY KEY,
                label TEXT NOT NULL,
                value TEXT NOT NULL DEFAULT '',
                updated_at TEXT DEFAULT (datetime('now'))
            );

            -- 可配置系统 check-in。用于长期/重复的主动触发项；
            -- 一次性 reminders 仍保留在 reminders 表。
            CREATE TABLE IF NOT EXISTS check_ins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                label TEXT NOT NULL,
                enabled INTEGER DEFAULT 1,
                schedule_type TEXT NOT NULL,
                time_start TEXT,
                time_end TEXT,
                days_of_week TEXT,
                interval_min_minutes INTEGER,
                interval_max_minutes INTEGER,
                prompt_template TEXT NOT NULL DEFAULT '',
                instructions TEXT DEFAULT '',
                context_config_json TEXT,
                tool_profile TEXT NOT NULL DEFAULT 'poll',
                allow_silent INTEGER DEFAULT 1,
                last_scheduled_for TEXT,
                last_fired_at TEXT,
                built_in INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );

            -- AI 行为可追溯性：每次 AI 调用（chat/scheduled_action）一行，
            -- 复用 bot/trace.py 的 run 生命周期，finalize() 时落库。
            CREATE TABLE IF NOT EXISTS ai_runs (
                id TEXT PRIMARY KEY,
                trigger TEXT NOT NULL,
                model TEXT,
                provider TEXT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT,
                error TEXT,
                final_text TEXT
            );

            -- 每次工具调用一行，关联到 ai_runs。
            CREATE TABLE IF NOT EXISTS tool_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES ai_runs(id),
                round_n INTEGER,
                tool_name TEXT NOT NULL,
                arguments_json TEXT,
                result_json TEXT,
                success INTEGER,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_tool_calls_run_id ON tool_calls(run_id);

            -- 记忆系统 v4：异步 curator 维护的长期记忆。旧 memories 表在
            -- LT-132 前仍是 memory.md shadow，不能复用或覆盖。
            CREATE TABLE IF NOT EXISTS personal_memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                summary TEXT NOT NULL,
                quote TEXT,
                reason TEXT NOT NULL,
                memory_type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK(status IN ('active', 'superseded', 'archived')),
                superseded_by INTEGER REFERENCES personal_memories(id),
                curator_model TEXT NOT NULL,
                embedding TEXT,
                embedding_model TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                CHECK(superseded_by IS NULL OR superseded_by != id)
            );

            CREATE INDEX IF NOT EXISTS idx_personal_memories_status_type_updated
                ON personal_memories(status, memory_type, updated_at DESC);

            CREATE TABLE IF NOT EXISTS personal_memory_sources (
                memory_id INTEGER NOT NULL REFERENCES personal_memories(id) ON DELETE CASCADE,
                conversation_message_id INTEGER NOT NULL
                    REFERENCES conversation_messages(id) ON DELETE RESTRICT,
                quote TEXT,
                evidence_role TEXT NOT NULL DEFAULT 'supports'
                    CHECK(evidence_role IN ('supports', 'contradicts',
                                            'supersedes', 'contextualizes')),
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY(memory_id, conversation_message_id, evidence_role)
            );

            CREATE INDEX IF NOT EXISTS idx_personal_memory_sources_message
                ON personal_memory_sources(conversation_message_id);

            CREATE TABLE IF NOT EXISTS curator_cursors (
                curator_name TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                last_message_id INTEGER NOT NULL DEFAULT 0,
                last_successful_run_id TEXT REFERENCES ai_runs(id),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY(curator_name, channel_id)
            );
        """)
        conn.commit()
        # 兼容已有库：evidence_role CHECK 扩容（contextualizes）。
        # SQLite 无法修改 CHECK，只能重建表；没有外键指向本表，重建安全
        sources_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='personal_memory_sources'").fetchone()
        if sources_sql and "contextualizes" not in (sources_sql[0] or ""):
            conn.executescript("""
                CREATE TABLE personal_memory_sources_new (
                    memory_id INTEGER NOT NULL REFERENCES personal_memories(id) ON DELETE CASCADE,
                    conversation_message_id INTEGER NOT NULL
                        REFERENCES conversation_messages(id) ON DELETE RESTRICT,
                    quote TEXT,
                    evidence_role TEXT NOT NULL DEFAULT 'supports'
                        CHECK(evidence_role IN ('supports', 'contradicts',
                                                'supersedes', 'contextualizes')),
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    PRIMARY KEY(memory_id, conversation_message_id, evidence_role)
                );
                INSERT INTO personal_memory_sources_new
                    SELECT memory_id, conversation_message_id, quote,
                           evidence_role, created_at
                    FROM personal_memory_sources;
                DROP TABLE personal_memory_sources;
                ALTER TABLE personal_memory_sources_new
                    RENAME TO personal_memory_sources;
                CREATE INDEX IF NOT EXISTS idx_personal_memory_sources_message
                    ON personal_memory_sources(conversation_message_id);
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
            conn.execute("ALTER TABLE memories ADD COLUMN memory_type TEXT")
        except sqlite3.OperationalError:
            pass

        try:
            conn.execute("ALTER TABLE memories ADD COLUMN valid_until TEXT")
        except sqlite3.OperationalError:
            pass

        # memory v3 Part B2：对话日志 embedding 检索
        try:
            conn.execute("ALTER TABLE conversation_messages ADD COLUMN embedding TEXT")
            conn.execute("ALTER TABLE conversation_messages ADD COLUMN embedding_context TEXT")
            conn.execute("ALTER TABLE conversation_messages ADD COLUMN embedding_model TEXT")
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

        for stmt in (
            "ALTER TABLE check_ins ADD COLUMN instructions TEXT DEFAULT ''",
            "ALTER TABLE check_ins ADD COLUMN context_config_json TEXT",
            "ALTER TABLE check_ins ADD COLUMN allow_silent INTEGER DEFAULT 1",
            "ALTER TABLE check_ins ADD COLUMN last_scheduled_for TEXT",
            "ALTER TABLE check_ins ADD COLUMN last_fired_at TEXT",
            "ALTER TABLE check_ins ADD COLUMN built_in INTEGER DEFAULT 0",
            "ALTER TABLE check_ins ADD COLUMN updated_at TEXT DEFAULT (datetime('now'))",
        ):
            try:
                conn.execute(stmt)
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

        from bot.prompts import PROMPT_SECTION_LABELS
        from bot.prompt_store import (
            initialize_prompts_if_empty,
            migrate_main_template_if_missing,
        )
        for key, label in PROMPT_SECTION_LABELS.items():
            conn.execute(
                """
                INSERT INTO prompt_sections (key, label, value, updated_at)
                VALUES (?, ?, '', datetime('now'))
                ON CONFLICT(key) DO UPDATE SET label = excluded.label
                """,
                (key, label),
            )
        initialize_prompts_if_empty(conn)
        # LT-129：存量库从旧结构化 section 合成 main_template（fresh 库上面
        # 一步已灌默认集，这里是 no-op）
        if migrate_main_template_if_missing(conn):
            from bot.logger import get_logger
            get_logger(__name__).info("✅ [LT-129] main_template 已从旧结构化 prompt section 合成")

        self._ensure_default_check_ins(conn)

        conn.commit()
        conn.close()

    # 永久内置的 check-in：built_in=1，不允许删除。原先有一个全局的
    # checkin_ttl_followup_enabled 开关 gate 住所有 after_ai_call 类型的
    # check-in，它会在条目自己 enabled=1 的情况下静默阻止调度，两个开关语义
    # 重复且界面上毫无关联，所以取消了，改成这个删不掉的条目本身。
    PERMANENT_CHECK_INS = ("ttl_followup",)
    # 只在数据库第一次初始化时灌入的默认 check-in：built_in=0，默认开启，
    # 用户删掉之后不会在下次启动时被重新插回来。
    SEEDED_CHECK_INS = ("random_poll", "morning", "bedtime_1", "bedtime_2")
    CHECK_IN_SEED_FLAG = "check_in_defaults_seeded"
    # 间隔被锁死的内置 check-in：调度器不读这两列，Admin 页面也改不动。
    # 数据库里仍然存着同样的值，只为在界面上如实显示。
    LOCKED_CHECK_IN_INTERVALS = {"ttl_followup": (45, 55)}

    def _ensure_default_check_ins(self, conn: sqlite3.Connection) -> None:
        """Seed built-in configurable check-ins without overwriting user edits.

        两类默认 check-in 的处理方式不同：

        - PERMANENT_CHECK_INS 每次启动都确保存在，并且强制 built_in=1；
        - SEEDED_CHECK_INS 只在首次初始化时灌一次，强制 built_in=0。
          是否已经灌过记录在 app_state 的 CHECK_IN_SEED_FLAG 里——如果不记，
          每次启动的 INSERT OR IGNORE 都会把用户刚删掉的默认项重新插回来，
          「可删除」就形同虚设。
        """
        rows = conn.execute("SELECT key, value FROM prompt_sections").fetchall()
        sections = {row["key"]: row["value"] for row in rows}
        state_row = conn.execute(
            "SELECT value FROM app_state WHERE key = 'poll_enabled'"
        ).fetchone()
        random_poll_enabled = state_row["value"] if state_row else None
        if random_poll_enabled is None:
            random_poll_enabled = "1"

        defaults = [
            {
                # 永久内置项：间隔硬编码 45-55min，删不掉也改不了间隔，
                # 但可以开关、可以改 prompt。默认关闭——开着的话会和
                # random_poll 抢同一个「上次 AI 调用」基准。
                "name": "ttl_followup",
                "label": "TTL follow-up",
                "enabled": 0,
                "schedule_type": "after_ai_call",
                "interval_min_minutes": 45,
                "interval_max_minutes": 55,
                "prompt_template": sections.get("proactive_claude") or sections.get("proactive_gemini") or (
                    "Current timestamp: {timestamp}\n\n"
                    "Decide whether to proactively message the user. If there is nothing useful to say, respond with [SILENT]."
                ),
                "tool_profile": "poll",
                "allow_silent": 1,
            },
            {
                "name": "random_poll",
                "label": "Random poll",
                "enabled": 1 if random_poll_enabled != "0" else 0,
                "schedule_type": "after_ai_call",
                "interval_min_minutes": 45,
                "interval_max_minutes": 55,
                "prompt_template": sections.get("proactive_claude") or sections.get("proactive_gemini") or (
                    "Current timestamp: {timestamp}\n\n"
                    "Decide whether to proactively message the user. If there is nothing useful to say, respond with [SILENT]."
                ),
                "tool_profile": "poll",
                "allow_silent": 1,
            },
            {
                "name": "morning",
                "label": "Morning check-in",
                "enabled": 1,
                "schedule_type": "window",
                "time_start": "08:00",
                "time_end": "09:00",
                "prompt_template": sections.get("morning") or (
                    "Current timestamp: {timestamp}\n\n"
                    "This is a morning check-in. If useful, help the user orient around today's known commitments and one practical next step. If not useful, respond with [SILENT]."
                ),
                "tool_profile": "poll",
                "allow_silent": 1,
            },
            {
                "name": "bedtime_1",
                "label": "Bedtime check-in 1",
                "enabled": 1,
                "schedule_type": "window",
                "time_start": "22:30",
                "time_end": "23:30",
                "prompt_template": sections.get("bedtime") or (
                    "Current timestamp: {timestamp}\n\n"
                    "This is a bedtime check-in. If useful, help the user close the day with a brief summary or one practical next step. If not useful, respond with [SILENT]."
                ),
                "tool_profile": "poll",
                "allow_silent": 1,
            },
            {
                "name": "bedtime_2",
                "label": "Bedtime check-in 2",
                "enabled": 1,
                "schedule_type": "window",
                "time_start": "23:30",
                "time_end": "00:00",
                "prompt_template": sections.get("bedtime") or (
                    "Current timestamp: {timestamp}\n\n"
                    "This is a bedtime check-in. If useful, help the user close the day with a brief summary or one practical next step. If not useful, respond with [SILENT]."
                ),
                "tool_profile": "poll",
                "allow_silent": 1,
            },
        ]

        default_context = json.dumps({
            "include_projects": True,
            "include_memories": True,
            "include_relevant_history": True,
            "include_today_timeline": True,
            "include_pending_reminders": True,
            "include_deadlines": True,
            "include_weather": True,
            "include_calendar": True,
        })

        already_seeded = conn.execute(
            "SELECT 1 FROM app_state WHERE key = ?", (self.CHECK_IN_SEED_FLAG,)
        ).fetchone() is not None

        for item in defaults:
            permanent = item["name"] in self.PERMANENT_CHECK_INS
            if not permanent and already_seeded:
                continue
            conn.execute(
                """
                INSERT OR IGNORE INTO check_ins (
                    name, label, enabled, schedule_type, time_start, time_end,
                    days_of_week, interval_min_minutes, interval_max_minutes,
                    prompt_template, instructions, context_config_json,
                    tool_profile, allow_silent, built_in
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item["name"],
                    item["label"],
                    item["enabled"],
                    item["schedule_type"],
                    item.get("time_start"),
                    item.get("time_end"),
                    None,
                    item.get("interval_min_minutes"),
                    item.get("interval_max_minutes"),
                    item["prompt_template"],
                    "",
                    default_context,
                    item["tool_profile"],
                    item["allow_silent"],
                    1 if permanent else 0,
                ),
            )

        if not already_seeded:
            conn.execute(
                """
                INSERT INTO app_state (key, value, updated_at)
                VALUES (?, '1', datetime('now'))
                ON CONFLICT(key) DO NOTHING
                """,
                (self.CHECK_IN_SEED_FLAG,),
            )

        # 存量库迁移：morning / bedtime_* 原本也是 built_in=1（删不掉），
        # 现在降级成普通的默认项；random_poll 反过来必须保证是 1。
        seeded_placeholders = ",".join("?" * len(self.SEEDED_CHECK_INS))
        conn.execute(
            f"UPDATE check_ins SET built_in = 0 WHERE name IN ({seeded_placeholders})",
            self.SEEDED_CHECK_INS,
        )
        permanent_placeholders = ",".join("?" * len(self.PERMANENT_CHECK_INS))
        conn.execute(
            f"UPDATE check_ins SET built_in = 1 WHERE name IN ({permanent_placeholders})",
            self.PERMANENT_CHECK_INS,
        )
        # 锁定间隔的内置项：把数据库里的展示值强制同步回硬编码值。调度器根本
        # 不读这两列，有人直接改库的话，Admin 上显示的就会和实际跑的不一致。
        for locked_name, (lo, hi) in self.LOCKED_CHECK_IN_INTERVALS.items():
            conn.execute(
                "UPDATE check_ins SET interval_min_minutes = ?, interval_max_minutes = ? "
                "WHERE name = ?",
                (lo, hi, locked_name),
            )
        # 全局 TTL follow-up 开关已经取消，遗留的键会误导后来读库的人。
        conn.execute("DELETE FROM app_state WHERE key = 'checkin_ttl_followup_enabled'")

    # ============ Prompt 管理 ============

    def get_prompt_sections(self) -> dict[str, str]:
        """返回所有 DB 管理的 prompt sections。"""
        conn = self._get_conn()
        rows = conn.execute("SELECT key, value FROM prompt_sections ORDER BY key").fetchall()
        conn.close()
        return {row["key"]: row["value"] for row in rows}

    def list_prompt_sections(self) -> list[dict]:
        """返回 prompt sections，包含更新时间，给 Admin UI 展示。"""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT key, label, value, updated_at FROM prompt_sections ORDER BY key"
        ).fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def set_prompt_section(self, key: str, value: str) -> bool:
        """保存单个 prompt section。返回是否发生变化。"""
        k = (key or "").strip()
        v = (value or "").strip()
        if not k:
            return False
        from bot.prompts import PROMPT_SECTION_LABELS
        if k not in PROMPT_SECTION_LABELS:
            raise ValueError(f"unknown prompt section: {k}")
        conn = self._get_conn()
        current = conn.execute(
            "SELECT value FROM prompt_sections WHERE key = ?",
            (k,)
        ).fetchone()
        conn.execute(
            """
            INSERT INTO prompt_sections (key, label, value, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(key) DO UPDATE SET
                label = excluded.label,
                value = excluded.value,
                updated_at = datetime('now')
            """,
            (k, PROMPT_SECTION_LABELS[k], v)
        )
        conn.commit()
        conn.close()
        return current is None or current["value"] != v

    # ============ Check-in 管理 ============

    CHECK_IN_SCHEDULE_TYPES = {"window", "after_ai_call"}
    CHECK_IN_TOOL_PROFILES = {"poll", "reminder_safe", "none"}

    @staticmethod
    def _decode_check_in(row: sqlite3.Row) -> dict:
        item = dict(row)
        for key in ("enabled", "allow_silent", "built_in"):
            item[key] = bool(item.get(key))
        for key, default in (
            ("days_of_week", None),
            ("context_config_json", {}),
        ):
            raw = item.get(key)
            if raw:
                try:
                    item[key] = json.loads(raw)
                except json.JSONDecodeError:
                    item[key] = default
            else:
                item[key] = default
        item["context_config"] = item.pop("context_config_json")
        # 间隔是否硬编码锁死，供 Admin 页面把输入框禁掉——不然用户改了保存，
        # 后端静默丢弃，界面上却像是改成功了。
        item["interval_locked"] = item.get("name") in Database.LOCKED_CHECK_IN_INTERVALS
        return item

    @staticmethod
    def _encode_json(value, default):
        if value is None:
            value = default
        return json.dumps(value)

    def list_check_ins(self, enabled_only: bool = False) -> list[dict]:
        """List configured check-ins, built-ins first."""
        conn = self._get_conn()
        if enabled_only:
            rows = conn.execute(
                """
                SELECT * FROM check_ins
                WHERE enabled = 1
                ORDER BY built_in DESC, id ASC
                """
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM check_ins ORDER BY built_in DESC, id ASC"
            ).fetchall()
        conn.close()
        return [self._decode_check_in(row) for row in rows]

    def get_check_in(self, check_in_id_or_name) -> Optional[dict]:
        conn = self._get_conn()
        if isinstance(check_in_id_or_name, int) or str(check_in_id_or_name).isdigit():
            row = conn.execute(
                "SELECT * FROM check_ins WHERE id = ?",
                (int(check_in_id_or_name),),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM check_ins WHERE name = ?",
                (str(check_in_id_or_name),),
            ).fetchone()
        conn.close()
        return self._decode_check_in(row) if row else None

    def create_check_in(self, **fields) -> int:
        name = (fields.get("name") or "").strip()
        label = (fields.get("label") or name).strip()
        prompt_template = (fields.get("prompt_template") or "").strip()
        schedule_type = (fields.get("schedule_type") or "").strip()
        tool_profile = (fields.get("tool_profile") or "poll").strip()
        if not name:
            raise ValueError("name required")
        if not prompt_template:
            raise ValueError("prompt_template required")
        if schedule_type not in self.CHECK_IN_SCHEDULE_TYPES:
            raise ValueError(f"invalid schedule_type: {schedule_type}")
        if tool_profile not in self.CHECK_IN_TOOL_PROFILES:
            raise ValueError(f"invalid tool_profile: {tool_profile}")
        conn = self._get_conn()
        cursor = conn.execute(
            """
            INSERT INTO check_ins (
                name, label, enabled, schedule_type, time_start, time_end,
                days_of_week, interval_min_minutes, interval_max_minutes,
                prompt_template, instructions, context_config_json,
                tool_profile, allow_silent, built_in
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                name,
                label,
                1 if fields.get("enabled", True) else 0,
                schedule_type,
                fields.get("time_start"),
                fields.get("time_end"),
                self._encode_json(fields.get("days_of_week"), None),
                fields.get("interval_min_minutes"),
                fields.get("interval_max_minutes"),
                prompt_template,
                fields.get("instructions") or "",
                self._encode_json(fields.get("context_config"), {}),
                tool_profile,
                1 if fields.get("allow_silent", True) else 0,
            ),
        )
        conn.commit()
        check_in_id = cursor.lastrowid
        conn.close()
        return check_in_id

    def update_check_in(self, check_in_id_or_name, **fields) -> bool:
        allowed = {
            "label", "enabled", "schedule_type", "time_start", "time_end",
            "days_of_week", "interval_min_minutes", "interval_max_minutes",
            "prompt_template", "instructions", "context_config",
            "tool_profile", "allow_silent", "last_scheduled_for",
            "last_fired_at",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return False
        if "schedule_type" in updates and updates["schedule_type"] not in self.CHECK_IN_SCHEDULE_TYPES:
            raise ValueError(f"invalid schedule_type: {updates['schedule_type']}")
        if "tool_profile" in updates and updates["tool_profile"] not in self.CHECK_IN_TOOL_PROFILES:
            raise ValueError(f"invalid tool_profile: {updates['tool_profile']}")
        if {"interval_min_minutes", "interval_max_minutes"} & updates.keys():
            if self._locked_interval_for(check_in_id_or_name):
                # Admin 的编辑弹窗每次提交整份表单，必然带上这两列。这里不能报错，
                # 否则改 prompt 会被连坐；静默丢弃即可——调度器本来就不读它们。
                updates.pop("interval_min_minutes", None)
                updates.pop("interval_max_minutes", None)
        if "context_config" in updates:
            updates["context_config_json"] = self._encode_json(updates.pop("context_config"), {})
        if "days_of_week" in updates:
            updates["days_of_week"] = self._encode_json(updates["days_of_week"], None)
        for key in ("enabled", "allow_silent"):
            if key in updates:
                updates[key] = 1 if updates[key] else 0
        updates["updated_at"] = datetime.now().isoformat(timespec="seconds")

        set_clause = ", ".join(f"{key} = ?" for key in updates)
        values = list(updates.values())
        conn = self._get_conn()
        if isinstance(check_in_id_or_name, int) or str(check_in_id_or_name).isdigit():
            values.append(int(check_in_id_or_name))
            cursor = conn.execute(
                f"UPDATE check_ins SET {set_clause} WHERE id = ?",
                values,
            )
        else:
            values.append(str(check_in_id_or_name))
            cursor = conn.execute(
                f"UPDATE check_ins SET {set_clause} WHERE name = ?",
                values,
            )
        conn.commit()
        affected = cursor.rowcount
        conn.close()
        return affected > 0

    def _locked_interval_for(self, check_in_id_or_name) -> tuple[int, int] | None:
        """这条 check-in 的间隔是不是硬编码锁死的；不是则返回 None。"""
        if isinstance(check_in_id_or_name, int) or str(check_in_id_or_name).isdigit():
            conn = self._get_conn()
            row = conn.execute(
                "SELECT name FROM check_ins WHERE id = ?",
                (int(check_in_id_or_name),),
            ).fetchone()
            conn.close()
            name = row["name"] if row else None
        else:
            name = str(check_in_id_or_name)
        return self.LOCKED_CHECK_IN_INTERVALS.get(name) if name else None

    def delete_check_in(self, check_in_id_or_name) -> bool:
        """Delete custom check-ins. Built-ins are preserved and should be disabled."""
        conn = self._get_conn()
        if isinstance(check_in_id_or_name, int) or str(check_in_id_or_name).isdigit():
            cursor = conn.execute(
                "DELETE FROM check_ins WHERE id = ? AND built_in = 0",
                (int(check_in_id_or_name),),
            )
        else:
            cursor = conn.execute(
                "DELETE FROM check_ins WHERE name = ? AND built_in = 0",
                (str(check_in_id_or_name),),
            )
        conn.commit()
        affected = cursor.rowcount
        conn.close()
        return affected > 0

    def set_check_in_last_scheduled(self, check_in_id: int, scheduled_for: str | None) -> None:
        self.update_check_in(check_in_id, last_scheduled_for=scheduled_for)

    def mark_check_in_fired(self, check_in_id: int, fired_at: str | None = None) -> None:
        self.update_check_in(
            check_in_id,
            last_fired_at=fired_at or datetime.now().isoformat(timespec="seconds"),
            last_scheduled_for=None,
        )

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

    def get_today_events(self, now: Optional[datetime] = None) -> list[dict]:
        """获取本地今天完整 timeline 原始事件。"""
        now = now or datetime.now()
        start = now.strftime("%Y-%m-%dT00:00:00")
        end = now.strftime("%Y-%m-%dT23:59:59")
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM events WHERE "
            "(start_time >= ? AND start_time <= ?) "
            "OR (start_time < ? AND end_time > ?) "
            "OR (start_time < ? AND end_time IS NULL) "
            "ORDER BY start_time",
            (start, end, start, start, start)
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

    # ============ Discord 原始会话日志 ============

    def add_conversation_message(self, *,
                                 discord_message_id: str | None,
                                 channel_id: str,
                                 role: str,
                                 content: str,
                                 created_at: str,
                                 guild_id: str | None = None,
                                 author_id: str | None = None,
                                 author_name: str | None = None,
                                 reply_to_message_id: str | None = None,
                                 metadata: dict | None = None) -> int | None:
        """保存一条 Discord 会话消息。返回新行 id；重复 message id 会被忽略并返回 None。"""
        if role not in {"user", "assistant", "system"}:
            raise ValueError(f"invalid conversation role: {role}")
        if not channel_id:
            raise ValueError("channel_id is required")
        text = content or ""
        metadata_json = json.dumps(metadata, ensure_ascii=False) if metadata else None
        conn = self._get_conn()
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO conversation_messages (
                discord_message_id, channel_id, guild_id, author_id, author_name,
                role, content, created_at, reply_to_message_id, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(discord_message_id) if discord_message_id is not None else None,
                str(channel_id),
                str(guild_id) if guild_id is not None else None,
                str(author_id) if author_id is not None else None,
                author_name,
                role,
                text,
                created_at,
                str(reply_to_message_id) if reply_to_message_id is not None else None,
                metadata_json,
            )
        )
        conn.commit()
        row_id = cursor.lastrowid if cursor.rowcount > 0 else None
        conn.close()
        return row_id

    def get_recent_conversation_messages(self, channel_id: str, limit: int = 20) -> list[dict]:
        """按频道获取最近的 Discord 会话消息，返回时间正序。"""
        conn = self._get_conn()
        rows = conn.execute(
            """
            SELECT *
            FROM conversation_messages
            WHERE channel_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (str(channel_id), limit)
        ).fetchall()
        conn.close()
        messages = []
        for row in reversed(rows):
            item = dict(row)
            if item.get("metadata_json"):
                try:
                    item["metadata"] = json.loads(item["metadata_json"])
                except json.JSONDecodeError:
                    item["metadata"] = None
            else:
                item["metadata"] = None
            messages.append(item)
        return messages

    @staticmethod
    def _message_timestamp(item: dict) -> str:
        try:
            return datetime.fromisoformat(item["created_at"]).astimezone().strftime("%Y-%m-%d %H:%M")
        except (ValueError, TypeError):
            return item.get("created_at", "")[:16]

    @staticmethod
    def _to_ai_message(item: dict) -> dict | None:
        """把一行 conversation_messages 转成 AI 引擎的 role/content 消息；不适用返回 None。

        两侧都打 [时间] 前缀。assistant 侧同样要打，是因为 _ensure_valid_messages
        会把连续同角色消息拼成一条：用户长时间不回复时，几天内的主动消息会塌缩成
        一整块无日期文本，模型无从分辨哪句是刚说的、哪句是三天前说的，于是反复
        重复同样的问候。前缀是这块文本里唯一的时间锚点。
        """
        role = item.get("role")
        if role not in {"user", "assistant"}:
            return None
        metadata = item.get("metadata") or {}
        if role == "user":
            content = metadata.get("current_content")
            if not content:
                content = f"[{Database._message_timestamp(item)}] {item.get('content') or ''}".strip()
        else:
            raw = item.get("content") or ""
            content = f"[{Database._message_timestamp(item)}] {raw}".strip() if raw else raw
        if not content:
            return None
        return {"role": role, "content": content}

    def get_recent_ai_messages(self, channel_id: str, limit: int = 20) -> list[dict]:
        """按频道获取最近会话，转换为 AI 引擎需要的 role/content 消息。"""
        messages = []
        for item in self.get_recent_conversation_messages(channel_id, limit=limit):
            msg = self._to_ai_message(item)
            if msg:
                messages.append(msg)
        return messages

    def get_ai_messages_after(self, channel_id: str, after_id: int,
                              limit: int | None = None,
                              upto_id: int | None = None) -> list[dict]:
        """取连续的 ``(after_id, upto_id]`` 会话消息，按 id 正序返回。

        返回元素带 id（compact 游标/冻结切片用），content 格式与
        get_recent_ai_messages 一致。缺省不截断；显式 limit 时从最老的
        未处理消息开始取，确保调用方永远拿到连续前缀，不能越过未读消息
        推进 compact cursor。
        """
        conn = self._get_conn()
        where = "channel_id = ? AND id > ?"
        params: list = [str(channel_id), int(after_id)]
        if upto_id is not None:
            where += " AND id <= ?"
            params.append(int(upto_id))
        sql = f"SELECT * FROM conversation_messages WHERE {where} ORDER BY id ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(int(limit), 0))
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        messages = []
        for row in rows:
            item = dict(row)
            if item.get("metadata_json"):
                try:
                    item["metadata"] = json.loads(item["metadata_json"])
                except json.JSONDecodeError:
                    item["metadata"] = None
            else:
                item["metadata"] = None
            msg = self._to_ai_message(item)
            if msg:
                msg["id"] = item["id"]
                msg["created_at"] = item["created_at"]
                messages.append(msg)
        return messages

    def get_conversation_messages_after(
        self, channel_id: str, after_id: int, *, limit: int | None = None,
        upto_id: int | None = None,
    ) -> list[dict]:
        """Return a continuous raw-message interval for curator evidence."""
        where = "channel_id = ? AND id > ?"
        params: list = [str(channel_id), int(after_id)]
        if upto_id is not None:
            where += " AND id <= ?"
            params.append(int(upto_id))
        sql = (
            "SELECT id, role, content, created_at "
            f"FROM conversation_messages WHERE {where} ORDER BY id ASC"
        )
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(int(limit), 0))
        conn = self._get_conn()
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return [dict(row) for row in rows]

    # ============ 对话日志 embedding 检索（memory v3 Part B2）============

    def get_conversation_messages_upto(self, channel_id: str, upto_id: int,
                                       limit: int = 5) -> list[dict]:
        """取该 channel 中 id <= upto_id 的最近 limit 条，时间正序。
        embed_and_store 拼接上下文用（最后一条即 upto_id 本身）。"""
        conn = self._get_conn()
        rows = conn.execute(
            """
            SELECT id, role, author_name, content, created_at
            FROM conversation_messages
            WHERE channel_id = ? AND id <= ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (str(channel_id), upto_id, limit)
        ).fetchall()
        conn.close()
        return [dict(row) for row in reversed(rows)]

    def update_conversation_embedding(self, row_id: int, embedding: list[float],
                                      context: str, model: str) -> None:
        """后台 embedding 任务算完后写回该行。"""
        conn = self._get_conn()
        conn.execute(
            """
            UPDATE conversation_messages
            SET embedding = ?, embedding_context = ?, embedding_model = ?
            WHERE id = ?
            """,
            (json.dumps(embedding), context, model, row_id)
        )
        conn.commit()
        conn.close()

    def get_conversation_ids_needing_embedding(
            self, channel_id: str, *, upto_id: int, model: str,
            after_id: int = 0, limit: int = 50) -> list[int]:
        """Return a stable page of compacted rows missing the requested embedding."""
        conn = self._get_conn()
        rows = conn.execute(
            """
            SELECT id
            FROM conversation_messages
            WHERE channel_id = ? AND id > ? AND id <= ?
              AND (embedding IS NULL OR embedding_model IS NULL OR embedding_model != ?)
            ORDER BY id ASC
            LIMIT ?
            """,
            (str(channel_id), int(after_id), int(upto_id), model, max(int(limit), 1)),
        ).fetchall()
        conn.close()
        return [int(row["id"]) for row in rows]

    def clear_conversation_embeddings_after(self, channel_id: str,
                                            upto_id: int) -> int:
        """Remove legacy embeddings from the current plaintext tail."""
        conn = self._get_conn()
        cursor = conn.execute(
            """
            UPDATE conversation_messages
            SET embedding = NULL, embedding_context = NULL, embedding_model = NULL
            WHERE channel_id = ? AND id > ?
              AND (embedding IS NOT NULL OR embedding_context IS NOT NULL
                   OR embedding_model IS NOT NULL)
            """,
            (str(channel_id), int(upto_id)),
        )
        conn.commit()
        cleared = cursor.rowcount
        conn.close()
        return cleared

    def get_relevant_conversation_snippets(self, query_embedding: list[float],
                                           channel_id: str, *, model: str,
                                           limit: int = 5,
                                           exclude_recent: int = 20,
                                           min_relevance: float = 0.55) -> list[dict]:
        """
        语义检索历史对话片段，按 relevance(cosine) + 0.1 * recency(0.995^小时) 打分取 top。

        - 排除该 channel 最近 exclude_recent 条：它们已经在 AI 的工作窗口里，
          再注入就是重复内容（调用方应传入与 get_recent_ai_messages 一致的窗口大小）
        - 只比对同一 embedding_model 的行：换 embedding 模型后旧向量维度/空间不兼容，
          直接当没有 embedding 处理，等后台任务用新模型逐渐补齐
        - min_relevance 与 embedding 模型强绑定，跟着 config 的 ai.embedding.min_relevance
          走（调用方显式传入），换模型必须跑 scripts/calibrate_embedding_threshold.py 重校准。
          已校准值：智谱 embedding-3 → 0.55（无关地板 0.45~0.52，相关 0.65+）；
          Qwen/Qwen3-VL-Embedding-8B → 0.50（2026-07-08 校准：无关地板 0.37~0.48，
          相关命中 0.55~0.82）。recency 只配 0.1——它只该在相关度接近时偏向最近的，
          实测 0.25 会让"最近但一般相关"压过"三周前但高度相关"
        - 同段对话去重：id 相差 <= CONTEXT_MESSAGES 的命中行是同一段对话
          （embedding_context 互相重叠），只保留一条；保留 id 最大的——
          context 是向前拼接的，id 大的行覆盖整段内容——分数沿用簇内最高
        - 全表暴力扫描 O(N)，当前不到一千条毫无压力；几万条以上再考虑
          限制扫描窗口或 sqlite-vec（见 docs/memory v3.md §3）
        """
        conn = self._get_conn()
        window_row = conn.execute(
            """
            SELECT MIN(id) AS min_id FROM (
                SELECT id FROM conversation_messages
                WHERE channel_id = ?
                ORDER BY id DESC LIMIT ?
            )
            """,
            (str(channel_id), exclude_recent)
        ).fetchone()
        window_start = window_row["min_id"] if window_row else None
        if window_start is None:
            conn.close()
            return []
        rows = conn.execute(
            """
            SELECT id, role, content, created_at, embedding, embedding_context
            FROM conversation_messages
            WHERE channel_id = ? AND embedding IS NOT NULL
              AND embedding_model = ? AND id < ?
            """,
            (str(channel_id), model, window_start)
        ).fetchall()
        conn.close()

        scored = []
        for row in rows:
            try:
                emb = json.loads(row["embedding"])
            except (json.JSONDecodeError, TypeError):
                continue
            relevance = cosine_similarity(query_embedding, emb)
            if relevance < min_relevance:
                continue
            score = relevance + 0.1 * recency_weight(row["created_at"])
            scored.append((score, relevance, row))
        scored.sort(key=lambda t: t[0], reverse=True)

        picked: list[dict] = []
        for score, relevance, row in scored:
            cluster = next(
                (p for p in picked if abs(row["id"] - p["id"]) <= CONTEXT_MESSAGES),
                None,
            )
            if cluster is not None:
                # 同段对话已有代表：id 更大的行 context 覆盖更全，换内容、保留高分
                if row["id"] > cluster["id"]:
                    cluster.update(
                        id=row["id"], role=row["role"], content=row["content"],
                        created_at=row["created_at"],
                        embedding_context=row["embedding_context"],
                    )
                continue
            if len(picked) >= limit:
                continue  # 已满仍继续遍历，让后续行有机会合并进已选簇
            picked.append({
                "id": row["id"],
                "role": row["role"],
                "content": row["content"],
                "created_at": row["created_at"],
                "embedding_context": row["embedding_context"],
                "score": round(score, 4),
                "relevance": round(relevance, 4),
            })
        return picked

    # ============ 提醒队列 ============

    REMINDER_DEDUPE_SECONDS = 5

    @staticmethod
    def normalize_local_time(value: str) -> datetime:
        """把 ISO 时间统一成进程本地时区的 naive datetime。

        AI 有时会传 `...Z` / `...+10:00`。本项目 scheduler 使用
        datetime.now() 的本地 naive 时间域，因此入库和比较前必须归一。
        """
        raw = (value or "").strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt

    def add_reminder(self, trigger_time: str, action: str, group_id: str = None,
                     priority: str = "normal") -> int:
        """添加一个提醒"""
        trigger_dt = self.normalize_local_time(trigger_time)
        now = datetime.now()
        if trigger_dt < now:
            raise ValueError(
                f"trigger_time is in the past after timezone normalization: "
                f"{trigger_time} -> {trigger_dt.isoformat(timespec='seconds')} "
                f"(now={now.isoformat(timespec='seconds')})"
            )
        normalized_trigger_time = trigger_dt.isoformat(timespec="seconds")

        existing_id = self.find_duplicate_pending_reminder(
            trigger_dt=trigger_dt,
            action=action,
            group_id=group_id,
        )
        if existing_id is not None:
            return existing_id

        conn = self._get_conn()
        cursor = conn.execute(
            "INSERT INTO reminders (trigger_time, action, group_id, priority) VALUES (?, ?, ?, ?)",
            (normalized_trigger_time, action, group_id, priority)
        )
        conn.commit()
        reminder_id = cursor.lastrowid
        conn.close()
        # 通知 scheduler 重新计算倒计时
        if self._on_reminder_added:
            self._on_reminder_added()
        return reminder_id

    def find_duplicate_pending_reminder(
        self,
        *,
        trigger_dt: datetime,
        action: str,
        group_id: str = None,
    ) -> Optional[int]:
        """查找等价 pending reminder，防止 AI/重试重复插入。"""
        normalized_action = (action or "").strip()
        normalized_group = (group_id or "").strip()
        conn = self._get_conn()
        if normalized_group:
            rows = conn.execute(
                "SELECT * FROM reminders WHERE status = 'pending' AND action = ? AND group_id = ?",
                (normalized_action, normalized_group),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM reminders WHERE status = 'pending' AND action = ? "
                "AND (group_id IS NULL OR group_id = '')",
                (normalized_action,),
            ).fetchall()
        conn.close()

        for row in rows:
            item = dict(row)
            try:
                existing_dt = self.normalize_local_time(item["trigger_time"])
            except (TypeError, ValueError):
                continue
            if abs((existing_dt - trigger_dt).total_seconds()) <= self.REMINDER_DEDUPE_SECONDS:
                return item["id"]
        return None

    def get_pending_reminders(self) -> list[dict]:
        """获取所有未完成且已到时间的提醒"""
        now = datetime.now()
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM reminders WHERE status = 'pending' ORDER BY trigger_time ASC"
        ).fetchall()
        conn.close()
        out = []
        for row in rows:
            item = dict(row)
            try:
                if self.normalize_local_time(item["trigger_time"]) <= now:
                    out.append(item)
            except (TypeError, ValueError):
                continue
        return out

    def get_pending_reminders_until(self, cutoff_time: str) -> list[dict]:
        """获取 cutoff_time 前所有待触发提醒，用于 scheduler 合并临近提醒。"""
        cutoff_dt = self.normalize_local_time(cutoff_time)
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM reminders WHERE status = 'pending'"
        ).fetchall()
        conn.close()
        out = []
        for row in rows:
            item = dict(row)
            try:
                trigger_dt = self.normalize_local_time(item["trigger_time"])
            except (TypeError, ValueError):
                continue
            if trigger_dt <= cutoff_dt:
                item["_trigger_dt"] = trigger_dt
                out.append(item)
        priority_rank = {"high": 0, "normal": 1, "low": 2}
        out.sort(key=lambda r: (
            r["_trigger_dt"],
            priority_rank.get(r.get("priority") or "normal", 1),
            r["id"],
        ))
        for item in out:
            item.pop("_trigger_dt", None)
        return out

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
        rows = conn.execute(
            "SELECT * FROM reminders WHERE status = 'pending'"
        ).fetchall()
        conn.close()
        reminders = []
        for row in rows:
            item = dict(row)
            try:
                reminders.append((self.normalize_local_time(item["trigger_time"]), item["trigger_time"]))
            except (TypeError, ValueError):
                continue
        if not reminders:
            return None
        return min(reminders, key=lambda r: r[0])[1]

    # ============ AI 可追溯性（ai_runs / tool_calls） ============

    def save_ai_run(self, *, run_id: str, trigger: str, model: str | None,
                     provider: str | None, started_at: str, finished_at: str,
                     status: str, error: str | None, final_text: str | None,
                     tool_calls: list[dict]) -> None:
        """写入一次 AI run 及其工具调用记录（bot/trace.py finalize() 调用）。"""
        conn = self._get_conn()
        conn.execute(
            """INSERT OR REPLACE INTO ai_runs
               (id, trigger, model, provider, started_at, finished_at, status, error, final_text)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (run_id, trigger, model, provider, started_at, finished_at, status, error, final_text),
        )
        for tc in tool_calls:
            conn.execute(
                """INSERT INTO tool_calls
                   (run_id, round_n, tool_name, arguments_json, result_json, success)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (run_id, tc.get("round_n"), tc["tool_name"], tc.get("arguments_json"),
                 tc.get("result_json"), tc.get("success")),
            )
        conn.commit()
        conn.close()

    # ============ 记忆系统 ============

    def get_all_memories(self, include_expired: bool = False) -> list[dict]:
        """获取记忆，按时间倒序。

        这张表现在只用来存长期不变的事实（偏好/身份信息），不再有数量上限。
        默认只返回未过期的（valid_until 为 NULL = 永久，或还没到期）供 prompt 使用；
        include_expired=True 给管理界面看全部，方便手动整理。
        """
        conn = self._get_conn()
        if include_expired:
            rows = conn.execute("SELECT * FROM memories ORDER BY created_at DESC").fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM memories
                   -- valid_until is normalized on write to SQLite UTC datetime text.
                   -- Use datetime() anyway so ISO inputs from older rows do not fall
                   -- back to fragile lexicographic string comparison.
                   WHERE valid_until IS NULL OR datetime(valid_until) > datetime('now')
                   ORDER BY created_at DESC"""
            ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def add_memory(self, content: str, source: str = 'ai',
                    memory_type: str | None = None,
                    valid_until: str | None = None) -> int:
        """添加一条记忆。memory_type/valid_until 可选；valid_until 为 None 表示永久有效。"""
        normalized_valid_until = _normalize_memory_valid_until(valid_until)
        conn = self._get_conn()
        cursor = conn.execute(
            "INSERT INTO memories (content, source, memory_type, valid_until) VALUES (?, ?, ?, ?)",
            (content, source, memory_type, normalized_valid_until)
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

    def update_memory(self, memory_id: int, **fields) -> None:
        """更新记忆的部分字段（content / memory_type / valid_until）。

        只更新 fields 里实际传入的键；某个键传 None 表示显式清空该字段
        （比如把 valid_until 设回永久）。
        """
        allowed = {"content", "memory_type", "valid_until"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        if "valid_until" in updates:
            updates["valid_until"] = _normalize_memory_valid_until(updates["valid_until"])
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        conn = self._get_conn()
        conn.execute(
            f"UPDATE memories SET {set_clause} WHERE id = ?",
            (*updates.values(), memory_id),
        )
        conn.commit()
        conn.close()

    def sync_memories_shadow(self, memories: list[dict]) -> None:
        """Mirror Markdown durable memories into the legacy migration table.

        The application no longer reads this table. It remains an exact shadow
        until production validation and offsite Markdown backup are complete.
        """
        conn = self._get_conn()
        conn.execute("DELETE FROM memories")
        conn.executemany(
            """INSERT INTO memories
               (id, content, created_at, source, memory_type, valid_until)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [
                (
                    item["id"],
                    item["content"],
                    item.get("created_at"),
                    item.get("source") or "ai",
                    item.get("memory_type"),
                    item.get("valid_until"),
                )
                for item in memories
            ],
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
