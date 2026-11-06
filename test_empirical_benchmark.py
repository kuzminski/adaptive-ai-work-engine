#!/usr/bin/env python3
"""Tests for AAW Empirical Benchmark Suite (Phase P2.5 Mandate)."""
from __future__ import annotations

import pytest

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

    # Costs come from the single price source (MODEL_PRICING.json); the S4-vs-S1 ordering is a property of
    # that table (SIMULATED), not an invariant of the policy, so only sanity is asserted here.
    assert s1["total_cost_usd"] > 0 and s4["total_cost_usd"] > 0

    # Comparative analysis exists and has positive speedup
    comp = results["comparative_analysis"]
    assert comp["s4_vs_s1"]["speedup"] > 1.0
    assert "cost_reduction_percent" in comp["s4_vs_s1"]

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


def test_simulator_cost_uses_single_price_source():
    import aaw_telemetry as tel
    import empirical_benchmark as eb
    pricing = tel.load_pricing()
    for profile, model in eb.PROFILE_MODEL.items():
        assert tel.price_row(pricing, model) is not None, profile
        tokens = {"input_total": 3000, "input_cached": 1000, "input_cache_write": 0, "output": 500}
        expected = tel.estimate_cost(tokens, tel.price_row(pricing, model))
        assert eb.calculate_cost(profile, 2000, 1000, 500) == pytest.approx(expected, abs=1e-6)
    assert not hasattr(eb, "MODEL_PRICING")


def test_simulator_and_real_benchmark_never_share_name_or_provenance():
    """Roadmap 0.3: the strategy simulator is SIMULATED, the experience benchmark is real work; never mixed."""
    import aaw_experience as ex
    sim = eb.run_empirical_benchmark(eb.CORPUS_TASKS[:1])
    real = ex.benchmark([], scope="all")
    assert sim["provenance"] == "SIMULATED" and sim["display_name"] == "Symulator strategii"
    assert real["provenance"] == "REAL_MEASURED" and real["display_name"] == "Benchmark z prawdziwej pracy"
    assert sim["benchmark_kind"] != real["benchmark_kind"] and sim["display_name"] != real["display_name"]
    assert "prawdziwej pracy" not in sim["display_name"] and "Symulator" not in real["display_name"]
