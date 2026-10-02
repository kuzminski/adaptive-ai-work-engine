#!/usr/bin/env python3
"""AAW PRODUCT MVP V0.1 — task creation, run control and the Human Gate surface.

This is a thin product layer over the frozen V0.3 autonomy engine. It owns
no execution logic and no run state of its own:

  * execution, phases, policy, ledger, run lock, resume reconciliation and the
    Human Gate are `autonomy_controller` / `autonomy_adapters` /
    `autonomy_policy` / `execution_ledger` / `autonomy_run_lock`, unchanged;
  * the only state authority is the engine's `autonomy_state.json`,
    `autonomy_events.jsonl` and the V0.4B ledger of each run.

What this module adds, all of it input or request records under
`<run>/PRODUCT/` (never read back by the engine):

  task.json           the form the user filled in, the resolved bindings
                      (shown before START) and the workspace it created;
  stop_request.json   a STOP SAFELY / force-stop request for the worker;
  worker.json / worker_exit.json / worker.log   the background worker process;
  decision.json       what the product did on a Human Gate action.

A run executes in a separate background worker process (`AAW --run-worker`),
so closing the window does not stop work and a product restart can show and
resume it.
"""

from __future__ import annotations

import datetime as dt
import getpass
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import autonomy_contract as ac
import autonomy_run_lock as rl
import product_home
import product_providers as pp
import product_recommendations as pr
import run_cancellation as rc

PRODUCT_DIR = "PRODUCT"
TASK_SCHEMA = "AAW_PRODUCT_TASK_V0.1"
MAX_DIRECTIONS = 12
BRANCH_PREFIX = "aaw/"


class ProductError(ValueError):
    """A user-facing refusal with a plain-language message."""


def now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def runs_root() -> Path:
    return product_home.runs_root()


def run_dir(run_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_\-]{6,80}", run_id or ""):
        raise ProductError("nieprawidłowy identyfikator zadania")
    return runs_root() / run_id


def product_dir(run_id: str) -> Path:
    return run_dir(run_id) / PRODUCT_DIR


def autonomy_dir(run_id: str) -> Path:
    return run_dir(run_id) / "AUTONOMY"


def load_task(run_id: str) -> dict[str, Any]:
    task = product_home.read_json(product_dir(run_id) / "task.json")
    if not isinstance(task, dict):
        raise ProductError(f"zadanie {run_id} nie istnieje")
    return task


def list_run_ids() -> list[str]:
    root = runs_root()
    rows = [p.name for p in root.iterdir() if (p / PRODUCT_DIR / "task.json").is_file()] if root.is_dir() else []
    return sorted(rows, reverse=True)


# ── git helpers (plain argv, never a shell) ──────────────────────────────────

def _git(path: Path | str, *args: str, check: bool = True, identity: bool = False) -> str:
    argv = ["git"]
    if identity:
        argv += ["-c", "user.name=AAW", "-c", "user.email=aaw@localhost"]
    argv += ["-C", str(path), *args]
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    done = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          stdin=subprocess.DEVNULL, **kwargs)
    if check and done.returncode != 0:
        raise ProductError(f"git {' '.join(args[:2])} nie powiodło się: {(done.stderr or done.stdout).strip()[:400]}")
    return (done.stdout or "").strip()


def _has_identity(path: Path) -> bool:
    return bool(_git(path, "config", "user.email", check=False)) and bool(_git(path, "config", "user.name", check=False))


