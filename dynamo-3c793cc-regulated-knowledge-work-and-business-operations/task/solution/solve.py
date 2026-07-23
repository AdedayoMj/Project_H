#!/usr/bin/env python3
"""Reference solver for the T&E expense reimbursement audit task.

Processes /app/input/expense_lines.csv against /app/input/policy.json and the
supporting rate tables, and reconstructs per-diem day by day from
/app/input/itinerary.json, to produce an exact, deterministic reimbursement
figure and compliance disposition for every expense line, plus trip- and
category-level totals. All money is carried as integer home-currency cents
throughout -- never floating-point currency -- and rounded at each specified
conversion/proration step using the policy's stated rounding mode, not once
at the end.

Per-line rule chain (MEALS / AIRFARE): VAT-reclaim subtraction (in the
transaction currency) -> two-hop dated FX conversion (transaction-currency ->
card-billing-currency using the transaction date, then card-billing-currency
-> home-currency using the statement-post date) -> category cap (blended
per-attendee cap for qualifying client meals, solo cap otherwise; lesser of
actual/counterfactual fare for airfare on any trip containing a personal day)
-> reimbursable foreign-transaction fee, converted through the same two hops
and capped, added on top.

MILEAGE lines skip FX/VAT entirely: reimbursable = distance (from the
directional distance matrix) x the rate in effect on the travel date, minus
the flat commute deduction, floored at zero.

Disposition precedence per line: NON_REIMBURSABLE (marked personal expense)
> DUPLICATE_EXCLUDED (exact vendor+amount+date+category match, first
occurrence kept) > NEEDS_MANAGER_APPROVAL (per-line cap or category
trip-aggregate cap tripped) > APPROVED.
"""

from __future__ import annotations

import csv
import json
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

INPUT = Path("/app/input")
OUTPUT = Path("/app/output.json")

CENT = Decimal("0.01")


def round_cents(amount: Decimal) -> int:
    """Round a Decimal currency amount to the nearest cent, half rounds up
    (the policy's stated rounding mode), returned as an integer count of
    cents."""
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def load_json(name: str) -> dict:
    return json.loads((INPUT / name).read_text())


def load_fx_rates() -> dict[tuple[str, str, str], Decimal]:
    rates = {}
    with (INPUT / "fx_rates.csv").open(newline="") as handle:
        for row in csv.DictReader(handle):
            key = (row["date"], row["from_currency"], row["to_currency"])
            rates[key] = Decimal(row["rate"])
    return rates


def fx_rate(rates: dict, date: str, from_ccy: str, to_ccy: str) -> Decimal:
    if from_ccy == to_ccy:
        return Decimal("1")
    return rates[(date, from_ccy, to_ccy)]


def load_distance_matrix() -> dict[tuple[str, str], Decimal]:
    matrix = {}
    with (INPUT / "distance_matrix.csv").open(newline="") as handle:
        for row in csv.DictReader(handle):
            matrix[(row["origin_city"], row["destination_city"])] = Decimal(row["miles"])
    return matrix


def mileage_rate_cents(rate_table: list[dict], date: str) -> int:
    for entry in rate_table:
        if entry["effective_start"] <= date <= entry["effective_end"]:
            return entry["rate_cents_per_mile"]
    raise ValueError(f"no mileage rate covers {date}")


def two_hop_convert(rates: dict, amount_txn_ccy: Decimal, txn_date: str, post_date: str, txn_ccy: str, card_ccy: str, home_ccy: str) -> int:
    """Convert an amount from transaction currency to home currency via the
    card-billing currency, rounding to cents after each hop."""
    hop1_rate = fx_rate(rates, txn_date, txn_ccy, card_ccy)
    after_hop1 = round_cents(amount_txn_ccy * hop1_rate) if txn_ccy != card_ccy else round_cents(amount_txn_ccy)
    hop2_rate = fx_rate(rates, post_date, card_ccy, home_ccy)
    after_hop2 = round_cents((Decimal(after_hop1) / 100) * hop2_rate) if card_ccy != home_ccy else after_hop1
    return after_hop2


