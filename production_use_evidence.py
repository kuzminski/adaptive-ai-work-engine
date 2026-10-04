#!/usr/bin/env python3
"""AAW PRODUCTION USE EVIDENCE CAPTURE V0.1 — real-operator-session evidence.

This is an instrumentation layer, not a redesign. It durabilizes one concrete
gap found by auditing the current live-canvas product: `aaw_bridge.AawBridge`
already emits a full planner-decision event vocabulary (`PLANNER_STARTED`,
`PLANNER_COMPLETED`/`FAILED`, `PROPOSAL_READY`/`INVALID`/`STALE`,
`PROPOSAL_ACCEPTED`/`REJECTED` — see `planner_proposal.py`), but keeps it in
`self._planner_journal`, an in-memory list capped at 500 rows and lost on
restart. Everything on the *execution* side is already durable and already
indexed (`execution_ledger.py`, `routing_contract.RoutingJournal`,
`workflow_runner.workflow_summary()`, `CONTROL_CENTER/ANALYTICS/aaw_analytics.py`)
and none of it is duplicated here — it is read straight from
`03_STATS/<run_id>/...` at projection time.

What this module adds, concretely:

  * `EvidenceRecorder` — durabilizes the bridge's own planner-decision journal
    verbatim to `PLANNER_EVIDENCE_ROOT/planner_decisions.jsonl`, links an
    accepted proposal to the run it fed (`session_run_links.jsonl`, observed
    directly at the bridge call site — never inferred from timestamps or
    filenames), and records append-only operator feedback
    (`operator_feedback.jsonl`).
  * `EvidenceBridge(AawBridge)` — a thin subclass overriding exactly four
    methods (`plan_from_node`, `accept_proposal`, `reject_proposal`,
    `start_run`). Each calls the real method first and hands its already-true
    result to the recorder; no return value is ever altered, so no planner or
    runtime behavior can change by using this class instead of `AawBridge`.
  * `all_sessions()` / `session_summary()` / `aggregate_report()` — pure,
    stateless projections folded from the three JSONL files plus existing
    `03_STATS` evidence. There is no cached/derived database: calling these
    again after deleting nothing (there is nothing derived to delete) always
    reproduces the same output from the same raw evidence.

Session identity (`session_id`) is always an ID the bridge already mints —
`request_id` (`"PLANREQ-..."`) for a planned session, or `run_id` itself for a
run started without a preceding planner ask. No new identity is invented.

No adaptive scoring, cost computation, or model recommendation happens here.
`known_cost` is always `null`: the codebase has no dollar-cost field anywhere
(confirmed by audit — only a paid/free `access_class` flag), and this task
does not invent one.
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

import aaw_bridge
import aaw_paths
import execution_ledger
import planner_proposal as pp
import routing_contract

SCHEMA_VERSION = "AAW_PRODUCTION_USE_EVIDENCE_V0.1"

USEFULNESS_VALUES = ("USEFUL", "PARTIAL", "NOT_USEFUL")
REUSE_INTENT_VALUES = ("YES", "WITH_CHANGES", "NO")

DECISIONS_FILENAME = "planner_decisions.jsonl"
LINKS_FILENAME = "session_run_links.jsonl"
FEEDBACK_FILENAME = "operator_feedback.jsonl"

_PLAN_PHASE_STATUS_EVENTS = (pp.PROPOSAL_READY, pp.PROPOSAL_INVALID, pp.PROPOSAL_STALE)


def _now() -> str:
    return routing_contract.now()


# ─────────────────────────── JSONL primitives ───────────────────────────

def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def _append_jsonl(path: Path, row: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(dict(row), ensure_ascii=False, default=str))
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())


# ─────────────────────────── EvidenceRecorder ───────────────────────────

class EvidenceRecorder:
    """Owns the three append-only JSONL evidence files for one AAW checkout.

    All state that matters across a restart is rebuilt from disk in
    `__init__` — nothing durable lives only in memory. The one thing that is
    *not* restored across a restart is "accepted, not yet run" — matching
    `aaw_bridge.py`'s own stance on proposals: BUILD-time authoring state,
    not evidence, until it resolves into something durable (a run, or a
    reject).
    """

    def __init__(self, evidence_root: Path | None = None) -> None:
        self.root = Path(evidence_root) if evidence_root else aaw_paths.PLANNER_EVIDENCE_ROOT
        self._lock = threading.RLock()
        self._proposal_session: dict[str, str] = {}
        self._pending_accept: dict[str, dict[str, Any]] = {}
        # Dedup guard for one instance's own lifetime, keyed on `sequence` —
        # the bridge's own stable identity for one event: strictly monotonic
        # and unique for as long as this instance lives, guarded by
        # `AawBridge`'s own lock. Preferred over a content-derived key
        # because it *is* the substrate's identity for "this exact event",
        # not an approximation of it.
        #
        # Deliberately NOT seeded from any pre-existing file on disk. A
        # fresh instance's own `sequence` counter also starts at 0, so a low
        # sequence number already on disk (written by a *different*
        # instance — a prior process, or another bridge sharing this
        # evidence root) can never legitimately collide with anything THIS
        # instance is about to write: two rows that happen to share a
        # `sequence` value across different instances are two genuinely
        # different events, and both must be kept. Nothing on the read side
        # ever treats `sequence` as unique across the whole file — sessions
        # are joined by `request_id`/`proposal_id`, which stay distinct per
        # real event by construction (a fresh `uuid4` per planner ask, a
        # content hash per proposal) — so collapsing on `sequence` beyond
        # this instance's own lifetime would only risk dropping real
        # evidence for no correctness benefit.
        self._seen_decision_sequences: set[int] = set()
        for row in _read_jsonl(self.decisions_path):
            pid, rid = row.get("proposal_id"), row.get("request_id")
            if pid and rid:
                self._proposal_session[str(pid)] = str(rid)

    @property
    def decisions_path(self) -> Path:
        return self.root / DECISIONS_FILENAME

    @property
    def links_path(self) -> Path:
        return self.root / LINKS_FILENAME

    @property
    def feedback_path(self) -> Path:
        return self.root / FEEDBACK_FILENAME

    # -- planner decisions -------------------------------------------------
    def record_planner_events(self, rows: Sequence[Mapping[str, Any]]) -> None:
        """Durabilize a batch of journal rows exactly as the bridge produced them.

        Idempotent within this instance's own lifetime: a `sequence` this
        instance has already written (possible when two calls' drain
        windows overlap) is skipped rather than appended a second time. A
        row with no usable `sequence` is always appended — there is no
        identity to dedup it against, so refusing to drop it is the
        fail-safe choice.
        """
        with self._lock:
            for row in rows:
                sequence = row.get("sequence")
                if isinstance(sequence, int):
                    if sequence in self._seen_decision_sequences:
                        continue
                    self._seen_decision_sequences.add(sequence)
                _append_jsonl(self.decisions_path, row)
                pid, rid = row.get("proposal_id"), row.get("request_id")
                if pid and rid:
                    self._proposal_session[str(pid)] = str(rid)

    def note_accept(self, *, proposal_id: str, workflow_id: str, semantic_hash: str) -> None:
        session_id = self._proposal_session.get(str(proposal_id))
        if session_id is None:
            return  # a proposal accepted without ever having gone through this
                    # recorder's plan_from_node (e.g. a differently-constructed
                    # bridge) has no session to attach to; nothing to link.
        with self._lock:
            self._pending_accept[str(workflow_id)] = {
                "session_id": session_id, "proposal_id": str(proposal_id),
                "semantic_hash_after_accept": semantic_hash,
            }

    def note_reject(self, *, workflow_id: str) -> None:
        with self._lock:
            self._pending_accept.pop(str(workflow_id), None)

    def link_run(self, *, workflow_id: str, run_id: str, base_semantic_hash: str | None) -> None:
        with self._lock:
            pending = self._pending_accept.pop(str(workflow_id), None)
        if pending is not None:
            manual_edit = (base_semantic_hash is not None
                           and base_semantic_hash != pending["semantic_hash_after_accept"])
            row = {"session_id": pending["session_id"], "proposal_id": pending["proposal_id"],
                   "workflow_id": str(workflow_id), "run_id": str(run_id),
                   "linked_at": _now(), "post_accept_manual_edit": manual_edit}
        else:
            row = {"session_id": str(run_id), "proposal_id": None,
                   "workflow_id": str(workflow_id), "run_id": str(run_id),
                   "linked_at": _now(), "post_accept_manual_edit": None}
        _append_jsonl(self.links_path, row)

    # -- operator feedback ---------------------------------------------------
    def record_feedback(self, *, session_id: str, usefulness: str,
                        reuse_intent: str | None = None,
                        comment: str | None = None) -> dict[str, Any]:
        if usefulness not in USEFULNESS_VALUES:
            raise ValueError(f"usefulness must be one of {USEFULNESS_VALUES}, got {usefulness!r}")
        if reuse_intent is not None and reuse_intent not in REUSE_INTENT_VALUES:
            raise ValueError(f"reuse_intent must be one of {REUSE_INTENT_VALUES}, got {reuse_intent!r}")
        prior = _latest_feedback_row(_read_jsonl(self.feedback_path), session_id)
        row = {"feedback_id": "FDBK-" + uuid.uuid4().hex[:12], "session_id": str(session_id),
               "usefulness": usefulness, "reuse_intent": reuse_intent,
               "comment": str(comment) if comment else None,
               "recorded_at": _now(), "supersedes": prior["feedback_id"] if prior else None}
        _append_jsonl(self.feedback_path, row)
        return row


def _latest_feedback_row(rows: Sequence[Mapping[str, Any]], session_id: str) -> dict[str, Any] | None:
    latest = None
    for row in rows:
        if str(row.get("session_id")) == str(session_id):
            latest = row  # append-only in write order: the last match is current
    return dict(latest) if latest is not None else None


def _proposal_session_map(decision_rows: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for row in decision_rows:
        pid, rid = row.get("proposal_id"), row.get("request_id")
        if pid and rid:
            mapping[str(pid)] = str(rid)
    return mapping


# ─────────────────────────── pure projections ───────────────────────────

def _fold_planner_sessions(decision_rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Group durable planner-decision rows into one record per `request_id`."""
    sessions: dict[str, dict[str, Any]] = {}
    for row in decision_rows:
        request_id = row.get("request_id")
        if not request_id:
            continue
        session = sessions.setdefault(str(request_id), {
            "session_id": str(request_id), "workflow_id": row.get("workflow_id"),
            "anchor_node_id": row.get("anchor_node_id"), "instruction_chars": None,
            "provider": None, "model": None, "effort": None, "planner_latency_s": None,
            "proposal_id": None, "proposal_hash": None, "node_count": None,
            "edge_count": None, "proposal_status": None, "refusal_code": None,
            "operator_decision": None, "resolved_at": None,
            "created_at": row.get("at"),
        })
        event_type = row.get("event_type")
        if event_type == pp.PLANNER_STARTED:
            session["instruction_chars"] = row.get("instruction_chars")
        elif event_type == pp.PLANNER_COMPLETED:
            planner = row.get("planner") or {}
            session["provider"] = planner.get("provider")
            session["model"] = planner.get("model")
            session["effort"] = planner.get("effort")
            session["planner_latency_s"] = planner.get("wall_time_s")
        elif event_type == pp.PLANNER_FAILED:
            session["proposal_status"] = pp.PLANNER_FAILED
            session["refusal_code"] = row.get("code")
        elif event_type in _PLAN_PHASE_STATUS_EVENTS:
            session["proposal_status"] = event_type
            session["proposal_id"] = row.get("proposal_id")
            session["proposal_hash"] = row.get("proposal_hash")
            session["node_count"] = row.get("node_count")
            session["edge_count"] = row.get("edge_count")
            diagnostics = row.get("diagnostics") or []
            if diagnostics and not session["refusal_code"]:
                session["refusal_code"] = diagnostics[0].get("code")
    return sessions


