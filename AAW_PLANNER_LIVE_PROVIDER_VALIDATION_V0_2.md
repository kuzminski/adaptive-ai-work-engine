# AAW Planner Live-Provider / Real-Workflow Validation V0.2

Evidence-oriented record of validating the frozen Planner Proposal Pipeline
V0.1 contract against a **real** planner provider, over a deterministic
corpus of real AAW workflow fixtures. Does not rewrite the V0.1 contract.

Raw evidence: [`EVIDENCE/planner_live_validation_v0_2.json`](EVIDENCE/planner_live_validation_v0_2.json)
(baseline + model-comparison captures), [`EVIDENCE/planner_live_validation_v0_2_after_repair.json`](EVIDENCE/planner_live_validation_v0_2_after_repair.json)
(post-repair reruns), [`EVIDENCE/planner_accept_run_v0_2.json`](EVIDENCE/planner_accept_run_v0_2.json)
(real Accept+Save+Run evidence), and the human-scored rollup in
[`EVIDENCE/planner_live_validation_v0_2_scored.json`](EVIDENCE/planner_live_validation_v0_2_scored.json).
Reproduce with `python planner_live_validation.py` and
`python planner_accept_run_validation.py`.

---

## A. V0.1 closure

- Pre-commit `HEAD`: `6d6ea8f` (`fix(ui): close authoring history and freeze convergence v0.1`)
- Verified suite before commit: `377 passed, 2 skipped` (matches the reported baseline exactly)
- Planner V0.1 commit: **`8ac0462`** — `feat(planner): add proposal-only planner pipeline v0.1`
  (tree `acca418`), 12 files changed, all previously-untracked/modified planner
  files and none else
- Clean tree confirmed before branching

## B. V0.2 baseline

- Branch: `feature/planner-live-provider-validation-v0-2`, from `8ac0462`
- Provider path used: `aaw_planner.provider_planner` — the existing, documented
  seam. No second provider layer was built.
- Providers actually exercised (both real, both already configured on this
  machine, both under the account's included plan, `NO_EXTRA_PAID_USAGE` never
  overridden):
  - **`SOL_HIGH`** — `gpt-5.6-sol`, harness `codex`, effort `high`. This is
    the catalog **default** planner profile. `codex.exe` is installed at
    `%LOCALAPPDATA%\OpenAI\Codex\bin\...\codex.exe` (not on `PATH` under the
    bare name `codex`, but `workflow_runner.harness_executable` already
    resolves that).
  - **`OPUS_HIGH`** — `claude-opus-5`, harness `claude`, effort `high`. Used
    for a small model-capability comparison (§I).
  - `ASTRA_*` (also `codex`) and `FABLE_HIGH` were catalog-eligible but not
    exercised: `ASTRA_*` shares `SOL_HIGH`'s harness and is documented as for
    "exceptionally difficult" planning, out of scope for a tier comparison;
    `FABLE_HIGH` is blocked by `NO_EXTRA_PAID_USAGE`/CLI version per
    `MODEL_CATALOG.json` and was correctly refused by `planner_status`.
- Live-call count: **13 in the final corpus** (10 `SOL_HIGH` + 3 `OPUS_HIGH`
  comparison), **+2 repair-verification reruns** = 15 substantive planner
  calls in the reported evidence. A further ~4 real calls were spent on an
  earlier corpus pass that a genuine infra defect invalidated (§E) before it
  was fixed and the corpus re-run cleanly; those are not in the final tables.

## C. Evaluation corpus

Ten cases: the eight synthetic/controlled classes (§9 A–H) plus two
project-grounded exercises (§10), each a `(workflow, anchor, operator
instruction)` triple against an existing real fixture in `WORKFLOWS/` —
`PLANNER_SLICE_V1`, `MERGE_SLICE_V1`, `MULTIROUTING_SLICE_V1`, and
`IMPLEMENT_REVIEW_REPAIR_V1` — no new fixtures were authored. See
`planner_live_validation.py`'s `CASES` list for the exact instruction text and
rationale per case; the design deliberately varied which existing edge (if
any) each anchor already carried, so detach/splice, FIRST_MATCH ordering, an
existing MERGE, and HUMAN_GATE semantics were each tested at least once
against a genuinely pre-existing graph, not a purpose-built toy.

