"""Independent verifier-side implementation of the published simulation contract."""
from __future__ import annotations

import bisect
import csv
import json
import math
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from shapely.geometry import box, shape


def load_json(path: Path):
    with path.open() as handle:
        return json.load(handle)


def table(path: Path):
    with path.open(newline="") as handle:
        yield from csv.DictReader(handle)


def clipped_grid(boundary, grid):
    """Enumerate north-to-south cells directly from the specified affine grid."""
    ox, oy = float(grid["origin_x"]), float(grid["origin_y"])
    dx, dy = float(grid["cell_width_m"]), float(grid["cell_height_m"])
    west, south, east, north = boundary.bounds
    columns = range(math.floor((west - ox) / dx), math.ceil((east - ox) / dx))
    rows = range(math.floor((oy - north) / dy), math.ceil((oy - south) / dy))
    result = []
    for row in rows:
        y1 = oy - row * dy
        for column in columns:
            x0 = ox + column * dx
            intersection = boundary.intersection(box(x0, y1 - dy, x0 + dx, y1))
            if intersection.area > 0:
                result.append(
                    {
                        "unit_id": f"r{row:04d}c{column:04d}",
                        "row": row,
                        "column": column,
                        "geometry": intersection,
                    }
                )
    return result


def horizon_rows(path: Path):
    return [
        (
            row["map_unit_id"],
            float(row["top_cm"]),
            float(row["bottom_cm"]),
            float(row["theta_fc"]),
            float(row["theta_wp"]),
        )
        for row in table(path)
    ]


def map_unit_storage(horizons, root_depth_m: float):
    root_cm = 100.0 * root_depth_m
    by_map = defaultdict(float)
    for map_unit_id, top_cm, bottom_cm, theta_fc, theta_wp in horizons:
        upper = max(0.0, top_cm)
        lower = min(root_cm, bottom_cm)
        if lower > upper:
            by_map[map_unit_id] += (lower - upper) * (theta_fc - theta_wp) * 10.0
    return by_map


def survey_weights(cell, survey):
    pieces = []
    for map_unit_id, polygon in survey:
        if not cell.intersects(polygon):
            continue
        area = cell.intersection(polygon).area
        if area:
            pieces.append((map_unit_id, area))
    covered = math.fsum(piece[1] for piece in pieces)
    if abs(covered - cell.area) > 1e-6:
        raise AssertionError("the published soil partition does not cover a reference cell")
    return [(map_unit_id, area / cell.area) for map_unit_id, area in pieces]


def capacity_for_cell(weights, storage):
    return math.fsum(fraction * storage[map_unit_id] for map_unit_id, fraction in weights)


def interpolate_series(series, wanted_ordinal):
    days = [item[0] for item in series]
    position = bisect.bisect_right(days, wanted_ordinal)
    if position == 0:
        return series[0][1]
    if position == len(series):
        return series[-1][1]
    day0, value0 = series[position - 1]
    day1, value1 = series[position]
    return value0 + (value1 - value0) * (wanted_ordinal - day0) / (day1 - day0)


def classify_zone(value, edges):
    return f"Z{bisect.bisect_right(edges, value) - 1}"


