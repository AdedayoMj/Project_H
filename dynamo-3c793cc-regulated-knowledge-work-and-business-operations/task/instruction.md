Audit the T&E inputs in `/app/input/`; do not change them. Write `/app/output.json`. CSV headers and policy tables are normative. Categories are `MEALS`, `AIRFARE`, `MILEAGE`; `attendees` is a `;`-separated `Name:CLIENT|INTERNAL` list. Process rows in file order. Statuses are `APPROVED`, `NEEDS_MANAGER_APPROVAL`, `DUPLICATE_EXCLUDED`, `NON_REIMBURSABLE`, `DEFERRED_OVER_BUDGET`.

1. Exclusions
- `is_personal_expense=TRUE`: `NON_REIMBURSABLE`, amount `0`.
- A personal-day meal is non-reimbursable unless `payer=EXECUTIVE` and an attendee is `CLIENT`.
- For duplicate `(vendor,amount,date,category)` among otherwise eligible lines, keep the first; later lines are `DUPLICATE_EXCLUDED`, amount `0`.

2. Eligible amounts
- For VAT-eligible meals/airfare, `VAT=round_half_up(amount*rate)` in transaction-currency cents; net is `amount-VAT`.
- Convert transaction→card at `date`, then card→home at `statement_post_date`, rounding half up after each hop; same-currency hops do nothing.
- Meals use the per-head cap only for an executive-paid meal with a client attendee. Headcount is listed attendees, excluding the executive; otherwise use the solo cap.
- If any trip day is personal, cap airfare at its counterfactual fare.
- If transaction/card currencies differ and `fee_free_alternative_existed != TRUE`, convert the fee identically. Cap the base amount first, then add the policy-capped fee.
- Mileage skips VAT/FX: directional miles × the date-effective rate, rounded half up, minus commute deduction, floored at `0`.

3. Per diem and approval
- Personal days pay `0`. Other days use the `sleeping_city` rate; apply policy proration on the first/last calendar days. Subtract same-day pre-deferral reimbursable meals, floor daily at `0`, and sum.
- Within each category, process eligible lines by `(date,line_id)`. Flag a line when it exceeds the per-line threshold or the running category total exceeds its aggregate threshold.

4. Primary retention
- Gross equals per diem plus all eligible pre-deferral amounts. Only whole lines may be deferred.
- A set is admissible iff per diem plus retained amounts is within the cap; every portfolio's retained `line_units` is within its inclusive bounds (unlisted lines add `0`); and each retained commitment trigger makes its retained companion count fall within the stated bounds.
- A coverage rule earns `bonus_points` iff at least `minimum_retained` listed lines are retained; overlapping bonuses all count. Priority `p` has weight `6-p`.
- Every scenario-unavailable line must be primary-retained.

5. Recoveries and certification
- For each scenario, remove its unavailable lines, keep other primary lines, then reactivate at most its stated number of primary-deferred lines. Unavailable lines cannot return.
- The reserve is exactly the union of reactivated lines and obeys both reserve-size and category-certification-unit limits.
- Each reserve line gets exactly one certification. Use a period no earlier than its `statement_post_date` release; the reviewer must accept its category; reviewer-period capacity, conditional precedence, and separation-group limits apply. Score = reviewer-period points + category adjustment.
- A recovery obeys its reduced cap. Portfolio maxima are unchanged; each minimum is reduced, no lower than `0`, by unavailable-line units. Re-evaluate commitments and bonuses.
- Jointly maximize lexicographically: (1) minimum recovery priority, (2) primary priority, (3) minimum recovery coverage, (4) total recovery coverage, (5) primary coverage, (6) certification score, (7) minimum recovery retained amount, (8) primary retained amount.
- Fix all optima. Greedily prefer retention while feasible: primary `(date,line_id)`, then scenario `scenario_id` and deferred `(date,line_id)`, ascending. Then, in primary `(date,line_id)` order, choose each reserve line's earliest feasible `(period,reviewer_id)`, period then reviewer ascending. Keep earlier decisions fixed.
- Primary-deferred lines become `DEFERRED_OVER_BUDGET`, amount `0`. Other eligible lines keep `APPROVED` or the approval flag. Recoveries do not alter primary line items.

6. Output
- The root has exactly `line_items` and `trip_summary`.
- `line_items` has one row per input row, in input order, with exactly `line_id`, `reimbursable_cents`, `status`.
- `trip_summary` has exactly `total_per_diem_cents`, `gross_reimbursable_cents`, `trip_budget_cap_cents`, `final_reimbursable_cents`, `retained_priority_weight`, `retained_coverage_bonus_points`, `worst_case_recovery_priority_weight`, `worst_case_recovery_coverage_bonus_points`, `total_recovery_coverage_bonus_points`, `worst_case_recovery_final_reimbursable_cents`, `recovery_reserve_line_ids`, `recovery_reserve_certification_units`, `recovery_reserve_certification_score`, `recovery_reserve_certifications`, `retention_recovery_scenarios`, `deferred_over_budget_line_ids`, `total_flagged_for_approval_cents`, `category_breakdown`.
- Sort reserve/deferred ID arrays. Sort certification rows by `line_id`; each has exactly `line_id`, `reviewer_id`, `period`, `certification_units`, `score_points`.
- Sort scenarios by `scenario_id`; each has exactly `scenario_id`, sorted `unavailable_line_ids`, sorted `reactivated_line_ids`, `retained_priority_weight`, `retained_coverage_bonus_points`, `final_reimbursable_cents`.
- Final reimbursement is per diem plus primary-kept amounts. Each category maps to exactly `reimbursable_cents` and `flagged_for_approval_cents`; flagged amounts count in both, and the total flagged field sums categories. All numbers are non-negative JSON integers; money is home-currency cents.
