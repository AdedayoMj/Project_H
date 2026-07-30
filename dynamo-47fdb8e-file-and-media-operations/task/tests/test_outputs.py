from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

import cv2
import fitz
import numpy as np
import pikepdf
import qrcode
from PIL import Image
from scipy.ndimage import binary_erosion, distance_transform_edt
from skimage.color import deltaE_ciede2000, rgb2lab
from skimage.metrics import structural_similarity


APP = Path(os.environ.get("CARTON_APP_ROOT", "/app"))
INPUT = APP / "input"
EVIDENCE = APP / "evidence"
OUTPUT = APP / "output"
REFERENCE = Path("/tests/reference_master.npz")
EXPECTED_SOURCE_SHA256 = "aaffcf531de4e9532ef28f96ac4616bfefdb9b2ead97a2b862230b3afc320084"
REQUIRED_FILES = {
    "plates.npz",
    "proof.png",
    "manifest.json",
    "production.svg",
    "production.pdf",
}
EXPECTED_TEXT = {
    "brand": "NORTHSTAR",
    "product": "SERUM 07",
    "volume": "30 mL",
    "lot": "LOT H7K4-29",
    "maker": "POLARIS LABS",
}
EXPECTED_BARCODE = "09506000123457|LOT-H7K4-29"
SOLID_THRESHOLDS = {"C": 128, "M": 128, "Y": 128, "K": 128, "SC": 128, "OW": 128, "V": 116}


def ticket() -> dict:
    return json.loads((INPUT / "job_ticket.json").read_text())


def evidence_index() -> dict:
    return json.loads((EVIDENCE / "index.json").read_text())


def source_digest() -> str:
    rows = []
    for root in (INPUT, EVIDENCE):
        for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(APP).as_posix().encode()):
            info = path.lstat()
            row = {
                "path": path.relative_to(APP).as_posix(),
                "mode": stat.S_IMODE(info.st_mode),
            }
            if stat.S_ISREG(info.st_mode):
                row.update(
                    kind="file",
                    size=info.st_size,
                    sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                )
            elif stat.S_ISDIR(info.st_mode):
                row["kind"] = "directory"
            elif stat.S_ISLNK(info.st_mode):
                row.update(kind="symlink", target=os.readlink(path))
            rows.append(row)
    payload = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def load_plates() -> dict[str, np.ndarray]:
    with np.load(OUTPUT / "plates.npz", allow_pickle=False) as archive:
        return {key: archive[key] for key in archive.files}


def render_from_plates(plates: dict[str, np.ndarray], spec: dict) -> np.ndarray:
    model = spec["render_model"]
    height = spec["canvas"]["height_px"]
    width = spec["canvas"]["width_px"]
    rgb = np.broadcast_to(
        np.asarray(model["substrate_linear_rgb"], dtype=np.float32),
        (height, width, 3),
    ).copy()
    white = plates["OW"].astype(np.float32)[..., None] / 255.0
    white_rgb = np.asarray(model["opaque_white_linear_rgb"], dtype=np.float32)
    rgb = rgb * (1.0 - white) + white_rgb * white
    for plate_id in ("C", "M", "Y", "K", "SC"):
        coverage = plates[plate_id].astype(np.float32)[..., None] / 255.0
        transmission = np.asarray(
            spec["plate_registry"][plate_id]["transmission_linear_rgb"],
            dtype=np.float32,
        )
        rgb *= 1.0 - coverage + coverage * transmission
    varnish = plates["V"].astype(np.float32)[..., None] / 255.0
    rgb += varnish * (1.0 - rgb) * 0.035
    srgb = np.where(
        rgb <= 0.0031308,
        12.92 * rgb,
        1.055 * np.power(np.clip(rgb, 0, 1), 1 / 2.4) - 0.055,
    )
    return np.clip(np.rint(srgb * 255), 0, 255).astype(np.uint8)


