# Executive Schedule & Inbox Triage

A Harbor task (see [`task/`](task/)) that asks an agent to act as an executive assistant
reconciling three overlapping calendars, a travel-time matrix, and scheduling policy notes
into a single conflict-free 33-day operating window, then triage an inbox against a fixed
classification rule.

## Overview

`task/environment/input/` (baked into `/app/input/` in the container) contains:

- `calendars/executive.json`, `calendars/operations.json`, `calendars/travel.json` — recurring
  and one-off events across ten venues in three timezone regions (US/UK/Singapore), with
  `movable`/`protected`/`vip_override` flags, `candidate_slots`, `dependencies` chained up to
  three deep, and RRULE-style recurrence (`count` or `until`, `byweekday`, `exdates`). The
  planning window (March 2 – April 3, 2026) spans both the US and UK DST transitions. 33
  movable events and their conflicting fixed counterparts form roughly a dozen conflict
  clusters: most are small and mostly-independent, but two are dense same-venue clusters
  (26 and 28 candidate meetings) each competing for a window that can only fit a fraction
  of their combined duration, with priorities deliberately clustered close together --
  genuinely NP-hard multiple-choice weighted interval scheduling instances, not just
  bigger versions of the small clusters.
- `travel_matrix.csv` — one-way transit minutes between every venue.
- `policy_notes.json` — the planning window, the after-hours cutoff, the exact objective to
  optimize (`objective_order` / `objective_definitions`), the exact conflict/precedence rules
  (`scheduling_rules`), and the exact deterministic inbox-classification procedure
  (`reply_rule_order` / `reply_rule_definitions`).
- `inbox/messages.json` — 24 inbox messages carrying the boolean fields the classification
  rule is written against, covering every precedence-order interaction among the flags.

The agent must expand every recurring event into concrete UTC occurrences, apply the
scheduling rules (which interact — a protected block can force a VIP-flagged meeting to
move, which can in turn make some other event's only remaining slot illegal, and VIP status
only *unlocks* displacing a protected block rather than guaranteeing it), search for the one
assignment that is optimal under the stated objective, classify every inbox message, and
write a single `/app/output.json`.

## Approach

The reference solution ([`task/solution/solve.py`](task/solution/solve.py)) expands
recurrences with `dateutil.rrule` plus `EXDATE` removal and `zoneinfo` for DST-correct
timezone conversion, computes each occurrence's legal placements, and partitions occurrences
into connected components by possible conflict or shared dependency. Each component is solved
for the assignment maximizing scheduled priority, then minimizing lateness, then moved count:
small components (the overwhelming majority) via exhaustive search, and the two large,
densely-packed components via OR-Tools CP-SAT (sequential lexicographic optimization, plus a
constraint-based check confirming each optimum is unique). It keeps every locally-tied-optimal
assignment, combines components to minimize total travel minutes, and breaks any remaining tie
by occurrence rank. Inbox replies are classified by applying `reply_rule_order` directly to
each message's boolean fields.

## Verification

[`task/tests/test_outputs.py`](task/tests/test_outputs.py) checks that `/app/output.json` has
the exact required schema, that every file under `/app/input/` is byte-identical to a golden
copy kept in `tests/` (never visible to the agent), and that `final_schedule`, `moved_items`,
`deferred_items`, `reply_categories`, and `objective_score` exactly match the reference
solver's output (`tests/expected_output.json`), which the hidden grader recomputes
independently against reference inputs.



## Personal Use for clean up
# 1. Build the image directly from the Dockerfile (same one Harbor uses)
docker build -t exec-schedule-triage:dev task/environment

# 2. Clean-image check -- expect NO output
docker run --rm exec-schedule-triage:dev /bin/bash -lc \
  'find / \( -name solve.sh -o -name test.sh -o -name expected_output.json -o -name golden_input \) 2>/dev/null'

# 3. Remove that manually-built image, it was just for the check above
docker rmi exec-schedule-triage:dev

# 4. Repeatability check -- run oracle twice
harbor run -p task --agent oracle --job-name check-oracle-1
harbor run -p task --agent oracle --job-name check-oracle-2

# 5. Run nop twice
harbor run -p task --agent nop --job-name check-nop-1
harbor run -p task --agent nop --job-name check-nop-2

# 6. Clean up the job logs once you've confirmed the rewards look right
rm -rf jobs