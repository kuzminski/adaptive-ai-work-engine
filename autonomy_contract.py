#!/usr/bin/env python3
"""AAW AUTONOMOUS ITERATIONS V0.1 — the pure contract layer.

Everything here is a deterministic function of its arguments: no clock, no
filesystem writes, no model calls. `autonomy_controller.py` owns state and
I/O and delegates every *decision* to this module, so any decision can be
recomputed from stored evidence and compared with what the run actually did
(the same discipline `routing_contract.evaluate_gate` established).

Authority split — the point of the whole design:

  * a human supplies a MANDATE (iteration contract + roadmap mandate);
  * the planner may choose *how* and *in what order*, never *what for*;
  * a passing, independently reviewed iteration hands control back to the
    controller, which re-asks the planner — not the human — what comes next;
  * the human is a mandatory gate only at the end of the roadmap, at a real
    escalation, and before PROMOTE. Autonomy never includes promotion.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

CONTRACT_ID = "AAW_AUTONOMOUS_ITERATIONS_V0.3"
SCHEMA_VERSION = "AAW_AUTONOMY_STATE_V0.3"
JOURNAL_SCHEMA_VERSION = "AAW_AUTONOMY_JOURNAL_V0.3"
# V0.3 keeps the V0.2 execution/ledger protocol and can read historical runs.
READABLE_SCHEMA_VERSIONS = ("AAW_AUTONOMY_STATE_V0.1", "AAW_AUTONOMY_STATE_V0.2", SCHEMA_VERSION)

# ── lifecycle ────────────────────────────────────────────────────────────────

PLAN = "PLAN"
EXECUTE = "EXECUTE"
SELF_VERIFY = "SELF_VERIFY"
AWAITING_REVIEW = "AWAITING_REVIEW"
REVIEW = "REVIEW"
REPAIR = "REPAIR"
FINAL_REVIEW = "FINAL_REVIEW"
ROADMAP_CHECK = "ROADMAP_CHECK"
AWAITING_HUMAN = "AWAITING_HUMAN"
HUMAN_APPROVED = "HUMAN_APPROVED"
PROMOTE = "PROMOTE"
PROMOTED = "PROMOTED"
REJECTED = "REJECTED"

# The complete edge set. `transition_allowed` is the only gate on phase moves,
# so "can the agent reach PROMOTE without a human" reduces to reading this
# table: PROMOTE is only reachable from HUMAN_APPROVED, which is only
# reachable from AWAITING_HUMAN.
TRANSITIONS: dict[str, frozenset[str]] = {
    PLAN: frozenset({EXECUTE, AWAITING_HUMAN}),
    EXECUTE: frozenset({SELF_VERIFY, AWAITING_HUMAN}),
    SELF_VERIFY: frozenset({AWAITING_REVIEW, REPAIR, AWAITING_HUMAN}),
    AWAITING_REVIEW: frozenset({REVIEW, AWAITING_HUMAN}),
    REVIEW: frozenset({REPAIR, FINAL_REVIEW, AWAITING_HUMAN}),
    # Every repair returns through self-verification and primary review before
    # a fresh final review; the same bounded repair/no-progress gates apply.
    REPAIR: frozenset({SELF_VERIFY, FINAL_REVIEW, AWAITING_HUMAN}),
    FINAL_REVIEW: frozenset({REPAIR, ROADMAP_CHECK, AWAITING_HUMAN}),
    ROADMAP_CHECK: frozenset({PLAN, AWAITING_HUMAN}),
    AWAITING_HUMAN: frozenset({HUMAN_APPROVED, REJECTED}),
    HUMAN_APPROVED: frozenset({PROMOTE}),
    PROMOTE: frozenset({PROMOTED}),
    PROMOTED: frozenset(),
    REJECTED: frozenset(),
}
PHASES = tuple(TRANSITIONS)
# Phases whose executor may have changed the worktree. An interrupted one is
# never silently re-run (the V0.2 rule: no repeated side effects).
SIDE_EFFECT_PHASES = frozenset({EXECUTE, REPAIR})

# V0.2: every role call is one V0.4A execution. The invocation kind is drawn
# from the closed V0.4A vocabulary (IMPLEMENT / SELF_VERIFY are `LLM` with the
# role kept as a node attribute, exactly as V0.4A prescribes).
INVOCATION_KIND_BY_EXECUTOR: dict[str, str] = {
    "plan": "PLAN", "execute": "LLM", "self_verify": "LLM", "review": "REVIEW",
    "repair": "REPAIR", "final_review": "REVIEW", "prepare_packet": "PREPROCESS",
}

RUNNING = "RUNNING"
STATUSES = (RUNNING, AWAITING_HUMAN, HUMAN_APPROVED, PROMOTED, REJECTED)

# Why the controller stopped for a human.
HOLD_ROADMAP_EXHAUSTED = "ROADMAP_EXHAUSTED"
HOLD_ITERATION_CAP = "ITERATION_CAP_REACHED"
HOLD_ESCALATION = "ESCALATION"

# Verdicts a reviewer may return.
V_PASS = "PASS"
V_REPAIR = "REPAIR_REQUIRED"
V_ESCALATE = "ESCALATE"
REVIEW_VERDICTS = (V_PASS, V_REPAIR, V_ESCALATE)
SEVERITY_LADDER = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
BLOCKING_SEVERITIES = frozenset({"HIGH", "CRITICAL"})
FAILING_STATUSES = frozenset({"FAIL", "ERROR"})
ADVERSE_STATUSES = frozenset({"FAIL", "ERROR", "WARN", "WARNING", "SKIPPED"})

# Absolute ceilings no mandate can raise: a human typo (max_iterations: 10**9)
# must not turn into an unbounded autonomous run. These are execution fuses,
# not the definition of autonomy: the loop PLAN -> ... -> FINAL_REVIEW ->
# ROADMAP_CHECK -> PLAN runs as long as the roadmap (including a standing
# `recurring` item) offers justified work and no fuse fires. They are generous
# on purpose (V0.1-V0.3 capped at 50).
HARD_MAX_ITERATIONS = 200
DEFAULT_MAX_ITERATIONS = 40
HARD_MAX_REPAIR_ATTEMPTS = 6

# Escalation codes (closed vocabulary; the journal and tests key on these).
E_SCOPE = "OUT_OF_MANDATE"
E_MANDATE_MISMATCH = "MANDATE_MISMATCH"
E_MANDATE_TAMPERED = "MANDATE_TAMPERED"
E_EXTENSION = "MANDATE_EXTENSION_ATTEMPT"
E_LINK = "ROADMAP_LINK_UNPROVEN"
E_DECISION = "DECISION_REQUIRES_HUMAN"
E_PLAN_INVALID = "PLAN_INVALID"
E_REVIEW_INVALID = "REVIEW_RESULT_INVALID"
E_REVIEW = "REVIEW_ESCALATED"
E_REPAIR_LIMIT = "REPAIR_LIMIT_EXCEEDED"
E_NO_PROGRESS = "REPAIR_NO_PROGRESS"
E_PLANNER = "PLANNER_ESCALATED"
E_GIT = "GIT_BOUNDARY_VIOLATION"
E_INTERRUPTED = "INTERRUPTED_IN_FLIGHT"
E_EXECUTOR = "EXECUTOR_FAILED"
E_EXECUTOR_TIMEOUT = "EXECUTOR_TIMEOUT"
E_UNEXPLAINED_END = "ROADMAP_END_UNJUSTIFIED"
# V0.2
E_ROLE_UNAVAILABLE = "ROLE_PROFILE_UNAVAILABLE"      # configured profile not runnable now; no substitution
E_LEDGER = "LEDGER_WRITE_ERROR"                      # durable INTENT failed: nothing was dispatched
E_RECONCILE = "RECONCILIATION_REQUIRED"              # lifecycle evidence is ambiguous; a human decides
E_SESSION_REUSE = "PROVIDER_SESSION_REUSED"          # a fresh-context role reported a reused provider session
E_VERIFY_MUTATION = "SELF_VERIFY_MUTATED_WORKTREE"   # a read-only phase changed the diff


class AutonomyError(ValueError):
    """Fail-closed contract violation."""

    classification = "AUTONOMY_CONTRACT_ERROR"


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AutonomyError(message)


def canonical_hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def transition_allowed(source: str, target: str) -> bool:
    return target in TRANSITIONS.get(source, frozenset())


# ── mandate ──────────────────────────────────────────────────────────────────

def _str_list(value: Any, name: str, *, non_empty: bool = False) -> list[str]:
    _require(isinstance(value, list) and all(isinstance(x, str) and x.strip() for x in value),
             f"{name} must be an array of non-empty strings")
    _require(bool(value) or not non_empty, f"{name} must not be empty")
    return list(value)


def validate_mandate(data: Any) -> dict[str, Any]:
    """Validate the human's first prompt and freeze it with a content hash.

    The two halves are deliberately separate objects: `iteration_contract` is
    an exact spec for iteration 1; `roadmap_mandate` is direction, not a TODO
    list. The hash covers both, so any later edit is detectable.
    """
    _require(isinstance(data, dict), "mandate must be a JSON object")
    mandate_id = data.get("mandate_id")
    _require(isinstance(mandate_id, str) and mandate_id.strip(), "mandate_id must be a non-empty string")

    contract = data.get("iteration_contract")
    _require(isinstance(contract, dict), "iteration_contract must be an object")
    _require(isinstance(contract.get("goal"), str) and contract["goal"].strip(), "iteration_contract.goal is required")
    _str_list(contract.get("acceptance_criteria"), "iteration_contract.acceptance_criteria", non_empty=True)
    for key in ("scope", "constraints", "forbidden_changes", "required_evidence"):
        _str_list(contract.get(key, []), f"iteration_contract.{key}")

    roadmap = data.get("roadmap_mandate")
    _require(isinstance(roadmap, dict), "roadmap_mandate must be an object")
    _require(isinstance(roadmap.get("objective"), str) and roadmap["objective"].strip(),
             "roadmap_mandate.objective is required")
    items = roadmap.get("items")
    _require(isinstance(items, list), "roadmap_mandate.items must be an array")
    ids: set[str] = set()
    for item in items:
        _require(isinstance(item, dict), "every roadmap item must be an object")
        item_id = item.get("item_id")
        _require(isinstance(item_id, str) and item_id.strip(), "every roadmap item needs an item_id")
        _require(item_id not in ids, f"duplicate roadmap item_id {item_id!r}")
        ids.add(item_id)
        _require(isinstance(item.get("title"), str) and item["title"].strip(), f"{item_id}: title is required")
        _require("human_required" not in item or type(item["human_required"]) is bool,
                 f"{item_id}: human_required must be a boolean")
        _require("recurring" not in item or type(item["recurring"]) is bool,
                 f"{item_id}: recurring must be a boolean")
        _require(not (item.get("recurring") and item.get("human_required")),
                 f"{item_id}: a recurring standing item cannot be human_required")
    for item in items:
        deps = item.get("depends_on", [])
        _require(isinstance(deps, list) and all(d in ids and d != item["item_id"] for d in deps),
                 f"{item['item_id']}: depends_on must name other roadmap items")

    bounds = roadmap.get("autonomy_bounds")
    _require(isinstance(bounds, dict), "roadmap_mandate.autonomy_bounds must be an object")
    for key, ceiling in (("max_iterations", HARD_MAX_ITERATIONS),
                         ("max_repair_attempts", HARD_MAX_REPAIR_ATTEMPTS)):
        value = bounds.get(key)
        _require(type(value) is int and 0 < value <= ceiling, f"autonomy_bounds.{key} must be an integer in 1..{ceiling}")
    allowed = bounds.get("allowed_areas")
    _require(allowed is None or isinstance(allowed, list) and all(isinstance(x, str) and x for x in allowed),
             "autonomy_bounds.allowed_areas must be null or an array of path prefixes")
    _str_list(bounds.get("forbidden_areas", []), "autonomy_bounds.forbidden_areas")
    _require("critical_scope" not in bounds or type(bounds["critical_scope"]) is bool,
             "autonomy_bounds.critical_scope must be a boolean")
    overrides = data.get("model_policy_overrides", {})
    _require(isinstance(overrides, dict), "model_policy_overrides must be an object")
    _require(set(overrides).issubset({"implementation"}),
             "model_policy_overrides may only name implementation")
    if "implementation" in overrides:
        override = overrides["implementation"]
        _require(isinstance(override, dict)
                 and override.get("profile_key") == "implementer_capability_escalation"
                 and isinstance(override.get("reason"), str) and override["reason"].strip(),
                 "model_policy_overrides.implementation must explicitly name the escalation profile and reason")
    for key in ("priorities", "possible_directions"):
        _str_list(roadmap.get(key, []), f"roadmap_mandate.{key}")

    frozen = json.loads(json.dumps(data, ensure_ascii=False))
    frozen.pop("mandate_hash", None)
    frozen["mandate_hash"] = canonical_hash(frozen)
    return frozen


def mandate_hash_ok(mandate: Mapping[str, Any]) -> bool:
    body = {k: v for k, v in mandate.items() if k != "mandate_hash"}
    return mandate.get("mandate_hash") == canonical_hash(body)


# ── autonomy levels ──────────────────────────────────────────────────────────

AUTO = "AUTO"
AUTO_WITHIN_SCOPE = "AUTO_WITHIN_SCOPE"
ESCALATE = "ESCALATE"

LEVEL_BY_KIND: dict[str, str] = {
    # inside an iteration: the implementer's call
    "IMPLEMENTATION_STRUCTURE": AUTO, "REFACTORING": AUTO, "LOCAL_TECHNICAL": AUTO,
    "TESTS": AUTO, "REPAIR_STRATEGY": AUTO, "ORDER_WITHIN_ITERATION": AUTO,
    # how to reach the goal: the planner's call, but only against the mandate
    "INTERNAL_ARCHITECTURE": AUTO_WITHIN_SCOPE, "REORDER_ROADMAP": AUTO_WITHIN_SCOPE,
    "SPLIT_STAGE": AUTO_WITHIN_SCOPE, "MERGE_STAGES": AUTO_WITHIN_SCOPE,
    "SKIP_STAGE": AUTO_WITHIN_SCOPE, "REPLACE_STAGE": AUTO_WITHIN_SCOPE,
    # what and whether: never the planner's call
    "GOAL_CHANGE": ESCALATE, "REQUIREMENT_CHANGE": ESCALATE, "PRODUCT_BEHAVIOR_CHANGE": ESCALATE,
    "PUBLIC_CONTRACT_CHANGE": ESCALATE, "MANDATE_EXTENSION": ESCALATE,
    "IRREVERSIBLE_MIGRATION": ESCALATE, "BUSINESS_DECISION": ESCALATE,
    "UNACCEPTABLE_RISK": ESCALATE, "REQUIREMENT_CONFLICT": ESCALATE,
}


def classify_decision(kind: Any) -> str:
    """Unknown kinds escalate: a new kind of decision has no pre-granted mandate."""
    return LEVEL_BY_KIND.get(str(kind), ESCALATE)


# ── plan / scope guard ───────────────────────────────────────────────────────

# Keys through which a plan could try to grow its own mandate.
EXTENSION_KEYS = frozenset({"mandate_amendment", "new_roadmap_items", "add_roadmap_items",
                            "extend_mandate", "new_mandate", "autonomy_bounds"})
PLAN_ITERATION = "ITERATION"
PLAN_NO_FURTHER_ACTION = "NO_FURTHER_ACTION"
PLAN_ESCALATE = "ESCALATE"
PLAN_STATUSES = (PLAN_ITERATION, PLAN_NO_FURTHER_ACTION, PLAN_ESCALATE)

R_PENDING, R_DONE, R_SKIPPED, R_HUMAN_REQUIRED = "PENDING", "DONE", "SKIPPED", "HUMAN_REQUIRED"


def initial_roadmap(mandate: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    items = mandate["roadmap_mandate"]["items"]
    roadmap = {}
    for item in items:
        human_required = item.get("human_required") is True
        roadmap[item["item_id"]] = {
            "status": R_HUMAN_REQUIRED if human_required else R_PENDING,
            "iteration_id": None,
            "reason": "human-required roadmap item" if human_required else None,
            "depends_on": list(item.get("depends_on", [])),
            "dependency_state": "HUMAN_REQUIRED" if human_required else "READY",
            "human_required": human_required,
        }
        if item.get("recurring") is True:
            # A standing item: an accepted iteration records progress on it but never completes it.
            roadmap[item["item_id"]]["recurring"] = True
            roadmap[item["item_id"]]["iterations"] = []
    refresh_dependency_states(roadmap)
    return roadmap


def refresh_dependency_states(roadmap: dict[str, dict[str, Any]]) -> None:
    """Keep dependency readiness visible without treating missing work as done."""
    for item in roadmap.values():
        if item.get("status") in {R_DONE, R_SKIPPED, R_HUMAN_REQUIRED}:
            continue
        dependencies = [roadmap.get(dep, {}) for dep in item.get("depends_on", [])]
        if any(dep.get("status") == R_HUMAN_REQUIRED for dep in dependencies):
            item["dependency_state"] = "HUMAN_REQUIRED"
        elif any(dep.get("status") == R_SKIPPED for dep in dependencies):
            item["dependency_state"] = "BLOCKED_BY_SKIPPED_DEPENDENCY"
        elif all(dep.get("status") == R_DONE for dep in dependencies):
            item["dependency_state"] = "READY"
        else:
            item["dependency_state"] = "WAITING_FOR_DEPENDENCIES"


def remaining_items(roadmap: Mapping[str, Mapping[str, Any]]) -> list[str]:
    return [k for k, v in roadmap.items() if v["status"] == R_PENDING]


def autonomous_remaining_items(roadmap: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """Pending work the autonomous planner can still select under current gates."""
    return [item_id for item_id in remaining_items(roadmap)
            if roadmap[item_id].get("dependency_state") not in {"HUMAN_REQUIRED", "BLOCKED_BY_SKIPPED_DEPENDENCY"}]


# Human Gate conditions every directional charter must carry verbatim.
REQUIRED_CHARTER_GATE_CONDITIONS = ("ROADMAP_EXHAUSTED", "SCOPE_CHANGE", "ROLE_PROFILE_UNAVAILABLE",
                                    "PROMOTION_REQUIRES_HUMAN")


def directional_charter_template(mandate: Mapping[str, Any]) -> dict[str, Any]:
    """The charter fields that are verbatim copies of the frozen mandate.

    Handed to the initial architect so it does not have to re-derive them;
    `validate_directional_charter` remains the authority and still checks
    every field. The architect contributes `risk_guidance` (and may append
    further Human Gate conditions).
    """
    source = mandate["roadmap_mandate"]
    contract = mandate["iteration_contract"]
    return {"mandate_hash": mandate["mandate_hash"], "objective": source["objective"],
            "roadmap_items": [{"item_id": item["item_id"], "title": item["title"],
                               "depends_on": list(item.get("depends_on", [])),
                               "human_required": item.get("human_required") is True}
                              for item in source["items"]],
            "acceptance_criteria": list(contract["acceptance_criteria"]),
            "boundaries": {"scope": list(contract.get("scope", [])),
                           "constraints": list(contract.get("constraints", [])),
                           "forbidden_changes": list(contract.get("forbidden_changes", [])),
                           "allowed_areas": source["autonomy_bounds"].get("allowed_areas"),
                           "forbidden_areas": list(source["autonomy_bounds"].get("forbidden_areas", []))},
            "human_gate_conditions": list(REQUIRED_CHARTER_GATE_CONDITIONS)}


def validate_directional_charter(value: Any, mandate: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the one-time architect output against the immutable human mandate."""
    _require(isinstance(value, dict), "initial architect must return a directional_charter object")
    _require(mandate_hash_ok(mandate), "the frozen mandate no longer matches its hash")
    _require(value.get("mandate_hash") == mandate["mandate_hash"],
             "directional charter does not reference the frozen human mandate")
    source = mandate["roadmap_mandate"]
    expected_items = [{"item_id": item["item_id"], "title": item["title"],
                       "depends_on": list(item.get("depends_on", [])),
                       "human_required": item.get("human_required") is True}
                      for item in source["items"]]
    _require(value.get("objective") == source["objective"],
             "directional charter changed the human roadmap objective")
    _require(value.get("roadmap_items") == expected_items,
             "directional charter added, removed, or changed roadmap items or dependencies")
    item_ids = {item["item_id"] for item in expected_items}
    risk_guidance = value.get("risk_guidance")
    _require(isinstance(risk_guidance, list), "directional charter risk_guidance must be an array")
    normalized_risk: list[dict[str, str]] = []
    seen_risks: set[str] = set()
    for row in risk_guidance:
        _require(isinstance(row, dict) and row.get("item_id") in item_ids,
                 "each risk_guidance row must reference a roadmap item")
        item_id = row["item_id"]
        _require(item_id not in seen_risks, f"duplicate risk_guidance for {item_id!r}")
        seen_risks.add(item_id)
        implementation_floor = row.get("implementation_floor")
        review_floor = row.get("final_review_floor")
        reason = row.get("reason")
        _require(implementation_floor in {"NORMAL", "HARDER", "SIGNIFICANTLY_DIFFICULT"},
                 f"{item_id}: invalid implementation_floor")
        _require(review_floor in {"DEFAULT", "HARD", "CRITICAL"}, f"{item_id}: invalid final_review_floor")
        _require(isinstance(reason, str) and reason.strip(), f"{item_id}: risk guidance needs a reason")
        normalized_risk.append({"item_id": item_id, "implementation_floor": implementation_floor,
                                "final_review_floor": review_floor, "reason": reason.strip()})
    _require(value.get("acceptance_criteria") == mandate["iteration_contract"]["acceptance_criteria"],
             "directional charter changed the human acceptance criteria")
    expected_boundaries = {
        "scope": list(mandate["iteration_contract"].get("scope", [])),
        "constraints": list(mandate["iteration_contract"].get("constraints", [])),
        "forbidden_changes": list(mandate["iteration_contract"].get("forbidden_changes", [])),
        "allowed_areas": source["autonomy_bounds"].get("allowed_areas"),
        "forbidden_areas": list(source["autonomy_bounds"].get("forbidden_areas", [])),
    }
    _require(value.get("boundaries") == expected_boundaries,
             "directional charter changed or omitted human boundaries")
    required_gates = set(REQUIRED_CHARTER_GATE_CONDITIONS)
    gates = value.get("human_gate_conditions")
    _require(isinstance(gates, list) and required_gates.issubset(set(gates)),
             "directional charter removed a mandatory Human Gate condition")
    frozen = {key: value[key] for key in ("mandate_hash", "objective", "roadmap_items", "acceptance_criteria",
                                          "boundaries", "human_gate_conditions")}
    frozen["risk_guidance"] = normalized_risk
    return {**frozen, "charter_hash": canonical_hash(frozen)}


