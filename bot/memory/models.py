"""Structured values returned by the memory layer."""
from dataclasses import dataclass, field


@dataclass
class MemoryContext:
    """Memory-only context consumed by prompt construction."""

    durable_memories: list[dict] = field(default_factory=list)
    durable_markdown: str = ""
    relevant_history: list[dict] = field(default_factory=list)
