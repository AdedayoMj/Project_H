from __future__ import annotations

import json
from pathlib import Path

OUTPUT_PATH = Path("/app/output.json")
INPUT_PATH = Path("/app/input")
GOLDEN_INPUT_PATH = Path(__file__).parent / "golden_input"
EXPECTED_PATH = Path(__file__).parent / "expected_output.json"


def _load_output() -> dict:
    return json.loads(OUTPUT_PATH.read_text())


def _load_expected() -> dict:
    return json.loads(EXPECTED_PATH.read_text())


def _recipe_ids() -> set[str]:
    golden = json.loads((GOLDEN_INPUT_PATH / "ring_recipes.json").read_text())
    return {recipe["recipe_id"] for recipe in golden["recipes"]}


def test_output_file_exists():
    """The agent must write a single, non-symlinked JSON object to /app/output.json."""
    assert OUTPUT_PATH.exists()
    assert not OUTPUT_PATH.is_symlink()
    data = _load_output()
    assert isinstance(data, dict)


def test_input_files_unchanged():
    """Nothing under /app/input/ may be modified, added, or removed."""
    golden_files = sorted(p.relative_to(GOLDEN_INPUT_PATH) for p in GOLDEN_INPUT_PATH.rglob("*") if p.is_file())
    actual_files = sorted(p.relative_to(INPUT_PATH) for p in INPUT_PATH.rglob("*") if p.is_file())
    assert actual_files == golden_files
    for rel in golden_files:
        assert (INPUT_PATH / rel).read_bytes() == (GOLDEN_INPUT_PATH / rel).read_bytes()


def test_output_top_level_schema():
    """output.json has exactly one top-level key, isomer_counts, holding an object."""
    data = _load_output()
    assert set(data) == {"isomer_counts"}
    assert isinstance(data["isomer_counts"], dict)


def test_isomer_counts_keys_match_recipes():
    """isomer_counts must map exactly the recipe_ids present in the input -- no missing or invented ones."""
    data = _load_output()
    assert set(data["isomer_counts"]) == _recipe_ids()


def test_isomer_counts_values_are_nonnegative_integers():
    """Every isomer_counts value must be a JSON integer (not bool/float/string) and non-negative."""
    data = _load_output()
    for value in data["isomer_counts"].values():
        assert isinstance(value, int) and not isinstance(value, bool)
        assert value >= 0


def test_isomer_counts_match_reference():
    """Every recipe's isomer_counts value must exactly equal the reference count -- no tolerance."""
    data = _load_output()
    expected = _load_expected()
    assert data["isomer_counts"] == expected["isomer_counts"]
