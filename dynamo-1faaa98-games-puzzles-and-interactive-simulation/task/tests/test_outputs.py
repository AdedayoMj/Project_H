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
from functools import lru_cache
from pathlib import Path

import pytest
from shapely import affinity
from shapely.geometry import mapping, shape

from reference_model import SCENARIO_IDS, calculate


APP = Path(os.environ.get("IRRIGATION_APP_ROOT", "/app"))
INPUT = APP / "input"
OUTPUT = APP / "output"
TESTS = Path(__file__).parent
GEOJSON_PATH = OUTPUT / "allocation-frontier.geojson"
CSV_PATH = OUTPUT / "allocation-frontier.csv"
CERTIFICATE_PATH = OUTPUT / "optimality-certificate.json"
SOLVER_PATH = OUTPUT / "solver.py"
RESTRICTED_RUNNER = TESTS / "restricted_solver_runner.py"

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
PROPERTY_FIELDS = BASE_FIELDS + [
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
IDENTITY_FIELDS = ("campaign_id", "unit_id", "row", "column")
TEXT_FIELDS = {
    "campaign_id",
    "unit_id",
    "activation_scenario",
    "satisfaction_scenario",
}
INTEGER_FIELDS = {"row", "column", "stress_days", "salinity_stress_days"}
SCENARIO_FIELDS = {
    "scenario_id",
    "budget_m3",
    "allocated_volume_m3",
    "shortfall_volume_m3",
    "binding",
    "depth_shadow_price",
    "weighted_shortfall_cost",
    "active_unit_count",
    "satisfied_unit_count",
    "mean_service_ratio",
    "transition_volume_m3",
}
CAMPAIGN_FIELDS = {
    "campaign_id",
    "analysis_unit_count",
    "field_area_m2",
    "requested_volume_m3",
    "satisfaction_ratio",
    "scenarios",
}
INTEGER_TEXT = re.compile(r"[+-]?[0-9]+\Z")
DECIMAL_TEXT = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)\Z")

UNIT_TOLERANCES = {
    "clipped_area_m2": (1e-6, 0.0),
    "area_fraction": (1e-6, 0.0),
    "taw_mm": (0.01, 0.001),
    "raw_mm": (0.01, 0.001),
    "final_dr_mm": (0.01, 0.001),
    "final_kc": (0.0001, 0.001),
    "final_ec_ds_m": (0.0001, 0.001),
    "minimum_k_sal": (0.0001, 0.001),
    "request_mm": (0.01, 0.001),
    "water_need_index": (0.0001, 0.001),
    "salt_need_index": (0.0001, 0.001),
    "history_need_index": (0.0001, 0.001),
    "seasonal_etc_mm": (0.01, 0.0),
    "seasonal_effective_irrigation_mm": (0.01, 0.0),
    "seasonal_drainage_mm": (0.01, 0.0),
    "seasonal_leached_salt_index": (0.01, 0.0),
    "minimum_ks": (0.0001, 0.001),
    "robustness_score": (0.0001, 0.001),
    "frontier_gain_mm": (0.01, 0.001),
}
for _scenario_id in SCENARIO_IDS:
    UNIT_TOLERANCES[f"{_scenario_id}_priority"] = (0.0001, 0.001)
    UNIT_TOLERANCES[f"{_scenario_id}_depth_mm"] = (0.01, 0.001)
    UNIT_TOLERANCES[f"{_scenario_id}_shortfall_mm"] = (0.01, 0.001)

CERTIFICATE_TOLERANCES = {
    "field_area_m2": (1e-6, 0.0),
    "requested_volume_m3": (0.01, 0.001),
    "satisfaction_ratio": (0.0001, 0.001),
}
SCENARIO_TOLERANCES = {
    "budget_m3": (0.01, 0.001),
    "allocated_volume_m3": (0.01, 0.001),
    "shortfall_volume_m3": (0.01, 0.001),
    "depth_shadow_price": (0.0001, 0.001),
    "weighted_shortfall_cost": (0.01, 0.001),
    "mean_service_ratio": (0.0001, 0.001),
    "transition_volume_m3": (0.01, 0.001),
}
BIAS_FIELDS = tuple(
    key for key in UNIT_TOLERANCES if key not in {"clipped_area_m2", "area_fraction"}
)
MAX_MEAN_BIAS = 0.05
MAX_RMS = 0.25


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
        return reader.fieldnames, list(reader)


