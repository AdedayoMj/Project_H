#!/usr/bin/env python3
from __future__ import annotations

import bisect
import json
import os
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path


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
    # state value: (cost, handoffs, compressed_sequence, slab_assignments)
    states: dict[str, tuple[int, int, tuple[str, ...], tuple[str, ...]]] = {
        observer_id: (0, 0, (observer_id,), (observer_id,))
        for observer_id in availability[0]
    }
    for choices in availability[1:]:
        next_states: dict[str, tuple[int, int, tuple[str, ...], tuple[str, ...]]] = {}
        for current in choices:
            best = None
            for previous, state in states.items():
                cost, handoffs, sequence, assignments = state
                if previous == current:
                    candidate = (cost, handoffs, sequence, assignments + (current,))
                elif (previous, current) in transition_cost:
                    candidate = (
                        cost + transition_cost[(previous, current)],
                        handoffs + 1,
                        sequence + (current,),
                        assignments + (current,),
                    )
                else:
                    continue
                if best is None or candidate[:3] < best[:3]:
                    best = candidate
            if best is not None:
                next_states[current] = best
        if not next_states:
            raise RuntimeError("generated instance lacks transition-compatible coverage")
        states = next_states
    optimum = min(states.values(), key=lambda state: state[:3])
    total_cost, handoffs, sequence, assignments = optimum

    schedule = []
    start_slab = 0
    for index in range(1, len(assignments) + 1):
        if index == len(assignments) or assignments[index] != assignments[start_slab]:
            schedule.append(
                {
                    "observer": assignments[start_slab],
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
            "transition_cost": total_cost,
            "handoffs": handoffs,
            "observer_sequence": list(sequence),
        },
    }
    OUTPUT.write_text(json.dumps(output, indent=2) + "\n")


if __name__ == "__main__":
    main()
