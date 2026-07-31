# Irrigation world model

This Harbor task reconstructs four deterministic variable-rate irrigation field seasons. The image generates 6,573 analysis units across irregular fields with holes, heterogeneous multi-horizon soils, per-unit satellite time series, daily weather, and overlapping spatial irrigation events. The solver must produce a GeoJSON prescription, a matching unit table, and reconciled field and zone summaries.

The task uses one digest-pinned Python image for the agent and verifier. Shapely performs standards-compliant geometry operations; all Python dependencies are pinned in the Dockerfile. Agent-visible inputs and the normative contract are generated into `/app/input`, while the independent reference model and build-time input hashes remain verifier-only.

The verifier checks immutable inputs, exact schemas and identities, clipped geometry, soil storage, the daily FAO-56 state and audit quantities, capacity-limited prescriptions, fixed management zones, cross-artifact consistency, and aggregate reconciliation. The oracle and independently organized verifier model agree to a maximum observed relative difference of `2.56e-14`; acceptance retains the proposal's mixed absolute and 0.5% bands for equivalent implementations.

Run locally with:

```bash
harbor run -p task --agent oracle
harbor run -p task --agent nop
```
