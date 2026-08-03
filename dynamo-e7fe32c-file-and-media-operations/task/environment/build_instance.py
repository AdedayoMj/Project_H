#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import shutil
import tempfile
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from font_model import build_variable_font, instantiate_font


SEED = 7_032_681
PAGE_WIDTH = 1200
PAGE_HEIGHT = 760
PAGE_FONT_PX = 140


UPPER = {
    "A": ["01110", "10001", "10001", "11111", "10001", "10001", "10001"],
    "B": ["11110", "10001", "10001", "11110", "10001", "10001", "11110"],
    "C": ["01111", "10000", "10000", "10000", "10000", "10000", "01111"],
    "D": ["11110", "10001", "10001", "10001", "10001", "10001", "11110"],
    "E": ["11111", "10000", "10000", "11110", "10000", "10000", "11111"],
    "F": ["11111", "10000", "10000", "11110", "10000", "10000", "10000"],
    "G": ["01111", "10000", "10000", "10111", "10001", "10001", "01111"],
    "H": ["10001", "10001", "10001", "11111", "10001", "10001", "10001"],
    "I": ["11111", "00100", "00100", "00100", "00100", "00100", "11111"],
    "J": ["00111", "00010", "00010", "00010", "10010", "10010", "01100"],
    "K": ["10001", "10010", "10100", "11000", "10100", "10010", "10001"],
    "L": ["10000", "10000", "10000", "10000", "10000", "10000", "11111"],
    "M": ["10001", "11011", "10101", "10101", "10001", "10001", "10001"],
    "N": ["10001", "11001", "10101", "10011", "10001", "10001", "10001"],
    "O": ["01110", "10001", "10001", "10001", "10001", "10001", "01110"],
    "P": ["11110", "10001", "10001", "11110", "10000", "10000", "10000"],
    "Q": ["01110", "10001", "10001", "10001", "10101", "10010", "01101"],
    "R": ["11110", "10001", "10001", "11110", "10100", "10010", "10001"],
    "S": ["01111", "10000", "10000", "01110", "00001", "00001", "11110"],
    "T": ["11111", "00100", "00100", "00100", "00100", "00100", "00100"],
    "U": ["10001", "10001", "10001", "10001", "10001", "10001", "01110"],
    "V": ["10001", "10001", "10001", "10001", "10001", "01010", "00100"],
    "W": ["10001", "10001", "10001", "10101", "10101", "10101", "01010"],
    "X": ["10001", "10001", "01010", "00100", "01010", "10001", "10001"],
    "Y": ["10001", "10001", "01010", "00100", "00100", "00100", "00100"],
    "Z": ["11111", "00001", "00010", "00100", "01000", "10000", "11111"],
}

DIGITS = {
    "zero": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
    "one": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "two": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
    "three": ["11110", "00001", "00001", "01110", "00001", "00001", "11110"],
    "four": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "five": ["11111", "10000", "10000", "11110", "00001", "00001", "11110"],
    "six": ["01110", "10000", "10000", "11110", "10001", "10001", "01110"],
    "seven": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "eight": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
    "nine": ["01110", "10001", "10001", "01111", "00001", "00001", "01110"],
}

LOWER = {
    "f": ["00110", "01000", "01000", "11100", "01000", "01000", "01000"],
    "i": ["00100", "00000", "01100", "00100", "00100", "00100", "01110"],
    "s": ["00000", "00000", "01110", "10000", "01110", "00001", "11110"],
    "t": ["01000", "01000", "11100", "01000", "01000", "01001", "00110"],
}


def combine(first: list[str], second: list[str]) -> list[str]:
    return [left + "0" + right for left, right in zip(first, second)]


