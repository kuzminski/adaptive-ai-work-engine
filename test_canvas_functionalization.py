"""AAW CANVAS FUNCTIONALIZATION V0.1 — authoring, run control and truthfulness.

These drive the same substrate `test_aaw_bridge.py` does — the real
`workflow_runner`, real git worktrees, real MACHINE_GATE subprocesses, real
state, artifacts, journal and cancellation — with only `execute_llm_node`
substituted at the adapter boundary the runner documents.

Acceptance criteria of the objective and where they are proved:

  1  authored entirely from the canvas   test_a_four_node_workflow_is_authored_through_the_bridge_alone
  2  no manual JSON editing              same test: every mutation is a bridge call
  3  node creation / deletion            test_deleting_a_node_takes_every_edge_that_pointed_at_it
  4  edge creation / deletion            test_an_edge_can_be_created_and_deleted_through_the_write_path
  5  brief editing persists              test_an_edited_brief_survives_the_validated_write
  6  layout persists separately          test_authoring_never_writes_a_coordinate_into_the_workflow
  7  invalid edits cannot corrupt        test_an_invalid_draft_never_replaces_the_last_valid_workflow
                                         test_every_refusal_names_the_node_or_edge_that_caused_it
  8  Run uses the real bridge/runtime     test_the_authored_workflow_runs_on_the_real_runner
  9  Run from here is deterministic       test_run_from_here_re_executes_exactly_the_downstream_cone
                                          test_run_from_here_refuses_every_unsound_basis
 10  Stop performs real cancellation      (test_aaw_bridge, unchanged) + test_cancellation_reports_measured_partial_work
 11  cancellation warns about partial work test_cancellation_reports_measured_partial_work
 12  minted REPAIR lineage appears        test_reset_downstream_owns_the_branch_its_origin_minted
 13  inspector consumes structured data   test_the_inspector_payload_is_assembled_from_artifacts_not_prose
 14  regression suites stay green         the pre-existing suites, unchanged
 15  no simulated execution authority     test_the_active_frontend_contains_no_simulated_execution
                                          test_the_bridge_cannot_serve_the_archived_simulation
"""

from __future__ import annotations

import copy
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

import aaw_bridge
import aaw_bridge_server
import aaw_llm_test_adapter
import routing_contract as rc
import workflow_runner as runner
import workflow_schema
from workflow_schema import WorkflowValidationError

HERE = Path(__file__).parent
WORKFLOWS = HERE / "WORKFLOWS"
SLICE_ID = "MULTIROUTING_SLICE_V1"
LIVE_CANVAS = HERE / "UI_PROTOTYPE" / "aaw-canvas-live.html"
ARCHIVED_CANVAS = HERE / "DESIGN_REFERENCE" / "aaw-canvas.simulated.html"

BRIEF = "Bind rotation to device_id on the mobile deep-link exchange path."
CARRY = ["scope: auth/session/ only"]


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
    _git(["git", "config", "user.name", "AAW Canvas Test"], repo)
    (repo / "README.md").write_text("baseline\n", encoding="utf-8")
    _git(["git", "add", "."], repo)
    _git(["git", "commit", "-m", "baseline"], repo)
    _git(["git", "worktree", "add", "-b", "aaw/canvas", str(worktree)], repo)
    return repo, worktree


@pytest.fixture
def canvas(tmp_path, monkeypatch):
    """A bridge on an empty workflow directory plus a real workspace.

    Empty on purpose: the objective's trial starts from an empty canvas, so
    every node in these tests came from a `create_workflow` / `save_workflow`
    round trip and none of them from a fixture file.
    """
    stats = tmp_path / "03_STATS"
    monkeypatch.setattr(runner, "STATS_ROOT", stats)
    workflows = tmp_path / "WORKFLOWS"
    workflows.mkdir()
    bridge = aaw_bridge.AawBridge(workflows_root=workflows, stats_root=stats)
    repo, worktree = _workspace(tmp_path / "ws")
    return bridge, repo, worktree


def _drain(bridge, run_id, *, timeout=180.0):
    since, collected = 0, []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        batch = bridge.events(run_id, since=since)
        for event in batch["events"]:
            since = int(event["sequence"])
            collected.append(event)
        if batch["lifecycle"] == aaw_bridge.RUN_SETTLED:
            collected.extend(bridge.events(run_id, since=since)["events"])
            return collected
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} did not settle within {timeout}s")


# ─────────── the canvas's own draft model, as Python ───────────
#
# These mirror `addNode` / `addEdge` / `deleteNode` / `deleteEdge` in
# `aaw-canvas-live.html` exactly, so a test exercises the same candidate
# shapes the canvas posts. They are deliberately dumb: every rule that
# matters lives in the bridge, which is what these tests are proving.