def _fold_operator_decisions(decision_rows: Sequence[Mapping[str, Any]],
                             proposal_session: Mapping[str, str],
                             sessions: dict[str, dict[str, Any]]) -> None:
    for row in decision_rows:
        event_type = row.get("event_type")
        if event_type not in (pp.PROPOSAL_ACCEPTED, pp.PROPOSAL_REJECTED):
            continue
        proposal_id = row.get("proposal_id")
        session_id = proposal_session.get(str(proposal_id)) if proposal_id else None
        session = sessions.get(session_id) if session_id else None
        if session is None:
            continue
        session["operator_decision"] = "ACCEPT" if event_type == pp.PROPOSAL_ACCEPTED else "REJECT"
        session["resolved_at"] = row.get("at")


def _read_workflow_state(stats_root: Path, run_id: str) -> dict[str, Any] | None:
    path = Path(stats_root) / str(run_id) / "WORKFLOW" / "workflow_state.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _human_gates_used(stats_root: Path, run_id: str, state: Mapping[str, Any] | None) -> int | None:
    try:
        ledger = execution_ledger.ExecutionLedger.for_run(str(run_id), stats_root)
        count = sum(1 for event in ledger.events()
                    if event.get("event_type") == "HUMAN_DECISION_RECORDED")
    except Exception:
        count = 0
    if count == 0 and state is not None and state.get("status") == "WAITING_FOR_HUMAN":
        return 1  # gate reached, not yet resolved -- still real Human Gate use
    return count


