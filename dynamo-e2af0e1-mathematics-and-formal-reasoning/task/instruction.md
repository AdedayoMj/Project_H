`/app/input/ring_recipes.json` contains a `recipes` array. Each entry has:

- `recipe_id`: the identifier to preserve in the output.
- `n_positions`: the number of substitution sites around the closed ring.
- `symmetry_group`: either `cyclic` or `dihedral`.
- `composition`: exact counts of each substituent code; the values sum to `n_positions`.
- `forbidden_adjacent_pairs`: two-element lists of substituent codes that may not appear on adjacent ring positions, including the wraparound edge. A pair may name the same substituent code twice.
- `required_adjacent_pair_counts` (optional): entries with `pair` (two substituent codes) and `count` (a non-negative integer). Each entry requires exactly that many physical ring edges to have the stated unordered endpoint pair. Count every ring edge once, including the wraparound edge; `[A, B]` and `[B, A]` describe the same contact, while `[A, A]` describes a self-contact. Unlisted pairs have no exact-count requirement but remain subject to `forbidden_adjacent_pairs`.

For each recipe, count the exact number of valid substitution patterns that use every unit of `composition`, avoid every forbidden adjacent pair, satisfy every required adjacent-pair count simultaneously, and are identified according to `symmetry_group`.

Do not modify anything under `/app/input/`. Write `/app/output.json` as a single JSON object with exactly one key, `isomer_counts`, mapping every `recipe_id` from the input to its exact count as a JSON integer.
