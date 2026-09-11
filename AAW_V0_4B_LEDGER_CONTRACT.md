# AAW V0.4B — execution lifecycle ledger contract

Schema version: `AAW_EXECUTION_LEDGER_V0.4B`. Identity contract: unchanged
`AAW_EXECUTION_DESCRIPTOR_V0.4A`.

## 1. Purpose

The ledger records **observed lifecycle facts** about invocations so that AAW
can later diagnose a crash, reconcile an uncertain effect, and give a future
Analytics V2 and Execution Supervisor a durable lifecycle boundary to join on.

It is deliberately **not** event sourcing. It is not a message bus. It is not a
database. It holds five event types and nothing else.

## 2. Non-authority rule

The ledger is **not execution authority** and **workflow state is never
reconstructed from it**.

| Question | Authority |
|---|---|
| What was this invocation? | `03_STATS/<run>/EXECUTIONS/<execution_id>.json` (V0.4A descriptor) |
| What did the node decide? | node result / job state / workflow state |
| What is in Git? | Git |
| What did the human decide? | the immutable `HDE_*.json` decision artifact |
| What was *observed* about the invocation's lifecycle? | this ledger |

If the ledger and a raw artifact disagree, the raw artifact wins and the
disagreement is a diagnostic, not a state change.

## 3. Location and file shape

```
03_STATS/<RUN_ID>/LEDGER/execution_events.jsonl
03_STATS/<RUN_ID>/LEDGER/execution_events.jsonl.lock
```

One ledger per run: local containment, no global writer contention, corruption
isolated to one run, easy to rebuild or discard. Line-delimited JSON, one event
per line, append-only.

## 4. The five event types

`EXECUTION_INTENT`, `EXECUTION_STARTED`, `EXECUTION_CLOSED`,
`COMMIT_RECORDED`, `HUMAN_DECISION_RECORDED`.

There is no `RUN_STARTED`, `NODE_STARTED`, `NODE_FINISHED`, `MODEL_BOUND`,
`PREPROCESS_FINISHED`, `REVIEW_FINISHED`, `FINDING_RECORDED` or
`STATE_CHANGED`. Where that information already exists in an immutable
artifact, the ledger references the artifact instead of restating it.

Consequently:

- **Preprocess** is not a ledger concept. An *invoked* preprocess is an
  execution and takes the three generic execution events. A *skipped*
  preprocess has a `PRE_` decision ID, no execution ID, and no ledger event.
- **Machine gates** are executions. They take the three generic events. There
  is no `MACHINE_GATE` event type; the gate's semantic result stays in the gate
  artifact and state.
- **Review / repair / delta review** are executions. Their semantic
  relationships stay in the V0.4A descriptor `relations` and finding keys. The
  ledger tracks only their invocation lifecycle.

## 5. Event envelope

```json
{
  "schema_version": "AAW_EXECUTION_LEDGER_V0.4B",
  "event_id": "EVT_<32 lowercase uuid4 hex>",
  "event_type": "EXECUTION_INTENT",
  "run_id": "AAW_YYYYMMDD_HHMMSS_xxxxxxxx",
  "sequence": 1,
  "recorded_at": "2026-09-06T20:40:17.621+02:00",
  "execution_id": "EXE_<32 hex> | null",
  "payload": { }
}
```

`event_id` is UUID-backed and locally generated, matching AAW identity style.
**A timestamp is never identity.** `execution_id` is required for the three
`EXECUTION_*` types and null for the other two.

## 6. Sequence

`sequence` is a per-run, monotonically increasing integer starting at 1,
allocated under the ledger lock from the last complete line on disk.

**`sequence` expresses ledger append order only.** It does not prove real-world
causal ordering, process start time, completion time, or the absence of an
unrecorded side effect. For elapsed durations, keep using the monotonic timing
the execution paths already record in telemetry.

