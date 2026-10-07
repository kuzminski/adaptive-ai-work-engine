# Changelog — AAW product app

## Unreleased — known risks set the planner charter's risk floors

Details: `AAW_RISK_REGISTER_CHARTER_V0_1.md`.

- New wizard field "Znane ryzyka" (Zaawansowane), also fed by rows from the idea intake: each risk has a level
  (niskie / średnie / wysokie / krytyczne) and optional roadmap points; stored as the mandate's `risk_register`.
- The level sets minimum implementation / final-review floors in the frozen directional charter
  (MEDIUM → hard review; HIGH → harder implementation + hard review; CRITICAL → strongest implementation and
  critical review). The initial architect may only raise them; a dropped or lowered floor is restored and
  recorded (`risk_floor_adjustments`), never a rejection.
- Planner names applicable risks in work-packet pitfalls; implementer gets `RISK_FOCUS`, reviewers `RISK_CHECKS`.
- Summary before START lists the risks and the floors (CRITICAL adds a cost warning); the run's charter brief
  shows what the risks enforced. Summary table stacks on phones.
- Windows walkthrough uses the example presets (the old example buttons are gone).


## Unreleased — chain mode, telemetry V1, AAW-Bench

Details and rationale: `AAW_OPTIMIZATION_PROJECT_V0_1.md`.
- **Chain mode** (`autonomy_chain.py`, `chain` block of `AUTONOMY_ROLES.json`, on by default; `enabled: false` restores
  the classic cycle): iterations run in chains of 8 with deterministic self-verification and one cheap review that only
  a CRITICAL defect can fail; the last iteration of a chain gets the serious review (final-review tier floor `HARD`) over
  the whole diff; HIGH/CRITICAL defects are repaired, smaller ones go to `deferred_findings` and are polished once at the
  end of the run; the planner may outline a whole chain (`chain_plan`, every stub re-validated by `check_plan`).
- **Telemetry V1** (`aaw_telemetry.py`, `MODEL_PRICING.json`): one record per executor call in
  `AUTONOMY/telemetry.jsonl` (tokens, cost REPORTED/ESTIMATED/UNPRICED, wall time, category, chain), reports by
  category/role/profile/chain, legacy runs rebuilt from state. Prices are NOT_VERIFIED list prices.
- **AAW-Bench** (`BENCH/`): 9 verified tasks scored by hidden tests through the production controller and `execute`
  executor; cost per solved task, Pareto front, escalation ladders and per-tier recommendations. Live run not done yet.
- Product view: a provisional iteration is labelled "PASS (wstępnie)", open deferred findings are listed as warnings.
- Review fixes during the project: the Human Gate now lists only findings the polish pass did not attempt; an
  unrunnable final-review floor profile falls back instead of stopping the run.
