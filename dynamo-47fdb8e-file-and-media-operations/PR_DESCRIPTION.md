## One-sentence problem

The task is done when `/app/output/` contains a correct, auditable, and semantically editable folding-carton production package reconstructed from the corrupted scan evidence.

## Success criteria (numbered, mirror instruction.md)

1. Leave `/app/input/` and `/app/evidence/` unchanged and reconstruct all seven canonical production plates in `/app/output/plates.npz` as ordered two-dimensional `uint8` coverage planes on the specified master grid.
2. Create `/app/output/proof.png` from the submitted plates using the normative linear-light rendering model.
3. Create `/app/output/manifest.json` with the exact normative schema, canonical ink identities, correctly classified observation audit trail, recovered text, barcode payload, and PDF identifiers.
4. Create `/app/output/production.svg` with every required semantic role, live recovered text, vector QR modules, and an equivalent classified dieline.
5. Create `/app/output/production.pdf` as an unencrypted one-page PDF/X-4 proof with the correct page boxes, embedded output profile and fonts, live text, named spot `/Separation` colour spaces, optional-content roles, and rendering consistent with the proof PNG.
6. Ensure `/app/output/` contains exactly the five required artifacts and that every plate, geometric, colour, structural, and semantic result satisfies the tolerances in `/app/input/job_ticket.json`. For solid-mask metrics, use threshold 116 for varnish and 128 for every other plate.

## Calibration results

- Golden solve.sh: reward 1.0
- Bad / nop solution: reward < 1.0

## How to run

```bash
harbor run -p . --agent oracle   # reward 1.0
harbor run -p . --agent nop      # reward < 1.0
```

## Notes / open questions

No unresolved interpretation remains. `/app/input/job_ticket.json` is normative for trusted evidence, registration and rendering models, output schemas, semantic equivalence, and numerical tolerances. Evidence filenames and `reported_ink_untrusted` values are deliberately unreliable; plate identity must be recovered from the spectral evidence. SVG element order, group names, nesting, and Bézier representation are non-normative when the required semantic roles and geometric tolerances are satisfied.
