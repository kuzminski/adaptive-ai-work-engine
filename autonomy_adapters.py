#!/usr/bin/env python3
"""AAW AUTONOMOUS ITERATIONS V0.3 — real role executors.

Binds the controller's six role operations to the existing DIRECT_CLI_CONTROL
provider path. Resolution is pure configuration:

    role → AUTONOMY_ROLES.json profile_id → IMPLEMENTER_PROFILES → MODEL_CATALOG
         → harness (codex | claude) → `workflow_runner.run_process(dispatch=True)`

No model name appears here. The *controller* allocates the V0.4A execution and
records EXECUTION_INTENT before calling an executor; this module only

  1. refuses (`preflight`) a profile that is not runnable now — the controller
     then stops with ROLE_PROFILE_UNAVAILABLE; nothing is substituted;
  2. dispatches one fresh provider process inside the controller's observation
     scope (so EXECUTION_STARTED is the real spawn receipt);
  3. persists the raw result artifact, records the provider session on the
     descriptor, and closes the execution with the observed exit.

Each role gets a structured handoff (frozen mandate, plan, packet, raw diff,
explicit source references) — never another role's chat history. Every call is
a new process with no session persistence (`claude --no-session-persistence`,
`codex exec --ephemeral`), and any provider session id inherited from a parent
Claude Code session is removed from the child environment.

Local models (ollama) are not bound to these roles: the catalog forbids them
for implementation and independent review.
"""

from __future__ import annotations

import copy
import json
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

import provider_adapters as pa
import work_packet as wp
import workflow_runner as wr
import autonomy_contract as ac
from autonomy_controller import ExecutorFailure, RoleUnavailable, _write_once
from execution_contract import update_execution
from autonomy_contract import (LEVEL_BY_KIND, REQUIRED_CHARTER_GATE_CONDITIONS, directional_charter_template,
                               risks_for_items)
from model_catalog import CatalogError, validate_model_effort

ADAPTER_ID = "AAW_AUTONOMY_DIRECT_CLI_V0.3"
SUPPORTED_HARNESSES = ("codex", "claude", "agy")
DEFAULT_PROVIDER_TIMEOUT_SECONDS = 3600
MIN_SIDE_EFFECT_TIMEOUT_SECONDS = 3600
# Antigravity CLI (`agy`, successor of the Gemini CLI). In print mode it does not read stdin when the
# prompt is given by flag, and a handoff with a diff exceeds the Windows command-line limit, so the
# full role prompt is written to a file in the system temp directory (readable by agy by default)
# and the `--print` prompt only points at it.
AGY_PROMPT_FILE = "aaw_role_prompt.md"
AGY_HEADLESS_PROMPT = ("Read the file {path} completely and do exactly what it says: it holds your AAW role "
                       "instructions and the full handoff. Your final answer must be the JSON object it asks for.")
# Provider-session variables a parent Claude Code session exports. Inherited by
# a child `claude --print`, they make the child report the *parent's* session
# id — fresh context would then be unprovable from evidence.
INHERITED_SESSION_ENV = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_REMOTE_SESSION_ID")
READ_ONLY_EXECUTORS = frozenset({"plan", "self_verify", "prepare_packet", "review", "final_review", "diagnose"})
MAX_PROMPT_DIFF = 120_000

_STRS = {"type": "array", "items": {"type": "string"}}
# `command`, `exit_code`, `log_ref` and `supersedes` carry the evidence a later repair or reviewer needs
# (the failing command, its exit code, where its output lives, and the stale checks a re-run replaces).
_CHECK = {"type": "object", "additionalProperties": False, "required": ["name", "status", "summary"],
          "properties": {"name": {"type": "string"},
                         "status": {"type": "string", "enum": ["PASS", "FAIL", "ERROR", "WARN", "SKIPPED"]},
                         "summary": {"type": "string"}, "command": {"type": ["string", "null"]},
                         "exit_code": {"type": ["integer", "null"]}, "log_ref": {"type": ["string", "null"]},
                         "supersedes": _STRS}}
_DIAGNOSIS = {"type": ["object", "null"], "additionalProperties": False,
              "required": ["root_cause", "why_prior_attempts_failed", "next_actions", "classification"],
              "properties": {"root_cause": {"type": "string"}, "why_prior_attempts_failed": {"type": "string"},
                             "next_actions": _STRS,
                             "classification": {"type": ["string", "null"], "enum": [
                                 None, "PRODUCT_DEFECT", "MISSING_EVIDENCE", "STALE_FINDING", "PRE_EXISTING_BASELINE",
                                 "ENVIRONMENTAL_LIMITATION", "EVIDENCE_PROPAGATION", "REVIEW_PROCESS"]}}}

