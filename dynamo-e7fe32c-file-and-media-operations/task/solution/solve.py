#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections import Counter, defaultdict
from pathlib import Path

import cv2
import numpy as np

from font_model import (
    build_variable_font,
    geometry_for,
    render_proof,
    write_sources_zip,
    write_specimen_pdf,
)


APP = Path(os.environ.get("FONT_REVIVAL_APP_ROOT", "/app"))
INPUT = APP / "input"
EVIDENCE = APP / "evidence"
OUTPUT = APP / "output"


def decode_axis(record: dict, ticket: dict) -> dict[str, float]:
    samples = np.asarray(record["calibration_patch_samples"], dtype=np.float64)
    encoded = np.median(samples, axis=0)
    axis_specs = {axis["tag"]: axis for axis in ticket["font_contract"]["axes"]}
    estimate = {}
    for index, tag in enumerate(("wght", "wdth", "opsz")):
        axis = axis_specs[tag]
        estimate[tag] = axis["minimum"] + (axis["maximum"] - axis["minimum"]) * encoded[index]
    locations = ticket["evidence_contract"]["capture_locations"]
    scales = np.asarray(
        [axis_specs[tag]["maximum"] - axis_specs[tag]["minimum"] for tag in ("wght", "wdth", "opsz")]
    )
    vector = np.asarray([estimate[tag] for tag in ("wght", "wdth", "opsz")])
    return min(
        locations,
        key=lambda item: float(
            np.linalg.norm(
                (vector - np.asarray([item[tag] for tag in ("wght", "wdth", "opsz")])) / scales
            )
        ),
    )


def fit_image_to_page(record: dict) -> np.ndarray:
    image_points = np.asarray([item["image_px"] for item in record["fiducials"]], dtype=np.float64)
    page_points = np.asarray([item["page_px"] for item in record["fiducials"]], dtype=np.float64)
    matrix, mask = cv2.findHomography(
        image_points,
        page_points,
        method=cv2.RANSAC,
        ransacReprojThreshold=0.65,
        maxIters=10000,
        confidence=0.9999,
    )
    if matrix is None or mask is None or int(mask.sum()) < 18:
        raise RuntimeError(f"registration failed for {record['id']}")
    matrix = matrix / matrix[2, 2]
    return matrix


def recover_patterns(
    ticket: dict,
    index: dict,
) -> tuple[dict[str, list[str]], list[dict]]:
    page_width = ticket["evidence_contract"]["page_width_px"]
    page_height = ticket["evidence_contract"]["page_height_px"]
    font_size = ticket["evidence_contract"]["font_size_px"]
    scale = font_size / ticket["font_contract"]["units_per_em"]
    glyph_specs = {item["name"]: item for item in ticket["font_contract"]["glyphs"]}
    slots_by_layout: dict[str, list[dict]] = defaultdict(list)
    for slot in ticket["evidence_contract"]["page_slots"]:
        slots_by_layout[slot["layout_id"]].append(slot)
    samples: dict[tuple[str, int, int], list[float]] = defaultdict(list)
    captures = []

    for record in index["observations"]:
        location = dict(decode_axis(record, ticket))
        matrix = fit_image_to_page(record)
        evidence_path = APP / Path(record["file"]).relative_to("/app")
        image = cv2.imread(str(evidence_path), cv2.IMREAD_GRAYSCALE)
        if image is None:
            raise FileNotFoundError(record["file"])
        rectified = cv2.warpPerspective(
            image,
            matrix,
            (page_width, page_height),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=247,
        )
        for slot in slots_by_layout[record["layout_id"]]:
            spec = glyph_specs[slot["glyph"]]
            geom = geometry_for(spec, location, ticket)
            for row in range(spec["rows"]):
                for column in range(spec["columns"]):
                    page_x = slot["origin_x_px"] + (
                        geom["first_center_x"] + column * geom["pitch_x"]
                    ) * scale
                    page_y = slot["baseline_y_px"] - (
                        geom["top_center_y"] - row * geom["pitch_y"]
                    ) * scale
                    x = int(round(page_x))
                    y = int(round(page_y))
                    patch = rectified[max(0, y - 2) : y + 3, max(0, x - 2) : x + 3]
                    samples[(slot["glyph"], row, column)].append(float(np.median(patch)))
        reported = record["reported_axis_untrusted"]
        captures.append(
            {
                "id": record["id"],
                "file": record["file"],
                "layout_id": record["layout_id"],
                "axis_location": location,
                "reported_axis_status": "correct" if reported == location else "mislabelled",
                "image_to_page_homography": matrix.tolist(),
            }
        )

    patterns = {}
    for glyph_name in ticket["font_contract"]["glyph_order"]:
        spec = glyph_specs[glyph_name]
        rows = []
        for row in range(spec["rows"]):
            bits = []
            for column in range(spec["columns"]):
                values = samples[(glyph_name, row, column)]
                if not values:
                    raise RuntimeError(f"no samples for {glyph_name} cell {row},{column}")
                bits.append("1" if float(np.median(values)) < 145.0 else "0")
            rows.append("".join(bits))
        patterns[glyph_name] = rows
    return patterns, captures


