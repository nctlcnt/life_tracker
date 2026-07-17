"""Export the legacy memories table to the canonical Markdown repository."""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bot.database import Database
from bot.memory import MarkdownMemoryRepository


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/life_tracker.db")
    parser.add_argument("--output", default="data/memory.md")
    parser.add_argument("--token-budget", type=int, default=4000)
    args = parser.parse_args()

    if os.path.exists(args.output):
        print(f"已存在，未覆盖: {os.path.abspath(args.output)}")
        return 1

    db = Database(args.db)
    repository = MarkdownMemoryRepository(
        args.output,
        legacy_repository=db,
        token_budget=args.token_budget,
    )
    stats = repository.stats()
    print(
        f"迁移完成: {stats['total_entries']} 条 -> {os.path.abspath(args.output)} "
        f"(prompt 估算 {stats['token_estimate']}/{stats['token_budget']} tokens)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