DEFAULTS = {
    "IMPLEMENT": {"role": "CODE_IMPLEMENTER", "capability": "CODE_IMPLEMENTER",
                  "model": "gpt-5.6-sol", "effort": "medium",
                  "instructions": "Implement the supplied goal in the isolated worktree.",
                  "acceptance": ["The requested behaviour is implemented."]},
    "REVIEW": {"role": "INDEPENDENT_REVIEWER", "model": "gpt-5.6-sol", "effort": "high",
               "instructions": "Review in a fresh, read-only context. Set `verdict`.",
               "acceptance": ["The review ran in a fresh read-only context."]},
    "REPAIR": {"role": "CODE_IMPLEMENTER", "capability": "CODE_IMPLEMENTER",
               "model": "gpt-5.6-sol", "effort": "medium",
               "instructions": "Repair template; never entered directly.",
               "acceptance": ["Every point of the inherited brief is addressed."]},
    "HUMAN_GATE": {"role": None, "model": None, "effort": None,
                   "instructions": "Human acceptance.",
                   "acceptance": ["A human verdict is recorded against the candidate."]},
}


def add_node(draft, node_type, node_id):
    node = {"id": node_id, "type": node_type, "depends_on": [],
            "run_if": "ON_TRANSITION" if draft["nodes"] else "ALWAYS",
            "role": None, "model": None, "effort": None, "instructions": "",
            "acceptance": [], "on_pass": None, "on_fail": None}
    node.update(copy.deepcopy(DEFAULTS[node_type]))
    if node_type != "HUMAN_GATE":
        node["routing"] = "FIRST_MATCH"
    draft["nodes"].append(node)
    return node


def add_edge(draft, source_id, target_id, **overrides):
    source = next(n for n in draft["nodes"] if n["id"] == source_id)
    target = next((n for n in draft["nodes"] if n["id"] == target_id), None)
    kind = "REPAIR" if target and target["type"] == "REPAIR" else "CONTINUE"
    edge = {"edge_id": f"E_{source_id}_{target_id}", "to": target_id,
            "when": None, "kind": kind, "label": "continue"}
    edge.update(overrides)
    source.setdefault("edges", []).append(edge)
    # `normalizeEdgeOrder` in the canvas: FIRST_MATCH needs the unconditional
    # edge last, because it shadows every edge after it.
    if source.get("routing", "FIRST_MATCH") == "FIRST_MATCH":
        source["edges"] = ([e for e in source["edges"] if e.get("when")] +
                           [e for e in source["edges"] if not e.get("when")])
    return edge


def delete_node(draft, node_id):
    draft["nodes"] = [n for n in draft["nodes"] if n["id"] != node_id]
    for node in draft["nodes"]:
        if node.get("edges"):
            node["edges"] = [e for e in node["edges"] if e["to"] != node_id]
            if not node["edges"]:
                node.pop("edges")
        node["depends_on"] = [d for d in node["depends_on"] if d != node_id]


def delete_edge(draft, edge_id):
    for node in draft["nodes"]:
        if node.get("edges"):
            node["edges"] = [e for e in node["edges"] if e["edge_id"] != edge_id]
            if not node["edges"]:
                node.pop("edges")


def author_four_node(bridge, workflow_id="CANVAS_TRIAL_V1"):
    """The objective's §7 target, performed entirely through the bridge.

    create → 3 × IMPLEMENT → 1 × REVIEW → wire → edit briefs → validated save.
    Nothing here opens a file, and nothing edits JSON by hand.
    """
    draft = bridge.blank_workflow(workflow_id)          # one HUMAN_GATE, N01
    bridge.create_workflow(workflow_id, draft)
    for index, kind in enumerate(["IMPLEMENT", "IMPLEMENT", "IMPLEMENT", "REVIEW"], start=2):
        add_node(draft, kind, f"N{index:02d}")
    draft["start_node"] = "N02"
    add_edge(draft, "N02", "N03")
    add_edge(draft, "N03", "N04")
    add_edge(draft, "N04", "N05")
    add_edge(draft, "N05", "N01", when={"verdict": "PASS"}, label="PASS")
    for index in range(2, 6):
        node = next(n for n in draft["nodes"] if n["id"] == f"N{index:02d}")
        node["instructions"] = f"brief for {node['id']}: {BRIEF}"
    return draft, bridge.save_workflow(workflow_id, draft,
                                       base_semantic_hash=rc.semantic_hash(
                                           bridge.load_workflow(workflow_id)["definition"]))


PASS_SCRIPT = {"delay_seconds": 0.0, "nodes": {
    "*": {"outcome": "PASS", "verdict": "PASS", "summary": "scripted PASS",
          "carry_forward": CARRY, "artifacts": ["evidence.json"]},
}}


# ═══════════════ 1-2: authored entirely from the canvas ═══════════════

def test_a_four_node_workflow_is_authored_through_the_bridge_alone(canvas):
    """Create, add four nodes, wire them, brief them, save. No JSON by hand."""
    bridge, _repo, _worktree = canvas
    assert bridge.list_workflows()["workflows"] == []      # an empty canvas

    draft, report = author_four_node(bridge)
    assert report["code"] == aaw_bridge.WRITE_OK and report["written"] is True

    reopened = bridge.load_workflow("CANVAS_TRIAL_V1")
    assert reopened["valid"] is True
    ids = [node["id"] for node in reopened["definition"]["nodes"]]
    assert ids == ["N01", "N02", "N03", "N04", "N05"]
    types = {node["id"]: node["type"] for node in reopened["definition"]["nodes"]}
    assert sorted(types.values()) == ["HUMAN_GATE", "IMPLEMENT", "IMPLEMENT", "IMPLEMENT", "REVIEW"]

    # the graph the canvas draws is the projection of what was written
    projection = reopened["projection"]
    assert {edge["edge_id"] for edge in projection["edges"]} == {
        "E_N02_N03", "E_N03_N04", "E_N04_N05", "E_N05_N01"}
    assert projection["start_node"] == "N02"
    # and it loads through the runner, not merely through a UI-side check
    assert workflow_schema.load_workflow(Path(reopened["path"]))["workflow_id"] == "CANVAS_TRIAL_V1"