## D. Baseline results

| case | profile | status | nodes | edges | detach | wall (s) | quality | notes |
|---|---|---|---|---|---|---|---|---|
| A_simple_linear | SOL_HIGH | READY | 1 | 2 | 1 | 22.6 | 3 | accept-ready |
| B_parallel_merge | SOL_HIGH | INVALID | 3 | 5 | 1 | 28.6 | 1 | role on MERGE |
| B_parallel_merge | SOL_HIGH *(after repair)* | READY | 4 | 6 | 1 | 40.1 | 3 | accept-ready, real Accept+Run |
| B_parallel_merge | OPUS_HIGH | INVALID | 3 | 5 | 1 | 85.9 | 1 | same defect, cross-model |
| C_review_repair | SOL_HIGH | INVALID | 6 | 14 | 0 | 85.7 | 1 | role on MERGE + over-scoped |
| D_existing_edge_splice | SOL_HIGH | READY | 1 | 2 | 1 | 24.1 | 3 | accept-ready |
| E_first_match_hazard | SOL_HIGH | READY | 2 | 5 | 1 | 40.4 | 3 | accept-ready |
| E_first_match_hazard | OPUS_HIGH | READY | 3 | 8 | 1 | 119.6 | 3 | more elaborate, still legal |
| F_existing_merge | SOL_HIGH | READY | 1 | 2 | 1 | 30.2 | 2 | legal edge-id-reuse workaround |
| F_existing_merge | OPUS_HIGH | INVALID | 1 | 3 | 1 | 105.4 | — | correctly refused (intended) |
| G_human_gate | SOL_HIGH | READY | 2 | 5 | 0 | 45.9 | 1 | legal but runtime-dead routing |
| G_human_gate | SOL_HIGH *(after repair)* | INVALID | 0 | 0 | 0 | 26.2 | — | declined rather than repeat the mistake |
| H_ambiguous | SOL_HIGH | READY | 3 | 8 | 1 | 52.7 | 2 | bounded, assumptions stated |
| RP1_impl_review_repair | SOL_HIGH | INVALID | 4 | 11 | 2 | 98.6 | 1 | hit the *workflow's own* node cap |
| RP2_research_merge_decision | SOL_HIGH | READY | 3 | 5 | 1 | 45.3 | 3 | accept-ready |

Aggregate (13-call final corpus, pre-repair-verification):

- **Provider dispatch success (post-fix): 13/13 (100%)** — every call returned
  real, parseable structured output.
- **Schema-envelope-valid: 13/13 (100%)** — every failure was a closed-set or
  business-rule refusal, never a malformed-JSON/shape failure.
- **`PROPOSAL_READY` rate: 8/13 (61.5%)**
- **Accept-ready (quality 3) of all attempts: 5/13 (38.5%)**; **of `READY`
  proposals: 5/8 (62.5%)**
- **Modify-candidate rate: 2/13 (15.4%)** clean cases (both instances of the
  same one-field fix), one weaker partial candidate
- Token usage/cost: **not reported.** `aaw_planner.provider_planner`'s
  telemetry dict does carry a `usage` block, but
  `aaw_bridge.plan_from_node`'s `planner_row` only forwards a fixed subset of
  telemetry keys (`harness, provider, profile, model, effort, access_class,
  provider_session_id, wall_time_s, adapter, telemetry_status, invocation`) —
  `usage` is not one of them. This evaluation drives the bridge exactly as a
  real caller would, so it inherits that gap; wall-clock time was captured
  for every call and is reported above. Not fabricated.

## E. Observed defects (evidence only)

Two genuine defects in `aaw_planner.py`/`planner_proposal.py` were found —
neither in the planner's *judgement*, both in how AAW's own code talks to a
real strict-mode provider:

