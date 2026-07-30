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
    "retained_priority_weight",
    "retained_coverage_bonus_points",
    "worst_case_recovery_priority_weight",
    "worst_case_recovery_coverage_bonus_points",
    "total_recovery_coverage_bonus_points",
    "worst_case_recovery_final_reimbursable_cents",
    "recovery_reserve_line_ids",
    "recovery_reserve_certification_units",
    "recovery_reserve_certification_score",
    "recovery_reserve_certifications",
    "retention_recovery_scenarios",
    "deferred_over_budget_line_ids",
    "total_flagged_for_approval_cents",
    "category_breakdown",
}
RECOVERY_KEYS = {
    "scenario_id",
    "unavailable_line_ids",
    "reactivated_line_ids",
    "retained_priority_weight",
    "retained_coverage_bonus_points",
    "final_reimbursable_cents",
}
CERTIFICATION_KEYS = {
    "line_id",
    "reviewer_id",
    "period",
    "certification_units",
    "score_points",
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
    """The summary has every disclosed field with valid nested and scalar types."""
    data = _load_output()
    summary = data["trip_summary"]
    assert set(summary) == TRIP_SUMMARY_KEYS
    for key in (
        "total_per_diem_cents",
        "gross_reimbursable_cents",
        "trip_budget_cap_cents",
        "final_reimbursable_cents",
        "retained_priority_weight",
        "retained_coverage_bonus_points",
        "worst_case_recovery_priority_weight",
        "worst_case_recovery_coverage_bonus_points",
        "total_recovery_coverage_bonus_points",
        "worst_case_recovery_final_reimbursable_cents",
        "recovery_reserve_certification_units",
        "recovery_reserve_certification_score",
        "total_flagged_for_approval_cents",
    ):
        assert isinstance(summary[key], int) and not isinstance(summary[key], bool)
        assert summary[key] >= 0
    assert isinstance(summary["deferred_over_budget_line_ids"], list)
    assert all(isinstance(x, str) for x in summary["deferred_over_budget_line_ids"])
    assert isinstance(summary["retention_recovery_scenarios"], list)
    assert isinstance(summary["recovery_reserve_line_ids"], list)
    assert isinstance(summary["recovery_reserve_certifications"], list)
    assert all(
        isinstance(line_id, str)
        for line_id in summary["recovery_reserve_line_ids"]
    )
    assert summary["recovery_reserve_line_ids"] == sorted(
        summary["recovery_reserve_line_ids"]
    )
    assert len(summary["recovery_reserve_line_ids"]) == len(
        set(summary["recovery_reserve_line_ids"])
    )
    certification_line_ids = []
    for row in summary["recovery_reserve_certifications"]:
        assert set(row) == CERTIFICATION_KEYS
        assert isinstance(row["line_id"], str)
        assert isinstance(row["reviewer_id"], str)
        assert isinstance(row["period"], str)
        for key in ("certification_units", "score_points"):
            assert isinstance(row[key], int) and not isinstance(
                row[key], bool
            )
            assert row[key] >= 0
        certification_line_ids.append(row["line_id"])
    assert certification_line_ids == sorted(certification_line_ids)
    assert len(certification_line_ids) == len(
        set(certification_line_ids)
    )
    for scenario in summary["retention_recovery_scenarios"]:
        assert set(scenario) == RECOVERY_KEYS
        assert isinstance(scenario["scenario_id"], str)
        for key in ("unavailable_line_ids", "reactivated_line_ids"):
            assert isinstance(scenario[key], list)
            assert all(isinstance(line_id, str) for line_id in scenario[key])
            assert scenario[key] == sorted(scenario[key])
            assert len(scenario[key]) == len(set(scenario[key]))
        for key in (
            "retained_priority_weight",
            "retained_coverage_bonus_points",
            "final_reimbursable_cents",
        ):
            assert isinstance(scenario[key], int) and not isinstance(scenario[key], bool)
            assert scenario[key] >= 0
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


def test_retention_rules_and_coverage_score_satisfied():
    """The retained set satisfies every portfolio/bounded commitment and reconciles its coverage score."""
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
            assert rule["minimum_then_retained"] <= retained_then <= rule["maximum_then_retained"]
    coverage_score = sum(
        rule["bonus_points"]
        for rule in policy["retention_coverage_bonuses"]
        if len(retained.intersection(rule["line_ids"])) >= rule["minimum_retained"]
    )
    assert data["trip_summary"]["retained_coverage_bonus_points"] == coverage_score


def test_recovery_scenarios_are_feasible_and_reconciled():
    """Recoveries and their shared certification schedule obey every coupled rule."""
    data = _load_output()
    summary = data["trip_summary"]
    policy = json.loads((INPUT_PATH / "policy.json").read_text())
    rows = _golden_expense_rows()
    row_by_id = {row["line_id"]: row for row in rows}
    expected = sorted(
        policy["retention_recovery_scenarios"],
        key=lambda scenario: scenario["scenario_id"],
    )
    actual = summary["retention_recovery_scenarios"]
    assert [scenario["scenario_id"] for scenario in actual] == [
        scenario["scenario_id"] for scenario in expected
    ]

    primary = {
        item["line_id"]
        for item in data["line_items"]
        if item["status"] not in {
            "DEFERRED_OVER_BUDGET",
            "DUPLICATE_EXCLUDED",
            "NON_REIMBURSABLE",
        }
    }
    primary_deferred = {
        item["line_id"]
        for item in data["line_items"]
        if item["status"] == "DEFERRED_OVER_BUDGET"
    }
    recovery_weights = []
    recovery_bonuses = []
    recovery_finals = []
    for rule, reported in zip(expected, actual):
        unavailable = set(rule["unavailable_line_ids"])
        reactivated = set(reported["reactivated_line_ids"])
        assert reported["unavailable_line_ids"] == sorted(unavailable)
        assert unavailable <= primary
        assert reactivated <= primary_deferred
        assert reactivated.isdisjoint(unavailable)
        assert len(reactivated) <= rule["maximum_reactivated_lines"]
        recovered = (primary - unavailable) | reactivated

        final = reported["final_reimbursable_cents"]
        assert final >= summary["total_per_diem_cents"]
        assert final <= (
            policy["trip_reimbursement_cap_cents"]
            - rule["additional_cap_reduction_cents"]
        )

        for portfolio in policy["retention_portfolios"]:
            units = sum(
                unit for line_id, unit in portfolio["line_units"].items()
                if line_id in recovered
            )
            waived = sum(
                portfolio["line_units"].get(line_id, 0)
                for line_id in unavailable
            )
            minimum = max(0, portfolio["minimum_retained_units"] - waived)
            assert minimum <= units <= portfolio["maximum_retained_units"]
        for commitment in policy["retention_commitments"]:
            if commitment["if_retained_line_id"] in recovered:
                count = len(recovered.intersection(commitment["then_line_ids"]))
                assert commitment["minimum_then_retained"] <= count
                assert count <= commitment["maximum_then_retained"]

        weight = sum(6 - int(row_by_id[line_id]["priority"]) for line_id in recovered)
        bonus = sum(
            coverage["bonus_points"]
            for coverage in policy["retention_coverage_bonuses"]
            if len(recovered.intersection(coverage["line_ids"]))
            >= coverage["minimum_retained"]
        )
        assert reported["retained_priority_weight"] == weight
        assert reported["retained_coverage_bonus_points"] == bonus
        recovery_weights.append(weight)
        recovery_bonuses.append(bonus)
        recovery_finals.append(final)

    primary_weight = sum(6 - int(row_by_id[line_id]["priority"]) for line_id in primary)
    assert summary["retained_priority_weight"] == primary_weight
    assert summary["worst_case_recovery_priority_weight"] == min(recovery_weights)
    assert summary["worst_case_recovery_coverage_bonus_points"] == min(recovery_bonuses)
    assert summary["total_recovery_coverage_bonus_points"] == sum(recovery_bonuses)
    assert summary["worst_case_recovery_final_reimbursable_cents"] == min(recovery_finals)
    reserve = set(summary["recovery_reserve_line_ids"])
    assert reserve == set().union(
        *(set(scenario["reactivated_line_ids"]) for scenario in actual)
    )
    reserve_policy = policy["retention_recovery_reserve"]
    assert len(reserve) <= reserve_policy["maximum_reserved_lines"]
    units = sum(
        reserve_policy["category_certification_units"][row_by_id[line_id]["category"]]
        for line_id in reserve
    )
    assert units == summary["recovery_reserve_certification_units"]
    assert units <= reserve_policy["maximum_certification_units"]

    schedule_policy = policy[
        "retention_recovery_certification_schedule"
    ]
    periods = schedule_policy["period_order"]
    period_index = {
        period: index for index, period in enumerate(periods)
    }
    reviewers = {
        reviewer["reviewer_id"]: reviewer
        for reviewer in schedule_policy["reviewers"]
    }
    certifications = summary["recovery_reserve_certifications"]
    by_line = {row["line_id"]: row for row in certifications}
    assert set(by_line) == reserve
    certification_score = 0
    capacity_used = {
        (reviewer_id, period): 0
        for reviewer_id in reviewers
        for period in periods
    }
    for line_id, assignment in by_line.items():
        expense = row_by_id[line_id]
        reviewer = reviewers[assignment["reviewer_id"]]
        period = assignment["period"]
        category = expense["category"]
        assert category in reviewer["eligible_categories"]
        release = schedule_policy[
            "release_period_by_statement_post_date"
        ][expense["statement_post_date"]]
        assert period_index[period] >= period_index[release]
        expected_units = reserve_policy[
            "category_certification_units"
        ][category]
        assert assignment["certification_units"] == expected_units
        expected_score = (
            reviewer["score_points_by_period"][period]
            + schedule_policy["category_score_adjustment"][category]
        )
        assert assignment["score_points"] == expected_score
        certification_score += expected_score
        capacity_used[(assignment["reviewer_id"], period)] += (
            expected_units
        )
    assert (
        certification_score
        == summary["recovery_reserve_certification_score"]
    )
    for (reviewer_id, period), used in capacity_used.items():
        assert used <= reviewers[reviewer_id][
            "capacity_units_by_period"
        ][period]
    for rule in schedule_policy["conditional_precedence"]:
        before = by_line.get(rule["before_line_id"])
        after = by_line.get(rule["after_line_id"])
        if before is not None and after is not None:
            assert period_index[before["period"]] < period_index[
                after["period"]
            ]
    for rule in schedule_policy["period_separation_groups"]:
        for period in periods:
            assigned = sum(
                by_line.get(line_id, {}).get("period") == period
                for line_id in rule["line_ids"]
            )
            assert assigned <= rule["maximum_in_one_period"]


def test_line_items_match_reference():
    """Every line's exact reimbursable amount and final status match the reference audit."""
    assert _by_line_id(_load_output()["line_items"]) == _by_line_id(_load_expected()["line_items"])


def test_trip_summary_matches_reference():
    """Every trip-level and category-level total exactly matches the reference audit."""
    assert _load_output()["trip_summary"] == _load_expected()["trip_summary"]