def _area_match(path: str, areas: Sequence[str]) -> bool:
    norm = path.replace("\\", "/").lstrip("./")
    return any(norm == a.rstrip("/").lstrip("./") or norm.startswith(a.rstrip("/").lstrip("./") + "/") for a in areas)


def check_plan(plan: Any, mandate: Mapping[str, Any], roadmap: Mapping[str, Mapping[str, Any]],
               iteration_index: int, *, expected_mandate_hash: str | None = None,
               expected_directional_charter_hash: str | None = None) -> dict[str, Any]:
    """Decide whether a planner proposal stays inside the human's mandate.

    Returns `{"decision": "ACCEPT"|"ESCALATE", "code", "reasons", "levels"}`.
    The planner cannot appeal an ESCALATE: the controller stops autonomy. This
    is the anti-scope-creep gate — every accepted plan carries a proven link to
    the roadmap, a scope justification and completion criteria, or it does not
    run at all.
    """
    reasons: list[str] = []
    levels: list[dict[str, str]] = []

    def escalate(code: str, *why: str) -> dict[str, Any]:
        return {"decision": ESCALATE, "code": code, "reasons": reasons + list(why), "levels": levels}

    if not isinstance(plan, dict):
        return escalate(E_PLAN_INVALID, "plan must be an object")
    if not mandate_hash_ok(mandate) or (expected_mandate_hash and mandate.get("mandate_hash") != expected_mandate_hash):
        return escalate(E_MANDATE_TAMPERED, "the frozen mandate no longer matches its hash")
    if plan.get("mandate_hash") != mandate["mandate_hash"]:
        return escalate(E_MANDATE_MISMATCH, "plan was not derived from the frozen mandate")
    if expected_directional_charter_hash:
        if plan.get("directional_charter_hash") != expected_directional_charter_hash:
            return escalate(E_MANDATE_MISMATCH, "plan was not derived from the frozen directional charter")
        if plan.get("directional_charter") is not None:
            return escalate(E_EXTENSION, "a later planner attempted to rewrite the frozen directional charter")
    extension = sorted(EXTENSION_KEYS & set(plan))
    if extension and any(plan.get(k) for k in extension):
        return escalate(E_EXTENSION, f"planner attempted to alter its own mandate via {extension}")
    complexity = plan.get("implementation_complexity", "NORMAL")
    if complexity not in {"NORMAL", "HARDER", "SIGNIFICANTLY_DIFFICULT"}:
        return escalate(E_PLAN_INVALID, f"unknown implementation_complexity {complexity!r}")
    complexity_evidence = plan.get("complexity_evidence", [])
    if not (isinstance(complexity_evidence, list)
            and all(isinstance(item, str) and item.strip() for item in complexity_evidence)):
        return escalate(E_PLAN_INVALID, "complexity_evidence must be an array of non-empty strings")
    if complexity != "NORMAL" and not complexity_evidence:
        return escalate(E_PLAN_INVALID, "a harder implementation tier needs concrete complexity_evidence")
    semantic_required = plan.get("semantic_verification_required", False)
    semantic_reason = plan.get("semantic_verification_reason")
    if type(semantic_required) is not bool:
        return escalate(E_PLAN_INVALID, "semantic_verification_required must be a boolean")
    if semantic_required and not (isinstance(semantic_reason, str) and semantic_reason.strip()):
        return escalate(E_PLAN_INVALID, "semantic verification needs an explicit reason")

    status = plan.get("status", PLAN_ITERATION)
    if status not in PLAN_STATUSES:
        return escalate(E_PLAN_INVALID, f"unknown plan status {status!r}")
    if status == PLAN_ESCALATE:
        return escalate(E_PLANNER, str(plan.get("reason") or "planner requested a human decision"))

    bounds = mandate["roadmap_mandate"]["autonomy_bounds"]
    remaining = remaining_items(roadmap)

    skipped = plan.get("skipped_items", [])
    skipped_ids: list[str] = []
    if not isinstance(skipped, list):
        return escalate(E_PLAN_INVALID, "skipped_items must be an array")
    for row in skipped:
        ok = (isinstance(row, dict) and row.get("item_id") in remaining
              and isinstance(row.get("reason"), str) and row["reason"].strip())
        if not ok:
            return escalate(E_LINK, f"a skipped roadmap item needs a pending item_id and a reason: {row!r}")
        skipped_ids.append(row["item_id"])
        levels.append({"kind": "SKIP_STAGE", "level": AUTO_WITHIN_SCOPE})

    if status == PLAN_NO_FURTHER_ACTION:
        # The roadmap is not an unconditional backlog, but it is also not
        # something a planner may silently stop reading: every remaining item
        # must be explicitly, individually justified away.
        if iteration_index == 1:
            return escalate(E_PLAN_INVALID, "iteration 1 is fixed by the human's iteration contract")
        unexplained = [i for i in remaining if i not in skipped_ids]
        if unexplained:
            return escalate(E_UNEXPLAINED_END, f"roadmap ended with unjustified pending items {unexplained}")
        return {"decision": ACCEPT_END, "code": None, "reasons": [], "levels": levels, "skipped": skipped_ids}

    # status == ITERATION
    for key in ("goal", "scope_justification"):
        if not (isinstance(plan.get(key), str) and plan[key].strip()):
            return escalate(E_LINK if key == "scope_justification" else E_PLAN_INVALID, f"plan.{key} is required")
    criteria = plan.get("acceptance_criteria")
    if not (isinstance(criteria, list) and criteria and all(isinstance(c, str) and c.strip() for c in criteria)):
        return escalate(E_PLAN_INVALID, "plan.acceptance_criteria (completion criteria) are required")
    if iteration_index == 1:
        # The human's acceptance criteria for iteration 1 are not the planner's to weaken.
        missing = [c for c in mandate["iteration_contract"]["acceptance_criteria"] if c not in criteria]
        if missing:
            return escalate(E_SCOPE, f"plan dropped human acceptance criteria: {missing}")

    refs = plan.get("roadmap_refs", [])
    if not (isinstance(refs, list) and all(isinstance(r, str) for r in refs)):
        return escalate(E_PLAN_INVALID, "plan.roadmap_refs must be an array")
    if iteration_index > 1:
        if not refs:
            return escalate(E_LINK, "iteration has no demonstrated link to the roadmap mandate")
        unknown = [r for r in refs if r not in remaining]
        if unknown:
            return escalate(E_LINK, f"roadmap_refs not pending in the mandate: {unknown}")
    elif any(r not in roadmap for r in refs):
        return escalate(E_LINK, "roadmap_refs name items that do not exist")
    overlap = set(refs) & set(skipped_ids)
    if overlap:
        return escalate(E_PLAN_INVALID, f"items both planned and skipped: {sorted(overlap)}")
    by_item = {i["item_id"]: i for i in mandate["roadmap_mandate"]["items"]}
    for ref in refs:
        if by_item[ref].get("human_required") is True or roadmap[ref].get("status") == R_HUMAN_REQUIRED:
            return escalate(E_SCOPE, f"{ref} is explicitly human-required and cannot be selected autonomously")
        dependencies = by_item[ref].get("depends_on", [])
        gated = [d for d in dependencies if roadmap[d]["status"] in {R_HUMAN_REQUIRED, R_SKIPPED}]
        if gated:
            return escalate(E_SCOPE, f"{ref} depends on human-required or skipped items {gated}")
        unmet = [d for d in dependencies if roadmap[d]["status"] == R_PENDING and d not in refs]
        if unmet:
            return escalate(E_SCOPE, f"{ref} depends on unfinished items {unmet}")

    areas = plan.get("touched_areas", [])
    if not (isinstance(areas, list) and all(isinstance(a, str) for a in areas)):
        return escalate(E_PLAN_INVALID, "plan.touched_areas must be an array of paths")
    allowed, forbidden = bounds.get("allowed_areas"), bounds.get("forbidden_areas", [])
    for area in areas:
        if allowed is not None and not _area_match(area, allowed):
            return escalate(E_SCOPE, f"{area} is outside the mandate's allowed areas")
        if _area_match(area, forbidden):
            return escalate(E_SCOPE, f"{area} is a forbidden area")

    decisions = plan.get("decisions", [])
    if not (isinstance(decisions, list) and all(isinstance(d, dict) for d in decisions)):
        return escalate(E_PLAN_INVALID, "plan.decisions must be an array of objects")
    for row in decisions:
        level = classify_decision(row.get("kind"))
        levels.append({"kind": str(row.get("kind")), "level": level})
        if level == ESCALATE:
            return escalate(E_DECISION, f"decision {row.get('kind')!r} needs a human")
    return {"decision": ACCEPT, "code": None, "reasons": [], "levels": levels, "skipped": skipped_ids}


