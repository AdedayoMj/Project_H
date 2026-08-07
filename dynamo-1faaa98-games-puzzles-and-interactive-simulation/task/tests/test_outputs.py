from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

import pytest

from reference_model import (
    CLASS_IDS,
    SCENARIO_IDS,
    SLOT_FIELDS,
    calculate,
    exact_plan,
    parse_mission,
    replay_plan,
)


APP = Path(os.environ.get("DOWNLINK_APP_ROOT", "/app"))
INPUT = APP / "input"
OUTPUT = APP / "output"
TESTS = Path(__file__).parent
PLAN_PATH = OUTPUT / "downlink-plan.json"
CSV_PATH = OUTPUT / "downlink-plan.csv"
CERTIFICATE_PATH = OUTPUT / "robustness-certificate.json"
SOLVER_PATH = OUTPUT / "solver.py"
RESTRICTED_RUNNER = TESTS / "restricted_solver_runner.py"

MISSION_FIELDS = {"mission_id", "slots", "outcomes"}
SLOT_FIELD_SET = set(SLOT_FIELDS)
OUTCOME_FIELDS = {
    "scenario_id",
    "weighted_loss",
    "delivered_packets",
    "lost_packets_by_class",
    "delivered_packets_by_class",
}
CERTIFICATE_FIELDS = {
    "mission_id",
    "objective_prefix",
    "total_energy_units",
    "minimum_battery_units",
    "peak_thermal_units",
    "contact_count",
    "plan_sha256",
    "outcomes",
}
INTEGER_FIELDS = {
    "slot",
    "pointing_step",
    "slew_steps",
    "energy_used_units",
    "battery_units",
    "thermal_units",
}
TEXT_FIELDS = {"mission_id", "action_id", "station_id"}
INTEGER_TEXT = re.compile(r"[+-]?[0-9]+\Z")
SHA256_TEXT = re.compile(r"[0-9a-f]{64}\Z")


def regular_file(path: Path) -> bool:
    return path.exists() and not path.is_symlink() and stat.S_ISREG(path.stat().st_mode)


@lru_cache(maxsize=1)
def submitted_plan():
    return json.loads(PLAN_PATH.read_text())


@lru_cache(maxsize=1)
def submitted_certificate():
    return json.loads(CERTIFICATE_PATH.read_text())


@lru_cache(maxsize=1)
def submitted_csv():
    with CSV_PATH.open(newline="") as handle:
        reader = csv.DictReader(handle)
        return reader.fieldnames, list(reader)


@lru_cache(maxsize=1)
def expected():
    return calculate(INPUT)


def manifest_entries(input_root: Path):
    return json.loads((input_root / "manifest.json").read_text())["missions"]


def validate_outcome(outcome: dict) -> None:
    assert set(outcome) == OUTCOME_FIELDS
    assert outcome["scenario_id"] in SCENARIO_IDS
    assert type(outcome["weighted_loss"]) is int
    assert type(outcome["delivered_packets"]) is int
    assert list(outcome["lost_packets_by_class"]) == list(CLASS_IDS)
    assert list(outcome["delivered_packets_by_class"]) == list(CLASS_IDS)
    for class_id in CLASS_IDS:
        assert type(outcome["lost_packets_by_class"][class_id]) is int
        assert type(outcome["delivered_packets_by_class"][class_id]) is int


def validate_documents(plan: dict, certificate: dict) -> None:
    assert set(plan) == {"schema_version", "missions"}
    assert plan["schema_version"] == 1
    assert set(certificate) == {"schema_version", "missions"}
    assert certificate["schema_version"] == 1
    assert len(plan["missions"]) == len(certificate["missions"])
    for mission, mission_certificate in zip(
        plan["missions"], certificate["missions"], strict=True
    ):
        assert set(mission) == MISSION_FIELDS
        assert set(mission_certificate) == CERTIFICATE_FIELDS
        assert mission["mission_id"] == mission_certificate["mission_id"]
        assert [row["slot"] for row in mission["slots"]] == list(
            range(len(mission["slots"]))
        )
        for row in mission["slots"]:
            assert set(row) == SLOT_FIELD_SET
            assert row["mission_id"] == mission["mission_id"]
            for field in INTEGER_FIELDS:
                assert type(row[field]) is int
            for field in TEXT_FIELDS:
                assert type(row[field]) is str
            if row["action_id"] == "idle":
                assert row["station_id"] == "none"
                assert row["pointing_step"] == 0
                assert row["slew_steps"] == 0
        assert [row["scenario_id"] for row in mission["outcomes"]] == list(
            SCENARIO_IDS
        )
        for outcome in mission["outcomes"]:
            validate_outcome(outcome)
        assert mission_certificate["outcomes"] == mission["outcomes"]
        assert len(mission_certificate["objective_prefix"]) == 6
        assert all(type(value) is int for value in mission_certificate["objective_prefix"])
        for field in (
            "total_energy_units",
            "minimum_battery_units",
            "peak_thermal_units",
            "contact_count",
        ):
            assert type(mission_certificate[field]) is int
        assert SHA256_TEXT.fullmatch(mission_certificate["plan_sha256"])


