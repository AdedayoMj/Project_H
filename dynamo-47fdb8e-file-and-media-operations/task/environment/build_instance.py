#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import shutil
from pathlib import Path

import cv2
import numpy as np
import qrcode
from PIL import Image, ImageDraw, ImageFont


SEED = 470_478
DPI = 600
MM_PER_INCH = 25.4
PX_PER_MM = DPI / MM_PER_INCH
WIDTH_MM = 72.0
HEIGHT_MM = 48.0
WIDTH = round(WIDTH_MM * PX_PER_MM)
HEIGHT = round(HEIGHT_MM * PX_PER_MM)

PLATES = [
    {
        "id": "C",
        "canonical_name": "PROCESS_CYAN",
        "role": "process",
        "lab": [55.2, -37.8, -50.1],
        "transmission_linear_rgb": [0.18, 0.72, 0.86],
    },
    {
        "id": "M",
        "canonical_name": "PROCESS_MAGENTA",
        "role": "process",
        "lab": [48.7, 70.1, -14.2],
        "transmission_linear_rgb": [0.82, 0.14, 0.51],
    },
    {
        "id": "Y",
        "canonical_name": "PROCESS_YELLOW",
        "role": "process",
        "lab": [88.6, -5.3, 88.1],
        "transmission_linear_rgb": [0.96, 0.83, 0.10],
    },
    {
        "id": "K",
        "canonical_name": "PROCESS_BLACK",
        "role": "process",
        "lab": [17.8, 0.3, -0.5],
        "transmission_linear_rgb": [0.08, 0.08, 0.075],
    },
    {
        "id": "SC",
        "canonical_name": "DYNAMO_COPPER_876",
        "role": "spot",
        "lab": [53.4, 25.8, 34.6],
        "transmission_linear_rgb": [0.55, 0.25, 0.09],
    },
    {
        "id": "OW",
        "canonical_name": "DYNAMO_OPAQUE_WHITE",
        "role": "opaque_white",
        "lab": [95.4, 0.2, 1.1],
        "transmission_linear_rgb": [0.98, 0.98, 0.965],
    },
    {
        "id": "V",
        "canonical_name": "DYNAMO_GLOSS_VARNISH",
        "role": "varnish",
        "lab": [82.0, -0.2, 0.4],
        "transmission_linear_rgb": [1.0, 1.0, 1.0],
    },
]

NOMINAL_COVERAGE_LEVELS = {
    "C": [0, 170, 255],
    "M": [0, 255],
    "Y": [0, 210, 255],
    "K": [0, 110, 255],
    "SC": [0, 255],
    "OW": [0, 255],
    "V": [0, 150, 210, 255],
}

TEXT_OBJECTS = [
    {
        "id": "brand",
        "text": "NORTHSTAR",
        "x_mm": 19.0,
        "y_mm": 18.0,
        "font_file": "DejaVuSans-Bold.ttf",
        "font_family": "DejaVu Sans",
        "font_style": "Bold",
        "font_size_pt": 10.0,
    },
    {
        "id": "product",
        "text": "SERUM 07",
        "x_mm": 20.0,
        "y_mm": 24.2,
        "font_file": "DejaVuSans.ttf",
        "font_family": "DejaVu Sans",
        "font_style": "Regular",
        "font_size_pt": 6.0,
    },
    {
        "id": "volume",
        "text": "30 mL",
        "x_mm": 20.0,
        "y_mm": 28.4,
        "font_file": "DejaVuSans.ttf",
        "font_family": "DejaVu Sans",
        "font_style": "Regular",
        "font_size_pt": 4.2,
    },
    {
        "id": "lot",
        "text": "LOT H7K4-29",
        "x_mm": 45.0,
        "y_mm": 31.5,
        "font_file": "DejaVuSans.ttf",
        "font_family": "DejaVu Sans",
        "font_style": "Regular",
        "font_size_pt": 3.4,
    },
    {
        "id": "maker",
        "text": "POLARIS LABS",
        "x_mm": 44.2,
        "y_mm": 35.0,
        "font_file": "DejaVuSans-Bold.ttf",
        "font_family": "DejaVu Sans",
        "font_style": "Bold",
        "font_size_pt": 3.0,
    },
]

