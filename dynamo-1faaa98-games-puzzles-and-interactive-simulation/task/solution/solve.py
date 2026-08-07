#!/usr/bin/env python3
"""Reference implementation of the irrigation response-frontier contract."""
from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from shapely.geometry import box, mapping, shape


DEFAULT_INPUT = Path("/app/input")
DEFAULT_OUTPUT = Path("/app/output")
SCENARIO_IDS = ("critical", "severe", "restricted", "nominal", "recovery")
BASE_FIELDS = [
    "campaign_id",
    "unit_id",
    "row",
    "column",
    "clipped_area_m2",
    "area_fraction",
    "taw_mm",
    "raw_mm",
    "final_dr_mm",
    "final_kc",
    "final_ec_ds_m",
    "minimum_k_sal",
    "request_mm",
    "water_need_index",
    "salt_need_index",
    "history_need_index",
    "seasonal_etc_mm",
    "seasonal_effective_irrigation_mm",
    "seasonal_drainage_mm",
    "seasonal_leached_salt_index",
    "stress_days",
    "salinity_stress_days",
    "minimum_ks",
]
UNIT_FIELDS = BASE_FIELDS + [
    field
    for scenario_id in SCENARIO_IDS
    for field in (
        f"{scenario_id}_priority",
        f"{scenario_id}_depth_mm",
        f"{scenario_id}_shortfall_mm",
    )
] + [
    "activation_scenario",
    "satisfaction_scenario",
    "robustness_score",
    "frontier_gain_mm",
]
INTEGER_FIELDS = {"row", "column", "stress_days", "salinity_stress_days"}
TEXT_FIELDS = {"campaign_id", "unit_id", "activation_scenario", "satisfaction_scenario"}


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


