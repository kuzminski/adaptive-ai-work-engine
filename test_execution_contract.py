from pathlib import Path

import pytest

import hashlib

from execution_contract import (
    ExecutionIdentityError, allocate_execution, canonical_hash, create_candidate,
    create_human_decision, execution_ref, new_execution_id, update_execution,
)


def test_execution_id_format_and_uniqueness():
    values = {new_execution_id() for _ in range(500)}
    assert len(values) == 500
    assert all(value.startswith("EXE_") and len(value) == 36 for value in values)


def test_same_node_retry_and_repair_are_explicit(tmp_path: Path):
    first, p1 = allocate_execution(descriptor_root=tmp_path, run_id="R1", node_id="N01", invocation_kind="LLM")
    retry, _ = allocate_execution(descriptor_root=tmp_path, run_id="R1", node_id="N01", invocation_kind="LLM", retry_of_execution_id=first["execution_id"])
    repair, _ = allocate_execution(descriptor_root=tmp_path, run_id="R1", node_id="N04", invocation_kind="REPAIR")
    assert first["execution_id"] != retry["execution_id"]
    assert retry["retry_of_execution_id"] == first["execution_id"]
    assert repair["retry_of_execution_id"] is None
    assert execution_ref(first, p1)["execution_id"] == first["execution_id"]


def test_forced_execution_id_collision_fails_closed_as_data_integrity_error(tmp_path: Path, monkeypatch):
    fixed = "EXE_" + "0" * 32
    monkeypatch.setattr("execution_contract.new_execution_id", lambda: fixed)

    first, path = allocate_execution(descriptor_root=tmp_path, run_id="R1", node_id="N01", invocation_kind="LLM")
    assert path.name == f"{fixed}.json"
    original_bytes = path.read_bytes()
    original_hash = hashlib.sha256(original_bytes).hexdigest()

    with pytest.raises(ExecutionIdentityError) as excinfo:
        allocate_execution(descriptor_root=tmp_path, run_id="R2", node_id="N09", invocation_kind="REPAIR")

    error = excinfo.value
    # Machine-recognisable classification, not a retryable operational error.
    assert error.classification == "DATA_INTEGRITY_ERROR"
    assert "DATA_INTEGRITY_ERROR" in str(error)
    assert not isinstance(error, FileExistsError)
    # Conflicting execution_id is available diagnostically.
    assert error.execution_id == fixed
    assert fixed in str(error)
    assert error.path == str(path)
    # No overwrite / no merge / no reuse: the existing artifact is byte-identical.
    assert path.read_bytes() == original_bytes
    assert hashlib.sha256(path.read_bytes()).hexdigest() == original_hash
    assert list(tmp_path.glob("*.json")) == [path]


def test_provider_session_timestamp_model_and_node_are_not_identity(tmp_path: Path):
    common = dict(descriptor_root=tmp_path, node_id="N01", invocation_kind="LLM", provider="OPENAI", model="same", profile="same", provider_session_id="same")
    a, _ = allocate_execution(run_id="R1", **common)
    b, _ = allocate_execution(run_id="R2", **common)
    c, _ = allocate_execution(run_id="R1", **common)
    assert len({a["execution_id"], b["execution_id"], c["execution_id"]}) == 3


def test_subtask_is_explicit_and_not_filename_derived(tmp_path: Path):
    row, path = allocate_execution(descriptor_root=tmp_path, run_id="R", node_id="CUSTOM_NODE", subtask_id="invoice-7", invocation_kind="LLM")
    assert row["subtask_id"] == "invoice-7"
    assert "invoice-7" not in path.name


def test_review_finding_scope_and_lineage(tmp_path: Path):
    producer, _ = allocate_execution(descriptor_root=tmp_path, run_id="R", node_id="N01", invocation_kind="LLM")
    review1, _ = allocate_execution(descriptor_root=tmp_path, run_id="R", node_id="N03", invocation_kind="REVIEW", relations={"reviewed_execution_ids": [producer["execution_id"]]})
    review2, _ = allocate_execution(descriptor_root=tmp_path, run_id="R", node_id="N03", invocation_kind="REVIEW", relations={"reviewed_execution_ids": [producer["execution_id"]]})
    assert (review1["execution_id"], "F001") != (review2["execution_id"], "F001")


def test_preprocess_identity_is_distinct_and_repeat_safe(tmp_path: Path):
    downstream, _ = allocate_execution(descriptor_root=tmp_path, run_id="R", node_id="N03", invocation_kind="REVIEW")
    p1, _ = allocate_execution(descriptor_root=tmp_path, run_id="R", node_id="N03:PRE", invocation_kind="PREPROCESS", relations={"preprocess_id": "PRE_A", "downstream_execution_id": downstream["execution_id"]})
    p2, _ = allocate_execution(descriptor_root=tmp_path, run_id="R", node_id="N03:PRE", invocation_kind="PREPROCESS", relations={"preprocess_id": "PRE_B", "downstream_execution_id": downstream["execution_id"]})
    assert p1["execution_id"] != p2["execution_id"]
    assert p1["execution_id"] != p1["relations"]["preprocess_id"]


def test_machine_gate_has_own_identity(tmp_path: Path):
    gate, _ = allocate_execution(descriptor_root=tmp_path, run_id="R", node_id="N02", invocation_kind="MACHINE_GATE", harness="subprocess")
    assert gate["execution_id"].startswith("EXE_")


def test_descriptor_update_preserves_identity(tmp_path: Path):
    row, path = allocate_execution(descriptor_root=tmp_path, run_id="R", node_id="N", invocation_kind="LLM")
    updated = update_execution(path, row["execution_id"], status="COMPLETED", provider_session_id="S")
    assert updated["execution_id"] == row["execution_id"] and updated["provider_session_id"] == "S"
    with pytest.raises(ValueError):
        update_execution(path, row["execution_id"], node_id="OTHER")


def test_candidate_and_human_decision_exact_reference(tmp_path: Path):
    candidate = create_candidate(path=tmp_path / "candidate.json", run_id="R", repository="repo", worktree="wt", candidate_head="abc", artifact_manifest=None, review_execution_ids=["EXE_r"], check_execution_ids=["EXE_g"])
    decision = create_human_decision(path=tmp_path / "decision.json", candidate_id=candidate["candidate_id"], verdict="ACCEPTED")
    assert decision["candidate_id"] == candidate["candidate_id"]
    assert candidate["content_identity_hash"] == canonical_hash({k: v for k, v in candidate.items() if k not in {"candidate_id", "created_at", "content_identity_hash"}})
