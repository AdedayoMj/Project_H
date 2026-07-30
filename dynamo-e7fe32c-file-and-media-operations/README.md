# Variable Font Revival

This task reconstructs a semantically editable three-axis OpenType variable font from 42 projectively distorted, mislabeled, noisy, stained, and partially occluded synthetic specimen scans. The hidden family contains 48 glyph programs, seven compatible masters, nonlinear axis mappings, kerning, ligatures, and mark attachment.

The intended solve robustly classifies each capture, rejects fiducial and measurement outliers, rectifies the specimens, fuses cell evidence, rebuilds compatible TrueType and UFO outlines, compiles functional variation and layout tables, and authors a normative proof and live-font PDF.

Verification uses a hidden reference family and withheld design-space coordinates. It audits every observation, parses the TTF, Designspace, UFOs, ZIP, JSON, PNG, and PDF, shapes hidden strings with HarfBuzz, and compares FreeType outlines, metrics, anchors, proof rendering, and PDF structure.

```bash
harbor run -p . --agent oracle
harbor run -p . --agent nop
```