ACCEPT = "ACCEPT"
ACCEPT_END = "ACCEPT_END"  # the planner legitimately reports nothing sensible remains


# ── review results ───────────────────────────────────────────────────────────

def evidence_failures(evidence: Mapping[str, str]) -> list[str]:
    return sorted(name for name, status in evidence.items() if str(status).upper() in FAILING_STATUSES)


def normalize_review(raw: Any, *, failing_evidence: Sequence[str] = ()) -> dict[str, Any]:
    """Validate a reviewer result and make PASS impossible against known failures.

    Fail-closed: an unparseable result is an ESCALATE, never a silent PASS. A
    PASS that coexists with a blocking finding or with a still-failing check
    is contradictory; it is downgraded to REPAIR_REQUIRED, and the downgrade is
    recorded so the reviewer's original word is never lost.
    """
    if not isinstance(raw, dict) or raw.get("verdict") not in REVIEW_VERDICTS:
        return {"verdict": V_ESCALATE, "summary": "reviewer returned no valid verdict", "findings": [],
                "code": E_REVIEW_INVALID, "downgraded_from": None, "raw": raw if isinstance(raw, dict) else None}
    findings = raw.get("findings", [])
    if not (isinstance(findings, list) and all(isinstance(f, dict) for f in findings)):
        return {"verdict": V_ESCALATE, "summary": "reviewer findings were malformed", "findings": [],
                "code": E_REVIEW_INVALID, "downgraded_from": None, "raw": raw}
    clean: list[dict[str, Any]] = []
    for index, row in enumerate(findings):
        severity = str(row.get("severity", "MEDIUM")).upper()
        severity = severity if severity in SEVERITY_LADDER else "MEDIUM"
        summary = str(row.get("summary", "")).strip() or "(no summary)"
        key = str(row.get("finding_key") or canonical_hash({"s": severity, "m": summary, "f": row.get("file")})[7:19])
        clean.append({**row, "finding_key": key, "severity": severity, "summary": summary,
                      "blocking": bool(row.get("blocking")) or severity in BLOCKING_SEVERITIES})
    verdict, downgraded = raw["verdict"], None
    blocking = [f for f in clean if f["blocking"]]
    if verdict == V_PASS and (blocking or failing_evidence):
        downgraded, verdict = V_PASS, V_REPAIR
        for name in failing_evidence:
            clean.append({"finding_key": f"EVIDENCE_FAILING::{name}", "severity": "HIGH", "blocking": True,
                          "summary": f"check {name!r} is still failing; PASS is not admissible"})
    if verdict == V_REPAIR and not any(f["blocking"] for f in clean):
        # A repair verdict must name something to repair.
        clean.append({"finding_key": "UNSPECIFIED_REPAIR", "severity": "HIGH", "blocking": True,
                      "summary": str(raw.get("summary") or "reviewer required repair without findings")})
    requests = raw.get("raw_evidence_requests", [])
    if not isinstance(requests, list) or any(not isinstance(row, dict) for row in requests):
        return {"verdict": V_ESCALATE, "summary": "raw evidence requests were malformed", "findings": clean,
                "code": E_REVIEW_INVALID, "downgraded_from": downgraded, "raw": None}
    uncertainties = raw.get("uncertainties", [])
    if not (isinstance(uncertainties, list) and all(isinstance(item, str) and item.strip() for item in uncertainties)):
        return {"verdict": V_ESCALATE, "summary": "review uncertainties were malformed", "findings": clean,
                "code": E_REVIEW_INVALID, "downgraded_from": downgraded, "raw": None}
    return {"verdict": verdict, "summary": str(raw.get("summary", "")), "findings": clean,
            "uncertainties": list(uncertainties),
            "raw_evidence_requests": [dict(row) for row in requests],
            "code": None, "downgraded_from": downgraded, "raw": None}


