#!/usr/bin/env python3
"""AAW UX RUNTIME BRIDGE V0.1 — visual layout, stored apart from semantics.

Node coordinates, viewport and collapsed state are properties of *a view of* a
workflow, not of the workflow. They therefore live in their own files, keyed by
workflow identity, and never enter the workflow definition:

    WORKFLOWS/LAYOUTS/<workflow_id>.layout.json

Two independent guarantees, both tested:

  1. This module writes only under the layout root. It is given no path into a
     workflow file and holds no workflow writer, so saving a layout cannot
     modify executable semantics or the semantic hash.
  2. The stored shape is a strict whitelist of numbers and booleans. A layout
     cannot carry instructions, edges, predicates or any other semantic field,
     so a round-trip through the layout store can never smuggle graph meaning.

`workflow_semantic_hash` is recorded at save time as *provenance*, not as a
lock: a layout authored against an older graph is still the user's layout, and
the UX is told the graph moved rather than having its positions discarded.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path
from typing import Any, Mapping


LAYOUT_SCHEMA_VERSION = "AAW_WORKFLOW_LAYOUT_V0.1"

# Everything a layout may say about one node. Purely presentational.
NODE_LAYOUT_NUMBERS = ("x", "y", "w", "h")
NODE_LAYOUT_FLAGS = ("collapsed", "pinned")
VIEWPORT_NUMBERS = ("x", "y", "k")

_WORKFLOW_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class LayoutError(ValueError):
    """Fail-closed layout-store violation."""

    classification = "LAYOUT_ERROR"


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise LayoutError(message)


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="milliseconds")


def _number(value: Any, label: str) -> float:
    _require(isinstance(value, (int, float)) and not isinstance(value, bool),
             f"{label} must be a number")
    _require(value == value and abs(float(value)) < 1e9, f"{label} must be finite")
    return float(value)


def layout_root(workflows_root: Path) -> Path:
    return Path(workflows_root) / "LAYOUTS"


def layout_path(workflows_root: Path, workflow_id: str) -> Path:
    """Path of one workflow's layout. Refuses anything that is not a plain id.

    The workflow id arrives from a client, so it is validated as an identifier
    rather than trusted as a path component.
    """
    _require(bool(_WORKFLOW_ID.match(str(workflow_id))), f"unsafe workflow id {workflow_id!r}")
    return layout_root(workflows_root) / f"{workflow_id}.layout.json"


def empty_layout(workflow_id: str) -> dict[str, Any]:
    return {
        "schema_version": LAYOUT_SCHEMA_VERSION, "workflow_id": str(workflow_id),
        "workflow_semantic_hash": None, "updated_at": None,
        "viewport": None, "nodes": {}, "exists": False,
    }


def normalize_layout(workflow_id: str, layout: Mapping[str, Any], *,
                     workflow_semantic_hash: str | None = None) -> dict[str, Any]:
    """Validate and reduce a client layout to the stored whitelist.

    Unknown keys are dropped rather than rejected: a newer canvas sending a
    field this version does not store must not be unable to save its
    positions. Known keys with wrong types *are* rejected, because silently
    storing a coordinate as a string would break the next reader.
    """
    _require(isinstance(layout, Mapping), "layout must be an object")
    nodes_in = layout.get("nodes") or {}
    _require(isinstance(nodes_in, Mapping), "layout.nodes must be an object keyed by node id")

    nodes: dict[str, dict[str, Any]] = {}
    for node_id, entry in nodes_in.items():
        _require(isinstance(entry, Mapping), f"layout.nodes[{node_id!r}] must be an object")
        row: dict[str, Any] = {}
        for key in NODE_LAYOUT_NUMBERS:
            if entry.get(key) is not None:
                row[key] = _number(entry[key], f"layout.nodes[{node_id!r}].{key}")
        for key in NODE_LAYOUT_FLAGS:
            if entry.get(key) is not None:
                _require(isinstance(entry[key], bool), f"layout.nodes[{node_id!r}].{key} must be a boolean")
                row[key] = bool(entry[key])
        _require("x" in row and "y" in row, f"layout.nodes[{node_id!r}] needs x and y")
        nodes[str(node_id)] = row

    viewport = None
    if layout.get("viewport") is not None:
        raw = layout["viewport"]
        _require(isinstance(raw, Mapping), "layout.viewport must be an object")
        viewport = {key: _number(raw[key], f"layout.viewport.{key}")
                    for key in VIEWPORT_NUMBERS if raw.get(key) is not None}

    return {
        "schema_version": LAYOUT_SCHEMA_VERSION,
        "workflow_id": str(workflow_id),
        "workflow_semantic_hash": str(workflow_semantic_hash) if workflow_semantic_hash else None,
        "updated_at": _now(),
        "viewport": viewport,
        "nodes": nodes,
    }


def load_layout(workflows_root: Path, workflow_id: str) -> dict[str, Any]:
    path = layout_path(workflows_root, workflow_id)
    if not path.is_file():
        return empty_layout(workflow_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LayoutError(f"cannot read layout for {workflow_id}: {exc}") from exc
    _require(isinstance(data, dict), "stored layout is not an object")
    data["exists"] = True
    data.setdefault("nodes", {})
    return data


def save_layout(workflows_root: Path, workflow_id: str, layout: Mapping[str, Any], *,
                workflow_semantic_hash: str | None = None) -> dict[str, Any]:
    """Persist a layout atomically. Touches no workflow file, by construction."""
    normalized = normalize_layout(workflow_id, layout, workflow_semantic_hash=workflow_semantic_hash)
    path = layout_path(workflows_root, workflow_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(normalized, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temp, path)
    return {**normalized, "exists": True, "path": str(path)}


def delete_layout(workflows_root: Path, workflow_id: str) -> bool:
    path = layout_path(workflows_root, workflow_id)
    if not path.is_file():
        return False
    path.unlink()
    return True


# ───────────────────────────── first-render defaults ─────────────────────────

COLUMN = 296
ROW = 168
BRANCH_ROW = 280


def auto_layout(projection: Mapping[str, Any]) -> dict[str, Any]:
    """Deterministic default positions for a graph nobody has arranged yet.

    Longest-path depth for the column, declaration order for the row. Only a
    starting point: the moment a human moves a node, the stored layout wins
    and this is never consulted again for that workflow.
    """
    nodes = list(projection.get("nodes") or [])
    edges = list(projection.get("edges") or [])
    order = [str(node["node_id"]) for node in nodes]
    position = {node_id: index for index, node_id in enumerate(order)}
    outgoing: dict[str, list[str]] = {node_id: [] for node_id in order}
    for edge in edges:
        source, target = str(edge.get("from")), str(edge.get("to"))
        if source in outgoing and target in position:
            outgoing[source].append(target)

    depth = {node_id: 0 for node_id in order}
    # Declaration order plus |nodes| relaxation passes settles the longest path
    # on any DAG without needing a topological sort, and terminates on a cycle.
    for _ in range(len(order)):
        changed = False
        for node_id in order:
            for target in outgoing[node_id]:
                if depth[target] < depth[node_id] + 1:
                    depth[target] = depth[node_id] + 1
                    changed = True
        if not changed:
            break

    # A repair template is never entered, so it must not take a lane in the
    # main flow or it would read as the next task. Templates are placed below
    # their column instead, and the nodes that do run keep the top lanes.
    templates = {str(node["node_id"]) for node in nodes if node.get("is_repair_template")}
    lanes: dict[int, int] = {}
    branches: dict[int, int] = {}
    placed: dict[str, dict[str, Any]] = {}
    for node_id in order:
        column = depth[node_id]
        if node_id in templates:
            index = branches.get(column, 0)
            branches[column] = index + 1
            placed[node_id] = {"x": float(column * COLUMN),
                               "y": float(BRANCH_ROW + index * ROW)}
            continue
        lane = lanes.get(column, 0)
        lanes[column] = lane + 1
        placed[node_id] = {"x": float(column * COLUMN), "y": float(lane * ROW)}

    return {
        "schema_version": LAYOUT_SCHEMA_VERSION,
        "workflow_id": projection.get("workflow_id"),
        "workflow_semantic_hash": projection.get("semantic_hash"),
        "updated_at": None, "viewport": None, "nodes": placed,
        "exists": False, "source": "AUTO_LAYOUT",
    }


def place_minted_node(layout: Mapping[str, Any], node_id: str, origin_node_id: str | None,
                      branch_index: int = 1) -> dict[str, float]:
    """Where to put a node the runtime created after the layout was authored.

    Offset from its origin so a repair branch appears next to the review that
    minted it. Returned, not stored: a runtime-minted position is a rendering
    decision, and persisting it would make a run mutate a BUILD artifact.
    """
    nodes = layout.get("nodes") or {}
    origin = nodes.get(str(origin_node_id)) if origin_node_id else None
    base_x = float(origin.get("x", 0.0)) if isinstance(origin, Mapping) else 0.0
    base_y = float(origin.get("y", 0.0)) if isinstance(origin, Mapping) else 0.0
    # One column across from the origin and below the template band, so a
    # minted branch never lands on top of the template it was minted from and
    # a second branch never lands on top of the first.
    return {"x": base_x + COLUMN,
            "y": base_y + BRANCH_ROW + ROW * max(1, int(branch_index))}
