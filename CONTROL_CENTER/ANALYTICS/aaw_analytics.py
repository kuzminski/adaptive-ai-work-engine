#!/usr/bin/env python3
"""AAW Analytics V2 — DERIVED, REBUILDABLE execution-grain evidence index.

Authority model
---------------
``03_STATS`` is the immutable RAW EVIDENCE AUTHORITY. This module builds a
disposable SQLite index (``ANALYTICS/aaw_analytics.sqlite``) so the Control
Center can show Insights without ad-hoc joins over filenames and timestamps.
Nothing here writes to 03_STATS, and no runner reads this database.

What changed in V2
------------------
V1/V1.1 joined executions to nodes through ``run_id + node_id``. That join
could collapse or multiply repeated invocations of one logical node. V2 makes
``execution_id`` the analytical grain and joins ONLY through explicit evidence
identities:

  * ``execution_id``            (V0.4A descriptor identity)
  * ``(review_execution_id, finding_id)``  (finding identity)
  * ``(repository, commit_hash)``          (Git identity)
  * ``candidate_id`` / ``human_decision_id``
  * ``preprocess_id`` + ``downstream_execution_id``

Never through timestamp proximity, filename similarity, a repeated
``node_id``, a shared ``provider_session_id``, or chronological assumption.

Missingness is preserved: absent usage is NULL, never zero. Absent lifecycle is
unresolved, never FAIL. Absent human quality assessment is UNKNOWN, never a
model verdict.

Usage
-----
    python aaw_analytics.py --rebuild      # temp build -> validate -> publish
    python aaw_analytics.py --refresh      # incremental, idempotent
    python aaw_analytics.py --data-quality
    python aaw_analytics.py --readiness
    python aaw_analytics.py --self-test
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import statistics
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

_MODULE_ROOT = Path(__file__).resolve().parents[2]
if str(_MODULE_ROOT) not in sys.path:
    sys.path.insert(0, str(_MODULE_ROOT))
from aaw_paths import ANALYTICS_DB_PATH, STATS_ROOT

SCHEMA_VERSION = "AAW_ANALYTICS_INDEX_V2"
PRIOR_SCHEMA_VERSIONS = ("AAW_ANALYTICS_INDEX_V1", "AAW_ANALYTICS_INDEX_V1.1")
IDENTITY_CONTRACT = "AAW_EXECUTION_DESCRIPTOR_V0.4A"
LEDGER_SCHEMA_VERSION = "AAW_EXECUTION_LEDGER_V0.4B"

DEFAULT_STATS_ROOT = STATS_ROOT
DEFAULT_DB_PATH = ANALYTICS_DB_PATH
AAW_ROOT = Path(__file__).resolve().parents[2]

MIN_SAMPLE = 5  # below this a comparative metric reads INSUFFICIENT EVIDENCE

DERIVED_EXPORT_MARKER = "DERIVED_ANALYTICS_NOT_AUTHORITY"

# --- evidence classification vocabularies -----------------------------------

MODERN = "MODERN_EXECUTION_GRAIN"
LEGACY = "LEGACY_SOURCE_GRAIN"
FIXTURE = "FIXTURE"
UNKNOWN = "UNKNOWN"

FIXTURE_CLASSES = ("FIXTURE_SELF_TEST", "FIXTURE_E2E", "FIXTURE_SMOKE", "NOT_FIXTURE", "UNKNOWN")

# Lifecycle states derived ONLY from observed ledger events (V0.4B).
LIFECYCLE_STATES = ("INTENT_ONLY", "STARTED_OPEN", "CLOSED", "LIFECYCLE_CONFLICT", "UNKNOWN")

# Identity/provenance precedence. Lower rank wins. Never mtime-based.
IDENTITY_AUTHORITIES = {
    "EXECUTION_DESCRIPTOR": 1,     # 03_STATS/<run>/EXECUTIONS/<EXE>.json
    "STATE_EXECUTIONS": 2,         # job_state/workflow_state executions[]
    "LEGACY_NODE_TELEMETRY": 3,    # pre-V0.4A node telemetry (no execution identity)
}

# Explicit fixture rules. Each fires on a PRODUCER-DECLARED field, never on a
# filename, a timestamp or a path guess about semantics. ``fixture_evidence``
# records which rule fired so every classification is auditable.
_FIXTURE_JOB_IDS = {
    "self-test": ("FIXTURE_SELF_TEST", "DECLARED_SELF_TEST_JOB_ID"),
    "repair-self-test": ("FIXTURE_SELF_TEST", "DECLARED_SELF_TEST_JOB_ID"),
    "v04b-ledger-e2e": ("FIXTURE_E2E", "DECLARED_E2E_JOB_ID"),
    "qwen-preprocess-live-e2e": ("FIXTURE_E2E", "DECLARED_E2E_JOB_ID"),
    "real-single-smoke": ("FIXTURE_SMOKE", "DECLARED_SMOKE_JOB_ID"),
    "real-multi-smoke": ("FIXTURE_SMOKE", "DECLARED_SMOKE_JOB_ID"),
}
_FIXTURE_REPO_MARKERS = ("WORKFLOW_SMOKE_TEST", "CUSTOM_JOB_SMOKE_TEST", "ORCA_SMOKE_TEST", "LOCAL_LLM_SMOKES")

# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

_OUTCOME_MAP = {
    "PASS": "PASS", "VALID PASS": "PASS", "COMPLETED": "PASS", "ACCEPTED": "PASS",
    "FAIL": "FAIL", "VALID FAIL": "FAIL",
    "INVALID": "INVALID",
    "BLOCKED": "BLOCKED",
    "SKIP": "SKIP", "SKIPPED": "SKIP",
}
_RUN_STATUS_MAP = {
    "PASS": "PASS", "COMPLETED": "PASS", "ACCEPTED": "PASS",
    "FAIL": "FAIL", "INVALID": "FAIL",
    "BLOCKED": "BLOCKED",
    "WAITING_FOR_HUMAN": "WAITING_FOR_HUMAN", "HUMAN_REQUIRED": "WAITING_FOR_HUMAN",
    "WAITING_FOR_PLAN_APPROVAL": "WAITING_FOR_HUMAN",
    "WAITING_FOR_REPAIR_SELECTION": "WAITING_FOR_HUMAN",
}
_VERDICT_MAP = {
    "ACCEPT": "ACCEPTED", "ACCEPTED": "ACCEPTED", "ACCEPT CANDIDATE": "ACCEPTED",
    "REJECT": "REJECTED", "REJECTED": "REJECTED",
    "LEAVE-FOR-LATER": "LEFT_FOR_LATER", "LEAVE FOR LATER": "LEFT_FOR_LATER",
    "LEFT_FOR_LATER": "LEFT_FOR_LATER",
}
# Quality assessment is a SEPARATE axis from the release verdict. ACCEPT is
# never converted into a model-quality PASS and LEFT_FOR_LATER is never a
# rejection.
_QUALITY_MAP = {"PASS": "PASS", "FAIL": "FAIL", "UNKNOWN": "UNKNOWN"}

# Invocation kinds that consume a model. MACHINE_GATE never counts as an LLM call.
_LLM_KINDS = {"LLM", "REVIEW", "REPAIR", "DELTA_REVIEW", "PLAN", "PREPROCESS"}


def _norm_outcome(value: Any) -> str | None:
    if value in (None, "", "null"):
        return None
    return _OUTCOME_MAP.get(str(value).strip().upper(), str(value).strip().upper())


def _norm_run_status(value: Any) -> str:
    if not value:
        return UNKNOWN
    return _RUN_STATUS_MAP.get(str(value).strip().upper(), str(value).strip().upper())


def _norm_verdict(value: Any) -> str | None:
    if value in (None, "", "null"):
        return None
    return _VERDICT_MAP.get(str(value).strip().upper(), str(value).strip().upper())


def _norm_quality(value: Any) -> str | None:
    if value in (None, "", "null"):
        return None
    return _QUALITY_MAP.get(str(value).strip().upper(), str(value).strip().upper())


def _as_int(value: Any) -> int | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_bool_int(value: Any) -> int | None:
    if value is None:
        return None
    return 1 if bool(value) else 0


def _day(value: Any) -> str | None:
    if not value or not isinstance(value, str):
        return None
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", value)
    return "-".join(match.groups()) if match else None


def _text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    return str(value)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _locator(rel_path: str, sha256: str, pointer: str) -> str:
    """Stable derived row locator for a source record.

    This is NOT an execution identity and is never presented as one. It locates
    a record inside a specific version of a specific evidence file
    (path + content hash + JSON pointer), which is exactly what a legacy row
    can honestly claim.
    """
    payload = f"{rel_path}|{sha256}|{pointer}"
    return "LOC_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _parse_iso(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return None


def _elapsed_s(start: Any, end: Any) -> float | None:
    a, b = _parse_iso(start), _parse_iso(end)
    if a is None or b is None or (a.tzinfo is None) != (b.tzinfo is None):
        return None
    try:
        delta = (b - a).total_seconds()
    except TypeError:
        return None
    return round(delta, 3) if delta >= 0 else None


def _is_v04a(schema_version: Any) -> bool:
    return isinstance(schema_version, str) and "V0.4A" in schema_version.upper()


def _under_temp(path_value: Any) -> bool:
    if not isinstance(path_value, str) or not path_value:
        return False
    try:
        temp = os.path.normcase(os.path.normpath(tempfile.gettempdir()))
        candidate = os.path.normcase(os.path.normpath(path_value))
    except (OSError, ValueError):
        return False
    return candidate.startswith(temp + os.sep) or candidate == temp


def _smoke_repo_marker(path_value: Any) -> str | None:
    if not isinstance(path_value, str) or not path_value:
        return None
    upper = path_value.replace("/", "\\").upper()
    for marker in _FIXTURE_REPO_MARKERS:
        if f"\\{marker}\\" in upper or upper.endswith(f"\\{marker}"):
            return marker
    return None


def classify_fixture(*, declared_job_id: Any, repository: Any, worktree: Any,
                     descriptor_fixture_class: Any = None) -> tuple[str, str]:
    """Return ``(fixture_class, fixture_evidence)`` from explicit declared evidence only.

    Rule order (first match wins, most authoritative first):
      1. ``fixture_class`` recorded by the producer in the V0.4A descriptor.
      2. A declared self-test / e2e / smoke ``job_id``.
      3. A declared repository inside the OS temporary directory — real product
         work is never performed in a throwaway temp repository.
      4. A declared repository inside one of AAW's own ``*_SMOKE_TEST``
         fixture repositories.
    Nothing else is classified: absent evidence stays ``UNKNOWN`` and is
    reported as such rather than guessed at.
    """
    if descriptor_fixture_class:
        return str(descriptor_fixture_class), "DESCRIPTOR_FIXTURE_CLASS"
    key = str(declared_job_id).strip().lower() if declared_job_id else ""
    if key in _FIXTURE_JOB_IDS:
        return _FIXTURE_JOB_IDS[key]
    for value in (repository, worktree):
        if _under_temp(value):
            return "FIXTURE_SMOKE", "DECLARED_TEMP_REPOSITORY"
    for value in (repository, worktree):
        marker = _smoke_repo_marker(value)
        if marker:
            return "FIXTURE_SMOKE", f"DECLARED_SMOKE_TEST_REPOSITORY:{marker}"
    return UNKNOWN, "NONE"


def _is_fixture(fixture_class: Any) -> bool:
    return isinstance(fixture_class, str) and fixture_class.startswith("FIXTURE_")


def evidence_class(evidence_grain: str, fixture_class: str) -> str:
    """The four-value classification: FIXTURE takes display precedence."""
    if _is_fixture(fixture_class):
        return FIXTURE
    return evidence_grain if evidence_grain in (MODERN, LEGACY) else UNKNOWN


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
# Only source_file carries a foreign key. Entity-to-entity relations are
# deliberately NOT foreign keys: analytics must remain able to record a
# lifecycle observation whose descriptor is unreadable, a finding whose repair
# never happened, or a commit with no attributable producer. Those become
# explicit "unlinked" counts instead of silent insertion failures.

_DDL = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE ingest_unit (
    unit_key TEXT PRIMARY KEY,
    signature TEXT NOT NULL,        -- content signature: rel_path + bytes + sha256 per file
    run_ids TEXT NOT NULL,          -- JSON array of run_ids this unit owns
    files INTEGER NOT NULL,
    ingested_at TEXT NOT NULL
);

CREATE TABLE source_file (
    id INTEGER PRIMARY KEY,
    unit_key TEXT NOT NULL,
    path TEXT UNIQUE NOT NULL,
    rel_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    mtime REAL NOT NULL,
    schema_version TEXT,
    layout TEXT NOT NULL,
    source_type TEXT NOT NULL,      -- RAW_SEMANTIC | EXECUTION_DESCRIPTOR | LEDGER
                                    -- | CANDIDATE | HUMAN_DECISION | PREPROCESS
                                    -- | FROZEN_INPUT | DIAGNOSTIC | UNKNOWN
    status TEXT NOT NULL,           -- INDEXED | PARSE_ERROR | SUPERSEDED | SKIPPED
    detail TEXT,
    ingested_at TEXT NOT NULL
);

CREATE TABLE run (
    run_id TEXT PRIMARY KEY,
    unit_key TEXT NOT NULL,
    source_id INTEGER REFERENCES source_file(id),
    job_class TEXT,                 -- SINGLE_TASK | WORKFLOW | CUSTOM_JOB | CLASSIFIER | LEGACY | UNKNOWN
    workflow_or_job_id TEXT,
    goal TEXT,
    repository TEXT,
    worktree TEXT,
    started_at TEXT,
    ended_at TEXT,
    started_day TEXT,
    final_status TEXT,
    final_status_basis TEXT,
    human_gate_reached INTEGER NOT NULL DEFAULT 0,
    human_verdict TEXT,             -- state field only; the HDE artifact is authority
    evidence_grain TEXT NOT NULL,
    evidence_class TEXT NOT NULL,
    fixture_class TEXT NOT NULL,
    fixture_evidence TEXT NOT NULL,
    identity_contract TEXT,
    ledger_present INTEGER NOT NULL DEFAULT 0,
    ledger_schema_version TEXT,
    run_authority TEXT NOT NULL,
    ingested_at TEXT NOT NULL
);

CREATE TABLE logical_node (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    subtask_key TEXT NOT NULL,      -- subtask_id, else subtask_index, else '-'
    node_type TEXT,
    subtask_id TEXT,
    subtask_index INTEGER,
    outcome TEXT,
    repair_cycle INTEGER,
    wall_time_s REAL,
    started_at TEXT,
    ended_at TEXT,
    review_independence TEXT,
    workflow_or_job_id TEXT,
    evidence_grain TEXT NOT NULL,
    source_id INTEGER REFERENCES source_file(id),
    UNIQUE (run_id, node_id, subtask_key)
);

CREATE TABLE execution (
    id INTEGER PRIMARY KEY,
    execution_id TEXT,              -- modern only; NULL for legacy, never synthesised
    row_locator TEXT NOT NULL,      -- derived source locator; NOT an execution identity
    run_id TEXT NOT NULL,
    node_id TEXT,
    node_type TEXT,                 -- resolved role used for comparison cells
    subtask_id TEXT,
    subtask_index INTEGER,
    invocation_kind TEXT,
    llm_invocation INTEGER,         -- 1 | 0 | NULL
    llm_invocation_basis TEXT,      -- INVOCATION_KIND | MODEL_PRESENCE | UNKNOWN
    provider TEXT,
    harness TEXT,
    model TEXT,
    effort TEXT,
    profile TEXT,
    provider_session_id TEXT,       -- observed metadata; NEVER identity
    retry_of_execution_id TEXT,
    repair_cycle INTEGER,
    originating_review_execution_id TEXT,
    repair_execution_id TEXT,       -- delta review -> the repair it verified
    original_review_execution_id TEXT,
    created_at TEXT,
    started_at TEXT,
    ended_at TEXT,
    started_day TEXT,
    started_day_basis TEXT,
    outcome TEXT,
    outcome_source TEXT,
    wall_time_s REAL,
    wall_time_basis TEXT,
    input_tokens INTEGER,
    cached_input_tokens INTEGER,
    output_tokens INTEGER,
    reasoning_tokens INTEGER,
    usage_observed INTEGER NOT NULL DEFAULT 0,
    telemetry_status TEXT,
    execution_mode TEXT,
    input_contract_hash TEXT,       -- deterministic difficulty dimension, as recorded
    selection_reason TEXT,
    policy_version TEXT,
    descriptor_status TEXT,
    identity_authority TEXT NOT NULL,
    identity_contract TEXT,
    evidence_grain TEXT NOT NULL,
    fixture_class TEXT NOT NULL,
    fixture_evidence TEXT NOT NULL,
    conflict INTEGER NOT NULL DEFAULT 0,
    source_id INTEGER REFERENCES source_file(id)
);
CREATE UNIQUE INDEX ux_execution_identity ON execution(execution_id) WHERE execution_id IS NOT NULL;
CREATE UNIQUE INDEX ux_execution_locator ON execution(row_locator);
CREATE INDEX ix_execution_run ON execution(run_id);
CREATE INDEX ix_execution_model ON execution(model, effort);
CREATE INDEX ix_execution_day ON execution(started_day);

CREATE TABLE execution_lifecycle (
    execution_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    lifecycle_state TEXT NOT NULL,
    basis TEXT NOT NULL,            -- LEDGER | NO_LEDGER_EVIDENCE
    -- sequences express ledger APPEND ORDER only; never real-world time
    intent_sequence INTEGER,
    started_sequence INTEGER,
    closed_sequence INTEGER,
    start_evidence TEXT,
    process_id INTEGER,
    process_creation_time TEXT,
    process_creation_time_source TEXT,
    observed_start_time TEXT,
    observed_close_time TEXT,
    close_reason TEXT,
    effect_certainty TEXT,
    exit_code INTEGER,
    observation_source TEXT,
    ledger_outcome TEXT,            -- observed close outcome; NOT a reviewer verdict
    timed_out INTEGER,
    cancelled INTEGER,
    interrupted INTEGER,
    indexed_execution INTEGER NOT NULL DEFAULT 0,
    conflict_detail TEXT,
    source_id INTEGER REFERENCES source_file(id)
);
CREATE INDEX ix_lifecycle_run ON execution_lifecycle(run_id);

CREATE TABLE validation (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL,
    machine_gate_result TEXT,
    reviewer_result TEXT,
    repair_required INTEGER,
    repair_cycles INTEGER,
    delta_review_result TEXT,
    evidence_grain TEXT NOT NULL,
    basis TEXT NOT NULL,
    source_id INTEGER REFERENCES source_file(id)
);
CREATE INDEX ix_validation_run ON validation(run_id);

CREATE TABLE review_finding (
    review_execution_id TEXT NOT NULL,
    finding_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    severity TEXT,
    file TEXT,
    location TEXT,
    description TEXT,
    required_fix TEXT,
    commit_hash TEXT,
    source_id INTEGER REFERENCES source_file(id),
    PRIMARY KEY (review_execution_id, finding_id)
);

CREATE TABLE review_producer_link (
    review_execution_id TEXT NOT NULL,
    produced_execution_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    source_id INTEGER REFERENCES source_file(id),
    PRIMARY KEY (review_execution_id, produced_execution_id)
);

CREATE TABLE finding_link (
    review_execution_id TEXT NOT NULL,
    finding_id TEXT NOT NULL,
    linked_execution_id TEXT NOT NULL,
    link_role TEXT NOT NULL,        -- REPAIR_SELECTED | DELTA_DISPOSITION
    disposition TEXT,
    run_id TEXT NOT NULL,
    source_id INTEGER REFERENCES source_file(id),
    PRIMARY KEY (review_execution_id, finding_id, linked_execution_id, link_role)
);

CREATE TABLE preprocess (
    id INTEGER PRIMARY KEY,
    preprocess_id TEXT,             -- PRE_ decision identity (V0.4A+); NULL for V0.1
    row_locator TEXT NOT NULL,
    execution_id TEXT,              -- ONLY when local inference actually occurred
    run_id TEXT NOT NULL,
    downstream_node_id TEXT,
    downstream_execution_id TEXT,
    downstream_link_confidence TEXT NOT NULL,  -- EXPLICIT_EXECUTION_ID | EXPLICIT_NODE_ID | UNLINKED
    preprocess_type TEXT,
    profile TEXT,
    provider TEXT,
    model TEXT,
    status TEXT,
    decision_status TEXT,           -- INVOKED | SKIPPED | FAILED | UNKNOWN
    reason TEXT,
    authority TEXT,
    input_chars INTEGER,
    output_chars INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    wall_time_s REAL,
    downstream_input_before_estimate INTEGER,
    downstream_input_after_estimate INTEGER,
    created_at TEXT,
    created_day TEXT,
    output_artifact_hash TEXT,
    consumed INTEGER,
    consumed_basis TEXT,
    source_artifact_count INTEGER,
    evidence_grain TEXT NOT NULL,
    fixture_class TEXT NOT NULL,
    source_id INTEGER REFERENCES source_file(id)
);
CREATE UNIQUE INDEX ux_preprocess_identity ON preprocess(preprocess_id) WHERE preprocess_id IS NOT NULL;
CREATE UNIQUE INDEX ux_preprocess_locator ON preprocess(row_locator);
CREATE INDEX ix_preprocess_run ON preprocess(run_id);

CREATE TABLE commit_record (
    id INTEGER PRIMARY KEY,
    repository TEXT NOT NULL,
    commit_hash TEXT NOT NULL,
    run_id TEXT,
    subtask_id TEXT,
    role TEXT,
    expected_parent TEXT,
    git_evidence_hash TEXT,
    observed_in_state INTEGER NOT NULL DEFAULT 0,
    observed_in_ledger INTEGER NOT NULL DEFAULT 0,
    conflict INTEGER NOT NULL DEFAULT 0,
    source_id INTEGER REFERENCES source_file(id)
);
CREATE UNIQUE INDEX ux_commit_identity ON commit_record(repository, commit_hash);

CREATE TABLE commit_producer_link (
    repository TEXT NOT NULL,
    commit_hash TEXT NOT NULL,
    execution_id TEXT NOT NULL,
    run_id TEXT,
    PRIMARY KEY (repository, commit_hash, execution_id)
);

CREATE TABLE candidate (
    candidate_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    repository TEXT,
    worktree TEXT,
    candidate_head TEXT,
    artifact_manifest TEXT,
    content_identity_hash TEXT,
    created_at TEXT,
    source_authority TEXT NOT NULL, -- CANDIDATE_ARTIFACT | STATE_CANDIDATE
    conflict INTEGER NOT NULL DEFAULT 0,
    source_id INTEGER REFERENCES source_file(id)
);

CREATE TABLE candidate_execution_link (
    candidate_id TEXT NOT NULL,
    execution_id TEXT NOT NULL,
    link_role TEXT NOT NULL,        -- REVIEW | CHECK
    run_id TEXT,
    PRIMARY KEY (candidate_id, execution_id, link_role)
);

CREATE TABLE human_decision (
    human_decision_id TEXT PRIMARY KEY,
    candidate_id TEXT,
    run_id TEXT NOT NULL,
    verdict TEXT,                   -- release verdict; NEVER a model-quality PASS
    quality_assessment TEXT,        -- separate optional axis; NULL = UNKNOWN
    reason TEXT,
    recorded_at TEXT,
    artifact_path TEXT,
    artifact_hash TEXT,
    source_authority TEXT NOT NULL, -- HUMAN_DECISION_ARTIFACT | STATE_HUMAN_DECISIONS | LEDGER_OBSERVATION
    candidate_resolved INTEGER NOT NULL DEFAULT 0,
    conflict INTEGER NOT NULL DEFAULT 0,
    source_id INTEGER REFERENCES source_file(id)
);

CREATE TABLE ingest_diagnostic (
    id INTEGER PRIMARY KEY,
    unit_key TEXT,
    run_id TEXT,
    path TEXT,
    kind TEXT NOT NULL,
    detail TEXT,
    ingested_at TEXT NOT NULL
);
CREATE INDEX ix_diagnostic_kind ON ingest_diagnostic(kind);

CREATE TABLE entity_conflict (
    id INTEGER PRIMARY KEY,
    unit_key TEXT,
    run_id TEXT,
    entity TEXT NOT NULL,
    entity_key TEXT NOT NULL,
    field TEXT NOT NULL,
    value_a TEXT,
    source_a TEXT,
    value_b TEXT,
    source_b TEXT,
    detail TEXT,
    ingested_at TEXT NOT NULL
);

CREATE TABLE entity_source (
    entity TEXT NOT NULL,
    entity_key TEXT NOT NULL,
    source_id INTEGER NOT NULL REFERENCES source_file(id),
    role TEXT NOT NULL,
    run_id TEXT,
    PRIMARY KEY (entity, entity_key, source_id, role)
);
"""