# ── review packet ────────────────────────────────────────────────────────────

def adverse_items(*, execution: Mapping[str, Any] | None, checks: Sequence[Mapping[str, Any]],
                  prior_findings: Sequence[Mapping[str, Any]] = ()) -> list[dict[str, Any]]:
    """Everything unfavourable to the implementation, extracted verbatim.

    This list is built from raw material by code, never by a summarising
    model, and is what compression is forbidden to lose.
    """
    out: list[dict[str, Any]] = []
    for check in checks:
        status = str(check.get("status", "")).upper()
        warnings = check.get("warnings") or []
        if status in ADVERSE_STATUSES or warnings:
            out.append({"source": "CHECK", "name": check.get("name"), "status": status or None,
                        "summary": check.get("summary"), "warnings": list(warnings),
                        "log_ref": check.get("log_ref")})
    for key, label in (("deviations", "DEVIATION"), ("uncertainties", "UNCERTAINTY"), ("unresolved", "UNRESOLVED")):
        for text in (execution or {}).get(key, []) or []:
            out.append({"source": label, "summary": str(text)})
    for row in prior_findings:
        out.append({"source": "PRIOR_FINDING", "finding_key": row.get("finding_key"),
                    "severity": row.get("severity"), "summary": row.get("summary")})
    for item in out:
        item["item_key"] = canonical_hash({k: v for k, v in item.items() if k != "item_key"})[7:23]
    return out


