#!/usr/bin/env python3
"""AAW PLANNER PROPOSAL PIPELINE V0.1 — the proposal contract.

    anchor node + operator instruction
             │
             ▼
      planning_package()        bounded, inspectable, hashable planner input
             │
             ▼  (aaw_planner — provider seam, not this module)
      raw planner output        UNTRUSTED structured data
             │
             ▼
      normalize_proposal()      fills only what AAW owns, never planner content
             │
             ▼
      validate_proposal()       deterministic, fail-closed  →  PROPOSAL_INVALID
             │
             ▼
      apply_proposal()          pure; returns a NEW workflow document

The authority boundary this module exists to hold
-------------------------------------------------

  * **A planner proposes; it never executes and never mutates.** Every
    function here is pure. Nothing in this module opens a file, reaches a
    runner, a run, a journal or a Git repository. `apply_proposal` returns a
    new document; the caller decides whether anything is ever done with it.
  * **Planner output is untrusted structured input.** It is validated the way
    a network payload is: closed key sets, closed value sets, size bounds and
    character-class checks, before it is allowed anywhere near the real
    `workflow_schema.validate_workflow`.
  * **The real schema is the authority, not a parallel one.** A proposal is
    expressed in the workflow's own node and edge vocabulary, applied to a
    copy, and then validated by exactly the validator the runner loads a
    workflow through. There is no second definition of a legal graph.
  * **No silent repair.** The only values AAW fills are the ones the planner
    is not permitted to choose (identity, base hash, provider-bound model);
    every one of them is recorded in `materialization` so an operator can see
    it happened. A proposal that breaks a rule is refused with the rule's own
    message, never quietly corrected.
  * **Existing structure is protected.** After apply, every pre-existing node
    must be semantically byte-identical, apart from the anchor's declared,
    explicit edge changes. This is checked, not assumed.

Deliberately absent in V0.1, and named so nobody has to guess: `Modify`
(operator editing of a proposal before acceptance), `replacements` (rewriting
an existing node's body — the key is reserved and must be empty), automatic
three-way merge of a stale proposal, MACHINE_GATE proposals (a planner must
not author a command line), and planner-chosen model bindings.
"""

from __future__ import annotations

import copy
import os
import re
from dataclasses import dataclass, replace
from typing import Any, Mapping, Sequence

import routing_contract
import workflow_schema


PROPOSAL_CONTRACT = "AAW_PLANNER_PROPOSAL_PIPELINE_V0.1"
PROPOSAL_VERSION = "0.1"

# ── proposal lifecycle, as the UX sees it ────────────────────────────────
PROPOSAL_READY = "PROPOSAL_READY"
PROPOSAL_INVALID = "PROPOSAL_INVALID"
PROPOSAL_STALE = "PROPOSAL_STALE"
PROPOSAL_ACCEPTED = "PROPOSAL_ACCEPTED"
PROPOSAL_REJECTED = "PROPOSAL_REJECTED"
PROPOSAL_STATUSES = (PROPOSAL_READY, PROPOSAL_INVALID, PROPOSAL_STALE,
                     PROPOSAL_ACCEPTED, PROPOSAL_REJECTED)

# ── planner/proposal journal events ──────────────────────────────────────
PLANNER_STARTED = "PLANNER_STARTED"
PLANNER_COMPLETED = "PLANNER_COMPLETED"
PLANNER_FAILED = "PLANNER_FAILED"
PLANNER_EVENT_TYPES = (
    PLANNER_STARTED, PLANNER_COMPLETED, PLANNER_FAILED,
    PROPOSAL_READY, PROPOSAL_INVALID, PROPOSAL_STALE,
    PROPOSAL_ACCEPTED, PROPOSAL_REJECTED,
)

# ── refusal codes. Closed set: the UX renders these directly. ────────────
INVALID_SHAPE = "PROPOSAL_SHAPE"
INVALID_FIELD = "UNSUPPORTED_FIELD"
INVALID_TEXT = "UNSAFE_TEXT"
INVALID_SIZE = "SIZE_LIMIT"
INVALID_ANCHOR = "UNKNOWN_ANCHOR"
INVALID_NODE_ID = "NODE_ID"
INVALID_NODE_TYPE = "NODE_TYPE"
INVALID_COLLISION = "COLLIDES_WITH_EXISTING"
INVALID_DUPLICATE = "DUPLICATE_ID"
INVALID_EDGE_REF = "EDGE_REFERENCE"
INVALID_EDGE_SOURCE = "EDGE_SOURCE_NOT_ALLOWED"
INVALID_DETACH = "DETACH_NOT_ALLOWED"
INVALID_PROTECTED = "PROTECTED_STRUCTURE"
INVALID_SCHEMA = "SCHEMA"
INVALID_ROUTING = "ROUTING"
INVALID_BASE = "BASE_WORKFLOW"
REFUSAL_CODES = (
    INVALID_SHAPE, INVALID_FIELD, INVALID_TEXT, INVALID_SIZE, INVALID_ANCHOR,
    INVALID_NODE_ID, INVALID_NODE_TYPE, INVALID_COLLISION, INVALID_DUPLICATE,
    INVALID_EDGE_REF, INVALID_EDGE_SOURCE, INVALID_DETACH, INVALID_PROTECTED,
    INVALID_SCHEMA, INVALID_ROUTING, INVALID_BASE,
)

# ── what a planner may say at all ────────────────────────────────────────
#
# MACHINE_GATE is absent on purpose: its `command` field is an argv the runner
# actually spawns, and a planner must not author one. A machine gate is added
# by a human on the canvas, where the command is visible before it is saved.
PROPOSAL_NODE_TYPES = ("IMPLEMENT", "REVIEW", "REPAIR", "MERGE", "HUMAN_GATE", "FINAL_GATE")
PROPOSAL_ROLES = ("CODE_IMPLEMENTER", "INDEPENDENT_REVIEWER",
                  "ARCHITECT_STRONG", "RESEARCH_SYNTHESIZER")
PROPOSAL_CAPABILITIES = PROPOSAL_ROLES
PROPOSAL_EFFORTS = ("low", "medium", "high", "xhigh", "max")

PROPOSAL_FIELDS = ("proposal_version", "proposal_id", "workflow_id", "anchor_node_id",
                   "base_semantic_hash", "intent", "nodes", "edges", "detach_edges",
                   "anchor_routing", "replacements", "assumptions", "warnings")
# What a planner is required to author itself. Everything else in
# PROPOSAL_FIELDS is AAW's to fill, and filling it is recorded.
PLANNER_AUTHORED_FIELDS = ("intent", "nodes", "edges", "detach_edges",
                           "anchor_routing", "replacements", "assumptions", "warnings")

# `model` is absent: a planner does not choose a provider binding. `command`,
# `timeout_seconds` and `preprocess` are absent: a planner does not author an
# argv, a timeout or a local-preprocessing policy. `edges` is absent from the
# node: edges are declared once, at the top level, with an explicit source.
PROPOSAL_NODE_FIELDS = ("id", "type", "role", "capability", "effort", "instructions",
                        "acceptance", "depends_on", "run_if", "merge_policy",
                        "expected_incoming")