def validate_csv(plan: dict, csv_path: Path) -> None:
    with csv_path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        header, rows = reader.fieldnames, list(reader)
    assert header == list(SLOT_FIELDS)
    expected_rows = [
        row for mission in plan["missions"] for row in mission["slots"]
    ]
    assert len(rows) == len(expected_rows)
    for csv_row, json_row in zip(rows, expected_rows, strict=True):
        assert list(csv_row) == list(SLOT_FIELDS)
        for field in TEXT_FIELDS:
            assert csv_row[field] == json_row[field]
        for field in INTEGER_FIELDS:
            assert INTEGER_TEXT.fullmatch(csv_row[field])
            assert int(csv_row[field]) == json_row[field]


def validate_complete_output(output_root: Path, input_root: Path) -> None:
    plan_path = output_root / "downlink-plan.json"
    csv_path = output_root / "downlink-plan.csv"
    certificate_path = output_root / "robustness-certificate.json"
    assert all(regular_file(path) for path in (plan_path, csv_path, certificate_path))
    plan = json.loads(plan_path.read_text())
    certificate = json.loads(certificate_path.read_text())
    validate_documents(plan, certificate)
    validate_csv(plan, csv_path)
    expected_plans, expected_certificates = calculate(input_root)
    assert plan == {"schema_version": 1, "missions": expected_plans}
    assert certificate == {"schema_version": 1, "missions": expected_certificates}


def transform_csv(path: Path, transform) -> None:
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        fields, rows = reader.fieldnames, list(reader)
    assert fields is not None
    transform(rows)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def build_counterfactual_input(destination: Path) -> str:
    entries = manifest_entries(INPUT)
    entry = min(
        entries,
        key=lambda row: json.loads(
            (INPUT / row["directory"] / "mission.json").read_text()
        )["horizon_slots"],
    )
    mission_id = entry["mission_id"]
    source = INPUT / entry["directory"]
    target = destination / "missions" / mission_id
    destination.mkdir(parents=True)
    shutil.copytree(source, target)
    shutil.copy2(INPUT / "specification.md", destination / "specification.md")

    ticket_path = target / "mission.json"
    ticket = json.loads(ticket_path.read_text())
    ticket["storage_capacity_packets"] -= 2
    ticket["battery"]["initial_units"] += 3
    ticket["battery"]["reserve_units"] += 1
    ticket["thermal"]["limit_units"] += 2
    ticket["slew"]["max_steps_per_slot"] = max(
        2, ticket["slew"]["max_steps_per_slot"] - 1
    )
    point_by_station = {}
    for index, station in enumerate(ticket["stations"]):
        station["pointing_step"] = -int(station["pointing_step"]) + (index % 2)
        point_by_station[station["station_id"]] = station["pointing_step"]
    ticket_path.write_text(json.dumps(ticket, indent=2) + "\n")

    def alter_timeline(rows):
        for index, row in enumerate(rows):
            value = int(row["solar_units"])
            row["solar_units"] = max(0, value + ((index * 3 + 1) % 5) - 2)

    def alter_packets(rows):
        horizon = ticket["horizon_slots"]
        for index, row in enumerate(rows):
            release = int(row["release_slot"])
            deadline = int(row["deadline_slot"])
            if index % 4 == 1 and release < deadline:
                release += 1
            if index % 3 == 0:
                deadline = min(horizon - 1, deadline + 1)
            elif index % 3 == 2:
                deadline = max(release, deadline - 1)
            row["release_slot"] = release
            row["deadline_slot"] = deadline
            row["packet_count"] = int(row["packet_count"]) + 1 + (index % 2)

    def alter_contacts(rows):
        for index, row in enumerate(rows):
            row["pointing_step"] = point_by_station[row["station_id"]]
            row["nominal_capacity_packets"] = max(
                2, int(row["nominal_capacity_packets"]) + (index % 3) - 1
            )
            row["energy_units"] = max(2, int(row["energy_units"]) + (index % 2))
            row["heat_units"] = max(1, int(row["heat_units"]) + ((index + 1) % 3) - 1)

    transform_csv(target / "timeline.csv", alter_timeline)
    transform_csv(target / "packets.csv", alter_packets)
    transform_csv(target / "contacts.csv", alter_contacts)
    (destination / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "missions": [
                    {"mission_id": mission_id, "directory": f"missions/{mission_id}"}
                ],
            },
            indent=2,
        )
        + "\n"
    )
    return mission_id