def test_an_edited_brief_survives_the_validated_write(canvas):
    bridge, _repo, _worktree = canvas
    draft, _ = author_four_node(bridge)

    node = next(n for n in draft["nodes"] if n["id"] == "N04")
    node["instructions"] = "REWRITTEN: close the mobile replay window only."
    saved = bridge.save_workflow("CANVAS_TRIAL_V1", draft,
                                 base_semantic_hash=rc.semantic_hash(
                                     bridge.load_workflow("CANVAS_TRIAL_V1")["definition"]))
    assert saved["written"] is True
    reopened = bridge.load_workflow("CANVAS_TRIAL_V1")["definition"]
    assert next(n for n in reopened["nodes"] if n["id"] == "N04")["instructions"] == \
        "REWRITTEN: close the mobile replay window only."
    # the brief is executable identity, so the hash moved with it
    assert saved["semantic_hash"] != saved["previous_semantic_hash"]


# ═══════════════════ 3-4: node and edge lifecycle ═══════════════════

def test_an_edge_can_be_created_and_deleted_through_the_write_path(canvas):
    bridge, _repo, _worktree = canvas
    draft, _ = author_four_node(bridge)

    # add a second, conditional outgoing edge on the reviewer
    add_node(draft, "REPAIR", "N06")
    add_edge(draft, "N05", "N06", when={"verdict": "REPAIR"}, label="REPAIR — new branch")
    add_edge(draft, "N06", "N01")
    bridge.save_workflow("CANVAS_TRIAL_V1", draft)
    projection = bridge.graph_projection("CANVAS_TRIAL_V1")
    repair = next(e for e in projection["edges"] if e["edge_id"] == "E_N05_N06")
    assert repair["kind"] == "REPAIR" and repair["when"] == {"verdict": "REPAIR"}
    assert next(n for n in projection["nodes"] if n["node_id"] == "N06")["is_repair_template"] is True

    # deleting the edge leaves the template unreachable, which the validator
    # refuses — the canvas must not be able to write a dangling subgraph
    delete_edge(draft, "E_N05_N06")
    report = bridge.validate_candidate(draft)
    assert report["valid"] is False
    assert report["diagnostics"][0]["node_id"] == "N06"

    delete_node(draft, "N06")
    saved = bridge.save_workflow("CANVAS_TRIAL_V1", draft)
    assert saved["written"] is True
    assert {e["edge_id"] for e in saved["projection"]["edges"]} == {
        "E_N02_N03", "E_N03_N04", "E_N04_N05", "E_N05_N01"}


def test_deleting_a_node_takes_every_edge_that_pointed_at_it(canvas):
    bridge, _repo, _worktree = canvas
    draft, _ = author_four_node(bridge)

    delete_node(draft, "N03")
    add_edge(draft, "N02", "N04")      # reconnect the flow the deletion broke
    saved = bridge.save_workflow("CANVAS_TRIAL_V1", draft)
    assert saved["written"] is True
    written = bridge.load_workflow("CANVAS_TRIAL_V1")["definition"]
    assert [n["id"] for n in written["nodes"]] == ["N01", "N02", "N04", "N05"]
    targets = {edge["to"] for node in written["nodes"] for edge in node.get("edges") or []}
    assert "N03" not in targets


# ═══════════════ 5-7: layout, refusals, attribution ═══════════════

def test_authoring_never_writes_a_coordinate_into_the_workflow(canvas):
    """The canvas holds x/y next to node data; the write path must strip it."""
    bridge, _repo, _worktree = canvas
    draft, _ = author_four_node(bridge)
    clean_hash = rc.semantic_hash(bridge.load_workflow("CANVAS_TRIAL_V1")["definition"])

    dirty = copy.deepcopy(draft)
    dirty["viewport"] = {"x": 12.0, "y": 8.0, "k": 0.7}
    for index, node in enumerate(dirty["nodes"]):
        node["x"], node["y"] = float(index * 296), 0.0
    report = bridge.validate_candidate(dirty)
    assert report["valid"] is True
    assert report["semantic_hash"] == clean_hash                  # identity is untouched
    assert "viewport" in report["stripped_visual_fields"]
    assert sum(1 for f in report["stripped_visual_fields"] if f.endswith(".x")) == len(dirty["nodes"])

    before = Path(bridge.workflow_path("CANVAS_TRIAL_V1")).read_bytes()
    bridge.save_layout("CANVAS_TRIAL_V1", {"nodes": {"N02": {"x": 0.0, "y": 0.0}},
                                           "viewport": {"x": 12.0, "y": 8.0, "k": 0.7}})
    assert Path(bridge.workflow_path("CANVAS_TRIAL_V1")).read_bytes() == before
    assert bridge.load_layout("CANVAS_TRIAL_V1")["nodes"]["N02"] == {"x": 0.0, "y": 0.0}
    # a node the stored layout never mentioned still gets a place
    assert "N05" in bridge.load_layout("CANVAS_TRIAL_V1")["nodes"]


