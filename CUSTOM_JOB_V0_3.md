# AAW Custom Job V0.3

Custom Job is a dependency-free, static execution specification. It extends rather than replaces `SINGLE TASK` and the V0.2 workflow runner.

## Modes

- `SINGLE_IMPLEMENTATION`: IMPLEMENT → machine gates → REVIEW → optional selected REPAIR → DELTA_REVIEW → HUMAN GATE.
- `MULTI_SUBTASK`: sequential fresh sessions in one registered isolated worktree; each accepted subtask is followed by a runner-owned checkpoint commit, then one final semantic review.
- `MULTI_STAGE`: persisted ordered composition of `SINGLE_TASK`, `WORKFLOW`, or `CUSTOM_JOB` references. V0.3 validates and dry-runs the composition; child stages require explicit human starts.

No mode merges, pushes, amends prior commits, creates a PR, or changes the model binding mid-run.

## Model layers and access

`MODEL_CATALOG.json` records runtime/account facts, effort support and billing class. `MODEL_REGISTRY.json` remains capability policy. `IMPLEMENTER_PROFILES.json` contains convenient GUI presets.

Default billing policy is `NO_EXTRA_PAID_USAGE`. Subscription-included models can start. Credit-required and API-only models are blocked. V0.3 does not store API keys or fall back to API billing.

Bindings use `HUMAN_OVERRIDE` or `WORKFLOW_DEFAULT` and are frozen into `frozen_job.json` before the first LLM node. `ADAPTIVE_POLICY` is reserved for a future policy engine using the same runner contract; no adaptive selection exists in V0.3.

## Planning

PLAN is optional. Default approval is `HUMAN_APPROVAL`; a successful plan stops at `WAITING_FOR_PLAN_APPROVAL`. PLAN is read-only and may not expand the predefined scope. Astra is manual-only and intended for exceptional planning, architecture and adjudication, never everyday automatic implementation.

## Multi-subtask and commits

Each subtask gets the frozen goal, its own node contract, current Git state, prior checkpoint list and structured prior results. It does not receive another provider session's chat history. Each provider invocation is fresh (`--ephemeral` for Codex, `--no-session-persistence` for Claude).

The runner records HEAD before each subtask, runs configured local machine gates, verifies that the provider did not change history, and creates `AAW: <subtask-id> <short-title>`. Canonical checkout HEAD/status are checked around the run.

## Review, repair and delta review

Final review receives baseline, HEAD, commit list, structured subtask results, machine evidence, changed files and `git diff baseline..HEAD` in a fresh read-only session. Its findings have stable IDs.

On FAIL, default `HUMAN_SELECTED` stops at `WAITING_FOR_REPAIR_SELECTION`. The operator passes selected finding IDs back to the runner. Repair sees only that selected scope and creates a separate repair commit in multi-subtask mode. Delta review receives original/selected findings, repair range, before/after diff and machine evidence.

## Queue integration and artifacts

A queue task with mode `CUSTOM_JOB` persists the full job-spec reference, delegates to `custom_job_runner.py`, and uses existing STOP/CONTINUE semantics. All Human Gate statuses stop the queue.

Artifacts are stored under `03_STATS/<AAW_RUN_ID>/CUSTOM_JOB/`: frozen spec/bindings, state, subtask results, commits, tests, review, selected repairs, delta review, provider sessions and token/wall-time telemetry.

## Commands

```powershell
python custom_job_runner.py --validate JOBS\job.json
python custom_job_runner.py --job JOBS\job.json --dry-run
python custom_job_runner.py --job JOBS\job.json
python custom_job_runner.py --run-id AAW_... --select-finding F001
python custom_job_runner.py --run-id AAW_... --human-verdict accept
```

Human acceptance means only `READY_FOR_EXTERNAL_INTEGRATION`; it never means merge.
