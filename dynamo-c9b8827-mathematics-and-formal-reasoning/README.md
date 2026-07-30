# Fault-Tolerant Pooling Enumeration

## One-sentence problem

The task is done when `/app/output.json` exactly enumerates every valid three-hexagonal-plate diagnostic pooling design up to the disclosed sample and plate symmetries and reconciles the enumeration through stabilizers, orbit–stabilizer, and Burnside's lemma.

## Success criteria

1. Count all normalized and fully labelled designs satisfying replication, plate balance, pool capacity, cohort-orbit, ordered contingency-profile, ternary-XOR-spectrum, fourth-order union/XOR-spectrum, GF(2) deletion-rank, and every one- and two-pool-loss separation rule.
2. Derive the generated pool and within-cohort sample-permutation group orders exactly.
3. Emit every equivalence class once as its UTF-8-byte-sorted canonical incidence encoding.
4. Compute the complete nonzero stabilizer histogram and reconcile both normalized and labelled totals by orbit–stabilizer.
5. Compute every residual pool-group conjugacy class and fixed-design count and reconcile the class total through Burnside's lemma.

## Calibration results

- Golden `solve.sh`: reward 1.0
- Bad / nop solution: reward < 1.0

## How to run

```bash
harbor run -p . --agent oracle   # reward 1.0
harbor run -p . --agent nop      # reward < 1.0
```

## Notes

The input is a deterministic synthetic laboratory-design instance. It contains 121,500 admissible balanced three-sample cohort partitions and ten ordered cohorts. Fully mixed cross-witness pair, ternary, and fourth-order decoys make local profiles deliberately non-identifying, while overlapping deletion-rank invariants enforce global compatibility. Independent within-cohort `S₃` sample actions are quotiented first, followed by the residual 72-element synchronized-hexagon and three-plate action.
