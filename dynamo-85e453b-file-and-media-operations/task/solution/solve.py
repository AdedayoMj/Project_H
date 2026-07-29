#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import struct
import unicodedata
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import xml.etree.ElementTree as ET


APP_ROOT = Path(os.environ.get("ESI_APP_ROOT", "/app"))
ROOT = APP_ROOT / "corpus"
INPUT = APP_ROOT / "input"
OUTPUT = APP_ROOT / "output.json"
TECHNICAL = {"SYMLINK", "HARDLINK_ALIAS", "DEPTH_LIMIT", "UNSUPPORTED_TYPE", "CONTAINER"}
ALL_DISPOSITIONS = [
    "SYMLINK",
    "HARDLINK_ALIAS",
    "DEPTH_LIMIT",
    "UNSUPPORTED_TYPE",
    "CONTAINER",
    "PRIVILEGED",
    "FAMILY_PRIVILEGE",
    "OUTSIDE_DATE",
    "NOT_RESPONSIVE",
    "DUPLICATE",
    "PRODUCED",
]


@dataclass
class Item:
    path: str
    size: int
    custodian: str
    rank: int
    family: str
    media: str = "unknown"
    data: bytes | None = None
    physical: Path | None = None
    inherited_date: datetime | None = None
    effective: datetime | None = None
    normalized_hash: str | None = None
    responsive: bool = False
    privileged: bool = False
    disposition: str | None = None
    duplicate_of: str | None = None
    trigger_paths: list[str] = field(default_factory=list)


def path_key(value: str) -> bytes:
    return value.encode("utf-8")


def decode_text(data: bytes, media: str) -> str:
    if media == "utf16le_text":
        return data[2:].decode("utf-16le")
    if media == "utf8_text":
        return data[3:].decode("utf-8")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def classify(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"%PDF-"):
        return "pdf"
    if data.startswith(b"\xff\xfe"):
        try:
            data[2:].decode("utf-16le")
            return "utf16le_text"
        except UnicodeError:
            return "unsupported"
    if data.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = zf.namelist()
            if "[Content_Types].xml" in names:
                if any(name.startswith("word/") for name in names):
                    return "docx"
                if any(name.startswith("xl/") for name in names):
                    return "xlsx"
            return "zip"
        except (zipfile.BadZipFile, OSError):
            return "unsupported"
    if data.startswith(b"\xef\xbb\xbf"):
        try:
            data[3:].decode("utf-8")
            return "utf8_text"
        except UnicodeError:
            return "unsupported"
    if data.startswith(b"\x00\x01\x02\x03"):
        return "unsupported"
    if b"\x00" in data:
        return "unsupported"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    if any(ord(ch) < 32 and ch not in "\t\n\r" for ch in text):
        return "unsupported"
    return "text"


def _exif_fields(data: bytes) -> dict[int, str]:
    if not data.startswith(b"\xff\xd8"):
        return {}
    pos = 2
    payload = None
    while pos + 4 <= len(data) and data[pos] == 0xFF:
        marker = data[pos + 1]
        if marker in (0xD8, 0xD9):
            pos += 2
            continue
        length = struct.unpack(">H", data[pos + 2 : pos + 4])[0]
        segment = data[pos + 4 : pos + 2 + length]
        if marker == 0xE1 and segment.startswith(b"Exif\0\0"):
            payload = segment[6:]
            break
        pos += 2 + length
    if payload is None or len(payload) < 8:
        return {}
    endian = "<" if payload[:2] == b"II" else ">"
    if payload[:2] not in (b"II", b"MM"):
        return {}

    def u16(offset: int) -> int:
        return struct.unpack_from(endian + "H", payload, offset)[0]

    def u32(offset: int) -> int:
        return struct.unpack_from(endian + "I", payload, offset)[0]

    def parse_ifd(offset: int) -> tuple[dict[int, str], int | None]:
        values: dict[int, str] = {}
        exif_ptr = None
        if offset < 0 or offset + 2 > len(payload):
            return values, exif_ptr
        count = u16(offset)
        for index in range(count):
            entry = offset + 2 + 12 * index
            if entry + 12 > len(payload):
                break
            tag, kind = struct.unpack_from(endian + "HH", payload, entry)
            amount = u32(entry + 4)
            raw_offset = entry + 8 if amount <= 4 else u32(entry + 8)
            if tag == 0x8769:
                exif_ptr = u32(entry + 8)
            if kind == 2 and raw_offset + amount <= len(payload):
                values[tag] = payload[raw_offset : raw_offset + amount].rstrip(b"\0").decode("ascii", "strict")
        return values, exif_ptr

    root_offset = u32(4)
    values, exif_ptr = parse_ifd(root_offset)
    if exif_ptr is not None:
        nested, _ = parse_ifd(exif_ptr)
        values.update(nested)
    return values


