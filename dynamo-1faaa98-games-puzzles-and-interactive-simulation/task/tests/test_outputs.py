from __future__ import annotations

import csv
import hashlib
import json
import os
import random
import re
import shutil
import stat
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

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

MISSION_KEYS = {"mission_id", "slots", "outcomes"}
SLOT_KEYS = set(SLOT_FIELDS)
RESULT_KEYS = {
    "scenario_id",
    "weighted_loss",
    "delivered_packets",
    "lost_packets_by_class",
    "delivered_packets_by_class",
}
CERTIFICATE_KEYS = {
    "mission_id",
    "objective_prefix",
    "total_energy_units",
    "minimum_battery_units",
    "peak_thermal_units",
    "contact_count",
    "plan_sha256",
    "outcomes",
}
SLOT_INTEGER_KEYS = {
    "slot",
    "pointing_step",
    "slew_steps",
    "energy_used_units",
    "battery_units",
    "thermal_units",
}
SLOT_TEXT_KEYS = {"mission_id", "action_id", "station_id"}
DECIMAL_INTEGER = re.compile(r"[+-]?[0-9]+\Z")
HEX_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
PRIVATE_MISSION_SEED = 0xD017E57
PRIVATE_VARIANT_COUNT = 3


def ordinary_file(path: Path) -> bool:
    """Accept an existing regular file while rejecting symlinks and devices."""
    return path.exists() and not path.is_symlink() and stat.S_ISREG(path.stat().st_mode)


def read_json(path: Path):
    """Decode one UTF-8 JSON artifact."""
    return json.loads(path.read_text())


@lru_cache(maxsize=1)
def candidate_plan():
    """Load the submitted slot plan once."""
    return read_json(PLAN_PATH)


@lru_cache(maxsize=1)
def candidate_certificate():
    """Load the submitted robustness certificate once."""
    return read_json(CERTIFICATE_PATH)


@lru_cache(maxsize=1)
def candidate_table():
    """Load the submitted flat slot table once."""
    with CSV_PATH.open(newline="") as handle:
        reader = csv.DictReader(handle)
        return reader.fieldnames, list(reader)


@lru_cache(maxsize=1)
def oracle_outputs():
    """Compute all optimal mission plans with the independent implementation."""
    return calculate(INPUT)


def mission_entries(input_root: Path):
    """Return the manifest's ordered mission references."""
    return read_json(input_root / "manifest.json")["missions"]


