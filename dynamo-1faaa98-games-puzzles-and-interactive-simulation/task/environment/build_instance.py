#!/usr/bin/env python3
"""Build deterministic, realistic field-season evidence for the irrigation task."""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
from datetime import date, timedelta
from pathlib import Path

from shapely import affinity
from shapely.geometry import MultiPolygon, Polygon, box, mapping
from shapely.geometry.polygon import orient


CAMPAIGNS = (
    {
        "campaign_id": "cedar_2023",
        "seed": 104729,
        "base_x": 486_320.0,
        "base_y": 4_507_640.0,
        "width": 472.0,
        "height": 386.0,
        "start": date(2023, 4, 17),
        "days": 139,
        "cell_size_m": (7.5, 12.5),
        "root_depth_curve": ((0.00, 0.28), (0.18, 0.52), (0.42, 0.88), (0.68, 1.08)),
        "depletion_fraction": 0.48,
        "efficiency": 0.83,
        "max_application_mm": 34.0,
        "zone_edges_mm": (0.0, 8.0, 16.0, 24.0, 32.0),
        "soil_rotation_degrees": 8.0,
        "split_field": False,
        "drainage_storm": (2, 72.0),
        "kc": (1.31, -0.09, 0.18, 1.17),
    },
    {
        "campaign_id": "mesa_2024",
        "seed": 130363,
        "base_x": 612_150.0,
        "base_y": 3_908_210.0,
        "width": 438.0,
        "height": 421.0,
        "start": date(2024, 3, 29),
        "days": 147,
        "cell_size_m": (11.0, 9.0),
        "root_depth_curve": ((0.00, 0.24), (0.23, 0.44), (0.49, 0.71), (0.74, 0.94)),
        "depletion_fraction": 0.44,
        "efficiency": 0.79,
        "max_application_mm": 27.0,
        "zone_edges_mm": (0.0, 7.5, 15.0, 22.5),
        "soil_rotation_degrees": -13.0,
        "split_field": True,
        "drainage_storm": (0, 84.0),
        "kc": (1.24, -0.04, 0.16, 1.12),
    },
    {
        "campaign_id": "northfork_2022",
        "seed": 155921,
        "base_x": 356_780.0,
        "base_y": 4_318_560.0,
        "width": 506.0,
        "height": 352.0,
        "start": date(2022, 5, 3),
        "days": 132,
        "cell_size_m": (8.0, 14.0),
        "root_depth_curve": ((0.00, 0.33), (0.16, 0.62), (0.39, 0.98), (0.64, 1.16)),
        "depletion_fraction": 0.51,
        "efficiency": 0.86,
        "max_application_mm": 58.0,
        "zone_edges_mm": (0.0, 12.0, 24.0, 36.0, 48.0, 56.0),
        "soil_rotation_degrees": 21.0,
        "split_field": False,
        "drainage_storm": (5, 96.0),
        "kc": (1.37, -0.12, 0.20, 1.20),
    },
    {
        "campaign_id": "willow_2025",
        "seed": 196613,
        "base_x": 703_410.0,
        "base_y": 4_126_980.0,
        "width": 461.0,
        "height": 404.0,
        "start": date(2025, 4, 8),
        "days": 143,
        "cell_size_m": (12.5, 7.5),
        "root_depth_curve": ((0.00, 0.26), (0.20, 0.49), (0.46, 0.82), (0.72, 1.02)),
        "depletion_fraction": 0.46,
        "efficiency": 0.81,
        "max_application_mm": 43.0,
        "zone_edges_mm": (0.0, 10.0, 20.0, 30.0, 40.0),
        "soil_rotation_degrees": -27.0,
        "split_field": False,
        "drainage_storm": (1, 78.0),
        "kc": (1.28, -0.06, 0.17, 1.15),
    },
)


