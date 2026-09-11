#!/usr/bin/env python3
"""Adaptive AI Work Control Center V0.2 (Windows, Python stdlib only)."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import uuid
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Iterable, Mapping, Sequence

from ui_components import CollapsibleSection, ScrollablePage, load_ui_state, save_ui_state, section_header

try:  # charts are optional; the GUI must still start without them
    import ui_charts
except Exception:  # noqa: BLE001
    ui_charts = None  # type: ignore[assignment]

_ANALYTICS_DIR = Path(__file__).resolve().parent / "ANALYTICS"
if str(_ANALYTICS_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYTICS_DIR))
try:  # analytics index is optional and never required for execution
    import aaw_analytics
except Exception:  # noqa: BLE001
    aaw_analytics = None  # type: ignore[assignment]

# The LOCAL_LLM adapter lives one directory up (05_AAW). Importing it is optional:
# the Control Center must still start if the adapter file is absent.
if str(Path(__file__).resolve().parent.parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:  # pragma: no cover - import guard
    import local_llm_adapter as local_llm
except Exception:  # noqa: BLE001 - never let an adapter import fail the GUI
    local_llm = None  # type: ignore[assignment]
from aaw_paths import (
    AAW_ROOT, ANALYTICS_DB_PATH, CLASSIFIER_PROMPT_PATH, CONTROL_CENTER_STATE,
    MODEL_REGISTRY_PATH, PLAYBOOK_ROOT, ROUTING_ROOT, STATS_ROOT,
)


LAUNCHER = AAW_ROOT / "aaw_run_v0_1.py"
WORKFLOW_RUNNER = AAW_ROOT / "workflow_runner.py"
CUSTOM_JOB_RUNNER = AAW_ROOT / "custom_job_runner.py"
JOBS = AAW_ROOT / "JOBS"
WORKFLOWS = AAW_ROOT / "WORKFLOWS"
PLAYBOOK = PLAYBOOK_ROOT
STATS = STATS_ROOT
ROUTING = ROUTING_ROOT
ARTIFACT_ROOTS = (ROUTING, STATS)
MODEL_REGISTRY = MODEL_REGISTRY_PATH
IMPLEMENTER_PROFILES = AAW_ROOT / "IMPLEMENTER_PROFILES.json"
STATE = CONTROL_CENTER_STATE
GUI_STATE = STATE / "gui_state.json"
MODEL_RUNTIME_STATE = STATE / "model_runtime_state.json"
QUEUES = AAW_ROOT / "CONTROL_CENTER" / "QUEUES"
QUEUE_SETTINGS = STATE / "queue_settings.json"
RECENT_WORKSPACES = STATE / "recent_workspaces.json"
APP_SETTINGS = STATE / "app_settings.json"
ANALYTICS_DB = ANALYTICS_DB_PATH
DEFAULT_WORKFLOW_ID = "IMPLEMENT_REVIEW_REPAIR_V1"
CONTROL_CENTER_VERSION = "0.4"

# Human-facing recipes. Each maps onto an existing AAW primitive; the GUI only
# translates intent -> primitive and never renames or forks the runners.
RECIPES: dict[str, dict[str, Any]] = {
    "QUICK": {
        "label": "Szybkie zadanie",
        "blurb": "Jedno jasno określone zadanie. Routing i model dobiera klasyfikator.",
        "primitive": "SINGLE_TASK",
    },
    "IMPLEMENT_VERIFY": {
        "label": "Implementacja i weryfikacja",
        "blurb": "Implementacja → testy → niezależny audyt → ewentualna naprawa. Kończy się akceptacją.",
        "primitive": "WORKFLOW",
    },
    "MULTI_PART": {
        "label": "Zmiana wieloczęściowa",
        "blurb": "Kilka powiązanych podzadań z checkpoint commitami i wspólnym audytem końcowym.",
        "primitive": "CUSTOM_JOB",
    },
    "ADVANCED": {
        "label": "Zaawansowane",
        "blurb": "Pełna konfiguracja workflow lub Custom Job — wszystkie kontrolki widoczne.",
        "primitive": "ADVANCED",
    },
}

# Curated execution presets over IMPLEMENTER_PROFILES. Not a ranking or policy.
EXECUTION_PRESETS: dict[str, dict[str, Any]] = {
    "FAST": {
        "label": "Szybki / ograniczony",
        "blurb": "Jasna praca. Implementacja Terra/high, audyt Sonnet/high, naprawa Terra/high.",
        "bindings": {"IMPLEMENT": "TERRA_HIGH", "REVIEW": "SONNET_HIGH", "REPAIR": "TERRA_HIGH"},
        "preprocess": "OFF",
    },
    "BALANCED": {
        "label": "Zrównoważony",
        "blurb": "Więcej lokalnego planowania. Implementacja Sol/medium, audyt Sonnet/high, naprawa Sol/medium.",
        "bindings": {"IMPLEMENT": "SOL_MEDIUM", "REVIEW": "SONNET_HIGH", "REPAIR": "SOL_MEDIUM"},
        "preprocess": "OFF",
    },
    "DEEP": {
        "label": "Dogłębny",
        "blurb": "Trudne rozumowanie. Implementacja Sol/high, audyt Opus/high, naprawa Sol/high.",
        "bindings": {"IMPLEMENT": "SOL_HIGH", "REVIEW": "OPUS_HIGH", "REPAIR": "SOL_HIGH"},
        "preprocess": "OFF",
    },
    "LOCAL_ASSIST": {
        "label": "Ze wsparciem lokalnym",
        "blurb": "Jak Zrównoważony, plus bezpieczny preprocessing lokalnym Qwen (AUTO_SAFE).",
        "bindings": {"IMPLEMENT": "SOL_MEDIUM", "REVIEW": "SONNET_HIGH", "REPAIR": "SOL_MEDIUM"},
        "preprocess": "AUTO_SAFE",
    },
}


class AppSettings:
    """Long-lived operator preferences (parametrisation, not execution policy)."""

    DEFAULTS: dict[str, Any] = {
        "default_recipe": "IMPLEMENT_VERIFY",
        "default_execution_preset": "BALANCED",
        "default_preprocess_policy": "AUTO_SAFE",
        "remember_last_workspace": True,
        "analytics_auto_refresh_on_open": True,
    }

    def __init__(self, path: Path = APP_SETTINGS) -> None:
        self.path = path

    def load(self) -> dict[str, Any]:
        saved = read_json_object(self.path)
        merged = dict(self.DEFAULTS)
        for key, value in saved.items():
            if key in self.DEFAULTS:
                merged[key] = value
        if merged["default_recipe"] not in RECIPES:
            merged["default_recipe"] = self.DEFAULTS["default_recipe"]
        if merged["default_execution_preset"] not in EXECUTION_PRESETS:
            merged["default_execution_preset"] = self.DEFAULTS["default_execution_preset"]
        return merged

    def save(self, values: Mapping[str, Any]) -> dict[str, Any]:
        merged = self.load() | {k: v for k, v in values.items() if k in self.DEFAULTS}
        write_json_atomic(self.path, merged)
        return self.load()  # re-validate on the way out
PLAYBOOK_VERSION = "0.3"
MODEL_RUNTIME_SCHEMA_VERSION = 1
CLASSIFIER_MODEL = "gpt-5.6-luna"
CLASSIFIER_EFFORT = "none"

EXIT_STATUS = {0: "DRY_RUN_READY", 20: "HUMAN_REQUIRED", 21: "BLOCKED", 25: "ORCA_FAILED", 26: "WORKER_FAILED"}
PIPELINE_LABELS = {
    "P01": "RESEARCH / EXPERIMENT",
    "P02": "GREENFIELD APPLICATION",
    "P03": "REUSE / REPLACEMENT",
    "P04": "BOUNDED CHANGE",
    "P05": "DEBUG / REPAIR",
    "P06": "DESIGN TO CODE",
}
CAPABILITY_LABELS = {
    "C0": "Direct execution — no separate coordinator",
    "C1": "Light coordination",
    "C2": "Strong coordinator",
}
RUN_KEYS = ("aaw_run_id", "run_id", "AAW_RUN_ID")
PROFILE_STATES = {"VERIFIED", "AVAILABLE", "UNAVAILABLE", "NOT_CHECKED", "UNKNOWN"}
QUEUE_STATUSES = {"IDLE", "RUNNING", "PAUSED", "WAITING_FOR_HUMAN", "COMPLETED", "BLOCKED", "WAITING"}
TASK_STATUSES = {"WAITING", "RUNNING", "PASS", "FAIL", "BLOCKED", "WAITING_FOR_HUMAN", "CANCELLED", "INTERRUPTED"}
QUEUE_STOP_RESULTS = {"FAIL", "BLOCKED", "INVALID", "WAITING_FOR_HUMAN", "WAITING_FOR_PLAN_APPROVAL", "WAITING_FOR_REPAIR_SELECTION", "WORKFLOW_LIMIT_REACHED", "HUMAN_REQUIRED", "CANCELLED", "REJECTED"}


def run_process(argv: Sequence[str], timeout: float = 15.0) -> tuple[int, str, str]:
    """Run a bounded command without a shell; preflight callers are read-only."""
    try:
        environment = dict(os.environ); environment.pop("TERM", None)
        completed = subprocess.run(
            list(argv), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", shell=False,
            timeout=timeout, env=environment,
        )
        return completed.returncode, completed.stdout, completed.stderr
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 70, "", f"{type(exc).__name__}: {exc}"


def run_workflow_process(argv: Sequence[str]) -> tuple[int, str, str, list[str]]:
    try:
        completed = subprocess.run(
            list(argv), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", shell=False,
        )
        return completed.returncode, completed.stdout, completed.stderr, list(argv)
    except OSError as exc:
        return 70, "", f"{type(exc).__name__}: {exc}", list(argv)


def workflow_argv(workflow: str, goal: str, repo: str, worktree: str, dry_run: bool, bindings: Sequence[str] = (), preprocess_policy: str = "AUTO_SAFE") -> list[str]:
    argv = [sys.executable, str(WORKFLOW_RUNNER), "--workflow", workflow, "--goal", goal, "--repo", repo, "--worktree", worktree]
    for binding in bindings:
        argv.extend(("--bind", binding))
    argv.extend(("--preprocess-policy", preprocess_policy))
    if dry_run:
        argv.append("--dry-run")
    return argv


def custom_job_argv(job: str, dry_run: bool) -> list[str]:
    argv = [sys.executable, str(CUSTOM_JOB_RUNNER), "--job", job]
    if dry_run:
        argv.append("--dry-run")
    return argv


def git_worktree_paths(repository: str) -> list[str]:
    path = Path(repository.strip()) if repository.strip() else None
    if not path or not path.is_dir():
        return []
    rc, stdout, _stderr = run_process(["git", "-C", str(path), "worktree", "list", "--porcelain"])
    if rc != 0:
        return []
    return [line[9:] for line in stdout.splitlines() if line.startswith("worktree ")]


def load_recent_workspaces(path: Path = RECENT_WORKSPACES) -> dict[str, list[str]]:
    value = read_json_object(path)
    repositories = [str(item) for item in value.get("repositories", []) if isinstance(item, str)]
    worktrees = [str(item) for item in value.get("worktrees", []) if isinstance(item, str)]
    gui_state = read_json_object(GUI_STATE)
    repositories.extend(str(gui_state.get(key, "")) for key in ("repo", "repository"))
    worktrees.extend(str(gui_state.get(key, "")) for key in ("worktree", "cwd"))
    for queue_path in sorted(QUEUES.glob("*.json")):
        queue = read_json_object(queue_path)
        for task in queue.get("tasks", []) if isinstance(queue.get("tasks"), list) else []:
            if isinstance(task, Mapping):
                repositories.append(str(task.get("repository") or task.get("repo") or ""))
                worktrees.append(str(task.get("worktree") or ""))
    def existing_unique(items: list[str], limit: int) -> list[str]:
        output: list[str] = []
        for item in items:
            if not item or not Path(item).is_dir():
                continue
            normalized = str(Path(item).resolve())
            if all(os.path.normcase(normalized) != os.path.normcase(old) for old in output):
                output.append(normalized)
        return output[:limit]
    return {
        "repositories": existing_unique(repositories, 20),
        "worktrees": existing_unique(worktrees, 30),
    }


def remember_workspace(repository: str = "", worktree: str = "", path: Path = RECENT_WORKSPACES) -> dict[str, list[str]]:
    state = load_recent_workspaces(path)
    for key, raw in (("repositories", repository), ("worktrees", worktree)):
        text = str(raw).strip()
        if text and Path(text).is_dir():
            normalized = str(Path(text).resolve())
            state[key] = [normalized] + [item for item in state[key] if os.path.normcase(item) != os.path.normcase(normalized)]
            state[key] = state[key][:20 if key == "repositories" else 30]
    write_json_atomic(path, state)
    return state


def load_implementer_profiles_for_ui() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(IMPLEMENTER_PROFILES.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    profiles = data.get("profiles") if isinstance(data, Mapping) else None
    if not isinstance(profiles, list):
        return {}
    return {str(item["profile_id"]): dict(item) for item in profiles if isinstance(item, Mapping) and isinstance(item.get("profile_id"), str)}


def read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


def write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def ensure_codex_cli_on_path() -> str | None:
    """Resolve the bundled Codex CLI once for GUI processes started by Explorer."""
    found = shutil.which("codex")
    if found:
        return found
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        return None
    root = Path(local_app_data) / "OpenAI" / "Codex" / "bin"
    candidates = sorted(root.glob("*/codex.exe"), key=lambda path: path.stat().st_mtime, reverse=True) if root.is_dir() else []
    if not candidates:
        return None
    directory = str(candidates[0].parent)
    os.environ["PATH"] = directory + os.pathsep + os.environ.get("PATH", "")
    return shutil.which("codex")


def profile_catalog_version() -> str:
    value = read_json_object(IMPLEMENTER_PROFILES)
    return str(value.get("profile_catalog_version") or "UNKNOWN")


def get_runtime_profile_state(
    profile_id: str,
    catalog: Mapping[str, Mapping[str, Any]],
    runtime: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return the one authoritative, non-probing runtime state for a stable profile ID."""
    profile = catalog.get(profile_id, {})
    harness = str(profile.get("harness") or "").lower()
    model_id = str(profile.get("runtime_model_id") or "").strip()
    effort = str(profile.get("effort") or "")
    checked_at = str(runtime.get("checked_at") or "") if isinstance(runtime, Mapping) else ""
    base: dict[str, Any] = {
        "profile_id": profile_id,
        "state": "UNKNOWN",
        "reason": "Profile is not present in IMPLEMENTER_PROFILES.json",
        "harness": harness,
        "runtime_model_id": model_id or None,
        "effort": effort,
        "checked_at": checked_at or None,
    }
    if not profile:
        return base
    catalog_state = str(profile.get("availability") or "UNKNOWN").upper()
    if catalog_state == "KNOWN_BUT_UNAVAILABLE":
        base.update(state="UNAVAILABLE", reason=str(profile.get("unavailable_reason") or "Runtime is not configured"))
        return base
    if not model_id:
        base.update(state="UNAVAILABLE", reason=str(profile.get("unavailable_reason") or "Runtime model ID is not configured"))
        return base
    if runtime is None:
        base.update(state="NOT_CHECKED", reason="Local runtime has not been checked")
        return base
    profile_rows = runtime.get("profiles", {}) if isinstance(runtime.get("profiles"), Mapping) else {}
    saved = profile_rows.get(profile_id, {}) if isinstance(profile_rows.get(profile_id), Mapping) else {}
    if saved and str(saved.get("profile_id") or profile_id) == profile_id:
        state = str(saved.get("state") or "UNKNOWN").upper()
        state = state if state in PROFILE_STATES else "UNKNOWN"
        base.update(state=state, reason=str(saved.get("reason") or "Runtime state is unknown"))
        return base
    harnesses = runtime.get("harnesses", {}) if isinstance(runtime.get("harnesses"), Mapping) else {}
    row = harnesses.get(harness, {}) if isinstance(harnesses.get(harness), Mapping) else {}
    state = str(row.get("state") or "UNKNOWN").upper()
    if state == "UNAVAILABLE":
        base.update(state="UNAVAILABLE", reason=str(row.get("reason") or f"{harness.title()} CLI not configured"))
        return base
    if state not in {"AVAILABLE", "VERIFIED"}:
        base.update(state="UNKNOWN" if state == "UNKNOWN" else "NOT_CHECKED", reason=str(row.get("reason") or "Runtime state is unknown"))
        return base
    verified = catalog_state == "VERIFIED"
    base.update(
        state="VERIFIED" if verified else "AVAILABLE",
        reason=str(row.get("reason") or ("Profile evidence and local runtime verified" if verified else "Local runtime available; model profile not actively probed")),
    )
    return base


def load_model_runtime_snapshot(path: Path, catalog: Mapping[str, Mapping[str, Any]]) -> dict[str, Any] | None:
    snapshot = read_json_object(path)
    if not snapshot:
        return None
    if snapshot.get("snapshot_schema_version") != MODEL_RUNTIME_SCHEMA_VERSION:
        return None
    if str(snapshot.get("profile_catalog_version")) != profile_catalog_version():
        return None
    rows = snapshot.get("profiles")
    if not isinstance(rows, Mapping) or any(profile_id not in rows for profile_id in catalog):
        return None
    return snapshot


def process_exists(pid: Any) -> bool:
    try:
        numeric = int(pid)
        if numeric <= 0:
            return False
        if os.name == "nt":
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, numeric)
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        os.kill(numeric, 0)
        return True
    except (TypeError, ValueError, OSError):
        return False


def queue_decision(result_status: str, *, auto_continue: bool, pause_after_current: bool, remaining_waiting: bool) -> tuple[str, bool]:
    """Return (queue status, dispatch next) without retries, skips or rerouting."""
    status = result_status.upper()
    if status in {"WAITING_FOR_HUMAN", "WAITING_FOR_PLAN_APPROVAL", "WAITING_FOR_REPAIR_SELECTION", "HUMAN_REQUIRED", "HUMAN_GATE"}:
        return "WAITING_FOR_HUMAN", False
    if status in {"BLOCKED", "INVALID", "WORKFLOW_LIMIT_REACHED"}:
        return "BLOCKED", False
    if status in {"FAIL", "CANCELLED", "REJECTED"}:
        return "PAUSED", False
    if status != "PASS":
        return "PAUSED", False
    if pause_after_current or not auto_continue:
        return "IDLE", False
    if remaining_waiting:
        return "IDLE", True
    return "COMPLETED", False


class QueueStore:
    """Dependency-free operator backlog persistence; not Playbook authority."""

    def __init__(self, root: Path = QUEUES, settings_path: Path = QUEUE_SETTINGS) -> None:
        self.root = root
        self.settings_path = settings_path
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def now() -> str:
        return dt.datetime.now().astimezone().isoformat(timespec="seconds")

    @staticmethod
    def _id(prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex[:12]}"

    def settings(self) -> dict[str, Any]:
        saved = read_json_object(self.settings_path)
        maximum = saved.get("max_simultaneous_active_queues", 1)
        try:
            maximum = max(1, min(3, int(maximum)))
        except (TypeError, ValueError):
            maximum = 1
        return {
            "max_simultaneous_active_queues": maximum,
            "default_auto_continue": bool(saved.get("default_auto_continue", True)),
            "default_workflow": str(saved.get("default_workflow") or ""),
            "default_implement_profile": str(saved.get("default_implement_profile") or "TERRA_HIGH"),
            "default_review_profile": str(saved.get("default_review_profile") or "SOL_HIGH"),
            "default_repair_profile": str(saved.get("default_repair_profile") or "SOL_MEDIUM"),
        }

    def save_settings(self, settings: Mapping[str, Any]) -> dict[str, Any]:
        merged = self.settings() | dict(settings)
        maximum = max(1, min(3, int(merged.get("max_simultaneous_active_queues", 1))))
        merged["max_simultaneous_active_queues"] = maximum
        write_json_atomic(self.settings_path, merged)
        return merged

    def path_for(self, queue_id: str) -> Path:
        safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", queue_id)
        return self.root / f"{safe}.json"

    def save(self, document: Mapping[str, Any]) -> dict[str, Any]:
        value = dict(document)
        queue_id = str(value.get("queue_id") or "")
        if not queue_id:
            raise ValueError("queue_id is required")
        write_json_atomic(self.path_for(queue_id), value)
        return value

    def load_all(self, recover: bool = True) -> list[dict[str, Any]]:
        queues: list[dict[str, Any]] = []
        for path in sorted(self.root.glob("queue_*.json")):
            item = read_json_object(path)
            if not item or not isinstance(item.get("tasks", []), list):
                continue
            changed = False
            if recover:
                for task in item.get("tasks", []):
                    if isinstance(task, dict) and task.get("status") == "RUNNING" and not process_exists(task.get("process_pid")):
                        task["status"] = "INTERRUPTED"
                        task["finished_at"] = None
                        changed = True
                if item.get("status") == "RUNNING" or changed:
                    item["status"] = "PAUSED"
                    item["recovery_reason"] = "Interrupted by Control Center restart; human decision required"
                    changed = True
            if changed:
                write_json_atomic(path, item)
            queues.append(item)
        return queues

    def create(self, display_name: str, auto_continue: bool | None = None) -> dict[str, Any]:
        name = display_name.strip()
        if not name:
            raise ValueError("Queue name is required")
        queue_id = self._id("queue")
        item = {
            "schema_version": "AAW_QUEUE_V0.1",
            "queue_id": queue_id,
            "display_name": name,
            "created_at": self.now(),
            "status": "IDLE",
            "auto_continue": self.settings()["default_auto_continue"] if auto_continue is None else bool(auto_continue),
            "pause_after_current": False,
            "created_by": "HUMAN",
            "tasks": [],
        }
        return self.save(item)

    def add_task(self, item: dict[str, Any], *, title: str, goal: str, mode: str, workflow_id: str = "", job_spec: str = "", repo: str = "", worktree: str = "", bindings: Mapping[str, str] | None = None) -> dict[str, Any]:
        if mode not in {"SINGLE_TASK", "WORKFLOW", "CUSTOM_JOB"}:
            raise ValueError("mode must be SINGLE_TASK, WORKFLOW or CUSTOM_JOB")
        if not title.strip() or not goal.strip():
            raise ValueError("Task title and goal are required")
        tasks = item.setdefault("tasks", [])
        task = {
            "task_id": self._id("task"), "title": title.strip(), "goal": goal.strip(), "mode": mode,
            "workflow_id": workflow_id, "job_spec": job_spec, "repo": repo, "worktree": worktree,
            "bindings": dict(bindings or {}), "resolved_bindings": {}, "status": "WAITING",
            "position": len(tasks) + 1, "created_at": self.now(), "started_at": None,
            "finished_at": None, "run_id": None, "result_status": None, "created_by": "HUMAN",
            "process_pid": None,
        }
        tasks.append(task)
        if item.get("status") == "COMPLETED":
            item["status"] = "IDLE"
        self.save(item)
        return task

    def reorder(self, item: dict[str, Any], task_id: str, delta: int) -> bool:
        tasks = item.get("tasks", [])
        index = next((i for i, task in enumerate(tasks) if task.get("task_id") == task_id), -1)
        target = index + delta
        if index < 0 or target < 0 or target >= len(tasks) or tasks[index].get("status") == "RUNNING" or tasks[target].get("status") == "RUNNING":
            return False
        tasks[index], tasks[target] = tasks[target], tasks[index]
        for position, task in enumerate(tasks, 1):
            task["position"] = position
        self.save(item)
        return True

    def delete_waiting(self, item: dict[str, Any], task_id: str) -> bool:
        tasks = item.get("tasks", [])
        target = next((task for task in tasks if task.get("task_id") == task_id), None)
        if not target or target.get("status") != "WAITING":
            return False
        tasks.remove(target)
        for position, task in enumerate(tasks, 1):
            task["position"] = position
        self.save(item)
        return True


def workflow_timeline(data: Mapping[str, Any]) -> str:
    rows = [f"{data.get('workflow_id', 'UNKNOWN')}  |  {data.get('status', 'UNKNOWN')}", ""]
    for item in data.get("completed_nodes", []):
        implementer = item.get("implementer_profile") or item.get("model") or "NO_LLM"
        effort = item.get("effort") or "—"
        rows.append(f"{item.get('node_id', '?'):4} {item.get('node_type', '?'):14} {item.get('outcome', '?'):8}  {implementer} / {effort}  {item.get('duration_s') if item.get('duration_s') is not None else '—'}s")
    current = data.get("current_node")
    if current and not any(item.get("node_id") == current and item.get("node_type") == "HUMAN_GATE" for item in data.get("completed_nodes", [])):
        rows.append(f"{current:4} {'CURRENT':14} {data.get('status', 'WAITING')}")
    if data.get("status") == "WAITING_FOR_HUMAN":
        bindings = data.get("workflow_bindings", {})
        if isinstance(bindings, Mapping):
            rows.extend(("", "HUMAN GATE SUMMARY"))
            for label, node_type in (("IMPLEMENTATION PROFILE", "IMPLEMENT"), ("REPAIR PROFILE", "REPAIR"), ("REVIEW PROFILE", "REVIEW")):
                values = [f"{node_id}: {binding.get('profile', 'UNKNOWN')} / {binding.get('effort', '—')}" for node_id, binding in bindings.items() if isinstance(binding, Mapping) and binding.get("role") == node_type]
                rows.append(f"{label}: {'; '.join(values) if values else 'NOT_RUN_OR_NOT_APPLICABLE'}")
        summary = data.get("workflow_summary", {})
        if isinstance(summary, Mapping):
            rows.append(f"LLM calls: {summary.get('llm_calls', '—')} | tokens in/out: {summary.get('input_tokens_total', '—')}/{summary.get('output_tokens_total', '—')} | wall time: {summary.get('wall_time_total', '—')}s")
    return "\n".join(rows)


def parse_json_flex(text: str) -> dict[str, Any]:
    text = text.strip()
    if not text:
        return {}
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                value = json.loads(text[start : end + 1])
                return value if isinstance(value, dict) else {}
            except json.JSONDecodeError:
                pass
    return {}


def latest_orca_state(state_root: Path = STATE) -> dict[str, Any]:
    if not state_root.is_dir():
        return {}
    candidates = sorted(state_root.glob("orca_preflight_*.json"), reverse=True)
    for path in candidates:
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            return value
    return {}


LOCAL_LLM_PROFILE_IDS = ("LOCAL_QWEN_FAST", "LOCAL_QWEN_JSON", "LOCAL_QWEN_DELTA", "LOCAL_QWEN_SUMMARY")


def probe_local_llm_runtime() -> dict[str, Any]:
    """
    Read-only probe of the AnythingLLM-managed local Qwen runtime.

    Never starts or stops AnythingLLM. GET /api/version + /api/tags only - no
    inference call. Returns a harness-shaped row: state in
    AVAILABLE / UNAVAILABLE / NOT_CHECKED plus a human reason.
    """
    if local_llm is None:
        return {"state": "NOT_CHECKED", "reason": "local_llm_adapter.py not importable",
                "endpoint": None, "model": None}
    try:
        soft = local_llm.precheck_soft()
    except Exception as exc:  # noqa: BLE001
        return {"state": "UNAVAILABLE", "reason": f"probe error: {exc}",
                "endpoint": getattr(local_llm, "ENDPOINT", None), "model": getattr(local_llm, "LOCAL_MODEL_ID", None)}
    if soft.get("ok"):
        return {
            "state": "AVAILABLE",
            "reason": f"Local Qwen runtime responds (Ollama {soft.get('runtime_version')})",
            "endpoint": soft.get("endpoint"),
            "model": soft.get("model"),
            "model_loaded": soft.get("model_loaded"),
            "bind_check": soft.get("bind_check", {}).get("state"),
            "anythingllm_required": True,
        }
    code = str(soft.get("state") or "LOCAL_LLM_UNAVAILABLE")
    reason = {
        "LOCAL_LLM_UNAVAILABLE": "Local runtime not running (start AnythingLLM Desktop)",
        "LOCAL_LLM_UNSAFE_BIND": "BLOCKED: a non-loopback listener answers on :11434",
        "LOCAL_LLM_TIMEOUT": "Local runtime did not answer in time",
    }.get(code, soft.get("reason") or code)
    return {"state": "UNAVAILABLE", "reason": reason, "endpoint": soft.get("endpoint"),
            "model": soft.get("model"), "anythingllm_required": True, "code": code}


