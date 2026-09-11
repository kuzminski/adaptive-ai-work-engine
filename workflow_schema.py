#!/usr/bin/env python3
"""Strict, dependency-free schema validation for AAW static workflows V0.2."""

from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterator, Mapping


NODE_TYPES = {"IMPLEMENT", "REVIEW", "MACHINE_GATE", "REPAIR", "HUMAN_GATE", "FINAL_GATE"}
OUTCOMES = {"PASS", "FAIL", "BLOCKED", "INVALID"}
LLM_NODE_TYPES = {"IMPLEMENT", "REVIEW", "REPAIR"}
TERMINALS = {"STOP", "HUMAN_REQUIRED"}
REQUIRED_LIMITS = {"max_nodes", "max_repair_cycles", "max_wall_time_minutes", "max_llm_calls"}

# AAW MULTIROUTING RUNTIME CONTRACT V0.1 — declarative edges. A node either
# declares `edges` (contract mode) or keeps `on_pass`/`on_fail` (legacy mode);
# the gate compiles both into one representation, so there is one router.
ROUTING_CONTRACT_ID = "AAW_MULTIROUTING_RUNTIME_CONTRACT_V0.1"
ROUTING_MODES = {"FIRST_MATCH", "ALL_MATCHES"}
EDGE_KINDS = {"CONTINUE", "REPAIR", "FALLBACK"}
PREDICATE_KEYS = {"verdict", "outcome", "has_findings", "min_severity"}
VERDICTS = {"PASS", "REPAIR", "BLOCKED"}
SEVERITY_LADDER = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
EDGE_FIELDS = {"edge_id", "to", "when", "kind", "label", "legacy"}


# AAW CANVAS FUNCTIONALIZATION V0.1 — error attribution.
#
# A canvas must be able to put a validation error on the node or edge that
# caused it. The one technique the rest of this project refuses everywhere is
# recovering that from the message text, so the subject is carried out of the
# validator itself: the node loop and the edge loop declare what they are
# validating, and every `_require` raised inside picks it up. Messages, rules
# and the exception type are unchanged, so every existing caller and every
# existing assertion over `str(exc)` still holds.
_SUBJECT: ContextVar[tuple[str | None, str | None]] = ContextVar(
    "aaw_validation_subject", default=(None, None))


@contextmanager
def _subject_scope() -> Iterator[None]:
    """Contain every attribution set inside one validation pass.

    `validate_workflow` runs under this, so the plain `_SUBJECT.set` calls in
    its loops need no reindenting and still cannot leak into the caller.
    """
    handle = _SUBJECT.set((None, None))
    try:
        yield
    finally:
        _SUBJECT.reset(handle)


@contextmanager
def _subject(*, node_id: str | None = None, edge_id: str | None = None) -> Iterator[None]:
    current_node, current_edge = _SUBJECT.get()
    handle = _SUBJECT.set((node_id if node_id is not None else current_node,
                           edge_id if edge_id is not None else current_edge))
    try:
        yield
    finally:
        _SUBJECT.reset(handle)


class WorkflowValidationError(ValueError):
    """A workflow the schema refuses.

    `node_id` / `edge_id` name the subject being validated when the rule
    failed, or None for a workflow-level rule. They are attribution, never a
    substitute for the message.
    """

    def __init__(self, message: str, *, node_id: str | None = None,
                 edge_id: str | None = None) -> None:
        super().__init__(message)
        if node_id is None and edge_id is None:
            node_id, edge_id = _SUBJECT.get()
        self.node_id = node_id
        self.edge_id = edge_id

    def as_diagnostic(self) -> dict[str, Any]:
        return {"message": str(self), "node_id": self.node_id,
                "edge_id": self.edge_id, "source": "SCHEMA"}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise WorkflowValidationError(message)


