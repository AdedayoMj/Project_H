from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
import tarfile
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath


APP = Path(os.environ.get("VFX_APP_ROOT", "/app"))
REPOSITORY = APP / "repository"
INPUT = APP / "input"
SELECTION = APP / "selection.json"
PROVENANCE = APP / "provenance.json"
EXCLUSIONS = APP / "exclusions.json"
VALIDATION = APP / "validation.json"
PACKAGE = APP / "package.tar"

EXPECTED_REPOSITORY_SHA256 = "a7aa53d42621481957ef438cda569121f7cd49dc90cd511ffdd2e4e3711d648b"
EXPECTED_REPORTS_SHA256 = "0857dd16a09504b86f01ba1fd0afa19a08edace1ff9abc93946bd0329201aec0"
EXPECTED_INPUT_SHA256 = {
    "catalog.jsonl": "0db8570240b6c7d3943a14be153608c6e926d9be122fdeae800f5a055c5d2c1b",
    "cut.json": "972b2c599b2d80742860d3f9921ecf9e32cd018f8e5cbcfb3eed8ea0c881c034",
    "dependencies.jsonl": "b4e7dedbe7d278c2414ca6fa577a4453fde187f8309f5e5b60e401b8b8e70644",
    "fixity.jsonl": "47dd21d5f943285ed00c9effafdb6050c2adf8028999e8bcd35ac9d07c18c9ac",
    "journal.jsonl": "3b2128566e7dde6cc89d4b2998728c641f0ee68a84a749c621b54a9aef75573e",
    "policy.json": "6558fd1bb0ea396a52e16f788fad9a09f72e85b2856c5a78436717d51e1a6700",
    "revocations.json": "d3a39a6b51e7947cd08ebdfc6c8ba805338ffc6c45b01b54374ffc8b0153b5cc",
}


def byte_key(value: str) -> bytes:
    return value.encode("utf-8")


def canonical_digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def load_reports() -> dict[str, dict]:
    return {
        "selection": json.loads(SELECTION.read_text()),
        "provenance": json.loads(PROVENANCE.read_text()),
        "exclusions": json.loads(EXCLUSIONS.read_text()),
        "validation": json.loads(VALIDATION.read_text()),
    }


def repository_digest() -> str:
    rows = []
    paths = sorted(
        REPOSITORY.rglob("*"),
        key=lambda path: byte_key(path.relative_to(REPOSITORY).as_posix()),
    )
    for path in paths:
        info = path.lstat()
        rel = path.relative_to(REPOSITORY).as_posix()
        row: dict = {"path": rel}
        if stat.S_ISDIR(info.st_mode):
            row["kind"] = "dir"
        elif stat.S_ISLNK(info.st_mode):
            row.update(kind="symlink", target=os.readlink(path))
        elif stat.S_ISREG(info.st_mode):
            row.update(
                kind="file",
                size=info.st_size,
                sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        else:
            row["kind"] = "other"
        rows.append(row)
    return canonical_digest(rows)


def assert_exact_keys(value: dict, keys: list[str]) -> None:
    assert isinstance(value, dict)
    assert set(value) == set(keys)


def assert_ranges(ranges: object, *, half_open: bool) -> None:
    assert isinstance(ranges, list)
    previous_end = None
    for pair in ranges:
        assert (
            isinstance(pair, list)
            and len(pair) == 2
            and all(isinstance(value, int) and not isinstance(value, bool) for value in pair)
        )
        start, end = pair
        assert start < end if half_open else start <= end
        if previous_end is not None:
            assert start > previous_end if half_open else start > previous_end + 1
        previous_end = end


def normalize_equivalent_sources(reports: dict[str, dict]) -> dict[str, dict]:
    """Normalize only the explicitly permitted byte-identical source representative choice."""
    normalized = copy.deepcopy(reports)
    selections = normalized["selection"]["entries"]
    exclusions = normalized["exclusions"]["entries"]
    rebuilt_exclusions = [row for row in exclusions if row["reason"] != "EQUIVALENT_COPY"]
    for entry in selections:
        related = [
            row
            for row in exclusions
            if row["reason"] == "EQUIVALENT_COPY"
            and row["logical_id"] == entry["logical_id"]
            and row["revision"] == entry["revision"]
            and row["sha256"] == entry["sha256"]
            and row["selected_as"] == entry["source_path"]
        ]
        paths = [entry["source_path"]] + [row["source_path"] for row in related]
        representative = min(paths, key=byte_key)
        entry["source_path"] = representative
        for path in paths:
            if path == representative:
                continue
            rebuilt_exclusions.append(
                {
                    "source_path": path,
                    "reason": "EQUIVALENT_COPY",
                    "logical_id": entry["logical_id"],
                    "revision": entry["revision"],
                    "sha256": entry["sha256"],
                    "selected_as": representative,
                }
            )
    rebuilt_exclusions.sort(key=lambda row: byte_key(row["source_path"]))
    normalized["exclusions"]["entries"] = rebuilt_exclusions
    return normalized


def test_all_five_requested_artifacts_are_regular_files():
    """Every requested JSON report and the TAR package exists as a non-symlinked regular file."""
    for path in (SELECTION, PROVENANCE, EXCLUSIONS, VALIDATION, PACKAGE):
        assert path.exists()
        assert not path.is_symlink()
        assert path.is_file()
    for path in (SELECTION, PROVENANCE, EXCLUSIONS, VALIDATION):
        assert isinstance(json.loads(path.read_text()), dict)


def test_repository_and_normative_inputs_are_unchanged():
    """The source repository topology/bytes and every normative input retain their build fingerprints."""
    actual_inputs = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in INPUT.iterdir()
        if path.is_file()
    }
    assert actual_inputs == EXPECTED_INPUT_SHA256
    assert repository_digest() == EXPECTED_REPOSITORY_SHA256


