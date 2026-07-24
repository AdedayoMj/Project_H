# Macrocycle Isomer Census

A Harbor task (see [`task/`](task/)) that asks an agent to act as a combinatorial
chemist scoping a substitution library: for each of several macrocyclic ring
scaffolds, compute the exact number of physically distinct substitution
patterns consistent with a fixed feedstock composition and a steric-clash
table. Some recipes are cyclic-only and others are dihedral, so the task now
mixes rotation-only and rotation-plus-reflection symmetry in one input file.

## Overview

`task/environment/input/ring_recipes.json` (baked into `/app/input/` in the
container) lists 12 ring "recipes", each with `recipe_id`, `n_positions`,
`symmetry_group`, a `composition` (exact per-substituent counts summing to
`n_positions`), and `forbidden_adjacent_pairs` (a steric-clash table over the
ring's real physical edges, wraparound included). Several recipes remain small
enough to cross-check by brute force, but the larger mixed-symmetry cases
foreclose a naive generate-and-canonicalize approach.

The agent must, for every recipe, count physically distinct substitution
patterns up to that recipe's symmetry group, use every unit of the composition
and contain no forbidden adjacent pair, and write a single `/app/output.json`.

## Approach

The reference solution ([`task/solution/solve.py`](task/solution/solve.py))
applies Burnside's lemma over the symmetry group named by each recipe. For
every group element, its cycles on the ring positions are contracted to nodes
of a "quotient graph" (edges induced by real physical ring-adjacency crossing
cycle boundaries). This quotient graph is always exactly a path or a simple
cycle, so `|Fix(g)|` reduces to one generic content-constrained path/cycle
coloring DP (per-substituent remaining budget, previous node's color, and a
closing-edge check against the first node when the quotient graph is itself a
cycle). Summing `|Fix(g)|` over a recipe's symmetry group and dividing by the
group size gives each recipe's exact count. The whole mixed-symmetry run still
finishes quickly.

## Verification

[`task/tests/test_outputs.py`](task/tests/test_outputs.py) checks that
`/app/output.json` has the exact required schema, that every file under
`/app/input/` is byte-identical to a golden copy kept in `tests/` (never
visible to the agent), and that `isomer_counts` exactly matches the
reference solver's output (`tests/expected_output.json`) for all 7 recipes,
with no tolerance.

## Authoring validation (not part of the shipped task)

The Burnside/quotient-graph method was cross-validated during authoring
against a completely independent, differently-implemented brute-force
method (enumerate every distinct linear arrangement via next-permutation,
filter by the clash table, canonicalize under all 2n dihedral images, count
distinct canonical forms) across ~90 randomized small rings (n=3-14,
varying color counts and clash densities) plus explicit edge cases
(empty clash table, fully-forbidden clash table forcing a zero count,
single-substituent composition) -- see the prototype script this task was
derived from if that cross-check ever needs to be re-run or extended.

## Personal Use for clean up
# 1. Build the image directly from the Dockerfile (same one Harbor uses)
docker build -t macrocycle-isomer-census:dev task/environment

# 2. Clean-image check -- expect NO output
docker run --rm macrocycle-isomer-census:dev /bin/bash -lc \
  'find / \( -name solve.sh -o -name test.sh -o -name expected_output.json -o -name golden_input \) 2>/dev/null'

# 3. Remove that manually-built image, it was just for the check above
docker rmi macrocycle-isomer-census:dev

# 4. Repeatability check -- run oracle twice
harbor run -p task --agent oracle --job-name check-oracle-1
harbor run -p task --agent oracle --job-name check-oracle-2

# 5. Run nop twice
harbor run -p task --agent nop --job-name check-nop-1
harbor run -p task --agent nop --job-name check-nop-2

# 6. Clean up the job logs once you've confirmed the rewards look right
rm -rf jobs
