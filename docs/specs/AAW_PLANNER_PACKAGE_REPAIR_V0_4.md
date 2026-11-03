# AAW Planner Package Repair V0.4 — Complexity Discipline / Smallest Sufficient Graph

A narrow, evidence-driven repair stage. Does not expand planner capabilities,
weaken validation, add hidden repair, change runtime semantics, raise the
default model tier, add Modify, or add planner autonomy.

Raw evidence: [`EVIDENCE/planner_live_validation_v0_4.json`](EVIDENCE/planner_live_validation_v0_4.json)
(8 live SOL_HIGH calls), [`EVIDENCE/planner_accept_run_v0_4.json`](EVIDENCE/planner_accept_run_v0_4.json)
(real Accept+Save+Run evidence), human-scored rollup in
[`EVIDENCE/planner_live_validation_v0_4_scored.json`](EVIDENCE/planner_live_validation_v0_4_scored.json).
Reproduce with `python planner_live_validation_v0_4.py` and
`python planner_accept_run_validation_v0_4.py`.

---

## A. Baseline

- HEAD at start: `d26377f` (V0.3), branch `feature/planner-live-provider-validation-v0-2`
- V0.3 deterministic suite: `386 passed, 2 skipped`
- V0.3 final observed rates (context, not re-derived): `PROPOSAL_READY` 66.7%,
  useful 66.7%, Accept-ready 41.7%

## B. Complexity failure reconstruction

Built from the actual recorded evidence in
`EVIDENCE/planner_live_validation_v0_2.json` and
`EVIDENCE/planner_live_validation_v0_3_scored.json` — full node/edge content
pulled and inspected, not re-summarized from memory — before any change was
made, per §3.