1. **Every `codex`-harness call failed dispatch.** `provider_planner`'s
   `--output-schema` is validated by OpenAI's structured-output *strict
   mode*, which (unlike plain JSON Schema) requires every object's `required`
   array to equal its full `properties` key set — `proposal_output_schema()`
   only listed the contract's own required fields.
   `invalid_json_schema: 'required' is required to ... include every key in
   properties. Missing 'role'.` Reproduced directly against the real `codex`
   CLI outside this pipeline first, to confirm it was a schema problem and
   not a package/prompt problem.
2. **Every proposal with a conditional edge then failed real validation**,
   once (1) was fixed: `outcome`/`verdict`/etc. must appear in every `when`
   object for strict mode to accept it, so a provider forced to supply all
   four `PREDICATE_KEYS` fills the unused ones with `null` —
   `_validate_when` (`workflow_schema.py`) treats a *present* key as an
   asserted predicate regardless of value, so `{"outcome": null}` read as
   "outcome must equal null" and was refused:
   `invalid outcome predicate None`.

Both are provider/transport integration defects the bounded-repair policy
explicitly allows fixing (§14: "provider/transport integration defect",
"deterministic serialization/normalization defect"). Neither weakens
`planner_proposal.validate_proposal` or `workflow_schema`/`routing_contract`
for anything else — hand-authored workflows never produce this shape, since
nothing forces them to.

A repeated *quality* pattern, not an infra defect, also emerged with enough
independent evidence to call it repeatable rather than noise: **`role` (or
`capability`/`effort`) applied to a MERGE node**, refused with
`UNSUPPORTED_FIELD`. It happened on `B_parallel_merge` under **both**
`SOL_HIGH` and `OPUS_HIGH`, and again (a different node, unreported because
validation stops at the first failure) on `C_review_repair`. Three
independent occurrences across two models is a real signal, not chance.

## F. Bounded repairs

All three repairs are additive: closed field sets, refusal codes, and
`workflow_schema`/`routing_contract` are untouched. `test_planner_proposal_pipeline.py`
gained four regression tests; the rest of the suite is unmodified.

| # | evidence | fix | file | tests | before → after |
|---|---|---|---|---|---|
| 1 | 10/10 `codex` dispatches failed `invalid_json_schema` | `proposal_output_schema()`: every object's `required` now lists every one of its own properties; genuinely optional fields made nullable instead of omitted; `when` expanded into its own closed, nullable, 4-key predicate object; edge `kind` kept a plain (non-nullable) enum since the real validator only defaults a *missing* kind, never an explicit `null` | `aaw_planner.py` | `test_the_planner_output_schema_satisfies_openai_strict_mode`, `test_edge_kind_stays_a_plain_enum_not_a_nullable_one` | 0/10 real `codex` calls reached the model → 13/13 post-fix calls dispatched and returned structured output |
| 2 | Every case with a conditional edge (post-fix-1) failed with `invalid outcome/verdict predicate None` | `apply_proposal` now materializes a proposed edge's `when` through `_materialize_when`, which drops any predicate key whose value is `None` before it reaches the real validator — a null value and an absent key are declared equivalent for a *proposed* edge only; `workflow_schema.py`/`routing_contract.py` (the shared authority for every workflow, hand-authored included) are untouched | `planner_proposal.py` | `test_a_null_valued_predicate_key_is_the_same_as_an_absent_one` | Cases C and D of the first (pre-repair-2) corpus pass both failed this way; re-run after the fix (the final corpus in §D) had zero occurrences |
| 3a | `role` on a MERGE node: `B_parallel_merge` (SOL_HIGH and OPUS_HIGH), `C_review_repair` (SOL_HIGH) — 3 independent occurrences | Added one rule to `planning_package()`'s `rules`: `role`/`capability`/`effort` are required on IMPLEMENT/REVIEW/REPAIR and must be null everywhere else; `merge_policy`/`expected_incoming` are the reverse | `planner_proposal.py` | (package-content change; covered by the existing planning-package tests, none of which assert an exact `rules` count) | `B_parallel_merge`/SOL_HIGH: `PROPOSAL_INVALID` → `PROPOSAL_READY` (4 nodes, 6 edges), then really Accepted, Saved, and Run to `WAITING_FOR_HUMAN` (§H) |
| 3b | `G_human_gate`: a legal but runtime-dead proposal — `workflow_runner.py` (~line 1529) sets `state["status"] = "WAITING_FOR_HUMAN"` and returns for *every* `HUMAN_GATE`/`FINAL_GATE`, before ever reaching `routing_contract.evaluate_gate`; any edges declared on such a node are never evaluated | Added one rule stating this plainly: a HUMAN_GATE/FINAL_GATE halts the run unconditionally; continuation is a separate resumed run, not routing through this graph's edges | `planner_proposal.py` | same as above | `G_human_gate`/SOL_HIGH: `PROPOSAL_READY`-but-wrong (legal, functionally dead) → `PROPOSAL_INVALID` with an **empty** proposal — the model declined rather than repeat the mistake. See §K for why this is read as progress, not a regression, and what is still missing. |

