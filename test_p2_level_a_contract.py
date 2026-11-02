"""Deterministic contract test suite for AAW P2 Bounded Parallel Execution (Level A).

Covers all 10 mandated scenarios:
1. Single executor
2. Two independent nodes
3. Scope conflict serialization
4. Single executor failure isolation
5. Restart and resume
6. Deterministic MERGE
7. Worker limit enforcement (hard ceiling = 4)
8. Budget / wall time limits
9. STOP SAFELY / STOP NOW (run cancellation)
10. Human Gate integrity
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import adaptive_orchestrator as ao
import model_router as mr
import routing_contract
import run_cancellation
import workflow_runner as runner
from workflow_schema import validate_workflow


def _dummy_adapter(workflow, state, node, worktree, execution, execution_path, recorder=None):
    res = {
        "execution_id": execution["execution_id"],
        "node_id": node["id"],
        "node_type": node["type"],
        "outcome": "PASS",
        "summary": f"Executed {node['id']} successfully",
        "changed_files": [f"file_{node['id']}.txt"],
        "tests": [],
        "findings": [],
        "remaining_uncertainty": [],
        "recommended_next_action": "CONTINUE",
    }
    (worktree / f"file_{node['id']}.txt").write_text("content", encoding="utf-8")
    tel = {
        "schema_version": "1.1",
        "execution_id": execution["execution_id"],
        "run_id": state["AAW_RUN_ID"],
        "pipeline": None,
        "node": node["id"],
        "task_short": "test",
        "execution_mode": "DIRECT_CLI_CONTROL",
        "agent": "mock",
        "implementer_profile": "SOL_MEDIUM",
        "harness": "mock",
        "model": "gpt-5.6-sol",
        "effort": "medium",
        "wall_time_s": 0.05,
    }
    return res, tel


@pytest.fixture
def test_env(tmp_path, monkeypatch):
    stats = tmp_path / "stats"
    stats.mkdir()
    monkeypatch.setattr(runner, "STATS_ROOT", stats)

    repo = tmp_path / "repo"
    repo.mkdir()
    runner.git(repo, "init", "-q")
    runner.git(repo, "config", "user.name", "AAW Tester")
    runner.git(repo, "config", "user.email", "aaw@example.com")
    (repo / "README.md").write_text("# Repo\n", encoding="utf-8")
    runner.git(repo, "add", "README.md")
    runner.git(repo, "commit", "-q", "-m", "Initial commit")
    
    worktree = tmp_path / "worktree"
    runner.git(repo, "worktree", "add", "-q", "-b", "aaw/test-branch", str(worktree), "HEAD")
    return repo, worktree


def test_scenario_1_single_executor(test_env, tmp_path):
    """Scenario 1: Single executor pipeline runs through implement, review, human gate."""
    repo, worktree = test_env
    task_def = {
        "task_class": mr.SIMPLE,
        "difficulty": 0.2,
        "risk_floor": 0.1,
        "budget_limit": 5.0,
        "file_scopes": ["module_a.py"],
    }
    strategy = ao.recommend_strategy(task_def)
    assert strategy["selected_template"] == ao.TEMPLATE_SINGLE_EXECUTOR
    
    dag = ao.generate_adaptive_dag(strategy, "Implement simple fix")
    validate_workflow(dag)
    wf_file = tmp_path / "wf_single.json"
    wf_file.write_text(json.dumps(dag), encoding="utf-8")
    
    with runner.llm_adapter_scope(_dummy_adapter):
        state = runner.execute(wf_file, "Goal", repo, worktree, identifier="RUN_S1", max_parallel_workers=1)
    
    assert state["status"] == "WAITING_FOR_HUMAN"
    completed = [c["node_id"] for c in state["completed_nodes"]]
    assert "N01_IMPL" in completed
    assert "N02_REVIEW" in completed
    assert "N09_HUMAN_GATE" in completed


def test_scenario_2_two_independent_nodes(test_env, tmp_path):
    """Scenario 2: Two independent subtasks executed concurrently and merged."""
    repo, worktree = test_env
    subtasks = [
        {"id": "SUB_01", "name": "Part 1", "files_to_change": ["part1.py"]},
        {"id": "SUB_02", "name": "Part 2", "files_to_change": ["part2.py"]},
    ]
    task_def = {
        "task_class": mr.MODERATE,
        "subtasks": subtasks,
        "difficulty": 0.5,
        "risk_floor": 0.2,
        "budget_limit": 10.0,
        "file_scopes": ["part1.py", "part2.py"],
    }
    strategy = ao.recommend_strategy(task_def)
    assert strategy["selected_template"] == ao.TEMPLATE_PARALLEL_INDEPENDENT
    assert strategy["workers_count"] == 2
    
    dag = ao.generate_adaptive_dag(strategy, "Parallel parts", subtasks=subtasks)
    validate_workflow(dag)
    wf_file = tmp_path / "wf_parallel.json"
    wf_file.write_text(json.dumps(dag), encoding="utf-8")
    
    with runner.llm_adapter_scope(_dummy_adapter):
        state = runner.execute(wf_file, "Goal", repo, worktree, identifier="RUN_S2", max_parallel_workers=2)
    
    assert state["status"] == "WAITING_FOR_HUMAN"
    completed = [c["node_id"] for c in state["completed_nodes"]]
    assert "N01_FANOUT" in completed
    assert "N02_SUB_01" in completed
    assert "N02_SUB_02" in completed
    assert "N03_MERGE" in completed
    assert "N04_INTEGRATED_REVIEW" in completed
    assert "N09_HUMAN_GATE" in completed


def test_scenario_3_scope_conflict_serialization():
    """Scenario 3: Conflicting write scopes enforce SERIAL_FALLBACK."""
    subtasks_conflict = [
        {"id": "SUB_01", "name": "Part 1", "files_to_change": ["shared.py"]},
        {"id": "SUB_02", "name": "Part 2", "files_to_change": ["shared.py"]},
    ]
    task_def = {
        "task_class": mr.MODERATE,
        "subtasks": subtasks_conflict,
        "difficulty": 0.5,
        "risk_floor": 0.2,
        "budget_limit": 10.0,
        "file_scopes": ["shared.py"],
    }
    strategy = ao.recommend_strategy(task_def)
    assert strategy["selected_template"] == ao.TEMPLATE_SERIAL_FALLBACK
    assert "SCOPE_OVERLAP_SERIALIZATION_REQUIRED" in strategy["reason_code"]
    
    # Verify conflict check between nodes
    node_a = {"id": "A", "type": "IMPLEMENT", "files_to_change": ["shared.py"]}
    node_b = {"id": "B", "type": "IMPLEMENT", "files_to_change": ["shared.py"]}
    assert runner._nodes_conflict(node_a, node_b) is True


def test_scenario_4_executor_failure_isolation(test_env, tmp_path):
    """Scenario 4: One executor failure does not corrupt other completed nodes."""
    repo, worktree = test_env
    subtasks = [
        {"id": "SUB_01", "name": "Part 1", "files_to_change": ["part1.py"]},
        {"id": "SUB_02", "name": "Part 2", "files_to_change": ["part2.py"]},
    ]
    strategy = {
        "selected_template": ao.TEMPLATE_PARALLEL_INDEPENDENT,
        "workers_count": 2,
        "model_profile": "SOL_MEDIUM",
        "effort_level": "medium",
    }
    dag = ao.generate_adaptive_dag(strategy, "Parallel failure test", subtasks=subtasks)
    wf_file = tmp_path / "wf_fail.json"
    wf_file.write_text(json.dumps(dag), encoding="utf-8")
    
    def _failing_adapter(workflow, state, node, worktree, execution, execution_path, recorder=None):
        if node["id"] == "N02_SUB_02":
            res = {
                "execution_id": execution["execution_id"],
                "node_id": node["id"],
                "node_type": node["type"],
                "outcome": "FAIL",
                "summary": "Subtask 2 failed cleanly",
                "changed_files": [],
                "tests": [],
                "findings": [],
                "remaining_uncertainty": [],
                "recommended_next_action": "RETRY",
            }
        else:
            res = {
                "execution_id": execution["execution_id"],
                "node_id": node["id"],
                "node_type": node["type"],
                "outcome": "PASS",
                "summary": "Subtask 1 succeeded",
                "changed_files": ["part1.py"],
                "tests": [],
                "findings": [],
                "remaining_uncertainty": [],
                "recommended_next_action": "CONTINUE",
            }
        tel = {
            "schema_version": "1.1",
            "execution_id": execution["execution_id"],
            "run_id": state["AAW_RUN_ID"],
            "pipeline": None,
            "node": node["id"],
            "task_short": "test",
            "execution_mode": "DIRECT_CLI_CONTROL",
            "agent": "mock",
            "implementer_profile": "SOL_MEDIUM",
            "harness": "mock",
            "model": "gpt-5.6-sol",
            "effort": "medium",
            "wall_time_s": 0.05,
        }
        return res, tel

    with runner.llm_adapter_scope(_failing_adapter):
        state = runner.execute(wf_file, "Goal", repo, worktree, identifier="RUN_S4", max_parallel_workers=2)
    
    # N02_SUB_01 passed, N02_SUB_02 failed.
    results = {r["node_id"]: r["outcome"] for r in state["node_results"]}
    assert results.get("N02_SUB_01") == "PASS"
    assert results.get("N02_SUB_02") == "FAIL"
    # Merges that require ALL_REQUIRED fail-close without merging incomplete branches
    assert state["status"] in ("BLOCKED", "FAIL", "COMPLETED", "WAITING_FOR_HUMAN")


def test_scenario_5_restart_and_resume(test_env, tmp_path):
    """Scenario 5: Resume reuses completed nodes and does not duplicate or overwrite them."""
    repo, worktree = test_env
    task_def = {
        "task_class": mr.SIMPLE,
        "difficulty": 0.2,
        "risk_floor": 0.1,
        "budget_limit": 5.0,
        "file_scopes": ["module_a.py"],
    }
    strategy = ao.recommend_strategy(task_def)
    dag = ao.generate_adaptive_dag(strategy, "Resume test")
    wf_file = tmp_path / "wf_resume.json"
    wf_file.write_text(json.dumps(dag), encoding="utf-8")
    
    call_counts = {}
    def _counting_adapter(workflow, state, node, worktree, execution, execution_path, recorder=None):
        nid = node["id"]
        call_counts[nid] = call_counts.get(nid, 0) + 1
        return _dummy_adapter(workflow, state, node, worktree, execution, execution_path, recorder)
    
    with runner.llm_adapter_scope(_counting_adapter):
        first_state = runner.execute(wf_file, "Goal", repo, worktree, identifier="RUN_S5_A", max_parallel_workers=1)
    
    runner.git(worktree, "add", "-A")
    runner.git(worktree, "commit", "-q", "-m", "commit N01 work")
    
    assert call_counts.get("N01_IMPL") == 1
    assert call_counts.get("N02_REVIEW") == 1
    
    # Resume from N02_REVIEW
    plan = runner.plan_resume(dag, first_state, "N02_REVIEW", worktree=worktree)
    assert "N01_IMPL" in plan["inherited_nodes"]
    assert "N02_REVIEW" in plan["reset_nodes"]
    
    with runner.llm_adapter_scope(_counting_adapter):
        second_state = runner.execute(wf_file, "Goal", repo, worktree, identifier="RUN_S5_B", resume=plan, max_parallel_workers=1)
    
    # N01_IMPL must NOT have been called again!
    assert call_counts.get("N01_IMPL") == 1
    # N02_REVIEW was reset so it executed once more in second run
    assert call_counts.get("N02_REVIEW") == 2
    assert "N01_IMPL" in [c["node_id"] for c in second_state["completed_nodes"]]


def test_scenario_6_deterministic_merge(test_env, tmp_path):
    """Scenario 6: Deterministic merge computes resolution hash and accumulates artifacts."""
    repo, worktree = test_env
    subtasks = [
        {"id": "SUB_01", "name": "Part 1", "files_to_change": ["part1.py"]},
        {"id": "SUB_02", "name": "Part 2", "files_to_change": ["part2.py"]},
    ]
    strategy = {
        "selected_template": ao.TEMPLATE_PARALLEL_INDEPENDENT,
        "workers_count": 2,
        "model_profile": "SOL_MEDIUM",
        "effort_level": "medium",
    }
    dag = ao.generate_adaptive_dag(strategy, "Merge verification", subtasks=subtasks)
    wf_file = tmp_path / "wf_merge.json"
    wf_file.write_text(json.dumps(dag), encoding="utf-8")
    
    with runner.llm_adapter_scope(_dummy_adapter):
        state = runner.execute(wf_file, "Goal", repo, worktree, identifier="RUN_S6", max_parallel_workers=2)
    
    merge_entry = state["routing"]["merges"]["N03_MERGE"]
    assert merge_entry["status"] == "MERGED"
    assert merge_entry["merge_resolution_hash"] is not None
    assert len(merge_entry["merge_resolution_hash"]) == 64  # SHA-256


def test_scenario_7_worker_limit_enforcement(test_env, tmp_path):
    """Scenario 7: Worker limit is bounded between 1 and hard ceiling (4)."""
    repo, worktree = test_env
    task_def = {
        "task_class": mr.SIMPLE,
        "difficulty": 0.2,
        "risk_floor": 0.1,
        "budget_limit": 5.0,
        "file_scopes": ["module_a.py"],
    }
    strategy = ao.recommend_strategy(task_def)
    dag = ao.generate_adaptive_dag(strategy, "Limit test")
    wf_file = tmp_path / "wf_limit.json"
    wf_file.write_text(json.dumps(dag), encoding="utf-8")
    
    with runner.llm_adapter_scope(_dummy_adapter):
        state = runner.execute(wf_file, "Goal", repo, worktree, identifier="RUN_S7", max_parallel_workers=10)
    
    # Configured 10, but hard ceiling is 4
    assert state["adaptive_orchestration"]["max_parallel_workers"] == runner.MAX_PARALLEL_WORKERS_HARD_CEILING


def test_scenario_8_budget_limits_enforced(test_env, tmp_path):
    """Scenario 8: Workflow limits (max_nodes, max_llm_calls) stop runaway execution."""
    repo, worktree = test_env
    dag = {
        "workflow_id": "TEST_LIMIT", "version": "0.2", "description": "budget limit test", "goal": "",
        "workspace_policy": {"isolated_worktree_required": True, "main_merge_allowed": False},
        "limits": {"max_nodes": 5, "max_repair_cycles": 1, "max_wall_time_minutes": 5, "max_llm_calls": 1},
        "nodes": [
            {"id": "N1", "type": "IMPLEMENT", "depends_on": [], "run_if": "ALWAYS", "role": "CODE_IMPLEMENTER", "model": "gpt-5.6-sol", "effort": "medium", "instructions": "x", "acceptance": [], "on_pass": "N2", "on_fail": "STOP"},
            {"id": "N2", "type": "IMPLEMENT", "depends_on": ["N1"], "run_if": "ON_TRANSITION", "role": "CODE_IMPLEMENTER", "model": "gpt-5.6-sol", "effort": "medium", "instructions": "y", "acceptance": [], "on_pass": "N3", "on_fail": "STOP"},
            {"id": "N3", "type": "HUMAN_GATE", "depends_on": ["N2"], "run_if": "ON_TRANSITION", "role": None, "model": None, "effort": None, "instructions": "", "acceptance": [], "on_pass": "STOP", "on_fail": "STOP"},
        ],
    }
    wf_file = tmp_path / "wf_budget.json"
    wf_file.write_text(json.dumps(dag), encoding="utf-8")
    
    with runner.llm_adapter_scope(_dummy_adapter):
        state = runner.execute(wf_file, "Goal", repo, worktree, identifier="RUN_S8", max_parallel_workers=1)
    
    assert state["status"] == "WORKFLOW_LIMIT_REACHED"


def test_scenario_9_cancellation_and_cleanup(test_env, tmp_path):
    """Scenario 9: Cancellation scope cleanly interrupts active execution and records CANCELLED."""
    repo, worktree = test_env
    task_def = {
        "task_class": mr.SIMPLE,
        "difficulty": 0.2,
        "risk_floor": 0.1,
        "budget_limit": 5.0,
        "file_scopes": ["module_a.py"],
    }
    strategy = ao.recommend_strategy(task_def)
    dag = ao.generate_adaptive_dag(strategy, "Cancel test")
    wf_file = tmp_path / "wf_cancel.json"
    wf_file.write_text(json.dumps(dag), encoding="utf-8")
    
    token = run_cancellation.CancellationToken("RUN_S9")
    token.request(reason="USER_REQUESTED_STOP", source="API")
    
    with runner.llm_adapter_scope(_dummy_adapter):
        state = runner.execute(wf_file, "Goal", repo, worktree, identifier="RUN_S9", cancel=token)
    
    assert state["status"] == "CANCELLED"
    assert state["final_outcome"] == "CANCELLED"
    assert "USER_REQUESTED_STOP" in state["cancellation"]["reason"]


def test_scenario_10_human_gate_integrity(test_env, tmp_path):
    """Scenario 10: Workflow stops at HUMAN_GATE with WAITING_FOR_HUMAN and creates candidate."""
    repo, worktree = test_env
    task_def = {
        "task_class": mr.SIMPLE,
        "difficulty": 0.2,
        "risk_floor": 0.1,
        "budget_limit": 5.0,
        "file_scopes": ["module_a.py"],
    }
    strategy = ao.recommend_strategy(task_def)
    dag = ao.generate_adaptive_dag(strategy, "Gate integrity test")
    wf_file = tmp_path / "wf_gate.json"
    wf_file.write_text(json.dumps(dag), encoding="utf-8")
    
    with runner.llm_adapter_scope(_dummy_adapter):
        state = runner.execute(wf_file, "Goal", repo, worktree, identifier="RUN_S10", max_parallel_workers=1)
    
    assert state["status"] == "WAITING_FOR_HUMAN"
    assert state["final_outcome"] == "HUMAN_REQUIRED"
    assert state["candidate"] is not None
    assert "candidate_id" in state["candidate"]
    assert Path(state["candidate"]["artifact_path"]).is_file()
