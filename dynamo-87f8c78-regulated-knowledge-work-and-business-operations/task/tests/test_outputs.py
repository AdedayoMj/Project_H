from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

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


def test_weighted_selection_portfolios_satisfied():
    """Every disclosed cross-date weighted portfolio meets its unit floor and cap."""
    data = _load_output()
    policy = json.loads((INPUT_PATH / "policy_notes.json").read_text())
    for rule in policy["weighted_selection_portfolios"]:
        units = sum(
            rule["event_units"].get(entry["event_id"], 0)
            for entry in data["final_schedule"]
        )
        assert rule["minimum_units"] <= units <= rule["maximum_units"]


def test_placement_sensitive_rules_satisfied():
    """Placement portfolios and conditional slot commitments use each chosen distinct rank."""
    data = _load_output()
    policy = json.loads((INPUT_PATH / "policy_notes.json").read_text())
    events = {}
    for calendar_path in (INPUT_PATH / "calendars").glob("*.json"):
        for event in json.loads(calendar_path.read_text())["events"]:
            events[event["event_id"]] = event

    chosen_ranks = {}
    for entry in data["final_schedule"]:
        event = events[entry["event_id"]]
        local_start = (
            datetime.fromisoformat(entry["start_utc"].replace("Z", "+00:00"))
            .astimezone(ZoneInfo(event["timezone"]))
            .strftime("%Y-%m-%dT%H:%M:%S")
        )
        occurrence_original = entry["occurrence_id"].split("@", 1)[1]
        raw_options = [
            (occurrence_original, event["venue"]),
            *((slot["start_local"], slot["venue"]) for slot in event.get("candidate_slots", [])),
        ]
        distinct_options = list(dict.fromkeys(raw_options))
        chosen_ranks[entry["occurrence_id"]] = distinct_options.index(
            (local_start, entry["venue"])
        )

    for rule in policy["placement_resource_portfolios"]:
        units = sum(
            rule["event_base_units"][entry["event_id"]]
            + rule["rank_adjustments"][str(chosen_ranks[entry["occurrence_id"]])]
            for entry in data["final_schedule"]
            if entry["event_id"] in rule["event_base_units"]
        )
        assert rule["minimum_units"] <= units <= rule["maximum_units"]

    scheduled_event_ranks = {
        entry["event_id"]: chosen_ranks[entry["occurrence_id"]]
        for entry in data["final_schedule"]
    }
    for rule in policy["placement_commitments"]:
        triggered = scheduled_event_ranks.get(rule["if_event_id"]) in set(
            rule["if_placement_ranks"]
        )
        if triggered:
            count = sum(
                scheduled_event_ranks.get(event_id) in set(ranks)
                for event_id, ranks in rule["then_event_ranks"].items()
            )
            assert count >= rule["minimum_then_scheduled"]


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
