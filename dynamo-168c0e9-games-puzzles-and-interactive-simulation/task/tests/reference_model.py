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
    def within(self, state, allowance):
        if self.goal(state):
            return (0, 0, 0), None, (), 1
        if allowance <= 0:
            return None
        chosen = None
        chosen_action = None
        chosen_branches = ()
        ties = 0
        for action in self.available(state):
            time, energy, handoff = self.cost(state, action)
            if time > allowance:
                continue
            branches = self.advance(state, action)
            children = [self.within(child, allowance - time) for _, child in branches]
            if not branches or any(child is None for child in children):
                continue
            values = [child[0] for child in children]
            value = (
                time + max(item[0] for item in values),
                energy + max(item[1] for item in values),
                handoff + max(item[2] for item in values),
            )
            if value[0] > allowance:
                continue
            if chosen is None or value < chosen:
                chosen = value
                chosen_action = action
                chosen_branches = branches
                ties = 1
            elif value == chosen:
                ties += 1
        if chosen is None:
            return None
        return chosen, chosen_action, chosen_branches, ties

    @cache
    def exact(self, state):
        for allowance in range(self.limit + 1):
            answer = self.within(state, allowance)
            if answer is not None:
                return answer
        raise AssertionError(f"{self.source['arena_id']} is outside its strong-policy bound")

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

    def identifier(self, state):
        return "n_" + hashlib.sha256(encoded(self.state_json(state)).encode()).hexdigest()[:24]

    def document(self):
        root = self.initial()
        root_value = self.exact(root)[0]
        pending = deque([root])
        visited = set()
        rows = []
        while pending:
            state = pending.popleft()
            if state in visited:
                continue
            visited.add(state)
            value, action, branches, count = self.exact(state)
            outcomes = []
            for observation, child in branches:
                outcomes.append(
                    {"observation": observation, "next_node_id": self.identifier(child)}
                )
                pending.append(child)
            rows.append(
                {
                    "node_id": self.identifier(state),
                    **self.state_json(state),
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
            "root_node_id": self.identifier(root),
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
    return {"schema_version": 1, "arenas": arenas}, engines
