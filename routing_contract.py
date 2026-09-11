#!/usr/bin/env python3
"""AAW MULTIROUTING RUNTIME CONTRACT V0.1 — deterministic edge/gate layer.

Frozen scope. This module answers exactly one question:

    given one validated structured node result, which outgoing edges of that
    node are selected?

`evaluate_gate` is a pure function of (node, result). It does not execute
nodes, does not talk to providers, does not touch the worktree and does not
own workflow state. The reviewer/model never steers the graph: it produces a
structured result, and this layer decides the route.

Deliberately NOT in V0.1: expression DSL, merge/rejoin semantics, planner
mutation, retry loops. Bounded repair stays a runner guardrail
(`limits.max_repair_cycles`), it is not re-implemented here.

Layering:

    node execution -> structured result
                   -> evaluate_gate()            (pure, this module)
                   -> GateDecision
                   -> runner frontier + lineage  (workflow_runner)
                   -> routing journal            (this module, append-only)
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import os
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


CONTRACT_VERSION = "AAW_MULTIROUTING_RUNTIME_CONTRACT_V0.1"
JOURNAL_SCHEMA_VERSION = "AAW_ROUTING_JOURNAL_V0.1"
GATE_DECISION_SCHEMA_VERSION = "AAW_GATE_DECISION_V0.1"

VERDICTS = ("PASS", "REPAIR", "BLOCKED")
EDGE_KINDS = ("CONTINUE", "REPAIR", "FALLBACK")
ROUTING_MODES = ("FIRST_MATCH", "ALL_MATCHES")
PREDICATE_KEYS = ("verdict", "outcome", "has_findings", "min_severity")
SEVERITY_LADDER = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
TERMINALS = ("STOP", "HUMAN_REQUIRED")
NO_ROUTE = "NO_ROUTE"

# An INVALID execution never yields a verdict: it is fail-closed upstream of
# the gate. FAIL maps to REPAIR because in AAW a failed-but-valid result is a
# repairable one; a reviewer that means "stop" says so with an explicit verdict.
DEFAULT_VERDICT_FOR_OUTCOME = {"PASS": "PASS", "FAIL": "REPAIR", "BLOCKED": "BLOCKED"}

# Routing journal event vocabulary. The execution ledger already owns the
# process lifecycle (EXECUTION_INTENT/STARTED/CLOSED) and explicitly refuses to
# hold semantic workflow state, so graph facts live here instead of being
# forced into it. HUMAN_DECISION_REQUIRED pairs with the ledger's existing
# HUMAN_DECISION_RECORDED.
NODE_STARTED = "NODE_STARTED"
NODE_COMPLETED = "NODE_COMPLETED"
NODE_FAILED = "NODE_FAILED"
GATE_EVALUATED = "GATE_EVALUATED"
EDGE_SELECTED = "EDGE_SELECTED"
EDGE_HELD = "EDGE_HELD"
BRANCH_CREATED = "BRANCH_CREATED"
HUMAN_DECISION_REQUIRED = "HUMAN_DECISION_REQUIRED"
# V0.1.1 additive counterpart. HUMAN_DECISION_REQUIRED opens a gate; without
# this a stream consumer never sees it close. The ledger's
# HUMAN_DECISION_RECORDED remains the lifecycle authority.
HUMAN_DECISION_RESOLVED = "HUMAN_DECISION_RESOLVED"
ROUTE_UNRESOLVED = "ROUTE_UNRESOLVED"
# V0.1.1 additive event. Cancellation abandons a frontier, which is a graph
# fact, and it is the one runtime outcome that otherwise leaves no structured
# trace at all. Process-level cancellation facts stay in the execution
# ledger's close taxonomy; this records only what cancelling did to the graph.
RUN_CANCELLED = "RUN_CANCELLED"

# AAW CANVAS FUNCTIONALIZATION V0.1. A run that begins from a seeded frontier
# rather than from `start_node` is a graph fact, and it was the one runtime
# entry with no structured trace — the same justification that added
# RUN_CANCELLED. Its payload carries the inherited records, so a canvas
# watching only this run's stream can paint the upstream it did not execute
# without reading another run's journal.
RUN_RESUMED = "RUN_RESUMED"

EVENT_TYPES = (
    NODE_STARTED, NODE_COMPLETED, NODE_FAILED,
    GATE_EVALUATED, EDGE_SELECTED, EDGE_HELD,
    BRANCH_CREATED, HUMAN_DECISION_REQUIRED, HUMAN_DECISION_RESOLVED,
    ROUTE_UNRESOLVED, RUN_CANCELLED, RUN_RESUMED,
)

# Why an edge was not selected. Closed set: the canvas renders these directly.
HELD_NOT_MATCHED = "PREDICATE_NOT_MATCHED"
HELD_NOT_FIRST = "NOT_FIRST_MATCH"
HELD_BLOCKED_CONTINUE = "BLOCKED_CANNOT_CONTINUE"
HOLD_REASONS = (HELD_NOT_MATCHED, HELD_NOT_FIRST, HELD_BLOCKED_CONTINUE)

BRANCH_SUFFIXES = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

# V0.1.1. How a node came to exist. A declared node is in the workflow file; a
# minted one was created by a selected REPAIR edge at runtime. The UX draws
# them differently and must not have to guess which is which.
DECLARED_NODE = "DECLARED"
MINTED_REPAIR_BRANCH = "MINTED_REPAIR_BRANCH"
NODE_KINDS = (DECLARED_NODE, MINTED_REPAIR_BRANCH)


class RoutingContractError(ValueError):
    """Fail-closed routing-contract violation."""

    classification = "ROUTING_CONTRACT_ERROR"


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RoutingContractError(message)


def now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="milliseconds")


def canonical_hash(value: Any) -> str:
    import hashlib

    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ───────────────────────────── result projection ─────────────────────────────

def declares_edges(node: Mapping[str, Any]) -> bool:
    """True when the node opts into the multirouting contract."""
    return isinstance(node.get("edges"), list)


def derive_verdict(result: Mapping[str, Any]) -> str | None:
    """Semantic verdict the gate routes on.

    `outcome` stays the execution-grade fact (did the invocation produce a
    usable result). `verdict` is the work-grade fact. A node may state it
    explicitly; otherwise it is derived deterministically. INVALID has no
    verdict by design — it is fail-closed before routing.
    """
    explicit = result.get("verdict")
    if explicit is not None:
        _require(explicit in VERDICTS, f"unsupported verdict {explicit!r}")
        return str(explicit)
    return DEFAULT_VERDICT_FOR_OUTCOME.get(str(result.get("outcome")))


def severity_rank(value: Any) -> tuple[int, bool]:
    """Rank one finding severity. Unknown severities escalate, never dampen."""
    text = str(value or "").strip().upper()
    if text in SEVERITY_LADDER:
        return SEVERITY_LADDER.index(text), True
    return len(SEVERITY_LADDER) - 1, False


def project_result(result: Mapping[str, Any]) -> dict[str, Any]:
    """The exact, minimal slice of a node result the predicates may read.

    Everything routing depends on is in here, so an identical projection
    provably produces an identical decision.
    """
    findings = result.get("findings") or []
    ranks = [severity_rank(item.get("severity")) for item in findings if isinstance(item, Mapping)]
    return {
        "outcome": str(result.get("outcome")) if result.get("outcome") is not None else None,
        "verdict": derive_verdict(result),
        "has_findings": bool(findings),
        "max_severity_rank": max((rank for rank, _ in ranks), default=-1),
        "unknown_severities": sorted({
            str(item.get("severity"))
            for item, (_, known) in zip([f for f in findings if isinstance(f, Mapping)], ranks)
            if not known
        }),
    }


# ───────────────────────────── predicates ─────────────────────────────

def _evaluate_predicate(key: str, expected: Any, projection: Mapping[str, Any]) -> tuple[bool, dict[str, Any]]:
    if key == "verdict":
        actual = projection["verdict"]
        return actual == expected, {"predicate": key, "expected": expected, "actual": actual}
    if key == "outcome":
        actual = projection["outcome"]
        return actual == expected, {"predicate": key, "expected": expected, "actual": actual}
    if key == "has_findings":
        actual = projection["has_findings"]
        return actual is bool(expected), {"predicate": key, "expected": bool(expected), "actual": actual}
    if key == "min_severity":
        threshold = SEVERITY_LADDER.index(str(expected).upper())
        actual = projection["max_severity_rank"]
        return actual >= threshold, {
            "predicate": key, "expected": str(expected).upper(),
            "actual": SEVERITY_LADDER[actual] if actual >= 0 else None,
        }
    raise RoutingContractError(f"unsupported predicate {key!r}")


def match_condition(when: Any, projection: Mapping[str, Any]) -> tuple[bool, list[dict[str, Any]]]:
    """Evaluate one `when` block. Keys are ANDed. `None`/`{}` is unconditional."""
    if when in (None, {}):
        return True, [{"predicate": "unconditional", "expected": None, "actual": None}]
    _require(isinstance(when, Mapping), "edge.when must be an object or null")
    trace: list[dict[str, Any]] = []
    matched = True
    for key in sorted(when):
        ok, row = _evaluate_predicate(key, when[key], projection)
        row["matched"] = ok
        trace.append(row)
        matched = matched and ok
    return matched, trace


# ───────────────────────────── edges ─────────────────────────────

def compile_edges(node: Mapping[str, Any]) -> tuple[list[dict[str, Any]], str]:
    """Return (edges, routing_mode) for a node.

    A node that declares `edges` uses them verbatim. A legacy node is compiled
    from `on_pass`/`on_fail` so one gate implementation serves both and no
    second engine appears.
    """
    if declares_edges(node):
        mode = str(node.get("routing") or "FIRST_MATCH")
        _require(mode in ROUTING_MODES, f"{node.get('id')}: unsupported routing mode {mode!r}")
        return [dict(edge) for edge in node["edges"]], mode
    edges: list[dict[str, Any]] = []
    if node.get("on_pass") is not None:
        edges.append({"edge_id": f"{node['id']}:LEGACY_PASS", "to": node["on_pass"],
                      "when": {"outcome": "PASS"}, "kind": "CONTINUE", "legacy": True})
    if node.get("on_fail") is not None:
        edges.append({"edge_id": f"{node['id']}:LEGACY_FAIL", "to": node["on_fail"],
                      "when": {"outcome": "FAIL"}, "kind": "CONTINUE", "legacy": True})
    return edges, "FIRST_MATCH"


def routing_input_hash(node: Mapping[str, Any], result: Mapping[str, Any]) -> str:
    """Hash of everything — and only what — the decision depends on."""
    edges, mode = compile_edges(node)
    projection = project_result(result)
    return canonical_hash({
        "contract": CONTRACT_VERSION,
        "routing": mode,
        "edges": [{"edge_id": e.get("edge_id"), "to": e.get("to"),
                   "kind": e.get("kind", "CONTINUE"), "when": e.get("when")} for e in edges],
        "projection": projection,
    })


# ───────────────────────────── the gate ─────────────────────────────

def evaluate_gate(node: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, Any]:
    """Deterministic routing decision for one settled node. Pure function.

    Selection rules, complete:
      * an edge is *matched* when its `when` block matches the result projection;
      * an edge is *eligible* unless the verdict is BLOCKED and the edge kind is
        CONTINUE — a blocked node never starts downstream work on its own;
      * FIRST_MATCH selects the first matched+eligible edge in declaration
        order, ALL_MATCHES selects every matched+eligible edge;
      * zero selected edges is NO_ROUTE: fail-closed, never a silent stop.
    """
    edges, mode = compile_edges(node)
    projection = project_result(result)
    verdict = projection["verdict"]

    candidates: list[dict[str, Any]] = []
    for position, edge in enumerate(edges):
        edge_id = str(edge.get("edge_id") or f"{node['id']}:#{position}")
        kind = str(edge.get("kind") or "CONTINUE")
        _require(kind in EDGE_KINDS, f"{edge_id}: unsupported edge kind {kind!r}")
        matched, trace = match_condition(edge.get("when"), projection)
        eligible = not (verdict == "BLOCKED" and kind == "CONTINUE")
        candidates.append({
            "edge_id": edge_id, "position": position, "to": edge.get("to"), "kind": kind,
            "when": edge.get("when"), "label": edge.get("label"),
            "matched": matched, "eligible": eligible, "trace": trace,
        })

    viable = [row for row in candidates if row["matched"] and row["eligible"]]
    selected = viable[:1] if mode == "FIRST_MATCH" else list(viable)
    selected_ids = {row["edge_id"] for row in selected}

    held: list[dict[str, Any]] = []
    for row in candidates:
        if row["edge_id"] in selected_ids:
            continue
        if not row["eligible"]:
            reason = HELD_BLOCKED_CONTINUE
        elif not row["matched"]:
            reason = HELD_NOT_MATCHED
        else:
            reason = HELD_NOT_FIRST
        held.append({**row, "hold_reason": reason})

    decision = {
        "schema_version": GATE_DECISION_SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "evaluated_at": now(),
        "node_id": node.get("id"),
        "node_type": node.get("type"),
        "routing_mode": mode,
        "outcome": projection["outcome"],
        "verdict": verdict,
        "projection": projection,
        "candidates": candidates,
        "selected": [{k: row[k] for k in ("edge_id", "to", "kind", "when", "label")} for row in selected],
        "held": [{**{k: row[k] for k in ("edge_id", "to", "kind", "when", "label")},
                  "hold_reason": row["hold_reason"]} for row in held],
        "no_route": not selected,
        "routing_input_hash": routing_input_hash(node, result),
    }
    decision["decision_hash"] = canonical_hash({
        "routing_input_hash": decision["routing_input_hash"],
        "selected": [row["edge_id"] for row in selected],
        "held": [(row["edge_id"], row["hold_reason"]) for row in held],
    })
    return decision


# ───────────────────────────── branch lineage ─────────────────────────────

def branch_suffix(index: int) -> str:
    """1 -> 'A', 26 -> 'Z', 27 -> 'AA'. Explicit, human-readable lineage ids."""
    _require(index >= 1, "branch index starts at 1")
    text = ""
    while index > 0:
        index, remainder = divmod(index - 1, len(BRANCH_SUFFIXES))
        text = BRANCH_SUFFIXES[remainder] + text
    return text


def next_branch_id(origin_node_id: str, known_ids: Iterable[str]) -> tuple[str, int]:
    taken = set(known_ids)
    index = 1
    while True:
        candidate = f"{origin_node_id}{branch_suffix(index)}"
        if candidate not in taken:
            return candidate, index
        index += 1


def mint_branch_node(template: Mapping[str, Any], *, origin_node: Mapping[str, Any],
                     origin_result: Mapping[str, Any], edge: Mapping[str, Any],
                     known_ids: Iterable[str]) -> dict[str, Any]:
    """Create one explicit lineage child. This is what replaces the retry loop.

    The template node is never entered itself. Each repair attempt becomes a
    distinct node id with its own artifacts, so the old path stays on the graph
    instead of being overwritten by a second pass over the same node.
    """
    _require(str(template.get("type")) == "REPAIR",
             f"REPAIR edge must target a REPAIR template, got {template.get('type')!r}")
    branch_id, index = next_branch_id(str(origin_node["id"]), known_ids)
    node = copy.deepcopy(dict(template))
    next_brief = origin_result.get("next_brief")
    carry = [str(item) for item in (origin_result.get("carry_forward") or [])]

    node["id"] = branch_id
    node["depends_on"] = [str(origin_node["id"])]
    node["run_if"] = "ON_TRANSITION"
    node["node_kind"] = MINTED_REPAIR_BRANCH
    node["lineage"] = {
        "template_id": str(template["id"]),
        "node_type": str(template.get("type")),
        "origin_node_id": str(origin_node["id"]),
        "origin_node_type": str(origin_node.get("type")),
        "origin_execution_id": origin_result.get("execution_id"),
        "origin_verdict": derive_verdict(origin_result),
        "selected_edge_id": edge.get("edge_id"),
        "branch_index": index,
        "created_at": now(),
        "inherited_brief": next_brief,
        "carry_forward": carry,
    }
    # V0.1.1. `carry_forward` is a *path* fact: a constraint inherited from
    # upstream that must survive downstream. `acceptance` is a *node* fact:
    # what this node must satisfy to be accepted. V0.1 appended the first onto
    # the second, which left every reader — runtime and UX alike — unable to
    # tell them apart except by matching a prose prefix, the one thing the rest
    # of this contract refuses to do. The template's acceptance is now
    # inherited verbatim and the constraints keep their own first-class field.
    node["carry_forward"] = carry
    inherited: list[str] = []
    if next_brief:
        inherited.append(
            f"INHERITED_BRIEF (authored by {origin_node['id']}, binding for this branch):"
            f"\n{next_brief}")
    if carry:
        bullets = "\n".join(f"- {item}" for item in carry)
        inherited.append(
            "CARRY_FORWARD (constraints inherited from the path that reached this branch. "
            "They bind the work; they are not this node's acceptance criteria):"
            f"\n{bullets}")
    if inherited:
        node["instructions"] = "\n\n".join(
            [str(template.get("instructions", "")).strip(), *inherited]).strip()
    return node


def branch_projection(node: Mapping[str, Any]) -> dict[str, Any]:
    """Declarative projection of a runtime-minted node, for state and UX.

    A minted branch lives only in the runner's in-memory node table, so
    without this a consumer reading `workflow_state.json` sees a bare id in
    the frontier and cannot render the node at all until it completes.
    Everything here is already-declared data; nothing is inferred.
    """
    edges, mode = compile_edges(node)
    return {
        "node_id": str(node["id"]),
        "node_type": str(node.get("type")),
        "node_kind": str(node.get("node_kind") or DECLARED_NODE),
        "role": node.get("role"),
        "depends_on": list(node.get("depends_on") or []),
        "run_if": node.get("run_if"),
        "instructions": node.get("instructions"),
        "acceptance": list(node.get("acceptance") or []),
        "carry_forward": list(node.get("carry_forward") or []),
        "lineage": node.get("lineage"),
        "routing": mode,
        "edges": [{"edge_id": edge.get("edge_id"), "to": edge.get("to"), "when": edge.get("when"),
                   "kind": edge.get("kind", "CONTINUE"), "label": edge.get("label")}
                  for edge in edges],
    }


def downstream_cone(workflow: Mapping[str, Any], node_id: str, *,
                    minted: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Everything a re-run of `node_id` would replace. Deterministic.

    AAW CANVAS FUNCTIONALIZATION V0.1 §2. `Run from here` and `Reset
    downstream` are the *same* graph question — "what does this node own?" —
    and both must answer it from the routing contract rather than from an
    ad-hoc frontend traversal.

    The cone is transitive reachability over the node's **compiled** edges,
    i.e. the same edge list `evaluate_gate` routes on, so a legacy
    `on_pass`/`on_fail` node and a contract node are treated identically and
    a predicate never has to be interpreted. `node_id` itself is in the cone:
    re-running a node replaces its own result too.

    Runtime-minted branches join the cone through their lineage: a branch is
    owned by the node that minted it, not by the template it was minted from.
    Terminals (`STOP`, `HUMAN_REQUIRED`) are not nodes and are reported
    separately.

    This says nothing about what may be *deleted*. It is a set of node ids;
    deciding what is safe to discard is §2's `reset_downstream`, which resets
    no durable artifact at all.
    """
    nodes = {str(node["id"]): node for node in (workflow.get("nodes") or [])}
    start = str(node_id)
    _require(start in nodes, f"unknown node {node_id!r}")

    cone: set[str] = set()
    terminals: set[str] = set()
    stack = [start]
    while stack:
        current = stack.pop()
        if current in cone:
            continue
        cone.add(current)
        edges, _mode = compile_edges(nodes[current])
        for edge in edges:
            target = str(edge.get("to") or "")
            if target in TERMINALS:
                terminals.add(target)
            elif target in nodes and target not in cone:
                stack.append(target)

    # A minted branch belongs to its origin. Repeated until stable so a branch
    # minted from a branch follows its whole chain.
    minted_rows = dict(minted or {})
    minted_in_cone: set[str] = set()
    changed = True
    while changed:
        changed = False
        for branch_id, row in minted_rows.items():
            if branch_id in minted_in_cone:
                continue
            origin = str(((row or {}).get("lineage") or {}).get("origin_node_id") or "")
            if origin in cone or origin in minted_in_cone:
                minted_in_cone.add(str(branch_id))
                changed = True

    return {
        "contract_version": CONTRACT_VERSION,
        "from_node": start,
        "declared": sorted(cone),
        "minted": sorted(minted_in_cone),
        "nodes": sorted(cone | minted_in_cone),
        "terminals": sorted(terminals),
    }