def run_restricted_solver(
    solver_path: Path,
    input_root: Path,
    output_root: Path,
    cwd: Path,
    timeout: int = 180,
):
    environment = os.environ.copy()
    environment.pop("DOWNLINK_APP_ROOT", None)
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


def test_requested_artifacts_are_regular_parseable_files():
    """All three data products and the reusable solver are ordinary files."""
    for path in (PLAN_PATH, CSV_PATH, CERTIFICATE_PATH, SOLVER_PATH):
        assert regular_file(path)
    assert isinstance(submitted_plan(), dict)
    assert submitted_csv()[0] == list(SLOT_FIELDS)
    assert isinstance(submitted_certificate(), dict)


def test_generated_mission_evidence_is_hash_locked_and_builder_is_absent():
    """Published evidence cannot be edited and its generator is not agent-visible."""
    expected_hashes = json.loads((TESTS / "input-manifest.json").read_text())
    actual_hashes = {}
    for path in sorted(INPUT.rglob("*")):
        if path.is_dir():
            continue
        assert not path.is_symlink() and stat.S_ISREG(path.stat().st_mode)
        actual_hashes[path.relative_to(INPUT).as_posix()] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    assert actual_hashes == expected_hashes
    assert not Path("/tmp/downlink-task/build_instance.py").exists()


def test_input_worlds_are_structurally_diverse_and_closed():
    """Missions vary horizons, resource limits, contacts, and packet calendars."""
    manifest = json.loads((INPUT / "manifest.json").read_text())
    assert set(manifest) == {"schema_version", "missions"}
    assert manifest["schema_version"] == 1
    assert len(manifest["missions"]) == 5
    horizons = set()
    contact_counts = set()
    packet_counts = set()
    for entry in manifest["missions"]:
        assert set(entry) == {"mission_id", "directory"}
        root = INPUT / entry["directory"]
        ticket = json.loads((root / "mission.json").read_text())
        assert ticket["mission_id"] == entry["mission_id"]
        assert [row["scenario_id"] for row in ticket["scenarios"]] == list(SCENARIO_IDS)
        assert [row["class_id"] for row in ticket["classes"]] == list(CLASS_IDS)
        horizons.add(ticket["horizon_slots"])
        contact_counts.add(len(list(csv.DictReader((root / "contacts.csv").open()))))
        packet_counts.add(len(list(csv.DictReader((root / "packets.csv").open()))))
    assert len(horizons) >= 4
    assert len(contact_counts) >= 3
    assert len(packet_counts) >= 3


def test_plan_and_certificate_schemas_are_exact():
    """No undeclared fields, weak types, or alternate scenario structures are accepted."""
    validate_documents(submitted_plan(), submitted_certificate())


def test_unique_robust_action_sequence_matches_independent_optimizer():
    """Every slot action equals the unique global optimum, including final tie-breaking."""
    expected_plans, _ = expected()
    actual_missions = submitted_plan()["missions"]
    assert [row["mission_id"] for row in actual_missions] == [
        row["mission_id"] for row in expected_plans
    ]
    for actual, reference in zip(actual_missions, expected_plans, strict=True):
        assert [row["action_id"] for row in actual["slots"]] == [
            row["action_id"] for row in reference["slots"]
        ]


def test_every_resource_transition_and_slew_audit_is_exact():
    """Battery, heat, energy and pointing history match an independent replay."""
    expected_plans, _ = expected()
    for actual, reference in zip(
        submitted_plan()["missions"], expected_plans, strict=True
    ):
        assert actual["slots"] == reference["slots"]


