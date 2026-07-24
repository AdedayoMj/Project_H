#!/usr/bin/env python3
"""Reference solver for the macrocycle isomer census task.

For each ring recipe, counts the exact number of substitution patterns
(assignments of substituents to the n ring positions) that use every unit
of the given composition, contain no forbidden adjacent pair on any
physical ring edge (including the wraparound edge), and are counted once
per orbit of the recipe's symmetry group (rotation-only cyclic recipes or
rotation-plus-reflection dihedral recipes).

Method: Burnside's lemma over the 2n elements of D_n. For a single group
element g, |Fix(g)| is computed via a "quotient graph" reduction: contract
each of g's position-cycles to one node; connect two nodes with an edge
whenever some physical ring edge (i, i+1 mod n) joins positions in those two
different cycles. Because g comes from the ring's own dihedral action, this
quotient graph is always exactly a single path or a single simple cycle over
g's cycles (never anything more complex) -- so |Fix(g)| reduces to one
generic content-constrained path/cycle coloring DP, with per-color budget
state and (only when the quotient graph is itself a cycle) a closing-edge
check against the first node visited.

A forbidden pair may name the same substituent twice ([X, X]: no two X on
adjacent positions). Such a self-pair is the one case where an edge INTERNAL
to a single g-cycle carries a real constraint: a coloring fixed by g is
constant on each cycle, so both endpoints of an internal edge get that cycle's
color, and if that color self-clashes the whole assignment is void. Each
g-cycle therefore also records whether it owns any internal physical edge
(a "self-loop"), and the DP rejects a self-clashing color on such a cycle.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

INPUT = Path("/app/input/ring_recipes.json")
OUTPUT = Path("/app/output.json")


def cycles_of_permutation(perm: list[int]) -> list[list[int]]:
    n = len(perm)
    seen = [False] * n
    cycles = []
    for start in range(n):
        if seen[start]:
            continue
        cycle, x = [], start
        while not seen[x]:
            seen[x] = True
            cycle.append(x)
            x = perm[x]
        cycles.append(cycle)
    return cycles


def quotient_graph(perm: list[int], n: int) -> tuple[list[int], dict[int, set[int]], set[int]]:
    """Cycles of perm become nodes; physical ring edges crossing cycle
    boundaries become quotient edges (deduped, undirected). An edge internal
    to a single cycle (both endpoints in the same cycle) marks that cycle as
    having a self-loop -- relevant only for self-clash pairs [X, X], since a
    coloring fixed by perm is constant on each cycle and two same-cycle
    neighbors therefore always share a color."""
    cycles = cycles_of_permutation(perm)
    cycle_of_pos = {pos: cid for cid, cycle in enumerate(cycles) for pos in cycle}

    adjacency: dict[int, set[int]] = {cid: set() for cid in range(len(cycles))}
    self_loops: set[int] = set()
    for i in range(n):
        j = (i + 1) % n
        ci, cj = cycle_of_pos[i], cycle_of_pos[j]
        if ci != cj:
            adjacency[ci].add(cj)
            adjacency[cj].add(ci)
        else:
            self_loops.add(ci)

    lengths = [len(cycle) for cycle in cycles]
    return lengths, adjacency, self_loops


def order_path_or_cycle(adjacency: dict[int, set[int]]) -> tuple[list[int], bool]:
    """Traverse the (guaranteed max-degree-2) quotient graph and return its
    node order plus whether it closes into a cycle."""
    k = len(adjacency)
    if k == 1:
        return [0], False

    degrees = {node: len(neighbors) for node, neighbors in adjacency.items()}
    assert all(d <= 2 for d in degrees.values()), "quotient graph must be max-degree-2"
    endpoints = [node for node, d in degrees.items() if d == 1]
    is_cycle = not endpoints
    start = 0 if is_cycle else endpoints[0]

    order, prev, cur = [start], None, start
    while len(order) < k:
        candidates = [x for x in adjacency[cur] if x != prev]
        # Only the very first step of a cycle traversal (prev is None) has
        # two candidates -- either direction is a valid walk; pick either.
        nxt = candidates[0]
        order.append(nxt)
        prev, cur = cur, nxt

    assert len(set(order)) == k, f"traversal did not cover all {k} quotient nodes cleanly: {order}"
    if is_cycle:
        assert order[0] in adjacency[order[-1]], "cycle traversal did not close back to its start"
    return order, is_cycle


def fix_count(
    perm: list[int], n: int, target_vector: tuple[int, ...], clash_indices: set[frozenset[int]]
) -> int:
    """|Fix(perm)|: colorings constant on perm's cycles that hit target_vector
    exactly and respect clash_indices on every physical ring edge. target_vector
    and clash_indices are recipe-wide constants (independent of which group
    element perm is) -- callers compute them once per recipe and reuse them
    across all 2n calls, rather than rebuilding them per element."""
    lengths, adjacency, self_loops = quotient_graph(perm, n)
    order, is_cycle = order_path_or_cycle(adjacency)
    k = len(order)
    m = len(target_vector)

    def edge_exists(a: int, b: int) -> bool:
        return b in adjacency[a]

    @lru_cache(maxsize=None)
    def dp(i: int, remaining: tuple[int, ...], prev_color: int, first_color: int) -> int:
        if i == k:
            if is_cycle and edge_exists(order[-1], order[0]):
                if frozenset((prev_color, first_color)) in clash_indices:
                    return 0
            return 1 if all(r == 0 for r in remaining) else 0

        node = order[i]
        length = lengths[node]
        needs_prev_check = i > 0 and edge_exists(order[i - 1], node)
        node_self_loops = node in self_loops

        total = 0
        for c in range(m):
            if remaining[c] < length:
                continue
            # Self-clash: a cycle that owns an internal edge cannot take a color
            # that is forbidden adjacent to itself ([c, c] in the clash table).
            if node_self_loops and frozenset((c, c)) in clash_indices:
                continue
            if needs_prev_check and frozenset((prev_color, c)) in clash_indices:
                continue
            new_remaining = list(remaining)
            new_remaining[c] -= length
            first = c if i == 0 else first_color
            total += dp(i + 1, tuple(new_remaining), c, first)
        return total

    result = dp(0, target_vector, -1, -1)
    dp.cache_clear()
    return result


def rotation_perm(n: int, d: int) -> list[int]:
    return [(i + d) % n for i in range(n)]


def reflection_perm(n: int, c: int) -> list[int]:
    return [(c - i) % n for i in range(n)]


def isomer_count(
    n: int, symmetry_group: str, composition: dict[str, int], forbidden_pairs: list[list[str]]
) -> int:
    assert sum(composition.values()) == n
    colors = sorted(composition)
    color_index = {c: i for i, c in enumerate(colors)}
    target_vector = tuple(composition[c] for c in colors)
    clash_indices = {
        frozenset((color_index[a], color_index[b]))
        for a, b in (tuple(pair) for pair in forbidden_pairs)
        if a in color_index and b in color_index
    }

    if symmetry_group == "cyclic":
        group_elements = [rotation_perm(n, d) for d in range(n)]
        group_size = n
    elif symmetry_group == "dihedral":
        group_elements = [rotation_perm(n, d) for d in range(n)] + [reflection_perm(n, c) for c in range(n)]
        group_size = 2 * n
    else:
        raise ValueError(f"unknown symmetry group: {symmetry_group!r}")

    total = sum(fix_count(perm, n, target_vector, clash_indices) for perm in group_elements)

    assert total % group_size == 0, f"Burnside sum {total} is not divisible by {group_size}"
    return total // group_size


def main() -> None:
    data = json.loads(INPUT.read_text())

    isomer_counts = {}
    for recipe in data["recipes"]:
        isomer_counts[recipe["recipe_id"]] = isomer_count(
            recipe["n_positions"],
            recipe.get("symmetry_group", "dihedral"),
            recipe["composition"],
            recipe["forbidden_adjacent_pairs"],
        )

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps({"isomer_counts": isomer_counts}, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
