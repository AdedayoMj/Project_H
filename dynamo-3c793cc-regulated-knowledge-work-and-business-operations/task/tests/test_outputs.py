from __future__ import annotations

import csv
import json
from decimal import ROUND_HALF_UP, Decimal
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


def _golden_expense_rows() -> list[dict]:
    with (GOLDEN_INPUT_PATH / "expense_lines.csv").open(newline="") as handle:
        return list(csv.DictReader(handle))


def _golden_line_ids() -> list[str]:
    return [row["line_id"] for row in _golden_expense_rows()]


def _by_line_id(line_items: list[dict]) -> dict[str, dict]:
    return {item["line_id"]: item for item in line_items}


def test_output_file_exists():
    """The required artifact exists, is a regular JSON file, and parses to an object."""
    assert OUTPUT_PATH.exists()
    assert not OUTPUT_PATH.is_symlink()
    assert isinstance(_load_output(), dict)


def test_input_files_unchanged():
    """The agent leaves every provided input file byte-identical and adds no input files."""
    golden_files = sorted(p.relative_to(GOLDEN_INPUT_PATH) for p in GOLDEN_INPUT_PATH.rglob("*") if p.is_file())
    actual_files = sorted(p.relative_to(INPUT_PATH) for p in INPUT_PATH.rglob("*") if p.is_file())
    assert actual_files == golden_files
    for rel in golden_files:
        assert (INPUT_PATH / rel).read_bytes() == (GOLDEN_INPUT_PATH / rel).read_bytes()


def test_output_top_level_schema():
    """The artifact has exactly the disclosed line_items and trip_summary containers."""
    data = _load_output()
    assert set(data) == {"line_items", "trip_summary"}
    assert isinstance(data["line_items"], list)
    assert isinstance(data["trip_summary"], dict)


def test_line_items_cover_every_expense_line_exactly_once():
    """Every input line_id appears exactly once and in the original CSV order."""
    data = _load_output()
    ids = [item["line_id"] for item in data["line_items"]]
    assert len(ids) == len(set(ids))
    assert ids == _golden_line_ids()


def test_line_items_well_formed():
    """Each line item has the exact fields, legal status, and non-negative integer amount."""
    data = _load_output()
    for item in data["line_items"]:
        assert set(item) == LINE_ITEM_KEYS
        assert item["status"] in STATUSES
        assert isinstance(item["reimbursable_cents"], int) and not isinstance(item["reimbursable_cents"], bool)
        assert item["reimbursable_cents"] >= 0


def test_trip_summary_well_formed():
    """The summary has all seven disclosed fields with valid category and value types."""
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
    """The final reimbursement never exceeds the disclosed whole-trip budget cap."""
    summary = _load_output()["trip_summary"]
    assert summary["final_reimbursable_cents"] <= summary["trip_budget_cap_cents"]


def test_deferred_list_consistent_with_line_items():
    """The deferred summary list exactly identifies zeroed DEFERRED_OVER_BUDGET lines."""
    data = _load_output()
    deferred_lines = {i["line_id"] for i in data["line_items"] if i["status"] == "DEFERRED_OVER_BUDGET"}
    assert set(data["trip_summary"]["deferred_over_budget_line_ids"]) == deferred_lines
    for item in data["line_items"]:
        if item["status"] == "DEFERRED_OVER_BUDGET":
            assert item["reimbursable_cents"] == 0


def test_boundary_per_diem_proration_is_exercised():
    """Expense-free boundary days make the disclosed first/last-day proration observable."""
    data = _load_output()
    itinerary = json.loads((INPUT_PATH / "itinerary.json").read_text())
    policy = json.loads((INPUT_PATH / "policy.json").read_text())
    rates = json.loads((INPUT_PATH / "per_diem_rates.json").read_text())
    dates = [day["date"] for day in itinerary["days"]]
    boundaries = {min(dates), max(dates)}
    assert boundaries.isdisjoint(row["date"] for row in _golden_expense_rows())

    fraction = Decimal(str(policy["per_diem_proration_fraction"]))
    boundary_total = sum(
        int(
            (Decimal(rates[day["sleeping_city"]]) * fraction).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )
        for day in itinerary["days"]
        if day["date"] in boundaries
    )
    assert boundary_total > 0
    assert data["trip_summary"]["total_per_diem_cents"] == boundary_total


def test_summary_is_consistent_with_line_items():
    """Final, flagged, deferred, and category totals are recomputed from submitted lines."""
    data = _load_output()
    summary = data["trip_summary"]
    policy = json.loads((INPUT_PATH / "policy.json").read_text())
    category_by_line = {
        row["line_id"]: row["category"] for row in _golden_expense_rows()
    }

    expected_breakdown = {
        category: {
            "reimbursable_cents": 0,
            "flagged_for_approval_cents": 0,
        }
        for category in CATEGORIES
    }
    total_flagged = 0
    for item in data["line_items"]:
        category = category_by_line[item["line_id"]]
        amount = item["reimbursable_cents"]
        expected_breakdown[category]["reimbursable_cents"] += amount
        if item["status"] == "NEEDS_MANAGER_APPROVAL":
            expected_breakdown[category]["flagged_for_approval_cents"] += amount
            total_flagged += amount

    line_total = sum(item["reimbursable_cents"] for item in data["line_items"])
    assert summary["category_breakdown"] == expected_breakdown
    assert summary["total_flagged_for_approval_cents"] == total_flagged
    assert summary["final_reimbursable_cents"] == (
        summary["total_per_diem_cents"] + line_total
    )
    assert summary["trip_budget_cap_cents"] == policy["trip_reimbursement_cap_cents"]
    assert summary["gross_reimbursable_cents"] >= summary["final_reimbursable_cents"]
    assert summary["deferred_over_budget_line_ids"] == sorted(
        summary["deferred_over_budget_line_ids"]
    )


def test_retention_rules_satisfied():
    """The retained set satisfies every disclosed weighted portfolio and conditional bundle."""
    data = _load_output()
    policy = json.loads((INPUT_PATH / "policy.json").read_text())
    retained = {
        item["line_id"]
        for item in data["line_items"]
        if item["status"] not in {"DEFERRED_OVER_BUDGET", "DUPLICATE_EXCLUDED", "NON_REIMBURSABLE"}
    }
    for rule in policy["retention_portfolios"]:
        units = sum(
            unit for line_id, unit in rule["line_units"].items()
            if line_id in retained
        )
        assert rule["minimum_retained_units"] <= units <= rule["maximum_retained_units"]
    for rule in policy["retention_commitments"]:
        if rule["if_retained_line_id"] in retained:
            retained_then = len(retained.intersection(rule["then_line_ids"]))
            assert retained_then >= rule["minimum_then_retained"]


def test_line_items_match_reference():
    """Every line's exact reimbursable amount and final status match the reference audit."""
    assert _by_line_id(_load_output()["line_items"]) == _by_line_id(_load_expected()["line_items"])


def test_trip_summary_matches_reference():
    """Every trip-level and category-level total exactly matches the reference audit."""
    assert _load_output()["trip_summary"] == _load_expected()["trip_summary"]