def build_review_packet(*, iteration: Mapping[str, Any], mandate: Mapping[str, Any],
                        diff_sha256: str, changed_files: Sequence[str], head: str | None,
                        base: str | None, worktree: str | None,
                        checks: Sequence[Mapping[str, Any]], adverse: Sequence[Mapping[str, Any]],
                        diff_stat: str | None = None, kind: str = "REVIEW",
                        commits: Sequence[str] = ()) -> dict[str, Any]:
    """The map handed to a reviewer — explicitly not the territory.

    `access` tells the reviewer where the real diff, files and evidence live
    and carries the diff hash the packet was built against, so staleness is
    mechanically detectable. `authoritative` is hard-wired False.
    """
    plan = iteration["plan"]
    execution = iteration.get("execution") or {}
    return {
        "packet_kind": kind, "contract": CONTRACT_ID, "authoritative": False,
        "TASK": {"goal": plan["goal"], "acceptance_criteria": list(plan["acceptance_criteria"]),
                 "constraints": list(mandate["iteration_contract"].get("constraints", [])),
                 "forbidden_changes": list(mandate["iteration_contract"].get("forbidden_changes", [])),
                 "required_evidence": list(mandate["iteration_contract"].get("required_evidence", []))},
        "PLAN": {"key_decisions": list(plan.get("decisions", [])), "roadmap_refs": list(plan.get("roadmap_refs", [])),
                 "scope_justification": plan.get("scope_justification")},
        "IMPLEMENTATION": {"summary": execution.get("summary"), "changed_files": list(changed_files),
                           "touched_symbols": list(execution.get("touched_symbols", [])),
                           "deviations_from_plan": list(execution.get("deviations", [])),
                           "repairs": [{"summary": r.get("summary"), "addresses": r.get("addresses")}
                                       for r in iteration.get("repairs", [])]},
        "EVIDENCE": {"checks": [dict(c) for c in checks], "diff_stat": diff_stat},
        "RISKS": {"adverse_items": [dict(a) for a in adverse],
                  "uncertainties": list(execution.get("uncertainties", [])),
                  "unresolved": list(execution.get("unresolved", []))},
        "REVIEW_TARGETS": list(execution.get("review_targets", [])) or list(changed_files)[:10],
        "access": {"source_of_truth": "REPOSITORY_STATE", "worktree": worktree, "base_head": base,
                   "head": head, "diff_sha256": diff_sha256, "changed_files": list(changed_files),
                   "commits": list(commits),
                   "note": "this packet is a map; verify against the real diff, files and test output"},
    }


