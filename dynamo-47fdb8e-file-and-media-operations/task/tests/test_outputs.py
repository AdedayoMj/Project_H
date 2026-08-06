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
EXPECTED_SOURCE_SHA256 = "f63f3a20a7cfeac9bbbfd537dc1413b91599b71802f6beb05b75be0c76ae1fe9"
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


def largest_component_area_mm2(mask: np.ndarray, dpi: float) -> float:
    """Measure the largest 8-connected defect in physical production units."""
    component_count, _, stats, _ = cv2.connectedComponentsWithStats(
        mask.astype(np.uint8), connectivity=8
    )
    largest_pixels = (
        int(stats[1:, cv2.CC_STAT_AREA].max()) if component_count > 1 else 0
    )
    return largest_pixels * (25.4 / dpi) ** 2


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
    plate_order = spec["manifest_schema"]["plate_order"]
    classification = spec["observation_classification"]
    sources: dict[str, list[str]] = defaultdict(list)
    for record in evidence_index()["observations"]:
        samples = np.asarray(record[classification["sample_field"]], dtype=np.float64)
        assert samples.shape == (classification["sample_count"], 3)
        assert classification["aggregation"] == "componentwise_median"
        assert classification["distance_metric"] == "euclidean_cie_lab"
        assert classification["assignment"] == "minimum_distance"
        assert classification["tie_break_order"] == "manifest_schema.plate_order"
        robust = np.median(samples, axis=0)
        plate_id = min(
            plate_order,
            key=lambda key: float(
                np.linalg.norm(robust - np.asarray(registry[key]["reference_lab"], dtype=np.float64))
            ),
        )
        sources[plate_id].append(record["id"])
    for values in sources.values():
        values.sort()
    text_specs = {row["id"]: row for row in spec["fonts"]["text_objects"]}
    order = plate_order
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
    assert {path.name for path in OUTPUT.iterdir()} == REQUIRED_FILES
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
    assert spec["observation_classification"] == {
        "sample_field": "spectral_samples_lab",
        "sample_count": 9,
        "aggregation": "componentwise_median",
        "aggregation_definition": "take the median independently across all nine samples for L*, a*, and b*",
        "distance_metric": "euclidean_cie_lab",
        "reference_field": "plate_registry.<plate_id>.reference_lab",
        "assignment": "minimum_distance",
        "tie_break_order": "manifest_schema.plate_order",
    }
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


def test_reconstructed_plate_coverage_and_nominal_tones_match_hidden_master():
    """Every declared tone region preserves coverage values, including zero-ink backgrounds and localized tints."""
    spec = ticket()
    limits = spec["acceptance_tolerances"]
    submitted = load_plates()
    with np.load(REFERENCE, allow_pickle=False) as reference:
        for plate_id in spec["manifest_schema"]["plate_order"]:
            actual = submitted[plate_id].astype(np.int16)
            expected = reference[plate_id].astype(np.int16)
            absolute_error = np.abs(actual - expected)
            for nominal in spec["plate_registry"][plate_id]["nominal_coverage_levels"]:
                region = expected == nominal
                assert np.count_nonzero(region) > 0, (plate_id, nominal)
                region_error = absolute_error[region]
                mean_error = float(np.mean(region_error))
                median_error = float(np.median(region_error))
                mean_limit = (
                    limits["plate_background_mean_absolute_coverage_max"]
                    if nominal == 0
                    else limits["plate_nominal_region_mean_absolute_coverage_max"]
                )
                assert mean_error <= mean_limit, (plate_id, nominal, mean_error)
                assert median_error <= limits[
                    "plate_nominal_region_median_absolute_coverage_max"
                ], (plate_id, nominal, median_error)


def test_zero_ink_background_has_no_localized_contamination():
    """No spatially coherent or distributed background defect may hide inside an acceptable global mean."""
    spec = ticket()
    limits = spec["acceptance_tolerances"]
    submitted = load_plates()
    with np.load(REFERENCE, allow_pickle=False) as reference:
        for plate_id in spec["manifest_schema"]["plate_order"]:
            actual = submitted[plate_id].astype(np.int16)
            expected = reference[plate_id].astype(np.int16)
            substantial_error = (
                (expected == 0)
                & (
                    np.abs(actual - expected)
                    >= limits["plate_background_local_error_code_value_min"]
                )
            )
            largest_area = largest_component_area_mm2(
                substantial_error, float(spec["canvas"]["dpi"])
            )
            total_area = float(np.count_nonzero(substantial_error)) * (
                25.4 / float(spec["canvas"]["dpi"])
            ) ** 2
            assert largest_area <= limits[
                "plate_background_local_component_area_mm2_max"
            ], (plate_id, largest_area)
            assert total_area <= limits[
                "plate_background_local_total_area_mm2_max"
            ], (plate_id, total_area)


