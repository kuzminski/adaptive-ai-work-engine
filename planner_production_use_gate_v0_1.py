#!/usr/bin/env python3
"""AAW PRODUCTION USE GATE V0.1 — realistic-operator-work evaluation harness.

Reuses the exact live-provider seam and recording shape from
`planner_live_validation.run_one` (V0.2/V0.3/V0.4). This corpus is NEW: ten
cases phrased as an operator would actually ask (goal/outcome language, not
graph topology), spanning software engineering, research/technical analysis,
document/data work, and deliberately ambiguous asks, each anchored against an
existing real WORKFLOWS fixture. None of these ten (case_id, workflow,
anchor, instruction) tuples exist in the V0.2/V0.3/V0.4 corpora -- some reuse
an anchor already validated as legal, with a materially different, more
open-ended instruction.

This is real usage, not free: every case is one live subprocess dispatch to
the configured harness CLI at the requested profile's model/effort (default
SOL_HIGH), same as every prior live-validation harness in this repo.

Usage:

    python planner_production_use_gate_v0_1.py
    python planner_production_use_gate_v0_1.py --only DOC_numbers_match_sources
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import planner_live_validation as v2
import planner_proposal as pp

HERE = Path(__file__).resolve().parent

CASES: list[dict[str, Any]] = [
    dict(case_id="SWE_bug_investigate_fix_verify", cls="PUG-SWE1",
         workflow="PLANNER_SLICE_V1", anchor="N01", profile="SOL_HIGH",
         instruction="QA flagged that this feature sometimes silently drops data under "
                     "load. Figure out what's actually going wrong, put together a fix, "
                     "and make sure someone independent checks the fix actually addresses "
                     "it before we call this resolved.",
         expect="operator did not specify a graph shape; a reasonable answer investigates, "
                "implements a fix, and adds independent verification before the existing "
                "review/gate."),
    dict(case_id="SWE_pre_release_safety_check", cls="PUG-SWE2",
         workflow="MULTIROUTING_SLICE_V1", anchor="N09", profile="SOL_HIGH",
         instruction="Before this goes out the door, I want confidence the recent routing "
                     "changes haven't broken any of the existing paths through this thing. "
                     "Check it over properly and make the call on whether it's safe to "
                     "proceed.",
         expect="anchor is a HUMAN_GATE -- a working answer must respect that a HUMAN_GATE "
                "halts the run unconditionally (V0.3/V0.4 gap K.1); a proposal that reuses "
                "that dead-end shape again would be a repeat of the known failure mode."),
    dict(case_id="SWE_bounded_infra_change", cls="PUG-SWE3",
         workflow="IMPLEMENT_REVIEW_REPAIR_V1", anchor="N05", profile="SOL_HIGH",
         instruction="This next piece touches production configuration, so build it, but "
                     "don't let a single pass be good enough -- get it independently "
                     "checked, and give it a real chance to be patched up if the check "
                     "finds something wrong.",
         expect="realistic bounded IMPLEMENT->REVIEW->REPAIR ask anchored after an existing "
                "human acceptance gate, phrased by outcome rather than node names."),
    dict(case_id="RESEARCH_two_explanations_one_verdict", cls="PUG-R1",
         workflow="MERGE_SLICE_DRAIN_V1", anchor="N01", profile="SOL_HIGH",
         instruction="We have two competing theories about why this behaved oddly last "
                     "time. I want both looked into independently and one verified "
                     "conclusion out the other end -- not just a hunch.",
         expect="operator did not say 'branch' or 'MERGE'; a good proposal recognizes two "
                "independent investigations converging on one decision as the natural "
                "shape, in a workflow that already has a REPAIR-drain MERGE downstream."),
    dict(case_id="RESEARCH_architecture_comparison_sanity_check", cls="PUG-R2",
         workflow="MERGE_SLICE_V1", anchor="N01", profile="SOL_HIGH",
         instruction="Before we lock in this design direction, get a genuine, "
                     "evidence-backed comparison of the two approaches on the table, then "
                     "have someone sanity-check that comparison before it goes to the "
                     "team.",
         expect="anchor is upstream of the workflow's protected MERGE (M08); a good "
                "proposal should not need to touch M08's incoming set at all for this ask."),
    dict(case_id="DOC_extract_verify_synthesize", cls="PUG-D1",
         workflow="PLANNER_SLICE_V1", anchor="N02", profile="SOL_HIGH",
         instruction="I've got a batch of source material that's supposed to back up the "
                     "claims in this report. Pull out what it actually says, spot-check a "
                     "couple of the more surprising numbers against the originals, and "
                     "give me something I can actually hand to the team.",
         expect="anchor is the existing REVIEW node; a good proposal inserts extraction + "
                "spot-check verification + synthesis ahead of the human acceptance gate "
                "without disturbing the review's own PASS/FAIL routing."),
    dict(case_id="DOC_numbers_match_sources", cls="PUG-D2",
         workflow="MERGE_SLICE_REPAIR_V1", anchor="N01", profile="SOL_HIGH",
         instruction="Pull the structured data points out of this material, and before it "
                     "goes anywhere near the deck, make sure the numbers actually match "
                     "what the sources say.",
         expect="a modest extract-then-verify ask; watch for unrequested branching/MERGE "
                "on a workflow that already has one (residual over-enumeration risk, "
                "V0.4 §13)."),
    dict(case_id="AMBIGUOUS_cleanup_vague", cls="PUG-A1",
         workflow="MULTIROUTING_SLICE_V1", anchor="N02", profile="SOL_HIGH",
         instruction="This is a mess. Clean it up before it goes any further.",
         expect="maximally under-specified, like H_ambiguous but a fresh anchor/workflow; "
                "watch for a cautious bounded proposal with stated assumptions versus "
                "runaway complexity."),
    dict(case_id="AMBIGUOUS_handoff_ready", cls="PUG-A2",
         workflow="MERGE_SLICE_DRAIN_V1", anchor="N02", profile="SOL_HIGH",
         instruction="Get this ready for someone else to pick up cleanly.",
         expect="no concrete deliverable named; watch whether the planner asks a bounded "
                "clarifying question via assumptions/warnings or invents unwarranted scope."),
    dict(case_id="AMBIGUOUS_go_deeper", cls="PUG-A3",
         workflow="MERGE_SLICE_REPAIR_V1", anchor="N05", profile="SOL_HIGH",
         instruction="I don't think this step goes far enough. Go deeper on it and make "
                     "sure it's actually solid before it moves on.",
         expect="vague intensifier request ('deeper', 'solid') with no named risk; watch "
                "for restraint versus invented structure."),
]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="PLANNER_PRODUCTION_USE_GATE_RUNS")
    ap.add_argument("--only", nargs="+", help="case_id(s) to run; default is every case")
    ap.add_argument("--profile", help="override profile_id for every case run")
    ap.add_argument("--evidence-out", default="EVIDENCE/planner_production_use_gate_v0_1.json")
    args = ap.parse_args(argv)

    out_root = (HERE / args.out).resolve()
    wanted = set(args.only) if args.only else {c["case_id"] for c in CASES}
    cases = [c for c in CASES if c["case_id"] in wanted]

    results: list[dict[str, Any]] = []
    for case in cases:
        print(f"\n=== {case['case_id']} ({case['cls']}) — "
              f"{args.profile or case['profile']} ===", flush=True)
        record = v2.run_one(out_root, case, profile=args.profile)
        print(f"  status {record['status']}  nodes={record['node_count']} "
              f"edges={record['edge_count']} detach={record['detach_count']} "
              f"wall={record['harness_wall_time_s']}s "
              f"model={record['planner'].get('model')}")
        if record["diagnostics"]:
            for row in record["diagnostics"]:
                print(f"    refusal {row.get('code')}: {row.get('message')}")
        results.append(record)

    evidence_path = (HERE / args.evidence_out).resolve()
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, Any] = {}
    if evidence_path.is_file():
        try:
            existing = json.loads(evidence_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}
    by_key = {(r["case_id"], r["requested_profile"]): r
             for r in (existing.get("results") or [])}
    for record in results:
        by_key[(record["case_id"], record["requested_profile"])] = record
    merged = {
        "contract": pp.PROPOSAL_CONTRACT,
        "evaluation": "AAW_PRODUCTION_USE_GATE_V0.1",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "results": list(by_key.values()),
    }
    evidence_path.write_text(json.dumps(merged, indent=2, ensure_ascii=False, default=str),
                             encoding="utf-8")
    print(f"\nwrote {evidence_path} ({len(merged['results'])} recorded runs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