def extract_text(data: bytes, media: str) -> str:
    if media in {"text", "utf8_text", "utf16le_text"}:
        return decode_text(data, media)
    if media == "pdf":
        chunks = re.findall(rb"stream\n(.*?)\nendstream", data, re.S)
        output = []
        for chunk in chunks:
            try:
                output.append(chunk.decode("utf-8"))
            except UnicodeDecodeError:
                output.append(chunk.decode("latin-1"))
        return "\n".join(output)
    if media in {"docx", "xlsx"}:
        member = "word/document.xml" if media == "docx" else "xl/sharedStrings.xml"
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                root = ET.fromstring(zf.read(member))
            return " ".join(part for part in root.itertext())
        except (KeyError, ET.ParseError, zipfile.BadZipFile):
            return ""
    if media == "jpeg":
        return _exif_fields(data).get(0x010E, "")
    return ""


def parse_pdf_date(data: bytes) -> datetime | None:
    match = re.search(rb"/ModDate\s*\(D:(\d{14})(Z|[+-]\d{2}'?\d{2}'?)?\)", data)
    if not match:
        return None
    base = datetime.strptime(match.group(1).decode(), "%Y%m%d%H%M%S")
    suffix = (match.group(2) or b"").decode()
    if suffix == "Z":
        return base.replace(tzinfo=timezone.utc)
    if suffix:
        sign = 1 if suffix[0] == "+" else -1
        digits = suffix[1:].replace("'", "")
        offset = timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
        return base.replace(tzinfo=timezone(sign * offset)).astimezone(timezone.utc)
    return base.replace(tzinfo=ZoneInfo("America/New_York"), fold=0).astimezone(timezone.utc)


def parse_ooxml_date(data: bytes) -> datetime | None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            root = ET.fromstring(zf.read("docProps/core.xml"))
        for element in root.iter():
            if element.tag.endswith("modified") and element.text:
                value = element.text.strip().replace("Z", "+00:00")
                parsed = datetime.fromisoformat(value)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=ZoneInfo("America/New_York"), fold=0)
                return parsed.astimezone(timezone.utc)
    except (KeyError, ValueError, ET.ParseError, zipfile.BadZipFile):
        return None
    return None


def parse_jpeg_date(data: bytes) -> datetime | None:
    try:
        fields = _exif_fields(data)
        base = datetime.strptime(fields[0x9003], "%Y:%m:%d %H:%M:%S")
        offset = fields.get(0x9011)
        if offset:
            sign = 1 if offset[0] == "+" else -1
            delta = timedelta(hours=int(offset[1:3]), minutes=int(offset[4:6]))
            return base.replace(tzinfo=timezone(sign * delta)).astimezone(timezone.utc)
        return base.replace(tzinfo=ZoneInfo("America/New_York"), fold=0).astimezone(timezone.utc)
    except (KeyError, ValueError):
        return None


