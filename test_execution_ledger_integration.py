"""AAW V0.4B ledger integration tests against the real runners.

These exercise the actual dispatch boundaries rather than the ledger in
isolation: that intent precedes dispatch, that a real child process produces a
real start observation, that machine gates and preprocess reuse the generic
execution events, and that the ledger is not indexed as execution telemetry.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import custom_job_runner
import execution_ledger as L
import local_preprocess
import process_observation
import workflow_runner


def _git(argv, cwd):
    subprocess.run(argv, cwd=cwd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


@pytest.fixture()
def repo_and_worktree(tmp_path):
    repo, worktree = tmp_path / "main", tmp_path / "worktree"
    repo.mkdir()
    _git(["git", "init", "-b", "main"], repo)
    _git(["git", "config", "user.email", "aaw@example.invalid"], repo)
    _git(["git", "config", "user.name", "AAW Test"], repo)
    (repo / "README.md").write_text("baseline\n", encoding="utf-8")
    _git(["git", "add", "."], repo)
    _git(["git", "commit", "-m", "baseline"], repo)
    _git(["git", "worktree", "add", "-b", "aaw/ledger-test", str(worktree)], repo)
    return repo, worktree


def _job(repo: Path, worktree: Path, subtasks: int = 2) -> dict:
    return {
        "schema_version": "AAW_CUSTOM_JOB_V0.3", "job_id": "ledger-test", "job_type": "MULTI_SUBTASK",
        "goal": "ledger fixture", "repository": str(repo), "worktree": str(worktree),
        "preprocess_policy": "OFF", "preprocess": {},
        "worktree_policy": {"isolated_worktree_required": True, "checkpoint_commits": True,
                            "main_merge_allowed": False, "push_allowed": False},
        "execution_adapter": "DIRECT_CLI_CONTROL", "binding_source": "HUMAN_OVERRIDE", "plan": {"enabled": False},
        "subtasks": [{"subtask_id": f"S{i}", "title": f"file {i}", "instructions": f"create file {i}",
                      "profile_id": "TERRA_HIGH",
                      "machine_gates": [{"command": [sys.executable, "-c",
                                                     f"from pathlib import Path; assert Path('S{i}.txt').is_file()"],
                                         "timeout_seconds": 30}]} for i in range(1, subtasks + 1)],
        "machine_gates": {"final": [{"command": [sys.executable, "-c", "raise SystemExit(0)"], "timeout_seconds": 30}]},
        "review": {"profile_id": "SOL_HIGH"},
        "repair": {"profile_id": "LUNA_HIGH", "selection_mode": "HUMAN_SELECTED", "max_cycles": 1},
        "delta_review": {"profile_id": "LUNA_HIGH"},
        "limits": {"max_subtasks": 10, "max_llm_calls": 10, "max_wall_time_minutes": 30},
    }


def _mock_adapter(role, package, worktree, binding):
    if role == "SUBTASK":
        (worktree / f"{package['SUBTASK_ID']}.txt").write_text("x\n", encoding="utf-8")
    result = {"outcome": "PASS", "summary": f"{role} pass", "changed_files": [], "tests": [], "findings": [],
              "remaining_uncertainty": [], "recommended_next_action": "continue", "provider_session": f"s-{role}"}
    telemetry = {"node_type": role, "model": binding["runtime_model_id"], "effort": binding["effort"],
                 "harness": binding["harness"], "provider": binding["provider"],
                 "access_class": binding["access_class"], "binding_source": binding["binding_source"],
                 "provider_session": f"s-{role}", "wall_time_s": 0.01,
                 "input_tokens": 1, "cached_input": 0, "output_tokens": 1, "reasoning_tokens": 0}
    return result, telemetry


@pytest.fixture()
def custom_job_run(tmp_path, repo_and_worktree, monkeypatch):
    repo, worktree = repo_and_worktree
    stats = tmp_path / "03_STATS"
    monkeypatch.setattr(custom_job_runner, "STATS_ROOT", stats)
    job_path = tmp_path / "job.json"
    job_path.write_text(json.dumps(_job(repo, worktree)), encoding="utf-8")
    state = custom_job_runner.execute(job_path, _mock_adapter)
    assert state["status"] == "WAITING_FOR_HUMAN", state.get("stop_reason")
    return state, L.ExecutionLedger.for_run(state["AAW_RUN_ID"], stats), stats


# ---------------------------------------------------------------------------
# Custom Job lifecycle
# ---------------------------------------------------------------------------

def test_every_execution_has_durable_intent_and_the_ledger_validates(custom_job_run):
    state, ledger, _ = custom_job_run
    intents = {row["execution_id"] for row in ledger.read().by_type(L.EXECUTION_INTENT)}
    assert intents == {row["execution_id"] for row in state["executions"]}
    report = L.validate_ledger(ledger.path, state["AAW_RUN_ID"])
    assert report["valid"], report["errors"]
    assert ledger.unresolved() == []


def test_intent_is_appended_before_any_start_or_close_for_its_execution(custom_job_run):
    _, ledger, _ = custom_job_run
    order = {}
    for row in ledger.read().events:
        if row.get("execution_id"):
            order.setdefault(row["execution_id"], []).append((row["sequence"], row["event_type"]))
    assert order
    for execution_id, rows in order.items():
        sequences = [sequence for sequence, _ in rows]
        assert sequences == sorted(sequences)
        assert rows[0][1] == L.EXECUTION_INTENT, f"{execution_id} has a non-intent first event"


def test_only_the_five_event_types_exist(custom_job_run):
    _, ledger, _ = custom_job_run
    kinds = {row["event_type"] for row in ledger.read().events}
    assert kinds <= set(L.EVENT_TYPES)
    assert not kinds & {"RUN_STARTED", "NODE_STARTED", "MACHINE_GATE", "PREPROCESS_FINISHED",
                        "REVIEW_FINISHED", "FINDING_RECORDED", "STATE_CHANGED", "MODEL_BOUND"}


def test_machine_gates_use_generic_execution_events_with_a_real_process_receipt(custom_job_run):
    state, ledger, _ = custom_job_run
    gate_ids = {row["execution_id"] for row in state["executions"] if row["invocation_kind"] == "MACHINE_GATE"}
    assert len(gate_ids) == 3, "two subtask gates plus one final gate"
    started = {row["execution_id"]: row["payload"] for row in ledger.read().by_type(L.EXECUTION_STARTED)}
    for execution_id in gate_ids:
        receipt = started[execution_id]
        assert receipt["start_evidence"] == "CHILD_PROCESS_SPAWNED"
        assert isinstance(receipt["process_id"], int) and receipt["process_id"] > 0
        assert ledger.close_status(execution_id)["observation_source"] == "CHILD_PROCESS_EXIT"
        assert ledger.close_status(execution_id)["exit_code"] == 0


def test_an_adapter_that_owns_no_process_records_no_fabricated_start(custom_job_run):
    state, ledger, _ = custom_job_run
    llm_ids = {row["execution_id"] for row in state["executions"] if row["invocation_kind"] != "MACHINE_GATE"}
    started = {row["execution_id"] for row in ledger.read().by_type(L.EXECUTION_STARTED)}
    assert not (llm_ids & started), "the in-process fixture adapter spawns nothing, so nothing is claimed"
    for execution_id in llm_ids:
        assert ledger.close_status(execution_id)["observation_source"] == "IN_PROCESS_ADAPTER_RETURN"
        assert ledger.close_status(execution_id)["start_evidence_observed"] is False


def test_a_git_helper_spawned_by_an_adapter_is_not_recorded_as_the_start(tmp_path, repo_and_worktree, monkeypatch):
    """Regression: only the marked dispatch is start evidence, never a helper call."""
    repo, worktree = repo_and_worktree
    stats = tmp_path / "03_STATS"
    monkeypatch.setattr(custom_job_runner, "STATS_ROOT", stats)

    def adapter_that_calls_git(role, package, worktree_path, binding):
        # A real `git` child process spawned while the adapter assembles its result.
        custom_job_runner.git(Path(worktree_path), "rev-parse", "HEAD")
        return _mock_adapter(role, package, Path(worktree_path), binding)

    job = _job(repo, worktree, subtasks=1)
    job["subtasks"][0]["machine_gates"] = []
    job["machine_gates"]["final"] = []
    job_path = tmp_path / "job.json"
    job_path.write_text(json.dumps(job), encoding="utf-8")
    state = custom_job_runner.execute(job_path, adapter_that_calls_git)
    assert state["status"] == "WAITING_FOR_HUMAN", state.get("stop_reason")
    ledger = L.ExecutionLedger.for_run(state["AAW_RUN_ID"], stats)
    assert ledger.read().by_type(L.EXECUTION_STARTED) == [], "a git helper is not the invocation's start"
    assert L.validate_ledger(ledger.path, state["AAW_RUN_ID"])["valid"]
    for row in state["executions"]:
        assert ledger.close_status(row["execution_id"])["observation_source"] == "IN_PROCESS_ADAPTER_RETURN"


def test_commit_records_carry_producer_executions_and_repository_scoped_identity(custom_job_run):
    state, ledger, _ = custom_job_run
    events = ledger.read().by_type(L.COMMIT_RECORDED)
    assert len(events) == len(state["checkpoint_commits"]) == 2
    for payload in (row["payload"] for row in events):
        assert payload["commit_identity"]["repository"] == state["repository"]
        assert payload["commit_hash"] in state["checkpoint_commits"]
        assert payload["producer_execution_ids"]
        assert payload["role"] == "SUBTASK" and payload["subtask_id"]
        assert payload["git_evidence"]["expected_parent"] == payload["expected_parent"]


def test_human_decision_is_recorded_only_after_the_artifact_exists(custom_job_run, monkeypatch):
    state, ledger, stats = custom_job_run
    monkeypatch.setattr(custom_job_runner, "STATS_ROOT", stats)
    assert ledger.human_decisions() == [], "no decision before a human acts"
    decided = custom_job_runner.human_verdict(state["AAW_RUN_ID"], "leave-for-later")
    recorded = ledger.human_decisions()
    assert len(recorded) == 1
    artifact = Path(decided["human_decisions"][-1]["artifact_path"])
    assert artifact.is_file()
    assert recorded[0]["candidate_id"] == state["candidate"]["candidate_id"]
    assert recorded[0]["decision_artifact_hash"] == L.file_hash(artifact)
    assert recorded[0]["verdict"] == "LEFT_FOR_LATER"
    assert L.validate_ledger(ledger.path, state["AAW_RUN_ID"])["valid"]


def test_state_carries_lifecycle_records_without_becoming_the_authority(custom_job_run):
    state, ledger, _ = custom_job_run
    records = state["lifecycle_records"]
    assert len(records) == len(state["executions"])
    assert all(row["requires_reconciliation"] is False for row in records)
    assert state["ledger_schema_version"] == L.SCHEMA_VERSION
    assert Path(state["ledger_path"]) == ledger.path


def test_a_failed_machine_gate_still_produces_a_terminal_fact(tmp_path, repo_and_worktree, monkeypatch):
    repo, worktree = repo_and_worktree
    stats = tmp_path / "03_STATS"
    monkeypatch.setattr(custom_job_runner, "STATS_ROOT", stats)
    job = _job(repo, worktree, subtasks=1)
    job["subtasks"][0]["machine_gates"] = [{"command": [sys.executable, "-c", "raise SystemExit(3)"],
                                            "timeout_seconds": 30}]
    job_path = tmp_path / "job.json"
    job_path.write_text(json.dumps(job), encoding="utf-8")
    state = custom_job_runner.execute(job_path, _mock_adapter)
    assert state["status"] == "FAIL"
    ledger = L.ExecutionLedger.for_run(state["AAW_RUN_ID"], stats)
    gate = next(row for row in state["executions"] if row["invocation_kind"] == "MACHINE_GATE")
    close = ledger.close_status(gate["execution_id"])
    assert close["close_reason"] == "FAILED" and close["exit_code"] == 3
    assert close["effect_certainty"] == "CONFIRMED"
    assert L.validate_ledger(ledger.path, state["AAW_RUN_ID"])["valid"]


# ---------------------------------------------------------------------------
# Workflow runner
# ---------------------------------------------------------------------------

def test_workflow_machine_gate_records_the_full_lifecycle(tmp_path, repo_and_worktree, monkeypatch):
    repo, worktree = repo_and_worktree
    stats = tmp_path / "03_STATS"
    monkeypatch.setattr(workflow_runner, "STATS_ROOT", stats)
    workflow = {
        "workflow_id": "LEDGER_E2E", "version": "0.4B", "description": "ledger fixture", "goal": None,
        "start_node": "N02",
        "workspace_policy": {"isolated_worktree_required": True, "main_merge_allowed": False},
        "limits": {"max_nodes": 4, "max_repair_cycles": 1, "max_wall_time_minutes": 5,
                   "max_llm_calls": 3, "max_token_budget": None},
        "nodes": [
            {"id": "N02", "type": "MACHINE_GATE", "depends_on": [], "run_if": "ALWAYS", "role": None,
             "model": None, "effort": None, "instructions": "fixture", "acceptance": ["exit zero"],
             "command": [sys.executable, "-c", "raise SystemExit(0)"], "timeout_seconds": 30,
             "on_pass": "N05", "on_fail": "STOP"},
            {"id": "N05", "type": "HUMAN_GATE", "depends_on": ["N02"], "run_if": "ON_TRANSITION", "role": None,
             "model": None, "effort": None, "instructions": "fixture", "acceptance": ["human"],
             "on_pass": "STOP", "on_fail": "STOP"},
        ],
    }
    workflow_path = tmp_path / "workflow.json"
    workflow_path.write_text(json.dumps(workflow), encoding="utf-8")
    state = workflow_runner.execute(workflow_path, "ledger fixture", repo, worktree, preprocess_policy="OFF")
    assert state["status"] == "WAITING_FOR_HUMAN", state.get("stop_reason")
    ledger = L.ExecutionLedger.for_run(state["AAW_RUN_ID"], stats)
    execution_id = state["executions"][0]["execution_id"]
    types = [row["event_type"] for row in ledger.read().events if row.get("execution_id") == execution_id]
    assert types == [L.EXECUTION_INTENT, L.EXECUTION_STARTED, L.EXECUTION_CLOSED]
    assert ledger.close_status(execution_id)["close_reason"] == "COMPLETED"

    decided = workflow_runner.apply_human_verdict(state["AAW_RUN_ID"], "leave-for-later")
    assert len(ledger.human_decisions()) == 1
    assert ledger.human_decisions()[0]["human_decision_id"] == decided["human_decisions"][-1]["human_decision_id"]
    assert L.validate_ledger(ledger.path, state["AAW_RUN_ID"])["valid"]


# ---------------------------------------------------------------------------
# Preprocess
# ---------------------------------------------------------------------------

def test_skipped_preprocess_creates_no_execution_and_no_ledger_event(tmp_path):
    artifact_root = tmp_path / "run" / "PREPROCESS"
    result = local_preprocess.preprocess_for_node(
        run_id="RUN_PRE", node_id="N01", node_type="IMPLEMENT", package={"GOAL": "x"},
        artifact_root=artifact_root, policy="OFF", downstream_execution_id="EXE_" + "d" * 32,
        execution_root=tmp_path / "run" / "EXECUTIONS")
    assert result["status"] == "SKIPPED_POLICY_OFF"
    assert result["execution_id"] is None and result["preprocess_id"].startswith("PRE_")
    ledger = L.ExecutionLedger(L.ledger_beside_descriptors(tmp_path / "run" / "EXECUTIONS"), "RUN_PRE")
    assert ledger.read().events == [], "a skipped decision is not an execution"


def test_invoked_preprocess_uses_generic_execution_events(tmp_path, monkeypatch):
    """An invoked preprocess is an execution, so it takes INTENT/STARTED/CLOSED."""
    monkeypatch.setattr(local_preprocess.local_llm, "precheck_soft", lambda **_k: {"ok": True})
    monkeypatch.setattr(local_preprocess.local_llm, "chat_json",
                        lambda *_a, **_k: {"json": {"summary": "ok"}, "content": "ok", "usage": {}})
    artifact_root = tmp_path / "run" / "PREPROCESS"
    execution_root = tmp_path / "run" / "EXECUTIONS"
    result = local_preprocess.preprocess_for_node(
        run_id="RUN_PRE", node_id="N07", node_type="REVIEW",
        package={"GIT_DIFF": "diff --git a b\n" * 50, "GOAL": "review"},
        artifact_root=artifact_root, policy="MANUAL", requested_type="LOCAL_QWEN_DIFF_TRIAGE",
        downstream_execution_id="EXE_" + "d" * 32, execution_root=execution_root)
    assert result["status"] == "COMPLETED_ADVISORY"
    ledger = L.ExecutionLedger(L.ledger_beside_descriptors(execution_root), "RUN_PRE")
    events = ledger.read().events
    assert [row["event_type"] for row in events] == [L.EXECUTION_INTENT, L.EXECUTION_STARTED, L.EXECUTION_CLOSED]
    assert {row["execution_id"] for row in events} == {result["execution_id"]}
    assert events[0]["payload"]["invocation_kind"] == "PREPROCESS"
    assert events[0]["payload"]["preprocess_id"] == result["preprocess_id"]
    start = events[1]["payload"]
    # AAW owns no process for local HTTP inference and does not pretend otherwise.
    assert start["start_evidence"] == "HTTP_REQUEST_DISPATCH_INITIATED"
    assert start["process_id"] is None
    assert L.validate_ledger(ledger.path, "RUN_PRE")["valid"]


def test_preprocess_fails_closed_when_intent_cannot_be_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(local_preprocess.local_llm, "precheck_soft", lambda **_k: {"ok": True})
    called: list[int] = []
    monkeypatch.setattr(local_preprocess.local_llm, "chat_json",
                        lambda *_a, **_k: (called.append(1), {"json": {}})[1])
    monkeypatch.setattr(L.os, "write", lambda *_a, **_k: (_ for _ in ()).throw(OSError("disk full")))
    result = local_preprocess.preprocess_for_node(
        run_id="RUN_PRE", node_id="N07", node_type="REVIEW",
        package={"GIT_DIFF": "diff\n" * 50, "GOAL": "review"},
        artifact_root=tmp_path / "run" / "PREPROCESS", policy="MANUAL", required=True,
        requested_type="LOCAL_QWEN_DIFF_TRIAGE", execution_root=tmp_path / "run" / "EXECUTIONS")
    assert called == [], "no inference is dispatched without durable intent"
    assert result["status"] == "BLOCKED"
    assert result["failure_code"] == "LEDGER_WRITE_ERROR"


# ---------------------------------------------------------------------------
# Analytics boundary
# ---------------------------------------------------------------------------

def test_analytics_indexes_the_ledger_as_lifecycle_evidence_only(custom_job_run):
    """V0.4C boundary: the ledger IS indexed, but only as lifecycle evidence.

    Superseding the V0.4B analytics boundary, Analytics V2 reads the ledger
    through this module's own reader and derives ``execution_lifecycle`` rows
    from it. What has not changed is the authority rule: a ledger event never
    becomes execution telemetry and never carries execution identity.
    """
    from CONTROL_CENTER.ANALYTICS import aaw_analytics
    state, ledger, stats = custom_job_run
    assert aaw_analytics.classify(ledger.path, stats) == ("LEDGER", "LEDGER")
    database = stats.parent / "index.sqlite"
    aaw_analytics.ingest(db_path=database, stats_root=stats, rebuild=True)
    import sqlite3

    connection = sqlite3.connect(database)
    try:
        row = connection.execute(
            "SELECT layout, status, source_type FROM source_file WHERE path=?",
            (str(ledger.path),)).fetchone()
        assert row is not None, "the ledger is recognised, not silently ignored"
        assert row[0] == "LEDGER" and row[1] == "INDEXED" and row[2] == "LEDGER"
        indexed = connection.execute(
            "SELECT COUNT(*) FROM execution e JOIN source_file s ON s.id=e.source_id "
            "WHERE s.layout='LEDGER'").fetchone()[0]
        assert indexed == 0, "ledger events never become execution telemetry rows"
        lifecycle = connection.execute(
            "SELECT COUNT(*) FROM execution_lifecycle l JOIN source_file s ON s.id=l.source_id "
            "WHERE s.layout='LEDGER'").fetchone()[0]
        assert lifecycle > 0, "the ledger supplies observed lifecycle, and only that"
        # the descriptor, not the ledger, remains identity authority
        authorities = {row[0] for row in connection.execute(
            "SELECT DISTINCT identity_authority FROM execution WHERE execution_id IS NOT NULL")}
        assert authorities == {"EXECUTION_DESCRIPTOR"}
        unknown = connection.execute(
            "SELECT COUNT(*) FROM ingest_diagnostic WHERE kind='UNKNOWN_LAYOUT' AND path LIKE ?",
            (f"%{L.LEDGER_DIRNAME}%",)).fetchone()[0]
        assert unknown == 0
        # a close observation is kept apart from the semantic node verdict
        observed = connection.execute(
            "SELECT COUNT(*) FROM execution_lifecycle WHERE ledger_outcome IS NOT NULL").fetchone()[0]
        assert observed > 0
    finally:
        connection.close()
