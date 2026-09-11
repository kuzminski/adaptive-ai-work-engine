"""Tests for AAW Analytics V2 — the execution-grain evidence index.

Covers ingestion, the false-join regression suite (§34 A-N of the V0.4C task),
fixture exclusion, damaged-ledger ingestion, duplicate-identity quarantine,
deterministic rebuild, idempotent refresh, retained query-API surface and a
non-destructive rebuild against the real 03_STATS.

Never writes to 03_STATS and never touches the published aaw_analytics.sqlite.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import aaw_analytics as A  # noqa: E402


# ---------------------------------------------------------------------------
# Synthetic evidence builders
# ---------------------------------------------------------------------------

def _ids(run_id: str) -> dict[str, str]:
    """Per-run execution identities. Real IDs are UUID-backed and never reused."""
    return {key: A._exe(f"{run_id}:{key}")
            for key in ("A", "B", "GATE", "R1", "R2", "REP", "DEL")}


_V2 = _ids("AAW_V2")
EXEC_A = _V2["A"]             # node S1, first invocation
EXEC_B = _V2["B"]             # node S1, second invocation (a genuine retry)
EXEC_GATE = _V2["GATE"]
EXEC_REVIEW_1 = _V2["R1"]
EXEC_REVIEW_2 = _V2["R2"]
EXEC_REPAIR = _V2["REP"]
EXEC_DELTA = _V2["DEL"]

SHARED_SESSION = "01a06d44-d6fb-7c00-9f4e-6600a714ae1d"
SHARED_TIME = "2026-09-06T20:00:00.000+02:00"
REPO = r"D:\product\repo"


def _modern_run(root: Path, run_id: str = "AAW_V2") -> Path:
    """A V0.4A custom job: one logical node invoked twice, review/repair/delta."""
    ids = _ids(run_id)
    EXEC_A, EXEC_B, EXEC_GATE = ids["A"], ids["B"], ids["GATE"]
    EXEC_REVIEW_1, EXEC_REVIEW_2 = ids["R1"], ids["R2"]
    EXEC_REPAIR, EXEC_DELTA = ids["REP"], ids["DEL"]
    run = root / run_id
    (run / "CUSTOM_JOB").mkdir(parents=True)
    (run / "PREPROCESS").mkdir(parents=True)

    # Two executions of ONE logical node, identical provider session and
    # identical created_at: only execution_id separates them.
    A.write_descriptor(run, execution_id=EXEC_A, node_id="S1", subtask_id="S1",
                       invocation_kind="LLM", model="gpt-5.6-terra", effort="high",
                       provider_session_id=SHARED_SESSION, created_at=SHARED_TIME)
    A.write_descriptor(run, execution_id=EXEC_B, node_id="S1", subtask_id="S1",
                       invocation_kind="LLM", model="gpt-5.6-terra", effort="high",
                       provider_session_id=SHARED_SESSION, created_at=SHARED_TIME,
                       retry_of=EXEC_A)
    A.write_descriptor(run, execution_id=EXEC_GATE, node_id="S1:GATE:1", subtask_id="S1",
                       invocation_kind="MACHINE_GATE", provider="LOCAL", harness="subprocess",
                       created_at=SHARED_TIME)
    # Two review executions, each raising its own F001.
    A.write_descriptor(run, execution_id=EXEC_REVIEW_1, node_id="REVIEW",
                       invocation_kind="REVIEW", model="gpt-5.6-sol", effort="high",
                       created_at=SHARED_TIME,
                       relations={"reviewed_execution_ids": [EXEC_A],
                                  "reviewed_base": "base", "reviewed_head": "head1"})
    A.write_descriptor(run, execution_id=EXEC_REVIEW_2, node_id="REVIEW",
                       invocation_kind="REVIEW", model="gpt-5.6-sol", effort="high",
                       created_at=SHARED_TIME,
                       relations={"reviewed_execution_ids": [EXEC_B],
                                  "reviewed_base": "base", "reviewed_head": "head2"})
    # REPAIR is not a retry: it names its originating review and finding keys.
    A.write_descriptor(run, execution_id=EXEC_REPAIR, node_id="S1",
                       invocation_kind="REPAIR", subtask_id="S1", model="gpt-5.6-luna",
                       effort="high", created_at=SHARED_TIME,
                       relations={"originating_review_execution_id": EXEC_REVIEW_2,
                                  "selected_finding_keys": [
                                      {"review_execution_id": EXEC_REVIEW_2,
                                       "finding_id": "F001"}]})
    A.write_descriptor(run, execution_id=EXEC_DELTA, node_id="DELTA_REVIEW",
                       invocation_kind="DELTA_REVIEW", model="gpt-5.6-luna", effort="high",
                       created_at=SHARED_TIME,
                       relations={"original_review_execution_id": EXEC_REVIEW_2,
                                  "repair_execution_id": EXEC_REPAIR,
                                  "finding_dispositions": [
                                      {"finding_key": {"review_execution_id": EXEC_REVIEW_2,
                                                       "finding_id": "F001"},
                                       "disposition": "RESOLVED"}]})

    (run / "CUSTOM_JOB" / "job_state.json").write_text(json.dumps({
        "schema_version": "AAW_CUSTOM_JOB_RUN_V0.4A", "AAW_RUN_ID": run_id,
        "job_id": "product-work", "job_type": "MULTI_SUBTASK", "goal": "one node twice",
        "status": "WAITING_FOR_HUMAN", "final_acceptance": "WAITING_FOR_HUMAN",
        "started_at": SHARED_TIME, "updated_at": "2026-09-06T20:10:00+02:00",
        "repository": REPO, "worktree": r"D:\product\wt", "review_findings_count": 1,
        "executions": [
            {"execution_id": EXEC_A, "node_id": "S1", "subtask_id": "S1",
             "invocation_kind": "LLM", "model": "gpt-5.6-terra", "effort": "high"},
            {"execution_id": EXEC_B, "node_id": "S1", "subtask_id": "S1",
             "invocation_kind": "LLM", "model": "gpt-5.6-terra", "effort": "high"},
            {"execution_id": EXEC_GATE, "node_id": "S1:GATE:1", "subtask_id": "S1",
             "invocation_kind": "MACHINE_GATE"},
            {"execution_id": EXEC_REVIEW_1, "node_id": "REVIEW", "invocation_kind": "REVIEW"},
            {"execution_id": EXEC_REVIEW_2, "node_id": "REVIEW", "invocation_kind": "REVIEW"},
            {"execution_id": EXEC_REPAIR, "node_id": "S1", "invocation_kind": "REPAIR"},
            {"execution_id": EXEC_DELTA, "node_id": "DELTA_REVIEW",
             "invocation_kind": "DELTA_REVIEW"},
        ],
        "telemetry": [
            {"node_type": "SUBTASK", "execution_id": EXEC_A, "node_id": "S1", "subtask_id": "S1",
             "subtask_index": 1, "model": "gpt-5.6-terra", "effort": "high", "harness": "codex",
             "provider_session": SHARED_SESSION, "started_at": SHARED_TIME, "wall_time_s": 40.0,
             "input_tokens": 100, "cached_input": 50, "output_tokens": 10, "reasoning_tokens": 1},
            {"node_type": "SUBTASK", "execution_id": EXEC_B, "node_id": "S1", "subtask_id": "S1",
             "subtask_index": 1, "model": "gpt-5.6-terra", "effort": "high", "harness": "codex",
             "provider_session": SHARED_SESSION, "started_at": SHARED_TIME, "wall_time_s": 60.0,
             "input_tokens": 200, "cached_input": 60, "output_tokens": 20, "reasoning_tokens": 2},
            {"node_type": "REVIEW", "execution_id": EXEC_REVIEW_1, "node_id": "REVIEW",
             "model": "gpt-5.6-sol", "effort": "high", "harness": "codex",
             "started_at": SHARED_TIME, "wall_time_s": 30.0},
            {"node_type": "REVIEW", "execution_id": EXEC_REVIEW_2, "node_id": "REVIEW",
             "model": "gpt-5.6-sol", "effort": "high", "harness": "codex",
             "started_at": SHARED_TIME, "wall_time_s": 35.0},
        ],
        "subtask_results": [
            {"outcome": "PASS", "subtask_id": "S1", "subtask_index": 1, "execution_id": EXEC_B,
             "machine_gate_result": "PASS", "checkpoint_commit": f"aaa111{run_id}",
             "checkpoint_commit_record": {
                 "repository": REPO, "commit_hash": f"aaa111{run_id}", "subtask_id": "S1",
                 "producer_execution_ids": [EXEC_B], "expected_parent": "base",
                 "role": "SUBTASK"}},
        ],
        "machine_gates": [
            {"command": ["python"], "result": "PASS", "returncode": 0, "wall_time_s": 0.3,
             "execution_id": EXEC_GATE, "node_id": "S1:GATE:1", "subtask_id": "S1"},
        ],
        "commit_records": [
            {"repository": REPO, "commit_hash": f"aaa111{run_id}", "subtask_id": "S1",
             "producer_execution_ids": [EXEC_B], "expected_parent": "base", "role": "SUBTASK"},
        ],
        "review": {
            "outcome": "FAIL", "execution_id": EXEC_REVIEW_2,
            "reviewed_execution_ids": [EXEC_B],
            "findings": [{"finding_id": "F001", "severity": "P1", "file": "a.py",
                          "location": "1", "description": "second review finding",
                          "required_fix": "fix",
                          "finding_key": {"review_execution_id": EXEC_REVIEW_2,
                                          "finding_id": "F001"}}],
        },
        "selected_repairs": ["F001"], "repair_commit": f"bbb222{run_id}",
        "delta_review": {"outcome": "PASS", "execution_id": EXEC_DELTA,
                         "repair_execution_id": EXEC_REPAIR,
                         "original_review_execution_id": EXEC_REVIEW_2},
        "human_decisions": [],
    }), encoding="utf-8")

    (run / "CUSTOM_JOB" / "candidate.json").write_text(json.dumps({
        "schema_version": "AAW_CANDIDATE_V0.4A", "candidate_id": f"CAN_{run_id}",
        "run_id": run_id, "repository": REPO, "candidate_head": f"bbb222{run_id}",
        "artifact_manifest": None, "review_execution_ids": [EXEC_REVIEW_2, EXEC_DELTA],
        "check_execution_ids": [EXEC_GATE], "created_at": "2026-09-06T20:09:00+02:00",
        "content_identity_hash": "c" * 64,
    }), encoding="utf-8")
    (run / "CUSTOM_JOB" / f"HDE_{run_id}.json").write_text(json.dumps({
        "schema_version": "AAW_HUMAN_DECISION_V0.4A", "human_decision_id": f"HDE_{run_id}",
        "candidate_id": f"CAN_{run_id}", "verdict": "ACCEPT",
        "timestamp": "2026-09-06T20:10:00+02:00", "quality_assessment": None,
        "reason": "release now",
    }), encoding="utf-8")

    # Preprocess: the SAME logical node preprocessed twice, once per downstream
    # execution. The two records must not collapse into one.
    for tag, downstream in (("first", EXEC_A), ("second", EXEC_B)):
        (run / "PREPROCESS" / f"S1__SKIP__{tag}.json").write_text(json.dumps({
            "schema_version": "AAW_LOCAL_QWEN_PREPROCESS_V0.4A", "run_id": run_id,
            "node_id": "S1", "node_type": "SUBTASK", "preprocess_id": f"PRE_{run_id}_{tag}",
            "execution_id": None, "status": "SKIPPED_POLICY_OFF",
            "downstream_execution_id": downstream, "consumed": False,
            "created_at": SHARED_TIME,
            "telemetry": {"downstream_node_id": "S1", "input_chars": 0, "output_chars": 0,
                          "wall_time_s": 0.0, "downstream_input_before_estimate": 400},
        }), encoding="utf-8")

    # Lifecycle: gate closed, EXEC_A started but never closed, EXEC_B intent only.
    A.write_ledger(run, run_id, [
        A._intent(EXEC_GATE, node_id="S1:GATE:1", kind="MACHINE_GATE"),
        A._started(EXEC_GATE), A._closed(EXEC_GATE),
        A._intent(EXEC_A, node_id="S1", kind="LLM"), A._started(EXEC_A),
        A._intent(EXEC_B, node_id="S1", kind="LLM"),
        {"event_type": "COMMIT_RECORDED", "execution_id": None,
         "payload": {"repository": REPO, "commit_hash": f"aaa111{run_id}", "subtask_id": "S1",
                     "role": "SUBTASK", "expected_parent": "base",
                     "producer_execution_ids": [EXEC_B], "git_evidence_hash": "g" * 64}},
        {"event_type": "HUMAN_DECISION_RECORDED", "execution_id": None,
         "payload": {"human_decision_id": f"HDE_{run_id}",
                     "candidate_id": f"CAN_{run_id}",
                     "verdict": "ACCEPT", "quality_assessment": None,
                     "decision_artifact_hash": "h" * 64}},
    ])
    return run


def _legacy_run(root: Path) -> None:
    """A pre-V0.4A workflow run that reuses node id S1 and the same timestamp."""
    legacy = root / "AAW_OLD"
    (legacy / "WORKFLOW").mkdir(parents=True)
    (legacy / "WORKFLOW" / "workflow_state.json").write_text(json.dumps({
        "workflow_id": "IMPLEMENT_REVIEW_REPAIR_V1", "AAW_RUN_ID": "AAW_OLD",
        "goal": "legacy", "status": "WAITING_FOR_HUMAN", "final_outcome": "HUMAN_REQUIRED",
        "started_at": SHARED_TIME, "updated_at": "2026-09-06T20:05:00+02:00",
        "repo": r"D:\product\legacy", "worktree": r"D:\product\legacy\wt", "repair_cycle": 0,
        "completed_nodes": [{"node_id": "S1", "node_type": "IMPLEMENT", "outcome": "PASS",
                             "duration_s": 55.0}],
        "telemetry": [{"workflow_node_id": "S1", "workflow_node_type": "IMPLEMENT",
                       "harness": "codex", "model": "gpt-5.6-terra", "effort": "high",
                       "provider_session_id": SHARED_SESSION, "started_at": SHARED_TIME,
                       "wall_time_s": 55.0, "outcome": "VALID PASS",
                       "usage": {"input_tokens": 7, "output_tokens": 1}}],
    }), encoding="utf-8")


def _fixture_run(root: Path) -> None:
    """A declared self-test run. Must not reach default model-performance views."""
    fixture = root / "AAW_FIXTURE"
    (fixture / "CUSTOM_JOB").mkdir(parents=True)
    execution_id = A._exe("FIXTURE")
    A.write_descriptor(fixture, execution_id=execution_id, node_id="S1", subtask_id="S1",
                       invocation_kind="LLM", model="fixture-only-model", effort="high",
                       created_at=SHARED_TIME, run_id="AAW_FIXTURE")
    (fixture / "CUSTOM_JOB" / "job_state.json").write_text(json.dumps({
        "schema_version": "AAW_CUSTOM_JOB_RUN_V0.4A", "AAW_RUN_ID": "AAW_FIXTURE",
        "job_id": "self-test", "job_type": "MULTI_SUBTASK", "goal": "fixture",
        "status": "WAITING_FOR_HUMAN", "started_at": SHARED_TIME, "repository": REPO,
        "executions": [{"execution_id": execution_id, "node_id": "S1", "subtask_id": "S1",
                        "invocation_kind": "LLM", "model": "fixture-only-model",
                        "effort": "high"}],
        "telemetry": [{"node_type": "SUBTASK", "execution_id": execution_id, "node_id": "S1",
                       "subtask_id": "S1", "subtask_index": 1, "model": "fixture-only-model",
                       "effort": "high", "wall_time_s": 0.01, "input_tokens": 1,
                       "output_tokens": 1}],
        "subtask_results": [{"outcome": "PASS", "subtask_id": "S1", "subtask_index": 1,
                             "execution_id": execution_id}],
    }), encoding="utf-8")


@pytest.fixture()
def indexed(tmp_path: Path):
    root = tmp_path / "03_STATS"
    root.mkdir()
    _modern_run(root)
    _legacy_run(root)
    _fixture_run(root)
    (root / "run_20260901_flat__N05_BUILD__20260901T210001+0200.json").write_text(json.dumps({
        "run_id": "run_20260901_flat", "pipeline": "P04", "node": "N05_BUILD", "agent": "claude",
        "model": "claude-opus-5", "effort": "high", "completed_at": "2026-09-01T21:33:59+02:00",
        "usage": None, "outcome": "skipped",
    }), encoding="utf-8")
    db = tmp_path / "index.sqlite"
    summary = A.ingest(db, root, rebuild=True)
    return root, db, summary


def _rows(db: Path, sql: str, *params):
    conn = A.connect(db)
    try:
        return [dict(row) for row in conn.execute(sql, params)]
    finally:
        conn.close()


def _one(db: Path, sql: str, *params):
    conn = A.connect(db)
    try:
        row = conn.execute(sql, params).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Module self-test and ingestion basics
# ---------------------------------------------------------------------------

def test_module_self_test_passes():
    assert A._self_test() == 0


def test_schema_marker_is_v2_and_not_a_silent_v1_mutation(indexed):
    _root, db, _summary = indexed
    assert _one(db, "SELECT value FROM meta WHERE key='schema_version'") == "AAW_ANALYTICS_INDEX_V2"
    assert A.SCHEMA_VERSION not in A.PRIOR_SCHEMA_VERSIONS


def test_ingestion_validates_before_publish(indexed):
    _root, _db, summary = indexed
    assert summary["validation"]["valid"] is True
    assert summary["validation"]["errors"] == []
    assert summary["errors"] == 0


def test_raw_evidence_is_never_mutated(indexed):
    root, db, _summary = indexed
    before = {path: path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}
    A.ingest(db, root, rebuild=True)
    A.ingest(db, root, rebuild=False)
    after = {path: path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}
    assert before == after


# ---------------------------------------------------------------------------
# §34 A - N: the false-join regression suite
# ---------------------------------------------------------------------------

def test_A_same_run_same_node_two_execution_ids_stay_two_executions(indexed):
    _root, db, _summary = indexed
    rows = _rows(db, "SELECT execution_id FROM execution WHERE run_id='AAW_V2' AND node_id='S1' "
                     "AND invocation_kind='LLM' ORDER BY execution_id")
    assert [row["execution_id"] for row in rows] == sorted([EXEC_A, EXEC_B])
    # and the logical node itself remains ONE node
    assert _one(db, "SELECT COUNT(*) FROM logical_node WHERE run_id='AAW_V2' AND node_id='S1'") == 1


def test_B_shared_provider_session_does_not_merge_executions(indexed):
    _root, db, _summary = indexed
    modern = _one(db, "SELECT COUNT(DISTINCT execution_id) FROM execution "
                      "WHERE provider_session_id=? AND execution_id IS NOT NULL", SHARED_SESSION)
    assert modern == 2
    # the legacy row shares the session too and still never joins the modern ones
    assert _one(db, "SELECT COUNT(*) FROM execution WHERE provider_session_id=?",
                SHARED_SESSION) == 3


def test_C_same_finding_id_under_two_reviews_stays_two_findings(tmp_path):
    """`F001` is meaningless on its own: identity is (review_execution_id, finding_id)."""
    root = tmp_path / "03_STATS"
    root.mkdir()
    _modern_run(root, run_id="AAW_C1")
    _modern_run(root, run_id="AAW_C2")
    db = tmp_path / "index.sqlite"
    A.ingest(db, root, rebuild=True)
    rows = _rows(db, "SELECT review_execution_id, finding_id, run_id FROM review_finding "
                     "WHERE finding_id='F001' ORDER BY run_id")
    assert len(rows) == 2, "one F001 per owning review execution, never collapsed"
    assert {row["run_id"] for row in rows} == {"AAW_C1", "AAW_C2"}
    assert len({row["review_execution_id"] for row in rows}) == 2
    # each repair selects the finding of ITS OWN review, never the other run's
    links = _rows(db, "SELECT review_execution_id, finding_id, linked_execution_id, run_id "
                      "FROM finding_link WHERE link_role='REPAIR_SELECTED' ORDER BY run_id")
    assert len(links) == 2
    for link in links:
        owner_run = _one(db, "SELECT run_id FROM review_finding "
                             "WHERE review_execution_id=? AND finding_id=?",
                         link["review_execution_id"], link["finding_id"])
        assert owner_run == link["run_id"]


def test_D_identical_created_at_does_not_merge_executions(indexed):
    _root, db, _summary = indexed
    same_time = _rows(db, "SELECT execution_id FROM execution WHERE created_at=? "
                          "AND run_id='AAW_V2' AND execution_id IS NOT NULL", SHARED_TIME)
    assert len({row["execution_id"] for row in same_time}) == 7
    # every row still has its own derived locator
    assert _one(db, "SELECT COUNT(DISTINCT row_locator) FROM execution") == \
        _one(db, "SELECT COUNT(*) FROM execution")


def test_E_preprocess_of_one_node_links_per_downstream_execution(indexed):
    _root, db, _summary = indexed
    rows = _rows(db, "SELECT preprocess_id, downstream_node_id, downstream_execution_id, "
                     "downstream_link_confidence FROM preprocess ORDER BY preprocess_id")
    assert len(rows) == 2
    assert {row["downstream_node_id"] for row in rows} == {"S1"}
    assert {row["downstream_execution_id"] for row in rows} == {EXEC_A, EXEC_B}
    assert {row["downstream_link_confidence"] for row in rows} == {"EXPLICIT_EXECUTION_ID"}
    # a skipped decision has a PRE_ identity and NO execution identity
    assert _one(db, "SELECT COUNT(*) FROM preprocess WHERE execution_id IS NOT NULL") == 0
    assert _one(db, "SELECT COUNT(*) FROM preprocess WHERE decision_status='SKIPPED'") == 2


def test_F_repair_and_retry_on_one_node_keep_distinct_semantics(indexed):
    _root, db, _summary = indexed
    retry = _rows(db, "SELECT execution_id, retry_of_execution_id, "
                      "originating_review_execution_id FROM execution "
                      "WHERE execution_id IN (?,?,?) ORDER BY invocation_kind",
                  EXEC_A, EXEC_B, EXEC_REPAIR)
    by_id = {row["execution_id"]: row for row in retry}
    # the genuine retry carries a retry relation and no review lineage
    assert by_id[EXEC_B]["retry_of_execution_id"] == EXEC_A
    assert by_id[EXEC_B]["originating_review_execution_id"] is None
    # the repair carries review lineage and NO retry relation
    assert by_id[EXEC_REPAIR]["retry_of_execution_id"] is None
    assert by_id[EXEC_REPAIR]["originating_review_execution_id"] == EXEC_REVIEW_2
    # all three share one logical node and stay three executions
    assert _one(db, "SELECT COUNT(*) FROM execution WHERE run_id='AAW_V2' AND node_id='S1'") == 3
    # the repair's selected finding is keyed to the review that raised it
    link = _rows(db, "SELECT review_execution_id, finding_id, disposition FROM finding_link "
                     "WHERE linked_execution_id=? AND link_role='REPAIR_SELECTED'", EXEC_REPAIR)
    assert link == [{"review_execution_id": EXEC_REVIEW_2, "finding_id": "F001",
                     "disposition": "SELECTED_FOR_REPAIR"}]
    # the delta review names the repair and the original review explicitly
    delta = _rows(db, "SELECT repair_execution_id, original_review_execution_id FROM execution "
                      "WHERE execution_id=?", EXEC_DELTA)
    assert delta == [{"repair_execution_id": EXEC_REPAIR,
                      "original_review_execution_id": EXEC_REVIEW_2}]


def test_G_commit_in_state_and_ledger_is_one_entity(indexed):
    _root, db, _summary = indexed
    rows = _rows(db, "SELECT repository, commit_hash, observed_in_state, observed_in_ledger, "
                     "conflict FROM commit_record")
    assert len(rows) == 1
    assert rows[0]["observed_in_state"] == 1 and rows[0]["observed_in_ledger"] == 1
    assert rows[0]["conflict"] == 0
    producers = _rows(db, "SELECT execution_id FROM commit_producer_link")
    assert [row["execution_id"] for row in producers] == [EXEC_B]


def test_H_human_decision_references_the_exact_candidate(indexed):
    _root, db, _summary = indexed
    rows = _rows(db, "SELECT human_decision_id, candidate_id, verdict, quality_assessment, "
                     "source_authority, candidate_resolved FROM human_decision")
    assert rows == [{"human_decision_id": "HDE_AAW_V2", "candidate_id": "CAN_AAW_V2",
                     "verdict": "ACCEPTED", "quality_assessment": None,
                     "source_authority": "HUMAN_DECISION_ARTIFACT", "candidate_resolved": 1}]
    # ACCEPT is a release verdict and is never turned into a model-quality PASS
    assert _one(db, "SELECT quality_assessment FROM human_decision") is None


def test_I_changed_candidate_content_is_a_different_identity(tmp_path):
    root = tmp_path / "03_STATS"
    root.mkdir()
    run = _modern_run(root)
    db = tmp_path / "index.sqlite"
    A.ingest(db, root, rebuild=True)
    assert _one(db, "SELECT content_identity_hash FROM candidate WHERE candidate_id='CAN_AAW_V2'") \
        == "c" * 64

    # a changed candidate must arrive as a NEW candidate id, not an edit
    (run / "CUSTOM_JOB" / "candidate.json").write_text(json.dumps({
        "schema_version": "AAW_CANDIDATE_V0.4A", "candidate_id": "CAN_second",
        "run_id": "AAW_V2", "repository": REPO, "candidate_head": "ccc333",
        "review_execution_ids": [EXEC_REVIEW_2], "check_execution_ids": [],
        "created_at": "2026-09-06T20:20:00+02:00", "content_identity_hash": "d" * 64,
    }), encoding="utf-8")
    A.ingest(db, root, rebuild=False)
    ids = {row["candidate_id"] for row in _rows(db, "SELECT candidate_id FROM candidate")}
    assert ids == {"CAN_second"}
    # the decision still points at the candidate it actually referenced
    assert _one(db, "SELECT candidate_id FROM human_decision") == "CAN_AAW_V2"
    assert _one(db, "SELECT candidate_resolved FROM human_decision") == 0
    assert _one(db, "SELECT COUNT(*) FROM ingest_diagnostic "
                    "WHERE kind='HUMAN_DECISION_CANDIDATE_UNRESOLVED'") == 1

    # the same candidate id re-declared with different content is an integrity error
    (run / "CUSTOM_JOB" / "candidate.json").write_text(json.dumps({
        "schema_version": "AAW_CANDIDATE_V0.4A", "candidate_id": "CAN_second",
        "run_id": "AAW_V2", "repository": REPO, "candidate_head": "eee555",
        "created_at": "2026-09-06T20:30:00+02:00", "content_identity_hash": "e" * 64,
    }), encoding="utf-8")
    (run / "CUSTOM_JOB" / "candidate_state_copy.json").write_text("{}", encoding="utf-8")
    state = json.loads((run / "CUSTOM_JOB" / "job_state.json").read_text(encoding="utf-8"))
    state["candidate"] = {"schema_version": "AAW_CANDIDATE_V0.4A", "candidate_id": "CAN_second",
                          "run_id": "AAW_V2", "candidate_head": "fff666",
                          "content_identity_hash": "f" * 64}
    (run / "CUSTOM_JOB" / "job_state.json").write_text(json.dumps(state), encoding="utf-8")
    A.ingest(db, root, rebuild=False)
    assert _one(db, "SELECT conflict FROM candidate WHERE candidate_id='CAN_second'") == 1
    assert _one(db, "SELECT COUNT(*) FROM entity_conflict WHERE entity='candidate'") >= 1
    # the artifact remains the authority over the state copy
    assert _one(db, "SELECT source_authority FROM candidate WHERE candidate_id='CAN_second'") \
        == "CANDIDATE_ARTIFACT"


def test_J_legacy_records_keep_a_null_execution_identity(indexed):
    _root, db, _summary = indexed
    legacy = _rows(db, "SELECT run_id, execution_id, row_locator, evidence_grain FROM execution "
                       "WHERE run_id IN ('AAW_OLD','run_20260901_flat')")
    assert legacy, "legacy evidence is indexed"
    for row in legacy:
        assert row["execution_id"] is None
        assert row["evidence_grain"] == A.LEGACY
        assert row["row_locator"].startswith("LOC_")
    # no EXE_ identity is ever synthesised for historical evidence
    assert _one(db, "SELECT COUNT(*) FROM execution WHERE execution_id IS NOT NULL "
                    "AND execution_id NOT LIKE 'EXE_%'") == 0
    # and a legacy flat run gets no invented run-level status
    assert _one(db, "SELECT final_status FROM run WHERE run_id='run_20260901_flat'") == A.UNKNOWN


def test_K_fixtures_do_not_contaminate_default_metrics(indexed):
    _root, db, _summary = indexed
    api = A.Analytics(db)
    default_models = {row["model"] for row in api.outcome_by_model_profile()}
    assert "fixture-only-model" not in default_models
    assert "gpt-5.6-terra" in default_models
    included = {row["model"] for row in
                api.outcome_by_model_profile({"include_fixtures": True})}
    assert "fixture-only-model" in included
    # the fixture classification is explicit and auditable
    assert _rows(db, "SELECT fixture_class, fixture_evidence FROM run "
                     "WHERE run_id='AAW_FIXTURE'") == \
        [{"fixture_class": "FIXTURE_SELF_TEST", "fixture_evidence": "DECLARED_SELF_TEST_JOB_ID"}]
    # data quality still counts them, so the exclusion is visible not hidden
    assert api.data_quality()["fixture_runs"] == 1


def test_L_duplicate_incompatible_execution_id_is_quarantined(tmp_path):
    root = tmp_path / "03_STATS"
    duplicate = A._exe("DUP")
    for run_id, node_id in (("AAW_ONE", "S1"), ("AAW_TWO", "S9")):
        run = root / run_id
        (run / "CUSTOM_JOB").mkdir(parents=True)
        A.write_descriptor(run, execution_id=duplicate, node_id=node_id, invocation_kind="LLM",
                           model="m", effort="high", created_at=SHARED_TIME, run_id=run_id)
        (run / "CUSTOM_JOB" / "job_state.json").write_text(json.dumps({
            "schema_version": "AAW_CUSTOM_JOB_RUN_V0.4A", "AAW_RUN_ID": run_id,
            "job_id": "product", "job_type": "MULTI_SUBTASK", "goal": "dup",
            "status": "RUNNING", "started_at": SHARED_TIME, "repository": REPO,
            "executions": [], "telemetry": [],
        }), encoding="utf-8")
    db = tmp_path / "index.sqlite"
    A.ingest(db, root, rebuild=True)
    # exactly one row survives; the original is preserved, not overwritten
    assert _one(db, "SELECT COUNT(*) FROM execution WHERE execution_id=?", duplicate) == 1
    assert _one(db, "SELECT run_id FROM execution WHERE execution_id=?", duplicate) == "AAW_ONE"
    assert _one(db, "SELECT conflict FROM execution WHERE execution_id=?", duplicate) == 1
    diagnostics = _rows(db, "SELECT detail FROM ingest_diagnostic WHERE kind='DATA_INTEGRITY_ERROR'")
    assert diagnostics and "duplicate modern execution_id" in diagnostics[0]["detail"]
    assert _one(db, "SELECT COUNT(*) FROM entity_conflict WHERE entity='execution'") >= 1
    # the conflicted entity is excluded from relationship metrics
    assert A.Analytics(db).outcome_by_model_profile({"include_fixtures": True}) == []


def test_M_started_without_closed_is_unresolved_not_fail(indexed):
    _root, db, _summary = indexed
    row = _rows(db, "SELECT lifecycle_state, basis, started_sequence, closed_sequence "
                    "FROM execution_lifecycle WHERE execution_id=?", EXEC_A)
    assert row[0]["lifecycle_state"] == "STARTED_OPEN"
    assert row[0]["closed_sequence"] is None
    # an unresolved lifecycle never becomes a semantic outcome
    assert _one(db, "SELECT close_reason FROM execution_lifecycle WHERE execution_id=?",
                EXEC_A) is None
    assert _one(db, "SELECT outcome FROM execution WHERE execution_id=?", EXEC_A) != "FAIL"
    health = A.Analytics(db).lifecycle_health({"include_fixtures": True})
    assert health["started_open"] == 1
    assert "not an analytical FAIL" in health["note"]


def test_N_intent_only_is_unresolved_not_success_or_failure(indexed):
    _root, db, _summary = indexed
    row = _rows(db, "SELECT lifecycle_state, intent_sequence, started_sequence, closed_sequence, "
                    "close_reason FROM execution_lifecycle WHERE execution_id=?", EXEC_B)
    assert row[0]["lifecycle_state"] == "INTENT_ONLY"
    assert row[0]["started_sequence"] is None and row[0]["closed_sequence"] is None
    assert row[0]["close_reason"] is None
    # the semantic subtask result still stands on its own authority
    assert _one(db, "SELECT outcome FROM execution WHERE execution_id=?", EXEC_B) == "PASS"
    assert _one(db, "SELECT outcome_source FROM execution WHERE execution_id=?", EXEC_B) \
        == "SUBTASK_RESULT"
    health = A.Analytics(db).lifecycle_health({"include_fixtures": True})
    assert health["intent_only"] == 1


# ---------------------------------------------------------------------------
# Source authority, precedence and conflicts
# ---------------------------------------------------------------------------

def test_descriptor_is_identity_authority_over_state(indexed):
    _root, db, _summary = indexed
    rows = _rows(db, "SELECT identity_authority, identity_contract FROM execution "
                     "WHERE execution_id IS NOT NULL")
    assert {row["identity_authority"] for row in rows} == {"EXECUTION_DESCRIPTOR"}
    assert {row["identity_contract"] for row in rows} == {A.IDENTITY_CONTRACT}


def test_ledger_close_outcome_never_replaces_a_reviewer_verdict(indexed):
    _root, db, _summary = indexed
    # the ledger observed the gate closing with outcome PASS
    assert _one(db, "SELECT ledger_outcome FROM execution_lifecycle WHERE execution_id=?",
                EXEC_GATE) == "PASS"
    # the reviewer FAIL is kept on its own authority
    assert _one(db, "SELECT outcome, outcome_source FROM execution WHERE execution_id=?",
                EXEC_REVIEW_2) == "FAIL"
    assert _one(db, "SELECT outcome_source FROM execution WHERE execution_id=?",
                EXEC_REVIEW_2) == "REVIEW_RESULT"
    # a repair has no recorded semantic outcome and is not given the ledger's
    assert _one(db, "SELECT outcome FROM execution WHERE execution_id=?", EXEC_REPAIR) is None


def test_source_lineage_is_traceable_for_every_modern_execution(indexed):
    _root, db, _summary = indexed
    orphans = _one(db, """SELECT COUNT(*) FROM execution e
                          WHERE e.execution_id IS NOT NULL AND NOT EXISTS (
                            SELECT 1 FROM entity_source s
                            WHERE s.entity='execution' AND s.entity_key=e.execution_id)""")
    assert orphans == 0
    roles = {row["role"] for row in
             _rows(db, "SELECT DISTINCT role FROM entity_source WHERE entity='execution'")}
    assert "EXECUTION_DESCRIPTOR" in roles and "TELEMETRY" in roles


def test_conflicting_authoritative_sources_are_recorded_not_silently_resolved(tmp_path):
    root = tmp_path / "03_STATS"
    root.mkdir()
    run = _modern_run(root)
    # the state disagrees with the descriptor about the model
    state = json.loads((run / "CUSTOM_JOB" / "job_state.json").read_text(encoding="utf-8"))
    for entry in state["executions"]:
        if entry["execution_id"] == EXEC_A:
            entry["model"] = "some-other-model"
    (run / "CUSTOM_JOB" / "job_state.json").write_text(json.dumps(state), encoding="utf-8")
    db = tmp_path / "index.sqlite"
    A.ingest(db, root, rebuild=True)
    # the descriptor wins, the disagreement is visible, the row is flagged
    assert _one(db, "SELECT model FROM execution WHERE execution_id=?", EXEC_A) == "gpt-5.6-terra"
    assert _one(db, "SELECT conflict FROM execution WHERE execution_id=?", EXEC_A) == 1
    conflicts = _rows(db, "SELECT entity, field, source_a, source_b FROM entity_conflict")
    assert {"entity": "execution", "field": "model", "source_a": "EXECUTION_DESCRIPTOR",
            "source_b": "STATE_EXECUTIONS"} in conflicts


# ---------------------------------------------------------------------------
# Ledger ingestion
# ---------------------------------------------------------------------------

def test_ledger_is_read_through_the_existing_reader(indexed):
    assert A.ledger_module() is not None, "execution_ledger must be importable, not reimplemented"
    _root, db, _summary = indexed
    assert _one(db, "SELECT COUNT(*) FROM run WHERE ledger_present=1") == 1
    assert _one(db, "SELECT layout FROM source_file WHERE rel_path LIKE '%execution_events.jsonl'") \
        == "LEDGER"
    assert _one(db, "SELECT status FROM source_file WHERE rel_path LIKE '%execution_events.jsonl'") \
        == "INDEXED"


def test_damaged_ledger_tail_is_diagnosed_without_losing_earlier_events(tmp_path):
    root = tmp_path / "03_STATS"
    root.mkdir()
    run = _modern_run(root)
    A.write_ledger(run, "AAW_V2", [
        A._intent(EXEC_GATE, node_id="S1:GATE:1", kind="MACHINE_GATE"),
        A._started(EXEC_GATE), A._closed(EXEC_GATE),
        A._intent(EXEC_A, node_id="S1", kind="LLM"),
    ], damaged_tail=True)
    db = tmp_path / "index.sqlite"
    summary = A.ingest(db, root, rebuild=True)
    assert summary["validation"]["valid"] is True
    assert _one(db, "SELECT COUNT(*) FROM ingest_diagnostic WHERE kind='LEDGER_DAMAGED_TAIL'") == 1
    # every valid preceding event is still indexed
    assert _one(db, "SELECT lifecycle_state FROM execution_lifecycle WHERE execution_id=?",
                EXEC_GATE) == "CLOSED"
    assert _one(db, "SELECT lifecycle_state FROM execution_lifecycle WHERE execution_id=?",
                EXEC_A) == "INTENT_ONLY"
    # the raw ledger is never repaired by analytics
    assert (run / "LEDGER" / "execution_events.jsonl").read_text(
        encoding="utf-8").endswith('"event_i')


def test_lifecycle_conflict_is_surfaced_not_normalised(tmp_path):
    root = tmp_path / "03_STATS"
    root.mkdir()
    run = _modern_run(root)
    # a close with no start and a source that is not an allowed never-started
    # source is a lifecycle conflict per the V0.4B contract
    A.write_ledger(run, "AAW_V2", [
        A._intent(EXEC_A, node_id="S1", kind="LLM"),
        A._closed(EXEC_A, source="CHILD_PROCESS_EXIT"),
    ])
    db = tmp_path / "index.sqlite"
    A.ingest(db, root, rebuild=True)
    row = _rows(db, "SELECT lifecycle_state, conflict_detail FROM execution_lifecycle "
                    "WHERE execution_id=?", EXEC_A)
    assert row[0]["lifecycle_state"] == "LIFECYCLE_CONFLICT"
    assert "CLOSED_WITHOUT_STARTED_NOT_RECONCILED" in row[0]["conflict_detail"]
    assert A.Analytics(db).lifecycle_health({"include_fixtures": True})["conflicts"] == 1


def test_modern_execution_without_ledger_evidence_is_unknown_not_closed(indexed):
    _root, db, _summary = indexed
    rows = _rows(db, "SELECT lifecycle_state, basis FROM execution_lifecycle "
                     "WHERE basis='NO_LEDGER_EVIDENCE'")
    assert rows, "executions with no ledger evidence still get an honest lifecycle row"
    assert {row["lifecycle_state"] for row in rows} == {A.UNKNOWN}
    # one lifecycle row per modern execution, no more and no fewer
    assert _one(db, "SELECT COUNT(*) FROM execution_lifecycle") == \
        _one(db, "SELECT COUNT(*) FROM execution WHERE execution_id IS NOT NULL")


# ---------------------------------------------------------------------------
# Rebuild, refresh, migration safety
# ---------------------------------------------------------------------------

def test_deterministic_rebuild(indexed):
    root, db, _summary = indexed
    first = A.normalized_snapshot(db)
    A.ingest(db, root, rebuild=True)
    second = A.normalized_snapshot(db)
    assert second == first
    A.ingest(db, root, rebuild=True)
    assert A.normalized_snapshot(db) == first


def test_incremental_refresh_is_idempotent(indexed):
    root, db, _summary = indexed
    first = A.normalized_snapshot(db)
    A.ingest(db, root, rebuild=False)
    A.ingest(db, root, rebuild=False)
    assert A.normalized_snapshot(db) == first


def test_growing_ledger_is_detected_and_never_duplicated(indexed):
    root, db, _summary = indexed
    run = root / "AAW_V2"
    A.write_ledger(run, "AAW_V2", [
        A._intent(EXEC_GATE, node_id="S1:GATE:1", kind="MACHINE_GATE"),
        A._started(EXEC_GATE), A._closed(EXEC_GATE),
        A._intent(EXEC_A, node_id="S1", kind="LLM"), A._started(EXEC_A), A._closed(EXEC_A),
        A._intent(EXEC_B, node_id="S1", kind="LLM"),
    ])
    A.ingest(db, root, rebuild=False)
    assert _one(db, "SELECT lifecycle_state FROM execution_lifecycle WHERE execution_id=?",
                EXEC_A) == "CLOSED"
    assert _one(db, "SELECT COUNT(*) FROM execution_lifecycle WHERE execution_id=?", EXEC_A) == 1
    assert _one(db, "SELECT COUNT(*) FROM execution WHERE execution_id=?", EXEC_A) == 1
    assert _one(db, "SELECT COUNT(*) FROM commit_record") == 1


def test_removed_evidence_stops_being_indexed(indexed):
    root, db, _summary = indexed
    assert _one(db, "SELECT COUNT(*) FROM run WHERE run_id='AAW_FIXTURE'") == 1
    for path in sorted((root / "AAW_FIXTURE").rglob("*"), reverse=True):
        if path.is_file():
            path.unlink()
        else:
            path.rmdir()
    (root / "AAW_FIXTURE").rmdir()
    A.ingest(db, root, rebuild=False)
    assert _one(db, "SELECT COUNT(*) FROM run WHERE run_id='AAW_FIXTURE'") == 0
    assert _one(db, "SELECT COUNT(*) FROM source_file WHERE unit_key='AAW_FIXTURE'") == 0


def test_a_v1_index_is_replaced_by_a_validated_v2_build(tmp_path):
    root = tmp_path / "03_STATS"
    root.mkdir()
    _modern_run(root)
    db = tmp_path / "index.sqlite"
    # simulate a V1.1 index sitting at the published path
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO meta VALUES ('schema_version','AAW_ANALYTICS_INDEX_V1.1')")
    conn.commit(); conn.close()
    A.ingest(db, root, rebuild=False)  # must upgrade rather than append to V1
    assert _one(db, "SELECT value FROM meta WHERE key='schema_version'") == A.SCHEMA_VERSION
    assert not (db.with_name(db.name + ".v2build.tmp")).exists()


def test_failed_build_leaves_the_published_index_untouched(tmp_path, monkeypatch):
    root = tmp_path / "03_STATS"
    root.mkdir()
    _modern_run(root)
    db = tmp_path / "index.sqlite"
    A.ingest(db, root, rebuild=True)
    good = A.normalized_snapshot(db)

    def broken(_conn):
        return {"valid": False, "errors": ["injected validation failure"], "warnings": [],
                "runs": 0, "indexed_sources": 0}

    monkeypatch.setattr(A, "validate_index", broken)
    with pytest.raises(RuntimeError, match="ANALYTICS_V2_VALIDATION_FAILED"):
        A.ingest(db, root, rebuild=True)
    monkeypatch.undo()
    assert A.normalized_snapshot(db) == good
    assert not (db.with_name(db.name + ".v2build.tmp")).exists()


# ---------------------------------------------------------------------------
# Query API
# ---------------------------------------------------------------------------

def test_query_api_surface_is_retained(indexed):
    _root, db, _summary = indexed
    api = A.Analytics(db)
    assert api.available
    for name in ("meta", "filter_options", "data_quality", "key_metrics", "usage_over_time",
                 "outcome_by_model_profile", "review_first_pass", "median_exec_time",
                 "qwen_preprocess_metrics", "export_csv", "lifecycle_health",
                 "analytics_readiness"):
        assert callable(getattr(api, name)), name
    km = api.key_metrics()
    assert set(km) >= {"runs", "llm_calls", "median_wall_time_s", "wall_time_sample",
                       "human_acceptance_coverage"}
    assert set(api.data_quality()) >= {"indexed_runs", "unlinked_preprocess_artifacts",
                                       "source_parse_errors", "data_integrity_errors"}
    rfp = api.review_first_pass()
    assert set(rfp) >= {"n", "first_pass", "needed_repair", "insufficient_evidence",
                        "accepted", "rejected"}
    assert isinstance(api.usage_over_time(), list)
    assert isinstance(api.outcome_by_model_profile(), list)
    assert isinstance(api.median_exec_time("model"), list)
    assert "ESTIMATED CONTEXT REDUCTION" in api.qwen_preprocess_metrics()["note"]
    options = api.filter_options()
    assert set(options) >= {"job_class", "node_type", "model", "profile", "invocation_kind", "grain"}
    assert options["grain"] == [A.MODERN, A.LEGACY]


def test_usage_counts_executions_not_gates_or_skipped_preprocess(indexed):
    _root, db, _summary = indexed
    usage = A.Analytics(db).usage_over_time({"include_fixtures": True})
    calls = sum(row["calls"] for row in usage)
    gates = sum(row["machine_gate_calls"] for row in usage)
    # 6 modern LLM-family executions + 1 fixture + 1 legacy workflow + 1 legacy flat
    assert calls == 9
    assert gates == 1
    # the two skipped preprocess decisions are not invocations
    assert _one(db, "SELECT COUNT(*) FROM preprocess WHERE decision_status='SKIPPED'") == 2


def test_review_first_pass_uses_explicit_lineage(indexed):
    _root, db, _summary = indexed
    rfp = A.Analytics(db).review_first_pass({"include_fixtures": True})
    assert rfp["basis"] == "EXPLICIT_EXECUTION_LINEAGE"
    assert rfp["n"] == 2                      # two review executions
    # only the second review has a recorded verdict and a finding; the first has
    # no recorded outcome and is reported as unknown rather than as a pass
    assert rfp["needed_repair"] == 1
    assert rfp["first_pass"] == 0
    assert rfp["outcome_unknown"] == 1
    assert rfp["reviews_without_producer_link"] == 0
    assert rfp["legacy_source_grain"]["basis"] == "RUN_LEVEL_SOURCE_GRAIN"
    assert rfp["accepted"] == 1 and rfp["rejected"] == 0


def test_readiness_reports_evidence_not_permission(indexed):
    _root, db, _summary = indexed
    readiness = A.Analytics(db).analytics_readiness()
    assert readiness["kind"] == "EVIDENCE_READINESS"
    assert "READY_FOR_ADAPTIVE" not in json.dumps(readiness)
    assert readiness["human_quality_coverage"] == 0.0
    assert set(readiness) >= {"modern_execution_count", "closed_lifecycle_coverage",
                              "model_provenance_coverage", "usage_coverage",
                              "review_lineage_coverage", "human_quality_coverage",
                              "integrity_error_count", "unlinked_count"}


def test_release_verdict_and_quality_coverage_are_separate(indexed):
    _root, db, _summary = indexed
    dq = A.Analytics(db).data_quality()
    assert dq["release_verdict_coverage"]["n"] == 1
    assert dq["quality_assessment_coverage"]["n"] == 0
    assert dq["release_verdict_coverage"] != dq["quality_assessment_coverage"]


def test_csv_export_is_marked_derived_and_carries_execution_identity(indexed, tmp_path):
    _root, db, _summary = indexed
    target = tmp_path / "export.csv"
    count = A.Analytics(db).export_csv(target, {"include_fixtures": True})
    assert count > 0
    lines = target.read_text(encoding="utf-8").splitlines()
    header = lines[0].split(",")
    assert header[0] == "notice"
    assert "execution_id" in header and "row_locator" in header
    assert all(line.startswith(A.DERIVED_EXPORT_MARKER) for line in lines[1:])
    # legacy rows export a blank execution identity
    legacy_lines = [line for line in lines[1:] if "run_20260901_flat" in line]
    assert legacy_lines and legacy_lines[0].split(",")[1] == ""


# ---------------------------------------------------------------------------
# Real evidence
# ---------------------------------------------------------------------------

def test_real_stats_rebuild_is_non_destructive_and_deterministic(tmp_path):
    if not A.DEFAULT_STATS_ROOT.is_dir():
        pytest.skip("03_STATS not present on this machine")
    db = tmp_path / "real.sqlite"
    first = A.ingest(db, A.DEFAULT_STATS_ROOT, rebuild=True)
    assert first["errors"] == 0
    assert first["validation"]["valid"] is True
    assert first["runs"] > 0 and first["modern_executions"] > 0
    snapshot = A.normalized_snapshot(db)
    A.ingest(db, A.DEFAULT_STATS_ROOT, rebuild=True)
    assert A.normalized_snapshot(db) == snapshot
    # the real evidence has no human quality assessment yet: report it, do not hide it
    api = A.Analytics(db)
    assert api.data_quality()["quality_assessment_coverage"]["n"] == 0
    assert api.analytics_readiness()["human_quality_coverage"] == 0.0
    # no source falls through as an unrecognised layout
    assert api.data_quality()["unknown_layout_sources"] == 0
    assert db != A.DEFAULT_DB_PATH


def test_real_stats_refresh_after_rebuild_changes_nothing(tmp_path):
    if not A.DEFAULT_STATS_ROOT.is_dir():
        pytest.skip("03_STATS not present on this machine")
    db = tmp_path / "real.sqlite"
    A.ingest(db, A.DEFAULT_STATS_ROOT, rebuild=True)
    snapshot = A.normalized_snapshot(db)
    summary = A.ingest(db, A.DEFAULT_STATS_ROOT, rebuild=False)
    assert summary["units_reingested"] == 0
    assert A.normalized_snapshot(db) == snapshot


def test_cross_run_identity_collision_preserves_the_original(tmp_path):
    """A finding key, candidate or decision id claimed by two runs is an error."""
    root = tmp_path / "03_STATS"
    root.mkdir()
    _modern_run(root, run_id="AAW_X1")
    _modern_run(root, run_id="AAW_X2")
    # force the second run to reuse the first run's review/candidate/decision ids
    second = root / "AAW_X2" / "CUSTOM_JOB"
    state = json.loads((second / "job_state.json").read_text(encoding="utf-8"))
    state["review"]["execution_id"] = _ids("AAW_X1")["R2"]
    state["review"]["findings"][0]["finding_key"]["review_execution_id"] = _ids("AAW_X1")["R2"]
    (second / "job_state.json").write_text(json.dumps(state), encoding="utf-8")
    candidate = json.loads((second / "candidate.json").read_text(encoding="utf-8"))
    candidate["candidate_id"] = "CAN_AAW_X1"
    (second / "candidate.json").write_text(json.dumps(candidate), encoding="utf-8")
    decision_path = second / "HDE_AAW_X2.json"
    decision = json.loads(decision_path.read_text(encoding="utf-8"))
    decision["human_decision_id"] = "HDE_AAW_X1"
    decision["candidate_id"] = "CAN_AAW_X1"
    decision_path.write_text(json.dumps(decision), encoding="utf-8")

    db = tmp_path / "index.sqlite"
    A.ingest(db, root, rebuild=True)
    # the first run keeps every contested identity
    assert _one(db, "SELECT run_id FROM review_finding WHERE review_execution_id=?",
                _ids("AAW_X1")["R2"]) == "AAW_X1"
    assert _one(db, "SELECT run_id FROM candidate WHERE candidate_id='CAN_AAW_X1'") == "AAW_X1"
    assert _one(db, "SELECT run_id FROM human_decision WHERE human_decision_id='HDE_AAW_X1'") \
        == "AAW_X1"
    # and each collision is reported rather than silently resolved
    entities = {row["entity"] for row in
                _rows(db, "SELECT entity FROM entity_conflict WHERE field='run_id'")}
    assert {"review_finding", "candidate", "human_decision"} <= entities
    assert _one(db, "SELECT COUNT(*) FROM ingest_diagnostic "
                    "WHERE kind='DATA_INTEGRITY_ERROR'") >= 3
