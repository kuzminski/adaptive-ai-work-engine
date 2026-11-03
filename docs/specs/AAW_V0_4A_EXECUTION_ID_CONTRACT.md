# AAW V0.4A — execution identity contract

## Identity

`execution_id` identifies exactly one executable invocation. The format is `EXE_<32 lowercase UUIDv4 hex characters>` (for example, `EXE_0123456789abcdef0123456789abcdef`). It is generated locally and is independent of provider, time, run, node, model, profile and provider session.

The runner allocates the ID and atomically persists `03_STATS/<run_id>/EXECUTIONS/<execution_id>.json` before dispatch. The immutable identity fields are schema, execution/run/node/subtask identity, invocation kind and creation time. Observed session/status fields may be atomically completed after the process returns. A persisted reservation proves intent, not process start.

`run_id` identifies the enclosing run. `node_id` identifies a logical workflow node. Repeating a node creates another execution ID. A genuine retry keeps run/node IDs, receives a new execution ID and sets `retry_of_execution_id`; REPAIR is not a retry. `provider_session_id` is nullable observed provider metadata and never identity.

Invocation kinds are bounded to `LLM`, `MACHINE_GATE`, `PREPROCESS`, `REVIEW`, `REPAIR`, `DELTA_REVIEW`, and `PLAN`. IMPLEMENT/SUBTASK remain role/node attributes of `LLM`.

## Producer matrix

| Producer | Logical node | Invocation created at | Session ID | Artifact | V0.4A behavior |
|---|---|---|---|---|---|
| `aaw_run_v0_1.py` direct worker | requested semantic node | immediately before direct CLI dispatch | provider result | node telemetry + receipt | descriptor reserved; ID exposed |
| Codex/override classifier | `CLASSIFIER` | before classifier command | Codex thread when observable | classifier telemetry or descriptor | descriptor reserved; ID exposed in telemetry |
| `workflow_runner.py` LLM | workflow node ID | before optional preprocess and provider CLI | provider result | descriptor, telemetry, node result, state ref | descriptor reserved; lineage attached |
| workflow machine gate | workflow node ID | before subprocess | null | descriptor, node result, state ref | independent execution ID |
| `custom_job_runner.py` PLAN/LLM/review/repair/delta | explicit role or `subtask_id` | before optional preprocess and adapter | provider result | descriptor + state/result/telemetry refs | explicit subtask and lineage |
| Custom Job machine gate | explicit subtask/final gate key | before subprocess | null | descriptor + gate result/state ref | independent execution ID |
| `local_preprocess.py` | `<downstream node>:PREPROCESS` | after availability check, before Qwen inference | null | preprocess decision/output + descriptor | `preprocess_id` remains distinct; skipped decisions have no execution ID |
| Control Center queue dispatcher | queue task | delegates to authoritative runner | n/a | queue state | no invented workflow node or duplicate execution identity |
| ORCA supervised path | existing ORCA task | existing experimental adapter | ORCA IDs | existing receipt | intentionally unchanged by V0.4A |

## Lineage

- Run → node → execution is recorded by the descriptor and a compact `executions[]` state reference.
- Custom Job requires explicit `subtask_id`; it is never inferred from a filename. Controlled commits record repository path, commit hash, subtask, producer execution IDs, expected parent and role. Git identity is repository plus commit hash.
- REVIEW records producer execution IDs and reviewed base/head/artifact references. Each finding is keyed by `(review_execution_id, finding_id)`.
- REPAIR records its originating review and selected finding keys. It has a new execution ID and no retry relation unless an actual retry is explicitly requested.
- DELTA_REVIEW records the repair execution, original review and represented finding dispositions.
- Every preprocess decision has a `PRE_` UUID-backed `preprocess_id`. Only invoked Qwen work has an execution ID. It records the downstream node and reserved downstream execution, source references, output hash and reliable consumption state.

## Candidate and human decision

A candidate uses a UUID-backed `CAN_` identity and binds run, repository/worktree, candidate HEAD or manifest, and review/check execution references. Content identity is hashed; changed content requires a new candidate. A human decision uses an `HDE_` identity and references the exact candidate plus verdict and timestamp. ACCEPT is a release verdict, not a model-quality PASS.

## Legacy and analytics

PRE-V0.4A artifacts remain readable and unchanged. Missing legacy execution identity stays null/absent; no ID is synthesized from timestamps, filenames, sessions or row locators.

The disposable descriptive analytics index is V1.1-compatible: nullable execution/preprocess identity columns are additive, and historical metrics/joins remain V1. A unique partial index protects non-null modern execution IDs. A conflicting modern duplicate is rejected from derived rows and emitted as `DATA_INTEGRITY_ERROR`; it is never overwritten. Execution-grain joins, causal/recommendation analysis and Analytics V2 migration remain deferred.

## Invariants

1. Every spawn/inference has one newly generated execution ID; a second invocation never reuses it.
2. Same logical node, timestamp, provider session, model or profile does not imply same execution.
3. Retry creates a new ID with `retry_of_execution_id`; REPAIR does not implicitly set it.
4. Machine gates and invoked preprocessors have independent IDs.
5. Multi-subtask execution always carries explicit `subtask_id`.
6. Review, finding, repair and delta-review relations use explicit execution IDs.
7. Historical evidence is not backfilled.
8. Candidate decisions reference exact immutable candidate IDs.
9. Descriptors/state/results are atomically published; no JSONL lifecycle ledger or Execution Supervisor is introduced in this patch.

## Known boundary

V0.4A does not change the experimental ORCA path, add lifecycle events, supervise processes, implement automatic retries, or create Analytics V2. A descriptor left `RESERVED` after a crash is intentionally unresolved evidence, not proof that the process started or did not start.