def enforce_adverse_preservation(packet: Mapping[str, Any], adverse: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Re-inject any unfavourable item a compression step dropped, verbatim.

    Returns a new packet plus an `integrity` block. Compression may shorten
    prose; it may not change the set of bad news.
    """
    out = json.loads(json.dumps(packet, default=str))
    risks = out.setdefault("RISKS", {})
    present = {a.get("item_key") for a in risks.get("adverse_items", []) if isinstance(a, dict)}
    missing = [dict(a) for a in adverse if a["item_key"] not in present]
    risks["adverse_items"] = list(risks.get("adverse_items", [])) + missing
    out["authoritative"] = False
    out["integrity"] = {"adverse_total": len(adverse), "adverse_in_draft": len(adverse) - len(missing),
                        "reinjected": [a["item_key"] for a in missing]}
    return out


# ── role configuration (kept apart from the state machine) ──────────────────

ROLES = ("planner", "implementer", "review_prep", "reviewer", "final_reviewer")
# V0.2 roles that V0.1 executed under the implementer binding. A config may
# bind them to their own profile; when it does not, the alias below is the
# *declared contract default* (recorded as binding_source), never a runtime
# substitution for an unavailable model.
OPTIONAL_ROLE_ALIASES = {"self_verifier": "implementer", "repairer": "implementer"}
ROLE_BY_EXECUTOR = {"plan": "planner", "execute": "implementer", "self_verify": "self_verifier",
                    "review": "reviewer", "repair": "repairer", "final_review": "final_reviewer",
                    "prepare_packet": "review_prep"}


def validate_roles(config: Any, profiles: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Resolve logical roles to profile bindings; never names a model itself.

    `profiles` is IMPLEMENTER_PROFILES' catalog. Independence is a property
    the config must declare honestly: reviewer on the same runtime model as
    the implementer is accepted only with `allow_same_model_fresh_context`.
    """
    _require(isinstance(config, dict) and isinstance(config.get("roles"), dict), "roles config must contain `roles`")
    resolved: dict[str, dict[str, Any]] = {}
    for role in ROLES:
        row = config["roles"].get(role)
        _require(isinstance(row, dict) and isinstance(row.get("profile_id"), str), f"role {role} needs a profile_id")
        profile = profiles.get(row["profile_id"])
        _require(profile is not None, f"role {role}: unknown profile {row['profile_id']!r}")
        resolved[role] = _binding(role, row["profile_id"], profile, "AUTONOMY_ROLES")
    for role, alias in OPTIONAL_ROLE_ALIASES.items():
        row = config["roles"].get(role)
        if row is None:
            resolved[role] = {**resolved[alias], "role": role, "binding_source": f"CONTRACT_ALIAS:{alias}"}
            continue
        _require(isinstance(row, dict) and isinstance(row.get("profile_id"), str), f"role {role} needs a profile_id")
        profile = profiles.get(row["profile_id"])
        _require(profile is not None, f"role {role}: unknown profile {row['profile_id']!r}")
        resolved[role] = _binding(role, row["profile_id"], profile, "AUTONOMY_ROLES")
    policy_ids = config.get("policy_profiles")
    if policy_ids is not None:
        _require(isinstance(policy_ids, dict), "policy_profiles must be an object of profile IDs")
        resolved_policy: dict[str, dict[str, Any]] = {}
        for key, profile_id in policy_ids.items():
            _require(isinstance(key, str) and isinstance(profile_id, str) and profile_id.strip(),
                     f"policy profile {key!r} needs a profile_id")
            profile = profiles.get(profile_id)
            if profile is None:
                resolved_policy[key] = {"role": key, "profile_id": profile_id,
                                        "availability": "KNOWN_BUT_UNAVAILABLE",
                                        "binding_source": "AUTONOMY_ROLES.policy_profiles"}
            else:
                resolved_policy[key] = _binding(key, profile_id, profile, "AUTONOMY_ROLES.policy_profiles")
        resolved["policy_profiles"] = resolved_policy
    same = resolved["reviewer"]["runtime_model_id"] == resolved["implementer"]["runtime_model_id"]
    _require(not same or config.get("allow_same_model_fresh_context") is True,
             "reviewer must not share the implementer's runtime model unless allow_same_model_fresh_context is true")
    independence = "SAME_MODEL_FRESH_CONTEXT" if same else "DIFFERENT_MODEL"
    for role, binding in resolved.items():
        if role == "policy_profiles":
            continue
        binding["review_independence"] = independence if binding["role"] in {"reviewer", "final_reviewer"} else None
    return resolved


def _binding(role: str, profile_id: str, profile: Mapping[str, Any], source: str) -> dict[str, Any]:
    return {"role": role, "profile_id": profile_id, "harness": profile.get("harness"),
            "provider": profile.get("provider"), "runtime_model_id": profile.get("runtime_model_id"),
            "effort": profile.get("effort"), "availability": profile.get("availability"),
            "binding_source": source}


def load_roles(path: Path, profiles_path: Path) -> dict[str, dict[str, Any]]:
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    catalog = json.loads(Path(profiles_path).read_text(encoding="utf-8"))
    return validate_roles(config, {p["profile_id"]: p for p in catalog.get("profiles", [])})


def agent_identities(roles: Mapping[str, Mapping[str, Any]]) -> set[str]:
    out: set[str] = set()
    bindings: list[Mapping[str, Any]] = []
    for value in roles.values():
        if not isinstance(value, Mapping):
            continue
        if "profile_id" in value:
            bindings.append(value)
        else:
            bindings.extend(row for row in value.values()
                            if isinstance(row, Mapping) and "profile_id" in row)
    for binding in bindings:
        for key in ("role", "profile_id", "runtime_model_id"):
            if binding.get(key):
                out.add(str(binding[key]).lower())
    return out


# ── git policy ───────────────────────────────────────────────────────────────

PROTECTED_BRANCHES = ("main", "master")
_GIT_OPTS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}