@lru_cache(maxsize=1)
def submitted_certificate():
    return json.loads(CERTIFICATE_PATH.read_text())


def regular_file(path):
    return path.exists() and not path.is_symlink() and stat.S_ISREG(path.stat().st_mode)


def finite_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def close(actual, expected, absolute, relative=0.001):
    assert finite_number(actual)
    assert abs(float(actual) - float(expected)) <= max(
        absolute, relative * abs(float(expected))
    )


def residual(actual, expected, absolute, relative):
    return (float(actual) - float(expected)) / max(
        absolute, relative * abs(float(expected))
    )


def assert_residual_distribution(actual, expected, absolute, relative, label):
    values = [
        residual(a, e, absolute, relative)
        for a, e in zip(actual, expected, strict=True)
    ]
    assert values, label
    mean = math.fsum(values) / len(values)
    rms = math.sqrt(math.fsum(value * value for value in values) / len(values))
    assert abs(mean) <= MAX_MEAN_BIAS, (label, "mean", mean)
    assert rms <= MAX_RMS, (label, "rms", rms)


def assert_no_coherent_bias(actual_rows, expected_rows):
    for campaign_id in sorted({row["campaign_id"] for row in expected_rows}):
        actual_campaign = [row for row in actual_rows if row["campaign_id"] == campaign_id]
        expected_campaign = [row for row in expected_rows if row["campaign_id"] == campaign_id]
        assert len(actual_campaign) == len(expected_campaign)
        for field in BIAS_FIELDS:
            absolute, relative = UNIT_TOLERANCES[field]
            assert_residual_distribution(
                [row[field] for row in actual_campaign],
                [row[field] for row in expected_campaign],
                absolute,
                relative,
                f"{campaign_id}:{field}",
            )


def feature_rows():
    return [feature["properties"] for feature in submitted_geojson()["features"]]


def decimal_text(value):
    text = format(float(value), ".12f").rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def mutate_csv(path, transforms):
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames
        rows = list(reader)
    assert fields is not None
    for row in rows:
        for field, transform in transforms.items():
            row[field] = decimal_text(transform(float(row[field])))
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def transform_geojson(path, x_factor, y_factor, x_offset, y_offset):
    """Apply the same invertible affine transform to every GeoJSON geometry."""
    document = json.loads(path.read_text())
    features = (
        document["features"]
        if document["type"] == "FeatureCollection"
        else [document]
    )
    for feature in features:
        geometry = affinity.scale(
            shape(feature["geometry"]),
            xfact=x_factor,
            yfact=y_factor,
            origin=(0.0, 0.0),
        )
        geometry = affinity.translate(geometry, xoff=x_offset, yoff=y_offset)
        feature["geometry"] = mapping(geometry)
    path.write_text(json.dumps(document, separators=(",", ":")) + "\n")


def build_counterfactual_input(destination):
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

    x_factor, y_factor = 1.071, 0.943
    x_offset, y_offset = 137.25, -83.75
    for filename in (
        "field_boundary.geojson",
        "soil_map_units.geojson",
        "irrigation_events.geojson",
    ):
        transform_geojson(
            target / filename,
            x_factor,
            y_factor,
            x_offset,
            y_offset,
        )

    ticket_path = target / "job_ticket.json"
    ticket = json.loads(ticket_path.read_text())
    grid = ticket["grid"]
    grid["origin_x"] = x_factor * float(grid["origin_x"]) + x_offset
    grid["origin_y"] = y_factor * float(grid["origin_y"]) + y_offset
    grid["cell_width_m"] = x_factor * float(grid["cell_width_m"])
    grid["cell_height_m"] = y_factor * float(grid["cell_height_m"])
    ticket["crop"]["kc_intercept"] = float(ticket["crop"]["kc_intercept"]) + 0.017
    ticket["pump"]["volume_budget_m3"] *= 0.82
    ticket["salinity"]["rainfall_ec_ds_m"] += 0.037
    ticket["response_frontier"]["satisfaction_ratio"] = 0.84
    for index, scenario in enumerate(ticket["response_frontier"]["scenarios"]):
        if scenario["scenario_id"] != "nominal":
            scenario["nominal_budget_fraction"] *= 0.91
        scenario["water_weight_multiplier"] *= 1.0 + 0.03 * index
        scenario["salinity_weight_multiplier"] *= 1.06 - 0.01 * index
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