def test_all_scenario_losses_and_deliveries_are_exact():
    """One action sequence is replayed in every capacity world with exact class ledgers."""
    expected_plans, _ = expected()
    for actual, reference in zip(
        submitted_plan()["missions"], expected_plans, strict=True
    ):
        assert actual["outcomes"] == reference["outcomes"]
        weighted = [row["weighted_loss"] for row in actual["outcomes"]]
        assert weighted[0] >= weighted[1] >= weighted[2] >= weighted[3]


def test_certificate_matches_optimum_resources_outcomes_and_digest():
    """The certificate is fully pinned and its digest is derived from submitted actions."""
    _, expected_certificates = expected()
    actual_certificates = submitted_certificate()["missions"]
    assert actual_certificates == expected_certificates
    for mission, certificate in zip(
        submitted_plan()["missions"], actual_certificates, strict=True
    ):
        action_text = "\n".join(row["action_id"] for row in mission["slots"]) + "\n"
        assert certificate["plan_sha256"] == hashlib.sha256(action_text.encode()).hexdigest()
        assert certificate["total_energy_units"] == sum(
            row["energy_used_units"] for row in mission["slots"]
        )
        assert certificate["minimum_battery_units"] == min(
            row["battery_units"] for row in mission["slots"]
        )
        assert certificate["peak_thermal_units"] == max(
            row["thermal_units"] for row in mission["slots"]
        )


def test_csv_is_a_lossless_flat_view_in_manifest_slot_order():
    """The table cannot disagree with any JSON decision or resource state."""
    validate_csv(submitted_plan(), CSV_PATH)


def test_published_optima_exercise_all_coupled_constraints():
    """Fixtures require idle gaps, station changes, losses, and binding resource tradeoffs."""
    certificates = submitted_certificate()["missions"]
    assert all(7 <= row["contact_count"] < 14 for row in certificates)
    assert all(row["objective_prefix"][0] > 0 for row in certificates)
    assert any(row["objective_prefix"][2] > 0 for row in certificates)
    assert any(row["peak_thermal_units"] >= 29 for row in certificates)
    for mission in submitted_plan()["missions"]:
        action_ids = [row["action_id"] for row in mission["slots"]]
        stations = {row["station_id"] for row in mission["slots"] if row["action_id"] != "idle"}
        assert "idle" in action_ids
        assert len(stations) >= 3
        assert any(row["slew_steps"] > 0 for row in mission["slots"])


def test_feasible_last_contact_deletion_is_rejected_as_nonoptimal():
    """A resource-feasible schedule still fails when it is not the unique robust optimum."""
    entry = manifest_entries(INPUT)[0]
    world = parse_mission(INPUT, entry)
    optimal_actions, objective = exact_plan(world)
    last_contact = max(index for index, action in enumerate(optimal_actions) if action != "idle")
    mutated_actions = list(optimal_actions)
    mutated_actions[last_contact] = "idle"
    mutated_plan, _ = replay_plan(world, tuple(mutated_actions), objective)
    expected_plan = expected()[0][0]
    assert mutated_plan["slots"] != expected_plan["slots"]
    with pytest.raises(AssertionError):
        assert mutated_plan == expected_plan


def test_restricted_runner_denies_oracle_reads_and_process_escape(tmp_path):
    """A submitted solver cannot delegate planning to the mounted verifier."""
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


def test_reusable_solver_optimizes_a_private_changed_mission(tmp_path):
    """Published schedules fail when timing, contacts, pointing and resources all change."""
    private_input = tmp_path / "private-input"
    private_output = tmp_path / "private-output"
    mission_id = build_counterfactual_input(private_input)
    private_plans, private_certificates = calculate(private_input)
    public_plan = next(
        row for row in submitted_plan()["missions"] if row["mission_id"] == mission_id
    )
    assert [row["action_id"] for row in private_plans[0]["slots"]] != [
        row["action_id"] for row in public_plan["slots"]
    ]
    assert private_certificates[0]["objective_prefix"] != next(
        row["objective_prefix"]
        for row in submitted_certificate()["missions"]
        if row["mission_id"] == mission_id
    )

    completed = run_restricted_solver(
        SOLVER_PATH,
        private_input,
        private_output,
        tmp_path,
    )
    assert completed.returncode == 0, (completed.stdout[-2000:], completed.stderr[-2000:])
    validate_complete_output(private_output, private_input)
