Audit one executive T&E reimbursement from `/app/input/` and write `/app/output.json`. Do not modify `/app/input/`.

Inputs:
- `itinerary.json`: trip days with `sleeping_city` and personal-day flags.
- `policy.json`: rounding, caps, thresholds, commute deduction, trip budget, portfolios, commitments, coverage bonuses, and tie-break.
- `per_diem_rates.json`: city daily rates.
- `expense_lines.csv`: rows with `line_id,date,statement_post_date,category (MEALS/AIRFARE/MILEAGE),vendor,country,amount,transaction_currency,card_billing_currency,foreign_fee_amount,fee_free_alternative_existed,attendees,payer,origin_city,destination_city,is_personal_expense,priority`.
  `attendees` is a `;`-separated list of `Name:TYPE` entries (`TYPE` is `CLIENT` or `INTERNAL`); empty means solo.
- Lookup files provide dated currency-pair FX, country/category VAT reclaim, dated mileage rates, directional distances, and personal-day comparison airfare.

Process rows in file order. Statuses are exactly `APPROVED`, `NEEDS_MANAGER_APPROVAL`, `DUPLICATE_EXCLUDED`, `NON_REIMBURSABLE`, `DEFERRED_OVER_BUDGET`.

1. Exclusions
- `is_personal_expense=TRUE` -> `NON_REIMBURSABLE`, amount `0`.
- `MEALS` on a personal day -> `NON_REIMBURSABLE`, except a genuine client meal (`payer=EXECUTIVE` and at least one `CLIENT` attendee), which is business.
- If `(vendor, amount, date, category)` exactly matches an earlier still-eligible line, mark the later line `DUPLICATE_EXCLUDED`, amount `0`.

2. Reimbursable amount for non-excluded lines
- VAT-eligible `MEALS` and `AIRFARE` are VAT-inclusive: `VAT = round_half_up(amount * rate)` to transaction-currency cents; net is `amount - VAT`, not `amount / (1 + rate)`.
- Convert to home via `transaction_currency -> card_billing_currency` at the `date` rate, then `card_billing_currency -> home` at the `statement_post_date` rate. Round half up after each hop; same-currency hops are unchanged.
- `MEALS`: use the per-head cap only when `payer=EXECUTIVE` and at least one attendee is `CLIENT`; headcount is the number of listed attendees, excluding the executive. Otherwise cap at the solo amount.
- `AIRFARE`: if the trip has any personal day, cap each airfare line at the counterfactual fare; otherwise use the full converted fare.
- If `transaction_currency != card_billing_currency` and `fee_free_alternative_existed` is not `TRUE`, convert `foreign_fee_amount` likewise. Cap the meal or airfare first, then add the fee capped at the policy maximum.
- `MILEAGE`: skip VAT and fx; look up directional mileage for `(origin_city,destination_city)`, multiply by the date's rate, subtract the commute deduction, and floor at `0`.

3. Per diem
- Personal days pay `0`; others use the `sleeping_city` rate, prorated by the policy fraction on the first and last calendar days.
- Subtract that day's pre-deferral reimbursable `MEALS` (excluded meals count `0`), floor at `0`, and sum into `total_per_diem_cents`.

4. Approval flags
- By category, walk eligible lines in `(date, line_id)` order. A line gets `NEEDS_MANAGER_APPROVAL` if its amount exceeds the per-line threshold or its running category total exceeds the aggregate threshold.

5. Trip budget
- `gross_reimbursable_cents = total_per_diem_cents +` all surviving amounts before budget deferral.
- If gross exceeds `trip_reimbursement_cap_cents`, defer whole surviving lines until the total fits; never defer per diem or a partial line.
- A retained set is *admissible* when per diem plus retained amounts does not exceed the cap; each portfolio's retained `line_units` sum is within its inclusive minimum/maximum (unlisted lines contribute `0`); and retaining `if_retained_line_id` makes the retained `then_line_ids` count fall between `minimum_then_retained` and `maximum_then_retained`, inclusive.
- A `retention_coverage_bonuses` entry earns its `bonus_points` exactly when at least `minimum_retained` of its `line_ids` are retained. Scores from overlapping entries all count.
- Among admissible sets, keep the one that maximizes, in order: 1) total retained priority weight (`priority p` contributes `6-p`), 2) total earned coverage bonus points, 3) total retained line amount, 4) earlier lines.
- For 4), fix winning values `W`, `B`, and `A`; scan surviving lines in ascending `(date,line_id)`, retaining each when feasibility with those values and prior decisions remains, otherwise deferring it.
- Deferred lines become `DEFERRED_OVER_BUDGET` with amount `0`. Kept lines keep their earlier status, except flagged lines become `NEEDS_MANAGER_APPROVAL`.

6. Output
- Write `/app/output.json` as one JSON object with exactly `line_items` and `trip_summary`.
- `line_items`: one object per input row in file order, with `line_id`, `reimbursable_cents`, and `status`.
- `trip_summary`: `{"total_per_diem_cents": ..., "gross_reimbursable_cents": ..., "trip_budget_cap_cents": ..., "final_reimbursable_cents": ..., "retained_coverage_bonus_points": ..., "deferred_over_budget_line_ids": [...], "total_flagged_for_approval_cents": ..., "category_breakdown": {"MEALS": {...}, "AIRFARE": {...}, "MILEAGE": {...}}}`.
- `retained_coverage_bonus_points` is a non-negative JSON integer.
- `final_reimbursable_cents` is per diem plus every kept amount and never exceeds the cap.
- `deferred_over_budget_line_ids` is the sorted list of deferred `line_id`s.
- Each category entry is `{"reimbursable_cents": <int>, "flagged_for_approval_cents": <int>}` over kept lines only. Flagged kept lines count in both; `total_flagged_for_approval_cents` sums them across categories.
- All monetary values are integer home-currency cents, never strings or decimals.
