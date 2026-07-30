#!/usr/bin/env python3
"""Reference solver — T&E reimbursement audit (complex version).

Pipeline per trip:
  1. Per-line disposition + amount via the disclosed rule chain, including
     (a) personal-expense exclusion, (b) the personal-day-meal rule (a meal
     dated on a personal itinerary day is non-reimbursable UNLESS it is a
     genuine client meal), (c) exact-duplicate exclusion.
  2. Per-diem reconstructed day-by-day, then REDUCED per day by the reimbursed
     meals dated that day (anti-double-dip), floored at zero.
  3. Approval disposition (per-line threshold + running category-aggregate).
  4. Robust trip-budget optimization: choose a primary retained set and, for
     each disclosed post-close rejection scenario, a limited recovery from the
     primary-deferred claims. Every primary/recovery set obeys the cap,
     portfolios, commitments, coverage rules, and reserve-certification
     schedule. Eight objective levels and a primary/recovery/certification
     canonical walk make the joint solution unique.
All money is integer home-currency cents.
"""
from __future__ import annotations

import csv
import json
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from ortools.sat.python import cp_model

INPUT = Path("/app/input")
OUTPUT = Path("/app/output.json")
CENT = Decimal("0.01")


def round_cents(amount: Decimal) -> int:
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def load_json(name: str) -> dict:
    return json.loads((INPUT / name).read_text())


def load_fx_rates():
    rates = {}
    with (INPUT / "fx_rates.csv").open(newline="") as h:
        for row in csv.DictReader(h):
            rates[(row["date"], row["from_currency"], row["to_currency"])] = Decimal(row["rate"])
    return rates


def fx_rate(rates, date, a, b):
    return Decimal("1") if a == b else rates[(date, a, b)]


def load_distance_matrix():
    m = {}
    with (INPUT / "distance_matrix.csv").open(newline="") as h:
        for row in csv.DictReader(h):
            m[(row["origin_city"], row["destination_city"])] = Decimal(row["miles"])
    return m


def mileage_rate_cents(table, date):
    for e in table:
        if e["effective_start"] <= date <= e["effective_end"]:
            return e["rate_cents_per_mile"]
    raise ValueError(f"no mileage rate covers {date}")


def two_hop(rates, amt, txn_date, post_date, txn_ccy, card_ccy, home_ccy) -> int:
    r1 = fx_rate(rates, txn_date, txn_ccy, card_ccy)
    h1 = round_cents(amt * r1) if txn_ccy != card_ccy else round_cents(amt)
    r2 = fx_rate(rates, post_date, card_ccy, home_ccy)
    return round_cents((Decimal(h1) / 100) * r2) if card_ccy != home_ccy else h1


def vat_net(amt, country, category, vat):
    e = vat.get(country)
    if e and category in e["eligible_categories"]:
        v = (amt * Decimal(str(e["rate"]))).quantize(CENT, rounding=ROUND_HALF_UP)
        return amt - v
    return amt


def meal_or_air(line, policy, rates, vat, personal_day, cf):
    home = policy["home_currency"]
    txn, card = line["transaction_currency"], line["card_billing_currency"]
    amt = Decimal(line["amount"])
    net = vat_net(amt, line["country"], line["category"], vat)
    home_cents = two_hop(rates, net, line["date"], line["statement_post_date"], txn, card, home)
    if line["category"] == "MEALS":
        att = [a for a in line["attendees"].split(";") if a]
        headcount = max(1, len(att))
        is_client = line["payer"] == "EXECUTIVE" and any(a.split(":")[1] == "CLIENT" for a in att)
        cap = policy["client_meal_cap_cents_per_head"] * headcount if is_client else policy["solo_meal_cap_cents"]
        capped = min(home_cents, cap)
    else:
        capped = min(home_cents, cf) if personal_day else home_cents
    fee = 0
    if txn != card and line["fee_free_alternative_existed"] != "TRUE":
        fa = Decimal(line["foreign_fee_amount"])
        fee_home = two_hop(rates, fa, line["date"], line["statement_post_date"], txn, card, home)
        fee = min(fee_home, policy["max_foreign_fee_reimbursement_cents"])
    return capped + fee


