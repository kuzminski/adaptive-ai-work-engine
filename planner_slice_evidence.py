#!/usr/bin/env python3
"""Runnable evidence for AAW PLANNER PROPOSAL PIPELINE V0.1.

Drives the real bridge over the committed `WORKFLOWS/PLANNER_SLICE_V1.json`,
with semantic hashes and file bytes behind every claim:

  1 PLAN    - a proposal is generated; the workflow file and its semantic hash
              are byte-identical before and after
  2 INVALID - refused planner output, with the rule that refused it; nothing moves
  3 ACCEPT  - slice A applied atomically: one hash in, one hash out
  4 BRANCH  - slice B: fan-out, MERGE ALL_REQUIRED with a closed incoming set
  5 STALE   - slice C: the graph moves under an open proposal; Accept is refused
              and the workflow is bit-for-bit what it was
  6 REJECT  - the proposal is discarded and the workflow is unchanged

Usage:

    python planner_slice_evidence.py                 # every section, write evidence
    python planner_slice_evidence.py --serve         # serve the canvas for a walkthrough
    python planner_slice_evidence.py --only stale

The planner provider is scripted at the documented seam
(`aaw_planner.provider_planner`). Everything else is real: the planning
package and its hash, normalization, the deterministic validator, the real
`workflow_schema.validate_workflow` behind it, proposal identity, staleness,
Accept/Reject, and the validated atomic save path.
"""

from __future__ import annotations

import argparse
import copy
import json
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import aaw_bridge
import aaw_llm_test_adapter
import aaw_planner
import aaw_planner_test_adapter as planner_adapter
import planner_proposal as pp
import routing_contract as rc
import workflow_runner as runner

HERE = Path(__file__).resolve().parent
REAL_WORKFLOWS = HERE / "WORKFLOWS"
SLICE_ID = "PLANNER_SLICE_V1"


# ── the three slices, as planner scripts ────────────────────────────────

SLICE_A = {
    "intent": "Insert a bounded research pass and an independent review before the existing review.",
    "detach_edges": ["E_N01_CONTINUE"],
    "nodes": [
        {"id": "P01", "type": "IMPLEMENT", "role": "RESEARCH_SYNTHESIZER",
         "capability": "RESEARCH_SYNTHESIZER", "effort": "medium",
         "instructions": "Research the affected surface and synthesise a bounded note. "
                         "Do not implement anything; the note is the deliverable.",
         "acceptance": ["A bounded synthesis exists.", "No scope was widened."],
         "depends_on": ["N01"], "run_if": "ON_TRANSITION"},
        {"id": "P02", "type": "REVIEW", "role": "INDEPENDENT_REVIEWER", "effort": "high",
         "instructions": "Review the research note in a fresh read-only context. Set `verdict`.",
         "acceptance": ["The review ran read-only."],
         "depends_on": ["P01"], "run_if": "ON_TRANSITION"},
    ],
    "edges": [
        {"edge_id": "E_N01_P01", "from": "N01", "to": "P01", "when": None,
         "kind": "CONTINUE", "label": "research"},
        {"edge_id": "E_P01_P02", "from": "P01", "to": "P02", "when": None,
         "kind": "CONTINUE", "label": "review"},
        {"edge_id": "E_P02_N02", "from": "P02", "to": "N02", "when": None,
         "kind": "CONTINUE", "label": "continue"},
    ],
    "assumptions": ["The research belongs on the main path, not on a branch."],
    "warnings": ["This lengthens the path by two LLM nodes."],
}

