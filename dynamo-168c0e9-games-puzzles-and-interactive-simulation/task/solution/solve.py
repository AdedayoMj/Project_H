#!/usr/bin/env python3
"""Synthesize canonical minimax policies for partially observable rescue networks."""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict, deque
from functools import lru_cache
from pathlib import Path


DEFAULT_INPUT = Path("/app/input")
DEFAULT_OUTPUT = Path("/app/output")


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class PolicyEngine:
    def __init__(self, arena: dict):
        self.arena = arena
        self.arena_id = arena["arena_id"]
        self.mode_ids = tuple(arena["mode_ids"])
        self.mode_index = {mode_id: index for index, mode_id in enumerate(self.mode_ids)}
        self.all_modes = (1 << len(self.mode_ids)) - 1
        self.teams = {row["team_id"]: row for row in arena["teams"]}
        self.nodes = {row["node_id"]: row for row in arena["nodes"]}
        self.victims = tuple(row["victim_id"] for row in arena["victims"])
        self.victim_index = {
            victim_id: index for index, victim_id in enumerate(self.victims)
        }
        self.victims_at = defaultdict(int)
        for row in arena["victims"]:
            self.victims_at[row["node"]] |= 1 << self.victim_index[row["victim_id"]]
        self.all_victims = (1 << len(self.victims)) - 1
        self.edges = {row["edge_id"]: row for row in arena["edges"]}
        self.incident = defaultdict(list)
        self.safe_masks = {}
        for edge in arena["edges"]:
            self.incident[edge["a"]].append(edge["edge_id"])
            self.incident[edge["b"]].append(edge["edge_id"])
            mask = 0
            for mode_id in edge["safe_modes"]:
                mask |= 1 << self.mode_index[mode_id]
            self.safe_masks[edge["edge_id"]] = mask
        for values in self.incident.values():
            values.sort()
        self.max_turns = int(arena["max_worst_case_turns"])

    def starting_state(self) -> tuple[str, str, int, int, int]:
        a = self.teams["A"]["start_node"]
        b = self.teams["B"]["start_node"]
        rescued = self.victims_at[a] | self.victims_at[b]
        return a, b, rescued, self.all_modes, 0

    def is_goal(self, state: tuple[str, str, int, int, int]) -> bool:
        return state[2] == self.all_victims

    def action_key(self, action: str) -> tuple[int, int, str]:
        team_id, kind, *remainder = action.split(":")
        return (0 if team_id == "A" else 1, 0 if kind == "SCAN" else 1, remainder[0] if remainder else "")

    @lru_cache(maxsize=None)
    def actions(self, state: tuple[str, str, int, int, int]) -> tuple[str, ...]:
        if self.is_goal(state):
            return ()
        result = []
        for team_number, team_id in enumerate(("A", "B")):
            position = state[team_number]
            if self.nodes[position]["signals"] is not None:
                result.append(f"{team_id}:SCAN")
            result.extend(
                f"{team_id}:MOVE:{edge_id}" for edge_id in self.incident[position]
            )
        return tuple(sorted(result, key=self.action_key))

    def action_cost(
        self, state: tuple[str, str, int, int, int], action: str
    ) -> tuple[int, int, int]:
        team_id, kind, *remainder = action.split(":")
        actor = 1 if team_id == "A" else 2
        handoff = int(state[4] not in (0, actor))
        if kind == "SCAN":
            return 1, int(self.teams[team_id]["scan_energy"]), handoff
        edge = self.edges[remainder[0]]
        energy = int(edge["energy_cost"]) * int(
            self.teams[team_id]["move_energy_multiplier"]
        )
        return int(edge["turn_cost"]), energy, handoff

    @lru_cache(maxsize=None)
    def outcomes(
        self, state: tuple[str, str, int, int, int], action: str
    ) -> tuple[tuple[str, tuple[str, str, int, int, int]], ...]:
        a, b, rescued, belief, _ = state
        team_id, kind, *remainder = action.split(":")
        team_number = 0 if team_id == "A" else 1
        actor = 1 if team_id == "A" else 2
        position = a if team_number == 0 else b
        partitions = defaultdict(int)
        if kind == "SCAN":
            signals = self.nodes[position]["signals"]
            if signals is None:
                raise ValueError(f"scan unavailable at {position}")
            for mode_number, mode_id in enumerate(self.mode_ids):
                bit = 1 << mode_number
                if belief & bit:
                    partitions[str(signals[mode_id])] |= bit
            return tuple(
                (observation, (a, b, rescued, mask, actor))
                for observation, mask in sorted(partitions.items())
            )

        edge_id = remainder[0]
        edge = self.edges[edge_id]
        if position not in (edge["a"], edge["b"]):
            raise ValueError(f"{edge_id} is not incident to {position}")
        destination = edge["b"] if position == edge["a"] else edge["a"]
        safe_mask = self.safe_masks[edge_id]
        arrived = belief & safe_mask
        blocked = belief & ~safe_mask
        result = []
        if arrived:
            next_a, next_b = (destination, b) if team_number == 0 else (a, destination)
            result.append(
                (
                    "ARRIVED",
                    (
                        next_a,
                        next_b,
                        rescued | self.victims_at[destination],
                        arrived,
                        actor,
                    ),
                )
            )
        if blocked:
            result.append(("BLOCKED", (a, b, rescued, blocked, actor)))
        return tuple(sorted(result))

    @lru_cache(maxsize=None)
    def bounded(
        self, state: tuple[str, str, int, int, int], turn_budget: int
    ) -> tuple[tuple[int, int, int], str | None, tuple, int] | None:
        if self.is_goal(state):
            return (0, 0, 0), None, (), 1
        if turn_budget <= 0:
            return None
        best_value = None
        best_action = None
        best_outcomes = ()
        optimal_action_count = 0
        for action in self.actions(state):
            turn_cost, energy_cost, handoff_cost = self.action_cost(state, action)
            if turn_cost > turn_budget:
                continue
            transitions = self.outcomes(state, action)
            child_results = [
                self.bounded(child, turn_budget - turn_cost)
                for _, child in transitions
            ]
            if not transitions or any(result is None for result in child_results):
                continue
            child_values = [result[0] for result in child_results]
            candidate = (
                turn_cost + max(value[0] for value in child_values),
                energy_cost + max(value[1] for value in child_values),
                handoff_cost + max(value[2] for value in child_values),
            )
            if candidate[0] > turn_budget:
                continue
            if best_value is None or candidate < best_value:
                best_value = candidate
                best_action = action
                best_outcomes = transitions
                optimal_action_count = 1
            elif candidate == best_value:
                optimal_action_count += 1
        if best_value is None:
            return None
        return best_value, best_action, best_outcomes, optimal_action_count

    @lru_cache(maxsize=None)
    def optimal(
        self, state: tuple[str, str, int, int, int]
    ) -> tuple[tuple[int, int, int], str | None, tuple, int]:
        for turn_budget in range(self.max_turns + 1):
            result = self.bounded(state, turn_budget)
            if result is not None:
                return result
        raise RuntimeError(
            f"{self.arena_id} has no strong rescue policy inside the published bound"
        )

    def state_document(self, state: tuple[str, str, int, int, int]) -> dict:
        a, b, rescued, belief, last_actor = state
        return {
            "team_a_node": a,
            "team_b_node": b,
            "rescued_victims": [
                victim_id
                for index, victim_id in enumerate(self.victims)
                if rescued & (1 << index)
            ],
            "possible_modes": [
                mode_id
                for index, mode_id in enumerate(self.mode_ids)
                if belief & (1 << index)
            ],
            "last_actor": {0: None, 1: "A", 2: "B"}[last_actor],
        }

    def node_id(self, state: tuple[str, str, int, int, int]) -> str:
        digest = hashlib.sha256(canonical_json(self.state_document(state)).encode()).hexdigest()
        return "n_" + digest[:24]

    def synthesize(self) -> dict:
        root = self.starting_state()
        root_value = self.optimal(root)[0]
        queue = deque([root])
        seen = set()
        nodes = []
        identities = {}
        while queue:
            state = queue.popleft()
            if state in seen:
                continue
            seen.add(state)
            value, action, transitions, action_count = self.optimal(state)
            state_fields = self.state_document(state)
            identifier = self.node_id(state)
            if identifier in identities and identities[identifier] != state_fields:
                raise RuntimeError("policy-node digest collision")
            identities[identifier] = state_fields
            outcome_rows = []
            for observation, child in transitions:
                child_id = self.node_id(child)
                outcome_rows.append(
                    {"observation": observation, "next_node_id": child_id}
                )
                queue.append(child)
            nodes.append(
                {
                    "node_id": identifier,
                    **state_fields,
                    "remaining_worst_case_turns": value[0],
                    "remaining_worst_case_energy": value[1],
                    "remaining_worst_case_handoffs": value[2],
                    "optimal_action_count": action_count,
                    "action": action,
                    "outcomes": outcome_rows,
                }
            )
        nodes.sort(key=lambda row: row["node_id"].encode())
        policy_digest = hashlib.sha256(canonical_json(nodes).encode()).hexdigest()
        return {
            "arena_id": self.arena_id,
            "root_node_id": self.node_id(root),
            "worst_case_turns": root_value[0],
            "worst_case_energy": root_value[1],
            "worst_case_handoffs": root_value[2],
            "policy_node_count": len(nodes),
            "policy_sha256": policy_digest,
            "nodes": nodes,
        }


def solve_root(input_root: Path) -> dict:
    manifest = json.loads((input_root / "manifest.json").read_text())
    arenas = []
    for record in manifest["arenas"]:
        arena = json.loads((input_root / record["file"]).read_text())
        arenas.append(PolicyEngine(arena).synthesize())
    return {"schema_version": 1, "arenas": arenas}


def main(input_root: Path, output_root: Path) -> None:
    output_root.mkdir(parents=True, exist_ok=True)
    document = solve_root(input_root)
    (output_root / "rescue_policy.json").write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-root", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT)
    arguments = parser.parse_args()
    main(arguments.input_root, arguments.output_root)
