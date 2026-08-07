Produce a flight-operations decision tape for every mission listed in `/app/input/manifest.json`. A tape chooses `idle` or one visible ground-station contact in every slot. It is fixed before the link-quality world is known: the same choices, battery trajectory, thermal trajectory, and antenna history must be used in all four ordered capacity scenarios, while packet queues, deliveries, and losses evolve separately in each scenario.

This is a robust store-and-forward planning problem, not four independent schedules. Command, science, and engineering batches enter finite storage, expire after their deadlines, and compete under weighted overflow and transmission priorities. Solar input can saturate the battery; contacts consume energy and add heat; passive cooling acts between slots; and contact feasibility depends on the time available to slew from the last non-idle pointing. An idle slot retains that pointing history. Apply releases, overflow, charging/cooling, action effects, packet service, and deadline loss in the exact order defined by `/app/input/specification.md`. That file is normative for all input and output schemas, integer transitions, ordering rules, feasibility conditions, and tie-breaking.

For each mission, minimize the prescribed robust comparison key: worst scenario weighted loss, summed scenario weighted loss, nominal-scenario weighted loss, total energy, peak thermal state, contact count, and finally the complete action-rank sequence. The last component selects one deterministic plan. A plan that is feasible only in the nominal capacity world, or that uses a different contact sequence for different scenarios, is invalid.

Write four artifacts under `/app/output/`:

- `downlink-plan.json` contains every slot decision, shared spacecraft state, and exact scenario outcome.
- `downlink-plan.csv` is the lossless manifest-order flat view of those slot records.
- `robustness-certificate.json` repeats the six numerical objective components, resource extrema, contact count, scenario results, and canonical action digest.
- `solver.py` is the reusable program that generated the other artifacts.

The reusable program must accept `--input-root INPUT_DIRECTORY --output-root OUTPUT_DIRECTORY`. It is evaluated on an unseen schema-compatible mission whose packet calendar, solar profile, contact capacities and costs, station pointings, and spacecraft limits differ from the published evidence. That run cannot read verifier files, start child processes, or use the network.

All submitted schemas, mission/slot/scenario/class orders, actions, resource states, ledgers, objective values, and digests are exact integers or exact strings as specified. Greedy earliest-contact service, copied published outputs, incorrect overflow/service priority, altered transition order, or a merely feasible but non-optimal tape does not satisfy the task.
