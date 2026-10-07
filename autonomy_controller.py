#!/usr/bin/env python3
"""AAW AUTONOMOUS ITERATIONS V0.3 — the iteration controller.

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

V0.2/V0.3 evidence boundary (see AAW_AUTONOMOUS_ITERATIONS_V0_3.md):

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
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import autonomy_contract as ac
import autonomy_policy as ap
import autonomy_run_lock as rl
import execution_contract as xc
import model_router as mr
import process_observation
import repair_escalation as rx
import run_cancellation
import work_packet as wp
from aaw_paths import AAW_ROOT, STATS_ROOT
from execution_ledger import ExecutionLedger, LedgerError, LifecycleRecorder

ROLES_PATH = AAW_ROOT / "AUTONOMY_ROLES.json"
PROFILES_PATH = AAW_ROOT / "IMPLEMENTER_PROFILES.json"


_FAILOVER = object()    # sentinel: the attempt failed softly and the next ranked profile should be tried


class ExecutorFailure(RuntimeError):
    """An executor reporting that it cannot continue (as opposed to crashing).

    Becomes an ESCALATE. Any *other* exception propagates and leaves the
    in-flight marker in the state, which is exactly what `resume` keys on.
    `code` lets an adapter name a more precise escalation (e.g. an unavailable
    role profile); `dispatched` says whether a provider was contacted.
    """

    def __init__(self, message: str, *, code: str | None = None, dispatched: bool | None = None,
                 retryable: bool = False, failure_class: str | None = None,
                 retry_after_minutes: float | None = None) -> None:
        super().__init__(message)
        self.code, self.dispatched = code, dispatched
        self.retryable = bool(retryable)
        # RATE_LIMIT / TIMEOUT / AUTH / UNAVAILABLE feed provider health (model_router); None = not a provider signal.
        self.failure_class, self.retry_after_minutes = failure_class, retry_after_minutes


class RoleUnavailable(ExecutorFailure):
    """The configured profile for a role is not runnable now. Never substituted."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code=ac.E_ROLE_UNAVAILABLE, dispatched=False, failure_class=mr.F_UNAVAILABLE)


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

    V0.2/V0.3: the controller — not the executor — owns execution identity. Every
    executor call is allocated a V0.4A `execution_id`, recorded as
    EXECUTION_INTENT before dispatch and closed in the V0.4B ledger. The
    executor receives the allocation in `ctx["execution"]`.
    """

    def __init__(self, run_id: str, *, executors: Mapping[str, Callable[[dict], dict]],
                 env: WorkspaceEnvironment, roles: Mapping[str, Mapping[str, Any]],
                 stats_root: Path | None = None,
                 quota_source: Callable[[], Mapping[str, Any]] | None = None,
                 clock: Callable[[], Any] | None = None) -> None:
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
        self.policy_bindings = dict(self.roles.get("policy_profiles") or {})
        self.policy_active = bool(self.policy_bindings)
        if self.policy_active:
            ap.validate_policy_ids(ap.profile_id_map(self.policy_bindings))
        self._load_implementer_chain()
        self.quota_source, self._clock = quota_source, clock
        self._configure_routing()
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
            "directional_charter": None, "directional_charter_hash": None,
            "planner_invocation_count": 0, "policy_preset": ap.PRESET_ID if self.policy_active else None,
            "in_flight": None, "hold": None, "escalation": None, "human": None, "promotion": None,
            "planning": None, "executions": [], "workspace": self.env.describe(),
            "workspace_checkpoint": self.env.checkpoint(), "router_state": mr.empty_state(),
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
        # A run keeps the role/profile bindings with which it started. This
        # preserves V0.1/V0.2 resume semantics and prevents a config edit from
        # silently changing tiers mid-run.
        saved_roles = self.state.get("roles")
        if isinstance(saved_roles, dict) and saved_roles:
            self.roles = dict(saved_roles)
            self.policy_bindings = dict(self.roles.get("policy_profiles") or {})
            self.policy_active = bool(self.policy_bindings)
            if self.policy_active:
                ap.validate_policy_ids(ap.profile_id_map(self.policy_bindings))
            self._load_implementer_chain()
            self._configure_routing()
        self.state.setdefault("router_state", mr.empty_state())
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
                "started": bool(entry and entry.get("started")),
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
        elif flight.get("role") == "initial_planner" and view.get("started"):
            # The initial architect is explicitly one-shot. A spawned call with
            # no durable result is ambiguous, so it must reach a human instead
            # of issuing another Opus invocation on resume.
            decision = "ESCALATE_INITIAL_PLANNER_ALREADY_INVOKED"
        else:
            decision = "REPLAY_READ_ONLY_PHASE_WITH_NEW_EXECUTION_ID"
        self.journal.append("IN_FLIGHT_RECONCILED", iteration_id=flight.get("iteration_id"), phase=phase,
                            payload={**view, "executor": flight.get("executor"), "decision": decision})
        if decision in {"ESCALATE_SIDE_EFFECT_PHASE", "ESCALATE_INITIAL_PLANNER_ALREADY_INVOKED"}:
            # The worktree may already hold half of this phase's effects.
            # Never repeat it blindly (same rule as workflow V0.2).
            code = ac.E_INTERRUPTED if decision == "ESCALATE_SIDE_EFFECT_PHASE" else ac.E_RECONCILE
            reason = (f"{phase} was interrupted mid-flight; its worktree effects are unknown "
                      if decision == "ESCALATE_SIDE_EFFECT_PHASE" else
                      "the one-time initial architect was already invoked but has no adoptable result ")
            self._escalate(code, f"{reason}and it is not re-run automatically (execution {execution_id}: ledger "
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
                if self._pause_requested(phase):
                    break
                try:
                    self.env.assert_safe()
                except ac.GitPolicyViolation as exc:
                    self._escalate(ac.E_GIT, str(exc), self._iteration_id())
                    break
                self.state["workspace_checkpoint"] = self.env.checkpoint()  # persisted with the phase's next save
                try:
                    handlers[phase]()
                except run_cancellation.RunCancelled as exc:
                    # Cooperative stop inside a phase: the call was terminated
                    # or never spawned. Status stays RUNNING and `in_flight`
                    # names the interrupted execution for `resume`.
                    flight = self.state.get("in_flight") or {}
                    self.journal.append("RUN_CANCELLED_IN_FLIGHT", iteration_id=flight.get("iteration_id"),
                                        phase=phase, payload={
                                            "reason": exc.reason, "source": exc.source,
                                            "at_boundary": exc.at_boundary,
                                            "execution_id": flight.get("execution_id"),
                                            "executor": flight.get("executor"),
                                            "side_effect_phase": flight.get("phase") in ac.SIDE_EFFECT_PHASES})
                    self._save()
                    break
        finally:
            # Normal end, escalation or a crash in this process: this controller
            # stops controlling. A crash leaves `in_flight` for `resume`; a
            # killed process leaves the lock for explicit reconciliation.
            self.lock.release()
        return self.state

    def _pause_requested(self, phase: str) -> bool:
        """Honour an ambient stop request at a phase boundary (`run_cancellation`).

        Nothing is in flight here, so stopping is clean: the next phase is
        persisted unchanged and `resume` continues from it. No token in scope
        (the default) means this never fires.
        """
        try:
            run_cancellation.check(f"AUTONOMY_PHASE_BOUNDARY:{phase}")
        except run_cancellation.RunCancelled as exc:
            self.journal.append("RUN_PAUSED", iteration_id=self._iteration_id(), phase=phase, payload={
                                    "reason": exc.reason, "source": exc.source, "at_boundary": exc.at_boundary,
                                    "resume_phase": phase, "in_flight": None})
            self._save()
            return True
        return False

    # small helpers -----------------------------------------------------------

    def _save(self) -> None:
        self.state["updated_at"] = _now()
        _atomic_json(self.state_path, self.state)

    def _role_audit(self) -> dict[str, Any]:
        return {r: {k: b.get(k) for k in ("role", "profile_id", "runtime_model_id", "effort", "review_independence",
                                          "binding_source")}
                for r, b in self.roles.items() if r not in ac.NON_ROLE_KEYS}

    def _policy_ids(self) -> dict[str, str]:
        return ap.with_fallbacks(ap.profile_id_map(self.policy_bindings))

    def _load_implementer_chain(self) -> None:
        """The frozen implementer escalation chain (None = legacy tier slots), with its resolved bindings."""
        rows = self.roles.get(ap.CHAIN_KEY) if self.policy_active else None
        rows = [r for r in rows if isinstance(r, Mapping) and r.get("profile_id")] if isinstance(rows, list) else []
        self.implementer_chain: list[str] | None = ap.validate_chain([r["profile_id"] for r in rows]) if rows else None
        self.chain_bindings = {str(r["profile_id"]): dict(r) for r in rows}

    def _configure_routing(self) -> None:
        """Quota routing and repair escalation come from the frozen role config.

        With neither block present the controller behaves exactly as before,
        except that a surviving finding now climbs the default repair ladder
        (derived from the policy profiles when there are any) instead of
        stopping at the Human Gate after one unchanged repair.
        """
        explicit = self.roles.get("repair_escalation")
        if explicit is None and self.policy_active:
            # Raw slots (no fallbacks): a run frozen before `implementer_strong` keeps its old ladder.
            explicit = ap.default_repair_escalation(ap.profile_id_map(self.policy_bindings), self.implementer_chain)
        self.escalation_cfg = rx.normalize_config(explicit)
        routing = self.roles.get("routing")
        self.routing_cfg = dict(routing) if isinstance(routing, Mapping) else None

    def _policy_selection(self, name: str, ctx: Mapping[str, Any]) -> dict[str, Any] | None:
        supplied = ctx.get("model_selection")
        if isinstance(supplied, Mapping):
            return dict(supplied)
        if not self.policy_active:
            return None
        ids = self._policy_ids()
        if name == "plan":
            index = int(ctx.get("iteration_index", len(self.state.get("iterations", [])) + 1))
            if index == 1:
                return ap.select_initial_planner(ids)
            prior = self.state.get("iterations", [])[-1].get("final_review_selection") or {}
            return ap.select_continuation_planner(ids, prior)
        if name == "execute":
            plan = ctx.get("plan") or {}
            mandate_override = (self.state.get("mandate") or {}).get("model_policy_overrides", {}).get("implementation")
            complexity = str(plan.get("implementation_complexity", "NORMAL")).upper()
            complexity_evidence = list(plan.get("complexity_evidence", []))
            floors = {"NORMAL": 0, "HARDER": 1, "SIGNIFICANTLY_DIFFICULT": 2}
            charter = self.state.get("directional_charter") or {}
            refs = set(plan.get("roadmap_refs", []))
            risk_rows = [row for row in charter.get("risk_guidance", []) if row.get("item_id") in refs]
            if risk_rows:
                floor = max((row["implementation_floor"] for row in risk_rows), key=lambda item: floors[item])
                if floors[floor] > floors[complexity]:
                    complexity = floor
                complexity_evidence.extend(f"FROZEN_CHARTER_RISK:{row['item_id']}:{row['reason']}"
                                           for row in risk_rows)
            difficulty = (self._it().get("difficulty") if self.state.get("iterations") else None) or \
                wp.assess_difficulty(plan, wp.lint_work_packet(plan, self._required_evidence()))
            return ap.select_implementation(ids, complexity, evidence=complexity_evidence,
                                             human_override=mandate_override, difficulty=difficulty,
                                             chain=self.implementer_chain)
        if name == "prepare_packet":
            return {"policy_version": ap.POLICY_VERSION, "profile_key": "review_pretreatment",
                    "profile_id": ids["review_pretreatment"], "selection_reason": "REVIEW_PRETREATMENT",
                    "tier": "VERY_HIGH", "complexity_risk_evidence": [], "previous_attempt": None,
                    "escalated_from": None}
        if name == "review":
            return {"policy_version": ap.POLICY_VERSION, "profile_key": "primary_reviewer",
                    "profile_id": ids["primary_reviewer"], "selection_reason": "PRIMARY_REVIEW",
                    "tier": "LIGHT", "complexity_risk_evidence": [], "previous_attempt": None,
                    "escalated_from": None}
        if name == "repair":
            it = self._it()
            prior_attempt = self._luna_max_capability_attempt(it)
            return ap.select_repair(ids, attempt=int(ctx.get("attempt", 1)),
                                    findings=ctx.get("findings", []), previous_attempt=prior_attempt,
                                    implementation_profile_id=next(
                                        (e.get("profile") for e in reversed(self.state["executions"])
                                         if e.get("executor") == "execute"), None),
                                    chain=self.implementer_chain)
        if name == "final_review":
            it = self._it()
            prior_findings = [f for review in it.get("reviews", []) for f in review.get("findings", [])]
            selection = ap.select_final_review(
                ids, changed_files=self.env.changed_files(), repair_attempts=it.get("repair_attempts", 0),
                findings=prior_findings, human_critical=bool(
                    self.state["mandate"]["roadmap_mandate"]["autonomy_bounds"].get("critical_scope")),
                implementation_profile_id=next((e.get("profile") for e in reversed(self.state["executions"])
                                                if e.get("executor") == "execute"), None),
                uncertainty=bool((it.get("execution") or {}).get("uncertainties") or
                                 (it.get("execution") or {}).get("unresolved") or
                                 any(review.get("uncertainties") for review in it.get("reviews", []))),
                chain=self.implementer_chain)
            current_refs = set(it.get("lineage", {}).get("roadmap_refs", []))
            risk_rows = [row for row in (self.state.get("directional_charter") or {}).get("risk_guidance", [])
                         if row.get("item_id") in current_refs]
            review_order = {"DEFAULT": 0, "HARD": 1, "CRITICAL": 2}
            if risk_rows:
                floor = max((row["final_review_floor"] for row in risk_rows), key=lambda item: review_order[item])
                if review_order[floor] > review_order[selection["tier"]]:
                    profile_key = {"DEFAULT": "final_review_default", "HARD": "final_review_hard",
                                   "CRITICAL": "final_review_critical"}[floor]
                    previous_profile = selection["profile_id"]
                    selection.update({"profile_key": profile_key, "profile_id": ids[profile_key], "tier": floor,
                                      "selection_reason": "DIRECTIONAL_CHARTER_RISK_FLOOR",
                                      "complexity_risk_evidence": [*selection["complexity_risk_evidence"],
                                          *(f"FROZEN_CHARTER_RISK:{row['item_id']}:{row['reason']}"
                                            for row in risk_rows)],
                                      "escalated_from": previous_profile})
            selection["iteration_id"] = it["iteration_id"]
            return selection
        return None

    def _luna_max_capability_attempt(self, iteration: Mapping[str, Any]) -> dict[str, Any] | None:
        findings = [f for review in iteration.get("reviews", []) + iteration.get("final_reviews", [])
                    for f in review.get("findings", [])]
        finding = next((f for f in findings if f.get("finding_code") == "IMPLEMENTATION_CAPABILITY_MISMATCH"
                        and f.get("blocking")), None)
        if not finding:
            return None
        if self.implementer_chain:
            # The latest implementation/repair call by a chain step is the one whose capability failed.
            execution = next((e for e in reversed(self.state.get("executions", []))
                              if e.get("executor") in ("execute", "repair")
                              and e.get("profile") in self.implementer_chain), None)
            failed_profile = execution.get("profile") if execution else None
        else:
            execution = next((e for e in reversed(self.state.get("executions", []))
                              if e.get("executor") == "execute"
                              and e.get("profile") == self._policy_ids()["implementer_hard"]), None)
            failed_profile = self._policy_ids()["implementer_hard"]
        evidence_ref = finding.get("evidence_ref")
        if not execution or not isinstance(evidence_ref, str) or not evidence_ref.strip():
            return None
        return {"profile_id": failed_profile, "outcome": "FAILED",
                "finding_code": "IMPLEMENTATION_CAPABILITY_MISMATCH", "execution_id": execution.get("execution_id"),
                "evidence_ref": evidence_ref}

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

    # routing ------------------------------------------------------------------

    def _now_dt(self) -> Any:
        import datetime as dt
        return self._clock() if self._clock else dt.datetime.now(dt.timezone.utc)

    def _catalog_binding(self, role: str, profile_id: str) -> dict[str, Any]:
        from model_catalog import CatalogError, load_profiles
        try:
            profile = load_profiles().get(profile_id)
        except CatalogError:
            profile = None
        if profile is None:
            return {"role": role, "profile_id": profile_id, "availability": "KNOWN_BUT_UNAVAILABLE",
                    "binding_source": "AUTONOMY_ROLES.policy_profiles"}
        return ac._binding(role, profile_id, profile, "MODEL_ROUTER")

    def _binding_for(self, role: str, selection: Mapping[str, Any] | None) -> dict[str, Any]:
        if selection:
            bound = self.policy_bindings.get(selection.get("profile_key"))
            chain_bound = self.chain_bindings.get(str(selection.get("profile_id")))
            if isinstance(bound, Mapping) and bound.get("profile_id") == selection.get("profile_id"):
                binding = dict(bound)
            elif chain_bound is not None and str(selection.get("profile_key", "")).startswith(ap.CHAIN_KEY):
                binding = dict(chain_bound)
            else:
                binding = self._catalog_binding(role, str(selection["profile_id"]))
            binding["role"] = role
            return binding
        role_key = role if role in self.roles else ac.OPTIONAL_ROLE_ALIASES.get(role, role)
        return dict(self.roles[role_key])

    def _pool_of(self, binding: Mapping[str, Any]) -> str | None:
        traits = ((self.routing_cfg or {}).get("profiles") or {}).get(binding.get("profile_id")) or {}
        return traits.get("pool") or binding.get("provider") or binding.get("harness")

    def _task_class(self, ctx: Mapping[str, Any]) -> str:
        it = self.state["iterations"][-1] if self.state["iterations"] else {}
        plan = ctx.get("plan") or it.get("plan") or {}
        size = str(plan.get("task_size") or "").upper()
        if size in mr.TASK_CLASSES:
            return size
        complexity = str(plan.get("implementation_complexity") or "NORMAL").upper()
        if complexity == "SIGNIFICANTLY_DIFFICULT":
            return mr.VERY_LARGE
        if complexity == "HARDER":
            return mr.LARGE
        return mr.SMALL if len(plan.get("touched_areas") or []) <= 1 and plan else mr.MEDIUM

    def _telemetry(self) -> Mapping[str, Any]:
        if self.quota_source is not None:
            try:
                return self.quota_source() or {}
            except Exception:                       # telemetry must never stop a run: UNKNOWN is the honest fallback
                return {}
        import provider_adapters
        path = (self.routing_cfg or {}).get("telemetry_file")
        merged = {"pools": dict(provider_adapters.collect_telemetry()["pools"])}
        if path:       # operator/adapter-maintained file wins over an adapter's own (usually UNKNOWN) snapshot
            merged["pools"].update(mr.load_telemetry(AAW_ROOT / path).get("pools") or {})
        return merged

    def _route(self, name: str, role: str, ctx: Mapping[str, Any], binding: Mapping[str, Any],
               selection: Mapping[str, Any] | None) -> dict[str, Any] | None:
        """Quota/trust/capability routing around the policy's preferred profile. None = router inactive."""
        cfg = self.routing_cfg
        if not cfg or not mr.normalize_policy(cfg.get("policy"))["enabled"]:
            return None
        traits = cfg.get("profiles") or {}
        preferred = str((selection or {}).get("profile_id") or binding.get("profile_id"))
        executor = self.executors[name]
        preflight = getattr(executor, "preflight", None)
        # The policy's own choice is preflighted exactly as before. An unrunnable preferred profile fails
        # closed (ROLE_PROFILE_UNAVAILABLE, no silent substitution) unless the config opts in to
        # `substitute_unavailable`. Alternatives are not probed up front: one that turns out unrunnable
        # fails over to the next ranked profile at dispatch and is remembered as unhealthy.
        preferred_reason = preflight(binding) if callable(preflight) and preferred == binding.get("profile_id") else None
        if preferred_reason and not cfg.get("substitute_unavailable"):
            return None
        candidates = []
        for pid, row in traits.items():
            cand_binding = binding if pid == preferred else self._catalog_binding(role, pid)
            reason = preferred_reason if pid == preferred else (
                row.get("unavailable_reason") or "declared unavailable" if row.get("available") is False else None)
            candidates.append({"profile_id": pid, "pool": row.get("pool") or cand_binding.get("provider")
                               or cand_binding.get("harness"), "runtime_model_id": cand_binding.get("runtime_model_id"),
                               "trust": row.get("trust"), "capability_class": row.get("capability_class", 0),
                               "capabilities": row.get("capabilities", []), "cost_weight": row.get("cost_weight", 1.0),
                               "available": reason is None, "unavailable_reason": reason})
        critical = bool(self.state["mandate"]["roadmap_mandate"]["autonomy_bounds"].get("critical_scope")) or \
            str((selection or {}).get("tier", "")).upper() == "CRITICAL"
        exclude_models: list[str] = []
        if name in ("review", "final_review") and (self.roles.get("reviewer") or {}).get("review_independence") \
                == "DIFFERENT_MODEL":
            exclude_models = sorted({e.get("model") for e in self.state["executions"]
                                     if e.get("executor") in ("execute", "repair") and e.get("model")})
        request = {"phase": name, "role": role, "task_class": self._task_class(ctx),
                   "criticality": mr.CRITICAL if critical else mr.NORMAL_CRITICALITY,
                   "required_capabilities": (cfg.get("phase_capabilities") or {}).get(name, []),
                   "min_capability_class": ctx.get("min_capability_class"),
                   "preferred_profile_id": preferred, "current_pool": self.state.get("last_pool"),
                   "current_profile_id": ctx.get("current_profile_id"),
                   "operation_in_progress": bool(ctx.get("operation_in_progress")),
                   "repo_modifying": name in ("execute", "repair"),
                   # a human's explicit profile choice (mandate model_policy_overrides, or a caller-supplied
                   # profile) outranks every routing heuristic when it is technically runnable
                   "manual_override": ctx.get("manual_override_profile_id") or (
                       preferred if (selection or {}).get("selection_reason") == "HUMAN_OVERRIDE" else None),
                   "exclude_models": exclude_models}
        decision = mr.route(request, candidates, self._telemetry(), self.state.get("router_state"),
                            policy=cfg.get("policy"), now=self._now_dt())
        self.state["router_state"] = decision["router_state"]
        self.journal.append("ROUTING_DECISION", iteration_id=self._iteration_id(), phase=self.state["phase"],
                            payload={**{k: v for k, v in decision.items() if k != "router_state"},
                                     "request": request, "executor": name})
        return decision

    def _routed_selection(self, base: Mapping[str, Any] | None, decision: Mapping[str, Any], pid: str,
                          name: str) -> dict[str, Any] | None:
        compact = {"reason_code": decision["reason_code"], "preferred_profile_id": decision["preferred_profile_id"],
                   "selected_profile_id": pid, "selected_pool": decision.get("selected_pool"),
                   "quota": decision.get("selected_quota"), "trust": decision.get("selected_trust"),
                   "task_class": decision["task_class"], "switch_penalty": decision["switch_penalty_applied"],
                   "alternatives": [{"profile_id": a["profile_id"], "eligible": a["eligible"], "zone": a["zone"],
                                     "rejected_by": a["rejected_by"][:2]} for a in decision["alternatives"]]}
        if pid == decision["preferred_profile_id"] and base:
            return {**dict(base), "routing": compact}
        sel = dict(base or {})
        sel.update({"policy_version": sel.get("policy_version") or mr.ROUTER_VERSION,
                    "profile_key": "routed:" + pid, "profile_id": pid,
                    "selection_reason": "ROUTED:" + decision["reason_code"], "tier": sel.get("tier", "ROUTED"),
                    "escalated_from": decision["preferred_profile_id"], "routing": compact})
        sel.setdefault("complexity_risk_evidence", [])
        sel.setdefault("previous_attempt", None)
        return sel

    def _call(self, name: str, role: str, ctx: dict[str, Any]) -> Any:
        """Select (policy → router), then dispatch with bounded provider failover."""
        phase = self.state["phase"]
        if self._recovered and self._recovered["phase"] == phase and self._recovered["executor"] == name:
            return self._call_once(name, role, ctx, None, None, None, failover=False)
        selection = self._policy_selection(name, ctx)
        binding = self._binding_for(role, selection)
        decision = self._route(name, role, ctx, binding, selection)
        if decision is None:
            return self._call_once(name, role, ctx, selection, binding, None, failover=False)
        ranked = list(decision["ranked_profile_ids"])[:1 + int((self.routing_cfg or {}).get("max_failovers", 2))]
        if decision["selected_profile_id"] is None or not ranked:
            self._save()
            self._escalate(ac.E_ROUTING, f"{name}: no adequate profile ({decision['reason_code']}"
                           f"{': ' + decision['detail'] if decision.get('detail') else ''}); nothing was dispatched",
                           self._iteration_id())
            return None
        for index, pid in enumerate(ranked):
            chosen = self._routed_selection(selection, decision, pid, name)
            chosen_binding = self._binding_for(role, chosen) if pid != binding.get("profile_id") else dict(binding)
            result = self._call_once(name, role, ctx, chosen, chosen_binding, decision,
                                     failover=index < len(ranked) - 1)
            if result is not _FAILOVER:
                return result
        return None   # unreachable: the last candidate never fails over

    def _failover_safe(self, name: str, before_digest: str | None) -> bool:
        """A failed attempt may be retried elsewhere only if it provably left the worktree untouched."""
        if name not in ("execute", "repair"):
            return True
        return before_digest is not None and diff_digest(self.env.diff()) == before_digest

    def _call_once(self, name: str, role: str, ctx: dict[str, Any], selection: Mapping[str, Any] | None,
                   binding: dict[str, Any] | None, route: Mapping[str, Any] | None, *, failover: bool) -> Any:
        """Invoke an executor as one V0.4A execution, with an in-flight marker around it.

        Order (each step durable before the next): role preflight → descriptor →
        in-flight marker naming the execution_id → EXECUTION_INTENT → dispatch
        (STARTED only from a real spawn receipt) → result artifact →
        EXECUTION_CLOSED → state.
        """
        phase = self.state["phase"]
        base_ctx = dict(ctx)
        if self._recovered and self._recovered["phase"] == phase and self._recovered["executor"] == name:
            adopted, self._recovered = self._recovered, None
            self._adopted_execution_id = adopted["execution_id"]
            self.journal.append("EXECUTION_RESULT_ADOPTED", iteration_id=self._iteration_id(), phase=phase,
                                payload={"execution_id": adopted["execution_id"], "role": role,
                                         "reason": "recorded COMPLETED result reconciled after restart; not re-invoked"})
            return adopted["result"]
        if binding is None:                  # adopted-result path above returned; otherwise resolve here
            selection = self._policy_selection(name, ctx)
            binding = self._binding_for(role, selection)
        executor = self.executors[name]
        preflight = getattr(executor, "preflight", None)
        reason = preflight(binding) if callable(preflight) else None
        if reason:
            # Nothing invoked, so nothing allocated: an execution_id names an invocation.
            self.journal.append("ROLE_UNAVAILABLE", iteration_id=self._iteration_id(), phase=phase,
                                payload={"role": role, "profile_id": binding.get("profile_id"), "reason": reason,
                                         "failover": failover})
            if failover:
                self.state["router_state"] = mr.record_failure(
                    self.state.get("router_state"), "profile:" + str(binding.get("profile_id")), mr.F_UNAVAILABLE,
                    now=self._now_dt(), policy=(self.routing_cfg or {}).get("policy"), detail=reason)
                return _FAILOVER
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
            input_contract_hash=xc.canonical_hash(serial),
            selection_reason=(selection or {}).get("selection_reason") or f"AUTONOMY_ROLES:{role}",
            policy_version=ac.CONTRACT_ID, fixture_class=getattr(executor, "fixture_class", "UNDECLARED_EXECUTOR"),
            retry_of_execution_id=self._retry_of, relations=self._relations(name))
        self._retry_of = None
        execution_id = descriptor["execution_id"]
        ref = {"execution_id": execution_id, "iteration_id": iteration_id, "role": role, "executor": name,
               "phase": phase, "node_id": node_id, "profile": binding.get("profile_id"),
               "selection": dict(selection) if selection else None,
               "harness": binding.get("harness"), "model": binding.get("runtime_model_id"),
               "effort": binding.get("effort"), "descriptor_path": str(descriptor_path),
               "retry_of_execution_id": descriptor.get("retry_of_execution_id")}
        if selection and selection.get("routing"):
            ref["routing"] = selection["routing"]
        if selection:
            self.journal.append("MODEL_POLICY_SELECTED", iteration_id=iteration_id, phase=phase, payload={
                **dict(selection), "role": role, "selected_profile": binding.get("profile_id"),
                "execution_id": execution_id})
        self.state["in_flight"] = {"phase": phase, "executor": name, "role": role, "iteration_id": iteration_id,
                                   "execution_id": execution_id, "execution_ref": ref, "started_at": _now()}
        self._save()
        diff_before = diff_digest(self.env.diff()) if name in ("execute", "repair") else None
        try:
            self.ledger.record_execution_intent(
                execution_id=execution_id, node_id=node_id, invocation_kind=descriptor["invocation_kind"],
                descriptor_path=descriptor_path, provider=descriptor.get("provider"), harness=descriptor.get("harness"),
                model=descriptor.get("model"), effort=descriptor.get("effort"), profile=descriptor.get("profile"),
                input_contract_hash=descriptor.get("input_contract_hash"),
                repository=self.env.describe().get("repo"), worktree=self.env.describe().get("worktree"),
                extra={"autonomy_iteration_id": iteration_id, "autonomy_role": role,
                       "model_policy": dict(selection) if selection else None,
                       "routing": (selection or {}).get("routing")})
        except LedgerError as exc:
            self.state["in_flight"] = None
            self._escalate(ac.E_LEDGER, f"execution intent for {execution_id} could not be durably recorded; "
                           f"nothing was dispatched: {exc}", iteration_id)
            return None
        self.journal.append("PHASE_STARTED", iteration_id=iteration_id, phase=phase,
                            payload={"role": role, "binding": binding,
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
        except run_cancellation.RunCancelled as exc:
            # A stop request terminated this call (or refused its spawn). The
            # in-flight marker stays: `resume` decides from it exactly as after
            # a crash (read-only phase -> replay under a new execution_id;
            # EXECUTE/REPAIR -> INTERRUPTED_IN_FLIGHT, never a blind replay).
            recorder.close(close_reason="CANCELLED",
                           effect_certainty="UNKNOWN" if recorder.started else "CONFIRMED",
                           observation_source="RUNNER_EXCEPTION" if recorder.started else "PRE_DISPATCH_FAILURE",
                           outcome="CANCELLED_BY_REQUEST", detail=f"{exc.reason} at {exc.at_boundary}")
            raise
        except ExecutorFailure as exc:
            recorder.close(close_reason="FAILED", effect_certainty="CONFIRMED" if not recorder.started else "UNKNOWN",
                           observation_source="RUNNER_EXCEPTION" if recorder.started else
                           ("PRE_DISPATCH_FAILURE" if exc.dispatched is False else "IN_PROCESS_ADAPTER_RETURN"),
                           outcome="EXECUTOR_FAILED", detail=str(exc)[-2000:])
            self.state["in_flight"] = None
            ref.update(self._execution_observations(descriptor_path, recorder))
            self.state["executions"].append(ref)
            if exc.retryable and descriptor.get("retry_of_execution_id") is None:
                self.state["in_flight"] = None
                self._retry_of = execution_id
                self.journal.append("EXECUTOR_RETRY_SCHEDULED", iteration_id=iteration_id, phase=phase,
                                    payload={"execution_id": execution_id, "executor": name,
                                             "reason": str(exc), "retry_limit": 1})
                self._save()
                return self._call(name, role, base_ctx)
            pool = self._pool_of(binding) or "?"
            if exc.failure_class:
                self.state["router_state"] = mr.record_failure(
                    self.state.get("router_state"), pool, exc.failure_class, now=self._now_dt(),
                    retry_after_minutes=exc.retry_after_minutes, policy=(self.routing_cfg or {}).get("policy"),
                    detail=str(exc))
            if failover and exc.failure_class and self._failover_safe(name, diff_before):
                self.journal.append("PROVIDER_FAILOVER", iteration_id=iteration_id, phase=phase, payload={
                    "executor": name, "failed_profile_id": binding.get("profile_id"), "pool": pool,
                    "failure_class": exc.failure_class, "execution_id": execution_id, "detail": str(exc)[-500:],
                    "worktree_untouched": True})
                self._save()
                return _FAILOVER
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
        self.state["last_pool"] = self._pool_of(binding)
        if self.state.get("router_state") is not None and self.state["last_pool"]:
            self.state["router_state"] = mr.record_success(self.state["router_state"], self.state["last_pool"])
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
        self._ledger_dispositions(rx.DISP_HUMAN_GATE, f"{code}: {detail}"[:500])
        self.state["status"] = ac.AWAITING_HUMAN
        self.state["phase"] = ac.AWAITING_HUMAN
        self.state["escalation"] = {"code": code, "detail": detail, "iteration_id": iteration_id, "at": _now(),
                                    "from_phase": from_phase}
        self.state["hold"] = {"reason": ac.HOLD_ESCALATION, "promotable": False, "roadmap_exhausted": False,
                              "candidate_id": None, **self._escalation_candidate()}
        self.journal.append("ESCALATED", iteration_id=iteration_id, phase=from_phase,
                            payload={"code": code, "detail": detail})
        self.journal.append("AWAITING_HUMAN", iteration_id=iteration_id, payload={"reason": ac.HOLD_ESCALATION})
        self._save()

    def _escalation_candidate(self) -> dict[str, Any]:
        """What the worktree holds at an escalation, so the Human Gate never shows an empty candidate.

        `candidate_fingerprint` is the whole iteration's produced state (diff against the baseline);
        `last_repair` separately says what the *latest* repair changed, so "the last repair changed
        nothing" is not confused with "the iteration changed nothing".
        """
        try:
            fingerprint = candidate_fingerprint(self.env)
        except Exception:                       # a missing worktree must not turn an escalation into a crash
            return {}
        out: dict[str, Any] = {"candidate_fingerprint": fingerprint, "iteration_changed_files": fingerprint["changed_files"]}
        if self.state.get("iterations"):
            repairs = self._it().get("repairs", [])
            if repairs:
                last = repairs[-1]
                out["last_repair"] = {"attempt": last["attempt"], "stage": last.get("stage"),
                                      "profile_id": last.get("profile_id"), "code_changed": last.get("code_changed"),
                                      "evidence_changed": last.get("evidence_changed"),
                                      "reported_changed_files": last.get("changed_files", []),
                                      "observed_files_delta": last.get("observed_files_delta", []),
                                      "signals": last.get("signals", [])}
        return out

    def _checks(self) -> dict[str, str]:
        return dict(self._it().get("evidence_state", {}))

    def _record_checks(self, checks: Any) -> list[dict[str, Any]]:
        rows = [dict(c) for c in (checks or []) if isinstance(c, Mapping)]
        for row in rows:
            self._it().setdefault("evidence_state", {})[str(row.get("name"))] = str(row.get("status", "")).upper()
            self._it().setdefault("checks", []).append(row)
        return rows

    def _required_evidence(self) -> list[str]:
        return list(((self.state.get("mandate") or {}).get("iteration_contract") or {}).get("required_evidence", []))

    def _record_self_audit(self, result: Mapping[str, Any], phase: str) -> None:
        """The implementer's own final audit becomes ordinary check rows (FAIL → cheap REPAIR before review)."""
        if not (self.policy_active or "self_audit" in result):
            return
        rows = self._record_checks(wp.audit_checks(result))
        self._it().setdefault("self_audits", []).append({"phase": phase, "audit": result.get("self_audit"),
                                                         "at": _now()})
        self.journal.append("IMPLEMENTER_SELF_AUDIT", iteration_id=self._iteration_id(), phase=self.state["phase"],
                            payload={"source": phase, "failing": [r["name"] for r in rows if r["status"] == "FAIL"],
                                     "warnings": [r["name"] for r in rows if r["status"] == "WARN"],
                                     "fixed_during_audit": list((result.get("self_audit") or {}).get(
                                         "fixed_during_audit") or [])
                                     if isinstance(result.get("self_audit"), Mapping) else []})

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
        initial_architect = self.policy_active and self.state.get("directional_charter") is None
        if self.policy_active and not initial_architect:
            frozen = self.state.get("directional_charter") or {}
            if frozen.get("charter_hash") != self.state.get("directional_charter_hash"):
                self._escalate(ac.E_MANDATE_TAMPERED, "frozen directional charter hash does not match", None)
                return
        role = "initial_planner" if initial_architect else "continuation_planner" if self.policy_active else "planner"
        plan = self._call("plan", role, {
            "iteration_index": index, "planning_stage": "INITIAL_ARCHITECT" if initial_architect else "NEXT_ITERATION_PLAN",
            "roadmap": json.loads(json.dumps(self.state["roadmap"])), "history": history,
            "workspace": self.env.describe(),
            "directional_charter": self.state.get("directional_charter"),
            "directional_charter_hash": self.state.get("directional_charter_hash"),
            "working_roadmap": self.state.get("working_roadmap"),
            "iteration_contract": self.state["mandate"]["iteration_contract"] if index == 1 else None})
        if self.state["status"] != ac.RUNNING:
            return
        if not isinstance(plan, dict):
            self._escalate(ac.E_PLAN_INVALID, "planner returned no structured plan", None)
            return
        if initial_architect:
            try:
                charter = ac.validate_directional_charter(plan.get("directional_charter"), self.state["mandate"])
            except ac.AutonomyError as exc:
                self._escalate(ac.E_EXTENSION, f"initial directional charter was rejected: {exc}", None)
                return
            self.state["directional_charter"] = charter
            self.state["directional_charter_hash"] = charter["charter_hash"]
            plan["directional_charter_hash"] = charter["charter_hash"]
            plan.pop("directional_charter", None)
            self.state["planner_invocation_count"] = int(self.state.get("planner_invocation_count", 0)) + 1
            self.journal.append("DIRECTIONAL_CHARTER_FROZEN", phase=ac.PLAN, payload={
                "mandate_hash": self.state["mandate_hash"], "charter_hash": charter["charter_hash"],
                "roadmap_items": [item["item_id"] for item in charter["roadmap_items"]],
                "risk_guidance": [{k: row[k] for k in ("item_id", "implementation_floor", "final_review_floor")}
                                  for row in charter["risk_guidance"]],
                "mandated_risk_floors": len(charter.get("mandated_risk_floors", [])),
                "risk_floor_adjustments": charter.get("risk_floor_adjustments", []),
                "initial_planner_execution_id": next((e.get("execution_id") for e in reversed(self.state["executions"])
                                                       if e.get("role") == "initial_planner"), None)})
        verdict = ac.check_plan(plan, self.state["mandate"], self.state["roadmap"], index,
                                expected_mandate_hash=self.state["mandate_hash"],
                                expected_directional_charter_hash=self.state.get("directional_charter_hash")
                                if self.policy_active else None)
        self.journal.append("SCOPE_CHECK", phase=ac.PLAN, payload={
            "iteration_index": index, "decision": verdict["decision"], "code": verdict["code"],
            "reasons": verdict["reasons"], "levels": verdict["levels"],
            "directional_charter_hash": self.state.get("directional_charter_hash"),
            "within_mandate": verdict["decision"] != ac.ESCALATE})
        if verdict["decision"] == ac.ESCALATE:
            self._escalate(verdict["code"], "; ".join(verdict["reasons"]), None)
            return
        for item_id in verdict.get("skipped", []):
            reason = next(r["reason"] for r in plan["skipped_items"] if r["item_id"] == item_id)
            self.state["roadmap"][item_id].update({"status": ac.R_SKIPPED, "iteration_id": None, "reason": reason})
            ac.refresh_dependency_states(self.state["roadmap"])
            self.journal.append("ROADMAP_ITEM_SKIPPED", phase=ac.PLAN, payload={"item_id": item_id, "reason": reason})
        if verdict["decision"] == ac.ACCEPT_END:
            self._done(outcome="NO_FURTHER_ACTION")
            self.state["planning"] = None  # no iteration was created; do not show a phantom "planning" row
            self._await_human(ac.HOLD_ROADMAP_EXHAUSTED, "planner found no further justified action; "
                              "every autonomous roadmap item was individually skipped with a reason")
            return
        iteration_id = self.state["planning"]["iteration_id"]
        plan_execution = next((e for e in reversed(self.state["executions"]) if e.get("executor") == "plan"), {})
        self.state["iterations"].append({
            "iteration_id": iteration_id, "index": index, "status": "IN_PROGRESS", "outcome": None,
            "lineage": {"mandate_id": self.state["mandate"]["mandate_id"], "mandate_hash": self.state["mandate_hash"],
                        "directional_charter_hash": self.state.get("directional_charter_hash"),
                        "source": ("ITERATION_CONTRACT" if index == 1 else
                                   "FROZEN_DIRECTIONAL_CHARTER" if self.policy_active else "ROADMAP_MANDATE"),
                        "roadmap_refs": list(plan.get("roadmap_refs", [])),
                        "parent_iteration_id": self.state["iterations"][-1]["iteration_id"] if index > 1 else None,
                        "scope_justification": plan["scope_justification"]},
            "plan": plan, "planned_by": {"role": plan_execution.get("role"), "profile_id": plan_execution.get("profile"),
                                          "runtime_model_id": plan_execution.get("model"), "effort": plan_execution.get("effort")},
            "executed_by": self._role_audit()["implementer"],
            "execution": None, "self_verify": [], "checks": [], "evidence_state": {}, "repairs": [],
            "repair_attempts": 0, "reviews": [], "final_reviews": [], "packet": None, "repair_origin": None,
            "started_at": _now(), "finished_at": None,
            "plan_execution_id": plan_execution.get("execution_id")})
        lint = wp.lint_work_packet(plan, self._required_evidence())
        difficulty = wp.assess_difficulty(plan, lint)
        self._it().update(work_packet_lint=lint, difficulty=difficulty)
        self.journal.append("WORK_PACKET_ASSESSED", iteration_id=iteration_id, phase=ac.PLAN, payload={
            "version": wp.VERSION, "present": lint["present"], "issues": lint["issues"], "metrics": lint["metrics"],
            "route": difficulty["route"], "reasons": difficulty["reasons"]})
        self.state["planning"] = None
        self._record_working_roadmap(plan, index, iteration_id)
        self.journal.append("ITERATION_PLANNED", iteration_id=iteration_id, phase=ac.PLAN, payload={
            "index": index, "goal": plan["goal"], "roadmap_refs": plan.get("roadmap_refs", []),
            "scope_justification": plan["scope_justification"], "acceptance_criteria": plan["acceptance_criteria"],
            "planned_by": self._it()["planned_by"], "lineage": self._it()["lineage"],
            "directional_charter_hash": self.state.get("directional_charter_hash"),
            "plan_hash": ac.canonical_hash(plan)})
        self._done(outcome="PLANNED")
        self._goto(ac.EXECUTE)
        self._save()
    def _record_working_roadmap(self, plan: Mapping[str, Any], index: int, iteration_id: str) -> None:
        """Keep the planner's living notes SEPARATE from the frozen mandate.

        The mandate (the human's direction) is hashed and never edited; this is advisory state the
        planner rewrites each iteration and the operator reads. It grants no scope: `check_plan`
        still binds every iteration to pending mandate items.
        """
        notes, next_step = plan.get("working_roadmap"), plan.get("next_recommended_step")
        if not (isinstance(notes, str) and notes.strip()) and not (isinstance(next_step, str) and next_step.strip()):
            return
        previous = self.state.get("working_roadmap") or {}
        entry = {"iteration_id": iteration_id, "index": index, "at": _now(),
                 "notes": notes.strip() if isinstance(notes, str) and notes.strip() else previous.get("notes"),
                 "next_step": next_step.strip() if isinstance(next_step, str) and next_step.strip() else None}
        self.state["working_roadmap"] = entry
        self.state.setdefault("working_roadmap_history", []).append(entry)

    # EXECUTE / SELF_VERIFY ---------------------------------------------------

    def _do_execute(self) -> None:
        result = self._call("execute", "implementer", {"iteration": self._it(), "plan": self._it()["plan"]})
        if self.state["status"] != ac.RUNNING:
            return
        if not (isinstance(result, dict) and isinstance(result.get("summary"), str)):
            self._escalate(ac.E_EXECUTOR, "implementer returned no structured result", self._iteration_id())
            return
        self._it()["execution"] = result
        execute_ref = self.state["executions"][-1] if self.state.get("executions") else {}
        self._it()["executed_by"] = self._execution_audit(execute_ref.get("execution_id"))
        self._record_checks(result.get("checks"))
        self._record_self_audit(result, "EXECUTE")
        self._done(summary=result["summary"], deviations=result.get("deviations", []))
        self._goto(ac.SELF_VERIFY)
        self._save()

    def _do_self_verify(self) -> None:
        before = diff_digest(self.env.diff())
        verification_mode = "MODEL"
        semantic_reason: Any = None
        if self.policy_active:
            rows = self._deterministic_self_verification()
            iteration = self._it()
            execution_result = iteration.get("execution") or {}
            plan = iteration.get("plan") or {}
            semantic_required = bool(plan.get("semantic_verification_required")) or bool(
                execution_result.get("uncertainties") or execution_result.get("unresolved"))
            if semantic_required:
                verification_mode = "DETERMINISTIC_PLUS_SEMANTIC_MODEL"
                semantic_reason = plan.get("semantic_verification_reason") or \
                    execution_result.get("uncertainties") or execution_result.get("unresolved")
                semantic = self._call("self_verify", "self_verifier", {
                    "iteration": iteration, "plan": plan, "diff": self.env.diff(),
                    "changed_files": self.env.changed_files(), "deterministic_checks": rows,
                    "semantic_verification_reason": semantic_reason})
                if self.state["status"] != ac.RUNNING:
                    return
                if not isinstance(semantic, dict) or not isinstance(semantic.get("checks"), list):
                    self._escalate(ac.E_EXECUTOR, "semantic self-verification returned no checks", self._iteration_id())
                    return
                rows.extend(dict(row) for row in semantic["checks"] if isinstance(row, Mapping))
            else:
                verification_mode = "DETERMINISTIC"
        else:
            result = self._call("self_verify", "self_verifier", {"iteration": self._it(), "plan": self._it()["plan"],
                                                                 "diff": self.env.diff(),
                                                                 "changed_files": self.env.changed_files()})
            if self.state["status"] != ac.RUNNING:
                return
            if not (isinstance(result, dict) and isinstance(result.get("checks"), list)):
                self._escalate(ac.E_EXECUTOR, "self-verification returned no checks", self._iteration_id())
                return
            rows = [dict(row) for row in result["checks"] if isinstance(row, Mapping)]
        if diff_digest(self.env.diff()) != before:
            # SELF_VERIFY is read-only (it is replayed after a crash); a verifier
            # that edits the candidate has silently become an implementer.
            self._escalate(ac.E_VERIFY_MUTATION, "the worktree diff changed during SELF_VERIFY", self._iteration_id())
            return
        rows = self._record_checks(rows)
        failing = ac.evidence_failures(self._checks())
        self._it()["self_verify"].append({"mode": verification_mode, "semantic_reason": semantic_reason,
                                           "checks": rows, "failing": failing, "at": _now()})
        self._done(failing=failing, checks=len(rows))
        if failing:
            findings = [{"finding_key": f"SELF_VERIFY::{n}", "severity": "HIGH", "blocking": True,
                         "summary": f"self-verification check {n!r} failed"} for n in failing]
            self._begin_repair("SELF_VERIFY", findings)
        else:
            self._goto(ac.AWAITING_REVIEW)
            self._save()

    def _deterministic_self_verification(self) -> list[dict[str, Any]]:
        """Check evidence coverage and statuses without spending a model call."""
        it = self._it()
        required = self.state["mandate"]["iteration_contract"].get("required_evidence", [])
        checks = list(it.get("checks", []))
        rows: list[dict[str, Any]] = []
        for evidence in required:
            target = " ".join(str(evidence).casefold().split())
            matches = [row for row in checks if target in " ".join(
                f"{row.get('name', '')} {row.get('summary', '')}".casefold().split()) or
                " ".join(str(row.get("name", "")).casefold().split()) in target or
                wp.evidence_matches(str(evidence), str(row.get("name", "")))]
            classified = [row for row in matches if str(row.get("status", "")).upper() in ac.CLASSIFIED_STATUSES
                          and row.get("log_ref")]
            passed = any(str(row.get("status", "")).upper() == "PASS" for row in matches) or bool(classified)
            rows.append({"name": f"required_evidence::{evidence}", "status": "PASS" if passed else "FAIL",
                         "summary": (f"required evidence is backed by {len(matches)} recorded check(s)"
                                     + (f"; {len(classified)} classified with evidence (not a pass)" if classified else "")
                                     if passed else f"required evidence {evidence!r} has no passing recorded check"),
                         "source_refs": [row.get("log_ref") or row.get("name") for row in matches]})
        rows.extend(self._static_sanity_rows())
        try:
            self.env.assert_safe()
            rows.append({"name": "controller.git_boundary", "status": "PASS",
                         "summary": "workspace and protected Git boundaries pass"})
        except ac.GitPolicyViolation as exc:
            rows.append({"name": "controller.git_boundary", "status": "FAIL", "summary": str(exc)})
        if not rows:
            rows.append({"name": "controller.evidence_integrity", "status": "PASS",
                         "summary": "all implementation and machine check records are retained for independent review"})
        return rows

    def _static_sanity_rows(self) -> list[dict[str, Any]]:
        """Deterministic detection of the simplest defects before any reviewer is paid for."""
        it = self._it()
        lint = it.get("work_packet_lint") or {}
        claimed = list((it.get("execution") or {}).get("changed_files") or [])
        for repair in it.get("repairs", []):
            claimed.extend(repair.get("changed_files") or [])
        try:
            changed = self.env.changed_files()
        except Exception:  # an environment that cannot list changes simply gets no static rows
            return []
        worktree = (self.env.describe() or {}).get("worktree")
        return wp.static_sanity_checks(worktree, changed, claimed_files=claimed,
                                       planned_files=lint.get("files_to_change") or [],
                                       expect_changes=bool(lint.get("present") and lint.get("files_to_change")))

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
        if self.executors.get("prepare_packet") is not None:
            draft = self._call("prepare_packet", "review_prep", {"iteration": it, "packet": built})
            if self.state["status"] != ac.RUNNING:
                return built
            if self._valid_pretreatment(draft):
                packet = json.loads(json.dumps(built))
                packet["PRETREATMENT"] = {key: draft[key] for key in
                    ("summary", "implementation_claims", "check_refs", "finding_refs", "changed_files", "source_refs")}
            else:
                self.journal.append("REVIEW_PRETREATMENT_REJECTED", iteration_id=it["iteration_id"],
                                    phase=self.state["phase"], payload={
                                        "execution_id": self.state["executions"][-1].get("execution_id")
                                        if self.state["executions"] else None,
                                        "reason": "malformed packet or authoritative verdict field"})
        # The deterministic packet remains authoritative for coverage. Pretreatment
        # can only add a non-authoritative index; every adverse item and source link
        # remains in the original packet.
        packet = ac.enforce_adverse_preservation(packet, adverse)
        packet["access"] = built["access"]
        packet["authoritative"] = False
        return packet

    def _do_prepare_review(self) -> None:
        """Build a fresh deterministic packet, then advance to the reviewer."""
        packet = self._build_packet("REVIEW")
        if self.state["status"] != ac.RUNNING:
            return
        self._it()["packet"] = packet
        self.journal.append("REVIEW_PACKET_PREPARED", iteration_id=self._iteration_id(),
                            phase=ac.AWAITING_REVIEW, payload={
                                "authoritative": False,
                                "diff_sha256": packet.get("access", {}).get("diff_sha256"),
                                "integrity": packet.get("integrity")})
        self._done(packet_prepared=True)
        self._goto(ac.REVIEW)
        self._save()

    @staticmethod
    def _valid_pretreatment(draft: Any) -> bool:
        if not isinstance(draft, dict):
            return False
        forbidden = {"verdict", "decision", "is_correct", "is_ready", "approved", "pass", "fail"}
        def contains_forbidden(value: Any) -> bool:
            if isinstance(value, Mapping):
                return any(str(key).lower() in forbidden or contains_forbidden(item)
                           for key, item in value.items())
            if isinstance(value, list):
                return any(contains_forbidden(item) for item in value)
            return False
        if contains_forbidden(draft):
            return False
        required = {"summary": str, "implementation_claims": list, "check_refs": list,
                    "finding_refs": list, "changed_files": list, "source_refs": list}
        return all(isinstance(draft.get(key), kind) for key, kind in required.items()) and all(
            all(isinstance(item, str) for item in draft[key]) for key in
            ("implementation_claims", "check_refs", "finding_refs", "changed_files", "source_refs"))

    def _reviewer_context(self, packet: dict[str, Any], kind: str) -> dict[str, Any]:
        # Only a compact manifest is sent initially. Raw bytes stay in the
        # controller-owned files until the reviewer requests specific sources.
        diff = self.env.diff()
        it = self._it()
        sequence = len(it["reviews"]) + len(it["final_reviews"]) + 1
        diff_path = self.dir / "PACKETS" / f"{it['iteration_id']}_{kind}_{sequence}.diff"
        diff_path.parent.mkdir(parents=True, exist_ok=True)
        diff_path.write_bytes(diff.encode("utf-8"))
        diff_file_sha256 = hashlib.sha256(diff_path.read_bytes()).hexdigest()
        manifest: list[dict[str, Any]] = []

        def add_source(source_ref: str, source_kind: str, path: Path) -> None:
            try:
                resolved = path.resolve(strict=True)
                if not resolved.is_file():
                    return
                raw = resolved.read_bytes()
            except OSError:
                return
            manifest.append({"source_ref": source_ref, "kind": source_kind, "path": str(resolved),
                             "sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)})

        add_source("RAW_DIFF", "DIFF", diff_path)
        for execution in self.state.get("executions", []):
            execution_id = execution.get("execution_id")
            if not execution_id:
                continue
            result_path = self.results_root / f"{execution_id}.json"
            add_source(f"EXECUTION_RESULT:{execution_id}", "EXECUTION_RESULT", result_path)
        worktree = (self.env.describe() or {}).get("worktree")
        worktree_root = Path(worktree).resolve() if worktree else None
        for index, row in enumerate(it.get("checks", []), 1):
            log_ref = row.get("log_ref")
            if not isinstance(log_ref, str) or not log_ref:
                continue
            path = Path(log_ref)
            if not path.is_absolute() and worktree_root:
                path = worktree_root / path
            if not worktree_root:
                continue
            try:
                path.resolve(strict=True).relative_to(worktree_root)
            except (OSError, ValueError):
                continue
            add_source(f"CHECK_LOG:{index}", "CHECK_LOG", path)
        return {"iteration": it, "packet": packet, "review_kind": kind,
                "directional_charter": self.state.get("directional_charter"),
                "directional_charter_hash": self.state.get("directional_charter_hash"),
                "raw": {"diff_sha256": diff_digest(diff), "diff_path": str(diff_path),
                        "diff_file_sha256": diff_file_sha256,
                        "diff_digest_note": ("diff_sha256 is the controller's canonical digest of the diff text "
                                             "(sha256:<hex> over canonical JSON). diff_file_sha256 and the "
                                             "RAW_DIFF manifest sha256 are the plain SHA-256 of the raw diff file "
                                             "bytes. Different algorithms over the same content; they are "
                                             "expected to differ and diff_file_sha256 must equal the manifest value."),
                        "changed_files": self.env.changed_files(), "head": self.env.head(),
                        "base_head": self.env.describe().get("base_head"), "commits": self.env.commits(),
                        "evidence": list(it["checks"]), "self_verify": list(it["self_verify"]),
                        "implementation": it["execution"], "manifest": manifest,
                        "previous_findings": [f for r in it["reviews"] + it["final_reviews"] for f in r["findings"]]}}

    def _review_with_raw_access(self, name: str, role: str, context: dict[str, Any],
                                model_selection: Mapping[str, Any] | None = None) -> Any:
        if model_selection:
            context = {**context, "model_selection": dict(model_selection)}
        raw = self._call(name, role, context)
        if self.state["status"] != ac.RUNNING or not isinstance(raw, dict):
            return raw
        requests = raw.get("raw_evidence_requests") or []
        if not requests:
            return raw
        manifest = {item["source_ref"]: item for item in context["raw"].get("manifest", [])}
        if len(requests) > 5:
            self.journal.append("RAW_EVIDENCE_REQUEST_REJECTED", iteration_id=self._iteration_id(),
                                phase=self.state["phase"], payload={"reason": "request_count_exceeded", "count": len(requests)})
            return {"verdict": "ESCALATE", "summary": "reviewer requested more than five raw sources",
                    "findings": [], "raw_evidence_requests": []}
        provided = []
        total_raw_bytes = 0
        request_execution_id = self.state["executions"][-1].get("execution_id") if self.state["executions"] else None
        for request in requests:
            source_ref = request.get("source_ref") if isinstance(request, Mapping) else None
            reason = request.get("reason") if isinstance(request, Mapping) else None
            if not isinstance(source_ref, str) or not isinstance(reason, str) or not reason.strip() or source_ref not in manifest:
                self.journal.append("RAW_EVIDENCE_REQUEST_REJECTED", iteration_id=self._iteration_id(),
                                    phase=self.state["phase"], payload={"source_ref": source_ref, "reason": reason,
                                                                         "detail": "unknown source_ref or empty reason"})
                return {"verdict": "ESCALATE", "summary": "reviewer requested an unknown source or gave no reason",
                        "findings": [], "raw_evidence_requests": []}
            item = manifest[source_ref]
            self.journal.append("RAW_EVIDENCE_REQUESTED", iteration_id=self._iteration_id(),
                                phase=self.state["phase"], payload={"execution_id": request_execution_id,
                                    "source_ref": source_ref, "reason": reason[:500], "sha256": item["sha256"]})
            try:
                path = Path(item["path"]).resolve(strict=True)
                if item.get("kind") == "CHECK_LOG":
                    worktree = (self.env.describe() or {}).get("worktree")
                    if not worktree:
                        raise ValueError("worktree root unavailable for check log")
                    path.relative_to(Path(worktree).resolve())
                else:
                    path.relative_to(self.dir.resolve())
                data = path.read_bytes()
                if len(data) > 1_000_000:
                    raise ValueError("source exceeds the 1 MB per-source limit")
                if total_raw_bytes + len(data) > 1_000_000:
                    raise ValueError("requested raw sources exceed the 1 MB total retrieval limit")
                digest = hashlib.sha256(data).hexdigest()
                if digest != item["sha256"]:
                    raise ValueError("source hash changed after manifest creation")
            except (OSError, ValueError) as exc:
                self.journal.append("RAW_EVIDENCE_REQUEST_REJECTED", iteration_id=self._iteration_id(),
                                    phase=self.state["phase"], payload={"source_ref": source_ref,
                                                                        "detail": str(exc)})
                return {"verdict": "ESCALATE", "summary": f"requested source {source_ref} could not be verified",
                        "findings": [], "raw_evidence_requests": []}
            provided.append({"source_ref": source_ref, "kind": item["kind"], "sha256": digest,
                             "content": data.decode("utf-8", errors="replace")})
            total_raw_bytes += len(data)
            self.journal.append("RAW_EVIDENCE_PROVIDED", iteration_id=self._iteration_id(),
                                phase=self.state["phase"], payload={"source_ref": source_ref,
                                    "sha256": digest, "size_bytes": len(data)})
        retry_context = {**context, "raw_evidence_results": provided}
        if model_selection:
            retry_context["model_selection"] = dict(model_selection)
        retry = self._call(name, role, retry_context)
        if self.state["status"] != ac.RUNNING or not isinstance(retry, dict):
            return retry
        if retry.get("raw_evidence_requests"):
            self.journal.append("RAW_EVIDENCE_REQUEST_REJECTED", iteration_id=self._iteration_id(),
                                phase=self.state["phase"], payload={"reason": "one_targeted_retrieval_round_limit",
                                                                     "requests": retry["raw_evidence_requests"]})
            retry = dict(retry)
            retry["raw_evidence_requests"] = []
            retry["verdict"] = "ESCALATE"
            retry["summary"] = "review remains ambiguous after the bounded raw evidence retrieval"
        return retry
    def _do_review(self) -> None:
        it = self._it()
        if it["packet"]["access"]["diff_sha256"] != diff_digest(self.env.diff()):
            self.journal.append("PACKET_STALE", iteration_id=it["iteration_id"], phase=ac.REVIEW,
                                payload={"reason": "worktree changed after the packet was built; rebuilt"})
            it["packet"] = self._build_packet("REVIEW")
            if self.state["status"] != ac.RUNNING:
                return
        raw = self._review_with_raw_access("review", "reviewer", self._reviewer_context(it["packet"], "REVIEW"))
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
            "reviewed_by": self._execution_audit(result.get("execution_id")),
            "execution_id": result.get("execution_id")})

    def _execution_audit(self, execution_id: str | None) -> dict[str, Any] | None:
        execution = next((row for row in reversed(self.state.get("executions", []))
                          if row.get("execution_id") == execution_id), None)
        if not execution:
            return None
        return {key: execution.get(key) for key in
                ("role", "profile", "model", "effort", "execution_id", "selection")}

    # REPAIR ------------------------------------------------------------------

    def _begin_repair(self, origin: str, findings: Sequence[Mapping[str, Any]]) -> None:
        """Enter REPAIR on the right rung of the bounded escalation ladder, or stop for a human.

        A finding that survives a REPAIR is not, by itself, a reason to stop:
        the ladder (repair_escalation) first raises effort, then a stronger
        implementer diagnoses before repairing, then the planner analyses.
        Only an exhausted ladder or the attempt bounds reach the Human Gate.
        """
        it = self._it()
        bounds = self.state["mandate"]["roadmap_mandate"]["autonomy_bounds"]
        limit = bounds["max_repair_attempts"]
        keys = sorted(f["finding_key"] for f in findings)
        cfg = self.escalation_cfg
        last = it["repairs"][-1] if it["repairs"] else None
        assessment = None
        if last is not None:
            assessment = rx.assess_progress(keys_before=last["addresses"], keys_after=keys,
                                            attempt_signals=last.get("signals", []))
            if last.get("outcome") is None:
                last["outcome"] = {"signals": assessment["signals"], "resolved_keys": assessment["resolved_keys"],
                                   "remaining_keys": keys, "progressed": assessment["progressed"]}
                self.journal.append("REPAIR_ASSESSED", iteration_id=it["iteration_id"], phase=self.state["phase"],
                                    payload={"attempt": last["attempt"], "stage": last.get("stage"),
                                             "profile_id": last.get("profile_id"), **assessment,
                                             "code_changed": last.get("code_changed"),
                                             "evidence_changed": last.get("evidence_changed")})
                self._ledger_result(last, assessment)
        if not cfg["enabled"]:
            if it["repair_attempts"] >= limit:
                self._escalate(ac.E_REPAIR_LIMIT, f"{it['repair_attempts']} repair attempts did not converge "
                               f"(limit {limit}); open findings: {keys}", it["iteration_id"])
                return
            if last and last.get("addresses") == keys and last.get("origin") == origin:
                self._escalate(ac.E_NO_PROGRESS, f"repair {it['repair_attempts']} did not change the findings: "
                               f"{keys}", it["iteration_id"])
                return
            step = {"action": "STEP", "stage": rx.STAGE_CURRENT, "profile_id": None, "mode": rx.MODE_REPAIR,
                    "reason": "ESCALATION_DISABLED", "diagnose_profile_id": None, "role": "repairer", "note": None}
        else:
            ladder = it.get("repair_ladder")
            if ladder is None or (assessment and assessment["fully_new_problem"]):
                if ladder is not None:
                    self._ledger_dispositions(rx.DISP_RESOLVED, "previous finding set replaced by a different one")
                ladder = it["repair_ladder"] = rx.new_ladder(keys)
                progressed = False
            else:
                progressed = bool(assessment and assessment["progressed"])
            step = rx.next_step(cfg, ladder, progressed=progressed,
                                last_profile_id=(last or {}).get("profile_id"),
                                diagnose_available=self.executors.get("diagnose") is not None)
            if step["action"] == "EXHAUSTED":
                tried = [f"{r.get('stage')}:{r.get('profile_id')}" for r in it["repairs"]]
                self._ledger_dispositions(rx.DISP_HUMAN_GATE, "repair ladder exhausted")
                self._escalate(ac.E_NO_PROGRESS, f"the repair escalation ladder is exhausted after "
                               f"{it['repair_attempts']} attempt(s) ({', '.join(tried)}); open findings: {keys}",
                               it["iteration_id"])
                return
            standard = sum(1 for r in it["repairs"] if r.get("stage") in (None, rx.STAGE_CURRENT))
            escalation_cap = limit + len(cfg["stages"]) * cfg["max_attempts_per_stage"]
            if (step["stage"] == rx.STAGE_CURRENT and standard >= limit) or it["repair_attempts"] >= escalation_cap \
                    or it["repair_attempts"] >= ac.HARD_MAX_REPAIR_ATTEMPTS + 8:
                self._ledger_dispositions(rx.DISP_HUMAN_GATE, "repair attempt bound reached")
                self._escalate(ac.E_REPAIR_LIMIT, f"{it['repair_attempts']} repair attempts did not converge "
                               f"(limit {limit}); open findings: {keys}", it["iteration_id"])
                return
            if step["stage"] != rx.STAGE_CURRENT or step["reason"] == "PROGRESS_RETRY_SAME_STAGE":
                step["escalation_id"] = self._ledger_escalation(step, last, assessment, keys, origin) \
                    if step["stage"] != rx.STAGE_CURRENT else None
        it["repair_origin"] = {"origin": origin, "findings": [dict(f) for f in findings], "step": step}
        self._goto(ac.REPAIR)
        self._save()

    # escalation ledger ----------------------------------------------------------

    def _escalation_ledger_path(self) -> Path:
        return self.dir / "repair_escalation_ledger.jsonl"

    def _ledger_append(self, entry: Mapping[str, Any]) -> None:
        path = self._escalation_ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":"), default=str) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _profile_effort(self, profile_id: str | None) -> dict[str, Any]:
        if not profile_id:
            return {"profile_id": None, "model": None, "effort": None}
        binding = self._catalog_binding("repairer", profile_id)
        return {"profile_id": profile_id, "model": binding.get("runtime_model_id"), "effort": binding.get("effort")}

    def _ledger_escalation(self, step: Mapping[str, Any], last: Mapping[str, Any] | None,
                           assessment: Mapping[str, Any] | None, keys: Sequence[str], origin: str) -> str:
        it = self._it()
        escalation_id = "ESC_" + uuid.uuid4().hex[:16]
        previous = {"profile_id": (last or {}).get("profile_id"), "model": (last or {}).get("model"),
                    "effort": (last or {}).get("effort"), "stage": (last or {}).get("stage")}
        new_profile = step.get("profile_id") or (last or {}).get("profile_id")
        new = {**self._profile_effort(new_profile), "stage": step["stage"], "mode": step["mode"],
               "diagnose_profile_id": step.get("diagnose_profile_id")}
        entry = rx.ledger_entry(
            escalation_id=escalation_id, run_id=self.run_id, iteration_id=it["iteration_id"], finding_ids=keys,
            previous=previous, new=new, reason=str(step["reason"]) + (f" ({step['note']})" if step.get("note") else ""),
            previous_result={"remaining_finding_keys": list(keys), "origin": origin,
                             "summary": (last or {}).get("summary"),
                             "signals": (assessment or {}).get("signals", [])},
            code_state_changed=(last or {}).get("code_changed"),
            evidence_state_changed=(last or {}).get("evidence_changed"),
            signals=(assessment or {}).get("signals", []), at=_now())
        self._ledger_append(entry)
        it.setdefault("repair_escalations", []).append({"escalation_id": escalation_id, "stage": step["stage"],
                                                         "disposition": rx.DISP_PENDING})
        self.journal.append("REPAIR_ESCALATED", iteration_id=it["iteration_id"], phase=self.state["phase"], payload={
            "escalation_id": escalation_id, "stage": step["stage"], "from": previous, "to": new,
            "reason": entry["reason"], "finding_ids": list(keys)})
        return escalation_id

    def _ledger_result(self, last: Mapping[str, Any], assessment: Mapping[str, Any]) -> None:
        escalation_id = last.get("escalation_id")
        if not escalation_id:
            return
        self._ledger_append(rx.result_entry(
            escalation_id=escalation_id,
            new_result={"remaining_finding_keys": assessment["unchanged_keys"] + assessment["new_keys"],
                        "resolved_keys": assessment["resolved_keys"], "summary": last.get("summary"),
                        "profile_id": last.get("profile_id")},
            code_state_changed=last.get("code_changed"), evidence_state_changed=last.get("evidence_changed"),
            signals=assessment["signals"], at=_now()))

    def _ledger_dispositions(self, disposition: str, detail: str | None) -> None:
        if not self.state.get("iterations"):
            return
        for row in self._it().get("repair_escalations", []):
            if row["disposition"] == rx.DISP_PENDING:
                row["disposition"] = disposition
                self._ledger_append(rx.disposition_entry(escalation_id=row["escalation_id"],
                                                         disposition=disposition, detail=detail, at=_now()))

    # REPAIR ------------------------------------------------------------------

    def _repair_packet(self, origin: Mapping[str, Any], step: Mapping[str, Any]) -> dict[str, Any]:
        it = self._it()
        mandate = self.state["mandate"]["iteration_contract"]
        return rx.build_repair_packet(
            findings=origin["findings"], criteria=list((it.get("plan") or {}).get("acceptance_criteria", [])),
            checks=it.get("checks", []), evidence_state=self._checks(), changed_files=self.env.changed_files(),
            diff=self.env.diff(), prior_attempts=it["repairs"], step=step,
            known_limitations=[*mandate.get("known_limitations", []), *it.get("classified", [])],
            diagnoses=it.get("diagnoses", []))

    def _diagnose(self, origin: Mapping[str, Any], step: Mapping[str, Any], packet: dict[str, Any],
                  attempt: int) -> dict[str, Any] | None:
        """Read-only root-cause analysis by the step's diagnosing profile (optional executor)."""
        it = self._it()
        last = it["repairs"][-1] if it["repairs"] else {}
        selection = ap.select_repair_step({**step, "stage": step["stage"]}, profile_id=step["diagnose_profile_id"],
                                          previous_profile_id=last.get("profile_id"),
                                          evidence=[step["reason"]])
        before = diff_digest(self.env.diff())
        result = self._call("diagnose", "diagnostician", {
            "iteration": it, "plan": it["plan"], "findings": origin["findings"], "attempt": attempt,
            "step": step, "repair_packet": packet, "model_selection": selection})
        if self.state["status"] != ac.RUNNING:
            return None
        if diff_digest(self.env.diff()) != before:
            self._escalate(ac.E_VERIFY_MUTATION, "the worktree diff changed during DIAGNOSE (a read-only phase)",
                           it["iteration_id"])
            return None
        diagnosis = result.get("diagnosis") if isinstance(result, dict) and isinstance(result.get("diagnosis"), dict) \
            else result if isinstance(result, dict) and result.get("root_cause") else None
        execution = self.state["executions"][-1] if self.state.get("executions") else {}
        self._done(attempt=attempt, diagnose=True)
        if diagnosis and str(diagnosis.get("root_cause") or "").strip():
            record = {"by": execution.get("profile"), "attempt": attempt, "root_cause": str(diagnosis["root_cause"]),
                      "next_actions": list(diagnosis.get("next_actions") or []),
                      "classification": diagnosis.get("classification"), "execution_id": execution.get("execution_id")}
            it.setdefault("diagnoses", []).append(record)
            return record
        return None

    def _do_repair(self) -> None:
        it = self._it()
        origin = it["repair_origin"]
        step = origin.get("step") or {"stage": rx.STAGE_CURRENT, "profile_id": None, "mode": rx.MODE_REPAIR,
                                      "reason": "LEGACY", "diagnose_profile_id": None}
        it["repair_attempts"] += 1
        attempt = it["repair_attempts"]
        last = it["repairs"][-1] if it["repairs"] else None
        before_diff, before_files = diff_digest(self.env.diff()), list(self.env.changed_files())
        before_evidence = dict(self._checks())
        seen_triples = {rx._h([c.get("name"), str(c.get("status", "")).upper(), c.get("summary")])
                        for c in it.get("checks", [])}
        seen_refs = {str(c["log_ref"]) for c in it.get("checks", []) if c.get("log_ref")} | set(it.get("evidence_refs", []))
        seen_dx = {rx._h(str(d.get("root_cause", "")).strip().casefold()) for d in it.get("diagnoses", [])}
        # Later attempts get a compact packet instead of the whole iteration context.
        packet = self._repair_packet(origin, step) if (last is not None or step["stage"] != rx.STAGE_CURRENT) else None
        if step.get("diagnose_profile_id") and self.executors.get("diagnose") is not None:
            diagnosis = self._diagnose(origin, step, packet or self._repair_packet(origin, step), attempt)
            if self.state["status"] != ac.RUNNING:
                return
            if packet is not None and diagnosis:
                packet["PRIOR_DIAGNOSES"] = [*packet.get("PRIOR_DIAGNOSES", []), {
                    "by": diagnosis["by"], "root_cause": diagnosis["root_cause"],
                    "next_actions": diagnosis["next_actions"][:5]}]
            elif packet is None and diagnosis:
                packet = self._repair_packet(origin, step)
        ctx: dict[str, Any] = {"iteration": it, "plan": it["plan"], "findings": origin["findings"],
                               "attempt": attempt, "step": step, "repair_mode": step["mode"],
                               "repair_packet": packet}
        if step.get("profile_id"):
            ctx["model_selection"] = ap.select_repair_step(
                step, profile_id=step["profile_id"], previous_profile_id=(last or {}).get("profile_id"),
                evidence=[step["reason"]])
            ctx["min_capability_class"] = self._capability_class(step["profile_id"])
        # EXACT_ALLOWED_REPAIR_SCOPE: only the findings, never the goal.
        result = self._call("repair", "repairer", ctx)
        if self.state["status"] != ac.RUNNING:
            return
        if not (isinstance(result, dict) and isinstance(result.get("summary"), str)):
            self._escalate(ac.E_EXECUTOR, "repair returned no structured result", it["iteration_id"])
            return
        self._record_checks(result.get("checks"))
        self._record_self_audit(result, "REPAIR")
        accepted, rejected = self._apply_reclassifications(result)
        found = rx.repair_signals(result, evidence_before=before_evidence, seen_check_triples=seen_triples,
                                  seen_evidence_refs=seen_refs, seen_diagnosis_hashes=seen_dx)
        signals = list(dict.fromkeys([*found["signals"], *rx.classification_signals(accepted)]))
        it.setdefault("evidence_refs", [])
        it["evidence_refs"] = sorted({*it["evidence_refs"], *found["detail"].get("new_evidence_refs", [])})
        diagnosis = result.get("diagnosis") if isinstance(result.get("diagnosis"), dict) else None
        if diagnosis and str(diagnosis.get("root_cause") or "").strip():
            it.setdefault("diagnoses", []).append({
                "by": None, "attempt": attempt, "root_cause": str(diagnosis["root_cause"]),
                "next_actions": list(diagnosis.get("next_actions") or []),
                "classification": diagnosis.get("classification")})
        summary_hash = rx._h(result["summary"].strip().casefold())
        repeated = bool(last) and last.get("summary_hash") == summary_hash and not signals
        after_files = list(self.env.changed_files())
        record = {"attempt": attempt, "origin": origin["origin"], "stage": step["stage"], "mode": step["mode"],
                  "addresses": sorted(f["finding_key"] for f in origin["findings"]),
                  "summary": result["summary"], "summary_hash": summary_hash,
                  "changed_files": result.get("changed_files", []),
                  "observed_files_delta": sorted(set(after_files) ^ set(before_files)),
                  "code_changed": diff_digest(self.env.diff()) != before_diff,
                  "evidence_changed": dict(self._checks()) != before_evidence or bool(accepted),
                  "signals": signals, "signal_detail": found["detail"], "repeated_response": repeated,
                  "reclassifications": {"accepted": accepted, "rejected": rejected},
                  "diagnosis_provided": bool(diagnosis and str(diagnosis.get("root_cause") or "").strip()),
                  "escalation_id": step.get("escalation_id"), "outcome": None, "at": _now()}
        it["repairs"].append(record)
        if step["mode"] == rx.MODE_DIAGNOSE_THEN_REPAIR and not record["diagnosis_provided"] \
                and not it.get("diagnoses"):
            self.journal.append("REPAIR_DIAGNOSIS_MISSING", iteration_id=it["iteration_id"], phase=ac.REPAIR,
                                payload={"attempt": attempt, "stage": step["stage"]})
        # Deviations and uncertainties a repair reports join the iteration's record.
        for key in ("deviations", "uncertainties", "unresolved"):
            if result.get(key):
                it["execution"][key] = list(it["execution"].get(key, [])) + list(result[key])
        repair_execution = self.state["executions"][-1] if self.state.get("executions") else {}
        record.update({"profile_id": repair_execution.get("profile"), "model": repair_execution.get("model"),
                       "effort": repair_execution.get("effort"),
                       "execution_id": repair_execution.get("execution_id"),
                       "selection": repair_execution.get("selection")})
        self.journal.append("REPAIR_COMPLETED", iteration_id=it["iteration_id"], phase=ac.REPAIR, payload={
            "attempt": attempt, "origin": origin["origin"], "addresses": record["addresses"],
            "stage": step["stage"], "mode": step["mode"], "signals": signals,
            "code_changed": record["code_changed"], "evidence_changed": record["evidence_changed"],
            "repeated_response": repeated,
            "repaired_by": self._execution_audit(repair_execution.get("execution_id")),
            "execution_id": repair_execution.get("execution_id")})
        self._done(attempt=attempt)
        # Every repair returns through deterministic/semantic self-verification
        # and the primary review pipeline before a fresh final review.
        self._goto(ac.SELF_VERIFY)
        self._save()

    def _capability_class(self, profile_id: str) -> int | None:
        row = ((self.routing_cfg or {}).get("profiles") or {}).get(profile_id) or {}
        value = row.get("capability_class")
        return value if type(value) is int else None

    def _apply_reclassifications(self, result: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Validate evidence-backed reclassifications and stale-check supersession reported by a repair.

        A failing check can leave the blocking set only through a newer PASS
        for the same name, or a validated, acceptance-neutral classification
        with an evidence reference. Everything stays visible to reviewers.
        """
        it = self._it()
        rows = [dict(r) for r in (result.get("reclassifications") or []) if isinstance(r, Mapping)]
        passing = {str(c.get("name")) for c in (result.get("checks") or [])
                   if isinstance(c, Mapping) and str(c.get("status", "")).upper() == "PASS"}
        for check in result.get("checks") or []:
            if isinstance(check, Mapping) and str(check.get("status", "")).upper() == "PASS":
                for stale in check.get("supersedes") or []:
                    rows.append({"check_name": stale, "classification": rx.C_SUPERSEDED, "superseded_by": check.get("name"),
                                 "evidence_ref": check.get("log_ref") or f"CHECK:{check.get('name')}",
                                 "explanation": f"re-checked by {check.get('name')!r}: {check.get('summary')}",
                                 "acceptance_impact": "NONE"})
        failing = ac.evidence_failures(self._checks())
        accepted, rejected = rx.validate_reclassifications(rows, failing_names=failing, passing_names=passing)
        for row in accepted:
            self._it().setdefault("evidence_state", {})[row["check_name"]] = row["status"]
            self._it().setdefault("checks", []).append({
                "name": row["check_name"], "status": row["status"], "log_ref": row["evidence_ref"],
                "summary": f"{row['classification']}: {row['explanation']}", "classification": row["classification"]})
            it.setdefault("classified", []).append({"id": row["check_name"], "classification": row["classification"],
                                                     "description": row["explanation"],
                                                     "evidence_ref": row["evidence_ref"]})
        if accepted or rejected:
            self.journal.append("REPAIR_RECLASSIFIED", iteration_id=it["iteration_id"], phase=ac.REPAIR,
                                payload={"accepted": accepted, "rejected": rejected})
        return accepted, rejected

    # FINAL_REVIEW ------------------------------------------------------------

    def _do_final_review(self) -> None:
        it = self._it()
        packet = self._build_packet("FINAL_REVIEW")  # fresh: it must describe the state after repairs
        if self.state["status"] != ac.RUNNING:
            return
        it["packet"] = packet
        selection = self._policy_selection("final_review", {})
        it["final_review_selection"] = dict(selection) if selection else None
        self._save()
        raw = self._review_with_raw_access("final_review", "final_reviewer",
                                           self._reviewer_context(packet, "FINAL_REVIEW"), selection)
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
                row = self.state["roadmap"][item_id]
                if row.get("recurring") is True:
                    # A standing item records progress but stays pending: ending it is the planner's
                    # explicit, reasoned skip (or an execution fuse), never a side effect of one PASS.
                    row.setdefault("iterations", []).append(it["iteration_id"])
                    row["last_iteration_id"] = it["iteration_id"]
                    continue
                self.state["roadmap"][item_id] = {"status": ac.R_DONE, "iteration_id": it["iteration_id"], "reason": None}
            ac.refresh_dependency_states(self.state["roadmap"])
            self._ledger_dispositions(rx.DISP_RESOLVED, "iteration accepted by final review")
            self.journal.append("ITERATION_ACCEPTED", iteration_id=it["iteration_id"], phase=ac.FINAL_REVIEW, payload={
                "classified_limitations": list(it.get("classified", [])),
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
        autonomous_remaining = ac.autonomous_remaining_items(self.state["roadmap"])
        cap = self.state["mandate"]["roadmap_mandate"]["autonomy_bounds"]["max_iterations"]
        done = len(self.state["iterations"])
        human_gated = [item_id for item_id, item in self.state["roadmap"].items()
                       if item.get("status") == ac.R_HUMAN_REQUIRED or
                       item.get("dependency_state") in {"HUMAN_REQUIRED", "BLOCKED_BY_SKIPPED_DEPENDENCY"}]
        payload = {"remaining": remaining, "autonomous_remaining": autonomous_remaining,
                   "human_gated": human_gated, "iterations_done": done, "max_iterations": cap}
        if not autonomous_remaining:
            self.journal.append("ROADMAP_DECISION", phase=ac.ROADMAP_CHECK, payload={
                **payload, "next_action_available": False, "roadmap_exhausted": True,
                "reason": "no valid autonomous roadmap item remains; remaining human-gated items are preserved"})
            self._done(next="AWAITING_HUMAN")
            self._await_human(ac.HOLD_ROADMAP_EXHAUSTED,
                              "no valid autonomous roadmap item remains; human-gated items: " +
                              (", ".join(human_gated) if human_gated else "none"))
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


def adopt_timed_out_side_effect_result(
        run_id: str, *, execution_id: str, result: Mapping[str, Any], env: WorkspaceEnvironment,
        operator: str, stats_root: Path | None = None) -> dict[str, Any]:
    """Adopt verified partial work after an EXECUTE/REPAIR process timeout.

    This is an explicit operator recovery action.  It never re-dispatches the
    side-effecting call, never rewrites its TIMEOUT ledger close, and resumes at
    SELF_VERIFY so the preserved work still passes the normal evidence and
    independent-review pipeline.
    """
    root = Path(stats_root or STATS_ROOT)
    if not isinstance(operator, str) or not operator.strip():
        raise ac.AutonomyError("timeout recovery requires an operator identity")
    if not isinstance(result, Mapping) or not isinstance(result.get("summary"), str):
        raise ac.AutonomyError("timeout recovery requires a structured implementation result")
    with _human_surface_lock(run_id, root):
        state = load_state(run_id, root)
        escalation = state.get("escalation") or {}
        if state.get("status") != ac.AWAITING_HUMAN or state.get("phase") != ac.AWAITING_HUMAN:
            raise ac.AutonomyError("timeout recovery is only possible from AWAITING_HUMAN")
        if escalation.get("from_phase") not in ac.SIDE_EFFECT_PHASES:
            raise ac.AutonomyError("timeout recovery requires an EXECUTE or REPAIR escalation")
        if escalation.get("code") not in {ac.E_EXECUTOR, ac.E_EXECUTOR_TIMEOUT}:
            raise ac.AutonomyError("timeout recovery requires an executor timeout escalation")
        execution = next((row for row in reversed(state.get("executions", []))
                          if row.get("execution_id") == execution_id), None)
        if not execution or execution.get("phase") != escalation.get("from_phase") \
                or execution.get("iteration_id") != escalation.get("iteration_id"):
            raise ac.AutonomyError("execution does not match the escalated side-effecting phase")
        close = ExecutionLedger.for_run(run_id, root).close_status(execution_id) or {}
        if close.get("close_reason") != "TIMEOUT" or close.get("exit_code") != 124:
            raise ac.AutonomyError("execution ledger does not prove a provider timeout with exit code 124")
        env.restore(state.get("workspace_checkpoint") or {})
        env.assert_safe()
        actual_files = env.changed_files()
        if not actual_files:
            raise ac.AutonomyError("timeout recovery found no preserved worktree changes")
        reported_files = result.get("changed_files")
        if reported_files is not None and set(reported_files) != set(actual_files):
            raise ac.AutonomyError("recovered result changed_files do not match the current worktree")

        recovery_id = "REC_" + uuid.uuid4().hex
        recovery_dir = autonomy_dir(run_id, root) / "RECOVERY" / recovery_id
        recovery_dir.mkdir(parents=True, exist_ok=False)
        state_path = autonomy_dir(run_id, root) / "autonomy_state.json"
        events_path = autonomy_dir(run_id, root) / "autonomy_events.jsonl"
        (recovery_dir / "autonomy_state.before.json").write_bytes(state_path.read_bytes())
        if events_path.exists():
            (recovery_dir / "autonomy_events.before.jsonl").write_bytes(events_path.read_bytes())

        adopted = json.loads(json.dumps(dict(result), ensure_ascii=False, default=str))
        adopted["changed_files"] = actual_files
        adopted["recovered_after_timeout"] = True
        adopted["source_execution_id"] = execution_id
        iteration = next((row for row in state.get("iterations", [])
                          if row.get("iteration_id") == escalation.get("iteration_id")), None)
        if iteration is None:
            raise ac.AutonomyError("escalated iteration is missing")
        if iteration.get("execution") is not None:
            raise ac.AutonomyError("iteration already has an adopted implementation result")
        checks = [dict(row) for row in adopted.get("checks", []) if isinstance(row, Mapping)]
        iteration["execution"] = adopted
        iteration["executed_by"] = {key: execution.get(key) for key in
                                    ("role", "profile", "model", "effort", "execution_id", "selection")}
        for row in checks:
            iteration.setdefault("evidence_state", {})[str(row.get("name"))] = str(row.get("status", "")).upper()
            iteration.setdefault("checks", []).append(row)
        recovery = {
            "recovery_id": recovery_id, "at": _now(), "operator": operator.strip(),
            "kind": "TIMED_OUT_SIDE_EFFECT_RESULT_ADOPTED", "execution_id": execution_id,
            "iteration_id": iteration["iteration_id"], "from_phase": escalation["from_phase"],
            "ledger_close": close, "changed_files": actual_files, "backup_dir": str(recovery_dir),
            "previous_escalation": escalation, "previous_hold": state.get("hold"),
        }
        (recovery_dir / "recovered_result.json").write_text(
            json.dumps(adopted, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        state.setdefault("recovery_history", []).append(recovery)
        state["status"], state["phase"] = ac.RUNNING, ac.SELF_VERIFY
        state["escalation"], state["hold"], state["in_flight"] = None, None, None
        _save_state(run_id, state, root)
        AutonomyJournal(events_path, run_id).append(
            "TIMEOUT_PARTIAL_PROGRESS_ADOPTED", iteration_id=iteration["iteration_id"], phase=ac.SELF_VERIFY,
            payload={key: recovery[key] for key in ("recovery_id", "operator", "execution_id", "from_phase",
                                                     "changed_files", "backup_dir")})
        return state


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
