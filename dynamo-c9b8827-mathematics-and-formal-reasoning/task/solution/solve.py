#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from collections import Counter, deque
from itertools import combinations
from pathlib import Path


APP_ROOT = Path(os.environ.get("POOLING_APP_ROOT", "/app"))
INPUT = APP_ROOT / "input" / "pooling.json"
OUTPUT = APP_ROOT / "output.json"

Permutation = tuple[int, ...]
Design = tuple[int, ...]


def compose(left: Permutation, right: Permutation) -> Permutation:
    """Return left o right for old-index -> new-index image permutations."""
    return tuple(left[right[index]] for index in range(len(left)))


def inverse(permutation: Permutation) -> Permutation:
    result = [0] * len(permutation)
    for source, target in enumerate(permutation):
        result[target] = source
    return tuple(result)


def generated_group(generators: list[Permutation], degree: int) -> list[Permutation]:
    identity = tuple(range(degree))
    known = {identity}
    pending = deque([identity])
    while pending:
        current = pending.popleft()
        for generator in generators:
            for candidate in (
                compose(generator, current),
                compose(current, generator),
            ):
                if candidate not in known:
                    known.add(candidate)
                    pending.append(candidate)
    return sorted(known)


def signature(mask: int, width: int) -> str:
    return "".join("1" if mask & (1 << index) else "0" for index in range(width))


def complement(mask: int, width: int) -> int:
    return ((1 << width) - 1) ^ mask


def normalize_mask(mask: int, width: int) -> int:
    other = complement(mask, width)
    return min((mask, other), key=lambda value: signature(value, width))


def permute_mask(mask: int, permutation: Permutation, width: int) -> int:
    result = 0
    for source in range(width):
        if mask & (1 << source):
            result |= 1 << permutation[source]
    return result


def quotient_distance(left: int, right: int, width: int) -> int:
    distance = (left ^ right).bit_count()
    return min(distance, width - distance)


def balanced_cohort_classes(data: dict) -> list[int]:
    pool_order = data["pool_order"]
    width = len(pool_order)
    replication = data["incidence_rules"]["sample_replication"]
    per_plate = data["incidence_rules"]["per_plate_replication"]
    plate_indices: dict[str, list[int]] = {}
    for index, pool in enumerate(data["pools"]):
        plate_indices.setdefault(pool["plate"], []).append(index)

    classes = set()
    for chosen in combinations(range(width), replication):
        mask = sum(1 << index for index in chosen)
        if all(
            sum(bool(mask & (1 << index)) for index in indices) == per_plate
            for indices in plate_indices.values()
        ):
            classes.add(normalize_mask(mask, width))
    return sorted(classes, key=lambda value: signature(value, width))


def robust_after_pool_loss(design: Design, width: int) -> bool:
    rows = []
    for mask in design:
        rows.extend((mask, complement(mask, width)))
    for deleted in range(width):
        surviving = [mask & ~(1 << deleted) for mask in rows]
        if any(mask == 0 for mask in surviving) or len(set(surviving)) != len(rows):
            return False
    return True


def enumerate_normalized_designs(data: dict, classes: list[int]) -> set[Design]:
    cohort_order = data["cohort_order"]
    cohort_index = {cohort: index for index, cohort in enumerate(cohort_order)}
    width = len(data["pool_order"])
    rules: dict[tuple[int, int], int] = {}
    degree = [0] * len(cohort_order)
    for row in data["cohort_pair_rules"]["constraints"]:
        left = cohort_index[row["cohort_a"]]
        right = cohort_index[row["cohort_b"]]
        key = tuple(sorted((left, right)))
        rules[key] = row["quotient_distance"]
        degree[left] += 1
        degree[right] += 1

    relation = [
        [quotient_distance(left, right, width) for right in classes]
        for left in classes
    ]
    variable_order = sorted(
        range(len(cohort_order)), key=lambda index: (-degree[index], index)
    )
    assignment = [-1] * len(cohort_order)
    valid: set[Design] = set()

    def search(depth: int, used: int) -> None:
        if depth == len(variable_order):
            design = tuple(assignment)
            if robust_after_pool_loss(
                tuple(classes[class_index] for class_index in design), width
            ):
                valid.add(design)
            return
        cohort = variable_order[depth]
        for candidate in range(len(classes)):
            if used & (1 << candidate):
                continue
            acceptable = True
            for other, assigned in enumerate(assignment):
                if assigned < 0:
                    continue
                key = tuple(sorted((cohort, other)))
                if key in rules and relation[candidate][assigned] != rules[key]:
                    acceptable = False
                    break
            if acceptable:
                assignment[cohort] = candidate
                search(depth + 1, used | (1 << candidate))
                assignment[cohort] = -1

    search(0, 0)
    return valid


