"""SQLite persistence for the LT-175 tool batch table.

这一层只负责把批次写进 `tool_batches` 并读回来。什么时候该建批、
消息区间的两个端点怎么算，依赖游标，属于 LT-176。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from bot.database import Database
from .repository import _utc_text


# 区间唯一约束和部分唯一索引都按 worker_name 分组判断，取值不统一会让
# 两条保证一起失效，所以所有写入方共用这一个常量。
TOOL_WORKER = "tool_worker"

OPEN_STATUSES = frozenset({"pending", "running", "retry_wait"})
TERMINAL_STATUSES = frozenset({"completed", "failed"})
EXECUTION_MODES = frozenset({"shadow", "apply"})

# 占用有效期。超过这个时间还停在 running 的批次，视为持有它的进程
# 已经不在了，下一次领取会把它取回来重新执行。
LEASE_SECONDS = 300.0

DELIVERY_KINDS = frozenset({"none", "reaction", "message"})
DELIVERY_STATUSES = frozenset({"not_needed", "pending", "queued", "sent",
                               "superseded", "failed"})


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

    # --- 领取 ---------------------------------------------------------------

    def claim_next(self, *, now=None, lease_seconds: float = LEASE_SECONDS
                   ) -> dict[str, Any] | None:
        """领取一批活，没有可领的就返回 None。

        三类记录可以被领走：到期的 `pending`、到期的 `retry_wait`、以及
        `locked_at` 已经超过占用有效期的 `running`。第三类就是卡住的
        批次——上一个持有者把它标成 running 之后进程退出了，没有人再
        改它的状态。**回收就发生在这一条语句里，没有第二条路径**，所以
        进程崩溃重启之后第一次领取就能把它取回来，不必等心跳。

        每次领取都换一个新的 lease_token。后续所有写入都必须带上它，
        这样上一个持有者即使缓过来，也改不动这一行了。

        一次只领一批：工具 worker 是单个协程，串行处理。
        """
        now_text = _utc_text(now)
        expiry_cutoff = _utc_text(
            (now or datetime.now(timezone.utc))
            - timedelta(seconds=lease_seconds))
        lease_token = uuid.uuid4().hex

        conn = self.db._get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT * FROM tool_batches
                WHERE worker_name = ?
                  AND (
                    (status IN ('pending', 'retry_wait') AND available_at <= ?)
                    OR
                    (status = 'running' AND locked_at <= ?)
                  )
                ORDER BY created_at, rowid
                LIMIT 1
                """,
                (self.worker_name, now_text, expiry_cutoff),
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            cursor = conn.execute(
                """
                UPDATE tool_batches
                SET status = 'running',
                    attempt_count = attempt_count + 1,
                    locked_at = ?, lease_token = ?, updated_at = ?
                WHERE id = ? AND status = ? AND lease_token IS ?
                """,
                (now_text, lease_token, now_text,
                 row["id"], row["status"], row["lease_token"]),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                return None
            claimed = conn.execute(
                "SELECT * FROM tool_batches WHERE id = ?", (row["id"],)
            ).fetchone()
            conn.commit()
            return _decode(claimed)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def renew_lease(self, batch_id: str, lease_token: str, *, now=None) -> bool:
        """续租：把占用有效期从现在起重新算。

        执行时间长的批次要定期调用，否则占用一过期，同一个 worker 的
        下一轮循环就会把它当成卡住的批次重新领走。返回 False 表示这一行
        已经不归你了，调用方应当停下来，不要再写它的结果。
        """
        now_text = _utc_text(now)
        conn = self.db._get_conn()
        try:
            cursor = conn.execute(
                """
                UPDATE tool_batches
                SET locked_at = ?, updated_at = ?
                WHERE id = ? AND status = 'running' AND lease_token = ?
                """,
                (now_text, now_text, str(batch_id), str(lease_token)),
            )
            conn.commit()
            return cursor.rowcount == 1
        finally:
            conn.close()

    # --- 终态 ---------------------------------------------------------------

    def mark_completed(
        self, batch_id: str, lease_token: str, *,
        result: dict[str, Any] | None = None,
        delivery_kind: str = "none",
        last_run_id: str | None = None,
        now=None,
    ) -> bool:
        """这一批干完了，同时记下结果和该怎么交付。

        交付要求必须和结果一起落库，不能等投递的时候再补：批次已经结束，
        那时候没有持有者能保证这两次写入都成功。三种交付方式对应第 2.1
        节——例行写入加一个反应，需要说话的走消息，确实没有产出就不交付。

        `delivery_status` 由 `delivery_kind` 推导，调用方写不出「说要发
        消息但状态是不需要」这种组合；后续的状态流转交给 advance_delivery。
        """
        if delivery_kind not in DELIVERY_KINDS:
            raise ValueError(
                f"delivery_kind must be one of {sorted(DELIVERY_KINDS)}")
        delivery_status = "not_needed" if delivery_kind == "none" else "pending"
        return self._finish(
            batch_id, lease_token, status="completed",
            result=result, delivery_kind=delivery_kind,
            delivery_status=delivery_status, last_run_id=last_run_id,
            error=None, now=now)

    def mark_failed(self, batch_id: str, lease_token: str, error: str, *,
                    last_run_id: str | None = None, now=None) -> bool:
        """重试预算用完了，这一批不再执行。

        失败同样是终态，`last_error` 必须留下来：这一批覆盖的那段消息
        里的活没有做，需要有人能查到，而不是让它悄悄消失。
        """
        return self._finish(
            batch_id, lease_token, status="failed", result=None,
            delivery_kind="none", delivery_status="not_needed",
            last_run_id=last_run_id, error=str(error), now=now)

    def mark_retry(self, batch_id: str, lease_token: str, error: str, *,
                   retry_after_seconds: float = 0.0, now=None) -> bool:
        """这一次没成，过一会儿再试。

        `retry_after_seconds` 之内不会被领取。这不是终态，占用会被释放，
        重试次数已经在领取时加过了。
        """
        moment = now or datetime.now(timezone.utc)
        now_text = _utc_text(moment)
        available_at = _utc_text(
            moment + timedelta(seconds=max(0.0, retry_after_seconds)))
        conn = self.db._get_conn()
        try:
            cursor = conn.execute(
                """
                UPDATE tool_batches
                SET status = 'retry_wait', available_at = ?, last_error = ?,
                    updated_at = ?, locked_at = NULL, lease_token = NULL
                WHERE id = ? AND status = 'running' AND lease_token = ?
                """,
                (available_at, str(error), now_text,
                 str(batch_id), str(lease_token)),
            )
            conn.commit()
            return cursor.rowcount == 1
        finally:
            conn.close()

    def _finish(self, batch_id: str, lease_token: str, *, status: str,
                result: dict[str, Any] | None, delivery_kind: str,
                delivery_status: str, last_run_id: str | None,
                error: str | None, now=None) -> bool:
        now_text = _utc_text(now)
        conn = self.db._get_conn()
        try:
            cursor = conn.execute(
                """
                UPDATE tool_batches
                SET status = ?, result_json = ?,
                    delivery_kind = ?, delivery_status = ?,
                    last_run_id = COALESCE(?, last_run_id),
                    last_error = ?, completed_at = ?, updated_at = ?,
                    locked_at = NULL, lease_token = NULL
                WHERE id = ? AND status = 'running' AND lease_token = ?
                """,
                (status,
                 json.dumps(result, ensure_ascii=False) if result is not None
                 else None,
                 delivery_kind, delivery_status, last_run_id, error,
                 now_text, now_text, str(batch_id), str(lease_token)),
            )
            conn.commit()
            return cursor.rowcount == 1
        finally:
            conn.close()

    # --- 交付与运维查询 -----------------------------------------------------

    def pending_deliveries(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """已经结束、但结果还没有送出去的批次，按结束顺序排。

        LT-177 的心跳靠它取活，而不是靠「批次已经 completed」去猜用户
        是不是已经收到了。
        """
        conn = self.db._get_conn()
        try:
            return [_decode(row) for row in conn.execute(
                """
                SELECT * FROM tool_batches
                WHERE worker_name = ? AND delivery_status = 'pending'
                ORDER BY completed_at, rowid
                LIMIT ?
                """,
                (self.worker_name, int(limit)),
            )]
        finally:
            conn.close()

    def advance_delivery(self, batch_id: str, *, from_status: str,
                         to_status: str, now=None) -> bool:
        """推进交付状态，只有当前状态符合预期时才动。

        投递本身可能被重复触发（心跳每两分钟跑一次），靠这个条件保证
        同一条结果不会被送出去两次。
        """
        for value in (from_status, to_status):
            if value not in DELIVERY_STATUSES:
                raise ValueError(
                    f"delivery_status must be one of "
                    f"{sorted(DELIVERY_STATUSES)}")
        now_text = _utc_text(now)
        conn = self.db._get_conn()
        try:
            cursor = conn.execute(
                """
                UPDATE tool_batches
                SET delivery_status = ?, updated_at = ?
                WHERE id = ? AND delivery_status = ?
                """,
                (to_status, now_text, str(batch_id), from_status),
            )
            conn.commit()
            return cursor.rowcount == 1
        finally:
            conn.close()

    def non_terminal_count(self) -> int:
        """还没结束的批次有多少。回退之前要等它归零。"""
        conn = self.db._get_conn()
        try:
            return int(conn.execute(
                f"""
                SELECT COUNT(*) FROM tool_batches
                WHERE worker_name = ?
                  AND status IN ({', '.join('?' for _ in OPEN_STATUSES)})
                """,
                (self.worker_name, *sorted(OPEN_STATUSES)),
            ).fetchone()[0])
        finally:
            conn.close()

    def non_terminal_ids(self, *, limit: int = 100) -> list[str]:
        """还没结束的批次的 id。紧急回退时要把它们显式列出来，不能遗忘。"""
        conn = self.db._get_conn()
        try:
            return [row["id"] for row in conn.execute(
                f"""
                SELECT id FROM tool_batches
                WHERE worker_name = ?
                  AND status IN ({', '.join('?' for _ in OPEN_STATUSES)})
                ORDER BY created_at, rowid
                LIMIT ?
                """,
                (self.worker_name, *sorted(OPEN_STATUSES), int(limit)),
            )]
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
