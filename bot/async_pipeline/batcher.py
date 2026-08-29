"""LT-178 batch timing and wake-up coordination.

SQLite owns the durable queue.  This module only supplies the low-latency
30-second silence timer and the 60-second maximum wait.  Losing its in-process
event on restart is harmless: the heartbeat calls the same due-aware planner.
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from bot.database import Database
from bot.logger import get_logger

from .batch_planner import plan_next_batch
from .tool_batches import ToolBatchRepository


logger = get_logger(__name__)
SILENCE_SECONDS = 30.0
MAX_WAIT_SECONDS = 60.0


def _utc_datetime(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def plan_due_batch(
    db: Database,
    repository: ToolBatchRepository,
    *,
    channel_id: str,
    execution_mode: str,
    silence_seconds: float = SILENCE_SECONDS,
    max_wait_seconds: float = MAX_WAIT_SECONDS,
    force: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Plan one conversation batch only when its timing rule is due.

    ``plan_next_batch`` deliberately knows nothing about clocks.  Keeping this
    wrapper separate lets the normal timer and the two-minute heartbeat share
    exactly the same decision without creating a second durable queue.
    """
    channel_id = str(channel_id)
    open_batch = repository.open_conversation_batch(channel_id)
    if open_batch is not None:
        return {
            "action": "waiting",
            "reason": "open_batch",
            "batch": open_batch,
            "cursor": repository.get_cursor(channel_id),
        }

    cursor = repository.get_cursor(channel_id)
    messages = db.get_conversation_messages_after(channel_id, cursor)
    if not messages:
        return {"action": "idle", "cursor": cursor}

    user_messages = [item for item in messages if item["role"] == "user"]
    if not user_messages:
        # Assistant/system-only tails never need to wait for a silence window;
        # the lower-level planner advances the cursor without opening a batch.
        return plan_next_batch(
            db,
            repository,
            channel_id=channel_id,
            execution_mode=execution_mode,
        )

    moment = _utc_datetime(now or datetime.now(timezone.utc))
    first_at = _utc_datetime(user_messages[0]["created_at"])
    last_at = _utc_datetime(user_messages[-1]["created_at"])
    silence_due = (moment - last_at).total_seconds() >= max(
        float(silence_seconds), 0.0)
    max_wait_due = (moment - first_at).total_seconds() >= max(
        float(max_wait_seconds), 0.0)
    if not (force or silence_due or max_wait_due):
        silence_left = max(
            float(silence_seconds) - (moment - last_at).total_seconds(), 0.0)
        max_wait_left = max(
            float(max_wait_seconds) - (moment - first_at).total_seconds(), 0.0)
        return {
            "action": "waiting",
            "reason": "silence_window",
            "cursor": cursor,
            "due_in_seconds": min(silence_left, max_wait_left),
        }

    outcome = plan_next_batch(
        db,
        repository,
        channel_id=channel_id,
        execution_mode=execution_mode,
    )
    outcome["reason"] = (
        "forced" if force else "max_wait" if max_wait_due else "silence"
    )
    return outcome


BatchReadyCallback = Callable[[dict[str, Any]], Any]


class BatchCoordinator:
    """Low-latency batch timer plus check-in batch producer."""

    def __init__(
        self,
        db: Database,
        repository: ToolBatchRepository,
        *,
        channel_ids: Iterable[str],
        execution_mode: str,
        silence_seconds: float = SILENCE_SECONDS,
        max_wait_seconds: float = MAX_WAIT_SECONDS,
        idle_poll_seconds: float = 60.0,
        on_batch_ready: BatchReadyCallback | None = None,
    ) -> None:
        self.db = db
        self.repository = repository
        self.channel_ids = [str(value) for value in channel_ids]
        self.execution_mode = execution_mode
        self.silence_seconds = max(float(silence_seconds), 0.0)
        self.max_wait_seconds = max(float(max_wait_seconds), 0.0)
        self.idle_poll_seconds = max(float(idle_poll_seconds), 0.01)
        self.on_batch_ready = on_batch_ready
        self._running = False
        self._wake = asyncio.Event()
        self._forced_channels: set[str] = set()

    @property
    def running(self) -> bool:
        return self._running

    def set_batch_ready_callback(
        self, callback: BatchReadyCallback | None
    ) -> None:
        self.on_batch_ready = callback

    def notify_user_message(self, channel_id: str, row_id: int) -> None:
        """Wake the timer after a newly inserted durable user-message row."""
        logger.debug(
            "🧰 工具批次计时器收到消息: channel=%s row=%s",
            channel_id,
            row_id,
        )
        self._wake.set()

    def notify_batch_finished(self, _batch: dict[str, Any] | None = None) -> None:
        """Re-evaluate messages that arrived while the previous batch ran."""
        self._wake.set()

    def plan_channel(
        self,
        channel_id: str,
        *,
        force: bool = False,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        channel_id = str(channel_id)
        effective_force = force or channel_id in self._forced_channels
        outcome = plan_due_batch(
            self.db,
            self.repository,
            channel_id=channel_id,
            execution_mode=self.execution_mode,
            silence_seconds=self.silence_seconds,
            max_wait_seconds=self.max_wait_seconds,
            force=effective_force,
            now=now,
        )
        if outcome.get("action") == "created":
            self._forced_channels.discard(channel_id)
            self._announce(outcome["batch"])
        elif outcome.get("action") in {"idle", "skipped"}:
            self._forced_channels.discard(channel_id)
        return outcome

    def force(self, channel_id: str) -> dict[str, Any]:
        """Create the current batch immediately for a chat ``[SILENT]``."""
        channel_id = str(channel_id)
        # An older batch may still occupy the channel.  Remember the force
        # request until that batch finishes; otherwise the wake-up would fall
        # back to the normal 30-second silence delay for the factual query.
        self._forced_channels.add(channel_id)
        self._wake.set()
        return self.plan_channel(channel_id, force=True)

    def create_check_in(
        self,
        *,
        channel_id: str,
        source_ref: str,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        batch, created = self.repository.create_check_in_batch(
            channel_id=str(channel_id),
            source_ref=str(source_ref),
            payload=payload,
            execution_mode=self.execution_mode,
        )
        if created:
            self._announce(batch)
        return batch, created

    async def run(self) -> None:
        if self._running:
            raise RuntimeError("BatchCoordinator is already running")
        self._running = True
        logger.info("⏳ 工具批次计时器已启动")
        try:
            while self._running:
                self._wake.clear()
                delays: list[float] = []
                for channel_id in self.channel_ids:
                    try:
                        outcome = self.plan_channel(channel_id)
                    except Exception:
                        # A malformed historical timestamp or one locked DB
                        # row must not permanently kill the normal timer.
                        logger.exception(
                            "工具批次计时器规划失败: channel=%s", channel_id
                        )
                        continue
                    if outcome.get("reason") == "silence_window":
                        delays.append(float(outcome["due_in_seconds"]))
                timeout = min(delays) if delays else self.idle_poll_seconds
                try:
                    await asyncio.wait_for(
                        self._wake.wait(), timeout=max(timeout, 0.01)
                    )
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise
        finally:
            self._running = False
            logger.info("⏳ 工具批次计时器已停止")

    async def stop(self) -> None:
        self._running = False
        self._wake.set()

    def _announce(self, batch: dict[str, Any]) -> None:
        callback = self.on_batch_ready
        if callback is None:
            return
        try:
            result = callback(batch)
            if inspect.isawaitable(result):
                asyncio.create_task(result)
        except Exception:
            logger.exception("工具批次 ready 回调失败: %s", batch.get("id"))
