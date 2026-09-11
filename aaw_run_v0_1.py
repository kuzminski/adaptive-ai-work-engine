#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Adaptive AI Work Launcher / Router V0.1 (Windows, stdlib only)

Pipeline:
TASK -> deterministic router -> optional cheap classifier -> problem JSON
     -> capability binding -> model binding -> execution adapter -> worker

Important V0.1 constraints:
- DIRECT_CLI_CONTROL is the primary execution adapter.
- ORCA_SUPERVISED is experimental/version-gated, not policy authority.
- Dry-run is the default. Use --launch explicitly.
- No "latest file" inference and no overwrites.
- Direct workers use verified provider CLI syntax and require a complete binding.
- For experimental ORCA execution the current documented diagnostic path is:
    terminal create -> wait tui-idle -> task-create -> dispatch --inject
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import process_observation
from execution_contract import allocate_execution, canonical_hash, update_execution
from execution_ledger import ExecutionLedger, LedgerError, LifecycleRecorder
from aaw_paths import CLASSIFIER_PROMPT_PATH, MODEL_REGISTRY_PATH, PLAYBOOK_ROOT, ROUTING_ROOT, STATS_ROOT

# ---------------------------------------------------------------------------
# Portable installation paths (V0.1).  External assets require explicit
# AAW_* environment configuration; defaults are relative to this module.
# ---------------------------------------------------------------------------
MODEL_REGISTRY_DEFAULT = MODEL_REGISTRY_PATH
CLASSIFIER_PROMPT_DEFAULT = CLASSIFIER_PROMPT_PATH
CLASSIFIER_THRESHOLD_DEFAULT = 0.95
DEFAULT_CLASSIFIER_BACKEND = "codex"
DEFAULT_CLASSIFIER_MODEL = "gpt-5.6-luna"
DEFAULT_CLASSIFIER_EFFORT = "none"
DEFAULT_EXECUTION_MODE = "direct"
NODE_FILES = {
    "N00": "N00_INTAKE.md", "N01": "N01_EVIDENCE.md",
    "N02": "N02_RESEARCH_REUSE.md", "N03": "N03_CONTRACT.md",
    "N04": "N04_PLAN.md", "N05": "N05_BUILD.md",
    "N06": "N06_MACHINE_CHECK.md", "N07": "N07_AI_REVIEW.md",
    "N08": "N08_REPAIR.md", "N09": "N09_HUMAN_GATE.md",
    "N10": "N10_FREEZE_RELEASE.md", "N11": "N11_MEASURE.md",
}

# ---------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------
OK = 0
HUMAN_REQUIRED = 20
BLOCKED = 21
INVALID_INPUT = 22
CLASSIFIER_FAILED = 23
REGISTRY_FAILED = 24
ORCA_FAILED = 25
WORKER_FAILED = 26
INTERNAL_ERROR = 70

PIPELINES = {"P01", "P02", "P03", "P04", "P05", "P06"}
TASK_CLASSES = {
    "research_experiment", "research_analysis", "greenfield_mvp",
    "greenfield_application", "reuse_replacement", "reuse_integration",
    "bounded_feature_change", "bounded_refactor", "debug_bugfix",
    "regression_repair", "design_to_code", "visual_fidelity",
}
AMBIGUITY = {"low", "medium", "high"}
RISK = {"low", "medium", "high"}
COORD_CAP = {"direct_execution", "light_coordination", "strong_planning"}
CAP_TO_CLASS = {
    "direct_execution": "C0",
    "light_coordination": "C1",
    "strong_planning": "C2",
}
EXPECTED_CLASSIFIER_KEYS = [
    "pipeline", "task_class", "ambiguity", "risk",
    "recommended_coordinator_capability", "confidence",
]

DEFAULT_ROLE_BY_PIPELINE = {
    "P01": "RESEARCH_SYNTHESIZER",
    "P02": "ARCHITECT_STRONG",
    "P03": "RESEARCH_SYNTHESIZER",
    "P04": "CODE_IMPLEMENTER",
    "P05": "CODE_IMPLEMENTER",
    "P06": "CODE_IMPLEMENTER",
}
DEFAULT_COORD_ROLE = {"C1": "LIGHT_COORDINATOR", "C2": "ARCHITECT_STRONG"}

# Structural routing patterns. These are intentionally explicit and conservative.
PATTERNS = {
    "P05": [r"\bbug\b", r"\bdebug\b", r"\bregresj\w*", r"\bnapraw\w*", r"\bcrash\w*", r"\bfailing test\w*", r"\breproduce\w*"],
    "P06": [r"\bfigma\b", r"\bdesign[- ]to[- ]code\b", r"\bvisual fidelity\b", r"\bpixel[- ]perfect\b", r"\bgolden master\b", r"\bprojekt wizualn\w*"],
    "P02": [r"\bgreenfield\b", r"\bmvp\b", r"\bnow[aą] aplikacj\w*", r"\bzbuduj aplikacj\w*", r"\bstw[oó]rz aplikacj\w*", r"\bnew app\b", r"\bfrom scratch\b"],
    "P03": [r"\breuse[- ]first\b", r"\breplacement\b", r"\bwybierz bibliotek\w*", r"\bwyb[oó]r bibliotek\w*", r"\bmature dependency\b", r"\bznajd[zź]\w* gotow\w* rozwi[aą]z\w*"],
    "P01": [r"\bhipotez\w*", r"\beksperyment\w*", r"\bexperiment\b", r"\bbenchmark\b", r"\bbadani\w*", r"\bresearch\b"],
    "P04": [r"\bbounded change\b", r"\bexisting app\b", r"\bistniej[aą]c\w* aplikacj\w*", r"\bdodaj funkcj\w*", r"\brefactor\w*", r"\brefaktoryz\w*", r"\bfeature\b"],
}


