#!/usr/bin/env python3
"""Generate the hidden Sokoban benchmark and its move-optimal reference answers."""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import time
from pathlib import Path

from sokoban import (
    INFINITY,
    Board,
    DIRECTIONS,
    DIRECTION_ORDER,
    matching_bound,
    solve_move_optimal,
    walk_distances,
)

SEED = 168_0_9_431
PUZZLE_COUNT = 12

# Calibration envelopes. The caps are on expansions, not wall-clock, so the
# accepted instance set is identical on every machine; measured seconds are
# informational. Most puzzles retain the original breadth, while the hard tail
# makes the tighter push-distance matching bound genuinely load-bearing.
STANDARD_PUZZLE_COUNT = 8
STANDARD_EXPANSION_LIMIT = 150_000
STANDARD_MIN_OPTIMAL_MOVES = 40
STANDARD_MIN_EXPANSIONS = 2_500
STANDARD_INTERIOR_MIN = 10
STANDARD_INTERIOR_MAX = 16
STANDARD_BOX_MIN = 4
STANDARD_BOX_MAX = 5

HARD_PUZZLE_COUNT = PUZZLE_COUNT - STANDARD_PUZZLE_COUNT
HARD_SEED_OFFSETS = (101, 211, 307, 401)
HARD_EXPANSION_LIMIT = 450_000
HARD_MIN_OPTIMAL_MOVES = 40
HARD_MIN_EXPANSIONS = 120_000
HARD_INTERIOR_MIN = 12
HARD_INTERIOR_MAX = 18
HARD_BOX_COUNT = 6
HARD_REVERSE_PULLS = 180

assert len(HARD_SEED_OFFSETS) == HARD_PUZZLE_COUNT

OPPOSITE = {"U": "D", "D": "U", "L": "R", "R": "L"}


