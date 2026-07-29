from __future__ import annotations

import hashlib
import json
import os
import re
from fractions import Fraction
from pathlib import Path


APP_ROOT = Path(os.environ.get("GEOM_APP_ROOT", "/app"))
OUTPUT = APP_ROOT / "output.json"
INPUT = APP_ROOT / "input" / "facility.json"
EXPECTED_INPUT_SHA256 = "701dbdcf64a5e2fe5c789cd788e69798b28bf44e4cb5f35f5fcaf77716a1a878"
EXPECTED_OUTPUT_SHA256 = "e433b0634a34ff213ecbc19b0e9b1e9ff2bcb65a14a8e7fe2359c44eaa7c6072"


def load_output() -> dict:
    return json.loads(OUTPUT.read_text())


def canonical_digest(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def parse_rational(value: str) -> Fraction:
    assert isinstance(value, str)
    assert re.fullmatch(r"-?(0|[1-9]\d*)(/[1-9]\d*)?", value)
    result = Fraction(value)
    assert value == (str(result.numerator) if result.denominator == 1 else f"{result.numerator}/{result.denominator}")
    return result


def test_output_is_a_regular_json_object():
    """The sole required output exists as a non-symlinked regular JSON object."""
    assert OUTPUT.exists()
    assert not OUTPUT.is_symlink()
    assert OUTPUT.is_file()
    assert isinstance(load_output(), dict)


def test_facility_input_is_unchanged():
    """The exact geometry and transition instance supplied to the agent remains byte-identical."""
    assert hashlib.sha256(INPUT.read_bytes()).hexdigest() == EXPECTED_INPUT_SHA256
    assert sorted(path.name for path in INPUT.parent.iterdir()) == ["facility.json"]


def test_exact_documented_schema():
    """Every required section and nested record has exactly the documented keys and container types."""
    data = load_output()
    assert set(data) == {"critical_times", "open_intervals", "event_visibility", "schedule", "objective"}
    assert all(isinstance(data[key], list) for key in ("critical_times", "open_intervals", "event_visibility", "schedule"))
    assert isinstance(data["objective"], dict)
    assert all(set(row) == {"start", "end", "visible"} for row in data["open_intervals"])
    assert all(set(row) == {"time", "visible"} for row in data["event_visibility"])
    assert all(set(row) == {"observer", "start", "end"} for row in data["schedule"])
    assert set(data["objective"]) == {
        "transition_cost",
        "handoffs",
        "observer_sequence",
        "handoff_times",
    }


def test_exact_rational_partition_and_observer_sets():
    """Critical times are canonical, and event/open records form the required complete non-overlapping partition."""
    data = load_output()
    facility = json.loads(INPUT.read_text())
    known = set(facility["observer_ids"])
    times = [parse_rational(value) for value in data["critical_times"]]
    assert times == sorted(set(times))
    assert times[0] == 0
    assert times[-1] == len(facility["route"]) - 1
    assert len(data["event_visibility"]) == len(times)
    assert len(data["open_intervals"]) == len(times) - 1
    for index, row in enumerate(data["event_visibility"]):
        assert parse_rational(row["time"]) == times[index]
        assert row["visible"] == sorted(set(row["visible"]))
        assert set(row["visible"]) <= known
    for index, row in enumerate(data["open_intervals"]):
        assert parse_rational(row["start"]) == times[index]
        assert parse_rational(row["end"]) == times[index + 1]
        assert row["visible"] == sorted(set(row["visible"]))
        assert set(row["visible"]) <= known
    for index in range(1, len(times) - 1):
        point = data["event_visibility"][index]["visible"]
        left = data["open_intervals"][index - 1]["visible"]
        right = data["open_intervals"][index]["visible"]
        assert point != left or point != right or left != right


def test_schedule_is_continuous_visible_and_transition_legal():
    """Closed schedule ranges cover the route, use reported visibility throughout, and make only allowed handoffs."""
    data = load_output()
    facility = json.loads(INPUT.read_text())
    times = [parse_rational(value) for value in data["critical_times"]]
    time_index = {time: index for index, time in enumerate(times)}
    schedule = data["schedule"]
    assert schedule
    assert parse_rational(schedule[0]["start"]) == times[0]
    assert parse_rational(schedule[-1]["end"]) == times[-1]
    transitions = {(row["from"], row["to"]): row["cost"] for row in facility["transitions"]}
    total_cost = 0
    for index, row in enumerate(schedule):
        start = parse_rational(row["start"])
        end = parse_rational(row["end"])
        assert start in time_index and end in time_index and start < end
        left, right = time_index[start], time_index[end]
        observer = row["observer"]
        assert observer in data["event_visibility"][left]["visible"]
        assert observer in data["event_visibility"][right]["visible"]
        assert all(observer in data["open_intervals"][cell]["visible"] for cell in range(left, right))
        assert all(observer in data["event_visibility"][cell]["visible"] for cell in range(left, right + 1))
        if index:
            previous = schedule[index - 1]
            assert parse_rational(previous["end"]) == start
            assert previous["observer"] != observer
            assert (previous["observer"], observer) in transitions
            assert previous["observer"] in data["event_visibility"][left]["visible"]
            total_cost += transitions[(previous["observer"], observer)]
    assert total_cost == data["objective"]["transition_cost"]


def test_objective_fields_reconcile_with_schedule():
    """All four reported objective components exactly match the normalized schedule."""
    data = load_output()
    sequence = [row["observer"] for row in data["schedule"]]
    handoff_times = [row["start"] for row in data["schedule"][1:]]
    objective = data["objective"]
    assert isinstance(objective["transition_cost"], int) and not isinstance(objective["transition_cost"], bool)
    assert isinstance(objective["handoffs"], int) and not isinstance(objective["handoffs"], bool)
    assert objective["transition_cost"] >= 0
    assert objective["handoffs"] == len(sequence) - 1
    assert objective["observer_sequence"] == sequence
    assert objective["handoff_times"] == handoff_times
    assert [parse_rational(value) for value in objective["handoff_times"]] == sorted(
        parse_rational(value) for value in objective["handoff_times"]
    )


def test_exact_visibility_decomposition_and_global_optimum():
    """The complete exact decomposition and lexicographically resolved optimum match the reference result."""
    assert canonical_digest(load_output()) == EXPECTED_OUTPUT_SHA256