def test_nominal_ink_regions_have_no_localized_tone_damage():
    """No coherent or distributed interior defect may hide inside a nominal tone region."""
    spec = ticket()
    limits = spec["acceptance_tolerances"]
    submitted = load_plates()
    edge_exclusion = max(
        1,
        math.ceil(
            limits["plate_boundary_hausdorff_mm_max"]
            * float(spec["canvas"]["dpi"])
            / 25.4
        ),
    )
    with np.load(REFERENCE, allow_pickle=False) as reference:
        for plate_id in spec["manifest_schema"]["plate_order"]:
            actual = submitted[plate_id].astype(np.int16)
            expected = reference[plate_id].astype(np.int16)
            absolute_error = np.abs(actual - expected)
            for nominal in spec["plate_registry"][plate_id]["nominal_coverage_levels"]:
                if nominal == 0:
                    continue
                interior = binary_erosion(expected == nominal, iterations=edge_exclusion)
                if not np.any(interior):
                    continue
                substantial_error = interior & (
                    absolute_error
                    >= limits["plate_nominal_local_error_code_value_min"]
                )
                largest_area = largest_component_area_mm2(
                    substantial_error, float(spec["canvas"]["dpi"])
                )
                total_area = float(np.count_nonzero(substantial_error)) * (
                    25.4 / float(spec["canvas"]["dpi"])
                ) ** 2
                assert largest_area <= limits[
                    "plate_nominal_local_component_area_mm2_max"
                ], (plate_id, nominal, largest_area)
                assert total_area <= limits[
                    "plate_nominal_local_total_area_mm2_max"
                ], (plate_id, nominal, total_area)


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

    text_id_attribute = spec["svg_contract"]["text_id_attribute"]
    payload_attribute = spec["svg_contract"]["barcode_payload_attribute"]
    module_attribute = spec["svg_contract"]["barcode_module_attribute"]
    submitted_text = {}
    for element in elements:
        if element.tag.endswith("text") and text_id_attribute in element.attrib:
            submitted_text[element.attrib[text_id_attribute]] = element
    assert set(submitted_text) == set(EXPECTED_TEXT)
    specs = {row["id"]: row for row in spec["fonts"]["text_objects"]}
    tolerance = spec["svg_contract"]["text_position_tolerance_mm"]
    for text_id, value in EXPECTED_TEXT.items():
        element = submitted_text[text_id]
        assert "".join(element.itertext()) == value
        assert element.attrib["font-family"] == specs[text_id]["font_family"]
        expected_weight = spec["svg_contract"]["font_weight_by_style"][
            specs[text_id]["font_style"]
        ]
        assert element.attrib["font-weight"] == expected_weight
        expected_font_size_mm = specs[text_id]["font_size_pt"] * 25.4 / 72.0
        assert abs(float(element.attrib["font-size"]) - expected_font_size_mm) <= spec[
            "svg_contract"
        ]["font_size_tolerance_mm"]
        assert abs(float(element.attrib["x"]) - specs[text_id]["x_mm"]) <= tolerance
        assert abs(float(element.attrib["y"]) - specs[text_id]["y_mm"]) <= tolerance

    barcode_group = next(element for element in elements if element.attrib.get("data-role") == "barcode")
    assert barcode_group.attrib[payload_attribute] == EXPECTED_BARCODE
    expected_modules = qr_modules(EXPECTED_BARCODE, spec)
    submitted_modules = {
        tuple(int(value) for value in element.attrib[module_attribute].split(":"))
        for element in barcode_group
        if element.tag.endswith("rect") and module_attribute in element.attrib
    }
    assert submitted_modules == expected_modules
    matrix_size = max(max(row, col) for row, col in expected_modules) + 1
    module_size = spec["barcode"]["size_mm"] / matrix_size
    geometry_tolerance = spec["svg_contract"]["barcode_module_geometry_tolerance_mm"]
    for element in barcode_group:
        if not element.tag.endswith("rect") or module_attribute not in element.attrib:
            continue
        row, col = (int(value) for value in element.attrib[module_attribute].split(":"))
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
        metadata = pdf.Root.get("/Metadata")
        assert metadata is not None
        assert str(metadata["/Type"]) == "/Metadata"
        assert str(metadata["/Subtype"]) == "/XML"
        xmp = metadata.read_bytes().decode("utf-8")
        xmp_root = ET.fromstring(xmp)
        xmp_contract = spec["output_intent"]["xmp_pdfx"]
        assert xmp_contract["accepted_serializations"] == [
            "namespaced_attribute",
            "namespaced_element",
        ]
        assert xmp_contract["namespace_prefix_is_normative"] is False
        property_name = (
            f'{{{xmp_contract["namespace_uri"]}}}'
            f'{xmp_contract["version_property"]}'
        )
        declared_versions = []
        for element in xmp_root.iter():
            if property_name in element.attrib:
                declared_versions.append(element.attrib[property_name].strip())
            if element.tag == property_name and element.text:
                declared_versions.append(element.text.strip())
        assert spec["output_intent"]["pdfx_version"] in declared_versions
        intents = pdf.Root["/OutputIntents"]
        assert len(intents) == 1
        assert str(intents[0]["/S"]) == "/GTS_PDFX"
        assert str(intents[0]["/OutputConditionIdentifier"]) == spec["output_intent"]["identifier"]
        embedded_profile = intents[0]["/DestOutputProfile"]
        assert int(embedded_profile["/N"]) == 4
        assert embedded_profile.read_bytes() == Path(spec["output_intent"]["profile"]).read_bytes()

        page = pdf.pages[0]
        expected_media = [value * 72.0 / 25.4 for value in spec["canvas"]["media_box_mm"]]
        expected_trim = [value * 72.0 / 25.4 for value in spec["canvas"]["trim_box_mm"]]
        assert np.allclose([float(value) for value in page.MediaBox], expected_media, atol=0.01)
        assert np.allclose([float(value) for value in page.TrimBox], expected_trim, atol=0.01)
        assert np.allclose([float(value) for value in page.BleedBox], expected_media, atol=0.01)

        # Resource keys are arbitrary, but every required colorant must satisfy the
        # complete, agent-visible Separation and tint-transform contract.
        separation_contract = spec["output_intent"]["separation_color_space"]
        color_spaces = page.Resources["/ColorSpace"]
        separations = {}
        for resource_name, value in color_spaces.items():
            if not value or str(value[0]) != "/Separation":
                continue
            assert len(value) == separation_contract["array_length"]
            colorant_name = str(value[1])
            assert colorant_name not in separations
            alternate_name = str(value[2])
            alternate_components = {
                "/" + name: count
                for name, count in separation_contract[
                    "alternate_space_component_counts"
                ].items()
            }.get(alternate_name)
            assert alternate_components is not None, alternate_name
            tint_function = value[3]
            assert int(tint_function["/FunctionType"]) == separation_contract[
                "tint_transform_function_type"
            ]
            assert np.allclose(
                [float(item) for item in tint_function["/Domain"]],
                separation_contract["tint_domain"],
                atol=1e-12,
            )
            exponent = float(tint_function["/N"])
            assert math.isfinite(exponent) and exponent > separation_contract[
                "tint_exponent_min_exclusive"
            ]
            start = [float(item) for item in tint_function.get("/C0", [0.0])]
            end = [float(item) for item in tint_function.get("/C1", [1.0])]
            assert len(start) == len(end) == alternate_components
            component_min, component_max = separation_contract[
                "component_value_range"
            ]
            assert all(
                math.isfinite(item) and component_min <= item <= component_max
                for item in start + end
            )
            if separation_contract["require_nontrivial_endpoints"]:
                assert not np.allclose(start, end, atol=1e-12)
            separations[colorant_name] = str(resource_name)

        expected_separations = {
            "/" + spec["plate_registry"][plate_id]["canonical_name"]
            for plate_id in separation_contract["plate_ids"]
        }
        assert expected_separations.issubset(separations)

        # A resource dictionary entry alone is inert. Require the page program to
        # select, tint, and invoke a fill operation with every required spot space.
        graphics_stack = []
        fill_space = None
        fill_tint_set = False
        painted_spaces = set()
        fill_operators = {"f", "F", "f*", "B", "B*", "b", "b*"}
        for operands, operator in pikepdf.parse_content_stream(page):
            operation = str(operator)
            if operation == "q":
                graphics_stack.append((fill_space, fill_tint_set))
            elif operation == "Q":
                fill_space, fill_tint_set = graphics_stack.pop()
            elif operation == "cs" and len(operands) == 1:
                fill_space = str(operands[0])
                fill_tint_set = False
            elif operation in {"sc", "scn"}:
                fill_tint_set = True
            elif operation in fill_operators and fill_space and fill_tint_set:
                painted_spaces.add(fill_space)
        if separation_contract["require_painted_usage"]:
            for plate_id in separation_contract["plate_ids"]:
                colorant_name = "/" + spec["plate_registry"][plate_id]["canonical_name"]
                assert separations[colorant_name] in painted_spaces
        ocg_names = {str(item["/Name"]) for item in pdf.Root["/OCProperties"]["/OCGs"]}
        assert set(spec["svg_contract"]["required_data_roles"]).issubset(ocg_names)

        embedded_font_names = set()
        fonts = page.Resources.get("/Font")
        assert fonts is not None
        for _, font in fonts.items():
            resolved = font
            descendants = resolved.get("/DescendantFonts", [])
            candidates = list(descendants) if descendants else [resolved]
            for candidate in candidates:
                descriptor = candidate.get("/FontDescriptor")
                if descriptor and any(key in descriptor for key in ("/FontFile", "/FontFile2", "/FontFile3")):
                    base_font = str(candidate.get("/BaseFont", resolved.get("/BaseFont", ""))).lstrip("/")
                    embedded_font_names.add(base_font.split("+", 1)[-1])
        assert {Path(name).stem for name in spec["fonts"]["permitted_files"]}.issubset(
            embedded_font_names
        )

    document = fitz.open(pdf_path)
    page = document[0]
    spans = [
        span
        for block in page.get_text("dict")["blocks"]
        if block["type"] == 0
        for line in block["lines"]
        for span in line["spans"]
        if span["text"]
    ]
    assert len(spans) == len(EXPECTED_TEXT)
    spans_by_text = {span["text"]: span for span in spans}
    assert set(spans_by_text) == set(EXPECTED_TEXT.values())
    text_specs = {row["id"]: row for row in spec["fonts"]["text_objects"]}
    position_tolerance_pt = spec["svg_contract"]["text_position_tolerance_mm"] * 72.0 / 25.4
    advance_tolerance_pt = spec["fonts"]["equivalence"]["advance_width_mm_max"] * 72.0 / 25.4
    for text_id, value in EXPECTED_TEXT.items():
        text_spec = text_specs[text_id]
        span = spans_by_text[value]
        assert span["font"] == Path(text_spec["font_file"]).stem
        assert abs(float(span["size"]) - text_spec["font_size_pt"]) <= 0.01
        expected_origin = np.asarray(
            [
                text_spec["x_mm"] * 72.0 / 25.4,
                text_spec["y_mm"] * 72.0 / 25.4 + text_spec["font_size_pt"],
            ]
        )
        assert float(np.linalg.norm(np.asarray(span["origin"]) - expected_origin)) <= position_tolerance_pt
        authoritative_font = fitz.Font(fontfile=str(INPUT / "fonts" / text_spec["font_file"]))
        expected_advance = authoritative_font.text_length(value, fontsize=text_spec["font_size_pt"])
        actual_advance = float(span["bbox"][2] - span["bbox"][0])
        assert abs(actual_advance - expected_advance) <= advance_tolerance_pt
    scale = spec["canvas"]["dpi"] / 72.0
    pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False, colorspace=fitz.csRGB)
    rendered = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(pixmap.height, pixmap.width, 3)
    document.close()
    proof = np.asarray(Image.open(OUTPUT / "proof.png").convert("RGB"))
    assert rendered.shape == proof.shape
    limits = spec["acceptance_tolerances"]
    absolute_difference = np.abs(
        rendered.astype(np.int16) - proof.astype(np.int16)
    ).astype(np.float32)
    mean_absolute = float(np.mean(absolute_difference))
    assert mean_absolute <= limits["pdf_render_mean_absolute_rgb_max"], mean_absolute
    local_window_px = max(
        1,
        round(
            limits["pdf_render_local_window_mm"]
            * float(spec["canvas"]["dpi"])
            / 25.4
        ),
    )
    pixel_mean_absolute = np.mean(absolute_difference, axis=2)
    local_mean_absolute = cv2.boxFilter(
        pixel_mean_absolute,
        cv2.CV_32F,
        (local_window_px, local_window_px),
        normalize=True,
    )
    maximum_local_mean = float(np.max(local_mean_absolute))
    assert maximum_local_mean <= limits[
        "pdf_render_local_mean_absolute_rgb_max"
    ], maximum_local_mean
