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

import json
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

import workflow_runner as wr
import autonomy_contract as ac
from autonomy_controller import ExecutorFailure, RoleUnavailable, _write_once
from execution_contract import update_execution
from autonomy_contract import LEVEL_BY_KIND, REQUIRED_CHARTER_GATE_CONDITIONS, directional_charter_template
from model_catalog import CatalogError, validate_model_effort

ADAPTER_ID = "AAW_AUTONOMY_DIRECT_CLI_V0.3"
SUPPORTED_HARNESSES = ("codex", "claude")
DEFAULT_PROVIDER_TIMEOUT_SECONDS = 3600
MIN_SIDE_EFFECT_TIMEOUT_SECONDS = 3600
# Provider-session variables a parent Claude Code session exports. Inherited by
# a child `claude --print`, they make the child report the *parent's* session
# id — fresh context would then be unprovable from evidence.
INHERITED_SESSION_ENV = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_REMOTE_SESSION_ID")
READ_ONLY_EXECUTORS = frozenset({"plan", "self_verify", "prepare_packet", "review", "final_review"})
MAX_PROMPT_DIFF = 120_000

_CHECK = {"type": "object", "additionalProperties": False, "required": ["name", "status", "summary"],
          "properties": {"name": {"type": "string"},
                         "status": {"type": "string", "enum": ["PASS", "FAIL", "ERROR", "WARN", "SKIPPED"]},
                         "summary": {"type": "string"}}}
_STRS = {"type": "array", "items": {"type": "string"}}

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
                 "next_recommended_step": {"type": ["string", "null"]}}},
    "execute": {"type": "object", "additionalProperties": False,
                "required": ["summary", "changed_files", "checks", "deviations", "uncertainties"],
                "properties": {"summary": {"type": "string"}, "changed_files": _STRS,
                               "checks": {"type": "array", "items": _CHECK},
                               "deviations": _STRS, "uncertainties": _STRS}},
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
                              "checks": {"type": "array", "items": _CHECK}, "uncertainties": _STRS}},
}
OUTPUT_SCHEMAS["final_review"] = OUTPUT_SCHEMAS["review"]


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
             "REQUIRED_HUMAN_GATE_CONDITIONS exactly as written (you may append others). Include risk_guidance rows only where a "
             "roadmap item warrants a higher implementation or final-review floor, with an evidence-based reason; use "
             "an empty array when no item warrants escalation. Do not add roadmap work. On later invocations, "
             "omit directional_charter and echo its directional_charter_hash; select only the next bounded iteration "
             "from that frozen charter. Decide one iteration, no further justified action, or ESCALATE. Echo "
             "MANDATE.mandate_hash exactly. Iteration 1 carries all human acceptance criteria verbatim. roadmap_refs "
             "must be pending items with dependencies met. Set implementation_complexity to NORMAL, HARDER, or "
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
             "or grant scope. Do not modify any file."),
    "execute": ("You are the AAW IMPLEMENTER. Implement exactly PLAN inside WORKTREE_PATH. Respect CONSTRAINTS and "
                "FORBIDDEN_CHANGES. Do not merge, push, rebase, switch branches, or touch any other checkout; leave "
                "your changes uncommitted. Do not run Git commands: the controller owns and verifies Git boundaries. "
                "Run the acceptance checks you can and report each honestly (FAIL is a valid "
                "status). Name each check in English using the exact wording of the REQUIRED_EVIDENCE item it "
                "substantiates (for example a check named 'unit tests'); the controller matches evidence by that "
                "name. For every test or command check, the summary must state the exact command, its exit code and "
                "the reported result counts, so a reviewer can verify it from this record alone. "
                "Do not delete files such as __pycache__. "
                "Report deviations from the plan and known limitations (uncertainties)."),
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
               "diff/source evidence and use targeted retrieval when needed. Otherwise "
               "return PASS, REPAIR_REQUIRED, or ESCALATE with evidence references. Do not modify any file."),
    "repair": ("You are the AAW REPAIRER. Address only FINDINGS, inside EXACT_ALLOWED_REPAIR_SCOPE. Do not expand "
               "the goal; do not merge, push, rebase or switch branches; leave changes uncommitted. Report which "
               "finding keys you addressed and the checks you ran."),
}
ROLE_INSTRUCTIONS["final_review"] = ROLE_INSTRUCTIONS["review"].replace("REVIEWER", "FINAL REVIEWER")


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
    if harness not in SUPPORTED_HARNESSES:
        return None, f"harness {harness!r} is not a direct CLI harness for autonomy roles"
    status, reason = wr.profile_availability(profile)
    if status != "VERIFIED":
        return None, reason or "profile unavailable"
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
                "MANDATE": mandate, "ITERATION_INDEX": ctx["iteration_index"],
                "PLANNING_STAGE": ctx.get("planning_stage"),
                "FROZEN_DIRECTIONAL_CHARTER": ctx.get("directional_charter"),
                "FROZEN_DIRECTIONAL_CHARTER_HASH": ctx.get("directional_charter_hash"),
                "ROADMAP_STATUS": ctx["roadmap"], "HISTORY": ctx["history"],
                "WORKING_ROADMAP": ctx.get("working_roadmap"),
                "ITERATION_CONTRACT": ctx.get("iteration_contract"), "WORKSPACE": ctx.get("workspace")}
    if name in ("execute", "repair", "self_verify"):
        it = ctx["iteration"]
        out = {**common, "PLAN": ctx["plan"], "CONSTRAINTS": contract.get("constraints", []),
               "FORBIDDEN_CHANGES": contract.get("forbidden_changes", []),
               "REQUIRED_EVIDENCE": contract.get("required_evidence", []),
               "FORBIDDEN_AREAS": mandate["roadmap_mandate"]["autonomy_bounds"].get("forbidden_areas", [])}
        if name == "self_verify":
            out.update({"CHANGED_FILES": ctx.get("changed_files", []), "DIFF": _bounded(ctx.get("diff")),
                        "IMPLEMENTATION_RESULT": it.get("execution")})
        if name == "repair":
            out.update({"FINDINGS": ctx["findings"], "ATTEMPT": ctx["attempt"],
                        "EXACT_ALLOWED_REPAIR_SCOPE": {
                            "findings": [f.get("finding_key") for f in ctx["findings"]],
                            "rule": "Only changes directly required by FINDINGS; no new goal, no new feature."}})
        return out
    if name == "prepare_packet":
        return {**common, "REVIEW_KIND": "PRETREATMENT", "PACKET": ctx["packet"],
                "ROLE_RULE": "organize evidence only; do not assess correctness or emit a verdict"}
    # Review starts with the compact packet and source manifest. The controller
    # adds only specifically requested, hash-verified raw sources on a second call.
    raw = dict(ctx["raw"])
    return {**common, "REVIEW_KIND": ctx["review_kind"], "FROZEN_MANDATE": mandate,
            "FROZEN_DIRECTIONAL_CHARTER": ctx.get("directional_charter"),
            "FROZEN_DIRECTIONAL_CHARTER_HASH": ctx.get("directional_charter_hash"),
            "PLAN": ctx["iteration"]["plan"], "PACKET": ctx["packet"],
            "RAW_METADATA": {key: raw.get(key) for key in
                             ("diff_sha256", "diff_file_sha256", "diff_digest_note", "diff_path",
                              "changed_files", "head", "base_head", "commits")},
            "RAW_EVIDENCE_MANIFEST": raw.get("manifest", []),
            "PREVIOUS_UNRESOLVED_FINDINGS": raw.get("previous_findings", []),
            "RAW_EVIDENCE": ctx.get("raw_evidence_results", [])}