def run_restricted_solver(solver_path, input_root, output_root, cwd, timeout=180):
    """Execute a solver in isolated mode with verifier access denied by an audit hook."""
    environment = os.environ.copy()
    environment.pop("IRRIGATION_APP_ROOT", None)
    environment.pop("PYTHONPATH", None)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [
            sys.executable,
            "-I",
            str(RESTRICTED_RUNNER),
            str(solver_path),
            "--input-root",
            str(input_root),
            "--output-root",
            str(output_root),
        ],
        cwd=cwd,
        env=environment,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def validate_complete_output(output_root, input_root):
    geojson_path = output_root / "allocation-frontier.geojson"
    csv_path = output_root / "allocation-frontier.csv"
    certificate_path = output_root / "optimality-certificate.json"
    assert all(regular_file(path) for path in (geojson_path, csv_path, certificate_path))
    document = json.loads(geojson_path.read_text())
    with csv_path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        header, csv_rows = reader.fieldnames, list(reader)
    certificate = json.loads(certificate_path.read_text())
    expected_rows, expected_certificates = calculate(input_root)

    assert set(document) == {"type", "features"}
    assert document["type"] == "FeatureCollection"
    assert len(document["features"]) == len(expected_rows)
    actual_rows = []
    for feature, expected in zip(document["features"], expected_rows, strict=True):
        assert set(feature) == {"type", "geometry", "properties"}
        assert feature["type"] == "Feature"
        geometry = shape(feature["geometry"])
        assert geometry.is_valid and not geometry.is_empty
        assert geometry.symmetric_difference(expected["geometry"]).area <= 1e-6
        actual = feature["properties"]
        assert set(actual) == set(PROPERTY_FIELDS)
        for field in IDENTITY_FIELDS:
            assert actual[field] == expected[field]
        for field in ("stress_days", "salinity_stress_days"):
            assert actual[field] == expected[field]
        for field in ("activation_scenario", "satisfaction_scenario"):
            assert actual[field] == expected[field]
        for field, (absolute, relative) in UNIT_TOLERANCES.items():
            close(actual[field], expected[field], absolute, relative)
        actual_rows.append(actual)
    assert_no_coherent_bias(actual_rows, expected_rows)

    assert header == PROPERTY_FIELDS
    assert len(csv_rows) == len(actual_rows)
    for csv_row, geo_row in zip(csv_rows, actual_rows, strict=True):
        for field in TEXT_FIELDS:
            assert csv_row[field] == geo_row[field]
        for field in INTEGER_FIELDS:
            assert INTEGER_TEXT.fullmatch(csv_row[field])
            assert int(csv_row[field]) == geo_row[field]
        for field in set(PROPERTY_FIELDS) - TEXT_FIELDS - INTEGER_FIELDS:
            assert DECIMAL_TEXT.fullmatch(csv_row[field])
            close(float(csv_row[field]), geo_row[field], 1e-10, 1e-10)

    assert set(certificate) == {"schema_version", "campaigns"}
    assert certificate["schema_version"] == 2
    assert len(certificate["campaigns"]) == len(expected_certificates)
    for actual, expected in zip(
        certificate["campaigns"], expected_certificates, strict=True
    ):
        assert set(actual) == CAMPAIGN_FIELDS
        assert actual["campaign_id"] == expected["campaign_id"]
        assert actual["analysis_unit_count"] == expected["analysis_unit_count"]
        for field, tolerance in CERTIFICATE_TOLERANCES.items():
            close(actual[field], expected[field], *tolerance)
        assert [point["scenario_id"] for point in actual["scenarios"]] == list(
            SCENARIO_IDS
        )
        for actual_point, expected_point in zip(
            actual["scenarios"], expected["scenarios"], strict=True
        ):
            assert set(actual_point) == SCENARIO_FIELDS
            assert actual_point["scenario_id"] == expected_point["scenario_id"]
            assert actual_point["binding"] is expected_point["binding"]
            assert actual_point["active_unit_count"] == expected_point["active_unit_count"]
            assert actual_point["satisfied_unit_count"] == expected_point["satisfied_unit_count"]
            for field, tolerance in SCENARIO_TOLERANCES.items():
                close(actual_point[field], expected_point[field], *tolerance)


