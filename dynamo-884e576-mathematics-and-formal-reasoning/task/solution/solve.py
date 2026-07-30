#!/usr/bin/env python3
from __future__ import annotations

import bisect
import json
import os
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from ortools.sat.python import cp_model


APP_ROOT = Path(os.environ.get("GEOM_APP_ROOT", "/app"))
INPUT = APP_ROOT / "input" / "facility.json"
OUTPUT = APP_ROOT / "output.json"
Point = tuple[int, int]
HomPoint = tuple[int, int, int]


def cross(ax: int, ay: int, bx: int, by: int) -> int:
    return ax * by - ay * bx


def rational(value: Fraction) -> str:
    if value.denominator == 1:
        return str(value.numerator)
    return f"{value.numerator}/{value.denominator}"


def route_point(route: list[Point], time: Fraction) -> HomPoint:
    final = len(route) - 1
    if time == final:
        return route[-1][0], route[-1][1], 1
    index = time.numerator // time.denominator
    local = time - index
    numerator, denominator = local.numerator, local.denominator
    a, b = route[index], route[index + 1]
    return (
        (denominator - numerator) * a[0] + numerator * b[0],
        (denominator - numerator) * a[1] + numerator * b[1],
        denominator,
    )


def between(value: int, left: int, right: int) -> bool:
    return min(left, right) <= value <= max(left, right)


def sight_intersects_edge(observer: Point, robot: HomPoint, edge: tuple[Point, Point]) -> bool:
    ox, oy = observer
    rx, ry, den = robot
    c, d = edge
    sight_x, sight_y = rx - ox * den, ry - oy * den
    o1 = cross(sight_x, sight_y, c[0] - ox, c[1] - oy)
    o2 = cross(sight_x, sight_y, d[0] - ox, d[1] - oy)
    edge_x, edge_y = d[0] - c[0], d[1] - c[1]
    o3 = cross(edge_x, edge_y, ox - c[0], oy - c[1])
    o4 = cross(edge_x, edge_y, rx - c[0] * den, ry - c[1] * den)
    if ((o1 > 0 and o2 < 0) or (o1 < 0 and o2 > 0)) and (
        (o3 > 0 and o4 < 0) or (o3 < 0 and o4 > 0)
    ):
        return True
    if o1 == 0 and between(c[0] * den, ox * den, rx) and between(c[1] * den, oy * den, ry):
        return True
    if o2 == 0 and between(d[0] * den, ox * den, rx) and between(d[1] * den, oy * den, ry):
        return True
    if o3 == 0 and between(ox, c[0], d[0]) and between(oy, c[1], d[1]):
        return True
    if o4 == 0 and between(rx, c[0] * den, d[0] * den) and between(ry, c[1] * den, d[1] * den):
        return True
    return False


def visible(
    observer: Point,
    halfplanes: list[tuple[int, int]],
    robot: HomPoint,
    edges: list[tuple[Point, Point]],
) -> bool:
    rx, ry, den = robot
    dx, dy = rx - observer[0] * den, ry - observer[1] * den
    if any(a * dx + b * dy < 0 for a, b in halfplanes):
        return False
    if dx == 0 and dy == 0:
        return True
    return not any(sight_intersects_edge(observer, robot, edge) for edge in edges)


@dataclass
class Profile:
    candidates: list[Fraction]
    point_values: list[bool]
    interval_values: list[bool]
    retained: list[Fraction]

    def at(self, time: Fraction) -> bool:
        index = bisect.bisect_left(self.candidates, time)
        if index < len(self.candidates) and self.candidates[index] == time:
            return self.point_values[index]
        if index == 0 or index == len(self.candidates):
            raise ValueError("time outside route")
        return self.interval_values[index - 1]


