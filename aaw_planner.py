#!/usr/bin/env python3
"""AAW PLANNER PROPOSAL PIPELINE V0.1 — the planner provider seam.

One function is the boundary: `invoke_planner(package) -> (raw, telemetry)`.
Everything above it (`aaw_bridge.plan_from_node`) treats the result as
untrusted structured data; everything below it is the project's existing
provider machinery, reused rather than duplicated:

  * `model_catalog.resolve_profile` decides which model may be used, under the
    catalog's own availability and paid-usage policy;
  * `workflow_runner.harness_executable` finds the CLI;
  * `workflow_runner.run_process` spawns it, with the same cancellation and
    process-observation behaviour every other AAW dispatch has.

What makes a planner invocation *not* a workflow node execution, and why that
matters:

  * it has no node, no execution id, no ledger entry and no run;
  * it never touches a worktree — it is dispatched read-only, in a throwaway
    temporary directory, so it cannot edit the repository it is planning over;
  * it produces a **proposal**, which is inert until an operator accepts it,
    not a node result the router will act on.

The seam is a `ContextVar`, mirroring `workflow_runner.llm_adapter_scope` and
`run_cancellation`, so a substitute installed for one request reaches only
that call stack and never the process.
"""

from __future__ import annotations

import datetime as dt
import json
import tempfile
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

import model_catalog
import planner_proposal
import workflow_runner


PLANNER_CONTRACT = planner_proposal.PROPOSAL_CONTRACT

# Profiles whose catalog entry declares them suitable for planning. A planner
# binding is chosen from these and nowhere else, so "which model plans" is a
# catalog decision, not a constant hidden in this file.
PLANNING_TASK = "PLAN"
DEFAULT_PLANNER_PROFILE = "SOL_HIGH"
DEFAULT_TIMEOUT_SECONDS = 300

# Why a planner invocation produced nothing. Closed set: the UX renders these.
PLANNER_NO_BINDING = "PLANNER_NO_BINDING"
PLANNER_UNAVAILABLE = "PLANNER_UNAVAILABLE"
PLANNER_DISPATCH_FAILED = "PLANNER_DISPATCH_FAILED"
PLANNER_TIMEOUT = "PLANNER_TIMEOUT"
PLANNER_MALFORMED_OUTPUT = "PLANNER_MALFORMED_OUTPUT"
PLANNER_FAILURE_CODES = (PLANNER_NO_BINDING, PLANNER_UNAVAILABLE, PLANNER_DISPATCH_FAILED,
                         PLANNER_TIMEOUT, PLANNER_MALFORMED_OUTPUT)