def dump_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def field_geometry(config: dict):
    """Return an irregular field, optionally split by a non-cropped service corridor."""
    x, y = config["base_x"], config["base_y"]
    w, h = config["width"], config["height"]
    jitter = (config["seed"] % 19) / 10.0
    shell = [
        (x + 8.3, y + 31.7),
        (x + 0.4, y + 0.25 * h),
        (x + 25.8, y + 0.43 * h),
        (x + 3.9, y + 0.61 * h),
        (x + 44.2, y + h - 18.6),
        (x + 0.24 * w, y + h - 2.9),
        (x + 0.39 * w, y + h - 25.1),
        (x + 0.57 * w, y + h - 3.2),
        (x + 0.77 * w, y + h - 15.4),
        (x + w - 11.6, y + 0.83 * h),
        (x + w - 0.8, y + 0.60 * h),
        (x + w - 22.1, y + 0.45 * h),
        (x + w - 2.7, y + 0.24 * h),
        (x + 0.84 * w, y + 6.1),
        (x + 0.66 * w, y + 20.6 + jitter),
        (x + 0.48 * w, y + 1.8),
        (x + 0.29 * w, y + 17.3),
        (x + 0.13 * w, y + 3.5),
    ]
    hole_one = [
        (x + 0.44 * w, y + 0.42 * h),
        (x + 0.47 * w, y + 0.37 * h),
        (x + 0.53 * w, y + 0.36 * h),
        (x + 0.57 * w, y + 0.42 * h),
        (x + 0.55 * w, y + 0.49 * h),
        (x + 0.49 * w, y + 0.51 * h),
        (x + 0.44 * w, y + 0.47 * h),
    ]
    hole_two = [
        (x + 0.72 * w, y + 0.66 * h),
        (x + 0.755 * w, y + 0.625 * h),
        (x + 0.80 * w, y + 0.66 * h),
        (x + 0.79 * w, y + 0.72 * h),
        (x + 0.74 * w, y + 0.735 * h),
    ]
    polygon = orient(Polygon(shell, [hole_one, hole_two]), sign=1.0)
    if not polygon.is_valid:
        raise RuntimeError(f"invalid generated boundary for {config['campaign_id']}")
    if config["split_field"]:
        corridor_x = x + 0.34 * w
        polygon = polygon.difference(box(corridor_x - 3.5, y - 25.0, corridor_x + 3.5, y + h + 25.0))
        if not isinstance(polygon, MultiPolygon) or len(polygon.geoms) < 2:
            raise RuntimeError(f"service corridor did not split {config['campaign_id']}")
    return polygon


def analysis_units(
    field, origin_x: float, origin_y: float, cell_width: float, cell_height: float
) -> list[dict]:
    min_x, min_y, max_x, max_y = field.bounds
    min_col = math.floor((min_x - origin_x) / cell_width)
    max_col = math.ceil((max_x - origin_x) / cell_width) - 1
    min_row = math.floor((origin_y - max_y) / cell_height)
    max_row = math.ceil((origin_y - min_y) / cell_height) - 1
    units: list[dict] = []
    for row in range(min_row, max_row + 1):
        top = origin_y - row * cell_height
        bottom = top - cell_height
        for column in range(min_col, max_col + 1):
            left = origin_x + column * cell_width
            clipped = field.intersection(box(left, bottom, left + cell_width, top))
            if clipped.area <= 0.0:
                continue
            units.append(
                {
                    "unit_id": f"r{row:04d}c{column:04d}",
                    "row": row,
                    "column": column,
                    "geometry": clipped,
                }
            )
    return units


def soil_partition(config: dict, field) -> tuple[list[dict], list[dict]]:
    """Create a rotated complete survey partition with depth-varying storage."""
    min_x, min_y, max_x, max_y = field.bounds
    width, height = max_x - min_x, max_y - min_y
    x_fracs = (0.0, 0.173, 0.392, 0.641, 0.827, 1.0)
    y_fracs = (0.0, 0.226, 0.518, 0.771, 1.0)
    padding = max(width, height)
    x_edges = [min_x - padding] + [min_x + width * f for f in x_fracs[1:-1]] + [max_x + padding]
    y_edges = [min_y - padding] + [min_y + height * f for f in y_fracs[1:-1]] + [max_y + padding]
    features: list[dict] = []
    horizons: list[dict] = []
    layer_edges = (0, 17, 39, 72, 108, 145)
    sequence = 0
    for band_y in range(len(y_edges) - 1):
        for band_x in range(len(x_edges) - 1):
            map_id = f"MU{sequence + 1:02d}"
            geometry = affinity.rotate(
                box(
                    x_edges[band_x],
                    y_edges[band_y],
                    x_edges[band_x + 1],
                    y_edges[band_y + 1],
                ),
                config["soil_rotation_degrees"],
                origin=((min_x + max_x) / 2.0, (min_y + max_y) / 2.0),
                use_radians=False,
            )
            features.append(
                {
                    "type": "Feature",
                    "properties": {"map_unit_id": map_id},
                    "geometry": mapping(geometry),
                }
            )
            for layer in range(len(layer_edges) - 1):
                texture = (sequence * 7 + layer * 11 + config["seed"]) % 23
                theta_wp = 0.072 + 0.0038 * texture + 0.004 * layer
                available = 0.074 + 0.0035 * ((sequence * 13 + layer * 5 + 3) % 24)
                theta_fc = min(0.46, theta_wp + available)
                horizons.append(
                    {
                        "map_unit_id": map_id,
                        "top_cm": layer_edges[layer],
                        "bottom_cm": layer_edges[layer + 1],
                        "theta_fc": f"{theta_fc:.6f}",
                        "theta_wp": f"{theta_wp:.6f}",
                    }
                )
            sequence += 1
    return features, horizons


