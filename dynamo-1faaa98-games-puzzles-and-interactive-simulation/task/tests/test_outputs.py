from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
from collections import defaultdict
from datetime import date
from functools import lru_cache
from pathlib import Path

import pytest
from shapely.geometry import shape

from reference_model import calculate


APP = Path(os.environ.get("IRRIGATION_APP_ROOT", "/app"))
INPUT = APP / "input"
OUTPUT = APP / "output"
TESTS = Path(__file__).parent
GEOJSON_PATH = OUTPUT / "prescription.geojson"
CSV_PATH = OUTPUT / "units.csv"
SUMMARY_PATH = OUTPUT / "summary.json"
SOLVER_PATH = OUTPUT / "solver.py"

PROPERTY_FIELDS = [
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
    "final_ec_ds_m",
    "minimum_k_sal",
    "recommended_gross_mm",
    "unconstrained_gross_mm",
    "allocation_priority",
    "allocation_shortfall_mm",
    "seasonal_etc_mm",
    "seasonal_effective_irrigation_mm",
    "seasonal_drainage_mm",
    "seasonal_leached_salt_index",
    "stress_days",
    "salinity_stress_days",
    "minimum_ks",
]
IDENTITY_FIELDS = ["campaign_id", "unit_id", "row", "column"]
SOIL_FIELDS = ["taw_mm", "raw_mm"]
DYNAMIC_STATE_DEPTH_FIELDS = [
    "final_dr_mm",
    "unconstrained_gross_mm",
    "allocation_shortfall_mm",
]
ACCUMULATED_DEPTH_FIELDS = [
    "seasonal_etc_mm",
    "seasonal_effective_irrigation_mm",
    "seasonal_drainage_mm",
    "seasonal_leached_salt_index",
]
COEFFICIENT_FIELDS = [
    "final_kc",
    "final_ec_ds_m",
    "minimum_ks",
    "minimum_k_sal",
    "allocation_priority",
]
SUMMARY_FIELDS = [
    "campaign_id",
    "analysis_unit_count",
    "field_area_m2",
    "irrigated_area_m2",
    "area_weighted_mean_depth_mm",
    "total_gross_volume_m3",
    "requested_gross_volume_m3",
    "pump_budget_m3",
    "allocation_shortfall_volume_m3",
    "quota_binding",
    "zones",
]
ZONE_FIELDS = ["zone_id", "unit_count", "area_m2", "area_fraction", "mean_depth_mm"]
INTEGER_TEXT = re.compile(r"[+-]?[0-9]+\Z")
DECIMAL_TEXT = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)\Z")
UNIT_NUMERIC_TOLERANCES = {
    "clipped_area_m2": (1e-6, 0.0),
    "area_fraction": (1e-6, 0.0),
    "taw_mm": (0.01, 0.001),
    "raw_mm": (0.01, 0.001),
    "final_dr_mm": (0.01, 0.001),
    "final_kc": (0.0001, 0.001),
    "final_ec_ds_m": (0.0001, 0.001),
    "minimum_k_sal": (0.0001, 0.001),
    "recommended_gross_mm": (0.01, 0.001),
    "unconstrained_gross_mm": (0.01, 0.001),
    "allocation_priority": (0.0001, 0.001),
    "allocation_shortfall_mm": (0.01, 0.001),
    "seasonal_etc_mm": (0.01, 0.0),
    "seasonal_effective_irrigation_mm": (0.01, 0.0),
    "seasonal_drainage_mm": (0.01, 0.0),
    "seasonal_leached_salt_index": (0.01, 0.0),
    "minimum_ks": (0.0001, 0.001),
}
BIAS_GUARDED_FIELDS = tuple(
    field
    for field in UNIT_NUMERIC_TOLERANCES
    if field not in {"clipped_area_m2", "area_fraction"}
)
SUMMARY_NUMERIC_TOLERANCES = {
    "field_area_m2": (1e-6, 0.0),
    "irrigated_area_m2": (1e-6, 0.0),
    "area_weighted_mean_depth_mm": (0.01, 0.001),
    "total_gross_volume_m3": (0.01, 0.001),
    "requested_gross_volume_m3": (0.01, 0.001),
    "pump_budget_m3": (0.01, 0.001),
    "allocation_shortfall_volume_m3": (0.01, 0.001),
}
ZONE_NUMERIC_TOLERANCES = {
    "area_m2": (1e-6, 0.0),
    "area_fraction": (1e-6, 0.0),
    "mean_depth_mm": (0.01, 0.001),
}
MAX_NORMALIZED_MEAN_BIAS = 0.05
MAX_NORMALIZED_RMS_RESIDUAL = 0.25