# ── the executor ────────────────────────────────────────────────────────────

def _argv(runtime: Mapping[str, Any], name: str, worktree: str, schema_path: Path, final_path: Path,
          schema: Mapping[str, Any], max_turns: int) -> tuple[list[str], bool]:
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
        prompt = (ROLE_INSTRUCTIONS[self.name] + "\nFinish with exactly the JSON object required by the output "
                  "schema.\n\nHANDOFF:\n" + json.dumps(handoff, indent=2, ensure_ascii=False, default=str))
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="aaw_autonomy_role_") as temp:
            schema_path, final_path = Path(temp) / "schema.json", Path(temp) / "final.json"
            schema_path.write_text(json.dumps(schema), encoding="utf-8")
            argv, on_stdin = _argv(runtime, self.name, worktree, schema_path, final_path, schema, self.max_turns)
            try:
                rc, stdout, stderr = wr.run_process(argv, cwd=Path(worktree), stdin=prompt if on_stdin else None,
                                                    timeout=self.timeout, provider=runtime["provider"],
                                                    adapter=ADAPTER_ID, dispatch=True,
                                                    env_remove=INHERITED_SESSION_ENV)
            except wr.WorkflowStop as exc:
                recorder.close(close_reason="FAILED", effect_certainty="CONFIRMED", outcome="BLOCKED",
                               observation_source="RUNNER_EXCEPTION" if recorder.started else "SPAWN_FAILURE",
                               detail=str(exc))
                raise ExecutorFailure(f"{self.name}: dispatch failed: {exc}", dispatched=recorder.started) from exc
            session, usage, raw, provider_meta = self._parse(runtime["harness"], rc, stdout, final_path)
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
            raise ExecutorFailure(f"{self.name}: provider process exited rc={rc}", code=code,
                                  dispatched=True, retryable=retryable)
        if not valid and self.name not in ("review", "final_review"):
            raise ExecutorFailure(f"{self.name}: provider returned no structured result", dispatched=True)
        # An invalid reviewer result is handed back as-is: `normalize_review`
        # turns it into ESCALATE (REVIEW_RESULT_INVALID), never a silent PASS.
        return raw

    @staticmethod
    def _parse(harness: str, rc: int, stdout: str, final_path: Path) -> tuple[Any, dict, Any, dict]:
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


def build_direct_executors(*, timeout: int = DEFAULT_PROVIDER_TIMEOUT_SECONDS,
                           max_turns: int = 30) -> dict[str, DirectRoleExecutor]:
    """The production executor set for `AutonomyController` (no `prepare_packet`:
    the deterministic packet is used as-is)."""
    return {name: DirectRoleExecutor(
                name,
                timeout=max(int(timeout), MIN_SIDE_EFFECT_TIMEOUT_SECONDS)
                if name in ("execute", "repair") else int(timeout),
                max_turns=max_turns)
            for name in ("plan", "execute", "self_verify", "prepare_packet", "review", "repair", "final_review")}
