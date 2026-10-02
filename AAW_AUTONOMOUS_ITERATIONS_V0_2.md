# AAW Autonomous Iterations V0.2

Real adapter integration, run lock and evidence hardening for the V0.1
controller. The V0.1 state machine (`TRANSITIONS`, scope guard, repair loop,
Human Gate) is unchanged; V0.2 changes *how a phase is executed and evidenced*,
not *which phases exist*.

Files: `autonomy_adapters.py` (role → profile → direct CLI),
`autonomy_run_lock.py` (one controller per run), `autonomy_controller.py` /
`autonomy_contract.py` (execution lifecycle, iteration identity, resume
reconciliation, Git checks), `autonomy_e2e_v0_2.py` (real E2E driver),
`aaw_autonomy_fake_cli.py` (test-only scripted `claude` binary),
`test_autonomy_v0_2.py`, `test_autonomy_git_containment.py`,
`EVIDENCE/AAW_AUTONOMY_V0_2_E2E.json`.

Legend used below: **guaranteed** = enforced before the action can happen;
**detected** = noticed at the next phase boundary after another process did
it (not prevented); **checked** = verified on demand / when configured.

## 1. Real role execution

| Executor (phase) | Role | V0.4A `invocation_kind` | Read-only? |
|---|---|---|---|
| `plan` (PLAN) | `planner` | `PLAN` | yes |
| `execute` (EXECUTE) | `implementer` | `LLM` | no |
| `self_verify` (SELF_VERIFY) | `self_verifier` | `LLM` | yes (enforced, see §2) |
| `review` (REVIEW) | `reviewer` | `REVIEW` | yes |
| `repair` (REPAIR) | `repairer` | `REPAIR` | no |
| `final_review` (FINAL_REVIEW) | `final_reviewer` | `REVIEW` | yes |

Resolution is configuration only: `AUTONOMY_ROLES.json` role → `profile_id` →
`IMPLEMENTER_PROFILES.json` → `MODEL_CATALOG.json` → harness (`codex` |
`claude`). No model name appears in controller or adapter logic (asserted by a
test). `self_verifier` and `repairer` are new explicit roles; a config that
omits them gets the **declared contract alias** to `implementer`
(`binding_source: CONTRACT_ALIAS:implementer`) — V0.1's behaviour — which is not
a runtime substitution. The shipped `AUTONOMY_ROLES.json` binds them explicitly
to the same profile V0.1 used.

**Availability.** Before allocating anything, the controller calls the
executor's `preflight` (`workflow_runner.profile_availability`: catalog,
account, NO_EXTRA_PAID_USAGE, harness executable). Unavailable ⇒
`ROLE_PROFILE_UNAVAILABLE` escalation → `AWAITING_HUMAN`, not promotable, **no
execution_id allocated** (an execution_id names an invocation). There is no
fallback; none is declared in config, so none exists.

**Dispatch** reuses the direct-CLI primitives of `workflow_runner`
(`run_process(dispatch=True)`, `harness_executable`, `profile_availability`,
`parse_codex_events`, `_close_from_returncode`) with the same argv shape as
`execute_llm_node` (`codex exec --ephemeral --sandbox read-only|workspace-write`
/ `claude --print --no-session-persistence --permission-mode plan|acceptEdits`),
a role-specific JSON schema, and the prompt on **stdin** (a review handoff with
a diff exceeds the Windows command-line limit). For write roles on `claude`,
`--allowedTools` permits a shell for checks and `--disallowedTools` denies
`git push/merge/rebase/pull/checkout/switch/reset` (defence only; §7 is the
authority). Local Qwen is not bound to any role: the catalog forbids it for
implementation and independent review. `review_prep` / `prepare_packet` keeps
its V0.1 optional status; the production executor set does not provide it, so
the deterministic packet is used as-is.

## 2. Role output contracts

Small closed JSON schemas (`autonomy_adapters.OUTPUT_SCHEMAS`). Field names are
the V0.1 controller contract; the V0.2 vocabulary maps onto them:

| Role | V0.2 term → field |
|---|---|
| Planner | next_action → `goal`; roadmap_reference → `roadmap_refs`; rationale → `scope_justification`; acceptance criteria → `acceptance_criteria`; scope references → `touched_areas`; stop/escalate → `status` (`ITERATION` / `NO_FURTHER_ACTION` / `ESCALATE`) + `reason`; plus `mandate_hash` echo, `decisions`, `skipped_items` |
| Implementer | result summary → `summary`; `changed_files`; checks run → `checks[]`; known limitations → `uncertainties`; `deviations` |
| Self-verifier | `summary`, `checks[]` |
| Reviewer / final reviewer | `verdict` ∈ PASS / REPAIR_REQUIRED / ESCALATE; `findings[]` (`finding_key`, `severity`, `summary`, `file`, `blocking`, `evidence_ref`) |
| Repairer | `summary`; addressed findings → `addressed_findings`; `changed_files`; `checks`; remaining uncertainty → `uncertainties` |

