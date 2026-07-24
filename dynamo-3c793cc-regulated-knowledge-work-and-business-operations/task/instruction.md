Compute one executive T&E reimbursement audit from `/app/input/` and write `/app/output.json`. Do not modify anything under `/app/input/`.

Inputs:
- `itinerary.json`: trip days, each with `sleeping_city` and a personal-day flag.
- `policy.json`: rounding mode, per-diem proration fraction, meal caps, max reimbursable foreign fee, approval thresholds, commute deduction, and `trip_reimbursement_cap_cents`.
- `per_diem_rates.json`: nightly meals-and-incidentals rates by city.
- `expense_lines.csv`: rows with `line_id,date,statement_post_date,category (MEALS/AIRFARE/MILEAGE),vendor,country,amount,transaction_currency,card_billing_currency,foreign_fee_amount,fee_free_alternative_existed,attendees,payer,origin_city,destination_city,is_personal_expense,priority`.
  `attendees` is a `;`-separated list of `Name:TYPE` entries (`TYPE` is `CLIENT` or `INTERNAL`); empty means solo.
- `fx_rates.csv`: `(date,from_currency,to_currency) -> rate` to convert 1 unit of `from_currency`.
- `vat_reclaim_table.json`: VAT rate and reclaimable categories by country.
- `mileage_rates.json`: home-currency cents per mile by effective date.
- `distance_matrix.csv`: directional mileage between city pairs.
- `counterfactual_fares.json`: the comparison airfare used if the trip contains any personal day.

Process rows in file order. Use only these statuses: `APPROVED`, `NEEDS_MANAGER_APPROVAL`, `DUPLICATE_EXCLUDED`, `NON_REIMBURSABLE`, `DEFERRED_OVER_BUDGET`.

1. Exclusions
- `is_personal_expense=TRUE` -> `NON_REIMBURSABLE`, amount `0`.
- `MEALS` on a personal day -> `NON_REIMBURSABLE`, except a genuine client meal (`payer=EXECUTIVE` and at least one `CLIENT` attendee), which is business.
- If `(vendor, amount, date, category)` matches an earlier still-eligible line exactly, mark the later line `DUPLICATE_EXCLUDED`, amount `0`. Keep the first occurrence.

2. Reimbursable amount for non-excluded lines
- For `MEALS` and `AIRFARE`, if the country/category is VAT-eligible, subtract VAT from `amount` first and round half up to cents.
- Convert the remainder to home currency in two hops: `transaction_currency -> card_billing_currency` using the `date` rate, then `card_billing_currency -> home` using the `statement_post_date` rate. Round half up after each hop; if a hop is same-currency, leave it unchanged.
- `MEALS`: use the per-head cap only when `payer=EXECUTIVE` and at least one attendee is `CLIENT`; headcount is the number of listed attendees, excluding the executive. Otherwise cap at the solo amount.
- `AIRFARE`: if the trip has any personal day, cap each airfare line at the counterfactual fare; otherwise use the full converted fare.
- If `transaction_currency != card_billing_currency` and `fee_free_alternative_existed` is not `TRUE`, convert `foreign_fee_amount` the same way and add it, capped at the policy max fee.
- `MILEAGE`: skip VAT and fx; look up directional mileage for `(origin_city,destination_city)`, multiply by the date's rate, subtract the commute deduction, and floor at `0`.

3. Per diem
- Personal days pay `0`.
- Otherwise use the `sleeping_city` rate; if the day is the first or last calendar day, apply the policy proration fraction instead of the full rate.
- Reduce each day by that day's total reimbursable `MEALS` amount (use meal amounts before budget deferral; excluded meals count `0`), floored at `0`.
- Sum all days into `total_per_diem_cents`.

4. Approval flags
- For each category, walk the lines not marked `DUPLICATE_EXCLUDED` or `NON_REIMBURSABLE` in `(date, line_id)` order and keep a running total of their per-line amounts.
- A line gets `NEEDS_MANAGER_APPROVAL` if its own amount exceeds the per-line threshold or the running total through that line exceeds the category aggregate threshold.

5. Trip budget
- `gross_reimbursable_cents = total_per_diem_cents +` all surviving line amounts before budget deferral.
- If gross exceeds `trip_reimbursement_cap_cents`, defer whole surviving lines until the total fits; never defer per diem or a partial line.
- Keep the set of lines that maximizes, in order: 1) total retained priority weight (`priority p` contributes `6-p`), 2) total retained line amount, 3) tie-break by walking surviving lines in `(date, line_id)` order and keeping a line whenever that is still consistent with 1, 2, and the cap.
- Deferred lines become `DEFERRED_OVER_BUDGET` with amount `0`. Kept lines keep their earlier status, except flagged lines become `NEEDS_MANAGER_APPROVAL`.

6. Output
- Write `/app/output.json` as one JSON object with exactly `line_items` and `trip_summary`.
- `line_items`: one object per input row, in file order, each with `line_id`, `reimbursable_cents`, and `status`.
- `trip_summary`: `{"total_per_diem_cents": ..., "gross_reimbursable_cents": ..., "trip_budget_cap_cents": ..., "final_reimbursable_cents": ..., "deferred_over_budget_line_ids": [...], "total_flagged_for_approval_cents": ..., "category_breakdown": {"MEALS": {...}, "AIRFARE": {...}, "MILEAGE": {...}}}`.
- `gross_reimbursable_cents` is `total_per_diem_cents` plus every surviving line's amount before budget deferral.
- `final_reimbursable_cents` is `total_per_diem_cents` plus every kept line's amount and never exceeds the cap.
- `deferred_over_budget_line_ids` is the sorted list of deferred `line_id`s.
- Each category entry is `{"reimbursable_cents": <int>, "flagged_for_approval_cents": <int>}` summed over kept lines in that category only. A kept `NEEDS_MANAGER_APPROVAL` line counts toward both fields; deferred, duplicate, and non-reimbursable lines count toward neither. `total_flagged_for_approval_cents` sums only kept `NEEDS_MANAGER_APPROVAL` lines.
- All monetary values are integer home-currency cents, never strings or decimals.