def load_workflow(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise WorkflowValidationError(f"cannot read workflow: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise WorkflowValidationError(f"invalid workflow JSON: {exc}") from exc
    validate_workflow(data)
    return data


def validate_workflow(data: Any) -> dict[str, Any]:
    """Validate one workflow definition. Unchanged rules; attributed errors."""
    with _subject_scope():
        return _validate_workflow(data)


def _validate_workflow(data: Any) -> dict[str, Any]:
    _require(isinstance(data, dict), "workflow must be a JSON object")
    required = {"workflow_id", "version", "description", "goal", "workspace_policy", "limits", "nodes"}
    missing = required - set(data)
    _require(not missing, f"missing workflow fields: {sorted(missing)}")
    for key in ("workflow_id", "version", "description"):
        _require(isinstance(data[key], str) and data[key].strip(), f"{key} must be a non-empty string")
    _require(data["goal"] in (None, ""), "workflow goal must be supplied at runtime in V0.2")

    policy = data["workspace_policy"]
    _require(isinstance(policy, dict), "workspace_policy must be an object")
    _require(policy.get("isolated_worktree_required") is True, "isolated_worktree_required must be true")
    _require(policy.get("main_merge_allowed") is False, "main_merge_allowed must be false")

    limits = data["limits"]
    _require(isinstance(limits, dict), "limits must be an object")
    _require(not (REQUIRED_LIMITS - set(limits)), f"missing limits: {sorted(REQUIRED_LIMITS - set(limits))}")
    for key in REQUIRED_LIMITS:
        _require(type(limits[key]) is int and limits[key] > 0, f"limits.{key} must be a positive integer")
    if "max_token_budget" in limits:
        _require(limits["max_token_budget"] is None or (type(limits["max_token_budget"]) is int and limits["max_token_budget"] > 0), "limits.max_token_budget must be null or positive integer")

    nodes = data["nodes"]
    _require(isinstance(nodes, list) and nodes, "nodes must be a non-empty array")
    _require(len(nodes) <= limits["max_nodes"], "node count exceeds limits.max_nodes")
    ids = [node.get("id") for node in nodes if isinstance(node, dict)]
    _require(len(ids) == len(nodes) and all(isinstance(item, str) and item for item in ids), "every node needs a non-empty id")
    _require(len(ids) == len(set(ids)), "node ids must be unique")
    known = set(ids)
    types = {str(node["id"]): node.get("type") for node in nodes}

    for node in nodes:
        node_id = node["id"]
        _SUBJECT.set((str(node_id), None))  # every rule below is about this node
        required_node = {"id", "type", "depends_on", "run_if", "role", "effort", "instructions", "acceptance", "on_pass", "on_fail"}
        _require(not (required_node - set(node)), f"{node_id}: missing fields {sorted(required_node - set(node))}")
        node_type = node["type"]
        _require(node_type in NODE_TYPES, f"{node_id}: unknown node type {node_type!r}")
        _require(isinstance(node["depends_on"], list) and all(x in known for x in node["depends_on"]), f"{node_id}: invalid depends_on")
        _require(node["run_if"] in {"ALWAYS", "ON_TRANSITION"}, f"{node_id}: unsupported run_if")
        _require(isinstance(node["instructions"], str), f"{node_id}: instructions must be a string")
        _require(isinstance(node["acceptance"], list) and all(isinstance(x, str) for x in node["acceptance"]), f"{node_id}: acceptance must be an array of strings")
        for edge in ("on_pass", "on_fail"):
            _require(node[edge] in known | TERMINALS | {None}, f"{node_id}: broken transition {edge}={node[edge]!r}")
        if node.get("edges") is not None:
            _validate_edges(node, known, types)
            _SUBJECT.set((str(node_id), None))  # leave the edge subject behind
        else:
            _require(node["on_pass"] is not None or node["on_fail"] is not None or node_type in {"HUMAN_GATE", "FINAL_GATE"},
                     f"{node_id}: node has neither edges nor a legacy transition")

        if node_type in LLM_NODE_TYPES:
            has_model = isinstance(node.get("model"), str) and bool(node.get("model"))
            has_capability = isinstance(node.get("capability"), str) and bool(node.get("capability"))
            _require(has_model or has_capability, f"{node_id}: LLM node needs model or capability")
            _require(isinstance(node["role"], str) and node["role"], f"{node_id}: LLM node needs role")
            _require(isinstance(node["effort"], str) and node["effort"], f"{node_id}: LLM node needs effort")
        elif node_type == "MACHINE_GATE":
            command = node.get("command")
            _require(isinstance(command, list) and command and all(isinstance(x, str) and x for x in command), f"{node_id}: command must be a non-empty argv array")
            _require(type(node.get("timeout_seconds")) is int and node["timeout_seconds"] > 0, f"{node_id}: timeout_seconds must be positive")
        else:
            _require(node.get("model") in (None, ""), f"{node_id}: gate must not bind an LLM model")

    _SUBJECT.set((None, None))  # the rules below are about the graph, not a node
    start = data.get("start_node", ids[0])
    _require(start in known, "start_node is unknown")
    human_nodes = [node for node in nodes if node["type"] == "HUMAN_GATE"]
    _require(human_nodes, "workflow needs a HUMAN_GATE")
    _validate_reachable_and_cycles(nodes, start, limits["max_repair_cycles"])
    return data


def _validate_edges(node: Mapping[str, Any], known: set[str], types: Mapping[str, Any]) -> None:
    node_id = node["id"]
    edges = node["edges"]
    _require(isinstance(edges, list) and edges, f"{node_id}: edges must be a non-empty array")
    mode = node.get("routing", "FIRST_MATCH")
    _require(mode in ROUTING_MODES, f"{node_id}: unsupported routing mode {mode!r}")
    seen_ids: set[str] = set()
    for edge in edges:
        _SUBJECT.set((str(node_id), None))
        _require(isinstance(edge, dict), f"{node_id}: every edge must be an object")
        unknown = set(edge) - EDGE_FIELDS
        _require(not unknown, f"{node_id}: unsupported edge fields {sorted(unknown)}")
        edge_id = edge.get("edge_id")
        _require(isinstance(edge_id, str) and edge_id, f"{node_id}: every edge needs an edge_id")
        _require(edge_id not in seen_ids, f"{node_id}: duplicate edge_id {edge_id!r}")
        seen_ids.add(edge_id)
        _SUBJECT.set((str(node_id), str(edge_id)))  # this edge owns every rule below
        kind = edge.get("kind", "CONTINUE")
        _require(kind in EDGE_KINDS, f"{edge_id}: unsupported edge kind {kind!r}")
        target = edge.get("to")
        _require(target in known | TERMINALS, f"{edge_id}: broken edge target {target!r}")
        if kind == "REPAIR":
            # A REPAIR edge names a template, never a node to re-enter. The
            # runner mints an explicit lineage child from it, which is what
            # keeps repair out of a hidden retry loop.
            _require(target in known and types.get(str(target)) == "REPAIR",
                     f"{edge_id}: REPAIR edge must target a REPAIR template node")
        _validate_when(edge.get("when"), edge_id)
    _SUBJECT.set((str(node_id), None))
    if mode == "FIRST_MATCH":
        unconditional = [edge["edge_id"] for edge in edges if edge.get("when") in (None, {})]
        _require(len(unconditional) <= 1, f"{node_id}: FIRST_MATCH allows at most one unconditional edge")
        if unconditional:
            _require(edges[-1].get("edge_id") == unconditional[0],
                     f"{node_id}: an unconditional edge must be declared last, it shadows every edge after it")


def _validate_when(when: Any, edge_id: str) -> None:
    if when in (None, {}):
        return
    _require(isinstance(when, dict), f"{edge_id}: when must be an object or null")
    unknown = set(when) - PREDICATE_KEYS
    _require(not unknown, f"{edge_id}: unsupported predicates {sorted(unknown)}; V0.1 has no expression DSL")
    if "verdict" in when:
        _require(when["verdict"] in VERDICTS, f"{edge_id}: invalid verdict predicate {when['verdict']!r}")
    if "outcome" in when:
        _require(when["outcome"] in OUTCOMES, f"{edge_id}: invalid outcome predicate {when['outcome']!r}")
    if "has_findings" in when:
        _require(isinstance(when["has_findings"], bool), f"{edge_id}: has_findings must be a boolean")
    if "min_severity" in when:
        _require(str(when["min_severity"]).upper() in SEVERITY_LADDER,
                 f"{edge_id}: min_severity must be one of {list(SEVERITY_LADDER)}")


def _outgoing(node: Mapping[str, Any]) -> list[str]:
    if node.get("edges") is not None:
        return [str(edge.get("to")) for edge in node["edges"] if edge.get("to")]
    return [str(edge) for edge in (node.get("on_pass"), node.get("on_fail")) if edge]


def _validate_reachable_and_cycles(nodes: list[Mapping[str, Any]], start: str, max_repairs: int) -> None:
    by_id = {str(node["id"]): node for node in nodes}
    contract_mode = any(node.get("edges") is not None for node in nodes)
    reachable: set[str] = set()
    stack = [start]
    while stack:
        node_id = stack.pop()
        if node_id in reachable:
            continue
        reachable.add(node_id)
        for edge in _outgoing(by_id[node_id]):
            if edge in by_id:
                stack.append(edge)
    unreachable = sorted(set(by_id) - reachable)
    if unreachable:
        # Same message, attributed: a node nothing routes to is the canvas's
        # single most common intermediate state while a graph is being drawn,
        # and it must be markable on the node rather than only in a sentence.
        raise WorkflowValidationError(f"unreachable nodes: {unreachable}", node_id=unreachable[0])

    # Legacy workflows permit cycles only when every cyclic component includes
    # REPAIR. Contract workflows must be acyclic: explicit branch lineage is
    # what replaces the loop, so a declared cycle would reintroduce the very
    # hidden retry the contract removes.
    visiting: list[str] = []
    visited: set[str] = set()

    def walk(node_id: str) -> None:
        if node_id in visiting:
            cycle = visiting[visiting.index(node_id):]
            _require(not contract_mode,
                     f"routing-contract workflows must be acyclic; repair uses branch lineage, not a cycle: {cycle}")
            _require(any(by_id[item]["type"] == "REPAIR" for item in cycle), f"unbounded cycle without REPAIR: {cycle}")
            _require(max_repairs > 0, "repair cycle exists but max_repair_cycles is zero")
            return
        if node_id in visited:
            return
        visiting.append(node_id)
        for edge in _outgoing(by_id[node_id]):
            if edge in by_id:
                walk(edge)
        visiting.pop()
        visited.add(node_id)

    walk(start)


def validate_node_result(data: Any, expected_id: str, expected_type: str) -> dict[str, Any]:
    _require(isinstance(data, dict), "node result must be a JSON object")
    required = {"node_id", "node_type", "outcome", "summary", "changed_files", "tests", "findings", "remaining_uncertainty", "recommended_next_action"}
    _require(not (required - set(data)), f"node result missing fields: {sorted(required - set(data))}")
    _require(data["node_id"] == expected_id, "node result id mismatch")
    _require(data["node_type"] == expected_type, "node result type mismatch")
    _require(data["outcome"] in OUTCOMES, f"invalid node outcome {data['outcome']!r}")
    _require(isinstance(data["summary"], str), "summary must be a string")
    for key in ("changed_files", "tests", "findings", "remaining_uncertainty"):
        _require(isinstance(data[key], list), f"{key} must be an array")
    _require(isinstance(data["recommended_next_action"], str), "recommended_next_action must be a string")

    # Routing-contract handoff fields. Optional and additive: a node that does
    # not emit them routes on `outcome` exactly as before.
    if data.get("verdict") is not None:
        _require(data["verdict"] in VERDICTS, f"invalid verdict {data['verdict']!r}")
    if data.get("next_brief") is not None:
        _require(isinstance(data["next_brief"], str) and data["next_brief"].strip(),
                 "next_brief must be a non-empty string when present")
    for key in ("carry_forward", "artifacts"):
        if data.get(key) is not None:
            _require(isinstance(data[key], list) and all(isinstance(x, str) for x in data[key]),
                     f"{key} must be an array of strings")
    return dict(data)