Each call receives a **structured handoff** — frozen mandate (planner,
reviewers), plan, constraints, packet, raw diff, explicit source references
(execution descriptor path, mandate hash) — never another role's transcript.
Invalid non-review output ⇒ `EXECUTOR_FAILED`; invalid reviewer output is
passed to `normalize_review` ⇒ `REVIEW_RESULT_INVALID` (ESCALATE), never PASS.
A self-verifier that changes the diff ⇒ `SELF_VERIFY_MUTATED_WORKTREE`
(guaranteed: compared before/after by the controller).

## 3. Review packet

`build_review_packet` (V0.1) plus, in V0.2, `access.commits`. The reviewer's
`raw` context is read from the repository at call time: `diff` (base..worktree:
committed, staged, unstaged and untracked) and `diff_path` (persisted under
`AUTONOMY/PACKETS/`), `base_head`, `head`, `commits`, `changed_files`, checks,
`self_verify` results, the structured implementation result and previous
findings. Each reviewer call is a new process with no session persistence.

*Defect fixed:* V0.1 diffed against `HEAD`, so a local checkpoint commit made
the iteration's work invisible to the reviewer. The diff base is now the
worktree head at run start. `changed_files` uses name-only listings
(`workflow_runner.changed_files` truncates the first porcelain path — e.g.
`calc.py` → `alc.py` — because `git()` strips its output; that function is not
changed here).

## 4. Iteration identity

`ITER_<uuid4 hex>`, allocated when PLAN starts and persisted in
`state.planning` before the planner is called, so a replayed PLAN keeps the same
iteration identity. One iteration owns several executions (PLAN, IMPLEMENT,
SELF_VERIFY, REVIEW, REPAIR*, FINAL_REVIEW). `index` remains as ordinal only.
An iteration id is never an execution id.

## 5. Execution identity and the evidence boundary

Every executor call — real or scripted — is one V0.4A execution, owned by the
controller (`AutonomyController._call`):

```
role preflight → allocate EXE_ descriptor (03_STATS/<run>/EXECUTIONS/)
  → in_flight marker {phase, executor, iteration_id, execution_id} saved
  → EXECUTION_INTENT (fail-closed: failure ⇒ LEDGER_WRITE_ERROR, nothing dispatched)
  → executor inside process_observation scope
       real adapter: spawn (dispatch=True) ⇒ EXECUTION_STARTED with PID + OS creation time
  → result artifact  AUTONOMY/RESULTS/<execution_id>.json  (write-once)
  → EXECUTION_CLOSED (adapter: CHILD_PROCESS_EXIT; scripted: IN_PROCESS_ADAPTER_RETURN)
  → execution reference + provider_session_id into autonomy_state.json
```

Descriptor fields: `run_id`, `node_id = AUTONOMY:<iteration_id>:<EXECUTOR>`,
`invocation_kind`, `profile`, `harness`, `model`, `effort`, `provider_session_id`,
`fixture_class` (`REAL_PROVIDER_DIRECT_CLI` / `UNDECLARED_EXECUTOR`),
`retry_of_execution_id`, `relations` (`iteration_id`, `executor`, `phase`,
`reviewed_execution_ids`, `repairs_findings_of`).

| Stream | Authority for | Must not contain |
|---|---|---|
| V0.4B `LEDGER/execution_events.jsonl` | physical execution lifecycle (INTENT / STARTED / CLOSED) | controller decisions |
| `AUTONOMY/autonomy_events.jsonl` | logical controller transitions (PLAN accepted, verdicts, repairs, roadmap decisions, holds, reconciliation decisions, lock refusals) | PID, start/close times, close reasons, effect certainty |

Shared keys: `run_id`; `iteration_id` (ledger INTENT payload
`autonomy_iteration_id`, descriptor `relations.iteration_id`); `execution_id`
(autonomy `PHASE_STARTED` / `PHASE_COMPLETED` / `REVIEW_VERDICT` /
`REPAIR_COMPLETED` / `IN_FLIGHT_RECONCILED` / `EXECUTION_RESULT_ADOPTED` carry
it as a **reference**). Controller-only phases (packet preparation,
ROADMAP_CHECK) reference no execution. A test asserts the journal contains no
ledger event types and no lifecycle fields.

**Fresh context.** Each call is a new process (`--no-session-persistence` /
`--ephemeral`). Provider-session variables inherited from a parent Claude Code
session (`CLAUDE_CODE_SESSION_ID`, `CLAUDE_CODE_REMOTE_SESSION_ID`) are removed
from the child environment (`run_process(env_remove=...)`, the only change to
`workflow_runner`): without that, a child `claude --print` reports the parent's
session id. If any two executions of a run report the same provider session id,
the run escalates `PROVIDER_SESSION_REUSED`.

