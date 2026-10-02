#!/usr/bin/env python3
"""AAW AUTONOMOUS ITERATIONS V0.2 — the iteration controller.

Sits above the static workflow layer and adds the one thing it lacked: after
an iteration passes independent review the *controller* re-asks the planner
what is next, instead of handing control to a human. Decisions live in
`autonomy_contract`; this module owns state, evidence, persistence and resume.

State: `<STATS_ROOT>/<run_id>/AUTONOMY/autonomy_state.json` (atomic, rewritten
at every phase boundary). Audit stream: `autonomy_events.jsonl` next to it.

Why a separate JSONL stream rather than extra RoutingJournal event types:
`routing_contract.EVENT_TYPES` is a closed vocabulary that the (frozen) canvas
bridge asserts equal to its own contract list, and that stream models graph
semantics, not iteration lifecycle. Extending it would change a UI-facing
contract. This stream reuses the same shape (append-only, fsync, sequence).

V0.2 evidence boundary (see AAW_AUTONOMOUS_ITERATIONS_V0_2.md):

  * every role call is exactly one V0.4A execution: descriptor
    (`<run>/EXECUTIONS/<execution_id>.json`) → EXECUTION_INTENT → dispatch →
    EXECUTION_STARTED (only on a real spawn receipt) → EXECUTION_CLOSED, all in
    the V0.4B ledger (`<run>/LEDGER/execution_events.jsonl`) — the physical
    lifecycle authority;
  * `autonomy_events.jsonl` records logical controller transitions only and
    *references* the execution_id; it never states PID / started / closed
    facts of its own;
  * one controller per run (`autonomy_run_lock.RunLock`), acquired before any
    state is read for planning or execution.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import autonomy_contract as ac
import autonomy_run_lock as rl
import execution_contract as xc
import process_observation
from aaw_paths import AAW_ROOT, STATS_ROOT
from execution_ledger import ExecutionLedger, LedgerError, LifecycleRecorder

ROLES_PATH = AAW_ROOT / "AUTONOMY_ROLES.json"
PROFILES_PATH = AAW_ROOT / "IMPLEMENTER_PROFILES.json"


class ExecutorFailure(RuntimeError):
    """An executor reporting that it cannot continue (as opposed to crashing).

    Becomes an ESCALATE. Any *other* exception propagates and leaves the
    in-flight marker in the state, which is exactly what `resume` keys on.
    `code` lets an adapter name a more precise escalation (e.g. an unavailable
    role profile); `dispatched` says whether a provider was contacted.
    """

    def __init__(self, message: str, *, code: str | None = None, dispatched: bool | None = None) -> None:
        super().__init__(message)
        self.code, self.dispatched = code, dispatched


class RoleUnavailable(ExecutorFailure):
    """The configured profile for a role is not runnable now. Never substituted."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code=ac.E_ROLE_UNAVAILABLE, dispatched=False)


# ── persistence ──────────────────────────────────────────────────────────────

def _now() -> str:
    import datetime as dt
    return dt.datetime.now().astimezone().isoformat(timespec="milliseconds")


def _atomic_json(path: Path, data: Mapping[str, Any]) -> None:
    # Reuse the runner's hardened atomic writer (Windows replace-retry).
    import workflow_runner
    workflow_runner.atomic_json(path, data)


def autonomy_dir(run_id: str, stats_root: Path | None = None) -> Path:
    return Path(stats_root or STATS_ROOT) / run_id / "AUTONOMY"


class AutonomyJournal:
    """Append-only audit stream. Same shape as RoutingJournal, own vocabulary."""

    def __init__(self, path: Path, run_id: str) -> None:
        self.path, self.run_id, self._sequence = Path(path), run_id, 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self._sequence = sum(1 for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip())

    def append(self, event_type: str, *, iteration_id: str | None = None, phase: str | None = None,
               payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        self._sequence += 1
        event = {"schema_version": ac.JOURNAL_SCHEMA_VERSION, "contract": ac.CONTRACT_ID,
                 "event_id": "AEV_" + uuid.uuid4().hex, "sequence": self._sequence, "event_type": event_type,
                 "occurred_at": _now(), "run_id": self.run_id, "iteration_id": iteration_id, "phase": phase,
                 "payload": dict(payload or {})}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":"), default=str) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return event

    def read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def by_type(self, event_type: str) -> list[dict[str, Any]]:
        return [row for row in self.read() if row["event_type"] == event_type]


# ── workspace environment ────────────────────────────────────────────────────

class WorkspaceEnvironment:
    """What the controller may observe about the real repository.

    The controller never trusts an executor's account of the diff: the packet
    hash, the reviewer's raw material and every boundary check come from here.
    """

    def head(self) -> str | None: raise NotImplementedError
    def diff(self) -> str: raise NotImplementedError
    def changed_files(self) -> list[str]: raise NotImplementedError
    def commits(self) -> list[str]: return []
    def assert_safe(self) -> None:
        """Raise `GitPolicyViolation` if the Git boundary was crossed."""
    def describe(self) -> dict[str, Any]: return {}
    def checkpoint(self) -> dict[str, Any]:
        """Durable boundary snapshot, persisted in the run state."""
        return {}
    def restore(self, checkpoint: Mapping[str, Any]) -> None:
        """Re-adopt a persisted snapshot on resume; raise GitPolicyViolation if it no longer holds."""


