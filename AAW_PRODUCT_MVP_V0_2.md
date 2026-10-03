# AAW Product MVP V0.2 — Release Candidate 1 (`0.2.0-rc1`)

Release hardening of the product layer for a first external user. The
autonomy engine is the frozen `AAW_DEFAULT_AUTONOMOUS_POLICY_V0.3` (freeze
`961e7f6`): **no engine module was changed** in V0.2 (`autonomy_*.py`,
`run_cancellation.py`, `workflow_runner.py`, `execution_ledger.py` are
byte-identical to V0.1). The only engine-visible change is *additive catalog
data* (three new profiles, two new catalog models — see "Model availability").
Base: Product MVP V0.1 (`aa117c3`).

User flow: `AAW-Windows-x64.zip` → unzip → `AAW\AAW.exe` → browser opens →
welcome → 7-step wizard → START → live process view → STOP SAFELY / STOP NOW /
RESUME → Human Gate. See `QUICK_START.md`.

## What changed

| Area | V0.2 |
|---|---|
| First run | Welcome screen (CLI needed and logged in, isolated copy, no merge/push, safe stop) opens automatically on first launch; 7-step wizard: project → AI tools → models → goal → first iteration → direction → START. Worktrees, execution IDs, ledger, lock token and exact profiles only under "Zaawansowane". Three one-click examples, recently used projects. |
| Project | `inspect_repo` explains every blocking state with the exact action (`action` field): not a repo (explicit, confirmed "create repository"), no commits, uncommitted/untracked changes (commit / `git stash -u` / `.gitignore`), merge/rebase/cherry-pick in progress, bare repo, Git missing, AAW data folder chosen, subfolder (uses the repo top). Never cleans or commits the user's checkout. Fixed V0.1 truncation of the first dirty file name. |
| Providers | Per CLI: FOUND/NOT FOUND, version, login (CLI's own status command), per-model availability state, install/login/verify/docs help. |
| Models | Explicit "Sprawdź modele" probe; exact runtime mappings for the frozen Claude 5.5 tiers (below). Simple levels show the actual models underneath. |
| Process | Status banner (status · iteration · phase); checklist Plan → Implementacja → Weryfikacja → Review → Naprawa → Final review → Następna iteracja; current activity, model, step/total elapsed, roadmap done/total; explicitly no ETA. |
| Briefs | Unchanged concept (projection only): asked / did / checks / problems / passed on + "View raw evidence". |
| STOP/RESUME | STOP SAFELY, STOP NOW, RESUME as visible buttons, each with its explanation. Paused and interrupted runs on Home under "Wstrzymane" with a RESUME button (V0.3 resume semantics; stale lock reconciled with the token the user saw). |
| Human Gate | Why / done / remaining / warnings / current candidate; Accept explains `READY_FOR_EXTERNAL_INTEGRATION` and how to take the result; `main_merge_allowed` only under Advanced. |
| Home | Sections in order Running · Paused · Needs attention · Completed; card: project, short goal, iteration, phase, last activity, status. |
| Release | `product_version.py` (`0.2.0-rc1`), `AAW --version`, self-test checks UI files and mapping consistency; ZIP contains `QUICK_START.md`, `README.md`, `CHANGELOG.md`, `VERSION.txt`, `LICENSE`, `SZYBKI_START.txt`, `EXAMPLES\` (3 tasks). |

## Model availability (the V0.1 inconsistency)

V0.1: the frozen catalog marks `OPUS_5_5_HIGH`, `OPUS_5_5_MEDIUM`,
`SONNET_5_5_MEDIUM` `KNOWN_BUT_UNAVAILABLE` (no runtime ID), while the
installed Claude CLI accepts `claude-opus-5-5` / `claude-sonnet-5-5`.

V0.2 resolution — **versioned AAW policy + actual local runtime capability →
user-visible availability**:

1. *Policy/catalog (versioned, additive).* New profiles
   `CLAUDE_OPUS_5_5_HIGH`, `CLAUDE_OPUS_5_5_MEDIUM`, `CLAUDE_SONNET_5_5_MEDIUM`
   (same harness and effort, exact runtime ID, `exact_runtime_mapping_of`,
   `availability_policy: LOCAL_RUNTIME_PROBE_REQUIRED`) and two
   `MODEL_CATALOG` rows (`DYNAMIC_PREFLIGHT`, access `ACCOUNT_DEPENDENT`).
   `MODEL_RECOMMENDATIONS.json` 2026.10.03.1 adds `exact_runtime_mappings`.
   The frozen V0.3 entries are untouched (frozen test `test_16` still passes);
   the default levels still list the frozen profile first.
2. *Runtime evidence.* "Sprawdź modele" (wizard step 3, Settings) runs one
   minimal request per distinct model through the installed CLI
   (`--tools ""`, one-line system prompt, `--max-turns 1`, plan mode / codex
   `--sandbox read-only --ephemeral`). Result per (CLI, model): ACCEPTED /
   REJECTED / UNKNOWN; stored in `%LOCALAPPDATA%\AAW\model_probes.json`;
   expires on a CLI version change; an offline/unknown probe never overwrites
   earlier evidence; the classifier never infers ACCEPTED without the token
   in a non-error response.
3. *Availability state per profile:* AVAILABLE, NOT_VERIFIED (catalog
   `DYNAMIC_PREFLIGHT`, usable, warned before START), VERIFIED_HERE,
   NEEDS_CHECK (exact mapping without a positive probe — **not usable**),
   REJECTED_HERE, POLICY_UNAVAILABLE, CLI_NOT_FOUND, NOT_LOGGED_IN.
4. *Resolution.* A frozen candidate the catalog cannot run is served by its
   exact mapping only when that mapping is usable; the slot is then
   RECOMMENDED with `exact_mapping_of`. Otherwise the next candidate is a
   visible ALTERNATIVE whose reason says how to enable the recommendation
   ("Sprawdź modele") or why it is unavailable (rejected, CLI missing). A
   required slot with nothing usable blocks START with a way out. Nothing is
   substituted silently; bindings freeze at run start (engine, unchanged).

Measured on the real Claude CLI 2.1.288 (Linux): both probes ACCEPTED in 7.7 s
total (a minimal probe on Sonnet 5.5 reported ≈ 0.0025 USD; the same probe
with the default Claude Code system prompt and tools cost ≈ 0.26 USD on
Opus 5.5 — hence the minimal form).

## Validation

| Check | Result |
|---|---|
| Baseline before V0.2 (V0.1 `aa117c3`, Python 3.12, codex stub on PATH, Xvfb, root `test_*.py`) | 413 passed |
| Full regression after V0.2 (same setup) | **431 passed, 0 failed** (413 + 18 new in `test_product_mvp_v0_2.py`) |
| GitHub Actions — Linux full regression (`ubuntu-latest`, Python 3.12) | green |
| GitHub Actions — Windows (`windows-latest`): safe-stop + both product suites with real worker processes and `.cmd` fake CLIs; PyInstaller build; frozen `--version`, `--self-test`, `--detect`; UI on loopback; ZIP contents; **browser walkthrough on the unzipped `AAW.exe`** incl. STOP NOW; `AAW-Windows-x64.zip` uploaded | see "Windows validation" |
| Deterministic first-user walkthrough, Linux portable build (unzipped frozen binary, Chromium, scripted CLIs) | 30/30 checks — `EVIDENCE/AAW_PRODUCT_MVP_V0_2_WALKTHROUGH/` |
| Live smoke, **real Claude CLI** (Linux, source app, real browser): wizard → real probes → START → 1 iteration PASS → Human Gate → Accept | PROMOTED / `READY_FOR_EXTERNAL_INTEGRATION`, merged=false, pushed=false; main, remote, checkout unchanged; 6 executions all `CLOSED/COMPLETED`; ≈ 1.73 USD — `EVIDENCE/AAW_PRODUCT_MVP_V0_2_LIVE_E2E.json` |

Live smoke bindings that actually ran: initial architect
`CLAUDE_OPUS_5_5_HIGH` → `claude-opus-5-5`/high (exact mapping of the frozen
`OPUS_5_5_HIGH`); the engine's own risk policy then chose the hard final-review
tier `CLAUDE_SONNET_5_5_MEDIUM` → `claude-sonnet-5-5`/medium — so the exact
mappings are proven end-to-end through the unchanged engine with the real CLI.
Other slots used visible Claude alternatives (no Codex CLI here).

### Scenario matrix

| Scenario | Where verified |
|---|---|
| no CLI | `test_no_cli_found_blocks_start_with_setup_help`, `test_no_cli_case_explains_setup_in_plain_language` |
| Claude only | `test_only_claude`, `test_claude_only_without_probe_uses_visible_alternative_and_offers_check`, live smoke |
| Codex only | `test_only_codex_keeps_v03_policy`, `test_codex_dynamic_models_are_usable_but_flagged_until_verified` |
| both | `test_both_providers`, `test_both_providers_detection_summary`, walkthrough |
| not logged in | `test_not_logged_in_blocks_and_explains`, `test_not_logged_in_provider_has_no_usable_models[claude/codex]` |
| offline | `test_offline_update_does_not_block`, `test_offline_probe_changes_nothing_and_keeps_earlier_evidence` |
| unavailable recommended profile | `test_unavailable_recommended_profile_is_visible_and_required_slot_blocks`, `test_rejected_probe_keeps_mapping_unavailable_and_says_why`, `test_unavailable_escalation_profile_stops_fail_closed_at_human_gate` |
| exact mapping used, no substitution | `test_verify_models_activates_exact_mapping_and_run_uses_exact_model_id` (argv `--model claude-opus-5-5 --effort high`), live smoke |
| Start, visible phase changes | `test_visible_phase_transitions`, walkthrough (banner sequence recorded), live smoke |
| briefs, raw evidence | `test_start_to_human_gate_with_briefs_timeline_and_no_merge_push`, walkthrough |
| repair | `test_repair_is_visible_in_timeline`, walkthrough (REPAIR → PASS) |
| safe stop | `test_stop_during_execute_waits_then_pauses_and_resume_completes`, `test_stop_during_review_cancels_read_only_call…`, walkthrough |
| immediate stop | `test_force_stop_during_execute_is_never_replayed_blindly`, walkthrough `--stop-now` (implementer not re-run, gate explains uncertainty) |
| restart / resume | `test_restart_after_worker_crash_resumes_through_lock_reconciliation`, `test_home_order_and_paused_runs_offer_resume`, walkthrough (RESUME from Home) |
| Human Gate, Accept = READY_FOR_EXTERNAL_INTEGRATION | `test_accept_reject_and_continue_never_merge_or_push`, walkthrough, live smoke |
| roadmap exhaustion | walkthrough (4 iterations → ROADMAP_EXHAUSTED), live smoke; iteration cap: `test_iteration_cap_needs_explicit_early_end` |
| no merge / push | every run-level test (bare remote), walkthrough, live smoke |

## First-user walkthrough

`packaging/first_user_walkthrough.py` drives the real app (source or the
built executable) in Chromium as a first-time user who knows Git only at a
basic level: welcome → project path → detection → "Sprawdź modele" → example
goal → START → watches phases → STOP SAFELY during implementation → Home →
RESUME → repair → Human Gate → raw evidence → Accept; `--stop-now` adds a
second task with STOP NOW during implementation. Only the model is scripted.

Launch → START: **13 user interactions** (one of them optional: "Sprawdź
modele"), 3.3 s of machine time. With human reading and typing (welcome ≈ 30 s,
choosing a folder ≈ 20 s, reading detection/models ≈ 60 s, writing a goal and
direction ≈ 90 s, summary ≈ 30 s) the estimate is **≈ 4 minutes** from
double-clicking `AAW.exe`; unzip and SmartScreen add < 1 minute. No terminal,
no manual worktree, no config editing, no model runtime IDs required.

Friction found and fixed during the walkthrough / live smoke:

1. Model step said "details in the table below" — no table there → reworded.
2. Level descriptions used jargon ("Polityka V0.3", "tier") → plain language.
3. Human Gate showed `main_merge_allowed = false` and a `[object Object]`
   integration value → plain text, technical value under Advanced.
4. `READY_FOR_EXTERNAL_INTEGRATION` overflowed its column → wraps.
5. Escalation/brief texts said "worktree" → "kopia robocza".
6. After Accept the page waited for the next poll → refreshes immediately.
7. Codex-only user whose account rejects GPT-6 Luna got a dead-end blocker →
   blocker now names the way out (other level / other CLI).
8. First dirty file name lost its first character (V0.1 bug) → fixed.
9. Windows CI (first run): the walkthrough's own console output and the
   scripted CLI used the Windows code page; with a Polish goal the scripted
   CLI mangled the engine's UTF-8 handoff and the engine correctly refused
   it (`MANDATE_EXTENSION_ATTEMPT`). Both test tools now use UTF-8 I/O
   (reproduced locally with `PYTHONIOENCODING=cp1252`, then 30/30). The app
   and the engine were not affected — the engine already speaks UTF-8.

Not fixed (low value / out of scope): the generated `__pycache__` files of the
project's own tests appear in the candidate when the project has no
`.gitignore` (engine behaviour; the final reviewer flagged it as a warning);
UI is Polish only; the native folder dialog needs Tk (bundled in the Windows
build; on other systems the path is typed).

## Windows validation — labelled accurately

* **Proven in CI on `windows-latest`:** build, frozen `AAW.exe` launch
  (`--version`, `--self-test`, `--detect`), UI server on loopback, all product
  tests (real worker processes, Windows process-tree STOP, `.cmd` fake CLIs),
  ZIP contents, and the browser walkthrough on the unzipped `AAW.exe` (real
  frozen worker, real engine, real Git worktree, scripted model).
* **Not performed:** a run with a real, authenticated Claude/Codex CLI on
  Windows (no such machine available). Real-provider evidence for V0.2 is the
  Linux live smoke above plus the V0.1 Linux live E2E.
* Codex model probes follow the `codex exec` invocation the adapters already
  use, but no real Codex CLI was available; Codex probe behaviour is covered
  with the fake CLI only.

## Known limitations

* Unsigned executable (SmartScreen warning on first start).
* UI language: Polish only (docs English; `SZYBKI_START.txt` in Polish).
* No real-provider run on Windows; Codex paths validated with scripted CLIs.
* The canonical checkout must stay clean during a run (V0.3 Git boundary).
* Probe results are per CLI version, not per account switch: after logging in
  with a different account, run "Sprawdź modele" again.
* POSIX STOP terminates the provider process, not its process group (V0.1).
* The app runs in the user's default browser; if it does not open, the URL is
  in `%LOCALAPPDATA%\AAW\aaw.log` (windowed build has no console).
