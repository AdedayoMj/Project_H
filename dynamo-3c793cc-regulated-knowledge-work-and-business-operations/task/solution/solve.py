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
  4. Trip-budget optimization: if per-diem + all reimbursable line amounts
     exceeds the trip cap, defer a subset of lines (0/1 knapsack) maximizing,
     lexicographically, retained priority-weight, then retained amount, then
     the line-id-lexicographic retention vector (a total order -> unique).
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


def solve_budget_knapsack(reimb_lines, per_diem_total, cap):
    """reimb_lines: list of dicts {line_id, date, amount, weight}. Choose a
    RETAINED subset maximizing (sum weight, then sum amount, then line-id
    lexicographic retention) s.t. per_diem_total + sum retained amount <= cap.
    Returns set of DEFERRED line_ids (empty if everything fits)."""
    budget = cap - per_diem_total
    if sum(l["amount"] for l in reimb_lines) <= budget:
        return set()
    # order by (date, line_id) so the lexicographic tie-break is well-defined
    order = sorted(reimb_lines, key=lambda l: (l["date"], l["line_id"]))

    def solve(fix_weight=None, fix_amount=None, forced=None):
        m = cp_model.CpModel()
        x = {l["line_id"]: m.NewBoolVar(l["line_id"]) for l in order}  # 1 = retained
        m.Add(sum(x[l["line_id"]] * l["amount"] for l in order) <= budget)
        W = sum(x[l["line_id"]] * l["weight"] for l in order)
        A = sum(x[l["line_id"]] * l["amount"] for l in order)
        if fix_weight is not None:
            m.Add(W == fix_weight)
        if fix_amount is not None:
            m.Add(A == fix_amount)
        for lid, val in (forced or {}).items():
            m.Add(x[lid] == val)
        return m, x, W, A

    s = cp_model.CpSolver()
    s.parameters.num_search_workers = 8

    m, x, W, A = solve()
    m.Maximize(W)
    assert s.Solve(m) in (cp_model.OPTIMAL, cp_model.FEASIBLE)
    best_w = int(s.ObjectiveValue())

    m, x, W, A = solve(fix_weight=best_w)
    m.Maximize(A)
    assert s.Solve(m) in (cp_model.OPTIMAL, cp_model.FEASIBLE)
    best_a = int(s.ObjectiveValue())

    # Iterative lexicographic tie-break: retain earlier line_ids preferentially.
    forced = {}
    for l in order:
        trial = dict(forced); trial[l["line_id"]] = 1
        m, x, W, A = solve(fix_weight=best_w, fix_amount=best_a, forced=trial)
        forced[l["line_id"]] = 1 if s.Solve(m) in (cp_model.OPTIMAL, cp_model.FEASIBLE) else 0
    return {lid for lid, v in forced.items() if v == 0}


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
    knap_lines = [{"line_id": l["line_id"], "date": l["date"], "amount": computed[l["line_id"]],
                   "weight": 6 - int(l["priority"])} for l in reimb]
    deferred = solve_budget_knapsack(knap_lines, total_per_diem, policy["trip_reimbursement_cap_cents"])

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
        "line_items": sorted(line_items, key=lambda x: x["line_id"]),
        "trip_summary": {
            "total_per_diem_cents": total_per_diem,
            "gross_reimbursable_cents": gross,
            "trip_budget_cap_cents": policy["trip_reimbursement_cap_cents"],
            "final_reimbursable_cents": final,
            "deferred_over_budget_line_ids": sorted(deferred),
            "total_flagged_for_approval_cents": total_flagged,
            "category_breakdown": cat_break,
        },
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
