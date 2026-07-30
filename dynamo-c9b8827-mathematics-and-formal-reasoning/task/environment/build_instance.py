#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
from collections import deque
from itertools import combinations, permutations, product
from pathlib import Path


PLATES = ("A", "B", "C")
POSITIONS_PER_PLATE = 6
POOL_ORDER = [
    f"{plate}{position}"
    for plate in PLATES
    for position in range(POSITIONS_PER_PLATE)
]
WIDTH = len(POOL_ORDER)

COHORTS = [
    ("RESP", "respiratory-panel blinded aliquots"),
    ("ONC", "oncology-panel blinded aliquots"),
    ("CARD", "cardiology-panel blinded aliquots"),
    ("NEUR", "neurology-panel blinded aliquots"),
    ("IMMU", "immunology-panel blinded aliquots"),
    ("CTRL", "synthetic-control blinded aliquots"),
    ("RENAL", "renal-panel blinded aliquots"),
    ("META", "metabolic-panel blinded aliquots"),
]

# Three non-equivalent planted witnesses generate allowed invariant values. They
# are never copied into the agent environment. The public JSON contains only the
# derived orbit/profile restrictions, and both oracle and verifier enumerate all
# solutions independently from those restrictions.
WITNESS_SIGNATURES = [
    [
        ["001100100100110000", "010001001001001100", "100010010010000011"],
        ["001001010010001100", "010010100100000011", "100100001001110000"],
        ["000011000110000011", "001100011000110000", "110000100001001100"],
        ["001001001100110000", "010010000011001100", "100100110000000011"],
        ["001100000011001100", "010010011000000011", "100001100100110000"],
        ["000110001100000011", "010001100010110000", "101000010001001100"],
        ["000011010001110000", "011000101000001100", "100100000110000011"],
        ["000110100001001100", "011000010100000011", "100001001010110000"],
    ],
    [
        ["000011010010010100", "001100001001000011", "110000100100101000"],
        ["001001010010000110", "010010100100100001", "100100001001011000"],
        ["000110000011001100", "011000110000110000", "100001001100000011"],
        ["000011110000001001", "001100000011100100", "110000001100010010"],
        ["001100100100000110", "010010000011011000", "100001011000100001"],
        ["001100011000000110", "010001100001101000", "100010000110010001"],
        ["000110100010100001", "001001001100000110", "110000010001011000"],
        ["000110110000000011", "011000000101110000", "100001001010001100"],
    ],
    [
        ["000011001001001100", "010100010010000011", "101000100100110000"],
        ["000110100100001001", "011000010010100100", "100001001001010010"],
        ["000011011000110000", "001100000110001100", "110000100001000011"],
        ["000011110000001001", "001100000011100100", "110000001100010010"],
        ["001100100100100001", "010010000011000110", "100001011000011000"],
        ["000110010100001010", "011000100001110000", "100001001010000101"],
        ["001010000011001100", "010100011000110000", "100001100100000011"],
        ["000011010100100001", "001100000011011000", "110000101000000110"],
    ],
]

Permutation = tuple[int, ...]
CohortClass = tuple[int, ...]


def generator(generator_id: str, image: list[str]) -> dict:
    return {"generator_id": generator_id, "image": image}


def compose(left: Permutation, right: Permutation) -> Permutation:
    return tuple(left[right[index]] for index in range(WIDTH))


def close_group(generators: list[Permutation]) -> list[Permutation]:
    identity = tuple(range(WIDTH))
    known = {identity}
    pending = deque([identity])
    while pending:
        current = pending.popleft()
        for operation in generators:
            candidate = compose(operation, current)
            if candidate not in known:
                known.add(candidate)
                pending.append(candidate)
    return sorted(known)


def signature(mask: int) -> str:
    return "".join(
        "1" if mask & (1 << index) else "0"
        for index in range(WIDTH)
    )


def from_signature(value: str) -> int:
    return sum(
        1 << index for index, bit in enumerate(value) if bit == "1"
    )


def normalize_class(masks: CohortClass) -> CohortClass:
    return tuple(sorted(masks, key=signature))


def permute_mask(mask: int, operation: Permutation) -> int:
    return sum(
        1 << operation[source]
        for source in range(WIDTH)
        if mask & (1 << source)
    )


def permute_class(
    cohort_class: CohortClass, operation: Permutation
) -> CohortClass:
    return normalize_class(
        tuple(permute_mask(mask, operation) for mask in cohort_class)
    )