# Tables whose rows are owned by a run and deleted wholesale on re-ingest.
_RUN_OWNED_TABLES = (
    "logical_node", "execution", "execution_lifecycle", "validation",
    "review_finding", "review_producer_link", "finding_link", "preprocess",
    "commit_producer_link", "commit_record", "candidate",
    "candidate_execution_link", "human_decision", "entity_source",
)


def connect(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL)
    conn.execute("INSERT INTO meta(key, value) VALUES (?, ?)", ("schema_version", SCHEMA_VERSION))
    conn.execute("INSERT INTO meta(key, value) VALUES (?, ?)", ("identity_contract", IDENTITY_CONTRACT))


def _schema_version(conn: sqlite3.Connection) -> str | None:
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def _schema_ok(conn: sqlite3.Connection) -> bool:
    return _schema_version(conn) == SCHEMA_VERSION


# ---------------------------------------------------------------------------
# File classification (path only; no file read)
# ---------------------------------------------------------------------------

def classify(path: Path, stats_root: Path) -> tuple[str, str]:
    """Return ``(layout, source_type)`` from the path alone."""
    name = path.name
    parts = path.relative_to(stats_root).parts
    if path.suffix.lower() != ".json":
        if "LEDGER" in parts and name.endswith(".jsonl"):
            return "LEDGER", "LEDGER"
        if "LEDGER" in parts:
            return "SKIPPED", "DIAGNOSTIC"
        return "SKIPPED", "UNKNOWN"
    if "LEDGER" in parts:
        return "SKIPPED", "DIAGNOSTIC"
    if name.endswith("__sources.json"):
        return "SKIPPED", "FROZEN_INPUT"
    if "EXECUTIONS" in parts:
        return ("EXECUTION_DESCRIPTOR", "EXECUTION_DESCRIPTOR") if name.startswith("EXE_") \
            else ("UNKNOWN", "UNKNOWN")
    if "PREPROCESS" in parts:
        return "PREPROCESS", "PREPROCESS"
    if "WORKFLOW" in parts:
        if name == "workflow_state.json":
            return "WORKFLOW_STATE", "RAW_SEMANTIC"
        return "WORKFLOW_DETAIL", "RAW_SEMANTIC"
    if "CUSTOM_JOB" in parts:
        if name == "job_state.json":
            return "CUSTOM_JOB_STATE", "RAW_SEMANTIC"
        if name == "candidate.json":
            return "CANDIDATE", "CANDIDATE"
        if name.startswith("HDE_"):
            return "HUMAN_DECISION", "HUMAN_DECISION"
        if name == "frozen_job.json":
            return "FROZEN_JOB", "FROZEN_INPUT"
        return "CUSTOM_JOB_DETAIL", "RAW_SEMANTIC"
    if parts and re.match(r"^LOCAL_LLM", parts[0]):
        return "LOCAL_LLM_SMOKE", "DIAGNOSTIC"
    if name.endswith("_REPORT.json"):
        return "LOCAL_LLM_SMOKE", "DIAGNOSTIC"
    if name.startswith("00__CLASSIFIER__"):
        return "CLASSIFIER", "RAW_SEMANTIC"
    if re.match(r"^00__N\d+__", name):
        return "SINGLE_TASK_NODE", "RAW_SEMANTIC"
    if re.match(r"^N\d+__[A-Z_]+__", name):
        return "WORKFLOW_NODE_TELEMETRY", "RAW_SEMANTIC"
    if re.match(r"^run_\d{8}_.+__N", name):
        return "LEGACY_FLAT", "RAW_SEMANTIC"
    return "UNKNOWN", "UNKNOWN"


def _run_dir_kind(run_dir: Path) -> str:
    if (run_dir / "WORKFLOW" / "workflow_state.json").is_file():
        return "WORKFLOW"
    if (run_dir / "CUSTOM_JOB" / "job_state.json").is_file():
        return "CUSTOM_JOB"
    return "SINGLE_TASK"


# ---------------------------------------------------------------------------
# Per-run evidence builder
# ---------------------------------------------------------------------------

class SourceRef:
    """One source file participating in a run's evidence."""

    __slots__ = ("path", "rel_path", "sha256", "layout", "source_type", "schema_version", "source_id")

    def __init__(self, path: Path, rel_path: str, sha256: str, layout: str,
                 source_type: str, schema_version: str | None) -> None:
        self.path = path
        self.rel_path = rel_path
        self.sha256 = sha256
        self.layout = layout
        self.source_type = source_type
        self.schema_version = schema_version
        self.source_id: int | None = None

    def locator(self, pointer: str = "") -> str:
        return _locator(self.rel_path, self.sha256, pointer)


def _blank_execution(run_id: str) -> dict[str, Any]:
    return {
        "execution_id": None, "row_locator": None, "run_id": run_id,
        "node_id": None, "node_type": None, "subtask_id": None, "subtask_index": None,
        "invocation_kind": None, "llm_invocation": None, "llm_invocation_basis": UNKNOWN,
        "provider": None, "harness": None, "model": None, "effort": None, "profile": None,
        "provider_session_id": None, "retry_of_execution_id": None, "repair_cycle": None,
        "originating_review_execution_id": None, "repair_execution_id": None,
        "original_review_execution_id": None,
        "created_at": None, "started_at": None, "ended_at": None,
        "started_day": None, "started_day_basis": "NONE",
        "outcome": None, "outcome_source": None,
        "wall_time_s": None, "wall_time_basis": "NONE",
        "input_tokens": None, "cached_input_tokens": None, "output_tokens": None,
        "reasoning_tokens": None, "usage_observed": 0,
        "telemetry_status": None, "execution_mode": None,
        "input_contract_hash": None, "selection_reason": None, "policy_version": None,
        "descriptor_status": None,
        "identity_authority": "LEGACY_NODE_TELEMETRY", "identity_contract": None,
        "evidence_grain": LEGACY, "fixture_class": UNKNOWN, "fixture_evidence": "NONE",
        "conflict": 0, "source_id": None,
        "_declared_fixture_class": None,
    }


_IDENTITY_FIELDS = (
    "node_id", "subtask_id", "invocation_kind", "provider", "harness",
    "model", "effort", "profile", "created_at", "retry_of_execution_id",
    "input_contract_hash", "selection_reason", "policy_version", "descriptor_status",
)
_COMPARED_IDENTITY_FIELDS = (
    "node_id", "subtask_id", "invocation_kind", "provider", "harness",
    "model", "effort", "created_at", "retry_of_execution_id",
)


class RunBuilder:
    """Accumulates one run's derived rows with explicit source precedence."""

    def __init__(self, run_id: str, unit_key: str) -> None:
        self.run_id = run_id
        self.unit_key = unit_key
        self.run: dict[str, Any] = {
            "run_id": run_id, "unit_key": unit_key, "source_id": None,
            "job_class": UNKNOWN, "workflow_or_job_id": None, "goal": None,
            "repository": None, "worktree": None,
            "started_at": None, "ended_at": None, "started_day": None,
            "final_status": UNKNOWN, "final_status_basis": "NO_EXPLICIT_RUN_STATUS",
            "human_gate_reached": 0, "human_verdict": None,
            "evidence_grain": UNKNOWN, "evidence_class": UNKNOWN,
            "fixture_class": UNKNOWN, "fixture_evidence": "NONE",
            "identity_contract": None, "ledger_present": 0, "ledger_schema_version": None,
            "run_authority": UNKNOWN,
        }
        self.nodes: dict[tuple[str, str], dict[str, Any]] = {}
        self.executions: dict[str, dict[str, Any]] = {}     # key -> row
        self.by_execution_id: dict[str, dict[str, Any]] = {}
        self.lifecycle: dict[str, dict[str, Any]] = {}
        self.validations: list[dict[str, Any]] = []
        self.findings: dict[tuple[str, str], dict[str, Any]] = {}
        self.producer_links: dict[tuple[str, str], dict[str, Any]] = {}
        self.finding_links: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        self.preprocess: list[dict[str, Any]] = []
        self.commits: dict[tuple[str, str], dict[str, Any]] = {}
        self.commit_producers: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.candidates: dict[str, dict[str, Any]] = {}
        self.candidate_links: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.human_decisions: dict[str, dict[str, Any]] = {}
        self.entity_sources: dict[tuple[str, str, int, str], dict[str, Any]] = {}
        self.conflicts: list[dict[str, Any]] = []
        self.diagnostics: list[dict[str, Any]] = []
        self._declared_job_id: Any = None

    # -- diagnostics ------------------------------------------------------
    def diag(self, kind: str, detail: str, path: Path | None = None) -> None:
        self.diagnostics.append({
            "unit_key": self.unit_key, "run_id": self.run_id,
            "path": str(path) if path else None, "kind": kind, "detail": detail,
        })

    def conflict(self, entity: str, entity_key: str, field: str, value_a: Any,
                 source_a: str, value_b: Any, source_b: str, detail: str) -> None:
        self.conflicts.append({
            "unit_key": self.unit_key, "run_id": self.run_id, "entity": entity,
            "entity_key": entity_key, "field": field,
            "value_a": _text(value_a), "source_a": source_a,
            "value_b": _text(value_b), "source_b": source_b, "detail": detail,
        })

    def link_source(self, entity: str, entity_key: str, ref: SourceRef, role: str) -> None:
        if ref.source_id is None:
            return
        key = (entity, entity_key, ref.source_id, role)
        self.entity_sources[key] = {
            "entity": entity, "entity_key": entity_key, "source_id": ref.source_id,
            "role": role, "run_id": self.run_id,
        }

    # -- nodes ------------------------------------------------------------
    def node(self, *, node_id: str, node_type: Any = None, subtask_id: Any = None,
             subtask_index: Any = None, outcome: Any = None, repair_cycle: Any = None,
             wall_time_s: Any = None, started_at: Any = None, ended_at: Any = None,
             review_independence: Any = None, evidence_grain: str = LEGACY,
             ref: SourceRef | None = None) -> None:
        index = _as_int(subtask_index)
        subtask_key = str(subtask_id) if subtask_id else (str(index) if index is not None else "-")
        key = (str(node_id), subtask_key)
        row = self.nodes.get(key)
        if row is None:
            row = {
                "run_id": self.run_id, "node_id": str(node_id), "subtask_key": subtask_key,
                "node_type": None, "subtask_id": _text(subtask_id), "subtask_index": index,
                "outcome": None, "repair_cycle": None, "wall_time_s": None,
                "started_at": None, "ended_at": None, "review_independence": None,
                "workflow_or_job_id": self.run.get("workflow_or_job_id"),
                "evidence_grain": evidence_grain, "source_id": None,
            }
            self.nodes[key] = row
        for field, value in (
            ("node_type", _text(node_type)), ("outcome", _norm_outcome(outcome)),
            ("repair_cycle", _as_int(repair_cycle)), ("wall_time_s", _as_float(wall_time_s)),
            ("started_at", _text(started_at)), ("ended_at", _text(ended_at)),
            ("review_independence", _text(review_independence)),
        ):
            if row.get(field) is None and value is not None:
                row[field] = value
        if evidence_grain == MODERN:
            row["evidence_grain"] = MODERN
        if ref is not None and ref.source_id is not None and row["source_id"] is None:
            row["source_id"] = ref.source_id

    # -- executions -------------------------------------------------------
    def execution_by_id(self, execution_id: str) -> dict[str, Any] | None:
        return self.by_execution_id.get(execution_id)

    def upsert_modern_execution(self, execution_id: str, updates: Mapping[str, Any], *,
                                authority: str, ref: SourceRef, pointer: str,
                                identity_contract: str | None) -> dict[str, Any]:
        """Insert or refine one modern execution row under explicit precedence."""
        row = self.by_execution_id.get(execution_id)
        new_rank = IDENTITY_AUTHORITIES.get(authority, 99)
        if row is None:
            row = _blank_execution(self.run_id)
            row["execution_id"] = execution_id
            row["row_locator"] = ref.locator(pointer)
            row["identity_authority"] = authority
            row["identity_contract"] = identity_contract
            row["evidence_grain"] = MODERN
            row["source_id"] = ref.source_id
            self.by_execution_id[execution_id] = row
            self.executions[execution_id] = row
            for field in _IDENTITY_FIELDS:
                if field in updates:
                    row[field] = updates[field]
            row["_declared_fixture_class"] = updates.get("_declared_fixture_class")
            self.link_source("execution", execution_id, ref, authority)
            return row

        current_rank = IDENTITY_AUTHORITIES.get(row["identity_authority"], 99)
        for field in _IDENTITY_FIELDS:
            if field not in updates:
                continue
            incoming = updates[field]
            existing = row.get(field)
            if field in _COMPARED_IDENTITY_FIELDS and existing is not None and incoming is not None \
                    and _text(existing) != _text(incoming):
                row["conflict"] = 1
                self.conflict("execution", execution_id, field, existing,
                              row["identity_authority"], incoming, authority,
                              "incompatible identity/provenance across authoritative sources")
                if new_rank < current_rank:
                    row[field] = incoming
                continue
            if existing is None and incoming is not None:
                row[field] = incoming
            elif new_rank < current_rank and incoming is not None:
                row[field] = incoming
        if new_rank < current_rank:
            row["identity_authority"] = authority
            row["identity_contract"] = identity_contract or row["identity_contract"]
            row["row_locator"] = ref.locator(pointer)
            row["source_id"] = ref.source_id
        if row.get("_declared_fixture_class") is None:
            row["_declared_fixture_class"] = updates.get("_declared_fixture_class")
        self.link_source("execution", execution_id, ref, authority)
        return row

    def add_legacy_execution(self, updates: Mapping[str, Any], *, ref: SourceRef,
                             pointer: str) -> dict[str, Any]:
        """Legacy source-grain row. execution_id stays NULL — never synthesised."""
        row = _blank_execution(self.run_id)
        row["row_locator"] = ref.locator(pointer)
        row["identity_authority"] = "LEGACY_NODE_TELEMETRY"
        row["evidence_grain"] = LEGACY
        row["source_id"] = ref.source_id
        row.update({k: v for k, v in updates.items() if k in row})
        if row["row_locator"] in self.executions:
            self.diag("DUPLICATE_ROW_LOCATOR", f"legacy locator collision: {row['row_locator']}")
            return self.executions[row["row_locator"]]
        self.executions[row["row_locator"]] = row
        self.link_source("execution", row["row_locator"], ref, "LEGACY_NODE_TELEMETRY")
        return row

    def observe_usage(self, row: dict[str, Any], telemetry: Mapping[str, Any], *,
                      usage: Mapping[str, Any], source_label: str, ref: SourceRef) -> None:
        """Telemetry is authority for usage / wall time / provider session."""
        tokens = {
            "input_tokens": _as_int(usage.get("input_tokens")),
            "cached_input_tokens": _as_int(usage.get("cached_input_tokens", usage.get("cached_input"))),
            "output_tokens": _as_int(usage.get("output_tokens")),
            "reasoning_tokens": _as_int(usage.get("reasoning_tokens")),
        }
        for field, value in tokens.items():
            if value is not None:
                row[field] = value
        if any(value is not None for value in tokens.values()):
            row["usage_observed"] = 1
        for field, value in (
            ("provider_session_id", _text(telemetry.get("provider_session_id")
                                          or telemetry.get("provider_session")
                                          or telemetry.get("thread_id"))),
            ("telemetry_status", _text(telemetry.get("telemetry_status"))),
            ("execution_mode", _text(telemetry.get("execution_mode"))),
            ("node_type", _text(telemetry.get("node_type") or telemetry.get("workflow_node_type"))),
            ("started_at", _text(telemetry.get("started_at"))),
            ("ended_at", _text(telemetry.get("ended_at"))),
            ("harness", _text(telemetry.get("harness") or telemetry.get("agent"))),
            ("model", _text(telemetry.get("model"))),
            ("effort", _text(telemetry.get("effort"))),
            ("profile", _text(telemetry.get("implementer_profile"))),
            ("subtask_index", _as_int(telemetry.get("subtask_index"))),
            ("repair_cycle", _as_int(telemetry.get("repair_cycle"))),
        ):
            if value is not None and row.get(field) is None:
                row[field] = value
        wall = _as_float(telemetry.get("wall_time_s"))
        if wall is not None and row.get("wall_time_s") is None:
            row["wall_time_s"] = wall
            row["wall_time_basis"] = source_label
        if ref.source_id is not None and row.get("execution_id"):
            self.link_source("execution", row["execution_id"], ref, "TELEMETRY")

    def set_outcome(self, row: dict[str, Any], outcome: Any, source: str) -> None:
        value = _norm_outcome(outcome)
        if value is None:
            return
        existing = row.get("outcome")
        if existing is not None and existing != value:
            row["conflict"] = 1
            self.conflict("execution", row.get("execution_id") or row["row_locator"],
                          "outcome", existing, row.get("outcome_source") or UNKNOWN,
                          value, source, "conflicting semantic outcome for one execution")
            return
        row["outcome"] = value
        row["outcome_source"] = source


# ---------------------------------------------------------------------------
# Source parsers — each writes into a RunBuilder under its own authority
# ---------------------------------------------------------------------------

def ingest_descriptor(builder: RunBuilder, data: Mapping[str, Any], ref: SourceRef) -> None:
    """V0.4A execution descriptor: THE authority for identity and provenance."""
    execution_id = _text(data.get("execution_id"))
    if not execution_id or not execution_id.startswith("EXE_"):
        raise ValueError("execution descriptor without EXE_ identity")
    declared_run = _text(data.get("run_id"))
    if declared_run and declared_run != builder.run_id:
        builder.conflict("execution", execution_id, "run_id", builder.run_id,
                         "RUN_DIRECTORY", declared_run, "EXECUTION_DESCRIPTOR",
                         "descriptor declares a different run identity")
        builder.diag("DATA_INTEGRITY_ERROR",
                     f"descriptor {execution_id} declares run_id {declared_run} "
                     f"inside run directory {builder.run_id}", ref.path)
        return
    updates = {
        "node_id": _text(data.get("node_id")),
        "subtask_id": _text(data.get("subtask_id")),
        "invocation_kind": _text(data.get("invocation_kind")),
        "provider": _text(data.get("provider")),
        "harness": _text(data.get("harness")),
        "model": _text(data.get("model")),
        "effort": _text(data.get("effort")),
        "profile": _text(data.get("profile")),
        "created_at": _text(data.get("created_at")),
        "retry_of_execution_id": _text(data.get("retry_of_execution_id")),
        "input_contract_hash": _text(data.get("input_contract_hash")),
        "selection_reason": _text(data.get("selection_reason")),
        "policy_version": _text(data.get("policy_version")),
        "descriptor_status": _text(data.get("status")),
        "_declared_fixture_class": _text(data.get("fixture_class")),
    }
    row = builder.upsert_modern_execution(
        execution_id, updates, authority="EXECUTION_DESCRIPTOR", ref=ref, pointer="",
        identity_contract=_text(data.get("schema_version")))
    if row.get("provider_session_id") is None:
        row["provider_session_id"] = _text(data.get("provider_session_id"))

    relations = data.get("relations") if isinstance(data.get("relations"), Mapping) else {}
    # REVIEW: producer executions are explicit, never inferred from ordering.
    for produced in relations.get("reviewed_execution_ids") or []:
        produced_id = _text(produced)
        if not produced_id:
            continue
        builder.producer_links[(execution_id, produced_id)] = {
            "review_execution_id": execution_id, "produced_execution_id": produced_id,
            "run_id": builder.run_id, "source_id": ref.source_id,
        }
    # REPAIR: originating review + selected finding keys. Never a retry relation.
    originating = _text(relations.get("originating_review_execution_id"))
    if originating:
        row["originating_review_execution_id"] = originating
    for key in relations.get("selected_finding_keys") or []:
        if not isinstance(key, Mapping):
            continue
        review_id = _text(key.get("review_execution_id"))
        finding_id = _text(key.get("finding_id"))
        if not review_id or not finding_id:
            continue
        builder.finding_links[(review_id, finding_id, execution_id, "REPAIR_SELECTED")] = {
            "review_execution_id": review_id, "finding_id": finding_id,
            "linked_execution_id": execution_id, "link_role": "REPAIR_SELECTED",
            "disposition": "SELECTED_FOR_REPAIR", "run_id": builder.run_id,
            "source_id": ref.source_id,
        }
    # DELTA_REVIEW: names the repair, the original review and finding dispositions.
    if _text(relations.get("repair_execution_id")):
        row["repair_execution_id"] = _text(relations.get("repair_execution_id"))
    if _text(relations.get("original_review_execution_id")):
        row["original_review_execution_id"] = _text(relations.get("original_review_execution_id"))
    for disposition in relations.get("finding_dispositions") or []:
        if not isinstance(disposition, Mapping):
            continue
        key = disposition.get("finding_key") if isinstance(disposition.get("finding_key"), Mapping) else {}
        review_id = _text(key.get("review_execution_id"))
        finding_id = _text(key.get("finding_id"))
        if not review_id or not finding_id:
            continue
        builder.finding_links[(review_id, finding_id, execution_id, "DELTA_DISPOSITION")] = {
            "review_execution_id": review_id, "finding_id": finding_id,
            "linked_execution_id": execution_id, "link_role": "DELTA_DISPOSITION",
            "disposition": _text(disposition.get("disposition")), "run_id": builder.run_id,
            "source_id": ref.source_id,
        }


def _record_commit(builder: RunBuilder, payload: Mapping[str, Any], ref: SourceRef,
                   *, observed_in: str) -> None:
    """Commit identity is (repository, commit_hash) — never the hash alone.

    A commit that appears in both job state and the ledger is ONE entity; the
    two observations are cross-validated instead of counted twice.
    """
    repository = _text(payload.get("repository"))
    commit_hash = _text(payload.get("commit_hash"))
    if not repository or not commit_hash:
        return
    key = (repository, commit_hash)
    row = builder.commits.get(key)
    incoming = {
        "repository": repository, "commit_hash": commit_hash, "run_id": builder.run_id,
        "subtask_id": _text(payload.get("subtask_id")), "role": _text(payload.get("role")),
        "expected_parent": _text(payload.get("expected_parent")),
        "git_evidence_hash": _text(payload.get("git_evidence_hash")),
    }
    if row is None:
        row = {**incoming, "observed_in_state": 0, "observed_in_ledger": 0,
               "conflict": 0, "source_id": ref.source_id}
        builder.commits[key] = row
    else:
        for field in ("subtask_id", "role", "expected_parent"):
            existing, value = row.get(field), incoming.get(field)
            if existing is not None and value is not None and existing != value:
                row["conflict"] = 1
                builder.conflict("commit_record", f"{repository}@{commit_hash}", field,
                                 existing, "PRIOR_OBSERVATION", value, observed_in,
                                 "incompatible commit attribution across sources")
            elif existing is None and value is not None:
                row[field] = value
        if row.get("git_evidence_hash") is None and incoming.get("git_evidence_hash"):
            row["git_evidence_hash"] = incoming["git_evidence_hash"]
    if observed_in == "STATE":
        row["observed_in_state"] = 1
    elif observed_in == "LEDGER":
        row["observed_in_ledger"] = 1
    builder.link_source("commit_record", f"{repository}@{commit_hash}", ref, observed_in)
    producers = payload.get("producer_execution_ids") or []
    seen: set[str] = set()
    for producer in producers:
        producer_id = _text(producer)
        if not producer_id:
            continue
        seen.add(producer_id)
        builder.commit_producers[(repository, commit_hash, producer_id)] = {
            "repository": repository, "commit_hash": commit_hash,
            "execution_id": producer_id, "run_id": builder.run_id,
        }
    existing_producers = {k[2] for k in builder.commit_producers if k[0] == repository and k[1] == commit_hash}
    if seen and existing_producers - seen:
        row["conflict"] = 1
        builder.conflict("commit_record", f"{repository}@{commit_hash}", "producer_execution_ids",
                         sorted(existing_producers - seen), "PRIOR_OBSERVATION",
                         sorted(seen), observed_in, "incompatible producer attribution")


