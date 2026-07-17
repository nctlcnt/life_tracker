"""Markdown-backed durable memory storage."""
from __future__ import annotations

import fcntl
import json
import math
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from bot.logger import get_logger

logger = get_logger(__name__)

_ENTRY_RE = re.compile(
    r"<!-- memory:(?P<meta>\{.*?\}) -->\n(?P<body>.*?)"
    r"(?=\n<!-- memory:|\n## |\Z)",
    re.DOTALL,
)
_META_RE = re.compile(r"<!-- memory:\{.*?\} -->\n?", re.DOTALL)

_SECTION_ORDER = (
    ("preference", "Preferences"),
    ("interaction_style", "Interaction Guidance"),
    ("identity", "Stable Profile"),
    ("health", "Health and Wellbeing"),
    ("plan", "Current Plans"),
    ("academic", "Academic"),
)
_SECTION_BY_TYPE = dict(_SECTION_ORDER)


def estimate_tokens(text: str) -> int:
    """Conservative provider-independent estimate for mixed Chinese/English."""
    if not text:
        return 0
    cjk = sum(
        1
        for char in text
        if "\u3400" <= char <= "\u9fff"
        or "\uf900" <= char <= "\ufaff"
    )
    other = len(text) - cjk
    return math.ceil((cjk + other / 4) * 1.1)