def vat_reclaim_net(amount_txn_ccy: Decimal, country: str, category: str, vat_table: dict) -> Decimal:
    entry = vat_table.get(country)
    if entry and category in entry["eligible_categories"]:
        vat = (amount_txn_ccy * Decimal(str(entry["rate"]))).quantize(CENT, rounding=ROUND_HALF_UP)
        return amount_txn_ccy - vat
    return amount_txn_ccy


def compute_per_diem(itinerary: dict, per_diem_rates: dict, proration_fraction: Decimal) -> int:
    days = itinerary["days"]
    first_date = min(d["date"] for d in days)
    last_date = max(d["date"] for d in days)
    total = Decimal(0)
    for day in days:
        if day["is_personal_day"]:
            continue
        rate = Decimal(per_diem_rates[day["sleeping_city"]])
        if day["date"] in (first_date, last_date):
            rate = rate * proration_fraction
        total += rate
    return round_cents(total / 100)


def process_meal_or_airfare(
    line: dict,
    policy: dict,
    rates: dict,
    vat_table: dict,
    trip_has_personal_day: bool,
    counterfactual_fare_cents: int,
) -> tuple[int, int]:
    """Returns (capped_amount_cents, fee_reimbursement_cents), before
    duplicate/approval disposition."""
    home_ccy = policy["home_currency"]
    txn_ccy = line["transaction_currency"]
    card_ccy = line["card_billing_currency"]
    amount = Decimal(line["amount"])

    net_txn_ccy = vat_reclaim_net(amount, line["country"], line["category"], vat_table)
    home_cents = two_hop_convert(rates, net_txn_ccy, line["date"], line["statement_post_date"], txn_ccy, card_ccy, home_ccy)

    if line["category"] == "MEALS":
        attendees = [a for a in line["attendees"].split(";") if a]
        headcount = max(1, len(attendees))
        is_client_meal = line["payer"] == "EXECUTIVE" and any(a.split(":")[1] == "CLIENT" for a in attendees)
        # The per-attendee blended cap only rewards headcount when a client is present and the
        # executive is the payer of record; any other meal (solo or an internal group) is capped
        # at the flat solo amount regardless of how many people attended.
        cap = policy["client_meal_cap_cents_per_head"] * headcount if is_client_meal else policy["solo_meal_cap_cents"]
        capped = min(home_cents, cap)
    else:  # AIRFARE
        capped = min(home_cents, counterfactual_fare_cents) if trip_has_personal_day else home_cents

    fee_reimb = 0
    if txn_ccy != card_ccy and line["fee_free_alternative_existed"] != "TRUE":
        fee_amount = Decimal(line["foreign_fee_amount"])
        fee_home_cents = two_hop_convert(rates, fee_amount, line["date"], line["statement_post_date"], txn_ccy, card_ccy, home_ccy)
        fee_reimb = min(fee_home_cents, policy["max_foreign_fee_reimbursement_cents"])

    return capped, fee_reimb


def process_mileage(line: dict, policy: dict, distance_matrix: dict, mileage_rates: list[dict]) -> int:
    miles = distance_matrix[(line["origin_city"], line["destination_city"])]
    rate = mileage_rate_cents(mileage_rates, line["date"])
    gross = round_cents(miles * Decimal(rate) / 100)
    return max(0, gross - policy["normal_commute_deduction_cents"])


