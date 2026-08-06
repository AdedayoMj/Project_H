#!/usr/bin/env python3
"""Generate the hidden Sokoban benchmark and its canonical optimal answers."""
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
    screen_move_optimal,
    solve_move_optimal,
    walk_distances,
)

SEED = 168_0_9_431
PUZZLE_COUNT = 10

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
HARD_SEED_OFFSETS = (101, 211)
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
    screening = screen_move_optimal(
        board,
        boxes,
        player,
        expansion_limit=expansion_limit,
    )
    screening_elapsed = time.monotonic() - started
    if hard:
        print(
            f"{puzzle_id} hard candidate: optimal={screening['optimal_moves']} "
            f"expansions={screening['expansions']} "
            f"screen_seconds={screening_elapsed:.3f}",
            flush=True,
        )
    if screening["optimal_moves"] is None:
        return None
    if (
        screening["optimal_moves"] < min_optimal_moves
        or screening["expansions"] < min_expansions
    ):
        return None

    moves, stats = solve_move_optimal(
        board,
        boxes,
        player,
        expansion_limit=expansion_limit,
    )
    elapsed = time.monotonic() - started
    if moves is None or stats["optimal_moves"] != screening["optimal_moves"]:
        raise RuntimeError(f"canonical proof disagrees with screening for {puzzle_id}")

    rows = compose(height, width, floor, goals, set(boxes), player, crop=True)
    return {
        "puzzle_id": puzzle_id,
        "rows": rows,
        "optimal_moves": int(stats["optimal_moves"]),
        "optimal_pushes": int(stats["optimal_pushes"]),
        "optimal_solution_count_mod": int(stats["optimal_solution_count_mod"]),
        "box_count": box_count,
        "interior": [height, width],
        "expansions": screening["expansions"],
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
            "metric": "the lexicographic tuple (total player moves, pushes, move string)",
            "primary": "minimize the number of characters in the move string; every player step counts whether or not it pushes a box",
            "secondary": "among move-optimal solutions, minimize the number of moves that push a box",
            "tertiary": "among solutions tied on moves and pushes, choose the lexicographically smallest complete move string under the explicit character order D < L < R < U",
            "requirement": "each submitted solution must be legal, solve its puzzle, and equal the unique canonical optimum under all three levels",
        },
        "optimal_solution_count": {
            "definition": "the number of distinct complete move strings attaining both the minimum total-move count and, among those, the minimum push count",
            "distinctness": "two solutions are distinct exactly when their complete move strings differ at one or more character positions",
            "modulus": 1_000_000_007,
            "requirement": "report the count modulo 1000000007 for every puzzle",
        },
        "output_contract": {
            "path": "/app/output/solutions.json",
            "top_level_keys": ["schema_version", "solutions"],
            "schema_version": 1,
            "entry_keys": ["puzzle_id", "moves", "optimal_solution_count_mod"],
            "ordering": "solutions ascending by puzzle_id UTF-8 bytes, exactly one entry per puzzle id",
            "moves_alphabet": "uppercase U, D, L, R only; no separators, whitespace, lowercase, or any other character",
            "optimal_solution_count_mod": "an integer from 0 through 1000000006",
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
            "guarantee": "every instance is generated backwards from its solved state, so a legal solution exists, and the reference solver proved its canonical optimum and tied-solution count inside the published expansion envelope",
            "reference_expansion_limit": HARD_EXPANSION_LIMIT,
            "canonical_move_order": list(DIRECTION_ORDER),
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
            f"pushes={candidate['optimal_pushes']} boxes={candidate['box_count']} "
            f"count_mod={candidate['optimal_solution_count_mod']} "
            f"interior={candidate['interior']} "
            f"expansions={candidate['expansions']} proof_seconds={candidate['proof_seconds']}",
            flush=True,
        )

    input_dir = root / "input"
    puzzle_dir = input_dir / "puzzles"
    puzzle_dir.mkdir(parents=True, exist_ok=True)
    for puzzle in puzzles:
        (puzzle_dir / f"{puzzle['puzzle_id']}.txt").write_text("\n".join(puzzle["rows"]) + "\n")
    specification = rules_document()
    (input_dir / "rules.json").write_text(json.dumps(specification, indent=2) + "\n")
    (root / "output").mkdir(parents=True, exist_ok=True)
    print(f"total attempts: {attempts}", flush=True)

    if tests_root is not None:
        tests_root.mkdir(parents=True, exist_ok=True)
        expected = {
            "schema_version": 1,
            "fixture_contract": {
                "rules_schema_version": specification["schema_version"],
                "puzzle_count": PUZZLE_COUNT,
                "puzzle_ids": specification["puzzle_ids"],
                "expected_puzzle_keys": [
                    "puzzle_id",
                    "optimal_moves",
                    "optimal_pushes",
                    "optimal_solution_count_mod",
                    "canonical_moves_sha256",
                    "board_sha256",
                ],
                "output_entry_keys": specification["output_contract"]["entry_keys"],
                "count_modulus": specification["optimal_solution_count"]["modulus"],
            },
            "puzzles": [
                {
                    "puzzle_id": puzzle["puzzle_id"],
                    "optimal_moves": puzzle["optimal_moves"],
                    "optimal_pushes": puzzle["optimal_pushes"],
                    "optimal_solution_count_mod": puzzle[
                        "optimal_solution_count_mod"
                    ],
                    "canonical_moves_sha256": hashlib.sha256(
                        puzzle["reference_moves"].encode()
                    ).hexdigest(),
                    "board_sha256": hashlib.sha256(
                        ("\n".join(puzzle["rows"]) + "\n").encode()
                    ).hexdigest(),
                }
                for puzzle in puzzles
            ],
            "rules_sha256": hashlib.sha256(
                (json.dumps(specification, indent=2) + "\n").encode()
            ).hexdigest(),
        }
        (tests_root / "expected.json").write_text(json.dumps(expected, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("/app"))
    parser.add_argument("--tests-root", type=Path)
    arguments = parser.parse_args()
    build(arguments.root, arguments.tests_root)
