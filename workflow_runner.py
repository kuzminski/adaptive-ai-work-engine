#!/usr/bin/env python3
"""AAW V0.2 static workflow runner (DIRECT_CLI_CONTROL, stdlib only)."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

import process_observation
import routing_contract
import run_cancellation
import run_recovery
from routing_contract import RoutingJournal
from run_cancellation import RunCancelled
from workflow_schema import LLM_NODE_TYPES, WorkflowValidationError, load_workflow, validate_node_result, validate_workflow
from model_catalog import CatalogError, PAID_ACCESS_CLASSES, validate_model_effort
from local_preprocess import compact_package, preprocess_for_node
from execution_contract import (
    allocate_execution, canonical_hash, create_candidate, create_human_decision,
    execution_ref, new_human_decision_id, update_execution,
)
from execution_ledger import SCHEMA_VERSION as LEDGER_SCHEMA_VERSION
from execution_ledger import ExecutionLedger, LedgerError, LifecycleRecorder
from aaw_paths import AAW_ROOT, MODEL_REGISTRY_PATH, STATS_ROOT


MODEL_REGISTRY = MODEL_REGISTRY_PATH
IMPLEMENTER_PROFILES = AAW_ROOT / "IMPLEMENTER_PROFILES.json"
EXECUTION_MODE = "DIRECT_CLI_CONTROL"
RESULT_OUTCOMES = {"PASS", "FAIL", "BLOCKED", "INVALID"}
STOP_STATUSES = {"BLOCKED", "INVALID", "WORKFLOW_LIMIT_REACHED", "WAITING_FOR_HUMAN", "READY_FOR_EXTERNAL_INTEGRATION", "REJECTED", "NO_ROUTE", "COMPLETED", "CANCELLED"}
IMPLEMENTER_NODE_TYPES = {"IMPLEMENT", "REVIEW", "REPAIR"}


class WorkflowStop(RuntimeError):
    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status


def now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="milliseconds")


def run_id() -> str:
    return f"AAW_{dt.datetime.now().astimezone():%Y%m%d_%H%M%S}_{secrets.token_hex(4)}"


def run_process(argv: Sequence[str], *, cwd: Path | None = None, stdin: str | None = None,
                timeout: int | None = None, provider: str | None = None,
                adapter: str | None = None, dispatch: bool = False) -> tuple[int, str, str]:
    """Spawn one child process, reporting the spawn only when it is a dispatch.

    ``Popen`` replaces ``subprocess.run`` so that V0.4B can record
    ``EXECUTION_STARTED`` from an actual observed process receipt (PID plus OS
    creation time) instead of inferring a start from intent. ``dispatch=True``
    marks the call that launches an invocation; helper spawns such as ``git``
    leave it false and are never reported as start evidence.

    A dispatch is also the cancellation boundary. When a token is in scope the
    child is registered on it for the duration of the wait, so a Stop from
    another thread terminates the process AAW is actually blocked on instead of
    setting a flag nobody reads until the node finishes.
    """
    environment = dict(os.environ)
    environment.pop("TERM", None)
    token = run_cancellation.current_token() if dispatch else None
    if token is not None:
        token.raise_if_requested(f"before_spawn:{argv[0]}")
    try:
        process = subprocess.Popen(
            list(argv), cwd=str(cwd) if cwd else None, text=True,
            encoding="utf-8", errors="replace",
            stdin=subprocess.PIPE if stdin is not None else None,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False, env=environment,
        )
    except FileNotFoundError as exc:
        raise WorkflowStop("BLOCKED", f"executable unavailable: {argv[0]}") from exc
    except OSError as exc:
        raise WorkflowStop("BLOCKED", f"process launch failed: {exc}") from exc
    process_observation.notify_process_start(
        process, dispatch=dispatch, argv=list(argv), cwd=cwd, provider=provider,
        adapter=adapter or EXECUTION_MODE)
    if token is None:
        return _wait_for_process(process, stdin=stdin, timeout=timeout)
    with token.track(process) as tracked:
        if not tracked:
            # Cancellation landed between the check and the spawn. `track` has
            # already stopped the child; do not report its exit as a result.
            token.raise_if_requested(f"spawn_raced_cancel:{argv[0]}")
        rc, stdout, stderr = _wait_for_process(process, stdin=stdin, timeout=timeout)
    token.raise_if_requested(f"after_child:{argv[0]}")
    return rc, stdout, stderr


def _wait_for_process(process: subprocess.Popen[str], *, stdin: str | None,
                      timeout: int | None) -> tuple[int, str, str]:
    try:
        stdout, stderr = process.communicate(input=stdin, timeout=timeout)
        return process.returncode, stdout or "", stderr or ""
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
        return 124, stdout or "", (stderr or "") + "\nPROCESS_TIMEOUT"


def _close_from_returncode(rc: int | None, *, provider_session_id: str | None = None) -> dict[str, Any]:
    """Translate one observed child exit into the bounded close taxonomy."""
    extra = {"provider_session_id": str(provider_session_id) if provider_session_id else None}
    if rc is None:
        return {"close_reason": "UNKNOWN", "effect_certainty": "UNKNOWN",
                "observation_source": "RUNNER_EXCEPTION", "exit_code": None, "extra": extra}
    if rc == 124:
        # The runner killed the child after a timeout. Whatever it already did
        # in the worktree, and any provider-side work, stay unknown.
        return {"close_reason": "TIMEOUT", "effect_certainty": "PARTIAL",
                "observation_source": "CHILD_PROCESS_EXIT", "exit_code": rc,
                "timed_out": True, "extra": extra}
    return {"close_reason": "COMPLETED" if rc == 0 else "FAILED",
            "effect_certainty": "CONFIRMED", "observation_source": "CHILD_PROCESS_EXIT",
            "exit_code": rc, "extra": extra}


def _record_intent(ledger: ExecutionLedger, state: dict[str, Any], state_path: Path,
                   execution: Mapping[str, Any], execution_path: Path, *,
                   repository: str | None = None, worktree: str | None = None,
                   binding: Mapping[str, Any] | None = None) -> LifecycleRecorder:
    """Durably record EXECUTION_INTENT before dispatch.

    Fail-closed: without durable intent, later reconciliation loses its primary
    lifecycle boundary, so no process is launched.
    """
    extra = {"binding_source": dict(binding).get("binding_source")} if binding else None
    try:
        event = ledger.record_execution_intent(
            execution_id=str(execution["execution_id"]), node_id=str(execution["node_id"]),
            subtask_id=execution.get("subtask_id"), invocation_kind=str(execution["invocation_kind"]),
            descriptor_path=execution_path, provider=execution.get("provider"),
            harness=execution.get("harness"), model=execution.get("model"),
            effort=execution.get("effort"), profile=execution.get("profile"),
            input_contract_hash=execution.get("input_contract_hash"),
            repository=repository, worktree=worktree, extra=extra,
        )
    except LedgerError as exc:
        raise WorkflowStop(
            "BLOCKED",
            f"{getattr(exc, 'classification', 'LEDGER_ERROR')} execution intent could not be durably "
            f"recorded; no process was launched: {exc}",
        ) from exc
    state.setdefault("lifecycle_records", []).append({
        "execution_id": str(execution["execution_id"]), "intent_event_id": event["event_id"],
        "intent_sequence": event["sequence"], "start_record_status": "NOT_OBSERVED",
        "close_record_status": "NOT_OBSERVED", "requires_reconciliation": False,
    })
    save_state(state_path, state)
    return LifecycleRecorder(ledger, str(execution["execution_id"]))


def _record_lifecycle_status(state: dict[str, Any], state_path: Path, recorder: LifecycleRecorder) -> None:
    """Surface an uncertain start/close record so it is never silently forgotten."""
    status = recorder.status()
    for row in state.get("lifecycle_records", []):
        if row.get("execution_id") == status["execution_id"]:
            row.update(status)
    save_state(state_path, state)


def git(path: Path, *args: str) -> str:
    rc, out, err = run_process(["git", "-C", str(path), *args])
    if rc != 0:
        raise WorkflowStop("BLOCKED", f"git {' '.join(args)} failed in {path}: {err.strip()}")
    return out.strip()


def canonical(path: Path) -> Path:
    return Path(os.path.realpath(path.resolve()))


def validate_workspace(repo: Path, worktree: Path) -> dict[str, Any]:
    if not repo.is_dir() or not worktree.is_dir():
        raise WorkflowStop("BLOCKED", "repo and worktree must already exist")
    repo_top = canonical(Path(git(repo, "rev-parse", "--show-toplevel")))
    worktree_top = canonical(Path(git(worktree, "rev-parse", "--show-toplevel")))
    if repo_top == worktree_top:
        raise WorkflowStop("BLOCKED", "worktree=main/canonical checkout is forbidden")
    repo_common = canonical(repo_top / git(repo_top, "rev-parse", "--git-common-dir"))
    worktree_common = canonical(worktree_top / git(worktree_top, "rev-parse", "--git-common-dir"))
    if repo_common != worktree_common:
        raise WorkflowStop("BLOCKED", "repo and worktree do not belong to the same Git repository")
    registered = git(repo_top, "worktree", "list", "--porcelain")
    registered_paths = {
        canonical(Path(line[9:])) for line in registered.splitlines() if line.startswith("worktree ")
    }
    if worktree_top not in registered_paths:
        raise WorkflowStop("BLOCKED", "execution workspace is not a registered Git worktree")
    repo_status = git(repo_top, "status", "--porcelain=v1", "--untracked-files=all")
    worktree_status = git(worktree_top, "status", "--porcelain=v1", "--untracked-files=all")
    if repo_status:
        raise WorkflowStop("BLOCKED", "canonical checkout has unexpected dirty state")
    authorized_baseline = run_recovery.inspect_authorized_baseline(worktree_top)
    if authorized_baseline.get("state") == run_recovery.BASELINE_DIVERGED:
        reasons = "; ".join(authorized_baseline.get("proof_reasons") or [])
        raise WorkflowStop("BLOCKED", f"BASELINE DIVERGED: {reasons or 'proof failed'}")
    if worktree_status:
        if authorized_baseline.get("state") != run_recovery.BASELINE_AUTHORIZED:
            raise WorkflowStop("BLOCKED", "execution worktree has unexpected dirty state")
    elif authorized_baseline.get("state") != run_recovery.BASELINE_AUTHORIZED:
        authorized_baseline = None
    return {
        "repo": str(repo_top), "worktree": str(worktree_top),
        "git_common_dir": str(repo_common),
        "main_head": git(repo_top, "rev-parse", "HEAD"),
        "main_status": repo_status,
        "worktree_head": git(worktree_top, "rev-parse", "HEAD"),
        "worktree_status": worktree_status,
        "authorized_workspace_baseline": authorized_baseline,
    }


def assert_main_unchanged(baseline: Mapping[str, Any]) -> None:
    repo = Path(str(baseline["repo"]))
    if git(repo, "rev-parse", "HEAD") != baseline["main_head"]:
        raise WorkflowStop("BLOCKED", "mutation outside allowed worktree: canonical HEAD changed")
    if git(repo, "status", "--porcelain=v1", "--untracked-files=all") != baseline["main_status"]:
        raise WorkflowStop("BLOCKED", "mutation outside allowed worktree: canonical status changed")


def changed_files(worktree: Path) -> list[str]:
    output = git(worktree, "status", "--porcelain=v1", "--untracked-files=all")
    files: list[str] = []
    for line in output.splitlines():
        value = line[3:]
        if " -> " in value:
            value = value.split(" -> ", 1)[1]
        files.append(value.strip('"'))
    return sorted(dict.fromkeys(files))


def git_diff(worktree: Path) -> str:
    tracked = git(worktree, "diff", "--no-ext-diff", "--binary", "HEAD")
    # `git diff` omits untracked files; include their complete text, bounded.
    untracked = git(worktree, "ls-files", "--others", "--exclude-standard").splitlines()
    chunks = [tracked]
    for rel in untracked:
        path = worktree / rel
        if path.is_file() and path.stat().st_size <= 200_000:
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                content = "<binary or unreadable>"
            chunks.append(f"\n--- /dev/null\n+++ b/{rel}\n{content}")
    return "\n".join(chunk for chunk in chunks if chunk)[-500_000:]


REPLACE_ATTEMPTS = 40
REPLACE_BACKOFF_SECONDS = 0.01


def atomic_json(path: Path, data: Mapping[str, Any]) -> None:
    """Write JSON so a reader never sees a partial document.

    ``os.replace`` is atomic, but on Windows it is *refused* while any other
    handle on the destination is open without ``FILE_SHARE_DELETE`` — which is
    exactly what a plain reader has. Before the UX bridge nothing read this
    file while a run was writing it, so a single attempt was enough; now a
    canvas polling the run state could make an authoritative state save fail
    and take the run down with it.

    Retrying is bounded and preserves atomicity: each attempt either replaces
    the file completely or does nothing, so the destination is always one whole
    document. Exhausting the attempts raises, because a state write that did
    not happen must never be reported as if it had.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            os.replace(temp, path)
            return
        except PermissionError:
            if attempt == REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(REPLACE_BACKOFF_SECONDS)


