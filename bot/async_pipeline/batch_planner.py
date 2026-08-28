"""LT-176：决定下一批活覆盖哪一段消息。

游标只管 `conversation` 这一种来源。check-in 批次靠 `source_ref` 定位，
本来就没有消息区间，表上那条 CHECK 也强制它的三个区间字段为空，不要
试图把主动联系穿进这里。

什么时候调用这个函数由 worker 决定（第 2 节那个「30 秒无新消息或最早
消息已等待 60 秒」的触发条件属于 LT-178），这里只回答「按现在的状态，
该排什么批」。
"""

from __future__ import annotations

from typing import Any

from bot.database import Database

from .tool_batches import ToolBatchRepository


# 一次规划最多冻结多少条消息。超出的部分留给下一批：区间的终点一旦定下
# 就不再变，worker 运行期间到达的新消息一定落进后面的批次。
DEFAULT_PLAN_LIMIT = 100


def plan_next_batch(
    db: Database, repository: ToolBatchRepository, *,
    channel_id: str, execution_mode: str,
    limit: int = DEFAULT_PLAN_LIMIT,
) -> dict[str, Any]:
    """看一眼当前状态，决定要不要建批。

    返回的 `action` 有四种：

    * `waiting` —— 这个频道还有一批没干完，新消息等下一轮。
    * `idle` —— 游标之后没有新消息。
    * `skipped` —— 游标之后只有自己的发言，不建批，但游标要越过它们。
    * `created` —— 建了一批，区间在 `batch` 里。
    """
    channel_id = str(channel_id)

    open_batch = repository.open_conversation_batch(channel_id)
    if open_batch is not None:
        return {"action": "waiting", "batch": open_batch,
                "cursor": repository.get_cursor(channel_id)}

    cursor = repository.get_cursor(channel_id)
    messages = db.get_conversation_messages_after(
        channel_id, cursor, limit=max(int(limit), 1))
    if not messages:
        return {"action": "idle", "cursor": cursor}

    through = int(messages[-1]["id"])
    user_ids = [int(m["id"]) for m in messages if m["role"] == "user"]

    if not user_ids:
        # 只有新增的用户消息能触发动手。她自己说过的话不是事实——聊天模型
        # 可能说出「帮你记下了」这种没有依据的句子，而那句话会进历史被工具
        # 模型读到。所以游标要越过这些行，但它们不开批次。
        #
        # 推进到窗口内最后一条是安全的：这一窗全是非用户消息，窗口之外的
        # 用户消息 id 更大，不会被跳过。
        repository.advance_cursor(channel_id, through)
        return {"action": "skipped", "cursor": through,
                "skipped": len(messages)}

    batch, created = repository.create_conversation_batch(
        channel_id=channel_id,
        after_message_id=cursor,
        through_message_id=through,
        last_user_message_id=user_ids[-1],
        execution_mode=execution_mode,
    )
    return {"action": "created" if created else "waiting", "batch": batch,
            "cursor": cursor}