class GitPolicyViolation(AutonomyError):
    classification = "GIT_POLICY_VIOLATION"


def _git_subcommand(argv: Sequence[str]) -> tuple[str | None, list[str]]:
    args = list(argv)
    if args and Path(args[0]).stem.lower() == "git":
        args = args[1:]
    i = 0
    while i < len(args):
        if args[i] in _GIT_OPTS_WITH_VALUE:
            i += 2
        elif args[i].startswith("-"):
            i += 1
        else:
            return args[i].lower(), args[i + 1:]
    return None, []


def classify_git_command(argv: Sequence[str], *, promotion_authorized: bool = False,
                         protected: Sequence[str] = PROTECTED_BRANCHES) -> tuple[bool, str]:
    """Allow/deny one git invocation. Deny is the default for anything integrating.

    Local commits on an iteration branch are fine (AAW checkpoints rely on
    them). Anything that integrates into, rewrites or publishes a protected
    branch needs the one-shot promotion authorization that only a human
    approval can mint.
    """
    sub, rest = _git_subcommand(argv)
    if sub is None:
        return False, "unparseable git command"
    words = " ".join(rest).lower()
    names_protected = any(re.search(rf"(^|[\s/:+]){re.escape(b)}($|\s)", words) for b in protected)
    if sub in {"merge", "pull", "push", "am", "cherry-pick", "rebase", "revert"} or \
            (sub == "update-ref" and names_protected) or \
            (sub == "branch" and names_protected and any(f in rest for f in ("-f", "-D", "-d", "-M", "-m", "--force"))) or \
            (sub in {"checkout", "switch"} and names_protected and "-b" not in rest and "-c" not in rest) or \
            (sub == "reset" and "--hard" in rest and names_protected) or \
            (sub == "tag" and any(f in rest for f in ("-d", "-f"))) or sub in {"fast-import", "replace", "filter-branch"}:
        if promotion_authorized and sub in {"merge", "push"}:
            return True, f"{sub} permitted by one-shot human-approved promotion authorization"
        return False, f"git {sub} integrates or publishes history and is reserved for an approved PROMOTE"
    return True, "local, non-integrating git command"