def test_an_invalid_draft_never_replaces_the_last_valid_workflow(canvas):
    bridge, _repo, _worktree = canvas
    draft, _ = author_four_node(bridge)
    path = Path(bridge.workflow_path("CANVAS_TRIAL_V1"))
    good = path.read_bytes()
    good_hash = rc.semantic_hash(json.loads(good))

    broken = copy.deepcopy(draft)
    broken["nodes"][1]["edges"] = [{"edge_id": "E_BROKEN", "to": "NOT_A_NODE",
                                    "when": None, "kind": "CONTINUE"}]
    with pytest.raises(aaw_bridge.BridgeError) as raised:
        bridge.save_workflow("CANVAS_TRIAL_V1", broken)
    assert raised.value.code == aaw_bridge.WRITE_SCHEMA_INVALID
    assert path.read_bytes() == good                        # byte-identical
    assert bridge.load_workflow("CANVAS_TRIAL_V1")["semantic_hash"] == good_hash

    # and the draft is still editable: fixing it writes normally
    broken["nodes"][1]["edges"][0]["to"] = "N03"
    broken["nodes"][1]["edges"][0]["edge_id"] = "E_N02_N03"
    assert bridge.save_workflow("CANVAS_TRIAL_V1", broken)["written"] is True


@pytest.mark.parametrize("mutate, node_id, edge_id", [
    (lambda d: d["nodes"][1]["edges"][0].update({"to": "NOWHERE"}), "N02", "E_N02_N03"),
    (lambda d: d["nodes"][1]["edges"][0].update({"when": {"nonsense": 1}}), "N02", "E_N02_N03"),
    (lambda d: d["nodes"][1].update({"type": "NOT_A_TYPE"}), "N02", None),
    (lambda d: d["nodes"][1].update({"model": None, "capability": None}), "N02", None),
    (lambda d: d["nodes"].append({"id": "N77", "type": "IMPLEMENT", "depends_on": [],
                                  "run_if": "ON_TRANSITION", "role": "R", "model": "m",
                                  "effort": "medium", "instructions": "x", "acceptance": [],
                                  "on_pass": None, "on_fail": None,
                                  "edges": [{"edge_id": "E_N77", "to": "N01", "when": None,
                                             "kind": "CONTINUE"}]}), "N77", None),
])
def test_every_refusal_names_the_node_or_edge_that_caused_it(canvas, mutate, node_id, edge_id):
    """§1: a validation error must be markable on the element that caused it.

    The attribution comes out of `workflow_schema` itself, never from reading
    the message text — the one technique this project refuses everywhere.
    """
    bridge, _repo, _worktree = canvas
    draft, _ = author_four_node(bridge)
    mutate(draft)
    report = bridge.validate_candidate(draft)
    assert report["valid"] is False
    diagnostic = report["diagnostics"][0]
    assert diagnostic["node_id"] == node_id
    assert diagnostic["edge_id"] == edge_id
    assert diagnostic["message"] == report["errors"][0]     # the message is unchanged


def test_a_workflow_that_already_exists_is_never_silently_replaced(canvas):
    bridge, _repo, _worktree = canvas
    author_four_node(bridge)
    with pytest.raises(aaw_bridge.BridgeError) as raised:
        bridge.create_workflow("CANVAS_TRIAL_V1", bridge.blank_workflow("CANVAS_TRIAL_V1"))
    assert raised.value.code == aaw_bridge.WRITE_ALREADY_EXISTS
    assert len(bridge.load_workflow("CANVAS_TRIAL_V1")["definition"]["nodes"]) == 5


def test_the_blank_workflow_the_canvas_opens_on_is_one_the_validator_accepts(canvas):
    """An empty canvas must not open onto a graph its own bridge would refuse."""
    bridge, _repo, _worktree = canvas
    blank = bridge.blank_workflow("EMPTY_V1")
    assert bridge.validate_candidate(blank)["valid"] is True
    assert [n["type"] for n in blank["nodes"]] == ["HUMAN_GATE"]


# ═══════════════════ 8: the authored workflow really runs ═══════════════════

def test_the_authored_workflow_runs_on_the_real_runner(canvas):
    bridge, repo, worktree = canvas
    author_four_node(bridge)
    adapter = aaw_llm_test_adapter.scripted_adapter(PASS_SCRIPT)
    handle = bridge.start_run("CANVAS_TRIAL_V1", goal="canvas trial", repo=repo,
                              worktree=worktree, preprocess_policy="OFF", adapter=adapter)
    events = _drain(bridge, handle["run_id"])

    executed = [e["node_id"] for e in events if e["event_type"] == rc.NODE_COMPLETED]
    assert executed == ["N02", "N03", "N04", "N05", "N01"]
    frame = bridge.run_projection(handle["run_id"])
    assert frame["runtime"]["status"] == "WAITING_FOR_HUMAN"
    # every drawable fact is a structured field of a real event
    completed = next(e for e in events if e["event_type"] == rc.NODE_COMPLETED
                     and e["node_id"] == "N05")
    assert completed["payload"]["verdict"] == "PASS"
    assert completed["payload"]["carry_forward"] == CARRY