def encode_class(cohort_class: CohortClass) -> str:
    return "/".join(signature(mask) for mask in cohort_class)


def pool_generators() -> list[dict]:
    rows = []
    identity = list(POOL_ORDER)
    rotate = identity.copy()
    reflect = identity.copy()
    for plate_index, plate in enumerate(PLATES):
        offset = POSITIONS_PER_PLATE * plate_index
        for position in range(POSITIONS_PER_PLATE):
            rotate[offset + position] = (
                f"{plate}{(position + 1) % POSITIONS_PER_PLATE}"
            )
            reflect[offset + position] = (
                f"{plate}{(-position) % POSITIONS_PER_PLATE}"
            )
    rows.append(generator("rotate-all-plates", rotate))
    rows.append(generator("reflect-all-plates", reflect))

    for left_plate, right_plate in zip(PLATES, PLATES[1:]):
        swap = identity.copy()
        for position in range(POSITIONS_PER_PLATE):
            left_index = POOL_ORDER.index(f"{left_plate}{position}")
            right_index = POOL_ORDER.index(f"{right_plate}{position}")
            swap[left_index] = f"{right_plate}{position}"
            swap[right_index] = f"{left_plate}{position}"
        rows.append(
            generator(
                f"swap-plates-{left_plate}-{right_plate}",
                swap,
            )
        )
    return rows


def class_orbit_id(
    cohort_class: CohortClass, group: list[Permutation]
) -> str:
    return min(
        (
            encode_class(permute_class(cohort_class, operation))
            for operation in group
        ),
        key=str.encode,
    )


def pair_profile(
    left: CohortClass,
    right: CohortClass,
    plate_masks: list[int],
) -> str:
    best: tuple[int, ...] | None = None
    sample_orders = list(permutations(range(3)))
    for left_order in sample_orders:
        for right_order in sample_orders:
            plate_segments = [
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
                    for value in plate_segments[plate]
                )
                if best is None or candidate < best:
                    best = candidate
    assert best is not None
    return "".join(str(value) for value in best)


def triple_xor_profile(classes: list[CohortClass]) -> str:
    values = []
    for sample_indices in product(range(3), repeat=3):
        mask = 0
        for cohort_class, sample_index in zip(
            classes, sample_indices
        ):
            mask ^= cohort_class[sample_index]
        weight = mask.bit_count()
        values.append(min(weight, WIDTH - weight))
    return "".join(str(value) for value in sorted(values))


def quadruple_union_xor_profile(classes: list[CohortClass]) -> str:
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
                min(xor_weight, WIDTH - xor_weight),
            )
        )
    return "".join(
        f"{union_weight:02d}{xor_weight:02d}"
        for union_weight, xor_weight in sorted(values)
    )