def effective_date(item: Item) -> datetime:
    assert item.data is not None
    embedded = None
    if item.media == "pdf":
        embedded = parse_pdf_date(item.data)
    elif item.media in {"docx", "xlsx"}:
        embedded = parse_ooxml_date(item.data)
    elif item.media == "jpeg":
        embedded = parse_jpeg_date(item.data)
    if embedded is not None:
        return embedded
    if item.inherited_date is not None:
        return item.inherited_date
    assert item.physical is not None
    return datetime.fromtimestamp(item.physical.stat().st_mtime, timezone.utc)


def contains_term(text: str, term: str) -> bool:
    normalized = unicodedata.normalize("NFC", text).casefold()
    needle = unicodedata.normalize("NFC", term).casefold()
    start = 0
    while True:
        at = normalized.find(needle, start)
        if at < 0:
            return False
        before = normalized[at - 1] if at else ""
        end = at + len(needle)
        after = normalized[end] if end < len(normalized) else ""
        if not (before and (before.isalnum() or before == "_")) and not (
            after and (after.isalnum() or after == "_")
        ):
            return True
        start = at + 1


def normalized_hash(item: Item) -> str:
    assert item.data is not None
    if item.media in {"text", "utf8_text", "utf16le_text"}:
        value = unicodedata.normalize("NFC", decode_text(item.data, item.media))
        value = value.replace("\r\n", "\n").replace("\r", "\n")
        value = "\n".join(line.rstrip(" \t") for line in value.split("\n"))
        payload = value.encode("utf-8")
    else:
        payload = item.data
    return hashlib.sha256(payload).hexdigest()


def corpus_inventory() -> tuple[list[tuple[str, Path, os.stat_result]], list[tuple[str, Path, os.stat_result]]]:
    regular: list[tuple[str, Path, os.stat_result]] = []
    links: list[tuple[str, Path, os.stat_result]] = []

    def walk(directory: Path) -> None:
        entries = sorted(os.scandir(directory), key=lambda entry: path_key(entry.name))
        for entry in entries:
            full = Path(entry.path)
            info = entry.stat(follow_symlinks=False)
            rel = full.relative_to(ROOT).as_posix()
            if stat.S_ISLNK(info.st_mode):
                links.append((rel, full, info))
            elif stat.S_ISDIR(info.st_mode):
                walk(full)
            elif stat.S_ISREG(info.st_mode):
                regular.append((rel, full, info))

    walk(ROOT)
    return regular, links


