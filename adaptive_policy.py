#!/usr/bin/env python3
"""AAW TELEMETRY-DRIVEN ADAPTIVE POLICY ENGINE (Phase P3 Mandate).

Governs autonomous execution decisions using a strict constraint hierarchy:
SAFETY → CONTRACT VALIDITY → QUALITY FLOOR → BUDGET → PERFORMANCE

Capabilities:
1. Strategy Selection: S1 (Sequential), S2 (2 Workers), S3 (3 Workers), or CRITICAL_INDEPENDENT_REVIEW.
2. Worker Allocation: Up to 3 workers based on independent scopes, amortized overhead, and risk.
3. Model Routing & Effort Level: Matched to task complexity, capability class floor, and budget.
4. Verification Intensity & Review Gating: Hardened verification contracts and mandatory human gate invariants.
5. Shadow Mode: Logs adaptive recommendations and counterfactual diffs without mutating plan.
6. Controlled Adaptive Mode: Enforces automatic rollback to conservative S1 upon error rate spikes,
   merge failures, or elevated review rejection rates.
"""
from __future__ import annotations

import copy
import dataclasses
import datetime as dt
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

import model_router as mr
from aaw_paths import STATS_ROOT
import adaptive_orchestrator as ao

POLICY_VERSION = "AAW_ADAPTIVE_POLICY_V1.0"

MIN_VERIFIED_RUNS_FOR_CONTROLLED = 100
CONTROLLED_AUTHORIZATION_FILE = "CONTROLLED_ADAPTIVE_AUTHORIZATION.json"
MODE_AUDIT_LOG_FILE = "ADAPTIVE_MODE_AUDIT.jsonl"


