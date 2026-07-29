#!/usr/bin/env python3
from __future__ import annotations

import argparse
import io
import json
import os
import random
import shutil
import struct
import unicodedata
import zipfile
from datetime import datetime, timezone
from pathlib import Path


PROTOCOL = {
    "protocol_version": 1,
    "path_model": {
        "root": "/app/corpus",
        "physical_paths": "POSIX paths relative to /app/corpus, preserved exactly",
        "archive_separator": "!",
        "archive_entries": "append ! and the ZIP entry name at each nesting level",
        "ordering": "compare the UTF-8 bytes of the exact relative path",
        "path_depth": "count non-empty components after splitting the exact path at both / and !",
        "symlinks": "inventory but never follow",
        "hardlinks": "the UTF-8-byte-smallest path for each (device,inode) is canonical",
    },
    "archive": {
        "designated_media_type": "zip",
        "maximum_depth": 3,
        "depth_definition": "a physical corpus ZIP is depth 0; each non-directory member has its containing ZIP's depth plus 1, so a physical ZIP's direct members are depth 1",
        "depth_boundary": "examine members whose depth is at most maximum_depth; inventory a member above maximum_depth as DEPTH_LIMIT with media type unexamined and do not read or expand it",
        "ooxml_is_atomic": True,
        "entry_uncompressed_limit_bytes": 16777216,
        "total_uncompressed_limit_bytes_per_container": 67108864,
        "size_limit_boundary": "use each ZIP member's declared uncompressed size and the running sum in exact entry-path UTF-8 byte order; a member that makes either limit exceed its value is DEPTH_LIMIT with media type unexamined and is not read or expanded",
    },
    "signature_precedence": [
        {"media_type": "jpeg", "hex_prefix": "ffd8ff"},
        {"media_type": "pdf", "hex_prefix": "255044462d"},
        {"media_type": "utf16le_text", "hex_prefix": "fffe"},
        {"media_type": "zip_or_ooxml", "hex_prefix": "504b0304"},
        {"media_type": "utf8_text", "hex_prefix": "efbbbf"},
        {"media_type": "unsupported", "hex_prefix": "00010203"},
        {"media_type": "text", "fallback": "UTF-8, then Latin-1, with no NUL or disallowed C0 controls"},
    ],
    "ooxml_detection": {
        "required_entry": "[Content_Types].xml",
        "word_prefix": "word/",
        "spreadsheet_prefix": "xl/",
        "media_types": {"word": "docx", "spreadsheet": "xlsx"},
    },
    "effective_date": {
        "window_timezone": "America/New_York",
        "window_start_local": "2024-03-10T01:30:00",
        "window_start_fold": 0,
        "window_end_local": "2024-11-03T01:30:00",
        "window_end_fold": 1,
        "inclusive": True,
        "pdf_field": "/ModDate",
        "ooxml_field": "docProps/core.xml dcterms:modified",
        "jpeg_field": "EXIF DateTimeOriginal (0x9003), with OffsetTimeOriginal (0x9011) when present",
        "naive_embedded_timezone": "America/New_York",
        "naive_embedded_fold": 0,
        "fallback": "filesystem mtime; archive children inherit their immediate container effective date",
        "output_format": "UTC RFC3339 seconds with Z",
    },
    "content": {
        "keywords": ["merger", "project atlas", "café"],
        "privilege_terms": ["attorney-client", "legal advice", "work product"],
        "matching": "Unicode NFC casefold; phrase ends must not touch a Unicode alphanumeric or underscore",
        "pdf_text": "bytes between stream LF and LF endstream, decoded UTF-8 then Latin-1",
        "docx_text": "all XML character data in word/document.xml",
        "xlsx_text": "all XML character data in xl/sharedStrings.xml",
        "jpeg_text": "EXIF ImageDescription (0x010e), or empty when absent",
    },
    "normalized_hash": {
        "algorithm": "sha256 lowercase hex",
        "text_media_types": ["text", "utf8_text", "utf16le_text"],
        "text_rule": "decode according to media type after removing the UTF-8 or UTF-16LE signature BOM, NFC-normalize, convert CRLF/CR to LF, strip trailing space and tab from every line, UTF-8 encode",
        "binary_rule": "hash exact bytes",
    },
    "families": {
        "manifest": "/app/input/families.json",
        "unlisted": "SINGLE:: followed by the exact corpus path",
        "privilege_propagation": "an item's privilege term withholds every non-technical member of its family",
    },
    "disposition_precedence": [
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
    ],
    "technical_exclusion_media_types": {
        "SYMLINK": "symlink",
        "HARDLINK_ALIAS": "hardlink",
        "DEPTH_LIMIT": "unexamined",
        "UNSUPPORTED_TYPE": "unsupported",
        "CONTAINER": "zip",
    },
    "deduplication": {
        "scope": "all remaining responsive items",
        "key": "normalized_hash",
        "representative_order": [
            "custodian rank ascending",
            "effective UTC date ascending",
            "path depth ascending",
            "exact path UTF-8 bytes ascending",
        ],
        "duplicate_of": "path of the elected representative",
    },
    "production": {
        "volume_limit_bytes": 32768,
        "packing": "bundle produced items by family; order bundles by their earliest item key, order within a bundle by the same key, then next-fit whole bundles",
        "item_order_key": ["effective UTC date ascending", "exact path UTF-8 bytes ascending"],
        "volume_format": "VOL- followed by a three-digit 1-based number",
        "sequence_format": "PROD- followed by a six-digit 1-based number",
    },
}