Concurrency: AAW currently has one ledger writer per run. That assumption is
**enforced, not trusted** — `_LedgerLock` takes an in-process `RLock` plus an
OS file lock (`msvcrt.locking` on Windows, `fcntl.flock` elsewhere) on a
sidecar `.lock` file, and the sequence is read from disk inside the lock. This
is a minimal local lock; there is no distributed locking.

## 7. Durability

Appends are `O_APPEND` writes followed by `os.fsync`. The complete JSONL file is
never rewritten for an event. A short write raises rather than reporting
success. **An event reported as appended is on disk, not only in Python
buffers.**

## 8. Damaged tail

A crash can leave a partial final line. On read:

- every valid preceding event is retained;
- an unparseable **final** line with no terminating newline is classified
  `LEDGER_DAMAGED_TAIL` (a warning: the tail is damaged, the ledger is not);
- an unparseable **interior** line is `LEDGER_DAMAGED_RECORD` (an error);
- a parsed final line with no terminating newline is
  `LEDGER_UNTERMINATED_FINAL_RECORD`;
- partial content is retained verbatim and is **never interpreted**;
- **reads never rewrite historical ledger content.**

A later append *seals* a damaged tail by prefixing one newline, so the partial
bytes stay verbatim on their own line and keep reporting as damaged. That is an
append, not a rewrite — nothing is edited or deleted.

## 9. Deduplication

`event_id` is unique within a run ledger.

- Duplicate ID with **identical** content → idempotent; it does not create a
  second semantic observation.
- Duplicate ID with **different** content → `LedgerIntegrityError`,
  `classification = "DATA_INTEGRITY_ERROR"`.

There is no last-write-wins and no deduplication by timestamp or payload
similarity.

## 10. Lifecycle semantics

### EXECUTION_INTENT

Written after the V0.4A descriptor is durably allocated and **before** any
provider or process dispatch.

Payload: `execution_id`, `node_id`, `subtask_id`, `invocation_kind`,
`descriptor_path` + `descriptor_hash`, `identity_contract`, provider / harness /
model / effort / profile, `input_contract_hash`, repository / worktree.

> Means: AAW durably registered the intention to perform this exact invocation.
> Does **not** mean: a spawn occurred, a provider received a request, or billing happened.

**Fail-closed.** If the intent append fails, nothing is dispatched. Without
durable intent, later reconciliation loses its primary lifecycle boundary.

### EXECUTION_STARTED

Written only on positive evidence that execution started. Never inferred from
intent. Per adapter, `start_evidence` means exactly:

| Adapter | `start_evidence` | Proves |
|---|---|---|
| Direct CLI (`codex` / `claude`) | `CHILD_PROCESS_SPAWNED` | AAW spawned the child and received its PID and OS creation time |
| Machine gate | `CHILD_PROCESS_SPAWNED` | same |
| Local Qwen HTTP | `HTTP_REQUEST_DISPATCH_INITIATED` | AAW dispatched a local request after a successful availability precheck |

Only a spawn explicitly marked as the invocation's **dispatch**
(`dispatch=True` at the launching call site) is reported. Helper processes an
adapter or runner happens to spawn inside the same observation scope — a
`git rev-parse` while assembling a package, for instance — are never reported,
so a helper can never be recorded as an execution's start evidence. An adapter
that launches its own provider process must mark that call itself; an adapter
that spawns nothing produces no `EXECUTION_STARTED` at all, and its close then
carries `observation_source = IN_PROCESS_ADAPTER_RETURN`.

`process_creation_time` comes from the OS (`GetProcessTimes` on Windows,
`/proc/<pid>/stat` on POSIX) and is `null` with
`process_creation_time_source = UNAVAILABLE_NO_PROCESS_OWNERSHIP_API` when the
platform does not expose it. **It is never substituted with the runner's own
clock.** For HTTP there is no child process and `process_id` stays `null`; no
PID is fabricated and no claim is made about remote provider internals.

### EXECUTION_CLOSED

