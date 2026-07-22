#!/usr/bin/env python3
"""Reference solver for the executive scheduling / inbox triage task.

Pipeline: expand recurring calendar events into concrete occurrences within
the planning window, determine the legal placements for each occurrence
under the scheduling_rules in policy_notes.json, search the (small) space of
placement choices for the occurrence set that optimizes objective_order, and
classify every inbox message with the deterministic reply_rule_order.
"""

from __future__ import annotations

import csv
import itertools
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dateutil.rrule import DAILY, FR, MO, SA, SU, TH, TU, WE, WEEKLY, rrule
from ortools.sat.python import cp_model

INPUT = Path("/app/input")
OUTPUT = Path("/app/output.json")
UTC = ZoneInfo("UTC")

WEEKDAY_MAP = {"MO": MO, "TU": TU, "WE": WE, "TH": TH, "FR": FR, "SA": SA, "SU": SU}
FREQ_MAP = {"DAILY": DAILY, "WEEKLY": WEEKLY}

DEFER = "DEFER"

# Components with an exhaustive choice-space at or below this size use the brute-force
# search (solve_group_bruteforce); larger ones use the CP-SAT search (solve_group_cpsat).
BRUTE_FORCE_LIMIT = 200_000


def load_json(path: Path) -> dict:
    return json.loads(path.read_text())


def load_travel_matrix() -> dict[str, dict[str, int]]:
    with (INPUT / "travel_matrix.csv").open(newline="") as handle:
        rows = list(csv.reader(handle))
    header = rows[0][1:]
    return {row[0]: {name: int(value) for name, value in zip(header, row[1:])} for row in rows[1:]}


def travel_between(matrix: dict[str, dict[str, int]], a: str, b: str) -> int:
    if a == b:
        return 0
    return matrix[a][b]


def expand_occurrences(event: dict, calendar: str, window_start: datetime, window_end: datetime) -> list[dict]:
    tz = ZoneInfo(event["timezone"])
    dtstart = datetime.fromisoformat(event["start_local"])
    exdates = set(event.get("exdates") or [])
    rule = event.get("rrule")
    if rule:
        byweekday = [WEEKDAY_MAP[d] for d in rule.get("byweekday", [])] or None
        kwargs = {}
        if rule.get("count") is not None:
            kwargs["count"] = rule["count"]
        if rule.get("until") is not None:
            kwargs["until"] = datetime.fromisoformat(rule["until"])
        raw_dates = list(rrule(FREQ_MAP[rule["freq"]], dtstart=dtstart, byweekday=byweekday, **kwargs))
    else:
        raw_dates = [dtstart]

    occurrences = []
    for dt in raw_dates:
        local_str = dt.strftime("%Y-%m-%dT%H:%M:%S")
        if local_str in exdates:
            continue
        start_utc = dt.replace(tzinfo=tz).astimezone(UTC)
        if not (window_start <= start_utc <= window_end):
            continue
        occurrences.append(
            {
                "occurrence_id": f'{event["event_id"]}@{local_str}',
                "event_id": event["event_id"],
                "calendar": calendar,
                "title": event["title"],
                "timezone": event["timezone"],
                "duration_minutes": event["duration_minutes"],
                "priority": event["priority"],
                "protected": bool(event.get("protected", False)),
                "vip_override": bool(event.get("vip_override", False)),
                "movable": bool(event.get("movable", False)),
                "after_hours_ok": bool(event.get("after_hours_ok", False)),
                "dependencies": list(event.get("dependencies") or []),
                "original_local": local_str,
                "original_venue": event["venue"],
                "candidate_slots": list(event.get("candidate_slots") or []),
            }
        )
    return occurrences


