"""Persistence contract required by the memory service."""
from typing import Protocol


class MemoryRepository(Protocol):
    """Subset of storage operations owned by the memory layer."""

    def get_all_memories(self, include_expired: bool = False) -> list[dict]: ...

    def add_memory(self, content: str, source: str = "ai",
                   memory_type: str | None = None,
                   valid_until: str | None = None) -> int: ...

    def update_memory(self, memory_id: int, **fields) -> None: ...

    def delete_memory(self, memory_id: int) -> None: ...

    def add_conversation_message(self, **message) -> int | None: ...

    def get_recent_ai_messages(self, channel_id: str,
                               limit: int = 20) -> list[dict]: ...

    def get_relevant_conversation_snippets(
        self,
        query_embedding: list[float],
        channel_id: str,
        *,
        model: str,
        limit: int = 5,
        exclude_recent: int = 20,
        min_relevance: float = 0.55,
    ) -> list[dict]: ...

    def get_conversation_messages_upto(self, channel_id: str, upto_id: int,
                                       limit: int = 5) -> list[dict]: ...

    def update_conversation_embedding(self, row_id: int,
                                      embedding: list[float], context: str,
                                      model: str) -> None: ...