def main() -> None:
    protocol = json.loads((INPUT / "protocol.json").read_text())
    roster = json.loads((INPUT / "custodians.json").read_text())["custodians"]
    family_map = json.loads((INPUT / "families.json").read_text())["path_to_family"]
    cust_by_prefix = {entry["path_prefix"]: (entry["custodian"], entry["rank"]) for entry in roster}

    def attributes(path: str) -> tuple[str, int, str]:
        prefix = path.split("/", 1)[0]
        custodian, rank = cust_by_prefix[prefix]
        return custodian, rank, family_map.get(path, "SINGLE::" + path)

    items: list[Item] = []
    regular, links = corpus_inventory()
    inode_paths: dict[tuple[int, int], list[str]] = defaultdict(list)
    for rel, _, info in regular:
        inode_paths[(info.st_dev, info.st_ino)].append(rel)
    canonical = {
        identity: min(paths, key=path_key)
        for identity, paths in inode_paths.items()
    }

    for rel, _, info in links:
        custodian, rank, family = attributes(rel)
        items.append(Item(rel, info.st_size, custodian, rank, family, media="symlink", disposition="SYMLINK"))

    max_depth = protocol["archive"]["maximum_depth"]
    entry_limit = protocol["archive"]["entry_uncompressed_limit_bytes"]
    total_limit = protocol["archive"]["total_uncompressed_limit_bytes_per_container"]

    def add_atomic(
        path: str,
        data: bytes,
        *,
        physical: Path | None,
        inherited: datetime | None,
        archive_depth: int,
    ) -> Item:
        custodian, rank, family = attributes(path)
        item = Item(path, len(data), custodian, rank, family, data=data, physical=physical, inherited_date=inherited)
        if archive_depth > max_depth:
            item.media = "unexamined"
            item.disposition = "DEPTH_LIMIT"
            item.data = None
            items.append(item)
            return item
        item.media = classify(data)
        if item.media == "unsupported":
            item.disposition = "UNSUPPORTED_TYPE"
            item.data = None
            items.append(item)
            return item
        item.effective = effective_date(item)
        if item.media == "zip":
            item.disposition = "CONTAINER"
            items.append(item)
            expand(item, data, archive_depth)
            item.data = None
            return item
        text = extract_text(data, item.media)
        item.responsive = any(contains_term(text, term) for term in protocol["content"]["keywords"])
        item.privileged = any(contains_term(text, term) for term in protocol["content"]["privilege_terms"])
        item.normalized_hash = normalized_hash(item)
        item.data = None
        items.append(item)
        return item

    def expand(parent: Item, data: bytes, archive_depth: int) -> None:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            entries = [entry for entry in zf.infolist() if not entry.is_dir()]
            entries.sort(key=lambda entry: path_key(entry.filename))
            total = 0
            for entry in entries:
                child_path = parent.path + "!" + entry.filename
                custodian, rank, family = attributes(child_path)
                total += entry.file_size
                if entry.file_size > entry_limit or total > total_limit:
                    items.append(
                        Item(
                            child_path,
                            entry.file_size,
                            custodian,
                            rank,
                            family,
                            media="unexamined",
                            disposition="DEPTH_LIMIT",
                        )
                    )
                    continue
                child_data = zf.read(entry)
                add_atomic(
                    child_path,
                    child_data,
                    physical=None,
                    inherited=parent.effective,
                    archive_depth=archive_depth + 1,
                )

    for rel, full, info in regular:
        identity = (info.st_dev, info.st_ino)
        custodian, rank, family = attributes(rel)
        if canonical[identity] != rel:
            items.append(
                Item(rel, info.st_size, custodian, rank, family, media="hardlink", disposition="HARDLINK_ALIAS")
            )
            continue
        with full.open("rb") as fh:
            prefix = fh.read(8)
            if prefix.startswith(b"\x00\x01\x02\x03"):
                items.append(
                    Item(rel, info.st_size, custodian, rank, family, media="unsupported", disposition="UNSUPPORTED_TYPE")
                )
                continue
            fh.seek(0)
            data = fh.read()
        add_atomic(rel, data, physical=full, inherited=None, archive_depth=0)

    family_triggers: dict[str, list[str]] = defaultdict(list)
    for item in items:
        if item.privileged:
            family_triggers[item.family].append(item.path)
    for triggers in family_triggers.values():
        triggers.sort(key=path_key)

    zone = ZoneInfo(protocol["effective_date"]["window_timezone"])
    start = datetime.fromisoformat(protocol["effective_date"]["window_start_local"]).replace(
        tzinfo=zone, fold=protocol["effective_date"]["window_start_fold"]
    ).astimezone(timezone.utc)
    end = datetime.fromisoformat(protocol["effective_date"]["window_end_local"]).replace(
        tzinfo=zone, fold=protocol["effective_date"]["window_end_fold"]
    ).astimezone(timezone.utc)

    candidates: list[Item] = []
    for item in items:
        if item.disposition in TECHNICAL:
            continue
        if item.privileged:
            item.disposition = "PRIVILEGED"
            item.trigger_paths = family_triggers[item.family]
        elif item.family in family_triggers:
            item.disposition = "FAMILY_PRIVILEGE"
            item.trigger_paths = family_triggers[item.family]
        elif item.effective is None or not (start <= item.effective <= end):
            item.disposition = "OUTSIDE_DATE"
        elif not item.responsive:
            item.disposition = "NOT_RESPONSIVE"
        else:
            candidates.append(item)

    def depth(path: str) -> int:
        return len([part for part in re.split(r"[!/]", path) if part])

    by_hash: dict[str, list[Item]] = defaultdict(list)
    for item in candidates:
        assert item.normalized_hash is not None
        by_hash[item.normalized_hash].append(item)
    for group in by_hash.values():
        group.sort(
            key=lambda item: (
                item.rank,
                item.effective,
                depth(item.path),
                path_key(item.path),
            )
        )
        representative = group[0]
        representative.disposition = "PRODUCED"
        for duplicate in group[1:]:
            duplicate.disposition = "DUPLICATE"
            duplicate.duplicate_of = representative.path

    produced = [item for item in items if item.disposition == "PRODUCED"]

    def production_key(item: Item) -> tuple[datetime, bytes]:
        assert item.effective is not None
        return item.effective, path_key(item.path)

    bundles: dict[str, list[Item]] = defaultdict(list)
    for item in produced:
        bundles[item.family].append(item)
    ordered_bundles = []
    for family, members in bundles.items():
        members.sort(key=production_key)
        ordered_bundles.append((production_key(members[0]), family, members))
    ordered_bundles.sort(key=lambda bundle: (bundle[0], path_key(bundle[1])))

    limit = protocol["production"]["volume_limit_bytes"]
    ordered_produced: list[tuple[Item, str]] = []
    volume_number = 1
    volume_bytes = 0
    volume_stats: list[dict] = []
    for _, _, members in ordered_bundles:
        bundle_size = sum(member.size for member in members)
        if volume_bytes and volume_bytes + bundle_size > limit:
            volume_number += 1
            volume_bytes = 0
        volume = f"VOL-{volume_number:03d}"
        while len(volume_stats) < volume_number:
            volume_stats.append({"volume": f"VOL-{len(volume_stats) + 1:03d}", "bytes": 0, "items": 0})
        for member in members:
            ordered_produced.append((member, volume))
            volume_stats[-1]["bytes"] += member.size
            volume_stats[-1]["items"] += 1
        volume_bytes += bundle_size

    production = []
    for index, (item, volume) in enumerate(ordered_produced, 1):
        assert item.effective is not None and item.normalized_hash is not None
        production.append(
            {
                "sequence": f"PROD-{index:06d}",
                "path": item.path,
                "media_type": item.media,
                "custodian": item.custodian,
                "family_id": item.family,
                "normalized_hash": item.normalized_hash,
                "effective_utc_date": item.effective.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "size_bytes": item.size,
                "volume": volume,
            }
        )

    privileged = sorted(
        [
            {
                "path": item.path,
                "family_id": item.family,
                "reason": item.disposition,
                "trigger_paths": item.trigger_paths,
            }
            for item in items
            if item.disposition in {"PRIVILEGED", "FAMILY_PRIVILEGE"}
        ],
        key=lambda row: path_key(row["path"]),
    )
    exclusions = sorted(
        [
            {
                "path": item.path,
                "reason": item.disposition,
                "media_type": item.media,
                "family_id": item.family,
                "duplicate_of": item.duplicate_of,
            }
            for item in items
            if item.disposition != "PRODUCED"
        ],
        key=lambda row: path_key(row["path"]),
    )
    counts = Counter(item.disposition for item in items)
    output = {
        "production": production,
        "privilege_log": privileged,
        "exclusions": exclusions,
        "summary": {
            "inventoried": len(items),
            "produced": counts["PRODUCED"],
            "privileged": counts["PRIVILEGED"] + counts["FAMILY_PRIVILEGE"],
            "excluded": len(items) - counts["PRODUCED"],
            "by_disposition": {name: counts[name] for name in ALL_DISPOSITIONS},
            "volumes": volume_stats,
        },
    }
    OUTPUT.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
