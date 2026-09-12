#!/usr/bin/env python3
"""AAW PLANNER LIVE PROVIDER / REAL-WORKFLOW VALIDATION V0.2 — evaluation harness.

Drives `aaw_bridge.plan_from_node` against the REAL planner provider seam
(`aaw_planner.provider_planner`; no scripted substitute) over a small,
deterministic evaluation corpus of real AAW workflow fixtures. Everything
downstream of the planner call is the same real pipeline
`planner_slice_evidence.py` exercises with a scripted planner: bounded
package construction, normalization, the deterministic validator, the real
`workflow_schema`/`routing_contract`, and the atomic save path. Only the
planner call itself is live.

This is real usage, not free: every case here is one subprocess dispatch to
the configured harness CLI (`claude` or `codex`) at the requested profile's
model/effort, with real latency and real token usage against the account's
included plan. Kept deliberately small — see AAW_PLANNER_LIVE_PROVIDER_VALIDATION_V0_2.md
for the call budget and rationale.

Usage:

    python planner_live_validation.py                    # every case, SOL_HIGH
    python planner_live_validation.py --only A_simple_linear F_existing_merge
    python planner_live_validation.py --profile OPUS_HIGH --only B_parallel_merge

The script does not accept or execute anything by itself — it only calls
`plan_from_node` and records what came back. Accept/Run evidence is captured
separately (see `section_accept_run` and the V0.2 report) against a
proposal this script already generated, never a fresh live call.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import aaw_bridge
import aaw_planner
import planner_proposal as pp
import planner_slice_evidence as pse
import routing_contract as rc

HERE = Path(__file__).resolve().parent

CASES: list[dict[str, Any]] = [
    dict(case_id="A_simple_linear", cls="9A", workflow="PLANNER_SLICE_V1", anchor="N01",
         profile="SOL_HIGH",
         instruction="Before the review, insert a bounded research/synthesis step that "
                     "gathers context on the change, then let the review proceed exactly "
                     "as it does today.",
         expect="2-3 nodes: a research/synthesis step then the existing review path "
                "preserved via detach+re-route of E_N01_CONTINUE."),
    dict(case_id="B_parallel_merge", cls="9B", workflow="PLANNER_SLICE_V1", anchor="N01",
         profile="SOL_HIGH",
         instruction="Investigate this problem independently from two different "
                     "perspectives in parallel, then merge both sets of findings into one "
                     "before the review proceeds.",
         expect="fan-out into two branches, MERGE ALL_REQUIRED, then into the existing "
                "review; a legal closed-incoming MERGE."),
    dict(case_id="C_review_repair", cls="9C", workflow="PLANNER_SLICE_V1", anchor="N09",
         profile="SOL_HIGH",
         instruction="After human acceptance, add one more implementation step, followed "
                     "by a strict independent review that can route any defects it finds "
                     "into a bounded repair before the work is considered done.",
         expect="IMPLEMENT -> REVIEW -> REPAIR trio with a legal REPAIR-kind edge "
                "targeting a REPAIR node; anchor N09 has no existing outgoing edges."),
    dict(case_id="D_existing_edge_splice", cls="9D", workflow="PLANNER_SLICE_V1", anchor="N02",
         profile="SOL_HIGH",
         instruction="When the review passes, insert a short documentation-update step "
                     "before the human acceptance gate. Nothing about what happens when "
                     "the review does not pass should change.",
         expect="detach_edges=[E_N02_PASS], one new node, edges preserving the PASS "
                "condition into the new node and then to N09."),
    dict(case_id="E_first_match_hazard", cls="9E", workflow="PLANNER_SLICE_V1", anchor="N01",
         profile="SOL_HIGH",
         instruction="Before continuing to the review, add a check: if the change touches "
                     "anything under WORKFLOWS/, route it to a mandatory compliance review "
                     "first. For every other change, continue to the existing review "
                     "exactly as before.",
         expect="a new conditional edge plus the original continuation preserved as an "
                "explicit, correctly-ordered fallback -- the classic FIRST_MATCH "
                "unconditional-must-be-last hazard."),
    dict(case_id="F_existing_merge", cls="9F", workflow="MERGE_SLICE_V1", anchor="N05",
         profile="SOL_HIGH",
         instruction="Add a validation step on the hardening branch before it rejoins the "
                     "migration branch at the merge.",
         expect="EXPECTED REFUSAL: splicing before M08 would change M08's protected "
                "expected_incoming set. A good planner declines or the validator refuses."),
    dict(case_id="G_human_gate", cls="9G", workflow="MULTIROUTING_SLICE_V1", anchor="N09",
         profile="SOL_HIGH",
         instruction="After the existing human acceptance, add one more implementation "
                     "step for a follow-on task. That step touches deployment "
                     "configuration, so it must not run until a second, separate human "
                     "approval explicitly authorizes it.",
         expect="a HUMAN_GATE node gating a new IMPLEMENT node, correctly ordered."),
    dict(case_id="H_ambiguous", cls="9H", workflow="PLANNER_SLICE_V1", anchor="N01",
         profile="SOL_HIGH",
         instruction="Make this more robust.",
         expect="a cautious, bounded proposal with stated assumptions -- not runaway "
                "complexity for an under-specified instruction."),
    dict(case_id="RP1_impl_review_repair", cls="10.1",
         workflow="IMPLEMENT_REVIEW_REPAIR_V1", anchor="N05", profile="SOL_HIGH",
         instruction="We also need to add rate-limiting to the public API. Add an "
                     "implementation step for that, an independent review, and a bounded "
                     "repair path if the review finds issues -- matching how the rest of "
                     "this workflow already handles review and repair.",
         expect="realistic project case: implementation -> review -> bounded repair, "
                "anchored after an existing human acceptance gate."),
    dict(case_id="RP2_research_merge_decision", cls="10.2",
         workflow="MERGE_SLICE_DRAIN_V1", anchor="N01", profile="SOL_HIGH",
         instruction="Before this goes to the machine-gated review, we need two "
                     "independent security audits of the change -- static analysis and "
                     "dependency/supply-chain -- run in parallel, with their findings "
                     "merged into one report for the reviewer to read.",
         expect="realistic project case: research/synthesis -> independent verification "
                "-> merge -> decision, in a workflow that already has a REPAIR-drain "
                "MERGE downstream."),
]

# A small subset re-run under a second profile for the model-capability
# comparison (§19). Chosen for structural difficulty: fan-out/MERGE, the
# FIRST_MATCH hazard, and the protected-MERGE refusal.
COMPARISON_CASE_IDS = ("B_parallel_merge", "E_first_match_hazard", "F_existing_merge")
COMPARISON_PROFILE = "OPUS_HIGH"


def run_one(out_root: Path, case: dict[str, Any], *, profile: str | None = None) -> dict[str, Any]:
    profile_id = profile or case["profile"]
    name = f"{case['case_id']}__{profile_id}"
    bridge, path = pse.fresh(out_root, name)
    before = pse.state(path)
    started = time.monotonic()
    frame = bridge.plan_from_node(case["workflow"], case["anchor"],
                                  instruction=case["instruction"], profile_id=profile_id)
    wall = round(time.monotonic() - started, 3)
    after = pse.state(path)

    proposal = frame.get("proposal") or {}
    planner = frame.get("planner") or {}
    record = {
        "case_id": case["case_id"],
        "class": case["cls"],
        "workflow_id": case["workflow"],
        "anchor_node_id": case["anchor"],
        "operator_instruction": case["instruction"],
        "expected": case["expect"],
        "requested_profile": profile_id,
        "harness_wall_time_s": wall,
        "planner": planner,
        "status": frame.get("status"),
        "errors": frame.get("errors"),
        "diagnostics": frame.get("diagnostics"),
        "proposal_id": proposal.get("proposal_id"),
        "proposal_hash": (frame.get("summary") or {}).get("proposal_hash"),
        "intent": proposal.get("intent"),
        "node_count": len(proposal.get("nodes") or []),
        "edge_count": len(proposal.get("edges") or []),
        "detach_count": len(proposal.get("detach_edges") or []),
        "anchor_routing": proposal.get("anchor_routing"),
        "assumptions": proposal.get("assumptions"),
        "warnings": proposal.get("warnings"),
        "added_node_ids": frame.get("added_node_ids"),
        "added_edge_ids": frame.get("added_edge_ids"),
        "detached_edge_ids": frame.get("detached_edge_ids"),
        "package_bytes": (frame.get("package") or {}).get("package_bytes"),
        "input_hash": frame.get("input_hash"),
        "workflow_unchanged_by_planning": before == after,
        "base_semantic_hash": frame.get("base_semantic_hash"),
        "base_semantic_hash_after": frame.get("base_semantic_hash_after"),
        "full_proposal": proposal,
    }
    return record


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="PLANNER_LIVE_VALIDATION_RUNS")
    ap.add_argument("--only", nargs="+", help="case_id(s) to run; default is every case")
    ap.add_argument("--profile", help="override profile_id for every case run")
    ap.add_argument("--comparison", action="store_true",
                    help=f"also re-run {COMPARISON_CASE_IDS} under {COMPARISON_PROFILE}")
    ap.add_argument("--evidence-out", default="EVIDENCE/planner_live_validation_v0_2.json")
    args = ap.parse_args(argv)

    out_root = (HERE / args.out).resolve()
    wanted = set(args.only) if args.only else {c["case_id"] for c in CASES}
    cases = [c for c in CASES if c["case_id"] in wanted]

    results: list[dict[str, Any]] = []
    for case in cases:
        print(f"\n=== {case['case_id']} ({case['cls']}) — "
              f"{args.profile or case['profile']} ===", flush=True)
        record = run_one(out_root, case, profile=args.profile)
        print(f"  status {record['status']}  nodes={record['node_count']} "
              f"edges={record['edge_count']} detach={record['detach_count']} "
              f"wall={record['harness_wall_time_s']}s "
              f"model={record['planner'].get('model')}")
        if record["diagnostics"]:
            for row in record["diagnostics"]:
                print(f"    refusal {row.get('code')}: {row.get('message')}")
        results.append(record)

    if args.comparison and not args.only:
        for case_id in COMPARISON_CASE_IDS:
            case = next(c for c in CASES if c["case_id"] == case_id)
            print(f"\n=== {case['case_id']} ({case['cls']}) — {COMPARISON_PROFILE} "
                  f"[comparison] ===", flush=True)
            record = run_one(out_root, case, profile=COMPARISON_PROFILE)
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
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "results": list(by_key.values()),
    }
    evidence_path.write_text(json.dumps(merged, indent=2, ensure_ascii=False, default=str),
                             encoding="utf-8")
    print(f"\nwrote {evidence_path} ({len(merged['results'])} recorded runs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