CUSTODIANS = {
    "custodians": [
        {"path_prefix": "Avery", "custodian": "Avery Quinn", "rank": 1},
        {"path_prefix": "Blake", "custodian": "Blake Rowan", "rank": 2},
        {"path_prefix": "Casey", "custodian": "Casey Morgan", "rank": 3},
        {"path_prefix": "Shared", "custodian": "Shared Drive", "rank": 4},
    ]
}


def set_mtime(path: Path, iso_utc: str) -> None:
    stamp = datetime.fromisoformat(iso_utc.replace("Z", "+00:00")).timestamp()
    os.utime(path, (stamp, stamp), follow_symlinks=False)


def text_bytes(text: str, encoding: str) -> bytes:
    if encoding == "utf16le":
        return b"\xff\xfe" + text.encode("utf-16le")
    if encoding == "utf8bom":
        return b"\xef\xbb\xbf" + text.encode("utf-8")
    return text.encode(encoding)


def minimal_pdf(text: str, mod_date: str) -> bytes:
    stream = text.encode("utf-8")
    return (
        b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n"
        + f"2 0 obj\n<< /ModDate ({mod_date}) /Length {len(stream)} >>\nstream\n".encode()
        + stream
        + b"\nendstream\nendobj\n%%EOF\n"
    )


def exif_jpeg(description: str, date_time: str, offset: str | None = None) -> bytes:
    # A compact little-endian TIFF with ImageDescription and an Exif sub-IFD.
    desc = description.encode("ascii") + b"\0"
    dt = date_time.encode("ascii") + b"\0"
    off = (offset.encode("ascii") + b"\0") if offset else b""
    ifd0_offset = 8
    ifd0_len = 2 + 2 * 12 + 4
    exif_offset = ifd0_offset + ifd0_len
    exif_count = 2 if offset else 1
    exif_len = 2 + exif_count * 12 + 4
    data_offset = exif_offset + exif_len
    desc_offset = data_offset
    dt_offset = desc_offset + len(desc)
    off_offset = dt_offset + len(dt)
    tiff = bytearray(b"II*\x00" + struct.pack("<I", ifd0_offset))
    tiff += struct.pack("<H", 2)
    tiff += struct.pack("<HHII", 0x010E, 2, len(desc), desc_offset)
    tiff += struct.pack("<HHII", 0x8769, 4, 1, exif_offset)
    tiff += struct.pack("<I", 0)
    tiff += struct.pack("<H", exif_count)
    tiff += struct.pack("<HHII", 0x9003, 2, len(dt), dt_offset)
    if offset:
        tiff += struct.pack("<HHII", 0x9011, 2, len(off), off_offset)
    tiff += struct.pack("<I", 0)
    tiff += desc + dt + off
    payload = b"Exif\0\0" + bytes(tiff)
    return b"\xff\xd8\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload + b"\xff\xd9"


