# AAW Autonomous Iterations V0.1

Additive layer above the static workflow runner. A human supplies a **mandate**;
AAW runs independently reviewed iterations inside it and stops only at the end
of the roadmap, at a real escalation, or before PROMOTE. Autonomy covers *how*
the goal is reached — never promotion.

Files: `autonomy_contract.py` (pure decisions), `autonomy_controller.py`
(state, journal, resume, human surface), `AUTONOMY_ROLES.json` (role → profile),
`test_autonomy.py`. `workflow_runner.py`, the routing contract, the ledger and
the UI/bridge are unchanged.

## Mandate

First prompt, split into two frozen, hashed halves (`validate_mandate`):

* `iteration_contract` — exact spec of iteration 1: goal, scope, acceptance
  criteria, constraints, forbidden changes, required evidence.
* `roadmap_mandate` — objective, items (with `depends_on`), priorities and
  `autonomy_bounds` (`max_iterations`, `max_repair_attempts`, `allowed_areas`,
  `forbidden_areas`). Direction, not a TODO list. Hard ceilings (50 iterations,
  6 repairs) cannot be raised by a mandate.

## Lifecycle

```
PLAN -> EXECUTE -> SELF_VERIFY -> AWAITING_REVIEW -> REVIEW
     -> [REPAIR ->] FINAL_REVIEW -> ROADMAP_CHECK -> PLAN (next iteration)
                                                  \-> AWAITING_HUMAN
AWAITING_HUMAN -> HUMAN_APPROVED -> PROMOTE -> PROMOTED      (or REJECTED)
```

`TRANSITIONS` in `autonomy_contract.py` is the whole edge set. PROMOTE is only
reachable from HUMAN_APPROVED, which is only reachable from AWAITING_HUMAN
(asserted by a test). A failed SELF_VERIFY repairs and re-verifies; a REVIEW or
FINAL_REVIEW repair goes to FINAL_REVIEW. Reviewer verdicts: `PASS`,
`REPAIR_REQUIRED`, `ESCALATE`. A PASS that coexists with a blocking finding or a
still-failing check is downgraded to `REPAIR_REQUIRED` (recorded); an unparseable
verdict is an ESCALATE.

After FINAL_REVIEW = PASS the controller marks the iteration's roadmap items
DONE and runs `ROADMAP_CHECK`: items remain and budget is left → PLAN again
(no human); nothing remains → `AWAITING_HUMAN` (`ROADMAP_EXHAUSTED`); budget
spent → `AWAITING_HUMAN` (`ITERATION_CAP_REACHED`, promotable only by explicit
`early_end`).

## Autonomy levels and the scope guard

`AUTO` (inside an iteration), `AUTO_WITHIN_SCOPE` (planner: internal
architecture, reorder/split/merge/skip/replace stages), `ESCALATE` (goal,
requirement, product behaviour, public contract, irreversible migration,
business decision, unacceptable risk, mandate extension, **any unknown kind**).

`check_plan` accepts an iteration only if it carries: the frozen `mandate_hash`,
a goal, a scope justification, completion criteria, `roadmap_refs` that are
*pending* items whose dependencies are met, touched areas inside the mandate,
and only non-ESCALATE decisions. Iteration 1 may not drop the human's acceptance
criteria. Skipping an item needs a per-item reason; the planner may end the
roadmap early only by individually justifying every remaining item. Extension
attempts (`new_roadmap_items`, `mandate_amendment`, …) escalate; the persisted
mandate is re-hashed on every load.

## Repair loop

Bounded by `max_repair_attempts` per iteration; exceeding it, or two repairs in
a row that leave the same findings (`REPAIR_NO_PROGRESS`), escalates.

## Review packet

`build_review_packet` → TASK / PLAN / IMPLEMENTATION / EVIDENCE / RISKS /
REVIEW_TARGETS plus a code-owned `access` block (diff hash, head, pointers).
`authoritative` is always false. The reviewer executor is handed the packet
**and** a `raw` object read from the repository at that moment (diff, hash,
files, head, evidence). A packet whose diff hash no longer matches is rebuilt
(`PACKET_STALE`). Adverse items (failed/warn/skipped checks, warnings,
deviations, uncertainties, prior findings) are extracted by code; an optional
`prepare_packet` compressor may shorten prose but dropped items are re-injected
verbatim (`integrity.reinjected`).

## Roles

`AUTONOMY_ROLES.json` maps `planner`, `implementer`, `review_prep`, `reviewer`,
`final_reviewer` to `IMPLEMENTER_PROFILES` ids. No model name appears in logic.
Reviewer on the implementer's runtime model is refused unless
`allow_same_model_fresh_context` is true; the recorded independence is
`DIFFERENT_MODEL` or `SAME_MODEL_FRESH_CONTEXT`.

## Git and promotion

* Local commits on the iteration branch are allowed. `classify_git_command` /
  `GuardedGit` deny merge, pull, push, rebase, cherry-pick, checkout/switch of a
  protected branch, protected ref rewrites; only a one-shot `PromotionToken`
  (minted by `promote()` from a recorded human approval) lets merge/push through.
* `GitWorkspaceEnvironment` reuses `validate_workspace` / `assert_main_unchanged`
  and snapshots protected local and remote-tracking refs; any change (merge or
  push by any subprocess) escalates at the next phase boundary
  (`GIT_BOUNDARY_VIOLATION`). A `git fetch` that moves `origin/main` also trips
  it — fail-closed by design.
* `approve_promotion` (human surface, not reachable from executor context)
  requires AWAITING_HUMAN, a promotable hold, the exact `candidate_id`, the
  HUMAN channel, an approver that is not an agent role/profile/model, and
  `early_end=True` unless the roadmap is exhausted. Holds caused by escalation
  are never promotable. `promote()` without a `promoter` records
  `READY_FOR_EXTERNAL_INTEGRATION` and merges/pushes nothing — the Human Gate
  semantics of V0.2.

## Audit

`autonomy_state.json` holds mandate, roadmap statuses, every iteration (lineage,
plan, planned_by / executed_by bindings, execution, checks, repairs, reviews,
final reviews, packet) and the hold/escalation. `autonomy_events.jsonl` is the
ordered why-stream: MANDATE_FROZEN, SCOPE_CHECK, ITERATION_PLANNED,
PHASE_STARTED/COMPLETED, REVIEW_VERDICT, REPAIR_COMPLETED, ITERATION_ACCEPTED,
ROADMAP_DECISION, ESCALATED, AWAITING_HUMAN, HUMAN_APPROVED, PROMOTED,
RUN_RESUMED. It is its own stream because `routing_contract.EVENT_TYPES` is a
closed, UI-facing vocabulary.

## Resume

State is saved atomically at every boundary with an `in_flight` marker around
each executor call. Resume re-runs an interrupted read-only phase (PLAN,
SELF_VERIFY, REVIEW, FINAL_REVIEW) and escalates an interrupted EXECUTE/REPAIR
(`INTERRUPTED_IN_FLIGHT`) rather than repeat unknown worktree side effects.
Counters and holds survive; a restart cannot reset the repair budget or pass the
human gate.

## Not included

Concrete DIRECT_CLI_CONTROL executors for the five roles (the controller takes
them as injected callables — `plan`, `execute`, `self_verify`, `review`,
`repair`, `final_review`, optional `prepare_packet`); a UI surface; a lock
against two controllers on one run.
