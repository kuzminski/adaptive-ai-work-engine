"""AAW UX RUNTIME BRIDGE V0.1 — the boundary, on the real runtime.

These drive `aaw_bridge` against the committed
`WORKFLOWS/MULTIROUTING_SLICE_V1.json` and the real `workflow_runner`: real git
worktrees, real workspace guards, real MACHINE_GATE subprocesses, real state,
artifacts, journal and cancellation. Only `execute_llm_node` is substituted, at
the adapter boundary the runner documents, which is the same convention
`test_multirouting_slice.py` uses.

Acceptance criteria of the objective and where they are proved:

  1 BUILD graph from real AAW data   test_build_graph_comes_from_the_real_workflow_file
  2 layout does not touch semantics  test_saving_a_layout_changes_no_workflow_byte
                                     test_a_candidate_carrying_coordinates_is_stripped
  3 invalid edits cannot commit      test_every_invalid_edit_is_refused_before_any_write
                                     test_a_stale_edit_is_refused
  4 a real run starts from the UX    test_start_run_returns_a_handle_before_the_run_finishes
  5 events drive the canvas          test_events_carry_everything_the_canvas_needs
  6 REPAIR materialises lineage      test_repair_lineage_is_visible_the_moment_it_is_minted
  7 selected/held from evidence      test_selected_and_held_edges_come_from_routing_evidence
  8 reconnect from a sequence        test_a_consumer_resumes_from_its_last_rendered_sequence
  9 Stop is a real mechanism         test_stop_terminates_the_child_the_run_is_waiting_on
                                     test_stop_between_nodes_abandons_the_frontier
 10 regressions stay green           the pre-existing suites, unchanged
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

import aaw_bridge
import aaw_llm_test_adapter
import routing_contract as rc
import run_cancellation
import workflow_layout
import workflow_runner as runner
from workflow_schema import WorkflowValidationError

WORKFLOWS = Path(__file__).with_name("WORKFLOWS")
SLICE_ID = "MULTIROUTING_SLICE_V1"
SLICE = WORKFLOWS / f"{SLICE_ID}.json"

BRIEF = "Bind rotation to device_id on the mobile deep-link exchange path."
CARRY = ["scope: auth/session/ only", "no merge to canonical"]
FINDING = {"severity": "HIGH", "file": "auth/session/refresh.py", "location": "L42",
           "description": "device_id dropped on rotate", "required_fix": "rebind device_id"}

REPAIR_SCRIPT = {
    "delay_seconds": 0.0,
    "nodes": {
        "N01": {"outcome": "PASS", "summary": "scoped"},
        "N02": {"outcome": "PASS", "summary": "implemented", "carry_forward": CARRY},
        "N03": {"outcome": "FAIL", "verdict": "REPAIR", "next_brief": BRIEF,
                "carry_forward": CARRY, "artifacts": ["N03_review.json"],
                "summary": "mobile replay window open", "findings": [FINDING]},
        "N03R": {"outcome": "PASS", "summary": "replay window closed"},
    },
}


# ───────────────────────────── fixtures ─────────────────────────────

def _git(argv, cwd):
    subprocess.run(argv, cwd=cwd, check=True, stdout=subprocess.PIPE,
                   stderr=subprocess.PIPE, text=True)


def _workspace(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    repo, worktree = root / "repo", root / "worktree"
    repo.mkdir()
    _git(["git", "init", "-b", "main"], repo)
    _git(["git", "config", "user.email", "aaw@example.invalid"], repo)
    _git(["git", "config", "user.name", "AAW Bridge Test"], repo)
    (repo / "README.md").write_text("baseline\n", encoding="utf-8")
    _git(["git", "add", "."], repo)
    _git(["git", "commit", "-m", "baseline"], repo)
    _git(["git", "worktree", "add", "-b", "aaw/bridge", str(worktree)], repo)
    return repo, worktree


def _workflows_copy(root: Path) -> Path:
    """A byte-identical copy, so a write test never edits the committed file."""
    target = root / "WORKFLOWS"
    target.mkdir(parents=True)
    for path in WORKFLOWS.glob("*.json"):
        (target / path.name).write_bytes(path.read_bytes())
    return target


@pytest.fixture
def read_bridge():
    """A bridge over the real, committed workflow directory. Read-only tests."""
    return aaw_bridge.AawBridge(workflows_root=WORKFLOWS)


@pytest.fixture
def write_bridge(tmp_path):
    return aaw_bridge.AawBridge(workflows_root=_workflows_copy(tmp_path / "build"))


@pytest.fixture
def run_bridge(tmp_path, monkeypatch):
    """A bridge whose runs land in tmp_path but whose workflows are the real ones."""
    stats = tmp_path / "03_STATS"
    monkeypatch.setattr(runner, "STATS_ROOT", stats)
    bridge = aaw_bridge.AawBridge(workflows_root=WORKFLOWS, stats_root=stats)
    repo, worktree = _workspace(tmp_path / "ws")
    return bridge, repo, worktree


def _drain(bridge, run_id, *, timeout=120.0, until_settled=True):
    """Consume events the way a stream consumer does: incrementally, by sequence."""
    since, collected = 0, []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        batch = bridge.events(run_id, since=since)
        for event in batch["events"]:
            since = int(event["sequence"])
            collected.append(event)
        if not until_settled or batch["lifecycle"] == aaw_bridge.RUN_SETTLED:
            tail = bridge.events(run_id, since=since)
            collected.extend(tail["events"])
            return collected
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} did not settle within {timeout}s")


def _run_repair_slice(run_bridge, script=None, captured=None):
    bridge, repo, worktree = run_bridge
    adapter = aaw_llm_test_adapter.scripted_adapter(script or REPAIR_SCRIPT, captured=captured)
    handle = bridge.start_run(SLICE_ID, goal="bridge slice", repo=repo, worktree=worktree,
                              preprocess_policy="OFF", adapter=adapter)
    events = _drain(bridge, handle["run_id"])
    return bridge, handle, events


# ═════════════════════════ 1: BUILD from real data ═════════════════════════

def test_build_graph_comes_from_the_real_workflow_file(read_bridge):
    loaded = read_bridge.load_workflow(SLICE_ID)
    assert Path(loaded["path"]) == SLICE
    on_disk = json.loads(SLICE.read_text(encoding="utf-8"))
    assert loaded["definition"] == on_disk

    projection = loaded["projection"]
    assert [node["node_id"] for node in projection["nodes"]] == \
        [node["id"] for node in on_disk["nodes"]]
    assert projection["semantic_hash"] == rc.semantic_hash(on_disk)


def test_the_projection_names_the_facts_the_canvas_draws(read_bridge):
    projection = read_bridge.graph_projection(SLICE_ID)
    by_edge = {edge["edge_id"]: edge for edge in projection["edges"]}

    # predicate, kind and label reach the UI verbatim; nothing is inferred
    assert by_edge["E_N03_PASS"]["when"] == {"verdict": "PASS"}
    assert by_edge["E_N03_REPAIR"]["kind"] == "REPAIR"
    assert by_edge["E_N03_BLOCKED"]["kind"] == "FALLBACK"
    assert by_edge["E_N04_HARDENING"]["routing"] == "ALL_MATCHES"

    # a REPAIR template is on the graph but must not read as a queued task
    template = next(n for n in projection["nodes"] if n["node_id"] == "N03R")
    assert template["is_repair_template"] is True
    assert next(n for n in projection["nodes"] if n["node_id"] == "N03")["is_repair_template"] is False


def test_a_broken_workflow_is_listed_with_its_error_not_hidden(write_bridge, tmp_path):
    broken = write_bridge.workflows_root / "BROKEN_V1.json"
    broken.write_text(json.dumps({"workflow_id": "BROKEN_V1"}), encoding="utf-8")
    row = next(r for r in write_bridge.list_workflows()["workflows"]
               if r["workflow_id"] == "BROKEN_V1")
    assert row["valid"] is False and row["error"]

    # it opens for editing, with the reason it cannot be drawn - not as an
    # empty graph, and not as an exception the editor cannot act on
    frame = write_bridge.load_workflow("BROKEN_V1")
    assert frame["valid"] is False
    assert frame["errors"] and "missing workflow fields" in frame["errors"][0]
    assert frame["definition"] == {"workflow_id": "BROKEN_V1"}
    assert frame["projection"] is None and frame["semantic_hash"] is None
    # but nothing will project or run it
    with pytest.raises(WorkflowValidationError):
        write_bridge.graph_projection("BROKEN_V1")


def test_the_ui_never_needs_the_runner(read_bridge):
    """Everything the UI reads is a projection, not runner internals."""
    frame_keys = set(read_bridge.load_workflow(SLICE_ID))
    assert frame_keys == {"bridge_version", "workflow_id", "path", "valid", "errors",
                          "semantic_hash", "definition", "projection", "layout"}
    # the boundary advertises itself, so a canvas can refuse a bridge it
    # does not understand instead of failing one field at a time
    contract = aaw_bridge.public_contract()
    assert contract["routing_contract"] == rc.CONTRACT_VERSION
    assert set(contract["event_types"]) == set(rc.EVENT_TYPES)


# ═════════════════════════ 2: layout is not semantics ═════════════════════════

def test_saving_a_layout_changes_no_workflow_byte(write_bridge):
    path = Path(write_bridge.workflow_path(SLICE_ID))
    before_bytes, before_hash = path.read_bytes(), write_bridge.graph_projection(SLICE_ID)["semantic_hash"]

    layout = write_bridge.load_layout(SLICE_ID)
    moved = {node_id: {"x": row["x"] + 41, "y": row["y"] - 17, "collapsed": True}
             for node_id, row in layout["nodes"].items()}
    saved = write_bridge.save_layout(SLICE_ID, {"nodes": moved, "viewport": {"x": 9, "y": 8, "k": 1.2}})

    assert path.read_bytes() == before_bytes
    assert write_bridge.graph_projection(SLICE_ID)["semantic_hash"] == before_hash
    # it went to its own store, keyed by workflow identity
    assert Path(saved["path"]) == workflow_layout.layout_path(write_bridge.workflows_root, SLICE_ID)
    assert Path(saved["path"]).parent.name == "LAYOUTS"
    # and it round-trips
    reloaded = write_bridge.load_layout(SLICE_ID)
    assert reloaded["nodes"]["N01"]["x"] == moved["N01"]["x"]
    assert reloaded["nodes"]["N01"]["collapsed"] is True
    assert reloaded["workflow_semantic_hash"] == before_hash


def test_a_layout_cannot_carry_semantic_fields(write_bridge):
    """The stored shape is a whitelist of numbers and flags, so a round trip
    through the layout store cannot smuggle graph meaning."""
    saved = write_bridge.save_layout(SLICE_ID, {"nodes": {
        "N01": {"x": 1, "y": 2, "instructions": "do something else",
                "edges": [{"edge_id": "X", "to": "N09"}], "acceptance": ["nope"]}}})
    assert saved["nodes"]["N01"] == {"x": 1.0, "y": 2.0}
    assert "instructions" not in saved["nodes"]["N01"]
    assert "edges" not in saved["nodes"]["N01"]


def test_a_layout_with_a_non_numeric_coordinate_is_refused(write_bridge):
    with pytest.raises(workflow_layout.LayoutError):
        write_bridge.save_layout(SLICE_ID, {"nodes": {"N01": {"x": "left", "y": 0}}})
    with pytest.raises(workflow_layout.LayoutError):
        write_bridge.save_layout(SLICE_ID, {"nodes": {"N01": {"y": 0}}})


def test_a_workflow_id_cannot_become_a_path(write_bridge):
    with pytest.raises(workflow_layout.LayoutError):
        workflow_layout.layout_path(write_bridge.workflows_root, "../../etc/passwd")


def test_a_candidate_carrying_coordinates_is_stripped(write_bridge):
    definition = json.loads(Path(write_bridge.workflow_path(SLICE_ID)).read_text(encoding="utf-8"))
    clean_hash = rc.semantic_hash(definition)
    polluted = json.loads(json.dumps(definition))
    polluted["viewport"] = {"x": 1, "y": 2, "k": 1}
    for index, node in enumerate(polluted["nodes"]):
        node["x"], node["y"], node["collapsed"] = index * 296, 0, True

    report = write_bridge.validate_candidate(polluted)
    assert report["valid"] is True
    assert "viewport" in report["stripped_visual_fields"]
    assert "nodes[0].x" in report["stripped_visual_fields"]
    # the decisive part: after stripping, identity is unchanged
    assert report["semantic_hash"] == clean_hash
    # so saving it writes nothing, because nothing semantic changed
    saved = write_bridge.save_workflow(SLICE_ID, polluted, base_semantic_hash=clean_hash)
    assert saved["written"] is False
    assert saved["reason"] == "SEMANTICALLY_IDENTICAL"


def test_the_semantic_hash_ignores_visual_metadata_by_construction():
    """Not "ignores by filtering" but "cannot see": the projection is a whitelist."""
    definition = json.loads(SLICE.read_text(encoding="utf-8"))
    base = rc.semantic_hash(definition)
    smuggled = json.loads(json.dumps(definition))
    smuggled["layout"] = {"nodes": {"N01": {"x": 5}}}
    smuggled["nodes"][0]["x"] = 999
    smuggled["nodes"][0]["_layout"] = {"collapsed": True}
    assert rc.semantic_hash(smuggled) == base
    # while a real semantic change does move it
    smuggled["nodes"][0]["acceptance"] = ["something new"]
    assert rc.semantic_hash(smuggled) != base


# ═════════════════════════ 3: safe writes ═════════════════════════

def _mutate(bridge, mutate):
    definition = json.loads(Path(bridge.workflow_path(SLICE_ID)).read_text(encoding="utf-8"))
    candidate = json.loads(json.dumps(definition))
    mutate(candidate, {node["id"]: node for node in candidate["nodes"]})
    return candidate


INVALID_EDITS = {
    "edge_to_a_missing_node": lambda wf, by_id: by_id["N03"]["edges"].append(
        {"edge_id": "E_GHOST", "to": "N99", "when": {"verdict": "PASS"}, "kind": "CONTINUE"}),
    "predicate_outside_the_closed_set": lambda wf, by_id: by_id["N03"]["edges"][0].__setitem__(
        "when", {"verdict": "PASS", "severity_at_least": "HIGH"}),
    "repair_edge_to_a_non_template": lambda wf, by_id: by_id["N03"]["edges"][1].__setitem__("to", "N04"),
    "cycle_in_a_contract_graph": lambda wf, by_id: by_id["N03R"]["edges"][0].__setitem__("to", "N03"),
    "duplicate_edge_id": lambda wf, by_id: by_id["N03"]["edges"].append(
        dict(by_id["N03"]["edges"][0])),
    "unsupported_edge_field": lambda wf, by_id: by_id["N03"]["edges"][0].__setitem__("priority", 3),
    "unknown_routing_mode": lambda wf, by_id: by_id["N03"].__setitem__("routing", "BEST_MATCH"),
    "unreachable_node": lambda wf, by_id: by_id["N02"]["edges"].__setitem__(
        0, {"edge_id": "E_N02_CONTINUE", "to": "N09", "when": None, "kind": "CONTINUE"}),
    "no_human_gate": lambda wf, by_id: wf.__setitem__(
        "nodes", [n for n in wf["nodes"] if n["type"] != "HUMAN_GATE"]),
    "goal_baked_into_the_file": lambda wf, by_id: wf.__setitem__("goal", "do the thing"),
    "main_merge_allowed": lambda wf, by_id: wf["workspace_policy"].__setitem__("main_merge_allowed", True),
}


@pytest.mark.parametrize("label", sorted(INVALID_EDITS))
def test_every_invalid_edit_is_refused_before_any_write(write_bridge, label):
    path = Path(write_bridge.workflow_path(SLICE_ID))
    original = path.read_bytes()
    base = write_bridge.graph_projection(SLICE_ID)["semantic_hash"]
    candidate = _mutate(write_bridge, INVALID_EDITS[label])

    report = write_bridge.validate_candidate(candidate)
    assert report["valid"] is False, f"{label} validated when it should not"
    assert report["errors"]

    with pytest.raises(aaw_bridge.BridgeError) as raised:
        write_bridge.save_workflow(SLICE_ID, candidate, base_semantic_hash=base)
    assert raised.value.code == aaw_bridge.WRITE_SCHEMA_INVALID
    # the decisive assertion: the valid workflow on disk is byte-identical
    assert path.read_bytes() == original


def test_a_stale_edit_is_refused(write_bridge):
    path = Path(write_bridge.workflow_path(SLICE_ID))
    base = write_bridge.graph_projection(SLICE_ID)["semantic_hash"]

    # somebody else commits a change while this edit is open
    other = _mutate(write_bridge, lambda wf, by_id: by_id["N01"]["acceptance"].append("Concurrent."))
    write_bridge.save_workflow(SLICE_ID, other, base_semantic_hash=base)
    moved = write_bridge.graph_projection(SLICE_ID)["semantic_hash"]
    assert moved != base
    after_other = path.read_bytes()

    mine = _mutate(write_bridge, lambda wf, by_id: by_id["N02"]["acceptance"].append("Mine."))
    with pytest.raises(aaw_bridge.BridgeError) as raised:
        write_bridge.save_workflow(SLICE_ID, mine, base_semantic_hash=base)
    assert raised.value.code == aaw_bridge.WRITE_STALE
    assert path.read_bytes() == after_other  # my stale edit clobbered nothing

    # rebasing on what is actually on disk succeeds
    rebased = _mutate(write_bridge, lambda wf, by_id: by_id["N02"]["acceptance"].append("Mine."))
    assert write_bridge.save_workflow(SLICE_ID, rebased, base_semantic_hash=moved)["written"] is True


def test_a_layout_save_never_makes_an_open_edit_stale(write_bridge):
    """The stale check is over semantics, so arranging a graph does not
    invalidate somebody's in-flight edit of it."""
    base = write_bridge.graph_projection(SLICE_ID)["semantic_hash"]
    layout = write_bridge.load_layout(SLICE_ID)
    write_bridge.save_layout(SLICE_ID, {"nodes": {
        node_id: {"x": row["x"] + 5, "y": row["y"]} for node_id, row in layout["nodes"].items()}})
    candidate = _mutate(write_bridge, lambda wf, by_id: by_id["N01"]["acceptance"].append("Later."))
    assert write_bridge.save_workflow(SLICE_ID, candidate, base_semantic_hash=base)["written"] is True