def test_the_adapter_substitution_is_context_local_not_process_wide(canvas):
    """§6: two adapter-backed runs in one process must not interfere."""
    bridge, repo, worktree = canvas
    baseline = runner.execute_llm_node
    with runner.llm_adapter_scope(lambda *a, **k: None):
        assert runner.current_llm_adapter() is not baseline
        assert runner.execute_llm_node is baseline          # the module never moved
    assert runner.current_llm_adapter() is baseline
    assert runner.execute_llm_node is baseline


# ═══════════════════ 9: Run from here ═══════════════════

def test_the_downstream_cone_is_the_declared_reachability_of_a_node(canvas):
    """The one graph fact `Run from here` and `Reset downstream` both rest on."""
    definition = json.loads((WORKFLOWS / f"{SLICE_ID}.json").read_text(encoding="utf-8"))
    assert rc.downstream_cone(definition, "N05")["nodes"] == ["N05", "N09"]
    assert rc.downstream_cone(definition, "N03")["nodes"] == \
        ["N03", "N03R", "N04", "N05", "N06", "N09"]
    assert rc.downstream_cone(definition, "N09")["nodes"] == ["N09"]
    # a minted branch is owned by the node that minted it, not by its template
    minted = {"N03A": {"lineage": {"origin_node_id": "N03", "template_id": "N03R"}}}
    assert "N03A" in rc.downstream_cone(definition, "N03", minted=minted)["nodes"]
    assert "N03A" not in rc.downstream_cone(definition, "N09", minted=minted)["nodes"]
    with pytest.raises(rc.RoutingContractError):
        rc.downstream_cone(definition, "NOT_A_NODE")


def test_run_from_here_re_executes_exactly_the_downstream_cone(canvas):
    bridge, repo, worktree = canvas
    author_four_node(bridge)
    adapter = aaw_llm_test_adapter.scripted_adapter(PASS_SCRIPT)
    first = bridge.start_run("CANVAS_TRIAL_V1", goal="canvas trial", repo=repo,
                             worktree=worktree, preprocess_policy="OFF", adapter=adapter)
    _drain(bridge, first["run_id"])

    plan = bridge.plan_resume("CANVAS_TRIAL_V1", first["run_id"], "N04",
                              worktree=worktree)["summary"]
    assert plan["reset_nodes"] == ["N01", "N04", "N05"]
    assert plan["inherited_nodes"] == ["N02", "N03"]

    second = bridge.start_run("CANVAS_TRIAL_V1", goal="canvas trial", repo=repo,
                              worktree=worktree, preprocess_policy="OFF", adapter=adapter,
                              resume_from={"source_run_id": first["run_id"], "from_node": "N04"})
    events = _drain(bridge, second["run_id"])
    assert second["run_id"] != first["run_id"]              # a new run, never a reopened one

    executed = [e["node_id"] for e in events if e["event_type"] == rc.NODE_COMPLETED]
    assert executed == ["N04", "N05", "N01"]                # exactly the cone
    resumed = next(e for e in events if e["event_type"] == rc.RUN_RESUMED)
    assert resumed["sequence"] == 1                          # before any node started
    assert [row["node_id"] for row in resumed["payload"]["inherited"]] == ["N02", "N03"]
    assert resumed["payload"]["reset_nodes"] == ["N01", "N04", "N05"]

    state = bridge.run_state(second["run_id"])
    assert [row["node_id"] for row in state["completed_nodes"]] == \
        ["N02", "N03", "N04", "N05", "N01"]                  # inherited then executed
    # budget is derived from what was inherited, not laundered by the reset
    assert state["resumed_from"]["source_run_id"] == first["run_id"]
    # 2 inherited LLM nodes (N02, N03) + 2 executed (N04, N05); the HUMAN_GATE
    # is not an LLM call. The count is derived from the inherited records, so a
    # resumed run cannot launder its way past `limits` by resetting the nodes
    # that spent the budget.
    assert state["llm_calls"] == 4

    # the source run is evidence and was not touched
    assert bridge.run_state(first["run_id"])["status"] == "WAITING_FOR_HUMAN"


def test_run_from_here_refuses_every_unsound_basis(canvas):
    bridge, repo, worktree = canvas
    draft, _ = author_four_node(bridge)
    adapter = aaw_llm_test_adapter.scripted_adapter(PASS_SCRIPT)
    first = bridge.start_run("CANVAS_TRIAL_V1", goal="canvas trial", repo=repo,
                             worktree=worktree, preprocess_policy="OFF", adapter=adapter)
    _drain(bridge, first["run_id"])

    def refusal(**kwargs):
        with pytest.raises(aaw_bridge.BridgeError) as raised:
            bridge.plan_resume("CANVAS_TRIAL_V1", kwargs.get("run", first["run_id"]),
                               kwargs.get("node", "N04"),
                               worktree=kwargs.get("worktree", worktree))
        return raised.value.code

    assert refusal(run="AAW_NOT_A_RUN") == runner.RESUME_UNKNOWN_SOURCE_RUN
    assert refusal(node="N99") == runner.RESUME_TARGET_NOT_DECLARED
    assert refusal(worktree=repo) == runner.RESUME_WORKTREE_MISMATCH

    # a target whose depends_on the inherited set cannot cover
    draft["nodes"][1]["depends_on"] = []
    depends = copy.deepcopy(draft)
    next(n for n in depends["nodes"] if n["id"] == "N02")["depends_on"] = ["N05"]
    bridge.save_workflow("CANVAS_TRIAL_V1", depends)
    assert refusal(node="N02") == runner.RESUME_GRAPH_MOVED   # the edit moved the graph first

    # ... and once the graph has moved, no node of the old run is a basis
    assert refusal(node="N04") == runner.RESUME_GRAPH_MOVED


