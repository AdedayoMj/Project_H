#!/usr/bin/env python3
"""Board model and move-optimal solver shared by the instance builder."""
from __future__ import annotations

import heapq
from collections import deque

WALL = "#"
FLOOR = " "
GOAL = "."
BOX = "$"
BOX_ON_GOAL = "*"
PLAYER = "@"
PLAYER_ON_GOAL = "+"

# Row/column deltas keyed by the normative move characters.
DIRECTIONS = {"U": (-1, 0), "D": (1, 0), "L": (0, -1), "R": (0, 1)}
DIRECTION_ORDER = ("D", "L", "R", "U")
COUNT_MODULUS = 1_000_000_007

INFINITY = float("inf")


class Board:
    """A parsed Sokoban instance on a fixed rectangular grid."""

    def __init__(self, rows: list[str]) -> None:
        self.height = len(rows)
        self.width = max(len(row) for row in rows)
        self.rows = [row.ljust(self.width) for row in rows]
        self.walls: set[int] = set()
        self.goals: set[int] = set()
        boxes: set[int] = set()
        player = -1
        for r, row in enumerate(self.rows):
            for c, glyph in enumerate(row):
                cell = r * self.width + c
                if glyph == WALL:
                    self.walls.add(cell)
                    continue
                if glyph in (GOAL, BOX_ON_GOAL, PLAYER_ON_GOAL):
                    self.goals.add(cell)
                if glyph in (BOX, BOX_ON_GOAL):
                    boxes.add(cell)
                if glyph in (PLAYER, PLAYER_ON_GOAL):
                    player = cell
        self.boxes = frozenset(boxes)
        self.player = player
        self.floor = {
            cell
            for cell in range(self.height * self.width)
            if cell not in self.walls
        }
        self._neighbours = {
            key: {
                cell: cell + delta_r * self.width + delta_c
                for cell in range(self.height * self.width)
                if 0 <= cell % self.width + delta_c < self.width
                and 0 <= cell // self.width + delta_r < self.height
            }
            for key, (delta_r, delta_c) in DIRECTIONS.items()
        }
        self.push_distance = self._push_distances()
        self.dead = {
            cell
            for cell in self.floor
            if all(table.get(cell, INFINITY) == INFINITY for table in self.push_distance)
        }

    def step(self, cell: int, key: str) -> int | None:
        return self._neighbours[key].get(cell)

    def _push_distances(self) -> list[dict[int, float]]:
        """Minimum pushes from each cell to each goal, ignoring other boxes."""
        tables = []
        for goal in sorted(self.goals):
            distance = {goal: 0.0}
            queue = deque([goal])
            while queue:
                cell = queue.popleft()
                for key in DIRECTION_ORDER:
                    origin = self.step(cell, key)
                    if origin is None or origin in self.walls or origin in distance:
                        continue
                    # The pusher must stand one further along the same axis.
                    stand = self.step(origin, key)
                    if stand is None or stand in self.walls:
                        continue
                    distance[origin] = distance[cell] + 1
                    queue.append(origin)
            tables.append(distance)
        return tables

    def render(self, boxes: frozenset[int], player: int) -> list[str]:
        rows = []
        for r in range(self.height):
            row = []
            for c in range(self.width):
                cell = r * self.width + c
                if cell in self.walls:
                    row.append(WALL)
                elif cell == player:
                    row.append(PLAYER_ON_GOAL if cell in self.goals else PLAYER)
                elif cell in boxes:
                    row.append(BOX_ON_GOAL if cell in self.goals else BOX)
                elif cell in self.goals:
                    row.append(GOAL)
                else:
                    row.append(FLOOR)
            rows.append("".join(row).rstrip())
        return rows


def walk_distances(board: Board, boxes: frozenset[int], start: int) -> dict[int, int]:
    """Player step counts to every cell reachable without disturbing a box."""
    distance = {start: 0}
    queue = deque([start])
    while queue:
        cell = queue.popleft()
        for key in DIRECTION_ORDER:
            target = board.step(cell, key)
            if target is None or target in board.walls or target in boxes:
                continue
            if target in distance:
                continue
            distance[target] = distance[cell] + 1
            queue.append(target)
    return distance


