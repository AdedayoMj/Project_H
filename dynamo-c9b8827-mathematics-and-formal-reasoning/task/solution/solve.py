#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from collections import Counter, deque
from functools import lru_cache
from itertools import combinations, permutations, product
from pathlib import Path


APP_ROOT = Path(os.environ.get("POOLING_APP_ROOT", "/app"))
INPUT = APP_ROOT / "input" / "pooling.json"
OUTPUT = APP_ROOT / "output.json"

Permutation = tuple[int, ...]
CohortClass = tuple[int, ...]
Design = tuple[int, ...]
SAMPLE_ORDERS = tuple(permutations(range(3)))


def compose(left: Permutation, right: Permutation) -> Permutation:
    return tuple(left[right[index]] for index in range(len(left)))


def inverse(permutation: Permutation) -> Permutation:
    result = [0] * len(permutation)
    for source, target in enumerate(permutation):
        result[target] = source
    return tuple(result)


def generated_group(
    generators: list[Permutation], degree: int
) -> list[Permutation]:
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
    return "".join(
        "1" if mask & (1 << index) else "0"
        for index in range(width)
    )


def normalize_class(
    masks: CohortClass, width: int
) -> CohortClass:
    return tuple(sorted(masks, key=lambda mask: signature(mask, width)))


def permute_mask(
    mask: int, operation: Permutation, width: int
) -> int:
    return sum(
        1 << operation[source]
        for source in range(width)
        if mask & (1 << source)
    )


def permute_class(
    cohort_class: CohortClass,
    operation: Permutation,
    width: int,
) -> CohortClass:
    return normalize_class(
        tuple(
            permute_mask(mask, operation, width)
            for mask in cohort_class
        ),
        width,
    )


def encode_class(
    cohort_class: CohortClass, width: int
) -> str:
    return "/".join(signature(mask, width) for mask in cohort_class)


def plate_partitions(
    indices: tuple[int, ...], per_sample: int
) -> list[CohortClass]:
    available = set(indices)
    rows = []
    for first in combinations(indices, per_sample):
        after_first = available - set(first)
        for second in combinations(sorted(after_first), per_sample):
            third = tuple(sorted(after_first - set(second)))
            if len(third) != per_sample:
                continue
            rows.append(
                tuple(
                    sum(1 << index for index in part)
                    for part in (first, second, third)
                )
            )
    return rows


def balanced_cohort_classes(data: dict) -> list[CohortClass]:
    width = len(data["pool_order"])
    per_plate = data["incidence_rules"]["per_plate_replication"]
    plate_indices: dict[str, list[int]] = {}
    for index, pool in enumerate(data["pools"]):
        plate_indices.setdefault(pool["plate"], []).append(index)
    per_plate_partitions = [
        plate_partitions(tuple(indices), per_plate)
        for indices in plate_indices.values()
    ]

    classes = set()
    for selected in product(*per_plate_partitions):
        masks = tuple(
            sum(partition[sample] for partition in selected)
            for sample in range(3)
        )
        classes.add(normalize_class(masks, width))
    return sorted(
        classes,
        key=lambda row: tuple(signature(mask, width) for mask in row),
    )


@lru_cache(maxsize=None)
def pair_profile(
    left: CohortClass,
    right: CohortClass,
    plate_masks: tuple[int, ...],
) -> str:
    best: tuple[int, ...] | None = None
    for left_order in SAMPLE_ORDERS:
        for right_order in SAMPLE_ORDERS:
            segments = [
                tuple(
                    (
                        left[left_order[row]]
                        & right[right_order[column]]
                        & plate_mask
                    ).bit_count()
                    for row in range(3)
                    for column in range(3)
                )
                for plate_mask in plate_masks
            ]
            for plate_order in permutations(range(len(plate_masks))):
                candidate = tuple(
                    value
                    for plate in plate_order
                    for value in segments[plate]
                )
                if best is None or candidate < best:
                    best = candidate
    assert best is not None
    return "".join(str(value) for value in best)


@lru_cache(maxsize=None)
def triple_xor_profile(
    classes: tuple[CohortClass, ...], width: int
) -> str:
    values = []
    for sample_indices in product(range(3), repeat=3):
        mask = 0
        for cohort_class, sample_index in zip(
            classes, sample_indices
        ):
            mask ^= cohort_class[sample_index]
        weight = mask.bit_count()
        values.append(min(weight, width - weight))
    return "".join(str(value) for value in sorted(values))


@lru_cache(maxsize=None)
def quadruple_union_xor_profile(
    classes: tuple[CohortClass, ...], width: int
) -> str:
    values = []
    for sample_indices in product(range(3), repeat=4):
        union = 0
        xor = 0
        for cohort_class, sample_index in zip(
            classes, sample_indices
        ):
            mask = cohort_class[sample_index]
            union |= mask
            xor ^= mask
        xor_weight = xor.bit_count()
        values.append(
            (
                union.bit_count(),
                min(xor_weight, width - xor_weight),
            )
        )
    return "".join(
        f"{union_weight:02d}{xor_weight:02d}"
        for union_weight, xor_weight in sorted(values)
    )


