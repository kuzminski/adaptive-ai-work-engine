#!/usr/bin/env python3
"""Tests for AAW Adaptive Policy Engine (Phase P3 Mandate)."""
from __future__ import annotations

import json
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
    # Policy recommendation is preserved
    assert decision.selected_strategy == "S2"
    assert decision.selected_template == ao.TEMPLATE_PARALLEL_INDEPENDENT
    assert decision.workers_count == 2

    # Counterfactual shadow diff documents guarded baseline execution
    assert decision.shadow_diff is not None
    diff = decision.shadow_diff
    assert diff["divergence"] is True
    assert diff["recommended_strategy"] == "S2"
    assert diff["recommended_workers"] == 2
    assert diff["executed_strategy"] == "S1"
    assert diff["executed_workers"] == 1
    assert diff["executed_template"] == ao.TEMPLATE_SINGLE_EXECUTOR


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


def test_default_mode_is_strictly_shadow() -> None:
    # When no explicit AAW_ADAPTIVE_MODE is specified, defaults strictly to SHADOW mode
    old_env = os.environ.pop("AAW_ADAPTIVE_MODE", None)
    old_flag = os.environ.pop("AAW_ADAPTIVE_ORCHESTRATION_ENABLED", None)
    try:
        assert ap.resolve_effective_mode() == ap.MODE_SHADOW
        assert ap.resolve_effective_mode({}) == ap.MODE_SHADOW
        assert ap.resolve_effective_mode({"adaptive_orchestration_enabled": True}) == ap.MODE_SHADOW
    finally:
        if old_env:
            os.environ["AAW_ADAPTIVE_MODE"] = old_env
        if old_flag:
            os.environ["AAW_ADAPTIVE_ORCHESTRATION_ENABLED"] = old_flag


def test_shadow_mode_computes_predicted_benefits() -> None:
    task = {
        "id": "T_SHADOW_PRED_01",
        "task_class": mr.MODERATE,
        "criticality": mr.NORMAL_CRITICALITY,
        "subtasks": [
            {"id": "S1", "files_to_change": ["service_a.py"]},
            {"id": "S2", "files_to_change": ["service_b.py"]},
        ],
    }
    decision = ap.evaluate_policy(task, mode=ap.MODE_SHADOW)
    assert decision.mode == ap.MODE_SHADOW
    assert decision.shadow_diff is not None
    benefits = decision.shadow_diff["predicted_benefits"]
    speedup = benefits["predicted_speedup"]["value"] if isinstance(benefits["predicted_speedup"], dict) else benefits["predicted_speedup"]
    assert speedup >= 1.3
    assert "risk_mitigation" in benefits
    assert "predicted_quality_delta" in benefits
    assert decision.shadow_diff["executed_strategy"] == "S1"
    assert decision.shadow_diff["executed_workers"] == 1


def test_controlled_adaptive_requires_explicit_opt_in() -> None:
    task = {
        "id": "T_CONTROLLED_01",
        "task_class": mr.MODERATE,
        "criticality": mr.NORMAL_CRITICALITY,
        "subtasks": [
            {"id": "S1", "files_to_change": ["module_a.py"]},
            {"id": "S2", "files_to_change": ["module_b.py"]},
        ],
    }
    # Security Hardening (P3.0.1): Direct request for CONTROLLED_ADAPTIVE clamps to SHADOW
    decision = ap.evaluate_policy(task, mode=ap.MODE_CONTROLLED_ADAPTIVE)
    assert decision.mode == ap.MODE_SHADOW
    assert decision.shadow_diff["executed_workers"] == 1
    assert decision.shadow_diff["executed_template"] == ao.TEMPLATE_SINGLE_EXECUTOR
    assert decision.selected_strategy == "S2"




# ─────────────────────────────────────────────────────────────────────────────
# Gate 3: Hard Gating for CONTROLLED_ADAPTIVE & Passive SHADOW Invariants
# ─────────────────────────────────────────────────────────────────────────────

def _make_verified_runs(count: int, corrupted: bool = False) -> list[dict]:
    runs = []
    for i in range(1, count + 1):
        runs.append({
            "run_id": f"RUN_PROD_{i:04d}",
            "status": "COMPLETED",
            "provenance": "REAL_MEASURED",
            "telemetry": {
                "wall_time_s": 12.5 if not (corrupted and i == count) else 0.0,
                "tokens_total": 450 if not (corrupted and i == count) else 0,
            },
        })
    return runs