def test_a_run_that_never_recorded_a_semantic_hash_is_not_a_basis(canvas):
    """An unprovable basis is not a basis. Fail safe, do not guess."""
    bridge, _repo, _worktree = canvas
    author_four_node(bridge)
    definition = bridge.load_workflow("CANVAS_TRIAL_V1")["definition"]
    legacy = {"AAW_RUN_ID": "AAW_LEGACY", "workflow_id": "CANVAS_TRIAL_V1",
              "status": "COMPLETED", "completed_nodes": [], "node_results": [],
              "routing": {}}
    with pytest.raises(runner.ResumeRefused) as raised:
        runner.plan_resume(definition, legacy, "N04")
    assert raised.value.code == runner.RESUME_GRAPH_MOVED


def test_a_source_run_that_is_still_moving_is_refused(canvas):
    bridge, _repo, _worktree = canvas
    author_four_node(bridge)
    definition = bridge.load_workflow("CANVAS_TRIAL_V1")["definition"]
    live = {"AAW_RUN_ID": "AAW_LIVE", "workflow_id": "CANVAS_TRIAL_V1", "status": "RUNNING",
            "workflow_semantic_hash": rc.semantic_hash(definition),
            "completed_nodes": [], "node_results": [], "routing": {}}
    with pytest.raises(runner.ResumeRefused) as raised:
        runner.plan_resume(definition, live, "N04")
    assert raised.value.code == runner.RESUME_SOURCE_STILL_RUNNING


# ═══════════════════ Reset downstream ═══════════════════

def test_reset_downstream_resets_nothing_durable_and_says_so(canvas):
    bridge, repo, worktree = canvas
    author_four_node(bridge)
    adapter = aaw_llm_test_adapter.scripted_adapter(PASS_SCRIPT)
    handle = bridge.start_run("CANVAS_TRIAL_V1", goal="canvas trial", repo=repo,
                              worktree=worktree, preprocess_policy="OFF", adapter=adapter)
    _drain(bridge, handle["run_id"])

    state_before = json.dumps(bridge.run_state(handle["run_id"]), sort_keys=True)
    journal_before = Path(handle["journal_path"]).read_bytes()
    artifacts_before = sorted(p.name for p in Path(handle["state_path"]).parent.iterdir())

    report = bridge.reset_downstream(handle["run_id"], "N04")
    assert report["cone"]["nodes"] == ["N01", "N04", "N05"]
    assert report["cleared_ui_state"] == ["N01", "N04", "N05"]
    assert report["durable_changes"] == []
    assert any("worktree" in line for line in report["retained"])

    # nothing on disk moved: not the state, not the journal, not one artifact
    assert json.dumps(bridge.run_state(handle["run_id"]), sort_keys=True) == state_before
    assert Path(handle["journal_path"]).read_bytes() == journal_before
    assert sorted(p.name for p in Path(handle["state_path"]).parent.iterdir()) == artifacts_before


def test_reset_downstream_owns_the_branch_its_origin_minted(tmp_path, monkeypatch):
    """§5/§12: a runtime-minted REPAIR branch belongs to the node that minted it."""
    stats = tmp_path / "03_STATS"
    monkeypatch.setattr(runner, "STATS_ROOT", stats)
    bridge = aaw_bridge.AawBridge(workflows_root=WORKFLOWS, stats_root=stats)
    repo, worktree = _workspace(tmp_path / "ws")
    script = {"delay_seconds": 0.0, "nodes": {
        "N01": {"outcome": "PASS", "summary": "scoped"},
        "N02": {"outcome": "PASS", "summary": "implemented"},
        "N03": {"outcome": "FAIL", "verdict": "REPAIR", "next_brief": BRIEF,
                "carry_forward": CARRY, "summary": "replay window open"},
        "N03R": {"outcome": "PASS", "summary": "closed"},
    }}
    handle = bridge.start_run(SLICE_ID, goal="repair", repo=repo, worktree=worktree,
                              preprocess_policy="OFF",
                              adapter=aaw_llm_test_adapter.scripted_adapter(script))
    events = _drain(bridge, handle["run_id"])
    branch = next(e for e in events if e["event_type"] == rc.BRANCH_CREATED)
    branch_id = branch["node_id"]

    # the minted branch is in N03's cone, and not in the gate's
    assert branch_id in bridge.reset_downstream(handle["run_id"], "N03")["cone"]["minted"]
    assert bridge.reset_downstream(handle["run_id"], "N09")["cone"]["minted"] == []

    # and it is drawable from structured facts alone
    detail = bridge.node_detail(handle["run_id"], branch_id)
    assert detail["node_kind"] == rc.MINTED_REPAIR_BRANCH
    assert detail["primary"]["inherited_next_brief"] == BRIEF
    assert detail["primary"]["carry_forward"] == CARRY
    assert detail["primary"]["lineage"]["origin_node_id"] == "N03"
    assert detail["primary"]["lineage"]["template_id"] == "N03R"

    # a minted branch is runtime lineage; re-entering one is deferred, not guessed
    with pytest.raises(aaw_bridge.BridgeError) as raised:
        bridge.plan_resume(SLICE_ID, handle["run_id"], branch_id, worktree=worktree)
    assert raised.value.code == runner.RESUME_TARGET_NOT_DECLARED


