#!/usr/bin/env python3
"""Prove a move-optimal solution for every published Sokoban instance."""
from __future__ import annotations

import heapq
import json
import os
from collections import deque
from pathlib import Path

APP = Path(os.environ.get("SOKOBAN_APP_ROOT", "/app"))
INPUT = APP / "input"
OUTPUT = APP / "output"

DIRECTIONS = {"U": (-1, 0), "D": (1, 0), "L": (0, -1), "R": (0, 1)}
ORDER = ("D", "L", "R", "U")
OPPOSITE = {"U": "D", "D": "U", "L": "R", "R": "L"}
COUNT_MODULUS = 1_000_000_007
INFINITY = float("inf")


class Puzzle:
    def __init__(self, text: str) -> None:
        rows = text.split("\n")
        while rows and not rows[-1].strip():
            rows.pop()
        self.height = len(rows)
        self.width = max(len(row) for row in rows)
        self.walls: set[int] = set()
        self.goals: set[int] = set()
        boxes: set[int] = set()
        self.player = -1
        for r, row in enumerate(rows):
            padded = row.ljust(self.width)
            for c, glyph in enumerate(padded):
                cell = r * self.width + c
                if glyph == "#":
                    self.walls.add(cell)
                    continue
                if glyph in (".", "*", "+"):
                    self.goals.add(cell)
                if glyph in ("$", "*"):
                    boxes.add(cell)
                if glyph in ("@", "+"):
                    self.player = cell
        self.boxes = frozenset(boxes)
        self.neighbour = {
            key: {
                cell: cell + dr * self.width + dc
                for cell in range(self.height * self.width)
                if 0 <= cell % self.width + dc < self.width
                and 0 <= cell // self.width + dr < self.height
            }
            for key, (dr, dc) in DIRECTIONS.items()
        }
        self.push_distance = self._push_distances()
        self.dead = {
            cell
            for cell in range(self.height * self.width)
            if cell not in self.walls
            and all(table.get(cell, INFINITY) == INFINITY for table in self.push_distance)
        }

    def step(self, cell: int, key: str) -> int | None:
        return self.neighbour[key].get(cell)

    def _push_distances(self) -> list[dict[int, float]]:
        """Pushes required to bring a lone box from each cell to each goal."""
        tables = []
        for goal in sorted(self.goals):
            distance = {goal: 0.0}
            queue = deque([goal])
            while queue:
                cell = queue.popleft()
                for key in ORDER:
                    origin = self.step(cell, key)
                    if origin is None or origin in self.walls or origin in distance:
                        continue
                    stand = self.step(origin, key)
                    if stand is None or stand in self.walls:
                        continue
                    distance[origin] = distance[cell] + 1
                    queue.append(origin)
            tables.append(distance)
        return tables


def routes(
    puzzle: Puzzle, boxes: frozenset[int], start: int
) -> dict[int, tuple[str, int]]:
    """Canonical shortest walk and number of shortest walks to each cell."""
    result = {start: ("", 1)}
    distances = {start: 0}
    queue = deque([start])
    while queue:
        cell = queue.popleft()
        for key in ORDER:
            nxt = puzzle.step(cell, key)
            if nxt is None or nxt in puzzle.walls or nxt in boxes:
                continue
            distance = distances[cell] + 1
            candidate = result[cell][0] + key
            if nxt not in distances:
                distances[nxt] = distance
                result[nxt] = (candidate, result[cell][1])
                queue.append(nxt)
                continue
            if distances[nxt] != distance:
                continue
            canonical, count = result[nxt]
            result[nxt] = (
                min(canonical, candidate),
                (count + result[cell][1]) % COUNT_MODULUS,
            )
    return result


def blocked_square(puzzle: Puzzle, boxes: frozenset[int], cell: int) -> bool:
    """A 2x2 block of walls and boxes can never move; fatal if it holds a stray box."""
    r, c = divmod(cell, puzzle.width)
    for top in (r - 1, r):
        for left in (c - 1, c):
            square = []
            valid = True
            for rr in (top, top + 1):
                for cc in (left, left + 1):
                    if not (0 <= rr < puzzle.height and 0 <= cc < puzzle.width):
                        valid = False
                        break
                    square.append(rr * puzzle.width + cc)
                if not valid:
                    break
            if not valid:
                continue
            if any(item not in puzzle.walls and item not in boxes for item in square):
                continue
            if any(item in boxes and item not in puzzle.goals for item in square):
                return True
    return False


def lower_bound(puzzle: Puzzle, boxes: frozenset[int]) -> float:
    """Cheapest box-to-goal assignment in pushes; each push costs at least one move."""
    order = sorted(boxes)
    size = len(order)
    best = [INFINITY] * (1 << size)
    best[0] = 0.0
    for mask in range(1 << size):
        current = best[mask]
        if current == INFINITY:
            continue
        index = bin(mask).count("1")
        if index == size:
            continue
        cell = order[index]
        for slot in range(size):
            if mask >> slot & 1:
                continue
            cost = puzzle.push_distance[slot].get(cell, INFINITY)
            if cost == INFINITY:
                continue
            candidate = current + cost
            if candidate < best[mask | (1 << slot)]:
                best[mask | (1 << slot)] = candidate
    return best[(1 << size) - 1]


