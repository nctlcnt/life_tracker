"""SQLite repository for curator-owned long-term memories (LT-136)."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass


MEMORY_STATUSES = {"active", "superseded", "archived"}
EVIDENCE_ROLES = {"supports", "contradicts", "supersedes"}


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
