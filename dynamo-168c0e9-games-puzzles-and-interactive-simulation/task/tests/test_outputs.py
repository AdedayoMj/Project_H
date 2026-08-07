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

DOCUMENT_KEYS = {"schema_version", "arenas"}
ARENA_KEYS = {
    "arena_id",
    "root_node_id",
    "worst_case_turns",
    "worst_case_energy",
    "worst_case_handoffs",
    "policy_node_count",
    "policy_sha256",
    "nodes",
}
CONTEXT_KEYS = {
    "node_id",
    "team_a_node",
    "team_b_node",
    "rescued_victims",
    "possible_modes",
    "last_actor",
    "turn_budget",
    "energy_budget",
    "remaining_worst_case_turns",
    "remaining_worst_case_energy",
    "remaining_worst_case_handoffs",
    "optimal_action_count",
    "action",
    "outcomes",
}
BRANCH_KEYS = {"observation", "next_node_id"}


@lru_cache(maxsize=1)
def candidate_policy():
    """Load the submitted policy once for all read-only checks."""
    return read_json(POLICY_PATH)


@lru_cache(maxsize=1)
def oracle_policy():
    """Compute the independent answer and retain its arena engines."""
    return calculate(INPUT)


def read_json(path):
    """Decode one UTF-8 JSON artifact."""
    return json.loads(path.read_text())


def ordinary_file(path):
    """Reject missing files, symlinks, sockets, and other special paths."""
    return path.exists() and not path.is_symlink() and stat.S_ISREG(path.stat().st_mode)


def file_sha256(path):
    """Hash a file exactly as it appears in the generated input tree."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def arena_inputs(input_root=INPUT):
    """Yield generated arena documents in manifest order."""
    manifest = read_json(input_root / "manifest.json")
    return [read_json(input_root / item["file"]) for item in manifest["arenas"]]


class PolicyAudit:
    """Reconstruct and replay a complete policy against one input directory."""

    def __init__(self, document, input_root):
        self.document = document
        self.expected, self.engines = calculate(input_root)

    def run(self):
        """Apply deep equality plus independent identity and transition checks."""
        assert set(self.document) == DOCUMENT_KEYS
        assert type(self.document["schema_version"]) is int
        assert self.document["schema_version"] == 1
        assert self.document == self.expected
        for arena in self.document["arenas"]:
            self.audit_arena(arena)

    def audit_arena(self, arena):
        """Check one arena's summary, node index, digest, and closed branches."""
        assert set(arena) == ARENA_KEYS
        engine = self.engines[arena["arena_id"]]
        node_index = {row["node_id"]: row for row in arena["nodes"]}
        assert len(node_index) == len(arena["nodes"])
        assert arena["policy_node_count"] == len(node_index)
        assert arena["root_node_id"] in node_index
        assert arena["policy_sha256"] == hashlib.sha256(
            encoded(arena["nodes"]).encode()
        ).hexdigest()
        try:
            for row in arena["nodes"]:
                self.audit_context(engine, node_index, row)
        finally:
            engine.clear_caches()

    @staticmethod
    def audit_context(engine, node_index, row):
        """Re-derive one contextual decision and all of its observation targets."""
        assert set(row) == CONTEXT_KEYS
        state, turn_room, energy_room = engine.context_from_json(row)
        context = state, turn_room, energy_room
        assert row["node_id"] == engine.identifier(*context)

        remaining = engine.contextual_value(*context)
        _, selected, branches, tie_count = engine.choice(*context)
        recorded = (
            row["remaining_worst_case_turns"],
            row["remaining_worst_case_energy"],
            row["remaining_worst_case_handoffs"],
        )
        assert recorded == remaining
        assert row["action"] == selected
        assert row["optimal_action_count"] == tie_count

        turn_charge = energy_charge = 0
        if selected is not None:
            turn_charge, energy_charge, _ = engine.cost(state, selected)
        derived_branches = [
            {
                "observation": observation,
                "next_node_id": engine.identifier(
                    successor,
                    turn_room - turn_charge,
                    energy_room - energy_charge,
                ),
            }
            for observation, successor in branches
        ]
        assert row["outcomes"] == derived_branches
        assert all(branch["next_node_id"] in node_index for branch in derived_branches)