@dataclass(frozen=True)
class RunCtx:
    run_id: str
    run8: str
    stamp: str
    slug: str
    prefix: str


def log(msg: str) -> None:
    print(f"[AAW] {msg}", file=sys.stderr)


def die(msg: str, code: int) -> None:
    log("ERROR: " + msg)
    raise SystemExit(code)


def record_intent(ctx: "RunCtx", execution: Mapping[str, Any], execution_path: Path,
                  working_directory: Optional[Path] = None) -> LifecycleRecorder:
    """Durably record EXECUTION_INTENT before dispatch. Fail-closed."""
    ledger = ExecutionLedger.for_run(ctx.run_id, STATS_ROOT)
    try:
        ledger.record_execution_intent(
            execution_id=str(execution["execution_id"]), node_id=str(execution["node_id"]),
            subtask_id=execution.get("subtask_id"), invocation_kind=str(execution["invocation_kind"]),
            descriptor_path=execution_path, provider=execution.get("provider"),
            harness=execution.get("harness"), model=execution.get("model"),
            effort=execution.get("effort"), profile=execution.get("profile"),
            input_contract_hash=execution.get("input_contract_hash"),
            worktree=str(working_directory) if working_directory else None,
        )
    except LedgerError as exc:
        die(f"{getattr(exc, 'classification', 'LEDGER_ERROR')} execution intent could not be durably "
            f"recorded; no process was launched: {exc}", BLOCKED)
    return LifecycleRecorder(ledger, str(execution["execution_id"]))


def close_from_returncode(rc: Optional[int], **extra: Any) -> Dict[str, Any]:
    """Translate one observed child exit into the bounded close taxonomy."""
    if rc is None:
        return {"close_reason": "UNKNOWN", "effect_certainty": "UNKNOWN",
                "observation_source": "RUNNER_EXCEPTION", "exit_code": None, "extra": extra or None}
    return {"close_reason": "COMPLETED" if rc == 0 else "FAILED", "effect_certainty": "CONFIRMED",
            "observation_source": "CHILD_PROCESS_EXIT", "exit_code": rc, "extra": extra or None}


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        die(f"Cannot read {path}: {exc}", INVALID_INPUT)


def read_json(path: Path) -> Dict[str, Any]:
    try:
        obj = json.loads(read_text(path))
    except json.JSONDecodeError as exc:
        die(f"Invalid JSON in {path}: {exc}", INVALID_INPUT)
    if not isinstance(obj, dict):
        die(f"Expected object in {path}", INVALID_INPUT)
    return obj


