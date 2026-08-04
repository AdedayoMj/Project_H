#!/usr/bin/env python3
"""Reference implementation of the prescribed agricultural world simulation."""
from __future__ import annotations

import csv
import json
import math
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from shapely.geometry import box, mapping, shape


INPUT = Path("/app/input")
OUTPUT = Path("/app/output")
UNIT_FIELDS = [
    "campaign_id",
    "unit_id",
    "row",
    "column",
    "zone_id",
    "clipped_area_m2",
    "area_fraction",
    "taw_mm",
    "raw_mm",
    "final_dr_mm",
    "final_kc",
    "recommended_gross_mm",
    "seasonal_etc_mm",
    "seasonal_effective_irrigation_mm",
    "seasonal_drainage_mm",
    "stress_days",
    "minimum_ks",
]
INTEGER_FIELDS = {"row", "column", "stress_days"}
TEXT_FIELDS = {"campaign_id", "unit_id", "zone_id"}


def decimal_text(value: float) -> str:
    """Serialize finite task values without exponent notation or precision loss."""
    text = format(float(value), ".17f").rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def csv_record(record: dict) -> dict:
    return {
        key: (
            record[key]
            if key in TEXT_FIELDS or key in INTEGER_FIELDS
            else decimal_text(record[key])
        )
        for key in UNIT_FIELDS
    }


def read_json(path: Path) -> object:
    return json.loads(path.read_text())


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def dates_inclusive(start: date, end: date) -> list[date]:
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def make_units(field, grid: dict) -> list[dict]:
    origin_x = float(grid["origin_x"])
    origin_y = float(grid["origin_y"])
    width = float(grid["cell_width_m"])
    height = float(grid["cell_height_m"])
    min_x, min_y, max_x, max_y = field.bounds
    first_column = math.floor((min_x - origin_x) / width)
    last_column = math.ceil((max_x - origin_x) / width) - 1
    first_row = math.floor((origin_y - max_y) / height)
    last_row = math.ceil((origin_y - min_y) / height) - 1
    units = []
    for row in range(first_row, last_row + 1):
        upper = origin_y - row * height
        lower = upper - height
        for column in range(first_column, last_column + 1):
            left = origin_x + column * width
            geometry = field.intersection(box(left, lower, left + width, upper))
            if geometry.area <= 0.0:
                continue
            units.append(
                {
                    "unit_id": f"r{row:04d}c{column:04d}",
                    "row": row,
                    "column": column,
                    "geometry": geometry,
                }
            )
    return units


def soil_horizons(directory: Path) -> list[dict]:
    return [
        {
            "map_unit_id": record["map_unit_id"],
            "top_cm": float(record["top_cm"]),
            "bottom_cm": float(record["bottom_cm"]),
            "theta_fc": float(record["theta_fc"]),
            "theta_wp": float(record["theta_wp"]),
        }
        for record in read_csv(directory / "soil_horizons.csv")
    ]


def soil_storages(horizons: list[dict], root_depth_m: float) -> dict[str, float]:
    root_bottom_cm = root_depth_m * 100.0
    result: dict[str, float] = defaultdict(float)
    for record in horizons:
        top = record["top_cm"]
        bottom = record["bottom_cm"]
        thickness = max(0.0, min(bottom, root_bottom_cm) - max(top, 0.0))
        result[record["map_unit_id"]] += (
            10.0 * (record["theta_fc"] - record["theta_wp"]) * thickness
        )
    return dict(result)


def soil_area_weights(unit_geometry, soil_features: list[tuple[str, object]]) -> dict[str, float]:
    overlaps = {}
    covered = 0.0
    u_min_x, u_min_y, u_max_x, u_max_y = unit_geometry.bounds
    for map_id, geometry in soil_features:
        min_x, min_y, max_x, max_y = geometry.bounds
        if max_x <= u_min_x or u_max_x <= min_x or max_y <= u_min_y or u_max_y <= min_y:
            continue
        overlap = unit_geometry.intersection(geometry).area
        if overlap > 0.0:
            overlaps[map_id] = overlap
            covered += overlap
    if not math.isclose(covered, unit_geometry.area, rel_tol=0.0, abs_tol=1e-6):
        raise RuntimeError(f"soil partition leaves {unit_geometry.area - covered} square metres uncovered")
    return {map_id: overlap / unit_geometry.area for map_id, overlap in overlaps.items()}


