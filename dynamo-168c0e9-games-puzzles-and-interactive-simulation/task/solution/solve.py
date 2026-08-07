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
    def minimum_energy(
        self, state: tuple[str, str, int, int, int], turn_budget: int
    ) -> int | None:
        """Return the least worst-case energy among policies inside a turn cap."""
        if turn_budget < 0:
            return None
        if self.is_goal(state):
            return 0
        best = None
        for action in self.actions(state):
            turn_cost, energy_cost, _ = self.action_cost(state, action)
            if turn_cost > turn_budget:
                continue
            transitions = self.outcomes(state, action)
            child_energy = [
                self.minimum_energy(child, turn_budget - turn_cost)
                for _, child in transitions
            ]
            if not transitions or any(value is None for value in child_energy):
                continue
            candidate = energy_cost + max(child_energy)
            if best is None or candidate < best:
                best = candidate
        return best

    def root_budgets(
        self, state: tuple[str, str, int, int, int]
    ) -> tuple[int, int]:
        """Find the lexicographically minimal turn and energy bounds."""
        for turn_budget in range(self.max_turns + 1):
            energy_budget = self.minimum_energy(state, turn_budget)
            if energy_budget is not None:
                return turn_budget, energy_budget
        raise RuntimeError(
            f"{self.arena_id} has no strong rescue policy inside the published bound"
        )

    @lru_cache(maxsize=None)
    def minimum_handoffs(
        self,
        state: tuple[str, str, int, int, int],
        turn_budget: int,
        energy_budget: int,
    ) -> int | None:
        """Minimize worst-case handoffs while respecting both earlier-objective caps."""
        if turn_budget < 0 or energy_budget < 0:
            return None
        if self.is_goal(state):
            return 0
        least_energy = self.minimum_energy(state, turn_budget)
        if least_energy is None or least_energy > energy_budget:
            return None
        best = None
        for action in self.actions(state):
            turn_cost, energy_cost, handoff_cost = self.action_cost(state, action)
            if turn_cost > turn_budget or energy_cost > energy_budget:
                continue
            transitions = self.outcomes(state, action)
            child_handoffs = [
                self.minimum_handoffs(
                    child,
                    turn_budget - turn_cost,
                    energy_budget - energy_cost,
                )
                for _, child in transitions
            ]
            if not transitions or any(value is None for value in child_handoffs):
                continue
            candidate = handoff_cost + max(child_handoffs)
            if best is None or candidate < best:
                best = candidate
        return best

    @lru_cache(maxsize=None)
    def decision(
        self,
        state: tuple[str, str, int, int, int],
        turn_budget: int,
        energy_budget: int,
    ) -> tuple[int, str | None, tuple, int]:
        """Choose the canonical handoff-optimal action inside a budget context."""
        if self.is_goal(state):
            return 0, None, (), 1
        optimum = self.minimum_handoffs(state, turn_budget, energy_budget)
        if optimum is None:
            raise RuntimeError("encountered an infeasible policy context")
        chosen_action = None
        chosen_outcomes = ()
        optimal_action_count = 0
        for action in self.actions(state):
            turn_cost, energy_cost, handoff_cost = self.action_cost(state, action)
            if turn_cost > turn_budget or energy_cost > energy_budget:
                continue
            transitions = self.outcomes(state, action)
            child_handoffs = [
                self.minimum_handoffs(
                    child,
                    turn_budget - turn_cost,
                    energy_budget - energy_cost,
                )
                for _, child in transitions
            ]
            if not transitions or any(value is None for value in child_handoffs):
                continue
            candidate = handoff_cost + max(child_handoffs)
            if candidate != optimum:
                continue
            optimal_action_count += 1
            if chosen_action is None:
                chosen_action = action
                chosen_outcomes = transitions
        return optimum, chosen_action, chosen_outcomes, optimal_action_count

    @lru_cache(maxsize=None)
    def policy_value(
        self,
        state: tuple[str, str, int, int, int],
        turn_budget: int,
        energy_budget: int,
    ) -> tuple[int, int, int]:
        """Compute the exact worst-case value of the selected contextual policy."""
        if self.is_goal(state):
            return 0, 0, 0
        _, action, transitions, _ = self.decision(
            state, turn_budget, energy_budget
        )
        turn_cost, energy_cost, handoff_cost = self.action_cost(state, action)
        child_values = [
            self.policy_value(
                child,
                turn_budget - turn_cost,
                energy_budget - energy_cost,
            )
            for _, child in transitions
        ]
        return (
            turn_cost + max(value[0] for value in child_values),
            energy_cost + max(value[1] for value in child_values),
            handoff_cost + max(value[2] for value in child_values),
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

    def context_document(
        self,
        state: tuple[str, str, int, int, int],
        turn_budget: int,
        energy_budget: int,
    ) -> dict:
        return {
            **self.state_document(state),
            "turn_budget": turn_budget,
            "energy_budget": energy_budget,
        }

    def node_id(
        self,
        state: tuple[str, str, int, int, int],
        turn_budget: int,
        energy_budget: int,
    ) -> str:
        digest = hashlib.sha256(
            canonical_json(
                self.context_document(state, turn_budget, energy_budget)
            ).encode()
        ).hexdigest()
        return "n_" + digest[:24]

    def clear_caches(self) -> None:
        """Release this arena's dynamic-programming tables before solving the next."""
        self.actions.cache_clear()
        self.outcomes.cache_clear()
        self.minimum_energy.cache_clear()
        self.minimum_handoffs.cache_clear()
        self.decision.cache_clear()
        self.policy_value.cache_clear()

    def synthesize(self) -> dict:
        root = self.starting_state()
        root_turn_budget, root_energy_budget = self.root_budgets(root)
        root_context = (root, root_turn_budget, root_energy_budget)
        root_value = self.policy_value(*root_context)
        if root_value[:2] != (root_turn_budget, root_energy_budget):
            raise RuntimeError("root policy failed to attain its optimal budgets")
        queue = deque([root_context])
        seen = set()
        nodes = []
        identities = {}
        while queue:
            state, turn_budget, energy_budget = queue.popleft()
            context = (state, turn_budget, energy_budget)
            if context in seen:
                continue
            seen.add(context)
            _, action, transitions, action_count = self.decision(*context)
            value = self.policy_value(*context)
            context_fields = self.context_document(*context)
            identifier = self.node_id(*context)
            if identifier in identities and identities[identifier] != context_fields:
                raise RuntimeError("policy-node digest collision")
            identities[identifier] = context_fields
            outcome_rows = []
            if action is not None:
                turn_cost, energy_cost, _ = self.action_cost(state, action)
                for observation, child in transitions:
                    child_context = (
                        child,
                        turn_budget - turn_cost,
                        energy_budget - energy_cost,
                    )
                    child_id = self.node_id(*child_context)
                    outcome_rows.append(
                        {"observation": observation, "next_node_id": child_id}
                    )
                    queue.append(child_context)
            nodes.append(
                {
                    "node_id": identifier,
                    **context_fields,
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
            "root_node_id": self.node_id(*root_context),
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
        engine = PolicyEngine(arena)
        arenas.append(engine.synthesize())
        engine.clear_caches()
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
