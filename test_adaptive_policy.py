#!/usr/bin/env python3
"""Tests for AAW Adaptive Policy Engine (Phase P3 Mandate)."""
from __future__ import annotations

import os
from typing import Any

import adaptive_orchestrator as ao
import adaptive_policy as ap
import model_router as mr


def test_constraint_hierarchy_order() -> None:
    expected = (
        ap.CONSTRAINT_1_SAFETY,
        ap.CONSTRAINT_2_CONTRACT_VALIDITY,
        ap.CONSTRAINT_3_QUALITY_FLOOR,
        ap.CONSTRAINT_4_BUDGET,
        ap.CONSTRAINT_5_PERFORMANCE,
    )
    assert ap.CONSTRAINT_HIERARCHY == expected


def test_trivial_deterministic_bypass() -> None:
    task = {
        "id": "T_TRIVIAL_01",
        "task_class": mr.TRIVIAL,
        "criticality": mr.NORMAL_CRITICALITY,
        "subtasks": [{"id": "S1", "files_to_change": ["config.json"]}],
    }
    decision = ap.evaluate_policy(task, mode=ap.MODE_CONTROLLED_ADAPTIVE)
    assert decision.deterministic_short_circuit is True
    assert decision.workers_count == 0
    assert decision.model_profile is None
    assert decision.enforced_constraint == ap.CONSTRAINT_2_CONTRACT_VALIDITY
    assert decision.rule_id == ap.RULE_TRIVIAL_DETERMINISTIC_BYPASS


def test_critical_security_mandatory_independent_review() -> None:
    task = {
        "id": "T_CRIT_01",
        "task_class": mr.CRITICAL,
        "criticality": mr.CRITICAL,
        "subtasks": [{"id": "S1", "files_to_change": ["crypto.py"]}],
    }
    decision = ap.evaluate_policy(task, mode=ap.MODE_CONTROLLED_ADAPTIVE)
    assert decision.enforced_constraint == ap.CONSTRAINT_1_SAFETY
    assert decision.rule_id == ap.RULE_SAFETY_CRITICAL_SECURITY
    assert decision.selected_template == ao.TEMPLATE_CRITICAL_INDEPENDENT_REVIEW
    assert decision.model_profile == "OPUS_5_5_HIGH"
    assert decision.independent_review_required is True
    assert decision.verification_intensity == "critical_independent"


def test_safety_scope_conflict_forces_serial_fallback() -> None:
    task = {
        "id": "T_CONFLICT_01",
        "task_class": mr.MODERATE,
        "criticality": mr.NORMAL_CRITICALITY,
        "subtasks": [
            {"id": "S1", "files_to_change": ["workflow_runner.py"]},
            {"id": "S2", "files_to_change": ["workflow_runner.py"]},
        ],
    }
    decision = ap.evaluate_policy(task, mode=ap.MODE_CONTROLLED_ADAPTIVE)
    assert decision.enforced_constraint == ap.CONSTRAINT_1_SAFETY
    assert decision.rule_id == ap.RULE_SAFETY_SCOPE_CONFLICT
    assert decision.selected_template == ao.TEMPLATE_SERIAL_FALLBACK
    assert decision.workers_count == 1
    assert decision.conflict_analysis is not None
    assert decision.conflict_analysis["has_conflict"] is True


def test_performance_parallel_selection_when_safe() -> None:
    task_2 = {
        "id": "T_INDEP_02",
        "task_class": mr.MODERATE,
        "criticality": mr.NORMAL_CRITICALITY,
        "subtasks": [
            {"id": "S1", "files_to_change": ["module_a.py"]},
            {"id": "S2", "files_to_change": ["module_b.py"]},
        ],
    }
    dec_2 = ap.evaluate_policy(task_2, mode=ap.MODE_CONTROLLED_ADAPTIVE)
    assert dec_2.enforced_constraint == ap.CONSTRAINT_5_PERFORMANCE
    assert dec_2.rule_id == ap.RULE_PERF_BOUNDED_PARALLEL
    assert dec_2.selected_template == ao.TEMPLATE_PARALLEL_INDEPENDENT
    assert dec_2.workers_count == 2

    task_3 = {
        "id": "T_INDEP_03",
        "task_class": mr.MODERATE,
        "criticality": mr.NORMAL_CRITICALITY,
        "subtasks": [
            {"id": "S1", "files_to_change": ["module_a.py"]},
            {"id": "S2", "files_to_change": ["module_b.py"]},
            {"id": "S3", "files_to_change": ["module_c.py"]},
        ],
    }
    dec_3 = ap.evaluate_policy(task_3, mode=ap.MODE_CONTROLLED_ADAPTIVE)
    assert dec_3.workers_count == 3
    assert dec_3.selected_template == ao.TEMPLATE_PARALLEL_INDEPENDENT


def test_rollback_on_merge_failure() -> None:
    health = ap.PolicyHealthState(merge_failures=1)
    task = {
        "id": "T_RECOVER_01",
        "task_class": mr.MODERATE,
        "criticality": mr.NORMAL_CRITICALITY,
        "subtasks": [
            {"id": "S1", "files_to_change": ["module_a.py"]},
            {"id": "S2", "files_to_change": ["module_b.py"]},
        ],
    }
    decision = ap.evaluate_policy(task, health_state=health, mode=ap.MODE_CONTROLLED_ADAPTIVE)
    assert decision.is_rollback is True
    assert decision.rule_id == ap.RULE_ROLLBACK_MERGE_FAILURE
    assert decision.selected_template == ao.TEMPLATE_SINGLE_EXECUTOR
    assert decision.workers_count == 1
    assert "merge failure" in str(decision.rollback_reason)