def test_gate3_controlled_mode_refused_at_0_runs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AAW_ADAPTIVE_MODE", "CONTROLLED_ADAPTIVE")
    mode = ap.resolve_effective_mode(stats_root=tmp_path)
    assert mode == ap.MODE_SHADOW, "Must fail-closed to SHADOW when no authorization/telemetry exists"
    audit_file = tmp_path / ap.MODE_AUDIT_LOG_FILE
    assert audit_file.is_file()
    assert "REFUSAL_CONTROLLED_ADAPTIVE_LOCKED" in audit_file.read_text(encoding="utf-8")


def test_gate3_controlled_mode_refused_at_99_runs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AAW_ADAPTIVE_MODE", "CONTROLLED_ADAPTIVE")
    auth_file = tmp_path / ap.CONTROLLED_AUTHORIZATION_FILE
    auth_file.write_text(json.dumps({
        "policy_version": ap.POLICY_VERSION,
        "authorized_mode": ap.MODE_CONTROLLED_ADAPTIVE,
        "authorized_by": "lead-engineer@organization.invalid",
        "authorization_token": "AUTH_TOKEN_TEST_99",
        "revoked": False,
        "runs_summary": _make_verified_runs(99),
    }), encoding="utf-8")

    mode = ap.resolve_effective_mode(stats_root=tmp_path)
    assert mode == ap.MODE_SHADOW, "Must fail-closed to SHADOW with 99 verified runs (< 100)"
    audit_file = tmp_path / ap.MODE_AUDIT_LOG_FILE
    assert "REFUSAL_CONTROLLED_ADAPTIVE_LOCKED" in audit_file.read_text(encoding="utf-8")


