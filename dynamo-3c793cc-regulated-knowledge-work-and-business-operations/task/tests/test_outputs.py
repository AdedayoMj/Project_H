from __future__ import annotations

import csv
import json
from pathlib import Path

OUTPUT_PATH = Path("/app/output.json")
INPUT_PATH = Path("/app/input")
GOLDEN_INPUT_PATH = Path(__file__).parent / "golden_input"
EXPECTED_PATH = Path(__file__).parent / "expected_output.json"

STATUSES = {
    "APPROVED",
    "NEEDS_MANAGER_APPROVAL",
    "DUPLICATE_EXCLUDED",
    "NON_REIMBURSABLE",
    "DEFERRED_OVER_BUDGET",
}
LINE_ITEM_KEYS = {"line_id", "reimbursable_cents", "status"}
CATEGORIES = {"MEALS", "AIRFARE", "MILEAGE"}
TRIP_SUMMARY_KEYS = {
    "total_per_diem_cents",
    "gross_reimbursable_cents",
    "trip_budget_cap_cents",
    "final_reimbursable_cents",
    "deferred_over_budget_line_ids",
    "total_flagged_for_approval_cents",
    "category_breakdown",
}


def _load_output() -> dict:
    return json.loads(OUTPUT_PATH.read_text())


def _load_expected() -> dict:
    return json.loads(EXPECTED_PATH.read_text())


def _golden_line_ids() -> set[str]:
    with (GOLDEN_INPUT_PATH / "expense_lines.csv").open(newline="") as handle:
        return {row["line_id"] for row in csv.DictReader(handle)}


def _by_line_id(line_items: list[dict]) -> dict[str, dict]:
    return {item["line_id"]: item for item in line_items}


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
    """output.json has exactly the two required top-level keys, correctly typed."""
    data = _load_output()
    assert set(data) == {"line_items", "trip_summary"}
    assert isinstance(data["line_items"], list)
    assert isinstance(data["trip_summary"], dict)


def test_line_items_cover_every_expense_line_exactly_once():
    """line_items has exactly one entry per row of expense_lines.csv, no missing or invented line_ids."""
    data = _load_output()
    ids = [item["line_id"] for item in data["line_items"]]
    assert len(ids) == len(set(ids))
    assert set(ids) == _golden_line_ids()


def test_line_items_well_formed():
    """Every line_items entry has exactly the required fields, a legal status, and a non-negative integer amount."""
    data = _load_output()
    for item in data["line_items"]:
        assert set(item) == LINE_ITEM_KEYS
        assert item["status"] in STATUSES
        assert isinstance(item["reimbursable_cents"], int) and not isinstance(item["reimbursable_cents"], bool)
        assert item["reimbursable_cents"] >= 0


def test_trip_summary_well_formed():
    """trip_summary has exactly the required fields, correctly typed, with a full category breakdown."""
    data = _load_output()
    summary = data["trip_summary"]
    assert set(summary) == TRIP_SUMMARY_KEYS
    for key in (
        "total_per_diem_cents",
        "gross_reimbursable_cents",
        "trip_budget_cap_cents",
        "final_reimbursable_cents",
        "total_flagged_for_approval_cents",
    ):
        assert isinstance(summary[key], int) and not isinstance(summary[key], bool)
        assert summary[key] >= 0
    assert isinstance(summary["deferred_over_budget_line_ids"], list)
    assert all(isinstance(x, str) for x in summary["deferred_over_budget_line_ids"])
    assert set(summary["category_breakdown"]) == CATEGORIES
    for entry in summary["category_breakdown"].values():
        assert set(entry) == {"reimbursable_cents", "flagged_for_approval_cents"}
        for value in entry.values():
            assert isinstance(value, int) and not isinstance(value, bool) and value >= 0


def test_final_within_budget_cap():
    """The final reimbursable total must never exceed the trip budget cap."""
    summary = _load_output()["trip_summary"]
    assert summary["final_reimbursable_cents"] <= summary["trip_budget_cap_cents"]


def test_deferred_list_consistent_with_line_items():
    """deferred_over_budget_line_ids must be exactly the DEFERRED_OVER_BUDGET lines, all with amount 0."""
    data = _load_output()
    deferred_lines = {i["line_id"] for i in data["line_items"] if i["status"] == "DEFERRED_OVER_BUDGET"}
    assert set(data["trip_summary"]["deferred_over_budget_line_ids"]) == deferred_lines
    for item in data["line_items"]:
        if item["status"] == "DEFERRED_OVER_BUDGET":
            assert item["reimbursable_cents"] == 0


def test_line_items_match_reference():
    """Every line's reimbursable_cents and status must exactly match the reference solver's output."""
    data = _load_output()
    expected = _load_expected()
    assert _by_line_id(data["line_items"]) == _by_line_id(expected["line_items"])


def test_trip_summary_matches_reference():
    """trip_summary, including the full category breakdown, must exactly match the reference solver's output."""
    data = _load_output()
    expected = _load_expected()
    assert data["trip_summary"] == expected["trip_summary"]