PROPOSAL_REQUIRED_NODE_FIELDS = ("id", "type", "instructions", "acceptance")
PROPOSAL_EDGE_FIELDS = ("edge_id", "from", "to", "when", "kind", "label")
PROPOSAL_REQUIRED_EDGE_FIELDS = ("edge_id", "from", "to")

ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")

# Fields excluded from proposal identity: they are volatile or they are
# provenance about *how* the proposal was obtained, not what it proposes.
VOLATILE_PROPOSAL_FIELDS = ("proposal_id", "created_at", "status", "planner",
                            "input_hash", "telemetry", "request_id", "validation",
                            "preview", "materialization", "resolved_at")


# ───────────────────────────── limits ─────────────────────────────

@dataclass(frozen=True)
class ProposalLimits:
    """Bounds on planner input and planner output. V0.1 is deliberately small.

    The goal is a useful extension of a graph a human is authoring, not the
    generation of an arbitrary one. Every bound is overridable from the
    environment so an operator can widen them without editing code.
    """

    max_nodes: int = 8
    max_edges: int = 16
    max_detach_edges: int = 4
    max_proposal_bytes: int = 64 * 1024
    max_package_bytes: int = 48 * 1024
    max_instruction_chars: int = 2_000
    max_text_chars: int = 4_000
    max_list_items: int = 16
    max_neighborhood_nodes: int = 24

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "ProposalLimits":
        source = dict(os.environ if environ is None else environ)
        values: dict[str, int] = {}
        for field in cls.__dataclass_fields__:
            raw = source.get(f"AAW_PLANNER_{field.upper()}")
            if raw is None:
                continue
            try:
                parsed = int(str(raw).strip())
            except ValueError:
                continue          # an unreadable override is ignored, never guessed at
            if parsed > 0:
                values[field] = parsed
        return replace(cls(), **values) if values else cls()

    def as_dict(self) -> dict[str, int]:
        return {field: getattr(self, field) for field in self.__dataclass_fields__}


DEFAULT_LIMITS = ProposalLimits()


# ───────────────────────────── refusals ─────────────────────────────

class ProposalRefusal(Exception):
    """One deterministic refusal, with the subject that caused it.

    Carries a closed `code` so the UX renders a reason rather than parsing a
    sentence — the one technique this project refuses everywhere.
    """

    def __init__(self, code: str, message: str, *, node_id: str | None = None,
                 edge_id: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.node_id = node_id
        self.edge_id = edge_id

    def as_diagnostic(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), "node_id": self.node_id,
                "edge_id": self.edge_id, "source": "PLANNER_PROPOSAL"}


def _refuse(condition: bool, code: str, message: str, *, node_id: str | None = None,
            edge_id: str | None = None) -> None:
    if not condition:
        raise ProposalRefusal(code, message, node_id=node_id, edge_id=edge_id)


# ───────────────────────────── text safety ─────────────────────────────

# Control characters are refused outright, plus U+2028/U+2029, which terminate
# a line inside a JavaScript string literal. Rendering safety in the canvas is
# escaping, and escaping is not negotiable there; this is the second wall, so
# that a proposal cannot carry a payload whose only defence is one `esc()`.
_FORBIDDEN_CHARS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u2028\u2029]")


def safe_text(value: Any, *, field: str, limits: ProposalLimits,
              node_id: str | None = None, edge_id: str | None = None,
              max_chars: int | None = None) -> str:
    _refuse(isinstance(value, str), INVALID_SHAPE, f"{field} must be a string",
            node_id=node_id, edge_id=edge_id)
    text = str(value)
    ceiling = int(max_chars or limits.max_text_chars)
    _refuse(len(text) <= ceiling, INVALID_SIZE,
            f"{field} exceeds {ceiling} characters ({len(text)})",
            node_id=node_id, edge_id=edge_id)
    match = _FORBIDDEN_CHARS.search(text)
    _refuse(match is None, INVALID_TEXT,
            f"{field} contains a forbidden control character U+{ord(match.group()):04X}"
            if match else "", node_id=node_id, edge_id=edge_id)
    return text


def _closed(value: Any, allowed: Sequence[str], *, code: str, field: str,
            node_id: str | None = None, edge_id: str | None = None) -> str:
    _refuse(isinstance(value, str) and value in allowed, code,
            f"{field} must be one of {list(allowed)}, got {value!r}",
            node_id=node_id, edge_id=edge_id)
    return str(value)


def _keys(row: Mapping[str, Any], allowed: Sequence[str], *, label: str,
          node_id: str | None = None, edge_id: str | None = None) -> None:
    unknown = sorted(set(row) - set(allowed))
    _refuse(not unknown, INVALID_FIELD,
            f"{label}: unsupported field(s) {unknown}; V0.1 accepts {list(allowed)}",
            node_id=node_id, edge_id=edge_id)


def _byte_size(value: Any) -> int:
    import json

    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                          default=str).encode("utf-8"))


# ───────────────────────── proposal identity ─────────────────────────

def canonical_proposal(proposal: Mapping[str, Any]) -> dict[str, Any]:
    """The identity-bearing slice of a proposal, in canonical form.

    A whitelist, like `routing_contract.semantic_workflow`: volatile and
    provenance fields are *absent* rather than ignored, so no timestamp,
    request id, telemetry blob or UI state can reach the hash however it was
    attached. Collections are ordered by their own ids so that a planner that
    emits the same mutation in a different order produces the same identity.
    """
    nodes = []
    for node in proposal.get("nodes") or []:
        if not isinstance(node, Mapping):
            continue
        nodes.append({key: node.get(key) for key in PROPOSAL_NODE_FIELDS if key in node})
    edges = []
    for edge in proposal.get("edges") or []:
        if not isinstance(edge, Mapping):
            continue
        edges.append({key: edge.get(key) for key in PROPOSAL_EDGE_FIELDS if key in edge})
    return {
        "proposal_version": proposal.get("proposal_version", PROPOSAL_VERSION),
        "contract": PROPOSAL_CONTRACT,
        "workflow_id": proposal.get("workflow_id"),
        "anchor_node_id": proposal.get("anchor_node_id"),
        "base_semantic_hash": proposal.get("base_semantic_hash"),
        "intent": proposal.get("intent"),
        "nodes": sorted(nodes, key=lambda row: str(row.get("id"))),
        "edges": sorted(edges, key=lambda row: str(row.get("edge_id"))),
        "detach_edges": sorted(str(item) for item in (proposal.get("detach_edges") or [])),
        "anchor_routing": proposal.get("anchor_routing"),
        "replacements": list(proposal.get("replacements") or []),
        "assumptions": list(proposal.get("assumptions") or []),
        "warnings": list(proposal.get("warnings") or []),
    }


def proposal_hash(proposal: Mapping[str, Any]) -> str:
    """Content identity of one proposal. Reuses the project's canonical hash.

    This is the identity of the *artifact*, not of the model call that
    produced it: two runs of a planner are not claimed to be deterministic,
    but two proposals with the same structured content always hash the same.
    """
    return routing_contract.canonical_hash(canonical_proposal(proposal))


def mint_proposal_id(proposal: Mapping[str, Any]) -> str:
    return "PROP-" + proposal_hash(proposal)[:16]


# ───────────────────────── planning package ─────────────────────────

