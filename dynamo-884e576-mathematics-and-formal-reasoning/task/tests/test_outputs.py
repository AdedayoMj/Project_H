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
EXPECTED_OUTPUT = Path(__file__).with_name("expected_output.json")
EXPECTED_INPUT_SHA256 = "701dbdcf64a5e2fe5c789cd788e69798b28bf44e4cb5f35f5fcaf77716a1a878"


def load_output() -> dict:
    return json.loads(OUTPUT.read_text())


def load_expected_output() -> dict:
    return json.loads(EXPECTED_OUTPUT.read_text())


def first_difference(actual: object, expected: object, path: str = "$") -> str | None:
    if type(actual) is not type(expected):
        return f"{path}: expected {type(expected).__name__}, got {type(actual).__name__}"
    if isinstance(expected, dict):
        if set(actual) != set(expected):
            return f"{path}: expected keys {sorted(expected)}, got {sorted(actual)}"
        for key in expected:
            difference = first_difference(actual[key], expected[key], f"{path}.{key}")
            if difference is not None:
                return difference
        return None
    if isinstance(expected, list):
        if len(actual) != len(expected):
            return f"{path}: expected {len(expected)} entries, got {len(actual)}"
        for index, (actual_item, expected_item) in enumerate(zip(actual, expected)):
            difference = first_difference(actual_item, expected_item, f"{path}[{index}]")
            if difference is not None:
                return difference
        return None
    if actual != expected:
        return f"{path}: expected {expected!r}, got {actual!r}"
    return None


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


def test_reported_objective_is_the_readable_four_level_optimum():
    """Each objective component equals the verifier's readable exact global optimum."""
    actual = load_output()["objective"]
    expected = load_expected_output()["objective"]
    for component in ("transition_cost", "handoffs", "observer_sequence", "handoff_times"):
        assert actual[component] == expected[component], (
            f"objective.{component}: expected {expected[component]!r}, "
            f"got {actual[component]!r}"
        )


def test_exact_visibility_decomposition_and_global_optimum():
    """The complete result equals the readable reference, reporting the first differing field or array index."""
    difference = first_difference(load_output(), load_expected_output())
    assert difference is None, difference