PATTERNS: dict[str, list[str]] = {
    ".notdef": ["11111", "10001", "10101", "10101", "10101", "10001", "11111"],
    "space": ["000", "000", "000", "000", "000", "000", "000"],
    **UPPER,
    **DIGITS,
    **LOWER,
    "hyphen": ["00000", "00000", "00000", "11111", "00000", "00000", "00000"],
    "period": ["000", "000", "000", "000", "000", "010", "010"],
    "comma": ["000", "000", "000", "000", "000", "010", "100"],
    "acutecomb": ["010", "100"],
    "f_i": combine(LOWER["f"], LOWER["i"]),
    "s_t": combine(LOWER["s"], LOWER["t"]),
}

KERNING = [
    {"left": "A", "right": "V", "value": -84},
    {"left": "A", "right": "T", "value": -58},
    {"left": "T", "right": "A", "value": -72},
    {"left": "T", "right": "O", "value": -34},
    {"left": "V", "right": "A", "value": -80},
    {"left": "W", "right": "A", "value": -68},
    {"left": "Y", "right": "A", "value": -90},
    {"left": "L", "right": "T", "value": -44},
    {"left": "P", "right": "A", "value": -36},
    {"left": "F", "right": "O", "value": -28},
    {"left": "one", "right": "seven", "value": -30},
    {"left": "seven", "right": "four", "value": -42},
]

ANCHORS = {
    "mark": {"glyph": "acutecomb", "x": 180, "y": 640},
    "bases": {
        "A": {"x_offset": -8, "y": 790},
        "E": {"x_offset": 12, "y": 765},
        "I": {"x_offset": 0, "y": 780},
        "O": {"x_offset": -5, "y": 790},
        "U": {"x_offset": 7, "y": 770},
    },
}

LIGATURES = [
    {"feature": "liga", "input": ["f", "i"], "output": "f_i"},
    {"feature": "liga", "input": ["s", "t"], "output": "s_t"},
]


def glyph_registry() -> list[dict]:
    result = [
        {"name": ".notdef", "codepoints": [], "evidence_codepoint": 0xE000},
        {"name": "space", "codepoints": [0x20], "evidence_codepoint": 0x20},
    ]
    result.extend(
        {"name": char, "codepoints": [ord(char)], "evidence_codepoint": ord(char)}
        for char in UPPER
    )
    digit_names = list(DIGITS)
    result.extend(
        {
            "name": name,
            "codepoints": [ord(str(index))],
            "evidence_codepoint": ord(str(index)),
        }
        for index, name in enumerate(digit_names)
    )
    result.extend(
        {"name": name, "codepoints": [ord(name)], "evidence_codepoint": ord(name)}
        for name in LOWER
    )
    result.extend(
        [
            {"name": "hyphen", "codepoints": [0x2D], "evidence_codepoint": 0x2D},
            {"name": "period", "codepoints": [0x2E], "evidence_codepoint": 0x2E},
            {"name": "comma", "codepoints": [0x2C], "evidence_codepoint": 0x2C},
            {"name": "acutecomb", "codepoints": [0x0301, 0xE100], "evidence_codepoint": 0xE100},
            {"name": "f_i", "codepoints": [0xE001], "evidence_codepoint": 0xE001},
            {"name": "s_t", "codepoints": [0xE002], "evidence_codepoint": 0xE002},
        ]
    )
    for item in result:
        rows = PATTERNS[item["name"]]
        item["rows"] = len(rows)
        item["columns"] = len(rows[0])
        item["advance_adjust_units"] = 0
    return result


AXES = [
    {
        "tag": "wght",
        "name": "Weight",
        "minimum": 300.0,
        "default": 500.0,
        "maximum": 800.0,
        "map": [[300, 300], [400, 430], [500, 500], [650, 620], [800, 800]],
    },
    {
        "tag": "wdth",
        "name": "Width",
        "minimum": 75.0,
        "default": 100.0,
        "maximum": 125.0,
        "map": [[75, 75], [90, 92], [100, 100], [112, 110], [125, 125]],
    },
    {
        "tag": "opsz",
        "name": "Optical Size",
        "minimum": 8.0,
        "default": 16.0,
        "maximum": 72.0,
        "map": [[8, 8], [12, 13], [16, 16], [40, 35], [72, 72]],
    },
]

