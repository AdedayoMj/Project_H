from __future__ import annotations

import json
from pathlib import Path

OUTPUT_PATH = Path("/app/output.json")
INPUT_PATH = Path("/app/input")
GOLDEN_INPUT_PATH = Path(__file__).parent / "golden_input"
EXPECTED_PATH = Path(__file__).parent / "expected_output.json"

REPLY_CATEGORIES = {"ACK", "DECLINE", "PROPOSE_ALTERNATE", "REQUEST_INFO", "ESCALATE"}
SCHEDULE_KEYS = {"occurrence_id", "event_id", "calendar", "venue", "start_utc", "end_utc"}
TOP_LEVEL_KEYS = {"final_schedule", "moved_items", "deferred_items", "reply_categories", "objective_score"}
OBJECTIVE_KEYS = {"priority_score", "lateness_minutes", "moved_count", "travel_minutes"}


def _load_output() -> dict:
    return json.loads(OUTPUT_PATH.read_text())


def _load_expected() -> dict:
    return json.loads(EXPECTED_PATH.read_text())


def _schedule_key(entry: dict) -> tuple:
    return (
        entry["occurrence_id"],
        entry["event_id"],
        entry["calendar"],
        entry["venue"],
        entry["start_utc"],
        entry["end_utc"],
    )


def test_output_file_exists():
    """The agent must write a single, non-symlinked JSON object to /app/output.json."""
    assert OUTPUT_PATH.exists()
    assert not OUTPUT_PATH.is_symlink()
    data = _load_output()
    assert isinstance(data, dict)


def test_input_files_unchanged():
    """Nothing under /app/input/ may be modified, added, or removed."""
    golden_files = sorted(p.relative_to(GOLDEN_INPUT_PATH) for p in GOLDEN_INPUT_PATH.rglob("*") if p.is_file())
    actual_files = sorted(p.relative_to(INPUT_PATH) for p in INPUT_PATH.rglob("*") if p.is_file())
    assert actual_files == golden_files
    for rel in golden_files:
        assert (INPUT_PATH / rel).read_bytes() == (GOLDEN_INPUT_PATH / rel).read_bytes()


def test_output_top_level_schema():
    """output.json has exactly the five required top-level keys, correctly typed."""
    data = _load_output()
    assert set(data) == TOP_LEVEL_KEYS
    assert isinstance(data["final_schedule"], list)
    assert isinstance(data["moved_items"], list)
    assert isinstance(data["deferred_items"], list)
    assert isinstance(data["reply_categories"], dict)
    assert isinstance(data["objective_score"], dict)


def test_final_schedule_entries_well_formed():
    """Every final_schedule entry has exactly the required fields and a legal category-free shape."""
    data = _load_output()
    for entry in data["final_schedule"]:
        assert isinstance(entry, dict)
        assert set(entry) == SCHEDULE_KEYS
        assert entry["calendar"] in {"executive", "operations", "travel"}
        for key in ("start_utc", "end_utc"):
            assert isinstance(entry[key], str) and entry[key].endswith("Z")
    occurrence_ids = [entry["occurrence_id"] for entry in data["final_schedule"]]
    assert len(occurrence_ids) == len(set(occurrence_ids))


def test_every_occurrence_appears_exactly_once():
    """Every occurrence must appear in exactly one of final_schedule or deferred_items."""
    data = _load_output()
    scheduled_ids = [entry["occurrence_id"] for entry in data["final_schedule"]]
    deferred_ids = list(data["deferred_items"])
    assert len(scheduled_ids) == len(set(scheduled_ids))
    assert len(deferred_ids) == len(set(deferred_ids))
    assert set(scheduled_ids).isdisjoint(deferred_ids)

    expected = _load_expected()
    expected_universe = {e["occurrence_id"] for e in expected["final_schedule"]} | set(expected["deferred_items"])
    assert set(scheduled_ids) | set(deferred_ids) == expected_universe


def test_reply_categories_values_are_legal():
    """Every reply_categories value is one of the five fixed category labels."""
    data = _load_output()
    assert all(v in REPLY_CATEGORIES for v in data["reply_categories"].values())


def test_final_schedule_matches_reference():
    """final_schedule must equal the unique optimal schedule (as an unordered set of occurrences)."""
    data = _load_output()
    expected = _load_expected()
    got = sorted(_schedule_key(e) for e in data["final_schedule"])
    want = sorted(_schedule_key(e) for e in expected["final_schedule"])
    assert got == want


def test_moved_items_matches_reference():
    """moved_items must equal the reference set of occurrence_ids that were relocated."""
    data = _load_output()
    expected = _load_expected()
    assert sorted(data["moved_items"]) == sorted(expected["moved_items"])


def test_deferred_items_matches_reference():
    """deferred_items must equal the reference set of occurrence_ids left out of the schedule."""
    data = _load_output()
    expected = _load_expected()
    assert sorted(data["deferred_items"]) == sorted(expected["deferred_items"])


def test_reply_categories_matches_reference():
    """reply_categories must classify every message exactly as the deterministic rule order dictates."""
    data = _load_output()
    expected = _load_expected()
    assert data["reply_categories"] == expected["reply_categories"]


def test_objective_score_matches_reference():
    """objective_score must equal the reference values for the optimal schedule, exactly."""
    data = _load_output()
    expected = _load_expected()
    assert set(data["objective_score"]) == OBJECTIVE_KEYS
    assert data["objective_score"] == expected["objective_score"]