def upstream_of_cone(workflow: Mapping[str, Any], cone: Iterable[str]) -> list[str]:
    """The declared nodes a cone does not own — the seed set of a resumed run."""
    owned = set(str(item) for item in cone)
    return [str(node["id"]) for node in (workflow.get("nodes") or [])
            if str(node["id"]) not in owned]


def accumulate_carry_forward(results: Sequence[Mapping[str, Any]]) -> list[str]:
    """Order-preserving union of carry_forward across the executed path."""
    seen: list[str] = []
    for result in results:
        for item in result.get("carry_forward") or []:
            text = str(item)
            if text not in seen:
                seen.append(text)
    return seen


def collect_artifacts(results: Sequence[Mapping[str, Any]]) -> list[str]:
    seen: list[str] = []
    for result in results:
        for item in result.get("artifacts") or []:
            text = str(item)
            if text not in seen:
                seen.append(text)
    return seen


# ───────────────────────────── routing journal ─────────────────────────────

class RoutingJournal:
    """Append-only, ordered graph-semantics stream for the canvas UX.

    The authoritative record of a routing decision is the gate-decision
    artifact next to the node results; this file is the ordered index over
    them. It carries no process lifecycle facts — those stay in the execution
    ledger — so the two never disagree about who owns what.
    """

    def __init__(self, path: Path, *, run_id: str, workflow_id: str | None = None) -> None:
        self.path = Path(path)
        self.run_id = str(run_id)
        self.workflow_id = workflow_id
        self._sequence = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self._sequence = sum(1 for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip())

    @classmethod
    def for_run(cls, run_id: str, stats_root: Path, workflow_id: str | None = None) -> "RoutingJournal":
        path = Path(stats_root) / str(run_id) / "WORKFLOW" / "ROUTING" / "routing_events.jsonl"
        return cls(path, run_id=run_id, workflow_id=workflow_id)

    def append(self, event_type: str, *, node_id: str | None = None,
               payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        _require(event_type in EVENT_TYPES, f"unsupported routing event {event_type!r}")
        self._sequence += 1
        event = {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "contract_version": CONTRACT_VERSION,
            "event_id": "REV_" + uuid.uuid4().hex,
            "sequence": self._sequence,
            "event_type": event_type,
            "occurred_at": now(),
            "run_id": self.run_id,
            "workflow_id": self.workflow_id,
            "node_id": node_id,
            "payload": dict(payload or {}),
        }
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":"), default=str)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return event

    def read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return rows

    def by_type(self, event_type: str) -> list[dict[str, Any]]:
        return [row for row in self.read() if row.get("event_type") == event_type]