def observation_offsets(days: int, seed: int, row: int, column: int) -> list[int]:
    rng = random.Random(seed ^ 0x5A17 ^ (row * 1_000_003) ^ (column * 97_409))
    offsets = [-rng.randint(0, 9)]
    while offsets[-1] < days - 7:
        offsets.append(offsets[-1] + rng.randint(9, 18))
    terminal = days + rng.randint(0, 8)
    if terminal > offsets[-1]:
        offsets.append(terminal)
    return sorted(set(offsets))


def vegetation_rows(config: dict, units: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for unit in units:
        offsets = observation_offsets(
            config["days"], config["seed"], unit["row"], unit["column"]
        )
        spatial = (
            0.044 * math.sin((unit["column"] + 1) * 0.419)
            + 0.031 * math.cos((unit["row"] + 2) * 0.347)
            + 0.018 * math.sin((unit["row"] - unit["column"]) * 0.173)
        )
        for offset in offsets:
            phase = offset / (config["days"] - 1)
            canopy = math.sin(math.pi * min(1.0, max(0.0, phase))) ** 0.78
            senescence = 0.055 * max(0.0, (phase - 0.73) / 0.27)
            ripple = 0.012 * math.sin(offset * 0.31 + unit["row"] * 0.07)
            vi = min(0.91, max(0.11, 0.18 + 0.65 * canopy - senescence + spatial + ripple))
            rows.append(
                {
                    "unit_id": unit["unit_id"],
                    "date": (config["start"] + timedelta(days=offset)).isoformat(),
                    "vi": f"{vi:.6f}",
                }
            )
    return rows


def weather_rows(config: dict) -> list[dict]:
    rng = random.Random(config["seed"] ^ 0xC0FFEE)
    storm_offset, storm_depth = config["drainage_storm"]
    rain_days = {
        storm_offset: storm_depth,
        8: 7.5,
        19: 3.2,
        37: 15.8,
        38: 4.6,
        61: 9.1,
        83: 2.9,
        101: 18.4,
        config["days"] - 23: 6.7,
        config["days"] - 11: 2.4,
    }
    rows: list[dict] = []
    for offset in range(config["days"]):
        phase = offset / (config["days"] - 1)
        eto = (
            2.65
            + 3.35 * math.sin(math.pi * phase) ** 0.88
            + 0.34 * math.sin(offset * 0.47)
            + rng.uniform(-0.18, 0.18)
        )
        precipitation = rain_days.get(offset, 0.0)
        if offset % 47 == (config["seed"] % 17):
            precipitation += 1.35
        rows.append(
            {
                "date": (config["start"] + timedelta(days=offset)).isoformat(),
                "eto_mm": f"{max(0.4, eto):.5f}",
                "effective_precipitation_mm": f"{precipitation:.5f}",
            }
        )
    return rows


def irrigation_features(config: dict, field) -> list[dict]:
    min_x, min_y, max_x, max_y = field.bounds
    cx, cy = (min_x + max_x) / 2.0, (min_y + max_y) / 2.0
    width, height = max_x - min_x, max_y - min_y
    definitions = [
        (16, 10.0, box(min_x - 20, min_y - 20, max_x + 20, max_y + 20), 0.0),
        (33, 14.5, box(cx - 0.50 * width, cy - 0.13 * height, cx + 0.50 * width, cy + 0.13 * height), 7.0),
        (51, 12.0, box(cx - 0.16 * width, cy - 0.55 * height, cx + 0.16 * width, cy + 0.55 * height), -11.0),
        (69, 17.0, box(cx - 0.48 * width, cy - 0.12 * height, cx + 0.12 * width, cy + 0.19 * height), 19.0),
        (86, 9.5, box(cx - 0.18 * width, cy - 0.52 * height, cx + 0.20 * width, cy + 0.52 * height), 5.0),
        (103, 15.0, box(cx - 0.12 * width, cy - 0.50 * height, cx + 0.50 * width, cy + 0.14 * height), -17.0),
        (config["days"] - 24, 12.5, box(cx - 0.52 * width, cy - 0.17 * height, cx + 0.52 * width, cy + 0.16 * height), 13.0),
        (config["days"] - 13, 55.0, box(cx - 0.50 * width, cy - 0.51 * height, cx - 0.02 * width, cy + 0.51 * height), -6.0),
        (config["days"] - 13, 45.0, box(cx - 0.05 * width, cy - 0.50 * height, cx + 0.23 * width, cy + 0.52 * height), 9.0),
        (config["days"] - 7, 70.0, box(cx + 0.18 * width, cy - 0.50 * height, cx + 0.52 * width, cy + 0.50 * height), -4.0),
    ]
    multipart = MultiPolygon(
        [
            box(min_x - 0.03 * width, cy - 0.31 * height, min_x + 0.23 * width, cy + 0.29 * height),
            box(max_x - 0.21 * width, cy - 0.27 * height, max_x + 0.03 * width, cy + 0.33 * height),
        ]
    )
    definitions.append((config["days"] - 24, 7.75, multipart, 3.0))
    features: list[dict] = []
    for index, (offset, depth, rectangle, degrees) in enumerate(definitions, start=1):
        geometry = affinity.rotate(rectangle, degrees, origin=(cx, cy), use_radians=False)
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "event_id": f"E{index:02d}",
                    "date": (config["start"] + timedelta(days=offset)).isoformat(),
                    "gross_depth_mm": depth,
                },
                "geometry": mapping(geometry),
            }
        )
    return features