Repair 3a is the only one with a full before/after *acceptance* proof: the
repaired proposal was replayed (not re-called — see §H), Accepted, Saved, and
actually executed under the real runner.

Full suite after all three repairs: **`380 passed, 2 skipped`** (377 + 3 new
regression tests — see §M for the exact count and names).

## G. Final live results

- Parse/dispatch rate (post-fix): **100%** (13/13)
- Schema-envelope-valid rate: **100%** (13/13)
- `PROPOSAL_READY` rate: **61.5%** (8/13)
- Useful (quality ≥ 2) rate: **9/13 (69%)** — 7 at quality 3, 2 at quality 2
- Accept-ready (quality 3) rate: **38.5%** of all attempts, **62.5%** of
  `READY` proposals
- Modify-candidate rate: **15.4%** (2/13 clean instances, 1 partial)

## H. Real Accept/Run evidence

Both required demonstrations were done by **replaying the exact recorded
live-provider output** through the scripted planner seam (not a second live
call for the same content — see `planner_accept_run_validation.py`) and
driving the rest of the pipeline for real: `normalize_proposal` →
`validate_proposal` (recomputes `proposal_id`/`base_semantic_hash`
independent of anything echoed back, so an identical replayed proposal_id is
itself a check that nothing drifted) → `accept_proposal(persist=True)` →
`save_workflow` → `start_run` under the real runner with a scripted node
adapter (§21: "downstream task providers do not need to incur unnecessary
live-model cost").

- **Simple (Case A, `SOL_HIGH`)** — proposal `PROP-e759ad1c2021d4e6`.
  Accepted and persisted (`4931c17f...` → `bf3162b3...`), added node `N03`,
  edges `E_N01_N03`/`E_N03_N02`, detached `E_N01_CONTINUE`. Run settled:
  `N01 → N03 → N02 → N09`, lifecycle `SETTLED`, `WAITING_FOR_HUMAN`/`HUMAN_REQUIRED`
  (correct terminal state for reaching a HUMAN_GATE).
- **Branch + MERGE (Case B, `SOL_HIGH`, after repair 3a)** — proposal
  `PROP-41e3887861712020`. Accepted and persisted (`4931c17f...` →
  `2aa878c2...`), added nodes `N03, N04, N05, N06`, six edges, detached
  `E_N01_CONTINUE`. Run settled: `N01 → {N03, N04} → N05(MERGE) → N06 → N02 →
  N09`, lifecycle `SETTLED`, `WAITING_FOR_HUMAN`. A genuine `ALL_MATCHES`
  fan-out into a closed-incoming `MERGE`, produced by a real model call,
  executed correctly end to end.

## I. Model / provider assessment

Only `SOL_HIGH` (`gpt-5.6-sol`/`codex`) and `OPUS_HIGH` (`claude-opus-5`/`claude`)
were exercisable here without a paid-API dependency (`ASTRA_*` shares
`SOL_HIGH`'s harness; `FABLE_HIGH` is blocked by policy). Three cases were
run under both for a direct comparison:

- **`B_parallel_merge`**: both models made the *identical* mistake (`role` on
  the MERGE node). `OPUS_HIGH` is not visibly more careful here.
- **`E_first_match_hazard`**: both produced a correct, legal, `PASS`-worthy
  proposal. `OPUS_HIGH`'s was more elaborate (added a bounded REPAIR loop not
  strictly asked for) and its `assumptions`/`warnings` were unusually
  thorough — e.g. it independently flagged that the existing review node now
  has three converging incoming routes and isn't a MERGE, and asked to
  confirm that's compatible with the runtime's re-entry semantics. Genuinely
  sharper self-critique, at real cost: **~3× the wall time** (119.6s vs 40.4s).
- **`F_existing_merge`**: `SOL_HIGH` found a legal way through (reusing the
  detached edge's own id from the new node); `OPUS_HIGH` attempted an
  edge-id change and was correctly refused.

`OPUS_HIGH` calls averaged **~104s**, `SOL_HIGH` **~47s** (both effort
`high`). No usage/cost telemetry is available through the bridge (§D). Given
`SOL_HIGH` is already the catalog default, is markedly faster, and showed no
consistent quality edge over three head-to-head cases (better on one, equal
on another, worse on the "did it find the workaround" question on the
third), **the evidence does not justify moving the default to a stronger,
slower profile.**

## J. Product findings (§27)

1. **Does the real planner work well enough to use?** Yes, with a clearly
   bounded gap. Dispatch and schema-envelope reliability are 100% post-fix;
   61.5% of real attempts produced a legal proposal, and most of those
   (62.5%) needed zero edits. Detach/splice handling and FIRST_MATCH ordering
   were correct in every case that exercised them (5/5 and 2/2
   respectively). One bounded repair, made and verified live in this same
   session, flipped a failing case to a fully working, actually-executed
   proposal.
2. **Accept-ready percentage?** 38.5% of all attempts; 62.5% of the
   proposals that reached `PROPOSAL_READY`.
3. **Useful-but-would-benefit-from-Modify percentage?** ~15% cleanly (both
   instances of the identical one-field MERGE-role mistake), a bit higher
   (~30%) if the weaker partial candidate (`C_review_repair`) is counted.
4. **Three dominant failure modes:** (1) `role`/`capability`/`effort`
   misapplied to a MERGE node — 3 occurrences across both models; (2)
   unrequested graph complexity for a modestly-scoped ask (`C`, `RP1`); (3)
   a legal-but-runtime-dead proposal from not knowing a HUMAN_GATE never
   evaluates its own outgoing edges (`G`, before repair 3b).
5. **Is FIRST_MATCH actually a practical problem?** No evidence of it in
   this round. The one case built to provoke it (`E`) was handled correctly
   by both models, and it never appeared incidentally elsewhere. §15 decision:
   **(A)** — the existing package is sufficient; do not build a
   proposal-normalization stage on this evidence.
6. **Is refusal to splice into an existing MERGE a practical problem?**
   Mixed, and less of a blocker than expected: `SOL_HIGH` found a fully legal
   route around it (reuse the detached edge's own id from the new source);
   `OPUS_HIGH` did not and was correctly refused. §16 decision: **defer** —
   the restriction did not, in this evidence, prevent a useful plan from a
   capable planner; keep the MERGE-protection contract exactly as frozen.
7. **Is the current planning package sufficient?** Mostly, and it measurably
   improved twice in this session. Two concrete gaps were found and fixed
   with evidence (repairs 3a/3b, §F); one more is identified but not yet
   fixed (the workflow's own `limits.max_nodes`, §K).
8. **Is a stronger planner model materially better?** Not on this evidence.
   `OPUS_HIGH` was ~3× slower with no consistent quality edge across three
   head-to-head cases (see §I).
9. **Is Modify justified by evidence?** A narrow yes: ~15% of attempts fail
   for a single removable field, and the SAME defect recurred identically
   across models and cases. That said, one of the three occurrences was
   already recovered for free by a text-only package fix with no Modify
   feature at all — so the case for Modify is real but not urgent.
10. **Single highest-value next planner improvement?** Extend the
    already-twice-validated pattern (a one-line rule addition to
    `planning_package()`) to state that a proposal's node count is bounded
    by the *workflow's own* `limits.max_nodes` remaining budget, not only
    `ProposalLimits.max_nodes`. Cheap, mechanical, and would have prevented
    `RP1`'s failure outright.

## K. Remaining gaps, ranked by operational importance

1. **HUMAN_GATE-gated follow-on steps have no answer yet.** After repair 3b,
   the model correctly stopped proposing a functionally-dead structure for
   "gate this step behind a second human approval" — but it also stopped
   proposing *anything*, so the operator's actual request is still
   unaddressed. The honest next step is telling the planner what a
   *working* answer looks like (two new nodes with no edge between them,
   continuation being a separate resumed run) rather than leaving it to
   infer that from a purely negative rule. Not attempted here — one repair
   per observed failure, re-tested once, is the bound this session set for
   itself; a second iteration on the same case would need its own fresh
   evidence.
2. **`role`/`capability`/`effort`-on-MERGE may recur on other node
   combinations the corpus didn't reach** (e.g. a HUMAN_GATE proposed
   alongside a MERGE in the same graph). The fix is evidenced only against
   the exact shape that failed.
3. **RP1's workflow-level node cap** (§J.10) — a cheap, well-understood fix,
   not yet made.
4. **No usage/cost telemetry reaches this evaluation** (§D) — a bridge
   contract gap, not a planner-quality one, but it means a future evaluation
   answering "what does this cost" needs `aaw_bridge.plan_from_node` to
   forward `telemetry.get("usage")`, which it currently drops.
5. **Model comparison is three cases on two profiles** — real evidence, not
   a benchmark. A materially different conclusion on §I would need more.

## L. Next recommendation

**One package-content addition** (the pattern already validated twice in
this session): add the `limits.max_nodes`-budget rule from §J.10, then
re-run `RP1_impl_review_repair` once to confirm before/after. Do **not** yet
build: a FIRST_MATCH proposal-normalization stage (§15 decision A), a
MERGE-rewrite mechanism (§16: defer), or a `Modify` feature (§9: real but
not urgent signal). Do not move the default planner profile off `SOL_HIGH`.

## M. Tests

Full deterministic suite after every repair in this document:

```
380 passed, 2 skipped
```

(baseline `377 passed, 2 skipped` + 3 new regression tests in
`test_planner_proposal_pipeline.py`:
`test_the_planner_output_schema_satisfies_openai_strict_mode`,
`test_a_null_valued_predicate_key_is_the_same_as_an_absent_one`,
`test_edge_kind_stays_a_plain_enum_not_a_nullable_one`). No existing test was
modified or weakened; no live-provider test was added to the deterministic
suite — every live call in this document was run by hand, once, via
`planner_live_validation.py`, kept entirely out of `pytest`'s collection so
the regular suite stays hermetic and fast.

## N. Verdict

**PASS WITH GAPS.**

Not `STRONG PASS`: one recurring, well-evidenced failure class (`role` on a
non-LLM node, ~23% of raw attempts) was only partially retired — the fix
demonstrably worked on the case it was built from (`B`), but `C` and a
second, cross-model recurrence were not individually re-verified, and two
further gaps (RP1's node cap, the HUMAN_GATE follow-on question) are
identified but not yet closed. Not `REPAIR` or `BLOCKED`: dispatch and
schema-envelope reliability are 100% post-fix, the majority of proposals
that validate need no edits, detach/splice and FIRST_MATCH handling were
correct in every case that exercised them, both required capability
demonstrations (simple splice; branch+MERGE) were not just generated but
really Accepted, Saved, and Run to completion by the real runner, and every
observed failure is understood, bounded, and traced to a specific line of
code or a specific missing sentence in the planning package — nothing here
points at an architectural mismatch. No safety boundary was weakened at any
point: the MERGE-protection and FIRST_MATCH-no-silent-repair rules were
exercised repeatedly and held every time, including once by a planner
finding a legal way to work *with* them rather than around them.