# Small, closed role output contracts. Field names are the V0.1 controller
# contract; the V0.2 role vocabulary maps onto them (see the V0.2 document).
OUTPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    "plan": {"type": "object", "additionalProperties": False,
             "required": ["status", "mandate_hash", "goal", "roadmap_refs", "scope_justification",
                          "acceptance_criteria", "touched_areas", "decisions", "skipped_items", "reason"],
             "properties": {
                 "status": {"type": "string", "enum": ["ITERATION", "NO_FURTHER_ACTION", "ESCALATE"]},
                 "mandate_hash": {"type": "string"}, "goal": {"type": ["string", "null"]},
                 "roadmap_refs": _STRS, "scope_justification": {"type": ["string", "null"]},
                 "acceptance_criteria": _STRS, "touched_areas": _STRS,
                 "decisions": {"type": "array", "items": {
                     "type": "object", "additionalProperties": False, "required": ["kind", "summary"],
                     "properties": {"kind": {"type": "string"}, "summary": {"type": "string"}}}},
                 "skipped_items": {"type": "array", "items": {
                     "type": "object", "additionalProperties": False, "required": ["item_id", "reason"],
                     "properties": {"item_id": {"type": "string"}, "reason": {"type": "string"}}}},
                 "reason": {"type": ["string", "null"]},
                 "directional_charter": {"type": ["object", "null"], "additionalProperties": False,
                    "required": ["mandate_hash", "objective", "roadmap_items", "acceptance_criteria", "boundaries",
                                 "human_gate_conditions", "risk_guidance"],
                    "properties": {
                        "mandate_hash": {"type": "string"}, "objective": {"type": "string"},
                        "roadmap_items": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                            "required": ["item_id", "title", "depends_on", "human_required"],
                            "properties": {"item_id": {"type": "string"}, "title": {"type": "string"},
                                           "depends_on": _STRS, "human_required": {"type": "boolean"}}}},
                        "acceptance_criteria": _STRS,
                        "boundaries": {"type": "object", "additionalProperties": False,
                            "required": ["scope", "constraints", "forbidden_changes", "allowed_areas", "forbidden_areas"],
                            "properties": {"scope": _STRS, "constraints": _STRS, "forbidden_changes": _STRS,
                                           "allowed_areas": {"type": ["array", "null"], "items": {"type": "string"}},
                                           "forbidden_areas": _STRS}},
                        "human_gate_conditions": _STRS,
                        "risk_guidance": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                            "required": ["item_id", "implementation_floor", "final_review_floor", "reason"],
                            "properties": {"item_id": {"type": "string"},
                                "implementation_floor": {"type": "string", "enum": ["NORMAL", "HARDER", "SIGNIFICANTLY_DIFFICULT"]},
                                "final_review_floor": {"type": "string", "enum": ["DEFAULT", "HARD", "CRITICAL"]},
                                "reason": {"type": "string"}}}}}},
                 "directional_charter_hash": {"type": ["string", "null"]},
                 "implementation_complexity": {"type": "string", "enum": ["NORMAL", "HARDER", "SIGNIFICANTLY_DIFFICULT"]},
                 "complexity_evidence": _STRS,
                 "semantic_verification_required": {"type": "boolean"},
                 "semantic_verification_reason": {"type": ["string", "null"]},
                 "working_roadmap": {"type": ["string", "null"]},
                 "next_recommended_step": {"type": ["string", "null"]},
                 "chain_plan": {"type": ["array", "null"], "items": {
                     "type": "object", "additionalProperties": False,
                     "required": ["goal", "roadmap_refs", "scope_justification", "acceptance_criteria",
                                  "touched_areas", "implementation_complexity", "complexity_evidence", "work_packet"],
                     "properties": {"goal": {"type": "string"}, "roadmap_refs": _STRS,
                                    "scope_justification": {"type": "string"}, "acceptance_criteria": _STRS,
                                    "touched_areas": _STRS,
                                    "implementation_complexity": {"type": "string",
                                                                  "enum": ["NORMAL", "HARDER", "SIGNIFICANTLY_DIFFICULT"]},
                                    "complexity_evidence": _STRS,
                                    "work_packet": copy.deepcopy(wp.WORK_PACKET_SCHEMA)}}},
                 "work_packet": copy.deepcopy(wp.WORK_PACKET_SCHEMA)}},
    "execute": {"type": "object", "additionalProperties": False,
                "required": ["summary", "changed_files", "checks", "deviations", "uncertainties"],
                "properties": {"summary": {"type": "string"}, "changed_files": _STRS,
                               "checks": {"type": "array", "items": _CHECK},
                               "deviations": _STRS, "uncertainties": _STRS,
                               "self_audit": copy.deepcopy(wp.SELF_AUDIT_SCHEMA)}},
    "self_verify": {"type": "object", "additionalProperties": False, "required": ["summary", "checks"],
                    "properties": {"summary": {"type": "string"}, "checks": {"type": "array", "items": _CHECK}}},
    "review": {"type": "object", "additionalProperties": False, "required": ["verdict", "summary", "findings"],
               "properties": {
                   "verdict": {"type": "string", "enum": ["PASS", "REPAIR_REQUIRED", "ESCALATE"]},
                   "summary": {"type": "string"},
                   "findings": {"type": "array", "items": {
                       "type": "object", "additionalProperties": False,
                       "required": ["finding_key", "severity", "summary", "file", "blocking", "evidence_ref"],
                       "properties": {"finding_key": {"type": ["string", "null"]},
                                      "severity": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH", "CRITICAL"]},
                                      "summary": {"type": "string"}, "file": {"type": ["string", "null"]},
                                      "blocking": {"type": "boolean"}, "evidence_ref": {"type": ["string", "null"]},
                                      "finding_code": {"type": ["string", "null"], "enum": [None, "IMPLEMENTATION_CAPABILITY_MISMATCH"]}}}},
                   "raw_evidence_requests": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                       "required": ["source_ref", "reason"],
                       "properties": {"source_ref": {"type": "string"}, "reason": {"type": "string"}}}},
                   "uncertainties": _STRS}},
    "prepare_packet": {"type": "object", "additionalProperties": False,
                       "required": ["summary", "implementation_claims", "check_refs", "finding_refs", "changed_files", "source_refs"],
                       "properties": {"summary": {"type": "string"}, "implementation_claims": _STRS,
                                      "check_refs": _STRS, "finding_refs": _STRS, "changed_files": _STRS,
                                      "source_refs": _STRS}},
    "repair": {"type": "object", "additionalProperties": False,
               "required": ["summary", "addressed_findings", "changed_files", "checks", "uncertainties"],
               "properties": {"summary": {"type": "string"}, "addressed_findings": _STRS, "changed_files": _STRS,
                              "checks": {"type": "array", "items": _CHECK}, "uncertainties": _STRS,
                              "self_audit": copy.deepcopy(wp.SELF_AUDIT_SCHEMA),
                              "diagnosis": _DIAGNOSIS, "evidence_refs": _STRS,
                              "reclassifications": {"type": "array", "items": {
                                  "type": "object", "additionalProperties": False,
                                  "required": ["check_name", "classification", "evidence_ref", "explanation",
                                               "acceptance_impact", "superseded_by"],
                                  "properties": {"check_name": {"type": "string"},
                                                 "classification": {"type": "string", "enum": [
                                                     "PRE_EXISTING_BASELINE", "ENVIRONMENTAL_LIMITATION",
                                                     "SUPERSEDED_BY_NEWER_CHECK"]},
                                                 "evidence_ref": {"type": "string"}, "explanation": {"type": "string"},
                                                 "acceptance_impact": {"type": "string", "enum": ["NONE", "AFFECTED"]},
                                                 "superseded_by": {"type": ["string", "null"]}}}},
                              "process_fixes": {"type": "array", "items": {
                                  "type": "object", "additionalProperties": False,
                                  "required": ["kind", "description", "evidence_ref"],
                                  "properties": {"kind": {"type": "string"}, "description": {"type": "string"},
                                                 "evidence_ref": {"type": "string"}}}}}},
    "diagnose": {"type": "object", "additionalProperties": False, "required": ["summary", "diagnosis"],
                 "properties": {"summary": {"type": "string"}, "diagnosis": _DIAGNOSIS}},
}
OUTPUT_SCHEMAS["final_review"] = OUTPUT_SCHEMAS["review"]
# The keys a role result really needs (before Codex's "every key is required" rewrite below).
# A provider without native schema enforcement (Gemini CLI) is validated against these.
CORE_REQUIRED: dict[str, list[str]] = {name: list(schema.get("required", [])) for name, schema in OUTPUT_SCHEMAS.items()}