MASTERS = [
    {
        "name": "Default",
        "style_name": "Regular",
        "location": {"wght": 500.0, "wdth": 100.0, "opsz": 16.0},
    },
    {
        "name": "WeightMin",
        "style_name": "Weight Min",
        "location": {"wght": 300.0, "wdth": 100.0, "opsz": 16.0},
    },
    {
        "name": "WeightMax",
        "style_name": "Weight Max",
        "location": {"wght": 800.0, "wdth": 100.0, "opsz": 16.0},
    },
    {
        "name": "WidthMin",
        "style_name": "Width Min",
        "location": {"wght": 500.0, "wdth": 75.0, "opsz": 16.0},
    },
    {
        "name": "WidthMax",
        "style_name": "Width Max",
        "location": {"wght": 500.0, "wdth": 125.0, "opsz": 16.0},
    },
    {
        "name": "OpticalMin",
        "style_name": "Optical Min",
        "location": {"wght": 500.0, "wdth": 100.0, "opsz": 8.0},
    },
    {
        "name": "OpticalMax",
        "style_name": "Optical Max",
        "location": {"wght": 500.0, "wdth": 100.0, "opsz": 72.0},
    },
]


def make_ticket() -> dict:
    glyphs = glyph_registry()
    glyph_order = [item["name"] for item in glyphs]
    layout_slots = []
    for index, glyph_name in enumerate(glyph_order):
        layout_index = index // 24
        slot_index = index % 24
        row, column = divmod(slot_index, 6)
        layout_slots.append(
            {
                "layout_id": f"layout-{layout_index}",
                "slot": slot_index,
                "glyph": glyph_name,
                "origin_x_px": 34 + column * 195,
                "baseline_y_px": 152 + row * 180,
            }
        )
    return {
        "schema_version": 1,
        "authority": {
            "trusted": ["/app/input/job_ticket.json", "layout_id", "page_slot_geometry", "fiducial page coordinates"],
            "untrusted": ["evidence filenames", "reported_axis_untrusted", "individual calibration samples"],
        },
        "font_contract": {
            "family_name": "Dynamo Palimpsest",
            "postscript_prefix": "DynamoPalimpsest",
            "units_per_em": 1000,
            "ascender": 840,
            "descender": -160,
            "line_gap": 80,
            "x_height": 520,
            "cap_height": 780,
            "glyph_order": glyph_order,
            "glyphs": glyphs,
            "axes": AXES,
            "masters": MASTERS,
            "default_master": "Default",
            "required_tables": [
                "avar",
                "cmap",
                "fvar",
                "GDEF",
                "glyf",
                "GPOS",
                "GSUB",
                "gvar",
                "head",
                "hhea",
                "hmtx",
                "maxp",
                "name",
                "OS/2",
                "post",
            ],
            "forbidden_tables": ["CBDT", "CBLC", "COLR", "CPAL", "EBDT", "EBLC", "sbix", "SVG "],
            "geometry": {
                "axis_normalization": "piecewise linear: (v-default)/(default-minimum) below default and (v-default)/(maximum-default) above default",
                "coordinate_rounding": "round to nearest integer, ties to even, at source masters",
                "pitch_x_base": 100,
                "pitch_x_wdth_delta": 16,
                "pitch_y": 100,
                "cell_width_base": 56,
                "cell_width_wght_delta": 16,
                "cell_width_opsz_delta": -4,
                "cell_height_base": 66,
                "cell_height_wght_delta": 18,
                "cell_height_opsz_delta": -4,
                "corner_radius_base": 12,
                "corner_radius_wght_delta": -3,
                "corner_radius_opsz_delta": 6,
                "first_center_x": 80,
                "top_center_y": 740,
                "advance_side_space": 80,
                "cell_outline": "clockwise rounded rectangle with quadratic corners tangent at radius",
            },
            "open_type_recovery": {
                "kerning_candidate_pairs": [[item["left"], item["right"]] for item in KERNING],
                "base_anchor_glyphs": list(ANCHORS["bases"]),
                "base_anchor_parameterization": "x=master advance/2+x_offset; y is invariant",
                "mark_glyph": "acutecomb",
                "mark_anchor_name": "_top",
                "base_anchor_name": "top",
                "ligature_inputs": [item["input"] for item in LIGATURES],
                "required_features": ["kern", "liga", "mark"],
            },
            "pattern_digest": "lowercase SHA-256 hex of the UTF-8 cell rows joined by LF with no final LF",
        },
        "evidence_contract": {
            "index": "/app/evidence/index.json",
            "layout_probes": "/app/evidence/layout_probes.json",
            "scan_count": 42,
            "page_width_px": PAGE_WIDTH,
            "page_height_px": PAGE_HEIGHT,
            "font_size_px": PAGE_FONT_PX,
            "page_slots": layout_slots,
            "registration_model": "projective homography from image pixels to canonical page pixels",
            "fiducials_per_scan": 24,
            "fiducial_outliers_per_scan": 6,
            "axis_patch_encoding": {
                "wght": "(wght-300)/(800-300)",
                "wdth": "(wdth-75)/(125-75)",
                "opsz": "(opsz-8)/(72-8)",
                "recovery": "component-wise median, inverse affine map, then nearest capture location",
            },
            "capture_locations": [item["location"] for item in MASTERS],
            "cell_recovery": "rectify each page, sample the disclosed cell centres, and robustly fuse redundant observations; black is occupied",
        },
        "source_contract": {
            "archive": "/app/output/sources.zip",
            "exact_top_level_files": ["features.fea", "font.designspace"],
            "masters": [f"masters/{item['name']}.ufo" for item in MASTERS],
            "format": "Designspace version 5 plus UFO version 3 masters",
            "requirements": "all glyphs present in every master, no raster images, interpolation-compatible contour topology, recovered kerning/features/anchors",
        },
        "manifest_schema": {
            "top_level_exact_keys": [
                "schema_version",
                "family",
                "axes",
                "glyphs",
                "open_type",
                "captures",
                "pdf",
            ],
            "family_exact_keys": ["name", "units_per_em"],
            "axis_exact_keys": ["tag", "minimum", "default", "maximum"],
            "glyph_exact_keys": ["name", "pattern_sha256"],
            "open_type_exact_keys": ["kerning", "anchors", "ligatures"],
            "kerning_exact_keys": ["left", "right", "value"],
            "base_anchor_exact_keys": ["glyph", "kind", "x_offset", "y"],
            "mark_anchor_exact_keys": ["glyph", "kind", "x", "y"],
            "ligature_exact_keys": ["feature", "input", "output"],
            "capture_exact_keys": [
                "id",
                "file",
                "layout_id",
                "axis_location",
                "reported_axis_status",
                "image_to_page_homography",
            ],
            "pdf_exact_keys": ["title", "subject"],
            "array_order": "font axes, glyph order, candidate kerning order, base-anchor order then mark, ligature-input order, and observation id order",
        },
        "proof_contract": {
            "width_px": 1600,
            "height_px": 900,
            "background_rgb": [244, 239, 228],
            "rendering": "Pillow 11.3.0 BASIC layout, no hinting contract beyond the pinned environment; instantiate the submitted variable font before each line",
            "lines": [
                {
                    "text": "DYNAMO TYPE REVIVAL 2048",
                    "x_px": 74,
                    "baseline_y_px": 175,
                    "size_px": 112,
                    "rgb": [28, 24, 21],
                    "location": {"wght": 420, "wdth": 94, "opsz": 13},
                },
                {
                    "text": "VARIABLE ARCHIVE 07",
                    "x_px": 74,
                    "baseline_y_px": 350,
                    "size_px": 132,
                    "rgb": [77, 35, 29],
                    "location": {"wght": 720, "wdth": 112, "opsz": 38},
                },
                {
                    "text": "WIDTH 75-125  WEIGHT 300-800",
                    "x_px": 74,
                    "baseline_y_px": 515,
                    "size_px": 74,
                    "rgb": [31, 55, 63],
                    "location": {"wght": 535, "wdth": 78, "opsz": 24},
                },
                {
                    "text": "OPTICAL SIZE 8-72.",
                    "x_px": 74,
                    "baseline_y_px": 680,
                    "size_px": 118,
                    "rgb": [35, 32, 78],
                    "location": {"wght": 610, "wdth": 119, "opsz": 61},
                },
                {
                    "text": "AVATAR  WAVE  TYPOGRAPHY",
                    "x_px": 74,
                    "baseline_y_px": 830,
                    "size_px": 82,
                    "rgb": [24, 24, 24],
                    "location": {"wght": 360, "wdth": 104, "opsz": 10},
                },
            ],
        },
        "pdf_contract": {
            "title": "Dynamo Palimpsest Variable Font Specimen",
            "subject": "Forensic variable-font revival proof",
            "render_dpi": 144,
            "one_page": True,
            "unencrypted": True,
            "live_text": True,
            "embedded_recovered_font": True,
        },
        "acceptance_tolerances": {
            "axis_coordinate_max": {"wght": 1.0, "wdth": 0.15, "opsz": 0.15},
            "registration_inlier_residual_px_max": 0.65,
            "registration_required_inliers": 18,
            "glyph_mask_iou_min": 0.997,
            "glyph_boundary_hausdorff_em_max": 0.004,
            "metric_error_units_max": 2,
            "anchor_error_units_max": 3,
            "source_coordinate_error_units_max": 1.0,
            "proof_median_absolute_rgb_max": 0.35,
            "proof_p99_absolute_rgb_max": 3.0,
            "pdf_render_mean_absolute_rgb_max": 1.0,
        },
    }