def write_unseen_incident(destination):
    """Materialize a schema-compatible rescue case not present in the image."""
    incident = read_json(INPUT / "arenas" / "rescue_02.json")
    incident["arena_id"] = "private_contingency"
    places = [row["node_id"] for row in incident["nodes"]]
    team_a, team_b = incident["teams"]
    team_a["start_node"], team_b["start_node"] = places[2], places[-2]
    team_a["scan_energy"] += 2
    team_b["move_energy_multiplier"] += 1

    relocated = (places[-1], places[-3], places[4], places[6])
    for victim, place in zip(incident["victims"], relocated, strict=True):
        victim["node"] = place

    modes = incident["mode_ids"]
    mode_position = {mode: index for index, mode in enumerate(modes)}
    for number, road in enumerate(incident["edges"]):
        road["energy_cost"] += 1 + number % 2
        if len(road["safe_modes"]) < len(modes):
            road["safe_modes"] = sorted(
                modes[(mode_position[mode] + 1 + number % 2) % len(modes)]
                for mode in road["safe_modes"]
            )

    for number, place in enumerate(incident["nodes"]):
        signals = place["signals"]
        if signals is not None:
            place["signals"] = {
                mode: signals[modes[-1 - mode_position[mode]]] + f"_P{number % 2}"
                for mode in modes
            }

    arena_directory = destination / "arenas"
    arena_directory.mkdir(parents=True)
    payloads = {
        arena_directory / "private_contingency.json": incident,
        destination / "manifest.json": {
            "schema_version": 1,
            "arenas": [
                {
                    "arena_id": "private_contingency",
                    "file": "arenas/private_contingency.json",
                }
            ],
        },
    }
    for path, payload in payloads.items():
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    shutil.copy2(INPUT / "rules.json", destination / "rules.json")
    return incident


def arena_engine_pairs():
    """Pair each submitted arena record with its independent replay engine."""
    _, engines = oracle_policy()
    return [
        (record, engines[record["arena_id"]])
        for record in candidate_policy()["arenas"]
    ]


def remaining_value(context_row):
    """Read the three trace maxima recorded at one policy context."""
    return tuple(
        context_row[field]
        for field in (
            "remaining_worst_case_turns",
            "remaining_worst_case_energy",
            "remaining_worst_case_handoffs",
        )
    )


def test_required_artifacts_are_regular_parseable_files():
    """The policy and reusable solver exist as ordinary top-level artifacts."""
    assert ordinary_file(POLICY_PATH)
    assert ordinary_file(SOLVER_PATH)
    assert isinstance(candidate_policy(), dict)
    compile(SOLVER_PATH.read_text(), str(SOLVER_PATH), "exec")