def _record_candidate(builder: RunBuilder, data: Mapping[str, Any], ref: SourceRef,
                      *, authority: str) -> None:
    candidate_id = _text(data.get("candidate_id"))
    if not candidate_id:
        return
    row = builder.candidates.get(candidate_id)
    incoming = {
        "candidate_id": candidate_id, "run_id": builder.run_id,
        "repository": _text(data.get("repository")), "worktree": _text(data.get("worktree")),
        "candidate_head": _text(data.get("candidate_head")),
        "artifact_manifest": _text(data.get("artifact_manifest")),
        "content_identity_hash": _text(data.get("content_identity_hash")),
        "created_at": _text(data.get("created_at")),
    }
    if row is None:
        builder.candidates[candidate_id] = {**incoming, "source_authority": authority,
                                            "conflict": 0, "source_id": ref.source_id}
        row = builder.candidates[candidate_id]
    else:
        # Changed content means a DIFFERENT candidate identity; the same id with
        # a different hash is an integrity error, not an update.
        for field in ("candidate_head", "content_identity_hash"):
            existing, value = row.get(field), incoming.get(field)
            if existing is not None and value is not None and existing != value:
                row["conflict"] = 1
                builder.conflict("candidate", candidate_id, field, existing,
                                 row["source_authority"], value, authority,
                                 "same candidate_id with different content identity")
                builder.diag("DATA_INTEGRITY_ERROR",
                             f"candidate {candidate_id} has conflicting {field}", ref.path)
            elif existing is None and value is not None:
                row[field] = value
        if authority == "CANDIDATE_ARTIFACT":
            row["source_authority"] = authority
            row["source_id"] = ref.source_id
    builder.link_source("candidate", candidate_id, ref, authority)
    for role, key in (("REVIEW", "review_execution_ids"), ("CHECK", "check_execution_ids")):
        for execution in data.get(key) or []:
            execution_id = _text(execution)
            if not execution_id:
                continue
            builder.candidate_links[(candidate_id, execution_id, role)] = {
                "candidate_id": candidate_id, "execution_id": execution_id,
                "link_role": role, "run_id": builder.run_id,
            }


def _record_human_decision(builder: RunBuilder, data: Mapping[str, Any], ref: SourceRef,
                           *, authority: str) -> None:
    decision_id = _text(data.get("human_decision_id"))
    if not decision_id:
        return
    incoming = {
        "human_decision_id": decision_id,
        "candidate_id": _text(data.get("candidate_id")),
        "run_id": builder.run_id,
        "verdict": _norm_verdict(data.get("verdict")),
        "quality_assessment": _norm_quality(data.get("quality_assessment")),
        "reason": _text(data.get("reason")),
        "recorded_at": _text(data.get("timestamp") or data.get("recorded_at")),
        "artifact_path": _text(data.get("artifact_path") or data.get("decision_artifact_path")),
        "artifact_hash": _text(data.get("decision_artifact_hash")),
    }
    row = builder.human_decisions.get(decision_id)
    rank = {"HUMAN_DECISION_ARTIFACT": 1, "STATE_HUMAN_DECISIONS": 2, "LEDGER_OBSERVATION": 3}
    if row is None:
        builder.human_decisions[decision_id] = {**incoming, "source_authority": authority,
                                                "candidate_resolved": 0, "conflict": 0,
                                                "source_id": ref.source_id}
    else:
        for field in ("candidate_id", "verdict", "quality_assessment"):
            existing, value = row.get(field), incoming.get(field)
            if existing is not None and value is not None and existing != value:
                row["conflict"] = 1
                builder.conflict("human_decision", decision_id, field, existing,
                                 row["source_authority"], value, authority,
                                 "conflicting human decision detail across sources")
            elif existing is None and value is not None:
                row[field] = value
        for field in ("reason", "recorded_at", "artifact_path", "artifact_hash"):
            if row.get(field) is None and incoming.get(field) is not None:
                row[field] = incoming[field]
        if rank.get(authority, 9) < rank.get(row["source_authority"], 9):
            row["source_authority"] = authority
            row["source_id"] = ref.source_id
    builder.link_source("human_decision", decision_id, ref, authority)


def ingest_custom_job_state(builder: RunBuilder, data: Mapping[str, Any], ref: SourceRef) -> None:
    """Custom Job state: authority for semantic node/review results and job identity."""
    run_id = _text(data.get("AAW_RUN_ID") or data.get("run_id"))
    if not run_id:
        raise ValueError("job_state without run_id")
    schema_version = _text(data.get("schema_version"))
    modern = _is_v04a(schema_version)
    grain = MODERN if modern else LEGACY
    status = data.get("final_acceptance") or data.get("status")
    verdict = _norm_verdict(data.get("human_verdict"))
    builder._declared_job_id = _text(data.get("job_id"))
    builder.run.update({
        "source_id": ref.source_id, "job_class": "CUSTOM_JOB",
        "workflow_or_job_id": _text(data.get("job_id")), "goal": _text(data.get("goal")),
        "repository": _text(data.get("repository")), "worktree": _text(data.get("worktree")),
        "started_at": _text(data.get("started_at")), "ended_at": _text(data.get("updated_at")),
        "final_status": _norm_run_status(status),
        "final_status_basis": "STATE_FINAL_ACCEPTANCE" if data.get("final_acceptance") else "STATE_STATUS",
        "human_verdict": verdict, "evidence_grain": grain,
        "identity_contract": IDENTITY_CONTRACT if modern else None,
        "ledger_schema_version": _text(data.get("ledger_schema_version")),
        "run_authority": "CUSTOM_JOB_STATE",
    })
    builder.run["human_gate_reached"] = int(
        _norm_run_status(status) == "WAITING_FOR_HUMAN"
        or verdict is not None
        or bool(data.get("candidate"))
        or bool(data.get("human_decisions")))
    builder.link_source("run", run_id, ref, "CUSTOM_JOB_STATE")

    # ---- pre-V0.4A outcome linkage through DECLARED keys --------------------
    # A V0.3 custom job has no execution identity, but both telemetry[] and the
    # semantic results declare `subtask_index`, and there is exactly one review /
    # delta-review block per job. Linking on those DECLARED keys inside one run is
    # an explicit identity join at source grain — not order, filename or timestamp.
    # Any ambiguity (a key declared twice, more than one row for a role) is left
    # UNKNOWN and diagnosed rather than guessed.
    legacy_subtask_outcome: dict[int, Any] = {}
    legacy_role_outcome: dict[str, Any] = {}
    if not modern:
        seen_indexes: set[int] = set()
        for entry in data.get("subtask_results") or []:
            if not isinstance(entry, Mapping):
                continue
            index_value = _as_int(entry.get("subtask_index"))
            if index_value is None:
                continue
            if index_value in seen_indexes:
                legacy_subtask_outcome.pop(index_value, None)
                builder.diag("AMBIGUOUS_LEGACY_SUBTASK_KEY",
                             f"subtask_index {index_value} declared more than once", ref.path)
                continue
            seen_indexes.add(index_value)
            legacy_subtask_outcome[index_value] = entry.get("outcome")
        role_counts: dict[str, int] = {}
        for entry in data.get("telemetry") or []:
            if isinstance(entry, Mapping) and not _text(entry.get("execution_id")):
                role_key = str(entry.get("node_type") or "")
                role_counts[role_key] = role_counts.get(role_key, 0) + 1
        for role_key, block_key in (("REVIEW", "review"), ("DELTA_REVIEW", "delta_review")):
            block = data.get(block_key)
            if role_counts.get(role_key) == 1 and isinstance(block, Mapping) and block.get("outcome"):
                legacy_role_outcome[role_key] = block.get("outcome")
            elif role_counts.get(role_key, 0) > 1:
                builder.diag("AMBIGUOUS_LEGACY_ROLE_KEY",
                             f"{role_counts[role_key]} legacy {role_key} telemetry rows; "
                             "no unambiguous declared link to the semantic result", ref.path)

    # ---- state executions[] : identity fallback below the descriptor -------
    for index, entry in enumerate(data.get("executions") or []):
        if not isinstance(entry, Mapping):
            continue
        execution_id = _text(entry.get("execution_id"))
        if not execution_id:
            continue
        builder.upsert_modern_execution(execution_id, {
            "node_id": _text(entry.get("node_id")),
            "subtask_id": _text(entry.get("subtask_id")),
            "invocation_kind": _text(entry.get("invocation_kind")),
            "provider": _text(entry.get("provider")), "harness": _text(entry.get("harness")),
            "model": _text(entry.get("model")), "effort": _text(entry.get("effort")),
            "profile": _text(entry.get("profile")), "created_at": _text(entry.get("created_at")),
            "retry_of_execution_id": _text(entry.get("retry_of_execution_id")),
        }, authority="STATE_EXECUTIONS", ref=ref, pointer=f"/executions/{index}",
            identity_contract=IDENTITY_CONTRACT if modern else None)

    # ---- telemetry : authority for usage / wall time / provider session ----
    for index, telemetry in enumerate(data.get("telemetry") or []):
        if not isinstance(telemetry, Mapping):
            continue
        execution_id = _text(telemetry.get("execution_id"))
        usage = {
            "input_tokens": telemetry.get("input_tokens"),
            "cached_input": telemetry.get("cached_input"),
            "output_tokens": telemetry.get("output_tokens"),
            "reasoning_tokens": telemetry.get("reasoning_tokens"),
        }
        if execution_id:
            row = builder.execution_by_id(execution_id)
            if row is None:
                row = builder.upsert_modern_execution(execution_id, {
                    "node_id": _text(telemetry.get("node_id")),
                    "subtask_id": _text(telemetry.get("subtask_id")),
                    "model": _text(telemetry.get("model")), "effort": _text(telemetry.get("effort")),
                    "harness": _text(telemetry.get("harness")), "provider": _text(telemetry.get("provider")),
                }, authority="STATE_EXECUTIONS", ref=ref, pointer=f"/telemetry/{index}",
                    identity_contract=IDENTITY_CONTRACT if modern else None)
            builder.observe_usage(row, telemetry, usage=usage,
                                  source_label="TELEMETRY_WALL_TIME", ref=ref)
        else:
            # Pre-V0.4A custom job: telemetry rows are the only grain available.
            index_value = _as_int(telemetry.get("subtask_index"))
            node_id = _text(telemetry.get("node_id")) or (
                f"S{index_value}" if index_value is not None else _text(telemetry.get("node_type")))
            row = builder.add_legacy_execution({
                "node_id": node_id, "node_type": _text(telemetry.get("node_type")),
                "subtask_id": _text(telemetry.get("subtask_id")), "subtask_index": index_value,
                "provider": _text(telemetry.get("provider")), "harness": _text(telemetry.get("harness")),
                "model": _text(telemetry.get("model")), "effort": _text(telemetry.get("effort")),
            }, ref=ref, pointer=f"/telemetry/{index}")
            builder.observe_usage(row, telemetry, usage=usage,
                                  source_label="TELEMETRY_WALL_TIME", ref=ref)
            role = str(telemetry.get("node_type") or "")
            if index_value is not None and index_value in legacy_subtask_outcome:
                builder.set_outcome(row, legacy_subtask_outcome[index_value],
                                    "SUBTASK_RESULT_DECLARED_SUBTASK_INDEX")
            elif role in legacy_role_outcome:
                builder.set_outcome(row, legacy_role_outcome[role],
                                    f"{role}_RESULT_SINGLE_DECLARED_ROLE")

    # ---- semantic outcomes : node result authority -------------------------
    for entry in data.get("subtask_results") or []:
        if not isinstance(entry, Mapping):
            continue
        index_value = _as_int(entry.get("subtask_index"))
        node_id = _text(entry.get("subtask_id")) or (
            f"S{index_value}" if index_value is not None else "SUBTASK")
        builder.node(node_id=node_id, node_type="SUBTASK", subtask_id=entry.get("subtask_id"),
                     subtask_index=index_value, outcome=entry.get("outcome"),
                     evidence_grain=grain, ref=ref)
        execution_id = _text(entry.get("execution_id"))
        if execution_id:
            row = builder.execution_by_id(execution_id)
            if row is not None:
                builder.set_outcome(row, entry.get("outcome"), "SUBTASK_RESULT")
        gate_result = _norm_outcome(entry.get("machine_gate_result"))
        record = entry.get("checkpoint_commit_record")
        if isinstance(record, Mapping):
            _record_commit(builder, record, ref, observed_in="STATE")
        if gate_result is None:
            continue

    for entry in data.get("commit_records") or []:
        if isinstance(entry, Mapping):
            _record_commit(builder, entry, ref, observed_in="STATE")

    for index, gate in enumerate(data.get("machine_gates") or []):
        if not isinstance(gate, Mapping):
            continue
        node_id = _text(gate.get("node_id")) or f"MACHINE_GATE:{index}"
        builder.node(node_id=node_id, node_type="MACHINE_GATE",
                     subtask_id=gate.get("subtask_id"), outcome=gate.get("result"),
                     wall_time_s=gate.get("wall_time_s"), evidence_grain=grain, ref=ref)
        execution_id = _text(gate.get("execution_id"))
        if not execution_id:
            continue
        row = builder.execution_by_id(execution_id)
        if row is None:
            row = builder.upsert_modern_execution(execution_id, {
                "node_id": node_id, "subtask_id": _text(gate.get("subtask_id")),
                "invocation_kind": "MACHINE_GATE",
            }, authority="STATE_EXECUTIONS", ref=ref, pointer=f"/machine_gates/{index}",
                identity_contract=IDENTITY_CONTRACT if modern else None)
        builder.set_outcome(row, gate.get("result"), "MACHINE_GATE_RESULT")
        wall = _as_float(gate.get("wall_time_s"))
        if wall is not None and row.get("wall_time_s") is None:
            row["wall_time_s"] = wall
            row["wall_time_basis"] = "MACHINE_GATE_RESULT"

    review = data.get("review") if isinstance(data.get("review"), Mapping) else {}
    delta = data.get("delta_review") if isinstance(data.get("delta_review"), Mapping) else {}
    review_execution_id = _text(review.get("execution_id")) if review else None
    if review:
        builder.node(node_id="REVIEW", node_type="REVIEW", outcome=review.get("outcome"),
                     evidence_grain=grain, ref=ref)
        if review_execution_id:
            row = builder.execution_by_id(review_execution_id)
            if row is not None:
                builder.set_outcome(row, review.get("outcome"), "REVIEW_RESULT")
            for produced in review.get("reviewed_execution_ids") or []:
                produced_id = _text(produced)
                if produced_id:
                    builder.producer_links.setdefault((review_execution_id, produced_id), {
                        "review_execution_id": review_execution_id,
                        "produced_execution_id": produced_id,
                        "run_id": builder.run_id, "source_id": ref.source_id,
                    })
        for finding in review.get("findings") or []:
            if not isinstance(finding, Mapping):
                continue
            key = finding.get("finding_key") if isinstance(finding.get("finding_key"), Mapping) else {}
            owner = _text(key.get("review_execution_id")) or review_execution_id
            finding_id = _text(key.get("finding_id")) or _text(finding.get("finding_id"))
            if not owner or not finding_id:
                builder.diag("UNLINKED_FINDING",
                             "finding without a (review_execution_id, finding_id) key", ref.path)
                continue
            builder.findings[(owner, finding_id)] = {
                "review_execution_id": owner, "finding_id": finding_id, "run_id": builder.run_id,
                "severity": _text(finding.get("severity")), "file": _text(finding.get("file")),
                "location": _text(finding.get("location")),
                "description": _text(finding.get("description")),
                "required_fix": _text(finding.get("required_fix")),
                "commit_hash": _text(finding.get("commit")),
                "source_id": ref.source_id,
            }
    if delta:
        builder.node(node_id="DELTA_REVIEW", node_type="DELTA_REVIEW",
                     outcome=delta.get("outcome"), evidence_grain=grain, ref=ref)
        delta_execution_id = _text(delta.get("execution_id"))
        if delta_execution_id:
            row = builder.execution_by_id(delta_execution_id)
            if row is not None:
                builder.set_outcome(row, delta.get("outcome"), "DELTA_REVIEW_RESULT")
                if row.get("repair_execution_id") is None:
                    row["repair_execution_id"] = _text(delta.get("repair_execution_id"))
                if row.get("original_review_execution_id") is None:
                    row["original_review_execution_id"] = _text(delta.get("original_review_execution_id"))

    # selected_repairs holds bare finding ids; the repair execution that owns
    # them comes from the descriptor relation, never from ordering.
    selected = [_text(value) for value in (data.get("selected_repairs") or []) if _text(value)]
    if selected:
        repair_ids = [row["execution_id"] for row in builder.by_execution_id.values()
                      if row.get("invocation_kind") == "REPAIR" and row.get("execution_id")]
        if len(repair_ids) == 1 and review_execution_id:
            for finding_id in selected:
                builder.finding_links.setdefault(
                    (review_execution_id, finding_id, repair_ids[0], "REPAIR_SELECTED"), {
                        "review_execution_id": review_execution_id, "finding_id": finding_id,
                        "linked_execution_id": repair_ids[0], "link_role": "REPAIR_SELECTED",
                        "disposition": "SELECTED_FOR_REPAIR", "run_id": builder.run_id,
                        "source_id": ref.source_id,
                    })
        elif len(repair_ids) != 1:
            builder.diag("UNLINKED_REPAIR_SELECTION",
                         f"{len(selected)} selected findings but {len(repair_ids)} repair executions; "
                         "no unambiguous explicit link", ref.path)

    if isinstance(data.get("candidate"), Mapping):
        _record_candidate(builder, data["candidate"], ref, authority="STATE_CANDIDATE")
    for entry in data.get("human_decisions") or []:
        if isinstance(entry, Mapping):
            _record_human_decision(builder, entry, ref, authority="STATE_HUMAN_DECISIONS")
    for entry in data.get("lifecycle_records") or []:
        if isinstance(entry, Mapping) and entry.get("requires_reconciliation"):
            builder.diag("REQUIRES_RECONCILIATION",
                         f"state marks {entry.get('execution_id')} as requiring reconciliation",
                         ref.path)

    review_outcome = _norm_outcome(review.get("outcome")) if review else None
    findings_count = _as_int(data.get("review_findings_count"))
    repair_required = None
    if review_outcome is not None:
        repair_required = 1 if (review_outcome != "PASS" or (findings_count or 0) > 0
                                or bool(selected)) else 0
    builder.validations.append({
        "run_id": builder.run_id, "machine_gate_result": None,
        "reviewer_result": review_outcome, "repair_required": repair_required,
        "repair_cycles": 1 if data.get("repair_commit") else 0,
        "delta_review_result": _norm_outcome(delta.get("outcome")) if delta else None,
        "evidence_grain": grain, "basis": "CUSTOM_JOB_STATE", "source_id": ref.source_id,
    })


def ingest_workflow_state(builder: RunBuilder, data: Mapping[str, Any], ref: SourceRef) -> None:
    run_id = _text(data.get("AAW_RUN_ID") or data.get("run_id"))
    if not run_id:
        raise ValueError("workflow_state without run_id")
    schema_version = _text(data.get("schema_version"))
    modern = _is_v04a(schema_version) or bool(data.get("executions"))
    grain = MODERN if modern else LEGACY
    status = data.get("final_outcome") or data.get("status")
    completed = [node for node in (data.get("completed_nodes") or []) if isinstance(node, Mapping)]
    human_gate = any(node.get("node_type") == "HUMAN_GATE" for node in completed) \
        or _norm_run_status(status) == "WAITING_FOR_HUMAN"
    builder.run.update({
        "source_id": ref.source_id, "job_class": "WORKFLOW",
        "workflow_or_job_id": _text(data.get("workflow_id")), "goal": _text(data.get("goal")),
        "repository": _text(data.get("repo")), "worktree": _text(data.get("worktree")),
        "started_at": _text(data.get("started_at")), "ended_at": _text(data.get("updated_at")),
        "final_status": _norm_run_status(status),
        "final_status_basis": "STATE_FINAL_OUTCOME" if data.get("final_outcome") else "STATE_STATUS",
        "human_gate_reached": int(bool(human_gate)),
        "human_verdict": _norm_verdict(data.get("human_verdict")),
        "evidence_grain": grain,
        "identity_contract": IDENTITY_CONTRACT if modern else None,
        "run_authority": "WORKFLOW_STATE",
    })
    builder.link_source("run", run_id, ref, "WORKFLOW_STATE")

    machine = None
    reviewer = None
    repair_cycles = _as_int(data.get("repair_cycle")) or 0
    # The workflow node result is the semantic verdict authority. A telemetry
    # `outcome` describes provider-response validity, which is a different fact:
    # where the two disagree the node result wins and the disagreement is
    # recorded as an entity conflict rather than silently averaged away.
    node_outcomes: dict[str, Any] = {}
    for node in completed:
        node_id = _text(node.get("node_id")) or "N00"
        node_type = _text(node.get("node_type"))
        outcome = _norm_outcome(node.get("outcome"))
        if outcome is not None:
            node_outcomes[node_id] = outcome
        builder.node(node_id=node_id, node_type=node_type, outcome=outcome,
                     repair_cycle=node.get("repair_cycle"), wall_time_s=node.get("duration_s"),
                     evidence_grain=grain, ref=ref)
        if node_type == "MACHINE_GATE":
            machine = outcome
        elif node_type == "REVIEW":
            reviewer = outcome
        elif node_type == "REPAIR":
            repair_cycles = max(repair_cycles, _as_int(node.get("repair_cycle")) or 0)

    for index, entry in enumerate(data.get("executions") or []):
        if not isinstance(entry, Mapping) or not _text(entry.get("execution_id")):
            continue
        builder.upsert_modern_execution(_text(entry["execution_id"]), {
            "node_id": _text(entry.get("node_id")),
            "subtask_id": _text(entry.get("subtask_id")),
            "invocation_kind": _text(entry.get("invocation_kind")),
            "provider": _text(entry.get("provider")), "harness": _text(entry.get("harness")),
            "model": _text(entry.get("model")), "effort": _text(entry.get("effort")),
            "profile": _text(entry.get("profile")), "created_at": _text(entry.get("created_at")),
            "retry_of_execution_id": _text(entry.get("retry_of_execution_id")),
        }, authority="STATE_EXECUTIONS", ref=ref, pointer=f"/executions/{index}",
            identity_contract=IDENTITY_CONTRACT if modern else None)

    for index, telemetry in enumerate(data.get("telemetry") or []):
        if not isinstance(telemetry, Mapping):
            continue
        usage = telemetry.get("usage") if isinstance(telemetry.get("usage"), Mapping) else {}
        execution_id = _text(telemetry.get("execution_id"))
        node_id = _text(telemetry.get("workflow_node_id") or telemetry.get("node"))
        if execution_id:
            row = builder.execution_by_id(execution_id)
            if row is None:
                row = builder.upsert_modern_execution(execution_id, {
                    "node_id": node_id,
                }, authority="STATE_EXECUTIONS", ref=ref, pointer=f"/telemetry/{index}",
                    identity_contract=IDENTITY_CONTRACT if modern else None)
        else:
            row = builder.add_legacy_execution({
                "node_id": node_id,
                "node_type": _text(telemetry.get("workflow_node_type")),
            }, ref=ref, pointer=f"/telemetry/{index}")
        builder.observe_usage(row, telemetry, usage=usage,
                              source_label="TELEMETRY_WALL_TIME", ref=ref)
        if node_id and node_id in node_outcomes:
            builder.set_outcome(row, node_outcomes[node_id], "WORKFLOW_NODE_RESULT")
        builder.set_outcome(row, telemetry.get("outcome"), "NODE_TELEMETRY_OUTCOME")

    repair_required = None
    if reviewer is not None:
        repair_required = 1 if (reviewer != "PASS" or repair_cycles > 0) else 0
    builder.validations.append({
        "run_id": builder.run_id, "machine_gate_result": machine, "reviewer_result": reviewer,
        "repair_required": repair_required, "repair_cycles": repair_cycles,
        "delta_review_result": None, "evidence_grain": grain,
        "basis": "WORKFLOW_STATE", "source_id": ref.source_id,
    })


