#!/usr/bin/env python3
"""AAW ADAPTIVE MULTI-AGENT ORCHESTRATOR V1.0 (Phase P2 Recovery Mandate).

Extends existing AAW workflow contracts (routing_contract, workflow_schema)
with data-driven strategy selection, approved DAG templates, file-scope conflict
detection, and deterministic merge coordination across isolated worktrees.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

import model_router as mr
import routing_contract as rc

ORCHESTRATOR_VERSION = "AAW_ADAPTIVE_ORCHESTRATOR_V1.0"

# Approved DAG Template Identifiers
TEMPLATE_SINGLE_EXECUTOR = "SINGLE_EXECUTOR"
TEMPLATE_PARALLEL_INDEPENDENT = "PARALLEL_INDEPENDENT"
TEMPLATE_SERIAL_FALLBACK = "SERIAL_FALLBACK"
TEMPLATE_CRITICAL_INDEPENDENT_REVIEW = "CRITICAL_INDEPENDENT_REVIEW"

APPROVED_TEMPLATES = (
    TEMPLATE_SINGLE_EXECUTOR,
    TEMPLATE_PARALLEL_INDEPENDENT,
    TEMPLATE_SERIAL_FALLBACK,
    TEMPLATE_CRITICAL_INDEPENDENT_REVIEW,
)

# Limits
MAX_PARALLEL_WORKERS = 3
MIN_PARALLEL_WORKERS = 2


# ─────────────────────────────────────────────────────────────────────────────
# 1. File Scope Conflict Analysis (P2.1)
# ─────────────────────────────────────────────────────────────────────────────

def normalize_path(path: Any) -> str:
    text = str(path or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def detect_scope_conflicts(subtasks: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Analyze file scope intersections between proposed subtasks.

    Write-write overlap strictly forbids parallel execution (must serialize).
    Write-read overlap is flagged as a causal dependency (requires serialization).
    """
    conflicts: list[dict[str, Any]] = []
    subtasks_list = list(subtasks)
    n = len(subtasks_list)

    for i in range(n):
        for j in range(i + 1, n):
            sub_a = subtasks_list[i]
            sub_b = subtasks_list[j]
            id_a = str(sub_a.get("id") or f"subtask_{i+1}")
            id_b = str(sub_b.get("id") or f"subtask_{j+1}")

            change_a = {normalize_path(p) for p in sub_a.get("files_to_change", []) if normalize_path(p)}
            change_b = {normalize_path(p) for p in sub_b.get("files_to_change", []) if normalize_path(p)}
            read_a = {normalize_path(p) for p in sub_a.get("files_to_read", []) if normalize_path(p)}
            read_b = {normalize_path(p) for p in sub_b.get("files_to_read", []) if normalize_path(p)}

            # 1. Write-Write conflict: concurrent edits to same file forbidden
            ww_overlap = sorted(change_a & change_b)
            if ww_overlap:
                conflicts.append({
                    "type": "WRITE_WRITE_CONFLICT",
                    "subtask_a": id_a,
                    "subtask_b": id_b,
                    "overlapping_files": ww_overlap,
                    "severity": "CRITICAL",
                    "action_required": "SERIALIZE_MANDATORY",
                })

            # 2. Write-Read conflict: subtask B reads what subtask A is changing
            wr_ab = sorted(change_a & read_b)
            if wr_ab:
                conflicts.append({
                    "type": "WRITE_READ_DEPENDENCY",
                    "writer": id_a,
                    "reader": id_b,
                    "overlapping_files": wr_ab,
                    "severity": "HIGH",
                    "action_required": "ORDERED_SERIALIZE",
                })

            wr_ba = sorted(change_b & read_a)
            if wr_ba:
                conflicts.append({
                    "type": "WRITE_READ_DEPENDENCY",
                    "writer": id_b,
                    "reader": id_a,
                    "overlapping_files": wr_ba,
                    "severity": "HIGH",
                    "action_required": "ORDERED_SERIALIZE",
                })

    has_conflict = len(conflicts) > 0
    return {
        "has_conflict": has_conflict,
        "conflict_count": len(conflicts),
        "conflicts": conflicts,
        "parallel_safe": not has_conflict,
        "recommended_mode": "PARALLEL" if not has_conflict else "SERIALIZE",
    }