class DownlinkAudit:
    """Validate the three linked data products for one input root."""

    def __init__(self, plan: dict, certificate: dict, table_path: Path):
        self.plan = plan
        self.certificate = certificate
        self.table_path = table_path

    def check_contract(self) -> None:
        """Enforce document shape, scalar types, canonical order, and cross-links."""
        for document in (self.plan, self.certificate):
            assert set(document) == {"schema_version", "missions"}
            assert type(document["schema_version"]) is int
            assert document["schema_version"] == 1
        assert len(self.plan["missions"]) == len(self.certificate["missions"])

        pairs = zip(
            self.plan["missions"], self.certificate["missions"], strict=True
        )
        for mission, proof in pairs:
            self.check_mission(mission, proof)
        self.check_flat_table()

    @staticmethod
    def check_mission(mission: dict, proof: dict) -> None:
        """Check one mission's slots, scenario ledgers, and certificate fields."""
        assert set(mission) == MISSION_KEYS
        assert set(proof) == CERTIFICATE_KEYS
        assert mission["mission_id"] == proof["mission_id"]
        assert [record["slot"] for record in mission["slots"]] == list(
            range(len(mission["slots"]))
        )
        for record in mission["slots"]:
            assert set(record) == SLOT_KEYS
            assert record["mission_id"] == mission["mission_id"]
            assert all(type(record[key]) is int for key in SLOT_INTEGER_KEYS)
            assert all(type(record[key]) is str for key in SLOT_TEXT_KEYS)
            if record["action_id"] == "idle":
                assert (
                    record["station_id"],
                    record["pointing_step"],
                    record["slew_steps"],
                ) == ("none", 0, 0)

        scenario_order = [result["scenario_id"] for result in mission["outcomes"]]
        assert scenario_order == list(SCENARIO_IDS)
        for result in mission["outcomes"]:
            DownlinkAudit.check_scenario_result(result)
        assert proof["outcomes"] == mission["outcomes"]
        assert len(proof["objective_prefix"]) == 6
        assert all(type(value) is int for value in proof["objective_prefix"])
        summary_keys = (
            "total_energy_units",
            "minimum_battery_units",
            "peak_thermal_units",
            "contact_count",
        )
        assert all(type(proof[key]) is int for key in summary_keys)
        assert HEX_DIGEST.fullmatch(proof["plan_sha256"])

    @staticmethod
    def check_scenario_result(result: dict) -> None:
        """Require complete exact loss and delivery ledgers in class order."""
        assert set(result) == RESULT_KEYS
        assert result["scenario_id"] in SCENARIO_IDS
        assert type(result["weighted_loss"]) is int
        assert type(result["delivered_packets"]) is int
        for ledger_name in (
            "lost_packets_by_class",
            "delivered_packets_by_class",
        ):
            ledger = result[ledger_name]
            assert list(ledger) == list(CLASS_IDS)
            assert all(type(ledger[class_id]) is int for class_id in CLASS_IDS)

    def check_flat_table(self) -> None:
        """Match every CSV cell to the corresponding JSON slot record."""
        with self.table_path.open(newline="") as handle:
            reader = csv.DictReader(handle)
            header, rows = reader.fieldnames, list(reader)
        assert header == list(SLOT_FIELDS)
        json_rows = [
            record
            for mission in self.plan["missions"]
            for record in mission["slots"]
        ]
        assert len(rows) == len(json_rows)
        for table_row, json_row in zip(rows, json_rows, strict=True):
            assert list(table_row) == list(SLOT_FIELDS)
            assert all(
                table_row[key] == json_row[key] for key in SLOT_TEXT_KEYS
            )
            for key in SLOT_INTEGER_KEYS:
                assert DECIMAL_INTEGER.fullmatch(table_row[key])
                assert int(table_row[key]) == json_row[key]

    def check_against_oracle(self, input_root: Path) -> None:
        """Require complete equality with an independently optimized answer."""
        self.check_contract()
        optimal_plans, optimal_certificates = calculate(input_root)
        assert self.plan == {"schema_version": 1, "missions": optimal_plans}
        assert self.certificate == {
            "schema_version": 1,
            "missions": optimal_certificates,
        }


def audit_output_directory(output_root: Path, input_root: Path) -> None:
    """Load and completely audit an output directory produced by a solver run."""
    plan_path = output_root / "downlink-plan.json"
    table_path = output_root / "downlink-plan.csv"
    proof_path = output_root / "robustness-certificate.json"
    assert all(ordinary_file(path) for path in (plan_path, table_path, proof_path))
    DownlinkAudit(
        read_json(plan_path),
        read_json(proof_path),
        table_path,
    ).check_against_oracle(input_root)