def test_generated_input_tree_is_immutable_and_hash_locked():
    """Every agent-visible arena and rule file retains its generated bytes."""
    expected = json.loads((TESTS / "expected.json").read_text())["input_sha256"]
    found = {
        path.relative_to(INPUT).as_posix(): file_sha256(path)
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
    for record, arena in zip(manifest["arenas"], arena_inputs(), strict=True):
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
    for arena in arena_inputs():
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
    document, _ = oracle_policy()
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


def test_policy_serialization_has_the_exact_typed_contract():
    """Reject Boolean integers, extra keys, bad ordering, and malformed contexts."""
    policy = candidate_policy()
    assert set(policy) == DOCUMENT_KEYS
    assert type(policy["schema_version"]) is int and policy["schema_version"] == 1

    manifest_order = [
        item["arena_id"] for item in read_json(INPUT / "manifest.json")["arenas"]
    ]
    assert [item["arena_id"] for item in policy["arenas"]] == manifest_order

    summary_integers = (
        "worst_case_turns",
        "worst_case_energy",
        "worst_case_handoffs",
        "policy_node_count",
    )
    context_integers = (
        "turn_budget",
        "energy_budget",
        "remaining_worst_case_turns",
        "remaining_worst_case_energy",
        "remaining_worst_case_handoffs",
        "optimal_action_count",
    )
    for report in policy["arenas"]:
        assert set(report) == ARENA_KEYS
        assert all(type(report[field]) is int for field in summary_integers)
        assert all(report[field] >= 0 for field in summary_integers)
        assert len(report["policy_sha256"]) == 64
        identifiers = [row["node_id"] for row in report["nodes"]]
        assert identifiers == sorted(identifiers)

        for context in report["nodes"]:
            assert set(context) == CONTEXT_KEYS
            assert context["last_actor"] in (None, "A", "B")
            assert all(type(context[field]) is int for field in context_integers)
            assert all(context[field] >= 0 for field in context_integers)
            assert context["optimal_action_count"] >= 1
            assert context["remaining_worst_case_turns"] <= context["turn_budget"]
            assert context["remaining_worst_case_energy"] <= context["energy_budget"]
            observations = [branch["observation"] for branch in context["outcomes"]]
            assert observations == sorted(observations)
            assert all(set(branch) == BRANCH_KEYS for branch in context["outcomes"])


def test_arena_summaries_and_complete_policy_values_match_independent_model():
    """Every root value, action, local value, multiplicity, and observation target is pinned."""
    expected, _ = oracle_policy()
    assert candidate_policy() == expected


def test_context_hashes_root_identity_and_policy_digest_are_rederived():
    """Bind every identifier to state plus envelopes and bind each arena to its nodes."""
    for report, engine in arena_engine_pairs():
        initial_state = engine.initial()
        initial_context = initial_state, *engine.initial_budgets(initial_state)
        assert report["root_node_id"] == engine.identifier(*initial_context)
        assert report["policy_node_count"] == len(report["nodes"])
        digest = hashlib.sha256(encoded(report["nodes"]).encode()).hexdigest()
        assert report["policy_sha256"] == digest
        for context in report["nodes"]:
            decoded = engine.context_from_json(context)
            assert context["node_id"] == engine.identifier(*decoded)
        engine.clear_caches()


def test_actions_replay_every_mode_partition_into_the_reported_successors():
    """Re-execute each move or scan and account for its turn and energy charges."""
    for report, engine in arena_engine_pairs():
        identifiers = {context["node_id"] for context in report["nodes"]}
        for context in report["nodes"]:
            state, turn_room, energy_room = engine.context_from_json(context)
            if engine.goal(state):
                assert context["action"] is None
                assert context["outcomes"] == []
                continue
            action = context["action"]
            assert action in engine.available(state)
            turn_charge, energy_charge, _ = engine.cost(state, action)
            replayed = [
                {
                    "observation": observation,
                    "next_node_id": engine.identifier(
                        successor,
                        turn_room - turn_charge,
                        energy_room - energy_charge,
                    ),
                }
                for observation, successor in engine.advance(state, action)
            ]
            assert context["outcomes"] == replayed
            assert all(branch["next_node_id"] in identifiers for branch in replayed)
        engine.clear_caches()


def test_incident_policy_is_closed_and_descends_to_rescue_on_every_observation():
    """Walk the graph and prove resource descent, reachability, and terminal rescue."""
    for report, engine in arena_engine_pairs():
        contexts = {row["node_id"]: row for row in report["nodes"]}
        visited = set()
        frontier = deque([report["root_node_id"]])
        while frontier:
            identifier = frontier.popleft()
            if identifier in visited:
                continue
            visited.add(identifier)
            context = contexts[identifier]
            state, turn_room, energy_room = engine.context_from_json(context)
            if engine.goal(state):
                assert remaining_value(context) == (0, 0, 0)
                continue
            turn_charge, energy_charge, _ = engine.cost(state, context["action"])
            for branch in context["outcomes"]:
                successor = contexts[branch["next_node_id"]]
                assert successor["turn_budget"] == turn_room - turn_charge
                assert successor["energy_budget"] == energy_room - energy_charge
                assert successor["remaining_worst_case_turns"] <= (
                    context["remaining_worst_case_turns"] - turn_charge
                )
                frontier.append(branch["next_node_id"])
        assert visited == set(contexts)
        engine.clear_caches()


def test_each_residual_envelope_uses_the_canonical_handoff_minimizer():
    """Pin the contextual action, tie multiplicity, and selected subtree maxima."""
    for report, engine in arena_engine_pairs():
        for row in report["nodes"]:
            context = engine.context_from_json(row)
            expected_value = engine.contextual_value(*context)
            _, expected_action, _, expected_count = engine.choice(*context)
            assert row["action"] == expected_action
            assert row["optimal_action_count"] == expected_count
            assert remaining_value(row) == expected_value
        engine.clear_caches()


def test_budgeted_lexicographic_oracle_preserves_turn_slack_for_energy():
    """A slower child policy may minimize global energy inside its inherited turn cap."""
    arena = {
        "arena_id": "branch_slack_regression",
        "mode_ids": ["M0", "M1", "M2"],
        "teams": [
            {
                "team_id": "A",
                "start_node": "S",
                "move_energy_multiplier": 1,
                "scan_energy": 1,
            },
            {
                "team_id": "B",
                "start_node": "S",
                "move_energy_multiplier": 100,
                "scan_energy": 100,
            },
        ],
        "victims": [{"victim_id": "V0", "node": "V"}],
        "nodes": [
            {
                "node_id": "S",
                "signals": {"M0": "ZERO", "M1": "ONE", "M2": "TWO"},
            },
            {"node_id": "X", "signals": None},
            {"node_id": "V", "signals": None},
        ],
        "edges": [
            {
                "edge_id": "E000",
                "a": "S",
                "b": "V",
                "turn_cost": 1,
                "energy_cost": 10,
                "safe_modes": ["M0"],
            },
            {
                "edge_id": "E001",
                "a": "S",
                "b": "X",
                "turn_cost": 1,
                "energy_cost": 1,
                "safe_modes": ["M0"],
            },
            {
                "edge_id": "E002",
                "a": "X",
                "b": "V",
                "turn_cost": 1,
                "energy_cost": 1,
                "safe_modes": ["M0"],
            },
            {
                "edge_id": "E003",
                "a": "S",
                "b": "V",
                "turn_cost": 2,
                "energy_cost": 5,
                "safe_modes": ["M1"],
            },
            {
                "edge_id": "E004",
                "a": "S",
                "b": "V",
                "turn_cost": 2,
                "energy_cost": 5,
                "safe_modes": ["M2"],
            },
        ],
        "max_worst_case_turns": 6,
    }
    engine = VerifierEngine(arena)
    root = engine.initial()
    assert engine.initial_budgets(root) == (3, 6)
    assert engine.choice(root, 3, 6)[1] == "A:SCAN"
    assert engine.contextual_value(root, 3, 6) == (3, 6, 0)

    m0_child = dict(engine.advance(root, "A:SCAN"))["ZERO"]
    assert engine.least_energy(m0_child, 1) == 10
    assert engine.least_energy(m0_child, 2) == 2
    assert engine.choice(m0_child, 2, 2)[1] == "A:MOVE:E001"


def test_reusable_solver_generalizes_to_private_contingency(tmp_path):
    """Fixed published policies fail when starts, rescues, observations, modes, and costs change."""
    hidden_input = tmp_path / "hidden-input"
    hidden_output = tmp_path / "hidden-output"
    hidden_arena = write_unseen_incident(hidden_input)
    expected, _ = calculate(hidden_input)
    published = next(
        arena for arena in candidate_policy()["arenas"] if arena["arena_id"] == "rescue_02"
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
    PolicyAudit(actual, hidden_input).run()


def test_complete_value_level_validator_accepts_published_oracle():
    """The reusable full validator covers the published output without special paths."""
    PolicyAudit(candidate_policy(), INPUT).run()
