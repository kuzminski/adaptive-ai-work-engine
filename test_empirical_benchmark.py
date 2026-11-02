#!/usr/bin/env python3
"""Tests for AAW Empirical Benchmark Suite (Phase P2.5 Mandate)."""
from __future__ import annotations

import json
from pathlib import Path

import empirical_benchmark as eb
import model_router as mr


def test_corpus_structure_and_categories() -> None:
    corpus = eb.CORPUS_TASKS
    assert len(corpus) == 24, "Benchmark corpus must contain exactly 24 tasks"

    categories = {t["category"] for t in corpus}
    expected_categories = {
        "MINOR_FIX",
        "MECHANICAL_REFACTOR",
        "MULTIFILE_IMPLEMENTATION",
        "CODE_ANALYSIS",
        "REGRESSION_REPAIR",
        "TEST_GENERATION",
        "CONFLICTING_SCOPES",
        "CRITICAL_INDEPENDENT_REVIEW",
    }
    assert categories == expected_categories, f"Expected 8 categories, got {categories}"

    for cat in expected_categories:
        cat_tasks = [t for t in corpus if t["category"] == cat]
        assert len(cat_tasks) == 3, f"Category {cat} must contain exactly 3 tasks"


def test_corpus_task_contracts() -> None:
    for task in eb.CORPUS_TASKS:
        assert "id" in task
        assert "name" in task
        assert "category" in task
        assert "task_class" in task
        assert "criticality" in task
        assert "files_count" in task
        assert "files_to_change" in task
        assert "files_to_read" in task
        assert "subtasks" in task
        assert len(task["subtasks"]) >= 1
        assert "parallel_potential" in task
        assert task["parallel_potential"] in ("NONE", "MEDIUM", "HIGH", "FORBIDDEN_CONFLICT")
        assert "risk_level" in task
        assert task["risk_level"] in ("LOW", "MEDIUM", "HIGH", "CRITICAL")
        assert "requires_testing" in task
        assert "requires_independent_review" in task


def test_benchmark_execution_and_telemetry_aggregation(tmp_path: Path) -> None:
    results = eb.run_empirical_benchmark()
    assert results["total_tasks"] == 24
    assert results["total_executions"] == 96  # 24 * 4
    assert results["corpus_id"] == "AAW-BENCH-CORPUS-V1"
    assert results["corpus_version"] == "1.0.0"

    summaries = results["strategy_summaries"]
    assert "S1" in summaries
    assert "S2" in summaries
    assert "S3" in summaries
    assert "S4" in summaries

    s1 = summaries["S1"]
    s2 = summaries["S2"]
    s3 = summaries["S3"]
    s4 = summaries["S4"]

    # All runs completed and tested
    assert s1["total_runs"] == 24
    assert s2["total_runs"] == 24
    assert s3["total_runs"] == 24
    assert s4["total_runs"] == 24
    assert s1["tests_pass_rate"] == 1.0
    assert s4["tests_pass_rate"] == 1.0

    # S2 and S3 experience merge conflicts on CONFLICTING_SCOPES
    assert s2["total_merge_conflicts"] > 0
    assert s3["total_merge_conflicts"] > 0

    # S4 adaptive policy prevents all merge conflicts via safe serialization
    assert s4["total_merge_conflicts"] == 0

    # S4 has lowest total cost due to model routing and trivial bypass
    assert s4["total_cost_usd"] < s1["total_cost_usd"]

    # Comparative analysis exists and has positive speedup
    comp = results["comparative_analysis"]
    assert comp["s4_vs_s1"]["speedup"] > 1.0
    assert comp["s4_vs_s1"]["cost_reduction_percent"] > 0.0

    # Test saving to disk
    out_file = tmp_path / "test_benchmark_results.json"
    saved = eb.save_benchmark_results(results, out_file)
    assert saved.exists()
    with open(saved, "r", encoding="utf-8") as f:
        loaded = json.load(f)
    assert loaded["total_tasks"] == 24


def test_s4_trivial_task_zero_llm_calls() -> None:
    results = eb.run_empirical_benchmark()
    trivial_runs = [
        t for t in results["task_comparisons"]
        if t["task_class"] == mr.TRIVIAL
    ]
    assert len(trivial_runs) >= 1
    for t in trivial_runs:
        s4 = t["S4"]
        assert s4["allocated_workers"] == 0
        assert s4["model_calls"] == 0
        assert s4["estimated_cost_usd"] == 0.0
        assert s4["status"] == "PASS"


def test_s4_critical_tasks_mandatory_independent_review() -> None:
    results = eb.run_empirical_benchmark()
    critical_runs = [
        t for t in results["task_comparisons"]
        if t["category"] == "CRITICAL_INDEPENDENT_REVIEW"
    ]
    assert len(critical_runs) == 3
    for t in critical_runs:
        s4 = t["S4"]
        assert s4["selected_model_profile"] == "OPUS_5_5_HIGH"
        assert s4["tests_passed"] is True
