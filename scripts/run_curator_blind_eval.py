#!/usr/bin/env python3
"""Run and score an isolated, anonymized memory-curator evaluation."""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sqlite3
import string
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from bot import trace
from bot.database import Database
from bot.memory import curator_service
from bot.memory.curator import CURATOR_NAME
from bot.memory.personal_repository import (
    UNTARGETABLE_STATUSES,
    PersonalMemoryRepository,
)
from bot.timezone_state import init_timezone

init_timezone(config.TIMEZONE)

SCHEMA_VERSION = 1
HUMAN_FIELDS = {
    "factual_errors": (0, None),
    "unsafe_mutations": (0, None),
    "precision": (1, 5),
    "recall": (1, 5),
    "action_choice": (1, 5),
    "restraint": (1, 5),
}
HUMAN_WEIGHTS = {
    "precision": 0.35,
    "recall": 0.25,
    "action_choice": 0.20,
    "restraint": 0.20,
}


@dataclass(frozen=True)
class CandidateSpec:
    label: str
    preset_name: str
    batch_size: int


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _candidate_label(index: int) -> str:
    if index >= len(string.ascii_uppercase):
        raise ValueError("at most 26 blind candidates are supported")
    return f"Candidate {string.ascii_uppercase[index]}"


def build_candidate_specs(
    preset_names: list[str], batch_sizes: list[int], *, seed: int
) -> list[CandidateSpec]:
    combinations = [
        (preset_name, int(batch_size))
        for preset_name in preset_names
        for batch_size in batch_sizes
    ]
    random.Random(seed).shuffle(combinations)
    return [
        CandidateSpec(_candidate_label(index), preset_name, batch_size)
        for index, (preset_name, batch_size) in enumerate(combinations)
    ]


def _snapshot_database(source: Path, destination: Path) -> None:
    source_uri = f"file:{source.resolve()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as source_conn:
        with sqlite3.connect(destination) as destination_conn:
            source_conn.backup(destination_conn)


def _set_eval_cursor(db: Database, channel_id: str, start_after: int) -> None:
    conn = db._get_conn()
    conn.execute(
        """
        INSERT INTO curator_cursors
            (curator_name, channel_id, last_message_id,
             last_successful_run_id, updated_at)
        VALUES (?, ?, ?, NULL, datetime('now'))
        ON CONFLICT(curator_name, channel_id) DO UPDATE SET
            last_message_id = excluded.last_message_id,
            last_successful_run_id = NULL,
            updated_at = datetime('now')
        """,
        (CURATOR_NAME, str(channel_id), int(start_after)),
    )
    conn.commit()
    conn.close()