class GuardedGit:
    """Run git on the controller's behalf under `classify_git_command`.

    `runner(argv) -> (rc, out, err)` is injected (workflow_runner.run_process
    in production). An agent-facing handle is built without a token, so merge
    and push raise before anything is executed.
    """

    def __init__(self, runner: Callable[[Sequence[str]], tuple[int, str, str]], *,
                 token: "PromotionToken | None" = None, on_denied: Callable[[Sequence[str], str], None] | None = None,
                 candidate_id: str | None = None, run_id: str | None = None) -> None:
        self._runner, self._token, self._on_denied = runner, token, on_denied
        # V0.2: a handle bound to a candidate/run accepts only a token minted for exactly that.
        self._candidate_id, self._run_id = candidate_id, run_id

    def _token_matches(self) -> bool:
        token = self._token
        if not (token and token.valid()):
            return False
        if self._candidate_id is not None and token.candidate_id != self._candidate_id:
            return False
        return self._run_id is None or token.run_id == self._run_id

    def run(self, argv: Sequence[str]) -> tuple[int, str, str]:
        ok, why = classify_git_command(argv, promotion_authorized=self._token_matches())
        if not ok:
            if self._on_denied:
                self._on_denied(argv, why)
            raise GitPolicyViolation(why)
        if self._token_matches() and _git_subcommand(argv)[0] in {"merge", "push"}:
            self._token.consume()
        return self._runner(argv)


class PromotionToken:
    """One-shot authority to integrate. Only `AutonomyController.promote` mints
    one, and only from a recorded human approval of one exact candidate."""

    def __init__(self, run_id: str, approval_id: str, candidate_id: str | None = None) -> None:
        self.run_id, self.approval_id, self.candidate_id, self._uses = run_id, approval_id, candidate_id, 1

    def valid(self) -> bool:
        return self._uses > 0

    def consume(self) -> None:
        _require(self._uses > 0, "promotion token already used")
        self._uses -= 1
