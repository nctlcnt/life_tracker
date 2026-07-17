"""Application-facing memory service.

The SQLite Database remains the repository during the first refactor phase.
Callers use this service so storage, recall, and future compaction can evolve
without spreading another implementation across the runtime.
"""
import asyncio
import inspect

import config
from bot import embeddings
from bot.logger import get_logger
from bot.memory.models import MemoryContext
from bot.memory.repository import MemoryRepository

logger = get_logger(__name__)


class MemoryRecallDisabled(RuntimeError):
    """Semantic recall is not configured."""


class MemoryRecallUnavailable(RuntimeError):
    """Semantic recall was configured but the embedding request failed."""


class MemoryService:
    """Own the current durable-memory, conversation, and recall workflows."""

    def __init__(self, repository: MemoryRepository, *, durable_repository=None):
        self.repository = repository
        self.durable_repository = durable_repository or repository
        self._embedding_tasks: set[asyncio.Task] = set()
        # Runtime collaborators can resolve the shared service from the DB while
        # keeping existing public call signatures stable.
        repository._memory_service = self

    # Durable memories
    def list_durable(self, *, include_expired: bool = False) -> list[dict]:
        if hasattr(self.durable_repository, "list"):
            return self.durable_repository.list(include_expired=include_expired)
        return self.durable_repository.get_all_memories(
            include_expired=include_expired
        )

    def save_durable(self, content: str, *, source: str = "ai",
                     memory_type: str | None = None,
                     valid_until: str | None = None) -> int:
        if hasattr(self.durable_repository, "save"):
            return self.durable_repository.save(
                content,
                source=source,
                memory_type=memory_type,
                valid_until=valid_until,
            )
        return self.durable_repository.add_memory(
            content,
            source=source,
            memory_type=memory_type,
            valid_until=valid_until,
        )

    def update_durable(self, memory_id: int, **fields) -> None:
        if hasattr(self.durable_repository, "update"):
            self.durable_repository.update(memory_id, **fields)
            return
        self.durable_repository.update_memory(memory_id, **fields)

    def delete_durable(self, memory_id: int) -> None:
        if hasattr(self.durable_repository, "delete"):
            self.durable_repository.delete(memory_id)
            return
        self.durable_repository.delete_memory(memory_id)

    def durable_markdown(self) -> str:
        if hasattr(self.durable_repository, "prompt_markdown"):
            return self.durable_repository.prompt_markdown()
        memories = self.list_durable()
        return "\n".join(f"- {item['content']}" for item in memories)

    def durable_stats(self) -> dict:
        if hasattr(self.durable_repository, "stats"):
            return self.durable_repository.stats()
        memories = self.list_durable()
        return {"total_entries": len(memories), "active_entries": len(memories)}

    # Conversation log and working window
    async def ingest_message(self, **message) -> int | None:
        """Persist one raw message and asynchronously enrich it for recall."""
        result = self.repository.add_conversation_message(**message)
        row_id = await result if inspect.isawaitable(result) else result
        self.schedule_embedding(row_id, str(message["channel_id"]))
        return row_id

    def recent_messages(self, channel_id: str, *, limit: int = 20) -> list[dict]:
        return self.repository.get_recent_ai_messages(str(channel_id), limit=limit)

    def schedule_embedding(self, row_id: int | None, channel_id: str) -> None:
        """Start best-effort enrichment without delaying the message path."""
        if row_id is None or not config.EMBEDDING_ENABLED:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self.embed_message(row_id, channel_id))
        self._embedding_tasks.add(task)
        task.add_done_callback(self._embedding_tasks.discard)

    async def embed_message(self, row_id: int, channel_id: str) -> bool:
        return await embeddings.embed_and_store(self.repository, row_id, str(channel_id))

    # Semantic recall
    async def recall(self, query: str, channel_id: str, *, limit: int = 5,
                     exclude_recent: int = 20) -> list[dict]:
        query = (query or "").strip()
        if not query:
            return []
        if not config.EMBEDDING_ENABLED:
            raise MemoryRecallDisabled("embedding is not configured")
        query_embedding = await embeddings.embed_text(query)
        if not query_embedding:
            raise MemoryRecallUnavailable("embedding request failed")
        return await asyncio.to_thread(
            self.repository.get_relevant_conversation_snippets,
            query_embedding,
            str(channel_id),
            model=config.EMBEDDING_MODEL,
            limit=limit,
            exclude_recent=exclude_recent,
            min_relevance=config.EMBEDDING_MIN_RELEVANCE,
        )

    async def build_context(self, *, query: str | None, channel_id: str,
                            include_memories: bool = True,
                            include_relevant_history: bool = True,
                            recall_limit: int = 5,
                            exclude_recent: int = 20) -> MemoryContext:
        memories = self.list_durable() if include_memories else []
        relevant_history = []
        if query and include_relevant_history:
            try:
                relevant_history = await self.recall(
                    query,
                    channel_id,
                    limit=recall_limit,
                    exclude_recent=exclude_recent,
                )
            except (MemoryRecallDisabled, MemoryRecallUnavailable) as exc:
                logger.warning("Memory recall skipped: %s", exc)
            except Exception as exc:
                logger.warning(
                    "Memory recall failed: %s: %s", type(exc).__name__, exc
                )
        return MemoryContext(
            durable_memories=memories,
            durable_markdown=self.durable_markdown() if include_memories else "",
            relevant_history=relevant_history,
        )
