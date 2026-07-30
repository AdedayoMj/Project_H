Audit one executive T&E reimbursement from `/app/input/` and write `/app/output.json`. Do not modify `/app/input/`.

Inputs are `itinerary.json`, `policy.json`, `per_diem_rates.json`, and the header-defined `expense_lines.csv`, plus dated FX/VAT/mileage, directional-distance, and comparison-airfare lookups. Categories are `MEALS`, `AIRFARE`, `MILEAGE`. `attendees` is a `;`-separated `Name:CLIENT|INTERNAL` list; empty means solo.

Process rows in file order. Statuses are exactly `APPROVED`, `NEEDS_MANAGER_APPROVAL`, `DUPLICATE_EXCLUDED`, `NON_REIMBURSABLE`, `DEFERRED_OVER_BUDGET`.

1. Exclusions
- `is_personal_expense=TRUE` -> `NON_REIMBURSABLE`, amount `0`.
- `MEALS` on a personal day -> `NON_REIMBURSABLE`, except a genuine client meal (`payer=EXECUTIVE` and at least one `CLIENT` attendee), which is business.
- If `(vendor, amount, date, category)` exactly matches an earlier still-eligible line, mark the later line `DUPLICATE_EXCLUDED`, amount `0`.

2. Reimbursable amount for non-excluded lines
- VAT-eligible `MEALS` and `AIRFARE` are VAT-inclusive: `VAT = round_half_up(amount * rate)` to transaction-currency cents; net is `amount - VAT`, not `amount / (1 + rate)`.
- Convert transaction to card currency at `date`, then card to home at `statement_post_date`; round half up after each hop. Same-currency hops are unchanged.
- `MEALS`: use the per-head cap only when `payer=EXECUTIVE` and at least one attendee is `CLIENT`; headcount is the number of listed attendees, excluding the executive. Otherwise cap at the solo amount.
- `AIRFARE`: when any trip day is personal, cap each line at the counterfactual fare.
- When transaction/card currencies differ and `fee_free_alternative_existed != TRUE`, convert the fee likewise. Apply the meal/airfare cap first, then add the policy-capped fee.
- `MILEAGE`: skip VAT and fx; look up directional mileage for `(origin_city,destination_city)`, multiply by the date's rate, subtract the commute deduction, and floor at `0`.

3. Per diem
- Personal days pay `0`; others use the `sleeping_city` rate, prorated by the policy fraction on the first and last calendar days.
- Subtract that day's pre-deferral reimbursable `MEALS` (excluded meals count `0`), floor at `0`, and sum into `total_per_diem_cents`.

4. Approval flags
- By category, walk eligible lines in `(date, line_id)` order. A line gets `NEEDS_MANAGER_APPROVAL` if its amount exceeds the per-line threshold or its running category total exceeds the aggregate threshold.

5. Primary trip budget
- Gross is per diem plus all surviving amounts before deferral. If it exceeds the trip cap, defer whole lines; never defer per diem or part of a line.
- A retained set is *admissible* when per diem plus retained amounts does not exceed the cap; each portfolio's retained `line_units` sum is within its inclusive minimum/maximum (unlisted lines contribute `0`); and retaining `if_retained_line_id` makes the retained `then_line_ids` count fall between `minimum_then_retained` and `maximum_then_retained`, inclusive.
- A `retention_coverage_bonuses` entry earns its `bonus_points` exactly when at least `minimum_retained` of its `line_ids` are retained. Scores from overlapping entries all count.
- Priority `p` contributes weight `6-p`.
- Every line named unavailable by a recovery scenario must be retained in the primary set.

6. Post-close recovery
- Per recovery scenario, remove `unavailable_line_ids`, keep every other primary line, then reactivate at most `maximum_reactivated_lines` primary-deferred lines; unavailable lines cannot return.
- The recovery reserve is exactly the union of all reactivated lines. It contains at most `maximum_reserved_lines`; its certification units are the sum of `category_certification_units` for its lines and cannot exceed `maximum_certification_units`.
- A recovery is admissible under the additionally reduced cap. Portfolio maximums stay fixed; each minimum decreases (floor `0`) by its unavailable-line units. Re-evaluate commitments and bonuses.
- Jointly choose the primary and all recoveries to maximize, in order: 1) the minimum recovered priority weight, 2) primary priority weight, 3) minimum recovered coverage score, 4) sum of recovered coverage scores, 5) primary coverage score, 6) minimum recovered retained-line amount, 7) primary retained-line amount.
- Fix all seven optima. Greedily retain when joint feasibility remains, first over primary `(date,line_id)`, then over scenario `scenario_id` and deferred `(date,line_id)`, all ascending; keep prior decisions fixed.
- Primary-deferred lines become `DEFERRED_OVER_BUDGET` with amount `0`. Other surviving lines keep their earlier status, except flagged lines become `NEEDS_MANAGER_APPROVAL`. Recovery choices do not change primary line items.

7. Output
- Write `/app/output.json` as one JSON object with exactly `line_items` and `trip_summary`.
- `line_items`: one object per input row in file order, with `line_id`, `reimbursable_cents`, and `status`.
- `trip_summary` has exactly `total_per_diem_cents`, `gross_reimbursable_cents`, `trip_budget_cap_cents`, `final_reimbursable_cents`, `retained_priority_weight`, `retained_coverage_bonus_points`, `worst_case_recovery_priority_weight`, `worst_case_recovery_coverage_bonus_points`, `total_recovery_coverage_bonus_points`, `worst_case_recovery_final_reimbursable_cents`, sorted `recovery_reserve_line_ids`, `recovery_reserve_certification_units`, `retention_recovery_scenarios`, sorted `deferred_over_budget_line_ids`, `total_flagged_for_approval_cents`, and `category_breakdown`.
- `retention_recovery_scenarios` is in ascending `scenario_id`. Each entry has exactly `scenario_id`, sorted `unavailable_line_ids`, sorted `reactivated_line_ids`, `retained_priority_weight`, `retained_coverage_bonus_points`, and `final_reimbursable_cents`.
- Final reimbursement is per diem plus kept amounts. Each category is `{"reimbursable_cents": <int>, "flagged_for_approval_cents": <int>}` over primary-kept lines; flagged lines count in both, and the total flagged field sums categories.
- All numbers are non-negative JSON integers; monetary values are home-currency cents.