def observer_profile(
    route: list[Point],
    boundary_vertices: list[Point],
    edges: list[tuple[Point, Point]],
    observer: Point,
    halfplanes: list[tuple[int, int]],
) -> Profile:
    count = len(route) - 1
    candidates = {Fraction(index) for index in range(count + 1)}
    for index, (start, end) in enumerate(zip(route, route[1:])):
        dx, dy = end[0] - start[0], end[1] - start[1]
        for vertex in boundary_vertices:
            vx, vy = vertex[0] - observer[0], vertex[1] - observer[1]
            constant = cross(vx, vy, start[0] - observer[0], start[1] - observer[1])
            slope = cross(vx, vy, dx, dy)
            if slope:
                local = Fraction(-constant, slope)
                if 0 <= local <= 1:
                    candidates.add(index + local)
        for a, b in halfplanes:
            constant = a * (start[0] - observer[0]) + b * (start[1] - observer[1])
            slope = a * dx + b * dy
            if slope:
                local = Fraction(-constant, slope)
                if 0 <= local <= 1:
                    candidates.add(index + local)
    ordered = sorted(candidates)
    point_values = [visible(observer, halfplanes, route_point(route, time), edges) for time in ordered]
    interval_values = [
        visible(observer, halfplanes, route_point(route, (left + right) / 2), edges)
        for left, right in zip(ordered, ordered[1:])
    ]
    retained = [ordered[0]]
    for index in range(1, len(ordered) - 1):
        point = point_values[index]
        if point != interval_values[index - 1] or point != interval_values[index]:
            retained.append(ordered[index])
    retained.append(ordered[-1])
    return Profile(ordered, point_values, interval_values, retained)


