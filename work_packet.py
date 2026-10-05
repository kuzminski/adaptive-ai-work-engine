#!/usr/bin/env python3
"""AAW WORK PACKET V0.1 — plans a weaker implementer can actually finish.

Evidence from real runs: a plan that is only `goal + acceptance_criteria` is
enough for a strong model, but a weaker implementer (GPT-6 Luna high/max, a
free/secondary provider) then has to *re-plan* inside its own call, guesses
file locations, names checks differently from REQUIRED_EVIDENCE, forgets to run
them, and returns "done" with trivial defects. Each of those costs a full
SELF_VERIFY → REVIEW → REPAIR round, and repeated repairs by the same weak model
rarely converge.

This module adds three pure, deterministic pieces (no clock, no model calls):

1. **Work packet** — a mandatory, structured part of every ITERATION plan:
   files to read first, small ordered steps (each with files and its own
   verification), exact verification commands mapped to REQUIRED_EVIDENCE
   names, a definition of done, pitfalls and explicit out-of-scope items.
   `lint_work_packet` reports what is missing or vague.
2. **Difficulty assessment** — `assess_difficulty` turns visible plan facts
   (complexity label, size, cross-cutting changes, an under-specified packet,
   the planner's own `needs_strong_implementer`) into a route: the default
   bounded implementer, or a stronger implementer up front instead of a long
   repair thread by a weak model.
3. **Final self-audit** — every implement/repair call ends with a short,
   fixed checklist (`AUDIT_CHECKLIST`) inside the same call (the implementer
   still has its context, so this is the cheapest place to catch simple
   mistakes). `audit_checks` turns the reported audit into ordinary check rows,
   and `static_sanity_checks` adds deterministic detection of the simplest
   defects (conflict markers, Python syntax, JSON syntax, "done" with no
   change) before any reviewer is paid for.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

VERSION = "AAW_WORK_PACKET_V0.1"

ROUTE_DEFAULT = "DEFAULT"
ROUTE_HARDER = "HARDER"
ROUTE_STRONG = "STRONG"

# Size thresholds for a single bounded iteration handled by a weaker implementer.
MAX_STEPS_WEAK = 6
MAX_FILES_WEAK = 6
MAX_DIRS_WEAK = 3

_STRS = {"type": "array", "items": {"type": "string"}}

WORK_PACKET_SCHEMA: dict[str, Any] = {
    "type": ["object", "null"], "additionalProperties": False,
    "required": ["files_to_read", "files_to_change", "steps", "verification_commands", "definition_of_done",
                 "pitfalls", "out_of_scope", "needs_strong_implementer", "strong_implementer_reason"],
    "properties": {
        "files_to_read": _STRS,
        "files_to_change": _STRS,
        "steps": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["step_id", "action", "files", "details", "verify"],
            "properties": {"step_id": {"type": "string"}, "action": {"type": "string"},
                           "files": _STRS, "details": {"type": "string"}, "verify": {"type": "string"}}}},
        "verification_commands": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["evidence_name", "command", "expect"],
            "properties": {"evidence_name": {"type": "string"}, "command": {"type": "string"},
                           "expect": {"type": "string"}}}},
        "definition_of_done": _STRS,
        "pitfalls": _STRS,
        "out_of_scope": _STRS,
        "needs_strong_implementer": {"type": "boolean"},
        "strong_implementer_reason": {"type": ["string", "null"]},
    },
}

AUDIT_CHECKLIST: tuple[dict[str, str], ...] = (
    {"id": "A1_DONE", "check": "Every WORK_PACKET.definition_of_done item and every PLAN acceptance criterion is "
                               "actually met in the files (not only described)."},
    {"id": "A2_VERIFIED", "check": "Every WORK_PACKET.verification_commands command was run after the LAST edit; "
                                   "each is reported in checks under its evidence_name with exit code and counts."},
    {"id": "A3_SYNTAX", "check": "Every changed file parses/compiles (no syntax error, unbalanced bracket, broken "
                                 "JSON/YAML, merge-conflict marker)."},
    {"id": "A4_REFERENCES", "check": "All imports, call sites, names, paths and config keys you added or renamed "
                                     "resolve; nothing references a symbol that no longer exists."},
    {"id": "A5_LEFTOVERS", "check": "No placeholder, stub, TODO/FIXME you introduced, debug print, commented-out "
                                    "block, or temporary file is left behind."},
    {"id": "A6_SCOPE", "check": "Only WORK_PACKET.files_to_change (or files a step clearly required) changed; no "
                                "unrelated reformatting, deletions or out_of_scope work."},
    {"id": "A7_REPORT", "check": "changed_files in your result lists exactly the files you changed; deviations and "
                                 "uncertainties are honest."},
)
AUDIT_IDS = tuple(row["id"] for row in AUDIT_CHECKLIST)
AUDIT_STATUSES = ("PASS", "FAIL", "NOT_APPLICABLE")

SELF_AUDIT_SCHEMA: dict[str, Any] = {
    "type": ["object", "null"], "additionalProperties": False, "required": ["items", "fixed_during_audit"],
    "properties": {
        "items": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["id", "status", "evidence"],
            "properties": {"id": {"type": "string", "enum": list(AUDIT_IDS)},
                           "status": {"type": "string", "enum": list(AUDIT_STATUSES)},
                           "evidence": {"type": "string"}}}},
        "fixed_during_audit": _STRS,
    },
}

PLANNER_RULES = (
    "For status ITERATION, work_packet is MANDATORY and is written for a weaker implementer that must not have "
    "to re-plan: files_to_read (exact repository-relative paths to read first), files_to_change (exact paths), "
    "1-6 small ordered steps (each: step_id, a concrete action, its files, details naming the functions/sections "
    "to change and the intended behaviour, and how to verify that step), verification_commands (exact shell "
    "commands runnable from WORKTREE_PATH; evidence_name copied verbatim from REQUIRED_EVIDENCE; expect states "
    "the passing result), definition_of_done (checkable statements), pitfalls (known traps: encodings, "
    "Windows paths, existing tests, generated files), out_of_scope. Prefer the smallest sufficient change; split "
    "large work into more iterations instead of more steps. Set needs_strong_implementer true (with a concrete "
    "strong_implementer_reason) when the work needs cross-cutting design, subtle concurrency/state/security "
    "reasoning, or cannot be expressed as small concrete steps. Read the repository (read-only) as needed to "
    "name real paths; never invent paths. For NO_FURTHER_ACTION or ESCALATE set work_packet to null.")

IMPLEMENTER_RULES = (
    " Follow WORK_PACKET step by step: read files_to_read first, do the steps in order, run each step's verify, "
    "and do nothing listed in out_of_scope. Name every check exactly as its evidence_name / REQUIRED_EVIDENCE "
    "item. Keep the work short and focused; do not explore unrelated parts of the repository. "
    "BEFORE RETURNING run the FINAL SELF-AUDIT: go through every AUDIT_CHECKLIST item, fix in-scope problems "
    "you find, re-run the affected verification commands, and report self_audit.items (one row per checklist id, "
    "status PASS / FAIL / NOT_APPLICABLE, concrete evidence such as a command and its exit code) and "
    "self_audit.fixed_during_audit. Report FAIL honestly when you could not fix something: an honest FAIL is "
    "repaired cheaply, a false PASS costs a full review round.")


def _strings(value: Any) -> list[str]:
    return [str(item).strip() for item in value if str(item).strip()] if isinstance(value, list) else []


def _norm(text: Any) -> str:
    return " ".join(str(text or "").casefold().split())


def _rel(path: Any) -> str:
    text = str(path).strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def _tokens(text: Any) -> set[str]:
    return {tok for tok in re.split(r"[^0-9a-z]+", _norm(text)) if len(tok) > 1}


def evidence_matches(evidence: str, name: str, summary: str = "") -> bool:
    """True when a recorded check substantiates a REQUIRED_EVIDENCE item.

    Exact containment (the original rule) or, as a tolerant fallback, every
    significant token of the evidence name appears in the check's name. A weak
    implementer that names a check "Unit-tests (pytest)" for the evidence
    "unit tests" is not a failed check, it is the same check.
    """
    target, check_name = _norm(evidence), _norm(name)
    if not target:
        return False
    if target in f"{check_name} {_norm(summary)}" or (check_name and check_name in target):
        return True
    wanted = _tokens(evidence)
    return bool(wanted) and wanted <= _tokens(name)


def lint_work_packet(plan: Mapping[str, Any] | None, required_evidence: Sequence[str] = ()) -> dict[str, Any]:
    """Deterministic quality report of a plan's work packet (never raises)."""
    packet = (plan or {}).get("work_packet") if isinstance(plan, Mapping) else None
    issues: list[str] = []
    if not isinstance(packet, Mapping):
        return {"present": False, "issues": ["WORK_PACKET_MISSING"], "metrics": {"steps": 0, "files": 0, "dirs": 0},
                "files_to_change": [], "uncovered_evidence": list(required_evidence)}
    steps = [s for s in packet.get("steps") or [] if isinstance(s, Mapping)]
    files = _strings(packet.get("files_to_change"))
    for step in steps:
        files.extend(f for f in _strings(step.get("files")) if f not in files)
    commands = [c for c in packet.get("verification_commands") or [] if isinstance(c, Mapping)]
    if not steps:
        issues.append("NO_STEPS")
    for index, step in enumerate(steps, 1):
        label = str(step.get("step_id") or index)
        if not _strings(step.get("files")):
            issues.append(f"STEP_WITHOUT_FILES:{label}")
        if not str(step.get("verify") or "").strip():
            issues.append(f"STEP_WITHOUT_VERIFY:{label}")
        if len(str(step.get("action") or "").split()) < 3:
            issues.append(f"STEP_ACTION_VAGUE:{label}")
    if not files:
        issues.append("NO_FILES_TO_CHANGE")
    if not commands:
        issues.append("NO_VERIFICATION_COMMANDS")
    if not _strings(packet.get("definition_of_done")):
        issues.append("NO_DEFINITION_OF_DONE")
    covered = {_norm(c.get("evidence_name")) for c in commands}
    uncovered = [e for e in required_evidence
                 if not any(evidence_matches(e, name) for name in covered) and _norm(e) not in covered]
    issues.extend(f"EVIDENCE_WITHOUT_COMMAND:{e}" for e in uncovered)
    dirs = {str(Path(f.replace("\\", "/")).parent) for f in files}
    return {"present": True, "issues": issues,
            "metrics": {"steps": len(steps), "files": len(files), "dirs": len(dirs),
                        "verification_commands": len(commands)},
            "files_to_change": files, "uncovered_evidence": uncovered}


