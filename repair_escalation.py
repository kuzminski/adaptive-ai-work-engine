#!/usr/bin/env python3
"""AAW REPAIR ESCALATION V0.1 — bounded automatic escalation of failed REPAIRs.

The lifecycle is unchanged (… REVIEW → REPAIR → SELF_VERIFY → … → FINAL_REVIEW).
What changes is what happens when a finding survives a REPAIR. Previously a
single repair that left the same finding keys in place stopped the run at the
Human Gate (REPAIR_NO_PROGRESS). That conflated "the finding text did not
change" with "nothing more can be done automatically". Now a surviving finding
climbs a configured, bounded ladder, and only an exhausted ladder (or a real
need for a human) reaches the Human Gate:

    CURRENT                 the repairer the policy chose
    EFFORT_UP               one effort level higher, re-diagnose then repair
    DIFFICULT_IMPLEMENTER   a stronger implementer: DIAGNOSE first, then REPAIR
    PLANNER_DIAGNOSIS       the planner as a last automatic analysis
    -> Human Gate only after the ladder is exhausted

Roles, effort ladder and stages are configuration (AUTONOMY_ROLES.json
`repair_escalation`); no model name appears in this module.

"Progress" is evidence-based and does not depend on the number of changed
files. A repair that needs no product change (re-ran a test, supplied missing
evidence, proved a baseline failure or an environment limit, fixed evidence
propagation) is progress and never needs an artificial diff.

Pure functions only: no clock, no filesystem, no model calls.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any, Iterable, Mapping, Sequence

VERSION = "AAW_REPAIR_ESCALATION_V0.1"

STAGE_CURRENT = "CURRENT"
STAGE_EFFORT_UP = "EFFORT_UP"
STAGE_DIFFICULT = "DIFFICULT_IMPLEMENTER"
STAGE_PLANNER = "PLANNER_DIAGNOSIS"
STAGES = (STAGE_CURRENT, STAGE_EFFORT_UP, STAGE_DIFFICULT, STAGE_PLANNER)

MODE_REPAIR = "REPAIR"
MODE_DIAGNOSE_THEN_REPAIR = "DIAGNOSE_THEN_REPAIR"

# progress signals
S_RESOLVED = "FINDING_RESOLVED"
S_TEST = "NEW_TEST_RESULT"
S_EVIDENCE = "NEW_EVIDENCE"
S_DIAGNOSIS = "BETTER_DIAGNOSIS"
S_RECLASSIFIED = "EVIDENCE_BACKED_RECLASSIFICATION"
S_BASELINE = "PRE_EXISTING_BASELINE_CONFIRMED"
S_ENVIRONMENT = "ENVIRONMENTAL_LIMITATION_CONFIRMED"
S_PROCESS = "PROCESS_REPAIRED"
S_SUPERSEDED = "STALE_EVIDENCE_SUPERSEDED"

# evidence classifications (the executor proposes, the controller validates)
C_BASELINE = "PRE_EXISTING_BASELINE"
C_ENVIRONMENT = "ENVIRONMENTAL_LIMITATION"
C_SUPERSEDED = "SUPERSEDED_BY_NEWER_CHECK"
CLASSIFICATIONS = (C_BASELINE, C_ENVIRONMENT, C_SUPERSEDED)
# evidence-state statuses a validated classification produces; none is FAIL/ERROR,
# all remain visible to reviewers as adverse items.
STATUS_BY_CLASSIFICATION = {C_BASELINE: "BASELINE_FAILURE", C_ENVIRONMENT: "ENVIRONMENTAL_LIMITATION",
                            C_SUPERSEDED: "SUPERSEDED"}
CLASSIFIED_STATUSES = frozenset(STATUS_BY_CLASSIFICATION.values())

DISP_RESOLVED = "RESOLVED"
DISP_PENDING = "PENDING"
DISP_HUMAN_GATE = "HUMAN_GATE"

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "stages": list(STAGES),
    "max_effort_steps": 1,          # "one level higher", then the difficult implementer
    "max_attempts_per_stage": 2,    # progress earns another try on the same stage, never an unbounded loop
    "effort_ladder": [],            # profile IDs ordered by effort, low -> high (same model family)
    "roles": {},                    # default_implementer / difficult_implementer / planner / reviewer / final_reviewer
}


class EscalationError(ValueError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise EscalationError(message)


def normalize_config(raw: Mapping[str, Any] | None, *, known_profiles: Iterable[str] | None = None) -> dict[str, Any]:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    for key, value in (raw or {}).items():
        cfg[key] = copy.deepcopy(value)
    _require(isinstance(cfg["enabled"], bool), "repair_escalation.enabled must be a boolean")
    _require(isinstance(cfg["stages"], list) and cfg["stages"] and all(s in STAGES for s in cfg["stages"]),
             f"repair_escalation.stages must be a non-empty subset of {list(STAGES)}")
    _require(len(set(cfg["stages"])) == len(cfg["stages"]), "repair_escalation.stages must not repeat a stage")
    _require(cfg["stages"][0] == STAGE_CURRENT, "repair_escalation.stages must start with CURRENT")
    for key in ("max_effort_steps", "max_attempts_per_stage"):
        _require(type(cfg[key]) is int and 0 <= cfg[key] <= 6 and (key != "max_attempts_per_stage" or cfg[key] >= 1),
                 f"repair_escalation.{key} must be a small non-negative integer")
    _require(isinstance(cfg["effort_ladder"], list) and all(isinstance(x, str) and x for x in cfg["effort_ladder"]),
             "repair_escalation.effort_ladder must be a list of profile IDs")
    _require(isinstance(cfg["roles"], dict) and all(isinstance(v, str) and v for v in cfg["roles"].values()),
             "repair_escalation.roles must map role names to profile IDs")
    if known_profiles is not None:
        known = set(known_profiles)
        unknown = sorted({*cfg["effort_ladder"], *cfg["roles"].values()} - known)
        _require(not unknown, f"repair_escalation references unknown profiles: {unknown}")
    return cfg


# ── the ladder ───────────────────────────────────────────────────────────────

def new_ladder(keys: Sequence[str]) -> dict[str, Any]:
    return {"stage": None, "attempts_on_stage": 0, "effort_steps": 0, "keys": sorted(keys), "last_profile_id": None,
            "step": None}


def _next_effort_profile(cfg: Mapping[str, Any], profile_id: str | None) -> str | None:
    ladder = cfg["effort_ladder"]
    if profile_id in ladder:
        index = ladder.index(profile_id)
        return ladder[index + 1] if index + 1 < len(ladder) else None
    return None


def _step(stage: str, profile_id: str | None, mode: str, reason: str, *, diagnose_profile_id: str | None = None,
          role: str | None = None, note: str | None = None) -> dict[str, Any]:
    return {"action": "STEP", "stage": stage, "profile_id": profile_id, "mode": mode, "reason": reason,
            "diagnose_profile_id": diagnose_profile_id, "role": role, "note": note}


def next_step(cfg: Mapping[str, Any], ladder: dict[str, Any], *, progressed: bool, last_profile_id: str | None,
              diagnose_available: bool = False) -> dict[str, Any]:
    """Advance `ladder` (mutated) to the next attempt, or report EXHAUSTED.

    `progressed` is the assessment of the previous attempt. Progress earns a
    retry on the same stage (bounded by `max_attempts_per_stage`); no progress
    climbs. The first call (`ladder["stage"] is None`) yields CURRENT.
    """
    ladder["last_profile_id"] = last_profile_id or ladder.get("last_profile_id")
    stages = list(cfg["stages"])
    if ladder["stage"] is None:
        step = _step(STAGE_CURRENT, None, MODE_REPAIR, "INITIAL_REPAIR", role="repairer")
        ladder.update(stage=STAGE_CURRENT, attempts_on_stage=1, step=step)
        return dict(step)
    stage = ladder["stage"]
    if progressed and ladder["attempts_on_stage"] < cfg["max_attempts_per_stage"]:
        ladder["attempts_on_stage"] += 1
        return {**ladder["step"], "reason": "PROGRESS_RETRY_SAME_STAGE"}
    order = stages[stages.index(stage) + 1:] if stage in stages else []
    if stage == STAGE_EFFORT_UP and STAGE_EFFORT_UP in stages:
        order.insert(0, STAGE_EFFORT_UP)          # may repeat while the effort budget lasts
    for target in order:
        step = _try_stage(cfg, ladder, target, diagnose_available)
        if step is not None:
            ladder.update(stage=target, attempts_on_stage=1, step=step)
            if target == STAGE_EFFORT_UP:
                ladder["effort_steps"] += 1
            return dict(step)
    return {"action": "EXHAUSTED", "stage": stage, "reason": "LADDER_EXHAUSTED",
            "stages_tried": stages[:stages.index(stage) + 1] if stage in stages else stages}


def _try_stage(cfg: Mapping[str, Any], ladder: Mapping[str, Any], stage: str,
               diagnose_available: bool) -> dict[str, Any] | None:
    roles, last = cfg["roles"], ladder.get("last_profile_id")
    reason = "NO_PROGRESS_ESCALATION"
    if stage == STAGE_EFFORT_UP:
        if ladder["effort_steps"] >= cfg["max_effort_steps"]:
            return None
        if not cfg["effort_ladder"]:
            # Nothing to climb: the remaining lever is a fresh diagnosis on the same profile.
            return _step(stage, last, MODE_DIAGNOSE_THEN_REPAIR, reason, role="repairer",
                         note="EFFORT_LADDER_NOT_CONFIGURED")
        higher = _next_effort_profile(cfg, last)
        # already at the top (or off the ladder): skip straight to the next stage
        return _step(stage, higher, MODE_DIAGNOSE_THEN_REPAIR, reason, role="repairer") if higher else None
    if stage == STAGE_DIFFICULT:
        profile = roles.get("difficult_implementer")
        if not profile:
            return None
        # DIAGNOSE first when a read-only diagnose executor exists; otherwise the repair call itself
        # must state a root cause before editing (MODE_DIAGNOSE_THEN_REPAIR).
        return _step(stage, profile, MODE_DIAGNOSE_THEN_REPAIR, reason, role="repairer",
                     diagnose_profile_id=profile if diagnose_available else None)
    if stage == STAGE_PLANNER:
        planner = roles.get("planner")
        if not planner or not diagnose_available:
            return None
        return _step(stage, roles.get("difficult_implementer") or last, MODE_DIAGNOSE_THEN_REPAIR, reason,
                     role="repairer", diagnose_profile_id=planner)
    return None


# ── progress ─────────────────────────────────────────────────────────────────

def _h(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


def validate_reclassifications(rows: Any, *, failing_names: Iterable[str], passing_names: Iterable[str] = ()
                               ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Accept only evidence-backed, acceptance-neutral reclassifications of a currently failing check.

    Returns (accepted, rejected). A rejection is recorded, never silently
    dropped, and never changes an evidence status.
    """
    failing, passing = set(failing_names), set(passing_names)
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, Mapping):
            rejected.append({"row": row, "why": "NOT_AN_OBJECT"})
            continue
        name = str(row.get("check_name") or "").strip()
        classification = str(row.get("classification") or "").upper()
        why = None
        if classification not in CLASSIFICATIONS:
            why = "UNKNOWN_CLASSIFICATION"
        elif name not in failing:
            why = "CHECK_NOT_CURRENTLY_FAILING"
        elif not str(row.get("evidence_ref") or "").strip():
            why = "EVIDENCE_REF_REQUIRED"
        elif not str(row.get("explanation") or "").strip():
            why = "EXPLANATION_REQUIRED"
        elif str(row.get("acceptance_impact") or "").upper() != "NONE":
            why = "ACCEPTANCE_IMPACT_NOT_NONE"      # a failure that touches an acceptance criterion is not waivable
        elif classification == C_SUPERSEDED and str(row.get("superseded_by") or "") not in passing:
            why = "SUPERSEDING_CHECK_NOT_PASSING"
        if why:
            rejected.append({"check_name": name, "classification": classification, "why": why})
        else:
            accepted.append({"check_name": name, "classification": classification,
                             "status": STATUS_BY_CLASSIFICATION[classification],
                             "evidence_ref": str(row["evidence_ref"]).strip(),
                             "explanation": str(row["explanation"]).strip()[:1000],
                             "superseded_by": row.get("superseded_by") if classification == C_SUPERSEDED else None})
    return accepted, rejected