def record_decision(journal: RoutingJournal, decision: Mapping[str, Any]) -> None:
    """Emit the full evidence trail for one gate evaluation.

    GATE_EVALUATED carries the whole candidate set; EDGE_SELECTED and
    EDGE_HELD then name each path individually so a reader never has to infer
    the road not taken.
    """
    node_id = decision.get("node_id")
    journal.append(GATE_EVALUATED, node_id=node_id, payload={
        "routing_mode": decision["routing_mode"], "outcome": decision["outcome"],
        "verdict": decision["verdict"], "candidates": decision["candidates"],
        "no_route": decision["no_route"], "decision_hash": decision["decision_hash"],
        "routing_input_hash": decision["routing_input_hash"],
    })
    for row in decision["selected"]:
        journal.append(EDGE_SELECTED, node_id=node_id, payload=dict(row))
    for row in decision["held"]:
        journal.append(EDGE_HELD, node_id=node_id, payload=dict(row))
    if decision["no_route"]:
        journal.append(ROUTE_UNRESOLVED, node_id=node_id, payload={
            "status": NO_ROUTE, "verdict": decision["verdict"], "outcome": decision["outcome"],
            "candidates_considered": [row["edge_id"] for row in decision["candidates"]],
        })


# ───────────────────── static projection (BUILD mode) ─────────────────────

