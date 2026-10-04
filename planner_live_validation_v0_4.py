#!/usr/bin/env python3
"""AAW PLANNER PACKAGE REPAIR V0.4 — complexity-discipline evaluation harness.

Reuses six cases verbatim from the V0.2/V0.3 corpus (`planner_live_validation.CASES`)
-- the ones with the most direct complexity evidence -- and adds two new,
narrowly-targeted cases aimed at the two most concrete rules V0.4 added to
`planner_proposal.planning_package()`:

  * `L_repair_reuse_test`   — tests the REPAIR/REVIEW-reuse rule (V0.2/V0.3's
                              single most repeated over-complexity pattern:
                              H_ambiguous, RP1, C_review_repair all added a
                              needless second REVIEW node after REPAIR)
  * `M_fake_merge_temptation` — tests MERGE/branch discipline against a request
                              phrased to tempt a spurious fan-out+MERGE for
                              what one node can do

Reused cases (unchanged instructions, for direct before/after comparison
against the already-recorded V0.2/V0.3 evidence -- see
AAW_PLANNER_PACKAGE_REPAIR_V0_4.md §D):

  A_simple_linear, B_parallel_merge, C_review_repair, E_first_match_hazard,
  H_ambiguous, RP2_research_merge_decision

Same real-usage caveat as the V0.2/V0.3 harnesses: every case is one live
subprocess dispatch to the configured harness CLI, at real latency and real
token usage.

Usage:

    python planner_live_validation_v0_4.py                  # full V0.4 corpus, SOL_HIGH
    python planner_live_validation_v0_4.py --only L_repair_reuse_test
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

REUSED_CASE_IDS = ("A_simple_linear", "B_parallel_merge", "C_review_repair",
                   "E_first_match_hazard", "H_ambiguous", "RP2_research_merge_decision")

NEW_CASES: list[dict[str, Any]] = [
    dict(case_id="L_repair_reuse_test", cls="V0.4-1", workflow="PLANNER_SLICE_V1",
         anchor="N01", profile="SOL_HIGH",
         instruction="Add an independent review of this implementation, with a bounded "
                     "repair path if the review finds issues, before the existing review "
                     "runs.",
         expect="minimal sufficient: REVIEW + REPAIR (2 nodes). REPAIR's successful "
                "continuation should route forward to the existing N02 review rather than "
                "to a freshly invented third review node -- the exact pattern V0.2's "
                "H_ambiguous/RP1/C_review_repair all got wrong."),
    dict(case_id="M_fake_merge_temptation", cls="V0.4-2", workflow="PLANNER_SLICE_V1",
         anchor="N01", profile="SOL_HIGH",
         instruction="Before the review, gather relevant context from both the codebase "
                     "and the design docs, and produce one combined synthesis for the "
                     "reviewer.",
         expect="minimal sufficient: 1 node (a single research/synthesis step whose own "
                "instructions cover both sources). No branch, no MERGE -- there is no "
                "independent-perspectives request here, just two inputs to one step."),
]

ALL_CASES = [c for c in v2.CASES if c["case_id"] in REUSED_CASE_IDS] + NEW_CASES


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="PLANNER_LIVE_VALIDATION_RUNS_V0_4")
    ap.add_argument("--only", nargs="+", help="case_id(s) to run; default is every case")
    ap.add_argument("--profile", help="override profile_id for every case run")
    ap.add_argument("--evidence-out", default="EVIDENCE/planner_live_validation_v0_4.json")
    args = ap.parse_args(argv)

    out_root = (HERE / args.out).resolve()
    wanted = set(args.only) if args.only else {c["case_id"] for c in ALL_CASES}
    cases = [c for c in ALL_CASES if c["case_id"] in wanted]

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
        "evaluation": "AAW_PLANNER_PACKAGE_REPAIR_V0.4",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "results": list(by_key.values()),
    }
    evidence_path.write_text(json.dumps(merged, indent=2, ensure_ascii=False, default=str),
                             encoding="utf-8")
    print(f"\nwrote {evidence_path} ({len(merged['results'])} recorded runs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