def _require_all_schema_properties(node: Any) -> None:
    """Codex structured outputs require every declared object key in required."""
    if isinstance(node, dict):
        properties = node.get("properties")
        if isinstance(properties, dict):
            required = list(node.get("required") or [])
            required.extend(key for key in properties if key not in required)
            node["required"] = required
        for value in node.values():
            _require_all_schema_properties(value)
    elif isinstance(node, list):
        for value in node:
            _require_all_schema_properties(value)


for _schema in OUTPUT_SCHEMAS.values():
    _require_all_schema_properties(_schema)

ROLE_INSTRUCTIONS: dict[str, str] = {
    "plan": ("You are the AAW PLANNER. On the first invocation, act as INITIAL_ARCHITECT: return a directional_charter "
             "that exactly preserves the frozen MANDATE objective, roadmap item IDs/titles/dependencies, acceptance "
             "criteria, boundaries, and all required Human Gate conditions: copy every field of "
             "DIRECTIONAL_CHARTER_TEMPLATE verbatim; human_gate_conditions must contain every code in "
             "REQUIRED_HUMAN_GATE_CONDITIONS exactly as written (you may append others). DIRECTIONAL_CHARTER_TEMPLATE.risk_guidance, "
             "when present, holds the floors set by the human-confirmed MANDATE.roadmap_mandate.risk_register: keep every "
             "such row (you may raise a floor or extend its reason, never lower or drop it). Add further risk_guidance "
             "rows only where a roadmap item warrants a higher implementation or final-review floor, with an "
             "evidence-based reason; with no template rows, use an empty array when no item warrants escalation. "
             "Do not add roadmap work. On later invocations, "
             "omit directional_charter and echo its directional_charter_hash; select only the next bounded iteration "
             "from that frozen charter. Decide one iteration, no further justified action, or ESCALATE. Echo "
             "MANDATE.mandate_hash exactly. Iteration 1 carries all human acceptance criteria verbatim. roadmap_refs "
             "must be pending items with dependencies met. For every MANDATE.roadmap_mandate.risk_register entry that "
             "applies to the selected roadmap_refs (an entry without item_ids applies to all), name the risk_id in "
             "work_packet pitfalls together with how the step avoids it, and add a verification step when one can show "
             "it. Set implementation_complexity to NORMAL, HARDER, or "
             "SIGNIFICANTLY_DIFFICULT and cite concrete complexity_evidence. Never use that label alone to request Sonnet. "
             "Every decisions[].kind must be a key of DECISION_KINDS (its value is the autonomy level); a kind whose "
             "level is ESCALATE, or any kind not listed, stops autonomy for a human, so record ordinary technical "
             "choices with AUTO / AUTO_WITHIN_SCOPE kinds. skipped_items PERMANENTLY removes a roadmap item from this "
             "run: list an item there only with a reason why it should never be done autonomously; never list an item "
             "merely because it is waiting for its dependencies (it stays pending for a later iteration). "
             "A roadmap item marked recurring is a standing item: it is not completed by an accepted iteration. "
             "While user direction items remain pending, prefer them; once only the recurring item remains, "
             "inspect the repository and choose the next most valuable bounded step toward MANDATE.roadmap_mandate.objective "
             "(missing functionality, integration, tests, UX, documentation) and reference the recurring item in "
             "roadmap_refs. Only when no sensible further work remains, return NO_FURTHER_ACTION and list the recurring "
             "item in skipped_items with the concrete reason. On every ITERATION plan, set working_roadmap to the "
             "updated working roadmap (Markdown: done, open problems, decisions, next steps) and next_recommended_step "
             "to the single next step; both are advisory notes for the operator and never change the user's direction "
             "or grant scope. "
             + wp.PLANNER_RULES + " Do not modify any file."),
    "execute": ("You are the AAW IMPLEMENTER. Implement exactly PLAN inside WORKTREE_PATH. Respect CONSTRAINTS and "
                "FORBIDDEN_CHANGES. Do not merge, push, rebase, switch branches, or touch any other checkout; leave "
                "your changes uncommitted. Do not run Git commands: the controller owns and verifies Git boundaries. "
                "Run the acceptance checks you can and report each honestly (FAIL is a valid "
                "status). Name each check in English using the exact wording of the REQUIRED_EVIDENCE item it "
                "substantiates (for example a check named 'unit tests'); the controller matches evidence by that "
                "name. For every test or command check, the summary must state the exact command, its exit code and "
                "the reported result counts, so a reviewer can verify it from this record alone. "
                "Do not delete files such as __pycache__. "
                "Report deviations from the plan and known limitations (uncertainties). RISK_FOCUS, when present, lists "
                "human-confirmed risks for this step: avoid each one and report any you could not rule out as an "
                "uncertainty." + wp.IMPLEMENTER_RULES),
    "self_verify": ("You are the AAW SELF-VERIFIER. Verify the current worktree against PLAN.acceptance_criteria and "
                    "REQUIRED_EVIDENCE only where semantic verification is needed; run no mechanical checks that are "
                    "already recorded. Do not run Git commands; the controller verifies Git boundaries. "
                    "You are READ-ONLY: do not create, edit or "
                    "delete files (the controller compares the diff before and after you). Report each check."),
    "prepare_packet": ("You are the AAW REVIEW-PRETREATMENT, not a reviewer. Condense and organize only the supplied evidence "
                       "into the requested summary fields. Preserve every failure, warning, blocking finding, and source "
                       "reference. Do not issue PASS/FAIL, assess correctness, hide evidence, or infer conclusions."),
    "review": ("You are an independent AAW REVIEWER with a fresh context. The compact PACKET and "
               "RAW_EVIDENCE_MANIFEST are supplied first; raw evidence is not included by default. If an evident "
               "ambiguity changes interpretation, request specific source_ref values with a concise reason in "
               "raw_evidence_requests and return ESCALATE pending the controller's targeted retrieval. List any "
               "remaining substantive uncertainty in uncertainties. Do not run Git commands; review the supplied "
               "diff/source evidence and use targeted retrieval when needed. RISK_CHECKS, when present, lists the "
               "human-confirmed risks for this iteration's roadmap items: check each against the diff and evidence, "
               "raise a finding only when the change realizes the risk or skips its stated mitigation, and name every "
               "risk_id you checked in summary. Otherwise "
               "return PASS, REPAIR_REQUIRED, or ESCALATE with evidence references. Do not modify any file."),
    "repair": ("You are the AAW REPAIRER. Address only FINDINGS, inside EXACT_ALLOWED_REPAIR_SCOPE. Do not expand "
               "the goal; do not merge, push, rebase or switch branches; leave changes uncommitted. Report which "
               "finding keys you addressed and the checks you ran. Re-run WORK_PACKET.verification_commands that "
               "your change can affect, and end with the FINAL SELF-AUDIT over AUDIT_CHECKLIST (report "
               "self_audit; an honest FAIL is better than a false PASS)."),
}
ROLE_INSTRUCTIONS["repair"] += (
    " If REPAIR_PACKET is present, start from it and do not re-read the whole repository; if MODE is "
    "DIAGNOSE_THEN_REPAIR, first state the root cause in `diagnosis` (and why earlier attempts failed) and do "
    "not repeat an earlier approach. A repair may need no product change: re-run the failing check and report "
    "its command, exit code and log_ref; when a re-run replaces a stale check, list that check in `supersedes`; "
    "a failure that is pre-existing or environmental goes in `reclassifications` ONLY with an evidence_ref and "
    "acceptance_impact NONE. Never invent a diff to look like progress.")