def test_a_candidate_declaring_another_identity_is_refused(write_bridge):
    candidate = _mutate(write_bridge, lambda wf, by_id: wf.__setitem__("workflow_id", "ELSEWHERE"))
    with pytest.raises(aaw_bridge.BridgeError) as raised:
        write_bridge.save_workflow(SLICE_ID, candidate)
    assert raised.value.code == aaw_bridge.WRITE_IDENTITY_MISMATCH


def test_a_written_workflow_still_loads_through_the_runner(write_bridge):
    base = write_bridge.graph_projection(SLICE_ID)["semantic_hash"]
    candidate = _mutate(write_bridge, lambda wf, by_id: by_id["N01"]["acceptance"].append("Scoped."))
    report = write_bridge.save_workflow(SLICE_ID, candidate, base_semantic_hash=base)
    # the runner's own loader is the real acceptance test of a write
    loaded = runner.load_workflow(Path(report["path"]))
    assert loaded["workflow_id"] == SLICE_ID
    assert "Scoped." in next(n for n in loaded["nodes"] if n["id"] == "N01")["acceptance"]


def test_a_malformed_candidate_is_reported_not_raised(write_bridge):
    report = write_bridge.validate_candidate({"workflow_id": "X", "nodes": "not a list"})
    assert report["valid"] is False and report["errors"]