def weighted_taw(weights: dict[str, float], storage: dict[str, float]) -> float:
    return sum(fraction * storage[map_id] for map_id, fraction in weights.items())


def vegetation_by_unit(directory: Path) -> dict[str, list[tuple[int, float]]]:
    result: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for record in read_csv(directory / "vegetation_index.csv"):
        result[record["unit_id"]].append((date.fromisoformat(record["date"]).toordinal(), float(record["vi"])))
    for observations in result.values():
        observations.sort()
    return dict(result)


def interpolate(observations: list[tuple[int, float]], ordinal: int) -> float:
    if ordinal <= observations[0][0]:
        return observations[0][1]
    if ordinal >= observations[-1][0]:
        return observations[-1][1]
    low = 0
    high = len(observations) - 1
    while high - low > 1:
        middle = (low + high) // 2
        if observations[middle][0] <= ordinal:
            low = middle
        else:
            high = middle
    day_zero, value_zero = observations[low]
    day_one, value_one = observations[high]
    fraction = (ordinal - day_zero) / (day_one - day_zero)
    return value_zero + fraction * (value_one - value_zero)


def zone_for(depth: float, edges: list[float]) -> str:
    index = 0
    for candidate in range(1, len(edges)):
        if depth < edges[candidate]:
            break
        index = candidate
    return f"Z{index}"


