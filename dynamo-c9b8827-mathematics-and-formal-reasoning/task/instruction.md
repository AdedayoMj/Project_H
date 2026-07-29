Enumerate the exact symmetry classes of fault-tolerant diagnostic pooling designs defined by `/app/input/pooling.json`. That file is normative for incidence validity, single-pool-loss separation, cohort-pair restrictions, generated group actions, normalization, permutation composition, conjugacy class IDs, canonical encodings, and all ordering rules. Do not modify it. Every count is exact; no approximation or tolerance is accepted.

Write `/app/output.json` as one JSON object with exactly:

- `counts`: exactly `normalized_designs`, `labelled_designs`, and `equivalence_classes`, each a non-negative JSON integer with the meaning given in the input.
- `group_orders`: exactly `pool`, `sample_swaps`, and `full`, each a positive JSON integer.
- `stabilizer_histogram`: the ascending-by-`stabilizer_size` array of all nonzero bins. Each record has exactly `stabilizer_size` and `classes`, both positive JSON integers.
- `burnside`: exactly `numerator`, `denominator`, and `conjugacy_classes`. The first two are positive JSON integers. `conjugacy_classes` is ordered by `class_id`; each record has exactly `class_id`, `class_size`, and `fixed_normalized_designs`.
- `canonical_representatives`: the complete UTF-8-byte-sorted array containing the canonical encoding of every equivalence class exactly once.

The result must reconcile in all required ways. The representative count equals `equivalence_classes`; the stabilizer histogram accounts for every representative; orbit–stabilizer over the full group gives `labelled_designs`; the corresponding calculation over the pool group gives `normalized_designs`; every conjugacy-class record has the disclosed canonical ID and exact class size; and Burnside's numerator divided by its denominator gives `equivalence_classes`.

Every representative must decode to a valid complete incidence matrix, remain separating after deletion of each pool, satisfy every cohort-pair quotient distance, and be lexicographically canonical under the entire disclosed equivalence action. JSON object-key order and insignificant whitespace are immaterial; every array order is normative.
