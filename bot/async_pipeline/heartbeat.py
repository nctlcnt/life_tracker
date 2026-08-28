"""LT-177：每两分钟跑一次的心跳。

它做四件事：补漏、投递、收尾、报警。**它不执行批次**——那需要工具模型，
属于 LT-178。第 4.3 节那套失败降级阶梯里，「重试一次」「返回做不到」这
几级也在执行的那一侧；心跳只管最后一级，把重试次数用完的批次终结掉并
发出告警。

过期批次的回收同样不在这里：按 LT-175 的决定，`claim_next` 自己会取回
占用过期的 `running`，心跳不自行转换状态。

原则来自第 4.3 节：**任何一层的失败方式都不能是「静默地什么都没做」。**
所以每一条岔路要么把活往前推一步，要么留下一条告警。
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Iterable

from bot.database import Database
from bot.logger import get_logger

from .batch_planner import plan_next_batch
from .outbound import OutboundQueue
from .tool_batches import ToolBatchRepository

logger = get_logger(__name__)


HEARTBEAT_SECONDS = 120.0
MAX_ATTEMPTS = 3
BACKLOG_THRESHOLD = 50
DONE_REACTION = "✅"

# 批次结果里放「要说的话」的键。工具 worker 与心跳的约定：写了这个键就
# 表示这一批有话要讲，由聊天模型用她自己的口吻说出来。
SAY_KEY = "say"


class BatchHeartbeat:
    """补漏、投递、收尾、报警。一次 tick 做完这四件事。"""

    def __init__(
        self, db: Database, repository: ToolBatchRepository,
        outbound: OutboundQueue, *, channel_ids: Iterable[str],
        execution_mode: str = "shadow",
        period_seconds: float = HEARTBEAT_SECONDS,
        max_attempts: int = MAX_ATTEMPTS,
        backlog_threshold: int = BACKLOG_THRESHOLD,
        reaction: str = DONE_REACTION,
        on_alert: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.db = db
        self.repository = repository
        self.outbound = outbound
        self.channel_ids = [str(c) for c in channel_ids]
        self.execution_mode = execution_mode
        self.period_seconds = max(float(period_seconds), 0.01)
        self.max_attempts = max(int(max_attempts), 1)
        self.backlog_threshold = max(int(backlog_threshold), 1)
        self.reaction = reaction
        self.on_alert = on_alert
        self._running = False
        self._wake = asyncio.Event()

    @property
    def running(self) -> bool:
        return self._running

    # --- 循环 ---------------------------------------------------------------

    async def run(self) -> None:
        if self._running:
            raise RuntimeError("BatchHeartbeat is already running")
        self._running = True
        logger.info("💓 批次心跳已启动")
        try:
            while self._running:
                self._wake.clear()
                try:
                    await self.tick()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    # 一次 tick 出错不能让心跳整个停摆，否则后面所有的补漏、
                    # 投递和收尾都跟着不做了，而那才是真正的静默失败。
                    logger.exception(f"💔 心跳这一轮出错，下一轮继续: {e}")
                    self._alert("heartbeat_tick_failed", {"error": str(e)})
                try:
                    await asyncio.wait_for(
                        self._wake.wait(), timeout=self.period_seconds)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise
        finally:
            self._running = False
            logger.info("💓 批次心跳已停止")

    async def stop(self) -> None:
        self._running = False
        self._wake.set()

    def wake(self) -> None:
        """让心跳立刻跑一轮，不必等到下一个周期。"""
        self._wake.set()

    # --- 一轮 ---------------------------------------------------------------

    async def tick(self) -> dict[str, Any]:
        """跑一轮，返回这一轮做了什么，便于观察与测试。"""
        planned = self._plan()
        reaped = self._reap()
        delivered = await self._deliver()
        backlog = self._check_backlog()
        return {"planned": planned, "reaped": reaped,
                "delivered": delivered, "backlog": backlog}

    def _plan(self) -> list[dict[str, Any]]:
        """补漏：确认每个频道的新消息都有批次在排队。"""
        results = []
        for channel_id in self.channel_ids:
            outcome = plan_next_batch(
                self.db, self.repository, channel_id=channel_id,
                execution_mode=self.execution_mode)
            results.append({"channel_id": channel_id, **outcome})
        return results

    def _reap(self) -> list[str]:
        """收尾：重试次数用完的批次转失败，并为每一批发一条告警。"""
        reaped = self.repository.reap_exhausted(max_attempts=self.max_attempts)
        for batch in reaped:
            logger.error(
                f"💀 批次重试用完，放弃执行: {batch['id']} "
                f"({batch['source_kind']}, {batch['attempt_count']} 次)")
            self._alert("batch_exhausted", {
                "batch_id": batch["id"],
                "channel_id": batch["channel_id"],
                "source_kind": batch["source_kind"],
                "attempt_count": batch["attempt_count"],
                "last_error": batch["last_error"],
            })
        return [batch["id"] for batch in reaped]

    async def _deliver(self) -> list[str]:
        """投递：把已经结束、还没送出去的结果交给统一发送队列。

        一条投递出意外不能连累后面几条。这里不推进状态，所以下一轮会
        重新交一次，而 dedupe_key 保证重复交付是幂等的。
        """
        handed_off = []
        for batch in self.repository.pending_deliveries():
            try:
                if await self._deliver_one(batch):
                    handed_off.append(batch["id"])
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception(f"❗ 投递批次结果时出错: {batch['id']}: {e}")
                self._alert("delivery_failed", {"batch_id": batch["id"],
                                                "error": str(e)})
        return handed_off

    async def _deliver_one(self, batch: dict[str, Any]) -> bool:
        batch_id = batch["id"]
        kind = batch["delivery_kind"]
        # 键只由批次 id 和交付方式决定，不掺时间也不掺内容：重复入队必须
        # 生成同一个键，否则下面那层的去重就失效了。
        dedupe_key = f"tool_batch:{batch_id}:{kind}"

        if kind == "reaction":
            target = self._discord_id_of(batch["last_user_message_id"])
            # 查目标放在入队之前，这样失败是 pending → failed 一步到位，
            # 不会留下半截状态。
            if target is None:
                logger.error(f"❗ 批次要加反应但找不到目标消息: {batch_id}")
                self._alert("delivery_target_missing", {
                    "batch_id": batch_id,
                    "last_user_message_id": batch["last_user_message_id"],
                })
                self.repository.advance_delivery(
                    batch_id, from_status="pending", to_status="failed")
                return False
            await self.outbound.enqueue_reaction(
                channel_id=batch["channel_id"], reaction=self.reaction,
                target_discord_message_id=target,
                source_type="tool_batch", source_id=batch_id,
                dedupe_key=dedupe_key, wait_for_delivery=False)
        else:
            content = (batch.get("result") or {}).get(SAY_KEY)
            if not (content or "").strip():
                logger.error(f"❗ 批次说要发消息但没有内容: {batch_id}")
                self._alert("delivery_content_missing", {"batch_id": batch_id})
                self.repository.advance_delivery(
                    batch_id, from_status="pending", to_status="failed")
                return False
            await self.outbound.enqueue_message(
                channel_id=batch["channel_id"], content=content,
                source_type="tool_batch", source_id=batch_id,
                dedupe_key=dedupe_key, wait_for_delivery=False)

        # 先入队再推进。反过来的话，崩在中间会留下一条已经标成 queued、
        # 却从来没有入过队的记录，那条投递就永久丢了。现在这个顺序最坏
        # 是下一轮重复入队一次，而 outbound_deliveries 上 dedupe_key 的
        # 唯一约束会把它挡掉，结果是幂等的。
        #
        # 因此 queued 的含义是「至少交给发送队列一次」。之后的重试归发送
        # 队列自己管，那张表是持久的；不要再加一个「重投 queued」的清扫，
        # 那会造成重复发送。
        self.repository.advance_delivery(
            batch_id, from_status="pending", to_status="queued")
        return True

    def _check_backlog(self) -> list[dict[str, Any]]:
        """报警：积压只用来判断系统是不是堵了。

        它判断不了某个 running 批次是否还在跑——如果整个系统只有一条消息，
        worker 领取后退出，条数永远不会超过阈值。那种情况靠占用有效期处理，
        不靠这里。
        """
        alerts = []
        for channel_id in self.channel_ids:
            cursor = self.repository.get_cursor(channel_id)
            pending = self._count_messages_after(channel_id, cursor)
            if pending >= self.backlog_threshold:
                logger.warning(
                    f"🐌 未处理消息积压: {channel_id} 有 {pending} 条")
                self._alert("backlog", {"channel_id": channel_id,
                                        "pending": pending})
                alerts.append({"channel_id": channel_id, "pending": pending})
        return alerts

    # --- 小工具 -------------------------------------------------------------

    def _discord_id_of(self, message_row_id: int | None) -> str | None:
        if message_row_id is None:
            return None
        conn = self.db._get_conn()
        try:
            row = conn.execute(
                "SELECT discord_message_id FROM conversation_messages "
                "WHERE id = ?", (int(message_row_id),)).fetchone()
        finally:
            conn.close()
        if row is None or not (row[0] or "").strip():
            return None
        return str(row[0])

    def _count_messages_after(self, channel_id: str, cursor: int) -> int:
        conn = self.db._get_conn()
        try:
            return int(conn.execute(
                "SELECT COUNT(*) FROM conversation_messages "
                "WHERE channel_id = ? AND id > ?",
                (str(channel_id), int(cursor))).fetchone()[0])
        finally:
            conn.close()

    def _alert(self, kind: str, payload: dict[str, Any]) -> None:
        if self.on_alert is None:
            return
        try:
            self.on_alert(kind, payload)
        except Exception as e:
            logger.warning(f"⚠️ 告警回调本身出错: {e}")