def simulate_campaign(campaign: dict) -> tuple[list[dict], list[dict], dict]:
    directory = Path(campaign["directory"])
    job = read_json(directory / "job_ticket.json")
    field_document = read_json(directory / "field_boundary.geojson")
    field = shape(field_document["geometry"])
    field_area = field.area
    units = make_units(field, job["grid"])

    soil_document = read_json(directory / "soil_map_units.geojson")
    soil_features = [
        (feature["properties"]["map_unit_id"], shape(feature["geometry"]))
        for feature in soil_document["features"]
    ]
    horizons = soil_horizons(directory)
    initial_fraction = {
        record["unit_id"]: float(record["depletion_fraction"])
        for record in read_csv(directory / "initial_depletion.csv")
    }
    vegetation = vegetation_by_unit(directory)
    weather = {
        date.fromisoformat(record["date"]): (
            float(record["eto_mm"]),
            float(record["effective_precipitation_mm"]),
        )
        for record in read_csv(directory / "weather.csv")
    }
    event_document = read_json(directory / "irrigation_events.geojson")
    events_by_day: dict[date, list[tuple[object, float]]] = defaultdict(list)
    for feature in event_document["features"]:
        properties = feature["properties"]
        events_by_day[date.fromisoformat(properties["date"])].append(
            (shape(feature["geometry"]), float(properties["gross_depth_mm"]))
        )

    start = date.fromisoformat(job["simulation"]["start_date"])
    end = date.fromisoformat(job["simulation"]["end_date"])
    days = dates_inclusive(start, end)
    crop = job["crop"]
    root_curve = [
        (date.fromisoformat(item["date"]).toordinal(), float(item["root_depth_m"]))
        for item in crop["root_depth_curve"]
    ]
    daily_storage = {
        current_day: soil_storages(
            horizons, interpolate(root_curve, current_day.toordinal())
        )
        for current_day in days
    }
    depletion_fraction = float(crop["depletion_fraction"])
    efficiency = float(job["irrigation"]["efficiency"])
    maximum = float(job["irrigation"]["max_application_mm"])
    edges = [float(value) for value in job["management_zone_edges_mm"]]
    campaign_id = campaign["campaign_id"]
    properties_records: list[dict] = []
    features: list[dict] = []

    for unit in units:
        geometry = unit["geometry"]
        area = geometry.area
        soil_weights = soil_area_weights(geometry, soil_features)
        taw = weighted_taw(soil_weights, daily_storage[start])
        depletion = initial_fraction[unit["unit_id"]] * taw
        observations = vegetation[unit["unit_id"]]
        seasonal_etc = 0.0
        seasonal_irrigation = 0.0
        seasonal_drainage = 0.0
        stress_days = 0
        minimum_ks = 1.0
        final_kc = 0.0
        raw = 0.0

        event_depths: dict[date, float] = {}
        for event_day, event_list in events_by_day.items():
            gross = 0.0
            for event_geometry, event_depth in event_list:
                overlap = geometry.intersection(event_geometry).area
                if overlap > 0.0:
                    gross += event_depth * overlap / area
            event_depths[event_day] = gross * efficiency

        for current_day in days:
            taw = weighted_taw(soil_weights, daily_storage[current_day])
            depletion = min(depletion, taw)
            vi = interpolate(observations, current_day.toordinal())
            kc = min(float(crop["kc_max"]), max(float(crop["kc_min"]), float(crop["kc_slope"]) * vi + float(crop["kc_intercept"])))
            eto, precipitation = weather[current_day]
            potential_etc = kc * eto
            daily_p = min(0.8, max(0.1, depletion_fraction + 0.04 * (5.0 - potential_etc)))
            raw = daily_p * taw
            if depletion <= raw:
                ks = 1.0
            else:
                ks = min(1.0, max(0.0, (taw - depletion) / (taw - raw)))
            if ks < 1.0:
                stress_days += 1
            minimum_ks = min(minimum_ks, ks)
            etc = ks * kc * eto
            effective_irrigation = event_depths.get(current_day, 0.0)
            unconstrained = depletion + etc - precipitation - effective_irrigation
            drainage = max(0.0, -unconstrained)
            depletion = min(taw, max(0.0, unconstrained))
            seasonal_etc += etc
            seasonal_irrigation += effective_irrigation
            seasonal_drainage += drainage
            final_kc = kc

        recommendation = 0.0 if depletion <= raw else min(maximum, depletion / efficiency)
        properties = {
            "campaign_id": campaign_id,
            "unit_id": unit["unit_id"],
            "row": unit["row"],
            "column": unit["column"],
            "zone_id": zone_for(recommendation, edges),
            "clipped_area_m2": area,
            "area_fraction": area / field_area,
            "taw_mm": taw,
            "raw_mm": raw,
            "final_dr_mm": depletion,
            "final_kc": final_kc,
            "recommended_gross_mm": recommendation,
            "seasonal_etc_mm": seasonal_etc,
            "seasonal_effective_irrigation_mm": seasonal_irrigation,
            "seasonal_drainage_mm": seasonal_drainage,
            "stress_days": stress_days,
            "minimum_ks": minimum_ks,
        }
        properties_records.append(properties)
        features.append({"type": "Feature", "geometry": mapping(geometry), "properties": properties})

    zone_records = []
    for zone_index in range(len(edges)):
        zone_id = f"Z{zone_index}"
        members = [record for record in properties_records if record["zone_id"] == zone_id]
        zone_area = sum(record["clipped_area_m2"] for record in members)
        zone_weighted_depth = sum(
            record["clipped_area_m2"] * record["recommended_gross_mm"] for record in members
        )
        zone_records.append(
            {
                "zone_id": zone_id,
                "unit_count": len(members),
                "area_m2": zone_area,
                "area_fraction": zone_area / field_area,
                "mean_depth_mm": zone_weighted_depth / zone_area if zone_area else 0.0,
            }
        )
    weighted_depth = sum(
        record["clipped_area_m2"] * record["recommended_gross_mm"]
        for record in properties_records
    )
    summary = {
        "campaign_id": campaign_id,
        "analysis_unit_count": len(properties_records),
        "field_area_m2": field_area,
        "irrigated_area_m2": sum(
            record["clipped_area_m2"]
            for record in properties_records
            if record["recommended_gross_mm"] > 0.0
        ),
        "area_weighted_mean_depth_mm": weighted_depth / field_area,
        "total_gross_volume_m3": weighted_depth / 1000.0,
        "zones": zone_records,
    }
    return properties_records, features, summary


def main() -> None:
    manifest = read_json(INPUT / "manifest.json")
    records: list[dict] = []
    features: list[dict] = []
    summaries: list[dict] = []
    for campaign in manifest["campaigns"]:
        campaign_records, campaign_features, campaign_summary = simulate_campaign(campaign)
        records.extend(campaign_records)
        features.extend(campaign_features)
        summaries.append(campaign_summary)

    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "prescription.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": features}, separators=(",", ":")) + "\n"
    )
    with (OUTPUT / "units.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=UNIT_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(csv_record(record) for record in records)
    (OUTPUT / "summary.json").write_text(
        json.dumps({"schema_version": 1, "campaigns": summaries}, separators=(",", ":")) + "\n"
    )


if __name__ == "__main__":
    main()
