# AAW — implementer chain V0.1

The implementer (and the whole implementation family: repair, review preparation) is chosen **at start** as an
ordered chain of profiles, weakest step first. One step = one model for everything; several steps = escalation
in exactly the order the user (or the system default) set.

## Default chain (system)

`MODEL_RECOMMENDATIONS.json → default_implementer_chain`, used by the `RECOMMENDED` implementation level:

| # | profile | model / effort |
|---|---------|----------------|
| 1 | `GPT6_LUNA_HIGH` | gpt-6-luna / high |
| 2 | `GPT6_LUNA_VERY_HIGH` | gpt-6-luna / xhigh |
| 3 | `GPT6_LUNA_MAX` | gpt-6-luna / max |
| 4 | `TERRA_HIGH` | gpt-5.6-terra / high |
| 5 | `TERRA_VERY_HIGH` | gpt-5.6-terra / xhigh |
| 6 | `TERRA_MAX` | gpt-5.6-terra / max |
| 7 | `CLAUDE_SONNET_5_5_MEDIUM` | claude-sonnet-5-5 / medium (needs "Sprawdź modele") |
| 8 | `CLAUDE_SONNET_5_5_HIGH` | claude-sonnet-5-5 / high (needs "Sprawdź modele") |

Interpretation of the request: "Luna 6 … max" is the existing Luna ladder up to max, followed by Terra 5.6 and
Sonnet 5.5. Changing the default is a data edit in that JSON block.

## Choosing a chain

- Wizard step "Modele" → "Łańcuch implementatora": pick a model per step, reorder (↑ ↓), remove (✕), add a step,
  "Przywróć domyślny". One step pins a single model. API: `implementer_chain` (list of profile IDs) in the task
  form, `/api/setup/resolve` and `/api/models/verify`.
- Other implementation levels (Ekonomiczna / Zbalansowana / Silna) keep their per-slot candidates (no chain).

## Behaviour

- **Start step** follows the plan's complexity: NORMAL → step 1, HARDER → step 2, SIGNIFICANTLY_DIFFICULT → step 3
  (clamped to the chain length).
- **Escalation:** a step that fails with a blocking `IMPLEMENTATION_CAPABILITY_MISMATCH` finding (with
  `execution_id` and `evidence_ref`) moves the next implementation/repair call to the next step. Never past the last.
- **Repairs:** `repair_default` = step 2, `repair_hard` = step 3, review preparation = step 2 (all clamped), and the
  repair ladder (effort up → difficult implementer → planner diagnosis) walks the chain; the last step is the
  "difficult implementer".
- **Final review** becomes `HARD` when the implementation reached step 4 or later (single-model chains never do).
- The chain is validated by `validate_roles` and frozen in the run state (`roles.implementer_chain`); resuming keeps it.

## Availability — nothing is replaced silently

- User chain: kept exactly as given. Unknown / repeated / local profiles, a non-runnable step 1 or 2 block START
  (steps 1–2 also repair and prepare review); a non-runnable later step is a warning and the engine stops with
  `ROLE_PROFILE_UNAVAILABLE` only if that escalation is ever needed.
- System chain: used only if its starting step is runnable here (a missing CLI skips leading steps of that
  provider); later non-runnable steps are skipped **visibly** with a warning. Otherwise the per-slot candidates
  with their visible ALTERNATIVE / UNAVAILABLE statuses decide, as before.
- A manual per-slot profile for a chain-owned slot conflicts with the chain and blocks START.

## Not changed

`AUTONOMY_ROLES.json` (frozen V0.3 policy used by the engine CLI/tests) has no chain, so engine runs without a
chain behave exactly as before.