def expected_manifest() -> dict:
    spec = ticket()
    registry = spec["plate_registry"]
    sources: dict[str, list[str]] = defaultdict(list)
    for record in evidence_index()["observations"]:
        samples = np.asarray(record["spectral_samples_lab"], dtype=np.float64)
        robust = np.median(samples, axis=0)
        plate_id = min(
            registry,
            key=lambda key: float(
                np.linalg.norm(robust - np.asarray(registry[key]["reference_lab"], dtype=np.float64))
            ),
        )
        sources[plate_id].append(record["id"])
    for values in sources.values():
        values.sort()
    text_specs = {row["id"]: row for row in spec["fonts"]["text_objects"]}
    order = spec["manifest_schema"]["plate_order"]
    return {
        "schema_version": 1,
        "canvas": {
            "dpi": spec["canvas"]["dpi"],
            "width_px": spec["canvas"]["width_px"],
            "height_px": spec["canvas"]["height_px"],
            "trim_box_mm": spec["canvas"]["trim_box_mm"],
            "bleed_mm": spec["canvas"]["bleed_mm"],
        },
        "plates": [
            {
                "id": plate_id,
                "canonical_name": registry[plate_id]["canonical_name"],
                "role": registry[plate_id]["role"],
                "source_observations": sources[plate_id],
            }
            for plate_id in order
        ],
        "text_objects": [
            {
                "id": text_id,
                "text": EXPECTED_TEXT[text_id],
                "font_family": text_specs[text_id]["font_family"],
                "font_style": text_specs[text_id]["font_style"],
            }
            for text_id in spec["manifest_schema"]["text_order"]
        ],
        "barcode": {
            "id": spec["barcode"]["id"],
            "symbology": spec["barcode"]["symbology"],
            "payload": EXPECTED_BARCODE,
        },
        "pdf": {
            "pdfx_version": spec["output_intent"]["pdfx_version"],
            "output_condition_identifier": spec["output_intent"]["identifier"],
        },
    }


def parse_points(value: str) -> np.ndarray:
    return np.asarray(
        [[float(component) for component in pair.split(",")] for pair in value.split()],
        dtype=np.float64,
    )


def qr_modules(payload: str, spec: dict) -> set[tuple[int, int]]:
    code = qrcode.QRCode(
        version=spec["barcode"]["version"],
        error_correction=qrcode.constants.ERROR_CORRECT_Q,
        box_size=1,
        border=0,
    )
    code.add_data(payload)
    code.make(fit=False)
    return {
        (row, col)
        for row, values in enumerate(code.get_matrix())
        for col, enabled in enumerate(values)
        if enabled
    }


def test_required_artifacts_are_regular_files():
    """Every requested artifact exists at the documented path as a non-symlinked regular file."""
    assert OUTPUT.is_dir()
    assert {path.name for path in OUTPUT.iterdir() if path.is_file()} == REQUIRED_FILES
    for name in REQUIRED_FILES:
        path = OUTPUT / name
        assert path.exists()
        assert not path.is_symlink()
        assert stat.S_ISREG(path.stat().st_mode)
        assert path.stat().st_size > 100


def test_agent_visible_sources_are_unchanged_and_recoverability_is_present():
    """The evidence, job ticket, fonts, profile, and canonical dieline remain immutable and complete."""
    assert source_digest() == EXPECTED_SOURCE_SHA256
    spec = ticket()
    index = evidence_index()
    assert index["plate_scan_count"] == 56
    assert len(index["observations"]) == 56
    assert all(len(record["fiducials"]) == 42 for record in index["observations"])
    assert all(len(record["spectral_samples_lab"]) == 9 for record in index["observations"])
    assert spec["registration_model"]["true_fiducials_per_scan"] == 34
    assert set(spec["plate_registry"]) == {"C", "M", "Y", "K", "SC", "OW", "V"}


