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

SCENARIO_IDS = ("critical", "severe", "restricted", "nominal", "recovery")


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


def allocate_frontier_point(rows, scenario_id, budget, satisfaction_ratio):
    requested_volume = math.fsum(
        row["clipped_area_m2"] * row["request_mm"] / 1000.0 for row in rows
    )
    binding = requested_volume > budget
    if binding:
        lower = 0.0
        upper = max(
            2.0 * row[f"{scenario_id}_priority"] * row["request_mm"]
            for row in rows
        )
        for _ in range(100):
            shadow = (lower + upper) / 2.0
            used = math.fsum(
                row["clipped_area_m2"]
                * max(
                    0.0,
                    row["request_mm"]
                    - shadow / (2.0 * row[f"{scenario_id}_priority"]),
                )
                / 1000.0
                for row in rows
            )
            if used > budget:
                lower = shadow
            else:
                upper = shadow
        shadow = upper
    else:
        shadow = 0.0

    for row in rows:
        allocated = max(
            0.0,
            row["request_mm"]
            - shadow / (2.0 * row[f"{scenario_id}_priority"]),
        )
        row[f"{scenario_id}_depth_mm"] = allocated
        row[f"{scenario_id}_shortfall_mm"] = row["request_mm"] - allocated

    allocated_volume = math.fsum(
        row["clipped_area_m2"] * row[f"{scenario_id}_depth_mm"] / 1000.0
        for row in rows
    )
    return {
        "scenario_id": scenario_id,
        "budget_m3": budget,
        "allocated_volume_m3": allocated_volume,
        "shortfall_volume_m3": requested_volume - allocated_volume,
        "binding": binding,
        "depth_shadow_price": shadow,
        "weighted_shortfall_cost": math.fsum(
            row["clipped_area_m2"]
            * row[f"{scenario_id}_priority"]
            * row[f"{scenario_id}_shortfall_mm"] ** 2
            for row in rows
        ),
        "active_unit_count": sum(
            row[f"{scenario_id}_depth_mm"] > 1e-12 for row in rows
        ),
        "satisfied_unit_count": sum(
            row[f"{scenario_id}_depth_mm"]
            >= satisfaction_ratio * row["request_mm"]
            for row in rows
        ),
        "mean_service_ratio": math.fsum(
            1.0
            if row["request_mm"] <= 1e-12
            else row[f"{scenario_id}_depth_mm"] / row["request_mm"]
            for row in rows
        )
        / len(rows),
        "transition_volume_m3": 0.0,
    }


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
    initial_ec = {
        row["unit_id"]: float(row["initial_ec_ds_m"])
        for row in table(directory / "initial_salinity.csv")
    }
    satellite = defaultdict(list)
    for row in table(directory / "vegetation_index.csv"):
        satellite[row["unit_id"]].append((date.fromisoformat(row["date"]).toordinal(), float(row["vi"])))
    for series in satellite.values():
        series.sort(key=lambda item: item[0])

    event_doc = load_json(directory / "irrigation_events.geojson")
    events = defaultdict(list)
    for feature in event_doc["features"]:
        events[date.fromisoformat(feature["properties"]["date"])].append(
            (
                shape(feature["geometry"]),
                float(feature["properties"]["gross_depth_mm"]),
                float(feature["properties"]["water_ec_ds_m"]),
            )
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
    salt_contract = ticket["salinity"]
    rain_ec = float(salt_contract["rainfall_ec_ds_m"])
    threshold_ec = float(salt_contract["crop_threshold_ec_ds_m"])
    yield_slope = float(salt_contract["yield_slope_per_ds_m"])
    minimum_k_sal = float(salt_contract["minimum_stress_coefficient"])
    leaching_efficiency = float(salt_contract["leaching_efficiency"])
    new_root_ec = float(salt_contract["new_root_zone_ec_ds_m"])
    solution_floor = float(salt_contract["minimum_solution_depth_mm"])
    leaching_requirement = float(salt_contract["leaching_requirement_mm_per_ds_m"])
    frontier = ticket["response_frontier"]
    satisfaction_ratio = float(frontier["satisfaction_ratio"])
    scenario_definitions = list(frontier["scenarios"]) + [
        {
            "scenario_id": "recovery",
            "nominal_budget_fraction": None,
            "water_weight_multiplier": 1.0,
            "salinity_weight_multiplier": 1.0,
            "history_weight_multiplier": 1.0,
        }
    ]
    results = []
    for cell in cells:
        geometry = cell["geometry"]
        area = geometry.area
        weights = survey_weights(geometry, survey)
        taw = capacity_for_cell(weights, storages[start])
        depletion = initial[cell["unit_id"]] * taw
        salt_mass = initial_ec[cell["unit_id"]] * max(taw - depletion, solution_floor)
        prior_taw = taw
        accumulated_etc = 0.0
        accumulated_irrigation = 0.0
        accumulated_drainage = 0.0
        accumulated_leaching = 0.0
        minimum_stress = 1.0
        minimum_salt_stress = 1.0
        stressed = 0
        salt_stressed = 0
        final_kc = None
        raw = None

        effective_events = defaultdict(lambda: (0.0, 0.0))
        for event_date, footprints in events.items():
            contributions = [
                (
                    depth * geometry.intersection(footprint).area / area * efficiency,
                    water_ec,
                )
                for footprint, depth, water_ec in footprints
                if geometry.intersects(footprint)
            ]
            effective_events[event_date] = (
                math.fsum(depth for depth, _ in contributions),
                math.fsum(depth * water_ec for depth, water_ec in contributions),
            )

        for current in calendar:
            taw = capacity_for_cell(weights, storages[current])
            depletion = min(depletion, taw)
            if taw > prior_taw:
                salt_mass += (taw - prior_taw) * new_root_ec
            prior_taw = taw
            root_zone_ec = salt_mass / max(taw - depletion, solution_floor)
            vi = interpolate_series(satellite[cell["unit_id"]], current.toordinal())
            kc = max(float(crop["kc_min"]), min(float(crop["kc_max"]), float(crop["kc_slope"]) * vi + float(crop["kc_intercept"])))
            eto, rain = weather[current]
            potential_use = kc * eto
            p = max(0.1, min(0.8, reference_p + 0.04 * (5.0 - potential_use)))
            raw = p * taw
            stress = 1.0 if depletion <= raw else max(0.0, min(1.0, (taw - depletion) / (taw - raw)))
            stressed += int(stress < 1.0)
            minimum_stress = min(minimum_stress, stress)
            salt_stress = max(
                minimum_k_sal,
                min(1.0, 1.0 - yield_slope * max(root_zone_ec - threshold_ec, 0.0)),
            )
            salt_stressed += int(salt_stress < 1.0)
            minimum_salt_stress = min(minimum_salt_stress, salt_stress)
            crop_use = stress * salt_stress * kc * eto
            applied, irrigation_salt = effective_events[current]
            stored_water = taw - depletion
            candidate = depletion + crop_use - rain - applied
            drainage = max(0.0, -candidate)
            depletion = max(0.0, min(taw, candidate))
            pre_leach_salt = salt_mass + rain * rain_ec + irrigation_salt
            removal_fraction = leaching_efficiency * min(
                1.0, drainage / max(stored_water + rain + applied, solution_floor)
            )
            leached = pre_leach_salt * removal_fraction
            salt_mass = max(0.0, pre_leach_salt - leached)
            accumulated_etc += crop_use
            accumulated_irrigation += applied
            accumulated_drainage += drainage
            accumulated_leaching += leached
            final_kc = kc

        final_ec = salt_mass / max(taw - depletion, solution_floor)
        water_request = 0.0 if depletion <= raw else depletion / efficiency
        salt_request = max(final_ec - threshold_ec, 0.0) * leaching_requirement / efficiency
        requested = min(maximum, water_request + salt_request)
        water_need = depletion / max(taw, solution_floor)
        salt_need = max(final_ec / threshold_ec - 1.0, 0.0)
        history_need = (stressed + salt_stressed) / len(calendar)
        result = {
                "campaign_id": campaign["campaign_id"],
                "unit_id": cell["unit_id"],
                "row": cell["row"],
                "column": cell["column"],
                "clipped_area_m2": area,
                "area_fraction": area / field_area,
                "taw_mm": taw,
                "raw_mm": raw,
                "final_dr_mm": depletion,
                "final_kc": final_kc,
                "final_ec_ds_m": final_ec,
                "minimum_k_sal": minimum_salt_stress,
                "request_mm": requested,
                "water_need_index": water_need,
                "salt_need_index": salt_need,
                "history_need_index": history_need,
                "seasonal_etc_mm": accumulated_etc,
                "seasonal_effective_irrigation_mm": accumulated_irrigation,
                "seasonal_drainage_mm": accumulated_drainage,
                "seasonal_leached_salt_index": accumulated_leaching,
                "stress_days": stressed,
                "salinity_stress_days": salt_stressed,
                "minimum_ks": minimum_stress,
                "geometry": geometry,
            }
        for definition in scenario_definitions:
            scenario_id = definition["scenario_id"]
            result[f"{scenario_id}_priority"] = (
                1.0
                + float(ticket["pump"]["water_deficit_priority_weight"])
                * water_need
                * float(definition["water_weight_multiplier"])
                + float(ticket["pump"]["salinity_priority_weight"])
                * salt_need
                * float(definition["salinity_weight_multiplier"])
                + float(ticket["pump"]["stress_history_priority_weight"])
                * history_need
                * float(definition["history_weight_multiplier"])
            )
        results.append(result)

    requested_volume = math.fsum(
        row["clipped_area_m2"] * row["request_mm"] / 1000.0
        for row in results
    )
    nominal_budget = float(ticket["pump"]["volume_budget_m3"])
    scenario_certificates = []
    previous_volume = 0.0
    for definition in scenario_definitions:
        scenario_id = definition["scenario_id"]
        budget = (
            requested_volume
            if scenario_id == "recovery"
            else nominal_budget * float(definition["nominal_budget_fraction"])
        )
        point = allocate_frontier_point(
            results, scenario_id, budget, satisfaction_ratio
        )
        point["transition_volume_m3"] = point["allocated_volume_m3"] - previous_volume
        previous_volume = point["allocated_volume_m3"]
        scenario_certificates.append(point)

    for row in results:
        if row["request_mm"] <= 1e-12:
            row["activation_scenario"] = "none"
            row["satisfaction_scenario"] = "none"
            row["robustness_score"] = 1.0
        else:
            row["activation_scenario"] = next(
                scenario_id
                for scenario_id in SCENARIO_IDS
                if row[f"{scenario_id}_depth_mm"] > 1e-12
            )
            row["satisfaction_scenario"] = next(
                scenario_id
                for scenario_id in SCENARIO_IDS
                if row[f"{scenario_id}_depth_mm"]
                >= satisfaction_ratio * row["request_mm"]
            )
            row["robustness_score"] = math.fsum(
                row[f"{scenario_id}_depth_mm"] / row["request_mm"]
                for scenario_id in SCENARIO_IDS[:-1]
            ) / 4.0
        row["frontier_gain_mm"] = row["recovery_depth_mm"] - row["critical_depth_mm"]

    certificate = {
        "campaign_id": campaign["campaign_id"],
        "analysis_unit_count": len(results),
        "field_area_m2": field_area,
        "requested_volume_m3": requested_volume,
        "satisfaction_ratio": satisfaction_ratio,
        "scenarios": scenario_certificates,
    }
    return results, certificate


def calculate(input_root: Path):
    manifest = load_json(input_root / "manifest.json")
    all_units = []
    summaries = []
    for campaign in manifest["campaigns"]:
        relocated = {
            **campaign,
            "directory": str(input_root / "campaigns" / campaign["campaign_id"]),
        }
        units, summary = calculate_campaign(relocated)
        all_units.extend(units)
        summaries.append(summary)
    return all_units, summaries