class PlannerError(RuntimeError):
    """A planner invocation that produced no usable proposal. Fail-closed.

    Raised, never swallowed into an empty proposal: a planner that did not
    answer must not look like a planner that proposed nothing.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code

    def as_dict(self) -> dict[str, Any]:
        return {"error": self.code, "message": str(self)}


PlannerAdapter = Callable[..., "tuple[dict[str, Any], dict[str, Any]]"]

_PLANNER_ADAPTER: ContextVar["PlannerAdapter | None"] = ContextVar(
    "aaw_planner_adapter", default=None)


@contextmanager
def planner_adapter_scope(adapter: "PlannerAdapter | None") -> Iterator[None]:
    """Make `adapter` the planner seam for this call stack only."""
    handle = _PLANNER_ADAPTER.set(adapter)
    try:
        yield
    finally:
        _PLANNER_ADAPTER.reset(handle)


def current_planner_adapter() -> "PlannerAdapter":
    """The adapter in force here: the context-local one, else the real provider."""
    return _PLANNER_ADAPTER.get() or globals()["provider_planner"]


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="milliseconds")


# ───────────────────────────── binding ─────────────────────────────

def planner_profiles() -> list[str]:
    """Every catalog profile the project already marks as suitable for planning."""
    try:
        profiles = model_catalog.load_profiles()
    except model_catalog.CatalogError:
        return []
    return sorted(profile_id for profile_id, row in profiles.items()
                  if PLANNING_TASK in set(row.get("suitable_for") or ()))


def resolve_planner_binding(profile_id: str | None = None, *,
                            allow_paid: bool = False) -> dict[str, Any]:
    """Which model is allowed to plan, decided by the existing catalog rules.

    `resolve_profile` is the project's own gate: it refuses a profile that is
    unavailable on this runtime or account, and refuses a paid one unless paid
    usage is explicitly permitted. Planning does not get an exemption from it.
    """
    eligible = planner_profiles()
    if not eligible:
        raise PlannerError(PLANNER_NO_BINDING,
                           "no profile in IMPLEMENTER_PROFILES declares suitable_for PLAN")
    wanted = str(profile_id or DEFAULT_PLANNER_PROFILE)
    if wanted not in eligible:
        if profile_id is not None:
            raise PlannerError(PLANNER_NO_BINDING,
                               f"profile {wanted!r} is not declared suitable for planning; "
                               f"eligible: {eligible}")
        wanted = eligible[0]
    try:
        binding = model_catalog.resolve_profile(wanted, allow_paid=allow_paid)
    except model_catalog.CatalogError as exc:
        raise PlannerError(PLANNER_UNAVAILABLE, str(exc)) from exc
    binding["planner_profile"] = wanted
    binding["eligible_profiles"] = eligible
    return binding


# ───────────────────────── prompt construction ─────────────────────────

BEHAVIORAL_PREAMBLE = (
    "You are the AAW planner. You PROPOSE one bounded extension to an existing "
    "workflow graph. You have no execution authority: nothing you return runs, "
    "and nothing you return changes the workflow until a human operator "
    "explicitly accepts it.\n\n"
    "Return exactly one JSON object matching PLANNING_PACKAGE.response_schema. "
    "Obey every entry in PLANNING_PACKAGE.rules and every closed value set in "
    "PLANNING_PACKAGE.constraints. Do not exceed PLANNING_PACKAGE.limits. Do not "
    "invent fields. Do not name a model, a command, a file path, a shell "
    "invocation or any code to execute. Do not restate or modify existing nodes. "
    "State anything you had to assume in `assumptions` and anything an operator "
    "should check before accepting in `warnings`.\n\nPLANNING_PACKAGE:\n"
)


def proposal_output_schema() -> dict[str, Any]:
    """The JSON schema a provider is asked to conform its output to.

    Advisory only: the provider's schema support reduces malformed output, it
    does not make the output trusted. `planner_proposal.validate_proposal` is
    still the authority and still assumes the answer is hostile.
    """
    return {
        "type": "object", "additionalProperties": False,
        "required": ["intent", "nodes", "edges"],
        "properties": {
            "intent": {"type": "string"},
            "nodes": {
                "type": "array",
                "maxItems": planner_proposal.DEFAULT_LIMITS.max_nodes,
                "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["id", "type", "instructions", "acceptance"],
                    "properties": {
                        "id": {"type": "string"},
                        "type": {"enum": list(planner_proposal.PROPOSAL_NODE_TYPES)},
                        "role": {"enum": list(planner_proposal.PROPOSAL_ROLES)},
                        "capability": {"enum": list(planner_proposal.PROPOSAL_CAPABILITIES)},
                        "effort": {"enum": list(planner_proposal.PROPOSAL_EFFORTS)},
                        "instructions": {"type": "string"},
                        "acceptance": {"type": "array", "items": {"type": "string"}},
                        "depends_on": {"type": "array", "items": {"type": "string"}},
                        "run_if": {"enum": ["ALWAYS", "ON_TRANSITION"]},
                        "merge_policy": {"type": "string"},
                        "expected_incoming": {"type": "array", "items": {"type": "string"}},
                    },
                },
            },
            "edges": {
                "type": "array",
                "maxItems": planner_proposal.DEFAULT_LIMITS.max_edges,
                "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["edge_id", "from", "to"],
                    "properties": {
                        "edge_id": {"type": "string"},
                        "from": {"type": "string"},
                        "to": {"type": "string"},
                        "when": {"type": ["object", "null"]},
                        "kind": {"enum": ["CONTINUE", "REPAIR", "FALLBACK"]},
                        "label": {"type": "string"},
                    },
                },
            },
            "detach_edges": {"type": "array", "items": {"type": "string"}},
            "anchor_routing": {"type": ["string", "null"]},
            "replacements": {"type": "array", "items": {"type": "object"}},
            "assumptions": {"type": "array", "items": {"type": "string"}},
            "warnings": {"type": "array", "items": {"type": "string"}},
        },
    }


def planner_prompt(package: Mapping[str, Any]) -> str:
    return BEHAVIORAL_PREAMBLE + json.dumps(package, indent=2, ensure_ascii=False)


# ───────────────────────── the provider adapter ─────────────────────────

def provider_planner(package: Mapping[str, Any], *, profile_id: str | None = None,
                     timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
                     allow_paid: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
    """Dispatch one real planner invocation. Returns (raw proposal, telemetry).

    Read-only by construction: the child runs in a fresh temporary directory
    with the harness's non-writing mode, so a planner cannot edit the
    repository it is proposing over even if it tried.
    """
    binding = resolve_planner_binding(profile_id, allow_paid=allow_paid)
    harness = str(binding["harness"])
    executable = workflow_runner.harness_executable(harness)
    if not executable:
        raise PlannerError(PLANNER_UNAVAILABLE, f"{harness} CLI unavailable")
    model, effort = str(binding["runtime_model_id"]), str(binding["effort"])
    prompt = planner_prompt(package)
    request_id = f"PLANREQ-{uuid.uuid4().hex[:12]}"
    started_at, start = _now(), time.monotonic()

    with tempfile.TemporaryDirectory(prefix="aaw_planner_") as temp_dir:
        temp = Path(temp_dir)
        schema_path = temp / "proposal_schema.json"
        final_path = temp / "proposal.json"
        schema_path.write_text(json.dumps(proposal_output_schema()), encoding="utf-8")
        if harness == "codex":
            argv = [executable, "exec", "--ephemeral", "--skip-git-repo-check",
                    "--sandbox", "read-only", "--model", model,
                    "--config", f'model_reasoning_effort="{effort}"',
                    "--cd", str(temp), "--output-schema", str(schema_path),
                    "--output-last-message", str(final_path), "--json", "-"]
            stdin: str | None = prompt
        elif harness == "claude":
            argv = [executable, "--print", "--no-session-persistence",
                    "--permission-mode", "plan", "--model", model, "--effort", effort,
                    "--output-format", "json",
                    "--json-schema", json.dumps(proposal_output_schema(), separators=(",", ":")),
                    "--max-turns", "4", prompt]
            stdin = None
        else:
            raise PlannerError(PLANNER_NO_BINDING,
                               f"harness {harness!r} has no planner dispatch in V0.1")
        try:
            rc, stdout, stderr = workflow_runner.run_process(
                argv, cwd=temp, stdin=stdin, timeout=int(timeout_seconds),
                provider=binding.get("provider"), dispatch=True)
        except workflow_runner.WorkflowStop as exc:
            code = PLANNER_TIMEOUT if "timed out" in str(exc).lower() else PLANNER_DISPATCH_FAILED
            raise PlannerError(code, f"planner dispatch failed: {exc}") from exc
        elapsed = time.monotonic() - start
        if rc != 0:
            raise PlannerError(PLANNER_DISPATCH_FAILED,
                               f"planner exited {rc}: {(stderr or stdout or '')[:400]}")
        if harness == "codex":
            session_id, usage = None, {}
            try:
                _events, session_id, usage = workflow_runner.parse_codex_events(stdout)
            except Exception:                      # telemetry, never the result
                pass
            if not final_path.is_file():
                raise PlannerError(PLANNER_MALFORMED_OUTPUT,
                                   "planner produced no final message")
            try:
                raw = json.loads(final_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise PlannerError(PLANNER_MALFORMED_OUTPUT,
                                   f"planner output is not JSON: {exc}") from exc
        else:
            try:
                envelope = json.loads(stdout)
            except json.JSONDecodeError as exc:
                raise PlannerError(PLANNER_MALFORMED_OUTPUT,
                                   f"planner envelope is not JSON: {exc}") from exc
            session_id = envelope.get("session_id")
            usage = dict(envelope.get("usage") or {})
            raw = envelope.get("structured_output")
            if raw is None:
                candidate = envelope.get("result")
                try:
                    raw = (json.loads(candidate)
                           if isinstance(candidate, str) and candidate.lstrip().startswith("{")
                           else candidate)
                except json.JSONDecodeError as exc:
                    raise PlannerError(PLANNER_MALFORMED_OUTPUT,
                                       f"planner output is not JSON: {exc}") from exc

    if not isinstance(raw, Mapping):
        raise PlannerError(PLANNER_MALFORMED_OUTPUT,
                           f"planner output is {type(raw).__name__}, expected an object")
    telemetry = {
        "schema_version": "AAW_PLANNER_TELEMETRY_V0.1",
        "request_id": request_id,
        "invocation": "PLANNER_PROPOSAL",          # never a workflow node execution
        "harness": harness, "provider": binding.get("provider"),
        "profile": binding.get("planner_profile"),
        "model": model, "effort": effort,
        "access_class": binding.get("access_class"),
        "binding_source": binding.get("binding_source"),
        "provider_session_id": session_id,
        "started_at": started_at, "ended_at": _now(),
        "wall_time_s": round(elapsed, 3),
        "usage": {
            "input_tokens": usage.get("input_tokens"),
            "cached_input_tokens": usage.get("cached_input_tokens",
                                             usage.get("cache_read_input_tokens")),
            "output_tokens": usage.get("output_tokens"),
            "reasoning_tokens": usage.get("reasoning_output_tokens"),
        },
        "telemetry_status": "CAPTURED" if session_id and usage else "PARTIAL",
    }
    return dict(raw), telemetry


def invoke_planner(package: Mapping[str, Any], *, adapter: PlannerAdapter | None = None,
                   **kwargs: Any) -> tuple[Any, dict[str, Any]]:
    """Call the planner in force here, explicit substitute taking priority.

    The result is returned exactly as the adapter gave it, of whatever shape.
    Deciding whether planner output is usable is not this seam's job — it is
    `planner_proposal.normalize_proposal`'s, which treats the answer as
    hostile. A provider that could not be *parsed* at all is a different fact
    and is raised by `provider_planner` itself, where the parsing happens.
    """
    chosen = adapter or current_planner_adapter()
    raw, telemetry = chosen(package, **kwargs)
    return raw, dict(telemetry or {})


def planner_status(profile_id: str | None = None) -> dict[str, Any]:
    """Whether a real planner call is possible here, without making one."""
    row: dict[str, Any] = {"contract": PLANNER_CONTRACT,
                           "eligible_profiles": planner_profiles(),
                           "available": False, "reason": None, "binding": None}
    try:
        binding = resolve_planner_binding(profile_id)
    except PlannerError as exc:
        row["reason"] = exc.as_dict()
        return row
    executable = workflow_runner.harness_executable(str(binding["harness"]))
    row["binding"] = {key: binding.get(key) for key in
                      ("planner_profile", "harness", "provider", "runtime_model_id",
                       "effort", "access_class")}
    if not executable:
        row["reason"] = {"error": PLANNER_UNAVAILABLE,
                         "message": f"{binding['harness']} CLI unavailable"}
        return row
    row["available"] = True
    return row
