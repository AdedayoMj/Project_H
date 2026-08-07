#!/usr/bin/env python3
"""Exact robust satellite downlink planner."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path


DEFAULT_INPUT = Path("/app/input")
DEFAULT_OUTPUT = Path("/app/output")
SLOT_FIELDS = [
    "mission_id",
    "slot",
    "action_id",
    "station_id",
    "pointing_step",
    "slew_steps",
    "energy_used_units",
    "battery_units",
    "thermal_units",
]


@dataclass(frozen=True)
class Batch:
    batch_id: str
    release: int
    deadline: int
    class_index: int
    count: int


@dataclass(frozen=True)
class Contact:
    action_id: str
    slot: int
    station_id: str
    pointing: int
    capacity: int
    energy: int
    heat: int


@dataclass(frozen=True)
class State:
    battery: int
    thermal: int
    last_pointing: int
    last_contact_slot: int
    queues: tuple[int, ...]
    losses: tuple[int, ...]


@dataclass(frozen=True)
class Prefix:
    energy: int
    peak_thermal: int
    contacts: int
    ranks: tuple[int, ...]
    actions: tuple[str, ...]

    def comparison(self) -> tuple[object, ...]:
        return self.energy, self.peak_thermal, self.contacts, self.ranks


@dataclass
class Mission:
    mission_id: str
    horizon: int
    storage: int
    battery_capacity: int
    battery_initial: int
    battery_reserve: int
    idle_cost: int
    thermal_initial: int
    thermal_limit: int
    cooling: int
    slew_heat: int
    max_slew: int
    slew_energy: int
    class_ids: list[str]
    class_weights: list[int]
    scenario_ids: list[str]
    scenario_fractions: list[tuple[int, int]]
    nominal_scenario: int
    solar: list[int]
    batches: list[Batch]
    contacts_by_slot: list[list[Contact]]


def read_json(path: Path) -> object:
    return json.loads(path.read_text())


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def resolve_directory(input_root: Path, directory: str) -> Path:
    path = Path(directory)
    return path if path.is_absolute() else input_root / path


def load_mission(input_root: Path, entry: dict[str, object]) -> Mission:
    root = resolve_directory(input_root, str(entry["directory"]))
    spec = read_json(root / "mission.json")
    assert isinstance(spec, dict)
    class_ids = [str(row["class_id"]) for row in spec["classes"]]
    class_weights = [int(row["loss_weight"]) for row in spec["classes"]]
    class_lookup = {class_id: index for index, class_id in enumerate(class_ids)}
    scenario_ids = [str(row["scenario_id"]) for row in spec["scenarios"]]
    scenario_fractions = [
        (int(row["capacity_numerator"]), int(row["capacity_denominator"]))
        for row in spec["scenarios"]
    ]
    timeline = read_csv(root / "timeline.csv")
    solar = [int(row["solar_units"]) for row in timeline]
    batches = [
        Batch(
            batch_id=row["batch_id"],
            release=int(row["release_slot"]),
            deadline=int(row["deadline_slot"]),
            class_index=class_lookup[row["class_id"]],
            count=int(row["packet_count"]),
        )
        for row in read_csv(root / "packets.csv")
    ]
    horizon = int(spec["horizon_slots"])
    contacts_by_slot: list[list[Contact]] = [[] for _ in range(horizon)]
    for row in read_csv(root / "contacts.csv"):
        contact = Contact(
            action_id=row["action_id"],
            slot=int(row["slot"]),
            station_id=row["station_id"],
            pointing=int(row["pointing_step"]),
            capacity=int(row["nominal_capacity_packets"]),
            energy=int(row["energy_units"]),
            heat=int(row["heat_units"]),
        )
        contacts_by_slot[contact.slot].append(contact)
    for contacts in contacts_by_slot:
        contacts.sort(key=lambda contact: contact.action_id)
    battery = spec["battery"]
    thermal = spec["thermal"]
    slew = spec["slew"]
    return Mission(
        mission_id=str(spec["mission_id"]),
        horizon=horizon,
        storage=int(spec["storage_capacity_packets"]),
        battery_capacity=int(battery["capacity_units"]),
        battery_initial=int(battery["initial_units"]),
        battery_reserve=int(battery["reserve_units"]),
        idle_cost=int(battery["idle_cost_units"]),
        thermal_initial=int(thermal["initial_units"]),
        thermal_limit=int(thermal["limit_units"]),
        cooling=int(thermal["passive_cooling_units"]),
        slew_heat=int(thermal["slew_heat_per_step"]),
        max_slew=int(slew["max_steps_per_slot"]),
        slew_energy=int(slew["energy_per_step"]),
        class_ids=class_ids,
        class_weights=class_weights,
        scenario_ids=scenario_ids,
        scenario_fractions=scenario_fractions,
        nominal_scenario=scenario_ids.index(str(spec["nominal_scenario_id"])),
        solar=solar,
        batches=batches,
        contacts_by_slot=contacts_by_slot,
    )


def queue_offset(mission: Mission, scenario: int, batch: int) -> int:
    return scenario * len(mission.batches) + batch


def loss_offset(mission: Mission, scenario: int, class_index: int) -> int:
    return scenario * len(mission.class_ids) + class_index


def overflow_order(mission: Mission, slot: int) -> list[int]:
    active = [
        index
        for index, batch in enumerate(mission.batches)
        if batch.release <= slot <= batch.deadline
    ]
    active.sort(key=lambda index: mission.batches[index].batch_id, reverse=True)
    active.sort(key=lambda index: mission.batches[index].deadline, reverse=True)
    active.sort(key=lambda index: mission.class_weights[mission.batches[index].class_index])
    return active


def service_order(mission: Mission, slot: int) -> list[int]:
    active = [
        index
        for index, batch in enumerate(mission.batches)
        if batch.release <= slot <= batch.deadline
    ]
    return sorted(
        active,
        key=lambda index: (
            -mission.class_weights[mission.batches[index].class_index],
            mission.batches[index].deadline,
            mission.batches[index].batch_id,
        ),
    )


def prepare_slot(mission: Mission, state: State, slot: int) -> State:
    queues = list(state.queues)
    losses = list(state.losses)
    discard_order = overflow_order(mission, slot)
    for scenario in range(len(mission.scenario_ids)):
        stored = sum(queues[queue_offset(mission, scenario, batch)] for batch in discard_order)
        excess = max(0, stored - mission.storage)
        for batch_index in discard_order:
            if excess == 0:
                break
            offset = queue_offset(mission, scenario, batch_index)
            discarded = min(excess, queues[offset])
            queues[offset] -= discarded
            batch = mission.batches[batch_index]
            losses[loss_offset(mission, scenario, batch.class_index)] += discarded
            excess -= discarded
    return State(
        battery=min(mission.battery_capacity, state.battery + mission.solar[slot]),
        thermal=max(0, state.thermal - mission.cooling),
        last_pointing=state.last_pointing,
        last_contact_slot=state.last_contact_slot,
        queues=tuple(queues),
        losses=tuple(losses),
    )


def apply_action(
    mission: Mission,
    prepared: State,
    slot: int,
    contact: Contact | None,
) -> tuple[State, int, int] | None:
    if contact is None:
        slew_steps = 0
        energy = mission.idle_cost
        heat = 0
        last_pointing = prepared.last_pointing
        last_contact_slot = prepared.last_contact_slot
    else:
        slew_steps = (
            0
            if prepared.last_contact_slot < 0
            else abs(contact.pointing - prepared.last_pointing)
        )
        elapsed = slot - prepared.last_contact_slot
        if prepared.last_contact_slot >= 0 and slew_steps > mission.max_slew * elapsed:
            return None
        energy = mission.idle_cost + contact.energy + mission.slew_energy * slew_steps
        heat = contact.heat + mission.slew_heat * slew_steps
        last_pointing = contact.pointing
        last_contact_slot = slot
    battery = prepared.battery - energy
    thermal = prepared.thermal + heat
    if battery < mission.battery_reserve or thermal > mission.thermal_limit:
        return None

    queues = list(prepared.queues)
    losses = list(prepared.losses)
    order = service_order(mission, slot)
    for scenario, (numerator, denominator) in enumerate(mission.scenario_fractions):
        capacity = 0 if contact is None else contact.capacity * numerator // denominator
        for batch_index in order:
            if capacity == 0:
                break
            offset = queue_offset(mission, scenario, batch_index)
            sent = min(capacity, queues[offset])
            queues[offset] -= sent
            capacity -= sent
        for batch_index, batch in enumerate(mission.batches):
            if batch.deadline != slot:
                continue
            offset = queue_offset(mission, scenario, batch_index)
            expired = queues[offset]
            if expired:
                queues[offset] = 0
                losses[loss_offset(mission, scenario, batch.class_index)] += expired
    return (
        State(
            battery=battery,
            thermal=thermal,
            last_pointing=last_pointing,
            last_contact_slot=last_contact_slot,
            queues=tuple(queues),
            losses=tuple(losses),
        ),
        energy,
        slew_steps,
    )


def initial_state(mission: Mission) -> State:
    queue = tuple(
        batch.count
        for _scenario in mission.scenario_ids
        for batch in mission.batches
    )
    return State(
        battery=mission.battery_initial,
        thermal=mission.thermal_initial,
        last_pointing=0,
        last_contact_slot=-1,
        queues=queue,
        losses=(0,) * (len(mission.scenario_ids) * len(mission.class_ids)),
    )


def outcomes(mission: Mission, state: State) -> list[dict[str, object]]:
    totals = [0] * len(mission.class_ids)
    for batch in mission.batches:
        totals[batch.class_index] += batch.count
    result = []
    for scenario, scenario_id in enumerate(mission.scenario_ids):
        lost = {
            class_id: state.losses[loss_offset(mission, scenario, class_index)]
            for class_index, class_id in enumerate(mission.class_ids)
        }
        delivered = {
            class_id: totals[class_index] - lost[class_id]
            for class_index, class_id in enumerate(mission.class_ids)
        }
        weighted_loss = sum(
            lost[class_id] * mission.class_weights[class_index]
            for class_index, class_id in enumerate(mission.class_ids)
        )
        result.append(
            {
                "scenario_id": scenario_id,
                "weighted_loss": weighted_loss,
                "delivered_packets": sum(delivered.values()),
                "lost_packets_by_class": lost,
                "delivered_packets_by_class": delivered,
            }
        )
    return result


def optimize(mission: Mission) -> tuple[tuple[str, ...], tuple[int, ...]]:
    states: dict[State, Prefix] = {
        initial_state(mission): Prefix(0, 0, 0, (), ())
    }
    for slot in range(mission.horizon):
        next_states: dict[State, Prefix] = {}
        choices: list[Contact | None] = [None, *mission.contacts_by_slot[slot]]
        for state, prefix in states.items():
            prepared = prepare_slot(mission, state, slot)
            for rank, contact in enumerate(choices):
                transition = apply_action(mission, prepared, slot, contact)
                if transition is None:
                    continue
                next_state, energy, _slew = transition
                candidate = Prefix(
                    energy=prefix.energy + energy,
                    peak_thermal=max(prefix.peak_thermal, next_state.thermal),
                    contacts=prefix.contacts + (contact is not None),
                    ranks=prefix.ranks + (rank,),
                    actions=prefix.actions + ((contact.action_id if contact else "idle"),),
                )
                incumbent = next_states.get(next_state)
                if incumbent is None or candidate.comparison() < incumbent.comparison():
                    next_states[next_state] = candidate
        if not next_states:
            raise RuntimeError(f"mission {mission.mission_id} has no feasible plan at slot {slot}")
        states = next_states

    best_key: tuple[object, ...] | None = None
    best_actions: tuple[str, ...] | None = None
    best_prefix: tuple[int, ...] | None = None
    for state, prefix in states.items():
        scenario_outcomes = outcomes(mission, state)
        weighted = [int(row["weighted_loss"]) for row in scenario_outcomes]
        objective = (
            max(weighted),
            sum(weighted),
            weighted[mission.nominal_scenario],
            prefix.energy,
            prefix.peak_thermal,
            prefix.contacts,
        )
        key: tuple[object, ...] = (*objective, prefix.ranks)
        if best_key is None or key < best_key:
            best_key = key
            best_actions = prefix.actions
            best_prefix = objective
    assert best_actions is not None and best_prefix is not None
    return best_actions, best_prefix


def replay(
    mission: Mission,
    action_ids: tuple[str, ...],
) -> tuple[list[dict[str, object]], State, int, int, int, int]:
    state = initial_state(mission)
    records: list[dict[str, object]] = []
    total_energy = 0
    minimum_battery = mission.battery_capacity
    peak_thermal = 0
    contact_count = 0
    for slot, action_id in enumerate(action_ids):
        by_id = {contact.action_id: contact for contact in mission.contacts_by_slot[slot]}
        contact = None if action_id == "idle" else by_id[action_id]
        prepared = prepare_slot(mission, state, slot)
        transition = apply_action(mission, prepared, slot, contact)
        if transition is None:
            raise RuntimeError("optimizer emitted an infeasible action")
        state, energy, slew_steps = transition
        total_energy += energy
        minimum_battery = min(minimum_battery, state.battery)
        peak_thermal = max(peak_thermal, state.thermal)
        contact_count += contact is not None
        records.append(
            {
                "mission_id": mission.mission_id,
                "slot": slot,
                "action_id": action_id,
                "station_id": contact.station_id if contact else "none",
                "pointing_step": contact.pointing if contact else 0,
                "slew_steps": slew_steps,
                "energy_used_units": energy,
                "battery_units": state.battery,
                "thermal_units": state.thermal,
            }
        )
    return records, state, total_energy, minimum_battery, peak_thermal, contact_count


def solve_mission(mission: Mission) -> tuple[dict[str, object], dict[str, object]]:
    actions, objective = optimize(mission)
    records, final_state, energy, minimum_battery, peak, contacts = replay(mission, actions)
    scenario_outcomes = outcomes(mission, final_state)
    digest = hashlib.sha256(("\n".join(actions) + "\n").encode()).hexdigest()
    plan = {
        "mission_id": mission.mission_id,
        "slots": records,
        "outcomes": scenario_outcomes,
    }
    certificate = {
        "mission_id": mission.mission_id,
        "objective_prefix": list(objective),
        "total_energy_units": energy,
        "minimum_battery_units": minimum_battery,
        "peak_thermal_units": peak,
        "contact_count": contacts,
        "plan_sha256": digest,
        "outcomes": scenario_outcomes,
    }
    return plan, certificate


def main(input_root: Path = DEFAULT_INPUT, output_root: Path = DEFAULT_OUTPUT) -> None:
    manifest = read_json(input_root / "manifest.json")
    assert isinstance(manifest, dict)
    plans = []
    certificates = []
    flat_records = []
    for entry in manifest["missions"]:
        mission = load_mission(input_root, entry)
        plan, certificate = solve_mission(mission)
        plans.append(plan)
        certificates.append(certificate)
        flat_records.extend(plan["slots"])

    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "downlink-plan.json").write_text(
        json.dumps({"schema_version": 1, "missions": plans}, separators=(",", ":"))
        + "\n"
    )
    with (output_root / "downlink-plan.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SLOT_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(flat_records)
    (output_root / "robustness-certificate.json").write_text(
        json.dumps(
            {"schema_version": 1, "missions": certificates},
            separators=(",", ":"),
        )
        + "\n"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-root", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT)
    arguments = parser.parse_args()
    main(arguments.input_root, arguments.output_root)