class GitWorkspaceEnvironment(WorkspaceEnvironment):
    """The real thing, built on the existing runner guards (not re-implemented).

    Reuses `validate_workspace` (isolated registered worktree, clean canonical
    checkout, same git common dir) and `assert_main_unchanged`, and checks at
    every phase boundary (V0.2 contract — *detected at the next boundary*,
    not prevented, when another process does it):

      * protected local refs and protected remote-tracking refs are unchanged
        (snapshot of `refs/heads/<p>` and `refs/remotes/*/<p>`);
      * the canonical checkout is still on the same symbolic ref and commit;
      * the worktree is not on a protected branch;
      * history observed at the previous boundary is an ancestor of the
        current worktree HEAD (catches rebase / reset / amend of observed
        commits);
      * optionally (`remote_check=True`) the *actual* remote protected refs,
        read with `git ls-remote`. Without it a push or force-push done from a
        different clone is NOT visible locally: the remote-tracking ref only
        moves on a fetch or on a push made from this repository.
    """

    def __init__(self, repo: Path, worktree: Path, *, remote_check: bool = False, remote: str = "origin",
                 resuming: bool = False) -> None:
        import workflow_runner as wr
        self._wr = wr
        try:
            # A resumed run's worktree legitimately holds the run's own
            # uncommitted work; the structural checks still apply, and the
            # persisted checkpoint (`restore`) re-establishes the baseline.
            self.baseline = (self._validate_for_resume(Path(repo), Path(worktree)) if resuming
                             else wr.validate_workspace(Path(repo), Path(worktree)))
        except wr.WorkflowStop as exc:
            raise ac.GitPolicyViolation(str(exc)) from exc
        self.worktree = wr.canonical(Path(worktree))
        self.repo = Path(self.baseline["repo"])
        self.remote, self.remote_check = remote, remote_check
        self._refs = self._protected_refs()
        self._canonical_symref = self._symref(self.repo)
        self._last_head = self.baseline["worktree_head"]
        self._remote_refs = self._ls_remote() if remote_check else None

    def _validate_for_resume(self, repo: Path, worktree: Path) -> dict[str, Any]:
        wr = self._wr
        repo_top = wr.canonical(Path(wr.git(repo, "rev-parse", "--show-toplevel")))
        wt_top = wr.canonical(Path(wr.git(worktree, "rev-parse", "--show-toplevel")))
        if repo_top == wt_top:
            raise wr.WorkflowStop("BLOCKED", "worktree=main/canonical checkout is forbidden")
        common = lambda top: wr.canonical(top / wr.git(top, "rev-parse", "--git-common-dir"))
        if common(repo_top) != common(wt_top):
            raise wr.WorkflowStop("BLOCKED", "repo and worktree do not belong to the same Git repository")
        registered = {wr.canonical(Path(line[9:])) for line in wr.git(repo_top, "worktree", "list", "--porcelain")
                      .splitlines() if line.startswith("worktree ")}
        if wt_top not in registered:
            raise wr.WorkflowStop("BLOCKED", "execution workspace is not a registered Git worktree")
        status = wr.git(repo_top, "status", "--porcelain=v1", "--untracked-files=all")
        if status:
            raise wr.WorkflowStop("BLOCKED", "canonical checkout has unexpected dirty state")
        return {"repo": str(repo_top), "worktree": str(wt_top), "git_common_dir": str(common(repo_top)),
                "main_head": wr.git(repo_top, "rev-parse", "HEAD"), "main_status": status,
                "worktree_head": wr.git(wt_top, "rev-parse", "HEAD")}

    def checkpoint(self) -> dict[str, Any]:
        return {"base_head": self.baseline["worktree_head"], "main_head": self.baseline["main_head"],
                "last_head": self._last_head, "canonical_symref": self._canonical_symref,
                "protected_refs": self._refs, "remote_refs": self._remote_refs, "remote_check": self.remote_check}

    def restore(self, checkpoint: Mapping[str, Any]) -> None:
        """Hold the resumed run to the baseline it started from, not to whatever is there now."""
        if not checkpoint:
            return
        current_main = self._wr.git(self.repo, "rev-parse", "HEAD")
        if current_main != checkpoint["main_head"]:
            raise ac.GitPolicyViolation("canonical HEAD changed while the controller was down")
        if self._refs != checkpoint["protected_refs"]:
            raise ac.GitPolicyViolation("a protected ref moved while the controller was down")
        if self._canonical_symref != checkpoint["canonical_symref"]:
            raise ac.GitPolicyViolation("canonical checkout switched branch while the controller was down")
        if self.remote_check and checkpoint.get("remote_refs") is not None and self._remote_refs != checkpoint["remote_refs"]:
            raise ac.GitPolicyViolation("remote protected refs changed while the controller was down")
        head = self.head()
        for label in ("base_head", "last_head"):
            rc, _, _ = self._wr.run_process(["git", "-C", str(self.worktree), "merge-base", "--is-ancestor",
                                             str(checkpoint[label]), str(head)])
            if rc != 0:
                raise ac.GitPolicyViolation(f"persisted {label} is no longer an ancestor of the worktree HEAD")
        self.baseline["worktree_head"] = checkpoint["base_head"]
        self._last_head = checkpoint["last_head"]
        if checkpoint.get("remote_refs") is not None:
            self._remote_refs = checkpoint["remote_refs"]

    def _protected_refs(self) -> str:
        patterns = [f"refs/heads/{b}" for b in ac.PROTECTED_BRANCHES] + \
                   [f"refs/remotes/*/{b}" for b in ac.PROTECTED_BRANCHES]
        return self._wr.git(self.repo, "for-each-ref", "--format=%(refname) %(objectname)", *patterns)

    def _symref(self, path: Path) -> str:
        rc, out, _ = self._wr.run_process(["git", "-C", str(path), "symbolic-ref", "-q", "HEAD"])
        return out.strip() if rc == 0 else "DETACHED"

    def _ls_remote(self) -> str:
        rc, out, err = self._wr.run_process(["git", "-C", str(self.repo), "ls-remote", self.remote,
                                             *[f"refs/heads/{b}" for b in ac.PROTECTED_BRANCHES]], timeout=60)
        if rc != 0:
            # Fail closed: a remote we were told to watch but cannot read is not "unchanged".
            raise ac.GitPolicyViolation(f"remote {self.remote!r} protected refs unreadable: {err.strip()[:300]}")
        return out.strip()

    def head(self) -> str | None:
        return self._wr.git(self.worktree, "rev-parse", "HEAD")

    def diff(self) -> str:
        """Everything the iteration produced: committed since the base, staged,
        unstaged and untracked. Local checkpoint commits are allowed, so a
        diff against HEAD alone would hide committed work from the reviewer."""
        base = self.baseline["worktree_head"]
        tracked = self._wr.git(self.worktree, "diff", "--no-ext-diff", "--binary", base)
        untracked = self._wr.git(self.worktree, "ls-files", "--others", "--exclude-standard").splitlines()
        chunks = [tracked]
        for rel in untracked:
            path = self.worktree / rel
            if path.is_file() and path.stat().st_size <= 200_000:
                try:
                    content = path.read_text(encoding="utf-8")
                except (OSError, UnicodeError):
                    content = "<binary or unreadable>"
                chunks.append(f"\n--- /dev/null\n+++ b/{rel}\n{content}")
        return "\n".join(chunk for chunk in chunks if chunk)[-500_000:]

    def changed_files(self) -> list[str]:
        # Name-only listings, not porcelain: `workflow_runner.git` strips its
        # output, which eats the leading status column of the first porcelain
        # line (" M calc.py" -> "alc.py" in `workflow_runner.changed_files`).
        base = self.baseline["worktree_head"]
        tracked = self._wr.git(self.worktree, "diff", "--name-only", base).splitlines()
        untracked = self._wr.git(self.worktree, "ls-files", "--others", "--exclude-standard").splitlines()
        return sorted(dict.fromkeys(f for f in [*tracked, *untracked] if f.strip()))

    def commits(self) -> list[str]:
        base = self.baseline["worktree_head"]
        out = self._wr.git(self.worktree, "log", "--format=%H %s", f"{base}..HEAD")
        return [line for line in out.splitlines() if line.strip()]

    def assert_safe(self) -> None:
        try:
            self._wr.assert_main_unchanged(self.baseline)
            if self._symref(self.repo) != self._canonical_symref:
                raise ac.GitPolicyViolation("canonical checkout switched branch")
            branch = self._wr.git(self.worktree, "rev-parse", "--abbrev-ref", "HEAD")
            if branch in ac.PROTECTED_BRANCHES:
                raise ac.GitPolicyViolation(f"worktree is on protected branch {branch!r}")
            if self._protected_refs() != self._refs:
                raise ac.GitPolicyViolation("a protected branch or its remote-tracking ref moved "
                                            "(merge, push from this repository, or fetch)")
            head = self.head()
            rc, _, _ = self._wr.run_process(["git", "-C", str(self.worktree), "merge-base", "--is-ancestor",
                                             str(self._last_head), str(head)])
            if rc != 0:
                raise ac.GitPolicyViolation(f"worktree history observed at the last boundary ({self._last_head[:12]}) "
                                            "is no longer an ancestor of HEAD (rebase, reset or amend)")
            if self.remote_check and self._ls_remote() != self._remote_refs:
                raise ac.GitPolicyViolation(f"protected refs on remote {self.remote!r} changed (push or force-push)")
            self._last_head = head
        except self._wr.WorkflowStop as exc:
            raise ac.GitPolicyViolation(str(exc)) from exc

    def describe(self) -> dict[str, Any]:
        return {"worktree": str(self.worktree), "base_head": self.baseline["worktree_head"],
                "repo": str(self.repo), "remote_check": self.remote_check}


def diff_digest(text: str) -> str:
    return ac.canonical_hash({"diff": text})


def new_iteration_id() -> str:
    """Stable, globally unique iteration identity. Not an index (a resumed run
    can re-plan), not an execution_id (an iteration holds many executions)."""
    return "ITER_" + uuid.uuid4().hex


