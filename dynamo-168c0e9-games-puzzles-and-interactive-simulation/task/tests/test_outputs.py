from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path


APP = Path(os.environ.get("SOKOBAN_APP_ROOT", "/app"))
INPUT = APP / "input"
PUZZLES = INPUT / "puzzles"
SOLUTIONS = APP / "output" / "solutions.json"
EXPECTED = json.loads((Path(__file__).parent / "expected.json").read_text())

DIRECTIONS = {"U": (-1, 0), "D": (1, 0), "L": (0, -1), "R": (0, 1)}
ALPHABET = set(DIRECTIONS)


def rules() -> dict:
    return json.loads((INPUT / "rules.json").read_text())


def submitted() -> dict:
    return json.loads(SOLUTIONS.read_text())


def parse(text: str) -> dict:
    """Independent board reader, so the verifier shares no code with the solution."""
    rows = text.split("\n")
    while rows and not rows[-1].strip():
        rows.pop()
    width = max(len(row) for row in rows)
    walls, goals, boxes = set(), set(), set()
    player = None
    for r, row in enumerate(rows):
        for c, glyph in enumerate(row.ljust(width)):
            cell = (r, c)
            if glyph == "#":
                walls.add(cell)
                continue
            if glyph in (".", "*", "+"):
                goals.add(cell)
            if glyph in ("$", "*"):
                boxes.add(cell)
            if glyph in ("@", "+"):
                player = cell
    return {
        "height": len(rows),
        "width": width,
        "walls": walls,
        "goals": goals,
        "boxes": boxes,
        "player": player,
    }


def replay(board: dict, moves: str) -> tuple[set, tuple, int, int]:
    """Apply the move string under official rules, raising on the first illegal step."""
    boxes = set(board["boxes"])
    player = board["player"]
    pushes = 0
    push_direction_changes = 0
    last_push_direction = None
    for index, key in enumerate(moves):
        delta_r, delta_c = DIRECTIONS[key]
        target = (player[0] + delta_r, player[1] + delta_c)
        if not (0 <= target[0] < board["height"] and 0 <= target[1] < board["width"]):
            raise ValueError(f"move {index} leaves the grid")
        if target in board["walls"]:
            raise ValueError(f"move {index} walks into a wall")
        if target in boxes:
            beyond = (target[0] + delta_r, target[1] + delta_c)
            if not (0 <= beyond[0] < board["height"] and 0 <= beyond[1] < board["width"]):
                raise ValueError(f"move {index} pushes a box off the grid")
            if beyond in board["walls"]:
                raise ValueError(f"move {index} pushes a box into a wall")
            if beyond in boxes:
                raise ValueError(f"move {index} pushes a box into another box")
            boxes.discard(target)
            boxes.add(beyond)
            pushes += 1
            if last_push_direction is not None and last_push_direction != key:
                push_direction_changes += 1
            last_push_direction = key
        player = target
    return boxes, player, pushes, push_direction_changes


def test_solution_document_is_a_regular_file():
    """The single requested artifact exists at the documented path as a real file."""
    assert SOLUTIONS.exists()
    assert not SOLUTIONS.is_symlink()
    assert stat.S_ISREG(SOLUTIONS.stat().st_mode)
    assert isinstance(json.loads(SOLUTIONS.read_text()), dict)


def test_published_instances_are_unchanged():
    """Every board and the normative rule document are byte-identical to the build."""
    specification = rules()
    fixture_contract = EXPECTED["fixture_contract"]
    expected_ids = [record["puzzle_id"] for record in EXPECTED["puzzles"]]
    assert EXPECTED["schema_version"] == specification["schema_version"]
    assert specification["output_contract"]["schema_version"] == EXPECTED["schema_version"]
    assert fixture_contract == {
        "rules_schema_version": specification["schema_version"],
        "puzzle_count": len(expected_ids),
        "puzzle_ids": expected_ids,
        "expected_puzzle_keys": [
            "puzzle_id",
            "optimal_moves",
            "optimal_pushes",
            "optimal_push_direction_changes",
            "optimal_solution_count_mod",
            "canonical_moves_sha256",
            "board_sha256",
        ],
        "output_entry_keys": specification["output_contract"]["entry_keys"],
        "count_modulus": specification["optimal_solution_count"]["modulus"],
        "push_direction_discriminating_puzzle_count": fixture_contract[
            "push_direction_discriminating_puzzle_count"
        ],
        "hard_tail_push_direction_discriminating_puzzle_count": fixture_contract[
            "hard_tail_push_direction_discriminating_puzzle_count"
        ],
    }
    assert fixture_contract["push_direction_discriminating_puzzle_count"] >= specification[
        "instance_bounds"
    ]["push_direction_discriminating_puzzles_min"]
    assert fixture_contract[
        "hard_tail_push_direction_discriminating_puzzle_count"
    ] >= specification["instance_bounds"][
        "hard_tail_push_direction_discriminating_puzzles_min"
    ]
    assert all(
        set(record) == set(fixture_contract["expected_puzzle_keys"])
        for record in EXPECTED["puzzles"]
    )
    assert specification["puzzle_ids"] == expected_ids
    assert specification["instance_bounds"]["puzzle_count"] == len(expected_ids)
    assert (
        hashlib.sha256((INPUT / "rules.json").read_bytes()).hexdigest()
        == EXPECTED["rules_sha256"]
    )
    for record in EXPECTED["puzzles"]:
        path = PUZZLES / f"{record['puzzle_id']}.txt"
        assert path.is_file()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == record["board_sha256"]
    published = {path.name for path in PUZZLES.iterdir() if path.is_file()}
    assert published == {f"{puzzle_id}.txt" for puzzle_id in expected_ids}


