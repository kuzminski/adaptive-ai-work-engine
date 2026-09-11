#!/usr/bin/env python3
"""Reproducible run evidence for AAW MULTIROUTING RUNTIME CONTRACT V0.1.

Drives the real `workflow_runner.execute` over the committed
`WORKFLOWS/MULTIROUTING_SLICE_V1.json` four times — PASS + fan-out, REPAIR,
BLOCKED and NO_ROUTE — and prints what the deterministic gate decided, with
the artifact paths behind every claim.

    python multirouting_evidence.py [--out MULTIROUTING_EVIDENCE]

Provider calls are scripted at the documented adapter boundary
(`execute_llm_node`); everything else is the real runner: real git worktrees,
real workspace guards, real MACHINE_GATE subprocesses, real artifacts.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
from pathlib import Path

import routing_contract as rc
import workflow_runner as runner
from test_multirouting_slice import (
    CARRY, REVIEW_BRIEF, SLICE, _install_fake_llm, _workflow_copy, _workspace,
)

HIGH_FINDING = {
    "severity": "HIGH", "file": "auth/session/refresh.py", "location": "L42",
    "description": "mobile deep-link exchange re-issues a refresh token without re-binding device_id",
    "required_fix": "bind rotation to device_id and reject a family mismatch",
}

VARIANTS = {
    "pass_fanout": {
        "title": "PASS -> N04 -> ALL_MATCHES fan-out (N05 + N06)",
        "scripts": {"N03": {"outcome": "PASS", "verdict": "PASS", "carry_forward": CARRY}},
        "gate": {"exit_code": 0},
    },
    "repair_branch": {
        "title": "REPAIR -> explicit branch lineage N03A carrying next_brief",
        "scripts": {
            "N03": {"outcome": "FAIL", "verdict": "REPAIR", "next_brief": REVIEW_BRIEF,
                    "carry_forward": CARRY, "artifacts": ["N03_review.json"],
                    "findings": [HIGH_FINDING]},
            "N03A": {"outcome": "PASS", "summary": "device binding restored; replay window closed"},
        },
        "gate": {"exit_code": 0},
    },
    "blocked_human": {
        "title": "BLOCKED -> HUMAN_GATE, no downstream work started",
        "scripts": {"N03": {"outcome": "BLOCKED", "verdict": "BLOCKED",
                            "summary": "cannot proceed without a product decision on the legacy cookie path"}},
        "gate": {"exit_code": 0},
    },
    "no_route": {
        "title": "MACHINE_GATE timeout -> no edge matches -> deterministic NO_ROUTE",
        "scripts": {"N03": {"outcome": "PASS", "verdict": "PASS"}},
        "gate": {"sleep": 30, "timeout_seconds": 1},
    },
}


class _Patch:
    """Minimal monkeypatch stand-in so the harness runs outside pytest."""

    def __init__(self) -> None:
        self._undo: list[tuple[object, str, object]] = []

    def setattr(self, obj: object, name: str, value: object) -> None:
        self._undo.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def undo(self) -> None:
        for obj, name, old in reversed(self._undo):
            setattr(obj, name, old)
        self._undo.clear()


def _force_rmtree(path: Path) -> None:
    """Git keeps its loose objects read-only; plain rmtree fails on Windows."""
    def _clear(func, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        func(target)

    try:
        shutil.rmtree(path, onexc=_clear)
    except TypeError:  # Python < 3.12
        shutil.rmtree(path, onerror=lambda f, t, e: _clear(f, t, e))


def run_variant(name: str, spec: dict, out_root: Path) -> dict:
    root = out_root / name
    if root.exists():
        _force_rmtree(root)
    repo, worktree = _workspace(root)
    workflow_path = _workflow_copy(SLICE, root / "workflow.json", gate_node="N04", **spec["gate"])
    patch = _Patch()
    captured: dict = {}
    try:
        patch.setattr(runner, "STATS_ROOT", root / "03_STATS")
        _install_fake_llm(patch, spec["scripts"], captured)
        state = runner.execute(workflow_path, "harden refresh-token rotation", repo, worktree,
                               preprocess_policy="OFF")
    finally:
        patch.undo()
    journal = rc.RoutingJournal(Path(state["routing"]["journal_path"]), run_id=state["AAW_RUN_ID"])
    return {"state": state, "journal": journal, "captured": captured, "root": root}


def report(name: str, spec: dict, run: dict) -> None:
    state, journal = run["state"], run["journal"]
    line = "=" * 78
    print(f"\n{line}\n{name.upper()}  ::  {spec['title']}\n{line}")
    print(f"AAW_RUN_ID : {state['AAW_RUN_ID']}")
    print(f"status     : {state['status']}   final_outcome: {state['final_outcome']}")
    if state.get("stop_reason"):
        print(f"stop_reason: {state['stop_reason']}")
    print(f"frontier   : {state['frontier']}")

    print("\nEXECUTED NODES")
    for row in state["completed_nodes"]:
        lineage = row.get("lineage") or {}
        origin = f"  <- lineage of {lineage['origin_node_id']} via {lineage['selected_edge_id']}" if lineage else ""
        print(f"  {row['node_id']:<5} {row['node_type']:<13} outcome={row['outcome']:<8} "
              f"verdict={str(row.get('verdict')):<7}{origin}")

    print("\nGATE DECISIONS")
    for row in state["routing"]["decisions"]:
        print(f"  {row['node_id']:<5} mode={row['routing_mode']:<12} verdict={str(row['verdict']):<7} "
              f"no_route={row['no_route']}")
        for edge in row["selected"]:
            print(f"        SELECTED  {edge}")
        for held in row["held"]:
            print(f"        HELD      {held['edge_id']:<20} -> {held['to']:<5} ({held['hold_reason']})")
        print(f"        decision_hash={row['decision_hash'][:16]}..  artifact={Path(row['artifact']).name}")

    if state["routing"]["dedup"]:
        print("\nFRONTIER DEDUP (node entered at most once; not a join)")
        for row in state["routing"]["dedup"]:
            print(f"  {row['edge_id']} -> {row['target']} : {row['reason']}")

    if state["routing"]["lineage"]:
        print("\nBRANCH LINEAGE")
        for branch_id, lineage in state["routing"]["lineage"].items():
            print(f"  {branch_id}: template={lineage['template_id']} origin={lineage['origin_node_id']} "
                  f"index={lineage['branch_index']}")
            print(f"    inherited_brief: {lineage['inherited_brief']}")
            print(f"    carry_forward  : {lineage['carry_forward']}")

    print("\nROUTING JOURNAL")
    for row in journal.read():
        payload = row["payload"]
        detail = ""
        if row["event_type"] in {rc.EDGE_SELECTED, rc.EDGE_HELD}:
            detail = f" {payload.get('edge_id')} -> {payload.get('to')}"
            if payload.get("hold_reason"):
                detail += f" ({payload['hold_reason']})"
        elif row["event_type"] == rc.NODE_COMPLETED:
            detail = f" outcome={payload.get('outcome')} verdict={payload.get('verdict')}"
        elif row["event_type"] == rc.BRANCH_CREATED:
            detail = f" from {payload.get('origin_node_id')} template={payload.get('template_id')}"
        elif row["event_type"] == rc.ROUTE_UNRESOLVED:
            detail = f" {payload.get('status')} considered={payload.get('candidates_considered')}"
        print(f"  {row['sequence']:>3}  {row['event_type']:<24} {str(row['node_id'] or ''):<6}{detail}")

    print(f"\nARTIFACT ROOT: {run['root'] / '03_STATS'}")
    print(f"JOURNAL      : {journal.path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="MULTIROUTING_EVIDENCE", help="evidence output directory")
    parser.add_argument("--variant", choices=sorted(VARIANTS), action="append",
                        help="run only these variants (repeatable)")
    args = parser.parse_args(argv)

    out_root = Path(args.out).resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    names = args.variant or list(VARIANTS)
    summary = []
    for name in names:
        spec = VARIANTS[name]
        run = run_variant(name, spec, out_root)
        report(name, spec, run)
        state = run["state"]
        summary.append({
            "variant": name, "status": state["status"],
            "executed": [row["node_id"] for row in state["completed_nodes"]],
            "decisions": [{"node_id": row["node_id"], "selected": row["selected"],
                           "held": row["held"], "decision_hash": row["decision_hash"]}
                          for row in state["routing"]["decisions"]],
            "lineage": state["routing"]["lineage"],
            "run_id": state["AAW_RUN_ID"],
        })
    (out_root / "SUMMARY.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nwrote {out_root / 'SUMMARY.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
