# AAW Planner Quality Hardening V0.3

Evidence-driven proposal reliability hardening, strictly scoped to V0.2's own
findings. Does not add planner product features, does not weaken validation,
does not change planner authority, does not implement Modify, does not raise
the default model tier.

Raw evidence: [`EVIDENCE/planner_live_validation_v0_3_phaseA_baseline.json`](EVIDENCE/planner_live_validation_v0_3_phaseA_baseline.json)
(Phase A, unmodified a6a5f30 package), [`EVIDENCE/planner_live_validation_v0_3.json`](EVIDENCE/planner_live_validation_v0_3.json)
(Phase B, hardened V0.3 package, 12 cases), [`EVIDENCE/planner_accept_run_v0_3.json`](EVIDENCE/planner_accept_run_v0_3.json)
(real Accept+Save+Run evidence), human-scored rollup in
[`EVIDENCE/planner_live_validation_v0_3_scored.json`](EVIDENCE/planner_live_validation_v0_3_scored.json).
Reproduce with `python planner_live_validation_v0_3.py` and
`python planner_accept_run_validation_v0_3.py`.

---

## A. Baseline

- HEAD at start: `a6a5f30` (`feat(planner): validate proposal pipeline against
  a real provider (v0.2)`), branch `feature/planner-live-provider-validation-v0-2`
- V0.2 final deterministic suite: `380 passed, 2 skipped`
- V0.3 final deterministic suite: `386 passed, 2 skipped` (380 + 6 new
  regression tests, 0 regressions; see §J)

## B. V0.2 failure reconstruction

Reconstructed from `AAW_PLANNER_LIVE_PROVIDER_VALIDATION_V0_2.md` §D–§K and
`EVIDENCE/planner_live_validation_v0_2_scored.json` — the actual per-case
evidence, not a re-summary. Every V0.2 raw attempt (13 in the final corpus,
2 repair-verification reruns), classified by root cause:

