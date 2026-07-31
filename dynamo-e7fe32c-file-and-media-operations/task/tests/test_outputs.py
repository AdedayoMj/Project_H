#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import io
import json
import math
import os
import stat
import tempfile
import zipfile
from pathlib import Path

import fitz
import freetype
import numpy as np
import pikepdf
import uharfbuzz as hb
from PIL import Image
from fontTools.designspaceLib import DesignSpaceDocument
from fontTools.feaLib.ast import FeatureBlock
from fontTools.feaLib.parser import Parser
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont
from scipy.ndimage import binary_erosion, distance_transform_edt
from ufoLib2 import Font as UFOFont

from font_model import geometry_for, render_proof


APP = Path(os.environ.get("FONT_REVIVAL_APP_ROOT", "/app"))
INPUT = APP / "input"
EVIDENCE = APP / "evidence"
OUTPUT = APP / "output"
TESTS = Path(__file__).parent
EXPECTED = json.loads((TESTS / "expected.json").read_text())
REFERENCE_FONT = TESTS / "reference.ttf"
EXPECTED_FILES = {"recovered.ttf", "sources.zip", "manifest.json", "proof.png", "specimen.pdf"}


def ticket() -> dict:
    return json.loads((INPUT / "job_ticket.json").read_text())


def evidence_index() -> dict:
    return json.loads((EVIDENCE / "index.json").read_text())