def _outgoing(node: Mapping[str, Any]) -> list[dict[str, Any]]:
    if isinstance(node.get("edges"), list):
        return [{"edge_id": edge.get("edge_id"), "to": edge.get("to"),
                 "when": edge.get("when"), "kind": edge.get("kind", "CONTINUE"),
                 "label": edge.get("label")}
                for edge in node["edges"] if isinstance(edge, Mapping)]
    rows = []
    for field, kind in (("on_pass", "CONTINUE"), ("on_fail", "FALLBACK")):
        if node.get(field):
            rows.append({"edge_id": f"{node.get('id')}:LEGACY_{field[3:].upper()}",
                         "to": node[field], "when": None, "kind": kind, "label": field})
    return rows


def node_type_field_compatibility() -> dict[str, dict[str, bool]]:
    """Which of the type-conditional fields a planner may set, per node type.

    AAW_PLANNER_QUALITY_HARDENING_V0.3 evidence: `role`/`capability`/`effort`
    misapplied to a non-LLM node (chiefly MERGE) was the single most frequent
    V0.2 failure class (3 independent occurrences across two models). The
    English rule already stated this; this map states it again as a small,
    closed lookup table so a planner does not have to parse a paragraph to
    get a yes/no answer for one field on one node type. It is *generated*
    from the same authorities `_validate_nodes` and `workflow_schema` already
    enforce (`workflow_schema.LLM_NODE_TYPES`, the MERGE-only pair) — there is
    no second, parallel definition of node-type legality here.
    """
    llm = {"role": True, "capability": True, "effort": True,
           "merge_policy": False, "expected_incoming": False}
    merge = {"role": False, "capability": False, "effort": False,
             "merge_policy": True, "expected_incoming": True}
    neither = {"role": False, "capability": False, "effort": False,
               "merge_policy": False, "expected_incoming": False}
    return {
        node_type: dict(llm) if node_type in workflow_schema.LLM_NODE_TYPES
        else dict(merge) if node_type == "MERGE"
        else dict(neither)
        for node_type in PROPOSAL_NODE_TYPES
    }


def _neighborhood(workflow: Mapping[str, Any], anchor_id: str, *,
                  limits: ProposalLimits) -> dict[str, Any]:
    """Anchor + its direct predecessors + two hops downstream, bounded.

    Not the whole graph, and never the repository: a planner extending one
    node needs the shape around that node. Which nodes were included is part
    of the package, so the context is inspectable rather than implied.
    """
    nodes = [node for node in (workflow.get("nodes") or []) if isinstance(node, Mapping)]
    by_id = {str(node.get("id")): node for node in nodes}
    chosen: list[str] = [anchor_id] if anchor_id in by_id else []
    frontier, depth = [anchor_id], 0
    while frontier and depth < 2 and len(chosen) < limits.max_neighborhood_nodes:
        nxt: list[str] = []
        for node_id in frontier:
            for edge in _outgoing(by_id.get(node_id) or {}):
                target = str(edge.get("to"))
                if target in by_id and target not in chosen:
                    chosen.append(target)
                    nxt.append(target)
                    if len(chosen) >= limits.max_neighborhood_nodes:
                        break
        frontier, depth = nxt, depth + 1
    for node_id, node in by_id.items():
        if len(chosen) >= limits.max_neighborhood_nodes:
            break
        if node_id in chosen:
            continue
        if any(str(edge.get("to")) == anchor_id for edge in _outgoing(node)):
            chosen.append(node_id)
    rows = []
    for node_id in chosen:
        node = by_id[node_id]
        rows.append({
            "id": node_id, "type": node.get("type"), "role": node.get("role"),
            "effort": node.get("effort"), "run_if": node.get("run_if"),
            "routing": node.get("routing", "FIRST_MATCH"),
            "instructions": str(node.get("instructions") or "")[:600],
            "acceptance": [str(item)[:240] for item in (node.get("acceptance") or [])][:6],
            "merge_policy": node.get("merge_policy"),
            "expected_incoming": list(node.get("expected_incoming") or []) or None,
            "edges": _outgoing(node),
        })
    return {"included_node_ids": chosen, "nodes": rows,
            "truncated": len(chosen) < len(by_id)}