def _write_once(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    try:
        os.link(temp, path)  # refuses to overwrite an existing result
    finally:
        temp.unlink(missing_ok=True)


# ── the controller ───────────────────────────────────────────────────────────

EXECUTOR_NAMES = ("plan", "execute", "self_verify", "review", "repair", "final_review")


class AutonomyController:
    """Drive iterations to AWAITING_HUMAN. Never past it.

    `executors` maps role operations to callables `(ctx) -> dict`; they are the
    model/harness boundary (production binds them to the direct CLI adapters
    in `autonomy_adapters`, tests script them). `prepare_packet` is optional
    compression; the deterministic packet is always built and always keeps the
    bad news.

    V0.2: the controller — not the executor — owns execution identity. Every
    executor call is allocated a V0.4A `execution_id`, recorded as
    EXECUTION_INTENT before dispatch and closed in the V0.4B ledger. The
    executor receives the allocation in `ctx["execution"]`.
    """

    def __init__(self, run_id: str, *, executors: Mapping[str, Callable[[dict], dict]],
                 env: WorkspaceEnvironment, roles: Mapping[str, Mapping[str, Any]],
                 stats_root: Path | None = None) -> None:
        missing = [n for n in EXECUTOR_NAMES if n not in executors]
        if missing:
            raise ac.AutonomyError(f"missing executors: {missing}")
        self.run_id, self.executors, self.env, self.roles = run_id, dict(executors), env, dict(roles)
        self.stats_root = Path(stats_root or STATS_ROOT)
        self.dir = autonomy_dir(run_id, self.stats_root)
        self.state_path = self.dir / "autonomy_state.json"
        self.journal = AutonomyJournal(self.dir / "autonomy_events.jsonl", run_id)
        self.ledger = ExecutionLedger.for_run(run_id, self.stats_root)
        self.descriptor_root = self.stats_root / run_id / "EXECUTIONS"
        self.results_root = self.dir / "RESULTS"
        self.lock = rl.RunLock(self.dir, run_id)
        self.state: dict[str, Any] = {}
        self._recovered: dict[str, Any] | None = None   # adopted result of a reconciled read-only call
        self._retry_of: str | None = None                # execution a replayed read-only call supersedes
        self._adopted_execution_id: str | None = None

    # lifecycle ---------------------------------------------------------------

    @classmethod
    def start(cls, run_id: str, mandate: Any, **kwargs: Any) -> "AutonomyController":
        self = cls(run_id, **kwargs)
        if self.state_path.exists():
            raise ac.AutonomyError(f"run {run_id} already exists; use resume")
        self._acquire_lock()
        if self.state_path.exists():  # lost a creation race while acquiring
            self.lock.release()
            raise ac.AutonomyError(f"run {run_id} already exists; use resume")
        try:
            frozen = ac.validate_mandate(mandate)
        except ac.AutonomyError:
            self.lock.release()
            raise
        self.state = {
            "schema_version": ac.SCHEMA_VERSION, "contract": ac.CONTRACT_ID, "run_id": run_id,
            "status": ac.RUNNING, "phase": ac.PLAN, "mandate": frozen, "mandate_hash": frozen["mandate_hash"],
            "roadmap": ac.initial_roadmap(frozen), "iterations": [], "roles": dict(self.roles),
            "in_flight": None, "hold": None, "escalation": None, "human": None, "promotion": None,
            "planning": None, "executions": [], "workspace": self.env.describe(),
            "workspace_checkpoint": self.env.checkpoint(),
            "started_at": _now(), "updated_at": _now(), "main_merge_allowed": False,
        }
        self.journal.append("MANDATE_FROZEN", payload={
            "mandate_id": frozen["mandate_id"], "mandate_hash": frozen["mandate_hash"],
            "roadmap_items": [i["item_id"] for i in frozen["roadmap_mandate"]["items"]],
            "bounds": frozen["roadmap_mandate"]["autonomy_bounds"], "roles": self._role_audit(),
            "controller_lock": self._lock_ref()})
        self._save()
        return self

    @classmethod
    def resume(cls, run_id: str, *, stats_root: Path | None = None, **kwargs: Any) -> "AutonomyController":
        """Acquire the run → inspect state → inspect the ledger → reconcile.

        The lock comes first: nothing about a run is planned or executed by a
        controller that does not exclusively own it.
        """
        self = cls(run_id, stats_root=stats_root, **kwargs)
        self._acquire_lock()
        try:
            self.state = load_state(run_id, stats_root)
        except ac.AutonomyError:
            self.lock.release()
            raise
        self.state.setdefault("executions", [])
        self.state.setdefault("planning", None)
        self.journal.append("RUN_RESUMED", phase=self.state["phase"], payload={
            "status": self.state["status"], "in_flight": self.state.get("in_flight"),
            "controller_lock": self._lock_ref()})
        if self.state["status"] == ac.RUNNING:
            try:
                self.env.restore(self.state.get("workspace_checkpoint") or {})
            except ac.GitPolicyViolation as exc:
                self._escalate(ac.E_GIT, f"resume: {exc}", (self.state.get("in_flight") or {}).get("iteration_id"))
                return self
        flight = self.state.get("in_flight")
        if flight and self.state["status"] == ac.RUNNING:
            self._reconcile_in_flight(flight)
        return self

    def _acquire_lock(self) -> None:
        try:
            self.lock.acquire()
        except rl.RunLockError as exc:
            # Recorded where the operator looks, then refused. No state is touched.
            self.journal.append(exc.outcome, payload={"detail": str(exc), "owner": exc.owner,
                                                       "liveness": exc.liveness})
            raise

    def _lock_ref(self) -> dict[str, Any]:
        record = self.lock.record or {}
        # A reference only: the owner's process evidence lives in the lock file.
        return {"outcome": rl.RUN_LOCK_ACQUIRED, "owner_token": record.get("owner_token"),
                "lock_path": str(self.lock.path)}

    def release(self) -> None:
        self.lock.release()

    def _ledger_view(self, execution_id: str) -> dict[str, Any]:
        entry = self.ledger.lifecycle().get(execution_id)
        result_path = self.results_root / f"{execution_id}.json"
        view = {"execution_id": execution_id,
                "ledger_state": entry["state"] if entry else "NO_INTENT",
                "close": dict(entry["closed"][-1]["payload"]) if entry and entry["closed"] else None,
                "result_artifact": str(result_path) if result_path.is_file() else None}
        return view

    def _reconcile_in_flight(self, flight: Mapping[str, Any]) -> None:
        """Decide from durable evidence; never re-dispatch something uncertain.

        * side-effecting phase (EXECUTE/REPAIR): escalate, whatever the ledger
          says — worktree effects of an interrupted call are unknown, and even a
          recorded result is a human's to adopt;
        * read-only phase whose execution CLOSED as COMPLETED with a persisted
          result: adopt that result, no new invocation;
        * read-only phase otherwise: replay under a NEW execution_id with
          `retry_of_execution_id` set. The old id is never dispatched again
          (the ledger refuses a second INTENT) and stays unresolved evidence.
        """
        execution_id = flight.get("execution_id")
        view = self._ledger_view(execution_id) if execution_id else {"execution_id": None,
                                                                     "ledger_state": "PRE_V0_2_NO_EXECUTION_ID"}
        phase = flight["phase"]
        if phase in ac.SIDE_EFFECT_PHASES:
            decision = "ESCALATE_SIDE_EFFECT_PHASE"
        elif view.get("close") and view["close"].get("close_reason") == "COMPLETED" and view.get("result_artifact"):
            decision = "ADOPT_RECORDED_RESULT"
        else:
            decision = "REPLAY_READ_ONLY_PHASE_WITH_NEW_EXECUTION_ID"
        self.journal.append("IN_FLIGHT_RECONCILED", iteration_id=flight.get("iteration_id"), phase=phase,
                            payload={**view, "executor": flight.get("executor"), "decision": decision})
        if decision == "ESCALATE_SIDE_EFFECT_PHASE":
            # The worktree may already hold half of this phase's effects.
            # Never repeat it blindly (same rule as workflow V0.2).
            self._escalate(ac.E_INTERRUPTED, f"{phase} was interrupted mid-flight; its worktree effects are unknown "
                           f"and it is not re-run automatically (execution {execution_id}: ledger "
                           f"{view['ledger_state']}, result artifact {view.get('result_artifact')})",
                           flight["iteration_id"])
            return
        if decision == "ADOPT_RECORDED_RESULT":
            raw = json.loads(Path(view["result_artifact"]).read_text(encoding="utf-8"))
            self._recovered = {"phase": phase, "executor": flight.get("executor"), "execution_id": execution_id,
                               "result": raw.get("result")}
            if not any(r.get("execution_id") == execution_id for r in self.state["executions"]):
                self.state["executions"].append({**dict(flight.get("execution_ref") or {}),
                                                 "reconciled": "ADOPTED_AFTER_RESTART"})
        else:
            self._retry_of = execution_id
        self.state["in_flight"] = None  # read-only phase: safe to take up again
        self._save()

    def run(self) -> dict[str, Any]:
        handlers = {ac.PLAN: self._do_plan, ac.EXECUTE: self._do_execute, ac.SELF_VERIFY: self._do_self_verify,
                    ac.AWAITING_REVIEW: self._do_prepare_review, ac.REVIEW: self._do_review,
                    ac.REPAIR: self._do_repair, ac.FINAL_REVIEW: self._do_final_review,
                    ac.ROADMAP_CHECK: self._do_roadmap_check}
        try:
            while self.state["status"] == ac.RUNNING:
                phase = self.state["phase"]
                self.lock.assert_held()
                try:
                    self.env.assert_safe()
                except ac.GitPolicyViolation as exc:
                    self._escalate(ac.E_GIT, str(exc), self._iteration_id())
                    break
                self.state["workspace_checkpoint"] = self.env.checkpoint()  # persisted with the phase's next save
                handlers[phase]()
        finally:
            # Normal end, escalation or a crash in this process: this controller
            # stops controlling. A crash leaves `in_flight` for `resume`; a
            # killed process leaves the lock for explicit reconciliation.
            self.lock.release()
        return self.state

    # small helpers -----------------------------------------------------------

    def _save(self) -> None:
        self.state["updated_at"] = _now()
        _atomic_json(self.state_path, self.state)

    def _role_audit(self) -> dict[str, Any]:
        return {r: {k: b.get(k) for k in ("role", "profile_id", "runtime_model_id", "effort", "review_independence",
                                          "binding_source")}
                for r, b in self.roles.items()}

    def _it(self) -> dict[str, Any]:
        return self.state["iterations"][-1]

    def _iteration_id(self) -> str | None:
        if self.state.get("phase") == ac.PLAN and self.state.get("planning"):
            return self.state["planning"]["iteration_id"]
        return self._it()["iteration_id"] if self.state["iterations"] else None

    def _goto(self, phase: str) -> None:
        current = self.state["phase"]
        if not ac.transition_allowed(current, phase):
            raise ac.AutonomyError(f"illegal transition {current} -> {phase}")
        self.state["phase"] = phase

    def _relations(self, name: str) -> dict[str, Any]:
        rel: dict[str, Any] = {"autonomy_run_id": self.run_id, "iteration_id": self._iteration_id(),
                               "executor": name, "phase": self.state["phase"]}
        if self.state["iterations"] and self.state["phase"] != ac.PLAN:
            mine = [e for e in self.state["executions"] if e.get("iteration_id") == self._iteration_id()]
            if name in ("review", "final_review"):
                rel["reviewed_execution_ids"] = [e["execution_id"] for e in mine
                                                 if e.get("executor") in ("execute", "repair")]
            if name == "repair":
                rel["repairs_findings_of"] = [e["execution_id"] for e in mine
                                              if e.get("executor") in ("review", "final_review", "self_verify")][-1:]
        return rel

    def _call(self, name: str, role: str, ctx: dict[str, Any]) -> Any:
        """Invoke an executor as one V0.4A execution, with an in-flight marker around it.

        Order (each step durable before the next): role preflight → descriptor →
        in-flight marker naming the execution_id → EXECUTION_INTENT → dispatch
        (STARTED only from a real spawn receipt) → result artifact →
        EXECUTION_CLOSED → state.
        """
        phase = self.state["phase"]
        if self._recovered and self._recovered["phase"] == phase and self._recovered["executor"] == name:
            adopted, self._recovered = self._recovered, None
            self._adopted_execution_id = adopted["execution_id"]
            self.journal.append("EXECUTION_RESULT_ADOPTED", iteration_id=self._iteration_id(), phase=phase,
                                payload={"execution_id": adopted["execution_id"], "role": role,
                                         "reason": "recorded COMPLETED result reconciled after restart; not re-invoked"})
            return adopted["result"]
        role_key = role if role in self.roles else ac.OPTIONAL_ROLE_ALIASES.get(role, role)
        binding = dict(self.roles[role_key])
        executor = self.executors[name]
        preflight = getattr(executor, "preflight", None)
        reason = preflight(binding) if callable(preflight) else None
        if reason:
            # Nothing invoked, so nothing allocated: an execution_id names an invocation.
            self.journal.append("ROLE_UNAVAILABLE", iteration_id=self._iteration_id(), phase=phase,
                                payload={"role": role, "profile_id": binding.get("profile_id"), "reason": reason})
            self._escalate(ac.E_ROLE_UNAVAILABLE, f"role {role} profile {binding.get('profile_id')} is not runnable: "
                           f"{reason}; no substitute is chosen by the controller", self._iteration_id())
            return None
        iteration_id = self._iteration_id()
        node_id = f"AUTONOMY:{iteration_id or 'RUN'}:{name.upper()}"
        serial = {k: v for k, v in ctx.items() if k not in ("env",)}
        descriptor, descriptor_path = xc.allocate_execution(
            descriptor_root=self.descriptor_root, run_id=self.run_id, node_id=node_id,
            invocation_kind=ac.INVOCATION_KIND_BY_EXECUTOR[name], subtask_id=None,
            provider=binding.get("provider") or binding.get("harness"), harness=binding.get("harness"),
            model=binding.get("runtime_model_id"), effort=binding.get("effort"), profile=binding.get("profile_id"),
            input_contract_hash=xc.canonical_hash(serial), selection_reason=f"AUTONOMY_ROLES:{role}",
            policy_version=ac.CONTRACT_ID, fixture_class=getattr(executor, "fixture_class", "UNDECLARED_EXECUTOR"),
            retry_of_execution_id=self._retry_of, relations=self._relations(name))
        self._retry_of = None
        execution_id = descriptor["execution_id"]
        ref = {"execution_id": execution_id, "iteration_id": iteration_id, "role": role, "executor": name,
               "phase": phase, "node_id": node_id, "profile": binding.get("profile_id"),
               "harness": binding.get("harness"), "model": binding.get("runtime_model_id"),
               "effort": binding.get("effort"), "descriptor_path": str(descriptor_path),
               "retry_of_execution_id": descriptor.get("retry_of_execution_id")}
        self.state["in_flight"] = {"phase": phase, "executor": name, "role": role, "iteration_id": iteration_id,
                                   "execution_id": execution_id, "execution_ref": ref, "started_at": _now()}
        self._save()
        try:
            self.ledger.record_execution_intent(
                execution_id=execution_id, node_id=node_id, invocation_kind=descriptor["invocation_kind"],
                descriptor_path=descriptor_path, provider=descriptor.get("provider"), harness=descriptor.get("harness"),
                model=descriptor.get("model"), effort=descriptor.get("effort"), profile=descriptor.get("profile"),
                input_contract_hash=descriptor.get("input_contract_hash"),
                repository=self.env.describe().get("repo"), worktree=self.env.describe().get("worktree"),
                extra={"autonomy_iteration_id": iteration_id, "autonomy_role": role})
        except LedgerError as exc:
            self.state["in_flight"] = None
            self._escalate(ac.E_LEDGER, f"execution intent for {execution_id} could not be durably recorded; "
                           f"nothing was dispatched: {exc}", iteration_id)
            return None
        self.journal.append("PHASE_STARTED", iteration_id=iteration_id, phase=phase,
                            payload={"role": role, "binding": self._role_audit().get(role_key),
                                     "execution_id": execution_id})
        recorder = LifecycleRecorder(self.ledger, execution_id)
        result_path = self.results_root / f"{execution_id}.json"
        ctx = {**ctx, "role": role, "binding": binding, "phase": phase,
               "mandate": self.state["mandate"], "env": self.env,
               "execution": {"execution_id": execution_id, "descriptor_path": descriptor_path, "node_id": node_id,
                             "iteration_id": iteration_id, "run_id": self.run_id, "recorder": recorder,
                             "result_path": result_path, "stats_root": self.stats_root}}
        try:
            with process_observation.observation_scope(recorder.observe_start):
                result = executor(ctx)
        except ExecutorFailure as exc:
            recorder.close(close_reason="FAILED", effect_certainty="CONFIRMED" if not recorder.started else "UNKNOWN",
                           observation_source="RUNNER_EXCEPTION" if recorder.started else
                           ("PRE_DISPATCH_FAILURE" if exc.dispatched is False else "IN_PROCESS_ADAPTER_RETURN"),
                           outcome="EXECUTOR_FAILED", detail=str(exc)[-2000:])
            self.state["in_flight"] = None
            ref.update(self._execution_observations(descriptor_path, recorder))
            self.state["executions"].append(ref)
            self._escalate(exc.code or ac.E_EXECUTOR, f"{name} executor failed: {exc} (execution {execution_id})",
                           iteration_id)
            return None
        if not result_path.exists():
            _write_once(result_path, {"execution_id": execution_id, "role": role, "executor": name,
                                      "recorded_by": "CONTROLLER", "result": result})
        recorder.close(close_reason="COMPLETED", effect_certainty="CONFIRMED",
                       observation_source="IN_PROCESS_ADAPTER_RETURN", outcome="RETURNED",
                       result_refs=[str(result_path)])
        ref.update(self._execution_observations(descriptor_path, recorder))
        self._record_execution_ref(ref)
        reused = self._session_reused(ref)
        if reused:
            self._escalate(ac.E_SESSION_REUSE, f"{role} execution {execution_id} reported provider session "
                           f"{ref['provider_session_id']} already used by {reused}; fresh context is not proven",
                           iteration_id)
            return None
        return result

    def _execution_observations(self, descriptor_path: Path, recorder: LifecycleRecorder) -> dict[str, Any]:
        try:
            descriptor = json.loads(Path(descriptor_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            descriptor = {}
        return {"provider_session_id": descriptor.get("provider_session_id"),
                "descriptor_status": descriptor.get("status"), **recorder.status()}

    def _record_execution_ref(self, ref: dict[str, Any]) -> None:
        """Bind the closed execution to the controller state (a reference, not lifecycle truth)."""
        self.state["executions"].append(ref)
        self._save()

    def _session_reused(self, ref: Mapping[str, Any]) -> str | None:
        session = ref.get("provider_session_id")
        if not session:
            return None
        return next((e["execution_id"] for e in self.state["executions"]
                     if e is not ref and e.get("provider_session_id") == session), None)

    def _done(self, **payload: Any) -> None:
        flight = self.state.get("in_flight") or {}
        self.state["in_flight"] = None
        # Only a phase that invoked something references an execution; packet
        # preparation and ROADMAP_CHECK are controller-only and carry none.
        payload.setdefault("execution_id", flight.get("execution_id") or self._adopted_execution_id)
        self._adopted_execution_id = None
        self.journal.append("PHASE_COMPLETED", iteration_id=self._iteration_id(), phase=self.state["phase"],
                            payload=payload)

    def _escalate(self, code: str, detail: str, iteration_id: str | None) -> None:
        """Stop autonomy. From here only a human can act, and promotion is off the table."""
        from_phase = self.state["phase"]
        self.state["in_flight"] = None
        self.state["status"] = ac.AWAITING_HUMAN
        self.state["phase"] = ac.AWAITING_HUMAN
        self.state["escalation"] = {"code": code, "detail": detail, "iteration_id": iteration_id, "at": _now(),
                                    "from_phase": from_phase}
        self.state["hold"] = {"reason": ac.HOLD_ESCALATION, "promotable": False, "roadmap_exhausted": False,
                              "candidate_id": None}
        self.journal.append("ESCALATED", iteration_id=iteration_id, phase=from_phase,
                            payload={"code": code, "detail": detail})
        self.journal.append("AWAITING_HUMAN", iteration_id=iteration_id, payload={"reason": ac.HOLD_ESCALATION})
        self._save()

    def _checks(self) -> dict[str, str]:
        return dict(self._it().get("evidence_state", {}))

    def _record_checks(self, checks: Any) -> list[dict[str, Any]]:
        rows = [dict(c) for c in (checks or []) if isinstance(c, Mapping)]
        for row in rows:
            self._it().setdefault("evidence_state", {})[str(row.get("name"))] = str(row.get("status", "")).upper()
            self._it().setdefault("checks", []).append(row)
        return rows

    # PLAN --------------------------------------------------------------------

    def _do_plan(self) -> None:
        index = len(self.state["iterations"]) + 1
        if not self.state.get("planning") or self.state["planning"].get("index") != index:
            # Allocated once and persisted, so a replayed PLAN keeps the same identity.
            self.state["planning"] = {"iteration_id": new_iteration_id(), "index": index, "at": _now()}
            self._save()
        history = [{"iteration_id": i["iteration_id"], "outcome": i.get("outcome"),
                    "roadmap_refs": i["lineage"]["roadmap_refs"],
                    "unresolved": (i.get("final_review") or {}).get("non_blocking_findings", [])}
                   for i in self.state["iterations"]]
        plan = self._call("plan", "planner", {
            "iteration_index": index, "roadmap": json.loads(json.dumps(self.state["roadmap"])),
            "history": history, "workspace": self.env.describe(),
            "iteration_contract": self.state["mandate"]["iteration_contract"] if index == 1 else None})
        if self.state["status"] != ac.RUNNING:
            return
        verdict = ac.check_plan(plan, self.state["mandate"], self.state["roadmap"], index,
                                expected_mandate_hash=self.state["mandate_hash"])
        self.journal.append("SCOPE_CHECK", phase=ac.PLAN, payload={
            "iteration_index": index, "decision": verdict["decision"], "code": verdict["code"],
            "reasons": verdict["reasons"], "levels": verdict["levels"],
            "within_mandate": verdict["decision"] != ac.ESCALATE})
        if verdict["decision"] == ac.ESCALATE:
            self._escalate(verdict["code"], "; ".join(verdict["reasons"]), None)
            return
        for item_id in verdict.get("skipped", []):
            reason = next(r["reason"] for r in plan["skipped_items"] if r["item_id"] == item_id)
            self.state["roadmap"][item_id] = {"status": ac.R_SKIPPED, "iteration_id": None, "reason": reason}
            self.journal.append("ROADMAP_ITEM_SKIPPED", phase=ac.PLAN, payload={"item_id": item_id, "reason": reason})
        if verdict["decision"] == ac.ACCEPT_END:
            self._done(outcome="NO_FURTHER_ACTION")
            self._await_human(ac.HOLD_ROADMAP_EXHAUSTED, "planner found no further justified action; "
                              "every remaining item was individually skipped with a reason")
            return
        iteration_id = self.state["planning"]["iteration_id"]
        self.state["iterations"].append({
            "iteration_id": iteration_id, "index": index, "status": "IN_PROGRESS", "outcome": None,
            "lineage": {"mandate_id": self.state["mandate"]["mandate_id"], "mandate_hash": self.state["mandate_hash"],
                        "source": "ITERATION_CONTRACT" if index == 1 else "ROADMAP_MANDATE",
                        "roadmap_refs": list(plan.get("roadmap_refs", [])),
                        "parent_iteration_id": self.state["iterations"][-1]["iteration_id"] if index > 1 else None,
                        "scope_justification": plan["scope_justification"]},
            "plan": plan, "planned_by": self._role_audit()["planner"], "executed_by": self._role_audit()["implementer"],
            "execution": None, "self_verify": [], "checks": [], "evidence_state": {}, "repairs": [],
            "repair_attempts": 0, "reviews": [], "final_reviews": [], "packet": None, "repair_origin": None,
            "started_at": _now(), "finished_at": None,
            "plan_execution_id": self.state["executions"][-1]["execution_id"] if self.state["executions"] else None})
        self.state["planning"] = None
        self.journal.append("ITERATION_PLANNED", iteration_id=iteration_id, phase=ac.PLAN, payload={
            "index": index, "goal": plan["goal"], "roadmap_refs": plan.get("roadmap_refs", []),
            "scope_justification": plan["scope_justification"], "acceptance_criteria": plan["acceptance_criteria"],
            "planned_by": self._role_audit()["planner"], "lineage": self._it()["lineage"],
            "plan_hash": ac.canonical_hash(plan)})
        self._done(outcome="PLANNED")
        self._goto(ac.EXECUTE)
        self._save()

    # EXECUTE / SELF_VERIFY ---------------------------------------------------

    def _do_execute(self) -> None:
        result = self._call("execute", "implementer", {"iteration": self._it(), "plan": self._it()["plan"]})
        if self.state["status"] != ac.RUNNING:
            return
        if not (isinstance(result, dict) and isinstance(result.get("summary"), str)):
            self._escalate(ac.E_EXECUTOR, "implementer returned no structured result", self._iteration_id())
            return
        self._it()["execution"] = result
        self._record_checks(result.get("checks"))
        self._done(summary=result["summary"], deviations=result.get("deviations", []))
        self._goto(ac.SELF_VERIFY)
        self._save()

    def _do_self_verify(self) -> None:
        before = diff_digest(self.env.diff())
        result = self._call("self_verify", "self_verifier", {"iteration": self._it(), "plan": self._it()["plan"],
                                                             "diff": self.env.diff(),
                                                             "changed_files": self.env.changed_files()})
        if self.state["status"] != ac.RUNNING:
            return
        if diff_digest(self.env.diff()) != before:
            # SELF_VERIFY is read-only (it is replayed after a crash); a verifier
            # that edits the candidate has silently become an implementer.
            self._escalate(ac.E_VERIFY_MUTATION, "the worktree diff changed during SELF_VERIFY", self._iteration_id())
            return
        if not (isinstance(result, dict) and isinstance(result.get("checks"), list)):
            self._escalate(ac.E_EXECUTOR, "self-verification returned no checks", self._iteration_id())
            return
        rows = self._record_checks(result["checks"])
        failing = ac.evidence_failures(self._checks())
        self._it()["self_verify"].append({"checks": rows, "failing": failing, "at": _now()})
        self._done(failing=failing, checks=len(rows))
        if failing:
            findings = [{"finding_key": f"SELF_VERIFY::{n}", "severity": "HIGH", "blocking": True,
                         "summary": f"self-verification check {n!r} failed"} for n in failing]
            self._begin_repair("SELF_VERIFY", findings)
        else:
            self._goto(ac.AWAITING_REVIEW)
            self._save()

    # packet + REVIEW ---------------------------------------------------------

    def _build_packet(self, kind: str) -> dict[str, Any]:
        it, diff = self._it(), self.env.diff()
        prior = [f for r in it["reviews"] + it["final_reviews"] for f in r["findings"]]
        adverse = ac.adverse_items(execution=it["execution"], checks=it["checks"], prior_findings=prior)
        built = ac.build_review_packet(
            iteration=it, mandate=self.state["mandate"], diff_sha256=diff_digest(diff),
            changed_files=self.env.changed_files(), head=self.env.head(),
            base=self.env.describe().get("base_head"), worktree=self.env.describe().get("worktree"),
            checks=it["checks"], adverse=adverse, kind=kind, commits=self.env.commits())
        packet = built
        compress = self.executors.get("prepare_packet")
        if compress is not None:
            draft = self._call("prepare_packet", "review_prep", {"iteration": it, "packet": built})
            if self.state["status"] != ac.RUNNING:
                return built
            if isinstance(draft, dict):
                packet = draft
        # Whatever the compressor did, the bad news is put back, and the
        # access block (diff hash, pointers) stays code-owned, never model-owned.
        packet = ac.enforce_adverse_preservation(packet, adverse)
        packet["access"] = built["access"]
        return packet

    def _do_prepare_review(self) -> None:
        packet = self._build_packet("REVIEW")
        if self.state["status"] != ac.RUNNING:
            return
        self._it()["packet"] = packet
        self._done(integrity=packet.get("integrity"), diff_sha256=packet["access"]["diff_sha256"])
        self._goto(ac.REVIEW)
        self._save()

    def _reviewer_context(self, packet: dict[str, Any], kind: str) -> dict[str, Any]:
        # The reviewer is handed the packet AND the territory. `raw` is read
        # from the repository now, not from anything the implementer produced.
        diff = self.env.diff()
        it = self._it()
        diff_path = self.dir / "PACKETS" / f"{it['iteration_id']}_{kind}_{len(it['reviews']) + len(it['final_reviews']) + 1}.diff"
        diff_path.parent.mkdir(parents=True, exist_ok=True)
        diff_path.write_text(diff, encoding="utf-8")
        return {"iteration": it, "packet": packet, "review_kind": kind,
                "raw": {"diff": diff, "diff_sha256": diff_digest(diff), "diff_path": str(diff_path),
                        "changed_files": self.env.changed_files(), "head": self.env.head(),
                        "base_head": self.env.describe().get("base_head"), "commits": self.env.commits(),
                        "evidence": list(it["checks"]), "self_verify": list(it["self_verify"]),
                        "implementation": it["execution"],
                        "previous_findings": [f for r in it["reviews"] + it["final_reviews"] for f in r["findings"]]}}

    def _do_review(self) -> None:
        it = self._it()
        if it["packet"]["access"]["diff_sha256"] != diff_digest(self.env.diff()):
            self.journal.append("PACKET_STALE", iteration_id=it["iteration_id"], phase=ac.REVIEW,
                                payload={"reason": "worktree changed after the packet was built; rebuilt"})
            it["packet"] = self._build_packet("REVIEW")
            if self.state["status"] != ac.RUNNING:
                return
        raw = self._call("review", "reviewer", self._reviewer_context(it["packet"], "REVIEW"))
        if self.state["status"] != ac.RUNNING:
            return
        result = ac.normalize_review(raw, failing_evidence=ac.evidence_failures(self._checks()))
        result["at"] = _now()
        result["execution_id"] = self.state["executions"][-1]["execution_id"] if self.state["executions"] else None
        result["reviewed_diff_sha256"] = it["packet"]["access"]["diff_sha256"]
        it["reviews"].append(result)
        self._journal_verdict(result, ac.REVIEW)
        self._done(verdict=result["verdict"])
        if result["verdict"] == ac.V_PASS:
            self._goto(ac.FINAL_REVIEW)
            self._save()
        elif result["verdict"] == ac.V_REPAIR:
            self._begin_repair("REVIEW", result["findings"])
        else:
            self._escalate(result["code"] or ac.E_REVIEW, result["summary"] or "reviewer escalated", it["iteration_id"])

    def _journal_verdict(self, result: Mapping[str, Any], phase: str) -> None:
        self.journal.append("REVIEW_VERDICT", iteration_id=self._iteration_id(), phase=phase, payload={
            "verdict": result["verdict"], "downgraded_from": result.get("downgraded_from"),
            "summary": result.get("summary"), "findings": result["findings"],
            "reviewed_by": self._role_audit()["reviewer" if phase == ac.REVIEW else "final_reviewer"],
            "execution_id": result.get("execution_id")})

    # REPAIR ------------------------------------------------------------------

    def _begin_repair(self, origin: str, findings: Sequence[Mapping[str, Any]]) -> None:
        """Enter REPAIR, or stop: the loop is bounded and refuses to spin."""
        it = self._it()
        limit = self.state["mandate"]["roadmap_mandate"]["autonomy_bounds"]["max_repair_attempts"]
        keys = sorted(f["finding_key"] for f in findings)
        if it["repair_attempts"] >= limit:
            self._escalate(ac.E_REPAIR_LIMIT, f"{it['repair_attempts']} repair attempts did not converge "
                           f"(limit {limit}); open findings: {keys}", it["iteration_id"])
            return
        last = it["repairs"][-1] if it["repairs"] else None
        if last and last.get("addresses") == keys and last.get("origin") == origin:
            self._escalate(ac.E_NO_PROGRESS, f"repair {it['repair_attempts']} did not change the findings: {keys}",
                           it["iteration_id"])
            return
        it["repair_origin"] = {"origin": origin, "findings": [dict(f) for f in findings]}
        self._goto(ac.REPAIR)
        self._save()

    def _do_repair(self) -> None:
        it = self._it()
        origin = it["repair_origin"]
        it["repair_attempts"] += 1
        # EXACT_ALLOWED_REPAIR_SCOPE: only the findings, never the goal.
        result = self._call("repair", "repairer", {
            "iteration": it, "plan": it["plan"], "findings": origin["findings"], "attempt": it["repair_attempts"]})
        if self.state["status"] != ac.RUNNING:
            return
        if not (isinstance(result, dict) and isinstance(result.get("summary"), str)):
            self._escalate(ac.E_EXECUTOR, "repair returned no structured result", it["iteration_id"])
            return
        self._record_checks(result.get("checks"))
        it["repairs"].append({"attempt": it["repair_attempts"], "origin": origin["origin"],
                              "addresses": sorted(f["finding_key"] for f in origin["findings"]),
                              "summary": result["summary"], "changed_files": result.get("changed_files", []),
                              "at": _now()})
        # Deviations and uncertainties a repair reports join the iteration's record.
        for key in ("deviations", "uncertainties", "unresolved"):
            if result.get(key):
                it["execution"][key] = list(it["execution"].get(key, [])) + list(result[key])
        self.journal.append("REPAIR_COMPLETED", iteration_id=it["iteration_id"], phase=ac.REPAIR, payload={
            "attempt": it["repair_attempts"], "origin": origin["origin"], "addresses": it["repairs"][-1]["addresses"],
            "repaired_by": self._role_audit()["repairer"],
            "execution_id": self.state["executions"][-1]["execution_id"] if self.state["executions"] else None})
        self._done(attempt=it["repair_attempts"])
        self._goto(ac.SELF_VERIFY if origin["origin"] == "SELF_VERIFY" else ac.FINAL_REVIEW)
        self._save()

    # FINAL_REVIEW ------------------------------------------------------------

    def _do_final_review(self) -> None:
        it = self._it()
        packet = self._build_packet("FINAL_REVIEW")  # fresh: it must describe the state after repairs
        if self.state["status"] != ac.RUNNING:
            return
        it["packet"] = packet
        raw = self._call("final_review", "final_reviewer", self._reviewer_context(packet, "FINAL_REVIEW"))
        if self.state["status"] != ac.RUNNING:
            return
        result = ac.normalize_review(raw, failing_evidence=ac.evidence_failures(self._checks()))
        result["at"] = _now()
        result["execution_id"] = self.state["executions"][-1]["execution_id"] if self.state["executions"] else None
        result["reviewed_diff_sha256"] = packet["access"]["diff_sha256"]
        result["non_blocking_findings"] = [f for f in result["findings"] if not f["blocking"]]
        it["final_reviews"].append(result)
        it["final_review"] = result
        self._journal_verdict(result, ac.FINAL_REVIEW)
        self._done(verdict=result["verdict"])
        if result["verdict"] == ac.V_PASS:
            it["status"], it["outcome"], it["finished_at"] = "ACCEPTED", "PASS", _now()
            for item_id in it["lineage"]["roadmap_refs"]:
                self.state["roadmap"][item_id] = {"status": ac.R_DONE, "iteration_id": it["iteration_id"], "reason": None}
            self.journal.append("ITERATION_ACCEPTED", iteration_id=it["iteration_id"], phase=ac.FINAL_REVIEW, payload={
                "roadmap_refs": it["lineage"]["roadmap_refs"], "repair_attempts": it["repair_attempts"],
                "repaired": [r["addresses"] for r in it["repairs"]], "checks": it["evidence_state"],
                "final_head": self.env.head(), "diff_sha256": packet["access"]["diff_sha256"]})
            self._goto(ac.ROADMAP_CHECK)
            self._save()
        elif result["verdict"] == ac.V_REPAIR:
            self._begin_repair("FINAL_REVIEW", result["findings"])
        else:
            it["status"], it["outcome"] = "ESCALATED", "ESCALATE"
            self._escalate(result["code"] or ac.E_REVIEW, result["summary"] or "final review escalated", it["iteration_id"])

    # ROADMAP_CHECK -----------------------------------------------------------

    def _do_roadmap_check(self) -> None:
        remaining = ac.remaining_items(self.state["roadmap"])
        cap = self.state["mandate"]["roadmap_mandate"]["autonomy_bounds"]["max_iterations"]
        done = len(self.state["iterations"])
        payload = {"remaining": remaining, "iterations_done": done, "max_iterations": cap}
        if not remaining:
            self.journal.append("ROADMAP_DECISION", phase=ac.ROADMAP_CHECK, payload={
                **payload, "next_action_available": False, "roadmap_exhausted": True,
                "reason": "every roadmap item is DONE or justified SKIPPED"})
            self._done(next="AWAITING_HUMAN")
            self._await_human(ac.HOLD_ROADMAP_EXHAUSTED, "roadmap exhausted")
        elif done >= cap:
            self.journal.append("ROADMAP_DECISION", phase=ac.ROADMAP_CHECK, payload={
                **payload, "next_action_available": False, "roadmap_exhausted": False,
                "reason": "mandate iteration budget used; remaining work needs a human decision"})
            self._done(next="AWAITING_HUMAN")
            self._await_human(ac.HOLD_ITERATION_CAP, "iteration budget reached with roadmap items remaining")
        else:
            self.journal.append("ROADMAP_DECISION", phase=ac.ROADMAP_CHECK, payload={
                **payload, "next_action_available": True, "roadmap_exhausted": False,
                "reason": "pending roadmap items remain; planner decides whether the next step still makes sense"})
            self._done(next="PLAN")
            self._goto(ac.PLAN)
            self._save()

    def _await_human(self, reason: str, detail: str) -> None:
        exhausted = reason == ac.HOLD_ROADMAP_EXHAUSTED
        accepted = [i for i in self.state["iterations"] if i["status"] == "ACCEPTED"]
        head = self.env.head()
        fingerprint = candidate_fingerprint(self.env)
        candidate_id = "CAND_" + ac.canonical_hash({"m": self.state["mandate_hash"], "head": head,
                                                    "diff": fingerprint["diff_sha256"],
                                                    "its": [i["iteration_id"] for i in accepted]})[7:23]
        self.state["phase"] = ac.AWAITING_HUMAN
        self.state["status"] = ac.AWAITING_HUMAN
        self.state["hold"] = {"reason": reason, "detail": detail, "roadmap_exhausted": exhausted,
                              "promotable": bool(accepted), "candidate_id": candidate_id, "candidate_head": head,
                              "candidate_fingerprint": fingerprint,
                              "accepted_iterations": [i["iteration_id"] for i in accepted],
                              "execution_ids": [e["execution_id"] for e in self.state.get("executions", [])]}
        self.journal.append("AWAITING_HUMAN", phase=ac.AWAITING_HUMAN, payload=dict(self.state["hold"]))
        self._save()


def candidate_fingerprint(env: WorkspaceEnvironment) -> dict[str, Any]:
    """What a candidate *is*: HEAD plus the full produced diff (uncommitted work included)."""
    return {"head": env.head(), "diff_sha256": diff_digest(env.diff()), "changed_files": env.changed_files()}


# ── the human side: approval and promotion ───────────────────────────────────
#
# These are module functions, not methods, and they take no executors. Nothing
# an executor receives in its context can reach them; the agent surface and the
# human surface are different objects by construction.

def load_state(run_id: str, stats_root: Path | None = None) -> dict[str, Any]:
    path = autonomy_dir(run_id, stats_root) / "autonomy_state.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ac.AutonomyError(f"cannot load autonomy state: {exc}") from exc
    if not ac.mandate_hash_ok(state.get("mandate", {})) or state.get("mandate_hash") != state["mandate"]["mandate_hash"]:
        raise ac.AutonomyError(f"{ac.E_MANDATE_TAMPERED}: frozen mandate does not match its hash")
    return state


def _save_state(run_id: str, state: dict[str, Any], stats_root: Path) -> None:
    state["updated_at"] = _now()
    _atomic_json(autonomy_dir(run_id, stats_root) / "autonomy_state.json", state)


def _human_surface_lock(run_id: str, stats_root: Path | None) -> rl.RunLock:
    """The human surface writes the same state file, so it takes the same run
    lock: approval cannot race a controller that is (re)driving the run."""
    lock = rl.RunLock(autonomy_dir(run_id, stats_root), run_id, purpose="HUMAN_SURFACE")
    try:
        lock.acquire()
    except rl.RunLockError as exc:
        raise ac.AutonomyError(str(exc)) from exc
    return lock


def approve_promotion(run_id: str, *, approver: str, candidate_id: str, early_end: bool = False,
                      channel: str = "HUMAN", note: str | None = None,
                      stats_root: Path | None = None) -> dict[str, Any]:
    """Record the human's explicit acceptance of one exact candidate."""
    with _human_surface_lock(run_id, stats_root):
        return _approve_promotion(run_id, approver=approver, candidate_id=candidate_id, early_end=early_end,
                                  channel=channel, note=note, stats_root=stats_root)


def _approve_promotion(run_id: str, *, approver: str, candidate_id: str, early_end: bool,
                       channel: str, note: str | None, stats_root: Path | None) -> dict[str, Any]:
    state = load_state(run_id, stats_root)
    journal = AutonomyJournal(autonomy_dir(run_id, stats_root) / "autonomy_events.jsonl", run_id)
    if state["status"] != ac.AWAITING_HUMAN or state["phase"] != ac.AWAITING_HUMAN:
        raise ac.AutonomyError("approval is only possible in AWAITING_HUMAN")
    hold = state["hold"]
    if channel != "HUMAN":
        raise ac.AutonomyError("approval must arrive on the HUMAN channel")
    if not isinstance(approver, str) or not approver.strip() or approver.lower() in ac.agent_identities(state["roles"]):
        raise ac.AutonomyError("approver must be a human identity, not one of the run's agent roles/models")
    if not hold.get("promotable"):
        raise ac.AutonomyError("this hold is not promotable: autonomy stopped on an escalation or before any "
                               "iteration passed FINAL_REVIEW")
    if candidate_id != hold.get("candidate_id"):
        raise ac.AutonomyError("candidate_id does not match the candidate this run is holding")
    if not hold["roadmap_exhausted"] and not early_end:
        raise ac.AutonomyError("roadmap not exhausted: promotion needs an explicit early_end decision")
    approval = {"approval_id": "APR_" + uuid.uuid4().hex, "approver": approver, "channel": channel,
                "candidate_id": candidate_id, "candidate_head": hold.get("candidate_head"),
                "early_end": bool(early_end and not hold["roadmap_exhausted"]), "note": note, "at": _now(),
                "hold_reason": hold["reason"]}
    state["human"] = {"verdict": "APPROVED", **approval}
    state["status"], state["phase"] = ac.HUMAN_APPROVED, ac.HUMAN_APPROVED
    journal.append("HUMAN_APPROVED", phase=ac.HUMAN_APPROVED, payload=approval)
    _save_state(run_id, state, stats_root)
    return state


def reject(run_id: str, *, approver: str, reason: str, stats_root: Path | None = None) -> dict[str, Any]:
    with _human_surface_lock(run_id, stats_root):
        return _reject(run_id, approver=approver, reason=reason, stats_root=stats_root)


def _reject(run_id: str, *, approver: str, reason: str, stats_root: Path | None) -> dict[str, Any]:
    state = load_state(run_id, stats_root)
    if state["status"] != ac.AWAITING_HUMAN:
        raise ac.AutonomyError("reject is only possible in AWAITING_HUMAN")
    state["human"] = {"verdict": "REJECTED", "approver": approver, "reason": reason, "at": _now()}
    state["status"], state["phase"] = ac.REJECTED, ac.REJECTED
    AutonomyJournal(autonomy_dir(run_id, stats_root) / "autonomy_events.jsonl", run_id).append(
        "HUMAN_REJECTED", phase=ac.REJECTED, payload=state["human"])
    _save_state(run_id, state, stats_root)
    return state


def promote(run_id: str, *, promoter: Callable[[ac.PromotionToken, dict[str, Any]], Mapping[str, Any]] | None = None,
            stats_root: Path | None = None, env: WorkspaceEnvironment | None = None) -> dict[str, Any]:
    with _human_surface_lock(run_id, stats_root):
        return _promote(run_id, promoter=promoter, stats_root=stats_root, env=env)


def _promote(run_id: str, *, promoter: Callable[[ac.PromotionToken, dict[str, Any]], Mapping[str, Any]] | None,
             stats_root: Path | None, env: WorkspaceEnvironment | None) -> dict[str, Any]:
    """PROMOTE — reachable only from a recorded human approval.

    With no `promoter` (the default) this authorizes and records the candidate
    as READY_FOR_EXTERNAL_INTEGRATION, exactly the semantics of the existing
    Human Gate: AAW itself still merges and pushes nothing. A `promoter` is the
    explicit, human-initiated integration hook; it receives a one-shot token
    that `GuardedGit` requires before it will run merge or push.

    V0.2: the token is bound to the approved `candidate_id`. A `promoter` (real
    integration) additionally requires `env`, and the candidate is re-read from
    the repository: if HEAD or the produced diff differs from what the human
    approved, PROMOTE is refused. Without `env` nothing is integrated and the
    approved fingerprint is recorded for the external integrator to verify.
    """
    state = load_state(run_id, stats_root)
    human = state.get("human") or {}
    if state["phase"] != ac.HUMAN_APPROVED or human.get("verdict") != "APPROVED" or not human.get("approval_id"):
        raise ac.AutonomyError("PROMOTE requires a recorded human approval (HUMAN_APPROVED)")
    approved = (state.get("hold") or {}).get("candidate_fingerprint")
    if promoter is not None and env is None:
        raise ac.AutonomyError("an integrating promoter requires the workspace to re-verify the approved candidate")
    if env is not None:
        current = candidate_fingerprint(env)
        if not approved or current["head"] != approved["head"] or current["diff_sha256"] != approved["diff_sha256"]:
            raise ac.AutonomyError("candidate changed after approval: HEAD or produced diff no longer matches "
                                   "the candidate the human approved")
    journal = AutonomyJournal(autonomy_dir(run_id, stats_root) / "autonomy_events.jsonl", run_id)
    state["phase"] = ac.PROMOTE
    token = ac.PromotionToken(run_id, human["approval_id"], human.get("candidate_id"))
    integration: Mapping[str, Any] = {"status": "READY_FOR_EXTERNAL_INTEGRATION", "merged": False, "pushed": False}
    if promoter is not None:
        integration = dict(promoter(token, state))
    state["promotion"] = {"approval_id": human["approval_id"], "candidate_id": human.get("candidate_id"),
                          "approved_candidate_fingerprint": approved,
                          "candidate_verified_against_workspace": env is not None,
                          "integration": dict(integration), "at": _now()}
    state["phase"], state["status"] = ac.PROMOTED, ac.PROMOTED
    journal.append("PROMOTED", phase=ac.PROMOTED, payload=state["promotion"])
    _save_state(run_id, state, stats_root)
    return state


# ── CLI (human surface only) ─────────────────────────────────────────────────

def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="AAW autonomous iterations — human surface")
    p.add_argument("--validate-mandate", type=Path)
    p.add_argument("--status")
    p.add_argument("--approve")
    p.add_argument("--reject")
    p.add_argument("--promote")
    p.add_argument("--approver")
    p.add_argument("--candidate-id")
    p.add_argument("--early-end", action="store_true")
    p.add_argument("--reason", default="")
    a = p.parse_args(argv)
    try:
        if a.validate_mandate:
            m = ac.validate_mandate(json.loads(a.validate_mandate.read_text(encoding="utf-8")))
            print(json.dumps({"status": "VALID", "mandate_hash": m["mandate_hash"]}))
        elif a.status:
            s = load_state(a.status)
            print(json.dumps({k: s.get(k) for k in ("status", "phase", "hold", "escalation", "human", "promotion")},
                             indent=2))
        elif a.approve:
            s = approve_promotion(a.approve, approver=a.approver or "", candidate_id=a.candidate_id or "",
                                  early_end=a.early_end)
            print(json.dumps({"status": s["status"], "phase": s["phase"]}))
        elif a.reject:
            s = reject(a.reject, approver=a.approver or "", reason=a.reason)
            print(json.dumps({"status": s["status"]}))
        elif a.promote:
            s = promote(a.promote)
            print(json.dumps({"status": s["status"], "promotion": s["promotion"]}))
        else:
            p.print_help()
            return 2
    except ac.AutonomyError as exc:
        print(json.dumps({"status": "REFUSED", "reason": str(exc)}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