def rewrite_csv(path: Path, mutation) -> None:
    """Apply a row mutation while preserving a table's header."""
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames, rows = reader.fieldnames, list(reader)
    assert fieldnames is not None
    mutation(rows)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def write_csv_rows(path: Path, fields: list[str], rows: list[dict]) -> None:
    """Write a verifier-created CSV fixture with canonical newlines."""
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def write_peak_merge_mission(destination: Path) -> dict[str, str]:
    """Materialize the max-thermal state-merge counterexample for solver audit."""
    mission_id = "private-peak-merge-regression"
    mission_root = destination / "missions" / mission_id
    mission_root.mkdir(parents=True)
    ticket = {
        "schema_version": 1,
        "mission_id": mission_id,
        "horizon_slots": 4,
        "storage_capacity_packets": 10,
        "battery": {
            "capacity_units": 20,
            "initial_units": 20,
            "reserve_units": 0,
            "idle_cost_units": 1,
        },
        "thermal": {
            "initial_units": 0,
            "limit_units": 20,
            "passive_cooling_units": 5,
            "slew_heat_per_step": 0,
        },
        "slew": {"max_steps_per_slot": 4, "energy_per_step": 0},
        "classes": [
            {"class_id": "command", "loss_weight": 19},
            {"class_id": "science", "loss_weight": 7},
            {"class_id": "engineering", "loss_weight": 3},
        ],
        "scenarios": [
            {
                "scenario_id": scenario_id,
                "capacity_numerator": 1,
                "capacity_denominator": 1,
            }
            for scenario_id in SCENARIO_IDS
        ],
        "nominal_scenario_id": "nominal",
        "stations": [{"station_id": "S", "pointing_step": 0}],
    }
    (mission_root / "mission.json").write_text(
        json.dumps(ticket, indent=2) + "\n"
    )
    write_csv_rows(
        mission_root / "timeline.csv",
        ["slot", "solar_units"],
        [{"slot": slot, "solar_units": 1} for slot in range(4)],
    )
    write_csv_rows(
        mission_root / "packets.csv",
        ["batch_id", "release_slot", "deadline_slot", "class_id", "packet_count"],
        [
            {
                "batch_id": "B0",
                "release_slot": 0,
                "deadline_slot": 1,
                "class_id": "command",
                "packet_count": 2,
            },
            {
                "batch_id": "B1",
                "release_slot": 2,
                "deadline_slot": 2,
                "class_id": "command",
                "packet_count": 1,
            },
            {
                "batch_id": "B2",
                "release_slot": 3,
                "deadline_slot": 3,
                "class_id": "command",
                "packet_count": 1,
            },
        ],
    )
    write_csv_rows(
        mission_root / "contacts.csv",
        [
            "action_id",
            "slot",
            "station_id",
            "pointing_step",
            "nominal_capacity_packets",
            "energy_units",
            "heat_units",
        ],
        [
            {
                "action_id": "cool",
                "slot": 0,
                "station_id": "S",
                "pointing_step": 0,
                "nominal_capacity_packets": 1,
                "energy_units": 0,
                "heat_units": 2,
            },
            {
                "action_id": "hot",
                "slot": 0,
                "station_id": "S",
                "pointing_step": 0,
                "nominal_capacity_packets": 2,
                "energy_units": 0,
                "heat_units": 8,
            },
            {
                "action_id": "medium",
                "slot": 1,
                "station_id": "S",
                "pointing_step": 0,
                "nominal_capacity_packets": 1,
                "energy_units": 0,
                "heat_units": 3,
            },
            {
                "action_id": "anchor",
                "slot": 2,
                "station_id": "S",
                "pointing_step": 0,
                "nominal_capacity_packets": 1,
                "energy_units": 0,
                "heat_units": 1,
            },
            {
                "action_id": "final",
                "slot": 3,
                "station_id": "S",
                "pointing_step": 0,
                "nominal_capacity_packets": 1,
                "energy_units": 0,
                "heat_units": 10,
            },
        ],
    )
    return {"mission_id": mission_id, "directory": f"missions/{mission_id}"}


def write_unseen_missions(destination: Path) -> list[tuple[str, str | None]]:
    """Create a reproducible adversarial corpus spanning public mission families."""
    generator = random.Random(PRIVATE_MISSION_SEED)
    source_entries = sorted(
        mission_entries(INPUT),
        key=lambda item: (
            read_json(INPUT / item["directory"] / "mission.json")[
                "horizon_slots"
            ],
            item["mission_id"],
        ),
    )
    destination.mkdir(parents=True)
    shutil.copy2(INPUT / "specification.md", destination / "specification.md")
    peak_record = write_peak_merge_mission(destination)
    records = [peak_record]
    provenance: list[tuple[str, str | None]] = [
        (peak_record["mission_id"], None)
    ]

    for private_number in range(1, PRIVATE_VARIANT_COUNT + 1):
        source_entry = source_entries[(private_number - 1) % len(source_entries)]
        public_mission_id = source_entry["mission_id"]
        private_mission_id = (
            f"private-{private_number}-{generator.getrandbits(64):016x}"
        )
        public_directory = INPUT / source_entry["directory"]
        private_directory = destination / "missions" / private_mission_id
        shutil.copytree(public_directory, private_directory)

        ticket_path = private_directory / "mission.json"
        ticket = read_json(ticket_path)
        ticket["mission_id"] = private_mission_id
        ticket["storage_capacity_packets"] = max(
            4,
            ticket["storage_capacity_packets"] - generator.randint(1, 3),
        )
        battery_boost = (
            ticket["horizon_slots"] * ticket["battery"]["idle_cost_units"]
            + generator.randint(4, 12)
        )
        ticket["battery"]["capacity_units"] += battery_boost
        ticket["battery"]["initial_units"] += battery_boost
        ticket["battery"]["reserve_units"] += generator.randint(0, 1)
        ticket["thermal"]["limit_units"] += generator.randint(2, 6)
        ticket["slew"]["max_steps_per_slot"] = max(
            2,
            ticket["slew"]["max_steps_per_slot"] - generator.randint(0, 1),
        )
        orientation = generator.choice((-1, 1))
        pointing_shift = generator.randint(-2, 2)
        station_pointing = {}
        for station in ticket["stations"]:
            station["pointing_step"] = (
                orientation * int(station["pointing_step"])
                + pointing_shift
                + generator.randint(-1, 1)
            )
            station_pointing[station["station_id"]] = station["pointing_step"]
        ticket_path.write_text(json.dumps(ticket, indent=2) + "\n")

        def perturb_solar(rows):
            for row in rows:
                row["solar_units"] = max(
                    0,
                    int(row["solar_units"]) + generator.randint(-2, 2),
                )

        def perturb_packet_calendar(rows):
            horizon = ticket["horizon_slots"]
            for row in rows:
                release = int(row["release_slot"])
                deadline = int(row["deadline_slot"])
                release += generator.randint(0, min(1, deadline - release))
                deadline = min(
                    horizon - 1,
                    max(release, deadline + generator.randint(-1, 1)),
                )
                row["release_slot"] = release
                row["deadline_slot"] = deadline
                row["packet_count"] = (
                    int(row["packet_count"]) + generator.randint(1, 3)
                )

        def perturb_contacts(rows):
            for row in rows:
                row["pointing_step"] = station_pointing[row["station_id"]]
                row["nominal_capacity_packets"] = max(
                    2,
                    int(row["nominal_capacity_packets"])
                    + generator.randint(-2, 2),
                )
                row["energy_units"] = (
                    int(row["energy_units"]) + generator.randint(1, 3)
                )
                row["heat_units"] = max(
                    1,
                    int(row["heat_units"]) + generator.randint(-1, 2),
                )

        rewrite_csv(private_directory / "timeline.csv", perturb_solar)
        rewrite_csv(private_directory / "packets.csv", perturb_packet_calendar)
        rewrite_csv(private_directory / "contacts.csv", perturb_contacts)
        records.append(
            {
                "mission_id": private_mission_id,
                "directory": f"missions/{private_mission_id}",
            }
        )
        provenance.append((private_mission_id, public_mission_id))

    (destination / "manifest.json").write_text(
        json.dumps({"schema_version": 1, "missions": records}, indent=2)
        + "\n"
    )
    return provenance