def walk_routes(
    board: Board, boxes: frozenset[int], start: int
) -> dict[int, tuple[str, int]]:
    """Canonical shortest walk and number of shortest walks to each cell."""
    routes = {start: ("", 1)}
    distances = {start: 0}
    queue = deque([start])
    while queue:
        cell = queue.popleft()
        for key in DIRECTION_ORDER:
            target = board.step(cell, key)
            if target is None or target in board.walls or target in boxes:
                continue
            distance = distances[cell] + 1
            candidate = routes[cell][0] + key
            if target not in distances:
                distances[target] = distance
                routes[target] = (candidate, routes[cell][1])
                queue.append(target)
                continue
            if distances[target] != distance:
                continue
            canonical, count = routes[target]
            routes[target] = (
                min(canonical, candidate),
                (count + routes[cell][1]) % COUNT_MODULUS,
            )
    return routes


def frozen_deadlock(board: Board, boxes: frozenset[int], cell: int) -> bool:
    """True when `cell` joins a fully blocked 2x2 square holding an off-goal box."""
    r, c = divmod(cell, board.width)
    for top in (r - 1, r):
        for left in (c - 1, c):
            square = []
            for rr in (top, top + 1):
                for cc in (left, left + 1):
                    if not (0 <= rr < board.height and 0 <= cc < board.width):
                        square = []
                        break
                    square.append(rr * board.width + cc)
                if not square:
                    break
            if not square:
                continue
            if any(
                item not in board.walls and item not in boxes for item in square
            ):
                continue
            if any(item in boxes and item not in board.goals for item in square):
                return True
    return False


def matching_bound(board: Board, boxes: frozenset[int]) -> float:
    """Admissible lower bound: min-cost box-to-goal assignment over push distances."""
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
            cost = board.push_distance[slot].get(cell, INFINITY)
            if cost == INFINITY:
                continue
            candidate = current + cost
            target = mask | (1 << slot)
            if candidate < best[target]:
                best[target] = candidate
    return best[(1 << size) - 1]


def screen_move_optimal(
    board: Board,
    boxes: frozenset[int] | None = None,
    player: int | None = None,
    expansion_limit: int = 4_000_000,
) -> dict:
    """Prove only the primary move optimum for deterministic candidate screening.

    Instance generation rejects many candidates. Counting every tied optimum for
    those discarded boards is unnecessary and makes image builds much slower, so
    this lean A* retains one move-cost label per push state. Accepted boards are
    subsequently passed to :func:`solve_move_optimal` for the full canonical and
    multiplicity proof.
    """
    boxes = board.boxes if boxes is None else boxes
    player = board.player if player is None else player
    goals = frozenset(board.goals)
    if boxes == goals:
        return {"expansions": 0, "generated": 0, "optimal_moves": 0}

    bounds: dict[frozenset[int], float] = {}

    def heuristic(state_boxes: frozenset[int]) -> float:
        value = bounds.get(state_boxes)
        if value is None:
            value = matching_bound(board, state_boxes)
            bounds[state_boxes] = value
        return value

    initial = heuristic(boxes)
    if initial == INFINITY:
        return {"expansions": 0, "generated": 0, "optimal_moves": None}

    best_cost = {(player, boxes): 0}
    queue = [(initial, 0, player, boxes)]
    expansions = 0
    generated = 0
    while queue:
        _, cost, current_player, current_boxes = heapq.heappop(queue)
        state = (current_player, current_boxes)
        if cost != best_cost.get(state):
            continue
        if current_boxes == goals:
            return {
                "expansions": expansions,
                "generated": generated,
                "optimal_moves": cost,
            }
        expansions += 1
        if expansions > expansion_limit:
            return {
                "expansions": expansions,
                "generated": generated,
                "optimal_moves": None,
                "exhausted": False,
            }
        reach = walk_distances(board, current_boxes, current_player)
        for box in sorted(current_boxes):
            for key in DIRECTION_ORDER:
                target = board.step(box, key)
                if target is None or target in board.walls or target in current_boxes:
                    continue
                if target in board.dead:
                    continue
                stand = board.step(
                    box,
                    {"U": "D", "D": "U", "L": "R", "R": "L"}[key],
                )
                if stand is None or stand in board.walls or stand in current_boxes:
                    continue
                walk = reach.get(stand)
                if walk is None:
                    continue
                moved = frozenset(current_boxes - {box} | {target})
                if frozen_deadlock(board, moved, target):
                    continue
                estimate = heuristic(moved)
                if estimate == INFINITY:
                    continue
                next_cost = cost + walk + 1
                next_state = (box, moved)
                if next_cost >= best_cost.get(next_state, INFINITY):
                    continue
                best_cost[next_state] = next_cost
                generated += 1
                heapq.heappush(
                    queue,
                    (next_cost + estimate, next_cost, box, moved),
                )

    return {
        "expansions": expansions,
        "generated": generated,
        "optimal_moves": None,
        "exhausted": True,
    }


