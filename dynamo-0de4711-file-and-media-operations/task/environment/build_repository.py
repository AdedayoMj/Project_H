#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import shutil
import stat
import struct
from pathlib import Path


KINDS = ["usda", "exr", "alembic", "texture", "cube", "ocio", "wav", "otf", "vtt"]
EXTENSIONS = {
    "usda": "usda",
    "exr": "exr",
    "alembic": "abc",
    "texture": "tx",
    "cube": "cube",
    "ocio": "ocio",
    "wav": "wav",
    "otf": "otf",
    "vtt": "vtt",
}
MAGICS = {
    "usda": b"#usda 1.0\n",
    "exr": bytes.fromhex("762f3101"),
    "alembic": b"Ogawa",
    "texture": b"TXPK",
    "cube": b"TITLE ",
    "ocio": b"ocio_profile_version: 2\n",
    "wav": b"RIFF",
    "otf": b"OTTO",
    "vtt": b"WEBVTT\n",
}
REVISION_ORDER = ["r1", "r2", "r3"]
LOCKED_AT = "2025-06-01T00:00:00Z"
ROOT = Path("/app")


def stable_int(value: str) -> int:
    return int.from_bytes(hashlib.sha256(value.encode()).digest()[:8], "big")


def token(value: str, length: int = 20) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:length]