def write_json_new(path: Path, obj: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        die(f"Refusing overwrite: {path}", BLOCKED)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_text_new(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        die(f"Refusing overwrite: {path}", BLOCKED)
    path.write_text(text, encoding="utf-8")


def slugify(s: str, limit: int = 48) -> str:
    s = s.lower().translate(str.maketrans("ąćęłńóśżź", "acelnoszz"))
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")[:limit].rstrip("-")
    return s or "task"


def make_ctx(task: str) -> RunCtx:
    now = dt.datetime.now().astimezone()
    stamp = now.strftime("%Y%m%d_%H%M%S")
    run8 = secrets.token_hex(4)
    run_id = f"AAW_{stamp}_{run8}"
    slug = slugify(task)
    return RunCtx(run_id, run8, stamp, slug, f"{stamp}__{slug}__{run8}")


def artifact_paths(ctx: RunCtx) -> Dict[str, Path]:
    return {
        "problem": ROUTING_ROOT / f"{ctx.prefix}__zdefiniowanie_problemu.json",
        "capability": ROUTING_ROOT / f"{ctx.prefix}__capability_binding.json",
        "model": ROUTING_ROOT / f"{ctx.prefix}__model_binding.json",
        "dryrun": ROUTING_ROOT / f"{ctx.prefix}__dry_run.txt",
        "launch": ROUTING_ROOT / f"{ctx.prefix}__execution_receipt.json",
    }


def contains_any(text: str, pats: Iterable[str]) -> bool:
    return any(re.search(p, text, flags=re.I) for p in pats)


def deterministic_route(task: str) -> Tuple[Optional[str], List[str]]:
    matched = [p for p, pats in PATTERNS.items() if contains_any(task, pats)]
    matched = list(dict.fromkeys(matched))

    # P05 bug/repair beats bounded change.
    if "P05" in matched and "P04" in matched:
        matched.remove("P04")
    # P06 visual/design authority beats bounded change.
    if "P06" in matched and "P04" in matched:
        matched.remove("P04")
    # New application + reuse preference remains P02 unless reuse selection itself is the deliverable.
    if "P02" in matched and "P03" in matched:
        reuse_deliverable = contains_any(task, [
            r"\bcelem jest wyb[oó]r\b", r"\bcelem jest znalezienie\b",
            r"\bwybierz bibliotek\w*", r"\breplacement\b",
        ])
        if not reuse_deliverable:
            matched.remove("P03")
    # Research supporting a new product does not make the pipeline P01.
    if "P02" in matched and "P01" in matched:
        research_deliverable = contains_any(task, [
            r"\bcelem jest badani\w*", r"\bcelem jest eksperyment\w*",
            r"\bhipotez\w*",
        ])
        if not research_deliverable:
            matched.remove("P01")

    if len(matched) == 1:
        return matched[0], matched
    return None, matched


def deterministic_problem(pipeline: str, task: str) -> Dict[str, Any]:
    lower = task.lower()
    task_class = {
        "P01": "research_experiment" if re.search(r"\b(hipotez|eksperyment|experiment)\w*", lower) else "research_analysis",
        "P02": "greenfield_mvp" if re.search(r"\bmvp\b", lower) else "greenfield_application",
        "P03": "reuse_replacement" if re.search(r"\b(replacement|wybierz|zast[aą]p)\w*", lower) else "reuse_integration",
        "P04": "bounded_refactor" if re.search(r"\b(refactor|refaktoryz)\w*", lower) else "bounded_feature_change",
        "P05": "regression_repair" if re.search(r"\bregresj\w*", lower) else "debug_bugfix",
        "P06": "visual_fidelity" if re.search(r"\b(pixel|visual fidelity|golden master|odwzorow)\w*", lower) else "design_to_code",
    }[pipeline]

    ambiguity = "medium" if contains_any(task, [r"\bniejasn\w*", r"\bsprzeczn\w*", r"\barchitecture\b", r"\barchitektur\w*"]) else "low"
    risk = "high" if contains_any(task, [r"\bproduction\b", r"\bprodukc\w*", r"\bsecurity\b", r"\bbezpiecze[nń]\w*", r"\bmigracj\w*", r"\bcritical\b", r"\bkrytyczn\w*"]) else "medium"

    if pipeline in {"P04", "P05"} and ambiguity == "low":
        cap = "direct_execution"
    elif pipeline in {"P01", "P02", "P03"}:
        cap = "strong_planning" if ambiguity != "low" or risk == "high" else "light_coordination"
    else:
        cap = "strong_planning" if ambiguity == "medium" or risk == "high" else "light_coordination"

    # confidence=1.0 means unique deterministic structural match, not calibrated probability.
    return {
        "pipeline": pipeline,
        "task_class": task_class,
        "ambiguity": ambiguity,
        "risk": risk,
        "recommended_coordinator_capability": cap,
        "confidence": 1.0,
    }


def validate_problem(obj: Any) -> Dict[str, Any]:
    if not isinstance(obj, dict) or list(obj.keys()) != EXPECTED_CLASSIFIER_KEYS:
        die("Classifier JSON must contain exactly the six frozen keys in frozen order.", CLASSIFIER_FAILED)
    if obj["pipeline"] not in PIPELINES:
        die("Invalid pipeline", CLASSIFIER_FAILED)
    if obj["task_class"] not in TASK_CLASSES:
        die("Invalid task_class", CLASSIFIER_FAILED)
    if obj["ambiguity"] not in AMBIGUITY:
        die("Invalid ambiguity", CLASSIFIER_FAILED)
    if obj["risk"] not in RISK:
        die("Invalid risk", CLASSIFIER_FAILED)
    if obj["recommended_coordinator_capability"] not in COORD_CAP:
        die("Invalid coordinator capability", CLASSIFIER_FAILED)
    c = obj["confidence"]
    if isinstance(c, bool) or not isinstance(c, (int, float)) or not 0 <= float(c) <= 1:
        die("Invalid confidence", CLASSIFIER_FAILED)
    return dict(obj)


def extract_classifier_prompt(path: Path) -> str:
    text = read_text(path)
    blocks = re.findall(r"```(?:text)?\s*\n(.*?)```", text, flags=re.S | re.I)
    if not blocks:
        die(f"No fenced classifier prompt found in {path}", CLASSIFIER_FAILED)
    # Pick the block containing the immutable classifier identity if possible.
    for block in blocks:
        if "CHEAP_CLASSIFIER_V1" in block:
            return block.strip()
    return blocks[0].strip()


def split_cmd(cmd: str) -> List[str]:
    parts = shlex.split(cmd, posix=False)
    return [p[1:-1] if len(p) >= 2 and p[0] == p[-1] == '"' else p for p in parts]


def run_cmd(argv: Sequence[str], stdin: Optional[str] = None, cwd: Optional[Path] = None,
            provider: Optional[str] = None, dispatch: bool = False) -> Tuple[int, str, str]:
    """Spawn one child process, reporting the spawn only when it is a dispatch.

    ``Popen`` replaces ``subprocess.run`` so that ``EXECUTION_STARTED`` can be
    an observed process receipt rather than an inference from intent.
    ``dispatch=True`` marks the call that launches an invocation; helper spawns
    are never reported as start evidence.
    """
    log("EXEC: " + subprocess.list2cmdline(list(argv)))
    try:
        process = subprocess.Popen(
            list(argv), text=True, encoding="utf-8", errors="replace",
            stdin=subprocess.PIPE if stdin is not None else None,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
            cwd=str(cwd) if cwd else None,
        )
    except FileNotFoundError:
        die(f"Executable not found: {argv[0]}", BLOCKED)
    except OSError as exc:
        die(f"Cannot execute command: {exc}", BLOCKED)
    process_observation.notify_process_start(
        process, dispatch=dispatch, argv=list(argv), cwd=cwd, provider=provider,
        adapter="DIRECT_CLI_CONTROL")
    stdout, stderr = process.communicate(input=stdin)
    return process.returncode, stdout or "", stderr or ""


def parse_json_flex(text: str) -> Optional[Any]:
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    objs = []
    for line in text.splitlines():
        try:
            objs.append(json.loads(line.strip()))
        except Exception:
            pass
    if objs:
        return objs[-1]
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end+1])
        except json.JSONDecodeError:
            pass
    return None