def test_solution_document_matches_the_normative_schema():
    """Exact key sets, schema version, puzzle-id ordering, and the uppercase alphabet."""
    contract = rules()["output_contract"]
    data = submitted()
    assert set(data) == set(contract["top_level_keys"])
    assert data["schema_version"] == contract["schema_version"]
    entries = data["solutions"]
    assert isinstance(entries, list)
    identifiers = [record["puzzle_id"] for record in entries]
    assert identifiers == sorted(identifiers, key=lambda value: value.encode())
    assert len(identifiers) == len(set(identifiers))
    assert identifiers == [record["puzzle_id"] for record in EXPECTED["puzzles"]]
    for record in entries:
        assert set(record) == set(contract["entry_keys"])
        assert isinstance(record["moves"], str)
        assert record["moves"], f"{record['puzzle_id']} has an empty solution"
        assert set(record["moves"]) <= ALPHABET, record["puzzle_id"]
        assert type(record["optimal_solution_count_mod"]) is int
        assert type(record["optimal_push_direction_changes"]) is int
        assert record["optimal_push_direction_changes"] >= 0
        assert (
            0
            <= record["optimal_solution_count_mod"]
            < EXPECTED["fixture_contract"]["count_modulus"]
        )


def test_every_solution_is_legal_and_solves_its_puzzle():
    """Replaying each move string under official rules leaves every box on a goal."""
    entries = {record["puzzle_id"]: record["moves"] for record in submitted()["solutions"]}
    for record in EXPECTED["puzzles"]:
        puzzle_id = record["puzzle_id"]
        board = parse((PUZZLES / f"{puzzle_id}.txt").read_text())
        boxes, _, _, _ = replay(board, entries[puzzle_id])
        assert boxes == board["goals"], puzzle_id


def test_every_solution_is_the_hidden_canonical_optimum():
    """Require all numeric optima plus the published canonical tie-break."""
    entries = {
        record["puzzle_id"]: record for record in submitted()["solutions"]
    }
    for record in EXPECTED["puzzles"]:
        puzzle_id = record["puzzle_id"]
        moves = entries[puzzle_id]["moves"]
        assert len(moves) == record["optimal_moves"], (
            puzzle_id,
            len(moves),
            record["optimal_moves"],
        )
        board = parse((PUZZLES / f"{puzzle_id}.txt").read_text())
        _, _, pushes, push_direction_changes = replay(board, moves)
        assert pushes == record["optimal_pushes"], (
            puzzle_id,
            pushes,
            record["optimal_pushes"],
        )
        assert push_direction_changes == record["optimal_push_direction_changes"], (
            puzzle_id,
            push_direction_changes,
            record["optimal_push_direction_changes"],
        )
        assert entries[puzzle_id]["optimal_push_direction_changes"] == record[
            "optimal_push_direction_changes"
        ], puzzle_id
        assert hashlib.sha256(moves.encode()).hexdigest() == record[
            "canonical_moves_sha256"
        ], puzzle_id


def test_reported_optimal_solution_counts_are_exact():
    """Match strings tied on moves, pushes, and push-direction changes modulo the prime."""
    entries = {
        record["puzzle_id"]: record for record in submitted()["solutions"]
    }
    for record in EXPECTED["puzzles"]:
        puzzle_id = record["puzzle_id"]
        assert entries[puzzle_id]["optimal_solution_count_mod"] == record[
            "optimal_solution_count_mod"
        ], puzzle_id
