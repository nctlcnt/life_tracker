"""SQLite persistence for the LT-175 tool batch table.

这一层只负责把批次写进 `tool_batches` 并读回来。什么时候该建批、
消息区间的两个端点怎么算，依赖游标，属于 LT-176。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from bot.database import Database


# 区间唯一约束和部分唯一索引都按 worker_name 分组判断，取值不统一会让
# 两条保证一起失效，所以所有写入方共用这一个常量。
TOOL_WORKER = "tool_worker"

OPEN_STATUSES = frozenset({"pending", "running", "retry_wait"})
TERMINAL_STATUSES = frozenset({"completed", "failed"})
EXECUTION_MODES = frozenset({"shadow", "apply"})


class BatchSourceConflict(ValueError):
    """同一个 source_ref 被用在了另一批内容不同的活上。"""


def _decode(row) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    for raw_key, key in (("input_json", "input"), ("result_json", "result")):
        raw = item.get(raw_key)
        if raw:
            try:
                item[key] = json.loads(raw)
            except json.JSONDecodeError:
                item[key] = None
        else:
            item[key] = None
    return item


class ToolBatchRepository:
    """`tool_batches` 的建批与读取。领取与终态属于后面几步。"""

    def __init__(self, db: Database, *, worker_name: str = TOOL_WORKER):
        self.db = db
        self.worker_name = worker_name

    # --- 读 -----------------------------------------------------------------

    def get(self, batch_id: str) -> dict[str, Any] | None:
        conn = self.db._get_conn()
        try:
            return _decode(conn.execute(
                "SELECT * FROM tool_batches WHERE id = ?", (batch_id,)
            ).fetchone())
        finally:
            conn.close()

    def open_conversation_batch(self, channel_id: str) -> dict[str, Any] | None:
        """这个频道当前那一批还没干完的聊天批次，没有则返回 None。

        部分唯一索引保证它最多只有一批。LT-176 的游标要靠它判断
        上一批是否已经结束。
        """
        conn = self.db._get_conn()
        try:
            return _decode(conn.execute(
                f"""
                SELECT * FROM tool_batches
                WHERE worker_name = ? AND channel_id = ?
                  AND source_kind = 'conversation'
                  AND status IN ({', '.join('?' for _ in OPEN_STATUSES)})
                """,
                (self.worker_name, str(channel_id), *sorted(OPEN_STATUSES)),
            ).fetchone())
        finally:
            conn.close()

    # --- 建批 ---------------------------------------------------------------

    def create_conversation_batch(
        self, *, channel_id: str, after_message_id: int,
        through_message_id: int, execution_mode: str,
        last_user_message_id: int | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """为一段消息区间建一批，返回 (批次, 是否新建)。

        两种情况会返回既有的批次而不是新建：这段区间已经建过批（不论
        它是否已经结束），或者这个频道还有另一批没干完。后者是常态而
        不是异常——部分唯一索引本来就只允许同时开着一批，新来的消息
        应该等下一批，所以这里不抛异常，由调用方看 `created` 决定。
        """
        self._check_common(channel_id, execution_mode)
        after = int(after_message_id)
        through = int(through_message_id)
        if through < after:
            raise ValueError("through_message_id 不能小于 after_message_id")
        if last_user_message_id is not None:
            last_user = int(last_user_message_id)
            if not after < last_user <= through:
                raise ValueError(
                    "last_user_message_id 必须落在 (after, through] 区间内")
        else:
            last_user = None

        conn = self.db._get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                """
                SELECT * FROM tool_batches
                WHERE worker_name = ? AND channel_id = ?
                  AND after_message_id = ? AND through_message_id = ?
                """,
                (self.worker_name, str(channel_id), after, through),
            ).fetchone()
            if existing is None:
                existing = conn.execute(
                    f"""
                    SELECT * FROM tool_batches
                    WHERE worker_name = ? AND channel_id = ?
                      AND source_kind = 'conversation'
                      AND status IN ({', '.join('?' for _ in OPEN_STATUSES)})
                    """,
                    (self.worker_name, str(channel_id), *sorted(OPEN_STATUSES)),
                ).fetchone()
            if existing is not None:
                conn.commit()
                return _decode(existing), False

            batch_id = uuid.uuid4().hex
            conn.execute(
                """
                INSERT INTO tool_batches (
                    id, worker_name, channel_id, source_kind,
                    after_message_id, through_message_id, last_user_message_id,
                    execution_mode
                ) VALUES (?, ?, ?, 'conversation', ?, ?, ?, ?)
                """,
                (batch_id, self.worker_name, str(channel_id),
                 after, through, last_user, execution_mode),
            )
            created = conn.execute(
                "SELECT * FROM tool_batches WHERE id = ?", (batch_id,)
            ).fetchone()
            conn.commit()
            return _decode(created), True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def create_check_in_batch(
        self, *, channel_id: str, source_ref: str, payload: dict[str, Any],
        execution_mode: str,
    ) -> tuple[dict[str, Any], bool]:
        """为一次 check-in 触发建一批，返回 (批次, 是否新建)。

        幂等靠 `UNIQUE(source_kind, source_ref)`：`source_ref` 里带着
        这次触发的时间，所以调度器重启之后重复建批只会拿回同一批。
        """
        self._check_common(channel_id, execution_mode)
        if not str(source_ref).strip():
            raise ValueError("source_ref is required")
        if not isinstance(payload, dict) or not payload:
            raise ValueError("payload 必须是非空 dict")
        input_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)

        conn = self.db._get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO tool_batches (
                    id, worker_name, channel_id, source_kind,
                    source_ref, input_json, execution_mode
                ) VALUES (?, ?, ?, 'check_in', ?, ?, ?)
                """,
                (uuid.uuid4().hex, self.worker_name, str(channel_id),
                 str(source_ref), input_json, execution_mode),
            )
            created = cursor.rowcount == 1
            row = conn.execute(
                "SELECT * FROM tool_batches "
                "WHERE source_kind = 'check_in' AND source_ref = ?",
                (str(source_ref),),
            ).fetchone()
            if row is None:
                raise RuntimeError("tool batch disappeared after insert")
            item = _decode(row)
            if not created:
                expected = {
                    "worker_name": self.worker_name,
                    "channel_id": str(channel_id),
                    "input_json": input_json,
                }
                if any(item[key] != value for key, value in expected.items()):
                    raise BatchSourceConflict(
                        f"source_ref {source_ref!r} belongs to another batch")
            conn.commit()
            return item, created
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # --- 共用校验 -----------------------------------------------------------

    @staticmethod
    def _check_common(channel_id: str, execution_mode: str) -> None:
        if not str(channel_id).strip():
            raise ValueError("channel_id is required")
        if execution_mode not in EXECUTION_MODES:
            raise ValueError(
                f"execution_mode must be one of {sorted(EXECUTION_MODES)}")