def classifier_schema() -> Dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "pipeline": {"type": "string", "enum": sorted(PIPELINES)},
            "task_class": {"type": "string", "enum": sorted(TASK_CLASSES)},
            "ambiguity": {"type": "string", "enum": sorted(AMBIGUITY)},
            "risk": {"type": "string", "enum": sorted(RISK)},
            "recommended_coordinator_capability": {"type": "string", "enum": sorted(COORD_CAP)},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": EXPECTED_CLASSIFIER_KEYS,
    }


def parse_codex_events(text: str) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    events: List[Dict[str, Any]] = []
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    thread_id = next(
        (e.get("thread_id") for e in events if e.get("type") == "thread.started" and e.get("thread_id")),
        None,
    )
    return events, thread_id


def classifier_telemetry(ctx: RunCtx, events: Sequence[Mapping[str, Any]], thread_id: Optional[str], execution_id: str) -> Path:
    usage = next(
        (e.get("usage") for e in reversed(events) if e.get("type") == "turn.completed" and isinstance(e.get("usage"), dict)),
        {},
    )
    record = {
        "schema_version": "0.4A",
        "execution_id": execution_id,
        "aaw_run_id": ctx.run_id,
        "stage": "00__CLASSIFIER",
        "backend": DEFAULT_CLASSIFIER_BACKEND,
        "thread_id": thread_id,
        "input_tokens": usage.get("input_tokens"),
        "cached_input_tokens": usage.get("cached_input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "reasoning_tokens": usage.get("reasoning_output_tokens"),
        "model": DEFAULT_CLASSIFIER_MODEL,
        "effort": DEFAULT_CLASSIFIER_EFFORT,
    }
    stamp = dt.datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")
    path = STATS_ROOT / ctx.run_id / f"00__CLASSIFIER__codex__{stamp}.json"
    write_json_new(path, record)
    return path


def codex_classifier_fallback(prompt_file: Path, task: str, ctx: RunCtx) -> Dict[str, Any]:
    codex = shutil.which("codex")
    if not codex:
        die("Classifier failed: Codex CLI is unavailable", CLASSIFIER_FAILED)

    prompt = extract_classifier_prompt(prompt_file)
    payload = prompt + "\n\nTASK TO CLASSIFY:\n" + task.strip() + "\n"
    execution, execution_path = allocate_execution(
        descriptor_root=STATS_ROOT / ctx.run_id / "EXECUTIONS", run_id=ctx.run_id,
        node_id="CLASSIFIER", invocation_kind="LLM", provider="OPENAI", harness="codex",
        model=DEFAULT_CLASSIFIER_MODEL, effort=DEFAULT_CLASSIFIER_EFFORT,
        input_contract_hash=canonical_hash(payload), selection_reason="AMBIGUOUS_DETERMINISTIC_ROUTE",
        policy_version="cheap_classifier_v1_codex",
    )
    recorder = record_intent(ctx, execution, execution_path)
    with tempfile.TemporaryDirectory(prefix="aaw_classifier_") as tmp:
        tmp_root = Path(tmp)
        schema_path = tmp_root / "classifier_schema.json"
        final_path = tmp_root / "final.json"
        schema_path.write_text(json.dumps(classifier_schema(), ensure_ascii=False), encoding="utf-8")
        argv = [
            codex, "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--sandbox", "read-only",
            "--model", DEFAULT_CLASSIFIER_MODEL,
            "--config", f'model_reasoning_effort="{DEFAULT_CLASSIFIER_EFFORT}"',
            "--output-schema", str(schema_path),
            "--output-last-message", str(final_path),
            "--json",
            "-",
        ]
        with process_observation.observation_scope(recorder.observe_start):
            rc, out, err = run_cmd(argv, stdin=payload, provider="OPENAI", dispatch=True)
        events, thread_id = parse_codex_events(out)
        update_execution(execution_path, str(execution["execution_id"]), provider_session_id=thread_id,
                         status="COMPLETED" if rc == 0 else "FAILED")
        recorder.close(outcome="COMPLETED" if rc == 0 else "FAILED",
                       **close_from_returncode(rc, provider_session_id=thread_id))
        if rc != 0:
            die(f"Classifier failed rc={rc}: {err.strip()}; events={out[-500:]!r}", CLASSIFIER_FAILED)
        try:
            final_text = final_path.read_text(encoding="utf-8")
        except OSError as exc:
            die(f"Classifier final response unavailable: {exc}", CLASSIFIER_FAILED)

    try:
        obj = json.loads(final_text.strip())
    except json.JSONDecodeError as exc:
        die(f"Classifier final response is not exact JSON: {exc}; final={final_text[:500]!r}", CLASSIFIER_FAILED)
    problem = validate_problem(obj)
    telemetry_path = classifier_telemetry(ctx, events, thread_id, str(execution["execution_id"]))
    log(f"classifier telemetry={telemetry_path}")
    return problem


def classifier_command_override(cmd: str, prompt_file: Path, task: str, ctx: RunCtx) -> Dict[str, Any]:
    prompt = extract_classifier_prompt(prompt_file)
    payload = prompt + "\n\nTASK TO CLASSIFY:\n" + task.strip() + "\n"
    argv = split_cmd(cmd)
    execution, execution_path = allocate_execution(
        descriptor_root=STATS_ROOT / ctx.run_id / "EXECUTIONS", run_id=ctx.run_id,
        node_id="CLASSIFIER", invocation_kind="LLM", harness=argv[0] if argv else None,
        input_contract_hash=canonical_hash(payload), selection_reason="EXPLICIT_CLASSIFIER_OVERRIDE",
        policy_version="cheap_classifier_v1_override",
    )
    recorder = record_intent(ctx, execution, execution_path)
    with process_observation.observation_scope(recorder.observe_start):
        rc, out, err = run_cmd(argv, stdin=payload, dispatch=True)
    update_execution(execution_path, str(execution["execution_id"]), status="COMPLETED" if rc == 0 else "FAILED")
    recorder.close(outcome="COMPLETED" if rc == 0 else "FAILED", **close_from_returncode(rc))
    if rc != 0:
        die(f"Classifier failed rc={rc}: {err.strip()}", CLASSIFIER_FAILED)
    try:
        obj = json.loads(out.strip())
    except json.JSONDecodeError as exc:
        die(f"Classifier stdout is not exact JSON: {exc}; stdout={out[:500]!r}", CLASSIFIER_FAILED)
    return validate_problem(obj)


def abstract_effort(problem: Mapping[str, Any], cls: str) -> str:
    if cls == "C0":
        return "LOW"
    if cls == "C1":
        return "MEDIUM" if problem["ambiguity"] == "medium" or problem["risk"] == "high" else "LOW"
    return "HIGH" if problem["ambiguity"] == "high" or problem["risk"] == "high" else "MEDIUM"


def resolve_capability(problem: Mapping[str, Any], explicit_role: Optional[str]) -> Dict[str, Any]:
    rec = str(problem["recommended_coordinator_capability"])
    cls = CAP_TO_CLASS[rec]
    role = explicit_role or DEFAULT_COORD_ROLE.get(cls) or DEFAULT_ROLE_BY_PIPELINE[str(problem["pipeline"])]
    return {
        "schema_version": "0.1",
        "pipeline": problem["pipeline"],
        "recommended_coordinator_capability": rec,
        "coordinator_class": cls,
        "role": role,
        "abstract_effort": abstract_effort(problem, cls),
        "separate_coordinator": cls != "C0",
    }


def select_binding(registry: Mapping[str, Any], capability: Mapping[str, Any]) -> Dict[str, Any]:
    items = registry.get("active_bindings")
    if not isinstance(items, list):
        die("MODEL_REGISTRY.active_bindings must be an array", REGISTRY_FAILED)

    role, cls = capability["role"], capability["coordinator_class"]
    candidates = []
    for b in items:
        if not isinstance(b, dict):
            continue
        if b.get("capability") == role or b.get("role") == role or b.get("coordinator_class") == cls:
            candidates.append(dict(b))

    if not candidates:
        die(f"HUMAN_REQUIRED: no active binding for role={role}, class={cls}", HUMAN_REQUIRED)

    if len(candidates) > 1:
        if not all(isinstance(x.get("priority"), int) for x in candidates):
            die(f"HUMAN_REQUIRED: multiple bindings for {role}; add unique integer priorities", HUMAN_REQUIRED)
        candidates.sort(key=lambda x: int(x["priority"]))
        if len(candidates) > 1 and candidates[0]["priority"] == candidates[1]["priority"]:
            die(f"HUMAN_REQUIRED: tied model-binding priority for {role}", HUMAN_REQUIRED)

    b = candidates[0]
    return {
        "schema_version": "0.1",
        "registry_version": registry.get("registry_version"),
        "binding_status": registry.get("binding_status"),
        "coordinator_class": cls,
        "role": role,
        "harness": b.get("harness") or b.get("agent"),
        "model_family": b.get("model_family") or b.get("model"),
        "runtime_model_id": b.get("runtime_model_id"),
        "effort": b.get("effort"),
        "launch_command": b.get("launch_command"),
        "confidence": b.get("confidence"),
        "source_binding": b,
    }


def recursive_find(obj: Any, names: Iterable[str]) -> Optional[Any]:
    names = set(names)
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in names and v not in (None, ""):
                return v
        for v in obj.values():
            found = recursive_find(v, names)
            if found not in (None, ""):
                return found
    if isinstance(obj, list):
        for v in obj:
            found = recursive_find(v, names)
            if found not in (None, ""):
                return found
    return None


def node_contract(node: str) -> Tuple[Path, str]:
    filename = NODE_FILES.get(node)
    if not filename:
        die(f"Unknown semantic node: {node}", INVALID_INPUT)
    path = PLAYBOOK_ROOT / "nodes" / filename
    return path, read_text(path)


def worker_prompt(task: str, ctx: RunCtx, problem: Mapping[str, Any], cap: Mapping[str, Any], node: str) -> str:
    contract_path, contract = node_contract(node)
    routing_artifact = ROUTING_ROOT / f"{ctx.prefix}__zdefiniowanie_problemu.json"
    return (
        f"AAW_RUN_ID={ctx.run_id}\n"
        f"ROUTING_ARTIFACT={routing_artifact}\n"
        f"PLAYBOOK_ROOT={PLAYBOOK_ROOT}\n"
        f"STATS_ROOT={STATS_ROOT}\n"
        f"PIPELINE={problem['pipeline']}\n"
        f"NODE={node}\n"
        f"NODE_CONTRACT_PATH={contract_path}\n"
        f"ROLE={cap['role']}\n\n"
        f"TASK:\n{task.strip()}\n\n"
        f"NODE CONTRACT (authoritative):\n{contract.strip()}\n"
    )


def normalized_usage(usage: Any) -> Dict[str, Any]:
    usage = usage if isinstance(usage, Mapping) else {}
    return {
        "input_tokens": usage.get("input_tokens"),
        "cached_input_tokens": usage.get("cached_input_tokens") or usage.get("cache_read_input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "reasoning_tokens": usage.get("reasoning_output_tokens") or usage.get("reasoning_tokens"),
    }


def write_node_telemetry(
    ctx: RunCtx, problem: Mapping[str, Any], binding: Mapping[str, Any], node: str,
    execution_mode: str, started_at: str, ended_at: str, wall_time_s: float,
    outcome: str, provider_session_id: Optional[str], usage: Any,
    orca_ids: Optional[Mapping[str, Any]] = None, execution_id: Optional[str] = None,
) -> Path:
    ids = orca_ids or {}
    normalized = normalized_usage(usage)
    record = {
        "schema_version": "1.1",
        "execution_id": execution_id,
        "run_id": ctx.run_id,
        "pipeline": problem["pipeline"],
        "node": node,
        "task_short": ctx.slug,
        "execution_mode": execution_mode,
        "agent": binding.get("harness"),
        "model": binding.get("runtime_model_id"),
        "effort": binding.get("effort"),
        "orca_run_id": ids.get("orca_run_id"),
        "orca_task_id": ids.get("orca_task_id"),
        "orca_dispatch_id": ids.get("orca_dispatch_id"),
        "provider_session_id": provider_session_id,
        "started_at": started_at,
        "ended_at": ended_at,
        "wall_time_s": round(wall_time_s, 3),
        "usage": normalized,
        "outcome": outcome,
        "telemetry_status": "CAPTURED" if provider_session_id and all(v is not None for v in normalized.values()) else "PARTIAL",
    }
    stamp = dt.datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")
    path = STATS_ROOT / ctx.run_id / f"00__{node}__{binding.get('harness')}__{stamp}.json"
    write_json_new(path, record)
    return path


def require_direct_binding(binding: Mapping[str, Any]) -> Tuple[str, str, str]:
    harness = str(binding.get("harness") or "").lower()
    model = str(binding.get("runtime_model_id") or "").strip()
    effort = str(binding.get("effort") or "").strip()
    if harness not in {"codex", "claude"} or not model or not effort:
        die(
            "BLOCKED / PARTIAL CAPABILITY: direct execution requires harness, "
            "runtime_model_id and effort in MODEL_REGISTRY.",
            BLOCKED,
        )
    return harness, model, effort


def launch_direct(task: str, ctx: RunCtx, problem: Mapping[str, Any], cap: Mapping[str, Any], binding: Mapping[str, Any], working_directory: Path, node: str) -> Dict[str, Any]:
    harness, model, effort = require_direct_binding(binding)
    if not working_directory.is_dir():
        die(f"Working directory does not exist: {working_directory}", BLOCKED)
    prompt = worker_prompt(task, ctx, problem, cap, node)
    execution, execution_path = allocate_execution(
        descriptor_root=STATS_ROOT / ctx.run_id / "EXECUTIONS", run_id=ctx.run_id, node_id=node,
        invocation_kind="LLM", provider=binding.get("provider"), harness=harness, model=model, effort=effort,
        profile=binding.get("profile"), input_contract_hash=canonical_hash(prompt),
        selection_reason=binding.get("selection_reason") or binding.get("binding_source"),
        policy_version=str(binding.get("registry_version")) if binding.get("registry_version") is not None else None,
    )
    recorder = record_intent(ctx, execution, execution_path, working_directory)
    started = dt.datetime.now().astimezone()
    monotonic_start = time.monotonic()

    if harness == "codex":
        executable = shutil.which("codex")
        if not executable:
            die("Codex CLI is unavailable", BLOCKED)
        argv = [
            executable, "exec", "--skip-git-repo-check", "--sandbox", "workspace-write",
            "--model", model, "--config", f'model_reasoning_effort="{effort}"',
            "--cd", str(working_directory), "--json", "-",
        ]
        with process_observation.observation_scope(recorder.observe_start):
            rc, out, err = run_cmd(argv, stdin=prompt, cwd=working_directory, provider=binding.get("provider"), dispatch=True)
        events, session_id = parse_codex_events(out)
        usage = next((e.get("usage") for e in reversed(events) if e.get("type") == "turn.completed"), {})
        provider_result: Any = events
    else:
        executable = shutil.which("claude")
        if not executable:
            die("BLOCKED / PARTIAL CAPABILITY: Claude CLI is unavailable", BLOCKED)
        help_rc, help_out, help_err = run_cmd([executable, "--help"])
        help_text = help_out + help_err
        required_flags = ("--model", "--effort", "--output-format")
        if help_rc != 0 or not all(flag in help_text for flag in required_flags) or not ("-p" in help_text or "--print" in help_text):
            die("BLOCKED / PARTIAL CAPABILITY: installed Claude CLI cannot express the requested binding", BLOCKED)
        argv = [executable, "-p", prompt, "--model", model, "--effort", effort, "--output-format", "json"]
        with process_observation.observation_scope(recorder.observe_start):
            rc, out, err = run_cmd(argv, cwd=working_directory, provider=binding.get("provider"), dispatch=True)
        provider_result = parse_json_flex(out) or {}
        session_id = recursive_find(provider_result, {"session_id", "sessionId"})
        usage = recursive_find(provider_result, {"usage"}) or {}

    ended = dt.datetime.now().astimezone()
    wall_time_s = time.monotonic() - monotonic_start
    outcome = "VALID PASS" if rc == 0 else "VALID FAIL"
    update_execution(execution_path, str(execution["execution_id"]), provider_session_id=str(session_id) if session_id else None,
                     status="COMPLETED" if rc == 0 else "FAILED")
    recorder.close(outcome=outcome, **close_from_returncode(
        rc, provider_session_id=str(session_id) if session_id else None))
    telemetry_path = write_node_telemetry(
        ctx, problem, binding, node, "DIRECT_CLI_CONTROL",
        started.isoformat(timespec="milliseconds"), ended.isoformat(timespec="milliseconds"),
        wall_time_s, outcome, str(session_id) if session_id else None, usage,
        execution_id=str(execution["execution_id"]),
    )
    receipt = {
        "schema_version": "0.4A",
        "execution_id": execution["execution_id"],
        "aaw_run_id": ctx.run_id,
        "execution_mode": "DIRECT_CLI_CONTROL",
        "status": "COMPLETED" if rc == 0 else "FAILED",
        "node": node,
        "working_directory": str(working_directory),
        "fresh_session": True,
        "provider_session_id": session_id,
        "returncode": rc,
        "stdout": out,
        "stderr": err,
        "argv": argv,
        "provider_result": provider_result,
        "telemetry_path": str(telemetry_path),
        "binding": dict(binding),
    }
    if rc != 0:
        log(f"Direct worker failed rc={rc}; telemetry={telemetry_path}")
    return receipt


def orca_json(argv: Sequence[str], *, required: bool = True) -> Dict[str, Any]:
    rc, out, err = run_cmd(argv)
    obj = parse_json_flex(out)
    if required and (rc != 0 or obj is None):
        die(f"ORCA command failed/invalid JSON rc={rc}; stderr={err.strip()}; stdout={out[:500]!r}", ORCA_FAILED)
    return {"returncode": rc, "stdout": out, "stderr": err, "json": obj, "argv": list(argv)}


def launch_orca_supervised(task: str, ctx: RunCtx, problem: Mapping[str, Any], cap: Mapping[str, Any], binding: Mapping[str, Any], worktree: str, launch_command_override: Optional[str], node: str) -> Dict[str, Any]:
    # Preflight runtime.
    status = orca_json(["orca", "status", "--json"])

    launch_cmd = launch_command_override or binding.get("launch_command")
    if not launch_cmd:
        die(
            "BLOCKED: no explicit launch_command. Add active_binding.launch_command "
            "or pass --launch-command. V0.1 will not guess provider-specific argv.",
            BLOCKED,
        )

    title = f"aaw-{ctx.run8}-{cap['role'].lower()}"

    # Current documented safe supervised path:
    # terminal create -> wait tui-idle -> task-create -> dispatch --inject.
    terminal = orca_json([
        "orca", "terminal", "create",
        "--worktree", worktree,
        "--title", title,
        "--command", launch_cmd,
        "--json",
    ])
    handle = recursive_find(terminal["json"], {"handle", "terminalHandle", "terminal_handle"})
    if not handle:
        die("Could not resolve fresh ORCA terminal handle", ORCA_FAILED)

    wait = orca_json([
        "orca", "terminal", "wait",
        "--terminal", str(handle),
        "--for", "tui-idle",
        "--timeout-ms", "120000",
        "--json",
    ])

    spec = worker_prompt(task, ctx, problem, cap, node)

    task_create = orca_json([
        "orca", "orchestration", "task-create",
        "--spec", spec,
        "--json",
    ])
    task_id = recursive_find(task_create["json"], {"task_id", "taskId", "id"})
    if not task_id:
        die("Could not resolve ORCA task id", ORCA_FAILED)

    dispatch = orca_json([
        "orca", "orchestration", "dispatch",
        "--task", str(task_id),
        "--to", str(handle),
        "--inject",
        "--json",
    ])

    return {
        "schema_version": "0.1",
        "aaw_run_id": ctx.run_id,
        "execution_mode": "ORCA_SUPERVISED",
        "node": node,
        "fresh_session": True,
        "status": status,
        "terminal_create": terminal,
        "terminal_wait": wait,
        "task_create": task_create,
        "dispatch": dispatch,
        "resolved": {"terminal_handle": handle, "orca_task_id": task_id},
        "binding": dict(binding),
    }


def build_dryrun(task: str, ctx: RunCtx, problem: Mapping[str, Any], cap: Mapping[str, Any], binding: Mapping[str, Any], worktree: str, working_directory: Path, execution_mode: str, node: str) -> str:
    return "\n".join([
        "ADAPTIVE AI WORK V0.1 — DRY RUN",
        "",
        f"AAW_RUN_ID={ctx.run_id}",
        f"PLAYBOOK_ROOT={PLAYBOOK_ROOT}",
        f"STATS_ROOT={STATS_ROOT}",
        f"ROUTING_ROOT={ROUTING_ROOT}",
        "",
        f"PIPELINE={problem['pipeline']}",
        f"NODE={node}",
        f"TASK_CLASS={problem['task_class']}",
        f"COORDINATOR_CLASS={cap['coordinator_class']}",
        f"ROLE={cap['role']}",
        f"ABSTRACT_EFFORT={cap['abstract_effort']}",
        "",
        f"HARNESS={binding.get('harness')}",
        f"MODEL_FAMILY={binding.get('model_family')}",
        f"RUNTIME_MODEL_ID={binding.get('runtime_model_id')}",
        f"PROVIDER_EFFORT={binding.get('effort')}",
        f"LAUNCH_COMMAND={binding.get('launch_command')}",
        f"REGISTRY_VERSION={binding.get('registry_version')}",
        f"WORKTREE={worktree}",
        f"WORKING_DIRECTORY={working_directory}",
        f"EXECUTION_MODE={'DIRECT_CLI_CONTROL' if execution_mode == 'direct' else 'ORCA_SUPERVISED'}",
        "",
        "TASK:", task.strip(), "",
        "REAL LAUNCH:",
        "direct provider CLI" if execution_mode == "direct" else "terminal create -> wait tui-idle -> orchestration task-create -> dispatch --inject",
        "No worker launch occurs unless --launch is supplied.",
        "",
    ])


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Adaptive AI Work launcher/router V0.1")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--task", help="Task text")
    g.add_argument("--task-file", help="UTF-8 file containing task")
    p.add_argument("--pipeline", choices=sorted(PIPELINES), help="Explicit pipeline override")
    p.add_argument("--role", help="Explicit abstract role, e.g. CODE_IMPLEMENTER")
    p.add_argument("--classifier-cmd", help="Explicit cheap-classifier command override; receives immutable prompt+TASK on stdin; stdout must be exact JSON")
    p.add_argument("--classifier-prompt", default=str(CLASSIFIER_PROMPT_DEFAULT))
    p.add_argument("--classifier-threshold", type=float, default=CLASSIFIER_THRESHOLD_DEFAULT)
    p.add_argument("--registry", default=str(MODEL_REGISTRY_DEFAULT))
    p.add_argument("--worktree", default="active", help="ORCA worktree selector")
    p.add_argument("--working-directory", default=".", help="Direct worker working directory")
    p.add_argument("--node", choices=sorted(NODE_FILES), default="N00", help="Semantic node contract supplied to the fresh worker")
    p.add_argument("--execution-mode", choices=("direct", "orca"), default=DEFAULT_EXECUTION_MODE)
    p.add_argument("--launch-command", help="Explicit provider CLI launch command; overrides registry launch_command")
    p.add_argument("--launch", action="store_true", help="Actually launch the selected execution adapter; default is dry-run")
    p.add_argument("--print-json", action="store_true")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parser().parse_args(argv)
    if not 0 <= args.classifier_threshold <= 1:
        die("classifier threshold must be in [0,1]", INVALID_INPUT)

    task = (args.task or read_text(Path(args.task_file))).strip()
    if not task:
        die("TASK is empty", INVALID_INPUT)

    ctx = make_ctx(task)
    paths = artifact_paths(ctx)
    log(f"run_id={ctx.run_id}")

    if args.pipeline:
        problem = deterministic_problem(args.pipeline, task)
        routing_source = "explicit_pipeline_override"
    else:
        route, matched = deterministic_route(task)
        if route:
            problem = deterministic_problem(route, task)
            routing_source = "deterministic_router"
        else:
            log(f"deterministic route ambiguous: {matched or 'no match'}")
            if args.classifier_cmd:
                problem = classifier_command_override(args.classifier_cmd, Path(args.classifier_prompt), task, ctx)
                routing_source = "cheap_classifier_v1_explicit_override"
            else:
                problem = codex_classifier_fallback(Path(args.classifier_prompt), task, ctx)
                routing_source = f"cheap_classifier_v1_{DEFAULT_CLASSIFIER_BACKEND}"
            if float(problem["confidence"]) < args.classifier_threshold or problem["ambiguity"] == "high":
                candidate = ROUTING_ROOT / f"{ctx.prefix}__classifier_candidate_HUMAN_REQUIRED.json"
                write_json_new(candidate, problem)
                log(f"HUMAN_REQUIRED: classifier gate rejected; candidate={candidate}")
                return HUMAN_REQUIRED

    problem = validate_problem(problem)
    write_json_new(paths["problem"], problem)  # exact six-key JSON only

    cap = resolve_capability(problem, args.role)
    cap_record = {**cap, "run_id": ctx.run_id, "routing_source": routing_source, "routing_artifact": str(paths["problem"])}
    write_json_new(paths["capability"], cap_record)

    registry_path = Path(args.registry)
    registry = read_json(registry_path)
    binding = select_binding(registry, cap)
    binding_record = {**binding, "run_id": ctx.run_id, "registry_path": str(registry_path), "capability_artifact": str(paths["capability"])}
    write_json_new(paths["model"], binding_record)

    working_directory = Path(args.working_directory).resolve()
    write_text_new(paths["dryrun"], build_dryrun(task, ctx, problem, cap, binding, args.worktree, working_directory, args.execution_mode, args.node))

    receipt_path = None
    exit_code = OK
    if args.launch:
        if args.execution_mode == "direct":
            receipt = launch_direct(task, ctx, problem, cap, binding, working_directory, args.node)
        else:
            receipt = launch_orca_supervised(task, ctx, problem, cap, binding, args.worktree, args.launch_command, args.node)
        write_json_new(paths["launch"], receipt)
        receipt_path = str(paths["launch"])
        if receipt.get("status") == "FAILED":
            status = "WORKER_FAILED"
            exit_code = WORKER_FAILED
        else:
            status = "COMPLETED" if receipt.get("status") == "COMPLETED" else "LAUNCHED"
    else:
        status = "DRY_RUN_READY"

    result = {
        "status": status,
        "run_id": ctx.run_id,
        "pipeline": problem["pipeline"],
        "routing_source": routing_source,
        "routing_artifact": str(paths["problem"]),
        "capability_artifact": str(paths["capability"]),
        "model_binding_artifact": str(paths["model"]),
        "dry_run_artifact": str(paths["dryrun"]),
        "launch_receipt": receipt_path,
        "execution_mode": "DIRECT_CLI_CONTROL" if args.execution_mode == "direct" else "ORCA_SUPERVISED",
        "node": args.node,
        "stats_root": str(STATS_ROOT / ctx.run_id),
        "execution_id": receipt.get("execution_id") if args.launch and args.execution_mode == "direct" else None,
    }

    if args.print_json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        for k, v in result.items():
            print(f"{k.upper()}: {v}")
    return exit_code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("Interrupted")
        raise SystemExit(130)
    except SystemExit:
        raise
    except Exception as exc:
        log(f"INTERNAL ERROR: {type(exc).__name__}: {exc}")
        raise SystemExit(INTERNAL_ERROR)
