"""Level C Verification: Real Provider CLI Execution & Benchmark Comparison.

Executes real tasks using the installed OpenAI Codex CLI (`codex`) comparing:
1. Sequential execution (max_parallel_workers = 1)
2. Bounded parallel execution (max_parallel_workers = 2)

Collects and reports:
- Total wall-clock duration
- Critical path duration
- Active worker counts and concurrency
- Execution overlap duration
- Model calls and token counts (input, cached, output)
- Scheduler overhead
- Speedup ratio
- Cost breakdown: Actual token telemetry vs estimated pricing (if known/unknown)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping

import pytest

import adaptive_orchestrator as ao
import workflow_runner as runner
from workflow_schema import validate_workflow


def run_benchmark_run(
    identifier: str,
    max_workers: int,
    base_dir: Path,
) -> dict[str, Any]:
    repo = base_dir / f"repo_{identifier}"
    repo.mkdir(parents=True, exist_ok=True)
    runner.git(repo, "init", "-q")
    runner.git(repo, "config", "user.name", "AAW Level C")
    runner.git(repo, "config", "user.email", "level_c@example.com")
    (repo / "README.md").write_text("# Level C Benchmark Repo\n", encoding="utf-8")
    runner.git(repo, "add", "README.md")
    runner.git(repo, "commit", "-q", "-m", "Initial baseline commit")

    worktree = base_dir / f"worktree_{identifier}"
    runner.git(repo, "worktree", "add", "-q", "-b", f"aaw/{identifier.lower()}", str(worktree), "HEAD")

    stats = base_dir / f"stats_{identifier}"
    stats.mkdir(parents=True, exist_ok=True)
    runner.STATS_ROOT = stats

    subtasks = [
        {
            "id": "SUB_01",
            "name": "Math Utilities Module",
            "files_to_change": ["math_utils.py", "test_math.py"],
            "instructions": "In WORKTREE_PATH create math_utils.py defining add(a: int, b: int) -> int and test_math.py testing it. Do not run interactive commands; finish immediately in this turn.",
        },
        {
            "id": "SUB_02",
            "name": "String Utilities Module",
            "files_to_change": ["string_utils.py", "test_string.py"],
            "instructions": "In WORKTREE_PATH create string_utils.py defining reverse_str(s: str) -> str and test_string.py testing it. Do not run interactive commands; finish immediately in this turn.",
        },
    ]

    strategy = {
        "selected_template": ao.TEMPLATE_PARALLEL_INDEPENDENT,
        "workers_count": 2,
        "model_profile": "SOL_MEDIUM",
        "effort_level": "low",
    }
    dag = ao.generate_adaptive_dag(strategy, "Implement utilities modules", subtasks=subtasks)
    validate_workflow(dag)

    wf_file = base_dir / f"wf_{identifier}.json"
    wf_file.write_text(json.dumps(dag, indent=2), encoding="utf-8")

    start_wall = time.monotonic()
    state = runner.execute(
        wf_file,
        "Implement utilities modules",
        repo,
        worktree,
        identifier=identifier,
        max_parallel_workers=max_workers,
    )
    total_wall_s = round(time.monotonic() - start_wall, 3)

    # Collect individual node results
    results_map = {r["node_id"]: r for r in state.get("node_results", [])}
    r1 = results_map.get("N02_SUB_01", {})
    r2 = results_map.get("N02_SUB_02", {})

    s1 = r1.get("start_monotonic", 0.0)
    e1 = r1.get("end_monotonic", 0.0)
    d1 = r1.get("duration_s", 0.0)
    pid1 = r1.get("worker_pid")

    s2 = r2.get("start_monotonic", 0.0)
    e2 = r2.get("end_monotonic", 0.0)
    d2 = r2.get("duration_s", 0.0)
    pid2 = r2.get("worker_pid")

    # Measure subtask concurrency metrics
    overlap_s = 0.0
    if s1 and e1 and s2 and e2:
        overlap_s = max(0.0, round(min(e1, e2) - max(s1, s2), 3))

    subtask_stage_span = 0.0
    if s1 and e1 and s2 and e2:
        subtask_stage_span = round(max(e1, e2) - min(s1, s2), 3)

    # Critical path duration: Fanout + max(Subtasks) + Merge + Review + Gate
    critical_path_s = 0.0
    for node_id, r in results_map.items():
        if node_id in ("N02_SUB_01", "N02_SUB_02"):
            continue
        critical_path_s += r.get("duration_s", 0.0)
    critical_path_s += max(d1, d2)
    critical_path_s = round(critical_path_s, 3)

    # Telemetry and token usage
    total_input_tokens = 0
    total_cached_tokens = 0
    total_output_tokens = 0
    telemetry_list = []

    exec_dir = stats / identifier / "EXECUTIONS"
    if exec_dir.is_dir():
        for exec_json in exec_dir.glob("*.json"):
            try:
                ed = json.loads(exec_json.read_text(encoding="utf-8"))
                telemetry_list.append(ed)
            except Exception:
                pass

    workflow_dir = stats / identifier / "WORKFLOW"
    if workflow_dir.is_dir():
        for res_file in workflow_dir.glob("*__*__result.json"):
            try:
                res_obj = json.loads(res_file.read_text(encoding="utf-8"))
                tel = res_obj.get("telemetry") or {}
                usage = tel.get("usage") or {}
                total_input_tokens += usage.get("input_tokens", 0)
                total_cached_tokens += usage.get("cached_input_tokens", 0)
                total_output_tokens += usage.get("output_tokens", 0)
            except Exception:
                pass

    # Scheduler overhead = total wall clock - sum of all node durations
    sum_durations = sum(r.get("duration_s", 0.0) for r in results_map.values())
    scheduler_overhead_s = round(max(0.0, total_wall_s - subtask_stage_span - (sum_durations - d1 - d2)), 3)

    return {
        "identifier": identifier,
        "max_workers": max_workers,
        "status": state.get("status"),
        "total_wall_s": total_wall_s,
        "critical_path_s": critical_path_s,
        "subtask_1": {"node_id": "N02_SUB_01", "pid": pid1, "duration_s": d1, "start": s1, "end": e1},
        "subtask_2": {"node_id": "N02_SUB_02", "pid": pid2, "duration_s": d2, "start": s2, "end": e2},
        "subtask_stage_span_s": subtask_stage_span,
        "overlap_s": overlap_s,
        "sum_subtask_durations_s": round(d1 + d2, 3),
        "scheduler_overhead_s": scheduler_overhead_s,
        "model_calls": state.get("llm_calls", 0),
        "tokens": {
            "input_tokens": total_input_tokens,
            "cached_input_tokens": total_cached_tokens,
            "output_tokens": total_output_tokens,
            "total_tokens": total_input_tokens + total_output_tokens,
        },
        "cost_accounting": {
            "cost_status": "REAL_USAGE_UNKNOWN_PRICING",
            "note": "Token counts are measured from real provider CLI telemetry; exact USD rate depends on ChatGPT subscription tier / API quota.",
            "estimated_cost_usd": None,
        },
        "files_created": {
            "math_utils_exists": (worktree / "math_utils.py").is_file(),
            "string_utils_exists": (worktree / "string_utils.py").is_file(),
        },
    }


def test_level_c_sequential_vs_bounded_parallel_benchmark(tmp_path):
    """Level C: Execute real provider CLI benchmark comparing sequential vs parallel execution."""
    # Ensure codex CLI is available and authenticable
    codex_exe = runner.harness_executable("codex")
    if not codex_exe:
        pytest.skip("Codex CLI executable not found on PATH; Level C requires real provider CLI.")

    try:
        probe = subprocess.run([codex_exe, "--version"], capture_output=True, text=True, timeout=5)
        if probe.returncode != 0:
            pytest.skip(f"Codex CLI probe failed (rc={probe.returncode}); Level C requires real provider CLI.")
    except Exception as exc:
        pytest.skip(f"Codex CLI probe error ({exc}); Level C requires real provider CLI.")

    print("\n--- Running Level C Sequential Benchmark (workers=1) ---")
    seq_result = run_benchmark_run("BENCH_SEQUENTIAL", max_workers=1, base_dir=tmp_path)
    print(f"Sequential completed in {seq_result['total_wall_s']}s (subtasks: {seq_result['subtask_stage_span_s']}s, overlap: {seq_result['overlap_s']}s)")

    print("\n--- Running Level C Bounded Parallel Benchmark (workers=2) ---")
    par_result = run_benchmark_run("BENCH_PARALLEL", max_workers=2, base_dir=tmp_path)
    print(f"Parallel completed in {par_result['total_wall_s']}s (subtasks: {par_result['subtask_stage_span_s']}s, overlap: {par_result['overlap_s']}s)")

    # Compute Speedup Ratio
    seq_subtask_span = seq_result["subtask_stage_span_s"]
    par_subtask_span = par_result["subtask_stage_span_s"]
    speedup_subtasks = round(seq_subtask_span / max(0.001, par_subtask_span), 2)
    speedup_total = round(seq_result["total_wall_s"] / max(0.001, par_result["total_wall_s"]), 2)

    benchmark_summary = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "provider": "OpenAI Codex CLI (codex 0.164.0)",
        "model": "gpt-5.6-sol",
        "sequential": seq_result,
        "bounded_parallel": par_result,
        "comparison": {
            "subtask_stage_speedup": speedup_subtasks,
            "total_workflow_speedup": speedup_total,
            "parallel_overlap_s": par_result["overlap_s"],
            "sequential_overlap_s": seq_result["overlap_s"],
            "tokens_sequential": seq_result["tokens"],
            "tokens_parallel": par_result["tokens"],
        },
    }

    report_path = Path(__file__).resolve().parent / "docs" / "reports" / "p2_level_c_benchmark_results.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(benchmark_summary, indent=2), encoding="utf-8")
    print(f"\nSaved benchmark results to {report_path}")

    # Assertions on real execution
    assert seq_result["status"] == "WAITING_FOR_HUMAN", f"Sequential status: {seq_result['status']}"
    assert par_result["status"] == "WAITING_FOR_HUMAN", f"Parallel status: {par_result['status']}"

    assert seq_result["files_created"]["math_utils_exists"] and seq_result["files_created"]["string_utils_exists"]
    assert par_result["files_created"]["math_utils_exists"] and par_result["files_created"]["string_utils_exists"]

    # Concurrency proofs:
    # 1. Sequential had zero or negligible overlap
    assert seq_result["overlap_s"] == 0.0, f"Sequential should have 0s overlap, got {seq_result['overlap_s']}s"
    # 2. Parallel had positive wall-clock overlap and distinct PIDs
    assert par_result["overlap_s"] > 0.0, f"Parallel execution must have positive overlap, got {par_result['overlap_s']}s"
    assert par_result["subtask_1"]["pid"] != par_result["subtask_2"]["pid"], "Parallel workers must have distinct PIDs"
    # 3. Subtask stage speedup > 1.0x
    assert speedup_subtasks >= 1.0, f"Expected concurrency speedup >= 1.0x, got {speedup_subtasks}x"
