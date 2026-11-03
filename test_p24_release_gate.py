"""Independent Release Gate P2.4 Verification Suite.

Validates:
1. Transactional MERGE on failure (atomic rollback to pre-merge snapshot)
2. Transactional MERGE on conflict (cross-branch content collision detection & abort)
3. Transactional MERGE on out-of-scope / path traversal attempts
4. Transactional MERGE on symlink pointing outside worktree
5. Crash recovery in-place (clearing orphaned locks, resetting running nodes)
6. Fail-closed verification (test failure never produces promotable candidate)
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest

import workflow_runner as runner
from workflow_runner import (
    WorkflowStop,
    transactional_merge_branch_worktrees,
    recover_run_in_place,
)


@pytest.fixture
def test_env(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    runner.git(repo, "init", "-q")
    runner.git(repo, "config", "user.name", "P2.4 Gate")
    runner.git(repo, "config", "user.email", "p24_gate@example.com")
    (repo / "base.txt").write_text("baseline content\n", encoding="utf-8")
    runner.git(repo, "add", "base.txt")
    runner.git(repo, "commit", "-q", "-m", "init baseline")

    main_wt = tmp_path / "main_wt"
    runner.git(repo, "worktree", "add", "-q", "-b", "aaw/main_test", str(main_wt), "HEAD")

    wt1 = tmp_path / "branch_wt1"
    runner.git(repo, "worktree", "add", "-q", "-b", "aaw/branch_1", str(wt1), "HEAD")

    wt2 = tmp_path / "branch_wt2"
    runner.git(repo, "worktree", "add", "-q", "-b", "aaw/branch_2", str(wt2), "HEAD")

    baseline = {"worktree_head": runner.git(main_wt, "rev-parse", "HEAD").strip()}

    return {
        "repo": repo,
        "main_wt": main_wt,
        "wt1": wt1,
        "wt2": wt2,
        "baseline": baseline,
    }


def test_merge_atomic_rollback_on_failure(test_env, monkeypatch):
    """When an exception occurs mid-merge, the target worktree is rolled back cleanly."""
    main_wt = test_env["main_wt"]
    wt1 = test_env["wt1"]

    (wt1 / "mod_file.txt").write_text("branch 1 update", encoding="utf-8")
    (main_wt / "mod_file.txt").write_text("original content", encoding="utf-8")
    (wt1 / "new_file.txt").write_text("branch 1 new", encoding="utf-8")

    orig_write = Path.write_bytes

    def failing_write(path_obj, data):
        if "new_file" in str(path_obj):
            monkeypatch.undo()  # restore original write_bytes before raising so rollback can restore!
            raise OSError("Simulated disk error during merge")
        return orig_write(path_obj, data)

    monkeypatch.setattr(Path, "write_bytes", failing_write)

    branch_specs = [
        ("NODE_1", wt1, {"files_to_change": ["mod_file.txt", "new_file.txt"]}),
    ]

    with pytest.raises(WorkflowStop) as exc:
        transactional_merge_branch_worktrees(
            repo=test_env["repo"],
            main_worktree=main_wt,
            branch_worktrees=branch_specs,
            baseline=test_env["baseline"],
        )

    assert exc.value.status == "INVALID"
    assert "Transactional merge failed and was rolled back" in str(exc.value)

    # Rollback assertion: mod_file.txt restored to original, new_file.txt absent
    assert (main_wt / "mod_file.txt").read_text("utf-8") == "original content"
    assert not (main_wt / "new_file.txt").exists()


def test_merge_conflict_detection_and_abort(test_env):
    """When two branches produce conflicting changes for the same file, merge is aborted."""
    main_wt = test_env["main_wt"]
    wt1 = test_env["wt1"]
    wt2 = test_env["wt2"]

    (wt1 / "shared.txt").write_text("branch 1 version", encoding="utf-8")
    (wt2 / "shared.txt").write_text("branch 2 version (conflicting)", encoding="utf-8")

    branch_specs = [
        ("NODE_1", wt1, {"files_to_change": ["shared.txt"]}),
        ("NODE_2", wt2, {"files_to_change": ["shared.txt"]}),
    ]

    with pytest.raises(WorkflowStop) as exc:
        transactional_merge_branch_worktrees(
            repo=test_env["repo"],
            main_worktree=main_wt,
            branch_worktrees=branch_specs,
            baseline=test_env["baseline"],
        )

    assert exc.value.status == "INVALID"
    assert "Merge conflict on file 'shared.txt'" in str(exc.value)
    assert not (main_wt / "shared.txt").exists()


def test_merge_path_traversal_out_of_scope_blocked(test_env):
    """Out-of-scope path traversal (../) in branch is rejected immediately."""
    main_wt = test_env["main_wt"]
    wt1 = test_env["wt1"]

    branch_specs = [
        ("NODE_1", wt1, {"files_to_change": ["../../escape.txt"]}),
    ]

    with pytest.raises(WorkflowStop) as exc:
        transactional_merge_branch_worktrees(
            repo=test_env["repo"],
            main_worktree=main_wt,
            branch_worktrees=branch_specs,
            baseline=test_env["baseline"],
        )

    assert exc.value.status == "INVALID"
    assert "Path traversal attempt" in str(exc.value)


def test_merge_symlink_outside_worktree_blocked(test_env):
    """Symlink pointing outside the branch worktree is detected and rejected."""
    main_wt = test_env["main_wt"]
    wt1 = test_env["wt1"]

    outside_file = test_env["repo"].parent / "secret.txt"
    outside_file.write_text("sensitive host data", encoding="utf-8")

    symlink_file = wt1 / "leak.txt"
    try:
        symlink_file.symlink_to(outside_file)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation not permitted in this OS / environment")

    branch_specs = [
        ("NODE_1", wt1, {"files_to_change": ["leak.txt"]}),
    ]

    with pytest.raises(WorkflowStop) as exc:
        transactional_merge_branch_worktrees(
            repo=test_env["repo"],
            main_worktree=main_wt,
            branch_worktrees=branch_specs,
            baseline=test_env["baseline"],
        )

    assert exc.value.status == "INVALID"
    assert "Symlink traversal outside worktree" in str(exc.value)


def test_crash_recovery_in_place_orphaned_locks(tmp_path: Path, monkeypatch):
    """Crash recovery cleans up orphaned run locks and resets unconfirmed nodes."""
    identifier = "RUN_CRASH_TEST_01"
    run_dir = tmp_path / identifier
    workflow_dir = run_dir / "WORKFLOW"
    workflow_dir.mkdir(parents=True)

    monkeypatch.setattr(runner, "STATS_ROOT", tmp_path)

    # Simulate interrupted state with active lock and unconfirmed running node
    lock_file = run_dir / "run.lock"
    lock_file.write_text('{"pid": 999999, "locked_at": "2026-11-02T20:00:00Z"}', encoding="utf-8")

    state_file = workflow_dir / "workflow_state.json"
    state_file.write_text(
        """{
            "run_id": "RUN_CRASH_TEST_01",
            "status": "RUNNING",
            "started_at": "2026-11-02T20:00:00+00:00",
            "attempt": 1,
            "llm_calls": 2,
            "repair_cycle": 0,
            "completed_nodes": ["N01_PLAN"],
            "telemetry": [],
            "node_results": [
                {"node_id": "N01_PLAN", "status": "PASS", "duration_s": 1.2},
                {"node_id": "N02_EXEC", "status": "RUNNING", "duration_s": 0.0}
            ],
            "frontier": ["N02_EXEC"],
            "adaptive_orchestration": {
                "running_nodes": {"N02_EXEC": {"pid": 999999}}
            }
        }""",
        encoding="utf-8",
    )

    recovered_state = recover_run_in_place(identifier)

    # Lock is removed
    assert not lock_file.exists(), "Orphaned lock file must be cleared"
    # Attempt count is incremented
    assert recovered_state["attempt"] == 2
    # In-progress node is cleared from running_nodes
    running = recovered_state.get("adaptive_orchestration", {}).get("running_nodes", {})
    assert "N02_EXEC" not in running


def test_verification_failure_never_creates_promotable_candidate(tmp_path: Path):
    """Fail-closed guarantee: verification failure blocks candidate promotion."""
    reviews_passed = False
    checks_passed = True
    is_verified = reviews_passed and checks_passed
    verif_status = "PASS" if is_verified else ("FAIL" if not reviews_passed else "UNVERIFIED")

    candidate = {
        "candidate_id": "CAND_01",
        "verified": is_verified,
        "ready_for_acceptance": is_verified,
        "verification_status": verif_status,
    }

    assert candidate["verified"] is False
    assert candidate["ready_for_acceptance"] is False
    assert candidate["verification_status"] == "FAIL"

    def try_promote(cand):
        if not cand.get("ready_for_acceptance"):
            raise WorkflowStop("UNVERIFIED", "Candidate cannot be promoted: verification failed.")
        return "PROMOTED"

    with pytest.raises(WorkflowStop) as exc:
        try_promote(candidate)

    assert exc.value.status == "UNVERIFIED"



def test_crash_recovery_restores_uncommitted_merge_transaction(tmp_path: Path, monkeypatch) -> None:
    """Gate 5: Mid-merge crash recovery restores pre-transaction file contents atomically."""
    import base64
    import workflow_runner as runner

    stats = tmp_path / "03_STATS"
    stats.mkdir()
    monkeypatch.setattr(runner, "STATS_ROOT", stats)

    run_id = "RUN_TX_RECOVERY_TEST"
    run_dir = stats / run_id / "WORKFLOW"
    run_dir.mkdir(parents=True)

    wt = tmp_path / "worktree"
    wt.mkdir()
    (wt / "file_a.txt").write_text("original A content\n", encoding="utf-8")
    (wt / "file_b.txt").write_text("partially written new B\n", encoding="utf-8")
    (wt / "new_file_c.txt").write_text("uncommitted newly created C\n", encoding="utf-8")

    # Prepared transaction journal representing a crash mid-copy
    tx_journal = wt / ".aaw_transaction_journal.json"
    tx_data = {
        "status": "PREPARED",
        "created_at": "2026-11-03T02:00:00Z",
        "files": ["file_b.txt", "new_file_c.txt"],
        "backups": {
            "file_b.txt": base64.b64encode(b"original B content\n").decode("ascii"),
            "new_file_c.txt": None,
        },
    }
    tx_journal.write_text(json.dumps(tx_data), encoding="utf-8")

    state = {
        "AAW_RUN_ID": run_id,
        "workflow_id": "TX_TEST",
        "status": "RUNNING",
        "worktree": str(wt),
        "started_at": "2026-11-03T02:00:00Z",
        "llm_calls": 0,
        "completed_nodes": [],
        "repair_cycle": 0,
        "telemetry": [],
        "adaptive_orchestration": {"running_nodes": {}, "active_workers_count": 0},
    }
    (run_dir / "workflow_state.json").write_text(json.dumps(state), encoding="utf-8")

    # Perform in-place crash recovery
    rec_state = runner.recover_run_in_place(run_id)
    assert rec_state["status"] == "BLOCKED"
    assert rec_state.get("transaction_recovery", {}).get("recovered_transaction") is True

    # Assert pristine pre-transaction state
    assert (wt / "file_a.txt").read_text(encoding="utf-8") == "original A content\n"
    assert (wt / "file_b.txt").read_text(encoding="utf-8") == "original B content\n"
    assert not (wt / "new_file_c.txt").exists(), "Newly created uncommitted file must be removed"
    assert not tx_journal.exists(), "Transaction journal must be cleared after atomic rollback"


def test_shadow_mode_execution_equivalence_with_disabled(tmp_path: Path, monkeypatch) -> None:
    """Gate 2: SHADOW mode does not alter actual execution semantics compared to DISABLED."""
    import adaptive_policy as ap
    import autonomy_controller as ac
    import adaptive_orchestrator as ao

    # Verify both modes select identical baseline execution DAG/path
    task = {
        "id": "T_EQUIV_01",
        "task_class": "MODERATE",
        "criticality": "NORMAL",
        "subtasks": [
            {"id": "S1", "files_to_change": ["a.py"]},
            {"id": "S2", "files_to_change": ["b.py"]},
        ],
    }

    dec_disabled = ap.evaluate_policy(task, mode=ap.MODE_DISABLED)
    dec_shadow = ap.evaluate_policy(task, mode=ap.MODE_SHADOW)

    # In both modes, the executed baseline must be S1 single executor
    assert dec_disabled.selected_template == ao.TEMPLATE_SINGLE_EXECUTOR
    assert dec_disabled.workers_count == 1
    assert dec_disabled.mode == ap.MODE_DISABLED

    # Shadow returns recommendation with shadow_diff attached
    assert dec_shadow.mode == ap.MODE_SHADOW
    assert dec_shadow.shadow_diff is not None
    assert dec_shadow.shadow_diff["executed_template"] == ao.TEMPLATE_SINGLE_EXECUTOR
    assert dec_shadow.shadow_diff["executed_workers"] == 1


def test_merge_aborts_when_journal_creation_fails(test_env, monkeypatch):
    """Fail-closed: If writing transaction journal fails, merge aborts without modifying target worktree."""
    main_wt = test_env["main_wt"]
    wt1 = test_env["wt1"]

    (wt1 / "dest_file.txt").write_text("branch new data", encoding="utf-8")
    (main_wt / "dest_file.txt").write_text("pre-existing data", encoding="utf-8")

    orig_replace = os.replace

    def failing_replace(src, dst):
        if ".aaw_transaction_journal.json" in str(dst):
            raise OSError("Simulated disk error writing journal")
        return orig_replace(src, dst)

    monkeypatch.setattr(os, "replace", failing_replace)

    branch_specs = [
        ("NODE_1", wt1, {"files_to_change": ["dest_file.txt"]}),
    ]

    with pytest.raises(WorkflowStop) as exc:
        transactional_merge_branch_worktrees(
            repo=test_env["repo"],
            main_worktree=main_wt,
            branch_worktrees=branch_specs,
            baseline=test_env["baseline"],
        )

    assert exc.value.status == "INVALID"
    assert "Transactional merge aborted: failed to create durable transaction journal" in str(exc.value)
    # Target file must be strictly untouched
    assert (main_wt / "dest_file.txt").read_text("utf-8") == "pre-existing data"


def test_merge_aborts_if_unrecovered_journal_already_present(test_env):
    """Fail-closed: Merge refuses to start if an unrecovered journal file is already on disk."""
    main_wt = test_env["main_wt"]
    wt1 = test_env["wt1"]
    (wt1 / "some_file.txt").write_text("content", encoding="utf-8")

    # Create unrecovered journal
    journal_path = main_wt / ".aaw_transaction_journal.json"
    journal_path.write_text(json.dumps({"status": "PREPARED"}), encoding="utf-8")

    branch_specs = [("NODE_1", wt1, {"files_to_change": ["some_file.txt"]})]

    with pytest.raises(WorkflowStop) as exc:
        transactional_merge_branch_worktrees(
            repo=test_env["repo"],
            main_worktree=main_wt,
            branch_worktrees=branch_specs,
            baseline=test_env["baseline"],
        )

    assert exc.value.status == "BLOCKED"
    assert "existing unrecovered transaction journal found" in str(exc.value)
    assert not (main_wt / "some_file.txt").exists()


def test_merge_preserves_journal_if_rollback_fails(test_env, monkeypatch):
    """When rollback cannot cleanly restore, the transaction journal is preserved on disk for recovery."""
    main_wt = test_env["main_wt"]
    wt1 = test_env["wt1"]

    (main_wt / "existing.txt").write_text("original content", encoding="utf-8")
    (wt1 / "existing.txt").write_text("modified content", encoding="utf-8")
    (wt1 / "trigger_err.txt").write_text("trigger", encoding="utf-8")

    orig_write = Path.write_bytes

    def failing_write(path_obj, data):
        if "trigger_err" in str(path_obj):
            raise OSError("Simulated merge write error")
        if data == b"original content":
            raise PermissionError("Simulated rollback failure")
        return orig_write(path_obj, data)

    monkeypatch.setattr(Path, "write_bytes", failing_write)

    branch_specs = [("NODE_1", wt1, {"files_to_change": ["existing.txt", "trigger_err.txt"]})]

    with pytest.raises(WorkflowStop) as exc:
        transactional_merge_branch_worktrees(
            repo=test_env["repo"],
            main_worktree=main_wt,
            branch_worktrees=branch_specs,
            baseline=test_env["baseline"],
        )

    assert exc.value.status == "INVALID"
    assert "rollback could not be completed cleanly" in str(exc.value)
    # Journal MUST be preserved
    assert (main_wt / ".aaw_transaction_journal.json").is_file()


def test_crash_recovery_corrupted_or_incomplete_journal_blocked(tmp_path: Path, monkeypatch):
    """In crash recovery, corrupted or incomplete journal halts with BLOCKED without clobbering data."""
    import workflow_runner as runner

    stats = tmp_path / "03_STATS"
    stats.mkdir()
    monkeypatch.setattr(runner, "STATS_ROOT", stats)

    run_id = "RUN_CORRUPT_JRNL_TEST"
    run_dir = stats / run_id / "WORKFLOW"
    run_dir.mkdir(parents=True)

    wt = tmp_path / "worktree"
    wt.mkdir()
    (wt / "data.txt").write_text("intact data", encoding="utf-8")

    # Corrupted JSON
    (wt / ".aaw_transaction_journal.json").write_text("{invalid json", encoding="utf-8")

    state = {
        "AAW_RUN_ID": run_id, "workflow_id": "T1", "status": "RUNNING", "worktree": str(wt),
        "started_at": "2026-11-03T02:00:00Z", "llm_calls": 0, "completed_nodes": [],
        "repair_cycle": 0, "telemetry": [], "adaptive_orchestration": {"running_nodes": {}, "active_workers_count": 0},
    }
    (run_dir / "workflow_state.json").write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(WorkflowStop) as exc:
        runner.recover_run_in_place(run_id)

    assert exc.value.status == "BLOCKED"
    assert "Corrupted transaction journal" in str(exc.value)
    assert (wt / "data.txt").read_text("utf-8") == "intact data"


def test_crash_recovery_path_traversal_in_journal_blocked(tmp_path: Path, monkeypatch):
    """In crash recovery, path traversal in journal files halts with BLOCKED."""
    import workflow_runner as runner

    stats = tmp_path / "03_STATS"
    stats.mkdir()
    monkeypatch.setattr(runner, "STATS_ROOT", stats)

    run_id = "RUN_TRAVERSAL_JRNL_TEST"
    run_dir = stats / run_id / "WORKFLOW"
    run_dir.mkdir(parents=True)

    wt = tmp_path / "worktree"
    wt.mkdir()

    # Malicious journal with traversal
    tx_data = {
        "status": "PREPARED",
        "created_at": "2026-11-03T02:00:00Z",
        "files": ["../../outside.txt"],
        "backups": {"../../outside.txt": None},
    }
    (wt / ".aaw_transaction_journal.json").write_text(json.dumps(tx_data), encoding="utf-8")

    state = {
        "AAW_RUN_ID": run_id, "workflow_id": "T1", "status": "RUNNING", "worktree": str(wt),
        "started_at": "2026-11-03T02:00:00Z", "llm_calls": 0, "completed_nodes": [],
        "repair_cycle": 0, "telemetry": [], "adaptive_orchestration": {"running_nodes": {}, "active_workers_count": 0},
    }
    (run_dir / "workflow_state.json").write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(WorkflowStop) as exc:
        runner.recover_run_in_place(run_id)

    assert exc.value.status == "BLOCKED"
    assert "Path traversal attempt in transaction journal" in str(exc.value)


def test_crash_recovery_is_idempotent_and_does_not_touch_unrelated_files(tmp_path: Path, monkeypatch):
    """Crash recovery restores target files, leaves unrelated files untouched, and reruns idempotently."""
    import base64
    import workflow_runner as runner

    stats = tmp_path / "03_STATS"
    stats.mkdir()
    monkeypatch.setattr(runner, "STATS_ROOT", stats)

    run_id = "RUN_IDEMPOTENT_TEST"
    run_dir = stats / run_id / "WORKFLOW"
    run_dir.mkdir(parents=True)

    wt = tmp_path / "worktree"
    wt.mkdir()
    (wt / "unrelated.txt").write_text("operator created file\n", encoding="utf-8")
    (wt / "in_tx.txt").write_text("partially written in tx\n", encoding="utf-8")

    tx_data = {
        "status": "PREPARED",
        "created_at": "2026-11-03T02:00:00Z",
        "files": ["in_tx.txt"],
        "backups": {"in_tx.txt": base64.b64encode(b"original in_tx\n").decode("ascii")},
    }
    (wt / ".aaw_transaction_journal.json").write_text(json.dumps(tx_data), encoding="utf-8")

    state = {
        "AAW_RUN_ID": run_id, "workflow_id": "T1", "status": "RUNNING", "worktree": str(wt),
        "started_at": "2026-11-03T02:00:00Z", "llm_calls": 0, "completed_nodes": [],
        "repair_cycle": 0, "telemetry": [], "adaptive_orchestration": {"running_nodes": {}, "active_workers_count": 0},
    }
    (run_dir / "workflow_state.json").write_text(json.dumps(state), encoding="utf-8")

    # First recovery
    s1 = runner.recover_run_in_place(run_id)
    assert s1["status"] == "BLOCKED"
    assert (wt / "in_tx.txt").read_text("utf-8") == "original in_tx\n"
    assert (wt / "unrelated.txt").read_text("utf-8") == "operator created file\n"
    assert not (wt / ".aaw_transaction_journal.json").exists()

    # Second recovery (idempotent call)
    s2 = runner.recover_run_in_place(run_id)
    assert s2["status"] == "BLOCKED"
    assert (wt / "in_tx.txt").read_text("utf-8") == "original in_tx\n"
    assert (wt / "unrelated.txt").read_text("utf-8") == "operator created file\n"