def planning_package(workflow: Mapping[str, Any], anchor_node_id: str, *,
                     instruction: str = "",
                     limits: ProposalLimits | None = None) -> dict[str, Any]:
    """The bounded, inspectable input one planner invocation is given.

    Everything in here is either a fact about the graph being extended or a
    rule the proposal must obey. No repository contents, no run history, no
    journal, no telemetry, no filesystem paths.
    """
    bounds = limits or DEFAULT_LIMITS
    anchor_id = str(anchor_node_id)
    by_id = {str(node.get("id")): node for node in (workflow.get("nodes") or [])
             if isinstance(node, Mapping)}
    _refuse(anchor_id in by_id, INVALID_ANCHOR,
            f"anchor node {anchor_id!r} is not in this workflow", node_id=anchor_id)
    anchor = by_id[anchor_id]
    text = safe_text(instruction or "", field="operator_instruction", limits=bounds,
                     max_chars=bounds.max_instruction_chars)

    existing_edge_ids = sorted({str(edge.get("edge_id"))
                                for node in by_id.values()
                                for edge in _outgoing(node) if edge.get("edge_id")})
    anchor_edges = _outgoing(anchor)
    unconditional = next((edge for edge in anchor_edges if not edge.get("when")), None)

    workflow_limits = workflow.get("limits") or {}
    node_count = len(by_id)
    workflow_max_nodes = workflow_limits.get("max_nodes")
    remaining_workflow_capacity = (max(0, int(workflow_max_nodes) - node_count)
                                   if isinstance(workflow_max_nodes, int) else None)
    proposal_node_budget = (bounds.max_nodes if remaining_workflow_capacity is None
                            else min(bounds.max_nodes, remaining_workflow_capacity))

    package = {
        "package_version": PROPOSAL_CONTRACT,
        "proposal_version": PROPOSAL_VERSION,
        "workflow": {
            "workflow_id": workflow.get("workflow_id"),
            "version": workflow.get("version"),
            "description": workflow.get("description"),
            "start_node": workflow.get("start_node"),
            "limits": workflow.get("limits"),
            "node_count": node_count,
        },
        "node_budget": {
            "current_nodes": node_count,
            "workflow_max_nodes": workflow_max_nodes,
            "remaining_workflow_capacity": remaining_workflow_capacity,
            "proposal_max_nodes": bounds.max_nodes,
            "proposal_node_budget": proposal_node_budget,
        },
        "base_semantic_hash": routing_contract.semantic_hash(workflow),
        "anchor": {
            "node_id": anchor_id, "type": anchor.get("type"), "role": anchor.get("role"),
            "effort": anchor.get("effort"),
            "routing": anchor.get("routing", "FIRST_MATCH"),
            "instructions": str(anchor.get("instructions") or "")[:1200],
            "acceptance": [str(item)[:240] for item in (anchor.get("acceptance") or [])][:8],
            "edges": anchor_edges,
            "unconditional_edge_id": unconditional.get("edge_id") if unconditional else None,
        },
        "neighborhood": _neighborhood(workflow, anchor_id, limits=bounds),
        "existing_node_ids": sorted(by_id),
        "existing_edge_ids": existing_edge_ids,
        "constraints": {
            "node_types": list(PROPOSAL_NODE_TYPES),
            "roles": list(PROPOSAL_ROLES),
            "capabilities": list(PROPOSAL_CAPABILITIES),
            "efforts": list(PROPOSAL_EFFORTS),
            "edge_kinds": list(routing_contract.EDGE_KINDS),
            "routing_modes": list(routing_contract.ROUTING_MODES),
            "predicate_keys": list(routing_contract.PREDICATE_KEYS),
            "verdicts": list(routing_contract.VERDICTS),
            "outcomes": sorted(workflow_schema.OUTCOMES),
            "severity_ladder": list(routing_contract.SEVERITY_LADDER),
            "merge_policies": list(routing_contract.MERGE_POLICIES),
            "terminals": list(routing_contract.TERMINALS),
            "node_fields": list(PROPOSAL_NODE_FIELDS),
            "edge_fields": list(PROPOSAL_EDGE_FIELDS),
            "node_type_field_compatibility": node_type_field_compatibility(),
        },
        "rules": [
            "Every proposed node id must be new; it may not collide with an existing id.",
            "An edge may leave only the anchor node or a node this proposal creates.",
            "An edge may arrive at a proposed node, an existing node, or a terminal "
            f"({', '.join(routing_contract.TERMINALS)}).",
            "Under FIRST_MATCH a node may declare at most one unconditional edge (when "
            "null/absent), and it must be declared last, because it shadows every edge "
            "after it. anchor.unconditional_edge_id names the anchor's own one, if any.",
            "A MERGE node must declare merge_policy and expected_incoming, and "
            "expected_incoming must exactly equal the edges that arrive at it in the "
            "resulting graph — no undeclared incoming edge, no empty declared slot.",
            "A REPAIR edge must target a REPAIR node; the runtime mints a lineage child "
            "from it and never re-enters it.",
            "The resulting graph must be acyclic and every node must be reachable.",
            "MACHINE_GATE may not be proposed: a planner does not author a command line.",
            "No model may be named: provider binding is resolved by AAW, not by the planner.",
            "To splice a subgraph into an existing route, list the anchor's own outgoing "
            "edge id in detach_edges and re-declare the route through the new nodes.",
            "Existing nodes are immutable; only the anchor's outgoing edges may change.",
            "A HUMAN_GATE or FINAL_GATE halts the run unconditionally the moment it is "
            "reached; the runtime never evaluates edges declared on one, even if legal. "
            "Model a step that must wait for a human decision as a HUMAN_GATE with no "
            "outgoing edges — the run resumes, if at all, as a separate later run started "
            "from a chosen node, not by continuing through this graph's edges.",
            "role, capability, effort, merge_policy and expected_incoming are legal only "
            "on the node types constraints.node_type_field_compatibility marks true; "
            "leave a field null wherever it is false there. In short: role/capability/"
            "effort on IMPLEMENT/REVIEW/REPAIR only, merge_policy/expected_incoming on "
            "MERGE only, nothing of the five on HUMAN_GATE or FINAL_GATE.",
            "This proposal may add at most node_budget.proposal_node_budget new nodes — "
            "the smaller of the per-proposal cap and the workflow's own remaining "
            "limits.max_nodes headroom (already computed for you; do not add nodes past "
            "it hoping the workflow limit does not apply, it does). If the budget is 0, "
            "propose only new edges/detach among existing nodes, or explain in warnings "
            "why no legal extension is possible.",
            "Prefer the smallest graph that satisfies the operator's stated instruction. "
            "Do not add a redundant review stage, an unnecessary branch, a decorative "
            "HUMAN_GATE, an extra MERGE, or an extra research/synthesis stage unless the "
            "instruction or the existing graph's semantics actually call for it.",
        ],
        "limits": bounds.as_dict(),
        "operator_instruction": text,
        "response_schema": {
            "intent": "one sentence: what this subgraph is for",
            "nodes": [{"id": "N??", "type": "|".join(PROPOSAL_NODE_TYPES),
                       "role": "|".join(PROPOSAL_ROLES),
                       "capability": "|".join(PROPOSAL_CAPABILITIES),
                       "effort": "|".join(PROPOSAL_EFFORTS),
                       "instructions": "string", "acceptance": ["string"],
                       "depends_on": ["existing or proposed node id"],
                       "run_if": "ALWAYS|ON_TRANSITION",
                       "merge_policy": "MERGE only", "expected_incoming": ["MERGE only"]}],
            "edges": [{"edge_id": "E_FROM_TO", "from": "node id", "to": "node id",
                       "when": None, "kind": "CONTINUE|REPAIR|FALLBACK", "label": "string"}],
            "detach_edges": ["anchor edge id to remove"],
            "anchor_routing": "FIRST_MATCH|ALL_MATCHES or null",
            "assumptions": ["string"], "warnings": ["string"],
        },
    }
    size = _byte_size(package)
    _refuse(size <= bounds.max_package_bytes, INVALID_SIZE,
            f"planning package is {size} bytes, over the {bounds.max_package_bytes} limit")
    package["package_bytes"] = size
    return package


def package_hash(package: Mapping[str, Any]) -> str:
    """Stable identity of one planner input."""
    return routing_contract.canonical_hash({"contract": PROPOSAL_CONTRACT,
                                            "package": dict(package)})


# ───────────────────────── normalization ─────────────────────────

def normalize_proposal(raw: Any, *, workflow: Mapping[str, Any], anchor_node_id: str,
                       intent_fallback: str = "") -> dict[str, Any]:
    """Turn raw planner output into a proposal envelope. Fills only AAW's own fields.

    What is filled, and why it is not planner content:

      `proposal_version`   the contract this pipeline speaks
      `workflow_id`        the workflow the operator is editing
      `anchor_node_id`     the node the operator selected
      `base_semantic_hash` measured from the workflow, never reported by a planner
      `proposal_id`        derived from the content, so identity is deterministic

    Everything else is the planner's, used exactly as given. A planner that
    contradicts one of the filled fields is refused here rather than corrected.
    """
    _refuse(isinstance(raw, Mapping), INVALID_SHAPE,
            f"planner output must be a JSON object, got {type(raw).__name__}")
    row = dict(raw)
    for field in ("proposal_version", "workflow_id", "anchor_node_id", "base_semantic_hash"):
        if field in row:
            # A planner may echo these back; it may not disagree about them.
            pass
    anchor = str(anchor_node_id)
    stated_anchor = row.get("anchor_node_id")
    _refuse(stated_anchor in (None, "", anchor), INVALID_ANCHOR,
            f"planner proposed against anchor {stated_anchor!r}, the operator selected {anchor!r}")
    stated_workflow = row.get("workflow_id")
    _refuse(stated_workflow in (None, "", workflow.get("workflow_id")), INVALID_SHAPE,
            f"planner proposed against workflow {stated_workflow!r}, "
            f"the operator is editing {workflow.get('workflow_id')!r}")

    proposal: dict[str, Any] = {
        "proposal_version": PROPOSAL_VERSION,
        "workflow_id": workflow.get("workflow_id"),
        "anchor_node_id": anchor,
        "base_semantic_hash": routing_contract.semantic_hash(workflow),
        "intent": row.get("intent") if isinstance(row.get("intent"), str) else intent_fallback,
        "nodes": row.get("nodes") if isinstance(row.get("nodes"), list) else [],
        "edges": row.get("edges") if isinstance(row.get("edges"), list) else [],
        "detach_edges": row.get("detach_edges") if isinstance(row.get("detach_edges"), list) else [],
        "anchor_routing": row.get("anchor_routing") or None,
        "replacements": row.get("replacements") if isinstance(row.get("replacements"), list) else [],
        "assumptions": row.get("assumptions") if isinstance(row.get("assumptions"), list) else [],
        "warnings": row.get("warnings") if isinstance(row.get("warnings"), list) else [],
    }
    # Keys the planner emitted that this contract does not carry are refused
    # rather than dropped: a planner that thinks it can set `model` or
    # `command` must find out, not be quietly overruled.
    _keys(row, PROPOSAL_FIELDS, label="proposal")
    proposal["proposal_id"] = mint_proposal_id(proposal)
    return proposal