def _location_key(location: dict[str, float]) -> tuple[float, float, float]:
    return tuple(float(location[tag]) for tag in ("wght", "wdth", "opsz"))


def make_canonical_page(
    ticket: dict,
    layout_id: str,
    instance_font: Path,
    rng: random.Random,
    np_rng: np.random.Generator,
    replicate: int,
) -> np.ndarray:
    y = np.linspace(0, 1, PAGE_HEIGHT, dtype=np.float32)[:, None]
    x = np.linspace(0, 1, PAGE_WIDTH, dtype=np.float32)[None, :]
    background = 242.0 - 8.0 * x + 5.0 * y + 3.0 * np.sin(2 * math.pi * (x + 0.3 * y))
    page = np.clip(background + np_rng.normal(0, 1.1, (PAGE_HEIGHT, PAGE_WIDTH)), 0, 255).astype(np.uint8)
    image = Image.fromarray(page, mode="L")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(str(instance_font), PAGE_FONT_PX, layout_engine=ImageFont.Layout.BASIC)
    glyph_by_name = {item["name"]: item for item in ticket["font_contract"]["glyphs"]}
    for slot in ticket["evidence_contract"]["page_slots"]:
        if slot["layout_id"] != layout_id:
            continue
        char = chr(glyph_by_name[slot["glyph"]]["evidence_codepoint"])
        draw.text(
            (slot["origin_x_px"], slot["baseline_y_px"]),
            char,
            font=font,
            fill=22 + replicate * 2,
            anchor="ls",
        )
    for row in range(4):
        for column in range(6):
            x0 = 18 + column * 195
            y0 = 18 + row * 180
            draw.rectangle((x0, y0, x0 + 174, y0 + 154), outline=218, width=1)

    # Faint bleed-through that does not share the foreground lattice.
    bleed = Image.new("L", image.size, 255)
    bleed_draw = ImageDraw.Draw(bleed)
    for _ in range(24):
        x0 = rng.randint(0, PAGE_WIDTH - 100)
        y0 = rng.randint(0, PAGE_HEIGHT - 30)
        bleed_draw.line((x0, y0, x0 + rng.randint(40, 180), y0 + rng.randint(-12, 12)), fill=205, width=2)
    image = Image.blend(image, bleed.transpose(Image.Transpose.FLIP_LEFT_RIGHT), 0.12)
    image = image.filter(ImageFilter.GaussianBlur(radius=0.45 + 0.2 * replicate))
    page = np.asarray(image, dtype=np.uint8)
    if replicate == 1:
        page = cv2.erode(page, np.ones((2, 2), dtype=np.uint8), iterations=1)

    damage = Image.fromarray(page, mode="L").convert("RGBA")
    damage_draw = ImageDraw.Draw(damage, "RGBA")
    for _ in range(8):
        cx = rng.randint(40, PAGE_WIDTH - 40)
        cy = rng.randint(30, PAGE_HEIGHT - 30)
        rx = rng.randint(12, 58)
        ry = rng.randint(8, 34)
        color = rng.choice([(92, 65, 38, 40), (250, 246, 232, 170), (48, 42, 39, 28)])
        damage_draw.ellipse((cx - rx, cy - ry, cx + rx, cy + ry), fill=color)
    for _ in range(2):
        x0 = rng.randint(20, PAGE_WIDTH - 180)
        y0 = rng.randint(20, PAGE_HEIGHT - 90)
        damage_draw.rectangle((x0, y0, x0 + rng.randint(55, 150), y0 + rng.randint(25, 70)), fill=(246, 241, 226, 230))
    return np.asarray(damage.convert("L"), dtype=np.uint8)