# ═════════════════════════ 4-7: a real run ═════════════════════════

def test_start_run_returns_a_handle_before_the_run_finishes(run_bridge):
    bridge, repo, worktree = run_bridge
    slow = {"delay_seconds": 0.6, "nodes": dict(REPAIR_SCRIPT["nodes"])}
    handle = bridge.start_run(SLICE_ID, goal="handle first", repo=repo, worktree=worktree,
                              preprocess_policy="OFF",
                              adapter=aaw_llm_test_adapter.scripted_adapter(slow))
    assert handle["run_id"].startswith("AAW_")
    assert handle["lifecycle"] == aaw_bridge.RUN_ACTIVE
    # addressable immediately: the id, journal path and event cursor all exist
    # before the first node has produced anything
    assert bridge.events(handle["run_id"], since=0)["run_id"] == handle["run_id"]
    assert handle["run_id"] in {row["run_id"] for row in bridge.list_runs()["runs"]}
    _drain(bridge, handle["run_id"])


def test_events_carry_everything_the_canvas_needs(run_bridge):
    bridge, handle, events = _run_repair_slice(run_bridge)
    kinds = [event["event_type"] for event in events]
    assert kinds[:4] == ["NODE_STARTED", "NODE_COMPLETED", "GATE_EVALUATED", "EDGE_SELECTED"]

    # every event has stable identity: run, node, monotonic sequence, type
    sequences = [event["sequence"] for event in events]
    assert sequences == sorted(sequences) == list(range(1, len(events) + 1))
    for event in events:
        assert event["run_id"] == handle["run_id"]
        assert event["event_type"] in rc.EVENT_TYPES
        assert event["event_id"].startswith("REV_")
        assert isinstance(event["payload"], dict)

    completed = {e["node_id"]: e["payload"] for e in events if e["event_type"] == "NODE_COMPLETED"}
    # the forwarded handoff data is on the wire, not only in a file
    assert completed["N03"]["verdict"] == "REPAIR"
    assert completed["N03"]["next_brief"] == BRIEF
    assert completed["N03"]["carry_forward"] == CARRY
    assert completed["N03"]["artifacts"] == ["N03_review.json"]
    # and the final status is reachable without reading a log
    assert bridge.events(handle["run_id"])["runner_status"] == "WAITING_FOR_HUMAN"