def test_rollback_on_consecutive_errors() -> None:
    health = ap.PolicyHealthState(consecutive_errors=2)
    task = {
        "id": "T_ERRORS_01",
        "task_class": mr.MODERATE,
        "criticality": mr.NORMAL_CRITICALITY,
        "subtasks": [
            {"id": "S1", "files_to_change": ["module_a.py"]},
            {"id": "S2", "files_to_change": ["module_b.py"]},
        ],
    }
    decision = ap.evaluate_policy(task, health_state=health, mode=ap.MODE_CONTROLLED_ADAPTIVE)
    assert decision.is_rollback is True
    assert decision.rule_id == ap.RULE_ROLLBACK_CONSECUTIVE_ERRORS
    assert decision.selected_template == ao.TEMPLATE_SINGLE_EXECUTOR
    assert decision.workers_count == 1


def test_rollback_on_high_repair_rejections() -> None:
    health = ap.PolicyHealthState(repair_cycles_history=[2, 3, 2])
    task = {
        "id": "T_REPAIR_01",
        "task_class": mr.MODERATE,
        "criticality": mr.NORMAL_CRITICALITY,
        "subtasks": [
            {"id": "S1", "files_to_change": ["module_a.py"]},
            {"id": "S2", "files_to_change": ["module_b.py"]},
        ],
    }
    decision = ap.evaluate_policy(task, health_state=health, mode=ap.MODE_CONTROLLED_ADAPTIVE)
    assert decision.is_rollback is True
    assert decision.rule_id == ap.RULE_ROLLBACK_HIGH_REJECTIONS
    assert decision.selected_template == ao.TEMPLATE_SINGLE_EXECUTOR


def test_budget_clamp_enforced_without_breaking_quality_floor() -> None:
    health = ap.PolicyHealthState(cumulative_spend_usd=90.0, budget_limit_usd=100.0)
    task = {
        "id": "T_BUDGET_01",
        "task_class": mr.MODERATE,
        "criticality": mr.NORMAL_CRITICALITY,
        "subtasks": [
            {"id": "S1", "files_to_change": ["module_a.py"]},
            {"id": "S2", "files_to_change": ["module_b.py"]},
        ],
    }
    decision = ap.evaluate_policy(task, health_state=health, mode=ap.MODE_CONTROLLED_ADAPTIVE)
    assert decision.enforced_constraint == ap.CONSTRAINT_4_BUDGET
    assert decision.workers_count == 1
    assert decision.model_profile == "AGY_GEMINI_FLASH"

    # Critical task under budget limit still maintains Quality Floor (Opus + independent review)
    crit_task = {
        "id": "T_CRIT_BUDGET_01",
        "task_class": mr.CRITICAL,
        "criticality": mr.CRITICAL,
        "subtasks": [{"id": "S1", "files_to_change": ["crypto.py"]}],
    }
    crit_dec = ap.evaluate_policy(crit_task, health_state=health, mode=ap.MODE_CONTROLLED_ADAPTIVE)
    assert crit_dec.enforced_constraint == ap.CONSTRAINT_1_SAFETY
    assert crit_dec.model_profile == "OPUS_5_5_HIGH"
    assert crit_dec.independent_review_required is True


def test_shadow_mode_records_diff_without_mutating_plan() -> None:
    task = {
        "id": "T_SHADOW_01",
        "task_class": mr.MODERATE,
        "criticality": mr.NORMAL_CRITICALITY,
        "subtasks": [
            {"id": "S1", "files_to_change": ["module_a.py"]},
            {"id": "S2", "files_to_change": ["module_b.py"]},
        ],
    }
    decision = ap.evaluate_policy(task, mode=ap.MODE_SHADOW)
    assert decision.mode == ap.MODE_SHADOW
    # Executed plan is baseline S1
    assert decision.selected_strategy == "S1"
    assert decision.selected_template == ao.TEMPLATE_SINGLE_EXECUTOR
    assert decision.workers_count == 1
    assert decision.model_profile == "GPT6_LUNA_HIGH"

    # Counterfactual shadow diff is recorded
    assert decision.shadow_diff is not None
    diff = decision.shadow_diff
    assert diff["divergence"] is True
    assert diff["recommended_strategy"] == "S2"
    assert diff["recommended_workers"] == 2
    assert diff["executed_strategy"] == "S1"
    assert diff["executed_workers"] == 1


def test_disabled_mode_returns_baseline_s1() -> None:
    task = {
        "id": "T_DISABLED_01",
        "task_class": mr.COMPLEX,
        "criticality": mr.NORMAL_CRITICALITY,
        "subtasks": [
            {"id": "S1", "files_to_change": ["module_a.py"]},
            {"id": "S2", "files_to_change": ["module_b.py"]},
            {"id": "S3", "files_to_change": ["module_c.py"]},
        ],
    }
    decision = ap.evaluate_policy(task, mode=ap.MODE_DISABLED)
    assert decision.mode == ap.MODE_DISABLED
    assert decision.rule_id == ap.RULE_DISABLED_BASELINE
    assert decision.workers_count == 1
    assert decision.selected_template == ao.TEMPLATE_SINGLE_EXECUTOR
