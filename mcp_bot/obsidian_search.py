"""Obsidian vault 全文检索：search_notes / read_note 纯函数。"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_TAGS_INLINE_RE = re.compile(r"^tags:\s*\[(.*?)\]", re.MULTILINE)
_TAGS_LIST_RE = re.compile(r"^tags:\s*\n((?:\s*-\s*\S.*\n?)+)", re.MULTILINE)
_TAG_LIST_ITEM_RE = re.compile(r"^\s*-\s*(\S+)", re.MULTILINE)

_MAX_NOTE_CHARS = 3000
_SNIPPET_RADIUS = 100


def _parse_frontmatter_tags(text: str) -> list[str]:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return []
    fm = m.group(1)
    inline = _TAGS_INLINE_RE.search(fm)
    if inline:
        return [
            t.strip().strip('"').strip("'")
            for t in inline.group(1).split(",")
            if t.strip()
        ]
    block = _TAGS_LIST_RE.search(fm)
    if block:
        return [t.strip() for t in _TAG_LIST_ITEM_RE.findall(block.group(1))]
    return []


def _strip_frontmatter(text: str) -> str:
    m = _FRONTMATTER_RE.match(text)
    return text[m.end():] if m else text


def _make_snippet(content: str, query_lower: str) -> tuple[str, list[int]]:
    if not query_lower:
        return content[: _SNIPPET_RADIUS * 2], []
    lower = content.lower()
    idx = lower.find(query_lower)
    if idx < 0:
        return "", []
    start = max(0, idx - _SNIPPET_RADIUS)
    end = min(len(content), idx + len(query_lower) + _SNIPPET_RADIUS)
    snippet = content[start:end]
    if start > 0:
        snippet = "..." + snippet
    if end < len(content):
        snippet = snippet + "..."
    matched_lines: list[int] = []
    for n, line in enumerate(content.splitlines(), 1):
        if query_lower in line.lower():
            matched_lines.append(n)
            if len(matched_lines) >= 5:
                break
    return snippet, matched_lines


def search_notes(
    vault_path: str,
    query: str,
    folder: Optional[str] = None,
    tags: Optional[list[str]] = None,
    max_results: int = 8,
) -> list[dict]:
    """Search vault for notes matching query, with optional folder/tags filters."""
    if not vault_path:
        return []
    vault = Path(vault_path)
    if not vault.is_dir():
        return []

    query_lower = query.lower() if query else ""
    required_tags = set(tags) if tags else set()
    folder_norm = folder.strip("/").replace("\\", "/") if folder else None

    results: list[dict] = []
    for md_path in vault.rglob("*.md"):
        rel_parts = md_path.relative_to(vault).parts
        if any(part.startswith(".") for part in rel_parts):
            continue
        rel = md_path.relative_to(vault).as_posix()
        if folder_norm and not (rel == folder_norm or rel.startswith(folder_norm + "/")):
            continue
        try:
            text = md_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        note_tags = _parse_frontmatter_tags(text)
        if required_tags and not required_tags.issubset(set(note_tags)):
            continue

        body = _strip_frontmatter(text)
        body_lower = body.lower()
        title = md_path.stem
        title_hit = (query_lower in title.lower()) if query_lower else True
        body_hit = (query_lower in body_lower) if query_lower else True
        if query_lower and not (title_hit or body_hit):
            continue

        snippet, matched_lines = _make_snippet(body, query_lower)
        score = (2 if title_hit else 0) + (body_lower.count(query_lower) if query_lower else 0)
        results.append({
            "path": rel,
            "title": title,
            "tags": note_tags,
            "snippet": snippet,
            "matched_lines": matched_lines,
            "_score": score,
        })

    results.sort(key=lambda r: r["_score"], reverse=True)
    for r in results:
        r.pop("_score", None)
    return results[:max_results]


def read_note(vault_path: str, relative_path: str) -> dict:
    """Read full note content (truncated to 3000 chars). Returns error key on failure."""
    empty = {"path": relative_path, "title": "", "content": "", "tags": []}
    if not vault_path:
        return {**empty, "error": "vault_path not configured"}
    vault = Path(vault_path).resolve()
    target = (vault / relative_path).resolve()
    try:
        target.relative_to(vault)
    except ValueError:
        return {**empty, "error": "path escapes vault"}
    if not target.is_file():
        return {**empty, "error": "not found"}
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return {**empty, "error": str(e)}

    tags = _parse_frontmatter_tags(text)
    body = _strip_frontmatter(text)
    truncated = body[:_MAX_NOTE_CHARS]
    if len(body) > _MAX_NOTE_CHARS:
        truncated += f"\n\n[...truncated, total {len(body)} chars]"
    return {"path": relative_path, "title": target.stem, "content": truncated, "tags": tags}