def test_repair_lineage_is_visible_the_moment_it_is_minted(run_bridge):
    captured: dict = {}
    bridge, handle, events = _run_repair_slice(run_bridge, captured=captured)

    branch = next(e for e in events if e["event_type"] == "BRANCH_CREATED")
    assert branch["node_id"] == "N03A"
    assert branch["payload"]["origin_node_id"] == "N03"
    assert branch["payload"]["template_id"] == "N03R"
    assert branch["payload"]["inherited_brief"] == BRIEF

    # BRANCH_CREATED arrives before the branch runs, and carries a drawable node
    node = branch["payload"]["node"]
    assert node["node_id"] == "N03A"
    assert node["node_type"] == "REPAIR"
    assert node["node_kind"] == rc.MINTED_REPAIR_BRANCH
    assert node["depends_on"] == ["N03"]
    order = [e["event_type"] for e in events if e["node_id"] in ("N03A",)]
    assert order[0] == "BRANCH_CREATED" and "NODE_STARTED" in order

    # and the same node is in the projection, so a late consumer sees it too
    frame = bridge.run_projection(handle["run_id"])
    minted = frame["runtime"]["minted_nodes"]
    assert [row["node_id"] for row in minted] == ["N03A"]
    assert minted[0]["lineage"]["selected_edge_id"] == "E_N03_REPAIR"
    # the template was never entered
    assert "N03R" not in [row["node_id"] for row in frame["runtime"]["nodes"]]
    assert [row["node_id"] for row in frame["runtime"]["nodes"]] == \
        ["N01", "N02", "N03", "N03A", "N09"]

    # a minted node gets a canvas position without a run writing into a
    # BUILD artifact
    assert frame["layout"]["nodes"]["N03A"]["minted"] is True
    assert "N03A" not in workflow_layout.load_layout(bridge.workflows_root, SLICE_ID)["nodes"]

    # V0.1.1: the branch inherits acceptance verbatim and keeps carry_forward apart
    template = next(n for n in json.loads(SLICE.read_text(encoding="utf-8"))["nodes"]
                    if n["id"] == "N03R")
    assert minted[0]["acceptance"] == template["acceptance"]
    assert minted[0]["carry_forward"] == CARRY
    package = captured["N03A"]["package"]
    assert package["ACCEPTANCE_CRITERIA"] == template["acceptance"]
    assert package["INHERITED_CARRY_FORWARD"] == CARRY
    assert package["INHERITED_BRIEF"] == BRIEF


