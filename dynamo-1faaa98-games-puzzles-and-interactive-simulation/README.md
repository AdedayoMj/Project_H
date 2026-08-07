# Robust satellite downlink policy

This Harbor task asks an agent to synthesize a single spacecraft contact sequence that is evaluated simultaneously under four link-capacity scenarios. Packet releases and deadlines interact with finite storage, weighted overflow, solar charging, battery reserve, thermal recovery, and sequence-dependent antenna slew.

The generated evidence and normative contract live under `/app/input`. A submission writes a per-slot JSON plan, its exact CSV view, a robustness certificate, and a reusable solver. The verifier independently optimizes and replays all published missions, checks exact schemas and hashes, rejects feasible-but-suboptimal schedules, and runs the solver on an isolated private mission with changed timing, contacts, pointing, and resource constraints.

Run locally with:

```bash
harbor run -p task --agent oracle
harbor run -p task --agent nop
```