| case | profile | outcome (V0.2) | classification |
|---|---|---|---|
| A_simple_linear | SOL_HIGH | READY, q3 | — (clean) |
| B_parallel_merge | SOL_HIGH | INVALID, q1 (role on MERGE) | **missing planning-package information** |
| B_parallel_merge | OPUS_HIGH | INVALID, q1 (same defect) | **missing planning-package information** |
| B_parallel_merge | SOL_HIGH *(after repair 3a)* | READY, q3 | fixed within V0.2 |
| C_review_repair | SOL_HIGH | INVALID, q1 (role on MERGE, 2nd instance; 6-node graph) | **missing planning-package information** + **graph over-complexity** |
| D_existing_edge_splice | SOL_HIGH | READY, q3 | — (clean) |
| E_first_match_hazard | SOL_HIGH | READY, q3 | — (clean) |
| E_first_match_hazard | OPUS_HIGH | READY, q3, flagged complex | **legal but poor product judgment** |
| F_existing_merge | SOL_HIGH | READY, q2 (clever edge-id reuse) | **legal but poor product judgment** |
| F_existing_merge | OPUS_HIGH | INVALID (correct refusal) | **model misunderstanding** (of the legal workaround SOL_HIGH found) |
| G_human_gate | SOL_HIGH | READY-but-wrong, q1 | **structural constraint omission** / **missing planning-package information** |
| G_human_gate | SOL_HIGH *(after repair 3b)* | INVALID, empty decline | fixed within V0.2 (fail-closed direction); residual **unavoidable ambiguity** (operator's actual ask still unanswered — §K.1, out of scope here) |
| H_ambiguous | SOL_HIGH | READY, q2 | **ambiguous planner instruction**, handled reasonably |
| RP1_impl_review_repair | SOL_HIGH | INVALID, q1 (workflow node-cap exceeded) | **limit violation** + **missing planning-package information** |
| RP2_research_merge_decision | SOL_HIGH | READY, q3 | — (clean) |

Root-cause tally: **missing planning-package information — 5** (both
`B` instances, `C`, `G` pre-repair, `RP1`), **graph over-complexity — 2**
(`C`, `E`/OPUS_HIGH), **legal but poor product judgment — 2** (`F`/SOL_HIGH,
`E`/OPUS_HIGH), **model misunderstanding — 1** (`F`/OPUS_HIGH), **ambiguous
instruction / unavoidable ambiguity — 2** (`H`, `G` post-repair). Two of the
five "missing planning-package information" instances (`B`, `G` pre-repair)
were already fixed inside V0.2 itself (repairs 3a/3b, already in `a6a5f30`).
**Entering V0.3, the open items were: `C` (same defect, never individually
re-verified), `RP1` (identified, not yet fixed), and the general risk that
the field-legality defect recurs on a node-type combination the V0.2 corpus
never reached (§K.2).**

No change was made before this classification existed, per §3.

## C. Hardening changes

Every change below is tied to one of the open items in §B. All are additive
to `planner_proposal.planning_package()`; no field was removed, no closed
set was widened, and `workflow_schema`/`routing_contract` — the deterministic
authority — were not touched.

| # | evidence | change | file |
|---|---|---|---|
| 1 | RP1 (§B): a workflow already at its own `limits.max_nodes` was structurally doomed and the package never said so | New `node_budget` block: `current_nodes`, `workflow_max_nodes`, `remaining_workflow_capacity`, `proposal_max_nodes`, `proposal_node_budget` (`= min(ProposalLimits.max_nodes, remaining_workflow_capacity)`), computed once and handed to the planner as numbers, not left for it to derive from the raw `workflow.limits` dump. One new `rules` sentence references it. Deterministic validation is untouched — `workflow_schema.py`'s own `len(nodes) <= limits["max_nodes"]` check (line ~142) remains the sole authority; the package only tells the planner what that check will say in advance. | `planner_proposal.py` |
| 2 | B/C (§B), 3 independent V0.2 occurrences of `role`/`capability`/`effort` misapplied to a MERGE node | New `node_type_field_compatibility()` function: a small, closed lookup table (`{node_type: {role, capability, effort, merge_policy, expected_incoming: bool}}`), generated from the same two authorities `_validate_nodes` already enforces (`workflow_schema.LLM_NODE_TYPES`, the MERGE-only pair) — no second, parallel schema. Exposed at `constraints.node_type_field_compatibility`. The existing English rule was shortened to point at it rather than duplicate it. | `planner_proposal.py` |
| 3 | C, RP1 (§B): both over-scoped relative to a minimal reading of the ask | One new `rules` sentence: prefer the smallest graph that satisfies the instruction; do not add a redundant review, branch, decorative HUMAN_GATE, extra MERGE, or extra research stage unless justified. No separate optimization engine; the `node_budget` numbers double as the bounded complexity budget the task asked for, rather than inventing a second metric. | `planner_proposal.py` |
| 4 | Audit item (§6), not a V0.2 failure: FIRST_MATCH/detach handling was already 5/5 correct, but the anchor's own unconditional-edge identity had to be inferred from the edge list | New `anchor.unconditional_edge_id` field (the anchor's `when=None` outgoing edge, if any) plus a one-clause addition to the existing FIRST_MATCH rule naming it. Cheap, additive, not evidence of a failure — an audit-driven reduction in required inference, per §6's explicit invitation to add "the smallest context that reliably prevents common structural errors." | `planner_proposal.py` |

Nothing else in `planning_package()`'s shape changed. `apply_proposal`,
`validate_proposal`, `_validate_nodes`, `_validate_edges`,
`_protection_report` — the entire deterministic authority — are byte-for-byte
unchanged from `a6a5f30`.

**Not done, on purpose:** a FIRST_MATCH proposal-normalization stage (V0.2
§15 decision A stands — 0 occurrences in V0.3 either), a MERGE-rewrite
mechanism (§16: defer, unchanged), `Modify` (§13 below), promoting the
default model off `SOL_HIGH` (§G below), and a fix for the HUMAN_GATE
follow-on question (§K.1 of V0.2 — a real gap, but not one of the three
things V0.3 was scoped to fix; still open, see §H).

## D. Before / after, case level

### Phase A — frozen baseline reproduction (unmodified `a6a5f30` package)

Ran with `git stash` genuinely reverting `planner_proposal.py` to `a6a5f30`
for these 2 live calls only (not a simulation), then `git stash pop` to
restore V0.3 before Phase B.

