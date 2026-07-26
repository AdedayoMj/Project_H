# Macrocycle Isomer Census

This repository contains a Harbor task about counting exact substitution-pattern
counts for ring recipes with symmetry and adjacency constraints.

Each recipe specifies a ring size, a symmetry group, an exact composition, and
a table of forbidden adjacent pairs. The verifier checks that the agent writes
`/app/output.json` with the required schema and the exact counts for every
recipe in the input.
