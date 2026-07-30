# Exact Kinetic Visibility Handoffs

## One-sentence problem

The task is done when `/app/output.json` gives the unique exact visibility decomposition of the complete robot route and the globally optimal fault-tolerant observer-pair schedule.

## Success criteria (numbered, mirror instruction.md)

1. Leave `/app/input/facility.json` unchanged and follow its normative route parameterization, rational syntax, boundary/FOV semantics, critical-time definition, role eligibility, failure domains, fatigue, handoff rules, and ordered objective.
2. Emit the strictly increasing canonical `critical_times`, including both route-domain endpoints and no unnecessary event.
3. Emit one ordered `open_intervals` record per consecutive critical-time pair, with the exact lexicographically sorted observer set for that maximal open interval.
4. Emit one ordered `event_visibility` record per critical time, with the exact lexicographically sorted observer set at the singleton event.
5. Emit a complete, maximally merged `schedule` of non-empty closed ranges whose ordered primary/backup pair remains visible, role-eligible, failure-domain-separated, and fatigue-compliant throughout, with every changed role using a legal directed transition.
6. Emit `objective` with the exact minimum transition cost, then minimum maximum observer load, pair-handoff count, individual role-change count, and lexicographically smallest full closed-cell pair sequence.

## Calibration results

- Golden solve.sh: reward 1.0
- Bad / nop solution: reward 0.0

## How to run

```bash
harbor run -p . --agent oracle   # reward 1.0
harbor run -p . --agent nop      # reward < 1.0
```

## Notes / open questions

No unresolved interpretation remains. Visibility on open cells and at critical singleton points is intentionally separate because closed obstacles can make a grazing event differ from both sides. Schedule ranges are closed, both observers must cover both endpoints of every assigned cell, and handoffs occur only at reported critical times. Fatigue is enforced on every sliding cell window, and observer load counts cells served in either role. Rational values must use the reduced canonical syntax defined in `/app/input/facility.json`.