def fingerprint_tree(paths: list[Path], base: Path) -> str:
    digest = hashlib.sha256()
    entries = []
    for parent in paths:
        entries.extend(item for item in parent.rglob("*") if item.is_file())
    for path in sorted(entries, key=lambda item: item.relative_to(base).as_posix().encode()):
        digest.update(path.relative_to(base).as_posix().encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def pattern_digest(rows: list[str]) -> str:
    return hashlib.sha256("\n".join(rows).encode()).hexdigest()


def project(matrix: np.ndarray, points: np.ndarray) -> np.ndarray:
    homogeneous = np.column_stack([points, np.ones(len(points))])
    projected = homogeneous @ matrix.T
    return projected[:, :2] / projected[:, 2:3]


def shape(font_path: Path, text: str, location: dict[str, float]) -> list[tuple]:
    data = font_path.read_bytes()
    face = hb.Face(data)
    font = hb.Font(face)
    upem = face.upem
    font.scale = (upem, upem)
    font.set_variations({key: float(value) for key, value in location.items()})
    buffer = hb.Buffer()
    buffer.add_str(text)
    buffer.guess_segment_properties()
    hb.shape(font, buffer, {"kern": True, "liga": True, "mark": True})
    glyph_order = TTFont(font_path).getGlyphOrder()
    return [
        (
            glyph_order[info.codepoint],
            position.x_advance,
            position.y_advance,
            position.x_offset,
            position.y_offset,
        )
        for info, position in zip(buffer.glyph_infos, buffer.glyph_positions)
    ]


def make_instance(font_path: Path, location: dict[str, float], output_path: Path) -> None:
    font = TTFont(font_path)
    instance = instantiateVariableFont(font, location, inplace=False)
    instance.save(output_path)


def render_glyph(instance_path: Path, glyph_name: str, pixel_size: int = 512) -> np.ndarray:
    face = freetype.Face(str(instance_path))
    face.set_pixel_sizes(0, pixel_size)
    glyph_index = face.get_name_index(glyph_name.encode())
    assert glyph_index != 0 or glyph_name == ".notdef"
    face.load_glyph(
        glyph_index,
        freetype.FT_LOAD_RENDER | freetype.FT_LOAD_NO_HINTING | freetype.FT_LOAD_NO_AUTOHINT,
    )
    bitmap = face.glyph.bitmap
    canvas = np.zeros((800, 900), dtype=np.uint8)
    if bitmap.width and bitmap.rows:
        data = np.asarray(bitmap.buffer, dtype=np.uint8).reshape(bitmap.rows, bitmap.pitch)
        data = data[:, : bitmap.width]
        x = 120 + face.glyph.bitmap_left
        y = 650 - face.glyph.bitmap_top
        canvas[y : y + bitmap.rows, x : x + bitmap.width] = data
    return canvas


def contour_coordinate_signature(contour) -> tuple[float, float, list[tuple[float, float]], int]:
    coordinates = [(float(point.x), float(point.y)) for point in contour.points]
    minimum_x = min(value[0] for value in coordinates)
    minimum_y = min(value[1] for value in coordinates)
    offcurves = sum(point.type is None for point in contour.points)
    return minimum_x, minimum_y, sorted(coordinates), offcurves


def expected_cell_signatures(rows: list[str], geom: dict[str, int]) -> list[tuple]:
    signatures = []
    for row_index, row in enumerate(rows):
        for column_index, bit in enumerate(row):
            if bit != "1":
                continue
            cx = geom["first_center_x"] + column_index * geom["pitch_x"]
            cy = geom["top_center_y"] - row_index * geom["pitch_y"]
            x0 = cx - geom["cell_width"] // 2
            x1 = cx + geom["cell_width"] // 2
            y0 = cy - geom["cell_height"] // 2
            y1 = cy + geom["cell_height"] // 2
            radius = max(
                1,
                min(geom["radius"], (x1 - x0) // 2, (y1 - y0) // 2),
            )
            coordinates = sorted(
                [
                    (x0 + radius, y0),
                    (x1 - radius, y0),
                    (x1, y0),
                    (x1, y0 + radius),
                    (x1, y1 - radius),
                    (x1, y1),
                    (x1 - radius, y1),
                    (x0 + radius, y1),
                    (x0, y1),
                    (x0, y1 - radius),
                    (x0, y0 + radius),
                    (x0, y0),
                ]
            )
            signatures.append((float(x0), float(y0), coordinates, 4))
    return sorted(signatures, key=lambda item: (item[0], item[1]))


def test_required_artifacts_are_exact_regular_files_and_inputs_are_untouched():
    """The output set is exact, every artifact is a regular file, and all generated evidence remains byte-identical."""
    assert OUTPUT.is_dir() and not OUTPUT.is_symlink()
    assert {item.name for item in OUTPUT.iterdir()} == EXPECTED_FILES
    for item in OUTPUT.iterdir():
        assert item.is_file() and not item.is_symlink()
    assert fingerprint_tree([INPUT, EVIDENCE], APP) == EXPECTED["input_fingerprint_sha256"]


def test_manifest_is_complete_and_recovers_hidden_design_and_capture_state():
    """The manifest follows the normative schema, recovers glyph programs and OpenType values, and audits every scan geometrically."""
    spec = ticket()
    schema = spec["manifest_schema"]
    manifest = json.loads((OUTPUT / "manifest.json").read_text())
    assert set(manifest) == set(schema["top_level_exact_keys"])
    assert manifest["schema_version"] == spec["schema_version"]
    assert set(manifest["family"]) == set(schema["family_exact_keys"])
    assert manifest["family"] == {
        "name": spec["font_contract"]["family_name"],
        "units_per_em": spec["font_contract"]["units_per_em"],
    }
    assert [item["tag"] for item in manifest["axes"]] == [
        item["tag"] for item in spec["font_contract"]["axes"]
    ]
    for actual, axis in zip(manifest["axes"], spec["font_contract"]["axes"]):
        assert set(actual) == set(schema["axis_exact_keys"])
        assert actual == {
            "tag": axis["tag"],
            "minimum": axis["minimum"],
            "default": axis["default"],
            "maximum": axis["maximum"],
        }

    assert [item["name"] for item in manifest["glyphs"]] == spec["font_contract"]["glyph_order"]
    for row in manifest["glyphs"]:
        assert set(row) == set(schema["glyph_exact_keys"])
        assert row["pattern_sha256"] == pattern_digest(EXPECTED["patterns"][row["name"]])

    open_type = manifest["open_type"]
    assert set(open_type) == set(schema["open_type_exact_keys"])
    assert len(open_type["kerning"]) == len(EXPECTED["kerning"])
    metric_tolerance = spec["acceptance_tolerances"]["metric_error_units_max"]
    for actual, expected in zip(open_type["kerning"], EXPECTED["kerning"]):
        assert set(actual) == set(schema["kerning_exact_keys"])
        assert (actual["left"], actual["right"]) == (expected["left"], expected["right"])
        assert abs(actual["value"] - expected["value"]) <= metric_tolerance

    expected_anchor_rows = [
        {"glyph": glyph, "kind": "base", **values}
        for glyph, values in EXPECTED["anchors"]["bases"].items()
    ] + [{"kind": "mark", **EXPECTED["anchors"]["mark"]}]
    assert len(open_type["anchors"]) == len(expected_anchor_rows)
    anchor_tolerance = spec["acceptance_tolerances"]["anchor_error_units_max"]
    for actual, expected in zip(open_type["anchors"], expected_anchor_rows):
        expected_keys = (
            schema["base_anchor_exact_keys"]
            if expected["kind"] == "base"
            else schema["mark_anchor_exact_keys"]
        )
        assert set(actual) == set(expected_keys)
        assert (actual["glyph"], actual["kind"]) == (expected["glyph"], expected["kind"])
        for key in set(expected_keys) - {"glyph", "kind"}:
            assert abs(actual[key] - expected[key]) <= anchor_tolerance
    assert open_type["ligatures"] == EXPECTED["ligatures"]
    for item in open_type["ligatures"]:
        assert set(item) == set(schema["ligature_exact_keys"])

    records = evidence_index()["observations"]
    expected_captures = {item["id"]: item for item in EXPECTED["captures"]}
    assert [item["id"] for item in manifest["captures"]] == sorted(expected_captures)
    assert len(manifest["captures"]) == len(records)
    records_by_id = {item["id"]: item for item in records}
    required_inliers = spec["acceptance_tolerances"]["registration_required_inliers"]
    residual_limit = spec["acceptance_tolerances"]["registration_inlier_residual_px_max"]
    axis_limits = spec["acceptance_tolerances"]["axis_coordinate_max"]
    for capture in manifest["captures"]:
        assert set(capture) == set(schema["capture_exact_keys"])
        record = records_by_id[capture["id"]]
        expected = expected_captures[capture["id"]]
        assert capture["file"] == record["file"]
        assert capture["layout_id"] == record["layout_id"]
        assert capture["reported_axis_status"] == expected["reported_axis_status"]
        assert set(capture["axis_location"]) == {"wght", "wdth", "opsz"}
        # The status must also follow from the submitted location, not just match truth.
        reported = record["reported_axis_untrusted"]
        agrees = all(
            abs(float(reported[tag]) - float(capture["axis_location"][tag])) <= axis_limits[tag]
            for tag in ("wght", "wdth", "opsz")
        )
        assert capture["reported_axis_status"] == ("correct" if agrees else "mislabelled")
        for tag, value in expected["axis_location"].items():
            assert abs(float(capture["axis_location"][tag]) - value) <= axis_limits[tag]
        matrix = np.asarray(capture["image_to_page_homography"], dtype=np.float64)
        assert matrix.shape == (3, 3) and np.isfinite(matrix).all()
        image_points = np.asarray([item["image_px"] for item in record["fiducials"]])
        page_points = np.asarray([item["page_px"] for item in record["fiducials"]])
        residuals = np.linalg.norm(project(matrix, image_points) - page_points, axis=1)
        assert int(np.count_nonzero(residuals <= residual_limit)) >= required_inliers
    assert manifest["pdf"] == {
        "title": spec["pdf_contract"]["title"],
        "subject": spec["pdf_contract"]["subject"],
    }


def test_variable_font_has_real_outline_variation_and_required_open_type_structure():
    """The TTF is a genuine three-axis outline variable font with the exact repertoire and no bitmap or SVG fallback."""
    spec = ticket()["font_contract"]
    font = TTFont(OUTPUT / "recovered.ttf")
    assert set(spec["required_tables"]).issubset(font.keys())
    assert set(spec["forbidden_tables"]).isdisjoint(font.keys())
    assert font.getGlyphOrder() == spec["glyph_order"]
    cmap = font.getBestCmap()
    expected_cmap = {}
    for glyph in spec["glyphs"]:
        for codepoint in glyph["codepoints"]:
            expected_cmap[codepoint] = glyph["name"]
    assert cmap == expected_cmap
    assert font["head"].unitsPerEm == spec["units_per_em"]
    # The contract fixes every vertical metric, so grade them rather than trusting them.
    assert font["hhea"].ascent == spec["ascender"]
    assert font["hhea"].descent == spec["descender"]
    assert font["hhea"].lineGap == spec["line_gap"]
    os2 = font["OS/2"]
    assert os2.sTypoAscender == spec["ascender"]
    assert os2.sTypoDescender == spec["descender"]
    assert os2.sTypoLineGap == spec["line_gap"]
    assert os2.sxHeight == spec["x_height"]
    assert os2.sCapHeight == spec["cap_height"]
    assert os2.usWinAscent == max(0, spec["ascender"])
    assert os2.usWinDescent == max(0, -spec["descender"])
    assert font["name"].getDebugName(1) == spec["family_name"]
    assert font["name"].getDebugName(6) == f"{spec['postscript_prefix']}-Regular"
    axes = font["fvar"].axes
    assert [axis.axisTag for axis in axes] == [axis["tag"] for axis in spec["axes"]]
    for actual, expected in zip(axes, spec["axes"]):
        assert actual.minValue == expected["minimum"]
        assert actual.defaultValue == expected["default"]
        assert actual.maxValue == expected["maximum"]
    varying = sum(bool(variations) for variations in font["gvar"].variations.values())
    assert varying >= len(spec["glyph_order"]) - 2


def test_hidden_instances_match_reference_outlines_and_metrics():
    """Withheld intermediate coordinates distinguish continuous reconstruction from copied static masters or scan traces."""
    spec = ticket()
    limits = spec["acceptance_tolerances"]
    glyph_specs = {item["name"]: item for item in spec["font_contract"]["glyphs"]}
    with tempfile.TemporaryDirectory(prefix="font-instance-test-") as tmp:
        root = Path(tmp)
        for case_index, case in enumerate(EXPECTED["hidden_render_cases"]):
            actual_path = root / f"actual-{case_index}.ttf"
            expected_path = root / f"expected-{case_index}.ttf"
            make_instance(OUTPUT / "recovered.ttf", case["location"], actual_path)
            make_instance(REFERENCE_FONT, case["location"], expected_path)
            actual_font = TTFont(actual_path)
            expected_font = TTFont(expected_path)
            for glyph_name in case["glyphs"]:
                actual = render_glyph(actual_path, glyph_name)
                expected = render_glyph(expected_path, glyph_name)
                actual_mask = actual >= 128
                expected_mask = expected >= 128
                intersection = np.count_nonzero(actual_mask & expected_mask)
                union = np.count_nonzero(actual_mask | expected_mask)
                assert union > 0
                assert intersection / union >= limits["glyph_mask_iou_min"], glyph_name
                actual_boundary = actual_mask ^ binary_erosion(actual_mask)
                expected_boundary = expected_mask ^ binary_erosion(expected_mask)
                directed_a = distance_transform_edt(~expected_boundary)[actual_boundary].max()
                directed_b = distance_transform_edt(~actual_boundary)[expected_boundary].max()
                maximum = limits["glyph_boundary_hausdorff_em_max"] * 512
                assert max(float(directed_a), float(directed_b)) <= maximum, glyph_name
                actual_advance = actual_font["hmtx"][glyph_name][0]
                expected_advance = expected_font["hmtx"][glyph_name][0]
                assert abs(actual_advance - expected_advance) <= limits["metric_error_units_max"]
                assert glyph_name in glyph_specs


def test_harfbuzz_shaping_recovers_ligatures_kerning_and_mark_attachment():
    """HarfBuzz shaping at hidden coordinates matches glyph substitutions, pair positioning, and combining-mark attachment."""
    limits = ticket()["acceptance_tolerances"]
    for case in EXPECTED["shaping_cases"]:
        actual = shape(OUTPUT / "recovered.ttf", case["text"], case["location"])
        expected = shape(REFERENCE_FONT, case["text"], case["location"])
        assert [item[0] for item in actual] == [item[0] for item in expected]
        assert len(actual) == len(expected)
        for actual_row, expected_row in zip(actual, expected):
            assert abs(actual_row[1] - expected_row[1]) <= limits["metric_error_units_max"]
            assert abs(actual_row[2] - expected_row[2]) <= limits["metric_error_units_max"]
            assert abs(actual_row[3] - expected_row[3]) <= limits["anchor_error_units_max"]
            assert abs(actual_row[4] - expected_row[4]) <= limits["anchor_error_units_max"]


def test_editable_sources_are_safe_complete_and_geometry_compatible():
    """The source archive contains real Designspace/UFO masters with compatible recovered cells, metrics, anchors, and features."""
    spec = ticket()
    contract = spec["font_contract"]
    source_contract = spec["source_contract"]
    archive_path = OUTPUT / "sources.zip"
    assert archive_path.stat().st_size < 5_000_000
    with zipfile.ZipFile(archive_path) as archive:
        seen = set()
        total = 0
        for info in archive.infolist():
            assert info.filename not in seen
            seen.add(info.filename)
            path = Path(info.filename)
            assert not path.is_absolute() and ".." not in path.parts and "\\" not in info.filename
            mode = info.external_attr >> 16
            assert not stat.S_ISLNK(mode)
            total += info.file_size
        assert total < 20_000_000
        with tempfile.TemporaryDirectory(prefix="font-source-test-") as tmp:
            root = Path(tmp)
            archive.extractall(root)
            assert {item.name for item in root.iterdir()} == set(source_contract["exact_top_level_files"]) | {
                "masters"
            }
            document = DesignSpaceDocument.fromfile(root / "font.designspace")
            assert len(document.axes) == len(contract["axes"])
            for actual, expected in zip(document.axes, contract["axes"]):
                assert (actual.name, actual.tag) == (expected["name"], expected["tag"])
                assert (actual.minimum, actual.default, actual.maximum) == (
                    expected["minimum"],
                    expected["default"],
                    expected["maximum"],
                )
                assert np.allclose(actual.map, expected["map"])
            assert [source.filename for source in document.sources] == [
                f"masters/{master['name']}.ufo" for master in contract["masters"]
            ]
            expected_patterns = EXPECTED["patterns"]
            expected_kerning = {
                (item["left"], item["right"]): item["value"] for item in EXPECTED["kerning"]
            }
            for source, master in zip(document.sources, contract["masters"]):
                assert source.location == {
                    axis["name"]: master["location"][axis["tag"]]
                    for axis in contract["axes"]
                }
                ufo = UFOFont.open(root / source.filename)
                assert set(ufo.keys()) == set(contract["glyph_order"])
                assert set(ufo.kerning) == set(expected_kerning)
                for pair, value in expected_kerning.items():
                    assert (
                        abs(ufo.kerning[pair] - value)
                        <= spec["acceptance_tolerances"]["metric_error_units_max"]
                    )
                parsed = Parser(io.StringIO(ufo.features.text), set(ufo.keys())).parse()
                feature_tags = {
                    statement.name
                    for statement in parsed.statements
                    if isinstance(statement, FeatureBlock)
                }
                assert set(contract["open_type_recovery"]["required_features"]).issubset(feature_tags)
                glyph_specs = {item["name"]: item for item in contract["glyphs"]}
                for glyph_name in contract["glyph_order"]:
                    glyph = ufo[glyph_name]
                    assert not getattr(glyph.image, "fileName", None)
                    geom = geometry_for(glyph_specs[glyph_name], master["location"], spec)
                    expected_width = (
                        0
                        if glyph_name == EXPECTED["anchors"]["mark"]["glyph"]
                        else geom["advance"]
                    )
                    assert (
                        abs(glyph.width - expected_width)
                        <= spec["acceptance_tolerances"]["source_coordinate_error_units_max"]
                    )
                    actual_signatures = sorted(
                        [contour_coordinate_signature(contour) for contour in glyph],
                        key=lambda item: (item[0], item[1]),
                    )
                    expected_signatures = expected_cell_signatures(
                        expected_patterns[glyph_name], geom
                    )
                    assert len(actual_signatures) == len(expected_signatures)
                    for actual_signature, expected_signature in zip(
                        actual_signatures, expected_signatures
                    ):
                        assert actual_signature[3] == expected_signature[3] == 4
                        assert len(actual_signature[2]) == len(expected_signature[2]) == 12
                        assert np.max(
                            np.abs(
                                np.asarray(actual_signature[2])
                                - np.asarray(expected_signature[2])
                            )
                        ) <= spec["acceptance_tolerances"]["source_coordinate_error_units_max"]
                    anchors = {anchor.name: anchor for anchor in glyph.anchors}
                    if glyph_name in EXPECTED["anchors"]["bases"]:
                        expected = EXPECTED["anchors"]["bases"][glyph_name]
                        assert set(anchors) == {"top"}
                        assert abs(
                            anchors["top"].x
                            - (geom["advance"] / 2 + expected["x_offset"])
                        ) <= spec["acceptance_tolerances"]["anchor_error_units_max"]
                        assert (
                            abs(anchors["top"].y - expected["y"])
                            <= spec["acceptance_tolerances"]["anchor_error_units_max"]
                        )
                    elif glyph_name == EXPECTED["anchors"]["mark"]["glyph"]:
                        expected = EXPECTED["anchors"]["mark"]
                        assert set(anchors) == {"_top"}
                        assert (
                            abs(anchors["_top"].x - expected["x"])
                            <= spec["acceptance_tolerances"]["anchor_error_units_max"]
                        )
                        assert (
                            abs(anchors["_top"].y - expected["y"])
                            <= spec["acceptance_tolerances"]["anchor_error_units_max"]
                        )
                    else:
                        assert not anchors


def test_proof_is_derived_from_submitted_font_and_matches_hidden_design():
    """The PNG is the normative render of the submitted font and remains colorimetrically aligned with the hidden revival."""
    spec = ticket()
    proof = np.asarray(Image.open(OUTPUT / "proof.png").convert("RGB"))
    derived = np.asarray(render_proof(OUTPUT / "recovered.ttf", spec))
    assert proof.shape == (
        spec["proof_contract"]["height_px"],
        spec["proof_contract"]["width_px"],
        3,
    )
    assert np.array_equal(proof, derived)
    expected = np.asarray(render_proof(REFERENCE_FONT, spec))
    difference = np.abs(proof.astype(np.int16) - expected.astype(np.int16))
    limits = spec["acceptance_tolerances"]
    assert float(np.median(difference)) <= limits["proof_median_absolute_rgb_max"]
    assert float(np.percentile(difference, 99)) <= limits["proof_p99_absolute_rgb_max"]


def test_pdf_is_one_page_live_embedded_and_visually_consistent():
    """The specimen PDF is unencrypted, embeds the recovered family, retains live proof strings, and rasterizes to the PNG."""
    spec = ticket()
    pdf_path = OUTPUT / "specimen.pdf"
    with pikepdf.open(pdf_path) as pdf:
        assert not pdf.is_encrypted
        assert len(pdf.pages) == 1
        assert str(pdf.docinfo["/Title"]) == spec["pdf_contract"]["title"]
        assert str(pdf.docinfo["/Subject"]) == spec["pdf_contract"]["subject"]
        page = pdf.pages[0]
        expected_box = [
            0,
            0,
            spec["proof_contract"]["width_px"] * 72 / spec["pdf_contract"]["render_dpi"],
            spec["proof_contract"]["height_px"] * 72 / spec["pdf_contract"]["render_dpi"],
        ]
        assert np.allclose([float(value) for value in page.MediaBox], expected_box, atol=0.01)
        fonts = page.Resources.get("/Font")
        assert fonts is not None
        embedded = 0
        family_named = False
        for _, font in fonts.items():
            base_name = str(font.get("/BaseFont", ""))
            family_named |= "DynamoPalimpsest" in base_name
            descendants = list(font.get("/DescendantFonts", []))
            candidates = descendants if descendants else [font]
            for candidate in candidates:
                descriptor = candidate.get("/FontDescriptor")
                if descriptor and any(
                    key in descriptor for key in ("/FontFile", "/FontFile2", "/FontFile3")
                ):
                    embedded += 1
        assert embedded >= 1 and family_named

    document = fitz.open(pdf_path)
    page = document[0]
    extracted = page.get_text()
    for line in spec["proof_contract"]["lines"]:
        assert line["text"] in extracted
    scale = spec["pdf_contract"]["render_dpi"] / 72.0
    pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False, colorspace=fitz.csRGB)
    rendered = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(
        pixmap.height, pixmap.width, 3
    )
    document.close()
    proof = np.asarray(Image.open(OUTPUT / "proof.png").convert("RGB"))
    assert rendered.shape == proof.shape
    mean_absolute = float(
        np.mean(np.abs(rendered.astype(np.int16) - proof.astype(np.int16)))
    )
    assert (
        mean_absolute
        <= spec["acceptance_tolerances"]["pdf_render_mean_absolute_rgb_max"]
    ), mean_absolute