def repair_signals(result: Mapping[str, Any], *, evidence_before: Mapping[str, str],
                   seen_check_triples: Iterable[str], seen_evidence_refs: Iterable[str],
                   seen_diagnosis_hashes: Iterable[str]) -> dict[str, Any]:
    """Progress signals visible from one repair result (the "finding resolved" signal needs the next review)."""
    signals: list[str] = []
    detail: dict[str, Any] = {}
    seen_triples, seen_refs, seen_dx = set(seen_check_triples), set(seen_evidence_refs), set(seen_diagnosis_hashes)
    checks = [c for c in (result.get("checks") or []) if isinstance(c, Mapping)]
    new_triples = []
    for check in checks:
        triple = _h([check.get("name"), str(check.get("status", "")).upper(), check.get("summary")])
        if triple not in seen_triples:
            new_triples.append(triple)
    if new_triples:
        signals.append(S_TEST)
        detail["new_check_results"] = len(new_triples)
    refs = [str(r) for r in (result.get("evidence_refs") or []) if str(r).strip()]
    refs += [str(c["log_ref"]) for c in checks if c.get("log_ref")]
    fresh_refs = sorted({r for r in refs if r not in seen_refs})
    if fresh_refs:
        signals.append(S_EVIDENCE)
        detail["new_evidence_refs"] = fresh_refs
    diagnosis = result.get("diagnosis")
    if isinstance(diagnosis, Mapping) and str(diagnosis.get("root_cause") or "").strip():
        digest = _h(str(diagnosis["root_cause"]).strip().casefold())
        if digest not in seen_dx:
            signals.append(S_DIAGNOSIS)
            detail["diagnosis_hash"] = digest
    fixes = [f for f in (result.get("process_fixes") or []) if isinstance(f, Mapping)
             and str(f.get("evidence_ref") or "").strip() and str(f.get("description") or "").strip()]
    if fixes:
        signals.append(S_PROCESS)
        detail["process_fixes"] = [{"kind": f.get("kind"), "evidence_ref": f["evidence_ref"]} for f in fixes]
    return {"signals": signals, "detail": detail}