def ooxml(kind: str, text: str, modified: str) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
        def add(name: str, value: str) -> None:
            info = zipfile.ZipInfo(name, (2024, 5, 20, 12, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, value)

        add("[Content_Types].xml", "<Types/>")
        add(
            "docProps/core.xml",
            (
                '<cp:coreProperties xmlns:cp="x" xmlns:dcterms="http://purl.org/dc/terms/">'
                f"<dcterms:modified>{modified}</dcterms:modified></cp:coreProperties>"
            ),
        )
        if kind == "word":
            add(
                "word/document.xml",
                f'<w:document xmlns:w="w"><w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>',
            )
        else:
            add(
                "xl/sharedStrings.xml",
                f'<sst xmlns="s"><si><t>{text}</t></si></sst>',
            )
    return output.getvalue()


def make_zip(entries: list[tuple[str, bytes]]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries:
            info = zipfile.ZipInfo(name, (2024, 5, 20, 12, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data)
    return output.getvalue()


def build(root: Path) -> None:
    corpus = root / "corpus"
    inputs = root / "input"
    if corpus.exists():
        shutil.rmtree(corpus)
    if inputs.exists():
        shutil.rmtree(inputs)
    corpus.mkdir(parents=True)
    inputs.mkdir(parents=True)
    families: dict[str, str] = {}

    def write(rel: str, data: bytes, mtime: str = "2024-05-20T12:00:00Z", family: str | None = None) -> Path:
        path = corpus / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        set_mtime(path, mtime)
        if family:
            families[rel] = family
        return path

    # Hand-crafted cases exercise misleading extensions, embedded dates, encodings,
    # family privilege, duplicate re-election, and filename safety.
    write("Avery/contracts/master.txt", text_bytes("Project Atlas merger terms  \r\n", "utf-8"), family="FAM-PRIV-01")
    write("Blake/mail/attachment.txt", text_bytes("Attorney-client legal advice about the merger\n", "utf-8"), family="FAM-PRIV-01")
    write("Casey/copies/master-copy.txt", text_bytes("Project Atlas merger terms\n", "utf-8"), family="FAM-PRIV-01")

    write("Avery/finance/forecast.doc", ooxml("spreadsheet", "café merger forecast", "2024-03-10T07:00:00Z"), family="FAM-FIN-02")
    write("Blake/finance/forecast-copy.xlsx", ooxml("spreadsheet", "café merger forecast", "2024-03-10T07:00:00Z"), family="FAM-FIN-02")
    write("Shared/finance/cover.txt", text_bytes("Project Atlas cover note", "latin-1"), family="FAM-FIN-02")

    write("Avery/mail/edge-start.bin", minimal_pdf("merger at spring edge", "D:20240310013000-05'00'"))
    write("Avery/mail/edge-before.bin", minimal_pdf("merger just too early", "D:20240310012959-05'00'"))
    write("Blake/mail/fall-fold.doc", minimal_pdf("Project Atlas closing", "D:20241103013000-05'00'"))
    write("Blake/mail/fall-too-late.doc", minimal_pdf("Project Atlas closing", "D:20241103013001-05'00'"))
    write("Casey/notes/utf16-only.txt", text_bytes("The MERGER ledger is responsive.\r\n", "utf16le"))
    write("Casey/notes/latin1-café.txt", text_bytes("Budget for CAFÉ vendors and project atlas.\r\n", "latin-1"))
    write("Shared/notes/not-a-token.txt", text_bytes("mergerish and preproject atlasx are noise\n", "utf-8"))
    write("Shared/odd/weird ;$name\nline.txt", text_bytes("project atlas survives unsafe shell pipelines\n", "utf-8"))
    write("Shared/unicode/café.txt", text_bytes("merger NFC filename\n", "utf-8"))
    write("Shared/unicode/" + unicodedata.normalize("NFD", "café") + ".txt", text_bytes("merger NFD filename\n", "utf-8"))
    write("Shared/case/CASE.txt", text_bytes("merger uppercase path\n", "utf-8"))
    write("Shared/case/case.txt", text_bytes("merger lowercase path\n", "utf-8"))

    write("Avery/images/scan.pdf", exif_jpeg("project atlas photograph", "2024:03:10 03:15:00", "-04:00"))
    write("Blake/images/old.jpg", exif_jpeg("merger legacy board", "2024:03:10 01:00:00"))
    poly_zip = make_zip([("hidden.txt", b"merger must not be expanded")])
    write("Casey/images/polyglot.zip", exif_jpeg("ordinary photograph", "2024:06:01 10:00:00") + poly_zip)

    archive_level4 = make_zip([("too-deep.txt", b"merger beyond depth"), ("also.bin", b"\x00\x01\x02")])
    archive_level3 = make_zip([("inside-level3.txt", b"merger depth three\n"), ("level4.zip", archive_level4)])
    archive_level2 = make_zip([("inside-level2.txt", text_bytes("project atlas nested child\n", "utf-8")), ("level3.zip", archive_level3)])
    archive_level1 = make_zip(
        [
            ("mail body.txt", text_bytes("merger archive parent\n", "utf-8")),
            ("attachment-utf16.txt", text_bytes("legal advice merger\n", "utf16le")),
            ("level2.zip", archive_level2),
        ]
    )
    write("Avery/mail/export.zip", archive_level1, family="FAM-ARCH-03")
    families.update(
        {
            "Avery/mail/export.zip!mail body.txt": "FAM-ARCH-03",
            "Avery/mail/export.zip!attachment-utf16.txt": "FAM-ARCH-03",
            "Avery/mail/export.zip!level2.zip": "FAM-ARCH-03",
            "Avery/mail/export.zip!level2.zip!inside-level2.txt": "FAM-ARCH-03",
            "Avery/mail/export.zip!level2.zip!level3.zip": "FAM-ARCH-03",
            "Avery/mail/export.zip!level2.zip!level3.zip!inside-level3.txt": "FAM-ARCH-03",
            "Avery/mail/export.zip!level2.zip!level3.zip!level4.zip": "FAM-ARCH-03",
            "Avery/mail/export.zip!level2.zip!level3.zip!level4.zip!too-deep.txt": "FAM-ARCH-03",
            "Avery/mail/export.zip!level2.zip!level3.zip!level4.zip!also.bin": "FAM-ARCH-03",
        }
    )

    atomic_doc = ooxml("word", "project atlas atomic OOXML", "2024-05-01T10:00:00Z")
    write("Blake/mail/report.zip", atomic_doc)
    outer = make_zip([("renamed-office.bin", atomic_doc), ("plain.txt", b"merger in container\n")])
    write("Casey/mail/mixed.zip", outer)

    # A hardlinked pair and several symlink hazards.
    canonical = write("Avery/hardlinks/canonical.txt", b"merger hardlink content\n")
    alias = corpus / "Blake/hardlinks/alias.txt"
    alias.parent.mkdir(parents=True, exist_ok=True)
    os.link(canonical, alias)
    families["Avery/hardlinks/canonical.txt"] = "FAM-HARD-04"
    families["Blake/hardlinks/alias.txt"] = "FAM-HARD-04"
    syms = corpus / "Shared/symlinks"
    syms.mkdir(parents=True)
    os.symlink("../../Avery/contracts/master.txt", syms / "inside")
    os.symlink("/etc/passwd", syms / "outside")
    os.symlink("cycle-b", syms / "cycle-a")
    os.symlink("cycle-a", syms / "cycle-b")

    # This sparse file must be classified before hashing or reading to EOF.
    sparse = write("Shared/large/sparse-evidence.bin", b"\x00\x01\x02\x03")
    with sparse.open("r+b") as fh:
        fh.truncate(67_108_865)
    set_mtime(sparse, "2024-05-20T12:00:00Z")

    rng = random.Random(850453)
    custodian_dirs = ["Avery", "Blake", "Casey", "Shared"]
    encodings = ["utf-8", "utf8bom", "utf16le", "latin-1"]
    dates = [
        "2024-02-01T11:00:00Z",
        "2024-03-10T06:30:00Z",
        "2024-05-20T12:00:00Z",
        "2024-11-03T06:30:00Z",
        "2024-12-01T09:00:00Z",
    ]
    for i in range(1800):
        cust = custodian_dirs[i % 4]
        rel = f"{cust}/bulk/batch-{i // 100:02d}/record-{i:04d}.dat"
        mode = i % 12
        if mode == 0:
            data = text_bytes(f"routine nonresponsive status {i}\n", encodings[i % 4])
        elif mode == 1:
            data = text_bytes(f"merger analysis record {i}\n", encodings[i % 4])
        elif mode == 2:
            data = text_bytes(f"PROJECT ATLAS forecast {i}  \r\n", encodings[i % 4])
        elif mode == 3:
            data = text_bytes(f"attorney-client merger work product {i}\n", encodings[i % 4])
        elif mode == 4:
            data = b"\x00\x01\x02\x03" + rng.randbytes(48)
        elif mode == 5:
            data = minimal_pdf(f"café acquisition record {i}", "D:20240520120000Z")
        elif mode == 6:
            data = ooxml("word", f"project atlas document {i}", "2024-05-20T12:00:00Z")
        elif mode == 7:
            data = text_bytes(f"mergerish record {i}\n", encodings[i % 4])
        elif mode == 8:
            data = text_bytes(f"café vendor record {i}\n", encodings[i % 4])
        elif mode == 9:
            data = text_bytes("shared duplicate merger payload\n", encodings[i % 4])
        elif mode == 10:
            data = text_bytes(f"legal advice unrelated record {i}\n", encodings[i % 4])
        else:
            data = text_bytes(f"project atlas confidential work product {i}\n", encodings[i % 4])
        family = f"BULK-FAM-{i // 30:04d}" if i % 30 < 4 else None
        write(rel, data, dates[(i * 7) % len(dates)], family)

    (inputs / "protocol.json").write_text(json.dumps(PROTOCOL, indent=2, ensure_ascii=False) + "\n")
    (inputs / "custodians.json").write_text(json.dumps(CUSTODIANS, indent=2) + "\n")
    (inputs / "families.json").write_text(
        json.dumps({"path_to_family": dict(sorted(families.items()))}, indent=2, ensure_ascii=False) + "\n"
    )
    for path in inputs.iterdir():
        set_mtime(path, "2025-01-01T00:00:00Z")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("/app"))
    args = parser.parse_args()
    build(args.root)
