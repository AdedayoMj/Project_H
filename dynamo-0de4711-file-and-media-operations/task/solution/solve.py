#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import stat
import tarfile
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path


APP = Path(os.environ.get("VFX_APP_ROOT", "/app"))
REPOSITORY = APP / "repository"
INPUT = APP / "input"
SELECTION = APP / "selection.json"
PROVENANCE = APP / "provenance.json"
EXCLUSIONS = APP / "exclusions.json"
VALIDATION = APP / "validation.json"
PACKAGE = APP / "package.tar"


def byte_key(value: str) -> bytes:
    return value.encode("utf-8")


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def drop_to_frame(value: str, nominal: int, drop: int) -> int:
    hh = int(value[0:2])
    mm = int(value[3:5])
    ss = int(value[6:8])
    ff = int(value[9:11])
    if value[8] != ";" or mm >= 60 or ss >= 60 or ff >= nominal:
        raise ValueError(f"invalid drop-frame timecode {value}")
    if mm % 10 and ss == 0 and ff < drop:
        raise ValueError(f"nonexistent drop-frame timecode {value}")
    total_minutes = hh * 60 + mm
    return ((hh * 3600 + mm * 60 + ss) * nominal + ff) - drop * (
        total_minutes - total_minutes // 10
    )


def round_even(value: Fraction) -> int:
    quotient, remainder = divmod(value.numerator, value.denominator)
    doubled = remainder * 2
    if doubled < value.denominator:
        return quotient
    if doubled > value.denominator:
        return quotient + 1
    return quotient if quotient % 2 == 0 else quotient + 1


def coalesce_points(points: set[int]) -> list[list[int]]:
    if not points:
        return []
    ordered = sorted(points)
    result: list[list[int]] = []
    start = previous = ordered[0]
    for value in ordered[1:]:
        if value == previous + 1:
            previous = value
            continue
        result.append([start, previous])
        start = previous = value
    result.append([start, previous])
    return result


def coalesce_half_open(ranges: list[list[int]]) -> list[list[int]]:
    result: list[list[int]] = []
    for start, end in sorted(ranges):
        if start >= end:
            continue
        if result and start <= result[-1][1]:
            result[-1][1] = max(result[-1][1], end)
        else:
            result.append([start, end])
    return result


def classify(data: bytes, signatures: list[dict]) -> str | None:
    for signature in signatures:
        offset = signature["offset"]
        expected = bytes.fromhex(signature["hex"])
        if data[offset : offset + len(expected)] != expected:
            continue
        if "also_offset" in signature:
            second_offset = signature["also_offset"]
            second = bytes.fromhex(signature["also_hex"])
            if data[second_offset : second_offset + len(second)] != second:
                continue
        return signature["kind"]
    return None


@dataclass
class Candidate:
    path: str
    kind: str | None
    sha256: str | None
    size: int
    regular: bool
    evidence: dict | None = None
    symlink: bool = False


