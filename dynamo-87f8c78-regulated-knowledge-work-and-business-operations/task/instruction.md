You are the executive assistant responsible for finalizing next week's operating schedule and inbox responses. The business's raw operations data is under `/app/input/`:

`/app/input/calendars/executive.json`, `/app/input/calendars/operations.json`, `/app/input/calendars/travel.json` each list recurring and one-off calendar events (fields include `event_id`, `title`, `start_local`, `timezone`, `duration_minutes`, `venue`, `rrule`, `exdates`, `movable`, `candidate_slots`, `dependencies`, `priority`, `protected`, `vip_override`, `after_hours_ok`).

`/app/input/travel_matrix.csv` gives one-way transit minutes between every pair of venues.

`/app/input/policy_notes.json` gives the planning window, the after-hours cutoff, the exact objective to optimize (`objective_order`, spelled out precisely in `objective_definitions`), the exact scheduling constraints (`scheduling_rules`, covering conflicts/travel gaps, protected blocks, VIP overrides, after-hours eligibility, movable-vs-fixed placement, dependency ordering, and the tie-break rule), and the exact deterministic message-classification procedure (`reply_rule_order` and `reply_rule_definitions`).

`/app/input/inbox/messages.json` lists inbox messages, each carrying the boolean fields (`missing_required_details`, `requires_escalation`, `no_legal_alternative`, `conflict`, `alternate_available`) that `reply_rule_definitions` is written against.

Every recurring event must first be expanded into its concrete occurrences inside the planning window given in `policy_notes.json`, honoring `rrule` (`freq`, `byweekday`, `count` or `until`) and `exdates`, with each occurrence's wall-clock time converted to UTC using its own `timezone`. Identify each occurrence as `<event_id>@<original start_local>`, e.g. `mov-west-brief@2026-03-09T10:10:00`, using its original (undisturbed) `start_local` even if the occurrence ends up moved elsewhere.

Apply every rule in `scheduling_rules` together — they interact (a protected block can force a VIP-flagged event to relocate, which can in turn make a completely different event's only remaining option illegal) — to decide, for every occurrence, whether it appears in the final schedule at its original placement, at one of its `candidate_slots` (only if `movable` is true), or is left out entirely. Then pick the one full assignment that is optimal under `objective_order`, using `objective_definitions` and the `tie_break_rule` exactly as written.

Classify every message in `/app/input/inbox/messages.json` using `reply_rule_order` and `reply_rule_definitions`, in order, on that message's own boolean fields.

Do not modify anything under `/app/input/`. Write only `/app/output.json`, a single JSON object with exactly these keys:

`final_schedule` — a list of the occurrences that end up scheduled. Each entry: `occurrence_id`, `event_id`, `calendar` (`executive`, `operations`, or `travel`), `venue` (the venue actually used), `start_utc`, `end_utc` (both `"YYYY-MM-DDTHH:MM:SSZ"`, the actually-scheduled UTC instant, reflecting any move).

`moved_items` — a list (any order) of `occurrence_id` values for scheduled occurrences whose final `start_local`/venue differs from their original one.

`deferred_items` — a list (any order) of `occurrence_id` values for occurrences that do not appear in `final_schedule`. Every occurrence expanded from the calendars must appear in exactly one of `final_schedule` or `deferred_items`, never both, never neither, and never more than once within either.

`reply_categories` — an object mapping every `message_id` to exactly one of `ACK`, `DECLINE`, `PROPOSE_ALTERNATE`, `REQUEST_INFO`, `ESCALATE`.

`objective_score` — an object with integer fields `priority_score`, `lateness_minutes`, `moved_count`, `travel_minutes`, computed exactly as defined in `objective_definitions` for your `final_schedule`.