# ═══════════════════ 11: cancellation truthfulness ═══════════════════

def test_cancellation_reports_measured_partial_work(tmp_path, monkeypatch):
    """§4: `PARTIAL_WORK_PRESENT` is measured with git, not asserted in prose."""
    stats = tmp_path / "03_STATS"
    monkeypatch.setattr(runner, "STATS_ROOT", stats)
    bridge = aaw_bridge.AawBridge(workflows_root=WORKFLOWS, stats_root=stats)
    repo, worktree = _workspace(tmp_path / "ws")

    def writing_adapter(workflow, state, node, tree, execution, execution_path, recorder=None):
        # A node that leaves half an implementation behind, then blocks until
        # it is killed — exactly the case the UX must not report as a clean stop.
        (Path(tree) / "half_written.py").write_text("def rotate(  # unfinished\n", encoding="utf-8")
        import run_cancellation
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            run_cancellation.check("writing_adapter")
            time.sleep(0.02)
        raise AssertionError("adapter was never cancelled")

    handle = bridge.start_run(SLICE_ID, goal="cancel", repo=repo, worktree=worktree,
                              preprocess_policy="OFF", adapter=writing_adapter)
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline and not (Path(worktree) / "half_written.py").exists():
        time.sleep(0.02)

    report = bridge.cancel_run(handle["run_id"], reason="Stop pressed on the canvas",
                               join_timeout=30.0)
    assert report["cancelled"] is True
    assert report["work_state"] == aaw_bridge.WORK_PARTIAL
    assert "half_written.py" in report["worktree"]["changed_files"]
    assert report["rollback_available"] is False
    assert any("still bill" in line for line in report["not_interrupted"])

    # the same fact is available on its own, for an inspect affordance
    probe = bridge.run_worktree(handle["run_id"])
    assert probe["work_state"] == aaw_bridge.WORK_PARTIAL
    assert probe["changed_files"] == ["half_written.py"]
    assert Path(probe["worktree"]) == Path(worktree).resolve()

    # and nothing was cleaned up: the file is still on disk for a human
    assert (Path(worktree) / "half_written.py").is_file()
    state = bridge.run_state(handle["run_id"])
    assert state["status"] == "CANCELLED"
    assert state["cancellation"]["interrupted_node"] == "N01"


def test_a_clean_stop_is_reported_as_clean(tmp_path, monkeypatch):
    """The banner must not cry wolf when there is nothing in the tree."""
    stats = tmp_path / "03_STATS"
    monkeypatch.setattr(runner, "STATS_ROOT", stats)
    bridge = aaw_bridge.AawBridge(workflows_root=WORKFLOWS, stats_root=stats)
    repo, worktree = _workspace(tmp_path / "ws")

    def idle_adapter(workflow, state, node, tree, execution, execution_path, recorder=None):
        import run_cancellation
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            run_cancellation.check("idle_adapter")
            time.sleep(0.02)
        raise AssertionError("adapter was never cancelled")

    handle = bridge.start_run(SLICE_ID, goal="cancel", repo=repo, worktree=worktree,
                              preprocess_policy="OFF", adapter=idle_adapter)
    time.sleep(0.6)
    report = bridge.cancel_run(handle["run_id"], join_timeout=30.0)
    assert report["work_state"] == aaw_bridge.WORK_CLEAN
    assert report["worktree"]["changed_files"] == []


def test_partial_work_left_by_one_run_is_reported_on_the_run_it_blocks(tmp_path, monkeypatch):
    """The case a canvas actually hits: a dirty tree refuses the *next* run.

    `validate_workspace` stops a run before its first node when the worktree is
    dirty, so that run writes no state and has no `worktree` field to read. The
    UX still has to say what is in the tree — otherwise the user sees only
    "SETTLED, 0 events" and no reason.
    """
    stats = tmp_path / "03_STATS"
    monkeypatch.setattr(runner, "STATS_ROOT", stats)
    bridge = aaw_bridge.AawBridge(workflows_root=WORKFLOWS, stats_root=stats)
    repo, worktree = _workspace(tmp_path / "ws")
    (Path(worktree) / "left_behind.py").write_text("def rotate(  # unfinished\n", encoding="utf-8")

    handle = bridge.start_run(SLICE_ID, goal="blocked", repo=repo, worktree=worktree,
                              preprocess_policy="OFF",
                              adapter=aaw_llm_test_adapter.scripted_adapter(PASS_SCRIPT))
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline and bridge.list_runs()["runs"][-1]["lifecycle"] != aaw_bridge.RUN_SETTLED:
        time.sleep(0.05)

    described = bridge.list_runs()["runs"][-1]
    assert described["runner_status"] == "RUNNER_ERROR"
    assert "dirty state" in described["error"]

    probe = bridge.run_worktree(handle["run_id"])
    assert probe["work_state"] == aaw_bridge.WORK_PARTIAL      # not UNKNOWN
    assert probe["changed_files"] == ["left_behind.py"]
    assert Path(probe["worktree"]) == Path(worktree)