def run_preflight_checks() -> dict[str, Any]:
    """Deterministic filesystem/process checks only; never starts a worker."""
    registry: dict[str, Any] = {}
    registry_error = ""
    try:
        value = json.loads(MODEL_REGISTRY.read_text(encoding="utf-8-sig"))
        if isinstance(value, dict):
            registry = value
        else:
            registry_error = "root is not an object"
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        registry_error = f"{type(exc).__name__}: {exc}"

    ensure_codex_cli_on_path()
    codex_path = shutil.which("codex")
    codex_rc, codex_out, codex_err = run_process(["codex", "--version"]) if codex_path else (70, "", "Codex CLI not configured")
    claude_path = shutil.which("claude")
    if not claude_path:
        candidate = Path.home() / ".local" / "bin" / "claude.exe"
        claude_path = str(candidate) if candidate.is_file() else None
    claude_rc, claude_out, claude_err = run_process([claude_path, "--version"]) if claude_path else (70, "", "Claude CLI not configured")
    orca_version_rc, orca_version_out, orca_version_err = run_process(["orca", "--version"])
    orca_rc, orca_out, orca_err = run_process(["orca", "status", "--json"])
    help_rc, help_out, help_err = run_process(["orca", "orchestration", "worker-start", "--help"])
    dispatch_rc, dispatch_out, dispatch_err = run_process(["orca", "orchestration", "dispatch", "--help"])
    check_rc, check_out, check_err = run_process(["orca", "orchestration", "check", "--help"])
    hooks_rc, hooks_out, hooks_err = run_process(["orca", "agent", "hooks", "status", "--json"])
    status_doc = parse_json_flex(orca_out)
    help_text = help_out + "\n" + help_err
    hooks_doc = parse_json_flex(hooks_out)

    status_result = status_doc.get("result", {}) if isinstance(status_doc.get("result"), Mapping) else {}
    runtime = status_result.get("runtime", {}) if isinstance(status_result.get("runtime"), Mapping) else {}
    graph = status_result.get("graph", {}) if isinstance(status_result.get("graph"), Mapping) else {}
    capabilities = runtime.get("capabilities", []) if isinstance(runtime.get("capabilities"), list) else []
    runtime_ready = bool(
        orca_rc == 0 and runtime.get("reachable") is True
        and runtime.get("state") == "ready" and graph.get("state") == "ready"
    )
    orchestration_available = bool(
        help_rc == 0 and "orchestration.contract.v1" in capabilities
        and "--task" in help_text and "--agent" in help_text
    )

    hook_result = hooks_doc.get("result", {}) if isinstance(hooks_doc.get("result"), Mapping) else {}
    hook_rows = hook_result.get("statuses", []) if isinstance(hook_result.get("statuses"), list) else []
    agent_states = {
        str(row.get("agent")): str(row.get("state"))
        for row in hook_rows if isinstance(row, Mapping) and row.get("agent")
    }
    last = latest_orca_state()
    current_orca_version = str(runtime.get("appVersion") or ((orca_version_out or orca_version_err).strip() if orca_version_rc == 0 else "UNKNOWN"))
    last_verified = str(last.get("last_smoke_tested_orca_version") or last.get("orca_version") or "NOT RECORDED")
    last_verdict = str(last.get("last_smoke_verdict") or last.get("recommendation") or "NOT RECORDED")
    last_timestamp = str(last.get("last_smoke_timestamp") or last.get("checked_at") or "NOT RECORDED")
    version_changed = last_verified not in ("NOT RECORDED", "UNKNOWN", current_orca_version)

    checked_at = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    local_llm_row = probe_local_llm_runtime()
    runtime_state = {
        "snapshot_schema_version": MODEL_RUNTIME_SCHEMA_VERSION,
        "profile_catalog_version": profile_catalog_version(),
        "checked_at": checked_at,
        "probe_policy": "CLI_ONLY_NO_MODEL_PROBE",
        "local_runtime_probe_policy": "HTTP_VERSION_AND_TAGS_NO_INFERENCE",
        "harnesses": {
            "ollama_openai_compat": {
                "state": local_llm_row["state"],
                "executable": None,
                "endpoint": local_llm_row.get("endpoint"),
                "version": "",
                "reason": local_llm_row["reason"],
            },
            "codex": {
                "state": "AVAILABLE" if codex_path and codex_rc == 0 else "UNAVAILABLE",
                "executable": codex_path,
                "version": (codex_out or codex_err).strip(),
                "reason": "Codex CLI responds" if codex_path and codex_rc == 0 else "Codex CLI not configured or not responding",
            },
            "claude": {
                "state": "AVAILABLE" if claude_path and claude_rc == 0 else "UNAVAILABLE",
                "executable": claude_path,
                "version": (claude_out or claude_err).strip(),
                "reason": "Claude CLI responds; per-model state comes from MODEL_CATALOG" if claude_path and claude_rc == 0 else "Claude CLI not configured",
            },
        },
    }
    profiles = load_implementer_profiles_for_ui()
    runtime_state["profiles"] = {
        profile_id: get_runtime_profile_state(profile_id, profiles, runtime_state)
        for profile_id in profiles
    }

    return {
        "checked_at": checked_at,
        "launcher": "OK" if LAUNCHER.is_file() else "FAIL",
        "playbook_root": "OK" if PLAYBOOK.is_dir() else "FAIL",
        "stats_root": "OK" if STATS.is_dir() else "WARN",
        "routing_root": "OK" if ROUTING.is_dir() else "WARN",
        "model_registry": "OK" if registry and not registry_error else "FAIL",
        "model_registry_error": registry_error,
        "python_version": platform.python_version(),
        "codex_version": (codex_out or codex_err).strip() if codex_rc == 0 else "UNAVAILABLE",
        "codex_executable": codex_path or "NOT FOUND",
        "cheap_classifier": f"{CLASSIFIER_MODEL} / {CLASSIFIER_EFFORT}",
        "orca_version": current_orca_version,
        "orca_runtime": "READY" if runtime_ready else "NOT READY",
        "orca_orchestration": "AVAILABLE" if orchestration_available else "UNAVAILABLE",
        "registry_version": str(registry.get("registry_version") or "UNKNOWN"),
        "launcher_version": CONTROL_CENTER_VERSION,
        "playbook_version": PLAYBOOK_VERSION,
        "worker_start": help_rc == 0,
        "worker_start_agent": "--agent" in help_text,
        "worker_start_model": "--model" in help_text,
        "worker_start_effort": "--effort" in help_text,
        "dispatch_inject": dispatch_rc == 0 and "--inject" in (dispatch_out + dispatch_err),
        "check_wait": check_rc == 0 and "--wait" in (check_out + check_err),
        "codex_detected": agent_states.get("codex") == "installed" and hooks_rc == 0,
        "claude_detected": agent_states.get("claude") == "installed" and hooks_rc == 0,
        "nested_worker_depth": "NOT VERIFIED",
        "last_verified_orca_version": last_verified,
        "last_smoke_tested_orca_version": last_verified,
        "last_smoke_verdict": last_verdict,
        "last_smoke_timestamp": last_timestamp,
        "version_changed": version_changed,
        "smoke_test": str(last.get("smoke_test") or "NOT RUN"),
        "smoke": last,
        "model_runtime": runtime_state,
        "diagnostics": {
            "orca_status_rc": orca_rc,
            "orca_status_stderr": orca_err.strip(),
            "worker_help_rc": help_rc,
            "dispatch_help_rc": dispatch_rc,
            "check_help_rc": check_rc,
            "hooks_rc": hooks_rc,
            "hooks_stderr": hooks_err.strip(),
        },
    }


@dataclass
class Artifact:
    path: Path
    source: str
    mtime: float
    run_id: str
    task_slug: str
    data: Any = None
    error: str = ""


def nested_find(obj: Any, names: Iterable[str]) -> Any:
    wanted = set(names)
    if isinstance(obj, Mapping):
        for key, value in obj.items():
            if key in wanted and value not in (None, ""):
                return value
        for value in obj.values():
            found = nested_find(value, wanted)
            if found not in (None, ""):
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = nested_find(value, wanted)
            if found not in (None, ""):
                return found
    return None


def filename_metadata(path: Path) -> tuple[str, str]:
    name = path.name
    match = re.match(r"^(\d{8}_\d{6})__(.*?)__([0-9a-fA-F]{8})(?:__|\.)", name)
    if match:
        stamp, slug, run8 = match.groups()
        return f"AAW_{stamp}_{run8.lower()}", slug
    match = re.search(r"(AAW_\d{8}_\d{6}_[0-9a-fA-F]{8})", str(path))
    return (match.group(1), "") if match else ("", "")


def load_artifact(path: Path, source: str) -> Artifact:
    fallback_run, slug = filename_metadata(path)
    data: Any = None
    error = ""
    run_id = ""
    try:
        text = path.read_text(encoding="utf-8-sig")
        if path.suffix.lower() == ".json":
            data = json.loads(text)
            value = nested_find(data, RUN_KEYS)
            if value is not None:
                run_id = str(value)
        else:
            match = re.search(r"(?m)^AAW_RUN_ID\s*[:=]\s*(\S+)", text)
            if match:
                run_id = match.group(1)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        error = f"{type(exc).__name__}: {exc}"
    return Artifact(path, source, path.stat().st_mtime, run_id or fallback_run, slug, data, error)


def scan_artifacts(roots: Sequence[Path] = ARTIFACT_ROOTS) -> tuple[list[Artifact], list[str]]:
    artifacts: list[Artifact] = []
    warnings: list[str] = []
    for root in roots:
        if not root.is_dir():
            warnings.append(f"Missing artifact directory: {root}")
            continue
        source = "Routing" if root == ROUTING or "ROUTING" in root.name.upper() else "Stats"
        try:
            paths = [p for p in root.rglob("*") if p.is_file()]
        except OSError as exc:
            warnings.append(f"Cannot scan {root}: {exc}")
            continue
        for path in paths:
            try:
                artifacts.append(load_artifact(path, source))
            except OSError as exc:
                warnings.append(f"Cannot inspect {path}: {exc}")
    artifacts.sort(key=lambda item: item.mtime, reverse=True)
    return artifacts, warnings


def group_artifacts(artifacts: Sequence[Artifact]) -> dict[str, list[Artifact]]:
    groups: dict[str, list[Artifact]] = {}
    for artifact in artifacts:
        key = artifact.run_id or f"UNASSIGNED::{artifact.path.stem}"
        groups.setdefault(key, []).append(artifact)
    return groups


def validate_task(task: str) -> str:
    task = task.strip()
    if not task:
        raise ValueError("TASK cannot be empty.")
    return task


def confirm_orca_launch(parent: tk.Misc, version_changed: bool = False) -> bool:
    """Explicit two-choice warning; never called for Dry Run or direct execution."""
    result = {"launch": False}
    dialog = tk.Toplevel(parent)
    dialog.title("Experimental ORCA execution")
    dialog.transient(parent)
    dialog.grab_set()
    drift = "\n\nORCA VERSION CHANGED — PREFLIGHT / SMOKE TEST RECOMMENDED" if version_changed else ""
    message = (
        "ORCA 1.4.194 failed the last AAW smoke test.\n\n"
        "Failure:\nagent_prompt_stalled\n\n"
        "Worker terminal remained alive after dispatch failure.\n\n"
        "Recommended execution:\nDIRECT CLI\n\n"
        f"Continue with ORCA anyway?{drift}"
    )
    ttk.Label(dialog, text=message, justify="left", padding=14).grid(row=0, column=0, columnspan=2)

    def close(value: bool) -> None:
        result["launch"] = value
        dialog.destroy()

    ttk.Button(dialog, text="Cancel", command=lambda: close(False)).grid(row=1, column=0, padx=8, pady=(0, 12))
    ttk.Button(dialog, text="Launch ORCA anyway", command=lambda: close(True)).grid(row=1, column=1, padx=8, pady=(0, 12))
    dialog.protocol("WM_DELETE_WINDOW", lambda: close(False))
    dialog.wait_window()
    return result["launch"]


def build_launcher_argv(task: str, launch: bool = False, pipeline: str = "", execution_mode: str = "direct", working_directory: str = "") -> list[str]:
    argv = [sys.executable, str(LAUNCHER), "--task", validate_task(task), "--print-json"]
    if working_directory:
        argv.extend(("--working-directory", working_directory))
    if pipeline:
        if pipeline not in PIPELINE_LABELS:
            raise ValueError(f"Unknown pipeline: {pipeline}")
        argv.extend(("--pipeline", pipeline))
    if launch:
        argv.extend(("--launch", "--execution-mode", execution_mode))
    return argv


def run_launcher(task: str, launch: bool = False, pipeline: str = "", execution_mode: str = "direct", working_directory: str = "") -> tuple[int, str, str, list[str]]:
    argv = build_launcher_argv(task, launch, pipeline, execution_mode, working_directory)
    try:
        completed = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
        )
        return completed.returncode, completed.stdout, completed.stderr, argv
    except OSError as exc:
        return 70, "", f"Cannot start launcher: {exc}", argv


def parse_launcher_result(stdout: str) -> dict[str, Any]:
    text = stdout.strip()
    if not text:
        return {}
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                value = json.loads(text[start : end + 1])
                return value if isinstance(value, dict) else {}
            except json.JSONDecodeError:
                pass
    return {}


def status_for(returncode: int, result: Mapping[str, Any]) -> str:
    if returncode == 0:
        return str(result.get("status") or "DRY_RUN_READY")
    return EXIT_STATUS.get(returncode, "FAILED")


def deterministic_interpretation(data: Mapping[str, Any], status: str = "") -> str:
    parts: list[str] = []
    pipeline = nested_find(data, ("pipeline",))
    capability = nested_find(data, ("coordinator_class", "capability"))
    if pipeline in PIPELINE_LABELS:
        parts.append(f"{pipeline} — {PIPELINE_LABELS[pipeline]}")
    if capability in CAPABILITY_LABELS:
        parts.append(f"{capability} — {CAPABILITY_LABELS[capability]}")
    if status == "HUMAN_REQUIRED":
        parts.append("Routing confidence insufficient — human decision required")
    return " | ".join(parts)


KNOWN_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Problem definition", ("pipeline", "task_class", "ambiguity", "risk", "recommended_coordinator_capability", "confidence")),
    ("Capability binding", ("coordinator_class", "role", "abstract_effort", "separate_coordinator")),
    ("Model binding", ("harness", "model_family", "runtime_model_id", "effort", "confidence", "registry_version")),
    ("Execution receipt", ("execution_mode", "status", "node", "provider_session_id", "orca_task_id", "orca_run_id", "terminal_handle", "terminal_id", "requested", "effective", "errors")),
    ("Stats", ("node", "task_short", "execution_mode", "agent", "model", "effort", "wall_time_s", "usage", "outcome", "telemetry_status", "provider_session_id", "thread_id")),
)


def artifact_kind(path: Path) -> tuple[str, tuple[str, ...]]:
    name = path.name.lower()
    if name.endswith("__zdefiniowanie_problemu.json"):
        return KNOWN_FIELDS[0]
    if name.endswith("__capability_binding.json"):
        return KNOWN_FIELDS[1]
    if name.endswith("__model_binding.json"):
        return KNOWN_FIELDS[2]
    if name.endswith("__orca_launch_receipt.json") or name.endswith("__execution_receipt.json"):
        return KNOWN_FIELDS[3]
    return KNOWN_FIELDS[4]


def artifact_header(artifact: Artifact) -> str:
    lines = [f"Path: {artifact.path}", f"Source: {artifact.source}"]
    if artifact.run_id:
        lines.append(f"Run ID: {artifact.run_id}")
    if artifact.error:
        lines.extend(("", f"READ ERROR: {artifact.error}"))
        return "\n".join(lines)
    if isinstance(artifact.data, Mapping):
        kind, fields = artifact_kind(artifact.path)
        values = [(field, nested_find(artifact.data, (field,))) for field in fields]
        values = [(field, value) for field, value in values if value not in (None, "")]
        if values:
            lines.extend(("", kind + ":"))
            lines.extend(f"  {field}: {value}" for field, value in values)
        meaning = deterministic_interpretation(artifact.data)
        if meaning:
            lines.extend(("", "Interpretation: " + meaning))
    return "\n".join(lines)


def read_for_viewer(artifact: Artifact) -> str:
    header = artifact_header(artifact)
    if artifact.error:
        return header
    try:
        if artifact.path.suffix.lower() == ".json":
            body = json.dumps(artifact.data, indent=2, ensure_ascii=False)
        else:
            body = artifact.path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        body = f"READ ERROR: {type(exc).__name__}: {exc}"
    return header + "\n\n" + ("=" * 72) + "\n\n" + body