The strongest observed terminal fact. Payload: `observed_close_time`,
`close_reason`, `effect_certainty`, `observation_source`, `exit_code`,
`outcome`, `timed_out` / `cancelled` / `interrupted`, `result_refs`, `detail`.

Close reasons: `COMPLETED`, `FAILED`, `TIMEOUT`, `CANCELLED`, `INTERRUPTED`,
`RECONCILED`, `UNKNOWN`. Detailed provider errors belong in referenced
artifacts and telemetry, not here.

Effect certainty: `CONFIRMED`, `PARTIAL`, `UNKNOWN`.

Observation sources: `CHILD_PROCESS_EXIT`, `ADAPTER_RESPONSE`,
`IN_PROCESS_ADAPTER_RETURN`, `RUNNER_EXCEPTION`, `SPAWN_FAILURE`,
`PRE_DISPATCH_FAILURE`, `RECONCILIATION`, `LEDGER_UNCERTAIN`, `UNKNOWN`.

Distinctions the taxonomy keeps visible:

- provider returned successfully ≠ workflow state advanced;
- process exited ≠ all side effects known (a killed timeout is `PARTIAL`);
- timeout or cancel requested ≠ a remote provider definitely stopped.

Completion is never invented when evidence is missing.

### COMMIT_RECORDED

Emitted only for a runner-owned commit AAW has positive Git evidence for.
Commit identity is **(repository, commit_hash)** — never the hash alone.
Payload also carries `expected_parent`, `producer_execution_ids`, `subtask_id`,
`role`, and observed `git_evidence` + hash.

> Means: AAW observed and recorded this Git commit.
> Does **not** mean: the workflow accepted it.

If a commit exists but its event write failed, reconciliation may append
`COMMIT_RECORDED` later after verifying exact Git evidence. **Another commit is
never created.**

### HUMAN_DECISION_RECORDED

Emitted only **after** the immutable human decision artifact is durably
written. Payload references `human_decision_id`, `candidate_id`, the artifact
path and its SHA-256, `verdict`, and optional `quality_assessment` / `reason`.

The artifact remains the detailed authority. `ACCEPT` is a release verdict and
is never transformed into a model-quality `PASS`.

## 11. Valid lifecycle shapes

```
descriptor → INTENT → STARTED → CLOSED          normal
descriptor → INTENT                              unknown whether it started
descriptor → INTENT → STARTED                    known started, terminal state unresolved
descriptor → INTENT → CLOSED                     only via explicit reconciliation,
                                                 provable never-start, or an adapter
                                                 that owns no process
```

A `CLOSED` without a `STARTED` is a validation error unless
`observation_source` is `RECONCILIATION`, `SPAWN_FAILURE`,
`PRE_DISPATCH_FAILURE`, or `IN_PROCESS_ADAPTER_RETURN`. `STARTED` is **never**
synthesised retroactively to make a sequence look tidy.

## 12. Failure semantics

| Failure | Behaviour | Classification |
|---|---|---|
| INTENT write fails | fail closed, **no process launches** | `LEDGER_WRITE_ERROR` |
| Second INTENT for one execution ID | refused | `AT_MOST_ONCE_DISPATCH_VIOLATION` |
| STARTED write fails | **no second process**; the state requires reconciliation and the later close carries `start_record_status` | `EXECUTION_STARTED_LEDGER_UNCERTAIN` |
| CLOSED write fails | **no provider retry**; the invocation may already have produced effects | `CLOSE_RECORD_UNCERTAIN` |
| Incompatible duplicate event ID | refused, original preserved | `DATA_INTEGRITY_ERROR` |

Runner state files carry a `lifecycle_records[]` entry per execution with
`start_record_status`, `close_record_status` and `requires_reconciliation`, so
an uncertain process is never silently forgotten.

## 13. At-most-once dispatch boundary

**AAW does not promise exactly-once execution.**

For the integrated runner path: if durable `EXECUTION_INTENT` already exists for
an `execution_id`, a second dispatch for that ID is refused. A deliberate retry
gets a **new** execution ID; an execution ID is never reused for replay.

