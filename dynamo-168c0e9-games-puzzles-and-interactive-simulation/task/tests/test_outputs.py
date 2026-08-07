from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from collections import Counter, deque
from functools import lru_cache
from pathlib import Path

from reference_model import VerifierEngine, calculate, encoded


APP = Path(os.environ.get("RESCUE_APP_ROOT", "/app"))
INPUT = APP / "input"
OUTPUT = APP / "output"
TESTS = Path(__file__).parent
POLICY_PATH = OUTPUT / "rescue_policy.json"
SOLVER_PATH = OUTPUT / "solver.py"

TOP_FIELDS = {"schema_version", "arenas"}
ARENA_FIELDS = {
    "arena_id",
    "root_node_id",
    "worst_case_turns",
    "worst_case_energy",
    "worst_case_handoffs",
    "policy_node_count",
    "policy_sha256",
    "nodes",
}
NODE_FIELDS = {
    "node_id",
    "team_a_node",
    "team_b_node",
    "rescued_victims",
    "possible_modes",
    "last_actor",
    "remaining_worst_case_turns",
    "remaining_worst_case_energy",
    "remaining_worst_case_handoffs",
    "optimal_action_count",
    "action",
    "outcomes",
}
OUTCOME_FIELDS = {"observation", "next_node_id"}


@lru_cache(maxsize=1)
def submitted():
    return json.loads(POLICY_PATH.read_text())


@lru_cache(maxsize=1)
def reference():
    return calculate(INPUT)


def regular_file(path):
    return path.exists() and not path.is_symlink() and stat.S_ISREG(path.stat().st_mode)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_arenas(input_root=INPUT):
    manifest = json.loads((input_root / "manifest.json").read_text())
    return [json.loads((input_root / row["file"]).read_text()) for row in manifest["arenas"]]


def validate_complete_document(document, input_root):
    expected, engines = calculate(input_root)
    assert document == expected
    assert set(document) == TOP_FIELDS
    assert document["schema_version"] == 1
    for arena_row in document["arenas"]:
        assert set(arena_row) == ARENA_FIELDS
        engine = engines[arena_row["arena_id"]]
        by_id = {node["node_id"]: node for node in arena_row["nodes"]}
        assert len(by_id) == arena_row["policy_node_count"] == len(arena_row["nodes"])
        assert arena_row["root_node_id"] in by_id
        assert arena_row["policy_sha256"] == hashlib.sha256(
            encoded(arena_row["nodes"]).encode()
        ).hexdigest()
        for node in arena_row["nodes"]:
            assert set(node) == NODE_FIELDS
            state = engine.state_from_json(node)
            assert node["node_id"] == engine.identifier(state)
            value, action, branches, action_count = engine.exact(state)
            assert (
                node["remaining_worst_case_turns"],
                node["remaining_worst_case_energy"],
                node["remaining_worst_case_handoffs"],
            ) == value
            assert node["optimal_action_count"] == action_count
            assert node["action"] == action
            expected_outcomes = [
                {"observation": observation, "next_node_id": engine.identifier(child)}
                for observation, child in branches
            ]
            assert node["outcomes"] == expected_outcomes
            assert all(outcome["next_node_id"] in by_id for outcome in node["outcomes"])