def test_selected_and_held_edges_come_from_routing_evidence(run_bridge):
    bridge, handle, events = _run_repair_slice(run_bridge)
    selected = [e["payload"] for e in events if e["event_type"] == "EDGE_SELECTED"]
    held = [e["payload"] for e in events if e["event_type"] == "EDGE_HELD"]

    assert [row["edge_id"] for row in selected] == [
        "E_N01_CONTINUE", "E_N02_CONTINUE", "E_N03_REPAIR", "E_N03R_CONTINUE"]
    assert {row["edge_id"]: row["hold_reason"] for row in held} == {
        "E_N03_PASS": rc.HELD_NOT_MATCHED, "E_N03_BLOCKED": rc.HELD_NOT_MATCHED}
    assert all(row["hold_reason"] in rc.HOLD_REASONS for row in held)

    # the same facts are in the durable decision the journal indexes, and the
    # gate trace behind them is on disk for the "why" panel
    decision = next(row for row in bridge.run_projection(handle["run_id"])["runtime"]["routing"]["decisions"]
                    if row["node_id"] == "N03")
    assert decision["selected"] == ["E_N03_REPAIR"]
    artifact = json.loads(Path(decision["artifact"]).read_text(encoding="utf-8"))
    trace = next(c for c in artifact["candidates"] if c["edge_id"] == "E_N03_PASS")
    assert trace["matched"] is False
    assert trace["trace"][0] == {"predicate": "verdict", "expected": "PASS",
                                "actual": "REPAIR", "matched": False}


def test_the_canvas_never_has_to_parse_prose(run_bridge):
    """Every visual decision the slice requires is a structured field."""
    bridge, handle, events = _run_repair_slice(run_bridge)
    by_kind = {}
    for event in events:
        by_kind.setdefault(event["event_type"], []).append(event["payload"])
    assert by_kind["NODE_STARTED"][0]["node_type"] == "IMPLEMENT"       # active node
    assert by_kind["NODE_COMPLETED"][0]["outcome"] == "PASS"            # completed node
    assert by_kind["NODE_COMPLETED"][2]["verdict"] == "REPAIR"          # review verdict
    assert by_kind["EDGE_SELECTED"][2]["edge_id"] == "E_N03_REPAIR"     # selected edge
    assert by_kind["EDGE_HELD"][0]["hold_reason"] in rc.HOLD_REASONS    # held edge
    assert by_kind["BRANCH_CREATED"][0]["node"]["node_id"] == "N03A"    # repair lineage
    assert by_kind["HUMAN_DECISION_REQUIRED"][0]["candidate_id"]        # final status


def test_a_human_decision_closes_the_gate_it_opened(run_bridge):
    bridge, handle, events = _run_repair_slice(run_bridge)
    assert any(e["event_type"] == "HUMAN_DECISION_REQUIRED" for e in events)
    opened = max(e["sequence"] for e in events)

    resolved = bridge.resolve_human_decision(handle["run_id"], "ACCEPT")
    assert resolved["runner_status"] == "READY_FOR_EXTERNAL_INTEGRATION"
    tail = bridge.events(handle["run_id"], since=opened)
    closing = next(e for e in tail["events"] if e["event_type"] == "HUMAN_DECISION_RESOLVED")
    assert closing["payload"]["verdict"] == "ACCEPTED"
    assert closing["sequence"] > opened


# ═════════════════════════ 8: resume ═════════════════════════

def test_a_consumer_resumes_from_its_last_rendered_sequence(run_bridge):
    bridge, handle, events = _run_repair_slice(run_bridge)
    run_id = handle["run_id"]
    total = len(events)

    cold = bridge.events(run_id, since=0)
    assert cold["count"] == total

    midpoint = total // 2
    resumed = bridge.events(run_id, since=midpoint)
    assert resumed["since"] == midpoint
    assert resumed["count"] == total - midpoint
    assert all(event["sequence"] > midpoint for event in resumed["events"])
    assert resumed["last_sequence"] == total

    # already fully caught up: nothing is replayed
    assert bridge.events(run_id, since=total)["count"] == 0
    assert bridge.events(run_id, since=total)["last_sequence"] == total


def test_resuming_in_pages_reconstructs_the_stream_exactly(run_bridge):
    bridge, handle, events = _run_repair_slice(run_bridge)
    since, pages = 0, []
    while True:
        batch = bridge.events(handle["run_id"], since=since, limit=3)
        if not batch["events"]:
            break
        pages.extend(batch["events"])
        since = batch["last_sequence"]
    assert [event["sequence"] for event in pages] == [event["sequence"] for event in events]


def test_an_unknown_run_is_refused_not_invented(run_bridge):
    bridge, _, _ = run_bridge
    with pytest.raises(aaw_bridge.BridgeError) as raised:
        bridge.events("AAW_19700101_000000_deadbeef")
    assert raised.value.code == "UNKNOWN_RUN"


def test_a_run_started_elsewhere_can_be_adopted_read_only(run_bridge):
    bridge, handle, events = _run_repair_slice(run_bridge)
    fresh = aaw_bridge.AawBridge(workflows_root=WORKFLOWS, stats_root=bridge.stats_root)
    adopted = fresh.adopt_run(handle["run_id"], workflow_id=SLICE_ID)
    assert adopted["runner_status"] == "WAITING_FOR_HUMAN"
    assert fresh.events(handle["run_id"])["count"] == len(events)
    assert adopted["adopted"] is True
    # a bridge will not claim it can stop a run it does not own
    refused = fresh.cancel_run(handle["run_id"])
    assert refused["cancelled"] is False
    assert "another process" in refused["reason"]


