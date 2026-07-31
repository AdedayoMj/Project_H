# Variable Font Revival

## One-sentence problem

The task is done when `/app/output/` contains a correct, auditable, semantically editable, and production-functional three-axis OpenType variable-font revival reconstructed from the damaged specimen evidence.

## Success criteria (numbered, mirror instruction.md)

1. Leave `/app/input/` and `/app/evidence/` unchanged and apply the normative font, evidence, registration, calibration, rendering, schema, and tolerance contracts in `/app/input/job_ticket.json`.
2. Create `/app/output/recovered.ttf` with the exact ordered glyph repertoire and Unicode mappings, genuine compatible `wght`, `wdth`, and `opsz` variation, required naming and metrics, recovered kerning, standard ligatures, mark attachment, all required tables, and no forbidden bitmap, colour-glyph, or SVG fallback tables.
3. Create `/app/output/sources.zip` as a safe editable package containing the declared Designspace and seven UFO version 3 masters, with every glyph, compatible recovered contours, widths, kerning, anchors, and feature definitions, and no raster images.
4. Create `/app/output/manifest.json` with the exact normative schema, canonical family and axes, every recovered glyph-pattern digest and OpenType value, and a complete ordered capture audit whose axis classifications and image-to-page homographies meet the disclosed tolerances.
5. Create `/app/output/proof.png` as the exact RGB rendering of the submitted variable font under the normative proof contract at every declared text line, design-space location, position, size, and colour.
6. Create `/app/output/specimen.pdf` as the required one-page, unencrypted specimen with correct page geometry and metadata, rendering consistent with the proof PNG, every proof string retained as live selectable text, and the recovered font family embedded.
7. Ensure `/app/output/` contains exactly the five required artifacts and that their font structure, shaping behavior, source geometry, capture audit, rendering, and cross-artifact consistency satisfy every acceptance tolerance in the job ticket.

## Calibration results

- Golden `solve.sh`: reward 1.0
- Bad / nop solution: reward 0.0

## How to run

```bash
harbor run -p . --agent oracle   # reward 1.0
harbor run -p . --agent nop      # reward < 1.0
```

## Notes / open questions

No unresolved interpretation remains. `/app/input/job_ticket.json` is normative for the exact glyph inventory, nonlinear axis maps, compatible rounded-cell source geometry, trusted and untrusted evidence, capture layouts, calibration decoding, homography model, OpenType candidates, source-package structure, manifest schemas, proof rendering, PDF contract, and all numerical tolerances. Filenames and reported axis labels are deliberately unreliable. ZIP entry ordering, compression, timestamps, ownership, and permissions are non-semantic, while the declared JSON array ordering is normative and JSON object-key ordering and whitespace are not.