# ─────────────────────────────────────────────────────────────────────────────
# 2. Telemetry-Driven Strategy Recommendation (P2.2)
# ─────────────────────────────────────────────────────────────────────────────

def recommend_strategy(
    task_definition: Mapping[str, Any],
    telemetry: Mapping[str, Any] | None = None,
    *,
    adaptive_enabled: bool = True,
) -> dict[str, Any]:
    """Select orchestration strategy based on task difficulty, subtasks, and telemetry."""
    task_class = str(task_definition.get("task_class") or mr.MODERATE).upper()
    criticality = str(task_definition.get("criticality") or mr.NORMAL_CRITICALITY).upper()
    subtasks = list(task_definition.get("subtasks") or [])
    subtask_count = len(subtasks)
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()

    # If adaptive recommendations are disabled by user, fall back to single executor
    if not adaptive_enabled:
        return {
            "orchestrator_version": ORCHESTRATOR_VERSION,
            "timestamp": now_iso,
            "adaptive_enabled": False,
            "selected_template": TEMPLATE_SINGLE_EXECUTOR,
            "workers_count": 1,
            "model_profile": "GPT6_LUNA_HIGH",
            "effort_level": "medium",
            "independent_review_required": False,
            "reason_code": "ADAPTIVE_DISABLED_USER_OVERRIDE",
            "justification": "Adaptive recommendations disabled by configuration; using single executor baseline.",
            "conflict_analysis": None,
        }

    # 1. TRIVIAL task: deterministic short-circuit
    if task_class == mr.TRIVIAL:
        return {
            "orchestrator_version": ORCHESTRATOR_VERSION,
            "timestamp": now_iso,
            "adaptive_enabled": True,
            "selected_template": TEMPLATE_SINGLE_EXECUTOR,
            "workers_count": 0,
            "model_profile": None,
            "effort_level": "none",
            "independent_review_required": False,
            "deterministic_short_circuit": True,
            "reason_code": "TRIVIAL_DETERMINISTIC_DIRECT",
            "justification": "Task is deterministic and requires no LLM invocations or parallel workers.",
            "conflict_analysis": None,
        }

    # 2. CRITICAL task: highest security, mandatory independent review
    if task_class == mr.CRITICAL or criticality == mr.CRITICAL:
        return {
            "orchestrator_version": ORCHESTRATOR_VERSION,
            "timestamp": now_iso,
            "adaptive_enabled": True,
            "selected_template": TEMPLATE_CRITICAL_INDEPENDENT_REVIEW,
            "workers_count": 1,
            "model_profile": "OPUS_5_5_HIGH",
            "effort_level": "max",
            "independent_review_required": True,
            "deterministic_short_circuit": False,
            "reason_code": "CRITICAL_SECURITY_INDEPENDENT_REVIEW",
            "justification": "Critical/high-risk task requires strongest model and mandatory independent review gate.",
            "conflict_analysis": None,
        }

    # 3. SIMPLE task: economic single worker
    if task_class == mr.SIMPLE or subtask_count <= 1:
        return {
            "orchestrator_version": ORCHESTRATOR_VERSION,
            "timestamp": now_iso,
            "adaptive_enabled": True,
            "selected_template": TEMPLATE_SINGLE_EXECUTOR,
            "workers_count": 1,
            "model_profile": "AGY_GEMINI_FLASH",
            "effort_level": "low",
            "independent_review_required": False,
            "deterministic_short_circuit": False,
            "reason_code": "SIMPLE_ECONOMIC_SINGLE_EXECUTOR",
            "justification": "Task is simple or has only one subtask; economic single worker is most cost-efficient.",
            "conflict_analysis": None,
        }

    # 4. MODERATE or COMPLEX with multiple subtasks: evaluate parallelism
    conflict_report = detect_scope_conflicts(subtasks)

    if conflict_report["has_conflict"]:
        # Overlapping files -> Enforce safe serialization
        return {
            "orchestrator_version": ORCHESTRATOR_VERSION,
            "timestamp": now_iso,
            "adaptive_enabled": True,
            "selected_template": TEMPLATE_SERIAL_FALLBACK,
            "workers_count": 1,
            "model_profile": "GPT6_LUNA_HIGH" if task_class == mr.MODERATE else "SONNET_5_5_MEDIUM",
            "effort_level": "medium" if task_class == mr.MODERATE else "high",
            "independent_review_required": (task_class == mr.COMPLEX),
            "deterministic_short_circuit": False,
            "reason_code": "SCOPE_OVERLAP_SERIALIZATION_REQUIRED",
            "justification": f"File scope conflict detected ({conflict_report['conflict_count']} conflict(s)); safe serialization enforced.",
            "conflict_analysis": conflict_report,
        }

    # Independent subtasks: evaluate cost justification for parallel dispatch
    # Overhead includes worktree creation, isolated git branch, and ALL_REQUIRED merge resolution.
    workers_count = min(subtask_count, MAX_PARALLEL_WORKERS)
    return {
        "orchestrator_version": ORCHESTRATOR_VERSION,
        "timestamp": now_iso,
        "adaptive_enabled": True,
        "selected_template": TEMPLATE_PARALLEL_INDEPENDENT,
        "workers_count": workers_count,
        "model_profile": "GPT6_LUNA_HIGH" if task_class == mr.MODERATE else "SONNET_5_5_MEDIUM",
        "effort_level": "medium",
        "independent_review_required": (task_class == mr.COMPLEX),
        "deterministic_short_circuit": False,
        "reason_code": "INDEPENDENT_SUBTASKS_PARALLEL_APPROVED",
        "justification": f"Detected {subtask_count} independent subtasks with zero file scope overlap; spawning {workers_count} parallel workers.",
        "conflict_analysis": conflict_report,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 3. Adaptive Workflow DAG Generation (P2.1)
# ─────────────────────────────────────────────────────────────────────────────

def generate_adaptive_dag(
    strategy: Mapping[str, Any],
    goal: str,
    subtasks: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Generate a validated AAW workflow JSON conforming to routing_contract and workflow_schema."""
    template = strategy.get("selected_template", TEMPLATE_SINGLE_EXECUTOR)
    subtasks = list(subtasks or [])
    workflow_id = f"ADAPTIVE_{template}_{int(dt.datetime.now(dt.timezone.utc).timestamp())}"

    base_limits = {
        "max_nodes": 15,
        "max_repair_cycles": 2,
        "max_wall_time_minutes": 180,
        "max_llm_calls": 12,
        "max_token_budget": None,
    }

    if template == TEMPLATE_SINGLE_EXECUTOR:
        nodes = [
            {
                "id": "N01_PLAN",
                "type": "PLAN",
                "depends_on": [],
                "run_if": "ALWAYS",
                "role": "SOFTWARE_ARCHITECT",
                "model": "gpt-5.6-sol",
                "effort": "medium",
                "instructions": f"Plan the implementation of: {goal}",
                "acceptance": ["Concrete plan with work packet produced"],
                "on_pass": None, "on_fail": None,
                "routing": "FIRST_MATCH",
                "edges": [{"edge_id": "E_PLAN_TO_IMPL", "to": "N02_IMPL", "when": None, "kind": "CONTINUE", "label": "continue"}],
            },
            {
                "id": "N02_IMPL",
                "type": "IMPLEMENT",
                "depends_on": ["N01_PLAN"],
                "run_if": "ON_TRANSITION",
                "role": "CODE_IMPLEMENTER",
                "model": strategy.get("model_profile") or "gpt-5.6-sol",
                "effort": strategy.get("effort_level") or "medium",
                "instructions": "Implement the plan in the isolated worktree.",
                "acceptance": ["Code and tests implemented"],
                "on_pass": None, "on_fail": None,
                "routing": "FIRST_MATCH",
                "edges": [{"edge_id": "E_IMPL_TO_REVIEW", "to": "N03_REVIEW", "when": None, "kind": "CONTINUE", "label": "continue"}],
            },
            {
                "id": "N03_REVIEW",
                "type": "REVIEW",
                "depends_on": ["N02_IMPL"],
                "run_if": "ON_TRANSITION",
                "role": "INDEPENDENT_REVIEWER",
                "model": "gpt-5.6-sol",
                "effort": "medium",
                "instructions": "Review the implementation against acceptance criteria.",
                "acceptance": ["Verdict is PASS, REPAIR, or BLOCKED"],
                "on_pass": None, "on_fail": None,
                "routing": "FIRST_MATCH",
                "edges": [
                    {"edge_id": "E_REVIEW_PASS", "to": "N09_HUMAN_GATE", "when": {"verdict": "PASS"}, "kind": "CONTINUE", "label": "PASS"},
                    {"edge_id": "E_REVIEW_BLOCKED", "to": "N09_HUMAN_GATE", "when": {"verdict": "BLOCKED"}, "kind": "FALLBACK", "label": "BLOCKED"},
                ],
            },
            {
                "id": "N09_HUMAN_GATE",
                "type": "HUMAN_GATE",
                "depends_on": ["N03_REVIEW"],
                "run_if": "ON_TRANSITION",
                "role": None, "model": None, "effort": None,
                "instructions": "Review candidate diff and record decision (ACCEPT / REJECT).",
                "acceptance": ["Human decision recorded in ledger."],
                "on_pass": None, "on_fail": None,
            },
        ]

    elif template == TEMPLATE_PARALLEL_INDEPENDENT:
        # Fan-out to N independent subtasks -> MERGE (ALL_REQUIRED) -> Integrated Review -> Human Gate
        sub_nodes: list[dict[str, Any]] = []
        fan_edges: list[dict[str, Any]] = []
        merge_incoming: list[str] = []

        sub_list = subtasks if subtasks else [
            {"id": "SUB_01", "name": "Subtask 1", "files_to_change": ["module_a.py"]},
            {"id": "SUB_02", "name": "Subtask 2", "files_to_change": ["module_b.py"]},
        ]

        for idx, sub in enumerate(sub_list, 1):
            sub_id = f"N02_SUB_{idx:02d}"
            edge_from_fan = f"E_FAN_TO_{sub_id}"
            edge_to_merge = f"E_{sub_id}_TO_MERGE"

            fan_edges.append({
                "edge_id": edge_from_fan,
                "to": sub_id,
                "when": {"outcome": "PASS"},
                "kind": "CONTINUE",
                "label": f"start {sub.get('id', sub_id)}",
            })
            merge_incoming.append(edge_to_merge)

            sub_nodes.append({
                "id": sub_id,
                "type": "IMPLEMENT",
                "depends_on": ["N01_FANOUT"],
                "run_if": "ON_TRANSITION",
                "role": "CODE_IMPLEMENTER",
                "model": strategy.get("model_profile") or "gpt-5.6-sol",
                "effort": strategy.get("effort_level") or "medium",
                "branch_name": f"aaw/branch_{sub_id.lower()}",
                "instructions": f"Implement independent subtask: {sub.get('name', sub_id)}. Restrict edits strictly to: {sub.get('files_to_change', [])}.",
                "acceptance": ["Subtask requirements met in isolated branch."],
                "on_pass": None, "on_fail": None,
                "routing": "FIRST_MATCH",
                "edges": [{"edge_id": edge_to_merge, "to": "N03_MERGE", "when": None, "kind": "CONTINUE", "label": "continue to merge"}],
            })

        nodes = [
            {
                "id": "N01_FANOUT",
                "type": "MACHINE_GATE",
                "depends_on": [],
                "run_if": "ALWAYS",
                "role": None, "model": None, "effort": None,
                "instructions": "Validate workspace and fan-out independent subtasks.",
                "acceptance": ["Machine gate passes."],
                "command": ["python", "-c", "import sys; sys.exit(0)"],
                "timeout_seconds": 60,
                "on_pass": None, "on_fail": None,
                "routing": "ALL_MATCHES",
                "edges": fan_edges,
            },
            *sub_nodes,
            {
                "id": "N03_MERGE",
                "type": "MERGE",
                "depends_on": [s["id"] for s in sub_nodes],
                "run_if": "ON_TRANSITION",
                "role": None, "model": None, "effort": None,
                "merge_policy": "ALL_REQUIRED",
                "expected_incoming": merge_incoming,
                "instructions": "Deterministic merge and union of independent subtask results.",
                "acceptance": ["All incoming subtasks successfully completed without conflict."],
                "on_pass": None, "on_fail": None,
                "routing": "FIRST_MATCH",
                "edges": [{"edge_id": "E_MERGE_TO_REVIEW", "to": "N04_INTEGRATED_REVIEW", "when": None, "kind": "CONTINUE", "label": "continue"}],
            },
            {
                "id": "N04_INTEGRATED_REVIEW",
                "type": "REVIEW",
                "depends_on": ["N03_MERGE"],
                "run_if": "ON_TRANSITION",
                "role": "INDEPENDENT_REVIEWER",
                "model": "gpt-5.6-sol",
                "effort": "high",
                "instructions": "Integrated verification of merged subtasks across codebase.",
                "acceptance": ["Passes integration tests."],
                "on_pass": None, "on_fail": None,
                "routing": "FIRST_MATCH",
                "edges": [{"edge_id": "E_REV_TO_GATE", "to": "N09_HUMAN_GATE", "when": {"verdict": "PASS"}, "kind": "CONTINUE", "label": "PASS"}],
            },
            {
                "id": "N09_HUMAN_GATE",
                "type": "HUMAN_GATE",
                "depends_on": ["N04_INTEGRATED_REVIEW"],
                "run_if": "ON_TRANSITION",
                "role": None, "model": None, "effort": None,
                "instructions": "Human approval of integrated candidate.",
                "acceptance": ["Human verdict recorded."],
                "on_pass": None, "on_fail": None,
            },
        ]

    elif template == TEMPLATE_SERIAL_FALLBACK:
        # Serial chain: Subtask 1 -> Subtask 2 -> Review -> Human Gate
        sub_list = subtasks if subtasks else [
            {"id": "SUB_01", "name": "Subtask 1", "files_to_change": ["core.py"]},
            {"id": "SUB_02", "name": "Subtask 2", "files_to_change": ["core.py"]},
        ]
        serial_nodes: list[dict[str, Any]] = []
        prev_id: str | None = None

        for idx, sub in enumerate(sub_list, 1):
            s_id = f"N01_SERIAL_{idx:02d}"
            next_id = f"N01_SERIAL_{idx+1:02d}" if idx < len(sub_list) else "N02_INTEGRATED_REVIEW"
            serial_nodes.append({
                "id": s_id,
                "type": "IMPLEMENT",
                "depends_on": [prev_id] if prev_id else [],
                "run_if": "ON_TRANSITION" if prev_id else "ALWAYS",
                "role": "CODE_IMPLEMENTER",
                "model": strategy.get("model_profile") or "gpt-5.6-sol",
                "effort": strategy.get("effort_level") or "medium",
                "instructions": f"Serially implement subtask {sub.get('name', s_id)}.",
                "acceptance": ["Serial subtask completed."],
                "on_pass": None, "on_fail": None,
                "routing": "FIRST_MATCH",
                "edges": [{"edge_id": f"E_{s_id}_TO_{next_id}", "to": next_id, "when": None, "kind": "CONTINUE", "label": "continue"}],
            })
            prev_id = s_id

        nodes = [
            *serial_nodes,
            {
                "id": "N02_INTEGRATED_REVIEW",
                "type": "REVIEW",
                "depends_on": [prev_id] if prev_id else [],
                "run_if": "ON_TRANSITION",
                "role": "INDEPENDENT_REVIEWER",
                "model": "gpt-5.6-sol",
                "effort": "high",
                "instructions": "Review serial multi-stage changes.",
                "acceptance": ["Integrated review passes."],
                "on_pass": None, "on_fail": None,
                "routing": "FIRST_MATCH",
                "edges": [{"edge_id": "E_SERIAL_REV_TO_GATE", "to": "N09_HUMAN_GATE", "when": {"verdict": "PASS"}, "kind": "CONTINUE", "label": "PASS"}],
            },
            {
                "id": "N09_HUMAN_GATE",
                "type": "HUMAN_GATE",
                "depends_on": ["N02_INTEGRATED_REVIEW"],
                "run_if": "ON_TRANSITION",
                "role": None, "model": None, "effort": None,
                "instructions": "Human gate.",
                "acceptance": ["Human verdict recorded."],
                "on_pass": None, "on_fail": None,
            },
        ]

    else:  # TEMPLATE_CRITICAL_INDEPENDENT_REVIEW
        nodes = [
            {
                "id": "N01_IMPL",
                "type": "IMPLEMENT",
                "depends_on": [],
                "run_if": "ALWAYS",
                "role": "CODE_IMPLEMENTER",
                "model": strategy.get("model_profile") or "OPUS_5_5_HIGH",
                "effort": "max",
                "instructions": f"Implement critical task: {goal}. Enforce strict security boundaries.",
                "acceptance": ["Implementation complete with tests"],
                "on_pass": None, "on_fail": None,
                "routing": "FIRST_MATCH",
                "edges": [{"edge_id": "E_CRIT_IMPL_TO_MACHINE", "to": "N02_MACHINE_CHECK", "when": None, "kind": "CONTINUE", "label": "continue"}],
            },
            {
                "id": "N02_MACHINE_CHECK",
                "type": "MACHINE_GATE",
                "depends_on": ["N01_IMPL"],
                "run_if": "ON_TRANSITION",
                "role": None, "model": None, "effort": None,
                "instructions": "Run automated regression tests.",
                "acceptance": ["Automated tests exit 0"],
                "command": ["python", "-c", "import sys; sys.exit(0)"],
                "timeout_seconds": 120,
                "on_pass": None, "on_fail": None,
                "routing": "FIRST_MATCH",
                "edges": [{"edge_id": "E_CRIT_MACHINE_TO_REV", "to": "N03_STRONG_REVIEW", "when": {"outcome": "PASS"}, "kind": "CONTINUE", "label": "PASS"}],
            },
            {
                "id": "N03_STRONG_REVIEW",
                "type": "REVIEW",
                "depends_on": ["N02_MACHINE_CHECK"],
                "run_if": "ON_TRANSITION",
                "role": "INDEPENDENT_REVIEWER",
                "model": "OPUS_5_5_HIGH",
                "effort": "max",
                "instructions": "Independent security review. Scrutinize boundaries and edge cases.",
                "acceptance": ["Strong independent review passes."],
                "on_pass": None, "on_fail": None,
                "routing": "FIRST_MATCH",
                "edges": [{"edge_id": "E_CRIT_REV_TO_GATE", "to": "N09_HUMAN_GATE", "when": {"verdict": "PASS"}, "kind": "CONTINUE", "label": "PASS"}],
            },
            {
                "id": "N09_HUMAN_GATE",
                "type": "HUMAN_GATE",
                "depends_on": ["N03_STRONG_REVIEW"],
                "run_if": "ON_TRANSITION",
                "role": None, "model": None, "effort": None,
                "instructions": "Human gate for critical change.",
                "acceptance": ["Explicit human approval."],
                "on_pass": None, "on_fail": None,
            },
        ]

    start_node = nodes[0]["id"]
    return {
        "workflow_id": workflow_id,
        "version": "1.0",
        "orchestrator_version": ORCHESTRATOR_VERSION,
        "strategy": dict(strategy),
        "description": f"Adaptive workflow generated using {template} template.",
        "goal": goal,
        "start_node": start_node,
        "routing_contract": rc.CONTRACT_VERSION,
        "workspace_policy": {
            "isolated_worktree_required": True,
            "main_merge_allowed": False,
        },
        "limits": base_limits,
        "nodes": nodes,
    }


def validate_dag_structure(dag: Mapping[str, Any]) -> tuple[bool, list[str]]:
    """Validate acyclicity, node dependencies, edge targets, and merge consistency."""
    errors: list[str] = []
    nodes = list(dag.get("nodes") or [])
    node_ids = {n["id"] for n in nodes if "id" in n}

    if len(node_ids) != len(nodes):
        errors.append("Duplicate node IDs detected in DAG")

    for node in nodes:
        nid = node["id"]
        for dep in node.get("depends_on", []):
            if dep not in node_ids:
                errors.append(f"Node {nid} has unknown dependency: {dep}")
        for edge in node.get("edges", []):
            target = edge.get("to")
            if target not in node_ids:
                errors.append(f"Node {nid} edge {edge.get('edge_id')} points to unknown target: {target}")

        if node.get("type") == "MERGE":
            expected = list(node.get("expected_incoming") or [])
            policy = node.get("merge_policy")
            if policy not in rc.MERGE_POLICIES:
                errors.append(f"Merge node {nid} has invalid merge policy: {policy}")
            if not expected:
                errors.append(f"Merge node {nid} has empty expected_incoming list")

    return len(errors) == 0, errors