def load_baseline_memories(
    evaluation_dir: Path, preset_name: str, *, expected_upto: int
) -> list[dict]:
    answer_key_path = evaluation_dir / "answer-key.json"
    try:
        answer_key = json.loads(answer_key_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read baseline answer key: {exc}") from exc
    actual_upto = int(answer_key.get("range", {}).get("last_message_id", -1))
    if actual_upto != int(expected_upto):
        raise ValueError(
            f"baseline evaluation ends at {actual_upto}, expected {expected_upto}"
        )
    matches = [
        candidate for candidate in answer_key.get("candidates", [])
        if candidate.get("identity", {}).get("preset") == preset_name
    ]
    if len(matches) != 1:
        raise ValueError(
            f"baseline preset must match exactly one candidate: {preset_name}"
        )
    candidate = matches[0]
    automation = candidate.get("automation", {})
    if automation.get("completed_batches") != automation.get("expected_batches"):
        raise ValueError(f"baseline candidate is incomplete: {preset_name}")
    memories = candidate.get("final_memories")
    if not isinstance(memories, list) or not memories:
        raise ValueError(f"baseline candidate has no final memories: {preset_name}")
    return memories


def replace_baseline_memories(db: Database, memories: list[dict]) -> None:
    """Rebuild the evaluation-only active set with stable memory IDs."""
    conn = db._get_conn()
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM personal_memory_sources")
        conn.execute("DELETE FROM personal_memories")
        for memory in sorted(memories, key=lambda item: int(item["memory_id"])):
            memory_id = int(memory["memory_id"])
            conn.execute(
                """
                INSERT INTO personal_memories
                    (id, summary, reason, memory_type, status, curator_model)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    memory_id,
                    str(memory["summary"]),
                    "carried forward from reviewed blind evaluation",
                    str(memory["memory_type"]),
                    str(memory.get("status") or "active"),
                    "blind-eval-baseline",
                ),
            )
            for source in memory.get("sources", []):
                conn.execute(
                    """
                    INSERT INTO personal_memory_sources
                        (memory_id, conversation_message_id, quote, evidence_role)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        memory_id,
                        int(source["message_id"]),
                        source.get("quote"),
                        str(source["evidence_role"]),
                    ),
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _sanitize_memory(memory: dict) -> dict:
    return {
        "memory_id": int(memory["id"]),
        "summary": memory["summary"],
        "memory_type": memory["memory_type"],
        "status": memory["status"],
        "sources": [
            {
                "message_id": int(source["conversation_message_id"]),
                "quote": source.get("quote"),
                "evidence_role": source["evidence_role"],
            }
            for source in memory.get("sources", [])
        ],
    }


def _operation_with_evidence(operation: dict, content_by_id: dict[int, str]) -> dict:
    item = json.loads(json.dumps(operation, ensure_ascii=False))
    for source in item.get("sources", []):
        source["message_content"] = content_by_id.get(int(source["message_id"]), "")
    return item


async def _run_candidate(
    *, source_db: Path, temp_dir: Path, spec: CandidateSpec,
    channel_id: str, start_after: int, message_count: int,
    prompt_now: str,
) -> dict:
    candidate_db = temp_dir / f"candidate-{spec.label[-1]}.db"
    _snapshot_database(source_db, candidate_db)
    db = Database(str(candidate_db))
    repository = PersonalMemoryRepository(db)
    _set_eval_cursor(db, channel_id, start_after)

    preset = config.PRESETS[spec.preset_name]
    expected_batches = message_count // spec.batch_size
    batches = []
    error = None
    started = time.monotonic()
    for _ in range(expected_batches):
        try:
            result = await curator_service.propose_batch(
                db,
                repository,
                channel_id=channel_id,
                preset=preset,
                limit=spec.batch_size,
                prompt_now=prompt_now,
                repair=True,
                auto_apply=True,
            )
            batches.append(result)
        except Exception as exc:  # Keep other blind candidates runnable.
            error = f"{type(exc).__name__}: {exc}"
            break

    elapsed_seconds = round(time.monotonic() - started, 3)
    final_memories = [
        _sanitize_memory(memory) for memory in repository.list(exclude_statuses=UNTARGETABLE_STATUSES)
    ]
    operations = [
        operation
        for batch in batches
        for operation in batch.get("operations", [])
    ]
    return {
        "label": spec.label,
        "identity": {
            "preset": spec.preset_name,
            "model": preset.model,
            "provider": preset.provider,
            "batch_size": spec.batch_size,
        },
        "automation": {
            "expected_batches": expected_batches,
            "completed_batches": len(batches),
            "repaired_batches": sum(bool(batch.get("repaired")) for batch in batches),
            "failed_batches": expected_batches - len(batches),
            "elapsed_seconds": elapsed_seconds,
            "error": error,
        },
        "batches": batches,
        "operations": operations,
        "final_memories": final_memories,
    }


def _render_transcript(messages: list[dict]) -> str:
    lines = ["# Shared transcript", ""]
    for message in messages:
        role = str(message["role"]).upper()
        lines.extend([
            f"## Message {message['id']} - {role}",
            "",
            f"Time: `{message.get('created_at') or 'unknown'}`",
            "",
            str(message["content"]),
            "",
        ])
    return "\n".join(lines).rstrip() + "\n"


def _render_review(blind_payload: dict) -> str:
    evaluation = blind_payload["evaluation"]
    lines = [
        "# Curator blind review",
        "",
        f"Evaluation: `{evaluation['evaluation_id']}`",
        f"Shared range: messages {evaluation['first_message_id']} to "
        f"{evaluation['last_message_id']} ({evaluation['message_count']} total)",
        "",
        "Do not open `answer-key.json` before completing `scorecard.json`.",
        "Read `transcript.md`, then score every candidate independently. Empty output can "
        "be correct; do not reward operation count.",
        "Candidates without complete output are automatically disqualified; leave their "
        "scorecard fields as null.",
        "",
        "Hard failures:",
        "",
        "- `factual_errors`: summary claims not supported by the cited message/context.",
        "- `unsafe_mutations`: an unjustified update, supersede, or archive.",
        "",
        "Quality scores use 1 (poor) to 5 (excellent): precision, recall, action choice, "
        "and restraint.",
        "",
        "# Baseline active memories",
        "",
    ]
    for memory in blind_payload["baseline_memories"]:
        lines.append(
            f"- Memory {memory['memory_id']} [{memory['memory_type']}]: "
            f"{memory['summary']}"
        )

    for candidate in blind_payload["candidates"]:
        lines.extend(["", f"# {candidate['candidate']}", ""])
        if not candidate["output_available"]:
            lines.extend(["No complete output was available for this candidate.", ""])
        elif not candidate["operations"]:
            lines.extend(["This candidate proposed no operations.", ""])
        else:
            for index, operation in enumerate(candidate["operations"], start=1):
                target = (
                    f" memory_id={operation['memory_id']}"
                    if "memory_id" in operation else ""
                )
                lines.extend([
                    f"## Operation {index}: {operation['action']}{target}",
                    "",
                    f"- Summary: {operation.get('summary', '(unchanged)')}",
                    f"- Type: {operation.get('memory_type', '(unchanged)')}",
                    f"- Reason: {operation['reason']}",
                    "- Evidence:",
                ])
                for source in operation["sources"]:
                    lines.append(
                        f"  - Message {source['message_id']} "
                        f"[{source['evidence_role']}], quote: "
                        f"{source.get('quote') or '(none)'}"
                    )
                lines.append("")

        lines.extend(["## Final active memories", ""])
        for memory in candidate["final_active_memories"]:
            lines.append(
                f"- Memory {memory['memory_id']} [{memory['memory_type']}]: "
                f"{memory['summary']}"
            )
    return "\n".join(lines).rstrip() + "\n"


def build_scorecard(candidate_labels: list[str], evaluation_id: str) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "evaluation_id": evaluation_id,
        "instructions": {
            "counts": "factual_errors and unsafe_mutations are integers >= 0",
            "quality": "precision, recall, action_choice, restraint are integers 1-5",
            "incomplete": "leave all numeric fields null when review.md says output is incomplete",
            "notes": "free text; keep model guesses out until after scoring",
        },
        "scores": [
            {
                "candidate": label,
                "factual_errors": None,
                "unsafe_mutations": None,
                "precision": None,
                "recall": None,
                "action_choice": None,
                "restraint": None,
                "notes": "",
            }
            for label in candidate_labels
        ],
    }