def robust_after_pool_losses(
    design: tuple[CohortClass, ...],
    width: int,
    maximum_losses: int,
) -> bool:
    rows = [mask for cohort_class in design for mask in cohort_class]
    for loss_count in range(1, maximum_losses + 1):
        for deleted in combinations(range(width), loss_count):
            deleted_mask = sum(1 << pool for pool in deleted)
            survivors = [mask & ~deleted_mask for mask in rows]
            if any(mask == 0 for mask in survivors):
                return False
            if len(set(survivors)) != len(rows):
                return False
    return True


def enumerate_normalized_designs(
    data: dict,
    classes: list[CohortClass],
    actions: list[tuple[int, ...]],
    plate_masks: tuple[int, ...],
) -> set[Design]:
    cohort_order = data["cohort_order"]
    cohort_count = len(cohort_order)
    cohort_index = {
        cohort: index for index, cohort in enumerate(cohort_order)
    }
    width = len(data["pool_order"])

    class_index = {
        cohort_class: class_id
        for class_id, cohort_class in enumerate(classes)
    }
    orbit_domains: dict[str, set[int]] = {}
    required_orbit_ids = {
        row["required_pool_orbit_id"]
        for row in data["cohort_class_rules"]["constraints"]
    }
    for orbit_id in required_orbit_ids:
        cohort_class = normalize_class(
            tuple(
                sum(
                    1 << index
                    for index, bit in enumerate(value)
                    if bit == "1"
                )
                for value in orbit_id.split("/")
            ),
            width,
        )
        class_id = class_index[cohort_class]
        orbit_domains[orbit_id] = {
            action[class_id] for action in actions
        }
    domains = [set(range(len(classes))) for _ in range(cohort_count)]
    for row in data["cohort_class_rules"]["constraints"]:
        domains[cohort_index[row["cohort_id"]]] = set(
            orbit_domains[row["required_pool_orbit_id"]]
        )

    pair_neighbors: list[list[tuple[int, bool, frozenset[str]]]] = [
        [] for _ in range(cohort_count)
    ]
    for row in data["cohort_pair_rules"]["constraints"]:
        left = cohort_index[row["cohort_a"]]
        right = cohort_index[row["cohort_b"]]
        allowed = frozenset(row["allowed_profiles"])
        pair_neighbors[left].append((right, True, allowed))
        pair_neighbors[right].append((left, False, allowed))

    triples_by_cohort: list[
        list[tuple[tuple[int, ...], frozenset[str]]]
    ] = [[] for _ in range(cohort_count)]
    for row in data["cohort_triple_rules"]["constraints"]:
        cohorts = tuple(cohort_index[value] for value in row["cohorts"])
        allowed = frozenset(row["allowed_profiles"])
        for cohort in cohorts:
            triples_by_cohort[cohort].append((cohorts, allowed))

    quadruples_by_cohort: list[
        list[tuple[tuple[int, ...], frozenset[str]]]
    ] = [[] for _ in range(cohort_count)]
    for row in data["cohort_quadruple_rules"]["constraints"]:
        cohorts = tuple(cohort_index[value] for value in row["cohorts"])
        allowed = frozenset(row["allowed_profiles"])
        for cohort in cohorts:
            quadruples_by_cohort[cohort].append((cohorts, allowed))

    assignment = [-1] * cohort_count
    valid: set[Design] = set()
    maximum_losses = data["incidence_rules"][
        "maximum_simultaneous_pool_losses"
    ]

    def candidates_for(cohort: int, used: set[int]) -> set[int]:
        candidates = domains[cohort] - used
        for other, candidate_is_left, allowed in pair_neighbors[cohort]:
            assigned = assignment[other]
            if assigned < 0:
                continue
            if candidate_is_left:
                candidates = {
                    candidate
                    for candidate in candidates
                    if pair_profile(
                        classes[candidate],
                        classes[assigned],
                        plate_masks,
                    )
                    in allowed
                }
            else:
                candidates = {
                    candidate
                    for candidate in candidates
                    if pair_profile(
                        classes[assigned],
                        classes[candidate],
                        plate_masks,
                    )
                    in allowed
                }
        for cohorts, allowed in triples_by_cohort[cohort]:
            others = [
                assignment[other]
                for other in cohorts
                if other != cohort
            ]
            if all(class_id >= 0 for class_id in others):
                candidates = {
                    candidate
                    for candidate in candidates
                    if triple_xor_profile(
                        tuple(
                            classes[
                                candidate
                                if member == cohort
                                else assignment[member]
                            ]
                            for member in cohorts
                        ),
                        width,
                    )
                    in allowed
                }
        for cohorts, allowed in quadruples_by_cohort[cohort]:
            others = [
                assignment[other]
                for other in cohorts
                if other != cohort
            ]
            if all(class_id >= 0 for class_id in others):
                candidates = {
                    candidate
                    for candidate in candidates
                    if quadruple_union_xor_profile(
                        tuple(
                            classes[
                                candidate
                                if member == cohort
                                else assignment[member]
                            ]
                            for member in cohorts
                        ),
                        width,
                    )
                    in allowed
                }
        return candidates

    def search(unassigned: set[int], used: set[int]) -> None:
        if not unassigned:
            design_classes = tuple(
                classes[class_id] for class_id in assignment
            )
            if robust_after_pool_losses(
                design_classes, width, maximum_losses
            ):
                valid.add(tuple(assignment))
            return

        candidates_by_size = [
            (len(candidates_for(cohort, used)), cohort)
            for cohort in unassigned
        ]
        _, cohort = min(candidates_by_size)
        for candidate in sorted(candidates_for(cohort, used)):
            assignment[cohort] = candidate
            search(unassigned - {cohort}, used | {candidate})
            assignment[cohort] = -1

    search(set(range(cohort_count)), set())
    return valid