def encode_design(design: Design, classes: list[int], width: int) -> str:
    rows = []
    for class_index in design:
        first = signature(classes[class_index], width)
        second = "".join("1" if bit == "0" else "0" for bit in first)
        rows.extend((first, second))
    return "/".join(rows)


def conjugacy_classes(group: list[Permutation]) -> list[list[Permutation]]:
    remaining = set(group)
    result = []
    inverses = {element: inverse(element) for element in group}
    while remaining:
        representative = min(remaining)
        conjugates = {
            compose(compose(element, representative), inverses[element])
            for element in group
        }
        result.append(sorted(conjugates))
        remaining.difference_update(conjugates)
    result.sort(key=lambda row: row[0])
    return result


def permutation_word(permutation: Permutation) -> str:
    return "".join(str(value) for value in permutation)


def main() -> None:
    data = json.loads(INPUT.read_text())
    pool_order = data["pool_order"]
    pool_index = {pool_id: index for index, pool_id in enumerate(pool_order)}
    width = len(pool_order)

    generators = [
        tuple(pool_index[target] for target in row["image"])
        for row in data["symmetry_rules"]["pool_generators"]
    ]
    pool_group = generated_group(generators, width)
    expected_pool_order = data["symmetry_rules"]["pool_group_order"]
    if len(pool_group) != expected_pool_order:
        raise RuntimeError("pool generators do not have the disclosed order")

    classes = balanced_cohort_classes(data)
    class_index = {mask: index for index, mask in enumerate(classes)}
    actions = []
    for permutation in pool_group:
        action = []
        for mask in classes:
            moved = permute_mask(mask, permutation, width)
            action.append(class_index[normalize_mask(moved, width)])
        actions.append(tuple(action))
    permutation_to_action = dict(zip(pool_group, actions))

    valid_designs = enumerate_normalized_designs(data, classes)
    unseen = set(valid_designs)
    representatives: list[str] = []
    stabilizers: Counter[int] = Counter()

    while unseen:
        seed = next(iter(unseen))
        images = {
            tuple(action[class_id] for class_id in seed) for action in actions
        }
        canonical = min(images)
        stabilizer = sum(
            tuple(action[class_id] for class_id in canonical) == canonical
            for action in actions
        )
        representatives.append(encode_design(canonical, classes, width))
        stabilizers[stabilizer] += 1
        unseen.difference_update(images)

    representatives.sort(key=str.encode)
    conjugacy_output = []
    burnside_numerator = 0
    for conjugacy_class in conjugacy_classes(pool_group):
        representative = conjugacy_class[0]
        action = permutation_to_action[representative]
        fixed_count = sum(
            all(action[class_id] == class_id for class_id in design)
            for design in valid_designs
        )
        class_size = len(conjugacy_class)
        burnside_numerator += class_size * fixed_count
        conjugacy_output.append(
            {
                "class_id": permutation_word(representative),
                "class_size": class_size,
                "fixed_normalized_designs": fixed_count,
            }
        )

    sample_swap_order = data["symmetry_rules"]["sample_swap_group_order"]
    full_group_order = data["symmetry_rules"]["full_group_order"]
    normalized_count = len(valid_designs)
    labelled_count = normalized_count * sample_swap_order
    orbit_count = len(representatives)

    if burnside_numerator != orbit_count * len(pool_group):
        raise RuntimeError("Burnside reconciliation failed")
    if sum(
        (full_group_order // stabilizer) * count
        for stabilizer, count in stabilizers.items()
    ) != labelled_count:
        raise RuntimeError("orbit-stabilizer reconciliation failed")

    result = {
        "counts": {
            "normalized_designs": normalized_count,
            "labelled_designs": labelled_count,
            "equivalence_classes": orbit_count,
        },
        "group_orders": {
            "pool": len(pool_group),
            "sample_swaps": sample_swap_order,
            "full": full_group_order,
        },
        "stabilizer_histogram": [
            {"stabilizer_size": stabilizer, "classes": count}
            for stabilizer, count in sorted(stabilizers.items())
        ],
        "burnside": {
            "numerator": burnside_numerator,
            "denominator": len(pool_group),
            "conjugacy_classes": conjugacy_output,
        },
        "canonical_representatives": representatives,
    }
    OUTPUT.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