def solve(puzzle: Puzzle) -> tuple[str, int, int]:
    """A* for the canonical optimum and its tied-solution count.

    Push edges retain the lexicographically first shortest setup walk. State labels
    compare total moves, pushes, and changes between consecutive push directions,
    while equal labels accumulate path counts. The state retains the last push
    direction because it determines the next transition's tertiary cost. A separate
    canonical label retains the smallest complete D/L/R/U move string.
    """
    goals = frozenset(puzzle.goals)
    if puzzle.boxes == goals:
        return "", 0, 1
    cache: dict[frozenset[int], float] = {}

    def estimate(boxes: frozenset[int]) -> float:
        value = cache.get(boxes)
        if value is None:
            value = lower_bound(puzzle, boxes)
            cache[boxes] = value
        return value

    start = (puzzle.player, puzzle.boxes, None)
    best = {start: (0, 0, 0)}
    canonical = {start: ""}
    ways = {start: 1}
    queue = [(estimate(puzzle.boxes), 0, 0, 0, "", puzzle.player, puzzle.boxes, None)]
    goal_primary: tuple[int, int, int] | None = None
    goal_path: str | None = None
    goal_ways = 0
    while queue:
        if goal_primary is not None and queue[0][0] > goal_primary[0]:
            break
        _, cost, pushes, changes, path, player, boxes, last_push = heapq.heappop(queue)
        state = (player, boxes, last_push)
        if (cost, pushes, changes) != best.get(state) or path != canonical.get(state):
            continue
        if boxes == goals:
            primary = (cost, pushes, changes)
            if goal_primary is None or primary < goal_primary:
                goal_primary = primary
                goal_path = path
                goal_ways = ways[state]
            elif primary == goal_primary:
                goal_path = min(goal_path, path)
                goal_ways = (goal_ways + ways[state]) % COUNT_MODULUS
            continue
        walk = routes(puzzle, boxes, player)
        for box in sorted(boxes):
            for key in ORDER:
                target = puzzle.step(box, key)
                if target is None or target in puzzle.walls or target in boxes:
                    continue
                if target in puzzle.dead:
                    continue
                stand = puzzle.step(box, OPPOSITE[key])
                if stand is None or stand in puzzle.walls or stand in boxes:
                    continue
                route_data = walk.get(stand)
                if route_data is None:
                    continue
                approach, route_count = route_data
                moved = frozenset(boxes - {box} | {target})
                if blocked_square(puzzle, moved, target):
                    continue
                bound = estimate(moved)
                if bound == INFINITY:
                    continue
                edge = approach + key
                total = cost + len(edge)
                next_changes = changes + int(
                    last_push is not None and last_push != key
                )
                state = (box, moved, key)
                primary = (total, pushes + 1, next_changes)
                old_primary = best.get(state, (INFINITY, INFINITY, INFINITY))
                next_path = path + edge
                next_ways = ways[(player, boxes, last_push)] * route_count % COUNT_MODULUS
                if primary > old_primary:
                    continue
                if primary < old_primary:
                    best[state] = primary
                    canonical[state] = next_path
                    ways[state] = next_ways
                    heapq.heappush(
                        queue,
                        (
                            total + bound,
                            total,
                            pushes + 1,
                            next_changes,
                            next_path,
                            box,
                            moved,
                            key,
                        ),
                    )
                    continue
                ways[state] = (ways[state] + next_ways) % COUNT_MODULUS
                if next_path < canonical[state]:
                    canonical[state] = next_path
                    heapq.heappush(
                        queue,
                        (
                            total + bound,
                            total,
                            pushes + 1,
                            next_changes,
                            next_path,
                            box,
                            moved,
                            key,
                        ),
                    )

    if goal_primary is None:
        raise RuntimeError("instance is unsolvable, which contradicts the published guarantee")
    return goal_path, goal_primary[2], goal_ways


def main() -> None:
    rules = json.loads((INPUT / "rules.json").read_text())
    output_contract = rules["output_contract"]
    expected_entry_keys = set(output_contract["entry_keys"])
    solutions = []
    for puzzle_id in sorted(rules["puzzle_ids"]):
        text = (INPUT / "puzzles" / f"{puzzle_id}.txt").read_text()
        moves, push_direction_changes, count = solve(Puzzle(text))
        record = {
            "puzzle_id": puzzle_id,
            "moves": moves,
            "optimal_push_direction_changes": push_direction_changes,
            "optimal_solution_count_mod": count,
        }
        if set(record) != expected_entry_keys:
            raise RuntimeError(
                "reference solution record does not match rules.json output_contract"
            )
        solutions.append(record)
    solutions.sort(key=lambda row: row["puzzle_id"].encode())

    document = {
        "schema_version": output_contract["schema_version"],
        "solutions": solutions,
    }
    if set(document) != set(output_contract["top_level_keys"]):
        raise RuntimeError(
            "reference solution document does not match rules.json output_contract"
        )

    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "solutions.json").write_text(
        json.dumps(document, indent=2) + "\n"
    )


if __name__ == "__main__":
    main()