ROLE_INSTRUCTIONS["diagnose"] = (
    "You are the AAW DIAGNOSTICIAN. You are READ-ONLY: do not create, edit or delete files. Starting from "
    "REPAIR_PACKET, find the root cause of the surviving FINDINGS and explain why the earlier attempts did not "
    "resolve them. Say whether the cause is a product defect, missing evidence, a stale finding, a pre-existing "
    "baseline failure, an environmental limitation, or a defect in evidence propagation / the review process. "
    "Propose concrete next_actions. Do not repeat a previous diagnosis; do not edit anything.")
ROLE_INSTRUCTIONS["final_review"] = ROLE_INSTRUCTIONS["review"].replace("REVIEWER", "FINAL REVIEWER")

# Chain mode (autonomy_chain). Added to the role instruction only when the handoff has CHAIN / CHAIN_PLANNING.
CHAIN_INSTRUCTIONS: dict[str, str] = {
    "plan": (" CHAIN PLANNING: CHAIN_PLANNING.slots_after_this is how many further iterations may run back to back "
             "before one serious review. After the iteration you return now, outline those iterations in `chain_plan` "
             "(in execution order, at most slots_after_this entries, each with goal, roadmap_refs, scope_justification, "
             "acceptance_criteria, touched_areas, implementation_complexity, complexity_evidence, work_packet). Each entry must be a "
             "self-contained, bounded step that can be implemented and tested without re-planning, reference only "
             "pending roadmap items whose dependencies are met by this iteration or earlier entries (one item per "
             "iteration; an item is completed by its iteration), and stay inside the mandate. Return null or [] when "
             "fewer steps are justified. Entries are re-validated before use; an invalid one is dropped. Give an entry "
             "a work_packet (same rules as the iteration's own) only for paths that already exist or that an earlier "
             "entry creates; otherwise null (that iteration then runs from its acceptance criteria alone)."),
    "review_light": (" CHAIN MODE, LIGHT REVIEW (CHAIN.mode LIGHT): this is a quick mid-chain check, not the serious "
                     "review. Look only for defects that harm real behaviour: crashes, wrong results, data loss, "
                     "security holes, a failed or missing required check, an acceptance criterion not met. Report "
                     "anything smaller (style, naming, minor edge cases, docs, performance nits) as severity LOW or "
                     "MEDIUM with blocking=false in a single short line; those are polished at the end. Use severity "
                     "CRITICAL with blocking=true ONLY when the work cannot continue on top of this change (build "
                     "broken, feature entirely non-functional); use HIGH for serious defects, which the chain-closing "
                     "review will repair. Prefer PASS."),
    "review_close": (" CHAIN MODE, SERIOUS REVIEW (CHAIN.mode CHAIN_CLOSE or POLISH): review the whole diff of "
                     "CHAIN.iterations as one change: does it do what each iteration's acceptance_criteria say, and do "
                     "the iterations fit together? Re-verify the HIGH items in CHAIN.deferred_findings and report any "
                     "that are real. Findings that harm real behaviour (wrong results, crashes, data loss, security, "
                     "failing checks, unmet criteria) are severity HIGH or CRITICAL with blocking=true and will be "
                     "repaired. Everything smaller is severity LOW or MEDIUM with blocking=false: it is recorded and "
                     "polished later and must not hold the chain back. Do not block on taste. PASS means the "
                     "chain is good enough to ship as it is."),
}