def inherited_model(workflow: Mapping[str, Any], anchor_node_id: str) -> str | None:
    """The model a proposed LLM node is materialized with. Deterministic.

    A planner never names a model — choosing a provider binding is not a
    planning decision. The proposed node inherits the anchor's model when the
    anchor is an LLM node that binds one, else the first bound model in the
    graph, else nothing (and then `capability` carries the binding, exactly as
    it does for a hand-authored node).
    """
    nodes = [node for node in (workflow.get("nodes") or []) if isinstance(node, Mapping)]
    anchor = next((node for node in nodes if str(node.get("id")) == str(anchor_node_id)), None)
    if anchor and isinstance(anchor.get("model"), str) and anchor["model"]:
        return str(anchor["model"])
    for node in nodes:
        if str(node.get("type")) in workflow_schema.LLM_NODE_TYPES and \
                isinstance(node.get("model"), str) and node["model"]:
            return str(node["model"])
    return None


def materialize_node(spec: Mapping[str, Any], *, model: str | None) -> dict[str, Any]:
    """One proposed node as a real workflow node.

    The added fields are the schema's scaffolding — the keys
    `validate_workflow` requires of every node regardless of type — plus the
    inherited model. Nothing here invents behaviour.
    """
    node: dict[str, Any] = {
        "id": str(spec["id"]),
        "type": str(spec["type"]),
        "depends_on": [str(item) for item in (spec.get("depends_on") or [])],
        "run_if": str(spec.get("run_if") or "ON_TRANSITION"),
        "role": spec.get("role"),
        "model": None,
        "effort": spec.get("effort"),
        "instructions": str(spec.get("instructions") or ""),
        "acceptance": [str(item) for item in (spec.get("acceptance") or [])],
        "on_pass": None,
        "on_fail": None,
    }
    if str(spec["type"]) in workflow_schema.LLM_NODE_TYPES:
        if spec.get("capability"):
            node["capability"] = str(spec["capability"])
        if model:
            node["model"] = model
    if str(spec["type"]) == "MERGE":
        node["merge_policy"] = spec.get("merge_policy")
        node["expected_incoming"] = [str(item) for item in (spec.get("expected_incoming") or [])]
    return node


# ───────────────────────────── apply ─────────────────────────────

def _materialize_when(when: Any) -> dict[str, Any] | None:
    """A proposed edge's `when`, with an explicit-null predicate key dropped.

    AAW_PLANNER_LIVE_PROVIDER_VALIDATION_V0.2 evidence: a provider whose
    structured-output mode requires every declared object property to be
    present (OpenAI's strict JSON Schema — see `aaw_planner.proposal_output_schema`)
    cannot emit a `when` with only the one predicate key it means; it must
    supply all of `routing_contract.PREDICATE_KEYS` and set the rest to
    `null`. `workflow_schema._validate_when` treats a *present* key as an
    asserted predicate regardless of its value, so an unstripped null reads as
    "outcome must equal null" and is refused — a representation artifact, not
    a predicate the planner meant to assert. A null value and an absent key
    are the same predicate; this is the one place that equivalence is made,
    for a proposed edge only, before the real validator ever sees it.
    """
    if not isinstance(when, Mapping):
        return None
    cleaned = {key: value for key, value in when.items() if value is not None}
    return cleaned or None


def apply_proposal(workflow: Mapping[str, Any], proposal: Mapping[str, Any]) -> dict[str, Any]:
    """`workflow` + `proposal` → a NEW workflow document. Pure.

    The input is deep-copied first, so a caller holding the live definition
    cannot observe a half-applied graph even if this raises. Nothing is
    written; the result is a candidate, and a candidate is not a workflow
    until the validated save path accepts it.
    """
    applied = copy.deepcopy(dict(workflow))
    nodes = list(applied.get("nodes") or [])
    anchor_id = str(proposal.get("anchor_node_id"))
    model = inherited_model(workflow, anchor_id)

    for spec in proposal.get("nodes") or []:
        nodes.append(materialize_node(spec, model=model))
    applied["nodes"] = nodes
    by_id = {str(node.get("id")): node for node in nodes if isinstance(node, dict)}

    detach = {str(item) for item in (proposal.get("detach_edges") or [])}
    if detach:
        anchor = by_id.get(anchor_id)
        if isinstance(anchor, dict) and isinstance(anchor.get("edges"), list):
            anchor["edges"] = [edge for edge in anchor["edges"]
                               if str(edge.get("edge_id")) not in detach]

    for edge in proposal.get("edges") or []:
        source = by_id.get(str(edge.get("from")))
        if not isinstance(source, dict):
            raise ProposalRefusal(INVALID_EDGE_SOURCE,
                                  f"edge {edge.get('edge_id')!r} leaves unknown node "
                                  f"{edge.get('from')!r}", edge_id=str(edge.get("edge_id")))
        row = {"edge_id": str(edge.get("edge_id")), "to": edge.get("to"),
               "when": _materialize_when(edge.get("when")),
               "kind": str(edge.get("kind") or "CONTINUE")}
        if edge.get("label"):
            row["label"] = str(edge["label"])
        source.setdefault("edges", []).append(row)
        source.setdefault("routing", "FIRST_MATCH")

    routing = proposal.get("anchor_routing")
    if routing:
        anchor = by_id.get(anchor_id)
        if isinstance(anchor, dict):
            anchor["routing"] = str(routing)

    # A node that now declares `edges` no longer routes on the legacy pair, and
    # a HUMAN_GATE/FINAL_GATE that gained no edge keeps neither. Nothing else
    # about any node is touched.
    for node in nodes:
        if isinstance(node, dict) and isinstance(node.get("edges"), list) and not node["edges"]:
            node.pop("edges")
    return applied


# ───────────────────────────── validation ─────────────────────────────