BARCODE = {
    "id": "trace_code",
    "symbology": "QR",
    "payload": "09506000123457|LOT-H7K4-29",
    "x_mm": 7.6,
    "y_mm": 21.0,
    "size_mm": 10.8,
    "version": 3,
    "error_correction": "Q",
}

DIELINE = {
    "cut": [
        [[3.0, 3.0], [69.0, 3.0], [69.0, 45.0], [3.0, 45.0], [3.0, 3.0]],
    ],
    "crease": [
        [[9.0, 3.0], [9.0, 45.0]],
        [[21.0, 3.0], [21.0, 45.0]],
        [[41.0, 3.0], [41.0, 45.0]],
        [[53.0, 3.0], [53.0, 45.0]],
        [[3.0, 13.0], [69.0, 13.0]],
        [[3.0, 35.0], [69.0, 35.0]],
    ],
    "perforation": [
        [[53.0, 17.0], [69.0, 17.0]],
    ],
}


def mm(value: float) -> int:
    return round(value * PX_PER_MM)


def qr_matrix(payload: str) -> list[list[bool]]:
    code = qrcode.QRCode(
        version=BARCODE["version"],
        error_correction=qrcode.constants.ERROR_CORRECT_Q,
        box_size=1,
        border=0,
    )
    code.add_data(payload)
    code.make(fit=False)
    return code.get_matrix()


def polyline(
    target: np.ndarray,
    points: list[tuple[int, int]],
    thickness: int,
    closed: bool = False,
) -> None:
    cv2.polylines(
        target,
        [np.asarray(points, dtype=np.int32)],
        closed,
        255,
        thickness,
        lineType=cv2.LINE_AA,
    )