def test_selection_schema_order_hashes_and_range_forms():
    """Selection uses the exact schema, canonical ordering, exact source hashes, and normalized range forms."""
    policy = json.loads((INPUT / "policy.json").read_text())
    data = json.loads(SELECTION.read_text())
    spec = policy["outputs"]["selection"]
    assert_exact_keys(data, spec["top_level_keys"])
    assert data["schema_version"] == 1
    assert isinstance(data["cut_id"], str)
    assert isinstance(data["entries"], list)
    assert_exact_keys(data["totals"], spec["totals_keys"])
    assert [row["canonical_path"] for row in data["entries"]] == sorted(
        [row["canonical_path"] for row in data["entries"]], key=byte_key
    )
    assert len({row["logical_id"] for row in data["entries"]}) == len(data["entries"])
    assert len({row["canonical_path"] for row in data["entries"]}) == len(data["entries"])
    source_paths = set()
    kind_counts = Counter()
    total_bytes = 0
    for row in data["entries"]:
        assert_exact_keys(row, spec["entry_keys"])
        assert all(
            isinstance(row[field], str)
            for field in (
                "logical_id",
                "revision",
                "kind",
                "canonical_path",
                "sha256",
                "source_path",
            )
        )
        assert len(row["sha256"]) == 64
        assert isinstance(row["size_bytes"], int) and not isinstance(row["size_bytes"], bool)
        assert row["size_bytes"] >= 0
        assert_ranges(row["timeline_ranges"], half_open=False)
        assert_ranges(row["sample_ranges"], half_open=True)
        source = REPOSITORY / row["source_path"]
        assert source.is_file() and not source.is_symlink()
        payload = source.read_bytes()
        assert len(payload) == row["size_bytes"]
        assert hashlib.sha256(payload).hexdigest() == row["sha256"]
        assert row["source_path"] not in source_paths
        source_paths.add(row["source_path"])
        kind_counts[row["kind"]] += 1
        total_bytes += row["size_bytes"]
    totals = data["totals"]
    assert totals["logical_assets"] == len(data["entries"])
    assert totals["source_bytes"] == totals["archive_bytes"] == total_bytes
    expected_kinds = [row["kind"] for row in policy["format_signatures"]]
    assert set(totals["by_kind"]) == set(expected_kinds)
    assert totals["by_kind"] == {kind: kind_counts[kind] for kind in expected_kinds}


def test_provenance_schema_order_and_dependency_closure():
    """Roots and effective dependency edges use the exact schema/order and terminate at selected assets."""
    policy = json.loads((INPUT / "policy.json").read_text())
    data = json.loads(PROVENANCE.read_text())
    selection = json.loads(SELECTION.read_text())
    selected = {row["logical_id"] for row in selection["entries"]}
    spec = policy["outputs"]["provenance"]
    assert_exact_keys(data, spec["top_level_keys"])
    assert data["schema_version"] == 1
    assert data["cut_id"] == selection["cut_id"]
    cut = json.loads((INPUT / "cut.json").read_text())
    assert len(data["roots"]) == sum(1 + len(segment["audio"]) for segment in cut["segments"])
    for row in data["roots"]:
        assert_exact_keys(row, spec["root_keys"])
        assert row["logical_id"] in selected
        assert_ranges([row["record_range"]], half_open=False)
        assert_ranges([row["source_range"]], half_open=False)
        if row["sample_range"] is not None:
            assert_ranges([row["sample_range"]], half_open=True)
        assert isinstance(row["variants"], list)
    edge_order = [
        tuple(byte_key(row[field]) for field in ("rule_id", "from_logical_id", "to_logical_id", "relation"))
        for row in data["edges"]
    ]
    assert edge_order == sorted(edge_order)
    assert len(edge_order) == len(set(edge_order))
    unresolved = {
        row["to_logical_id"]
        for row in json.loads(VALIDATION.read_text())["unresolved_dependencies"]
    }
    assert unresolved.isdisjoint(selected)
    for row in data["edges"]:
        assert_exact_keys(row, spec["edge_keys"])
        assert row["from_logical_id"] in selected
        assert row["to_logical_id"] in selected or row["to_logical_id"] in unresolved
        assert_ranges(row["timeline_ranges"], half_open=False)


