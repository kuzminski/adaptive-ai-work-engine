# AAW V0.4 — Astra decisions

2026-09-06. Sources: [Sol Evidence Pack](AAW_V0_4_SOL_EVIDENCE_PACK.md), [identity draft](AAW_V0_4_EVIDENCE_MODEL_DRAFT.json), targeted analytics inspection. Operational facts inherit Sol's scope. Thresholds are proposed governance defaults.

## 1. Trustworthy learning under selection bias

Optimize verified quality first, then total time or measured cost within a human-declared quality floor. Report tokens separately unless versioned prices justify conversion. Include preprocessing, review and repairs in workflow consumption.

Freeze a small difficulty card **before allocation**: task-family/version, node type, workflow/contract version, risk tier, expected scope band, input-context size band, acceptance-test specification/hash, repository/base revision, and prior-failure count known at dispatch. Scope comes from declared paths/subtasks, not eventual diff size. Unknown fields remain UNKNOWN. Record model/effort, reviewer configuration, preprocessing, runtime versions, selection reason, eligible alternatives and policy version separately. Do not collapse these into a learned difficulty score. Existing routing classes may supply fields when explicitly linked.

Comparison cells: node type × task family × risk/scope band, within compatible contract/evaluation versions. Other fields expose imbalance; absent overlap requires abstention. Repairs, final diff, findings and time are outcomes, never matching features.

| Comparison | Permitted interpretation |
|---|---|
| Same node type | Descriptive only unless difficulty and evaluation also match. |
| Comparable task class | Conditional association within overlapping cells; residual confounding remains. |
| Within workflow | Compare the same role across comparable runs; compare entire workflows only with equivalent entry contracts and outcome criteria. |
| Paired challenger | Strongest local evidence: identical frozen input/base in isolated worktrees, randomized order, blinded fixed evaluation and no shared answer. Supports the tested configuration on sampled tasks. |
| Historical unmatched | Workload, cost and reliability history; hypothesis generation, never a model winner. |

N counts independent tasks per cell/arm or distinct pairs. Retries, dependent subtasks and corpus reruns cannot inflate N. Conclusions concern tested model configurations.

| Approximate N | Maximum justified claim |
|---|---|
| 10 | Case analysis and failure hypotheses; no ranking. |
| 30 | Tentative, scoped human-confirmed recommendation if comparisons and coverage qualify. |
| 100 | Narrow automation eligibility with prospective challenger evidence and quality bounds; size alone grants nothing. |
| 500 | Stable-cell policy refinement and limited subgroup checks; no causal claim from unmatched history or assurance about rare catastrophic failures. |

