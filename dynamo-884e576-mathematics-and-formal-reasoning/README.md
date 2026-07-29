# Exact Kinetic Visibility Handoffs

## One-sentence problem

The task is done when `/app/output.json` gives the unique exact visibility decomposition of the complete robot route and the globally optimal legal observer-handoff schedule.

## Success criteria (numbered, mirror instruction.md)

1. Leave `/app/input/facility.json` unchanged and follow its normative route parameterization, rational syntax, boundary/FOV semantics, critical-time definition, handoff rules, and ordered objective.
2. Emit the strictly increasing canonical `critical_times`, including both route-domain endpoints and no unnecessary event.
3. Emit one ordered `open_intervals` record per consecutive critical-time pair, with the exact lexicographically sorted observer set for that maximal open interval.
4. Emit one ordered `event_visibility` record per critical time, with the exact lexicographically sorted observer set at the singleton event.
5. Emit a complete, maximally merged `schedule` of non-empty closed ranges whose assigned observers remain visible throughout and whose directed handoffs are legal at critical times.
6. Emit `objective` with the exact transition cost, handoff count, compressed observer sequence, and handoff-time vector attaining minimum cost, then minimum handoffs, then the lexicographically smallest sequence, then the lexicographically earliest exact handoff times.

## Calibration results

- Golden solve.sh: reward 1.0
- Bad / nop solution: reward 0.0

## How to run

```bash
harbor run -p . --agent oracle   # reward 1.0
harbor run -p . --agent nop      # reward < 1.0
```

## Notes / open questions

No unresolved interpretation remains. Visibility on open cells and at critical singleton points is intentionally separate because closed obstacles can make a grazing event differ from both sides. Schedule ranges are closed, handoffs occur only at reported critical times, and rational values must use the reduced canonical syntax defined in `/app/input/facility.json`. If several schedules share the first three objective components, the earliest exact time at the first differing handoff position is selected.