def inspect_repo(path: str) -> dict[str, Any]:
    """Plain-language readiness of a folder for an isolated AAW run."""
    raw = (path or "").strip().strip('"')
    out: dict[str, Any] = {"path": raw, "exists": False, "is_git": False, "ready": False, "message": None}
    if not raw:
        out["message"] = "Wskaż folder projektu."
        return out
    folder = Path(raw).expanduser()
    if not folder.is_dir():
        out["message"] = "Ten folder nie istnieje."
        return out
    out["exists"] = True
    out["name"] = folder.resolve().name
    top = _git(folder, "rev-parse", "--show-toplevel", check=False)
    if not top:
        out["message"] = ("Ten folder nie jest repozytorium Git. AAW potrzebuje Gita, aby pracować w izolowanej "
                          "kopii (worktree) i nigdy nie zmieniać Twoich plików. Możesz utworzyć repozytorium "
                          "przyciskiem „Utwórz repozytorium Git”.")
        out["can_init_git"] = True
        return out
    out.update(is_git=True, top=str(Path(top)), name=Path(top).name)
    head = _git(top, "rev-parse", "--verify", "-q", "HEAD", check=False)
    if not head:
        out["message"] = "Repozytorium nie ma jeszcze żadnego commita. Zrób pierwszy commit (lub użyj „Utwórz repozytorium Git”)."
        out["can_init_git"] = True
        return out
    out["head"] = head
    out["branch"] = _git(top, "rev-parse", "--abbrev-ref", "HEAD", check=False)
    status = _git(top, "status", "--porcelain=v1", "--untracked-files=all", check=False)
    dirty = [line[3:] for line in status.splitlines() if line.strip()]
    out["dirty_files"] = dirty[:20]
    out["dirty_count"] = len(dirty)
    if dirty:
        out["message"] = (f"Repozytorium ma niezatwierdzone zmiany ({len(dirty)} plików). AAW startuje z ostatniego "
                          "commita i wymaga czystego katalogu głównego — zrób commit lub stash, potem spróbuj ponownie.")
        return out
    out["ready"] = True
    out["message"] = (f"Gotowe. AAW utworzy izolowany worktree z {out['branch']} @ {head[:10]}; "
                      "Twój folder nie zostanie zmieniony.")
    return out


def init_git_repo(path: str) -> dict[str, Any]:
    """Explicit user action: make a folder a Git repo with one snapshot commit."""
    folder = Path((path or "").strip().strip('"')).expanduser()
    if not folder.is_dir():
        raise ProductError("Ten folder nie istnieje.")
    top = _git(folder, "rev-parse", "--show-toplevel", check=False)
    if not top:
        _git(folder, "init", "-q")
        top = str(folder)
    if not _git(top, "rev-parse", "--verify", "-q", "HEAD", check=False):
        _git(top, "add", "-A")
        _git(top, "commit", "-q", "--allow-empty", "-m", "Initial snapshot (created by AAW)",
             identity=not _has_identity(Path(top)))
    return inspect_repo(str(folder))


# ── task → mandate ───────────────────────────────────────────────────────────

def _clean_list(values: Any, limit: int = MAX_DIRECTIONS) -> list[str]:
    if isinstance(values, str):
        values = values.splitlines()
    rows = []
    for value in values or []:
        text = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", str(value)).strip()
        if text:
            rows.append(text[:400])
    return rows[:limit]


def normalize_form(form: Mapping[str, Any]) -> dict[str, Any]:
    goal = str(form.get("goal") or "").strip()
    if len(goal) < 5:
        raise ProductError("Opisz, co chcesz zbudować (pole „Co chcesz zbudować?”).")
    advanced = dict(form.get("advanced") or {})
    return {
        "repo": str(form.get("repo") or "").strip().strip('"'),
        "goal": goal[:2000],
        "first_iteration": str(form.get("first_iteration") or "").strip()[:2000],
        "directions": _clean_list(form.get("directions")),
        "planning": form.get("planning") or None,
        "implementation": form.get("implementation") or None,
        "review": form.get("review") or None,
        "advanced": {
            "acceptance_criteria": _clean_list(advanced.get("acceptance_criteria"), 20),
            "required_evidence": _clean_list(advanced.get("required_evidence"), 10),
            "forbidden_areas": _clean_list(advanced.get("forbidden_areas"), 20),
            "max_iterations": advanced.get("max_iterations"),
            "max_repair_attempts": advanced.get("max_repair_attempts"),
            "profile_overrides": {k: str(v) for k, v in (advanced.get("profile_overrides") or {}).items() if v},
        },
        "base": form.get("base") if isinstance(form.get("base"), dict) else None,
    }