def calculate_campaign(campaign):
    directory = Path(campaign["directory"])
    ticket = load_json(directory / "job_ticket.json")
    boundary = shape(load_json(directory / "field_boundary.geojson")["geometry"])
    field_area = boundary.area
    cells = clipped_grid(boundary, ticket["grid"])

    soil_doc = load_json(directory / "soil_map_units.geojson")
    survey = [
        (feature["properties"]["map_unit_id"], shape(feature["geometry"]))
        for feature in soil_doc["features"]
    ]
    horizons = horizon_rows(directory / "soil_horizons.csv")
    start = date.fromisoformat(ticket["simulation"]["start_date"])
    finish = date.fromisoformat(ticket["simulation"]["end_date"])
    calendar = [start + timedelta(days=index) for index in range((finish - start).days + 1)]
    weather = {
        date.fromisoformat(row["date"]): (
            float(row["eto_mm"]),
            float(row["effective_precipitation_mm"]),
        )
        for row in table(directory / "weather.csv")
    }
    initial = {row["unit_id"]: float(row["depletion_fraction"]) for row in table(directory / "initial_depletion.csv")}
    satellite = defaultdict(list)
    for row in table(directory / "vegetation_index.csv"):
        satellite[row["unit_id"]].append((date.fromisoformat(row["date"]).toordinal(), float(row["vi"])))
    for series in satellite.values():
        series.sort(key=lambda item: item[0])

    event_doc = load_json(directory / "irrigation_events.geojson")
    events = defaultdict(list)
    for feature in event_doc["features"]:
        events[date.fromisoformat(feature["properties"]["date"])].append(
            (shape(feature["geometry"]), float(feature["properties"]["gross_depth_mm"]))
        )

    crop = ticket["crop"]
    root_curve = [
        (date.fromisoformat(item["date"]).toordinal(), float(item["root_depth_m"]))
        for item in crop["root_depth_curve"]
    ]
    storages = {
        current: map_unit_storage(horizons, interpolate_series(root_curve, current.toordinal()))
        for current in calendar
    }
    reference_p = float(crop["depletion_fraction"])
    efficiency = float(ticket["irrigation"]["efficiency"])
    maximum = float(ticket["irrigation"]["max_application_mm"])
    edges = [float(value) for value in ticket["management_zone_edges_mm"]]
    results = []
    for cell in cells:
        geometry = cell["geometry"]
        area = geometry.area
        weights = survey_weights(geometry, survey)
        taw = capacity_for_cell(weights, storages[start])
        depletion = initial[cell["unit_id"]] * taw
        accumulated_etc = 0.0
        accumulated_irrigation = 0.0
        accumulated_drainage = 0.0
        minimum_stress = 1.0
        stressed = 0
        final_kc = None
        raw = None

        effective_events = defaultdict(float)
        for event_date, footprints in events.items():
            covered_gross = math.fsum(
                depth * geometry.intersection(footprint).area / area
                for footprint, depth in footprints
                if geometry.intersects(footprint)
            )
            effective_events[event_date] = efficiency * covered_gross

        for current in calendar:
            taw = capacity_for_cell(weights, storages[current])
            depletion = min(depletion, taw)
            vi = interpolate_series(satellite[cell["unit_id"]], current.toordinal())
            kc = max(float(crop["kc_min"]), min(float(crop["kc_max"]), float(crop["kc_slope"]) * vi + float(crop["kc_intercept"])))
            eto, rain = weather[current]
            potential_use = kc * eto
            p = max(0.1, min(0.8, reference_p + 0.04 * (5.0 - potential_use)))
            raw = p * taw
            stress = 1.0 if depletion <= raw else max(0.0, min(1.0, (taw - depletion) / (taw - raw)))
            stressed += int(stress < 1.0)
            minimum_stress = min(minimum_stress, stress)
            crop_use = stress * kc * eto
            applied = effective_events[current]
            candidate = depletion + crop_use - rain - applied
            drainage = max(0.0, -candidate)
            depletion = max(0.0, min(taw, candidate))
            accumulated_etc += crop_use
            accumulated_irrigation += applied
            accumulated_drainage += drainage
            final_kc = kc

        gross = 0.0 if depletion <= raw else min(maximum, depletion / efficiency)
        results.append(
            {
                "campaign_id": campaign["campaign_id"],
                "unit_id": cell["unit_id"],
                "row": cell["row"],
                "column": cell["column"],
                "zone_id": classify_zone(gross, edges),
                "clipped_area_m2": area,
                "area_fraction": area / field_area,
                "taw_mm": taw,
                "raw_mm": raw,
                "final_dr_mm": depletion,
                "final_kc": final_kc,
                "recommended_gross_mm": gross,
                "seasonal_etc_mm": accumulated_etc,
                "seasonal_effective_irrigation_mm": accumulated_irrigation,
                "seasonal_drainage_mm": accumulated_drainage,
                "stress_days": stressed,
                "minimum_ks": minimum_stress,
                "geometry": geometry,
            }
        )

    zone_rows = []
    for number in range(len(edges)):
        zone_id = f"Z{number}"
        members = [row for row in results if row["zone_id"] == zone_id]
        area = math.fsum(row["clipped_area_m2"] for row in members)
        depth_area = math.fsum(row["clipped_area_m2"] * row["recommended_gross_mm"] for row in members)
        zone_rows.append(
            {
                "zone_id": zone_id,
                "unit_count": len(members),
                "area_m2": area,
                "area_fraction": area / field_area,
                "mean_depth_mm": depth_area / area if area else 0.0,
            }
        )
    total_depth_area = math.fsum(row["clipped_area_m2"] * row["recommended_gross_mm"] for row in results)
    summary = {
        "campaign_id": campaign["campaign_id"],
        "analysis_unit_count": len(results),
        "field_area_m2": field_area,
        "irrigated_area_m2": math.fsum(
            row["clipped_area_m2"] for row in results if row["recommended_gross_mm"] > 0
        ),
        "area_weighted_mean_depth_mm": total_depth_area / field_area,
        "total_gross_volume_m3": total_depth_area / 1000.0,
        "zones": zone_rows,
    }
    return results, summary


def calculate(input_root: Path):
    manifest = load_json(input_root / "manifest.json")
    all_units = []
    summaries = []
    for campaign in manifest["campaigns"]:
        units, summary = calculate_campaign(campaign)
        all_units.extend(units)
        summaries.append(summary)
    return all_units, summaries
