#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import random
import shutil
from pathlib import Path


def segment_hits_box(a: list[int], b: list[int], box: tuple[int, int, int, int]) -> bool:
    xmin, ymin, xmax, ymax = box
    dx, dy = b[0] - a[0], b[1] - a[1]
    low, high = 0.0, 1.0
    for p, q in ((-dx, a[0] - xmin), (dx, xmax - a[0]), (-dy, a[1] - ymin), (dy, ymax - a[1])):
        if p == 0:
            if q < 0:
                return False
            continue
        ratio = q / p
        if p < 0:
            low = max(low, ratio)
        else:
            high = min(high, ratio)
        if low > high:
            return False
    return True


def overlap(a: tuple[int, int, int, int], b: tuple[int, int, int, int], margin: int = 0) -> bool:
    return not (
        a[2] + margin < b[0]
        or b[2] + margin < a[0]
        or a[3] + margin < b[1]
        or b[3] + margin < a[1]
    )


def rectangle(box: tuple[int, int, int, int]) -> list[list[int]]:
    xmin, ymin, xmax, ymax = box
    return [[xmin, ymin], [xmax, ymin], [xmax, ymax], [xmin, ymax]]


def build(root: Path) -> None:
    input_dir = root / "input"
    if input_dir.exists():
        shutil.rmtree(input_dir)
    input_dir.mkdir(parents=True)

    outer = [[0, 0], [12000, 0], [12000, 8000], [0, 8000]]
    wall_boxes = []
    route: list[list[int]] = [[200, 1200]]
    for index in range(1, 12):
        x = index * 1000
        if index % 2:
            box = (x - 90, 200, x + 90, 5600)
            passage_y = 6800
        else:
            box = (x - 90, 2400, x + 90, 7800)
            passage_y = 1200
        wall_boxes.append(box)
        route.extend([[x - 240, passage_y], [x + 240, passage_y]])
    route.append([11800, route[-1][1]])

    holes = [{"hole_id": f"WALL-{i:02d}", "vertices": rectangle(box)} for i, box in enumerate(wall_boxes, 1)]
    occupied = list(wall_boxes)
    rng = random.Random(884576)
    pillars: list[tuple[int, int, int, int]] = []
    attempts = 0
    while len(pillars) < 30 and attempts < 20000:
        attempts += 1
        width = rng.choice([70, 90, 110, 130])
        height = rng.choice([70, 100, 140])
        cx = rng.randrange(400, 11600, 20)
        cy = rng.randrange(400, 7600, 20)
        box = (cx - width, cy - height, cx + width, cy + height)
        expanded = (box[0] - 110, box[1] - 110, box[2] + 110, box[3] + 110)
        if any(overlap(box, other, 80) for other in occupied):
            continue
        if any(segment_hits_box(route[i], route[i + 1], expanded) for i in range(len(route) - 1)):
            continue
        pillars.append(box)
        occupied.append(box)
    if len(pillars) != 30:
        raise RuntimeError("failed to place deterministic pillars")
    holes.extend(
        {"hole_id": f"PILLAR-{i:02d}", "vertices": rectangle(box)}
        for i, box in enumerate(pillars, 1)
    )

    observers = []
    for index, position in enumerate(route):
        if index < len(route) - 1:
            direction = [route[index + 1][0] - position[0], route[index + 1][1] - position[1]]
        else:
            direction = [route[index - 1][0] - position[0], route[index - 1][1] - position[1]]
        halfplanes = [] if index % 3 == 0 else [{"a": direction[0], "b": direction[1]}]
        observers.append(
            {
                "observer_id": f"O{index:02d}",
                "position": position,
                "fov_halfplanes": halfplanes,
                "roles": ["primary"],
                "failure_domain": f"GRID-{index % 7}",
                "maximum_consecutive_cells": 6 + index % 4,
            }
        )

    # Every route observer has an independently powered, geometrically
    # co-located replica.  The identical visibility profile guarantees
    # two-observer coverage at the narrow route ends, while different fatigue
    # limits, failure domains, and transition arcs prevent a trivial pairing.
    route_observers = list(observers)
    for index, original in enumerate(route_observers):
        observers.append(
            {
                "observer_id": f"R{index:02d}",
                "position": original["position"],
                "fov_halfplanes": original["fov_halfplanes"],
                "roles": ["backup"],
                "failure_domain": f"GRID-{(index * 3 + 5) % 7}",
                "maximum_consecutive_cells": 7 + (index * 5) % 4,
            }
        )

    for index in range(8):
        route_index = 1 + index * 3
        base = route[route_index]
        offset = 320 if base[1] < 4000 else -320
        position = [base[0], base[1] + offset]
        center = [base[0] - position[0], base[1] - position[1]]
        rotated = [-center[1], center[0]]
        left = [center[0] + rotated[0], center[1] + rotated[1]]
        right = [center[0] - rotated[0], center[1] - rotated[1]]
        observers.append(
            {
                "observer_id": f"AUX{index:02d}",
                "position": position,
                "fov_halfplanes": [{"a": left[0], "b": left[1]}, {"a": right[0], "b": right[1]}],
                "roles": ["primary"] if index % 2 == 0 else ["backup"],
                "failure_domain": f"GRID-{(index * 2 + 3) % 7}",
                "maximum_consecutive_cells": 5 + index % 4,
            }
        )

    observer_ids = [observer["observer_id"] for observer in observers]
    transitions: dict[tuple[str, str], int] = {}

    def add(source: str, target: str, cost: int) -> None:
        if source != target:
            transitions[(source, target)] = min(cost, transitions.get((source, target), cost))

    for index in range(len(route) - 1):
        add(f"O{index:02d}", f"O{index + 1:02d}", 1 + (index * 7) % 11)
        add(f"O{index + 1:02d}", f"O{index:02d}", 2 + (index * 5) % 13)
        add(f"R{index:02d}", f"R{index + 1:02d}", 2 + (index * 11) % 13)
        add(f"R{index + 1:02d}", f"R{index:02d}", 3 + (index * 7) % 11)
    for i in range(len(route)):
        for jump in (2, 3, 5):
            j = i + jump
            if j < len(route):
                token = hashlib.sha256(f"{i}:{j}:884576".encode()).digest()
                if token[0] % 3:
                    add(f"O{i:02d}", f"O{j:02d}", 3 + token[1] % 15)
                if token[2] % 4 == 0:
                    add(f"O{j:02d}", f"O{i:02d}", 4 + token[3] % 17)
                if token[4] % 3:
                    add(f"R{i:02d}", f"R{j:02d}", 4 + token[5] % 16)
                if token[6] % 4 == 0:
                    add(f"R{j:02d}", f"R{i:02d}", 5 + token[7] % 18)
        for offset in (-2, -1, 0, 1, 2):
            j = i + offset
            if 0 <= j < len(route):
                add(f"O{i:02d}", f"R{j:02d}", 2 + (i * 7 + j * 3) % 15)
                add(f"R{i:02d}", f"O{j:02d}", 3 + (i * 5 + j * 11) % 14)
    for aux in range(8):
        anchor = 1 + aux * 3
        aid = f"AUX{aux:02d}"
        for offset in (-2, -1, 0, 1, 2, 3):
            route_index = anchor + offset
            if 0 <= route_index < len(route):
                oid = f"O{route_index:02d}"
                rid = f"R{route_index:02d}"
                add(oid, aid, 2 + (aux * 3 + route_index) % 12)
                add(aid, oid, 1 + (aux * 5 + route_index) % 14)
                add(rid, aid, 3 + (aux * 7 + route_index) % 11)
                add(aid, rid, 2 + (aux * 11 + route_index) % 13)
        if aux + 1 < 8:
            add(aid, f"AUX{aux + 1:02d}", 2 + (aux * 7) % 9)
            add(f"AUX{aux + 1:02d}", aid, 3 + (aux * 5) % 10)
    # A few long, inexpensive directed links make local "stay as long as possible"
    # choices diverge from the globally cheapest compatible sequence.
    for source, target, cost in [
        ("O01", "AUX02", 1),
        ("AUX02", "O11", 1),
        ("O07", "AUX05", 2),
        ("AUX05", "O19", 1),
        ("AUX00", "AUX06", 3),
    ]:
        add(source, target, cost)

    instance = {
        "format_version": 1,
        "route_parameter": {
            "domain": f"[0,{len(route) - 1}]",
            "definition": "for t=i+u with integer i and 0<=u<=1, R(t)=(1-u)*route[i]+u*route[i+1]",
            "rational_format": "reduced p/q with positive q, or p when q=1",
        },
        "visibility_rules": {
            "free_space": "inside or on the outer boundary, excluding every closed hole",
            "blocking": "a sight segment that intersects or touches any outer or hole boundary edge is blocked",
            "fov": "every listed a*(x-ox)+b*(y-oy)>=0 must hold; boundaries are included",
            "observer_at_robot": "visible when the common point is in free space",
        },
        "decomposition_rules": {
            "critical_times": "exactly the endpoints and times where singleton visibility differs from either adjacent open interval, or the two adjacent open-interval visible sets differ",
            "open_intervals": "the maximal open cells between consecutive critical times, including empty visible sets if any",
            "event_visibility": "visibility at each critical time itself",
        },
        "schedule_rules": {
            "segments": "non-empty closed ranges labelled by an ordered (primary,backup) pair; their union is the route domain and interiors do not overlap",
            "handoff_times": "critical times only",
            "closed_cells": "cell j is the closed range from critical_times[j] through critical_times[j+1]",
            "visibility": "both assigned observers must be visible at every point of every closed cell they cover",
            "separation": "primary and backup must have different observer IDs and different failure_domain values",
            "role_eligibility": "an observer may serve only a role listed in its roles array",
            "handoff": "adjacent ranges meet at one critical time; each changed role separately requires its directed transition and pays that cost",
            "same_pair": "adjacent equal ordered pairs must be merged",
            "observer_load": "the number of closed cells on which the observer serves in either role",
            "fatigue": "in every window of maximum_consecutive_cells+1 closed cells, an observer must be absent from at least one cell",
            "cell_pair_sequence": "one [primary,backup] pair per closed cell; compare sequences lexicographically by primary ID then backup ID at the first differing cell",
            "objective": [
                "minimum total directed transition cost across both roles",
                "minimum maximum observer_load",
                "minimum number of pair-handoff events",
                "minimum number of individual role changes",
                "lexicographically smallest cell_pair_sequence",
            ],
        },
        "outer_boundary": outer,
        "holes": holes,
        "route": route,
        "observers": observers,
        "transitions": [
            {"from": source, "to": target, "cost": cost}
            for (source, target), cost in sorted(transitions.items())
        ],
        "observer_ids": sorted(observer_ids),
    }
    (input_dir / "facility.json").write_text(json.dumps(instance, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("/app"))
    args = parser.parse_args()
    build(args.root)
