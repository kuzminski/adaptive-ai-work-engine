"""AAW PRODUCTION USE EVIDENCE CAPTURE V0.1 — tests.

Two layers, matching how the module itself is built:

  * unit tests of the pure projections (`all_sessions`/`aggregate_report`)
    against hand-written JSONL/`workflow_state.json` fixtures — no bridge, no
    planner, no runner. These prove the read side is correct and rebuildable.
  * integration tests driving a real `EvidenceBridge` through
    `aaw_planner_test_adapter.scripted_planner` and
    `aaw_llm_test_adapter.scripted_adapter` — the same substitution convention
    `test_planner_proposal_pipeline.py` and `test_multirouting_slice.py` use.
    Only the provider seam is fake; routing, the runner, the ledger and the
    journal are all real.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

import aaw_bridge
import aaw_llm_test_adapter
import aaw_planner
import aaw_planner_test_adapter as scripted
import planner_proposal as pp
import production_use_evidence as puv
import workflow_runner as runner

WORKFLOW_ID = "PRODUCTION_USE_EVIDENCE_SLICE_V1"


# ═══════════════════════════ shared fixtures ═══════════════════════════

def _git(argv, cwd):
    subprocess.run(argv, cwd=cwd, check=True, stdout=subprocess.PIPE,
                   stderr=subprocess.PIPE, text=True)


def _workspace(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    repo, worktree = root / "repo", root / "worktree"
    repo.mkdir()
    _git(["git", "init", "-b", "main"], repo)
    _git(["git", "config", "user.email", "aaw@example.invalid"], repo)
    _git(["git", "config", "user.name", "AAW Evidence Test"], repo)
    (repo / "README.md").write_text("baseline\n", encoding="utf-8")
    _git(["git", "add", "."], repo)
    _git(["git", "commit", "-m", "baseline"], repo)
    _git(["git", "worktree", "add", "-b", "aaw/evidence", str(worktree)], repo)
    return repo, worktree


def _repair_workflow(bridge):
    """IMPLEMENT -> REVIEW -> {PASS/BLOCKED: HUMAN_GATE, REPAIR: repair template
    -> HUMAN_GATE}. Same REPAIR-branch shape `WORKFLOWS/MULTIROUTING_SLICE_V1.json`
    uses (N03/N03R), without the MACHINE_GATE step this test has no need of.
    """
    draft = bridge.blank_workflow(WORKFLOW_ID)
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
    bridge.create_workflow(WORKFLOW_ID, draft)
    return draft


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


@pytest.fixture
def evidence_bridge(tmp_path, monkeypatch):
    stats = tmp_path / "03_STATS"
    evidence_root = tmp_path / "PLANNER_EVIDENCE"
    monkeypatch.setattr(runner, "STATS_ROOT", stats)
    workflows = tmp_path / "WORKFLOWS"
    workflows.mkdir()
    bridge = puv.EvidenceBridge(workflows_root=workflows, stats_root=stats,
                                evidence_root=evidence_root)
    _repair_workflow(bridge)
    return bridge


def _plan(bridge, script, anchor="N01", *, instruction=""):
    with aaw_planner.planner_adapter_scope(scripted.scripted_planner(script)):
        return bridge.plan_from_node(WORKFLOW_ID, anchor, instruction=instruction)


def _run_and_wait(bridge, tmp_path, *, node_script, goal="evidence test run",
                  timeout=60.0):
    repo, worktree = _workspace(tmp_path / "ws")
    adapter = aaw_llm_test_adapter.scripted_adapter(node_script)
    handle = bridge.start_run(WORKFLOW_ID, goal=goal, repo=repo, worktree=worktree,
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


def _session_for_run(bridge, run_id):
    for session in bridge.all_sessions():
        if session["execution"]["run_id"] == run_id:
            return session
    raise AssertionError(f"no session links to run {run_id}")


# ═══════════════════════ planner-decision evidence ═══════════════════════

def test_session_identity_is_the_planner_request_id(evidence_bridge):
    frame = _plan(evidence_bridge, SLICE, instruction="add research")
    assert frame["status"] == pp.PROPOSAL_READY
    session = evidence_bridge.session_summary(frame["request_id"])
    assert session is not None
    assert session["session_id"] == frame["request_id"]
    assert session["planner"]["proposal_id"] == frame["proposal"]["proposal_id"]
    assert session["planner"]["proposal_status"] == pp.PROPOSAL_READY
    assert session["planner"]["operator_decision"] is None  # not yet decided


def test_accept_evidence_links_proposal_to_the_run_it_fed(evidence_bridge, tmp_path):
    frame = _plan(evidence_bridge, SLICE, instruction="add research")
    session_id = frame["request_id"]
    evidence_bridge.accept_proposal(frame["proposal"]["proposal_id"], persist=True)

    decided = evidence_bridge.session_summary(session_id)
    assert decided["planner"]["operator_decision"] == "ACCEPT"
    assert decided["execution"]["run_id"] is None  # accepted, not yet run

    node_script = {"nodes": {"*": {"outcome": "PASS", "verdict": "PASS"}}}
    run_id = _run_and_wait(evidence_bridge, tmp_path, node_script=node_script)

    linked = evidence_bridge.session_summary(session_id)
    assert linked["execution"]["run_id"] == run_id
    assert linked["execution"]["run_status"] in {"WAITING_FOR_HUMAN", "COMPLETED"}
    assert linked["post_accept_manual_edit"] is False
    # one coherent session: operator request -> proposal -> ACCEPT -> the real run
    assert linked["planner"]["operator_decision"] == "ACCEPT"


def test_reject_evidence_has_no_execution_and_no_fabricated_result(evidence_bridge):
    frame = _plan(evidence_bridge, SLICE, instruction="add research")
    session_id = frame["request_id"]
    evidence_bridge.reject_proposal(frame["proposal"]["proposal_id"], reason="not needed")

    session = evidence_bridge.session_summary(session_id)
    assert session["planner"]["operator_decision"] == "REJECT"
    assert session["execution"]["run_id"] is None
    assert session["execution"]["run_status"] is None
    assert session["execution"]["final_outcome"] is None
    assert session["resources"]["input_tokens"] is None
    assert session["resources"]["output_tokens"] is None


def test_invalid_proposal_evidence_is_captured(evidence_bridge):
    frame = _plan(evidence_bridge, {"anchors": {"N01": {"raw": ["not", "an", "object"]}}})
    assert frame["status"] == pp.PROPOSAL_INVALID
    session = evidence_bridge.session_summary(frame["request_id"])
    assert session["planner"]["proposal_status"] == pp.PROPOSAL_INVALID
    assert session["planner"]["operator_decision"] is None
    assert session["planner"]["proposal_id"] is None  # never became a real proposal


# ═══════════════════════════ dedup identity ═══════════════════════════
#
# The recorder's dedup guard is keyed on the bridge's own `sequence` --
# monotonic and unique for the life of one bridge instance -- scoped to that
# instance's own lifetime only (never seeded from a pre-existing file on
# disk). Two things must both hold:
#
#   1. replaying the SAME concrete event (the same row, same `sequence`,
#      drained twice by two overlapping calls on the SAME instance) is
#      deduplicated -- exactly one row lands on disk;
#   2. two DISTINCT real events/sessions that happen to carry semantically
#      identical *content* (same proposal hash, or even a coincidentally
#      reused `sequence` from a different instance) are never collapsed --
#      each remains its own evidence observation, distinguished by the
#      substrate's own identity (`request_id`, freshly minted per planner
#      ask; `sequence`, scoped per instance), never by comparing payloads.

def test_replay_of_the_same_concrete_event_is_deduplicated(tmp_path):
    """A row the bridge journal produced once, drained twice (the exact race
    two overlapping `plan_from_node`/`accept_proposal` calls on one live
    bridge instance can trigger), must be written to disk exactly once."""
    recorder = puv.EvidenceRecorder(tmp_path / "evidence")
    row = {"sequence": 7, "at": "2026-01-01T00:00:00.000", "contract": pp.PROPOSAL_CONTRACT,
          "event_type": pp.PLANNER_STARTED, "request_id": "PLANREQ-same-event",
          "workflow_id": "W", "anchor_node_id": "N01"}

    recorder.record_planner_events([row])
    recorder.record_planner_events([dict(row)])  # the second, overlapping drain

    on_disk = puv._read_jsonl(recorder.decisions_path)
    assert len(on_disk) == 1
    assert on_disk[0]["request_id"] == "PLANREQ-same-event"


def test_two_distinct_sessions_with_identical_content_remain_independent(evidence_bridge):
    """Two separate, real planning asks against the identical unchanged base
    graph with the identical scripted content necessarily produce the
    identical `proposal_id`/`proposal_hash` (proposal identity is a pure
    content hash -- see `test_proposal_identity_is_deterministic_over_content`
    in `test_planner_proposal_pipeline.py`). Despite that shared content,
    they are two distinct real operator interactions and must remain two
    distinct sessions."""
    first = _plan(evidence_bridge, SLICE, instruction="add research")
    second = _plan(evidence_bridge, SLICE, instruction="add research")
    assert first["status"] == second["status"] == pp.PROPOSAL_READY
    assert first["proposal"]["proposal_id"] == second["proposal"]["proposal_id"], \
        "content-identical proposals must share identity -- this is the base bridge's own contract"
    assert first["request_id"] != second["request_id"], \
        "but each planning ask still mints its own fresh session identity"

    sessions = evidence_bridge.all_sessions()
    session_ids = {s["session_id"] for s in sessions}
    assert first["request_id"] in session_ids and second["request_id"] in session_ids
    first_session = evidence_bridge.session_summary(first["request_id"])
    second_session = evidence_bridge.session_summary(second["request_id"])
    assert first_session["session_id"] != second_session["session_id"]
    # both real, both independently reconstructable, both point at the same
    # (correctly) shared proposal content
    assert first_session["planner"]["proposal_id"] == second_session["planner"]["proposal_id"]


def test_events_from_two_different_instances_are_never_cross_deduplicated(tmp_path):
    """The dedup guard is scoped to one recorder instance's own `sequence`
    numbering. A second instance (a restart, or a second bridge sharing this
    evidence root) starts its own `sequence` count at the same low numbers
    -- those are two DIFFERENT real events that must never be collapsed just
    because an unrelated instance already used the same integer."""
    evidence_root = tmp_path / "evidence"
    row_a = {"sequence": 1, "at": "2026-01-01T00:00:00.000", "contract": pp.PROPOSAL_CONTRACT,
            "event_type": pp.PLANNER_STARTED, "request_id": "PLANREQ-instance-a",
            "workflow_id": "W", "anchor_node_id": "N01"}
    row_b = {"sequence": 1, "at": "2026-01-01T00:00:01.000", "contract": pp.PROPOSAL_CONTRACT,
            "event_type": pp.PLANNER_STARTED, "request_id": "PLANREQ-instance-b",
            "workflow_id": "W", "anchor_node_id": "N01"}

    recorder_a = puv.EvidenceRecorder(evidence_root)
    recorder_a.record_planner_events([row_a])
    recorder_b = puv.EvidenceRecorder(evidence_root)  # a fresh instance, e.g. after a restart
    recorder_b.record_planner_events([row_b])

    on_disk = puv._read_jsonl(evidence_root / puv.DECISIONS_FILENAME)
    assert [row["request_id"] for row in on_disk] == ["PLANREQ-instance-a", "PLANREQ-instance-b"]


# ═══════════════════════════ runtime evidence ═══════════════════════════

def test_runtime_evidence_captures_a_pass(evidence_bridge, tmp_path):
    node_script = {"nodes": {"*": {"outcome": "PASS", "verdict": "PASS"}}}
    run_id = _run_and_wait(evidence_bridge, tmp_path, node_script=node_script)
    session = _session_for_run(evidence_bridge, run_id)
    assert session["planner"] is None  # a manual run, no planner ask preceded it
    assert session["session_id"] == run_id  # run_id reused as identity, not invented
    assert session["execution"]["run_status"] == "WAITING_FOR_HUMAN"
    assert session["execution"]["repair_cycles"] == 0
    assert session["execution"]["human_gates"] == 1  # reached N09, not yet resolved


def test_runtime_evidence_captures_a_repair_cycle(evidence_bridge, tmp_path):
    node_script = {"nodes": {
        "N01": {"outcome": "PASS", "verdict": "PASS"},
        "N02": {"outcome": "FAIL", "verdict": "REPAIR", "next_brief": "fix the gap"},
        "N02R": {"outcome": "PASS", "verdict": "PASS"},
    }}
    run_id = _run_and_wait(evidence_bridge, tmp_path, node_script=node_script)
    session = _session_for_run(evidence_bridge, run_id)
    assert session["execution"]["repair_cycles"] == 1

    state = runner.STATS_ROOT / run_id / "WORKFLOW" / "workflow_state.json"
    completed = json.loads(state.read_text(encoding="utf-8"))["completed_nodes"]
    verdicts = {row["node_id"]: row.get("verdict") for row in completed}
    assert verdicts["N02"] == "REPAIR"           # the original failing attempt
    repaired = [row for row in completed if row.get("lineage")]
    assert repaired and repaired[0]["verdict"] == "PASS"  # the repair descendant
    # both the original failure and its repair remain visible in the evidence


def test_runtime_evidence_captures_a_blocked_verdict(evidence_bridge, tmp_path):
    node_script = {"nodes": {
        "N01": {"outcome": "PASS", "verdict": "PASS"},
        "N02": {"outcome": "BLOCKED", "verdict": "BLOCKED"},
    }}
    run_id = _run_and_wait(evidence_bridge, tmp_path, node_script=node_script)
    state = runner.STATS_ROOT / run_id / "WORKFLOW" / "workflow_state.json"
    completed = json.loads(state.read_text(encoding="utf-8"))["completed_nodes"]
    assert next(row for row in completed if row["node_id"] == "N02")["verdict"] == "BLOCKED"
    session = _session_for_run(evidence_bridge, run_id)
    assert session["execution"]["run_id"] == run_id


def test_cancellation_evidence(evidence_bridge, tmp_path):
    node_script = {"delay_seconds": 5.0, "nodes": {"*": {"outcome": "PASS", "verdict": "PASS"}}}
    repo, worktree = _workspace(tmp_path / "ws")
    adapter = aaw_llm_test_adapter.scripted_adapter(node_script)
    handle = evidence_bridge.start_run(WORKFLOW_ID, goal="cancel me", repo=repo,
                                       worktree=worktree, adapter=adapter)
    run_id = handle["run_id"]
    evidence_bridge.cancel_run(run_id, join_timeout=20.0)
    session = _session_for_run(evidence_bridge, run_id)
    assert session["execution"]["run_status"] == "CANCELLED"
    assert session["execution"]["final_outcome"] == "CANCELLED"


# ═══════════════════════════ operator feedback ═══════════════════════════

def test_feedback_attach_and_update_supersedes(evidence_bridge):
    frame = _plan(evidence_bridge, SLICE)
    session_id = frame["request_id"]

    first = evidence_bridge.record_operator_feedback(session_id, "PARTIAL", comment="ok-ish")
    session = evidence_bridge.session_summary(session_id)
    assert session["operator_feedback"] == {
        "usefulness": "PARTIAL", "reuse_intent": None, "comment": "ok-ish",
        "recorded_at": session["operator_feedback"]["recorded_at"],
    }

    second = evidence_bridge.record_operator_feedback(
        session_id, "USEFUL", reuse_intent="YES", comment="actually great")
    assert second["supersedes"] == first["feedback_id"]
    updated = evidence_bridge.session_summary(session_id)
    assert updated["operator_feedback"]["usefulness"] == "USEFUL"
    assert updated["operator_feedback"]["reuse_intent"] == "YES"

    # both rows are still on disk, append-only -- nothing was overwritten in place
    rows = puv._read_jsonl(evidence_bridge.evidence.feedback_path)
    assert [row["session_id"] for row in rows] == [session_id, session_id]


def test_a_session_with_no_feedback_projects_cleanly(evidence_bridge):
    frame = _plan(evidence_bridge, SLICE)
    session = evidence_bridge.session_summary(frame["request_id"])
    assert session["operator_feedback"] is None


def test_feedback_rejects_an_invalid_usefulness_value(evidence_bridge):
    frame = _plan(evidence_bridge, SLICE)
    with pytest.raises(ValueError):
        evidence_bridge.record_operator_feedback(frame["request_id"], "GREAT")


# ═══════════════════════ pure projection unit tests ═══════════════════════

def _write_decision(evidence_root, **fields):
    row = {"sequence": 1, "at": "2026-01-01T00:00:00", "contract": pp.PROPOSAL_CONTRACT}
    row.update(fields)
    puv._append_jsonl(evidence_root / puv.DECISIONS_FILENAME, row)


def test_token_capture_known_values_flow_through(tmp_path):
    evidence_root, stats_root = tmp_path / "evidence", tmp_path / "stats"
    run_id = "AAW_RUN_KNOWN_TOKENS"
    state_dir = stats_root / run_id / "WORKFLOW"
    state_dir.mkdir(parents=True)
    (state_dir / "workflow_state.json").write_text(json.dumps({
        "status": "COMPLETED", "final_outcome": "COMPLETED",
        "workflow_summary": {"nodes_completed": 3, "repair_cycles": 0,
                             "input_tokens_total": 1234, "output_tokens_total": 456,
                             "wall_time_total": 12.5},
    }), encoding="utf-8")
    puv._append_jsonl(evidence_root / puv.LINKS_FILENAME, {
        "session_id": run_id, "proposal_id": None, "workflow_id": "W", "run_id": run_id,
        "linked_at": "2026-01-01T00:00:00", "post_accept_manual_edit": None,
    })
    session = puv.session_summary(run_id, evidence_root=evidence_root, stats_root=stats_root)
    assert session["resources"]["input_tokens"] == 1234
    assert session["resources"]["output_tokens"] == 456
    assert session["resources"]["known_cost"] is None  # never fabricated


def test_token_capture_missing_values_stay_null_not_zero(tmp_path):
    evidence_root, stats_root = tmp_path / "evidence", tmp_path / "stats"
    run_id = "AAW_RUN_NO_TOKENS"
    state_dir = stats_root / run_id / "WORKFLOW"
    state_dir.mkdir(parents=True)
    (state_dir / "workflow_state.json").write_text(json.dumps({
        "status": "COMPLETED", "final_outcome": "COMPLETED",
        "workflow_summary": {"nodes_completed": 1, "repair_cycles": 0,
                             "input_tokens_total": None, "output_tokens_total": None,
                             "wall_time_total": 1.0},
    }), encoding="utf-8")
    puv._append_jsonl(evidence_root / puv.LINKS_FILENAME, {
        "session_id": run_id, "proposal_id": None, "workflow_id": "W", "run_id": run_id,
        "linked_at": "2026-01-01T00:00:00", "post_accept_manual_edit": None,
    })
    session = puv.session_summary(run_id, evidence_root=evidence_root, stats_root=stats_root)
    assert session["resources"]["input_tokens"] is None
    assert session["resources"]["output_tokens"] is None


def test_aggregate_report_excludes_undecided_and_incomplete_sessions(tmp_path):
    evidence_root = tmp_path / "evidence"
    # one session that never got an operator decision -- must not count as a
    # reject or an accept
    _write_decision(evidence_root, event_type=pp.PLANNER_STARTED, request_id="REQ1",
                    workflow_id="W", anchor_node_id="N01", instruction_chars=10)
    _write_decision(evidence_root, event_type=pp.PROPOSAL_READY, request_id="REQ1",
                    workflow_id="W", proposal_id="PROP1", node_count=1, edge_count=1)
    # one session that was accepted and rejected... i.e. a clean accept
    _write_decision(evidence_root, event_type=pp.PLANNER_STARTED, request_id="REQ2",
                    workflow_id="W", anchor_node_id="N01", instruction_chars=10)
    _write_decision(evidence_root, event_type=pp.PROPOSAL_READY, request_id="REQ2",
                    workflow_id="W", proposal_id="PROP2", node_count=1, edge_count=1)
    _write_decision(evidence_root, event_type=pp.PROPOSAL_ACCEPTED, proposal_id="PROP2")

    report = puv.aggregate_report(evidence_root=evidence_root, stats_root=tmp_path / "stats")
    assert report["session_count"] == 2
    assert report["planner_decided_sessions"] == 1       # REQ1 excluded: no decision yet
    assert report["planner_accept_rate"] == 1.0           # 1/1 decided, not 1/2
    assert report["runs"] == 0
    assert report["completed_run_rate"] is None            # no denominator -> None, not 0


def test_aggregate_report_is_deterministic_on_rebuild(evidence_bridge, tmp_path):
    frame = _plan(evidence_bridge, SLICE)
    evidence_bridge.accept_proposal(frame["proposal"]["proposal_id"], persist=True)
    node_script = {"nodes": {"*": {"outcome": "PASS", "verdict": "PASS"}}}
    _run_and_wait(evidence_bridge, tmp_path, node_script=node_script)
    evidence_bridge.record_operator_feedback(frame["request_id"], "USEFUL", reuse_intent="YES")

    first = evidence_bridge.evidence_report()
    second = evidence_bridge.evidence_report()  # nothing derived/cached to go stale
    assert first == second

    # and a session index rebuilt from a brand-new reader over the same files
    # (no live bridge, no in-memory state at all) reproduces it identically
    rebuilt = puv.aggregate_report(evidence_root=evidence_bridge.evidence.root,
                                   stats_root=evidence_bridge.stats_root)
    assert rebuilt == first


def test_evidence_survives_a_simulated_bridge_restart(evidence_bridge, tmp_path):
    frame = _plan(evidence_bridge, SLICE)
    session_id = frame["request_id"]
    evidence_bridge.accept_proposal(frame["proposal"]["proposal_id"], persist=True)
    node_script = {"nodes": {"*": {"outcome": "PASS", "verdict": "PASS"}}}
    run_id = _run_and_wait(evidence_bridge, tmp_path, node_script=node_script)
    before = evidence_bridge.session_summary(session_id)

    # simulate a restart: fresh reader, no shared in-memory state with the
    # bridge above at all -- only the files on disk
    after = puv.session_summary(session_id, evidence_root=evidence_bridge.evidence.root,
                                stats_root=evidence_bridge.stats_root)
    assert after == before
    assert after["execution"]["run_id"] == run_id