def invoke_submitted_solver(
    solver_path: Path,
    input_root: Path,
    output_root: Path,
    cwd: Path,
    timeout: int = 180,
) -> subprocess.CompletedProcess[str]:
    """Execute a solver behind the verifier-read/process/network audit hook."""
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
        assert ordinary_file(path)
    assert isinstance(candidate_plan(), dict)
    assert candidate_table()[0] == list(SLOT_FIELDS)
    assert isinstance(candidate_certificate(), dict)


def test_generated_mission_evidence_is_hash_locked_and_builder_is_absent():
    """Published evidence cannot be edited and its generator is not agent-visible."""
    expected_hashes = read_json(TESTS / "input-manifest.json")
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
    manifest = read_json(INPUT / "manifest.json")
    assert set(manifest) == {"schema_version", "missions"}
    assert manifest["schema_version"] == 1
    assert len(manifest["missions"]) == 5
    horizons = set()
    contact_counts = set()
    packet_counts = set()
    for entry in manifest["missions"]:
        assert set(entry) == {"mission_id", "directory"}
        root = INPUT / entry["directory"]
        ticket = read_json(root / "mission.json")
        assert ticket["mission_id"] == entry["mission_id"]
        assert [row["scenario_id"] for row in ticket["scenarios"]] == list(SCENARIO_IDS)
        assert [row["class_id"] for row in ticket["classes"]] == list(CLASS_IDS)
        horizons.add(ticket["horizon_slots"])
        contact_counts.add(len(list(csv.DictReader((root / "contacts.csv").open()))))
        packet_counts.add(len(list(csv.DictReader((root / "packets.csv").open()))))
    assert len(horizons) >= 4
    assert len(contact_counts) >= 3
    assert len(packet_counts) >= 3


def test_linked_artifacts_use_the_exact_typed_contract():
    """Reject undeclared fields, Boolean integers, bad ordering, and CSV drift."""
    DownlinkAudit(
        candidate_plan(), candidate_certificate(), CSV_PATH
    ).check_contract()


def test_shared_action_tapes_match_the_independent_robust_optimizer():
    """Pin every contact choice and the final action-rank tie-break."""
    optimal_plans, _ = oracle_outputs()
    submitted_missions = candidate_plan()["missions"]
    assert [mission["mission_id"] for mission in submitted_missions] == [
        mission["mission_id"] for mission in optimal_plans
    ]
    for submitted, optimal in zip(submitted_missions, optimal_plans, strict=True):
        assert [record["action_id"] for record in submitted["slots"]] == [
            record["action_id"] for record in optimal["slots"]
        ]


