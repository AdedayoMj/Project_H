#!/usr/bin/env python3
from __future__ import annotations

import copy
import tempfile
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from fontTools.designspaceLib import AxisDescriptor, DesignSpaceDocument, SourceDescriptor
from fontTools.feaLib.builder import addOpenTypeFeaturesFromString
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont
from fontTools.varLib import build as build_var
from fontTools.varLib.instancer import instantiateVariableFont
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont as ReportLabTTFont
from reportlab.pdfgen import canvas
from ufoLib2 import Font as UFOFont


def axis_normalized(value: float, axis: dict) -> float:
    default = float(axis["default"])
    if value == default:
        return 0.0
    if value < default:
        return (value - default) / (default - float(axis["minimum"]))
    return (value - default) / (float(axis["maximum"]) - default)


def geometry_for(glyph_spec: dict, location: dict[str, float], ticket: dict) -> dict[str, int]:
    axes = {axis["tag"]: axis for axis in ticket["font_contract"]["axes"]}
    wn = axis_normalized(float(location["wght"]), axes["wght"])
    dn = axis_normalized(float(location["wdth"]), axes["wdth"])
    on = axis_normalized(float(location["opsz"]), axes["opsz"])
    model = ticket["font_contract"]["geometry"]
    pitch_x = round(model["pitch_x_base"] + model["pitch_x_wdth_delta"] * dn)
    cell_width = round(
        model["cell_width_base"]
        + model["cell_width_wght_delta"] * wn
        + model["cell_width_opsz_delta"] * on
    )
    cell_height = round(
        model["cell_height_base"]
        + model["cell_height_wght_delta"] * wn
        + model["cell_height_opsz_delta"] * on
    )
    radius = round(
        model["corner_radius_base"]
        + model["corner_radius_wght_delta"] * wn
        + model["corner_radius_opsz_delta"] * on
    )
    columns = int(glyph_spec["columns"])
    advance = round(
        model["advance_side_space"] * 2
        + max(0, columns - 1) * pitch_x
        + glyph_spec.get("advance_adjust_units", 0)
    )
    return {
        "pitch_x": pitch_x,
        "pitch_y": int(model["pitch_y"]),
        "cell_width": cell_width,
        "cell_height": cell_height,
        "radius": radius,
        "first_center_x": int(model["first_center_x"]),
        "top_center_y": int(model["top_center_y"]),
        "advance": advance,
    }


