#!/usr/bin/env python3
"""Generate deterministic partially observable rescue-network instances."""
from __future__ import annotations

import argparse
import hashlib
import json
import random
from pathlib import Path


INSTANCE_COUNT = 10
BASE_SEED = 0x5EEDC0DE


def dump(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def stable_number(*parts: object) -> int:
    payload = "|".join(map(str, parts)).encode()
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def victim_nodes(node_count: int, starts: set[int], seed: int) -> list[int]:
    order = sorted(
        (node for node in range(node_count) if node not in starts),
        key=lambda node: stable_number(seed, "victim", node),
    )
    return sorted(order[:4])


def add_edge(
    edges: dict[tuple[int, int], dict],
    a: int,
    b: int,
    turn_cost: int,
    energy_cost: int,
    safe_modes: list[str],
) -> None:
    key = tuple(sorted((a, b)))
    if key in edges:
        return
    edges[key] = {
        "a": f"N{key[0]:02d}",
        "b": f"N{key[1]:02d}",
        "turn_cost": turn_cost,
        "energy_cost": energy_cost,
        "safe_modes": sorted(safe_modes),
    }


def build_arena(index: int) -> dict:
    seed = BASE_SEED + 104729 * index
    rng = random.Random(seed)
    node_count = 10 + index % 4
    mode_count = 6 + index % 3
    mode_ids = [f"M{number:02d}" for number in range(mode_count)]
    start_a = index % 2
    start_b = node_count // 2 + index % 2
    victims = victim_nodes(node_count, {start_a, start_b}, seed)

    edges: dict[tuple[int, int], dict] = {}
    # A universally traversable but deliberately expensive cycle guarantees that every
    # hidden mode is winnable. Conditional chords make the minimax optimum non-trivial.
    for node in range(node_count):
        other = (node + 1) % node_count
        add_edge(
            edges,
            node,
            other,
            2 + stable_number(seed, "ring-turn", node) % 3,
            3 + stable_number(seed, "ring-energy", node) % 6,
            mode_ids,
        )

    chord_pairs = set()
    for offset in (3, 4, 5):
        for node in range(node_count):
            other = (node + offset) % node_count
            pair = tuple(sorted((node, other)))
            if pair[0] == pair[1] or pair in chord_pairs:
                continue
            chord_pairs.add(pair)
            edge_index = len(chord_pairs)
            safe = [
                mode_id
                for mode_number, mode_id in enumerate(mode_ids)
                if stable_number(seed, "safe", edge_index, mode_number) % 7 < 4
            ]
            if not safe:
                safe = [mode_ids[edge_index % mode_count]]
            if len(safe) == mode_count:
                safe.pop(edge_index % mode_count)
            add_edge(
                edges,
                pair[0],
                pair[1],
                1 + stable_number(seed, "chord-turn", edge_index) % 2,
                1 + stable_number(seed, "chord-energy", edge_index) % 5,
                safe,
            )
            if len(chord_pairs) >= node_count + 5:
                break
        if len(chord_pairs) >= node_count + 5:
            break

    edge_rows = []
    for edge_number, (_, edge) in enumerate(sorted(edges.items())):
        edge_rows.append({"edge_id": f"E{edge_number:03d}", **edge})

    sensor_nodes = {
        start_a,
        start_b,
        node_count // 3,
        (2 * node_count) // 3,
    }
    nodes = []
    for node in range(node_count):
        if node not in sensor_nodes:
            signals = None
        else:
            sensor_rank = sorted(sensor_nodes).index(node)
            divisor = (1, 2, 3, 4)[sensor_rank % 4]
            alphabet = 2 + (sensor_rank + index) % 3
            signals = {
                mode_id: f"S{sensor_rank}_{(mode_number // divisor) % alphabet}"
                for mode_number, mode_id in enumerate(mode_ids)
            }
        nodes.append({"node_id": f"N{node:02d}", "signals": signals})

    return {
        "schema_version": 1,
        "arena_id": f"rescue_{index + 1:02d}",
        "mode_ids": mode_ids,
        "teams": [
            {
                "team_id": "A",
                "start_node": f"N{start_a:02d}",
                "move_energy_multiplier": 1 + index % 2,
                "scan_energy": 3 + index % 3,
            },
            {
                "team_id": "B",
                "start_node": f"N{start_b:02d}",
                "move_energy_multiplier": 2 - index % 2,
                "scan_energy": 4 + (index + 1) % 3,
            },
        ],
        "victims": [
            {"victim_id": f"V{number}", "node": f"N{node:02d}"}
            for number, node in enumerate(victims)
        ],
        "nodes": nodes,
        "edges": edge_rows,
        "max_worst_case_turns": 84,
        "generation_seed": seed,
    }


def rules() -> dict:
    return {
        "schema_version": 1,
        "action_order": "team A before B; SCAN before MOVE; MOVE edges by edge_id",
        "move_observations": ["ARRIVED", "BLOCKED"],
        "objective_order": [
            "worst_case_turns",
            "worst_case_energy",
            "worst_case_handoffs",
            "canonical_action_order",
        ],
        "goal": "every victim has been reached by at least one team",
        "policy_context": "physical belief state plus remaining turn and energy budgets",
        "budget_transition": "each observation child receives both parent budgets minus the action costs",
        "policy_node_id": "n_ plus the first 24 hex digits of SHA-256 over canonical context JSON",
        "policy_digest": "SHA-256 over canonical compact JSON of the node array",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("/app"))
    arguments = parser.parse_args()
    input_root = arguments.root / "input"
    arena_root = input_root / "arenas"
    arena_root.mkdir(parents=True, exist_ok=True)
    records = []
    for index in range(INSTANCE_COUNT):
        arena = build_arena(index)
        relative = f"arenas/{arena['arena_id']}.json"
        dump(input_root / relative, arena)
        records.append({"arena_id": arena["arena_id"], "file": relative})
    dump(input_root / "manifest.json", {"schema_version": 1, "arenas": records})
    dump(input_root / "rules.json", rules())


if __name__ == "__main__":
    main()
