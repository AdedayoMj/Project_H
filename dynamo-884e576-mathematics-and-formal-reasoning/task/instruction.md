Compute the exact kinetic visibility decomposition and optimal observer-handoff schedule for `/app/input/facility.json`. That file is normative for route parameterization, rational syntax, boundary and field-of-view semantics, critical-time minimality, legal handoffs, and the ordered objective. Do not modify it. All geometry decisions must be exact; no tolerance is accepted.

Write `/app/output.json` as one JSON object with exactly:

- `critical_times`: the strictly increasing array of all critical route times, including both domain endpoints, in canonical rational form.
- `open_intervals`: one record for every consecutive pair of critical times, in order. Each record has exactly `start`, `end`, and `visible`; the first two are those critical times and `visible` is the lexicographically sorted array of observer IDs visible throughout that maximal open interval.
- `event_visibility`: one record for every critical time, in order, with exactly `time` and `visible`. `visible` is the lexicographically sorted array of observer IDs visible at that singleton point.
- `schedule`: the ordered, maximally merged schedule. Each record has exactly `observer`, `start`, and `end` and denotes a non-empty closed range. Adjacent records must meet at the same critical time and must name different observers.
- `objective`: exactly `transition_cost`, `handoffs`, and `observer_sequence`. The first two are non-negative JSON integers. `observer_sequence` is the schedule's observer array after mandatory merging.

The open intervals and singleton events together must represent the whole route without an unnecessary critical time. The schedule must cover the complete route, assign an observer visible at every point of each closed range, obey every directed transition, and attain the facility file's three-level optimum. JSON key order and whitespace are immaterial; all array orderings are normative.
