# Changelog — AAW product app

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