def write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def build_campaign(root: Path, config: dict) -> dict:
    campaign_id = config["campaign_id"]
    directory = root / "campaigns" / campaign_id
    directory.mkdir(parents=True, exist_ok=True)
    field = field_geometry(config)
    min_x, _, _, max_y = field.bounds
    cell_width, cell_height = config["cell_size_m"]
    origin_x = math.floor(min_x / cell_width) * cell_width
    origin_y = math.ceil(max_y / cell_height) * cell_height
    units = analysis_units(field, origin_x, origin_y, cell_width, cell_height)
    soil_features, soil_rows = soil_partition(config, field)

    job = {
        "campaign_id": campaign_id,
        "crs": "EPSG:32614",
        "grid": {
            "origin_x": origin_x,
            "origin_y": origin_y,
            "cell_width_m": cell_width,
            "cell_height_m": cell_height,
            "row_direction": "north_to_south",
            "column_direction": "west_to_east",
        },
        "simulation": {
            "start_date": config["start"].isoformat(),
            "end_date": (config["start"] + timedelta(days=config["days"] - 1)).isoformat(),
        },
        "crop": {
            "root_depth_curve": [
                {
                    "date": (
                        config["start"]
                        + timedelta(days=round(fraction * (config["days"] - 1)))
                    ).isoformat(),
                    "root_depth_m": depth,
                }
                for fraction, depth in config["root_depth_curve"]
            ],
            "depletion_fraction": config["depletion_fraction"],
            "kc_slope": config["kc"][0],
            "kc_intercept": config["kc"][1],
            "kc_min": config["kc"][2],
            "kc_max": config["kc"][3],
        },
        "irrigation": {
            "efficiency": config["efficiency"],
            "max_application_mm": config["max_application_mm"],
        },
        "management_zone_edges_mm": list(config["zone_edges_mm"]),
    }
    dump_json(directory / "job_ticket.json", job)
    dump_json(
        directory / "field_boundary.geojson",
        {"type": "Feature", "properties": {"campaign_id": campaign_id}, "geometry": mapping(field)},
    )
    dump_json(directory / "soil_map_units.geojson", {"type": "FeatureCollection", "features": soil_features})
    write_csv(
        directory / "soil_horizons.csv",
        ["map_unit_id", "top_cm", "bottom_cm", "theta_fc", "theta_wp"],
        soil_rows,
    )
    initial_rows = []
    for unit in units:
        fraction = 0.12 + 0.72 * (
            ((unit["row"] * 37 + unit["column"] * 19 + config["seed"]) % 211) / 210.0
        )
        initial_rows.append({"unit_id": unit["unit_id"], "depletion_fraction": f"{fraction:.8f}"})
    write_csv(directory / "initial_depletion.csv", ["unit_id", "depletion_fraction"], initial_rows)
    write_csv(directory / "vegetation_index.csv", ["unit_id", "date", "vi"], vegetation_rows(config, units))
    write_csv(
        directory / "weather.csv",
        ["date", "eto_mm", "effective_precipitation_mm"],
        weather_rows(config),
    )
    dump_json(
        directory / "irrigation_events.geojson",
        {"type": "FeatureCollection", "features": irrigation_features(config, field)},
    )
    return {"campaign_id": campaign_id, "directory": f"/app/input/campaigns/{campaign_id}"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("/app"))
    args = parser.parse_args()
    input_root = args.root / "input"
    input_root.mkdir(parents=True, exist_ok=True)
    records = [build_campaign(input_root, config) for config in CAMPAIGNS]
    records.sort(key=lambda item: item["campaign_id"].encode())
    dump_json(input_root / "manifest.json", {"schema_version": 1, "campaigns": records})


if __name__ == "__main__":
    main()