SLICE_B = {
    "intent": "Fan out into two bounded branches, rejoin through a MERGE, then review.",
    "detach_edges": ["E_N01_CONTINUE"],
    "anchor_routing": "ALL_MATCHES",
    "nodes": [
        {"id": "B01", "type": "IMPLEMENT", "role": "CODE_IMPLEMENTER",
         "capability": "CODE_IMPLEMENTER", "effort": "medium",
         "instructions": "Branch: hardening. Honour every carry_forward constraint.",
         "acceptance": ["Hardening is applied without widening the goal."],
         "depends_on": ["N01"], "run_if": "ON_TRANSITION"},
        {"id": "B02", "type": "IMPLEMENT", "role": "CODE_IMPLEMENTER",
         "capability": "CODE_IMPLEMENTER", "effort": "medium",
         "instructions": "Branch: migration. Honour every carry_forward constraint.",
         "acceptance": ["The migration is forward-only and has a dry-run mode."],
         "depends_on": ["N01"], "run_if": "ON_TRANSITION"},
        {"id": "M01", "type": "MERGE", "merge_policy": "ALL_REQUIRED",
         "expected_incoming": ["E_B01_M01", "E_B02_M01"],
         "instructions": "Rejoin both branches before the review.",
         "acceptance": ["Both branches are represented, source-attributed."],
         "depends_on": [], "run_if": "ON_TRANSITION"},
        {"id": "R01", "type": "REVIEW", "role": "INDEPENDENT_REVIEWER", "effort": "high",
         "instructions": "Review the merged candidate read-only. Set `verdict`.",
         "acceptance": ["The review ran read-only over the merged package."],
         "depends_on": ["M01"], "run_if": "ON_TRANSITION"},
    ],
    "edges": [
        {"edge_id": "E_N01_B01", "from": "N01", "to": "B01", "when": None,
         "kind": "CONTINUE", "label": "hardening"},
        {"edge_id": "E_N01_B02", "from": "N01", "to": "B02", "when": None,
         "kind": "CONTINUE", "label": "migration"},
        {"edge_id": "E_B01_M01", "from": "B01", "to": "M01", "when": None,
         "kind": "CONTINUE", "label": "rejoin"},
        {"edge_id": "E_B02_M01", "from": "B02", "to": "M01", "when": None,
         "kind": "CONTINUE", "label": "rejoin"},
        {"edge_id": "E_M01_R01", "from": "M01", "to": "R01", "when": None,
         "kind": "CONTINUE", "label": "review"},
        {"edge_id": "E_R01_N02", "from": "R01", "to": "N02", "when": None,
         "kind": "CONTINUE", "label": "continue"},
    ],
    "assumptions": ["The two branches are independent and may run concurrently."],
    "warnings": [],
}

# Deliberately wrong: two node ids the same. Kept here because the pipeline's
# job is refusing planner output, and evidence that only ever shows the happy
# path is evidence of nothing.
SLICE_INVALID = copy.deepcopy(SLICE_A)
SLICE_INVALID["intent"] = "Malformed on purpose: two proposed nodes share an id."
SLICE_INVALID["nodes"][1]["id"] = "P01"

SERVE_SCRIPT = {
    "schema_version": planner_adapter.SCRIPT_SCHEMA_VERSION,
    "delay_seconds": 0.0,
    "anchors": {"N01": SLICE_A, "P01": SLICE_B, "*": SLICE_A},
}


# ───────────────────────────── plumbing ─────────────────────────────

def head(title: str) -> None:
    print(f"\n\n{'=' * 78}\n{title}\n{'=' * 78}")


def force_rmtree(path: Path) -> None:
    def unlock(func, target, _exc):
        try:
            Path(target).chmod(stat.S_IWRITE)
            func(target)
        except OSError:
            pass
    if path.exists():
        shutil.rmtree(path, onerror=unlock)


def git(argv: list[str], cwd: Path) -> None:
    subprocess.run(argv, cwd=cwd, check=True, stdout=subprocess.PIPE,
                   stderr=subprocess.PIPE, text=True)


def workspace(root: Path) -> tuple[Path, Path]:
    force_rmtree(root)
    repo, tree = root / "repo", root / "worktree"
    repo.mkdir(parents=True)
    git(["git", "init", "-b", "main"], repo)
    git(["git", "config", "user.email", "aaw@example.invalid"], repo)
    git(["git", "config", "user.name", "AAW Planner Slice"], repo)
    (repo / "README.md").write_text("baseline\n", encoding="utf-8")
    git(["git", "add", "."], repo)
    git(["git", "commit", "-m", "baseline"], repo)
    git(["git", "worktree", "add", "-b", "aaw/planner-slice", str(tree)], repo)
    return repo, tree


def sandbox_workflows(root: Path) -> Path:
    """A byte-identical copy of WORKFLOWS/, so a trial cannot touch the real ones."""
    target = root / "WORKFLOWS"
    force_rmtree(target)
    target.mkdir(parents=True)
    for path in sorted(REAL_WORKFLOWS.glob("*.json")):
        shutil.copy2(path, target / path.name)
    return target


def fresh(out: Path, name: str) -> tuple[aaw_bridge.AawBridge, Path]:
    root = out / name
    workflows = sandbox_workflows(root)
    bridge = aaw_bridge.AawBridge(workflows_root=workflows, stats_root=root / "03_STATS")
    return bridge, workflows / f"{SLICE_ID}.json"


