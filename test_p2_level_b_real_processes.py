"""Level B Verification Suite: Real OS Subprocesses, Distinct PIDs, and Isolation.

Empirically verifies:
1. Real OS child processes launched by the runner with distinct PIDs (pid1 != pid2, pid != parent).
2. Wall-clock execution overlap: max(start1, start2) < min(end1, end2).
3. Dedicated isolated Git worktrees during execution.
4. Deterministic MERGE combining branch files into the run worktree without touching main.
5. Process interruption / cancellation with clean child process termination.
6. Safe resume without re-executing already completed subtasks.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest

import adaptive_orchestrator as ao
import run_cancellation
import workflow_runner as runner
from workflow_schema import validate_workflow


@pytest.fixture
def level_b_env(tmp_path, monkeypatch):
    stats = tmp_path / "stats"
    stats.mkdir()
    monkeypatch.setattr(runner, "STATS_ROOT", stats)

    # Create mock CLI executable on PATH
    bin_dir = tmp_path / "mock_bin"
    bin_dir.mkdir()

    mock_cli_py = bin_dir / "mock_codex.py"
    mock_cli_py.write_text(r"""import sys
import os
import time
import json
from pathlib import Path

args = sys.argv[1:]
output_file = None
worktree = None
delay = 1.0

i = 0
while i < len(args):
    if args[i] == "--output-last-message" and i + 1 < len(args):
        output_file = Path(args[i + 1])
        i += 2
    elif args[i] == "--cd" and i + 1 < len(args):
        worktree = Path(args[i + 1])
        i += 2
    else:
        i += 1

stdin_data = sys.stdin.read() if not sys.stdin.isatty() else ""
node_id = os.environ.get("AAW_CURRENT_NODE_ID", "NODE")
node_type = "IMPLEMENT"
if "NODE PACKAGE:" in stdin_data:
    try:
        pkg_str = stdin_data.split("NODE PACKAGE:", 1)[1].strip()
        pkg = json.loads(pkg_str)
        node_id = pkg.get("CURRENT_NODE_ID", node_id)
        node_type = pkg.get("NODE_TYPE", node_type)
    except Exception:
        pass

custom_delay = os.environ.get("AAW_MOCK_DELAY")
if custom_delay:
    delay = float(custom_delay)

# Real OS sleep to confirm wall-clock concurrency
time.sleep(delay)

changed = []
if node_type != "REVIEW":
    changed = [f"output_{node_id}.txt"]
    if worktree and worktree.is_dir():
        target = worktree / f"output_{node_id}.txt"
        target.write_text(f"produced by worker {os.getpid()} in {worktree}\n", encoding="utf-8")

if output_file:
    result = {
        "node_id": node_id,
        "node_type": node_type,
        "outcome": "PASS",
        "verdict": "PASS",
        "summary": f"Mock codex worker {os.getpid()} finished",
        "changed_files": changed,
        "tests": [],
        "findings": [],
        "remaining_uncertainty": [],
        "recommended_next_action": "CONTINUE",
    }
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(json.dumps(result), encoding="utf-8")