def encode_design(
    design: Design,
    classes: list[CohortClass],
    width: int,
) -> str:
    return "/".join(
        signature(mask, width)
        for class_id in design
        for mask in classes[class_id]
    )


def conjugacy_classes(
    group: list[Permutation],
) -> list[list[Permutation]]:
    remaining = set(group)
    result = []
    inverses = {element: inverse(element) for element in group}
    while remaining:
        representative = min(remaining)
        conjugates = {
            compose(
                compose(element, representative), inverses[element]
            )
            for element in group
        }
        result.append(sorted(conjugates))
        remaining.difference_update(conjugates)
    result.sort(key=lambda row: row[0])
    return result


def permutation_word(permutation: Permutation) -> str:
    return "".join(f"{value:02d}" for value in permutation)


def main() -> None:
    data = json.loads(INPUT.read_text())
    pool_order = data["pool_order"]
    pool_index = {
        pool_id: index for index, pool_id in enumerate(pool_order)
    }
    width = len(pool_order)

    generators = [
        tuple(pool_index[target] for target in row["image"])
        for row in data["symmetry_rules"]["pool_generators"]
    ]
    pool_group = generated_group(generators, width)
    if len(pool_group) != data["symmetry_rules"]["pool_group_order"]:
        raise RuntimeError("pool generators do not have the disclosed order")

    classes = balanced_cohort_classes(data)
    class_index = {
        cohort_class: index
        for index, cohort_class in enumerate(classes)
    }
    masks = {
        mask for cohort_class in classes for mask in cohort_class
    }
    mask_keys = {
        mask: signature(mask, width) for mask in masks
    }
    actions = []
    for operation in pool_group:
        mask_images = {
            mask: permute_mask(mask, operation, width)
            for mask in masks
        }
        actions.append(
            tuple(
                class_index[
                    tuple(
                        sorted(
                            (
                                mask_images[mask]
                                for mask in cohort_class
                            ),
                            key=mask_keys.__getitem__,
                        )
                    )
                ]
                for cohort_class in classes
            )
        )
    permutation_to_action = dict(zip(pool_group, actions))
    plate_masks = tuple(
        sum(
            1 << index
            for index, pool in enumerate(data["pools"])
            if pool["plate"] == plate
        )
        for plate in dict.fromkeys(pool["plate"] for pool in data["pools"])
    )

    valid_designs = enumerate_normalized_designs(
        data, classes, actions, plate_masks
    )
    unseen = set(valid_designs)
    representatives: list[str] = []
    stabilizers: Counter[int] = Counter()
    while unseen:
        seed = next(iter(unseen))
        images = {
            tuple(action[class_id] for class_id in seed)
            for action in actions
        }
        canonical = min(images)
        stabilizer = sum(
            tuple(action[class_id] for class_id in canonical) == canonical
            for action in actions
        )
        representatives.append(
            encode_design(canonical, classes, width)
        )
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

    sample_order = data["symmetry_rules"][
        "sample_permutation_group_order"
    ]
    full_order = data["symmetry_rules"]["full_group_order"]
    normalized_count = len(valid_designs)
    labelled_count = normalized_count * sample_order
    orbit_count = len(representatives)
    if burnside_numerator != orbit_count * len(pool_group):
        raise RuntimeError("Burnside reconciliation failed")
    if sum(
        (full_order // stabilizer) * count
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
            "sample_permutations": sample_order,
            "full": full_order,
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