def recover_open_type(ticket: dict, probes: dict) -> tuple[list[dict], dict, list[dict]]:
    kerning = []
    for item in probes["kerning"]:
        value = int(round(float(np.median(np.asarray(item["measured_adjustment_units"], dtype=np.float64)))))
        kerning.append({"left": item["left"], "right": item["right"], "value": value})

    bases = {}
    mark = None
    for item in probes["anchors"]:
        recovered = np.rint(
            np.median(np.asarray(item["measured_parameter_samples"], dtype=np.float64), axis=0)
        ).astype(int)
        if item["kind"] == "base":
            bases[item["glyph"]] = {"x_offset": int(recovered[0]), "y": int(recovered[1])}
        else:
            mark = {"glyph": item["glyph"], "x": int(recovered[0]), "y": int(recovered[1])}
    if mark is None:
        raise RuntimeError("mark anchor probe missing")
    anchors = {"mark": mark, "bases": bases}

    ligatures = []
    for item in probes["ligatures"]:
        output = Counter(item["output_votes"]).most_common(1)[0][0]
        ligatures.append({"feature": item["feature"], "input": item["input"], "output": output})
    return kerning, anchors, ligatures


def pattern_digest(rows: list[str]) -> str:
    return hashlib.sha256("\n".join(rows).encode()).hexdigest()


def build_manifest(
    ticket: dict,
    patterns: dict[str, list[str]],
    kerning: list[dict],
    anchors: dict,
    ligatures: list[dict],
    captures: list[dict],
) -> dict:
    anchor_rows = [
        {
            "glyph": glyph,
            "kind": "base",
            "x_offset": item["x_offset"],
            "y": item["y"],
        }
        for glyph, item in anchors["bases"].items()
    ]
    anchor_rows.append(
        {
            "glyph": anchors["mark"]["glyph"],
            "kind": "mark",
            "x": anchors["mark"]["x"],
            "y": anchors["mark"]["y"],
        }
    )
    return {
        "schema_version": 1,
        "family": {
            "name": ticket["font_contract"]["family_name"],
            "units_per_em": ticket["font_contract"]["units_per_em"],
        },
        "axes": [
            {
                "tag": axis["tag"],
                "minimum": axis["minimum"],
                "default": axis["default"],
                "maximum": axis["maximum"],
            }
            for axis in ticket["font_contract"]["axes"]
        ],
        "glyphs": [
            {"name": name, "pattern_sha256": pattern_digest(patterns[name])}
            for name in ticket["font_contract"]["glyph_order"]
        ],
        "open_type": {
            "kerning": kerning,
            "anchors": anchor_rows,
            "ligatures": ligatures,
        },
        "captures": sorted(captures, key=lambda item: item["id"]),
        "pdf": {
            "title": ticket["pdf_contract"]["title"],
            "subject": ticket["pdf_contract"]["subject"],
        },
    }


def main() -> None:
    ticket = json.loads((INPUT / "job_ticket.json").read_text())
    index = json.loads((EVIDENCE / "index.json").read_text())
    probes = json.loads((EVIDENCE / "layout_probes.json").read_text())
    patterns, captures = recover_patterns(ticket, index)
    kerning, anchors, ligatures = recover_open_type(ticket, probes)

    shutil.rmtree(OUTPUT, ignore_errors=True)
    OUTPUT.mkdir(parents=True)
    font_path = OUTPUT / "recovered.ttf"
    build_variable_font(font_path, patterns, ticket, kerning, anchors, ligatures)
    write_sources_zip(OUTPUT / "sources.zip", patterns, ticket, kerning, anchors, ligatures)
    manifest = build_manifest(ticket, patterns, kerning, anchors, ligatures, captures)
    (OUTPUT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    proof_path = OUTPUT / "proof.png"
    render_proof(font_path, ticket).save(proof_path)
    write_specimen_pdf(OUTPUT / "specimen.pdf", proof_path, font_path, ticket)


if __name__ == "__main__":
    main()