def test_requested_frontier_artifacts_are_regular_and_parseable():
    """The frontier map, table, certificate, and reusable solver are real files."""
    for path in (GEOJSON_PATH, CSV_PATH, CERTIFICATE_PATH, SOLVER_PATH):
        assert regular_file(path)
    assert isinstance(submitted_geojson(), dict)
    assert submitted_csv()[0] == PROPERTY_FIELDS
    assert isinstance(submitted_certificate(), dict)


def test_generated_evidence_is_hash_locked_and_builder_is_absent():
    """The solver cannot alter evidence or replay the removed fixture generator."""
    expected = json.loads((TESTS / "input-manifest.json").read_text())
    found = {}
    for path in sorted(INPUT.rglob("*")):
        if path.is_dir():
            continue
        assert not path.is_symlink() and stat.S_ISREG(path.stat().st_mode)
        found[path.relative_to(INPUT).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    assert found == expected
    assert not Path("/tmp/irrigation-task/build_instance.py").exists()


def test_ticket_frontiers_are_ordered_and_campaign_specific():
    """Every campaign publishes four ordered scarcity policies plus implicit recovery."""
    manifest = json.loads((INPUT / "manifest.json").read_text())
    factor_vectors = set()
    for campaign in manifest["campaigns"]:
        ticket = json.loads(
            (INPUT / "campaigns" / campaign["campaign_id"] / "job_ticket.json").read_text()
        )
        assert set(ticket) == {
            "campaign_id", "crs", "grid", "simulation", "crop", "irrigation",
            "salinity", "pump", "response_frontier",
        }
        frontier = ticket["response_frontier"]
        assert set(frontier) == {"satisfaction_ratio", "scenarios"}
        assert 0.5 < frontier["satisfaction_ratio"] < 1.0
        assert [row["scenario_id"] for row in frontier["scenarios"]] == list(
            SCENARIO_IDS[:-1]
        )
        factors = tuple(row["nominal_budget_fraction"] for row in frontier["scenarios"])
        assert list(factors) == sorted(set(factors)) and factors[-1] == 1.0
        factor_vectors.add(factors)
        for row in frontier["scenarios"]:
            assert set(row) == {
                "scenario_id", "nominal_budget_fraction", "water_weight_multiplier",
                "salinity_weight_multiplier", "history_weight_multiplier",
            }
    assert len(factor_vectors) >= 3


def test_reference_world_activates_all_frontier_cruxes():
    """Scarcity points bind, transition volumes are positive, and recovery is complete."""
    rows, certificates = reference()
    by_campaign = defaultdict(list)
    for row in rows:
        by_campaign[row["campaign_id"]].append(row)
    assert len(certificates) >= 6
    for certificate in certificates:
        points = certificate["scenarios"]
        budgets = [point["budget_m3"] for point in points]
        volumes = [point["allocated_volume_m3"] for point in points]
        assert budgets == sorted(budgets)
        assert volumes == sorted(volumes)
        assert all(point["binding"] for point in points[:-1])
        assert points[-1]["binding"] is False
        assert abs(points[-1]["allocated_volume_m3"] - certificate["requested_volume_m3"]) < 1e-8
        assert all(point["transition_volume_m3"] > 0 for point in points)
        campaign_rows = by_campaign[certificate["campaign_id"]]
        assert len({row["activation_scenario"] for row in campaign_rows}) >= 2
        assert len({round(row["critical_priority"], 4) for row in campaign_rows}) >= 20
        assert any(row["frontier_gain_mm"] > 1.0 for row in campaign_rows)


def test_geojson_and_csv_use_the_exact_frontier_schema():
    """No old one-prescription fields or undeclared keys can enter either artifact."""
    document = submitted_geojson()
    assert set(document) == {"type", "features"}
    assert document["type"] == "FeatureCollection"
    for feature in document["features"]:
        assert set(feature) == {"type", "geometry", "properties"}
        assert set(feature["properties"]) == set(PROPERTY_FIELDS)
        for field in INTEGER_FIELDS:
            assert type(feature["properties"][field]) is int
        for field in set(PROPERTY_FIELDS) - TEXT_FIELDS - INTEGER_FIELDS:
            assert finite_number(feature["properties"][field])
    header, rows = submitted_csv()
    assert header == PROPERTY_FIELDS and len(rows) == len(document["features"])
    for row in rows:
        assert list(row) == PROPERTY_FIELDS
        for field in INTEGER_FIELDS:
            assert INTEGER_TEXT.fullmatch(row[field])
        for field in set(PROPERTY_FIELDS) - TEXT_FIELDS - INTEGER_FIELDS:
            assert DECIMAL_TEXT.fullmatch(row[field]) and math.isfinite(float(row[field]))


def test_certificate_uses_the_exact_nested_schema():
    """Campaign and scenario certificate structures are exact and fully populated."""
    document = submitted_certificate()
    assert set(document) == {"schema_version", "campaigns"}
    assert document["schema_version"] == 2
    for campaign in document["campaigns"]:
        assert set(campaign) == CAMPAIGN_FIELDS
        assert type(campaign["analysis_unit_count"]) is int
        assert [point["scenario_id"] for point in campaign["scenarios"]] == list(SCENARIO_IDS)
        for point in campaign["scenarios"]:
            assert set(point) == SCENARIO_FIELDS
            assert type(point["binding"]) is bool
            assert type(point["active_unit_count"]) is int
            assert type(point["satisfied_unit_count"]) is int
            for field in SCENARIO_FIELDS - {
                "scenario_id", "binding", "active_unit_count", "satisfied_unit_count"
            }:
                assert finite_number(point[field])


def test_unit_identity_order_and_clipped_geometry_are_exact():
    """Every positive-area grid intersection appears once in canonical campaign/grid order."""
    expected_rows, _ = reference()
    features = submitted_geojson()["features"]
    assert len(features) == len(expected_rows)
    identities = []
    for feature, expected in zip(features, expected_rows, strict=True):
        actual = feature["properties"]
        identity = tuple(actual[field] for field in IDENTITY_FIELDS)
        assert identity == tuple(expected[field] for field in IDENTITY_FIELDS)
        identities.append(identity)
        geometry = shape(feature["geometry"])
        assert geometry.is_valid and not geometry.is_empty
        assert geometry.symmetric_difference(expected["geometry"]).area <= 1e-6
        close(actual["clipped_area_m2"], expected["clipped_area_m2"], 1e-6, 0.0)
        close(actual["area_fraction"], expected["area_fraction"], 1e-6, 0.0)
    assert len(identities) == len(set(identities))


def test_coupled_terminal_state_and_need_decomposition_match_reference():
    """Water, salt, stress history, request, and seasonal audits are independently pinned."""
    expected_rows, _ = reference()
    physical_fields = [
        field for field in BASE_FIELDS
        if field not in TEXT_FIELDS | INTEGER_FIELDS | {"clipped_area_m2", "area_fraction"}
    ]
    for actual, expected in zip(feature_rows(), expected_rows, strict=True):
        for field in physical_fields:
            close(actual[field], expected[field], *UNIT_TOLERANCES[field])
        assert actual["stress_days"] == expected["stress_days"]
        assert actual["salinity_stress_days"] == expected["salinity_stress_days"]


def test_every_policy_priority_depth_and_shortfall_match_reference():
    """All five joint allocation results are graded per unit, not just aggregates."""
    expected_rows, _ = reference()
    for actual, expected in zip(feature_rows(), expected_rows, strict=True):
        for scenario_id in SCENARIO_IDS:
            for suffix in ("priority", "depth_mm", "shortfall_mm"):
                field = f"{scenario_id}_{suffix}"
                close(actual[field], expected[field], *UNIT_TOLERANCES[field])
            close(
                actual[f"{scenario_id}_depth_mm"] + actual[f"{scenario_id}_shortfall_mm"],
                actual["request_mm"],
                0.01,
                0.001,
            )


def test_frontier_labels_ratios_and_gain_match_reference():
    """Activation, satisfaction, robustness, and recovery gain reflect the whole path."""
    expected_rows, _ = reference()
    allowed = set(SCENARIO_IDS) | {"none"}
    for actual, expected in zip(feature_rows(), expected_rows, strict=True):
        assert actual["activation_scenario"] in allowed
        assert actual["satisfaction_scenario"] in allowed
        assert actual["activation_scenario"] == expected["activation_scenario"]
        assert actual["satisfaction_scenario"] == expected["satisfaction_scenario"]
        close(actual["robustness_score"], expected["robustness_score"], 0.0001, 0.001)
        close(actual["frontier_gain_mm"], expected["frontier_gain_mm"], 0.01, 0.001)


def test_unit_fields_have_no_coherent_in_band_bias():
    """Systematic offsets cannot hide inside otherwise valid pointwise bands."""
    expected_rows, _ = reference()
    assert_no_coherent_bias(feature_rows(), expected_rows)


def test_bias_guard_rejects_additive_and_multiplicative_regressions():
    """Direct probes prove that both common coherent-bias attacks are rejected."""
    expected = [4.0 + index / 25.0 for index in range(200)]
    additive = [value + 0.009 for value in expected]
    with pytest.raises(AssertionError):
        assert_residual_distribution(additive, expected, 0.01, 0.0, "additive")
    coefficients = [0.6 + index / 500.0 for index in range(200)]
    multiplicative = [value * 1.0009 for value in coefficients]
    with pytest.raises(AssertionError):
        assert_residual_distribution(
            multiplicative, coefficients, 0.0001, 0.001, "multiplicative"
        )


def test_certificate_matches_independent_reference_values():
    """Every campaign/scenario certificate field matches a second implementation."""
    _, expected_certificates = reference()
    actual_certificates = submitted_certificate()["campaigns"]
    assert [row["campaign_id"] for row in actual_certificates] == [
        row["campaign_id"] for row in expected_certificates
    ]
    for actual, expected in zip(actual_certificates, expected_certificates, strict=True):
        assert actual["analysis_unit_count"] == expected["analysis_unit_count"]
        for field, tolerance in CERTIFICATE_TOLERANCES.items():
            close(actual[field], expected[field], *tolerance)
        for actual_point, expected_point in zip(
            actual["scenarios"], expected["scenarios"], strict=True
        ):
            assert actual_point["scenario_id"] == expected_point["scenario_id"]
            assert actual_point["binding"] is expected_point["binding"]
            assert actual_point["active_unit_count"] == expected_point["active_unit_count"]
            assert actual_point["satisfied_unit_count"] == expected_point["satisfied_unit_count"]
            for field, tolerance in SCENARIO_TOLERANCES.items():
                close(actual_point[field], expected_point[field], *tolerance)


def test_certificate_reconciles_submission_and_kkt_conditions():
    """Certificates must be internally true for submitted depths, budgets, and priorities."""
    rows_by_campaign = defaultdict(list)
    for row in feature_rows():
        rows_by_campaign[row["campaign_id"]].append(row)
    for campaign in submitted_certificate()["campaigns"]:
        rows = rows_by_campaign[campaign["campaign_id"]]
        previous_volume = 0.0
        requested_volume = math.fsum(
            row["clipped_area_m2"] * row["request_mm"] / 1000.0 for row in rows
        )
        close(campaign["requested_volume_m3"], requested_volume, 0.01, 0.001)
        for point in campaign["scenarios"]:
            scenario_id = point["scenario_id"]
            volume = math.fsum(
                row["clipped_area_m2"] * row[f"{scenario_id}_depth_mm"] / 1000.0
                for row in rows
            )
            cost = math.fsum(
                row["clipped_area_m2"]
                * row[f"{scenario_id}_priority"]
                * row[f"{scenario_id}_shortfall_mm"] ** 2
                for row in rows
            )
            close(point["allocated_volume_m3"], volume, 0.01, 0.001)
            close(point["shortfall_volume_m3"], requested_volume - volume, 0.01, 0.001)
            close(point["weighted_shortfall_cost"], cost, 0.01, 0.001)
            close(point["transition_volume_m3"], volume - previous_volume, 0.01, 0.001)
            previous_volume = volume
            if point["binding"]:
                close(volume, point["budget_m3"], 0.01, 0.001)
                for row in rows:
                    depth = row[f"{scenario_id}_depth_mm"]
                    if 1e-8 < depth < row["request_mm"] - 1e-8:
                        stationarity = 2.0 * row[f"{scenario_id}_priority"] * (
                            row["request_mm"] - depth
                        )
                        close(stationarity, point["depth_shadow_price"], 0.001, 0.001)
            else:
                close(point["depth_shadow_price"], 0.0, 0.0001, 0.0)


def test_csv_is_a_lossless_flat_view_of_geojson_properties():
    """The table cannot disagree with the spatial frontier on any scenario field."""
    _, csv_rows = submitted_csv()
    geo_rows = feature_rows()
    for csv_row, geo_row in zip(csv_rows, geo_rows, strict=True):
        for field in TEXT_FIELDS:
            assert csv_row[field] == geo_row[field]
        for field in INTEGER_FIELDS:
            assert int(csv_row[field]) == geo_row[field]
        for field in set(PROPERTY_FIELDS) - TEXT_FIELDS - INTEGER_FIELDS:
            close(float(csv_row[field]), geo_row[field], 1e-10, 1e-10)


def test_restricted_runner_denies_the_oracle_and_process_escape(tmp_path):
    """A solver cannot import/read the verifier oracle or spawn an escape process."""
    oracle_probe = tmp_path / "oracle_probe.py"
    oracle_probe.write_text(
        "from pathlib import Path\n"
        f"Path({str(TESTS / 'reference_model.py')!r}).read_text()\n"
    )
    completed = run_restricted_solver(
        oracle_probe,
        tmp_path / "unused-input",
        tmp_path / "unused-output",
        tmp_path,
        timeout=30,
    )
    assert completed.returncode != 0
    assert "submitted solvers cannot read verifier files" in completed.stderr

    process_probe = tmp_path / "process_probe.py"
    process_probe.write_text(
        "import subprocess, sys\n"
        "subprocess.run([sys.executable, '-c', 'pass'], check=True)\n"
    )
    completed = run_restricted_solver(
        process_probe,
        tmp_path / "unused-input",
        tmp_path / "unused-output",
        tmp_path,
        timeout=30,
    )
    assert completed.returncode != 0
    assert "submitted solvers cannot use subprocess.Popen" in completed.stderr


def test_reusable_solver_generalizes_to_private_geometry_state_and_policy(tmp_path):
    """The solver must handle unseen geometry plus changed state and contingency policy."""
    challenge_input = tmp_path / "counterfactual-input"
    challenge_output = tmp_path / "counterfactual-output"
    campaign_id = build_counterfactual_input(challenge_input)
    counterfactual_rows, counterfactual_certificates = calculate(challenge_input)
    published_rows, published_certificates = reference()
    published_campaign = [row for row in published_rows if row["campaign_id"] == campaign_id]
    assert [row["unit_id"] for row in counterfactual_rows] == [
        row["unit_id"] for row in published_campaign
    ]
    source = INPUT / "campaigns" / campaign_id
    challenge = challenge_input / "campaigns" / campaign_id
    source_field = shape(json.loads((source / "field_boundary.geojson").read_text())["geometry"])
    challenge_field = shape(
        json.loads((challenge / "field_boundary.geojson").read_text())["geometry"]
    )
    assert source_field.symmetric_difference(challenge_field).area > 1.0
    source_grid = json.loads((source / "job_ticket.json").read_text())["grid"]
    challenge_grid = json.loads((challenge / "job_ticket.json").read_text())["grid"]
    assert challenge_grid != source_grid
    assert challenge_grid["cell_width_m"] != source_grid["cell_width_m"]
    assert challenge_grid["cell_height_m"] != source_grid["cell_height_m"]
    changed = 0
    for counterfactual, published in zip(
        counterfactual_rows, published_campaign, strict=True
    ):
        for field in (
            "request_mm", "critical_priority", "critical_depth_mm",
            "restricted_depth_mm", "robustness_score", "final_ec_ds_m",
        ):
            absolute, relative = UNIT_TOLERANCES[field]
            if abs(counterfactual[field] - published[field]) > 5 * max(
                absolute, relative * abs(published[field])
            ):
                changed += 1
    assert changed >= 3 * len(counterfactual_rows)
    published_certificate = next(
        row for row in published_certificates if row["campaign_id"] == campaign_id
    )
    assert counterfactual_certificates[0]["satisfaction_ratio"] != published_certificate[
        "satisfaction_ratio"
    ]

    completed = run_restricted_solver(
        SOLVER_PATH,
        challenge_input,
        challenge_output,
        tmp_path,
    )
    assert completed.returncode == 0, (completed.stdout[-2000:], completed.stderr[-2000:])
    validate_complete_output(challenge_output, challenge_input)