def process_mileage(line, policy, dist, mrates):
    miles = dist[(line["origin_city"], line["destination_city"])]
    rate = mileage_rate_cents(mrates, line["date"])
    gross = round_cents(miles * Decimal(rate) / 100)
    return max(0, gross - policy["normal_commute_deduction_cents"])


def is_client_meal(line):
    att = [a for a in line["attendees"].split(";") if a]
    return line["payer"] == "EXECUTIVE" and any(a.split(":")[1] == "CLIENT" for a in att)


def compute_per_diem_by_day(itinerary, rates_by_city, fraction: Decimal):
    days = itinerary["days"]
    first, last = min(d["date"] for d in days), max(d["date"] for d in days)
    out = {}
    for d in days:
        if d["is_personal_day"]:
            out[d["date"]] = 0
            continue
        r = Decimal(rates_by_city[d["sleeping_city"]])
        if d["date"] in (first, last):
            r = r * fraction
        out[d["date"]] = round_cents(r / 100)
    return out


def solve_budget_knapsack(
    reimb_lines,
    per_diem_total,
    cap,
    retention_portfolios,
    retention_commitments,
    retention_coverage_bonuses,
    retention_recovery_scenarios,
    retention_recovery_reserve,
    retention_recovery_certification_schedule,
    tie_break,
):
    """Jointly optimize the primary closeout and every rejection recovery."""
    budget = cap - per_diem_total

    def coverage_score(retained):
        return sum(
            rule["bonus_points"]
            for rule in retention_coverage_bonuses
            if len(retained.intersection(rule["line_ids"])) >= rule["minimum_retained"]
        )

    sort_keys = tuple(tie_break["sort_keys"])
    order = sorted(
        reimb_lines,
        key=lambda l: tuple(l[k] for k in sort_keys),
        reverse=tie_break["direction"] == "descending",
    )
    scenarios = sorted(
        retention_recovery_scenarios,
        key=lambda scenario: scenario["scenario_id"],
    )
    known_line_ids = {line["line_id"] for line in order}
    referenced_line_ids = {
        line_id
        for rule in retention_portfolios
        for line_id in rule["line_units"]
    }
    referenced_line_ids.update(
        line_id
        for rule in retention_commitments
        for line_id in [rule["if_retained_line_id"], *rule["then_line_ids"]]
    )
    referenced_line_ids.update(
        line_id
        for rule in retention_coverage_bonuses
        for line_id in rule["line_ids"]
    )
    referenced_line_ids.update(
        line_id
        for scenario in scenarios
        for line_id in scenario["unavailable_line_ids"]
    )
    assert referenced_line_ids <= known_line_ids, (
        "retention rules may reference only surviving reimbursable lines"
    )
    assert len({scenario["scenario_id"] for scenario in scenarios}) == len(scenarios)
    line_by_id = {line["line_id"]: line for line in order}
    max_weight = sum(line["weight"] for line in order)
    max_bonus = sum(rule["bonus_points"] for rule in retention_coverage_bonuses)
    max_amount = sum(line["amount"] for line in order)

    def add_admissibility(model, selected, scenario=None):
        reduction = 0 if scenario is None else scenario["additional_cap_reduction_cents"]
        model.Add(
            sum(selected[line["line_id"]] * line["amount"] for line in order)
            <= budget - reduction
        )
        unavailable = set() if scenario is None else set(scenario["unavailable_line_ids"])
        for rule in retention_portfolios:
            units = sum(
                unit * selected[line_id]
                for line_id, unit in rule["line_units"].items()
            )
            waived_units = sum(
                rule["line_units"].get(line_id, 0) for line_id in unavailable
            )
            minimum = max(0, rule["minimum_retained_units"] - waived_units)
            model.Add(units >= minimum)
            model.Add(units <= rule["maximum_retained_units"])
        for rule in retention_commitments:
            companion_count = sum(selected[line_id] for line_id in rule["then_line_ids"])
            trigger = selected[rule["if_retained_line_id"]]
            model.Add(
                companion_count >= rule["minimum_then_retained"]
            ).OnlyEnforceIf(trigger)
            model.Add(
                companion_count <= rule["maximum_then_retained"]
            ).OnlyEnforceIf(trigger)

    def add_coverage(model, selected, name):
        earned = []
        for rule in retention_coverage_bonuses:
            met = model.NewBoolVar(f"{name}__{rule['bonus_id']}")
            count = sum(selected[line_id] for line_id in rule["line_ids"])
            model.Add(count >= rule["minimum_retained"]).OnlyEnforceIf(met)
            model.Add(count <= rule["minimum_retained"] - 1).OnlyEnforceIf(met.Not())
            earned.append(rule["bonus_points"] * met)
        return sum(earned)

    certification = retention_recovery_certification_schedule
    periods = tuple(certification["period_order"])
    period_index = {
        period: index for index, period in enumerate(periods)
    }
    reviewers = tuple(
        sorted(
            certification["reviewers"],
            key=lambda reviewer: reviewer["reviewer_id"],
        )
    )

    def build(
        fixed=None,
        primary_forced=None,
        recovery_forced=None,
        certification_forced=None,
    ):
        m = cp_model.CpModel()
        x = {
            line["line_id"]: m.NewBoolVar(f"primary__{line['line_id']}")
            for line in order
        }
        add_admissibility(m, x)
        primary_w = sum(x[line["line_id"]] * line["weight"] for line in order)
        primary_b = add_coverage(m, x, "primary")
        primary_a = sum(x[line["line_id"]] * line["amount"] for line in order)

        recovery_x = {}
        recovery_w = {}
        recovery_b = {}
        recovery_a = {}
        activation_by_line = {line["line_id"]: [] for line in order}
        for scenario in scenarios:
            sid = scenario["scenario_id"]
            unavailable = set(scenario["unavailable_line_ids"])
            for line_id in unavailable:
                m.Add(x[line_id] == 1)
            selected = {
                line["line_id"]: m.NewBoolVar(f"{sid}__{line['line_id']}")
                for line in order
            }
            activations = []
            for line in order:
                line_id = line["line_id"]
                if line_id in unavailable:
                    m.Add(selected[line_id] == 0)
                else:
                    m.Add(selected[line_id] >= x[line_id])
                    activation = selected[line_id] - x[line_id]
                    activations.append(activation)
                    activation_by_line[line_id].append(activation)
            m.Add(sum(activations) <= scenario["maximum_reactivated_lines"])
            add_admissibility(m, selected, scenario)
            recovery_x[sid] = selected
            recovery_w[sid] = sum(
                selected[line["line_id"]] * line["weight"] for line in order
            )
            recovery_b[sid] = add_coverage(m, selected, sid)
            recovery_a[sid] = sum(
                selected[line["line_id"]] * line["amount"] for line in order
            )

        reserve = {}
        for line in order:
            lid = line["line_id"]
            reserve[lid] = m.NewBoolVar(f"reserve__{lid}")
            m.Add(reserve[lid] <= 1 - x[lid])
            for activation in activation_by_line[lid]:
                m.Add(reserve[lid] >= activation)
            m.Add(reserve[lid] <= sum(activation_by_line[lid]))
        m.Add(
            sum(reserve.values())
            <= retention_recovery_reserve["maximum_reserved_lines"]
        )
        category_units = retention_recovery_reserve["category_certification_units"]
        m.Add(
            sum(
                reserve[line["line_id"]] * category_units[line["category"]]
                for line in order
            )
            <= retention_recovery_reserve["maximum_certification_units"]
        )

        certification_assignment = {}
        certification_period = {}
        certification_score_terms = []
        category_adjustment = certification[
            "category_score_adjustment"
        ]
        for line in order:
            lid = line["line_id"]
            release_period = certification[
                "release_period_by_statement_post_date"
            ][line["statement_post_date"]]
            release_index = period_index[release_period]
            options = {}
            for reviewer in reviewers:
                if line["category"] not in reviewer["eligible_categories"]:
                    continue
                reviewer_id = reviewer["reviewer_id"]
                for period in periods[release_index:]:
                    option = m.NewBoolVar(
                        f"cert__{lid}__{reviewer_id}__{period}"
                    )
                    options[(reviewer_id, period)] = option
                    score = (
                        reviewer["score_points_by_period"][period]
                        + category_adjustment[line["category"]]
                    )
                    certification_score_terms.append(score * option)
            m.Add(sum(options.values()) == reserve[lid])
            certification_assignment[lid] = options
            certification_period[lid] = sum(
                period_index[period] * option
                for (_, period), option in options.items()
            )

        for reviewer in reviewers:
            reviewer_id = reviewer["reviewer_id"]
            for period in periods:
                m.Add(
                    sum(
                        retention_recovery_reserve[
                            "category_certification_units"
                        ][line["category"]]
                        * certification_assignment[line["line_id"]].get(
                            (reviewer_id, period), 0
                        )
                        for line in order
                    )
                    <= reviewer["capacity_units_by_period"][period]
                )

        big_m = len(periods)
        for rule in certification["conditional_precedence"]:
            before = rule["before_line_id"]
            after = rule["after_line_id"]
            m.Add(
                certification_period[before] + 1
                <= certification_period[after]
                + big_m * (2 - reserve[before] - reserve[after])
            )

        for rule in certification["period_separation_groups"]:
            for period in periods:
                m.Add(
                    sum(
                        certification_assignment[line_id].get(
                            (reviewer["reviewer_id"], period), 0
                        )
                        for line_id in rule["line_ids"]
                        for reviewer in reviewers
                    )
                    <= rule["maximum_in_one_period"]
                )

        certification_score = sum(certification_score_terms)
        worst_w = m.NewIntVar(0, max_weight, "worst_recovery_priority_weight")
        worst_b = m.NewIntVar(0, max_bonus, "worst_recovery_coverage_bonus")
        worst_a = m.NewIntVar(0, max_amount, "worst_recovery_amount")
        for scenario in scenarios:
            sid = scenario["scenario_id"]
            m.Add(worst_w <= recovery_w[sid])
            m.Add(worst_b <= recovery_b[sid])
            m.Add(worst_a <= recovery_a[sid])
        total_b = sum(recovery_b.values())
        objectives = {
            "worst_recovery_priority_weight": worst_w,
            "primary_priority_weight": primary_w,
            "worst_recovery_coverage_bonus_points": worst_b,
            "total_recovery_coverage_bonus_points": total_b,
            "primary_coverage_bonus_points": primary_b,
            "reserve_certification_score": certification_score,
            "worst_recovery_amount": worst_a,
            "primary_retained_amount": primary_a,
        }
        for key, value in (fixed or {}).items():
            m.Add(objectives[key] == value)
        for lid, val in (primary_forced or {}).items():
            m.Add(x[lid] == val)
        for (sid, lid), val in (recovery_forced or {}).items():
            m.Add(recovery_x[sid][lid] == val)
        for (lid, reviewer_id, period), val in (
            certification_forced or {}
        ).items():
            m.Add(
                certification_assignment[lid][
                    (reviewer_id, period)
                ]
                == val
            )
        return (
            m,
            x,
            recovery_x,
            certification_assignment,
            objectives,
        )

    s = cp_model.CpSolver()
    s.parameters.num_search_workers = 4
    objective_order = list(tie_break["applies_after"])
    fixed = {}
    for key in objective_order:
        m, x, recovery_x, cert_x, objectives = build(fixed=fixed)
        m.Maximize(objectives[key])
        assert s.Solve(m) == cp_model.OPTIMAL
        fixed[key] = int(s.Value(objectives[key]))

    primary_forced = {}
    for line in order:
        lid = line["line_id"]
        trial = {**primary_forced, lid: 1}
        m, x, recovery_x, cert_x, objectives = build(
            fixed=fixed,
            primary_forced=trial,
        )
        status = s.Solve(m)
        primary_forced[lid] = 1 if status in (cp_model.OPTIMAL, cp_model.FEASIBLE) else 0

    recovery_forced = {}
    for scenario in scenarios:
        sid = scenario["scenario_id"]
        unavailable = set(scenario["unavailable_line_ids"])
        for line in order:
            lid = line["line_id"]
            if lid in unavailable or primary_forced[lid]:
                continue
            trial = {**recovery_forced, (sid, lid): 1}
            m, x, recovery_x, cert_x, objectives = build(
                fixed=fixed,
                primary_forced=primary_forced,
                recovery_forced=trial,
            )
            status = s.Solve(m)
            recovery_forced[(sid, lid)] = (
                1 if status in (cp_model.OPTIMAL, cp_model.FEASIBLE) else 0
            )

    m, x, recovery_x, cert_x, objectives = build(
        fixed=fixed,
        primary_forced=primary_forced,
        recovery_forced=recovery_forced,
    )
    assert s.Solve(m) in (cp_model.OPTIMAL, cp_model.FEASIBLE)

    reserve_line_ids = {
        lid
        for lid, options in cert_x.items()
        if any(s.Value(option) for option in options.values())
    }
    certification_forced = {}
    for line in order:
        lid = line["line_id"]
        if lid not in reserve_line_ids:
            continue
        options = sorted(
            cert_x[lid],
            key=lambda option: (
                period_index[option[1]],
                option[0],
            ),
        )
        for reviewer_id, period in options:
            trial = {
                **certification_forced,
                (lid, reviewer_id, period): 1,
            }
            trial_model, _, _, _, _ = build(
                fixed=fixed,
                primary_forced=primary_forced,
                recovery_forced=recovery_forced,
                certification_forced=trial,
            )
            status = s.Solve(trial_model)
            if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                certification_forced[
                    (lid, reviewer_id, period)
                ] = 1
                break
        else:
            raise RuntimeError(
                f"no canonical certification option for {lid}"
            )

    m, x, recovery_x, cert_x, objectives = build(
        fixed=fixed,
        primary_forced=primary_forced,
        recovery_forced=recovery_forced,
        certification_forced=certification_forced,
    )
    assert s.Solve(m) in (cp_model.OPTIMAL, cp_model.FEASIBLE)
    primary_retained = {lid for lid, val in primary_forced.items() if val}
    recovery_results = []
    recovery_weights = []
    recovery_bonuses = []
    recovery_amounts = []
    for scenario in scenarios:
        sid = scenario["scenario_id"]
        recovered = {
            lid for lid in known_line_ids if s.Value(recovery_x[sid][lid])
        }
        reactivated = recovered - primary_retained
        weight = sum(line_by_id[lid]["weight"] for lid in recovered)
        bonus = coverage_score(recovered)
        amount = sum(line_by_id[lid]["amount"] for lid in recovered)
        recovery_weights.append(weight)
        recovery_bonuses.append(bonus)
        recovery_amounts.append(amount)
        recovery_results.append(
            {
                "scenario_id": sid,
                "unavailable_line_ids": sorted(scenario["unavailable_line_ids"]),
                "reactivated_line_ids": sorted(reactivated),
                "retained_priority_weight": weight,
                "retained_coverage_bonus_points": bonus,
                "final_reimbursable_cents": per_diem_total + amount,
            }
        )
    metrics = {
        "retained_priority_weight": sum(
            line_by_id[lid]["weight"] for lid in primary_retained
        ),
        "retained_coverage_bonus_points": coverage_score(primary_retained),
        "worst_case_recovery_priority_weight": min(recovery_weights),
        "worst_case_recovery_coverage_bonus_points": min(recovery_bonuses),
        "total_recovery_coverage_bonus_points": sum(recovery_bonuses),
        "worst_case_recovery_final_reimbursable_cents": (
            per_diem_total + min(recovery_amounts)
        ),
        "recovery_reserve_line_ids": sorted(
            set().union(
                *(set(result["reactivated_line_ids"]) for result in recovery_results)
            )
        ),
        "retention_recovery_scenarios": recovery_results,
    }
    metrics["recovery_reserve_certification_units"] = sum(
        retention_recovery_reserve["category_certification_units"][
            line_by_id[line_id]["category"]
        ]
        for line_id in metrics["recovery_reserve_line_ids"]
    )
    certification_rows = []
    certification_score = 0
    reviewer_by_id = {
        reviewer["reviewer_id"]: reviewer
        for reviewer in reviewers
    }
    for line in order:
        lid = line["line_id"]
        for (reviewer_id, period), option in cert_x[lid].items():
            if not s.Value(option):
                continue
            units = retention_recovery_reserve[
                "category_certification_units"
            ][line["category"]]
            score = (
                reviewer_by_id[reviewer_id][
                    "score_points_by_period"
                ][period]
                + certification["category_score_adjustment"][
                    line["category"]
                ]
            )
            certification_score += score
            certification_rows.append(
                {
                    "line_id": lid,
                    "reviewer_id": reviewer_id,
                    "period": period,
                    "certification_units": units,
                    "score_points": score,
                }
            )
    metrics["recovery_reserve_certifications"] = sorted(
        certification_rows,
        key=lambda row: row["line_id"],
    )
    metrics["recovery_reserve_certification_score"] = (
        certification_score
    )
    assert certification_score == fixed["reserve_certification_score"]
    return known_line_ids - primary_retained, metrics


