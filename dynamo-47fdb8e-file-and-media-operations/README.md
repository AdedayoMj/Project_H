# Carton Plate Recovery

This task reconstructs a seven-separation folding-carton production package from mislabeled, nonlinearly warped, photometrically distorted, and partially occluded scan evidence. The authoritative job ticket fixes the genuinely exact production identifiers and documents every equivalence rule and numerical tolerance.

The intended solve robustly identifies ink plates from contaminated spectral measurements, rejects fiducial outliers, inverts cubic scan warps and density curves, and fuses redundant observations onto the 600 dpi master grid. It then emits the recovered plate archive, a linear-light proof, an auditable manifest, an editable semantic SVG overlay, and a PDF/X-4 production proof.

Verification uses a hidden deterministic master. It checks solid-mask IoU, area and boundary geometry, ΔE00, multiscale SSIM, exact audit content, live SVG text and vector QR geometry, classified dielines, and functional PDF structure and rendering.

```bash
harbor run -p . --agent oracle
harbor run -p . --agent nop
```