def test_plate_archive_schema_and_numeric_types():
    """The plate archive contains exactly seven canonical uint8 coverage planes on the production grid."""
    spec = ticket()
    plates = load_plates()
    assert list(plates) == spec["manifest_schema"]["plate_order"]
    shape = (spec["canvas"]["height_px"], spec["canvas"]["width_px"])
    for plate in plates.values():
        assert isinstance(plate, np.ndarray)
        assert plate.dtype == np.uint8
        assert plate.shape == shape


def test_reconstructed_plate_masks_match_hidden_master():
    """All solid masks meet the documented IoU and area tolerances against the hidden production master."""
    limits = ticket()["acceptance_tolerances"]
    submitted = load_plates()
    with np.load(REFERENCE, allow_pickle=False) as reference:
        for plate_id in ticket()["manifest_schema"]["plate_order"]:
            actual_mask = submitted[plate_id] >= SOLID_THRESHOLDS[plate_id]
            expected_mask = reference[plate_id] >= SOLID_THRESHOLDS[plate_id]
            intersection = np.count_nonzero(actual_mask & expected_mask)
            union = np.count_nonzero(actual_mask | expected_mask)
            iou = intersection / union
            area_error = abs(np.count_nonzero(actual_mask) - np.count_nonzero(expected_mask)) / np.count_nonzero(
                expected_mask
            )
            assert iou >= limits["plate_solid_mask_iou_min"], (plate_id, iou)
            assert area_error <= limits["plate_area_error_fraction_max"], (plate_id, area_error)


def test_reconstructed_plate_boundaries_match_as_geometry():
    """Equivalent rasterizations are accepted while each solid boundary stays within the 0.16 mm Hausdorff band."""
    spec = ticket()
    max_pixels = spec["acceptance_tolerances"]["plate_boundary_hausdorff_mm_max"] * spec["canvas"]["dpi"] / 25.4
    submitted = load_plates()
    with np.load(REFERENCE, allow_pickle=False) as reference:
        for plate_id in spec["manifest_schema"]["plate_order"]:
            actual = submitted[plate_id] >= SOLID_THRESHOLDS[plate_id]
            expected = reference[plate_id] >= SOLID_THRESHOLDS[plate_id]
            actual_boundary = actual ^ binary_erosion(actual)
            expected_boundary = expected ^ binary_erosion(expected)
            distance_to_expected = distance_transform_edt(~expected_boundary)
            distance_to_actual = distance_transform_edt(~actual_boundary)
            directed_a = float(distance_to_expected[actual_boundary].max())
            directed_b = float(distance_to_actual[expected_boundary].max())
            assert max(directed_a, directed_b) <= max_pixels, (plate_id, directed_a, directed_b)


