"""Helpers for importing/exporting DB-managed prompt sections."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bot.prompts import (
    LEGACY_STRUCTURED_KEYS,
    PROMPT_SECTION_LABELS,
    synthesize_main_template,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROMPTS_PATH = REPO_ROOT / "docs" / "default-prompts.json"


class PromptSeedError(ValueError):
    """Raised when a prompt import/export file is invalid."""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_prompt_file(path: str | os.PathLike[str]) -> dict[str, str]:
    """Load a prompt JSON file and return normalized section values."""
    p = Path(path)
    with p.open(encoding="utf-8") as f:
        raw: Any = json.load(f)

    sections = raw.get("sections") if isinstance(raw, dict) else None
    if not isinstance(sections, dict):
        raise PromptSeedError(f"{p} must contain a top-level object field named 'sections'")

    unknown = sorted(set(sections) - set(PROMPT_SECTION_LABELS))
    if unknown:
        raise PromptSeedError(f"{p} contains unknown prompt section(s): {', '.join(unknown)}")

    # missing key 不再报错（LT-129 新增 main_template 后，历史备份 JSON 缺它
    # 属于正常情况）：缺的填空串，启动时的迁移钩子会自动合成
    normalized: dict[str, str] = {}
    for key in PROMPT_SECTION_LABELS:
        value = sections.get(key, "")
        if not isinstance(value, str):
            raise PromptSeedError(f"{p} section '{key}' must be a string")
        normalized[key] = value.strip()
    return normalized


def export_prompt_file(
    sections: dict[str, str],
    path: str | os.PathLike[str],
    *,
    kind: str = "private-backup",
) -> Path:
    """Write prompt sections to JSON."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "kind": kind,
        "exported_at": utc_now_iso(),
        "sections": {key: (sections.get(key) or "") for key in PROMPT_SECTION_LABELS},
    }
    with p.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return p


def apply_prompt_sections(
    conn: sqlite3.Connection,
    sections: dict[str, str],
    *,
    overwrite: bool,
) -> int:
    """Apply prompt sections to an open DB connection. Returns changed row count."""
    changed = 0
    for key, label in PROMPT_SECTION_LABELS.items():
        value = (sections.get(key) or "").strip()
        if not overwrite:
            row = conn.execute(
                "SELECT value FROM prompt_sections WHERE key = ?",
                (key,),
            ).fetchone()
            if row and (row["value"] or "").strip():
                continue

        conn.execute(
            """
            INSERT INTO prompt_sections (key, label, value, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(key) DO UPDATE SET
                label = excluded.label,
                value = excluded.value,
                updated_at = datetime('now')
            """,
            (key, label, value),
        )
        changed += 1
    return changed


def initialize_prompts_if_empty(
    conn: sqlite3.Connection,
    prompt_path: Path = DEFAULT_PROMPTS_PATH,
) -> bool:
    """Load default prompts when all known prompt sections are empty."""
    if not prompt_path.exists():
        return False

    row = conn.execute(
        """
        SELECT COUNT(*) AS filled
        FROM prompt_sections
        WHERE key IN ({})
          AND TRIM(value) != ''
        """.format(",".join("?" for _ in PROMPT_SECTION_LABELS)),
        tuple(PROMPT_SECTION_LABELS),
    ).fetchone()
    if row and row["filled"] > 0:
        return False

    sections = load_prompt_file(prompt_path)
    apply_prompt_sections(conn, sections, overwrite=True)
    return True


def migrate_main_template_if_missing(conn: sqlite3.Connection) -> bool:
    """LT-129 迁移：main_template 为空且旧结构化 section 有内容时，
    用 DB 里的实际内容合成等价模板写入。

    幂等；唯一写操作是 upsert main_template 一行，旧 section 行原样保留
    （UI 隐藏但 API 可读写），git revert 代码即可完整回滚。
    fresh DB 不走这里：initialize_prompts_if_empty 已灌入含 main_template
    的默认集，本函数因 main_template 非空直接返回。
    """
    row = conn.execute(
        "SELECT value FROM prompt_sections WHERE key = 'main_template'"
    ).fetchone()
    if row and (row[0] or "").strip():
        return False

    legacy: dict[str, str] = {}
    for key in (*LEGACY_STRUCTURED_KEYS, "tools"):
        r = conn.execute(
            "SELECT value FROM prompt_sections WHERE key = ?", (key,)
        ).fetchone()
        legacy[key] = (r[0] if r else "") or ""
    if not any(v.strip() for v in legacy.values()):
        return False  # 全空：没有可迁移的内容（理论上被 initialize 挡住了）

    template = synthesize_main_template(legacy)
    conn.execute(
        """
        INSERT INTO prompt_sections (key, label, value, updated_at)
        VALUES ('main_template', ?, ?, datetime('now'))
        ON CONFLICT(key) DO UPDATE SET
            label = excluded.label,
            value = excluded.value,
            updated_at = datetime('now')
        """,
        (PROMPT_SECTION_LABELS["main_template"], template),
    )
    return True