@lru_cache(maxsize=1)
def reference():
    return calculate(INPUT)


@lru_cache(maxsize=1)
def submitted_geojson():
    return json.loads(GEOJSON_PATH.read_text())


@lru_cache(maxsize=1)
def submitted_csv():
    with CSV_PATH.open(newline="") as handle:
        reader = csv.DictReader(handle)
        header = reader.fieldnames
        rows = list(reader)
    return header, rows


@lru_cache(maxsize=1)
def submitted_summary():
    return json.loads(SUMMARY_PATH.read_text())


def regular_file(path: Path) -> bool:
    return path.exists() and not path.is_symlink() and stat.S_ISREG(path.stat().st_mode)


def finite_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def close(actual, expected, absolute, relative=0.001):
    assert finite_number(actual)
    assert abs(float(actual) - float(expected)) <= max(absolute, relative * abs(float(expected)))


def normalized_residual(actual, expected, absolute, relative):
    scale = max(absolute, relative * abs(float(expected)))
    return (float(actual) - float(expected)) / scale


def assert_residual_distribution(actual_values, expected_values, absolute, relative, label):
    assert len(actual_values) == len(expected_values) and actual_values, label
    residuals = [
        normalized_residual(actual, expected, absolute, relative)
        for actual, expected in zip(actual_values, expected_values, strict=True)
    ]
    mean = math.fsum(residuals) / len(residuals)
    rms = math.sqrt(math.fsum(value * value for value in residuals) / len(residuals))
    assert abs(mean) <= MAX_NORMALIZED_MEAN_BIAS, (label, "mean_bias", mean)
    assert rms <= MAX_NORMALIZED_RMS_RESIDUAL, (label, "rms_residual", rms)


def assert_no_coherent_unit_bias(actual_rows, expected_rows):
    campaigns = sorted({row["campaign_id"] for row in expected_rows})
    for campaign_id in campaigns:
        actual_campaign = [row for row in actual_rows if row["campaign_id"] == campaign_id]
        expected_campaign = [row for row in expected_rows if row["campaign_id"] == campaign_id]
        assert len(actual_campaign) == len(expected_campaign)
        for field in BIAS_GUARDED_FIELDS:
            absolute, relative = UNIT_NUMERIC_TOLERANCES[field]
            assert_residual_distribution(
                [row[field] for row in actual_campaign],
                [row[field] for row in expected_campaign],
                absolute,
                relative,
                f"{campaign_id}:{field}",
            )


