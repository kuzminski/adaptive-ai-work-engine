#!/usr/bin/env python3
"""AAW PLANNER QUALITY HARDENING V0.3 — Phase B evaluation harness.

Reuses the entire V0.2 corpus (`planner_live_validation.CASES`, ten cases)
unchanged, so before/after results stay comparable, and adds a small number
of new, targeted cases (`NEW_CASES`) aimed at the three things V0.3 actually
changed in `planner_proposal.planning_package()`:

  * `node_budget`               — reruns RP1_impl_review_repair (already in
                                   the V0.2 corpus; §K.3's exact gap)
  * node-type field compatibility — J_merge_and_human_gate: a MERGE *and* a
                                   HUMAN_GATE in the same proposal, the exact
                                   untested combination V0.2 §K.2 flagged
  * complexity discipline        — K_minimal_ask: a deliberately small ask,
                                   testing whether the model still over-builds

Same real-usage caveat as `planner_live_validation.py`: every case here is
one live subprocess dispatch to the configured harness CLI, at real latency
and real token usage. Kept to the same order of magnitude as V0.2 (13 final
calls) on purpose — see AAW_PLANNER_QUALITY_HARDENING_V0_3.md.

Usage:

    python planner_live_validation_v0_3.py                 # full V0.3 corpus, SOL_HIGH
    python planner_live_validation_v0_3.py --only J_merge_and_human_gate
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

NEW_CASES: list[dict[str, Any]] = [
    dict(case_id="J_merge_and_human_gate", cls="V0.3-1", workflow="PLANNER_SLICE_V1",
         anchor="N01", profile="SOL_HIGH",
         instruction="Investigate this from two different angles in parallel, merge both "
                     "sets of findings into one, and require an explicit second human "
                     "sign-off on the merged findings before the existing review proceeds.",
         expect="fan-out -> MERGE (ALL_REQUIRED) -> HUMAN_GATE -> existing review. Tests "
                "node_type_field_compatibility on MERGE and HUMAN_GATE together in one "
                "proposal -- the exact combination V0.2 §K.2 flagged as untested."),
    dict(case_id="K_minimal_ask", cls="V0.3-2", workflow="PLANNER_SLICE_V1",
         anchor="N02", profile="SOL_HIGH",
         instruction="Add one quick automated sanity check step before the review's normal "
                     "pass path continues. Keep it as small as possible.",
         expect="a single small node on the PASS path, nothing else -- tests the new "
                "complexity-discipline rule against a deliberately small, easy-to-over-"
                "build ask."),
]

ALL_CASES = list(v2.CASES) + NEW_CASES


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="PLANNER_LIVE_VALIDATION_RUNS_V0_3")
    ap.add_argument("--only", nargs="+", help="case_id(s) to run; default is every case")
    ap.add_argument("--profile", help="override profile_id for every case run")
    ap.add_argument("--evidence-out", default="EVIDENCE/planner_live_validation_v0_3.json")
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
        "evaluation": "AAW_PLANNER_QUALITY_HARDENING_V0.3",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "results": list(by_key.values()),
    }
    evidence_path.write_text(json.dumps(merged, indent=2, ensure_ascii=False, default=str),
                             encoding="utf-8")
    print(f"\nwrote {evidence_path} ({len(merged['results'])} recorded runs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