- Tests: `test_autonomy_chain.py` (34), `test_aaw_bench.py` (17). Lifecycle tests pin the classic cycle explicitly.
- **Experience** (`aaw_experience.py`, `aaw_benchstats.py`, `PRODUCT_UI/experience.js`; `AAW_UX_REALTIME_EXPERIENCE_V0_1.md`):
  the benchmark now builds itself from real runs (no lab step): per-task-kind cost per solved task with intervals, Pareto
  front and a user-mix-weighted overall choice, "what would be optimal" statements with evidence, horizon ("how far runs get
  without a human"), forecast for a running or new run (a range, or an explicit "no data"), settlement at the gate.
  Run page: live journey strip, chain slots, cost meter and split, ticking timer, event feed. New page "Doświadczenie" with a
  labelled synthetic preview (3 personas). API: `/api/experience`, `/api/experience/forecast`, `live` + `settlement` in `/api/runs/<id>`.
  Product option `advanced.chain_mode` (default on).
- Tests: `test_aaw_experience.py` (40), `test_product_experience.py` (7).
- **Recommended model button** (wizard summary): applies the model that works best for this kind of work as the default
  implementer for that task only, after an explicit confirmation, with an audit entry; undoable; never automatic.
- **Opt-in controlled exploration** (`autonomy_explore.py`; settings + per-task switch; off by default): a small visible share of
  ordinary iterations is implemented by another runnable model of the same or lower cost class to fill thin benchmark cells.
  Never repairs, harder tiers, first iteration, critical scope; hard per-run budget; journaled and marked in the run and on the
  Experience page. Role config key `exploration` (frozen per run).
- **Idea intake** (`product_intake.py`, `/api/intake/propose`): one confirmed, read-only call to the planner turns a loose idea into
  scope, roadmap, criteria, assumptions, open questions and risks, shown with horizon and cost before START. Roadmap lines
  prefixed `[człowiek]` become human-required items. The wizard summary now forecasts the whole roadmap (iterations, chains,
  cost/time ranges, chance of finishing without a human, gates).
- Tests: `test_autonomy_explore.py` (20), `test_product_intake.py` (25).
- Performance: the run page's live block is cached by file state and skipped on the Home list (the first version recomputed history and benchmark on every 0.15-1.5 s poll and slowed a product test by ~20 s).

- Goal / First iteration / Direction fields are no longer cut (was 2000 / 2000 / 12 points × 400 chars);
  safety ceilings only (200k / 200k / 500k chars, 200 points), refused with a clear message, never truncated.
- Direction text is split into points preserving multi-line Markdown; the verbatim text is kept in the mandate.
- New "standing" roadmap item (`recurring: true`, id `CONTINUE`): an accepted iteration never completes it, so
  PLAN → … → FINAL_REVIEW → ROADMAP_CHECK → PLAN continues until the planner skips it with a reason or a fuse
  fires. Option `advanced.continue_autonomously` (default on; off = previous behaviour).
- Iteration cap is a fuse: default 40 (was `len(items)+2`), hard ceiling 200 (was 50).
- Planner writes advisory `working_roadmap` / `next_recommended_step`, stored apart from the frozen mandate.
- UI: first-class long-text editor (autosize, enlarge, copy, counter, local draft), example presets
  (`product_presets.py`), "Direction and roadmap" panel, moderate visual polish.

## 0.4.0 — 2026-10-06 — Implementer effectiveness (phase 1) + Antigravity CLI (includes 0.3.0)

Details and the full repair plan: `AAW_IMPLEMENTER_EFFECTIVENESS_PLAN_V0_1.md`. Lifecycle unchanged.
- Plans carry a mandatory **work packet** (files to read/change, small ordered steps with per-step verification,
  exact verification commands mapped to the required evidence, definition of done, pitfalls, out-of-scope),
  written for a weaker implementer that must not re-plan (`work_packet.py`).
- **Final self-audit** at the end of every implement/repair call (checklist A1–A7); a failed item is repaired
  before any reviewer is called. **Deterministic simple-error checks** in SELF_VERIFY: conflict markers, Python
  and JSON syntax, "done" with no change.
- **Difficulty routing**: visibly hard iterations (label, size, cross-cutting, under-specified packet, planner
  request) go to GPT-6.1 Sol medium up front (new slot `implementer_strong`); a finding that survives the weak
  repairer goes straight to it with diagnosis first (EFFORT_UP skipped). Next iterations are planned by
  GPT-6.1 Sol medium (new slot `continuation_planner`) instead of Sol 5.6 light.
- With an implementer chain (the default since 0.3.0, or one chosen at start) the chain stays the authority:
  difficulty routing only raises the starting step (packet route HARDER → at least step 2, STRONG → at least
  step 3) and `implementer_strong` maps to chain step 3; GPT-6.1 Sol medium is used up front only by the
  model sets without a chain (Szybka / Zbalansowana / Silna).
- Review pretreatment is off by default (one model call per review round with no effect on the verdict).
- **Antigravity CLI (`agy`)** support — successor of the Gemini CLI, which Google shut down (detection, login
  status via `agy -p /usage`, "Sprawdź modele", role dispatch with `--json-schema`); profiles `AGY_GEMINI_3_1_PRO`,
  `AGY_GEMINI_FLASH`, usable after a local probe. The interim Gemini CLI harness is removed. Routing
  alternatives are limited to profiles runnable on this machine.
- Required-evidence matching tolerates naming variants ("Unit-tests (pytest)" substantiates "unit tests").

## Unreleased — long-form fields, example presets, count-free autonomy

Details and rationale: `AAW_LONG_AUTONOMY_V0_1.md`.

- Goal / First iteration / Direction fields are no longer cut (was 2000 / 2000 / 12 points × 400 chars);
  safety ceilings only (200k / 200k / 500k chars, 200 points), refused with a clear message, never truncated.
- Direction text is split into points preserving multi-line Markdown; the verbatim text is kept in the mandate.
- New "standing" roadmap item (`recurring: true`, id `CONTINUE`): an accepted iteration never completes it, so
  PLAN → … → FINAL_REVIEW → ROADMAP_CHECK → PLAN continues until the planner skips it with a reason or a fuse
  fires. Option `advanced.continue_autonomously` (default on; off = previous behaviour).
- Iteration cap is a fuse: default 40 (was `len(items)+2`), hard ceiling 200 (was 50).
- Planner writes advisory `working_roadmap` / `next_recommended_step`, stored apart from the frozen mandate.
- UI: first-class long-text editor (autosize, enlarge, copy, counter, local draft), example presets
  (`product_presets.py`), "Direction and roadmap" panel, moderate visual polish.

## 0.3.0 — 2026-10-04 — Implementer chain chosen at start (includes 0.2.1)

Details: `AAW_IMPLEMENTER_CHAIN_V0_1.md`. Lifecycle unchanged.
- The implementer is no longer fixed: at the "Modele" step the user can pick any model as the implementer, or an
  ordered escalation chain of models (from → to, any length 1–12). Default chain set by the system:
  GPT-6 Luna high → very high → max → GPT-5.6 Terra high → very high → max → Claude Sonnet 5.5 medium → high.
- New exact profiles: `TERRA_VERY_HIGH`, `TERRA_MAX`, `CLAUDE_SONNET_5_5_HIGH`.
- The chain is frozen with the run (`roles.implementer_chain`) and drives start (by plan complexity), capability
  escalation, repairs and the repair ladder. A step that is not runnable here is never replaced by another model.

## 0.2.1 — 2026-10-04 — antivirus false-positive mitigation
- `AAW.exe` now carries a Windows version resource (company, product, description, version).
- CI builds the PyInstaller bootloader from source and scans the built app with Microsoft Defender (informational).
- Start guides explain what to do when Defender quarantines the file.

## 0.2.0 — 2026-10-04 — Product MVP V0.2 (adds adaptive model routing + bounded repair escalation)

Details: `AAW_QUOTA_ROUTING_AND_REPAIR_ESCALATION_V0_1.md`. Lifecycle unchanged.
- Quota/trust/capability routing around the policy's preferred profile (`model_router.py`), provider failover,
  audited `ROUTING_DECISION`s. Antigravity FREE: EXPERIMENTAL, adapter contract only (live NOT_TESTED).
- A finding that survives one REPAIR no longer goes straight to the Human Gate: bounded ladder (effort up →
  difficult implementer with diagnosis → planner diagnosis), evidence-based progress, escalation ledger,
  compact repair packet. `REPAIR_NO_PROGRESS` now means the ladder is exhausted.
- Fixed: Human Gate after an escalation showed an empty "Zmienione pliki" (no candidate fingerprint was stored).
- Fixed: a stale failing check could never be superseded by a re-run under another name.

## 0.2.0-rc1 — 2026-10-03 — Product MVP V0.2, release candidate 1

Release hardening of the product layer. The autonomy engine is the frozen
`AAW_DEFAULT_AUTONOMOUS_POLICY_V0.3` (freeze `961e7f6`); no new autonomy
states, roles or architecture. Details: `AAW_PRODUCT_MVP_V0_2.md`.

### Distribution
- Public download: the GitHub Release `v0.2.0-rc1` carries `AAW-Windows-x64.zip`
  (built, validated and published by `.github/workflows/aaw-portable.yml`; no
  binaries in Git). The root `README.md` starts with the download link;
  `SZYBKI_START.txt` is in the repository root and next to `AAW.exe` in the ZIP.
- Start guides describe exactly the extracted ZIP (one `AAW` folder); checked by
  `packaging/check_release_zip.py` in CI and before publishing.

### First-run experience
- First launch opens a welcome screen (what AAW needs, isolated copy, no
  merge/push) and a 7-step wizard: project → AI tools → models → goal →
  first iteration → direction → START. Worktrees, execution IDs, ledger and
  exact profiles are behind "Zaawansowane" (Advanced).
- Three one-click example tasks; recently used project folders.

### Project handling
- Plain-language readiness check with the exact action needed: not a Git repo
  (optional explicit "create repository"), no commits, uncommitted/untracked
  changes (commit or stash), merge/rebase in progress, bare repository, Git
  missing, an AAW work folder chosen by mistake, subfolder of a repository.
  AAW never cleans or commits the user's checkout on its own.
- Fixed: the first changed file name was shown truncated by one character.

### Provider detection and model availability
- Per CLI: FOUND / NOT FOUND, version, login status, usable models with an
  explicit availability state, and step-by-step setup help.
- New explicit **"Sprawdź modele" (Verify models)**: one tiny request per model
  through the installed CLI; results are stored per CLI version and never
  overwritten by an offline/unknown probe.
- Resolved the catalog inconsistency: the frozen V0.3 profiles
  `OPUS_5_5_HIGH`, `OPUS_5_5_MEDIUM`, `SONNET_5_5_MEDIUM` (no runtime ID in the
  frozen catalog) are now served by **exact runtime mappings**
  (`claude-opus-5-5`, `claude-sonnet-5-5`, same effort) **only after** the
  local probe saw the Claude CLI accept that exact model ID. Shown before
  START as "exact mapping"; never a different model. The frozen V0.3 profile
  entries themselves are unchanged.

### Models
- Simple levels (Planning / Implementation / Review) now show the actual
  models underneath; exact role bindings remain in Advanced. Bindings are
  frozen at run start (unchanged).

### Process view, briefs, STOP/RESUME
- Status banner with iteration and active phase; current activity, model,
  step and total elapsed time, roadmap progress; no ETA.
- **STOP SAFELY**, **STOP NOW** and **RESUME** as visible buttons with the
  difference explained under each. Paused/interrupted runs appear on Home
  with a RESUME button.
- Briefs: what the phase was asked to do, what it did, checks, problems,
  what was passed on, and "View raw evidence".

### Human Gate
- Why AAW stopped / what was done / what remains / warnings / current
  candidate; Accept explains `READY_FOR_EXTERNAL_INTEGRATION` and how to take
  the result; no merge, no push.

### Home
- Sections in order: W toku (running), Wstrzymane (paused and interrupted,
  with RESUME), Wymaga uwagi (needs attention), Zakończone (completed).

### Release package
- `AAW-Windows-x64.zip` now contains `QUICK_START.md`, `README.md`,
  `CHANGELOG.md`, `VERSION.txt`, `LICENSE` and `EXAMPLES\`.
- `AAW.exe --version`; `--self-test` also verifies the UI files and the
  exact-mapping consistency.
- Windows CI: full product suite, build, self-test, detection, UI launch,
  a frozen-exe end-to-end smoke (START → Human Gate) and the scripted
  first-user browser walkthrough; ZIP uploaded as an artifact.

### Known limitations
- Unsigned executable; UI in Polish only.
- No real provider run was executed on Windows (CI uses scripted CLIs);
  real-provider evidence is from Linux.
- Codex model probes use the documented `codex exec` path but were not run
  against a real Codex CLI in this environment.

## 0.1 — Product MVP V0.1

First portable product layer over the frozen V0.3 engine. See
`AAW_PRODUCT_MVP_V0_1.md`.
