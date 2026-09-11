#!/usr/bin/env python3
"""AAW UX RUNTIME BRIDGE V0.1 — scripted substitute for the LLM adapter.

What this replaces and what it deliberately does not
----------------------------------------------------
It replaces exactly one function: `workflow_runner.execute_llm_node`, the
adapter boundary that module already documents as the provider seam, and the
same seam the existing test suite substitutes. Nothing else is faked.

Still entirely real when this adapter is installed:

  * the runner's frontier loop, limits and workspace guards;
  * every gate evaluation, every selected and held edge, every hold reason;
  * repair-branch minting, lineage, inherited brief and carry_forward;
  * `MACHINE_GATE` subprocesses, which are actually spawned;
  * execution identity, the execution ledger, the routing journal, gate
    decision artifacts and `workflow_state.json`;
  * cancellation, including child-process termination.

So a slice driven by this adapter exercises real routing and real runtime. The
only thing it does not exercise is a paid provider producing the structured
result — the script supplies that instead, and it is still validated through
`validate_node_result`, so a script cannot smuggle in a result the contract
would reject.

Script shape (JSON), keyed by node id or by REPAIR template id:

    {
      "schema_version": "AAW_SCRIPTED_ADAPTER_V0.1",
      "delay_seconds": 0.4,
      "nodes": {
        "N03": {"outcome": "FAIL", "verdict": "REPAIR",
                "write_files": {"partial.py": "unfinished = True\n"},
                "next_brief": "...", "carry_forward": ["..."],
                "artifacts": ["..."], "findings": [...]},
        "N03R": {"outcome": "PASS", "summary": "..."}
      }
    }

A minted branch (`N03A`) resolves against its own id first and then against
its template id, so one entry covers every branch minted from that template.
The reserved id `"*"`, when a script declares it, answers every node no other
entry covers; without it an unknown node is refused rather than invented.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Mapping

import run_cancellation
from execution_contract import update_execution
from workflow_schema import validate_node_result


SCRIPT_SCHEMA_VERSION = "AAW_SCRIPTED_ADAPTER_V0.1"
RESULT_KEYS = ("verdict", "next_brief", "carry_forward", "artifacts")
DEFAULT_DELAY_SECONDS = 0.0


class ScriptError(ValueError):
    """Fail-closed: an unusable script must not silently become a PASS."""


def load_script(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ScriptError(f"cannot read adapter script: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("nodes"), dict):
        raise ScriptError("adapter script needs an object with a `nodes` object")
    return data


def scripted_adapter(script: Mapping[str, Any], *,
                     captured: dict[str, Any] | None = None) -> Callable[..., Any]:
    """Build a substitute for `workflow_runner.execute_llm_node`.

    Signature matches the boundary exactly, including the optional recorder,
    so installing it changes no call site.
    """
    nodes: Mapping[str, Any] = script.get("nodes") or {}
    delay = float(script.get("delay_seconds", DEFAULT_DELAY_SECONDS))

    def adapter(workflow: Mapping[str, Any], state: Mapping[str, Any], node: Mapping[str, Any],
                worktree: Path, execution: Mapping[str, Any], execution_path: Path,
                recorder: Any = None) -> tuple[dict[str, Any], dict[str, Any]]:
        import workflow_runner  # local: avoids an import cycle at module load

        node_id = str(node["id"])
        lineage = node.get("lineage") or {}
        entry = nodes.get(node_id)
        if entry is None:
            entry = nodes.get(str(lineage.get("template_id") or ""))
        if entry is None:
            # A script may declare one explicit fallback under the reserved key
            # `*`. Refusing remains the default: the fallback exists so a
            # canvas-authored graph whose node ids nobody knew in advance can
            # be exercised, and it is a statement the script author made, not
            # an invention by this adapter.
            entry = nodes.get("*")
        if entry is None:
            raise ScriptError(
                f"adapter script has no entry for node {node_id!r} "
                f"(template {lineage.get('template_id')!r}); refusing to invent a result")

        if captured is not None:
            captured[node_id] = {
                "instructions": node.get("instructions"),
                "acceptance": list(node.get("acceptance") or []),
                "carry_forward": list(node.get("carry_forward") or []),
                "package": workflow_runner.node_package(workflow, state, node, worktree),
                "lineage": lineage or None,
                "binding": dict(state["workflow_bindings"].get(node_id, {})),
            }

        # Opt-in operator-trial side effect. Paths stay inside the supplied
        # isolated worktree and are explicit in the script; production
        # adapters are unaffected. Writing before the cancellable delay makes
        # partial-work recovery observable in a real browser trial.
        for relative, content in dict(entry.get("write_files") or {}).items():
            target = (Path(worktree).resolve() / str(relative)).resolve()
            try:
                target.relative_to(Path(worktree).resolve())
            except ValueError as exc:
                raise ScriptError(f"write_files path escapes worktree: {relative}") from exc
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(content), encoding="utf-8")

        # A visible, interruptible pause. Sliced so a Stop lands inside a node
        # rather than only between nodes: the point of the delay is to make
        # cancellation observable, so it must itself be cancellable.
        deadline = time.monotonic() + max(0.0, delay)
        while time.monotonic() < deadline:
            run_cancellation.check(f"scripted_adapter:{node_id}")
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        run_cancellation.check(f"scripted_adapter:{node_id}")

        update_execution(execution_path, str(execution["execution_id"]), status="COMPLETED")
        result: dict[str, Any] = {
            "execution_id": execution["execution_id"],
            "node_id": node_id, "node_type": str(node["type"]),
            "outcome": str(entry.get("outcome", "PASS")),
            "summary": str(entry.get("summary") or f"{node_id} scripted result"),
            "changed_files": list(entry.get("changed_files") or []),
            "tests": list(entry.get("tests") or []),
            "findings": list(entry.get("findings") or []),
            "remaining_uncertainty": list(entry.get("remaining_uncertainty") or []),
            "recommended_next_action": str(entry.get("recommended_next_action") or "the gate decides"),
        }
        for key in RESULT_KEYS:
            if key in entry:
                result[key] = entry[key]
        if str(node["type"]) == "REVIEW":
            for index, finding in enumerate(result["findings"], 1):
                finding.setdefault("finding_id", f"F{index:03d}")
                finding["finding_key"] = {"review_execution_id": execution["execution_id"],
                                          "finding_id": finding["finding_id"]}
        # The real adapter validates before returning; so does this one, so a
        # script cannot produce a result the contract would have refused.
        validate_node_result(result, node_id, str(node["type"]))

        telemetry = {
            "schema_version": "1.1", "execution_id": execution["execution_id"],
            "run_id": state["AAW_RUN_ID"], "node": node_id, "workflow_node_id": node_id,
            "workflow_node_type": str(node["type"]), "harness": "scripted",
            "model": "scripted", "effort": "n/a", "provider_session_id": None,
            "wall_time_s": round(max(0.0, delay), 3), "usage": {},
            "adapter": SCRIPT_SCHEMA_VERSION,
            "telemetry_status": "SYNTHETIC_NO_PROVIDER_CALL",
        }
        return result, telemetry

    return adapter
