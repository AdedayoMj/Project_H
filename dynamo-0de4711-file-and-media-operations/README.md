# VFX Preservation Cull

This Harbor task asks an agent to recover the minimal reproducible asset package for a locked synthetic VFX sequence from a contaminated studio migration.

The instance combines drop-frame edit arithmetic, exact audio alignment, time-varying layered dependencies, variants, cycles, frame and UDIM expansion, conflicting signed fixity, revocations, rename journals, content equivalence, and atomic sequence-version rollback. The generated repository contains more than ten thousand physical entries but has a uniquely recoverable preservation closure.

The oracle derives a safe TAR package and four audit reports. Verification fingerprints all inputs, checks each report's semantics and reconciliation, validates archive safety and exact bytes, and compares the normalized result with hidden reference ground truth while allowing only the documented byte-identical source-copy equivalence.

```bash
harbor run -p . --agent oracle
harbor run -p . --agent nop
```
