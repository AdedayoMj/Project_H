# Dynamo/Harbor Task Difficulty Playbook

Distilled from actually clearing the difficulty gates on `exec-schedule-triage`
(regulated-knowledge-work category, an optimization/scheduling task). Split
into a **universal core** (applies to any task category — coding, data
analysis, document review, whatever the next one is) and a **combinatorial
addendum** (only relevant if the next task's core mechanic is itself a
search/optimization/packing problem, like the scheduling task was).

> **Honesty constraint, carried over on purpose:** none of this predicts a
> specific future pass@2/pass@5 result. It's a list of mechanisms that made
> agents *actually* fail in this task's CI runs, and the diagnostic process
> that found them. Only the platform's own CI run is ground truth — treat every
> checklist item below as "worth trying," not "guaranteed to work."

---

## 1. The two gates, in plain terms

- **pass@2 (too-easy gate):** if a capable agent solves the task twice in a
  row, the task is rejected as too easy before it even reaches harder review.
- **pass@5 (real difficulty gate):** of 5 genuine agent attempts, at least 3
  must fail on the actual rubric/verifier — not on infrastructure, not on
  ambiguity, on the substance of the task.

Both gates are about the same underlying thing: does solving this actually
require the insight you built the task around, or is there a shortcut that
gets full credit without it?

## 2. Diagnosing "too easy" — what to actually check

When pass@2 comes back 2/2 solved, don't just make the task "bigger." Read the
trajectory/CI report and ask, in this order:

1. **Is the hard part decomposable into small independent pieces an agent can
   solve one at a time?** If so, whatever "easy path" exists per piece (brute
   force, a lookup, a simple heuristic) trivially clears the whole task and
   the intended hardness never bites. *(Root cause #1 in the scheduling task —
   the original dataset's conflict clusters were all small and independent.)*
2. **Even after scaling up, did agents just reach for the "obviously correct"
   approach and still finish inside budget?** Bigger data/scope alone doesn't
   create difficulty if a standard tool or method solves it once an agent
   recognizes it's needed. Scale has to be paired with something that trips up
   recognition or execution, not just volume.
3. **Read pass@5 trajectory reports, don't guess.** The real bottleneck is
   often *not* the piece you assumed. In the scheduling task, the intended
   trap was "this is NP-hard, you need an exact method" — but the actual
   failure mode agents hit was a **secondary, orthogonal inefficiency**: a
   textbook-correct encoding of the objective's tie-break clause that scaled
   linearly and stalled, while a smarter single-pass encoding of the *same*
   tie-break stayed fast. Dataset size was fine; the trap that mattered was
   one layer deeper than the first thing that got fixed.

**Takeaway:** every round of "make it harder" should be justified by evidence
from a trajectory report about *why* agents succeeded, not by intuition about
what "feels" hard.

## 3. Universal difficulty mechanisms

These held up regardless of task type — they're about how a task is
structured, not about any particular algorithm:

- **Composed constraints that are each easy alone but only produce the correct
  answer when applied jointly.** In the scheduling task: protected blocks +
  VIP override + travel buffers + dependency chains + RRULE/DST expansion, all
  interacting in one schedule. In a different task category this looks like:
  multiple interacting edge cases in a debugging task, multiple interacting
  clauses in a document/policy-review task, multiple interacting fields in a
  reconciliation task. An agent that gets 4 of 5 rules right still produces a
  wrong final artifact under exact-match grading.
- **A rule that reads like it says one thing but actually says something
  narrower** — a plausible-sounding misreading that a careful agent must
  reject. In the scheduling task: "VIP overrides protected" sounds absolute,
  but the real rule is "VIP only *unlocks* the possibility of displacing
  protected; the objective still decides." Cheap to add in any domain that has
  written policy/spec text the agent must follow — just write one rule with an
  exception clause that's easy to over-generalize. Make sure the wording in
  any human-readable notes matches the verifier's actual behavior exactly, or
  you'll catch yourself in the trap instead of the agent (this happened once
  during authoring).
- **Large surface area of required-correct decisions, graded by exact match.**
  200+ scheduled/deferred occurrences and 24 classified messages, all exact
  match, meant a single mis-modeled rule anywhere in the pipeline produced a
  detectably wrong final artifact. Generalizes directly: many test assertions,
  many records to classify, many fields to fill — doesn't need to be "hard" on
  its own, it just compounds the odds that *some* subtle case exposes a
  shortcut.
- **A second, independent trap layered on top of the first.** Don't rely on
  one mechanism. In the scheduling task, even after an agent correctly
  identified "this needs CP-SAT," a second, unrelated trap (tie-break encoding
  efficiency) still caught agents who stopped reasoning one step too early.
  The general lesson: one trap catches one class of shortcut; a second,
  unrelated trap catches agents who cleared the first but stopped there.
- **A task that mirrors a real, recognizable expert workflow.** Difficulty
  that comes from genuine domain complexity (how a real chief of staff/EA
  actually reconciles calendars, how a real on-call engineer actually
  triages an incident) reads as legitimate to reviewers, versus difficulty
  that feels artificially bolted on.

## 4. The iteration loop that actually worked

