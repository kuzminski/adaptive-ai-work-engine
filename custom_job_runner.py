#!/usr/bin/env python3
"""AAW V0.3 static Custom Job runner (Direct CLI primary, stdlib only)."""

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
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import process_observation
from custom_job_schema import JobValidationError, load_job, validate_job
from model_catalog import CatalogError, load_profiles, resolve_profile
from local_preprocess import compact_package, preprocess_for_node
from workflow_runner import WorkflowStop, assert_main_unchanged, canonical, changed_files, git, git_diff, validate_workspace
from execution_contract import (
    allocate_execution, canonical_hash, create_candidate, create_human_decision,
    execution_ref, new_human_decision_id, update_execution,
)
from execution_ledger import SCHEMA_VERSION as LEDGER_SCHEMA_VERSION
from execution_ledger import ExecutionLedger, LedgerError, LifecycleRecorder
from aaw_paths import AAW_ROOT, STATS_ROOT


RESULTS = {"PASS", "FAIL", "BLOCKED", "INVALID"}
Adapter = Callable[[str, Mapping[str, Any], Path, Mapping[str, Any]], tuple[dict[str, Any], dict[str, Any]]]


def now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="milliseconds")


def new_run_id() -> str:
    return f"AAW_{dt.datetime.now().astimezone():%Y%m%d_%H%M%S}_{secrets.token_hex(4)}"


def atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def result_schema() -> dict[str, Any]:
    return {
        "type": "object", "additionalProperties": False,
        "properties": {
            "outcome": {"type": "string", "enum": sorted(RESULTS)},
            "summary": {"type": "string"},
            "changed_files": {"type": "array", "items": {"type": "string"}},
            "tests": {"type": "array", "items": {"type": "string"}},
            "findings": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "finding_id": {"type": "string"}, "severity": {"type": "string"},
                    "commit": {"type": ["string", "null"]}, "file": {"type": ["string", "null"]},
                    "location": {"type": ["string", "null"]}, "description": {"type": "string"},
                    "required_fix": {"type": "string"}
                },
                "required": ["finding_id", "severity", "commit", "file", "location", "description", "required_fix"]
            }},
            "remaining_uncertainty": {"type": "array", "items": {"type": "string"}},
            "recommended_next_action": {"type": "string"}
        },
        "required": ["outcome", "summary", "changed_files", "tests", "findings", "remaining_uncertainty", "recommended_next_action"]
    }