def build(root: Path) -> None:
    input_dir = root / "input"
    if input_dir.exists():
        shutil.rmtree(input_dir)
    input_dir.mkdir(parents=True)

    # Geometric coordinates are descriptive only; clockwise_position and the
    # disclosed generator images normatively define the hexagon action.
    coordinates = [
        (0, 1),
        (0, 2),
        (1, 3),
        (2, 2),
        (2, 1),
        (1, 0),
    ]
    pools = []
    for plate in PLATES:
        for position, (row, column) in enumerate(coordinates):
            pools.append(
                {
                    "pool_id": f"{plate}{position}",
                    "plate": plate,
                    "clockwise_position": position,
                    "row": row,
                    "column": column,
                }
            )

    generator_rows = pool_generators()
    pool_index = {
        pool_id: index for index, pool_id in enumerate(POOL_ORDER)
    }
    operations = [
        tuple(pool_index[target] for target in row["image"])
        for row in generator_rows
    ]
    group = close_group(operations)
    assert len(group) == 72

    witnesses = [
        [
            normalize_class(
                tuple(from_signature(value) for value in cohort)
            )
            for cohort in witness
        ]
        for witness in WITNESS_SIGNATURES
    ]
    for witness in witnesses:
        assert all(
            len(cohort) == 3
            and all(mask.bit_count() == 2 * len(PLATES) for mask in cohort)
            and all(
                left & right == 0
                for left, right in combinations(cohort, 2)
            )
            and sum(cohort) == (1 << WIDTH) - 1
            for cohort in witness
        )
        sample_masks = [
            mask for cohort in witness for mask in cohort
        ]
        assert len(sample_masks) == len(set(sample_masks))
        assert all(
            (left ^ right).bit_count() > 2
            for left, right in combinations(sample_masks, 2)
        )
    cohort_rows = [
        {
            "cohort_id": cohort_id,
            "description": description,
            "samples": [
                f"{cohort_id}-01",
                f"{cohort_id}-02",
                f"{cohort_id}-03",
            ],
        }
        for cohort_id, description in COHORTS
    ]
    cohort_order = [row["cohort_id"] for row in cohort_rows]
    plate_masks = [
        sum(
            1 << index
            for index, pool in enumerate(pools)
            if pool["plate"] == plate
        )
        for plate in PLATES
    ]

    class_constraints = [
        {
            "cohort_id": cohort_id,
            "required_pool_orbit_id": class_orbit_id(
                witnesses[0][index], group
            ),
        }
        for index, cohort_id in enumerate(cohort_order)
    ]
    for witness in witnesses[1:]:
        assert all(
            class_orbit_id(witness[index], group)
            == class_constraints[index]["required_pool_orbit_id"]
            for index in range(len(cohort_order))
        )
    pair_constraints = []
    for left, right in combinations(range(len(cohort_order)), 2):
        profiles = {
            pair_profile(
                witness[left], witness[right], plate_masks
            )
            for witness in witnesses
        }
        pair_constraints.append(
            {
                "cohort_a": cohort_order[left],
                "cohort_b": cohort_order[right],
                "allowed_profiles": sorted(profiles),
            }
        )

    triple_index_sets = [
        (0, 1, 4),
        (1, 2, 5),
        (2, 3, 6),
        (3, 4, 7),
        (0, 5, 6),
        (1, 6, 7),
    ]
    triple_constraints = []
    for triple_indices in triple_index_sets:
        triple_profiles = {
            triple_xor_profile(
                [witness[index] for index in triple_indices]
            )
            for witness in witnesses
        }
        triple_constraints.append(
            {
                "cohorts": [
                    cohort_order[index] for index in triple_indices
                ],
                "allowed_profiles": sorted(triple_profiles),
            }
        )

    quadruple_index_sets = [
        (0, 1, 4, 6),
        (1, 2, 5, 7),
        (0, 2, 3, 7),
        (3, 4, 5, 6),
    ]
    quadruple_constraints = []
    for quadruple_indices in quadruple_index_sets:
        quadruple_profiles = {
            quadruple_union_xor_profile(
                [witness[index] for index in quadruple_indices]
            )
            for witness in witnesses
        }
        quadruple_constraints.append(
            {
                "cohorts": [
                    cohort_order[index]
                    for index in quadruple_indices
                ],
                "allowed_profiles": sorted(quadruple_profiles),
            }
        )

    instance = {
        "format_version": 4,
        "provenance": {
            "kind": "synthetic",
            "description": (
                "A deterministic three-hexagonal-plate diagnostic pooling instance "
                "modelling blinded three-sample cohorts, two simultaneous dropped "
                "wells, plate symmetries, and multi-cohort assay profiles."
            ),
        },
        "pool_order": POOL_ORDER,
        "pools": pools,
        "cohort_order": cohort_order,
        "cohorts": cohort_rows,
        "incidence_rules": {
            "matrix": (
                "Rows are samples in cohort_order and then listed sample order; "
                "columns are pool_order. Entry 1 means the sample is included."
            ),
            "binary": True,
            "samples_per_cohort": 3,
            "sample_replication": 6,
            "per_plate_replication": 2,
            "pool_capacity": 8,
            "cohort_pool_occupancy": (
                "Every pool contains exactly one of the three samples from every cohort."
            ),
            "maximum_simultaneous_pool_losses": 2,
            "pool_loss_separation": (
                "After deleting any subset of at most two pool columns, all twenty-four "
                "sample signatures remain pairwise distinct and every sample remains present."
            ),
            "distinct_cohort_classes": (
                "The eight unordered three-signature cohort partitions must be distinct."
            ),
        },
        "cohort_class_rules": {
            "pool_orbit_id": (
                "For one normalized three-signature cohort partition, apply every "
                "generated pool permutation, normalize by sorting its three signatures, "
                "encode them with '/', and take the UTF-8-byte-smallest encoding."
            ),
            "constraints": class_constraints,
        },
        "cohort_pair_rules": {
            "profile": (
                "For ordered cohorts X=cohort_a and Y=cohort_b, form on each plate "
                "the 3-by-3 matrix whose row i, column j entry counts pools containing "
                "both X sample i and Y sample j. Apply every independent permutation "
                "of X's three rows and Y's three columns and every plate order. Flatten "
                "all three matrices row-major, concatenate the twenty-seven single-digit "
                "entries, and take the lexicographically smallest string. X and Y are "
                "not transposed: the stated cohort_a/cohort_b order is normative."
            ),
            "constraints": pair_constraints,
        },
        "cohort_triple_rules": {
            "profile": (
                "For the three listed cohorts, choose one of the three sample masks "
                "from each in all 27 ways. XOR the three chosen masks; for weight w "
                "record min(w,18-w). Sort the 27 single-digit values and concatenate "
                "them. This profile is invariant under all within-cohort sample permutations."
            ),
            "constraints": triple_constraints,
        },
        "cohort_quadruple_rules": {
            "profile": (
                "For the four listed cohorts, choose one of the three sample masks "
                "from each in all 81 ways. Record the union weight u and, for XOR "
                "weight w, min(w,18-w). Sort the 81 (u, folded-XOR-weight) pairs and "
                "concatenate each pair as exactly four decimal digits: two for u then "
                "two for the folded XOR weight. This jointly constrains coverage and "
                "parity and is invariant under every within-cohort sample permutation."
            ),
            "constraints": quadruple_constraints,
        },
        "symmetry_rules": {
            "sample_action": (
                "The three samples inside each cohort may be permuted independently; "
                "cohorts themselves may not be permuted."
            ),
            "sample_permutation_group_order": 1679616,
            "pool_action": (
                "A generator image lists, in pool_order, the destination pool of each "
                "old pool. Close the generators under composition. Synchronized hexagon "
                "rotations/reflections and every whole-plate "
                "permutation are allowed."
            ),
            "pool_generators": generator_rows,
            "pool_group_order": 72,
            "full_group_order": 120932352,
            "equivalence": (
                "Two labelled incidence matrices are equivalent exactly when one is "
                "obtained from the other by a generated pool permutation and any "
                "independent within-cohort sample permutations."
            ),
        },
        "canonicalization_rules": {
            "signature": (
                "A sample signature is its eighteen 0/1 entries read in pool_order."
            ),
            "cohort_normalization": (
                "Within each cohort sort its three signatures lexicographically."
            ),
            "encoding": (
                "After cohort normalization, read cohorts in cohort_order, retain all "
                "three signatures in normalized order, and join the twenty-four "
                "signatures with one '/' byte."
            ),
            "canonical_representative": (
                "Apply every generated pool permutation, normalize every cohort, encode "
                "the result, and take the lexicographically smallest encoding."
            ),
            "representative_order": (
                "Sort canonical encodings by their exact UTF-8 bytes."
            ),
        },
        "enumeration_rules": {
            "labelled_designs": (
                "Sample IDs and pool IDs are fixed; every valid ordering of the three "
                "samples inside every cohort counts separately."
            ),
            "normalized_designs": (
                "First quotient labelled designs by the 1,679,616 independent "
                "within-cohort sample permutations, using cohort_normalization."
            ),
            "equivalence_classes": (
                "Then quotient normalized designs by the generated pool group. This "
                "two-stage quotient equals quotienting labelled designs by the full group."
            ),
            "stabilizer_size": (
                "For a normalized design, count pool-group elements that leave its "
                "ordered eight cohort classes unchanged. This equals the stabilizer size "
                "of its canonical labelled representative under the full group."
            ),
            "conjugacy": (
                "Use composition (g o h)[i] = g[h[i]] on pool-order indices. Conjugate "
                "g by h as h o g o inverse(h)."
            ),
            "permutation_word": (
                "Encode a pool permutation by concatenating each destination index "
                "from 0 through 17 as exactly two decimal digits in pool_order."
            ),
            "conjugacy_class_id": (
                "The lexicographically smallest permutation_word in the conjugacy class."
            ),
            "burnside": (
                "For every pool-group conjugacy class, fixed_normalized_designs is the "
                "number of valid normalized designs fixed by one (hence every) member. "
                "The Burnside numerator is the sum of class_size times that count."
            ),
        },
    }

    (input_dir / "pooling.json").write_text(
        json.dumps(instance, indent=2, ensure_ascii=False) + "\n"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("/app"))
    args = parser.parse_args()
    build(args.root)