def decimal_text(value):
    text = format(float(value), ".12f").rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def mutate_csv(path, transforms):
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames
        rows = list(reader)
    assert fieldnames is not None
    for row in rows:
        for field, transform in transforms.items():
            row[field] = decimal_text(transform(float(row[field])))
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def build_counterfactual_input(destination):
    """Create a verifier-only one-campaign world with coupled state and budget perturbations."""
    manifest = json.loads((INPUT / "manifest.json").read_text())
    campaign = min(
        manifest["campaigns"],
        key=lambda item: (
            INPUT / "campaigns" / item["campaign_id"] / "initial_depletion.csv"
        ).stat().st_size,
    )
    campaign_id = campaign["campaign_id"]
    source = INPUT / "campaigns" / campaign_id
    target = destination / "campaigns" / campaign_id
    destination.mkdir(parents=True)
    shutil.copytree(source, target)
    shutil.copy2(INPUT / "specification.md", destination / "specification.md")

    ticket_path = target / "job_ticket.json"
    ticket = json.loads(ticket_path.read_text())
    ticket["crop"]["kc_intercept"] = float(ticket["crop"]["kc_intercept"]) + 0.017
    ticket["pump"]["volume_budget_m3"] = (
        float(ticket["pump"]["volume_budget_m3"]) * 0.82
    )
    ticket["salinity"]["rainfall_ec_ds_m"] = (
        float(ticket["salinity"]["rainfall_ec_ds_m"]) + 0.037
    )
    ticket_path.write_text(json.dumps(ticket, indent=2) + "\n")

    mutate_csv(
        target / "weather.csv",
        {
            "eto_mm": lambda value: value * 1.037 + 0.013,
            "effective_precipitation_mm": lambda value: value * 0.91,
        },
    )
    mutate_csv(
        target / "initial_depletion.csv",
        {"depletion_fraction": lambda value: min(0.95, value * 0.91 + 0.043)},
    )
    mutate_csv(
        target / "initial_salinity.csv",
        {"initial_ec_ds_m": lambda value: value * 1.11 + 0.09},
    )

    (destination / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "campaigns": [
                    {"campaign_id": campaign_id, "directory": str(target)}
                ],
            },
            indent=2,
        )
        + "\n"
    )
    return campaign_id


def validate_complete_output(output_root, input_root):
    """Apply the complete value-level contract to a solver run at arbitrary roots."""
    geojson_path = output_root / "prescription.geojson"
    csv_path = output_root / "units.csv"
    summary_path = output_root / "summary.json"
    assert regular_file(geojson_path)
    assert regular_file(csv_path)
    assert regular_file(summary_path)

    document = json.loads(geojson_path.read_text())
    with csv_path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        header = reader.fieldnames
        csv_rows = list(reader)
    summary_document = json.loads(summary_path.read_text())
    expected_units, expected_summaries = calculate(input_root)

    assert set(document) == {"type", "features"}
    assert document["type"] == "FeatureCollection"
    assert len(document["features"]) == len(expected_units)
    actual_rows = []
    for feature, expected in zip(document["features"], expected_units, strict=True):
        assert set(feature) == {"type", "geometry", "properties"}
        assert feature["type"] == "Feature"
        actual_geometry = shape(feature["geometry"])
        assert actual_geometry.is_valid and not actual_geometry.is_empty
        assert actual_geometry.symmetric_difference(expected["geometry"]).area <= 1e-6
        actual = feature["properties"]
        assert set(actual) == set(PROPERTY_FIELDS)
        for field in IDENTITY_FIELDS:
            assert actual[field] == expected[field]
        assert actual["zone_id"] == expected["zone_id"]
        assert actual["stress_days"] == expected["stress_days"]
        assert actual["salinity_stress_days"] == expected["salinity_stress_days"]
        for field, (absolute, relative) in UNIT_NUMERIC_TOLERANCES.items():
            close(actual[field], expected[field], absolute, relative)
        actual_rows.append(actual)
    assert_no_coherent_unit_bias(actual_rows, expected_units)

    assert header == PROPERTY_FIELDS
    assert len(csv_rows) == len(actual_rows)
    text_fields = {"campaign_id", "unit_id", "zone_id"}
    integer_fields = {"row", "column", "stress_days", "salinity_stress_days"}
    for csv_row, geo_row in zip(csv_rows, actual_rows, strict=True):
        for field in text_fields:
            assert csv_row[field] == geo_row[field]
        for field in integer_fields:
            assert INTEGER_TEXT.fullmatch(csv_row[field])
            assert int(csv_row[field]) == geo_row[field]
        for field in set(PROPERTY_FIELDS) - text_fields - integer_fields:
            assert DECIMAL_TEXT.fullmatch(csv_row[field])
            close(float(csv_row[field]), geo_row[field], 1e-10, 1e-10)

    assert set(summary_document) == {"schema_version", "campaigns"}
    assert summary_document["schema_version"] == 1
    actual_summaries = summary_document["campaigns"]
    assert len(actual_summaries) == len(expected_summaries)
    for actual, expected in zip(actual_summaries, expected_summaries, strict=True):
        assert set(actual) == set(SUMMARY_FIELDS)
        assert actual["campaign_id"] == expected["campaign_id"]
        assert actual["analysis_unit_count"] == expected["analysis_unit_count"]
        assert actual["quota_binding"] is expected["quota_binding"]
        for field, (absolute, relative) in SUMMARY_NUMERIC_TOLERANCES.items():
            close(actual[field], expected[field], absolute, relative)
        assert len(actual["zones"]) == len(expected["zones"])
        for actual_zone, expected_zone in zip(
            actual["zones"], expected["zones"], strict=True
        ):
            assert set(actual_zone) == set(ZONE_FIELDS)
            assert actual_zone["zone_id"] == expected_zone["zone_id"]
            assert actual_zone["unit_count"] == expected_zone["unit_count"]
            for field, (absolute, relative) in ZONE_NUMERIC_TOLERANCES.items():
                close(actual_zone[field], expected_zone[field], absolute, relative)


