from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter, deque
from functools import lru_cache
from itertools import combinations, permutations, product
from pathlib import Path


APP_ROOT = Path(os.environ.get("POOLING_APP_ROOT", "/app"))
INPUT = APP_ROOT / "input" / "pooling.json"
OUTPUT = APP_ROOT / "output.json"
EXPECTED_INPUT_SHA256 = "0c463d88a78c987a50c7b88ea92fe9923b1df3fd71274102b5af20ea6221f4af"
EXPECTED_OUTPUT_SHA256 = "00cbd219e30872f001d1c2950a4e33e539188c86d52530137f3d12116d14d946"

Permutation = tuple[int, ...]
CohortClass = tuple[int, ...]
Design = tuple[int, ...]
SAMPLE_ORDERS = tuple(permutations(range(3)))


def load_output() -> dict:
    return json.loads(OUTPUT.read_text())


def canonical_digest(value: object) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def exact_int(value: object, *, positive: bool = False) -> int:
    assert isinstance(value, int) and not isinstance(value, bool)
    assert value > 0 if positive else value >= 0
    return value


def compose(left: Permutation, right: Permutation) -> Permutation:
    return tuple(left[right[index]] for index in range(len(left)))


def invert(permutation: Permutation) -> Permutation:
    result = [0] * len(permutation)
    for source, target in enumerate(permutation):
        result[target] = source
    return tuple(result)


def close_generators(
    generators: list[Permutation], width: int
) -> list[Permutation]:
    identity = tuple(range(width))
    discovered = {identity}
    frontier = deque([identity])
    while frontier:
        element = frontier.popleft()
        for generator in generators:
            candidate = compose(element, generator)
            if candidate not in discovered:
                discovered.add(candidate)
                frontier.append(candidate)
    return sorted(discovered)


def bit_string(mask: int, width: int) -> str:
    return "".join(
        "1" if mask & (1 << position) else "0"
        for position in range(width)
    )


def mask_from_string(value: str) -> int:
    return sum(
        1 << index for index, bit in enumerate(value) if bit == "1"
    )


def normalize_class(
    masks: CohortClass, width: int
) -> CohortClass:
    return tuple(
        sorted(masks, key=lambda mask: bit_string(mask, width))
    )


def move_mask(
    mask: int, permutation: Permutation
) -> int:
    return sum(
        1 << destination
        for source, destination in enumerate(permutation)
        if mask & (1 << source)
    )


def labelled_plate_partitions(
    positions: tuple[int, ...], block_size: int
) -> list[CohortClass]:
    universe = set(positions)
    result = []
    for first in combinations(positions, block_size):
        remainder = universe - set(first)
        for second in combinations(sorted(remainder), block_size):
            third = tuple(sorted(remainder - set(second)))
            if len(third) == block_size:
                result.append(
                    tuple(
                        sum(1 << position for position in block)
                        for block in (first, second, third)
                    )
                )
    return result