def classification_signals(accepted: Sequence[Mapping[str, Any]]) -> list[str]:
    out: list[str] = []
    if accepted:
        out.append(S_RECLASSIFIED)
    if any(a["classification"] == C_BASELINE for a in accepted):
        out.append(S_BASELINE)
    if any(a["classification"] == C_ENVIRONMENT for a in accepted):
        out.append(S_ENVIRONMENT)
    if any(a["classification"] == C_SUPERSEDED for a in accepted):
        out.append(S_SUPERSEDED)
    return out


def assess_progress(*, keys_before: Sequence[str], keys_after: Sequence[str],
                    attempt_signals: Sequence[str]) -> dict[str, Any]:
    """Did the last repair move the problem? Independent of how many files changed."""
    before, after = set(keys_before), set(keys_after)
    resolved = sorted(before - after)
    signals = list(dict.fromkeys([*([S_RESOLVED] if resolved else []), *attempt_signals]))
    return {"progressed": bool(signals), "signals": signals, "resolved_keys": resolved,
            "new_keys": sorted(after - before), "unchanged_keys": sorted(before & after),
            "fully_new_problem": bool(after) and not (before & after) and bool(resolved or not before)}


# ── compact repair packet ────────────────────────────────────────────────────

_AC_REF = re.compile(r"\bAC\s*-?\s*(\d+)\b", re.IGNORECASE)


