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
from math import gcd
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
    perm: list[int],
    n: int,
    budgets: tuple[int, ...],
    mults: list[int],
    digit_tables: list[bytearray],
    allowed_next: tuple[tuple[int, ...], ...],
    allowed_mask: tuple[int, ...],
    self_forbidden: tuple[bool, ...],
    full_code: int,
) -> int:
    """|Fix(perm)| under the recipe's exact composition and clash rules.

    The quotient graph is always a path or a cycle. We process that graph
    iteratively with a mixed-radix encoding of the remaining composition
    state, which is much faster than the earlier recursive tuple memoization.
    """
    lengths, adjacency, self_loops = quotient_graph(perm, n)
    order, is_cycle = order_path_or_cycle(adjacency)
    ordered_lengths = [lengths[node] for node in order]
    ordered_self_loops = [node in self_loops for node in order]
    node_count = len(order)
    m = len(budgets)

    first_length = ordered_lengths[0]
    first_self_loop = ordered_self_loops[0]

    def run_from_start(start_color: int) -> int:
        if first_length > budgets[start_color]:
            return 0
        if first_self_loop and self_forbidden[start_color]:
            return 0

        states_by_last = [dict() for _ in range(m)]
        start_code = first_length * mults[start_color]
        states_by_last[start_color][start_code] = 1

        for idx in range(1, node_count):
            length = ordered_lengths[idx]
            node_self_loop = ordered_self_loops[idx]
            next_states = [dict() for _ in range(m)]

            for last_color in range(m):
                current_states = states_by_last[last_color]
                if not current_states:
                    continue
                next_colors = allowed_next[last_color]
                if node_self_loop:
                    next_colors = tuple(c for c in next_colors if not self_forbidden[c])

                for code, ways in current_states.items():
                    for c in next_colors:
                        if digit_tables[c][code] + length > budgets[c]:
                            continue
                        new_code = code + length * mults[c]
                        bucket = next_states[c]
                        bucket[new_code] = bucket.get(new_code, 0) + ways

            states_by_last = next_states

        total = 0
        if is_cycle:
            for last_color in range(m):
                if (allowed_mask[last_color] >> start_color) & 1:
                    total += states_by_last[last_color].get(full_code, 0)
        else:
            for last_color in range(m):
                total += states_by_last[last_color].get(full_code, 0)
        return total

    if is_cycle:
        return sum(run_from_start(start_color) for start_color in range(m))

    total = 0
    for start_color in range(m):
        total += run_from_start(start_color)
    return total


def rotation_perm(n: int, d: int) -> list[int]:
    return [(i + d) % n for i in range(n)]


def reflection_perm(n: int, c: int) -> list[int]:
    return [(c - i) % n for i in range(n)]


def build_recipe_tables(
    composition: dict[str, int], forbidden_pairs: list[list[str]]
) -> tuple[
    tuple[int, ...],
    list[int],
    list[bytearray],
    tuple[tuple[int, ...], ...],
    tuple[int, ...],
    tuple[bool, ...],
    int,
]:
    colors = sorted(composition)
    color_index = {c: i for i, c in enumerate(colors)}
    budgets = tuple(composition[c] for c in colors)
    bases = [budget + 1 for budget in budgets]

    mults = [1] * len(budgets)
    for i in range(1, len(budgets)):
        mults[i] = mults[i - 1] * bases[i - 1]

    state_count = 1
    for base in bases:
        state_count *= base

    digit_tables = [bytearray(state_count) for _ in budgets]
    for code in range(state_count):
        x = code
        for i, base in enumerate(bases):
            digit_tables[i][code] = x % base
            x //= base

    forbidden_mask = [0] * len(budgets)
    for a, b in forbidden_pairs:
        if a in color_index and b in color_index:
            ia = color_index[a]
            ib = color_index[b]
            forbidden_mask[ia] |= 1 << ib
            forbidden_mask[ib] |= 1 << ia

    allowed_next = tuple(
        tuple(c for c in range(len(budgets)) if not ((forbidden_mask[prev] >> c) & 1))
        for prev in range(len(budgets))
    )
    allowed_mask = tuple(
        sum(1 << c for c in allowed_next[prev]) for prev in range(len(budgets))
    )
    self_forbidden = tuple(bool(forbidden_mask[c] & (1 << c)) for c in range(len(budgets)))
    full_code = sum(budget * mult for budget, mult in zip(budgets, mults))
    return budgets, mults, digit_tables, allowed_next, allowed_mask, self_forbidden, full_code


def isomer_count(
    n: int, symmetry_group: str, composition: dict[str, int], forbidden_pairs: list[list[str]]
) -> int:
    assert sum(composition.values()) == n
    budgets, mults, digit_tables, allowed_next, allowed_mask, self_forbidden, full_code = build_recipe_tables(
        composition, forbidden_pairs
    )

    rotation_classes: dict[int, tuple[list[int], int]] = {}
    for d in range(n):
        g = gcd(n, d)
        if g not in rotation_classes:
            rotation_classes[g] = (rotation_perm(n, d), 0)
        rep, multiplicity = rotation_classes[g]
        rotation_classes[g] = (rep, multiplicity + 1)

    if symmetry_group == "cyclic":
        group_terms = list(rotation_classes.values())
        group_size = n
    elif symmetry_group == "dihedral":
        group_terms = list(rotation_classes.values())
        if n % 2 == 0:
            group_terms.append((reflection_perm(n, 0), n // 2))
            group_terms.append((reflection_perm(n, 1), n // 2))
        else:
            group_terms.append((reflection_perm(n, 0), n))
        group_size = 2 * n
    else:
        raise ValueError(f"unknown symmetry group: {symmetry_group!r}")

    total = sum(
        multiplicity
        * fix_count(
            perm,
            n,
            budgets,
            mults,
            digit_tables,
            allowed_next,
            allowed_mask,
            self_forbidden,
            full_code,
        )
        for perm, multiplicity in group_terms
    )

    assert total % group_size == 0, f"Burnside sum {total} is not divisible by {group_size}"
    return total // group_size


def main() -> None:
    data = json.loads(INPUT.read_text())
    isomer_counts = {}
    for recipe in data["recipes"]:
        recipe_id = recipe["recipe_id"]
        isomer_counts[recipe_id] = isomer_count(
            recipe["n_positions"],
            recipe["symmetry_group"],
            recipe["composition"],
            recipe["forbidden_adjacent_pairs"],
        )

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps({"isomer_counts": isomer_counts}, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