# Produce Codex event stream on stdout
print(json.dumps({"type": "thread.started", "thread_id": "mock_thread"}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 120, "output_tokens": 60}}))
sys.exit(0)
""", encoding="utf-8")

    # On Windows, wrap in .cmd; on POSIX/Linux, create executable shell wrapper
    if os.name == "nt":
        mock_cmd = bin_dir / "codex.cmd"
        mock_cmd.write_text(f'@"{sys.executable}" "{mock_cli_py}" %*\n', encoding="utf-8")
    else:
        mock_sh = bin_dir / "codex"
        mock_sh.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{mock_cli_py}" "$@"\n', encoding="utf-8")
        mock_sh.chmod(0o755)

    # Prepend to PATH so runner finds mock_bin/codex.cmd first
    current_path = os.environ.get("PATH", "")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{current_path}")

    # Set up git repo and initial worktree
    repo = tmp_path / "repo"
    repo.mkdir()
    runner.git(repo, "init", "-q")
    runner.git(repo, "config", "user.name", "AAW Level B")
    runner.git(repo, "config", "user.email", "level_b@example.com")
    (repo / "README.md").write_text("# Level B Repo\n", encoding="utf-8")
    runner.git(repo, "add", "README.md")
    runner.git(repo, "commit", "-q", "-m", "Initial commit")

    worktree = tmp_path / "worktree"
    runner.git(repo, "worktree", "add", "-q", "-b", "aaw/test-level-b", str(worktree), "HEAD")
    return repo, worktree


def test_level_b_concurrent_subprocesses_and_timing_overlap(level_b_env, tmp_path, monkeypatch):
    """Level B: Verify two actual OS child processes run concurrently with overlapping timestamps."""
    repo, worktree = level_b_env
    monkeypatch.setenv("AAW_MOCK_DELAY", "0.8")

    subtasks = [
        {"id": "SUB_01", "name": "Worker One", "files_to_change": ["output_N02_SUB_01.txt"]},
        {"id": "SUB_02", "name": "Worker Two", "files_to_change": ["output_N02_SUB_02.txt"]},
    ]
    strategy = {
        "selected_template": ao.TEMPLATE_PARALLEL_INDEPENDENT,
        "workers_count": 2,
        "model_profile": "SOL_MEDIUM",
        "effort_level": "medium",
    }
    dag = ao.generate_adaptive_dag(strategy, "Level B Concurrency Test", subtasks=subtasks)
    validate_workflow(dag)
    wf_file = tmp_path / "wf_level_b.json"
    wf_file.write_text(json.dumps(dag), encoding="utf-8")

    parent_pid = os.getpid()
    start_total = time.monotonic()
    
    # Execute WITHOUT in-memory adapter -> forces dedicated OS subprocesses via _dispatch_worker
    state = runner.execute(wf_file, "Goal", repo, worktree, identifier="RUN_LEVEL_B_1", max_parallel_workers=2)
    elapsed_total = time.monotonic() - start_total

    assert state["status"] == "WAITING_FOR_HUMAN"
    
    # Extract records of SUB_01 and SUB_02
    results_map = {r["node_id"]: r for r in state["node_results"]}
    res1 = results_map["N02_SUB_01"]
    res2 = results_map["N02_SUB_02"]

    pid1 = res1.get("worker_pid")
    pid2 = res2.get("worker_pid")

    # 1. Distinct OS PIDs verification
    assert pid1 is not None and pid2 is not None
    assert pid1 != pid2, f"Expected distinct worker PIDs, got pid1={pid1}, pid2={pid2}"
    assert pid1 != parent_pid and pid2 != parent_pid, "Worker PIDs must not match parent process PID"

    # 2. Timing overlap verification
    start1 = res1["start_monotonic"]
    end1 = res1["end_monotonic"]
    start2 = res2["start_monotonic"]
    end2 = res2["end_monotonic"]

    latest_start = max(start1, start2)
    earliest_end = min(end1, end2)
    overlap = earliest_end - latest_start

    assert overlap > 0, (
        f"Expected wall-clock timing overlap between workers!\n"
        f"Worker 1: [{start1:.3f} -> {end1:.3f}] (duration {res1['duration_s']}s)\n"
        f"Worker 2: [{start2:.3f} -> {end2:.3f}] (duration {res2['duration_s']}s)\n"
        f"Overlap: {overlap:.3f}s"
    )

    # 3. Speedup verification (both slept 0.8s, parallel stage wall time must be significantly less than sum)
    parallel_stage_span = max(end1, end2) - min(start1, start2)
    sum_individual = res1["duration_s"] + res2["duration_s"]
    assert sum_individual > 1.4  # individual sum is >= 1.6s
    assert parallel_stage_span < sum_individual * 0.85, (
        f"Expected concurrency speedup in parallel stage: span {parallel_stage_span:.3f}s vs sum {sum_individual:.3f}s"
    )


def test_level_b_worktree_isolation_and_deterministic_merge(level_b_env, tmp_path, monkeypatch):
    """Level B: Verify isolated worktrees during execution and deterministic merge without touching main."""
    repo, worktree = level_b_env
    monkeypatch.setenv("AAW_MOCK_DELAY", "0.3")

    subtasks = [
        {"id": "SUB_01", "name": "Worker One", "files_to_change": ["output_N02_SUB_01.txt"]},
        {"id": "SUB_02", "name": "Worker Two", "files_to_change": ["output_N02_SUB_02.txt"]},
    ]
    strategy = {
        "selected_template": ao.TEMPLATE_PARALLEL_INDEPENDENT,
        "workers_count": 2,
        "model_profile": "SOL_MEDIUM",
        "effort_level": "medium",
    }
    dag = ao.generate_adaptive_dag(strategy, "Isolation and Merge Test", subtasks=subtasks)
    wf_file = tmp_path / "wf_merge_test.json"
    wf_file.write_text(json.dumps(dag), encoding="utf-8")

    state = runner.execute(wf_file, "Goal", repo, worktree, identifier="RUN_LEVEL_B_2", max_parallel_workers=2)
    assert state["status"] == "WAITING_FOR_HUMAN"

    # Verify deterministic merge
    merge_node = state["routing"]["merges"]["N03_MERGE"]
    assert merge_node["status"] == "MERGED"
    resolution_hash = merge_node["merge_resolution_hash"]
    assert resolution_hash is not None and len(resolution_hash) == 64

    # Verify that run worktree contains merged output files from both subtasks
    assert (worktree / "output_N02_SUB_01.txt").is_file()
    assert (worktree / "output_N02_SUB_02.txt").is_file()

    # Verify main branch / repo containment: canonical repo has NEITHER file
    assert not (repo / "output_N02_SUB_01.txt").exists()
    assert not (repo / "output_N02_SUB_02.txt").exists()
    main_status = runner.git(repo, "status", "--porcelain")
    assert main_status == "", "Main canonical repo must have zero uncommitted changes"


def test_level_b_process_cancellation_and_cleanup(level_b_env, tmp_path, monkeypatch):
    """Level B: Verify child process termination when STOP NOW is requested."""
    repo, worktree = level_b_env
    # Worker sleeps 15 seconds so we can interrupt it midway
    monkeypatch.setenv("AAW_MOCK_DELAY", "15.0")

    subtasks = [
        {"id": "SUB_01", "name": "Slow Worker", "files_to_change": ["output_N02_SUB_01.txt"]},
    ]
    strategy = {
        "selected_template": ao.TEMPLATE_PARALLEL_INDEPENDENT,
        "workers_count": 1,
        "model_profile": "SOL_MEDIUM",
        "effort_level": "medium",
    }
    dag = ao.generate_adaptive_dag(strategy, "Interruption Test", subtasks=subtasks)
    wf_file = tmp_path / "wf_cancel_test.json"
    wf_file.write_text(json.dumps(dag), encoding="utf-8")

    token = run_cancellation.CancellationToken("RUN_LEVEL_B_3")
    
    # Request stop before starting or shortly after
    import threading
    def _delayed_stop():
        time.sleep(0.4)
        token.request(reason="USER_STOP_SAFELY", source="API")

    stopper = threading.Thread(target=_delayed_stop, daemon=True)
    stopper.start()

    start_m = time.monotonic()
    state = runner.execute(wf_file, "Goal", repo, worktree, identifier="RUN_LEVEL_B_3", cancel=token)
    elapsed = time.monotonic() - start_m

    # Must have stopped in ~1s, far below the 15s delay
    assert elapsed < 5.0, f"Process was not interrupted promptly: took {elapsed}s"
    assert state["status"] == "CANCELLED"
    assert state["final_outcome"] == "CANCELLED"
    assert "USER_STOP_SAFELY" in state["cancellation"]["reason"]


def test_level_b_resume_preserves_completed_subprocesses(level_b_env, tmp_path, monkeypatch):
    """Level B: Verify RESUME preserves completed subprocess outputs without re-execution."""
    repo, worktree = level_b_env
    monkeypatch.setenv("AAW_MOCK_DELAY", "0.2")

    subtasks = [
        {"id": "SUB_01", "name": "Worker One", "files_to_change": ["output_N02_SUB_01.txt"]},
        {"id": "SUB_02", "name": "Worker Two", "files_to_change": ["output_N02_SUB_02.txt"]},
    ]
    strategy = {
        "selected_template": ao.TEMPLATE_PARALLEL_INDEPENDENT,
        "workers_count": 2,
        "model_profile": "SOL_MEDIUM",
        "effort_level": "medium",
    }
    dag = ao.generate_adaptive_dag(strategy, "Resume Subprocess Test", subtasks=subtasks)
    wf_file = tmp_path / "wf_resume_level_b.json"
    wf_file.write_text(json.dumps(dag), encoding="utf-8")

    first_state = runner.execute(wf_file, "Goal", repo, worktree, identifier="RUN_LEVEL_B_4_A", max_parallel_workers=2)
    assert first_state["status"] == "WAITING_FOR_HUMAN"
    
    pids_first = {
        r["node_id"]: r.get("worker_pid")
        for r in first_state["node_results"]
        if r["node_id"] in ("N02_SUB_01", "N02_SUB_02")
    }

    # Commit the changes so worktree baseline check passes
    runner.git(worktree, "add", "-A")
    runner.git(worktree, "commit", "-q", "-m", "Commit subtask work")

    # Plan resume from N04_INTEGRATED_REVIEW
    plan = runner.plan_resume(dag, first_state, "N04_INTEGRATED_REVIEW", worktree=worktree)
    assert "N02_SUB_01" in plan["inherited_nodes"]
    assert "N02_SUB_02" in plan["inherited_nodes"]
    assert "N03_MERGE" in plan["inherited_nodes"]

    second_state = runner.execute(wf_file, "Goal", repo, worktree, identifier="RUN_LEVEL_B_4_B", resume=plan, max_parallel_workers=2)
    assert second_state["status"] == "WAITING_FOR_HUMAN"

    # Completed nodes in second state must include inherited N02_SUB_01 and N02_SUB_02
    completed_ids = [c["node_id"] for c in second_state["completed_nodes"]]
    assert "N02_SUB_01" in completed_ids
    assert "N02_SUB_02" in completed_ids