def normalize_result(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise WorkflowStop("INVALID", "provider result is not an object")
    missing = set(result_schema()["required"]) - set(value)
    if missing or value.get("outcome") not in RESULTS:
        raise WorkflowStop("INVALID", f"malformed provider result; missing={sorted(missing)}")
    for key in ("changed_files", "tests", "findings", "remaining_uncertainty"):
        if not isinstance(value.get(key), list):
            raise WorkflowStop("INVALID", f"provider result {key} must be an array")
    return dict(value)


def _run(argv: Sequence[str], cwd: Path, stdin: str | None = None, timeout: int = 1800,
         *, provider: str | None = None, adapter: str | None = None,
         dispatch: bool = False) -> tuple[int, str, str]:
    """Spawn one child process, reporting the spawn only when it is a dispatch.

    ``Popen`` replaces ``subprocess.run`` so that ``EXECUTION_STARTED`` can be
    an observed process receipt rather than an inference from intent.
    ``dispatch=True`` marks the call that launches an invocation; helper spawns
    an adapter happens to make are never reported as start evidence.
    """
    environment = dict(os.environ)
    environment.pop("TERM", None)
    try:
        process = subprocess.Popen(
            list(argv), cwd=str(cwd), text=True, encoding="utf-8", errors="replace",
            stdin=subprocess.PIPE if stdin is not None else None,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False, env=environment,
        )
    except OSError as exc:
        return 70, "", f"{type(exc).__name__}: {exc}"
    process_observation.notify_process_start(
        process, dispatch=dispatch, argv=list(argv), cwd=cwd, provider=provider,
        adapter=adapter or "DIRECT_CLI_CONTROL")
    try:
        stdout, stderr = process.communicate(input=stdin, timeout=timeout)
        return process.returncode, stdout or "", stderr or ""
    except subprocess.TimeoutExpired as exc:
        process.kill()
        stdout, stderr = process.communicate()
        return 70, stdout or "", f"{type(exc).__name__}: {exc}"


def _record_intent(ledger: ExecutionLedger, state: dict[str, Any], state_path: Path,
                   execution: Mapping[str, Any], execution_path: Path, *,
                   repository: str | None = None, worktree: str | None = None) -> LifecycleRecorder:
    """Durably record EXECUTION_INTENT before dispatch. Fail-closed."""
    try:
        event = ledger.record_execution_intent(
            execution_id=str(execution["execution_id"]), node_id=str(execution["node_id"]),
            subtask_id=execution.get("subtask_id"), invocation_kind=str(execution["invocation_kind"]),
            descriptor_path=execution_path, provider=execution.get("provider"),
            harness=execution.get("harness"), model=execution.get("model"),
            effort=execution.get("effort"), profile=execution.get("profile"),
            input_contract_hash=execution.get("input_contract_hash"),
            repository=repository, worktree=worktree,
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
    atomic_json(state_path, state)
    return LifecycleRecorder(ledger, str(execution["execution_id"]))


def _record_lifecycle_status(state: dict[str, Any], state_path: Path, recorder: LifecycleRecorder) -> None:
    """Surface an uncertain start/close record so it is never silently forgotten."""
    status = recorder.status()
    for row in state.get("lifecycle_records", []):
        if row.get("execution_id") == status["execution_id"]:
            row.update(status)
    atomic_json(state_path, state)


def _close_after_adapter_result(recorder: LifecycleRecorder, result: Mapping[str, Any],
                                session_id: Any = None) -> None:
    """Terminal fact for an adapter that returned a structured result.

    A returned result is not proof that workflow state advanced; it only proves
    that this invocation reached a terminal, observed boundary. When no process
    spawn was observed the adapter owned no process, and the close says so
    rather than implying a start that was never seen.
    """
    recorder.close(close_reason="COMPLETED", effect_certainty="CONFIRMED",
                   observation_source="ADAPTER_RESPONSE" if recorder.started else "IN_PROCESS_ADAPTER_RETURN",
                   outcome=str(result.get("outcome")),
                   extra={"provider_session_id": str(session_id) if session_id else None,
                          "start_evidence_observed": recorder.started})


def _close_after_adapter_failure(recorder: LifecycleRecorder, exc: BaseException) -> None:
    """Terminal fact for an adapter that raised.

    If a process was observed to start, side effects are unknown, so effect
    certainty is PARTIAL. If nothing was ever spawned, the failure is certain.
    """
    if recorder.started:
        recorder.close(close_reason="FAILED", effect_certainty="PARTIAL",
                       observation_source="RUNNER_EXCEPTION", outcome="BLOCKED",
                       detail=f"{type(exc).__name__}: {exc}"[:2000])
    else:
        recorder.close(close_reason="FAILED", effect_certainty="CONFIRMED",
                       observation_source="SPAWN_FAILURE", outcome="BLOCKED",
                       detail=f"{type(exc).__name__}: {exc}"[:2000])


def _close_from_returncode(rc: int | None) -> dict[str, Any]:
    """Translate one observed child exit into the bounded close taxonomy."""
    if rc is None:
        return {"close_reason": "UNKNOWN", "effect_certainty": "UNKNOWN",
                "observation_source": "RUNNER_EXCEPTION", "exit_code": None}
    return {"close_reason": "COMPLETED" if rc == 0 else "FAILED",
            "effect_certainty": "CONFIRMED", "observation_source": "CHILD_PROCESS_EXIT", "exit_code": rc}


def _executable(harness: str) -> str | None:
    found = shutil.which(harness)
    if found:
        return found
    if harness == "claude":
        candidate = Path.home() / ".local" / "bin" / "claude.exe"
        return str(candidate) if candidate.is_file() else None
    if harness == "codex":
        root = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI" / "Codex" / "bin"
        candidates = sorted(root.glob("*/codex.exe"), key=lambda p: p.stat().st_mtime, reverse=True) if root.is_dir() else []
        return str(candidates[0]) if candidates else None
    return None


def _codex_usage(stdout: str) -> tuple[str | None, dict[str, Any]]:
    session = None
    usage: dict[str, Any] = {}
    for line in stdout.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("type") == "thread.started":
            session = row.get("thread_id")
        if row.get("type") == "turn.completed" and isinstance(row.get("usage"), Mapping):
            usage = dict(row["usage"])
    return str(session) if session else None, usage


def direct_adapter(role: str, package: Mapping[str, Any], worktree: Path, binding: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    harness = str(binding["harness"])
    executable = _executable(harness)
    if not executable:
        raise WorkflowStop("BLOCKED", f"{harness} CLI unavailable")
    readonly = role in {"PLAN", "REVIEW", "DELTA_REVIEW"}
    prompt = (
        "Execute exactly one frozen AAW Custom Job node. Stay inside WORKTREE. Do not merge, push, amend, commit, create a PR, or expand scope. "
        "PLAN, REVIEW, and DELTA_REVIEW are read-only. Implementation nodes may edit only the requested scope. Do not use subagents. "
        "Return only the required structured result.\n\nNODE PACKAGE:\n" + json.dumps(package, ensure_ascii=False, indent=2)
    )
    started = now(); clock = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="aaw_custom_node_") as temp_name:
        temp = Path(temp_name); schema_path = temp / "schema.json"; final_path = temp / "final.json"
        schema = result_schema(); schema_path.write_text(json.dumps(schema), encoding="utf-8")
        if harness == "codex":
            argv = [executable, "exec", "--ephemeral", "--skip-git-repo-check", "--sandbox", "read-only" if readonly else "workspace-write", "--model", str(binding["runtime_model_id"]), "--config", f'model_reasoning_effort="{binding["effort"]}"', "--cd", str(worktree), "--output-schema", str(schema_path), "--output-last-message", str(final_path), "--json", "-"]
            rc, stdout, stderr = _run(argv, worktree, prompt, provider=str(binding.get("provider") or ""), dispatch=True)
            session, usage = _codex_usage(stdout)
            raw = json.loads(final_path.read_text(encoding="utf-8")) if rc == 0 and final_path.is_file() else None
        else:
            argv = [executable, "--print", "--no-session-persistence", "--permission-mode", "plan" if readonly else "acceptEdits", "--model", str(binding["runtime_model_id"]), "--effort", str(binding["effort"]), "--output-format", "json", "--json-schema", json.dumps(schema, separators=(",", ":")), "--max-turns", "30", prompt]
            rc, stdout, stderr = _run(argv, worktree, provider=str(binding.get("provider") or ""), dispatch=True)
            envelope = json.loads(stdout) if rc == 0 else {}
            session = envelope.get("session_id")
            usage = dict(envelope.get("usage") or {})
            raw = envelope.get("structured_output")
            if raw is None:
                candidate = envelope.get("result")
                raw = json.loads(candidate) if isinstance(candidate, str) and candidate.lstrip().startswith("{") else candidate
        if rc != 0:
            raise WorkflowStop("BLOCKED", f"{harness} node failed rc={rc}: {(stderr or stdout)[-1500:]}")
        result = normalize_result(raw)
    telemetry = {
        "node_type": role, "model": binding["runtime_model_id"], "effort": binding["effort"], "harness": harness,
        "provider": binding["provider"], "access_class": binding["access_class"], "binding_source": binding["binding_source"],
        "provider_session": session, "started_at": started, "ended_at": now(), "wall_time_s": round(time.monotonic() - clock, 3),
        "input_tokens": usage.get("input_tokens"), "cached_input": usage.get("cached_input_tokens", usage.get("cache_read_input_tokens")),
        "output_tokens": usage.get("output_tokens"), "reasoning_tokens": usage.get("reasoning_output_tokens", (usage.get("output_tokens_details") or {}).get("thinking_tokens") if isinstance(usage.get("output_tokens_details"), Mapping) else None),
    }
    result["provider_session"] = session
    return result, telemetry


def resolve_bindings(job: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    requested: dict[str, str] = {}
    if job["plan"].get("enabled"):
        requested["PLAN"] = str(job["plan"]["profile_id"])
    if job["job_type"] == "SINGLE_IMPLEMENTATION":
        requested["IMPLEMENT"] = str(job["implement_profile_id"])
    if job["job_type"] == "MULTI_SUBTASK":
        requested.update({str(row["subtask_id"]): str(row["profile_id"]) for row in job["subtasks"]})
    requested.update({"REVIEW": str(job["review"]["profile_id"]), "REPAIR": str(job["repair"]["profile_id"]), "DELTA_REVIEW": str(job["delta_review"]["profile_id"])})
    frozen: dict[str, dict[str, Any]] = {}
    profiles = load_profiles()
    for key, profile_id in requested.items():
        role = "SUBTASK" if key not in {"PLAN", "IMPLEMENT", "REVIEW", "REPAIR", "DELTA_REVIEW"} else key
        profile = profiles[profile_id]
        if role not in set(profile.get("suitable_for", [])):
            raise CatalogError(f"profile {profile_id} is not eligible for {role}")
        if profile.get("not_implementer") and role in {"IMPLEMENT", "SUBTASK", "REVIEW", "REPAIR"}:
            raise CatalogError(f"profile {profile_id} is not eligible for {role}")
        binding = resolve_profile(profile_id)
        binding["binding_source"] = str(job["binding_source"])
        frozen[key] = binding
    return frozen


def _gate(gate: Mapping[str, Any], worktree: Path, recorder: LifecycleRecorder | None = None) -> dict[str, Any]:
    """A machine gate is an execution: it receives the same three lifecycle events."""
    recorder = recorder or LifecycleRecorder(None, "")
    clock = time.monotonic()
    with process_observation.observation_scope(recorder.observe_start):
        rc, stdout, stderr = _run([str(x) for x in gate["command"]], worktree,
                                  timeout=int(gate["timeout_seconds"]), provider="LOCAL", adapter="MACHINE_GATE", dispatch=True)
    result = {"command": list(gate["command"]), "result": "PASS" if rc == 0 else "FAIL", "returncode": rc, "wall_time_s": round(time.monotonic()-clock, 3), "stdout_tail": stdout[-5000:], "stderr_tail": stderr[-5000:]}
    if recorder.started:
        recorder.close(outcome=result["result"], **_close_from_returncode(rc))
    else:
        # Nothing was spawned: `_run` reports a launch failure as rc 70.
        recorder.close(close_reason="FAILED", effect_certainty="CONFIRMED", outcome=result["result"],
                       observation_source="SPAWN_FAILURE", exit_code=rc, detail=stderr[-1000:] or None)
    return result


def _commit(worktree: Path, task_id: str, title: str, before_head: str) -> str:
    if git(worktree, "rev-parse", "HEAD") != before_head:
        raise WorkflowStop("INVALID", "provider changed Git history; runner-owned checkpoint required")
    if not git(worktree, "status", "--porcelain=v1", "--untracked-files=all"):
        raise WorkflowStop("FAIL", f"{task_id} produced no changes")
    git(worktree, "add", "-A")
    safe = " ".join(title.split())[:64]
    git(worktree, "commit", "-m", f"AAW: {task_id} {safe}")
    return git(worktree, "rev-parse", "HEAD")


def _check_limits(job: Mapping[str, Any], state: Mapping[str, Any]) -> None:
    limits = job["limits"]
    used_calls = len(state.get("telemetry", []))
    if used_calls >= int(limits["max_llm_calls"]):
        raise WorkflowStop("BLOCKED", f"max_llm_calls reached ({used_calls})")
    started = dt.datetime.fromisoformat(str(state["started_at"]))
    elapsed_minutes = (dt.datetime.now().astimezone() - started).total_seconds() / 60
    if elapsed_minutes >= float(limits["max_wall_time_minutes"]):
        raise WorkflowStop("BLOCKED", f"max_wall_time_minutes reached ({elapsed_minutes:.2f})")


def _review_package(job: Mapping[str, Any], state: Mapping[str, Any], worktree: Path) -> dict[str, Any]:
    baseline = str(state["baseline_commit"]); head = git(worktree, "rev-parse", "HEAD")
    return {"JOB_ID": job["job_id"], "GOAL": job["goal"], "WORKTREE": str(worktree), "ROLE": "REVIEW", "BASELINE_COMMIT": baseline, "HEAD": head,
            "COMMIT_LIST": git(worktree, "log", "--format=%H%x09%s", f"{baseline}..{head}").splitlines(), "SUBTASK_RESULTS": state.get("subtask_results", []),
            "MACHINE_TEST_EVIDENCE": state.get("machine_gates", []), "GIT_DIFF_BASELINE_TO_HEAD": git(worktree, "diff", "--no-ext-diff", "--binary", f"{baseline}..{head}"),
            "UNCOMMITTED_DIFF": git_diff(worktree), "CHANGED_FILES": sorted(set(git(worktree, "diff", "--name-only", f"{baseline}..{head}").splitlines()) | set(changed_files(worktree))), "ACCEPTANCE": job.get("acceptance", []),
            "REVIEWED_EXECUTION_IDS": [row.get("execution_id") for row in state.get("subtask_results", []) if row.get("execution_id")]}


def dry_run(job_path: Path) -> dict[str, Any]:
    job = load_job(job_path); baseline = validate_workspace(Path(job["repository"]), Path(job["worktree"])); frozen = resolve_bindings(job)
    warnings = [f"{key}: {row['access_class']}" for key, row in frozen.items() if row["access_class"] != "VERIFIED_INCLUDED"]
    subtasks = [{"subtask_id": row["subtask_id"], "title": row["title"], "binding": frozen[row["subtask_id"]]} for row in job.get("subtasks", [])]
    return {"status":"DRY_RUN_READY","job_id":job["job_id"],"job_type":job["job_type"],"repository":job["repository"],"worktree":job["worktree"],"baseline":baseline["worktree_head"],
            "subtasks":subtasks,"frozen_bindings":frozen,"expected_commit_boundaries":len(subtasks),"machine_gates":job["machine_gates"],"limits":job["limits"],
            "access_billing_warnings":warnings,"execution_adapter":job["execution_adapter"],"main_merge_allowed":False,"push_allowed":False,"llm_started":False}


def execute(job_path: Path, adapter: Adapter = direct_adapter) -> dict[str, Any]:
    job = load_job(job_path)
    if job["execution_adapter"] != "DIRECT_CLI_CONTROL":
        raise WorkflowStop("BLOCKED", "Custom Job V0.3 execution is Direct CLI primary; ORCA remains experimental and requires a separate verified runtime")
    baseline = validate_workspace(Path(job["repository"]), Path(job["worktree"])); worktree = canonical(Path(job["worktree"])); frozen = resolve_bindings(job)
    identifier = new_run_id(); root = STATS_ROOT / identifier / "CUSTOM_JOB"; state_path = root / "job_state.json"
    ledger = ExecutionLedger.for_run(identifier, STATS_ROOT)
    snapshot_path = root / "frozen_job.json"; atomic_json(snapshot_path, {**job, "frozen_bindings": frozen, "started_at": now()})
    state: dict[str, Any] = {"schema_version":"AAW_CUSTOM_JOB_RUN_V0.4A","AAW_RUN_ID":identifier,"job_id":job["job_id"],"job_type":job["job_type"],"goal":job["goal"],"status":"RUNNING",
        "started_at":now(),"updated_at":now(),"repository":str(canonical(Path(job["repository"]))),"worktree":str(worktree),"baseline_commit":baseline["worktree_head"],
        "main_baseline":baseline,"frozen_job_path":str(snapshot_path),"frozen_bindings":frozen,"subtask_results":[],"checkpoint_commits":[],"machine_gates":[],"telemetry":[],
        "review":None,"selected_repairs":[],"repair_commit":None,"delta_review":None,"human_verdict":None,"main_merge_allowed":False,"push_allowed":False,
        "preprocess_policy":str(job.get("preprocess_policy", "AUTO_SAFE")),"preprocess_bindings":dict(job.get("preprocess", {})),"preprocess_root":str(STATS_ROOT / identifier / "PREPROCESS"),"preprocess_telemetry":[],
        "executions":[],"commit_records":[],"candidate":None,"human_decisions":[],
        "ledger_path":str(ledger.path),"ledger_schema_version":LEDGER_SCHEMA_VERSION,"lifecycle_records":[]}
    atomic_json(state_path, state)
    def supported_adapter(role: str, package: Mapping[str, Any], binding: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        node_id = str(package.get("SUBTASK_ID") or role)
        subtask_id = str(package["SUBTASK_ID"]) if package.get("SUBTASK_ID") is not None else None
        relations: dict[str, Any] = {}
        if role == "REVIEW":
            relations = {"reviewed_execution_ids": list(package.get("REVIEWED_EXECUTION_IDS") or []), "reviewed_base": package.get("BASELINE_COMMIT"), "reviewed_head": package.get("HEAD")}
        kind = {"PLAN":"PLAN","REVIEW":"REVIEW","REPAIR":"REPAIR","DELTA_REVIEW":"DELTA_REVIEW"}.get(role, "LLM")
        execution, execution_path = allocate_execution(
            descriptor_root=STATS_ROOT / identifier / "EXECUTIONS", run_id=identifier, node_id=node_id,
            subtask_id=subtask_id, invocation_kind=kind, provider=binding.get("provider"), harness=binding.get("harness"),
            model=binding.get("runtime_model_id"), effort=binding.get("effort"), profile=binding.get("profile"),
            input_contract_hash=canonical_hash(package), selection_reason=binding.get("binding_source"),
            policy_version="AAW_CUSTOM_JOB_V0.3", relations=relations,
        )
        state["executions"].append(execution_ref(execution, execution_path)); atomic_json(state_path, state)
        recorder = _record_intent(ledger, state, state_path, execution, execution_path,
                                  repository=str(state["repository"]), worktree=str(worktree))
        spec = dict(state["preprocess_bindings"].get(role, {}))
        prepared_package = {**dict(package), "EXECUTION_ID": execution["execution_id"]}
        prep = preprocess_for_node(run_id=identifier, node_id=node_id, node_type=role, package=prepared_package, artifact_root=Path(state["preprocess_root"]), policy=state["preprocess_policy"], requested_type=spec.get("type"), required=bool(spec.get("required", False)), reason=spec.get("reason"), downstream_execution_id=str(execution["execution_id"]), execution_root=execution_path.parent)
        state["preprocess_telemetry"].append({**dict(prep.get("telemetry") or {}), "status":prep.get("status"),"preprocess_type":prep.get("preprocess_type"),"profile":prep.get("profile"),"artifact_path":prep.get("artifact_path")})
        if prep.get("status") == "BLOCKED":
            recorder.close(close_reason="FAILED", effect_certainty="CONFIRMED", outcome="BLOCKED",
                           observation_source="PRE_DISPATCH_FAILURE",
                           detail=str(prep.get("failure_reason") or prep.get("failure_code")))
            _record_lifecycle_status(state, state_path, recorder)
            raise WorkflowStop("BLOCKED", f"required local preprocessing unavailable: {prep.get('failure_reason') or prep.get('failure_code')}")
        try:
            with process_observation.observation_scope(recorder.observe_start):
                result, telemetry = adapter(role, compact_package(prepared_package, prep), worktree, binding)
        except Exception as exc:
            update_execution(execution_path, str(execution["execution_id"]), status="FAILED")
            _close_after_adapter_failure(recorder, exc)
            _record_lifecycle_status(state, state_path, recorder)
            raise
        result["execution_id"] = execution["execution_id"]
        if subtask_id is not None:
            result["subtask_id"] = subtask_id
        telemetry.update({"execution_id":execution["execution_id"],"node_id":node_id,"subtask_id":subtask_id})
        telemetry.update({"preprocess_artifact":prep.get("artifact_path"),"preprocess_status":prep.get("status")})
        session_id = telemetry.get("provider_session") or telemetry.get("provider_session_id")
        update_execution(execution_path, str(execution["execution_id"]), provider_session_id=str(session_id) if session_id else None, status="COMPLETED")
        _close_after_adapter_result(recorder, result, session_id)
        _record_lifecycle_status(state, state_path, recorder)
        if role == "REVIEW":
            result["reviewed_execution_ids"] = list(relations.get("reviewed_execution_ids") or [])
            for index, finding in enumerate(result.get("findings", []), 1):
                finding_id = str(finding.get("finding_id") or f"F{index:03d}")
                finding["finding_id"] = finding_id
                finding["finding_key"] = {"review_execution_id":execution["execution_id"],"finding_id":finding_id}
        return result, telemetry
    def supported_gate(gate: Mapping[str, Any], node_id: str, subtask_id: str | None = None) -> dict[str, Any]:
        execution, execution_path = allocate_execution(
            descriptor_root=STATS_ROOT / identifier / "EXECUTIONS", run_id=identifier, node_id=node_id,
            subtask_id=subtask_id, invocation_kind="MACHINE_GATE", provider="LOCAL", harness="subprocess",
            input_contract_hash=canonical_hash({"command":gate["command"],"cwd":str(worktree)}), policy_version="AAW_CUSTOM_JOB_V0.3",
        )
        state["executions"].append(execution_ref(execution, execution_path)); atomic_json(state_path, state)
        recorder = _record_intent(ledger, state, state_path, execution, execution_path,
                                  repository=str(state["repository"]), worktree=str(worktree))
        result = _gate(gate, worktree, recorder); result.update({"execution_id":execution["execution_id"],"node_id":node_id,"subtask_id":subtask_id})
        _record_lifecycle_status(state, state_path, recorder)
        update_execution(execution_path, str(execution["execution_id"]), status="COMPLETED" if result["result"] in {"PASS","FAIL"} else "BLOCKED")
        return result
    def record_commit_event(commit_record: Mapping[str, Any], before_head: str) -> None:
        """Emitted only for a runner-owned commit AAW has positive Git evidence for."""
        try:
            ledger.record_commit(
                repository=str(commit_record["repository"]), commit_hash=str(commit_record["commit_hash"]),
                producer_execution_ids=list(commit_record.get("producer_execution_ids") or []),
                expected_parent=commit_record.get("expected_parent"), subtask_id=commit_record.get("subtask_id"),
                role=commit_record.get("role"),
                git_evidence={"observed_head": git(worktree, "rev-parse", "HEAD"), "expected_parent": before_head,
                              "worktree": str(worktree)},
            )
        except LedgerError as exc:
            # The commit already exists in Git and stays authoritative. Never
            # create another commit; reconciliation may append the record later.
            raise WorkflowStop("BLOCKED", f"{getattr(exc, 'classification', 'LEDGER_ERROR')} commit "
                                          f"{commit_record['commit_hash']} exists in Git but its ledger record "
                                          f"failed: {exc}") from exc
    try:
        if job["plan"].get("enabled"):
            _check_limits(job, state)
            result, telemetry = supported_adapter("PLAN", {"JOB_ID":job["job_id"],"GOAL":job["goal"],"WORKTREE":str(worktree),"SCOPE_EXPANSION_ALLOWED":False,"EXPECTED_OUTPUT":"structured bounded execution plan"}, frozen["PLAN"])
            state["plan"] = result; state["telemetry"].append(telemetry)
            if result["outcome"] != "PASS":
                raise WorkflowStop(result["outcome"], result["summary"])
            if job["plan"].get("approval") == "HUMAN_APPROVAL":
                state["status"] = "WAITING_FOR_PLAN_APPROVAL"; atomic_json(state_path, state); return state

        if job["job_type"] == "MULTI_STAGE":
            state["status"] = "BLOCKED"; state["stop_reason"] = "MULTI_STAGE composition is persisted and dry-runnable in V0.3; child-stage execution requires explicit human starts"
            atomic_json(state_path, state); return state
        tasks = job.get("subtasks") if job["job_type"] == "MULTI_SUBTASK" else [{"subtask_id":"IMPLEMENT","title":"Single implementation","instructions":job["goal"],"profile_id":job["implement_profile_id"],"machine_gates":[]}]
        for index, task in enumerate(tasks, 1):
            assert_main_unchanged(baseline); before = git(worktree, "rev-parse", "HEAD")
            package = {"JOB_ID":job["job_id"],"JOB_CLASS":job["job_type"],"GOAL":job["goal"],"WORKTREE":str(worktree),"NODE_TYPE":"SUBTASK" if job["job_type"] == "MULTI_SUBTASK" else "IMPLEMENT",
                       "SUBTASK_INDEX":index,"SUBTASK_ID":task["subtask_id"],"CURRENT_NODE_CONTRACT":task["instructions"],"PREVIOUS_COMMITS":list(state["checkpoint_commits"]),
                       "PREVIOUS_STRUCTURED_RESULTS":list(state["subtask_results"]),"CURRENT_GIT_HEAD":before,"NO_CHAT_HISTORY_TRANSFER":True}
            _check_limits(job, state)
            result, telemetry = supported_adapter(package["NODE_TYPE"], package, frozen[str(task["subtask_id"] if job["job_type"] == "MULTI_SUBTASK" else "IMPLEMENT")])
            telemetry.update({"task_job_class":job["job_type"],"subtask_index":index}); state["telemetry"].append(telemetry)
            if result["outcome"] != "PASS":
                raise WorkflowStop(result["outcome"], result["summary"])
            gates = list(task.get("machine_gates", []))
            gate_results = [supported_gate(gate, f"{task['subtask_id']}:GATE:{gate_index}", str(task["subtask_id"])) for gate_index, gate in enumerate(gates, 1)]; state["machine_gates"].extend(gate_results)
            if any(row["result"] != "PASS" for row in gate_results):
                raise WorkflowStop("FAIL", f"machine gate failed after {task['subtask_id']}")
            result["subtask_index"] = index; result["subtask_id"] = str(task["subtask_id"]); result["machine_gate_result"] = "PASS"
            if job["job_type"] == "MULTI_SUBTASK":
                commit = _commit(worktree, str(task["subtask_id"]), str(task["title"]), before); result["checkpoint_commit"] = commit; state["checkpoint_commits"].append(commit)
                commit_record = {"repository":str(canonical(Path(job["repository"]))),"commit_hash":commit,"subtask_id":str(task["subtask_id"]),"producer_execution_ids":[result["execution_id"]],"expected_parent":before,"role":"SUBTASK"}
                result["checkpoint_commit_record"] = commit_record; state["commit_records"].append(commit_record)
                record_commit_event(commit_record, before)
            state["subtask_results"].append(result); atomic_json(state_path, state)

        for gate_index, gate in enumerate(job["machine_gates"].get("final", []), 1):
            verdict = supported_gate(gate, f"FINAL_GATE:{gate_index}"); state["machine_gates"].append(verdict)
            if verdict["result"] != "PASS":
                raise WorkflowStop("FAIL", "final machine gate failed")
        before_review_head = git(worktree, "rev-parse", "HEAD"); before_review_status = git(worktree, "status", "--porcelain=v1", "--untracked-files=all")
        _check_limits(job, state)
        review, telemetry = supported_adapter("REVIEW", _review_package(job, state, worktree), frozen["REVIEW"]); state["telemetry"].append(telemetry)
        if git(worktree, "rev-parse", "HEAD") != before_review_head or git(worktree, "status", "--porcelain=v1", "--untracked-files=all") != before_review_status:
            raise WorkflowStop("INVALID", "read-only REVIEW mutated Git state")
        state["review"] = review; state["review_findings_count"] = len(review["findings"])
        if review["outcome"] == "PASS":
            candidate_path = root / "candidate.json"
            candidate = create_candidate(path=candidate_path, run_id=identifier, repository=str(canonical(Path(job["repository"]))), worktree=str(worktree), candidate_head=git(worktree,"rev-parse","HEAD"), artifact_manifest=None,
                                         review_execution_ids=[str(review["execution_id"])], check_execution_ids=[str(row["execution_id"]) for row in state["machine_gates"] if row.get("execution_id")])
            state["candidate"] = {**candidate,"artifact_path":str(candidate_path)}
            state["status"] = "WAITING_FOR_HUMAN"
        elif review["outcome"] == "FAIL" and job["repair"]["selection_mode"] == "HUMAN_SELECTED":
            state["status"] = "WAITING_FOR_REPAIR_SELECTION"
        else:
            raise WorkflowStop(review["outcome"], review["summary"])
    except WorkflowStop as exc:
        state["status"] = exc.status; state["stop_reason"] = str(exc)
    finally:
        assert_main_unchanged(baseline); state["updated_at"] = now(); state["final_acceptance"] = state.get("status"); atomic_json(state_path, state)
    return state


def human_verdict(identifier: str, verdict: str) -> dict[str, Any]:
    path = STATS_ROOT / identifier / "CUSTOM_JOB" / "job_state.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("status") != "WAITING_FOR_HUMAN":
        raise WorkflowStop("BLOCKED", "verdict allowed only at WAITING_FOR_HUMAN")
    candidate = state.get("candidate") if isinstance(state.get("candidate"), Mapping) else None
    if not candidate or not candidate.get("candidate_id"):
        raise WorkflowStop("BLOCKED", "human verdict requires an exact candidate_id")
    if verdict == "accept":
        state["human_verdict"]="ACCEPTED"; state["status"]="READY_FOR_EXTERNAL_INTEGRATION"
    elif verdict == "reject":
        state["human_verdict"]="REJECTED"; state["status"]="REJECTED"
    else:
        state["human_verdict"]=None
    decision_id = new_human_decision_id(); decision_path = path.parent / f"{decision_id}.json"
    decision = create_human_decision(path=decision_path, candidate_id=str(candidate["candidate_id"]), verdict=str(state.get("human_verdict") or "LEFT_FOR_LATER"), human_decision_id=decision_id)
    state.setdefault("human_decisions", []).append({**decision,"artifact_path":str(decision_path)})
    # Emitted only after the immutable decision artifact is durably written.
    try:
        ExecutionLedger.for_run(identifier, STATS_ROOT).record_human_decision(
            human_decision_id=decision_id, candidate_id=str(candidate["candidate_id"]),
            verdict=str(decision["verdict"]), decision_artifact_path=decision_path,
            quality_assessment=decision.get("quality_assessment"), reason=decision.get("reason"))
    except LedgerError as exc:
        raise WorkflowStop("BLOCKED", f"{getattr(exc, 'classification', 'LEDGER_ERROR')} human decision "
                                      f"{decision_id} is durably written at {decision_path} but its ledger "
                                      f"reference failed: {exc}") from exc
    state["final_acceptance"] = state["status"]; atomic_json(path, state); return state


def selected_repair(identifier: str, finding_ids: Sequence[str], adapter: Adapter = direct_adapter) -> dict[str, Any]:
    path = STATS_ROOT / identifier / "CUSTOM_JOB" / "job_state.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("status") != "WAITING_FOR_REPAIR_SELECTION":
        raise WorkflowStop("BLOCKED", "selected repair is allowed only at WAITING_FOR_REPAIR_SELECTION")
    frozen_doc = json.loads(Path(state["frozen_job_path"]).read_text(encoding="utf-8")); job = validate_job(frozen_doc)
    selected_set = {str(item) for item in finding_ids}
    findings = state.get("review", {}).get("findings", []) if isinstance(state.get("review"), Mapping) else []
    selected = [dict(row) for row in findings if isinstance(row, Mapping) and str(row.get("finding_id")) in selected_set]
    if not selected or {str(row.get("finding_id")) for row in selected} != selected_set:
        raise WorkflowStop("INVALID", "every selected finding ID must exist in the frozen review")
    worktree = canonical(Path(state["worktree"])); baseline = state["main_baseline"]
    ledger = ExecutionLedger.for_run(identifier, STATS_ROOT)
    assert_main_unchanged(baseline); before_head = git(worktree, "rev-parse", "HEAD"); before_diff = git_diff(worktree)
    review_execution_id = state.get("review", {}).get("execution_id") if isinstance(state.get("review"), Mapping) else None
    selected_keys = [row.get("finding_key") or {"review_execution_id":review_execution_id,"finding_id":row.get("finding_id")} for row in selected]
    package = {"JOB_ID":job["job_id"],"GOAL":job["goal"],"WORKTREE":str(worktree),"NODE_TYPE":"REPAIR","SELECTED_FINDINGS":selected,"SELECTED_FINDING_KEYS":selected_keys,"ORIGINATING_REVIEW_EXECUTION_ID":review_execution_id,"UNSELECTED_FINDINGS":[row for row in findings if row not in selected],"EXACT_ALLOWED_SCOPE":"Only selected findings; do not rewrite history","CURRENT_HEAD":before_head,"CURRENT_DIFF":before_diff}
    def assisted(role: str, payload: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        relations = {"originating_review_execution_id":review_execution_id,"selected_finding_keys":selected_keys} if role == "REPAIR" else {
            "original_review_execution_id":review_execution_id,"repair_execution_id":state.get("repair_result", {}).get("execution_id"),
            "finding_dispositions":payload.get("FINDING_DISPOSITIONS"),
        }
        execution, execution_path = allocate_execution(
            descriptor_root=STATS_ROOT / identifier / "EXECUTIONS", run_id=identifier, node_id=role,
            invocation_kind=role, provider=state["frozen_bindings"][role].get("provider"), harness=state["frozen_bindings"][role].get("harness"),
            model=state["frozen_bindings"][role].get("runtime_model_id"), effort=state["frozen_bindings"][role].get("effort"),
            profile=state["frozen_bindings"][role].get("profile"), input_contract_hash=canonical_hash(payload),
            selection_reason=state["frozen_bindings"][role].get("binding_source"), policy_version="AAW_CUSTOM_JOB_V0.3", relations=relations,
        )
        state.setdefault("executions", []).append(execution_ref(execution, execution_path)); atomic_json(path, state)
        recorder = _record_intent(ledger, state, path, execution, execution_path,
                                  repository=str(state["repository"]), worktree=str(worktree))
        spec = dict(state.get("preprocess_bindings", {}).get(role, {}))
        prepared_payload = {**dict(payload),"EXECUTION_ID":execution["execution_id"]}
        prep = preprocess_for_node(run_id=identifier, node_id=role, node_type=role, package=prepared_payload, artifact_root=Path(str(state["preprocess_root"])), policy=str(state.get("preprocess_policy", "AUTO_SAFE")), requested_type=spec.get("type"), required=bool(spec.get("required", False)), reason=spec.get("reason"), downstream_execution_id=str(execution["execution_id"]), execution_root=execution_path.parent)
        state.setdefault("preprocess_telemetry", []).append({**dict(prep.get("telemetry") or {}), "status":prep.get("status"),"preprocess_type":prep.get("preprocess_type"),"profile":prep.get("profile"),"artifact_path":prep.get("artifact_path")})
        if prep.get("status") == "BLOCKED":
            recorder.close(close_reason="FAILED", effect_certainty="CONFIRMED", outcome="BLOCKED",
                           observation_source="PRE_DISPATCH_FAILURE",
                           detail=str(prep.get("failure_reason") or prep.get("failure_code")))
            _record_lifecycle_status(state, path, recorder)
            raise WorkflowStop("BLOCKED", f"required local preprocessing unavailable: {prep.get('failure_reason') or prep.get('failure_code')}")
        try:
            with process_observation.observation_scope(recorder.observe_start):
                result, telemetry = adapter(role, compact_package(prepared_payload, prep), worktree, state["frozen_bindings"][role])
        except Exception as exc:
            update_execution(execution_path, str(execution["execution_id"]), status="FAILED")
            _close_after_adapter_failure(recorder, exc)
            _record_lifecycle_status(state, path, recorder)
            raise
        result["execution_id"] = execution["execution_id"]
        telemetry.update({"execution_id":execution["execution_id"],"node_id":role,"subtask_id":None,"preprocess_artifact":prep.get("artifact_path"),"preprocess_status":prep.get("status")})
        session_id = telemetry.get("provider_session") or telemetry.get("provider_session_id")
        update_execution(execution_path, str(execution["execution_id"]), provider_session_id=str(session_id) if session_id else None, status="COMPLETED")
        _close_after_adapter_result(recorder, result, session_id)
        _record_lifecycle_status(state, path, recorder)
        if role == "DELTA_REVIEW":
            result["repair_execution_id"] = relations.get("repair_execution_id")
            result["original_review_execution_id"] = review_execution_id
        return result, telemetry
    _check_limits(job, state)
    repair, telemetry = assisted("REPAIR", package); state["telemetry"].append(telemetry)
    if repair["outcome"] != "PASS":
        state["status"] = repair["outcome"]; state["stop_reason"] = repair["summary"]
    else:
        state["selected_repairs"] = sorted(selected_set); state["repair_result"] = repair
        if state["job_type"] == "MULTI_SUBTASK":
            state["repair_commit"] = _commit(worktree, "REPAIR", "selected review findings", before_head)
            repair_record = {"repository":str(canonical(Path(state["repository"]))),"commit_hash":state["repair_commit"],"subtask_id":None,"producer_execution_ids":[repair["execution_id"]],"expected_parent":before_head,"role":"REPAIR"}
            state.setdefault("commit_records", []).append(repair_record); repair["commit_record"] = repair_record
            try:
                ledger.record_commit(
                    repository=str(repair_record["repository"]), commit_hash=str(repair_record["commit_hash"]),
                    producer_execution_ids=list(repair_record["producer_execution_ids"]),
                    expected_parent=before_head, subtask_id=None, role="REPAIR",
                    git_evidence={"observed_head": git(worktree, "rev-parse", "HEAD"),
                                  "expected_parent": before_head, "worktree": str(worktree)})
            except LedgerError as exc:
                raise WorkflowStop("BLOCKED", f"{getattr(exc, 'classification', 'LEDGER_ERROR')} repair commit "
                                              f"{repair_record['commit_hash']} exists in Git but its ledger "
                                              f"record failed: {exc}") from exc
            repair_range = f"{before_head}..{state['repair_commit']}"
        else:
            repair_range = "UNCOMMITTED_SINGLE_IMPLEMENTATION_REPAIR"
        delta_package = {"JOB_ID":job["job_id"],"GOAL":job["goal"],"WORKTREE":str(worktree),"NODE_TYPE":"DELTA_REVIEW","ORIGINAL_FINDINGS":findings,"SELECTED_FINDINGS":selected,
                         "ORIGINAL_REVIEW_EXECUTION_ID":review_execution_id,"REPAIR_EXECUTION_ID":repair["execution_id"],"FINDING_DISPOSITIONS":[{"finding_key":key,"disposition":"SELECTED_FOR_REPAIR"} for key in selected_keys],
                         "REPAIR_COMMIT_RANGE":repair_range,"DIFF_BEFORE_REPAIR":before_diff,"DIFF_AFTER_REPAIR":git_diff(worktree) if state["job_type"] != "MULTI_SUBTASK" else git(worktree,"diff","--binary",repair_range),
                         "MACHINE_GATE_RESULTS":state.get("machine_gates",[])}
        head = git(worktree,"rev-parse","HEAD"); status = git(worktree,"status","--porcelain=v1","--untracked-files=all")
        _check_limits(job, state)
        delta, delta_telemetry = assisted("DELTA_REVIEW", delta_package); state["telemetry"].append(delta_telemetry); state["delta_review"] = delta
        if git(worktree,"rev-parse","HEAD") != head or git(worktree,"status","--porcelain=v1","--untracked-files=all") != status:
            state["status"]="INVALID"; state["stop_reason"]="read-only DELTA_REVIEW mutated Git state"
        else:
            if delta["outcome"] == "PASS":
                candidate_path = path.parent / "candidate.json"
                candidate = create_candidate(path=candidate_path, run_id=identifier, repository=str(canonical(Path(state["repository"]))), worktree=str(worktree), candidate_head=git(worktree,"rev-parse","HEAD"), artifact_manifest=None,
                                             review_execution_ids=[str(review_execution_id),str(delta["execution_id"])], check_execution_ids=[str(row["execution_id"]) for row in state.get("machine_gates",[]) if row.get("execution_id")])
                state["candidate"] = {**candidate,"artifact_path":str(candidate_path)}
                state["status"]="WAITING_FOR_HUMAN"
            else:
                state["status"]=delta["outcome"]
    assert_main_unchanged(baseline); state["updated_at"]=now(); state["final_acceptance"]=state["status"]; atomic_json(path,state); return state


def self_test() -> int:
    with tempfile.TemporaryDirectory(prefix="aaw_job_test_") as temp_name:
        root=Path(temp_name); repo=root/"main"; wt=root/"worktree"; repo.mkdir()
        _run(["git","init","-b","main"],repo); _run(["git","config","user.email","aaw@example.invalid"],repo); _run(["git","config","user.name","AAW Test"],repo)
        (repo/"README.md").write_text("baseline\n",encoding="utf-8"); _run(["git","add","."],repo); _run(["git","commit","-m","baseline"],repo)
        _run(["git","worktree","add","-b","aaw/test",str(wt)],repo); main_head=git(repo,"rev-parse","HEAD")
        job={"schema_version":"AAW_CUSTOM_JOB_V0.3","job_id":"self-test","job_type":"MULTI_SUBTASK","goal":"three files","repository":str(repo),"worktree":str(wt),"preprocess_policy":"OFF","preprocess":{},
             "worktree_policy":{"isolated_worktree_required":True,"checkpoint_commits":True,"main_merge_allowed":False,"push_allowed":False},"execution_adapter":"DIRECT_CLI_CONTROL","binding_source":"HUMAN_OVERRIDE","plan":{"enabled":False},
             "subtasks":[{"subtask_id":f"S{i}","title":f"file {i}","instructions":f"create file {i}","profile_id":"TERRA_HIGH","machine_gates":[{"command":[sys.executable,"-c",f"from pathlib import Path; assert Path('S{i}.txt').is_file()"],"timeout_seconds":30}]} for i in range(1,4)],
             "machine_gates":{"final":[{"command":[sys.executable,"-c","from pathlib import Path; assert len(list(Path('.').glob('S*.txt'))) == 3"],"timeout_seconds":30}]},"review":{"profile_id":"SOL_HIGH"},"repair":{"profile_id":"LUNA_HIGH","selection_mode":"HUMAN_SELECTED","max_cycles":1},"delta_review":{"profile_id":"LUNA_HIGH"},
             "limits":{"max_subtasks":10,"max_llm_calls":10,"max_wall_time_minutes":30}}
        job_path=root/"job.json"; job_path.write_text(json.dumps(job),encoding="utf-8")
        sessions: list[str] = []
        def mock(role: str, package: Mapping[str,Any], worktree: Path, binding: Mapping[str,Any]) -> tuple[dict[str,Any],dict[str,Any]]:
            session=f"fresh-{len(sessions)+1}"; sessions.append(session)
            if role=="SUBTASK":
                (worktree/f"{package['SUBTASK_ID']}.txt").write_text(str(package["CURRENT_NODE_CONTRACT"]),encoding="utf-8")
            result={"outcome":"PASS","summary":f"{role} pass","changed_files":[],"tests":[],"findings":[],"remaining_uncertainty":[],"recommended_next_action":"continue","provider_session":session}
            telemetry={"node_type":role,"model":binding["runtime_model_id"],"effort":binding["effort"],"harness":binding["harness"],"provider":binding["provider"],"access_class":binding["access_class"],"binding_source":binding["binding_source"],"provider_session":session,"wall_time_s":0.01,"input_tokens":1,"cached_input":0,"output_tokens":1,"reasoning_tokens":0}
            return result,telemetry
        state=execute(job_path,mock)
        assert state["status"]=="WAITING_FOR_HUMAN" and len(state["checkpoint_commits"])==3
        assert len(set(sessions))==4 and git(repo,"rev-parse","HEAD")==main_head and not git(repo,"status","--porcelain=v1")
        assert len(git(wt,"log","--format=%H",f"{state['baseline_commit']}..HEAD").splitlines())==3
        assert len(state["executions"]) == 8 and len({row["execution_id"] for row in state["executions"]}) == 8
        assert all(row.get("subtask_id") for row in state["executions"] if row["node_id"] in {"S1","S2","S3"})
        assert state["review"]["reviewed_execution_ids"] == [row["execution_id"] for row in state["subtask_results"]]
        assert len(state["commit_records"]) == 3 and all(row["producer_execution_ids"] for row in state["commit_records"])
        assert state["candidate"]["candidate_id"].startswith("CAN_")
        decided = human_verdict(state["AAW_RUN_ID"], "leave-for-later")
        assert decided["human_decisions"][-1]["candidate_id"] == state["candidate"]["candidate_id"]
        repair_wt=root/"repair_worktree"; _run(["git","worktree","add","-b","aaw/repair-test",str(repair_wt)],repo)
        repair_job={**job,"job_id":"repair-self-test","worktree":str(repair_wt),"subtasks":[{"subtask_id":"S1","title":"repair fixture","instructions":"create bad file","profile_id":"TERRA_HIGH","machine_gates":[]}],"machine_gates":{"final":[]}}
        repair_path=root/"repair-job.json"; repair_path.write_text(json.dumps(repair_job),encoding="utf-8")
        repair_sessions: list[str] = []
        def repair_mock(role: str, package: Mapping[str,Any], worktree: Path, binding: Mapping[str,Any]) -> tuple[dict[str,Any],dict[str,Any]]:
            session=f"repair-fresh-{len(repair_sessions)+1}"; repair_sessions.append(session)
            findings=[]; outcome="PASS"
            if role=="SUBTASK": (worktree/"repair.txt").write_text("bad\n",encoding="utf-8")
            elif role=="REVIEW":
                outcome="FAIL"; findings=[{"finding_id":"F001","severity":"P1","commit":git(worktree,"rev-parse","HEAD"),"file":"repair.txt","location":"1","description":"fixture is bad","required_fix":"write good"}]
            elif role=="REPAIR":
                assert [row["finding_id"] for row in package["SELECTED_FINDINGS"]]==["F001"]
                (worktree/"repair.txt").write_text("good\n",encoding="utf-8")
            result={"outcome":outcome,"summary":f"{role} fixture","changed_files":[],"tests":[],"findings":findings,"remaining_uncertainty":[],"recommended_next_action":"continue","provider_session":session}
            telemetry={"node_type":role,"model":binding["runtime_model_id"],"effort":binding["effort"],"harness":binding["harness"],"provider":binding["provider"],"access_class":binding["access_class"],"binding_source":binding["binding_source"],"provider_session":session,"wall_time_s":0.01,"input_tokens":1,"cached_input":0,"output_tokens":1,"reasoning_tokens":0}
            return result,telemetry
        repair_state=execute(repair_path,repair_mock)
        assert repair_state["status"]=="WAITING_FOR_REPAIR_SELECTION"
        repair_state=selected_repair(repair_state["AAW_RUN_ID"],["F001"],repair_mock)
        assert repair_state["status"]=="WAITING_FOR_HUMAN" and repair_state["repair_commit"]
        assert len(git(repair_wt,"log","--format=%H",f"{repair_state['baseline_commit']}..HEAD").splitlines())==2
        assert len(set(repair_sessions))==4 and git(repo,"rev-parse","HEAD")==main_head and not git(repo,"status","--porcelain=v1")
        repair_exec = repair_state["repair_result"]["execution_id"]
        assert repair_state["delta_review"]["repair_execution_id"] == repair_exec
        assert repair_state["delta_review"]["original_review_execution_id"] == repair_state["review"]["execution_id"]
        assert repair_state["candidate"]["candidate_id"].startswith("CAN_")
        print(json.dumps({"status":"PASS","e2e_run_id":state["AAW_RUN_ID"],"e2e_state":str(STATS_ROOT/state["AAW_RUN_ID"]/"CUSTOM_JOB"/"job_state.json"),"repair_fixture_run_id":repair_state["AAW_RUN_ID"],"checkpoint_commits":3,"machine_gate_executions":4,"unique_executions":len(state["executions"]),"fresh_sessions":len(sessions),"selected_repair":"PASS","delta_review":"PASS","repair_commits":2,"repair_fresh_sessions":len(repair_sessions),"main_unchanged":True,"no_merge":True,"no_push":True}))
    return 0


def main(argv: Sequence[str] | None=None) -> int:
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument("--job",type=Path); parser.add_argument("--dry-run",action="store_true"); parser.add_argument("--validate",type=Path); parser.add_argument("--self-test",action="store_true")
    parser.add_argument("--run-id"); parser.add_argument("--human-verdict",choices=("accept","reject","leave-for-later")); parser.add_argument("--select-finding",action="append",default=[]); args=parser.parse_args(argv)
    try:
        if args.self_test: return self_test()
        if args.validate:
            job=load_job(args.validate); print(json.dumps({"status":"VALID","job_id":job["job_id"]},indent=2)); return 0
        if args.select_finding:
            if not args.run_id: raise JobValidationError("--select-finding requires --run-id")
            result=selected_repair(args.run_id,args.select_finding)
        elif args.human_verdict:
            if not args.run_id: raise JobValidationError("--human-verdict requires --run-id")
            result=human_verdict(args.run_id,args.human_verdict)
        else:
            if not args.job: raise JobValidationError("--job is required")
            result=dry_run(args.job) if args.dry_run else execute(args.job)
        print(json.dumps(result,ensure_ascii=True,indent=2)); return 0 if result.get("status") in {"DRY_RUN_READY","WAITING_FOR_HUMAN","WAITING_FOR_PLAN_APPROVAL","WAITING_FOR_REPAIR_SELECTION","READY_FOR_EXTERNAL_INTEGRATION","REJECTED"} else 20
    except (JobValidationError,CatalogError,WorkflowStop,OSError,json.JSONDecodeError) as exc:
        status=exc.status if isinstance(exc,WorkflowStop) else "INVALID"; print(json.dumps({"status":status,"error":str(exc)},ensure_ascii=True,indent=2)); return 21 if status=="BLOCKED" else 22


if __name__=="__main__": raise SystemExit(main())
