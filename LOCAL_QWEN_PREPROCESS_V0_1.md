# AAW local Qwen preprocess V0.1

## Purpose and authority

This is an optional execution-support layer between raw inputs/evidence and a
frontier workflow node. It exists only where a compact, stable handoff can
reduce initial frontier context or make irregular material easier to inspect.
Its results are `ADVISORY_SUPPORTING_ARTIFACT`, never a workflow node or gate.
Qwen cannot PASS/FAIL, choose bindings or scope, change goals/acceptance,
resolve findings, or replace a human decision.

The order of work is fixed: deterministic code first, local Qwen second,
frontier model third. The policy does not invoke Qwen where parser, regex,
Git, or bounded deterministic extraction is equally sufficient.

## Types

- `LOCAL_QWEN_TASK_NORMALIZE`: messy long raw task to non-authoritative metadata.
- `LOCAL_QWEN_SUMMARY`: compact handoff across several prior results.
- `LOCAL_QWEN_JSON`: prose-only / irregular source normalization.
- `LOCAL_QWEN_LOG_TRIAGE`: only after deterministic error/test/traceback extraction.
- `LOCAL_QWEN_DIFF_TRIAGE`: a review map, never a replacement for Git diff review.
- `LOCAL_QWEN_FINDINGS_PREP`: preserves original findings and makes overlap explicit.
- `LOCAL_QWEN_DELTA_ASSIST`: experimental precheck before the frontier delta reviewer.

## Policy and thresholds

`AUTO_SAFE` is the default; `OFF` records no call; `CUSTOM`/`MANUAL` can pin a
per-node type and frozen profile. Conservative defaults are: task 4,000 chars,
handoff 9,000 chars or 3 prior artifacts, log 12,000 chars, diff 16,000–30,000
chars, and at least 5 findings. Every run records a concrete reason such as
`INPUT_TOO_LARGE`, `MANY_PREVIOUS_RESULTS`, `LONG_MACHINE_LOG`,
`MANY_FINDINGS`, `MULTI_COMMIT_REVIEW`, or `MANUAL_USER_SELECTION`; otherwise
it is `SKIPPED_NOT_USEFUL`.

## Evidence, failure behavior, telemetry

Artifacts live in `03_STATS/<run>/PREPROCESS/`. Each retains a source snapshot,
source path and SHA-256, model/profile/type, created timestamp, reason, output
and telemetry. Frontier prompts receive the compact artifact plus source paths
and an explicit instruction to inspect originals when required; they do not
receive both the full snapshot and its summary.

AUTO_SAFE local failure or unsafe bind records `SKIPPED_LOCAL_UNAVAILABLE` and
continues with the normal package. Required CUSTOM/MANUAL local preprocessing
records `BLOCKED` / `LOCAL_LLM_UNAVAILABLE`; there is no provider fallback.
The adapter validates localhost-only bind and never starts AnythingLLM.

Telemetry includes type, profile, provider/model, status, reason, source count,
input/output chars and tokens when supplied by the runtime, wall time,
downstream node ID, and before/after input estimates (clearly estimates).

## UI and future policy

Workflow and Custom Job show a small `Local preprocessing` selector:
`AUTO_SAFE`, `OFF`, or `CUSTOM`. The Custom Job advanced choices expose only
node-eligible Qwen assistance. Qwen remains absent from normal implementer
pickers; LOCAL profiles are visible in advanced model status only. Custom Job
freezes `preprocess_policy` and per-node `preprocess` alongside execution
bindings at START.

This deterministic policy is intentionally simple. A future adaptive policy
may use recorded telemetry to adjust thresholds or request full-source review,
but no learning/automatic policy is implemented in V0.1.