def allocate_scenario(
    records: list[dict], scenario_id: str, budget: float, satisfaction_ratio: float
) -> dict:
    """Solve one weighted quadratic allocation and return its KKT certificate."""
    requested_volume = math.fsum(
        record["clipped_area_m2"] * record["request_mm"] / 1000.0
        for record in records
    )
    binding = requested_volume > budget
    if binding:
        low = 0.0
        high = max(
            2.0 * record[f"{scenario_id}_priority"] * record["request_mm"]
            for record in records
        )
        for _ in range(100):
            shadow = (low + high) / 2.0
            volume = math.fsum(
                record["clipped_area_m2"]
                * max(
                    0.0,
                    record["request_mm"]
                    - shadow / (2.0 * record[f"{scenario_id}_priority"]),
                )
                / 1000.0
                for record in records
            )
            if volume > budget:
                low = shadow
            else:
                high = shadow
        shadow = high
    else:
        shadow = 0.0

    allocated_volume = 0.0
    weighted_cost = 0.0
    service_ratios = []
    active = 0
    for record in records:
        depth = max(
            0.0,
            record["request_mm"]
            - shadow / (2.0 * record[f"{scenario_id}_priority"]),
        )
        shortfall = record["request_mm"] - depth
        record[f"{scenario_id}_depth_mm"] = depth
        record[f"{scenario_id}_shortfall_mm"] = shortfall
        allocated_volume += record["clipped_area_m2"] * depth / 1000.0
        weighted_cost += (
            record["clipped_area_m2"]
            * record[f"{scenario_id}_priority"]
            * shortfall
            * shortfall
        )
        active += int(depth > 1e-12)
        service_ratios.append(
            1.0 if record["request_mm"] <= 1e-12 else depth / record["request_mm"]
        )

    satisfied = sum(
        record[f"{scenario_id}_depth_mm"]
        >= satisfaction_ratio * record["request_mm"]
        for record in records
    )
    return {
        "scenario_id": scenario_id,
        "budget_m3": budget,
        "allocated_volume_m3": allocated_volume,
        "shortfall_volume_m3": requested_volume - allocated_volume,
        "binding": binding,
        "depth_shadow_price": shadow,
        "weighted_shortfall_cost": weighted_cost,
        "active_unit_count": active,
        "satisfied_unit_count": satisfied,
        "mean_service_ratio": math.fsum(service_ratios) / len(service_ratios),
        "transition_volume_m3": 0.0,
    }


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
    initial_salinity = {
        record["unit_id"]: float(record["initial_ec_ds_m"])
        for record in read_csv(directory / "initial_salinity.csv")
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
    events_by_day: dict[date, list[tuple[object, float, float]]] = defaultdict(list)
    for feature in event_document["features"]:
        properties = feature["properties"]
        events_by_day[date.fromisoformat(properties["date"])].append(
            (
                shape(feature["geometry"]),
                float(properties["gross_depth_mm"]),
                float(properties["water_ec_ds_m"]),
            )
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
    salinity = job["salinity"]
    rain_ec = float(salinity["rainfall_ec_ds_m"])
    threshold_ec = float(salinity["crop_threshold_ec_ds_m"])
    yield_slope = float(salinity["yield_slope_per_ds_m"])
    minimum_k_sal = float(salinity["minimum_stress_coefficient"])
    leaching_efficiency = float(salinity["leaching_efficiency"])
    new_root_ec = float(salinity["new_root_zone_ec_ds_m"])
    minimum_solution = float(salinity["minimum_solution_depth_mm"])
    leaching_requirement = float(salinity["leaching_requirement_mm_per_ds_m"])
    response_frontier = job["response_frontier"]
    satisfaction_ratio = float(response_frontier["satisfaction_ratio"])
    scenario_definitions = list(response_frontier["scenarios"]) + [
        {
            "scenario_id": "recovery",
            "nominal_budget_fraction": None,
            "water_weight_multiplier": 1.0,
            "salinity_weight_multiplier": 1.0,
            "history_weight_multiplier": 1.0,
        }
    ]
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
        salt_load = initial_salinity[unit["unit_id"]] * max(
            taw - depletion, minimum_solution
        )
        previous_taw = taw
        seasonal_etc = 0.0
        seasonal_irrigation = 0.0
        seasonal_drainage = 0.0
        seasonal_leached_salt = 0.0
        stress_days = 0
        salinity_stress_days = 0
        minimum_ks = 1.0
        minimum_salinity_stress = 1.0
        final_kc = 0.0
        raw = 0.0

        event_depths: dict[date, tuple[float, float]] = {}
        for event_day, event_list in events_by_day.items():
            effective_depth = 0.0
            salt_input = 0.0
            for event_geometry, event_depth, event_ec in event_list:
                overlap = geometry.intersection(event_geometry).area
                if overlap > 0.0:
                    event_effective = event_depth * overlap / area * efficiency
                    effective_depth += event_effective
                    salt_input += event_effective * event_ec
            event_depths[event_day] = (effective_depth, salt_input)

        for current_day in days:
            taw = weighted_taw(soil_weights, daily_storage[current_day])
            depletion = min(depletion, taw)
            if taw > previous_taw:
                salt_load += (taw - previous_taw) * new_root_ec
            previous_taw = taw
            root_zone_ec = salt_load / max(taw - depletion, minimum_solution)
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
            k_sal = min(
                1.0,
                max(
                    minimum_k_sal,
                    1.0 - yield_slope * max(root_zone_ec - threshold_ec, 0.0),
                ),
            )
            if k_sal < 1.0:
                salinity_stress_days += 1
            minimum_salinity_stress = min(minimum_salinity_stress, k_sal)
            etc = ks * k_sal * kc * eto
            effective_irrigation, irrigation_salt = event_depths.get(
                current_day, (0.0, 0.0)
            )
            storage_before = taw - depletion
            unconstrained = depletion + etc - precipitation - effective_irrigation
            drainage = max(0.0, -unconstrained)
            depletion = min(taw, max(0.0, unconstrained))
            salt_before_leaching = salt_load + precipitation * rain_ec + irrigation_salt
            leaching_fraction = leaching_efficiency * min(
                1.0,
                drainage
                / max(storage_before + precipitation + effective_irrigation, minimum_solution),
            )
            leached_salt = salt_before_leaching * leaching_fraction
            salt_load = max(0.0, salt_before_leaching - leached_salt)
            seasonal_etc += etc
            seasonal_irrigation += effective_irrigation
            seasonal_drainage += drainage
            seasonal_leached_salt += leached_salt
            final_kc = kc

        final_ec = salt_load / max(taw - depletion, minimum_solution)
        deficit_request = 0.0 if depletion <= raw else depletion / efficiency
        leaching_request = (
            max(final_ec - threshold_ec, 0.0) * leaching_requirement / efficiency
        )
        request = min(maximum, deficit_request + leaching_request)
        water_need = depletion / max(taw, minimum_solution)
        salt_need = max(final_ec / threshold_ec - 1.0, 0.0)
        history_need = (stress_days + salinity_stress_days) / len(days)
        properties = {
            "campaign_id": campaign_id,
            "unit_id": unit["unit_id"],
            "row": unit["row"],
            "column": unit["column"],
            "clipped_area_m2": area,
            "area_fraction": area / field_area,
            "taw_mm": taw,
            "raw_mm": raw,
            "final_dr_mm": depletion,
            "final_kc": final_kc,
            "final_ec_ds_m": final_ec,
            "minimum_k_sal": minimum_salinity_stress,
            "request_mm": request,
            "water_need_index": water_need,
            "salt_need_index": salt_need,
            "history_need_index": history_need,
            "seasonal_etc_mm": seasonal_etc,
            "seasonal_effective_irrigation_mm": seasonal_irrigation,
            "seasonal_drainage_mm": seasonal_drainage,
            "seasonal_leached_salt_index": seasonal_leached_salt,
            "stress_days": stress_days,
            "salinity_stress_days": salinity_stress_days,
            "minimum_ks": minimum_ks,
        }
        for definition in scenario_definitions:
            scenario_id = definition["scenario_id"]
            properties[f"{scenario_id}_priority"] = (
                1.0
                + float(job["pump"]["water_deficit_priority_weight"])
                * water_need
                * float(definition["water_weight_multiplier"])
                + float(job["pump"]["salinity_priority_weight"])
                * salt_need
                * float(definition["salinity_weight_multiplier"])
                + float(job["pump"]["stress_history_priority_weight"])
                * history_need
                * float(definition["history_weight_multiplier"])
            )
        properties_records.append(properties)
        features.append({"type": "Feature", "geometry": mapping(geometry), "properties": properties})

    requested_volume = math.fsum(
        record["clipped_area_m2"] * record["request_mm"] / 1000.0
        for record in properties_records
    )
    nominal_budget = float(job["pump"]["volume_budget_m3"])
    scenario_certificates = []
    previous_volume = 0.0
    for definition in scenario_definitions:
        scenario_id = definition["scenario_id"]
        budget = (
            requested_volume
            if scenario_id == "recovery"
            else nominal_budget * float(definition["nominal_budget_fraction"])
        )
        certificate = allocate_scenario(
            properties_records, scenario_id, budget, satisfaction_ratio
        )
        certificate["transition_volume_m3"] = (
            certificate["allocated_volume_m3"] - previous_volume
        )
        previous_volume = certificate["allocated_volume_m3"]
        scenario_certificates.append(certificate)

    for record in properties_records:
        if record["request_mm"] <= 1e-12:
            record["activation_scenario"] = "none"
            record["satisfaction_scenario"] = "none"
            record["robustness_score"] = 1.0
        else:
            record["activation_scenario"] = next(
                scenario_id
                for scenario_id in SCENARIO_IDS
                if record[f"{scenario_id}_depth_mm"] > 1e-12
            )
            record["satisfaction_scenario"] = next(
                scenario_id
                for scenario_id in SCENARIO_IDS
                if record[f"{scenario_id}_depth_mm"]
                >= satisfaction_ratio * record["request_mm"]
            )
            record["robustness_score"] = math.fsum(
                record[f"{scenario_id}_depth_mm"] / record["request_mm"]
                for scenario_id in SCENARIO_IDS[:-1]
            ) / 4.0
        record["frontier_gain_mm"] = (
            record["recovery_depth_mm"] - record["critical_depth_mm"]
        )

    certificate = {
        "campaign_id": campaign_id,
        "analysis_unit_count": len(properties_records),
        "field_area_m2": field_area,
        "requested_volume_m3": requested_volume,
        "satisfaction_ratio": satisfaction_ratio,
        "scenarios": scenario_certificates,
    }
    ordered_records = [
        {field: record[field] for field in UNIT_FIELDS}
        for record in properties_records
    ]
    for feature, ordered in zip(features, ordered_records, strict=True):
        feature["properties"] = ordered
    return ordered_records, features, certificate


def main(input_root: Path = DEFAULT_INPUT, output_root: Path = DEFAULT_OUTPUT) -> None:
    manifest = read_json(input_root / "manifest.json")
    records: list[dict] = []
    features: list[dict] = []
    certificates: list[dict] = []
    for campaign in manifest["campaigns"]:
        relocated = {
            **campaign,
            "directory": str(input_root / "campaigns" / campaign["campaign_id"]),
        }
        campaign_records, campaign_features, campaign_certificate = simulate_campaign(
            relocated
        )
        records.extend(campaign_records)
        features.extend(campaign_features)
        certificates.append(campaign_certificate)

    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "allocation-frontier.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": features}, separators=(",", ":")) + "\n"
    )
    with (output_root / "allocation-frontier.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=UNIT_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(csv_record(record) for record in records)
    (output_root / "optimality-certificate.json").write_text(
        json.dumps({"schema_version": 2, "campaigns": certificates}, separators=(",", ":")) + "\n"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-root", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT)
    arguments = parser.parse_args()
    main(arguments.input_root, arguments.output_root)