def artifacts_merged(artifacts: Sequence[Artifact]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for artifact in artifacts:
        if isinstance(artifact.data, Mapping):
            for key, value in artifact.data.items():
                merged.setdefault(key, value)
    return merged


def classifier_candidate(artifacts: Sequence[Artifact]) -> Artifact | None:
    return next(
        (
            artifact for artifact in artifacts
            if "classifier_candidate_human_required" in artifact.path.name.lower()
            and isinstance(artifact.data, Mapping)
        ),
        None,
    )


def build_timeline(run_id: str, artifacts: Sequence[Artifact], status: str = "") -> str:
    """Build an authority-preserving timeline from recorded artifacts only."""
    by_name = {artifact.path.name.lower(): artifact for artifact in artifacts}

    def data_with(fragment: str) -> Mapping[str, Any]:
        artifact = next((item for name, item in by_name.items() if fragment in name), None)
        return artifact.data if artifact and isinstance(artifact.data, Mapping) else {}

    problem = data_with("zdefiniowanie_problemu") or data_with("classifier_candidate_human_required")
    capability = data_with("capability_binding")
    binding = data_with("model_binding")
    receipt = data_with("execution_receipt") or data_with("orca_launch_receipt")
    classifier_stats = next(
        (
            artifact.data for artifact in artifacts
            if artifact.source == "Stats" and "classifier" in artifact.path.name.lower()
            and isinstance(artifact.data, Mapping)
        ),
        {},
    )

    pipeline = nested_find(problem, ("pipeline",)) or "UNKNOWN"
    pipeline_label = PIPELINE_LABELS.get(str(pipeline), "UNKNOWN")
    capability_id = nested_find(capability, ("coordinator_class",)) or "UNKNOWN"
    role = nested_find(capability, ("role",)) or "UNKNOWN"
    harness = nested_find(binding, ("harness",)) or "UNKNOWN"
    model = nested_find(binding, ("runtime_model_id", "model_family")) or "UNKNOWN"
    effort = nested_find(binding, ("effort",)) or nested_find(capability, ("abstract_effort",)) or "UNKNOWN"
    routing_source = nested_find(capability, ("routing_source",)) or "NOT RECORDED"

    lines = [run_id or "UNKNOWN", "", "TASK"]
    if classifier_stats:
        classifier_model = nested_find(classifier_stats, ("model",)) or "UNKNOWN"
        classifier_effort = nested_find(classifier_stats, ("effort",)) or "UNKNOWN"
        confidence = nested_find(problem, ("confidence",))
        lines.extend(("  ↓", "deterministic routing ambiguous", "  ↓", f"{classifier_model} / {classifier_effort}", "  ↓", f"{pipeline} / confidence {confidence if confidence is not None else 'UNKNOWN'}"))
    else:
        lines.extend(("  ↓", f"{pipeline} — {pipeline_label}", "  ↓", str(routing_source)))
    execution_mode = nested_find(receipt, ("execution_mode",)) or "NOT LAUNCHED"
    lines.extend(("  ↓", str(capability_id), "  ↓", str(role), "  ↓", f"{harness} / {model} / {effort}", "  ↓", str(execution_mode)))

    if receipt:
        if execution_mode == "DIRECT_CLI_CONTROL":
            session = nested_find(receipt, ("provider_session_id", "thread_id", "session_id")) or "NOT RECORDED"
            lines.extend((f"  ├─ session: {session}", "  └─ fresh session: yes"))
        else:
            orca_task = nested_find(receipt, ("orca_task_id", "taskId", "task_id")) or "NOT RECORDED"
            dispatch = nested_find(receipt, ("dispatchId", "dispatch_id")) or "NOT RECORDED"
            terminal = nested_find(receipt, ("terminal_handle", "agent_terminal_handle", "handle")) or "NOT RECORDED"
            failure = nested_find(receipt, ("last_failure", "error", "failedStage"))
            lines.extend((f"  ├─ task: {orca_task}", f"  ├─ dispatch: {dispatch}", f"  ├─ terminal: {terminal}"))
            if failure:
                lines.append(f"  └─ {failure}")
    else:
        lines.append("  └─ NOT LAUNCHED")
    lines.extend(("  ↓", "OUTCOME", status or "NOT RECORDED"))
    return "\n".join(str(value) for value in lines)


class ControlCenter(ttk.Frame):
    def __init__(self, master: tk.Tk) -> None:
        super().__init__(master)
        self.master = master
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.artifacts: list[Artifact] = []
        self.groups: dict[str, list[Artifact]] = {}
        self.visible_runs: list[str] = []
        self.visible_files: list[Artifact] = []
        self.current_result: dict[str, Any] = {}
        self.current_run_id = ""
        self.current_task = ""
        self.human_required_run_id = ""
        self.last_launch = False
        self.last_execution_mode = "direct"
        self.ui_state = load_ui_state(GUI_STATE)
        self.mode_var = tk.StringVar(value="SINGLE TASK")
        workflow_files = sorted(WORKFLOWS.glob("*.json")) if WORKFLOWS.is_dir() else []
        self.workflow_var = tk.StringVar(value=str(workflow_files[0]) if workflow_files else "")
        ensure_codex_cli_on_path()
        self.profile_catalog = load_implementer_profiles_for_ui()
        self.model_runtime = load_model_runtime_snapshot(MODEL_RUNTIME_STATE, self.profile_catalog)
        self.profile_comboboxes: list[tuple[ttk.Combobox, tk.StringVar]] = []
        self.profile_status_var = tk.StringVar()
        self.queue_store = QueueStore()
        self.queue_docs = {item["queue_id"]: item for item in self.queue_store.load_all(recover=True) if item.get("queue_id")}
        self.queue_run_owners: dict[str, tuple[str, str]] = {}
        self.selected_queue_id = ""
        self.selected_queue_task_id = ""
        self.node_binding_vars: dict[str, tk.StringVar] = {}
        self.binding_default_profiles: dict[str, str] = {}
        self.binding_display_to_id: dict[str, str] = {}
        self.repo_var = tk.StringVar()
        self.worktree_var = tk.StringVar()
        self.repo_var.set(str(self.ui_state.get("recent_repo") or ""))
        self.worktree_var.set(str(self.ui_state.get("recent_worktree") or ""))
        self.recent_workspaces = load_recent_workspaces()
        self.repo_recent_var = tk.StringVar()
        self.worktree_recent_var = tk.StringVar()
        self.recent_repo_combos: list[ttk.Combobox] = []
        self.recent_worktree_combos: list[ttk.Combobox] = []
        self.custom_job_type_var = tk.StringVar(value="MULTI_SUBTASK")
        self.custom_plan_var = tk.BooleanVar(value=False)
        self.custom_subtasks: list[dict[str, Any]] = []
        self.custom_job_last_spec = ""
        self.workflow_human_run_id = ""
        self.preflight_result: dict[str, Any] = {}
        self.dry_var = tk.BooleanVar(value=True)
        self.launch_var = tk.BooleanVar(value=False)
        self.execution_mode_var = tk.StringVar(value="direct")
        self.preprocess_policy_var = tk.StringVar(value="AUTO_SAFE")
        self.custom_preprocess_vars = {key: tk.StringVar(value="Auto") for key in ("PLAN", "IMPLEMENT", "SUBTASK", "REVIEW", "REPAIR", "DELTA_REVIEW")}
        self.auto_var = tk.BooleanVar(value=False)
        self.filter_var = tk.StringVar()
        self.status_var = tk.StringVar(value="READY")
        self.exit_var = tk.StringVar(value="Exit code: —")
        self.warning_var = tk.StringVar()
        self.pipeline_var = tk.StringVar()
        self.resolver_status_var = tk.StringVar(value="No HUMAN_REQUIRED decision pending.")
        self.version_warning_var = tk.StringVar()
        self.system_status_var = tk.StringVar(value="Checking system…")
        self.current_run_header_var = tk.StringVar(value="No active run")
        self.workflow_display_var = tk.StringVar()
        self.workflow_details_var = tk.StringVar()
        self.advanced_var = tk.BooleanVar(value=False)
        self.app_settings_store = AppSettings()
        self.app_settings = self.app_settings_store.load()
        self.recipe_var = tk.StringVar(value=self.app_settings["default_recipe"])
        self.preset_var = tk.StringVar(value=self.app_settings["default_execution_preset"])
        self.preprocess_policy_var.set(self.app_settings["default_preprocess_policy"])
        self.customize_var = tk.BooleanVar(value=False)
        self.analytics = aaw_analytics.Analytics(ANALYTICS_DB) if aaw_analytics is not None else None
        self.insight_filter_vars: dict[str, tk.StringVar] = {}
        self.setting_default_recipe_var = tk.StringVar(value=RECIPES[self.app_settings["default_recipe"]]["label"])
        self.setting_default_preset_var = tk.StringVar(value=EXECUTION_PRESETS[self.app_settings["default_execution_preset"]]["label"])
        self.setting_preprocess_var = tk.StringVar(value=self.app_settings["default_preprocess_policy"])
        self.setting_remember_ws_var = tk.BooleanVar(value=bool(self.app_settings["remember_last_workspace"]))
        self.setting_analytics_auto_var = tk.BooleanVar(value=bool(self.app_settings["analytics_auto_refresh_on_open"]))
        queue_settings = self.queue_store.settings()
        if queue_settings["default_workflow"] and Path(queue_settings["default_workflow"]).is_file():
            self.workflow_var.set(queue_settings["default_workflow"])
        self.max_active_queues_var = tk.IntVar(value=queue_settings["max_simultaneous_active_queues"])
        self.default_queue_auto_var = tk.BooleanVar(value=queue_settings["default_auto_continue"])
        self.default_queue_workflow_var = tk.StringVar(value=queue_settings["default_workflow"] or self.workflow_var.get())
        self.default_implement_profile_var = tk.StringVar(value=self._profile_display(self.profile_catalog.get(queue_settings["default_implement_profile"], {})))
        self.default_review_profile_var = tk.StringVar(value=self._profile_display(self.profile_catalog.get(queue_settings["default_review_profile"], {})))
        self.default_repair_profile_var = tk.StringVar(value=self._profile_display(self.profile_catalog.get(queue_settings["default_repair_profile"], {})))
        self.profile_status_var.set("\n".join(self._profile_settings_lines()))
        self.frozen_bindings: dict[str, str] = {}
        self.pages: dict[str, ttk.Frame] = {}
        self.nav_buttons: dict[str, tk.Button] = {}
        self.active_page = ""
        self._build()
        self.workflow_var.trace_add("write", lambda *_: self._refresh_binding_rows())
        self.execution_mode_var.trace_add("write", lambda *_: self._refresh_system_status())
        self._refresh_binding_rows()
        self._refresh_runtime_dependent_views()
        self.refresh_artifacts()
        self.after(100, self._drain_events)
        self.after(5000, self._poll)
        self.master.protocol("WM_DELETE_WINDOW", self._close)

    def _build(self) -> None:
        self.master.title("AAW Control Center")
        self.master.geometry(str(self.ui_state.get("geometry") or "1180x760"))
        self.master.minsize(980, 620)
        self._configure_styles()
        self.grid(sticky="nsew")
        self.master.rowconfigure(0, weight=1)
        self.master.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)
        self._build_sidebar()
        shell = ttk.Frame(self, style="Page.TFrame")
        shell.grid(row=0, column=1, sticky="nsew")
        shell.rowconfigure(1, weight=1)
        shell.columnconfigure(0, weight=1)
        self._build_header(shell)
        self.page_host = ttk.Frame(shell, style="Page.TFrame")
        self.page_host.grid(row=1, column=0, sticky="nsew")
        self.page_host.rowconfigure(0, weight=1)
        self.page_host.columnconfigure(0, weight=1)
        self._build_home_page()
        self._build_new_task_page()
        self._build_workflow_page()
        self._build_custom_job_page()
        self._build_queues_page()
        self._build_runs_page()
        self._build_insights_page()
        self._build_settings_page()
        self.show_page(str(self.ui_state.get("last_page") or "home"))

    def _configure_styles(self) -> None:
        style = ttk.Style(self.master)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        self.master.option_add("*Font", "{Segoe UI} 10")
        style.configure("Page.TFrame", background="#f5f6f8")
        style.configure("Sidebar.TFrame", background="#202733")
        style.configure("Header.TFrame", background="#ffffff")
        style.configure("Card.TFrame", background="#ffffff", relief="flat")
        style.configure("Title.TLabel", background="#f5f6f8", foreground="#17202b", font=("Segoe UI Semibold", 18))
        style.configure("Section.TLabel", background="#f5f6f8", foreground="#17202b", font=("Segoe UI Semibold", 11))
        style.configure("CardTitle.TLabel", background="#ffffff", foreground="#17202b", font=("Segoe UI Semibold", 11))
        style.configure("Card.TLabel", background="#ffffff", foreground="#27313d")
        style.configure("Muted.TLabel", background="#f5f6f8", foreground="#657080")
        style.configure("CardMuted.TLabel", background="#ffffff", foreground="#657080")
        style.configure("Header.TLabel", background="#ffffff", foreground="#17202b")
        style.configure("HeaderMuted.TLabel", background="#ffffff", foreground="#657080")
        style.configure("SidebarTitle.TLabel", background="#202733", foreground="#ffffff", font=("Segoe UI Semibold", 15))
        style.configure("SidebarMuted.TLabel", background="#202733", foreground="#9ca7b5")
        style.configure("Nav.TButton", anchor="w", padding=(18, 11), background="#202733", foreground="#e8edf2", borderwidth=0)
        style.map("Nav.TButton", background=[("active", "#2b3543")])
        style.configure("Primary.TButton", padding=(16, 8), font=("Segoe UI Semibold", 10))
        style.configure("Danger.TButton", foreground="#9b1c1c")
        style.configure("Status.TLabel", background="#eef7f1", foreground="#17643a", padding=(9, 4))
        style.configure("Disclosure.Toolbutton", anchor="w")

    def _build_sidebar(self) -> None:
        sidebar = ttk.Frame(self, style="Sidebar.TFrame", width=190, padding=(16, 20))
        sidebar.grid(row=0, column=0, sticky="ns")
        sidebar.grid_propagate(False)
        sidebar.columnconfigure(0, weight=1)
        ttk.Label(sidebar, text="AAW", style="SidebarTitle.TLabel").grid(row=0, column=0, sticky="w", padx=8)
        ttk.Label(sidebar, text="CONTROL CENTER", style="SidebarMuted.TLabel").grid(row=1, column=0, sticky="w", padx=8, pady=(0, 24))
        for row, (page, label) in enumerate((("home", "Dzisiaj"), ("new", "Nowe zadanie"), ("queues", "Kolejki"), ("runs", "Przebiegi"), ("insights", "Wgląd")), start=2):
            button = tk.Button(sidebar, text=label, command=lambda key=page: self.show_page(key), anchor="w", padx=18, pady=10, relief="flat", borderwidth=0, background="#202733", foreground="#e8edf2", activebackground="#2b3543", activeforeground="#ffffff", font=("Segoe UI", 10))
            button.grid(row=row, column=0, sticky="ew", pady=2)
            self.nav_buttons[page] = button
        ttk.Separator(sidebar).grid(row=7, column=0, sticky="ew", padx=8, pady=16)
        button = tk.Button(sidebar, text="Ustawienia", command=lambda: self.show_page("settings"), anchor="w", padx=18, pady=10, relief="flat", borderwidth=0, background="#202733", foreground="#e8edf2", activebackground="#2b3543", activeforeground="#ffffff", font=("Segoe UI", 10))
        button.grid(row=8, column=0, sticky="ew")
        self.nav_buttons["settings"] = button
        sidebar.rowconfigure(9, weight=1)
        ttk.Label(sidebar, text="Local engineering tool\nNo auto-merge", style="SidebarMuted.TLabel", justify="left").grid(row=10, column=0, sticky="sw", padx=8)

    def _build_header(self, master: ttk.Frame) -> None:
        header = ttk.Frame(master, style="Header.TFrame", padding=(24, 12))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)
        ttk.Label(header, text="AAW Control Center", style="Header.TLabel", font=("Segoe UI Semibold", 11)).grid(row=0, column=0, sticky="w")
        ttk.Label(header, text="DIRECT CLI", style="HeaderMuted.TLabel").grid(row=0, column=1, sticky="e", padx=(12, 10))
        ttk.Button(header, textvariable=self.system_status_var, command=lambda: self.show_page("settings")).grid(row=0, column=2, sticky="e")
        ttk.Label(header, textvariable=self.current_run_header_var, style="HeaderMuted.TLabel").grid(row=0, column=3, sticky="e", padx=(14, 0))

    def _page(self, key: str) -> ScrollablePage:
        page = ScrollablePage(self.page_host)
        page.grid(row=0, column=0, sticky="nsew")
        self.pages[key] = page
        return page

    def _title(self, parent: ttk.Frame, title: str, subtitle: str) -> None:
        ttk.Label(parent, text=title, style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(parent, text=subtitle, style="Muted.TLabel", wraplength=800).grid(row=1, column=0, sticky="w", pady=(5, 20))

    def _workspace_row(self, parent: ttk.Frame, row: int, label: str, variable: tk.StringVar, *, worktree: bool = False) -> None:
        parent.columnconfigure(2, weight=1)
        ttk.Label(parent, text=label, style="Card.TLabel").grid(row=row, column=0, sticky="w", pady=4)
        recent_var = self.worktree_recent_var if worktree else self.repo_recent_var
        values = tuple(self.recent_workspaces["worktrees" if worktree else "repositories"])
        combo = ttk.Combobox(parent, textvariable=recent_var, values=values, state="readonly", width=23)
        combo.grid(row=row, column=1, sticky="ew", padx=(8, 4), pady=4)
        combo.bind("<<ComboboxSelected>>", lambda _e, v=variable, r=recent_var: v.set(r.get()))
        (self.recent_worktree_combos if worktree else self.recent_repo_combos).append(combo)
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=2, sticky="ew", padx=4, pady=4)
        ttk.Button(parent, text="Wybierz…", command=self.browse_worktree if worktree else self.browse_repository).grid(row=row, column=3, padx=4, pady=4)
        ttk.Button(parent, text="Otwórz folder", command=lambda v=variable: self.open_folder(v.get())).grid(row=row, column=4, padx=(4, 0), pady=4)

    # ------------------------------------------------------------------
    # Home / Today
    # ------------------------------------------------------------------
    def _build_home_page(self) -> None:
        page = self._page("home")
        body = page.content
        self._title(body, "Dzisiaj", "Co wymaga decyzji, co się dzieje, co ostatnio się zakończyło.")
        top = ttk.Frame(body, style="Page.TFrame")
        top.grid(row=2, column=0, sticky="ew"); top.columnconfigure(1, weight=1)
        ttk.Button(top, text="+  Nowe zadanie", style="Primary.TButton",
                   command=lambda: self.show_page("new")).grid(row=0, column=0, sticky="w")
        ttk.Button(top, text="Odśwież", command=self._refresh_home).grid(row=0, column=2, sticky="e")
        self.home_attention = ttk.Frame(body, style="Card.TFrame", padding=16)
        self.home_attention.grid(row=3, column=0, sticky="ew", pady=(14, 10)); self.home_attention.columnconfigure(0, weight=1)
        self.home_running = ttk.Frame(body, style="Card.TFrame", padding=16)
        self.home_running.grid(row=4, column=0, sticky="ew", pady=(0, 10)); self.home_running.columnconfigure(0, weight=1)
        self.home_recent = ttk.Frame(body, style="Card.TFrame", padding=16)
        self.home_recent.grid(row=5, column=0, sticky="ew", pady=(0, 10)); self.home_recent.columnconfigure(0, weight=1)
        self.home_system = ttk.Frame(body, style="Card.TFrame", padding=16)
        self.home_system.grid(row=6, column=0, sticky="ew", pady=(0, 24)); self.home_system.columnconfigure(0, weight=1)
        self._refresh_home()

    def _refresh_home(self) -> None:
        for frame in (self.home_attention, self.home_running, self.home_recent, self.home_system):
            for child in frame.winfo_children():
                child.destroy()
        attention, running = self._home_queue_signals()
        recent = self._home_recent_runs()

        ttk.Label(self.home_attention, text="Wymaga uwagi", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        if not attention:
            ttk.Label(self.home_attention, text="Nic nie czeka na Twoją decyzję.", style="CardMuted.TLabel").grid(row=1, column=0, sticky="w", pady=(6, 0))
        for i, (text, action) in enumerate(attention, start=1):
            row = ttk.Frame(self.home_attention, style="Card.TFrame"); row.grid(row=i, column=0, sticky="ew", pady=3)
            row.columnconfigure(0, weight=1)
            ttk.Label(row, text=text, style="Card.TLabel", wraplength=680).grid(row=0, column=0, sticky="w")
            if action:
                ttk.Button(row, text=action[0], command=action[1]).grid(row=0, column=1, sticky="e", padx=(8, 0))

        ttk.Label(self.home_running, text="W toku", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        if not running:
            ttk.Label(self.home_running, text="Brak aktywnych przebiegów ani kolejek.", style="CardMuted.TLabel").grid(row=1, column=0, sticky="w", pady=(6, 0))
        for i, text in enumerate(running, start=1):
            ttk.Label(self.home_running, text=text, style="Card.TLabel", wraplength=680).grid(row=i, column=0, sticky="w", pady=2)

        ttk.Label(self.home_recent, text="Ostatnie", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        if not recent:
            ttk.Label(self.home_recent, text="Brak zapisanych przebiegów.", style="CardMuted.TLabel").grid(row=1, column=0, sticky="w", pady=(6, 0))
        for i, (label, run_id) in enumerate(recent, start=1):
            row = ttk.Frame(self.home_recent, style="Card.TFrame"); row.grid(row=i, column=0, sticky="ew", pady=2)
            row.columnconfigure(0, weight=1)
            ttk.Label(row, text=label, style="Card.TLabel").grid(row=0, column=0, sticky="w")
            ttk.Button(row, text="Szczegóły", command=lambda rid=run_id: self._open_run(rid)).grid(row=0, column=1, sticky="e")

        ttk.Label(self.home_system, text="System", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(self.home_system, textvariable=self.system_status_var, style="CardMuted.TLabel").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Button(self.home_system, text="Szczegóły / Preflight", command=lambda: self.show_page("settings")).grid(row=1, column=1, sticky="e")

    def _open_run(self, run_id: str) -> None:
        self.show_page("runs")
        self.refresh_artifacts(select_run=run_id)

    def _home_queue_signals(self) -> tuple[list[tuple[str, tuple[str, Any] | None]], list[str]]:
        attention: list[tuple[str, tuple[str, Any] | None]] = []
        running: list[str] = []
        self.queue_docs = {item["queue_id"]: item for item in self.queue_store.load_all(recover=True) if item.get("queue_id")}
        for doc in self.queue_docs.values():
            name = doc.get("display_name", "kolejka")
            status = str(doc.get("status", "IDLE"))
            if status == "WAITING_FOR_HUMAN":
                attention.append((f"Kolejka „{name}” czeka na decyzję (Human Gate).",
                                  ("Otwórz kolejki", lambda: self.show_page("queues"))))
            elif status == "BLOCKED":
                attention.append((f"Kolejka „{name}” zablokowana: {doc.get('recovery_reason') or 'BLOCKED'}.",
                                  ("Otwórz kolejki", lambda: self.show_page("queues"))))
            elif status == "PAUSED" and doc.get("recovery_reason"):
                attention.append((f"Kolejka „{name}” wstrzymana po restarcie — wymagana decyzja.",
                                  ("Otwórz kolejki", lambda: self.show_page("queues"))))
            elif status == "RUNNING":
                running.append(f"Kolejka „{name}” w toku.")
            for task in doc.get("tasks", []):
                if isinstance(task, Mapping) and task.get("status") == "RUNNING":
                    running.append(f"  · {task.get('title', 'zadanie')} ({task.get('mode', '')})")
                if isinstance(task, Mapping) and task.get("status") == "INTERRUPTED":
                    attention.append((f"Zadanie „{task.get('title', 'zadanie')}” przerwane restartem.",
                                      ("Otwórz kolejki", lambda: self.show_page("queues"))))
        if self.status_var.get() == "RUNNING" and self.current_task:
            running.append(f"Bieżący przebieg: {self.current_task.splitlines()[0][:70]}")
        if self.status_var.get() == "WAITING_FOR_HUMAN" and self.current_run_id:
            attention.append((f"Przebieg {self.current_run_id} czeka na akceptację.",
                              ("Otwórz przebiegi", lambda: self._open_run(self.current_run_id))))
        for state in self._scan_run_states():
            if state["status"] in ("WAITING_FOR_HUMAN", "WAITING_FOR_PLAN_APPROVAL", "WAITING_FOR_REPAIR_SELECTION"):
                rid = state["run_id"]
                if any(rid in text for text, _ in attention):
                    continue
                attention.append((f"{rid}: {state['goal'][:64]} — czeka na decyzję.",
                                  ("Szczegóły", lambda r=rid: self._open_run(r))))
        return attention[:8], running[:8]

    def _scan_run_states(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if not STATS.is_dir():
            return out
        try:
            run_dirs = sorted((p for p in STATS.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return out
        for run_dir in run_dirs[:40]:
            for rel in ("WORKFLOW/workflow_state.json", "CUSTOM_JOB/job_state.json"):
                path = run_dir / rel
                if not path.is_file():
                    continue
                data = read_json_object(path)
                if not data:
                    continue
                out.append({
                    "run_id": str(data.get("AAW_RUN_ID") or run_dir.name),
                    "status": str(data.get("status") or data.get("final_acceptance") or "UNKNOWN"),
                    "goal": str(data.get("goal") or ""),
                    "mtime": path.stat().st_mtime,
                })
        return out

    def _home_recent_runs(self) -> list[tuple[str, str]]:
        rows: list[tuple[float, str, str]] = []
        for state in self._scan_run_states():
            label = f"{state['run_id']}  ·  {state['status']}  ·  {state['goal'][:56]}"
            rows.append((state["mtime"], label, state["run_id"]))
        if STATS.is_dir():
            try:
                for path in sorted(STATS.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:20]:
                    data = read_json_object(path)
                    rid = str(data.get("run_id") or data.get("aaw_run_id") or "")
                    if rid and not any(rid == r[2] for r in rows):
                        rows.append((path.stat().st_mtime, f"{rid}  ·  {data.get('outcome') or data.get('stage') or 'run'}", rid))
            except OSError:
                pass
        rows.sort(key=lambda r: r[0], reverse=True)
        return [(label, rid) for _m, label, rid in rows[:8]]

    # ------------------------------------------------------------------
    # New Work — human-facing front door
    # ------------------------------------------------------------------
    def _build_new_task_page(self) -> None:
        page = self._page("new")
        body = page.content
        self._title(body, "Nowe zadanie", "Opisz rezultat. Wybierz przepis. AAW przełoży to na istniejące mechanizmy.")

        goal_card = ttk.Frame(body, style="Card.TFrame", padding=18)
        goal_card.grid(row=2, column=0, sticky="ew"); goal_card.columnconfigure(0, weight=1)
        ttk.Label(goal_card, text="1  Co chcesz zrobić?", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(goal_card, text="Np. Napraw regresję importu CSV — po ostatniej zmianie część rekordów jest pomijana.",
                  style="CardMuted.TLabel").grid(row=1, column=0, sticky="w", pady=(3, 8))
        self.task_text = tk.Text(goal_card, height=6, wrap="word", undo=True, font=("Segoe UI", 11), relief="solid", borderwidth=1)
        self.task_text.grid(row=2, column=0, sticky="ew")

        recipe_card = ttk.Frame(body, style="Card.TFrame", padding=18)
        recipe_card.grid(row=3, column=0, sticky="ew", pady=(12, 0)); recipe_card.columnconfigure(0, weight=1)
        ttk.Label(recipe_card, text="2  Przepis", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        self.recipe_detail_var = tk.StringVar()
        for i, (key, spec) in enumerate(RECIPES.items(), start=1):
            ttk.Radiobutton(recipe_card, text=spec["label"], value=key, variable=self.recipe_var,
                            command=self._recipe_changed).grid(row=i, column=0, sticky="w", pady=(6, 0))
        ttk.Label(recipe_card, textvariable=self.recipe_detail_var, style="CardMuted.TLabel",
                  wraplength=760, justify="left").grid(row=len(RECIPES) + 1, column=0, sticky="w", pady=(8, 0))

        ws_card = ttk.LabelFrame(body, text="3–4  Repository / Worktree", padding=12)
        ws_card.grid(row=4, column=0, sticky="ew", pady=(12, 0)); ws_card.columnconfigure(2, weight=1)
        self._workspace_row(ws_card, 0, "Repository", self.repo_var)
        self._workspace_row(ws_card, 1, "Worktree", self.worktree_var, worktree=True)
        ttk.Button(ws_card, text="Utwórz izolowany worktree", command=self.create_worktree).grid(row=2, column=2, sticky="w", pady=(6, 0))

        self.preset_card = ttk.Frame(body, style="Card.TFrame", padding=18)
        self.preset_card.grid(row=5, column=0, sticky="ew", pady=(12, 0)); self.preset_card.columnconfigure(1, weight=1)
        ttk.Label(self.preset_card, text="5  Preset wykonania", style="CardTitle.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")
        self.preset_display_to_key = {spec["label"]: key for key, spec in EXECUTION_PRESETS.items()}
        self.preset_display_var = tk.StringVar(value=EXECUTION_PRESETS[self.preset_var.get()]["label"])
        combo = ttk.Combobox(self.preset_card, textvariable=self.preset_display_var,
                             values=tuple(self.preset_display_to_key), state="readonly", width=28)
        combo.grid(row=1, column=0, sticky="w", pady=(8, 0))
        combo.bind("<<ComboboxSelected>>", lambda _e: self._preset_changed())
        ttk.Checkbutton(self.preset_card, text="Dostosuj (per-node model / effort / preprocessing)",
                        variable=self.customize_var, command=self._toggle_customize).grid(row=1, column=1, sticky="w", padx=(12, 0), pady=(8, 0))
        self.preset_detail_var = tk.StringVar()
        ttk.Label(self.preset_card, textvariable=self.preset_detail_var, style="CardMuted.TLabel",
                  wraplength=760, justify="left").grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))
        self.customize_hint = ttk.Label(self.preset_card,
                                        text="Dostosowanie otwiera pełną stronę Workflow / Custom Job z wypełnionym celem i presetem.",
                                        style="CardMuted.TLabel", wraplength=760)

        actions = ttk.Frame(body, style="Card.TFrame", padding=18)
        actions.grid(row=6, column=0, sticky="ew", pady=(12, 24)); actions.columnconfigure(0, weight=1)
        ttk.Label(actions, text="Execution: Direct CLI · brak merge / push · brak automatycznej eskalacji modelu",
                  style="CardMuted.TLabel").grid(row=0, column=0, sticky="w")
        button_row = ttk.Frame(actions, style="Card.TFrame")
        button_row.grid(row=1, column=0, sticky="e", pady=(12, 0))
        ttk.Button(button_row, text="DRY RUN", command=lambda: self._start_new_work(False)).grid(row=0, column=0, padx=(0, 8))
        self.run_button = ttk.Button(button_row, text="URUCHOM", style="Primary.TButton",
                                     command=lambda: self._start_new_work(True))
        self.run_button.grid(row=0, column=1)
        self.add_queue_menu = tk.Menubutton(button_row, text="DODAJ DO KOLEJKI ▾", relief="raised", padx=10, pady=5)
        self.add_queue_menu.grid(row=0, column=2, padx=(8, 0))
        self._refresh_add_queue_menu()
        self._recipe_changed()

    def _recipe_changed(self) -> None:
        key = self.recipe_var.get()
        spec = RECIPES.get(key, RECIPES["IMPLEMENT_VERIFY"])
        self.recipe_detail_var.set(spec["blurb"])
        uses_preset = spec["primitive"] in ("WORKFLOW", "CUSTOM_JOB")
        if uses_preset:
            self.preset_card.grid()
        else:
            self.preset_card.grid_remove()
        self._preset_changed()

    def _preset_changed(self) -> None:
        key = self.preset_display_to_key.get(self.preset_display_var.get(), self.preset_var.get())
        self.preset_var.set(key)
        spec = EXECUTION_PRESETS[key]
        binding_text = " · ".join(f"{role}: {pid}" for role, pid in spec["bindings"].items())
        self.preset_detail_var.set(f"{spec['blurb']}\n{binding_text} · preprocessing: {spec['preprocess']}")

    def _toggle_customize(self) -> None:
        if self.customize_var.get():
            self.customize_hint.grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 0))
        else:
            self.customize_hint.grid_remove()

    def _apply_preset_to_workflow(self) -> str:
        """Freeze the current preset onto the workflow binding vars. Returns preprocess policy."""
        spec = EXECUTION_PRESETS[self.preset_var.get()]
        workflow_files = sorted(WORKFLOWS.glob("*.json")) if WORKFLOWS.is_dir() else []
        target = next((str(p) for p in workflow_files if p.stem == DEFAULT_WORKFLOW_ID), str(workflow_files[0]) if workflow_files else "")
        if target:
            self.workflow_var.set(target)
            display = next((label for label, path in getattr(self, "workflow_display_to_path", {}).items() if path == target), "")
            if display:
                self.workflow_display_var.set(display)
        self._refresh_binding_rows()
        for node_id, variable in self.node_binding_vars.items():
            node_type = next((str(n.get("type")) for n in self._workflow_llm_nodes() if str(n.get("id")) == node_id), "")
            profile_id = spec["bindings"].get(node_type)
            if not profile_id:
                continue
            display = next((d for d, pid in self.binding_display_to_id.items() if pid == profile_id), "")
            if display:
                variable.set(display)
        self._refresh_binding_details()
        self.preprocess_policy_var.set(spec["preprocess"])
        return spec["preprocess"]

    def _start_new_work(self, launch: bool) -> None:
        recipe = RECIPES.get(self.recipe_var.get(), RECIPES["IMPLEMENT_VERIFY"])
        primitive = recipe["primitive"]
        goal = self.task_text.get("1.0", "end").strip()
        if not goal:
            messagebox.showwarning("Cel wymagany", "Opisz, co chcesz osiągnąć.", parent=self.master)
            return
        if primitive == "ADVANCED":
            self.workflow_goal_text.delete("1.0", "end"); self.workflow_goal_text.insert("1.0", goal)
            self.custom_goal_text.delete("1.0", "end"); self.custom_goal_text.insert("1.0", goal)
            self._apply_preset_to_workflow()
            self.show_page("workflow")
            return
        if self.customize_var.get() and primitive in ("WORKFLOW", "CUSTOM_JOB"):
            if primitive == "WORKFLOW":
                self.workflow_goal_text.delete("1.0", "end"); self.workflow_goal_text.insert("1.0", goal)
                self._apply_preset_to_workflow()
                self.show_page("workflow")
            else:
                self.custom_goal_text.delete("1.0", "end"); self.custom_goal_text.insert("1.0", goal)
                self.custom_job_type_var.set("MULTI_SUBTASK")
                self.show_page("custom")
            return
        if primitive == "SINGLE_TASK":
            self.task_text.delete("1.0", "end"); self.task_text.insert("1.0", goal)
            self._start_task(launch)
            return
        if primitive == "WORKFLOW":
            self.workflow_goal_text.delete("1.0", "end"); self.workflow_goal_text.insert("1.0", goal)
            self._apply_preset_to_workflow()
            self._start_workflow(not launch)
            return
        if primitive == "CUSTOM_JOB":
            self._start_preset_multi_part(goal, launch)

    def _start_preset_multi_part(self, goal: str, launch: bool) -> None:
        repo, worktree = self.repo_var.get().strip(), self.worktree_var.get().strip()
        if not repo or not worktree:
            messagebox.showwarning("Workspace", "Wybierz Repository i Worktree.", parent=self.master)
            return
        spec = EXECUTION_PRESETS[self.preset_var.get()]
        stamp = dt.datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
        job_id = f"newwork-{stamp}-{uuid.uuid4().hex[:6]}"
        lines = [line.strip("-• ").strip() for line in goal.splitlines() if line.strip()]
        subtask_lines = lines[1:] if len(lines) > 1 else [goal]
        subtasks = [
            {"subtask_id": f"S{i:02d}", "title": text[:64], "instructions": text,
             "profile_id": spec["bindings"].get("IMPLEMENT", "TERRA_HIGH"), "machine_gates": []}
            for i, text in enumerate(subtask_lines, 1)
        ]
        job = {
            "schema_version": "AAW_CUSTOM_JOB_V0.3", "job_id": job_id, "job_type": "MULTI_SUBTASK",
            "goal": goal, "repository": repo, "worktree": worktree,
            "worktree_policy": {"isolated_worktree_required": True, "checkpoint_commits": True,
                                "main_merge_allowed": False, "push_allowed": False},
            "execution_adapter": "DIRECT_CLI_CONTROL", "binding_source": "HUMAN_OVERRIDE",
            "preprocess_policy": spec["preprocess"], "preprocess": {},
            "plan": {"enabled": False},
            "machine_gates": {"final": []},
            "review": {"profile_id": spec["bindings"].get("REVIEW", "SOL_HIGH")},
            "repair": {"profile_id": spec["bindings"].get("REPAIR", "LUNA_HIGH"),
                       "selection_mode": "HUMAN_SELECTED", "max_cycles": 1},
            "delta_review": {"profile_id": spec["bindings"].get("REPAIR", "LUNA_HIGH")},
            "limits": {"max_subtasks": 20, "max_llm_calls": 30, "max_wall_time_minutes": 180},
            "subtasks": subtasks,
        }
        JOBS.mkdir(parents=True, exist_ok=True)
        path = JOBS / f"{job_id}.json"
        write_json_atomic(path, job)
        self.custom_job_last_spec = str(path)
        remember_workspace(repo, worktree); self._refresh_recent_choices()
        argv = custom_job_argv(str(path), not launch)
        self.status_var.set("DRY RUN…" if not launch else "RUNNING…")
        threading.Thread(target=self._run_custom_process, args=(argv,), daemon=True).start()
        self.show_page("runs")

    def _build_workflow_page(self) -> None:
        page = self._page("workflow")
        body = page.content
        self._title(body, "Workflow", "Zdefiniuj pracę, wybierz izolowane środowisko i sprawdź plan wykonania.")
        self.workflow_form = ttk.Frame(body, style="Page.TFrame")
        self.workflow_form.grid(row=2, column=0, sticky="ew")
        self.workflow_form.columnconfigure(0, weight=1)
        goal = ttk.Frame(self.workflow_form, style="Card.TFrame", padding=18)
        goal.grid(row=0, column=0, sticky="ew")
        goal.columnconfigure(0, weight=1)
        ttk.Label(goal, text="1  Cel zadania", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        self.workflow_goal_text = tk.Text(goal, height=6, wrap="word", undo=True, font=("Segoe UI", 10), relief="solid", borderwidth=1)
        self.workflow_goal_text.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        choose = ttk.Frame(self.workflow_form, style="Card.TFrame", padding=18)
        choose.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        choose.columnconfigure(1, weight=1)
        ttk.Label(choose, text="2  Workflow", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w", columnspan=3)
        self.workflow_choices = self._workflow_choices()
        self.workflow_display_to_path = {label: path for label, path in self.workflow_choices}
        if self.workflow_choices:
            current = next((label for label, path in self.workflow_choices if path == self.workflow_var.get()), self.workflow_choices[0][0])
            self.workflow_display_var.set(current)
            self.workflow_var.set(self.workflow_display_to_path[current])
        self.workflow_selector = ttk.Combobox(choose, textvariable=self.workflow_display_var, values=tuple(self.workflow_display_to_path), state="readonly")
        self.workflow_selector.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        self.workflow_selector.bind("<<ComboboxSelected>>", self._workflow_selected)
        ttk.Button(choose, text="Szczegóły", command=self._show_workflow_details).grid(row=1, column=2, padx=(8, 0), pady=(10, 0))
        paths = ttk.Frame(self.workflow_form, style="Card.TFrame", padding=18)
        paths.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        paths.columnconfigure(2, weight=1)
        ttk.Label(paths, text="3–4  Repository / Worktree", style="CardTitle.TLabel").grid(row=0, column=0, columnspan=5, sticky="w")
        self._workspace_row(paths, 1, "Repository", self.repo_var)
        self._workspace_row(paths, 2, "Worktree", self.worktree_var, worktree=True)
        ttk.Button(paths, text="Utwórz izolowany worktree", command=self.create_worktree).grid(row=3, column=2, sticky="w", pady=(10, 0))
        plan = ttk.Frame(self.workflow_form, style="Card.TFrame", padding=18)
        plan.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        plan.columnconfigure(0, weight=1)
        ttk.Label(plan, text="5  Execution Plan", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(plan, text="Wybory wykonawców zostaną zamrożone przy starcie.", style="CardMuted.TLabel").grid(row=1, column=0, sticky="w", pady=(3, 10))
        self.bindings_box = ttk.Frame(plan, style="Card.TFrame")
        self.bindings_box.grid(row=2, column=0, sticky="ew")
        self.bindings_box.columnconfigure(0, weight=1)
        apply_row = ttk.Frame(plan, style="Card.TFrame")
        apply_row.grid(row=3, column=0, sticky="e", pady=(10, 0))
        self.apply_all_profile_var = tk.StringVar()
        self.apply_all_profile_combo = ttk.Combobox(apply_row, textvariable=self.apply_all_profile_var, state="readonly", width=27)
        self.apply_all_profile_combo.grid(row=0, column=0, padx=(0, 8))
        self.profile_comboboxes.append((self.apply_all_profile_combo, self.apply_all_profile_var))
        ttk.Button(apply_row, text="Apply to all eligible", command=self._apply_profile_to_all).grid(row=0, column=1)
        advanced = CollapsibleSection(self.workflow_form, "Zaawansowane")
        advanced.grid(row=4, column=0, sticky="ew", pady=(12, 0))
        self.workflow_advanced = advanced
        ttk.Label(advanced.body, text="Execution adapter", style="Card.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Combobox(advanced.body, textvariable=self.execution_mode_var, values=("direct", "orca"), state="readonly", width=18).grid(row=1, column=0, sticky="w", pady=(4, 10))
        ttk.Label(advanced.body, text="Local preprocessing", style="Card.TLabel").grid(row=2, column=0, sticky="w")
        ttk.Combobox(advanced.body, textvariable=self.preprocess_policy_var, values=("AUTO_SAFE", "OFF", "CUSTOM"), state="readonly", width=18).grid(row=3, column=0, sticky="w", pady=(4, 5))
        ttk.Label(advanced.body, text="Qwen3-VL 4B · Local. Auto uses only deterministic policy thresholds; Custom uses per-node spec fields.", style="CardMuted.TLabel", wraplength=760, justify="left").grid(row=4, column=0, sticky="w", pady=(0, 5))
        ttk.Label(advanced.body, textvariable=self.workflow_details_var, style="CardMuted.TLabel", wraplength=760, justify="left").grid(row=5, column=0, sticky="w")
        start = ttk.Frame(self.workflow_form, style="Page.TFrame")
        start.grid(row=5, column=0, sticky="e", pady=(18, 30))
        ttk.Button(start, text="DRY RUN", command=lambda: self._start_workflow(True)).grid(row=0, column=0, padx=(0, 8))
        self.workflow_button = ttk.Button(start, text="START WORKFLOW", command=lambda: self._start_workflow(False), style="Primary.TButton")
        self.workflow_button.grid(row=0, column=1)
        self.monitor_frame = ttk.Frame(body, style="Card.TFrame", padding=18)
        self.monitor_frame.columnconfigure(0, weight=1)
        ttk.Label(self.monitor_frame, text="Current run", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        self.monitor_title_var = tk.StringVar(value="Workflow")
        ttk.Label(self.monitor_frame, textvariable=self.monitor_title_var, style="CardTitle.TLabel", font=("Segoe UI Semibold", 15)).grid(row=1, column=0, sticky="w", pady=(10, 2))
        ttk.Label(self.monitor_frame, textvariable=self.status_var, style="Status.TLabel").grid(row=2, column=0, sticky="w")
        self.monitor_nodes = ttk.Frame(self.monitor_frame, style="Card.TFrame")
        self.monitor_nodes.grid(row=3, column=0, sticky="ew", pady=(14, 0))
        self.monitor_nodes.columnconfigure(0, weight=1)
        ttk.Button(self.monitor_frame, text="Pokaż szczegóły techniczne", command=lambda: self.show_page("runs")).grid(row=4, column=0, sticky="w", pady=(16, 0))

    def _build_custom_job_page(self) -> None:
        page = self._page("custom"); body = page.content
        self._title(body, "Custom Job", "Statyczny composer: pojedyncza implementacja, sekwencja etapów lub wiele subtasków z checkpoint commits.")
        form = ttk.Frame(body, style="Card.TFrame", padding=18); form.grid(row=2, column=0, sticky="ew"); form.columnconfigure(0, weight=1)
        ttk.Label(form, text="Cel nadrzędny", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        self.custom_goal_text = tk.Text(form, height=5, wrap="word", undo=True); self.custom_goal_text.grid(row=1, column=0, sticky="ew", pady=(6, 10))
        mode = ttk.Frame(form, style="Card.TFrame"); mode.grid(row=2, column=0, sticky="ew")
        ttk.Label(mode, text="Szablon", style="Card.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Combobox(mode, textvariable=self.custom_job_type_var, values=("SINGLE_IMPLEMENTATION", "MULTI_SUBTASK", "MULTI_STAGE"), state="readonly", width=25).grid(row=0, column=1, sticky="w", padx=8)
        ttk.Checkbutton(mode, text="PLAN (default: Human approval)", variable=self.custom_plan_var).grid(row=0, column=2, sticky="w", padx=8)
        ttk.Label(mode, text="Local preprocessing").grid(row=1, column=0, sticky="w", pady=(8, 0))
        custom_policy = ttk.Combobox(mode, textvariable=self.preprocess_policy_var, values=("AUTO_SAFE", "OFF", "CUSTOM"), state="readonly", width=25)
        custom_policy.grid(row=1, column=1, sticky="w", padx=8, pady=(8, 0)); custom_policy.bind("<<ComboboxSelected>>", lambda _event: self._toggle_custom_preprocess())
        ttk.Label(mode, text="Qwen3-VL 4B · Local; Custom reads per-node preprocess from saved JSON.", style="CardMuted.TLabel").grid(row=1, column=2, sticky="w", pady=(8, 0))
        self.custom_preprocess_frame = ttk.Frame(mode, style="Card.TFrame")
        self.custom_preprocess_frame.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        rows = (("PLAN", "Task normalization", ("Auto", "Off", "Qwen Task Normalize", "Qwen Summary")), ("IMPLEMENT", "Implementation handoff", ("Auto", "Off", "Qwen Task Normalize", "Qwen Summary")), ("SUBTASK", "Subtask handoff", ("Auto", "Off", "Qwen Task Normalize", "Qwen Summary")), ("REVIEW", "Review preparation", ("Auto", "Off", "Qwen Summary", "Qwen Diff Triage")), ("REPAIR", "Machine logs / findings", ("Auto", "Off", "Qwen Log Triage", "Qwen Findings Prep")), ("DELTA_REVIEW", "Delta review assist", ("Auto", "Off", "Qwen Delta Assist")))
        for row, (key, label, choices) in enumerate(rows):
            ttk.Label(self.custom_preprocess_frame, text=label).grid(row=row, column=0, sticky="w", pady=2)
            ttk.Combobox(self.custom_preprocess_frame, textvariable=self.custom_preprocess_vars[key], values=choices, state="readonly", width=26).grid(row=row, column=1, sticky="w", padx=8, pady=2)
        self._toggle_custom_preprocess()
        workspace = ttk.LabelFrame(form, text="Repository / Worktree", padding=8); workspace.grid(row=3, column=0, sticky="ew", pady=(12, 0)); workspace.columnconfigure(2, weight=1)
        self._workspace_row(workspace, 0, "Repository", self.repo_var); self._workspace_row(workspace, 1, "Worktree", self.worktree_var, worktree=True)
        ttk.Button(workspace, text="Utwórz izolowany worktree", command=self.create_worktree).grid(row=2, column=2, sticky="w", pady=(6, 0))
        tasks = ttk.LabelFrame(form, text="Subtasks / stage specs", padding=8); tasks.grid(row=4, column=0, sticky="ew", pady=(12, 0)); tasks.columnconfigure(0, weight=1)
        self.custom_subtask_frame = ttk.Frame(tasks); self.custom_subtask_frame.grid(row=0, column=0, sticky="ew"); self.custom_subtask_frame.columnconfigure(1, weight=1)
        ttk.Button(tasks, text="+ Dodaj subtask", command=self._add_custom_subtask).grid(row=1, column=0, sticky="w", pady=(8, 0))
        self._add_custom_subtask(); self._add_custom_subtask(); self._add_custom_subtask()
        bindings = ttk.LabelFrame(form, text="PLAN / final review / repair / delta review", padding=8); bindings.grid(row=5, column=0, sticky="ew", pady=(12, 0)); bindings.columnconfigure(1, weight=1)
        self.custom_plan_profile_var=tk.StringVar(value=self._profile_display(self.profile_catalog.get("ASTRA_XHIGH",{}))); self.custom_review_var=tk.StringVar(value=self._profile_display(self.profile_catalog.get("SOL_HIGH",{}))); self.custom_repair_var=tk.StringVar(value=self._profile_display(self.profile_catalog.get("LUNA_HIGH",{}))); self.custom_delta_var=tk.StringVar(value=self._profile_display(self.profile_catalog.get("SONNET_HIGH",{})))
        for row,(label,var) in enumerate((("PLAN",self.custom_plan_profile_var),("Final Review",self.custom_review_var),("Repair",self.custom_repair_var),("Delta Review",self.custom_delta_var))):
            ttk.Label(bindings,text=label).grid(row=row,column=0,sticky="w",pady=3); combo=ttk.Combobox(bindings,textvariable=var,values=self._profile_choices(),state="readonly"); combo.grid(row=row,column=1,sticky="ew",padx=(8,0),pady=3); self.profile_comboboxes.append((combo,var))
        self.custom_status_var=tk.StringVar(value="Bindings freeze at START. No merge, push or automatic escalation.")
        ttk.Label(form,textvariable=self.custom_status_var,style="CardMuted.TLabel",wraplength=760).grid(row=6,column=0,sticky="w",pady=(10,0))
        actions=ttk.Frame(form,style="Card.TFrame"); actions.grid(row=7,column=0,sticky="e",pady=(16,0))
        ttk.Button(actions,text="ZAPISZ SPEC",command=self._save_custom_job).grid(row=0,column=0,padx=4)
        ttk.Button(actions,text="DRY RUN",command=lambda:self._start_custom_job(True)).grid(row=0,column=1,padx=4)
        ttk.Button(actions,text="START",command=lambda:self._start_custom_job(False),style="Primary.TButton").grid(row=0,column=2,padx=4)
        ttk.Button(actions,text="DODAJ DO KOLEJKI",command=self._add_custom_job_to_queue).grid(row=0,column=3,padx=4)

    def _toggle_custom_preprocess(self) -> None:
        if not hasattr(self, "custom_preprocess_frame"):
            return
        if self.preprocess_policy_var.get() == "CUSTOM":
            self.custom_preprocess_frame.grid()
        else:
            self.custom_preprocess_frame.grid_remove()

    def _custom_preprocess_spec(self) -> dict[str, dict[str, Any]]:
        mapping = {"Qwen Task Normalize": ("LOCAL_QWEN_TASK_NORMALIZE", "LOCAL_QWEN_JSON"), "Qwen Summary": ("LOCAL_QWEN_SUMMARY", "LOCAL_QWEN_SUMMARY"), "Qwen Log Triage": ("LOCAL_QWEN_LOG_TRIAGE", "LOCAL_QWEN_LOG_TRIAGE"), "Qwen Diff Triage": ("LOCAL_QWEN_DIFF_TRIAGE", "LOCAL_QWEN_DIFF_TRIAGE"), "Qwen Findings Prep": ("LOCAL_QWEN_FINDINGS_PREP", "LOCAL_QWEN_FINDINGS_PREP"), "Qwen Delta Assist": ("LOCAL_QWEN_DELTA_ASSIST", "LOCAL_QWEN_DELTA")}
        result: dict[str, dict[str, Any]] = {}
        for node, variable in self.custom_preprocess_vars.items():
            selected = variable.get()
            if selected in mapping:
                kind, profile = mapping[selected]
                result[node] = {"type": kind, "profile": profile, "required": True, "reason": "MANUAL_USER_SELECTION"}
        return result

    def _add_custom_subtask(self) -> None:
        index=len(self.custom_subtasks)+1
        self.custom_subtasks.append({"text":tk.StringVar(value=""),"profile":tk.StringVar(value=self._profile_display(self.profile_catalog.get("TERRA_HIGH",{})))})
        self._render_custom_subtasks()

    def _move_custom_subtask(self,index:int,delta:int) -> None:
        target=index+delta
        if 0<=target<len(self.custom_subtasks): self.custom_subtasks[index],self.custom_subtasks[target]=self.custom_subtasks[target],self.custom_subtasks[index]; self._render_custom_subtasks()

    def _remove_custom_subtask(self,index:int) -> None:
        if len(self.custom_subtasks)>1: self.custom_subtasks.pop(index); self._render_custom_subtasks()

    def _render_custom_subtasks(self) -> None:
        if not hasattr(self,"custom_subtask_frame"): return
        for child in self.custom_subtask_frame.winfo_children(): child.destroy()
        for row,item in enumerate(self.custom_subtasks):
            ttk.Label(self.custom_subtask_frame,text=f"{row+1}.").grid(row=row,column=0,sticky="w",pady=3)
            ttk.Entry(self.custom_subtask_frame,textvariable=item["text"]).grid(row=row,column=1,sticky="ew",padx=4,pady=3)
            ttk.Combobox(self.custom_subtask_frame,textvariable=item["profile"],values=self._profile_choices(),state="readonly",width=25).grid(row=row,column=2,padx=4,pady=3)
            ttk.Button(self.custom_subtask_frame,text="↑",width=3,command=lambda i=row:self._move_custom_subtask(i,-1)).grid(row=row,column=3)
            ttk.Button(self.custom_subtask_frame,text="↓",width=3,command=lambda i=row:self._move_custom_subtask(i,1)).grid(row=row,column=4)
            ttk.Button(self.custom_subtask_frame,text="Usuń",command=lambda i=row:self._remove_custom_subtask(i)).grid(row=row,column=5,padx=(4,0))

    def _custom_profile_id(self, variable: tk.StringVar, role: str = "") -> str:
        profile_id=self.binding_display_to_id.get(variable.get(),"")
        if not profile_id: raise ValueError(f"Invalid profile selection: {variable.get()}")
        profile=self.profile_catalog.get(profile_id,{})
        if profile.get("not_implementer") and role in {"IMPLEMENT","SUBTASK","REPAIR","REVIEW"}: raise ValueError(f"{profile_id} is not eligible for {role}")
        if role and role not in set(profile.get("suitable_for",[])): raise ValueError(f"{profile_id} is not eligible for {role}")
        state=self.get_runtime_profile_state(profile_id)
        if state["state"] not in {"VERIFIED","AVAILABLE"}: raise ValueError(f"{profile_id}: {state['state']} — {state['reason']}")
        return profile_id

    def _save_custom_job(self) -> str:
        goal=self.custom_goal_text.get("1.0","end").strip(); repo=self.repo_var.get().strip(); worktree=self.worktree_var.get().strip(); job_type=self.custom_job_type_var.get()
        if not goal or not repo or not worktree: raise ValueError("Goal, Repository and Worktree are required")
        stamp=dt.datetime.now().astimezone().strftime("%Y%m%d_%H%M%S"); job_id=f"custom-{stamp}-{uuid.uuid4().hex[:6]}"
        rows=[]
        for index,item in enumerate(self.custom_subtasks,1):
            text=item["text"].get().strip()
            if text: rows.append({"subtask_id":f"S{index:02d}","title":text.splitlines()[0][:64],"instructions":text,"profile_id":self._custom_profile_id(item["profile"],"SUBTASK"),"machine_gates":[]})
        base={"schema_version":"AAW_CUSTOM_JOB_V0.3","job_id":job_id,"job_type":job_type,"goal":goal,"repository":repo,"worktree":worktree,"worktree_policy":{"isolated_worktree_required":True,"checkpoint_commits":True,"main_merge_allowed":False,"push_allowed":False},"execution_adapter":"DIRECT_CLI_CONTROL","binding_source":"HUMAN_OVERRIDE","preprocess_policy":self.preprocess_policy_var.get(),"preprocess":self._custom_preprocess_spec() if self.preprocess_policy_var.get()=="CUSTOM" else {},
              "plan":{"enabled":self.custom_plan_var.get(),"profile_id":self._custom_profile_id(self.custom_plan_profile_var,"PLAN"),"approval":"HUMAN_APPROVAL","scope_expansion_allowed":False},"machine_gates":{"final":[]},
              "review":{"profile_id":self._custom_profile_id(self.custom_review_var,"REVIEW")},"repair":{"profile_id":self._custom_profile_id(self.custom_repair_var,"REPAIR"),"selection_mode":"HUMAN_SELECTED","max_cycles":1},"delta_review":{"profile_id":self._custom_profile_id(self.custom_delta_var,"DELTA_REVIEW")},"limits":{"max_subtasks":20,"max_llm_calls":30,"max_wall_time_minutes":180}}
        if job_type=="SINGLE_IMPLEMENTATION": base["implement_profile_id"]=rows[0]["profile_id"] if rows else "TERRA_HIGH"
        elif job_type=="MULTI_SUBTASK":
            if not rows: raise ValueError("At least one subtask is required")
            base["subtasks"]=rows
        else:
            if not rows: raise ValueError("At least one child spec path is required")
            base["stages"]=[{"stage_id":row["subtask_id"],"stage_type":"CUSTOM_JOB","spec_path":row["instructions"]} for row in rows]
        JOBS.mkdir(parents=True,exist_ok=True); path=JOBS/f"{job_id}.json"; write_json_atomic(path,base); self.custom_job_last_spec=str(path); remember_workspace(repo,worktree); self._refresh_recent_choices(); self.custom_status_var.set(f"Saved: {path}"); return str(path)

    def _start_custom_job(self,dry_run:bool) -> None:
        try: spec=self._save_custom_job()
        except ValueError as exc: messagebox.showwarning("Custom Job",str(exc),parent=self.master); return
        argv=custom_job_argv(spec,dry_run); self.custom_status_var.set("DRY RUN…" if dry_run else "RUNNING…"); threading.Thread(target=self._run_custom_process,args=(argv,),daemon=True).start()

    def _run_custom_process(self,argv:list[str]) -> None:
        self.events.put(("custom_complete",run_workflow_process(argv)))

    def _on_custom_complete(self,payload:tuple[int,str,str,list[str]]) -> None:
        rc,stdout,stderr,argv=payload; result=parse_json_flex(stdout); status=str(result.get("status") or ("BLOCKED" if rc else "PASS")); self.custom_status_var.set(status)
        self.current_run_id=str(result.get("AAW_RUN_ID") or ""); self.current_result=result; self._set_text(self.output,f"CUSTOM JOB (shell=False):\n{subprocess.list2cmdline(argv)}\n\nSTDOUT:\n{stdout}\n\nSTDERR:\n{stderr}")
        self.refresh_artifacts(select_run=self.current_run_id)
        if status in {"WAITING_FOR_HUMAN","WAITING_FOR_PLAN_APPROVAL","WAITING_FOR_REPAIR_SELECTION"}: self._show_custom_human_gate(result)

    def _show_custom_human_gate(self, state: Mapping[str, Any]) -> None:
        dialog=tk.Toplevel(self.master); dialog.title("Custom Job — Human Gate"); dialog.transient(self.master); dialog.geometry("760x620"); dialog.columnconfigure(0,weight=1); dialog.rowconfigure(1,weight=1)
        status=str(state.get("status","")); run_id=str(state.get("AAW_RUN_ID",self.current_run_id)); commits=list(state.get("checkpoint_commits",[])); telemetry=list(state.get("telemetry",[]))
        tokens=sum(int(row.get("input_tokens") or 0)+int(row.get("output_tokens") or 0) for row in telemetry if isinstance(row,Mapping)); wall=sum(float(row.get("wall_time_s") or 0) for row in telemetry if isinstance(row,Mapping))
        review=state.get("review") if isinstance(state.get("review"),Mapping) else {}; findings=list(review.get("findings",[])) if isinstance(review,Mapping) else []
        summary=(f"{status}\n\nGoal: {state.get('goal','—')}\nSubtasks: {len(state.get('subtask_results',[]))}\nCommits: {len(commits)}\n"
                 f"Review: {review.get('outcome','—')}\nFindings: {len(findings)}\nRepair commit: {state.get('repair_commit') or '—'}\n"
                 f"Delta review: {(state.get('delta_review') or {}).get('outcome','—') if isinstance(state.get('delta_review'),Mapping) else '—'}\nTokens: {tokens}\nWall time: {wall:.2f}s\n\nAccept candidate never means merge or push.")
        ttk.Label(dialog,text=summary,justify="left",wraplength=710,padding=14).grid(row=0,column=0,sticky="ew")
        body=ttk.Frame(dialog,padding=(14,0,14,8)); body.grid(row=1,column=0,sticky="nsew"); body.columnconfigure(0,weight=1)
        selected: list[tuple[str,tk.BooleanVar]]=[]
        if status=="WAITING_FOR_REPAIR_SELECTION":
            ttk.Label(body,text="Wybierz findings przekazane do REPAIR:").grid(row=0,column=0,sticky="w",pady=(0,6))
            for index,finding in enumerate(findings,1):
                finding_id=str(finding.get("finding_id","")); severity=str(finding.get("severity","")).upper(); variable=tk.BooleanVar(value=severity in {"BLOCKING","HIGH","P0","P1"})
                selected.append((finding_id,variable)); text=f"{finding_id} [{severity}] {finding.get('file') or ''} {finding.get('description') or ''}"
                ttk.Checkbutton(body,text=text,variable=variable).grid(row=index,column=0,sticky="w",pady=2)
        actions=ttk.Frame(dialog,padding=14); actions.grid(row=2,column=0,sticky="ew")
        ttk.Button(actions,text="Open diff",command=lambda:self._show_custom_diff(state)).pack(side="left",padx=3)
        ttk.Button(actions,text="Open worktree",command=lambda:self.open_folder(str(state.get("worktree","")))).pack(side="left",padx=3)
        if status=="WAITING_FOR_REPAIR_SELECTION":
            ttk.Button(actions,text="Repair selected",style="Primary.TButton",command=lambda:self._run_selected_custom_repair(dialog,run_id,[finding_id for finding_id,var in selected if var.get()])).pack(side="right",padx=3)
        elif status=="WAITING_FOR_HUMAN":
            ttk.Button(actions,text="Accept candidate",style="Primary.TButton",command=lambda:self._run_custom_verdict(dialog,run_id,"accept")).pack(side="right",padx=3)
            ttk.Button(actions,text="Reject",command=lambda:self._run_custom_verdict(dialog,run_id,"reject")).pack(side="right",padx=3)
        ttk.Button(actions,text="Leave for later",command=dialog.destroy).pack(side="right",padx=3)

    def _show_custom_diff(self,state: Mapping[str,Any]) -> None:
        worktree=str(state.get("worktree","")).strip(); baseline=str(state.get("baseline_commit","")).strip()
        if not worktree or not baseline: return
        rc,stdout,stderr=run_process(["git","-C",worktree,"diff","--no-ext-diff",f"{baseline}..HEAD"],timeout=30)
        self._set_text(self.output,f"CUSTOM JOB DIFF rc={rc}\n\n{stdout}\n{stderr}"); self.show_page("runs")

    def _run_selected_custom_repair(self,dialog: tk.Toplevel,run_id: str,finding_ids: list[str]) -> None:
        if not finding_ids: messagebox.showwarning("Custom Job","Wybierz co najmniej jedno finding.",parent=dialog); return
        dialog.destroy(); argv=[sys.executable,str(CUSTOM_JOB_RUNNER),"--run-id",run_id]
        for finding_id in finding_ids: argv.extend(["--select-finding",finding_id])
        self.custom_status_var.set("REPAIR…"); threading.Thread(target=self._run_custom_process,args=(argv,),daemon=True).start()

    def _run_custom_verdict(self,dialog: tk.Toplevel,run_id: str,verdict: str) -> None:
        dialog.destroy(); argv=[sys.executable,str(CUSTOM_JOB_RUNNER),"--run-id",run_id,"--human-verdict",verdict]
        threading.Thread(target=self._run_custom_process,args=(argv,),daemon=True).start()

    def _add_custom_job_to_queue(self) -> None:
        if not self.selected_queue_id: messagebox.showwarning("Queue required","Wybierz kolejkę na stronie Kolejki.",parent=self.master); return
        try: spec=self._save_custom_job()
        except ValueError as exc: messagebox.showwarning("Custom Job",str(exc),parent=self.master); return
        item=self.queue_docs[self.selected_queue_id]; goal=self.custom_goal_text.get("1.0","end").strip(); self.queue_store.add_task(item,title=goal.splitlines()[0][:80],goal=goal,mode="CUSTOM_JOB",job_spec=spec,repo=self.repo_var.get(),worktree=self.worktree_var.get()); self.refresh_queues(item["queue_id"]); self.show_page("queues")

    def _build_queues_page(self) -> None:
        page = ttk.Frame(self.page_host, style="Page.TFrame", padding=(28, 22, 28, 24))
        page.grid(row=0, column=0, sticky="nsew")
        page.rowconfigure(3, weight=1); page.columnconfigure(0, weight=1)
        self.pages["queues"] = page
        ttk.Label(page, text="Kolejki", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(page, text="Uporządkowane backlogi operatora uruchamiane przez istniejące runnery.", style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(5, 14))
        toolbar = ttk.Frame(page, style="Page.TFrame")
        toolbar.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        for column, (label, command) in enumerate((("+ Nowa kolejka", self.new_queue_dialog), ("Zmień nazwę", self.rename_queue_dialog), ("+ Dodaj zadanie", self.add_queue_task_dialog), ("START QUEUE", self.start_selected_queue), ("PAUSE AFTER CURRENT TASK", self.pause_selected_queue))):
            ttk.Button(toolbar, text=label, command=command, style="Primary.TButton" if label == "START QUEUE" else "TButton").grid(row=0, column=column, padx=(0, 7))
        split = ttk.Panedwindow(page, orient="horizontal"); split.grid(row=3, column=0, sticky="nsew")
        left = ttk.Frame(split, style="Card.TFrame", padding=12); right = ttk.Frame(split, style="Card.TFrame", padding=12)
        split.add(left, weight=1); split.add(right, weight=3)
        left.rowconfigure(1, weight=1); left.columnconfigure(0, weight=1)
        ttk.Label(left, text="KOLEJKI", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 8))
        self.queue_overview = ttk.Treeview(left, columns=("progress", "status"), show="tree headings", selectmode="browse")
        self.queue_overview.heading("#0", text="Nazwa"); self.queue_overview.heading("progress", text="Postęp"); self.queue_overview.heading("status", text="Status")
        self.queue_overview.column("#0", width=150); self.queue_overview.column("progress", width=70, stretch=False); self.queue_overview.column("status", width=135, stretch=False)
        self.queue_overview.grid(row=1, column=0, sticky="nsew"); self.queue_overview.bind("<<TreeviewSelect>>", self._queue_selected)
        right.rowconfigure(2, weight=1); right.columnconfigure(0, weight=1)
        self.queue_detail_title_var = tk.StringVar(value="Wybierz kolejkę")
        self.queue_action_var = tk.StringVar(value="")
        ttk.Label(right, textvariable=self.queue_detail_title_var, style="CardTitle.TLabel", font=("Segoe UI Semibold", 15)).grid(row=0, column=0, sticky="w")
        ttk.Label(right, textvariable=self.queue_action_var, foreground="#9b1c1c", background="#ffffff").grid(row=1, column=0, sticky="w", pady=(3, 8))
        self.queue_tasks = ttk.Treeview(right, columns=("mode", "profiles", "status", "run"), show="tree headings", selectmode="browse")
        for key, label, width in (("#0", "Zadanie", 220), ("mode", "Tryb", 100), ("profiles", "Execution", 210), ("status", "Status", 145), ("run", "Run ID", 160)):
            self.queue_tasks.heading(key, text=label); self.queue_tasks.column(key, width=width, stretch=key in {"#0", "profiles"})
        self.queue_tasks.grid(row=2, column=0, sticky="nsew"); self.queue_tasks.bind("<<TreeviewSelect>>", self._queue_task_selected)
        actions = ttk.Frame(right, style="Card.TFrame"); actions.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        ttk.Button(actions, text="↑", width=4, command=lambda: self.move_queue_task(-1)).grid(row=0, column=0)
        ttk.Button(actions, text="↓", width=4, command=lambda: self.move_queue_task(1)).grid(row=0, column=1, padx=5)
        ttk.Button(actions, text="Usuń WAITING", command=self.delete_queue_task).grid(row=0, column=2)
        ttk.Button(actions, text="Wznów", command=self.resume_selected_queue).grid(row=0, column=3, padx=(8, 0))
        ttk.Button(actions, text="Szczegóły", command=self.show_queue_task_details).grid(row=0, column=4, padx=(8, 0))
        ttk.Button(actions, text="Otwórz przebieg", command=self.open_selected_queue_run).grid(row=0, column=5, padx=(8, 0))
        self.refresh_queues()

    def _refresh_add_queue_menu(self) -> None:
        if not hasattr(self, "add_queue_menu"):
            return
        menu = tk.Menu(self.add_queue_menu, tearoff=False)
        for item in sorted(self.queue_docs.values(), key=lambda row: str(row.get("created_at", ""))):
            menu.add_command(label=str(item.get("display_name") or item.get("queue_id")), command=lambda queue_id=item["queue_id"]: self.add_current_task_to_queue(queue_id))
        if self.queue_docs:
            menu.add_separator()
        menu.add_command(label="+ Nowa kolejka", command=self.new_queue_dialog)
        self.add_queue_menu.configure(menu=menu)

    def refresh_queues(self, select_queue: str = "") -> None:
        if not hasattr(self, "queue_overview"):
            return
        selected = select_queue or self.selected_queue_id
        for row in self.queue_overview.get_children():
            self.queue_overview.delete(row)
        for item in sorted(self.queue_docs.values(), key=lambda row: str(row.get("created_at", ""))):
            tasks = item.get("tasks", [])
            done = sum(1 for task in tasks if task.get("status") == "PASS")
            queue_id = str(item["queue_id"])
            self.queue_overview.insert("", "end", iid=queue_id, text=str(item.get("display_name") or queue_id), values=(f"{done} / {len(tasks)}", item.get("status", "IDLE")))
        if selected in self.queue_docs and self.queue_overview.exists(selected):
            self.queue_overview.selection_set(selected); self.queue_overview.focus(selected)
        elif self.queue_docs:
            first = next(iter(self.queue_overview.get_children()), "")
            if first:
                self.queue_overview.selection_set(first); self.queue_overview.focus(first); selected = first
        self.selected_queue_id = selected if selected in self.queue_docs else ""
        self._populate_queue_tasks()
        self._refresh_add_queue_menu()

    def _queue_selected(self, _event: Any = None) -> None:
        selected = self.queue_overview.selection()
        self.selected_queue_id = selected[0] if selected else ""
        self.selected_queue_task_id = ""
        self._populate_queue_tasks()

    def _queue_task_selected(self, _event: Any = None) -> None:
        selected = self.queue_tasks.selection()
        self.selected_queue_task_id = selected[0] if selected else ""

    def _task_profiles_label(self, task: Mapping[str, Any]) -> str:
        if task.get("mode") == "CUSTOM_JOB":
            return f"Frozen job: {Path(str(task.get('job_spec') or '')).name or 'UNKNOWN'}"
        if task.get("mode") != "WORKFLOW":
            return "Single Task"
        bindings = task.get("resolved_bindings") or task.get("bindings") or {}
        if not isinstance(bindings, Mapping):
            return "Workflow"
        labels = []
        for node_id, profile_id in bindings.items():
            profile = self.profile_catalog.get(str(profile_id), {})
            labels.append(self._profile_display(profile))
        return " → ".join(labels) or "Workflow defaults"

    def _populate_queue_tasks(self) -> None:
        if not hasattr(self, "queue_tasks"):
            return
        for row in self.queue_tasks.get_children():
            self.queue_tasks.delete(row)
        item = self.queue_docs.get(self.selected_queue_id)
        if not item:
            self.queue_detail_title_var.set("Wybierz kolejkę"); self.queue_action_var.set("")
            return
        self.queue_detail_title_var.set(str(item.get("display_name") or item.get("queue_id")))
        stop = str(item.get("status")) in {"WAITING_FOR_HUMAN", "BLOCKED", "PAUSED"}
        reason = str(item.get("stop_reason") or item.get("recovery_reason") or "")
        self.queue_action_var.set(f"ACTION REQUIRED · {reason}" if stop and reason else ("ACTION REQUIRED" if stop else ""))
        for task in sorted(item.get("tasks", []), key=lambda row: int(row.get("position", 0))):
            task_id = str(task.get("task_id"))
            self.queue_tasks.insert("", "end", iid=task_id, text=f"{task.get('position')}. {task.get('title')}", values=(task.get("mode"), self._task_profiles_label(task), task.get("status"), task.get("run_id") or "—"))
        if self.selected_queue_task_id and self.queue_tasks.exists(self.selected_queue_task_id):
            self.queue_tasks.selection_set(self.selected_queue_task_id)

    def _name_dialog(self, title: str, initial: str, submit: Any) -> None:
        dialog = tk.Toplevel(self.master); dialog.title(title); dialog.transient(self.master); dialog.grab_set(); dialog.geometry(self._center_dialog(430, 180))
        frame = ttk.Frame(dialog, padding=20); frame.grid(sticky="nsew"); frame.columnconfigure(0, weight=1)
        value = tk.StringVar(value=initial)
        ttk.Label(frame, text="Nazwa").grid(row=0, column=0, sticky="w")
        entry = ttk.Entry(frame, textvariable=value); entry.grid(row=1, column=0, sticky="ew", pady=(6, 16)); entry.focus_set()
        ttk.Button(frame, text="Zapisz", command=lambda: (submit(value.get()), dialog.destroy()), style="Primary.TButton").grid(row=2, column=0, sticky="e")

    def new_queue_dialog(self) -> None:
        self._name_dialog("Nowa kolejka", "", self._create_queue)

    def _create_queue(self, name: str) -> None:
        try:
            item = self.queue_store.create(name, self.default_queue_auto_var.get())
        except ValueError as exc:
            messagebox.showwarning("Queue name required", str(exc), parent=self.master); return
        self.queue_docs[item["queue_id"]] = item
        self.refresh_queues(item["queue_id"])

    def rename_queue_dialog(self) -> None:
        item = self.queue_docs.get(self.selected_queue_id)
        if item:
            self._name_dialog("Zmień nazwę kolejki", str(item.get("display_name") or ""), self._rename_selected_queue)

    def _rename_selected_queue(self, name: str) -> None:
        item = self.queue_docs.get(self.selected_queue_id)
        if not item or not name.strip():
            return
        item["display_name"] = name.strip(); self.queue_store.save(item); self.refresh_queues(item["queue_id"])

    def _current_binding_snapshot(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for node_id, variable in self.node_binding_vars.items():
            profile_id = self.binding_display_to_id.get(variable.get())
            if profile_id:
                result[node_id] = profile_id
        return result

    def add_current_task_to_queue(self, queue_id: str) -> None:
        item = self.queue_docs.get(queue_id)
        if not item:
            return
        primitive = RECIPES.get(self.recipe_var.get(), {}).get("primitive", "SINGLE_TASK") if hasattr(self, "recipe_var") else "SINGLE_TASK"
        goal = self.task_text.get("1.0", "end").strip() or self.workflow_goal_text.get("1.0", "end").strip()
        if not goal:
            messagebox.showwarning("Task required", "Wpisz cel zadania przed dodaniem do kolejki.", parent=self.master); return
        if primitive in ("CUSTOM_JOB", "ADVANCED"):
            messagebox.showinfo("Kolejka", "Dla „Zmiana wieloczęściowa” i „Zaawansowane” zapisz spec na stronie Custom Job / Workflow, a potem dodaj do kolejki tam.", parent=self.master)
            return
        title = goal.splitlines()[0][:80]
        if primitive == "WORKFLOW":
            preset = EXECUTION_PRESETS[self.preset_var.get()]
            self._apply_preset_to_workflow()
            bindings = {nid: preset["bindings"].get(next((str(n.get("type")) for n in self._workflow_llm_nodes() if str(n.get("id")) == nid), ""), "")
                        for nid in ("N01", "N03", "N04")}
            bindings = {k: v for k, v in bindings.items() if v}
            mode, workflow_id = "WORKFLOW", self.workflow_var.get()
        else:
            mode, workflow_id, bindings = "SINGLE_TASK", "", {}
        try:
            self.queue_store.add_task(item, title=title, goal=goal, mode=mode, workflow_id=workflow_id, repo=self.repo_var.get(), worktree=self.worktree_var.get(), bindings=bindings)
        except ValueError as exc:
            messagebox.showwarning("Cannot add task", str(exc), parent=self.master); return
        self.refresh_queues(queue_id); self.show_page("queues")

    def add_queue_task_dialog(self) -> None:
        item = self.queue_docs.get(self.selected_queue_id)
        if not item:
            messagebox.showwarning("Queue required", "Wybierz kolejkę.", parent=self.master); return
        dialog = tk.Toplevel(self.master); dialog.title("Dodaj zadanie"); dialog.transient(self.master); dialog.grab_set(); dialog.geometry(self._center_dialog(720, 610)); dialog.minsize(620, 540)
        frame = ttk.Frame(dialog, padding=18); frame.grid(sticky="nsew"); frame.columnconfigure(1, weight=1)
        dialog.rowconfigure(0, weight=1); dialog.columnconfigure(0, weight=1)
        title_var = tk.StringVar(); mode_var = tk.StringVar(value="SINGLE_TASK"); workflow_var = tk.StringVar(value=self.workflow_var.get()); job_var=tk.StringVar(value=self.custom_job_last_spec); repo_var = tk.StringVar(value=self.repo_var.get()); worktree_var = tk.StringVar(value=self.worktree_var.get())
        labels = (("Nazwa", title_var), ("Tryb", mode_var), ("Workflow", workflow_var), ("Custom Job",job_var), ("Repository", repo_var), ("Worktree", worktree_var))
        for row, (label, variable) in enumerate(labels):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=4)
            if label == "Tryb":
                ttk.Combobox(frame, textvariable=variable, values=("SINGLE_TASK", "WORKFLOW", "CUSTOM_JOB"), state="readonly").grid(row=row, column=1, sticky="ew", pady=4)
            elif label == "Workflow":
                ttk.Combobox(frame, textvariable=variable, values=tuple(path for _label, path in self.workflow_choices), state="readonly").grid(row=row, column=1, sticky="ew", pady=4)
            elif label == "Custom Job":
                ttk.Combobox(frame,textvariable=variable,values=tuple(str(path) for path in sorted(JOBS.glob("*.json"))) if JOBS.is_dir() else (),state="normal").grid(row=row,column=1,sticky="ew",pady=4)
                ttk.Button(frame,text="Wybierz…",command=lambda v=variable: v.set(filedialog.askopenfilename(parent=dialog,title="Wybierz Custom Job",filetypes=(("JSON","*.json"),)) or v.get())).grid(row=row,column=2,padx=(6,0))
            else:
                ttk.Entry(frame, textvariable=variable).grid(row=row, column=1, sticky="ew", pady=4)
                if label in {"Repository","Worktree"}:
                    ttk.Button(frame,text="Wybierz…",command=lambda v=variable: v.set(filedialog.askdirectory(parent=dialog,title=f"Wybierz {label}",initialdir=v.get() or str(Path.home())) or v.get())).grid(row=row,column=2,padx=(6,0))
                    ttk.Button(frame,text="Otwórz folder",command=lambda v=variable:self.open_folder(v.get())).grid(row=row,column=3,padx=(6,0))
        ttk.Label(frame, text="Cel").grid(row=6, column=0, sticky="nw", pady=4)
        goal = tk.Text(frame, height=8, wrap="word"); goal.grid(row=6, column=1, sticky="nsew", pady=4); frame.rowconfigure(6, weight=1)
        binding_vars = {"IMPLEMENT": tk.StringVar(value=self.default_implement_profile_var.get()), "REVIEW": tk.StringVar(value=self.default_review_profile_var.get()), "REPAIR": tk.StringVar(value=self.default_repair_profile_var.get())}
        binding_frame = ttk.LabelFrame(frame, text="Execution Plan", padding=8); binding_frame.grid(row=7, column=0, columnspan=4, sticky="ew", pady=(10, 0)); binding_frame.columnconfigure(1, weight=1)
        displays = self._profile_choices()
        for row, (role, variable) in enumerate(binding_vars.items()):
            default_display = next((display for display, pid in self.binding_display_to_id.items() if pid == variable.get()), variable.get())
            variable.set(default_display); ttk.Label(binding_frame, text=role.title()).grid(row=row, column=0, sticky="w")
            combo = ttk.Combobox(binding_frame, textvariable=variable, values=displays, state="readonly")
            combo.grid(row=row, column=1, sticky="ew", padx=(8, 0), pady=2)
            self.profile_comboboxes.append((combo, variable))
        def submit(run_now: bool = False) -> None:
            bindings = {role: self.binding_display_to_id.get(variable.get(), "") for role, variable in binding_vars.items()}
            # Role keys are converted to concrete node IDs at START from the selected workflow.
            try:
                task = self.queue_store.add_task(item, title=title_var.get(), goal=goal.get("1.0", "end"), mode=mode_var.get(), workflow_id=workflow_var.get(), job_spec=job_var.get(), repo=repo_var.get(), worktree=worktree_var.get(), bindings=bindings if mode_var.get() == "WORKFLOW" else {})
            except ValueError as exc:
                messagebox.showwarning("Cannot add task", str(exc), parent=dialog); return
            dialog.destroy(); self.refresh_queues(item["queue_id"])
            if run_now:
                self.start_selected_queue()
        actions = ttk.Frame(frame); actions.grid(row=8, column=1, sticky="e", pady=(14, 0))
        ttk.Button(actions, text="Dodaj do kolejki", command=submit).grid(row=0, column=0)
        ttk.Button(actions, text="Dodaj i uruchom", command=lambda: submit(True), style="Primary.TButton").grid(row=0, column=1, padx=(8, 0))

    def move_queue_task(self, delta: int) -> None:
        item = self.queue_docs.get(self.selected_queue_id)
        if item and self.selected_queue_task_id and self.queue_store.reorder(item, self.selected_queue_task_id, delta):
            self._populate_queue_tasks()

    def delete_queue_task(self) -> None:
        item = self.queue_docs.get(self.selected_queue_id)
        if item and self.selected_queue_task_id and self.queue_store.delete_waiting(item, self.selected_queue_task_id):
            self.selected_queue_task_id = ""; self.refresh_queues(item["queue_id"])

    def open_selected_queue_run(self) -> None:
        item = self.queue_docs.get(self.selected_queue_id)
        task = next((row for row in item.get("tasks", []) if row.get("task_id") == self.selected_queue_task_id), None) if item else None
        if task and task.get("run_id"):
            self.current_run_id = str(task["run_id"]); self.show_page("runs")

    def show_queue_task_details(self) -> None:
        item = self.queue_docs.get(self.selected_queue_id)
        task = next((row for row in item.get("tasks", []) if row.get("task_id") == self.selected_queue_task_id), None) if item else None
        if not task:
            return
        run_id = str(task.get("run_id") or "")
        workflow_state = read_json_object(STATS / run_id / "WORKFLOW" / "workflow_state.json") if run_id else {}
        summary = workflow_state.get("workflow_summary", {}) if isinstance(workflow_state.get("workflow_summary"), Mapping) else {}
        completed = workflow_state.get("completed_nodes", []) if isinstance(workflow_state.get("completed_nodes"), list) else []
        tests = next((row.get("summary") or row.get("outcome") for row in completed if isinstance(row, Mapping) and row.get("node_type") == "MACHINE_GATE"), "Not recorded")
        review = next((row.get("summary") or row.get("outcome") for row in completed if isinstance(row, Mapping) and row.get("node_type") == "REVIEW"), "Not recorded")
        human = next((row.get("summary") or row.get("outcome") for row in completed if isinstance(row, Mapping) and row.get("node_type") == "HUMAN_GATE"), "Not recorded")
        text = (
            f"Status: {task.get('status')}\nRun ID: {run_id or 'Not recorded'}\n"
            f"Started: {task.get('started_at') or 'Not recorded'}\nFinished: {task.get('finished_at') or 'Not recorded'}\n"
            f"Execution profiles: {self._task_profiles_label(task)}\nTests: {tests}\nReview: {review}\n"
            f"Repair cycles: {summary.get('repair_cycles', summary.get('repair_count', 'Not recorded'))}\nHuman verdict: {human}"
        )
        messagebox.showinfo(str(task.get("title") or "Task details"), text, parent=self.master)

    def _active_queue_count(self) -> int:
        return sum(1 for item in self.queue_docs.values() if item.get("status") == "RUNNING")

    @staticmethod
    def _worktree_identity(value: Any) -> str:
        text = str(value or "").strip()
        return os.path.normcase(os.path.abspath(text)) if text else ""

    def _worktree_in_use(self, queue_id: str, worktree: str) -> bool:
        identity = self._worktree_identity(worktree)
        if not identity:
            return False
        for other_id, item in self.queue_docs.items():
            if other_id == queue_id:
                continue
            for task in item.get("tasks", []):
                if task.get("status") == "RUNNING" and self._worktree_identity(task.get("worktree")) == identity:
                    return True
        return False

    def _resolve_queue_bindings(self, task: dict[str, Any]) -> dict[str, str]:
        if task.get("mode") != "WORKFLOW":
            return {}
        path = Path(str(task.get("workflow_id") or ""))
        workflow = read_json_object(path)
        nodes = [node for node in workflow.get("nodes", []) if isinstance(node, Mapping) and node.get("type") in {"IMPLEMENT", "REVIEW", "REPAIR"}]
        if not nodes:
            raise ValueError("Workflow definition has no executable LLM nodes")
        snapshot = task.get("bindings", {}) if isinstance(task.get("bindings"), Mapping) else {}
        settings = self.queue_store.settings()
        defaults = {"IMPLEMENT": settings["default_implement_profile"], "REVIEW": settings["default_review_profile"], "REPAIR": settings["default_repair_profile"]}
        resolved: dict[str, str] = {}
        for node in nodes:
            node_id, role = str(node.get("id")), str(node.get("type"))
            profile_id = str(snapshot.get(node_id) or snapshot.get(role) or defaults[role])
            profile = self.profile_catalog.get(profile_id)
            if not profile:
                raise ValueError(f"{node_id}: unknown profile {profile_id}")
            availability = self.get_runtime_profile_state(profile_id)
            if availability["state"] not in {"VERIFIED", "AVAILABLE"}:
                raise ValueError(f"{node_id}: {profile.get('display_name', profile_id)} is {availability['state']}: {availability['reason']}")
            resolved[node_id] = profile_id
        return resolved

    def start_selected_queue(self) -> None:
        if self.selected_queue_id:
            self._start_queue(self.selected_queue_id)

    def _start_queue(self, queue_id: str) -> None:
        item = self.queue_docs.get(queue_id)
        if not item or item.get("status") == "RUNNING":
            return
        maximum = self.queue_store.settings()["max_simultaneous_active_queues"]
        if self._active_queue_count() >= maximum:
            item["status"] = "WAITING"; item["stop_reason"] = f"Concurrency limit {maximum} reached"; self.queue_store.save(item); self.refresh_queues(queue_id); return
        task = next((row for row in sorted(item.get("tasks", []), key=lambda row: int(row.get("position", 0))) if row.get("status") == "WAITING"), None)
        if not task:
            if item.get("tasks") and all(row.get("status") == "PASS" for row in item["tasks"]):
                item["status"] = "COMPLETED"
            else:
                item["status"] = "PAUSED"; item["stop_reason"] = "No WAITING task; review interrupted or stopped task"
            self.queue_store.save(item); self.refresh_queues(queue_id); return
        if self._worktree_in_use(queue_id, str(task.get("worktree") or "")):
            item["status"] = "WAITING"; item["stop_reason"] = "WORKTREE_IN_USE"; task["result_status"] = "WORKTREE_IN_USE"; self.queue_store.save(item); self.refresh_queues(queue_id); return
        try:
            if task.get("mode") == "WORKFLOW":
                if not all(str(task.get(key) or "").strip() for key in ("workflow_id", "repo", "worktree")):
                    raise ValueError("Workflow, repository and worktree are required")
                resolved = self._resolve_queue_bindings(task)
                argv = workflow_argv(str(task["workflow_id"]), str(task["goal"]), str(task["repo"]), str(task["worktree"]), False, [f"{node_id}={profile}" for node_id, profile in resolved.items()])
            elif task.get("mode") == "CUSTOM_JOB":
                if not Path(str(task.get("job_spec") or "")).is_file():
                    raise ValueError("Custom Job spec is required")
                resolved = {"frozen_job_reference": str(task["job_spec"])}
                argv = custom_job_argv(str(task["job_spec"]), False)
            else:
                resolved = {}
                argv = build_launcher_argv(str(task["goal"]), True, execution_mode="direct", working_directory=str(task.get("worktree") or task.get("repo") or ""))
        except ValueError as exc:
            task["status"] = "BLOCKED"; task["result_status"] = "INVALID"; task["finished_at"] = QueueStore.now()
            item["status"] = "BLOCKED"; item["stop_reason"] = str(exc); self.queue_store.save(item); self.refresh_queues(queue_id); return
        task["resolved_bindings"] = resolved
        task["status"] = "RUNNING"; task["started_at"] = QueueStore.now(); task["finished_at"] = None; task["process_pid"] = None
        item["status"] = "RUNNING"; item["pause_after_current"] = False; item.pop("stop_reason", None); item.pop("recovery_reason", None)
        self.queue_store.save(item); self.refresh_queues(queue_id)
        threading.Thread(target=self._run_queue_process, args=(queue_id, str(task["task_id"]), str(task["mode"]), argv), daemon=True).start()

    def _run_queue_process(self, queue_id: str, task_id: str, mode: str, argv: list[str]) -> None:
        try:
            process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", shell=False)
            self.events.put(("queue_process_started", (queue_id, task_id, process.pid)))
            stdout, stderr = process.communicate()
            result = (process.returncode, stdout, stderr, argv)
        except OSError as exc:
            result = (70, "", f"Cannot start queue task: {exc}", argv)
        self.events.put(("queue_task_complete", (queue_id, task_id, mode, result)))

    def _queue_process_started(self, queue_id: str, task_id: str, pid: int) -> None:
        item = self.queue_docs.get(queue_id)
        task = next((row for row in item.get("tasks", []) if row.get("task_id") == task_id), None) if item else None
        if task and task.get("status") == "RUNNING":
            task["process_pid"] = pid; self.queue_store.save(item); self._populate_queue_tasks()

    def _mark_queue_resume_running(self, owner: tuple[str, str]) -> None:
        item = self.queue_docs.get(owner[0])
        task = next((row for row in item.get("tasks", []) if row.get("task_id") == owner[1]), None) if item else None
        if item and task:
            task["status"] = "RUNNING"; task["process_pid"] = None; item["status"] = "RUNNING"; item.pop("stop_reason", None)
            self.queue_store.save(item); self.refresh_queues(owner[0])

    def _queue_result_status(self, mode: str, returncode: int, result: Mapping[str, Any]) -> str:
        raw = str(result.get("status") or result.get("outcome") or "").upper()
        if raw == "REJECTED":
            return "CANCELLED"
        if raw in QUEUE_STOP_RESULTS:
            return raw
        if returncode != 0:
            return "BLOCKED" if returncode in {21, 25, 26, 70} else "FAIL"
        if raw in {"PASS", "COMPLETED", "COMPLETE", "ACCEPTED", "WORKER_COMPLETED", "SUCCESS"}:
            return "PASS"
        # Existing launchers use several success labels; exit 0 remains their success contract.
        return "PASS"

    def _on_queue_task_complete(self, queue_id: str, task_id: str, mode: str, payload: tuple[int, str, str, list[str]]) -> None:
        returncode, stdout, stderr, argv = payload
        item = self.queue_docs.get(queue_id)
        task = next((row for row in item.get("tasks", []) if row.get("task_id") == task_id), None) if item else None
        if not item or not task:
            return
        result = parse_json_flex(stdout) if mode in {"WORKFLOW", "CUSTOM_JOB"} else parse_launcher_result(stdout)
        status = self._queue_result_status(mode, returncode, result)
        run_id = str(result.get("AAW_RUN_ID") or result.get("run_id") or "")
        task["run_id"] = run_id or task.get("run_id"); task["result_status"] = status; task["process_pid"] = None
        task["status"] = "WAITING_FOR_HUMAN" if status in {"WAITING_FOR_HUMAN", "WAITING_FOR_PLAN_APPROVAL", "WAITING_FOR_REPAIR_SELECTION", "HUMAN_REQUIRED", "HUMAN_GATE"} else ("BLOCKED" if status in {"BLOCKED", "INVALID", "WORKFLOW_LIMIT_REACHED"} else status)
        if task["status"] in {"PASS", "FAIL", "BLOCKED", "CANCELLED"}:
            task["finished_at"] = QueueStore.now()
        if run_id:
            self.queue_run_owners[run_id] = (queue_id, task_id)
        self.current_task = str(task.get("goal") or ""); self.current_result = result; self.current_run_id = run_id
        self._set_text(self.output, f"QUEUE COMMAND (shell=False):\n{subprocess.list2cmdline(argv)}\n\nSTDOUT:\n{stdout or '(empty)'}\n\nSTDERR:\n{stderr or '(empty)'}")
        remaining = any(row.get("status") == "WAITING" for row in item.get("tasks", []))
        queue_status, dispatch_next = queue_decision(status, auto_continue=bool(item.get("auto_continue")), pause_after_current=bool(item.get("pause_after_current")), remaining_waiting=remaining)
        item["status"] = queue_status
        if queue_status in {"PAUSED", "BLOCKED", "WAITING_FOR_HUMAN"}:
            item["stop_reason"] = status
        item["pause_after_current"] = False
        self.queue_store.save(item); self.refresh_queues(queue_id); self.refresh_artifacts(select_run=run_id)
        if status in {"WAITING_FOR_HUMAN", "WAITING_FOR_PLAN_APPROVAL", "WAITING_FOR_REPAIR_SELECTION", "HUMAN_REQUIRED", "HUMAN_GATE"}:
            if mode in {"WORKFLOW", "CUSTOM_JOB"}:
                self.workflow_human_run_id = run_id; self._show_human_gate(result)
            else:
                self._show_human_required(run_id)
        elif dispatch_next:
            self._start_queue(queue_id)
        self._start_waiting_queues()

    def _recover_queue_processes(self) -> None:
        changed_ids: list[str] = []
        for queue_id, item in self.queue_docs.items():
            for task in item.get("tasks", []):
                pid = task.get("process_pid")
                if task.get("status") == "RUNNING" and pid and not process_exists(pid):
                    task["status"] = "INTERRUPTED"; task["process_pid"] = None; task["result_status"] = "INTERRUPTED"
                    item["status"] = "PAUSED"; item["recovery_reason"] = "Runtime process ended while Control Center was not supervising it; human decision required"
                    self.queue_store.save(item); changed_ids.append(queue_id)
        if changed_ids:
            self.refresh_queues(self.selected_queue_id)

    def pause_selected_queue(self) -> None:
        item = self.queue_docs.get(self.selected_queue_id)
        if not item:
            return
        if item.get("status") == "RUNNING":
            item["pause_after_current"] = True; item["stop_reason"] = "Pause requested after current task"
        else:
            item["status"] = "PAUSED"; item["stop_reason"] = "Paused by operator"
        self.queue_store.save(item); self.refresh_queues(item["queue_id"])

    def resume_selected_queue(self) -> None:
        item = self.queue_docs.get(self.selected_queue_id)
        if not item or item.get("status") == "RUNNING":
            return
        interrupted = next((row for row in item.get("tasks", []) if row.get("status") == "INTERRUPTED"), None)
        if interrupted:
            if not messagebox.askyesno("Resume interrupted task", "Reset the selected interrupted task to WAITING? It will not be treated as PASS or FAIL.", parent=self.master):
                return
            interrupted["status"] = "WAITING"; interrupted["result_status"] = None
        if any(row.get("status") in {"FAIL", "BLOCKED", "WAITING_FOR_HUMAN"} for row in item.get("tasks", [])):
            messagebox.showwarning("Human decision required", "Resolve the stopped task in the existing Human Gate / run view before continuing.", parent=self.master); return
        item["status"] = "IDLE"; item.pop("stop_reason", None); item.pop("recovery_reason", None); self.queue_store.save(item); self._start_queue(item["queue_id"])

    def _start_waiting_queues(self) -> None:
        maximum = self.queue_store.settings()["max_simultaneous_active_queues"]
        for item in self.queue_docs.values():
            if self._active_queue_count() >= maximum:
                break
            if item.get("status") == "WAITING":
                self._start_queue(str(item["queue_id"]))

    def _build_runs_page(self) -> None:
        page = ttk.Frame(self.page_host, style="Page.TFrame", padding=(28, 22, 28, 24))
        page.grid(row=0, column=0, sticky="nsew")
        page.rowconfigure(3, weight=1)
        page.columnconfigure(0, weight=1)
        self.pages["runs"] = page
        ttk.Label(page, text="Przebiegi", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(page, text="Najpierw podsumowanie; artefakty i telemetry są dostępne na żądanie.", style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(5, 14))
        filter_row = ttk.Frame(page, style="Page.TFrame")
        filter_row.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        filter_row.columnconfigure(1, weight=1)
        ttk.Label(filter_row, text="Szukaj", style="Muted.TLabel").grid(row=0, column=0)
        ttk.Entry(filter_row, textvariable=self.filter_var).grid(row=0, column=1, sticky="ew", padx=8)
        self.filter_var.trace_add("write", lambda *_: self._populate_runs())
        ttk.Button(filter_row, text="Odśwież", command=self.refresh_artifacts).grid(row=0, column=2)
        ttk.Checkbutton(filter_row, text="Auto", variable=self.auto_var).grid(row=0, column=3, padx=(8, 0))
        split = ttk.Panedwindow(page, orient="horizontal")
        split.grid(row=3, column=0, sticky="nsew")
        left = ttk.Frame(split, style="Card.TFrame", padding=12)
        right = ttk.Frame(split, style="Card.TFrame", padding=12)
        split.add(left, weight=1)
        split.add(right, weight=3)
        left.rowconfigure(2, weight=2)
        left.rowconfigure(4, weight=1)
        left.columnconfigure(0, weight=1)
        ttk.Label(left, text="Lista", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(left, textvariable=self.warning_var, style="CardMuted.TLabel", wraplength=280).grid(row=1, column=0, sticky="ew", pady=(4, 8))
        self.runs = tk.Listbox(left, exportselection=False, borderwidth=0, highlightthickness=1)
        self.runs.grid(row=2, column=0, sticky="nsew")
        self.runs.bind("<<ListboxSelect>>", self._run_selected)
        ttk.Label(left, text="Pliki", style="CardTitle.TLabel").grid(row=3, column=0, sticky="w", pady=(12, 5))
        self.files = tk.Listbox(left, exportselection=False, borderwidth=0, highlightthickness=1)
        self.files.grid(row=4, column=0, sticky="nsew")
        self.files.bind("<<ListboxSelect>>", self._file_selected)
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)
        tabs = ttk.Notebook(right)
        tabs.grid(row=0, column=0, sticky="nsew")
        summary_tab = ttk.Frame(tabs, padding=8)
        timeline_tab = ttk.Frame(tabs, padding=8)
        artifacts_tab = ttk.Frame(tabs, padding=8)
        telemetry_tab = ttk.Frame(tabs, padding=8)
        tabs.add(summary_tab, text="Summary")
        tabs.add(timeline_tab, text="Timeline")
        tabs.add(artifacts_tab, text="Artifacts")
        tabs.add(telemetry_tab, text="Telemetry")
        for tab in (summary_tab, timeline_tab, artifacts_tab, telemetry_tab):
            tab.rowconfigure(0, weight=1); tab.columnconfigure(0, weight=1)
        self.summary = ttk.Treeview(summary_tab, columns=("value",), show="tree headings")
        self.summary.heading("#0", text="Pole"); self.summary.heading("value", text="Wartość")
        self.summary.column("#0", width=190, stretch=False); self.summary.grid(row=0, column=0, sticky="nsew")
        self.timeline = tk.Text(timeline_tab, wrap="none", state="disabled")
        self.timeline.grid(row=0, column=0, sticky="nsew")
        self.viewer = tk.Text(artifacts_tab, wrap="none", state="disabled")
        self.viewer.grid(row=0, column=0, sticky="nsew")
        self.output = tk.Text(telemetry_tab, wrap="word", state="disabled")
        self.output.grid(row=0, column=0, sticky="nsew")

    def _build_settings_page(self) -> None:
        page = self._page("settings")
        body = page.content
        self._title(body, "Ustawienia", "Domyślne zachowanie, modele, ścieżki i diagnostyka systemowa.")
        row = 2
        for title, lines in (
            ("GENERAL", ("Default mode: Single task", "Default workflow: Implement → Test → Review → Repair", "Recent repository paths are remembered locally.")),
            ("EXECUTION", ("Direct CLI — recommended", "ORCA — experimental", "Current policy: explicit launch, no auto-merge")),
            ("MODELS", tuple(self._profile_settings_lines())),
            ("WORKFLOW DEFAULTS", ("IMPLEMENT: Terra / high", "REVIEW: Sol / high", "REPAIR: Sol / medium", "Limits remain owned by the workflow definition.")),
            ("PATHS", (f"AAW root: {AAW_ROOT}", f"Playbook: {PLAYBOOK}", f"Stats: {STATS}", f"Routing: {ROUTING}", f"Workflows: {WORKFLOWS}")),
        ):
            card = ttk.Frame(body, style="Card.TFrame", padding=16)
            card.grid(row=row, column=0, sticky="ew", pady=(0, 10)); card.columnconfigure(0, weight=1)
            ttk.Label(card, text=title, style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
            if title == "MODELS":
                ttk.Label(card, textvariable=self.profile_status_var, style="CardMuted.TLabel", justify="left", wraplength=820).grid(row=1, column=0, sticky="w", pady=(8, 0))
            else:
                ttk.Label(card, text="\n".join(lines), style="CardMuted.TLabel", justify="left", wraplength=820).grid(row=1, column=0, sticky="w", pady=(8, 0))
            row += 1
        prefs = ttk.Frame(body, style="Card.TFrame", padding=16)
        prefs.grid(row=row, column=0, sticky="ew", pady=(0, 10)); prefs.columnconfigure(1, weight=1)
        ttk.Label(prefs, text="PREFERENCJE (Nowe zadanie)", style="CardTitle.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(prefs, text="Domyślny przepis").grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Combobox(prefs, textvariable=self.setting_default_recipe_var,
                     values=tuple(spec["label"] for spec in RECIPES.values()), state="readonly", width=32
                     ).grid(row=1, column=1, sticky="w", padx=8, pady=(10, 0))
        ttk.Label(prefs, text="Domyślny preset wykonania").grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Combobox(prefs, textvariable=self.setting_default_preset_var,
                     values=tuple(spec["label"] for spec in EXECUTION_PRESETS.values()), state="readonly", width=32
                     ).grid(row=2, column=1, sticky="w", padx=8, pady=(6, 0))
        ttk.Label(prefs, text="Domyślna polityka preprocessingu").grid(row=3, column=0, sticky="w", pady=(6, 0))
        ttk.Combobox(prefs, textvariable=self.setting_preprocess_var,
                     values=("AUTO_SAFE", "OFF", "CUSTOM"), state="readonly", width=32
                     ).grid(row=3, column=1, sticky="w", padx=8, pady=(6, 0))
        ttk.Checkbutton(prefs, text="Zapamiętuj ostatni Repository / Worktree",
                        variable=self.setting_remember_ws_var).grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Checkbutton(prefs, text="Odświeżaj wgląd przy otwarciu strony",
                        variable=self.setting_analytics_auto_var).grid(row=5, column=0, columnspan=2, sticky="w")
        ttk.Button(prefs, text="Zapisz preferencje", command=self.save_app_settings).grid(row=1, column=2, rowspan=2, sticky="e")
        row += 1
        queue_settings = ttk.Frame(body, style="Card.TFrame", padding=16)
        queue_settings.grid(row=row, column=0, sticky="ew", pady=(0, 10)); queue_settings.columnconfigure(1, weight=1)
        ttk.Label(queue_settings, text="KOLEJKI", style="CardTitle.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(queue_settings, text="Max simultaneous active queues").grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Spinbox(queue_settings, from_=1, to=3, textvariable=self.max_active_queues_var, width=6, state="readonly").grid(row=1, column=1, sticky="w", padx=8, pady=(10, 0))
        ttk.Checkbutton(queue_settings, text="Default auto-continue", variable=self.default_queue_auto_var).grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        workflow_values = tuple(path for _label, path in self.workflow_choices)
        profile_values = self._profile_choices()
        for offset, (label, variable, values) in enumerate((("Default workflow", self.default_queue_workflow_var, workflow_values), ("Default IMPLEMENT profile", self.default_implement_profile_var, profile_values), ("Default REVIEW profile", self.default_review_profile_var, profile_values), ("Default REPAIR profile", self.default_repair_profile_var, profile_values)), start=3):
            ttk.Label(queue_settings, text=label).grid(row=offset, column=0, sticky="w", pady=(6, 0))
            combo = ttk.Combobox(queue_settings, textvariable=variable, values=values, state="readonly", width=48)
            combo.grid(row=offset, column=1, sticky="w", padx=8, pady=(6, 0))
            if "profile" in label.lower():
                self.profile_comboboxes.append((combo, variable))
        ttk.Button(queue_settings, text="Zapisz ustawienia kolejek", command=self.save_queue_settings).grid(row=1, column=2, rowspan=2, sticky="e")
        row += 1
        system = ttk.Frame(body, style="Card.TFrame", padding=16)
        system.grid(row=row, column=0, sticky="ew", pady=(0, 24)); system.columnconfigure(0, weight=1)
        ttk.Label(system, text="SYSTEM / PREFLIGHT", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        self.preflight_button = ttk.Button(system, text="RUN PREFLIGHT", command=self.run_preflight)
        self.preflight_button.grid(row=0, column=1, sticky="e")
        ttk.Label(system, textvariable=self.version_warning_var, foreground="#9b1c1c", background="#ffffff", wraplength=700).grid(row=1, column=0, columnspan=2, sticky="w", pady=(5, 8))
        self.preflight = ttk.Treeview(system, columns=("value",), show="tree headings", height=15)
        self.preflight.heading("#0", text="Check"); self.preflight.heading("value", text="Value")
        self.preflight.column("#0", width=220, stretch=False); self.preflight.column("value", width=620)
        self.preflight.grid(row=2, column=0, columnspan=2, sticky="ew")

    def show_page(self, key: str) -> None:
        if key not in self.pages:
            key = "new"
        previous = self.pages.get(self.active_page)
        if isinstance(previous, ScrollablePage):
            previous.deactivate()
        self.pages[key].tkraise()
        if isinstance(self.pages[key], ScrollablePage):
            self.pages[key].activate()
        self.active_page = key
        for page_key, button in self.nav_buttons.items():
            button.configure(background="#334155" if page_key == key else "#202733")
        if key == "runs":
            self.refresh_artifacts(select_run=self.current_run_id)
        elif key == "queues":
            self.refresh_queues()
        elif key == "home":
            self._refresh_home()
        elif key == "insights":
            self._refresh_insights(auto=self.app_settings.get("analytics_auto_refresh_on_open", True))

    def save_queue_settings(self) -> None:
        try:
            self.queue_store.save_settings({
                "max_simultaneous_active_queues": self.max_active_queues_var.get(),
                "default_auto_continue": self.default_queue_auto_var.get(),
                "default_workflow": self.default_queue_workflow_var.get(),
                "default_implement_profile": self.binding_display_to_id.get(self.default_implement_profile_var.get(), "TERRA_HIGH"),
                "default_review_profile": self.binding_display_to_id.get(self.default_review_profile_var.get(), "SOL_HIGH"),
                "default_repair_profile": self.binding_display_to_id.get(self.default_repair_profile_var.get(), "SOL_MEDIUM"),
            })
        except (TypeError, ValueError) as exc:
            messagebox.showwarning("Invalid queue settings", str(exc), parent=self.master); return
        self._start_waiting_queues()

    def save_app_settings(self) -> None:
        recipe_by_label = {spec["label"]: key for key, spec in RECIPES.items()}
        preset_by_label = {spec["label"]: key for key, spec in EXECUTION_PRESETS.items()}
        self.app_settings = self.app_settings_store.save({
            "default_recipe": recipe_by_label.get(self.setting_default_recipe_var.get(), "IMPLEMENT_VERIFY"),
            "default_execution_preset": preset_by_label.get(self.setting_default_preset_var.get(), "BALANCED"),
            "default_preprocess_policy": self.setting_preprocess_var.get(),
            "remember_last_workspace": bool(self.setting_remember_ws_var.get()),
            "analytics_auto_refresh_on_open": bool(self.setting_analytics_auto_var.get()),
        })
        self.recipe_var.set(self.app_settings["default_recipe"])
        self.preset_var.set(self.app_settings["default_execution_preset"])
        self.preset_display_var.set(EXECUTION_PRESETS[self.preset_var.get()]["label"])
        self._recipe_changed()
        messagebox.showinfo("Preferencje", "Zapisano preferencje Nowego zadania.", parent=self.master)

    # ------------------------------------------------------------------
    # Insights (derived analytics over 03_STATS)
    # ------------------------------------------------------------------
    def _build_insights_page(self) -> None:
        page = self._page("insights")
        body = page.content
        self._title(body, "Wgląd", "Pochodny indeks nad dowodami z 03_STATS. Bez wniosków przyczynowych; każde porównanie pokazuje N.")

        if aaw_analytics is None or ui_charts is None:
            ttk.Label(body, text="Moduł analityki niedostępny (ANALYTICS/aaw_analytics.py nie zaimportowany).",
                      style="Muted.TLabel").grid(row=2, column=0, sticky="w")
            return

        bar = ttk.Frame(body, style="Page.TFrame"); bar.grid(row=2, column=0, sticky="ew"); bar.columnconfigure(6, weight=1)
        ttk.Button(bar, text="Odśwież analitykę", command=lambda: self._refresh_insights(force=True)).grid(row=0, column=0, padx=(0, 6))
        ttk.Button(bar, text="Przebuduj z 03_STATS", command=self._rebuild_insights).grid(row=0, column=1, padx=6)
        self.insight_meta_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.insight_meta_var, style="Muted.TLabel").grid(row=0, column=6, sticky="e")

        filt = ttk.Frame(body, style="Card.TFrame", padding=12)
        filt.grid(row=3, column=0, sticky="ew", pady=(10, 10));
        for c in range(8):
            filt.columnconfigure(c, weight=1)
        ttk.Label(filt, text="Filtry", style="CardTitle.TLabel").grid(row=0, column=0, columnspan=8, sticky="w")
        specs = [("date_from", "Od (RRRR-MM-DD)"), ("date_to", "Do (RRRR-MM-DD)"), ("job_class", "Typ zadania"),
                 ("node_type", "Rola / typ node"), ("model", "Model"), ("profile", "Profil"),
                 ("invocation_kind", "Rodzaj wywołania"), ("grain", "Ziarno dowodu")]
        for i, (key, label) in enumerate(specs):
            var = tk.StringVar()
            self.insight_filter_vars[key] = var
            ttk.Label(filt, text=label, style="CardMuted.TLabel").grid(row=1, column=i, sticky="w", padx=4)
            if key in ("date_from", "date_to"):
                ttk.Entry(filt, textvariable=var, width=14).grid(row=2, column=i, sticky="ew", padx=4, pady=(2, 0))
            else:
                combo = ttk.Combobox(filt, textvariable=var, values=("",), state="readonly", width=16)
                combo.grid(row=2, column=i, sticky="ew", padx=4, pady=(2, 0))
                self.insight_filter_vars[f"__combo_{key}"] = combo  # type: ignore[assignment]
        self.insight_include_fixtures = tk.BooleanVar(value=False)
        ttk.Checkbutton(filt, text="Uwzględnij przebiegi testowe / fixture",
                        variable=self.insight_include_fixtures,
                        command=lambda: self._refresh_insights(force=True)).grid(
            row=3, column=2, columnspan=4, sticky="w", pady=(8, 0))
        ttk.Button(filt, text="Zastosuj", command=lambda: self._refresh_insights(force=True)).grid(row=3, column=0, sticky="w", pady=(8, 0))
        ttk.Button(filt, text="Wyczyść", command=self._clear_insight_filters).grid(row=3, column=1, sticky="w", pady=(8, 0))
        ttk.Button(filt, text="Eksport CSV…", command=self._export_insights_csv).grid(row=3, column=7, sticky="e", pady=(8, 0))

        cards = ttk.Frame(body, style="Page.TFrame"); cards.grid(row=4, column=0, sticky="ew", pady=(0, 6))
        for c in range(4):
            cards.columnconfigure(c, weight=1)
        self.insight_cards: dict[str, tk.StringVar] = {}
        for i, (key, label) in enumerate((("runs", "Przebiegi"),
                                          ("llm_calls", "Wykonania LLM (execution)"),
                                          ("median_wall_time_s", "Mediana czasu (s)"),
                                          ("human_acceptance_coverage", "Dowód człowieka"))):
            card = ttk.Frame(cards, style="Card.TFrame", padding=12); card.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 6, 0))
            var = tk.StringVar(value="—"); self.insight_cards[key] = var
            ttk.Label(card, text=label, style="CardMuted.TLabel").grid(row=0, column=0, sticky="w")
            ttk.Label(card, textvariable=var, style="CardTitle.TLabel", font=("Segoe UI Semibold", 15)).grid(row=1, column=0, sticky="w")

        charts = ttk.Frame(body, style="Page.TFrame"); charts.grid(row=5, column=0, sticky="ew")
        charts.columnconfigure(0, weight=1); charts.columnconfigure(1, weight=1)
        self.chart_usage = ui_charts.Chart(charts, "A · Zużycie w czasie (wywołania / tokeny)")
        self.chart_usage.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        self.chart_outcome = ui_charts.Chart(charts, "B · Wynik wykonania wg modelu w tej samej roli (obserwacja)")
        self.chart_outcome.grid(row=1, column=0, sticky="ew", padx=(0, 5), pady=(0, 10))
        self.chart_review = ui_charts.Chart(charts, "C · Audyt za pierwszym razem (jawna linia wykonań)")
        self.chart_review.grid(row=1, column=1, sticky="ew", padx=(5, 0), pady=(0, 10))
        self.chart_time = ui_charts.Chart(charts, "D · Mediana czasu wykonania")
        self.chart_time.grid(row=2, column=0, sticky="ew", padx=(0, 5), pady=(0, 10))
        self.chart_qwen = ui_charts.Chart(charts, "E · Lokalny preprocessing Qwen")
        self.chart_qwen.grid(row=2, column=1, sticky="ew", padx=(5, 0), pady=(0, 10))

        life = ttk.Frame(body, style="Card.TFrame", padding=14)
        life.grid(row=6, column=0, sticky="ew", pady=(4, 6)); life.columnconfigure(0, weight=1)
        ttk.Label(life, text="Kondycja cyklu życia wykonań (dowód operacyjny, nie jakość modelu)",
                  style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        self.insight_lifecycle_var = tk.StringVar(value="")
        ttk.Label(life, textvariable=self.insight_lifecycle_var, style="CardMuted.TLabel",
                  justify="left", wraplength=1000).grid(row=1, column=0, sticky="w", pady=(8, 0))

        dq = ttk.Frame(body, style="Card.TFrame", padding=14)
        dq.grid(row=7, column=0, sticky="ew", pady=(4, 24)); dq.columnconfigure(0, weight=1)
        ttk.Label(dq, text="Jakość danych (ziarno wykonania)", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        self.insight_dq_var = tk.StringVar(value="")
        ttk.Label(dq, textvariable=self.insight_dq_var, style="CardMuted.TLabel", justify="left", wraplength=1000).grid(row=1, column=0, sticky="w", pady=(8, 0))

    def _insight_filters(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if getattr(self, "insight_include_fixtures", None) is not None \
                and bool(self.insight_include_fixtures.get()):
            out["include_fixtures"] = True
        for key in ("date_from", "date_to", "job_class", "node_type", "model", "profile",
                    "invocation_kind", "grain"):
            value = self.insight_filter_vars.get(key)
            if value is not None and str(value.get()).strip():
                out[key] = str(value.get()).strip()
        return out

    def _clear_insight_filters(self) -> None:
        for key in ("date_from", "date_to", "job_class", "node_type", "model", "profile",
                    "invocation_kind", "grain"):
            if key in self.insight_filter_vars:
                self.insight_filter_vars[key].set("")
        if getattr(self, "insight_include_fixtures", None) is not None:
            self.insight_include_fixtures.set(False)
        self._refresh_insights(force=True)

    def _rebuild_insights(self) -> None:
        if aaw_analytics is None:
            return
        self.insight_meta_var.set("Przebudowa…")
        def worker() -> None:
            try:
                summary = aaw_analytics.ingest(ANALYTICS_DB, STATS, rebuild=True)
                self.events.put(("insights_built", summary))
            except Exception as exc:  # noqa: BLE001
                self.events.put(("insights_built", {"error": str(exc)}))
        threading.Thread(target=worker, daemon=True).start()

    def _refresh_insights(self, *, force: bool = False, auto: bool = False) -> None:
        if aaw_analytics is None or ui_charts is None or self.analytics is None:
            return
        if (force or auto) and not self.analytics.available:
            self._rebuild_insights()
            return
        if (force or auto) and self.analytics.available and (force or self.app_settings.get("analytics_auto_refresh_on_open", True)):
            def worker() -> None:
                try:
                    aaw_analytics.ingest(ANALYTICS_DB, STATS, rebuild=False)
                except Exception:  # noqa: BLE001
                    pass
                self.events.put(("insights_built", {"refreshed": True}))
            threading.Thread(target=worker, daemon=True).start()
            return
        self._render_insights()

    def _render_insights(self) -> None:
        if self.analytics is None or not self.analytics.available:
            self.insight_meta_var.set("Indeks niedostępny — kliknij „Przebuduj z 03_STATS”.")
            for chart in (self.chart_usage, self.chart_outcome, self.chart_review, self.chart_time, self.chart_qwen):
                chart.show_note("Brak indeksu analitycznego")
            return
        meta = self.analytics.meta()
        stamp = meta.get("last_refresh") or meta.get("last_rebuild") or "?"
        self.insight_meta_var.set(f"Indeks: {meta.get('schema_version', '?')} · {stamp}")
        options = self.analytics.filter_options()
        for key in ("job_class", "node_type", "model", "profile", "invocation_kind", "grain"):
            combo = self.insight_filter_vars.get(f"__combo_{key}")
            if isinstance(combo, ttk.Combobox):
                combo.configure(values=("",) + tuple(options.get(key, [])))
        filters = self._insight_filters()
        with_fixtures = bool(filters.get("include_fixtures"))

        km = self.analytics.key_metrics(filters)
        self.insight_cards["runs"].set(str(km["runs"]))
        self.insight_cards["llm_calls"].set(f"{km['llm_calls']}  (+{km['machine_gate_calls']} bramek)")
        self.insight_cards["median_wall_time_s"].set(
            f"{km['median_wall_time_s']}  (N={km['wall_time_sample']})"
            if km["median_wall_time_s"] is not None else "—")
        self.insight_cards["human_acceptance_coverage"].set(
            f"{km['human_acceptance_coverage']}  ·  jakość {km['human_quality_coverage']}")

        usage = self.analytics.usage_over_time(filters)
        if usage:
            self.chart_usage.draw_lines(
                [r["day"] for r in usage],
                {"wykonania LLM": [r["calls"] for r in usage],
                 "bramki maszynowe": [r["machine_gate_calls"] for r in usage],
                 "tok. wej (k)": [round((r["input_tokens"] or 0) / 1000, 1) for r in usage],
                 "tok. wyj (k)": [round((r["output_tokens"] or 0) / 1000, 1) for r in usage]},
            )
            covered = sum(r["calls_with_usage"] for r in usage)
            total = sum(r["calls"] for r in usage)
            self.chart_usage.set_caption(
                f"Zliczane w ziarnie execution_id. Bramki maszynowe osobno; pominięty preprocessing "
                f"nie ma tożsamości wykonania i nie jest liczony. Telemetria tokenów: {covered}/{total} wykonań.")
        else:
            self.chart_usage.show_note("Brak danych dla bieżących filtrów")

        outcome = self.analytics.outcome_by_model_profile(filters)[:6]
        if outcome:
            labels = [f"{r['model'] or '?'}/{r['effort'] or '?'}\n{r['node_type']}" for r in outcome]
            self.chart_outcome.draw_grouped_bars(
                labels, ["PASS", "FAIL", "INVALID", "BLOCKED"],
                [[r["pass_n"], r["fail_n"], r["invalid_n"], r["blocked_n"]] for r in outcome],
                sample_sizes=[r["n"] for r in outcome],
            )
            small = [r for r in outcome if r["insufficient_evidence"]]
            unknown = sum(r["unknown_n"] for r in outcome)
            self.chart_outcome.set_caption(
                "Grupowanie w obrębie jednej roli. OBSERWACJA, nie ranking modeli — przydział nie był losowy."
                + (f" Bez wyniku semantycznego: {unknown}." if unknown else "")
                + (f" {len(small)} grup poniżej progu N={aaw_analytics.MIN_SAMPLE}." if small else "")
                + ("" if with_fixtures else " Fixture/smoke wykluczone."))
        else:
            self.chart_outcome.show_note(
                "Brak danych dla bieżących filtrów"
                + ("" if with_fixtures else " (fixture/smoke wykluczone — zaznacz „Uwzględnij przebiegi testowe”)"))

        rfp = self.analytics.review_first_pass(filters)
        legacy = rfp["legacy_source_grain"]
        if rfp["n"] and not rfp["insufficient_evidence"]:
            self.chart_review.draw_bars(
                ["audyt OK od razu", "wymagało naprawy"],
                [rfp["first_pass"] or 0, rfp["needed_repair"] or 0],
                sample_sizes=[rfp["n"], rfp["n"]])
            self.chart_review.set_caption(
                f"N={rfp['n']} wykonań REVIEW połączonych jawną linią wykonanie→audyt→naprawa. "
                f"Legacy (ziarno źródła, osobno): N={legacy['n']}. "
                f"Werdykt człowieka: ACCEPT {rfp['accepted']} · REJECT {rfp['rejected']} · "
                f"ZOSTAW NA PÓŹNIEJ {rfp['left_for_later']} (to nie jest odrzucenie).")
        else:
            self.chart_review.show_note(
                f"INSUFFICIENT EVIDENCE (N={rfp['n']}, próg {aaw_analytics.MIN_SAMPLE})"
                + (f" · legacy N={legacy['n']}" if legacy["n"] else "")
                + ("" if with_fixtures else " · fixture/smoke wykluczone"))

        times = self.analytics.median_exec_time("model", filters)
        if times:
            self.chart_time.draw_bars(
                [t["group"] for t in times], [t["median_wall_time_s"] for t in times],
                sample_sizes=[t["n"] for t in times],
                insufficient=[t["insufficient_evidence"] for t in times], value_suffix="s")
            self.chart_time.set_caption(
                "Mediana czasu z telemetrii wykonania (lub z obserwowanych czasów w ledgerze). "
                "Mediana, nie średnia." + ("" if with_fixtures else " Fixture/smoke wykluczone."))
        else:
            self.chart_time.show_note(
                "Brak danych dla bieżących filtrów"
                + ("" if with_fixtures else " (fixture/smoke wykluczone)"))

        qp = self.analytics.qwen_preprocess_metrics(filters)
        if qp["decisions"]:
            self.chart_qwen.draw_bars(
                ["decyzje", "wykonane", "pominięte", "lokalny niedostępny", "timeout/err", "bez powiązania"],
                [qp["decisions"], qp["completed_advisory"], qp["skipped"],
                 qp["skipped_local_unavailable"], qp["timeout_or_error"], qp["unlinked"]])
            reduction = qp["estimated_context_reduction_tokens_median"]
            self.chart_qwen.set_caption(
                f"Mediana latencji: {qp['median_latency_s'] if qp['median_latency_s'] is not None else '—'}s. "
                f"ESTIMATED CONTEXT REDUCTION (mediana): {reduction if reduction is not None else '—'} tok "
                f"(N={qp['estimated_context_reduction_sample']}). {qp['note']}")
        else:
            self.chart_qwen.show_note("Brak artefaktów preprocessingu")

        health = self.analytics.lifecycle_health(filters)
        self.insight_lifecycle_var.set(
            f"Zamknięte: {health['closed']}  ·  Uruchomione bez zamknięcia: {health['started_open']}  ·  "
            f"Tylko intencja: {health['intent_only']}  ·  Konflikty: {health['conflicts']}  ·  "
            f"Bez dowodu w ledgerze: {health['unknown']}  (z {health['total']} wykonań)\n"
            f"Pewność efektu przy zamknięciu: {health['effect_certainty'] or '—'}  ·  "
            f"Powody zamknięcia: {health['close_reasons'] or '—'}\n"
            f"{health['note']}")

        dq = self.analytics.data_quality()
        prov = dq["executions_with_model_provenance"]
        toks = dq["executions_with_token_telemetry"]
        sem = dq["executions_with_semantic_outcome"]
        rev = dq["executions_with_review_outcome"]
        release = dq["release_verdict_coverage"]
        quality = dq["quality_assessment_coverage"]
        self.insight_dq_var.set(
            f"Przebiegi: {dq['indexed_runs']}  (modern {dq['modern_runs']} · legacy {dq['legacy_runs']} · "
            f"nieznane ziarno {dq['unknown_grain_runs']})  ·  z ledgerem: {dq['runs_with_ledger']}\n"
            f"Wykonania: modern {dq['modern_executions']} · wiersze legacy (bez tożsamości) "
            f"{dq['legacy_source_grain_rows']}  ·  LLM {dq['llm_executions']} · bramki {dq['machine_gate_executions']}\n"
            f"Cykl życia: CLOSED {dq['lifecycle_closed']} · STARTED_OPEN {dq['lifecycle_started_open']} · "
            f"INTENT_ONLY {dq['lifecycle_intent_only']} · KONFLIKT {dq['lifecycle_conflicts']} · "
            f"UNKNOWN {dq['lifecycle_unknown']}  ({dq['complete_lifecycle_pct']}% zamkniętych)\n"
            f"Proweniencja modelu: {prov['label']} ({prov['pct']}%)  ·  telemetria tokenów: "
            f"{toks['label']} ({toks['pct']}%)  ·  wynik semantyczny: {sem['label']} ({sem['pct']}%)  ·  "
            f"objęte audytem: {rev['label']} ({rev['pct']}%)\n"
            f"Bez powiązania: preprocessing {dq['unlinked_preprocess_records']} · ustalenia "
            f"{dq['unlinked_findings']} (bez naprawy: {dq['findings_without_repair_link']}) · naprawy "
            f"{dq['unlinked_repairs']} · commity {dq['unlinked_commits']} · cykl życia bez wykonania "
            f"{dq['lifecycle_without_indexed_execution']}\n"
            f"Commity: {dq['commits']} (potwierdzone w state+ledger: "
            f"{dq['commits_cross_validated_state_and_ledger']})  ·  kandydaci: {dq['candidates']}  ·  "
            f"Human Gate: {dq['human_gate_reached']}\n"
            f"Werdykt wydania: {release['label']} ({release['pct']}%)  ·  ocena jakości przez człowieka: "
            f"{quality['label']} ({quality['pct']}%)  — to dwie różne rzeczy\n"
            f"Fixture / testy: {dq['fixture_runs']} przebiegów (nieokreślone: {dq['unknown_fixture_runs']}) — "
            f"domyślnie wykluczone z obserwacji jakości modelu\n"
            f"Integralność: DATA_INTEGRITY_ERROR {dq['data_integrity_errors']} · konflikty encji "
            f"{dq['entity_conflicts']} · wykonania z konfliktem {dq['conflicted_executions']} · "
            f"błędy parsowania {dq['source_parse_errors']} · nierozpoznane źródła {dq['unknown_layout_sources']} · "
            f"diagnostyka ledgera {dq['ledger_diagnostics']}")

    def _export_insights_csv(self) -> None:
        if self.analytics is None or not self.analytics.available:
            messagebox.showwarning("Wgląd", "Brak indeksu analitycznego.", parent=self.master); return
        target = filedialog.asksaveasfilename(parent=self.master, defaultextension=".csv",
                                              initialfile="aaw_insights_export.csv",
                                              filetypes=[("CSV", "*.csv")])
        if not target:
            return
        try:
            n = self.analytics.export_csv(Path(target), self._insight_filters())
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Eksport", str(exc), parent=self.master); return
        messagebox.showinfo("Eksport", f"Zapisano {n} wierszy (dane pochodne, nie autorytatywne):\n{target}", parent=self.master)

    def _close(self) -> None:
        save_ui_state(GUI_STATE, {"geometry": self.master.geometry(), "last_page": self.active_page, "recent_repo": self.repo_var.get().strip(), "recent_worktree": self.worktree_var.get().strip()})
        self.master.destroy()

    def _profile_settings_lines(self) -> list[str]:
        lines: list[str] = []
        for profile_id, profile in self.profile_catalog.items():
            resolved = self.get_runtime_profile_state(profile_id)
            lines.append(f"{profile.get('display_name', profile.get('profile_id'))}: {resolved['state']} — {resolved['reason']}")
        return lines or ["No execution profiles found."]

    def _workflow_choices(self) -> list[tuple[str, str]]:
        choices: list[tuple[str, str]] = []
        for path in sorted(WORKFLOWS.glob("*.json")) if WORKFLOWS.is_dir() else ():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            nodes = data.get("nodes", []) if isinstance(data, Mapping) else []
            labels = {"IMPLEMENT": "Implement", "MACHINE_GATE": "Test", "REVIEW": "Review", "REPAIR": "Repair", "HUMAN_GATE": "Accept", "FINAL_GATE": "Finalize"}
            sequence = [labels.get(str(node.get("type")), str(node.get("type", "Step")).title()) for node in nodes if isinstance(node, Mapping)]
            friendly = " → ".join(sequence) or str(data.get("workflow_id") or path.stem)
            choices.append((friendly, str(path)))
        return choices

    def _workflow_selected(self, _event: Any = None) -> None:
        path = self.workflow_display_to_path.get(self.workflow_display_var.get(), "")
        if path:
            self.workflow_var.set(path)
        self._show_workflow_details(inline=True)

    def _show_workflow_details(self, inline: bool = False) -> None:
        try:
            data = json.loads(Path(self.workflow_var.get()).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            details = f"Workflow details unavailable: {exc}"
        else:
            limits = data.get("limits", {}) if isinstance(data, Mapping) else {}
            node_ids = ", ".join(str(node.get("id")) for node in data.get("nodes", []) if isinstance(node, Mapping))
            details = (
                f"ID: {data.get('workflow_id', 'UNKNOWN')}\n"
                f"File: {self.workflow_var.get()}\n"
                f"Nodes: {node_ids or '—'}\n"
                f"Limits: {limits}"
            )
        self.workflow_details_var.set(details)
        if not inline:
            self.workflow_advanced.expanded.set(True)
            self.workflow_advanced._toggle()

    def browse_repository(self) -> None:
        selected = filedialog.askdirectory(parent=self.master, title="Wybierz repository", initialdir=self.repo_var.get() or str(Path.home()))
        if selected:
            self.repo_var.set(selected)
            remember_workspace(repository=selected)
            self.recent_workspaces = load_recent_workspaces()
            discovered = git_worktree_paths(selected)
            if discovered:
                self.recent_workspaces["worktrees"] = list(dict.fromkeys(discovered + self.recent_workspaces["worktrees"]))[:30]
            self._refresh_recent_choices()

    def browse_worktree(self) -> None:
        selected = filedialog.askdirectory(parent=self.master, title="Wybierz worktree", initialdir=self.worktree_var.get() or self.repo_var.get() or str(Path.home()))
        if selected:
            self.worktree_var.set(selected)
            remember_workspace(worktree=selected)
            self.recent_workspaces = load_recent_workspaces(); self._refresh_recent_choices()

    def _refresh_recent_choices(self) -> None:
        self.recent_workspaces = load_recent_workspaces()
        discovered = git_worktree_paths(self.repo_var.get())
        worktrees = tuple(dict.fromkeys(discovered + self.recent_workspaces["worktrees"]))
        for combo in self.recent_repo_combos:
            try: combo.configure(values=tuple(self.recent_workspaces["repositories"]))
            except tk.TclError: pass
        for combo in self.recent_worktree_combos:
            try: combo.configure(values=worktrees)
            except tk.TclError: pass

    def open_folder(self, value: str) -> None:
        path = Path(value.strip()) if value.strip() else None
        if not path or not path.is_dir():
            messagebox.showwarning("Folder unavailable", "Wybierz istniejący folder.", parent=self.master)
            return
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except OSError as exc:
            messagebox.showerror("Cannot open folder", str(exc), parent=self.master)

    def _start_task(self, launch: bool) -> None:
        self.launch_var.set(launch)
        self.dry_var.set(not launch)
        self.run_task()

    def _start_workflow(self, dry_run: bool) -> None:
        self.dry_var.set(dry_run)
        self.launch_var.set(not dry_run)
        self.run_workflow()

    def _show_workflow_monitor(self, data: Mapping[str, Any] | None = None) -> None:
        self.workflow_form.grid_remove()
        self.monitor_frame.grid(row=2, column=0, sticky="ew")
        for child in self.monitor_nodes.winfo_children():
            child.destroy()
        state = data or {}
        completed = {str(item.get("node_id")): item for item in state.get("completed_nodes", []) if isinstance(item, Mapping)}
        current = str(state.get("current_node") or "")
        frozen = state.get("workflow_bindings", {}) if isinstance(state.get("workflow_bindings"), Mapping) else {}
        nodes = self._workflow_llm_nodes(include_all=True)
        for row, node in enumerate(nodes):
            node_id, node_type = str(node.get("id")), str(node.get("type"))
            result = completed.get(node_id, {})
            status = str(result.get("outcome") or ("RUNNING" if node_id == current else "WAITING"))
            binding = frozen.get(node_id, {}) if isinstance(frozen.get(node_id), Mapping) else {}
            profile = binding.get("profile") or result.get("implementer_profile") or "Automatic"
            duration = result.get("duration_s")
            line = ttk.Frame(self.monitor_nodes, style="Card.TFrame", padding=(0, 8))
            line.grid(row=row, column=0, sticky="ew"); line.columnconfigure(1, weight=1)
            ttk.Label(line, text=str(row + 1), style="CardMuted.TLabel", width=3).grid(row=0, column=0, sticky="nw")
            ttk.Label(line, text=self._node_title(node_type), style="CardTitle.TLabel").grid(row=0, column=1, sticky="w")
            ttk.Label(line, text=f"{profile}" + (f" · {duration:.1f}s" if isinstance(duration, (int, float)) else ""), style="CardMuted.TLabel").grid(row=1, column=1, sticky="w")
            ttk.Label(line, text=status, style="Card.TLabel").grid(row=0, column=2, rowspan=2, sticky="e")

    @staticmethod
    def _node_title(node_type: str) -> str:
        return {"IMPLEMENT": "Implementacja", "MACHINE_GATE": "Testy", "REVIEW": "Audyt", "REPAIR": "Naprawa, jeśli potrzebna", "HUMAN_GATE": "Akceptacja", "FINAL_GATE": "Finalizacja"}.get(node_type, node_type.title())

    def _dry_changed(self) -> None:
        if self.dry_var.get():
            self.launch_var.set(False)

    def _mode_changed(self, _event: Any = None) -> None:
        workflow_mode = self.mode_var.get() == "WORKFLOW"
        if workflow_mode:
            text = self.task_text.get("1.0", "end").strip()
            if text and not self.workflow_goal_text.get("1.0", "end").strip():
                self.workflow_goal_text.insert("1.0", text)
            self.show_page("workflow")
        elif self.mode_var.get() in {"MULTI_SUBTASK", "CUSTOM_JOB"}:
            text = self.task_text.get("1.0", "end").strip()
            if text and not self.custom_goal_text.get("1.0", "end").strip():
                self.custom_goal_text.insert("1.0", text)
            self.custom_job_type_var.set("MULTI_SUBTASK" if self.mode_var.get() == "MULTI_SUBTASK" else "SINGLE_IMPLEMENTATION")
            self.show_page("custom")
        else:
            self.show_page("new")

    def get_runtime_profile_state(self, profile_id: str) -> dict[str, Any]:
        return get_runtime_profile_state(profile_id, self.profile_catalog, self.model_runtime)

    def _profile_display(self, profile: Mapping[str, Any]) -> str:
        label = str(profile.get("display_name") or profile.get("profile_id") or "UNKNOWN")
        profile_id = str(profile.get("profile_id") or "")
        state = self.get_runtime_profile_state(profile_id)["state"]
        if state == "UNAVAILABLE":
            return f"{label} — unavailable"
        if state in {"UNKNOWN", "NOT_CHECKED"}:
            return f"{label} — not checked"
        return label

    def _node_binding_profiles(self) -> dict[str, Mapping[str, Any]]:
        """Profiles selectable as an IMPLEMENT/REVIEW/REPAIR node binding.

        Local-runtime presets (Qwen) are deliberately excluded here in V0.1:
        they are bounded-LOW helpers, never implementers. They remain visible
        on Settings -> Models and callable programmatically via the adapter.
        """
        return {pid: p for pid, p in self.profile_catalog.items() if not p.get("local_runtime")}

    def _profile_choices(self) -> tuple[str, ...]:
        return tuple(self._profile_display(profile) for profile in self._node_binding_profiles().values())

    def _profile_detail(self, profile_id: str) -> str:
        profile = self.profile_catalog.get(profile_id, {})
        resolved = self.get_runtime_profile_state(profile_id)
        hint = str(profile.get("ui_hint") or "Automatyczne")
        if resolved["state"] in {"UNAVAILABLE", "UNKNOWN", "NOT_CHECKED"}:
            return f"{hint} · {resolved['reason']}"
        return hint

    def _apply_profile_to_all(self) -> None:
        profile_id = self.binding_display_to_id.get(self.apply_all_profile_var.get(), "")
        profile = self.profile_catalog.get(profile_id, {})
        suitable = {str(value) for value in profile.get("suitable_for", [])}
        for node in self._workflow_llm_nodes():
            node_id, node_type = str(node.get("id")), str(node.get("type"))
            if node_type in suitable and node_id in self.node_binding_vars:
                self.node_binding_vars[node_id].set(self._profile_display(profile))
        self._refresh_binding_details()

    def _refresh_binding_details(self) -> None:
        for node_id, variable in self.node_binding_vars.items():
            profile_id = self.binding_display_to_id.get(variable.get(), "")
            detail = self.binding_detail_vars.get(node_id)
            if detail is not None:
                detail.set(self._profile_detail(profile_id))
        self._refresh_start_state()
        self._refresh_system_status()

    def _refresh_start_state(self) -> None:
        if not hasattr(self, "workflow_button"):
            return
        selected_ids = [self.binding_display_to_id.get(var.get(), "") for var in self.node_binding_vars.values()]
        blocked = any(profile_id and self.get_runtime_profile_state(profile_id)["state"] == "UNAVAILABLE" for profile_id in selected_ids)
        self.workflow_button.configure(state="disabled" if blocked else "normal")

    def _refresh_runtime_dependent_views(self) -> None:
        old_map = dict(self.binding_display_to_id)
        remembered: list[tuple[ttk.Combobox, tk.StringVar, str]] = []
        for combo, variable in self.profile_comboboxes:
            profile_id = old_map.get(variable.get(), "")
            if not profile_id:
                profile_id = next((pid for pid, profile in self.profile_catalog.items() if variable.get() in {pid, str(profile.get('display_name') or '')}), "")
            remembered.append((combo, variable, profile_id))
        self._refresh_binding_rows()
        values = self._profile_choices()
        for combo, variable, profile_id in remembered:
            try:
                if not combo.winfo_exists():
                    continue
                combo.configure(values=values)
                if profile_id in self.profile_catalog:
                    variable.set(self._profile_display(self.profile_catalog[profile_id]))
                elif not variable.get() and values:
                    variable.set(values[0])
            except tk.TclError:
                continue
        self.profile_status_var.set("\n".join(self._profile_settings_lines()))
        self._populate_queue_tasks()
        self._refresh_binding_details()
        self._refresh_system_status()

    def _refresh_system_status(self) -> None:
        if self.model_runtime is None:
            self.system_status_var.set("Preflight required")
            return
        core = (self.preflight_result.get("launcher", "OK"), self.preflight_result.get("playbook_root", "OK"), self.preflight_result.get("model_registry", "OK"))
        core_warnings = sum(value != "OK" for value in core)
        selected_ids = [self.binding_display_to_id.get(var.get(), "") for var in self.node_binding_vars.values()]
        selected_warnings = sum(self.get_runtime_profile_state(profile_id)["state"] == "UNAVAILABLE" for profile_id in selected_ids if profile_id)
        if self.execution_mode_var.get() == "orca" and self.preflight_result.get("orca_runtime") not in {None, "READY"}:
            core_warnings += 1
        warning_count = core_warnings + selected_warnings
        self.system_status_var.set("System ready" if warning_count == 0 else f"{warning_count} warnings")

    def _workflow_llm_nodes(self, include_all: bool = False) -> list[Mapping[str, Any]]:
        try:
            data = json.loads(Path(self.workflow_var.get()).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        nodes = data.get("nodes") if isinstance(data, Mapping) else None
        allowed = {"IMPLEMENT", "REVIEW", "REPAIR"}
        return [node for node in nodes or () if isinstance(node, Mapping) and (include_all or node.get("type") in allowed)]

    def _refresh_binding_rows(self) -> None:
        if not hasattr(self, "bindings_box"):
            return
        previous_map = dict(self.binding_display_to_id)
        previous = {node_id: previous_map.get(variable.get(), self.binding_default_profiles.get(node_id, "")) for node_id, variable in self.node_binding_vars.items()}
        for child in self.bindings_box.winfo_children():
            child.destroy()
        self.node_binding_vars = {}
        self.binding_default_profiles = {}
        self.binding_detail_vars: dict[str, tk.StringVar] = {}
        self.binding_display_to_id = {self._profile_display(profile): profile_id for profile_id, profile in self._node_binding_profiles().items()}
        values = tuple(self.binding_display_to_id)
        nodes = self._workflow_llm_nodes()
        if not nodes:
            ttk.Label(self.bindings_box, text="Wybierz workflow zawierający node'y LLM.", style="CardMuted.TLabel").grid(row=0, column=0, sticky="w")
            return
        defaults = {"IMPLEMENT": "TERRA_HIGH", "REVIEW": "SOL_HIGH", "REPAIR": "SOL_MEDIUM"}
        for row, node in enumerate(nodes):
            node_id, node_type = str(node["id"]), str(node["type"])
            default_id = previous.get(node_id) or defaults.get(node_type, "SOL_MEDIUM")
            default_display = next((display for display, profile_id in self.binding_display_to_id.items() if profile_id == default_id), values[0] if values else "")
            self.binding_default_profiles[node_id] = default_id
            variable = tk.StringVar(value=default_display)
            self.node_binding_vars[node_id] = variable
            card = ttk.Frame(self.bindings_box, style="Card.TFrame", padding=(0, 9))
            card.grid(row=row, column=0, sticky="ew"); card.columnconfigure(1, weight=1)
            ttk.Label(card, text=str(row + 1), style="CardMuted.TLabel", width=3).grid(row=0, column=0, rowspan=2, sticky="nw")
            ttk.Label(card, text=self._node_title(node_type), style="CardTitle.TLabel").grid(row=0, column=1, sticky="w")
            profile = self.profile_catalog.get(default_id, {})
            detail_var = tk.StringVar(value=self._profile_detail(default_id))
            self.binding_detail_vars[node_id] = detail_var
            ttk.Label(card, textvariable=detail_var, style="CardMuted.TLabel", wraplength=520).grid(row=1, column=1, sticky="w")
            if node_type in {"IMPLEMENT", "REVIEW", "REPAIR"}:
                combo = ttk.Combobox(card, textvariable=variable, values=values, state="readonly", width=27)
                combo.grid(row=0, column=2, rowspan=2, sticky="e")
                combo.bind("<<ComboboxSelected>>", lambda _event: self._refresh_binding_details())
            else:
                ttk.Label(card, text="Automatyczne", style="CardMuted.TLabel").grid(row=0, column=2, rowspan=2, sticky="e")

    def _gui_bindings(self) -> list[str]:
        selected: list[str] = []
        node_types = {str(node.get("id")): str(node.get("type")) for node in self._workflow_llm_nodes()}
        for node_id, variable in self.node_binding_vars.items():
            profile_id = self.binding_display_to_id.get(variable.get(), "")
            profile = self.profile_catalog.get(profile_id, {})
            if not profile_id:
                raise ValueError(f"No valid profile is selected for {node_id}.")
            if profile.get("not_implementer") or node_types.get(node_id) not in set(profile.get("suitable_for", [])):
                raise ValueError(f"{profile_id} is not eligible for {node_types.get(node_id, 'this node')}.")
            resolved = self.get_runtime_profile_state(profile_id)
            if resolved["state"] not in {"VERIFIED", "AVAILABLE"}:
                raise ValueError(f"{node_id}: {profile.get('display_name', profile_id)} is {resolved['state']}: {resolved['reason']}")
            selected.append(f"{node_id}={profile_id}")
        return selected

    def _launch_changed(self) -> None:
        if self.launch_var.get():
            self.dry_var.set(False)
        elif not self.dry_var.get():
            self.dry_var.set(True)

    def _set_text(self, widget: tk.Text, value: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", value)
        widget.configure(state="disabled")

    def run_preflight(self) -> None:
        self.preflight_button.configure(state="disabled")
        self.version_warning_var.set("Checking…")
        threading.Thread(
            target=lambda: self.events.put(("preflight_complete", run_preflight_checks())),
            daemon=True,
        ).start()

    def _show_preflight(self, result: Mapping[str, Any]) -> None:
        self.preflight_button.configure(state="normal")
        self.preflight_result = dict(result)
        runtime = result.get("model_runtime")
        if isinstance(runtime, Mapping):
            self.model_runtime = dict(runtime)
            write_json_atomic(MODEL_RUNTIME_STATE, self.model_runtime)
            self._refresh_runtime_dependent_views()
        for item in self.preflight.get_children():
            self.preflight.delete(item)
        rows = (
            ("Recommended execution", "DIRECT CLI"),
            ("Launcher", result.get("launcher")),
            ("Playbook root", result.get("playbook_root")),
            ("Stats root", result.get("stats_root")),
            ("Routing root", result.get("routing_root")),
            ("MODEL_REGISTRY", result.get("model_registry")),
            ("Python", result.get("python_version")),
            ("Codex CLI", result.get("codex_version")),
            ("Codex executable", result.get("codex_executable")),
            ("Runtime snapshot", str(MODEL_RUNTIME_STATE)),
            ("Snapshot timestamp", self.model_runtime.get("checked_at") if isinstance(self.model_runtime, Mapping) else "NOT CHECKED"),
            ("TERRA_HIGH", self.get_runtime_profile_state("TERRA_HIGH")["state"]),
            ("SOL_MEDIUM", self.get_runtime_profile_state("SOL_MEDIUM")["state"]),
            ("SOL_HIGH", self.get_runtime_profile_state("SOL_HIGH")["state"]),
            ("SONNET_HIGH", self.get_runtime_profile_state("SONNET_HIGH")["state"]),
            ("Cheap classifier", result.get("cheap_classifier")),
            ("ORCA", result.get("orca_version")),
            ("ORCA runtime", result.get("orca_runtime")),
            ("ORCA orchestration", result.get("orca_orchestration")),
            ("ORCA policy", "EXPERIMENTAL / RUNTIME INCONSISTENT"),
            ("Model Registry version", result.get("registry_version")),
            ("Launcher version", result.get("launcher_version")),
            ("Playbook version", result.get("playbook_version")),
            ("Codex detected by ORCA", "PASS" if result.get("codex_detected") else "FAIL"),
            ("Claude detected by ORCA", "PASS" if result.get("claude_detected") else "FAIL"),
            ("Supervised dispatch", nested_find(result.get("smoke", {}), ("supervised_dispatch",)) or ("AVAILABLE" if result.get("worker_start") else "UNAVAILABLE")),
            ("Fresh worker", nested_find(result.get("smoke", {}), ("fresh_worker",)) or "NOT TESTED"),
            ("worker_done", nested_find(result.get("smoke", {}), ("worker_done",)) or "NOT TESTED"),
            ("Model binding", nested_find(result.get("smoke", {}), ("model_binding",)) or ("SUPPORTED (syntax only)" if result.get("worker_start_model") else "PARTIAL")),
            ("Effort binding", nested_find(result.get("smoke", {}), ("effort_binding",)) or ("SUPPORTED (syntax only)" if result.get("worker_start_effort") else "PARTIAL")),
            ("Nested worker depth", result.get("nested_worker_depth")),
            ("Last smoke-tested ORCA", result.get("last_smoke_tested_orca_version")),
            ("Last smoke verdict", result.get("last_smoke_verdict")),
            ("Last smoke timestamp", result.get("last_smoke_timestamp")),
            ("Smoke test", result.get("smoke_test")),
            ("Last checked", result.get("checked_at")),
        )
        for label, value in rows:
            self.preflight.insert("", "end", text=label, values=(value if value not in (None, "") else "UNKNOWN",))
        self.version_warning_var.set(
            "ORCA VERSION CHANGED — PREFLIGHT / SMOKE TEST RECOMMENDED"
            if result.get("version_changed") else ""
        )
        self._refresh_system_status()

    def _pipeline_selected(self) -> None:
        enabled = bool(self.human_required_run_id and self.pipeline_var.get() in PIPELINE_LABELS)
        if hasattr(self, "continue_button"):
            self.continue_button.configure(state="normal" if enabled else "disabled")

    def _show_human_required(self, run_id: str) -> None:
        candidate = classifier_candidate(self.groups.get(run_id, []))
        self.human_required_run_id = run_id
        self.pipeline_var.set("")
        dialog = tk.Toplevel(self.master)
        dialog.title("Potrzebna decyzja")
        dialog.transient(self.master); dialog.grab_set(); dialog.geometry(self._center_dialog(650, 430)); dialog.minsize(560, 380)
        frame = ttk.Frame(dialog, padding=22)
        frame.grid(sticky="nsew"); frame.columnconfigure(0, weight=1)
        dialog.rowconfigure(0, weight=1); dialog.columnconfigure(0, weight=1)
        ttk.Label(frame, text="Potrzebna decyzja", font=("Segoe UI Semibold", 16)).grid(row=0, column=0, sticky="w")
        ttk.Label(frame, text="AAW nie rozstrzygnął jednoznacznie rodzaju zadania.", wraplength=580).grid(row=1, column=0, sticky="w", pady=(6, 14))
        if not candidate or not isinstance(candidate.data, Mapping):
            self.resolver_status_var.set(f"{run_id}: HUMAN_REQUIRED; classifier candidate NOT RECORDED.")
            ttk.Label(frame, text="Classifier candidate could not be located for this run.", foreground="#9b1c1c").grid(row=2, column=0, sticky="w")
        else:
            ttk.Label(frame, text=f"Suggested: {candidate.data.get('pipeline', 'UNKNOWN')}\nConfidence: {candidate.data.get('confidence', 'UNKNOWN')}", justify="left").grid(row=2, column=0, sticky="w")
            self.resolver_status_var.set(f"{run_id}: explicit human pipeline selection required. Candidate: {candidate.path}")
        options = ttk.Frame(frame); options.grid(row=3, column=0, sticky="ew", pady=(15, 10))
        for index, (pipeline, label) in enumerate(PIPELINE_LABELS.items()):
            ttk.Radiobutton(options, text=label.title(), value=pipeline, variable=self.pipeline_var, command=self._pipeline_selected).grid(row=index, column=0, sticky="w", pady=2)
        actions = ttk.Frame(frame); actions.grid(row=4, column=0, sticky="e", pady=(12, 0))
        ttk.Button(actions, text="Anuluj", command=dialog.destroy).grid(row=0, column=0, padx=(0, 8))
        self.continue_button = ttk.Button(actions, text="KONTYNUUJ", command=lambda: (dialog.destroy(), self.continue_with_pipeline()), state="disabled", style="Primary.TButton")
        self.continue_button.grid(row=0, column=1)

    def _center_dialog(self, width: int, height: int) -> str:
        self.master.update_idletasks()
        sw, sh = self.master.winfo_screenwidth(), self.master.winfo_screenheight()
        width, height = min(width, sw - 40), min(height, sh - 80)
        return f"{width}x{height}+{max(0, (sw-width)//2)}+{max(0, (sh-height)//2)}"

    def _show_human_gate(self, result: Mapping[str, Any]) -> None:
        dialog = tk.Toplevel(self.master)
        dialog.title("Workflow zakończył pracę")
        dialog.transient(self.master); dialog.grab_set(); dialog.geometry(self._center_dialog(680, 440)); dialog.minsize(580, 390)
        frame = ttk.Frame(dialog, padding=22); frame.grid(sticky="nsew"); frame.columnconfigure(0, weight=1)
        dialog.rowconfigure(0, weight=1); dialog.columnconfigure(0, weight=1)
        ttk.Label(frame, text="Workflow zakończył pracę", font=("Segoe UI Semibold", 16)).grid(row=0, column=0, sticky="w")
        ttk.Label(frame, text="Candidate ready for review", style="Status.TLabel").grid(row=1, column=0, sticky="w", pady=(8, 16))
        bindings = result.get("workflow_bindings", {}) if isinstance(result.get("workflow_bindings"), Mapping) else {}
        completed = result.get("completed_nodes", []) if isinstance(result.get("completed_nodes"), list) else []
        summary = result.get("workflow_summary", {}) if isinstance(result.get("workflow_summary"), Mapping) else {}
        profiles = {str(value.get("role")): value.get("profile") for value in bindings.values() if isinstance(value, Mapping)}
        profile_labels = {
            role: self._profile_display(self.profile_catalog.get(str(profile_id), {"profile_id": profile_id}))
            for role, profile_id in profiles.items()
        }
        tests = next((item.get("summary") or item.get("outcome") for item in completed if isinstance(item, Mapping) and item.get("node_type") == "MACHINE_GATE"), "Not recorded")
        ttk.Label(frame, text=(
            f"Implementation: {profile_labels.get('IMPLEMENT', 'Not recorded')}\n"
            f"Review: {profile_labels.get('REVIEW', 'Not recorded')}\n"
            f"Tests: {tests}\n"
            f"Repairs: {summary.get('repair_cycles', summary.get('repair_count', '—'))}\n"
            f"Duration: {summary.get('wall_time_total', '—')} s"
        ), justify="left", wraplength=600).grid(row=2, column=0, sticky="w")
        ttk.Label(frame, text="ACCEPT oznacza gotowość kandydata do zewnętrznej integracji. Nie wykonuje merge.", foreground="#657080", wraplength=600).grid(row=3, column=0, sticky="w", pady=(14, 18))
        actions = ttk.Frame(frame); actions.grid(row=4, column=0, sticky="ew")
        ttk.Button(actions, text="Otwórz zmiany", command=lambda: self.open_folder(self.worktree_var.get())).grid(row=0, column=0)
        ttk.Button(actions, text="ACCEPT CANDIDATE", command=lambda: (dialog.destroy(), self.workflow_verdict("accept")), style="Primary.TButton").grid(row=0, column=1, padx=8)
        ttk.Button(actions, text="REJECT", command=lambda: (dialog.destroy(), self.workflow_verdict("reject")), style="Danger.TButton").grid(row=0, column=2)
        ttk.Button(actions, text="LEAVE FOR LATER", command=dialog.destroy).grid(row=0, column=3, padx=(8, 0))

    def continue_with_pipeline(self) -> None:
        pipeline = self.pipeline_var.get()
        if not self.human_required_run_id or pipeline not in PIPELINE_LABELS:
            return
        if hasattr(self, "continue_button"):
            self.continue_button.configure(state="disabled")
        self.run_button.configure(state="disabled")
        self.status_var.set("RUNNING")
        self.exit_var.set("Exit code: —")
        self.resolver_status_var.set(
            f"Continuing {self.human_required_run_id} task with explicit {pipeline}; original candidate remains unchanged."
        )
        self._set_text(self.output, f"Rerunning same TASK with --pipeline {pipeline}…")
        owner = self.queue_run_owners.get(self.human_required_run_id)
        if owner:
            queue_id, task_id = owner
            self._mark_queue_resume_running(owner)
            argv = build_launcher_argv(self.current_task, True, pipeline, "direct")
            threading.Thread(target=self._run_queue_process, args=(queue_id, task_id, "SINGLE_TASK", argv), daemon=True).start()
        else:
            threading.Thread(target=self._run_worker, args=(self.current_task, self.last_launch, pipeline, self.last_execution_mode), daemon=True).start()

    def create_worktree(self) -> None:
        repo_text = self.repo_var.get().strip()
        if not repo_text:
            self.browse_repository()
            repo_text = self.repo_var.get().strip()
        repo=Path(repo_text)
        if not repo.is_dir(): messagebox.showwarning("Cannot create","Repository must exist.",parent=self.master); return
        dialog=tk.Toplevel(self.master); dialog.title("Utwórz izolowany worktree"); dialog.transient(self.master); dialog.grab_set(); dialog.geometry(self._center_dialog(680,300))
        frame=ttk.Frame(dialog,padding=18); frame.grid(sticky="nsew"); frame.columnconfigure(1,weight=1)
        name_var=tk.StringVar(value=f"aaw-{dt.datetime.now():%Y%m%d-%H%M%S}"); base_var=tk.StringVar(value="HEAD"); dest_var=tk.StringVar(value=str(repo.parent/"AAW_WORKTREES"/name_var.get()))
        ttk.Label(frame,text="Name").grid(row=0,column=0,sticky="w",pady=5); ttk.Entry(frame,textvariable=name_var).grid(row=0,column=1,columnspan=2,sticky="ew",pady=5)
        ttk.Label(frame,text="Base").grid(row=1,column=0,sticky="w",pady=5); ttk.Entry(frame,textvariable=base_var).grid(row=1,column=1,columnspan=2,sticky="ew",pady=5)
        ttk.Label(frame,text="Destination").grid(row=2,column=0,sticky="w",pady=5); ttk.Entry(frame,textvariable=dest_var).grid(row=2,column=1,sticky="ew",pady=5)
        def choose_destination() -> None:
            selected=filedialog.askdirectory(parent=dialog,title="Wybierz folder nadrzędny",initialdir=str(repo.parent))
            if selected: dest_var.set(str(Path(selected)/name_var.get().strip()))
        ttk.Button(frame,text="Wybierz…",command=choose_destination).grid(row=2,column=2,padx=(6,0))
        ttk.Label(frame,text="Runner utworzy nową gałąź codex/<name>. Nie wykona merge ani push.",wraplength=580).grid(row=3,column=0,columnspan=3,sticky="w",pady=(10,12))
        def create() -> None:
            safe=re.sub(r"[^a-zA-Z0-9._-]+","-",name_var.get().strip()).strip("-")
            worktree=Path(dest_var.get().strip()); branch=f"codex/{safe}"
            if not safe or worktree.exists(): messagebox.showwarning("Cannot create","Name is required and destination must not exist.",parent=dialog); return
            rc,out,err=run_process(["git","-C",str(repo),"worktree","add","-b",branch,str(worktree),base_var.get().strip() or "HEAD"],timeout=60)
            self._set_text(self.output,f"CREATE ISOLATED WORKTREE (shell=False)\nSTDOUT:\n{out}\nSTDERR:\n{err}"); self.status_var.set("READY" if rc==0 else "BLOCKED")
            if rc==0: self.worktree_var.set(str(worktree)); remember_workspace(repo_text,str(worktree)); self._refresh_recent_choices(); dialog.destroy()
        ttk.Button(frame,text="Utwórz",command=create,style="Primary.TButton").grid(row=4,column=2,sticky="e")

    def run_workflow(self) -> None:
        try:
            goal = validate_task(self.workflow_goal_text.get("1.0", "end"))
        except ValueError as exc:
            messagebox.showwarning("Goal required", str(exc), parent=self.master)
            return
        workflow, repo, worktree = self.workflow_var.get().strip(), self.repo_var.get().strip(), self.worktree_var.get().strip()
        if not all((workflow, repo, worktree)):
            messagebox.showwarning("Workflow paths required", "Workflow definition, Repository, and Worktree are required.", parent=self.master)
            return
        if not WORKFLOW_RUNNER.is_file():
            messagebox.showerror("Workflow runner missing", str(WORKFLOW_RUNNER), parent=self.master)
            return
        try:
            bindings = self._gui_bindings()
        except ValueError as exc:
            self.status_var.set("BLOCKED")
            title = "Preflight required" if "NOT_CHECKED" in str(exc) or "UNKNOWN" in str(exc) else "Unavailable execution profile"
            messagebox.showwarning(title, str(exc), parent=self.master)
            return
        argv = workflow_argv(workflow, goal, repo, worktree, self.dry_var.get(), bindings, self.preprocess_policy_var.get())
        self.frozen_bindings = {item.partition("=")[0]: item.partition("=")[2] for item in bindings}
        self.current_task = goal
        self.monitor_title_var.set(goal.splitlines()[0][:72])
        self._show_workflow_monitor({"status": "RUNNING", "current_node": "N01", "workflow_bindings": {node_id: {"profile": profile_id} for node_id, profile_id in self.frozen_bindings.items()}})
        self.workflow_button.configure(state="disabled")
        self.run_button.configure(state="disabled")
        self.status_var.set("RUNNING")
        self.exit_var.set("Exit code: —")
        self._set_text(self.output, "Running static workflow…")
        threading.Thread(target=lambda: self.events.put(("workflow_complete", run_workflow_process(argv))), daemon=True).start()

    def _on_workflow_complete(self, returncode: int, stdout: str, stderr: str, argv: list[str]) -> None:
        self._refresh_start_state()
        self.run_button.configure(state="normal")
        result = parse_json_flex(stdout)
        status = str(result.get("status") or ("BLOCKED" if returncode else "UNKNOWN"))
        identifier = str(result.get("AAW_RUN_ID") or result.get("run_id") or "")
        self.current_result = result
        self.current_run_id = identifier
        self.workflow_human_run_id = identifier if status == "WAITING_FOR_HUMAN" else ""
        self.status_var.set(status)
        self.current_run_header_var.set(f"Current run: {identifier}" if identifier else "No active run")
        self.exit_var.set(f"Exit code: {returncode}")
        self._set_text(self.output, f"COMMAND (shell=False):\n{subprocess.list2cmdline(argv)}\n\nSTDOUT:\n{stdout or '(empty)'}\n\nSTDERR:\n{stderr or '(empty)'}")
        self.refresh_artifacts(select_run=identifier)
        self._show_current_summary(status)
        if identifier:
            self._set_text(self.timeline, workflow_timeline(result))
        self._show_workflow_monitor(result)
        if status == "WAITING_FOR_HUMAN":
            self.resolver_status_var.set(f"{identifier}: candidate awaits explicit verdict. ACCEPT does not merge.")
            self._show_human_gate(result)

    def workflow_verdict(self, verdict: str) -> None:
        if not self.workflow_human_run_id:
            return
        argv = [sys.executable, str(WORKFLOW_RUNNER), "--run-id", self.workflow_human_run_id, "--human-verdict", verdict]
        self.workflow_button.configure(state="disabled")
        owner = self.queue_run_owners.get(self.workflow_human_run_id)
        if owner:
            self._mark_queue_resume_running(owner)
            threading.Thread(target=self._run_queue_process, args=(owner[0], owner[1], "WORKFLOW", argv), daemon=True).start()
        else:
            threading.Thread(target=lambda: self.events.put(("workflow_complete", run_workflow_process(argv))), daemon=True).start()

    def run_task(self) -> None:
        try:
            task = validate_task(self.task_text.get("1.0", "end"))
        except ValueError as exc:
            self.status_var.set("BLOCKED")
            messagebox.showwarning("TASK required", str(exc), parent=self.master)
            return
        if not LAUNCHER.is_file():
            self.status_var.set("BLOCKED")
            messagebox.showerror("Launcher missing", str(LAUNCHER), parent=self.master)
            return
        launch = self.launch_var.get()
        execution_mode = self.execution_mode_var.get()
        if launch and execution_mode == "orca":
            proceed = confirm_orca_launch(self.master, bool(self.preflight_result.get("version_changed")))
            if not proceed:
                self.status_var.set("READY")
                return
        self.current_task = task
        self.last_launch = launch
        self.last_execution_mode = execution_mode
        self.human_required_run_id = ""
        self.pipeline_var.set("")
        if hasattr(self, "continue_button"):
            self.continue_button.configure(state="disabled")
        self.resolver_status_var.set("No HUMAN_REQUIRED decision pending.")
        self.run_button.configure(state="disabled")
        self.status_var.set("RUNNING")
        self.exit_var.set("Exit code: —")
        self._set_text(self.output, "Running launcher…")
        working_directory=self.worktree_var.get().strip() or self.repo_var.get().strip()
        if not working_directory:
            messagebox.showwarning("Workspace required","Wybierz Repository lub Worktree.",parent=self.master); self.run_button.configure(state="normal"); self.status_var.set("BLOCKED"); return
        remember_workspace(self.repo_var.get(),self.worktree_var.get()); self._refresh_recent_choices()
        threading.Thread(target=self._run_worker, args=(task, launch, "", execution_mode, working_directory), daemon=True).start()

    def _run_worker(self, task: str, launch: bool, pipeline: str = "", execution_mode: str = "direct", working_directory: str = "") -> None:
        self.events.put(("run_complete", run_launcher(task, launch, pipeline, execution_mode, working_directory)))

    def _drain_events(self) -> None:
        try:
            while True:
                event, payload = self.events.get_nowait()
                if event == "run_complete":
                    self._on_run_complete(*payload)
                elif event == "workflow_complete":
                    self._on_workflow_complete(*payload)
                elif event == "custom_complete":
                    self._on_custom_complete(payload)
                elif event == "preflight_complete":
                    self._show_preflight(payload)
                elif event == "queue_process_started":
                    self._queue_process_started(*payload)
                elif event == "queue_task_complete":
                    self._on_queue_task_complete(*payload)
                elif event == "insights_built":
                    if isinstance(payload, Mapping) and payload.get("error"):
                        self.insight_meta_var.set(f"Błąd indeksu: {payload['error']}")
                    else:
                        self.analytics = aaw_analytics.Analytics(ANALYTICS_DB) if aaw_analytics is not None else None
                        self._render_insights()
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    def _on_run_complete(self, returncode: int, stdout: str, stderr: str, argv: list[str]) -> None:
        self.run_button.configure(state="normal")
        result = parse_launcher_result(stdout)
        status = status_for(returncode, result)
        self.status_var.set(status)
        self.exit_var.set(f"Exit code: {returncode}")
        command = subprocess.list2cmdline(argv)
        self._set_text(self.output, f"COMMAND (shell=False):\n{command}\n\nSTDOUT:\n{stdout or '(empty)'}\n\nSTDERR:\n{stderr or '(empty)'}")
        self.current_result = result
        prior_human_run = self.human_required_run_id
        self.current_run_id = str(result.get("run_id") or "")
        if not self.current_run_id:
            match = re.search(r"run_id=(AAW_\S+)", stderr)
            if match:
                self.current_run_id = match.group(1)
        self.current_run_header_var.set(f"Current run: {self.current_run_id}" if self.current_run_id else "No active run")
        self.refresh_artifacts(select_run=self.current_run_id)
        self._show_current_summary(status)
        if status == "HUMAN_REQUIRED":
            self._show_human_required(self.current_run_id)
        elif prior_human_run:
            self.resolver_status_var.set(
                f"Resolved: {prior_human_run} → {self.current_run_id or 'UNKNOWN'} using explicit {nested_find(result, ('pipeline',)) or self.pipeline_var.get() or 'UNKNOWN'}."
            )
            self.human_required_run_id = ""

    def refresh_artifacts(self, select_run: str = "") -> None:
        self.artifacts, warnings = scan_artifacts()
        self.groups = group_artifacts(self.artifacts)
        self.warning_var.set(" | ".join(warnings))
        self._populate_runs(select_run or self._selected_run())

    def _selected_run(self) -> str:
        selected = self.runs.curselection() if hasattr(self, "runs") else ()
        return self.visible_runs[selected[0]] if selected and selected[0] < len(self.visible_runs) else ""

    def _populate_runs(self, select_run: str = "") -> None:
        needle = self.filter_var.get().strip().lower()
        ordered = sorted(self.groups, key=lambda key: max(a.mtime for a in self.groups[key]), reverse=True)
        self.visible_runs = [
            key for key in ordered
            if not needle or needle in key.lower() or any(needle in a.task_slug.lower() or needle in a.path.name.lower() for a in self.groups[key])
        ]
        self.runs.delete(0, "end")
        for key in self.visible_runs:
            slugs = next((a.task_slug for a in self.groups[key] if a.task_slug), "")
            label = key if not slugs else f"{key}  |  {slugs}"
            self.runs.insert("end", label)
        if select_run in self.visible_runs:
            index = self.visible_runs.index(select_run)
            self.runs.selection_set(index)
            self.runs.see(index)
            self._load_run_files(select_run)
        elif self.visible_runs:
            self.runs.selection_set(0)
            self._load_run_files(self.visible_runs[0])
        else:
            self.files.delete(0, "end")
            self.visible_files = []

    def _run_selected(self, _event: Any = None) -> None:
        run_id = self._selected_run()
        if run_id:
            self._load_run_files(run_id)

    def _load_run_files(self, run_id: str) -> None:
        self.visible_files = sorted(self.groups.get(run_id, []), key=lambda item: item.mtime, reverse=True)
        self.files.delete(0, "end")
        for artifact in self.visible_files:
            marker = " [invalid]" if artifact.error else ""
            self.files.insert("end", f"[{artifact.source}] {artifact.path.name}{marker}")
        if self.visible_files:
            self.files.selection_set(0)
            self._show_artifact(self.visible_files[0])
        workflow_state = next((a.data for a in self.visible_files if a.path.name == "workflow_state.json" and isinstance(a.data, Mapping)), None)
        selected_status = self.status_var.get() if run_id == self.current_run_id else "NOT RECORDED"
        self._set_text(self.timeline, workflow_timeline(workflow_state) if workflow_state else build_timeline(run_id, self.visible_files, selected_status))

    def _file_selected(self, _event: Any = None) -> None:
        selected = self.files.curselection()
        if selected and selected[0] < len(self.visible_files):
            self._show_artifact(self.visible_files[selected[0]])

    def _show_artifact(self, artifact: Artifact) -> None:
        self._set_text(self.viewer, read_for_viewer(artifact))

    def _show_current_summary(self, status: str) -> None:
        for item in self.summary.get_children():
            self.summary.delete(item)
        run_artifacts = self.groups.get(self.current_run_id, [])
        merged: dict[str, Any] = dict(self.current_result)
        paths: dict[str, str] = {}
        for key, value in artifacts_merged(run_artifacts).items():
            merged.setdefault(key, value)
        for artifact in run_artifacts:
            name = artifact.path.name.lower()
            if "zdefiniowanie_problemu" in name:
                paths["routing artifact path"] = str(artifact.path)
            elif "capability_binding" in name:
                paths["capability binding path"] = str(artifact.path)
            elif "model_binding" in name:
                paths["model binding path"] = str(artifact.path)
            elif "execution_receipt" in name or "orca_launch_receipt" in name:
                paths["launch receipt path"] = str(artifact.path)
        stats_files = [a for a in run_artifacts if a.source == "Stats"]
        rows = [
            ("Status", status),
            ("AAW_RUN_ID", self.current_run_id or "—"),
            ("Pipeline", nested_find(merged, ("pipeline",)) or "—"),
            ("Routing source", nested_find(merged, ("routing_source",)) or "—"),
            ("Capability", nested_find(merged, ("coordinator_class",)) or "—"),
            ("Role", nested_find(merged, ("role",)) or "—"),
            ("Harness", nested_find(merged, ("harness",)) or "—"),
            ("Model", nested_find(merged, ("runtime_model_id", "model_family", "model")) or "—"),
            ("Effort", nested_find(merged, ("effort", "abstract_effort")) or "—"),
            ("Interpretation", deterministic_interpretation(merged, status) or "—"),
        ]
        rows.extend(paths.items())
        rows.append(("stats path", str(stats_files[0].path.parent) if stats_files else str(self.current_result.get("stats_root") or "—")))
        for label, value in rows:
            self.summary.insert("", "end", text=label, values=(value,))
        self._set_text(self.timeline, build_timeline(self.current_run_id, run_artifacts, status))

    def _poll(self) -> None:
        self._recover_queue_processes()
        if self.auto_var.get():
            self.refresh_artifacts()
        self.after(5000, self._poll)


def self_test() -> int:
    failures: list[str] = []
    total = 0

    def check(condition: bool, name: str) -> None:
        nonlocal total
        total += 1
        if not condition:
            failures.append(name)

    try:
        validate_task(" \n ")
        check(False, "empty TASK rejected")
    except ValueError:
        check(True, "empty TASK rejected")
    multiline = "line one\nline two"
    dry_argv = build_launcher_argv(multiline, False)
    launch_argv = build_launcher_argv(multiline, True)
    orca_argv = build_launcher_argv(multiline, True, execution_mode="orca")
    resolved_argv = build_launcher_argv(multiline, False, "P05")
    check("--launch" not in dry_argv, "dry run never launches a worker")
    check("--launch" in launch_argv, "launch flag forwarded")
    check(launch_argv[-2:] == ["--execution-mode", "direct"], "real launch defaults to direct")
    check(orca_argv[-2:] == ["--execution-mode", "orca"], "ORCA requires explicit selection")
    check(resolved_argv[-2:] == ["--pipeline", "P05"], "manual pipeline forwarded")
    check(multiline in dry_argv and dry_argv[dry_argv.index("--task") + 1] == multiline, "multiline TASK is one argv item")
    check(status_for(20, {}) == "HUMAN_REQUIRED", "HUMAN_REQUIRED mapping")
    check(deterministic_interpretation({"pipeline": "P05", "coordinator_class": "C0"}) == "P05 — DEBUG / REPAIR | C0 — Direct execution — no separate coordinator", "deterministic interpretation")
    wf_argv = workflow_argv("flow.json", multiline, "D:\\repo", "D:\\worktree", True)
    check("--dry-run" in wf_argv and wf_argv[wf_argv.index("--goal") + 1] == multiline, "workflow goal remains one argv item")
    check("WAITING_FOR_HUMAN" in workflow_timeline({"workflow_id": "WF", "status": "WAITING_FOR_HUMAN", "completed_nodes": []}), "workflow timeline status")
    timeline = workflow_timeline({
        "workflow_id": "WF", "status": "WAITING_FOR_HUMAN",
        "completed_nodes": [{"node_id": "N01", "node_type": "IMPLEMENT", "outcome": "PASS", "implementer_profile": "TERRA_HIGH", "effort": "high", "duration_s": 1.2}],
        "workflow_bindings": {"N01": {"role": "IMPLEMENT", "profile": "TERRA_HIGH", "effort": "high"}},
        "workflow_summary": {"llm_calls": 1, "input_tokens_total": 2, "output_tokens_total": 3, "wall_time_total": 4.0},
    })
    check("TERRA_HIGH" in timeline and "HUMAN GATE SUMMARY" in timeline, "timeline shows frozen profile and human summary")

    with tempfile.TemporaryDirectory(prefix="aaw_cc_test_") as temp:
        root = Path(temp)
        valid = root / "20260902_120000__repair-csv__abcdef12__capability_binding.json"
        invalid = root / "broken.json"
        text_file = root / "note.txt"
        valid.write_text(json.dumps({"run_id": "AAW_TEST", "coordinator_class": "C0", "role": "CODE_IMPLEMENTER"}), encoding="utf-8")
        invalid.write_text("{not json", encoding="utf-8")
        text_file.write_text("AAW_RUN_ID=AAW_TEST\nhello", encoding="utf-8")
        found, warnings = scan_artifacts((root, root / "missing"))
        check(len(found) == 3, "refresh finds files")
        check(any(a.path == invalid and a.error for a in found), "invalid JSON is contained")
        check(bool(warnings), "missing directory warning")
        check(len(group_artifacts(found).get("AAW_TEST", [])) == 2, "run_id grouping beats filename")
        check("CODE_IMPLEMENTER" in artifact_header(next(a for a in found if a.path == valid)), "known artifact summary")

        candidate_path = root / "20260902_120000__ambiguous__abcdef12__classifier_candidate_HUMAN_REQUIRED.json"
        candidate_path.write_text(json.dumps({
            "pipeline": "P03", "task_class": "reuse", "ambiguity": "high",
            "risk": "medium", "recommended_coordinator_capability": "LIGHT_COORDINATOR",
            "confidence": 0.6,
        }), encoding="utf-8")
        candidate_artifact = load_artifact(candidate_path, "Routing")
        check(classifier_candidate((candidate_artifact,)) == candidate_artifact, "classifier candidate located")
        timeline = build_timeline("AAW_TEST", (candidate_artifact,), "HUMAN_REQUIRED")
        check("P03" in timeline and "NOT LAUNCHED" in timeline and "HUMAN_REQUIRED" in timeline, "timeline preserves missing execution state")

        state_root = root / "state"
        state_root.mkdir()
        (state_root / "orca_preflight_20260902_120000.json").write_text(
            json.dumps({"orca_version": "1.2.3", "smoke_test": "PASS"}), encoding="utf-8"
        )
        check(latest_orca_state(state_root).get("smoke_test") == "PASS", "latest preflight state read")
        ui_state_path = state_root / "gui_state.json"
        save_ui_state(ui_state_path, {"geometry": "1180x760", "last_page": "workflow"})
        check(load_ui_state(ui_state_path).get("last_page") == "workflow", "GUI state round trip")

        queue_store = QueueStore(root / "queues", state_root / "queue_settings.json")
        first = queue_store.create("Primary", True)
        check(first["status"] == "IDLE" and first["auto_continue"] is True, "create queue")
        first["display_name"] = "Primary renamed"; queue_store.save(first)
        check(queue_store.load_all(False)[0]["display_name"] == "Primary renamed", "rename queue persisted")
        single = queue_store.add_task(first, title="Fleet", goal="Repair Fleet", mode="SINGLE_TASK")
        workflow_task = queue_store.add_task(first, title="Water", goal="Repair Water", mode="WORKFLOW", workflow_id="flow.json", repo="D:\\repo", worktree="D:\\wt", bindings={"IMPLEMENT": "TERRA_HIGH", "REVIEW": "SOL_HIGH", "REPAIR": "SOL_MEDIUM"})
        check(single["position"] == 1 and workflow_task["position"] == 2, "single and workflow tasks added in order")
        custom_task = queue_store.add_task(first, title="Custom", goal="Custom goal", mode="CUSTOM_JOB", job_spec="job.json", repo="repo", worktree="wt2")
        check(custom_task["mode"] == "CUSTOM_JOB" and custom_task["job_spec"] == "job.json", "custom job reference persisted")
        check(workflow_task["bindings"]["IMPLEMENT"] == "TERRA_HIGH", "per-task bindings persisted")
        check(queue_store.reorder(first, workflow_task["task_id"], -1) and first["tasks"][0]["task_id"] == workflow_task["task_id"], "reorder waiting tasks")
        first["tasks"][0]["status"] = "RUNNING"
        check(not queue_store.reorder(first, first["tasks"][0]["task_id"], 1), "running task cannot be reordered")
        first["tasks"][0]["status"] = "WAITING"
        disposable = queue_store.add_task(first, title="Disposable", goal="Delete me", mode="SINGLE_TASK")
        check(queue_store.delete_waiting(first, disposable["task_id"]), "delete waiting task")
        check(len(queue_store.load_all(False)[0]["tasks"]) == 3, "queue JSON persistence")
        second = queue_store.create("OFFICE", False)
        check(len(queue_store.load_all(False)) == 2 and second["auto_continue"] is False, "multiple queues persisted")
        check(queue_store.save_settings({"max_simultaneous_active_queues": 1})["max_simultaneous_active_queues"] == 1, "max simultaneous queues one")
        check(queue_store.save_settings({"max_simultaneous_active_queues": 2})["max_simultaneous_active_queues"] == 2, "max simultaneous queues two")
        check(queue_store.save_settings({"max_simultaneous_active_queues": 99})["max_simultaneous_active_queues"] == 3, "queue concurrency capped at three")
        check(queue_decision("PASS", auto_continue=True, pause_after_current=False, remaining_waiting=True) == ("IDLE", True), "PASS auto-continues")
        check(queue_decision("PASS", auto_continue=False, pause_after_current=False, remaining_waiting=True) == ("IDLE", False), "auto_continue false stops")
        check(queue_decision("FAIL", auto_continue=True, pause_after_current=False, remaining_waiting=True)[0] == "PAUSED", "FAIL stops queue")
        check(queue_decision("BLOCKED", auto_continue=True, pause_after_current=False, remaining_waiting=True)[0] == "BLOCKED", "BLOCKED stops queue")
        check(queue_decision("HUMAN_GATE", auto_continue=True, pause_after_current=False, remaining_waiting=True)[0] == "WAITING_FOR_HUMAN", "HUMAN_GATE stops queue")
        check(queue_decision("HUMAN_REQUIRED", auto_continue=True, pause_after_current=False, remaining_waiting=True)[0] == "WAITING_FOR_HUMAN", "HUMAN_REQUIRED stops queue")
        first["status"] = "RUNNING"; first["tasks"][0]["status"] = "RUNNING"; first["tasks"][0]["process_pid"] = 99999999; queue_store.save(first)
        recovered = {item["queue_id"]: item for item in queue_store.load_all(True)}[first["queue_id"]]
        check(recovered["tasks"][0]["status"] == "INTERRUPTED" and recovered["status"] == "PAUSED", "stale running becomes interrupted")
        check(process_exists(os.getpid()), "live process ownership check")
        runtime = {"harnesses": {"codex": {"state": "AVAILABLE", "reason": "Codex CLI responds"}, "claude": {"state": "UNAVAILABLE", "reason": "Claude CLI not configured"}}}
        catalog = load_implementer_profiles_for_ui()
        check(get_runtime_profile_state("TERRA_HIGH", catalog, runtime)["state"] == "VERIFIED", "Terra verified with shared runtime")
        check(get_runtime_profile_state("SOL_MEDIUM", catalog, runtime)["state"] == "VERIFIED", "Sol verified with shared runtime")
        sonnet_state = get_runtime_profile_state("SONNET_HIGH", catalog, runtime)
        check(sonnet_state["state"] == "UNAVAILABLE" and sonnet_state["reason"] == "Claude CLI not configured", "Sonnet remains unavailable with readable reason")
        check(get_runtime_profile_state("TERRA_HIGH", catalog, None)["state"] == "NOT_CHECKED", "not checked is distinct from unavailable")
        check("merge" not in " ".join(workflow_argv("flow.json", "goal", "D:\\repo", "D:\\wt", False)).lower(), "queue workflow argv does not merge or push")

        app_settings = AppSettings(state_root / "app_settings.json")
        defaults = app_settings.load()
        check(defaults["default_recipe"] in RECIPES and defaults["default_execution_preset"] in EXECUTION_PRESETS, "app settings defaults valid")
        saved = app_settings.save({"default_recipe": "QUICK", "default_execution_preset": "DEEP", "unknown_key": "x"})
        check(saved["default_recipe"] == "QUICK" and "unknown_key" not in saved, "app settings persist known keys only")
        check(app_settings.load()["default_execution_preset"] == "DEEP", "app settings round trip")
        bad = app_settings.save({"default_recipe": "NONSENSE"})
        check(bad["default_recipe"] == AppSettings.DEFAULTS["default_recipe"], "invalid recipe falls back to default")
        catalog_ids = set(load_implementer_profiles_for_ui())
        for preset in EXECUTION_PRESETS.values():
            for role, profile_id in preset["bindings"].items():
                check(profile_id in catalog_ids, f"preset profile {profile_id} exists in catalog")
            check(preset["preprocess"] in ("OFF", "AUTO_SAFE", "CUSTOM"), "preset preprocess policy is valid")
        for spec in RECIPES.values():
            check(spec["primitive"] in ("SINGLE_TASK", "WORKFLOW", "CUSTOM_JOB", "ADVANCED"), "recipe maps to a known primitive")
        check(aaw_analytics is not None, "analytics module importable from Control Center")

    if failures:
        print("SELF_TEST_FAILED")
        for failure in failures:
            print("-", failure)
        return 1
    print(f"SELF_TEST_OK ({total} checks)")
    return 0


def gui_smoke_test() -> int:
    root = tk.Tk()
    root.withdraw()
    app = ControlCenter(root)
    app._show_preflight(run_preflight_checks())
    root.update_idletasks()
    check = app.dry_var.get() and not app.launch_var.get() and app.execution_mode_var.get() == "direct" and app.mode_var.get() == "SINGLE TASK" and str(app.viewer.cget("state")) == "disabled"
    check = check and {"home", "new", "queues", "runs", "insights", "settings", "workflow", "custom"}.issubset(app.pages)
    check = check and {"home", "new", "queues", "runs", "insights", "settings"}.issubset(app.nav_buttons)
    check = check and "workflow" not in app.nav_buttons and "custom" not in app.nav_buttons
    check = check and {"N01", "N03", "N04"}.issubset(app.node_binding_vars)
    check = check and app._gui_bindings() == ["N01=TERRA_HIGH", "N03=SOL_HIGH", "N04=SOL_MEDIUM"]
    terra = next(display for display, profile_id in app.binding_display_to_id.items() if profile_id == "TERRA_HIGH")
    app.node_binding_vars["N01"].set(terra)
    check = check and app._gui_bindings() == ["N01=TERRA_HIGH", "N03=SOL_HIGH", "N04=SOL_MEDIUM"]
    sonnet = next(display for display, profile_id in app.binding_display_to_id.items() if profile_id == "SONNET_HIGH")
    app.node_binding_vars["N01"].set(sonnet)
    check = check and app._gui_bindings()[0] == "N01=SONNET_HIGH"
    fable = next(display for display, profile_id in app.binding_display_to_id.items() if profile_id == "FABLE_HIGH")
    app.node_binding_vars["N01"].set(fable)
    try:
        app._gui_bindings()
    except ValueError:
        pass
    else:
        check = False
    original_chooser = filedialog.askdirectory
    try:
        filedialog.askdirectory = lambda **_kwargs: str(AAW_ROOT)  # type: ignore[assignment]
        app.repo_var.set("")
        app.browse_repository()
        check = check and app.repo_var.get() == str(AAW_ROOT)
        app.worktree_var.set("")
        app.browse_worktree()
        check = check and app.worktree_var.get() == str(AAW_ROOT)
    finally:
        filedialog.askdirectory = original_chooser  # type: ignore[assignment]
    app._show_human_required("AAW_TEST")
    dialogs = [child for child in root.winfo_children() if isinstance(child, tk.Toplevel)]
    check = check and any(child.title() == "Potrzebna decyzja" for child in dialogs)
    for child in dialogs:
        child.destroy()
    app.workflow_human_run_id = "AAW_TEST"
    app._show_human_gate({"workflow_bindings": {"N01": {"role": "IMPLEMENT", "profile": "TERRA_HIGH"}, "N03": {"role": "REVIEW", "profile": "SOL_HIGH"}}, "completed_nodes": [], "workflow_summary": {}})
    dialogs = [child for child in root.winfo_children() if isinstance(child, tk.Toplevel)]
    check = check and any(child.title() == "Workflow zakończył pracę" for child in dialogs)
    for child in dialogs:
        child.destroy()
    root.destroy()
    print("GUI_SMOKE_OK" if check else "GUI_SMOKE_FAILED")
    return 0 if check else 1


def queue_smoke_test() -> int:
    checks: list[tuple[bool, str]] = []
    def check(value: bool, name: str) -> None:
        checks.append((bool(value), name))
    with tempfile.TemporaryDirectory(prefix="aaw_queue_smoke_") as temp:
        root = Path(temp)
        store = QueueStore(root / "queues", root / "settings.json")
        store.save_settings({"max_simultaneous_active_queues": 2, "default_auto_continue": True})
        workflow_path = root / "workflow.json"
        workflow_path.write_text(json.dumps({"workflow_id": "SMOKE", "nodes": [{"id": "N01", "type": "IMPLEMENT"}, {"id": "N03", "type": "REVIEW"}, {"id": "N04", "type": "REPAIR"}]}), encoding="utf-8")
        primary = store.create("Primary", True)
        first = store.add_task(primary, title="Single", goal="single smoke", mode="SINGLE_TASK")
        second = store.add_task(primary, title="Workflow", goal="workflow smoke", mode="WORKFLOW", workflow_id=str(workflow_path), repo="D:\\repo", worktree="D:\\wt-a", bindings={"IMPLEMENT": "TERRA_HIGH", "REVIEW": "SOL_HIGH", "REPAIR": "SOL_MEDIUM"})
        app = ControlCenter.__new__(ControlCenter)
        app.queue_store = store; app.queue_docs = {primary["queue_id"]: primary}; app.profile_catalog = load_implementer_profiles_for_ui()
        app.model_runtime = {"harnesses": {"codex": {"state": "AVAILABLE", "reason": "Codex CLI responds"}, "claude": {"state": "UNAVAILABLE", "reason": "Claude CLI not configured"}}}
        app.queue_run_owners = {}; app.current_task = ""; app.current_result = {}; app.current_run_id = ""; app.output = None
        captured: list[tuple[str, str, str, list[str]]] = []
        app._run_queue_process = lambda queue_id, task_id, mode, argv: captured.append((queue_id, task_id, mode, argv))
        app.refresh_queues = lambda *_args, **_kwargs: None; app.refresh_artifacts = lambda *_args, **_kwargs: None
        app._set_text = lambda *_args, **_kwargs: None; app._show_human_gate = lambda *_args: None; app._show_human_required = lambda *_args: None
        app._start_waiting_queues = lambda: None
        original_thread = threading.Thread
        class ImmediateThread:
            def __init__(self, target: Any, args: tuple[Any, ...] = (), daemon: bool = False, **_kwargs: Any) -> None:
                self.target, self.args = target, args
            def start(self) -> None:
                self.target(*self.args)
        try:
            threading.Thread = ImmediateThread  # type: ignore[assignment]
            ControlCenter._start_queue(app, primary["queue_id"])
            check(primary["status"] == "RUNNING" and first["status"] == "RUNNING" and captured[0][2] == "SINGLE_TASK", "queue START dispatches first waiting task")
            ControlCenter._on_queue_task_complete(app, primary["queue_id"], first["task_id"], "SINGLE_TASK", (0, json.dumps({"status": "SUCCESS", "run_id": "RUN_SINGLE"}), "", captured[0][3]))
            check(first["status"] == "PASS" and second["status"] == "RUNNING" and len(captured) == 2, "PASS automatically dispatches next task")
            check("--bind" in captured[1][3] and second["resolved_bindings"] == {"N01": "TERRA_HIGH", "N03": "SOL_HIGH", "N04": "SOL_MEDIUM"}, "workflow bindings freeze at task start")
            ControlCenter._on_queue_task_complete(app, primary["queue_id"], second["task_id"], "WORKFLOW", (1, json.dumps({"status": "FAIL", "run_id": "RUN_WORKFLOW"}), "", captured[1][3]))
            check(primary["status"] == "PAUSED" and second["status"] == "FAIL", "FAIL stops queue without retry or skip")

            q1 = store.create("Office", True); t1 = store.add_task(q1, title="One", goal="one", mode="WORKFLOW", workflow_id=str(workflow_path), repo="D:\\repo", worktree="D:\\shared", bindings={"IMPLEMENT": "TERRA_HIGH", "REVIEW": "SOL_HIGH", "REPAIR": "SOL_MEDIUM"})
            q2 = store.create("Experiments", True); t2 = store.add_task(q2, title="Two", goal="two", mode="WORKFLOW", workflow_id=str(workflow_path), repo="D:\\repo", worktree="D:\\shared", bindings={"IMPLEMENT": "TERRA_HIGH", "REVIEW": "SOL_HIGH", "REPAIR": "SOL_MEDIUM"})
            app.queue_docs |= {q1["queue_id"]: q1, q2["queue_id"]: q2}
            ControlCenter._start_queue(app, q1["queue_id"]); ControlCenter._start_queue(app, q2["queue_id"])
            check(t1["status"] == "RUNNING" and t2["status"] == "WAITING" and q2.get("stop_reason") == "WORKTREE_IN_USE", "same worktree collision stays waiting")
            q3 = store.create("Third", True); store.add_task(q3, title="Three", goal="three", mode="SINGLE_TASK"); app.queue_docs[q3["queue_id"]] = q3
            ControlCenter._start_queue(app, q3["queue_id"])
            check(q3["status"] == "RUNNING" and ControlCenter._active_queue_count(app) == 2, "two independent queues run at configured maximum")
            q4 = store.create("Fourth", True); store.add_task(q4, title="Four", goal="four", mode="SINGLE_TASK"); app.queue_docs[q4["queue_id"]] = q4
            ControlCenter._start_queue(app, q4["queue_id"])
            check(q4["status"] == "WAITING", "third active queue is held by concurrency limit")
            q1["status"] = "PAUSED"; q3["status"] = "PAUSED"
            custom_path = root / "custom_job.json"; custom_path.write_text("{}", encoding="utf-8")
            qcustom = store.create("Custom", True); custom = store.add_task(qcustom, title="Custom", goal="custom", mode="CUSTOM_JOB", job_spec=str(custom_path), repo="D:\\repo", worktree="D:\\wt-custom"); app.queue_docs[qcustom["queue_id"]] = qcustom
            ControlCenter._start_queue(app, qcustom["queue_id"])
            check(captured[-1][2] == "CUSTOM_JOB" and captured[-1][3] == custom_job_argv(str(custom_path), False), "queue delegates Custom Job to its runner")
            ControlCenter._on_queue_task_complete(app, qcustom["queue_id"], custom["task_id"], "CUSTOM_JOB", (0, json.dumps({"status":"WAITING_FOR_HUMAN","AAW_RUN_ID":"RUN_CUSTOM"}), "", captured[-1][3]))
            check(qcustom["status"] == "WAITING_FOR_HUMAN" and custom["status"] == "WAITING_FOR_HUMAN", "Custom Job Human Gate stops queue")
        finally:
            threading.Thread = original_thread  # type: ignore[assignment]
    failures = [name for passed, name in checks if not passed]
    if failures:
        print("QUEUE_SMOKE_FAILED")
        for name in failures:
            print("-", name)
        return 1
    print(f"QUEUE_SMOKE_OK ({len(checks)} checks; no worker process started)")
    return 0


def gui_layout_test() -> int:
    root = tk.Tk()
    app = ControlCenter(root)
    app._show_preflight(run_preflight_checks())
    checks: list[bool] = []
    for width, height in ((1920, 1080), (1366, 768), (1180, 760), (980, 620)):
        root.geometry(f"{width}x{height}+0+0")
        for nav_page in ("home", "new", "insights", "workflow"):
            app.show_page(nav_page)
            root.update()
        page = app.pages["workflow"]
        checks.append(isinstance(page, ScrollablePage) and str(page.scrollbar.grid_info().get("sticky")) == "ns")
        if isinstance(page, ScrollablePage):
            page.canvas.yview_moveto(1.0)
            root.update()
            top = app.workflow_button.winfo_rooty() - root.winfo_rooty()
            checks.append(0 <= top < height and top + app.workflow_button.winfo_height() <= height)
        for nav_page in ("home", "new", "queues", "runs", "insights", "settings"):
            checks.append(app.nav_buttons[nav_page].winfo_manager() == "grid")
        new_page = app.pages["new"]
        if isinstance(new_page, ScrollablePage):
            app.show_page("new"); root.update()
            new_page.canvas.yview_moveto(1.0); root.update()
            btn_top = app.run_button.winfo_rooty() - root.winfo_rooty()
            checks.append(0 <= btn_top < height and btn_top + app.run_button.winfo_height() <= height + 2)
    page = app.pages["workflow"]
    if isinstance(page, ScrollablePage):
        page.canvas.yview_moveto(0.0)
        before = page.canvas.yview()
        page._on_wheel(type("WheelEvent", (), {"delta": -120})())
        checks.append(page.canvas.yview() != before)
    checks.append({"N01", "N03", "N04"}.issubset(app.node_binding_vars))
    fable = next(display for display, profile_id in app.binding_display_to_id.items() if profile_id == "FABLE_HIGH")
    checks.append("unavailable" in fable and "custom" in app.pages)
    root.destroy()
    ok = all(checks)
    print(f"GUI_LAYOUT_OK ({len(checks)} checks; 1920x1080, 1366x768, 1180x760, 980x620)" if ok else f"GUI_LAYOUT_FAILED {checks}")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true", help="Run non-destructive internal tests")
    parser.add_argument("--gui-smoke-test", action="store_true", help="Create, update and close the GUI")
    parser.add_argument("--gui-layout-test", action="store_true", help="Validate page scrolling and compact-window access")
    parser.add_argument("--queue-smoke-test", action="store_true", help="Validate queue dispatch/state logic without starting workers")
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    if args.gui_smoke_test:
        return gui_smoke_test()
    if args.gui_layout_test:
        return gui_layout_test()
    if args.queue_smoke_test:
        return queue_smoke_test()
    root = tk.Tk()
    ControlCenter(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