def main():
    policy = load_json("policy.json")
    itinerary = load_json("itinerary.json")
    per_diem_rates = load_json("per_diem_rates.json")
    vat = load_json("vat_reclaim_table.json")
    mrates = load_json("mileage_rates.json")
    cf = load_json("counterfactual_fares.json")["fare_cents"]
    rates = load_fx_rates()
    dist = load_distance_matrix()

    personal_days = {d["date"] for d in itinerary["days"] if d["is_personal_day"]}
    trip_has_personal = bool(personal_days)

    with (INPUT / "expense_lines.csv").open(newline="") as h:
        lines = list(csv.DictReader(h))

    # ---- phase 1: per-line computed amount + base disposition ----
    computed, base_status = {}, {}
    seen = set()
    for line in lines:
        lid = line["line_id"]
        if line["is_personal_expense"] == "TRUE":
            computed[lid], base_status[lid] = 0, "NON_REIMBURSABLE"
            continue
        # personal-day-meal narrow rule
        if line["category"] == "MEALS" and line["date"] in personal_days and not is_client_meal(line):
            computed[lid], base_status[lid] = 0, "NON_REIMBURSABLE"
            continue
        key = (line["vendor"], line["amount"], line["date"], line["category"])
        if key in seen:
            computed[lid], base_status[lid] = 0, "DUPLICATE_EXCLUDED"
            continue
        seen.add(key)
        if line["category"] == "MILEAGE":
            computed[lid] = process_mileage(line, policy, dist, mrates)
        else:
            computed[lid] = meal_or_air(line, policy, rates, vat, trip_has_personal, cf)
        base_status[lid] = "REIMBURSABLE"

    # ---- phase 2: per-diem with anti-double-dip ----
    per_diem_by_day = compute_per_diem_by_day(itinerary, per_diem_rates, Decimal(str(policy["per_diem_proration_fraction"])))
    meal_by_day = {}
    for line in lines:
        lid = line["line_id"]
        if line["category"] == "MEALS" and base_status[lid] == "REIMBURSABLE":
            meal_by_day[line["date"]] = meal_by_day.get(line["date"], 0) + computed[lid]
    total_per_diem = 0
    for date, pd in per_diem_by_day.items():
        total_per_diem += max(0, pd - meal_by_day.get(date, 0))

    # ---- phase 3: approval disposition (pre-deferral) ----
    reimb = [l for l in lines if base_status[l["line_id"]] == "REIMBURSABLE"]
    ordered = sorted(reimb, key=lambda l: (l["date"], l["line_id"]))
    running, needs_appr = {}, set()
    for line in ordered:
        cat = line["category"]; lid = line["line_id"]
        running[cat] = running.get(cat, 0) + computed[lid]
        if computed[lid] > policy["per_line_approval_threshold_cents"] or running[cat] > policy["category_aggregate_approval_threshold_cents"][cat]:
            needs_appr.add(lid)

    # ---- phase 4: trip-budget knapsack ----
    knap_lines = [
        {
            "line_id": line["line_id"],
            "date": line["date"],
            "category": line["category"],
            "statement_post_date": line["statement_post_date"],
            "amount": computed[line["line_id"]],
            "weight": 6 - int(line["priority"]),
        }
        for line in reimb
    ]
    deferred, retention_metrics = solve_budget_knapsack(
        knap_lines,
        total_per_diem,
        policy["trip_reimbursement_cap_cents"],
        policy.get("retention_portfolios", []),
        policy.get("retention_commitments", []),
        policy.get("retention_coverage_bonuses", []),
        policy.get("retention_recovery_scenarios", []),
        policy["retention_recovery_reserve"],
        policy["retention_recovery_certification_schedule"],
        policy["retention_tie_break"],
    )

    # ---- assemble output ----
    cats = ("MEALS", "AIRFARE", "MILEAGE")
    cat_break = {c: {"reimbursable_cents": 0, "flagged_for_approval_cents": 0} for c in cats}
    line_items = []
    total_flagged = 0
    for line in lines:
        lid = line["line_id"]
        if base_status[lid] in ("NON_REIMBURSABLE", "DUPLICATE_EXCLUDED"):
            line_items.append({"line_id": lid, "reimbursable_cents": 0, "status": base_status[lid]})
            continue
        if lid in deferred:
            line_items.append({"line_id": lid, "reimbursable_cents": 0, "status": "DEFERRED_OVER_BUDGET"})
            continue
        amt = computed[lid]
        flagged = lid in needs_appr
        status = "NEEDS_MANAGER_APPROVAL" if flagged else "APPROVED"
        line_items.append({"line_id": lid, "reimbursable_cents": amt, "status": status})
        cat_break[line["category"]]["reimbursable_cents"] += amt
        if flagged:
            cat_break[line["category"]]["flagged_for_approval_cents"] += amt
            total_flagged += amt

    line_total = sum(i["reimbursable_cents"] for i in line_items)
    gross = total_per_diem + sum(computed[l["line_id"]] for l in reimb)
    final = total_per_diem + line_total

    output = {
        "line_items": line_items,
        "trip_summary": {
            "total_per_diem_cents": total_per_diem,
            "gross_reimbursable_cents": gross,
            "trip_budget_cap_cents": policy["trip_reimbursement_cap_cents"],
            "final_reimbursable_cents": final,
            "deferred_over_budget_line_ids": sorted(deferred),
            "total_flagged_for_approval_cents": total_flagged,
            "category_breakdown": cat_break,
            **retention_metrics,
        },
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
