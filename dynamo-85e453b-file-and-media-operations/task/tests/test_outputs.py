from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections import defaultdict
from pathlib import Path


APP_ROOT = Path(os.environ.get("ESI_APP_ROOT", "/app"))
OUTPUT = APP_ROOT / "output.json"
CORPUS = APP_ROOT / "corpus"
INPUT = APP_ROOT / "input"
EXPECTED_OUTPUT_SHA256 = "e146fff65ebd97ecc9630952d2edb650b22d1b7e62dd0d6f2de924542f944ef0"
EXPECTED_CORPUS_SHA256 = "dabe819b514f92f96b64dd07fa416dcb753d02f7a4da0800224c22f3d2f0bc8c"
EXPECTED_INPUT_SHA256 = {
    "custodians.json": "4a3e52bc97c76c334021c37ca52e99d72971cff6ed7f618fabafd88ac3080468",
    "families.json": "ca423b604d3a0743615ff911ccd8abf5c00b5a63c31b04ee4dd700fda7decc8e",
    "protocol.json": "9e05ce086a227977bce2ef93caa41792fedbb938cc7dddf15ff81b1c93c2eb0f",
}
DISPOSITIONS = {
    "SYMLINK", "HARDLINK_ALIAS", "DEPTH_LIMIT", "UNSUPPORTED_TYPE", "CONTAINER",
    "PRIVILEGED", "FAMILY_PRIVILEGE", "OUTSIDE_DATE", "NOT_RESPONSIVE", "DUPLICATE", "PRODUCED",
}


def load() -> dict:
    return json.loads(OUTPUT.read_text())


