A combinatorial chemistry group is scoping a substitution library built on macrocyclic ring scaffolds. `/app/input/ring_recipes.json` contains a `recipes` array. Each entry has:

- `recipe_id`: the identifier to preserve in the output.
- `n_positions`: the number of substitution sites around the closed ring.
- `symmetry_group`: either `cyclic` or `dihedral`.
- `composition`: exact counts of each substituent code; the values sum to `n_positions`.
- `forbidden_adjacent_pairs`: two-element lists of substituent codes that may not appear on adjacent ring positions, including the wraparound edge. A pair may name the same substituent code twice.

For each recipe, count the exact number of valid substitution patterns that use every unit of `composition`, avoid every forbidden adjacent pair, and are considered the same whenever the recipe's `symmetry_group` says they are.

Do not modify anything under `/app/input/`. Write `/app/output.json`, a single JSON object with exactly one key, `isomer_counts`, mapping every `recipe_id` from the input to its exact count as a JSON integer.