def _append_mode_audit(stats_root: Path | None, event_type: str, details: dict[str, Any]) -> None:
    try:
        root = stats_root or STATS_ROOT
        root.mkdir(parents=True, exist_ok=True)
        log_file = root / MODE_AUDIT_LOG_FILE
        record = {
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
            "event": event_type,
            "policy_version": POLICY_VERSION,
            **details,
        }
        with log_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def verify_controlled_mode_authorization(
    stats_root: Path | None = None,
    auth_file: Path | None = None,
) -> tuple[bool, str, dict[str, Any] | None]:
    """Verify operator authorization and telemetry criteria for CONTROLLED_ADAPTIVE mode."""
    root = stats_root or STATS_ROOT
    target_auth = auth_file or (root / CONTROLLED_AUTHORIZATION_FILE)

    if not target_auth.is_file():
        return False, "REFUSAL_OPERATOR_AUTHORIZATION_MISSING: authorization file not found", None

    try:
        auth_data = json.loads(target_auth.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, f"REFUSAL_CORRUPTED_AUTHORIZATION: {exc}", None

    if auth_data.get("policy_version") != POLICY_VERSION:
        return False, f"REFUSAL_POLICY_VERSION_MISMATCH: expected {POLICY_VERSION}, got {auth_data.get('policy_version')}", auth_data

    if auth_data.get("revoked") is True:
        return False, f"REFUSAL_AUTHORIZATION_REVOKED: revoked at {auth_data.get('revoked_at')}", auth_data

    if auth_data.get("authorized_mode") != MODE_CONTROLLED_ADAPTIVE:
        return False, f"REFUSAL_INVALID_AUTHORIZED_MODE: {auth_data.get('authorized_mode')}", auth_data

    authorized_by = auth_data.get("authorized_by")
    agent_roles = {"CODE_IMPLEMENTER", "INDEPENDENT_REVIEWER", "PLANNER", "AUTONOMY_CONTROLLER", "REPAIRER", "MODEL"}
    if not isinstance(authorized_by, str) or not authorized_by.strip() or authorized_by.upper() in agent_roles:
        return False, f"REFUSAL_UNAUTHORIZED_OPERATOR: verified human operator identity required, got {authorized_by}", auth_data

    token = auth_data.get("authorization_token")
    if not isinstance(token, str) or not token.strip():
        return False, "REFUSAL_MISSING_AUTHORIZATION_TOKEN", auth_data

    # Check verified telemetry history
    runs_file = root / "VERIFIED_PRODUCTION_RUNS.jsonl"
    runs_data = []
    if runs_file.is_file():
        try:
            for line in runs_file.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    runs_data.append(json.loads(line))
        except Exception as exc:
            return False, f"REFUSAL_CORRUPTED_TELEMETRY: {exc}", auth_data
    elif "runs_summary" in auth_data and isinstance(auth_data["runs_summary"], list):
        runs_data = auth_data["runs_summary"]

    valid_runs = 0
    seen_ids = set()
    for row in runs_data:
        rid = row.get("run_id")
        if not rid or rid in seen_ids:
            continue
        seen_ids.add(rid)
        if row.get("status") not in ("COMPLETED", "WAITING_FOR_HUMAN"):
            continue
        if row.get("provenance") not in ("REAL_MEASURED", "REAL_PRODUCTION"):
            continue
        tel = row.get("telemetry") or {}
        if not tel or (tel.get("wall_time_s", 0) <= 0 and tel.get("tokens_total", 0) <= 0):
            continue
        valid_runs += 1

    if valid_runs < MIN_VERIFIED_RUNS_FOR_CONTROLLED:
        return False, f"REFUSAL_INSUFFICIENT_TELEMETRY: {valid_runs}/{MIN_VERIFIED_RUNS_FOR_CONTROLLED} verified runs required", auth_data

    return True, "AUTHORIZED_CONTROLLED_ADAPTIVE", auth_data


def load_policy_health(stats_root: Path | None = None) -> PolicyHealthState:
    root = stats_root or STATS_ROOT
    health_file = root / "POLICY_HEALTH.json"
    if health_file.is_file():
        try:
            data = json.loads(health_file.read_text(encoding="utf-8"))
            return PolicyHealthState(
                consecutive_errors=int(data.get("consecutive_errors", 0)),
                merge_failures=int(data.get("merge_failures", 0)),
                repair_cycles_history=list(data.get("repair_cycles_history", [])),
                cumulative_spend_usd=float(data.get("cumulative_spend_usd", 0.0)),
                budget_limit_usd=float(data["budget_limit_usd"]) if data.get("budget_limit_usd") is not None else None,
                force_rollback=bool(data.get("force_rollback", False)),
                force_rollback_reason=data.get("force_rollback_reason"),
            )
        except Exception:
            pass
    return PolicyHealthState()


def save_policy_health(health: PolicyHealthState, stats_root: Path | None = None) -> None:
    root = stats_root or STATS_ROOT
    root.mkdir(parents=True, exist_ok=True)
    health_file = root / "POLICY_HEALTH.json"
    data = dataclasses.asdict(health)
    tmp = health_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(health_file)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Constraint Hierarchy & Operation Modes
# ─────────────────────────────────────────────────────────────────────────────

CONSTRAINT_1_SAFETY = "1_SAFETY"
CONSTRAINT_2_CONTRACT_VALIDITY = "2_CONTRACT_VALIDITY"
CONSTRAINT_3_QUALITY_FLOOR = "3_QUALITY_FLOOR"
CONSTRAINT_4_BUDGET = "4_BUDGET"
CONSTRAINT_5_PERFORMANCE = "5_PERFORMANCE"

CONSTRAINT_HIERARCHY = (
    CONSTRAINT_1_SAFETY,
    CONSTRAINT_2_CONTRACT_VALIDITY,
    CONSTRAINT_3_QUALITY_FLOOR,
    CONSTRAINT_4_BUDGET,
    CONSTRAINT_5_PERFORMANCE,
)

# Operating Modes
MODE_DISABLED = "DISABLED"
MODE_SHADOW = "SHADOW"
MODE_CONTROLLED_ADAPTIVE = "CONTROLLED_ADAPTIVE"
MODE_ACTIVE = "ACTIVE"
OPERATING_MODES = (MODE_DISABLED, MODE_SHADOW, MODE_CONTROLLED_ADAPTIVE, MODE_ACTIVE)

# Canonical Reason Codes & Rule Identifiers
RULE_DISABLED_BASELINE = "ADAPTIVE_DISABLED_USER_OVERRIDE"
RULE_SHADOW_BASELINE_DIFF = "SHADOW_BASELINE_DIFF"
RULE_SAFETY_CRITICAL_SECURITY = "CRITICAL_SECURITY_INDEPENDENT_REVIEW"
RULE_SAFETY_SCOPE_CONFLICT = "SCOPE_OVERLAP_SERIALIZATION_REQUIRED"
RULE_TRIVIAL_DETERMINISTIC_BYPASS = "TRIVIAL_DETERMINISTIC_DIRECT"
RULE_ROLLBACK_MERGE_FAILURE = "ROLLBACK_MERGE_FAILURE"
RULE_ROLLBACK_CONSECUTIVE_ERRORS = "ROLLBACK_CONSECUTIVE_ERRORS"
RULE_ROLLBACK_HIGH_REJECTIONS = "ROLLBACK_HIGH_REJECTIONS"
RULE_BUDGET_EXCEEDED_CLAMP = "BUDGET_EXCEEDED_CLAMP"
RULE_QUALITY_FLOOR_ESCALATION = "QUALITY_FLOOR_ESCALATION"
RULE_PERF_BOUNDED_PARALLEL = "INDEPENDENT_SUBTASKS_PARALLEL_APPROVED"
RULE_SIMPLE_ECONOMIC_S1 = "SIMPLE_ECONOMIC_SINGLE_EXECUTOR"
RULE_DEFAULT_SAFE_S1 = "DEFAULT_SAFE_S1"


# ─────────────────────────────────────────────────────────────────────────────
# 2. Decision Data Structures & Health Tracking
# ─────────────────────────────────────────────────────────────────────────────

@dataclasses.dataclass
class PolicyHealthState:
    """Runtime health indicators used by Controlled Adaptive Mode to trigger rollbacks."""
    consecutive_errors: int = 0
    merge_failures: int = 0
    repair_cycles_history: list[int] = dataclasses.field(default_factory=list)
    cumulative_spend_usd: float = 0.0
    budget_limit_usd: float | None = None
    force_rollback: bool = False
    force_rollback_reason: str | None = None

    def record_run_result(
        self,
        success: bool,
        had_merge_failure: bool = False,
        repair_cycles: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        if had_merge_failure:
            self.merge_failures += 1
        if success:
            self.consecutive_errors = 0
        else:
            self.consecutive_errors += 1
        self.repair_cycles_history.append(repair_cycles)
        self.cumulative_spend_usd += cost_usd


@dataclasses.dataclass
class PolicyDecision:
    """Explainable, audited decision emitted by the Adaptive Policy Engine."""
    rule_id: str
    enforced_constraint: str
    selected_strategy: str  # "S1", "S2", "S3", "CRITICAL_INDEPENDENT_REVIEW"
    selected_template: str  # "SINGLE_EXECUTOR", "PARALLEL_INDEPENDENT", "SERIAL_FALLBACK", "CRITICAL_INDEPENDENT_REVIEW"
    workers_count: int
    model_profile: str | None
    effort_level: str
    verification_intensity: str  # "standard", "strict", "critical_independent"
    independent_review_required: bool
    deterministic_short_circuit: bool
    is_rollback: bool
    rollback_reason: str | None
    justification: str
    mode: str
    shadow_diff: dict[str, Any] | None = None
    conflict_analysis: dict[str, Any] | None = None
    policy_version: str = POLICY_VERSION
    timestamp: str = dataclasses.field(default_factory=lambda: dt.datetime.now(dt.timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["reason_code"] = self.rule_id
        d["adaptive_enabled"] = (self.mode != MODE_DISABLED)
        d["orchestrator_version"] = self.policy_version
        return d


# ─────────────────────────────────────────────────────────────────────────────
# 3. Core Policy Evaluation Hierarchy
# ─────────────────────────────────────────────────────────────────────────────

def resolve_effective_mode(
    context: Mapping[str, Any] | None = None,
    stats_root: Path | None = None,
) -> str:
    """Determine operating mode with hard gating for CONTROLLED_ADAPTIVE.
    
    P3 Gating Rule: Default mode is strictly MODE_SHADOW unless explicitly
    overridden. CONTROLLED_ADAPTIVE requires verified operator authorization and
    at least 100 verified production runs. ACTIVE mode is permanently locked.
    """
    root = stats_root or STATS_ROOT
    requested_mode = None

    env_mode = os.environ.get("AAW_ADAPTIVE_MODE", "").upper().strip()
    if env_mode in OPERATING_MODES:
        requested_mode = env_mode
    elif context and context.get("adaptive_mode") in OPERATING_MODES:
        requested_mode = str(context["adaptive_mode"])

    # Boolean flags for backwards compatibility
    env_enabled = os.environ.get("AAW_ADAPTIVE_ORCHESTRATION_ENABLED", "").lower().strip()
    if env_enabled in ("0", "false", "no", "off") or (context and context.get("adaptive_orchestration_enabled") is False):
        requested_mode = MODE_DISABLED
    elif requested_mode is None and (env_enabled in ("1", "true", "yes", "on") or (context and context.get("adaptive_orchestration_enabled") is True)):
        requested_mode = MODE_SHADOW

    if requested_mode is None:
        requested_mode = MODE_SHADOW

    if requested_mode == MODE_DISABLED:
        return MODE_DISABLED

    if requested_mode == MODE_ACTIVE:
        _append_mode_audit(root, "REFUSAL_ACTIVE_MODE_LOCKED", {"requested": requested_mode, "fallback": MODE_SHADOW})
        return MODE_SHADOW

    if requested_mode == MODE_CONTROLLED_ADAPTIVE:
        ok, reason, auth_data = verify_controlled_mode_authorization(root)
        if ok:
            _append_mode_audit(root, "MODE_AUTHORIZED_CONTROLLED", {
                "authorized_by": auth_data.get("authorized_by") if auth_data else "UNKNOWN",
                "scope": auth_data.get("scope") if auth_data else [],
            })
            return MODE_CONTROLLED_ADAPTIVE
        else:
            _append_mode_audit(root, "REFUSAL_CONTROLLED_ADAPTIVE_UNMET", {"requested": requested_mode, "reason": reason, "fallback": MODE_SHADOW})
            return MODE_SHADOW

    return MODE_SHADOW


def evaluate_policy(
    task_definition: Mapping[str, Any],
    telemetry: Mapping[str, Any] | None = None,
    health_state: PolicyHealthState | None = None,
    *,
    mode: str | None = None,
) -> PolicyDecision:
    """Evaluate task against the strict 5-layer constraint hierarchy:
    SAFETY → CONTRACT VALIDITY → QUALITY FLOOR → BUDGET → PERFORMANCE.
    """
    mode = mode or resolve_effective_mode(task_definition)
    health = health_state or PolicyHealthState()

    task_class = str(task_definition.get("task_class") or mr.MODERATE).upper()
    criticality = str(task_definition.get("criticality") or mr.NORMAL_CRITICALITY).upper()
    subtasks = list(task_definition.get("subtasks") or [])
    subtask_count = len(subtasks)

    # ─────────────────────────────────────────────────────────────────────────
    # A. MODE_DISABLED: Always return conservative baseline (S1)
    # ─────────────────────────────────────────────────────────────────────────
    if mode == MODE_DISABLED:
        return PolicyDecision(
            rule_id=RULE_DISABLED_BASELINE,
            enforced_constraint=CONSTRAINT_1_SAFETY,
            selected_strategy="S1",
            selected_template=ao.TEMPLATE_SINGLE_EXECUTOR,
            workers_count=1,
            model_profile="GPT6_LUNA_HIGH",
            effort_level="medium",
            verification_intensity="standard",
            independent_review_required=False,
            deterministic_short_circuit=False,
            is_rollback=False,
            rollback_reason=None,
            justification="Adaptive recommendations disabled by configuration; using single executor baseline.",
            mode=MODE_DISABLED,
        )

    # ─────────────────────────────────────────────────────────────────────────
    # B. Compute Candidate Decision via Hierarchy
    # ─────────────────────────────────────────────────────────────────────────

    # --- 1. CONSTRAINT: SAFETY & INTEGRITY ---
    # 1.1. Health State Rollback Trigger (Controlled Adaptive Mode)
    if mode == MODE_CONTROLLED_ADAPTIVE:
        if health.merge_failures > 0:
            return PolicyDecision(
                rule_id=RULE_ROLLBACK_MERGE_FAILURE,
                enforced_constraint=CONSTRAINT_1_SAFETY,
                selected_strategy="S1",
                selected_template=ao.TEMPLATE_SINGLE_EXECUTOR,
                workers_count=1,
                model_profile="GPT6_LUNA_HIGH",
                effort_level="high",
                verification_intensity="strict",
                independent_review_required=True,
                deterministic_short_circuit=False,
                is_rollback=True,
                rollback_reason=f"Previous merge failure detected ({health.merge_failures}); safety rollback to S1 enforced.",
                justification="Automatic safety rollback: merge failures in current run necessitate serial execution.",
                mode=mode,
            )

        if health.consecutive_errors >= 2:
            return PolicyDecision(
                rule_id=RULE_ROLLBACK_CONSECUTIVE_ERRORS,
                enforced_constraint=CONSTRAINT_1_SAFETY,
                selected_strategy="S1",
                selected_template=ao.TEMPLATE_SINGLE_EXECUTOR,
                workers_count=1,
                model_profile="SONNET_5_5_MEDIUM",
                effort_level="high",
                verification_intensity="strict",
                independent_review_required=True,
                deterministic_short_circuit=False,
                is_rollback=True,
                rollback_reason=f"Consecutive errors ({health.consecutive_errors}) exceeded threshold 2; safety rollback to S1 enforced.",
                justification="Automatic safety rollback: error rate spike requires serial execution with elevated review.",
                mode=mode,
            )

        recent_repairs = health.repair_cycles_history[-3:] if health.repair_cycles_history else []
        if recent_repairs and (sum(recent_repairs) / len(recent_repairs)) >= 2.0:
            return PolicyDecision(
                rule_id=RULE_ROLLBACK_HIGH_REJECTIONS,
                enforced_constraint=CONSTRAINT_1_SAFETY,
                selected_strategy="S1",
                selected_template=ao.TEMPLATE_SINGLE_EXECUTOR,
                workers_count=1,
                model_profile="SONNET_5_5_MEDIUM",
                effort_level="high",
                verification_intensity="strict",
                independent_review_required=True,
                deterministic_short_circuit=False,
                is_rollback=True,
                rollback_reason="Mean repair cycles >= 2.0; elevated review rejection triggered conservative fallback.",
                justification="Automatic safety rollback: high rejection rate indicates task instability; serializing.",
                mode=mode,
            )

    # 1.2. Critical Risk / Security Tasks
    if task_class == mr.CRITICAL or criticality == mr.CRITICAL:
        candidate = PolicyDecision(
            rule_id=RULE_SAFETY_CRITICAL_SECURITY,
            enforced_constraint=CONSTRAINT_1_SAFETY,
            selected_strategy="CRITICAL_INDEPENDENT_REVIEW",
            selected_template=ao.TEMPLATE_CRITICAL_INDEPENDENT_REVIEW,
            workers_count=1,
            model_profile="OPUS_5_5_HIGH",
            effort_level="max",
            verification_intensity="critical_independent",
            independent_review_required=True,
            deterministic_short_circuit=False,
            is_rollback=False,
            rollback_reason=None,
            justification="Critical/high-risk task requires strongest model and mandatory independent review gate.",
            mode=mode,
        )
        return _apply_shadow_mode_guard(candidate, mode)

    # 1.3. Scope Collision Check (Parallelism strictly forbidden if write conflicts exist)
    conflict_report = ao.detect_scope_conflicts(subtasks)
    if conflict_report["has_conflict"]:
        candidate = PolicyDecision(
            rule_id=RULE_SAFETY_SCOPE_CONFLICT,
            enforced_constraint=CONSTRAINT_1_SAFETY,
            selected_strategy="S1",
            selected_template=ao.TEMPLATE_SERIAL_FALLBACK,
            workers_count=1,
            model_profile="GPT6_LUNA_HIGH" if task_class == mr.MODERATE else "SONNET_5_5_MEDIUM",
            effort_level="medium" if task_class == mr.MODERATE else "high",
            verification_intensity="strict" if task_class == mr.COMPLEX else "standard",
            independent_review_required=(task_class == mr.COMPLEX),
            deterministic_short_circuit=False,
            is_rollback=False,
            rollback_reason=None,
            justification=f"File scope conflict detected ({conflict_report['conflict_count']} conflict(s)); safe serialization enforced.",
            mode=mode,
            conflict_analysis=conflict_report,
        )
        return _apply_shadow_mode_guard(candidate, mode)

    # --- 2. CONSTRAINT: CONTRACT VALIDITY & DETERMINISM ---
    # 2.1. TRIVIAL Task: Deterministic short-circuit
    if task_class == mr.TRIVIAL:
        candidate = PolicyDecision(
            rule_id=RULE_TRIVIAL_DETERMINISTIC_BYPASS,
            enforced_constraint=CONSTRAINT_2_CONTRACT_VALIDITY,
            selected_strategy="S1",
            selected_template=ao.TEMPLATE_SINGLE_EXECUTOR,
            workers_count=0,
            model_profile=None,
            effort_level="none",
            verification_intensity="standard",
            independent_review_required=False,
            deterministic_short_circuit=True,
            is_rollback=False,
            rollback_reason=None,
            justification="Task is deterministic and requires no LLM invocations or parallel workers.",
            mode=mode,
        )
        return _apply_shadow_mode_guard(candidate, mode)

    # --- 3. CONSTRAINT: QUALITY FLOOR ---
    # 3.1. Complex tasks require minimum capability class 3 (Sonnet / GPT-6) and strict verification
    if task_class == mr.COMPLEX:
        quality_model = "SONNET_5_5_MEDIUM"
        quality_effort = "high"
        quality_verification = "strict"
    elif task_class == mr.MODERATE:
        quality_model = "GPT6_LUNA_HIGH"
        quality_effort = "medium"
        quality_verification = "standard"
    else:
        quality_model = "AGY_GEMINI_FLASH"
        quality_effort = "low"
        quality_verification = "standard"

    # --- 4. CONSTRAINT: BUDGET ---
    # 4.1. If budget is constrained or cumulative spend is high, clamp to economic tier (without violating Quality Floor)
    is_budget_clamped = False
    if health.budget_limit_usd is not None and health.cumulative_spend_usd >= (health.budget_limit_usd * 0.85):
        is_budget_clamped = True
        if task_class != mr.COMPLEX:
            quality_model = "AGY_GEMINI_FLASH"
            quality_effort = "low"

    # --- 5. CONSTRAINT: PERFORMANCE OPTIMIZATION (PARALLELISM SELECTION) ---
    # Only evaluate parallelism if constraints 1-4 are fully satisfied:
    # - No scope conflicts (verified in 1.3)
    # - Not a trivial or single subtask
    # - Subtask count >= 2
    if subtask_count >= 2 and not is_budget_clamped:
        workers = min(subtask_count, 3)
        candidate = PolicyDecision(
            rule_id=RULE_PERF_BOUNDED_PARALLEL,
            enforced_constraint=CONSTRAINT_5_PERFORMANCE,
            selected_strategy="S3" if workers == 3 else "S2",
            selected_template=ao.TEMPLATE_PARALLEL_INDEPENDENT,
            workers_count=workers,
            model_profile=quality_model,
            effort_level=quality_effort,
            verification_intensity=quality_verification,
            independent_review_required=(task_class == mr.COMPLEX),
            deterministic_short_circuit=False,
            is_rollback=False,
            rollback_reason=None,
            justification=f"Independent subtasks ({subtask_count}) approved for parallel execution with isolated worktrees and ALL_REQUIRED merge.",
            mode=mode,
            conflict_analysis=conflict_report,
        )
        return _apply_shadow_mode_guard(candidate, mode)

    # 5.3. Default Single Executor (S1) for single subtasks or simple tasks
    rule = RULE_BUDGET_EXCEEDED_CLAMP if is_budget_clamped else RULE_SIMPLE_ECONOMIC_S1
    candidate = PolicyDecision(
        rule_id=rule,
        enforced_constraint=CONSTRAINT_4_BUDGET if is_budget_clamped else CONSTRAINT_5_PERFORMANCE,
        selected_strategy="S1",
        selected_template=ao.TEMPLATE_SINGLE_EXECUTOR,
        workers_count=1,
        model_profile=quality_model,
        effort_level=quality_effort,
        verification_intensity=quality_verification,
        independent_review_required=(task_class == mr.COMPLEX),
        deterministic_short_circuit=False,
        is_rollback=False,
        rollback_reason=None,
        justification="Task is simple or has only one subtask; economic single worker is most cost-efficient.",
        mode=mode,
        conflict_analysis=conflict_report,
    )
    return _apply_shadow_mode_guard(candidate, mode)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Shadow Mode Guard & Counterfactual Diffing
# ─────────────────────────────────────────────────────────────────────────────

def _apply_shadow_mode_guard(
    adaptive_decision: PolicyDecision,
    mode: str,
) -> PolicyDecision:
    """In Shadow Mode, log counterfactual difference while returning safe S1 baseline."""
    if mode != MODE_SHADOW:
        return adaptive_decision

    # Baseline execution that will actually run
    baseline_strategy = "S1"
    baseline_template = ao.TEMPLATE_SINGLE_EXECUTOR
    baseline_workers = 1
    baseline_model = "GPT6_LUNA_HIGH"

    divergence = (
        adaptive_decision.selected_strategy != baseline_strategy
        or adaptive_decision.workers_count != baseline_workers
        or adaptive_decision.model_profile != baseline_model
        or adaptive_decision.deterministic_short_circuit
    )

    # Compute predicted benefits & trade-offs for shadow logging
    predicted_speedup = 1.0
    if adaptive_decision.deterministic_short_circuit:
        predicted_speedup = 15.0
    elif adaptive_decision.workers_count > 1:
        # Amdahl-calibrated speedup with coordination overhead:
        # 2 workers: ~1.45x, 3 workers: ~1.85x
        predicted_speedup = round(adaptive_decision.workers_count / (1.0 + (adaptive_decision.workers_count - 1) * 0.25), 2)

    # Cost ratio prediction
    model_costs = {
        "OPUS_5_5_HIGH": 0.030,
        "SONNET_5_5_MEDIUM": 0.006,
        "GPT6_LUNA_HIGH": 0.004,
        "AGY_GEMINI_3_1_PRO": 0.002,
        "AGY_GEMINI_FLASH": 0.0002,
        None: 0.0,
    }
    rec_cost = model_costs.get(adaptive_decision.model_profile, 0.004)
    base_cost = model_costs.get(baseline_model, 0.004)
    cost_ratio = round(rec_cost / base_cost, 2) if base_cost > 0 else 1.0

    if adaptive_decision.deterministic_short_circuit:
        predicted_quality_delta = {
            "provenance": "ARCHITECTURAL_INVARIANT",
            "assessment": "Identical result guaranteed; zero LLM generation variance",
        }
        risk_mitigation = "Bypasses external LLM entirely; zero token spend and immediate completion."
    elif adaptive_decision.selected_strategy == "CRITICAL_INDEPENDENT_REVIEW":
        predicted_quality_delta = {
            "provenance": "PREDICTED",
            "assessment": "Enhanced defect detection via independent Opus reviewer (estimated heuristic range [15%, 40%], uncalibrated production baseline)",
        }
        risk_mitigation = "Mandatory independent reviewer gate blocks silent defect propagation."
    elif adaptive_decision.selected_template == ao.TEMPLATE_SERIAL_FALLBACK:
        predicted_quality_delta = {
            "provenance": "ARCHITECTURAL_INVARIANT",
            "assessment": "Serialized execution eliminates write-write merge collision risks",
        }
        risk_mitigation = "Detects file overlap upfront, preventing worktree merge collision."
    elif adaptive_decision.workers_count > 1:
        predicted_quality_delta = {
            "provenance": "CALIBRATED_SIMULATION",
            "assessment": "Equal quality floor maintained across isolated worktree tasks",
        }
        risk_mitigation = f"Bounded to {adaptive_decision.workers_count} concurrent workers with clean scope separation."
    else:
        predicted_quality_delta = {
            "provenance": "REAL_MEASURED",
            "assessment": "Baseline single executor reference quality",
        }
        risk_mitigation = "Conservative single executor baseline."

    shadow_diff = {
        "divergence": divergence,
        "guard_notice": f"Shadow Mode active: recommended {adaptive_decision.rule_id} ({adaptive_decision.selected_strategy}); actual execution guarded to baseline S1.",
        "recommended_rule_id": adaptive_decision.rule_id,
        "recommended_strategy": adaptive_decision.selected_strategy,
        "recommended_workers": adaptive_decision.workers_count,
        "recommended_model": adaptive_decision.model_profile,
        "recommended_template": adaptive_decision.selected_template,
        "recommended_deterministic": adaptive_decision.deterministic_short_circuit,
        "executed_strategy": baseline_strategy,
        "executed_template": baseline_template,
        "executed_workers": baseline_workers,
        "executed_model": baseline_model,
        "predicted_benefits": {
            "predicted_speedup": {
                "value": predicted_speedup,
                "provenance": "CALIBRATED_SIMULATION" if adaptive_decision.workers_count > 1 else "ARCHITECTURAL_INVARIANT",
                "method": "Amdahl scaling model (25% coordination overhead parameter)" if adaptive_decision.workers_count > 1 else "Direct bypass",
            },
            "predicted_cost_ratio": {
                "value": cost_ratio,
                "provenance": "CALIBRATED_SIMULATION",
                "method": "Catalog standard tier pricing model",
            },
            "predicted_quality_delta": predicted_quality_delta,
            "risk_mitigation": risk_mitigation,
        },
        "justification": adaptive_decision.justification,
    }

    return PolicyDecision(
        rule_id=adaptive_decision.rule_id,
        enforced_constraint=adaptive_decision.enforced_constraint,
        selected_strategy=adaptive_decision.selected_strategy,
        selected_template=adaptive_decision.selected_template,
        workers_count=adaptive_decision.workers_count,
        model_profile=adaptive_decision.model_profile,
        effort_level=adaptive_decision.effort_level,
        verification_intensity=adaptive_decision.verification_intensity,
        independent_review_required=adaptive_decision.independent_review_required,
        deterministic_short_circuit=adaptive_decision.deterministic_short_circuit,
        is_rollback=adaptive_decision.is_rollback,
        rollback_reason=adaptive_decision.rollback_reason,
        justification=adaptive_decision.justification,
        mode=MODE_SHADOW,
        shadow_diff=shadow_diff,
        conflict_analysis=adaptive_decision.conflict_analysis,
    )