def _execution_projection(stats_root: Path, run_id: str | None) -> dict[str, Any]:
    if run_id is None:
        return {"run_id": None, "run_status": None, "final_outcome": None,
                "node_count": None, "repair_cycles": None, "human_gates": None,
                "wall_time_total": None}
    state = _read_workflow_state(stats_root, run_id)
    summary = (state or {}).get("workflow_summary") or {}
    return {
        "run_id": str(run_id),
        "run_status": (state or {}).get("status"),
        "final_outcome": (state or {}).get("final_outcome"),
        "node_count": summary.get("nodes_completed"),
        "repair_cycles": summary.get("repair_cycles"),
        "human_gates": _human_gates_used(stats_root, run_id, state),
        "wall_time_total": summary.get("wall_time_total"),
    }


def _resources_projection(stats_root: Path, run_id: str | None) -> dict[str, Any]:
    if run_id is None:
        return {"input_tokens": None, "output_tokens": None, "known_cost": None}
    state = _read_workflow_state(stats_root, run_id)
    summary = (state or {}).get("workflow_summary") or {}
    return {"input_tokens": summary.get("input_tokens_total"),
            "output_tokens": summary.get("output_tokens_total"),
            "known_cost": None}


def all_sessions(*, evidence_root: Path | None = None,
                 stats_root: Path | None = None) -> list[dict[str, Any]]:
    """Every reconstructable operator session, newest last. Pure and rebuildable:
    the only inputs are the three JSONL files and existing `03_STATS` evidence.
    """
    evidence_root = Path(evidence_root) if evidence_root else aaw_paths.PLANNER_EVIDENCE_ROOT
    stats_root = Path(stats_root) if stats_root else aaw_paths.STATS_ROOT

    decision_rows = _read_jsonl(evidence_root / DECISIONS_FILENAME)
    link_rows = _read_jsonl(evidence_root / LINKS_FILENAME)
    feedback_rows = _read_jsonl(evidence_root / FEEDBACK_FILENAME)

    sessions = _fold_planner_sessions(decision_rows)
    for session in sessions.values():
        session["_has_planner_activity"] = True
    proposal_session = _proposal_session_map(decision_rows)
    _fold_operator_decisions(decision_rows, proposal_session, sessions)

    links_by_session: dict[str, dict[str, Any]] = {}
    for row in link_rows:
        links_by_session[str(row["session_id"])] = row  # last-linked wins

    for row in link_rows:
        session_id = str(row["session_id"])
        if session_id not in sessions:
            sessions[session_id] = {
                "session_id": session_id, "workflow_id": row.get("workflow_id"),
                "anchor_node_id": None, "instruction_chars": None, "provider": None,
                "model": None, "effort": None, "planner_latency_s": None,
                "proposal_id": None, "proposal_hash": None, "node_count": None,
                "edge_count": None, "proposal_status": None, "refusal_code": None,
                "operator_decision": None, "resolved_at": None,
                "created_at": row.get("linked_at"), "_has_planner_activity": False,
            }

    results: list[dict[str, Any]] = []
    for session_id, session in sessions.items():
        link = links_by_session.get(session_id)
        run_id = link.get("run_id") if link else None
        feedback = _latest_feedback_row(feedback_rows, session_id)
        planner_block = None
        if session.get("_has_planner_activity"):
            planner_block = {
                "workflow_id": session.get("workflow_id"),
                "anchor_node_id": session.get("anchor_node_id"),
                "provider": session.get("provider"), "model": session.get("model"),
                "effort": session.get("effort"),
                "planner_latency_s": session.get("planner_latency_s"),
                "proposal_id": session.get("proposal_id"),
                "proposal_hash": session.get("proposal_hash"),
                "proposal_status": session.get("proposal_status"),
                "node_count": session.get("node_count"), "edge_count": session.get("edge_count"),
                "refusal_code": session.get("refusal_code"),
                "operator_decision": session.get("operator_decision"),
            }
        results.append({
            "schema_version": SCHEMA_VERSION,
            "session_id": session_id,
            "planner": planner_block,
            "execution": _execution_projection(stats_root, run_id),
            "resources": _resources_projection(stats_root, run_id),
            "operator_feedback": ({"usefulness": feedback["usefulness"],
                                   "reuse_intent": feedback.get("reuse_intent"),
                                   "comment": feedback.get("comment"),
                                   "recorded_at": feedback.get("recorded_at")}
                                  if feedback else None),
            "post_accept_manual_edit": link.get("post_accept_manual_edit") if link else None,
            "created_at": session.get("created_at"),
        })
    results.sort(key=lambda row: row.get("created_at") or "")
    return results


