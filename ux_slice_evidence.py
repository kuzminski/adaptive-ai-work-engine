#!/usr/bin/env python3
"""Runnable evidence for AAW UX RUNTIME BRIDGE V0.1.

Drives the real bridge over the committed `WORKFLOWS/MULTIROUTING_SLICE_V1.json`
and prints, with artifact paths behind every claim:

  1 BUILD  - the graph the canvas draws, projected from the real workflow file
  2 LAYOUT - positions persisted without touching workflow bytes or hash
  3 WRITE  - an invalid edit refused; a stale edit refused; a valid edit written
  4 RUN    - a real run: IMPLEMENT -> IMPLEMENT -> REVIEW -> minted repair branch
  5 RESUME - the same run replayed incrementally from a stored sequence
  6 CANCEL - a second run really stopped, with the terminated child's pid

Usage:

    python ux_slice_evidence.py                 # run every section, write evidence
    python ux_slice_evidence.py --serve          # prepare a workspace and serve the canvas
    python ux_slice_evidence.py --only run cancel

Provider calls are scripted at the documented adapter boundary
(`workflow_runner.execute_llm_node`). Routing, gates, lineage, the frontier,
MACHINE_GATE subprocesses, execution identity, the ledger, the routing journal
and cancellation are all real.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import aaw_bridge
import aaw_llm_test_adapter
import routing_contract as rc
import workflow_runner as runner

HERE = Path(__file__).resolve().parent
REAL_WORKFLOWS = HERE / "WORKFLOWS"
SLICE_ID = "MULTIROUTING_SLICE_V1"

REVIEW_BRIEF = (
    "Bind rotation to device_id on the mobile deep-link exchange path. "
    "Reject any refresh whose device_id does not match the family.\n\n"
    "Stay inside auth/session/. Extend the existing family-revocation test "
    "rather than adding a parallel suite."
)
CARRY = ["scope: auth/session/ only", "no merge to canonical",
         "legacy cookie fallback stays untouched"]
HIGH_FINDING = {
    "severity": "HIGH", "file": "auth/session/refresh.py", "location": "L42",
    "description": "the mobile deep-link exchange re-issues a refresh token without re-binding device_id",
    "required_fix": "bind rotation to device_id and reject a family mismatch",
}

# The required vertical-slice scenario. N03 returns REPAIR, so the gate mints
# N03A from the N03R template; the PASS and BLOCKED edges are held.
REPAIR_SCRIPT: dict[str, Any] = {
    "schema_version": aaw_llm_test_adapter.SCRIPT_SCHEMA_VERSION,
    "delay_seconds": 0.35,
    "nodes": {
        "N01": {"outcome": "PASS", "summary": "17 token entry points inventoried across 6 modules",
                "carry_forward": CARRY[:1]},
        "N02": {"outcome": "PASS", "summary": "refresh-token rotation implemented; six focused tests added",
                "carry_forward": CARRY},
        "N03": {"outcome": "FAIL", "verdict": "REPAIR", "next_brief": REVIEW_BRIEF,
                "carry_forward": CARRY, "artifacts": ["03_STATS/WORKFLOW/N03__REVIEW__result.json"],
                "summary": "rotation and reuse detection are correct for the web flow; the mobile "
                           "deep-link exchange leaves a replay window open",
                "findings": [HIGH_FINDING]},
        # keyed by template id, so it covers N03A, N03B, ...
        "N03R": {"outcome": "PASS", "summary": "device_id now travels with the token family; "
                                               "the mobile exchange rejects a mismatched refresh"},
    },
}

# A long first node, so Stop lands while a node is genuinely in flight.
CANCEL_SCRIPT: dict[str, Any] = {
    "schema_version": aaw_llm_test_adapter.SCRIPT_SCHEMA_VERSION,
    "delay_seconds": 30.0,
    "nodes": {"N01": {"outcome": "PASS", "summary": "never completes; cancelled mid-node"}},
}

LINE = "=" * 78


def head(title: str) -> None:
    print(f"\n{LINE}\n{title}\n{LINE}")


def force_rmtree(path: Path) -> None:
    """Git keeps loose objects read-only; plain rmtree fails on Windows."""
    def clear(func, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        func(target)
    if not path.exists():
        return
    try:
        shutil.rmtree(path, onexc=clear)
    except TypeError:  # Python < 3.12
        shutil.rmtree(path, onerror=lambda f, t, e: clear(f, t, e))


def git(argv: list[str], cwd: Path) -> None:
    subprocess.run(argv, cwd=cwd, check=True, stdout=subprocess.PIPE,
                   stderr=subprocess.PIPE, text=True)


def workspace(root: Path) -> tuple[Path, Path]:
    """A real git repo plus a real isolated worktree, as the runner demands."""
    root.mkdir(parents=True, exist_ok=True)
    repo, worktree = root / "repo", root / "worktree"
    force_rmtree(repo)
    force_rmtree(worktree)
    repo.mkdir(parents=True)
    git(["git", "init", "-b", "main"], repo)
    git(["git", "config", "user.email", "aaw@example.invalid"], repo)
    git(["git", "config", "user.name", "AAW Bridge Evidence"], repo)
    (repo / "README.md").write_text("baseline\n", encoding="utf-8")
    git(["git", "add", "."], repo)
    git(["git", "commit", "-m", "baseline"], repo)
    git(["git", "worktree", "add", "-b", "aaw/ux-slice", str(worktree)], repo)
    return repo, worktree


def sandbox_workflows(root: Path) -> Path:
    """A byte-identical copy of the real workflows, for write experiments.

    The write sections must prove a refusal and an acceptance without editing
    the committed workflow the regression suite runs against.
    """
    target = root / "WORKFLOWS"
    force_rmtree(target)
    target.mkdir(parents=True)
    for path in REAL_WORKFLOWS.glob("*.json"):
        shutil.copy2(path, target / path.name)
    return target


# ───────────────────────────── 1: BUILD ─────────────────────────────

def section_build(out: Path) -> dict[str, Any]:
    head("1  BUILD - the canvas graph, projected from the real workflow file")
    bridge = aaw_bridge.AawBridge(workflows_root=REAL_WORKFLOWS)
    listing = bridge.list_workflows()
    print(f"workflows_root : {listing['workflows_root']}")
    for row in listing["workflows"]:
        print(f"  {row['workflow_id']:<28} valid={row['valid']} nodes={row.get('node_count')} "
              f"semantic={str(row.get('semantic_hash'))[:16]}...")

    loaded = bridge.load_workflow(SLICE_ID)
    projection = loaded["projection"]
    print(f"\nsource file    : {loaded['path']}")
    print(f"semantic_hash  : {projection['semantic_hash']}")
    print(f"start_node     : {projection['start_node']}")
    print("\nNODES (as the canvas receives them)")
    for node in projection["nodes"]:
        flags = []
        if node["is_repair_template"]:
            flags.append("REPAIR TEMPLATE - never entered")
        if node["legacy_transitions"]:
            flags.append("legacy on_pass/on_fail")
        print(f"  {node['node_id']:<5} {node['node_type']:<13} routing={node['routing']:<11} "
              f"{'  '.join(flags)}")
    print("\nDECLARED EDGES (predicate, kind and label, no prose)")
    for edge in projection["edges"]:
        when = json.dumps(edge["when"], separators=(",", ":")) if edge["when"] else "unconditional"
        print(f"  {edge['edge_id']:<20} {edge['from']:>4} -> {str(edge['to']):<5} "
              f"{edge['kind']:<9} when={when:<24} label={edge['label']}")
    (out / "01_build_projection.json").write_text(
        json.dumps(projection, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nwrote {out / '01_build_projection.json'}")
    return {"semantic_hash": projection["semantic_hash"], "nodes": len(projection["nodes"]),
            "edges": len(projection["edges"])}


# ───────────────────────────── 2: LAYOUT ─────────────────────────────

def section_layout(out: Path) -> dict[str, Any]:
    head("2  LAYOUT - visual metadata persisted outside workflow semantics")
    bridge = aaw_bridge.AawBridge(workflows_root=REAL_WORKFLOWS)
    workflow_file = Path(bridge.workflow_path(SLICE_ID))
    before_bytes = workflow_file.read_bytes()
    before_hash = bridge.graph_projection(SLICE_ID)["semantic_hash"]

    auto = bridge.load_layout(SLICE_ID)
    print(f"auto layout source : {auto.get('source') or 'STORED'}  ({len(auto['nodes'])} nodes)")
    moved = {node_id: {"x": row["x"] + 37.5, "y": row["y"] - 12.0, "collapsed": False}
             for node_id, row in auto["nodes"].items()}
    saved = bridge.save_layout(SLICE_ID, {"nodes": moved, "viewport": {"x": 90, "y": 120, "k": 0.92}})

    after_bytes = workflow_file.read_bytes()
    after_hash = bridge.graph_projection(SLICE_ID)["semantic_hash"]
    print(f"\nlayout written to  : {saved['path']}")
    print(f"workflow file      : {workflow_file}")
    print(f"  bytes identical  : {before_bytes == after_bytes}")
    print(f"  semantic hash    : {before_hash[:24]}...  ->  {after_hash[:24]}...")
    print(f"  hash unchanged   : {before_hash == after_hash}")
    print(f"  layout records the workflow hash as provenance: "
          f"{saved['workflow_semantic_hash'] == after_hash}")

    # And the reverse direction: a candidate carrying coordinates is stripped.
    definition = json.loads(workflow_file.read_text(encoding="utf-8"))
    polluted = json.loads(json.dumps(definition))
    polluted["viewport"] = {"x": 1, "y": 2, "k": 1}
    for index, node in enumerate(polluted["nodes"]):
        node["x"], node["y"] = index * 296, 0
    report = bridge.validate_candidate(polluted)
    print(f"\ncandidate carrying coordinates:")
    print(f"  valid            : {report['valid']}")
    print(f"  stripped         : {report['stripped_visual_fields'][:4]} ... "
          f"({len(report['stripped_visual_fields'])} keys)")
    print(f"  semantic hash    : {report['semantic_hash'][:24]}...")
    print(f"  equals clean hash: {report['semantic_hash'] == after_hash}")
    return {"bytes_identical": before_bytes == after_bytes,
            "hash_unchanged": before_hash == after_hash,
            "stripped": len(report["stripped_visual_fields"]),
            "hash_after_strip_matches": report["semantic_hash"] == after_hash}


# ───────────────────────────── 3: SAFE WRITES ─────────────────────────────

def section_write(out: Path) -> dict[str, Any]:
    head("3  WRITE - candidate -> schema -> routing -> atomic, or refused")
    root = out / "write_sandbox"
    workflows = sandbox_workflows(root)
    bridge = aaw_bridge.AawBridge(workflows_root=workflows)
    path = Path(bridge.workflow_path(SLICE_ID))
    original = path.read_bytes()
    base_hash = bridge.graph_projection(SLICE_ID)["semantic_hash"]
    results: dict[str, Any] = {}

    def attempt(label: str, candidate: dict[str, Any], **kwargs: Any) -> None:
        try:
            report = bridge.save_workflow(SLICE_ID, candidate, **kwargs)
            print(f"  {label:<34} ACCEPTED  written={report['written']} "
                  f"hash={report['semantic_hash'][:16]}...")
            results[label] = {"accepted": True, "written": report["written"]}
        except aaw_bridge.BridgeError as exc:
            print(f"  {label:<34} REFUSED   [{exc.code}] {str(exc)[:88]}")
            results[label] = {"accepted": False, "code": exc.code}

    definition = json.loads(path.read_text(encoding="utf-8"))

    broken_edge = json.loads(json.dumps(definition))
    node = next(n for n in broken_edge["nodes"] if n["id"] == "N03")
    node["edges"].append({"edge_id": "E_N03_GHOST", "to": "N99_DOES_NOT_EXIST",
                          "when": {"verdict": "PASS"}, "kind": "CONTINUE"})
    attempt("edge to a node that is not there", broken_edge, base_semantic_hash=base_hash)

    bad_predicate = json.loads(json.dumps(definition))
    next(n for n in bad_predicate["nodes"] if n["id"] == "N03")["edges"][0]["when"] = {
        "verdict": "PASS", "severity_at_least": "HIGH"}
    attempt("predicate outside the closed set", bad_predicate, base_semantic_hash=base_hash)

    repair_to_task = json.loads(json.dumps(definition))
    next(n for n in repair_to_task["nodes"] if n["id"] == "N03")["edges"][1]["to"] = "N04"
    attempt("REPAIR edge targeting a non-template", repair_to_task, base_semantic_hash=base_hash)

    cyclic = json.loads(json.dumps(definition))
    next(n for n in cyclic["nodes"] if n["id"] == "N03R")["edges"][0]["to"] = "N03"
    attempt("cycle in a routing-contract graph", cyclic, base_semantic_hash=base_hash)

    renamed = json.loads(json.dumps(definition))
    renamed["workflow_id"] = "SOMETHING_ELSE"
    attempt("candidate declares another id", renamed, base_semantic_hash=base_hash)

    stale = json.loads(json.dumps(definition))
    next(n for n in stale["nodes"] if n["id"] == "N01")["instructions"] += "\n\nEdited."
    attempt("edit based on a stale hash", stale, base_semantic_hash="0" * 64)

    print(f"\n  workflow file untouched by every refusal : {path.read_bytes() == original}")
    results["file_untouched_after_refusals"] = path.read_bytes() == original

    valid = json.loads(json.dumps(definition))
    next(n for n in valid["nodes"] if n["id"] == "N01")["acceptance"].append(
        "The change is confined to the isolated worktree.")
    attempt("a valid edit", valid, base_semantic_hash=base_hash)
    after = bridge.graph_projection(SLICE_ID)["semantic_hash"]
    print(f"  semantic hash moved                      : {after != base_hash}")
    print(f"  the file still loads through the runner  : "
          f"{bool(runner.load_workflow(path)['workflow_id'])}")
    results["valid_edit_changed_hash"] = after != base_hash
    return results


# ───────────────────────────── 4 + 5: RUN and RESUME ─────────────────────────────

def section_run(out: Path) -> dict[str, Any]:
    head("4  RUN - a real run through the bridge, observed only through events")
    root = out / "run"
    repo, tree = workspace(root)
    stats = root / "03_STATS"
    previous_stats = runner.STATS_ROOT
    runner.STATS_ROOT = stats
    try:
        bridge = aaw_bridge.AawBridge(workflows_root=REAL_WORKFLOWS, stats_root=stats)
        captured: dict[str, Any] = {}
        adapter = aaw_llm_test_adapter.scripted_adapter(REPAIR_SCRIPT, captured=captured)
        handle = bridge.start_run(SLICE_ID, goal="harden refresh-token rotation",
                                  repo=repo, worktree=tree, preprocess_policy="OFF",
                                  adapter=adapter)
        print(f"run_id    : {handle['run_id']}")
        print(f"lifecycle : {handle['lifecycle']}   (returned before the first node finished)")
        print(f"journal   : {handle['journal_path']}")
        print("\nINCREMENTAL CONSUMPTION (exactly what the SSE stream delivers)")
        print("  seq  event                     node   detail")

        run_id, since, seen = handle["run_id"], 0, []
        deadline = time.monotonic() + 120
        checkpoint: int | None = None
        while time.monotonic() < deadline:
            batch = bridge.events(run_id, since=since)
            for event in batch["events"]:
                since = int(event["sequence"])
                seen.append(event)
                print(f"  {since:>3}  {event['event_type']:<24} {str(event['node_id'] or ''):<6} "
                      f"{event_detail(event)}")
                if event["event_type"] == "BRANCH_CREATED" and checkpoint is None:
                    checkpoint = since  # remembered for the resume section
            if batch["lifecycle"] == aaw_bridge.RUN_SETTLED:
                tail = bridge.events(run_id, since=since)
                for event in tail["events"]:
                    since = int(event["sequence"])
                    seen.append(event)
                    print(f"  {since:>3}  {event['event_type']:<24} "
                          f"{str(event['node_id'] or ''):<6} {event_detail(event)}")
                break
            time.sleep(0.1)

        frame = bridge.run_projection(run_id)
        state = bridge.run_state(run_id)
        print(f"\nfinal runner status : {state['status']}   ({frame['run']['runner_status']})")
        print(f"executed nodes      : {[row['node_id'] for row in frame['runtime']['nodes']]}")

        print("\nMINTED LINEAGE (nodes that did not exist when the run started)")
        for row in frame["runtime"]["minted_nodes"]:
            lineage = row["lineage"]
            print(f"  {row['node_id']} ({row['node_type']}, {row['node_kind']})")
            print(f"    origin        : {lineage['origin_node_id']} verdict={lineage['origin_verdict']} "
                  f"via {lineage['selected_edge_id']}")
            print(f"    template      : {lineage['template_id']}  (never entered)")
            print(f"    acceptance    : {row['acceptance']}")
            print(f"    carry_forward : {row['carry_forward']}")
            print(f"    brief         : {str(lineage['inherited_brief'])[:72]}...")
            print(f"    canvas position (per-frame, never persisted): "
                  f"{frame['layout']['nodes'].get(row['node_id'])}")

        print("\nROUTING EVIDENCE PER GATE (selected vs held, from the decision artifacts)")
        for row in frame["runtime"]["routing"]["decisions"]:
            print(f"  {row['node_id']:<5} {row['routing_mode']:<12} verdict={str(row['verdict']):<7} "
                  f"selected={row['selected']}")
            for held in row["held"]:
                print(f"        HELD {held['edge_id']:<16} -> {str(held['to']):<5} {held['hold_reason']}")

        print("\nV0.1.1 CHECK - carry_forward is not folded into acceptance")
        branch = next(iter(frame["runtime"]["minted_nodes"]), None)
        if branch:
            template = next(n for n in runner.load_workflow(
                Path(bridge.workflow_path(SLICE_ID)))["nodes"] if n["id"] == branch["lineage"]["template_id"])
            print(f"  template acceptance : {template['acceptance']}")
            print(f"  branch   acceptance : {branch['acceptance']}")
            print(f"  identical           : {branch['acceptance'] == template['acceptance']}")
            print(f"  branch carry_forward: {branch['carry_forward']}")
            package = captured.get(branch["node_id"], {}).get("package", {})
            print(f"  package ACCEPTANCE_CRITERIA      : {package.get('ACCEPTANCE_CRITERIA')}")
            print(f"  package INHERITED_CARRY_FORWARD  : {package.get('INHERITED_CARRY_FORWARD')}")
            print(f"  package INHERITED_BRIEF present  : {bool(package.get('INHERITED_BRIEF'))}")

        head("5  RESUME - reconnect from a stored sequence, no full replay")
        print(f"a consumer that had rendered up to sequence {checkpoint} reconnects:")
        resumed = bridge.events(run_id, since=checkpoint or 0)
        print(f"  total events in journal : {len(seen)}")
        print(f"  delivered on resume     : {resumed['count']} "
              f"(sequences {[e['sequence'] for e in resumed['events']][:6]}...)")
        print(f"  first delivered > since : "
              f"{bool(resumed['events']) and resumed['events'][0]['sequence'] > (checkpoint or 0)}")
        print(f"  nothing already seen is re-sent : "
              f"{all(e['sequence'] > (checkpoint or 0) for e in resumed['events'])}")
        from_zero = bridge.events(run_id, since=0)
        print(f"  a cold consumer gets everything : {from_zero['count']} events")

        (out / "04_run_frame.json").write_text(
            json.dumps(frame, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        (out / "04_run_events.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in seen), encoding="utf-8")
        print(f"\nwrote {out / '04_run_frame.json'}")
        return {
            "run_id": run_id, "status": state["status"],
            "executed": [row["node_id"] for row in frame["runtime"]["nodes"]],
            "minted": [row["node_id"] for row in frame["runtime"]["minted_nodes"]],
            "events": len(seen), "resume_from": checkpoint,
            "resume_delivered": resumed["count"], "cold_delivered": from_zero["count"],
        }
    finally:
        runner.STATS_ROOT = previous_stats


def event_detail(event: dict[str, Any]) -> str:
    payload = event.get("payload") or {}
    kind = event["event_type"]
    if kind in ("EDGE_SELECTED", "EDGE_HELD"):
        text = f"{payload.get('edge_id')} -> {payload.get('to')}"
        return text + (f"  ({payload['hold_reason']})" if payload.get("hold_reason") else "")
    if kind == "NODE_COMPLETED":
        return f"outcome={payload.get('outcome')} verdict={payload.get('verdict')}"
    if kind == "GATE_EVALUATED":
        return f"{payload.get('routing_mode')} verdict={payload.get('verdict')} no_route={payload.get('no_route')}"
    if kind == "BRANCH_CREATED":
        return f"from {payload.get('origin_node_id')} template={payload.get('template_id')}"
    if kind == "RUN_CANCELLED":
        return (f"{payload.get('reason')} @{payload.get('at_boundary')} "
                f"abandoned={payload.get('abandoned_frontier')}")
    if kind == "HUMAN_DECISION_REQUIRED":
        return f"candidate={payload.get('candidate_id')}"
    if kind == "NODE_STARTED":
        return f"type={payload.get('node_type')} frontier={payload.get('pending_frontier')}"
    return ""


# ───────────────────────────── 6: CANCEL ─────────────────────────────

def section_cancel(out: Path) -> dict[str, Any]:
    head("6  CANCEL - a Stop that terminates the process the run is waiting on")
    root = out / "cancel"
    repo, tree = workspace(root)
    stats = root / "03_STATS"
    previous_stats = runner.STATS_ROOT
    runner.STATS_ROOT = stats
    try:
        bridge = aaw_bridge.AawBridge(workflows_root=REAL_WORKFLOWS, stats_root=stats)
        adapter = aaw_llm_test_adapter.scripted_adapter(CANCEL_SCRIPT)
        handle = bridge.start_run(SLICE_ID, goal="cancellation evidence", repo=repo, worktree=tree,
                                  preprocess_policy="OFF", adapter=adapter)
        run_id = handle["run_id"]
        print(f"run_id : {run_id}")
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if any(row["event_type"] == "NODE_STARTED" for row in bridge.events(run_id)["events"]):
                break
            time.sleep(0.05)
        print("N01 is in flight (the scripted adapter holds it for 30s).")

        started = time.monotonic()
        report = bridge.cancel_run(run_id, reason="Stop pressed on the canvas", join_timeout=20)
        elapsed = time.monotonic() - started
        state = bridge.run_state(run_id)
        print(f"\ncancel returned in {elapsed:.2f}s")
        print(f"  accepted        : {report['cancelled']}")
        print(f"  effect          : {json.dumps(report['effect'], default=str)[:200]}")
        print(f"  runner status   : {state['status']}")
        cancellation = state.get("cancellation") or {}
        print(f"  stop_reason     : {state.get('stop_reason')}")
        print(f"  interrupted node: {cancellation.get('interrupted_node')}")
        print(f"  abandoned       : {cancellation.get('abandoned_frontier')}")
        print(f"  not interrupted : {cancellation.get('not_interrupted')}")
        events = bridge.events(run_id)["events"]
        for row in events:
            if row["event_type"] == "RUN_CANCELLED":
                print(f"  journal event   : seq {row['sequence']} {row['event_type']} "
                      f"{event_detail(row)}")
        idempotent = bridge.cancel_run(run_id, reason="pressed twice")
        print(f"  second Stop     : cancelled={idempotent['cancelled']} "
              f"({idempotent.get('reason') or 'accepted again, no new effect'})")

        # A run whose child is a real subprocess, to show a pid was signalled.
        print("\nAnd with a real child process rather than a scripted pause:")
        pid_evidence = cancel_with_real_child(out / "cancel_child")
        for key, value in pid_evidence.items():
            print(f"  {key:<16}: {value}")
        return {"status": state["status"], "elapsed_s": round(elapsed, 2),
                "cancelled_at": cancellation.get("at_boundary"),
                "abandoned": cancellation.get("abandoned_frontier"),
                "child": pid_evidence}
    finally:
        runner.STATS_ROOT = previous_stats


def cancel_with_real_child(root: Path) -> dict[str, Any]:
    """Cancel a run blocked on an actual MACHINE_GATE subprocess.

    The scripted adapter proves the frontier boundary; this proves the process
    boundary. A gate node running a long `python -c` sleep is a genuine child
    of the runner, and Stop must kill it rather than wait it out.
    """
    import run_cancellation

    repo, tree = workspace(root)
    stats = root / "03_STATS"
    previous_stats = runner.STATS_ROOT
    runner.STATS_ROOT = stats
    try:
        workflows = root / "WORKFLOWS"
        force_rmtree(workflows)
        workflows.mkdir(parents=True)
        (workflows / "GATE_CANCEL_PROBE_V1.json").write_text(
            json.dumps(gate_probe_workflow(timeout_seconds=300)), encoding="utf-8")

        bridge = aaw_bridge.AawBridge(workflows_root=workflows, stats_root=stats)
        handle = bridge.start_run("GATE_CANCEL_PROBE_V1", goal="cancel a real child",
                                  repo=repo, worktree=tree, preprocess_policy="OFF")
        run_id = handle["run_id"]
        supervised = bridge._handle(run_id)
        token: run_cancellation.CancellationToken = supervised.cancel
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline and token.snapshot()["live_processes"] == 0:
            if supervised.error:
                return {"runner_error": supervised.error}
            time.sleep(0.05)
        live = token.snapshot()["live_processes"]
        started = time.monotonic()
        report = bridge.cancel_run(run_id, reason="Stop pressed while the gate was running",
                                   join_timeout=25)
        elapsed = time.monotonic() - started
        killed = (report.get("effect") or {}).get("terminated_now") or []
        state = bridge.run_state(run_id)
        return {
            "live_children_at_stop": live,
            "signalled_pid": killed[0]["process_id"] if killed else None,
            "signal": killed[0]["signal"] if killed else None,
            "escalated_to_kill": killed[0]["escalated_to_kill"] if killed else None,
            "child_exit_code": killed[0]["exit_code"] if killed else None,
            "stopped_in_s": round(elapsed, 2),
            "child_would_have_slept_s": 120,
            "gate_timeout_was_s": 300,
            "runner_status": state.get("status"),
            "at_boundary": (state.get("cancellation") or {}).get("at_boundary"),
            "runner_error": supervised.error,
        }
    finally:
        runner.STATS_ROOT = previous_stats


def gate_probe_workflow(*, timeout_seconds: int) -> dict[str, Any]:
    """A minimal valid workflow whose only work is one long real subprocess.

    Deliberately not a mutation of the slice: repointing `start_node` there
    would strand the other nodes and the schema would rightly refuse it. Two
    nodes is the smallest graph the schema accepts (it requires a HUMAN_GATE).
    """
    def node(**fields: Any) -> dict[str, Any]:
        base = {"depends_on": [], "run_if": "ON_TRANSITION", "role": None, "model": None,
                "effort": None, "on_pass": None, "on_fail": None}
        return {**base, **fields}

    return {
        "workflow_id": "GATE_CANCEL_PROBE_V1", "version": "0.1",
        "description": "One long MACHINE_GATE subprocess, to prove Stop terminates a real child.",
        "goal": None, "start_node": "G01",
        "routing_contract": "AAW_MULTIROUTING_RUNTIME_CONTRACT_V0.1",
        "workspace_policy": {"isolated_worktree_required": True, "main_merge_allowed": False},
        "limits": {"max_nodes": 4, "max_repair_cycles": 1, "max_wall_time_minutes": 30,
                   "max_llm_calls": 1, "max_token_budget": None},
        "nodes": [
            node(id="G01", type="MACHINE_GATE", run_if="ALWAYS",
                 instructions="Sleep long enough that only a real termination can end it.",
                 acceptance=["The command exits with code 0."],
                 command=[sys.executable, "-c", "import time; time.sleep(120)"],
                 timeout_seconds=int(timeout_seconds), routing="FIRST_MATCH",
                 edges=[{"edge_id": "E_G01_CONTINUE", "to": "G09", "when": None,
                         "kind": "CONTINUE", "label": "continue"}]),
            node(id="G09", type="HUMAN_GATE",
                 instructions="Human acceptance.",
                 acceptance=["A human verdict is recorded against the candidate."]),
        ],
    }


# ───────────────────────────── serve ─────────────────────────────

def section_serve(out: Path, host: str, port: int, node_delay: float | None = None,
                  sandbox: bool = False) -> int:
    import aaw_bridge_server

    head("SERVE - the canvas against a live bridge")
    root = out / "serve"
    repo, tree = workspace(root)
    stats = root / "03_STATS"
    runner.STATS_ROOT = stats
    # `--sandbox-workflows` points the bridge at a byte-identical copy of the
    # real workflows, so an authoring trial can create, edit and delete freely
    # without touching WORKFLOWS/.
    roots = sandbox_workflows(root) if sandbox else REAL_WORKFLOWS
    bridge = aaw_bridge.AawBridge(workflows_root=roots, stats_root=stats)
    script = dict(REPAIR_SCRIPT)
    script["nodes"] = dict(script["nodes"])
    # An explicit fallback so a graph authored on the canvas during the trial
    # can actually run. Declared here, in the script, not inside the adapter.
    script["nodes"].setdefault("*", {
        "outcome": "PASS", "verdict": "PASS",
        "summary": "scripted PASS for a canvas-authored node",
        "recommended_next_action": "the gate decides",
    })
    if node_delay is not None:
        # A longer per-node pause so a human can actually press Stop mid-node.
        script["delay_seconds"] = float(node_delay)
    adapter = aaw_llm_test_adapter.scripted_adapter(script)
    server = aaw_bridge_server.serve(host=host, port=port, bridge=bridge,
                                     workspace=(repo, tree), adapter=adapter, verbose=True)
    url = f"http://{host}:{server.server_address[1]}/"
    print(f"canvas    : {url}")
    print(f"contract  : {url}api/contract")
    print(f"workflow  : {url}api/workflow?workflow_id={SLICE_ID}")
    print(f"workflows : {roots}")
    print(f"repo      : {repo}")
    print(f"worktree  : {tree}")
    print(f"stats     : {stats}")
    print(f"adapter   : SCRIPTED ({script['delay_seconds']}s per node, no paid calls)")
    print("\nopen the canvas, press Run, then press Stop mid-run. Ctrl+C here to stop the server.")
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\nstopping")
        server.shutdown()
    return 0


# ───────────────────────────── main ─────────────────────────────

SECTIONS = {"build": section_build, "layout": section_layout, "write": section_write,
            "run": section_run, "cancel": section_cancel}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="UX_SLICE_EVIDENCE")
    ap.add_argument("--only", nargs="+", choices=sorted(SECTIONS),
                    help="run only these sections")
    ap.add_argument("--serve", action="store_true", help="serve the canvas instead of reporting")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--node-delay", type=float,
                    help="seconds the scripted adapter holds each node, so Stop is clickable")
    ap.add_argument("--sandbox-workflows", action="store_true",
                    help="serve a copy of WORKFLOWS/ so authoring cannot touch the real ones")
    args = ap.parse_args(argv)

    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    if args.serve:
        return section_serve(out, args.host, args.port, args.node_delay,
                             sandbox=args.sandbox_workflows)

    summary: dict[str, Any] = {"bridge_version": aaw_bridge.BRIDGE_VERSION,
                               "routing_contract": rc.CONTRACT_VERSION}
    for name in (args.only or list(SECTIONS)):
        summary[name] = SECTIONS[name](out)
    (out / "SUMMARY.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\n\nwrote {out / 'SUMMARY.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