def main() -> None:
    policy = json.loads((INPUT / "policy.json").read_text())
    cut = json.loads((INPUT / "cut.json").read_text())
    catalog_rows = load_jsonl(INPUT / "catalog.jsonl")
    rules = load_jsonl(INPUT / "dependencies.jsonl")
    fixity = load_jsonl(INPUT / "fixity.jsonl")
    journal = load_jsonl(INPUT / "journal.jsonl")
    revocations = json.loads((INPUT / "revocations.json").read_text())["revocations"]
    locked_at = parse_time(policy["locked_at"])
    signer_rank: dict[str, int] = policy["evidence"]["trusted_fixity_signers"]
    trusted_journal = set(policy["evidence"]["trusted_journal_signers"])
    revision_order: list[str] = policy["versions"]["revision_order_ascending"]
    revision_rank = {revision: index for index, revision in enumerate(revision_order)}

    catalog = {
        (row["logical_id"], row["revision"]): row
        for row in catalog_rows
    }

    candidates: dict[str, Candidate] = {}
    by_sha: dict[str, list[str]] = defaultdict(list)
    paths = sorted(REPOSITORY.rglob("*"), key=lambda path: byte_key(path.relative_to(REPOSITORY).as_posix()))
    for path in paths:
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        rel = path.relative_to(REPOSITORY).as_posix()
        if not stat.S_ISREG(info.st_mode):
            candidates[rel] = Candidate(
                rel, None, None, info.st_size, False, symlink=stat.S_ISLNK(info.st_mode)
            )
            continue
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        kind = classify(data, policy["format_signatures"])
        candidates[rel] = Candidate(rel, kind, digest, len(data), True)
        by_sha[digest].append(rel)
    for values in by_sha.values():
        values.sort(key=byte_key)

    revoked = {
        row["record_id"]
        for row in revocations
        if parse_time(row["revoked_at"]) <= locked_at
    }
    valid_journal: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for event in journal:
        if event["signer"] not in trusted_journal or parse_time(event["occurred_at"]) > locked_at:
            continue
        if event["before_sha256"] != event["after_sha256"]:
            continue
        valid_journal[event["before_sha256"]][event["old_path"]].append(event["new_path"])
    for graph in valid_journal.values():
        for values in graph.values():
            values.sort(key=byte_key)

    def reachable_paths(start: str, digest: str) -> set[str]:
        seen = {start}
        queue = deque([start])
        graph = valid_journal.get(digest, {})
        while queue:
            current = queue.popleft()
            for following in graph.get(current, []):
                if following not in seen:
                    seen.add(following)
                    queue.append(following)
        return seen

    def evidence_key(record: dict) -> tuple[int, float, bytes]:
        return (
            signer_rank[record["signer"]],
            -parse_time(record["observed_at"]).timestamp(),
            byte_key(record["record_id"]),
        )

    applicable: dict[str, list[dict]] = defaultdict(list)
    for record in fixity:
        if (
            record["signer"] not in signer_rank
            or record["record_id"] in revoked
            or parse_time(record["observed_at"]) > locked_at
        ):
            continue
        digest = record["sha256"]
        if record["scope"] == "content":
            target_paths = by_sha.get(digest, [])
        elif record["scope"] == "path":
            reachable = reachable_paths(record["recorded_path"], digest)
            target_paths = [path for path in by_sha.get(digest, []) if path in reachable]
        else:
            continue
        for path in target_paths:
            applicable[path].append(record)
    for path, records in applicable.items():
        records.sort(key=evidence_key)
        candidates[path].evidence = records[0]

    valid_by_identity: dict[tuple[str, str], list[Candidate]] = defaultdict(list)
    for candidate in candidates.values():
        record = candidate.evidence
        if not candidate.regular or record is None:
            continue
        row = catalog.get((record["logical_id"], record["revision"]))
        if (
            row is not None
            and candidate.kind == record["kind"] == row["kind"]
            and candidate.sha256 == record["sha256"]
        ):
            valid_by_identity[(record["logical_id"], record["revision"])].append(candidate)

    content_choice: dict[tuple[str, str], str] = {}
    eligible_paths: dict[tuple[str, str, str], list[str]] = {}
    for identity, identity_candidates in valid_by_identity.items():
        support: dict[str, list[Candidate]] = defaultdict(list)
        for candidate in identity_candidates:
            assert candidate.sha256 is not None
            support[candidate.sha256].append(candidate)
        ranked_hashes = []
        for digest, digest_candidates in support.items():
            best = min(digest_candidates, key=lambda item: evidence_key(item.evidence))
            ranked_hashes.append(
                (
                    signer_rank[best.evidence["signer"]],
                    -parse_time(best.evidence["observed_at"]).timestamp(),
                    byte_key(digest),
                    digest,
                )
            )
            eligible_paths[(identity[0], identity[1], digest)] = sorted(
                [candidate.path for candidate in digest_candidates],
                key=byte_key,
            )
        content_choice[identity] = min(ranked_hashes)[3]

    # Evaluate the time-varying dependency closure independently of storage versions.
    nominal = policy["timebase"]["nominal_fps"]
    drop = policy["timebase"]["drop_frames"]
    rate_numerator = policy["timebase"]["rate_numerator"]
    rate_denominator = policy["timebase"]["rate_denominator"]
    rules_by_owner: dict[str, list[dict]] = defaultdict(list)
    for rule in rules:
        rules_by_owner[rule["owner"]].append(rule)
    for owner_rules in rules_by_owner.values():
        owner_rules.sort(key=lambda row: byte_key(row["rule_id"]))

    usage: dict[str, set[int]] = defaultdict(set)
    audio_ranges: dict[str, list[list[int]]] = defaultdict(list)
    edge_usage: dict[tuple[str, str, str, str], set[int]] = defaultdict(set)
    roots: list[dict] = []
    all_record_frames: set[int] = set()

    for segment in cut["segments"]:
        record_start = drop_to_frame(segment["record_in"], nominal, drop)
        record_end = drop_to_frame(segment["record_out"], nominal, drop)
        duration = record_end - record_start
        if duration <= 0:
            raise RuntimeError("cut segment has non-positive duration")
        source_start = segment["source_in"]
        source_end = source_start + duration - 1
        variants = segment["variants"]
        variant_set = set(variants)
        picture_root = segment["picture_root"]
        roots.append(
            {
                "segment_id": segment["segment_id"],
                "role": "picture",
                "logical_id": picture_root,
                "record_range": [record_start, record_end - 1],
                "source_range": [source_start, source_end],
                "sample_range": None,
                "variants": variants,
            }
        )

        for record_frame in range(record_start, record_end):
            all_record_frames.add(record_frame)
            source_frame = source_start + (record_frame - record_start)
            queue = deque([picture_root])
            visited: set[str] = set()
            while queue:
                owner = queue.popleft()
                usage[owner].add(record_frame)
                if owner in visited:
                    continue
                visited.add(owner)
                active_by_slot: dict[str, list[dict]] = defaultdict(list)
                for rule in rules_by_owner.get(owner, []):
                    if not (rule["source_start"] <= source_frame <= rule["source_end"]):
                        continue
                    if not set(rule["variants_all"]).issubset(variant_set):
                        continue
                    if set(rule["variants_none"]) & variant_set:
                        continue
                    active_by_slot[rule["slot"]].append(rule)
                for slot_rules in active_by_slot.values():
                    elected = min(
                        slot_rules,
                        key=lambda row: (-row["strength"], byte_key(row["rule_id"])),
                    )
                    if elected["mode"] == "mute":
                        continue
                    tiles = elected["tiles"] or [None]
                    for tile in tiles:
                        target = elected["target_template"].format(frame=source_frame, tile=tile)
                        usage[target].add(record_frame)
                        key = (
                            elected["rule_id"],
                            owner,
                            target,
                            elected["relation"],
                        )
                        edge_usage[key].add(record_frame)
                        queue.append(target)

        for audio in segment["audio"]:
            logical_id = audio["logical_id"]
            sample_rate = audio["sample_rate"]
            start_fraction = Fraction(
                (source_start - audio["origin_frame"]) * sample_rate * rate_denominator,
                rate_numerator,
            )
            end_fraction = Fraction(
                (source_start + duration - audio["origin_frame"]) * sample_rate * rate_denominator,
                rate_numerator,
            )
            sample_range = [round_even(start_fraction), round_even(end_fraction)]
            audio_ranges[logical_id].append(sample_range)
            usage[logical_id].update(range(record_start, record_end))
            roots.append(
                {
                    "segment_id": segment["segment_id"],
                    "role": audio["role"],
                    "logical_id": logical_id,
                    "record_range": [record_start, record_end - 1],
                    "source_range": [source_start, source_end],
                    "sample_range": sample_range,
                    "variants": variants,
                }
            )

    # Elect one coherent revision for every active group.
    # A reached target carrying no catalog row at any revision cannot be resolved to an
    # asset. Report the edges that reach it; it is never elected and never packaged.
    catalogued = {logical for logical, _ in catalog}
    unresolved_logical = {
        logical_id for logical_id in usage if logical_id not in catalogued
    }
    unresolved_dependencies = [
        {
            "rule_id": rule_id,
            "from_logical_id": from_logical_id,
            "to_logical_id": to_logical_id,
            "relation": relation,
        }
        for rule_id, from_logical_id, to_logical_id, relation in edge_usage
        if to_logical_id in unresolved_logical
    ]
    unresolved_dependencies.sort(
        key=lambda row: tuple(
            byte_key(row[field])
            for field in ("rule_id", "from_logical_id", "to_logical_id", "relation")
        )
    )
    for logical_id in unresolved_logical:
        del usage[logical_id]

    active_by_group: dict[str, set[str]] = defaultdict(set)
    for logical_id in usage:
        rows = [row for (logical, _), row in catalog.items() if logical == logical_id]
        group_ids = {row["group_id"] for row in rows}
        if len(group_ids) != 1:
            raise RuntimeError(f"inconsistent group for {logical_id}")
        active_by_group[next(iter(group_ids))].add(logical_id)

    pins: dict[str, str] = policy["versions"]["pinned_revisions"]
    selected_revision: dict[str, str] = {}
    feasible_revisions: dict[str, set[str]] = defaultdict(set)
    group_has_pin: dict[str, bool] = {}
    for group_id, members in active_by_group.items():
        pin_values = {pins[member] for member in members if member in pins}
        if len(pin_values) > 1:
            raise RuntimeError(f"conflicting pins in {group_id}")
        group_has_pin[group_id] = bool(pin_values)
        for revision in revision_order:
            feasible = True
            for member in members:
                row = catalog.get((member, revision))
                if (
                    row is None
                    or parse_time(row["published_at"]) > locked_at
                    or (member, revision) not in content_choice
                ):
                    feasible = False
                    break
            if feasible:
                feasible_revisions[group_id].add(revision)
        if pin_values:
            revision = next(iter(pin_values))
            if revision not in feasible_revisions[group_id]:
                raise RuntimeError(f"pinned revision unavailable for {group_id}")
        else:
            available = [
                revision
                for revision in revision_order
                if revision in feasible_revisions[group_id]
            ]
            if not available:
                raise RuntimeError(f"no complete revision for {group_id}")
            revision = available[-1]
        for member in members:
            selected_revision[member] = revision

    # Members absent from a revision strictly newer than the elected one are exactly why
    # that newer revision was not electable for the group.
    missing_sequence_members = [
        {"group_id": group_id, "revision": revision, "logical_id": member}
        for group_id, members in active_by_group.items()
        for revision in revision_order
        if revision_rank[revision]
        > revision_rank[selected_revision[next(iter(members))]]
        for member in members
        if (member, revision) not in catalog
    ]
    missing_sequence_members.sort(
        key=lambda row: tuple(
            byte_key(row[field]) for field in ("group_id", "revision", "logical_id")
        )
    )

    # Non-regular inventory entries can never be package members.
    unsafe_archive_entries = [
        {
            "source_path": candidate.path,
            "entry_type": "symlink" if candidate.symlink else "other",
        }
        for candidate in candidates.values()
        if not candidate.regular
    ]
    unsafe_archive_entries.sort(key=lambda row: byte_key(row["source_path"]))

    selected_source: dict[str, str] = {}
    selection_entries: list[dict] = []
    kinds = [row["kind"] for row in policy["format_signatures"]]
    kind_counts = Counter()
    for logical_id in usage:
        revision = selected_revision[logical_id]
        row = catalog[(logical_id, revision)]
        digest = content_choice[(logical_id, revision)]
        alternatives = eligible_paths[(logical_id, revision, digest)]
        source_path = alternatives[0]
        selected_source[logical_id] = source_path
        candidate = candidates[source_path]
        sample_ranges = coalesce_half_open(audio_ranges.get(logical_id, []))
        entry = {
            "logical_id": logical_id,
            "revision": revision,
            "kind": row["kind"],
            "canonical_path": row["canonical_path"],
            "sha256": digest,
            "size_bytes": candidate.size,
            "source_path": source_path,
            "timeline_ranges": coalesce_points(usage[logical_id]),
            "sample_ranges": sample_ranges,
        }
        selection_entries.append(entry)
        kind_counts[row["kind"]] += 1
    selection_entries.sort(key=lambda row: byte_key(row["canonical_path"]))
    canonical_paths = [row["canonical_path"] for row in selection_entries]
    if len(canonical_paths) != len(set(canonical_paths)):
        raise RuntimeError("catalog produced duplicate canonical paths")

    # Classify every unselected physical entry using the normative precedence.
    exclusion_entries: list[dict] = []
    reason_counts = Counter()
    selected_physical = set(selected_source.values())
    logical_to_group = {
        logical_id: catalog[(logical_id, selected_revision[logical_id])]["group_id"]
        for logical_id in usage
    }
    for path in sorted(candidates, key=byte_key):
        candidate = candidates[path]
        if path in selected_physical:
            continue
        logical_id = None
        revision = None
        selected_as = None
        if not candidate.regular:
            reason = "UNSAFE_LINK"
        elif candidate.kind is None:
            reason = "UNKNOWN_FORMAT"
        elif candidate.evidence is None:
            reason = "NO_TRUSTED_FIXITY"
        else:
            record = candidate.evidence
            logical_id = record["logical_id"]
            revision = record["revision"]
            row = catalog.get((logical_id, revision))
            if row is None:
                reason = "NO_TRUSTED_FIXITY"
                logical_id = revision = None
            elif candidate.kind != record["kind"] or record["kind"] != row["kind"]:
                reason = "FORMAT_MISMATCH"
            elif logical_id not in usage:
                reason = "INACTIVE_LOGICAL"
            elif revision != selected_revision[logical_id]:
                group_id = logical_to_group[logical_id]
                selected = selected_revision[logical_id]
                later = revision_rank[revision] > revision_rank[selected]
                published = parse_time(row["published_at"]) <= locked_at
                if (
                    later
                    and published
                    and not group_has_pin[group_id]
                    and revision not in feasible_revisions[group_id]
                ):
                    reason = "INCOMPLETE_GROUP"
                else:
                    reason = "SUPERSEDED_VERSION"
            elif candidate.sha256 != content_choice[(logical_id, revision)]:
                reason = "NONCANONICAL_CONTENT"
            else:
                reason = "EQUIVALENT_COPY"
                selected_as = selected_source[logical_id]
        reason_counts[reason] += 1
        exclusion_entries.append(
            {
                "source_path": path,
                "reason": reason,
                "logical_id": logical_id,
                "revision": revision,
                "sha256": candidate.sha256,
                "selected_as": selected_as,
            }
        )

    total_bytes = sum(row["size_bytes"] for row in selection_entries)
    selection_report = {
        "schema_version": 1,
        "cut_id": cut["cut_id"],
        "entries": selection_entries,
        "totals": {
            "logical_assets": len(selection_entries),
            "source_bytes": total_bytes,
            "archive_bytes": total_bytes,
            "by_kind": {kind: kind_counts[kind] for kind in kinds},
        },
    }

    edges = [
        {
            "rule_id": key[0],
            "from_logical_id": key[1],
            "to_logical_id": key[2],
            "relation": key[3],
            "timeline_ranges": coalesce_points(frames),
        }
        for key, frames in edge_usage.items()
    ]
    edges.sort(
        key=lambda row: tuple(
            byte_key(row[field])
            for field in ("rule_id", "from_logical_id", "to_logical_id", "relation")
        )
    )
    provenance_report = {
        "schema_version": 1,
        "cut_id": cut["cut_id"],
        "roots": roots,
        "edges": edges,
    }
    exclusion_report = {
        "schema_version": 1,
        "inventoried": len(candidates),
        "entries": exclusion_entries,
        "by_reason": {
            reason: reason_counts[reason]
            for reason in policy["exclusions"]["precedence"]
        },
    }
    audio_validation = [
        {"logical_id": logical_id, "ranges": coalesce_half_open(ranges)}
        for logical_id, ranges in audio_ranges.items()
    ]
    audio_validation.sort(key=lambda row: byte_key(row["logical_id"]))
    validation_report = {
        "schema_version": 1,
        "cut_id": cut["cut_id"],
        "record_frame_count": len(all_record_frames),
        "segment_count": len(cut["segments"]),
        "audio_sample_ranges": audio_validation,
        "selected_assets": len(selection_entries),
        "excluded_sources": len(exclusion_entries),
        "archive_entries": len(selection_entries),
        "archive_bytes": total_bytes,
        "unresolved_dependencies": unresolved_dependencies,
        "missing_sequence_members": missing_sequence_members,
        "unsafe_archive_entries": unsafe_archive_entries,
    }

    for path, report in (
        (SELECTION, selection_report),
        (PROVENANCE, provenance_report),
        (EXCLUSIONS, exclusion_report),
        (VALIDATION, validation_report),
    ):
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    with tarfile.open(PACKAGE, "w", format=tarfile.PAX_FORMAT) as archive:
        for entry in selection_entries:
            data = (REPOSITORY / entry["source_path"]).read_bytes()
            info = tarfile.TarInfo(entry["canonical_path"])
            info.size = len(data)
            info.mode = 0o640
            info.uid = 0
            info.gid = 0
            info.mtime = 0
            import io

            archive.addfile(info, io.BytesIO(data))


if __name__ == "__main__":
    main()