def project_points(matrix: np.ndarray, points: np.ndarray) -> np.ndarray:
    homogeneous = np.column_stack([points, np.ones(len(points))])
    projected = homogeneous @ matrix.T
    return projected[:, :2] / projected[:, 2:3]


def measurement_samples(
    rng: random.Random,
    np_rng: np.random.Generator,
    values: list[float],
    count: int,
    outliers: int,
    sigma: float,
    outlier_scale: float,
) -> list:
    samples = []
    for _ in range(count - outliers):
        samples.append([float(value + np_rng.normal(0, sigma)) for value in values])
    for _ in range(outliers):
        samples.append([float(value + rng.choice([-1, 1]) * rng.uniform(outlier_scale * 0.6, outlier_scale)) for value in values])
    rng.shuffle(samples)
    return [row[0] if len(row) == 1 else row for row in samples]


def make_layout_probes(root: Path, rng: random.Random, np_rng: np.random.Generator) -> None:
    probes = {
        "schema_version": 1,
        "kerning": [
            {
                "left": item["left"],
                "right": item["right"],
                "measured_adjustment_units": measurement_samples(
                    rng, np_rng, [item["value"]], 13, 4, 0.38, 65
                ),
            }
            for item in KERNING
        ],
        "anchors": [
            {
                "glyph": glyph,
                "kind": "base",
                "measured_parameter_samples": measurement_samples(
                    rng, np_rng, [item["x_offset"], item["y"]], 13, 4, 0.45, 70
                ),
            }
            for glyph, item in ANCHORS["bases"].items()
        ]
        + [
            {
                "glyph": ANCHORS["mark"]["glyph"],
                "kind": "mark",
                "measured_parameter_samples": measurement_samples(
                    rng,
                    np_rng,
                    [ANCHORS["mark"]["x"], ANCHORS["mark"]["y"]],
                    13,
                    4,
                    0.45,
                    70,
                ),
            }
        ],
        "ligatures": [],
    }
    for item in LIGATURES:
        votes = [item["output"]] * 7
        other = "s_t" if item["output"] == "f_i" else "f_i"
        votes.extend([other, item["input"][0], ".notdef"])
        rng.shuffle(votes)
        probes["ligatures"].append({"feature": "liga", "input": item["input"], "output_votes": votes})
    (root / "evidence" / "layout_probes.json").write_text(json.dumps(probes, indent=2) + "\n")