def test_spacecraft_state_audit_matches_an_independent_slot_replay():
    """Pin battery saturation, heat recovery, energy use, and pointing memory."""
    optimal_plans, _ = oracle_outputs()
    for submitted, optimal in zip(
        candidate_plan()["missions"], optimal_plans, strict=True
    ):
        assert submitted["slots"] == optimal["slots"]


def test_one_tape_has_exact_ledgers_in_all_four_capacity_worlds():
    """Pin overflow, service, expiry, class delivery, and weighted loss."""
    optimal_plans, _ = oracle_outputs()
    for submitted, optimal in zip(
        candidate_plan()["missions"], optimal_plans, strict=True
    ):
        assert submitted["outcomes"] == optimal["outcomes"]
        weighted = [result["weighted_loss"] for result in submitted["outcomes"]]
        assert weighted[0] >= weighted[1] >= weighted[2] >= weighted[3]


def test_robustness_certificate_reconciles_actions_resources_and_digest():
    """Derive every certificate summary from the submitted tape and slot states."""
    _, optimal_certificates = oracle_outputs()
    submitted_certificates = candidate_certificate()["missions"]
    assert submitted_certificates == optimal_certificates
    for mission, proof in zip(
        candidate_plan()["missions"], submitted_certificates, strict=True
    ):
        action_bytes = (
            "\n".join(record["action_id"] for record in mission["slots"]) + "\n"
        ).encode()
        assert proof["plan_sha256"] == hashlib.sha256(action_bytes).hexdigest()
        assert proof["total_energy_units"] == sum(
            record["energy_used_units"] for record in mission["slots"]
        )
        assert proof["minimum_battery_units"] == min(
            record["battery_units"] for record in mission["slots"]
        )
        assert proof["peak_thermal_units"] == max(
            record["thermal_units"] for record in mission["slots"]
        )


def test_slot_table_is_the_lossless_manifest_order_plan_projection():
    """Match every CSV cell to its JSON decision and spacecraft state."""
    DownlinkAudit(
        candidate_plan(), candidate_certificate(), CSV_PATH
    ).check_flat_table()


def test_published_optima_exercise_all_coupled_constraints():
    """Fixtures require idle gaps, station changes, losses, and binding resource tradeoffs."""
    certificates = candidate_certificate()["missions"]
    assert all(9 <= row["contact_count"] < 20 for row in certificates)
    assert all(row["objective_prefix"][0] > 0 for row in certificates)
    assert any(row["objective_prefix"][2] > 0 for row in certificates)
    assert any(row["peak_thermal_units"] >= 29 for row in certificates)
    for mission in candidate_plan()["missions"]:
        action_ids = [row["action_id"] for row in mission["slots"]]
        stations = {row["station_id"] for row in mission["slots"] if row["action_id"] != "idle"}
        assert "idle" in action_ids
        assert len(stations) >= 3
        assert any(row["slew_steps"] > 0 for row in mission["slots"])


def test_feasible_last_contact_deletion_is_rejected_as_nonoptimal():
    """A resource-feasible schedule still fails when it is not the unique robust optimum."""
    entry = mission_entries(INPUT)[0]
    world = parse_mission(INPUT, entry)
    optimal_actions, objective = exact_plan(world)
    last_contact = max(index for index, action in enumerate(optimal_actions) if action != "idle")
    mutated_actions = list(optimal_actions)
    mutated_actions[last_contact] = "idle"
    mutated_plan, _ = replay_plan(world, tuple(mutated_actions), objective)
    optimal_plan = oracle_outputs()[0][0]
    assert mutated_plan["slots"] != optimal_plan["slots"]
    assert mutated_plan != optimal_plan


