import json

import pytest

from scripts.run_curator_blind_eval import (
    _assert_blind_payload,
    build_candidate_specs,
    build_scorecard,
    load_baseline_memories,
    replace_baseline_memories,
    score_evaluation,
)
from bot.database import Database
from bot.memory.personal_repository import PersonalMemoryRepository


def test_candidate_mapping_is_deterministic_and_complete():
    first = build_candidate_specs(["main", "fallback"], [20, 50], seed=7)
    second = build_candidate_specs(["main", "fallback"], [20, 50], seed=7)

    assert first == second
    assert [spec.label for spec in first] == [
        "Candidate A", "Candidate B", "Candidate C", "Candidate D"]
    assert {(spec.preset_name, spec.batch_size) for spec in first} == {
        ("main", 20), ("main", 50), ("fallback", 20), ("fallback", 50)}


def test_blind_payload_rejects_identity_leaks():
    answer_key = {"candidates": [{
        "identity": {"preset": "secret-preset", "model": "secret-model", "provider": "relay"}
    }]}
    safe = {"candidates": [{"candidate": "Candidate A", "operations": []}]}
    _assert_blind_payload(safe, answer_key)

    with pytest.raises(RuntimeError, match="leaks candidate identity"):
        _assert_blind_payload({**safe, "note": "secret-model"}, answer_key)


def test_score_evaluation_applies_hard_failure_and_ranking(tmp_path):
    evaluation_id = "eval-1"
    scorecard = build_scorecard(
        ["Candidate A", "Candidate B", "Candidate C"], evaluation_id)
    scorecard["scores"][0].update({
        "factual_errors": 0,
        "unsafe_mutations": 0,
        "precision": 5,
        "recall": 4,
        "action_choice": 5,
        "restraint": 5,
    })
    scorecard["scores"][1].update({
        "factual_errors": 1,
        "unsafe_mutations": 0,
        "precision": 5,
        "recall": 5,
        "action_choice": 5,
        "restraint": 5,
    })
    answer_key = {
        "evaluation_id": evaluation_id,
        "candidates": [
            {
                "label": "Candidate A",
                "identity": {"preset": "p1", "model": "m1", "provider": "one", "batch_size": 20},
                "automation": {
                    "expected_batches": 5, "completed_batches": 5,
                    "repaired_batches": 1, "elapsed_seconds": 8.0,
                },
            },
            {
                "label": "Candidate B",
                "identity": {"preset": "p2", "model": "m2", "provider": "two", "batch_size": 50},
                "automation": {
                    "expected_batches": 2, "completed_batches": 2,
                    "repaired_batches": 0, "elapsed_seconds": 4.0,
                },
            },
            {
                "label": "Candidate C",
                "identity": {"preset": "p3", "model": "m3", "provider": "three", "batch_size": 20},
                "automation": {
                    "expected_batches": 5, "completed_batches": 2,
                    "repaired_batches": 0, "elapsed_seconds": 6.0,
                },
            },
        ],
    }
    (tmp_path / "scorecard.json").write_text(json.dumps(scorecard), encoding="utf-8")
    (tmp_path / "answer-key.json").write_text(json.dumps(answer_key), encoding="utf-8")

    result_path = score_evaluation(tmp_path)
    result = json.loads(result_path.read_text(encoding="utf-8"))

    assert result["ranking"][0]["candidate"] == "Candidate A"
    assert result["ranking"][0]["raw_compliance"] == 80.0
    assert result["ranking"][1]["candidate"] == "Candidate B"
    assert result["ranking"][1]["disqualified"] is True
    assert result["ranking"][1]["disqualification_reasons"] == ["factual_errors"]
    assert result["ranking"][2]["candidate"] == "Candidate C"
    assert result["ranking"][2]["disqualification_reasons"] == ["automation_incomplete"]


def test_carry_forward_loads_complete_candidate_and_rebuilds_ids(tmp_path):
    evaluation_dir = tmp_path / "prior"
    evaluation_dir.mkdir()
    answer_key = {
        "range": {"last_message_id": 10},
        "candidates": [{
            "identity": {"preset": "winner"},
            "automation": {"expected_batches": 1, "completed_batches": 1},
            "final_memories": [{
                "memory_id": 7,
                "summary": "用户希望减少提醒频率",
                "memory_type": "interaction_style",
                "status": "hypothesis",
                "sources": [{
                    "message_id": 10,
                    "quote": "少提醒一点",
                    "evidence_role": "supports",
                }],
            }],
        }],
    }
    (evaluation_dir / "answer-key.json").write_text(
        json.dumps(answer_key, ensure_ascii=False), encoding="utf-8")
    memories = load_baseline_memories(
        evaluation_dir, "winner", expected_upto=10)

    db = Database(str(tmp_path / "eval.db"))
    db.add_conversation_message(
        discord_message_id="m10", channel_id="channel", role="user",
        content="少提醒一点", created_at="2026-01-01T00:00:00+00:00")
    conn = db._get_conn()
    conn.execute("UPDATE conversation_messages SET id = 10")
    conn.commit()
    conn.close()
    replace_baseline_memories(db, memories)

    loaded = PersonalMemoryRepository(db).list(status="hypothesis")
    assert [(item["id"], item["summary"]) for item in loaded] == [
        (7, "用户希望减少提醒频率")]
    assert loaded[0]["source_message_ids"] == [10]


def test_carry_forward_rejects_non_contiguous_or_incomplete_baseline(tmp_path):
    evaluation_dir = tmp_path / "prior"
    evaluation_dir.mkdir()
    answer_key = {
        "range": {"last_message_id": 9},
        "candidates": [{
            "identity": {"preset": "winner"},
            "automation": {"expected_batches": 2, "completed_batches": 1},
            "final_memories": [{}],
        }],
    }
    (evaluation_dir / "answer-key.json").write_text(
        json.dumps(answer_key), encoding="utf-8")

    with pytest.raises(ValueError, match="ends at 9"):
        load_baseline_memories(evaluation_dir, "winner", expected_upto=10)
    answer_key["range"]["last_message_id"] = 10
    (evaluation_dir / "answer-key.json").write_text(
        json.dumps(answer_key), encoding="utf-8")
    with pytest.raises(ValueError, match="incomplete"):
        load_baseline_memories(evaluation_dir, "winner", expected_upto=10)
