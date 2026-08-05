#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import os
import random
import shutil
import tempfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from xml.sax.saxutils import quoteattr

import cv2
import numpy as np
import pikepdf
import qrcode
from PIL import Image
from pikepdf import Array, Dictionary, Name, String
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas


APP = Path(os.environ.get("CARTON_APP_ROOT", "/app"))
INPUT = APP / "input"
EVIDENCE = APP / "evidence"
OUTPUT = APP / "output"


def prepare_output_directory(path: Path) -> None:
    """Create an empty output directory without following directory symlinks."""
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        path.unlink()
    path.mkdir(parents=True, exist_ok=True)
    for entry in path.iterdir():
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()


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


def consensus(candidates: list[str]) -> str:
    lengths = defaultdict(int)
    for value in candidates:
        lengths[len(value)] += 1
    length = max(lengths, key=lambda item: (lengths[item], item))
    selected = [value for value in candidates if len(value) == length]
    result = []
    for index in range(length):
        counts = defaultdict(int)
        for value in selected:
            counts[value[index]] += 1
        result.append(max(counts, key=lambda char: (counts[char], -ord(char))))
    return "".join(result)


def classify_plate(record: dict, registry: dict) -> str:
    samples = np.asarray(record["spectral_samples_lab"], dtype=np.float64)
    robust_lab = np.median(samples, axis=0)
    return min(
        registry,
        key=lambda plate_id: float(
            np.linalg.norm(robust_lab - np.asarray(registry[plate_id]["reference_lab"], dtype=np.float64))
        ),
    )