There is **no automatic crash replay** and no automatic provider retry.

## 14. Crash windows

| Window | Ledger shows | Interpretation |
|---|---|---|
| A. descriptor + intent persisted, process never starts | `INTENT` only | unknown whether it started |
| B. process starts, runner crashes before result | `INTENT` + `STARTED` | known started, terminal state unresolved |
| C. provider returns or commit exists before state persistence | existing immutable evidence may later support reconciliation | never automatically replayed |
| D. partial final JSONL line | earlier events readable + `LEDGER_DAMAGED_TAIL` | tail damaged, ledger not discarded |

## 15. Validation

`validate_ledger(path, run_id)` detects: malformed events, unknown schema,
duplicate event IDs, invalid sequence, `STARTED` for an unknown execution
identity, `CLOSED` for an unknown execution identity, contradictory terminal
events, damaged tails and damaged records, incompatible duplicate commit
events, and malformed human-decision payloads.

**A missing `CLOSED` after a crash is valid unresolved evidence, not ledger
corruption.** It is reported under `unresolved`, never under `errors`.

## 16. Reconciliation minimum

Read-only helpers only — no automatic repair, no replay:

`intent_without_started()`, `started_without_closed()`, `unresolved()`,
`close_status(execution_id)`, `commits_for_execution(execution_id)`,
`human_decisions()`, `lifecycle()`, `summary()`.

Diagnostic CLI:

```bash
python execution_ledger.py --list
python execution_ledger.py --run-id AAW_20260906_204017_0b07e948
python execution_ledger.py --run-id AAW_20260906_204017_0b07e948 --validate
python execution_ledger.py --run-id AAW_20260906_204017_0b07e948 --unresolved
```

## 17. Clocks

`recorded_at`, `observed_start_time` and `observed_close_time` are diagnostic
wall-clock readings. They are never identity and never semantic ordering;
`sequence` carries append order. No cross-machine clock assumption is made.

## 18. Legacy

Historical runs are unchanged. **There is no backfill and no synthetic event
for an old run.** A run without a ledger is `PRE_V0_4B` / `LEGACY`, not
corrupted; reading it creates nothing. Old lifecycle is never inferred from
timestamps.

## 19. Analytics boundary

> **Superseded by V0.4C** ([Analytics V2 contract](AAW_V0_4C_ANALYTICS_V2_CONTRACT.md) §8).
> Analytics V2 now *indexes* the ledger — source layout `LEDGER`, status
> `INDEXED` — and derives `execution_lifecycle` rows from it through this
> module's own reader and validator. The authority rule is unchanged: a ledger
> event never becomes execution telemetry, never carries execution identity, and
> the observed close outcome is stored apart from the semantic node verdict. The
> paragraph below records the original V0.4B-era boundary.

As introduced in V0.4B, the descriptive analytics index classified
`03_STATS/*/LEDGER/*` as layout `LEDGER`, status `SKIPPED`, detail *"V0.4B
execution lifecycle ledger; known non-indexed observed lifecycle evidence, not
execution telemetry"*. It was recognised explicitly rather than accidentally
ignored, produced no execution rows, and raised no `UNKNOWN_LAYOUT` diagnostic.
No Analytics V2 and no new policy metric was introduced in V0.4B.

## 20. Compatibility boundary

V0.4B **adds** a ledger. It does not change V0.4A identity semantics: no new
descriptor field, no change to `EXE_`/`PRE_`/`CAN_`/`HDE_` identity, no change
to retry/repair semantics, and no change to descriptor status handling. A
V0.4A-only reader ignores the `LEDGER/` directory entirely.

## 21. Future Supervisor use

The ledger gives a future Execution Supervisor durable, honest anchors:
executions with intent but no start, executions started but not closed, close
status with effect certainty, commit records per producer execution, and human
decision references. It does **not** yet provide process ownership tokens,
cancellation, supervision, or automatic recovery — and an execution ID must
never by itself authorise cancellation.
