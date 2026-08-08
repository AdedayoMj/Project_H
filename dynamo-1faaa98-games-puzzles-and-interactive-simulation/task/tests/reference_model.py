"""Independent exact model for the robust downlink task."""
from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path


SCENARIO_IDS = ("storm", "degraded", "nominal", "clear")
CLASS_IDS = ("command", "science", "engineering")
SLOT_FIELDS = (
    "mission_id",
    "slot",
    "action_id",
    "station_id",
    "pointing_step",
    "slew_steps",
    "energy_used_units",
    "battery_units",
    "thermal_units",
)


@dataclass(frozen=True)
class Label:
    energy: int
    peak: int
    contact_count: int
    ranks: tuple[int, ...]
    actions: tuple[str, ...]


def json_file(path: Path):
    return json.loads(path.read_text())


def csv_rows(path: Path):
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def mission_directory(input_root: Path, entry: dict) -> Path:
    declared = Path(entry["directory"])
    return declared if declared.is_absolute() else input_root / declared


def parse_mission(input_root: Path, entry: dict) -> dict:
    root = mission_directory(input_root, entry)
    ticket = json_file(root / "mission.json")
    classes = ticket["classes"]
    class_ids = [row["class_id"] for row in classes]
    class_index = {class_id: index for index, class_id in enumerate(class_ids)}
    batches = []
    for row in csv_rows(root / "packets.csv"):
        batches.append(
            {
                "id": row["batch_id"],
                "release": int(row["release_slot"]),
                "deadline": int(row["deadline_slot"]),
                "class": class_index[row["class_id"]],
                "count": int(row["packet_count"]),
            }
        )
    contacts = [[] for _ in range(ticket["horizon_slots"])]
    for row in csv_rows(root / "contacts.csv"):
        action = {
            "id": row["action_id"],
            "station": row["station_id"],
            "slot": int(row["slot"]),
            "point": int(row["pointing_step"]),
            "capacity": int(row["nominal_capacity_packets"]),
            "energy": int(row["energy_units"]),
            "heat": int(row["heat_units"]),
        }
        contacts[action["slot"]].append(action)
    for choices in contacts:
        choices.sort(key=lambda action: action["id"])
    return {
        "id": ticket["mission_id"],
        "horizon": ticket["horizon_slots"],
        "storage": ticket["storage_capacity_packets"],
        "battery": ticket["battery"],
        "thermal": ticket["thermal"],
        "slew": ticket["slew"],
        "class_ids": class_ids,
        "weights": [row["loss_weight"] for row in classes],
        "scenario_ids": [row["scenario_id"] for row in ticket["scenarios"]],
        "fractions": [
            (row["capacity_numerator"], row["capacity_denominator"])
            for row in ticket["scenarios"]
        ],
        "nominal": [row["scenario_id"] for row in ticket["scenarios"]].index(
            ticket["nominal_scenario_id"]
        ),
        "solar": [int(row["solar_units"]) for row in csv_rows(root / "timeline.csv")],
        "batches": batches,
        "contacts": contacts,
    }


def qindex(world: dict, scenario: int, batch: int) -> int:
    return scenario * len(world["batches"]) + batch


def lindex(world: dict, scenario: int, class_index: int) -> int:
    return scenario * len(world["class_ids"]) + class_index


def state_at_start(world: dict) -> tuple:
    initial_queues = tuple(
        batch["count"]
        for _scenario in world["scenario_ids"]
        for batch in world["batches"]
    )
    return (
        world["battery"]["initial_units"],
        world["thermal"]["initial_units"],
        0,
        -1,
        initial_queues,
        (0,) * (len(world["scenario_ids"]) * len(world["class_ids"])),
    )


def drop_sequence(world: dict, slot: int) -> list[int]:
    indices = [
        index
        for index, batch in enumerate(world["batches"])
        if batch["release"] <= slot <= batch["deadline"]
    ]
    indices.sort(key=lambda index: world["batches"][index]["id"], reverse=True)
    indices.sort(key=lambda index: world["batches"][index]["deadline"], reverse=True)
    indices.sort(key=lambda index: world["weights"][world["batches"][index]["class"]])
    return indices