def unique_json(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise WorkflowStop("BLOCKED", f"refusing to overwrite artifact: {path}")
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def result_schema() -> dict[str, Any]:
    return {
        "type": "object", "additionalProperties": False,
        "properties": {
            "node_id": {"type": "string"}, "node_type": {"type": "string"},
            "outcome": {"type": "string", "enum": sorted(RESULT_OUTCOMES)},
            "summary": {"type": "string"},
            "changed_files": {"type": "array", "items": {"type": "string"}},
            "tests": {"type": "array", "items": {"type": "string"}},
            "findings": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "finding_id": {"type": "string"},
                    "severity": {"type": "string"}, "file": {"type": ["string", "null"]},
                    "location": {"type": ["string", "null"]}, "description": {"type": "string"},
                    "required_fix": {"type": "string"},
                },
                "required": ["severity", "file", "location", "description", "required_fix"],
            }},
            "remaining_uncertainty": {"type": "array", "items": {"type": "string"}},
            "recommended_next_action": {"type": "string"},
            # Routing-contract handoff. Optional: a node that omits these routes
            # on `outcome` exactly as before. `verdict` is the work-grade signal
            # the gate reads; `outcome` stays the execution-grade one.
            "verdict": {"type": ["string", "null"], "enum": ["PASS", "REPAIR", "BLOCKED", None]},
            "next_brief": {"type": ["string", "null"]},
            "carry_forward": {"type": "array", "items": {"type": "string"}},
            "artifacts": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["node_id", "node_type", "outcome", "summary", "changed_files", "tests", "findings", "remaining_uncertainty", "recommended_next_action"],
    }