def main() -> None:
    policy = load_json("policy.json")
    itinerary = load_json("itinerary.json")
    per_diem_rates = load_json("per_diem_rates.json")
    vat_table = load_json("vat_reclaim_table.json")
    mileage_rates = load_json("mileage_rates.json")
    counterfactual = load_json("counterfactual_fares.json")
    rates = load_fx_rates()
    distance_matrix = load_distance_matrix()

    trip_has_personal_day = any(d["is_personal_day"] for d in itinerary["days"])

    with (INPUT / "expense_lines.csv").open(newline="") as handle:
        lines = list(csv.DictReader(handle))

    computed = {}  # line_id -> reimbursable_cents (pre-disposition)
    for line in lines:
        if line["is_personal_expense"] == "TRUE":
            computed[line["line_id"]] = 0
            continue
        if line["category"] == "MILEAGE":
            computed[line["line_id"]] = process_mileage(line, policy, distance_matrix, mileage_rates)
        else:
            capped, fee = process_meal_or_airfare(
                line, policy, rates, vat_table, trip_has_personal_day, counterfactual["fare_cents"]
            )
            computed[line["line_id"]] = capped + fee

    # Duplicate detection: exact match on (vendor, amount, date, category) among
    # non-personal-expense lines; first occurrence is kept.
    seen_keys = set()
    duplicate_ids = set()
    for line in lines:
        if line["is_personal_expense"] == "TRUE":
            continue
        key = (line["vendor"], line["amount"], line["date"], line["category"])
        if key in seen_keys:
            duplicate_ids.add(line["line_id"])
        else:
            seen_keys.add(key)

    # Category trip-aggregate threshold: a running cumulative total per category,
    # walked in date order (line_id ascending breaks ties on the same date) over
    # non-personal, non-duplicate lines. A line is aggregate-tripped once the
    # running total *through that line* exceeds the category's threshold --
    # earlier lines that kept the running total under the threshold are not
    # retroactively flagged.
    ordered = sorted(
        (line for line in lines if line["is_personal_expense"] != "TRUE" and line["line_id"] not in duplicate_ids),
        key=lambda line: (line["date"], line["line_id"]),
    )
    running_totals: dict[str, int] = {}
    aggregate_tripped_ids: set[str] = set()
    for line in ordered:
        cat = line["category"]
        running_totals[cat] = running_totals.get(cat, 0) + computed[line["line_id"]]
        if running_totals[cat] > policy["category_aggregate_approval_threshold_cents"][cat]:
            aggregate_tripped_ids.add(line["line_id"])

    line_items = []
    category_breakdown: dict[str, dict[str, int]] = {
        cat: {"reimbursable_cents": 0, "flagged_for_approval_cents": 0} for cat in ("MEALS", "AIRFARE", "MILEAGE")
    }
    total_flagged = 0

    for line in lines:
        lid = line["line_id"]
        amount_cents = computed[lid]
        if line["is_personal_expense"] == "TRUE":
            status = "NON_REIMBURSABLE"
            reimbursable = 0
        elif lid in duplicate_ids:
            status = "DUPLICATE_EXCLUDED"
            reimbursable = 0
        else:
            needs_approval = amount_cents > policy["per_line_approval_threshold_cents"] or lid in aggregate_tripped_ids
            status = "NEEDS_MANAGER_APPROVAL" if needs_approval else "APPROVED"
            reimbursable = amount_cents
            category_breakdown[line["category"]]["reimbursable_cents"] += reimbursable
            if needs_approval:
                category_breakdown[line["category"]]["flagged_for_approval_cents"] += reimbursable
                total_flagged += reimbursable

        line_items.append({"line_id": lid, "reimbursable_cents": reimbursable, "status": status})

    total_per_diem_cents = compute_per_diem(itinerary, per_diem_rates, Decimal(str(policy["per_diem_proration_fraction"])))
    total_reimbursable = sum(item["reimbursable_cents"] for item in line_items) + total_per_diem_cents

    output = {
        "line_items": sorted(line_items, key=lambda x: x["line_id"]),
        "trip_summary": {
            "total_reimbursable_cents": total_reimbursable,
            "total_flagged_for_approval_cents": total_flagged,
            "total_per_diem_cents": total_per_diem_cents,
            "category_breakdown": category_breakdown,
        },
    }

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