# ═════════════════════════ 9: real cancellation ═════════════════════════

def test_stop_terminates_the_child_the_run_is_waiting_on(tmp_path, monkeypatch):
    """A gate blocked on a 120s subprocess, with a 300s timeout, must stop in
    about a second — which is only possible if the child is really killed."""
    stats = tmp_path / "03_STATS"
    monkeypatch.setattr(runner, "STATS_ROOT", stats)
    workflows = tmp_path / "WORKFLOWS"
    workflows.mkdir()
    (workflows / "GATE_PROBE_V1.json").write_text(json.dumps(_gate_probe()), encoding="utf-8")
    repo, worktree = _workspace(tmp_path / "ws")
    bridge = aaw_bridge.AawBridge(workflows_root=workflows, stats_root=stats)

    handle = bridge.start_run("GATE_PROBE_V1", goal="cancel a real child",
                              repo=repo, worktree=worktree, preprocess_policy="OFF")
    token = bridge._handle(handle["run_id"]).cancel
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and token.snapshot()["live_processes"] == 0:
        assert bridge._handle(handle["run_id"]).error is None
        time.sleep(0.05)
    assert token.snapshot()["live_processes"] == 1, "the gate child never registered"

    started = time.monotonic()
    report = bridge.cancel_run(handle["run_id"], reason="Stop pressed", join_timeout=30)
    elapsed = time.monotonic() - started

    assert report["cancelled"] is True
    killed = report["effect"]["terminated_now"]
    assert len(killed) == 1
    assert isinstance(killed[0]["process_id"], int)
    assert killed[0]["signal"] == "TERMINATE"
    assert elapsed < 20, f"stop took {elapsed:.1f}s; the child was waited out, not killed"

    state = bridge.run_state(handle["run_id"])
    assert state["status"] == "CANCELLED"
    assert state["cancellation"]["reason"] == "Stop pressed"
    assert state["cancellation"]["interrupted_node"] == "G01"
    # what cannot be interrupted is stated, not glossed over
    assert any("bill" in note for note in state["cancellation"]["not_interrupted"])
    cancelled = next(e for e in bridge.events(handle["run_id"])["events"]
                     if e["event_type"] == "RUN_CANCELLED")
    assert cancelled["payload"]["interrupted_node"] == "G01"


def test_stop_between_nodes_abandons_the_frontier(run_bridge):
    """Cancellation must also stop a run that is not inside a child process."""
    bridge, repo, worktree = run_bridge
    slow = {"delay_seconds": 20.0, "nodes": dict(REPAIR_SCRIPT["nodes"])}
    handle = bridge.start_run(SLICE_ID, goal="stop mid node", repo=repo, worktree=worktree,
                              preprocess_policy="OFF",
                              adapter=aaw_llm_test_adapter.scripted_adapter(slow))
    run_id = handle["run_id"]
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if any(e["event_type"] == "NODE_STARTED" for e in bridge.events(run_id)["events"]):
            break
        time.sleep(0.05)

    started = time.monotonic()
    assert bridge.cancel_run(run_id, reason="Stop pressed", join_timeout=30)["cancelled"] is True
    assert time.monotonic() - started < 15

    state = bridge.run_state(run_id)
    assert state["status"] == "CANCELLED"
    assert state["frontier"] == []
    assert state["cancellation"]["at_boundary"].startswith("scripted_adapter:")
    assert "N02" not in [row["node_id"] for row in state["completed_nodes"]]


def test_cancelling_is_idempotent_and_honest_about_a_settled_run(run_bridge):
    bridge, handle, _ = _run_repair_slice(run_bridge)
    first = bridge.cancel_run(handle["run_id"])
    assert first["cancelled"] is False
    assert "settled" in first["reason"]
    assert bridge.run_state(handle["run_id"])["status"] == "WAITING_FOR_HUMAN"


def test_a_token_refuses_to_spawn_once_cancellation_has_landed():
    """The pre-spawn check closes the window where a Stop could be followed by
    a brand-new untracked child process."""
    token = run_cancellation.CancellationToken("AAW_test")
    token.request("stop")
    with run_cancellation.cancellation_scope(token):
        with pytest.raises(run_cancellation.RunCancelled):
            runner.run_process([sys.executable, "-c", "pass"], dispatch=True)
    # a non-dispatch helper spawn is unaffected: git must still work while a
    # run is winding down
    rc_code, _, _ = runner.run_process([sys.executable, "-c", "pass"])
    assert rc_code == 0


def test_the_token_reports_what_it_actually_did():
    token = run_cancellation.CancellationToken("AAW_test")
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert token.register(child) is True
    effect = token.request("stop", source=run_cancellation.CANCEL_REQUESTED_BY_USER)
    assert effect["first_request"] is True
    assert effect["terminated_now"][0]["process_id"] == child.pid
    assert child.poll() is not None
    again = token.request("stop again")
    assert again["first_request"] is False
    assert again["terminated_now"] == []


# ═════════════════════════ transport (HTTP + SSE) ═════════════════════════

@pytest.fixture
def server(tmp_path, monkeypatch):
    """A real loopback server over a real bridge. Port 0, so tests never clash."""
    import aaw_bridge_server

    stats = tmp_path / "03_STATS"
    monkeypatch.setattr(runner, "STATS_ROOT", stats)
    bridge = aaw_bridge.AawBridge(workflows_root=_workflows_copy(tmp_path / "build"),
                                  stats_root=stats)
    repo, worktree = _workspace(tmp_path / "ws")
    slow = {"delay_seconds": 0.25, "nodes": dict(REPAIR_SCRIPT["nodes"])}
    instance = aaw_bridge_server.serve(
        port=0, bridge=bridge, workspace=(repo, worktree),
        adapter=aaw_llm_test_adapter.scripted_adapter(slow))
    try:
        yield f"http://127.0.0.1:{instance.server_address[1]}", bridge
    finally:
        instance.shutdown()