def _validate_envelope(proposal: Mapping[str, Any], *, limits: ProposalLimits) -> None:
    _keys(proposal, tuple(PROPOSAL_FIELDS), label="proposal")
    _refuse(str(proposal.get("proposal_version")) == PROPOSAL_VERSION, INVALID_SHAPE,
            f"unsupported proposal_version {proposal.get('proposal_version')!r}; "
            f"this pipeline speaks {PROPOSAL_VERSION}")
    safe_text(proposal.get("intent") or "", field="intent", limits=limits)
    for field in ("assumptions", "warnings"):
        rows = proposal.get(field) or []
        _refuse(isinstance(rows, list), INVALID_SHAPE, f"{field} must be an array")
        _refuse(len(rows) <= limits.max_list_items, INVALID_SIZE,
                f"{field} has {len(rows)} entries, over the {limits.max_list_items} limit")
        for index, item in enumerate(rows):
            safe_text(item, field=f"{field}[{index}]", limits=limits)
    replacements = proposal.get("replacements") or []
    _refuse(isinstance(replacements, list), INVALID_SHAPE, "replacements must be an array")
    _refuse(not replacements, INVALID_FIELD,
            "replacements is reserved in V0.1 and must be empty: rewriting an existing "
            "node's body is an explicitly deferred capability, not a silent one")
    if proposal.get("anchor_routing") is not None:
        _closed(proposal["anchor_routing"], list(routing_contract.ROUTING_MODES),
                code=INVALID_ROUTING, field="anchor_routing")
    size = _byte_size(canonical_proposal(proposal))
    _refuse(size <= limits.max_proposal_bytes, INVALID_SIZE,
            f"proposal is {size} bytes, over the {limits.max_proposal_bytes} limit")


def _validate_nodes(proposal: Mapping[str, Any], existing: Mapping[str, Any], *,
                    limits: ProposalLimits) -> list[str]:
    nodes = proposal.get("nodes") or []
    _refuse(isinstance(nodes, list), INVALID_SHAPE, "nodes must be an array")
    _refuse(len(nodes) <= limits.max_nodes, INVALID_SIZE,
            f"proposal declares {len(nodes)} nodes, over the {limits.max_nodes} limit")
    seen: list[str] = []
    for index, spec in enumerate(nodes):
        _refuse(isinstance(spec, Mapping), INVALID_SHAPE, f"nodes[{index}] must be an object")
        node_id = spec.get("id")
        _refuse(isinstance(node_id, str) and bool(ID_PATTERN.match(node_id)), INVALID_NODE_ID,
                f"nodes[{index}]: id must match {ID_PATTERN.pattern}, got {node_id!r}")
        node_id = str(node_id)
        _keys(spec, PROPOSAL_NODE_FIELDS, label=f"node {node_id}", node_id=node_id)
        missing = [field for field in PROPOSAL_REQUIRED_NODE_FIELDS if field not in spec]
        _refuse(not missing, INVALID_SHAPE,
                f"node {node_id}: missing field(s) {missing}", node_id=node_id)
        _refuse(node_id not in seen, INVALID_DUPLICATE,
                f"node id {node_id} is declared twice in this proposal", node_id=node_id)
        _refuse(node_id not in existing, INVALID_COLLISION,
                f"node id {node_id} already exists in this workflow", node_id=node_id)
        seen.append(node_id)

        node_type = _closed(spec.get("type"), PROPOSAL_NODE_TYPES, code=INVALID_NODE_TYPE,
                            field=f"node {node_id}: type", node_id=node_id)
        safe_text(spec.get("instructions"), field=f"node {node_id}: instructions",
                  limits=limits, node_id=node_id)
        acceptance = spec.get("acceptance")
        _refuse(isinstance(acceptance, list), INVALID_SHAPE,
                f"node {node_id}: acceptance must be an array", node_id=node_id)
        _refuse(len(acceptance) <= limits.max_list_items, INVALID_SIZE,
                f"node {node_id}: acceptance has {len(acceptance)} entries, over "
                f"the {limits.max_list_items} limit", node_id=node_id)
        for position, item in enumerate(acceptance):
            safe_text(item, field=f"node {node_id}: acceptance[{position}]",
                      limits=limits, node_id=node_id)
        if spec.get("run_if") is not None:
            _closed(spec["run_if"], ("ALWAYS", "ON_TRANSITION"), code=INVALID_SHAPE,
                    field=f"node {node_id}: run_if", node_id=node_id)
        depends = spec.get("depends_on") or []
        _refuse(isinstance(depends, list), INVALID_SHAPE,
                f"node {node_id}: depends_on must be an array", node_id=node_id)
        for item in depends:
            _refuse(isinstance(item, str) and bool(ID_PATTERN.match(item)), INVALID_SHAPE,
                    f"node {node_id}: depends_on entry {item!r} is not a node id",
                    node_id=node_id)
        if node_type in workflow_schema.LLM_NODE_TYPES:
            _refuse(spec.get("role") is not None, INVALID_SHAPE,
                    f"node {node_id}: an {node_type} node needs a role", node_id=node_id)
            _closed(spec.get("role"), PROPOSAL_ROLES, code=INVALID_SHAPE,
                    field=f"node {node_id}: role", node_id=node_id)
            _closed(spec.get("effort"), PROPOSAL_EFFORTS, code=INVALID_SHAPE,
                    field=f"node {node_id}: effort", node_id=node_id)
            if spec.get("capability") is not None:
                _closed(spec["capability"], PROPOSAL_CAPABILITIES, code=INVALID_SHAPE,
                        field=f"node {node_id}: capability", node_id=node_id)
        else:
            for field in ("role", "capability", "effort"):
                _refuse(spec.get(field) in (None, ""), INVALID_FIELD,
                        f"node {node_id}: {field} is meaningful only on an LLM node",
                        node_id=node_id)
        if node_type == "MERGE":
            _closed(spec.get("merge_policy"), list(routing_contract.MERGE_POLICIES),
                    code=INVALID_SHAPE, field=f"node {node_id}: merge_policy", node_id=node_id)
            expected = spec.get("expected_incoming")
            _refuse(isinstance(expected, list) and bool(expected) and
                    all(isinstance(item, str) and item for item in expected), INVALID_SHAPE,
                    f"node {node_id}: a MERGE needs a non-empty expected_incoming array",
                    node_id=node_id)
        else:
            for field in ("merge_policy", "expected_incoming"):
                _refuse(spec.get(field) in (None, [], ""), INVALID_FIELD,
                        f"node {node_id}: {field} is meaningful only on a MERGE node",
                        node_id=node_id)
    return seen


