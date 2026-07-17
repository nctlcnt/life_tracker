"""Inspect or atomically rebuild one channel's compact summary from raw history."""
import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config
from bot.database import Database
from bot.memory.compact import rebuild_compact_summary
from bot.memory.context_window import load_summary_state
from bot.memory.markdown_repository import estimate_tokens


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=config.DB_PATH, help="SQLite database path")
    parser.add_argument("--channel", default=str(config.CHANNEL_ID))
    parser.add_argument("--upto-id", type=int, required=True)
    parser.add_argument(
        "--apply", action="store_true",
        help="Generate and atomically install the rebuilt summary",
    )
    args = parser.parse_args()

    db = Database(args.db)
    before = load_summary_state(db, args.channel)
    messages = db.get_ai_messages_after(args.channel, 0, upto_id=args.upto_id)
    continuous = bool(messages) and messages[-1]["id"] == args.upto_id
    token_estimate = sum(estimate_tokens(m["content"]) + 4 for m in messages)
    print(
        f"channel={args.channel} target={args.upto_id} rows={len(messages)} "
        f"tokens~{token_estimate} target_exists={continuous} "
        f"current_upto={int(before.get('upto_message_id', 0)) if before else 0}"
    )
    if not continuous:
        print("Refusing: target id is not a message in this channel.")
        return 2
    if not args.apply:
        print("Dry run only. Re-run with --apply after backing up the database.")
        return 0

    ok = asyncio.run(rebuild_compact_summary(db, args.channel, args.upto_id))
    after = load_summary_state(db, args.channel)
    print(
        f"rebuilt={ok} new_upto="
        f"{int(after.get('upto_message_id', 0)) if after else 0}"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