def fit_registration(
    record: dict,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    width = record["width_px"]
    height = record["height_px"]
    image_points = np.asarray([row["image_px"] for row in record["fiducials"]], dtype=np.float64)
    master_points = np.asarray([row["master_px"] for row in record["fiducials"]], dtype=np.float64)
    u = 2.0 * image_points[:, 0] / (width - 1) - 1.0
    v = 2.0 * image_points[:, 1] / (height - 1) - 1.0
    basis = design_matrix(u, v)

    seed = sum(ord(char) for char in record["id"]) * 7919
    rng = random.Random(seed)
    best: tuple[int, float, np.ndarray] | None = None
    indices = list(range(len(image_points)))
    for _ in range(4000):
        sample = rng.sample(indices, 10)
        coefficients, *_ = np.linalg.lstsq(basis[sample], master_points[sample], rcond=None)
        residuals = np.linalg.norm(basis @ coefficients - master_points, axis=1)
        inliers = residuals < 0.20
        score = (int(inliers.sum()), -float(np.median(residuals[inliers])) if inliers.any() else -1e9)
        if best is None or score[:2] > best[:2]:
            best = (score[0], score[1], inliers)
        if best[0] == 34:
            break
    assert best is not None and best[0] == 34, f"registration failed for {record['id']}"

    forward, *_ = np.linalg.lstsq(basis[best[2]], master_points[best[2]], rcond=None)
    residuals = np.linalg.norm(basis @ forward - master_points, axis=1)
    keep_count = 34
    keep = np.argsort(residuals)[:keep_count]
    forward, *_ = np.linalg.lstsq(basis[keep], master_points[keep], rcond=None)

    inlier_master = master_points[keep]
    center = (inlier_master.min(axis=0) + inlier_master.max(axis=0)) / 2.0
    scale = (inlier_master.max(axis=0) - inlier_master.min(axis=0)) / 2.0
    inverse_basis = design_matrix(
        (inlier_master[:, 0] - center[0]) / scale[0],
        (inlier_master[:, 1] - center[1]) / scale[1],
    )
    inverse, *_ = np.linalg.lstsq(inverse_basis, image_points[keep], rcond=None)
    return forward, inverse, center, scale, inlier_master


def calibrate_density(level: np.ndarray, record: dict) -> np.ndarray:
    groups: dict[int, list[float]] = defaultdict(list)
    for sample in record["tone_wedge"]:
        groups[int(sample["true_coverage"])].append(float(sample["observed_level"]))
    coverages = np.asarray(sorted(groups), dtype=np.float64)
    observed = np.asarray([np.median(groups[int(value)]) for value in coverages], dtype=np.float64)
    normalized = coverages / 255.0
    best: tuple[float, float, float, float] | None = None
    for gamma in np.linspace(0.70, 1.30, 601):
        matrix = np.column_stack([np.ones_like(normalized), np.power(normalized, gamma)])
        coefficients, *_ = np.linalg.lstsq(matrix, observed, rcond=None)
        prediction = matrix @ coefficients
        error = float(np.median(np.abs(prediction - observed)))
        if best is None or error < best[0]:
            best = (error, gamma, float(coefficients[0]), float(coefficients[1]))
    assert best is not None
    _, gamma, paper, slope = best
    solid = paper + slope
    fraction = np.clip((paper - level.astype(np.float32)) / (paper - solid), 0.0, 1.0)
    return (255.0 * np.power(fraction, 1.0 / gamma)).astype(np.float32)


def invert_forward_map(
    target_x: np.ndarray,
    target_y: np.ndarray,
    forward: np.ndarray,
    inverse: np.ndarray,
    center: np.ndarray,
    scale: np.ndarray,
    width: int,
    height: int,
) -> tuple[np.ndarray, np.ndarray]:
    initial_basis = design_matrix(
        ((target_x - center[0]) / scale[0]).ravel(),
        ((target_y - center[1]) / scale[1]).ravel(),
    )
    initial = initial_basis @ inverse
    u = 2.0 * initial[:, 0] / (width - 1) - 1.0
    v = 2.0 * initial[:, 1] / (height - 1) - 1.0
    target_mx = target_x.ravel()
    target_my = target_y.ravel()
    cx = forward[:, 0]
    cy = forward[:, 1]
    for _ in range(4):
        basis = design_matrix(u, v)
        error_x = basis @ cx - target_mx
        error_y = basis @ cy - target_my
        dx_du = cx[1] + 2 * cx[3] * u + cx[4] * v + 3 * cx[6] * u * u + 2 * cx[7] * u * v + cx[8] * v * v
        dx_dv = cx[2] + cx[4] * u + 2 * cx[5] * v + cx[7] * u * u + 2 * cx[8] * u * v + 3 * cx[9] * v * v
        dy_du = cy[1] + 2 * cy[3] * u + cy[4] * v + 3 * cy[6] * u * u + 2 * cy[7] * u * v + cy[8] * v * v
        dy_dv = cy[2] + cy[4] * u + 2 * cy[5] * v + cy[7] * u * u + 2 * cy[8] * u * v + 3 * cy[9] * v * v
        determinant = dx_du * dy_dv - dx_dv * dy_du
        delta_u = (error_x * dy_dv - error_y * dx_dv) / determinant
        delta_v = (dx_du * error_y - dy_du * error_x) / determinant
        u -= delta_u
        v -= delta_v
    map_x = ((u + 1.0) * (width - 1) / 2.0).reshape(target_x.shape).astype(np.float32)
    map_y = ((v + 1.0) * (height - 1) / 2.0).reshape(target_y.shape).astype(np.float32)
    return map_x, map_y


def reconstruct_plates(ticket: dict, index: dict) -> tuple[dict[str, np.ndarray], dict[str, list[str]]]:
    width = ticket["canvas"]["width_px"]
    height = ticket["canvas"]["height_px"]
    registry = ticket["plate_registry"]
    sums = {plate_id: np.zeros((height, width), dtype=np.float64) for plate_id in registry}
    weights = {plate_id: np.zeros((height, width), dtype=np.float64) for plate_id in registry}
    sources: dict[str, list[str]] = {plate_id: [] for plate_id in registry}

    for record in index["observations"]:
        plate_id = classify_plate(record, registry)
        sources[plate_id].append(record["id"])
        image = cv2.imread(record["file"], cv2.IMREAD_UNCHANGED)
        if image is None or image.ndim != 3 or image.shape[2] != 4:
            raise ValueError(f"cannot read RGBA plate scan {record['file']}")
        level = image[:, :, 0]
        alpha = image[:, :, 3]
        coverage = calibrate_density(level, record)
        forward, inverse, center, scale, inlier_master = fit_registration(record)

        x_min = max(0, math.floor(float(inlier_master[:, 0].min()) - 75))
        x_max = min(width, math.ceil(float(inlier_master[:, 0].max()) + 75))
        y_min = max(0, math.floor(float(inlier_master[:, 1].min()) - 75))
        y_max = min(height, math.ceil(float(inlier_master[:, 1].max()) + 75))
        target_y, target_x = np.indices((y_max - y_min, x_max - x_min), dtype=np.float64)
        target_x += x_min
        target_y += y_min
        map_x, map_y = invert_forward_map(
            target_x,
            target_y,
            forward,
            inverse,
            center,
            scale,
            record["width_px"],
            record["height_px"],
        )
        warped = cv2.remap(
            coverage,
            map_x,
            map_y,
            interpolation=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        )
        validity = cv2.remap(
            alpha,
            map_x,
            map_y,
            interpolation=cv2.INTER_NEAREST,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        )
        valid = (validity >= 250).astype(np.float64)
        target_sum = sums[plate_id][y_min:y_max, x_min:x_max]
        target_weight = weights[plate_id][y_min:y_max, x_min:x_max]
        target_sum += warped * valid
        target_weight += valid

    result: dict[str, np.ndarray] = {}
    for plate_id in registry:
        fused = np.divide(
            sums[plate_id],
            weights[plate_id],
            out=np.zeros_like(sums[plate_id]),
            where=weights[plate_id] > 0,
        )
        # The evidence describes printed plate percentages. Suppress sub-one-percent
        # scanner haze while retaining intentional tints and antialiased boundaries.
        fused[fused < 2.5] = 0.0
        fused[fused > 252.5] = 255.0
        for nominal in registry[plate_id]["nominal_coverage_levels"]:
            close = np.abs(fused - nominal) <= 18.0
            fused[close] = nominal
        result[plate_id] = np.clip(np.rint(fused), 0, 255).astype(np.uint8)
        sources[plate_id].sort()
    return result, sources


def linear_to_srgb(values: np.ndarray) -> np.ndarray:
    return np.where(
        values <= 0.0031308,
        12.92 * values,
        1.055 * np.power(values, 1.0 / 2.4) - 0.055,
    )


def render_proof(plates: dict[str, np.ndarray], ticket: dict) -> np.ndarray:
    model = ticket["render_model"]
    height = ticket["canvas"]["height_px"]
    width = ticket["canvas"]["width_px"]
    rgb = np.broadcast_to(
        np.asarray(model["substrate_linear_rgb"], dtype=np.float32),
        (height, width, 3),
    ).copy()
    white = plates["OW"].astype(np.float32)[..., None] / 255.0
    white_rgb = np.asarray(model["opaque_white_linear_rgb"], dtype=np.float32)
    rgb = rgb * (1.0 - white) + white_rgb * white
    for plate_id in ("C", "M", "Y", "K", "SC"):
        amount = plates[plate_id].astype(np.float32)[..., None] / 255.0
        transmission = np.asarray(
            ticket["plate_registry"][plate_id]["transmission_linear_rgb"],
            dtype=np.float32,
        )
        rgb *= 1.0 - amount + amount * transmission
    varnish = plates["V"].astype(np.float32)[..., None] / 255.0
    rgb += varnish * (1.0 - rgb) * 0.035
    return np.clip(np.rint(linear_to_srgb(np.clip(rgb, 0, 1)) * 255), 0, 255).astype(np.uint8)


def make_manifest(
    ticket: dict,
    sources: dict[str, list[str]],
    text_values: dict[str, str],
    barcode_payload: str,
) -> dict:
    schema = ticket["manifest_schema"]
    registry = ticket["plate_registry"]
    text_specs = {row["id"]: row for row in ticket["fonts"]["text_objects"]}
    return {
        "schema_version": 1,
        "canvas": {
            "dpi": ticket["canvas"]["dpi"],
            "width_px": ticket["canvas"]["width_px"],
            "height_px": ticket["canvas"]["height_px"],
            "trim_box_mm": ticket["canvas"]["trim_box_mm"],
            "bleed_mm": ticket["canvas"]["bleed_mm"],
        },
        "plates": [
            {
                "id": plate_id,
                "canonical_name": registry[plate_id]["canonical_name"],
                "role": registry[plate_id]["role"],
                "source_observations": sources[plate_id],
            }
            for plate_id in schema["plate_order"]
        ],
        "text_objects": [
            {
                "id": text_id,
                "text": text_values[text_id],
                "font_family": text_specs[text_id]["font_family"],
                "font_style": text_specs[text_id]["font_style"],
            }
            for text_id in schema["text_order"]
        ],
        "barcode": {
            "id": ticket["barcode"]["id"],
            "symbology": ticket["barcode"]["symbology"],
            "payload": barcode_payload,
        },
        "pdf": {
            "pdfx_version": ticket["output_intent"]["pdfx_version"],
            "output_condition_identifier": ticket["output_intent"]["identifier"],
        },
    }


def qr_matrix(payload: str, ticket: dict) -> list[list[bool]]:
    code = qrcode.QRCode(
        version=ticket["barcode"]["version"],
        error_correction=qrcode.constants.ERROR_CORRECT_Q,
        box_size=1,
        border=0,
    )
    code.add_data(payload)
    code.make(fit=False)
    return code.get_matrix()


def write_svg(ticket: dict, text_values: dict[str, str], barcode_payload: str, path: Path) -> None:
    width_mm = ticket["canvas"]["media_box_mm"][2]
    height_mm = ticket["canvas"]["media_box_mm"][3]
    concrete_roles = {"text", "barcode", "dieline"}
    role_lines = [
        f"  <g data-role={quoteattr(role)}><rect x=\"0\" y=\"0\" "
        f"width=\"{width_mm}\" height=\"{height_mm}\" fill=\"none\"/></g>"
        for role in ticket["svg_contract"]["required_data_roles"]
        if role not in concrete_roles
    ]
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width_mm}mm" height="{height_mm}mm" viewBox="0 0 {width_mm} {height_mm}">',
        *role_lines,
        '  <g data-role="text" fill="#000000">',
    ]
    for item in ticket["fonts"]["text_objects"]:
        value = (
            text_values[item["id"]]
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        weight = ticket["svg_contract"]["font_weight_by_style"][item["font_style"]]
        lines.append(
            f'    <text data-id="{item["id"]}" x="{item["x_mm"]:.3f}" y="{item["y_mm"]:.3f}" '
            f'font-family="{item["font_family"]}" font-weight="{weight}" '
            f'font-size="{item["font_size_pt"] * 25.4 / 72.0:.5f}">{value}</text>'
        )
    lines.append("  </g>")

    matrix = qr_matrix(barcode_payload, ticket)
    barcode = ticket["barcode"]
    module = barcode["size_mm"] / len(matrix)
    lines.append(
        f'  <g data-role="barcode" data-id="{barcode["id"]}" data-symbology="{barcode["symbology"]}" '
        f'data-payload="{barcode_payload}" fill="#000000">'
    )
    for row, values in enumerate(matrix):
        for col, enabled in enumerate(values):
            if enabled:
                lines.append(
                    f'    <rect data-module="{row}:{col}" x="{barcode["x_mm"] + col * module:.5f}" '
                    f'y="{barcode["y_mm"] + row * module:.5f}" width="{module:.5f}" height="{module:.5f}"/>'
                )
    lines.append("  </g>")

    input_root = ET.parse(INPUT / "dieline.svg").getroot()
    lines.append('  <g data-role="dieline" fill="none">')
    for element in input_root.iter():
        if element.tag.endswith("polyline"):
            attrs = " ".join(
                f'{key}="{value}"'
                for key, value in element.attrib.items()
                if key in {"data-kind", "data-index", "points", "stroke", "stroke-width", "stroke-dasharray"}
            )
            lines.append(f"    <polyline {attrs}/>")
    lines.extend(["  </g>", "</svg>", ""])
    path.write_text("\n".join(lines))


def write_pdf(
    ticket: dict,
    proof_path: Path,
    text_values: dict[str, str],
    destination: Path,
) -> None:
    width_mm = ticket["canvas"]["media_box_mm"][2]
    height_mm = ticket["canvas"]["media_box_mm"][3]
    page_width = width_mm * 72.0 / 25.4
    page_height = height_mm * 72.0 / 25.4
    with tempfile.TemporaryDirectory() as temporary:
        base_path = Path(temporary) / "base.pdf"
        document = canvas.Canvas(
            str(base_path),
            pagesize=(page_width, page_height),
            pageCompression=1,
            pdfVersion=(1, 6),
        )
        document.setTitle("Northstar folding-carton production proof")
        document.setCreator("Dynamo deterministic reconstruction")
        document.drawImage(
            str(proof_path),
            0,
            0,
            width=page_width,
            height=page_height,
            preserveAspectRatio=False,
            mask=None,
        )
        registered = {}
        for font_file in ticket["fonts"]["permitted_files"]:
            name = "Dynamo-" + font_file.replace(".ttf", "")
            pdfmetrics.registerFont(TTFont(name, str(INPUT / "fonts" / font_file)))
            registered[font_file] = name
        text_object = document.beginText()
        text_object.setTextRenderMode(3)
        for item in ticket["fonts"]["text_objects"]:
            font_name = registered[item["font_file"]]
            text_object.setFont(font_name, item["font_size_pt"])
            x = item["x_mm"] * 72.0 / 25.4
            y = page_height - item["y_mm"] * 72.0 / 25.4 - item["font_size_pt"]
            text_object.setTextOrigin(x, y)
            text_object.textLine(text_values[item["id"]])
        document.drawText(text_object)
        document.showPage()
        document.save()

        with pikepdf.open(base_path) as pdf:
            pdf.docinfo["/GTS_PDFXVersion"] = ticket["output_intent"]["pdfx_version"]
            pdf.docinfo["/Title"] = "Northstar folding-carton production proof"
            profile = pdf.make_stream(Path(ticket["output_intent"]["profile"]).read_bytes())
            profile["/N"] = 4
            output_intent = pdf.make_indirect(
                Dictionary(
                    Type=Name("/OutputIntent"),
                    S=Name("/GTS_PDFX"),
                    OutputConditionIdentifier=String(ticket["output_intent"]["identifier"]),
                    RegistryName=String("https://www.color.org"),
                    DestOutputProfile=profile,
                )
            )
            pdf.Root["/OutputIntents"] = Array([output_intent])

            page = pdf.pages[0]
            media = ticket["canvas"]["media_box_mm"]
            trim = ticket["canvas"]["trim_box_mm"]
            page.MediaBox = Array([value * 72.0 / 25.4 for value in media])
            page.TrimBox = Array([value * 72.0 / 25.4 for value in trim])
            page.BleedBox = Array([value * 72.0 / 25.4 for value in media])
            resources = page.Resources
            color_spaces = Dictionary()
            separation_usage = []
            for plate_id in ("SC", "OW", "V"):
                registry = ticket["plate_registry"][plate_id]
                function = pdf.make_indirect(
                    Dictionary(
                        FunctionType=2,
                        Domain=Array([0.0, 1.0]),
                        C0=Array([0.0, 0.0, 0.0, 0.0]),
                        C1=Array([0.15, 0.55, 0.75, 0.12]),
                        N=1.0,
                    )
                )
                color_spaces[Name("/CS_" + plate_id)] = Array(
                    [
                        Name("/Separation"),
                        Name("/" + registry["canonical_name"]),
                        Name("/DeviceCMYK"),
                        function,
                    ]
                )
                separation_usage.extend(
                    [
                        "q",
                        f"/CS_{plate_id} cs",
                        "1 scn",
                        "0 0 0 0 re f",
                        "Q",
                    ]
                )
            resources["/ColorSpace"] = color_spaces

            roles = ticket["svg_contract"]["required_data_roles"]
            ocgs = []
            properties = Dictionary()
            marked = []
            for index, role in enumerate(roles):
                ocg = pdf.make_indirect(Dictionary(Type=Name("/OCG"), Name=String(role)))
                ocgs.append(ocg)
                property_name = Name(f"/DYN_LAYER_{index}")
                properties[property_name] = ocg
                marked.append(f"/OC {property_name} BDC EMC")
            resources["/Properties"] = properties
            pdf.Root["/OCProperties"] = Dictionary(
                OCGs=Array(ocgs),
                D=Dictionary(Order=Array(ocgs), ON=Array(ocgs)),
            )
            marker_stream = pdf.make_stream(
                ("\n".join([*marked, *separation_usage]) + "\n").encode()
            )
            current = page.Contents
            if isinstance(current, pikepdf.Array):
                page.Contents = Array([marker_stream, *current])
            else:
                page.Contents = Array([marker_stream, current])

            pdfx_version = ticket["output_intent"]["pdfx_version"]
            xmp = (
                '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>'
                '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
                '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
                '<rdf:Description xmlns:pdfxid="http://www.npes.org/pdfx/ns/id/" '
                f'pdfxid:GTS_PDFXVersion="{pdfx_version}"/>'
                "</rdf:RDF></x:xmpmeta><?xpacket end=\"w\"?>"
            ).encode("utf-8")
            metadata = pdf.make_stream(xmp)
            metadata["/Type"] = Name("/Metadata")
            metadata["/Subtype"] = Name("/XML")
            pdf.Root["/Metadata"] = metadata
            pdf.save(destination, min_version="1.6", fix_metadata_version=True)


def main() -> None:
    prepare_output_directory(OUTPUT)
    ticket = json.loads((INPUT / "job_ticket.json").read_text())
    index = json.loads((EVIDENCE / "index.json").read_text())
    consensus_evidence = json.loads((EVIDENCE / "text_consensus.json").read_text())
    text_values = {
        row["id"]: consensus(row["candidates"])
        for row in consensus_evidence["text_objects"]
    }
    barcode_payload = consensus(consensus_evidence["barcode"]["candidates"])

    plates, sources = reconstruct_plates(ticket, index)
    plate_order = ticket["manifest_schema"]["plate_order"]
    np.savez_compressed(OUTPUT / "plates.npz", **{plate_id: plates[plate_id] for plate_id in plate_order})
    proof = render_proof(plates, ticket)
    proof_path = OUTPUT / "proof.png"
    Image.fromarray(proof, mode="RGB").save(proof_path, optimize=True)
    manifest = make_manifest(ticket, sources, text_values, barcode_payload)
    (OUTPUT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    write_svg(ticket, text_values, barcode_payload, OUTPUT / "production.svg")
    write_pdf(ticket, proof_path, text_values, OUTPUT / "production.pdf")


if __name__ == "__main__":
    main()