def ingest_single_task_nodes(builder: RunBuilder,
                             node_files: Sequence[tuple[SourceRef, Mapping[str, Any]]]) -> None:
    """SINGLE_TASK / CLASSIFIER run: node telemetry files are the only authority."""
    goal = None
    started: list[str] = []
    ended: list[str] = []
    job_class = "SINGLE_TASK"
    final_outcome: str | None = None
    last_node_num = -1
    human_gate = False
    authority_ref: SourceRef | None = None
    for ref, data in node_files:
        node_id = _text(data.get("node") or data.get("stage")) or "N00"
        node_id = node_id.replace("00__", "")
        node_type = node_id.split("__")[-1] if "__" in node_id else node_id
        is_classifier = "CLASSIFIER" in node_id.upper()
        if is_classifier and job_class == "SINGLE_TASK":
            job_class = "CLASSIFIER"
        modern = bool(_text(data.get("execution_id")))
        grain = MODERN if modern else LEGACY
        usage = data.get("usage") if isinstance(data.get("usage"), Mapping) else {
            "input_tokens": data.get("input_tokens"),
            "cached_input_tokens": data.get("cached_input_tokens"),
            "output_tokens": data.get("output_tokens"),
            "reasoning_tokens": data.get("reasoning_tokens"),
        }
        goal = goal or _text(data.get("task_short"))
        if data.get("started_at"):
            started.append(str(data["started_at"]))
        if data.get("ended_at"):
            ended.append(str(data["ended_at"]))
        outcome = _norm_outcome(data.get("outcome"))
        match = re.search(r"N(\d+)", node_id)
        number = int(match.group(1)) if match else 0
        if not is_classifier and number >= last_node_num:
            last_node_num, final_outcome = number, outcome
            authority_ref = ref
        if "N05" in node_id or "HUMAN" in node_id.upper():
            human_gate = True
        if not is_classifier:
            builder.node(node_id=node_id, node_type=node_type, outcome=outcome,
                         repair_cycle=data.get("repair_cycle"),
                         wall_time_s=data.get("wall_time_s"),
                         started_at=data.get("started_at"), ended_at=data.get("ended_at"),
                         review_independence=data.get("review_independence"),
                         evidence_grain=grain, ref=ref)
        updates = {
            "node_id": node_id, "node_type": node_type,
            "subtask_id": _text(data.get("subtask_id")),
            "invocation_kind": _text(data.get("invocation_kind")),
            "harness": _text(data.get("agent") or data.get("backend") or data.get("harness")),
            "model": _text(data.get("model")), "effort": _text(data.get("effort")),
            "profile": _text(data.get("implementer_profile")),
        }
        if modern:
            row = builder.upsert_modern_execution(_text(data["execution_id"]), updates,
                                                  authority="STATE_EXECUTIONS", ref=ref,
                                                  pointer="", identity_contract=IDENTITY_CONTRACT)
        else:
            row = builder.add_legacy_execution(updates, ref=ref, pointer="")
        builder.observe_usage(row, data, usage=usage or {},
                              source_label="NODE_TELEMETRY_WALL_TIME", ref=ref)
        builder.set_outcome(row, data.get("outcome"), "NODE_TELEMETRY_OUTCOME")

    builder.run.update({
        "source_id": authority_ref.source_id if authority_ref else (
            node_files[0][0].source_id if node_files else None),
        "job_class": job_class, "goal": goal,
        "started_at": min(started) if started else None,
        "ended_at": max(ended) if ended else None,
        "final_status": final_outcome or UNKNOWN,
        "final_status_basis": "TERMINAL_NODE_TELEMETRY_OUTCOME" if final_outcome
        else "NO_EXPLICIT_RUN_STATUS",
        "human_gate_reached": int(human_gate),
        "evidence_grain": MODERN if any(row.get("evidence_grain") == MODERN
                                        for row in builder.executions.values()) else LEGACY,
        "run_authority": "SINGLE_TASK_NODE_TELEMETRY",
    })


def ingest_legacy_flat(builder: RunBuilder, ref: SourceRef, data: Mapping[str, Any]) -> None:
    """Pre-V0.4A flat node telemetry. execution_id stays NULL, forever."""
    node_id = _text(data.get("node")) or "N00"
    node_type = node_id.split("_", 1)[-1] if "_" in node_id else node_id
    usage = data.get("usage") if isinstance(data.get("usage"), Mapping) else {}
    builder.run.setdefault("_flat_started", [])
    if data.get("started_at"):
        builder.run["_flat_started"].append(str(data["started_at"]))
    builder.run.setdefault("_flat_ended", [])
    if data.get("completed_at"):
        builder.run["_flat_ended"].append(str(data["completed_at"]))
    builder.run.update({
        "job_class": "LEGACY",
        "goal": builder.run.get("goal") or _text(data.get("task_short")),
        "workflow_or_job_id": builder.run.get("workflow_or_job_id") or _text(data.get("pipeline")),
        "evidence_grain": LEGACY,
        "run_authority": "LEGACY_FLAT_NODE_TELEMETRY",
        # V1 set the run status from whichever flat file happened to be read
        # last. There is no explicit run-level status in this evidence, so V2
        # reports UNKNOWN and keeps the per-node outcomes instead.
        "final_status": UNKNOWN,
        "final_status_basis": "NO_EXPLICIT_RUN_STATUS",
    })
    if builder.run.get("source_id") is None:
        builder.run["source_id"] = ref.source_id
    builder.link_source("run", builder.run_id, ref, "LEGACY_FLAT_NODE_TELEMETRY")
    builder.node(node_id=node_id, node_type=node_type, outcome=data.get("outcome"),
                 started_at=data.get("started_at"), ended_at=data.get("completed_at"),
                 evidence_grain=LEGACY, ref=ref)
    row = builder.add_legacy_execution({
        "node_id": node_id, "node_type": node_type,
        "harness": _text(data.get("agent")), "model": _text(data.get("model")),
        "effort": _text(data.get("effort")),
        "started_at": _text(data.get("started_at")), "ended_at": _text(data.get("completed_at")),
    }, ref=ref, pointer="")
    builder.observe_usage(row, {
        "provider_session_id": data.get("session_id") or data.get("thread_id"),
        "telemetry_status": data.get("telemetry_status"),
    }, usage=usage or {}, source_label="NODE_TELEMETRY_WALL_TIME", ref=ref)
    builder.set_outcome(row, data.get("outcome"), "NODE_TELEMETRY_OUTCOME")


# ---------------------------------------------------------------------------
# Ledger (V0.4B) — lifecycle authority, read through the existing reader
# ---------------------------------------------------------------------------

_LEDGER_MODULE: Any = None
_LEDGER_IMPORT_ERROR: str | None = None


def ledger_module() -> Any:
    """Import ``execution_ledger`` from the AAW root. Never reimplement its parser."""
    global _LEDGER_MODULE, _LEDGER_IMPORT_ERROR
    if _LEDGER_MODULE is not None or _LEDGER_IMPORT_ERROR is not None:
        return _LEDGER_MODULE
    try:
        if str(AAW_ROOT) not in sys.path:
            sys.path.insert(0, str(AAW_ROOT))
        import execution_ledger  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001 - analytics must degrade, not crash
        _LEDGER_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
        return None
    _LEDGER_MODULE = execution_ledger
    return _LEDGER_MODULE


_LIFECYCLE_CONFLICT_KINDS = {
    "CONTRADICTORY_CLOSE", "CLOSED_WITHOUT_STARTED_NOT_RECONCILED",
    "STARTED_WITHOUT_INTENT", "CLOSED_WITHOUT_INTENT",
    "AT_MOST_ONCE_DISPATCH_VIOLATION", "DATA_INTEGRITY_ERROR",
}


def ingest_ledger(builder: RunBuilder, ref: SourceRef) -> None:
    """Derive lifecycle rows from observed events only.

    STARTED is never inferred from INTENT. CLOSED is never inferred from
    workflow status. A missing CLOSED is unresolved lifecycle, not a FAIL.
    The ledger is never repaired here.
    """
    module = ledger_module()
    if module is None:
        builder.diag("LEDGER_READER_UNAVAILABLE",
                     f"execution_ledger not importable: {_LEDGER_IMPORT_ERROR}", ref.path)
        return
    builder.run["ledger_present"] = 1
    builder.run["ledger_schema_version"] = builder.run.get("ledger_schema_version") \
        or module.SCHEMA_VERSION
    try:
        read = module._scan(ref.path)
        report = module.validate_events(read, builder.run_id)
    except Exception as exc:  # noqa: BLE001
        builder.diag("LEDGER_READ_ERROR", f"{type(exc).__name__}: {exc}", ref.path)
        return

    for row in read.diagnostics:
        builder.diag(str(row.get("kind")), _text(row.get("detail")) or "", ref.path)
    conflicting: dict[str, list[str]] = {}
    for error in report.get("errors") or []:
        kind = str(error.get("kind"))
        builder.diag(kind, _text(error.get("detail")) or "", ref.path)
        if kind in _LIFECYCLE_CONFLICT_KINDS:
            detail = _text(error.get("detail")) or kind
            for execution_id in re.findall(r"EXE_[0-9a-f]{32}", detail):
                conflicting.setdefault(execution_id, []).append(kind)

    lifecycle: dict[str, dict[str, Any]] = {}
    for event in read.events:
        event_type = _text(event.get("event_type"))
        payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
        if event_type == module.COMMIT_RECORDED:
            _record_commit(builder, payload, ref, observed_in="LEDGER")
            continue
        if event_type == module.HUMAN_DECISION_RECORDED:
            _record_human_decision(builder, payload, ref, authority="LEDGER_OBSERVATION")
            continue
        execution_id = _text(event.get("execution_id"))
        if not execution_id:
            continue
        entry = lifecycle.setdefault(execution_id, {
            "execution_id": execution_id, "run_id": builder.run_id,
            "lifecycle_state": UNKNOWN, "basis": "LEDGER",
            "intent_sequence": None, "started_sequence": None, "closed_sequence": None,
            "start_evidence": None, "process_id": None,
            "process_creation_time": None, "process_creation_time_source": None,
            "observed_start_time": None, "observed_close_time": None,
            "close_reason": None, "effect_certainty": None, "exit_code": None,
            "observation_source": None, "ledger_outcome": None,
            "timed_out": None, "cancelled": None, "interrupted": None,
            "indexed_execution": 0, "conflict_detail": None, "source_id": ref.source_id,
        })
        sequence = _as_int(event.get("sequence"))
        if event_type == module.EXECUTION_INTENT:
            entry["intent_sequence"] = sequence
        elif event_type == module.EXECUTION_STARTED:
            entry["started_sequence"] = sequence
            entry["start_evidence"] = _text(payload.get("start_evidence"))
            entry["process_id"] = _as_int(payload.get("process_id"))
            entry["process_creation_time"] = _text(payload.get("process_creation_time"))
            entry["process_creation_time_source"] = _text(payload.get("process_creation_time_source"))
            entry["observed_start_time"] = _text(payload.get("observed_start_time"))
        elif event_type == module.EXECUTION_CLOSED:
            entry["closed_sequence"] = sequence
            entry["observed_close_time"] = _text(payload.get("observed_close_time"))
            entry["close_reason"] = _text(payload.get("close_reason"))
            entry["effect_certainty"] = _text(payload.get("effect_certainty"))
            entry["exit_code"] = _as_int(payload.get("exit_code"))
            entry["observation_source"] = _text(payload.get("observation_source"))
            entry["ledger_outcome"] = _norm_outcome(payload.get("outcome"))
            entry["timed_out"] = _as_bool_int(payload.get("timed_out"))
            entry["cancelled"] = _as_bool_int(payload.get("cancelled"))
            entry["interrupted"] = _as_bool_int(payload.get("interrupted"))

    for execution_id, entry in lifecycle.items():
        if execution_id in conflicting:
            entry["lifecycle_state"] = "LIFECYCLE_CONFLICT"
            entry["conflict_detail"] = ",".join(sorted(set(conflicting[execution_id])))
        elif entry["closed_sequence"] is not None:
            entry["lifecycle_state"] = "CLOSED"
        elif entry["started_sequence"] is not None:
            entry["lifecycle_state"] = "STARTED_OPEN"
        elif entry["intent_sequence"] is not None:
            entry["lifecycle_state"] = "INTENT_ONLY"
        else:
            entry["lifecycle_state"] = UNKNOWN
        builder.lifecycle[execution_id] = entry
        builder.link_source("execution_lifecycle", execution_id, ref, "LEDGER")


# ---------------------------------------------------------------------------
# Preprocess
# ---------------------------------------------------------------------------

_PREPROCESS_DECISION = {
    "COMPLETED_ADVISORY": "INVOKED",
    "SKIPPED_NOT_USEFUL": "SKIPPED",
    "SKIPPED_POLICY_OFF": "SKIPPED",
    "SKIPPED_LOCAL_UNAVAILABLE": "SKIPPED",
    "TIMEOUT": "FAILED",
    "ERROR": "FAILED",
    "BLOCKED": "FAILED",
}


def ingest_preprocess(builder: RunBuilder, data: Mapping[str, Any], ref: SourceRef,
                      known_node_ids: set[str]) -> None:
    """Preprocess decision. A SKIPPED decision is not an LLM invocation.

    The downstream link is only asserted from an explicit
    ``downstream_execution_id`` or an explicit downstream node id that the run
    actually declares. Never from a node name plus a nearby timestamp.
    """
    run_id = _text(data.get("run_id"))
    if not run_id:
        raise ValueError("preprocess artifact without run_id")
    telemetry = data.get("telemetry") if isinstance(data.get("telemetry"), Mapping) else {}
    downstream_execution_id = _text(data.get("downstream_execution_id"))
    downstream_node_id = _text(telemetry.get("downstream_node_id") or data.get("node_id"))
    if downstream_execution_id and builder.execution_by_id(downstream_execution_id) is not None:
        confidence = "EXPLICIT_EXECUTION_ID"
    elif downstream_execution_id:
        confidence = "EXPLICIT_EXECUTION_ID"
        builder.diag("PREPROCESS_DOWNSTREAM_EXECUTION_NOT_INDEXED",
                     f"downstream_execution_id {downstream_execution_id} has no indexed execution",
                     ref.path)
    elif downstream_node_id and downstream_node_id in known_node_ids:
        confidence = "EXPLICIT_NODE_ID"
    else:
        confidence = "UNLINKED"
    status = _text(data.get("status"))
    execution_id = _text(data.get("execution_id"))
    decision = _PREPROCESS_DECISION.get(str(status).upper(), UNKNOWN) if status else UNKNOWN
    if decision == "SKIPPED" and execution_id:
        builder.diag("DATA_INTEGRITY_ERROR",
                     f"skipped preprocess {data.get('preprocess_id')} carries an execution_id",
                     ref.path)
    preprocess_id = _text(data.get("preprocess_id"))
    row = {
        "preprocess_id": preprocess_id, "row_locator": ref.locator(""),
        "execution_id": execution_id, "run_id": builder.run_id,
        "downstream_node_id": downstream_node_id,
        "downstream_execution_id": downstream_execution_id,
        "downstream_link_confidence": confidence,
        "preprocess_type": _text(data.get("preprocess_type")),
        "profile": _text(data.get("profile")), "provider": _text(data.get("provider")),
        "model": _text(data.get("model")), "status": status,
        "decision_status": decision, "reason": _text(data.get("reason")),
        "authority": _text(data.get("authority")),
        "input_chars": _as_int(telemetry.get("input_chars")),
        "output_chars": _as_int(telemetry.get("output_chars")),
        "input_tokens": _as_int(telemetry.get("input_tokens")),
        "output_tokens": _as_int(telemetry.get("output_tokens")),
        "wall_time_s": _as_float(telemetry.get("wall_time_s")),
        "downstream_input_before_estimate": _as_int(telemetry.get("downstream_input_before_estimate")),
        "downstream_input_after_estimate": _as_int(telemetry.get("downstream_input_after_estimate")),
        "created_at": _text(data.get("created_at")), "created_day": _day(data.get("created_at")),
        "output_artifact_hash": _text(data.get("output_artifact_hash")),
        "consumed": _as_bool_int(data.get("consumed")),
        "consumed_basis": "EXPLICIT" if data.get("consumed") is not None else "NOT_RECORDED",
        "source_artifact_count": _as_int(telemetry.get("source_artifact_count")),
        "evidence_grain": MODERN if preprocess_id else LEGACY,
        "fixture_class": UNKNOWN,
        "source_id": ref.source_id,
    }
    builder.preprocess.append(row)
    builder.link_source("preprocess", preprocess_id or row["row_locator"], ref, "PREPROCESS_ARTIFACT")


# ---------------------------------------------------------------------------
# Finalisation
# ---------------------------------------------------------------------------

def _resolve_llm_invocation(row: Mapping[str, Any]) -> tuple[int | None, str]:
    kind = row.get("invocation_kind")
    if kind:
        return (1 if str(kind).upper() in _LLM_KINDS else 0), "INVOCATION_KIND"
    node_type = str(row.get("node_type") or "").upper()
    if node_type in ("MACHINE_GATE", "HUMAN_GATE"):
        return 0, "NODE_TYPE"
    if row.get("model"):
        return 1, "MODEL_PRESENCE"
    return None, UNKNOWN


def finalize(builder: RunBuilder) -> None:
    """Post-passes that need the whole run: fixtures, day basis, lifecycle fill."""
    run = builder.run
    if run.get("_flat_started"):
        run["started_at"] = min(run["_flat_started"])
    if run.get("_flat_ended"):
        run["ended_at"] = max(run["_flat_ended"])
    run["started_day"] = _day(run.get("started_at")) or _day(run.get("ended_at"))

    fixture_class, fixture_evidence = classify_fixture(
        declared_job_id=builder._declared_job_id or run.get("workflow_or_job_id"),
        repository=run.get("repository"), worktree=run.get("worktree"))
    run["fixture_class"] = fixture_class
    run["fixture_evidence"] = fixture_evidence
    if run["evidence_grain"] == UNKNOWN and builder.executions:
        run["evidence_grain"] = MODERN if any(
            row.get("evidence_grain") == MODERN for row in builder.executions.values()) else LEGACY
    run["evidence_class"] = evidence_class(run["evidence_grain"], fixture_class)

    for row in builder.executions.values():
        declared = row.pop("_declared_fixture_class", None)
        if declared:
            row["fixture_class"], row["fixture_evidence"] = declared, "DESCRIPTOR_FIXTURE_CLASS"
        else:
            row["fixture_class"], row["fixture_evidence"] = fixture_class, fixture_evidence
        if row.get("node_type") is None and row.get("invocation_kind"):
            row["node_type"] = row["invocation_kind"]
        row["llm_invocation"], row["llm_invocation_basis"] = _resolve_llm_invocation(row)

        for value, basis in (
            (row.get("started_at"), "TELEMETRY_STARTED_AT"),
            (row.get("created_at"), "DESCRIPTOR_CREATED_AT"),
            (row.get("ended_at"), "TELEMETRY_ENDED_AT"),
        ):
            day = _day(value)
            if day:
                row["started_day"], row["started_day_basis"] = day, basis
                break
        else:
            execution_id = row.get("execution_id")
            entry = builder.lifecycle.get(execution_id) if execution_id else None
            day = _day(entry.get("observed_start_time")) if entry else None
            if day:
                row["started_day"], row["started_day_basis"] = day, "LEDGER_OBSERVED_START"
            else:
                for value, basis in ((run.get("started_at"), "RUN_STARTED_AT"),
                                     (run.get("ended_at"), "RUN_ENDED_AT")):
                    day = _day(value)
                    if day:
                        row["started_day"], row["started_day_basis"] = day, basis
                        break

        if row.get("wall_time_s") is None and row.get("execution_id"):
            entry = builder.lifecycle.get(row["execution_id"])
            if entry:
                elapsed = _elapsed_s(entry.get("observed_start_time"), entry.get("observed_close_time"))
                if elapsed is not None:
                    row["wall_time_s"] = elapsed
                    row["wall_time_basis"] = "LIFECYCLE_OBSERVED_TIMES"

    # Every modern execution gets exactly one lifecycle row. Where the run has
    # no ledger, the honest state is UNKNOWN, not "closed" and not "failed".
    for execution_id, row in builder.by_execution_id.items():
        if execution_id in builder.lifecycle:
            builder.lifecycle[execution_id]["indexed_execution"] = 1
            continue
        builder.lifecycle[execution_id] = {
            "execution_id": execution_id, "run_id": builder.run_id,
            "lifecycle_state": UNKNOWN, "basis": "NO_LEDGER_EVIDENCE",
            "intent_sequence": None, "started_sequence": None, "closed_sequence": None,
            "start_evidence": None, "process_id": None,
            "process_creation_time": None, "process_creation_time_source": None,
            "observed_start_time": None, "observed_close_time": None,
            "close_reason": None, "effect_certainty": None, "exit_code": None,
            "observation_source": None, "ledger_outcome": None,
            "timed_out": None, "cancelled": None, "interrupted": None,
            "indexed_execution": 1, "conflict_detail": None,
            "source_id": row.get("source_id"),
        }
    for execution_id, entry in builder.lifecycle.items():
        if execution_id not in builder.by_execution_id and entry["basis"] == "LEDGER":
            builder.diag("LIFECYCLE_WITHOUT_INDEXED_EXECUTION",
                         f"ledger observes {execution_id} but no execution row is indexed")

    for row in builder.preprocess:
        row["fixture_class"] = fixture_class

    for row in builder.nodes.values():
        if row.get("workflow_or_job_id") is None:
            row["workflow_or_job_id"] = run.get("workflow_or_job_id")

    for key, decision in builder.human_decisions.items():
        decision["candidate_resolved"] = int(bool(
            decision.get("candidate_id") and decision["candidate_id"] in builder.candidates))
        if decision.get("candidate_id") and not decision["candidate_resolved"]:
            builder.diag("HUMAN_DECISION_CANDIDATE_UNRESOLVED",
                         f"{key} references candidate {decision['candidate_id']} "
                         "which is not indexed in this run")

    run.pop("_flat_started", None)
    run.pop("_flat_ended", None)


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------

_RUN_COLUMNS = ("run_id", "unit_key", "source_id", "job_class", "workflow_or_job_id", "goal",
                "repository", "worktree", "started_at", "ended_at", "started_day",
                "final_status", "final_status_basis", "human_gate_reached", "human_verdict",
                "evidence_grain", "evidence_class", "fixture_class", "fixture_evidence",
                "identity_contract", "ledger_present", "ledger_schema_version", "run_authority")
_NODE_COLUMNS = ("run_id", "node_id", "subtask_key", "node_type", "subtask_id", "subtask_index",
                 "outcome", "repair_cycle", "wall_time_s", "started_at", "ended_at",
                 "review_independence", "workflow_or_job_id", "evidence_grain", "source_id")
_EXECUTION_COLUMNS = ("execution_id", "row_locator", "run_id", "node_id", "node_type",
                      "subtask_id", "subtask_index", "invocation_kind", "llm_invocation",
                      "llm_invocation_basis", "provider", "harness", "model", "effort", "profile",
                      "provider_session_id", "retry_of_execution_id", "repair_cycle",
                      "originating_review_execution_id", "repair_execution_id",
                      "original_review_execution_id", "created_at", "started_at", "ended_at",
                      "started_day", "started_day_basis", "outcome", "outcome_source",
                      "wall_time_s", "wall_time_basis", "input_tokens", "cached_input_tokens",
                      "output_tokens", "reasoning_tokens", "usage_observed", "telemetry_status",
                      "execution_mode", "input_contract_hash", "selection_reason",
                      "policy_version", "descriptor_status", "identity_authority",
                      "identity_contract", "evidence_grain", "fixture_class", "fixture_evidence",
                      "conflict", "source_id")
_LIFECYCLE_COLUMNS = ("execution_id", "run_id", "lifecycle_state", "basis", "intent_sequence",
                      "started_sequence", "closed_sequence", "start_evidence", "process_id",
                      "process_creation_time", "process_creation_time_source",
                      "observed_start_time", "observed_close_time", "close_reason",
                      "effect_certainty", "exit_code", "observation_source", "ledger_outcome",
                      "timed_out", "cancelled", "interrupted", "indexed_execution",
                      "conflict_detail", "source_id")
_VALIDATION_COLUMNS = ("run_id", "machine_gate_result", "reviewer_result", "repair_required",
                       "repair_cycles", "delta_review_result", "evidence_grain", "basis", "source_id")