def test_peak_history_survives_state_merge_until_future_maximum_is_known():
    """A hotter prefix can win after a later maximum ties peaks and contacts decide."""
    world = {
        "id": "peak_merge_regression",
        "horizon": 4,
        "storage": 10,
        "battery": {
            "capacity_units": 20,
            "initial_units": 20,
            "reserve_units": 0,
            "idle_cost_units": 1,
        },
        "thermal": {
            "initial_units": 0,
            "limit_units": 20,
            "passive_cooling_units": 5,
            "slew_heat_per_step": 0,
        },
        "slew": {"max_steps_per_slot": 4, "energy_per_step": 0},
        "class_ids": ["command"],
        "weights": [1],
        "scenario_ids": ["nominal"],
        "fractions": [(1, 1)],
        "nominal": 0,
        "solar": [1, 1, 1, 1],
        "batches": [
            {
                "id": "B0",
                "release": 0,
                "deadline": 1,
                "class": 0,
                "count": 2,
            },
            {
                "id": "B1",
                "release": 2,
                "deadline": 2,
                "class": 0,
                "count": 1,
            },
            {
                "id": "B2",
                "release": 3,
                "deadline": 3,
                "class": 0,
                "count": 1,
            },
        ],
        "contacts": [
            [
                {
                    "id": "cool",
                    "station": "S",
                    "slot": 0,
                    "point": 0,
                    "capacity": 1,
                    "energy": 0,
                    "heat": 2,
                },
                {
                    "id": "hot",
                    "station": "S",
                    "slot": 0,
                    "point": 0,
                    "capacity": 2,
                    "energy": 0,
                    "heat": 8,
                },
            ],
            [
                {
                    "id": "medium",
                    "station": "S",
                    "slot": 1,
                    "point": 0,
                    "capacity": 1,
                    "energy": 0,
                    "heat": 3,
                }
            ],
            [
                {
                    "id": "anchor",
                    "station": "S",
                    "slot": 2,
                    "point": 0,
                    "capacity": 1,
                    "energy": 0,
                    "heat": 1,
                }
            ],
            [
                {
                    "id": "final",
                    "station": "S",
                    "slot": 3,
                    "point": 0,
                    "capacity": 1,
                    "energy": 0,
                    "heat": 10,
                }
            ],
        ],
    }
    actions, objective = exact_plan(world)
    assert actions == ("hot", "idle", "anchor", "final")
    assert objective == (0, 0, 0, 4, 10, 3)


def test_restricted_runner_denies_oracle_reads_and_process_escape(tmp_path):
    """A submitted solver cannot delegate planning to the mounted verifier."""
    oracle_probe = tmp_path / "oracle_probe.py"
    oracle_probe.write_text(
        "from pathlib import Path\n"
        f"Path({str(TESTS / 'reference_model.py')!r}).read_text()\n"
    )
    completed = invoke_submitted_solver(
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
    completed = invoke_submitted_solver(
        process_probe,
        tmp_path / "unused-input",
        tmp_path / "unused-output",
        tmp_path,
        timeout=30,
    )
    assert completed.returncode != 0
    assert "submitted solvers cannot use subprocess.Popen" in completed.stderr


def test_reusable_solver_optimizes_adversarial_private_corpus(tmp_path):
    """A solver must handle a broad corpus and the peak-history counterexample."""
    private_input = tmp_path / "private-input"
    private_output = tmp_path / "private-output"
    private_cases = write_unseen_missions(private_input)
    private_plans, private_certificates = calculate(private_input)
    public_ids = {row["mission_id"] for row in candidate_plan()["missions"]}
    private_ids = [private_id for private_id, _source_id in private_cases]
    public_entries = {
        row["mission_id"]: row for row in mission_entries(INPUT)
    }
    assert len(private_cases) == PRIVATE_VARIANT_COUNT + 1
    assert len(set(private_ids)) == PRIVATE_VARIANT_COUNT + 1
    assert set(private_ids).isdisjoint(public_ids)
    assert [row["mission_id"] for row in private_plans] == private_ids
    assert [row["mission_id"] for row in private_certificates] == private_ids
    peak_plan = private_plans[0]
    peak_certificate = private_certificates[0]
    assert [row["action_id"] for row in peak_plan["slots"]] == [
        "hot",
        "idle",
        "anchor",
        "final",
    ]
    assert peak_certificate["objective_prefix"] == [0, 0, 0, 4, 10, 3]
    for private_id, public_id in private_cases:
        if public_id is None:
            continue
        private_ticket = read_json(
            private_input / "missions" / private_id / "mission.json"
        )
        public_ticket = read_json(
            INPUT / public_entries[public_id]["directory"] / "mission.json"
        )
        assert private_ticket["mission_id"] == private_id
        assert private_ticket["battery"]["capacity_units"] > public_ticket[
            "battery"
        ]["capacity_units"]

    completed = invoke_submitted_solver(
        SOLVER_PATH,
        private_input,
        private_output,
        tmp_path,
    )
    assert completed.returncode == 0, (completed.stdout[-2000:], completed.stderr[-2000:])
    audit_output_directory(private_output, private_input)