def chain_instruction(name: str, handoff: Mapping[str, Any]) -> str:
    chain_block = handoff.get("CHAIN")
    if name == "plan":
        view = handoff.get("CHAIN_PLANNING")
        return CHAIN_INSTRUCTIONS["plan"] if isinstance(view, Mapping) and view.get("slots_after_this") else ""
    if name in ("review", "final_review") and isinstance(chain_block, Mapping):
        return CHAIN_INSTRUCTIONS["review_light" if chain_block.get("mode") == "LIGHT" else "review_close"]
    return ""


# ── role → runtime resolution ───────────────────────────────────────────────

def resolve_runtime(binding: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Return (runtime, None) when the configured profile is runnable now, else (None, reason)."""
    try:
        profiles = wr.load_implementer_profiles()
    except wr.WorkflowStop as exc:
        return None, str(exc)
    profile_id = str(binding.get("profile_id") or "")
    profile = profiles.get(profile_id)
    if profile is None:
        return None, f"unknown profile {profile_id!r}"
    harness = str(profile.get("harness") or "")
    adapter = pa.PROVIDER_ADAPTERS.get(harness)
    if harness not in SUPPORTED_HARNESSES and adapter is None:
        return None, f"harness {harness!r} is not a direct CLI harness for autonomy roles"
    status, reason = wr.profile_availability(profile)
    if status != "VERIFIED":
        return None, reason or "profile unavailable"
    if adapter is not None and harness not in SUPPORTED_HARNESSES:
        adapter_reason = adapter.preflight(profile)
        if adapter_reason:
            return None, adapter_reason
    executable = wr.harness_executable(harness)
    try:
        model = validate_model_effort(str(profile["runtime_model_id"]), str(profile["effort"]))
    except CatalogError as exc:
        return None, str(exc)
    return {"profile_id": profile_id, "harness": harness, "executable": executable,
            "model": str(profile["runtime_model_id"]), "effort": str(profile["effort"]),
            "provider": model.get("provider")}, None


def preflight_roles(roles: Mapping[str, Mapping[str, Any]], executors: Sequence[str] | None = None) -> dict[str, Any]:
    """Availability of every role the run will use. Diagnostic; the controller re-checks per call."""
    from autonomy_contract import ROLE_BY_EXECUTOR
    report = {}
    for name in executors or ("plan", "execute", "self_verify", "review", "repair", "final_review"):
        role = ROLE_BY_EXECUTOR[name]
        runtime, reason = resolve_runtime(roles[role])
        report[role] = {"profile_id": roles[role].get("profile_id"), "available": runtime is not None,
                        "reason": reason, "harness": (runtime or {}).get("harness"),
                        "model": (runtime or {}).get("model"), "effort": (runtime or {}).get("effort")}
    return {"all_available": all(r["available"] for r in report.values()), "roles": report}


# ── handoffs (structured, never a chat history) ─────────────────────────────

def _bounded(text: str | None, limit: int = MAX_PROMPT_DIFF) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + f"\n...[truncated {len(text) - limit} chars; see diff_path]"


def build_handoff(name: str, ctx: Mapping[str, Any]) -> dict[str, Any]:
    mandate = ctx["mandate"]
    contract = mandate["iteration_contract"]
    execution = ctx["execution"]
    worktree = (ctx["env"].describe() or {}).get("worktree")
    common = {"ROLE": ctx["role"], "AAW_RUN_ID": execution["run_id"], "ITERATION_ID": execution["iteration_id"],
              "EXECUTION_ID": execution["execution_id"], "WORKTREE_PATH": worktree,
              "SOURCE_REFERENCES": {"execution_descriptor": str(execution["descriptor_path"]),
                                    "mandate_hash": mandate["mandate_hash"]}}
    if name == "plan":
        initial = ctx.get("planning_stage") == "INITIAL_ARCHITECT"
        architect = {"DIRECTIONAL_CHARTER_TEMPLATE": directional_charter_template(mandate),
                     "REQUIRED_HUMAN_GATE_CONDITIONS": list(REQUIRED_CHARTER_GATE_CONDITIONS)} if initial else {}
        return {**common, **architect, "DECISION_KINDS": dict(LEVEL_BY_KIND),
                "REQUIRED_EVIDENCE": contract.get("required_evidence", []),
                "MANDATE": mandate, "ITERATION_INDEX": ctx["iteration_index"],
                "PLANNING_STAGE": ctx.get("planning_stage"),
                "FROZEN_DIRECTIONAL_CHARTER": ctx.get("directional_charter"),
                "FROZEN_DIRECTIONAL_CHARTER_HASH": ctx.get("directional_charter_hash"),
                "ROADMAP_STATUS": ctx["roadmap"], "HISTORY": ctx["history"],
                "WORKING_ROADMAP": ctx.get("working_roadmap"),
                **({"CHAIN_PLANNING": ctx["chain"]} if ctx.get("chain") else {}),
                "ITERATION_CONTRACT": ctx.get("iteration_contract"), "WORKSPACE": ctx.get("workspace")}
    if name in ("execute", "repair", "self_verify", "diagnose"):
        it = ctx["iteration"]
        out = {**common, "PLAN": ctx["plan"], "CONSTRAINTS": contract.get("constraints", []),
               "FORBIDDEN_CHANGES": contract.get("forbidden_changes", []),
               "REQUIRED_EVIDENCE": contract.get("required_evidence", []),
               "FORBIDDEN_AREAS": mandate["roadmap_mandate"]["autonomy_bounds"].get("forbidden_areas", []),
               "WORK_PACKET": (ctx.get("plan") or {}).get("work_packet")}
        risks = risks_for_items(mandate, (ctx.get("plan") or {}).get("roadmap_refs") or [])
        if risks:
            out["RISK_FOCUS"] = risks
        if name in ("execute", "repair"):
            out["AUDIT_CHECKLIST"] = [dict(row) for row in wp.AUDIT_CHECKLIST]
        if name == "self_verify":
            out.update({"CHANGED_FILES": ctx.get("changed_files", []), "DIFF": _bounded(ctx.get("diff")),
                        "IMPLEMENTATION_RESULT": it.get("execution")})
        if name in ("repair", "diagnose"):
            out.update({"FINDINGS": ctx["findings"], "ATTEMPT": ctx["attempt"],
                        "EXACT_ALLOWED_REPAIR_SCOPE": {
                            "findings": [f.get("finding_key") for f in ctx["findings"]],
                            "rule": "Only changes directly required by FINDINGS; no new goal, no new feature."}})
            packet = ctx.get("repair_packet")
            if packet:
                # Later attempts get the compact packet instead of the full iteration context.
                out = {key: out[key] for key in ("ROLE", "AAW_RUN_ID", "ITERATION_ID", "EXECUTION_ID", "WORKTREE_PATH",
                                                 "SOURCE_REFERENCES", "CONSTRAINTS", "FORBIDDEN_CHANGES",
                                                 "REQUIRED_EVIDENCE", "FORBIDDEN_AREAS", "FINDINGS", "ATTEMPT",
                                                 "EXACT_ALLOWED_REPAIR_SCOPE", "WORK_PACKET", "AUDIT_CHECKLIST",
                                                 "RISK_FOCUS")
                       if key in out}
                out.update({"PLAN": {"goal": ctx["plan"].get("goal")}, "REPAIR_PACKET": packet,
                            "MODE": ctx.get("repair_mode"), "STEP": ctx.get("step")})
        return out
    if name == "prepare_packet":
        return {**common, "REVIEW_KIND": "PRETREATMENT", "PACKET": ctx["packet"],
                "ROLE_RULE": "organize evidence only; do not assess correctness or emit a verdict"}
    # Review starts with the compact packet and source manifest. The controller
    # adds only specifically requested, hash-verified raw sources on a second call.
    raw = dict(ctx["raw"])
    risks = risks_for_items(mandate, ctx["iteration"]["plan"].get("roadmap_refs") or [])
    if risks:
        common["RISK_CHECKS"] = risks
    return {**common, "REVIEW_KIND": ctx["review_kind"], "FROZEN_MANDATE": mandate,
            "FROZEN_DIRECTIONAL_CHARTER": ctx.get("directional_charter"),
            "FROZEN_DIRECTIONAL_CHARTER_HASH": ctx.get("directional_charter_hash"),
            "PLAN": ctx["iteration"]["plan"], "PACKET": ctx["packet"],
            "RAW_METADATA": {key: raw.get(key) for key in
                             ("diff_sha256", "diff_file_sha256", "diff_digest_note", "diff_path",
                              "changed_files", "head", "base_head", "commits")},
            "RAW_EVIDENCE_MANIFEST": raw.get("manifest", []),
            "PREVIOUS_UNRESOLVED_FINDINGS": raw.get("previous_findings", []),
            **({"CHAIN": ctx["chain"]} if ctx.get("chain") else {}),
            "RAW_EVIDENCE": ctx.get("raw_evidence_results", [])}


# ── the executor ────────────────────────────────────────────────────────────

def _argv(runtime: Mapping[str, Any], name: str, worktree: str, schema_path: Path, final_path: Path,
          schema: Mapping[str, Any], max_turns: int, prompt_path: Path | None = None) -> tuple[list[str], bool]:
    """Same direct-CLI invocation shape as `workflow_runner.execute_llm_node`.

    Returns (argv, prompt_on_stdin). The prompt always goes on stdin: a review
    handoff with a diff exceeds the Windows command-line limit as an argument.
    """
    read_only = name in READ_ONLY_EXECUTORS
    if runtime["harness"] == "codex":
        sandbox = "read-only" if read_only else "workspace-write"
        return [str(runtime["executable"]), "exec", "--ephemeral", "--skip-git-repo-check", "--sandbox", sandbox,
                "--model", runtime["model"], "--config", f'model_reasoning_effort="{runtime["effort"]}"',
                "--cd", worktree, "--output-schema", str(schema_path), "--output-last-message", str(final_path),
                "--json", "-"], True
    if runtime["harness"] == "agy":
        # Headless Antigravity CLI. Read-only roles keep the default permission mode: workspace reads are
        # granted, edits and commands need an approval nobody can give in print mode, so they are denied.
        # Write roles need a shell for their checks and auto-approve tools; the controller's Git boundary
        # check after the phase stays the authority. `--json-schema` enforces the role's output contract
        # (`structured_output`). Every option precedes `--print`: agy reads all trailing arguments as prompt.
        argv = [str(runtime["executable"]), "--model", runtime["model"], "--output-format", "json",
                "--json-schema", str(schema_path)]
        if not read_only:
            argv.append("--dangerously-skip-permissions")
        return argv + ["--print", AGY_HEADLESS_PROMPT.format(path=prompt_path)], False
    argv = [str(runtime["executable"]), "--print", "--no-session-persistence",
            "--permission-mode", "plan" if read_only else "acceptEdits",
            "--model", runtime["model"], "--effort", runtime["effort"], "--output-format", "json",
            "--json-schema", json.dumps(schema, separators=(",", ":")), "--max-turns", str(max_turns)]
    if not read_only:
        # Checks need a shell; integration commands stay denied (the Git
        # boundary check after the phase is the authority, this is defence).
        argv += ["--allowedTools", "Bash", "Read", "Edit", "Write", "Glob", "Grep",
                 "--disallowedTools", "Bash(git push:*)", "Bash(git merge:*)", "Bash(git rebase:*)",
                 "Bash(git pull:*)", "Bash(git checkout:*)", "Bash(git switch:*)", "Bash(git reset:*)"]
    return argv, True


_RATE_PATTERNS = ("rate limit", "rate_limit", "ratelimit", "usage limit", "quota", "too many requests",
                  "resource_exhausted", "limit reached", "overloaded")
_AUTH_PATTERNS = ("not signed in", "sign in to", "/login", "not logged in", "unauthorized", "authentication", "invalid api key",
                  "login required", "please log in", "token expired")


def classify_failure(rc: int | None, stdout: str | None, stderr: str | None) -> tuple[str | None, float | None]:
    """Map a provider process failure to a model_router failure class (hard signal, not an estimate)."""
    import re
    text = f"{stdout or ''}\n{stderr or ''}".lower()
    if rc == 124 or "process_timeout" in text:
        return "TIMEOUT", None
    import re as _re
    if any(p in text for p in _RATE_PATTERNS) or _re.search(r"\b429\b", text):
        match = re.search(r"retry[- _]after[^0-9]{0,12}(\d+(?:\.\d+)?)\s*(s|sec|seconds|m|min|minutes)?", text)
        minutes = None
        if match:
            value = float(match.group(1))
            minutes = value / 60.0 if (match.group(2) or "s").startswith("s") else value
        return "RATE_LIMIT", minutes
    if any(p in text for p in _AUTH_PATTERNS) or _re.search(r"\b40[13]\b", text):
        return "AUTH", None
    return None, None


class DirectRoleExecutor:
    """One role operation bound to the direct CLI provider path."""

    fixture_class = "REAL_PROVIDER_DIRECT_CLI"

    def __init__(self, name: str, *, timeout: int = DEFAULT_PROVIDER_TIMEOUT_SECONDS,
                 max_turns: int = 30) -> None:
        self.name, self.timeout, self.max_turns = name, timeout, max_turns

    def preflight(self, binding: Mapping[str, Any]) -> str | None:
        return resolve_runtime(binding)[1]

    def __call__(self, ctx: dict[str, Any]) -> dict[str, Any] | None:
        execution = ctx["execution"]
        recorder, descriptor_path = execution["recorder"], Path(execution["descriptor_path"])
        runtime, reason = resolve_runtime(ctx["binding"])
        if runtime is None:  # availability changed since preflight: still nothing dispatched
            raise RoleUnavailable(reason or "profile unavailable")
        worktree = str((ctx["env"].describe() or {}).get("worktree") or ".")
        schema = OUTPUT_SCHEMAS[self.name]
        handoff = build_handoff(self.name, ctx)
        adapter = pa.PROVIDER_ADAPTERS.get(runtime["harness"])
        if adapter is not None and runtime["harness"] not in SUPPORTED_HARNESSES:
            # A registered non-built-in provider (e.g. Antigravity) owns its own dispatch.
            return adapter.invoke(ctx, runtime, handoff)
        prompt = (ROLE_INSTRUCTIONS[self.name] + chain_instruction(self.name, handoff)
                  + "\nFinish with exactly the JSON object required by the output "
                  "schema.\n\nHANDOFF:\n" + json.dumps(handoff, indent=2, ensure_ascii=False, default=str))
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="aaw_autonomy_role_") as temp:
            schema_path, final_path = Path(temp) / "schema.json", Path(temp) / "final.json"
            schema_path.write_text(json.dumps(schema), encoding="utf-8")
            prompt_path = Path(temp) / AGY_PROMPT_FILE
            if runtime["harness"] == "agy":
                prompt_path.write_text(prompt, encoding="utf-8")
            argv, on_stdin = _argv(runtime, self.name, worktree, schema_path, final_path, schema, self.max_turns,
                                   prompt_path=prompt_path)
            try:
                rc, stdout, stderr = wr.run_process(argv, cwd=Path(worktree), stdin=prompt if on_stdin else None,
                                                    timeout=self.timeout, provider=runtime["provider"],
                                                    adapter=ADAPTER_ID, dispatch=True,
                                                    env_remove=INHERITED_SESSION_ENV)
            except wr.WorkflowStop as exc:
                recorder.close(close_reason="FAILED", effect_certainty="CONFIRMED", outcome="BLOCKED",
                               observation_source="RUNNER_EXCEPTION" if recorder.started else "SPAWN_FAILURE",
                               detail=str(exc))
                raise ExecutorFailure(f"{self.name}: dispatch failed: {exc}", dispatched=recorder.started,
                                      failure_class="UNAVAILABLE" if not recorder.started else None) from exc
            session, usage, raw, provider_meta = self._parse(runtime["harness"], rc, stdout, final_path)
            if runtime["harness"] == "agy" and raw is not None and not core_shape_ok(self.name, raw):
                provider_meta["schema_rejected"] = True
                raw = None
        elapsed = round(time.monotonic() - started, 3)
        valid = isinstance(raw, dict)
        timeout_result = rc == 124 and valid
        update_execution(descriptor_path, execution["execution_id"],
                         provider_session_id=str(session) if session else None,
                         status="COMPLETED" if rc == 0 or timeout_result else "FAILED")
        result_path = Path(execution["result_path"])
        _write_once(result_path, {
            "execution_id": execution["execution_id"], "role": ctx["role"], "executor": self.name,
            "recorded_by": ADAPTER_ID, "result": raw, "exit_code": rc, "provider_session_id": session,
            "harness": runtime["harness"], "model": runtime["model"], "effort": runtime["effort"],
            "profile_id": runtime["profile_id"], "wall_time_s": elapsed, "timeout_s": self.timeout,
            "result_recovered_after_timeout": timeout_result,
            "usage": usage, "provider_meta": provider_meta,
            "stderr_tail": (stderr or "")[-4000:], "stdout_tail": (stdout or "")[-4000:] if raw is None else None})
        close = wr._close_from_returncode(rc, provider_session_id=session)
        valid = (rc == 0 or rc == 124) and isinstance(raw, dict)
        if rc == 0 and not valid:
            close["effect_certainty"] = "PARTIAL"  # process exited cleanly; only the structured result is untrusted
        recorder.close(outcome="RESULT_RECEIVED" if valid else ("INVALID" if rc == 0 else "BLOCKED"),
                       result_refs=[str(result_path)], detail=None if valid else (stderr or stdout)[-2000:], **close)
        if rc != 0 and not timeout_result:
            retryable = rc == 124 and self.name in READ_ONLY_EXECUTORS and ctx.get("role") != "initial_planner"
            code = ac.E_EXECUTOR_TIMEOUT if rc == 124 else None
            failure_class, retry_after = classify_failure(rc, stdout, stderr)
            raise ExecutorFailure(f"{self.name}: provider process exited rc={rc}", code=code,
                                  dispatched=True, retryable=retryable,
                                  failure_class=failure_class, retry_after_minutes=retry_after)
        if not valid and self.name not in ("review", "final_review"):
            raise ExecutorFailure(f"{self.name}: provider returned no structured result", dispatched=True)
        # An invalid reviewer result is handed back as-is: `normalize_review`
        # turns it into ESCALATE (REVIEW_RESULT_INVALID), never a silent PASS.
        return raw

    @staticmethod
    def _parse(harness: str, rc: int, stdout: str, final_path: Path) -> tuple[Any, dict, Any, dict]:
        if harness == "agy":
            # agy --output-format json: {conversation_id, status, response, usage, structured_output}
            envelope = _json_object(stdout) or {}
            raw = envelope.get("structured_output") if isinstance(envelope.get("structured_output"), dict) else None
            text = envelope.get("response") if isinstance(envelope.get("response"), str) else None
            if raw is None and rc == 0 and text:
                raw = _json_object(text)
            if rc != 0:
                raw = None
            meta = {"status": envelope.get("status"), "error": envelope.get("error")}
            usage = envelope.get("usage") if isinstance(envelope.get("usage"), dict) else {}
            return envelope.get("conversation_id"), dict(usage), raw, meta
        if harness == "codex":
            _, session, usage = wr.parse_codex_events(stdout)
            raw = None
            if final_path.is_file():
                try:
                    raw = json.loads(final_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    raw = None
            return session, usage, raw, {}
        try:
            envelope = json.loads(stdout) if stdout.strip() else {}
        except json.JSONDecodeError:
            envelope = {}
        raw = envelope.get("structured_output")
        if raw is None and isinstance(envelope.get("result"), str):
            text = envelope["result"].strip()
            if text.startswith("```"):
                text = text.strip("`").split("\n", 1)[-1]
            try:
                raw = json.loads(text) if text.startswith("{") else None
            except json.JSONDecodeError:
                raw = None
        meta = {k: envelope.get(k) for k in ("total_cost_usd", "num_turns", "terminal_reason", "is_error",
                                             "permission_denials")}
        return envelope.get("session_id"), dict(envelope.get("usage") or {}), raw, meta


def _json_object(text: str | None) -> dict[str, Any] | None:
    """The JSON object in a model's text answer: whole text, a fenced block, or the outermost braces."""
    if not isinstance(text, str) or not text.strip():
        return None
    candidates = [text.strip()]
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced:
        candidates.append(fenced.group(1))
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start:end + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def core_shape_ok(name: str, raw: Any) -> bool:
    """Minimal structural check for providers without native schema enforcement."""
    return isinstance(raw, dict) and all(key in raw for key in CORE_REQUIRED.get(name, []))


def build_direct_executors(*, timeout: int = DEFAULT_PROVIDER_TIMEOUT_SECONDS, max_turns: int = 30,
                           review_pretreatment: bool = False) -> dict[str, DirectRoleExecutor]:
    """The production executor set for `AutonomyController`.

    Review pretreatment is OFF by default: it is a full model call per review
    round that may only re-index evidence the deterministic packet already
    carries (it can never change a verdict), so it cost time and quota on every
    REVIEW without changing outcomes. Pass `review_pretreatment=True` to keep it.
    Side-effecting roles (execute, repair) never get less than
    MIN_SIDE_EFFECT_TIMEOUT_SECONDS: a timeout there abandons real work.
    """
    names = ["plan", "execute", "self_verify", "review", "repair", "final_review", "diagnose"]
    if review_pretreatment:
        names.insert(3, "prepare_packet")
    return {name: DirectRoleExecutor(
                name,
                timeout=max(int(timeout), MIN_SIDE_EFFECT_TIMEOUT_SECONDS)
                if name in ("execute", "repair") else int(timeout),
                max_turns=max_turns)
            for name in names}