# Everything a workflow node may declare that the runtime actually reads. Any
# key outside this set is presentational or unknown and is excluded from the
# semantic projection, which is what makes the semantic hash layout-blind.
SEMANTIC_WORKFLOW_FIELDS = (
    "workflow_id", "version", "description", "goal", "start_node",
    "routing_contract", "workspace_policy", "limits",
)
SEMANTIC_NODE_FIELDS = (
    "id", "type", "depends_on", "run_if", "role", "capability", "model", "effort",
    "instructions", "acceptance", "on_pass", "on_fail", "routing", "edges",
    "command", "timeout_seconds", "preprocess",
)
SEMANTIC_EDGE_FIELDS = ("edge_id", "to", "when", "kind", "label", "legacy")

# Presentational keys the UX may attach to its own copy of a graph. They are
# named here so the write path can strip them deliberately rather than let
# them leak into a workflow file and silently change its identity.
VISUAL_FIELDS = ("x", "y", "layout", "ui", "collapsed", "viewport", "position", "_layout")


def semantic_workflow(workflow: Mapping[str, Any]) -> dict[str, Any]:
    """The execution-relevant slice of a workflow, in canonical form.

    Visual metadata is not merely ignored here, it is *absent*: the projection
    is a whitelist, so no coordinate, viewport or collapsed flag can reach the
    semantic hash however it was smuggled in.
    """
    projection: dict[str, Any] = {
        key: workflow.get(key) for key in SEMANTIC_WORKFLOW_FIELDS if key in workflow
    }
    nodes = []
    for node in workflow.get("nodes") or []:
        row = {key: node.get(key) for key in SEMANTIC_NODE_FIELDS if key in node}
        if isinstance(row.get("edges"), list):
            row["edges"] = [
                {key: edge.get(key) for key in SEMANTIC_EDGE_FIELDS if key in edge}
                for edge in row["edges"] if isinstance(edge, Mapping)
            ]
        nodes.append(row)
    projection["nodes"] = nodes
    return projection