def _get(base, path):
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(base + path, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _post(base, path, payload):
    import urllib.error
    import urllib.request
    request = urllib.request.Request(base + path, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _sse(base, run_id, *, last_event_id=None, stop_after=None, timeout=120):
    """Read an SSE stream frame by frame, the way a browser does."""
    import urllib.request
    request = urllib.request.Request(f"{base}/api/run/stream?run_id={run_id}")
    if last_event_id is not None:
        request.add_header("Last-Event-ID", str(last_event_id))
    routing, ids, kinds, done = [], [], [], False
    with urllib.request.urlopen(request, timeout=timeout) as response:
        buffer = b""
        while True:
            chunk = response.read(1)
            if not chunk:
                break
            buffer += chunk
            if not buffer.endswith(b"\n\n"):
                continue
            frame, buffer = buffer.decode("utf-8"), b""
            if frame.startswith(":"):
                kinds.append("keepalive")
                continue
            kind = next((line[7:] for line in frame.splitlines()
                         if line.startswith("event: ")), None)
            kinds.append(kind)
            if kind == "routing":
                routing.append(json.loads(frame.split("data: ", 1)[1]))
                ids.append(int(next(line[4:] for line in frame.splitlines()
                                    if line.startswith("id: "))))
                if stop_after and len(routing) >= stop_after:
                    return routing, ids, kinds, False
            if kind == "done":
                return routing, ids, kinds, True
    return routing, ids, kinds, done


def test_the_transport_serves_the_canvas_and_the_contract(server):
    import urllib.request
    base, _ = server
    with urllib.request.urlopen(base + "/", timeout=20) as page:
        body = page.read()
    assert page.status == 200
    assert b"AAW Canvas" in body
    # the served canvas is the bridge-driven one, and carries no simulation
    assert b"plannedVerdict" not in body
    assert b"/api/run/stream" in body

    status, contract = _get(base, "/api/contract")
    assert status == 200
    assert contract["routing_contract"] == rc.CONTRACT_VERSION
    assert _get(base, "/api/nope")[0] == 404
    assert _post(base, "/api/nope", {})[0] == 404


def test_the_transport_confines_static_serving(server):
    base, _ = server
    status, body = _get(base, "/ui/../../workflow_runner.py")
    assert status in (403, 404)
    assert body["error"] in ("OUTSIDE_UI_ROOT", "NO_FILE")


def test_the_transport_refuses_a_remote_bind():
    import aaw_bridge_server
    with pytest.raises(ValueError):
        aaw_bridge_server.serve(host="0.0.0.0", port=0)


def test_a_browser_cannot_choose_the_workspace(tmp_path, monkeypatch):
    """The repo and worktree a run may touch are fixed at server start."""
    import aaw_bridge_server
    monkeypatch.setattr(runner, "STATS_ROOT", tmp_path / "03_STATS")
    instance = aaw_bridge_server.serve(port=0, workspace=None)
    try:
        base = f"http://127.0.0.1:{instance.server_address[1]}"
        status, body = _post(base, "/api/run/start",
                             {"workflow_id": SLICE_ID, "goal": "x",
                              "repo": str(tmp_path), "worktree": str(tmp_path)})
        assert status == 400
        assert body["error"] == "NO_WORKSPACE"
    finally:
        instance.shutdown()


def test_invalid_and_stale_writes_are_distinct_over_http(server):
    base, bridge = server
    status, frame = _get(base, "/api/workflow?workflow_id=" + SLICE_ID)
    assert status == 200

    broken = json.loads(json.dumps(frame["definition"]))
    next(n for n in broken["nodes"] if n["id"] == "N03")["edges"].append(
        {"edge_id": "E_GHOST", "to": "NOPE", "when": None, "kind": "CONTINUE"})
    status, body = _post(base, "/api/workflow/save", {
        "workflow_id": SLICE_ID, "candidate": broken,
        "base_semantic_hash": frame["semantic_hash"]})
    assert (status, body["error"]) == (400, "SCHEMA_INVALID")

    status, body = _post(base, "/api/workflow/save", {
        "workflow_id": SLICE_ID, "candidate": frame["definition"],
        "base_semantic_hash": "0" * 64})
    # a conflict is not a bad request: the edit was fine, the world moved
    assert (status, body["error"]) == (409, aaw_bridge.WRITE_STALE)

    assert _post(base, "/api/workflow/validate", {"candidate": broken})[1]["valid"] is False
    assert bridge.graph_projection(SLICE_ID)["semantic_hash"] == frame["semantic_hash"]


def test_a_malformed_request_body_does_not_take_the_server_down(server):
    import urllib.error
    import urllib.request
    base, _ = server
    request = urllib.request.Request(base + "/api/workflow/validate", data=b"{not json",
                                     headers={"Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(request, timeout=20)
        raise AssertionError("malformed JSON was accepted")
    except urllib.error.HTTPError as exc:
        assert exc.code == 400
        assert json.loads(exc.read())["error"] == "MALFORMED_JSON"
    assert _get(base, "/api/contract")[0] == 200   # still serving


def test_the_sse_stream_resumes_from_last_event_id(server):
    """Criterion 8 over the wire, not just at the bridge."""
    base, _ = server
    status, run = _post(base, "/api/run/start", {"workflow_id": SLICE_ID, "goal": "sse"})
    assert status == 200
    run_id = run["run_id"]

    first, ids, _, done = _sse(base, run_id, stop_after=6)
    assert ids == [1, 2, 3, 4, 5, 6] and not done

    # reconnect exactly as a browser does, with the id it last rendered
    rest, more, kinds, done = _sse(base, run_id, last_event_id=ids[-1])
    assert done is True
    assert all(sequence > ids[-1] for sequence in more), "the stream replayed rendered history"
    assert sorted(ids + more) == list(range(1, len(ids) + len(more) + 1))
    assert [event["event_type"] for event in rest][-1] == "HUMAN_DECISION_REQUIRED"

    # status frames are sent on change, not on every poll
    assert kinds.count("status") <= kinds.count("routing")
    assert kinds[0] == "hello" and kinds[-1] == "done"

    # and a cold consumer still gets the whole run
    cold, cold_ids, _, _ = _sse(base, run_id)
    assert cold_ids == list(range(1, len(ids) + len(more) + 1))


def test_the_stream_refuses_an_unknown_run(server):
    base, _ = server
    status, body = _get(base, "/api/run/stream?run_id=AAW_19700101_000000_deadbeef")
    assert (status, body["error"]) == (400, "UNKNOWN_RUN")


def test_stop_over_http_reports_what_it_did(server):
    base, bridge = server
    slow = {"delay_seconds": 20.0, "nodes": dict(REPAIR_SCRIPT["nodes"])}
    bridge_run = bridge.start_run(
        SLICE_ID, goal="http stop", repo=Path(bridge.stats_root).parent / "ws" / "repo",
        worktree=Path(bridge.stats_root).parent / "ws" / "worktree", preprocess_policy="OFF",
        adapter=aaw_llm_test_adapter.scripted_adapter(slow))
    run_id = bridge_run["run_id"]
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if bridge.events(run_id)["events"]:
            break
        time.sleep(0.05)

    status, body = _post(base, "/api/run/cancel", {"run_id": run_id, "reason": "Stop pressed"})
    assert status == 200
    assert body["cancelled"] is True
    assert any("bill" in note for note in body["not_interrupted"])
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and bridge.run_state(run_id).get("status") == "RUNNING":
        time.sleep(0.1)
    assert bridge.run_state(run_id)["status"] == "CANCELLED"


def _gate_probe() -> dict:
    def node(**fields):
        base = {"depends_on": [], "run_if": "ON_TRANSITION", "role": None, "model": None,
                "effort": None, "on_pass": None, "on_fail": None}
        return {**base, **fields}
    return {
        "workflow_id": "GATE_PROBE_V1", "version": "0.1",
        "description": "One long MACHINE_GATE subprocess, to prove Stop kills a real child.",
        "goal": None, "start_node": "G01",
        "routing_contract": rc.CONTRACT_VERSION,
        "workspace_policy": {"isolated_worktree_required": True, "main_merge_allowed": False},
        "limits": {"max_nodes": 4, "max_repair_cycles": 1, "max_wall_time_minutes": 30,
                   "max_llm_calls": 1, "max_token_budget": None},
        "nodes": [
            node(id="G01", type="MACHINE_GATE", run_if="ALWAYS",
                 instructions="Sleep long enough that only a real termination can end it.",
                 acceptance=["The command exits with code 0."],
                 command=[sys.executable, "-c", "import time; time.sleep(120)"],
                 timeout_seconds=300, routing="FIRST_MATCH",
                 edges=[{"edge_id": "E_G01_CONTINUE", "to": "G09", "when": None,
                         "kind": "CONTINUE", "label": "continue"}]),
            node(id="G09", type="HUMAN_GATE", instructions="Human acceptance.",
                 acceptance=["A human verdict is recorded against the candidate."]),
        ],
    }


# ═════════════════════════ boundary hygiene ═════════════════════════

def test_the_bridge_exposes_no_way_to_mutate_runner_state():
    """The UI may read projections and ask for actions; it may not reach in."""
    public = {name for name in dir(aaw_bridge.AawBridge) if not name.startswith("_")}
    assert public == {
        "workflows_root", "stats_root",
        "list_workflows", "workflow_path", "load_workflow", "graph_projection",
        "validate_candidate", "save_workflow", "create_workflow", "blank_workflow",
        "load_layout", "save_layout",
        "start_run", "list_runs", "run_state", "run_projection", "events",
        "cancel_run", "resolve_human_decision", "adopt_run",
        # AAW CANVAS FUNCTIONALIZATION V0.1. All four are reads or plans:
        # `reset_downstream` mutates nothing durable and `plan_resume` starts
        # nothing, so the invariant this test pins is unchanged.
            "plan_resume", "reset_downstream", "run_worktree", "node_detail",
            # V0.2 recovery mutates only explicitly proven Git worktree paths
            # or its recovery manifest; neither can mutate runner history.
            "keep_run_changes", "discard_run_changes",
            # V0.3 adoption writes only content-addressed tree refs plus the
            # per-worktree authorization manifest; no runner state or branch.
            "adopt_run_changes_as_baseline",
        }


def test_a_run_handle_never_leaks_the_thread_or_the_token(run_bridge):
    bridge, handle, _ = _run_repair_slice(run_bridge)
    described = bridge.list_runs()["runs"][0]
    assert set(described) == {"run_id", "workflow_id", "goal", "lifecycle", "adopted",
                              "runner_status", "started_at", "finished_at", "error",
                              "cancellation", "resumed_from", "state_path", "journal_path"}
    assert isinstance(described["cancellation"], dict)   # a snapshot, not the token
    assert "thread" not in described


def test_the_layout_module_cannot_reach_a_workflow_file():
    """Structural, not incidental: nothing in the layout store writes outside
    LAYOUTS/, and it holds no workflow writer to borrow."""
    source = Path(workflow_layout.__file__).read_text(encoding="utf-8")
    assert "workflow_runner" not in source      # no runner, so no workflow writer
    assert "validate_workflow" not in source    # it does not know what a workflow is
    assert 'Path(workflows_root) / "LAYOUTS"' in source  # its only write root
    # every write goes through layout_path, which is anchored to that root
    assert str(workflow_layout.layout_path(Path("R"), "W")) == str(Path("R") / "LAYOUTS" / "W.layout.json")


def test_the_runner_still_runs_without_the_bridge(tmp_path, monkeypatch):
    """Cancellation and the run-id parameter are additive: the runner's own
    entry point behaves exactly as before when nothing is passed."""
    monkeypatch.setattr(runner, "STATS_ROOT", tmp_path / "03_STATS")
    repo, worktree = _workspace(tmp_path / "ws")
    monkeypatch.setattr(runner, "execute_llm_node",
                        aaw_llm_test_adapter.scripted_adapter(REPAIR_SCRIPT))
    state = runner.execute(SLICE, "no bridge", repo, worktree, preprocess_policy="OFF")
    assert state["status"] == "WAITING_FOR_HUMAN"
    assert [row["node_id"] for row in state["completed_nodes"]] == \
        ["N01", "N02", "N03", "N03A", "N09"]
    assert run_cancellation.current_token() is None
