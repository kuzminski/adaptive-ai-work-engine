#!/usr/bin/env python3
"""AAW PRODUCTION USE GATE V0.1 — real Accept/Run evidence.

Identical method to planner_accept_run_validation.py / _v0_3 / _v0_4: replays
the exact recorded content of a proposal a *live* planner call already
produced (`EVIDENCE/planner_production_use_gate_v0_1.json`) through the
scripted planner seam, then really Accepts, Saves, and Runs it under the real
runner with a scripted downstream node adapter -- no second live model call
for the same content. Only the original planner call was live.

Executes 5 of the 8 PROPOSAL_READY cases from the Production Use Gate V0.1
corpus: 3 simple/moderate single-node insertions and 2 structurally
non-trivial proposals (a real ALL_REQUIRED branch+MERGE, and an
IMPLEMENT->REVIEW->REPAIR trio with a FALLBACK route).

Usage:

    python planner_production_use_gate_run_v0_1.py
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import aaw_bridge
import aaw_llm_test_adapter
import aaw_planner
import aaw_planner_test_adapter as planner_adapter
import planner_proposal as pp
import planner_slice_evidence as pse

HERE = Path(__file__).resolve().parent
EVIDENCE = HERE / "EVIDENCE" / "planner_production_use_gate_v0_1.json"

NODE_SCRIPT = {"delay_seconds": 0.0, "nodes": {"*": {
    "outcome": "PASS", "verdict": "PASS",
    "summary": "scripted PASS -- downstream node execution is not the subject "
               "of this evaluation; only the planner call was live",
    "recommended_next_action": "the gate decides"}}}

CASES_TO_RUN = (
    "DOC_extract_verify_synthesize",
    "AMBIGUOUS_cleanup_vague",
    "AMBIGUOUS_handoff_ready",
    "RESEARCH_two_explanations_one_verdict",
    "DOC_numbers_match_sources",
)


def _load(case_id: str, profile: str, path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return next(r for r in data["results"]
               if r["case_id"] == case_id and r["requested_profile"] == profile)


def _replay_script(record: dict[str, Any]) -> dict[str, Any]:
    return {"schema_version": planner_adapter.SCRIPT_SCHEMA_VERSION, "delay_seconds": 0.0,
           "anchors": {record["anchor_node_id"]: record["full_proposal"]}}


def head(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def accept_and_run(*, label: str, record: dict[str, Any], out_root: Path) -> dict[str, Any]:
    head(f"{label}: replaying the live proposal ({record['case_id']} / "
        f"{record['requested_profile']}, proposal {record['proposal_id']})")

    root = out_root / label
    repo, tree = pse.workspace(root)
    stats = root / "03_STATS"
    import workflow_runner as runner
    runner.STATS_ROOT = stats
    workflows = pse.sandbox_workflows(root)
    bridge = aaw_bridge.AawBridge(workflows_root=workflows, stats_root=stats)
    path = workflows / f"{record['workflow_id']}.json"
    script = _replay_script(record)
    with aaw_planner.planner_adapter_scope(planner_adapter.scripted_planner(script)):
        frame = bridge.plan_from_node(record["workflow_id"], record["anchor_node_id"],
                                      instruction=record["operator_instruction"])
    print(f"  replay status     {frame['status']}")
    assert frame["status"] == pp.PROPOSAL_READY, frame.get("diagnostics")
    assert frame["proposal"]["proposal_id"] == record["proposal_id"], \
        "replayed content did not reproduce the live proposal's identity"
    print(f"  proposal_id       {frame['proposal']['proposal_id']} (matches the live run)")

    report = bridge.accept_proposal(frame["proposal"]["proposal_id"], persist=True)
    after = pse.state(path)
    print(f"  accept status     {report['status']}  persisted={report['persisted']}")
    print(f"  semantic hash     {report['previous_semantic_hash'][:16]} -> "
          f"{report['semantic_hash'][:16]}")
    print(f"  added nodes       {report['added_node_ids']}")
    print(f"  added edges       {report['added_edge_ids']}")
    print(f"  detached edges    {report['detached_edge_ids']}")
    assert after["semantic_hash"] == report["semantic_hash"]
    assert bridge.load_workflow(record["workflow_id"])["valid"] is True

    adapter = aaw_llm_test_adapter.scripted_adapter(NODE_SCRIPT)
    handle = bridge.start_run(record["workflow_id"],
                              goal=f"Production Use Gate V0.1 accept/run evidence: {label}",
                              repo=repo, worktree=tree, preprocess_policy="OFF", adapter=adapter)
    run_id = handle["run_id"]
    since, deadline = 0, time.monotonic() + 60.0
    lifecycle = None
    while time.monotonic() < deadline:
        batch = bridge.events(run_id, since=since)
        since = batch["last_sequence"]
        lifecycle = batch["lifecycle"]
        if lifecycle == aaw_bridge.RUN_SETTLED:
            break
        time.sleep(0.1)
    state = bridge.run_state(run_id)
    print(f"  run lifecycle     {lifecycle}")
    print(f"  run status        {state.get('status')}")
    print(f"  final_outcome     {state.get('final_outcome')}")
    print(f"  completed nodes   {[n.get('node_id') for n in state.get('completed_nodes', [])]}")
    assert lifecycle == aaw_bridge.RUN_SETTLED, "run did not settle within the evidence window"

    return {
        "label": label, "case_id": record["case_id"], "profile": record["requested_profile"],
        "proposal_id": frame["proposal"]["proposal_id"],
        "accept_status": report["status"], "persisted": report["persisted"],
        "semantic_hash_before": report["previous_semantic_hash"],
        "semantic_hash_after": report["semantic_hash"],
        "added_node_ids": report["added_node_ids"], "added_edge_ids": report["added_edge_ids"],
        "detached_edge_ids": report["detached_edge_ids"],
        "run_id": run_id, "run_lifecycle": lifecycle, "run_status": state.get("status"),
        "run_final_outcome": state.get("final_outcome"),
        "run_completed_nodes": [n.get("node_id") for n in state.get("completed_nodes", [])],
    }


def main() -> int:
    out_root = (HERE / "PLANNER_PRODUCTION_USE_GATE_RUN_EVIDENCE").resolve()
    results = []
    for case_id in CASES_TO_RUN:
        record = _load(case_id, "SOL_HIGH", EVIDENCE)
        results.append(accept_and_run(label=case_id, record=record, out_root=out_root))

    out_path = HERE / "EVIDENCE" / "planner_production_use_gate_run_v0_1.json"
    out_path.write_text(json.dumps({"results": results}, indent=2, default=str), encoding="utf-8")
    print(f"\nwrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