def _validate_edges(proposal: Mapping[str, Any], existing: Mapping[str, Any],
                    proposed_ids: Sequence[str], *, limits: ProposalLimits) -> None:
    edges = proposal.get("edges") or []
    _refuse(isinstance(edges, list), INVALID_SHAPE, "edges must be an array")
    _refuse(len(edges) <= limits.max_edges, INVALID_SIZE,
            f"proposal declares {len(edges)} edges, over the {limits.max_edges} limit")
    anchor_id = str(proposal.get("anchor_node_id"))
    existing_edge_ids = {str(edge.get("edge_id"))
                         for node in existing.values() for edge in _outgoing(node)
                         if edge.get("edge_id")}
    detached = {str(item) for item in (proposal.get("detach_edges") or [])}
    allowed_sources = {anchor_id, *proposed_ids}
    allowed_targets = set(existing) | set(proposed_ids) | set(routing_contract.TERMINALS)
    seen: set[str] = set()
    for index, edge in enumerate(edges):
        _refuse(isinstance(edge, Mapping), INVALID_SHAPE, f"edges[{index}] must be an object")
        edge_id = edge.get("edge_id")
        _refuse(isinstance(edge_id, str) and bool(edge_id) and len(edge_id) <= 64,
                INVALID_EDGE_REF, f"edges[{index}]: edge_id must be a short non-empty string")
        edge_id = str(edge_id)
        _keys(edge, PROPOSAL_EDGE_FIELDS, label=f"edge {edge_id}", edge_id=edge_id)
        missing = [field for field in PROPOSAL_REQUIRED_EDGE_FIELDS if field not in edge]
        _refuse(not missing, INVALID_SHAPE,
                f"edge {edge_id}: missing field(s) {missing}", edge_id=edge_id)
        _refuse(edge_id not in seen, INVALID_DUPLICATE,
                f"edge id {edge_id} is declared twice in this proposal", edge_id=edge_id)
        seen.add(edge_id)
        _refuse(edge_id not in (existing_edge_ids - detached), INVALID_COLLISION,
                f"edge id {edge_id} already exists in this workflow", edge_id=edge_id)
        source = str(edge.get("from"))
        _refuse(source in allowed_sources, INVALID_EDGE_SOURCE,
                f"edge {edge_id} leaves {source!r}; an edge may leave only the anchor "
                f"({anchor_id}) or a node this proposal creates", edge_id=edge_id)
        target = str(edge.get("to"))
        _refuse(target in allowed_targets, INVALID_EDGE_REF,
                f"edge {edge_id} arrives at {target!r}, which is neither a node nor a "
                f"terminal ({', '.join(routing_contract.TERMINALS)})", edge_id=edge_id)
        _closed(edge.get("kind", "CONTINUE"), list(routing_contract.EDGE_KINDS),
                code=INVALID_EDGE_REF, field=f"edge {edge_id}: kind", edge_id=edge_id)
        if edge.get("label") is not None:
            safe_text(edge["label"], field=f"edge {edge_id}: label", limits=limits,
                      edge_id=edge_id, max_chars=120)
        when = edge.get("when")
        if when not in (None, {}):
            _refuse(isinstance(when, Mapping), INVALID_EDGE_REF,
                    f"edge {edge_id}: when must be an object or null", edge_id=edge_id)
            unknown = sorted(set(when) - set(routing_contract.PREDICATE_KEYS))
            _refuse(not unknown, INVALID_EDGE_REF,
                    f"edge {edge_id}: unsupported predicate(s) {unknown}; V0.1 has no "
                    f"expression DSL", edge_id=edge_id)

    _refuse(isinstance(proposal.get("detach_edges") or [], list), INVALID_SHAPE,
            "detach_edges must be an array")
    _refuse(len(detached) <= limits.max_detach_edges, INVALID_SIZE,
            f"detach_edges names {len(detached)} edges, over the "
            f"{limits.max_detach_edges} limit")
    anchor_edge_ids = {str(edge.get("edge_id")) for edge in _outgoing(existing.get(anchor_id) or {})}
    for edge_id in sorted(detached):
        _refuse(edge_id in anchor_edge_ids, INVALID_DETACH,
                f"detach_edges names {edge_id!r}, which is not an outgoing edge of the "
                f"anchor {anchor_id}; only the anchor's own edges may be detached",
                edge_id=edge_id)


def _protection_report(before: Mapping[str, Any], after: Mapping[str, Any],
                       proposal: Mapping[str, Any]) -> None:
    """Prove that nothing outside the declared mutation moved.

    Compares the two *semantic* projections, so a difference here is a
    difference in what the graph executes — not in formatting, ordering of
    unrelated keys, or any visual field.
    """
    anchor_id = str(proposal.get("anchor_node_id"))
    left = routing_contract.semantic_workflow(before)
    right = routing_contract.semantic_workflow(after)
    for key, value in left.items():
        if key == "nodes":
            continue
        _refuse(right.get(key) == value, INVALID_PROTECTED,
                f"a proposal may not change workflow field {key!r} "
                f"({value!r} → {right.get(key)!r})")
    _refuse(set(left) == set(right), INVALID_PROTECTED,
            "a proposal may not add or remove a workflow-level field")

    left_nodes = {str(node.get("id")): node for node in left.get("nodes") or []}
    right_nodes = {str(node.get("id")): node for node in right.get("nodes") or []}
    missing = sorted(set(left_nodes) - set(right_nodes))
    _refuse(not missing, INVALID_PROTECTED, f"a proposal may not remove node(s) {missing}")
    proposed = {str(spec.get("id")) for spec in (proposal.get("nodes") or [])
                if isinstance(spec, Mapping)}
    unexpected = sorted(set(right_nodes) - set(left_nodes) - proposed)
    _refuse(not unexpected, INVALID_PROTECTED,
            f"applying the proposal created undeclared node(s) {unexpected}")

    added_edge_ids = {str(edge.get("edge_id")) for edge in (proposal.get("edges") or [])
                      if isinstance(edge, Mapping)}
    detached = {str(item) for item in (proposal.get("detach_edges") or [])}
    for node_id, original in left_nodes.items():
        current = right_nodes[node_id]
        if node_id != anchor_id:
            _refuse(current == original, INVALID_PROTECTED,
                    f"a proposal may not change existing node {node_id}", node_id=node_id)
            continue
        for key in set(original) | set(current):
            if key in ("edges", "routing"):
                continue
            _refuse(current.get(key) == original.get(key), INVALID_PROTECTED,
                    f"a proposal may change only the anchor's outgoing edges; "
                    f"{anchor_id}.{key} moved", node_id=anchor_id)
        if current.get("routing") != original.get("routing"):
            _refuse(proposal.get("anchor_routing") is not None, INVALID_PROTECTED,
                    f"{anchor_id}.routing changed without a declared anchor_routing",
                    node_id=anchor_id)
        was = {str(edge.get("edge_id")) for edge in (original.get("edges") or [])}
        now = {str(edge.get("edge_id")) for edge in (current.get("edges") or [])}
        _refuse(now == (was - detached) | {eid for eid in added_edge_ids
                                           if _edge_source(proposal, eid) == anchor_id},
                INVALID_PROTECTED,
                f"{anchor_id}'s outgoing edge set does not match the declared mutation "
                f"(was {sorted(was)}, now {sorted(now)}, detaching {sorted(detached)})",
                node_id=anchor_id)
        for edge in (original.get("edges") or []):
            edge_id = str(edge.get("edge_id"))
            if edge_id in detached:
                continue
            kept = next((row for row in (current.get("edges") or [])
                         if str(row.get("edge_id")) == edge_id), None)
            _refuse(kept == edge, INVALID_PROTECTED,
                    f"a proposal may not rewrite the anchor's existing edge {edge_id}",
                    node_id=anchor_id, edge_id=edge_id)


def _edge_source(proposal: Mapping[str, Any], edge_id: str) -> str | None:
    for edge in proposal.get("edges") or []:
        if isinstance(edge, Mapping) and str(edge.get("edge_id")) == str(edge_id):
            return str(edge.get("from"))
    return None