def build_mandate(form: Mapping[str, Any], mandate_id: str, settings: Mapping[str, Any]) -> dict[str, Any]:
    """The human mandate the engine freezes: iteration 1 contract + direction.

    Direction points become roadmap items that depend only on the first
    iteration: they set a direction, not an ordered TODO list, and the planner
    may skip a later item with an explicit reason.
    """
    first = form["first_iteration"] or form["goal"]
    items = [{"item_id": "STEP_1", "title": first[:400]}]
    for index, direction in enumerate(form["directions"], start=2):
        items.append({"item_id": f"STEP_{index}", "title": direction, "depends_on": ["STEP_1"]})
    advanced = form["advanced"]
    acceptance = advanced["acceptance_criteria"] or [
        f"The first iteration is implemented: {first[:400]}",
        "Project checks/tests that passed before this change still pass",
    ]
    max_iterations = advanced.get("max_iterations") or min(ac.HARD_MAX_ITERATIONS, len(items) + 2)
    max_repairs = advanced.get("max_repair_attempts") or settings.get("max_repair_attempts", 2)
    return {
        "mandate_id": mandate_id,
        "iteration_contract": {
            "goal": first,
            "scope": [],
            "acceptance_criteria": acceptance,
            "constraints": [
                "Work only inside the isolated AAW worktree; never merge, push, rebase or switch branches",
                "Leave changes uncommitted; a human decides about integration",
            ],
            "forbidden_changes": [],
            "required_evidence": advanced["required_evidence"],
        },
        "roadmap_mandate": {
            "objective": form["goal"],
            "items": items,
            "priorities": [],
            "possible_directions": form["directions"],
            "autonomy_bounds": {
                "max_iterations": int(max(1, min(ac.HARD_MAX_ITERATIONS, int(max_iterations)))),
                "max_repair_attempts": int(max(1, min(ac.HARD_MAX_REPAIR_ATTEMPTS, int(max_repairs)))),
                "allowed_areas": None,
                "forbidden_areas": [".git", *advanced["forbidden_areas"]],
            },
        },
    }


def detection_snapshot(refresh: bool = False) -> dict[str, Any]:
    cache = product_home.home() / "providers.json"
    data = None if refresh else product_home.read_json(cache)
    if not isinstance(data, dict) or "providers" not in data:
        data = pp.detect_all()
        product_home.write_json(cache, data)
    return data