@lru_cache(maxsize=1)
def instance_context() -> dict:
    data = json.loads(INPUT.read_text())
    pool_order = data["pool_order"]
    width = len(pool_order)
    pool_index = {
        pool_id: index for index, pool_id in enumerate(pool_order)
    }
    generators = [
        tuple(pool_index[target] for target in row["image"])
        for row in data["symmetry_rules"]["pool_generators"]
    ]
    group = close_generators(generators, width)

    plates: dict[str, list[int]] = {}
    for index, pool in enumerate(data["pools"]):
        plates.setdefault(pool["plate"], []).append(index)
    block_size = data["incidence_rules"]["per_plate_replication"]
    partitions = [
        labelled_plate_partitions(tuple(indices), block_size)
        for indices in plates.values()
    ]
    cohort_classes = set()
    for selected in product(*partitions):
        masks = tuple(
            sum(partition[sample] for partition in selected)
            for sample in range(3)
        )
        cohort_classes.add(normalize_class(masks, width))
    classes = sorted(
        cohort_classes,
        key=lambda row: tuple(
            bit_string(mask, width) for mask in row
        ),
    )
    class_index = {
        cohort_class: index
        for index, cohort_class in enumerate(classes)
    }
    masks = {
        mask for cohort_class in classes for mask in cohort_class
    }
    mask_keys = {
        mask: bit_string(mask, width) for mask in masks
    }
    actions = []
    for permutation in group:
        mask_images = {
            mask: move_mask(mask, permutation) for mask in masks
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
    plate_masks = tuple(
        sum(1 << position for position in indices)
        for indices in plates.values()
    )
    return {
        "data": data,
        "width": width,
        "group": group,
        "classes": classes,
        "class_index": class_index,
        "actions": actions,
        "perm_to_action": dict(zip(group, actions)),
        "plate_positions": tuple(tuple(row) for row in plates.values()),
        "plate_masks": plate_masks,
    }


def encoded_class(class_id: int) -> str:
    context = instance_context()
    width = context["width"]
    return "/".join(
        bit_string(mask, width)
        for mask in context["classes"][class_id]
    )


@lru_cache(maxsize=None)
def pool_orbit_id(class_id: int) -> str:
    return min(
        (
            encoded_class(action[class_id])
            for action in instance_context()["actions"]
        ),
        key=str.encode,
    )


@lru_cache(maxsize=None)
def contingency_profile(left_id: int, right_id: int) -> str:
    context = instance_context()
    left = context["classes"][left_id]
    right = context["classes"][right_id]
    plate_masks = context["plate_masks"]
    best = None
    for left_order in SAMPLE_ORDERS:
        for right_order in SAMPLE_ORDERS:
            segments = []
            for plate_mask in plate_masks:
                segments.append(
                    tuple(
                        (
                            left[left_order[row]]
                            & right[right_order[column]]
                            & plate_mask
                        ).bit_count()
                        for row in range(3)
                        for column in range(3)
                    )
                )
            for plate_order in permutations(range(len(segments))):
                flattened = tuple(
                    value
                    for plate in plate_order
                    for value in segments[plate]
                )
                if best is None or flattened < best:
                    best = flattened
    assert best is not None
    return "".join(str(value) for value in best)


@lru_cache(maxsize=None)
def xor_spectrum(class_ids: tuple[int, int, int]) -> str:
    classes = instance_context()["classes"]
    width = instance_context()["width"]
    values = []
    for sample_indices in product(range(3), repeat=3):
        mask = 0
        for class_id, sample_index in zip(
            class_ids, sample_indices
        ):
            mask ^= classes[class_id][sample_index]
        weight = mask.bit_count()
        values.append(min(weight, width - weight))
    return "".join(str(value) for value in sorted(values))


@lru_cache(maxsize=None)
def union_xor_spectrum(
    class_ids: tuple[int, int, int, int],
) -> str:
    classes = instance_context()["classes"]
    width = instance_context()["width"]
    values = []
    for sample_indices in product(range(3), repeat=4):
        union = 0
        xor = 0
        for class_id, sample_index in zip(
            class_ids, sample_indices
        ):
            mask = classes[class_id][sample_index]
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


def encoded_design(design: Design) -> str:
    context = instance_context()
    width = context["width"]
    return "/".join(
        bit_string(mask, width)
        for class_id in design
        for mask in context["classes"][class_id]
    )


def parse_representative(value: str) -> Design:
    context = instance_context()
    data = context["data"]
    width = context["width"]
    class_index = context["class_index"]
    rows = value.split("/")
    assert len(rows) == 3 * len(data["cohort_order"])
    result = []
    for cohort in range(len(data["cohort_order"])):
        strings = rows[3 * cohort : 3 * cohort + 3]
        assert strings == sorted(strings)
        assert all(re.fullmatch(rf"[01]{{{width}}}", row) for row in strings)
        masks = tuple(mask_from_string(row) for row in strings)
        assert all(
            left & right == 0
            for left, right in combinations(masks, 2)
        )
        assert sum(masks) == (1 << width) - 1
        assert masks in class_index
        result.append(class_index[masks])
    return tuple(result)


def valid_design(design: Design) -> bool:
    context = instance_context()
    data = context["data"]
    width = context["width"]
    classes = context["classes"]
    plate_positions = context["plate_positions"]
    if len(set(design)) != len(design):
        return False

    rows = [
        mask for class_id in design for mask in classes[class_id]
    ]
    replication = data["incidence_rules"]["sample_replication"]
    per_plate = data["incidence_rules"]["per_plate_replication"]
    capacity = data["incidence_rules"]["pool_capacity"]
    if any(mask.bit_count() != replication for mask in rows):
        return False
    if any(
        sum(bool(mask & (1 << position)) for position in plate)
        != per_plate
        for mask in rows
        for plate in plate_positions
    ):
        return False
    if any(
        sum(bool(mask & (1 << pool)) for mask in rows) != capacity
        for pool in range(width)
    ):
        return False

    loss_limit = data["incidence_rules"][
        "maximum_simultaneous_pool_losses"
    ]
    for loss_count in range(1, loss_limit + 1):
        for deleted in combinations(range(width), loss_count):
            removed = sum(1 << pool for pool in deleted)
            survivors = [mask & ~removed for mask in rows]
            if any(mask == 0 for mask in survivors):
                return False
            if len(set(survivors)) != len(rows):
                return False

    cohort_index = {
        cohort_id: index
        for index, cohort_id in enumerate(data["cohort_order"])
    }
    for rule in data["cohort_class_rules"]["constraints"]:
        cohort = cohort_index[rule["cohort_id"]]
        if pool_orbit_id(design[cohort]) != rule["required_pool_orbit_id"]:
            return False
    for rule in data["cohort_pair_rules"]["constraints"]:
        left = cohort_index[rule["cohort_a"]]
        right = cohort_index[rule["cohort_b"]]
        if (
            contingency_profile(design[left], design[right])
            not in rule["allowed_profiles"]
        ):
            return False
    for rule in data["cohort_triple_rules"]["constraints"]:
        class_ids = tuple(
            design[cohort_index[value]] for value in rule["cohorts"]
        )
        if xor_spectrum(class_ids) not in rule["allowed_profiles"]:
            return False
    for rule in data["cohort_quadruple_rules"]["constraints"]:
        class_ids = tuple(
            design[cohort_index[value]] for value in rule["cohorts"]
        )
        if union_xor_spectrum(class_ids) not in rule["allowed_profiles"]:
            return False
    return True


@lru_cache(maxsize=1)
def enumerate_valid_designs() -> frozenset[Design]:
    context = instance_context()
    data = context["data"]
    classes = context["classes"]
    cohort_count = len(data["cohort_order"])
    cohort_index = {
        cohort_id: index
        for index, cohort_id in enumerate(data["cohort_order"])
    }

    orbit_domains: dict[str, set[int]] = {}
    required_orbit_ids = {
        rule["required_pool_orbit_id"]
        for rule in data["cohort_class_rules"]["constraints"]
    }
    for orbit_id in required_orbit_ids:
        cohort_class = tuple(
            mask_from_string(value) for value in orbit_id.split("/")
        )
        class_id = context["class_index"][cohort_class]
        orbit_domains[orbit_id] = {
            action[class_id] for action in context["actions"]
        }
    domains = [set(range(len(classes))) for _ in range(cohort_count)]
    for rule in data["cohort_class_rules"]["constraints"]:
        domains[cohort_index[rule["cohort_id"]]] = set(
            orbit_domains[rule["required_pool_orbit_id"]]
        )

    neighbors: list[list[tuple[int, bool, frozenset[str]]]] = [
        [] for _ in range(cohort_count)
    ]
    for rule in data["cohort_pair_rules"]["constraints"]:
        left = cohort_index[rule["cohort_a"]]
        right = cohort_index[rule["cohort_b"]]
        allowed = frozenset(rule["allowed_profiles"])
        neighbors[left].append((right, True, allowed))
        neighbors[right].append((left, False, allowed))

    triples_by_cohort = [[] for _ in range(cohort_count)]
    for rule in data["cohort_triple_rules"]["constraints"]:
        triple = tuple(
            cohort_index[value] for value in rule["cohorts"]
        )
        allowed = frozenset(rule["allowed_profiles"])
        for cohort in triple:
            triples_by_cohort[cohort].append((triple, allowed))

    quadruples_by_cohort = [[] for _ in range(cohort_count)]
    for rule in data["cohort_quadruple_rules"]["constraints"]:
        quadruple = tuple(
            cohort_index[value] for value in rule["cohorts"]
        )
        allowed = frozenset(rule["allowed_profiles"])
        for cohort in quadruple:
            quadruples_by_cohort[cohort].append(
                (quadruple, allowed)
            )

    assignment = [-1] * cohort_count
    completed: set[Design] = set()

    def candidate_domain(cohort: int, used: set[int]) -> set[int]:
        candidates = domains[cohort] - used
        for other, candidate_is_left, allowed in neighbors[cohort]:
            assigned = assignment[other]
            if assigned < 0:
                continue
            candidates = {
                candidate
                for candidate in candidates
                if (
                    contingency_profile(candidate, assigned)
                    if candidate_is_left
                    else contingency_profile(assigned, candidate)
                )
                in allowed
            }
        for triple, allowed in triples_by_cohort[cohort]:
            others = [
                assignment[value]
                for value in triple
                if value != cohort
            ]
            if all(value >= 0 for value in others):
                candidates = {
                    candidate
                    for candidate in candidates
                    if xor_spectrum(
                        tuple(
                            candidate
                            if value == cohort
                            else assignment[value]
                            for value in triple
                        )
                    )
                    in allowed
                }
        for quadruple, allowed in quadruples_by_cohort[cohort]:
            others = [
                assignment[value]
                for value in quadruple
                if value != cohort
            ]
            if all(value >= 0 for value in others):
                candidates = {
                    candidate
                    for candidate in candidates
                    if union_xor_spectrum(
                        tuple(
                            candidate
                            if value == cohort
                            else assignment[value]
                            for value in quadruple
                        )
                    )
                    in allowed
                }
        return candidates

    def visit(unassigned: set[int], used: set[int]) -> None:
        if not unassigned:
            design = tuple(assignment)
            assert valid_design(design)
            completed.add(design)
            return
        candidates_by_size = [
            (len(candidate_domain(cohort, used)), cohort)
            for cohort in unassigned
        ]
        _, cohort = min(candidates_by_size)
        for class_id in sorted(candidate_domain(cohort, used)):
            assignment[cohort] = class_id
            visit(unassigned - {cohort}, used | {class_id})
            assignment[cohort] = -1

    visit(set(range(cohort_count)), set())
    return frozenset(completed)


@lru_cache(maxsize=1)
def orbit_reference() -> tuple[frozenset[str], Counter[int]]:
    actions = instance_context()["actions"]
    remaining = set(enumerate_valid_designs())
    representatives = set()
    stabilizers: Counter[int] = Counter()
    while remaining:
        seed = next(iter(remaining))
        orbit = {
            tuple(action[class_id] for class_id in seed)
            for action in actions
        }
        canonical = min(orbit)
        representatives.add(encoded_design(canonical))
        stabilizers[
            sum(
                tuple(action[class_id] for class_id in canonical)
                == canonical
                for action in actions
            )
        ] += 1
        remaining.difference_update(orbit)
    return frozenset(representatives), stabilizers


@lru_cache(maxsize=1)
def conjugacy_reference() -> tuple[tuple[str, int, int], ...]:
    context = instance_context()
    group = context["group"]
    perm_to_action = context["perm_to_action"]
    valid = enumerate_valid_designs()
    inverses = {permutation: invert(permutation) for permutation in group}
    remaining = set(group)
    rows = []
    while remaining:
        representative = min(remaining)
        conjugates = {
            compose(compose(element, representative), inverses[element])
            for element in group
        }
        action = perm_to_action[representative]
        fixed = sum(
            all(action[class_id] == class_id for class_id in design)
            for design in valid
        )
        class_id = "".join(f"{value:02d}" for value in representative)
        rows.append((class_id, len(conjugates), fixed))
        remaining.difference_update(conjugates)
    rows.sort()
    return tuple(rows)


def test_output_is_regular_json_and_input_is_unchanged():
    """The sole artifact is regular JSON and the normative pooling instance is byte-identical."""
    assert OUTPUT.exists()
    assert not OUTPUT.is_symlink()
    assert OUTPUT.is_file()
    assert isinstance(load_output(), dict)
    assert hashlib.sha256(INPUT.read_bytes()).hexdigest() == EXPECTED_INPUT_SHA256
    assert sorted(path.name for path in INPUT.parent.iterdir()) == ["pooling.json"]


def test_exact_documented_schema_and_scalar_types():
    """Every top-level section, nested record, and exact integer field matches the prompt."""
    data = load_output()
    assert set(data) == {
        "counts",
        "group_orders",
        "stabilizer_histogram",
        "burnside",
        "canonical_representatives",
    }
    assert set(data["counts"]) == {
        "normalized_designs",
        "labelled_designs",
        "equivalence_classes",
    }
    assert set(data["group_orders"]) == {
        "pool",
        "sample_permutations",
        "full",
    }
    assert set(data["burnside"]) == {
        "numerator",
        "denominator",
        "conjugacy_classes",
    }
    for value in data["counts"].values():
        exact_int(value)
    for value in data["group_orders"].values():
        exact_int(value, positive=True)
    exact_int(data["burnside"]["numerator"], positive=True)
    exact_int(data["burnside"]["denominator"], positive=True)
    assert isinstance(data["stabilizer_histogram"], list)
    assert isinstance(data["burnside"]["conjugacy_classes"], list)
    assert isinstance(data["canonical_representatives"], list)
    for row in data["stabilizer_histogram"]:
        assert set(row) == {"stabilizer_size", "classes"}
        exact_int(row["stabilizer_size"], positive=True)
        exact_int(row["classes"], positive=True)
    for row in data["burnside"]["conjugacy_classes"]:
        assert set(row) == {
            "class_id",
            "class_size",
            "fixed_normalized_designs",
        }
        assert re.fullmatch(r"(?:0[0-9]|1[0-7]){18}", row["class_id"])
        exact_int(row["class_size"], positive=True)
        exact_int(row["fixed_normalized_designs"])


def test_generated_group_orders_and_representative_validity():
    """The disclosed groups are derived and every representative passes every loss/profile rule."""
    data = load_output()
    context = instance_context()
    instance = context["data"]
    group_orders = data["group_orders"]
    assert len(context["classes"]) == 121500
    assert len(context["group"]) == instance["symmetry_rules"]["pool_group_order"]
    assert group_orders["pool"] == len(context["group"])
    assert (
        group_orders["sample_permutations"]
        == instance["symmetry_rules"]["sample_permutation_group_order"]
    )
    assert group_orders["full"] == instance["symmetry_rules"]["full_group_order"]
    assert (
        group_orders["full"]
        == group_orders["pool"] * group_orders["sample_permutations"]
    )

    representatives = data["canonical_representatives"]
    assert representatives == sorted(set(representatives), key=str.encode)
    for encoding in representatives:
        design = parse_representative(encoding)
        assert valid_design(design)
        images = [
            tuple(action[class_id] for class_id in design)
            for action in context["actions"]
        ]
        assert design == min(images)
        assert encoding == encoded_design(design)


def test_complete_orbit_enumeration_and_stabilizer_histogram():
    """Independent propagation proves no canonical class is missing or duplicated."""
    data = load_output()
    expected_representatives, expected_histogram = orbit_reference()
    submitted = data["canonical_representatives"]
    assert frozenset(submitted) == expected_representatives
    assert len(submitted) == data["counts"]["equivalence_classes"]
    expected_rows = [
        {"stabilizer_size": stabilizer, "classes": classes}
        for stabilizer, classes in sorted(expected_histogram.items())
    ]
    assert data["stabilizer_histogram"] == expected_rows
    assert sum(row["classes"] for row in expected_rows) == len(submitted)


def test_exact_counts_and_orbit_stabilizer_reconciliation():
    """Normalized and labelled totals equal both required orbit–stabilizer sums."""
    data = load_output()
    valid_count = len(enumerate_valid_designs())
    counts = data["counts"]
    groups = data["group_orders"]
    assert counts["normalized_designs"] == valid_count
    assert (
        counts["labelled_designs"]
        == valid_count * groups["sample_permutations"]
    )
    normalized_sum = sum(
        groups["pool"] // row["stabilizer_size"] * row["classes"]
        for row in data["stabilizer_histogram"]
    )
    labelled_sum = sum(
        groups["full"] // row["stabilizer_size"] * row["classes"]
        for row in data["stabilizer_histogram"]
    )
    assert normalized_sum == counts["normalized_designs"]
    assert labelled_sum == counts["labelled_designs"]


def test_conjugacy_fixed_counts_and_burnside_reconciliation():
    """Every residual conjugacy class and fixed count is exact and Burnside reconciles."""
    data = load_output()
    expected = [
        {
            "class_id": class_id,
            "class_size": class_size,
            "fixed_normalized_designs": fixed,
        }
        for class_id, class_size, fixed in conjugacy_reference()
    ]
    submitted = data["burnside"]["conjugacy_classes"]
    assert submitted == expected
    assert submitted == sorted(submitted, key=lambda row: row["class_id"].encode())
    assert sum(row["class_size"] for row in submitted) == data["group_orders"]["pool"]
    numerator = sum(
        row["class_size"] * row["fixed_normalized_designs"]
        for row in submitted
    )
    assert data["burnside"]["numerator"] == numerator
    assert data["burnside"]["denominator"] == data["group_orders"]["pool"]
    assert (
        numerator
        == data["burnside"]["denominator"]
        * data["counts"]["equivalence_classes"]
    )


def test_complete_canonical_artifact_matches_reference():
    """The complete deterministic enumeration has the independently pinned digest."""
    assert canonical_digest(load_output()) == EXPECTED_OUTPUT_SHA256
