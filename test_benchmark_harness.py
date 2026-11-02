#!/usr/bin/env python3
"""Tests for AAW Effectiveness Benchmark Harness V1.0 (P1.2)."""
from __future__ import annotations

import json
from pathlib import Path

import benchmark_harness as bh
import model_router as mr


def test_benchmark_task_suite_structure() -> None:
    tasks = bh.BENCHMARK_TASKS
    assert len(tasks) == 25, "Benchmark must contain 25 tasks"
    
    classes = {t["task_class"] for t in tasks}
    assert classes == {mr.TRIVIAL, mr.SIMPLE, mr.MODERATE, mr.COMPLEX, mr.CRITICAL}

    trivial_count = sum(1 for t in tasks if t["task_class"] == mr.TRIVIAL)
    assert trivial_count == 5, "Must have exactly 5 TRIVIAL tasks"

    simple_count = sum(1 for t in tasks if t["task_class"] == mr.SIMPLE)
    assert simple_count == 6, "Must have exactly 6 SIMPLE tasks"

    mod_count = sum(1 for t in tasks if t["task_class"] == mr.MODERATE)
    assert mod_count == 6, "Must have exactly 6 MODERATE tasks"

    comp_count = sum(1 for t in tasks if t["task_class"] == mr.COMPLEX)
    assert comp_count == 5, "Must have exactly 5 COMPLEX tasks"

    crit_count = sum(1 for t in tasks if t["task_class"] == mr.CRITICAL)
    assert crit_count == 3, "Must have exactly 3 CRITICAL tasks"


def test_benchmark_execution_and_aggregation() -> None:
    results = bh.run_benchmark(is_synthetic=True)
    assert results["tasks_count"] == 25
    assert results["is_synthetic_cli"] is True

    base = results["baseline_summary"]
    adap = results["adaptive_summary"]
    comp = results["comparison"]

    assert base["total_tasks"] == 25
    assert adap["total_tasks"] == 25

    # Adaptive routing must short-circuit the 5 TRIVIAL tasks
    assert adap["deterministic_bypasses"] == 5
    assert base["deterministic_bypasses"] == 0

    # Model calls must be reduced by 5
    assert adap["total_model_calls"] == base["total_model_calls"] - 5
    assert comp["model_calls_delta"] == -5

    # Acceptance rate must be 100% maintained
    assert adap["acceptance_rate"] == 1.0
    assert base["acceptance_rate"] == 1.0
    assert comp["acceptance_rate_maintained"] is True

    # Cost savings must be positive
    assert comp["cost_saving_percent"] > 50.0


def test_benchmark_trivial_tasks_zero_llm_calls() -> None:
    results = bh.run_benchmark()
    trivial_tasks = [t for t in results["tasks_detail"] if t["task_class"] == mr.TRIVIAL]
    assert len(trivial_tasks) == 5

    for t in trivial_tasks:
        adap = t["adaptive"]
        assert adap["deterministic_bypass"] is True
        assert adap["model_calls"] == 0
        assert adap["estimated_cost_usd"] == 0.0
        assert adap["completed"] is True
        assert adap["acceptance_met"] is True


def test_benchmark_critical_tasks_require_high_capability() -> None:
    results = bh.run_benchmark()
    critical_tasks = [t for t in results["tasks_detail"] if t["task_class"] == mr.CRITICAL]
    assert len(critical_tasks) == 3

    for t in critical_tasks:
        adap = t["adaptive"]
        # Router should select a high-capability profile (class >= 4)
        assert adap["selected_profile_id"] in ("OPUS_5_5_HIGH", "AGY_GEMINI_3_1_PRO")
        assert adap["completed"] is True
        assert adap["acceptance_met"] is True


def test_benchmark_markdown_formatting() -> None:
    results = bh.run_benchmark()
    md = bh.format_markdown_table(results)
    assert "### Wyniki benchmarku skuteczności AAW P1.2 (25 zadań)" in md
    assert "| Metryka | Baseline (Monolityczny Opus) | Routing Adaptacyjny (V0.2) | Zmiana / Zysk |" in md
    assert "TRIVIAL bypass" in md
    assert "Szacowany koszt USD" in md
    assert "is_synthetic_cli = Yes (Deterministic/Simulated CLI)" in md


def test_benchmark_json_file_saved(tmp_path: Path) -> None:
    out_file = tmp_path / "benchmark_out.json"
    results = bh.run_benchmark()
    out_file.write_text(json.dumps(results, indent=2), encoding="utf-8")

    assert out_file.exists()
    loaded = json.loads(out_file.read_text(encoding="utf-8"))
    assert loaded["tasks_count"] == 25
    assert "baseline_summary" in loaded
    assert "adaptive_summary" in loaded