def preview_task(form_in: Mapping[str, Any], *, detection: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Everything the user must see before START, plus blockers."""
    form = normalize_form(form_in)
    settings = product_home.load_settings()
    detection = detection or detection_snapshot()
    repo = inspect_repo(form["repo"])
    choices = {g: form[g] or settings.get(g) for g in pr.CHOICE_GROUPS}
    resolution = pr.resolve_choices(choices, runnable=pp.runnable_profiles(detection),
                                    overrides=form["advanced"]["profile_overrides"], detection=detection)
    mandate = build_mandate(form, "PREVIEW", settings)
    try:
        ac.validate_mandate(mandate)
        mandate_error = None
    except ac.AutonomyError as exc:
        mandate_error = str(exc)
    blockers = list(resolution["blockers"])
    if form["base"]:
        if not form["base"].get("commit"):
            blockers.append("brak commita bazowego dla kontynuacji")
    elif not repo["ready"]:
        blockers.append(repo["message"])
    if (detection.get("git") or {}).get("status") != pp.FOUND:
        blockers.append("Git nie jest zainstalowany.")
    if not detection.get("any_found"):
        blockers.append("Nie znaleziono żadnego obsługiwanego CLI AI (Claude CLI lub Codex CLI).")
    if mandate_error:
        blockers.append(f"zadanie odrzucone przez silnik: {mandate_error}")
    harnesses_used = {slot["harness"] for slot in resolution["slots"].values() if slot["required"]}
    logged_out = [p for p in detection.get("providers", [])
                  if p["harness"] in harnesses_used and p["status"] == pp.FOUND and p["login"] == pp.NOT_LOGGED_IN]
    for provider in logged_out:
        blockers.append(f"{provider['display_name']} nie jest zalogowane. {provider['setup_help'].get('login', '')}")
    warnings = list(resolution["warnings"])
    for provider in detection.get("providers", []):
        if provider["harness"] in harnesses_used and provider["login"] == pp.LOGIN_UNKNOWN \
                and provider["status"] == pp.FOUND:
            warnings.append(f"{provider['display_name']}: nie udało się bezpiecznie sprawdzić logowania.")
    planner = resolution["slots"]["initial_planner"]
    return {
        "can_start": not blockers,
        "blockers": [b for b in blockers if b],
        "warnings": warnings,
        "project": {"name": repo.get("name"), "path": repo.get("top") or form["repo"],
                    "branch": repo.get("branch"), "head": repo.get("head"), "base": form["base"]},
        "goal": form["goal"],
        "first_iteration": form["first_iteration"] or form["goal"],
        "roadmap": [{"item_id": i["item_id"], "title": i["title"]} for i in mandate["roadmap_mandate"]["items"]],
        "acceptance_criteria": mandate["iteration_contract"]["acceptance_criteria"],
        "limits": mandate["roadmap_mandate"]["autonomy_bounds"],
        "planner": planner,
        "implementer_policy": {k: resolution["slots"][k] for k in (
            "implementer_default", "implementer_harder", "implementer_hard",
            "implementer_capability_escalation", "repair_default", "repair_hard", "review_pretreatment")},
        "review_policy": {k: resolution["slots"][k] for k in (
            "primary_reviewer", "final_review_default", "final_review_hard", "final_review_critical")},
        "choices": resolution["choices"],
        "catalog": {"version": resolution["catalog_version"], "source": resolution["catalog_source"]},
        "providers": [{"display_name": p["display_name"], "status": p["status"], "version": p["version"],
                       "login": p["login"]} for p in detection.get("providers", [])],
        "safety": [
            "Izolowany worktree — Twój folder projektu nie jest modyfikowany.",
            "Brak automatycznego merge.",
            "Brak automatycznego push.",
            "Human Gate po zakończeniu — decyzja zawsze należy do Ciebie.",
            "Nie zmieniaj plików w głównym folderze repozytorium w trakcie pracy AAW — AAW to wykryje i zatrzyma się.",
        ],
        "_form": form, "_resolution": resolution, "_mandate": mandate,
    }


def public_preview(preview: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in preview.items() if not k.startswith("_")}


# ── worker launch ─────────────────────────────────────────────────────────────

def entry_argv() -> list[str]:
    """How to start this program again as a worker (portable exe or source)."""
    if product_home.is_frozen():
        return [sys.executable]
    return [sys.executable, str(Path(__file__).resolve().with_name("AAW.py"))]


def launch_worker(run_id: str, mode: str, extra: Sequence[str] = ()) -> int:
    pdir = product_dir(run_id)
    pdir.mkdir(parents=True, exist_ok=True)
    log = (pdir / "worker.log").open("a", encoding="utf-8")
    log.write(f"\n=== {now()} launching worker mode={mode}\n")
    log.flush()
    env = dict(os.environ)
    env["AAW_PRODUCT_HOME"] = str(product_home.home())
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) |
                                   getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen([*entry_argv(), "--run-worker", run_id, "--mode", mode, *extra],
                               stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, env=env,
                               cwd=str(product_home.home()), **kwargs)
    log.close()
    # Reap the child when it exits so a finished worker never lingers as a zombie.
    threading.Thread(target=process.wait, name=f"aaw-reap-{run_id}", daemon=True).start()
    product_home.write_json(pdir / "worker_launch.json", {"pid": process.pid, "mode": mode, "launched_at": now()})
    return process.pid


Launcher = Callable[[str, str, Sequence[str]], int]


def start_task(form_in: Mapping[str, Any], *, detection: Mapping[str, Any] | None = None,
               launcher: Launcher | None = None) -> dict[str, Any]:
    preview = preview_task(form_in, detection=detection)
    if not preview["can_start"]:
        raise ProductError("Nie można wystartować: " + " ".join(preview["blockers"]))
    form, resolution = preview["_form"], preview["_resolution"]
    run_id = "AAW_TASK_" + dt.datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    settings = product_home.load_settings()
    mandate = build_mandate(form, run_id, settings)
    branch = BRANCH_PREFIX + run_id.lower()
    worktree = product_home.worktrees_root() / run_id
    if form["base"]:
        repo_top = Path(form["base"]["repo"])
        base_commit = form["base"]["commit"]
    else:
        info = inspect_repo(form["repo"])
        if not info["ready"]:
            raise ProductError(info["message"])
        repo_top, base_commit = Path(info["top"]), info["head"]
    _git(repo_top, "worktree", "add", "-q", "-b", branch, str(worktree), base_commit)
    task = {
        "schema_version": TASK_SCHEMA, "run_id": run_id, "created_at": now(),
        "form": form, "mandate_input": mandate,
        "workspace": {"repo": str(repo_top), "worktree": str(worktree), "branch": branch,
                      "base_commit": base_commit, "project_name": repo_top.name},
        "resolution": {k: resolution[k] for k in ("choices", "catalog_version", "catalog_source", "slots",
                                                   "warnings")},
        "roles_config": resolution["roles_config"],
        "executor_limits": {"timeout_s": int(settings.get("provider_timeout_s", 1800)), "max_turns": 30},
        "authority_note": "Product input record. The engine state (AUTONOMY/autonomy_state.json), its journal and "
                          "the ledger are the only run authority; this file is never read by the engine.",
    }
    product_home.write_json(product_dir(run_id) / "task.json", task)
    (launcher or launch_worker)(run_id, "start", ())
    return {"run_id": run_id, "worktree": str(worktree), "branch": branch}


# ── stop / resume ────────────────────────────────────────────────────────────

def request_stop(run_id: str, *, force: bool = False) -> dict[str, Any]:
    load_task(run_id)
    request = {"requested_at": now(), "force": bool(force), "by": _human_identity(),
               "mode": "FORCE" if force else "SAFE"}
    product_home.write_json(product_dir(run_id) / "stop_request.json", request)
    return request


def clear_stop_request(run_id: str) -> None:
    path = product_dir(run_id) / "stop_request.json"
    if path.exists():
        history = product_dir(run_id) / "stop_history"
        history.mkdir(exist_ok=True)
        path.replace(history / f"stop_request_{dt.datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json")


def worker_alive(run_id: str) -> bool:
    found = rl.inspect_lock(autonomy_dir(run_id))
    if found["held"] and found["outcome"] == rl.RUN_LOCK_BUSY:
        return True
    launch = product_home.read_json(product_dir(run_id) / "worker_launch.json") or {}
    exit_record = product_home.read_json(product_dir(run_id) / "worker_exit.json") or {}
    if launch.get("pid") and exit_record.get("launched_pid") != launch.get("pid"):
        alive, _ = rl.process_identity(int(launch["pid"]))
        return bool(alive) and not _is_zombie(int(launch["pid"]))
    return False


def _is_zombie(pid: int) -> bool:
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0] == "Z"
    except (OSError, IndexError):
        return False


def resume_task(run_id: str, *, expected_lock_token: str | None = None,
                launcher: Launcher | None = None) -> dict[str, Any]:
    import autonomy_controller as ctl
    load_task(run_id)
    state = ctl.load_state(run_id, runs_root())
    if state["status"] != ac.RUNNING:
        raise ProductError("To zadanie nie jest wstrzymane — nie ma czego wznawiać.")
    if worker_alive(run_id):
        raise ProductError("To zadanie już pracuje.")
    found = rl.inspect_lock(autonomy_dir(run_id))
    extra: list[str] = []
    if found["held"]:
        token = (found["owner"] or {}).get("owner_token")
        if found["outcome"] != rl.RUN_LOCK_STALE:
            raise ProductError("Zadanie jest zablokowane przez inny, możliwie żywy proces — AAW go nie przejmie.")
        if expected_lock_token and expected_lock_token != token:
            raise ProductError("Stan zadania zmienił się od chwili wyświetlenia — odśwież widok.")
        extra = ["--reconcile-lock", token]
    clear_stop_request(run_id)
    pid = (launcher or launch_worker)(run_id, "resume", extra)
    return {"run_id": run_id, "pid": pid, "reconciled_lock": bool(extra)}


class ProductStopToken(rc.CancellationToken):
    """STOP SAFELY on top of the existing cooperative cancellation token.

    * SAFE: never start another phase (the controller honours the request at
      the next phase boundary and persists the next phase); a running
      read-only call (plan continuation, verification, review) is cancelled
      now — resume replays it under a new execution_id. EXECUTE / REPAIR and
      the one-shot initial architect are NOT killed: their effects would be
      uncertain, so the run waits for them to finish and then pauses.
    * FORCE: the base token's behaviour — terminate everything now. An
      interrupted EXECUTE/REPAIR is then escalated on resume
      (INTERRUPTED_IN_FLIGHT) and shown at the Human Gate as uncertain.
    """

    def __init__(self, run_id: str) -> None:
        super().__init__(run_id)
        self._pause: dict[str, Any] | None = None
        self.in_flight: Callable[[], Mapping[str, Any] | None] = lambda: None

    @staticmethod
    def _interruptible(flight: Mapping[str, Any] | None) -> bool:
        return bool(flight) and flight.get("phase") not in ac.SIDE_EFFECT_PHASES \
            and flight.get("role") != "initial_planner"

    def request_stop(self, *, force: bool, reason: str) -> dict[str, Any]:
        with self._lock:
            first = self._pause is None
            self._pause = {"reason": reason, "force": force, "at": now()}
        flight = self.in_flight()
        if force:
            return {"mode": "FORCE", "in_flight": dict(flight or {}), **self.request(reason)}
        if self._interruptible(flight):
            return {"mode": "CANCEL_READ_ONLY_CALL", "in_flight": dict(flight or {}), **self.request(reason)}
        return {"mode": "PAUSE_AT_NEXT_BOUNDARY", "first_request": first, "waiting_for": dict(flight or {})}

    def raise_if_requested(self, boundary: str) -> None:
        with self._lock:
            pause = self._pause
        if pause and (boundary.startswith("AUTONOMY_PHASE_BOUNDARY") or
                      (boundary.startswith("before_spawn") and self._interruptible(self.in_flight()))):
            raise rc.RunCancelled(pause["reason"], source=rc.CANCEL_REQUESTED_BY_USER, at_boundary=boundary)
        super().raise_if_requested(boundary)


def _human_identity() -> str:
    try:
        user = getpass.getuser()
    except Exception:
        user = "user"
    return f"human:{user}"


def _executors(limits: Mapping[str, Any]) -> dict[str, Any]:
    import autonomy_adapters as aa
    return aa.build_direct_executors(timeout=int(limits.get("timeout_s", 1800)),
                                     max_turns=int(limits.get("max_turns", 30)))


def worker_main(run_id: str, mode: str, *, reconcile_lock: str | None = None,
                executors: Mapping[str, Any] | None = None) -> int:
    """The background process that drives one run through the V0.3 engine."""
    import autonomy_controller as ctl
    pdir = product_dir(run_id)
    owner = rl.current_owner()
    product_home.write_json(pdir / "worker.json", {"run_id": run_id, "mode": mode, "pid": owner["pid"],
                                                   "process_creation_time": owner["process_creation_time"],
                                                   "started_at": now()})
    task = load_task(run_id)
    stats_root = runs_root()
    token = ProductStopToken(run_id)
    stop_events: list[dict[str, Any]] = []
    finished = threading.Event()

    def watch() -> None:
        seen = None
        while not finished.wait(0.4):
            request = product_home.read_json(pdir / "stop_request.json")
            if not isinstance(request, dict):
                continue
            key = (request.get("requested_at"), request.get("force"))
            if key == seen:
                continue
            seen = key
            effect = token.request_stop(force=bool(request.get("force")),
                                        reason=f"STOP {'FORCE' if request.get('force') else 'SAFELY'} requested "
                                               f"by {request.get('by')}")
            stop_events.append({**effect, "request": request, "observed_at": now()})
            product_home.write_json(pdir / "stop_effect.json", {"events": stop_events})

    outcome: dict[str, Any] = {"run_id": run_id, "mode": mode, "launched_pid": owner["pid"]}
    controller = None
    try:
        import workflow_runner as wr
        profiles = wr.load_implementer_profiles()
        roles = ac.validate_roles(task["roles_config"], profiles)
        workspace = task["workspace"]
        executor_set = dict(executors or _executors(task.get("executor_limits") or {}))
        threading.Thread(target=watch, name="aaw-stop-watch", daemon=True).start()
        with rc.cancellation_scope(token):
            if mode == "start":
                env = ctl.GitWorkspaceEnvironment(Path(workspace["repo"]), Path(workspace["worktree"]))
                controller = ctl.AutonomyController.start(run_id, task["mandate_input"], executors=executor_set,
                                                          env=env, roles=roles, stats_root=stats_root)
            else:
                if reconcile_lock:
                    record = rl.reconcile_stale_lock(
                        autonomy_dir(run_id), expected_owner_token=reconcile_lock,
                        operator=f"{_human_identity()} via AAW Product RESUME",
                        reason="user pressed RESUME on a run whose previous worker is no longer running")
                    outcome["lock_reconciliation"] = record
                env = ctl.GitWorkspaceEnvironment(Path(workspace["repo"]), Path(workspace["worktree"]),
                                                  resuming=True)
                controller = ctl.AutonomyController.resume(run_id, executors=executor_set, env=env, roles=roles,
                                                           stats_root=stats_root)
            token.in_flight = lambda: controller.state.get("in_flight")
            state = controller.run()
        outcome.update(status=state["status"], phase=state["phase"],
                       paused=state["status"] == ac.RUNNING)
        code = 0
    except Exception as exc:  # recorded for the user; the engine state keeps what it persisted
        outcome.update(status="WORKER_ERROR", error=f"{type(exc).__name__}: {exc}",
                       traceback=traceback.format_exc()[-6000:])
        if controller is not None:
            try:
                controller.release()
            except Exception:
                pass
        code = 1
    finally:
        finished.set()
    outcome.update(finished_at=now(), stop_events=stop_events)
    product_home.write_json(pdir / "worker_exit.json", outcome)
    return code


# ── Human Gate actions (engine human surface; no merge, no push) ─────────────

def accept(run_id: str, *, early_end: bool = False, note: str | None = None) -> dict[str, Any]:
    import autonomy_controller as ctl
    task = load_task(run_id)
    state = ctl.load_state(run_id, runs_root())
    hold = state.get("hold") or {}
    if state["status"] != ac.AWAITING_HUMAN:
        raise ProductError("Akceptacja jest możliwa tylko, gdy AAW czeka na Twoją decyzję.")
    if not hold.get("promotable"):
        raise ProductError("Tego wyniku nie można zaakceptować: AAW zatrzymał się na eskalacji albo żadna "
                           "iteracja nie przeszła final review. Możesz odrzucić lub zacząć nowe zadanie.")
    approver = _human_identity()
    try:
        ctl.approve_promotion(run_id, approver=approver, candidate_id=hold["candidate_id"],
                              early_end=bool(early_end), note=note, stats_root=runs_root())
        state = ctl.promote(run_id, stats_root=runs_root())  # no promoter: READY_FOR_EXTERNAL_INTEGRATION
    except ac.AutonomyError as exc:
        raise ProductError(str(exc)) from exc
    workspace = task["workspace"]
    decision = {"decision": "ACCEPTED", "at": now(), "by": approver, "candidate_id": hold["candidate_id"],
                "integration": state["promotion"]["integration"],
                "next_steps": [f"Zmiany są na gałęzi {workspace['branch']} w folderze {workspace['worktree']}.",
                               "AAW niczego nie zmergował ani nie wypchnął. Zintegruj je sam, gdy będziesz gotowy "
                               f"(np. git merge {workspace['branch']} po zatwierdzeniu zmian w worktree)."]}
    product_home.write_json(product_dir(run_id) / "decision.json", decision)
    return decision


def reject(run_id: str, *, reason: str) -> dict[str, Any]:
    import autonomy_controller as ctl
    load_task(run_id)
    try:
        ctl.reject(run_id, approver=_human_identity(), reason=reason or "rejected in AAW Product",
                   stats_root=runs_root())
    except ac.AutonomyError as exc:
        raise ProductError(str(exc)) from exc
    decision = {"decision": "REJECTED", "at": now(), "by": _human_identity(), "reason": reason,
                "next_steps": ["Worktree i gałąź zostały zachowane do wglądu; AAW niczego nie usunął."]}
    product_home.write_json(product_dir(run_id) / "decision.json", decision)
    return decision


def prepare_continuation(run_id: str, *, mode: str) -> dict[str, Any]:
    """'Dodaj dalszy kierunek' / 'Kontynuuj z nowym celem'.

    The frozen mandate of a run cannot grow (the engine escalates any extension
    attempt), so continuing is a NEW run. When the current candidate is
    promotable it is accepted (Human Gate) and checkpointed as a local commit
    on the run's own branch; the new run starts from that commit. Otherwise
    the old run is rejected as superseded and the new run starts from the
    repository's current HEAD. Never merges, never pushes.
    """
    import autonomy_controller as ctl
    if mode not in ("direction", "new_goal"):
        raise ProductError("nieznany tryb kontynuacji")
    task = load_task(run_id)
    state = ctl.load_state(run_id, runs_root())
    workspace = task["workspace"]
    base: dict[str, Any] | None = None
    if state["status"] == ac.AWAITING_HUMAN and (state.get("hold") or {}).get("promotable"):
        accept(run_id, early_end=not (state["hold"] or {}).get("roadmap_exhausted"),
               note=f"continued as a follow-up task ({mode})")
        state = ctl.load_state(run_id, runs_root())
    if state["status"] in (ac.HUMAN_APPROVED, ac.PROMOTED):
        worktree = Path(workspace["worktree"])
        if _git(worktree, "status", "--porcelain=v1", "--untracked-files=all", check=False):
            _git(worktree, "add", "-A")
            _git(worktree, "commit", "-q", "-m",
                 f"AAW: accepted candidate {(state.get('hold') or {}).get('candidate_id')} of {run_id}",
                 identity=not _has_identity(worktree))
        base = {"run_id": run_id, "repo": workspace["repo"], "branch": workspace["branch"],
                "commit": _git(worktree, "rev-parse", "HEAD")}
        product_home.write_json(product_dir(run_id) / "continuation.json", {"at": now(), "mode": mode, "base": base})
    elif state["status"] == ac.AWAITING_HUMAN:
        reject(run_id, reason=f"superseded by a follow-up task ({mode}); candidate was not promotable")
    elif state["status"] == ac.RUNNING:
        raise ProductError("Najpierw zatrzymaj zadanie albo poczekaj na Human Gate.")
    form = dict(task["form"])
    prefill = {"repo": workspace["repo"], "base": base,
               "planning": form.get("planning"), "implementation": form.get("implementation"),
               "review": form.get("review")}
    if mode == "direction":
        prefill.update(goal=form["goal"], first_iteration="", directions=[])
    else:
        prefill.update(goal="", first_iteration="", directions=[])
    return {"prefill": prefill, "base": base}