def state(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    import hashlib
    return {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
            "semantic_hash": rc.semantic_hash(json.loads(raw.decode("utf-8")))}


def plan(bridge: aaw_bridge.AawBridge, entry: dict[str, Any], anchor: str = "N01", *,
         instruction: str = "") -> dict[str, Any]:
    script = {"anchors": {anchor: entry}}
    with aaw_planner.planner_adapter_scope(planner_adapter.scripted_planner(script)):
        return bridge.plan_from_node(SLICE_ID, anchor, instruction=instruction)


def show(label: str, before: dict[str, Any], after: dict[str, Any]) -> bool:
    same = before == after
    print(f"  {label}")
    print(f"    before  sha256 {before['sha256'][:16]}  semantic {before['semantic_hash'][:16]}")
    print(f"    after   sha256 {after['sha256'][:16]}  semantic {after['semantic_hash'][:16]}")
    print(f"    -> {'IDENTICAL' if same else 'CHANGED'}")
    return same


# ───────────────────────────── sections ─────────────────────────────

def section_plan(out: Path) -> dict[str, Any]:
    head("PLAN - generating a proposal changes nothing")
    bridge, path = fresh(out, "plan")
    before = state(path)
    frame = plan(bridge, SLICE_A, instruction="add a bounded research pass before review")
    after = state(path)
    print(f"  status            {frame['status']}")
    print(f"  proposal_id       {frame['proposal']['proposal_id']}")
    print(f"  proposal_hash     {frame['summary']['proposal_hash']}")
    print(f"  input_hash        {frame['input_hash']}")
    print(f"  planner           {frame['planner']['harness']}/{frame['planner']['model']} "
          f"({frame['planner']['telemetry_status']})")
    print(f"  proposes          {frame['added_node_ids']} + {frame['added_edge_ids']}")
    print(f"  detaches          {frame['detached_edge_ids']}")
    print(f"  package bytes     {frame['package']['package_bytes']} "
          f"(limit {pp.DEFAULT_LIMITS.max_package_bytes})")
    identical = show("workflow file, before and after planning", before, after)
    print(f"  bridge measured   base {frame['base_semantic_hash'][:16]} -> "
          f"{frame['base_semantic_hash_after'][:16]}")
    preview = frame["preview"]["semantic_hash"]
    print(f"  preview hash      {preview[:16]}  (a projection, never written)")
    assert identical and preview != after["semantic_hash"]
    return {"status": frame["status"], "proposal_id": frame["proposal"]["proposal_id"],
            "proposal_hash": frame["summary"]["proposal_hash"],
            "input_hash": frame["input_hash"], "before": before, "after": after,
            "preview_semantic_hash": preview, "unchanged": identical}


def section_invalid(out: Path) -> dict[str, Any]:
    head("INVALID - refused planner output, with the rule that refused it")
    bridge, path = fresh(out, "invalid")
    before = state(path)
    frame = plan(bridge, SLICE_INVALID)
    after = state(path)
    print(f"  status            {frame['status']}")
    for row in frame["diagnostics"]:
        print(f"  refusal           {row['code']}  {row.get('node_id') or row.get('edge_id') or '-'}")
        print(f"                    {row['message']}")
    identical = show("workflow file, before and after a refused proposal", before, after)
    assert frame["status"] == pp.PROPOSAL_INVALID and identical
    return {"status": frame["status"], "diagnostics": frame["diagnostics"],
            "before": before, "after": after, "unchanged": identical}


def section_accept(out: Path) -> dict[str, Any]:
    head("ACCEPT - slice A applied atomically through the validated save path")
    bridge, path = fresh(out, "accept")
    before = state(path)
    frame = plan(bridge, SLICE_A)
    report = bridge.accept_proposal(frame["proposal"]["proposal_id"], persist=True)
    after = state(path)
    print(f"  status            {report['status']}")
    print(f"  semantic          {report['previous_semantic_hash'][:16]} -> "
          f"{report['semantic_hash'][:16]}")
    print(f"  added nodes       {report['added_node_ids']}")
    print(f"  added edges       {report['added_edge_ids']}")
    print(f"  detached edges    {report['detached_edge_ids']}")
    print(f"  persisted         {report['persisted']} (through save_workflow)")
    print(f"  undo label        {report['undo_label']}")
    show("workflow file, before and after Accept", before, after)
    projection = rc.workflow_projection(json.loads(path.read_text(encoding="utf-8")))
    print("  resulting route:")
    for edge in projection["edges"]:
        print(f"    {edge['from']:>4} --{edge['edge_id']:<14}--> {edge['to']}")
    assert after["semantic_hash"] == report["semantic_hash"]
    assert bridge.load_workflow(SLICE_ID)["valid"] is True

    print("\n  a second Accept of the same proposal:")
    try:
        bridge.accept_proposal(frame["proposal"]["proposal_id"], persist=True)
    except aaw_bridge.BridgeError as exc:
        print(f"    REFUSED {exc.code} - {exc}")
    duplicate = state(path)
    assert duplicate == after, "a duplicate Accept moved the workflow"
    print("    workflow after the refused duplicate: IDENTICAL")
    return {"status": report["status"], "before": before, "after": after,
            "added_node_ids": report["added_node_ids"],
            "added_edge_ids": report["added_edge_ids"],
            "detached_edge_ids": report["detached_edge_ids"],
            "duplicate_accept_refused": True}


def section_branch(out: Path) -> dict[str, Any]:
    head("BRANCH - slice B: fan-out into a MERGE with a closed incoming set")
    bridge, path = fresh(out, "branch")
    before = state(path)
    frame = plan(bridge, SLICE_B, instruction="fan out into two branches, then rejoin")
    print(f"  status            {frame['status']}")
    if frame["status"] != pp.PROPOSAL_READY:
        for row in frame["diagnostics"]:
            print(f"  refusal           {row['code']} {row['message']}")
        raise AssertionError("slice B did not validate")
    report = bridge.accept_proposal(frame["proposal"]["proposal_id"], persist=True)
    after = state(path)
    definition = json.loads(path.read_text(encoding="utf-8"))
    anchor = next(n for n in definition["nodes"] if n["id"] == "N01")
    merge = next(n for n in definition["nodes"] if n["id"] == "M01")
    arriving = sorted({edge["edge_id"] for node in definition["nodes"]
                       for edge in node.get("edges", []) if edge["to"] == "M01"})
    print(f"  anchor routing    {anchor['routing']} -> {[e['to'] for e in anchor['edges']]}")
    print(f"  merge policy      {merge['merge_policy']}")
    print(f"  expected_incoming {merge['expected_incoming']}")
    print(f"  graph incoming    {arriving}")
    print(f"  closed set holds  {sorted(merge['expected_incoming']) == arriving}")
    print(f"  merge binds model {merge['model']!r}")
    show("workflow file, before and after Accept", before, after)
    assert sorted(merge["expected_incoming"]) == arriving

    print("\n  splicing a node into that MERGE's incoming set afterwards:")
    follow = {"intent": "insert a check before the rejoin",
              "detach_edges": ["E_B01_M01"],
              "nodes": [{"id": "C01", "type": "REVIEW", "role": "INDEPENDENT_REVIEWER",
                         "effort": "high", "instructions": "Check before rejoin.",
                         "acceptance": ["Checked."], "depends_on": ["B01"],
                         "run_if": "ON_TRANSITION"}],
              "edges": [{"edge_id": "E_B01_C01", "from": "B01", "to": "C01", "when": None,
                         "kind": "CONTINUE", "label": "check"},
                        {"edge_id": "E_C01_M01", "from": "C01", "to": "M01", "when": None,
                         "kind": "CONTINUE", "label": "rejoin"}]}
    refused = plan(bridge, follow, anchor="B01")
    print(f"    status {refused['status']}")
    for row in refused["diagnostics"]:
        print(f"    {row['code']}: {row['message']}")
    print("    (a known V0.1 boundary: a MERGE's incoming set is existing structure)")
    assert state(path) == after
    return {"status": report["status"], "before": before, "after": after,
            "expected_incoming": merge["expected_incoming"], "graph_incoming": arriving,
            "merge_splice_refused": refused["status"]}


def section_stale(out: Path) -> dict[str, Any]:
    head("STALE - slice C: the graph moves under an open proposal")
    bridge, path = fresh(out, "stale")
    frame = plan(bridge, SLICE_A)
    generated_against = frame["base_semantic_hash"]
    print(f"  proposal generated against {generated_against[:16]}")

    definition = bridge.load_workflow(SLICE_ID)["definition"]
    next(n for n in definition["nodes"] if n["id"] == "N02")["effort"] = "max"
    bridge.save_workflow(SLICE_ID, definition, base_semantic_hash=generated_against)
    moved = state(path)
    print(f"  operator edits N02.effort -> max; workflow now {moved['semantic_hash'][:16]}")

    refusal = None
    try:
        bridge.accept_proposal(frame["proposal"]["proposal_id"], persist=True)
    except aaw_bridge.BridgeError as exc:
        refusal = exc
        print(f"  Accept            REFUSED {exc.code}")
        print(f"                    {exc}")
    after = state(path)
    identical = show("workflow file, before and after the refused Accept", moved, after)
    print(f"  proposal status   {bridge.proposal(frame['proposal']['proposal_id'])['status']}")
    assert refusal is not None and refusal.code == aaw_bridge.PROPOSAL_STALE and identical
    return {"code": refusal.code, "message": str(refusal),
            "generated_against": generated_against, "workflow_now": moved,
            "after_refused_accept": after, "unchanged": identical}


def section_reject(out: Path) -> dict[str, Any]:
    head("REJECT - the proposal is discarded and nothing else is")
    bridge, path = fresh(out, "reject")
    before = state(path)
    frame = plan(bridge, SLICE_A)
    report = bridge.reject_proposal(frame["proposal"]["proposal_id"], reason="not what I meant")
    after = state(path)
    print(f"  status            {report['status']}")
    print(f"  workflow_changed  {report['workflow_changed']}")
    print(f"  semantic_hash     {report['semantic_hash'][:16]}")
    print(f"  proposals held    {len(bridge.list_proposals(SLICE_ID)['proposals'])}")
    identical = show("workflow file, before and after Reject", before, after)
    print("\n  planner journal:")
    for row in bridge.planner_events()["events"]:
        print(f"    {row['sequence']:>2}  {row['event_type']:<20} "
              f"{row.get('proposal_id') or row.get('request_id')}")
    assert identical and report["workflow_changed"] is False
    return {"status": report["status"], "before": before, "after": after,
            "unchanged": identical,
            "events": [row["event_type"] for row in bridge.planner_events()["events"]]}


# ───────────────────────────── serve ─────────────────────────────

def section_serve(out: Path, host: str, port: int, node_delay: float | None) -> int:
    import aaw_bridge_server

    head("SERVE - the canvas against a live bridge with a scripted planner")
    root = out / "serve"
    repo, tree = workspace(root)
    stats = root / "03_STATS"
    runner.STATS_ROOT = stats
    workflows = sandbox_workflows(root)
    bridge = aaw_bridge.AawBridge(workflows_root=workflows, stats_root=stats)

    node_script = {"delay_seconds": float(node_delay or 0.0), "nodes": {"*": {
        "outcome": "PASS", "verdict": "PASS",
        "summary": "scripted PASS for a canvas node",
        "recommended_next_action": "the gate decides"}}}
    server = aaw_bridge_server.serve(
        host=host, port=port, bridge=bridge, workspace=(repo, tree),
        adapter=aaw_llm_test_adapter.scripted_adapter(node_script),
        planner_adapter=planner_adapter.scripted_planner(SERVE_SCRIPT), verbose=True)
    url = f"http://{host}:{server.server_address[1]}/"
    print(f"canvas    : {url}")
    print(f"contract  : {url}api/contract")
    print(f"workflow  : {url}api/workflow?workflow_id={SLICE_ID}")
    print(f"planner   : SCRIPTED (no paid planner calls) - N01 -> slice A, P01 -> slice B")
    print(f"workflows : {workflows}")
    print(f"repo      : {repo}")
    print(f"worktree  : {tree}")
    print("\nwalkthrough: open the canvas, pick PLANNER_SLICE_V1, select N01, "
          "'Plan from here',\nthen Accept (or Reject), then Undo, Redo, Save. "
          "Ctrl+C here to stop the server.")
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\nstopping")
        server.shutdown()
    return 0


# ───────────────────────────── main ─────────────────────────────

SECTIONS = {"plan": section_plan, "invalid": section_invalid, "accept": section_accept,
            "branch": section_branch, "stale": section_stale, "reject": section_reject}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="PLANNER_SLICE_EVIDENCE")
    ap.add_argument("--only", nargs="+", choices=sorted(SECTIONS))
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--node-delay", type=float,
                    help="seconds the scripted node adapter holds each node")
    args = ap.parse_args(argv)

    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    if args.serve:
        return section_serve(out, args.host, args.port, args.node_delay)

    summary: dict[str, Any] = {
        "bridge_version": aaw_bridge.BRIDGE_VERSION,
        "planner_contract": pp.PROPOSAL_CONTRACT,
        "routing_contract": rc.CONTRACT_VERSION,
        "limits": pp.ProposalLimits.from_env().as_dict(),
    }
    for name in (args.only or list(SECTIONS)):
        summary[name] = SECTIONS[name](out)
    (out / "SUMMARY.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\n\nwrote {out / 'SUMMARY.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
