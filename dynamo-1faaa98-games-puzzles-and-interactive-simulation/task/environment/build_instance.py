#!/usr/bin/env python3
"""Build deterministic store-and-forward satellite mission fixtures."""
from __future__ import annotations

import argparse
import csv
import json
import random
from pathlib import Path


MISSION_CONFIGS = (
    {
        "mission_id": "aegis-polar-17",
        "seed": 1703,
        "horizon": 15,
        "storage": 27,
        "battery": (88, 57, 12, 1),
        "thermal": (4, 31, 3, 1),
        "slew": (3, 1),
    },
    {
        "mission_id": "calypso-radar-04",
        "seed": 4119,
        "horizon": 16,
        "storage": 25,
        "battery": (82, 52, 11, 1),
        "thermal": (6, 29, 2, 1),
        "slew": (3, 1),
    },
    {
        "mission_id": "helix-weather-29",
        "seed": 2917,
        "horizon": 14,
        "storage": 23,
        "battery": (79, 49, 10, 1),
        "thermal": (3, 27, 3, 2),
        "slew": (4, 1),
    },
    {
        "mission_id": "janus-cubesat-12",
        "seed": 1229,
        "horizon": 17,
        "storage": 29,
        "battery": (94, 60, 14, 1),
        "thermal": (5, 33, 3, 1),
        "slew": (3, 2),
    },
    {
        "mission_id": "meridian-ocean-08",
        "seed": 8053,
        "horizon": 15,
        "storage": 24,
        "battery": (85, 54, 12, 1),
        "thermal": (7, 30, 2, 1),
        "slew": (4, 1),
    },
)

CLASSES = (
    {"class_id": "command", "loss_weight": 19},
    {"class_id": "science", "loss_weight": 7},
    {"class_id": "engineering", "loss_weight": 3},
)

SCENARIOS = (
    {"scenario_id": "storm", "capacity_numerator": 1, "capacity_denominator": 3},
    {"scenario_id": "degraded", "capacity_numerator": 2, "capacity_denominator": 3},
    {"scenario_id": "nominal", "capacity_numerator": 1, "capacity_denominator": 1},
    {"scenario_id": "clear", "capacity_numerator": 4, "capacity_denominator": 3},
)

STATIONS = (
    {"station_id": "svalbard", "pointing_step": -4},
    {"station_id": "alaska", "pointing_step": -1},
    {"station_id": "kiruna", "pointing_step": 2},
    {"station_id": "troll", "pointing_step": 4},
)


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


def write_csv(path: Path, fields: list[str], rows: list[dict[str, object]]) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def build_timeline(config: dict[str, object]) -> list[dict[str, int]]:
    seed = int(config["seed"])
    horizon = int(config["horizon"])
    rows = []
    for slot in range(horizon):
        phase = (slot + seed) % 11
        if phase <= 5:
            solar = 6 + ((slot * 5 + seed) % 5)
        elif phase <= 8:
            solar = 2 + ((slot + seed) % 3)
        else:
            solar = (slot + seed) % 2
        rows.append({"slot": slot, "solar_units": solar})
    return rows


def build_packets(config: dict[str, object]) -> list[dict[str, object]]:
    seed = int(config["seed"])
    horizon = int(config["horizon"])
    rng = random.Random(seed)
    rows: list[dict[str, object]] = []
    release = 0
    index = 0
    while release <= horizon - 3:
        selector = (index * 7 + seed) % 10
        if selector in {0, 5}:
            class_id, lifetime = "command", 3 + (index % 2)
        elif selector in {1, 2, 6, 8}:
            class_id, lifetime = "science", 5 + (index % 3)
        else:
            class_id, lifetime = "engineering", 4 + ((index + 1) % 3)
        count = 2 + rng.randrange(6)
        rows.append(
            {
                "batch_id": f"B{index:03d}",
                "release_slot": release,
                "deadline_slot": min(horizon - 1, release + lifetime),
                "class_id": class_id,
                "packet_count": count,
            }
        )
        index += 1
        release += 1 if (index + seed) % 4 else 2

    # Late command and science bursts make terminal choices consequential.
    for class_id, offset, count in (
        ("command", 5, 4 + seed % 3),
        ("science", 4, 6 + seed % 4),
    ):
        rows.append(
            {
                "batch_id": f"B{index:03d}",
                "release_slot": horizon - offset,
                "deadline_slot": horizon - 1,
                "class_id": class_id,
                "packet_count": count,
            }
        )
        index += 1
    return sorted(rows, key=lambda row: (int(row["release_slot"]), str(row["batch_id"])))