def _rounded_cell(pen, x0: int, y0: int, x1: int, y1: int, radius: int) -> None:
    radius = max(1, min(radius, (x1 - x0) // 2, (y1 - y0) // 2))
    pen.moveTo((x0 + radius, y0))
    pen.lineTo((x1 - radius, y0))
    pen.qCurveTo((x1, y0), (x1, y0 + radius))
    pen.lineTo((x1, y1 - radius))
    pen.qCurveTo((x1, y1), (x1 - radius, y1))
    pen.lineTo((x0 + radius, y1))
    pen.qCurveTo((x0, y1), (x0, y1 - radius))
    pen.lineTo((x0, y0 + radius))
    pen.qCurveTo((x0, y0), (x0 + radius, y0))
    pen.closePath()


def draw_pattern(pen, rows: list[str], geom: dict[str, int]) -> None:
    for row_index, row in enumerate(rows):
        for column_index, bit in enumerate(row):
            if bit != "1":
                continue
            center_x = geom["first_center_x"] + column_index * geom["pitch_x"]
            center_y = geom["top_center_y"] - row_index * geom["pitch_y"]
            x0 = center_x - geom["cell_width"] // 2
            x1 = center_x + geom["cell_width"] // 2
            y0 = center_y - geom["cell_height"] // 2
            y1 = center_y + geom["cell_height"] // 2
            _rounded_cell(pen, x0, y0, x1, y1, geom["radius"])


def _anchor_values(
    glyph_name: str,
    geom: dict[str, int],
    anchor_parameters: dict,
) -> list[dict]:
    values = []
    if glyph_name in anchor_parameters.get("bases", {}):
        item = anchor_parameters["bases"][glyph_name]
        values.append(
            {
                "name": "top",
                "x": round(geom["advance"] / 2 + item["x_offset"]),
                "y": int(item["y"]),
            }
        )
    if glyph_name == anchor_parameters["mark"]["glyph"]:
        values.append(
            {
                "name": "_top",
                "x": int(anchor_parameters["mark"]["x"]),
                "y": int(anchor_parameters["mark"]["y"]),
            }
        )
    return values


def feature_text(
    metrics: dict[str, tuple[int, int]],
    kerning: list[dict],
    anchors: dict,
    ligatures: list[dict],
) -> str:
    lines = ["languagesystem DFLT dflt;", "languagesystem latn dflt;"]
    mark = anchors["mark"]
    lines.append(
        f"markClass {mark['glyph']} <anchor {int(mark['x'])} {int(mark['y'])}> @MC_top;"
    )
    lines.append("feature liga {")
    for item in ligatures:
        lines.append(f"  sub {' '.join(item['input'])} by {item['output']};")
    lines.append("} liga;")
    lines.append("feature kern {")
    for item in kerning:
        lines.append(f"  pos {item['left']} {item['right']} {int(item['value'])};")
    lines.append("} kern;")
    lines.append("feature mark {")
    for glyph_name, item in anchors["bases"].items():
        x = round(metrics[glyph_name][0] / 2 + item["x_offset"])
        lines.append(f"  pos base {glyph_name} <anchor {x} {int(item['y'])}> mark @MC_top;")
    lines.append("} mark;")
    return "\n".join(lines) + "\n"


def build_static_font(
    output_path: Path,
    patterns: dict[str, list[str]],
    location: dict[str, float],
    ticket: dict,
    kerning: list[dict],
    anchors: dict,
    ligatures: list[dict],
    style_name: str,
) -> None:
    contract = ticket["font_contract"]
    glyph_specs = {item["name"]: item for item in contract["glyphs"]}
    glyph_order = contract["glyph_order"]
    glyphs = {}
    metrics: dict[str, tuple[int, int]] = {}
    for glyph_name in glyph_order:
        spec = glyph_specs[glyph_name]
        geom = geometry_for(spec, location, ticket)
        pen = TTGlyphPen(None)
        rows = patterns.get(glyph_name, [])
        draw_pattern(pen, rows, geom)
        glyphs[glyph_name] = pen.glyph()
        width = 0 if glyph_name == anchors["mark"]["glyph"] else geom["advance"]
        occupied_columns = [
            column
            for row in rows
            for column, bit in enumerate(row)
            if bit == "1"
        ]
        lsb = (
            geom["first_center_x"]
            + min(occupied_columns) * geom["pitch_x"]
            - geom["cell_width"] // 2
            if occupied_columns
            else 0
        )
        metrics[glyph_name] = (width, lsb)

    builder = FontBuilder(int(contract["units_per_em"]), isTTF=True)
    builder.setupGlyphOrder(glyph_order)
    cmap = {}
    for item in contract["glyphs"]:
        for codepoint in item.get("codepoints", []):
            cmap[int(codepoint)] = item["name"]
    builder.setupCharacterMap(cmap)
    builder.setupGlyf(glyphs)
    builder.setupHorizontalMetrics(metrics)
    builder.setupHorizontalHeader(
        ascent=int(contract["ascender"]),
        descent=int(contract["descender"]),
        lineGap=int(contract["line_gap"]),
    )
    builder.setupNameTable(
        {
            "familyName": contract["family_name"],
            "styleName": style_name,
            "uniqueFontIdentifier": f"{contract['family_name']} {style_name} 1.000",
            "fullName": f"{contract['family_name']} {style_name}",
            "psName": f"{contract['postscript_prefix']}-{style_name.replace(' ', '')}",
            "version": "Version 1.000",
        }
    )
    builder.setupOS2(
        sTypoAscender=int(contract["ascender"]),
        sTypoDescender=int(contract["descender"]),
        sTypoLineGap=int(contract["line_gap"]),
        usWinAscent=max(0, int(contract["ascender"])),
        usWinDescent=max(0, -int(contract["descender"])),
        sxHeight=int(contract["x_height"]),
        sCapHeight=int(contract["cap_height"]),
        usWeightClass=max(1, min(1000, round(location["wght"]))),
        usWidthClass=max(1, min(9, round((location["wdth"] - 50) / 12.5))),
        fsType=0,
    )
    builder.setupPost()
    builder.setupMaxp()
    font = builder.font
    addOpenTypeFeaturesFromString(font, feature_text(metrics, kerning, anchors, ligatures))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    font.save(output_path)


def _make_designspace(
    path: Path,
    master_paths: dict[str, Path],
    ticket: dict,
) -> DesignSpaceDocument:
    contract = ticket["font_contract"]
    document = DesignSpaceDocument()
    for axis_spec in contract["axes"]:
        axis = AxisDescriptor()
        axis.name = axis_spec["name"]
        axis.tag = axis_spec["tag"]
        axis.minimum = float(axis_spec["minimum"])
        axis.default = float(axis_spec["default"])
        axis.maximum = float(axis_spec["maximum"])
        axis.map = [(float(a), float(b)) for a, b in axis_spec["map"]]
        document.addAxis(axis)
    for master in contract["masters"]:
        source = SourceDescriptor()
        source.name = master["name"]
        source.path = str(master_paths[master["name"]])
        source.familyName = contract["family_name"]
        source.styleName = master["style_name"]
        source.location = {
            axis["name"]: float(master["location"][axis["tag"]])
            for axis in contract["axes"]
        }
        if master["name"] == contract["default_master"]:
            source.copyInfo = True
            source.copyLib = True
            source.copyFeatures = True
            source.copyGroups = True
        document.addSource(source)
    document.write(path)
    return document


def build_variable_font(
    output_path: Path,
    patterns: dict[str, list[str]],
    ticket: dict,
    kerning: list[dict],
    anchors: dict,
    ligatures: list[dict],
) -> None:
    with tempfile.TemporaryDirectory(prefix="dynamo-font-build-") as tmp:
        root = Path(tmp)
        master_paths = {}
        for master in ticket["font_contract"]["masters"]:
            path = root / f"{master['name']}.ttf"
            build_static_font(
                path,
                patterns,
                master["location"],
                ticket,
                kerning,
                anchors,
                ligatures,
                master["style_name"],
            )
            master_paths[master["name"]] = path
        designspace_path = root / "font.designspace"
        _make_designspace(designspace_path, master_paths, ticket)
        variable_font, _, _ = build_var(str(designspace_path))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        variable_font.save(output_path)


def instantiate_font(variable_path: Path, location: dict[str, float], output_path: Path) -> None:
    font = TTFont(variable_path)
    instance = instantiateVariableFont(font, location, inplace=False)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    instance.save(output_path)


def render_proof(font_path: Path, ticket: dict) -> Image.Image:
    contract = ticket["proof_contract"]
    image = Image.new("RGB", (contract["width_px"], contract["height_px"]), tuple(contract["background_rgb"]))
    draw = ImageDraw.Draw(image)
    with tempfile.TemporaryDirectory(prefix="dynamo-proof-") as tmp:
        root = Path(tmp)
        for index, line in enumerate(contract["lines"]):
            instance_path = root / f"line-{index}.ttf"
            instantiate_font(font_path, line["location"], instance_path)
            font = ImageFont.truetype(
                str(instance_path),
                size=int(line["size_px"]),
                layout_engine=ImageFont.Layout.BASIC,
            )
            draw.text(
                (int(line["x_px"]), int(line["baseline_y_px"])),
                line["text"],
                font=font,
                fill=tuple(line["rgb"]),
                anchor="ls",
            )
    return image


def write_sources_zip(
    output_path: Path,
    patterns: dict[str, list[str]],
    ticket: dict,
    kerning: list[dict],
    anchors: dict,
    ligatures: list[dict],
) -> None:
    contract = ticket["font_contract"]
    glyph_specs = {item["name"]: item for item in contract["glyphs"]}
    with tempfile.TemporaryDirectory(prefix="dynamo-ufo-") as tmp:
        root = Path(tmp)
        source_paths: dict[str, Path] = {}
        for master in contract["masters"]:
            font = UFOFont()
            font.info.familyName = contract["family_name"]
            font.info.styleName = master["style_name"]
            font.info.unitsPerEm = int(contract["units_per_em"])
            font.info.ascender = int(contract["ascender"])
            font.info.descender = int(contract["descender"])
            metrics = {}
            for glyph_name in contract["glyph_order"]:
                spec = glyph_specs[glyph_name]
                geom = geometry_for(spec, master["location"], ticket)
                glyph = font.newGlyph(glyph_name)
                glyph.width = 0 if glyph_name == anchors["mark"]["glyph"] else geom["advance"]
                glyph.unicodes = list(spec.get("codepoints", []))
                draw_pattern(glyph.getPen(), patterns.get(glyph_name, []), geom)
                for anchor in _anchor_values(glyph_name, geom, anchors):
                    glyph.appendAnchor(anchor)
                metrics[glyph_name] = (int(glyph.width), 0)
            for item in kerning:
                font.kerning[(item["left"], item["right"])] = int(item["value"])
            font.features.text = feature_text(metrics, kerning, anchors, ligatures)
            ufo_path = root / "masters" / f"{master['name']}.ufo"
            ufo_path.parent.mkdir(parents=True, exist_ok=True)
            font.save(ufo_path, overwrite=True)
            source_paths[master["name"]] = ufo_path

        document = DesignSpaceDocument()
        for axis_spec in contract["axes"]:
            axis = AxisDescriptor()
            axis.name = axis_spec["name"]
            axis.tag = axis_spec["tag"]
            axis.minimum = float(axis_spec["minimum"])
            axis.default = float(axis_spec["default"])
            axis.maximum = float(axis_spec["maximum"])
            axis.map = [(float(a), float(b)) for a, b in axis_spec["map"]]
            document.addAxis(axis)
        for master in contract["masters"]:
            source = SourceDescriptor()
            source.name = master["name"]
            source.filename = f"masters/{master['name']}.ufo"
            source.familyName = contract["family_name"]
            source.styleName = master["style_name"]
            source.location = {
                axis["name"]: float(master["location"][axis["tag"]])
                for axis in contract["axes"]
            }
            document.addSource(source)
        document.write(root / "font.designspace")
        (root / "features.fea").write_text(
            feature_text(
                {
                    name: (
                        0
                        if name == anchors["mark"]["glyph"]
                        else geometry_for(
                            glyph_specs[name],
                            next(
                                item["location"]
                                for item in contract["masters"]
                                if item["name"] == contract["default_master"]
                            ),
                            ticket,
                        )["advance"],
                        0,
                    )
                    for name in contract["glyph_order"]
                },
                kerning,
                anchors,
                ligatures,
            )
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for path in sorted(item for item in root.rglob("*") if item.is_file()):
                relative = path.relative_to(root).as_posix()
                info = zipfile.ZipInfo(relative, date_time=(2024, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def write_specimen_pdf(
    output_path: Path,
    proof_path: Path,
    font_path: Path,
    ticket: dict,
) -> None:
    proof = ticket["proof_contract"]
    pdf = ticket["pdf_contract"]
    page_width = proof["width_px"] * 72.0 / pdf["render_dpi"]
    page_height = proof["height_px"] * 72.0 / pdf["render_dpi"]
    font_name = "DynamoRecovered"
    pdfmetrics.registerFont(ReportLabTTFont(font_name, str(font_path)))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    document = canvas.Canvas(
        str(output_path),
        pagesize=(page_width, page_height),
        pageCompression=1,
        invariant=1,
    )
    document.setTitle(pdf["title"])
    document.setSubject(pdf["subject"])
    document.drawImage(ImageReader(str(proof_path)), 0, 0, width=page_width, height=page_height, mask=None)
    text = document.beginText()
    text.setTextRenderMode(3)
    text.setFont(font_name, 10)
    text.setTextOrigin(12, page_height - 12)
    for line in proof["lines"]:
        text.textLine(line["text"])
    document.drawText(text)
    document.showPage()
    document.save()
