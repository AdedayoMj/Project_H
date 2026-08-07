from __future__ import annotations

import hashlib
import json
import os
import stat
from collections import deque
from pathlib import Path


APP = Path(os.environ.get("SOKOBAN_APP_ROOT", "/app"))
INPUT = APP / "input"
PUZZLES = INPUT / "puzzles"
FRONTIER = APP / "output" / "first_push_frontier.json"
EXPECTED = json.loads((Path(__file__).parent / "expected.json").read_text())

DIRECTIONS = {"U": (-1, 0), "D": (1, 0), "L": (0, -1), "R": (0, 1)}
ORDER = ("D", "L", "R", "U")
OPPOSITE = {"U": "D", "D": "U", "L": "R", "R": "L"}
ALPHABET = set(DIRECTIONS)


def rules() -> dict:
    return json.loads((INPUT / "rules.json").read_text())


def submitted() -> dict:
    return json.loads(FRONTIER.read_text())


def parse(text: str) -> dict:
    """Independent board reader, sharing no implementation with the Oracle."""
    rows = text.split("\n")
    while rows and not rows[-1].strip():
        rows.pop()
    width = max(len(row) for row in rows)
    walls, goals, boxes = set(), set(), set()
    player = None
    for row_index, row in enumerate(rows):
        for column_index, glyph in enumerate(row.ljust(width)):
            cell = (row_index, column_index)
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


def inside(board: dict, cell: tuple[int, int]) -> bool:
    return 0 <= cell[0] < board["height"] and 0 <= cell[1] < board["width"]


def add(cell: tuple[int, int], key: str) -> tuple[int, int]:
    delta_row, delta_column = DIRECTIONS[key]
    return cell[0] + delta_row, cell[1] + delta_column


def reachable_first_pushes(board: dict) -> list[tuple[int, int, str]]:
    """Derive the complete opening frontier from only the published board."""
    reachable = {board["player"]}
    queue = deque([board["player"]])
    while queue:
        cell = queue.popleft()
        for key in ORDER:
            target = add(cell, key)
            if (
                not inside(board, target)
                or target in board["walls"]
                or target in board["boxes"]
                or target in reachable
            ):
                continue
            reachable.add(target)
            queue.append(target)

    result = []
    for box in sorted(board["boxes"]):
        for key in ORDER:
            destination = add(box, key)
            standing = add(box, OPPOSITE[key])
            if (
                inside(board, destination)
                and destination not in board["walls"]
                and destination not in board["boxes"]
                and standing in reachable
            ):
                result.append((box[0], box[1], key))
    return result


def replay(
    board: dict, moves: str
) -> tuple[set, tuple[int, int], int, int, tuple[int, int, str] | None]:
    """Replay a completion and identify the box/direction of its first push."""
    boxes = set(board["boxes"])
    player = board["player"]
    pushes = 0
    direction_changes = 0
    last_push = None
    first_push = None
    for index, key in enumerate(moves):
        target = add(player, key)
        if not inside(board, target):
            raise ValueError(f"move {index} leaves the grid")
        if target in board["walls"]:
            raise ValueError(f"move {index} walks into a wall")
        if target in boxes:
            destination = add(target, key)
            if not inside(board, destination):
                raise ValueError(f"move {index} pushes a box off the grid")
            if destination in board["walls"]:
                raise ValueError(f"move {index} pushes a box into a wall")
            if destination in boxes:
                raise ValueError(f"move {index} pushes a box into another box")
            if first_push is None:
                first_push = (target[0], target[1], key)
            boxes.remove(target)
            boxes.add(destination)
            pushes += 1
            if last_push is not None and last_push != key:
                direction_changes += 1
            last_push = key
        player = target
    return boxes, player, pushes, direction_changes, first_push


def test_frontier_document_is_a_regular_file():
    """The one requested artifact exists at the documented path as a real file."""
    assert FRONTIER.exists()
    assert not FRONTIER.is_symlink()
    assert stat.S_ISREG(FRONTIER.stat().st_mode)
    assert isinstance(json.loads(FRONTIER.read_text()), dict)


def test_published_fixture_and_hidden_contract_are_unchanged():
    """Hash-pin inputs and validate the generated frontier-coverage contract."""
    specification = rules()
    fixture = EXPECTED["fixture_contract"]
    expected_ids = [record["puzzle_id"] for record in EXPECTED["puzzles"]]
    assert EXPECTED["schema_version"] == specification["schema_version"] == 2
    assert fixture["rules_schema_version"] == specification["schema_version"]
    assert fixture["puzzle_count"] == len(expected_ids)
    assert fixture["puzzle_ids"] == expected_ids == specification["puzzle_ids"]
    assert fixture["expected_puzzle_keys"] == [
        "puzzle_id",
        "minimum_moves",
        "commitments",
        "board_sha256",
    ]
    assert fixture["expected_commitment_keys"] == [
        *specification["output_contract"]["commitment_keys"][:-1],
        "canonical_completion_sha256",
    ]
    assert (
        fixture["output_puzzle_keys"] == specification["output_contract"]["puzzle_keys"]
    )
    assert (
        fixture["output_commitment_keys"]
        == specification["output_contract"]["commitment_keys"]
    )
    assert fixture["count_modulus"] == specification["first_push_frontier"]["modulus"]
    bounds = specification["instance_bounds"]
    assert (
        fixture["push_direction_discriminating_puzzle_count"]
        >= bounds["push_direction_discriminating_puzzles_min"]
    )
    assert (
        fixture["hard_tail_push_direction_discriminating_puzzle_count"]
        >= bounds["hard_tail_push_direction_discriminating_puzzles_min"]
    )
    assert (
        fixture["frontier_unsolvable_commitment_count"]
        >= bounds["frontier_unsolvable_commitments_min"]
    )
    assert (
        fixture["frontier_positive_regret_commitment_count"]
        >= bounds["frontier_positive_regret_commitments_min"]
    )
    assert (
        fixture["frontier_multi_best_puzzle_count"]
        >= bounds["frontier_multi_best_puzzles_min"]
    )
    assert fixture["frontier_commitment_count"] == sum(
        len(record["commitments"]) for record in EXPECTED["puzzles"]
    )
    assert all(
        set(record) == set(fixture["expected_puzzle_keys"])
        and all(
            set(commitment) == set(fixture["expected_commitment_keys"])
            for commitment in record["commitments"]
        )
        for record in EXPECTED["puzzles"]
    )
    assert (
        hashlib.sha256((INPUT / "rules.json").read_bytes()).hexdigest()
        == EXPECTED["rules_sha256"]
    )
    for record in EXPECTED["puzzles"]:
        path = PUZZLES / f"{record['puzzle_id']}.txt"
        assert path.is_file()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == record["board_sha256"]
    assert {path.name for path in PUZZLES.iterdir() if path.is_file()} == {
        f"{puzzle_id}.txt" for puzzle_id in expected_ids
    }


