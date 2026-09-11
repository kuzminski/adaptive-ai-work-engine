# Adaptive AI Work Engine (AAW)

Adaptive AI Work Engine is a local, evidence-oriented framework for routing a
bounded task through static AI-assisted workflows. It keeps deterministic
routing, execution bindings, workflow state, and human approval distinct. AAW
does not merge code, push branches, create pull requests, or silently replace a
human decision.

## Maturity

**Early alpha.** The project is useful for controlled local experiments and
operator-led workflows, not yet for unattended production automation.

### What works now

| Capability | Status |
| --- | --- |
| Deterministic routing | IMPLEMENTED |
| Static workflows | IMPLEMENTED |
| Human Gate | IMPLEMENTED |
| Control Center | PARTIAL |
| Execution identity | PARTIAL |
| Descriptive analytics | PARTIAL |
| ORCA supervision | EXPERIMENTAL |
| Local Qwen preprocessing | EXPERIMENTAL |
| Dynamic planner | PLANNED |
| Repair merge/rejoin | PLANNED |

## Architecture

`aaw_run_v0_1.py` is the single-task entry router. It uses deterministic
classification first and invokes a cheaper classifier only for ambiguity.
`workflow_runner.py` executes declared, static workflow graphs with frozen
per-run bindings. `custom_job_runner.py` runs a fixed multi-stage job.
`execution_contract.py` and `execution_ledger.py` record identity and observed
lifecycle evidence. The optional Control Center is a local desktop interface;
its analytics index is disposable and never becomes execution authority.

Every route that needs approval ends at a Human Gate. A Human Gate can record
an acceptance candidate but cannot merge, push, or open a pull request.

## Installation

AAW currently targets Python 3.12+ and uses the standard library. Clone the
repository, then run commands from its root:

```powershell
python -m pytest -q (Get-ChildItem -File -Filter 'test_*.py' | Sort-Object Name | ForEach-Object FullName)
```

Runtime output defaults to `output/03_STATS` and routing artifacts default to
`output/routing`; both are ignored by Git. The bundled model registry is
intentionally empty: an operator must configure any automatic entry-router
binding. Configure an external registry, contracts, or shared evidence root
explicitly when your environment provides them:

```powershell
$env:AAW_MODEL_REGISTRY = 'C:\path\to\MODEL_REGISTRY.json'
$env:AAW_PLAYBOOK_ROOT = 'C:\path\to\playbook'
$env:AAW_STATS_ROOT = 'C:\path\to\aaw-evidence'
```

Available overrides are `AAW_ROOT`, `AAW_EXTERNAL_ROOT`, `AAW_PLAYBOOK_ROOT`,
`AAW_MODEL_REGISTRY`, `AAW_CLASSIFIER_PROMPT`, `AAW_STATS_ROOT`,
`AAW_ROUTING_ROOT`, `AAW_CONTROL_CENTER_STATE`, and `AAW_ANALYTICS_DB`.

## Minimal workflow

Validate a bundled static workflow, then make a dry run against an isolated
repository/worktree. A suitable model registry must be configured for a run
that dispatches model-backed nodes.

```powershell
python workflow_runner.py --validate WORKFLOWS\IMPLEMENT_REVIEW_REPAIR_V1.json
python workflow_runner.py --workflow WORKFLOWS\IMPLEMENT_REVIEW_REPAIR_V1.json `
  --goal 'Repair the CSV import regression within the stated scope.' `
  --repo C:\path\to\repo --worktree C:\path\to\repo-worktree --dry-run
```

The Control Center can be started locally with:

```powershell
python CONTROL_CENTER\aaw_control_center.py
```

## Trust and security model

AAW treats raw result JSON, explicit execution IDs, and explicit human-decision
IDs as authority. It does not infer lineage from filenames, timestamps, or
"latest" artifacts. Runners use explicit argv execution rather than shell
composition. Local runtime state, queues, SQLite indexes, journals, and
evidence are excluded from source control by default. See `SECURITY.md` for
reporting guidance and operating boundaries.

## Known limitations

- The project is Windows-oriented; Control Center needs a working Tk runtime.
- External model/playbook contracts are configured by the operator and are not
  bundled in this repository.
- ORCA supervision and local Qwen preprocessing are opt-in experiments, not
  policy authority or automatic fallbacks.
- Static workflow branches do not yet merge or rejoin automatically.
- Focused green tests do not prove provider availability, runtime credentials,
  or a release authorization.

## Roadmap

Near-term work focuses on completing portable configuration, stabilizing the
Control Center and execution-identity surfaces, and improving descriptive
analytics. Dynamic planning and repair merge/rejoin remain planned and require
separate design and safety gates.

## Test status

The pre-publication baseline recorded **225 root tests passed**. Re-run the
root-only command above (rather than recursive discovery) because local smoke
worktrees are deliberately excluded from the public tree. Control Center and
analytics checks are also run as a separate publication gate.

## License

Apache-2.0. See `LICENSE`.
