"""SQLite persistence and fencing operations for outbound deliveries."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from bot.database import Database


NON_TERMINAL_STATUSES = frozenset({"pending", "sending", "retry_wait"})
TERMINAL_STATUSES = frozenset({"sent", "failed"})


class DeliveryDedupeConflict(ValueError):
    """A dedupe key was reused for a different logical delivery."""


def _utc_text(value: datetime | None = None) -> str:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.isoformat(sep=" ", timespec="microseconds")


def _decode(row) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    raw_ids = item.get("discord_message_ids_json")
    if raw_ids:
        try:
            item["discord_message_ids"] = json.loads(raw_ids)
        except json.JSONDecodeError:
            item["discord_message_ids"] = []
    else:
        item["discord_message_ids"] = []
    return item


class OutboundDeliveryRepository:
    """Small transactional repository for the LT-170 SQLite outbox."""

    def __init__(self, db: Database):
        self.db = db

    def get(self, delivery_id: int) -> dict[str, Any] | None:
        conn = self.db._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM outbound_deliveries WHERE id = ?",
                (int(delivery_id),),
            ).fetchone()
            return _decode(row)
        finally:
            conn.close()

    def get_by_dedupe_key(self, dedupe_key: str) -> dict[str, Any] | None:
        conn = self.db._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM outbound_deliveries WHERE dedupe_key = ?",
                (dedupe_key,),
            ).fetchone()
            return _decode(row)
        finally:
            conn.close()

    def enqueue_message(self, *, channel_id: str, content: str,
                        source_type: str, source_id: str,
                        dedupe_key: str) -> tuple[dict[str, Any], bool]:
        return self._enqueue(
            channel_id=channel_id,
            kind="message",
            content=content,
            reaction=None,
            target_discord_message_id=None,
            source_type=source_type,
            source_id=source_id,
            dedupe_key=dedupe_key,
        )

    def enqueue_reaction(self, *, channel_id: str, reaction: str,
                         target_discord_message_id: str,
                         source_type: str, source_id: str,
                         dedupe_key: str) -> tuple[dict[str, Any], bool]:
        return self._enqueue(
            channel_id=channel_id,
            kind="reaction",
            content=None,
            reaction=reaction,
            target_discord_message_id=target_discord_message_id,
            source_type=source_type,
            source_id=source_id,
            dedupe_key=dedupe_key,
        )

    def _enqueue(self, *, channel_id: str, kind: str, content: str | None,
                 reaction: str | None, target_discord_message_id: str | None,
                 source_type: str, source_id: str,
                 dedupe_key: str) -> tuple[dict[str, Any], bool]:
        if not str(channel_id).strip():
            raise ValueError("channel_id is required")
        if not str(source_type).strip() or not str(source_id).strip():
            raise ValueError("source_type and source_id are required")
        if not str(dedupe_key).strip():
            raise ValueError("dedupe_key is required")
        if kind == "message" and not (content or "").strip():
            raise ValueError("message content is required")
        if kind == "reaction" and (
            not (reaction or "").strip()
            or not (target_discord_message_id or "").strip()
        ):
            raise ValueError("reaction and target_discord_message_id are required")

        values = (
            str(channel_id), kind, content, reaction,
            str(target_discord_message_id) if target_discord_message_id else None,
            str(source_type), str(source_id), str(dedupe_key),
        )
        conn = self.db._get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO outbound_deliveries (
                    channel_id, kind, content, reaction,
                    target_discord_message_id, source_type, source_id, dedupe_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            created = cursor.rowcount == 1
            row = conn.execute(
                "SELECT * FROM outbound_deliveries WHERE dedupe_key = ?",
                (dedupe_key,),
            ).fetchone()
            if row is None:
                raise RuntimeError("outbound delivery disappeared after enqueue")
            item = _decode(row)
            expected = {
                "channel_id": values[0],
                "kind": kind,
                "content": content,
                "reaction": reaction,
                "target_discord_message_id": values[4],
                "source_type": str(source_type),
                "source_id": str(source_id),
            }
            if not created and any(item[key] != value for key, value in expected.items()):
                raise DeliveryDedupeConflict(
                    f"dedupe key {dedupe_key!r} belongs to another delivery")
            conn.commit()
            return item, created
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def claim_next(self, *, now: datetime | None = None) -> dict[str, Any] | None:
        """Claim an eligible per-channel head with a fencing token."""
        now_text = _utc_text(now)
        lease_token = uuid.uuid4().hex
        conn = self.db._get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                WITH channel_heads AS (
                    SELECT channel_id, MIN(id) AS id
                    FROM outbound_deliveries
                    WHERE status IN ('pending', 'sending', 'retry_wait')
                    GROUP BY channel_id
                )
                SELECT delivery.*
                FROM outbound_deliveries AS delivery
                JOIN channel_heads AS head ON head.id = delivery.id
                WHERE delivery.status IN ('pending', 'retry_wait')
                  AND delivery.available_at <= ?
                ORDER BY delivery.id
                LIMIT 1
                """,
                (now_text,),
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            cursor = conn.execute(
                """
                UPDATE outbound_deliveries
                SET status = 'sending',
                    attempt_count = attempt_count + 1,
                    locked_at = ?, lease_token = ?,
                    updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (now_text, lease_token, now_text, row["id"], row["status"]),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                return None
            claimed = conn.execute(
                "SELECT * FROM outbound_deliveries WHERE id = ?",
                (row["id"],),
            ).fetchone()
            conn.commit()
            return _decode(claimed)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def mark_sent(self, delivery_id: int, lease_token: str,
                  discord_message_ids: list[str] | None = None,
                  *, now: datetime | None = None) -> bool:
        now_text = _utc_text(now)
        conn = self.db._get_conn()
        try:
            cursor = conn.execute(
                """
                UPDATE outbound_deliveries
                SET status = 'sent', discord_message_ids_json = ?,
                    sent_at = ?, updated_at = ?, last_error = NULL,
                    locked_at = NULL, lease_token = NULL
                WHERE id = ? AND status = 'sending' AND lease_token = ?
                """,
                (json.dumps(discord_message_ids or []), now_text, now_text,
                 int(delivery_id), lease_token),
            )
            conn.commit()
            return cursor.rowcount == 1
        finally:
            conn.close()

    def mark_retry(self, delivery_id: int, lease_token: str, error: str,
                   *, delay_seconds: float,
                   now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        now_text = _utc_text(now)
        available_at = _utc_text(now + timedelta(seconds=max(delay_seconds, 0)))
        conn = self.db._get_conn()
        try:
            cursor = conn.execute(
                """
                UPDATE outbound_deliveries
                SET status = 'retry_wait', available_at = ?, last_error = ?,
                    updated_at = ?, locked_at = NULL, lease_token = NULL
                WHERE id = ? AND status = 'sending' AND lease_token = ?
                """,
                (available_at, str(error)[:4000], now_text,
                 int(delivery_id), lease_token),
            )
            conn.commit()
            return cursor.rowcount == 1
        finally:
            conn.close()

    def mark_failed(self, delivery_id: int, lease_token: str, error: str,
                    *, now: datetime | None = None) -> bool:
        now_text = _utc_text(now)
        conn = self.db._get_conn()
        try:
            cursor = conn.execute(
                """
                UPDATE outbound_deliveries
                SET status = 'failed', last_error = ?, updated_at = ?,
                    locked_at = NULL, lease_token = NULL
                WHERE id = ? AND status = 'sending' AND lease_token = ?
                """,
                (str(error)[:4000], now_text, int(delivery_id), lease_token),
            )
            conn.commit()
            return cursor.rowcount == 1
        finally:
            conn.close()

    def recover_expired_sending(self, *, lease_seconds: float,
                                now: datetime | None = None) -> list[int]:
        now = now or datetime.now(timezone.utc)
        cutoff = _utc_text(now - timedelta(seconds=max(lease_seconds, 0)))
        now_text = _utc_text(now)
        conn = self.db._get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT id FROM outbound_deliveries
                WHERE status = 'sending' AND locked_at <= ?
                ORDER BY id
                """,
                (cutoff,),
            ).fetchall()
            ids = [int(row["id"]) for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                conn.execute(
                    f"""
                    UPDATE outbound_deliveries
                    SET status = 'retry_wait', available_at = ?,
                        locked_at = NULL, lease_token = NULL,
                        last_error = 'delivery lease expired', updated_at = ?
                    WHERE id IN ({placeholders}) AND status = 'sending'
                    """,
                    (now_text, now_text, *ids),
                )
            conn.commit()
            return ids
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def non_terminal_count(self) -> int:
        conn = self.db._get_conn()
        try:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count FROM outbound_deliveries
                WHERE status IN ('pending', 'sending', 'retry_wait')
                """
            ).fetchone()
            return int(row["count"])
        finally:
            conn.close()

    def non_terminal_ids(self, *, limit: int = 100) -> list[int]:
        conn = self.db._get_conn()
        try:
            rows = conn.execute(
                """
                SELECT id FROM outbound_deliveries
                WHERE status IN ('pending', 'sending', 'retry_wait')
                ORDER BY id
                LIMIT ?
                """,
                (max(int(limit), 1),),
            ).fetchall()
            return [int(row["id"]) for row in rows]
        finally:
            conn.close()
