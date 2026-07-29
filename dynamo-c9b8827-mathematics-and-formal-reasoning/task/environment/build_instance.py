#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path


POOL_ORDER = ["A0", "A1", "A2", "A3", "B0", "B1", "B2", "B3"]

COHORTS = [
    {
        "cohort_id": "RESP",
        "description": "respiratory-panel blinded aliquots",
        "samples": ["RESP-01", "RESP-02"],
    },
    {
        "cohort_id": "ONC",
        "description": "oncology-panel blinded aliquots",
        "samples": ["ONC-01", "ONC-02"],
    },
    {
        "cohort_id": "CARD",
        "description": "cardiology-panel blinded aliquots",
        "samples": ["CARD-01", "CARD-02"],
    },
    {
        "cohort_id": "NEUR",
        "description": "neurology-panel blinded aliquots",
        "samples": ["NEUR-01", "NEUR-02"],
    },
    {
        "cohort_id": "IMMU",
        "description": "immunology-panel blinded aliquots",
        "samples": ["IMMU-01", "IMMU-02"],
    },
    {
        "cohort_id": "CTRL",
        "description": "synthetic-control blinded aliquots",
        "samples": ["CTRL-01", "CTRL-02"],
    },
]


def generator(generator_id: str, image: list[str]) -> dict:
    return {"generator_id": generator_id, "image": image}


def build(root: Path) -> None:
    input_dir = root / "input"
    if input_dir.exists():
        shutil.rmtree(input_dir)
    input_dir.mkdir(parents=True)

    pools = []
    coordinates = [(0, 0), (0, 1), (1, 1), (1, 0)]
    for plate in ("A", "B"):
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

    instance = {
        "format_version": 1,
        "provenance": {
            "kind": "synthetic",
            "description": (
                "A deterministic two-plate diagnostic pooling instance designed to "
                "model blinded cohort aliquots, dropped wells, plate symmetries, and "
                "co-assay balance restrictions."
            ),
        },
        "pool_order": POOL_ORDER,
        "pools": pools,
        "cohort_order": [row["cohort_id"] for row in COHORTS],
        "cohorts": COHORTS,
        "incidence_rules": {
            "matrix": (
                "Rows are samples in cohort_order and then listed sample order; "
                "columns are pool_order. Entry 1 means the sample is included."
            ),
            "binary": True,
            "sample_replication": 4,
            "per_plate_replication": 2,
            "pool_capacity": 6,
            "cohort_pool_occupancy": (
                "Every pool contains exactly one of the two samples from every cohort."
            ),
            "single_pool_loss": (
                "After deleting any one pool column, all twelve sample signatures "
                "remain pairwise distinct and every sample remains present."
            ),
            "distinct_cohort_classes": (
                "The six unordered complementary signature pairs must be distinct."
            ),
        },
        "cohort_pair_rules": {
            "definition": (
                "For cohorts X and Y, choose either member signature x and y. Let d "
                "be their Hamming distance in pool_order. The orientation-independent "
                "quotient distance is min(d, 8-d); swapping either cohort member does "
                "not change it."
            ),
            "constraints": [
                {"cohort_a": "RESP", "cohort_b": "ONC", "quotient_distance": 2},
                {"cohort_a": "ONC", "cohort_b": "CARD", "quotient_distance": 2},
                {"cohort_a": "CARD", "cohort_b": "NEUR", "quotient_distance": 4},
                {"cohort_a": "NEUR", "cohort_b": "IMMU", "quotient_distance": 2},
                {"cohort_a": "IMMU", "cohort_b": "CTRL", "quotient_distance": 4},
            ],
        },
        "symmetry_rules": {
            "sample_action": (
                "The two samples inside each cohort may be swapped independently; "
                "cohorts themselves may not be permuted."
            ),
            "sample_swap_group_order": 64,
            "pool_action": (
                "A generator image lists, in pool_order, the destination pool of each "
                "old pool. Close the generators under composition. Independent square "
                "rotations/reflections and whole-plate exchange are allowed."
            ),
            "pool_generators": [
                generator(
                    "rotate-A",
                    ["A1", "A2", "A3", "A0", "B0", "B1", "B2", "B3"],
                ),
                generator(
                    "reflect-A",
                    ["A0", "A3", "A2", "A1", "B0", "B1", "B2", "B3"],
                ),
                generator(
                    "rotate-B",
                    ["A0", "A1", "A2", "A3", "B1", "B2", "B3", "B0"],
                ),
                generator(
                    "reflect-B",
                    ["A0", "A1", "A2", "A3", "B0", "B3", "B2", "B1"],
                ),
                generator(
                    "swap-plates",
                    ["B0", "B1", "B2", "B3", "A0", "A1", "A2", "A3"],
                ),
            ],
            "pool_group_order": 128,
            "full_group_order": 8192,
            "equivalence": (
                "Two labelled incidence matrices are equivalent exactly when one is "
                "obtained from the other by a generated pool permutation and any "
                "independent within-cohort sample swaps."
            ),
        },
        "canonicalization_rules": {
            "signature": (
                "A sample signature is its eight 0/1 entries read in pool_order."
            ),
            "cohort_normalization": (
                "Within each cohort place the lexicographically smaller of its two "
                "complementary signatures first."
            ),
            "encoding": (
                "After cohort normalization, read cohorts in cohort_order, retain both "
                "signatures in their normalized order, and join the twelve signatures "
                "with one '/' byte."
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
                "Sample IDs and pool IDs are fixed; every valid orientation of every "
                "cohort pair counts separately."
            ),
            "normalized_designs": (
                "First quotient labelled designs by the 64 independent sample swaps, "
                "using cohort_normalization."
            ),
            "equivalence_classes": (
                "Then quotient normalized designs by the generated pool group. This "
                "two-stage quotient equals quotienting labelled designs by the full group."
            ),
            "stabilizer_size": (
                "For a normalized design, count pool-group elements that leave its "
                "ordered six cohort classes unchanged. This equals the stabilizer size "
                "of its canonical labelled representative under the full group."
            ),
            "conjugacy": (
                "Use composition (g o h)[i] = g[h[i]] on pool-order indices. Conjugate "
                "g by h as h o g o inverse(h)."
            ),
            "permutation_word": (
                "Encode a pool permutation by concatenating the decimal destination "
                "indices 0 through 7 of its image in pool_order."
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