def json_line(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def payload(kind: str, logical_id: str, revision: str, salt: str) -> bytes:
    false_claim = f"legacy/{token(logical_id + salt, 12)}"
    metadata = json_line(
        {
            "asset_hint": false_claim,
            "revision_hint": REVISION_ORDER[(REVISION_ORDER.index(revision) + 1) % 3],
            "nonce": token(salt, 24),
        }
    )
    body = hashlib.shake_256(f"{logical_id}|{revision}|{salt}".encode()).digest(96 + stable_int(salt) % 160)
    if kind == "wav":
        wave_body = metadata + body
        return b"RIFF" + struct.pack("<I", len(wave_body) + 4) + b"WAVE" + wave_body
    return MAGICS[kind] + metadata + body


def frame_to_drop(frame_number: int) -> str:
    nominal = 30
    drop = 2
    frames_per_10_minutes = nominal * 60 * 10 - drop * 9
    frames_per_minute = nominal * 60 - drop
    frames_per_day = (nominal * 60 * 60 - drop * 54) * 24
    frame_number %= frames_per_day
    ten_minute_blocks, remainder = divmod(frame_number, frames_per_10_minutes)
    skipped = drop * 9 * ten_minute_blocks
    if remainder >= drop:
        skipped += drop * ((remainder - drop) // frames_per_minute)
    nominal_number = frame_number + skipped
    hours, remainder = divmod(nominal_number, nominal * 3600)
    minutes, remainder = divmod(remainder, nominal * 60)
    seconds, frames = divmod(remainder, nominal)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d};{frames:02d}"


def kind_for(logical_id: str) -> str:
    prefix = logical_id.split("/", 1)[0]
    return {
        "scene": "usda",
        "character": "usda",
        "environment": "usda",
        "material": "usda",
        "plate": "exr",
        "camera": "alembic",
        "geometry": "alembic",
        "fxcache": "alembic",
        "rig": "alembic",
        "texture": "texture",
        "lut": "cube",
        "config": "ocio",
        "audio": "wav",
        "font": "otf",
        "subtitle": "vtt",
    }[prefix]


def group_for(logical_id: str) -> str:
    parts = logical_id.split("/")
    if parts[0] in {"plate", "camera", "geometry", "fxcache"}:
        return "/".join(parts[:-1])
    return logical_id


def canonical_path(logical_id: str) -> str:
    return f"assets/{logical_id}.{EXTENSIONS[kind_for(logical_id)]}"


def build_policy(pins: dict[str, str]) -> dict:
    selection_keys = [
        "logical_id",
        "revision",
        "kind",
        "canonical_path",
        "sha256",
        "size_bytes",
        "source_path",
        "timeline_ranges",
        "sample_ranges",
    ]
    exclusion_reasons = [
        "UNSAFE_LINK",
        "UNKNOWN_FORMAT",
        "NO_TRUSTED_FIXITY",
        "FORMAT_MISMATCH",
        "INACTIVE_LOGICAL",
        "INCOMPLETE_GROUP",
        "SUPERSEDED_VERSION",
        "NONCANONICAL_CONTENT",
        "EQUIVALENT_COPY",
    ]
    return {
        "schema_version": 1,
        "repository_root": "/app/repository",
        "cut_manifest": "/app/input/cut.json",
        "catalog": "/app/input/catalog.jsonl",
        "dependency_rules": "/app/input/dependencies.jsonl",
        "fixity_ledger": "/app/input/fixity.jsonl",
        "rename_journal": "/app/input/journal.jsonl",
        "revocations": "/app/input/revocations.json",
        "locked_at": LOCKED_AT,
        "path_rules": {
            "physical_paths": "POSIX paths relative to /app/repository, preserved exactly",
            "canonical_paths": "catalog canonical_path values, preserved exactly",
            "ordering": "ascending UTF-8 bytes of the exact string",
            "inventory": "every non-directory entry beneath repository_root; use lstat and never follow symlinks",
            "package_safety": "TAR entries must be unique regular files at exactly selected canonical paths; absolute paths, dot/dot-dot components, links, devices, and undeclared entries are forbidden",
        },
        "format_signatures": [
            {"kind": "exr", "offset": 0, "hex": "762f3101"},
            {"kind": "alembic", "offset": 0, "hex": "4f67617761"},
            {"kind": "texture", "offset": 0, "hex": "5458504b"},
            {"kind": "otf", "offset": 0, "hex": "4f54544f"},
            {"kind": "wav", "offset": 0, "hex": "52494646", "also_offset": 8, "also_hex": "57415645"},
            {"kind": "usda", "offset": 0, "hex": "237573646120312e300a"},
            {"kind": "ocio", "offset": 0, "hex": "6f63696f5f70726f66696c655f76657273696f6e3a20320a"},
            {"kind": "cube", "offset": 0, "hex": "5449544c4520"},
            {"kind": "vtt", "offset": 0, "hex": "5745425654540a"},
        ],
        "format_classification": "Use the first matching format_signatures entry in listed order; otherwise the kind is unknown. Ignore filenames, extensions, and embedded asset/revision hints.",
        "timebase": {
            "rate_numerator": 30000,
            "rate_denominator": 1001,
            "nominal_fps": 30,
            "drop_frames": 2,
            "drop_rule": "For HH:MM:SS;FF, nominal=((HH*3600+MM*60+SS)*30+FF); subtract 2*(total_minutes-floor(total_minutes/10)). At non-tenth minutes, SS=00 with FF=00 or 01 is invalid.",
            "record_ranges": "inclusive integer absolute-frame pairs; cut record_out is exclusive",
            "source_mapping": "source frame increases by one from source_in for each record frame",
            "audio_rounding": "round an exact rational to nearest integer, ties to the even integer",
            "audio_ranges": "half-open sample pairs. For each audio root, round_even((source_boundary-origin_frame)*sample_rate*1001/30000) at both source boundaries; coalesce overlapping or adjacent ranges.",
            "range_coalescing": "sort integer points or ranges and merge exactly adjacent or overlapping values; picture timeline ranges are inclusive",
        },
        "composition": {
            "rule_activity": "A rule is active when owner matches, source_start<=source_frame<=source_end, every variants_all token is present, and no variants_none token is present.",
            "slot_election": "For each visited owner, source frame, and slot, elect the active rule with greatest integer strength; break a tie by smallest rule_id UTF-8 bytes.",
            "mute": "An elected mode=mute rule creates no dependency. An elected mode=include rule expands target_template once, or once per listed tile, using the current source frame and tile.",
            "template_expansion": "The only placeholders are {frame:04d}, replaced by the current source frame as zero-padded four-digit decimal, and {tile}, replaced by the listed tile as unpadded decimal.",
            "closure": "For every cut picture root and record frame, recursively visit elected include targets. Preserve effective edges even when a target was already visited; terminate recursion by logical identity within that frame context.",
            "usage": "A logical asset is active on every record frame on which it is a root or a reached target. Edge timeline ranges are the coalesced record frames on which that exact rule/from/to/relation edge is effective.",
        },
        "evidence": {
            "sha256": "lowercase SHA-256 of exact regular-file bytes",
            "trusted_fixity_signers": {"vault-A": 1, "vault-B": 2, "vault-C": 3},
            "trusted_journal_signers": ["migration-A", "migration-B"],
            "record_validity": "A fixity record is usable only when its signer is trusted, observed_at<=locked_at, its record_id is not revoked at or before locked_at, and its sha256 equals the candidate bytes.",
            "content_scope": "A usable scope=content record applies to every regular candidate with its sha256.",
            "path_scope": "A usable scope=path record applies only at recorded_path or at a path reachable through zero or more trusted journal events at or before locked_at. Every event in the chain must preserve the record sha256 in both before_sha256 and after_sha256.",
            "identity_election": "For each physical candidate, elect its applicable fixity record by signer rank ascending, observed_at descending, then record_id UTF-8 bytes ascending.",
            "format_check": "The elected record kind, the catalog kind for its logical_id/revision, and the actual signature kind must all agree.",
            "content_election": "For each logical_id/revision, elect among format-valid candidate hashes by the best supporting candidate's signer rank ascending, observed_at descending, then sha256 UTF-8 bytes ascending.",
            "source_equivalence": "All format-valid candidates carrying the elected content hash and elected identity are equivalent source representatives. Any one may be source_path; every other member must be EQUIVALENT_COPY and selected_as must name the chosen representative.",
        },
        "versions": {
            "revision_order_ascending": REVISION_ORDER,
            "pinned_revisions": pins,
            "catalog_eligibility": "A catalog row is eligible when published_at<=locked_at and it has elected content.",
            "group_election": "Partition active logical assets by group_id. A pinned member requires its pinned revision. Otherwise elect the greatest revision in revision_order_ascending that has an eligible catalog row and elected content for every active member of the group.",
            "canonical_paths": "Use the elected catalog row. Canonical paths must be unique across selected logical assets.",
            "incomplete_group": "A candidate in an active group at a revision greater than the elected revision is INCOMPLETE_GROUP when that revision was catalog-eligible by date but lacked elected content for at least one active group member.",
        },
        "exclusions": {
            "precedence": exclusion_reasons,
            "rules": {
                "UNSAFE_LINK": "non-regular inventory entry",
                "UNKNOWN_FORMAT": "regular file matching no format signature",
                "NO_TRUSTED_FIXITY": "regular file with no elected applicable fixity identity or no matching catalog row",
                "FORMAT_MISMATCH": "elected identity exists but actual, ledger, and catalog kinds do not agree",
                "INACTIVE_LOGICAL": "valid identity maps to a logical asset outside the approved closure",
                "INCOMPLETE_GROUP": "active logical asset at a later group revision rejected by the group rule",
                "SUPERSEDED_VERSION": "active logical asset at another non-selected revision not classified INCOMPLETE_GROUP",
                "NONCANONICAL_CONTENT": "selected logical_id/revision but a non-elected content hash",
                "EQUIVALENT_COPY": "selected logical_id/revision/hash at an equivalent non-selected source path",
            },
            "selected_sources": "Chosen source representatives do not appear in exclusions. Every other inventoried entry appears exactly once using the first applicable precedence token.",
        },
        "outputs": {
            "selection": {
                "path": "/app/selection.json",
                "top_level_keys": ["schema_version", "cut_id", "entries", "totals"],
                "entry_keys": selection_keys,
                "totals_keys": ["logical_assets", "source_bytes", "archive_bytes", "by_kind"],
                "ordering": "entries by canonical_path UTF-8 bytes; by_kind has every format-signature kind",
                "field_semantics": {
                    "schema_version": "integer 1",
                    "cut_id": "exact cut cut_id",
                    "logical_id_revision_kind_canonical_path": "exact elected catalog strings",
                    "sha256": "elected exact-byte lowercase SHA-256",
                    "size_bytes": "non-negative integer selected source byte length",
                    "source_path": "one permitted exact physical representative",
                    "timeline_ranges": "nonempty coalesced inclusive usage ranges",
                    "sample_ranges": "coalesced half-open audio sample ranges, or [] for non-audio assets",
                    "totals": "logical_assets is entries length; source_bytes and archive_bytes are both the sum of size_bytes; by_kind counts entries",
                },
            },
            "provenance": {
                "path": "/app/provenance.json",
                "top_level_keys": ["schema_version", "cut_id", "roots", "edges"],
                "root_keys": ["segment_id", "role", "logical_id", "record_range", "source_range", "sample_range", "variants"],
                "edge_keys": ["rule_id", "from_logical_id", "to_logical_id", "relation", "timeline_ranges"],
                "ordering": "roots in cut segment order with picture first then cut audio order; edges by rule_id, from_logical_id, to_logical_id, relation UTF-8 byte tuples",
                "field_semantics": {
                    "schema_version": "integer 1; cut_id is exact cut cut_id",
                    "root": "segment_id, role, logical_id, and variants are copied exactly; record_range and source_range are inclusive; sample_range is the segment half-open sample pair for audio and JSON null for picture",
                    "edge": "rule_id/from/to/relation identify one effective expanded edge; timeline_ranges is its nonempty coalesced inclusive record-frame usage",
                },
            },
            "exclusions": {
                "path": "/app/exclusions.json",
                "top_level_keys": ["schema_version", "inventoried", "entries", "by_reason"],
                "entry_keys": ["source_path", "reason", "logical_id", "revision", "sha256", "selected_as"],
                "ordering": "entries by source_path UTF-8 bytes; by_reason has every precedence token",
                "field_semantics": {
                    "schema_version": "integer 1",
                    "inventoried": "integer count of every non-directory repository entry",
                    "source_path_reason": "exact physical path and first applicable precedence token",
                    "logical_id_revision": "elected fixity identity strings when it has a matching catalog row; otherwise both JSON null",
                    "sha256": "exact regular-file lowercase SHA-256, or JSON null for non-regular entries",
                    "selected_as": "chosen equivalent source_path only for EQUIVALENT_COPY; JSON null otherwise",
                    "by_reason": "integer count of entries for every precedence token, including zero",
                },
            },
            "validation": {
                "path": "/app/validation.json",
                "top_level_keys": [
                    "schema_version",
                    "cut_id",
                    "record_frame_count",
                    "segment_count",
                    "audio_sample_ranges",
                    "selected_assets",
                    "excluded_sources",
                    "archive_entries",
                    "archive_bytes",
                    "unresolved_dependencies",
                    "missing_sequence_members",
                    "unsafe_archive_entries",
                ],
                "audio_entry_keys": ["logical_id", "ranges"],
                "ordering": "audio_sample_ranges by logical_id UTF-8 bytes; the three issue arrays must be empty",
                "field_semantics": {
                    "schema_version": "integer 1; cut_id is exact cut cut_id",
                    "record_frame_count": "integer count of distinct approved record frames",
                    "segment_count": "integer cut segment count",
                    "audio_sample_ranges": "one record for each selected asset with nonempty sample_ranges, copied as logical_id/ranges",
                    "selected_assets_excluded_sources": "integer selection entries and exclusion entries counts",
                    "archive_entries_archive_bytes": "integer selected canonical-file count and sum of selected size_bytes",
                    "issue_arrays": "unresolved_dependencies, missing_sequence_members, and unsafe_archive_entries are exactly [] for a valid package",
                },
            },
            "archive": {
                "path": "/app/package.tar",
                "logical_equivalence": "TAR entry order, header format, mode, owner, group, and timestamps are ignored; safely extracted path/type/exact bytes are normative",
            },
            "json": "Every JSON output is one object. Object key order and whitespace are immaterial. Listed key sets are exact; arrays and range pairs are JSON arrays.",
        },
    }


def build(root: Path) -> None:
    repository = root / "repository"
    inputs = root / "input"
    for path in (repository, inputs):
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True)

    rules: list[dict] = []
    all_logical: set[str] = set()

    def add_rule(
        owner: str,
        suffix: str,
        slot: str,
        strength: int,
        mode: str,
        target: str | None,
        relation: str,
        start: int = 1001,
        end: int = 1080,
        all_variants: list[str] | None = None,
        none_variants: list[str] | None = None,
        tiles: list[int] | None = None,
    ) -> None:
        all_logical.add(owner)
        rules.append(
            {
                "rule_id": f"RULE-{owner.replace('/', '_')}-{suffix}",
                "owner": owner,
                "slot": slot,
                "strength": strength,
                "mode": mode,
                "target_template": target,
                "relation": relation,
                "source_start": start,
                "source_end": end,
                "variants_all": all_variants or [],
                "variants_none": none_variants or [],
                "tiles": tiles or [],
            }
        )

    for shot_no in range(1, 9):
        shot = f"S{shot_no:02d}"
        root_id = f"scene/{shot}/master"
        char_id = f"character/{shot}/hero"
        crowd_id = f"character/{shot}/crowd"
        material_id = f"material/{shot}/hero"
        day_id = f"environment/{shot}/day"
        night_id = f"environment/{shot}/night"
        rain_id = f"environment/{shot}/rain"
        grade_id = f"scene/{shot}/grade"

        add_rule(root_id, "plate", "plate", 10, "include", f"plate/{shot}/{{frame:04d}}", "plate")
        add_rule(root_id, "camera", "camera", 10, "include", f"camera/{shot}/{{frame:04d}}", "camera")
        add_rule(root_id, "char-base", "character", 10, "include", crowd_id, "sublayer")
        add_rule(root_id, "char-hero", "character", 30, "include", char_id, "sublayer", all_variants=["hero"])
        add_rule(root_id, "env-day", "environment", 10, "include", day_id, "sublayer")
        add_rule(root_id, "env-night", "environment", 30, "include", night_id, "sublayer", all_variants=["night"])
        add_rule(root_id, "rain", "weather", 20, "include", rain_id, "sublayer", all_variants=["rain"])
        add_rule(root_id, "fx", "effects", 20, "include", f"fxcache/{shot}/{{frame:04d}}", "cache", all_variants=["fx"])
        add_rule(root_id, "fx-mute", "effects", 40, "mute", None, "cache", all_variants=["nofx"])
        add_rule(root_id, "grade", "grade", 10, "include", grade_id, "look")
        add_rule(root_id, "subtitle", "subtitle", 10, "include", f"subtitle/{shot}/approved", "subtitle")

        add_rule(char_id, "geo", "geometry", 10, "include", f"geometry/{shot}/{{frame:04d}}", "cache")
        add_rule(char_id, "material", "material", 10, "include", material_id, "material")
        add_rule(char_id, "rig", "rig", 10, "include", f"rig/{shot}/hero", "rig")
        add_rule(crowd_id, "geo", "geometry", 10, "include", f"geometry/{shot}/{{frame:04d}}", "cache")
        add_rule(crowd_id, "material", "material", 10, "include", material_id, "material")
        add_rule(material_id, "diffuse", "diffuse", 10, "include", f"texture/{shot}/hero/diffuse/{{tile}}", "texture", tiles=[1001, 1002, 1011, 1012])
        add_rule(material_id, "normal", "normal", 10, "include", f"texture/{shot}/hero/normal/{{tile}}", "texture", tiles=[1001, 1002, 1011, 1012])
        add_rule(material_id, "cycle", "metadata-cycle", 5, "include", char_id, "metadata")

        for env_id, label in ((day_id, "day"), (night_id, "night"), (rain_id, "rain")):
            add_rule(env_id, "geo", "geometry", 10, "include", f"rig/{shot}/environment", "geometry")
            add_rule(
                env_id,
                "tex",
                "environment-texture",
                10,
                "include",
                f"texture/{shot}/environment/{label}/{{tile}}",
                "texture",
                tiles=[1001, 1002, 1003, 1011],
            )

        add_rule(grade_id, "lut", "lut", 10, "include", f"lut/{shot}/show", "color")
        add_rule(grade_id, "ocio", "ocio", 10, "include", "config/show/ocio", "color")
        add_rule(grade_id, "font", "font", 10, "include", "font/show/title", "font")

        for frame in range(1001, 1081):
            all_logical.update(
                {
                    f"plate/{shot}/{frame:04d}",
                    f"camera/{shot}/{frame:04d}",
                    f"geometry/{shot}/{frame:04d}",
                    f"fxcache/{shot}/{frame:04d}",
                }
            )
        for tile in [1001, 1002, 1003, 1011, 1012]:
            all_logical.update(
                {
                    f"texture/{shot}/hero/diffuse/{tile}",
                    f"texture/{shot}/hero/normal/{tile}",
                    f"texture/{shot}/environment/day/{tile}",
                    f"texture/{shot}/environment/night/{tile}",
                    f"texture/{shot}/environment/rain/{tile}",
                }
            )
        all_logical.update(
            {
                root_id,
                char_id,
                crowd_id,
                material_id,
                day_id,
                night_id,
                rain_id,
                grade_id,
                f"rig/{shot}/hero",
                f"rig/{shot}/environment",
                f"lut/{shot}/show",
                f"subtitle/{shot}/approved",
                f"audio/{shot}/dialog",
                f"audio/{shot}/effects",
            }
        )
    all_logical.update({"config/show/ocio", "font/show/title"})

    # Rejected-cut assets are valid and well-attested but must never enter the closure.
    for index in range(72):
        all_logical.add(f"texture/rejected/R{index:03d}/1001")

    start_frame = 107_892  # 01:00:00;00 at 29.97 DF.
    cursor = start_frame
    segments = []
    for index in range(24):
        shot = f"S{(index * 5) % 8 + 1:02d}"
        duration = 15 + (index * 7) % 17
        source_in = 1001 + (index * 11) % 43
        variants = [
            "night" if index % 3 == 1 else "day",
            "hero" if index % 4 != 0 else "background",
            "rain" if index % 5 in {1, 4} else "dry",
            "fx" if index % 3 != 2 else "nofx",
        ]
        segments.append(
            {
                "segment_id": f"SEG-{index + 1:03d}",
                "record_in": frame_to_drop(cursor),
                "record_out": frame_to_drop(cursor + duration),
                "source_in": source_in,
                "picture_root": f"scene/{shot}/master",
                "variants": variants,
                "audio": [
                    {
                        "role": "dialog",
                        "logical_id": f"audio/{shot}/dialog",
                        "sample_rate": 48000,
                        "origin_frame": 960 + (index % 3),
                    },
                    {
                        "role": "effects",
                        "logical_id": f"audio/{shot}/effects",
                        "sample_rate": 48000,
                        "origin_frame": 950 + (index % 5),
                    },
                ],
            }
        )
        cursor += duration

    cut = {
        "schema_version": 1,
        "cut_id": "SEQ-MERIDIAN-LOCK-07",
        "record_start": frame_to_drop(start_frame),
        "record_end": frame_to_drop(cursor),
        "segments": segments,
    }

    pins = {f"scene/S{shot:02d}/master": "r2" for shot in range(1, 9)}
    pins.update({"config/show/ocio": "r1", "font/show/title": "r2"})
    policy = build_policy(pins)

    catalog: list[dict] = []
    for asset_index, logical_id in enumerate(sorted(all_logical, key=str.encode)):
        kind = kind_for(logical_id)
        group_id = group_for(logical_id)
        for revision_index, revision in enumerate(REVISION_ORDER):
            published = f"202{3 + revision_index}-0{revision_index + 2}-15T12:00:00Z"
            if revision == "r3" and group_id == logical_id and asset_index % 6 == 0:
                published = "2025-08-15T12:00:00Z"
            catalog.append(
                {
                    "logical_id": logical_id,
                    "group_id": group_id,
                    "revision": revision,
                    "kind": kind,
                    "canonical_path": canonical_path(logical_id),
                    "published_at": published,
                }
            )

    fixity: list[dict] = []
    journal: list[dict] = []
    revoked_ids: list[dict] = []
    rng = random.Random(4711)

    def write_file(rel: str, data: bytes) -> None:
        target = repository / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        os.chmod(target, 0o640)

    for index, row in enumerate(catalog):
        logical_id = row["logical_id"]
        revision = row["revision"]
        kind = row["kind"]
        is_sequence = row["group_id"] != logical_id
        omitted = revision == "r3" and is_sequence and stable_int(logical_id) % 19 == 0
        salt = f"primary:{index}:{rng.randrange(1 << 30)}"
        data = payload(kind, logical_id, revision, salt)
        digest = hashlib.sha256(data).hexdigest()
        misleading_ext = ["dat", "bak", "tmp", "bin", EXTENSIONS[kind]][index % 5]
        current_path = f"{['depot', 'nearline', 'migration'][index % 3]}/{token('path:' + salt, 2)}/{token(salt, 28)}.{misleading_ext}"

        if omitted:
            # A plausible but unattested member makes the newest sequence revision incomplete.
            write_file(current_path, data)
            continue

        write_file(current_path, data)
        record_id = f"FX-{index:06d}-A"
        signer = "vault-A" if index % 4 else "vault-B"
        scope = "path" if index % 9 == 0 else "content"
        recorded_path = current_path
        if scope == "path":
            recorded_path = f"legacy/mac/{token('old:' + salt, 24)}.{misleading_ext}"
            midpoint = f"staging/restore/{token('mid:' + salt, 24)}.{misleading_ext}"
            journal.extend(
                [
                    {
                        "event_id": f"JN-{index:06d}-1",
                        "old_path": recorded_path,
                        "new_path": midpoint,
                        "before_sha256": digest,
                        "after_sha256": digest,
                        "signer": "migration-A",
                        "occurred_at": "2024-08-01T10:00:00Z",
                    },
                    {
                        "event_id": f"JN-{index:06d}-2",
                        "old_path": midpoint,
                        "new_path": current_path,
                        "before_sha256": digest,
                        "after_sha256": digest,
                        "signer": "migration-B",
                        "occurred_at": "2025-02-01T10:00:00Z",
                    },
                ]
            )
        fixity.append(
            {
                "record_id": record_id,
                "sha256": digest,
                "logical_id": logical_id,
                "revision": revision,
                "kind": kind,
                "scope": scope,
                "recorded_path": recorded_path,
                "signer": signer,
                "observed_at": "2025-03-01T12:00:00Z",
            }
        )

        # Revoking selected newest candidates forces evidence-aware fallback for some singleton groups.
        if revision == "r3" and row["group_id"] == logical_id and index % 41 == 0:
            revoked_ids.append({"record_id": record_id, "revoked_at": "2025-04-01T00:00:00Z"})

        # Equivalent content copies are valid alternatives only for content-scoped evidence.
        if scope == "content" and index % 13 == 0:
            duplicate_path = f"migration/copies/{token('copy:' + salt, 30)}.{['mov', 'old', 'cache'][index % 3]}"
            write_file(duplicate_path, data)

        # A weaker but valid attestation for different bytes at the same logical revision.
        if index % 23 == 0:
            alternate = payload(kind, logical_id, revision, f"alternate:{salt}")
            alternate_digest = hashlib.sha256(alternate).hexdigest()
            alternate_path = f"nearline/conflicts/{token('alt:' + salt, 30)}.dat"
            write_file(alternate_path, alternate)
            fixity.append(
                {
                    "record_id": f"FX-{index:06d}-C",
                    "sha256": alternate_digest,
                    "logical_id": logical_id,
                    "revision": revision,
                    "kind": kind,
                    "scope": "content",
                    "recorded_path": alternate_path,
                    "signer": "vault-C",
                    "observed_at": "2025-05-01T12:00:00Z",
                }
            )

        # Conflicting lower-ranked identity evidence must lose before closure/version logic.
        if index % 29 == 0:
            fixity.append(
                {
                    "record_id": f"FX-{index:06d}-CONFLICT",
                    "sha256": digest,
                    "logical_id": f"texture/rejected/R{index % 72:03d}/1001",
                    "revision": revision,
                    "kind": kind,
                    "scope": "content",
                    "recorded_path": current_path,
                    "signer": "vault-C",
                    "observed_at": "2025-05-20T12:00:00Z",
                }
            )

    # Format-mismatch evidence, untrusted evidence, unknown data, and unsafe links.
    for index in range(64):
        logical_id = f"texture/rejected/R{index:03d}/1001"
        data = payload("exr", logical_id, "r2", f"mismatch:{index}")
        digest = hashlib.sha256(data).hexdigest()
        rel = f"depot/quarantine/{token('mismatch:' + str(index), 28)}.tx"
        write_file(rel, data)
        fixity.append(
            {
                "record_id": f"BADFMT-{index:04d}",
                "sha256": digest,
                "logical_id": logical_id,
                "revision": "r2",
                "kind": "texture",
                "scope": "content",
                "recorded_path": rel,
                "signer": "vault-A",
                "observed_at": "2025-01-10T00:00:00Z",
            }
        )
    for index in range(180):
        rel = f"nearline/orphans/{token('orphan:' + str(index), 30)}.{['exr', 'abc', 'dat'][index % 3]}"
        if index % 2:
            data = rng.randbytes(80 + index % 31)
        else:
            data = payload(KINDS[index % len(KINDS)], f"unknown/{index}", "r1", f"orphan:{index}")
        write_file(rel, data)
        if index % 11 == 0:
            fixity.append(
                {
                    "record_id": f"UNTRUSTED-{index:04d}",
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "logical_id": f"texture/rejected/R{index % 72:03d}/1001",
                    "revision": "r1",
                    "kind": KINDS[index % len(KINDS)],
                    "scope": "content",
                    "recorded_path": rel,
                    "signer": "vendor-Z",
                    "observed_at": "2025-01-10T00:00:00Z",
                }
            )

    links = repository / "migration" / "links"
    links.mkdir(parents=True, exist_ok=True)
    os.symlink("../../depot", links / "depot-alias")
    os.symlink("/etc/passwd", links / "external")
    os.symlink("cycle-b", links / "cycle-a")
    os.symlink("cycle-a", links / "cycle-b")

    # Deterministic modes make input tampering detectable without making mtimes normative.
    for directory in [path for path in repository.rglob("*") if path.is_dir() and not path.is_symlink()]:
        os.chmod(directory, 0o750)
    os.chmod(repository, 0o750)

    dependencies = sorted(rules, key=lambda row: row["rule_id"].encode())
    catalog.sort(key=lambda row: (row["logical_id"].encode(), REVISION_ORDER.index(row["revision"])))
    fixity.sort(key=lambda row: row["record_id"].encode())
    journal.sort(key=lambda row: row["event_id"].encode())

    (inputs / "policy.json").write_text(json.dumps(policy, indent=2, ensure_ascii=False) + "\n")
    (inputs / "cut.json").write_text(json.dumps(cut, indent=2) + "\n")
    (inputs / "revocations.json").write_text(json.dumps({"revocations": revoked_ids}, indent=2) + "\n")
    for name, rows in (
        ("catalog.jsonl", catalog),
        ("dependencies.jsonl", dependencies),
        ("fixity.jsonl", fixity),
        ("journal.jsonl", journal),
    ):
        (inputs / name).write_text(
            "".join(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n" for row in rows)
        )
    for path in inputs.iterdir():
        os.chmod(path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    build(args.root)
