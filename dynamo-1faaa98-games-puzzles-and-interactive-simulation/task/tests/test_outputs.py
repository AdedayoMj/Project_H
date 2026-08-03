from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import stat
from collections import defaultdict
from datetime import date
from functools import lru_cache
from pathlib import Path

from shapely.geometry import shape

from reference_model import calculate


APP = Path(os.environ.get("IRRIGATION_APP_ROOT", "/app"))
INPUT = APP / "input"
OUTPUT = APP / "output"
TESTS = Path(__file__).parent
GEOJSON_PATH = OUTPUT / "prescription.geojson"
CSV_PATH = OUTPUT / "units.csv"
SUMMARY_PATH = OUTPUT / "summary.json"

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
    "recommended_gross_mm",
    "seasonal_etc_mm",
    "seasonal_effective_irrigation_mm",
    "seasonal_drainage_mm",
    "stress_days",
    "minimum_ks",
]
IDENTITY_FIELDS = ["campaign_id", "unit_id", "row", "column"]
SOIL_FIELDS = ["taw_mm", "raw_mm"]
DYNAMIC_DEPTH_FIELDS = [
    "final_dr_mm",
    "seasonal_etc_mm",
    "seasonal_effective_irrigation_mm",
    "seasonal_drainage_mm",
]
COEFFICIENT_FIELDS = ["final_kc", "minimum_ks"]
SUMMARY_FIELDS = [
    "campaign_id",
    "analysis_unit_count",
    "field_area_m2",
    "irrigated_area_m2",
    "area_weighted_mean_depth_mm",
    "total_gross_volume_m3",
    "zones",
]
ZONE_FIELDS = ["zone_id", "unit_count", "area_m2", "area_fraction", "mean_depth_mm"]


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


def feature_properties():
    return [feature["properties"] for feature in submitted_geojson()["features"]]


def test_requested_artifacts_are_regular_parseable_files():
    """All three requested artifacts exist as regular files and parse in their documented formats."""
    assert regular_file(GEOJSON_PATH)
    assert regular_file(CSV_PATH)
    assert regular_file(SUMMARY_PATH)
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
        for key in set(PROPERTY_FIELDS) - {"campaign_id", "unit_id", "zone_id", "row", "column", "stress_days"}:
            assert finite_number(properties[key]), (properties["campaign_id"], properties["unit_id"], key)
    header, csv_rows = submitted_csv()
    assert header == PROPERTY_FIELDS
    for row in csv_rows:
        assert set(row) == set(PROPERTY_FIELDS)
        int(row["row"])
        int(row["column"])
        int(row["stress_days"])
        for key in set(PROPERTY_FIELDS) - {"campaign_id", "unit_id", "zone_id", "row", "column", "stress_days"}:
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
        for key in ("field_area_m2", "irrigated_area_m2", "area_weighted_mean_depth_mm", "total_gross_volume_m3"):
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
    """Final state and seasonal audits match the single-Kc stress-feedback simulation."""
    expected_units, _ = reference()
    for actual, expected in zip(feature_properties(), expected_units, strict=True):
        for field in DYNAMIC_DEPTH_FIELDS:
            close(actual[field], expected[field], 0.01)
        for field in COEFFICIENT_FIELDS:
            close(actual[field], expected[field], 0.0001)
        assert actual["stress_days"] == expected["stress_days"]


def test_prescription_depths_and_management_zones_are_correct():
    """Capacity-limited terminal recommendations and half-open zone assignments are correct."""
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
    integer_fields = {"row", "column", "stress_days"}
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
        assert [zone["zone_id"] for zone in actual["zones"]] == [
            zone["zone_id"] for zone in expected["zones"]
        ]
        for actual_zone, expected_zone in zip(actual["zones"], expected["zones"], strict=True):
            assert actual_zone["unit_count"] == expected_zone["unit_count"]
            assert abs(actual_zone["area_m2"] - expected_zone["area_m2"]) <= 1e-6
            assert abs(actual_zone["area_fraction"] - expected_zone["area_fraction"]) <= 1e-6
            close(actual_zone["mean_depth_mm"], expected_zone["mean_depth_mm"], 0.01)
