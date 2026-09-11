#!/usr/bin/env python3
"""AAW V0.4B minimal append-only execution lifecycle ledger.

The ledger records *observed lifecycle facts* about invocations. It is
deliberately not event sourcing, not execution authority, and not a place from
which workflow state is reconstructed. Raw artifacts (execution descriptors,
node results, state files, Git, decision artifacts) remain authoritative for
semantic workflow state.

Scope of this module: event schema validation, event ID generation, append,
safe per-run sequencing, durable flush, read, damaged-tail detection,
duplicate detection, and simple lifecycle validation/query helpers. Nothing
else belongs here.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from aaw_paths import STATS_ROOT


SCHEMA_VERSION = "AAW_EXECUTION_LEDGER_V0.4B"
IDENTITY_CONTRACT = "AAW_EXECUTION_DESCRIPTOR_V0.4A"
LEDGER_DIRNAME = "LEDGER"
LEDGER_FILENAME = "execution_events.jsonl"
EXECUTION_INTENT = "EXECUTION_INTENT"
EXECUTION_STARTED = "EXECUTION_STARTED"
EXECUTION_CLOSED = "EXECUTION_CLOSED"
COMMIT_RECORDED = "COMMIT_RECORDED"
HUMAN_DECISION_RECORDED = "HUMAN_DECISION_RECORDED"

EVENT_TYPES = (
    EXECUTION_INTENT, EXECUTION_STARTED, EXECUTION_CLOSED,
    COMMIT_RECORDED, HUMAN_DECISION_RECORDED,
)
EXECUTION_EVENT_TYPES = (EXECUTION_INTENT, EXECUTION_STARTED, EXECUTION_CLOSED)

CLOSE_REASONS = ("COMPLETED", "FAILED", "TIMEOUT", "CANCELLED", "INTERRUPTED", "RECONCILED", "UNKNOWN")
EFFECT_CERTAINTY = ("CONFIRMED", "PARTIAL", "UNKNOWN")
OBSERVATION_SOURCES = (
    "CHILD_PROCESS_EXIT", "ADAPTER_RESPONSE", "IN_PROCESS_ADAPTER_RETURN", "RUNNER_EXCEPTION",
    "SPAWN_FAILURE", "PRE_DISPATCH_FAILURE", "RECONCILIATION", "LEDGER_UNCERTAIN", "UNKNOWN",
)
# A close without an observed start is only honest in three situations: start
# observation was lost and reconciliation supplied the terminal fact; the
# invocation provably never began; or the adapter owns no process and therefore
# exposes no observable start boundary at all. Anything else — in particular a
# process execution whose start went unrecorded — stays a validation error.
CLOSE_WITHOUT_START_SOURCES = (
    "RECONCILIATION", "SPAWN_FAILURE", "PRE_DISPATCH_FAILURE", "IN_PROCESS_ADAPTER_RETURN",
)

# Bounded write statuses returned by the non-raising helpers.
RECORDED = "RECORDED"
NOT_OBSERVED = "NOT_OBSERVED"
STARTED_LEDGER_UNCERTAIN = "EXECUTION_STARTED_LEDGER_UNCERTAIN"
CLOSE_RECORD_UNCERTAIN = "CLOSE_RECORD_UNCERTAIN"


class LedgerError(RuntimeError):
    classification = "LEDGER_ERROR"

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.context = dict(context)


class LedgerWriteError(LedgerError):
    """Durable append failed. For EXECUTION_INTENT this is fail-closed."""

    classification = "LEDGER_WRITE_ERROR"


class LedgerIntegrityError(LedgerError):
    """Incompatible duplicate. Never resolved by last-write-wins."""

    classification = "DATA_INTEGRITY_ERROR"


class LedgerDispatchError(LedgerError):
    """A second dispatch was attempted for an execution ID that already has intent."""

    classification = "AT_MOST_ONCE_DISPATCH_VIOLATION"


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------

def now() -> str:
    """Diagnostic wall-clock time. Never identity, never semantic ordering."""
    return dt.datetime.now().astimezone().isoformat(timespec="milliseconds")


def new_event_id() -> str:
    """Collision-safe, locally generated. Never derived from a timestamp."""
    return "EVT_" + uuid.uuid4().hex


def canonical_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def file_hash(path: Path | str | None) -> str | None:
    if path is None:
        return None
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def ledger_path_for_run(run_id: str, stats_root: Path | str = STATS_ROOT) -> Path:
    return Path(stats_root) / str(run_id) / LEDGER_DIRNAME / LEDGER_FILENAME


def ledger_beside_descriptors(descriptor_root: Path | str) -> Path:
    """Ledger of the run whose execution descriptors live in ``descriptor_root``.

    Used by callers that receive a descriptor root rather than a stats root, so
    the ledger stays inside the same run directory either way.
    """
    return Path(descriptor_root).parent / LEDGER_DIRNAME / LEDGER_FILENAME


# ---------------------------------------------------------------------------
# Minimal per-run lock
# ---------------------------------------------------------------------------

_PROCESS_LOCKS: dict[str, threading.RLock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


def _process_lock(path: Path) -> threading.RLock:
    key = str(path)
    with _PROCESS_LOCKS_GUARD:
        lock = _PROCESS_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PROCESS_LOCKS[key] = lock
        return lock


class _LedgerLock:
    """Per-run advisory lock.

    AAW currently runs one ledger writer per run. This lock enforces that
    assumption instead of trusting it: a second writer waits rather than racing
    sequence allocation. It is intentionally one local file lock, not
    distributed locking.
    """

    def __init__(self, path: Path) -> None:
        self._lock_path = path.with_name(path.name + ".lock")
        self._thread_lock = _process_lock(path)
        self._fd: int | None = None

    def __enter__(self) -> "_LedgerLock":
        self._thread_lock.acquire()
        try:
            self._lock_path.parent.mkdir(parents=True, exist_ok=True)
            self._fd = os.open(str(self._lock_path), os.O_RDWR | os.O_CREAT)
            self._acquire_os_lock()
        except OSError as exc:
            self._release_fd()
            self._thread_lock.release()
            raise LedgerWriteError(f"cannot acquire ledger lock: {exc}", path=str(self._lock_path)) from exc
        return self

    def _acquire_os_lock(self) -> None:
        assert self._fd is not None
        if sys.platform == "win32":
            import msvcrt

            os.lseek(self._fd, 0, os.SEEK_SET)
            msvcrt.locking(self._fd, msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(self._fd, fcntl.LOCK_EX)

    def _release_os_lock(self) -> None:
        if self._fd is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                os.lseek(self._fd, 0, os.SEEK_SET)
                msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:
            pass

    def _release_fd(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

    def __exit__(self, *_exc: Any) -> None:
        self._release_os_lock()
        self._release_fd()
        self._thread_lock.release()


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

class LedgerRead:
    """Result of reading one run ledger.

    ``events`` holds every valid event read. Damaged content is reported
    separately, never guessed at, and never repaired during a read.
    """

    __slots__ = ("path", "events", "diagnostics", "exists", "ends_with_newline", "size")

    def __init__(self, path: Path, events: list[dict[str, Any]], diagnostics: list[dict[str, Any]],
                 exists: bool, ends_with_newline: bool, size: int) -> None:
        self.path = path
        self.events = events
        self.diagnostics = diagnostics
        self.exists = exists
        self.ends_with_newline = ends_with_newline
        self.size = size

    @property
    def damaged_tail(self) -> dict[str, Any] | None:
        return next((row for row in self.diagnostics if row["kind"] == "LEDGER_DAMAGED_TAIL"), None)

    @property
    def last_sequence(self) -> int:
        return max((int(row.get("sequence") or 0) for row in self.events
                    if isinstance(row.get("sequence"), int) and not isinstance(row.get("sequence"), bool)),
                   default=0)

    def by_type(self, event_type: str) -> list[dict[str, Any]]:
        return [row for row in self.events if row.get("event_type") == event_type]

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path), "exists": self.exists, "events": len(self.events),
            "diagnostics": list(self.diagnostics), "last_sequence": self.last_sequence,
        }


def _scan(path: Path) -> LedgerRead:
    """Parse a ledger file. A damaged tail never invalidates earlier events."""
    if not path.is_file():
        return LedgerRead(path, [], [], False, True, 0)
    raw = path.read_bytes()
    size = len(raw)
    text = raw.decode("utf-8", errors="replace")
    ends_with_newline = text.endswith("\n") if text else True
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    events: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    total = len(lines)
    for index, line in enumerate(lines, 1):
        stripped = line.strip()
        unterminated_final = index == total and not ends_with_newline
        if not stripped:
            diagnostics.append({"kind": "LEDGER_BLANK_RECORD", "line": index, "detail": "blank ledger line"})
            continue
        try:
            row = json.loads(stripped)
        except json.JSONDecodeError as exc:
            kind = "LEDGER_DAMAGED_TAIL" if unterminated_final else "LEDGER_DAMAGED_RECORD"
            diagnostics.append({
                "kind": kind, "line": index, "bytes": len(line),
                "detail": f"{type(exc).__name__}: {exc}",
                "note": "partial content is retained verbatim and is never interpreted",
            })
            continue
        if not isinstance(row, Mapping):
            diagnostics.append({"kind": "MALFORMED_EVENT", "line": index, "detail": "event is not a JSON object"})
            continue
        if unterminated_final:
            # Parsed, but the writer never confirmed the terminating newline.
            diagnostics.append({
                "kind": "LEDGER_UNTERMINATED_FINAL_RECORD", "line": index,
                "detail": "final record parsed but has no terminating newline",
            })
        events.append(dict(row))
    return LedgerRead(path, events, diagnostics, True, ends_with_newline, size)


# ---------------------------------------------------------------------------
# Validation primitives
# ---------------------------------------------------------------------------

def _require(condition: bool, message: str, **context: Any) -> None:
    if not condition:
        raise LedgerError(message, **context)


def _validate_envelope(event: Mapping[str, Any], run_id: str) -> None:
    _require(event.get("schema_version") == SCHEMA_VERSION,
             f"unknown ledger schema_version: {event.get('schema_version')!r}")
    event_id = event.get("event_id")
    _require(isinstance(event_id, str) and event_id.startswith("EVT_") and len(event_id) == 36,
             f"invalid event_id: {event_id!r}")
    _require(event.get("event_type") in EVENT_TYPES, f"unsupported event_type: {event.get('event_type')!r}")
    _require(event.get("run_id") == run_id, "event run_id does not match ledger run_id")
    sequence = event.get("sequence")
    _require(isinstance(sequence, int) and not isinstance(sequence, bool) and sequence >= 1,
             "sequence must be a positive integer")
    _require(isinstance(event.get("recorded_at"), str) and bool(event["recorded_at"]), "recorded_at is required")
    _require(isinstance(event.get("payload"), Mapping), "payload must be an object")
    if event["event_type"] in EXECUTION_EVENT_TYPES:
        execution_id = event.get("execution_id")
        _require(isinstance(execution_id, str) and execution_id.startswith("EXE_"),
                 f"{event['event_type']} requires an execution_id", event_type=event["event_type"])


def _validate_payload(event_type: str, payload: Mapping[str, Any]) -> None:
    if event_type == EXECUTION_STARTED:
        _require(bool(payload.get("start_evidence")), "EXECUTION_STARTED requires start_evidence")
        _require(bool(payload.get("observed_start_time")), "EXECUTION_STARTED requires observed_start_time")
    elif event_type == EXECUTION_CLOSED:
        _require(payload.get("close_reason") in CLOSE_REASONS,
                 f"invalid close_reason: {payload.get('close_reason')!r}")
        _require(payload.get("effect_certainty") in EFFECT_CERTAINTY,
                 f"invalid effect_certainty: {payload.get('effect_certainty')!r}")
        _require(payload.get("observation_source") in OBSERVATION_SOURCES,
                 f"invalid observation_source: {payload.get('observation_source')!r}")
        _require(bool(payload.get("observed_close_time")), "EXECUTION_CLOSED requires observed_close_time")
    elif event_type == COMMIT_RECORDED:
        _require(bool(payload.get("repository")), "COMMIT_RECORDED requires repository")
        _require(bool(payload.get("commit_hash")), "COMMIT_RECORDED requires commit_hash")
    elif event_type == HUMAN_DECISION_RECORDED:
        for key in ("human_decision_id", "candidate_id", "verdict"):
            _require(bool(payload.get(key)), f"HUMAN_DECISION_RECORDED requires {key}")


def _semantic_key(event: Mapping[str, Any]) -> str:
    """Identity of what an event asserts, excluding append-order bookkeeping."""
    return canonical_hash({
        "event_id": event.get("event_id"), "event_type": event.get("event_type"),
        "run_id": event.get("run_id"), "execution_id": event.get("execution_id"),
        "payload": event.get("payload"),
    })


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------

class ExecutionLedger:
    """Append-only per-run lifecycle ledger.

    One instance addresses exactly one
    ``03_STATS/<run_id>/LEDGER/execution_events.jsonl``.
    """

    def __init__(self, path: Path | str, run_id: str) -> None:
        self.path = Path(path)
        self.run_id = str(run_id)
        self._cache: LedgerRead | None = None
        self._cache_key: tuple[int, int] | None = None

    @classmethod
    def for_run(cls, run_id: str, stats_root: Path | str = STATS_ROOT) -> "ExecutionLedger":
        return cls(ledger_path_for_run(run_id, stats_root), run_id)

    # -- reading ----------------------------------------------------------
    def read(self) -> LedgerRead:
        try:
            stat = self.path.stat()
            key = (stat.st_size, stat.st_mtime_ns)
        except OSError:
            key = (-1, -1)
        if self._cache is not None and self._cache_key == key:
            return self._cache
        result = _scan(self.path)
        self._cache, self._cache_key = result, key
        return result

    def events(self) -> list[dict[str, Any]]:
        return list(self.read().events)

    # -- appending --------------------------------------------------------
    def append(self, event_type: str, payload: Mapping[str, Any], *,
               execution_id: str | None = None, event_id: str | None = None) -> dict[str, Any]:
        """Durably append one event and return it.

        A duplicate ``event_id`` carrying identical content is idempotent: it
        does not create a second semantic observation. A duplicate carrying
        different content is ``DATA_INTEGRITY_ERROR``; it is never resolved by
        last-write-wins and never deduplicated by timestamp or similarity.
        """
        if event_type not in EVENT_TYPES:
            raise LedgerError(f"unsupported event_type: {event_type!r}")
        _validate_payload(event_type, payload)
        with _LedgerLock(self.path):
            current = _scan(self.path)
            identifier = event_id or new_event_id()
            event = {
                "schema_version": SCHEMA_VERSION,
                "event_id": identifier,
                "event_type": event_type,
                "run_id": self.run_id,
                "sequence": current.last_sequence + 1,
                "recorded_at": now(),
                "execution_id": execution_id,
                "payload": dict(payload),
            }
            existing = next((row for row in current.events if row.get("event_id") == identifier), None)
            if existing is not None:
                if _semantic_key(existing) == _semantic_key(event):
                    return dict(existing)
                raise LedgerIntegrityError(
                    f"DATA_INTEGRITY_ERROR incompatible duplicate ledger event_id {identifier}",
                    event_id=identifier, path=str(self.path),
                )
            _validate_envelope(event, self.run_id)
            self._durable_append(event, current)
            self._cache = None
            self._cache_key = None
            return event

    def _durable_append(self, event: Mapping[str, Any], current: LedgerRead) -> None:
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":"), default=str)
        # A damaged tail is sealed with a newline, never rewritten: the partial
        # bytes stay verbatim on their own line and remain a diagnostic.
        prefix = "" if (not current.exists or current.ends_with_newline) else "\n"
        data = (prefix + line + "\n").encode("utf-8")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_APPEND)
            try:
                written = os.write(handle, data)
                if written != len(data):
                    raise OSError(f"short ledger write: {written}/{len(data)} bytes")
                os.fsync(handle)
            finally:
                os.close(handle)
        except OSError as exc:
            raise LedgerWriteError(
                f"LEDGER_WRITE_ERROR durable append failed: {exc}",
                path=str(self.path), event_type=event.get("event_type"),
                execution_id=event.get("execution_id"),
            ) from exc

    # -- the five events --------------------------------------------------
    def record_execution_intent(
        self, *, execution_id: str, node_id: str, invocation_kind: str,
        descriptor_path: Path | str | None = None, subtask_id: str | None = None,
        provider: str | None = None, harness: str | None = None, model: str | None = None,
        effort: str | None = None, profile: str | None = None,
        input_contract_hash: str | None = None, repository: str | None = None,
        worktree: str | None = None, extra: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Durably register the intention to perform this exact invocation.

        Fail-closed: if this raises, the caller must not dispatch.
        """
        prior = next((row for row in self.read().events
                      if row.get("event_type") == EXECUTION_INTENT and row.get("execution_id") == execution_id),
                     None)
        if prior is not None:
            raise LedgerDispatchError(
                f"AT_MOST_ONCE_DISPATCH_VIOLATION durable intent already exists for {execution_id}; "
                "a deliberate retry requires a new execution_id",
                execution_id=execution_id, existing_event_id=prior.get("event_id"), path=str(self.path),
            )
        descriptor = Path(descriptor_path) if descriptor_path else None
        payload: dict[str, Any] = {
            "node_id": node_id, "subtask_id": subtask_id, "invocation_kind": invocation_kind,
            "descriptor_path": str(descriptor) if descriptor else None,
            "descriptor_hash": file_hash(descriptor),
            "identity_contract": IDENTITY_CONTRACT,
            "provider": provider, "harness": harness, "model": model, "effort": effort, "profile": profile,
            "input_contract_hash": input_contract_hash,
            "repository": repository, "worktree": worktree,
            "means": "AAW durably registered the intention to perform this exact invocation.",
            "does_not_mean": "That a spawn occurred, a provider received a request, or billing happened.",
        }
        if extra:
            payload.update(dict(extra))
        return self.append(EXECUTION_INTENT, payload, execution_id=execution_id)

    def record_execution_started(self, *, execution_id: str, receipt: Mapping[str, Any]) -> dict[str, Any]:
        """Record positive evidence that execution started. Never inferred from intent."""
        return self.append(EXECUTION_STARTED, dict(receipt), execution_id=execution_id)

    def try_record_execution_started(self, *, execution_id: str,
                                     receipt: Mapping[str, Any]) -> tuple[str, dict[str, Any] | None]:
        """Non-raising variant.

        A failure here means a process may already exist. The caller must not
        launch a second process; it surfaces
        ``EXECUTION_STARTED_LEDGER_UNCERTAIN`` instead.
        """
        try:
            return RECORDED, self.record_execution_started(execution_id=execution_id, receipt=receipt)
        except LedgerError:
            return STARTED_LEDGER_UNCERTAIN, None

    def record_execution_closed(
        self, *, execution_id: str, close_reason: str, effect_certainty: str,
        observation_source: str, exit_code: int | None = None, outcome: str | None = None,
        timed_out: bool = False, cancelled: bool = False, interrupted: bool = False,
        result_refs: Any = None, detail: str | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record the strongest observed terminal lifecycle fact."""
        payload: dict[str, Any] = {
            "observed_close_time": now(), "close_reason": close_reason,
            "effect_certainty": effect_certainty, "observation_source": observation_source,
            "exit_code": exit_code, "outcome": outcome,
            "timed_out": bool(timed_out), "cancelled": bool(cancelled), "interrupted": bool(interrupted),
            "result_refs": result_refs,
            "detail": detail,
            "does_not_mean": "That workflow state advanced, or that every side effect is known.",
        }
        if extra:
            payload.update(dict(extra))
        return self.append(EXECUTION_CLOSED, payload, execution_id=execution_id)

    def try_record_execution_closed(self, **kwargs: Any) -> tuple[str, dict[str, Any] | None]:
        """Non-raising variant. Never triggers a provider retry."""
        try:
            return RECORDED, self.record_execution_closed(**kwargs)
        except LedgerError:
            return CLOSE_RECORD_UNCERTAIN, None

    def record_commit(
        self, *, repository: str, commit_hash: str, producer_execution_ids: Sequence[str],
        expected_parent: str | None = None, subtask_id: str | None = None, role: str | None = None,
        git_evidence: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record that AAW observed this Git commit. Not a workflow acceptance."""
        payload = {
            "repository": str(repository), "commit_hash": str(commit_hash),
            "commit_identity": {"repository": str(repository), "commit_hash": str(commit_hash)},
            "expected_parent": expected_parent, "subtask_id": subtask_id, "role": role,
            "producer_execution_ids": [str(item) for item in producer_execution_ids],
            "git_evidence": dict(git_evidence) if git_evidence else None,
            "git_evidence_hash": canonical_hash(git_evidence) if git_evidence else None,
            "means": "AAW observed and recorded this Git commit.",
            "does_not_mean": "That the workflow accepted it.",
        }
        return self.append(COMMIT_RECORDED, payload)

    def record_human_decision(
        self, *, human_decision_id: str, candidate_id: str, verdict: str,
        decision_artifact_path: Path | str | None = None, quality_assessment: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Reference an already-written immutable human decision artifact."""
        artifact = Path(decision_artifact_path) if decision_artifact_path else None
        payload = {
            "human_decision_id": str(human_decision_id), "candidate_id": str(candidate_id),
            "verdict": str(verdict),
            "decision_artifact_path": str(artifact) if artifact else None,
            "decision_artifact_hash": file_hash(artifact),
            "quality_assessment": quality_assessment, "reason": reason,
            "authority": "The immutable decision artifact remains the detailed authority.",
            "does_not_mean": "ACCEPT is a release verdict, never a model-quality PASS.",
        }
        return self.append(HUMAN_DECISION_RECORDED, payload)

    # -- reconciliation-minimum queries -----------------------------------
    def lifecycle(self) -> dict[str, dict[str, Any]]:
        """Per-execution lifecycle view built only from recorded observations."""
        view: dict[str, dict[str, Any]] = {}
        for row in self.read().events:
            execution_id = row.get("execution_id")
            if not execution_id or row.get("event_type") not in EXECUTION_EVENT_TYPES:
                continue
            entry = view.setdefault(str(execution_id), {
                "execution_id": str(execution_id), "intent": None, "started": [], "closed": [],
            })
            if row["event_type"] == EXECUTION_INTENT:
                entry["intent"] = row
            elif row["event_type"] == EXECUTION_STARTED:
                entry["started"].append(row)
            else:
                entry["closed"].append(row)
        for entry in view.values():
            entry["state"] = (
                "CLOSED" if entry["closed"]
                else "STARTED_NOT_CLOSED" if entry["started"]
                else "INTENT_ONLY"
            )
        return view

    def intent_without_started(self) -> list[str]:
        """Unknown whether execution started."""
        return sorted(key for key, value in self.lifecycle().items() if value["state"] == "INTENT_ONLY")

    def started_without_closed(self) -> list[str]:
        """Known started, terminal state unresolved."""
        return sorted(key for key, value in self.lifecycle().items() if value["state"] == "STARTED_NOT_CLOSED")

    def unresolved(self) -> list[str]:
        return sorted(set(self.intent_without_started()) | set(self.started_without_closed()))

    def close_status(self, execution_id: str) -> dict[str, Any] | None:
        entry = self.lifecycle().get(execution_id)
        if not entry or not entry["closed"]:
            return None
        return dict(entry["closed"][-1].get("payload") or {})

    def commits_for_execution(self, execution_id: str) -> list[dict[str, Any]]:
        return [dict(row.get("payload") or {}) for row in self.read().by_type(COMMIT_RECORDED)
                if execution_id in ((row.get("payload") or {}).get("producer_execution_ids") or [])]

    def human_decisions(self) -> list[dict[str, Any]]:
        return [dict(row.get("payload") or {}) for row in self.read().by_type(HUMAN_DECISION_RECORDED)]

    def summary(self) -> dict[str, Any]:
        read = self.read()
        lifecycle = self.lifecycle()
        report = validate_events(read, self.run_id)
        return {
            "run_id": self.run_id, "ledger_path": str(self.path), "exists": read.exists,
            "events": len(read.events), "executions": len(lifecycle),
            "closed": sum(1 for value in lifecycle.values() if value["state"] == "CLOSED"),
            "unresolved": len(self.unresolved()),
            "commit_records": len(read.by_type(COMMIT_RECORDED)),
            "human_decisions": len(read.by_type(HUMAN_DECISION_RECORDED)),
            "ledger_errors": len(report["errors"]),
            "diagnostics": list(read.diagnostics),
        }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_events(read: LedgerRead, run_id: str | None = None) -> dict[str, Any]:
    """Structural and lifecycle validation.

    A missing EXECUTION_CLOSED after a crash is unresolved evidence, not
    corruption: it is reported under ``unresolved``, never under ``errors``.
    """
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    def fail(kind: str, detail: str, **context: Any) -> None:
        errors.append({"kind": kind, "detail": detail, **context})

    for row in read.diagnostics:
        target = errors if row["kind"] in {"LEDGER_DAMAGED_RECORD", "MALFORMED_EVENT"} else warnings
        target.append(dict(row))

    seen_ids: dict[str, Mapping[str, Any]] = {}
    previous_sequence = 0
    intents: dict[str, Mapping[str, Any]] = {}
    started: dict[str, list[Mapping[str, Any]]] = {}
    closed: dict[str, list[Mapping[str, Any]]] = {}
    commits: dict[tuple[str, str], Mapping[str, Any]] = {}

    for row in read.events:
        expected_run = str(row.get("run_id")) if run_id is None else run_id
        try:
            _validate_envelope(row, expected_run)
        except LedgerError as exc:
            kind = "UNKNOWN_SCHEMA" if "schema_version" in str(exc) else "MALFORMED_EVENT"
            fail(kind, str(exc), event_id=row.get("event_id"), sequence=row.get("sequence"))
            continue
        event_id = str(row["event_id"])
        if event_id in seen_ids:
            if _semantic_key(seen_ids[event_id]) == _semantic_key(row):
                fail("DUPLICATE_EVENT_ID", f"identical event_id appended twice: {event_id}", event_id=event_id)
            else:
                fail("DATA_INTEGRITY_ERROR", f"incompatible duplicate event_id: {event_id}", event_id=event_id)
            continue
        seen_ids[event_id] = row
        sequence = int(row["sequence"])
        if sequence <= previous_sequence:
            fail("INVALID_SEQUENCE", f"sequence {sequence} does not increase after {previous_sequence}",
                 event_id=event_id)
        previous_sequence = max(previous_sequence, sequence)
        payload = dict(row.get("payload") or {})
        event_type = str(row["event_type"])
        execution_id = row.get("execution_id")
        try:
            _validate_payload(event_type, payload)
        except LedgerError as exc:
            kind = "MALFORMED_HUMAN_DECISION" if event_type == HUMAN_DECISION_RECORDED else "MALFORMED_EVENT"
            fail(kind, str(exc), event_id=event_id)
            continue
        if event_type == EXECUTION_INTENT:
            if execution_id in intents:
                fail("AT_MOST_ONCE_DISPATCH_VIOLATION", f"second intent for {execution_id}", event_id=event_id)
            intents[str(execution_id)] = row
        elif event_type == EXECUTION_STARTED:
            if execution_id not in intents:
                fail("STARTED_WITHOUT_INTENT",
                     f"start observed for unknown execution identity {execution_id}", event_id=event_id)
            started.setdefault(str(execution_id), []).append(row)
        elif event_type == EXECUTION_CLOSED:
            if execution_id not in intents:
                fail("CLOSED_WITHOUT_INTENT",
                     f"close observed for unknown execution identity {execution_id}", event_id=event_id)
            elif execution_id not in started and payload.get("observation_source") not in CLOSE_WITHOUT_START_SOURCES:
                fail("CLOSED_WITHOUT_STARTED_NOT_RECONCILED",
                     f"{execution_id} closed without a start observation and without an explicit "
                     "reconciliation or never-started source", event_id=event_id)
            prior = closed.setdefault(str(execution_id), [])
            for earlier in prior:
                earlier_payload = dict(earlier.get("payload") or {})
                if (earlier_payload.get("close_reason") != payload.get("close_reason")
                        or earlier_payload.get("exit_code") != payload.get("exit_code")):
                    fail("CONTRADICTORY_CLOSE",
                         f"{execution_id} has contradictory terminal events "
                         f"({earlier_payload.get('close_reason')} vs {payload.get('close_reason')})",
                         event_id=event_id)
            prior.append(row)
        elif event_type == COMMIT_RECORDED:
            key = (str(payload.get("repository")), str(payload.get("commit_hash")))
            earlier = commits.get(key)
            if earlier is not None:
                earlier_payload = dict(earlier.get("payload") or {})
                comparable = ("producer_execution_ids", "expected_parent", "role", "subtask_id")
                if any(earlier_payload.get(field) != payload.get(field) for field in comparable):
                    fail("INCOMPATIBLE_DUPLICATE_COMMIT",
                         f"conflicting commit record for {key[1]}", event_id=event_id)
            commits[key] = row

    unresolved = {
        "intent_without_started": sorted(key for key in intents if key not in started and key not in closed),
        "started_without_closed": sorted(key for key in started if key not in closed),
    }
    return {
        "schema_version": SCHEMA_VERSION, "path": str(read.path), "exists": read.exists,
        "events": len(read.events), "errors": errors, "warnings": warnings,
        "unresolved": unresolved, "valid": not errors,
    }


def validate_ledger(path: Path | str, run_id: str | None = None) -> dict[str, Any]:
    return validate_events(_scan(Path(path)), run_id)


# ---------------------------------------------------------------------------
# Lifecycle recorder used by runners (keeps ledger calls out of adapters)
# ---------------------------------------------------------------------------

class LifecycleRecorder:
    """Binds one allocated execution to its run ledger.

    Runners construct it right after ``allocate_execution`` and before
    dispatch. It records intent fail-closed, accepts a start receipt from
    ``process_observation``, and records exactly one close.
    """

    def __init__(self, ledger: "ExecutionLedger | None", execution_id: str) -> None:
        self.ledger = ledger
        self.execution_id = str(execution_id)
        self.start_status = NOT_OBSERVED
        self.close_status = NOT_OBSERVED
        self.started_event: dict[str, Any] | None = None
        self.closed_event: dict[str, Any] | None = None

    @property
    def started(self) -> bool:
        return self.started_event is not None

    def observe_start(self, receipt: Mapping[str, Any]) -> None:
        """Observer callback for ``process_observation.observation_scope``.

        Never raises: a process may already exist, and the caller must never be
        pushed into launching a second one.
        """
        if self.ledger is None or self.started_event is not None:
            return
        status, event = self.ledger.try_record_execution_started(
            execution_id=self.execution_id, receipt=receipt)
        self.start_status = status
        self.started_event = event

    def close(self, **kwargs: Any) -> str:
        if self.ledger is None or self.closed_event is not None:
            return self.close_status
        if self.start_status == STARTED_LEDGER_UNCERTAIN:
            extra = dict(kwargs.get("extra") or {})
            extra["start_record_status"] = STARTED_LEDGER_UNCERTAIN
            kwargs["extra"] = extra
        status, event = self.ledger.try_record_execution_closed(execution_id=self.execution_id, **kwargs)
        self.close_status = status
        self.closed_event = event
        return status

    def status(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "start_record_status": self.start_status,
            "close_record_status": self.close_status,
            "requires_reconciliation": (self.start_status == STARTED_LEDGER_UNCERTAIN
                                        or self.close_status == CLOSE_RECORD_UNCERTAIN),
        }


# ---------------------------------------------------------------------------
# Small diagnostic CLI
# ---------------------------------------------------------------------------

def _iter_run_ledgers(stats_root: Path) -> Iterable[tuple[str, Path]]:
    for candidate in sorted(stats_root.glob(f"*/{LEDGER_DIRNAME}/{LEDGER_FILENAME}")):
        yield candidate.parent.parent.name, candidate


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AAW V0.4B execution lifecycle ledger diagnostics")
    parser.add_argument("--stats-root", type=Path, default=STATS_ROOT)
    parser.add_argument("--run-id", help="inspect one run ledger")
    parser.add_argument("--list", action="store_true", help="list run ledgers")
    parser.add_argument("--validate", action="store_true", help="validate instead of summarise")
    parser.add_argument("--unresolved", action="store_true", help="show unresolved lifecycles only")
    args = parser.parse_args(argv)

    if args.list or not args.run_id:
        rows = [{"run_id": run_id, "path": str(path)} for run_id, path in _iter_run_ledgers(args.stats_root)]
        print(json.dumps({"stats_root": str(args.stats_root), "ledgers": rows}, indent=2, ensure_ascii=False))
        return 0
    ledger = ExecutionLedger.for_run(args.run_id, args.stats_root)
    if args.validate:
        print(json.dumps(validate_ledger(ledger.path, args.run_id), indent=2, ensure_ascii=False))
        return 0
    if args.unresolved:
        print(json.dumps({
            "run_id": args.run_id,
            "intent_without_started": ledger.intent_without_started(),
            "started_without_closed": ledger.started_without_closed(),
        }, indent=2, ensure_ascii=False))
        return 0
    print(json.dumps(ledger.summary(), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
