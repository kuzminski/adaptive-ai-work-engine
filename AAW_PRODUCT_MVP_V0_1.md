# AAW Product MVP V0.1

A local product layer for a normal user on top of the frozen
`AAW_DEFAULT_AUTONOMOUS_POLICY_V0_3` engine (V0.3 `604266a`, V0.3.2 freeze
`961e7f6`). The autonomy engine is not rebuilt: execution, policy, ledger,
run lock, resume reconciliation and the Human Gate are the V0.3 modules.

## User flow

download `AAW-Windows-x64.zip` → unzip → `AAW\AAW.exe` → the app opens in the
browser (127.0.0.1) → providers detected → **Nowe zadanie** (folder, goal,
first iteration, direction, three levels) → pre-run summary → **START** →
live process view → **STOP SAFELY / RESUME** → **Human Gate**.

Navigation: Home · Nowe zadanie · Zadania · Ustawienia. Technical details,
raw evidence, exact role profiles and the operator console (Control Center,
source checkout only) are under "Zaawansowane" / "Szczegóły techniczne".

## Files

| File | Role |
|---|---|
| `AAW.py` | entry point / `AAW.exe`: app server, `--run-worker`, `--detect`, `--self-test`, single instance |
| `product_server.py` + `PRODUCT_UI/` | loopback HTTP API + static single-page UI (stdlib only) |
| `product_providers.py` | CLI detection (FOUND, version, login via the CLI's own status command, runnable profiles); `ProviderSpec` registry for later providers |
| `MODEL_RECOMMENDATIONS.json` + `product_recommendations.py` | versioned built-in catalog, simple levels → V0.3 policy slots, optional GitHub update |
| `product_runs.py` | task → mandate, isolated worktree, background worker, STOP/RESUME, Human Gate actions, continuation |
| `product_view.py` | read-only projections: Home, process view, phase briefs, timeline, Human Gate, raw evidence |
| `product_home.py` | per-user data folder (`%LOCALAPPDATA%\AAW`) and settings |
| `packaging/` + `.github/workflows/aaw-portable.yml` | PyInstaller one-folder build, zip, Windows CI |
| `product_fake_cli.py` | test-only stand-in for `claude`/`codex` |
| `test_product_mvp.py`, `test_autonomy_safe_stop.py` | product and engine-hook tests |

## Authority (no second engine, no second state authority)

The only run authority stays `AUTONOMY/autonomy_state.json`,
`AUTONOMY/autonomy_events.jsonl` and the V0.4B ledger. The product writes only
input/request records under `<run>/PRODUCT/` (`task.json`, `stop_request.json`,
`worker*.json`, `decision.json`), which the engine never reads. Briefs,
timeline, status and the Human Gate page are computed on read from engine
artifacts and link to the raw evidence (descriptor, ledger lifecycle, result
artifact, journal events).

## Engine touch points (additive, policy unchanged)

### 1. Cooperative safe stop

`AutonomyController.run` honours the existing `run_cancellation` token
(already used by `workflow_runner`), additively:

* at a phase boundary → `RUN_PAUSED` in the journal, the next phase is
  persisted, `status` stays `RUNNING`, lock released; `resume` continues;
* a call terminated by the token → the execution is closed `CANCELLED` in the
  ledger (`effect_certainty` UNKNOWN if the process had started, CONFIRMED
  with `PRE_DISPATCH_FAILURE` if it never spawned), `RUN_CANCELLED_IN_FLIGHT`
  is journaled and `in_flight` is kept, so V0.3 resume rules decide.

Without a token in scope nothing changes (full regression unchanged).

### 2. Planner handoff contract (found by the live walkthrough)

The V0.3 freeze never ran the initial architect live (it was scripted). The
first real product runs exposed two contract-communication gaps in
`autonomy_adapters` — the validators were right, the prompt never told the
model the vocabulary they enforce:

* attempt 1 — the architect paraphrased the mandatory Human Gate conditions →
  `MANDATE_EXTENSION_ATTEMPT`. Now the INITIAL_ARCHITECT handoff carries
  `REQUIRED_HUMAN_GATE_CONDITIONS` and a `DIRECTIONAL_CHARTER_TEMPLATE` (the
  verbatim copy fields; `autonomy_contract.directional_charter_template`);
* attempt 2 — the planner invented decision kinds (`scope_selection`) →
  `DECISION_REQUIRES_HUMAN`, and listed a direction item that was only waiting
  for its dependency in `skipped_items` (permanent). Now every planner handoff
  carries `DECISION_KINDS` (the existing `LEVEL_BY_KIND`) and the instruction
  states that skipping is permanent.

Validation strictness is unchanged: a paraphrased gate list and unknown
decision kinds still escalate (tests assert both).

## Live walkthrough (this environment: Claude CLI only, logged in)

Portable Linux build → Chromium UI (Playwright) → provider detection → New
Task → START → autonomous run → Human Gate. Evidence:
`EVIDENCE/AAW_PRODUCT_MVP_V0_1_LIVE_E2E.json` and
`EVIDENCE/AAW_PRODUCT_MVP_V0_1_SCREENSHOTS/`.

| | |
|---|---|
| bindings | Claude-only alternatives, shown before START: architect/review/final review `OPUS_HIGH` (claude-opus-5), implementation/pretreatment/verification `SONNET_HIGH` (claude-sonnet-5) |
| iterations | 2 × PASS (Hello greeting; farewell direction item) |
| real executions | 13, all `CLOSED/COMPLETED`, 13 distinct provider sessions |
| stop | `ROADMAP_EXHAUSTED` → `AWAITING_HUMAN`, promotable candidate (left for the human) |
| Git | canonical `main` and remote unchanged, canonical checkout clean, `main_merge_allowed=false` |
| cost | ≈ 3.80 USD (provider-reported) |

Attempts 1–2 (`..._LIVE_ATTEMPT_01_CHARTER_REJECTED.json`,
`..._LIVE_ATTEMPT_02_DECISION_KIND.json`) are kept as evidence and were closed
through the product's Reject action.

## Models

Levels: planning (Średnia/Silna/Najsilniejsza), implementation
(Ekonomiczna/Zbalansowana/Silna/Rekomendowana), review
(Szybkie/Zbalansowane/Dokładne/Rekomendowane). Each level lists ordered
candidates per V0.3 policy slot. The first candidate of the default levels is
exactly the frozen V0.3 profile (asserted by a test). Per slot the first
runnable candidate is chosen and shown as RECOMMENDED or ALTERNATIVE before
START; a required slot with no runnable candidate blocks START; an optional
escalation slot keeps its V0.3 profile and the engine stops with
`ROLE_PROFILE_UNAVAILABLE` if that escalation is ever needed. The resolved
`roles` + `policy_profiles` are frozen by the engine in the run state; a
catalog update never changes a started run. Online update: GET of
`MODEL_RECOMMENDATIONS.json` from the AAW GitHub repo, validated, kept only if
newer; off by default; offline never blocks.

## STOP SAFELY / RESUME

| Moment of STOP | Effect |
|---|---|
| between phases | pause at the next boundary (`RUN_PAUSED`) |
| read-only call (plan continuation, verification, pretreatment, review) | provider process terminated; closed `CANCELLED`; resume replays it under a new `execution_id` with `retry_of_execution_id` |
| EXECUTE / REPAIR / initial architect | NOT killed (effects would be uncertain); the run pauses right after it |
| "Zatrzymaj natychmiast…" (force, confirmed) | everything terminated; an interrupted EXECUTE/REPAIR is never replayed — resume escalates `INTERRUPTED_IN_FLIGHT` and the Human Gate shows the uncertain effect |
| worker crash / power loss | shown as INTERRUPTED; RESUME retires the provably-stale lock via `reconcile_stale_lock` with the token the user saw, then V0.3 resume |

## Human Gate actions

Akceptuj → `approve_promotion` + `promote()` without a promoter =
`READY_FOR_EXTERNAL_INTEGRATION`, `merged=false`, `pushed=false`.
Odrzuć → `reject`. Dodaj dalszy kierunek / Kontynuuj z nowym celem → the frozen
mandate cannot grow, so a NEW run starts from the accepted candidate (local
checkpoint commit on the run's own `aaw/...` branch) or, for a non-promotable
hold, the old run is rejected as superseded. Pokaż dowody → raw artifacts.

## Not included (by design)

User accounts, payments, cloud backend, telemetry upload, marketplace,
benchmark scraping, team collaboration, automatic merge/push.