def carve(rng: random.Random, height: int, width: int, rooms: int) -> set[int]:
    """Carve rectangular rooms joined by one-wide corridors.

    Open blobs give the forward search an enormous push branching factor, so the
    deep-but-provable band is empty. Room-and-corridor layouts keep branching low
    while still admitting long optimal solutions, which is what real levels look like.
    """
    stride = width + 2
    floor: set[int] = set()
    centres: list[tuple[int, int]] = []
    for _ in range(rooms):
        room_h = rng.randint(2, 4)
        room_w = rng.randint(2, 4)
        top = rng.randint(1, max(1, height - room_h + 1))
        left = rng.randint(1, max(1, width - room_w + 1))
        for r in range(top, min(top + room_h, height + 1)):
            for c in range(left, min(left + room_w, width + 1)):
                floor.add(r * stride + c)
        centres.append((top + room_h // 2, left + room_w // 2))
    for index in range(1, len(centres)):
        (r0, c0), (r1, c1) = centres[index - 1], centres[index]
        if rng.random() < 0.5:
            for c in range(min(c0, c1), max(c0, c1) + 1):
                floor.add(min(r0, height) * stride + c)
            for r in range(min(r0, r1), max(r0, r1) + 1):
                floor.add(r * stride + min(c1, width))
        else:
            for r in range(min(r0, r1), max(r0, r1) + 1):
                floor.add(r * stride + min(c0, width))
            for c in range(min(c0, c1), max(c0, c1) + 1):
                floor.add(min(r1, height) * stride + c)
    return {
        cell
        for cell in floor
        if 1 <= cell // stride <= height and 1 <= cell % stride <= width
    }


def add_obstacles(rng: random.Random, floor: set[int], stride: int, count: int) -> set[int]:
    """Re-wall isolated floor cells while keeping the region connected."""
    result = set(floor)
    candidates = sorted(result)
    rng.shuffle(candidates)
    for cell in candidates:
        if count <= 0:
            break
        trial = result - {cell}
        if not trial or not connected(trial, stride):
            continue
        result = trial
        count -= 1
    return result


def connected(floor: set[int], stride: int) -> bool:
    start = next(iter(floor))
    seen = {start}
    stack = [start]
    while stack:
        cell = stack.pop()
        for delta_r, delta_c in DIRECTIONS.values():
            nxt = cell + delta_r * stride + delta_c
            if nxt in floor and nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return len(seen) == len(floor)


def compose(
    height: int,
    width: int,
    floor: set[int],
    goals: set[int],
    boxes: set[int],
    player: int,
    crop: bool = False,
) -> list[str]:
    """Render the board, optionally cropped to the floor region plus a wall border.

    Cropping must stay off while building the Board used for generation: Board derives
    its cell ids from the rendered row width, so a cropped render would put its
    coordinates out of step with this function's. Only the published board is cropped,
    which is a pure translation and leaves the optimum unchanged.
    """
    stride = width + 2
    if crop:
        rows_used = [cell // stride for cell in floor]
        cols_used = [cell % stride for cell in floor]
        top, bottom = min(rows_used) - 1, max(rows_used) + 1
        left, right = min(cols_used) - 1, max(cols_used) + 1
    else:
        top, bottom = 0, height + 1
        left, right = 0, stride - 1
    rows = []
    for r in range(top, bottom + 1):
        row = []
        for c in range(left, right + 1):
            cell = r * stride + c
            if cell not in floor:
                row.append("#")
            elif cell == player:
                row.append("+" if cell in goals else "@")
            elif cell in boxes:
                row.append("*" if cell in goals else "$")
            elif cell in goals:
                row.append(".")
            else:
                row.append(" ")
        rows.append("".join(row))
    return rows


def scatter(rng: random.Random, board: Board, pulls: int) -> tuple[frozenset[int], int] | None:
    """Walk backwards from the solved state so forward solvability is guaranteed.

    A random backward walk drifts back towards the goals, so keep the deepest state
    it visits, scored by the same admissible bound the forward search uses.
    """
    boxes = frozenset(board.goals)
    free = sorted(board.floor - boxes)
    if not free:
        return None
    player = rng.choice(free)
    best_score = -1.0
    best_state = None
    for _ in range(pulls):
        reach = walk_distances(board, boxes, player)
        options = []
        for box in sorted(boxes):
            for key in DIRECTION_ORDER:
                stand = board.step(box, key)
                if stand is None or stand in board.walls or stand in boxes:
                    continue
                landing = board.step(stand, key)
                if landing is None or landing in board.walls or landing in boxes:
                    continue
                if stand not in reach:
                    continue
                options.append((box, stand, landing))
        if not options:
            break
        box, stand, landing = rng.choice(options)
        boxes = frozenset(boxes - {box} | {stand})
        player = landing
        if boxes == frozenset(board.goals):
            continue
        bound = matching_bound(board, boxes)
        if bound == INFINITY:
            continue
        offgoal = len(boxes - board.goals)
        score = bound + offgoal
        if score > best_score:
            best_score = score
            best_state = (boxes, player)
    return best_state


def build_one(rng: random.Random, puzzle_id: str, *, hard: bool = False) -> dict | None:
    if hard:
        interior_min = HARD_INTERIOR_MIN
        interior_max = HARD_INTERIOR_MAX
        box_min = box_max = HARD_BOX_COUNT
        expansion_limit = HARD_EXPANSION_LIMIT
        min_optimal_moves = HARD_MIN_OPTIMAL_MOVES
        min_expansions = HARD_MIN_EXPANSIONS
    else:
        interior_min = STANDARD_INTERIOR_MIN
        interior_max = STANDARD_INTERIOR_MAX
        box_min = STANDARD_BOX_MIN
        box_max = STANDARD_BOX_MAX
        expansion_limit = STANDARD_EXPANSION_LIMIT
        min_optimal_moves = STANDARD_MIN_OPTIMAL_MOVES
        min_expansions = STANDARD_MIN_EXPANSIONS

    height = rng.randint(interior_min, interior_max)
    width = rng.randint(interior_min, interior_max)
    stride = width + 2
    floor = carve(rng, height, width, rng.randint(3, 6))
    if len(floor) < 32 or not connected(floor, stride):
        return None
    box_count = rng.randint(box_min, box_max)
    if len(floor) < box_count * 7:
        return None
    goals = set(rng.sample(sorted(floor), box_count))

    solved = compose(height, width, floor, goals, goals, next(iter(sorted(floor - goals))))
    try:
        board = Board(solved)
    except ValueError:
        return None
    if any(goal in board.dead for goal in board.goals):
        return None

    state = scatter(rng, board, HARD_REVERSE_PULLS if hard else 300)
    if state is None:
        return None
    boxes, player = state
    if any(cell in board.dead for cell in boxes):
        return None

    started = time.monotonic()
    moves, stats = solve_move_optimal(board, boxes, player, expansion_limit=expansion_limit)
    elapsed = time.monotonic() - started
    if hard:
        print(
            f"{puzzle_id} hard candidate: optimal={stats['optimal_moves']} "
            f"expansions={stats['expansions']} proof_seconds={elapsed:.3f}",
            flush=True,
        )
    if moves is None:
        return None
    if stats["optimal_moves"] < min_optimal_moves or stats["expansions"] < min_expansions:
        return None

    rows = compose(height, width, floor, goals, set(boxes), player, crop=True)
    return {
        "puzzle_id": puzzle_id,
        "rows": rows,
        "optimal_moves": int(stats["optimal_moves"]),
        "box_count": box_count,
        "interior": [height, width],
        "expansions": stats["expansions"],
        "proof_seconds": round(elapsed, 3),
        "reference_moves": moves,
    }


def rules_document() -> dict:
    return {
        "schema_version": 1,
        "puzzle_directory": "/app/input/puzzles",
        "puzzle_ids": [f"p{index:02d}" for index in range(1, PUZZLE_COUNT + 1)],
        "board_format": {
            "encoding": "UTF-8 text, one line per board row, trailing spaces stripped",
            "glyphs": {
                "#": "wall",
                " ": "floor",
                ".": "goal",
                "$": "box",
                "*": "box on goal",
                "@": "player",
                "+": "player on goal",
            },
            "geometry": "every row has the same length, the board is cropped to the playable region, and the outer border is wall",
        },
        "move_rules": {
            "characters": ["U", "D", "L", "R"],
            "semantics": "U decreases the row, D increases the row, L decreases the column, R increases the column",
            "walk": "the player may step onto floor or goal",
            "push": "stepping into a box moves that box one cell in the same direction; the destination must be floor or goal and must not hold another box",
            "illegal": "stepping into a wall, pushing into a wall, pushing into a second box, or leaving the grid",
            "solved": "every box occupies a goal",
        },
        "scoring": {
            "metric": "total player moves",
            "definition": "the number of characters in the move string; every player step counts whether or not it pushes a box",
            "pushes": "the number of pushes is NOT scored and is NOT a tie-break; any solution attaining the optimal move count is accepted",
            "requirement": "each submitted solution must be legal, must solve its puzzle, and its move count must equal the hidden move-optimal length",
        },
        "output_contract": {
            "path": "/app/output/solutions.json",
            "top_level_keys": ["schema_version", "solutions"],
            "schema_version": 1,
            "entry_keys": ["puzzle_id", "moves"],
            "ordering": "solutions ascending by puzzle_id UTF-8 bytes, exactly one entry per puzzle id",
            "moves_alphabet": "uppercase U, D, L, R only; no separators, whitespace, lowercase, or any other character",
        },
        "instance_bounds": {
            "puzzle_count": PUZZLE_COUNT,
            "interior_rows_max": HARD_INTERIOR_MAX,
            "interior_columns_max": HARD_INTERIOR_MAX,
            "boxes_min": STANDARD_BOX_MIN,
            "boxes_max": HARD_BOX_COUNT,
            "standard_puzzle_count": STANDARD_PUZZLE_COUNT,
            "hard_tail_puzzle_count": HARD_PUZZLE_COUNT,
            "hard_tail_min_reference_expansions": HARD_MIN_EXPANSIONS,
            "guarantee": "every instance is generated backwards from its solved state, so a legal solution exists, and the reference solver proved optimality inside the published expansion envelope",
            "reference_expansion_limit": HARD_EXPANSION_LIMIT,
        },
    }


def build(root: Path, tests_root: Path | None = None) -> None:
    standard_rng = random.Random(SEED)
    hard_rngs = [random.Random(SEED + offset) for offset in HARD_SEED_OFFSETS]
    puzzles: list[dict] = []
    attempts = 0
    seen: set[str] = set()
    while len(puzzles) < PUZZLE_COUNT:
        attempts += 1
        if attempts > 4000:
            raise RuntimeError("generator failed to reach the requested instance count")
        hard = len(puzzles) >= STANDARD_PUZZLE_COUNT
        hard_index = len(puzzles) - STANDARD_PUZZLE_COUNT
        rng = hard_rngs[hard_index] if hard else standard_rng
        candidate = build_one(rng, f"p{len(puzzles) + 1:02d}", hard=hard)
        if candidate is None:
            continue
        digest = hashlib.sha256("\n".join(candidate["rows"]).encode()).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        puzzles.append(candidate)
        print(
            f"{candidate['puzzle_id']}: optimal={candidate['optimal_moves']} "
            f"boxes={candidate['box_count']} interior={candidate['interior']} "
            f"expansions={candidate['expansions']} proof_seconds={candidate['proof_seconds']}",
            flush=True,
        )

    input_dir = root / "input"
    puzzle_dir = input_dir / "puzzles"
    puzzle_dir.mkdir(parents=True, exist_ok=True)
    for puzzle in puzzles:
        (puzzle_dir / f"{puzzle['puzzle_id']}.txt").write_text("\n".join(puzzle["rows"]) + "\n")
    (input_dir / "rules.json").write_text(json.dumps(rules_document(), indent=2) + "\n")
    (root / "output").mkdir(parents=True, exist_ok=True)
    print(f"total attempts: {attempts}", flush=True)

    if tests_root is not None:
        tests_root.mkdir(parents=True, exist_ok=True)
        expected = {
            "schema_version": 1,
            "puzzles": [
                {
                    "puzzle_id": puzzle["puzzle_id"],
                    "optimal_moves": puzzle["optimal_moves"],
                    "board_sha256": hashlib.sha256(
                        ("\n".join(puzzle["rows"]) + "\n").encode()
                    ).hexdigest(),
                }
                for puzzle in puzzles
            ],
            "rules_sha256": hashlib.sha256(
                (json.dumps(rules_document(), indent=2) + "\n").encode()
            ).hexdigest(),
        }
        (tests_root / "expected.json").write_text(json.dumps(expected, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("/app"))
    parser.add_argument("--tests-root", type=Path)
    arguments = parser.parse_args()
    build(arguments.root, arguments.tests_root)
