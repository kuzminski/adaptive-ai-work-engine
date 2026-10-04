#!/usr/bin/env python3
"""Real walkthrough for AAW PRODUCTION USE EVIDENCE CAPTURE V0.1.

Three real-operator-session scenarios, replayed through a real
`production_use_evidence.EvidenceBridge` with only the provider seam
scripted — the same substitution convention
`planner_production_use_gate_run_v0_1.py` already uses for this repo's other
evidence walkthroughs: `aaw_planner_test_adapter`/`aaw_llm_test_adapter`
replace the paid provider calls, but routing, the real runner, the real
execution ledger/routing journal, and this module's own durable evidence are
all real.

    A  plan -> ACCEPT -> Save -> Run -> complete -> operator feedback
       -> one coherent session summary
    B  plan -> REJECT
       -> confirms no run/fabricated execution result appears for it
    C  ACCEPT -> Run -> REVIEW -> REPAIR -> repair descendant -> final PASS
       -> confirms both the original failure and the repair remain visible

Each scenario gets its own workflow file (so accepting one never mutates the
graph the next scenario plans against) but all three share one
`PLANNER_EVIDENCE_ROOT`/`03_STATS`, so the closing aggregate report spans all
three real sessions the way a real operator's history would.

Usage:

    python production_use_evidence_walkthrough.py
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

import aaw_bridge
import aaw_llm_test_adapter
import aaw_planner
import aaw_planner_test_adapter as scripted
import planner_proposal as pp
import planner_slice_evidence as pse
import production_use_evidence as puv
import workflow_runner as runner

HERE = Path(__file__).resolve().parent
OUT_ROOT = HERE / "PRODUCTION_USE_EVIDENCE_WALKTHROUGH"

SLICE = {"anchors": {"N01": {
    "intent": "Insert a bounded research pass before the review.",
    "detach_edges": ["E_N01_CONTINUE"],
    "nodes": [
        {"id": "P01", "type": "IMPLEMENT", "role": "RESEARCH_SYNTHESIZER",
         "capability": "RESEARCH_SYNTHESIZER", "effort": "medium",
         "instructions": "Research the affected surface and synthesise a bounded note.",
         "acceptance": ["A bounded synthesis exists."],
         "depends_on": ["N01"], "run_if": "ON_TRANSITION"},
    ],
    "edges": [
        {"edge_id": "E_N01_P01", "from": "N01", "to": "P01", "when": None,
         "kind": "CONTINUE", "label": "research"},
        {"edge_id": "E_P01_N02", "from": "P01", "to": "N02", "when": None,
         "kind": "CONTINUE", "label": "continue"},
    ],
    "assumptions": [], "warnings": [],
}}}


def head(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def _repair_workflow(bridge, workflow_id: str) -> None:
    draft = bridge.blank_workflow(workflow_id)
    draft["nodes"][0]["id"] = "N09"
    draft["start_node"] = "N01"
    draft["nodes"] = [
        {"id": "N01", "type": "IMPLEMENT", "depends_on": [], "run_if": "ALWAYS",
         "role": "CODE_IMPLEMENTER", "capability": "CODE_IMPLEMENTER",
         "model": "gpt-5.6-sol", "effort": "medium", "routing": "FIRST_MATCH",
         "instructions": "Implement the supplied goal in the isolated worktree.",
         "acceptance": ["The requested behaviour is implemented."],
         "on_pass": None, "on_fail": None,
         "edges": [{"edge_id": "E_N01_CONTINUE", "to": "N02", "when": None,
                    "kind": "CONTINUE", "label": "continue"}]},
        {"id": "N02", "type": "REVIEW", "depends_on": ["N01"], "run_if": "ON_TRANSITION",
         "role": "INDEPENDENT_REVIEWER", "model": "gpt-5.6-sol", "effort": "high",
         "routing": "FIRST_MATCH",
         "instructions": "Review in a fresh, read-only context. Set `verdict`.",
         "acceptance": ["The review ran in a fresh read-only context."],
         "on_pass": None, "on_fail": None,
         "edges": [
             {"edge_id": "E_N02_PASS", "to": "N09", "when": {"verdict": "PASS"},
              "kind": "CONTINUE", "label": "PASS"},
             {"edge_id": "E_N02_REPAIR", "to": "N02R", "when": {"verdict": "REPAIR"},
              "kind": "REPAIR", "label": "REPAIR — new branch"},
             {"edge_id": "E_N02_BLOCKED", "to": "N09", "when": {"verdict": "BLOCKED"},
              "kind": "FALLBACK", "label": "BLOCKED — human"},
         ]},
        {"id": "N02R", "type": "REPAIR", "depends_on": [], "run_if": "ON_TRANSITION",
         "role": "CODE_IMPLEMENTER", "capability": "CODE_IMPLEMENTER",
         "model": "gpt-5.6-sol", "effort": "medium", "routing": "FIRST_MATCH",
         "instructions": "Repair template. Mints an explicit lineage child.",
         "acceptance": ["Every point of the inherited brief is addressed."],
         "on_pass": None, "on_fail": None,
         "edges": [{"edge_id": "E_N02R_CONTINUE", "to": "N09", "when": None,
                    "kind": "CONTINUE", "label": "repaired"}]},
        {"id": "N09", "type": "HUMAN_GATE", "depends_on": [], "run_if": "ON_TRANSITION",
         "role": None, "model": None, "effort": None,
         "instructions": "Human acceptance.",
         "acceptance": ["A human verdict is recorded against the candidate."],
         "on_pass": None, "on_fail": None},
    ]
    bridge.create_workflow(workflow_id, draft)


def _bridge_for(scenario: str, *, stats_root: Path, evidence_root: Path
               ) -> tuple[puv.EvidenceBridge, str]:
    workflows = OUT_ROOT / scenario / "WORKFLOWS"
    shutil.rmtree(workflows, ignore_errors=True)
    workflows.mkdir(parents=True)
    workflow_id = f"PRODUCTION_USE_EVIDENCE_WALKTHROUGH_{scenario}"
    bridge = puv.EvidenceBridge(workflows_root=workflows, stats_root=stats_root,
                                evidence_root=evidence_root)
    _repair_workflow(bridge, workflow_id)
    return bridge, workflow_id


def _run_and_wait(bridge: puv.EvidenceBridge, workflow_id: str, root: Path, *,
                  node_script: dict[str, Any], goal: str, timeout: float = 60.0) -> str:
    repo, worktree = pse.workspace(root)
    adapter = aaw_llm_test_adapter.scripted_adapter(node_script)
    handle = bridge.start_run(workflow_id, goal=goal, repo=repo, worktree=worktree,
                              adapter=adapter)
    run_id = handle["run_id"]
    since, deadline = 0, time.monotonic() + timeout
    while time.monotonic() < deadline:
        batch = bridge.events(run_id, since=since)
        since = batch["last_sequence"]
        if batch["lifecycle"] == aaw_bridge.RUN_SETTLED:
            break
        time.sleep(0.05)
    else:
        raise AssertionError(f"run {run_id} did not settle within {timeout}s")
    return run_id


def scenario_a(*, stats_root: Path, evidence_root: Path) -> dict[str, Any]:
    head("A — plan -> ACCEPT -> Save -> Run -> complete -> operator feedback")
    bridge, workflow_id = _bridge_for("A", stats_root=stats_root, evidence_root=evidence_root)
    with aaw_planner.planner_adapter_scope(scripted.scripted_planner(SLICE)):
        frame = bridge.plan_from_node(workflow_id, "N01",
                                      instruction="add a bounded research pass")
    assert frame["status"] == pp.PROPOSAL_READY, frame["diagnostics"]
    session_id = frame["request_id"]
    print(f"  session_id   {session_id}")
    print(f"  proposal     {frame['proposal']['proposal_id']}  "
         f"({frame['summary']['node_count']} nodes)")

    accept = bridge.accept_proposal(frame["proposal"]["proposal_id"], persist=True)
    print(f"  accept       {accept['status']}  persisted={accept['persisted']}")

    run_id = _run_and_wait(bridge, workflow_id, OUT_ROOT / "A" / "ws",
                           node_script={"nodes": {"*": {"outcome": "PASS", "verdict": "PASS"}}},
                           goal="Production use evidence walkthrough: scenario A")
    print(f"  run          {run_id}")

    feedback = bridge.record_operator_feedback(session_id, "USEFUL", reuse_intent="YES",
                                               comment="did exactly what I asked")
    print(f"  feedback     {feedback['usefulness']} / {feedback['reuse_intent']}")

    session = bridge.session_summary(session_id)
    assert session["planner"]["operator_decision"] == "ACCEPT"
    assert session["execution"]["run_id"] == run_id
    assert session["execution"]["run_status"] in {"WAITING_FOR_HUMAN", "COMPLETED"}
    assert session["operator_feedback"]["usefulness"] == "USEFUL"
    print(f"  session summary is coherent: {json.dumps(session, indent=2, default=str)}")
    return session


def scenario_b(*, stats_root: Path, evidence_root: Path) -> dict[str, Any]:
    head("B — plan -> REJECT")
    bridge, workflow_id = _bridge_for("B", stats_root=stats_root, evidence_root=evidence_root)
    with aaw_planner.planner_adapter_scope(scripted.scripted_planner(SLICE)):
        frame = bridge.plan_from_node(workflow_id, "N01", instruction="add a research pass")
    assert frame["status"] == pp.PROPOSAL_READY, frame["diagnostics"]
    session_id = frame["request_id"]

    reject = bridge.reject_proposal(frame["proposal"]["proposal_id"], reason="not needed")
    print(f"  reject       {reject['status']}")

    session = bridge.session_summary(session_id)
    assert session["planner"]["operator_decision"] == "REJECT"
    assert session["execution"]["run_id"] is None, "a rejected proposal must show no run"
    assert session["execution"]["run_status"] is None
    assert session["execution"]["final_outcome"] is None, \
        "no fabricated execution result may appear for a rejected proposal"
    print(f"  session summary shows no execution: {json.dumps(session, indent=2, default=str)}")
    return session


def scenario_c(*, stats_root: Path, evidence_root: Path) -> dict[str, Any]:
    head("C — ACCEPT -> Run -> REVIEW->REPAIR -> repair descendant -> final PASS")
    bridge, workflow_id = _bridge_for("C", stats_root=stats_root, evidence_root=evidence_root)
    with aaw_planner.planner_adapter_scope(scripted.scripted_planner(SLICE)):
        frame = bridge.plan_from_node(workflow_id, "N01", instruction="add a research pass")
    assert frame["status"] == pp.PROPOSAL_READY, frame["diagnostics"]
    session_id = frame["request_id"]
    bridge.accept_proposal(frame["proposal"]["proposal_id"], persist=True)

    node_script = {"nodes": {
        "N01": {"outcome": "PASS", "verdict": "PASS"},
        "P01": {"outcome": "PASS", "verdict": "PASS"},
        "N02": {"outcome": "FAIL", "verdict": "REPAIR", "next_brief": "fix the gap"},
        "N02R": {"outcome": "PASS", "verdict": "PASS"},
    }}
    run_id = _run_and_wait(bridge, workflow_id, OUT_ROOT / "C" / "ws", node_script=node_script,
                           goal="Production use evidence walkthrough: scenario C")
    print(f"  run          {run_id}")

    session = bridge.session_summary(session_id)
    assert session["execution"]["repair_cycles"] == 1

    state_path = stats_root / run_id / "WORKFLOW" / "workflow_state.json"
    completed = json.loads(state_path.read_text(encoding="utf-8"))["completed_nodes"]
    original = next(row for row in completed if row["node_id"] == "N02")
    repaired = next(row for row in completed if row.get("lineage"))
    assert original["verdict"] == "REPAIR", "the original failing attempt must remain visible"
    assert repaired["verdict"] == "PASS", "the repair descendant must remain visible"
    print(f"  original attempt   N02 verdict={original['verdict']}")
    print(f"  repair descendant  {repaired['node_id']} verdict={repaired['verdict']}  "
         f"lineage.origin_node_id={repaired['lineage'].get('origin_node_id')}")
    print(f"  session summary: {json.dumps(session, indent=2, default=str)}")
    return session


def main() -> int:
    shutil.rmtree(OUT_ROOT, ignore_errors=True)
    OUT_ROOT.mkdir(parents=True)
    stats_root = OUT_ROOT / "03_STATS"
    evidence_root = OUT_ROOT / "PLANNER_EVIDENCE"
    # The runner writes every run's evidence under its own module-level
    # `STATS_ROOT`, not the constructing bridge's `stats_root` property (the
    # bridge only uses that for its own handle bookkeeping) -- the same
    # reason `test_production_use_evidence.py`'s fixture and
    # `planner_production_use_gate_run_v0_1.py` both repoint this directly.
    runner.STATS_ROOT = stats_root

    scenario_a(stats_root=stats_root, evidence_root=evidence_root)
    scenario_b(stats_root=stats_root, evidence_root=evidence_root)
    scenario_c(stats_root=stats_root, evidence_root=evidence_root)

    head("aggregate product-use report across all three real sessions")
    report = puv.aggregate_report(evidence_root=evidence_root, stats_root=stats_root)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    assert report["session_count"] == 3
    assert report["planner_decided_sessions"] == 3
    assert report["planner_accept_rate"] == round(2 / 3, 4)
    assert report["planner_reject_rate"] == round(1 / 3, 4)
    assert report["runs"] == 2  # A and C ran; B never did

    out_path = HERE / "EVIDENCE" / "production_use_evidence_walkthrough_v0_1.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "sessions": puv.all_sessions(evidence_root=evidence_root, stats_root=stats_root),
        "report": report,
    }, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\nwrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
