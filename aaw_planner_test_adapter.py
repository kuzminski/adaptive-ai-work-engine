#!/usr/bin/env python3
"""AAW PLANNER PROPOSAL PIPELINE V0.1 — scripted substitute for the planner.

Replaces exactly one function: `aaw_planner.provider_planner`, the documented
provider seam. Nothing else is faked. Still entirely real when this adapter is
installed: the planning package and its hash, normalization, the whole
deterministic validator, the real `workflow_schema.validate_workflow` behind
it, proposal identity, staleness, accept/reject, the bridge's refusals and the
validated save path.

Deliberately, this adapter can also return output that is *wrong* — malformed
JSON shapes, duplicate ids, oversized graphs, an unsupported node type. That is
the point: the pipeline's job is to refuse untrusted planner output, and a
substitute that could only produce valid proposals would never exercise it.

Script shape (JSON), keyed by anchor node id:

    {
      "schema_version": "AAW_SCRIPTED_PLANNER_V0.1",
      "delay_seconds": 0.0,
      "anchors": {
        "N05": {"intent": "...", "nodes": [...], "edges": [...]},
        "*":   {"fail": "PLANNER_DISPATCH_FAILED", "message": "..."}
      }
    }

An entry containing `fail` raises `aaw_planner.PlannerError` with that code,
so provider failure and planner timeout are exercised through the same seam a
real failure arrives on. The reserved key `"*"` answers any anchor no other
entry covers; without it an unknown anchor is refused rather than invented.
"""

from __future__ import annotations

import copy
import json
import time
from pathlib import Path
from typing import Any, Callable, Mapping

import aaw_planner


SCRIPT_SCHEMA_VERSION = "AAW_SCRIPTED_PLANNER_V0.1"


class PlannerScriptError(ValueError):
    """Fail-closed: an unusable script must not silently become a proposal."""


def load_script(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlannerScriptError(f"cannot read planner script: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("anchors"), dict):
        raise PlannerScriptError("planner script needs an object with an `anchors` object")
    return data


def scripted_planner(script: Mapping[str, Any], *,
                     captured: dict[str, Any] | None = None) -> Callable[..., Any]:
    """Build a substitute for `aaw_planner.provider_planner`.

    Signature matches the seam, including the keyword arguments the real
    adapter accepts, so installing it changes no call site.
    """
    anchors: Mapping[str, Any] = script.get("anchors") or {}
    delay = float(script.get("delay_seconds", 0.0))

    def adapter(package: Mapping[str, Any], **kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        anchor_id = str((package.get("anchor") or {}).get("node_id"))
        entry = anchors.get(anchor_id)
        if entry is None:
            entry = anchors.get("*")
        if entry is None:
            raise aaw_planner.PlannerError(
                aaw_planner.PLANNER_DISPATCH_FAILED,
                f"planner script has no entry for anchor {anchor_id!r}; "
                f"refusing to invent a proposal")
        if captured is not None:
            captured[anchor_id] = {"package": copy.deepcopy(dict(package)),
                                   "kwargs": dict(kwargs)}
        if delay > 0:
            time.sleep(delay)
        if entry.get("fail"):
            raise aaw_planner.PlannerError(str(entry["fail"]),
                                           str(entry.get("message") or "scripted planner failure"))
        if "raw" in entry:
            # Verbatim, any JSON type. Exists so the pipeline's "planner output
            # is not an object at all" refusal can be exercised through the
            # same seam as a well-formed answer.
            raw = copy.deepcopy(entry["raw"])
        else:
            raw = copy.deepcopy({key: value for key, value in dict(entry).items()
                                 if key not in ("fail", "message")})
        telemetry = {
            "schema_version": "AAW_PLANNER_TELEMETRY_V0.1",
            "invocation": "PLANNER_PROPOSAL",
            "harness": "scripted", "provider": "scripted",
            "profile": "SCRIPTED", "model": "scripted", "effort": "n/a",
            "access_class": "NONE", "provider_session_id": None,
            "wall_time_s": round(delay, 3), "usage": {},
            "adapter": SCRIPT_SCHEMA_VERSION,
            "telemetry_status": "SYNTHETIC_NO_PROVIDER_CALL",
        }
        return raw, telemetry

    return adapter