def transmit_sequence(world: dict, slot: int) -> list[int]:
    return sorted(
        (
            index
            for index, batch in enumerate(world["batches"])
            if batch["release"] <= slot <= batch["deadline"]
        ),
        key=lambda index: (
            -world["weights"][world["batches"][index]["class"]],
            world["batches"][index]["deadline"],
            world["batches"][index]["id"],
        ),
    )


def advance(world: dict, state: tuple, slot: int, action: dict | None):
    battery, temperature, old_point, old_slot, queue_tuple, loss_tuple = state
    queues = list(queue_tuple)
    losses = list(loss_tuple)

    overflow_order = drop_sequence(world, slot)
    for scenario in range(len(world["scenario_ids"])):
        excess = max(
            0,
            sum(queues[qindex(world, scenario, batch)] for batch in overflow_order)
            - world["storage"],
        )
        for batch_index in overflow_order:
            if not excess:
                break
            position = qindex(world, scenario, batch_index)
            amount = min(excess, queues[position])
            queues[position] -= amount
            class_index = world["batches"][batch_index]["class"]
            losses[lindex(world, scenario, class_index)] += amount
            excess -= amount

    battery = min(world["battery"]["capacity_units"], battery + world["solar"][slot])
    temperature = max(0, temperature - world["thermal"]["passive_cooling_units"])
    if action is None:
        slew = 0
        energy = world["battery"]["idle_cost_units"]
        heat = 0
        new_point, new_slot = old_point, old_slot
    else:
        slew = 0 if old_slot < 0 else abs(action["point"] - old_point)
        if old_slot >= 0 and slew > world["slew"]["max_steps_per_slot"] * (slot - old_slot):
            return None
        energy = (
            world["battery"]["idle_cost_units"]
            + action["energy"]
            + world["slew"]["energy_per_step"] * slew
        )
        heat = action["heat"] + world["thermal"]["slew_heat_per_step"] * slew
        new_point, new_slot = action["point"], slot
    battery -= energy
    temperature += heat
    if battery < world["battery"]["reserve_units"]:
        return None
    if temperature > world["thermal"]["limit_units"]:
        return None

    service_order = transmit_sequence(world, slot)
    for scenario, (numerator, denominator) in enumerate(world["fractions"]):
        room = 0 if action is None else action["capacity"] * numerator // denominator
        for batch_index in service_order:
            if room <= 0:
                break
            position = qindex(world, scenario, batch_index)
            amount = min(room, queues[position])
            queues[position] -= amount
            room -= amount
        for batch_index, batch in enumerate(world["batches"]):
            if batch["deadline"] == slot:
                position = qindex(world, scenario, batch_index)
                losses[lindex(world, scenario, batch["class"])] += queues[position]
                queues[position] = 0
    return (
        (battery, temperature, new_point, new_slot, tuple(queues), tuple(losses)),
        energy,
        slew,
    )


def summarize(world: dict, state: tuple) -> list[dict]:
    losses = state[5]
    totals = [0] * len(world["class_ids"])
    for batch in world["batches"]:
        totals[batch["class"]] += batch["count"]
    summaries = []
    for scenario, scenario_id in enumerate(world["scenario_ids"]):
        lost_map = {
            class_id: losses[lindex(world, scenario, class_index)]
            for class_index, class_id in enumerate(world["class_ids"])
        }
        delivered_map = {
            class_id: totals[class_index] - lost_map[class_id]
            for class_index, class_id in enumerate(world["class_ids"])
        }
        summaries.append(
            {
                "scenario_id": scenario_id,
                "weighted_loss": sum(
                    lost_map[class_id] * world["weights"][class_index]
                    for class_index, class_id in enumerate(world["class_ids"])
                ),
                "delivered_packets": sum(delivered_map.values()),
                "lost_packets_by_class": lost_map,
                "delivered_packets_by_class": delivered_map,
            }
        )
    return summaries