def session_summary(session_id: str, *, evidence_root: Path | None = None,
                    stats_root: Path | None = None) -> dict[str, Any] | None:
    for session in all_sessions(evidence_root=evidence_root, stats_root=stats_root):
        if session["session_id"] == str(session_id):
            return session
    return None


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _avg(values: Sequence[float]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


def aggregate_report(*, evidence_root: Path | None = None,
                     stats_root: Path | None = None) -> dict[str, Any]:
    """Read-only fold over `all_sessions()`. No AI interpretation, no scoring."""
    sessions = all_sessions(evidence_root=evidence_root, stats_root=stats_root)
    planned = [s for s in sessions if s["planner"] is not None]
    decided = [s for s in planned if s["planner"]["operator_decision"] is not None]
    accepted = [s for s in decided if s["planner"]["operator_decision"] == "ACCEPT"]
    rejected = [s for s in decided if s["planner"]["operator_decision"] == "REJECT"]
    with_run = [s for s in sessions if s["execution"]["run_id"] is not None]
    completed = [s for s in with_run if s["execution"]["final_outcome"] == "COMPLETED"]
    repair_cycles = [s["execution"]["repair_cycles"] for s in with_run
                     if s["execution"]["repair_cycles"] is not None]
    manual_edits = [s for s in with_run if s.get("post_accept_manual_edit")]
    human_gate_runs = [s for s in with_run if (s["execution"]["human_gates"] or 0) > 0]
    input_tokens = [s["resources"]["input_tokens"] for s in sessions
                    if s["resources"]["input_tokens"] is not None]
    output_tokens = [s["resources"]["output_tokens"] for s in sessions
                     if s["resources"]["output_tokens"] is not None]
    feedback_counts = {value: 0 for value in USEFULNESS_VALUES}
    for session in sessions:
        if session["operator_feedback"]:
            feedback_counts[session["operator_feedback"]["usefulness"]] += 1

    return {
        "schema_version": SCHEMA_VERSION,
        "session_count": len(sessions),
        "planner_sessions": len(planned),
        "planner_decided_sessions": len(decided),
        "planner_accept_rate": _rate(len(accepted), len(decided)),
        "planner_reject_rate": _rate(len(rejected), len(decided)),
        "runs": len(with_run),
        "completed_run_rate": _rate(len(completed), len(with_run)),
        "avg_repair_cycles": _avg(repair_cycles),
        "manual_edit_frequency": _rate(len(manual_edits), len(with_run)),
        "human_gate_frequency": _rate(len(human_gate_runs), len(with_run)),
        "avg_input_tokens": _avg(input_tokens),
        "avg_output_tokens": _avg(output_tokens),
        "operator_feedback_distribution": feedback_counts,
        "feedback_count": sum(feedback_counts.values()),
    }


# ─────────────────────────── EvidenceBridge ───────────────────────────

class EvidenceBridge(aaw_bridge.AawBridge):
    """`AawBridge`, plus durable production-use evidence capture.

    Overrides exactly four methods. Each is a pure observer: call the real
    bridge method first, durabilize what it already reported, and return the
    original result completely unchanged. No planner or runtime behavior can
    change by constructing this class instead of `AawBridge`.
    """

    def __init__(self, *, evidence_root: Path | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.evidence = EvidenceRecorder(evidence_root)

    def _drain_planner_journal(self, since: int) -> None:
        batch = self.planner_events(since=since)
        if batch["events"]:
            self.evidence.record_planner_events(batch["events"])

    def _journal_cursor(self) -> int:
        return int(self.planner_events(since=0, limit=0)["journal_last_sequence"])

    def plan_from_node(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        since = self._journal_cursor()
        try:
            return super().plan_from_node(*args, **kwargs)
        finally:
            self._drain_planner_journal(since)

    def accept_proposal(self, proposal_id: str, **kwargs: Any) -> dict[str, Any]:
        since = self._journal_cursor()
        try:
            result = super().accept_proposal(proposal_id, **kwargs)
        finally:
            self._drain_planner_journal(since)
        self.evidence.note_accept(proposal_id=proposal_id, workflow_id=result["workflow_id"],
                                  semantic_hash=result["semantic_hash"])
        return result

    def reject_proposal(self, proposal_id: str, **kwargs: Any) -> dict[str, Any]:
        since = self._journal_cursor()
        try:
            result = super().reject_proposal(proposal_id, **kwargs)
        finally:
            self._drain_planner_journal(since)
        self.evidence.note_reject(workflow_id=result["workflow_id"])
        return result

    def start_run(self, workflow_id: str, **kwargs: Any) -> dict[str, Any]:
        result = super().start_run(workflow_id, **kwargs)
        base_hash = None
        try:
            base_hash = self._planning_base(str(workflow_id), None)[1]
        except Exception:
            pass  # evidence capture must never fail a run that already started
        self.evidence.link_run(workflow_id=workflow_id, run_id=result["run_id"],
                               base_semantic_hash=base_hash)
        return result

    # -- evidence-only reads/writes; not overrides of anything on AawBridge --
    def record_operator_feedback(self, session_id: str, usefulness: str, *,
                                 reuse_intent: str | None = None,
                                 comment: str | None = None) -> dict[str, Any]:
        return self.evidence.record_feedback(session_id=session_id, usefulness=usefulness,
                                             reuse_intent=reuse_intent, comment=comment)

    def all_sessions(self) -> list[dict[str, Any]]:
        return all_sessions(evidence_root=self.evidence.root, stats_root=self.stats_root)

    def session_summary(self, session_id: str) -> dict[str, Any] | None:
        return session_summary(session_id, evidence_root=self.evidence.root,
                               stats_root=self.stats_root)

    def evidence_report(self) -> dict[str, Any]:
        return aggregate_report(evidence_root=self.evidence.root, stats_root=self.stats_root)


# ─────────────────────────── CLI ───────────────────────────

def _print_report(report: Mapping[str, Any]) -> None:
    width = max(len(key) for key in report if key not in ("operator_feedback_distribution",))
    for key, value in report.items():
        if key == "operator_feedback_distribution":
            continue
        print(f"  {key:<{width}}  {value}")
    print("  operator_feedback_distribution")
    for value, count in report["operator_feedback_distribution"].items():
        print(f"    {value:<12}  {count}")


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--evidence-root", type=Path, default=None)
    ap.add_argument("--stats-root", type=Path, default=None)
    ap.add_argument("--report", action="store_true", help="print the aggregate product-use report")
    ap.add_argument("--sessions", action="store_true", help="list every reconstructable session")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = ap.parse_args(argv)

    if args.sessions:
        rows = all_sessions(evidence_root=args.evidence_root, stats_root=args.stats_root)
        if args.json:
            print(json.dumps(rows, indent=2, ensure_ascii=False, default=str))
        else:
            for row in rows:
                print(f"{row['session_id']}  planner={row['planner'] is not None}  "
                      f"run={row['execution']['run_id']}  "
                      f"status={row['execution']['run_status']}  "
                      f"feedback={(row['operator_feedback'] or {}).get('usefulness')}")
        return 0

    report = aggregate_report(evidence_root=args.evidence_root, stats_root=args.stats_root)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        _print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
