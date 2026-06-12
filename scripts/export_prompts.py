"""Export DB-managed prompt sections to a local JSON backup.

Usage:
    python -m scripts.export_prompts
    python -m scripts.export_prompts --db data-dev/life_tracker.db --out data-dev/backups/prompts/prompts.json
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from bot.prompt_store import export_prompt_file, utc_now_iso


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = REPO_ROOT / "data" / "life_tracker.db"
DEFAULT_BACKUP_DIR = REPO_ROOT / "data" / "backups" / "prompts"


def default_output_path() -> Path:
    stamp = utc_now_iso().replace(":", "").replace("-", "").replace("Z", "Z")
    return DEFAULT_BACKUP_DIR / f"prompts-{stamp}.json"


def main() -> None:
    parser = argparse.ArgumentParser(description="Export prompt_sections to JSON.")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite DB path")
    parser.add_argument("--out", default=None, help="Output JSON path")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        raise SystemExit(f"database not found: {db_path}")

    out_path = Path(args.out) if args.out else default_output_path()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT key, value FROM prompt_sections ORDER BY key").fetchall()
    conn.close()

    sections = {row["key"]: row["value"] for row in rows}
    export_prompt_file(sections, out_path)
    print(f"Exported prompts to {out_path}")


if __name__ == "__main__":
    main()