| Case | Requested intent | Proposed structure | Smallest sufficient structure | Unnecessary elements | Why the model likely added them |
|---|---|---|---|---|---|
| `E_first_match_hazard` (V0.3) | Gate `WORKFLOWS/`-touching changes through mandatory compliance review; everything else continues unchanged | 2 nodes / 8 edges: compliance-check REVIEW + a second "protected path" REVIEW, with 3 separate FALLBACK-to-`HUMAN_REQUIRED` edges (`BLOCKED` on node 1, an "unclassified" catch-all on node 1, `BLOCKED` on node 2) | 1-2 nodes; a single unconditional catch-all edge (declared last under FIRST_MATCH) instead of enumerating every non-primary outcome separately | 2 of the 3 FALLBACK edges (redundant with an unconditional catch-all); the second REVIEW node is defensible, not clearly required | No rule said "one unconditional catch-all can replace several explicit negative-case edges"; no rule said extra defensive FALLBACK edges for outcomes not mentioned in the instruction are optional, not required |
| `H_ambiguous` | "Make this more robust" (deliberately ambiguous) | 3 nodes: REVIEW → REPAIR → **second REVIEW**, 8 edges | 1-2 nodes: a hardening step (or REVIEW+REPAIR) feeding directly into the **existing** review | The second, freshly-invented REVIEW node whose only job was to re-check the REPAIR | No rule addressed what REPAIR's successful continuation should point to; the model's only path forward it could see was a brand-new node |
| `C_review_repair` (V0.2 original, anchor `N09` = `HUMAN_GATE`) | One more IMPLEMENT step + a strict independent review that can route defects into bounded repair | 6 nodes: IMPLEMENT→REVIEW→REPAIR→**second REVIEW**→**MERGE**→**FINAL_GATE**, 14 edges | 3 nodes: IMPLEMENT→REVIEW→REPAIR, repair's continuation routed to whatever the fresh review's own PASS path already used | Second REVIEW (same gap as `H_ambiguous`); MERGE used as a generic aggregation checkpoint with no real fan-out (§11's exact warned anti-pattern); an unrequested FINAL_GATE capstone | Same REPAIR-recheck gap, plus no rule against using MERGE without real fan-out, plus no rule against adding a terminal gate beyond what was asked. **Superseded for this specific anchor**: `N09` is a `HUMAN_GATE`, so under the rule already added in V0.2 repair 3b, this whole request is now correctly declined before any of this structure is built (confirmed live in V0.3 and again in V0.4, §D) — the three anti-patterns above are still worth fixing because they recur on non-`HUMAN_GATE` anchors (`H_ambiguous` is the live proof) |
| `RP1_impl_review_repair` (V0.2 original, anchor `N05` = `HUMAN_GATE`, workflow at `limits.max_nodes` cap) | Rate-limiting IMPLEMENT + independent REVIEW + bounded REPAIR, "matching how the rest of this workflow already handles review and repair" | 4 nodes: IMPLEMENT→REVIEW→REPAIR→**second REVIEW**, 11 edges | 3 nodes: IMPLEMENT→REVIEW→REPAIR (see below for why the existing workflow's own pattern is not directly transferable) | Second REVIEW (same gap, third occurrence) | The existing fixture's own pattern (`WORKFLOWS/IMPLEMENT_REVIEW_REPAIR_V1.json`, legacy `on_pass`/`on_fail` style) genuinely loops REPAIR back into the SAME review (`N04→N02→N03`) — but `workflow_schema._validate_reachable_and_cycles` explicitly forbids that exact loop shape for a **contract-style** (planner-authored) subgraph: *"routing-contract workflows must be acyclic; repair uses branch lineage, not a cycle."* The operator's own instruction ("matching how the rest of this workflow already handles it") is genuinely ambiguous once the acyclic constraint is factored in — a real, understandable source of the model's confusion, not a simple carelessness. **Superseded for this specific anchor** the same way as `C` (anchor is `HUMAN_GATE`, workflow is also at zero node budget — confirmed correctly declined in V0.3 and not rerun live in V0.4, see §D) |

No package change was made before this table existed.

## C. Package changes

All additive to `planner_proposal.planning_package()`'s `rules` list and one
new `complexity_budget` block. `workflow_schema`, `routing_contract`, and
every existing validation function are untouched. No deterministic
auto-pruning, no graph simplifier, no semantic-similarity machinery — per §15,
this stage tests whether the planner can produce better structure from better
context, nothing is fixed up after the fact.

| # | evidence | change |
|---|---|---|
| 1 | Table §B, column 3: no rule distinguished structure the instruction actually requires from structure that is merely possible | New rule: REQUIRED structure (explicit REVIEW when requested, MERGE only for real fan-out, HUMAN_GATE only for a real separate authorization, detach when splicing requires it) vs OPTIONAL structure, which must not be added unless omitting it would materially reduce correctness of what was actually asked for |
| 2 | Same | New rule: every proposed node must have a distinct responsibility; merge overlapping-responsibility nodes into one unless a graph rule (the acyclic requirement) forces them apart |
| 3 | `H_ambiguous`, `RP1`, `C_review_repair` — **the single most repeated pattern**: every case that added REPAIR also added a redundant second REVIEW | New rule, directly evidence-derived: REPAIR capability does not by itself justify a second REVIEW node; because the resulting graph must stay acyclic, REPAIR's successful continuation cannot loop back into the REVIEW that routed to it, but it should route forward to an existing or already-proposed node that can legally receive it, not to a freshly invented recheck node |
| 4 | `C_review_repair`'s MERGE-as-generic-aggregator | New rule: create a parallel branch only for an explicit multi-perspective request or genuine isolation need; a single node may hold multiple sub-questions when isolation has no semantic value |
| 5 | Same evidence, restated for REVIEW specifically | New rule: one REVIEW normally suffices; a second, independent REVIEW needs an explicit request for independent verification, a different authority, or staged acceptance — never merely because a REPAIR path exists |
| 6 | Audit item (§10), no direct V0.2/V0.3 HUMAN_GATE-overuse failure observed, but the existing HUMAN_GATE-halts rule said nothing about *when to add one* | New rule: add a HUMAN_GATE only for a real, separate human authorization the graph does not already provide; a step being consequential is not by itself a reason — Accept/Reject and existing execution safety already give the operator control |
| 7 | `C_review_repair`'s MERGE misuse, restated for MERGE specifically | New rule: MERGE only when actual fan-out requires a rejoin; never as a generic aggregation/synthesis node for one upstream path |
| 8 | §6: a bounded complexity budget, derived from the request rather than a global constant | New `complexity_budget` package block. `hard_max_new_nodes` is a **pointer to `node_budget.proposal_node_budget`**, not a second number — verified by a regression test that it is a string, not an int, so it cannot silently drift into a duplicated authority. `preferred_new_nodes_guidance` gives three illustrative examples (minimal single-step insertion → 1 node; explicit multi-perspective request → one node per perspective + one MERGE; review-with-repair → REVIEW + REPAIR, reusing an existing continuation) rather than a single magic number, since deriving an exact preferred count from free-text intent would require exactly the kind of semantic-similarity machinery §7/§15 forbid building in Python — the model, not this module, is the NLU component |

5 new regression tests were added (`test_complexity_budget_points_at_node_budget_not_a_second_number`,
`test_rules_state_the_repair_recheck_reuse_principle`,
`test_rules_state_required_vs_optional_structure`,
`test_rules_state_branch_review_merge_human_gate_discipline`,
`test_complexity_budget_and_new_rules_stay_within_the_package_byte_limit`),
plus the existing package-shape contract test was extended for the new
`complexity_budget` key.

## D. Before / after

8 live SOL_HIGH calls: 6 cases reused **verbatim** (same workflow, anchor,
instruction) from V0.2/V0.3 for direct comparison against already-recorded
evidence, 2 new cases (`L_repair_reuse_test`, `M_fake_merge_temptation`)
built specifically to stress the two most concrete new rules.

| case | before (node/edge/branch/review/merge/gate) | after | quality | complexity |
|---|---|---|---|---|
| A_simple_linear | 1/2/0/0/0/0, q3 | 1/2/0/0/0/0 (unchanged) | 3→3 | — /3 |
| B_parallel_merge | 3/5/2/0/1/0, q3 | 3/5/2/0/1/0 (unchanged) | 3→3 | — /3 |
| C_review_repair | *(V0.2)* 6/14/0/2/1/1, q1 → *(V0.3)* 0/0 empty decline | 0/0 empty decline (same as V0.3) | 1→n/a | n/a |
| E_first_match_hazard | *(V0.2)* 2/5, q3 → *(V0.3)* 2/8, q2 | 2/6/0/2/0/0 | 2→**3** | flagged→**2** |
| H_ambiguous | 3/8/0/2/1(REPAIR)/0, q2, flagged | **1/2/0/0/0/0** | 2→**3** | flagged→**3** |
| RP2_research_merge_decision | 3/5/2/2/1/0, q3 | 3/5/2/2/1/0 (unchanged) | 3→3 | — /3 |
| L_repair_reuse_test | *(new)* | 2/8/0/1/0/0 — REPAIR routes to the **existing** N02 review | new: **3** | new: 2 |
| M_fake_merge_temptation | *(new)* | 1/2/0/0/0/0 | new: **3** | new: **3** |

No case regressed. Two cases improved materially on both axes
(`E_first_match_hazard`, `H_ambiguous`); three controls held exactly
unchanged; both new cases produced the predicted, correct outcome. A smaller
proposal that loses necessary semantics would not count as an improvement —
none of the smaller proposals here lost anything the instruction asked for
(`H_ambiguous`'s single-node answer still gets hardened *and* reviewed, via
the existing review; `M`'s single node still combines both requested
sources).

## E. Quality metrics

| metric | V0.3 (12 calls) | V0.4 (8 calls) | target |
|---|---|---|---|
| Provider dispatch | 100% | 100% (8/8) | ~100% ✓ |
| `PROPOSAL_READY` | 66.7% | 87.5% (7/8) | ≥80% ✓ |
| Useful, all attempts | 66.7% | 87.5% (7/8) | ≥75% ✓ |
| Accept-ready, all attempts | 41.7% | 87.5% (7/8) | ≥60% ✓ |
| Accept-ready, of READY | 62.5% | 100% (7/7) | — |
| Complexity score ≥2 | n/a (new metric) | 100% (7/7 scored) | — |
| Complexity score = 3 | n/a | 71.4% (5/7 scored) | — |

**Read this with its explicit caveat, not as an unconditional headline
number**: this is a small (8-call), single-pass corpus that intentionally
excluded three of V0.2/V0.3's hardest structurally-blocked cases
(`G_human_gate`, `RP1_impl_review_repair`, `J_merge_and_human_gate`) because
they are already well-understood, non-complexity failures (HUMAN_GATE
reachability / zero node budget) whose outcome does not depend on anything
V0.4 changed — rerunning them would have spent real live-call cost to
re-confirm known behavior rather than generate new complexity evidence. The
87.5% figures describe this narrower, complexity-focused corpus honestly;
they are not a re-measurement of the full V0.2/V0.3 difficulty spread and
should not be read as "the planner now passes 87.5% of everything."

## F. Live execution

Both demonstrations used freshly-generated V0.4 live proposals (not replays
of earlier content), replayed once through the scripted seam for
Accept/Save/Run without a second live-model call, per policy.

- **Simple/review (A_simple_linear, SOL_HIGH)** — proposal
  `PROP-db6c2d75ebad9494`. Accepted, persisted (`4931c17f...` →
  `0924e58d...`), added `N03`, edges `E_N01_N03`/`E_N03_N02`, detached
  `E_N01_CONTINUE`. Run settled `N01→N03→N02→N09`,
  `WAITING_FOR_HUMAN`/`HUMAN_REQUIRED`.
- **Branch + MERGE (B_parallel_merge, SOL_HIGH)** — proposal
  `PROP-042fd30979219041`. Accepted, persisted (`4931c17f...` →
  `4bada7d6...`), added `N03, N04, N05`, five edges, detached
  `E_N01_CONTINUE`. Run settled `N01→{N03,N04}→N05(MERGE)→N02→N09`,
  `WAITING_FOR_HUMAN`.

## G. Model check

**No OPUS_HIGH escalation was run.** §16's trigger condition — "V0.4
guidance still yields repeated over-complex proposals, and it is unclear
whether the limitation is model capability or package design" — did not
occur: every complexity-relevant case in this corpus scored quality 3 and
complexity 2 or 3, with zero repeated over-complexity after the package
change. There is nothing here to diagnose as a capability ceiling.
`SOL_HIGH` remains sufficient and remains the default.

## H. Remaining failure modes, ranked

1. **Edge-level over-enumeration, not node-level.** `L_repair_reuse_test`
   gave REPAIR three separate FALLBACK-to-`HUMAN_REQUIRED` edges (`BLOCKED`,
   `FAIL`, `INVALID`) where one unconditional catch-all declared last would
   do — the exact collapsing trick `E_first_match_hazard`'s own first node
   already uses in the same corpus. This is a new, narrower, evidence-backed
   pattern (complexity has moved from the node level to the edge level,
   which is itself a sign the node-level rules worked). Not fixed here —
   out of this stage's explicit scope (§4-11 are about nodes/branches/
   REVIEW/MERGE/HUMAN_GATE, not edge enumeration) — but it is the concrete,
   named candidate for a future narrow repair, in the same evidence-driven
   shape as this stage and V0.3 before it.
2. **HUMAN_GATE-gated follow-on requests still have no legal single-run
   answer** (`C_review_repair`). Unchanged from V0.2 §K.1/V0.3 §H.2,
   confirmed again here, out of scope by design.
3. **Small-sample confidence.** 8 live calls, one pass each, no repeated-call
   variance check within this stage (V0.3's own Phase A work already showed
   the same case/profile/package can produce different outputs call to
   call). The improvements here are consistent with three stages of
   monotonic, non-overlapping evidence (V0.2's field-misapplication fix held
   in V0.3 and V0.4; V0.3's `node_budget` fix held in V0.4's `C` decline),
   which is meaningfully more than one lucky run, but it is not a
   statistically large sample.

## I. Production-gate decision

**A. PRODUCTION USE GATE** — qualified.

Every §18-A criterion is met on this stage's own evidence: Accept-ready
reached 87.5% (well past ~60%), useful reached 87.5% (well past ~75%),
remaining failures are bounded and named (§H.1, §H.2), and real proposals
executed correctly twice. Three consecutive stages (V0.2→V0.3→V0.4) show
monotonic improvement with zero regressions on any previously-fixed defect:
field-misapplication stayed at 0% recurrence, `node_budget`'s RP1 fix held,
and this stage's own controls (`A`, `B`, `RP2`) were unchanged.

The qualification: this recommendation is for the system as it already
operates — every proposal still requires explicit human Accept/Reject before
anything executes (§21 Accept/Reject semantics, unchanged). "Production use"
here means routing real operator requests to the planner and trusting the
Accept/Reject gate as the safety boundary, not unattended autonomous
application of proposals. Given §H.3's small-sample caveat, the honest
secondary recommendation is to keep the corpus growing organically from real
production usage rather than commissioning a large synthetic benchmark next.

**Not B** (accept current limit): the evidence clears the stated targets, not
merely "good enough under supervision" — supervision was never being
relaxed either way, since Accept/Reject is a permanent boundary, not a
crutch specific to this evaluation.
**Not C** (model escalation): §G — no capability ceiling in evidence.
**Not D** (architectural repair): no planner/runtime mismatch found; every
remaining item is package-content or corpus-design, the same shape V0.2/V0.3
already used successfully.

## J. Tests

```
391 passed, 2 skipped
```

(V0.3 baseline `386 passed, 2 skipped` + 5 new regression tests, 0
regressions.) Live-provider evaluation stayed outside the deterministic
suite (`planner_live_validation_v0_4.py` / `planner_accept_run_validation_v0_4.py`,
run by hand, as in V0.2/V0.3).

## K. Commit

One commit on `feature/planner-live-provider-validation-v0-2`, directly atop
`d26377f` (see `git log -1` on this branch for the exact hash). Not merged to
`main`, not pushed. Clean tree after commit: yes.

## L. Verdict

**STRONG PASS**, for this stage's own narrow scope.

Every §17 target metric was cleared (`PROPOSAL_READY` 87.5% ≥ 80%, useful
87.5% ≥ 75%, Accept-ready 87.5% ≥ 60%), with the explicit caveat in §E about
corpus size and composition. No regression in any control case, no loss of
required semantics in any smaller proposal, no safety boundary touched.
Both required live Accept+Save+Run demonstrations succeeded on freshly
generated proposals. The single most repeated V0.2/V0.3 over-complexity
pattern (a redundant second REVIEW after REPAIR) was directly targeted with
a new case (`L_repair_reuse_test`) and confirmed fixed by direct model
self-report, not inference. The one remaining named defect (edge-level
outcome over-enumeration, §H.1) is bounded, understood, and scoped for a
future stage rather than hidden or dismissed.
