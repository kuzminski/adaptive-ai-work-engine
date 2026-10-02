#!/usr/bin/env python3
"""AAW AUTONOMOUS ITERATIONS V0.1 — the iteration controller.

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
from aaw_paths import AAW_ROOT, STATS_ROOT

ROLES_PATH = AAW_ROOT / "AUTONOMY_ROLES.json"
PROFILES_PATH = AAW_ROOT / "IMPLEMENTER_PROFILES.json"


class ExecutorFailure(RuntimeError):
    """An executor reporting that it cannot continue (as opposed to crashing).

    Becomes an ESCALATE. Any *other* exception propagates and leaves the
    in-flight marker in the state, which is exactly what `resume` keys on.
    """


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
    def assert_safe(self) -> None:
        """Raise `GitPolicyViolation` if the Git boundary was crossed."""
    def describe(self) -> dict[str, Any]: return {}


class GitWorkspaceEnvironment(WorkspaceEnvironment):
    """The real thing, built on the existing runner guards (not re-implemented).

    Reuses `validate_workspace` (isolated registered worktree, clean canonical
    checkout, same git common dir) and `assert_main_unchanged`, and adds a
    protected-ref snapshot so a merge/push done by *any* subprocess is noticed
    at the next phase boundary.
    """

    def __init__(self, repo: Path, worktree: Path) -> None:
        import workflow_runner as wr
        self._wr = wr
        try:
            self.baseline = wr.validate_workspace(Path(repo), Path(worktree))
        except wr.WorkflowStop as exc:
            raise ac.GitPolicyViolation(str(exc)) from exc
        self.worktree = wr.canonical(Path(worktree))
        self._refs = self._protected_refs()

    def _protected_refs(self) -> str:
        patterns = [f"refs/heads/{b}" for b in ac.PROTECTED_BRANCHES] + \
                   [f"refs/remotes/*/{b}" for b in ac.PROTECTED_BRANCHES]
        return self._wr.git(Path(self.baseline["repo"]), "for-each-ref", "--format=%(refname) %(objectname)", *patterns)

    def head(self) -> str | None:
        return self._wr.git(self.worktree, "rev-parse", "HEAD")

    def diff(self) -> str:
        return self._wr.git_diff(self.worktree)

    def changed_files(self) -> list[str]:
        return self._wr.changed_files(self.worktree)

    def assert_safe(self) -> None:
        try:
            self._wr.assert_main_unchanged(self.baseline)
            branch = self._wr.git(self.worktree, "rev-parse", "--abbrev-ref", "HEAD")
            if branch in ac.PROTECTED_BRANCHES:
                raise ac.GitPolicyViolation(f"worktree is on protected branch {branch!r}")
            if self._protected_refs() != self._refs:
                raise ac.GitPolicyViolation("a protected branch or its remote-tracking ref moved (merge or push)")
        except self._wr.WorkflowStop as exc:
            raise ac.GitPolicyViolation(str(exc)) from exc

    def describe(self) -> dict[str, Any]:
        return {"worktree": str(self.worktree), "base_head": self.baseline["worktree_head"]}


def diff_digest(text: str) -> str:
    return ac.canonical_hash({"diff": text})


# ── the controller ───────────────────────────────────────────────────────────

EXECUTOR_NAMES = ("plan", "execute", "self_verify", "review", "repair", "final_review")


class AutonomyController:
    """Drive iterations to AWAITING_HUMAN. Never past it.

    `executors` maps role operations to callables `(ctx) -> dict`; they are the
    model/harness boundary (production binds them to the DIRECT_CLI_CONTROL
    adapters, tests script them). `prepare_packet` is optional compression; the
    deterministic packet is always built and always keeps the bad news.
    """

    def __init__(self, run_id: str, *, executors: Mapping[str, Callable[[dict], dict]],
                 env: WorkspaceEnvironment, roles: Mapping[str, Mapping[str, Any]],
                 stats_root: Path | None = None) -> None:
        missing = [n for n in EXECUTOR_NAMES if n not in executors]
        if missing:
            raise ac.AutonomyError(f"missing executors: {missing}")
        self.run_id, self.executors, self.env, self.roles = run_id, dict(executors), env, dict(roles)
        self.dir = autonomy_dir(run_id, stats_root)
        self.state_path = self.dir / "autonomy_state.json"
        self.journal = AutonomyJournal(self.dir / "autonomy_events.jsonl", run_id)
        self.state: dict[str, Any] = {}

    # lifecycle ---------------------------------------------------------------

    @classmethod
    def start(cls, run_id: str, mandate: Any, **kwargs: Any) -> "AutonomyController":
        self = cls(run_id, **kwargs)
        if self.state_path.exists():
            raise ac.AutonomyError(f"run {run_id} already exists; use resume")
        frozen = ac.validate_mandate(mandate)
        self.state = {
            "schema_version": ac.SCHEMA_VERSION, "contract": ac.CONTRACT_ID, "run_id": run_id,
            "status": ac.RUNNING, "phase": ac.PLAN, "mandate": frozen, "mandate_hash": frozen["mandate_hash"],
            "roadmap": ac.initial_roadmap(frozen), "iterations": [], "roles": dict(self.roles),
            "in_flight": None, "hold": None, "escalation": None, "human": None, "promotion": None,
            "started_at": _now(), "updated_at": _now(), "main_merge_allowed": False,
        }
        self.journal.append("MANDATE_FROZEN", payload={
            "mandate_id": frozen["mandate_id"], "mandate_hash": frozen["mandate_hash"],
            "roadmap_items": [i["item_id"] for i in frozen["roadmap_mandate"]["items"]],
            "bounds": frozen["roadmap_mandate"]["autonomy_bounds"], "roles": self._role_audit()})
        self._save()
        return self

    @classmethod
    def resume(cls, run_id: str, *, stats_root: Path | None = None, **kwargs: Any) -> "AutonomyController":
        self = cls(run_id, stats_root=stats_root, **kwargs)
        self.state = load_state(run_id, stats_root)
        self.journal.append("RUN_RESUMED", phase=self.state["phase"], payload={
            "status": self.state["status"], "in_flight": self.state.get("in_flight")})
        flight = self.state.get("in_flight")
        if flight and self.state["status"] == ac.RUNNING:
            if flight["phase"] in ac.SIDE_EFFECT_PHASES:
                # The worktree may already hold half of this phase's effects.
                # Never repeat it blindly (same rule as workflow V0.2).
                self._escalate(ac.E_INTERRUPTED, f"{flight['phase']} was interrupted mid-flight; its worktree "
                               "effects are unknown and it is not re-run automatically", flight["iteration_id"])
            else:
                self.state["in_flight"] = None  # read-only phase: safe to simply run again
                self._save()
        return self

    def run(self) -> dict[str, Any]:
        handlers = {ac.PLAN: self._do_plan, ac.EXECUTE: self._do_execute, ac.SELF_VERIFY: self._do_self_verify,
                    ac.AWAITING_REVIEW: self._do_prepare_review, ac.REVIEW: self._do_review,
                    ac.REPAIR: self._do_repair, ac.FINAL_REVIEW: self._do_final_review,
                    ac.ROADMAP_CHECK: self._do_roadmap_check}
        while self.state["status"] == ac.RUNNING:
            phase = self.state["phase"]
            try:
                self.env.assert_safe()
            except ac.GitPolicyViolation as exc:
                self._escalate(ac.E_GIT, str(exc), self._iteration_id())
                break
            handlers[phase]()
        return self.state

    # small helpers -----------------------------------------------------------

    def _save(self) -> None:
        self.state["updated_at"] = _now()
        _atomic_json(self.state_path, self.state)

    def _role_audit(self) -> dict[str, Any]:
        return {r: {k: b.get(k) for k in ("role", "profile_id", "runtime_model_id", "effort", "review_independence")}
                for r, b in self.roles.items()}

    def _it(self) -> dict[str, Any]:
        return self.state["iterations"][-1]

    def _iteration_id(self) -> str | None:
        return self._it()["iteration_id"] if self.state["iterations"] else None

    def _goto(self, phase: str) -> None:
        current = self.state["phase"]
        if not ac.transition_allowed(current, phase):
            raise ac.AutonomyError(f"illegal transition {current} -> {phase}")
        self.state["phase"] = phase

    def _call(self, name: str, role: str, ctx: dict[str, Any]) -> Any:
        """Invoke an executor with an in-flight marker around it."""
        phase = self.state["phase"]
        self.state["in_flight"] = {"phase": phase, "executor": name, "role": role,
                                   "iteration_id": self._iteration_id(), "started_at": _now()}
        self._save()
        self.journal.append("PHASE_STARTED", iteration_id=self._iteration_id(), phase=phase,
                            payload={"role": role, "binding": self._role_audit().get(role)})
        ctx = {**ctx, "role": role, "binding": dict(self.roles[role]), "phase": phase,
               "mandate": self.state["mandate"], "env": self.env}
        try:
            return self.executors[name](ctx)
        except ExecutorFailure as exc:
            self.state["in_flight"] = None
            self._escalate(ac.E_EXECUTOR, f"{name} executor failed: {exc}", self._iteration_id())
            return None

    def _done(self, **payload: Any) -> None:
        self.state["in_flight"] = None
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
        iteration_id = f"{self.run_id}_IT{index:02d}"
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
            "started_at": _now(), "finished_at": None})
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
        result = self._call("self_verify", "implementer", {"iteration": self._it(), "plan": self._it()["plan"]})
        if self.state["status"] != ac.RUNNING:
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
            checks=it["checks"], adverse=adverse, kind=kind)
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
        return {"iteration": self._it(), "packet": packet, "review_kind": kind,
                "raw": {"diff": diff, "diff_sha256": diff_digest(diff), "changed_files": self.env.changed_files(),
                        "head": self.env.head(), "evidence": list(self._it()["checks"])}}

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
            "reviewed_by": self._role_audit()["reviewer" if phase == ac.REVIEW else "final_reviewer"]})

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
        result = self._call("repair", "implementer", {
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
            "repaired_by": self._role_audit()["implementer"]})
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
        candidate_id = "CAND_" + ac.canonical_hash({"m": self.state["mandate_hash"], "head": head,
                                                    "its": [i["iteration_id"] for i in accepted]})[7:23]
        self.state["phase"] = ac.AWAITING_HUMAN
        self.state["status"] = ac.AWAITING_HUMAN
        self.state["hold"] = {"reason": reason, "detail": detail, "roadmap_exhausted": exhausted,
                              "promotable": bool(accepted), "candidate_id": candidate_id, "candidate_head": head,
                              "accepted_iterations": [i["iteration_id"] for i in accepted]}
        self.journal.append("AWAITING_HUMAN", phase=ac.AWAITING_HUMAN, payload=dict(self.state["hold"]))
        self._save()


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


def approve_promotion(run_id: str, *, approver: str, candidate_id: str, early_end: bool = False,
                      channel: str = "HUMAN", note: str | None = None,
                      stats_root: Path | None = None) -> dict[str, Any]:
    """Record the human's explicit acceptance of one exact candidate."""
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
            stats_root: Path | None = None) -> dict[str, Any]:
    """PROMOTE — reachable only from a recorded human approval.

    With no `promoter` (the default) this authorizes and records the candidate
    as READY_FOR_EXTERNAL_INTEGRATION, exactly the semantics of the existing
    Human Gate: AAW itself still merges and pushes nothing. A `promoter` is the
    explicit, human-initiated integration hook; it receives a one-shot token
    that `GuardedGit` requires before it will run merge or push.
    """
    state = load_state(run_id, stats_root)
    human = state.get("human") or {}
    if state["phase"] != ac.HUMAN_APPROVED or human.get("verdict") != "APPROVED" or not human.get("approval_id"):
        raise ac.AutonomyError("PROMOTE requires a recorded human approval (HUMAN_APPROVED)")
    journal = AutonomyJournal(autonomy_dir(run_id, stats_root) / "autonomy_events.jsonl", run_id)
    state["phase"] = ac.PROMOTE
    token = ac.PromotionToken(run_id, human["approval_id"])
    integration: Mapping[str, Any] = {"status": "READY_FOR_EXTERNAL_INTEGRATION", "merged": False, "pushed": False}
    if promoter is not None:
        integration = dict(promoter(token, state))
    state["promotion"] = {"approval_id": human["approval_id"], "integration": dict(integration), "at": _now()}
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
