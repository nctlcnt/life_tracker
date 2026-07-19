#!/usr/bin/env python3
"""Run one manual memory-curator batch; default is proposal-only."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from bot.timezone_state import init_timezone

# docker exec 单独起进程时容器默认 UTC；与 bot 主进程同钟，
# trace/proposal 里的时间戳才能和其他条目对得上
init_timezone(config.TIMEZONE)

from bot.database import Database
from bot.memory import curator_service
from bot.memory.curator import CURATOR_NAME, parse_curator_batch
from bot.memory.personal_repository import PersonalMemoryRepository


def _preset(name: str | None):
    if name:
        if name not in config.PRESETS:
            raise ValueError(f"unknown preset: {name}")
        return config.PRESETS[name]
    return config.get_active()


def _load_proposal(path: str) -> dict:
    try:
        proposal = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read proposal file: {exc}") from exc
    if not isinstance(proposal, dict):
        raise ValueError("proposal file must contain a JSON object")
    required = {
        "run_id", "curator", "channel_id", "after_message_id",
        "upto_message_id", "operations",
    }
    missing = sorted(required - set(proposal))
    if missing:
        raise ValueError(f"proposal file missing fields: {missing}")
    return proposal


def apply_proposal(args) -> dict:
    db = Database(args.db)
    repository = PersonalMemoryRepository(db)
    proposal = _load_proposal(args.apply_proposal)
    if str(proposal["channel_id"]) != str(args.channel):
        raise ValueError("proposal channel does not match --channel")
    if proposal["curator"] != args.curator_name:
        raise ValueError("proposal curator does not match --curator-name")
    after_id = int(proposal["after_message_id"])
    upto_id = int(proposal["upto_message_id"])
    messages = db.get_conversation_messages_after(
        str(args.channel), after_id, upto_id=upto_id)
    batch = parse_curator_batch({"operations": proposal["operations"]})
    visible_ids = {int(message["id"]) for message in messages}
    result = repository.apply_curator_batch(
        batch,
        curator_name=args.curator_name,
        channel_id=args.channel,
        after_message_id=after_id,
        upto_message_id=upto_id,
        visible_message_ids=visible_ids,
        run_id=str(proposal["run_id"]),
        curator_model=str(proposal.get("model") or "unknown"),
    )
    return {**proposal, "mode": "apply", "apply_result": result}


async def propose(args) -> dict:
    db = Database(args.db)
    repository = PersonalMemoryRepository(db)
    return await curator_service.propose_batch(
        db, repository,
        channel_id=args.channel,
        preset=_preset(args.preset),
        curator_name=args.curator_name,
        limit=args.limit,
        max_output_tokens=args.max_output_tokens,
        request_timeout=args.request_timeout,
        repair=not args.no_repair,
    )


async def run(args) -> dict:
    if args.apply_proposal:
        return apply_proposal(args)
    return await propose(args)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=config.DB_PATH)
    parser.add_argument("--channel", default=str(config.CHANNEL_ID))
    parser.add_argument("--curator-name", default=CURATOR_NAME)
    parser.add_argument("--preset", help="Preset name; default is active preset")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument(
        "--max-output-tokens", type=int, default=16384,
        help="Completion budget; reasoning tokens count against it, so "
             "curator batches need far more than the 4096 chat default.",
    )
    parser.add_argument(
        "--request-timeout", type=float, default=600.0,
        help="Per-request timeout in seconds; reasoning batches routinely "
             "outlive the 120s chat default.",
    )
    parser.add_argument(
        "--no-repair", action="store_true",
        help="Disable the one-shot repair round after a failed validation "
             "(useful for measuring raw model compliance).",
    )
    parser.add_argument(
        "--apply-proposal", metavar="FILE",
        help="Apply an exact reviewed dry-run JSON file; never calls the model.",
    )
    parser.add_argument(
        "--output", metavar="FILE",
        help="Also write the dry-run proposal or apply receipt to this JSON file.",
    )
    args = parser.parse_args()
    if args.limit <= 0:
        parser.error("--limit must be positive")
    if args.max_output_tokens <= 0:
        parser.error("--max-output-tokens must be positive")
    if args.request_timeout <= 0:
        parser.error("--request-timeout must be positive")
    result = asyncio.run(run(args))
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
