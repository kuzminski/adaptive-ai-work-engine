# AAW — long-form fields and count-free autonomy (V0.1)

## Audit (state before this change)

| Question | Finding |
|---|---|
| Field limits | **Backend only**, `product_runs.normalize_form`: goal `[:2000]`, first iteration `[:2000]`, direction points capped at `MAX_DIRECTIONS = 12`, each `[:400]` (also the STEP_1 title); one roadmap point = one line, so a multi-line Markdown roadmap was split into fragments. UI textareas and the DB had no limit; the HTTP body cap was 2 MB. |
| "~2 iterations" | Not a literal `== 2`. Product default `max_iterations = len(items)+2` (`product_runs.build_mandate`) **and**, more importantly, a finite static roadmap: when every mandate item was DONE, `ROADMAP_CHECK` → `AWAITING_HUMAN (ROADMAP_EXHAUSTED)`. With a goal plus one direction that is two iterations. The engine loop itself (`ROADMAP_CHECK → PLAN`) already existed. Hard ceiling `HARD_MAX_ITERATIONS = 50`. |
| Roadmap/state | `autonomy_state.json`: frozen, hashed `mandate` (planner cannot extend it – `E_EXTENSION`), per-item `roadmap` status, `iterations`. |
| Hard-coded local paths | None in product code: `aaw_paths.py` / `product_home.py` resolve everything from `AAW_*` env vars, per-user home outside the repo. |

## Changes

* **Fields**: no truncation; ceilings `MAX_FIELD_CHARS=200k`, `MAX_ROADMAP_CHARS=500k`, `MAX_DIRECTIONS=200`,
  `MAX_DIRECTION_CHARS=20k` (runaway-paste protection; exceeding gives a Polish error, never a silent cut).
  HTTP body cap 16 MB. `split_directions` keeps indented/multi-line points; `roadmap_mandate.direction_text`
  stores the verbatim roadmap (additive field, covered by the mandate hash for new runs only).
* **Autonomy**: mandate item flag `recurring` (validated boolean). A recurring item is not completed by an
  accepted iteration (progress recorded in its `iterations`); the planner ends the run by returning
  `NO_FURTHER_ACTION` with the item in `skipped_items` and a concrete reason. Product forms add the standing
  item `CONTINUE` by default (`advanced.continue_autonomously=false` restores listed-points-only behaviour).
* **Fuses** (not autonomy): iteration cap (default 40, hard 200, UI-configurable), STOP SAFELY / STOP NOW,
  escalation, Git boundary, unavailable profile, planner's reasoned end. Human Gate text distinguishes
  "planner finished" from "fuse stopped a run that could continue" (`gate.could_continue`).
* **Living roadmap**: plan fields `working_roadmap`, `next_recommended_step` → `state.working_roadmap(_history)`;
  advisory, never grants scope, never edits the user's direction (shown separately in the run view).
* **UI**: long-text editor (autosize up to 60 vh, enlarge, copy, counter, local draft with restore), example
  presets from `product_presets.py` via bootstrap, "Kierunek i roadmapa" panel, iteration counter,
  spacing/typography/focus/disabled/empty-state polish.
* **Compatibility**: old states/mandates have no `recurring`/`direction_text` and behave exactly as before;
  stored `max_iterations ≤ 50` stay valid; the `process.roadmap` API shape is unchanged.

## Cloud vs local
Paths come from `AAW_PRODUCT_HOME`, `AAW_ROOT`, `AAW_STATS_ROOT` etc.; no secrets are stored by AAW.
Tests run with the fake CLI (`product_fake_cli.py`, `AAW_FAKE_CONTINUATION_PASSES`) and need no provider login.

## Known baseline failures (not related)
37 tests (`test_aaw_bridge`, `test_canvas_functionalization`, `test_multirouting_slice`,
`test_operator_recovery_edit_safety`, `test_workflow_execution_identity`) fail identically on the untouched
repo in the cloud container (no `codex` CLI / browser tooling). `CONTROL_CENTER/test_*` need `tkinter`.
