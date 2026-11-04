"""AAW CHAIN MODE V1 - long implementation chains, one serious review per chain.

Pure helpers for the autonomy controller (no I/O, no model calls):

  * `normalize_config`  - the `chain` block of AUTONOMY_ROLES (validated, defaulted);
  * `review_mode_for`   - LIGHT inside a chain, CHAIN_CLOSE on its last iteration;
  * `relax_review`      - the severity policy: only findings that harm real behaviour
                          are repaired now, everything smaller goes to a backlog;
  * `defer`             - backlog records (deduplicated, never lost);
  * `build_polish_plan` - the controller-authored final polish iteration;
  * `stub_to_plan`      - a chain-plan stub (written once by the strong planner)
                          expanded into a normal iteration plan.

The philosophy in one paragraph: the strong planner thinks once for the whole
chain; implementation iterations run back to back, each guarded by deterministic
self-verification and (optionally) one cheap primary review that may only stop the
chain for a CRITICAL defect; at the end of the chain one serious review looks at
the whole diff, significant defects are repaired, small ones are recorded and
polished at the very end of the run. Nothing is silently dropped: every deferred
finding stays in `state["deferred_findings"]` and is shown at the Human Gate.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

import work_packet as wp

SCHEMA = "AAW_CHAIN_MODE_V1"
LIGHT, CLOSE, POLISH = "LIGHT", "CHAIN_CLOSE", "POLISH"
REVIEW_MODES = (LIGHT, CLOSE, POLISH)
RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}
FINAL_TIERS = ("DEFAULT", "HARD", "CRITICAL")

DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "length": 8,                      # iterations per chain; the last one carries the serious review
    "mid_chain_review": "PRIMARY",    # PRIMARY = one cheap review pass per iteration, NONE = self-verify only
    "mid_repair_min_severity": "CRITICAL",    # inside a chain only this severity (or failing checks) stops work
    "close_repair_min_severity": "HIGH",      # the serious review repairs HIGH and CRITICAL
    "close_final_floor": "HARD",      # lowest final-review tier at a chain close
    "plan_batching": True,            # the strong planner outlines the whole chain once (chain_plan)
    "polish": {"enabled": True, "max_findings": 12},
}


class ChainConfigError(ValueError):
    pass


def normalize_config(raw: Any) -> dict[str, Any]:
    """Validated config; disabled when absent. Unknown keys are rejected (typos must not pass silently)."""
    if raw is None or raw is False:
        return {**DEFAULTS, "polish": dict(DEFAULTS["polish"]), "enabled": False}
    if raw is True:
        raw = {"enabled": True}
    if not isinstance(raw, Mapping):
        raise ChainConfigError("chain must be an object")
    unknown = sorted(set(raw) - set(DEFAULTS) - {"_comment"})
    if unknown:
        raise ChainConfigError(f"unknown chain keys: {unknown}")
    cfg = {**DEFAULTS, "polish": dict(DEFAULTS["polish"])}
    cfg.update({k: v for k, v in raw.items() if k not in ("_comment", "polish")})
    if isinstance(raw.get("polish"), Mapping):
        cfg["polish"].update(raw["polish"])
    for key in ("enabled", "plan_batching"):
        if type(cfg[key]) is not bool:
            raise ChainConfigError(f"chain.{key} must be a boolean")
    if type(cfg["length"]) is not int or not 2 <= cfg["length"] <= 40:
        raise ChainConfigError("chain.length must be an integer in 2..40")
    if cfg["mid_chain_review"] not in ("PRIMARY", "NONE"):
        raise ChainConfigError("chain.mid_chain_review must be PRIMARY or NONE")
    if cfg["mid_repair_min_severity"] not in ("HIGH", "CRITICAL"):
        raise ChainConfigError("chain.mid_repair_min_severity must be HIGH or CRITICAL")
    if cfg["close_repair_min_severity"] not in ("MEDIUM", "HIGH"):
        raise ChainConfigError("chain.close_repair_min_severity must be MEDIUM or HIGH")
    if cfg["close_final_floor"] not in FINAL_TIERS:
        raise ChainConfigError(f"chain.close_final_floor must be one of {FINAL_TIERS}")
    polish = cfg["polish"]
    if set(polish) - {"enabled", "max_findings"} or type(polish.get("enabled")) is not bool \
            or type(polish.get("max_findings")) is not int or not 1 <= polish["max_findings"] <= 100:
        raise ChainConfigError("chain.polish must be {enabled: bool, max_findings: 1..100}")
    return cfg


def review_mode_for(cfg: Mapping[str, Any], position: int) -> str:
    """LIGHT for positions 1..length-1, CHAIN_CLOSE for the last position of a chain."""
    return CLOSE if position >= int(cfg["length"]) else LIGHT


def min_repair_severity(cfg: Mapping[str, Any], mode: str) -> str:
    return cfg["mid_repair_min_severity"] if mode == LIGHT else cfg["close_repair_min_severity"]


# -- severity policy ---------------------------------------------------------------------

def _key(finding: Mapping[str, Any]) -> str:
    return str(finding.get("finding_key") or f"{finding.get('severity')}::{finding.get('file')}::{finding.get('summary')}")


def relax_review(raw: Any, *, min_repair: str, honor_blocking_flag: bool) -> tuple[Any, list[dict[str, Any]], dict[str, Any]]:
    """Apply the chain severity policy to a raw reviewer result *before* `normalize_review`.

    Returns `(raw', deferred_findings, note)`. Findings below `min_repair` are removed from
    the result (so `normalize_review` cannot turn them back into blockers) and returned for
    the backlog. A REPAIR_REQUIRED verdict whose every finding was deferred becomes PASS.
    A reviewer's explicit `blocking: true` is honoured only when `honor_blocking_flag` (the
    serious review) and the finding is at least MEDIUM; mid-chain it never stops the chain.
    Malformed input is returned untouched - `normalize_review` already fails closed on it.
    """
    if not isinstance(raw, dict) or not isinstance(raw.get("findings", []), list) \
            or not all(isinstance(f, dict) for f in raw.get("findings", [])):
        return raw, [], {"relaxed": False}
    threshold = RANK[min_repair]
    kept: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    for finding in raw.get("findings", []):
        severity = str(finding.get("severity", "MEDIUM")).upper()
        severity = severity if severity in RANK else "MEDIUM"
        major = RANK[severity] >= threshold or (honor_blocking_flag and finding.get("blocking") is True
                                                 and RANK[severity] >= RANK["MEDIUM"])
        if major:
            kept.append({**finding, "severity": severity, "blocking": True})
        else:
            deferred.append({**finding, "severity": severity, "blocking": False})
    out = {**raw, "findings": kept}
    note: dict[str, Any] = {"relaxed": bool(deferred), "min_repair": min_repair, "deferred": len(deferred)}
    if raw.get("verdict") == "REPAIR_REQUIRED" and not kept:
        out["verdict"] = "PASS"
        note["verdict_relaxed_from"] = "REPAIR_REQUIRED"
        out["summary"] = (str(raw.get("summary") or "") + " [chain mode: nothing above the repair threshold; "
                          f"{len(deferred)} smaller finding(s) deferred to polish]").strip()
    return out, deferred, note


def defer(backlog: list[dict[str, Any]], findings: Sequence[Mapping[str, Any]], *, iteration_id: str | None,
          chain_id: int | None, origin: str, at: str) -> list[dict[str, Any]]:
    """Append findings to the backlog, deduplicating by (finding_key, file). Returns the new records."""
    seen = {(r.get("finding_key"), r.get("file")) for r in backlog}
    added = []
    for finding in findings:
        key = _key(finding)
        if (key, finding.get("file")) in seen:
            continue
        seen.add((key, finding.get("file")))
        row = {"finding_key": key, "severity": str(finding.get("severity", "MEDIUM")).upper(),
               "summary": str(finding.get("summary") or "(no summary)"), "file": finding.get("file"),
               "evidence_ref": finding.get("evidence_ref"), "iteration_id": iteration_id, "chain_id": chain_id,
               "origin": origin, "status": "OPEN", "at": at}
        backlog.append(row)
        added.append(row)
    return added


def open_backlog(backlog: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [dict(r) for r in backlog if r.get("status") == "OPEN"]


# -- polish ---------------------------------------------------------------------------------

def _area_ok(path: str | None, allowed: Sequence[str] | None, forbidden: Sequence[str]) -> bool:
    if not path:
        return True
    norm = path.replace("\\", "/").lstrip("./")

    def match(areas: Sequence[str]) -> bool:
        return any(norm == a.rstrip("/").lstrip("./") or norm.startswith(a.rstrip("/").lstrip("./") + "/") for a in areas)

    return (allowed is None or match(allowed)) and not match(forbidden)


def build_polish_plan(mandate: Mapping[str, Any], backlog: Sequence[Mapping[str, Any]], *, charter_hash: str | None,
                      max_findings: int) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[dict[str, Any]]]:
    """The final polish iteration, authored by the controller (no planner call).

    Returns `(plan | None, selected, out_of_scope)`. The mandate's allowed/forbidden areas still
    apply: a finding outside them is reported as out of scope, never polished. Highest severity
    first, then oldest first.
    """
    bounds = mandate["roadmap_mandate"]["autonomy_bounds"]
    allowed, forbidden = bounds.get("allowed_areas"), list(bounds.get("forbidden_areas", []))
    candidates = sorted(open_backlog(backlog), key=lambda r: (-RANK.get(r.get("severity"), 1), str(r.get("at"))))
    in_scope = [r for r in candidates if _area_ok(r.get("file"), allowed, forbidden)]
    out_of_scope = [r for r in candidates if r not in in_scope]
    selected = in_scope[:max_findings]
    if not selected:
        return None, [], out_of_scope
    criteria = [f"[{r['finding_key']}] {r['severity']}: {r['summary']}"
                + (f" ({r['file']})" if r.get("file") else "") for r in selected]
    criteria.append("every item above is fixed, or reported as deliberately left with a one-line reason")
    criteria.append("no change of behaviour, public interface or scope; every recorded check still passes")
    files = sorted({r["file"] for r in selected if r.get("file")})
    packet = _polish_packet(selected, files)
    plan = {"status": "ITERATION", "mandate_hash": mandate["mandate_hash"], "directional_charter_hash": charter_hash,
            "goal": "Polish: resolve the minor findings deferred during the implementation chains "
                    "(cosmetics, naming, small edge cases, docs) without changing behaviour or scope",
            "roadmap_refs": [], "scope_justification": "closing polish pass over findings recorded by earlier reviews; "
                                                         "adds no roadmap work",
            "acceptance_criteria": criteria, "touched_areas": files, "decisions": [], "skipped_items": [],
            "implementation_complexity": "NORMAL", "complexity_evidence": [],
            "semantic_verification_required": False, "semantic_verification_reason": None,
            "polish_findings": [r["finding_key"] for r in selected], "work_packet": packet}
    return plan, selected, out_of_scope


def _polish_packet(selected: Sequence[Mapping[str, Any]], files: Sequence[str]) -> dict[str, Any]:
    """A work packet for the polish pass, built from the recorded findings (no planner call, no invented paths)."""
    steps = [{"step_id": f"P{n}", "action": f"Resolve finding {r['finding_key']} ({r['severity']})",
              "files": [r["file"]] if r.get("file") else [], "details": str(r["summary"]),
              "verify": "re-read the change and re-run the recorded checks; or note a one-line reason for leaving it"}
             for n, r in enumerate(selected, 1)]
    return {"files_to_read": list(files), "files_to_change": list(files), "steps": steps, "verification_commands": [],
            "definition_of_done": [f"{r['finding_key']} is fixed, or left with a one-line reason" for r in selected]
                                  + ["every check recorded earlier in the run still passes"],
            "pitfalls": ["do not change behaviour, public interfaces or scope", "do not touch files no finding names",
                         "a finding that no longer reproduces is closed without a change"],
            "out_of_scope": ["new features", "refactors that are not needed to resolve a listed finding"],
            "needs_strong_implementer": False, "strong_implementer_reason": None}


# -- chain plan -------------------------------------------------------------------------------

STUB_KEYS = ("goal", "roadmap_refs", "scope_justification", "acceptance_criteria", "touched_areas",
             "implementation_complexity", "complexity_evidence", "work_packet")
STALE_PACKET_NOTE = ("this packet was drafted before the earlier iterations of the chain ran: re-read each file before "
                     "editing it, and where it disagrees with the repository as it is now, follow the repository")


def valid_stub(stub: Any) -> bool:
    return (isinstance(stub, Mapping)
            and isinstance(stub.get("goal"), str) and stub["goal"].strip()
            and isinstance(stub.get("scope_justification"), str) and stub["scope_justification"].strip()
            and isinstance(stub.get("roadmap_refs"), list) and stub["roadmap_refs"]
            and all(isinstance(r, str) for r in stub["roadmap_refs"])
            and isinstance(stub.get("acceptance_criteria"), list) and stub["acceptance_criteria"]
            and all(isinstance(c, str) and c.strip() for c in stub["acceptance_criteria"])
            and isinstance(stub.get("touched_areas"), list) and all(isinstance(a, str) for a in stub["touched_areas"])
            and stub.get("implementation_complexity", "NORMAL") in ("NORMAL", "HARDER", "SIGNIFICANTLY_DIFFICULT")
            and isinstance(stub.get("complexity_evidence", []), list)
            and (stub.get("work_packet") is None or isinstance(stub.get("work_packet"), Mapping)))


def clean_stubs(stubs: Any, limit: int) -> list[dict[str, Any]]:
    """Keep the valid stubs (in order, at most `limit`) of a planner's chain_plan."""
    if not isinstance(stubs, list):
        return []
    out = []
    for stub in stubs:
        if not valid_stub(stub):
            break          # order matters: a broken stub makes everything after it untrustworthy
        row = {k: stub.get(k) for k in STUB_KEYS}
        row["work_packet"] = _clean_packet(stub.get("work_packet"))
        out.append(row)
        if len(out) >= limit:
            break
    return out