def canonical_digest(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(payload).hexdigest()


def corpus_digest() -> str:
    rows = []
    inode_paths: dict[tuple[int, int], list[str]] = defaultdict(list)
    paths = sorted(CORPUS.rglob("*"), key=lambda path: path.relative_to(CORPUS).as_posix().encode())
    for path in paths:
        info = path.lstat()
        rel = path.relative_to(CORPUS).as_posix()
        row: dict = {"path": rel, "mode": stat.S_IMODE(info.st_mode)}
        if stat.S_ISLNK(info.st_mode):
            row.update(kind="symlink", target=os.readlink(path))
        elif stat.S_ISREG(info.st_mode):
            row.update(kind="file", size=info.st_size, mtime_ns=info.st_mtime_ns)
            inode_paths[(info.st_dev, info.st_ino)].append(rel)
            if info.st_size <= 64 * 1024 * 1024:
                row["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            else:
                samples = []
                with path.open("rb") as handle:
                    offsets = [0, 4096, 1024 * 1024, info.st_size // 2, max(0, info.st_size - 4096)]
                    for offset in offsets:
                        handle.seek(offset)
                        samples.append([offset, hashlib.sha256(handle.read(4096)).hexdigest()])
                row["samples"] = samples
        elif stat.S_ISDIR(info.st_mode):
            row["kind"] = "dir"
        rows.append(row)
    groups = [sorted(paths, key=str.encode) for paths in inode_paths.values() if len(paths) > 1]
    groups.sort(key=lambda paths: paths[0].encode())
    return canonical_digest({"entries": rows, "hardlink_groups": groups})


def test_output_is_a_regular_json_object():
    """The required artifact exists as a non-symlinked regular JSON object."""
    assert OUTPUT.exists()
    assert not OUTPUT.is_symlink()
    assert OUTPUT.is_file()
    assert isinstance(load(), dict)


def test_source_collection_and_protocol_are_unchanged():
    """The agent must not alter corpus content, topology, timestamps, modes, or protocol files."""
    actual_input = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in INPUT.iterdir() if path.is_file()}
    assert actual_input == EXPECTED_INPUT_SHA256
    assert corpus_digest() == EXPECTED_CORPUS_SHA256


def test_top_level_and_record_schemas_are_exact():
    """All four required report sections and their record schemas are present with no extra fields."""
    data = load()
    assert set(data) == {"production", "privilege_log", "exclusions", "summary"}
    assert isinstance(data["production"], list)
    assert isinstance(data["privilege_log"], list)
    assert isinstance(data["exclusions"], list)
    assert isinstance(data["summary"], dict)
    production_keys = {
        "sequence", "path", "media_type", "custodian", "family_id", "normalized_hash",
        "effective_utc_date", "size_bytes", "volume",
    }
    privilege_keys = {"path", "family_id", "reason", "trigger_paths"}
    exclusion_keys = {"path", "reason", "media_type", "family_id", "duplicate_of"}
    assert all(set(row) == production_keys for row in data["production"])
    assert all(set(row) == privilege_keys for row in data["privilege_log"])
    assert all(set(row) == exclusion_keys for row in data["exclusions"])
    assert set(data["summary"]) == {
        "inventoried", "produced", "privileged", "excluded", "by_disposition", "volumes",
    }


def test_manifest_types_partition_and_canonical_fields():
    """Production and exclusion paths form a unique complete partition with canonical identifiers and values."""
    data = load()
    protocol = json.loads((INPUT / "protocol.json").read_text())
    technical_media = protocol["technical_exclusion_media_types"]
    produced_paths = [row["path"] for row in data["production"]]
    excluded_paths = [row["path"] for row in data["exclusions"]]
    assert len(set(produced_paths)) == len(produced_paths)
    assert len(set(excluded_paths)) == len(excluded_paths)
    assert set(produced_paths).isdisjoint(excluded_paths)
    assert len(produced_paths) + len(excluded_paths) == data["summary"]["inventoried"]
    for index, row in enumerate(data["production"], 1):
        assert row["sequence"] == f"PROD-{index:06d}"
        assert isinstance(row["size_bytes"], int) and not isinstance(row["size_bytes"], bool)
        assert row["size_bytes"] >= 0
        assert re.fullmatch(r"[0-9a-f]{64}", row["normalized_hash"])
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", row["effective_utc_date"])
        assert re.fullmatch(r"VOL-\d{3}", row["volume"])
    for row in data["exclusions"]:
        assert row["reason"] in DISPOSITIONS - {"PRODUCED"}
        if row["reason"] in technical_media:
            assert row["media_type"] == technical_media[row["reason"]]
        if row["reason"] == "DUPLICATE":
            assert row["duplicate_of"] in produced_paths
        else:
            assert row["duplicate_of"] is None


def test_privilege_log_and_summary_are_internally_auditable():
    """Privilege entries exactly mirror privileged exclusions and all summary counters reconcile."""
    data = load()
    privileged_exclusions = {
        row["path"]: (row["family_id"], row["reason"])
        for row in data["exclusions"]
        if row["reason"] in {"PRIVILEGED", "FAMILY_PRIVILEGE"}
    }
    logged = {row["path"]: (row["family_id"], row["reason"]) for row in data["privilege_log"]}
    assert logged == privileged_exclusions
    assert all(row["trigger_paths"] for row in data["privilege_log"])
    summary = data["summary"]
    assert set(summary["by_disposition"]) == DISPOSITIONS
    assert all(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0
        for value in summary["by_disposition"].values()
    )
    assert sum(summary["by_disposition"].values()) == summary["inventoried"]
    assert summary["produced"] == len(data["production"]) == summary["by_disposition"]["PRODUCED"]
    assert summary["excluded"] == len(data["exclusions"])
    assert summary["privileged"] == len(data["privilege_log"])


def test_volume_accounting_and_family_atomicity():
    """Declared volume sizes/items match production and no produced attachment family spans volumes."""
    data = load()
    volume_rows = {row["volume"]: row for row in data["summary"]["volumes"]}
    assert all(set(row) == {"volume", "bytes", "items"} for row in data["summary"]["volumes"])
    actual: dict[str, dict[str, int]] = defaultdict(lambda: {"bytes": 0, "items": 0})
    family_volumes: dict[str, set[str]] = defaultdict(set)
    for row in data["production"]:
        actual[row["volume"]]["bytes"] += row["size_bytes"]
        actual[row["volume"]]["items"] += 1
        family_volumes[row["family_id"]].add(row["volume"])
    expected = {name: {"bytes": row["bytes"], "items": row["items"]} for name, row in volume_rows.items()}
    assert dict(actual) == expected
    assert all(row["bytes"] <= 32768 for row in volume_rows.values())
    assert all(len(volumes) == 1 for volumes in family_volumes.values())


def test_complete_result_matches_reference():
    """The complete canonical report exactly matches the independently generated reference result."""
    assert canonical_digest(load()) == EXPECTED_OUTPUT_SHA256
