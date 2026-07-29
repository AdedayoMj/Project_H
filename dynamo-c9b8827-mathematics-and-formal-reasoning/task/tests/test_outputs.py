from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter, deque
from functools import lru_cache
from itertools import combinations
from pathlib import Path


APP_ROOT = Path(os.environ.get("POOLING_APP_ROOT", "/app"))
INPUT = APP_ROOT / "input" / "pooling.json"
OUTPUT = APP_ROOT / "output.json"
EXPECTED_INPUT_SHA256 = "52c89629394661d977f53e284d030d61d1e22c42e2c1fa1c51dfea1bc5ad478b"
EXPECTED_OUTPUT_SHA256 = "755918aee48ee0d13cab2cb8d0fc461b1bba5d0c9d363c88dd17aea12e2e6091"

Permutation = tuple[int, ...]
Design = tuple[int, ...]


def load_output() -> dict:
    return json.loads(OUTPUT.read_text())


def canonical_digest(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
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


def close_generators(generators: list[Permutation], width: int) -> list[Permutation]:
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
    return "".join("1" if mask & (1 << position) else "0" for position in range(width))


def mask_from_string(value: str) -> int:
    return sum(1 << index for index, bit in enumerate(value) if bit == "1")


def complement(mask: int, width: int) -> int:
    return ((1 << width) - 1) ^ mask


def normalize(mask: int, width: int) -> int:
    opposite = complement(mask, width)
    return min((mask, opposite), key=lambda item: bit_string(item, width))


def move_mask(mask: int, permutation: Permutation, width: int) -> int:
    moved = 0
    for source, destination in enumerate(permutation):
        if mask & (1 << source):
            moved |= 1 << destination
    return moved


def quotient_distance(left: int, right: int, width: int) -> int:
    distance = (left ^ right).bit_count()
    return min(distance, width - distance)


@lru_cache(maxsize=1)
def instance_context() -> dict:
    data = json.loads(INPUT.read_text())
    pool_order = data["pool_order"]
    width = len(pool_order)
    pool_index = {pool_id: index for index, pool_id in enumerate(pool_order)}
    generators = [
        tuple(pool_index[target] for target in row["image"])
        for row in data["symmetry_rules"]["pool_generators"]
    ]
    group = close_generators(generators, width)

    plate_positions: dict[str, list[int]] = {}
    for index, pool in enumerate(data["pools"]):
        plate_positions.setdefault(pool["plate"], []).append(index)
    replication = data["incidence_rules"]["sample_replication"]
    per_plate = data["incidence_rules"]["per_plate_replication"]
    cohort_classes = set()
    for positions in combinations(range(width), replication):
        mask = sum(1 << position for position in positions)
        if all(
            sum(bool(mask & (1 << position)) for position in plate) == per_plate
            for plate in plate_positions.values()
        ):
            cohort_classes.add(normalize(mask, width))
    classes = sorted(cohort_classes, key=lambda mask: bit_string(mask, width))
    class_index = {mask: index for index, mask in enumerate(classes)}

    actions = []
    for permutation in group:
        actions.append(
            tuple(
                class_index[normalize(move_mask(mask, permutation, width), width)]
                for mask in classes
            )
        )
    return {
        "data": data,
        "width": width,
        "group": group,
        "classes": classes,
        "class_index": class_index,
        "actions": actions,
        "perm_to_action": dict(zip(group, actions)),
        "plate_positions": plate_positions,
    }


def encoded_design(design: Design) -> str:
    context = instance_context()
    width = context["width"]
    classes = context["classes"]
    rows = []
    for class_id in design:
        first = bit_string(classes[class_id], width)
        second = "".join("1" if bit == "0" else "0" for bit in first)
        rows.extend((first, second))
    return "/".join(rows)


def parse_representative(value: str) -> Design:
    context = instance_context()
    data = context["data"]
    width = context["width"]
    classes = context["classes"]
    class_index = context["class_index"]
    rows = value.split("/")
    assert len(rows) == 2 * len(data["cohort_order"])
    result = []
    for cohort in range(len(data["cohort_order"])):
        first, second = rows[2 * cohort : 2 * cohort + 2]
        assert re.fullmatch(rf"[01]{{{width}}}", first)
        assert re.fullmatch(rf"[01]{{{width}}}", second)
        assert first < second
        assert all(a != b for a, b in zip(first, second))
        mask = mask_from_string(first)
        assert mask in class_index
        assert bit_string(complement(mask, width), width) == second
        result.append(class_index[mask])
    return tuple(result)


def valid_design(design: Design) -> bool:
    context = instance_context()
    data = context["data"]
    width = context["width"]
    classes = context["classes"]
    plate_positions = context["plate_positions"]
    masks = [classes[index] for index in design]
    if len(set(masks)) != len(masks):
        return False

    rows = []
    for mask in masks:
        rows.extend((mask, complement(mask, width)))
    replication = data["incidence_rules"]["sample_replication"]
    per_plate = data["incidence_rules"]["per_plate_replication"]
    capacity = data["incidence_rules"]["pool_capacity"]
    if any(mask.bit_count() != replication for mask in rows):
        return False
    if any(
        sum(bool(mask & (1 << position)) for position in positions) != per_plate
        for mask in rows
        for positions in plate_positions.values()
    ):
        return False
    if any(
        sum(bool(mask & (1 << pool)) for mask in rows) != capacity
        for pool in range(width)
    ):
        return False

    for deleted in range(width):
        survivors = [mask & ~(1 << deleted) for mask in rows]
        if any(mask == 0 for mask in survivors) or len(set(survivors)) != len(rows):
            return False

    cohort_index = {
        cohort_id: index for index, cohort_id in enumerate(data["cohort_order"])
    }
    for rule in data["cohort_pair_rules"]["constraints"]:
        left = masks[cohort_index[rule["cohort_a"]]]
        right = masks[cohort_index[rule["cohort_b"]]]
        if quotient_distance(left, right, width) != rule["quotient_distance"]:
            return False
    return True


@lru_cache(maxsize=1)
def enumerate_valid_designs() -> frozenset[Design]:
    context = instance_context()
    data = context["data"]
    width = context["width"]
    classes = context["classes"]
    cohort_count = len(data["cohort_order"])
    cohort_index = {
        cohort_id: index for index, cohort_id in enumerate(data["cohort_order"])
    }
    restrictions: dict[tuple[int, int], int] = {}
    neighbors: list[list[tuple[int, int]]] = [[] for _ in range(cohort_count)]
    for rule in data["cohort_pair_rules"]["constraints"]:
        left = cohort_index[rule["cohort_a"]]
        right = cohort_index[rule["cohort_b"]]
        required = rule["quotient_distance"]
        restrictions[tuple(sorted((left, right)))] = required
        neighbors[left].append((right, required))
        neighbors[right].append((left, required))

    relation = {
        required: [
            {
                right
                for right in range(len(classes))
                if quotient_distance(classes[left], classes[right], width) == required
            }
            for left in range(len(classes))
        ]
        for required in set(restrictions.values())
    }
    order = sorted(
        range(cohort_count), key=lambda cohort: (-len(neighbors[cohort]), cohort)
    )
    assignment = [-1] * cohort_count
    completed: set[Design] = set()

    def visit(depth: int, available: set[int]) -> None:
        if depth == cohort_count:
            design = tuple(assignment)
            assert valid_design(design)
            completed.add(design)
            return
        cohort = order[depth]
        candidates = set(available)
        for other, required in neighbors[cohort]:
            assigned = assignment[other]
            if assigned >= 0:
                candidates.intersection_update(relation[required][assigned])
        for class_id in sorted(candidates):
            assignment[cohort] = class_id
            visit(depth + 1, available - {class_id})
            assignment[cohort] = -1

    visit(0, set(range(len(classes))))
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
            tuple(action[class_id] for class_id in seed) for action in actions
        }
        canonical = min(orbit)
        representatives.add(encoded_design(canonical))
        stabilizers[
            sum(
                tuple(action[class_id] for class_id in canonical) == canonical
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
        class_id = "".join(str(value) for value in representative)
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
    assert set(data["group_orders"]) == {"pool", "sample_swaps", "full"}
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
        assert re.fullmatch(r"[0-7]{8}", row["class_id"])
        exact_int(row["class_size"], positive=True)
        exact_int(row["fixed_normalized_designs"])


def test_generated_group_orders_and_representative_validity():
    """The disclosed groups are derived exactly and every representative is valid and canonical."""
    data = load_output()
    context = instance_context()
    instance = context["data"]
    group_orders = data["group_orders"]
    assert len(context["group"]) == instance["symmetry_rules"]["pool_group_order"]
    assert group_orders["pool"] == len(context["group"])
    assert (
        group_orders["sample_swaps"]
        == instance["symmetry_rules"]["sample_swap_group_order"]
    )
    assert group_orders["full"] == instance["symmetry_rules"]["full_group_order"]
    assert group_orders["full"] == group_orders["pool"] * group_orders["sample_swaps"]

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
    """Independent exhaustive search proves that no canonical class is missing or duplicated."""
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
    """Normalized and fully labelled totals equal the two required orbit–stabilizer sums."""
    data = load_output()
    valid_count = len(enumerate_valid_designs())
    counts = data["counts"]
    groups = data["group_orders"]
    assert counts["normalized_designs"] == valid_count
    assert counts["labelled_designs"] == valid_count * groups["sample_swaps"]
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
    """Every residual conjugacy class and fixed count is exact and Burnside gives the orbit total."""
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
        row["class_size"] * row["fixed_normalized_designs"] for row in submitted
    )
    assert data["burnside"]["numerator"] == numerator
    assert data["burnside"]["denominator"] == data["group_orders"]["pool"]
    assert (
        numerator
        == data["burnside"]["denominator"] * data["counts"]["equivalence_classes"]
    )


def test_complete_canonical_artifact_matches_reference():
    """The complete deterministic enumeration has the independently pinned canonical digest."""
    assert canonical_digest(load_output()) == EXPECTED_OUTPUT_SHA256