def solve_move_optimal(
    board: Board,
    boxes: frozenset[int] | None = None,
    player: int | None = None,
    expansion_limit: int = 4_000_000,
    optimize_push_direction_changes: bool = True,
) -> tuple[str | None, dict]:
    """Return the canonical optimum and count all numeric-objective optima.

    Every edge is one push plus the walk that positions the player for it, so the
    accumulated primary cost is exactly the player's move count. Labels then retain
    fewer pushes and fewer changes between consecutive push directions before the
    lexicographic move-string tie-break. The last push direction is part of the
    search state because it changes the cost of the next push. Setting
    ``optimize_push_direction_changes`` false reproduces the legacy two-numeric-level
    objective and is used only to prove that generated fixtures exercise the new
    criterion.
    """
    boxes = board.boxes if boxes is None else boxes
    player = board.player if player is None else player
    goals = frozenset(board.goals)
    if boxes == goals:
        return "", {
            "expansions": 0,
            "generated": 0,
            "optimal_moves": 0,
            "optimal_pushes": 0,
            "optimal_push_direction_changes": 0,
            "optimal_solution_count_mod": 1,
        }

    bounds: dict[frozenset[int], float] = {}

    def heuristic(state_boxes: frozenset[int]) -> float:
        value = bounds.get(state_boxes)
        if value is None:
            value = matching_bound(board, state_boxes)
            bounds[state_boxes] = value
        return value

    start = (player, boxes, None)
    best_primary = {start: (0, 0, 0)}
    canonical = {start: ""}
    ways = {start: 1}
    initial = heuristic(boxes)
    if initial == INFINITY:
        return None, {
            "expansions": 0,
            "generated": 0,
            "optimal_moves": None,
            "optimal_pushes": None,
            "optimal_push_direction_changes": None,
            "optimal_solution_count_mod": None,
        }
    queue = [(initial, 0, 0, 0, "", player, boxes, None)]
    expansions = 0
    generated = 0
    goal_primary: tuple[int, int, int] | None = None
    goal_path: str | None = None
    goal_ways = 0

    while queue:
        if goal_primary is not None and queue[0][0] > goal_primary[0]:
            break
        (
            _,
            cost,
            pushes,
            push_direction_changes,
            path,
            current_player,
            current_boxes,
            last_push_direction,
        ) = heapq.heappop(queue)
        state = (current_player, current_boxes, last_push_direction)
        if (
            (cost, pushes, push_direction_changes) != best_primary.get(state)
            or path != canonical.get(state)
        ):
            continue
        if current_boxes == goals:
            primary = (cost, pushes, push_direction_changes)
            if goal_primary is None or primary < goal_primary:
                goal_primary = primary
                goal_path = path
                goal_ways = ways[state]
            elif primary == goal_primary:
                goal_path = min(goal_path, path)
                goal_ways = (goal_ways + ways[state]) % COUNT_MODULUS
            continue
        expansions += 1
        if expansions > expansion_limit:
            return None, {
                "expansions": expansions,
                "generated": generated,
                "optimal_moves": None,
                "optimal_pushes": None,
                "optimal_push_direction_changes": None,
                "optimal_solution_count_mod": None,
                "exhausted": False,
            }
        routes = walk_routes(board, current_boxes, current_player)
        for box in sorted(current_boxes):
            for key in DIRECTION_ORDER:
                target = board.step(box, key)
                if target is None or target in board.walls or target in current_boxes:
                    continue
                if target in board.dead:
                    continue
                stand = board.step(box, {"U": "D", "D": "U", "L": "R", "R": "L"}[key])
                if stand is None or stand in board.walls or stand in current_boxes:
                    continue
                route_data = routes.get(stand)
                if route_data is None:
                    continue
                approach, route_count = route_data
                moved = frozenset(current_boxes - {box} | {target})
                if frozen_deadlock(board, moved, target):
                    continue
                estimate = heuristic(moved)
                if estimate == INFINITY:
                    continue
                edge = approach + key
                next_cost = cost + len(edge)
                next_pushes = pushes + 1
                next_changes = push_direction_changes + int(
                    optimize_push_direction_changes
                    and last_push_direction is not None
                    and last_push_direction != key
                )
                next_path = path + edge
                next_last_direction = key if optimize_push_direction_changes else None
                next_state = (box, moved, next_last_direction)
                next_primary = (next_cost, next_pushes, next_changes)
                old_primary = best_primary.get(
                    next_state, (INFINITY, INFINITY, INFINITY)
                )
                next_ways = ways[state] * route_count % COUNT_MODULUS
                if next_primary > old_primary:
                    continue
                if next_primary < old_primary:
                    best_primary[next_state] = next_primary
                    canonical[next_state] = next_path
                    ways[next_state] = next_ways
                    generated += 1
                    heapq.heappush(
                        queue,
                        (
                            next_cost + estimate,
                            next_cost,
                            next_pushes,
                            next_changes,
                            next_path,
                            box,
                            moved,
                            next_last_direction,
                        ),
                    )
                    continue
                ways[next_state] = (ways[next_state] + next_ways) % COUNT_MODULUS
                if next_path < canonical[next_state]:
                    canonical[next_state] = next_path
                    heapq.heappush(
                        queue,
                        (
                            next_cost + estimate,
                            next_cost,
                            next_pushes,
                            next_changes,
                            next_path,
                            box,
                            moved,
                            next_last_direction,
                        ),
                    )

    if goal_primary is not None:
        return goal_path, {
            "expansions": expansions,
            "generated": generated,
            "optimal_moves": goal_primary[0],
            "optimal_pushes": goal_primary[1],
            "optimal_push_direction_changes": goal_primary[2],
            "optimal_solution_count_mod": goal_ways,
        }

    return None, {
        "expansions": expansions,
        "generated": generated,
        "optimal_moves": None,
        "optimal_pushes": None,
        "optimal_push_direction_changes": None,
        "optimal_solution_count_mod": None,
        "exhausted": True,
    }


def replay(board: Board, moves: str) -> tuple[frozenset[int], int]:
    """Apply moves under official rules, raising ValueError on any illegal step."""
    boxes = set(board.boxes)
    player = board.player
    for index, key in enumerate(moves):
        if key not in DIRECTIONS:
            raise ValueError(f"move {index}: illegal character {key!r}")
        target = board.step(player, key)
        if target is None or target in board.walls:
            raise ValueError(f"move {index}: walks into a wall")
        if target in boxes:
            beyond = board.step(target, key)
            if beyond is None or beyond in board.walls or beyond in boxes:
                raise ValueError(f"move {index}: push is obstructed")
            boxes.discard(target)
            boxes.add(beyond)
        player = target
    return frozenset(boxes), player