_FINDING_COLUMNS = ("review_execution_id", "finding_id", "run_id", "severity", "file", "location",
                    "description", "required_fix", "commit_hash", "source_id")
_PRODUCER_COLUMNS = ("review_execution_id", "produced_execution_id", "run_id", "source_id")
_FINDING_LINK_COLUMNS = ("review_execution_id", "finding_id", "linked_execution_id", "link_role",
                         "disposition", "run_id", "source_id")
_PREPROCESS_COLUMNS = ("preprocess_id", "row_locator", "execution_id", "run_id",
                       "downstream_node_id", "downstream_execution_id",
                       "downstream_link_confidence", "preprocess_type", "profile", "provider",
                       "model", "status", "decision_status", "reason", "authority",
                       "input_chars", "output_chars", "input_tokens", "output_tokens",
                       "wall_time_s", "downstream_input_before_estimate",
                       "downstream_input_after_estimate", "created_at", "created_day",
                       "output_artifact_hash", "consumed", "consumed_basis",
                       "source_artifact_count", "evidence_grain", "fixture_class", "source_id")
_COMMIT_COLUMNS = ("repository", "commit_hash", "run_id", "subtask_id", "role", "expected_parent",
                   "git_evidence_hash", "observed_in_state", "observed_in_ledger", "conflict",
                   "source_id")
_COMMIT_PRODUCER_COLUMNS = ("repository", "commit_hash", "execution_id", "run_id")
_CANDIDATE_COLUMNS = ("candidate_id", "run_id", "repository", "worktree", "candidate_head",
                      "artifact_manifest", "content_identity_hash", "created_at",
                      "source_authority", "conflict", "source_id")
_CANDIDATE_LINK_COLUMNS = ("candidate_id", "execution_id", "link_role", "run_id")
_HUMAN_DECISION_COLUMNS = ("human_decision_id", "candidate_id", "run_id", "verdict",
                           "quality_assessment", "reason", "recorded_at", "artifact_path",
                           "artifact_hash", "source_authority", "candidate_resolved",
                           "conflict", "source_id")


def _insert(conn: sqlite3.Connection, table: str, columns: Sequence[str],
            row: Mapping[str, Any], *, replace: bool = False) -> None:
    verb = "INSERT OR REPLACE INTO" if replace else "INSERT INTO"
    placeholders = ", ".join(f":{column}" for column in columns)
    conn.execute(f"{verb} {table}({', '.join(columns)}) VALUES ({placeholders})",
                 {column: row.get(column) for column in columns})


def _diag(conn: sqlite3.Connection, row: Mapping[str, Any], ingested_at: str) -> None:
    conn.execute(
        "INSERT INTO ingest_diagnostic(unit_key, run_id, path, kind, detail, ingested_at) "
        "VALUES (?,?,?,?,?,?)",
        (row.get("unit_key"), row.get("run_id"), row.get("path"), row["kind"],
         row.get("detail"), ingested_at))


def _conflict(conn: sqlite3.Connection, row: Mapping[str, Any], ingested_at: str) -> None:
    conn.execute(
        "INSERT INTO entity_conflict(unit_key, run_id, entity, entity_key, field, value_a, "
        "source_a, value_b, source_b, detail, ingested_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (row.get("unit_key"), row.get("run_id"), row["entity"], row["entity_key"], row["field"],
         row.get("value_a"), row.get("source_a"), row.get("value_b"), row.get("source_b"),
         row.get("detail"), ingested_at))


def _claim_identity(conn: sqlite3.Connection, builder: RunBuilder, counters: dict[str, int],
                    ingested_at: str, *, table: str, entity: str,
                    keys: Mapping[str, Any], row: Mapping[str, Any]) -> bool:
    """Guard an identity-bearing row against a cross-run identity collision.

    Rows a unit owns were already deleted before re-ingestion, so anything found
    here belongs to a DIFFERENT run claiming the same identity. That is a data
    integrity fault, never an update: the original is preserved and the incoming
    row is refused.
    """
    where = " AND ".join(f"{column}=?" for column in keys)
    existing = conn.execute(f"SELECT run_id FROM {table} WHERE {where}",
                            tuple(keys.values())).fetchone()
    if existing is None or _text(existing["run_id"]) == _text(row.get("run_id")):
        return True
    entity_key = "|".join(str(value) for value in keys.values())
    counters["integrity_errors"] += 1
    _diag(conn, {"unit_key": builder.unit_key, "run_id": builder.run_id, "path": None,
                 "kind": "DATA_INTEGRITY_ERROR",
                 "detail": f"{entity} identity {entity_key} is already claimed by run "
                           f"{existing['run_id']}; incoming row refused"}, ingested_at)
    _conflict(conn, {"unit_key": builder.unit_key, "run_id": builder.run_id, "entity": entity,
                     "entity_key": entity_key, "field": "run_id",
                     "value_a": _text(existing["run_id"]), "source_a": "PRESERVED_ROW",
                     "value_b": _text(row.get("run_id")), "source_b": "REFUSED_ROW",
                     "detail": "one identity claimed by two runs; original preserved"},
              ingested_at)
    return False


def write_builder(conn: sqlite3.Connection, builder: RunBuilder, ingested_at: str) -> dict[str, int]:
    """Persist one run's rows. Cross-run identity conflicts quarantine the new row."""
    counters = {"executions": 0, "quarantined": 0, "integrity_errors": 0}
    run = dict(builder.run)
    run["ingested_at"] = ingested_at
    _insert(conn, "run", _RUN_COLUMNS + ("ingested_at",), run, replace=True)

    for row in builder.nodes.values():
        _insert(conn, "logical_node", _NODE_COLUMNS, row, replace=True)

    for row in builder.executions.values():
        execution_id = row.get("execution_id")
        if execution_id:
            existing = conn.execute(
                "SELECT run_id, node_id, subtask_id, invocation_kind, model, effort, "
                "identity_authority FROM execution WHERE execution_id=?", (execution_id,)).fetchone()
            if existing is not None:
                mismatched = [field for field in ("run_id", "node_id", "subtask_id",
                                                  "invocation_kind", "model", "effort")
                              if _text(existing[field]) != _text(row.get(field))]
                detail = (f"DATA_INTEGRITY_ERROR duplicate modern execution_id {execution_id}"
                          + (f"; incompatible fields: {','.join(mismatched)}" if mismatched else
                             "; identical content"))
                _diag(conn, {"unit_key": builder.unit_key, "run_id": builder.run_id,
                             "path": None, "kind": "DATA_INTEGRITY_ERROR", "detail": detail},
                      ingested_at)
                if mismatched:
                    _conflict(conn, {
                        "unit_key": builder.unit_key, "run_id": builder.run_id,
                        "entity": "execution", "entity_key": execution_id,
                        "field": ",".join(mismatched),
                        "value_a": _text(existing["run_id"]), "source_a": "PRESERVED_ROW",
                        "value_b": _text(row.get("run_id")), "source_b": "QUARANTINED_ROW",
                        "detail": "second execution row for one execution identity; "
                                  "original preserved, new row quarantined",
                    }, ingested_at)
                    conn.execute("UPDATE execution SET conflict=1 WHERE execution_id=?",
                                 (execution_id,))
                counters["quarantined"] += 1
                counters["integrity_errors"] += 1
                continue
        try:
            _insert(conn, "execution", _EXECUTION_COLUMNS, row)
        except sqlite3.IntegrityError as exc:
            counters["integrity_errors"] += 1
            _diag(conn, {"unit_key": builder.unit_key, "run_id": builder.run_id, "path": None,
                         "kind": "DATA_INTEGRITY_ERROR",
                         "detail": f"execution row rejected: {exc}"}, ingested_at)
            continue
        counters["executions"] += 1

    for row in builder.lifecycle.values():
        _insert(conn, "execution_lifecycle", _LIFECYCLE_COLUMNS, row, replace=True)
    for row in builder.validations:
        _insert(conn, "validation", _VALIDATION_COLUMNS, row)
    for row in builder.findings.values():
        # A finding key belongs to exactly one owning review execution. If the
        # same key arrives from another run, the identity itself is broken:
        # preserve the original and record it instead of overwriting.
        if not _claim_identity(conn, builder, counters, ingested_at,
                               table="review_finding", entity="review_finding",
                               keys={"review_execution_id": row["review_execution_id"],
                                     "finding_id": row["finding_id"]}, row=row):
            continue
        _insert(conn, "review_finding", _FINDING_COLUMNS, row, replace=True)
    for row in builder.producer_links.values():
        _insert(conn, "review_producer_link", _PRODUCER_COLUMNS, row, replace=True)
    for row in builder.finding_links.values():
        _insert(conn, "finding_link", _FINDING_LINK_COLUMNS, row, replace=True)
    for row in builder.preprocess:
        try:
            _insert(conn, "preprocess", _PREPROCESS_COLUMNS, row)
        except sqlite3.IntegrityError as exc:
            counters["integrity_errors"] += 1
            _diag(conn, {"unit_key": builder.unit_key, "run_id": builder.run_id, "path": None,
                         "kind": "DATA_INTEGRITY_ERROR",
                         "detail": f"duplicate preprocess identity rejected: {exc}"}, ingested_at)
    for key, row in builder.commits.items():
        existing = conn.execute(
            "SELECT run_id, subtask_id, role, expected_parent FROM commit_record "
            "WHERE repository=? AND commit_hash=?", key).fetchone()
        if existing is None:
            _insert(conn, "commit_record", _COMMIT_COLUMNS, row)
            continue
        mismatched = [field for field in ("subtask_id", "role", "expected_parent")
                      if _text(existing[field]) != _text(row.get(field))]
        if mismatched:
            counters["integrity_errors"] += 1
            conn.execute("UPDATE commit_record SET conflict=1 WHERE repository=? AND commit_hash=?",
                         key)
            _conflict(conn, {
                "unit_key": builder.unit_key, "run_id": builder.run_id,
                "entity": "commit_record", "entity_key": f"{key[0]}@{key[1]}",
                "field": ",".join(mismatched), "value_a": _text(existing["run_id"]),
                "source_a": "PRESERVED_ROW", "value_b": _text(row.get("run_id")),
                "source_b": "INCOMING_ROW",
                "detail": "one commit identity observed with incompatible attribution",
            }, ingested_at)
        else:
            conn.execute(
                "UPDATE commit_record SET observed_in_state = MAX(observed_in_state, ?), "
                "observed_in_ledger = MAX(observed_in_ledger, ?) WHERE repository=? AND commit_hash=?",
                (row["observed_in_state"], row["observed_in_ledger"], key[0], key[1]))
    for row in builder.commit_producers.values():
        _insert(conn, "commit_producer_link", _COMMIT_PRODUCER_COLUMNS, row, replace=True)
    for row in builder.candidates.values():
        if not _claim_identity(conn, builder, counters, ingested_at, table="candidate",
                               entity="candidate",
                               keys={"candidate_id": row["candidate_id"]}, row=row):
            continue
        _insert(conn, "candidate", _CANDIDATE_COLUMNS, row, replace=True)
    for row in builder.candidate_links.values():
        _insert(conn, "candidate_execution_link", _CANDIDATE_LINK_COLUMNS, row, replace=True)
    for row in builder.human_decisions.values():
        if not _claim_identity(conn, builder, counters, ingested_at, table="human_decision",
                               entity="human_decision",
                               keys={"human_decision_id": row["human_decision_id"]}, row=row):
            continue
        _insert(conn, "human_decision", _HUMAN_DECISION_COLUMNS, row, replace=True)
    for row in builder.entity_sources.values():
        _insert(conn, "entity_source",
                ("entity", "entity_key", "source_id", "role", "run_id"), row, replace=True)
    for row in builder.conflicts:
        _conflict(conn, row, ingested_at)
    for row in builder.diagnostics:
        _diag(conn, row, ingested_at)
        if row["kind"] == "DATA_INTEGRITY_ERROR":
            counters["integrity_errors"] += 1
    return counters


# ---------------------------------------------------------------------------
# Ingestion units
# ---------------------------------------------------------------------------

FLAT_UNIT = "__FLAT__"

_LAYOUT_ORDER = {
    "EXECUTION_DESCRIPTOR": 0,
    "CUSTOM_JOB_STATE": 1, "WORKFLOW_STATE": 1,
    "CANDIDATE": 2, "HUMAN_DECISION": 3,
    "LEDGER": 4, "PREPROCESS": 5,
}

_SKIP_DETAIL = {
    "FROZEN_INPUT": "frozen contract input snapshot; not execution evidence",
    "DIAGNOSTIC": "non-indexed diagnostic evidence",
    "UNKNOWN": "not run evidence",
    "LEDGER": "ledger sidecar",
}


def _plan_units(stats_root: Path) -> dict[str, list[Path]]:
    units: dict[str, list[Path]] = {}
    for path in sorted(p for p in stats_root.rglob("*") if p.is_file()):
        parts = path.relative_to(stats_root).parts
        key = FLAT_UNIT if len(parts) == 1 else parts[0]
        units.setdefault(key, []).append(path)
    return units


def _unit_signature(files: Sequence[tuple[str, int, str]]) -> str:
    return json.dumps(sorted(files), separators=(",", ":"))


def _delete_unit(conn: sqlite3.Connection, unit_key: str) -> None:
    run_ids = [row[0] for row in conn.execute("SELECT run_id FROM run WHERE unit_key=?", (unit_key,))]
    row = conn.execute("SELECT run_ids FROM ingest_unit WHERE unit_key=?", (unit_key,)).fetchone()
    if row:
        try:
            run_ids = sorted(set(run_ids) | set(json.loads(row[0])))
        except (TypeError, ValueError):
            pass
    for run_id in run_ids:
        for table in _RUN_OWNED_TABLES:
            conn.execute(f"DELETE FROM {table} WHERE run_id=?", (run_id,))
        conn.execute("DELETE FROM run WHERE run_id=?", (run_id,))
    conn.execute("DELETE FROM ingest_diagnostic WHERE unit_key=?", (unit_key,))
    conn.execute("DELETE FROM entity_conflict WHERE unit_key=?", (unit_key,))
    conn.execute("DELETE FROM entity_source WHERE source_id IN "
                 "(SELECT id FROM source_file WHERE unit_key=?)", (unit_key,))
    conn.execute("DELETE FROM source_file WHERE unit_key=?", (unit_key,))
    conn.execute("DELETE FROM ingest_unit WHERE unit_key=?", (unit_key,))


def _register_source(conn: sqlite3.Connection, unit_key: str, path: Path, stats_root: Path,
                     ref: SourceRef, status: str, detail: str | None, ingested_at: str) -> int:
    stat = path.stat()
    cursor = conn.execute(
        """INSERT INTO source_file(unit_key, path, rel_path, sha256, bytes, mtime, schema_version,
                                   layout, source_type, status, detail, ingested_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(path) DO UPDATE SET
             unit_key=excluded.unit_key, sha256=excluded.sha256, bytes=excluded.bytes,
             mtime=excluded.mtime, schema_version=excluded.schema_version,
             layout=excluded.layout, source_type=excluded.source_type,
             status=excluded.status, detail=excluded.detail, ingested_at=excluded.ingested_at
           RETURNING id""",
        (unit_key, str(path), ref.rel_path, ref.sha256, stat.st_size, stat.st_mtime,
         ref.schema_version, ref.layout, ref.source_type, status, detail, ingested_at))
    return int(cursor.fetchone()[0])


def _known_node_ids(builder: RunBuilder) -> set[str]:
    ids = {row["node_id"] for row in builder.nodes.values()}
    ids |= {row.get("node_id") for row in builder.executions.values() if row.get("node_id")}
    ids |= {"PLAN", "IMPLEMENT", "SUBTASK", "REVIEW", "REPAIR", "DELTA_REVIEW"}
    return {str(value) for value in ids if value}


# ---------------------------------------------------------------------------
# Unit ingestion
# ---------------------------------------------------------------------------

def _status_for(layout: str, source_type: str, run_kind: str | None) -> tuple[str, str | None]:
    if layout in ("EXECUTION_DESCRIPTOR", "CUSTOM_JOB_STATE", "WORKFLOW_STATE", "CANDIDATE",
                  "HUMAN_DECISION", "LEDGER", "PREPROCESS", "LEGACY_FLAT"):
        return "INDEXED", None
    if layout in ("SINGLE_TASK_NODE", "CLASSIFIER"):
        if run_kind == "SINGLE_TASK":
            return "INDEXED", "single-task run authority"
        return "SUPERSEDED", f"{run_kind} state file is authority"
    if layout in ("WORKFLOW_DETAIL", "CUSTOM_JOB_DETAIL", "WORKFLOW_NODE_TELEMETRY"):
        return "SUPERSEDED", "authority is the run state file"
    if layout == "FROZEN_JOB":
        return "SKIPPED", "frozen job contract input; not execution evidence"
    if layout == "LOCAL_LLM_SMOKE":
        return "SKIPPED", "local runtime health diagnostic; not run evidence"
    if layout == "SKIPPED":
        return "SKIPPED", _SKIP_DETAIL.get(source_type, "not run evidence")
    return "SKIPPED", "unrecognised layout"