def test_exclusions_exactly_partition_the_physical_inventory():
    """Chosen sources and ordered exclusions form the complete inventory partition with valid equivalence links."""
    policy = json.loads((INPUT / "policy.json").read_text())
    selection = json.loads(SELECTION.read_text())
    data = json.loads(EXCLUSIONS.read_text())
    spec = policy["outputs"]["exclusions"]
    assert_exact_keys(data, spec["top_level_keys"])
    reasons = policy["exclusions"]["precedence"]
    assert set(data["by_reason"]) == set(reasons)
    assert all(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0
        for value in data["by_reason"].values()
    )
    assert [row["source_path"] for row in data["entries"]] == sorted(
        [row["source_path"] for row in data["entries"]], key=byte_key
    )
    assert len({row["source_path"] for row in data["entries"]}) == len(data["entries"])
    selected_by_path = {row["source_path"]: row for row in selection["entries"]}
    excluded_by_path = {}
    for row in data["entries"]:
        assert_exact_keys(row, spec["entry_keys"])
        assert row["reason"] in reasons
        assert row["source_path"] not in selected_by_path
        excluded_by_path[row["source_path"]] = row
        if row["reason"] == "UNSAFE_LINK":
            assert row["sha256"] is None
        else:
            source = REPOSITORY / row["source_path"]
            if source.is_file() and not source.is_symlink():
                assert hashlib.sha256(source.read_bytes()).hexdigest() == row["sha256"]
        if row["reason"] == "EQUIVALENT_COPY":
            assert row["selected_as"] in selected_by_path
            selected = selected_by_path[row["selected_as"]]
            assert (row["logical_id"], row["revision"], row["sha256"]) == (
                selected["logical_id"],
                selected["revision"],
                selected["sha256"],
            )
        else:
            assert row["selected_as"] is None
    physical = {
        path.relative_to(REPOSITORY).as_posix()
        for path in REPOSITORY.rglob("*")
        if not stat.S_ISDIR(path.lstat().st_mode)
    }
    assert set(selected_by_path).isdisjoint(excluded_by_path)
    assert set(selected_by_path) | set(excluded_by_path) == physical
    assert data["inventoried"] == len(physical)
    counts = Counter(row["reason"] for row in data["entries"])
    assert data["by_reason"] == {reason: counts[reason] for reason in reasons}