def _assert_blind_payload(blind_payload: dict, answer_key: dict) -> None:
    rendered = json.dumps(blind_payload, ensure_ascii=False)
    forbidden = set()
    for candidate in answer_key["candidates"]:
        identity = candidate["identity"]
        forbidden.update((identity["preset"], identity["model"]))
    leaked = sorted(value for value in forbidden if value and value in rendered)
    if leaked:
        raise RuntimeError(f"blind payload leaks candidate identity: {leaked}")


async def run_evaluation(args) -> Path:
    source_db = Path(args.db)
    if not source_db.exists():
        raise ValueError(f"database does not exist: {source_db}")
    unknown = sorted(set(args.presets) - set(config.PRESETS))
    if unknown:
        raise ValueError(f"unknown presets: {unknown}")
    if len(set(args.presets)) != len(args.presets):
        raise ValueError("preset names must be unique")
    if len(set(args.batch_sizes)) != len(args.batch_sizes):
        raise ValueError("batch sizes must be unique")
    if any(size <= 0 or args.message_count % size for size in args.batch_sizes):
        raise ValueError("every batch size must be positive and divide message-count")

    with tempfile.TemporaryDirectory(prefix="curator-blind-eval-") as temp_name:
        temp_dir = Path(temp_name)
        # Host-side runs cannot append container-owned production trace files,
        # and evaluation traces are redundant with answer-key.json.
        trace._TRACE_DIR = temp_dir / "ai_traces"
        baseline_db = temp_dir / "baseline.db"
        _snapshot_database(source_db, baseline_db)
        db = Database(str(baseline_db))
        repository = PersonalMemoryRepository(db)
        baseline_source = None
        if args.baseline_eval:
            if not args.baseline_preset:
                raise ValueError("baseline-preset is required with baseline-eval")
            carried_memories = load_baseline_memories(
                Path(args.baseline_eval),
                args.baseline_preset,
                expected_upto=args.start_after,
            )
            replace_baseline_memories(db, carried_memories)
            baseline_source = {
                "evaluation_dir": str(Path(args.baseline_eval)),
                "preset": args.baseline_preset,
            }
        messages = db.get_conversation_messages_after(
            args.channel, args.start_after, limit=args.message_count)
        if len(messages) != args.message_count:
            raise ValueError(
                f"requested {args.message_count} messages after {args.start_after}, "
                f"found {len(messages)}"
            )
        max_source_id = 0
        conn = db._get_conn()
        row = conn.execute(
            "SELECT MAX(conversation_message_id) FROM personal_memory_sources"
        ).fetchone()
        conn.close()
        if row and row[0] is not None:
            max_source_id = int(row[0])
        if max_source_id > args.start_after:
            raise ValueError(
                "baseline memories contain evidence newer than start-after; "
                "choose a later boundary to avoid future-data leakage"
            )

        baseline_memories = [
            _sanitize_memory(memory) for memory in repository.list(exclude_statuses=UNTARGETABLE_STATUSES)
        ]
        prompt_now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        specs = build_candidate_specs(
            args.presets, args.batch_sizes, seed=args.seed)
        candidate_results = []
        for spec in specs:
            candidate_results.append(await _run_candidate(
                source_db=baseline_db,
                temp_dir=temp_dir,
                spec=spec,
                channel_id=args.channel,
                start_after=args.start_after,
                message_count=args.message_count,
                prompt_now=prompt_now,
            ))

    evaluation_id = (
        f"curator-blind-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-"
        f"{uuid.uuid4().hex[:6]}"
    )
    output_dir = Path(args.output_dir) if args.output_dir else (
        ROOT / "data" / "curator_blind_evals" / evaluation_id
    )
    output_dir.mkdir(parents=True, exist_ok=False)

    content_by_id = {int(message["id"]): message["content"] for message in messages}
    answer_key = {
        "schema_version": SCHEMA_VERSION,
        "evaluation_id": evaluation_id,
        "seed": args.seed,
        "prompt_now": prompt_now,
        "baseline_source": baseline_source,
        "range": {
            "channel_id": args.channel,
            "start_after": args.start_after,
            "first_message_id": int(messages[0]["id"]),
            "last_message_id": int(messages[-1]["id"]),
            "message_count": len(messages),
        },
        "candidates": candidate_results,
    }
    blind_payload = {
        "schema_version": SCHEMA_VERSION,
        "evaluation": {
            "evaluation_id": evaluation_id,
            "first_message_id": int(messages[0]["id"]),
            "last_message_id": int(messages[-1]["id"]),
            "message_count": len(messages),
        },
        "baseline_memories": baseline_memories,
        "candidates": [
            {
                "candidate": candidate["label"],
                "output_available": candidate["automation"]["failed_batches"] == 0,
                "operations": [
                    _operation_with_evidence(operation, content_by_id)
                    for operation in candidate["operations"]
                ],
                "final_active_memories": candidate["final_memories"],
            }
            for candidate in candidate_results
        ],
    }
    _assert_blind_payload(blind_payload, answer_key)

    _write_json(output_dir / "blind-review.json", blind_payload)
    (output_dir / "review.md").write_text(
        _render_review(blind_payload), encoding="utf-8")
    (output_dir / "transcript.md").write_text(
        _render_transcript(messages), encoding="utf-8")
    _write_json(
        output_dir / "scorecard.json",
        build_scorecard([spec.label for spec in specs], evaluation_id),
    )
    _write_json(output_dir / "answer-key.json", answer_key)
    return output_dir