1. Build the full correct reference solution first. Verify oracle=1.0,
   nop=0.0, clean-image check passes, repeatability holds (`oracle` run twice
   gives the same reward, `nop` run twice gives the same reward).
2. Run pass@2. If both pass, do **not** just scale the task — go read what
   the successful trajectories actually did.
3. Identify the *specific* shortcut that let them through (decomposability,
   recognition-is-enough, whatever it is per §2).
4. Add one genuine mechanism from §3 (or the addendum, if applicable) that
   closes exactly that shortcut.
5. Re-verify the reference solution still produces the correct answer, then
   re-run pass@2/pass@5.
6. If pass@5 comes back with fewer than 3/5 failing, read *those* trajectory
   reports too — the bottleneck you fixed may not be the one that mattered
   most (this happened here: fixing dataset size wasn't the fix; fixing the
   tie-break encoding was).
7. Repeat, always driven by evidence from a real report, never by guessing.

## 5. Common automated-review pitfalls (cheap fixes, check these before CI)

- `instruction_concision`: don't pad `instruction.md` with boilerplate
  (anti-cheat suffixes, restated countdown language) that isn't actually
  required by the current live template.
- `verification_explanation` wording: describe what the verifier actually does
  ("compares against a precomputed reference fixture"), not vaguer language
  like "re-derives," if that's not literally true.
- Missing `.dockerignore` on any non-trivial build context.
- `artifact_type` in `task.toml`: keep it to the minimal accurate tag(s) from
  the taxonomy file rather than stacking extra plausible-sounding tags.
- Make sure any human-readable policy/rule text shown to the agent matches the
  code's actual behavior exactly — a mismatch either makes the task
  unsolvable-as-stated or accidentally easier than intended.

## 6. Fill-in-the-blank template for a new task

```
## Task: <name>

### Core domain skill being tested
<the one real insight/judgment call this task is built around>

### Composed constraints (list each; mark which ones interact)
1. <constraint A>
2. <constraint B> — interacts with A when <condition>
...

### The "narrow reading" trap (optional but cheap and effective)
Plausible-but-wrong reading: <...>
Actual rule: <...>
Where this is checked in the verifier: <...>

### The genuine hard core mechanic
What "looks solvable via a shortcut" at small scale: <...>
Why that shortcut breaks down at the scale/scope you're setting: <...>
Why a naive-but-plausible correct-seeming approach still fails/stalls: <...>
Why the properly-scoped approach stays reliable/fast: <...>
(If this is a search/optimization/packing task, see the combinatorial
addendum below for the CP-SAT-specific version of this block.)

### Second, orthogonal trap (don't rely on only one)
<...>

### Surface area / exact-match grading
Total number of independently-gradable decisions: <...>
Why a single mis-modeled rule produces a detectable wrong artifact: <...>

### Verification checklist
[ ] oracle run twice -> reward 1.0 both times
[ ] nop run twice -> reward 0.0 both times
[ ] clean-image check -> no solution/test files leaked into the image
[ ] pass@2 run -> read trajectories regardless of outcome
[ ] pass@5 run -> read ALL 5 trajectories, root-cause each failure AND each success
[ ] policy/rule text shown to agent matches verifier code exactly
[ ] static checks: instruction_concision, verification_explanation wording,
    .dockerignore present, artifact_type minimal-and-accurate
```

## 7. What not to do

- Don't scale raw size/scope as a first response to "too easy" — check
  decomposability and shortcut-availability first; scale alone rarely fixes
  it.
- Don't stack difficulty mechanisms you haven't verified are necessary — each
  one should trace back to a specific trajectory-report finding.
- Don't claim or imply a pass@2/pass@5 outcome you haven't seen in an actual
  CI report. "This mechanism worked before" is not "this will pass now."
- Don't let a policy-note description drift from what the code actually does
  — reviewers and agents both read that text as ground truth.

---

## Addendum: combinatorial/optimization-task specifics

**Only relevant if the next task's core mechanic is itself a search,
optimization, packing, or scheduling problem** (i.e. there's an objective to
maximize/minimize and a large space of valid assignments). If the next task is
a different category (debugging, data reconciliation, document synthesis,
etc.), skip this section — force-fitting this language onto a non-search task
will just read as irrelevant boilerplate.

- **A genuinely NP-hard sub-instance, sized deliberately.** Not "big data" —
  specifically an instance where naive search (branch-and-bound, greedy,
  exhaustive) explores a large space before proving optimality, while a
  correctly-scoped exact method (CP-SAT/ILP) stays fast. Size it so brute
  force is provably intractable but a properly modeled solver call finishes
  in seconds.
- **Priorities/weights clustered close together on purpose.** If one option is
  obviously best, a greedy heuristic gets the right answer for the wrong
  reason. Clustering values close together forces an agent to actually search
  or optimize rather than eyeball it.
- **The tie-break/secondary-objective trap.** Even when an agent correctly
  reaches for an exact solver, a naive-but-textbook-correct encoding of a
  lexicographic tie-break (e.g. a sequence of per-item solver calls) can scale
  linearly and stall, where a single-pass weighted-objective encoding of the
  same tie-break stays fast. This is a distinct trap from "needs an exact
  method at all" — worth testing for separately.
