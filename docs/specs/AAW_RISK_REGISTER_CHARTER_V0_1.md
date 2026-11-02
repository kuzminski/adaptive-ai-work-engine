# AAW — known risks → risk floors in the planner's charter (V0.1)

## Why

The frozen directional charter already had `risk_guidance`: per roadmap item, a minimum implementation
tier (`NORMAL` / `HARDER` / `SIGNIFICANTLY_DIFFICULT`) and a minimum final-review tier (`DEFAULT` / `HARD` /
`CRITICAL`). Only the initial architect (a model) wrote it, so a risk the user had named before START — or
that the idea intake ("rozpis") had listed and the user confirmed — reached the run only if the architect
happened to repeat it. This change makes human-confirmed risks a deterministic minimum.

## Contract

**Mandate** (`roadmap_mandate.risk_register`, optional; validated and covered by the mandate hash):

```json
{"risk_id": "R1", "description": "data loss on an interrupted save", "severity": "HIGH",
 "item_ids": ["STEP_1"], "mitigation": "atomic write + test", "source": "USER"}
```

`item_ids` empty = the whole run (every roadmap item, including the standing `CONTINUE` item). At most 50
risks, 2000 characters per text.

**Severity → floors** (`autonomy_contract.RISK_SEVERITY_FLOORS`):

| Severity | Implementation floor | Final-review floor |
|---|---|---|
| LOW | NORMAL (no change) | DEFAULT (no change) |
| MEDIUM | NORMAL | HARD |
| HIGH | HARDER | HARD |
| CRITICAL | SIGNIFICANTLY_DIFFICULT | CRITICAL |

Several risks on one item → the maximum of each floor. LOW risks never raise a floor; they stay in the
mandate as context for the planner and reviewers.

**Charter** (`autonomy_contract`):

* `mandated_risk_floors(mandate)` — the minimum rows, in roadmap order, reason
  `HUMAN_CONFIRMED_RISK R1 (HIGH): …`.
* `directional_charter_template` pre-fills `risk_guidance` with those rows (only when a register exists).
* `validate_directional_charter` → `enforce_risk_floors`: a row the architect dropped is added back, a lower
  floor is raised, a higher floor stays, the human reason is appended. Never a rejection: a model that
  ignores the rule cannot weaken the run and cannot stop it either. The frozen charter records
  `mandated_risk_floors` and `risk_floor_adjustments`; without a register the charter keeps its previous
  shape and hash.
* The existing routing reads the floors unchanged (`DIRECTIONAL_CHARTER_RISK_FLOOR`,
  `FROZEN_CHARTER_RISK:<item>:<reason>` evidence); with an implementer chain, HARDER → at least step 2,
  SIGNIFICANTLY_DIFFICULT → at least step 3.

**Roles** (`autonomy_adapters`):

* Initial architect: keep every template row (raise or extend only).
* Every planner: name each applicable `risk_id` in `work_packet.pitfalls`, with a verification step when
  one can show it.
* Implementer / self-verify / repair / diagnose: `RISK_FOCUS` = the register entries for the plan's
  `roadmap_refs` (kept in the compact repair packet).
* Reviewer / final reviewer: `RISK_CHECKS` — check each risk against the diff and evidence, raise a finding
  only when the change realizes it or skips its stated mitigation, name the checked `risk_id`s in the summary.
* No register or no applicable risk → handoffs are unchanged.

**Journal**: `DIRECTIONAL_CHARTER_FROZEN` carries `risk_guidance` (floors), `mandated_risk_floors` (count)
and `risk_floor_adjustments`; the run view's charter brief shows "Progi z Twoich ryzyk: …" and what was
restored.

## Product

* Wizard → "Zaawansowane" → **Znane ryzyka**, one per line:
  `[wysokie] utrata danych przy zapisie (punkty: 1, 3)`. Level: niskie / średnie / wysokie / krytyczne
  (English names accepted; default średnie). Point numbers follow the summary's "Kierunek (roadmapa)" list
  (1 = the first iteration = `STEP_1`, k = `STEP_k`; `pierwsza` is an alias of 1); no `punkty` = whole run.
  Mistakes are refused with a plain Polish message, never guessed.
* Summary before START: a **Ryzyka** row with every risk and the floors the charter will get; a CRITICAL risk
  adds a cost/time warning.
* Continuation "same goal, new direction" carries the whole-run risks (point-scoped ones named old points).

### Hook for the idea intake ("rozpis")

`form.advanced.risks` (or `form.risks`) also accepts rows, so the intake's confirmed risks go straight in:

```json
[{"risk_id": "RZ1", "description": "…", "severity": "high", "points": [2], "mitigation": "…", "source": "INTAKE"}]
```

`points` use the same numbering as above; `item_ids` (`STEP_k`) are accepted instead. Given ids are kept
when unique, otherwise `R<n>` is assigned. In the UI, an array in `formState.advanced.risks` is rendered into
the text field by `risksText()`; the forecast should count an item whose final-review floor is HARD/CRITICAL
as a chain with a serious review (`preview.risk_floors`).

## Also in this change

* Phone width: the summary table stacks label above value below 560 px (the 210 px label column left
  ~100 px for values on a 390 px screen).
* `packaging/first_user_walkthrough.py` loads the `expense_tracker` preset: the examples became presets in
  the long-autonomy line and the Windows CI walkthrough still clicked the removed `[data-example='0']`.

## Validation

* `test_risk_register_charter.py` (24): register validation, severity mapping, whole-run vs scoped, charter
  shape without a register, verbatim copy / dropped / lowered / raised architect rows, controller routing
  when the architect ignores the floors, journal payload, handoff keys per role, wizard parsing and refusals,
  intake rows, preview, and one full product run (real worker, controller and executor; fake CLI only).
* Full regression and the first-user walkthrough from source (real Chromium, `--stop-now`) pass.
* Browser check of the wizard with three risks at 1280 px and 390 px (no horizontal overflow).

Not verified: a real model as initial architect with a pre-filled `risk_guidance` template (the enforcement
makes the outcome independent of it), and the intake integration itself (its code is not in this branch).
