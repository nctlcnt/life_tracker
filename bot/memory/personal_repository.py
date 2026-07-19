"""SQLite repository for curator-owned long-term memories (LT-136)."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot.memory.curator import CuratorBatch


MEMORY_STATUSES = {"active", "superseded", "archived"}
# 单一事实来源：parser、DB CHECK、此处三方必须一致（LT-136 曾因三份拷贝失配炸过 apply）
from bot.memory.curator import CURATOR_EVIDENCE_ROLES as EVIDENCE_ROLES


@dataclass(frozen=True)
class MemorySource:
    conversation_message_id: int
    quote: str | None = None
    evidence_role: str = "supports"


class PersonalMemoryRepository:
    """Owns v4 memory rows, evidence links, and curator cursors."""

    def __init__(self, database):
        self.database = database

    @staticmethod
    def _validate_text(value: str | None, field: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError(f"{field} is required")
        return text

    @staticmethod
    def _decode_memory(row, sources: list[dict]) -> dict:
        item = dict(row)
        raw_embedding = item.get("embedding")
        if raw_embedding:
            try:
                item["embedding"] = json.loads(raw_embedding)
            except (json.JSONDecodeError, TypeError):
                item["embedding"] = None
        item["sources"] = sources
        item["source_message_ids"] = [
            source["conversation_message_id"] for source in sources
        ]
        return item

    def _normalize_sources(self, sources: list[MemorySource | dict]) -> list[MemorySource]:
        normalized = []
        seen = set()
        for raw in sources:
            source = raw if isinstance(raw, MemorySource) else MemorySource(**raw)
            role = str(source.evidence_role or "supports").strip()
            if role not in EVIDENCE_ROLES:
                raise ValueError(f"invalid evidence_role: {role}")
            message_id = int(source.conversation_message_id)
            key = (message_id, role)
            if key in seen:
                continue
            seen.add(key)
            normalized.append(MemorySource(
                conversation_message_id=message_id,
                quote=(source.quote or "").strip() or None,
                evidence_role=role,
            ))
        if not normalized:
            raise ValueError("at least one source is required")
        return normalized

    @staticmethod
    def _validate_sources_exist(conn, channel_id: str,
                                sources: list[MemorySource]) -> None:
        ids = [source.conversation_message_id for source in sources]
        placeholders = ",".join("?" for _ in ids)
        rows = conn.execute(
            f"SELECT id FROM conversation_messages "
            f"WHERE channel_id = ? AND id IN ({placeholders})",
            (str(channel_id), *ids),
        ).fetchall()
        found = {int(row["id"]) for row in rows}
        missing = sorted(set(ids) - found)
        if missing:
            raise ValueError(f"unknown source_message_ids for channel: {missing}")

    def create(self, *, channel_id: str, summary: str, reason: str,
               memory_type: str, curator_model: str,
               sources: list[MemorySource | dict], quote: str | None = None,
               embedding: list[float] | None = None,
               embedding_model: str | None = None) -> int:
        summary = self._validate_text(summary, "summary")
        reason = self._validate_text(reason, "reason")
        memory_type = self._validate_text(memory_type, "memory_type")
        curator_model = self._validate_text(curator_model, "curator_model")
        normalized_sources = self._normalize_sources(sources)
        conn = self.database._get_conn()
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._validate_sources_exist(conn, channel_id, normalized_sources)
            cursor = conn.execute(
                """
                INSERT INTO personal_memories
                    (summary, quote, reason, memory_type, curator_model,
                     embedding, embedding_model)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (summary, (quote or "").strip() or None, reason, memory_type,
                 curator_model,
                 json.dumps(embedding) if embedding is not None else None,
                 embedding_model),
            )
            memory_id = int(cursor.lastrowid)
            conn.executemany(
                """
                INSERT INTO personal_memory_sources
                    (memory_id, conversation_message_id, quote, evidence_role)
                VALUES (?, ?, ?, ?)
                """,
                [(memory_id, source.conversation_message_id, source.quote,
                  source.evidence_role) for source in normalized_sources],
            )
            conn.commit()
            return memory_id
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get(self, memory_id: int) -> dict | None:
        conn = self.database._get_conn()
        row = conn.execute(
            "SELECT * FROM personal_memories WHERE id = ?", (int(memory_id),)
        ).fetchone()
        if row is None:
            conn.close()
            return None
        sources = [dict(source) for source in conn.execute(
            """
            SELECT conversation_message_id, quote, evidence_role, created_at
            FROM personal_memory_sources
            WHERE memory_id = ?
            ORDER BY conversation_message_id, evidence_role
            """,
            (int(memory_id),),
        ).fetchall()]
        conn.close()
        return self._decode_memory(row, sources)

    def list(self, *, status: str | None = None,
             memory_type: str | None = None) -> list[dict]:
        where = []
        params = []
        if status is not None:
            if status not in MEMORY_STATUSES:
                raise ValueError(f"invalid status: {status}")
            where.append("status = ?")
            params.append(status)
        if memory_type is not None:
            where.append("memory_type = ?")
            params.append(memory_type)
        sql = "SELECT id FROM personal_memories"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC, id DESC"
        conn = self.database._get_conn()
        ids = [int(row["id"]) for row in conn.execute(sql, params).fetchall()]
        conn.close()
        return [item for memory_id in ids if (item := self.get(memory_id)) is not None]

    def update_content(self, memory_id: int, *, summary: str | None = None,
                       reason: str | None = None, memory_type: str | None = None,
                       quote: str | None = None) -> bool:
        updates = {}
        if summary is not None:
            updates["summary"] = self._validate_text(summary, "summary")
            # The vector represents summary text; manual/curator edits must not
            # leave a stale embedding searchable under the new content.
            updates["embedding"] = None
            updates["embedding_model"] = None
        if reason is not None:
            updates["reason"] = self._validate_text(reason, "reason")
        if memory_type is not None:
            updates["memory_type"] = self._validate_text(memory_type, "memory_type")
        if quote is not None:
            updates["quote"] = str(quote).strip() or None
        if not updates:
            return False
        updates["updated_at"] = None
        assignments = [
            f"{field} = datetime('now')" if field == "updated_at" else f"{field} = ?"
            for field in updates
        ]
        values = [value for field, value in updates.items() if field != "updated_at"]
        conn = self.database._get_conn()
        cursor = conn.execute(
            f"UPDATE personal_memories SET {', '.join(assignments)} WHERE id = ?",
            (*values, int(memory_id)),
        )
        conn.commit()
        changed = cursor.rowcount > 0
        conn.close()
        return changed

    def set_status(self, memory_id: int, status: str,
                   *, superseded_by: int | None = None) -> bool:
        if status not in MEMORY_STATUSES:
            raise ValueError(f"invalid status: {status}")
        memory_id = int(memory_id)
        if status == "superseded":
            if superseded_by is None or int(superseded_by) == memory_id:
                raise ValueError("superseded status requires another memory id")
        elif superseded_by is not None:
            raise ValueError("superseded_by is only valid for superseded status")
        conn = self.database._get_conn()
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            cursor = conn.execute(
                """
                UPDATE personal_memories
                SET status = ?, superseded_by = ?, updated_at = datetime('now')
                WHERE id = ?
                """,
                (status, int(superseded_by) if superseded_by is not None else None,
                 memory_id),
            )
            conn.commit()
            return cursor.rowcount > 0
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def count_new_messages(self, channel_id: str, after_id: int) -> int:
        """cursor 之后的新消息数（curator 调度触发条件用）。"""
        conn = self.database._get_conn()
        row = conn.execute(
            "SELECT COUNT(*) FROM conversation_messages "
            "WHERE channel_id = ? AND id > ?",
            (str(channel_id), int(after_id)),
        ).fetchone()
        conn.close()
        return int(row[0])

    def get_cursor(self, curator_name: str, channel_id: str) -> dict:
        conn = self.database._get_conn()
        row = conn.execute(
            """
            SELECT * FROM curator_cursors
            WHERE curator_name = ? AND channel_id = ?
            """,
            (curator_name, str(channel_id)),
        ).fetchone()
        conn.close()
        return dict(row) if row else {
            "curator_name": curator_name,
            "channel_id": str(channel_id),
            "last_message_id": 0,
            "last_successful_run_id": None,
            "updated_at": None,
        }

    def advance_cursor(self, curator_name: str, channel_id: str,
                       last_message_id: int,
                       *, run_id: str | None = None) -> bool:
        current = self.get_cursor(curator_name, channel_id)
        if int(last_message_id) < int(current["last_message_id"]):
            return False
        conn = self.database._get_conn()
        conn.execute(
            """
            INSERT INTO curator_cursors
                (curator_name, channel_id, last_message_id,
                 last_successful_run_id, updated_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            ON CONFLICT(curator_name, channel_id) DO UPDATE SET
                last_message_id = excluded.last_message_id,
                last_successful_run_id = excluded.last_successful_run_id,
                updated_at = datetime('now')
            WHERE excluded.last_message_id >= curator_cursors.last_message_id
            """,
            (curator_name, str(channel_id), int(last_message_id), run_id),
        )
        changed = conn.total_changes > 0
        conn.commit()
        conn.close()
        return changed

    @staticmethod
    def _insert_sources(conn, memory_id: int,
                        sources: list[MemorySource]) -> None:
        conn.executemany(
            """
            INSERT OR IGNORE INTO personal_memory_sources
                (memory_id, conversation_message_id, quote, evidence_role)
            VALUES (?, ?, ?, ?)
            """,
            [(int(memory_id), source.conversation_message_id, source.quote,
              source.evidence_role) for source in sources],
        )

    @staticmethod
    def _insert_memory(conn, *, summary: str, quote: str | None,
                       reason: str, memory_type: str,
                       curator_model: str) -> int:
        cursor = conn.execute(
            """
            INSERT INTO personal_memories
                (summary, quote, reason, memory_type, curator_model)
            VALUES (?, ?, ?, ?, ?)
            """,
            (summary, quote, reason, memory_type, curator_model),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _require_active_memory(conn, memory_id: int) -> dict:
        row = conn.execute(
            "SELECT * FROM personal_memories WHERE id = ?",
            (int(memory_id),),
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown memory_id: {memory_id}")
        item = dict(row)
        if item["status"] != "active":
            raise ValueError(
                f"memory_id {memory_id} is not active: {item['status']}")
        return item

    @staticmethod
    def _validate_curator_run(conn, run_id: str, batch: "CuratorBatch",
                              curator_model: str) -> None:
        row = conn.execute(
            "SELECT trigger, status, model, final_text FROM ai_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown curator run_id: {run_id}")
        if row["trigger"] != "curator" or row["status"] != "success":
            raise ValueError(
                f"run_id {run_id} is not a successful curator run")
        from bot.memory.curator import parse_curator_batch
        try:
            traced_batch = parse_curator_batch(row["final_text"] or "")
        except ValueError as exc:
            raise ValueError(
                f"run_id {run_id} has invalid curator output") from exc
        if traced_batch != batch:
            raise ValueError(
                f"curator batch does not match run_id {run_id} output")
        if row["model"] != curator_model:
            raise ValueError(
                f"curator model does not match run_id {run_id}")

    @staticmethod
    def _validate_batch_evidence(
        conn, *, channel_id: str, after_message_id: int,
        upto_message_id: int, visible_message_ids: set[int], operations,
    ) -> dict[int, str]:
        interval_rows = conn.execute(
            """
            SELECT id FROM conversation_messages
            WHERE channel_id = ? AND id > ? AND id <= ?
            ORDER BY id
            """,
            (str(channel_id), int(after_message_id), int(upto_message_id)),
        ).fetchall()
        interval_ids = {int(row["id"]) for row in interval_rows}
        if interval_ids != visible_message_ids:
            missing = sorted(interval_ids - visible_message_ids)
            extra = sorted(visible_message_ids - interval_ids)
            raise ValueError(
                f"visible interval mismatch: missing={missing}, extra={extra}")
        evidence_ids = {
            int(source.message_id)
            for operation in operations
            for source in operation.sources
        }
        if not evidence_ids:
            return {}
        placeholders = ",".join("?" for _ in evidence_ids)
        rows = conn.execute(
            f"""
            SELECT id, content
            FROM conversation_messages
            WHERE channel_id = ? AND id IN ({placeholders})
              AND id > ? AND id <= ?
            """,
            (str(channel_id), *sorted(evidence_ids),
             int(after_message_id), int(upto_message_id)),
        ).fetchall()
        content_by_id = {int(row["id"]): row["content"] for row in rows}
        invalid = sorted(evidence_ids - set(content_by_id))
        invisible = sorted(evidence_ids - visible_message_ids)
        if invalid or invisible:
            bad = sorted(set(invalid) | set(invisible))
            raise ValueError(f"evidence outside visible interval: {bad}")
        for operation in operations:
            for source in operation.sources:
                if source.quote and source.quote not in content_by_id[source.message_id]:
                    raise ValueError(
                        f"evidence quote does not match message_id {source.message_id}")
        return content_by_id

    def validate_curator_batch(
        self, batch: "CuratorBatch", *, curator_name: str,
        channel_id: str, after_message_id: int, upto_message_id: int,
        visible_message_ids: set[int],
    ) -> None:
        """Validate a proposal against the frozen DB interval without writing."""
        conn = self.database._get_conn()
        try:
            cursor_row = conn.execute(
                """
                SELECT last_message_id FROM curator_cursors
                WHERE curator_name = ? AND channel_id = ?
                """,
                (curator_name, str(channel_id)),
            ).fetchone()
            current = int(cursor_row["last_message_id"]) if cursor_row else 0
            if current != int(after_message_id):
                raise ValueError(
                    f"curator cursor changed: expected {after_message_id}, current {current}")
            self._validate_batch_evidence(
                conn,
                channel_id=str(channel_id),
                after_message_id=int(after_message_id),
                upto_message_id=int(upto_message_id),
                visible_message_ids={int(value) for value in visible_message_ids},
                operations=batch.operations,
            )
            touched_targets: set[int] = set()
            for operation in batch.operations:
                if operation.memory_id is None:
                    continue
                if operation.memory_id in touched_targets:
                    raise ValueError(
                        f"memory_id changed more than once in batch: "
                        f"{operation.memory_id}")
                touched_targets.add(operation.memory_id)
                self._require_active_memory(conn, operation.memory_id)
        finally:
            conn.close()

    def apply_curator_batch(
        self, batch: "CuratorBatch", *, curator_name: str,
        channel_id: str, after_message_id: int, upto_message_id: int,
        visible_message_ids: set[int], run_id: str,
        curator_model: str,
    ) -> dict:
        """Atomically apply one validated curator batch and advance its cursor."""
        channel_id = str(channel_id)
        after_message_id = int(after_message_id)
        upto_message_id = int(upto_message_id)
        if upto_message_id < after_message_id:
            raise ValueError("upto_message_id cannot be before after_message_id")
        visible_message_ids = {int(value) for value in visible_message_ids}
        if upto_message_id > after_message_id and upto_message_id not in visible_message_ids:
            raise ValueError("upto_message_id must be the last visible message id")
        if any(
            message_id <= after_message_id or message_id > upto_message_id
            for message_id in visible_message_ids
        ):
            raise ValueError("visible_message_ids do not match the batch interval")

        conn = self.database._get_conn()
        conn.execute("PRAGMA foreign_keys = ON")
        created_ids: list[int] = []
        updated_ids: list[int] = []
        superseded_ids: list[int] = []
        archived_ids: list[int] = []
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor_row = conn.execute(
                """
                SELECT last_message_id, last_successful_run_id
                FROM curator_cursors
                WHERE curator_name = ? AND channel_id = ?
                """,
                (curator_name, channel_id),
            ).fetchone()
            current = int(cursor_row["last_message_id"]) if cursor_row else 0
            if current >= upto_message_id:
                conn.rollback()
                return {
                    "applied": False,
                    "duplicate": True,
                    "cursor": current,
                    "created_ids": [],
                    "updated_ids": [],
                    "superseded_ids": [],
                    "archived_ids": [],
                }
            if current != after_message_id:
                raise ValueError(
                    f"curator cursor changed: expected {after_message_id}, current {current}")

            self._validate_curator_run(conn, run_id, batch, curator_model)
            self._validate_batch_evidence(
                conn,
                channel_id=channel_id,
                after_message_id=after_message_id,
                upto_message_id=upto_message_id,
                visible_message_ids=visible_message_ids,
                operations=batch.operations,
            )

            touched_targets: set[int] = set()
            for operation in batch.operations:
                if operation.memory_id is not None:
                    if operation.memory_id in touched_targets:
                        raise ValueError(
                            f"memory_id changed more than once in batch: "
                            f"{operation.memory_id}")
                    touched_targets.add(operation.memory_id)

                sources = self._normalize_sources([
                    {
                        "conversation_message_id": source.message_id,
                        "quote": source.quote,
                        "evidence_role": source.evidence_role,
                    }
                    for source in operation.sources
                ])
                if operation.action == "create":
                    memory_id = self._insert_memory(
                        conn,
                        summary=operation.summary,
                        quote=operation.quote,
                        reason=operation.reason,
                        memory_type=operation.memory_type,
                        curator_model=curator_model,
                    )
                    self._insert_sources(conn, memory_id, sources)
                    created_ids.append(memory_id)
                    continue

                self._require_active_memory(conn, int(operation.memory_id))
                if operation.action == "update":
                    updates = {"reason": operation.reason}
                    if operation.summary is not None:
                        updates["summary"] = operation.summary
                        updates["embedding"] = None
                        updates["embedding_model"] = None
                    if operation.memory_type is not None:
                        updates["memory_type"] = operation.memory_type
                    if operation.has_quote:
                        updates["quote"] = operation.quote
                    assignments = [f"{field} = ?" for field in updates]
                    conn.execute(
                        f"""
                        UPDATE personal_memories
                        SET {', '.join(assignments)}, updated_at = datetime('now')
                        WHERE id = ?
                        """,
                        (*updates.values(), operation.memory_id),
                    )
                    self._insert_sources(conn, operation.memory_id, sources)
                    updated_ids.append(operation.memory_id)
                elif operation.action == "archive":
                    conn.execute(
                        """
                        UPDATE personal_memories
                        SET status = 'archived', superseded_by = NULL,
                            updated_at = datetime('now')
                        WHERE id = ?
                        """,
                        (operation.memory_id,),
                    )
                    self._insert_sources(conn, operation.memory_id, sources)
                    archived_ids.append(operation.memory_id)
                elif operation.action == "supersede":
                    replacement_id = self._insert_memory(
                        conn,
                        summary=operation.summary,
                        quote=operation.quote,
                        reason=operation.reason,
                        memory_type=operation.memory_type,
                        curator_model=curator_model,
                    )
                    self._insert_sources(conn, replacement_id, sources)
                    self._insert_sources(conn, operation.memory_id, sources)
                    conn.execute(
                        """
                        UPDATE personal_memories
                        SET status = 'superseded', superseded_by = ?,
                            updated_at = datetime('now')
                        WHERE id = ?
                        """,
                        (replacement_id, operation.memory_id),
                    )
                    created_ids.append(replacement_id)
                    superseded_ids.append(operation.memory_id)
                else:  # pragma: no cover - parser owns the action enum
                    raise ValueError(f"unsupported curator action: {operation.action}")

            conn.execute(
                """
                INSERT INTO curator_cursors
                    (curator_name, channel_id, last_message_id,
                     last_successful_run_id, updated_at)
                VALUES (?, ?, ?, ?, datetime('now'))
                ON CONFLICT(curator_name, channel_id) DO UPDATE SET
                    last_message_id = excluded.last_message_id,
                    last_successful_run_id = excluded.last_successful_run_id,
                    updated_at = datetime('now')
                """,
                (curator_name, channel_id, upto_message_id, run_id),
            )
            conn.commit()
            return {
                "applied": True,
                "duplicate": False,
                "cursor": upto_message_id,
                "created_ids": created_ids,
                "updated_ids": updated_ids,
                "superseded_ids": superseded_ids,
                "archived_ids": archived_ids,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
