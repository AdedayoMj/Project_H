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


def reachable(puzzle: Puzzle, boxes: frozenset[int], start: int) -> dict[int, int]:
    distance = {start: 0}
    queue = deque([start])
    while queue:
        cell = queue.popleft()
        for key in ORDER:
            nxt = puzzle.step(cell, key)
            if nxt is None or nxt in puzzle.walls or nxt in boxes or nxt in distance:
                continue
            distance[nxt] = distance[cell] + 1
            queue.append(nxt)
    return distance


def route(puzzle: Puzzle, boxes: frozenset[int], start: int, target: int) -> str:
    if start == target:
        return ""
    parent = {start: (-1, "")}
    queue = deque([start])
    while queue:
        cell = queue.popleft()
        for key in ORDER:
            nxt = puzzle.step(cell, key)
            if nxt is None or nxt in puzzle.walls or nxt in boxes or nxt in parent:
                continue
            parent[nxt] = (cell, key)
            if nxt == target:
                queue.clear()
                break
            queue.append(nxt)
    moves = []
    cell = target
    while cell != start:
        previous, key = parent[cell]
        moves.append(key)
        cell = previous
    return "".join(reversed(moves))


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


def solve(puzzle: Puzzle) -> str:
    """A* over pushes, charging each edge the walk that sets it up plus the push.

    Edge cost is exactly the number of player steps involved, so the optimal path
    cost equals the optimal move count. Searching pushes instead of individual
    steps keeps the state space small without changing the optimum.
    """
    goals = frozenset(puzzle.goals)
    if puzzle.boxes == goals:
        return ""
    cache: dict[frozenset[int], float] = {}

    def estimate(boxes: frozenset[int]) -> float:
        value = cache.get(boxes)
        if value is None:
            value = lower_bound(puzzle, boxes)
            cache[boxes] = value
        return value

    start = (puzzle.player, puzzle.boxes)
    cost_of = {start: 0}
    parent: dict[tuple[int, frozenset[int]], tuple] = {}
    queue = [(estimate(puzzle.boxes), 0, puzzle.player, puzzle.boxes)]
    final = None
    while queue:
        _, cost, player, boxes = heapq.heappop(queue)
        if cost > cost_of.get((player, boxes), INFINITY):
            continue
        if boxes == goals:
            final = (player, boxes)
            break
        walk = reachable(puzzle, boxes, player)
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
                approach = walk.get(stand)
                if approach is None:
                    continue
                moved = frozenset(boxes - {box} | {target})
                if blocked_square(puzzle, moved, target):
                    continue
                bound = estimate(moved)
                if bound == INFINITY:
                    continue
                total = cost + approach + 1
                state = (box, moved)
                if total >= cost_of.get(state, INFINITY):
                    continue
                cost_of[state] = total
                parent[state] = ((player, boxes), stand, key)
                heapq.heappush(queue, (total + bound, total, box, moved))

    if final is None:
        raise RuntimeError("instance is unsolvable, which contradicts the published guarantee")

    steps = []
    state = final
    while state in parent:
        previous, stand, key = parent[state]
        steps.append((previous, stand, key))
        state = previous
    steps.reverse()
    moves = []
    for (player, boxes), stand, key in steps:
        moves.append(route(puzzle, boxes, player, stand))
        moves.append(key)
    return "".join(moves)


def main() -> None:
    rules = json.loads((INPUT / "rules.json").read_text())
    solutions = []
    for puzzle_id in sorted(rules["puzzle_ids"]):
        text = (INPUT / "puzzles" / f"{puzzle_id}.txt").read_text()
        solutions.append({"puzzle_id": puzzle_id, "moves": solve(Puzzle(text))})
    solutions.sort(key=lambda row: row["puzzle_id"].encode())
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "solutions.json").write_text(
        json.dumps({"schema_version": 1, "solutions": solutions}, indent=2) + "\n"
    )


if __name__ == "__main__":
    main()