def exact_plan(world: dict) -> tuple[tuple[str, ...], tuple[int, ...]]:
    layer = {(state_at_start(world), 0): Label(0, 0, 0, (), ())}
    for slot in range(world["horizon"]):
        following = {}
        options = [None, *world["contacts"][slot]]
        for (state, _peak), label in layer.items():
            for rank, action in enumerate(options):
                result = advance(world, state, slot, action)
                if result is None:
                    continue
                next_state, energy, _slew = result
                candidate = Label(
                    energy=label.energy + energy,
                    peak=max(label.peak, next_state[1]),
                    contact_count=label.contact_count + (action is not None),
                    ranks=label.ranks + (rank,),
                    actions=label.actions + ((action["id"] if action else "idle"),),
                )
                # Peak is max-type: later heat can tie distinct historical peaks,
                # after which contact count and ranks still decide the winner.
                merge_state = next_state, candidate.peak
                previous = following.get(merge_state)
                candidate_prefix = (
                    candidate.energy,
                    candidate.contact_count,
                    candidate.ranks,
                )
                if previous is None or candidate_prefix < (
                    previous.energy,
                    previous.contact_count,
                    previous.ranks,
                ):
                    following[merge_state] = candidate
        layer = following

    winner = None
    for (state, _peak), label in layer.items():
        scenario_losses = [row["weighted_loss"] for row in summarize(world, state)]
        objective = (
            max(scenario_losses),
            sum(scenario_losses),
            scenario_losses[world["nominal"]],
            label.energy,
            label.peak,
            label.contact_count,
        )
        comparison = (*objective, label.ranks)
        if winner is None or comparison < winner[0]:
            winner = comparison, label.actions, objective
    assert winner is not None
    return winner[1], winner[2]


def replay_plan(world: dict, actions: tuple[str, ...], objective: tuple[int, ...]):
    state = state_at_start(world)
    slots = []
    energy_total = 0
    minimum_battery = world["battery"]["capacity_units"]
    peak = 0
    contacts_used = 0
    for slot, action_id in enumerate(actions):
        lookup = {action["id"]: action for action in world["contacts"][slot]}
        action = None if action_id == "idle" else lookup[action_id]
        result = advance(world, state, slot, action)
        assert result is not None
        state, energy, slew = result
        energy_total += energy
        minimum_battery = min(minimum_battery, state[0])
        peak = max(peak, state[1])
        contacts_used += action is not None
        slots.append(
            {
                "mission_id": world["id"],
                "slot": slot,
                "action_id": action_id,
                "station_id": action["station"] if action else "none",
                "pointing_step": action["point"] if action else 0,
                "slew_steps": slew,
                "energy_used_units": energy,
                "battery_units": state[0],
                "thermal_units": state[1],
            }
        )
    mission_outcomes = summarize(world, state)
    digest = hashlib.sha256(("\n".join(actions) + "\n").encode()).hexdigest()
    plan = {"mission_id": world["id"], "slots": slots, "outcomes": mission_outcomes}
    certificate = {
        "mission_id": world["id"],
        "objective_prefix": list(objective),
        "total_energy_units": energy_total,
        "minimum_battery_units": minimum_battery,
        "peak_thermal_units": peak,
        "contact_count": contacts_used,
        "plan_sha256": digest,
        "outcomes": mission_outcomes,
    }
    return plan, certificate


def calculate(input_root: Path):
    manifest = json_file(input_root / "manifest.json")
    plans = []
    certificates = []
    for entry in manifest["missions"]:
        world = parse_mission(input_root, entry)
        actions, objective = exact_plan(world)
        plan, certificate = replay_plan(world, actions, objective)
        plans.append(plan)
        certificates.append(certificate)
    return plans, certificates