## 6. One controller per run

`<run>/AUTONOMY/controller.lock`, published with `os.link` (atomic; fails if the
name exists). Record: `owner_token` (`LCK_<uuid>`), host, PID, **OS process
creation time** (`GetProcessTimes` / `/proc/<pid>/stat`), purpose, time.

| Situation | Outcome |
|---|---|
| no lock | `RUN_LOCK_ACQUIRED` |
| owner alive (same PID **and** same creation time) | `RUN_LOCK_BUSY` |
| owner on another host, PID alive without comparable creation time, unreadable/partial lock | `RUN_LOCK_BUSY` (ambiguous ⇒ busy) |
| PID gone, or PID alive with a different creation time (reused) | `RUN_LOCK_STALE_REQUIRES_RECONCILIATION` |

Never stolen. `reconcile_stale_lock(run_dir, expected_owner_token, operator,
reason)` is an explicit operator action: it re-proves staleness, requires the
exact token the operator inspected and a named operator, and moves the lock
aside as `controller.lock.retired.<token>.json`. `start` and `resume` acquire
the lock **before** reading state for planning; `run()` re-verifies ownership at
every phase boundary and releases in `finally` (normal end, escalation, or an
exception in this process). A killed process leaves its lock ⇒ stale ⇒ explicit
reconciliation. The human surface (`approve_promotion`, `reject`, `promote`)
takes the same lock, so an approval cannot race a controller. Local scope only;
no distributed locking.

## 7. Resume

```
acquire run lock → load state (mandate hash re-verified)
  → restore workspace checkpoint (Git, §8)  → violation ⇒ GIT_BOUNDARY_VIOLATION
  → inspect in_flight.execution_id in the ledger + result artifact
  → decide (journal: IN_FLIGHT_RECONCILED with ledger_state and decision)
```

| In-flight phase | Ledger / artifact | Decision |
|---|---|---|
| EXECUTE / REPAIR | anything | escalate `INTERRUPTED_IN_FLIGHT` (detail names execution id, ledger state, artifact); no replay |
| PLAN / SELF_VERIFY / REVIEW / FINAL_REVIEW | CLOSED `COMPLETED` + result artifact | adopt the recorded result (`EXECUTION_RESULT_ADOPTED`); no new invocation |
| read-only phase | `INTENT_ONLY`, `STARTED_NOT_CLOSED`, or no artifact | replay under a **new** `execution_id` with `retry_of_execution_id` = old |
| V0.1 state without execution_id | — | V0.1 rule (replay read-only, escalate side-effecting) |

The old execution id is never dispatched again (the ledger refuses a second
INTENT: `AT_MOST_ONCE_DISPATCH_VIOLATION`) and is not "tidied": it stays
`INTENT_ONLY` / `STARTED_NOT_CLOSED` (reported as `unresolved`, not as a ledger
error). Repair counters, iteration budget, holds and the Human Gate are state,
so a restart cannot reset or pass them. A resumed run whose worktree holds its
own uncommitted work is opened with `GitWorkspaceEnvironment(..., resuming=True)`
(structural checks kept, dirty-worktree refusal waived) and held to the
**persisted** baseline, not to the post-crash HEAD.

## 8. Git guarantees (measured, `test_autonomy_git_containment.py`)

**Guaranteed (blocked):** AAW's own `GuardedGit` refuses merge, pull, push,
rebase, cherry-pick, am, revert, protected-ref rewrites and checkout/switch of a
protected branch unless it holds a one-shot `PromotionToken`; V0.2 binds the
token to `run_id` and `candidate_id` (a handle for candidate X rejects a token
for Y or for another run). Claude write roles additionally have those git
commands in `--disallowedTools` (defence in depth, not authority).

**Detected at the next boundary** when another process does it — fixture:
canonical repo, isolated worktree, local bare remote, second clone; each
mutation run by a separate process:

| Scenario (another process) | local refs only | with `remote_check=True` |
|---|---|---|
| A commit in the worktree (allowed) | not a violation | not a violation |
| B merge into main in the canonical checkout | DETECTED (canonical HEAD) | DETECTED |
| B `update-ref refs/heads/main` from the worktree | DETECTED | DETECTED |
| C push to remote main **from this repository** | DETECTED (remote-tracking ref updated by the push) | DETECTED |
| C push to remote main **from another clone** | **NOT DETECTED** | DETECTED (`ls-remote`) |
| D `git fetch` moving `origin/main` | DETECTED (fail-closed by design) | DETECTED |
| E rebase dropping a commit observed at a boundary | DETECTED (observed head not an ancestor) | DETECTED |
| E amend of an observed commit | DETECTED | DETECTED |
| E rewrite of commits created *and* rewritten between two boundaries | **NOT DETECTED** | NOT DETECTED |
| F `checkout --ignore-other-worktrees main` in the worktree | DETECTED (protected branch) | DETECTED |
| F canonical checkout switches branch (same commit) | DETECTED (symbolic ref) | DETECTED |
| G force-push remote main from another clone | **NOT DETECTED** | DETECTED |
| G force-push remote main from this repository | DETECTED | DETECTED |
| push of a non-protected branch | NOT DETECTED (not protected) | NOT DETECTED |