def fingerprint_tree(paths: list[Path], base: Path) -> str:
    digest = hashlib.sha256()
    entries = []
    for parent in paths:
        entries.extend(item for item in parent.rglob("*") if item.is_file())
    for path in sorted(entries, key=lambda item: item.relative_to(base).as_posix().encode()):
        relative = path.relative_to(base).as_posix()
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def build(root: Path, tests_root: Path | None = None) -> None:
    rng = random.Random(SEED)
    np_rng = np.random.default_rng(SEED)
    input_dir = root / "input"
    evidence_dir = root / "evidence"
    scans_dir = evidence_dir / "scans"
    shutil.rmtree(input_dir, ignore_errors=True)
    shutil.rmtree(evidence_dir, ignore_errors=True)
    input_dir.mkdir(parents=True)
    scans_dir.mkdir(parents=True)
    ticket = make_ticket()
    (input_dir / "job_ticket.json").write_text(json.dumps(ticket, indent=2) + "\n")
    make_layout_probes(root, rng, np_rng)

    with tempfile.TemporaryDirectory(prefix="dynamo-hidden-font-") as tmp:
        work = Path(tmp)
        hidden_font = work / "hidden.ttf"
        build_variable_font(hidden_font, PATTERNS, ticket, KERNING, ANCHORS, LIGATURES)
        instances = {}
        for master in MASTERS:
            path = work / f"{master['name']}.ttf"
            instantiate_font(hidden_font, master["location"], path)
            instances[_location_key(master["location"])] = path

        records = []
        hidden_captures = []
        observation_index = 0
        layouts = ["layout-0", "layout-1"]
        canonical_fiducials = np.asarray(
            [
                [40 + column * 224, 42 + row * 220]
                for row in range(4)
                for column in range(6)
            ],
            dtype=np.float64,
        )
        for master in MASTERS:
            actual = master["location"]
            encoded = [
                (actual["wght"] - 300.0) / 500.0,
                (actual["wdth"] - 75.0) / 50.0,
                (actual["opsz"] - 8.0) / 64.0,
            ]
            for layout_id in layouts:
                for replicate in range(3):
                    canonical = make_canonical_page(
                        ticket,
                        layout_id,
                        instances[_location_key(actual)],
                        rng,
                        np_rng,
                        replicate,
                    )
                    source_corners = np.float32(
                        [[0, 0], [PAGE_WIDTH - 1, 0], [PAGE_WIDTH - 1, PAGE_HEIGHT - 1], [0, PAGE_HEIGHT - 1]]
                    )
                    margin = 24
                    destination_corners = np.float32(
                        [
                            [rng.uniform(4, margin), rng.uniform(4, margin)],
                            [PAGE_WIDTH - 1 - rng.uniform(4, margin), rng.uniform(4, margin)],
                            [
                                PAGE_WIDTH - 1 - rng.uniform(4, margin),
                                PAGE_HEIGHT - 1 - rng.uniform(4, margin),
                            ],
                            [rng.uniform(4, margin), PAGE_HEIGHT - 1 - rng.uniform(4, margin)],
                        ]
                    )
                    page_to_image = cv2.getPerspectiveTransform(source_corners, destination_corners).astype(np.float64)
                    warped = cv2.warpPerspective(
                        canonical,
                        page_to_image,
                        (PAGE_WIDTH, PAGE_HEIGHT),
                        flags=cv2.INTER_LINEAR,
                        borderMode=cv2.BORDER_CONSTANT,
                        borderValue=247,
                    )
                    warped = np.clip(
                        warped.astype(np.float32) + np_rng.normal(0, 1.2, warped.shape),
                        0,
                        255,
                    ).astype(np.uint8)
                    observation_id = f"OBS-{observation_index:03d}"
                    filename = f"scan-{hashlib.sha256((observation_id + ':font').encode()).hexdigest()[:20]}.png"
                    cv2.imwrite(str(scans_dir / filename), warped)

                    image_points = project_points(page_to_image, canonical_fiducials)
                    image_points += np_rng.normal(0, 0.08, image_points.shape)
                    outlier_indices = set(rng.sample(range(len(image_points)), 6))
                    for index in outlier_indices:
                        image_points[index] += np.asarray(
                            [rng.choice([-1, 1]) * rng.uniform(28, 90), rng.choice([-1, 1]) * rng.uniform(28, 90)]
                        )
                    fiducials = [
                        {
                            "id": f"F-{index:02d}",
                            "page_px": [float(value) for value in canonical_fiducials[index]],
                            "image_px": [float(value) for value in image_points[index]],
                        }
                        for index in range(len(canonical_fiducials))
                    ]
                    patch_samples = [
                        [float(value + np_rng.normal(0, 0.0025)) for value in encoded]
                        for _ in range(8)
                    ]
                    patch_samples.extend(
                        [[rng.random(), rng.random(), rng.random()] for _ in range(3)]
                    )
                    rng.shuffle(patch_samples)
                    if observation_index % 3 == 0:
                        reported = dict(actual)
                    else:
                        alternatives = [item["location"] for item in MASTERS if item["name"] != master["name"]]
                        reported = dict(rng.choice(alternatives))
                    record = {
                        "id": observation_id,
                        "file": f"/app/evidence/scans/{filename}",
                        "layout_id": layout_id,
                        "reported_axis_untrusted": reported,
                        "calibration_patch_samples": patch_samples,
                        "width_px": PAGE_WIDTH,
                        "height_px": PAGE_HEIGHT,
                        "fiducials": fiducials,
                    }
                    records.append(record)
                    hidden_captures.append(
                        {
                            "id": observation_id,
                            "axis_location": actual,
                            "reported_axis_status": "correct" if reported == actual else "mislabelled",
                            "inlier_ids": [
                                f"F-{index:02d}"
                                for index in range(len(canonical_fiducials))
                                if index not in outlier_indices
                            ],
                            "page_to_image_homography": page_to_image.tolist(),
                        }
                    )
                    observation_index += 1

        (evidence_dir / "index.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "observations": records,
                },
                indent=2,
            )
            + "\n"
        )

        if tests_root is not None:
            tests_root.mkdir(parents=True, exist_ok=True)
            shutil.copy2(hidden_font, tests_root / "reference.ttf")
            hidden_locations = [
                {"wght": 335, "wdth": 82, "opsz": 11},
                {"wght": 465, "wdth": 118, "opsz": 27},
                {"wght": 640, "wdth": 91, "opsz": 53},
                {"wght": 775, "wdth": 122, "opsz": 68},
            ]
            hidden_glyphs = [
                glyph_name
                for glyph_name in ticket["font_contract"]["glyph_order"]
                if glyph_name != "space"
            ]
            expected = {
                "schema_version": 1,
                "patterns": PATTERNS,
                "kerning": KERNING,
                "anchors": ANCHORS,
                "ligatures": LIGATURES,
                "captures": hidden_captures,
                "input_fingerprint_sha256": fingerprint_tree([input_dir, evidence_dir], root),
                "hidden_render_cases": [
                    {
                        "location": location,
                        "glyphs": hidden_glyphs[index::len(hidden_locations)],
                    }
                    for index, location in enumerate(hidden_locations)
                ],
                "shaping_cases": [
                    {"text": "AVATAR WAVE", "location": {"wght": 375, "wdth": 88, "opsz": 12}},
                    {"text": "TOYOTA", "location": {"wght": 705, "wdth": 116, "opsz": 46}},
                    {"text": "AT LT PA FO", "location": {"wght": 485, "wdth": 109, "opsz": 22}},
                    {"text": "YA", "location": {"wght": 755, "wdth": 94, "opsz": 58}},
                    {"text": "fi st", "location": {"wght": 530, "wdth": 103, "opsz": 19}},
                    {"text": "A\u0301 E\u0301 I\u0301 O\u0301 U\u0301", "location": {"wght": 590, "wdth": 97, "opsz": 32}},
                    {"text": "1747", "location": {"wght": 445, "wdth": 108, "opsz": 14}},
                ],
            }
            (tests_root / "expected.json").write_text(json.dumps(expected, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("/app"))
    parser.add_argument("--tests-root", type=Path)
    arguments = parser.parse_args()
    build(arguments.root, arguments.tests_root)