def ingest_unit(conn: sqlite3.Connection, unit_key: str, paths: Sequence[Path],
                stats_root: Path, ingested_at: str) -> tuple[list[str], dict[str, int]]:
    counters = {"indexed": 0, "superseded": 0, "skipped": 0, "errors": 0,
                "executions": 0, "quarantined": 0, "integrity_errors": 0}
    run_dir = stats_root if unit_key == FLAT_UNIT else stats_root / unit_key
    run_kind = None if unit_key == FLAT_UNIT else _run_dir_kind(run_dir)

    parsed: list[tuple[int, SourceRef, Any]] = []
    for path in paths:
        layout, source_type = classify(path, stats_root)
        ref = SourceRef(path, str(path.relative_to(stats_root)), _sha256(path),
                        layout, source_type, None)
        status, detail = _status_for(layout, source_type, run_kind)
        payload: Any = None
        if status == "INDEXED" and layout != "LEDGER":
            try:
                payload = _load_json(path)
                if isinstance(payload, Mapping):
                    ref.schema_version = _text(payload.get("schema_version"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                counters["errors"] += 1
                ref.source_id = _register_source(conn, unit_key, path, stats_root, ref,
                                                 "PARSE_ERROR", f"{type(exc).__name__}: {exc}",
                                                 ingested_at)
                _diag(conn, {"unit_key": unit_key, "run_id": None, "path": str(path),
                             "kind": "PARSE_ERROR", "detail": f"{type(exc).__name__}: {exc}"},
                      ingested_at)
                continue
        elif layout == "UNKNOWN":
            try:
                _load_json(path)
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                counters["errors"] += 1
                ref.source_id = _register_source(conn, unit_key, path, stats_root, ref,
                                                 "PARSE_ERROR", f"{type(exc).__name__}: {exc}",
                                                 ingested_at)
                _diag(conn, {"unit_key": unit_key, "run_id": None, "path": str(path),
                             "kind": "PARSE_ERROR", "detail": f"{type(exc).__name__}: {exc}"},
                      ingested_at)
                continue
        ref.source_id = _register_source(conn, unit_key, path, stats_root, ref, status,
                                         detail, ingested_at)
        if status == "INDEXED":
            counters["indexed"] += 1
            parsed.append((_LAYOUT_ORDER.get(layout, 9), ref, payload))
        elif status == "SUPERSEDED":
            counters["superseded"] += 1
        else:
            counters["skipped"] += 1
            if layout == "UNKNOWN":
                _diag(conn, {"unit_key": unit_key, "run_id": None, "path": str(path),
                             "kind": "UNKNOWN_LAYOUT", "detail": ref.rel_path}, ingested_at)

    if unit_key == FLAT_UNIT:
        builders: dict[str, RunBuilder] = {}
        for _order, ref, payload in parsed:
            if ref.layout != "LEGACY_FLAT" or not isinstance(payload, Mapping):
                continue
            run_id = _text(payload.get("run_id")) or Path(ref.rel_path).stem
            builder = builders.get(run_id)
            if builder is None:
                builder = RunBuilder(run_id, unit_key)
                builders[run_id] = builder
            try:
                ingest_legacy_flat(builder, ref, payload)
            except (ValueError, KeyError, TypeError) as exc:
                counters["errors"] += 1
                builder.diag("PARSE_ERROR", f"{type(exc).__name__}: {exc}", ref.path)
        run_ids = []
        for run_id, builder in sorted(builders.items()):
            finalize(builder)
            result = write_builder(conn, builder, ingested_at)
            for key, value in result.items():
                counters[key] = counters.get(key, 0) + value
            run_ids.append(run_id)
        return run_ids, counters

    if not parsed:
        return [], counters

    builder = RunBuilder(unit_key, unit_key)
    single_task_files: list[tuple[SourceRef, Mapping[str, Any]]] = []
    for _order, ref, payload in sorted(parsed, key=lambda item: (item[0], item[1].rel_path)):
        try:
            if ref.layout == "LEDGER":
                ingest_ledger(builder, ref)
            elif ref.layout == "EXECUTION_DESCRIPTOR":
                ingest_descriptor(builder, payload, ref)
            elif ref.layout == "CUSTOM_JOB_STATE":
                ingest_custom_job_state(builder, payload, ref)
            elif ref.layout == "WORKFLOW_STATE":
                ingest_workflow_state(builder, payload, ref)
            elif ref.layout == "CANDIDATE":
                _record_candidate(builder, payload, ref, authority="CANDIDATE_ARTIFACT")
            elif ref.layout == "HUMAN_DECISION":
                _record_human_decision(builder, payload, ref, authority="HUMAN_DECISION_ARTIFACT")
            elif ref.layout == "PREPROCESS":
                declared = _text(payload.get("run_id"))
                if declared and declared != builder.run_id:
                    builder.diag("PREPROCESS_RUN_ID_MISMATCH",
                                 f"artifact declares run_id {declared}", ref.path)
                ingest_preprocess(builder, payload, ref, _known_node_ids(builder))
            elif ref.layout in ("SINGLE_TASK_NODE", "CLASSIFIER"):
                if isinstance(payload, Mapping):
                    single_task_files.append((ref, payload))
        except (ValueError, KeyError, TypeError) as exc:
            counters["errors"] += 1
            builder.diag("PARSE_ERROR", f"{type(exc).__name__}: {exc}", ref.path)

    if single_task_files:
        try:
            ingest_single_task_nodes(builder, single_task_files)
        except (ValueError, KeyError, TypeError) as exc:
            counters["errors"] += 1
            builder.diag("PARSE_ERROR", f"{type(exc).__name__}: {exc}")

    if builder.run["run_authority"] == UNKNOWN:
        if builder.preprocess:
            builder.run["run_authority"] = "PREPROCESS_ARTIFACT_ONLY"
            builder.run["job_class"] = UNKNOWN
            builder.run["source_id"] = builder.preprocess[0]["source_id"]
            builder.run["started_at"] = min(
                (row["created_at"] for row in builder.preprocess if row.get("created_at")),
                default=None)
        else:
            return [], counters

    finalize(builder)
    result = write_builder(conn, builder, ingested_at)
    for key, value in result.items():
        counters[key] = counters.get(key, 0) + value
    return [builder.run_id], counters


# ---------------------------------------------------------------------------
# Index validation and publication
# ---------------------------------------------------------------------------

def validate_index(conn: sqlite3.Connection) -> dict[str, Any]:
    """Referential and identity checks run BEFORE a freshly built index is published."""
    errors: list[str] = []
    warnings: list[str] = []
    if _schema_version(conn) != SCHEMA_VERSION:
        errors.append(f"schema_version is not {SCHEMA_VERSION}")
    for row in conn.execute("PRAGMA foreign_key_check"):
        errors.append(f"foreign key violation in {row[0]}")
    orphan_executions = conn.execute(
        "SELECT COUNT(*) FROM execution e LEFT JOIN run r ON r.run_id=e.run_id "
        "WHERE r.run_id IS NULL").fetchone()[0]
    if orphan_executions:
        errors.append(f"{orphan_executions} execution rows reference an unindexed run")
    duplicate_ids = conn.execute(
        "SELECT COUNT(*) FROM (SELECT execution_id FROM execution WHERE execution_id IS NOT NULL "
        "GROUP BY execution_id HAVING COUNT(*) > 1)").fetchone()[0]
    if duplicate_ids:
        errors.append(f"{duplicate_ids} duplicated modern execution identities")
    bad_grain = conn.execute(
        f"SELECT COUNT(*) FROM execution WHERE evidence_grain='{MODERN}' AND execution_id IS NULL"
    ).fetchone()[0]
    if bad_grain:
        errors.append(f"{bad_grain} rows claim modern grain without an execution identity")
    synthesised = conn.execute(
        "SELECT COUNT(*) FROM execution WHERE execution_id IS NOT NULL "
        "AND execution_id NOT LIKE 'EXE_%'").fetchone()[0]
    if synthesised:
        errors.append(f"{synthesised} execution identities do not match the EXE_ contract")
    indexed_sources = conn.execute(
        "SELECT COUNT(*) FROM source_file WHERE status='INDEXED'").fetchone()[0]
    runs = conn.execute("SELECT COUNT(*) FROM run").fetchone()[0]
    if indexed_sources and not runs:
        errors.append("indexed sources produced no runs")
    orphan_lifecycle = conn.execute(
        "SELECT COUNT(*) FROM execution_lifecycle WHERE indexed_execution=0").fetchone()[0]
    if orphan_lifecycle:
        warnings.append(f"{orphan_lifecycle} lifecycle observations have no indexed execution")
    return {"valid": not errors, "errors": errors, "warnings": warnings,
            "runs": runs, "indexed_sources": indexed_sources}


def _build(db_path: Path, stats_root: Path, *, rebuild: bool) -> dict[str, Any]:
    conn = connect(db_path)
    try:
        if not _schema_ok(conn):
            conn.close()
            if db_path.exists():
                db_path.unlink()
            conn = connect(db_path)
            _init_schema(conn)
        ingested_at = _now()
        units = _plan_units(stats_root)
        stored = {row["unit_key"]: row["signature"]
                  for row in conn.execute("SELECT unit_key, signature FROM ingest_unit")}
        totals = {"scanned": 0, "units": len(units), "units_reingested": 0, "indexed": 0,
                  "superseded": 0, "skipped": 0, "errors": 0, "executions": 0,
                  "quarantined": 0, "integrity_errors": 0}
        for unit_key, paths in sorted(units.items()):
            totals["scanned"] += len(paths)
            signature = _unit_signature([
                (str(path.relative_to(stats_root)), path.stat().st_size, _sha256(path))
                for path in paths])
            if not rebuild and stored.get(unit_key) == signature:
                continue
            totals["units_reingested"] += 1
            _delete_unit(conn, unit_key)
            run_ids, counters = ingest_unit(conn, unit_key, paths, stats_root, ingested_at)
            for key, value in counters.items():
                totals[key] = totals.get(key, 0) + value
            conn.execute(
                "INSERT INTO ingest_unit(unit_key, signature, run_ids, files, ingested_at) "
                "VALUES (?,?,?,?,?)",
                (unit_key, signature, json.dumps(sorted(run_ids)), len(paths), ingested_at))
        for unit_key in sorted(set(stored) - set(units)):
            _delete_unit(conn, unit_key)
        for key, value in (
            ("schema_version", SCHEMA_VERSION),
            ("identity_contract", IDENTITY_CONTRACT),
            ("ledger_schema_version", LEDGER_SCHEMA_VERSION),
            ("stats_root", str(stats_root)),
            ("authority", "03_STATS is raw evidence authority; this index is derived and disposable"),
            ("last_rebuild" if rebuild else "last_refresh", ingested_at),
        ):
            conn.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        conn.commit()
        report = validate_index(conn)
        totals.update({
            "runs": conn.execute("SELECT COUNT(*) FROM run").fetchone()[0],
            "modern_runs": conn.execute(
                f"SELECT COUNT(*) FROM run WHERE evidence_grain='{MODERN}'").fetchone()[0],
            "legacy_runs": conn.execute(
                f"SELECT COUNT(*) FROM run WHERE evidence_grain='{LEGACY}'").fetchone()[0],
            "nodes": conn.execute("SELECT COUNT(*) FROM logical_node").fetchone()[0],
            "execution_rows": conn.execute("SELECT COUNT(*) FROM execution").fetchone()[0],
            "modern_executions": conn.execute(
                "SELECT COUNT(*) FROM execution WHERE execution_id IS NOT NULL").fetchone()[0],
            "lifecycle_rows": conn.execute("SELECT COUNT(*) FROM execution_lifecycle").fetchone()[0],
            "findings": conn.execute("SELECT COUNT(*) FROM review_finding").fetchone()[0],
            "commits": conn.execute("SELECT COUNT(*) FROM commit_record").fetchone()[0],
            "candidates": conn.execute("SELECT COUNT(*) FROM candidate").fetchone()[0],
            "human_decisions": conn.execute("SELECT COUNT(*) FROM human_decision").fetchone()[0],
            "preprocess": conn.execute("SELECT COUNT(*) FROM preprocess").fetchone()[0],
            "validation": report,
        })
        return totals
    finally:
        conn.close()


def ingest(db_path: Path = DEFAULT_DB_PATH, stats_root: Path = DEFAULT_STATS_ROOT,
           *, rebuild: bool = False) -> dict[str, Any]:
    """Scan ``stats_root`` and build the derived index.

    A ``rebuild`` builds into a temporary database, validates it and only then
    atomically replaces the published index, so a failed build never destroys a
    working one. Rollback is simply: delete the index and rebuild from raw
    evidence — no raw data migration exists or is needed.
    """
    if not stats_root.is_dir():
        raise FileNotFoundError(f"stats_root not found: {stats_root}")
    if not rebuild:
        conn = connect(db_path)
        try:
            compatible = _schema_ok(conn)
        finally:
            conn.close()
        if compatible:
            return _build(db_path, stats_root, rebuild=False)
        rebuild = True  # V1 index (or none): a full validated republish is required

    staging = db_path.with_name(db_path.name + ".v2build.tmp")
    if staging.exists():
        staging.unlink()
    summary = _build(staging, stats_root, rebuild=True)
    report = summary["validation"]
    if not report["valid"]:
        staging.unlink(missing_ok=True)
        raise RuntimeError("ANALYTICS_V2_VALIDATION_FAILED: " + "; ".join(report["errors"]))
    try:
        os.replace(staging, db_path)
    except OSError as exc:
        staging.unlink(missing_ok=True)
        raise RuntimeError(
            f"ANALYTICS_V2_PUBLISH_FAILED: could not replace {db_path} ({exc}); "
            "the existing index is unchanged") from exc
    summary["published"] = str(db_path)
    return summary


def normalized_snapshot(db_path: Path) -> dict[str, list[tuple[Any, ...]]]:
    """Content snapshot for deterministic-rebuild comparison.

    Excludes volatile bookkeeping (surrogate ids, ingest timestamps, mtimes) and
    resolves ``source_id`` to a stable relative path, so two rebuilds of
    unchanged evidence must compare equal even though the SQLite files differ.
    """
    conn = connect(db_path)
    try:
        sources = {row["id"]: row["rel_path"]
                   for row in conn.execute("SELECT id, rel_path FROM source_file")}
        snapshot: dict[str, list[tuple[Any, ...]]] = {}
        specs = {
            "source_file": ("rel_path", "sha256", "bytes", "layout", "source_type", "status",
                            "detail", "schema_version"),
            "run": _RUN_COLUMNS,
            "logical_node": _NODE_COLUMNS,
            "execution": _EXECUTION_COLUMNS,
            "execution_lifecycle": _LIFECYCLE_COLUMNS,
            "validation": _VALIDATION_COLUMNS,
            "review_finding": _FINDING_COLUMNS,
            "review_producer_link": _PRODUCER_COLUMNS,
            "finding_link": _FINDING_LINK_COLUMNS,
            "preprocess": _PREPROCESS_COLUMNS,
            "commit_record": _COMMIT_COLUMNS,
            "commit_producer_link": _COMMIT_PRODUCER_COLUMNS,
            "candidate": _CANDIDATE_COLUMNS,
            "candidate_execution_link": _CANDIDATE_LINK_COLUMNS,
            "human_decision": _HUMAN_DECISION_COLUMNS,
            "ingest_diagnostic": ("unit_key", "run_id", "path", "kind", "detail"),
            "entity_conflict": ("unit_key", "run_id", "entity", "entity_key", "field",
                                "value_a", "source_a", "value_b", "source_b", "detail"),
            "entity_source": ("entity", "entity_key", "source_id", "role", "run_id"),
        }
        for table, columns in specs.items():
            rows = []
            for row in conn.execute(f"SELECT {', '.join(columns)} FROM {table}"):
                values = []
                for column in columns:
                    value = row[column]
                    if column == "source_id":
                        value = sources.get(value)
                    values.append(value)
                rows.append(tuple(values))
            snapshot[table] = sorted(rows, key=lambda item: json.dumps(item, default=str))
        return snapshot
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Query API (read-only; used by the Control Center Insights page)
# ---------------------------------------------------------------------------

FILTER_KEYS = ("date_from", "date_to", "job_class", "node_type", "model", "profile",
               "invocation_kind", "grain", "include_fixtures")


def _pct(a: int | float | None, b: int | float | None) -> float:
    return round(100.0 * (a or 0) / b, 1) if b else 0.0


def _coverage(a: int, b: int) -> dict[str, Any]:
    return {"n": a, "eligible": b, "pct": _pct(a, b), "label": f"{a}/{b}"}


def _fixture_clause(filters: Mapping[str, Any], *, default_include: bool, alias: str) -> str:
    """Fixtures are excluded from model-performance views unless explicitly enabled.

    Operational views (workload, lifecycle health) include them by default and
    report the fixture share separately, because excluding a real invocation
    from an operational count would understate consumption.
    """
    setting = filters.get("include_fixtures")
    include = default_include if setting is None else bool(setting)
    return "1=1" if include else f"{alias}.fixture_class NOT LIKE 'FIXTURE_%'"


def _exec_where(filters: Mapping[str, Any], *, default_include_fixtures: bool) -> tuple[str, list[Any]]:
    clauses = [_fixture_clause(filters, default_include=default_include_fixtures, alias="e")]
    params: list[Any] = []
    if filters.get("date_from"):
        clauses.append("e.started_day >= ?"); params.append(filters["date_from"])
    if filters.get("date_to"):
        clauses.append("e.started_day <= ?"); params.append(filters["date_to"])
    if filters.get("node_type"):
        clauses.append("e.node_type = ?"); params.append(filters["node_type"])
    if filters.get("model"):
        clauses.append("e.model = ?"); params.append(filters["model"])
    if filters.get("profile"):
        clauses.append("e.profile = ?"); params.append(filters["profile"])
    if filters.get("invocation_kind"):
        clauses.append("e.invocation_kind = ?"); params.append(filters["invocation_kind"])
    if filters.get("grain"):
        clauses.append("e.evidence_grain = ?"); params.append(filters["grain"])
    if filters.get("job_class"):
        clauses.append("r.job_class = ?"); params.append(filters["job_class"])
    return " AND ".join(clauses), params


class Analytics:
    """Thin read-only accessor. Never mutates 03_STATS; only reads the derived index."""

    def __init__(self, db_path: Path = DEFAULT_DB_PATH) -> None:
        self.db_path = db_path

    @property
    def available(self) -> bool:
        if not self.db_path.exists():
            return False
        try:
            conn = connect(self.db_path)
        except sqlite3.Error:
            return False
        try:
            return _schema_ok(conn)
        finally:
            conn.close()

    def _conn(self) -> sqlite3.Connection:
        return connect(self.db_path)

    def meta(self) -> dict[str, str]:
        conn = self._conn()
        try:
            return {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM meta")}
        finally:
            conn.close()

    def filter_options(self) -> dict[str, list[str]]:
        conn = self._conn()
        try:
            def col(sql: str) -> list[str]:
                return [row[0] for row in conn.execute(sql) if row[0]]
            return {
                "job_class": col("SELECT DISTINCT job_class FROM run ORDER BY 1"),
                "node_type": col("SELECT DISTINCT node_type FROM execution ORDER BY 1"),
                "model": col("SELECT DISTINCT model FROM execution ORDER BY 1"),
                "profile": col("SELECT DISTINCT profile FROM execution WHERE profile IS NOT NULL ORDER BY 1"),
                "invocation_kind": col("SELECT DISTINCT invocation_kind FROM execution ORDER BY 1"),
                "grain": [MODERN, LEGACY],
                "fixture_class": col("SELECT DISTINCT fixture_class FROM execution ORDER BY 1"),
            }
        finally:
            conn.close()

    # -- data quality ----------------------------------------------------
    def data_quality(self) -> dict[str, Any]:
        """Execution-grain coverage over ALL evidence, fixtures broken out separately."""
        conn = self._conn()
        try:
            g = lambda sql, *p: conn.execute(sql, p).fetchone()[0]
            runs = g("SELECT COUNT(*) FROM run")
            modern_runs = g("SELECT COUNT(*) FROM run WHERE evidence_grain=?", MODERN)
            legacy_runs = g("SELECT COUNT(*) FROM run WHERE evidence_grain=?", LEGACY)
            unknown_runs = g("SELECT COUNT(*) FROM run WHERE evidence_grain=?", UNKNOWN)
            fixture_runs = g("SELECT COUNT(*) FROM run WHERE fixture_class LIKE 'FIXTURE_%'")
            unknown_fixture_runs = g("SELECT COUNT(*) FROM run WHERE fixture_class=?", UNKNOWN)

            modern_exec = g("SELECT COUNT(*) FROM execution WHERE execution_id IS NOT NULL")
            legacy_rows = g("SELECT COUNT(*) FROM execution WHERE execution_id IS NULL")
            llm_exec = g("SELECT COUNT(*) FROM execution WHERE llm_invocation=1")
            gate_exec = g("SELECT COUNT(*) FROM execution WHERE llm_invocation=0")

            lifecycle = {state: 0 for state in LIFECYCLE_STATES}
            for row in conn.execute(
                    "SELECT lifecycle_state, COUNT(*) n FROM execution_lifecycle "
                    "WHERE indexed_execution=1 GROUP BY 1"):
                lifecycle[row[0]] = row[1]

            provenance = g("""SELECT COUNT(*) FROM execution
                              WHERE llm_invocation=1 AND model IS NOT NULL AND effort IS NOT NULL
                                AND harness IS NOT NULL""")
            usage = g("SELECT COUNT(*) FROM execution WHERE llm_invocation=1 AND usage_observed=1")
            semantic_outcome = g("SELECT COUNT(*) FROM execution WHERE outcome IS NOT NULL")
            reviewed = g("""SELECT COUNT(DISTINCT l.produced_execution_id)
                            FROM review_producer_link l
                            JOIN execution e ON e.execution_id=l.review_execution_id
                            WHERE e.outcome IS NOT NULL""")

            unlinked_preprocess = g("SELECT COUNT(*) FROM preprocess WHERE downstream_link_confidence=?",
                                    "UNLINKED")
            preprocess_total = g("SELECT COUNT(*) FROM preprocess")
            preprocess_invoked = g("SELECT COUNT(*) FROM preprocess WHERE decision_status='INVOKED'")
            unlinked_findings = g("""SELECT COUNT(*) FROM review_finding f
                                     LEFT JOIN execution e ON e.execution_id=f.review_execution_id
                                     WHERE e.execution_id IS NULL""")
            findings_total = g("SELECT COUNT(*) FROM review_finding")
            findings_without_repair = g("""SELECT COUNT(*) FROM review_finding f
                                           WHERE NOT EXISTS (SELECT 1 FROM finding_link k
                                             WHERE k.review_execution_id=f.review_execution_id
                                               AND k.finding_id=f.finding_id)""")
            unlinked_repairs = g("""SELECT COUNT(*) FROM execution
                                    WHERE invocation_kind='REPAIR'
                                      AND originating_review_execution_id IS NULL""")
            commits_total = g("SELECT COUNT(*) FROM commit_record")
            unlinked_commits = g("""SELECT COUNT(*) FROM commit_record c
                                    WHERE NOT EXISTS (SELECT 1 FROM commit_producer_link p
                                      WHERE p.repository=c.repository AND p.commit_hash=c.commit_hash)""")
            commits_cross_validated = g("SELECT COUNT(*) FROM commit_record "
                                        "WHERE observed_in_state=1 AND observed_in_ledger=1")
            candidates = g("SELECT COUNT(*) FROM candidate")
            decisions = g("SELECT COUNT(*) FROM human_decision")
            with_verdict = g("SELECT COUNT(*) FROM human_decision WHERE verdict IS NOT NULL")
            with_quality = g("SELECT COUNT(*) FROM human_decision WHERE quality_assessment IS NOT NULL")
            gate_reached = g("SELECT COUNT(*) FROM run WHERE human_gate_reached=1")
            integrity = g("SELECT COUNT(*) FROM ingest_diagnostic WHERE kind='DATA_INTEGRITY_ERROR'")
            conflicts = g("SELECT COUNT(*) FROM entity_conflict")
            conflicted_entities = g("SELECT COUNT(*) FROM execution WHERE conflict=1")
            parse_errors = g("SELECT COUNT(*) FROM source_file WHERE status='PARSE_ERROR'")
            unknown_layout = g("SELECT COUNT(*) FROM ingest_diagnostic WHERE kind='UNKNOWN_LAYOUT'")
            ledger_runs = g("SELECT COUNT(*) FROM run WHERE ledger_present=1")
            ledger_diag = g("""SELECT COUNT(*) FROM ingest_diagnostic
                               WHERE kind IN ('LEDGER_DAMAGED_TAIL','LEDGER_DAMAGED_RECORD',
                                              'LEDGER_UNTERMINATED_FINAL_RECORD','LEDGER_BLANK_RECORD',
                                              'INVALID_SEQUENCE','MALFORMED_EVENT')""")
            orphan_lifecycle = g("SELECT COUNT(*) FROM execution_lifecycle WHERE indexed_execution=0")

            return {
                # V1-compatible keys
                "indexed_runs": runs,
                "unlinked_preprocess_artifacts": unlinked_preprocess,
                "source_parse_errors": parse_errors,
                "data_integrity_errors": integrity,
                # runs
                "modern_runs": modern_runs, "legacy_runs": legacy_runs,
                "unknown_grain_runs": unknown_runs,
                "fixture_runs": fixture_runs, "unknown_fixture_runs": unknown_fixture_runs,
                "runs_with_ledger": ledger_runs,
                # executions
                "modern_executions": modern_exec,
                "legacy_source_grain_rows": legacy_rows,
                "llm_executions": llm_exec, "machine_gate_executions": gate_exec,
                # lifecycle
                "lifecycle_closed": lifecycle["CLOSED"],
                "lifecycle_intent_only": lifecycle["INTENT_ONLY"],
                "lifecycle_started_open": lifecycle["STARTED_OPEN"],
                "lifecycle_conflicts": lifecycle["LIFECYCLE_CONFLICT"],
                "lifecycle_unknown": lifecycle["UNKNOWN"],
                "executions_with_complete_lifecycle": lifecycle["CLOSED"],
                "complete_lifecycle_pct": _pct(lifecycle["CLOSED"], modern_exec),
                # coverage
                "executions_with_model_provenance": _coverage(provenance, llm_exec),
                "executions_with_token_telemetry": _coverage(usage, llm_exec),
                "executions_with_semantic_outcome": _coverage(semantic_outcome,
                                                              modern_exec + legacy_rows),
                "executions_with_review_outcome": _coverage(reviewed, modern_exec),
                # lineage gaps
                "preprocess_records": preprocess_total,
                "preprocess_invoked": preprocess_invoked,
                "unlinked_preprocess_records": unlinked_preprocess,
                "findings": findings_total,
                "unlinked_findings": unlinked_findings,
                "findings_without_repair_link": findings_without_repair,
                "unlinked_repairs": unlinked_repairs,
                "commits": commits_total, "unlinked_commits": unlinked_commits,
                "commits_cross_validated_state_and_ledger": commits_cross_validated,
                # human evidence
                "candidates": candidates,
                "human_gate_reached": gate_reached,
                "human_decisions": decisions,
                "release_verdict_coverage": _coverage(with_verdict, candidates),
                "quality_assessment_coverage": _coverage(with_quality, candidates),
                # integrity
                "entity_conflicts": conflicts,
                "conflicted_executions": conflicted_entities,
                "unknown_layout_sources": unknown_layout,
                "ledger_diagnostics": ledger_diag,
                "lifecycle_without_indexed_execution": orphan_lifecycle,
                "note": ("Fixture and smoke evidence is counted here but excluded from "
                         "model-performance views by default. Missing values are UNKNOWN, "
                         "never zero and never FAIL."),
            }
        finally:
            conn.close()

    # -- headline cards ---------------------------------------------------
    def key_metrics(self, filters: Mapping[str, Any] | None = None) -> dict[str, Any]:
        filters = filters or {}
        where, params = _exec_where(filters, default_include_fixtures=True)
        conn = self._conn()
        try:
            base = f"FROM execution e JOIN run r ON r.run_id=e.run_id WHERE {where}"
            runs = conn.execute(f"SELECT COUNT(DISTINCT e.run_id) {base}", params).fetchone()[0]
            calls = conn.execute(f"SELECT COUNT(*) {base} AND e.llm_invocation=1", params).fetchone()[0]
            gates = conn.execute(f"SELECT COUNT(*) {base} AND e.llm_invocation=0", params).fetchone()[0]
            fixtures = conn.execute(
                f"SELECT COUNT(*) {base} AND e.fixture_class LIKE 'FIXTURE_%'", params).fetchone()[0]
            # The median is a performance observation, so it follows the
            # model-performance default and drops fixture/smoke stubs whose
            # sub-second wall times would otherwise dominate the sample.
            wall_where, wall_params = _exec_where(filters, default_include_fixtures=False)
            walls = [row[0] for row in conn.execute(
                f"""SELECT e.wall_time_s FROM execution e JOIN run r ON r.run_id=e.run_id
                    WHERE {wall_where} AND e.wall_time_s IS NOT NULL AND e.llm_invocation=1""",
                wall_params)]
            wall_scope = "ALL_EVIDENCE" if filters.get("include_fixtures") else "NON_FIXTURE_ONLY"
            candidates = conn.execute("SELECT COUNT(*) FROM candidate").fetchone()[0]
            verdicts = conn.execute(
                "SELECT COUNT(*) FROM human_decision WHERE verdict IS NOT NULL").fetchone()[0]
            quality = conn.execute(
                "SELECT COUNT(*) FROM human_decision WHERE quality_assessment IS NOT NULL").fetchone()[0]
            return {
                "runs": runs,
                "llm_calls": calls,
                "machine_gate_calls": gates,
                "fixture_executions": fixtures,
                "median_wall_time_s": round(statistics.median(walls), 1) if walls else None,
                "wall_time_sample": len(walls),
                "wall_time_scope": wall_scope,
                "human_acceptance_coverage": f"{verdicts}/{candidates}",
                "human_quality_coverage": f"{quality}/{candidates}",
            }
        finally:
            conn.close()

    # -- A. usage over time ----------------------------------------------
    def usage_over_time(self, filters: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        """Execution-grain LLM invocations per day. Machine gates counted separately.

        A skipped preprocess decision has no execution identity and therefore
        cannot appear here at all.
        """
        filters = filters or {}
        where, params = _exec_where(filters, default_include_fixtures=True)
        conn = self._conn()
        try:
            rows = conn.execute(
                f"""SELECT e.started_day AS day,
                           SUM(CASE WHEN e.llm_invocation=1 THEN 1 ELSE 0 END) AS calls,
                           SUM(CASE WHEN e.llm_invocation=0 THEN 1 ELSE 0 END) AS machine_gate_calls,
                           SUM(CASE WHEN e.llm_invocation=1 AND e.usage_observed=1 THEN 1 ELSE 0 END)
                               AS calls_with_usage,
                           SUM(e.input_tokens) AS input_tokens,
                           SUM(e.cached_input_tokens) AS cached_input_tokens,
                           SUM(e.output_tokens) AS output_tokens,
                           SUM(e.reasoning_tokens) AS reasoning_tokens
                    FROM execution e JOIN run r ON r.run_id=e.run_id
                    WHERE {where} AND e.started_day IS NOT NULL
                    GROUP BY e.started_day ORDER BY e.started_day""", params).fetchall()
            return [dict(row) for row in rows if (row["calls"] or row["machine_gate_calls"])]
        finally:
            conn.close()

    # -- B. outcome by model within a compatible role ---------------------
    def outcome_by_model_profile(self, filters: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        """Observed execution outcomes grouped by (model, effort, role).

        Grouping happens inside one node type / role so heterogeneous work is not
        compared across roles. This is DESCRIPTIVE: allocation was not randomised,
        so it is never a model ranking.
        """
        filters = filters or {}
        where, params = _exec_where(filters, default_include_fixtures=False)
        conn = self._conn()
        try:
            rows = conn.execute(
                f"""SELECT e.model AS model, e.effort AS effort, e.node_type AS node_type,
                           COUNT(*) AS n,
                           SUM(CASE WHEN e.outcome='PASS' THEN 1 ELSE 0 END) AS pass_n,
                           SUM(CASE WHEN e.outcome='FAIL' THEN 1 ELSE 0 END) AS fail_n,
                           SUM(CASE WHEN e.outcome='INVALID' THEN 1 ELSE 0 END) AS invalid_n,
                           SUM(CASE WHEN e.outcome='BLOCKED' THEN 1 ELSE 0 END) AS blocked_n,
                           SUM(CASE WHEN e.outcome IS NULL THEN 1 ELSE 0 END) AS unknown_n
                    FROM execution e JOIN run r ON r.run_id=e.run_id
                    WHERE {where} AND e.llm_invocation=1 AND e.model IS NOT NULL
                      AND e.node_type IS NOT NULL AND e.conflict=0
                    GROUP BY e.model, e.effort, e.node_type
                    ORDER BY n DESC""", params).fetchall()
            out = []
            for row in rows:
                item = dict(row)
                item["insufficient_evidence"] = item["n"] < MIN_SAMPLE
                item["comparison"] = "OBSERVATIONAL_ONLY"
                out.append(item)
            return out
        finally:
            conn.close()

    # -- C. review first pass through explicit lineage --------------------
    def review_first_pass(self, filters: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Review executions that required no repair, via EXPLICIT lineage only.

        Modern rows walk producer execution -> review execution -> finding ->
        selected repair execution. No run/node chronological assumption is used.
        Legacy rows keep the old source-grain run-level metric, reported apart.
        """
        filters = filters or {}
        conn = self._conn()
        try:
            fixture = _fixture_clause(filters, default_include=False, alias="e")
            clauses = [fixture, "e.invocation_kind='REVIEW'", "e.execution_id IS NOT NULL"]
            params: list[Any] = []
            if filters.get("date_from"):
                clauses.append("e.started_day >= ?"); params.append(filters["date_from"])
            if filters.get("date_to"):
                clauses.append("e.started_day <= ?"); params.append(filters["date_to"])
            if filters.get("job_class"):
                clauses.append("r.job_class = ?"); params.append(filters["job_class"])
            if filters.get("model"):
                clauses.append("e.model = ?"); params.append(filters["model"])
            rows = conn.execute(
                f"""SELECT e.execution_id AS review_execution_id, e.outcome AS outcome,
                           (SELECT COUNT(*) FROM review_producer_link l
                             WHERE l.review_execution_id=e.execution_id) AS producers,
                           (SELECT COUNT(*) FROM review_finding f
                             WHERE f.review_execution_id=e.execution_id) AS findings,
                           (SELECT COUNT(*) FROM finding_link k
                             WHERE k.review_execution_id=e.execution_id
                               AND k.link_role='REPAIR_SELECTED') AS selected_repairs
                    FROM execution e JOIN run r ON r.run_id=e.run_id
                    WHERE {' AND '.join(clauses)}""", params).fetchall()
            modern_n = len(rows)
            needed_repair = sum(1 for row in rows
                                if row["selected_repairs"] or row["findings"]
                                or (row["outcome"] not in (None, "PASS")))
            first_pass = sum(1 for row in rows
                             if row["outcome"] == "PASS" and not row["selected_repairs"]
                             and not row["findings"])
            unknown = sum(1 for row in rows if row["outcome"] is None)
            without_producer = sum(1 for row in rows if not row["producers"])

            legacy_fixture = _fixture_clause(filters, default_include=False, alias="r")
            legacy = conn.execute(
                f"""SELECT COUNT(*) AS n,
                           SUM(CASE WHEN v.reviewer_result='PASS' AND COALESCE(v.repair_required,0)=0
                                    THEN 1 ELSE 0 END) AS first_pass,
                           SUM(CASE WHEN COALESCE(v.repair_required,0)=1 THEN 1 ELSE 0 END)
                               AS needed_repair
                    FROM validation v JOIN run r ON r.run_id=v.run_id
                    WHERE {legacy_fixture} AND v.evidence_grain=? AND v.reviewer_result IS NOT NULL""",
                (LEGACY,)).fetchone()
            accepted = conn.execute(
                "SELECT COUNT(*) FROM human_decision WHERE verdict='ACCEPTED'").fetchone()[0]
            rejected = conn.execute(
                "SELECT COUNT(*) FROM human_decision WHERE verdict='REJECTED'").fetchone()[0]
            left = conn.execute(
                "SELECT COUNT(*) FROM human_decision WHERE verdict='LEFT_FOR_LATER'").fetchone()[0]
            return {
                "n": modern_n, "first_pass": first_pass, "needed_repair": needed_repair,
                "outcome_unknown": unknown, "reviews_without_producer_link": without_producer,
                "insufficient_evidence": modern_n < MIN_SAMPLE,
                "basis": "EXPLICIT_EXECUTION_LINEAGE",
                "accepted": accepted, "rejected": rejected, "left_for_later": left,
                "legacy_source_grain": {
                    "n": legacy["n"] or 0, "first_pass": legacy["first_pass"] or 0,
                    "needed_repair": legacy["needed_repair"] or 0,
                    "basis": "RUN_LEVEL_SOURCE_GRAIN",
                },
            }
        finally:
            conn.close()

    # -- D. median execution time -----------------------------------------
    def median_exec_time(self, group_by: str = "model",
                         filters: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        filters = filters or {}
        column = {"model": "e.model", "profile": "e.profile",
                  "node_type": "e.node_type"}.get(group_by, "e.model")
        where, params = _exec_where(filters, default_include_fixtures=False)
        conn = self._conn()
        try:
            rows = conn.execute(
                f"""SELECT {column} AS grp, e.wall_time_s AS wall, e.wall_time_basis AS basis
                    FROM execution e JOIN run r ON r.run_id=e.run_id
                    WHERE {where} AND e.wall_time_s IS NOT NULL AND {column} IS NOT NULL
                      AND e.llm_invocation=1""", params).fetchall()
            buckets: dict[str, list[float]] = {}
            bases: dict[str, set[str]] = {}
            for row in rows:
                buckets.setdefault(row["grp"], []).append(row["wall"])
                bases.setdefault(row["grp"], set()).add(row["basis"] or UNKNOWN)
            out = [{
                "group": group, "n": len(values),
                "median_wall_time_s": round(statistics.median(values), 1),
                "insufficient_evidence": len(values) < MIN_SAMPLE,
                "bases": sorted(bases.get(group, set())),
            } for group, values in sorted(buckets.items())]
            return sorted(out, key=lambda item: item["median_wall_time_s"])
        finally:
            conn.close()

    # -- E. local Qwen preprocessing ---------------------------------------
    def qwen_preprocess_metrics(self, filters: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Preprocess decisions, linked downstream only through explicit identities."""
        filters = filters or {}
        conn = self._conn()
        try:
            clauses = [_fixture_clause(filters, default_include=True, alias="p")]
            params: list[Any] = []
            if filters.get("date_from"):
                clauses.append("p.created_day >= ?"); params.append(filters["date_from"])
            if filters.get("date_to"):
                clauses.append("p.created_day <= ?"); params.append(filters["date_to"])
            where = " AND ".join(clauses)
            g = lambda sql: conn.execute(f"SELECT {sql} FROM preprocess p WHERE {where}",
                                         params).fetchone()[0]
            total = g("COUNT(*)")
            by_status = {row[0]: row[1] for row in conn.execute(
                f"SELECT p.status, COUNT(*) FROM preprocess p WHERE {where} GROUP BY 1", params)}
            invoked = conn.execute(
                f"""SELECT p.wall_time_s, p.downstream_input_before_estimate AS before_estimate,
                           p.downstream_input_after_estimate AS after_estimate,
                           p.input_tokens, p.output_tokens
                    FROM preprocess p WHERE {where} AND p.decision_status='INVOKED'""",
                params).fetchall()
            latencies = [row["wall_time_s"] for row in invoked if row["wall_time_s"] is not None]
            reductions = [row["before_estimate"] - row["after_estimate"] for row in invoked
                          if row["before_estimate"] is not None and row["after_estimate"] is not None]
            downstream = {row[0]: row[1] for row in conn.execute(
                f"""SELECT p.downstream_link_confidence, COUNT(*) FROM preprocess p
                    WHERE {where} GROUP BY 1""", params)}
            with_execution = g("SUM(CASE WHEN p.execution_id IS NOT NULL THEN 1 ELSE 0 END)") or 0
            return {
                "calls": total,
                "decisions": total,
                "by_status": by_status,
                "completed_advisory": len(invoked),
                "invoked_with_execution_identity": with_execution,
                # chart categories stay disjoint: local-unavailable is reported apart
                "skipped": sum(count for status, count in by_status.items()
                               if str(status).startswith("SKIPPED")
                               and status != "SKIPPED_LOCAL_UNAVAILABLE"),
                "skipped_local_unavailable": by_status.get("SKIPPED_LOCAL_UNAVAILABLE", 0),
                "timeout_or_error": sum(by_status.get(key, 0) for key in ("TIMEOUT", "ERROR", "BLOCKED")),
                "downstream_links": downstream,
                "unlinked": downstream.get("UNLINKED", 0),
                "median_latency_s": round(statistics.median(latencies), 1) if latencies else None,
                "estimated_context_reduction_tokens_median":
                    round(statistics.median(reductions), 0) if reductions else None,
                "estimated_context_reduction_sample": len(reductions),
                "note": ("ESTIMATED CONTEXT REDUCTION — derived from producer estimates, not "
                         "paired provider usage. A skipped decision is not an LLM invocation."),
            }
        finally:
            conn.close()

    # -- lifecycle health (the one new view) -------------------------------
    def lifecycle_health(self, filters: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Operational lifecycle evidence. NOT model-quality evidence."""
        filters = filters or {}
        conn = self._conn()
        try:
            fixture = _fixture_clause(filters, default_include=True, alias="e")
            params: list[Any] = []
            clauses = [fixture, "l.indexed_execution=1"]
            if filters.get("date_from"):
                clauses.append("e.started_day >= ?"); params.append(filters["date_from"])
            if filters.get("date_to"):
                clauses.append("e.started_day <= ?"); params.append(filters["date_to"])
            where = " AND ".join(clauses)
            counts = {state: 0 for state in LIFECYCLE_STATES}
            for row in conn.execute(
                    f"""SELECT l.lifecycle_state, COUNT(*) FROM execution_lifecycle l
                        JOIN execution e ON e.execution_id=l.execution_id
                        WHERE {where} GROUP BY 1""", params):
                counts[row[0]] = row[1]
            certainty = {row[0] or UNKNOWN: row[1] for row in conn.execute(
                f"""SELECT l.effect_certainty, COUNT(*) FROM execution_lifecycle l
                    JOIN execution e ON e.execution_id=l.execution_id
                    WHERE {where} AND l.lifecycle_state='CLOSED' GROUP BY 1""", params)}
            reasons = {row[0] or UNKNOWN: row[1] for row in conn.execute(
                f"""SELECT l.close_reason, COUNT(*) FROM execution_lifecycle l
                    JOIN execution e ON e.execution_id=l.execution_id
                    WHERE {where} AND l.lifecycle_state='CLOSED' GROUP BY 1""", params)}
            no_ledger = conn.execute(
                f"""SELECT COUNT(*) FROM execution_lifecycle l
                    JOIN execution e ON e.execution_id=l.execution_id
                    WHERE {where} AND l.basis='NO_LEDGER_EVIDENCE'""", params).fetchone()[0]
            total = sum(counts.values())
            return {
                "closed": counts["CLOSED"], "started_open": counts["STARTED_OPEN"],
                "intent_only": counts["INTENT_ONLY"], "conflicts": counts["LIFECYCLE_CONFLICT"],
                "unknown": counts["UNKNOWN"], "total": total,
                "without_ledger_evidence": no_ledger,
                "effect_certainty": certainty, "close_reasons": reasons,
                "note": ("Operational evidence. A missing CLOSED is unresolved lifecycle, "
                         "not an analytical FAIL. Ledger sequences are append order, not time."),
            }
        finally:
            conn.close()

    # -- readiness ---------------------------------------------------------
    def analytics_readiness(self) -> dict[str, Any]:
        """EVIDENCE readiness. Deliberately does not decide adaptive readiness."""
        dq = self.data_quality()
        modern = dq["modern_executions"]
        return {
            "schema_version": SCHEMA_VERSION,
            "modern_execution_count": modern,
            "legacy_source_grain_rows": dq["legacy_source_grain_rows"],
            "closed_lifecycle_coverage": _pct(dq["lifecycle_closed"], modern),
            "model_provenance_coverage": dq["executions_with_model_provenance"]["pct"],
            "usage_coverage": dq["executions_with_token_telemetry"]["pct"],
            "review_lineage_coverage": dq["executions_with_review_outcome"]["pct"],
            "human_release_verdict_coverage": dq["release_verdict_coverage"]["pct"],
            "human_quality_coverage": dq["quality_assessment_coverage"]["pct"],
            "integrity_error_count": dq["data_integrity_errors"] + dq["entity_conflicts"],
            "unlinked_count": (dq["unlinked_preprocess_records"] + dq["unlinked_findings"]
                               + dq["unlinked_repairs"] + dq["unlinked_commits"]
                               + dq["lifecycle_without_indexed_execution"]),
            "fixture_runs": dq["fixture_runs"],
            "non_fixture_runs": dq["indexed_runs"] - dq["fixture_runs"],
            "kind": "EVIDENCE_READINESS",
            "note": ("Evidence readiness only. Whether AAW may act on this evidence is a "
                     "governance decision and is NOT expressed here."),
        }

    # -- CSV export --------------------------------------------------------
    def export_csv(self, target: Path, filters: Mapping[str, Any] | None = None) -> int:
        """Execution-grain CSV. Derived data, explicitly marked as non-authority."""
        filters = filters or {}
        where, params = _exec_where(filters, default_include_fixtures=True)
        conn = self._conn()
        try:
            rows = conn.execute(
                f"""SELECT e.execution_id, e.row_locator, e.run_id, r.job_class,
                           r.workflow_or_job_id, e.node_id, e.node_type, e.subtask_id,
                           e.invocation_kind, e.llm_invocation, e.evidence_grain,
                           e.fixture_class, e.fixture_evidence, e.provider, e.harness,
                           e.model, e.effort, e.profile, e.started_day, e.started_day_basis,
                           e.outcome, e.outcome_source,
                           l.lifecycle_state, l.close_reason, l.effect_certainty,
                           e.input_tokens, e.cached_input_tokens, e.output_tokens,
                           e.reasoning_tokens, e.usage_observed,
                           e.wall_time_s, e.wall_time_basis, e.conflict
                    FROM execution e
                    JOIN run r ON r.run_id=e.run_id
                    LEFT JOIN execution_lifecycle l ON l.execution_id=e.execution_id
                    WHERE {where}
                    ORDER BY e.started_day, e.run_id, e.node_id, e.row_locator""",
                params).fetchall()
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                header = list(rows[0].keys()) if rows else [
                    "execution_id", "row_locator", "run_id"]
                writer.writerow(["notice"] + header)
                for row in rows:
                    writer.writerow([DERIVED_EXPORT_MARKER] + list(row))
            return len(rows)
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Synthetic-evidence helpers (used by --self-test and by the test suite)
# ---------------------------------------------------------------------------

def _exe(seed: str) -> str:
    return "EXE_" + hashlib.sha256(seed.encode()).hexdigest()[:32]


def _evt(seed: str) -> str:
    return "EVT_" + hashlib.sha256(seed.encode()).hexdigest()[:32]


def write_descriptor(run_dir: Path, *, execution_id: str, node_id: str, invocation_kind: str,
                     subtask_id: str | None = None, model: str | None = None,
                     effort: str | None = None, provider: str | None = "openai",
                     harness: str | None = "codex", created_at: str = "2026-09-06T20:00:00+02:00",
                     provider_session_id: str | None = None, relations: Mapping[str, Any] | None = None,
                     retry_of: str | None = None, fixture_class: str | None = None,
                     run_id: str | None = None) -> Path:
    target = run_dir / "EXECUTIONS"
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"{execution_id}.json"
    path.write_text(json.dumps({
        "schema_version": IDENTITY_CONTRACT, "execution_id": execution_id,
        "run_id": run_id or run_dir.name, "node_id": node_id, "subtask_id": subtask_id,
        "invocation_kind": invocation_kind, "provider": provider, "harness": harness,
        "model": model, "effort": effort, "profile": None,
        "provider_session_id": provider_session_id, "created_at": created_at,
        "input_contract_hash": "c" * 64, "selection_reason": None,
        "policy_version": "AAW_CUSTOM_JOB_V0.3", "fixture_class": fixture_class,
        "retry_of_execution_id": retry_of, "relations": dict(relations or {}),
        "status": "COMPLETED",
    }), encoding="utf-8")
    return path


def write_ledger(run_dir: Path, run_id: str, events: Sequence[Mapping[str, Any]], *,
                 damaged_tail: bool = False) -> Path:
    target = run_dir / "LEDGER"
    target.mkdir(parents=True, exist_ok=True)
    path = target / "execution_events.jsonl"
    lines = []
    for index, event in enumerate(events, 1):
        lines.append(json.dumps({
            "schema_version": LEDGER_SCHEMA_VERSION,
            "event_id": event.get("event_id") or _evt(f"{run_id}:{index}"),
            "event_type": event["event_type"], "run_id": run_id,
            "sequence": event.get("sequence", index),
            "recorded_at": event.get("recorded_at", "2026-09-06T20:00:0%d+02:00" % (index % 10)),
            "execution_id": event.get("execution_id"),
            "payload": event.get("payload") or {},
        }, separators=(",", ":")))
    body = "\n".join(lines) + "\n"
    if damaged_tail:
        body += '{"schema_version": "AAW_EXECUTION_LEDGER_V0.4B", "event_i'
    path.write_text(body, encoding="utf-8")
    return path


def _intent(execution_id: str, **payload: Any) -> dict[str, Any]:
    return {"event_type": "EXECUTION_INTENT", "execution_id": execution_id,
            "payload": {"node_id": payload.get("node_id"), "invocation_kind": payload.get("kind"),
                        "identity_contract": IDENTITY_CONTRACT}}


def _started(execution_id: str, **payload: Any) -> dict[str, Any]:
    return {"event_type": "EXECUTION_STARTED", "execution_id": execution_id,
            "payload": {"start_evidence": "CHILD_PROCESS_SPAWNED",
                        "observed_start_time": payload.get("start", "2026-09-06T20:00:01+02:00"),
                        "process_id": payload.get("pid", 4242),
                        "process_creation_time": "2026-09-06T20:00:00.900+02:00",
                        "process_creation_time_source": "OS_PROCESS_TIMES"}}


def _closed(execution_id: str, **payload: Any) -> dict[str, Any]:
    return {"event_type": "EXECUTION_CLOSED", "execution_id": execution_id,
            "payload": {"observed_close_time": payload.get("close", "2026-09-06T20:00:05+02:00"),
                        "close_reason": payload.get("reason", "COMPLETED"),
                        "effect_certainty": payload.get("certainty", "CONFIRMED"),
                        "observation_source": payload.get("source", "CHILD_PROCESS_EXIT"),
                        "exit_code": payload.get("exit_code", 0),
                        "outcome": payload.get("outcome", "PASS"),
                        "timed_out": False, "cancelled": False, "interrupted": False}}


# ---------------------------------------------------------------------------
# Self-test (offline, synthetic evidence, never touches 03_STATS)
# ---------------------------------------------------------------------------

def _self_test() -> int:
    failures: list[str] = []

    def check(condition: bool, name: str) -> None:
        if not condition:
            failures.append(name)

    with tempfile.TemporaryDirectory(prefix="aaw_analytics_v2_") as tmp:
        root = Path(tmp) / "03_STATS"
        run = root / "AAW_MODERN"
        (run / "CUSTOM_JOB").mkdir(parents=True)
        (run / "PREPROCESS").mkdir(parents=True)

        s1_a, s1_b = _exe("s1a"), _exe("s1b")
        gate = _exe("gate")
        review_a, review_b = _exe("reviewA"), _exe("reviewB")
        repair = _exe("repair")
        delta = _exe("delta")

        # Same logical node S1, two distinct executions (a genuine retry).
        write_descriptor(run, execution_id=s1_a, node_id="S1", subtask_id="S1",
                         invocation_kind="LLM", model="gpt-5.6-terra", effort="high",
                         provider_session_id="session-shared")
        write_descriptor(run, execution_id=s1_b, node_id="S1", subtask_id="S1",
                         invocation_kind="LLM", model="gpt-5.6-terra", effort="high",
                         provider_session_id="session-shared", retry_of=s1_a,
                         created_at="2026-09-06T20:00:00+02:00")
        write_descriptor(run, execution_id=gate, node_id="S1:GATE:1", subtask_id="S1",
                         invocation_kind="MACHINE_GATE", provider="LOCAL", harness="subprocess")
        write_descriptor(run, execution_id=review_a, node_id="REVIEW", invocation_kind="REVIEW",
                         model="gpt-5.6-sol", effort="high",
                         relations={"reviewed_execution_ids": [s1_b],
                                    "reviewed_base": "base", "reviewed_head": "head"})
        write_descriptor(run, execution_id=review_b, node_id="REVIEW", invocation_kind="REVIEW",
                         model="gpt-5.6-sol", effort="high",
                         relations={"reviewed_execution_ids": [s1_a]})
        write_descriptor(run, execution_id=repair, node_id="REPAIR", invocation_kind="REPAIR",
                         model="gpt-5.6-luna", effort="high",
                         relations={"originating_review_execution_id": review_a,
                                    "selected_finding_keys": [
                                        {"review_execution_id": review_a, "finding_id": "F001"}]})
        write_descriptor(run, execution_id=delta, node_id="DELTA_REVIEW",
                         invocation_kind="DELTA_REVIEW", model="gpt-5.6-luna", effort="high",
                         relations={"original_review_execution_id": review_a,
                                    "repair_execution_id": repair,
                                    "finding_dispositions": [
                                        {"finding_key": {"review_execution_id": review_a,
                                                         "finding_id": "F001"},
                                         "disposition": "RESOLVED"}]})

        (run / "CUSTOM_JOB" / "job_state.json").write_text(json.dumps({
            "schema_version": "AAW_CUSTOM_JOB_RUN_V0.4A", "AAW_RUN_ID": "AAW_MODERN",
            "job_id": "analytics-v2-fixture", "job_type": "MULTI_SUBTASK", "goal": "two runs",
            "status": "WAITING_FOR_HUMAN", "final_acceptance": "WAITING_FOR_HUMAN",
            "started_at": "2026-09-06T20:00:00+02:00", "updated_at": "2026-09-06T20:01:00+02:00",
            "repository": r"D:\real\repo", "worktree": r"D:\real\worktree",
            "human_verdict": None, "review_findings_count": 1,
            "executions": [
                {"execution_id": s1_a, "node_id": "S1", "subtask_id": "S1",
                 "invocation_kind": "LLM", "model": "gpt-5.6-terra", "effort": "high"},
                {"execution_id": s1_b, "node_id": "S1", "subtask_id": "S1",
                 "invocation_kind": "LLM", "model": "gpt-5.6-terra", "effort": "high"},
                {"execution_id": gate, "node_id": "S1:GATE:1", "subtask_id": "S1",
                 "invocation_kind": "MACHINE_GATE"},
                {"execution_id": review_a, "node_id": "REVIEW", "invocation_kind": "REVIEW"},
                {"execution_id": review_b, "node_id": "REVIEW", "invocation_kind": "REVIEW"},
                {"execution_id": repair, "node_id": "REPAIR", "invocation_kind": "REPAIR"},
                {"execution_id": delta, "node_id": "DELTA_REVIEW", "invocation_kind": "DELTA_REVIEW"},
            ],
            "telemetry": [
                {"node_type": "SUBTASK", "execution_id": s1_a, "node_id": "S1", "subtask_id": "S1",
                 "model": "gpt-5.6-terra", "effort": "high", "harness": "codex",
                 "provider_session": "session-shared", "wall_time_s": 11.0,
                 "input_tokens": 1000, "cached_input": 500, "output_tokens": 100,
                 "reasoning_tokens": 10, "subtask_index": 1},
                {"node_type": "SUBTASK", "execution_id": s1_b, "node_id": "S1", "subtask_id": "S1",
                 "model": "gpt-5.6-terra", "effort": "high", "harness": "codex",
                 "provider_session": "session-shared", "wall_time_s": 13.0,
                 "input_tokens": 900, "cached_input": 400, "output_tokens": 90,
                 "reasoning_tokens": 9, "subtask_index": 1},
                {"node_type": "REVIEW", "execution_id": review_a, "node_id": "REVIEW",
                 "model": "gpt-5.6-sol", "effort": "high", "harness": "codex",
                 "wall_time_s": 20.0, "input_tokens": 800, "output_tokens": 80},
                {"node_type": "REVIEW", "execution_id": review_b, "node_id": "REVIEW",
                 "model": "gpt-5.6-sol", "effort": "high", "harness": "codex",
                 "wall_time_s": 22.0},
            ],
            "subtask_results": [
                {"outcome": "PASS", "summary": "ok", "subtask_id": "S1", "subtask_index": 1,
                 "execution_id": s1_b, "machine_gate_result": "PASS",
                 "checkpoint_commit": "c0ffee", "checkpoint_commit_record": {
                     "repository": r"D:\real\repo", "commit_hash": "c0ffee", "subtask_id": "S1",
                     "producer_execution_ids": [s1_b], "expected_parent": "base",
                     "role": "SUBTASK"}},
            ],
            "machine_gates": [
                {"command": ["python"], "result": "PASS", "returncode": 0, "wall_time_s": 0.2,
                 "execution_id": gate, "node_id": "S1:GATE:1", "subtask_id": "S1"},
            ],
            "commit_records": [
                {"repository": r"D:\real\repo", "commit_hash": "c0ffee", "subtask_id": "S1",
                 "producer_execution_ids": [s1_b], "expected_parent": "base", "role": "SUBTASK"},
            ],
            "review": {
                "outcome": "FAIL", "summary": "one finding", "execution_id": review_a,
                "reviewed_execution_ids": [s1_b],
                "findings": [{"finding_id": "F001", "severity": "P1", "file": "a.py",
                              "location": "1", "description": "bad", "required_fix": "fix",
                              "finding_key": {"review_execution_id": review_a,
                                              "finding_id": "F001"}}],
            },
            "selected_repairs": ["F001"], "repair_commit": "beadfeed",
            "delta_review": {"outcome": "PASS", "execution_id": delta,
                             "repair_execution_id": repair,
                             "original_review_execution_id": review_a},
            "candidate": {"schema_version": "AAW_CANDIDATE_V0.4A", "candidate_id": "CAN_a" * 1,
                          "run_id": "AAW_MODERN"},
            "human_decisions": [],
        }), encoding="utf-8")
        (run / "CUSTOM_JOB" / "candidate.json").write_text(json.dumps({
            "schema_version": "AAW_CANDIDATE_V0.4A", "candidate_id": "CAN_selftest",
            "run_id": "AAW_MODERN", "repository": r"D:\real\repo",
            "candidate_head": "beadfeed", "artifact_manifest": None,
            "review_execution_ids": [review_a, delta], "check_execution_ids": [gate],
            "created_at": "2026-09-06T20:01:00+02:00", "content_identity_hash": "h" * 64,
        }), encoding="utf-8")
        (run / "CUSTOM_JOB" / "HDE_selftest.json").write_text(json.dumps({
            "schema_version": "AAW_HUMAN_DECISION_V0.4A", "human_decision_id": "HDE_selftest",
            "candidate_id": "CAN_selftest", "verdict": "LEFT_FOR_LATER",
            "timestamp": "2026-09-06T20:02:00+02:00", "quality_assessment": None, "reason": None,
        }), encoding="utf-8")

        # Preprocess: one skipped (no execution identity) linked to S1_a, one to
        # S1_b, and one dangling.
        for name, downstream in (("S1__SKIP__a", s1_a), ("S1__SKIP__b", s1_b)):
            (run / "PREPROCESS" / f"{name}.json").write_text(json.dumps({
                "schema_version": "AAW_LOCAL_QWEN_PREPROCESS_V0.4A", "run_id": "AAW_MODERN",
                "node_id": "S1", "node_type": "SUBTASK",
                "preprocess_id": "PRE_" + hashlib.sha256(name.encode()).hexdigest()[:32],
                "execution_id": None, "status": "SKIPPED_POLICY_OFF",
                "downstream_execution_id": downstream, "consumed": False,
                "created_at": "2026-09-06T20:00:00+02:00",
                "telemetry": {"downstream_node_id": "S1", "input_chars": 0, "output_chars": 0,
                              "wall_time_s": 0.0, "downstream_input_before_estimate": 500},
            }), encoding="utf-8")
        (run / "PREPROCESS" / "GHOST__SKIP.json").write_text(json.dumps({
            "schema_version": "AAW_LOCAL_QWEN_PREPROCESS_V0.1", "run_id": "AAW_MODERN",
            "node_id": "NOPE", "node_type": "GHOST", "status": "SKIPPED_NOT_USEFUL",
            "created_at": "2026-09-06T20:00:00+02:00",
            "telemetry": {"downstream_node_id": "NOPE_9"},
        }), encoding="utf-8")
        # One invoked preprocess with real local inference telemetry.
        (run / "PREPROCESS" / "REVIEW__LOCAL_QWEN_SUMMARY__x.json").write_text(json.dumps({
            "schema_version": "AAW_LOCAL_QWEN_PREPROCESS_V0.4A", "run_id": "AAW_MODERN",
            "node_id": "REVIEW", "node_type": "REVIEW", "preprocess_id": "PRE_invoked",
            "preprocess_type": "LOCAL_QWEN_SUMMARY", "profile": "LOCAL_QWEN_SUMMARY",
            "status": "COMPLETED_ADVISORY", "downstream_execution_id": review_a,
            "created_at": "2026-09-06T20:00:00+02:00", "consumed": True,
            "output_artifact_hash": "o" * 64,
            "telemetry": {"downstream_node_id": "REVIEW", "input_chars": 5000,
                          "output_chars": 400, "wall_time_s": 30.0,
                          "downstream_input_before_estimate": 5000,
                          "downstream_input_after_estimate": 1200},
        }), encoding="utf-8")

        # Ledger: gate closed, s1_a started but never closed, s1_b intent only.
        write_ledger(run, "AAW_MODERN", [
            _intent(gate, node_id="S1:GATE:1", kind="MACHINE_GATE"),
            _started(gate), _closed(gate),
            _intent(s1_a, node_id="S1", kind="LLM"), _started(s1_a),
            _intent(s1_b, node_id="S1", kind="LLM"),
            {"event_type": "COMMIT_RECORDED", "execution_id": None,
             "payload": {"repository": r"D:\real\repo", "commit_hash": "c0ffee",
                         "subtask_id": "S1", "role": "SUBTASK", "expected_parent": "base",
                         "producer_execution_ids": [s1_b], "git_evidence_hash": "g" * 64}},
            {"event_type": "HUMAN_DECISION_RECORDED", "execution_id": None,
             "payload": {"human_decision_id": "HDE_selftest", "candidate_id": "CAN_selftest",
                         "verdict": "LEFT_FOR_LATER", "quality_assessment": None,
                         "decision_artifact_hash": "d" * 64}},
        ], damaged_tail=True)

        # A legacy run: same node id, timestamp-adjacent, must never join.
        legacy = root / "AAW_LEGACY"
        (legacy / "WORKFLOW").mkdir(parents=True)
        (legacy / "WORKFLOW" / "workflow_state.json").write_text(json.dumps({
            "workflow_id": "IMPLEMENT_REVIEW_REPAIR_V1", "AAW_RUN_ID": "AAW_LEGACY",
            "goal": "legacy", "status": "WAITING_FOR_HUMAN", "final_outcome": "HUMAN_REQUIRED",
            "started_at": "2026-09-06T20:00:00+02:00", "updated_at": "2026-09-06T20:05:00+02:00",
            "repo": r"D:\real\legacy", "worktree": r"D:\real\legacy\wt",
            "human_verdict": None, "repair_cycle": 0,
            "completed_nodes": [
                {"node_id": "S1", "node_type": "IMPLEMENT", "outcome": "PASS", "duration_s": 60.0},
                {"node_id": "N03", "node_type": "REVIEW", "outcome": "PASS", "duration_s": 70.0},
            ],
            "telemetry": [
                {"workflow_node_id": "S1", "workflow_node_type": "IMPLEMENT", "harness": "codex",
                 "model": "gpt-5.6-terra", "effort": "high", "provider_session_id": "session-shared",
                 "started_at": "2026-09-06T20:00:00+02:00", "wall_time_s": 60.0,
                 "outcome": "VALID PASS",
                 "usage": {"input_tokens": 10, "output_tokens": 1}},
                {"workflow_node_id": "N03", "workflow_node_type": "REVIEW", "harness": "codex",
                 "model": "gpt-5.6-sol", "effort": "high",
                 "started_at": "2026-09-06T20:02:00+02:00", "wall_time_s": 70.0,
                 "outcome": "VALID PASS"},
            ],
        }), encoding="utf-8")
        (legacy / "N01__IMPLEMENT__codex__x.json").write_text(json.dumps({
            "schema_version": "1.0", "run_id": "AAW_LEGACY", "node": "S1",
            "model": "gpt-5.6-terra", "usage": {"input_tokens": 10, "output_tokens": 1},
        }), encoding="utf-8")
        (root / "run_20260901_old__N05_BUILD__20260901T210001+0200.json").write_text(json.dumps({
            "run_id": "run_20260901_old", "pipeline": "P04", "node": "N05_BUILD",
            "agent": "claude", "model": "claude-opus-5", "effort": "high",
            "completed_at": "2026-09-01T21:33:59+02:00", "usage": None, "outcome": "skipped",
        }), encoding="utf-8")
        (legacy / "broken.json").write_text("{ not json", encoding="utf-8")

        db = Path(tmp) / "idx.sqlite"
        summary = ingest(db, root, rebuild=True)
        check(summary["errors"] >= 1, "malformed evidence recorded as an error, not a crash")
        check(summary["validation"]["valid"], "freshly built index validates before publish")

        conn = connect(db)
        try:
            q = lambda sql, *p: conn.execute(sql, p).fetchone()[0]
            # A. same run + same node + two execution IDs -> two executions
            check(q("SELECT COUNT(*) FROM execution WHERE run_id='AAW_MODERN' AND node_id='S1'") == 2,
                  "repeated node keeps two distinct executions")
            # B. shared provider_session_id never merges executions
            check(q("""SELECT COUNT(DISTINCT execution_id) FROM execution
                       WHERE provider_session_id='session-shared' AND execution_id IS NOT NULL""") == 2,
                  "shared provider session does not merge executions")
            # C. F001 under two review executions stays two findings
            check(q("SELECT COUNT(*) FROM review_finding WHERE finding_id='F001'") >= 1,
                  "finding keyed by review execution")
            # D. legacy S1 never joins modern S1
            check(q("SELECT COUNT(*) FROM execution WHERE node_id='S1'") == 3,
                  "legacy S1 stays separate from the two modern S1 executions")
            check(q("SELECT COUNT(*) FROM execution WHERE run_id='AAW_LEGACY' "
                    "AND execution_id IS NOT NULL") == 0,
                  "legacy rows keep a NULL execution identity")
            check(q("SELECT COUNT(*) FROM execution WHERE row_locator IS NULL") == 0,
                  "every row has a derived locator")
            # E. preprocess links to two different downstream executions
            check(q("""SELECT COUNT(DISTINCT downstream_execution_id) FROM preprocess
                       WHERE downstream_link_confidence='EXPLICIT_EXECUTION_ID'""") == 3,
                  "preprocess links resolve per downstream execution")
            check(q("SELECT COUNT(*) FROM preprocess WHERE downstream_link_confidence='UNLINKED'") == 1,
                  "dangling preprocess stays UNLINKED")
            check(q("SELECT COUNT(*) FROM preprocess WHERE execution_id IS NOT NULL") == 0,
                  "skipped preprocess is never an LLM invocation")
            # F. repair is not a retry
            check(q("SELECT originating_review_execution_id FROM execution "
                    "WHERE invocation_kind='REPAIR'") == review_a,
                  "repair names its originating review")
            check(q("SELECT retry_of_execution_id FROM execution "
                    "WHERE invocation_kind='REPAIR'") is None,
                  "repair carries no retry relation")
            check(q("SELECT retry_of_execution_id FROM execution WHERE execution_id=?", s1_b) == s1_a,
                  "a genuine retry keeps its retry relation")
            # G. commit in state + ledger is one entity
            check(q("SELECT COUNT(*) FROM commit_record WHERE commit_hash='c0ffee'") == 1,
                  "a commit observed twice is one commit entity")
            check(q("SELECT observed_in_state + observed_in_ledger FROM commit_record "
                    "WHERE commit_hash='c0ffee'") == 2, "commit cross-validated in both sources")
            # H/I. human decision references the exact candidate
            check(q("SELECT candidate_id FROM human_decision WHERE human_decision_id='HDE_selftest'")
                  == "CAN_selftest", "human decision references the exact candidate")
            check(q("SELECT source_authority FROM human_decision "
                    "WHERE human_decision_id='HDE_selftest'") == "HUMAN_DECISION_ARTIFACT",
                  "the immutable decision artifact wins over state and ledger")
            check(q("SELECT verdict FROM human_decision") == "LEFT_FOR_LATER"
                  and q("SELECT quality_assessment FROM human_decision") is None,
                  "LEFT_FOR_LATER is not a rejection and quality stays UNKNOWN")
            # M/N. lifecycle honesty
            states = {row[0]: row[1] for row in conn.execute(
                "SELECT lifecycle_state, COUNT(*) FROM execution_lifecycle GROUP BY 1")}
            check(states.get("STARTED_OPEN") == 1, "started-without-closed stays unresolved")
            check(states.get("INTENT_ONLY") == 1, "intent-only stays unresolved")
            check(states.get("CLOSED") == 1, "closed lifecycle recorded")
            check(states.get(UNKNOWN, 0) == 4, "modern executions with no ledger evidence are UNKNOWN")
            check(q("SELECT COUNT(*) FROM execution WHERE outcome='FAIL' "
                    "AND execution_id=?", review_a) == 1, "reviewer FAIL preserved")
            # descriptor precedence
            check(q("SELECT identity_authority FROM execution WHERE execution_id=?", gate)
                  == "EXECUTION_DESCRIPTOR", "descriptor is identity authority")
            # damaged ledger tail is a warning, not data loss
            check(q("SELECT COUNT(*) FROM ingest_diagnostic WHERE kind='LEDGER_DAMAGED_TAIL'") == 1,
                  "damaged ledger tail reported without discarding the ledger")
            # machine gates are not LLM calls
            check(q("SELECT llm_invocation FROM execution WHERE execution_id=?", gate) == 0,
                  "machine gate is not counted as an LLM invocation")
            check(q("SELECT COUNT(*) FROM execution WHERE llm_invocation=1") == 9,
                  "LLM invocation count is execution-grain")
            # legacy flat run keeps no invented run status
            check(q("SELECT final_status FROM run WHERE run_id='run_20260901_old'") == UNKNOWN,
                  "legacy flat run has no invented run-level status")
        finally:
            conn.close()

        api = Analytics(db)
        # K. fixtures do not contaminate default real metrics
        fixture_root = Path(tmp) / "03_STATS_FIXTURE"
        (fixture_root / "AAW_FIX" / "CUSTOM_JOB").mkdir(parents=True)
        fixture_exec = _exe("fixture")
        write_descriptor(fixture_root / "AAW_FIX", execution_id=fixture_exec, node_id="S1",
                         subtask_id="S1", invocation_kind="LLM", model="fixture-model",
                         effort="high")
        (fixture_root / "AAW_FIX" / "CUSTOM_JOB" / "job_state.json").write_text(json.dumps({
            "schema_version": "AAW_CUSTOM_JOB_RUN_V0.4A", "AAW_RUN_ID": "AAW_FIX",
            "job_id": "self-test", "job_type": "MULTI_SUBTASK", "goal": "fixture",
            "status": "WAITING_FOR_HUMAN", "started_at": "2026-09-06T20:00:00+02:00",
            "repository": r"D:\real\repo2",
            "executions": [{"execution_id": fixture_exec, "node_id": "S1",
                            "invocation_kind": "LLM", "model": "fixture-model"}],
            "telemetry": [], "subtask_results": [
                {"outcome": "PASS", "subtask_id": "S1", "subtask_index": 1,
                 "execution_id": fixture_exec}],
        }), encoding="utf-8")
        fixture_db = Path(tmp) / "fixture.sqlite"
        ingest(fixture_db, fixture_root, rebuild=True)
        fixture_api = Analytics(fixture_db)
        models = {row["model"] for row in fixture_api.outcome_by_model_profile()}
        check("fixture-model" not in models, "fixtures excluded from default model view")
        check("fixture-model" in {row["model"] for row in
                                  fixture_api.outcome_by_model_profile({"include_fixtures": True})},
              "fixtures visible when explicitly enabled")
        conn = connect(fixture_db)
        try:
            check(conn.execute("SELECT fixture_class FROM run").fetchone()[0] == "FIXTURE_SELF_TEST",
                  "declared self-test job id classifies the run as a fixture")
            check(conn.execute("SELECT fixture_evidence FROM run").fetchone()[0]
                  == "DECLARED_SELF_TEST_JOB_ID", "fixture evidence is recorded and auditable")
        finally:
            conn.close()

        # deterministic rebuild
        first = normalized_snapshot(db)
        ingest(db, root, rebuild=True)
        check(normalized_snapshot(db) == first, "two rebuilds produce equivalent content")
        # idempotent refresh
        ingest(db, root, rebuild=False)
        ingest(db, root, rebuild=False)
        check(normalized_snapshot(db) == first, "incremental refresh is idempotent")

        # L. duplicate incompatible execution identity is quarantined
        duplicate_root = Path(tmp) / "03_STATS_DUP"
        for run_name, node in (("AAW_D1", "S1"), ("AAW_D2", "S2")):
            target = duplicate_root / run_name
            (target / "CUSTOM_JOB").mkdir(parents=True)
            write_descriptor(target, execution_id=_exe("dup"), node_id=node,
                             invocation_kind="LLM", model="m", effort="high",
                             run_id=run_name)
            (target / "CUSTOM_JOB" / "job_state.json").write_text(json.dumps({
                "schema_version": "AAW_CUSTOM_JOB_RUN_V0.4A", "AAW_RUN_ID": run_name,
                "job_id": "dup", "job_type": "MULTI_SUBTASK", "goal": "dup",
                "status": "RUNNING", "started_at": "2026-09-06T20:00:00+02:00",
                "repository": r"D:\real\dup", "executions": [], "telemetry": [],
            }), encoding="utf-8")
        duplicate_db = Path(tmp) / "dup.sqlite"
        ingest(duplicate_db, duplicate_root, rebuild=True)
        conn = connect(duplicate_db)
        try:
            check(conn.execute("SELECT COUNT(*) FROM execution WHERE execution_id=?",
                               (_exe("dup"),)).fetchone()[0] == 1,
                  "one row survives for one execution identity")
            check(conn.execute("SELECT COUNT(*) FROM ingest_diagnostic "
                               "WHERE kind='DATA_INTEGRITY_ERROR'").fetchone()[0] >= 1,
                  "incompatible duplicate identity raises DATA_INTEGRITY_ERROR")
            check(conn.execute("SELECT COUNT(*) FROM entity_conflict").fetchone()[0] >= 1,
                  "the conflict is recorded, not silently resolved")
        finally:
            conn.close()

        dq = api.data_quality()
        check(dq["modern_executions"] == 7, f"7 modern executions (got {dq['modern_executions']})")
        check(dq["legacy_source_grain_rows"] == 3,
              f"3 legacy source-grain rows (got {dq['legacy_source_grain_rows']})")
        check(dq["quality_assessment_coverage"]["n"] == 0,
              "no human quality assessment in synthetic evidence")
        check(dq["release_verdict_coverage"]["n"] == 1, "one release verdict recorded")
        health = api.lifecycle_health()
        check(health["closed"] == 1 and health["started_open"] == 1 and health["intent_only"] == 1,
              "lifecycle health reports observed states only")
        rfp = api.review_first_pass({"include_fixtures": True})
        check(rfp["n"] == 2 and rfp["basis"] == "EXPLICIT_EXECUTION_LINEAGE",
              f"review lineage is execution-grain (got n={rfp['n']})")
        check(rfp["needed_repair"] == 1, "the reviewed finding drives repair-required")
        readiness = api.analytics_readiness()
        check("READY_FOR_ADAPTIVE" not in json.dumps(readiness),
              "readiness never claims adaptive readiness")
        check(readiness["kind"] == "EVIDENCE_READINESS", "readiness is evidence readiness")
        usage = api.usage_over_time({"include_fixtures": True})
        check(sum(row["calls"] for row in usage) == 9, "usage counts LLM executions only")
        check(sum(row["machine_gate_calls"] for row in usage) == 1,
              "machine gates counted separately")
        export = Path(tmp) / "export.csv"
        rows = api.export_csv(export)
        text = export.read_text(encoding="utf-8")
        check(rows > 0 and DERIVED_EXPORT_MARKER in text, "CSV export marked as non-authority")
        check("execution_id" in text.splitlines()[0], "CSV export carries execution identity")

        # A growing ledger is detected on refresh and never duplicated. This runs
        # last because it deliberately changes the lifecycle evidence above.
        write_ledger(run, "AAW_MODERN", [
            _intent(gate, node_id="S1:GATE:1", kind="MACHINE_GATE"), _started(gate), _closed(gate),
            _intent(s1_a, node_id="S1", kind="LLM"), _started(s1_a), _closed(s1_a),
            _intent(s1_b, node_id="S1", kind="LLM"),
        ])
        ingest(db, root, rebuild=False)
        conn = connect(db)
        try:
            check(conn.execute("SELECT lifecycle_state FROM execution_lifecycle "
                               "WHERE execution_id=?", (s1_a,)).fetchone()[0] == "CLOSED",
                  "appended ledger evidence is picked up on refresh")
            check(conn.execute("SELECT COUNT(*) FROM execution_lifecycle "
                               "WHERE execution_id=?", (s1_a,)).fetchone()[0] == 1,
                  "refresh never duplicates lifecycle rows")
            check(conn.execute("SELECT COUNT(*) FROM execution WHERE execution_id=?",
                               (s1_a,)).fetchone()[0] == 1,
                  "refresh never duplicates executions")
        finally:
            conn.close()

    if failures:
        print("ANALYTICS_V2_SELF_TEST_FAILED")
        for failure in failures:
            print(" -", failure)
        return 1
    print("ANALYTICS_V2_SELF_TEST_OK")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--stats-root", type=Path, default=DEFAULT_STATS_ROOT)
    parser.add_argument("--rebuild", action="store_true",
                        help="Temp build from 03_STATS, validate, then publish atomically")
    parser.add_argument("--refresh", action="store_true", help="Incremental, idempotent update")
    parser.add_argument("--data-quality", action="store_true")
    parser.add_argument("--readiness", action="store_true")
    parser.add_argument("--lifecycle", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)

    if args.self_test:
        return _self_test()
    if args.rebuild or args.refresh:
        summary = ingest(args.db, args.stats_root, rebuild=args.rebuild)
        print(json.dumps(summary, indent=2, default=str))
        return 0
    if args.data_quality:
        print(json.dumps(Analytics(args.db).data_quality(), indent=2, default=str))
        return 0
    if args.readiness:
        print(json.dumps(Analytics(args.db).analytics_readiness(), indent=2, default=str))
        return 0
    if args.lifecycle:
        print(json.dumps(Analytics(args.db).lifecycle_health(), indent=2, default=str))
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
