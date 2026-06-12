"""Import prompt sections from JSON into SQLite.

Usage:
    python -m scripts.import_prompts docs/default-prompts.json --apply
    python -m scripts.import_prompts backups/prompts/prompts-YYYYMMDD.json --apply
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
from pathlib import Path

from bot.prompt_store import apply_prompt_sections, load_prompt_file


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = REPO_ROOT / "data" / "life_tracker.db"


def current_sections(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute("SELECT key, value FROM prompt_sections ORDER BY key").fetchall()
    return {row["key"]: row["value"] for row in rows}


def main() -> None:
    parser = argparse.ArgumentParser(description="Import JSON prompt sections into prompt_sections.")
    parser.add_argument("json_path", help="Prompt JSON path")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite DB path")
    parser.add_argument("--apply", action="store_true", help="Write changes. Without this, only preview.")
    parser.add_argument("--no-backup", action="store_true", help="Skip DB file backup before applying.")
    args = parser.parse_args()

    json_path = Path(args.json_path)
    db_path = Path(args.db)
    if not json_path.exists():
        raise SystemExit(f"prompt JSON not found: {json_path}")
    if not db_path.exists():
        raise SystemExit(f"database not found: {db_path}")

    incoming = load_prompt_file(json_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    existing = current_sections(conn)
    changed_keys = [
        key for key, value in incoming.items()
        if (existing.get(key) or "").strip() != value.strip()
    ]

    print(f"Prompt file: {json_path}")
    print(f"Database: {db_path}")
    print(f"Changed sections: {len(changed_keys)}")
    for key in changed_keys:
        old_len = len(existing.get(key) or "")
        new_len = len(incoming[key])
        print(f"  - {key}: {old_len} chars -> {new_len} chars")

    if not args.apply:
        print("\nPreview only. Re-run with --apply to import.")
        conn.close()
        return

    if not args.no_backup:
        backup_path = db_path.with_suffix(db_path.suffix + ".bak")
        shutil.copy2(db_path, backup_path)
        print(f"Backed up DB to {backup_path}")

    changed = apply_prompt_sections(conn, incoming, overwrite=True)
    conn.commit()
    conn.close()
    print(f"Imported {changed} prompt sections.")


if __name__ == "__main__":
    main()