def build_placements(occ: dict, cutoff_local: str) -> list[dict]:
    """Return the legal (start_local, venue, start_utc, end_utc, rank) placements."""
    tz = ZoneInfo(occ["timezone"])
    cutoff_h, cutoff_m = (int(p) for p in cutoff_local.split(":"))

    original = {"start_local": occ["original_local"], "venue": occ["original_venue"]}
    raw_options = [original]
    seen = {(original["start_local"], original["venue"])}
    if occ["movable"]:
        for cand in occ["candidate_slots"]:
            key = (cand["start_local"], cand["venue"])
            if key not in seen:
                seen.add(key)
                raw_options.append(cand)

    placements = []
    for rank, opt in enumerate(raw_options):
        dt = datetime.fromisoformat(opt["start_local"])
        legal = occ["after_hours_ok"] or occ["vip_override"] or (dt.hour, dt.minute) < (cutoff_h, cutoff_m)
        if not legal:
            continue
        start_utc = dt.replace(tzinfo=tz).astimezone(UTC)
        placements.append(
            {
                "rank": rank,
                "start_local": opt["start_local"],
                "venue": opt["venue"],
                "start_utc": start_utc,
                "end_utc": start_utc + timedelta(minutes=occ["duration_minutes"]),
            }
        )
    return placements


def overlaps_or_too_close(a: dict, matrix: dict, b: dict) -> bool:
    """Would a and b conflict if they were the only two things on the calendar --
    i.e. either they overlap, or a direct A<->B transition wouldn't have enough travel
    buffer. Correct for clustering (find_components) and for hypothetical pairwise
    justification checks, where there is no third occurrence to route through. NOT
    correct as the sole feasibility check on an actual multi-occurrence schedule --
    see intervals_overlap / travel_gap_ok for that."""
    if a["start_utc"] < b["end_utc"] and b["start_utc"] < a["end_utc"]:
        return True
    if a["end_utc"] <= b["start_utc"]:
        gap = (b["start_utc"] - a["end_utc"]).total_seconds() / 60
        return gap < travel_between(matrix, a["venue"], b["venue"])
    gap = (a["start_utc"] - b["end_utc"]).total_seconds() / 60
    return gap < travel_between(matrix, b["venue"], a["venue"])


def intervals_overlap(a: dict, b: dict) -> bool:
    return a["start_utc"] < b["end_utc"] and b["start_utc"] < a["end_utc"]


def travel_gap_ok(a: dict, matrix: dict, b: dict) -> bool:
    """a ends at or before b starts. True if the gap between them covers the required travel."""
    gap = (b["start_utc"] - a["end_utc"]).total_seconds() / 60
    return gap >= travel_between(matrix, a["venue"], b["venue"])


def schedule_is_feasible(schedule_entries: list[dict], matrix: dict) -> bool:
    """The real feasibility check for a full set of scheduled entries: no two may ever
    overlap (checked for every pair), but the travel-buffer requirement only applies
    between occurrences that are chronologically adjacent in the merged schedule --
    if a third scheduled occurrence sits between two others, the executive routes
    through it, so a direct pairwise buffer check between the outer two would be a
    spurious, over-strict constraint."""
    if any(intervals_overlap(a, b) for a, b in itertools.combinations(schedule_entries, 2)):
        return False
    ordered = sorted(schedule_entries, key=lambda e: e["start_utc"])
    return all(travel_gap_ok(a, matrix, b) for a, b in zip(ordered, ordered[1:]))


def _entry(occ: dict, placement: dict) -> dict:
    return {**placement, "occ": occ}


def _original_utc(occ: dict) -> datetime:
    return datetime.fromisoformat(occ["original_local"]).replace(tzinfo=ZoneInfo(occ["timezone"])).astimezone(UTC)


def score_schedule(schedule: list[dict], matrix: dict) -> dict:
    """priority/lateness/moved/travel for one list of chosen schedule entries -- the single
    formula for objective_definitions in policy_notes.json, used both to rank candidates
    during search and to report the final objective_score."""
    priority = sum(e["occ"]["priority"] for e in schedule)
    moved = sum(1 for e in schedule if e["rank"] != 0)
    lateness = sum(max(0.0, (e["start_utc"] - _original_utc(e["occ"])).total_seconds() / 60) for e in schedule)
    ordered = sorted(schedule, key=lambda e: e["start_utc"])
    travel = sum(travel_between(matrix, a["venue"], b["venue"]) for a, b in zip(ordered, ordered[1:]))
    return {"priority": priority, "lateness": lateness, "moved": moved, "travel": travel}