def _clip(text: Any, limit: int) -> str:
    text = "" if text is None else str(text)
    return text if len(text) <= limit else text[:limit] + f"…[+{len(text) - limit} chars]"


def relevant_criteria(findings: Sequence[Mapping[str, Any]], criteria: Sequence[str]) -> list[str]:
    """Only the acceptance criteria a finding names (e.g. `AC8`); a short head when none is named."""
    wanted: set[str] = set()
    for f in findings:
        text = f"{f.get('finding_key', '')} {f.get('summary', '')}"
        wanted.update(m.group(1) for m in _AC_REF.finditer(text))
    picked = []
    for index, crit in enumerate(criteria, start=1):
        head = _AC_REF.match(str(crit).strip())
        number = head.group(1) if head else str(index)
        if number in wanted:
            picked.append(str(crit))
    return [_clip(c, 600) for c in (picked or list(criteria)[:3])]


def build_repair_packet(*, findings: Sequence[Mapping[str, Any]], criteria: Sequence[str],
                        checks: Sequence[Mapping[str, Any]], evidence_state: Mapping[str, str],
                        changed_files: Sequence[str], diff: str, prior_attempts: Sequence[Mapping[str, Any]],
                        known_limitations: Sequence[Mapping[str, Any]] = (), step: Mapping[str, Any] | None = None,
                        diagnoses: Sequence[Mapping[str, Any]] = (), max_diff_chars: int = 6000,
                        max_output_chars: int = 1500) -> dict[str, Any]:
    """Everything the next, stronger attempt needs and nothing else.

    Replaces the full iteration context for later attempts: the finding, the
    criterion it concerns, the failing command/output, the relevant diff, what
    was already tried (with outcomes) and the environment's known limits.
    """
    failing_names = {str(f.get("finding_key", "")).split("::", 1)[-1] for f in findings}
    failing_rows = []
    for row in reversed(list(checks)):
        name = str(row.get("name"))
        if name in failing_names or evidence_state.get(name) in {"FAIL", "ERROR"} and name in failing_names:
            if all(r["name"] != name for r in failing_rows):
                failing_rows.append({"name": name, "status": str(row.get("status", "")).upper(),
                                     "command": row.get("command"), "exit_code": row.get("exit_code"),
                                     "output": _clip(row.get("summary"), max_output_chars),
                                     "log_ref": row.get("log_ref")})
    mentioned = [f for f in changed_files if any(str(f) in str(x.get("summary", "")) or str(f) == x.get("file")
                                                 for x in findings)]
    focus = mentioned or list(changed_files)[:8]
    diff_excerpt = _focused_diff(diff, focus, max_diff_chars)
    return {
        "packet": "COMPACT_REPAIR_PACKET", "version": VERSION,
        "FINDINGS": [{"finding_key": f.get("finding_key"), "severity": f.get("severity"),
                      "summary": _clip(f.get("summary"), 800), "file": f.get("file"),
                      "evidence_ref": f.get("evidence_ref")} for f in findings],
        "ACCEPTANCE_CRITERION": relevant_criteria(findings, criteria),
        "FAILING_CHECKS": failing_rows,
        "EVIDENCE_STATE_OF_FAILING": {n: evidence_state.get(n) for n in sorted(failing_names) if n in evidence_state},
        "RELEVANT_FILES": focus, "ALL_CHANGED_FILES": list(changed_files)[:40],
        "RELEVANT_DIFF": diff_excerpt,
        "PRIOR_ATTEMPTS": [{"attempt": a.get("attempt"), "stage": a.get("stage"), "profile_id": a.get("profile_id"),
                            "mode": a.get("mode"), "summary": _clip(a.get("summary"), 400),
                            "signals": a.get("signals", []), "code_changed": a.get("code_changed"),
                            "evidence_changed": a.get("evidence_changed"), "outcome": a.get("outcome")}
                           for a in prior_attempts],
        "PRIOR_DIAGNOSES": [{"by": d.get("by"), "root_cause": _clip(d.get("root_cause"), 600),
                             "next_actions": d.get("next_actions", [])[:5]} for d in diagnoses][-3:],
        "ENVIRONMENT_CONSTRAINTS": [{"id": k.get("id"), "classification": k.get("classification"),
                                     "description": _clip(k.get("description"), 300)} for k in known_limitations],
        "STEP": dict(step) if step else None,
        "RULES": ["Start from this packet; do not re-read the whole repository.",
                  "Diagnose the root cause first and state it; do not repeat an earlier attempt's approach.",
                  "A repair may need no product change: re-run the check, supply missing evidence, or classify a "
                  "pre-existing/environmental failure WITH evidence_ref and acceptance_impact NONE.",
                  "Do not fabricate a diff to look like progress."],
    }