def test_proof_is_colorimetrically_correct_and_derived_from_submitted_plates():
    """The proof follows the normative linear-light model and meets the stated ΔE00 and multiscale SSIM limits."""
    spec = ticket()
    proof = np.asarray(Image.open(OUTPUT / "proof.png").convert("RGB"))
    assert proof.shape == (spec["canvas"]["height_px"], spec["canvas"]["width_px"], 3)
    submitted = load_plates()
    derived = render_from_plates(submitted, spec)
    assert np.array_equal(proof, derived)
    with np.load(REFERENCE, allow_pickle=False) as reference:
        reference_plates = {key: reference[key] for key in reference.files}
        expected = render_from_plates(reference_plates, spec)
    delta = deltaE_ciede2000(rgb2lab(expected / 255.0), rgb2lab(proof / 255.0))
    limits = spec["acceptance_tolerances"]
    edge_union = np.zeros(delta.shape, dtype=np.uint8)
    for plate_id, plate in reference_plates.items():
        solid = plate >= SOLID_THRESHOLDS[plate_id]
        edge_union |= (solid ^ binary_erosion(solid)).astype(np.uint8)
    radius = limits["proof_color_edge_exclusion_px"]
    kernel = np.ones((radius * 2 + 1, radius * 2 + 1), dtype=np.uint8)
    color_region = cv2.dilate(edge_union, kernel) == 0
    assert float(np.median(delta[color_region])) <= limits["proof_median_delta_e00_max"]
    assert float(np.percentile(delta[color_region], 95)) <= limits["proof_p95_delta_e00_max"]
    scores = []
    for scale in limits["proof_ssim_area_resample_scales"]:
        size = (expected.shape[1] // scale, expected.shape[0] // scale)
        expected_scale = cv2.resize(expected, size, interpolation=cv2.INTER_AREA)
        proof_scale = cv2.resize(proof, size, interpolation=cv2.INTER_AREA)
        scores.append(
            structural_similarity(
                expected_scale,
                proof_scale,
                channel_axis=2,
                data_range=255,
            )
        )
    assert min(scores) >= limits["proof_multiscale_ssim_min"], scores


def test_manifest_is_exact_complete_and_auditable():
    """The normative manifest schema, canonical ink identities, source audit trail, live text, and PDF identifiers match."""
    submitted = json.loads((OUTPUT / "manifest.json").read_text())
    assert submitted == expected_manifest()


def test_svg_uses_semantic_equivalence_and_preserves_live_objects():
    """The editable overlay contains every semantic role, exact live content, equivalent dielines, and a vector QR matrix."""
    spec = ticket()
    root = ET.parse(OUTPUT / "production.svg").getroot()
    assert np.allclose(
        [float(value) for value in root.attrib["viewBox"].split()],
        spec["svg_contract"]["view_box"],
        atol=1e-9,
    )
    elements = list(root.iter())
    roles = {element.attrib["data-role"] for element in elements if "data-role" in element.attrib}
    assert set(spec["svg_contract"]["required_data_roles"]).issubset(roles)

    submitted_text = {}
    for element in elements:
        if element.tag.endswith("text") and "data-id" in element.attrib:
            submitted_text[element.attrib["data-id"]] = element
    assert set(submitted_text) == set(EXPECTED_TEXT)
    specs = {row["id"]: row for row in spec["fonts"]["text_objects"]}
    tolerance = spec["svg_contract"]["text_position_tolerance_mm"]
    for text_id, value in EXPECTED_TEXT.items():
        element = submitted_text[text_id]
        assert "".join(element.itertext()) == value
        assert element.attrib["font-family"] == specs[text_id]["font_family"]
        assert abs(float(element.attrib["x"]) - specs[text_id]["x_mm"]) <= tolerance
        assert abs(float(element.attrib["y"]) - specs[text_id]["y_mm"]) <= tolerance

    barcode_group = next(element for element in elements if element.attrib.get("data-role") == "barcode")
    assert barcode_group.attrib["data-payload"] == EXPECTED_BARCODE
    expected_modules = qr_modules(EXPECTED_BARCODE, spec)
    submitted_modules = {
        tuple(int(value) for value in element.attrib["data-module"].split(":"))
        for element in barcode_group
        if element.tag.endswith("rect") and "data-module" in element.attrib
    }
    assert submitted_modules == expected_modules
    matrix_size = max(max(row, col) for row, col in expected_modules) + 1
    module_size = spec["barcode"]["size_mm"] / matrix_size
    geometry_tolerance = spec["svg_contract"]["barcode_module_geometry_tolerance_mm"]
    for element in barcode_group:
        if not element.tag.endswith("rect") or "data-module" not in element.attrib:
            continue
        row, col = (int(value) for value in element.attrib["data-module"].split(":"))
        assert abs(float(element.attrib["x"]) - (spec["barcode"]["x_mm"] + col * module_size)) <= geometry_tolerance
        assert abs(float(element.attrib["y"]) - (spec["barcode"]["y_mm"] + row * module_size)) <= geometry_tolerance
        assert abs(float(element.attrib["width"]) - module_size) <= geometry_tolerance
        assert abs(float(element.attrib["height"]) - module_size) <= geometry_tolerance

    input_lines = {
        (element.attrib["data-kind"], element.attrib["data-index"]): parse_points(element.attrib["points"])
        for element in ET.parse(INPUT / "dieline.svg").getroot().iter()
        if element.tag.endswith("polyline")
    }
    output_lines = {
        (element.attrib["data-kind"], element.attrib["data-index"]): parse_points(element.attrib["points"])
        for element in elements
        if element.tag.endswith("polyline") and "data-kind" in element.attrib
    }
    assert set(output_lines) == set(input_lines)
    for key in input_lines:
        assert output_lines[key].shape == input_lines[key].shape
        assert float(np.max(np.linalg.norm(output_lines[key] - input_lines[key], axis=1))) <= spec["dieline"][
            "junction_error_mm_max"
        ]


def test_pdf_has_functional_production_structure_and_matching_render():
    """The PDF is parseable PDF/X-4 metadata with an ICC intent, embedded live text, spot spaces, OCG roles, and a matching proof."""
    spec = ticket()
    pdf_path = OUTPUT / "production.pdf"
    with pikepdf.open(pdf_path) as pdf:
        assert not pdf.is_encrypted
        assert float(pdf.pdf_version) >= 1.6
        assert len(pdf.pages) == 1
        assert str(pdf.docinfo["/GTS_PDFXVersion"]) == spec["output_intent"]["pdfx_version"]
        intents = pdf.Root["/OutputIntents"]
        assert len(intents) == 1
        assert str(intents[0]["/S"]) == "/GTS_PDFX"
        assert str(intents[0]["/OutputConditionIdentifier"]) == spec["output_intent"]["identifier"]
        assert int(intents[0]["/DestOutputProfile"]["/N"]) == 4

        page = pdf.pages[0]
        expected_media = [value * 72.0 / 25.4 for value in spec["canvas"]["media_box_mm"]]
        expected_trim = [value * 72.0 / 25.4 for value in spec["canvas"]["trim_box_mm"]]
        assert np.allclose([float(value) for value in page.MediaBox], expected_media, atol=0.01)
        assert np.allclose([float(value) for value in page.TrimBox], expected_trim, atol=0.01)
        assert np.allclose([float(value) for value in page.BleedBox], expected_media, atol=0.01)

        color_spaces = page.Resources["/ColorSpace"]
        for plate_id in ("SC", "OW", "V"):
            separation = color_spaces[f"/CS_{plate_id}"]
            assert str(separation[0]) == "/Separation"
            expected_name = "/" + spec["plate_registry"][plate_id]["canonical_name"]
            assert str(separation[1]) == expected_name
        ocg_names = {str(item["/Name"]) for item in pdf.Root["/OCProperties"]["/OCGs"]}
        assert set(spec["svg_contract"]["required_data_roles"]).issubset(ocg_names)

        embedded_font_count = 0
        fonts = page.Resources.get("/Font")
        assert fonts is not None
        for _, font in fonts.items():
            resolved = font
            descendants = resolved.get("/DescendantFonts", [])
            candidates = list(descendants) if descendants else [resolved]
            for candidate in candidates:
                descriptor = candidate.get("/FontDescriptor")
                if descriptor and any(key in descriptor for key in ("/FontFile", "/FontFile2", "/FontFile3")):
                    embedded_font_count += 1
        assert embedded_font_count >= 2

    document = fitz.open(pdf_path)
    page = document[0]
    extracted = page.get_text()
    for value in EXPECTED_TEXT.values():
        assert value in extracted
    scale = spec["canvas"]["dpi"] / 72.0
    pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False, colorspace=fitz.csRGB)
    rendered = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(pixmap.height, pixmap.width, 3)
    document.close()
    proof = np.asarray(Image.open(OUTPUT / "proof.png").convert("RGB"))
    assert rendered.shape == proof.shape
    mean_absolute = float(np.mean(np.abs(rendered.astype(np.int16) - proof.astype(np.int16))))
    assert mean_absolute <= spec["acceptance_tolerances"]["pdf_render_mean_absolute_rgb_max"], mean_absolute