def find_components(occurrences: list[dict], matrix: dict) -> list[list[dict]]:
    """Group occurrences that could ever conflict, or are dependency-linked."""
    parent = {occ["occurrence_id"]: occ["occurrence_id"] for occ in occurrences}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for a, b in itertools.combinations(occurrences, 2):
        if any(
            overlaps_or_too_close(_entry(a, pa), matrix, _entry(b, pb))
            for pa in a["placements"]
            for pb in b["placements"]
        ):
            union(a["occurrence_id"], b["occurrence_id"])

    by_event_id: dict[str, list[dict]] = {}
    for occ in occurrences:
        by_event_id.setdefault(occ["event_id"], []).append(occ)
    for occ in occurrences:
        for dep_id in occ["dependencies"]:
            for dep_occ in by_event_id.get(dep_id, []):
                union(occ["occurrence_id"], dep_occ["occurrence_id"])

    groups: dict[str, list[dict]] = {}
    for occ in occurrences:
        groups.setdefault(find(occ["occurrence_id"]), []).append(occ)
    return list(groups.values())


def solve_group(group: list[dict], matrix: dict) -> list[tuple[list[dict], dict[str, int]]]:
    """Dispatch to the exhaustive search for small components, or CP-SAT for components
    whose choice space is too large to brute force."""
    choice_space_size = 1
    for occ in group:
        choice_space_size *= max(1, len(occ["placements"]) + 1)
        if choice_space_size > BRUTE_FORCE_LIMIT:
            return solve_group_cpsat(group, matrix)
    return solve_group_bruteforce(group, matrix)