def validate_proposal(workflow: Mapping[str, Any], proposal: Mapping[str, Any], *,
                      limits: ProposalLimits | None = None,
                      schema_validator: Any = None) -> dict[str, Any]:
    """Deterministic, fail-closed validation of one proposal against one workflow.

    Order matters and is deliberate: envelope → size → ids → edges → apply →
    the *real* workflow validator → structural protection. The cheap, closed
    checks run first so that malformed planner output never reaches the real
    validator's assumptions, and the real validator is still the last word on
    whether the resulting graph is legal.

    `schema_validator(candidate) -> report` is the bridge's own
    `validate_candidate`, injected so this module stays pure and the schema
    rules keep exactly one implementation. Without it, the module falls back
    to `workflow_schema.validate_workflow` plus `routing_contract.compile_edges`,
    which is the same pair the bridge composes.
    """
    bounds = limits or DEFAULT_LIMITS
    report: dict[str, Any] = {
        "contract": PROPOSAL_CONTRACT, "status": PROPOSAL_INVALID, "valid": False,
        "diagnostics": [], "errors": [],
        "base_semantic_hash": routing_contract.semantic_hash(workflow),
        "candidate": None, "projection": None, "semantic_hash": None,
        "added_node_ids": [], "added_edge_ids": [], "detached_edge_ids": [],
        "limits": bounds.as_dict(),
    }

    def refuse(exc: ProposalRefusal) -> dict[str, Any]:
        report["diagnostics"] = [exc.as_diagnostic()]
        report["errors"] = [str(exc)]
        return report

    existing = {str(node.get("id")): node for node in (workflow.get("nodes") or [])
                if isinstance(node, Mapping)}
    try:
        _refuse(isinstance(proposal, Mapping), INVALID_SHAPE, "proposal must be an object")
        _refuse(str(proposal.get("workflow_id")) == str(workflow.get("workflow_id")),
                INVALID_BASE,
                f"proposal targets workflow {proposal.get('workflow_id')!r}, "
                f"this is {workflow.get('workflow_id')!r}")
        anchor_id = str(proposal.get("anchor_node_id"))
        _refuse(anchor_id in existing, INVALID_ANCHOR,
                f"anchor node {anchor_id!r} is not in this workflow", node_id=anchor_id)
        _validate_envelope(proposal, limits=bounds)
        proposed_ids = _validate_nodes(proposal, existing, limits=bounds)
        _validate_edges(proposal, existing, proposed_ids, limits=bounds)
        _refuse(bool(proposed_ids) or bool(proposal.get("edges")), INVALID_SHAPE,
                "a proposal must add at least one node or one edge")
        candidate = apply_proposal(workflow, proposal)
    except ProposalRefusal as exc:
        return refuse(exc)

    # The real validator, not an approximation of it. Everything above is a
    # closed-set gate over untrusted input; legality of the *graph* is decided
    # here and only here.
    if schema_validator is not None:
        schema_report = schema_validator(candidate)
        if not schema_report.get("valid"):
            report["diagnostics"] = [
                {**dict(row), "code": INVALID_SCHEMA, "source": row.get("source") or "SCHEMA"}
                for row in (schema_report.get("diagnostics") or [])
            ] or [{"code": INVALID_SCHEMA, "message": "; ".join(schema_report.get("errors") or []),
                   "node_id": None, "edge_id": None, "source": "SCHEMA"}]
            report["errors"] = list(schema_report.get("errors") or [])
            return report
        projection = schema_report.get("projection")
        semantic = schema_report.get("semantic_hash")
        candidate = schema_report.get("candidate") or candidate
    else:
        try:
            workflow_schema.validate_workflow(candidate)
            for node in candidate["nodes"]:
                routing_contract.compile_edges(node)
            projection = routing_contract.workflow_projection(candidate)
            semantic = projection["semantic_hash"]
        except workflow_schema.WorkflowValidationError as exc:
            report["diagnostics"] = [{"code": INVALID_SCHEMA, "message": str(exc),
                                      "node_id": getattr(exc, "node_id", None),
                                      "edge_id": getattr(exc, "edge_id", None),
                                      "source": "SCHEMA"}]
            report["errors"] = [str(exc)]
            return report
        except routing_contract.RoutingContractError as exc:
            report["diagnostics"] = [{"code": INVALID_ROUTING, "message": f"routing: {exc}",
                                      "node_id": None, "edge_id": None, "source": "ROUTING"}]
            report["errors"] = [f"routing: {exc}"]
            return report
        except (TypeError, KeyError, AttributeError) as exc:
            report["diagnostics"] = [{"code": INVALID_SCHEMA,
                                      "message": f"malformed candidate: {exc!r}",
                                      "node_id": None, "edge_id": None, "source": "MALFORMED"}]
            report["errors"] = [f"malformed candidate: {exc!r}"]
            return report

    try:
        _protection_report(workflow, candidate, proposal)
    except ProposalRefusal as exc:
        return refuse(exc)

    report.update({
        "status": PROPOSAL_READY, "valid": True, "diagnostics": [], "errors": [],
        "candidate": candidate, "projection": projection, "semantic_hash": semantic,
        "added_node_ids": list(proposed_ids),
        "added_edge_ids": [str(edge.get("edge_id")) for edge in (proposal.get("edges") or [])],
        "detached_edge_ids": sorted(str(item) for item in (proposal.get("detach_edges") or [])),
        "materialization": {
            "inherited_model": inherited_model(workflow, str(proposal["anchor_node_id"])),
            "filled_by_aaw": ["proposal_version", "workflow_id", "anchor_node_id",
                              "base_semantic_hash", "proposal_id", "node.model",
                              "node.on_pass", "node.on_fail"],
        },
    })
    return report


def is_stale(workflow: Mapping[str, Any], proposal: Mapping[str, Any]) -> bool:
    """True when the graph moved under this proposal since it was generated."""
    return routing_contract.semantic_hash(workflow) != str(proposal.get("base_semantic_hash"))


def proposal_summary(proposal: Mapping[str, Any]) -> dict[str, Any]:
    """A compact, non-executable description of one proposal, for the UX."""
    return {
        "proposal_id": proposal.get("proposal_id"),
        "proposal_version": proposal.get("proposal_version"),
        "workflow_id": proposal.get("workflow_id"),
        "anchor_node_id": proposal.get("anchor_node_id"),
        "base_semantic_hash": proposal.get("base_semantic_hash"),
        "intent": proposal.get("intent"),
        "node_count": len(proposal.get("nodes") or []),
        "edge_count": len(proposal.get("edges") or []),
        "detach_count": len(proposal.get("detach_edges") or []),
        "assumptions": list(proposal.get("assumptions") or []),
        "warnings": list(proposal.get("warnings") or []),
        "proposal_hash": proposal_hash(proposal),
    }


def public_contract() -> dict[str, Any]:
    """Machine-readable description of the proposal contract."""
    return {
        "contract": PROPOSAL_CONTRACT,
        "proposal_version": PROPOSAL_VERSION,
        "statuses": list(PROPOSAL_STATUSES),
        "event_types": list(PLANNER_EVENT_TYPES),
        "refusal_codes": list(REFUSAL_CODES),
        "node_types": list(PROPOSAL_NODE_TYPES),
        "roles": list(PROPOSAL_ROLES),
        "efforts": list(PROPOSAL_EFFORTS),
        "proposal_fields": list(PROPOSAL_FIELDS),
        "node_fields": list(PROPOSAL_NODE_FIELDS),
        "edge_fields": list(PROPOSAL_EDGE_FIELDS),
        "limits": ProposalLimits.from_env().as_dict(),
        "deferred": ["Modify a proposal before acceptance",
                     "replacements (rewriting an existing node)",
                     "automatic resolution of a stale proposal",
                     "three-way semantic graph merge",
                     "MACHINE_GATE proposals",
                     "planner-chosen model bindings",
                     "recursive or self-repairing planner loops"],
    }
