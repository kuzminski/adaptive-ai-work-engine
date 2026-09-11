"""AAW OPERATOR RECOVERY & EDIT SAFETY V0.2 acceptance evidence."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

import aaw_bridge
import aaw_llm_test_adapter
import run_recovery
import workflow_runner as runner
from test_canvas_functionalization import WORKFLOWS, SLICE_ID, _drain, _workspace


def _cancel_with_edits(tmp_path: Path, monkeypatch, *, tracked: bool = True):
    stats = tmp_path / "03_STATS"
    monkeypatch.setattr(runner, "STATS_ROOT", stats)
    bridge = aaw_bridge.AawBridge(workflows_root=WORKFLOWS, stats_root=stats)
    repo, worktree = _workspace(tmp_path / "ws")

    def writing_adapter(workflow, state, node, tree, execution, execution_path, recorder=None):
        tree = Path(tree)
        (tree / "run_only.py").write_text("unfinished = True\n", encoding="utf-8")
        if tracked:
            (tree / "README.md").write_text("baseline\nrun edit\n", encoding="utf-8")
        import run_cancellation
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            run_cancellation.check("recovery-test")
            time.sleep(0.01)
        raise AssertionError("adapter was never cancelled")

    handle = bridge.start_run(SLICE_ID, goal="recovery", repo=repo, worktree=worktree,
                              preprocess_policy="OFF", adapter=writing_adapter)
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and not (worktree / "run_only.py").exists():
        time.sleep(0.01)
    bridge.cancel_run(handle["run_id"], reason="operator trial", join_timeout=10.0)
    return bridge, repo, worktree, handle["run_id"]


def test_run_owned_changes_are_attributed_and_explicitly_discarded(tmp_path, monkeypatch):
    bridge, repo, worktree, run_id = _cancel_with_edits(tmp_path, monkeypatch)

    inspected = bridge.run_worktree(run_id)
    assert inspected["originating_run_id"] == run_id
    assert inspected["modified_files"] == ["README.md"]
    assert inspected["untracked_files"] == ["run_only.py"]
    assert inspected["ownership"] == run_recovery.OWNED
    assert inspected["mixed_change_possible"] is False
    assert inspected["cleanup_available"] is True

    resolved = bridge.discard_run_changes(run_id)
    assert resolved["resolution"] == run_recovery.DISCARDED
    assert resolved["changed_files"] == []
    assert resolved["mixed_change_possible"] is False
    assert (worktree / "README.md").read_text(encoding="utf-8") == "baseline\n"
    assert not (worktree / "run_only.py").exists()

    # A new run gets past the clean-worktree guard after canvas-only recovery.
    script = {"*": {"outcome": "PASS", "summary": "rerun", "changed_files": [],
                    "tests": [], "findings": [], "remaining_uncertainty": [],
                    "recommended_next_action": "HUMAN_REQUIRED"}}
    second = bridge.start_run(SLICE_ID, goal="rerun", repo=repo, worktree=worktree,
                              preprocess_policy="OFF",
                              adapter=aaw_llm_test_adapter.scripted_adapter(script))
    _drain(bridge, second["run_id"])
    state = bridge.run_state(second["run_id"])
    assert state["status"] != "BLOCKED" or "dirty state" not in str(state.get("stop_reason"))


def test_cleanup_fails_closed_when_a_user_edit_is_mixed_in(tmp_path, monkeypatch):
    bridge, _repo, worktree, run_id = _cancel_with_edits(tmp_path, monkeypatch, tracked=False)
    (worktree / "run_only.py").write_text("operator changed this after cancel\n", encoding="utf-8")

    inspected = bridge.run_worktree(run_id)
    assert inspected["mixed_change_possible"] is True
    assert inspected["ownership"] == run_recovery.AMBIGUOUS
    assert inspected["cleanup_available"] is False
    assert any("no longer match" in reason for reason in inspected["proof_reasons"])
    with pytest.raises(aaw_bridge.BridgeError) as raised:
        bridge.discard_run_changes(run_id)
    assert raised.value.code == "AMBIGUOUS_OWNERSHIP"
    assert (worktree / "run_only.py").read_text(encoding="utf-8") == "operator changed this after cancel\n"


def test_keep_is_a_non_destructive_operator_resolution(tmp_path, monkeypatch):
    bridge, _repo, worktree, run_id = _cancel_with_edits(tmp_path, monkeypatch, tracked=False)
    before = (worktree / "run_only.py").read_bytes()
    report = bridge.keep_run_changes(run_id)
    assert report["resolution"] == run_recovery.KEPT
    assert report["kept"] is True
    assert report["cleanup_available"] is False
    assert (worktree / "run_only.py").read_bytes() == before


def test_active_run_exclusively_owns_its_worktree(tmp_path, monkeypatch):
    stats = tmp_path / "03_STATS"
    monkeypatch.setattr(runner, "STATS_ROOT", stats)
    bridge = aaw_bridge.AawBridge(workflows_root=WORKFLOWS, stats_root=stats)
    repo, worktree = _workspace(tmp_path / "ws")
    script = {"schema_version": "AAW_SCRIPTED_ADAPTER_V0.1", "delay_seconds": 10,
              "nodes": {"*": {"outcome": "PASS"}}}
    first = bridge.start_run(SLICE_ID, goal="owner", repo=repo, worktree=worktree,
                             preprocess_policy="OFF",
                             adapter=aaw_llm_test_adapter.scripted_adapter(script))
    with pytest.raises(aaw_bridge.BridgeError) as raised:
        bridge.start_run(SLICE_ID, goal="overlap", repo=repo, worktree=worktree,
                         preprocess_policy="OFF",
                         adapter=aaw_llm_test_adapter.scripted_adapter(script))
    assert raised.value.code == "WORKTREE_IN_USE"
    # The same refusal holds across independent bridge/server instances.
    another_bridge = aaw_bridge.AawBridge(workflows_root=WORKFLOWS, stats_root=stats)
    with pytest.raises(aaw_bridge.BridgeError) as cross_process_shape:
        another_bridge.start_run(SLICE_ID, goal="other server", repo=repo, worktree=worktree,
                                 preprocess_policy="OFF",
                                 adapter=aaw_llm_test_adapter.scripted_adapter(script))
    assert cross_process_shape.value.code == "WORKTREE_IN_USE"
    bridge.cancel_run(first["run_id"], join_timeout=10.0)


def test_cleanup_implementation_has_no_blind_reset_or_recursive_delete():
    source = Path(run_recovery.__file__).read_text(encoding="utf-8")
    assert '"reset", "--hard"' not in source
    assert "rmtree" not in source
    assert '"restore"' in source


def test_conflicting_first_match_diagnostic_names_every_affected_edge():
    bridge = aaw_bridge.AawBridge(workflows_root=WORKFLOWS)
    candidate = json.loads((WORKFLOWS / f"{SLICE_ID}.json").read_text(encoding="utf-8"))
    node = candidate["nodes"][0]
    node["edges"] = [
        {"edge_id": "E_ONE", "to": node["edges"][0]["to"], "when": None,
         "kind": "CONTINUE", "label": "one"},
        {"edge_id": "E_TWO", "to": node["edges"][0]["to"], "when": None,
         "kind": "CONTINUE", "label": "two"},
    ]
    report = bridge.validate_candidate(candidate)
    assert report["valid"] is False
    assert {row["edge_id"] for row in report["diagnostics"]} == {"E_ONE", "E_TWO"}
    assert all(row["node_id"] == node["id"] for row in report["diagnostics"])


def test_bounded_draft_history_covers_every_core_build_mutation():
    history_js = Path(__file__).with_name("UI_PROTOTYPE") / "draft_history.js"
    program = r"""
