"""Independent verifier-side minimax model for the rescue-policy task."""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict, deque
from functools import cache
from pathlib import Path


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class VerifierEngine:
    def __init__(self, arena):
        self.source = arena
        self.modes = tuple(arena["mode_ids"])
        self.mode_number = {value: index for index, value in enumerate(self.modes)}
        self.teams = {row["team_id"]: row for row in arena["teams"]}
        self.places = {row["node_id"]: row for row in arena["nodes"]}
        self.edges = {row["edge_id"]: row for row in arena["edges"]}
        self.links = defaultdict(list)
        self.edge_mode_mask = {}
        for edge in arena["edges"]:
            self.links[edge["a"]].append(edge["edge_id"])
            self.links[edge["b"]].append(edge["edge_id"])
            self.edge_mode_mask[edge["edge_id"]] = sum(
                1 << self.mode_number[mode] for mode in edge["safe_modes"]
            )
        for edge_ids in self.links.values():
            edge_ids.sort()
        self.victim_ids = tuple(row["victim_id"] for row in arena["victims"])
        self.victim_number = {
            victim_id: index for index, victim_id in enumerate(self.victim_ids)
        }
        self.rescue_at = defaultdict(int)
        for victim in arena["victims"]:
            self.rescue_at[victim["node"]] |= 1 << self.victim_number[
                victim["victim_id"]
            ]
        self.complete_rescue = (1 << len(self.victim_ids)) - 1
        self.complete_belief = (1 << len(self.modes)) - 1
        self.limit = int(arena["max_worst_case_turns"])

    def initial(self):
        a = self.teams["A"]["start_node"]
        b = self.teams["B"]["start_node"]
        return a, b, self.rescue_at[a] | self.rescue_at[b], self.complete_belief, 0

    def goal(self, state):
        return state[2] == self.complete_rescue

    @staticmethod
    def order(action):
        team, kind, *tail = action.split(":")
        return team != "A", kind != "SCAN", tail[0] if tail else ""

    @cache
    def available(self, state):
        if self.goal(state):
            return ()
        found = []
        for offset, team in enumerate(("A", "B")):
            place = state[offset]
            if self.places[place]["signals"] is not None:
                found.append(f"{team}:SCAN")
            for edge_id in self.links[place]:
                found.append(f"{team}:MOVE:{edge_id}")
        return tuple(sorted(found, key=self.order))

    def cost(self, state, action):
        team, kind, *tail = action.split(":")
        actor = 1 if team == "A" else 2
        switch = int(state[4] not in (0, actor))
        if kind == "SCAN":
            return 1, int(self.teams[team]["scan_energy"]), switch
        edge = self.edges[tail[0]]
        return (
            int(edge["turn_cost"]),
            int(edge["energy_cost"])
            * int(self.teams[team]["move_energy_multiplier"]),
            switch,
        )

    @cache
    def advance(self, state, action):
        a, b, rescued, belief, _ = state
        team, kind, *tail = action.split(":")
        slot = 0 if team == "A" else 1
        actor = slot + 1
        place = state[slot]
        if kind == "SCAN":
            groups = defaultdict(int)
            signals = self.places[place]["signals"]
            for number, mode in enumerate(self.modes):
                if belief & (1 << number):
                    groups[str(signals[mode])] |= 1 << number
            return tuple(
                (signal, (a, b, rescued, mask, actor))
                for signal, mask in sorted(groups.items())
            )

        edge_id = tail[0]
        edge = self.edges[edge_id]
        destination = edge["b"] if place == edge["a"] else edge["a"]
        passing = belief & self.edge_mode_mask[edge_id]
        failing = belief & ~self.edge_mode_mask[edge_id]
        branches = []
        if passing:
            new_a, new_b = (destination, b) if slot == 0 else (a, destination)
            branches.append(
                (
                    "ARRIVED",
                    (
                        new_a,
                        new_b,
                        rescued | self.rescue_at[destination],
                        passing,
                        actor,
                    ),
                )
            )
        if failing:
            branches.append(("BLOCKED", (a, b, rescued, failing, actor)))
        return tuple(sorted(branches))

    @cache
    def least_energy(self, state, turns):
        if turns < 0:
            return None
        if self.goal(state):
            return 0
        candidates = []
        for action in self.available(state):
            elapsed, energy, _ = self.cost(state, action)
            if elapsed > turns:
                continue
            branches = self.advance(state, action)
            suffixes = [
                self.least_energy(child, turns - elapsed) for _, child in branches
            ]
            if branches and all(value is not None for value in suffixes):
                candidates.append(energy + max(suffixes))
        return min(candidates) if candidates else None

    def initial_budgets(self, state):
        for turns in range(self.limit + 1):
            energy = self.least_energy(state, turns)
            if energy is not None:
                return turns, energy
        raise AssertionError(f"{self.source['arena_id']} is outside its strong-policy bound")

    @cache
    def least_handoffs(self, state, turns, energy):
        if turns < 0 or energy < 0:
            return None
        if self.goal(state):
            return 0
        floor = self.least_energy(state, turns)
        if floor is None or floor > energy:
            return None
        candidates = []
        for action in self.available(state):
            elapsed, consumed, switch = self.cost(state, action)
            if elapsed > turns or consumed > energy:
                continue
            branches = self.advance(state, action)
            suffixes = [
                self.least_handoffs(
                    child, turns - elapsed, energy - consumed
                )
                for _, child in branches
            ]
            if branches and all(value is not None for value in suffixes):
                candidates.append(switch + max(suffixes))
        return min(candidates) if candidates else None

    @cache
    def choice(self, state, turns, energy):
        if self.goal(state):
            return 0, None, (), 1
        optimum = self.least_handoffs(state, turns, energy)
        assert optimum is not None
        winners = []
        for action in self.available(state):
            elapsed, consumed, switch = self.cost(state, action)
            if elapsed > turns or consumed > energy:
                continue
            branches = self.advance(state, action)
            suffixes = [
                self.least_handoffs(
                    child, turns - elapsed, energy - consumed
                )
                for _, child in branches
            ]
            if not branches or any(value is None for value in suffixes):
                continue
            if switch + max(suffixes) == optimum:
                winners.append((action, branches))
        action, branches = winners[0]
        return optimum, action, branches, len(winners)

    @cache
    def contextual_value(self, state, turns, energy):
        if self.goal(state):
            return 0, 0, 0
        _, action, branches, _ = self.choice(state, turns, energy)
        elapsed, consumed, switch = self.cost(state, action)
        suffixes = [
            self.contextual_value(
                child, turns - elapsed, energy - consumed
            )
            for _, child in branches
        ]
        return (
            elapsed + max(value[0] for value in suffixes),
            consumed + max(value[1] for value in suffixes),
            switch + max(value[2] for value in suffixes),
        )

    def state_json(self, state):
        a, b, rescued, belief, previous = state
        return {
            "last_actor": {0: None, 1: "A", 2: "B"}[previous],
            "possible_modes": [
                mode for number, mode in enumerate(self.modes) if belief & (1 << number)
            ],
            "rescued_victims": [
                victim
                for number, victim in enumerate(self.victim_ids)
                if rescued & (1 << number)
            ],
            "team_a_node": a,
            "team_b_node": b,
        }

    def state_from_json(self, row):
        rescued = sum(
            1 << self.victim_number[victim] for victim in row["rescued_victims"]
        )
        belief = sum(1 << self.mode_number[mode] for mode in row["possible_modes"])
        previous = {None: 0, "A": 1, "B": 2}[row["last_actor"]]
        return row["team_a_node"], row["team_b_node"], rescued, belief, previous

    def context_json(self, state, turns, energy):
        return {
            **self.state_json(state),
            "turn_budget": turns,
            "energy_budget": energy,
        }

    def context_from_json(self, row):
        return self.state_from_json(row), row["turn_budget"], row["energy_budget"]

    def identifier(self, state, turns, energy):
        return "n_" + hashlib.sha256(
            encoded(self.context_json(state, turns, energy)).encode()
        ).hexdigest()[:24]

    def clear_caches(self):
        self.available.cache_clear()
        self.advance.cache_clear()
        self.least_energy.cache_clear()
        self.least_handoffs.cache_clear()
        self.choice.cache_clear()
        self.contextual_value.cache_clear()

    def document(self):
        root = self.initial()
        root_turns, root_energy = self.initial_budgets(root)
        root_context = root, root_turns, root_energy
        root_value = self.contextual_value(*root_context)
        assert root_value[:2] == (root_turns, root_energy)
        pending = deque([root_context])
        visited = set()
        rows = []
        while pending:
            state, turns, energy = pending.popleft()
            context = state, turns, energy
            if context in visited:
                continue
            visited.add(context)
            value = self.contextual_value(*context)
            _, action, branches, count = self.choice(*context)
            outcomes = []
            if action is not None:
                elapsed, consumed, _ = self.cost(state, action)
                for observation, child in branches:
                    child_context = child, turns - elapsed, energy - consumed
                    outcomes.append(
                        {
                            "observation": observation,
                            "next_node_id": self.identifier(*child_context),
                        }
                    )
                    pending.append(child_context)
            rows.append(
                {
                    "node_id": self.identifier(*context),
                    **self.context_json(*context),
                    "remaining_worst_case_turns": value[0],
                    "remaining_worst_case_energy": value[1],
                    "remaining_worst_case_handoffs": value[2],
                    "optimal_action_count": count,
                    "action": action,
                    "outcomes": outcomes,
                }
            )
        rows.sort(key=lambda row: row["node_id"].encode())
        return {
            "arena_id": self.source["arena_id"],
            "root_node_id": self.identifier(*root_context),
            "worst_case_turns": root_value[0],
            "worst_case_energy": root_value[1],
            "worst_case_handoffs": root_value[2],
            "policy_node_count": len(rows),
            "policy_sha256": hashlib.sha256(encoded(rows).encode()).hexdigest(),
            "nodes": rows,
        }


def calculate(input_root: Path):
    manifest = json.loads((input_root / "manifest.json").read_text())
    arenas = []
    engines = {}
    for record in manifest["arenas"]:
        arena = json.loads((input_root / record["file"]).read_text())
        engine = VerifierEngine(arena)
        engines[arena["arena_id"]] = engine
        arenas.append(engine.document())
        engine.clear_caches()
    return {"schema_version": 1, "arenas": arenas}, engines
