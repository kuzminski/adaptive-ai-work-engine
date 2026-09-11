#!/usr/bin/env python3
"""Non-authoritative, deterministic-first local Qwen preprocessing for AAW.

This module is deliberately an execution-support layer, not a Playbook node.
It never returns a gate outcome and it never replaces raw evidence: every
successful compact artifact retains source snapshots and references.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

import local_llm_adapter as local_llm
import process_observation
from execution_contract import (
    allocate_execution, atomic_json, canonical_hash, new_preprocess_id, update_execution,
)
from execution_ledger import ExecutionLedger, LedgerError, LifecycleRecorder, ledger_beside_descriptors


SCHEMA_VERSION = "AAW_LOCAL_QWEN_PREPROCESS_V0.4A"
POLICIES = {"OFF", "AUTO_SAFE", "MANUAL", "CUSTOM"}
TYPES = {
    "LOCAL_QWEN_TASK_NORMALIZE", "LOCAL_QWEN_SUMMARY", "LOCAL_QWEN_JSON",
    "LOCAL_QWEN_LOG_TRIAGE", "LOCAL_QWEN_DIFF_TRIAGE", "LOCAL_QWEN_FINDINGS_PREP",
    "LOCAL_QWEN_DELTA_ASSIST",
}
TYPE_PROFILE = {
    "LOCAL_QWEN_TASK_NORMALIZE": "LOCAL_QWEN_JSON",
    "LOCAL_QWEN_SUMMARY": "LOCAL_QWEN_SUMMARY",
    "LOCAL_QWEN_JSON": "LOCAL_QWEN_JSON",
    "LOCAL_QWEN_LOG_TRIAGE": "LOCAL_QWEN_LOG_TRIAGE",
    "LOCAL_QWEN_DIFF_TRIAGE": "LOCAL_QWEN_DIFF_TRIAGE",
    "LOCAL_QWEN_FINDINGS_PREP": "LOCAL_QWEN_FINDINGS_PREP",
    "LOCAL_QWEN_DELTA_ASSIST": "LOCAL_QWEN_DELTA",
}
NODE_TYPES = {
    "PLAN": {"LOCAL_QWEN_TASK_NORMALIZE", "LOCAL_QWEN_SUMMARY"},
    "IMPLEMENT": {"LOCAL_QWEN_TASK_NORMALIZE", "LOCAL_QWEN_SUMMARY"},
    "SUBTASK": {"LOCAL_QWEN_TASK_NORMALIZE", "LOCAL_QWEN_SUMMARY"},
    "REVIEW": {"LOCAL_QWEN_SUMMARY", "LOCAL_QWEN_DIFF_TRIAGE"},
    "REPAIR": {"LOCAL_QWEN_LOG_TRIAGE", "LOCAL_QWEN_FINDINGS_PREP"},
    "DELTA_REVIEW": {"LOCAL_QWEN_DELTA_ASSIST"},
}
# Conservative configurable defaults.  They are policy thresholds, not claims
# about exact token counts.
DEFAULT_THRESHOLDS = {
    "task_chars": 4000, "handoff_chars": 9000, "previous_artifacts": 3,
    "log_chars": 12000, "diff_chars": 16000, "diff_max_chars": 30000,
    "findings": 5, "source_excerpt_chars": 26000,
}


def now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="milliseconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def _safe(value: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in value)[:64]


def deterministic_log_extract(text: str, *, tail_lines: int = 160) -> dict[str, Any]:
    """Keep mechanical facts deterministic before any optional LLM call."""
    lines = text.splitlines()
    patterns = re.compile(r"(?:error|failed|failure|traceback|exception|assert|^E\s)", re.I)
    selected = [(index + 1, line) for index, line in enumerate(lines) if patterns.search(line)]
    tests = sorted(set(re.findall(r"(?:FAILED|ERROR)\s+([^\s]+)|\b([\w./:-]+::[\w\[\]-]+)", text)))
    names = ["".join(pair) for pair in tests if "".join(pair)]
    return {
        "line_count": len(lines), "error_lines": [{"line": n, "text": line[:500]} for n, line in selected[-80:]],
        "failing_test_candidates": names[-40:], "tail": "\n".join(lines[-tail_lines:]),
    }


def select_auto_type(node_type: str, package: Mapping[str, Any], thresholds: Mapping[str, int]) -> tuple[str | None, str | None]:
    """Policy is intentionally deterministic and conservative."""
    node_type = node_type.upper()
    serialized = _json(package)
    if node_type in {"PLAN", "IMPLEMENT", "SUBTASK"} and len(str(package.get("GOAL", ""))) >= thresholds["task_chars"]:
        return "LOCAL_QWEN_TASK_NORMALIZE", "INPUT_TOO_LARGE"
    previous = package.get("PREVIOUS_NODE_RESULTS") or package.get("PREVIOUS_STRUCTURED_RESULTS") or package.get("SUBTASK_RESULTS") or []
    if node_type in {"PLAN", "IMPLEMENT", "SUBTASK", "REVIEW"} and isinstance(previous, list) and len(previous) >= thresholds["previous_artifacts"]:
        return "LOCAL_QWEN_SUMMARY", "MANY_PREVIOUS_RESULTS"
    if node_type == "REVIEW":
        diff = str(package.get("GIT_DIFF", package.get("GIT_DIFF_BASELINE_TO_HEAD", "")))
        commits = package.get("COMMIT_LIST", [])
        if thresholds["diff_chars"] <= len(diff) <= thresholds["diff_max_chars"] and (len(commits) > 1 or len(diff) >= thresholds["diff_chars"]):
            return "LOCAL_QWEN_DIFF_TRIAGE", "MULTI_COMMIT_REVIEW"
        if len(serialized) >= thresholds["handoff_chars"]:
            return "LOCAL_QWEN_SUMMARY", "INPUT_TOO_LARGE"
    if node_type == "REPAIR":
        logs = str(package.get("RAW_LOG", ""))
        findings = package.get("SELECTED_FINDINGS") or package.get("OPEN_ISSUES") or []
        if len(logs) >= thresholds["log_chars"]:
            return "LOCAL_QWEN_LOG_TRIAGE", "LONG_MACHINE_LOG"
        if isinstance(findings, list) and len(findings) >= thresholds["findings"]:
            return "LOCAL_QWEN_FINDINGS_PREP", "MANY_FINDINGS"
    if node_type == "DELTA_REVIEW" and len(str(package.get("REPAIR_DIFF", ""))) >= thresholds["diff_chars"]:
        return "LOCAL_QWEN_DELTA_ASSIST", "MULTI_COMMIT_REVIEW"
    return None, None


def _source_refs(source_path: Path, payload: str) -> list[dict[str, Any]]:
    return [{"source_artifact": "preprocess_source_snapshot", "source_path": str(source_path), "source_hash": _sha(payload)}]


def _instruction(preprocess_type: str) -> str:
    contracts = {
        "LOCAL_QWEN_TASK_NORMALIZE": "goal, constraints, explicit_non_goals, acceptance_candidates, mentioned_files, mentioned_components, uncertainties",
        "LOCAL_QWEN_SUMMARY": "goal, completed_work, changed_files, tests, known_findings, remaining_uncertainty, important_references",
        "LOCAL_QWEN_JSON": "normalized_data, uncertainties, important_references",
        "LOCAL_QWEN_LOG_TRIAGE": "failure_classes, affected_tests, likely_locations, repeated_errors, important_log_ranges, uncertainty, hypotheses",
        "LOCAL_QWEN_DIFF_TRIAGE": "changed_components, high_risk_areas, mechanical_changes, semantic_changes, suggested_review_focus, references",
        "LOCAL_QWEN_FINDINGS_PREP": "findings (finding_id, severity, file, location, required_fix, dependencies, possible_overlap), uncertainty",
        "LOCAL_QWEN_DELTA_ASSIST": "selected_findings_touched, unexpected_files_changed, obvious_scope_expansion, candidate_unresolved_findings, uncertainty",
    }
    return (
        "You are a local AAW preprocessing assistant. Return exactly one JSON object with these fields: "
        + contracts[preprocess_type]
        + ". This is ADVISORY only. Do not declare PASS/FAIL, solve findings, change severity as fact, change goals, "
        "acceptance criteria, model bindings, or scope. Preserve uncertainty explicitly; hypotheses are hypotheses."
    )


def preprocess_for_node(
    *, run_id: str, node_id: str, node_type: str, package: Mapping[str, Any], artifact_root: Path,
    policy: str = "AUTO_SAFE", requested_type: str | None = None, required: bool = False,
    reason: str | None = None, thresholds: Mapping[str, int] | None = None,
    downstream_execution_id: str | None = None, execution_root: Path | None = None,
) -> dict[str, Any]:
    """Create an advisory artifact or an explicit skip/block record.

    `MANUAL`/`CUSTOM` with `required=True` fail closed when local runtime is not
    usable. AUTO_SAFE never blocks an otherwise runnable frontier node.
    """
    merged = {**DEFAULT_THRESHOLDS, **dict(thresholds or {})}
    policy = policy.upper()
    if policy not in POLICIES:
        raise ValueError(f"unknown preprocess policy: {policy}")
    if requested_type and requested_type not in TYPES:
        raise ValueError(f"unknown preprocess type: {requested_type}")
    chosen, auto_reason = (requested_type, reason or "MANUAL_USER_SELECTION") if requested_type else select_auto_type(node_type, package, merged)
    status = "SKIPPED_NOT_USEFUL"
    if policy == "OFF":
        status, chosen, auto_reason = "SKIPPED_POLICY_OFF", None, None
    elif policy == "AUTO_SAFE" and requested_type is None and not chosen:
        chosen, auto_reason = None, None
    elif policy in {"MANUAL", "CUSTOM"} and not chosen:
        status = "SKIPPED_NOT_USEFUL"
    if chosen and chosen not in NODE_TYPES.get(node_type.upper(), set()):
        raise ValueError(f"{chosen} is not eligible for {node_type}")

    artifact_root.mkdir(parents=True, exist_ok=True)
    preprocess_id = new_preprocess_id()
    stamp = dt.datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")
    base = artifact_root / f"{_safe(node_id)}__{_safe(chosen or 'SKIP')}__{stamp}"
    source_text = _json(package)
    source_path = base.with_name(base.name + "__sources.json")
    atomic_json(source_path, dict(package))
    artifact: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION, "run_id": run_id, "node_id": node_id, "node_type": node_type,
        "preprocess_id": preprocess_id, "execution_id": None,
        "preprocess_type": chosen, "profile": TYPE_PROFILE.get(chosen or ""), "provider": "LOCAL",
        "model": getattr(local_llm, "LOCAL_MODEL_ID", "qwen3-vl:4b-instruct"), "created_at": now(),
        "reason": auto_reason, "source_artifacts": ["preprocess_source_snapshot"],
        "source_paths": [str(source_path)], "source_hashes": {str(source_path): _sha(source_text)},
        "source_refs": _source_refs(source_path, source_text), "status": status,
        "authority": "ADVISORY_SUPPORTING_ARTIFACT", "output": None, "output_artifact_hash": None,
        "downstream_execution_id": downstream_execution_id, "consumed": False,
        "telemetry": {"input_chars": 0, "input_tokens": None, "output_chars": 0, "output_tokens": None,
                      "wall_time_s": 0.0, "source_artifact_count": 1, "downstream_node_id": node_id,
                      "downstream_input_before_estimate": len(source_text), "downstream_input_after_estimate": None},
    }
    if not chosen:
        path = base.with_suffix(".json"); atomic_json(path, artifact); artifact["artifact_path"] = str(path); return artifact

    excerpt = source_text[:int(merged["source_excerpt_chars"])]
    if chosen == "LOCAL_QWEN_LOG_TRIAGE":
        log = str(package.get("RAW_LOG", source_text)); excerpt = _json({"deterministic_extraction": deterministic_log_extract(log), "raw_log_path": package.get("RAW_LOG_PATH"), "source_refs": artifact["source_refs"]})
    if len(excerpt) < 1:
        artifact["status"] = "SKIPPED_NOT_USEFUL"
        path = base.with_suffix(".json"); atomic_json(path, artifact); artifact["artifact_path"] = str(path); return artifact
    check = local_llm.precheck_soft()
    if not check.get("ok"):
        artifact["status"] = "BLOCKED" if required and policy in {"MANUAL", "CUSTOM"} else "SKIPPED_LOCAL_UNAVAILABLE"
        artifact["failure_code"] = "LOCAL_LLM_UNSAFE_BIND" if check.get("state") == "LOCAL_LLM_UNSAFE_BIND" else "LOCAL_LLM_UNAVAILABLE"
        artifact["failure_reason"] = check.get("reason")
        path = base.with_suffix(".json"); atomic_json(path, artifact); artifact["artifact_path"] = str(path); return artifact
    descriptor, descriptor_path = allocate_execution(
        descriptor_root=execution_root or artifact_root.parent / "EXECUTIONS",
        run_id=run_id, node_id=f"{node_id}:PREPROCESS", invocation_kind="PREPROCESS",
        provider="LOCAL", harness="ollama_openai_compat", model=getattr(local_llm, "LOCAL_MODEL_ID", None),
        profile=TYPE_PROFILE.get(chosen or ""), input_contract_hash=canonical_hash(package),
        selection_reason=auto_reason, policy_version=SCHEMA_VERSION,
        relations={"preprocess_id": preprocess_id, "downstream_node_id": node_id,
                   "downstream_execution_id": downstream_execution_id},
    )
    execution_id = str(descriptor["execution_id"])
    artifact["execution_id"] = execution_id
    path = base.with_suffix(".json")
    artifact["status"] = "EXECUTION_RESERVED"
    atomic_json(path, artifact)
    # An invoked preprocess is an execution, so it takes the generic lifecycle
    # events. A skipped decision has no execution ID and no ledger event.
    ledger = ExecutionLedger(ledger_beside_descriptors(descriptor_path.parent), run_id)
    recorder = LifecycleRecorder(ledger, execution_id)
    try:
        ledger.record_execution_intent(
            execution_id=execution_id, node_id=f"{node_id}:PREPROCESS", invocation_kind="PREPROCESS",
            descriptor_path=descriptor_path, provider="LOCAL", harness="ollama_openai_compat",
            model=getattr(local_llm, "LOCAL_MODEL_ID", None), profile=TYPE_PROFILE.get(chosen or ""),
            input_contract_hash=canonical_hash(package),
            extra={"preprocess_id": preprocess_id, "downstream_node_id": node_id,
                   "downstream_execution_id": downstream_execution_id},
        )
    except LedgerError as exc:
        # Fail closed: no durable intent, no inference. The reserved descriptor
        # stays unresolved evidence and is never reused.
        artifact["status"] = "BLOCKED" if required and policy in {"MANUAL", "CUSTOM"} else "SKIPPED_LOCAL_UNAVAILABLE"
        artifact["failure_code"] = getattr(exc, "classification", "LEDGER_WRITE_ERROR")
        artifact["failure_reason"] = str(exc)
        update_execution(descriptor_path, execution_id, status=artifact["status"])
        atomic_json(path, artifact); artifact["artifact_path"] = str(path); return artifact
    started = time.monotonic()
    # AAW owns no process here; the strongest observed boundary is that the
    # request was dispatched after a successful local availability precheck.
    recorder.observe_start(process_observation.http_receipt(
        endpoint=getattr(local_llm, "ENDPOINT", "http://127.0.0.1:11434"),
        adapter="ollama_openai_compat", provider="LOCAL",
        model=getattr(local_llm, "LOCAL_MODEL_ID", None)))
    try:
        response = local_llm.chat_json([
            {"role": "system", "content": _instruction(chosen)},
            {"role": "user", "content": "SOURCE SNAPSHOT (possibly bounded; original path is retained):\n" + excerpt},
        ], max_output_tokens=1200, execution_id=execution_id)
        output = response.get("json")
        if not isinstance(output, Mapping):
            raise local_llm.LocalLLMProtocolError("preprocessor output is not a JSON object")
        artifact["status"] = "COMPLETED_ADVISORY"; artifact["output"] = dict(output)
        artifact["output_artifact_hash"] = canonical_hash(output)
        usage = response.get("usage") or {}
        artifact["telemetry"].update({"input_chars": len(excerpt), "input_tokens": usage.get("input_tokens"), "output_chars": len(response.get("content", "")), "output_tokens": usage.get("output_tokens"), "wall_time_s": round(time.monotonic() - started, 3), "downstream_input_after_estimate": len(_json(output)) + 1000})
    except local_llm.LocalLLMError as exc:
        artifact["status"] = "BLOCKED" if required and policy in {"MANUAL", "CUSTOM"} else "SKIPPED_LOCAL_UNAVAILABLE"
        artifact["failure_code"] = getattr(exc, "code", "LOCAL_LLM_UNAVAILABLE"); artifact["failure_reason"] = str(exc)
    update_execution(descriptor_path, execution_id, status="COMPLETED" if artifact["status"] == "COMPLETED_ADVISORY" else artifact["status"])
    completed = artifact["status"] == "COMPLETED_ADVISORY"
    recorder.close(
        close_reason="COMPLETED" if completed else "FAILED",
        # A dispatched local request whose call failed leaves server-side state
        # unknown, so the effect is PARTIAL rather than CONFIRMED.
        effect_certainty="CONFIRMED" if completed else "PARTIAL",
        observation_source="ADAPTER_RESPONSE", outcome=artifact["status"],
        detail=artifact.get("failure_reason"),
        result_refs={"preprocess_artifact": str(path), "output_artifact_hash": artifact.get("output_artifact_hash")},
        extra={"preprocess_id": preprocess_id, "downstream_execution_id": downstream_execution_id},
    )
    atomic_json(path, artifact)
    artifact["artifact_path"] = str(path)
    artifact["lifecycle_record"] = recorder.status()
    return artifact


def compact_package(package: Mapping[str, Any], artifact: Mapping[str, Any]) -> dict[str, Any]:
    """Use compact output initially while keeping original evidence reachable."""
    if artifact.get("status") != "COMPLETED_ADVISORY":
        return dict(package)
    compact = dict(package)
    for key in ("PREVIOUS_NODE_RESULTS", "PREVIOUS_STRUCTURED_RESULTS", "SUBTASK_RESULTS", "GIT_DIFF", "GIT_DIFF_BASELINE_TO_HEAD", "UNCOMMITTED_DIFF", "RAW_LOG"):
        compact.pop(key, None)
    compact["LOCAL_PREPROCESS"] = {"artifact_path": artifact.get("artifact_path"), "preprocess_type": artifact.get("preprocess_type"), "output": artifact.get("output"), "source_refs": artifact.get("source_refs"), "instruction": "Local output is advisory. Inspect original sources by path or Git range if detail is needed."}
    path_value = artifact.get("artifact_path")
    if path_value and Path(str(path_value)).is_file():
        path = Path(str(path_value))
        persisted = json.loads(path.read_text(encoding="utf-8"))
        persisted["consumed"] = True
        atomic_json(path, persisted)
        if isinstance(artifact, dict):
            artifact["consumed"] = True
    return compact


def self_test() -> int:
    assert select_auto_type("REVIEW", {"GIT_DIFF": "x" * 17000, "COMMIT_LIST": ["a", "b"]}, DEFAULT_THRESHOLDS)[0] == "LOCAL_QWEN_DIFF_TRIAGE"
    assert select_auto_type("REPAIR", {"RAW_LOG": "x" * 13000}, DEFAULT_THRESHOLDS)[0] == "LOCAL_QWEN_LOG_TRIAGE"
    assert select_auto_type("REVIEW", {"GIT_DIFF": "short"}, DEFAULT_THRESHOLDS)[0] is None
    log = deterministic_log_extract("ok\nFAILED tests/test_x.py::test_y\nTraceback boom")
    assert log["error_lines"] and log["failing_test_candidates"]
    print(json.dumps({"status": "PASS", "thresholds": DEFAULT_THRESHOLDS, "types": len(TYPES)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(self_test())