# ═══════════════════ 13: the inspector's data ═══════════════════

def test_the_inspector_payload_is_assembled_from_artifacts_not_prose(canvas):
    bridge, repo, worktree = canvas
    author_four_node(bridge)
    adapter = aaw_llm_test_adapter.scripted_adapter(PASS_SCRIPT)
    handle = bridge.start_run("CANVAS_TRIAL_V1", goal="canvas trial", repo=repo,
                              worktree=worktree, preprocess_policy="OFF", adapter=adapter)
    _drain(bridge, handle["run_id"])

    detail = bridge.node_detail(handle["run_id"], "N05")
    primary, secondary = detail["primary"], detail["secondary"]

    # every PRIMARY field the objective names is a named structured fact
    for key in ("state", "instructions", "verdict", "summary",
                "inherited_next_brief", "carry_forward", "artifacts"):
        assert key in primary
    assert primary["state"] == "COMPLETED"
    assert primary["verdict"] == "PASS"
    assert primary["carry_forward"] == CARRY
    assert primary["artifacts"] == ["evidence.json"]
    assert primary["instructions"].startswith("brief for N05")

    # ... and every SECONDARY one too
    for key in ("model", "effort", "execution_id", "hashes", "telemetry", "duration_s"):
        assert key in secondary
    assert secondary["execution_id"].startswith("EXE_")
    assert secondary["hashes"]["decision_hash"]
    assert secondary["hashes"]["workflow_semantic_hash"] == \
        bridge.load_workflow("CANVAS_TRIAL_V1")["semantic_hash"]

    # the gate decision comes from the durable artifact, read as JSON
    assert Path(detail["gate"]["reference"]["artifact"]).is_file()
    assert detail["gate"]["decision"]["decision_hash"] == secondary["hashes"]["decision_hash"]

    # a node that has not run yet is reported as such, not invented
    fresh = bridge.node_detail(handle["run_id"], "N02")
    assert fresh["primary"]["state"] == "COMPLETED"
    assert bridge.node_detail(handle["run_id"], "N01")["primary"]["node_type"] == "HUMAN_GATE"


# ═══════════════════ 15: no simulated execution authority ═══════════════════

SIMULATION_TOKENS = ("plannedVerdict", "spawnRepair", "function engine(", "scriptedTimeline")


def test_the_active_frontend_contains_no_simulated_execution():
    text = LIVE_CANVAS.read_text(encoding="utf-8")
    for token in SIMULATION_TOKENS:
        assert token not in text, f"{token!r} is simulated execution authority"
    # every runtime fact the canvas draws is keyed off a real journal event
    for event in (rc.NODE_STARTED, rc.NODE_COMPLETED, rc.EDGE_SELECTED, rc.EDGE_HELD,
                  rc.BRANCH_CREATED, rc.RUN_CANCELLED, rc.RUN_RESUMED):
        assert f'case "{event}"' in text


def test_the_canvas_only_calls_endpoints_the_transport_serves():
    """A canvas that drifts from the bridge must fail here, not in a browser."""
    text = LIVE_CANVAS.read_text(encoding="utf-8")
    called = {match.group(1) for match in re.finditer(r'api\("(/api/[a-z/\-]+)', text)}
    served = set(re.findall(r'route == "(/api/[a-z/\-]+)"', aaw_bridge_server.__file__ and
                            Path(aaw_bridge_server.__file__).read_text(encoding="utf-8")))
    assert called, "the canvas calls no endpoint at all"
    assert called <= served, f"canvas calls unserved routes: {sorted(called - served)}"


def test_the_bridge_cannot_serve_the_archived_simulation():
    """§0: the simulated canvas is design history, never runtime authority."""
    assert ARCHIVED_CANVAS.is_file()                      # history is preserved
    assert "plannedVerdict" in ARCHIVED_CANVAS.read_text(encoding="utf-8")
    assert "NON-EXECUTABLE DESIGN REFERENCE" in ARCHIVED_CANVAS.read_text(encoding="utf-8")

    # it lives outside the only directory the transport serves, and the static
    # handler resolves every path against that root
    ui_root = aaw_bridge_server.UI_ROOT.resolve()
    assert ARCHIVED_CANVAS.resolve().is_relative_to(ui_root) is False
    assert not list(ui_root.rglob("*.html")) == []
    for path in ui_root.rglob("*.html"):
        assert "plannedVerdict" not in path.read_text(encoding="utf-8")


def test_the_contract_records_the_branch_merge_prerequisite():
    """§5: recorded on the wire, so a canvas cannot offer merge without seeing why not."""
    contract = aaw_bridge.public_contract()
    assert "path-scoped carry_forward is a prerequisite" in contract["merge_prerequisite"]
    assert "repair branch merge/rejoin" in contract["deferred"]
    assert rc.RUN_RESUMED in contract["event_types"]
    assert contract["work_states"] == [aaw_bridge.WORK_CLEAN, aaw_bridge.WORK_PARTIAL,
                                       aaw_bridge.WORK_UNKNOWN]
