# T&E Reimbursement Audit

A Harbor task (see [`task/`](task/)) that asks an agent to act as a corporate
travel-and-expense (T&E) compliance analyst closing out one executive's
international, multi-city, mixed-business/personal trip against a full
written expense policy, producing an exact reimbursement figure and
compliance disposition for every expense line plus trip- and category-level
totals.

## Overview

`task/environment/input/` (baked into `/app/input/` in the container)
contains a 10-day, 3-city (NYC/London/Paris), 2-currency (GBP/EUR against a
USD home currency) itinerary with 25 expense lines (23 MEALS/AIRFARE plus 2
MILEAGE), a multi-hop dated FX-rate table, a directional distance matrix, a
mileage-rate table with an effective-date crossover, a per-country
VAT-reclaim table, a city-tier per-diem rate table, a single counterfactual
comparison airfare, and `policy.json`, which states every numeric parameter
and rule the agent must apply. The rule families: sleeping-city per-diem
lookup with first/last-day proration and personal-day zeroing; a two-hop,
two-dated FX conversion (transaction currency -> card-billing currency at
the transaction date, then -> home currency at the statement-post date)
with reimbursable-fee eligibility; VAT-reclaim subtraction by country and
category; a per-attendee blended meal cap that applies only to a genuine
client meal (flat solo cap otherwise, regardless of headcount); a
counterfactual airfare cap triggered by any personal day in the trip;
mileage by rate-effective-date and directional distance, minus a flat
commute deduction; exact-match duplicate-receipt exclusion; and a
category-aggregate approval threshold evaluated as a running cumulative
total in ascending (date, line_id) order, which can flag a line whose own
amount is small.

## Approach

The reference solution ([`task/solution/solve.py`](task/solution/solve.py))
runs every MEALS/AIRFARE line through the rule chain in order (VAT subtraction,
two-hop dated FX conversion rounded at each hop, category cap, fee
eligibility), computes MILEAGE lines from the distance matrix and
rate-effective-date table, detects exact-match duplicates, evaluates the
per-line and running-total category-aggregate approval thresholds, and
reconstructs per diem day by day directly from the itinerary. All money is
carried as integer home-currency cents throughout.

## Verification

[`task/tests/test_outputs.py`](task/tests/test_outputs.py) checks that
`/app/output.json` has the exact required schema, that every file under
`/app/input/` is byte-identical to a golden copy kept in `tests/` (never
visible to the agent), and that every line item and every `trip_summary`
field exactly matches the reference solver's output
(`tests/expected_output.json`), with no tolerance.

## Authoring validation (not part of the shipped task)

The reference solver's values were independently cross-checked at authoring
time by a from-scratch script (not importing or calling `solve.py`) that
hand-recomputes seven curated lines spanning every rule family -- a GBP and
a EUR meal through the full VAT/FX/cap/fee chain, the flat-vs-per-head meal
cap distinction, the airfare counterfactual comparison, both sides of the
mileage rate-effective-date boundary, and the full day-by-day per-diem sum
-- all of which matched exactly before trusting the solver on the full
25-line dataset.

## Personal Use for clean up
# 1. Build the image directly from the Dockerfile (same one Harbor uses)
docker build -t te-reimbursement-audit:dev task/environment

# 2. Clean-image check -- expect NO output
docker run --rm te-reimbursement-audit:dev /bin/bash -lc \
  'find / \( -name solve.sh -o -name test.sh -o -name expected_output.json -o -name golden_input \) 2>/dev/null'

# 3. Remove that manually-built image, it was just for the check above
docker rmi te-reimbursement-audit:dev

# 4. Repeatability check -- run oracle twice
harbor run -p task --agent oracle --job-name check-oracle-1
harbor run -p task --agent oracle --job-name check-oracle-2

# 5. Run nop twice
harbor run -p task --agent nop --job-name check-nop-1
harbor run -p task --agent nop --job-name check-nop-2

# 6. Clean up the job logs once you've confirmed the rewards look right
rm -rf jobs