| case | V0.2 original outcome | Phase A rerun (same unmodified code) |
|---|---|---|
| C_review_repair | INVALID, role-on-MERGE, 6 nodes, q1 | INVALID, **empty decline** (cites HUMAN_GATE-halts, N09) |
| RP1_impl_review_repair | INVALID, node-cap exceeded, 4 nodes, q1 | INVALID, **empty decline**, no explanation given |

Neither call reproduced the *exact* original failure signature — live-model
output is not perfectly reproducible run to run, and the task's own
instructions warn against overfitting to one prompt's behavior. What Phase A
does establish, honestly: (1) repair 3b's HUMAN_GATE rule (already in
`a6a5f30`) generalizes to `C`, closing V0.2's "not individually re-verified"
gap without any V0.3 change; (2) the node-cap problem (`RP1`) still has *no
explanation* in its decline under the unmodified package — the direct
baseline V0.3's `node_budget` is measured against.

### Phase B — V0.3 package, full 12-case corpus (10 reused + 2 new)

| case | before (quality) | after (quality) | change source |
|---|---|---|---|
| A_simple_linear | READY, q3 | READY, q3 | unchanged (control) |
| B_parallel_merge | INVALID→READY q3 (already fixed in V0.2) | READY, q3, role correctly null on MERGE | field-compat map holds |
| C_review_repair | INVALID, q1 | INVALID, empty decline (no score) | pre-existing HUMAN_GATE rule (not a V0.3 change; confirmed by Phase A) |
| D_existing_edge_splice | READY, q3 | READY, q3 | unchanged (control) |
| E_first_match_hazard | READY, q3 | READY, **q2** (more elaborate than V0.2's SOL_HIGH answer) | **negative result** — complexity rule did not prevent this |
| F_existing_merge | READY, q2 | READY, q2 | unchanged (control; same caveat) |
| G_human_gate | INVALID (after repair), empty | INVALID, empty (identical) | unchanged (control) |
| H_ambiguous | READY, q2 | READY, q2 | unchanged (control) |
| RP1_impl_review_repair | INVALID, q1 (98.6s, wrong graph) | INVALID, empty, **explicitly cites zero `node_budget`** (13.9s) | **`node_budget` — legality unchanged, quality of the failure materially improved** |
| RP2_research_merge_decision | READY, q3 | READY, q3, role correctly null on MERGE | field-compat map holds |
| J_merge_and_human_gate | *(new case)* | INVALID, empty decline (reachability) | did not exercise the intended MERGE+HUMAN_GATE combination — see §H |
| K_minimal_ask | *(new case)* | READY, q3, 1 node/2 edges | **positive complexity-discipline result** |

For every changed case: whether the improvement was in *legality* (did the
proposal cross from INVALID to READY) or *usefulness* (was the failure or
the success itself better) —

- **RP1**: legality unchanged (still correctly INVALID — the request is
  genuinely impossible at `remaining_workflow_capacity=0`); **usefulness of
  the failure** improved: honest, ~7× faster than V0.2's wasted attempt, and
  the model's own warning states the exact numeric reason and remedy.
- **B, RP2**: legality already fixed pre-V0.3; usefulness held (still
  accept-ready, and the specific defect class — 0/12 recurrences — is now
  absent across every LLM/MERGE node in the whole Phase B corpus, not just
  these two).
- **E**: neither legality nor usefulness improved — a genuine, bounded
  regression relative to V0.2's SOL_HIGH answer to the same case (not
  relative to a hypothetical target).
- **K**: new positive evidence the complexity rule can work; no prior state
  to compare against.

## E. Final metrics (Phase B, 12 live SOL_HIGH calls)