const { DraftHistory } = require(process.argv[1]);
const assert = require('node:assert/strict');
const h = new DraftHistory(100);
const runtime = { run_id: 'RUNTIME_MUST_NOT_MOVE', events: [1, 2, 3] };
let state = { draft: { start_node: 'N01', nodes: [
  { id: 'N01', x: 0, brief: 'a', edges: [] },
  { id: 'N09', x: 90, brief: 'human', edges: [] }
] }, layout: { nodes: { N01: {x: 0, y: 0}, N09: {x: 90, y: 0} } } };
const states = [structuredClone(state)];
function edit(label, fn) { fn(state); h.record(state, label); states.push(structuredClone(state)); }
h.reset(state);
edit('node create', s => s.draft.nodes.push({id:'N02', x:20, brief:'b', edges:[]}));
edit('node move', s => s.layout.nodes.N02 = {x:25,y:40});
edit('node property edit', s => s.draft.nodes[2].brief = 'edited');
edit('edge create', s => s.draft.nodes[0].edges.push({edge_id:'E1',to:'N02',when:null}));
edit('edge edit', s => s.draft.nodes[0].edges[0].label = 'continue');
edit('start-node change', s => s.draft.start_node = 'N02');
edit('insert-into-edge', s => { s.draft.nodes.push({id:'N03',edges:[{edge_id:'E2',to:'N02'}]}); s.draft.nodes[0].edges[0].to='N03'; });
edit('edge delete', s => s.draft.nodes[0].edges = []);
edit('node delete', s => s.draft.nodes = s.draft.nodes.filter(n => n.id !== 'N03'));
assert.equal(h.undoStack.length, 9);
for (let i = states.length - 2; i >= 0; --i) assert.deepEqual(h.undo().state, states[i]);
assert.equal(h.canUndo(), false);
for (let i = 1; i < states.length; ++i) assert.deepEqual(h.redo().state, states[i]);
assert.equal(h.canRedo(), false);
assert.deepEqual(runtime, { run_id: 'RUNTIME_MUST_NOT_MOVE', events: [1, 2, 3] });
h.reset(states[states.length - 1]); // Save establishes a new clean baseline.
assert.equal(h.canUndo(), false);
console.log('DRAFT_HISTORY_CORE_MUTATIONS_OK');
"""
    completed = subprocess.run(
        ["node", "-e", program, str(history_js)], shell=False,
        capture_output=True, text=True, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "DRAFT_HISTORY_CORE_MUTATIONS_OK"


def test_canvas_exposes_history_recovery_and_contextual_repairs():
    canvas = (Path(__file__).with_name("UI_PROTOTYPE") / "aaw-canvas-live.html").read_text(encoding="utf-8")
    for element_id in ("btnUndo", "btnRedo", "btnDiscardDraft", "workKeep", "workAdopt", "workDiscard"):
        assert element_id in canvas
    assert "Ctrl+Shift+Z" in canvas
    assert "required completions (not routing)" in canvas
    assert "KEEP_ONLY_THIS_UNCONDITIONAL" in canvas
    assert "/api/run/worktree/keep" in canvas
    assert "/api/run/worktree/adopt" in canvas
    assert "/api/run/worktree/discard" in canvas


def test_adopted_baseline_supports_cancel_discard_and_third_run(tmp_path, monkeypatch):
    bridge, repo, worktree, first_run_id = _cancel_with_edits(tmp_path, monkeypatch)
    adopted = bridge.adopt_run_changes_as_baseline(first_run_id)

    assert adopted["resolution"] == run_recovery.ADOPTED
    assert adopted["baseline_state"] == run_recovery.BASELINE_AUTHORIZED
    assert adopted["baseline_id"].startswith("AWB_")
    approved_readme = (worktree / "README.md").read_bytes()
    approved_untracked = (worktree / "run_only.py").read_bytes()
    approved = run_recovery.inspect_authorized_baseline(worktree)
    assert approved["state"] == run_recovery.BASELINE_AUTHORIZED

    def second_adapter(workflow, state, node, tree, execution, execution_path, recorder=None):
        tree = Path(tree)
        (tree / "README.md").unlink()
        (tree / "run_only.py").unlink()
        (tree / "second_only.py").write_text("second run\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(tree), "add", "-A"], shell=False, check=True)
        import run_cancellation
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            run_cancellation.check("adopted-baseline-second-run")
            time.sleep(0.01)
        raise AssertionError("adapter was never cancelled")

    second = bridge.start_run(SLICE_ID, goal="second", repo=repo, worktree=worktree,
                              preprocess_policy="OFF", adapter=second_adapter)
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and not (worktree / "second_only.py").exists():
        time.sleep(0.01)
    bridge.cancel_run(second["run_id"], reason="operator cancel", join_timeout=10.0)
    inspected = bridge.run_worktree(second["run_id"])
    assert inspected["baseline_id"] == adopted["baseline_id"]
    assert inspected["baseline_provenance"]["originating_run_id"] == first_run_id
    assert set(inspected["changed_files"]) == {"README.md", "run_only.py", "second_only.py"}
    assert inspected["cleanup_available"] is True

    discarded = bridge.discard_run_changes(second["run_id"])
    assert discarded["changed_files"] == []
    assert (worktree / "README.md").read_bytes() == approved_readme
    assert (worktree / "run_only.py").read_bytes() == approved_untracked
    assert not (worktree / "second_only.py").exists()
    restored = run_recovery.inspect_authorized_baseline(worktree)
    assert restored["state"] == run_recovery.BASELINE_AUTHORIZED
    assert restored["current_snapshot"] == approved["snapshot"]

    script = {"*": {"outcome": "PASS", "summary": "third run", "changed_files": [],
                    "tests": [], "findings": [], "remaining_uncertainty": [],
                    "recommended_next_action": "HUMAN_REQUIRED"}}
    third = bridge.start_run(SLICE_ID, goal="third", repo=repo, worktree=worktree,
                             preprocess_policy="OFF",
                             adapter=aaw_llm_test_adapter.scripted_adapter(script))
    _drain(bridge, third["run_id"])
    state = bridge.run_state(third["run_id"])
    assert state["workspace_baseline"]["authorized_workspace_baseline"]["baseline_id"] == adopted["baseline_id"]


def test_manual_edit_after_adoption_is_baseline_divergence(tmp_path, monkeypatch):
    bridge, repo, worktree, run_id = _cancel_with_edits(tmp_path, monkeypatch, tracked=False)
    bridge.adopt_run_changes_as_baseline(run_id)
    (worktree / "run_only.py").write_text("manual divergence\n", encoding="utf-8")

    proof = run_recovery.inspect_authorized_baseline(worktree)
    assert proof["state"] == run_recovery.BASELINE_DIVERGED
    assert any("workspace contents changed" in reason for reason in proof["proof_reasons"])
    with pytest.raises(runner.WorkflowStop, match="BASELINE DIVERGED"):
        runner.validate_workspace(repo, worktree)


def test_head_change_after_adoption_is_never_silently_accepted(tmp_path, monkeypatch):
    bridge, repo, worktree, run_id = _cancel_with_edits(tmp_path, monkeypatch, tracked=False)
    bridge.adopt_run_changes_as_baseline(run_id)
    subprocess.run(["git", "-C", str(worktree), "add", "-A"], shell=False, check=True)
    subprocess.run(["git", "-C", str(worktree), "-c", "user.name=AAW Test", "-c",
                    "user.email=aaw@example.invalid", "commit", "-m", "external head move"],
                   shell=False, check=True, capture_output=True)

    proof = run_recovery.inspect_authorized_baseline(worktree)
    assert proof["state"] == run_recovery.BASELINE_DIVERGED
    assert any("HEAD changed" in reason for reason in proof["proof_reasons"])
    with pytest.raises(runner.WorkflowStop, match="BASELINE DIVERGED"):
        runner.validate_workspace(repo, worktree)


def test_adoption_uses_tree_refs_not_commits_or_branch_moves(tmp_path, monkeypatch):
    bridge, _repo, worktree, run_id = _cancel_with_edits(tmp_path, monkeypatch, tracked=False)
    before_head = subprocess.run(["git", "-C", str(worktree), "rev-parse", "HEAD"], shell=False,
                                 check=True, capture_output=True, text=True).stdout.strip()
    bridge.adopt_run_changes_as_baseline(run_id)
    proof = run_recovery.inspect_authorized_baseline(worktree)
    after_head = subprocess.run(["git", "-C", str(worktree), "rev-parse", "HEAD"], shell=False,
                                check=True, capture_output=True, text=True).stdout.strip()
    object_types = [subprocess.run(["git", "-C", str(worktree), "cat-file", "-t", proof[key]],
                                   shell=False, check=True, capture_output=True, text=True).stdout.strip()
                    for key in ("workspace_ref", "index_ref")]
    assert before_head == after_head
    assert object_types == ["tree", "tree"]
    source = Path(run_recovery.__file__).read_text(encoding="utf-8")
    assert '"commit"' not in source
    assert '"reset", "--hard"' not in source