class MarkdownMemoryRepository:
    """Store durable memories in one human-readable Markdown document.

    The legacy SQLite table is retained as a write-through shadow during the
    migration window so Litestream continues to provide an offsite fallback.
    Reads always come from Markdown.
    """

    def __init__(self, path: str, *, legacy_repository=None,
                 token_budget: int = 4000):
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.legacy_repository = legacy_repository
        self.token_budget = max(int(token_budget), 1)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_migrated()

    @contextmanager
    def _lock(self, *, exclusive: bool):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(
                lock_file.fileno(),
                fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH,
            )
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _ensure_migrated(self) -> None:
        with self._lock(exclusive=True):
            if self.path.exists():
                return
            entries = []
            if self.legacy_repository is not None:
                entries = self.legacy_repository.get_all_memories(
                    include_expired=True
                )
            self._write_unlocked(self._render(entries))

    def _read_unlocked(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return self._render([])

    def read_document(self) -> str:
        with self._lock(exclusive=False):
            return self._read_unlocked()

    def replace_document(self, content: str) -> None:
        """Replace the document after validating its managed memory blocks."""
        content = (content or "").strip() + "\n"
        entries = self._parse(content)
        if content.count("<!-- memory:") != len(entries):
            raise ValueError("memory.md contains an invalid memory metadata block")
        with self._lock(exclusive=True):
            self._write_unlocked(content)
            self._sync_legacy(entries)

    def _write_unlocked(self, content: str) -> None:
        fd, tmp_name = tempfile.mkstemp(
            dir=self.path.parent,
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                tmp.write(content)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_name, self.path)
            dir_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    @staticmethod
    def _normalize_body(body: str) -> str:
        body = body.strip()
        if body.startswith("- "):
            body = body[2:]
        return body.strip()

    def _parse(self, document: str) -> list[dict]:
        entries = []
        for match in _ENTRY_RE.finditer(document):
            try:
                meta = json.loads(match.group("meta"))
            except json.JSONDecodeError:
                continue
            content = self._normalize_body(match.group("body"))
            if not content or "id" not in meta:
                continue
            entries.append({
                "id": int(meta["id"]),
                "content": content,
                "created_at": meta.get("created_at"),
                "source": meta.get("source") or "ai",
                "memory_type": meta.get("memory_type"),
                "valid_until": meta.get("valid_until"),
            })
        return entries

    @staticmethod
    def _is_active(entry: dict) -> bool:
        raw = entry.get("valid_until")
        if not raw:
            return True
        try:
            expiry = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            return expiry > datetime.now(timezone.utc)
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _entry_block(entry: dict) -> str:
        meta = {
            "id": int(entry["id"]),
            "source": entry.get("source") or "ai",
            "memory_type": entry.get("memory_type"),
            "valid_until": entry.get("valid_until"),
            "created_at": entry.get("created_at"),
        }
        encoded = json.dumps(meta, ensure_ascii=False, separators=(",", ":"))
        content = str(entry.get("content") or "").strip()
        return f"<!-- memory:{encoded} -->\n- {content}"

    def _render(self, entries: list[dict]) -> str:
        groups: dict[str, list[dict]] = {}
        for entry in entries:
            memory_type = (entry.get("memory_type") or "general").strip()
            groups.setdefault(memory_type, []).append(entry)

        lines = [
            "# User Memory",
            "",
            "> Canonical durable memory for Hiyori. Structured deadlines, todos,",
            "> reminders, timeline events, and raw conversations remain in SQLite.",
        ]
        emitted = set()
        for memory_type, title in _SECTION_ORDER:
            items = groups.get(memory_type, [])
            if not items:
                continue
            emitted.add(memory_type)
            lines.extend(["", f"## {title}", ""])
            lines.extend(self._entry_block(item) + "\n" for item in items)

        for memory_type in sorted(set(groups) - emitted):
            title = "Other" if memory_type == "general" else memory_type.replace("_", " ").title()
            lines.extend(["", f"## {title}", ""])
            lines.extend(self._entry_block(item) + "\n" for item in groups[memory_type])
        return "\n".join(lines).rstrip() + "\n"

    def list(self, *, include_expired: bool = False) -> list[dict]:
        entries = self._parse(self.read_document())
        if not include_expired:
            entries = [entry for entry in entries if self._is_active(entry)]
        return sorted(entries, key=lambda entry: entry["id"], reverse=True)

    def save(self, content: str, *, source: str = "ai",
             memory_type: str | None = None,
             valid_until: str | None = None) -> int:
        with self._lock(exclusive=True):
            entries = self._parse(self._read_unlocked())
            memory_id = max((entry["id"] for entry in entries), default=0) + 1
            entries.append({
                "id": memory_id,
                "content": content,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "source": source,
                "memory_type": memory_type,
                "valid_until": valid_until,
            })
            self._write_unlocked(self._render(entries))
            self._sync_legacy(entries)
            return memory_id

    def update(self, memory_id: int, **fields) -> None:
        allowed = {"content", "memory_type", "valid_until"}
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return
        with self._lock(exclusive=True):
            entries = self._parse(self._read_unlocked())
            entry = next((item for item in entries if item["id"] == memory_id), None)
            if entry is None:
                return
            entry.update(updates)
            self._write_unlocked(self._render(entries))
            self._sync_legacy(entries)

    def delete(self, memory_id: int) -> None:
        with self._lock(exclusive=True):
            entries = self._parse(self._read_unlocked())
            updated = [entry for entry in entries if entry["id"] != memory_id]
            if len(updated) == len(entries):
                return
            self._write_unlocked(self._render(updated))
            self._sync_legacy(updated)

    def prompt_markdown(self) -> str:
        """Return active Markdown constrained to whole entries within budget."""
        entries = [entry for entry in self._parse(self.read_document()) if self._is_active(entry)]
        full = self._strip_metadata(self._render(entries))
        if estimate_tokens(full) <= self.token_budget:
            return full

        notice = "\n\n> Some memories were omitted by the prompt token budget.\n"
        selected = []
        for entry in entries:
            candidate = self._strip_metadata(self._render([*selected, entry])) + notice
            if estimate_tokens(candidate) > self.token_budget:
                continue
            selected.append(entry)
        limited = self._strip_metadata(self._render(selected)).rstrip() + notice
        if estimate_tokens(limited) <= self.token_budget:
            return limited
        fallback = "# User Memory\n\n> Memories omitted by the prompt token budget.\n"
        return fallback if estimate_tokens(fallback) <= self.token_budget else ""

    @staticmethod
    def _strip_metadata(document: str) -> str:
        return _META_RE.sub("", document).strip()

    def stats(self) -> dict:
        prompt = self.prompt_markdown()
        return {
            "path": str(self.path),
            "token_estimate": estimate_tokens(prompt),
            "token_budget": self.token_budget,
            "total_entries": len(self.list(include_expired=True)),
            "active_entries": len(self.list()),
        }

    def _sync_legacy(self, entries: list[dict]) -> None:
        if self.legacy_repository is None:
            return
        try:
            self.legacy_repository.sync_memories_shadow(entries)
        except Exception as exc:
            logger.warning(
                "Failed to update legacy memories shadow: %s: %s",
                type(exc).__name__,
                exc,
            )
