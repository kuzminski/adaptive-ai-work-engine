# AAW — adaptive model routing and bounded repair escalation (V0.1)

Lifecycle unchanged: `PLAN → EXECUTE → SELF_VERIFY → AWAITING_REVIEW → REVIEW → REPAIR → FINAL_REVIEW`;
after PASS with a roadmap remaining the controller returns to PLAN. No new phase, no new transition.

## 1. Quota / trust / capability routing (`model_router.py`)

Extends the existing profile selection: `autonomy_policy.select_*` still names the **preferred** profile;
`AutonomyController._call` asks the router whether an equally adequate profile should serve instead and
dispatches with bounded failover. `AUTONOMY_ROLES.json → routing` configures it (absent/`enabled:false` = old behaviour).

Order: hard filters (availability, trust, capabilities, capability-class floor, provider health, exhausted limit)
→ preferred profile stays while its quota is normal or UNKNOWN → quota optimisation among adequate profiles only
→ hysteresis. Quota never lifts a weaker class over a stronger one (class gap costs 40/step; quota ≤ 30 points).

| Default | Meaning |
|---|---|
| `soft_threshold_percent` 10 | `>=10` normal; `<10` prefer an adequate alternative with more headroom |
| `reserve_percent` 5 | `<=5` reserve: not for ordinary PLAN/EXECUTE; kept for `review/repair/final_review/diagnose` |
| `near_reset_minutes` 20 | reset within 20 min: a SMALL task already on this provider stays |
| `return_threshold_percent` 25 | after a quota switch, return only at `>=25%`, on a *confirmed* reset (`last_reset_at` after the switch), on TTL expiry without telemetry, or when no better alternative exists |

* Multiple simultaneous limits (5h, daily, weekly, cost, per-model, rate limit…): the most restrictive known limit decides.
* Certainty `EXACT | ESTIMATED | UNKNOWN`. UNKNOWN carries no percent (a supplied one is discarded); ESTIMATED without
  provenance becomes UNKNOWN; stale EXACT → ESTIMATED → UNKNOWN. Telemetry: `QUOTA_TELEMETRY.json` (optional) and/or
  adapter `quota_snapshot()`; missing = UNKNOWN = no change.
* Trust `PRODUCTION | SECONDARY | EXPERIMENTAL | DISABLED`. EXPERIMENTAL (Antigravity FREE): execute/repair/self_verify/
  diagnose, SMALL/MEDIUM, never CRITICAL; never review/final_review; never VERY_LARGE.
* Manual override (mandate `model_policy_overrides.implementation`) beats heuristics if technically runnable.
* A repo-modifying operation already in progress is not migrated for an estimate change; a failed attempt is retried on
  the next ranked profile only if the worktree diff is provably unchanged.
* Provider health: timeout / rate limit (honours retry-after) / auth / unavailable put the provider in cooldown.
* Fail-closed contract kept: an unrunnable **preferred** profile still stops with `ROLE_PROFILE_UNAVAILABLE` unless
  `routing.substitute_unavailable` is true. No adequate profile at all → `ROUTING_NO_ELIGIBLE_PROFILE` (nothing dispatched).
* Audit: journal `ROUTING_DECISION` (selected/alternatives, quota+certainty+reset, trust, task class, scores incl. switch
  penalty, reason code, thresholds), `PROVIDER_FAILOVER`, and `selection.routing` on each execution + ledger intent.

## 2. Bounded repair escalation (`repair_escalation.py`)

A finding that survives a REPAIR no longer stops at the Human Gate after one repair. Ladder (config:
`AUTONOMY_ROLES.json → repair_escalation`; derived from `policy_profiles` when absent):

`CURRENT` → `EFFORT_UP` (one level, re-diagnose then repair) → `DIFFICULT_IMPLEMENTER` (DIAGNOSE, then REPAIR)
→ `PLANNER_DIAGNOSIS` (planner diagnoses, difficult implementer repairs) → Human Gate (`REPAIR_NO_PROGRESS`, ladder exhausted).

Roles, effort ladder and stages are configuration; no model name is in the logic. `max_repair_attempts` bounds the
CURRENT stage; escalation attempts have their own cap. Read-only `diagnose` executor is optional.

**Progress** is evidence-based, not file-count-based: finding resolved, new test result, new evidence, new root-cause
diagnosis, evidence-backed reclassification (PRE_EXISTING_BASELINE / ENVIRONMENTAL_LIMITATION / SUPERSEDED_BY_NEWER_CHECK
with `evidence_ref`, `explanation`, `acceptance_impact: NONE`), process fix. Progress earns a retry on the same stage
(max 2); no progress climbs. A changed diff alone is not progress; a repair needing no code change needs no diff.
Reclassified checks stay visible (reviewer adverse items, Human Gate warnings, `ITERATION_ACCEPTED.classified_limitations`).
A newer PASS can supersede a stale failing check by name or via `supersedes`.

**Ledger** `<run>/AUTONOMY/repair_escalation_ledger.jsonl` (append-only): `ESCALATION` (finding IDs, previous/new
model+effort, reason, previous result, code/evidence state changed), `RESULT`, `DISPOSITION` (RESOLVED / HUMAN_GATE).
**Compact repair packet** (finding, relevant AC, failing command/output, focused diff, prior attempts, constraints) replaces
the full context for later attempts.

**Human Gate**: an escalation now records `candidate_fingerprint` (whole-iteration changed files) plus `last_repair`
(code changed? signals) — previously the escalation path stored no fingerprint, so "Zmienione pliki" was always empty.

## 3. Case AAW_TASK_20261003_214957_3725b4 (`SELF_VERIFY::AC8`)

The worktree/branch live on the operator's Windows machine and are not reachable from this repository (no `aaw/…`
branch on the remote), so the run itself was **not inspected**. From the code: `SELF_VERIFY::<name>` is raised by
`evidence_failures` for a recorded check whose latest status is FAIL/ERROR; the finding key *is* the check name
(`AC8 — required post-install checks`). Evidence state was name-keyed with no supersession, so a repair that re-ran the
checks under another name, or proved them baseline/environmental, left the old FAIL in place → "unchanged" → one-repair
stop. Both paths are now handled (see §2). To continue the real run without restarting PBEL, update AAW and use the
Human Gate action "Kontynuuj z nowym celem / Dodaj kierunek" (new task starting from the preserved worktree); inspect
`autonomy_state.json → iterations[-1].checks` for the AC8 row's `command`/`summary` to see which branch applies.

## 4. Antigravity (EXPERIMENTAL) — live integration NOT_TESTED

`provider_adapters.py` defines the adapter contract (`detect`, `quota_snapshot`, `preflight`, `invoke`),
`AntigravityAdapter` (capability detection; refuses dispatch with a non-dispatched UNAVAILABLE) and
`ScriptedProviderAdapter` (mock). Profile `ANTIGRAVITY_FREE` is `KNOWN_BUT_UNAVAILABLE` and `routing…available:false`.
Wiring a verified CLI = implement `invoke`, mark the profile VERIFIED, set `available:true`.