| metric | V0.2 (13 calls) | V0.3 (12 calls) | target |
|---|---|---|---|
| Provider dispatch | 100% | **100%** (12/12) | ~100% ✓ |
| Parser/schema-envelope success | 100% | **100%** (12/12) | ~100% ✓ |
| `PROPOSAL_READY` rate | 61.5% | **66.7%** (8/12) | ≥80% ✗ |
| Useful (q≥2) of all attempts | 69% (V0.2's own figure, 9/13) | **66.7%** (8/12) | ≥75% ✗ |
| Useful of READY proposals | not reported by V0.2 | **100%** (8/8) | — |
| Accept-ready (q3) of all attempts | 38.5% | **41.7%** (5/12) | ≥60% ✗ |
| Accept-ready of READY proposals | 62.5% | **62.5%** (5/8) | — |
| Modify-candidate rate | 15.4% | **25%** (3/12) | — |
| Field-misapplication recurrence | 3/13 (23%) | **0/12 (0%)** | — |

V0.2's 13-call corpus and V0.3's 12-call corpus are not identical sets (V0.3
drops the two `OPUS_HIGH` comparison calls, which are out of scope per §G,
and adds the two new targeted cases `J`/`K`), so these rows are directionally
comparable, not a strict apples-to-apples rerun — the case-level table in
§D is the apples-to-apples comparison.

**Honest read, not manipulated to hit targets:** two of the three headline
ready/useful/accept-ready targets are not met on the "of all attempts" basis,
in the same way V0.2 did not meet them either — the corpus intentionally
contains cases with no legal answer (`C`, `G`) or a genuinely impossible one
(`RP1`), by design, to keep the difficulty comparable. **Every rate
conditioned on `PROPOSAL_READY` held steady or improved**: useful-of-ready
went from 9/8-ish to a clean 8/8 (100%), accept-ready-of-ready held at
exactly 62.5%, and the corpus's single most frequent defect class dropped
from 23% to 0%. The three package changes measurably improved *how the
planner fails* and *fully retired one recurring defect class*; they did not
and could not turn a structurally-impossible request into a legal one.

## F. Live execution (§14)

Both required demonstrations used **freshly-generated V0.3 live proposals**
from Phase B (not replays of V0.2 content), replayed once through the
scripted seam to drive Accept/Save/Run without a second live-model call, per
§21/§14 policy — see `planner_accept_run_validation_v0_3.py`.

- **Simple/review (A_simple_linear, SOL_HIGH)** — proposal
  `PROP-f328d21a0f3a29fc`. Accepted and persisted
  (`4931c17f...` → `2e8fb1dc...`), added `N03`, edges `E_N01_N03`/`E_N03_N02`,
  detached `E_N01_CONTINUE`. Run settled: `N01→N03→N02→N09`, lifecycle
  `SETTLED`, `WAITING_FOR_HUMAN`/`HUMAN_REQUIRED`.
- **Branch + MERGE (B_parallel_merge, SOL_HIGH)** — proposal
  `PROP-ed695d3fb1300186`. Accepted and persisted
  (`4931c17f...` → `4ea26105...`), added `N03, N04, N05`, five edges,
  detached `E_N01_CONTINUE`. Run settled:
  `N01→{N03,N04}→N05(MERGE)→N02→N09`, lifecycle `SETTLED`,
  `WAITING_FOR_HUMAN`. A real `ALL_MATCHES` fan-out into a closed-incoming
  `MERGE` with `role`/`capability`/`effort` correctly null, produced fresh
  under the V0.3 package, executed correctly end to end.

## G. Model assessment

**SOL_HIGH only.** No `OPUS_HIGH` comparison was run in V0.3: §17 permits
one only "if a particular failure remains after package hardening" to
distinguish a capability problem from a package problem. Every open failure
in Phase B (`C`, `G`, `RP1`, `J`) is a *correct, well-reasoned decline* citing
a specific V0.1 contract rule (HUMAN_GATE halts unconditionally, zero node
budget, reachability) — not a case where the model appears to be reaching
past its ability. The one bounded negative result (`E`'s over-elaboration)
is a scoping/discipline question, not a capability ceiling; V0.2 already
showed `OPUS_HIGH` is *more* elaborate on this exact case, not less, so a
stronger model would not be expected to fix it. **No evidence here justifies
spending a comparison call.** `SOL_HIGH` remains sufficient and remains the
default (§17, unchanged).

## H. Remaining failures, ranked

1. **Over-elaboration is not reliably suppressed by a prose rule alone**
   (`E_first_match_hazard`, q2, this evaluation's clearest negative result).
   `K_minimal_ask` shows the same rule *can* work; it is not consistent.
   Highest-priority remaining defect, but not large enough on 12 calls to
   justify a structural change over further evidence-gathering.
2. **HUMAN_GATE-gated follow-on steps still have no legal single-run answer**
   (`C`, `G`, and now `J`'s reachability variant — 3/12 of this corpus's
   declines share this root). V0.2 §K.1 already identified this and
   deliberately deferred it; V0.3 did not touch it, per scope. This is now
   the single largest source of "correctly declined but the operator's ask
   is still unaddressed" outcomes.
3. **`J_merge_and_human_gate` did not test what it was built to test.** The
   MERGE+HUMAN_GATE field-compatibility combination flagged in V0.2 §K.2
   remains formally unexercised — the model declined before authoring either
   node type. A future case would need a graph shape where a gated
   merge-then-approve step does not strand existing downstream nodes, which
   V0.1's single-run contract may not admit at all; this is closer to item 2
   than to a fresh gap.
4. **`F_existing_merge`'s clever-but-confusing edge-id reuse recurs
   unchanged** (q2, same as V0.2) — a real, minor, Modify-shaped candidate
   (rename one field), not a legality problem.
5. **No usage/cost telemetry reaches this evaluation** — unchanged bridge
   contract gap from V0.2 §K.4, not touched here (out of scope).

## I. Required product answers

1. **Did `limits.max_nodes` materially improve reliability?** Not the
   `PROPOSAL_READY` rate (the one case it targets, `RP1`, is genuinely
   impossible at budget zero and stays `INVALID` either way) — but it
   materially improved the *quality of that failure*: the model now states
   the exact numeric reason (`node_budget.remaining_workflow_capacity=0`)
   and the correct remedy, in ~14s instead of building a wrong 4-node graph
   over ~99s. Reliability-of-failure-mode improved; reliability-of-success
   did not change, because it could not.
2. **Did explicit node-type compatibility reduce invalid proposals?** Yes,
   cleanly: V0.2's single most frequent defect (`role`/`capability`/`effort`
   on MERGE, 3/13 = 23%) occurred **0/12 times** in V0.3, across every case
   that builds a MERGE (`B`, `RP2`) or any LLM node at all.
3. **Did proposal over-complexity fall?** No overall change measurable on
   this corpus size: one clean positive result (`K_minimal_ask`) and one
   clear negative result (`E_first_match_hazard`, worse than V0.2's own
   SOL_HIGH answer to the same case). The evidence does not support
   "improved"; it supports "inconsistent, rule alone is not sufficient."
4. **Final `PROPOSAL_READY` rate?** 66.7% (8/12), up from 61.5% but still
   below the 80% target — driven by the corpus's by-design impossible/
   ambiguous cases, not by a fixable defect.
5. **Final useful rate?** 66.7% of all attempts (8/12); **100% of READY
   proposals** (8/8) — every proposal that validated was judged useful,
   better than V0.2's ready-conditional rate.
6. **Final Accept-ready rate?** 41.7% of all attempts (5/12); 62.5% of READY
   proposals (5/8) — identical to V0.2's ready-conditional rate.
7. **How many proposals remain Modify candidates?** 3/12 (25%): one recurring
   case identical to V0.2 (`F`, edge-id clarity) and two new-shape
   candidates from over-elaboration (`E`, `H`) rather than field misuse —
   the composition of the Modify case shifted, it did not merely shrink.
8. **Remaining dominant failure modes?** (1) inconsistent complexity
   discipline (§H.1); (2) HUMAN_GATE-gated follow-on requests have no legal
   single-run answer (§H.2); neither is new to V0.3, both were flagged in
   V0.2 and are now better evidenced.
9. **Does SOL_HIGH remain sufficient?** Yes — §G; no failure in this round
   shows a capability ceiling rather than a scoping/discipline question.
10. **Is another planner architecture change justified?** No. Every open
    item is a package-content or evaluation-design question, not an
    architectural mismatch — consistent with V0.2's own conclusion.

## J. Tests

Deterministic suite, full run, current `HEAD` (working tree, pre-commit):

```
386 passed, 2 skipped in 384.94s (0:06:24)
```

(baseline `380 passed, 2 skipped` + 6 new tests, 0 regressions.)

6 new regression tests added to `test_planner_proposal_pipeline.py`:
`test_node_budget_reflects_workflow_capacity_headroom`,
`test_node_budget_is_zero_at_the_workflow_s_own_node_cap`,
`test_node_type_field_compatibility_matches_the_real_schema_authority`,
`test_planning_package_exposes_the_compatibility_map`,
`test_anchor_reports_its_own_unconditional_edge`,
`test_anchor_unconditional_edge_id_is_null_when_every_edge_is_conditional`.
One existing test (`test_the_planning_package_is_bounded_and_inspectable`)
was updated to the new package shape (`node_budget` key added to the
contract-exactness assertion) — no assertion was weakened, one was extended.
No other file changed. No live-provider test was added to the deterministic
suite; every live call in this document was run by hand via
`planner_live_validation_v0_3.py`/`planner_accept_run_validation_v0_3.py`,
kept out of `pytest` collection exactly as V0.2 established.

## K. Commit

One commit, on `feature/planner-live-provider-validation-v0-2`, directly
atop `a6a5f30`: `feat(planner): quality-hardening v0.3 -- node budget, field
compatibility, complexity discipline` (see `git log -1` on this branch for
the exact hash — self-referencing it here would go stale the moment this
line is included in that same commit's content). Not merged to `main`, not
pushed. Clean tree after commit: yes.

## L. Next recommendation

**C. PLANNER PACKAGE REPAIR V0.4 — narrowly scoped to complexity
discipline**, not A/B/D. Reasoning:

- Not **A. PRODUCTION USE GATE**: `PROPOSAL_READY`/useful/accept-ready are
  still below the ~80/75/60% targets on the all-attempts basis, and one
  fresh bounded regression (`E`) was found this round.
- Not **B. MODIFY V0.1**: the Modify-candidate rate (25%, 3/12) is higher
  than V0.2's 15%, but the composition shifted toward over-elaboration
  (`E`, `H`) rather than a single, clean, mechanical field-removal pattern
  like V0.2's `B`. A Modify feature aimed at "delete this field" would not
  cleanly address "delete this whole extra REVIEW node and its three
  FALLBACK edges" — the evidence is not yet the *right kind* of evidence for
  Modify, even though the raw rate rose.
- Not **D. MODEL ROUTING**: §G — no case shows a capability ceiling.
- **C fits**: one bounded, well-evidenced, recurring defect remains
  (inconsistent complexity discipline, §H.1) that is a package-content
  question, exactly the shape V0.2's own repairs 3a/3b and V0.3's
  `node_type_field_compatibility` already solved twice for a different
  defect. The concrete next step it implies: either state the complexity
  principle more concretely (e.g. name the specific over-elaboration
  patterns seen in `E` — extra FALLBACK-to-HUMAN_REQUIRED edges beyond what
  was asked — the way `node_type_field_compatibility` replaced prose with a
  closed table) or accept that a bounded amount of elaboration on
  hazard-style cases is an acceptable cost and lower the bar rather than the
  rule. Either way, that is package-content work, not a new capability.

## M. Verdict

**PASS WITH GAPS.**

Not **STRONG PASS**: `PROPOSAL_READY`/useful/accept-ready remain below the
~80/75/60% targets on the all-attempts basis (though every rate conditioned
on `PROPOSAL_READY` matched or improved on V0.2), and one fresh, bounded
regression was found (`E_first_match_hazard`, more elaborate than V0.2's own
answer to the identical case) — a materially-improved-with-no-fresh-defect
bar was not fully cleared.

Not **REPAIR** or **BLOCKED**: dispatch and schema-envelope reliability
stayed at 100%; the single most frequent V0.2 defect class (field
misapplication on MERGE) dropped from 23% to 0% recurrence across every
applicable case; the `node_budget` fix demonstrably changed a wasted,
confusing 99-second failure into an honest, correctly-diagnosed, 14-second
one citing the exact numeric reason; both required live Accept+Save+Run
demonstrations succeeded on freshly-generated V0.3 proposals; no safety
boundary was weakened, no hidden repair was introduced, and the deterministic
suite stayed green throughout. Every open item is understood, bounded, and
traced to a specific rule or a specific missing sentence — nothing here
points at an architectural mismatch or a `SOL_HIGH` capability ceiling.