def build_contacts(config: dict[str, object]) -> list[dict[str, object]]:
    seed = int(config["seed"])
    horizon = int(config["horizon"])
    rng = random.Random(seed ^ 0x5A17)
    rows: list[dict[str, object]] = []
    for slot in range(horizon):
        if (slot * 3 + seed) % 7 == 0:
            continue
        primary = (slot * 5 + seed) % len(STATIONS)
        station_indices = [primary]
        if (slot + seed) % 5 == 1 or (slot * 2 + seed) % 11 == 3:
            station_indices.append((primary + 2 + slot % 2) % len(STATIONS))
        for choice, station_index in enumerate(dict.fromkeys(station_indices)):
            station = STATIONS[station_index]
            capacity = 4 + rng.randrange(6) + (1 if choice else 0)
            energy = 3 + rng.randrange(5) + choice
            heat = 2 + rng.randrange(5) + choice
            rows.append(
                {
                    "action_id": f"C{slot:02d}{choice}-{station['station_id']}",
                    "slot": slot,
                    "station_id": station["station_id"],
                    "pointing_step": station["pointing_step"],
                    "nominal_capacity_packets": capacity,
                    "energy_units": energy,
                    "heat_units": heat,
                }
            )
    return sorted(rows, key=lambda row: (int(row["slot"]), str(row["action_id"])))


def build_mission(input_root: Path, config: dict[str, object]) -> dict[str, str]:
    mission_id = str(config["mission_id"])
    mission_root = input_root / "missions" / mission_id
    mission_root.mkdir(parents=True)
    battery_capacity, battery_initial, reserve, idle_cost = config["battery"]
    thermal_initial, thermal_limit, cooling, slew_heat = config["thermal"]
    max_slew, slew_energy = config["slew"]
    mission = {
        "schema_version": 1,
        "mission_id": mission_id,
        "horizon_slots": config["horizon"],
        "storage_capacity_packets": config["storage"],
        "battery": {
            "capacity_units": battery_capacity,
            "initial_units": battery_initial,
            "reserve_units": reserve,
            "idle_cost_units": idle_cost,
        },
        "thermal": {
            "initial_units": thermal_initial,
            "limit_units": thermal_limit,
            "passive_cooling_units": cooling,
            "slew_heat_per_step": slew_heat,
        },
        "slew": {
            "max_steps_per_slot": max_slew,
            "energy_per_step": slew_energy,
        },
        "classes": list(CLASSES),
        "scenarios": list(SCENARIOS),
        "nominal_scenario_id": "nominal",
        "stations": list(STATIONS),
    }
    write_json(mission_root / "mission.json", mission)
    write_csv(
        mission_root / "timeline.csv",
        ["slot", "solar_units"],
        build_timeline(config),
    )
    write_csv(
        mission_root / "packets.csv",
        ["batch_id", "release_slot", "deadline_slot", "class_id", "packet_count"],
        build_packets(config),
    )
    write_csv(
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
        build_contacts(config),
    )
    return {"mission_id": mission_id, "directory": f"missions/{mission_id}"}


def build(root: Path) -> None:
    input_root = root / "input"
    input_root.mkdir(parents=True, exist_ok=True)
    missions = [build_mission(input_root, config) for config in MISSION_CONFIGS]
    write_json(input_root / "manifest.json", {"schema_version": 1, "missions": missions})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("/app"))
    build(parser.parse_args().root)
