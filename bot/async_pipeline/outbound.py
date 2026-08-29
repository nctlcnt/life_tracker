"""Generation serialization and the persistent outbound delivery consumer."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import inspect
from typing import Any, Awaitable, Callable

from bot.async_pipeline.repository import (
    TERMINAL_STATUSES,
    OutboundDeliveryRepository,
)
from bot.logger import get_logger


logger = get_logger(__name__)
DeliveryTransport = Callable[[dict], Awaitable[list[str] | None]]
TerminalCallback = Callable[[dict, str], Any]


class GenerationGate:
    """Process-wide lock for context snapshotting and user-visible generation."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    async def __aenter__(self):
        await self._lock.acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self._lock.release()

    def locked(self) -> bool:
        return self._lock.locked()


class NullGenerationGate:
    """No-op gate used while the outbound feature flag is disabled."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    def locked(self) -> bool:
        return False


@dataclass(frozen=True)
class DeliveryReceipt:
    id: int
    status: str
    discord_message_ids: tuple[str, ...] = ()
    last_error: str | None = None

    @classmethod
    def from_row(cls, row: dict) -> "DeliveryReceipt":
        return cls(
            id=int(row["id"]),
            status=str(row["status"]),
            discord_message_ids=tuple(
                str(value) for value in row.get("discord_message_ids", [])
            ),
            last_error=row.get("last_error"),
        )

    @property
    def delivered(self) -> bool:
        return self.status == "sent"


class DeliveryFailed(RuntimeError):
    def __init__(self, receipt: DeliveryReceipt):
        self.receipt = receipt
        super().__init__(
            f"outbound delivery {receipt.id} failed: "
            f"{receipt.last_error or 'unknown error'}"
        )


class OutboundQueue:
    """Single consumer backed by ``outbound_deliveries``.

    The SQLite row owns delivery state. ``_wake`` and per-delivery events only
    reduce latency inside the current process; losing them on restart is safe.
    """

    def __init__(self, repository: OutboundDeliveryRepository,
                 transport: DeliveryTransport, *,
                 lease_seconds: float = 300.0,
                 max_attempts: int = 3,
                 retry_base_seconds: float = 1.0,
                 idle_poll_seconds: float = 1.0,
                 on_terminal: TerminalCallback | None = None) -> None:
        self.repository = repository
        self.transport = transport
        self.lease_seconds = max(float(lease_seconds), 0.0)
        self.max_attempts = max(int(max_attempts), 1)
        self.retry_base_seconds = max(float(retry_base_seconds), 0.0)
        self.idle_poll_seconds = max(float(idle_poll_seconds), 0.01)
        self.on_terminal = on_terminal
        self._running = False
        self._wake = asyncio.Event()
        self._waiters: dict[int, asyncio.Event] = {}

    @property
    def running(self) -> bool:
        return self._running

    async def enqueue_message(self, *, channel_id: str, content: str,
                              source_type: str, source_id: str,
                              dedupe_key: str,
                              wait_for_delivery: bool = True) -> DeliveryReceipt:
        row, _created = self.repository.enqueue_message(
            channel_id=channel_id,
            content=content,
            source_type=source_type,
            source_id=source_id,
            dedupe_key=dedupe_key,
        )
        self._wake.set()
        if wait_for_delivery and row["status"] not in TERMINAL_STATUSES:
            row = await self._wait_for_terminal(int(row["id"]))
        return DeliveryReceipt.from_row(row)

    async def enqueue_reaction(self, *, channel_id: str, reaction: str,
                               target_discord_message_id: str,
                               source_type: str, source_id: str,
                               dedupe_key: str,
                               wait_for_delivery: bool = True) -> DeliveryReceipt:
        row, _created = self.repository.enqueue_reaction(
            channel_id=channel_id,
            reaction=reaction,
            target_discord_message_id=target_discord_message_id,
            source_type=source_type,
            source_id=source_id,
            dedupe_key=dedupe_key,
        )
        self._wake.set()
        if wait_for_delivery and row["status"] not in TERMINAL_STATUSES:
            row = await self._wait_for_terminal(int(row["id"]))
        return DeliveryReceipt.from_row(row)

    async def _wait_for_terminal(self, delivery_id: int) -> dict:
        waiter = self._waiters.setdefault(delivery_id, asyncio.Event())
        try:
            while True:
                row = self.repository.get(delivery_id)
                if row is None:
                    raise RuntimeError(f"outbound delivery {delivery_id} disappeared")
                if row["status"] in TERMINAL_STATUSES:
                    return row
                waiter.clear()
                # Close the race between the SELECT above and Event.clear().
                row = self.repository.get(delivery_id)
                if row is None:
                    raise RuntimeError(f"outbound delivery {delivery_id} disappeared")
                if row["status"] in TERMINAL_STATUSES:
                    return row
                await waiter.wait()
        finally:
            self._waiters.pop(delivery_id, None)

    def _signal_delivery(self, delivery_id: int) -> None:
        waiter = self._waiters.get(delivery_id)
        if waiter:
            waiter.set()

    def set_terminal_callback(
        self, callback: TerminalCallback | None
    ) -> None:
        self.on_terminal = callback

    async def _notify_terminal(self, delivery: dict, status: str) -> None:
        if self.on_terminal is None:
            return
        try:
            result = self.on_terminal(delivery, status)
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.exception(
                "outbound terminal callback failed: delivery=%s status=%s",
                delivery.get("id"),
                status,
            )

    async def run(self) -> None:
        if self._running:
            raise RuntimeError("OutboundQueue is already running")
        self._running = True
        logger.info("📤 OutboundQueue consumer 已启动")
        try:
            while self._running:
                self._wake.clear()
                recovered = self.repository.recover_expired_sending(
                    lease_seconds=self.lease_seconds)
                for delivery_id in recovered:
                    logger.warning(
                        f"♻️ 回收过期 outbound delivery: {delivery_id}")
                    self._signal_delivery(delivery_id)

                delivery = self.repository.claim_next()
                if delivery is not None:
                    await self._deliver(delivery)
                    continue

                try:
                    await asyncio.wait_for(
                        self._wake.wait(), timeout=self.idle_poll_seconds)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise
        finally:
            self._running = False
            for waiter in self._waiters.values():
                waiter.set()
            logger.info("📤 OutboundQueue consumer 已停止")

    async def stop(self) -> None:
        self._running = False
        self._wake.set()

    async def _deliver(self, delivery: dict) -> None:
        delivery_id = int(delivery["id"])
        lease_token = str(delivery["lease_token"])
        try:
            message_ids = await self.transport(delivery) or []
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            if int(delivery["attempt_count"]) >= self.max_attempts:
                updated = self.repository.mark_failed(
                    delivery_id, lease_token, error)
                if updated:
                    logger.error(
                        f"❌ outbound delivery {delivery_id} 最终失败: {error}")
                    await self._notify_terminal(delivery, "failed")
            else:
                delay = min(
                    self.retry_base_seconds
                    * (2 ** max(int(delivery["attempt_count"]) - 1, 0)),
                    60.0,
                )
                updated = self.repository.mark_retry(
                    delivery_id, lease_token, error, delay_seconds=delay)
                if updated:
                    logger.warning(
                        f"🔁 outbound delivery {delivery_id} 在 {delay:g}s 后重试: {error}")
            if not updated:
                logger.warning(
                    f"⚠️ outbound delivery {delivery_id} lease 已失效，忽略旧 owner 结果")
            self._signal_delivery(delivery_id)
            self._wake.set()
            return

        updated = self.repository.mark_sent(
            delivery_id, lease_token,
            [str(message_id) for message_id in message_ids],
        )
        if not updated:
            logger.warning(
                f"⚠️ outbound delivery {delivery_id} lease 已失效，未写入 sent")
        else:
            await self._notify_terminal(delivery, "sent")
        self._signal_delivery(delivery_id)
        self._wake.set()