def semantic_hash(workflow: Mapping[str, Any]) -> str:
    """Execution identity of a workflow definition.

    Reuses the project's canonical hashing so a BUILD write can be checked
    against the version the editor started from. Layout-blind by construction.
    """
    return canonical_hash({"contract": CONTRACT_VERSION, "workflow": semantic_workflow(workflow)})


def workflow_projection(workflow: Mapping[str, Any]) -> dict[str, Any]:
    """Static graph the canvas draws *before* a run exists (BUILD mode).

    Same shape of facts `graph_projection` reports at runtime — nodes, typed
    edges, predicates, kinds, labels — so BUILD and RUN share one spatial
    model instead of two independently drawn graphs. Pure; reads nothing but
    the definition it is handed.
    """
    nodes = list(workflow.get("nodes") or [])
    known = {str(node["id"]) for node in nodes}
    repair_templates = {
        str(edge.get("to"))
        for node in nodes
        for edge in (node.get("edges") or [])
        if str(edge.get("kind") or "CONTINUE") == "REPAIR" and edge.get("to")
    }
    projected_nodes: list[dict[str, Any]] = []
    projected_edges: list[dict[str, Any]] = []
    for node in nodes:
        node_id = str(node["id"])
        edges, mode = compile_edges(node)
        projected_nodes.append({
            "node_id": node_id,
            "node_type": str(node.get("type")),
            "node_kind": DECLARED_NODE,
            "role": node.get("role"),
            "depends_on": [str(item) for item in (node.get("depends_on") or [])],
            "run_if": node.get("run_if"),
            "instructions": node.get("instructions"),
            "acceptance": list(node.get("acceptance") or []),
            "model": node.get("model"),
            "capability": node.get("capability"),
            "effort": node.get("effort"),
            "command": node.get("command"),
            "routing": mode,
            # A REPAIR template is on the graph but is never entered: the gate
            # mints a lineage child from it. The canvas must render it as a
            # template, not as a queued task.
            "is_repair_template": node_id in repair_templates,
            # True only when the node actually routes on the pre-contract
            # fields. A terminal gate that declares neither is not "legacy".
            "legacy_transitions": bool(not declares_edges(node)
                                       and (node.get("on_pass") or node.get("on_fail"))),
        })
        for position, edge in enumerate(edges):
            projected_edges.append({
                "edge_id": str(edge.get("edge_id") or f"{node_id}:#{position}"),
                "from": node_id,
                "to": edge.get("to"),
                "kind": str(edge.get("kind") or "CONTINUE"),
                "when": edge.get("when"),
                "label": edge.get("label"),
                "position": position,
                "routing": mode,
                "legacy": bool(edge.get("legacy")),
                "terminal": str(edge.get("to")) if str(edge.get("to")) in TERMINALS else None,
                "resolves": str(edge.get("to")) if str(edge.get("to")) in known else None,
            })
    return {
        "contract_version": CONTRACT_VERSION,
        "workflow_id": workflow.get("workflow_id"),
        "version": workflow.get("version"),
        "description": workflow.get("description"),
        "start_node": str(workflow.get("start_node") or (projected_nodes[0]["node_id"] if projected_nodes else "")),
        "limits": workflow.get("limits"),
        "semantic_hash": semantic_hash(workflow),
        "nodes": projected_nodes,
        "edges": projected_edges,
        "terminals": list(TERMINALS),
    }