def _focused_diff(diff: str, files: Sequence[str], limit: int) -> str:
    if not diff:
        return ""
    sections = re.split(r"(?m)^(?=diff --git )", diff)
    wanted = [s for s in sections if any(f"b/{f}" in s.split("\n", 1)[0] or f in s.split("\n", 1)[0] for f in files)]
    text = "".join(wanted) if wanted else diff
    return _clip(text, limit)


# ── ledger records ───────────────────────────────────────────────────────────

def ledger_entry(*, escalation_id: str, run_id: str, iteration_id: str | None, finding_ids: Sequence[str],
                 previous: Mapping[str, Any], new: Mapping[str, Any], reason: str,
                 previous_result: Mapping[str, Any], code_state_changed: bool | None,
                 evidence_state_changed: bool | None, signals: Sequence[str], at: str) -> dict[str, Any]:
    return {"entry_type": "ESCALATION", "version": VERSION, "escalation_id": escalation_id, "run_id": run_id,
            "iteration_id": iteration_id, "finding_ids": sorted(finding_ids),
            "previous_model_effort": dict(previous), "new_model_effort": dict(new), "reason": reason,
            "previous_result": dict(previous_result), "new_result": None,
            "code_state_changed": code_state_changed, "evidence_state_changed": evidence_state_changed,
            "signals": list(signals), "final_disposition": DISP_PENDING, "at": at}


def result_entry(*, escalation_id: str, new_result: Mapping[str, Any], code_state_changed: bool | None,
                 evidence_state_changed: bool | None, signals: Sequence[str], at: str) -> dict[str, Any]:
    return {"entry_type": "RESULT", "version": VERSION, "escalation_id": escalation_id,
            "new_result": dict(new_result), "code_state_changed": code_state_changed,
            "evidence_state_changed": evidence_state_changed, "signals": list(signals), "at": at}


def disposition_entry(*, escalation_id: str, disposition: str, detail: str | None, at: str) -> dict[str, Any]:
    _require(disposition in (DISP_RESOLVED, DISP_HUMAN_GATE, DISP_PENDING), f"unknown disposition {disposition!r}")
    return {"entry_type": "DISPOSITION", "version": VERSION, "escalation_id": escalation_id,
            "final_disposition": disposition, "detail": detail, "at": at}
