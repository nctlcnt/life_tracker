"""Best-effort embedding worker for raw history already folded by compact."""
from __future__ import annotations

import asyncio

import config
from bot import embeddings
from bot.logger import get_logger

logger = get_logger(__name__)

PAGE_SIZE = 50
CONCURRENCY = 3

_inflight: dict[tuple[str, str], asyncio.Task] = {}
_requested_upto: dict[tuple[str, str], int] = {}


def _task_key(db, channel_id: str) -> tuple[str, str]:
    return (str(getattr(db, "db_path", id(db))), str(channel_id))


async def backfill_compacted_embeddings(db, channel_id: str,
                                        upto_id: int) -> dict:
    """Embed missing/old-model rows up to a frozen compact cursor.

    A failed row is left untouched and retried by the next startup/compact run.
    Paging advances by id, so one failure cannot spin the current worker forever.
    """
    channel_id = str(channel_id)
    upto_id = int(upto_id)
    if not config.EMBEDDING_ENABLED or upto_id <= 0:
        return {"attempted": 0, "completed": 0, "failed": 0, "upto_id": upto_id}

    attempted = completed = failed = 0
    after_id = 0
    logger.info(
        f"🔎 compact 历史 embedding 开始: channel={channel_id}, upto={upto_id}, "
        f"model={config.EMBEDDING_MODEL}")
    while True:
        row_ids = db.get_conversation_ids_needing_embedding(
            channel_id,
            upto_id=upto_id,
            model=config.EMBEDDING_MODEL,
            after_id=after_id,
            limit=PAGE_SIZE,
        )
        if not row_ids:
            break
        for start in range(0, len(row_ids), CONCURRENCY):
            chunk = row_ids[start:start + CONCURRENCY]
            results = await asyncio.gather(*(
                embeddings.embed_and_store(db, row_id, channel_id)
                for row_id in chunk
            ))
            attempted += len(chunk)
            completed += sum(bool(result) for result in results)
            failed += sum(not result for result in results)
        after_id = row_ids[-1]

    result = {
        "attempted": attempted,
        "completed": completed,
        "failed": failed,
        "upto_id": upto_id,
    }
    logger.info(
        f"🔎 compact 历史 embedding 完成: attempted={attempted}, "
        f"completed={completed}, failed={failed}, upto={upto_id}")
    return result


def schedule_compacted_embeddings(db, channel_id: str, upto_id: int) -> bool:
    """Start/coalesce one background backfill per database/channel."""
    if not config.EMBEDDING_ENABLED or int(upto_id) <= 0:
        return False
    key = _task_key(db, channel_id)
    upto_id = int(upto_id)
    _requested_upto[key] = max(_requested_upto.get(key, 0), upto_id)
    effective_upto = _requested_upto[key]
    cleared = db.clear_conversation_embeddings_after(
        str(channel_id), effective_upto)
    if cleared:
        logger.info(
            f"🔎 已清理明文尾巴遗留 embedding: {cleared} 条, cursor={effective_upto}")
    task = _inflight.get(key)
    if task is not None and not task.done():
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    async def run_pending_targets():
        processed_upto = 0
        try:
            while True:
                target = _requested_upto.get(key, 0)
                if target <= processed_upto:
                    break
                await backfill_compacted_embeddings(db, str(channel_id), target)
                processed_upto = target
        except Exception as exc:
            logger.warning(
                f"⚠️ compact 历史 embedding worker 异常退出，稍后重试: "
                f"{type(exc).__name__}: {exc}")

    task = loop.create_task(run_pending_targets())
    _inflight[key] = task

    def cleanup(done):
        if _inflight.get(key) is done:
            _inflight.pop(key, None)
            _requested_upto.pop(key, None)

    task.add_done_callback(cleanup)
    return True