def make_master(font_dir: Path) -> dict[str, np.ndarray]:
    plates = {plate["id"]: np.zeros((HEIGHT, WIDTH), dtype=np.uint8) for plate in PLATES}
    c, m, y, k, sc, ow, varnish = (plates[key] for key in ("C", "M", "Y", "K", "SC", "OW", "V"))

    # Cyan: a fold-spanning interference ribbon, fine rules, and panel blocks.
    ribbon = []
    for x in range(mm(4), mm(69), 5):
        phase = x / WIDTH * math.tau * 3.0
        ribbon.append((x, round(mm(26.0) + mm(3.2) * math.sin(phase))))
    polyline(c, ribbon, mm(1.45))
    for offset in range(0, mm(7.0), mm(0.65)):
        polyline(c, [(mm(4), mm(14) + offset), (mm(69), mm(33) + offset // 3)], max(1, mm(0.12)))
    cv2.rectangle(c, (mm(9.4), mm(17.0)), (mm(20.4), mm(34.2)), 255, -1)
    cv2.rectangle(c, (mm(53.4), mm(17.4)), (mm(68.5), mm(33.8)), 170, -1)

    # Magenta: an overlapping cellular field and two cross-fold arcs.
    for row in range(5):
        for col in range(12):
            cx = mm(5.5 + col * 5.6 + (row % 2) * 2.8)
            cy = mm(15.0 + row * 4.5)
            cv2.circle(m, (cx, cy), mm(1.35 + ((row + col) % 3) * 0.18), 255, -1, lineType=cv2.LINE_AA)
    cv2.ellipse(m, (mm(36), mm(24)), (mm(28), mm(8)), -8, 188, 350, 255, mm(0.8), cv2.LINE_AA)
    cv2.ellipse(m, (mm(36), mm(24)), (mm(24), mm(5)), 11, 4, 175, 255, mm(0.55), cv2.LINE_AA)

    # Yellow: radial shards whose continuations cross several creases.
    center = (mm(36), mm(24))
    for index in range(24):
        angle = index * math.tau / 24 + 0.07
        inner = mm(4.2 + (index % 3) * 0.45)
        outer = mm(31.0 + (index % 4) * 1.3)
        spread = 0.025 + (index % 2) * 0.011
        points = [
            (
                center[0] + round(inner * math.cos(angle - spread)),
                center[1] + round(inner * math.sin(angle - spread)),
            ),
            (
                center[0] + round(outer * math.cos(angle)),
                center[1] + round(outer * math.sin(angle)),
            ),
            (
                center[0] + round(inner * math.cos(angle + spread)),
                center[1] + round(inner * math.sin(angle + spread)),
            ),
        ]
        cv2.fillPoly(y, [np.asarray(points, dtype=np.int32)], 255, lineType=cv2.LINE_AA)
    cv2.circle(y, center, mm(4.5), 210, -1, lineType=cv2.LINE_AA)

    # Copper spot: nested emblem, micro-rules, and registration-sensitive corners.
    cv2.ellipse(sc, (mm(31), mm(24)), (mm(8.2), mm(7.0)), 0, 0, 360, 255, mm(1.0), cv2.LINE_AA)
    cv2.ellipse(sc, (mm(31), mm(24)), (mm(5.7), mm(4.8)), 0, 0, 360, 255, mm(0.45), cv2.LINE_AA)
    for index in range(11):
        x = mm(43.5 + index * 2.15)
        polyline(sc, [(x, mm(18.0)), (x + mm(1.2), mm(21.0)), (x, mm(24.0))], max(1, mm(0.22)))
    for x_mm, y_mm in [(6, 16), (67, 16), (6, 34), (67, 34)]:
        cv2.rectangle(
            sc,
            (mm(x_mm - 0.65), mm(y_mm - 0.65)),
            (mm(x_mm + 0.65), mm(y_mm + 0.65)),
            255,
            -1,
        )

    # Live text is printed on the K plate.
    k_image = Image.fromarray(k)
    draw = ImageDraw.Draw(k_image)
    for item in TEXT_OBJECTS:
        font = ImageFont.truetype(
            str(font_dir / item["font_file"]),
            round(item["font_size_pt"] * DPI / 72.0),
        )
        draw.text((mm(item["x_mm"]), mm(item["y_mm"])), item["text"], fill=255, font=font, anchor="lt")
    k[:] = np.asarray(k_image)
    cv2.rectangle(k, (mm(4.0), mm(14.0)), (mm(68.0), mm(36.0)), 255, max(1, mm(0.18)))
    for x_mm in (9.0, 21.0, 41.0, 53.0):
        cv2.line(k, (mm(x_mm), mm(13.5)), (mm(x_mm), mm(35.5)), 110, max(1, mm(0.10)), cv2.LINE_AA)

    matrix = qr_matrix(BARCODE["payload"])
    module = BARCODE["size_mm"] / len(matrix)
    for row, values in enumerate(matrix):
        for col, enabled in enumerate(values):
            if enabled:
                x0 = mm(BARCODE["x_mm"] + col * module)
                y0 = mm(BARCODE["y_mm"] + row * module)
                x1 = mm(BARCODE["x_mm"] + (col + 1) * module)
                y1 = mm(BARCODE["y_mm"] + (row + 1) * module)
                cv2.rectangle(k, (x0, y0), (x1, y1), 255, -1)

    # White underprint is related to, but not identical with, visible ink.
    visible_union = np.maximum.reduce([c, m, y, k, sc])
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (mm(0.55) * 2 + 1, mm(0.55) * 2 + 1))
    ow[:] = cv2.dilate((visible_union > 96).astype(np.uint8) * 255, kernel)
    cv2.rectangle(ow, (mm(17.5), mm(16.0)), (mm(40.5), mm(34.0)), 255, -1)
    # Deliberate knockout windows stop a solver deriving white by dilation alone.
    for x_mm, y_mm in [(25.0, 21.0), (35.5, 29.0), (57.0, 25.0)]:
        cv2.circle(ow, (mm(x_mm), mm(y_mm)), mm(1.25), 0, -1, lineType=cv2.LINE_AA)

    # Varnish has independent fine structure and selective omissions.
    cv2.ellipse(varnish, (mm(36), mm(24)), (mm(29), mm(9.5)), 0, 0, 360, 255, mm(1.15), cv2.LINE_AA)
    for index in range(16):
        x = mm(5.0 + index * 4.15)
        cv2.line(varnish, (x, mm(15.0)), (x + mm(2.8), mm(34.0)), 210, max(1, mm(0.25)), cv2.LINE_AA)
    cv2.rectangle(varnish, (mm(18.0), mm(17.0)), (mm(40.0), mm(33.5)), 150, -1)
    for item in TEXT_OBJECTS[:2]:
        cv2.rectangle(
            varnish,
            (mm(item["x_mm"] - 0.6), mm(item["y_mm"] - 0.5)),
            (mm(item["x_mm"] + 20.0), mm(item["y_mm"] + 3.4)),
            0,
            -1,
        )

    return plates


def linear_to_srgb(values: np.ndarray) -> np.ndarray:
    return np.where(
        values <= 0.0031308,
        12.92 * values,
        1.055 * np.power(values, 1.0 / 2.4) - 0.055,
    )


def render_proof(plates: dict[str, np.ndarray]) -> np.ndarray:
    substrate = np.array([0.62, 0.43, 0.24], dtype=np.float32)
    rgb = np.broadcast_to(substrate, (HEIGHT, WIDTH, 3)).copy()
    white = plates["OW"].astype(np.float32)[..., None] / 255.0
    rgb = rgb * (1.0 - white) + np.array([0.96, 0.965, 0.95], dtype=np.float32) * white
    by_id = {item["id"]: item for item in PLATES}
    for plate_id in ("C", "M", "Y", "K", "SC"):
        amount = plates[plate_id].astype(np.float32)[..., None] / 255.0
        transmission = np.asarray(by_id[plate_id]["transmission_linear_rgb"], dtype=np.float32)
        rgb *= 1.0 - amount + amount * transmission
    varnish = plates["V"].astype(np.float32)[..., None] / 255.0
    rgb += varnish * (1.0 - rgb) * 0.035
    return np.clip(np.rint(linear_to_srgb(np.clip(rgb, 0.0, 1.0)) * 255.0), 0, 255).astype(np.uint8)


def design_matrix(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    return np.column_stack(
        [
            np.ones_like(x),
            x,
            y,
            x * x,
            x * y,
            y * y,
            x * x * x,
            x * x * y,
            x * y * y,
            y * y * y,
        ]
    )


def corrupt_candidate(value: str, rng: random.Random, count: int) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ0123456789 -mL"
    chars = list(value)
    for index in rng.sample(range(len(chars)), count):
        choices = [char for char in alphabet if char != chars[index]]
        chars[index] = rng.choice(choices)
    return "".join(chars)


def write_dieline(path: Path) -> None:
    def points(values: list[list[float]]) -> str:
        return " ".join(f"{x:.3f},{y:.3f}" for x, y in values)

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH_MM}mm" height="{HEIGHT_MM}mm" viewBox="0 0 {WIDTH_MM} {HEIGHT_MM}">',
        '  <g data-role="dieline" fill="none">',
    ]
    for kind in ("cut", "crease", "perforation"):
        dash = ' stroke-dasharray="1.2 0.8"' if kind == "perforation" else ""
        color = {"cut": "#ff00ff", "crease": "#00a0ff", "perforation": "#ff8000"}[kind]
        for index, values in enumerate(DIELINE[kind]):
            lines.append(
                f'    <polyline data-kind="{kind}" data-index="{index}" points="{points(values)}" '
                f'stroke="{color}" stroke-width="0.10"{dash}/>'
            )
    lines.extend(["  </g>", "</svg>", ""])
    path.write_text("\n".join(lines))


def observation_mapping(
    obs_x: np.ndarray,
    obs_y: np.ndarray,
    x0: float,
    y0: float,
    obs_w: int,
    obs_h: int,
    coefficients: tuple[float, ...],
) -> tuple[np.ndarray, np.ndarray]:
    u = 2.0 * obs_x / (obs_w - 1) - 1.0
    v = 2.0 * obs_y / (obs_h - 1) - 1.0
    basis = design_matrix(u.ravel(), v.ravel())
    cx = np.asarray(
        [x0 + (obs_w - 1) / 2.0, (obs_w - 1) / 2.0, 0.0, *coefficients[:7]],
        dtype=np.float64,
    )
    cy = np.asarray(
        [y0 + (obs_h - 1) / 2.0, 0.0, (obs_h - 1) / 2.0, *coefficients[7:]],
        dtype=np.float64,
    )
    return (basis @ cx).reshape(obs_x.shape), (basis @ cy).reshape(obs_y.shape)


def build(root: Path) -> None:
    input_dir = root / "input"
    evidence_dir = root / "evidence"
    if input_dir.exists():
        shutil.rmtree(input_dir)
    if evidence_dir.exists():
        shutil.rmtree(evidence_dir)
    input_dir.mkdir(parents=True)
    evidence_dir.mkdir(parents=True)
    font_dir = input_dir / "fonts"
    font_dir.mkdir()

    bundled_assets = Path(__file__).parent / "assets"
    system_fonts = Path("/usr/share/fonts/truetype/dejavu")
    if not system_fonts.is_dir():
        system_fonts = bundled_assets
    for name in ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf"):
        shutil.copy2(system_fonts / name, font_dir / name)

    profile_candidates = [
        Path("/usr/share/color/icc/ghostscript/default_cmyk.icc"),
        Path("/usr/share/ghostscript/iccprofiles/default_cmyk.icc"),
        bundled_assets / "default_cmyk.icc",
    ]
    profile = next((path for path in profile_candidates if path.exists()), None)
    if profile is None:
        raise FileNotFoundError("Ghostscript default_cmyk.icc was not installed")
    shutil.copy2(profile, input_dir / "DYNAMO-SYNTH-CMYK-v1.icc")

    write_dieline(input_dir / "dieline.svg")
    master = make_master(font_dir)
    proof = render_proof(master)

    rng = random.Random(SEED)
    np_rng = np.random.default_rng(SEED)
    registry = {
        item["id"]: {
            "canonical_name": item["canonical_name"],
            "role": item["role"],
            "reference_lab": item["lab"],
            "transmission_linear_rgb": item["transmission_linear_rgb"],
            "nominal_coverage_levels": NOMINAL_COVERAGE_LEVELS[item["id"]],
        }
        for item in PLATES
    }

    ocr_objects = []
    for item in TEXT_OBJECTS:
        candidates = [item["text"], item["text"]]
        candidates.extend(corrupt_candidate(item["text"], rng, 1 + (index % 2)) for index in range(5))
        rng.shuffle(candidates)
        ocr_objects.append({"id": item["id"], "candidates": candidates})
    barcode_candidates = [BARCODE["payload"]] * 3 + [
        corrupt_candidate(BARCODE["payload"], rng, 1) for _ in range(4)
    ]
    rng.shuffle(barcode_candidates)
    (evidence_dir / "text_consensus.json").write_text(
        json.dumps(
            {
                "text_objects": ocr_objects,
                "barcode": {"id": BARCODE["id"], "candidates": barcode_candidates},
            },
            indent=2,
        )
        + "\n"
    )

    obs_w, obs_h = 980, 670
    grid_y, grid_x = np.indices((obs_h, obs_w), dtype=np.float64)
    tile_origins = [
        (-24.0, -22.0),
        (WIDTH - obs_w + 24.0, -22.0),
        (-24.0, HEIGHT - obs_h + 22.0),
        (WIDTH - obs_w + 24.0, HEIGHT - obs_h + 22.0),
    ]

    tasks: list[tuple[str, int, int]] = []
    for plate in PLATES:
        for tile_index in range(4):
            for repeat in range(2):
                tasks.append((plate["id"], tile_index, repeat))
    rng.shuffle(tasks)

    records = []
    for serial, (plate_id, tile_index, repeat) in enumerate(tasks, 1):
        plate = next(item for item in PLATES if item["id"] == plate_id)
        x0, y0 = tile_origins[tile_index]
        x0 += (-1 if repeat == 0 else 1) * (7.0 + tile_index * 1.5)
        y0 += (1 if repeat == 0 else -1) * (5.0 + tile_index)
        coefficients = tuple(rng.uniform(-2.2, 2.2) for _ in range(14))
        map_x, map_y = observation_mapping(grid_x, grid_y, x0, y0, obs_w, obs_h, coefficients)
        sampled = cv2.remap(
            master[plate_id],
            map_x.astype(np.float32),
            map_y.astype(np.float32),
            interpolation=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        ).astype(np.float32)

        paper = rng.uniform(235.0, 248.0)
        solid = rng.uniform(7.0, 24.0)
        gamma = rng.uniform(0.82, 1.18)
        level = paper - (paper - solid) * np.power(sampled / 255.0, gamma)
        level = cv2.GaussianBlur(level, (0, 0), rng.uniform(0.18, 0.48))
        level += np_rng.normal(0.0, 0.75, size=level.shape)
        level = np.clip(np.rint(level), 0, 255).astype(np.uint8)

        alpha = np.full((obs_h, obs_w), 255, dtype=np.uint8)
        if repeat == 0:
            for _ in range(2):
                occ_w = rng.randint(75, 155)
                occ_h = rng.randint(50, 125)
                ox = rng.randint(35, obs_w // 2 - occ_w - 25)
                oy = rng.randint(35, obs_h - occ_h - 35)
                if rng.random() < 0.5:
                    cv2.rectangle(alpha, (ox, oy), (ox + occ_w, oy + occ_h), 0, -1)
                else:
                    cv2.ellipse(
                        alpha,
                        (ox + occ_w // 2, oy + occ_h // 2),
                        (occ_w // 2, occ_h // 2),
                        rng.uniform(-35, 35),
                        0,
                        360,
                        0,
                        -1,
                    )
        rgba = np.dstack([level, level, level, alpha])

        digest = hashlib.sha256(f"{SEED}:{serial}:{plate_id}".encode()).hexdigest()[:18]
        filename = f"scan-{digest}.png"
        cv2.imwrite(str(evidence_dir / filename), rgba)

        fiducials = []
        fid_index = 0
        for gy in np.linspace(35, obs_h - 36, 6):
            for gx in np.linspace(38, obs_w - 39, 7):
                mx, my = observation_mapping(
                    np.asarray([[gx]]),
                    np.asarray([[gy]]),
                    x0,
                    y0,
                    obs_w,
                    obs_h,
                    coefficients,
                )
                fiducials.append(
                    {
                        "id": f"F{fid_index:02d}",
                        "image_px": [round(float(gx), 5), round(float(gy), 5)],
                        "master_px": [
                            round(float(mx[0, 0] + np_rng.normal(0, 0.025)), 5),
                            round(float(my[0, 0] + np_rng.normal(0, 0.025)), 5),
                        ],
                        "confidence": round(rng.uniform(0.72, 0.99), 4),
                    }
                )
                fid_index += 1
        for outlier_index in rng.sample(range(len(fiducials)), 8):
            fiducials[outlier_index]["master_px"] = [
                round(rng.uniform(0, WIDTH - 1), 5),
                round(rng.uniform(0, HEIGHT - 1), 5),
            ]
            fiducials[outlier_index]["confidence"] = round(rng.uniform(0.78, 0.995), 4)
        rng.shuffle(fiducials)

        tone_wedge = []
        for true_coverage in (0, 32, 64, 96, 128, 160, 192, 224, 255):
            expected = paper - (paper - solid) * ((true_coverage / 255.0) ** gamma)
            for _ in range(3):
                tone_wedge.append(
                    {
                        "true_coverage": true_coverage,
                        "observed_level": round(expected + rng.uniform(-0.65, 0.65), 4),
                    }
                )
        for outlier_index in rng.sample(range(len(tone_wedge)), 2):
            tone_wedge[outlier_index]["observed_level"] = round(rng.uniform(0, 255), 4)
        rng.shuffle(tone_wedge)

        spectral = []
        reference_lab = np.asarray(plate["lab"], dtype=np.float64)
        for _ in range(9):
            spectral.append((reference_lab + np_rng.normal(0, [0.42, 0.55, 0.55])).round(4).tolist())
        other_refs = [np.asarray(item["lab"]) for item in PLATES if item["id"] != plate_id]
        for outlier_index in rng.sample(range(9), 2):
            spectral[outlier_index] = (
                other_refs[rng.randrange(len(other_refs))] + np_rng.normal(0, 2.0, 3)
            ).round(4).tolist()

        reported = rng.choice([item["canonical_name"] for item in PLATES if item["id"] != plate_id])
        records.append(
            {
                "id": f"OBS-{serial:03d}",
                "kind": "plate_scan",
                "file": f"/app/evidence/{filename}",
                "width_px": obs_w,
                "height_px": obs_h,
                "reported_ink_untrusted": reported,
                "spectral_samples_lab": spectral,
                "fiducials": fiducials,
                "tone_wedge": tone_wedge,
            }
        )

    # Composite photographs are redundant evidence for overprint and white/varnish behavior.
    for composite_index, (x0, y0) in enumerate(tile_origins, 1):
        coefficients = tuple(rng.uniform(-1.8, 1.8) for _ in range(14))
        map_x, map_y = observation_mapping(grid_x, grid_y, x0, y0, obs_w, obs_h, coefficients)
        sampled = cv2.remap(
            cv2.cvtColor(proof, cv2.COLOR_RGB2BGR),
            map_x.astype(np.float32),
            map_y.astype(np.float32),
            interpolation=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=(0, 0, 0),
        )
        glare = np.zeros((obs_h, obs_w), dtype=np.float32)
        cv2.ellipse(
            glare,
            (rng.randint(250, 730), rng.randint(190, 480)),
            (rng.randint(90, 180), rng.randint(45, 100)),
            rng.uniform(-45, 45),
            0,
            360,
            rng.uniform(0.08, 0.16),
            -1,
        )
        sampled = sampled.astype(np.float32) / 255.0
        sampled = sampled * (1.0 - glare[..., None]) + glare[..., None]
        sampled = np.clip(sampled + np_rng.normal(0, 0.003, sampled.shape), 0, 1)
        alpha = np.full((obs_h, obs_w, 1), 255, dtype=np.uint8)
        composite_rgba = np.concatenate(
            [np.rint(sampled * 255).astype(np.uint8), alpha],
            axis=2,
        )
        filename = f"composite-{hashlib.sha256(f'composite:{composite_index}'.encode()).hexdigest()[:18]}.png"
        cv2.imwrite(str(evidence_dir / filename), composite_rgba)

    job_ticket = {
        "schema_version": 1,
        "authority": {
            "trusted": [
                "/app/input/job_ticket.json",
                "/app/input/dieline.svg",
                "/app/input/fonts/*",
                "/app/input/DYNAMO-SYNTH-CMYK-v1.icc",
            ],
            "untrusted": ["evidence filenames", "reported_ink_untrusted"],
        },
        "canvas": {
            "dpi": DPI,
            "width_px": WIDTH,
            "height_px": HEIGHT,
            "media_box_mm": [0.0, 0.0, WIDTH_MM, HEIGHT_MM],
            "trim_box_mm": [3.0, 3.0, 69.0, 45.0],
            "bleed_mm": 3.0,
        },
        "registration_model": {
            "mapping": "observation image pixels to master pixels",
            "basis": [
                "1",
                "u",
                "v",
                "u^2",
                "u*v",
                "v^2",
                "u^3",
                "u^2*v",
                "u*v^2",
                "v^3",
            ],
            "normalization": "u=2*x/(width_px-1)-1; v=2*y/(height_px-1)-1",
            "fiducials_per_scan": 42,
            "true_fiducials_per_scan": 34,
            "inlier_residual_px": 0.20,
        },
        "plate_registry": registry,
        "render_model": {
            "working_space": "linear RGB",
            "working_precision": "IEEE-754 binary32 (float32) for every linear-light arithmetic operation",
            "substrate_linear_rgb": [0.62, 0.43, 0.24],
            "opaque_white_linear_rgb": [0.96, 0.965, 0.95],
            "plate_order": ["OW", "C", "M", "Y", "K", "SC", "V"],
            "process_and_spot_rule": "rgb *= (1-coverage) + coverage*transmission_linear_rgb",
            "opaque_white_rule": "rgb = rgb*(1-coverage) + opaque_white_linear_rgb*coverage",
            "varnish_rule": "rgb += coverage*(1-rgb)*0.035",
            "coverage_scale": "uint8 0..255 maps linearly to 0..1",
            "encoding": "IEC 61966-2-1 sRGB transfer function",
        },
        "fonts": {
            "permitted_files": ["DejaVuSans.ttf", "DejaVuSans-Bold.ttf"],
            "text_objects": [
                {
                    key: value
                    for key, value in item.items()
                    if key not in {"text"}
                }
                for item in TEXT_OBJECTS
            ],
            "equivalence": {
                "used_glyph_outline_hausdorff_mm_max": 0.05,
                "advance_width_mm_max": 0.02,
                "text_must_remain_live": True,
                "selected_font_must_be_embedded": True,
            },
        },
        "barcode": {
            key: value
            for key, value in BARCODE.items()
            if key != "payload"
        },
        "dieline": {
            "source": "/app/input/dieline.svg",
            "roles_exact": ["cut", "crease", "perforation"],
            "path_symmetric_hausdorff_mm_max": 0.10,
            "junction_error_mm_max": 0.05,
        },
        "output_intent": {
            "identifier": "DYNAMO-SYNTH-CMYK-v1",
            "profile": "/app/input/DYNAMO-SYNTH-CMYK-v1.icc",
            "pdfx_version": "PDF/X-4",
            "separation_color_space": {
                "plate_ids": ["SC", "OW", "V"],
                "array_length": 4,
                "alternate_space_component_counts": {
                    "DeviceGray": 1,
                    "DeviceRGB": 3,
                    "DeviceCMYK": 4,
                },
                "tint_transform_function_type": 2,
                "tint_domain": [0.0, 1.0],
                "tint_exponent_min_exclusive": 0.0,
                "component_value_range": [0.0, 1.0],
                "require_nontrivial_endpoints": True,
                "require_painted_usage": True,
            },
        },
        "acceptance_tolerances": {
            "plate_solid_mask_iou_min": 0.995,
            "plate_area_error_fraction_max": 0.002,
            "plate_boundary_hausdorff_mm_max": 0.16,
            "plate_background_mean_absolute_coverage_max": 1.25,
            "plate_background_local_error_code_value_min": 1,
            "plate_background_local_component_area_mm2_max": 4.0,
            "plate_nominal_region_mean_absolute_coverage_max": 12.0,
            "plate_nominal_region_median_absolute_coverage_max": 1.0,
            "plate_nominal_local_error_code_value_min": 2,
            "plate_nominal_local_component_area_mm2_max": 4.0,
            "plate_nominal_local_total_area_mm2_max": 4.0,
            "proof_median_delta_e00_max": 1.0,
            "proof_p95_delta_e00_max": 2.5,
            "proof_color_edge_exclusion_px": 2,
            "proof_multiscale_ssim_min": 0.995,
            "proof_ssim_area_resample_scales": [2, 4, 8],
            "pdf_render_mean_absolute_rgb_max": 1.0,
            "pdf_render_local_window_mm": 4.0,
            "pdf_render_local_mean_absolute_rgb_max": 1.0,
        },
        "manifest_schema": {
            "top_level_exact_keys": [
                "schema_version",
                "canvas",
                "plates",
                "text_objects",
                "barcode",
                "pdf",
            ],
            "canvas_exact_keys": ["dpi", "width_px", "height_px", "trim_box_mm", "bleed_mm"],
            "plate_exact_keys": ["id", "canonical_name", "role", "source_observations"],
            "text_exact_keys": ["id", "text", "font_family", "font_style"],
            "barcode_exact_keys": ["id", "symbology", "payload"],
            "pdf_exact_keys": ["pdfx_version", "output_condition_identifier"],
            "plate_order": [item["id"] for item in PLATES],
            "source_observations_order": "ascending observation id",
            "text_order": [item["id"] for item in TEXT_OBJECTS],
        },
        "svg_contract": {
            "view_box": [0.0, 0.0, WIDTH_MM, HEIGHT_MM],
            "required_data_roles": [
                "process-art",
                "spot-art",
                "opaque-white",
                "varnish",
                "text",
                "barcode",
                "dieline",
            ],
            "semantic_layer_equivalence": "element order, group names, and nesting are ignored; each required role must be represented by data-role",
            "text_id_attribute": "data-id",
            "text_id_values": [item["id"] for item in TEXT_OBJECTS],
            "barcode_payload_attribute": "data-payload",
            "barcode_module_attribute": "data-module",
            "barcode_module_format": "zero-based 'row:column' of the QR module, counted from the top-left of the matrix",
            "text_position_tolerance_mm": 0.05,
            "font_weight_by_style": {"Regular": "400", "Bold": "700"},
            "font_size_tolerance_mm": 0.0001,
            "barcode_module_geometry_tolerance_mm": 0.05,
        },
    }
    (input_dir / "job_ticket.json").write_text(json.dumps(job_ticket, indent=2) + "\n")
    (evidence_dir / "index.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "plate_scan_count": len(records),
                "composite_count": 4,
                "observations": records,
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("/app"))
    args = parser.parse_args()
    build(args.root)
