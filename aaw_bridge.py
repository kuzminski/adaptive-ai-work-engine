#!/usr/bin/env python3
"""AAW UX RUNTIME BRIDGE V0.1 — the only surface a UI may talk to.

Purpose: give the canvas-first UX the smallest stable boundary over the
existing runtime, and nothing more.

    UI  ──HTTP/SSE──▶  aaw_bridge_server  ──▶  aaw_bridge  ──▶  workflow_runner
                                                          ──▶  workflow_schema
                                                          ──▶  routing_contract
                                                          ──▶  workflow_layout

Invariants this module exists to hold:

  * **The UI never touches runner state.** Everything the UI reads is a
    projection assembled here from durable artifacts — `workflow_state.json`,
    `routing_events.jsonl`, gate-decision files. No live runner object, no
    frontier list, no `by_id` table crosses the boundary. A run observed
    through this bridge and a run observed by reading the files by hand are
    the same run.
  * **No prose is parsed, ever.** Not by the UI and not here. Every field
    returned is a structured fact some runtime component already wrote.
  * **Writes are candidate-first.** A BUILD edit is validated as a candidate
    against the real `workflow_schema.validate_workflow` and the real routing
    rules before anything is written, and the write is atomic and
    stale-checked. There is no path from this module to a partial or invalid
    workflow file.
  * **Layout is not semantics.** Visual metadata goes to `workflow_layout`,
    which cannot reach a workflow file, and is stripped from any candidate.
  * **Cancellation is real.** `cancel_run` terminates the child process the
    run is blocked on. It does not set a flag for the UI to draw.

Deliberately absent, to keep the boundary small: planner proposals, branch
merge, workflow creation/deletion, multi-user identity, remote transport,
authentication beyond loopback binding, and any query language.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import threading
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import routing_contract
import run_cancellation
import run_recovery
import workflow_layout
import workflow_runner
import workflow_schema
from routing_contract import RoutingJournal
from workflow_schema import WorkflowValidationError, validate_workflow


BRIDGE_VERSION = "AAW_UX_RUNTIME_BRIDGE_V0.1"

# Run lifecycle as the UX sees it. `runner_status` carries the runtime's own
# richer status (WAITING_FOR_HUMAN, NO_ROUTE, CANCELLED, ...) unchanged; this
# is only the coarse question "is there still work happening".
RUN_PENDING = "PENDING"
RUN_ACTIVE = "ACTIVE"
RUN_SETTLED = "SETTLED"

# Why a write was refused. Closed set: the UX renders these directly.
WRITE_OK = "OK"
WRITE_SCHEMA_INVALID = "SCHEMA_INVALID"
WRITE_STALE = "STALE_BASE_HASH"
WRITE_IDENTITY_MISMATCH = "WORKFLOW_ID_MISMATCH"
WRITE_UNKNOWN_WORKFLOW = "UNKNOWN_WORKFLOW"
WRITE_ALREADY_EXISTS = "WORKFLOW_ALREADY_EXISTS"

# AAW CANVAS FUNCTIONALIZATION V0.1 — what a run's worktree is in, once the
# run has stopped. `PARTIAL_WORK_PRESENT` is not a warning the UX composes; it
# is a fact the bridge measures with `git status` in the run's own worktree.
WORK_CLEAN = "NO_PARTIAL_WORK"
WORK_PARTIAL = "PARTIAL_WORK_PRESENT"
WORK_UNKNOWN = "PARTIAL_WORK_UNKNOWN"

# Run-owned worktree resolution. These are recovery decisions, not runtime
# verdicts and not Git-history operations.
RECOVERY_UNRESOLVED = run_recovery.UNRESOLVED
RECOVERY_KEPT = run_recovery.KEPT
RECOVERY_DISCARDED = run_recovery.DISCARDED
RECOVERY_ADOPTED = run_recovery.ADOPTED


class BridgeError(ValueError):
    """A request the bridge refuses. Carries a machine-readable code."""

    def __init__(self, code: str, message: str,
                 diagnostics: Sequence[Mapping[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.code = code
        # Per-node / per-edge attribution when the refusal has one, so a
        # canvas can mark the offending element rather than print a sentence.
        self.diagnostics: list[dict[str, Any]] = [dict(row) for row in (diagnostics or [])]

    def as_dict(self) -> dict[str, Any]:
        return {"error": self.code, "message": str(self), "diagnostics": self.diagnostics}


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="milliseconds")


# ─────────────────────────────── run handles ───────────────────────────────

class RunHandle:
    """One supervised run. Owned by the bridge, never handed to the UI.

    The UI receives `describe()`: ids, coarse and runtime status, paths. It
    never receives the thread, the token or the returned state object.
    """

    def __init__(self, run_id: str, *, workflow_id: str, goal: str,
                 state_path: Path, journal_path: Path, adopted: bool = False) -> None:
        self.run_id = run_id
        self.workflow_id = workflow_id
        self.goal = goal
        # An adopted run was started by another process. It is readable, but
        # this bridge owns no thread and no child of it, so it must never
        # report that it can stop it.
        self.adopted = adopted
        self.state_path = state_path
        self.journal_path = journal_path
        self.started_at = _now()
        self.finished_at: str | None = None
        self.cancel = run_cancellation.CancellationToken(run_id)
        self.thread: threading.Thread | None = None
        self.error: str | None = None
        self._final_status: str | None = None
        # Last whole state document seen, and the file stamp it came from.
        self.state_cache: dict[str, Any] | None = None
        self.state_stamp: tuple[int, int] | None = None
        # Set when this run was started as a `Run from here`; the plan itself
        # lives in the run's own state, this is only the request that made it.
        self.resumed_from: dict[str, Any] | None = None
        # The worktree this run was started against. Recorded here because a
        # run that never got as far as writing state — the workspace guard
        # refuses a dirty tree before the first node — still has a worktree the
        # UX must be able to inspect. `run_state` remains the authority when
        # the run did write one.
        self.worktree: Path | None = None

    @property
    def alive(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    @property
    def lifecycle(self) -> str:
        if self.adopted:
            return RUN_SETTLED
        if self.alive:
            return RUN_ACTIVE
        if self.thread is None:
            return RUN_PENDING
        return RUN_SETTLED

    def describe(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "workflow_id": self.workflow_id, "goal": self.goal,
            "lifecycle": self.lifecycle, "adopted": self.adopted,
            "runner_status": self._final_status,
            "started_at": self.started_at, "finished_at": self.finished_at,
            "error": self.error, "cancellation": self.cancel.snapshot(),
            "resumed_from": self.resumed_from,
            "state_path": str(self.state_path), "journal_path": str(self.journal_path),
        }


# ─────────────────────────────── the bridge ───────────────────────────────

class AawBridge:
    """The public UX/runtime boundary. One instance per process."""

    def __init__(self, *, workflows_root: Path | None = None, stats_root: Path | None = None,
                 runner: Any = workflow_runner) -> None:
        self.runner = runner
        self._workflows_root = Path(workflows_root) if workflows_root else None
        self._stats_root = Path(stats_root) if stats_root else None
        self._runs: dict[str, RunHandle] = {}
        self._active_worktrees: dict[str, str] = {}
        self._lock = threading.RLock()

    # Roots are resolved late and through the runner module so that a test
    # which repoints `workflow_runner.STATS_ROOT` repoints the bridge too,
    # and there is exactly one answer to "where does a run live".
    @property
    def workflows_root(self) -> Path:
        return self._workflows_root or (Path(self.runner.AAW_ROOT) / "WORKFLOWS")

    @property
    def stats_root(self) -> Path:
        return self._stats_root or Path(self.runner.STATS_ROOT)

    # ══════════════════════════════ BUILD ══════════════════════════════

    def list_workflows(self) -> dict[str, Any]:
        """Every workflow definition on disk, with identity and health.

        A file that no longer validates is listed with its error rather than
        hidden: an editor must be able to open a broken workflow, which is
        precisely when the user needs it most.
        """
        rows: list[dict[str, Any]] = []
        for path in sorted(self.workflows_root.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                validate_workflow(data)
            except (OSError, json.JSONDecodeError, WorkflowValidationError) as exc:
                rows.append({"workflow_id": path.stem, "path": str(path), "valid": False,
                             "error": str(exc), "semantic_hash": None})
                continue
            rows.append({
                "workflow_id": data["workflow_id"], "path": str(path), "valid": True,
                "version": data.get("version"), "description": data.get("description"),
                "node_count": len(data.get("nodes") or []),
                "routing_contract": data.get("routing_contract"),
                "semantic_hash": routing_contract.semantic_hash(data),
                "has_layout": workflow_layout.layout_path(self.workflows_root, data["workflow_id"]).is_file(),
            })
        return {"bridge_version": BRIDGE_VERSION, "workflows_root": str(self.workflows_root),
                "workflows": rows}

    def workflow_path(self, workflow_id: str) -> Path:
        """Resolve an id to a file without letting the id become a path."""
        listing = self.list_workflows()["workflows"]
        for row in listing:
            if str(row["workflow_id"]) == str(workflow_id):
                return Path(row["path"])
        raise BridgeError(WRITE_UNKNOWN_WORKFLOW, f"unknown workflow {workflow_id!r}")

    def load_workflow(self, workflow_id: str) -> dict[str, Any]:
        """Definition, static projection, identity and layout in one read.

        One call, because the canvas needs all four to draw a single frame and
        a UI that has to stitch four responses will eventually draw a graph
        whose layout belongs to a different version of it.
        """
        path = self.workflow_path(workflow_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BridgeError("UNREADABLE_WORKFLOW", f"cannot read {path}: {exc}") from exc
        frame: dict[str, Any] = {
            "bridge_version": BRIDGE_VERSION,
            "workflow_id": data.get("workflow_id", workflow_id), "path": str(path),
            "valid": False, "errors": [], "semantic_hash": None,
            "definition": data, "projection": None, "layout": None,
        }
        try:
            validate_workflow(data)
        except (WorkflowValidationError, TypeError, KeyError, AttributeError) as exc:
            # An editor must be able to open a workflow that no longer
            # validates — that is precisely when the user needs it. The
            # definition is returned with the reason it cannot be projected;
            # nothing pretends the graph is drawable.
            frame["errors"] = [str(exc)]
            return frame
        projection = routing_contract.workflow_projection(data)
        frame.update({
            "valid": True, "semantic_hash": projection["semantic_hash"],
            "projection": projection,
            "layout": self.load_layout(str(data["workflow_id"])),
        })
        return frame

    def graph_projection(self, workflow_id: str) -> dict[str, Any]:
        """Static graph only — the BUILD-mode read (gap U2 of the V0.1 contract)."""
        path = self.workflow_path(workflow_id)
        data = json.loads(path.read_text(encoding="utf-8"))
        validate_workflow(data)
        return routing_contract.workflow_projection(data)

    def validate_candidate(self, candidate: Mapping[str, Any]) -> dict[str, Any]:
        """Dry-run a proposed workflow. Never writes.

        This is the same validator the runner loads a workflow through — not a
        UI-side approximation of it — so a candidate that validates here
        cannot fail to load later for a schema or routing reason.
        """
        cleaned, stripped = strip_visual_fields(candidate)
        report: dict[str, Any] = {
            "bridge_version": BRIDGE_VERSION,
            "stripped_visual_fields": stripped,
            "valid": False, "code": WRITE_SCHEMA_INVALID, "errors": [], "diagnostics": [],
            "semantic_hash": None, "projection": None,
        }

        def refuse(message: str, *, node_id: str | None = None, edge_id: str | None = None,
                   source: str = "SCHEMA") -> dict[str, Any]:
            # `errors` stays a list of strings for every existing consumer.
            # `diagnostics` is the same failure with its subject attached, so
            # a canvas can put the error on the node or edge that caused it
            # instead of reading it out of a log panel — and without parsing
            # the message, which is the one technique this project refuses.
            report["errors"] = [message]
            report["diagnostics"] = [{"message": message, "node_id": node_id,
                                      "edge_id": edge_id, "source": source}]
            return report

        try:
            validate_workflow(cleaned)
        except WorkflowValidationError as exc:
            # This validated authoring friction has several equally affected
            # edges. Attribute the same real schema refusal to each one so the
            # canvas can offer a local, non-inventive repair on the wires.
            node_id = getattr(exc, "node_id", None)
            if node_id and "FIRST_MATCH allows at most one unconditional edge" in str(exc):
                node = next((row for row in (cleaned.get("nodes") or [])
                             if isinstance(row, dict) and str(row.get("id")) == str(node_id)), None)
                unconditional = [row for row in ((node or {}).get("edges") or [])
                                 if isinstance(row, dict) and row.get("when") in (None, {})]
                report["errors"] = [str(exc)]
                report["diagnostics"] = [
                    {"message": str(exc), "node_id": str(node_id),
                     "edge_id": str(edge.get("edge_id")), "source": "SCHEMA",
                     "repair": "KEEP_ONLY_THIS_UNCONDITIONAL"}
                    for edge in unconditional if edge.get("edge_id")
                ]
                return report
            return refuse(str(exc), node_id=getattr(exc, "node_id", None),
                          edge_id=getattr(exc, "edge_id", None))
        except (TypeError, KeyError, AttributeError) as exc:
            # A malformed candidate can trip the validator's own assumptions
            # before it reaches an explicit check. Report it as invalid input,
            # never as a bridge crash.
            return refuse(f"malformed candidate: {exc!r}", source="MALFORMED")
        try:
            # Routing validity is not fully covered by the schema: an edge can
            # be well-formed and still name an unsupported kind or predicate
            # combination only the gate compiler rejects. Compile every node's
            # edges the way the runner will.
            for node in cleaned["nodes"]:
                try:
                    routing_contract.compile_edges(node)
                except routing_contract.RoutingContractError as exc:
                    return refuse(f"routing: {exc}", node_id=str(node.get("id")), source="ROUTING")
            projection = routing_contract.workflow_projection(cleaned)
        except routing_contract.RoutingContractError as exc:
            return refuse(f"routing: {exc}", source="ROUTING")
        report.update({"valid": True, "code": WRITE_OK, "errors": [], "diagnostics": [],
                       "semantic_hash": projection["semantic_hash"], "projection": projection,
                       "candidate": cleaned})
        return report

    def save_workflow(self, workflow_id: str, candidate: Mapping[str, Any], *,
                      base_semantic_hash: str | None = None,
                      allow_create: bool = False) -> dict[str, Any]:
        """candidate → schema validation → routing validation → atomic write.

        Refusals, all before any byte is written:

          * the candidate does not validate            → SCHEMA_INVALID
          * its `workflow_id` is not the target's      → WORKFLOW_ID_MISMATCH
          * the on-disk semantic hash moved under it   → STALE_BASE_HASH

        The stale check uses the same canonical hashing as execution identity,
        so "the file changed" means "its executable semantics changed" and a
        concurrent layout save can never trigger it.
        """
        report = self.validate_candidate(candidate)
        if not report["valid"]:
            error = BridgeError(WRITE_SCHEMA_INVALID,
                                "; ".join(report["errors"]) or "candidate is invalid")
            error.diagnostics = report["diagnostics"]
            raise error
        cleaned = report["candidate"]
        if str(cleaned["workflow_id"]) != str(workflow_id):
            raise BridgeError(
                WRITE_IDENTITY_MISMATCH,
                f"candidate declares workflow_id {cleaned['workflow_id']!r}, target is {workflow_id!r}")

        try:
            path = self.workflow_path(workflow_id)
            current = json.loads(path.read_text(encoding="utf-8"))
            current_hash = routing_contract.semantic_hash(current)
        except BridgeError:
            if not allow_create:
                raise
            path, current_hash = self.workflows_root / f"{workflow_id}.json", None
            workflow_layout.layout_path(self.workflows_root, workflow_id)  # validates the id

        if base_semantic_hash is not None and str(base_semantic_hash) != str(current_hash):
            raise BridgeError(WRITE_STALE, (
                f"workflow {workflow_id} changed since this edit began "
                f"(on disk {current_hash}, edit based on {base_semantic_hash})"))

        if current_hash == report["semantic_hash"]:
            return {"bridge_version": BRIDGE_VERSION, "code": WRITE_OK, "written": False,
                    "reason": "SEMANTICALLY_IDENTICAL", "path": str(path),
                    "semantic_hash": current_hash, "stripped_visual_fields": report["stripped_visual_fields"],
                    "projection": report["projection"]}

        # Atomic: the live file is replaced only once a complete, validated
        # document exists beside it. A crash mid-write leaves the old workflow.
        self.runner.atomic_json(path, cleaned)
        return {"bridge_version": BRIDGE_VERSION, "code": WRITE_OK, "written": True,
                "path": str(path), "previous_semantic_hash": current_hash,
                "semantic_hash": report["semantic_hash"],
                "stripped_visual_fields": report["stripped_visual_fields"],
                "projection": report["projection"]}

    def create_workflow(self, workflow_id: str, candidate: Mapping[str, Any]) -> dict[str, Any]:
        """Author a workflow that does not exist yet.

        AAW CANVAS FUNCTIONALIZATION V0.1 closes gap **B10** for creation only.
        It is `save_workflow(allow_create=True)` with one extra refusal — an
        existing file is never silently replaced by a create — so a canvas
        starting from an empty graph goes through exactly the same validation
        and atomic write as every edit. There is still no delete: the canvas
        authors workflows, it does not manage a library.
        """
        try:
            self.workflow_path(workflow_id)
        except BridgeError:
            pass
        else:
            raise BridgeError(WRITE_ALREADY_EXISTS,
                              f"workflow {workflow_id!r} already exists; open it instead")
        return self.save_workflow(workflow_id, candidate, allow_create=True)

    def blank_workflow(self, workflow_id: str) -> dict[str, Any]:
        """The smallest candidate the real validator accepts, for a new canvas.

        Not a template library: one node, because `validate_workflow` requires
        a non-empty `nodes` array and a `HUMAN_GATE`, and a canvas that opened
        onto a graph its own bridge would refuse would be lying about where
        the user is. Every field here is one the schema demands.
        """
        return {
            "workflow_id": str(workflow_id), "version": "0.1",
            "description": f"{workflow_id} — authored on the AAW canvas",
            "goal": None, "start_node": "N01",
            "routing_contract": routing_contract.CONTRACT_VERSION,
            "workspace_policy": {"isolated_worktree_required": True, "main_merge_allowed": False},
            "limits": {"max_nodes": 24, "max_repair_cycles": 2,
                       "max_wall_time_minutes": 240, "max_llm_calls": 24,
                       "max_token_budget": None},
            "nodes": [{
                "id": "N01", "type": "HUMAN_GATE", "depends_on": [], "run_if": "ON_TRANSITION",
                "role": None, "model": None, "effort": None,
                "instructions": "Human acceptance. The runner never merges, pushes or opens a PR.",
                "acceptance": ["A human verdict is recorded against the candidate."],
                "on_pass": None, "on_fail": None,
            }],
        }

    # ── layout (visual only, outside workflow semantics) ──────────────

    def load_layout(self, workflow_id: str) -> dict[str, Any]:
        """Positions for every node in the graph, stored ones taking priority.

        A stored layout is authored against one version of a graph; a node
        added since is simply absent from it. Merging over the automatic layout
        means a partial layout leaves one node in a default place, instead of
        collapsing every unlisted node onto the origin.
        """
        stored = workflow_layout.load_layout(self.workflows_root, workflow_id)
        try:
            auto = workflow_layout.auto_layout(self.graph_projection(workflow_id))
        except (BridgeError, WorkflowValidationError, OSError, json.JSONDecodeError):
            return stored  # a graph that will not project cannot be auto-arranged
        if not stored.get("nodes"):
            return auto
        merged = dict(auto)
        merged.update({key: value for key, value in stored.items() if key != "nodes"})
        nodes = dict(auto["nodes"])
        missing = []
        for node_id, row in stored["nodes"].items():
            nodes[node_id] = dict(row)
        for node_id in auto["nodes"]:
            if node_id not in stored["nodes"]:
                missing.append(node_id)
        merged["nodes"] = nodes
        merged["source"] = "STORED" if not missing else "STORED_PLUS_AUTO"
        merged["auto_placed"] = missing
        return merged

    def save_layout(self, workflow_id: str, layout: Mapping[str, Any]) -> dict[str, Any]:
        """Persist positions. Cannot alter workflow semantics: the layout store
        writes only under `WORKFLOWS/LAYOUTS/` and stores only numbers and
        booleans. The current semantic hash is recorded as provenance so the
        UX can say "arranged against an older graph"."""
        semantic = None
        try:
            semantic = self.graph_projection(workflow_id)["semantic_hash"]
        except (BridgeError, WorkflowValidationError, OSError, json.JSONDecodeError):
            pass  # a layout for a workflow that will not load is still the user's layout
        return workflow_layout.save_layout(self.workflows_root, workflow_id, layout,
                                           workflow_semantic_hash=semantic)

    # ══════════════════════════════ RUN ══════════════════════════════

    def start_run(self, workflow_id: str, *, goal: str, repo: str | Path, worktree: str | Path,
                  overrides: Mapping[str, str] | None = None, preprocess_policy: str = "AUTO_SAFE",
                  adapter: Callable[..., Any] | None = None,
                  resume_from: Mapping[str, Any] | None = None,
                  on_settled: Callable[[RunHandle], None] | None = None) -> dict[str, Any]:
        """Start one real run and return its handle immediately.

        The run id is minted here rather than inside the runner so the UX can
        address the run, subscribe to its journal and cancel it before the
        first node has produced anything.

        `adapter` substitutes the documented LLM adapter boundary for this run
        only, and only in this process. It exists so a slice can run without
        paid provider calls while routing, gates, lineage, state and artifacts
        stay entirely real. It cannot substitute routing, gates or the runner.

        `resume_from={"source_run_id": ..., "from_node": ...}` is `Run from
        here`. The plan is built and every precondition checked **before** a
        run id is minted, so a refused resume leaves no run and no artifact
        behind; see `workflow_runner.plan_resume` for the closed set of
        refusals. The source run is read, never reopened or mutated.
        """
        path = self.workflow_path(workflow_id)
        plan = None
        if resume_from is not None:
            plan = self.plan_resume(workflow_id, str(resume_from.get("source_run_id") or ""),
                                    str(resume_from.get("from_node") or ""),
                                    worktree=worktree)["plan"]
        worktree_key = str(Path(worktree).resolve()).casefold()
        with self._lock:
            owner = self._active_worktrees.get(worktree_key)
            if owner:
                raise BridgeError("WORKTREE_IN_USE",
                                  f"worktree is already owned by active run {owner}")
            identifier = str(self.runner.run_id())
            self._active_worktrees[worktree_key] = identifier
        try:
            worktree_lease = run_recovery.acquire_worktree_lease(Path(worktree), identifier)
        except run_recovery.RecoveryError as exc:
            with self._lock:
                if self._active_worktrees.get(worktree_key) == identifier:
                    self._active_worktrees.pop(worktree_key, None)
            raise BridgeError(exc.code, str(exc)) from exc
        state_path = self.stats_root / identifier / "WORKFLOW" / "workflow_state.json"
        journal_path = self.stats_root / identifier / "WORKFLOW" / "ROUTING" / "routing_events.jsonl"
        handle = RunHandle(identifier, workflow_id=str(workflow_id), goal=str(goal),
                           state_path=state_path, journal_path=journal_path)
        handle.resumed_from = dict(resume_from) if plan is not None else None
        handle.worktree = Path(worktree)

        def worker() -> None:
            try:
                # Both scopes are context-local (`ContextVar`), so they reach
                # this run's call stack and no other. Two adapter-backed runs
                # in one process no longer interfere — V0.1 gap **B1**.
                with run_cancellation.cancellation_scope(handle.cancel),                         self.runner.llm_adapter_scope(adapter):
                    state = self.runner.execute(
                        path, goal, Path(repo), Path(worktree), overrides,
                        preprocess_policy, identifier=identifier, cancel=handle.cancel,
                        resume=plan)
                handle._final_status = str(state.get("status"))
            except BaseException as exc:  # a supervised run must never kill the server
                handle.error = f"{type(exc).__name__}: {exc}"
                handle._final_status = "RUNNER_ERROR"
            finally:
                handle.finished_at = _now()
                # Freeze ownership immediately when the run stops moving.
                # Later cleanup is allowed only if the tree still matches this
                # exact path/content snapshot.
                try:
                    self._seal_recovery(handle)
                except Exception:
                    # Recovery evidence must never replace the runner's own
                    # final status. A missing manifest simply fails closed.
                    pass
                with self._lock:
                    if self._active_worktrees.get(worktree_key) == handle.run_id:
                        self._active_worktrees.pop(worktree_key, None)
                try:
                    run_recovery.release_worktree_lease(worktree_lease, handle.run_id)
                except run_recovery.RecoveryError:
                    # A changed lease means another owner may be involved. Do
                    # not delete it; the recovery manifest already fails closed
                    # if the worktree itself no longer matches.
                    pass
                if on_settled is not None:
                    try:
                        on_settled(handle)
                    except Exception:
                        pass

        try:
            with self._lock:
                self._runs[identifier] = handle
            handle.thread = threading.Thread(target=worker, name=f"aaw-run-{identifier}", daemon=True)
            handle.thread.start()
        except BaseException:
            with self._lock:
                self._runs.pop(identifier, None)
                if self._active_worktrees.get(worktree_key) == identifier:
                    self._active_worktrees.pop(worktree_key, None)
            try:
                run_recovery.release_worktree_lease(worktree_lease, identifier)
            except run_recovery.RecoveryError:
                pass
            raise
        return handle.describe()

    def list_runs(self) -> dict[str, Any]:
        with self._lock:
            handles = list(self._runs.values())
        return {"bridge_version": BRIDGE_VERSION,
                "runs": [handle.describe() for handle in handles]}

    def _handle(self, run_id: str) -> RunHandle:
        with self._lock:
            handle = self._runs.get(str(run_id))
        if handle is None:
            raise BridgeError("UNKNOWN_RUN", f"unknown run {run_id!r}")
        return handle

    def run_state(self, run_id: str) -> dict[str, Any]:
        """The durable run state as written by the runner. Read-only.

        Cached on the file's own modification stamp. Not an optimisation: a
        canvas polls this several times a second, and on Windows every open
        handle on the destination is a chance to refuse the runner's next
        `os.replace`. Holding the file open as rarely as the data actually
        changes keeps a reader from interfering with the run it is watching.
        """
        handle = self._handle(run_id)
        try:
            stamp = handle.state_path.stat().st_mtime_ns, handle.state_path.stat().st_size
        except OSError:
            return {}
        if handle.state_cache is not None and handle.state_stamp == stamp:
            return handle.state_cache
        try:
            state = json.loads(handle.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A replace observed mid-flight, or a momentary sharing refusal.
            # The last whole document we saw is still true-as-of-then; a stale
            # frame is correct, an empty one would look like a vanished run.
            return handle.state_cache or {}
        handle.state_cache, handle.state_stamp = state, stamp
        return state

    def run_projection(self, run_id: str, *, include_events: bool = False) -> dict[str, Any]:
        """The whole canvas frame for one run: static graph + runtime overlay.

        Assembled entirely from durable artifacts. Static topology comes from
        the workflow file, runtime facts from `workflow_state.json` and the
        routing journal, and nodes the runtime minted come from
        `routing.minted_nodes` — so a repair branch is drawable the moment the
        gate creates it, not only once it completes.
        """
        handle = self._handle(run_id)
        state = self.run_state(run_id)
        journal = RoutingJournal(handle.journal_path, run_id=handle.run_id,
                                 workflow_id=handle.workflow_id)
        runtime = routing_contract.graph_projection(state, journal) if state else {
            "status": None, "frontier": [], "nodes": [], "minted_nodes": [],
            "routing": {}, "events": [], "last_sequence": 0,
        }
        events = runtime.pop("events", [])
        try:
            static = self.graph_projection(handle.workflow_id)
        except (BridgeError, WorkflowValidationError) as exc:
            static = {"error": str(exc), "nodes": [], "edges": []}
        layout = self.load_layout(handle.workflow_id) if not static.get("error") else {"nodes": {}}
        frame = {
            "bridge_version": BRIDGE_VERSION,
            "run": handle.describe(),
            "graph": static,
            "layout": _layout_with_minted(layout, runtime.get("minted_nodes") or []),
            "runtime": runtime,
            "last_sequence": int(runtime.get("last_sequence") or 0),
        }
        if include_events:
            frame["events"] = events
        return frame

    def events(self, run_id: str, *, since: int = 0, limit: int | None = None) -> dict[str, Any]:
        """Routing/runtime events after `since`, in sequence order.

        `since` is the journal's own monotonically increasing `sequence`, so a
        consumer that stores the last sequence it rendered can reconnect and
        receive exactly the tail it missed. It never has to replay the run.
        """
        handle = self._handle(run_id)
        journal = RoutingJournal(handle.journal_path, run_id=handle.run_id,
                                 workflow_id=handle.workflow_id)
        rows = [row for row in journal.read() if int(row.get("sequence") or 0) > int(since)]
        truncated = bool(limit is not None and len(rows) > int(limit))
        if truncated:
            rows = rows[: int(limit)]
        last = int(rows[-1]["sequence"]) if rows else int(since)
        return {
            "bridge_version": BRIDGE_VERSION, "run_id": handle.run_id,
            "since": int(since), "last_sequence": last, "count": len(rows),
            "truncated": truncated, "lifecycle": handle.lifecycle,
            "runner_status": self.run_state(run_id).get("status"),
            "events": rows,
        }

    def cancel_run(self, run_id: str, *, reason: str = "stop requested from UX",
                   join_timeout: float = 0.0) -> dict[str, Any]:
        """Really stop a run.

        This terminates the child process the run is currently waiting on and
        raises at the runner's next frontier boundary. It is idempotent and
        reports what it actually did, including each process it signalled, so
        the UX can show a stop that happened rather than a stop it hopes for.

        Not interrupted, and reported as such: provider-side work already
        dispatched, and worktree edits a killed child had already made.
        """
        handle = self._handle(run_id)
        if handle.adopted:
            # Do not pretend. Another process owns the thread and the children;
            # a flag set here would stop nothing and the UX would show a lie.
            return {"bridge_version": BRIDGE_VERSION, "run_id": handle.run_id,
                    "cancelled": False,
                    "reason": "this run was started by another process; only its owner can stop it",
                    "runner_status": self.run_state(run_id).get("status"),
                    "run": handle.describe()}
        if handle.lifecycle == RUN_SETTLED:
            return {"bridge_version": BRIDGE_VERSION, "run_id": handle.run_id,
                    "cancelled": False, "reason": "run already settled",
                    "runner_status": handle._final_status, "run": handle.describe()}
        effect = handle.cancel.request(reason)
        if join_timeout and handle.thread is not None:
            handle.thread.join(timeout=float(join_timeout))
        # §4 cancellation truthfulness. A stop that leaves half an
        # implementation on the branch must say so with a measurement, not a
        # disclaimer, so the worktree is probed here rather than described.
        # Measured immediately: the runner is unwinding and writes nothing
        # more to the tree, so this is the state the stop left behind.
        try:
            worktree = self.run_worktree(run_id)
        except Exception as exc:
            worktree = {"work_state": WORK_UNKNOWN, "changed_files": [],
                        "error": f"{type(exc).__name__}: {exc}"}
        return {"bridge_version": BRIDGE_VERSION, "run_id": handle.run_id,
                "cancelled": True, "effect": effect,
                "work_state": worktree.get("work_state"),
                "worktree": worktree,
                "rollback_available": False,
                "cleanup_available": bool(worktree.get("cleanup_available")),
                "not_interrupted": [
                    "provider-side work already dispatched may still complete and may still bill",
                    "worktree edits made before the child was terminated are left in place",
                ],
                "run": handle.describe()}

    # ── run from here / reset downstream (§2) ─────────────────────────

    def _definition(self, workflow_id: str) -> dict[str, Any]:
        return json.loads(self.workflow_path(workflow_id).read_text(encoding="utf-8"))

    def plan_resume(self, workflow_id: str, source_run_id: str, from_node: str, *,
                    worktree: str | Path | None = None) -> dict[str, Any]:
        """What a `Run from here` would inherit and replace. Starts nothing.

        The whole decision lives in `workflow_runner.plan_resume`, which is
        pure: the same call the run itself makes. Exposing it separately means
        the canvas can show the user exactly which nodes are about to be
        re-executed *before* committing, and a refusal costs nothing.
        """
        workflow = self._definition(workflow_id)
        source = self._source_state(str(source_run_id))
        try:
            plan = self.runner.plan_resume(workflow, source, str(from_node),
                                           worktree=Path(worktree) if worktree else None)
        except self.runner.ResumeRefused as exc:
            raise BridgeError(exc.code, str(exc)) from exc
        except WorkflowValidationError as exc:
            raise BridgeError(WRITE_SCHEMA_INVALID, str(exc)) from exc
        return {"bridge_version": BRIDGE_VERSION, "workflow_id": str(workflow_id),
                "resumable": True, "plan": plan,
                # The plan carries whole records; the canvas only needs ids to
                # paint the two sets, so the summary is what a UX renders.
                "summary": {"from_node": plan["from_node"],
                            "source_run_id": plan["source_run_id"],
                            "reset_nodes": plan["reset_nodes"],
                            "inherited_nodes": plan["inherited_nodes"],
                            "llm_calls_inherited": plan["llm_calls"]}}

    def _source_state(self, run_id: str) -> dict[str, Any]:
        """Read a settled run's state, whether or not this bridge started it."""
        try:
            return self.run_state(run_id)
        except BridgeError:
            pass
        path = self.stats_root / str(run_id) / "WORKFLOW" / "workflow_state.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BridgeError(self.runner.RESUME_UNKNOWN_SOURCE_RUN,
                              f"no readable run state at {path}") from exc

    def reset_downstream(self, run_id: str, node_id: str) -> dict[str, Any]:
        """Say precisely what a re-run of `node_id` replaces — and reset nothing.

        AAW CANVAS FUNCTIONALIZATION V0.1 §2 asks what `Reset downstream`
        resets. The honest answer, given what exists today, is: **no durable
        artifact at all.**

          * The cone (`routing_contract.downstream_cone`) is what the next
            `Run from here` will re-execute. Those nodes' results are
            superseded by the new run; they are not deleted, because a
            completed execution is evidence and the ledger is append-only.
          * The canvas drops its runtime overlay for exactly those nodes, so
            the graph stops showing a verdict the next run is about to
            replace. That is UI state, and it is the only thing this clears.
          * The worktree is **not** touched. Neither are artifacts, execution
            descriptors, the ledger, the routing journal or any git state.

        Rolling those back is destructive cleanup, which this iteration
        explicitly does not implement, so the operation reports what it left
        alone rather than implying a rollback that does not exist.
        """
        handle = self._handle(run_id)
        workflow = self._definition(handle.workflow_id)
        state = self.run_state(run_id)
        minted = dict((state.get("routing") or {}).get("minted_nodes") or {})
        try:
            cone = routing_contract.downstream_cone(workflow, str(node_id), minted=minted)
        except routing_contract.RoutingContractError as exc:
            raise BridgeError("UNKNOWN_NODE", str(exc)) from exc
        completed = {str(row.get("node_id")) for row in state.get("completed_nodes") or []}
        return {
            "bridge_version": BRIDGE_VERSION, "run_id": handle.run_id,
            "from_node": str(node_id),
            "cleared_ui_state": sorted(set(cone["nodes"]) & completed),
            "cone": cone,
            "durable_changes": [],
            "retained": [
                "worktree contents and every uncommitted edit in it",
                "node result artifacts of the cleared nodes",
                "execution descriptors and the execution ledger (append-only)",
                "the routing journal of this run (append-only)",
                "runtime-minted repair lineage already recorded in run state",
            ],
            "next_step": (f"Run from here at {node_id} re-executes "
                          f"{len(cone['nodes'])} node(s) in the same worktree"),
        }

    # ── worktree truthfulness (§4) ────────────────────────────────────

    @staticmethod
    def _recovery_path(handle: RunHandle) -> Path:
        return handle.state_path.parent / "recovery_ownership.json"

    def _seal_recovery(self, handle: RunHandle) -> dict[str, Any]:
        path = self._recovery_path(handle)
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
        state = self.run_state(handle.run_id)
        baseline = state.get("workspace_baseline") or {}
        worktree = state.get("worktree") or (str(handle.worktree) if handle.worktree else None)
        if not worktree:
            manifest = {
                "schema_version": run_recovery.SCHEMA_VERSION,
                "originating_run_id": handle.run_id,
                "runner_status": state.get("status") or handle._final_status,
                "worktree": None, "baseline_head": None, "baseline_status": None,
                "settled_snapshot": {"worktree": None, "head": None, "entries": []},
                "ownership": run_recovery.AMBIGUOUS,
                "mixed_change_possible": True,
                "proof_reasons": ["this run names no worktree"],
                "resolution": run_recovery.UNRESOLVED,
            }
        else:
            manifest = run_recovery.freeze_manifest(
                run_id=handle.run_id, worktree=Path(worktree),
                baseline_head=baseline.get("worktree_head"),
                baseline_status=baseline.get("worktree_status"),
                runner_status=state.get("status") or handle._final_status,
                authorized_baseline=baseline.get("authorized_workspace_baseline"))
        self.runner.atomic_json(path, manifest)
        return manifest

    def _load_recovery(self, run_id: str) -> tuple[RunHandle, Path, dict[str, Any]]:
        handle = self._handle(run_id)
        path = self._recovery_path(handle)
        if not path.is_file():
            if handle.lifecycle != RUN_SETTLED:
                raise BridgeError("RUN_STILL_ACTIVE", "worktree ownership is frozen only after the run settles")
            try:
                self._seal_recovery(handle)
            except Exception as exc:
                raise BridgeError("OWNERSHIP_UNAVAILABLE", f"cannot freeze run ownership: {exc}") from exc
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BridgeError("OWNERSHIP_UNAVAILABLE", f"cannot read {path}: {exc}") from exc
        if str(manifest.get("originating_run_id")) != handle.run_id:
            raise BridgeError("OWNERSHIP_MISMATCH", "recovery manifest belongs to another run")
        return handle, path, manifest

    def run_worktree(self, run_id: str) -> dict[str, Any]:
        """What is physically in this run's worktree right now.

        A cancelled run may have left edits behind — the runner never reverts,
        exactly as it never merges. Nothing else in the system measures that,
        so the UX had no way to distinguish "stopped cleanly" from "stopped
        holding half an implementation". This measures it with the runner's
        own `git status` helper, in the worktree the run recorded.

        `work_state` is a closed token, not a sentence: `PARTIAL_WORK_PRESENT`
        / `NO_PARTIAL_WORK` / `PARTIAL_WORK_UNKNOWN`.
        """
        handle = self._handle(run_id)
        state = self.run_state(run_id)
        recovery_path = self._recovery_path(handle)
        if handle.lifecycle == RUN_SETTLED and not recovery_path.is_file():
            try:
                self._seal_recovery(handle)
            except Exception:
                pass
        if recovery_path.is_file():
            try:
                manifest = json.loads(recovery_path.read_text(encoding="utf-8"))
                inspected = run_recovery.inspect_manifest(manifest)
                adopted_baseline = inspected.get("adopted_baseline") or {}
                baseline_evidence = adopted_baseline or inspected.get("authorized_baseline") or {}
                return {
                    "bridge_version": BRIDGE_VERSION, "run_id": handle.run_id,
                    "originating_run_id": inspected["originating_run_id"],
                    "worktree": inspected["worktree"],
                    "runner_status": inspected.get("runner_status") or state.get("status"),
                    "work_state": WORK_PARTIAL if inspected["changed_files"] else WORK_CLEAN,
                    "changed_files": inspected["changed_files"],
                    "changes": inspected.get("changes") or [],
                    "modified_files": inspected["modified_files"],
                    "untracked_files": inspected["untracked_files"],
                    "head": (inspected.get("current_snapshot") or {}).get("head"),
                    "baseline_head": inspected.get("baseline_head"),
                    "baseline_id": (adopted_baseline.get("baseline_id") or
                                    inspected.get("baseline_id")),
                    "baseline_workspace_tree": (adopted_baseline.get("workspace_tree") or
                                                inspected.get("baseline_workspace_tree")),
                    "baseline_state": baseline_evidence.get("state"),
                    "baseline_fingerprint": baseline_evidence.get("fingerprint"),
                    "baseline_provenance": ({
                        "originating_run_id": baseline_evidence.get("originating_run_id"),
                        "adopted_at": baseline_evidence.get("adopted_at"),
                        "manifest_path": baseline_evidence.get("manifest_path"),
                    } if baseline_evidence else None),
                    "authorized_baseline": inspected.get("authorized_baseline"),
                    "committed_since_baseline": ((inspected.get("current_snapshot") or {}).get("head")
                                                  != inspected.get("baseline_head")),
                    "ownership": inspected["ownership"],
                    "mixed_change_possible": inspected["mixed_change_possible"],
                    "proof_reasons": inspected["proof_reasons"],
                    "cleanup_available": inspected["cleanup_available"],
                    "resolution": inspected["resolution"],
                    "ownership_manifest": str(recovery_path),
                    # Kept for V0.1 consumers. V0.2's explicit action is named
                    # cleanup_available; there is still no generic rollback.
                    "rollback_available": False,
                    "error": None,
                    "cancellation": state.get("cancellation"),
                }
            except (OSError, json.JSONDecodeError, run_recovery.RecoveryError) as exc:
                # Continue into the legacy read-only probe, but fail closed.
                recovery_error = f"{type(exc).__name__}: {exc}"
        else:
            recovery_error = None
        worktree = state.get("worktree") or (str(handle.worktree) if handle.worktree else None)
        frame: dict[str, Any] = {
            "bridge_version": BRIDGE_VERSION, "run_id": handle.run_id,
            "worktree": worktree,
            "runner_status": state.get("status") or handle._final_status,
            "work_state": WORK_UNKNOWN, "changed_files": [], "head": None,
            "baseline_head": (state.get("workspace_baseline") or {}).get("worktree_head"),
            "error": recovery_error, "rollback_available": False,
            "originating_run_id": handle.run_id,
            "modified_files": [], "untracked_files": [],
            "ownership": run_recovery.AMBIGUOUS,
            "mixed_change_possible": True, "proof_reasons": [],
            "cleanup_available": False, "resolution": RECOVERY_UNRESOLVED,
            "cancellation": state.get("cancellation"),
        }
        if not worktree:
            frame["error"] = "this run names no worktree"
            return frame
        try:
            frame["changed_files"] = self.runner.changed_files(Path(worktree))
            frame["head"] = self.runner.git(Path(worktree), "rev-parse", "HEAD").strip()
        except Exception as exc:  # a missing or moved worktree is a fact, not a crash
            frame["error"] = f"{type(exc).__name__}: {exc}"
            return frame
        frame["work_state"] = WORK_PARTIAL if frame["changed_files"] else WORK_CLEAN
        frame["modified_files"] = list(frame["changed_files"])
        frame["committed_since_baseline"] = bool(
            frame["baseline_head"] and frame["head"] and frame["head"] != frame["baseline_head"])
        frame["proof_reasons"] = ["run ownership was not frozen when the run settled"]
        return frame

    def keep_run_changes(self, run_id: str) -> dict[str, Any]:
        """Transfer the frozen edits to the operator without touching Git."""
        _handle, path, manifest = self._load_recovery(run_id)
        if manifest.get("resolution") == RECOVERY_DISCARDED:
            raise BridgeError("RECOVERY_ALREADY_RESOLVED", "run-owned changes were already discarded")
        manifest["resolution"] = RECOVERY_KEPT
        manifest["resolved_at"] = _now()
        self.runner.atomic_json(path, manifest)
        report = self.run_worktree(run_id)
        report["kept"] = True
        report["note"] = "files were left untouched; a dirty worktree still cannot start a run"
        return report

    def discard_run_changes(self, run_id: str) -> dict[str, Any]:
        """Discard exact run-owned paths; ambiguous ownership is a refusal."""
        _handle, path, manifest = self._load_recovery(run_id)
        if manifest.get("resolution") != RECOVERY_UNRESOLVED:
            raise BridgeError("RECOVERY_ALREADY_RESOLVED",
                              f"recovery was already resolved as {manifest.get('resolution')}")
        try:
            result = run_recovery.discard_owned_changes(manifest)
        except run_recovery.RecoveryError as exc:
            raise BridgeError(exc.code, str(exc)) from exc
        result["resolved_at"] = _now()
        self.runner.atomic_json(path, result)
        report = self.run_worktree(run_id)
        report["discarded"] = True
        report["discarded_modified_files"] = result.get("discarded_modified_files") or []
        report["discarded_untracked_files"] = result.get("discarded_untracked_files") or []
        return report

    def adopt_run_changes_as_baseline(self, run_id: str) -> dict[str, Any]:
        """Authorize the exact run-owned workspace without moving Git history."""
        _handle, path, manifest = self._load_recovery(run_id)
        try:
            baseline = run_recovery.adopt_manifest_as_baseline(manifest, recovery_path=path)
        except run_recovery.RecoveryError as exc:
            raise BridgeError(exc.code, str(exc)) from exc
        manifest["resolution"] = RECOVERY_ADOPTED
        manifest["resolved_at"] = _now()
        manifest["adopted_baseline"] = {
            key: baseline.get(key) for key in (
                "schema_version", "state", "baseline_id", "worktree", "head",
                "workspace_tree", "index_tree", "originating_run_id", "adopted_at",
                "manifest_path", "fingerprint")
        }
        self.runner.atomic_json(path, manifest)
        report = self.run_worktree(run_id)
        report["adopted"] = True
        report["baseline_state"] = baseline["state"]
        report["baseline_id"] = baseline["baseline_id"]
        report["baseline_fingerprint"] = baseline["fingerprint"]
        report["baseline_provenance"] = {
            "originating_run_id": baseline["originating_run_id"],
            "adopted_at": baseline["adopted_at"],
            "manifest_path": baseline["manifest_path"],
        }
        return report

    # ── structured node detail (§3) ───────────────────────────────────

    def node_detail(self, run_id: str, node_id: str) -> dict[str, Any]:
        """Everything the inspector shows about one node, as structured facts.

        Assembled from artifacts some runtime component already wrote — the
        run state, the node result artifact, the telemetry row, the gate
        decision artifact, the execution descriptor reference. Nothing here is
        recovered from a prose log, and nothing is computed from a summary.

        The split the objective asks for is expressed in the response itself:
        `primary` is the work (state, brief, verdict, summary, inherited
        brief, carry_forward, artifacts) and `secondary` is the machinery
        (binding, effort, execution id, hashes, timings, telemetry). The
        canvas renders the split; it does not invent it.
        """
        handle = self._handle(run_id)
        state = self.run_state(run_id)
        node_id = str(node_id)

        record = next((dict(row) for row in reversed(state.get("completed_nodes") or [])
                       if str(row.get("node_id")) == node_id), None)
        result = next((dict(row) for row in reversed(state.get("node_results") or [])
                       if str(row.get("node_id")) == node_id), None)
        telemetry = next((dict(row) for row in reversed(state.get("telemetry") or [])
                          if str(row.get("node")) == node_id), None)
        execution = next((dict(row) for row in reversed(state.get("executions") or [])
                          if str(row.get("node_id")) == node_id), None)
        decision_ref = next((dict(row) for row in reversed((state.get("routing") or {}).get("decisions") or [])
                             if str(row.get("node_id")) == node_id), None)
        decision = None
        if decision_ref and decision_ref.get("artifact"):
            try:
                decision = json.loads(Path(decision_ref["artifact"]).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                decision = None  # the reference stays; the detail is simply unavailable

        minted = ((state.get("routing") or {}).get("minted_nodes") or {}).get(node_id)
        lineage = (record or {}).get("lineage") or (minted or {}).get("lineage")
        declared = None
        try:
            declared = next((row for row in self.graph_projection(handle.workflow_id)["nodes"]
                             if str(row["node_id"]) == node_id), None)
        except (BridgeError, WorkflowValidationError):
            pass
        static = minted or declared or {}

        frontier = [str(item) for item in (state.get("frontier") or [])]
        if record is not None:
            node_state = "COMPLETED"
        elif str(state.get("current_node")) == node_id and state.get("status") == "RUNNING":
            node_state = "RUNNING"
        elif node_id in frontier:
            node_state = "QUEUED"
        else:
            node_state = "PENDING"

        return {
            "bridge_version": BRIDGE_VERSION, "run_id": handle.run_id, "node_id": node_id,
            "node_kind": (routing_contract.MINTED_REPAIR_BRANCH if minted
                          else routing_contract.DECLARED_NODE),
            "primary": {
                "state": node_state,
                "node_type": static.get("node_type") or (record or {}).get("node_type"),
                "role": static.get("role"),
                "instructions": static.get("instructions"),
                "acceptance": list(static.get("acceptance") or []),
                "outcome": (record or {}).get("outcome"),
                "verdict": (record or {}).get("verdict"),
                "summary": (result or {}).get("summary"),
                "next_brief": (result or {}).get("next_brief"),
                "inherited_next_brief": (lineage or {}).get("inherited_brief"),
                "carry_forward": list((result or {}).get("carry_forward")
                                      or (lineage or {}).get("carry_forward")
                                      or static.get("carry_forward") or []),
                "artifacts": list((result or {}).get("artifacts") or []),
                "changed_files": list((result or {}).get("changed_files") or []),
                "findings": list((result or {}).get("findings") or []),
                "tests": list((result or {}).get("tests") or []),
                "remaining_uncertainty": list((result or {}).get("remaining_uncertainty") or []),
                "lineage": lineage,
            },
            "secondary": {
                "model": (record or {}).get("model") or static.get("model"),
                "capability": static.get("capability"),
                "effort": (record or {}).get("effort") or static.get("effort"),
                "harness": (record or {}).get("harness"),
                "implementer_profile": (record or {}).get("implementer_profile"),
                "execution_id": (record or {}).get("execution_id"),
                "execution": execution,
                "artifact": (record or {}).get("artifact"),
                "repair_cycle": (record or {}).get("repair_cycle"),
                "duration_s": (record or {}).get("duration_s"),
                "telemetry": telemetry,
                "usage": (telemetry or {}).get("usage"),
                "hashes": {
                    "decision_hash": (decision_ref or {}).get("decision_hash"),
                    "routing_input_hash": (decision_ref or {}).get("routing_input_hash"),
                    "workflow_semantic_hash": state.get("workflow_semantic_hash"),
                },
            },
            "gate": {"reference": decision_ref, "decision": decision},
        }

    def resolve_human_decision(self, run_id: str, verdict: str) -> dict[str, Any]:
        """Record the human verdict a HUMAN_GATE is waiting on.

        Thin pass-through to the existing `apply_human_verdict`, which owns the
        candidate rules and writes the immutable decision artifact. Included
        because the event vocabulary opens a gate the UX would otherwise have
        no way to close.
        """
        handle = self._handle(run_id)
        state = self.runner.apply_human_verdict(handle.run_id, str(verdict))
        handle._final_status = str(state.get("status"))
        return {"bridge_version": BRIDGE_VERSION, "run_id": handle.run_id,
                "runner_status": state.get("status"), "human_verdict": state.get("human_verdict")}

    def adopt_run(self, run_id: str, *, workflow_id: str, goal: str = "") -> dict[str, Any]:
        """Register a run this process did not start, for read-only inspection.

        A run started by the CLI is still a run; the canvas should be able to
        open it. An adopted run has no cancellation token and reports
        `SETTLED`: the bridge will not claim it can stop a run it does not own.
        """
        state_path = self.stats_root / str(run_id) / "WORKFLOW" / "workflow_state.json"
        if not state_path.is_file():
            raise BridgeError("UNKNOWN_RUN", f"no run state at {state_path}")
        handle = RunHandle(str(run_id), workflow_id=str(workflow_id), goal=str(goal),
                           state_path=state_path, adopted=True,
                           journal_path=state_path.parent / "ROUTING" / "routing_events.jsonl")
        try:
            handle._final_status = str(json.loads(state_path.read_text(encoding="utf-8")).get("status"))
        except (OSError, json.JSONDecodeError):
            handle._final_status = None
        with self._lock:
            self._runs[str(run_id)] = handle
        return handle.describe()


# ─────────────────────────────── helpers ───────────────────────────────

def strip_visual_fields(candidate: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Remove presentational keys from a candidate workflow, and say which.

    A canvas holds coordinates next to node data; without this, one careless
    round-trip would persist `x`/`y` into the workflow file and change its
    identity for a reason that has nothing to do with execution. Stripping is
    reported rather than silent so the UX can see that it happened.
    """
    cleaned = copy.deepcopy(dict(candidate))
    stripped: list[str] = []
    for key in routing_contract.VISUAL_FIELDS:
        if key in cleaned:
            cleaned.pop(key)
            stripped.append(key)
    for index, node in enumerate(cleaned.get("nodes") or []):
        if not isinstance(node, dict):
            continue
        for key in routing_contract.VISUAL_FIELDS:
            if key in node:
                node.pop(key)
                stripped.append(f"nodes[{index}].{key}")
    return cleaned, stripped


def _layout_with_minted(layout: Mapping[str, Any], minted: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Give runtime-minted nodes a position without storing one.

    A run must not write into a BUILD artifact, so a minted node's coordinates
    are computed per frame from its origin. If the user later arranges the
    branch, that is a normal layout save.
    """
    merged = {"nodes": dict(layout.get("nodes") or {}),
              "viewport": layout.get("viewport"),
              "workflow_semantic_hash": layout.get("workflow_semantic_hash"),
              "source": layout.get("source"), "exists": layout.get("exists", False)}
    for row in minted:
        node_id = str(row.get("node_id"))
        if not node_id or node_id in merged["nodes"]:
            continue
        lineage = row.get("lineage") or {}
        merged["nodes"][node_id] = {
            **workflow_layout.place_minted_node(
                merged, node_id, lineage.get("origin_node_id"),
                int(lineage.get("branch_index") or 1)),
            "minted": True,
        }
    return merged


# `_substituted_adapter` is gone. It rebound `workflow_runner.execute_llm_node`,
# a module attribute, so a substitution held for one run was installed for
# every run in the process — V0.1 gap **B1**. The seam is now
# `workflow_runner.llm_adapter_scope`, a `ContextVar` mirroring the
# cancellation token, and `start_run` enters it on the run's own thread.


_DEFAULT: AawBridge | None = None


def default_bridge() -> AawBridge:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = AawBridge()
    return _DEFAULT


def public_contract() -> dict[str, Any]:
    """Machine-readable description of this boundary.

    Served to the UI on connect so a canvas can refuse to run against a bridge
    it does not understand, instead of failing one field at a time.
    """
    return {
        "bridge_version": BRIDGE_VERSION,
        "routing_contract": routing_contract.CONTRACT_VERSION,
        "journal_schema": routing_contract.JOURNAL_SCHEMA_VERSION,
        "layout_schema": workflow_layout.LAYOUT_SCHEMA_VERSION,
        "build": ["list_workflows", "load_workflow", "graph_projection",
                  "validate_candidate", "save_workflow", "create_workflow", "blank_workflow",
                  "load_layout", "save_layout"],
        "run": ["start_run", "list_runs", "run_projection", "events", "cancel_run",
                "resolve_human_decision", "adopt_run", "plan_resume", "reset_downstream",
                "run_worktree", "keep_run_changes", "discard_run_changes",
                "adopt_run_changes_as_baseline", "node_detail"],
        "event_types": list(routing_contract.EVENT_TYPES),
        "hold_reasons": list(routing_contract.HOLD_REASONS),
        "edge_kinds": list(routing_contract.EDGE_KINDS),
        "verdicts": list(routing_contract.VERDICTS),
        "node_kinds": list(routing_contract.NODE_KINDS),
        "write_codes": [WRITE_OK, WRITE_SCHEMA_INVALID, WRITE_STALE,
                        WRITE_IDENTITY_MISMATCH, WRITE_UNKNOWN_WORKFLOW, WRITE_ALREADY_EXISTS],
        "resume_codes": list(workflow_runner.RESUME_CODES),
        "work_states": [WORK_CLEAN, WORK_PARTIAL, WORK_UNKNOWN],
        "recovery_resolutions": [RECOVERY_UNRESOLVED, RECOVERY_KEPT, RECOVERY_DISCARDED],
        "node_types": sorted(workflow_schema.NODE_TYPES),
        "predicate_keys": list(routing_contract.PREDICATE_KEYS),
        "outcomes": sorted(workflow_schema.OUTCOMES),
        "severity_ladder": list(routing_contract.SEVERITY_LADDER),
        "routing_modes": list(routing_contract.ROUTING_MODES),
        "terminals": list(routing_contract.TERMINALS),
        "lifecycles": [RUN_PENDING, RUN_ACTIVE, RUN_SETTLED],
        "deferred": ["planner graph mutation", "repair branch merge/rejoin", "routing DSL",
                     "advanced loops", "multi-user", "remote deployment", "telemetry UI",
                     "workflow delete"],
        # Recorded per AAW CANVAS FUNCTIONALIZATION V0.1 §5, on the wire so a
        # canvas cannot offer merge without seeing why it is absent.
        "merge_prerequisite": (
            "path-scoped carry_forward is a prerequisite for future branch merge/rejoin; "
            "accumulate_carry_forward is currently run-global, not per-lineage"),
    }