def test_document_schema_ordering_and_frontier_membership_are_exact():
    """Require the nested schema and independently derived set of first pushes."""
    specification = rules()
    contract = specification["output_contract"]
    document = submitted()
    assert set(document) == set(contract["top_level_keys"])
    assert type(document["schema_version"]) is int
    assert document["schema_version"] == contract["schema_version"]
    assert isinstance(document["puzzles"], list)
    identifiers = [record["puzzle_id"] for record in document["puzzles"]]
    assert identifiers == specification["puzzle_ids"]
    assert identifiers == sorted(identifiers, key=lambda value: value.encode())

    for record in document["puzzles"]:
        assert set(record) == set(contract["puzzle_keys"])
        assert type(record["minimum_moves"]) is int and record["minimum_moves"] >= 0
        assert isinstance(record["commitments"], list)
        board = parse((PUZZLES / f"{record['puzzle_id']}.txt").read_text())
        identities = [
            (item["box_row"], item["box_column"], item["direction"])
            for item in record["commitments"]
        ]
        assert identities == reachable_first_pushes(board)
        for item in record["commitments"]:
            assert set(item) == set(contract["commitment_keys"])
            assert type(item["box_row"]) is int and item["box_row"] >= 0
            assert type(item["box_column"]) is int and item["box_column"] >= 0
            assert item["direction"] in ORDER
            assert type(item["solvable"]) is bool
            assert type(item["optimal_completion_count_mod"]) is int
            assert 0 <= item["optimal_completion_count_mod"] < 1_000_000_007
            nullable = (
                "conditional_moves",
                "move_regret",
                "conditional_pushes",
                "conditional_push_direction_changes",
            )
            if item["solvable"]:
                assert all(
                    type(item[key]) is int and item[key] >= 0 for key in nullable
                )
                assert isinstance(item["canonical_completion"], str)
                assert item["canonical_completion"]
                assert set(item["canonical_completion"]) <= ALPHABET
            else:
                assert all(item[key] is None for key in nullable)
                assert item["canonical_completion"] is None
                assert item["optimal_completion_count_mod"] == 0


def test_every_solvable_completion_is_functional_and_matches_its_commitment():
    """Replay each branch and verify its first push, solution, and reported metrics."""
    for record in submitted()["puzzles"]:
        board = parse((PUZZLES / f"{record['puzzle_id']}.txt").read_text())
        for item in record["commitments"]:
            if not item["solvable"]:
                continue
            moves = item["canonical_completion"]
            boxes, _, pushes, changes, first_push = replay(board, moves)
            assert boxes == board["goals"], (record["puzzle_id"], first_push)
            assert first_push == (
                item["box_row"],
                item["box_column"],
                item["direction"],
            )
            assert len(moves) == item["conditional_moves"]
            assert pushes == item["conditional_pushes"]
            assert changes == item["conditional_push_direction_changes"]


def test_hidden_conditional_optima_canonical_strings_and_counts_match():
    """Pin every conditional result while keeping canonical completions undisclosed."""
    actual_puzzles = {record["puzzle_id"]: record for record in submitted()["puzzles"]}
    for expected_puzzle in EXPECTED["puzzles"]:
        actual_puzzle = actual_puzzles[expected_puzzle["puzzle_id"]]
        assert actual_puzzle["minimum_moves"] == expected_puzzle["minimum_moves"]
        assert len(actual_puzzle["commitments"]) == len(expected_puzzle["commitments"])
        for actual, expected in zip(
            actual_puzzle["commitments"], expected_puzzle["commitments"], strict=True
        ):
            assert {
                key: value
                for key, value in actual.items()
                if key != "canonical_completion"
            } == {
                key: value
                for key, value in expected.items()
                if key != "canonical_completion_sha256"
            }
            if actual["canonical_completion"] is None:
                assert expected["canonical_completion_sha256"] is None
            else:
                assert (
                    hashlib.sha256(actual["canonical_completion"].encode()).hexdigest()
                    == expected["canonical_completion_sha256"]
                )


def test_regret_is_derived_from_each_complete_conditional_frontier():
    """Cross-check the coupling between branch costs, best opening, and regret."""
    for record in submitted()["puzzles"]:
        solvable = [item for item in record["commitments"] if item["solvable"]]
        assert solvable
        assert record["minimum_moves"] == min(
            item["conditional_moves"] for item in solvable
        )
        assert any(item["move_regret"] == 0 for item in solvable)
        for item in solvable:
            assert item["move_regret"] == (
                item["conditional_moves"] - record["minimum_moves"]
            )