def solve_group_cpsat(group: list[dict], _matrix: dict) -> list[tuple[list[dict], dict[str, int]]]:
    """CP-SAT search for a component too large to brute force. Only handles components
    with no protected/vip_override occurrences and a single shared venue (so overlap
    alone determines feasibility -- no sequence-dependent travel-buffer modeling is
    needed). A future large component mixing those in would need this extended, not
    silently mishandled, hence the asserts."""
    assert not any(occ["protected"] or occ["vip_override"] for occ in group), (
        "solve_group_cpsat does not implement protected/vip_override precedence"
    )
    venues = {p["venue"] for occ in group for p in occ["placements"]}
    assert len(venues) <= 1, "solve_group_cpsat assumes a single shared venue (no travel-buffer modeling)"

    model = cp_model.CpModel()
    epoch = min(p["start_utc"] for occ in group for p in occ["placements"])

    def minutes(dt: datetime) -> int:
        return int((dt - epoch).total_seconds() // 60)

    choice_vars: dict[tuple[str, int], cp_model.IntVar] = {}
    intervals = []
    for occ in group:
        option_vars = []
        for p in occ["placements"]:
            var = model.NewBoolVar(f'{occ["occurrence_id"]}__{p["rank"]}')
            choice_vars[(occ["occurrence_id"], p["rank"])] = var
            option_vars.append(var)
            start, end = minutes(p["start_utc"]), minutes(p["end_utc"])
            intervals.append(model.NewOptionalIntervalVar(start, end - start, end, var, f'{occ["occurrence_id"]}_{p["rank"]}_iv'))
        model.Add(sum(option_vars) <= 1)
    model.AddNoOverlap(intervals)

    for occ in group:
        for dep_id in occ["dependencies"]:
            for dep_occ in (d for d in group if d["event_id"] == dep_id):
                for p in occ["placements"]:
                    for dp in dep_occ["placements"]:
                        if p["start_utc"] < dp["end_utc"]:
                            model.Add(
                                choice_vars[(occ["occurrence_id"], p["rank"])]
                                + choice_vars[(dep_occ["occurrence_id"], dp["rank"])]
                                <= 1
                            )

    priority_terms, lateness_terms, moved_terms = [], [], []
    for occ in group:
        orig_utc = _original_utc(occ)
        for p in occ["placements"]:
            var = choice_vars[(occ["occurrence_id"], p["rank"])]
            priority_terms.append(occ["priority"] * var)
            lateness_terms.append(max(0, int((p["start_utc"] - orig_utc).total_seconds() // 60)) * var)
            if p["rank"] != 0:
                moved_terms.append(var)

    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 8

    for terms, sense in ((priority_terms, "max"), (lateness_terms, "min"), (moved_terms, "min")):
        if sense == "max":
            model.Maximize(sum(terms))
        else:
            model.Minimize(sum(terms))
        status = solver.Solve(model)
        assert status in (cp_model.OPTIMAL, cp_model.FEASIBLE), "CP-SAT component has no feasible solution"
        target = int(solver.ObjectiveValue())
        model.Add(sum(terms) == target)

    status = solver.Solve(model)
    assert status in (cp_model.OPTIMAL, cp_model.FEASIBLE)

    schedule_entries, ranks, chosen_vars = [], {}, []
    for occ in group:
        chosen = next((p for p in occ["placements"] if solver.Value(choice_vars[(occ["occurrence_id"], p["rank"])])), None)
        if chosen is None:
            ranks[occ["occurrence_id"]] = len(occ["placements"])
        else:
            schedule_entries.append(_entry(occ, chosen))
            ranks[occ["occurrence_id"]] = chosen["rank"]
            chosen_vars.append(choice_vars[(occ["occurrence_id"], chosen["rank"])])

    # Uniqueness check: forbid this exact set of choices and confirm no other
    # (priority, lateness, moved)-optimal assignment exists (this task's reference
    # data is designed to have a unique optimum; a full lexicographic tie-break
    # among CP-SAT solutions is not implemented, so this must hold).
    model.Add(sum(chosen_vars) <= len(chosen_vars) - 1)
    status = solver.Solve(model)
    assert status == cp_model.INFEASIBLE, "CP-SAT component optimum is not unique -- tie-break not implemented"

    return [(schedule_entries, ranks)]


def solve_group_bruteforce(group: list[dict], matrix: dict) -> list[tuple[list[dict], dict[str, int]]]:
    """Every (schedule_entries, ranks) tied for locally-optimal (priority, lateness, moved) in one component."""
    choice_spaces = [
        [(p["rank"], p) for p in occ["placements"]] + [(None, DEFER)] if occ["placements"] else [(None, DEFER)]
        for occ in group
    ]

    best_key = None
    best_candidates: list[tuple[list[dict], dict[str, int]]] = []

    for combo in itertools.product(*choice_spaces):
        schedule_entries = []
        chosen_by_event = {}
        for occ, (_, placement) in zip(group, combo):
            entry = None if placement is DEFER else _entry(occ, placement)
            chosen_by_event[occ["event_id"]] = entry
            if entry is not None:
                schedule_entries.append(entry)

        if not schedule_is_feasible(schedule_entries, matrix):
            continue

        # Protected / VIP precedence: a deferred protected occurrence is only
        # legal if a scheduled vip_override occurrence conflicts with it.
        deferred_protected = (
            occ for occ, (_, placement) in zip(group, combo) if occ["protected"] and placement is DEFER and occ["placements"]
        )
        if any(
            not any(
                entry["occ"]["vip_override"] and overlaps_or_too_close(_entry(occ, occ["placements"][0]), matrix, entry)
                for entry in schedule_entries
            )
            for occ in deferred_protected
        ):
            continue

        # Dependency ordering: dependent must start no earlier than the
        # prerequisite's scheduled end, when both are scheduled.
        if any(
            chosen_by_event[dep_id] is not None and entry["start_utc"] < chosen_by_event[dep_id]["end_utc"]
            for occ in group
            if (entry := chosen_by_event[occ["event_id"]]) is not None
            for dep_id in occ["dependencies"]
        ):
            continue

        score = score_schedule(schedule_entries, matrix)
        key = (score["priority"], -score["lateness"], -score["moved"])
        if best_key is None or key > best_key:
            best_key = key
            best_candidates = []
        if key == best_key:
            ranks = {
                occ["occurrence_id"]: len(occ["placements"]) if placement is DEFER else rank
                for occ, (rank, placement) in zip(group, combo)
            }
            best_candidates.append((schedule_entries, ranks))

    assert best_candidates, "no feasible placement for a connected component"
    return best_candidates


def solve_schedule(occurrences: list[dict], matrix: dict, cutoff_local: str) -> dict:
    for occ in occurrences:
        occ["placements"] = build_placements(occ, cutoff_local)

    group_candidates = [solve_group(group, matrix) for group in find_components(occurrences, matrix)]

    best = None  # (key, schedule, score)
    for combo in itertools.product(*group_candidates):
        schedule = list(itertools.chain.from_iterable(entries for entries, _ in combo))
        ranks = {oid: rank for _, group_ranks in combo for oid, rank in group_ranks.items()}

        score = score_schedule(schedule, matrix)
        tie_key = tuple(-ranks[oid] for oid in sorted(ranks))
        key = (score["priority"], -score["lateness"], -score["moved"], -score["travel"], tie_key)
        if best is None or key > best[0]:
            best = (key, schedule, score)

    assert best is not None, "no feasible schedule found"
    _, schedule, score = best
    return {"schedule": schedule, **score}


def classify_messages(messages: list[dict], rule_order: list[str]) -> dict[str, str]:
    categories = {}
    for msg in messages:
        category = None
        for rule in rule_order:
            if rule == "missing_required_details" and msg["missing_required_details"]:
                category = "REQUEST_INFO"
            elif rule == "requires_escalation" and msg["requires_escalation"]:
                category = "ESCALATE"
            elif rule == "no_legal_alternative" and msg["no_legal_alternative"]:
                category = "DECLINE"
            elif rule == "conflict_with_alternate" and msg["conflict"] and msg["alternate_available"]:
                category = "PROPOSE_ALTERNATE"
            elif rule == "default_ack":
                category = "ACK"
            if category is not None:
                break
        categories[msg["message_id"]] = category
    return categories


def main() -> None:
    policy = load_json(INPUT / "policy_notes.json")
    matrix = load_travel_matrix()
    window_start = datetime.fromisoformat(policy["planning_window"]["start_utc"].replace("Z", "+00:00"))
    window_end = datetime.fromisoformat(policy["planning_window"]["end_utc"].replace("Z", "+00:00"))
    cutoff_local = policy["after_hours_cutoff_local"]

    occurrences = []
    for calendar_file in sorted((INPUT / "calendars").glob("*.json")):
        data = load_json(calendar_file)
        for event in data["events"]:
            occurrences.extend(expand_occurrences(event, data["calendar"], window_start, window_end))

    result = solve_schedule(occurrences, matrix, cutoff_local)
    scheduled_ids = {e["occ"]["occurrence_id"] for e in result["schedule"]}

    final_schedule = []
    moved_items = []
    for entry in sorted(result["schedule"], key=lambda e: e["start_utc"]):
        occ = entry["occ"]
        final_schedule.append(
            {
                "occurrence_id": occ["occurrence_id"],
                "event_id": occ["event_id"],
                "calendar": occ["calendar"],
                "venue": entry["venue"],
                "start_utc": entry["start_utc"].strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end_utc": entry["end_utc"].strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        )
        if entry["rank"] != 0:
            moved_items.append(occ["occurrence_id"])

    deferred_items = sorted(occ["occurrence_id"] for occ in occurrences if occ["occurrence_id"] not in scheduled_ids)

    inbox = load_json(INPUT / "inbox" / "messages.json")
    reply_categories = classify_messages(inbox["messages"], policy["reply_rule_order"])

    output = {
        "final_schedule": final_schedule,
        "moved_items": sorted(moved_items),
        "deferred_items": deferred_items,
        "reply_categories": reply_categories,
        "objective_score": {
            "priority_score": result["priority"],
            "lateness_minutes": int(result["lateness"]),
            "moved_count": len(moved_items),
            "travel_minutes": int(result["travel"]),
        },
    }

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