def feature_properties():
    return [feature["properties"] for feature in submitted_geojson()["features"]]


def test_requested_artifacts_are_regular_parseable_files():
    """The three data artifacts and reusable solver are regular files with valid formats."""
    assert regular_file(GEOJSON_PATH)
    assert regular_file(CSV_PATH)
    assert regular_file(SUMMARY_PATH)
    assert regular_file(SOLVER_PATH)
    assert isinstance(submitted_geojson(), dict)
    header, rows = submitted_csv()
    assert header is not None and isinstance(rows, list)
    assert isinstance(submitted_summary(), dict)


def test_published_world_inputs_are_unchanged():
    """Every agent-visible campaign and specification file retains its build-time path and SHA-256."""
    expected = json.loads((TESTS / "input-manifest.json").read_text())
    found = {}
    for path in sorted(INPUT.rglob("*")):
        if path.is_dir():
            continue
        assert not path.is_symlink(), path
        assert stat.S_ISREG(path.stat().st_mode), path
        found[path.relative_to(INPUT).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    assert found == expected


def test_generated_campaigns_exercise_heterogeneous_model_paths():
    """The published world retains the structural variants that make simplifying assumptions fail."""
    manifest = json.loads((INPUT / "manifest.json").read_text())
    grid_sizes = set()
    zone_counts = set()
    field_types = set()
    event_types = set()
    observation_schedules = set()
    rotated_soil_edges = 0
    for campaign in manifest["campaigns"]:
        directory = INPUT / "campaigns" / campaign["campaign_id"]
        ticket = json.loads((directory / "job_ticket.json").read_text())
        grid_sizes.add(
            (float(ticket["grid"]["cell_width_m"]), float(ticket["grid"]["cell_height_m"]))
        )
        zone_counts.add(len(ticket["management_zone_edges_mm"]))
        curve = ticket["crop"]["root_depth_curve"]
        curve_dates = [date.fromisoformat(item["date"]) for item in curve]
        curve_depths = [float(item["root_depth_m"]) for item in curve]
        assert len(curve) >= 4
        assert curve_dates == sorted(set(curve_dates))
        assert curve_depths == sorted(curve_depths) and len(set(curve_depths)) == len(curve_depths)

        boundary = json.loads((directory / "field_boundary.geojson").read_text())
        field_types.add(boundary["geometry"]["type"])
        events = json.loads((directory / "irrigation_events.geojson").read_text())
        event_types.update(feature["geometry"]["type"] for feature in events["features"])
        soil = json.loads((directory / "soil_map_units.geojson").read_text())
        for feature in soil["features"]:
            ring = feature["geometry"]["coordinates"][0]
            for first, second in zip(ring, ring[1:]):
                if first[0] != second[0] and first[1] != second[1]:
                    rotated_soil_edges += 1
                    break

        by_unit = defaultdict(list)
        with (directory / "vegetation_index.csv").open(newline="") as handle:
            for row in csv.DictReader(handle):
                by_unit[row["unit_id"]].append(row["date"])
        observation_schedules.update(tuple(values) for values in by_unit.values())

    assert len(grid_sizes) == len(manifest["campaigns"])
    assert len(zone_counts) >= 3
    assert {"Polygon", "MultiPolygon"}.issubset(field_types)
    assert "MultiPolygon" in event_types
    assert len(observation_schedules) >= 20
    assert rotated_soil_edges >= len(manifest["campaigns"])


def test_generated_campaigns_exercise_deep_drainage_balance():
    """Storm regimes keep deep drainage live, spatially variable, and campaign-general."""
    records, _ = reference()
    by_campaign = defaultdict(list)
    for record in records:
        by_campaign[record["campaign_id"]].append(float(record["seasonal_drainage_mm"]))

    assert len(by_campaign) >= 4
    for campaign_id, values in by_campaign.items():
        positive = [value for value in values if value > 0.01]
        assert positive, campaign_id
        assert max(positive) >= 5.0, campaign_id
        assert len({round(value, 3) for value in positive}) >= 8, campaign_id
    all_values = [value for values in by_campaign.values() for value in values]
    assert sum(value > 0.01 for value in all_values) >= len(all_values) // 3


def test_generated_campaigns_exercise_coupled_salinity_paths():
    """Every campaign activates salt stress, salt leaching, and heterogeneous terminal salinity."""
    manifest = json.loads((INPUT / "manifest.json").read_text())
    records, _ = reference()
    by_campaign = defaultdict(list)
    for record in records:
        by_campaign[record["campaign_id"]].append(record)

    assert len(manifest["campaigns"]) >= 6
    for campaign in manifest["campaigns"]:
        campaign_id = campaign["campaign_id"]
        directory = INPUT / "campaigns" / campaign_id
        ticket = json.loads((directory / "job_ticket.json").read_text())
        assert set(ticket["salinity"]) == {
            "rainfall_ec_ds_m",
            "crop_threshold_ec_ds_m",
            "yield_slope_per_ds_m",
            "minimum_stress_coefficient",
            "leaching_efficiency",
            "new_root_zone_ec_ds_m",
            "minimum_solution_depth_mm",
            "leaching_requirement_mm_per_ds_m",
        }
        with (directory / "initial_salinity.csv").open(newline="") as handle:
            reader = csv.DictReader(handle)
            assert reader.fieldnames == ["unit_id", "initial_ec_ds_m"]
            salinity_rows = list(reader)
        assert salinity_rows
        assert set(salinity_rows[0]) == {"unit_id", "initial_ec_ds_m"}
        events = json.loads((directory / "irrigation_events.geojson").read_text())
        assert all(
            set(feature["properties"])
            == {"event_id", "date", "gross_depth_mm", "water_ec_ds_m"}
            for feature in events["features"]
        )
        rows = by_campaign[campaign_id]
        assert any(row["salinity_stress_days"] > 0 for row in rows), campaign_id
        assert any(row["seasonal_leached_salt_index"] > 0.01 for row in rows), campaign_id
        assert len({round(row["final_ec_ds_m"], 3) for row in rows}) >= 20, campaign_id


def test_campaign_pump_quota_is_binding_and_allocation_is_nonlocal():
    """Each campaign requires a genuine joint allocation rather than per-cell clipping."""
    records, summaries = reference()
    by_campaign = defaultdict(list)
    for record in records:
        by_campaign[record["campaign_id"]].append(record)
    for summary in summaries:
        rows = by_campaign[summary["campaign_id"]]
        assert summary["quota_binding"] is True, summary["campaign_id"]
        assert summary["requested_gross_volume_m3"] > summary["pump_budget_m3"]
        assert abs(summary["total_gross_volume_m3"] - summary["pump_budget_m3"]) <= 1e-8
        assert any(row["allocation_shortfall_mm"] > 0.01 for row in rows)
        assert len({round(row["allocation_priority"], 4) for row in rows}) >= 20


def test_geojson_and_csv_follow_the_normative_schemas():
    """GeoJSON features and CSV rows use the exact published keys, types, header, and finite values."""
    document = submitted_geojson()
    assert set(document) == {"type", "features"}
    assert document["type"] == "FeatureCollection"
    assert isinstance(document["features"], list)
    for feature in document["features"]:
        assert set(feature) == {"type", "geometry", "properties"}
        assert feature["type"] == "Feature"
        assert isinstance(feature["geometry"], dict)
        properties = feature["properties"]
        assert set(properties) == set(PROPERTY_FIELDS)
        assert isinstance(properties["campaign_id"], str)
        assert isinstance(properties["unit_id"], str)
        assert isinstance(properties["zone_id"], str)
        assert type(properties["row"]) is int
        assert type(properties["column"]) is int
        assert type(properties["stress_days"]) is int
        assert type(properties["salinity_stress_days"]) is int
        for key in set(PROPERTY_FIELDS) - {"campaign_id", "unit_id", "zone_id", "row", "column", "stress_days", "salinity_stress_days"}:
            assert finite_number(properties[key]), (properties["campaign_id"], properties["unit_id"], key)
    header, csv_rows = submitted_csv()
    assert header == PROPERTY_FIELDS
    for row in csv_rows:
        assert set(row) == set(PROPERTY_FIELDS)
        for key in ("row", "column", "stress_days", "salinity_stress_days"):
            assert INTEGER_TEXT.fullmatch(row[key]), (row["campaign_id"], row["unit_id"], key)
        for key in set(PROPERTY_FIELDS) - {"campaign_id", "unit_id", "zone_id", "row", "column", "stress_days", "salinity_stress_days"}:
            assert DECIMAL_TEXT.fullmatch(row[key]), (row["campaign_id"], row["unit_id"], key)
            assert math.isfinite(float(row[key]))


def test_summary_follows_the_normative_schema():
    """The field summary has exactly the documented campaign and campaign-specific zone structures."""
    document = submitted_summary()
    _, expected_summaries = reference()
    assert set(document) == {"schema_version", "campaigns"}
    assert document["schema_version"] == 1
    assert isinstance(document["campaigns"], list)
    assert len(document["campaigns"]) == len(expected_summaries)
    for campaign, expected in zip(document["campaigns"], expected_summaries, strict=True):
        assert set(campaign) == set(SUMMARY_FIELDS)
        assert isinstance(campaign["campaign_id"], str)
        assert type(campaign["analysis_unit_count"]) is int
        assert type(campaign["quota_binding"]) is bool
        for key in ("field_area_m2", "irrigated_area_m2", "area_weighted_mean_depth_mm", "total_gross_volume_m3", "requested_gross_volume_m3", "pump_budget_m3", "allocation_shortfall_volume_m3"):
            assert finite_number(campaign[key])
        assert isinstance(campaign["zones"], list)
        assert [zone["zone_id"] for zone in campaign["zones"]] == [
            zone["zone_id"] for zone in expected["zones"]
        ]
        for zone in campaign["zones"]:
            assert set(zone) == set(ZONE_FIELDS)
            assert isinstance(zone["zone_id"], str)
            assert type(zone["unit_count"]) is int
            for key in ("area_m2", "area_fraction", "mean_depth_mm"):
                assert finite_number(zone[key])


def test_analysis_unit_identity_order_and_coverage_are_exact():
    """The submission includes every prescribed positive-area grid cell once in canonical order."""
    expected_units, _ = reference()
    actual = feature_properties()
    expected_identity = [tuple(row[key] for key in IDENTITY_FIELDS) for row in expected_units]
    actual_identity = [tuple(row[key] for key in IDENTITY_FIELDS) for row in actual]
    assert actual_identity == expected_identity
    assert len(actual_identity) == len(set(actual_identity))
    header, csv_rows = submitted_csv()
    csv_identity = [
        (row["campaign_id"], row["unit_id"], int(row["row"]), int(row["column"]))
        for row in csv_rows
    ]
    assert csv_identity == expected_identity


def test_clipped_geometry_area_and_fraction_match_the_prescribed_overlay():
    """Every output geometry is the true field/cell intersection with its exact area weighting."""
    expected_units, _ = reference()
    features = submitted_geojson()["features"]
    for feature, expected in zip(features, expected_units, strict=True):
        actual_geometry = shape(feature["geometry"])
        assert actual_geometry.is_valid
        assert not actual_geometry.is_empty
        assert actual_geometry.symmetric_difference(expected["geometry"]).area <= 1e-6, (
            expected["campaign_id"],
            expected["unit_id"],
        )
        properties = feature["properties"]
        assert abs(properties["clipped_area_m2"] - expected["clipped_area_m2"]) <= 1e-6
        assert abs(properties["area_fraction"] - expected["area_fraction"]) <= 1e-6


def test_soil_horizon_integration_and_area_weighting_are_correct():
    """TAW and RAW reproduce depth integration and multi-map-unit intersection weighting."""
    expected_units, _ = reference()
    for actual, expected in zip(feature_properties(), expected_units, strict=True):
        for field in SOIL_FIELDS:
            close(actual[field], expected[field], 0.01)


def test_daily_water_balance_and_audit_state_are_correct():
    """Water, salt, stress, and seasonal audits match the coupled daily simulation."""
    expected_units, _ = reference()
    for actual, expected in zip(feature_properties(), expected_units, strict=True):
        for field in DYNAMIC_STATE_DEPTH_FIELDS:
            close(actual[field], expected[field], 0.01)
        for field in ACCUMULATED_DEPTH_FIELDS:
            close(actual[field], expected[field], 0.01, relative=0.0)
        for field in COEFFICIENT_FIELDS:
            close(actual[field], expected[field], 0.0001)
        assert actual["stress_days"] == expected["stress_days"]
        assert actual["salinity_stress_days"] == expected["salinity_stress_days"]


def test_physical_fields_have_no_coherent_in_band_bias():
    """Campaign/field residual distributions reject systematic bias hidden by pointwise bands."""
    expected_units, _ = reference()
    assert_no_coherent_unit_bias(feature_properties(), expected_units)


def test_residual_guard_rejects_pointwise_valid_additive_and_multiplicative_bias():
    """Regression probes prove that coherent offsets cannot consume the pointwise tolerance."""
    depth_expected = [5.0 + index / 10.0 for index in range(200)]
    depth_actual = [value + 0.009 for value in depth_expected]
    assert all(abs(actual - expected) <= 0.01 for actual, expected in zip(depth_actual, depth_expected))
    with pytest.raises(AssertionError):
        assert_residual_distribution(depth_actual, depth_expected, 0.01, 0.0, "additive")

    coefficient_expected = [0.5 + index / 500.0 for index in range(200)]
    coefficient_actual = [value * 1.0009 for value in coefficient_expected]
    assert all(
        abs(actual - expected) <= max(0.0001, 0.001 * abs(expected))
        for actual, expected in zip(coefficient_actual, coefficient_expected, strict=True)
    )
    with pytest.raises(AssertionError):
        assert_residual_distribution(
            coefficient_actual,
            coefficient_expected,
            0.0001,
            0.001,
            "multiplicative",
        )


def test_prescription_depths_and_management_zones_are_correct():
    """Jointly allocated terminal recommendations and half-open zone assignments are correct."""
    expected_units, _ = reference()
    for actual, expected in zip(feature_properties(), expected_units, strict=True):
        close(actual["recommended_gross_mm"], expected["recommended_gross_mm"], 0.01)
        assert actual["zone_id"] == expected["zone_id"]


def test_csv_records_correspond_to_the_geojson_properties():
    """The tabular artifact contains the same canonical per-unit simulation records as GeoJSON."""
    _, rows = submitted_csv()
    properties = feature_properties()
    assert len(rows) == len(properties)
    text_fields = {"campaign_id", "unit_id", "zone_id"}
    integer_fields = {"row", "column", "stress_days", "salinity_stress_days"}
    for csv_row, geo_row in zip(rows, properties, strict=True):
        for field in text_fields:
            assert csv_row[field] == geo_row[field]
        for field in integer_fields:
            assert int(csv_row[field]) == geo_row[field]
        for field in set(PROPERTY_FIELDS) - text_fields - integer_fields:
            close(float(csv_row[field]), geo_row[field], 1e-10, relative=1e-10)


def test_field_and_zone_summaries_match_independent_aggregation():
    """Field totals, volumes, area-weighted depths, and all zone reconciliations are correct."""
    _, expected_summaries = reference()
    actual_summaries = submitted_summary()["campaigns"]
    assert [row["campaign_id"] for row in actual_summaries] == [row["campaign_id"] for row in expected_summaries]
    for actual, expected in zip(actual_summaries, expected_summaries, strict=True):
        assert actual["analysis_unit_count"] == expected["analysis_unit_count"]
        assert abs(actual["field_area_m2"] - expected["field_area_m2"]) <= 1e-6
        assert abs(actual["irrigated_area_m2"] - expected["irrigated_area_m2"]) <= 1e-6
        close(actual["area_weighted_mean_depth_mm"], expected["area_weighted_mean_depth_mm"], 0.01)
        close(actual["total_gross_volume_m3"], expected["total_gross_volume_m3"], 0.01)
        close(actual["requested_gross_volume_m3"], expected["requested_gross_volume_m3"], 0.01)
        close(actual["pump_budget_m3"], expected["pump_budget_m3"], 0.01)
        close(actual["allocation_shortfall_volume_m3"], expected["allocation_shortfall_volume_m3"], 0.01)
        assert actual["quota_binding"] is expected["quota_binding"]
        assert [zone["zone_id"] for zone in actual["zones"]] == [
            zone["zone_id"] for zone in expected["zones"]
        ]
        for actual_zone, expected_zone in zip(actual["zones"], expected["zones"], strict=True):
            assert actual_zone["unit_count"] == expected_zone["unit_count"]
            assert abs(actual_zone["area_m2"] - expected_zone["area_m2"]) <= 1e-6
            assert abs(actual_zone["area_fraction"] - expected_zone["area_fraction"]) <= 1e-6
            close(actual_zone["mean_depth_mm"], expected_zone["mean_depth_mm"], 0.01)


def test_reusable_solver_generalizes_to_a_held_out_counterfactual(tmp_path):
    """Re-run submitted code on changed state drivers and budget; fixed-world answers must fail."""
    challenge_input = tmp_path / "counterfactual-input"
    challenge_output = tmp_path / "counterfactual-output"
    campaign_id = build_counterfactual_input(challenge_input)
    counterfactual_units, counterfactual_summaries = calculate(challenge_input)
    published_units, published_summaries = reference()
    published_campaign_units = [
        row for row in published_units if row["campaign_id"] == campaign_id
    ]
    published_campaign_summary = next(
        row for row in published_summaries if row["campaign_id"] == campaign_id
    )
    assert [row["unit_id"] for row in counterfactual_units] == [
        row["unit_id"] for row in published_campaign_units
    ]
    material_changes = 0
    for actual, published in zip(
        counterfactual_units, published_campaign_units, strict=True
    ):
        for field in BIAS_GUARDED_FIELDS:
            absolute, relative = UNIT_NUMERIC_TOLERANCES[field]
            if abs(float(actual[field]) - float(published[field])) > 5 * max(
                absolute, relative * abs(float(published[field]))
            ):
                material_changes += 1
    assert material_changes >= 3 * len(counterfactual_units)
    assert counterfactual_summaries[0]["pump_budget_m3"] != published_campaign_summary[
        "pump_budget_m3"
    ]
    environment = os.environ.copy()
    environment.pop("IRRIGATION_APP_ROOT", None)
    environment.pop("PYTHONPATH", None)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [
            sys.executable,
            str(SOLVER_PATH),
            "--input-root",
            str(challenge_input),
            "--output-root",
            str(challenge_output),
        ],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        timeout=180,
        check=False,
    )
    assert completed.returncode == 0, (
        campaign_id,
        completed.stdout[-2000:],
        completed.stderr[-2000:],
    )
    validate_complete_output(challenge_output, challenge_input)
