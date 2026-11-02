# AAW Production Use Gate V0.1

Real-operator-workflow validation of the frozen planner/runtime stack at
checkpoint `894a73f`. Not a planner-development stage: no planner prompt,
planning package, routing/MERGE/Accept/recovery/complexity code was touched
during this evaluation (§3 freeze honored in full -- zero production files
modified).

Raw evidence:
[`EVIDENCE/planner_production_use_gate_v0_1.json`](EVIDENCE/planner_production_use_gate_v0_1.json)
(10 live planner calls), [`EVIDENCE/planner_production_use_gate_run_v0_1.json`](EVIDENCE/planner_production_use_gate_run_v0_1.json)
(5 real Accept+Save+Run executions), and the consolidated per-case rollup in
[`EVIDENCE/production_use_gate_v0_1_scored.json`](EVIDENCE/production_use_gate_v0_1_scored.json).
Reproduce with `python planner_production_use_gate_v0_1.py` then
`python planner_production_use_gate_run_v0_1.py`.

---

## A. Baseline

- `HEAD`: `894a73f` (`feat(planner): package repair v0.4 -- complexity discipline / smallest sufficient graph`)
- Branch: `feature/planner-live-provider-validation-v0-2`
- Deterministic suite before this evaluation: **`391 passed, 2 skipped`** (verified fresh, matches the reported checkpoint exactly)
- Deterministic suite after: **unchanged** -- no production code was modified, so the suite was not re-run a second time (§19: no code changes → do not manufacture new tests or re-verify what wasn't touched)
- Planner provider actually exercised: `SOL_HIGH` (`gpt-5.6-sol`, harness `codex`, effort `high`) -- the catalog default, per §12. Zero escalations to `OPUS_HIGH` were used against a budget of 2 (see §G for why).

## B. Real task set

Ten new cases, none reused verbatim from the V0.2/V0.3/V0.4 corpora (some reuse an already-validated anchor with a materially different, open-ended instruction). Each is phrased as an operator would actually ask -- outcome/goal language, never graph topology ("branch", "MERGE", "REVIEW node") -- and anchored against a real existing `WORKFLOWS/` fixture:

| case | category | workflow / anchor | operator request (verbatim) |
|---|---|---|---|
| SWE_bug_investigate_fix_verify | software engineering | PLANNER_SLICE_V1 / N01 | "QA flagged that this feature sometimes silently drops data under load. Figure out what's actually going wrong, put together a fix, and make sure someone independent checks the fix actually addresses it before we call this resolved." |
| SWE_pre_release_safety_check | software engineering | MULTIROUTING_SLICE_V1 / N09 (HUMAN_GATE) | "Before this goes out the door, I want confidence the recent routing changes haven't broken any of the existing paths through this thing. Check it over properly and make the call on whether it's safe to proceed." |
| SWE_bounded_infra_change | software engineering | IMPLEMENT_REVIEW_REPAIR_V1 / N05 (HUMAN_GATE) | "This next piece touches production configuration, so build it, but don't let a single pass be good enough -- get it independently checked, and give it a real chance to be patched up if the check finds something wrong." |
| RESEARCH_two_explanations_one_verdict | research / technical analysis | MERGE_SLICE_DRAIN_V1 / N01 | "We have two competing theories about why this behaved oddly last time. I want both looked into independently and one verified conclusion out the other end -- not just a hunch." |
| RESEARCH_architecture_comparison_sanity_check | research / technical analysis | MERGE_SLICE_V1 / N01 | "Before we lock in this design direction, get a genuine, evidence-backed comparison of the two approaches on the table, then have someone sanity-check that comparison before it goes to the team." |
| DOC_extract_verify_synthesize | document / data work | PLANNER_SLICE_V1 / N02 (REVIEW) | "I've got a batch of source material that's supposed to back up the claims in this report. Pull out what it actually says, spot-check a couple of the more surprising numbers against the originals, and give me something I can actually hand to the team." |
| DOC_numbers_match_sources | document / data work | MERGE_SLICE_REPAIR_V1 / N01 | "Pull the structured data points out of this material, and before it goes anywhere near the deck, make sure the numbers actually match what the sources say." |
| AMBIGUOUS_cleanup_vague | ambiguous | MULTIROUTING_SLICE_V1 / N02 | "This is a mess. Clean it up before it goes any further." |
| AMBIGUOUS_handoff_ready | ambiguous | MERGE_SLICE_DRAIN_V1 / N02 (REVIEW) | "Get this ready for someone else to pick up cleanly." |
| AMBIGUOUS_go_deeper | ambiguous | MERGE_SLICE_REPAIR_V1 / N05 | "I don't think this step goes far enough. Go deeper on it and make sure it's actually solid before it moves on." |

All ten were dispatched live to `SOL_HIGH`/`codex`. Five of the eight
`PROPOSAL_READY` proposals were then really Accepted, Saved, and Run under
the real runner (3 simple/moderate single-node insertions, 2 structurally
non-trivial: a genuine `ALL_REQUIRED` branch+MERGE and an
`IMPLEMENT->REVIEW->REPAIR` trio with a `FALLBACK` route) -- exactly the same
replay-through-scripted-planner-seam method the V0.2/V0.3/V0.4 Accept/Run
evidence used, with a scripted PASS-everywhere downstream node adapter (§9:
downstream cost is not the subject of this evaluation).

## C. Operator outcomes

| outcome | count | cases |
|---|---|---|
| ACCEPT | 7 | SWE_bug_investigate_fix_verify, RESEARCH_architecture_comparison_sanity_check, DOC_extract_verify_synthesize, DOC_numbers_match_sources, AMBIGUOUS_cleanup_vague, AMBIGUOUS_handoff_ready, AMBIGUOUS_go_deeper |
| MODIFY-CANDIDATE | 1 | RESEARCH_two_explanations_one_verdict |
| REJECT | 2 | SWE_pre_release_safety_check, SWE_bounded_infra_change |
| MANUAL-PREFERRED | 0 | -- |

Both `REJECT`s are legal, correctly-declined empty proposals (`PROPOSAL_INVALID`,
`PROPOSAL_SHAPE` refusal), not bad proposals -- see §G for why they're still
scored `REJECT` rather than a fifth category.

## D. Product metrics

- **`PROPOSAL_READY` rate: 80%** (8/10)
- **Schema-envelope-valid rate: 100%** (10/10 -- both declines were clean, well-formed refusals)
- **Useful (quality ≥ 2) rate: 80%** (8/10)
- **Accept-ready (quality 3) rate: 70%** of all attempts (7/10); **87.5%** of `READY` proposals (7/8)
- **Modify-candidate rate: 10%** (1/10)
- **Reject rate: 20%** (2/10)
- **Manual-preferred rate: 0%** (0/10)
- **Accepted execution success rate: 100% settled without runtime error** (5/5); **60% reached full expected completion** (3/5 `WAITING_FOR_HUMAN`/`HUMAN_REQUIRED`), **40% correctly halted `BLOCKED`** (2/5) on a pre-existing fixture characteristic the live proposals themselves had already flagged before acceptance (see §E)
- **Primary product metric, (ACCEPT + genuinely useful MODIFY-CANDIDATE) / all attempts: 80%** (8/10); ACCEPT alone: **70%** (7/10)

## E. Execution

All 5 selected `PROPOSAL_READY` proposals were Accepted, Saved, and really
Run:

| case | added nodes | run lifecycle | run status | final_outcome |
|---|---|---|---|---|
| DOC_extract_verify_synthesize | N03 | SETTLED | WAITING_FOR_HUMAN | HUMAN_REQUIRED |
| AMBIGUOUS_cleanup_vague | N02C | SETTLED | WAITING_FOR_HUMAN | HUMAN_REQUIRED |
| DOC_numbers_match_sources | N10, N11, N12R | SETTLED | WAITING_FOR_HUMAN | HUMAN_REQUIRED |
| RESEARCH_two_explanations_one_verdict | N10, N11, M12 | SETTLED | BLOCKED | BLOCKED |
| AMBIGUOUS_handoff_ready | N10 | SETTLED | BLOCKED | BLOCKED |

Every run reached `SETTLED` cleanly (no crash, no runner exception, routing
matched the proposed semantics exactly, path-scoped context and MERGE/rejoin
worked correctly where present). The two `BLOCKED` outcomes are **not**
execution defects: both proposals were built on `MERGE_SLICE_DRAIN_V1`, whose
pre-existing `M08` merge is `ALL_REQUIRED` across a plain-PASS lineage
(`E_N02_PASS`) *and* a REPAIR lineage (`E_N02R_CONTINUE`). A scripted
all-PASS downstream adapter never exercises the REPAIR arm, so `M08`
correctly, deterministically never closes and the run fails closed at
`BLOCKED` rather than silently completing. Both live proposals' own
`warnings` fields explicitly named this exact risk *before* acceptance
(`"the existing M08 ALL_REQUIRED merge still requires both E_N02_PASS and
E_N02R_CONTINUE..."` / `"...the repair-owned slot remains absent on the plain
PASS lineage, so the existing drain-time fail-closed behavior... remain
intentional"`). This is `ALL_REQUIRED` MERGE fail-closed correctness working
exactly as designed, confirmed twice by real execution, and self-diagnosed
by the planner ahead of time both times.

## F. UX observations (interaction-derived only)

1. **The `BLOCKED`-on-drain risk above is visible only in free-text
   `warnings`, nowhere structurally.** An operator inspecting the ghost
   proposal in the canvas has no visual signal distinguishing "legal and
   fully executable" from "legal but sits upstream of a pre-existing MERGE
   this run is very unlikely to satisfy." They would have to read and
   correctly parse prose to catch it. Observed twice, on two different
   cases sharing the same fixture.
2. **A `HUMAN_GATE` anchor produces a silent dead end with no forward path
   inside the interaction.** Both `SWE_pre_release_safety_check` and
   `SWE_bounded_infra_change` anchored on the single most natural node an
   operator would click for "before this goes out" / "this next piece" --
   and got a correctly-reasoned but conversationally final decline. The
   explanation is good prose, but there is no next action offered (e.g.
   "try anchoring on N06 instead") from inside the same interaction.

No vague/cosmetic friction is recorded here per §10 -- both items above are
concrete, reproduced from actual interaction outcomes, not stylistic
preference.

## G. Planner failure modes (observed in this real-task corpus only)

1. **`HUMAN_GATE`-anchor dead end recurs under fresh, realistic phrasing.**
   2/10 cases (both `SWE` category) picked the release/acceptance gate as
   anchor -- an entirely natural operator choice -- and got a correct but
   totally unaddressed decline. This is the same gap identified as
   unresolved in `AAW_PLANNER_LIVE_PROVIDER_VALIDATION_V0_2.md` §K.1; it is
   now confirmed to recur on the single anchor choice a real operator is
   *most* likely to make for this class of ask, not just a synthetic
   corner case.
2. **`IMPLEMENT_REVIEW_REPAIR_V1`'s own `limits.max_nodes=5` is already
   fully consumed (5/5 nodes).** Deterministically confirmed independent of
   the model (`planning_package()`'s own `remaining_workflow_capacity`
   computation: `max(0, 5-5) = 0`). This is the same unfixed gap flagged in
   §K.3 of the V0.2 report ("RP1's workflow-level node cap... not yet
   made"), now hit a second time by an unrelated, freshly-phrased request on
   the same fixture. The planner respected the cap correctly (no
   over-budget proposal was generated) -- this is a real product-usefulness
   gap in an already-identified, already-unfixed direction, not a new
   correctness defect.
3. **Edge-outcome over-enumeration (V0.4 §13) recurred twice**
   (`RESEARCH_architecture_comparison_sanity_check`,
   `AMBIGUOUS_go_deeper`), both times as two separate explicit `FALLBACK`
   edges (one per verdict: `REPAIR`, `BLOCKED`) to the same target where one
   unconditional catch-all would suffice. Per §13's own instruction, this is
   recorded as observation only: it did not harm readability materially, did
   not cause a validation/execution problem, and did not cause an operator
   rejection in this corpus -- the threshold for prioritizing a fix was not
   met.
4. **One real, if minor, node-typing nuance**: `RESEARCH_two_explanations_one_verdict`
   used `REVIEW`-typed nodes with role `INDEPENDENT_REVIEWER` to conduct
   *primary* investigation of a theory, not review of already-completed
   work. Everything about the graph shape, routing, and MERGE was correct;
   only the node's type/role reads oddly next to what its instructions
   actually ask it to do.
5. **No dispatch or schema-envelope failures.** All 10 calls reached the
   model and returned a well-formed, schema-legal response (100%), matching
   the post-repair reliability established in V0.2-V0.4.

Zero model escalations were used against a budget of 2 (§12): both `REJECT`
cases were independently, deterministically confirmed to be structural
(a `HUMAN_GATE` anchor with no legal outgoing extension; a workflow already
at its authored node cap) rather than a plausible model-capability
limitation, so escalating to `OPUS_HIGH` would not have distinguished
anything new.

## H. Modify decision

**MODIFY-LITE JUSTIFIED, but the evidence for it is thin and the dominant
gap is elsewhere.** Only 1/10 cases (10%) produced a genuine
`MODIFY-CANDIDATE`, and the required change is a single field
(node type/role), consistent with the V0.2 report's own prior conclusion
("real but not urgent"). This round adds no new pressure toward
graph-editing or regeneration-feedback: zero cases needed a structural edit
to become acceptable, and zero cases were of the "regenerate, but change X"
shape. The more consequential finding this round is not about `Modify` at
all -- it's that 2/10 realistic requests (both hitting the single most
natural anchor for their ask) got a correct-but-terminal decline with no
forward path, which no `Modify` feature would help with (there is no
proposal to modify).

## I. Production readiness

**PASS WITH GAPS.**

Not `STRONG PASS`: 20% of realistic attempts (2/10) hit a genuine, already-
documented, still-unfixed product gap (`HUMAN_GATE` anchors as dead ends;
one of the two compounded by a workflow already at its node cap) on the
single most natural anchor choice for their category of request -- that is
disruptive to normal use, not a corner case. Not `REPAIR` or `BLOCKED`:
dispatch and schema-envelope reliability remained 100%; 87.5% of `READY`
proposals were immediately accept-ready with zero edits; every executed
proposal settled cleanly with routing, path-scoped context, and `MERGE`
fail-closed semantics all behaving exactly as designed (including two cases
where the planner itself correctly predicted an eventual `BLOCKED` outcome
ahead of acceptance); no new architectural, safety, or correctness defect
was found; both gaps present here were already known and already scoped in
prior evidence, not new surprises.

## J. Single next action

**Fix the `HUMAN_GATE`-anchor dead end (§K.1 from the V0.2 report), scoped
narrowly: extend `planning_package()`'s rules to state what a *working*
answer looks like when an operator's goal genuinely requires a second gated
approval after an existing `HUMAN_GATE`** (two new nodes with no edge
between them; continuation is a separate resumed run) rather than leaving
the planner to infer only the negative rule ("don't build the dead
structure") and stop there. This is the one gap in this round's evidence
that left a realistic, common-shape operator request (anchored at the most
natural node for it) with zero forward path, twice, out of only ten cases.
Do not implement Modify-Lite, graph editing, or regeneration feedback on
this evidence (§H). Do not touch the edge-outcome-over-enumeration pattern
(§G.3) -- it has not crossed the §13 threshold. Do not fix the
`IMPLEMENT_REVIEW_REPAIR_V1` node-cap gap as part of this action; it is a
distinct, already-scoped fix (§K.3 of the V0.2 report) that can be made
independently and cheaply, but two gaps in one bounded repair violates this
gate's own one-repair discipline.

---

## Appendix: full per-case detail

See [`EVIDENCE/production_use_gate_v0_1_scored.json`](EVIDENCE/production_use_gate_v0_1_scored.json)
for the complete durable record per case: operator request, planner
provider, proposal hash, graph size, validation status, operator outcome,
modification reason, execution result, latency, and UX friction.

### Latency

All ten calls, `SOL_HIGH`/`codex`, wall-clock (planner start -> proposal
ready/validated, single number since this bridge path does not expose a
separate validation-complete timestamp):

| case | wall (s) |
|---|---|
| SWE_bounded_infra_change | 18.42 |
| SWE_pre_release_safety_check | 30.01 |
| AMBIGUOUS_cleanup_vague | 27.38 |
| DOC_extract_verify_synthesize | 40.34 |
| DOC_numbers_match_sources | 42.23 |
| RESEARCH_two_explanations_one_verdict | 59.18 |
| RESEARCH_architecture_comparison_sanity_check | 55.41 |
| AMBIGUOUS_handoff_ready | 56.71 |
| AMBIGUOUS_go_deeper | 57.21 |
| SWE_bug_investigate_fix_verify | 63.28 |

Range 18-63s, median ~48s. Consistent with the `SOL_HIGH` averages already
reported in V0.2 (~47s); nothing here felt disruptive in actual use --
declines resolved *faster* than acceptances (both `REJECT` cases were among
the three fastest), which is the right latency shape for a
"tell the operator no" response. No optimization attempted or warranted per
§11.