def _clean_packet(packet: Any) -> dict[str, Any] | None:
    """Keep only the work-packet fields and mark the packet as drafted ahead of time; None if there is none."""
    if not isinstance(packet, Mapping):
        return None
    out = {k: packet.get(k) for k in wp.WORK_PACKET_SCHEMA["required"]}
    for key in ("files_to_read", "files_to_change", "definition_of_done", "pitfalls", "out_of_scope"):
        out[key] = [str(x) for x in out[key]] if isinstance(out[key], list) else []
    out["steps"] = [s for s in out["steps"] if isinstance(s, Mapping)] if isinstance(out["steps"], list) else []
    out["verification_commands"] = ([c for c in out["verification_commands"] if isinstance(c, Mapping)]
                                    if isinstance(out["verification_commands"], list) else [])
    out["needs_strong_implementer"] = out["needs_strong_implementer"] is True
    if STALE_PACKET_NOTE not in out["pitfalls"]:
        out["pitfalls"] = out["pitfalls"] + [STALE_PACKET_NOTE]
    return out


def stub_to_plan(stub: Mapping[str, Any], *, mandate_hash: str, charter_hash: str | None) -> dict[str, Any]:
    plan = _stub_plan(stub, mandate_hash=mandate_hash, charter_hash=charter_hash)
    if stub.get("work_packet"):           # no packet key at all stays a legacy plan, which is not penalised
        plan["work_packet"] = _clean_packet(stub["work_packet"])
    return plan


def _stub_plan(stub: Mapping[str, Any], *, mandate_hash: str, charter_hash: str | None) -> dict[str, Any]:
    return {"status": "ITERATION", "mandate_hash": mandate_hash, "directional_charter_hash": charter_hash,
            "goal": stub["goal"], "roadmap_refs": list(stub["roadmap_refs"]),
            "scope_justification": stub["scope_justification"],
            "acceptance_criteria": list(stub["acceptance_criteria"]), "touched_areas": list(stub["touched_areas"]),
            "decisions": [], "skipped_items": [],
            "implementation_complexity": stub.get("implementation_complexity") or "NORMAL",
            "complexity_evidence": list(stub.get("complexity_evidence") or []),
            "semantic_verification_required": False, "semantic_verification_reason": None,
            "working_roadmap": None, "next_recommended_step": None}