def assess_difficulty(plan: Mapping[str, Any] | None, lint: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Route the implementation by visible difficulty, before any model time is spent.

    STRONG: the planner says so, the label is SIGNIFICANTLY_DIFFICULT, or the
    iteration is too large/cross-cutting or too under-specified for a bounded
    weak implementer. HARDER: moderately large. DEFAULT otherwise.
    """
    plan = plan or {}
    lint = lint if lint is not None else lint_work_packet(plan)
    packet = plan.get("work_packet") if isinstance(plan.get("work_packet"), Mapping) else {}
    metrics = lint.get("metrics", {})
    strong: list[str] = []
    harder: list[str] = []
    complexity = str(plan.get("implementation_complexity") or "NORMAL").upper()
    if complexity == "SIGNIFICANTLY_DIFFICULT":
        strong.append("PLAN_COMPLEXITY:SIGNIFICANTLY_DIFFICULT")
    elif complexity == "HARDER":
        harder.append("PLAN_COMPLEXITY:HARDER")
    if packet.get("needs_strong_implementer") is True:
        strong.append("PLANNER_REQUESTED_STRONG:" + str(packet.get("strong_implementer_reason") or "no reason given"))
    steps, files, dirs = metrics.get("steps", 0), metrics.get("files", 0), metrics.get("dirs", 0)
    if steps > MAX_STEPS_WEAK or files > MAX_FILES_WEAK:
        strong.append(f"LARGE_ITERATION:steps={steps},files={files}")
    elif steps > MAX_STEPS_WEAK // 2 + 1 or files > MAX_FILES_WEAK // 2 + 1:
        harder.append(f"MEDIUM_ITERATION:steps={steps},files={files}")
    if dirs > MAX_DIRS_WEAK:
        strong.append(f"CROSS_CUTTING:dirs={dirs}")
    structural = [i for i in lint.get("issues", []) if i in ("WORK_PACKET_MISSING", "NO_STEPS", "NO_FILES_TO_CHANGE")]
    if "work_packet" not in plan:
        structural = []  # a plan from a planner that was never asked for a packet (frozen/legacy run)
    if structural:
        # An under-specified plan forces the implementer to plan on its own: give it to a model that can.
        strong.append("UNDER_SPECIFIED_PLAN:" + ",".join(structural))
    elif "work_packet" in plan and len(lint.get("issues", [])) >= 3:
        harder.append("VAGUE_PLAN:" + ",".join(lint["issues"][:3]))
    route = ROUTE_STRONG if strong else ROUTE_HARDER if harder else ROUTE_DEFAULT
    return {"version": VERSION, "route": route, "reasons": strong or harder, "metrics": dict(metrics)}


def audit_checks(result: Mapping[str, Any] | None, *, prefix: str = "self_audit") -> list[dict[str, Any]]:
    """Turn an implementer's final self-audit into check rows the controller records.

    Row names are stable (one per checklist id plus `completeness`) and every
    reported item yields a row, so a later audit's PASS supersedes an earlier
    FAIL of the same item instead of leaving a stale failure behind. FAIL items
    become FAIL checks (cheap, targeted REPAIR before any review); a missing or
    incomplete audit is a WARN the reviewer sees, never a forced repair loop.
    """
    audit = (result or {}).get("self_audit") if isinstance(result, Mapping) else None
    if not isinstance(audit, Mapping):
        return [{"name": f"{prefix}::completeness", "status": "WARN",
                 "summary": "implementer returned no final self-audit"}]
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in audit.get("items") or []:
        if not isinstance(item, Mapping):
            continue
        audit_id = str(item.get("id") or "").strip()
        status = str(item.get("status") or "").upper()
        if audit_id not in AUDIT_IDS or audit_id in seen or status not in AUDIT_STATUSES:
            continue
        seen.add(audit_id)
        evidence = str(item.get("evidence") or "no detail")[:400]
        rows.append({"name": f"{prefix}::{audit_id}", "status": "FAIL" if status == "FAIL" else "PASS",
                     "summary": ("implementer self-audit failed: " if status == "FAIL" else
                                 "not applicable: " if status == "NOT_APPLICABLE" else "self-audit: ") + evidence})
    missing = [audit_id for audit_id in AUDIT_IDS if audit_id not in seen]
    rows.append({"name": f"{prefix}::completeness", "status": "WARN" if missing else "PASS",
                 "summary": ("self-audit did not report: " + ", ".join(missing)) if missing
                 else "all checklist items reported"})
    return rows


_CONFLICT = re.compile(r"^(<{7}|>{7})( |$)|^={7}$", re.MULTILINE)
MAX_SANITY_BYTES = 2_000_000


def static_sanity_checks(worktree: str | Path | None, changed_files: Iterable[str], *,
                         claimed_files: Iterable[str] = (), planned_files: Iterable[str] = (),
                         expect_changes: bool = False, prefix: str = "static") -> list[dict[str, Any]]:
    """Deterministic detection of the simplest defects in the changed files.

    One row per category with a stable name, PASS or FAIL every time, so a
    later run supersedes an earlier FAIL. FAIL: merge-conflict markers, Python
    syntax errors, invalid JSON, or an implementation that changed nothing
    although its packet lists files to change. WARN: reported changed files
    that did not change, changes outside the packet's files. Nothing is
    executed (Python source is only compiled in memory). Without a readable
    worktree only the change-count rows are produced.
    """
    rows: list[dict[str, Any]] = []
    changed = [_rel(f) for f in changed_files if str(f).strip()]
    root = Path(worktree) if worktree else None
    if expect_changes:
        rows.append({"name": f"{prefix}::changes_present", "status": "PASS" if changed else "FAIL",
                     "summary": f"{len(changed)} changed file(s)" if changed else
                     "the work packet lists files to change but the worktree has no changes"})
    if root is not None and root.is_dir():
        problems: dict[str, list[str]] = {"conflict_markers": [], "python_syntax": [], "json_syntax": []}
        scanned = 0
        for rel in changed:
            path = root / rel
            if not path.is_file():
                continue  # deleted file
            try:
                if path.stat().st_size > MAX_SANITY_BYTES:
                    continue
                data = path.read_bytes()
            except OSError:
                continue
            if b"\x00" in data[:4096]:
                continue  # binary
            scanned += 1
            text = data.decode("utf-8", errors="replace")
            if _CONFLICT.search(text):
                problems["conflict_markers"].append(rel)
            suffix = path.suffix.lower()
            if suffix == ".py":
                try:
                    compile(text, rel, "exec", dont_inherit=True)
                except SyntaxError as exc:
                    problems["python_syntax"].append(f"{rel}:{exc.lineno}: {exc.msg}")
                except ValueError:
                    pass
            elif suffix == ".json":
                try:
                    json.loads(text.lstrip("\ufeff"))
                except json.JSONDecodeError as exc:
                    problems["json_syntax"].append(f"{rel}:{exc.lineno}: {exc.msg}")
        for category, found in problems.items():
            rows.append({"name": f"{prefix}::{category}", "status": "FAIL" if found else "PASS",
                         "summary": "; ".join(found[:10]) if found else f"{scanned} changed text file(s) clean"})
    claimed = {_rel(f) for f in claimed_files if str(f).strip()}
    actual = set(changed)
    phantom = sorted(claimed - actual)
    if claimed:
        rows.append({"name": f"{prefix}::reported_files_match", "status": "WARN" if phantom else "PASS",
                     "summary": ("reported as changed but not changed in the worktree: " + ", ".join(phantom[:10]))
                     if phantom else "reported changed files match the worktree"})
    planned = {_rel(f) for f in planned_files if str(f).strip()}
    if planned and actual:
        extra = sorted(f for f in actual - planned if not any(f.startswith(p.rstrip("/") + "/") for p in planned))
        rows.append({"name": f"{prefix}::within_work_packet", "status": "WARN" if extra else "PASS",
                     "summary": ("changed outside WORK_PACKET.files_to_change: " + ", ".join(extra[:10]))
                     if extra else "all changes are inside WORK_PACKET.files_to_change"})
    return rows