def main() -> None:
    data = json.loads(INPUT.read_text())
    route = [tuple(point) for point in data["route"]]
    polygons = [[tuple(point) for point in data["outer_boundary"]]]
    polygons.extend([[tuple(point) for point in hole["vertices"]] for hole in data["holes"]])
    edges: list[tuple[Point, Point]] = []
    boundary_vertices: list[Point] = []
    for polygon in polygons:
        boundary_vertices.extend(polygon)
        edges.extend((polygon[index], polygon[(index + 1) % len(polygon)]) for index in range(len(polygon)))

    observer_rows = {row["observer_id"]: row for row in data["observers"]}
    observers = {
        row["observer_id"]: (
            tuple(row["position"]),
            [(halfplane["a"], halfplane["b"]) for halfplane in row["fov_halfplanes"]],
        )
        for row in data["observers"]
    }
    observer_ids = sorted(observers)
    profiles = {
        observer_id: observer_profile(route, boundary_vertices, edges, *observers[observer_id])
        for observer_id in observer_ids
    }
    critical = sorted({time for profile in profiles.values() for time in profile.retained})

    event_sets = [
        [observer_id for observer_id in observer_ids if profiles[observer_id].at(time)]
        for time in critical
    ]
    open_sets = []
    for left, right in zip(critical, critical[1:]):
        midpoint = (left + right) / 2
        open_sets.append(
            [observer_id for observer_id in observer_ids if profiles[observer_id].at(midpoint)]
        )

    # Individual changes union to the exact global definition. Defensive removal
    # also proves no irrelevant time survives a simultaneous event.
    keep = [0]
    for index in range(1, len(critical) - 1):
        if (
            event_sets[index] != open_sets[index - 1]
            or event_sets[index] != open_sets[index]
            or open_sets[index - 1] != open_sets[index]
        ):
            keep.append(index)
    keep.append(len(critical) - 1)
    if len(keep) != len(critical):
        critical = [critical[index] for index in keep]
        event_sets = [
            [observer_id for observer_id in observer_ids if profiles[observer_id].at(time)]
            for time in critical
        ]
        open_sets = []
        for left, right in zip(critical, critical[1:]):
            midpoint = (left + right) / 2
            open_sets.append(
                [observer_id for observer_id in observer_ids if profiles[observer_id].at(midpoint)]
            )

    availability = []
    for index, open_visible in enumerate(open_sets):
        availability.append(
            sorted(set(open_visible) & set(event_sets[index]) & set(event_sets[index + 1]))
        )
    if any(not choices for choices in availability):
        raise RuntimeError("generated instance lacks closed-cell coverage")

    transition_cost = {
        (row["from"], row["to"]): row["cost"]
        for row in data["transitions"]
    }
    observer_index = {observer_id: index for index, observer_id in enumerate(observer_ids)}
    observer_count = len(observer_ids)
    pair_choices: list[list[tuple[int, int, int, str, str]]] = []
    for choices in availability:
        pairs = []
        for primary in choices:
            if "primary" not in observer_rows[primary]["roles"]:
                continue
            for backup in choices:
                if "backup" not in observer_rows[backup]["roles"]:
                    continue
                if primary == backup:
                    continue
                if (
                    observer_rows[primary]["failure_domain"]
                    == observer_rows[backup]["failure_domain"]
                ):
                    continue
                primary_index = observer_index[primary]
                backup_index = observer_index[backup]
                code = primary_index * observer_count + backup_index
                pairs.append((code, primary_index, backup_index, primary, backup))
        pairs.sort(key=lambda row: (row[3], row[4]))
        if not pairs:
            raise RuntimeError("generated instance lacks separated pair coverage")
        pair_choices.append(pairs)

    model = cp_model.CpModel()
    pair_states = [
        model.NewIntVarFromDomain(
            cp_model.Domain.FromValues([code for code, _, _, _, _ in choices]),
            f"pair_{cell}",
        )
        for cell, choices in enumerate(pair_choices)
    ]
    primary_states = [
        model.NewIntVarFromDomain(
            cp_model.Domain.FromValues(sorted({primary for _, primary, _, _, _ in choices})),
            f"primary_{cell}",
        )
        for cell, choices in enumerate(pair_choices)
    ]
    backup_states = [
        model.NewIntVarFromDomain(
            cp_model.Domain.FromValues(sorted({backup for _, _, backup, _, _ in choices})),
            f"backup_{cell}",
        )
        for cell, choices in enumerate(pair_choices)
    ]
    selected: list[dict[int, cp_model.IntVar]] = []
    for cell, choices in enumerate(pair_choices):
        literals = {}
        allowed_pairs = []
        for code, primary_index, backup_index, primary, backup in choices:
            literal = model.NewBoolVar(f"cell_{cell}__{primary}__{backup}")
            model.Add(pair_states[cell] == code).OnlyEnforceIf(literal)
            model.Add(pair_states[cell] != code).OnlyEnforceIf(literal.Not())
            literals[code] = literal
            allowed_pairs.append((primary_index, backup_index))
        model.AddExactlyOne(literals.values())
        model.AddAllowedAssignments(
            [primary_states[cell], backup_states[cell]],
            allowed_pairs,
        )
        model.Add(
            pair_states[cell]
            == primary_states[cell] * observer_count + backup_states[cell]
        )
        selected.append(literals)

    membership: dict[str, list[cp_model.LinearExpr]] = {
        observer_id: [] for observer_id in observer_ids
    }
    for cell, choices in enumerate(pair_choices):
        by_observer = {observer_id: [] for observer_id in observer_ids}
        for code, _, _, primary, backup in choices:
            by_observer[primary].append(selected[cell][code])
            by_observer[backup].append(selected[cell][code])
        for observer_id in observer_ids:
            membership[observer_id].append(sum(by_observer[observer_id]))

    maximum_load = model.NewIntVar(0, len(availability), "maximum_observer_load")
    for observer_id in observer_ids:
        load = sum(membership[observer_id])
        model.Add(maximum_load >= load)
        limit = observer_rows[observer_id]["maximum_consecutive_cells"]
        for start in range(0, len(availability) - limit):
            model.Add(sum(membership[observer_id][start : start + limit + 1]) <= limit)

    transition_cost_vars = []
    handoff_event_vars = []
    role_change_vars = []
    for boundary in range(1, len(availability)):
        role_costs = []
        role_cost_bounds = []
        role_changes_at_boundary = []
        for role, previous_states, current_states in (
            ("primary", primary_states, primary_states),
            ("backup", backup_states, backup_states),
        ):
            previous_values = sorted(
                {
                    row[1 if role == "primary" else 2]
                    for row in pair_choices[boundary - 1]
                }
            )
            current_values = sorted(
                {
                    row[1 if role == "primary" else 2]
                    for row in pair_choices[boundary]
                }
            )
            allowed = []
            for previous_index in previous_values:
                previous = observer_ids[previous_index]
                for current_index in current_values:
                    current = observer_ids[current_index]
                    if previous == current:
                        allowed.append((previous_index, current_index, 0, 0))
                    elif (previous, current) in transition_cost:
                        allowed.append(
                            (
                                previous_index,
                                current_index,
                                transition_cost[(previous, current)],
                                1,
                            )
                        )
            if not allowed:
                raise RuntimeError(
                    f"generated instance lacks {role} transition-compatible coverage"
                )
            cost_bound = max(row[2] for row in allowed)
            cost_var = model.NewIntVar(0, cost_bound, f"{role}_cost_{boundary}")
            change_var = model.NewBoolVar(f"{role}_change_{boundary}")
            model.AddAllowedAssignments(
                [
                    previous_states[boundary - 1],
                    current_states[boundary],
                    cost_var,
                    change_var,
                ],
                allowed,
            )
            role_costs.append(cost_var)
            role_cost_bounds.append(cost_bound)
            role_changes_at_boundary.append(change_var)

        cost_var = model.NewIntVar(
            0,
            sum(role_cost_bounds),
            f"cost_{boundary}",
        )
        model.Add(cost_var == sum(role_costs))
        event_var = model.NewBoolVar(f"event_{boundary}")
        changes_var = model.NewIntVar(0, 2, f"role_changes_{boundary}")
        model.AddMaxEquality(event_var, role_changes_at_boundary)
        model.Add(changes_var == sum(role_changes_at_boundary))
        transition_cost_vars.append(cost_var)
        handoff_event_vars.append(event_var)
        role_change_vars.append(changes_var)

    total_cost = sum(transition_cost_vars)
    handoff_events = sum(handoff_event_vars)
    role_changes = sum(role_change_vars)
    maximum_handoff_events = len(availability) - 1
    maximum_role_changes = 2 * maximum_handoff_events
    event_weight = maximum_role_changes + 1
    load_weight = (maximum_handoff_events + 1) * event_weight
    cost_weight = (len(availability) + 1) * load_weight
    combined_objective = (
        total_cost * cost_weight
        + maximum_load * load_weight
        + handoff_events * event_weight
        + role_changes
    )
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 4
    solver.parameters.random_seed = 884576
    model.Minimize(combined_objective)
    validation_error = model.Validate()
    if validation_error:
        raise RuntimeError(f"invalid CP-SAT model: {validation_error}")
    assert solver.Solve(model) == cp_model.OPTIMAL
    objective_values = [
        int(solver.Value(total_cost)),
        int(solver.Value(maximum_load)),
        int(solver.Value(handoff_events)),
        int(solver.Value(role_changes)),
    ]
    for objective, optimum in zip(
        (total_cost, maximum_load, handoff_events, role_changes),
        objective_values,
    ):
        model.Add(objective == optimum)

    for cell, state in enumerate(pair_states):
        minimum_code = pair_choices[cell][0][0]
        if solver.Value(state) == minimum_code:
            model.Add(state == minimum_code)
        else:
            model.ClearHints()
            for hinted_state in pair_states:
                model.AddHint(hinted_state, solver.Value(hinted_state))
            model.Minimize(state)
            assert solver.Solve(model) == cp_model.OPTIMAL
            model.Add(state == solver.Value(state))

    code_to_pair = {
        code: (primary, backup)
        for choices in pair_choices
        for code, _, _, primary, backup in choices
    }
    assignments = [code_to_pair[solver.Value(state)] for state in pair_states]

    schedule = []
    start_slab = 0
    for index in range(1, len(assignments) + 1):
        if index == len(assignments) or assignments[index] != assignments[start_slab]:
            schedule.append(
                {
                    "primary": assignments[start_slab][0],
                    "backup": assignments[start_slab][1],
                    "start": rational(critical[start_slab]),
                    "end": rational(critical[index]),
                }
            )
            start_slab = index

    output = {
        "critical_times": [rational(time) for time in critical],
        "open_intervals": [
            {"start": rational(left), "end": rational(right), "visible": visible_ids}
            for left, right, visible_ids in zip(critical, critical[1:], open_sets)
        ],
        "event_visibility": [
            {"time": rational(time), "visible": visible_ids}
            for time, visible_ids in zip(critical, event_sets)
        ],
        "schedule": schedule,
        "objective": {
            "maximum_observer_load": objective_values[1],
            "transition_cost": objective_values[0],
            "handoff_events": objective_values[2],
            "role_changes": objective_values[3],
            "cell_pair_sequence": [list(pair) for pair in assignments],
        },
    }
    OUTPUT.write_text(json.dumps(output, indent=2) + "\n")


if __name__ == "__main__":
    main()