def build_counterfactual(destination):
    arena = json.loads((INPUT / "arenas" / "rescue_02.json").read_text())
    arena["arena_id"] = "private_contingency"
    node_ids = [node["node_id"] for node in arena["nodes"]]
    arena["teams"][0]["start_node"] = node_ids[2]
    arena["teams"][1]["start_node"] = node_ids[-2]
    arena["teams"][0]["scan_energy"] += 2
    arena["teams"][1]["move_energy_multiplier"] += 1
    victim_nodes = [node_ids[-1], node_ids[-3], node_ids[4], node_ids[6]]
    for victim, node_id in zip(arena["victims"], victim_nodes, strict=True):
        victim["node"] = node_id
    modes = arena["mode_ids"]
    for edge_number, edge in enumerate(arena["edges"]):
        edge["energy_cost"] += 1 + edge_number % 2
        if len(edge["safe_modes"]) != len(modes):
            edge["safe_modes"] = sorted(
                modes[(modes.index(mode) + 1 + edge_number % 2) % len(modes)]
                for mode in edge["safe_modes"]
            )
    for node_number, node in enumerate(arena["nodes"]):
        if node["signals"] is not None:
            old = node["signals"]
            node["signals"] = {
                mode: old[modes[-1 - modes.index(mode)]] + f"_P{node_number % 2}"
                for mode in modes
            }

    arena_root = destination / "arenas"
    arena_root.mkdir(parents=True)
    arena_path = arena_root / "private_contingency.json"
    arena_path.write_text(json.dumps(arena, indent=2, sort_keys=True) + "\n")
    (destination / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "arenas": [
                    {
                        "arena_id": "private_contingency",
                        "file": "arenas/private_contingency.json",
                    }
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    shutil.copy2(INPUT / "rules.json", destination / "rules.json")
    return arena


def test_required_artifacts_are_regular_parseable_files():
    """The policy and reusable solver exist as ordinary top-level artifacts."""
    assert regular_file(POLICY_PATH)
    assert regular_file(SOLVER_PATH)
    assert isinstance(submitted(), dict)
    compile(SOLVER_PATH.read_text(), str(SOLVER_PATH), "exec")


def test_generated_input_tree_is_immutable_and_hash_locked():
    """Every agent-visible arena and rule file retains its generated bytes."""
    expected = json.loads((TESTS / "expected.json").read_text())["input_sha256"]
    found = {
        path.relative_to(INPUT).as_posix(): sha256(path)
        for path in sorted(INPUT.rglob("*"))
        if path.is_file()
    }
    assert found == expected


def test_environment_contains_no_generator_or_hidden_policy_engine():
    """Build-time generation and hidden verifier material are absent."""
    forbidden = {"build_instance.py", "reference_model.py", "expected.json"}
    found = {path.name for path in APP.rglob("*") if path.is_file()}
    assert forbidden.isdisjoint(found)


def test_manifest_and_arena_schemas_are_exact():
    """Published evidence uses the documented graph, mode, team, and sensor schemas."""
    manifest = json.loads((INPUT / "manifest.json").read_text())
    assert set(manifest) == {"schema_version", "arenas"}
    assert manifest["schema_version"] == 1
    assert len(manifest["arenas"]) == 10
    assert [row["arena_id"] for row in manifest["arenas"]] == sorted(
        row["arena_id"] for row in manifest["arenas"]
    )
    for record, arena in zip(manifest["arenas"], source_arenas(), strict=True):
        assert set(record) == {"arena_id", "file"}
        assert arena["arena_id"] == record["arena_id"]
        assert set(arena) == {
            "schema_version", "arena_id", "mode_ids", "teams", "victims",
            "nodes", "edges", "max_worst_case_turns", "generation_seed",
        }
        assert arena["schema_version"] == 1
        assert [team["team_id"] for team in arena["teams"]] == ["A", "B"]
        assert all(set(team) == {
            "team_id", "start_node", "move_energy_multiplier", "scan_energy"
        } for team in arena["teams"])
        assert all(set(victim) == {"victim_id", "node"} for victim in arena["victims"])
        assert all(set(node) == {"node_id", "signals"} for node in arena["nodes"])
        assert all(set(edge) == {
            "edge_id", "a", "b", "turn_cost", "energy_cost", "safe_modes"
        } for edge in arena["edges"])


def test_fixtures_exercise_partial_observation_and_asymmetric_resources():
    """The generated worlds require beliefs, conditional routes, sensors, and team costing."""
    mode_counts = set()
    node_counts = set()
    sensor_partitions = set()
    for arena in source_arenas():
        modes = arena["mode_ids"]
        mode_counts.add(len(modes))
        node_counts.add(len(arena["nodes"]))
        assert len(modes) >= 6
        assert len(arena["victims"]) == 4
        uncertain = [
            edge for edge in arena["edges"] if 0 < len(edge["safe_modes"]) < len(modes)
        ]
        assert len(uncertain) >= len(arena["nodes"])
        assert any(len(edge["safe_modes"]) == len(modes) for edge in arena["edges"])
        assert arena["teams"][0]["move_energy_multiplier"] != arena["teams"][1][
            "move_energy_multiplier"
        ]
        for node in arena["nodes"]:
            if node["signals"] is not None:
                assert set(node["signals"]) == set(modes)
                sensor_partitions.add(tuple(node["signals"][mode] for mode in modes))
    assert mode_counts == {6, 7, 8}
    assert len(node_counts) == 4
    assert len(sensor_partitions) >= 12


def test_reference_policies_activate_branching_scans_failures_and_ties():
    """Calibrated optima contain genuine observation branches and all policy cruxes."""
    document, _ = reference()
    calibration = json.loads((TESTS / "expected.json").read_text())["calibration"]
    assert len(document["arenas"]) == calibration["arena_count"]
    assert sum(row["policy_node_count"] for row in document["arenas"]) == calibration[
        "total_policy_nodes"
    ]
    actions = Counter()
    observations = Counter()
    tied_nodes = 0
    branching_nodes = 0
    for arena in document["arenas"]:
        assert arena["worst_case_turns"] >= 10
        assert arena["policy_node_count"] >= 5
        for node in arena["nodes"]:
            if node["action"]:
                team, kind, *_ = node["action"].split(":")
                actions[(team, kind)] += 1
            observations.update(row["observation"] for row in node["outcomes"])
            tied_nodes += node["optimal_action_count"] > 1
            branching_nodes += len(node["outcomes"]) > 1
    assert actions[("A", "MOVE")] > 0 and actions[("B", "MOVE")] > 0
    assert actions[("A", "SCAN")] + actions[("B", "SCAN")] >= 3
    assert observations["ARRIVED"] > 0 and observations["BLOCKED"] >= 10
    assert tied_nodes >= 5
    assert branching_nodes >= 10


def test_output_uses_exact_nested_schemas_and_canonical_order():
    """No undeclared fields, malformed types, or reordered policy records are accepted."""
    document = submitted()
    assert set(document) == TOP_FIELDS and document["schema_version"] == 1
    manifest = json.loads((INPUT / "manifest.json").read_text())
    assert [row["arena_id"] for row in document["arenas"]] == [
        row["arena_id"] for row in manifest["arenas"]
    ]
    for arena in document["arenas"]:
        assert set(arena) == ARENA_FIELDS
        assert type(arena["worst_case_turns"]) is int
        assert type(arena["worst_case_energy"]) is int
        assert type(arena["worst_case_handoffs"]) is int
        assert type(arena["policy_node_count"]) is int
        assert len(arena["policy_sha256"]) == 64
        assert [node["node_id"] for node in arena["nodes"]] == sorted(
            node["node_id"] for node in arena["nodes"]
        )
        for node in arena["nodes"]:
            assert set(node) == NODE_FIELDS
            assert node["last_actor"] in (None, "A", "B")
            assert type(node["optimal_action_count"]) is int
            assert node["optimal_action_count"] >= 1
            assert [row["observation"] for row in node["outcomes"]] == sorted(
                row["observation"] for row in node["outcomes"]
            )
            assert all(set(outcome) == OUTCOME_FIELDS for outcome in node["outcomes"])


def test_arena_summaries_and_complete_policy_values_match_independent_model():
    """Every root value, action, local value, multiplicity, and observation target is pinned."""
    expected, _ = reference()
    assert submitted() == expected


def test_state_ids_roots_and_policy_digests_are_functional():
    """State identities and whole-policy digests are recomputed from submitted content."""
    _, engines = reference()
    for arena in submitted()["arenas"]:
        engine = engines[arena["arena_id"]]
        assert arena["root_node_id"] == engine.identifier(engine.initial())
        assert arena["policy_node_count"] == len(arena["nodes"])
        assert arena["policy_sha256"] == hashlib.sha256(
            encoded(arena["nodes"]).encode()
        ).hexdigest()
        for node in arena["nodes"]:
            assert node["node_id"] == engine.identifier(engine.state_from_json(node))


def test_submitted_actions_reproduce_exact_belief_partitions_and_costs():
    """Every policy edge is a legal scan or move with complete mode-observation coverage."""
    _, engines = reference()
    for arena in submitted()["arenas"]:
        engine = engines[arena["arena_id"]]
        node_ids = {node["node_id"] for node in arena["nodes"]}
        for node in arena["nodes"]:
            state = engine.state_from_json(node)
            if engine.goal(state):
                assert node["action"] is None and node["outcomes"] == []
                continue
            assert node["action"] in engine.available(state)
            expected = [
                {"observation": observation, "next_node_id": engine.identifier(child)}
                for observation, child in engine.advance(state, node["action"])
            ]
            assert node["outcomes"] == expected
            assert all(row["next_node_id"] in node_ids for row in expected)


def test_policy_graph_is_closed_acyclic_and_strong_for_every_mode():
    """Every compatible observation trace stays in the graph and reaches a rescue goal."""
    _, engines = reference()
    for arena in submitted()["arenas"]:
        engine = engines[arena["arena_id"]]
        nodes = {node["node_id"]: node for node in arena["nodes"]}
        reachable = set()
        pending = deque([arena["root_node_id"]])
        while pending:
            node_id = pending.popleft()
            if node_id in reachable:
                continue
            reachable.add(node_id)
            node = nodes[node_id]
            state = engine.state_from_json(node)
            if engine.goal(state):
                assert node["remaining_worst_case_turns"] == 0
                continue
            turn_cost = engine.cost(state, node["action"])[0]
            for outcome in node["outcomes"]:
                child = nodes[outcome["next_node_id"]]
                assert child["remaining_worst_case_turns"] <= (
                    node["remaining_worst_case_turns"] - turn_cost
                )
                pending.append(outcome["next_node_id"])
        assert reachable == set(nodes)


def test_canonical_action_and_local_optimal_action_count_are_exact():
    """The policy uses the published action tie-break and reports every tied optimum."""
    _, engines = reference()
    for arena in submitted()["arenas"]:
        engine = engines[arena["arena_id"]]
        for node in arena["nodes"]:
            state = engine.state_from_json(node)
            value, action, _, count = engine.exact(state)
            assert node["action"] == action
            assert node["optimal_action_count"] == count
            assert (
                node["remaining_worst_case_turns"],
                node["remaining_worst_case_energy"],
                node["remaining_worst_case_handoffs"],
            ) == value


def test_reusable_solver_generalizes_to_private_contingency(tmp_path):
    """Fixed published policies fail when starts, rescues, observations, modes, and costs change."""
    hidden_input = tmp_path / "hidden-input"
    hidden_output = tmp_path / "hidden-output"
    hidden_arena = build_counterfactual(hidden_input)
    expected, _ = calculate(hidden_input)
    published = next(
        arena for arena in submitted()["arenas"] if arena["arena_id"] == "rescue_02"
    )
    assert expected["arenas"][0]["arena_id"] == hidden_arena["arena_id"]
    assert expected["arenas"][0]["root_node_id"] != published["root_node_id"]
    assert expected["arenas"][0]["policy_sha256"] != published["policy_sha256"]

    environment = os.environ.copy()
    environment.pop("RESCUE_APP_ROOT", None)
    environment.pop("PYTHONPATH", None)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [
            sys.executable,
            str(SOLVER_PATH),
            "--input-root",
            str(hidden_input),
            "--output-root",
            str(hidden_output),
        ],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        timeout=180,
        check=False,
    )
    assert completed.returncode == 0, (completed.stdout[-2000:], completed.stderr[-2000:])
    actual = json.loads((hidden_output / "rescue_policy.json").read_text())
    validate_complete_document(actual, hidden_input)


def test_complete_value_level_validator_accepts_published_oracle():
    """The reusable full validator covers the published output without special paths."""
    validate_complete_document(submitted(), INPUT)