def test_validation_report_reconciles_all_other_outputs():
    """Validation counts, audio ranges, archive totals, and derived issue lists reconcile with the inputs."""
    policy = json.loads((INPUT / "policy.json").read_text())
    selection = json.loads(SELECTION.read_text())
    provenance = json.loads(PROVENANCE.read_text())
    exclusions = json.loads(EXCLUSIONS.read_text())
    data = json.loads(VALIDATION.read_text())
    spec = policy["outputs"]["validation"]
    assert_exact_keys(data, spec["top_level_keys"])
    assert data["cut_id"] == selection["cut_id"] == provenance["cut_id"]
    assert data["segment_count"] == len(json.loads((INPUT / "cut.json").read_text())["segments"])
    record_frames = set()
    for root in provenance["roots"]:
        record_frames.update(range(root["record_range"][0], root["record_range"][1] + 1))
    assert data["record_frame_count"] == len(record_frames)
    expected_audio = [
        {"logical_id": row["logical_id"], "ranges": row["sample_ranges"]}
        for row in selection["entries"]
        if row["sample_ranges"]
    ]
    expected_audio.sort(key=lambda row: byte_key(row["logical_id"]))
    assert data["audio_sample_ranges"] == expected_audio
    assert all(set(row) == set(spec["audio_entry_keys"]) for row in data["audio_sample_ranges"])
    assert data["selected_assets"] == len(selection["entries"])
    assert data["excluded_sources"] == len(exclusions["entries"])
    assert data["archive_entries"] == len(selection["entries"])
    assert data["archive_bytes"] == selection["totals"]["archive_bytes"]
    catalog = [json.loads(line) for line in (INPUT / "catalog.jsonl").read_text().splitlines() if line]
    catalogued = {row["logical_id"] for row in catalog}
    group_of = {row["logical_id"]: row["group_id"] for row in catalog}
    revisions_of = defaultdict(set)
    for row in catalog:
        revisions_of[row["logical_id"]].add(row["revision"])
    revision_order = policy["versions"]["revision_order_ascending"]
    rank = {revision: index for index, revision in enumerate(revision_order)}

    # Every edge reaching an uncatalogued target must be reported, and only those.
    expected_unresolved = [
        {field: edge[field] for field in spec["unresolved_entry_keys"]}
        for edge in provenance["edges"]
        if edge["to_logical_id"] not in catalogued
    ]
    expected_unresolved.sort(
        key=lambda row: tuple(
            byte_key(row[field])
            for field in ("rule_id", "from_logical_id", "to_logical_id", "relation")
        )
    )
    assert expected_unresolved
    assert data["unresolved_dependencies"] == expected_unresolved
    packaged = {row["logical_id"] for row in selection["entries"]}
    assert packaged.isdisjoint(row["to_logical_id"] for row in expected_unresolved)

    # Members absent above the elected revision must be reported, and only those.
    elected = {row["logical_id"]: row["revision"] for row in selection["entries"]}
    members_by_group = defaultdict(set)
    for logical_id in elected:
        members_by_group[group_of[logical_id]].add(logical_id)
    expected_missing = [
        {"group_id": group_id, "revision": revision, "logical_id": member}
        for group_id, members in members_by_group.items()
        for revision in revision_order
        if rank[revision] > rank[elected[next(iter(members))]]
        for member in members
        if revision not in revisions_of[member]
    ]
    expected_missing.sort(
        key=lambda row: tuple(
            byte_key(row[field]) for field in ("group_id", "revision", "logical_id")
        )
    )
    assert expected_missing
    assert data["missing_sequence_members"] == expected_missing
    assert all(
        len({elected[member] for member in members}) == 1
        for members in members_by_group.values()
    )

    # Every non-regular inventory entry must be reported, and none of them packaged.
    expected_unsafe = []
    for path in REPOSITORY.rglob("*"):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        if stat.S_ISREG(info.st_mode):
            continue
        expected_unsafe.append(
            {
                "source_path": path.relative_to(REPOSITORY).as_posix(),
                "entry_type": "symlink" if stat.S_ISLNK(info.st_mode) else "other",
            }
        )
    expected_unsafe.sort(key=lambda row: byte_key(row["source_path"]))
    assert expected_unsafe
    assert data["unsafe_archive_entries"] == expected_unsafe

    for key, entry_keys in (
        ("unresolved_dependencies", "unresolved_entry_keys"),
        ("missing_sequence_members", "missing_member_entry_keys"),
        ("unsafe_archive_entries", "unsafe_entry_keys"),
    ):
        for row in data[key]:
            assert_exact_keys(row, spec[entry_keys])


def test_tar_is_safe_and_exactly_contains_selected_canonical_bytes():
    """The package has only unique safe regular canonical entries whose exact bytes match selection."""
    selection = json.loads(SELECTION.read_text())
    expected = {row["canonical_path"]: row for row in selection["entries"]}
    actual = {}
    with tarfile.open(PACKAGE, "r:*") as archive:
        for member in archive.getmembers():
            path = PurePosixPath(member.name)
            assert member.name and not path.is_absolute()
            assert all(part not in {"", ".", ".."} for part in path.parts)
            assert member.isfile()
            assert member.name not in actual
            extracted = archive.extractfile(member)
            assert extracted is not None
            payload = extracted.read()
            actual[member.name] = (len(payload), hashlib.sha256(payload).hexdigest())
    assert set(actual) == set(expected)
    assert actual == {
        path: (row["size_bytes"], row["sha256"])
        for path, row in expected.items()
    }


def test_complete_reports_match_the_hidden_production_graph():
    """After the documented source-copy equivalence, all exact decisions match the hidden reference result.

    This is a whole-output pin and reports no location on mismatch. To localise a failure,
    read the structural tests first: selection schema/hashes, provenance closure, the
    exclusions partition, and the validation reconciliation each re-derive their subject
    from the inputs and fail with the offending field.
    """
    normalized = normalize_equivalent_sources(load_reports())
    assert canonical_digest(normalized) == EXPECTED_REPORTS_SHA256