def test_gate3_controlled_mode_refused_at_100_runs_without_operator_auth(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AAW_ADAPTIVE_MODE", "CONTROLLED_ADAPTIVE")
    runs_file = tmp_path / "VERIFIED_PRODUCTION_RUNS.jsonl"
    with runs_file.open("w", encoding="utf-8") as f:
        for r in _make_verified_runs(100):
            f.write(json.dumps(r) + "\n")

    mode = ap.resolve_effective_mode(stats_root=tmp_path)
    assert mode == ap.MODE_SHADOW, "Must fail-closed to SHADOW if authorization record is missing"


def test_gate3_controlled_mode_refused_at_100_runs_with_corrupted_telemetry(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AAW_ADAPTIVE_MODE", "CONTROLLED_ADAPTIVE")
    auth_file = tmp_path / ap.CONTROLLED_AUTHORIZATION_FILE
    auth_file.write_text(json.dumps({
        "policy_version": ap.POLICY_VERSION,
        "authorized_mode": ap.MODE_CONTROLLED_ADAPTIVE,
        "authorized_by": "lead-engineer@organization.invalid",
        "authorization_token": "AUTH_TOKEN_TEST_CORRUPT",
        "revoked": False,
        "runs_summary": _make_verified_runs(100, corrupted=True),
    }), encoding="utf-8")

    mode = ap.resolve_effective_mode(stats_root=tmp_path)
    assert mode == ap.MODE_SHADOW, "Must fail-closed to SHADOW if telemetry is corrupted/incomplete"


def test_gate3_controlled_mode_locked_even_with_100_runs_and_untrusted_file(tmp_path: Path, monkeypatch) -> None:
    """Security Hardening (P3.0.1): Local JSON files cannot activate CONTROLLED_ADAPTIVE;
    it must remain locked to SHADOW pending trusted platform authorization.
    """
    monkeypatch.setenv("AAW_ADAPTIVE_MODE", "CONTROLLED_ADAPTIVE")
    auth_file = tmp_path / ap.CONTROLLED_AUTHORIZATION_FILE
    auth_file.write_text(json.dumps({
        "policy_version": ap.POLICY_VERSION,
        "authorized_mode": ap.MODE_CONTROLLED_ADAPTIVE,
        "authorized_by": "lead-engineer@organization.invalid",
        "authorization_token": "AUTH_TOKEN_VALID_100",
        "revoked": False,
        "scope": ["PARALLEL_EXECUTION", "BUDGET_CLAMP"],
        "runs_summary": _make_verified_runs(100),
    }), encoding="utf-8")

    mode = ap.resolve_effective_mode(stats_root=tmp_path)
    assert mode == ap.MODE_SHADOW, "Must lock to SHADOW; untrusted local auth cannot enable live CONTROLLED_ADAPTIVE"
    audit_file = tmp_path / ap.MODE_AUDIT_LOG_FILE
    assert audit_file.is_file()
    audit_txt = audit_file.read_text(encoding="utf-8")
    assert "REFUSAL_CONTROLLED_ADAPTIVE_LOCKED" in audit_txt
    assert "CONTROLLED_ADAPTIVE_LOCKED_PENDING_TRUSTED_AUTHORIZATION" in audit_txt


def test_gate3_direct_evaluate_policy_call_cannot_bypass_mode_lock(tmp_path: Path) -> None:
    """Direct invocation of evaluate_policy with mode=CONTROLLED_ADAPTIVE or ACTIVE is clamped to SHADOW."""
    task = {
        "id": "T_BYPASS_ATTEMPT",
        "task_class": "COMPLEX",
        "criticality": "HIGH",
        "subtasks": [
            {"id": "S1", "files_to_change": ["a.py"]},
            {"id": "S2", "files_to_change": ["b.py"]},
        ],
    }
    # Direct call with CONTROLLED_ADAPTIVE
    dec_ctrl = ap.evaluate_policy(task, mode=ap.MODE_CONTROLLED_ADAPTIVE)
    assert dec_ctrl.mode == ap.MODE_SHADOW, "Must clamp to SHADOW"
    assert dec_ctrl.shadow_diff["executed_workers"] == 1, "Must enforce single executor baseline in shadow"
    assert dec_ctrl.shadow_diff["executed_template"] == ao.TEMPLATE_SINGLE_EXECUTOR

    # Direct call with ACTIVE
    dec_act = ap.evaluate_policy(task, mode=ap.MODE_ACTIVE)
    assert dec_act.mode == ap.MODE_SHADOW, "Must clamp to SHADOW"
    assert dec_act.shadow_diff["executed_workers"] == 1
    assert dec_act.shadow_diff["executed_template"] == ao.TEMPLATE_SINGLE_EXECUTOR

def test_gate3_controlled_mode_refused_when_auth_revoked(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AAW_ADAPTIVE_MODE", "CONTROLLED_ADAPTIVE")
    auth_file = tmp_path / ap.CONTROLLED_AUTHORIZATION_FILE
    auth_file.write_text(json.dumps({
        "policy_version": ap.POLICY_VERSION,
        "authorized_mode": ap.MODE_CONTROLLED_ADAPTIVE,
        "authorized_by": "lead-engineer@organization.invalid",
        "authorization_token": "AUTH_TOKEN_REVOKED",
        "revoked": True,
        "revoked_at": "2026-11-03T02:00:00Z",
        "runs_summary": _make_verified_runs(100),
    }), encoding="utf-8")

    mode = ap.resolve_effective_mode(stats_root=tmp_path)
    assert mode == ap.MODE_SHADOW, "Must fail-closed to SHADOW when authorization is revoked"


def test_gate3_active_mode_is_permanently_locked(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AAW_ADAPTIVE_MODE", "ACTIVE")
    mode = ap.resolve_effective_mode(stats_root=tmp_path)
    assert mode == ap.MODE_SHADOW, "ACTIVE mode must never activate without independent deployment approval"
    audit_file = tmp_path / ap.MODE_AUDIT_LOG_FILE
    assert "REFUSAL_ACTIVE_MODE_LOCKED" in audit_file.read_text(encoding="utf-8")


def test_gate3_policy_health_persistence_across_restarts(tmp_path: Path) -> None:
    h1 = ap.PolicyHealthState()
    h1.record_run_result(success=False, had_merge_failure=True, repair_cycles=2, cost_usd=0.015)
    h1.record_run_result(success=False, had_merge_failure=False, repair_cycles=1, cost_usd=0.010)
    ap.save_policy_health(h1, stats_root=tmp_path)

    # Reload simulated restart
    h2 = ap.load_policy_health(stats_root=tmp_path)
    assert h2.consecutive_errors == 2
    assert h2.merge_failures == 1
    assert h2.repair_cycles_history == [2, 1]
    assert round(h2.cumulative_spend_usd, 4) == 0.025