def graph_projection(state: Mapping[str, Any], journal: RoutingJournal) -> dict[str, Any]:
    """Everything the canvas needs about one run, without parsing prose.

    Deliberately assembled from structured state plus the journal so the
    frontend never reconstructs routing semantics from text logs.
    """
    events = journal.read()
    routing = dict(state.get("routing") or {})
    return {
        "contract_version": CONTRACT_VERSION,
        "run_id": state.get("AAW_RUN_ID"),
        "workflow_id": state.get("workflow_id"),
        "status": state.get("status"),
        "current_node": state.get("current_node"),
        "frontier": list(state.get("frontier") or []),
        "nodes": [
            {
                "node_id": row.get("node_id"), "node_type": row.get("node_type"),
                "outcome": row.get("outcome"), "verdict": row.get("verdict"),
                "execution_id": row.get("execution_id"), "artifact": row.get("artifact"),
                "lineage": row.get("lineage"),
            }
            for row in state.get("completed_nodes", [])
        ],
        # V0.1.1: nodes that did not exist when the run started. Declarative,
        # so a repair branch is drawable the moment it is minted rather than
        # only once it completes.
        "minted_nodes": list((routing.get("minted_nodes") or {}).values()),
        "routing": routing,
        "events": events,
        "last_sequence": max((int(row.get("sequence") or 0) for row in events), default=0),
    }