Show numerators, denominators, missingness, medians and paired deltas. Wilson intervals quantify sampling uncertainty, not allocation bias. [NIST methodology](https://www.itl.nist.gov/div898/handbook/prc/section2/prc241.htm). With zero harms in N independent pairs, the exact one-sided 95% upper bound `1 - 0.05^(1/N)` is approximately 26%, 9.5%, 3.0%, 0.6% respectively; “all passed” is insufficient.

Never rank directly from raw PASS/reviewer rates, repairs, tokens, time, model self-assessment, human ACCEPT/REJECT, fixtures, estimated Qwen savings, missing-as-zero values, or ambiguous joins. Retain timeouts, cancellations, blocked starts and missing results in the allocation denominator with separate causes. Model-quality unknown is not PASS; operational failures still count against end-to-end reliability. Provider outage is not evidence of reasoning weakness.

Attribution permits multiple labels/UNKNOWN; attach evidence, assessor and confidence:

- `BAD_MODEL_CHOICE`: reproducible contract failure with challenger success under the same input, evaluation and budget; one pair is a hypothesis, recurrence strengthens attribution.
- `BAD_WORKFLOW_DESIGN`: evidence of broken handoff, ordering or omitted verification; fixing the workflow changes the outcome across configurations.
- `BAD_TASK_CONTRACT`: contradictory requirements, unavailable inputs or untestable acceptance criteria; correction creates a new comparison cohort.
- `UNAVOIDABLE_DIFFICULTY`: a documented external constraint or repeated failure of competent alternatives despite a valid contract; high resource use alone is insufficient.

Human Gate records release verdict separately from optional quality assessment `PASS/FAIL/UNKNOWN`, reason code and evidence references. Timing/business rejection is not model failure; unreconciled findings can survive acceptance. Missing quality assessment excludes that label, not the attempted task. Fresh same-provider review is an explicit low-risk baseline. High-risk work requires independently reproduced checks plus human domain review; cross-provider review is required only by a named risk contract, never silently substituted when unavailable. Provider diversity alone proves no independence.

## 2. Adaptive policy without self-confirmation

Promotions require human approval per cell/action and hashed evidence/policy snapshots. Freeze bindings before the first LLM node. Change one dimension per campaign; hold model/effort constant when comparing preprocessing or review depth.

| Promotion | Evidence, quality and confidence | Authority, visibility and rollback |
|---|---|---|
| Stage 0 → 1 | ≥30 observations per arm or 30 pairs; complete identity/provenance, ≥95% outcome coverage among eligible starts, exclusions disclosed; missing-outcome sensitivity preserves direction. | Recommend qualified actions, labelled observational or paired; show alternatives, N, uncertainty and reason. Human confirms each run. Abstain on drift, coverage failure or reversed evidence. |
| Stage 1 → 2 | ≥100 prospective distinct pairs for that low-risk action, across at least two time batches; 100% allocation registration and ≥98% outcome coverage. Require quality floor and benefit rule below; count unresolved outcomes conservatively. Supervisor recovery checks must pass. | Allocate among an approved model/effort whitelist for reversible work. Show selection before dispatch with override and policy version. Suspend automation on severe defect, ownership ambiguity, quality-bound failure or lost coverage. |
| Stage 2 → 3 | ≥500 distinct controlled comparison tasks for the cell, including ≥100 fresh prospective pairs since the last policy revision; ≥99% outcome coverage and complete registration/provenance. Repass the same bounds on held-out evidence. | At existing node boundaries, choose only preauthorized branches/bindings frozen for that run, with repair/time/token caps. Explain every choice; retain Human Gates. Breached cap, drift or challenger regression returns the action to Stage 1. |

For Stage 2+, predeclare a maximum 5% probability of **challenger FAIL while incumbent PASS**, with a stricter ceiling where needed. Its exact one-sided 95% upper bound must meet that ceiling; unresolved pairs count as harms. Separately require the challenger PASS lower bound to meet the human-set quality floor. Among pairs where both meet quality, require ≥10% median improvement in the chosen total-resource metric and an exact one-sided 95% sign-test lower bound above 0.5 for improvement; ties are non-improvements. These conservative rules need no causal regression model. If bounds fail, keep human selection. Inspect only prespecified batch endpoints for promotion; safety stops remain immediate. New versions/configurations need fresh qualification, not inherited N.

Maintain one small versioned frozen challenger corpus for regression detection, plus **one randomly selected eligible low-risk task in twenty** receiving a paired shadow challenger. Log eligibility, selection probability, seed/draw, veto and budget before outcomes. Rotate one challenger per campaign. Shadow work uses isolated artifacts and cannot release changes. A veto preserves the record but narrows inference. Cap additional tokens/time explicitly; insufficient budget pauses promotion rather than silently eliminating exploration. Frozen-corpus reruns test regressions but add no independent task evidence. No RL/bandit is needed.

Stage 1 may suggest all four dimensions with action-specific evidence. Stage 2 may automate MODEL/EFFORT; disabling optional preprocessing for timeout/unavailability is operational fallback. PREPROCESS ON and REVIEW DEPTH reductions require Stage 3 and separate paired qualification; mandatory gates remain. Stronger review may follow preauthorized risk rules earlier. No mid-execution rebinding or workflow invention.

Qwen `AUTO_SAFE` remains an available advisory mode, **not an unguarded default**. Require opt-in, original-input preservation, bounded latency and a circuit breaker, open now pending a controlled probe. Fix timeout duration recording before evaluating benefit. A probe establishes availability, not downstream quality or savings.

## 3. Evidence identity and ledger boundary

One `execution_id` identifies one executable invocation: provider, machine gate or invoked preprocessor. Allocate and persist it before dispatch/preprocessing; registration is not proof of process start. Run → stable logical node → executions is explicit. Custom nodes carry `subtask_id`; synthetic stable node keys cover Single Task and Custom review/gates. No generic `attempt_id`.

Retry keeps the logical node and creates a new execution with `retry_of_execution_id`; a new manual run gets a new run ID and optional explicit predecessor. Repair is a distinct execution with cycle and finding references, not a retry. Every fresh invocation gets a new ID even if a provider session is reused; session is an attribute. Interruptions close the invocation's observed lifecycle with effect/outcome UNKNOWN where necessary; never reopen that ID for another spawn. Display attempt numbers and durations may be derived.

The additive contract requires:

- Execution: schema, run/node/subtask identity, invocation kind, input/contract hashes, difficulty card, frozen selection/provenance, fixture class, nullable observed timestamps, lifecycle/outcome, artifact references/hashes and explicit unknown reasons. Never fabricate start/end times for an unobserved crash.
- Commit: `(repository_id, commit_hash)` plus producing execution references, subtask and role. Require execution marker and expected base before controlled commits; a shared aggregate commit lists contributors and its committing operation. Do not use the draft's run/hash key as universal Git identity.
- Review: reviewed base/head/artifact hashes and producer execution IDs; findings keyed by `(review_execution_id, finding_id)`. Repair names selected finding keys; delta review names repair execution/commit, original review and finding dispositions.
- Preprocess: `preprocess_id` identifies a decision, including SKIPPED; invoked work additionally has an execution ID. Downstream ID is reserved first; record consumed advisory hash or explicit non-consumption. Permit multiple preprocessing records after explicit retries, never filename joins.
- Candidate: immutable ID, repository/head or artifact manifest hashes and relevant review/check references. Human decision: unique ID, exact candidate ID, verdict, actor/time and optional quality/reason evidence. Changed candidates require new IDs; corrections append with `supersedes_decision_id`.

Keep the minimal ledger, but replace the draft event set with exactly five types:

| Event | Minimum purpose/payload |
|---|---|
| `EXECUTION_INTENT` | Durable pre-dispatch reservation and descriptor hash; detects crash before start receipt. |
| `EXECUTION_STARTED` | Observed process identity, owner token and actual start time. |
| `EXECUTION_CLOSED` | Observed exit/cancel/timeout/interruption or reconciliation, effect certainty, result hashes and observation source. |
| `COMMIT_RECORDED` | Verified repository/hash, producer links and attribution evidence. |
| `HUMAN_DECISION_RECORDED` | Decision/candidate IDs and immutable decision-artifact hash. |

Events carry schema, event/run IDs, sequence, recorded time and applicable execution ID. Serialize durable appends per run, deduplicate event IDs, preserve damaged tails and continue in numbered segments. Atomically publish results/descriptors/decisions; append reconciliation corrections referencing prior records.

Remove `RUN_STARTED` and generic `RUN_STATE_CHANGED`: run manifests/state already supply them. Candidate/preprocess/finding events add no benefit beyond immutable artifacts and explicit edges. Ledger order aids diagnosis, never proves an unrecorded action did not occur. Raw `03_STATS` artifacts remain authority; ledger supplies lifecycle observations, not execution commands, semantic state reconstruction or authorization.

Analytics compatibility is concrete: `aaw_analytics.py:187–207` lacks execution identity, and joins at lines 1071, 1128 and 1209 use run/node only. `link_confidence=HIGH` cannot certify execution uniqueness. Introduce index V2: unique non-null modern execution IDs, explicit occurrence joins, source-to-execution references and conflict diagnostics. Conflicting duplicates are quarantined, not overwritten.

Legacy rows keep null execution IDs and stable **derived row locators** from source path/hash/JSON pointer; these are not invented execution identities. Retain descriptive source-grain history and existing supported metrics. Exclude ambiguous relationship metrics, separate fixture/unknown populations, and expose corrected historical totals if old joins multiplied rows. Rebuild V2 into a temporary DB, validate referential integrity/count reconciliation, then replace the disposable index. Preserve reader API compatibility; never mutate or backfill raw historical artifacts.

## 4. Execution supervision and crash semantics

**Choose B: optional ORCA adapter and minimal AAW Execution Supervisor.** Ownership, cancellation, cross-run locks and recovery justify it. Direct CLI remains primary; experimental/version-gated ORCA must preserve one lifecycle owner per invocation.

An on-demand broker independent of GUI/runner admits frozen requests, reserves identity, spawns, persists ownership, captures stdout/stderr, applies timeout/cancel, enforces global concurrency/exclusive worktree ownership, detects stale processes and reconciles crashes. Return BUSY at capacity; existing queues submit. No chat, routing, planning, model selection, DAGs or automatic retries.

For direct execution, broker-owned Windows Job Objects provide containment and group termination; last-handle closure can terminate members. Keep handles non-inheritable and broker-owned, prevent breakaway, assign suspended children before resume, and fail admission if containment fails. These are design requirements requiring CLI-specific validation, not guarantees for arbitrary detached descendants. [Microsoft Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects). Persist host/boot identity, PID plus creation time, executable fingerprint, execution/owner token, worktree/base and output paths; PID alone never authorizes cancellation. Uncertain ownership blocks launch and reclamation.

| Crash/operation | Classification and action |
|---|---|
| RUNNING, process gone | `IDEMPOTENT_RECONCILE`: inspect immutable receipts/artifacts/Git; mark interrupted/unknown unless completion is proved. |
| Process survives GUI/runner | Broker continues only the admitted invocation; workflow advancement waits for recovered runner state. Reconnect to verified broker; unowned survivors require `HUMAN_RECONCILIATION_REQUIRED`. |
| Commit done, state write failed | `IDEMPOTENT_RECONCILE` only with marker, expected parent and verified tree; attach existing commit. Ambiguous attribution requires human reconciliation, never another commit. |
| Partial artifact | Quarantine; reconcile valid atomic outputs. `SAFE_RETRY` applies only to proven read-only deterministic regeneration into a fresh path. |
| Machine restart | Prior boot's processes are gone; reconcile disk/Git, preserve unresolved worktree reservations, never relaunch automatically. |
| Duplicate request or uncertain spawn | `AT_MOST_ONCE` dispatch per execution ID; duplicate returns receipt/status. Persisted intent without receipt requires reconciliation, not replay. |
| Provider invocation, commit, release | `AT_MOST_ONCE` dispatch; uncertain effects require human reconciliation before any new-ID retry. Local termination cannot prove remote billing/execution stopped. |

OS locks enforce exclusion while alive; durable reservations fence unresolved work after lock loss. Broker death terminates contained work and leaves reconciliation pending. There is no exactly-once claim.

## Minimum next architecture and implementation

**CURRENT:** existing runners + raw artifacts + descriptive SQLite + experimental ORCA → **NEXT:** same authorities + explicit execution evidence/lifecycle ledger + minimal process broker + rebuildable safe joins; recommendations consume frozen qualified exports later.

**KEEP:** launcher, routing, frozen bindings, workflows, queues, worktrees, GUI/Insights and Human Gates. **ADD:** identity/lineage, bounded ledger, process ownership and evidence gates. **SIMPLIFY:** duplicated lifecycle handling into one boundary. **DEFER:** automation, review reduction and preprocessing optimization until qualification. **DEPRECATE:** ambiguous execution joins and ORCA as a mandatory supervision dependency.

**First implementation: execution_id + evidence-contract patch.** Add IDs, lineage/provenance and immutable candidate/decision evidence across producers. Acceptance: repeated same-node executions remain distinct; retries, repairs, subtasks and preprocessing link exactly; old readers work; historical facts stay unchanged. Defer ledger, supervisor and policy implementation. Ledger requires identified invocations; analytics requires explicit joins; Human Gate quality population requires identified candidates; supervisor requires durable lifecycle records; recommendations require qualified observations. Identity loss cannot be repaired retrospectively.

Open risks: Windows containment, sparse outcomes and reviewer reliability; activation gates apply.

`MAX_REVIEW_REQUIRED: NONE`

`AAW_V0_4_ARCHITECTURE_READY_WITH_OPEN_RISKS`

| Required question | DECISION | CONFIDENCE | ONE-SENTENCE REASON |
|---|---|---|---|
| 1. Descriptive insights now? | YES, qualified | HIGH | Source-grain summaries remain useful with fixture, missingness and join caveats. |
| 2. Model recommendations now? | NO | HIGH | Current evidence lacks qualified comparisons and outcome lineage. |
| 3. Mandatory first evidence change? | Execution identity and explicit lineage | HIGH | Repeated invocations must never share an inferred occurrence. |
| 4. Minimal ledger? | YES | HIGH | Durable intent and observations expose unresolved effects. |
| 5. Exact ledger contents? | EXECUTION_INTENT, EXECUTION_STARTED, EXECUTION_CLOSED, COMMIT_RECORDED, HUMAN_DECISION_RECORDED | HIGH | These five types cover invocation, commit and decision reconciliation. |
| 6. Stage 1 start? | Qualified 30/cell/arm or 30 pairs | MEDIUM | Coverage and comparability permit tentative human-confirmed advice. |
| 7. Low-risk automatic selection? | Stage 2 after 100 prospective pairs and bounds | MEDIUM | Controlled evidence plus recovery gates limits unsupported allocation. |
| 8. Qwen AUTO_SAFE? | Conditional opt-in; breaker open now | HIGH | Timeout evidence defeats an unguarded default. |
| 9. ORCA role? | Optional, experimental adapter | HIGH | Its messaging surface is unnecessary for the identified gaps. |
| 10. Execution Supervisor? | YES, after evidence foundation | HIGH | Durable ownership and cancellation remain materially missing. |
| 11. Largest risk? | Self-confirming false model ranking | HIGH | Allocation bias and unreliable labels can turn descriptive errors into policy. |
| 12. Highest-value next task? | execution_id + evidence-contract patch | HIGH | Every later capability depends on trustworthy occurrence identity. |