Plus: an unreadable remote with `remote_check=True` fails closed; changes made
while the controller was down are detected on resume (§7).

**Therefore the V0.1 claim "any change (merge or push by any subprocess)
escalates" is narrowed.** A push to the remote is not a local event: without
`remote_check` it is visible only if made from this repository or after a
fetch. With `remote_check=True` the remote's protected refs are read at every
boundary — this is checked, not prevented, and costs one `git ls-remote` per
phase (network for a real remote). Nothing in AAW prevents another process on
the machine from mutating Git; AAW detects the listed cases and stops.

## 9. Human Gate

Unchanged semantics: review PASS ≠ merge; final review PASS ≠ merge;
`AWAITING_HUMAN` → human approves the exact `candidate_id` (HUMAN channel,
non-agent approver — every role, profile and model id of the run is refused,
including the new `self_verifier` / `repairer`) → `HUMAN_APPROVED` → `promote()`
⇒ `READY_FOR_EXTERNAL_INTEGRATION`, `merged: false`, `pushed: false` by default.

V0.2 hardening: the candidate id now binds HEAD **and** the produced diff
(`candidate_fingerprint`: head, diff hash, changed files — uncommitted work is
part of the candidate). An integrating `promoter` requires `env`; the candidate
is re-read and PROMOTE is refused if HEAD or diff changed after approval. The
token is one-shot and bound to run and candidate; a second `promote` is refused
(state is `PROMOTED`).

## 10. Real E2E (this environment)

`python autonomy_e2e_v0_2.py --work-dir <scratch> --evidence EVIDENCE/AAW_AUTONOMY_V0_2_E2E.json`
— disposable fixture (`calc.py` + unittest, bare remote, worktree), mandate
"add multiply() with a test", `remote_check=True`, real `claude` CLI. The
production `AUTONOMY_ROLES.json` was preflighted and recorded as-is (here:
`codex` CLI absent ⇒ implementer / self_verifier / reviewer / repairer
unavailable ⇒ such a run would stop `ROLE_PROFILE_UNAVAILABLE`). The run used a
declared validation binding: every role `SONNET_HIGH` with
`allow_same_model_fresh_context: true` (independence recorded as
`SAME_MODEL_FRESH_CONTEXT`; fresh context evidenced by distinct provider
sessions). Result: PLAN → EXECUTE → SELF_VERIFY → REVIEW (PASS) → FINAL_REVIEW
(PASS) → ROADMAP_CHECK (exhausted) → `AWAITING_HUMAN`, promotable candidate; 5
executions, 5 distinct provider sessions, 15 ledger events
(INTENT/STARTED/CLOSED ×5, 0 unresolved, valid); canonical `main` and remote
`main` unchanged. The multi-iteration flow (same run, distinct iteration and
execution ids, frozen mandate, preserved budgets) is covered with scripted
executors (`test_multi_iteration_same_run_distinct_ids_frozen_mandate_and_budgets`).

## 11. Limitations

* The production role binding is Codex-based; it was not exercised for real
  here (no `codex` CLI in this environment). Only the `claude` harness ran for
  real. The Codex argv mirrors `workflow_runner.execute_llm_node` but is
  unverified by a live run in V0.2.
* The E2E used one profile for all roles (same model, fresh context), which is
  weaker independence than the production DIFFERENT_MODEL binding.
* External pushes from other clones are detected only with `remote_check=True`;
  rewrites of never-observed commits and pushes of non-protected branches are
  not detected. Detection is at phase boundaries, never preventive.
* The run lock is local (host + PID + creation time). A lock owned by another
  host is always BUSY; a crashed controller needs explicit reconciliation.
* A replayed read-only phase whose orphan process may still be running can
  overlap with it (harmless for read-only roles, but it costs a call).
* `SELF_VERIFY` read-only is enforced on the produced diff, not on files outside
  the worktree; the Git boundary check covers protected refs only.
* `review_prep` (packet compression) has no real executor in V0.2.
* `workflow_runner.changed_files` keeps its first-path truncation defect (out of
  scope here; worked around in the autonomy layer).
* No UI surface; the Control Center does not show autonomy runs.