def _validated_score(row: dict) -> dict:
    values = {}
    for field, (minimum, maximum) in HUMAN_FIELDS.items():
        value = row.get(field)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{row.get('candidate')} {field} must be an integer")
        if value < minimum or (maximum is not None and value > maximum):
            raise ValueError(
                f"{row.get('candidate')} {field} must be within "
                f"[{minimum}, {maximum if maximum is not None else 'infinity'}]"
            )
        values[field] = value
    return values


def score_evaluation(directory: Path) -> Path:
    scorecard = json.loads((directory / "scorecard.json").read_text(encoding="utf-8"))
    answer_key = json.loads((directory / "answer-key.json").read_text(encoding="utf-8"))
    if scorecard.get("evaluation_id") != answer_key.get("evaluation_id"):
        raise ValueError("scorecard and answer key evaluation IDs do not match")
    key_by_label = {item["label"]: item for item in answer_key["candidates"]}
    rows = []
    for score_row in scorecard["scores"]:
        label = score_row["candidate"]
        if label not in key_by_label:
            raise ValueError(f"unknown candidate in scorecard: {label}")
        candidate = key_by_label[label]
        automation = candidate["automation"]
        raw_compliance = (
            (automation["completed_batches"] - automation["repaired_batches"])
            / automation["expected_batches"] * 100
        )
        completion = (
            automation["completed_batches"] / automation["expected_batches"] * 100
        )
        automation_incomplete = (
            automation["completed_batches"] < automation["expected_batches"]
        )
        if automation_incomplete:
            values = {field: None for field in HUMAN_FIELDS}
            human_quality = None
            disqualification_reasons = ["automation_incomplete"]
            total = None
        else:
            values = _validated_score(score_row)
            human_quality = sum(
                values[field] * weight for field, weight in HUMAN_WEIGHTS.items()
            ) / 5 * 100
            disqualification_reasons = []
            if values["factual_errors"]:
                disqualification_reasons.append("factual_errors")
            if values["unsafe_mutations"]:
                disqualification_reasons.append("unsafe_mutations")
            total = (
                None if disqualification_reasons else
                human_quality * 0.85 + raw_compliance * 0.10 + completion * 0.05
            )
        rows.append({
            "candidate": label,
            **candidate["identity"],
            **values,
            "notes": score_row.get("notes", ""),
            "human_quality": (
                round(human_quality, 2) if human_quality is not None else None
            ),
            "raw_compliance": round(raw_compliance, 2),
            "completion": round(completion, 2),
            "elapsed_seconds": automation["elapsed_seconds"],
            "disqualified": bool(disqualification_reasons),
            "disqualification_reasons": disqualification_reasons,
            "total_score": round(total, 2) if total is not None else None,
        })
    rows.sort(key=lambda row: (
        row["disqualified"],
        -(row["total_score"] if row["total_score"] is not None else -1),
        row["elapsed_seconds"],
    ))
    result = {
        "schema_version": SCHEMA_VERSION,
        "evaluation_id": answer_key["evaluation_id"],
        "formula": "85% human quality + 10% raw JSON compliance + 5% completion; incomplete automation, factual errors, or unsafe mutations disqualify",
        "ranking": rows,
    }
    result_path = directory / "results.json"
    _write_json(result_path, result)
    return result_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="generate a new blind evaluation")
    run_parser.add_argument("--db", default=config.DB_PATH)
    run_parser.add_argument("--channel", default=str(config.CHANNEL_ID))
    run_parser.add_argument("--start-after", type=int, required=True)
    run_parser.add_argument("--message-count", type=int, default=100)
    run_parser.add_argument("--presets", nargs="+", required=True)
    run_parser.add_argument("--batch-sizes", nargs="+", type=int, default=[20, 50])
    run_parser.add_argument("--seed", type=int, default=20260720)
    run_parser.add_argument("--output-dir")
    run_parser.add_argument(
        "--baseline-eval",
        help="Prior evaluation directory whose reviewed candidate state is carried forward",
    )
    run_parser.add_argument(
        "--baseline-preset",
        help="Preset identity to select from --baseline-eval answer-key.json",
    )

    score_parser = subparsers.add_parser("score", help="reveal and score a completed evaluation")
    score_parser.add_argument("directory", type=Path)

    args = parser.parse_args()
    if args.command == "run":
        if args.start_after < 0 or args.message_count <= 0:
            parser.error("start-after must be >= 0 and message-count must be > 0")
        output_dir = asyncio.run(run_evaluation(args))
        print(output_dir)
    else:
        print(score_evaluation(args.directory))


if __name__ == "__main__":
    main()