def parse_codex_events(text: str) -> tuple[list[dict[str, Any]], str | None, dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            events.append(item)
    thread_id = next((str(e["thread_id"]) for e in events if e.get("type") == "thread.started" and e.get("thread_id")), None)
    usage = next((dict(e["usage"]) for e in reversed(events) if e.get("type") == "turn.completed" and isinstance(e.get("usage"), dict)), {})
    return events, thread_id, usage


def harness_executable(harness: str) -> str | None:
    found = shutil.which(harness)
    if found:
        return found
    if harness == "claude":
        candidate = Path.home() / ".local" / "bin" / "claude.exe"
        return str(candidate) if candidate.is_file() else None
    if harness == "codex":
        local = os.environ.get("LOCALAPPDATA")
        root = Path(local) / "OpenAI" / "Codex" / "bin" if local else Path()
        candidates = sorted(root.glob("*/codex.exe"), key=lambda path: path.stat().st_mtime, reverse=True) if root.is_dir() else []
        return str(candidates[0]) if candidates else None
    return None


def node_package(workflow: Mapping[str, Any], state: Mapping[str, Any], node: Mapping[str, Any], worktree: Path) -> dict[str, Any]:
    results = list(state.get("node_results", []))
    package: dict[str, Any] = {
        "WORKFLOW_ID": workflow["workflow_id"], "AAW_RUN_ID": state["AAW_RUN_ID"],
        "GOAL": state["goal"], "WORKTREE_PATH": str(worktree),
        "CURRENT_NODE_ID": node["id"], "NODE_TYPE": node["type"],
        "NODE_INSTRUCTIONS": node["instructions"], "ACCEPTANCE_CRITERIA": node["acceptance"],
        "PREVIOUS_NODE_RESULTS": results[-6:], "CHANGED_FILES": changed_files(worktree),
        "TEST_EVIDENCE": [item for item in results if item.get("node_type") == "MACHINE_GATE"][-3:],
        "OPEN_ISSUES": [finding for item in results for finding in item.get("findings", [])][-30:],
        "REPAIR_CYCLE": state["repair_cycle"], "LIMITS": state["limits"],
    }
    if node["type"] == "REVIEW":
        package["GIT_DIFF"] = git_diff(worktree)
        package["IMPLEMENTATION_RESULT"] = next((item for item in reversed(results) if item.get("node_type") in {"IMPLEMENT", "REPAIR"}), None)
        package["REVIEW_INDEPENDENCE"] = "SAME_PROVIDER_FRESH_CONTEXT"
    if node["type"] == "REPAIR":
        package["EXACT_ALLOWED_REPAIR_SCOPE"] = {
            "existing_changed_files": changed_files(worktree),
            "rule": "Only changes directly required to resolve the supplied machine failure or reviewer findings are allowed; a new file is allowed only when the finding itself requires it.",
            "latest_machine_failure": next((item for item in reversed(results) if item.get("node_type") == "MACHINE_GATE" and item.get("outcome") == "FAIL"), None),
            "latest_review_findings": next((item.get("findings", []) for item in reversed(results) if item.get("node_type") == "REVIEW" and item.get("outcome") == "FAIL"), []),
        }
    # Routing-contract handoff: what this node inherits from the path that
    # reached it. A branch node also carries the brief in its instructions, so
    # the model sees it whether or not it reads the structured package.
    lineage = node.get("lineage")
    if lineage:
        package["BRANCH_LINEAGE"] = lineage
        package["INHERITED_BRIEF"] = lineage.get("inherited_brief")
        # V0.1.1. What this branch inherited as a constraint, kept apart from
        # ACCEPTANCE_CRITERIA, which remains exactly the template's. The two
        # answer different questions and are no longer merged.
        package["INHERITED_CARRY_FORWARD"] = list(node.get("carry_forward") or [])
    carry_forward = routing_contract.accumulate_carry_forward(results)
    if carry_forward:
        package["CARRY_FORWARD"] = carry_forward
    upstream_artifacts = routing_contract.collect_artifacts(results)
    if upstream_artifacts:
        package["UPSTREAM_ARTIFACTS"] = upstream_artifacts
    return package


# ── the adapter seam, scoped to one run ──────────────────────────────────────
#
# AAW CANVAS FUNCTIONALIZATION V0.1 (§6 runtime hygiene). Substituting the
# provider adapter used to mean rebinding this module's attribute, which is
# process-wide: two adapter-backed runs in one process interfered, and a
# supervised run could see a substitute installed for a different one.
#
# The substitute now travels in a `ContextVar`, exactly like the cancellation
# token in `run_cancellation`, so it reaches only the call stack that installed
# it. `current_llm_adapter` still falls back to this module's attribute, so
# monkeypatching `workflow_runner.execute_llm_node` — which several existing
# tests do — keeps working unchanged.

LlmAdapter = Callable[..., "tuple[dict[str, Any], dict[str, Any]]"]

_LLM_ADAPTER: ContextVar["LlmAdapter | None"] = ContextVar("aaw_llm_adapter", default=None)


@contextmanager
def llm_adapter_scope(adapter: "LlmAdapter | None") -> Iterator[None]:
    """Make `adapter` the provider seam for this call stack only."""
    handle = _LLM_ADAPTER.set(adapter)
    try:
        yield
    finally:
        _LLM_ADAPTER.reset(handle)


def current_llm_adapter() -> "LlmAdapter":
    """The adapter in force here: the context-local one, else the module's."""
    return _LLM_ADAPTER.get() or globals()["execute_llm_node"]


def execute_llm_node(workflow: Mapping[str, Any], state: Mapping[str, Any], node: Mapping[str, Any], worktree: Path, execution: Mapping[str, Any], execution_path: Path, recorder: LifecycleRecorder | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Adapter boundary: execute one fresh direct provider node and return result+telemetry."""
    recorder = recorder or LifecycleRecorder(None, str(execution["execution_id"]))
    binding = state["workflow_bindings"][node["id"]]
    harness = str(binding["harness"])
    executable = harness_executable(harness)
    if not executable:
        raise WorkflowStop("BLOCKED", f"{harness} CLI unavailable")
    model, effort = str(binding.get("runtime_model_id") or ""), str(binding.get("effort") or "")
    if not model or not effort:
        raise WorkflowStop("BLOCKED", "model binding unavailable")
    package = node_package(workflow, state, node, worktree)
    package["EXECUTION_ID"] = execution["execution_id"]
    preprocess_spec = dict(state.get("preprocess_bindings", {}).get(node["id"], {}))
    preprocess = preprocess_for_node(
        run_id=str(state["AAW_RUN_ID"]), node_id=str(node["id"]), node_type=str(node["type"]), package=package,
        artifact_root=Path(str(state["preprocess_root"])), policy=str(state.get("preprocess_policy", "AUTO_SAFE")),
        requested_type=preprocess_spec.get("type"), required=bool(preprocess_spec.get("required", False)), reason=preprocess_spec.get("reason"),
        downstream_execution_id=str(execution["execution_id"]), execution_root=execution_path.parent,
    )
    state.setdefault("preprocess_telemetry", []).append({**dict(preprocess.get("telemetry") or {}), "status": preprocess.get("status"), "preprocess_type": preprocess.get("preprocess_type"), "profile": preprocess.get("profile"), "artifact_path": preprocess.get("artifact_path")})
    if preprocess.get("status") == "BLOCKED":
        update_execution(execution_path, str(execution["execution_id"]), status="BLOCKED")
        # Nothing was dispatched: the node is terminal before any provider contact.
        recorder.close(close_reason="FAILED", effect_certainty="CONFIRMED",
                       observation_source="PRE_DISPATCH_FAILURE", outcome="BLOCKED",
                       detail=str(preprocess.get("failure_reason") or "LOCAL_LLM_UNAVAILABLE"))
        return ({"execution_id": execution["execution_id"], "node_id": node["id"], "node_type": node["type"], "outcome": "BLOCKED", "summary": "required local preprocessing unavailable", "changed_files": changed_files(worktree), "tests": [], "findings": [], "remaining_uncertainty": [str(preprocess.get("failure_reason") or "LOCAL_LLM_UNAVAILABLE")], "recommended_next_action": "HUMAN_REQUIRED", "preprocess_artifact": preprocess.get("artifact_path")}, {"schema_version": "1.0", "execution_id": execution["execution_id"], "run_id": state["AAW_RUN_ID"], "node": node["id"], "harness": harness, "wall_time_s": 0.0, "outcome": "BLOCKED", "preprocess_artifact": preprocess.get("artifact_path")})
    package = compact_package(package, preprocess)
    behavioral = (
        "You are executing one frozen AAW workflow node. Work only inside WORKTREE_PATH and within GOAL, "
        "NODE_INSTRUCTIONS, and ACCEPTANCE_CRITERIA. Do not merge, push, create a PR, mutate the canonical checkout, "
        "or expand scope. REVIEW is read-only and must not repair code. REPAIR may address only supplied failures/findings. "
        "Finish with exactly the structured JSON required by the output schema. FAIL is unmet acceptance after a valid attempt; "
        "BLOCKED is external/capability blockage; INVALID means evidence cannot be trusted.\n\nNODE PACKAGE:\n"
    )
    prompt = behavioral + json.dumps(package, indent=2, ensure_ascii=False)

    def dispatch(argv: Sequence[str], **kwargs: Any) -> tuple[int, str, str]:
        """Single dispatch boundary: start observation in, terminal fact out."""
        try:
            with process_observation.observation_scope(recorder.observe_start):
                return run_process(argv, provider=binding.get("provider"), dispatch=True, **kwargs)
        except WorkflowStop as exc:
            recorder.close(close_reason="FAILED", effect_certainty="CONFIRMED", outcome="BLOCKED",
                           observation_source="RUNNER_EXCEPTION" if recorder.started else "SPAWN_FAILURE",
                           detail=str(exc))
            raise

    started_at = now()
    start = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="aaw_workflow_node_") as temp_dir:
        temp = Path(temp_dir)
        schema_path = temp / "result_schema.json"
        final_path = temp / "final.json"
        schema_path.write_text(json.dumps(result_schema()), encoding="utf-8")
        sandbox = "read-only" if node["type"] == "REVIEW" else "workspace-write"
        if harness == "codex":
            argv = [
                executable, "exec", "--ephemeral", "--skip-git-repo-check", "--sandbox", sandbox,
                "--model", model, "--config", f'model_reasoning_effort="{effort}"',
                "--cd", str(worktree), "--output-schema", str(schema_path),
                "--output-last-message", str(final_path), "--json", "-",
            ]
            rc, stdout, stderr = dispatch(argv, cwd=worktree, stdin=prompt)
            events, session_id, usage = parse_codex_events(stdout)
            raw = json.loads(final_path.read_text(encoding="utf-8")) if rc == 0 and final_path.is_file() else None
        else:
            permission = "plan" if node["type"] == "REVIEW" else "acceptEdits"
            argv = [
                executable, "--print", "--no-session-persistence", "--permission-mode", permission,
                "--model", model, "--effort", effort, "--output-format", "json",
                "--json-schema", json.dumps(result_schema(), separators=(",", ":")),
                "--max-turns", "30", prompt,
            ]
            rc, stdout, stderr = dispatch(argv, cwd=worktree)
            events = []
            envelope = json.loads(stdout) if rc == 0 else {}
            session_id = envelope.get("session_id")
            usage = dict(envelope.get("usage") or {})
            raw = envelope.get("structured_output")
            if raw is None:
                candidate = envelope.get("result")
                raw = json.loads(candidate) if isinstance(candidate, str) and candidate.lstrip().startswith("{") else candidate
        elapsed = time.monotonic() - start
        telemetry = {
            "schema_version": "1.1", "execution_id": execution["execution_id"], "run_id": state["AAW_RUN_ID"], "pipeline": None,
            "node": node["id"], "task_short": state["goal"][:80], "execution_mode": EXECUTION_MODE,
            "agent": harness, "implementer_profile": binding["profile"], "harness": binding["harness"],
            "model": model, "effort": effort,
            "access_class": binding.get("access_class", "UNKNOWN"), "binding_source": binding.get("binding_source"),
            "orca_run_id": None, "orca_task_id": None, "orca_dispatch_id": None,
            "provider_session_id": session_id, "started_at": started_at, "ended_at": now(),
            "wall_time_s": round(elapsed, 3),
            "usage": {
                "input_tokens": usage.get("input_tokens"),
                "cached_input_tokens": usage.get("cached_input_tokens", usage.get("cache_read_input_tokens")),
                "output_tokens": usage.get("output_tokens"),
                "reasoning_tokens": usage.get("reasoning_output_tokens"),
            },
            "outcome": "VALID PASS" if rc == 0 else "BLOCKED",
            "telemetry_status": "CAPTURED" if session_id and usage else "PARTIAL",
            "workflow_id": workflow["workflow_id"], "workflow_node_id": node["id"],
            "workflow_node_type": node["type"], "repair_cycle": state["repair_cycle"],
            "review_independence": "SAME_PROVIDER_FRESH_CONTEXT" if node["type"] == "REVIEW" else None,
            "preprocess_artifact": preprocess.get("artifact_path"), "preprocess_status": preprocess.get("status"),
        }
        update_execution(execution_path, str(execution["execution_id"]), provider_session_id=str(session_id) if session_id else None,
                         status="COMPLETED" if rc == 0 else "FAILED")
        close_kwargs = _close_from_returncode(rc, provider_session_id=session_id)
        if rc != 0:
            detail = (stderr or stdout)[-2000:]
            telemetry["outcome"] = "BLOCKED"
            recorder.close(outcome="BLOCKED", detail=detail, **close_kwargs)
            return ({
                "execution_id": execution["execution_id"], "node_id": node["id"], "node_type": node["type"], "outcome": "BLOCKED",
                "summary": f"LLM node process failed rc={rc}", "changed_files": changed_files(worktree),
                "tests": [], "findings": [{"severity": "HIGH", "file": None, "location": None, "description": detail, "required_fix": "Restore the exact planned model binding/capability before retrying."}],
                "remaining_uncertainty": ["No valid structured provider result was produced."],
                "recommended_next_action": "HUMAN_REQUIRED",
            }, telemetry)
        try:
            result = validate_node_result(raw, str(node["id"]), str(node["type"]))
        except (OSError, json.JSONDecodeError, WorkflowValidationError) as exc:
            telemetry["outcome"] = "INVALID"
            # The process exited cleanly; only the structured result is untrusted.
            recorder.close(outcome="INVALID", detail=f"malformed node result: {exc}",
                           **{**close_kwargs, "effect_certainty": "PARTIAL"})
            return ({
                "execution_id": execution["execution_id"], "node_id": node["id"], "node_type": node["type"], "outcome": "INVALID",
                "summary": f"malformed node result: {exc}", "changed_files": changed_files(worktree),
                "tests": [], "findings": [],
                "remaining_uncertainty": ["Provider output cannot be accepted as a structured node result."],
                "recommended_next_action": "STOP",
            }, telemetry)
        recorder.close(outcome=str(result.get("outcome")), **close_kwargs)
    result["execution_id"] = execution["execution_id"]
    result["provider_session_id"] = session_id
    if node["type"] == "REVIEW":
        reviewed = list((execution.get("relations") or {}).get("reviewed_execution_ids") or [])
        result["reviewed_execution_ids"] = reviewed
        for index, finding in enumerate(result.get("findings", []), 1):
            finding_id = str(finding.get("finding_id") or f"F{index:03d}")
            finding["finding_id"] = finding_id
            finding["finding_key"] = {"review_execution_id": execution["execution_id"], "finding_id": finding_id}
    return result, telemetry


def execute_machine_gate(state: Mapping[str, Any], node: Mapping[str, Any], worktree: Path, execution_id: str, recorder: LifecycleRecorder | None = None) -> dict[str, Any]:
    """A machine gate is an execution: it receives the same three lifecycle events."""
    recorder = recorder or LifecycleRecorder(None, execution_id)
    started = now()
    start = time.monotonic()
    try:
        with process_observation.observation_scope(recorder.observe_start):
            rc, stdout, stderr = run_process(node["command"], cwd=worktree, timeout=node["timeout_seconds"],
                                             provider="LOCAL", adapter="MACHINE_GATE", dispatch=True)
        outcome = "BLOCKED" if rc == 124 else ("PASS" if rc == 0 else "FAIL")
        recorder.close(outcome=outcome, **_close_from_returncode(rc))
    except WorkflowStop as exc:
        rc, stdout, stderr, outcome = None, "", str(exc), "BLOCKED"
        recorder.close(close_reason="FAILED", effect_certainty="CONFIRMED", outcome="BLOCKED",
                       observation_source="RUNNER_EXCEPTION" if recorder.started else "SPAWN_FAILURE",
                       detail=str(exc))
    return {
        "execution_id": execution_id, "node_id": node["id"], "node_type": node["type"], "outcome": outcome,
        "summary": f"machine command exited {rc}", "changed_files": changed_files(worktree),
        "tests": [{"command": node["command"], "cwd": str(worktree), "exit_code": rc}],
        "findings": [] if outcome == "PASS" else [{"severity": "HIGH", "description": "machine gate failed" if outcome == "FAIL" else "machine gate could not execute", "required_fix": stderr[-2000:] or stdout[-2000:]}],
        "remaining_uncertainty": [], "recommended_next_action": "follow on_pass" if rc == 0 else "follow on_fail",
        "command": node["command"], "cwd": str(worktree), "started_at": started, "finished_at": now(),
        "duration_s": round(time.monotonic() - start, 3), "exit_code": rc,
        "stdout_tail": stdout[-8000:], "stderr_tail": stderr[-8000:],
    }


def load_implementer_profiles() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(IMPLEMENTER_PROFILES.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowStop("BLOCKED", f"IMPLEMENTER_PROFILES unavailable: {exc}") from exc
    values = data.get("profiles") if isinstance(data, dict) else None
    if not isinstance(values, list):
        raise WorkflowStop("BLOCKED", "IMPLEMENTER_PROFILES profiles must be an array")
    profiles: dict[str, dict[str, Any]] = {}
    for raw in values:
        if not isinstance(raw, dict):
            raise WorkflowStop("BLOCKED", "IMPLEMENTER_PROFILES contains a non-object profile")
        profile_id = raw.get("profile_id")
        if not isinstance(profile_id, str) or not profile_id or profile_id in profiles:
            raise WorkflowStop("BLOCKED", "IMPLEMENTER_PROFILES profile_id must be unique and non-empty")
        if not isinstance(raw.get("harness"), str) or not raw["harness"] or not isinstance(raw.get("effort"), str) or not raw["effort"]:
            raise WorkflowStop("BLOCKED", f"invalid implementer profile {profile_id}")
        profiles[profile_id] = dict(raw)
    return profiles


def profile_availability(profile: Mapping[str, Any]) -> tuple[str, str | None]:
    """Return current runnable state without probing or changing the local runtime."""
    static = str(profile.get("availability") or "KNOWN_BUT_UNAVAILABLE")
    harness = str(profile.get("harness") or "")
    runtime_model_id = profile.get("runtime_model_id")
    if static != "VERIFIED":
        return "UNAVAILABLE", str(profile.get("unavailable_reason") or "profile is not locally verified")
    if not isinstance(runtime_model_id, str) or not runtime_model_id:
        return "UNAVAILABLE", "runtime model ID is unverified"
    try:
        model = validate_model_effort(runtime_model_id, str(profile.get("effort") or ""))
    except CatalogError as exc:
        return "UNAVAILABLE", str(exc)
    if not model.get("runtime_available") or not model.get("account_available"):
        return "UNAVAILABLE", str(model.get("reason") or "model is not available on current runtime/account")
    if model.get("access_class") in PAID_ACCESS_CLASSES:
        return "UNAVAILABLE", "model requires extra paid usage and NO_EXTRA_PAID_USAGE is active"
    executable = harness_executable(harness)
    if not executable:
        return "UNAVAILABLE", f"{harness} CLI is not available in PATH"
    return "VERIFIED", None


def parse_cli_bindings(values: Sequence[str] | None) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for raw in values or ():
        node_id, separator, profile_id = raw.partition("=")
        node_id, profile_id = node_id.strip(), profile_id.strip()
        if not separator or not node_id or not profile_id:
            raise WorkflowValidationError(f"invalid --bind {raw!r}; expected NODE_ID=PROFILE_ID")
        if node_id in parsed:
            raise WorkflowValidationError(f"duplicate --bind for {node_id}")
        parsed[node_id] = profile_id
    return parsed


def resolve_execution_plan(workflow: Mapping[str, Any], overrides: Mapping[str, str] | None = None, *, allow_unavailable: bool = False) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Resolve workflow defaults and human overrides once; the returned plan is freeze-ready."""
    requested = dict(overrides or {})
    profiles = load_implementer_profiles()
    nodes_by_id = {str(node["id"]): node for node in workflow["nodes"]}
    for node_id, profile_id in requested.items():
        node = nodes_by_id.get(node_id)
        if node is None:
            raise WorkflowValidationError(f"--bind references unknown node {node_id}")
        if node["type"] not in IMPLEMENTER_NODE_TYPES:
            raise WorkflowValidationError(f"--bind is allowed only for LLM nodes; {node_id} is {node['type']}")
        if profile_id not in profiles:
            raise WorkflowValidationError(f"unknown implementer profile {profile_id}")
        profile = profiles[profile_id]
        if profile.get("not_implementer") or node["type"] not in set(profile.get("suitable_for", [])):
            raise WorkflowValidationError(f"profile {profile_id} is not eligible for {node['type']}")

    try:
        registry = json.loads(MODEL_REGISTRY.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowStop("BLOCKED", f"MODEL_REGISTRY unavailable: {exc}") from exc
    bindings = {item.get("capability"): item for item in registry.get("active_bindings", []) if isinstance(item, dict)}
    resolved: list[dict[str, Any]] = []
    frozen: dict[str, dict[str, Any]] = {}
    for source in workflow["nodes"]:
        node = dict(source)
        node_id = str(node["id"])
        if node["type"] in IMPLEMENTER_NODE_TYPES and node_id in requested:
            profile = profiles[requested[node_id]]
            availability, reason = profile_availability(profile)
            binding = {
                "role": node["type"], "profile": profile["profile_id"], "harness": profile["harness"],
                "runtime_model_id": profile.get("runtime_model_id"), "effort": profile["effort"],
                "availability": availability, "availability_reason": reason, "binding_source": "HUMAN_OVERRIDE",
                "access_class": validate_model_effort(str(profile.get("runtime_model_id")), str(profile.get("effort"))).get("access_class"),
            }
        elif node["type"] in IMPLEMENTER_NODE_TYPES and node.get("model") == "gpt-5.6-sol" and node.get("effort") == "medium" and "SOL_MEDIUM" in profiles:
            profile = profiles["SOL_MEDIUM"]
            # The profile labels the familiar default; the template remains the authority
            # for its concrete runtime values when no human override was supplied.
            workflow_default = dict(profile)
            workflow_default["runtime_model_id"] = node["model"]
            workflow_default["effort"] = node["effort"]
            availability, reason = profile_availability(workflow_default)
            binding = {
                "role": node["type"], "profile": "SOL_MEDIUM", "harness": profile["harness"],
                "runtime_model_id": node["model"], "effort": node["effort"],
                "availability": availability, "availability_reason": reason, "binding_source": "WORKFLOW_DEFAULT",
                "access_class": validate_model_effort(str(node["model"]), str(node["effort"])).get("access_class"),
            }
        else:
            if node["type"] in LLM_NODE_TYPES and not node.get("model"):
                registry_binding = bindings.get(node.get("capability"))
                if not registry_binding or registry_binding.get("harness") != "codex" or not registry_binding.get("runtime_model_id"):
                    raise WorkflowStop("BLOCKED", f"model binding unavailable for {node['id']}: {node.get('capability')}")
                node["provider"] = "codex"
                node["model"] = registry_binding["runtime_model_id"]
                node["effort"] = node.get("effort") or registry_binding.get("effort")
            if node["type"] in LLM_NODE_TYPES:
                binding = {
                    "role": node["type"], "profile": "CURRENT_REVIEWER_DEFAULT" if node["type"] == "REVIEW" else "WORKFLOW_DEFAULT",
                    "harness": node.get("provider", "codex"), "runtime_model_id": node.get("model"), "effort": node.get("effort"),
                    "availability": "VERIFIED" if harness_executable(str(node.get("provider", "codex"))) else "UNAVAILABLE",
                    "availability_reason": None if harness_executable(str(node.get("provider", "codex"))) else f"{node.get('provider', 'codex')} CLI is not available in PATH",
                    "binding_source": "WORKFLOW_DEFAULT",
                    "access_class": "UNKNOWN",
                }
            else:
                binding = None
        if binding is not None:
            if binding["availability"] != "VERIFIED" and not allow_unavailable:
                raise WorkflowStop("BLOCKED", f"selected profile {binding['profile']} for {node_id} is unavailable: {binding['availability_reason']}")
            frozen[node_id] = binding
            node["resolved_binding"] = binding
            node["provider"] = binding["harness"]
            node["model"] = binding["runtime_model_id"]
            node["effort"] = binding["effort"]
        resolved.append(node)
    return resolved, frozen


def resolve_bindings(workflow: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Compatibility wrapper retained for existing self-tests and callers."""
    return resolve_execution_plan(workflow)[0]


def artifact_name(node: Mapping[str, Any], state: Mapping[str, Any]) -> str:
    prior = sum(1 for item in state.get("completed_nodes", []) if item.get("node_id") == node["id"])
    suffix = f"__cycle_{state['repair_cycle']:02d}" if prior or state["repair_cycle"] else ""
    return f"{node['id']}__{node['type']}{suffix}__result.json"


def workflow_summary(state: Mapping[str, Any]) -> dict[str, Any]:
    usage_rows = [item.get("usage", {}) for item in state.get("telemetry", [])]
    def total(key: str) -> int | None:
        values = [row.get(key) for row in usage_rows if row.get(key) is not None]
        return sum(values) if values else None
    started = dt.datetime.fromisoformat(str(state["started_at"]))
    return {
        "wall_time_total": round((dt.datetime.now().astimezone() - started).total_seconds(), 3),
        "llm_calls": state["llm_calls"], "nodes_completed": len(state["completed_nodes"]),
        "repair_cycles": state["repair_cycle"], "input_tokens_total": total("input_tokens"),
        "output_tokens_total": total("output_tokens"), "final_status": state["status"],
    }


def save_state(path: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = now()
    state["workflow_summary"] = workflow_summary(state)
    atomic_json(path, state)


def enforce_limits(state: Mapping[str, Any], node_type: str) -> None:
    limits = state["limits"]
    started = dt.datetime.fromisoformat(str(state["started_at"]))
    elapsed_minutes = (dt.datetime.now().astimezone() - started).total_seconds() / 60
    if elapsed_minutes >= limits["max_wall_time_minutes"]:
        raise WorkflowStop("WORKFLOW_LIMIT_REACHED", "wall-time limit reached; HUMAN_REQUIRED")
    if node_type == "REPAIR" and state["repair_cycle"] >= limits["max_repair_cycles"]:
        raise WorkflowStop("WORKFLOW_LIMIT_REACHED", "repair cycle limit reached; HUMAN_REQUIRED")
    if node_type in LLM_NODE_TYPES and state["llm_calls"] >= limits["max_llm_calls"]:
        raise WorkflowStop("WORKFLOW_LIMIT_REACHED", "LLM call limit reached; HUMAN_REQUIRED")


def dry_run(workflow_path: Path, goal: str, repo: Path, worktree: Path, overrides: Mapping[str, str] | None = None, preprocess_policy: str = "AUTO_SAFE") -> dict[str, Any]:
    workflow = load_workflow(workflow_path)
    baseline = validate_workspace(repo, worktree)
    nodes, frozen = resolve_execution_plan(workflow, overrides, allow_unavailable=True)
    commands = []
    for node in nodes:
        if node["type"] == "MACHINE_GATE":
            executable = node["command"][0]
            exists = bool(shutil.which(executable) or (worktree / executable).exists())
            commands.append({"node": node["id"], "argv": node["command"], "executable_available": exists})
            if not exists:
                raise WorkflowStop("BLOCKED", f"machine command unavailable: {executable}")
    max_path = len(nodes) + workflow["limits"]["max_repair_cycles"] * 3
    return {
        "status": "DRY_RUN_READY", "workflow_id": workflow["workflow_id"], "goal": goal,
        "workspace": baseline,
        "nodes": [{
            "id": n["id"], "type": n["type"], "profile": frozen.get(n["id"], {}).get("profile"),
            "harness": frozen.get(n["id"], {}).get("harness"), "model": n.get("model"), "effort": n.get("effort"),
            "availability": frozen.get(n["id"], {}).get("availability", "NOT_APPLICABLE"),
            "availability_reason": frozen.get(n["id"], {}).get("availability_reason"),
            "binding_source": frozen.get(n["id"], {}).get("binding_source"),
            "on_pass": n["on_pass"], "on_fail": n["on_fail"],
        } for n in nodes],
        "resolved_execution_plan": frozen,
        "start_allowed": all(binding["availability"] == "VERIFIED" for binding in frozen.values()),
        "machine_commands": commands, "limits": workflow["limits"], "preprocess_policy": preprocess_policy.upper(),
        "maximum_possible_node_executions": max_path, "main_merge_allowed": False,
    }


def apply_gate_decision(state: dict[str, Any], by_id: dict[str, Any], frozen: dict[str, Any],
                        journal: RoutingJournal, node: Mapping[str, Any], result: Mapping[str, Any],
                        decision: Mapping[str, Any]) -> None:
    """Turn one gate decision into frontier and lineage mutations.

    The only place in the runner where a routing decision changes what runs
    next. `evaluate_gate` stays pure, so any decision can be recomputed from
    the stored evidence and compared against what actually happened.
    """
    limits = state["limits"]
    completed = {row["node_id"] for row in state["completed_nodes"]}
    for row in decision["selected"]:
        edge_id, target, kind = row["edge_id"], row["to"], row["kind"]
        if target == "STOP":
            state["routing"]["terminals"].append({"edge_id": edge_id, "from": node["id"], "terminal": "STOP"})
            continue
        if target == "HUMAN_REQUIRED":
            raise WorkflowStop("WORKFLOW_LIMIT_REACHED", f"edge {edge_id} of {node['id']} requires a human")
        target = str(target)
        if kind == "REPAIR":
            template = by_id.get(target)
            if template is None:
                raise WorkflowStop("INVALID", f"edge {edge_id}: unknown REPAIR template {target!r}")
            if len(by_id) >= limits["max_nodes"]:
                raise WorkflowStop("WORKFLOW_LIMIT_REACHED", "max_nodes reached; cannot mint another repair branch")
            try:
                branch = routing_contract.mint_branch_node(
                    template, origin_node=node, origin_result=result, edge=row, known_ids=set(by_id))
            except routing_contract.RoutingContractError as exc:
                raise WorkflowStop("INVALID", f"edge {edge_id}: {exc}") from exc
            branch_id = str(branch["id"])
            by_id[branch_id] = branch
            inherited = dict(frozen.get(str(template["id"]), {}))
            if inherited:
                inherited["binding_source"] = "INHERITED_FROM_TEMPLATE"
                inherited["binding_template_id"] = str(template["id"])
                frozen[branch_id] = inherited
            state["routing"]["lineage"][branch_id] = branch["lineage"]
            # V0.1.1: the minted node exists only in `by_id`, which is
            # in-memory. Persisting its declarative projection is what lets a
            # consumer of workflow_state.json draw the branch as soon as it is
            # created rather than only after it has completed.
            projection = routing_contract.branch_projection(branch)
            state["routing"]["minted_nodes"][branch_id] = projection
            journal.append(routing_contract.BRANCH_CREATED, node_id=branch_id, payload={
                "template_id": str(template["id"]), "origin_node_id": str(node["id"]),
                "selected_edge_id": edge_id, "lineage": branch["lineage"],
                "inherited_brief": branch["lineage"].get("inherited_brief"),
                "carry_forward": branch["lineage"].get("carry_forward"),
                "node": projection, "binding": frozen.get(branch_id),
            })
            state["frontier"].append(branch_id)
            continue
        if target not in by_id:
            raise WorkflowStop("INVALID", f"edge {edge_id} of {node['id']} targets unknown node {target!r}")
        if target in completed or target in state["frontier"]:
            # V0.1 enters every node at most once. This is frontier dedup, not
            # a join: nothing is merged, the second edge simply does not
            # enqueue a duplicate. Rejoin semantics stay out of scope.
            state["routing"]["dedup"].append({
                "edge_id": edge_id, "target": target, "from": str(node["id"]),
                "reason": "ALREADY_COMPLETED" if target in completed else "ALREADY_QUEUED",
            })
            continue
        state["frontier"].append(target)


class ResumeRefused(WorkflowValidationError):
    """A `Run from here` whose preconditions do not hold. Carries a code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# Why a resume was refused. Closed set: the UX renders these directly.
RESUME_UNKNOWN_SOURCE_RUN = "RESUME_UNKNOWN_SOURCE_RUN"
RESUME_SOURCE_STILL_RUNNING = "RESUME_SOURCE_STILL_RUNNING"
RESUME_WORKFLOW_MISMATCH = "RESUME_WORKFLOW_MISMATCH"
RESUME_GRAPH_MOVED = "RESUME_GRAPH_MOVED"
RESUME_TARGET_NOT_DECLARED = "RESUME_TARGET_NOT_DECLARED"
RESUME_UNSATISFIED_DEPENDS_ON = "RESUME_UNSATISFIED_DEPENDS_ON"
RESUME_WORKTREE_MISMATCH = "RESUME_WORKTREE_MISMATCH"
RESUME_CODES = (RESUME_UNKNOWN_SOURCE_RUN, RESUME_SOURCE_STILL_RUNNING, RESUME_WORKFLOW_MISMATCH,
                RESUME_GRAPH_MOVED, RESUME_TARGET_NOT_DECLARED, RESUME_UNSATISFIED_DEPENDS_ON,
                RESUME_WORKTREE_MISMATCH)

# A source run whose status is none of these is still moving; its results are
# not a stable basis for anything.
RESUMABLE_SOURCE_STATUSES = frozenset(STOP_STATUSES)


def plan_resume(workflow: Mapping[str, Any], source_state: Mapping[str, Any], from_node: str, *,
                worktree: Path | None = None) -> dict[str, Any]:
    """Decide, before any work starts, exactly what a `Run from here` inherits.

    AAW CANVAS FUNCTIONALIZATION V0.1 §2. `Run from here` is not a new
    traversal rule: it is one existing run, minus the downstream cone of one
    node, re-entered at that node. `routing_contract.downstream_cone` answers
    "what does this node own" from the compiled edges the gate itself routes
    on; everything the cone does not own is inherited verbatim from the source
    run, which is what makes `depends_on` satisfiable at all.

    Pure and total: it either returns a plan or raises `ResumeRefused` with a
    code. Every precondition is checked here, before a run id is minted, so a
    refused resume leaves no run behind.

    Refusals, and why each one is a refusal rather than a guess:

      * the source run has not settled — its results are still moving;
      * a different workflow, or the same workflow whose semantics moved since
        — the inherited records would describe a graph that no longer exists;
      * a source run that predates semantic-hash recording — the graph
        *cannot be proved* unmoved, and an unprovable basis is not a basis;
      * a target that is not a declared node — re-entering a runtime-minted
        branch is branch re-execution, which V0.1 defers;
      * `depends_on` the inherited set does not cover;
      * a different worktree — the worktree is where the inherited work
        physically is, so resuming elsewhere would review an empty tree.
    """
    nodes = {str(node["id"]): node for node in (workflow.get("nodes") or [])}
    target = str(from_node)
    if target not in nodes:
        raise ResumeRefused(RESUME_TARGET_NOT_DECLARED, (
            f"{target!r} is not a declared node of {workflow.get('workflow_id')!r}; "
            "re-entering a runtime-minted branch is deferred"))

    status = str(source_state.get("status") or "")
    if status not in RESUMABLE_SOURCE_STATUSES:
        raise ResumeRefused(RESUME_SOURCE_STILL_RUNNING, (
            f"source run {source_state.get('AAW_RUN_ID')} is {status or 'UNKNOWN'}; "
            "only a settled run is a stable basis"))
    if str(source_state.get("workflow_id")) != str(workflow.get("workflow_id")):
        raise ResumeRefused(RESUME_WORKFLOW_MISMATCH, (
            f"source run ran {source_state.get('workflow_id')!r}, "
            f"target is {workflow.get('workflow_id')!r}"))

    current_hash = routing_contract.semantic_hash(workflow)
    source_hash = source_state.get("workflow_semantic_hash")
    if not source_hash:
        raise ResumeRefused(RESUME_GRAPH_MOVED, (
            f"source run {source_state.get('AAW_RUN_ID')} recorded no workflow semantic hash, "
            "so it cannot be proved to have run this graph"))
    if str(source_hash) != current_hash:
        raise ResumeRefused(RESUME_GRAPH_MOVED, (
            f"the workflow changed since run {source_state.get('AAW_RUN_ID')} "
            f"(ran {str(source_hash)[:12]}…, now {current_hash[:12]}…); "
            "re-run the whole workflow instead"))

    if worktree is not None:
        source_worktree = str(source_state.get("worktree") or "")
        if source_worktree and str(canonical(Path(worktree))) != source_worktree:
            raise ResumeRefused(RESUME_WORKTREE_MISMATCH, (
                f"source run worked in {source_worktree}, this run would work in "
                f"{canonical(Path(worktree))}; the inherited work is not there"))

    minted = dict((source_state.get("routing") or {}).get("minted_nodes") or {})
    cone = routing_contract.downstream_cone(workflow, target, minted=minted)
    owned = set(cone["nodes"])

    inherited_records, inherited_results = [], []
    for record in source_state.get("completed_nodes") or []:
        if str(record.get("node_id")) not in owned:
            inherited_records.append(dict(record))
    inherited_ids = {str(row["node_id"]) for row in inherited_records}
    for result in source_state.get("node_results") or []:
        if str(result.get("node_id")) in inherited_ids:
            inherited_results.append(dict(result))

    missing = sorted(set(str(item) for item in nodes[target].get("depends_on") or []) - inherited_ids)
    if missing:
        raise ResumeRefused(RESUME_UNSATISFIED_DEPENDS_ON, (
            f"{target} depends on {missing}, which the source run did not complete "
            "outside the reset cone"))

    # Budget is derived from what is actually inherited, never copied from the
    # source totals: a resumed run must not be able to launder its way past
    # `limits` by dropping the nodes that consumed them.
    llm_calls = sum(1 for row in inherited_records if str(row.get("node_type")) in LLM_NODE_TYPES)
    repair_cycle = max((int(row.get("repair_cycle") or 0) for row in inherited_records), default=0)

    return {
        "source_run_id": str(source_state.get("AAW_RUN_ID") or ""),
        "source_status": status,
        "from_node": target,
        "workflow_semantic_hash": current_hash,
        "reset_nodes": list(cone["nodes"]),
        "reset_declared": list(cone["declared"]),
        "reset_minted": list(cone["minted"]),
        "inherited_nodes": sorted(inherited_ids),
        "inherited_records": inherited_records,
        "inherited_results": inherited_results,
        "inherited_minted": {key: value for key, value in minted.items() if key in inherited_ids},
        "inherited_lineage": {key: value for key, value
                              in ((source_state.get("routing") or {}).get("lineage") or {}).items()
                              if key in inherited_ids},
        "llm_calls": llm_calls,
        "repair_cycle": repair_cycle,
    }


def _seed_from_resume(state: dict[str, Any], by_id: dict[str, Any],
                      journal: RoutingJournal, plan: Mapping[str, Any]) -> None:
    """Install an inherited upstream into a fresh run, and say so on the wire.

    The inherited records are copied verbatim: they are evidence of work that
    really happened, in this same worktree, and rewriting them would make a
    resumed run claim executions it never performed. Their artifact paths keep
    pointing into the source run, which is where those artifacts are.

    A canvas subscribed only to this run's journal would otherwise draw every
    inherited node as pending, so the whole inheritance is published as one
    structured `RUN_RESUMED` event rather than left to be discovered by
    reading another run's files.
    """
    state["completed_nodes"] = [dict(row) for row in plan["inherited_records"]]
    state["node_results"] = [dict(row) for row in plan["inherited_results"]]
    state["llm_calls"] = int(plan["llm_calls"])
    state["repair_cycle"] = int(plan["repair_cycle"])
    state["routing"]["minted_nodes"] = dict(plan.get("inherited_minted") or {})
    state["routing"]["lineage"] = dict(plan.get("inherited_lineage") or {})
    state["resumed_from"] = {
        "source_run_id": plan["source_run_id"], "source_status": plan["source_status"],
        "from_node": plan["from_node"], "reset_nodes": list(plan["reset_nodes"]),
        "inherited_nodes": list(plan["inherited_nodes"]),
        "workflow_semantic_hash": plan["workflow_semantic_hash"],
        "resumed_at": now(),
    }
    # An inherited minted branch is a real completed node of this run's graph.
    # Registering it keeps `by_id` total, so nothing downstream has to special
    # case a node that exists in the record but not in the declaration.
    for branch_id, projection in (plan.get("inherited_minted") or {}).items():
        by_id.setdefault(str(branch_id), dict(projection))

    journal.append(routing_contract.RUN_RESUMED, node_id=str(plan["from_node"]), payload={
        "source_run_id": plan["source_run_id"], "source_status": plan["source_status"],
        "from_node": plan["from_node"],
        "reset_nodes": list(plan["reset_nodes"]),
        "inherited_nodes": list(plan["inherited_nodes"]),
        "inherited": [
            {"node_id": row.get("node_id"), "node_type": row.get("node_type"),
             "outcome": row.get("outcome"), "verdict": row.get("verdict"),
             "execution_id": row.get("execution_id"), "artifact": row.get("artifact"),
             "lineage": row.get("lineage")}
            for row in plan["inherited_records"]
        ],
        "minted_nodes": list((plan.get("inherited_minted") or {}).values()),
        "llm_calls_inherited": int(plan["llm_calls"]),
        "repair_cycle_inherited": int(plan["repair_cycle"]),
        "not_reset": [
            "worktree contents: the inherited work is physically still there, unchanged",
            "the source run's artifacts, executions, ledger and journal are immutable",
        ],
    })


def execute(workflow_path: Path, goal: str, repo: Path, worktree: Path, overrides: Mapping[str, str] | None = None,
            preprocess_policy: str = "AUTO_SAFE", *, identifier: str | None = None,
            cancel: "run_cancellation.CancellationToken | None" = None,
            resume: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Execute one workflow. `identifier` and `cancel` are additive V0.1.1.

    A caller that needs to address a run *before* it has produced state — a
    UX boundary handing out a run handle, say — may mint the run id itself and
    pass it in. `cancel` makes the run interruptible; see `run_cancellation`
    for exactly what can and cannot be interrupted.

    `resume` is a plan from `plan_resume` (AAW CANVAS FUNCTIONALIZATION V0.1
    §2, `Run from here`). It seeds this run's completed set from a settled
    earlier run and starts the frontier at one node instead of `start_node`.
    It is a *new* run with its own id, artifacts and ledger — the source run
    is never reopened or mutated.
    """
    workflow = load_workflow(workflow_path)
    nodes, frozen = resolve_execution_plan(workflow, overrides)
    by_id = {node["id"]: node for node in nodes}
    baseline = validate_workspace(repo, worktree)
    identifier = str(identifier) if identifier else run_id()
    if cancel is not None and cancel.run_id is None:
        cancel.run_id = identifier
    ledger = ExecutionLedger.for_run(identifier, STATS_ROOT)
    artifact_root = STATS_ROOT / identifier / "WORKFLOW"
    state_path = artifact_root / "workflow_state.json"
    bindings_path = artifact_root / "workflow_bindings.json"
    unique_json(bindings_path, {
        "schema_version": "0.4A", "workflow_id": workflow["workflow_id"], "run_id": identifier,
        "created_at": now(), "bindings": frozen,
    })
    preprocess_policy = preprocess_policy.upper()
    if preprocess_policy not in {"OFF", "AUTO_SAFE", "MANUAL", "CUSTOM"}:
        raise WorkflowValidationError("invalid preprocess policy")
    preprocess_bindings = {str(node["id"]): dict(node.get("preprocess") or {}) for node in nodes if isinstance(node.get("preprocess"), Mapping)}
    start_node = str(workflow.get("start_node", nodes[0]["id"]))
    if resume is not None:
        # The plan was validated before the run id was minted; here it only
        # decides where the frontier starts.
        start_node = str(resume["from_node"])
    journal = RoutingJournal.for_run(identifier, STATS_ROOT, workflow_id=str(workflow["workflow_id"]))
    state: dict[str, Any] = {
        "workflow_id": workflow["workflow_id"], "AAW_RUN_ID": identifier, "goal": goal,
        # Recorded so a later `Run from here` can prove this run executed the
        # graph it is about to be used as a basis for. Layout-blind.
        "workflow_semantic_hash": routing_contract.semantic_hash(workflow),
        "status": "RUNNING", "current_node": start_node,
        "completed_nodes": [], "node_results": [], "repair_cycle": 0,
        "started_at": now(), "updated_at": now(), "worktree": str(canonical(worktree)),
        "repo": str(canonical(repo)), "main_merge_allowed": False, "limits": workflow["limits"],
        "final_outcome": None, "human_verdict": None, "llm_calls": 0,
        "telemetry": [], "workspace_baseline": baseline, "workflow_bindings": frozen,
        "workflow_bindings_path": str(bindings_path), "preprocess_policy": preprocess_policy,
        "preprocess_bindings": preprocess_bindings, "preprocess_root": str(STATS_ROOT / identifier / "PREPROCESS"), "preprocess_telemetry": [],
        "executions": [], "candidate": None, "human_decisions": [],
        "ledger_path": str(ledger.path), "ledger_schema_version": LEDGER_SCHEMA_VERSION,
        "lifecycle_records": [],
        # AAW MULTIROUTING RUNTIME CONTRACT V0.1. `frontier` replaces the single
        # cursor so fan-out is expressible at all; `current_node` stays the head
        # of it so every existing reader of workflow_state.json keeps working.
        "frontier": [start_node],
        "routing": {
            "contract_version": routing_contract.CONTRACT_VERSION,
            "journal_schema_version": routing_contract.JOURNAL_SCHEMA_VERSION,
            "journal_path": str(journal.path),
            "decisions": [], "lineage": {}, "minted_nodes": {}, "dedup": [], "terminals": [],
        },
    }
    if resume is not None:
        _seed_from_resume(state, by_id, journal, resume)
    save_state(state_path, state)
    try:
      with run_cancellation.cancellation_scope(cancel):
        while state["frontier"]:
            assert_main_unchanged(baseline)
            # Cancellation boundary. Checked before a node is entered, so a
            # Stop never starts new work; a node already in flight is
            # interrupted by terminating the child it is waiting on.
            run_cancellation.check("frontier_head")
            state["current_node"] = str(state["frontier"].pop(0))
            node = by_id[state["current_node"]]
            enforce_limits(state, str(node["type"]))
            journal.append(routing_contract.NODE_STARTED, node_id=str(node["id"]), payload={
                "node_type": node["type"], "lineage": node.get("lineage"),
                "pending_frontier": list(state["frontier"]),
            })
            completed_ids = {item["node_id"] for item in state["completed_nodes"]}
            if not set(node["depends_on"]).issubset(completed_ids):
                raise WorkflowStop("INVALID", f"unmet depends_on for {node['id']}: {sorted(set(node['depends_on']) - completed_ids)}")
            before_files = changed_files(canonical(worktree))
            if node["type"] == "REPAIR":
                state["repair_cycle"] += 1
            if node["type"] in LLM_NODE_TYPES:
                state["llm_calls"] += 1
                binding = frozen[node["id"]]
                relations: dict[str, Any] = {}
                if node["type"] == "REVIEW":
                    relations["reviewed_execution_ids"] = [str(item["execution_id"]) for item in state["completed_nodes"] if item.get("node_type") in {"IMPLEMENT", "REPAIR"} and item.get("execution_id")]
                    relations["reviewed_artifact_refs"] = [str(item["artifact"]) for item in state["completed_nodes"] if item.get("node_type") in {"IMPLEMENT", "REPAIR", "MACHINE_GATE"}]
                    relations["reviewed_base"] = baseline["worktree_head"]
                    relations["reviewed_head"] = git(canonical(worktree), "rev-parse", "HEAD")
                elif node["type"] == "REPAIR":
                    latest_review = next((item for item in reversed(state["node_results"]) if item.get("node_type") == "REVIEW"), {})
                    relations["originating_review_execution_id"] = latest_review.get("execution_id")
                    relations["selected_finding_keys"] = [item.get("finding_key") for item in latest_review.get("findings", []) if item.get("finding_key")]
                kind = {"REVIEW": "REVIEW", "REPAIR": "REPAIR"}.get(str(node["type"]), "LLM")
                execution, execution_path = allocate_execution(
                    descriptor_root=STATS_ROOT / identifier / "EXECUTIONS", run_id=identifier,
                    node_id=str(node["id"]), invocation_kind=kind, provider=binding.get("provider"),
                    harness=binding.get("harness"), model=binding.get("runtime_model_id"), effort=binding.get("effort"),
                    profile=binding.get("profile"), input_contract_hash=canonical_hash(node_package(workflow, state, node, canonical(worktree))),
                    selection_reason=binding.get("binding_source"), policy_version=str(workflow.get("version")), relations=relations,
                )
                state["executions"].append(execution_ref(execution, execution_path))
                save_state(state_path, state)
                recorder = _record_intent(ledger, state, state_path, execution, execution_path,
                                          repository=str(canonical(repo)), worktree=str(canonical(worktree)),
                                          binding=binding)
                result, telemetry = current_llm_adapter()(workflow, state, node, canonical(worktree), execution, execution_path, recorder)
                _record_lifecycle_status(state, state_path, recorder)
                after_files = changed_files(canonical(worktree))
                if node["type"] == "REVIEW" and after_files != before_files:
                    result["outcome"] = "INVALID"
                    result["summary"] = "read-only REVIEW mutated the worktree"
                result["reported_changed_files"] = result["changed_files"]
                result["changed_files"] = after_files
                telemetry["outcome"] = {
                    "PASS": "VALID PASS", "FAIL": "VALID FAIL",
                    "BLOCKED": "BLOCKED", "INVALID": "INVALID",
                }[result["outcome"]]
                state["telemetry"].append(telemetry)
                stamp = dt.datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")
                telemetry_path = STATS_ROOT / identifier / f"{node['id']}__{node['type']}__{telemetry['harness']}__{stamp}.json"
                unique_json(telemetry_path, telemetry)
                result["telemetry_path"] = str(telemetry_path)
            elif node["type"] == "MACHINE_GATE":
                execution, execution_path = allocate_execution(
                    descriptor_root=STATS_ROOT / identifier / "EXECUTIONS", run_id=identifier,
                    node_id=str(node["id"]), invocation_kind="MACHINE_GATE", provider="LOCAL", harness="subprocess",
                    input_contract_hash=canonical_hash({"command": node["command"], "cwd": str(canonical(worktree))}),
                    policy_version=str(workflow.get("version")),
                )
                state["executions"].append(execution_ref(execution, execution_path)); save_state(state_path, state)
                recorder = _record_intent(ledger, state, state_path, execution, execution_path,
                                          repository=str(canonical(repo)), worktree=str(canonical(worktree)))
                result = execute_machine_gate(state, node, canonical(worktree), str(execution["execution_id"]), recorder)
                _record_lifecycle_status(state, state_path, recorder)
                update_execution(execution_path, str(execution["execution_id"]), status="COMPLETED" if result["outcome"] in {"PASS", "FAIL"} else "BLOCKED")
            elif node["type"] in {"HUMAN_GATE", "FINAL_GATE"}:
                review_ids = [str(item["execution_id"]) for item in state["completed_nodes"] if item.get("node_type") == "REVIEW" and item.get("execution_id")]
                check_ids = [str(item["execution_id"]) for item in state["completed_nodes"] if item.get("node_type") == "MACHINE_GATE" and item.get("execution_id")]
                candidate_path = artifact_root / "candidate.json"
                candidate = create_candidate(path=candidate_path, run_id=identifier, repository=str(canonical(repo)), worktree=str(canonical(worktree)),
                                             candidate_head=git(canonical(worktree), "rev-parse", "HEAD"), artifact_manifest=None,
                                             review_execution_ids=review_ids, check_execution_ids=check_ids)
                state["candidate"] = {**candidate, "artifact_path": str(candidate_path)}
                result = {
                    "candidate_id": candidate["candidate_id"],
                    "node_id": node["id"], "node_type": node["type"], "outcome": "PASS",
                    "summary": "candidate awaits explicit human verdict", "changed_files": changed_files(canonical(worktree)),
                    "tests": [], "findings": [], "remaining_uncertainty": [],
                    "recommended_next_action": "ACCEPT CANDIDATE, REJECT, or LEAVE FOR LATER",
                }
            else:
                raise WorkflowStop("INVALID", f"unknown node type at runtime: {node['type']}")

            assert_main_unchanged(baseline)
            artifact_path = artifact_root / artifact_name(node, state)
            unique_json(artifact_path, result)
            binding = frozen.get(node["id"], {})
            record = {"execution_id": result.get("execution_id"), "node_id": node["id"], "node_type": node["type"], "outcome": result["outcome"], "artifact": str(artifact_path), "repair_cycle": state["repair_cycle"], "implementer_profile": binding.get("profile"), "harness": binding.get("harness"), "model": binding.get("runtime_model_id") or node.get("model"), "effort": binding.get("effort") or node.get("effort"), "duration_s": result.get("duration_s") or next((t["wall_time_s"] for t in reversed(state["telemetry"]) if t["node"] == node["id"]), None)}
            record["verdict"] = routing_contract.derive_verdict(result)
            record["lineage"] = node.get("lineage")
            state["completed_nodes"].append(record)
            state["node_results"].append(result)

            # Fail-closed before routing. INVALID never routes. BLOCKED routes
            # only when the node opted into the contract, where it can select a
            # FALLBACK edge; a legacy node keeps the original hard stop.
            if result["outcome"] == "INVALID" or (result["outcome"] == "BLOCKED" and not routing_contract.declares_edges(node)):
                journal.append(routing_contract.NODE_FAILED, node_id=str(node["id"]), payload={
                    "node_type": node["type"], "outcome": result["outcome"], "routable": False,
                    "summary": result.get("summary"), "artifact": str(artifact_path),
                })
                save_state(state_path, state)
                raise WorkflowStop(result["outcome"], result["summary"])

            journal.append(routing_contract.NODE_COMPLETED, node_id=str(node["id"]), payload={
                "node_type": node["type"], "outcome": result["outcome"], "verdict": record["verdict"],
                "execution_id": result.get("execution_id"), "artifact": str(artifact_path),
                "summary": result.get("summary"), "next_brief": result.get("next_brief"),
                "carry_forward": result.get("carry_forward") or [],
                "artifacts": result.get("artifacts") or [], "lineage": node.get("lineage"),
            })

            if node["type"] in {"HUMAN_GATE", "FINAL_GATE"}:
                journal.append(routing_contract.HUMAN_DECISION_REQUIRED, node_id=str(node["id"]), payload={
                    "candidate_id": result.get("candidate_id"), "reason": "HUMAN_GATE_REACHED",
                    "held_frontier": list(state["frontier"]),
                })
                state["status"] = "WAITING_FOR_HUMAN"
                state["final_outcome"] = "HUMAN_REQUIRED"
                state["current_node"] = node["id"]
                save_state(state_path, state)
                return state

            decision = routing_contract.evaluate_gate(node, result)
            decision_path = artifact_root / f"{node['id']}__GATE__decision.json"
            unique_json(decision_path, decision)
            routing_contract.record_decision(journal, decision)
            state["routing"]["decisions"].append({
                "node_id": str(node["id"]), "artifact": str(decision_path),
                "routing_mode": decision["routing_mode"], "outcome": decision["outcome"],
                "verdict": decision["verdict"], "no_route": decision["no_route"],
                "selected": [row["edge_id"] for row in decision["selected"]],
                "held": [{"edge_id": row["edge_id"], "to": row["to"], "hold_reason": row["hold_reason"]}
                         for row in decision["held"]],
                "decision_hash": decision["decision_hash"],
                "routing_input_hash": decision["routing_input_hash"],
            })
            if decision["no_route"]:
                save_state(state_path, state)
                raise WorkflowStop("NO_ROUTE", (
                    f"no edge of {node['id']} matched outcome={decision['outcome']} "
                    f"verdict={decision['verdict']}; considered "
                    f"{[row['edge_id'] for row in decision['candidates']]}"))
            apply_gate_decision(state, by_id, frozen, journal, node, result, decision)
            save_state(state_path, state)
        if state["status"] == "RUNNING":
            state["status"] = "COMPLETED"
            state["final_outcome"] = "COMPLETED"
            save_state(state_path, state)
    except RunCancelled as exc:
        # A real stop, recorded as one. The abandoned frontier is named so the
        # canvas can grey out exactly the work that never started, and the
        # partially executed node is named so nobody reads its absence as a
        # pass. Nothing is rolled back: the worktree keeps whatever the killed
        # child had already written, for a human to inspect.
        abandoned = list(state["frontier"])
        state["frontier"] = []
        state["status"] = "CANCELLED"
        state["final_outcome"] = "CANCELLED"
        state["stop_reason"] = f"cancelled at {exc.at_boundary}: {exc.reason}"
        state["cancellation"] = {
            "reason": exc.reason, "source": exc.source, "at_boundary": exc.at_boundary,
            "cancelled_at": now(), "interrupted_node": state.get("current_node"),
            "abandoned_frontier": abandoned,
            "processes": (cancel.snapshot().get("terminated_processes") if cancel else []),
            "not_interrupted": [
                "provider-side work already dispatched may still complete and may still bill",
                "worktree edits made before the child was terminated are left in place",
            ],
        }
        journal.append(routing_contract.RUN_CANCELLED, node_id=state.get("current_node"), payload={
            "reason": exc.reason, "source": exc.source, "at_boundary": exc.at_boundary,
            "interrupted_node": state.get("current_node"), "abandoned_frontier": abandoned,
        })
        save_state(state_path, state)
        return state
    except WorkflowStop as exc:
        state["status"] = exc.status
        state["final_outcome"] = exc.status
        state["stop_reason"] = str(exc)
        save_state(state_path, state)
        return state
    finally:
        assert_main_unchanged(baseline)
    return state


def apply_human_verdict(identifier: str, verdict: str) -> dict[str, Any]:
    state_path = STATS_ROOT / identifier / "WORKFLOW" / "workflow_state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowStop("BLOCKED", f"cannot load workflow state: {exc}") from exc
    if state.get("status") != "WAITING_FOR_HUMAN":
        raise WorkflowStop("BLOCKED", "human verdict is allowed only in WAITING_FOR_HUMAN")
    candidate = state.get("candidate") if isinstance(state.get("candidate"), Mapping) else None
    if not candidate or not candidate.get("candidate_id"):
        raise WorkflowStop("BLOCKED", "human verdict requires an exact candidate_id")
    normalized = verdict.upper().replace("-", "_")
    if normalized == "ACCEPT":
        state["human_verdict"] = "ACCEPTED"
        state["status"] = "READY_FOR_EXTERNAL_INTEGRATION"
        state["final_outcome"] = "READY_FOR_EXTERNAL_INTEGRATION"
    elif normalized == "REJECT":
        state["human_verdict"] = "REJECTED"
        state["status"] = "REJECTED"
        state["final_outcome"] = "REJECTED"
    elif normalized in {"LEAVE", "LEAVE_FOR_LATER"}:
        state["human_verdict"] = None
    else:
        raise WorkflowStop("INVALID", f"unknown human verdict: {verdict}")
    decision_id = new_human_decision_id(); decision_path = state_path.parent / f"{decision_id}.json"
    decision = create_human_decision(path=decision_path, candidate_id=str(candidate["candidate_id"]), verdict=str(state.get("human_verdict") or "LEFT_FOR_LATER"), human_decision_id=decision_id)
    state.setdefault("human_decisions", []).append({**decision, "artifact_path": str(decision_path)})
    # Emitted only after the immutable decision artifact is durably written.
    _record_human_decision_event(ExecutionLedger.for_run(identifier, STATS_ROOT), decision, decision_path)
    # V0.1.1. The journal already says HUMAN_DECISION_REQUIRED; without its
    # counterpart a UX consuming the event stream would see a gate open and
    # never see it close. The ledger's HUMAN_DECISION_RECORDED stays the
    # lifecycle authority and the artifact stays the detailed one; this is the
    # ordered graph-side fact that the gate is no longer blocking.
    journal = RoutingJournal.for_run(identifier, STATS_ROOT, workflow_id=state.get("workflow_id"))
    journal.append(routing_contract.HUMAN_DECISION_RESOLVED, node_id=state.get("current_node"), payload={
        "candidate_id": str(candidate["candidate_id"]),
        "human_decision_id": decision_id,
        "verdict": state.get("human_verdict") or "LEFT_FOR_LATER",
        "status": state["status"], "artifact": str(decision_path),
    })
    save_state(state_path, state)
    return state


def _record_human_decision_event(ledger: ExecutionLedger, decision: Mapping[str, Any], decision_path: Path) -> None:
    """The ledger references the decision; the artifact stays the detailed authority."""
    try:
        ledger.record_human_decision(
            human_decision_id=str(decision["human_decision_id"]), candidate_id=str(decision["candidate_id"]),
            verdict=str(decision["verdict"]), decision_artifact_path=decision_path,
            quality_assessment=decision.get("quality_assessment"), reason=decision.get("reason"),
        )
    except LedgerError as exc:
        # The decision itself already exists and is authoritative; a failed
        # reference is unresolved evidence, never a reason to redo the decision.
        raise WorkflowStop(
            "BLOCKED",
            f"{getattr(exc, 'classification', 'LEDGER_ERROR')} human decision {decision['human_decision_id']} "
            f"is durably written at {decision_path} but its ledger reference failed; "
            f"reconciliation may append it later: {exc}",
        ) from exc


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="AAW static workflow runner V0.2")
    value.add_argument("--workflow", type=Path)
    value.add_argument("--goal")
    value.add_argument("--repo", type=Path)
    value.add_argument("--worktree", type=Path)
    value.add_argument("--preprocess-policy", choices=("OFF", "AUTO_SAFE", "MANUAL", "CUSTOM"), default="AUTO_SAFE")
    value.add_argument("--dry-run", action="store_true")
    value.add_argument("--bind", action="append", default=[], metavar="NODE_ID=PROFILE_ID", help="Freeze a manual IMPLEMENT/REVIEW/REPAIR profile for this run; repeatable")
    value.add_argument("--human-verdict", choices=("accept", "reject", "leave-for-later"))
    value.add_argument("--run-id")
    value.add_argument("--validate", type=Path)
    value.add_argument("--self-test", action="store_true")
    return value


def self_test() -> int:
    base = {
        "workflow_id": "TEST", "version": "0.2", "description": "test", "goal": None,
        "workspace_policy": {"isolated_worktree_required": True, "main_merge_allowed": False},
        "limits": {"max_nodes": 5, "max_repair_cycles": 2, "max_wall_time_minutes": 5, "max_llm_calls": 3},
        "nodes": [
            {"id": "N1", "type": "IMPLEMENT", "depends_on": [], "run_if": "ALWAYS", "role": "CODE_IMPLEMENTER", "model": "gpt-5.6-sol", "effort": "medium", "instructions": "x", "acceptance": [], "on_pass": "N2", "on_fail": "STOP"},
            {"id": "N2", "type": "HUMAN_GATE", "depends_on": ["N1"], "run_if": "ON_TRANSITION", "role": None, "model": None, "effort": None, "instructions": "", "acceptance": [], "on_pass": "STOP", "on_fail": "STOP"},
        ],
    }
    validate_workflow(base)
    tests = 1
    for mutate in (
        lambda d: d["nodes"][0].update(type="UNKNOWN"),
        lambda d: d["nodes"][0].update(on_pass="MISSING"),
        lambda d: d["nodes"][0].update(on_pass="N1"),
    ):
        candidate = json.loads(json.dumps(base))
        mutate(candidate)
        try:
            validate_workflow(candidate)
        except WorkflowValidationError:
            tests += 1
        else:
            raise AssertionError("invalid workflow unexpectedly accepted")
    try:
        validate_node_result({"node_id": "N1"}, "N1", "IMPLEMENT")
    except WorkflowValidationError:
        tests += 1
    else:
        raise AssertionError("malformed node result unexpectedly accepted")
    limit_state = {
        "started_at": now(), "repair_cycle": 2, "llm_calls": 1,
        "limits": {"max_repair_cycles": 2, "max_llm_calls": 1, "max_wall_time_minutes": 5},
    }
    for node_type in ("IMPLEMENT", "REPAIR"):
        try:
            enforce_limits(limit_state, node_type)
        except WorkflowStop as exc:
            assert exc.status == "WORKFLOW_LIMIT_REACHED"
            tests += 1
        else:
            raise AssertionError(f"{node_type} limit unexpectedly accepted")
    unavailable = json.loads(json.dumps(base))
    unavailable["nodes"][0]["model"] = None
    unavailable["nodes"][0]["capability"] = "NO_SUCH_CAPABILITY"
    validate_workflow(unavailable)
    try:
        resolve_bindings(unavailable)
    except WorkflowStop as exc:
        assert exc.status == "BLOCKED"
        tests += 1
    else:
        raise AssertionError("unavailable model binding unexpectedly accepted")
    profiles = load_implementer_profiles()
    assert {"TERRA_HIGH", "SONNET_HIGH", "SOL_MEDIUM", "SOL_HIGH"}.issubset(profiles)
    terra_nodes, terra_frozen = resolve_execution_plan(base, {"N1": "TERRA_HIGH"})
    assert terra_nodes[0]["model"] == "gpt-5.6-terra"
    assert terra_frozen["N1"]["binding_source"] == "HUMAN_OVERRIDE"
    assert terra_frozen["N1"]["availability"] == "VERIFIED"
    tests += 3
    review = json.loads(json.dumps(base))
    review["nodes"][0].update(type="REVIEW", role="INDEPENDENT_REVIEWER", model="gpt-5.6-sol", effort="high")
    validate_workflow(review)
    review_nodes, review_frozen = resolve_execution_plan(review, {"N1": "SOL_HIGH"})
    assert review_nodes[0]["effort"] == "high" and review_frozen["N1"]["profile"] == "SOL_HIGH"
    tests += 2
    for bindings_override in ({"N2": "TERRA_HIGH"}, {"N1": "NO_SUCH_PROFILE"}):
        try:
            resolve_execution_plan(base, bindings_override)
        except WorkflowValidationError:
            tests += 1
        else:
            raise AssertionError("invalid implementer binding unexpectedly accepted")
    sonnet_nodes, sonnet_frozen = resolve_execution_plan(base, {"N1": "SONNET_HIGH"}, allow_unavailable=True)
    assert sonnet_nodes[0]["model"] == "claude-sonnet-5" and sonnet_frozen["N1"]["availability"] == "VERIFIED"
    tests += 1
    print(json.dumps({"status": "PASS", "tests": tests}))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.self_test:
            return self_test()
        overrides = parse_cli_bindings(args.bind)
        if args.validate:
            workflow = load_workflow(args.validate)
            print(json.dumps({"status": "VALID", "workflow_id": workflow["workflow_id"]}, indent=2))
            return 0
        if args.human_verdict:
            if not args.run_id:
                raise WorkflowValidationError("--human-verdict requires --run-id")
            result = apply_human_verdict(args.run_id, args.human_verdict)
        else:
            if not all((args.workflow, args.goal, args.repo, args.worktree)):
                raise WorkflowValidationError("--workflow, --goal, --repo, and --worktree are required")
            result = dry_run(args.workflow, args.goal, args.repo, args.worktree, overrides, args.preprocess_policy) if args.dry_run else execute(args.workflow, args.goal, args.repo, args.worktree, overrides, args.preprocess_policy)
        # ASCII escaping keeps Windows consoles/pipes reliable even when captured tool
        # output contains replacement characters from a legacy code page.
        print(json.dumps(result, indent=2, ensure_ascii=True))
        return 0 if result.get("status") in {"DRY_RUN_READY", "WAITING_FOR_HUMAN", "READY_FOR_EXTERNAL_INTEGRATION", "REJECTED", "COMPLETED"} else 20
    except (WorkflowValidationError, WorkflowStop) as exc:
        status = exc.status if isinstance(exc, WorkflowStop) else "INVALID"
        print(json.dumps({"status": status, "error": str(exc)}, indent=2, ensure_ascii=True))
        return 21 if status == "BLOCKED" else 22


if __name__ == "__main__":
    raise SystemExit(main())
