#!/usr/bin/env python3
"""AAW V0.4A invocation identity and minimum evidence primitives."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = "AAW_EXECUTION_DESCRIPTOR_V0.4A"
INVOCATION_KINDS = {"LLM", "MACHINE_GATE", "PREPROCESS", "REVIEW", "REPAIR", "DELTA_REVIEW", "PLAN"}


class ExecutionIdentityError(RuntimeError):
    """Fail-closed execution-identity violation.

    Raised when an execution descriptor already exists for an allocated
    ``execution_id``. The existing identity is never reused, overwritten or
    merged. This is a data-integrity fault, not a retryable operational error;
    machine callers classify it via ``classification`` (or the
    ``DATA_INTEGRITY_ERROR`` token in the message, matching the analytics helper).
    """

    classification = "DATA_INTEGRITY_ERROR"

    def __init__(self, message: str, *, execution_id: str | None = None, path: Path | None = None) -> None:
        super().__init__(message)
        self.execution_id = execution_id
        self.path = str(path) if path is not None else None


def now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="milliseconds")


def new_execution_id() -> str:
    """Provider-independent, collision-safe identity for exactly one invocation."""
    return "EXE_" + uuid.uuid4().hex


def new_preprocess_id() -> str:
    return "PRE_" + uuid.uuid4().hex


def new_candidate_id() -> str:
    return "CAN_" + uuid.uuid4().hex


def new_human_decision_id() -> str:
    return "HDE_" + uuid.uuid4().hex


def canonical_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def allocate_execution(
    *, descriptor_root: Path, run_id: str, node_id: str, invocation_kind: str,
    subtask_id: str | None = None, provider: str | None = None,
    harness: str | None = None, model: str | None = None, effort: str | None = None,
    profile: str | None = None, provider_session_id: str | None = None,
    input_contract_hash: str | None = None, selection_reason: str | None = None,
    policy_version: str | None = None, fixture_class: str | None = None,
    retry_of_execution_id: str | None = None, relations: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], Path]:
    kind = invocation_kind.upper()
    if kind not in INVOCATION_KINDS:
        raise ValueError(f"unsupported invocation_kind: {invocation_kind!r}")
    if not run_id or not node_id:
        raise ValueError("run_id and node_id are required")
    execution_id = new_execution_id()
    descriptor: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "execution_id": execution_id,
        "run_id": run_id,
        "node_id": node_id,
        "subtask_id": subtask_id,
        "invocation_kind": kind,
        "provider": provider,
        "harness": harness,
        "model": model,
        "effort": effort,
        "profile": profile,
        "provider_session_id": provider_session_id,
        "created_at": now(),
        "input_contract_hash": input_contract_hash,
        "selection_reason": selection_reason,
        "policy_version": policy_version,
        "fixture_class": fixture_class,
        "retry_of_execution_id": retry_of_execution_id,
        "relations": dict(relations or {}),
        "status": "RESERVED",
    }
    path = descriptor_root / f"{execution_id}.json"
    if path.exists():
        raise ExecutionIdentityError(
            f"DATA_INTEGRITY_ERROR execution descriptor already exists for {execution_id}; "
            f"existing execution identity must not be reused, overwritten or merged: {path}",
            execution_id=execution_id,
            path=path,
        )
    atomic_json(path, descriptor)
    return descriptor, path


def update_execution(path: Path, execution_id: str, **changes: Any) -> dict[str, Any]:
    current = json.loads(path.read_text(encoding="utf-8"))
    if current.get("execution_id") != execution_id:
        raise ValueError("execution descriptor identity mismatch")
    immutable = {"schema_version", "execution_id", "run_id", "node_id", "subtask_id", "invocation_kind", "created_at"}
    if immutable.intersection(changes):
        raise ValueError("immutable execution descriptor field update")
    current.update(changes)
    atomic_json(path, current)
    return current


def execution_ref(descriptor: Mapping[str, Any], path: Path) -> dict[str, Any]:
    """Small state-file reference; the descriptor remains the full authority."""
    return {
        "execution_id": descriptor["execution_id"], "node_id": descriptor["node_id"],
        "subtask_id": descriptor.get("subtask_id"), "invocation_kind": descriptor["invocation_kind"],
        "provider": descriptor.get("provider"), "harness": descriptor.get("harness"),
        "model": descriptor.get("model"), "effort": descriptor.get("effort"),
        "profile": descriptor.get("profile"), "created_at": descriptor["created_at"],
        "retry_of_execution_id": descriptor.get("retry_of_execution_id"),
        "descriptor_path": str(path),
    }


def create_candidate(
    *, path: Path, run_id: str, repository: str, worktree: str,
    candidate_head: str | None, artifact_manifest: Mapping[str, Any] | None,
    review_execution_ids: list[str], check_execution_ids: list[str],
) -> dict[str, Any]:
    candidate = {
        "schema_version": "AAW_CANDIDATE_V0.4A", "candidate_id": new_candidate_id(),
        "run_id": run_id, "repository": repository, "worktree": worktree,
        "candidate_head": candidate_head, "artifact_manifest": dict(artifact_manifest or {}) or None,
        "review_execution_ids": list(review_execution_ids), "check_execution_ids": list(check_execution_ids),
        "created_at": now(),
    }
    candidate["content_identity_hash"] = canonical_hash({k: v for k, v in candidate.items() if k not in {"candidate_id", "created_at"}})
    if path.exists():
        raise FileExistsError(f"candidate artifact already exists: {path}")
    atomic_json(path, candidate)
    return candidate


def create_human_decision(*, path: Path, candidate_id: str, verdict: str, quality_assessment: str | None = None, reason: str | None = None, human_decision_id: str | None = None) -> dict[str, Any]:
    decision = {
        "schema_version": "AAW_HUMAN_DECISION_V0.4A", "human_decision_id": human_decision_id or new_human_decision_id(),
        "candidate_id": candidate_id, "verdict": verdict, "timestamp": now(),
        "quality_assessment": quality_assessment, "reason": reason,
    }
    if path.exists():
        raise FileExistsError(f"human decision artifact already exists: {path}")
    atomic_json(path, decision)
    return decision
